"""The KB web installer: one LLM "tool builder" session that writes, tests and
installs a tool package (see D21 in docs/web-v2-decisions.md, Phase 5c).

``run_install(openapi_url, instructions)`` is the whole feature:

1. fetch the OpenAPI document (bounded: 30s, capped redirects, 2MB body);
2. create a throwaway STAGING directory ``<tools_dir>/.staging/<uuid>`` --
   hidden, so the registry scan never lists an in-progress build (see
   ``tools._scan_all``);
3. run ONE ``generate_structured`` call (workflow ``tool_install``) armed with
   four META-TOOLS -- the file tools ``write_file`` / ``read_file`` /
   ``list_dir`` (paths jailed inside staging) plus ``run_shell`` (a full shell
   that merely STARTS in staging) -- under the installer's own (much larger)
   round and wall-clock budgets;
4. on a ``ready`` result, validate the staged package with the SAME checks the
   registry applies to installed packages (``tools.validate_package``) and move
   it into ``<tools_dir>/<name>``; on anything else, fail with a friendly
   error. Staging is always cleaned up.

Language rule for the strings in this module: META-TOOL RESULTS (and the
builder prompts) are MODEL-facing and therefore English, like every prompt in
this codebase; JOB/OUTCOME errors are USER-facing (rendered by the 工具 page)
and therefore zh-TW, like the 409 conflict message. The exception is output
truncation, which reuses ``tools._cap_output`` and thus its shared zh-TW
marker -- one implementation, one marker, everywhere output is capped.

SECURITY STANCE (D21 v1, same as tools.py). The builder LLM gets real file AND
real shell capability -- that IS the feature: the operator asked for a
Claude-Code-like builder. Be precise about what is and is not ENFORCED:

* the FILE meta-tools (``write_file`` / ``read_file`` / ``list_dir``) ARE jailed:
  every path must be relative and must RESOLVE inside staging
  (``_resolve_in_staging``; the attacks and why resolve-then-contain stops them
  are documented there). ``write_file``'s jail is the ONE hard write boundary
  this module enforces;
* ``run_shell`` is NOT jailed: it runs bash with the SERVICE'S OWN permissions
  and merely STARTS in the staging directory (cwd is a working convention, not a
  sandbox -- the command can read/write anywhere the service's uid can). There
  is deliberately NO container isolation in v1: this is a single-user, local
  tool (D21), so the trust boundary is the operator only installing API
  descriptions and instructions they trust -- not a confinement the code
  pretends to enforce. The system prompt still tells the model to treat staging
  as its workspace, but as GUIDANCE, not a wall;
* what run_shell DOES guarantee is a from-scratch environment (the
  ``tools._PASSTHROUGH_ENV`` allowlist only) so the builder can ``curl``/
  ``python3`` the real KB API but can never read OUR ``OPENAI_API_KEY`` out of
  the process environment;
* per-command timeout with a process-group kill, and output caps, so a hung or
  chatty command burns one round, never the session (see
  ``tool_install_shell_timeout_seconds`` in config.py for the nested-timeout
  rationale).

Job management is a deliberately primitive in-memory dict: this is a
single-process, single-user local service, jobs are NOT persisted, and a
backend restart forgets them (the FE then simply gets a 404 for its job id and
the user re-runs the install). Bounded to the most recent ``_MAX_JOBS``.
"""

import asyncio
import contextlib
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, model_validator
from starlette.concurrency import run_in_threadpool

from context_memory.config import get_settings
from context_memory.services import llm_log, tools
from context_memory.services.llm import (
    LLMNotConfiguredError,
    LlmTool,
    LLMUpstreamError,
    generate_structured,
)
from context_memory.services.memory_ai import _coerce_bool, _coerce_str, _truncate_to
from context_memory.services.tools import _NAME_RE

# The llm_log workflow name for every builder session -- what the AI 日誌 page
# shows, and the key `last_record_id_for_workflow` looks the session up by.
_WORKFLOW = "tool_install"

# Where in-progress builds live. Hidden (leading dot) ON PURPOSE: the registry
# scan skips hidden directories (tools._scan_all), so a build in flight can
# never surface in the tools list as a phantom broken package.
_STAGING_DIRNAME = ".staging"

# OpenAPI fetch bounds. 2MB is far above any sane API description document; a
# larger body is a wrong URL (a data dump, an HTML app) and must fail OUTRIGHT
# rather than be truncated into the prompt -- a silently-cut JSON document
# would just make the builder fail later, more confusingly.
_FETCH_TIMEOUT_SECONDS = 30.0
_FETCH_MAX_REDIRECTS = 5
_OPENAPI_MAX_BYTES = 2 * 1024 * 1024

# Total wall-clock deadline for the WHOLE fetch. Distinct from
# _FETCH_TIMEOUT_SECONDS above, which httpx applies as a PER-PHASE (connect/
# read/write) INACTIVITY timeout: a server that dribbles one byte just under the
# read timeout resets that clock forever and could hold the stream open
# indefinitely. This asyncio.timeout bounds the end-to-end duration regardless,
# mirroring the outer-deadline-around-a-per-phase-client-timeout pattern in
# llm.py (see _build_client / _run_structured). Expiry surfaces as TimeoutError,
# which the total except below turns into the friendly fetch-failure outcome.
_FETCH_TOTAL_TIMEOUT_SECONDS = 60

# write_file's per-call content bound. REJECTED (not truncated) when exceeded:
# a truncated source file is silently corrupt -- the builder would then test a
# file that is not what it thinks it wrote -- so the honest failure is an error
# result telling it to write smaller pieces. 512k covers any sane generated
# tool many times over.
_WRITE_CONTENT_MAX_CHARS = 512_000

# list_dir's entry bound: enough to show any sane staged package in full while
# keeping a pathological build (a run_shell that exploded thousands of files)
# from flooding one tool result.
_LIST_DIR_MAX_ENTRIES = 500

# InstallResult field bounds. tool_name's cap matches the name regex's own max
# length (64); summary is a short human report, capped like a progress note.
_TOOL_NAME_MAX = 64
_SUMMARY_CAP = 2000

# User-facing (zh-TW) outcome errors that need to be exact in more than one
# place (tests pin them; the FE renders them verbatim). The fullwidth
# punctuation ruff flags as "ambiguous" is authentic zh-TW typography, per the
# suite-wide precedent (tests/test_ai_supersede.py).
_ERROR_TOOLS_DISABLED = "工具功能未啟用（TOOLS_DIR 未設定）。"  # noqa: RUF001
_ERROR_OPENAPI_TOO_LARGE = "OpenAPI 文件過大（超過 2MB 上限）。"  # noqa: RUF001
_ERROR_NAME_TAKEN = "同名工具已存在，請先刪除舊工具再重新安裝。"  # noqa: RUF001

# The builder's system prompt. English, like every prompt in this codebase.
# It must carry the ENTIRE package contract (tool.json fields, the name regex,
# the stdin/stdout execution contract, .env for secrets) because the model has
# no other way to learn it -- the runtime that will execute the finished tool
# is not in the conversation. The suggested loop (write -> test via run_shell
# -> fix -> ready) is what makes max_tool_rounds=24 a working budget.
_BUILDER_SYSTEM_PROMPT = """\
You are a tool builder. Your working directory (the "workspace") is the future \
tool package: build a complete, WORKING tool package in it that gives an AI \
assistant access to the API described by the OpenAPI document, following the \
user's instructions.

The tool package contract (the runtime that will execute your finished tool):
- `tool.json` at the workspace root, a JSON object with exactly these fields:
  - "name": the tool's name; MUST match ^[a-z0-9][a-z0-9_-]{0,63}$ and will \
become the package directory name.
  - "description": one paragraph telling the AI assistant when to call this \
tool and what it returns (non-empty, max 1000 characters).
  - "parameters": a JSON Schema object describing the tool's arguments \
(type "object" with "properties").
  - "entry": the argv list that runs the tool, e.g. ["python3", "run.py"]; at \
least one element must be a file inside the package.
- At runtime the tool is invoked with the package directory as its working \
directory; the arguments arrive as ONE JSON object on STDIN; whatever it \
prints to STDOUT is the result shown to the AI assistant; a non-zero exit \
code means failure (STDERR is shown as the error).
- Secrets (API keys, tokens) go into a `.env` file (KEY=VALUE lines) in the \
workspace; at runtime those values are injected into the tool's environment. \
They are NOT auto-loaded inside your run_shell tests -- source them yourself \
when testing: `set -a; . ./.env 2>/dev/null; set +a; ...`.
- Prefer Python 3 with ONLY its standard library (urllib.request for HTTP), \
so the tool runs anywhere without installing dependencies.

Your meta-tools (the file tools below take paths RELATIVE to the workspace and \
cannot reach outside it):
- write_file {path, content}: create/overwrite a file (parent directories are \
created automatically).
- read_file {path}: read a file back.
- list_dir {path?}: recursively list the workspace (directories end with "/").
- run_shell {command}: run a bash command. It runs with the service's own \
permissions, starting in the staging directory -- treat the staging directory \
as your workspace and keep all your work inside it. It has a timeout of a \
couple of minutes and capped output; it CAN reach the network, so use curl or \
python3 to probe the real API and to test your tool end to end, e.g.: \
echo '{"query":"test"}' | python3 run.py

Recommended flow:
1. Read the user's instructions and the OpenAPI document; pick the endpoint(s) \
that serve the user's goal.
2. Write run.py and tool.json (and .env if the user supplied credentials).
3. Test with run_shell: pipe a realistic JSON argument object into your entry \
command; verify the output is genuinely useful to an AI assistant (compact, \
relevant, plain text or small JSON).
4. Fix and re-test until it works. Do not stop at "should work" -- prove it.
5. Only then finish with ready=true.

Finish by returning the final JSON object described in the system \
instructions: "tool_name" MUST equal the "name" in tool.json; "summary" is a \
short report of what you built and how you verified it (in the user's \
language); set "ready" to true ONLY if your own run_shell test succeeded. If \
you cannot make it work, set ready=false and explain the blocker in summary. \
Never print or embed secrets in the summary."""


class InstallResult(BaseModel):
    """The builder session's structured close: what got built, and is it usable.

    Sanitized like every other LLM-facing model (untrusted output): strings are
    defensively coerced/stripped/capped, ``ready`` uses the strict bool coercion
    (an ambiguous value never reads as true -- ``ready=true`` is what authorizes
    installing executable code, the same "must never promote by accident" bar
    as EnrichResult.checklist_complete). A ``ready`` result whose ``tool_name``
    fails the package-name regex is REJECTED (ValidationError -> one corrective
    retry -> 502), because that name is about to become a filesystem directory
    name and the mutation API's addressing key.
    """

    model_config = ConfigDict(extra="ignore")

    tool_name: str = ""
    summary: str = ""
    ready: bool = False

    @model_validator(mode="before")
    @classmethod
    def _sanitize(cls, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return {
            "tool_name": _coerce_str(data.get("tool_name")).strip()[:_TOOL_NAME_MAX],
            "summary": _coerce_str(data.get("summary")).strip()[:_SUMMARY_CAP],
            "ready": _coerce_bool(data.get("ready")),
        }

    @model_validator(mode="after")
    def _ready_requires_valid_name(self) -> InstallResult:
        if self.ready and not _NAME_RE.match(self.tool_name):
            raise ValueError(
                "a ready result must carry a tool_name matching ^[a-z0-9][a-z0-9_-]{0,63}$"
            )
        return self


@dataclass(slots=True)
class InstallOutcome:
    """What one ``run_install`` produced, success or failure.

    ``llm_log_id`` links to the builder session's AI 日誌 record whenever a
    session actually ran (None only when the run failed before the LLM call --
    feature off, fetch failure); it is set on FAILED outcomes too, since a
    failed build is exactly when the operator wants to read the trace.
    """

    ok: bool
    tool_name: str | None = None
    summary: str | None = None
    error: str | None = None
    llm_log_id: int | None = None


# --- staging containment + meta-tools ---------------------------------------


def _resolve_in_staging(staging: Path, raw: object) -> Path | None:
    """Resolve a model-supplied path strictly inside ``staging``, or None.

    The write-boundary check every meta-tool path goes through. Three attacks,
    three layers:

    * an ABSOLUTE path (``/etc/passwd``): rejected before any join --
      ``Path.joinpath`` with an absolute right side would REPLACE the base
      entirely, so the join itself is the vulnerability being blocked;
    * a TRAVERSAL path (``../../.env``, ``a/../../x``): ``resolve()`` collapses
      the dots and the resolved result then fails the containment check;
    * a SYMLINK escape (the builder first ``run_shell``s
      ``ln -s /home/user secret`` then reads/writes through ``secret/...``):
      ``resolve()`` FOLLOWS symlinks, so the resolved path lands at the real
      target outside staging and containment fails -- checking the unresolved
      string could never catch this one.

    Mirrors tools.py's ``_resolve_package_dir`` defense (same reasoning, same
    ``_is_within`` predicate), applied to the installer's boundary.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw.strip())
    if path.is_absolute():
        return None
    resolved = (staging / path).resolve()
    if not tools._is_within(staging.resolve(), resolved):
        return None
    return resolved


# The rejection text shared by every path-shaped argument that fails
# ``_resolve_in_staging`` -- model-facing English, naming the rule so the model
# can self-correct instead of retrying the same escape.
_PATH_REJECTED = "rejected: path must be a relative path that stays inside the workspace"


def _tool_spec(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    """One OpenAI function-tool spec for a meta-tool (all args required)."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties.keys()),
            },
        },
    }


def _run_shell_subprocess(staging: Path, command: str, timeout: float, cap: int) -> str:
    """Run one builder shell command to completion (or timeout); returns text.

    Blocking (called via ``run_in_threadpool``). Mirrors
    ``tools._run_tool_subprocess``'s discipline exactly -- from-scratch env,
    ``start_new_session`` + process-group SIGKILL on timeout, every outcome an
    agent-visible string -- with the differences a BUILDER shell needs:

    * stdout and stderr are MERGED (``stderr=STDOUT``): a failing test's
      traceback interleaved with its prints, in order, is precisely what the
      model needs to debug;
    * stdin is ``DEVNULL``: a command that blocks reading stdin (an interactive
      prompt) EOFs immediately instead of hanging its whole round;
    * NO tool ``.env`` injection -- the system prompt tells the model to source
      its own ``.env`` when testing, keeping this env identical for every
      command rather than varying with what the model wrote so far.

    Output is drained by ``tools._communicate_bounded`` (NOT ``communicate``),
    which caps the single merged pipe as it reads instead of slurping the whole
    stream first: a command that spews far past the cap is killed at the cap, so a
    runaway ``yes``/``cat`` can never OOM the service before the cap is applied.
    """
    env = {name: os.environ[name] for name in tools._PASSTHROUGH_ENV if name in os.environ}
    try:
        proc = subprocess.Popen(
            ["bash", "-c", command],
            cwd=staging,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        return f"command failed to start: {type(exc).__name__}"
    # One output pipe (stderr is merged into stdout); no stdin (DEVNULL).
    output = tools._communicate_bounded(proc, input_text=None, cap=cap, timeout=timeout)
    if output.timed_out:
        return f"command timed out after {timeout:g} seconds"
    # Streamed past the cap: return the truncated (already marked) output whatever
    # the kill-induced exit code, mirroring the runtime tool's over-cap handling.
    if output.stdout_overflow:
        return tools._cap_output(output.stdout, cap)
    if proc.returncode != 0:
        body = tools._cap_output(output.stdout.strip(), cap)
        return f"command failed (exit {proc.returncode}): {body}"
    return tools._cap_output(output.stdout, cap)


def _build_meta_tools(staging: Path) -> list[LlmTool]:
    """The four meta-tools, every handler a closure over this session's staging.

    All handlers follow the LlmTool no-raise contract: filesystem/subprocess
    failures come back as descriptive error TEXT the model can react to (the
    loop's own except is only the last backstop). Settings-bound limits are
    read at call time, mirroring the runtime tools' handlers.
    """

    async def write_file(args: dict[str, Any]) -> str:
        target = _resolve_in_staging(staging, args.get("path"))
        if target is None:
            return f"write_file {_PATH_REJECTED}"
        content = args.get("content")
        if not isinstance(content, str):
            return "write_file rejected: content must be a string"
        if len(content) > _WRITE_CONTENT_MAX_CHARS:
            return (
                f"write_file rejected: content exceeds {_WRITE_CONTENT_MAX_CHARS} characters; "
                "write the file in smaller pieces"
            )

        def _write() -> str:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"wrote {target.relative_to(staging.resolve())} ({len(content)} characters)"

        try:
            return await run_in_threadpool(_write)
        except Exception as exc:
            return f"write_file failed: {type(exc).__name__}"

    async def read_file(args: dict[str, Any]) -> str:
        target = _resolve_in_staging(staging, args.get("path"))
        if target is None:
            return f"read_file {_PATH_REJECTED}"

        def _read() -> str:
            cap = get_settings().llm_tool_output_max_chars
            # Two-layer size defense, cheap gate first, HARD gate second (N5):
            #  (1) stat().st_size is the CHEAP first gate -- a file already known
            #      huge is refused without opening it (comparing bytes against a
            #      CHAR cap is conservative: UTF-8 chars <= bytes, so within the
            #      byte budget guarantees within the char cap). A directory is
            #      rejected first, since its block-sized st_size could otherwise
            #      trip that gate with the wrong message; a missing path keeps the
            #      "no such file" wording rather than being mislabeled below.
            #  (2) require a REGULAR file, then read at most cap+1 chars -- the HARD
            #      gate. stat() alone is NOT enough: a FIFO (the builder can
            #      `mkfifo` one via run_shell) is neither a dir nor over-size
            #      (st_size 0), and read_text() on it would BLOCK FOREVER -- the
            #      outer asyncio timeout only cancels the await, leaving THIS
            #      threadpool worker wedged for good (a permanent worker leak).
            #      is_file() (S_ISREG) excludes FIFOs/sockets/devices outright, and
            #      the bounded read means even a regular file that GREW past the cap
            #      AFTER the stat (a TOCTOU race) can never buffer more than cap+1
            #      chars before we refuse it.
            if not target.exists():
                return "read_file failed: no such file"
            if target.is_dir():
                return "read_file failed: path is a directory"
            if not target.is_file():
                return "read_file failed: not a regular file"
            size = target.stat().st_size
            if size > cap:
                return (
                    "read_file failed: file is too large "
                    f"({size} bytes exceeds the {cap}-character output cap)"
                )
            with open(target, encoding="utf-8", errors="replace") as handle:
                text = handle.read(cap + 1)
            if len(text) > cap:
                # Post-stat growth (or an st_size that under-reported the char
                # length): the bounded read caught what the cheap stat did not.
                return (
                    f"read_file failed: file is too large (exceeds the {cap}-character output cap)"
                )
            return tools._cap_output(text, cap)

        try:
            return await run_in_threadpool(_read)
        except FileNotFoundError:
            return "read_file failed: no such file"
        except Exception as exc:
            return f"read_file failed: {type(exc).__name__}"

    async def list_dir(args: dict[str, Any]) -> str:
        raw = args.get("path")
        # An omitted/empty path lists the workspace root; anything else must
        # pass the same containment gate as every other path argument.
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            root = staging.resolve()
        else:
            resolved = _resolve_in_staging(staging, raw)
            if resolved is None:
                return f"list_dir {_PATH_REJECTED}"
            root = resolved

        def _list() -> str:
            if not root.is_dir():
                return "list_dir failed: not a directory"
            base = staging.resolve()
            # Enforce the entry cap DURING the walk, not after. The previous
            # sorted(root.rglob("*")) materialized AND sorted the ENTIRE subtree
            # before slicing, so a pathological build (a run_shell that untarred
            # thousands of files) meant unbounded memory and a long, uncancellable
            # threadpool stretch -- all to then throw most of it away. os.walk with
            # dirnames/filenames sorted IN PLACE yields a deterministic order while
            # holding at most one directory's entries at a time; we stop the instant
            # we have collected one MORE than the cap (that extra entry is only the
            # "there is more" probe -- dropped below in favor of the notice).
            entries: list[str] = []
            truncated = False
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                filenames.sort()
                here = Path(dirpath)
                # Directories (rendered with a trailing "/") then files, each in
                # sorted order -- the same "/"-suffix convention rglob used.
                level = [(name, True) for name in dirnames] + [(name, False) for name in filenames]
                for name, is_dir in level:
                    rel = str((here / name).relative_to(base))
                    entries.append(rel + "/" if is_dir else rel)
                    if len(entries) > _LIST_DIR_MAX_ENTRIES:
                        truncated = True
                        break
                if truncated:
                    break
            if not entries:
                return "(empty directory)"
            if truncated:
                # The exact overflow count is unknowable without the full-tree
                # enumeration this fix exists to avoid, so the notice keeps the
                # "... (truncated)" presentation but drops the (now uncountable)
                # number the old marker carried.
                entries = entries[:_LIST_DIR_MAX_ENTRIES]
                entries.append(
                    f"... (truncated at {_LIST_DIR_MAX_ENTRIES} entries; more not shown)"
                )
            return "\n".join(entries)

        try:
            return await run_in_threadpool(_list)
        except Exception as exc:
            return f"list_dir failed: {type(exc).__name__}"

    async def run_shell(args: dict[str, Any]) -> str:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return "run_shell rejected: command must be a non-empty string"
        settings = get_settings()
        return await run_in_threadpool(
            _run_shell_subprocess,
            staging,
            command,
            settings.tool_install_shell_timeout_seconds,
            settings.llm_tool_output_max_chars,
        )

    return [
        LlmTool(
            spec=_tool_spec(
                "write_file",
                "Create or overwrite a file in the workspace (parent directories are "
                "created automatically).",
                {
                    "path": {"type": "string", "description": "Relative path in the workspace."},
                    "content": {"type": "string", "description": "Full file content (UTF-8)."},
                },
            ),
            handler=write_file,
        ),
        LlmTool(
            spec=_tool_spec(
                "read_file",
                "Read a file from the workspace.",
                {
                    "path": {"type": "string", "description": "Relative path in the workspace."},
                },
            ),
            handler=read_file,
        ),
        LlmTool(
            spec=_tool_spec(
                "list_dir",
                "Recursively list the workspace (directories end with '/').",
                {
                    "path": {
                        "type": "string",
                        "description": "Relative subdirectory to list; empty for the root.",
                    },
                },
            ),
            handler=list_dir,
        ),
        LlmTool(
            spec=_tool_spec(
                "run_shell",
                "Run a bash command with the workspace as the working directory "
                "(bounded runtime and output; network access available).",
                {
                    "command": {"type": "string", "description": "The bash command to run."},
                },
            ),
            handler=run_shell,
        ),
    ]


# --- the OpenAPI fetch -------------------------------------------------------


async def _fetch_openapi(url: str) -> tuple[str | None, str | None]:
    """Fetch the OpenAPI document; returns (text, None) or (None, user_error).

    Bounded on every axis: a TOTAL wall-clock deadline (asyncio.timeout, see
    ``_FETCH_TOTAL_TIMEOUT_SECONDS``) wrapped around the whole fetch, the
    per-phase httpx timeout inside it, the redirect count, and the body SIZE --
    the Content-Length header is checked when present, and the streamed body is
    counted regardless (a server can lie about, or omit, the header). Over the
    cap fails OUTRIGHT (see ``_OPENAPI_MAX_BYTES``). Errors carry only the
    exception CATEGORY, never ``str(exc)`` -- an httpx error string embeds the
    full URL, and while the URL is the user's own input (not a secret), the
    category is what is diagnostic; the URL is already on the user's screen.
    """
    try:
        async with (
            asyncio.timeout(_FETCH_TOTAL_TIMEOUT_SECONDS),
            httpx.AsyncClient(
                timeout=_FETCH_TIMEOUT_SECONDS,
                follow_redirects=True,
                max_redirects=_FETCH_MAX_REDIRECTS,
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code // 100 != 2:
                return None, f"OpenAPI 文件下載失敗（HTTP {response.status_code}）。"  # noqa: RUF001
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > _OPENAPI_MAX_BYTES:
                return None, _ERROR_OPENAPI_TOO_LARGE
            received = 0
            chunks: list[bytes] = []
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > _OPENAPI_MAX_BYTES:
                    return None, _ERROR_OPENAPI_TOO_LARGE
                chunks.append(chunk)
    except Exception as exc:
        # httpx.HTTPError covers transport/timeout/redirect failures; a malformed
        # URL raises httpx.InvalidURL (a ValueError, NOT an HTTPError); and the
        # asyncio.timeout total-deadline expiry surfaces as TimeoutError. The
        # catch is total so every one of them lands on the friendly-outcome path
        # rather than crashing the background job.
        return None, f"OpenAPI 文件下載失敗（{type(exc).__name__}）。"  # noqa: RUF001
    return b"".join(chunks).decode("utf-8", errors="replace"), None


def _builder_user_prompt(instructions: str, openapi_text: str) -> str:
    """The session's user turn: the operator's instructions + the document.

    The OpenAPI text is bounded by the SAME ``llm_prompt_budget_chars`` knob
    that bounds item serialization (one operator-facing "how much may ride in a
    prompt" setting, not a second one), behind the shared truncation marker.
    Instructions are already request-bounded (<=20000) at the API layer.
    """
    budget = get_settings().llm_prompt_budget_chars
    return "\n\n".join(
        [
            "Build the tool described below.",
            "User instructions:",
            instructions,
            "OpenAPI document:",
            _truncate_to(openapi_text, budget),
        ]
    )


# --- staging promotion + cleanup ---------------------------------------------


def _promote_staging(staging: Path, name: str, base: Path) -> str | None:
    """Validate the staged package and move it to ``<base>/<name>``; None = ok.

    Blocking (runs via ``run_in_threadpool``). Validation runs the SAME checks
    the registry applies on every scan (``tools.validate_package``), so a
    package that passes here cannot list as broken after the move. The
    exists-check is LOAD-BEARING, not just a friendly error: ``shutil.move``
    onto an existing directory would nest the staging dir INSIDE it (a
    corrupted install) rather than fail. Check-then-move is a race only
    against a second concurrent install of the same name -- a single-user
    local tool's edge we accept, and the nested-dir result would still be an
    invalid package (name mismatch), never executable.
    """
    error = tools.validate_package(staging, expected_name=name)
    if error is not None:
        return f"工具包驗證失敗：{error}"  # noqa: RUF001
    target = base / name
    if target.exists():
        return _ERROR_NAME_TAKEN
    try:
        base.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(target))
    except OSError as exc:
        return f"工具包搬移失敗（{type(exc).__name__}）。"  # noqa: RUF001
    return None


def _cleanup_staging(staging: Path) -> None:
    """Remove the session's staging dir (if the move did not consume it) and
    drop the ``.staging`` shell when this was the last build in flight.

    Blocking (runs via ``run_in_threadpool``), never raises: cleanup is
    best-effort by definition. ``rmdir`` (not rmtree) on the parent: it only
    succeeds on an EMPTY directory, which is exactly the wanted semantics --
    a concurrent session's staging keeps the shell alive, and the suppressed
    OSError for that case is the mechanism, not an accident.
    """
    with contextlib.suppress(Exception):
        if staging.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
    with contextlib.suppress(OSError):
        staging.parent.rmdir()


# --- the install run ---------------------------------------------------------


async def run_install(openapi_url: str, instructions: str) -> InstallOutcome:
    """Run one whole install: fetch, build in staging, validate, promote.

    Every failure is a FRIENDLY OUTCOME (zh-TW ``error``), never an exception:
    this runs inside a fire-and-forget background job whose only consumer is
    the polling FE, so an exception here would just vanish into the task. The
    ``llm_log_id`` is captured immediately after the builder call -- success
    OR failure -- because a failed build is exactly when the trace matters.
    """
    base = tools.tools_dir()
    if base is None:
        return InstallOutcome(ok=False, error=_ERROR_TOOLS_DISABLED)

    openapi_text, fetch_error = await _fetch_openapi(openapi_url)
    if openapi_text is None:
        return InstallOutcome(ok=False, error=fetch_error)

    staging = base / _STAGING_DIRNAME / uuid4().hex
    try:
        await run_in_threadpool(staging.mkdir, parents=True)
    except OSError as exc:
        return InstallOutcome(ok=False, error=f"無法建立暫存工作區（{type(exc).__name__}）。")  # noqa: RUF001

    try:
        settings = get_settings()
        result: InstallResult | None = None
        llm_error: str | None = None
        try:
            result = await generate_structured(
                _BUILDER_SYSTEM_PROMPT,
                _builder_user_prompt(instructions, openapi_text),
                InstallResult,
                workflow=_WORKFLOW,
                tools=_build_meta_tools(staging),
                max_tool_rounds=settings.tool_install_max_rounds,
                timeout_seconds=settings.tool_install_timeout_seconds,
            )
        except LLMNotConfiguredError:
            llm_error = "LLM 尚未設定，無法執行安裝。"  # noqa: RUF001
        except LLMUpstreamError as exc:
            # str(exc) is safe by construction (category + fixed reason -- see
            # context_memory.services.llm); surfacing it names WHICH failure
            # (timeout vs invalid output vs upstream) without config leakage.
            llm_error = f"AI 建置工具失敗（{exc}）。"  # noqa: RUF001

        # The builder session ran (even a not-configured exit records one), so
        # link its AI 日誌 record to the outcome NOW -- error paths included.
        llm_log_id = llm_log.last_record_id_for_workflow(_WORKFLOW)

        if llm_error is not None:
            return InstallOutcome(ok=False, error=llm_error, llm_log_id=llm_log_id)
        assert result is not None  # exactly one of result/llm_error is set above

        if not result.ready:
            reason = result.summary or "（AI 未說明原因）"  # noqa: RUF001
            return InstallOutcome(
                ok=False,
                summary=result.summary or None,
                error=f"AI 判定工具尚未完成：{reason}",  # noqa: RUF001
                llm_log_id=llm_log_id,
            )

        promote_error = await run_in_threadpool(_promote_staging, staging, result.tool_name, base)
        if promote_error is not None:
            return InstallOutcome(
                ok=False,
                tool_name=result.tool_name,
                summary=result.summary or None,
                error=promote_error,
                llm_log_id=llm_log_id,
            )
        return InstallOutcome(
            ok=True,
            tool_name=result.tool_name,
            summary=result.summary or None,
            llm_log_id=llm_log_id,
        )
    finally:
        # A successful move consumed the staging dir; this then only drops the
        # (possibly empty) .staging shell. Every other exit removes the build.
        await run_in_threadpool(_cleanup_staging, staging)


# --- background jobs ---------------------------------------------------------

# In-memory, process-local, deliberately unpersisted (see the module
# docstring). One lock guards the dict, matching llm_log's pattern; the task
# set only holds strong references so a running install's Task is never
# garbage-collected mid-flight (asyncio keeps only weak refs to tasks). Since
# start_install_job now admits only ONE active install at a time (M7), _TASKS
# holds at most that one in-flight task plus any not-yet-collected finished
# ones -- it cannot grow without bound under rapid submits.
_MAX_JOBS = 20
_JOBS: dict[str, InstallJob] = {}
_JOBS_LOCK = threading.Lock()
_TASKS: set[asyncio.Task[None]] = set()


@dataclass(slots=True)
class InstallJob:
    """One install job's visible state, as polled by the FE."""

    job_id: str
    state: str  # queued | running | succeeded | failed
    created_at: str
    finished_at: str | None = None
    error: str | None = None
    tool_name: str | None = None
    summary: str | None = None
    llm_log_id: int | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _job_dict(job: InstallJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "state": job.state,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "error": job.error,
        "tool_name": job.tool_name,
        "summary": job.summary,
        "llm_log_id": job.llm_log_id,
    }


def _update_job(job_id: str, **changes: Any) -> None:
    """Apply field changes to a job, if it is still tracked.

    A job evicted past ``_MAX_JOBS`` while its task was still running simply
    stops being visible; the task's later updates land here as no-ops. That is
    the accepted cost of a bounded, unpersisted job table.
    """
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        for key, value in changes.items():
            setattr(job, key, value)


async def _run_job(job_id: str, openapi_url: str, instructions: str) -> None:
    """The background task body: run the install, record the outcome.

    ``run_install`` already maps every EXPECTED failure to a friendly outcome;
    the except here is the total backstop for a genuine bug, because an
    exception escaping a fire-and-forget task would otherwise vanish (leaving
    the job stuck on "running" forever from the FE's point of view). Category
    only -- a bug's str() could carry anything.
    """
    _update_job(job_id, state="running")
    try:
        outcome = await run_install(openapi_url, instructions)
    except Exception as exc:
        _update_job(
            job_id,
            state="failed",
            error=f"安裝過程發生未預期錯誤（{type(exc).__name__}）。",  # noqa: RUF001
            finished_at=_now_iso(),
        )
        return
    _update_job(
        job_id,
        state="succeeded" if outcome.ok else "failed",
        error=outcome.error,
        tool_name=outcome.tool_name,
        summary=outcome.summary,
        llm_log_id=outcome.llm_log_id,
        finished_at=_now_iso(),
    )


def start_install_job(openapi_url: str, instructions: str) -> str | None:
    """Create a job and launch its background task; returns the job id, or None
    when an install is ALREADY active (queued|running).

    Only ONE install runs at a time (M7): the active-check and the insert happen
    under the SAME lock, so there is no check-then-start race, and the router
    maps a None return to a 409. This also structurally BOUNDS ``_TASKS`` -- at
    most one install task is ever in flight, so the strong-ref set that keeps a
    running Task alive can no longer grow without limit under rapid submits.

    Must be called with a running event loop (the async router handler is).
    Eviction keeps the newest ``_MAX_JOBS`` by creation time (job_id as a
    deterministic tiebreak for identical timestamps); a TERMINAL (succeeded/
    failed) job stays pollable until evicted and never blocks a new submit.
    """
    job = InstallJob(job_id=uuid4().hex, state="queued", created_at=_now_iso())
    with _JOBS_LOCK:
        # A terminal job never blocks a new submit -- only queued|running does.
        if any(existing.state in ("queued", "running") for existing in _JOBS.values()):
            return None
        _JOBS[job.job_id] = job
        while len(_JOBS) > _MAX_JOBS:
            oldest = min(_JOBS.values(), key=lambda j: (j.created_at, j.job_id))
            del _JOBS[oldest.job_id]
    task = asyncio.create_task(_run_job(job.job_id, openapi_url, instructions))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return job.job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    """One job's state as a JSON-ready dict, or None (unknown/evicted/restart)."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return _job_dict(job) if job is not None else None


def _reset_jobs_for_tests() -> None:
    """Drop all tracked jobs so a test starts clean (tasks, if any, run out)."""
    with _JOBS_LOCK:
        _JOBS.clear()
