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
  ``os.environ``. This is the single most important guarantee in this module.
  Be honest about its reach, though: scrubbing prevents ACCIDENTAL leakage (the
  key is simply not in the child's ``os.environ``), but a same-UID subprocess
  can in principle read ``/proc/<ppid>/environ`` of the parent, so this is not
  adversarial isolation -- the real trust boundary is the operator only
  installing tool instructions they trust (D21), not the scrub;
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
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

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

# Hard ceiling on the tool.json FILE size, checked with stat() BEFORE the file is
# read so an enormous manifest is never loaded into memory at all. tool.json is
# re-read on EVERY registry scan (list_tools / enabled_llm_tools / every
# mutation), so an unbounded one is a per-scan I/O + memory hazard; a manifest
# over this is listed invalid rather than parsed. 64 KiB is orders of magnitude
# above any sane manifest (name + description + a JSON-Schema parameters block).
_MANIFEST_MAX_BYTES = 64 * 1024

# Tighter ceiling on the parameters JSON Schema specifically: unlike the rest of
# the manifest, this block is re-serialized into the tools array of EVERY LLM
# request of EVERY workflow that has this tool enabled (see _build_llm_tool), so
# an oversized schema is a recurring token/prompt-bloat + DoS hazard, not just a
# one-off read. Measured as ``len(json.dumps(parameters))``; a schema over this
# is listed invalid. 16 KiB comfortably fits a rich real-world argument schema.
_PARAMETERS_SCHEMA_MAX_BYTES = 16 * 1024

# Hard ceiling on a tool's optional ``.env`` FILE size, checked with stat() BEFORE
# the file is parsed. Unlike the manifest, ``.env`` is re-read on EVERY tool call
# (``_load_tool_dotenv`` runs inside the per-call ``_build_tool_env``), so an
# unbounded one is a per-CALL memory hazard, not a per-scan one; and the parsed
# dict becomes the child's environment BLOCK, where an enormous env can also hit
# the kernel's E2BIG limit at exec. Over this cap the runtime degrades to "no
# extra env" (``_load_tool_dotenv`` returns {}, the same contract a malformed
# ``.env`` already gets) and the installer refuses the package outright
# (``validate_package``). 64 KiB dwarfs any real secrets file (a handful of
# KEY=VALUE lines).
_ENV_FILE_MAX_BYTES = 64 * 1024

# Appended when a tool's stdout (or a failure's stderr) is cut for size. Mirrors
# the truncation markers in memory_ai / llm_log so an operator who has seen those
# recognizes this one; the distinct wording ("工具輸出" = tool output) tells it
# apart from a truncated item section or a truncated log body.
_OUTPUT_TRUNCATION_MARKER = "…[工具輸出過長已截斷]"

# How long the bounded reader is given to drain pipes and reap AFTER the process
# group has already been SIGKILLed on timeout. The group is dead, so this
# returns effectively immediately; it exists only so a wedged pipe can never
# turn cleanup itself into a hang.
_REAP_TIMEOUT_SECONDS = 5.0

# Chunk size for the bounded incremental pipe reads (see _communicate_bounded).
# 64 KiB is large enough that draining a normal tool's output is one or two
# reads, and small enough that a runaway stream is caught within roughly one
# chunk past the cap -- so a stream far over the cap is stopped after buffering
# at most ``cap + one chunk`` transiently, never the whole unbounded output.
_READ_CHUNK_CHARS = 64 * 1024


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

    # A package directory that is itself a SYMLINK is refused (listed invalid,
    # never executed): _scan_all's ``is_dir()`` filter FOLLOWS the link, so
    # without this a symlink to any real directory would be scanned -- and
    # potentially run -- as a package. Real directories only (H3 / D21). Staging
    # validation is unaffected: its directory is a real ``mkdir``ed uuid dir.
    if directory.is_symlink():
        return invalid("package directory must be a real directory (not a symlink)")

    tool_json = directory / "tool.json"
    # tool.json must be a REAL file too: a symlinked manifest is refused (listed
    # invalid) so a scan can never READ -- and set_enabled can never REWRITE --
    # a file outside the package through it (H3). Checked BEFORE ``is_file()``,
    # which follows the link and would otherwise accept it.
    if tool_json.is_symlink():
        return invalid("tool.json must be a real file (not a symlink)")
    if not tool_json.is_file():
        return invalid("missing tool.json")
    # Bound the manifest FILE size with stat() BEFORE reading it (see
    # _MANIFEST_MAX_BYTES): an oversized tool.json is a per-scan memory/I/O
    # hazard, so it is listed invalid rather than loaded into memory.
    try:
        manifest_size = tool_json.stat().st_size
    except OSError:
        return invalid("tool.json could not be read")
    if manifest_size > _MANIFEST_MAX_BYTES:
        return invalid("tool.json is too large")
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
    # Bound the parameters schema (see _PARAMETERS_SCHEMA_MAX_BYTES): it is
    # re-serialized into the tools array of EVERY LLM request that has this tool
    # enabled, so an oversized one is a recurring token/prompt-bloat hazard, not
    # just a big file. ``parameters`` came from json.loads, so json.dumps of it
    # cannot raise.
    if len(json.dumps(parameters)) > _PARAMETERS_SCHEMA_MAX_BYTES:
        return invalid("parameters schema is too large", enabled=enabled)

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
    that passes here can never turn up ``valid=False`` after the move -- PLUS one
    STRICTER install-only gate below.

    The .env size gate is deliberately NOT part of ``_scan_package`` (which the
    registry runs on every scan): an oversized ``.env`` must BLOCK a fresh install
    here, but a package whose ``.env`` is MUTATED oversized AFTER install should
    keep running under the runtime degrade (``_load_tool_dotenv`` -> {}), not
    vanish from the registry as invalid. So this gate lives on the installer's
    pre-move path only. Being stricter than the scan preserves the invariant above
    (passing here still implies passing the scan); only the reverse loosens.
    """
    error = _scan_package(directory, expected_name=expected_name).error
    if error is not None:
        return error
    # Install-only .env size gate (see _ENV_FILE_MAX_BYTES). Mirrors
    # _load_tool_dotenv's own is_file()-then-stat() shape; a stat failure is
    # treated as "not oversized" (the scan above already vetted the package, and
    # the runtime degrade remains the post-install defense).
    env_file = directory / ".env"
    try:
        oversized = env_file.is_file() and env_file.stat().st_size > _ENV_FILE_MAX_BYTES
    except OSError:
        oversized = False
    if oversized:
        return "`.env` is too large"
    return None


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

    ``interpolate=False`` is LOAD-BEARING, not a style choice: python-dotenv's
    default POSIX-style interpolation resolves a ``${VAR}`` reference against the
    PARENT process environment (see resolve_variables, which folds os.environ
    into the lookup context). A tool ``.env`` line like ``LEAK=${OPENAI_API_KEY}``
    would then resolve to our real backend key -- reinjecting into the child the
    very credential the from-scratch env exists to EXCLUDE. With interpolation
    off the value is kept as the literal string ``${OPENAI_API_KEY}``, so a
    generated tool can never exfiltrate a parent secret through its own manifest.
    """
    env_file = directory / ".env"
    if not env_file.is_file():
        return {}
    # Bound the .env FILE size with stat() BEFORE dotenv_values reads it (see
    # _ENV_FILE_MAX_BYTES): the parse slurps the whole file into memory on EVERY
    # tool call, and the parsed dict becomes the child's exec env block. An
    # oversized one degrades to "no extra env" -- the SAME degrade-to-{} contract
    # as the malformed-.env fallback below -- so a runaway or post-install-mutated
    # .env can never bloat memory per call or overflow the exec env.
    try:
        if env_file.stat().st_size > _ENV_FILE_MAX_BYTES:
            return {}
    except OSError:
        return {}
    try:
        values = dotenv_values(env_file, encoding="utf-8", interpolate=False)
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


@dataclass(slots=True)
class _BoundedOutput:
    """One bounded subprocess read's result (see ``_communicate_bounded``).

    ``stdout``/``stderr`` hold at most ``cap + 1`` chars each; the ``*_overflow``
    flags say a stream ran PAST the cap (and so the process was killed for it),
    which the callers use to decide between "here is the truncated output" and
    "the tool failed". ``timed_out`` is the wall-clock expiry, kept distinct so
    a caller can render the timeout message instead of an output.
    """

    stdout: str
    stderr: str
    stdout_overflow: bool
    stderr_overflow: bool
    timed_out: bool


class _CappedReader:
    """Drains ONE text pipe on its own thread, bounded to ``cap + 1`` chars.

    The building block that replaces ``subprocess.communicate``'s unbounded
    slurp: each output pipe gets one of these on its own thread (reading two
    pipes sequentially would DEADLOCK -- a child that fills the OTHER pipe's
    buffer blocks on write while we block reading the first). It reads in
    ``_READ_CHUNK_CHARS`` chunks and, the moment the running total passes
    ``cap``, marks ``overflow`` and calls ``kill``: the result is already
    truncated, so there is nothing to gain by reading on -- kill the process
    group at once (mirroring the timeout path's SIGKILL) and stop. Never raises
    into its thread: a read on a killed/closed pipe just ends the drain.
    """

    __slots__ = ("_cap", "_kill", "_stream", "overflow", "text")

    def __init__(self, stream: IO[str], cap: int, kill: Callable[[], None]) -> None:
        self._stream = stream
        self._cap = cap
        self._kill = kill
        self.text = ""
        self.overflow = False

    def run(self) -> None:
        collected: list[str] = []
        total = 0
        try:
            while True:
                chunk = self._stream.read(_READ_CHUNK_CHARS)
                if not chunk:
                    break
                collected.append(chunk)
                total += len(chunk)
                if total > self._cap:
                    self.overflow = True
                    self._kill()
                    break
        except Exception:
            # A read on a pipe we just SIGKILLed (or one closed under us) must
            # never crash the reader thread -- the partial text already collected
            # is the truncated result, exactly as intended.
            pass
        finally:
            with contextlib.suppress(Exception):
                self._stream.close()
        joined = "".join(collected)
        # Retain at most cap+1 even if the final chunk overshot the cap by a whole
        # chunk: the caller's _cap_output then trims cap+1 down to the marked cap.
        self.text = joined[: self._cap + 1] if self.overflow else joined


def _communicate_bounded(
    proc: subprocess.Popen[str], *, input_text: str | None, cap: int, timeout: float
) -> _BoundedOutput:
    """``communicate`` with a hard per-stream memory cap and a wall-clock timeout.

    ``subprocess.communicate`` reads stdout/stderr UNBOUNDED into memory before
    any cap is applied -- a runaway tool could OOM the service before
    ``_cap_output`` ever runs. This drains each present pipe with its own
    ``_CappedReader`` thread (concurrent, so a full pipe can never deadlock the
    other), writes ``input_text`` to stdin on ITS OWN thread (so a large stdin
    can never deadlock against a child that writes before it reads), and bounds
    the whole read+wait by ``timeout`` -- killing the process group on expiry,
    the same contract the old ``communicate(timeout=...)`` carried. Every stream
    is capped at ``cap + 1`` chars; a stream over the cap kills the group at once
    (see ``_CappedReader``). Threads are the mechanism, not asyncio: this runs in
    a ``run_in_threadpool`` worker, and ``file.read`` releases the GIL while it
    blocks, so the reads and the ``wait`` genuinely proceed in parallel.
    """
    threads: list[threading.Thread] = []

    def kill() -> None:
        _kill_process_group(proc)

    if input_text is not None and proc.stdin is not None:
        stdin = proc.stdin

        def _write_stdin() -> None:
            # Best-effort: a child that exits or closes stdin early makes this
            # raise BrokenPipeError -- not our failure. A SIGKILL later unblocks a
            # write stalled on a full pipe, so this thread always ends.
            with contextlib.suppress(Exception):
                stdin.write(input_text)
            with contextlib.suppress(Exception):
                stdin.close()

        # daemon=True on every reader/writer thread is a BACKSTOP, not the
        # mechanism: the group-kill escalation after the wait below is what
        # normally reaps a thread whose pipe is held open by a surviving
        # descendant. But should even that fail (a descendant that escaped the
        # process group entirely -- see the escalation comment), a NON-daemon
        # thread stuck in a blocking read/write would keep the interpreter alive at
        # shutdown; daemon makes that impossible -- at worst one FD leaks until
        # process exit, never a hung interpreter.
        writer = threading.Thread(target=_write_stdin, daemon=True)
        writer.start()
        threads.append(writer)

    stdout_reader = _CappedReader(proc.stdout, cap, kill) if proc.stdout is not None else None
    stderr_reader = _CappedReader(proc.stderr, cap, kill) if proc.stderr is not None else None
    for reader in (stdout_reader, stderr_reader):
        if reader is not None:
            # daemon=True for the same backstop reason as the writer above.
            thread = threading.Thread(target=reader.run, daemon=True)
            thread.start()
            threads.append(thread)

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(proc)
        # The group is SIGKILLed; reap so no zombie lingers. Bounded by the short
        # reap timeout, exactly like the old timeout path.
        with contextlib.suppress(Exception):
            proc.wait(timeout=_REAP_TIMEOUT_SECONDS)

    # ``proc.wait()`` returning means the LEADER process exited -- NOT that the
    # pipes are at EOF. A reader sees EOF only when the LAST holder of a pipe's
    # write end closes it, and a tool whose entry spawned a background descendant
    # that INHERITED stdout/stderr leaves those write ends open after the leader is
    # gone: the readers would then block forever, this join would return with the
    # (daemon) threads still alive, and we would leak the threads, their FDs, and
    # the surviving descendant. So join with the short reap bound FIRST -- the
    # common case (leader exit DID close the pipes: no such descendant, or the
    # overflow/timeout paths already group-killed everything) finishes well inside
    # it -- and escalate only if that is not enough.
    for thread in threads:
        thread.join(timeout=_REAP_TIMEOUT_SECONDS)
    # A thread still alive here means its pipe is held open by a descendant that
    # outlived the leader. ``start_new_session=True`` put every ORDINARY descendant
    # in the leader's process GROUP, so SIGKILLing the group closes their inherited
    # write ends -> the readers hit EOF and finish. We target the group by
    # ``proc.pid`` directly (a session leader's group id EQUALS its pid, and that id
    # stays reserved by the kernel while any group member survives): ``_kill_process
    # _group`` would be WRONG here because it derives the group via ``os.getpgid(
    # proc.pid)``, which now raises -- ``proc.wait()`` above already reaped the
    # leader -- and falls back to a no-op ``proc.kill`` that never reaches the
    # descendant. A descendant that double-forked / setsid'd OUT of the group
    # escapes even this and is beyond v1's non-container stance (D21); the daemon
    # flag on every thread is the honest backstop for that residual case.
    if any(thread.is_alive() for thread in threads):
        with contextlib.suppress(Exception):
            os.killpg(proc.pid, signal.SIGKILL)
        for thread in threads:
            thread.join(timeout=_REAP_TIMEOUT_SECONDS)

    return _BoundedOutput(
        stdout=stdout_reader.text if stdout_reader is not None else "",
        stderr=stderr_reader.text if stderr_reader is not None else "",
        stdout_overflow=stdout_reader.overflow if stdout_reader is not None else False,
        stderr_overflow=stderr_reader.overflow if stderr_reader is not None else False,
        timed_out=timed_out,
    )


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
    text pipes keeps a tool that emits invalid UTF-8 from crashing the reader.
    Output is drained by ``_communicate_bounded`` (NOT ``communicate``), which
    caps each stream in memory as it reads rather than slurping it whole first --
    a runaway tool is killed at the cap instead of OOMing the service.
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

    output = _communicate_bounded(proc, input_text=args_json, cap=output_cap, timeout=timeout)
    if output.timed_out:
        return f"tool timed out after {timeout:g} seconds"
    # STDOUT over the cap is the success result, already truncated -- return it
    # regardless of the (kill-induced, negative) exit code the overflow kill left
    # behind: the tool produced its answer, we just stopped reading it. _cap_output
    # trims the retained cap+1 chars down to the cap behind the marker.
    if output.stdout_overflow:
        return _cap_output(output.stdout, output_cap)
    if proc.returncode != 0:
        stderr = _cap_output(output.stderr.strip(), output_cap)
        return f"tool failed (exit {proc.returncode}): {stderr}"
    return _cap_output(output.stdout, output_cap)


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
    directory is an alias (see below), the package or its ``tool.json`` is
    missing/unreadable/oversized, or the write fails -- so the caller (a future
    PATCH route) maps a bad name and a missing package alike to a clean "did not
    happen" rather than a 500.
    """
    base = tools_dir()
    if base is None or not _NAME_RE.match(name):
        return False
    # INTERNAL-alias hard-block (H3), BEFORE resolve: an internal symlink
    # ``tools/<name> -> tools/real`` RESOLVES inside the tools root, so the
    # resolve-then-contain check below would PASS and this toggle would rewrite
    # the REAL package's manifest THROUGH the alias. ``is_symlink`` does not
    # follow the final component, so it detects the alias itself; refuse outright
    # -- set_enabled has no safe action on an alias (reading or writing a manifest
    # through it is exactly the escape we are stopping), unlike delete_tool which
    # can at least drop just the dangling link. Checked before resolve precisely
    # because ``resolve()`` erases the alias/real distinction. External-symlink
    # escapes are still separately caught by the containment check further down.
    if (base / name).is_symlink():
        return False
    directory = _resolve_package_dir(name)
    if directory is None or not directory.is_dir():
        return False
    tool_json = directory / "tool.json"
    # Re-verify containment at the WRITE boundary: ``directory`` is already the
    # RESOLVED package dir, but ``tool.json`` could be a SYMLINK pointing outside
    # it, and ``write_text`` follows symlinks -- so a symlinked manifest would
    # otherwise let this rewrite an arbitrary file the service can reach. Require
    # the RESOLVED manifest path to stay inside the resolved package dir before
    # touching it (mirrors _resolve_package_dir's own resolve-then-contain, H3).
    # The scan already lists such a package invalid, but set_enabled must not
    # trust that -- it is reached directly by the mutation API.
    if not _is_within(directory, tool_json.resolve()):
        return False
    # Bound the manifest FILE size with stat() BEFORE reading it (N2), the SAME
    # cap the scan enforces (_MANIFEST_MAX_BYTES): the cap must hold at EVERY
    # read entry, not just the scan, so an oversized manifest is never loaded into
    # memory through this path either -- refuse like every other failure here.
    try:
        if tool_json.stat().st_size > _MANIFEST_MAX_BYTES:
            return False
    except OSError:
        return False
    try:
        raw = json.loads(tool_json.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return False
    if not isinstance(raw, dict):
        return False
    raw["enabled"] = enabled
    # Refuse to WRITE if the re-serialized manifest would exceed the cap (N2).
    # ``indent=2`` pretty-prints, which EXPANDS a compact-but-legal manifest: a
    # manifest that sat just under the cap in its compact on-disk form can cross
    # it once pretty-printed, which would flip the tool ``valid=False`` on the very
    # next scan after a mere enable/disable toggle. Measure the ENCODED size (the
    # same UTF-8 byte unit the stat cap uses) and, when it would not fit, leave the
    # file byte-for-byte untouched and report failure rather than corrupt the row.
    new_text = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    if len(new_text.encode("utf-8")) > _MANIFEST_MAX_BYTES:
        return False
    try:
        tool_json.write_text(new_text, encoding="utf-8")
    except OSError:
        return False
    return True


def delete_tool(name: str) -> bool:
    """Delete a package (``rmtree``), or an alias (``unlink``). Returns success.

    Name-validated and containment-checked exactly like ``set_enabled`` (the
    traversal hard-block is what makes an ``rmtree`` here safe), so it can only
    ever remove a directory that genuinely sits inside ``tools_dir``. False when
    the name is unsafe, the package is absent, or the removal fails.
    """
    base = tools_dir()
    if base is None or not _NAME_RE.match(name):
        return False
    # INTERNAL-alias hard-block (H3), BEFORE resolve: an internal symlink
    # ``tools/<name> -> tools/real`` RESOLVES inside the tools root, so the
    # resolve-then-contain check below would PASS and ``rmtree`` would recurse
    # through the alias and DELETE THE REAL PACKAGE -- user-triggerable data loss,
    # since the scan lists the alias as an invalid row and the UI offers 刪除 on
    # invalid rows. ``is_symlink`` does not follow the final component, so it
    # detects the alias itself; ``unlink`` removes ONLY the link (its target,
    # internal OR external, is never touched), so the phantom row disappears and
    # the real package survives. This is a genuine deletion of what the user saw
    # (the alias row), so it returns True. It runs before resolve precisely
    # because ``resolve()`` would erase the alias/real distinction; the
    # resolve-then-contain check below still guards a non-symlink external escape.
    candidate = base / name
    if candidate.is_symlink():
        try:
            candidate.unlink()
        except OSError:
            return False
        return True
    directory = _resolve_package_dir(name)
    if directory is None or not directory.is_dir():
        return False
    try:
        shutil.rmtree(directory)
    except OSError:
        return False
    return True
