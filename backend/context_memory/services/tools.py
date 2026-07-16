"""Tool runtime: discover, validate, and execute installed tool packages
(see D21 in docs/web-v2-decisions.md).

A tool package is a directory ``<tools_dir>/<name>/`` holding:

* ``tool.json`` -- ``{"name", "description", "parameters", "entry", "enabled"?}``
  where ``name`` MUST equal the directory name and match
  ``^[a-z0-9][a-z0-9_-]{0,63}$``, ``description`` is a nonempty string,
  ``parameters`` is a JSON-Schema object for the arguments, ``entry`` is an argv
  list (e.g. ``["python3", "run.py"]``), and ``enabled`` (optional, default
  true) gates whether the tool is advertised to the model;
* the implementation files ``entry`` runs;
* an OPTIONAL ``.env`` (``KEY=VALUE`` lines) holding THAT tool's own secrets
  (e.g. a KB API key), which are injected into the subprocess environment.

Execution contract (``_run_tool_subprocess``): the runner invokes ``entry`` with
``cwd`` = the tool directory, writes the arguments JSON to the child's STDIN,
reads STDOUT as the tool result (capped at ``settings.llm_tool_output_max_chars``
behind a truncation marker), and enforces ``settings.llm_tool_timeout_seconds``
by killing the child's whole PROCESS GROUP on expiry. A non-zero exit becomes an
agent-visible ``"tool failed (exit N): <stderr>"`` result -- a failure the model
can react to, never an exception that 502s the interaction.

SECURITY STANCE (D21, v1). These are the operator's OWN local tools and shell
capability is an explicit requirement (same nature as a Claude Code skill), so
v1 does NOT sandbox with a container. What it DOES guarantee:

* the subprocess environment is built FROM SCRATCH -- only PATH/HOME/LANG/LC_ALL/
  TMPDIR are passed through from the parent, plus the tool's own ``.env``. The
  parent environment is NEVER inherited wholesale, because it carries
  ``OPENAI_API_KEY`` (and any other backend secret): a generated, possibly
  half-trusted tool must not be able to read our LLM credentials out of its own
  ``os.environ``. This is the single most important guarantee in this module;
* every mutating entry point (``set_enabled`` / ``delete_tool``) validates the
  name against the package-name regex AND re-checks resolved-path containment
  under ``tools_dir`` before touching the filesystem, so a traversal name like
  ``"../.."`` (or a symlinked package escaping the tools dir) can never make us
  rewrite or ``rmtree`` a path outside the tools directory;
* runtime and output are bounded (timeout + process-group kill, output cap), so
  a hung or runaway tool cannot pin the interaction or blow the prompt/log.

Settings-driven, exactly like ``llm_log``: ``tools_dir`` unset ("") means the
whole feature is OFF -- ``list_tools()`` / ``enabled_llm_tools()`` return empty,
so the AI workflows advertise no tools and their prompts stay byte-identical to
the tool-less build.
"""

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from starlette.concurrency import run_in_threadpool

from context_memory.config import get_settings
from context_memory.services.llm import LlmTool

# A package (and directory) name: lowercase alnum start, then up to 63 more of
# alnum/underscore/hyphen. Admits no ".", "/", or whitespace, so a traversal
# name (``".."``, ``"../evil"``, ``"/etc/x"``) can never match -- the regex is
# the first line of the path-traversal defense, backed by the resolved-path
# containment check in ``_resolve_package_dir``.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# The ONLY parent-environment variables a tool subprocess inherits. Everything
# else -- above all OPENAI_API_KEY / OPENAI_BASE_URL -- is withheld by building
# the child env from scratch (see the module docstring's security stance). PATH
# lets the child find its interpreter; HOME/LANG/LC_ALL/TMPDIR keep ordinary
# tooling (python, locale-aware libs, temp files) behaving normally.
_PASSTHROUGH_ENV: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")

# Uniform cap on a tool's description (applied once at scan time, so list_tools
# and the advertised OpenAI spec always agree). A package with a longer
# description is still VALID -- only the stored/advertised copy is trimmed -- so
# an over-long description never bloats the tools array sent to the model.
_DESCRIPTION_CAP = 1000

# Appended when a tool's stdout (or a failure's stderr) is cut for size. Mirrors
# the truncation markers in memory_ai / llm_log so an operator who has seen those
# recognizes this one; the distinct wording ("工具輸出" = tool output) tells it
# apart from a truncated item section or a truncated log body.
_OUTPUT_TRUNCATION_MARKER = "…[工具輸出過長已截斷]"

# How long ``communicate`` is given to drain pipes and reap AFTER the process
# group has already been SIGKILLed on timeout. The group is dead, so this
# returns effectively immediately; it exists only so a wedged pipe can never
# turn cleanup itself into a hang.
_REAP_TIMEOUT_SECONDS = 5.0


@dataclass(slots=True)
class _PackageScan:
    """The result of validating one candidate package directory.

    ``valid`` gates execution: an invalid package is listed (so the UI can show
    WHY, via ``error``) but never advertised to the model or executed.
    ``parameters`` / ``entry`` are populated only when ``valid`` is True.
    ``enabled`` is read as early as possible (right after the JSON parses) so
    even an otherwise-invalid package reports the toggle state the operator set.
    """

    name: str
    directory: Path
    valid: bool
    enabled: bool
    description: str
    error: str | None
    parameters: dict[str, Any] | None
    entry: list[str] | None


# --- discovery / validation ------------------------------------------------


def tools_dir() -> Path | None:
    """The configured tools directory, or None when the feature is disabled.

    ``settings.tools_dir`` empty ("") means OFF -- None here makes every reader
    (``list_tools`` / ``enabled_llm_tools`` / the mutators) a no-op. Mirrors
    ``llm_log``'s settings-driven pattern; packaged mode's cli.py injects
    ``<data-dir>/tools`` so a uvx install has it set without operator action.
    """
    raw = get_settings().tools_dir.strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _valid_entry(entry: Any) -> bool:
    """True when ``entry`` is a non-empty argv list of non-empty strings."""
    return (
        isinstance(entry, list)
        and len(entry) > 0
        and all(isinstance(part, str) and part for part in entry)
    )


def _is_within(base: Path, candidate: Path) -> bool:
    """True when the RESOLVED ``candidate`` lies at or under the RESOLVED ``base``.

    ``relative_to`` raises ValueError when it does not -- the containment test
    used both to validate an entry file (it must live inside its package) and to
    hard-block a package path that escapes ``tools_dir`` (a symlink, a traversal
    name that somehow reached here).
    """
    try:
        candidate.relative_to(base)
    except ValueError:
        return False
    return True


def _entry_file_exists(directory: Path, entry: list[str]) -> bool:
    """True when some ``entry`` element names an existing file inside ``directory``.

    ``entry`` is argv, so its elements mix an interpreter (``python3`` -- found
    on PATH, not a file here), flags, and the script (``run.py`` -- a real file
    in the package). Rather than guess which element is the script, treat EACH as
    a candidate path relative to the package and require at least one to resolve
    (with containment) to an existing regular file. That accepts
    ``["python3","run.py"]`` and ``["./run.sh"]`` alike, while an absolute or
    ``..``-laden element resolves OUTSIDE the package and is correctly ignored --
    so a bare ``["python3"]`` (no script shipped) is reported as a missing entry
    file rather than silently "valid".
    """
    base = directory.resolve()
    for part in entry:
        candidate = (directory / part).resolve()
        if _is_within(base, candidate) and candidate.is_file():
            return True
    return False


def _scan_package(directory: Path, expected_name: str | None = None) -> _PackageScan:
    """Validate one candidate directory into a ``_PackageScan``.

    Every failure path returns ``valid=False`` with a short, safe reason (never
    a raw filesystem error string), so a single broken package can never break
    the scan of the others and is never executable.

    ``expected_name`` is the name the manifest's ``name`` field must equal; it
    defaults to the directory's own name (the installed-package invariant). The
    installer's staging validation passes the FUTURE name instead -- its staging
    directory is a throwaway uuid, but the manifest must already carry the name
    the package is about to be installed under (see ``validate_package``).
    """
    name = expected_name if expected_name is not None else directory.name

    def invalid(error: str, *, enabled: bool = True) -> _PackageScan:
        return _PackageScan(
            name=name,
            directory=directory,
            valid=False,
            enabled=enabled,
            description="",
            error=error,
            parameters=None,
            entry=None,
        )

    tool_json = directory / "tool.json"
    if not tool_json.is_file():
        return invalid("missing tool.json")
    try:
        raw = json.loads(tool_json.read_text(encoding="utf-8"))
    except OSError:
        return invalid("tool.json could not be read")
    except ValueError:
        return invalid("tool.json is not valid JSON")
    if not isinstance(raw, dict):
        return invalid("tool.json is not a JSON object")

    # Read `enabled` as soon as the JSON is a dict so even an invalid package
    # (name mismatch, bad schema) still reports the operator's intended toggle.
    enabled_raw = raw.get("enabled", True)
    enabled = enabled_raw if isinstance(enabled_raw, bool) else True

    name_field = raw.get("name")
    if not isinstance(name_field, str) or not _NAME_RE.match(name_field):
        return invalid("name is missing or not a valid tool name", enabled=enabled)
    if name_field != name:
        return invalid("name does not match the package name", enabled=enabled)

    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        return invalid("description is missing or empty", enabled=enabled)

    parameters = raw.get("parameters")
    if not isinstance(parameters, dict):
        return invalid("parameters is not a JSON Schema object", enabled=enabled)

    entry = raw.get("entry")
    if not _valid_entry(entry):
        return invalid("entry is not a non-empty list of command strings", enabled=enabled)
    if not _entry_file_exists(directory, entry):
        return invalid("entry does not reference a file inside the tool package", enabled=enabled)

    return _PackageScan(
        name=name,
        directory=directory,
        valid=True,
        enabled=enabled,
        description=description.strip()[:_DESCRIPTION_CAP],
        error=None,
        parameters=parameters,
        entry=list(entry),
    )


def _scan_all() -> list[_PackageScan]:
    """Scan every immediate subdirectory of ``tools_dir`` in name order.

    A missing/unset dir yields [] (feature off, or nothing installed yet).
    Sorted so ``list_tools`` and ``enabled_llm_tools`` are deterministic. Stray
    non-directory entries are skipped, and so are HIDDEN directories (a leading
    "."): a dot can never begin a valid package name (see ``_NAME_RE``), and the
    installer parks its in-progress builds under ``<tools_dir>/.staging/`` --
    without this skip, every install in flight would surface in the tools list
    as a phantom broken package named ".staging".
    """
    base = tools_dir()
    if base is None or not base.is_dir():
        return []
    return [
        _scan_package(child)
        for child in sorted(base.iterdir())
        if child.is_dir() and not child.name.startswith(".")
    ]


def validate_package(directory: Path, expected_name: str) -> str | None:
    """Validate a candidate package OUTSIDE the tools dir; None means valid.

    The installer's pre-move gate: ``directory`` is its staging build (a uuid
    directory name, hence the explicit ``expected_name`` -- the name the package
    is about to be installed under, which the manifest must already carry).
    Runs the exact same checks an installed package faces on every scan
    (manifest shape, name regex + match, entry-file containment), so a package
    that passes here can never turn up ``valid=False`` after the move.
    """
    return _scan_package(directory, expected_name=expected_name).error


def list_tools() -> list[dict[str, Any]]:
    """List every installed package as a UI-facing summary.

    Each entry is ``{name, description, enabled, valid, error}``. A broken
    package (bad JSON, name mismatch, bad schema shape, missing entry file) is
    included with ``valid=False`` and a safe ``error`` reason, and is never
    executable; a missing/unset tools dir yields [].
    """
    return [
        {
            "name": scan.name,
            "description": scan.description,
            "enabled": scan.enabled,
            "valid": scan.valid,
            "error": scan.error,
        }
        for scan in _scan_all()
    ]


def _build_llm_tool(scan: _PackageScan) -> LlmTool:
    """Turn a valid ``_PackageScan`` into an executable ``LlmTool``.

    Callers pass only ``valid`` scans, so ``parameters`` / ``entry`` are present;
    the assertions document that precondition and keep the type checker happy
    without an ``Any`` escape hatch.
    """
    assert scan.parameters is not None
    assert scan.entry is not None
    spec: dict[str, Any] = {
        "type": "function",
        "function": {
            # scan.description is already stripped and capped at scan time.
            "name": scan.name,
            "description": scan.description,
            "parameters": scan.parameters,
        },
    }
    return LlmTool(spec=spec, handler=_make_handler(scan.directory, scan.entry))


def enabled_llm_tools() -> list[LlmTool]:
    """Every valid AND enabled package as an ``LlmTool``, for ``generate_structured``.

    This is what the AI workflows advertise to the model. An empty result (no
    tools dir, nothing installed, or nothing valid+enabled) means the workflows
    pass no tools and their prompts stay byte-identical to the tool-less build.
    """
    return [_build_llm_tool(scan) for scan in _scan_all() if scan.valid and scan.enabled]


# --- execution -------------------------------------------------------------


def _load_tool_dotenv(directory: Path) -> dict[str, str]:
    """Parse the package's optional ``.env`` into a plain env dict.

    Uses ``dotenv_values`` (already a project dependency, used by cli.py) to
    parse WITHOUT touching the parent ``os.environ`` -- these values go only into
    the child's environment, never ours. Bare keys (value None) and any parse
    failure are dropped defensively so a malformed ``.env`` degrades to "no extra
    env" rather than breaking the tool call.
    """
    env_file = directory / ".env"
    if not env_file.is_file():
        return {}
    try:
        values = dotenv_values(env_file, encoding="utf-8")
    except Exception:
        return {}
    return {key: value for key, value in values.items() if isinstance(value, str)}


def _build_tool_env(directory: Path) -> dict[str, str]:
    """Build the child environment FROM SCRATCH: passthrough allowlist + tool .env.

    The parent environment is NEVER copied wholesale -- see the module docstring.
    Only ``_PASSTHROUGH_ENV`` is carried over from the parent; the tool's own
    ``.env`` is layered on top (so a tool may set, and even override PATH for,
    its own needs), but our backend secrets (OPENAI_API_KEY, ...) are structurally
    absent because they were never copied in.
    """
    env = {name: os.environ[name] for name in _PASSTHROUGH_ENV if name in os.environ}
    env.update(_load_tool_dotenv(directory))
    return env


def _cap_output(text: str, cap: int) -> str:
    """Trim ``text`` to at most ``cap`` chars, marking it when cut.

    Mirrors ``memory_ai._truncate_to`` / ``llm_log._stored_body``, including the
    edge case where ``cap`` is too small to even hold the marker (the marker is
    dropped and the text hard-cut) -- unreachable given the setting's ge=1000
    floor, but kept correct independent of it.
    """
    if len(text) <= cap:
        return text
    marker_len = len(_OUTPUT_TRUNCATION_MARKER)
    if cap <= marker_len:
        return text[:cap]
    return text[: cap - marker_len] + _OUTPUT_TRUNCATION_MARKER


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the child's whole process group (it is a group leader).

    ``start_new_session=True`` made the child a session/group leader, so
    ``os.getpgid(pid) == pid`` and one ``killpg`` takes down the child AND any
    grandchildren it spawned -- a tool that forked a runaway helper cannot
    survive the timeout. Falls back to killing just the child if the group
    signal cannot be delivered (already reaped, or a platform edge), and never
    raises.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError, PermissionError, OSError:
        with contextlib.suppress(Exception):
            proc.kill()


def _run_tool_subprocess(
    entry: list[str],
    directory: Path,
    env: dict[str, str],
    args_json: str,
    timeout: float,
    output_cap: int,
) -> str:
    """Run one tool entry to completion (or timeout) and return its result STRING.

    Blocking; the async handler runs it via ``run_in_threadpool``. Every outcome
    is an agent-visible string, never an exception:

    * cannot even start (bad interpreter/entry, no exec bit) -> a "failed to
      start" category;
    * exceeded ``timeout`` -> the process GROUP is SIGKILLed and a "timed out"
      message returned;
    * non-zero exit -> ``"tool failed (exit N): <stderr>"`` (stderr size-capped);
    * success -> stdout, size-capped at ``output_cap``.

    ``shell=False`` (argv list, never a shell string) so nothing in the model's
    arguments or a tool name can be shell-injected. ``errors="replace"`` on the
    text pipes keeps a tool that emits invalid UTF-8 from crashing ``communicate``.
    """
    try:
        proc = subprocess.Popen(
            entry,
            cwd=directory,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            # New session/group so the timeout path can kill the whole tree.
            start_new_session=True,
        )
    except OSError as exc:
        # FileNotFoundError (bad interpreter/script), PermissionError (no exec
        # bit), ... -- the tool never ran. Category only, no raw error string.
        return f"tool failed to start: {type(exc).__name__}"

    try:
        stdout, stderr = proc.communicate(input=args_json, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        # Reap so no zombie lingers. The group is already SIGKILLed, so this
        # returns effectively at once; the short timeout only guards a wedged pipe.
        with contextlib.suppress(Exception):
            proc.communicate(timeout=_REAP_TIMEOUT_SECONDS)
        return f"tool timed out after {timeout:g} seconds"

    if proc.returncode != 0:
        return f"tool failed (exit {proc.returncode}): {_cap_output(stderr.strip(), output_cap)}"
    return _cap_output(stdout, output_cap)


def _make_handler(directory: Path, entry: list[str]) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Build the async handler an ``LlmTool`` runs, closing over its package.

    Settings (timeout, output cap) and the child environment are read at CALL
    time, not build time, so a settings override or an edited ``.env`` is
    honored on the next invocation. The blocking subprocess is off-loaded to the
    threadpool (the house pattern), so a slow tool never sits on the event loop.
    The handler honors ``LlmTool``'s no-raise contract: ``_run_tool_subprocess``
    turns every failure into a string, and the llm loop's own ``except`` is the
    final backstop.
    """

    async def _handler(arguments: dict[str, Any]) -> str:
        settings = get_settings()
        env = _build_tool_env(directory)
        args_json = json.dumps(arguments, ensure_ascii=False)
        return await run_in_threadpool(
            _run_tool_subprocess,
            list(entry),
            directory,
            env,
            args_json,
            settings.llm_tool_timeout_seconds,
            settings.llm_tool_output_max_chars,
        )

    return _handler


# --- mutation (name-validated + containment-checked) -----------------------


def _resolve_package_dir(name: str) -> Path | None:
    """Resolve ``<tools_dir>/<name>`` for a mutation, or None if it is unsafe.

    The path-traversal hard-block, in two independent layers:

    1. ``name`` must match ``_NAME_RE`` -- which admits no ".", "/", or
       whitespace, so ``".."`` / ``"../evil"`` / ``"/etc/passwd"`` never even
       reach the filesystem;
    2. the RESOLVED candidate must still be contained under the RESOLVED
       ``tools_dir``. Redundant with (1) for a plain name, but it is what
       actually stops a package that is a SYMLINK pointing outside the tools dir
       (or any future loosening of the regex) from letting ``delete_tool``
       ``rmtree`` an arbitrary path. Attack blocked: a request to
       ``set_enabled("../../etc")`` or a symlinked ``evil`` package escaping the
       directory returns None here and the caller no-ops.

    None also covers the feature being off (``tools_dir`` unset).
    """
    base = tools_dir()
    if base is None or not _NAME_RE.match(name):
        return None
    candidate = (base / name).resolve()
    if not _is_within(base.resolve(), candidate):
        return None
    return candidate


def set_enabled(name: str, enabled: bool) -> bool:
    """Flip a package's ``enabled`` flag in its ``tool.json``. Returns success.

    False when the name is unsafe (see ``_resolve_package_dir``), the package
    or its ``tool.json`` is missing/unreadable, or the write fails -- so the
    caller (a future PATCH route) maps a bad name and a missing package alike to
    a clean "did not happen" rather than a 500.
    """
    directory = _resolve_package_dir(name)
    if directory is None or not directory.is_dir():
        return False
    tool_json = directory / "tool.json"
    try:
        raw = json.loads(tool_json.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return False
    if not isinstance(raw, dict):
        return False
    raw["enabled"] = enabled
    try:
        tool_json.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def delete_tool(name: str) -> bool:
    """Delete a package directory (``rmtree``). Returns success.

    Name-validated and containment-checked exactly like ``set_enabled`` (the
    traversal hard-block is what makes an ``rmtree`` here safe), so it can only
    ever remove a directory that genuinely sits inside ``tools_dir``. False when
    the name is unsafe, the package is absent, or the removal fails.
    """
    directory = _resolve_package_dir(name)
    if directory is None or not directory.is_dir():
        return False
    try:
        shutil.rmtree(directory)
    except OSError:
        return False
    return True
