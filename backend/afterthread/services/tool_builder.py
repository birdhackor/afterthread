"""Build, install, and revise versioned tool packages.

Both entry points run the same LLM builder session inside
``<tools_dir>/.staging/<uuid>/build`` and reserve ``shell`` beside it for the
assembled artifact. A fresh install assembles an entire package shell and makes
it visible with one rename. A revise copies the resolved version into ``build``,
commits one new version in ``shell``, renames that version under ``versions/``,
and only then atomically publishes ``current``. A tool's draft is also published
as it is, with no session at all (``run_publish_draft``): its files are copied
into ``build`` and committed the way a revise commits its own.

The package-layer ``.env`` is never part of a revised version and is untouched
for the whole revise. Its values are nevertheless registered during the builder
session so prompts, results, summaries, and AI logs keep masking them. Builder
shell access remains deliberately unjailed under D21; the file meta-tools alone
are contained within the build root.

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
import concurrent.futures
import contextlib
import dataclasses
import functools
import logging
import os
import queue
import secrets
import shutil
import stat
import subprocess
import threading
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import httpx2
from pydantic import BaseModel, ConfigDict, model_validator
from starlette.concurrency import run_in_threadpool

from afterthread.config import get_settings
from afterthread.services import (
    llm,
    llm_log,
    token_budget,
    tool_drafts,
    tool_meta,
    tool_runs,
    tools,
)
from afterthread.services.llm import (
    FinalizeTrigger,
    LLMNotConfiguredError,
    LlmTool,
    LLMUpstreamError,
    RunFailure,
    StructuredRunStats,
    generate_structured,
)
from afterthread.services.memory_ai import (
    _TRUNCATION_MARKER,
    _coerce_bool,
    _coerce_str,
    _truncate_to,
)
from afterthread.services.tool_runs import AttachedRun
from afterthread.services.tools import _NAME_RE

_logger = logging.getLogger("afterthread.tool_builder")

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
# _FETCH_TIMEOUT_SECONDS above, which httpx2 applies as a PER-PHASE (connect/
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

# ReviseResult's two extra notes. The handoff is the longer one -- what was found,
# changed, verified and left, for whoever continues -- while operator_action is a
# few instructions and paste-ready KEY=value lines.
_HANDOFF_CAP = 4000
_OPERATOR_ACTION_CAP = 1000

# User-facing (zh-TW) outcome errors that need to be exact in more than one
# place (tests pin them; the FE renders them verbatim). The fullwidth
# punctuation ruff flags as "ambiguous" is authentic zh-TW typography, per the
# suite-wide precedent (tests/test_ai_supersede.py).
_ERROR_TOOLS_DISABLED = "工具功能未啟用（TOOLS_DIR 未設定）。"  # noqa: RUF001
_ERROR_OPENAPI_TOO_LARGE = "OpenAPI 文件過大（超過 2MB 上限）。"  # noqa: RUF001
_ERROR_NAME_TAKEN = "同名工具已存在，請先刪除舊工具再重新安裝。"  # noqa: RUF001
# D36: raised when writing the form secret into the staged .env would push it
# past the 64KiB cap, or when the .env cannot be written safely.
_ERROR_SECRET_ENV_TOO_LARGE = "工具包 .env 加入秘密後將超過大小上限，安裝已取消。"  # noqa: RUF001
_ERROR_SECRET_ENV_WRITE = "工具包 .env 無法寫入秘密值，安裝已取消。"  # noqa: RUF001
# D36/F5: raised when the submitted secret value cannot be serialized into the staged
# ``.env`` safely -- EITHER ``_dotenv_serialize_value`` refuses up front because the only
# available quoting would ESCAPE a ``"``/``\`` and so leak a reversible-but-unredactable
# spelling, OR the post-write round-trip check finds python-dotenv would parse the line
# back to something other than the submitted value. Refusing beats writing a value the
# runtime would parse differently from -- or expose more of than -- what the user submitted.
_ERROR_SECRET_ENV_UNSERIALIZABLE = "秘密值含特殊字元，無法安全寫入工具包 .env，安裝已取消。"  # noqa: RUF001
# Raised when a builder-written root ``.afterthread.meta/`` cannot be removed
# before validation. The strip is fail-closed because that directory will hold
# backend-authored provenance and the version's commit marker.
# Category-only by construction: a fixed string, never a path or a value, since
# the very name that failed to delete could have been chosen to embed a secret.
_ERROR_SIDECAR_STRIP = "無法清除工具包內的後端版本中繼資料，安裝已取消。"  # noqa: RUF001
# A builder-authored root .env is never version content.  Failure to remove it
# must stop publication; otherwise one filesystem edge case silently revives the
# configuration mechanism the prompt and package layout explicitly removed.
_ERROR_BUILDER_ENV_STRIP = "無法清除建置器寫入的 .env，操作已取消。"  # noqa: RUF001
# D40/R8-1: raised when staging itself fails ``_verify_staging_root``'s re-check --
# either the path IS a symlink, or its RESOLVED location no longer sits inside the
# resolved ``<tools_dir>/.staging`` shell (an ancestor swapped for a symlink). A
# TRUE sibling of ``_ERROR_SIDECAR_STRIP`` (same suffix, same category-only shape):
# naming what staging turned OUT to be would be naming attacker-controlled content.
_ERROR_STAGING_TAMPERED = "暫存工作區已被移動或替換，安裝已取消。"  # noqa: RUF001
# ``_verify_staging_root`` could not look at the workspace, the ``.staging`` shell
# or the tools directory, so whether staging is still the real, contained
# directory is not known: refused like a tampered one, but not called tampered --
# the next step is checking permissions and the filesystem, not hunting a swap.
_ERROR_STAGING_UNVERIFIED = "無法確認暫存工作區仍是原本的目錄（檔案系統查詢失敗），操作已取消。"  # noqa: RUF001

# D40 revise-only outcomes. Category-only by the same construction as the install
# ones above: a fixed zh-TW string, never a path and never a value.
_ERROR_REVISE_NOT_FOUND = "找不到要修訂的工具（可能已被刪除）。"  # noqa: RUF001
# Package ``.env`` values must be readable before a revise session because they
# are exported to run_shell and registered with both redactors.
_ERROR_REVISE_ENV_UNREADABLE = "無法讀取既有工具包的 .env，修訂已取消。"  # noqa: RUF001
_ERROR_REVISE_ENV_TOO_LARGE = "既有工具包的 .env 超過大小上限，修訂已取消。"  # noqa: RUF001
# A hand-edited ``.env`` holding a value BELOW the redactor's floor (R1-1). Names
# the CONDITION, the KEYS (web-v6 §5: the operator ruled key names may be shown;
# they already surface in ``env_keys`` and the migration report) and the two
# remedies -- never the value: the whole point is that this value cannot be
# masked, so it must not be echoed by the very message that refuses to expose it.
# It also says WHY a non-secret trips it, because the operator who first met the
# keyless version read "cannot be masked" as "you have a secret here" and could
# not find one: every ``.env`` value is a secret to the redactor, and a setting
# such as a TTL belongs in ``config.env``. ``{keys}`` is filled by
# ``_env_policy_message``. See ``run_revise``'s own gate for why refusing beats
# running.
_ERROR_REVISE_ENV_UNMASKABLE = (
    f"既有工具包 .env 的{{keys}}值短於 {tools._MIN_SECRET_LEN} 字元，修訂已取消："  # noqa: RUF001
    ".env 的每個值都會被當成秘密遮蔽，而太短的值遮不掉。"  # noqa: RUF001
    "如果它是秘密，請加長；如果只是設定（例如 TTL），請把那一行移到同目錄的 config.env。"  # noqa: RUF001
)
# A hand-edited ``.env`` whose assignment line does not spell a value the way this
# system itself would (R5-1, narrowed to that LINE by R6-1 and to EXACT equality by
# R7-1): ``KEY="ab'cd\"ef"`` parses to ``ab'cd"ef``, and every redactor we have only
# ever sees the PARSED value, so the escaped spelling -- which the builder's unjailed
# ``run_shell`` can ``cat`` straight out of the LIVE package, which is outside
# staging but perfectly reachable -- matches nothing and rides into the next round's
# prompt, the AI 日誌 and possibly the summary. A DISTINCT string from
# ``_ERROR_REVISE_ENV_UNMASKABLE`` because the REMEDY differs (lengthen the value vs.
# write the line the plain way), which is the same "same refusal point, different
# actionable cause" split R3-2 made for the sidecar reads. The remedy names the LINE
# rather than only its quoting, because R7-1's equality rule also refuses shapes with
# nothing wrong with their quotes -- a trailing comment, an unquoted value with
# spaces -- and a remedy that sent the operator hunting for escapes they do not have
# would be worse than the refusal. The condition, the KEYS (web-v6 §5, as above) and
# the remedy, never the value -- printing the value in the message that refuses to
# expose it would BE the leak. A setting that merely happens to be written this way
# has the same second way out as a short one, so the message offers it too.
_ERROR_REVISE_ENV_UNMATCHABLE = (
    "既有工具包 .env 中{keys}的寫法無法對應到實際值、無法遮蔽，修訂已取消："  # noqa: RUF001
    "請把該行的值寫成最單純的形式（原值直接寫，或整段用引號包住），"  # noqa: RUF001
    "並移除行尾註解與多餘的跳脫；如果它只是設定，也可以把那一行移到同目錄的 config.env。"  # noqa: RUF001
)
# Revise reads the package's ``config.env`` (web-v6 §4) for the same reason it
# reads ``.env``: its values go into run_shell's environment and its text into
# the prompt, and a builder testing against a different environment than the
# runtime's would be verifying the wrong thing. Category-only, like the two
# ``.env`` read failures above.
_ERROR_REVISE_CONFIG_UNREADABLE = "無法讀取既有工具包的 config.env，修訂已取消。"  # noqa: RUF001
_ERROR_REVISE_CONFIG_TOO_LARGE = "既有工具包的 config.env 超過大小上限，修訂已取消。"  # noqa: RUF001
# A fresh install publishes its package toggle before the final shell rename.
_ERROR_INSTALL_STATE_WRITE = "無法寫入工具的啟用狀態，安裝已取消。"  # noqa: RUF001
# Revise refuses a package that vanished, became an alias, or no longer matches
# the version identity captured before the paid builder session.
_ERROR_REVISE_TARGET_MISSING = "原工具已被刪除，修訂結果未安裝。"  # noqa: RUF001
_ERROR_REVISE_TARGET_ALIAS = "原工具目錄已被替換為連結，修訂已取消。"  # noqa: RUF001
# R10-1: the package of that NAME is still there, but its current version or the
# version this session copied from changed during the minutes the build ran.
# Publishing the old snapshot as a fresh successor would attach it to stale
# lineage and overwrite a supported D21 ``current`` edit. The remedy is to
# re-send the feedback against what is installed now, which is what the message
# says; like every outcome error here it names the condition only.
_ERROR_REVISE_TARGET_REPLACED = "原工具在修訂期間被改動或重新安裝，請確認現況後重新送出意見。"  # noqa: RUF001
# R11-2: we could not establish WHICH package this is before starting -- a missing
# or unreadable ``tool.json``. Refused up front rather than after a full build,
# because the pre-publication identity check would have nothing to compare against.
_ERROR_REVISE_IDENTITY_UNKNOWN = "無法確認原工具的內容（`tool.json` 讀取失敗），修訂已取消。"  # noqa: RUF001
_ERROR_VERSION_ID_WRITE = "無法為工具建立不重複的版本編號，操作已取消。"  # noqa: RUF001
_ERROR_ORIGIN_WRITE = "無法寫入工具版本的來源資料，操作已取消。"  # noqa: RUF001
# A revise's origin text (``_provenance_feedback``) is masked with the known
# secrets, and that query fails closed; ``{kind}`` is the exception's type name.
_ERROR_ORIGIN_MASK = "無法遮蔽工具版本來源資料裡的秘密值（{kind}），操作已取消。"  # noqa: RUF001
# The same, when it is the known secrets that could not all be read: their own
# message names the ``.env`` and what to check (``tools.KnownSecretsIncomplete``).
_ERROR_ORIGIN_MASK_INCOMPLETE = "無法遮蔽工具版本來源資料裡的秘密值，操作已取消。{detail}"  # noqa: RUF001
_ERROR_CURRENT_WRITE = "無法發布工具的新版本，操作已取消。"  # noqa: RUF001
_ERROR_DURABILITY = "無法確認工具資料已安全寫入磁碟，操作已取消。"  # noqa: RUF001

_VID_RETRY_LIMIT = 16


# The manifest-identity helper this module's pre-publication check uses. It LIVES in
# ``tools`` (which this module already imports, so that is the direction with no
# cycle) because the runtime asks the identical question on the identical tuple:
# ``tools._make_handler`` pins a package's identity when its schema is advertised
# to the model and re-checks it before executing, exactly as ``run_revise`` pins
# it at session start and re-checks it before installing a version. Two spellings would be two
# chances to drift, so there is one definition and this alias keeps the private
# name this module's three call sites already read as "the revise's identity".
# See ``tools.package_identity`` for why it is ``tool.json`` and not the directory.
#
# The execution-directory identity that used to sit beside this helper is gone.
# Destructive paths now coordinate on the persistent tools flock, so directory
# moves no longer need to be mapped back to a process-local registry entry.
_package_identity = tools.package_identity


def _mint_vid() -> str:
    """Create the human-readable UTC id used for one immutable version."""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(3)}"


def _choose_unused_vid(versions: Path) -> str | None:
    """Choose a vid whose complete namespace is unused, with bounded retries."""

    try:
        existing = [entry.name for entry in versions.iterdir()]
    except OSError:
        return None
    for _ in range(_VID_RETRY_LIMIT):
        candidate = _mint_vid()
        if not any(name.startswith(candidate) for name in existing):
            return candidate
    return None


def _entry_exists(path: Path) -> bool:
    """Test any directory entry, including a dangling symlink."""

    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _fsync_directory(path: Path) -> bool:
    """Make the directory entries already created under ``path`` durable."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return False
    try:
        os.fsync(fd)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def _fsync_tree(root: Path) -> bool:
    """Persist regular files and only real directories in this tree, bottom-up.

    False unless the whole tree was seen: ``os.walk`` skips a directory it cannot
    list, and hands over as a file an entry whose type check fails, never going
    into it. Verified: a True answer rests on every directory having been listed
    and every entry told apart. Not covered: an entry added after its directory
    was listed.
    """

    def unlisted(error: OSError) -> None:
        # ``os.walk``'s ``onerror``: a directory it cannot list fails the walk.
        raise error

    try:
        directories = [root]
        for dirpath, dirnames, filenames in os.walk(
            root, topdown=True, onerror=unlisted, followlinks=False
        ):
            here = Path(dirpath)
            dirnames.sort()
            filenames.sort()
            real_dirnames: list[str] = []
            for dirname in dirnames:
                info = os.lstat(here / dirname)
                if stat.S_ISLNK(info.st_mode):
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    raise OSError(f"{here / dirname} changed out of directory shape")
                real_dirnames.append(dirname)
            # ``followlinks=False`` declines to descend through a directory
            # symlink but still reports it here. Pruning keeps both traversal and
            # the later fsync list inside the tree.
            dirnames[:] = real_dirnames
            for filename in filenames:
                path = here / filename
                info = os.lstat(path)
                if stat.S_ISDIR(info.st_mode):
                    raise OSError(f"{path} could not be told a directory, so it was not walked")
                if not stat.S_ISREG(info.st_mode):
                    continue
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            directories.extend(here / dirname for dirname in real_dirnames)
        # A real in-tree directory whose filesystem rejects fsync means the
        # durability claim was not established, so callers still fail the build.
        return all(_fsync_directory(path) for path in reversed(directories))
    except OSError:
        return False


# The builder's system prompt. English, like every prompt in this codebase.
# It must carry the ENTIRE package contract (tool.json fields, the name regex,
# the stdin/stdout execution contract, and environment configuration) because the model has
# no other way to learn it -- the runtime that will execute the finished tool
# is not in the conversation. The suggested loop (write -> test via run_shell
# -> fix -> ready) is what makes max_tool_rounds=24 a working budget.
#
# Its one interpolation, ``{shell_timeout}``, is the backend's own run_shell
# timeout in seconds (``_builder_base_prompt``): the model sizes its test
# commands by that number, so it has to be the one that will kill them.
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
- Put non-secret configuration defaults in the tool's own code, using \
`os.environ.get("KEY", "default")` (or the equivalent in another language), so \
the defaults remain part of this version. The operator can override them for \
this package in a `config.env` kept outside your workspace; never create one \
yourself. Do NOT write a `.env` file. If the \
tool needs a credential, name the required environment variable clearly in \
tool.json's description; the operator supplies its value outside this build.
- The workspace-root `.afterthread.meta/` directory belongs to the backend and \
is DELETED before the tool is installed; do not create it. Legacy flat filenames \
such as `.ai_meta.json` and `.afterthread-state.json` are ordinary tool content \
inside this version workspace.
- Prefer Python 3 with ONLY its standard library (urllib.request for HTTP), \
so the tool runs anywhere without installing dependencies. If the environment \
variable TLS_NO_VERIFY is set to a truthy value, skip TLS certificate \
verification (for urllib.request: pass context=ssl._create_unverified_context()).
- `examples.json` at the workspace root is the tool's regression list: the \
inputs it must keep handling. A JSON object \
{"examples": [{"name": "<what this input checks>", "args": {<the JSON object \
piped to the tool>}}, ...]} with at most 10 entries. Record in it the inputs \
you tested with. Only READ-ONLY inputs (searches, lookups): the backend runs \
every example against the real API to verify the tool, so an input that \
creates, changes or deletes data would do so again on every check. Never put a \
secret or personal content in it.

Your meta-tools (the file tools below take paths RELATIVE to the workspace and \
cannot reach outside it):
- write_file {path, content}: create/overwrite a file (parent directories are \
created automatically).
- read_file {path}: read a file back.
- list_dir {path?}: recursively list the workspace (directories end with "/").
- run_shell {command}: run a bash command. It runs with the service's own \
permissions, starting in the staging directory -- treat the staging directory \
as your workspace and keep all your work inside it. It has a timeout of \
{shell_timeout} seconds and capped output; it CAN reach the network, so use curl or \
python3 to probe the real API and to test your tool end to end, e.g.: \
echo '{"query":"test"}' | python3 run.py

Recommended flow:
1. Read the user's instructions and the OpenAPI document; pick the endpoint(s) \
that serve the user's goal.
2. Write run.py and tool.json. Keep configuration defaults in code; never write \
.env.
3. Test with run_shell: pipe a realistic JSON argument object into your entry \
command; verify the output is genuinely useful to an AI assistant (compact, \
relevant, plain text or small JSON).
4. Fix and re-test until it works. Do not stop at "should work" -- prove it.
5. Write the inputs you tested into examples.json.
6. Only then finish with ready=true.

Finish by returning the final JSON object described in the system \
instructions: "tool_name" MUST equal the "name" in tool.json; "summary" is a \
short report of what you built and how you verified it (in the user's \
language); set "ready" to true ONLY if your own run_shell test succeeded. If \
you cannot make it work, set ready=false and explain the blocker in summary. \
Never print or embed secrets in the summary."""


# Appended to the builder system prompt ONLY when the install form supplied a
# secret (D36). It names the secret and tells the model how to USE it without
# ever revealing the value: the value is injected into run_shell's env under the
# given NAME (so the model can live-test the real API), and the backend writes it
# into the finished tool's .env at install time -- therefore the model must not
# ask for it, must not write it (or a placeholder) into any file, must not echo
# it, and the generated tool must read it from its OWN environment. English, like
# the rest of the prompt. Only the NAME is ever interpolated (via a literal
# ``{name}`` substring replace, which also turns each shell-style ``${name}``
# into ``$<NAME>``); the VALUE never appears in the prompt text at all.
_SECRET_PROMPT_ADDENDUM = """\


A secret named {name} has been provided by the user for this install. You do NOT \
know its value and must never try to discover or print it. Two facts about it:
- It is already available as the environment variable {name} inside run_shell, \
so you can live-test the real API with it directly (e.g. \
`curl -H "Authorization: Bearer ${name}" ...`) -- the shell expands it; you \
never see the value.
- The backend will add it to the finished tool's .env automatically at install \
time, under the name {name}.
Therefore: do NOT ask the user for this secret; do NOT write it, or any \
placeholder for it, into .env or any other file yourself; and do NOT echo it \
(no `echo ${name}`, no printing it in run_shell output or in your summary). The \
tool you generate must read {name} from its OWN environment at runtime (the \
runtime injects the tool's .env into the process environment)."""


def _seconds_text(seconds: float) -> str:
    """A backend duration setting as a prompt states it: ``120``, ``45.5``."""
    return f"{seconds:g}"


def _duration_text(seconds: float) -> str:
    """A session budget in words: whole minutes when it is a whole number of
    them, seconds otherwise -- never a rounded figure the deadline does not keep."""
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    return f"{_seconds_text(seconds)} seconds"


def _builder_base_prompt() -> str:
    """``_BUILDER_SYSTEM_PROMPT`` with its run_shell timeout filled in.

    The value is a number the backend itself enforces -- the same setting the
    ``run_shell`` meta-tool hands ``_run_shell_subprocess`` on every call -- the
    only kind of value this prompt's own text is ever interpolated with, never
    operator or file text.
    """
    seconds = get_settings().tool_install_shell_timeout_seconds
    return _BUILDER_SYSTEM_PROMPT.replace("{shell_timeout}", _seconds_text(seconds))


def _builder_system_prompt(secret_name: str | None) -> str:
    """The builder system prompt, plus the secret addendum when one was supplied.

    Only the NAME is interpolated (never the value). ``str.replace`` (not
    ``str.format``) so the many literal ``$``/``{`` characters in the shell
    examples above are left untouched -- only the ``{name}`` token is swapped,
    and only inside the addendum, after the base prompt's own number is in.
    """
    base = _builder_base_prompt()
    if not secret_name:
        return base
    return base + _SECRET_PROMPT_ADDENDUM.replace("{name}", secret_name)


# Appended to the builder system prompt for a REVISE session (D40). Same shape
# and same interpolation discipline as the secret addendum above -- English,
# model-facing, and only the package NAME is ever substituted (a literal
# ``{name}`` replace, so the base prompt's shell examples keep their ``$``/``{``).
#
# It has to correct three things the base prompt states for a FRESH build, or the
# model works against the backend rather than with it:
#
# * the workspace is not empty and the goal is not "build a tool" -- it already
#   holds the installed package, and untouched files must stay untouched;
# * there is no ``.env`` in a revise workspace (it is package-layer operator
#   state), so the addendum says where those values
#   actually are: already exported into run_shell's environment under their own
#   names. Without this the model reads the absence as "the tool has no
#   credentials" and starts inventing them;
# * ``tool_name`` is not a free choice here. It addresses WHICH installed package
#   gets replaced, so a rename is refused rather than honored -- and saying so up
#   front is cheaper than discarding a finished revision over it.
_REVISE_PROMPT_ADDENDUM = """\


REVISION MODE. The tool package {name} ALREADY EXISTS and its files are ALREADY \
IN YOUR WORKSPACE. You are not building a new tool: revise THIS package to \
address the user's feedback below, and leave everything the feedback does not \
ask you to change exactly as you found it.

What differs in this workspace from a fresh build:
- The package's operator-owned `.env` (secrets) and `config.env` (setting \
overrides) remain outside this version workspace. Do not write either one, do \
not invent placeholder values, and do not make your changes depend on rewriting \
them -- any `.env` you write is stripped before publication, and a `config.env` \
in the workspace is never loaded. Their values are \
already exported into your run_shell environment under their own names \
(`config.env` first, `.env` on top), so you \
can still live-test the real API (e.g. `echo '{"query":"test"}' | python3 \
run.py`) without ever seeing the secrets. If the user's feedback asks to CHANGE \
a secret value or an overridden setting, say so in your summary -- that is done \
by hand, not here.
- Never write a secret value into any file. A package that embeds a known \
secret is REFUSED, and that refusal discards your whole revision.
- The backend-owned root `.afterthread.meta/` directory is missing from this \
workspace and is rebuilt after you finish; do not create or edit it. Any legacy \
flat `.ai_meta.json` or `.afterthread-state.json` file you find here is this \
version's tool content: leave it alone unless the feedback asks you to change it.
- If the workspace has an `examples.json`, run EVERY example in it (pipe its \
"args" into the entry command) after your change, and finish with ready=true \
only if all of them still succeed (exit code 0, no timeout): a change that \
fixes one input and breaks another is not done. Keep the file and its \
examples; add to it rather than replace it. If there is none yet, create one \
as described above.

Finish exactly as described above, with one added rule: "tool_name" MUST be \
exactly {name}, because it names the installed package this revision updates \
-- any other value is treated as an attempt to rename the tool and the revision \
is discarded. "summary" reports what you CHANGED and how you verified it."""


# Appended after the revise addendum: the session's real limits, how to spend
# them, how to close when they run out, and what to leave for the operator
# whether or not the revision is finished. English and model-facing like the
# rest. Every ``{token}`` here is a number the backend itself enforces, filled in
# by ``_revise_system_prompt`` with ``str.replace`` -- the tool-call cap per reply
# straight from ``llm._MAX_TOOL_CALLS_PER_REPLY``, so the prompt can never quote
# a cap the loop does not apply.
#
# ``handoff`` and ``operator_action`` are the model's own account, for display and
# handover only: nothing in the backend decides anything by reading them, and how
# the session REALLY ended is what the loop measured (``StructuredRunStats``).
_REVISE_BUDGET_ADDENDUM = """\


BUDGET. The backend enforces these limits on this session:
- at most {rounds} tool rounds (every reply of yours that calls tools is one \
round);
- {session} of wall-clock time for the whole session. When that runs out the \
session ends at once, and you never get to write your final answer;
- each run_shell command is killed after {shell_timeout} seconds;
- the conversation itself, every tool result included, may grow to about \
{conversation_tokens} tokens.
When you run out of rounds, or the conversation grows past that size, the tool \
phase is over: you are told that the tool-call budget is exhausted, no further \
tool call runs, and your next reply must be the final JSON object.

Save rounds. One reply may call several tools at once, but at most {max_calls} \
calls of a reply are executed: calls past the first {max_calls} are rejected \
without running. Combine short tests into ONE run_shell command, as long as the whole \
command finishes well within {shell_timeout} seconds -- otherwise split it into \
several commands. Print each case's result on its own line, and treat the whole \
batch as failed if any one case fails.

If you cannot finish -- including when you are told that the tool-call budget \
is exhausted:
- set "ready" to false. Once told the budget is exhausted, answer ready=true \
only if your last test run already proved that the change works;
- write "handoff" for whoever continues this work: what you found, what you \
changed and where, what you verified, what is left, and the next step. Do not \
copy raw tool arguments, command output or personal content into it.
When you do finish, leave "handoff" empty.

Whether you finish or not, if the operator has to act outside your workspace \
(change a setting, replace a credential), write "operator_action": what to do, \
in the operator's language. For a setting, give the exact KEY=value line(s) to \
paste into the package's config.env. For example: your change is verified with \
a new value set in run_shell, but the package's config.env still holds the old \
one -- finish with ready=true and put the line to paste in "operator_action". \
Leave "operator_action" empty when the operator has nothing to do."""


# Appended last for a round that continues a draft. ``{attempt}`` is
# the round's number in the draft, a count the backend keeps, filled in with
# ``str.replace`` like the budget's numbers.
_CONTINUATION_PROMPT_ADDENDUM = """\


CONTINUATION. This is attempt {attempt} at this revision. Your workspace is what \
the earlier attempts left, not a clean copy of the installed version, and it may \
also hold changes the operator made by hand since. Check its current state \
first. Do not redo what is already done, and do not undo an earlier change \
without a reason."""


def _revise_system_prompt(name: str, *, attempt: int | None = None) -> str:
    """The builder system prompt, reframed for revising the package ``name``.

    ``attempt`` is the round's number in the draft it continues, None for a first
    round: the continuation addendum then says which attempt it is and what its
    workspace holds.

    Blocking, and called from a worker (R2-4): ``redact_known_secrets`` below is
    the whole-tools-directory scan, the same reason ``_revise_user_prompt``
    hops off the loop.

    The INTERPOLATION is redacted, the PROMPT is not, and the line between them is
    the point (R2-4). ``name`` is operator/filesystem-derived -- a directory name
    the operator chose -- so a registered ``.env`` value can equal it, and without
    this the system prompt would carry that value verbatim into the request while
    every other text in this session is masked. The static prompt text around it
    gets NO such treatment: it is backend-authored, open-source constant text, so
    masking it would be both useless and destructive. Useless because a value that
    happens to equal published constant text is not something the model learns
    from us; destructive because the redactor matches any occurrence at or above
    ``tools._MIN_SECRET_LEN`` (6) -- a hand-edited ``.env`` holding
    ``TOKEN=secret`` would shred our own instructions ("Never write a secret value
    into any file") into redaction markers and leave the model reading a prompt
    with holes in it. Only operator/filesystem-derived interpolations are
    redactable, and this addendum has exactly one.

    The knock-on is accepted, not overlooked: if the package's own NAME is a
    registered secret value, the model is told to answer with the MASKED name, its
    answer cannot equal ``name``, and ``run_revise``'s identity check fails the
    revise. That is the right outcome for a setup where the "secret" is also the
    public tool name -- the value is already in the tools listing, the job poll and
    the FE's URL, so the honest answer is that it cannot be kept out of a prompt
    that must name the package.

    The budget addendum's numbers are the settings this session actually runs
    under -- the ``max_tool_rounds``/``timeout_seconds`` ``run_revise`` passes to
    ``generate_structured`` and the limits the loop and ``run_shell`` apply -- and
    each piece is filled in on its own, so no value can land inside another
    piece's token.
    """
    settings = get_settings()
    budget = (
        _REVISE_BUDGET_ADDENDUM.replace("{rounds}", str(settings.tool_install_max_rounds))
        .replace("{session}", _duration_text(settings.tool_install_timeout_seconds))
        .replace("{shell_timeout}", _seconds_text(settings.tool_install_shell_timeout_seconds))
        .replace("{conversation_tokens}", f"{settings.llm_tool_conversation_budget_tokens:,}")
        .replace("{max_calls}", str(llm._MAX_TOOL_CALLS_PER_REPLY))
    )
    continuing = (
        "" if attempt is None else _CONTINUATION_PROMPT_ADDENDUM.replace("{attempt}", str(attempt))
    )
    return (
        _builder_base_prompt()
        + _REVISE_PROMPT_ADDENDUM.replace("{name}", tools.redact_known_secrets(name))
        + budget
        + continuing
    )


def _masked_and_capped(text: str, cap: int) -> str:
    """``text`` with every secret known NOW masked, then stripped, then cut to ``cap``.

    The one definition of that order for the model-authored fields whose contract
    is a length cap after masking: the summary (``_SUMMARY_CAP``), the handoff
    (``_HANDOFF_CAP``) and operator_action (``_OPERATOR_ACTION_CAP``). Both
    maskings of these fields in this module go through it -- the first when the
    result model validates the model's reply, the second when ``run_install`` or
    ``run_revise`` builds the outcome. Mask while the text is whole, so a secret
    straddling the cap is masked rather than cut into an interior fragment no
    later mask can match; strip after the mask, so a registered value with edge
    whitespace still matches; cut last, so a mask that lengthens the text -- the
    marker is longer than a short secret, and a secret registered between the two
    maskings is masked by the second alone -- cannot carry it past the cap.

    What the cap guarantees: at most ``cap`` characters (code points, not encoded
    bytes) after each call. What it does not cover: a secret registered after the
    last call stays unmasked; the cut can leave part of a marker at the end; and a
    masking that does not go through here has no cap -- the rename refusal's mask
    of an attempted name, and ``tool_name`` itself, which is cut but never masked,
    are not among these fields.
    """
    return tools.redact_known_secrets(text).strip()[:cap]


def _install_result_fields(data: Any) -> dict[str, Any]:
    """``InstallResult``'s three fields, sanitized from the model's raw object.

    The body of ``InstallResult._sanitize``, kept as a function so
    ``ReviseResult._sanitize`` builds on exactly the same three fields without
    calling the decorated validator, which the type checker does not see as
    callable.
    """
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    # The summary's first mask-then-cap (``_masked_and_capped`` holds the order and
    # why). The secrets registered for the session -- an install's form secret, a
    # revise's package values -- are known here, inside generate_structured's
    # model_validate. run_install and run_revise mask the summary again when they
    # build the outcome, through the same function: a secret registered in between
    # is masked then, and the cap is applied again after it.
    return {
        "tool_name": _coerce_str(data.get("tool_name")).strip()[:_TOOL_NAME_MAX],
        "summary": _masked_and_capped(_coerce_str(data.get("summary")), _SUMMARY_CAP),
        "ready": _coerce_bool(data.get("ready")),
    }


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
        return _install_result_fields(data)

    @model_validator(mode="after")
    def _ready_requires_valid_name(self) -> InstallResult:
        if self.ready and not _NAME_RE.match(self.tool_name):
            raise ValueError(
                "a ready result must carry a tool_name matching ^[a-z0-9][a-z0-9_-]{0,63}$"
            )
        return self


# A revise session's close: ``InstallResult`` plus two notes for people. Both are
# the model's own account, kept for display and handover only: no logic reads them
# to decide anything, and how the session really ended is what the loop measured
# (``StructuredRunStats``), never taken from here. Only a revise asks for them; the
# install keeps ``InstallResult``, whose schema has no notes.
#
# The docstring is MODEL-FACING: pydantic makes a model's docstring the
# "description" of its JSON Schema, which ``_schema_guided_system_prompt`` shows
# the model verbatim. So it says only what the model needs, in the prompt's terms.
class ReviseResult(InstallResult):
    """The final answer of a revision. "handoff" is only for an unfinished
    revision, for whoever continues it: what you found, what you changed and
    where, what you verified, what is left, and the next step; leave it empty
    when you finish. "operator_action" is what the operator must still do by
    hand outside the workspace, whether you finish or not -- such as a line to
    paste into the package's config.env; leave it empty when there is
    nothing."""

    handoff: str = ""
    operator_action: str = ""

    @model_validator(mode="before")
    @classmethod
    def _sanitize(cls, data: Any) -> dict[str, Any]:
        # The same name as the parent's validator ON PURPOSE: it replaces it rather
        # than running beside it, because the parent's rebuilds the dict from its
        # own three fields and would drop these two. The three are still built by
        # the same function the parent uses (the not-a-dict refusal included).
        #
        # Each note's first mask-then-cap goes through the summary's
        # ``_masked_and_capped``, with the note's own cap.
        fields = _install_result_fields(data)
        fields["handoff"] = _masked_and_capped(_coerce_str(data.get("handoff")), _HANDOFF_CAP)
        fields["operator_action"] = _masked_and_capped(
            _coerce_str(data.get("operator_action")), _OPERATOR_ACTION_CAP
        )
        return fields


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
    # Names extracted from a builder-written .env before that file was stripped.
    # Values are intentionally not representable in the outcome or job schema.
    env_keys: tuple[str, ...] = ()
    # True only for the builder's own ``ready=false`` close (web-v6 §8.3): the
    # session ran and the model chose not to publish. Kept apart from every other
    # failure because it is not one in the same sense -- the model may have found
    # an external cause (an expired credential) or simply given up, and the job
    # card must say "not published" rather than show the red "failed" both share
    # today, without claiming to know which of the two it was.
    not_ready: bool = False
    # How the published version did on its own examples.json (web-v6 §8.3): set
    # by a revise that published, None otherwise. A ``_verification_dict``.
    verification: dict[str, Any] | None = None
    # A revise's builder session as the backend measured it (``StructuredRunStats``),
    # set on every outcome whose session ran -- a failed one included, since a
    # failure is when the operator most needs to know which limit, if any, cut it
    # short. ``stopped_by`` is the budget that ended the tool phase, ``failure``
    # how the session failed, ``rounds_used`` of ``max_rounds`` the tool rounds.
    # All None when no session ran (and on every install).
    stopped_by: FinalizeTrigger | None = None
    failure: RunFailure | None = None
    rounds_used: int | None = None
    max_rounds: int | None = None
    # The revise model's own notes (``ReviseResult``), masked again at outcome
    # construction like ``summary``. Display and handover only.
    handoff: str | None = None
    operator_action: str | None = None
    # What became of this round's draft (``tool_drafts.SaveResult.report``): kept,
    # or why not, and where the work is when neither the draft nor a version has
    # it. None when the round had nothing to keep, published and took its draft
    # away, or never ran.
    draft: dict[str, Any] | None = None
    # What a publish of a draft as it is did with that draft (``run_publish_draft``,
    # ``_draft_publish_report``); None on every other kind of job.
    draft_publish: dict[str, Any] | None = None


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


# What run_shell hands back for a command its session had already stopped: never
# started, or killed part-way. Model-facing English like every run_shell result,
# though by then the session that would read it is over.
_SHELL_NOT_STARTED = "command not run: the session has ended"
_SHELL_STOPPED = "command stopped: the session has ended"
# What write_file hands back for a write whose worker began only after its session
# had stopped: the file was not written. Model-facing English, like the two above.
_WRITE_NOT_DONE = "write_file not done: the session has ended"


@dataclass(slots=True, eq=False)
class _ShellEvidence:
    """What the worker running one run_shell command can vouch for (``_BuilderShells``).

    ``entered`` is set by the worker before anything else, the stop check
    included; the event loop reads it. ``spawning`` and ``confirmed`` are written
    on the worker's own thread, at the points ``_run_shell_subprocess`` names --
    ``confirmed`` by the drain's teardown itself -- and read only by that worker,
    which reports ``stopped()`` to the loop.
    """

    entered: threading.Event
    spawning: bool = False
    confirmed: bool = False

    def confirm(self) -> None:
        """Record that the command left no process: its leader was reaped and its
        group then seen gone -- by the drain's teardown, as its ``on_reaped``, or
        by CPython after a failed exec -- or its start failed before the fork."""
        self.confirmed = True

    def stopped(self) -> bool:
        """Whether the command is known to have no process left. Stopped: its
        leader was reaped -- by the drain's teardown, or by CPython after a failed
        exec -- and its process group then seen gone. Never started: it did not
        get as far as starting, or its start failed before the fork."""
        return self.confirmed or not self.spawning


class _ShellPopen(subprocess.Popen[str]):
    """The ``Popen`` a run_shell command is started with, kept when the start fails.

    ``subprocess.Popen`` raises out of its constructor, and the object would go
    with the exception -- and with it CPython's own record of the start: whether
    it forked a child (``pid``) and whether it reaped that child again
    (``returncode``). This subclass keeps the object: it catches the ``OSError``
    its constructor raises and holds it as ``spawn_error``, so
    ``_start_left_no_process`` can read that record. ``pid`` and ``returncode`` are
    set to None before ``Popen`` runs, so a failure before ``Popen`` sets them
    itself still reads as nothing forked, nothing reaped.

    Only ``OSError`` is caught. Anything else -- one raised after the fork and
    before ``Popen`` records the pid, a ``MemoryError`` in C, say -- would read as
    nothing forked and be taken for evidence; it propagates instead, and the
    command stays unconfirmed. The error is kept without its traceback, whose
    frames hold this object: a still-running child's ``Popen`` then reaches
    ``__del__`` as soon as it is dropped, not at some later garbage collection.
    """

    pid: int | None  # None until CPython has forked
    spawn_error: OSError | None

    def __init__(self, command: str, *, cwd: Path, env: dict[str, str]) -> None:
        self.pid = None
        self.returncode = None
        self.spawn_error = None
        try:
            super().__init__(
                ["bash", "-c", command],
                cwd=cwd,
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
            self.spawn_error = exc.with_traceback(None)


def _start_left_no_process(proc: _ShellPopen) -> bool:
    """Whether a run_shell command whose start failed is known to have no process.

    Read from CPython's own record on the kept ``Popen``. No pid: the start
    failed before the fork, so nothing was started. A pid and a return code:
    CPython reaped the child before raising -- it does that for a child that
    reported a failed exec, one that never ran the command and so forked nothing
    -- and the group is then seen gone; asked once, since nothing is left to
    leave it. Anything else is a child forked and not reaped, which may be
    running the command now.
    """
    if proc.pid is None:
        return True
    return proc.returncode is not None and tools._process_group_is_gone(proc.pid)


def _run_shell_subprocess(
    staging: Path,
    command: str,
    timeout: float,
    cap: int,
    extra_env: dict[str, str] | None = None,
    abort: threading.Event | None = None,
    evidence: _ShellEvidence | None = None,
) -> str:
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

    ``extra_env`` is the ONE deliberate addition to the otherwise from-scratch
    env: the install-form secret (D36), ``{<NAME>: <value>}``, present ONLY when
    the user supplied one -- or, on a REVISE (D40), the whole existing package
    ``.env``, which is withheld from staging and so has nowhere else to come
    from. Either way it exists so the builder can live-test the real API. It is
    layered on top of the passthrough allowlist -- our own ``OPENAI_*`` secrets
    stay absent because the BASE env is still built from the allowlist alone --
    and it never enters any prompt (only this process env), so the model can use
    the keys without ever seeing their values.

    A SECOND, settings-derived addition sits between the allowlist and
    ``extra_env``: when ``settings.tls_no_verify`` is on, ``TLS_NO_VERIFY=1`` is
    injected too, so a builder-tested ``curl``/``python3`` invocation can honor
    the same TLS opt-out the backend's own outbound connections do (see
    config.py); it is simply absent -- never ``"0"`` -- when the flag is off,
    mirroring the passthrough allowlist's own "absent means not set" contract.

    Output is drained by ``tools._communicate_bounded`` (NOT ``communicate``),
    which caps the single merged pipe as it reads instead of slurping the whole
    stream first: a command that spews far past the cap is killed at the cap, so a
    runaway ``yes``/``cat`` can never OOM the service before the cap is applied.

    ``abort`` is the session's stop signal (``_BuilderShells``). Already set when
    this starts, the command is not started at all; set while it runs -- or
    between this check and the spawn -- ``_communicate_bounded`` kills and reaps
    its process group at its next poll. Omitted, nothing here changes.

    ``evidence`` records the only points at which the command is known to be
    stopped. ``spawning`` is set the instant before the start. ``confirmed`` is
    set by the teardown in ``_communicate_bounded``, through ``on_reaped``, once
    the leader is reaped and its group is then seen gone -- asked at once, and
    again until the teardown's time limit -- whether the drain then returns or
    raises. A start that fails with an ``OSError`` sets it only on CPython's own
    record of that start (``_ShellPopen``, ``_start_left_no_process``): no pid --
    it failed before the fork, so nothing was started -- or a pid CPython has
    reaped again, its group then seen gone. A child forked and not reaped leaves
    the command unconfirmed. Whatever happens after ``confirmed`` is set stays
    confirmed (a redaction that fails closed, say); an exception that leaves the
    drain before its teardown worked leaves the command unconfirmed.
    """
    if abort is not None and abort.is_set():
        return _SHELL_NOT_STARTED
    env = {name: os.environ[name] for name in tools._PASSTHROUGH_ENV if name in os.environ}
    if get_settings().tls_no_verify:
        env["TLS_NO_VERIFY"] = "1"
    if extra_env:
        env.update(extra_env)
    if evidence is not None:
        evidence.spawning = True
    proc = _ShellPopen(command, cwd=staging, env=env)
    if proc.spawn_error is not None:
        failed = f"command failed to start: {type(proc.spawn_error).__name__}"
        if not _start_left_no_process(proc):
            # Forked and not reaped: the child may be running the command, and the
            # command stays unconfirmed. It is left alone on purpose. Not killed:
            # the child may not have called setsid yet, so a killpg on its pid
            # could miss it. Not waited for: ``proc.wait()`` has no bound. CPython's
            # ``Popen.__del__`` keeps the object in ``subprocess._active``, and a
            # later ``Popen`` reaps the child once it has exited.
            return failed
        if evidence is not None:
            evidence.confirm()
        return failed
    # One output pipe (stderr is merged into stdout); no stdin (DEVNULL).
    output = tools._communicate_bounded(
        proc,
        input_text=None,
        cap=cap,
        timeout=timeout,
        abort=abort,
        on_reaped=evidence.confirm if evidence is not None else None,
    )
    if output.aborted:
        return _SHELL_STOPPED
    if output.timed_out:
        return f"command timed out after {timeout:g} seconds"
    # F1/D36: mask any known secret VALUE in the builder shell's output BEFORE the
    # size cap (redact-before-truncate, the same order as tools._run_tool_subprocess
    # and llm_log). run_shell is the EXACT vector D36 calls out -- a disobedient
    # builder `echo "$KB_API_KEY"` puts the value on stdout, which the llm loop wraps
    # VERBATIM into the next round's role:"tool" message -- so masking the result here
    # keeps the value out of the LIVE builder conversation (the in-flight install
    # secret is registered as known for the whole build; see run_install). Applied on
    # the taken branch only, so known_secret_values is computed once.
    # Streamed past the cap: return the truncated (already marked) output whatever
    # the kill-induced exit code, mirroring the runtime tool's over-cap handling.
    try:
        if output.stdout_overflow:
            return tools._cap_output(tools.redact_known_secrets(output.stdout), cap)
        if proc.returncode != 0:
            body = tools._cap_output(tools.redact_known_secrets(output.stdout).strip(), cap)
            return f"command failed (exit {proc.returncode}): {body}"
        return tools._cap_output(tools.redact_known_secrets(output.stdout), cap)
    except Exception:
        # The command RAN; only its output could not be checked against the known
        # secrets, so none of it is handed over. Said as it is, the way the
        # runtime tool says it: told it failed, the builder may run it again.
        return tools.output_withheld_text("command", proc.returncode)


def _workspace_kept_error(session_root: Path) -> str:
    """The job error for a session whose workspace is kept, its commands or its
    writes not confirmed ended (``_BuilderShells``). Appended to the session's own
    error.

    The path is shown as it is: the backend built it from the configured tools
    directory, a fixed name and a random id, and the operator has to copy it to
    act on it. A plain string, built on the event loop: the exit that reports it
    must not wait on a worker thread.
    """
    return (
        "工作階段結束後，有 run_shell 指令或 write_file 寫入無法確認已經停止：這次的成果不會發布，"  # noqa: RUF001
        f"暫存工作區原地保留在 {session_root}，確認沒有殘留的程序還在用它後可以手動刪除。"  # noqa: RUF001
    )


def _log_workspace_kept(session_root: Path) -> None:
    """Say in the backend log where a kept workspace is, and why it was kept."""
    with contextlib.suppress(Exception):
        _logger.warning(
            "Builder workspace kept at %s: its run_shell commands or write_file writes "
            "could not be confirmed stopped, so it was neither published nor removed.",
            session_root,
        )


def _shell_stop_seconds() -> float:
    """How long a builder session waits, once stopped, for its run_shell commands
    and its write_file writes: one limit for all of them together.

    The bounded part of what a command already running could still take without
    the stop signal: the rest of its own timeout, then the teardown in
    ``_communicate_bounded``, whose bounded waits -- the re-sent group kill, the
    watch for the group to be gone, the join of its one output reader -- share
    the single limit ``_REAP_TIMEOUT_SECONDS``; its reap of the leader has no
    bound. A write has no limit of its own and shares this one. The signal
    normally ends the wait within one poll and the teardown, and a write ends as
    soon as it is written; the bound only matters for a worker that entered and
    cannot report in time (a leader SIGKILL cannot reap, a thread stuck before its
    first poll or in a write the kernel does not finish, a redaction scan that
    hangs -- a worker reports only as it returns).
    """
    return get_settings().tool_install_shell_timeout_seconds + tools._REAP_TIMEOUT_SECONDS


class _BuilderShells:
    """The run_shell commands and write_file writes of ONE builder session, and the
    signal that stops them.

    ``run_install``/``run_revise`` create one per session and hand it to
    ``_build_meta_tools``; it is never module state, because other work (a health
    check, an AI request's tool call) runs subprocesses of its own meanwhile.

    Each command, and each write, runs as a task of its own, kept here by a strong
    reference and awaited through ``asyncio.shield`` -- the shape of
    ``tools._make_handler``. The session's deadline, or a cancellation of the
    session, then cancels only the handler's await: the task, and the worker thread
    it waits on, go on to their end.

    The two count as ended on different evidence, each from the worker running it,
    and neither stands in for the other.

    A write counts as ended once its worker has returned, its write done or failed:
    a worker that has returned writes no more. The worker reports that from a
    ``finally`` around the write, through ``loop.call_soon_threadsafe``. Not the
    write's task: a cancelled task is done while its worker writes on. Not the
    handler's await: the deadline cancels it with the file cut short.

    A command counts as stopped on evidence from the worker running it only
    (``_ShellEvidence``): its leader was reaped -- by the drain's teardown, or by
    CPython after a failed exec -- and its process group then seen gone, or it
    never started a subprocess (it did not get as far as starting, or its start
    failed before the fork). The worker reports which, from a ``finally`` around
    ``_run_shell_subprocess``, through ``loop.call_soon_threadsafe``. Nothing
    else counts. Not a start that failed after the fork: its child is not reaped
    and may run the command. Not what the group kill answered: a SIGKILL
    delivered is not a member exited (``_communicate_bounded`` says more). Not the
    command's task: a cancelled task is done too, and the event loop cancels every
    task as it shuts down while the worker runs on. Not the worker returning: it
    can return through an exception after the spawn and before the teardown. Not
    the clock: running out of time says only that no word came.

    While the loop shuts down it runs the gather of every task it cancelled, the
    session waiting here among them, so it is still running to take a worker's
    report for as long as the session waits. A worker reporting after its loop
    has closed finds nobody waiting; ``call_soon_threadsafe`` raising for the
    closed loop is swallowed.

    A worker that has not entered when the stop is set never starts its command,
    nor its write. The worker sets ``entered`` and only then reads the stop
    signal; ``stop_and_wait`` sets the stop and only then reads ``entered``. Under
    the GIL those four steps happen in one order, so a worker read here as not
    entered reads the stop as set when it gets there, and returns without
    spawning or writing. Such a command counts as never started, and such a write
    as never written, with no wait -- whether its pool runs it later or never.

    What it guarantees: once ``stop_and_wait`` returns True, no command this
    session ran has a process left: its leader was reaped and its process group
    was then seen gone, or it never started one -- it did not get as far as
    starting, or its start failed before the fork. And no write of this session
    is still writing: its worker returned, or never entered and so never wrote.
    ``stop_and_wait`` sets the stop signal before it waits: a command still
    running is killed at ``_communicate_bounded``'s next poll, by that function's
    own teardown under its own lock, and a command not spawned yet is never
    started -- or, spawned in the instant after that check, is killed at its first
    poll. Nothing here kills a process group from outside, so the guard opens no
    way to signal a pid that has been reaped and reused. A write already in its
    worker is not stopped: it is waited for.

    What it does not cover:

    * a descendant that leaves the process group (``setsid``, a double fork): the
      group can be gone while it lives on. run_shell is a real shell with the
      service's own rights, not a sandbox, and the group kill is all a stop can do
      -- as it is all a timeout can do. A member the service may not signal (a
      setuid program) is not such a case: it keeps the group in existence, so the
      command counts as unconfirmed;
    * a command or a write that cannot be confirmed ended: a command's worker
      reported it unconfirmed, or a worker -- a command's or a write's -- entered
      and sent no report within ``_shell_stop_seconds``, one limit for all of them
      (a leader SIGKILL cannot reap, a thread that is stuck), or a second
      cancellation of the session cut the wait short. The callers then give up
      saving, publishing and removing that workspace for good: it stays where it
      is, named in the backend log, and no later job touches it. Unless the
      session itself was cancelled, the job still ends -- failed, its error naming
      the workspace -- and gives up its slot: the slot keeps the backend's writers
      apart, and a session that has given up writing is no longer one. That exit
      waits on nothing a stuck worker could hold, the shared thread pool above
      all;
    * an interpreter without the GIL. Counting a worker read as not entered as
      never started rests on the four steps of the handshake above happening in
      one order, which the GIL gives; without it ``Event.is_set`` reads its flag
      without taking the event's lock, and the argument would have to be made
      again.
    """

    __slots__ = ("_commands", "_stop", "_tasks", "_writes")

    def __init__(self) -> None:
        self._stop = threading.Event()
        # Strong references, so a command or a write whose handler stopped waiting
        # is never collected mid-flight.
        self._tasks: set[asyncio.Task[str]] = set()
        # One entry per command: what its worker keeps, and the future that carries
        # the worker's report -- stopped or not -- to the loop. Only that worker
        # resolves it.
        self._commands: list[tuple[_ShellEvidence, asyncio.Future[bool]]] = []
        # One entry per write: the event its worker sets as it enters, and the
        # future that carries the worker's word that it returned. Only that worker
        # resolves it.
        self._writes: list[tuple[threading.Event, asyncio.Future[bool]]] = []

    async def run(
        self,
        staging: Path,
        command: str,
        timeout: float,
        cap: int,
        extra_env: dict[str, str] | None,
    ) -> str:
        """Run one command for the session; returns run_shell's result text."""
        loop = asyncio.get_running_loop()
        evidence = _ShellEvidence(threading.Event())
        report: asyncio.Future[bool] = loop.create_future()
        self._commands.append((evidence, report))
        stop = self._stop

        def worker() -> str:
            try:
                evidence.entered.set()
                return _run_shell_subprocess(
                    staging, command, timeout, cap, extra_env, stop, evidence
                )
            finally:
                stopped = evidence.stopped()
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._settle, report, stopped)

        task = asyncio.ensure_future(run_in_threadpool(worker))
        self._tasks.add(task)
        task.add_done_callback(self._forget)
        return await asyncio.shield(task)

    async def write(self, perform: Callable[[], str]) -> str:
        """Run one write for the session; returns write_file's result text.

        ``perform`` makes the write, on the worker, and gives the result text. A
        worker that enters once the session has stopped does not call it.
        """
        loop = asyncio.get_running_loop()
        entered = threading.Event()
        report: asyncio.Future[bool] = loop.create_future()
        self._writes.append((entered, report))
        stop = self._stop

        def worker() -> str:
            try:
                entered.set()
                if stop.is_set():
                    return _WRITE_NOT_DONE
                return perform()
            finally:
                # Returned or raised: either way this worker writes no more.
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._settle, report, True)

        task = asyncio.ensure_future(run_in_threadpool(worker))
        self._tasks.add(task)
        task.add_done_callback(self._forget)
        return await asyncio.shield(task)

    @staticmethod
    def _settle(report: asyncio.Future[bool], ended: bool) -> None:
        # On the loop, sent there by the worker as it returns.
        if not report.done():
            report.set_result(ended)

    def _forget(self, task: asyncio.Task[str]) -> None:
        self._tasks.discard(task)
        # Retrieved, so a command or a write that failed after its handler stopped
        # waiting is not logged as "Task exception was never retrieved": nobody is
        # left to read its result.
        if not task.cancelled():
            task.exception()

    async def stop_and_wait(self) -> bool:
        """Stop every command and write of this session and wait for word that each
        ended.

        True once every command is known to be stopped and every write to have
        returned. False as soon as a worker reports a command unconfirmed, or once
        a worker that had entered -- a command's or a write's -- has sent no report
        within ``_shell_stop_seconds``, one limit for all of them together: nothing
        then vouches that the workspace is still, and the caller must leave it
        exactly as it is.
        """
        self._stop.set()
        waiting: set[asyncio.Future[bool]] = set()
        commands = [(evidence.entered, report) for evidence, report in self._commands]
        for entered, report in [*commands, *self._writes]:
            if report.done():
                if not report.result():
                    return False
            elif entered.is_set():
                waiting.add(report)
            # Not entered yet: its worker finds the stop set before it could spawn
            # its command or make its write.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _shell_stop_seconds()
        while waiting:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            done, waiting = await asyncio.wait(
                waiting, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if not done or not all(report.result() for report in done):
                return False
        return True


def _build_meta_tools(
    staging: Path, secret_env: dict[str, str] | None = None, *, shells: _BuilderShells
) -> list[LlmTool]:
    """The four meta-tools, every handler a closure over this session's staging.

    All handlers follow the LlmTool no-raise contract: filesystem/subprocess
    failures come back as descriptive error TEXT the model can react to (the
    loop's own except is only the last backstop). Settings-bound limits are
    read at call time, mirroring the runtime tools' handlers.

    ``secret_env`` is the env addition injected into run_shell's environment so
    the builder can live-test the real API -- the install form's single
    ``{<NAME>: <value>}`` (D36), or a revise's whole preserved package ``.env``
    (D40) layered over its ``config.env`` (web-v6 §4) -- passed straight to
    ``_run_shell_subprocess``. It touches ONLY run_shell (the file meta-tools
    never see it) and never any prompt; the ``config.env`` TEXT reaches the
    prompt separately, through ``_revise_user_prompt``.

    ``shells`` is the session's ``_BuilderShells``: every run_shell command and
    every write_file write runs through it, so the session can stop them all and
    wait for them to end. read_file and list_dir only read, and hand their reads
    to the thread pool themselves: nothing the session does once it has stopped
    waits for them.
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
            # Write through tools._write_regular_file (F3c), the WRITE-side mirror of
            # the bounded read helper. A plain ``write_text()`` here does
            # ``open(O_WRONLY)``, and a reader-less FIFO the builder ``mkfifo``'d in
            # staging (run_shell can) BLOCKS that open FOREVER waiting for a reader --
            # wedging this threadpool worker (the outer asyncio timeout only cancels
            # the await, never the wedged worker). The helper's O_NONBLOCK makes that
            # open fail with ENXIO at once, its O_NOFOLLOW refuses a symlinked leaf
            # swapped in after _resolve_in_staging resolved the path (the TOCTOU the
            # resolve alone cannot close, so a generated symlink can never redirect
            # the write out of staging), and it creates parent dirs itself (this
            # tool's auto-create-parents contract). False => a non-regular /
            # symlinked / reader-less-FIFO target: the model-facing failure below.
            if not tools._write_regular_file(target, content):
                return "write_file failed: target is not a regular file"
            return f"wrote {target.relative_to(staging.resolve())} ({len(content)} characters)"

        try:
            return await shells.write(_write)
        except Exception as exc:
            return f"write_file failed: {type(exc).__name__}"

    async def read_file(args: dict[str, Any]) -> str:
        target = _resolve_in_staging(staging, args.get("path"))
        if target is None:
            return f"read_file {_PATH_REJECTED}"

        def _read() -> str:
            cap = get_settings().llm_tool_output_max_chars
            # Cheap pre-gates for the DISTINCT model-facing wording, then the ONE
            # shared bounded helper as the HARD gate (F2). exists()/is_dir() only
            # PICK the right message; ``tools._read_regular_file_capped`` is what
            # actually enforces the boundary -- its O_NONBLOCK+S_ISREG gate refuses
            # a FIFO (the builder can `mkfifo` one via run_shell; read_text() on it
            # would BLOCK FOREVER, wedging this threadpool worker since the outer
            # asyncio timeout only cancels the await), its O_NOFOLLOW refuses a
            # symlinked leaf swapped in after _resolve_in_staging resolved the path
            # (a TOCTOU race the resolve alone cannot close), and its cap+1 read
            # means even a file that GREW past the cap after any check buffers at
            # most cap+1 chars before we refuse it. None => existed and is not a
            # dir yet the helper declined => a non-regular/symlinked leaf.
            if not target.exists():
                return "read_file failed: no such file"
            if target.is_dir():
                return "read_file failed: path is a directory"
            text = tools._read_regular_file_capped(target, cap)
            if text is None:
                return "read_file failed: not a regular file"
            if len(text) > cap:
                return (
                    f"read_file failed: file is too large (exceeds the {cap}-character output cap)"
                )
            # F1/D36: mask any known secret the file CONTENTS carry (e.g. a value the
            # model piped into a staged file via run_shell) BEFORE the size cap, so a
            # secret straddling the cut is fully masked (redact-before-truncate, same
            # rationale as llm_log). read_file is a conversation-facing meta-tool
            # result, so this keeps the value out of the LIVE builder conversation.
            return tools._cap_output(tools.redact_known_secrets(text), cap)

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
            # F1/D36: a staged FILENAME could embed a known secret (e.g. the model ran
            # `... > "$KB_API_KEY.txt"` in run_shell), so mask any such value out of the
            # listing before it enters the LIVE builder conversation, uniformly with the
            # other meta-tools.
            return tools.redact_known_secrets("\n".join(entries))

        try:
            return await run_in_threadpool(_list)
        except Exception as exc:
            return f"list_dir failed: {type(exc).__name__}"

    async def run_shell(args: dict[str, Any]) -> str:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return "run_shell rejected: command must be a non-empty string"
        settings = get_settings()
        return await shells.run(
            staging,
            command,
            settings.tool_install_shell_timeout_seconds,
            settings.llm_tool_output_max_chars,
            secret_env,
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


# --- single-flight client construction --------------------------------------
#
# Building the OpenAPI-fetch client is routed through ONE dedicated daemon
# thread, NOT starlette's run_in_threadpool. This is a hard invariant, not an
# optimization -- the comments here and in _fetch_openapi are the contract.
#
# WHY NOT run_in_threadpool. asyncio.timeout preempts by cancelling the AWAIT,
# never the worker: when the deadline fires mid-construction the pool waiter is
# cancelled and its capacity token released, but the underlying pool thread
# keeps running the blocked constructor to completion. Every retry after a
# construction timeout therefore grabs ANOTHER fresh pool thread -- N
# consecutive construction timeouts leave N live workers (empirically: 45
# attempts spawned 44 threads, past the 40-token limiter). So "a hung
# constructor ties up one bounded worker" was FALSE: threads and memory grew
# without bound, and that non-daemon growth could also impede process exit.
#
# THE BOUND enforced instead (each numbered invariant is load-bearing):
#  (1) AT MOST ONE construction thread exists for the whole process lifetime,
#      started lazily, daemon=True -- a genuinely hung syscall then occupies
#      exactly ONE daemon thread that can NEVER block interpreter exit (Python
#      cannot kill a blocked thread, but a daemon one is abandoned at exit).
#  (2) Callers submit (make_client, Future) onto _SETUP_QUEUE and await the
#      future via asyncio.wrap_future INSIDE the caller's asyncio.timeout, so
#      deadline preemption of SETUP works exactly as before -- wrap_future turns
#      the deadline's task-cancellation into a prompt CancelledError at the
#      await even while the constructor is already RUNNING on the worker.
#  (3) A waiter whose deadline fires cancels its future; the worker gates every
#      job on set_running_or_notify_cancel(), so a future cancelled BEFORE it
#      starts is SKIPPED without ever calling the constructor -- a backlog of
#      abandoned retries constructs nothing.
#  (4) A construction that finishes AFTER its waiter gave up is dropped safely.
#      cancel() on a RUNNING concurrent future returns False and leaves it
#      RUNNING (it does NOT flip to CANCELLED), so the worker's later
#      set_result() lands on a RUNNING->FINISHED transition, never the CANCELLED
#      state that would raise InvalidStateError. wrap_future then finds its
#      asyncio destination already cancelled and discards the result. The
#      orphaned client was never __aenter__'d and httpx2 opens sockets lazily,
#      so dropping it holds nothing open.
#  (5) A truly stuck construction makes SUBSEQUENT fetches QUEUE behind it and
#      time out honestly at their OWN deadline -- zero extra threads. THAT is
#      the bounded promise, now true.

# The single construction thread's name. Distinctive on purpose: it is the ONE
# thread that may carry a hung client constructor, so the repeated-timeout test
# identifies it by this prefix and asserts it never multiplies.
_SETUP_THREAD_NAME = "afterthread-openapi-client-setup"

# Each job is (callable, future-to-resolve-with-its-result). The worker is
# deliberately heterogeneous -- it runs whatever callable it is handed and
# resolves that job's own future -- so the test-only drain PING (a no-op job)
# can ride the SAME FIFO queue to observe the worker has caught up. ``object``
# is the honest element type; the sole production submitter
# (_run_in_setup_worker) narrows its own result back to AsyncClient.
_SetupJob = tuple[Callable[[], object], concurrent.futures.Future[object]]
_SETUP_QUEUE: queue.SimpleQueue[_SetupJob] = queue.SimpleQueue()
_SETUP_WORKER_LOCK = threading.Lock()
_SETUP_WORKER: threading.Thread | None = None


def _setup_worker_loop() -> None:
    """The single construction thread: run queued jobs one at a time, forever.

    Every job is gated on set_running_or_notify_cancel(): a job whose waiter
    already gave up (future CANCELLED while it sat in the queue) returns False
    and is SKIPPED -- the callable never runs (invariant 3). A job that DOES
    start runs to completion even if its waiter later gives up; a RUNNING future
    cannot transition to CANCELLED, so the terminating set_result/set_exception
    can never race into InvalidStateError (invariant 4). Nothing the callable
    raises escapes this loop, so one bad construction can never kill the worker;
    a truly hung one blocks THIS thread (and queues everything behind it), the
    bounded cost invariant (5) promises.
    """
    while True:
        make, fut = _SETUP_QUEUE.get()
        if not fut.set_running_or_notify_cancel():
            continue
        try:
            result = make()
        except Exception as exc:
            fut.set_exception(exc)
        else:
            fut.set_result(result)


def _ensure_setup_worker() -> None:
    """Start the single construction thread on first use; a no-op thereafter.

    Double-checked under _SETUP_WORKER_LOCK so concurrent first-callers create
    exactly ONE thread (invariant 1). The is_alive() re-check also self-heals the
    (practically impossible) case of the loop having died, and never runs two
    workers at once.
    """
    global _SETUP_WORKER
    if _SETUP_WORKER is not None and _SETUP_WORKER.is_alive():
        return
    with _SETUP_WORKER_LOCK:
        if _SETUP_WORKER is not None and _SETUP_WORKER.is_alive():
            return
        worker = threading.Thread(target=_setup_worker_loop, name=_SETUP_THREAD_NAME, daemon=True)
        worker.start()
        _SETUP_WORKER = worker


async def _run_in_setup_worker(make_client: Callable[[], httpx2.AsyncClient]) -> httpx2.AsyncClient:
    """Build the client on the single construction thread, awaited under the
    caller's asyncio.timeout so the deadline preempts SETUP exactly as before.

    The (callable, future) pair is queued and the future awaited via
    asyncio.wrap_future, which forwards the deadline's task-cancellation to the
    concurrent future -- cancelling it if still queued (invariant 3) -- and
    raises CancelledError at THIS await promptly even when the constructor is
    already RUNNING on the worker (invariant 4). Replaces run_in_threadpool for
    THIS one call so a hung constructor occupies at most the one bounded worker.
    """
    _ensure_setup_worker()
    fut: concurrent.futures.Future[object] = concurrent.futures.Future()
    _SETUP_QUEUE.put((make_client, fut))
    return cast(httpx2.AsyncClient, await asyncio.wrap_future(fut))


def _drain_setup_worker_for_tests(timeout: float = 10.0) -> None:
    """Block until the construction worker has processed everything queued so
    far and is idle again -- TEST-ONLY, called from the suite's autouse reset.

    A prior test's still-running (or still-queued) fake construction occupies the
    single worker; left there it would queue behind and skew the NEXT timing
    test. A PING job (a no-op callable) rides the SAME FIFO queue: once its future
    resolves, every job ahead of it -- including a formerly-blocked constructor
    the test has since released -- has drained, so the worker is provably idle.
    The timeout turns a test that forgot to release its block into a loud failure
    here rather than a hang.
    """
    _ensure_setup_worker()
    ping: concurrent.futures.Future[object] = concurrent.futures.Future()
    _SETUP_QUEUE.put((lambda: None, ping))
    ping.result(timeout=timeout)


# --- the OpenAPI fetch -------------------------------------------------------


async def _fetch_openapi(url: str) -> tuple[str | None, str | None]:
    """Fetch the OpenAPI document; returns (text, None) or (None, user_error).

    Bounded on every axis: a TOTAL wall-clock deadline (asyncio.timeout, see
    ``_FETCH_TOTAL_TIMEOUT_SECONDS``) wrapped around the whole fetch, the
    per-phase httpx2 timeout inside it, the redirect count, and the body SIZE --
    the Content-Length header is checked when present, and the streamed body is
    counted regardless (a server can lie about, or omit, the header). Over the
    cap fails OUTRIGHT (see ``_OPENAPI_MAX_BYTES``). Errors carry only the
    exception CATEGORY, never ``str(exc)`` -- an httpx2 error string embeds the
    full URL, and while the URL is the user's own input (not a secret), the
    category is what is diagnostic; the URL is already on the user's screen.

    TLS verification stays ON by default. Only when ``settings.tls_no_verify``
    is true is the client built with ``verify=False`` (an intranet self-signed/
    private-CA deployment, see config.py); the default-off path constructs the
    client with EXACTLY the same arguments as before this flag existed -- no
    ``verify`` kwarg at all -- so ordinary behavior is byte-for-byte unchanged.

    Client CONSTRUCTION -- not merely the request -- sits INSIDE the
    asyncio.timeout scope on purpose, in BOTH branches above, AND runs on the
    dedicated single-flight construction worker (``_run_in_setup_worker``, whose
    contract is stated above) rather than starlette's ``run_in_threadpool``.
    Building an httpx2.AsyncClient can do real synchronous work (TLS context /
    trust-store initialization -- e.g. reading the system trust store or
    SSL_CERT_FILE), and a synchronous call has no AWAIT point for
    asyncio.timeout's scheduled cancellation to land on: the deadline could
    START counting before it but could never PREEMPT it mid-call, and the event
    loop would sit blocked running it -- for every other task too -- meanwhile.
    Awaiting the worker's future fixes both: the call becomes an awaited Future
    the same clock CAN cancel, and the blocking work runs off the event loop.
    ``run_in_threadpool`` would ALSO give those two, but NOT the crucial third
    property -- when the deadline preempts the await it cancels only the WAITER,
    never the blocked pool thread, so each retry after a construction timeout
    grabbed a FRESH pool thread and N timeouts leaked N live workers (the finding
    this fix closes). ``_run_in_setup_worker`` bounds that to ONE daemon thread
    for the whole process: a hung constructor ties up exactly that worker,
    subsequent fetches QUEUE behind it and time out at their own deadline, and no
    thread ever multiplies. That is also why ``_client_cm`` below is a closure
    CALLED as with-item #2's expression rather than a variable assigned before
    the ``async with``: with-item expressions are evaluated left to right, each
    AFTER the previous item's ``__aenter__`` returns, so calling it there defers
    the construction submission until asyncio.timeout (with-item #1) has already
    started its clock. On expiry mid-construction the abandoned client was never
    entered (its ``__aenter__`` never ran), so it holds no sockets (httpx2 opens
    connections lazily) and is dropped safely once the worker returns it (the
    worker's set_result lands on a still-RUNNING future, never a cancelled one,
    so it cannot raise); the daemon worker keeps running the blocked call in the
    background, which -- being daemon -- can never block interpreter exit.
    """

    def _client_cm() -> httpx2.AsyncClient:
        """Build the client for the current settings. Must stay a function
        CALLED (via ``_run_in_setup_worker``) from within the ``async with``
        below, not a variable computed before it -- see the docstring above
        for why."""
        if get_settings().tls_no_verify:
            return httpx2.AsyncClient(
                timeout=_FETCH_TIMEOUT_SECONDS,
                follow_redirects=True,
                max_redirects=_FETCH_MAX_REDIRECTS,
                verify=False,
            )
        return httpx2.AsyncClient(
            timeout=_FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            max_redirects=_FETCH_MAX_REDIRECTS,
        )

    try:
        async with (
            asyncio.timeout(_FETCH_TOTAL_TIMEOUT_SECONDS),
            await _run_in_setup_worker(_client_cm) as client,
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
        # httpx2.HTTPError covers transport/timeout/redirect failures; a
        # malformed URL raises httpx2.InvalidURL (a bare Exception -- NOT an
        # HTTPError and NOT a ValueError); and the asyncio.timeout
        # total-deadline expiry surfaces as TimeoutError. The catch is total so
        # every one of them lands on the friendly-outcome path rather than
        # crashing the background job.
        return None, f"OpenAPI 文件下載失敗（{type(exc).__name__}）。"  # noqa: RUF001
    return b"".join(chunks).decode("utf-8", errors="replace"), None


def _builder_user_prompt(instructions: str, openapi_text: str) -> str:
    """The session's user turn: the operator's instructions + the document.

    The OpenAPI text is bounded by the SAME ``llm_prompt_budget_tokens`` knob
    that bounds item serialization (one operator-facing "how much may ride in a
    prompt" setting, not a second one), converted to a CHAR allowance via the
    live chars<->tokens ratio (afterthread.services.token_budget), behind the
    shared truncation marker. Instructions are already request-bounded (<=20000)
    at the API layer.
    """
    budget = token_budget.char_allowance(get_settings().llm_prompt_budget_tokens)
    return "\n\n".join(
        [
            "Build the tool described below.",
            "User instructions:",
            instructions,
            "OpenAPI document:",
            # F1/D36: redact known secrets BEFORE the budget cut (redact->cap, the same
            # order every other prompt/live-text site uses). An internal API doc can
            # legitimately contain the very key the user just registered, and a secret
            # straddling THIS cut would become ``prefix + truncation marker`` -- an
            # INTERIOR fragment neither redaction pass can catch -- so it must be masked
            # while the document is still WHOLE.
            _truncate_to(tools.redact_known_secrets(openapi_text), budget),
        ]
    )


# The feedback line of a revise that only attaches failing runs (web-v6 §8.1):
# the one-click fix sends no words, and the builder is told what that means.
_NO_FEEDBACK = (
    "(none) The operator attached the failing runs below and asks only that you fix them."
)

# How each outcome reads to the builder. ``output_overflow`` is spelled out
# because the assistant was told "exit -9", which reads as a crash.
_RUN_OUTCOME_PROSE = {
    "ok": "the tool succeeded",
    "failed": "the tool exited with a non-zero code",
    "output_overflow": (
        "the tool was KILLED because its stderr passed the output cap (the "
        "assistant was told exit -9; it did not crash on its own)"
    ),
    "timeout": "the tool ran past its time limit and was killed",
    "start_failed": "the tool could not be started (interpreter or entry file)",
    "refused": "the call was refused before the tool ran",
    "error": (
        "the BACKEND failed around this call (an internal error, not the tool's "
        "own exit), whether or not the tool had started"
    ),
}

_FAILING_RUNS_HEADER = (
    "Failing runs attached by the operator. They are real calls the AI assistant "
    "made; the texts are what the backend recorded (secrets masked, long outputs "
    "cut: stderr keeps its END, stdout its START)."
)

# Follows the header, so it is read before the runs themselves: what their masked
# and cut texts mean for reproducing them. A record keeps a masked value as the
# marker, and the model would otherwise pipe that marker into the tool and debug a
# failure it caused itself. Every cut marker on this path (the record's own and
# this prompt's) contains 已截斷.
_FAILING_RUNS_CAVEAT = (
    f"In these texts {tools._REDACTION_MARKER} stands for a hidden value: it is NOT "
    "what the tool received, so never pass it to the tool or the API as if it were. "
    "Text that was cut carries a marker containing 已截斷. When a run's stdin "
    "arguments are incomplete -- a value masked, or the text cut -- say plainly that "
    "the run cannot be reproduced exactly, and do not guess the missing part."
)

# Marks the front of a text cut to its END (``_truncate_tail_to``); the head
# cut's ``_TRUNCATION_MARKER`` turned around.
_TAIL_TRUNCATION_MARKER = "[內容過長已截斷]…"


def _truncate_tail_to(value: str, cap: int) -> str:
    """``value`` cut to its LAST ``cap`` chars, marked in front when it was cut.

    stderr's counterpart of ``_truncate_to``: a traceback names its failure at
    the end, the stored copy already keeps that end (``tool_runs._tail``), and
    the header above tells the builder so -- a head cut here would drop exactly
    the lines that matter whenever the prompt budget is smaller than the record.
    """
    if len(value) <= cap:
        return value
    marker_len = len(_TAIL_TRUNCATION_MARKER)
    if cap <= marker_len:
        return value[len(value) - cap :]
    return _TAIL_TRUNCATION_MARKER + value[len(value) - cap + marker_len :]


# The fixed procedure that follows the attached runs (web-v6 §8.4).
_DEBUG_PROCEDURE = """How to work on these failures:
1. Reproduce first. Pipe the exact attached arguments into the entry command \
(for example: echo '<the stdin arguments>' | python3 run.py) and confirm you see \
the same error. If you cannot reproduce it, say so and find out why (the API \
changed, credentials, data-dependent behaviour) instead of changing code on a \
guess.
2. Fix the root cause.
3. Re-run every attached input and every example in examples.json. Finish with \
ready=true only if all of them succeed (exit code 0, no timeout).
4. If the cause is outside the code (expired credentials, the API is down), \
finish with ready=false and explain the cause and what the operator should do.
5. For each failure you fixed, add an example to examples.json: an equivalent \
input that takes the same code path WITHOUT any personal content the original \
may carry (for example another query that also returns no hits). Read-only \
inputs only.
6. In your summary: the root cause, what you changed, how you verified it, and \
which examples you added."""


# What an attached run says about the version it ran, for a round
# that continues a draft: the workspace is not the base version any more.
_RAN_THE_BASE = (
    "It ran the version this draft started from; your workspace may have changed "
    "since, so check its current state first."
)
# A run whose record was purged: its summary is all a continuing round has.
_RUN_PURGED = (
    "Its record was purged: only this summary is left, so it cannot be reproduced "
    "from here. Do not try to re-run it."
)
# A run whose texts the prompt's total cap left out.
_RUN_TEXTS_LEFT_OUT = (
    "Its recorded texts were left out to keep this prompt within its size limit: "
    "do not try to re-run it from here."
)


@dataclass(frozen=True, slots=True)
class _RunTexts:
    """One attached run as a prompt shows it: its summary lines, and each recorded
    text masked and then cut to the cap it is stored under, as (label, text,
    whether its end is the part kept)."""

    summary: str
    texts: tuple[tuple[str, str, bool], ...]


def _run_texts(
    run: AttachedRun, workspace_vid: str | None, *, base_vid: str | None = None
) -> _RunTexts:
    """``run`` masked once, for ``_run_text_shown`` to cut as often as it needs.

    A run of another version is called DIFFERENT, not older: an attached record
    (the API takes any of the tool's runs) may have run a version since
    discarded, newer than the workspace's, or one of an earlier package that had
    the same name. ``base_vid`` is the base of the draft a continuing round
    works on: a run of it ran the version the draft started from, which the
    workspace may since have changed.
    """
    if base_vid is not None and run.vid == base_vid:
        version = _RAN_THE_BASE
    elif base_vid is None and run.vid == workspace_vid:
        version = "It ran the version that is in your workspace."
    else:
        started_from = "this draft started from" if base_vid is not None else "in your workspace"
        version = (
            f"It ran a DIFFERENT version ({run.vid}) than the one {started_from}: "
            "check whether it still fails before changing anything."
        )
    lines = [
        f"Run #{run.id} at {run.started_at:%Y-%m-%d %H:%M:%S} UTC, "
        f"called from the {run.workflow} workflow.",
        version,
        "Outcome: "
        + _RUN_OUTCOME_PROSE.get(run.outcome, run.outcome)
        + (f" (exit code {run.exit_code})." if run.exit_code is not None else "."),
    ]
    if run.purged:
        lines.append(_RUN_PURGED)
        return _RunTexts("\n".join(lines), ())
    texts: list[tuple[str, str, bool]] = []
    for label, text, cap, keep_end in (
        ("What the AI assistant was told", run.message, tool_runs.MESSAGE_MAX_CHARS, False),
        ("stdin arguments (JSON)", run.args, tool_runs.ARGS_MAX_CHARS, False),
        ("stderr", run.stderr, tool_runs.STDERR_TAIL_MAX_CHARS, True),
        ("stdout", run.stdout, tool_runs.STDOUT_HEAD_MAX_CHARS, False),
    ):
        if text:
            masked = tools.redact_known_secrets(text)
            cut = _truncate_tail_to(masked, cap) if keep_end else _truncate_to(masked, cap)
            texts.append((label, cut, keep_end))
    return _RunTexts("\n".join(lines), tuple(texts))


def _run_text_shown(run: _RunTexts, limit: int | None) -> str:
    """One run as the prompt carries it: each text cut to ``limit`` more -- its end
    kept for stderr -- whole when ``limit`` is None, left out when it is 0."""
    if limit == 0 and run.texts:
        return f"{run.summary}\n{_RUN_TEXTS_LEFT_OUT}"
    lines = [run.summary]
    for label, text, keep_end in run.texts:
        if limit is not None:
            text = _truncate_tail_to(text, limit) if keep_end else _truncate_to(text, limit)
        lines += [f"{label}:", text]
    return "\n".join(lines)


# --- the revise prompt's size --------------------------------------------------------
#
# Each part of the user turn has a cap of its own, the cap its text has where it is
# kept, so a text is cut here only when it grew by a mask. The turn as a whole is
# then held to the smaller of the prompt budget and what the conversation budget's
# half leaves beside the system prompt (``_fit_prompt``), shrinking its parts in a
# fixed order, the lowest rank first. Headings, fixed instructions and a run's
# summary lines carry no rank: only the last resort, a cut of the whole turn, ever
# shortens them.

_PROMPT_ENDING_CAP = 2000
_PROMPT_EARLIER_ROUNDS = 4
_PROMPT_LINE_CAP = 300
_PROMPT_LIST_ENTRIES = 50

_RANK_LISTS = 1  # earlier rounds, the paths not brought in, the paths changed
_RANK_RUN_TEXTS = 2
_RANK_CONFIG = 3  # config.env, and the .env key names
_RANK_TOOL_JSON = 4
_RANK_ORIGINAL = 5  # the original request
_RANK_PREVIOUS = 6  # the previous round's notes
_RANK_NOTE = 7  # this round's note


@dataclass(frozen=True, slots=True)
class _Shrinkable:
    """A part of a prompt the total cap may shrink.

    ``render(None)`` is the part whole; ``render(n)`` holds its variable text to
    ``n`` chars, a text cut down to nothing still saying it was cut. ``size`` is
    the largest ``n`` that changes anything.
    """

    rank: int
    render: Callable[[int | None], str]
    size: int


def _cut(text: str, limit: int) -> str:
    """``text`` cut to its first ``limit`` chars, marked; at the least the marker."""
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER
    return text[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def _text_part(rank: int, header: str, body: str) -> _Shrinkable:
    """A heading and a body under it; the body is what shrinks."""

    def render(limit: int | None) -> str:
        return f"{header}\n\n{body if limit is None else _cut(body, limit)}"

    return _Shrinkable(rank, render, len(body))


def _line_part(rank: int, lead: str, body: str) -> _Shrinkable:
    """One line: fixed words, then a body that shrinks."""

    def render(limit: int | None) -> str:
        return lead + (body if limit is None else _cut(body, limit))

    return _Shrinkable(rank, render, len(body))


def _runs_part(runs: tuple[_RunTexts, ...]) -> _Shrinkable:
    """The attached runs: each run's texts cut to the same limit."""

    def render(limit: int | None) -> str:
        return "\n\n".join(_run_text_shown(run, limit) for run in runs)

    longest = max((len(text) for run in runs for _label, text, _end in run.texts), default=0)
    return _Shrinkable(_RANK_RUN_TEXTS, render, longest)


def _fit_prompt(parts: list[str | _Shrinkable], cap: int) -> str:
    """``parts`` joined as one prompt of at most ``cap`` chars.

    Whole when that fits. Otherwise the parts of the lowest rank shrink first:
    each rank is shrunk only as far as it must -- the longest common limit that
    fits -- and only once every lower rank is already as short as it goes. When
    even every rank at its shortest does not fit, the whole is cut: the last
    resort, the only one that reaches the fixed text.
    """
    limits: dict[int, int] = {}

    def build() -> str:
        texts = (
            part if isinstance(part, str) else part.render(limits.get(part.rank)) for part in parts
        )
        return "\n\n".join(text for text in texts if text)

    prompt = build()
    if len(prompt) <= cap:
        return prompt
    ranked = [part for part in parts if isinstance(part, _Shrinkable)]
    for rank in sorted({part.rank for part in ranked}):
        limits[rank] = 0
        if len(build()) > cap:
            continue
        low, high = 0, max(part.size for part in ranked if part.rank == rank)
        while low < high:
            middle = (low + high + 1) // 2
            limits[rank] = middle
            if len(build()) <= cap:
                low = middle
            else:
                high = middle - 1
        limits[rank] = low
        return build()
    return _truncate_to(build(), max(cap, 0))


def _revise_prompt_cap(system_chars: int) -> int:
    """How long a revise's user turn may be: within the prompt budget, and, with the
    system prompt, within half the conversation budget, the other half left for
    the work."""
    settings = get_settings()
    return min(
        token_budget.char_allowance(settings.llm_prompt_budget_tokens),
        token_budget.char_allowance(settings.llm_tool_conversation_budget_tokens) // 2
        - system_chars,
    )


@dataclass(frozen=True, slots=True)
class _History:
    """What a continuing round's prompt says of the draft it continues.

    ``original`` is the draft's own ``feedback``; ``attempts`` the rounds its
    manifest keeps, oldest first, as ``begin_round`` wrote it; ``not_brought_in``
    the draft's paths the workspace did not receive, in full; ``changed`` the
    paths that differ from ``base_vid``'s version, None when that could not be
    told; ``workspace_config`` whether the workspace holds a ``config.env``.
    """

    original: str | None
    attempts: tuple[dict[str, Any], ...]
    not_brought_in: tuple[tool_drafts.Omitted, ...]
    changed: tuple[str, ...] | None
    base_vid: str
    workspace_config: bool


# Why a path of the draft is not in the workspace, for the model.
_NOT_BROUGHT_IN_PROSE = {
    "secret_name": "its name holds a known secret",
    "secret_content": "its content holds a known secret",
    "secret_link_target": "its link target holds a known secret",
    "link_outside": "a link that leads outside the draft's files",
    "link_ambiguous": "a link whose target goes up through another link",
    "unreadable": "it could not be read, or whether it is a .env could not be told",
    "not_regular": "not a regular file, directory or link",
    "env_file": "a .env file: its values are never shown to you",
    "backend_meta": "the backend's own metadata directory",
}

_WORKSPACE_CONFIG_NOTE = (
    "Your workspace has a config.env file: it is NOT loaded when the tool runs. "
    "The package's own config.env, outside your workspace, is."
)


def _masked_cut(text: str, cap: int) -> str:
    """Operator- or file-derived text for a prompt: masked whole, then cut."""
    return _truncate_to(tools.redact_known_secrets(text), cap)


def _listed(entries: list[str]) -> str:
    """Up to ``_PROMPT_LIST_ENTRIES`` lines, each cut to ``_PROMPT_LINE_CAP``, and how
    many more there are."""
    shown = [_cut(entry, _PROMPT_LINE_CAP) for entry in entries[:_PROMPT_LIST_ENTRIES]]
    rest = len(entries) - len(shown)
    if rest:
        shown.append(f"... and {rest} more.")
    return "\n".join(shown)


def _ending_text(attempt: dict[str, Any]) -> str:
    """How a recorded round ended: what the backend measured, then its error."""
    outcome = attempt["outcome"] or "not recorded (saved just before a publish)"
    facts = [f"outcome: {outcome}"]
    if attempt["stopped_by"] is not None:
        facts.append(f"the tool phase was ended by the {attempt['stopped_by']} budget")
    if attempt["failure"] is not None:
        facts.append(f"the session failed: {attempt['failure']}")
    if attempt["rounds_used"] is not None and attempt["max_rounds"] is not None:
        facts.append(f"{attempt['rounds_used']} of {attempt['max_rounds']} tool rounds used")
    facts.append("its files were saved" if attempt["saved"] else "its files were not saved")
    text = "; ".join(facts) + "."
    if attempt["error"]:
        text += "\nError: " + tools.redact_known_secrets(attempt["error"])
    return _truncate_to(text, _PROMPT_ENDING_CAP)


def _previous_round_text(attempt: dict[str, Any]) -> str:
    """The previous round: how it ended, and its notes and the operator's, each at
    the cap it is kept under."""
    if attempt["outcome"] == "interrupted":
        lines = [
            "It left no record of how it ended (the backend may have stopped while it "
            "ran), and its progress was not saved into the draft."
        ]
    else:
        lines = ["How it ended, as the backend measured it: " + _ending_text(attempt)]
    for label, key, cap in (
        ("Its summary", "summary", tool_drafts.SUMMARY_CAP),
        ("Its handoff", "handoff", tool_drafts.HANDOFF_CAP),
        ("What it asked the operator to do", "operator_action", tool_drafts.OPERATOR_ACTION_CAP),
        ("The operator's note for that attempt", "note", tool_drafts.NOTE_CAP),
    ):
        if attempt[key]:
            lines.append(f"{label}: {_masked_cut(attempt[key], cap)}")
    return "\n".join(lines)


def _earlier_round_line(attempt: dict[str, Any]) -> str:
    """One earlier round in a line."""
    facts = [f"#{attempt['number']}: {attempt['outcome'] or 'outcome not recorded'}"]
    if attempt["stopped_by"] is not None:
        facts.append(f"ended by the {attempt['stopped_by']} budget")
    if attempt["failure"] is not None:
        facts.append(f"failed: {attempt['failure']}")
    facts.append("files saved" if attempt["saved"] else "files not saved")
    line = "; ".join(facts)
    if attempt["summary"]:
        line += "; summary: " + tools.redact_known_secrets(attempt["summary"])
    return _cut(line, _PROMPT_LINE_CAP)


def _history_parts(history: _History) -> list[str | _Shrinkable]:
    """A continuing round's own parts: the previous round, the earlier ones, the
    draft's paths not brought in, and the paths changed since the base."""
    parts: list[str | _Shrinkable] = []
    if history.attempts:
        latest = history.attempts[-1]
        parts.append(
            _text_part(
                _RANK_PREVIOUS,
                f"The previous attempt (#{latest['number']}):",
                _previous_round_text(latest),
            )
        )
        earlier = history.attempts[-1 - _PROMPT_EARLIER_ROUNDS : -1]
        if earlier:
            parts.append(
                _text_part(
                    _RANK_LISTS,
                    "Earlier attempts, one line each:",
                    "\n".join(_earlier_round_line(attempt) for attempt in earlier),
                )
            )
    if history.not_brought_in:
        entries = [
            f"- {tools.redact_known_secrets(item.path)}: "
            + _NOT_BROUGHT_IN_PROSE.get(item.reason, item.reason)
            for item in history.not_brought_in
        ]
        parts.append(
            _text_part(
                _RANK_LISTS,
                "Paths of the draft that are NOT in your workspace, and why:",
                _listed(entries),
            )
        )
    if history.workspace_config:
        parts.append(_WORKSPACE_CONFIG_NOTE)
    changed_header = (
        f"Paths that differ between your workspace and the installed version "
        f"{history.base_vid} this draft started from:"
    )
    if history.changed is None:
        parts += [changed_header, "They cannot be listed: one side could not be read whole."]
    elif not history.changed:
        parts += [changed_header, "None."]
    else:
        changed = [f"- {tools.redact_known_secrets(path)}" for path in history.changed]
        parts.append(_text_part(_RANK_LISTS, changed_header, _listed(changed)))
    return parts


def _revise_user_prompt(
    feedback: str | None,
    manifest_text: str | None,
    config_text: str = "",
    env_keys: tuple[str, ...] = (),
    conflicts: tuple[str, ...] = (),
    *,
    runs: tuple[AttachedRun, ...] = (),
    workspace_vid: str | None = None,
    system_chars: int = 0,
    history: _History | None = None,
) -> str:
    """The revise session's user turn, held to its cap (``_revise_prompt_cap``).

    Deliberately NOT a re-fetch of the OpenAPI document: the revision is
    driven by the user's words and by the code already in the workspace, the
    original URL is kept only as provenance (and is reduced to a host on
    capture), and re-fetching would spend a network round trip on a document the
    model can no longer be told is current.

    The parts, in a fixed order: the request; for a round that
    continues a draft (``history``), this round's note, the previous round and
    the earlier ones, the paths of the draft not brought in and the paths
    changed since its base; the workspace's ``tool.json``; the package's
    ``config.env`` and ``.env`` key names; the attached runs and how to work on
    them. For a first round ``feedback`` is the request; for a continuation it is
    this round's note, and the request is the draft's own.

    ``tool.json`` is quoted even though the model can `read_file` it, because it
    is the ONE file whose shape the feedback is most often about (the
    description and parameters an assistant sees) -- having it in the opening
    turn saves a round and keeps the model from revising against a guess.

    Every operator- or file-derived text is masked BEFORE it is cut, the same
    redact-then-cap order every prompt site here uses: a secret straddling the
    cut would become an unmatchable interior fragment. The feedback is redacted
    where the install's instructions are not -- it is written AFTER the tool has
    a live ``.env``, so a user quoting their own key is a value we now genuinely
    know. The backend's own words go in as they are: a registered value that
    happens to equal one of them would mangle it (see ``_revise_system_prompt``).
    A field a draft's manifest writes as free text is free text each time it is
    read back -- masked when written, with the secrets known then -- unless the
    reader checks its shape: the draft's request and its rounds' notes, errors
    and summaries are masked here; a kept run summary whose workflow, vid and
    outcome were checked to be of the backend's shapes (``tool_runs._summary_only``)
    goes in as it is, like a record read from the database.

    Two package-layer facts follow when present, because the model
    cannot see either file from its workspace. ``config.env`` is quoted WHOLE --
    its comments are part of what the operator meant, and its values are settings
    by the operator's own choice of file -- yet still through the redactor like
    every operator-derived text here, so a secret pasted into the wrong file is
    masked as long as ``.env`` holds it too. Without it the model changes a code
    default that an override silently shadows and reports success. The
    ``.env`` part is KEY NAMES only, which tells the model what credentials it
    has without telling it any of them; ``conflicts`` are the keys both files
    set, where the ``.env`` value is the one that runs.

    ``runs`` are the failing run records attached: each one's time,
    workflow, which version it ran, outcome, the string the assistant got, and
    the masked stdin arguments and output, followed by the fixed debugging
    procedure. Their texts were masked when the job was submitted and go through
    the redactor again here, then are cut to the caps they are stored under. A
    record purged since an earlier round attached it is its summary alone, said
    to be one that cannot be re-run. A revise with runs and no request says so
    in the request's slot, in the backend's own words.
    """
    parts: list[str | _Shrinkable] = ["Revise the existing tool package as described below."]
    request = feedback if history is None else history.original
    request_header = "User feedback:" if history is None else "The operator's original request:"
    if request is not None:
        parts.append(
            _text_part(
                _RANK_ORIGINAL, request_header, _masked_cut(request, tool_drafts.FEEDBACK_CAP)
            )
        )
    else:
        parts += [request_header, _NO_FEEDBACK]
    if history is not None:
        if feedback is not None:
            parts.append(
                _text_part(
                    _RANK_NOTE,
                    "The operator's note for this attempt:",
                    _masked_cut(feedback, tool_drafts.NOTE_CAP),
                )
            )
        parts += _history_parts(history)
    if manifest_text is not None:
        parts.append(
            _text_part(
                _RANK_TOOL_JSON,
                "The workspace's current tool.json:"
                if history is not None
                else "The package's current tool.json:",
                tools.redact_known_secrets(manifest_text),
            )
        )
    if config_text.strip():
        parts.append(
            _text_part(
                _RANK_CONFIG,
                "The operator's setting overrides for this package (its config.env, "
                "outside your workspace). These values override the defaults in the "
                "tool's code at runtime and are already in your run_shell environment. "
                "If the feedback asks to change one of these settings, changing the code "
                "default has NO effect while the override stays: say in your summary "
                "that the operator must edit config.env.",
                tools.redact_known_secrets(config_text),
            )
        )
        if conflicts:
            parts.append(
                _line_part(
                    _RANK_CONFIG,
                    "These keys are also set in the package's .env, whose value wins: ",
                    tools.redact_known_secrets(", ".join(conflicts)),
                )
            )
    if env_keys:
        parts.append(
            _line_part(
                _RANK_CONFIG,
                "Variables the package's .env sets in your run_shell environment "
                "(secrets; values hidden; never print them): ",
                tools.redact_known_secrets(", ".join(env_keys)),
            )
        )
    if runs:
        base_vid = history.base_vid if history is not None else None
        parts += [_FAILING_RUNS_HEADER, _FAILING_RUNS_CAVEAT]
        parts.append(
            _runs_part(tuple(_run_texts(run, workspace_vid, base_vid=base_vid) for run in runs))
        )
        parts.append(_DEBUG_PROCEDURE)
    return _fit_prompt(parts, _revise_prompt_cap(system_chars))


# The longest ``origin.feedback`` a revise writes, so ``origin.json`` stays far
# below ``tools._AI_META_MAX_BYTES`` and is never refused after the version moved.
_ORIGIN_FEEDBACK_CAP = 20_000


def _provenance_feedback(
    feedback: str | None,
    runs: tuple[AttachedRun, ...],
    draft_line: str | None = None,
    notes: tuple[tuple[int, str], ...] = (),
) -> str:
    """The ``origin.feedback`` a revise writes: the request, the rounds' notes, the runs,
    the draft.

    A version records WHY it exists: the operator's request -- for
    a round that continues a draft, the draft's own -- and each round's note
    (``notes``: round number, note) the draft still keeps; for a fix, which
    failures it was made for -- id, time, outcome and exit code, never the stderr
    or the arguments: the origin is kept as long as the version is,
    and the record's bodies are the operator's to purge. ``draft_line`` names the
    draft the round saved before publishing (``tool_drafts.provenance_line``):
    the tools list finds that draft's id here to tell a publish that went through
    from one that did not -- unless ``write_origin_meta`` writes the whole field
    as the redaction marker (``tools._redacted``), when the list cannot tell and
    says ``unknown``.

    Each part is masked, then the whole is cut to ``_ORIGIN_FEEDBACK_CAP`` with the
    draft line kept whole at its end, since it is what the list looks for. Blocking:
    the mask scans the tools directory.
    """
    parts = []
    if feedback:
        parts.append(tools.redact_known_secrets(feedback))
    for number, note in notes:
        parts.append(f"第 {number} 輪的說明：{tools.redact_known_secrets(note)}")  # noqa: RUF001
    if runs:
        entries = []
        for run in runs:
            detail = f"{run.started_at:%Y-%m-%d %H:%M:%S} UTC，{run.outcome}"  # noqa: RUF001
            if run.exit_code is not None:
                detail += f"，exit {run.exit_code}"  # noqa: RUF001
            entries.append(f"#{run.id}（{detail}）")  # noqa: RUF001
        parts.append(tools.redact_known_secrets("附上的失敗紀錄：" + "、".join(entries)))  # noqa: RUF001
    head = "\n\n".join(parts)
    if draft_line is None:
        return head[:_ORIGIN_FEEDBACK_CAP]
    tail = tools.redact_known_secrets(draft_line)
    if head:
        tail = "\n\n" + tail
    return head[: max(_ORIGIN_FEEDBACK_CAP - len(tail), 0)] + tail


# --- staging promotion + cleanup ---------------------------------------------


def _env_line_key(line: str) -> str | None:
    """The KEY a ``.env`` line assigns, or None if it assigns nothing.

    Tolerates the shapes python-dotenv itself accepts around a key: optional leading
    whitespace, an optional ``export `` prefix, the key, optional whitespace, then the
    ``=``. So ``NAME=v``, ``NAME =v``, ``  NAME=v``, and ``export NAME=v`` all yield
    ``NAME``; a comment or blank line (no ``=``) yields None and is never mistaken for
    the secret's key. Used by ``_inject_secret_into_env`` (F7) to drop EVERY prior line
    assigning our NAME, since python-dotenv is LAST-occurrence-wins.
    """
    if "=" not in line:
        return None
    key = line.split("=", 1)[0].strip()
    # An ``export `` prefix (dotenv accepts ``export FOO=bar``); require whitespace
    # after the keyword so a literal key named ``exportFOO`` is not misparsed.
    if key.startswith("export") and key[6:7].isspace():
        key = key[6:].strip()
    return key or None


# Characters that make an UNQUOTED dotenv value unsafe (interpolation, inline comments,
# quotes, escapes, whitespace-trimming) -- any of them, or leading/trailing whitespace,
# forces quoting in _dotenv_serialize_value. The round-trip check in
# _inject_secret_into_env makes this set non-load-bearing: it is a conservative first cut,
# and any value it mis-serializes is caught and refused rather than written wrong.
_DOTENV_VALUE_NEEDS_QUOTING = frozenset(" \t#'\"\\=$`!")


def _dotenv_serialize_value(value: str) -> str | None:
    r"""Serialize ``value`` into a dotenv RHS that parses back to it verbatim (F5); None
    signals the value is unrepresentable here without a LEAKY escape (refuse instead).

    Values are single-line by contract (the schema rejects newlines and strips
    surrounding whitespace). A value with none of the unsafe characters and no
    leading/trailing whitespace is written UNQUOTED -- byte-identical to the pre-F5
    behavior for the common alnum/``-_.`` key. Otherwise it is SINGLE-quoted when it holds
    no single quote (python-dotenv parses single-quoted values literally -- no
    interpolation, no escapes), so the raw line embeds the value VERBATIM.

    The remaining case -- the value HOLDS a single quote -- would need DOUBLE quotes, and
    there the escaping is the hazard: the instant an escape actually FIRES (the value also
    contains ``"`` or ``\``), the raw .env spelling (e.g. ``"ab'cd\"ef"`` for ``ab'cd"ef``)
    no longer contains the ORIGINAL value as an exact substring, so a runtime tool that
    cats the .env would emit a fully reversible spelling the redactors -- which only ever
    see the original value -- can NEVER match. So we REFUSE (None) a value that carries
    both a single quote AND a ``"``/``\``. Only when NEITHER escape would fire do we
    double-quote, which then embeds the value VERBATIM too (redactable). Every path that
    returns a string keeps the original value as an exact substring of the raw line; the
    round-trip check in ``_inject_secret_into_env`` remains the backstop for anything else.
    """
    if value and value == value.strip() and not (set(value) & _DOTENV_VALUE_NEEDS_QUOTING):
        return value
    if "'" not in value:
        return f"'{value}'"
    if '"' in value or "\\" in value:
        return None
    return f'"{value}"'


def _inject_secret_into_env(env_file: Path, name: str, value: str) -> str | None:
    """Write ``name=value`` into the staged package's ``.env``; None = ok (D36).

    Blocking (runs inside ``_promote_staging`` via ``run_in_threadpool``). Creates
    the file when the LLM wrote none; if the LLM DISOBEYED and already wrote one or
    more ``name=`` lines, EVERY such line is dropped and the single real line is
    appended, so the backend's value is unambiguously the one that wins. Read and
    write both funnel through the bounded, FIFO/symlink-hardened ``tools`` helpers
    (``.env`` files are small, so the manifest caps do not apply, but the jail
    hardening still should).

    Dropping EVERY match, not just the first, is load-bearing (F7): python-dotenv is
    LAST-occurrence-wins, so replacing only the first ``NAME=`` line would let a SECOND
    (model-written) ``NAME=`` line further down OVERRIDE the backend's real value while
    the install still succeeded. ``_env_line_key`` tolerates the ``NAME=`` / ``NAME =``
    / ``export NAME=`` shapes dotenv accepts so none of them can slip past the drop.

    ``validate_package`` already vetted any pre-existing ``.env`` at <=64KiB, but
    it never saw THIS appended line, so we re-check the POST-append encoded size
    against the same ``_ENV_FILE_MAX_BYTES`` cap ourselves and refuse the install
    (friendly error) rather than leave an oversized ``.env`` the runtime would
    silently drop to ``{}``.

    The value is serialized safely (``_dotenv_serialize_value``) and then the FINAL
    content is round-trip-checked (F5): we parse it back with the SAME interpolate=False
    loader the runtime uses (``tools._parse_dotenv_text``) and refuse unless ``name``
    parses back to the exact submitted value. Writing ``NAME=value`` raw let python-dotenv
    transform values containing quotes/``#``/escapes/leading whitespace, so the redaction
    registry -- which registers the PARSED value -- would diverge from the submitted one,
    and a runtime tool echoing the raw line could expose the original. The round-trip
    check is the load-bearing guarantee: it makes the quoting rules non-load-bearing (any
    future dotenv parsing quirk becomes a CLEAN refusal, never a silent divergence) and
    guarantees the runtime registry lands on the SAME value.
    """
    existing = ""
    if env_file.exists():
        text = tools._read_regular_file_capped(env_file, tools._ENV_FILE_MAX_BYTES)
        if text is None:
            # Non-regular / symlinked / unreadable .env: refuse rather than risk
            # writing THROUGH it. validate_package would already fail such a
            # package, so this is a defensive backstop, not the primary gate.
            return _ERROR_SECRET_ENV_WRITE
        existing = text
    # Serialize the value FIRST: None means it cannot be written without a leaky escape
    # (a single quote co-occurring with a ``"``/``\``), so refuse up front rather than
    # emit a reversible-but-unredactable spelling (see _dotenv_serialize_value).
    serialized = _dotenv_serialize_value(value)
    if serialized is None:
        return _ERROR_SECRET_ENV_UNSERIALIZABLE
    # Drop EVERY line assigning our NAME (any of the tolerated shapes), keep the rest
    # verbatim, then append the single real (safely-serialized) line LAST so it is the
    # effective value.
    kept = [line for line in existing.splitlines() if _env_line_key(line) != name]
    kept.append(f"{name}={serialized}")
    new_content = "\n".join(kept) + "\n"
    if len(new_content.encode("utf-8")) > tools._ENV_FILE_MAX_BYTES:
        return _ERROR_SECRET_ENV_TOO_LARGE
    # Round-trip guard (F5): refuse unless the runtime's own loader parses NAME back to the
    # exact submitted value from the FINAL bytes. This is what makes the serialization
    # correct-or-refused rather than correct-or-silently-divergent.
    if tools._parse_dotenv_text(new_content).get(name) != value:
        return _ERROR_SECRET_ENV_UNSERIALIZABLE
    if not tools._write_regular_file(env_file, new_content):
        return _ERROR_SECRET_ENV_WRITE
    return None


def _remove_reserved_sidecar_path(path: Path) -> None:
    """Delete one backend metadata entry without following a symlink.

    ``unlink`` is the whole answer for the cases that matter (a regular file, a
    symlink of any target) and it removes the LINK rather than following it, so a
    ``.afterthread.meta -> /etc`` planted in staging costs its target nothing.
    ``missing_ok`` covers the entry vanishing between the walk and here.

    A real DIRECTORY needs ``rmtree`` (``unlink`` answers EISDIR). The build-root
    metadata directory is always backend-owned because promote creates its
    ``origin.json`` commit marker there; nothing builder-authored may occupy it.

    Raises ``OSError`` on a refusal, which is what makes the caller fail-closed.
    """
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    path.unlink(missing_ok=True)


def _strip_builder_env(
    build_root: tools.BuildRoot, base: Path
) -> tuple[tuple[str, ...], str | None]:
    """Extract root ``.env`` key names, then remove the builder-owned entry.

    Values never leave this function.  The exact lowercase path is deliberate:
    on a case-sensitive filesystem a copied ``.ENV`` is ordinary version
    content, while on a case-insensitive filesystem opening ``.env`` naturally
    addresses the same entry.  The staging-root guard runs first because removal
    is destructive and the builder has unjailed shell access. The name is
    ``tool_drafts.STRIPPED_ROOT_NAMES``' ``env_file``, the list a continuation's
    copy of a draft withholds.
    """

    root_error = _verify_staging_root(build_root.path, base)
    if root_error is not None:
        return (), root_error
    env_file = build_root.path / tool_drafts.STRIPPED_ROOT_NAMES["env_file"]
    try:
        info = os.lstat(env_file)
    except FileNotFoundError:
        return (), None
    except OSError:
        return (), _ERROR_BUILDER_ENV_STRIP

    keys: set[str] = set()
    if stat.S_ISREG(info.st_mode):
        data = tools._read_regular_bytes_capped(env_file, tools._ENV_FILE_MAX_BYTES)
        if data is not None:
            text = data[: tools._ENV_FILE_MAX_BYTES].decode("utf-8", errors="replace")
            keys.update(
                key for line in text.splitlines() if (key := _env_line_key(line)) is not None
            )
    try:
        _remove_reserved_sidecar_path(env_file)
    except OSError:
        return tuple(sorted(keys)), _ERROR_BUILDER_ENV_STRIP
    return tuple(sorted(keys)), None


def _reraise_walk_error(exc: OSError) -> None:
    """Make an incomplete staging traversal fail closed instead of disappearing.

    ``os.walk`` otherwise swallows a subtree ``scandir`` failure. The v5 scope
    fix narrows WHAT is backend-owned to the root metadata directory; it does not
    make an unreadable builder tree safe to publish without having inspected it.
    """
    raise exc


def _strip_builder_sidecars(staging: Path) -> str | None:
    """Delete builder-written metadata at a VERSION root; None means success.

    The v5 ownership boundary is the scope rule: ``BuildRoot`` becomes
    ``versions/<vid>``, where only the root ``.afterthread.meta/`` belongs to the
    backend. Legacy flat names such as ``.ai_meta.json`` and
    ``.afterthread-state.json`` belong to the tool at this scope, at every depth;
    migration can deliberately place the latter here byte-for-byte.

    The exact metadata directory is still removed before validation and recreated
    by the publisher with ``origin.json`` as the commit marker. A symlink or odd
    directory shape is removed without following it. The preliminary walk deletes
    nothing; it preserves the existing fail-closed rule that an unreadable subtree
    cannot be silently omitted from inspection.

    The name is ``tool_drafts.STRIPPED_ROOT_NAMES``' ``backend_meta``, the list a
    continuation's copy of a draft withholds: on a filesystem that does not tell
    case apart, the path here reaches a name spelled in another case too.
    """
    metadata = staging / tool_drafts.STRIPPED_ROOT_NAMES["backend_meta"]
    try:
        for _entry in os.walk(staging, onerror=_reraise_walk_error):
            pass
        os.lstat(metadata)
    except FileNotFoundError:
        return None
    except OSError:
        return _ERROR_SIDECAR_STRIP
    try:
        _remove_reserved_sidecar_path(metadata)
    except OSError:
        return _ERROR_SIDECAR_STRIP
    return None


def _verify_staging_root(staging: Path, base: Path) -> str | None:
    """Re-verify ``staging`` is still the real, contained directory it was given;
    None = ok (R8-1).

    Called FIRST in ``_promote_staging``, before the metadata strip and therefore
    before validate and move too. The builder's ``run_shell`` runs with the
    SERVICE'S OWN uid and merely STARTS in staging (D21: deliberately unjailed,
    unlike the file
    meta-tools), so between the LLM session ending and promote running, a builder
    command can ``mv`` staging ASIDE and plant a SYMLINK at the original staging
    path pointing at ``base`` (the real, live tools directory) itself. The strip
    would otherwise remove a live backend metadata directory through that link,
    before ``validate_package`` ever ran.

    Two independent layers:

    * ``lstat`` on the path ITSELF, no resolve: it catches the attack above
      outright -- a symlink planted AT the staging leaf;
    * containment under the RESOLVED ``<base>/.staging`` shell
      (``tools._is_within``): catches ANCESTOR substitution the leaf check alone
      cannot -- e.g. ``.staging`` itself swapped for a symlink, which leaves the
      staging LEAF a perfectly ordinary directory while the fully RESOLVED path
      lands outside the shell entirely.

    Every look raises its errors (``os.lstat``, ``os.path.realpath`` with
    ``strict=True``), and one that fails refuses the workspace as unverified
    (``_ERROR_STAGING_UNVERIFIED``): ``is_symlink()`` answers False for a path it
    could not look at, and a resolve that is not strict hands back the path
    unresolved past a component it could not look at -- a shell swapped for a
    link then passes as the real one, and ``_cleanup_staging``'s rmtree would
    delete through it. Verified: a None answer rests on three looks that
    succeeded. Not covered: a swap between this check and the caller's use.

    This narrows a window rather than closing one, and is the SAME accepted
    residual class ``_promote_staging``'s own docstring names for its
    check-then-move exists-check (a race against a second concurrent actor with
    the service's uid) -- just one step earlier: check-then-REMOVE instead of
    check-then-move. A swap landing in the instant between this check and the
    strip's ``lstat`` syscall is still possible in principle; what this
    closes is the window the finding actually reported -- the ENTIRE builder
    session, start to finish -- down to that one syscall gap.
    """
    try:
        if stat.S_ISLNK(os.lstat(staging).st_mode):
            return _ERROR_STAGING_TAMPERED
        staging_root = Path(os.path.realpath(base, strict=True)) / _STAGING_DIRNAME
        resolved = Path(os.path.realpath(staging, strict=True))
    except OSError:
        return _ERROR_STAGING_UNVERIFIED
    if not tools._is_within(staging_root, resolved):
        return _ERROR_STAGING_TAMPERED
    return None


def _promote_staging(
    build_root: tools.BuildRoot,
    shell_root: Path,
    name: str,
    base: Path,
    origin: dict[str, Any],
    secret_name: str | None = None,
    secret_value: str | None = None,
) -> tuple[tools.Resolved | None, str | None]:
    """Assemble a complete package shell and atomically install it.

    Build validation happens before assembly. The committed version and package
    metadata are durable before the shell rename; the tools directory is durable
    before success is returned.
    """
    root_error = _verify_staging_root(build_root.path, base)
    if root_error is not None:
        return None, root_error
    if _entry_exists(shell_root):
        return None, _ERROR_STAGING_TAMPERED
    strip_error = _strip_builder_sidecars(build_root.path)
    if strip_error is not None:
        return None, strip_error
    error = tools.validate_tool_content(build_root, expected_name=name)
    if error is not None:
        return None, f"工具包驗證失敗：{error}"  # noqa: RUF001
    target = base / name
    if _entry_exists(target):
        return None, _ERROR_NAME_TAKEN

    try:
        base.mkdir(parents=True, exist_ok=True)
        shell_root.mkdir()
        versions = shell_root / tools._VERSIONS_DIRNAME
        versions.mkdir()
        (shell_root / tools._META_DIRNAME).mkdir()
    except OSError as exc:
        return None, f"無法組裝工具包（{type(exc).__name__}）。"  # noqa: RUF001

    vid = _choose_unused_vid(versions)
    if vid is None:
        return None, _ERROR_VERSION_ID_WRITE
    version = versions / vid
    try:
        os.rename(build_root.path, version)
        (version / tools._META_DIRNAME).mkdir()
    except OSError as exc:
        return None, f"無法組裝工具包（{type(exc).__name__}）。"  # noqa: RUF001
    if not tools.write_origin_meta(tools.BuildRoot(version), origin | {"previous": None}):
        return None, _ERROR_ORIGIN_WRITE

    # The committed marker and every entry it describes must survive before the
    # package-level pointer is allowed to name this version.
    if not _fsync_tree(version) or not _fsync_directory(versions):
        return None, _ERROR_DURABILITY
    package_layout = tools.PackageLayoutRoot(shell_root)
    if not tools.write_package_state(package_layout, True):
        return None, _ERROR_INSTALL_STATE_WRITE
    if not tools.publish_current(package_layout, vid):
        return None, _ERROR_CURRENT_WRITE
    if secret_name and secret_value:
        inject_error = _inject_secret_into_env(shell_root / ".env", secret_name, secret_value)
        if inject_error is not None:
            return None, inject_error

    # Persist the complete package shell, including backend-owned metadata and the
    # operator-supplied package-layer .env, before its one atomic install rename.
    # Then persist the parent entry before success.
    if not _fsync_tree(shell_root):
        return None, _ERROR_DURABILITY
    if _entry_exists(target):
        return None, _ERROR_NAME_TAKEN
    try:
        os.rename(shell_root, target)
    except OSError as exc:
        return None, f"工具包搬移失敗（{type(exc).__name__}）。"  # noqa: RUF001
    # Carry the identity established by this publish across later decoration.
    # Re-resolving ``current`` there would make this last typed-identity guarantee
    # a caller convention instead of a property of the publisher's return type.
    published = tools.Resolved(
        tools.PackageRoot(target),
        tools.VersionRoot(target / tools._VERSIONS_DIRNAME / vid),
        vid,
        tools.PREVIOUS_NULL,
    )
    if not _fsync_directory(base):
        return None, _ERROR_DURABILITY
    return published, None


@dataclass(frozen=True, slots=True)
class _PublishedRevision:
    """The exact committed version and provenance one revise put live."""

    resolution: tools.Resolved
    origin: dict[str, Any]


def _publish_revised_version(
    build_root: tools.BuildRoot,
    shell_root: Path,
    package_root: tools.PackageRoot,
    previous: tools.Resolved,
    expected_identity: tuple[int, int, int],
    feedback: str,
) -> tuple[_PublishedRevision | None, str | None, Path]:
    """Install one durable version and only then publish it through ``current``.

    Also says where the built content is when it returns, publish or not: it
    moves twice, ``build`` to the session's ``shell`` and then into
    ``versions/<vid>``, so after a failure it may be in staging still or already a
    version that ``current`` does not name. The caller reports that place when
    nothing else holds the work.
    """

    content: Path = build_root.path
    base = package_root.path.parent
    root_error = _verify_staging_root(build_root.path, base)
    if root_error is not None:
        return None, root_error, content
    if _entry_exists(shell_root):
        return None, _ERROR_STAGING_TAMPERED, content
    strip_error = _strip_builder_sidecars(build_root.path)
    if strip_error is not None:
        return None, strip_error, content
    error = tools.validate_tool_content(build_root, expected_name=package_root.path.name)
    if error is not None:
        return None, f"工具包驗證失敗：{error}", content  # noqa: RUF001

    try:
        package_info = os.lstat(package_root.path)
        versions_info = os.lstat(package_root.path / tools._VERSIONS_DIRNAME)
    except FileNotFoundError:
        return None, _ERROR_REVISE_TARGET_MISSING, content
    except OSError:
        return None, _ERROR_REVISE_TARGET_ALIAS, content
    if not stat.S_ISDIR(package_info.st_mode) or not stat.S_ISDIR(versions_info.st_mode):
        return None, _ERROR_REVISE_TARGET_ALIAS, content
    # Identity alone cannot answer whether the request's V is still current:
    # moving ``current`` to P leaves V's manifest tuple untouched. Re-resolve at
    # the publication stage, after the paid build but before assembling anything
    # into ``versions/``, so a D21 hand edit is preserved rather than overwritten.
    current = tools.resolve_current(package_root)
    if (
        isinstance(current, tools.Unresolved)
        or current.vid != previous.vid
        or tools.package_identity(previous.version_root) != expected_identity
    ):
        return None, _ERROR_REVISE_TARGET_REPLACED, content

    prior_origin = tools.read_origin_meta(previous.version_root) or {}
    origin = {
        "source": "builder-revise",
        "openapi_url": prior_origin.get("openapi_url"),
        "instructions": prior_origin.get("instructions"),
        "feedback": feedback,
        "previous": previous.vid,
    }
    try:
        os.rename(build_root.path, shell_root)
    except OSError as exc:
        return None, f"無法組裝工具版本（{type(exc).__name__}）。", content  # noqa: RUF001
    content = shell_root
    try:
        (shell_root / tools._META_DIRNAME).mkdir()
    except OSError as exc:
        return None, f"無法組裝工具版本（{type(exc).__name__}）。", content  # noqa: RUF001
    if not tools.write_origin_meta(tools.BuildRoot(shell_root), origin):
        return None, _ERROR_ORIGIN_WRITE, content
    if not _fsync_tree(shell_root):
        return None, _ERROR_DURABILITY, content

    versions = package_root.path / tools._VERSIONS_DIRNAME
    for _ in range(_VID_RETRY_LIMIT):
        vid = _choose_unused_vid(versions)
        if vid is None:
            return None, _ERROR_VERSION_ID_WRITE, content
        target = versions / vid
        if _entry_exists(target):
            continue
        try:
            os.rename(shell_root, target)
        except FileExistsError:
            continue
        except OSError as exc:
            return None, f"無法安裝工具版本（{type(exc).__name__}）。", content  # noqa: RUF001
        content = target
        published_version = tools.VersionRoot(target)
        if not _fsync_directory(versions):
            return None, _ERROR_DURABILITY, content

        # Re-check AFTER the rename and its directory fsync, immediately before
        # publishing ``current``. The earlier placement guarded creation of the
        # version entry but left that rename+fsync gap free for a D21 hand edit
        # that publication would overwrite. Check the manifest fact first, then
        # resolve ``current`` LAST so no other filesystem read sits between the
        # pointer answer and publish_current.
        if tools.package_identity(previous.version_root) != expected_identity:
            return None, _ERROR_REVISE_TARGET_REPLACED, content
        current = tools.resolve_current(package_root)
        if isinstance(current, tools.Unresolved) or current.vid != previous.vid:
            return None, _ERROR_REVISE_TARGET_REPLACED, content
        if not tools.publish_current(package_root, vid):
            return None, _ERROR_CURRENT_WRITE, content
        # Construct the typed target from the exact directory just renamed and
        # vid just published. Re-resolving by package name here or in the summary
        # hook would let a later current edit redirect the new version's work.
        published = tools.Resolved(
            package_root,
            published_version,
            vid,
            tools.PreviousValue(previous.vid),
        )
        return _PublishedRevision(published, origin), None, content
    return None, _ERROR_VERSION_ID_WRITE, content


def _sweep_stale_backups(base: Path) -> None:
    """Best-effort collection of marked packages, discarded versions and draft leftovers.

    One non-blocking exclusive lock replaces the old local-provenance and
    observed-running judgements. If any request or orphaned tool child still
    holds the shared side, the whole sweep is skipped and a later tool job
    retries. Hidden entries outside the marked namespace and symlinks are never
    traversed. A package directory and its ``versions/`` are gone into only when
    one ``lstat`` of each sees a real directory (``tool_drafts._is_real_dir``): one
    seen to be a link, or that cannot be looked at, is passed over, since through
    a link the discarded versions would be another package's -- maybe one outside
    the tools directory. A package's draft leftovers are
    ``tool_drafts.sweep_package``'s, which runs under this same lock and takes none
    of its own.
    """
    with tools.exclusive_tools_lock(base) as acquired:
        if not acquired:
            return
        with contextlib.suppress(Exception):
            for child in sorted(base.iterdir()):
                if (
                    not tools._STALE_BACKUP_RE.match(child.name)
                    or child.is_symlink()
                    or not child.is_dir()
                ):
                    continue
                shutil.rmtree(child, ignore_errors=True)
            # A durable discard can leave its former current version here when
            # best-effort cleanup fails. Retry only the exact committed namespace;
            # ordinary retained versions never match.
            for child in sorted(base.iterdir()):
                if not tools._NAME_RE.fullmatch(child.name) or not tool_drafts._is_real_dir(child):
                    continue
                tool_drafts.sweep_package(child, fsync_directory=_fsync_directory)
                versions = child / tools._VERSIONS_DIRNAME
                if not tool_drafts._is_real_dir(versions):
                    continue
                try:
                    entries = sorted(versions.iterdir())
                except OSError:
                    continue
                for entry in entries:
                    if (
                        entry.name.endswith(".discarded")
                        and tools._VID_RE.fullmatch(entry.name.removesuffix(".discarded"))
                        and not entry.is_symlink()
                        and entry.is_dir()
                        # Only discard itself can create a committed version
                        # under this suffix. A colliding operator entry is not
                        # ours to collect.
                        and tools.read_origin_meta(tools.VersionRoot(entry)) is not None
                    ):
                        shutil.rmtree(entry, ignore_errors=True)


def _cleanup_staging(staging: Path, base: Path) -> None:
    """Remove the whole session root (if the move did not consume it), drop the
    ``.staging`` shell when this was the last build in flight, and sweep whatever
    a delete had to leave behind.

    Blocking (runs via ``run_in_threadpool``), never raises: cleanup is
    best-effort by definition. ``rmdir`` (not rmtree) on the parent: it only
    succeeds on an EMPTY directory, which is exactly the wanted semantics --
    a concurrent session's staging keeps the shell alive, and the suppressed
    OSError for that case is the mechanism, not an accident.

    The rmtree is a DESTRUCTIVE traversal, so it runs ONLY on a root that
    passes the SAME ``_verify_staging_root`` gate ``_promote_staging`` applies.
    This is load-bearing, not symmetry for its own sake: ``run_install``'s
    ``finally`` reaches here on every exit but the two named below -- including
    after promote just REFUSED the workspace as tampered -- and CPython's rmtree
    protection only
    refuses a path that is ITSELF a symlink. With the ``.staging`` ANCESTOR
    swapped for a symlink to an external directory holding a real ``<uuid>``
    subdir, ``staging.is_dir()`` follows the link and the rmtree LEAF is an
    ordinary directory, so without this gate the external directory would be
    deleted straight through the link. A workspace that fails the gate is left
    IN PLACE, parent shell included: it is evidence of tampering (or of a race
    worth seeing), not garbage -- and the orphan it leaves is the same accepted
    residue class as the refused-cleanup orphan symlink.
    The check-to-rmtree instant remains the accepted check-then-act residual
    window ``_verify_staging_root``'s own docstring names.

    The sweep rides HERE rather than at the head of the next promote, and the
    reason is reach: every tool job ends here, bar the exits named below --
    installs as well as revises, failures as well as successes -- and a revise
    that keeps its staging runs the same sweep on its own (``_sweep_logged``), so
    cleanup-failure remains do not wait for another revise to succeed before
    anything looks at them. The non-blocking exclusive flock makes the same
    sweep safe after a restart: an inherited child keeps its shared reference,
    so the new process skips instead of mistaking an empty local registry for
    proof. This placement also keeps the promote's pre-publication sequence free
    of a new destructive traversal. It hangs off a ``finally`` because it is
    INDEPENDENT of everything above it: a tampered workspace returns early --
    deliberately, see above -- and that says nothing about whether marked remains
    elsewhere in ``base`` are collectable. The staging logic itself is untouched
    by this addition.

    Two kinds of exit never call this. One is a session whose run_shell commands
    or write_file writes could not be confirmed stopped (``_BuilderShells``): its
    workspace is kept whole, and that session skips the sweep as well -- the next
    job's call runs it. The other is a job that keeps its staging -- a revise or a
    publish of a draft for work no one holds, or any of the three for a worker
    copying into it or publishing it that may still be writing it, an install's
    promote among them (``run_install``, ``run_revise``, ``run_publish_draft``) --
    and runs the sweep without removing anything. Nothing in the backend ever
    collects what either keeps: a later job's call removes only its own session
    root, and ``.staging`` only once empty, while the sweep never looks inside
    ``.staging``. Only the operator removes it.
    """
    try:
        with contextlib.suppress(Exception):
            if _verify_staging_root(staging, base) is not None:
                return
            if staging.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
        with contextlib.suppress(OSError):
            staging.parent.rmdir()
    finally:
        _sweep_logged(base)


def _sweep_logged(base: Path) -> None:
    """``_sweep_stale_backups``, with every failure logged rather than raised.

    Blocking (runs via ``run_in_threadpool``). The one way a job ends the sweep,
    whether it removed its workspace first (``_cleanup_staging``) or kept it for
    the operator; the sweep never looks inside ``.staging`` either way.
    """
    try:
        _sweep_stale_backups(base)
    except tools.ToolsLockUnavailableError as exc:
        # This sweep is decoration after the job's real outcome is already
        # decided. Preserve the actionable lock path/remedy in the backend
        # console without turning an installed package into a failed job.
        # Logging is itself an observer and therefore cannot break the
        # cleanup contract even under a pathological custom handler.
        with contextlib.suppress(Exception):
            _logger.warning("Tool cleanup sweep skipped: %s", exc)
    except Exception as exc:
        # The sweep is best-effort for every failure class, not only lock
        # setup. Category-only avoids feeding an arbitrary filesystem
        # exception string into the operator log.
        with contextlib.suppress(Exception):
            _logger.warning(
                "Tool cleanup sweep failed (%s); a later tool job will retry.",
                type(exc).__name__,
            )


# --- the install run ---------------------------------------------------------


async def run_install(
    openapi_url: str,
    instructions: str,
    *,
    secret_name: str | None = None,
    secret_value: str | None = None,
) -> InstallOutcome:
    """Run one whole install: fetch, build in staging, validate, promote.

    Every failure is a FRIENDLY OUTCOME (zh-TW ``error``), never an exception:
    this runs inside a fire-and-forget background job whose only consumer is
    the polling FE, so an exception here would just vanish into the task. The
    ``llm_log_id`` is captured immediately after the builder call -- success
    OR failure -- because a failed build is exactly when the trace matters.

    ``secret_name``/``secret_value`` are the OPTIONAL install-form secret (D36),
    already validated (both-or-neither + env-var name) by ``ToolInstallRequest``.
    The value is registered as redactable for the whole build window, injected
    into run_shell's env so the builder can live-test the real API, and written
    into the promoted tool's ``.env`` -- but it NEVER enters any prompt text,
    job/outcome field, or error message (only the NAME reaches the prompt).
    """
    base = tools.tools_dir()
    if base is None:
        return InstallOutcome(ok=False, error=_ERROR_TOOLS_DISABLED)

    openapi_text, fetch_error = await _fetch_openapi(openapi_url)
    if openapi_text is None:
        return InstallOutcome(ok=False, error=fetch_error)

    session_root = base / _STAGING_DIRNAME / uuid4().hex
    build_root = tools.BuildRoot(session_root / "build")
    shell_root = session_root / "shell"
    try:
        await run_in_threadpool(session_root.mkdir, parents=True)
        await run_in_threadpool(build_root.path.mkdir)
    except OSError as exc:
        return InstallOutcome(ok=False, error=f"無法建立暫存工作區（{type(exc).__name__}）。")  # noqa: RUF001

    # The single {<NAME>: <value>} addition injected into run_shell's env (only
    # when both were supplied); None leaves the builder shell's env from-scratch.
    secret_env = {secret_name: secret_value} if (secret_name and secret_value) else None
    # Register the VALUE as redactable for the WHOLE build window: llm_log then
    # masks it out of every builder-session prompt/response it records -- covering
    # the gap BEFORE promote writes it into the package .env, after which the .env
    # scan in known_secret_values covers it too. The finally below releases this
    # hold on a return or an Exception; a job ended by a cancellation, an interrupt
    # or an exit keeps it until the process ends -- why, the finally says.
    if secret_value:
        tools.register_inflight_secret(secret_value)

    # True until the session starts: an exit before it has no command or write to
    # wait for.
    shells_ended = True
    # Up while the promote's worker may still be writing the staging. The await
    # ending with the worker's result or with an ``Exception`` lowers it -- the
    # worker has returned, or never began -- and a cancellation does not, since
    # the worker goes on without it.
    publish_running = False
    # Up once a BaseException that is not an Exception leaves the try below: the
    # event loop's shutdown cancelling the job, an interrupt, an exit -- each the
    # process ending. Set by the except clause, which runs before any step of the
    # finally, and never again: a step of the finally that raises afterwards does
    # not turn the exit into one the hold is released on.
    cancelled = False
    try:
        settings = get_settings()
        result: InstallResult | None = None
        llm_error: str | None = None
        shells = _BuilderShells()
        shells_ended = False
        try:
            result = await generate_structured(
                _builder_system_prompt(secret_name),
                _builder_user_prompt(instructions, openapi_text),
                InstallResult,
                workflow=_WORKFLOW,
                tools=_build_meta_tools(build_root.path, secret_env=secret_env, shells=shells),
                max_tool_rounds=settings.tool_install_max_rounds,
                timeout_seconds=settings.tool_install_timeout_seconds,
            )
        except LLMNotConfiguredError:
            llm_error = "LLM 尚未設定，無法執行安裝。"  # noqa: RUF001
        except LLMUpstreamError as exc:
            # str(exc) is safe by construction (category + fixed reason -- see
            # afterthread.services.llm); surfacing it names WHICH failure
            # (timeout vs invalid output vs upstream) without config leakage.
            llm_error = f"AI 建置工具失敗（{exc}）。"  # noqa: RUF001
        finally:
            # However the session ended -- an answer, an error, its deadline, or this
            # coroutine cancelled -- stop the commands and writes it started and wait
            # for them (``_BuilderShells``) before anything below touches the
            # workspace and before the job gives up its slot. If they cannot be
            # confirmed ended (``_BuilderShells``), or a second cancellation cuts the
            # wait short and this stays False, the workspace is not stripped,
            # published or removed.
            shells_ended = await shells.stop_and_wait()

        # The builder session ran (even a not-configured exit records one), so
        # link its AI 日誌 record to the outcome NOW -- error paths included.
        llm_log_id = llm_log.last_record_id_for_workflow(_WORKFLOW)
        if not shells_ended:
            # Given up for good: this returns at once, and nothing on the way out
            # waits on a worker thread (``_BuilderShells``).
            kept = _workspace_kept_error(session_root)
            return InstallOutcome(ok=False, error=(llm_error or "") + kept, llm_log_id=llm_log_id)
        env_keys, env_strip_error = await run_in_threadpool(_strip_builder_env, build_root, base)

        if env_strip_error is not None:
            return InstallOutcome(
                ok=False,
                error=env_strip_error,
                llm_log_id=llm_log_id,
                env_keys=env_keys,
            )
        if llm_error is not None:
            return InstallOutcome(
                ok=False,
                error=llm_error,
                llm_log_id=llm_log_id,
                env_keys=env_keys,
            )
        assert result is not None  # exactly one of result/llm_error is set above

        # F1/D36: the model is told never to print the secret in its summary, but a
        # disobedient builder could. Mask the summary again here -- the single choke
        # point for the outcome -- through the same ``_masked_and_capped`` as its
        # first mask, so the cap is applied again after a secret registered since
        # then is masked; derive every InstallOutcome below from that value. It
        # MUST happen HERE, not at job-update time: run_install's finally (below)
        # discards the in-flight secret before this function returns,
        # and a FAILED install also deletes the staging .env, so by the time _run_job
        # stores the outcome ``known_secret_values`` would no longer carry the value --
        # redacting then would be a no-op that leaks. The other outcome ``error`` fields
        # are safe by construction: the not-ready error is built from this masked
        # summary, and the llm/promote errors are backend/category strings that never
        # carry the value (str(exc) is category-only; promote errors are fixed zh-TW).
        summary = _masked_and_capped(result.summary, _SUMMARY_CAP) if result.summary else None

        if not result.ready:
            reason = summary or "（AI 未說明原因）"  # noqa: RUF001
            return InstallOutcome(
                ok=False,
                summary=summary,
                error=f"AI 判定工具尚未完成：{reason}",  # noqa: RUF001
                llm_log_id=llm_log_id,
                env_keys=env_keys,
                not_ready=True,
            )

        publish_running = True
        try:
            published, promote_error = await run_in_threadpool(
                _promote_staging,
                build_root,
                shell_root,
                result.tool_name,
                base,
                {
                    "source": "builder-install",
                    "openapi_url": tool_meta._sanitized_origin_url(openapi_url),
                    "instructions": instructions,
                    "feedback": None,
                },
                secret_name,
                secret_value,
            )
        except Exception:
            # The worker's own exception, or one before it began: either way
            # nothing of it writes into the staging any more.
            publish_running = False
            raise
        publish_running = False
        if promote_error is not None:
            return InstallOutcome(
                ok=False,
                tool_name=result.tool_name,
                summary=summary,
                error=promote_error,
                llm_log_id=llm_log_id,
                env_keys=env_keys,
            )
        assert published is not None
        # D40: the package is INSTALLED as of the line above -- everything from
        # here on is decoration. Generate the version's AI summary while we still
        # hold the install's context. Provenance itself is NOT at stake here: the
        # OpenAPI url and instructions were already written into the version's own
        # ``origin.json`` by ``_promote_staging`` before ``current`` named it (see
        # ``write_origin_meta``), so a later revise session reads them from there
        # whatever this hook does. What this placement buys is the summary's
        # quality and its redaction: the in-flight secret is still
        # registered, so the summary is redacted against it as well as against
        # the now-installed .env. ``generate_and_store_summary`` cannot raise
        # (see tool_meta): a summary that fails must never flip this outcome.
        #
        # The URL is sanitized HERE, at CAPTURE, so the credential-bearing form
        # never leaves this function (D40 r4): the raw string is what we FETCHED
        # with, and this call is the one place it would otherwise cross into a
        # module whose whole job is composing prompts and persisting text. A
        # presigned/unknown token in the query, or a known secret that appears
        # percent-encoded, is unmatchable by redaction -- so the parts that carry
        # credentials are dropped rather than masked (see
        # ``tool_meta._sanitized_origin_url``; nothing downstream re-fetches this
        # URL, it is provenance display). tool_meta sanitizes again on the prompt
        # and on a sidecar read-back, which covers hand-edited and pre-fix files;
        # this is what keeps the RAW value from ever being handed over at all.
        await tool_meta.generate_and_store_summary(
            published,
            origin={
                "openapi_url": tool_meta._sanitized_origin_url(openapi_url),
                "instructions": instructions,
            },
            builder_summary=summary,
        )
        return InstallOutcome(
            ok=True,
            tool_name=result.tool_name,
            summary=summary,
            llm_log_id=llm_log_id,
            env_keys=env_keys,
        )
    except BaseException as exc:
        cancelled = not isinstance(exc, Exception)
        raise
    finally:
        # The hold on the form's secret goes first, on a return or an Exception
        # only: every worker that took the known secrets after it was registered to
        # make something seen or written -- the promote, the summary's store -- was
        # then awaited to its result or to its own exception, or never began. What
        # may still run is a command or a write not confirmed stopped, or a
        # read_file or list_dir, and no one receives what they make. Ended any other
        # way -- cancelled, interrupted, exiting: the process ending each time -- the
        # job keeps the hold until the process ends. A promote's worker it started
        # goes on without it, and takes the known secrets to scan the build and to
        # write origin.json before the value is in an installed package's .env, the
        # one the scan of known secrets reads -- and a promote that fails never
        # installs one.
        #
        # Then the staging. A successful move consumed the staging dir; cleanup then
        # only drops the (possibly empty) .staging shell. Every other exit removes
        # the build -- except one whose commands or writes could not be confirmed
        # stopped, which keeps it, and one whose promote's worker may still be
        # writing it, which keeps it while the sweep still runs (it never looks
        # inside ``.staging``).
        if secret_value and not cancelled:
            tools.discard_inflight_secret(secret_value)
        if not shells_ended:
            # Kept in place: a command or a write not confirmed stopped may still be
            # using it.
            _log_workspace_kept(session_root)
        elif publish_running:
            _log_publish_running_kept(session_root)
            await run_in_threadpool(_sweep_logged, base)
        else:
            await run_in_threadpool(_cleanup_staging, session_root, base)


# --- the revise run -----------------------------------------------------------


def _revise_copy_ignore(root: Path, source_dir: Any, names: list[str]) -> set[str]:
    """The names ``shutil.copytree`` leaves out of a first round's copy of the
    version at ``root``: in ``root`` itself, those ``tool_drafts.withheld_root_names``
    leaves out, by the rule a copy of a draft keeps too -- the root entries the strip
    before a save removes (``tool_drafts.STRIPPED_ROOT_NAMES``), and a name that is
    one of them spelled in another case, or that cannot be told not to be. The
    version keeps whatever is left out. Nothing in a directory below ``root``.

    The source root is a VersionRoot, not a PackageRoot: the two legacy
    package-reserved filenames are ordinary tool content at this scope, since
    migration may have put a FOREIGN ``.afterthread-state.json`` here byte-for-byte,
    and ``.ai_meta.json`` has no v5 reader here either. So are nested names, and a
    case-variant that is an entry of its own.

    ``copytree`` calls this once for each directory it copies, with the names it
    listed there, before it makes that directory in the copy; it neither copies nor
    goes into a name this returns. Each look it makes is ``tool_drafts._same_entry``'s,
    which answers a failed one with None rather than raising.
    """
    if Path(source_dir) != root:
        return set()
    return set(tool_drafts.withheld_root_names(root, names))


def _copy_package_into_staging(source: tools.VersionRoot, staging: tools.BuildRoot) -> str | None:
    """Copy the installed package into a fresh staging dir; None = ok (D40).

    Blocking (runs via ``run_in_threadpool``). ``copytree`` creates ``staging``
    (and the ``.staging`` shell above it) itself, so there is no separate mkdir
    to race with.

    ``symlinks=True`` copies links AS links rather than dereferencing them. Both
    halves matter: it keeps the copy FAITHFUL (a package that shipped a symlink
    still has one, and a dangling link stays dangling instead of aborting the
    copy), and it refuses to slurp CONTENT from outside the package -- with the
    default, a link to another tool's ``.env`` would materialize as a real file
    full of live credentials inside staging, which the embedded-secret gate would
    then reject with the operator having written no such file.

    Any ``OSError`` -- including ``shutil.Error``, which subclasses it and is
    what ``copytree`` raises for per-file failures collected during the walk --
    becomes the friendly outcome. An unreadable subtree therefore FAILS the
    revise loudly here.
    """
    try:
        shutil.copytree(
            source.path,
            staging.path,
            symlinks=True,
            ignore=partial(_revise_copy_ignore, source.path),
        )
    except OSError as exc:
        return f"無法複製既有工具包到暫存工作區（{type(exc).__name__}）。"  # noqa: RUF001
    return None


def _read_env_for_values(directory: Path) -> tuple[bool, dict[str, str], str, str | None]:
    """Read package ``.env`` values for masking and builder-shell export.

    This is a bounded, non-following read. It never writes or copies the package
    file; revise leaves those bytes and that directory entry untouched.
    """
    env_file = directory / ".env"
    try:
        env_stat = env_file.lstat()
    except FileNotFoundError:
        return False, {}, "", None
    except OSError:
        return False, {}, "", _ERROR_REVISE_ENV_UNREADABLE
    if env_stat.st_size > tools._ENV_FILE_MAX_BYTES:
        # The BYTE ceiling promote already enforces, applied before the session
        # starts instead of after it (R5-3). ``lstat`` on a symlink or a directory
        # reports that entry's own size, so neither can buy its way past this --
        # the bounded reader below refuses both regardless.
        return False, {}, "", _ERROR_REVISE_ENV_TOO_LARGE
    text = tools._read_regular_file_capped(env_file, tools._ENV_FILE_MAX_BYTES)
    if text is None:
        return False, {}, "", _ERROR_REVISE_ENV_UNREADABLE
    if len(text) > tools._ENV_FILE_MAX_BYTES:
        return False, {}, "", _ERROR_REVISE_ENV_TOO_LARGE
    # The parse stays on THIS worker (R4-3): handing the text back and calling
    # python-dotenv from the caller put the whole parse back on the event loop.
    return True, tools._parse_dotenv_text(text), text, None


def _read_config_for_values(directory: Path) -> tuple[dict[str, str], str, str | None]:
    """Read package ``config.env`` (web-v6 §4) for the builder shell and the prompt.

    The same bounded, non-following read as ``_read_env_for_values`` and the same
    "missing is fine, unreadable refuses" split, for the reason the error
    constants give. Returns ``(values, text, error)``; the text is what the prompt
    quotes, since its comments are part of what the operator meant.
    """
    config_file = directory / tools._CONFIG_FILENAME
    try:
        config_stat = config_file.lstat()
    except FileNotFoundError:
        return {}, "", None
    except OSError:
        return {}, "", _ERROR_REVISE_CONFIG_UNREADABLE
    if config_stat.st_size > tools._ENV_FILE_MAX_BYTES:
        return {}, "", _ERROR_REVISE_CONFIG_TOO_LARGE
    text = tools._read_regular_file_capped(config_file, tools._ENV_FILE_MAX_BYTES)
    if text is None:
        return {}, "", _ERROR_REVISE_CONFIG_UNREADABLE
    if len(text) > tools._ENV_FILE_MAX_BYTES:
        return {}, "", _ERROR_REVISE_CONFIG_TOO_LARGE
    return tools._parse_dotenv_text(text), text, None


@dataclass(frozen=True, slots=True)
class _ReviseEnv:
    """What one read of a package's two env files gave the revise gate."""

    env_values: dict[str, str]
    config_values: dict[str, str]
    config_text: str


def _revise_env_gate(directory: Path) -> tuple[_ReviseEnv | None, str | None]:
    """Read ``.env`` and ``config.env`` and apply the whole revise env policy.

    Blocking, ONE worker hop for all of it (R1-3/R4-3): the ``.env`` read and
    parse, the masking policy over its values, and the ``config.env`` read. Called
    twice per revise, deliberately (web-v6 §5.2): once on the SUBMIT path, before
    the global slot is taken, so a refusal reaches the operator as the response
    to the click that caused it instead of as a job card they may never open; and
    again inside ``run_revise``, on whatever the files say by then, because a
    queued job can start minutes after its submit.

    Returns ``(env, None)`` on success and ``(None, error)`` on any refusal.
    Only the non-empty ``.env`` values are judged: a ``KEY=`` line contributes no
    secret to anything (``tools._cached_env_values``' own rule), so it can never
    make a package permanently unrevisable.

    It can also RAISE, and that is accepted rather than caught (P1 review C4): a
    refusal names keys through ``redact_known_secrets``, which fails closed when
    the known-secret scan itself fails (an unreadable tools directory). On the
    submit path that surfaces as a plain 500 before any slot, job or secret
    registration exists; inside the job it is ``_run_job``'s category-only
    backstop. Neither leaks text, and mapping it onto ``env_policy`` would tell the
    operator their files are wrong when the scan is what broke.
    """
    _env_existed, env_values, env_text, env_error = _read_env_for_values(directory)
    if env_error is not None:
        return None, env_error
    registered_by_key = {key: value for key, value in env_values.items() if value}
    mask_error = _unmaskable_env_error(env_text, registered_by_key)
    if mask_error is not None:
        return None, mask_error
    config_values, config_text, config_error = _read_config_for_values(directory)
    if config_error is not None:
        return None, config_error
    return _ReviseEnv(env_values, config_values, config_text), None


def _env_assignment_rhs(text: str) -> dict[str, str]:
    """Each key in a ``.env`` text mapped to the RAW RHS that DETERMINES its value.

    Pure. One pass over the lines, later assignments overwriting earlier ones,
    because python-dotenv is LAST-occurrence-wins: what survives per key is the
    spelling that actually decided the parsed value. The key recognizer is
    ``_env_line_key`` -- the same one ``_inject_secret_into_env`` uses to drop
    EVERY prior line assigning its name, for that identical last-wins reason, so
    the two sides of this module agree on what "the line that assigns K" means.
    The RHS is everything after the FIRST ``=`` (a dotenv key cannot contain one),
    whitespace-stripped.

    Split on ``"\\n"`` rather than ``str.splitlines()``, and that is correctness
    rather than habit: dotenv's own parser breaks lines on ``\\r\\n``/``\\n``/``\\r``
    only, and the bounded reader that produced this text already normalized the
    first two forms to ``\\n`` (universal newlines). ``splitlines()`` ALSO breaks on
    ``\\v``, ``\\f``, ``\\x1c``-``\\x1e``, ``\\u2028`` and ``\\u2029`` -- characters
    dotenv keeps INSIDE a value -- so it would invent assignment lines the parser
    never saw and answer for a key off a line that does not exist.

    Line-based, so a value that SPANS lines is deliberately not represented
    faithfully: its continuation lines assign nothing (no ``=``, so
    ``_env_line_key`` returns None) and the opening line's RHS holds only the first
    fragment. The caller's rule -- the parsed value must appear LITERALLY in the
    RHS -- therefore REFUSES those shapes instead of vouching for them, which is
    the direction every guard on this path takes.
    """
    spelled: dict[str, str] = {}
    for line in text.split("\n"):
        key = _env_line_key(line)
        if key is not None:
            spelled[key] = line.split("=", 1)[1].strip()
    return spelled


def _env_assignment_rhs_all(text: str) -> list[tuple[str, str]]:
    """EVERY assignment line's ``(key, raw RHS)``, shadowed ones included (R8-1).

    The key rides along only so a refusal can NAME the line (web-v6 §5); the RHS
    is still what is judged.

    Pure, and deliberately the counterpart of ``_env_assignment_rhs`` rather than
    a variant of it: that one answers "which spelling DECIDED this key's value"
    and therefore keeps exactly one line per key; this one answers "what spellings
    does this file CONTAIN", which is a different question with a different
    answer. Same line splitting and same key recognizer, so the two agree on what
    an assignment line is.

    The shadowed lines are the whole point. dotenv discards them, so they carry no
    parsed value -- but they can still SPELL the live credential in the reversible
    form the winning line avoided, and a ``cat`` of the file shows them all.
    """
    return [
        (key, line.split("=", 1)[1].strip())
        for line in text.split("\n")
        if (key := _env_line_key(line)) is not None
    ]


# The key name `_plainly_spelled` assigns its probe RHS to before handing the line
# to the real dotenv parser. Any valid key works; a fixed private one keeps the
# probe out of the way of whatever keys a package actually uses, and naming it
# here rather than inlining a literal makes the probe's purpose legible at the
# call site.
_SHAPE_PROBE_KEY = "AFTERTHREAD_SPELLING_PROBE"


def _plainly_spelled(rhs: str) -> bool:
    """True when an RHS holds its own text LITERALLY -- no reversible encoding.

    Pure, and answerable WITHOUT knowing which value the line was meant to spell,
    which is what makes it usable on a shadowed line (R8-1). It asks the same
    question ``_dotenv_safe_spellings`` asks, from the other end: rather than
    "which spellings are safe for THIS value", it asks "is this spelling safe for
    whatever it contains" -- so it strips the quoting dotenv would strip and
    checks that the result round-trips back to the same RHS through
    ``_dotenv_safe_spellings``.

    An empty RHS (``KEY=``) is plain: it holds nothing to hide.

    What this must NOT do is decide for itself what a spelling means. The first
    attempt stripped matching quotes and treated the remainder as literal, and
    that was wrong in a way worth recording (R9-1): python-dotenv decodes ``\\\\``
    to a single backslash INSIDE SINGLE QUOTES too -- verified against the pinned
    library, not assumed from shell habits, where single quotes are literal. So::

        TOKEN='abc\\\\defghi'   <- reversible: two backslashes on disk, one in the value
        TOKEN='abc\\defghi'    <- the same live value, spelled plainly

    the shadowed line passed as "plain" while spelling the winner's credential in
    a form no redactor matches. The fix is to stop hand-reading the spelling: the
    RHS is handed to the REAL parser (as a probe assignment) to learn what it
    means, and only then asked whether it is a spelling we could have written for
    that meaning. Two libraries answer the two questions they own -- dotenv what a
    line means, our serializer which spellings are safe -- and neither is
    re-implemented here. An RHS the parser cannot read as a value at all is
    refused, like every other shape this gate cannot account for.
    """
    if not rhs:
        return True
    probed = tools._parse_dotenv_text(f"{_SHAPE_PROBE_KEY}={rhs}\n").get(_SHAPE_PROBE_KEY)
    if probed is None:
        return False
    return rhs in _dotenv_safe_spellings(probed)


def _dotenv_safe_spellings(value: str) -> tuple[str, ...]:
    r"""Every RHS spelling this system is willing to VOUCH for ``value`` with.

    Pure. The set is EXACTLY the shapes ``_dotenv_serialize_value`` treats as
    safe, which is why that function is called here rather than its rules being
    restated: the bare value is admitted precisely when the serializer would have
    WRITTEN it bare (its first, most-preferred branch, so ``serialize(v) == v``
    holds for that case and no other -- a quoted result differs from the value by
    its two delimiters and can never equal it). The two quoted forms carry the
    serializer's own conditions verbatim: single quotes are literal to
    python-dotenv, so ``'value'`` embeds the value whenever the value has no
    single quote of its own; double quotes are safe only while NO escape can fire,
    which is exactly the ``"``/``\`` exclusion the serializer's docstring gives.

    The set is therefore a SUPERSET of what the serializer emits (it prefers one
    spelling; a hand-written file may legitimately have chosen a more-quoted one
    of the same three) and it is EMPTY exactly when the serializer returns None --
    a value carrying a single quote AND a ``"``/``\`` cannot be spelled here at
    all, which is the same verdict install reaches when it refuses to write one.

    Deliberately a set of literals rather than a re-parse: the caller compares the
    RAW right-hand side against these strings for EQUALITY, so a spelling we
    cannot generate ourselves cannot slip in beside one we can.
    """
    spellings: list[str] = []
    if _dotenv_serialize_value(value) == value:
        spellings.append(value)
    if "'" not in value:
        spellings.append(f"'{value}'")
    if '"' not in value and "\\" not in value:
        spellings.append(f'"{value}"')
    return tuple(spellings)


def _unmaskable_env_error(text: str, values: dict[str, str]) -> str | None:
    """The whole "can we mask this package's ``.env`` values" policy; None = yes.

    Blocking, and it runs on a worker for the reason every other scan in this
    module does (R1-3): a 64 KiB ``.env`` can hold thousands of values, and
    walking the file plus a substring search per value is hundreds of milliseconds
    of real work on a background job that shares the event loop with every HTTP
    request in the process. One extra hop on a session measured in MINUTES is not
    a cost worth arguing about; a loop stall is.

    ``values`` is the already-filtered non-empty ``key -> value`` mapping the
    caller will register -- an empty value (``KEY=``) is not a secret, contributes
    nothing to ``known_secret_values`` (``tools._cached_env_values``' own rule),
    and so can never make a package permanently unrevisable through either gate
    below. The KEYS are needed, not just the values: the second gate is about the
    line each value came from (R6-1).

    Both gates say the same thing -- a value the redactors cannot mask must not be
    handed to a builder session -- and they live together because they ARE one
    policy, differing only in WHY the mask would miss:

    * TOO SHORT (R1-1): ``tools.redact_known_secrets`` and ``llm_log._redact``
      deliberately ignore every value under ``tools._MIN_SECRET_LEN``, because
      masking a 1-5 char value would shred ordinary prose. Registering ``1234``
      therefore protects nothing;
    * SPELLED REVERSIBLY (R5-1): the redactors only ever see the value dotenv
      PARSED, while the file on disk may spell it some other way that parses back
      to the same thing -- ``KEY="ab'cd\\"ef"`` for ``ab'cd"ef``. The builder's
      unjailed ``run_shell`` (D21) can ``cat`` the LIVE package's ``.env``, which
      is outside staging but perfectly reachable, and that raw text equals no
      registered value, so neither the live redactor nor ``llm_log`` masks it: it
      lands in the next round's prompt, in the AI 日誌, and possibly in the summary
      -> job body -> sidecar.

    That second gate is asked of the ASSIGNMENT LINE, not of the whole file
    (R6-1), and it demands EQUALITY with a spelling we could have written
    ourselves, not mere containment (R7-1). PARTIAL matching has now been beaten
    twice by the same trick at two scales -- r5 searched the whole FILE, so a
    comment on the next line vouched for the assignment; r6 searched the raw RHS,
    so a comment on the SAME line did::

        TOKEN="abcd\\"efgh"
        # abcd"efgh                      <- defeated the r5 whole-file search
        TOKEN="abcd\\"efgh" # abcd"efgh" <- defeated the r6 same-line search

    Both times the line that actually holds the credential spelled it in a form no
    redactor matches, so a ``cat`` of the file emitted it and the live value was
    recoverable from the escape -- the entire leak this gate exists to stop --
    while some adjacent text answered "yes, it's in there". The lesson is that no
    substring rule can be vouched for by text sitting NEXT to it, so the gate now
    admits only what it can generate: the stripped RHS of the LAST line whose key
    matches (``_env_assignment_rhs``) must EQUAL one of ``_dotenv_safe_spellings``
    -- the bare value, ``'value'``, or ``"value"``, each admitted exactly when
    ``_dotenv_serialize_value`` considers it safe. ``KEY=abcdef``,
    ``KEY="abcdef"`` and ``KEY='abcdef'`` pass; ``KEY="ab\\"cd"`` does not, and
    neither does anything with something else on the line.

    That REFUSES shapes which are legitimate dotenv and were not leaks, and the
    cost is accepted deliberately:

    * ``KEY=value # comment`` -- an ordinary trailing comment. dotenv drops it and
      hands us ``value``, but the RHS is ``value # comment``, which is not a
      spelling we could produce. It is also indistinguishable, by any rule short
      of re-implementing dotenv's tokenizer, from the reversible line above with a
      comment stapled on;
    * ``KEY=two words`` or ``KEY=a#b`` -- values the serializer would have QUOTED.
      They do embed the value literally, but a bare RHS is only admitted when the
      serializer would have written it bare, so these ask for quotes instead;
    * a value holding both ``'`` and ``"``/``\\`` -- ``_dotenv_safe_spellings`` is
      empty for it, the same verdict install reaches when it refuses to WRITE such
      a value at all.

    Every one of them has the same one-line remedy the message already names
    (simplify the quoting), and none of them is a shape this system produces --
    which is the property that matters: an ``.env`` we wrote passes BY
    CONSTRUCTION (see below), so the tightening cannot make an ordinary installed
    package unrevisable.

    A key with NO locatable assignment line refuses too -- a value spanning lines,
    a continuation, any shape this line-based reading cannot account for. We
    verify what we can READ and refuse what we cannot, the same direction
    ``_read_env_for_values`` takes when an unreadable ``.env`` stops the revise. It
    narrows what r5 accepted -- a genuinely MULTI-LINE quoted value used to pass,
    because its newlines are real newlines in the file and the whole value really
    was a substring of the whole text -- and that narrowing is deliberate: a
    single assignment line can never contain a newline, so nothing here can tell
    such a spelling apart from one whose first fragment merely happens to sit on
    the opening line.

    EVERY assignment line is checked, not only the deciding one (R8-1), and the
    reasoning that let the earlier ones through was simply wrong. It ran: a
    shadowed line is not what dotenv parsed, so it holds no registered value and
    is ordinary file text. But a shadowed line can encode the SAME live value the
    winning line does, in the reversible spelling the winner avoided::

        TOKEN="abcd\\"efgh"    <- shadowed, but this IS the live credential
        TOKEN='abcd"efgh'      <- what dotenv parses; a spelling we accept

    The registered value is ``abcd"efgh``; a ``cat`` shows both lines; the
    redactor masks the second and hands the first to the model verbatim. So a
    shadowed line is checked too -- not against a value (we cannot know which
    value it was meant to spell) but for its SHAPE: its RHS must be a spelling
    that holds its own text literally, i.e. one ``_dotenv_safe_spellings`` would
    admit for the text it contains. A line written plainly can hide nothing; a
    line carrying an escape is refused whether or not we can prove what it hides,
    which is the same "refuse what we cannot verify" this whole gate is built on.

    The INSTALL path already treats exactly this as a leak and refuses:
    ``_dotenv_serialize_value`` returns None for a value it could only write with
    a FIRING escape, and ``_inject_secret_into_env``'s round-trip check backstops
    it, precisely so the raw line keeps the value as an exact substring. Install
    controls the spelling because install WRITES the file -- and every shape it
    can write passes here BY CONSTRUCTION: it drops every prior line assigning the
    name and APPENDS the serialized one, so its line is both the last one for that
    key and one of the three spellings ``_dotenv_safe_spellings`` admits -- which
    is not a coincidence to be maintained by hand, since that set is built by
    ASKING the very serializer install writes with. A revise INHERITS whatever
    a hand-edit left there, and until now inherited it without that guarantee.
    This is the same rule at the other entry point -- the same shape as the floor
    check being the install FORM's ``schemas._SECRET_VALUE_MIN_LEN`` applied here.

    The comparison text is what ``tools._read_regular_file_capped`` produced (utf-8
    with ``errors="replace"``, universal newlines), and that is the RIGHT
    representation rather than a convenient one: ``_run_shell_subprocess`` decodes
    its merged output with the identical settings, so a value that is a literal
    substring HERE is a literal substring of whatever a ``cat`` of that file puts
    in front of the redactor.

    Refusing is the only honest option (the same "do not run beats leak" the floor
    check already chose), and the messages name the CONDITION, the REMEDY and
    every offending KEY (web-v6 §5) -- never the value. Each gate therefore
    collects all of its keys before answering instead of stopping at the first,
    so one refusal names everything the operator has to move; the passing case
    already walked every key and every line, so this costs nothing new.
    """
    short = [key for key, value in values.items() if len(value) < tools._MIN_SECRET_LEN]
    if short:
        return _env_policy_message(_ERROR_REVISE_ENV_UNMASKABLE, short)
    spelled = _env_assignment_rhs(text)
    unmatched = [
        key
        for key, value in values.items()
        if (rhs := spelled.get(key)) is None or rhs not in _dotenv_safe_spellings(value)
    ]
    # ... and every OTHER assignment line, shadowed ones included (R8-1), for its
    # SHAPE alone: `_plainly_spelled` asks whether a line holds its own text
    # literally, which is answerable without knowing which value it was meant to
    # spell -- and it is exactly what a shadowed line needs to be innocent.
    # `spelled` cannot serve here: it keeps only the WINNER per key, which is the
    # one line this check does not need.
    unmatched += [key for key, rhs in _env_assignment_rhs_all(text) if not _plainly_spelled(rhs)]
    if unmatched:
        return _env_policy_message(_ERROR_REVISE_ENV_UNMATCHABLE, unmatched)
    return None


# At most this many keys are named in one ``.env`` policy message, each cut to
# ``_ENV_KEY_SHOWN_MAX`` characters: a hand-edited file can hold thousands of
# lines, and the message is a job error and a 409 body, not a report. The count of
# the rest is still given, so the operator knows the list is not complete.
_ENV_KEYS_SHOWN_MAX = 5
_ENV_KEY_SHOWN_MAX = 64


def _env_policy_message(template: str, keys: list[str]) -> str:
    """Fill a ``.env`` policy message's ``{keys}`` with the offending key names.

    Blocking (it redacts). Duplicates collapse in first-seen order, since one key
    can fail both the deciding-line and the shadowed-line check. Each name is
    redacted BEFORE it is cut, the redact->cap order every text in this module
    follows: key names are operator-chosen, so in principle one could equal a
    registered value, and cutting first could leave a fragment no redactor
    matches.

    A "key" that starts with ``#`` is a COMMENT line holding an ``=``: the
    shadowed-line check (R8-1) judges every line ``_env_line_key`` recognizes,
    comments included, and that is deliberate -- a ``cat`` prints a comment as
    readily as an assignment. Only the wording follows from it (P1 review C3): the
    line is named as a comment line, so the operator is not told a key called
    ``# Usage: KEY`` exists.
    """
    unique = list(dict.fromkeys(keys))
    shown = []
    for key in unique[:_ENV_KEYS_SHOWN_MAX]:
        redacted = tools.redact_known_secrets(key)
        if len(redacted) > _ENV_KEY_SHOWN_MAX:
            redacted = redacted[:_ENV_KEY_SHOWN_MAX] + "…"
        label = f"「{redacted}」"
        shown.append(f"註解行{label}" if key.startswith("#") else label)
    named = "、".join(shown)
    if len(unique) > _ENV_KEYS_SHOWN_MAX:
        named += f"等 {len(unique)} 個"
    return template.replace("{keys}", named)


def _current_manifest_text(directory: Path) -> str | None:
    """The ``tool.json`` text at the top of ``directory`` for the revise prompt, or None.

    Read from the round's workspace once it is made: for a first round that is
    the installed version as copied, for a continuation the draft's files, which
    may have changed it. Blocking, and best-effort: a manifest we cannot read is
    one the model will simply have to ``read_file`` for itself (or that is
    already broken), not a reason to refuse the revise the user asked for.
    """
    return tools._read_regular_file_capped(directory / "tool.json", tools._MANIFEST_MAX_BYTES)


# The job's note on a published revise whose draft stayed. Only when the removal
# ran on a draft found to be what the save made, and it is not seen gone, is
# discarding it all that is left to do; when the draft was left because it holds
# more, or because that could not be told, it may hold changes no version has,
# and the note says to take them out first (``tool_drafts.remove_after_publish``).
_DRAFT_NOT_REMOVED = "已發布，但未能刪除這份草稿，請在工具列放棄它。"  # noqa: RUF001
_DRAFT_KEPT_CHANGED = (
    "已發布，但草稿仍有額外的項目，可能含沒發布的內容。"  # noqa: RUF001
    "要保留的話，先從草稿目錄取出，再到工具列放棄這份草稿。"  # noqa: RUF001
)
_DRAFT_KEPT_UNCONFIRMED = (
    "已發布，但無法確認草稿內容仍與發布前相同，因此保留。"  # noqa: RUF001
    "草稿裡可能有沒發布的修改：需要的話先從草稿目錄取出，再到工具列放棄這份草稿。"  # noqa: RUF001
)
# By ``tool_drafts.Removal.kept``: why the removal was not attempted.
_DRAFT_LEFT_REASONS = {"changed": _DRAFT_KEPT_CHANGED, "unconfirmed": _DRAFT_KEPT_UNCONFIRMED}
# A continuation that published while its save before the publish did not keep
# the round: no ``publishing`` was written, the round left the draft's files as
# they are, and the version has what the round did. The round is recorded in the
# manifest once the publish went through (``_close_unrecorded``).
_DRAFT_ROUND_NOT_SAVED = "已發布；這一輪沒有存進草稿，請取回草稿裡需要的內容再放棄它。"  # noqa: RUF001

# Why a continuation did not begin: nothing was spent and no round is counted.
# ``{reason}`` is ``tool_drafts``' own words, last since it may be sentences.
_ERROR_CONTINUE_NOT_STARTED = "無法開始接續修正（這一輪沒有開始，也沒有呼叫 AI）：{reason}"  # noqa: RUF001
_ERROR_CONTINUE_NOT_COPIED = (
    "無法把草稿帶進新的暫存工作區（這一輪沒有開始，也沒有呼叫 AI）：{reason}"  # noqa: RUF001
)

# A publish that refused its workspace as not the staging directory it was given:
# where that workspace is now is not known, so no path is claimed for it.
_STAGING_REFUSALS = frozenset({_ERROR_STAGING_TAMPERED, _ERROR_STAGING_UNVERIFIED})


@dataclass(frozen=True, slots=True)
class _RoundDraft:
    """Everything a revise round's draft save needs, gathered once the session ended.

    The notes are the model's own words, already masked and capped; nothing reads
    them to decide anything (``tool_drafts.DraftRound``). A continuation also
    carries the manifest its ``begin_round`` wrote (``begun``), which every
    write of the round is matched against, ``copied_out`` from its copy of the
    draft, its own note, the draft's paths it did not receive, and the records
    it newly attached.
    """

    base: Path
    build_root: tools.BuildRoot
    package_root: tools.PackageRoot
    package_dir_identity: tuple[int, int] | None
    base_version: tools.VersionRoot
    copied_in: tool_drafts.TreeDigest | None
    start: tool_drafts.DraftStart
    job_id: str | None
    started_at: str
    facts: dict[str, Any]
    summary: str | None
    handoff: str | None
    operator_action: str | None
    begun: dict[str, Any] | None = None
    copied_out: tool_drafts.TreeDigest | None = None
    note: str | None = None
    not_brought_in: tuple[tool_drafts.Omitted, ...] = ()
    attached: tuple[AttachedRun, ...] = ()


def _draft_round(
    context: _RoundDraft, outcome: tool_drafts.RoundOutcome | None, error: str | None
) -> tool_drafts.DraftRound:
    """How the round ended, as its draft records it."""
    facts = context.facts
    return tool_drafts.DraftRound(
        finished_at=_now_iso(),
        job_id=context.job_id,
        started_at=context.started_at,
        outcome=outcome,
        stopped_by=facts.get("stopped_by"),
        failure=facts.get("failure"),
        rounds_used=facts.get("rounds_used"),
        max_rounds=facts.get("max_rounds"),
        error=error,
        summary=context.summary,
        handoff=context.handoff,
        operator_action=context.operator_action,
        note=context.note,
        not_brought_in=context.not_brought_in,
        attached=context.attached,
    )


def _workspace_confirmed(build_root: tools.BuildRoot, base: Path) -> bool:
    """Whether the round's workspace is still the real, contained staging directory
    it was given (``_verify_staging_root``), and there; never raises. A path that
    fails this is not one a job may name as where the work is."""
    try:
        workspace = build_root.path
        return (
            _verify_staging_root(workspace, base) is None
            and tool_drafts.directory_identity(workspace) is not None
        )
    except Exception:
        return False


def _save_files(
    context: _RoundDraft, outcome: tool_drafts.RoundOutcome | None, error: str | None
) -> tool_drafts.SaveResult:
    """The save itself: the steps it shares with the publish, then
    ``tool_drafts.save_round``. Never raises.

    The caller has confirmed the round's commands stopped and stripped the
    builder's ``.env``; this strips the root ``.afterthread.meta/`` and checks
    that the workspace is still the real, contained staging directory it was
    given. A workspace that cannot be confirmed is ``REASON_WORKSPACE``: where it
    is, nothing can say.
    """
    try:
        workspace = context.build_root.path
        if not _workspace_confirmed(context.build_root, context.base):
            return tool_drafts.SaveResult(worth_saving=True, reason=tool_drafts.REASON_WORKSPACE)
        if _strip_builder_sidecars(workspace) is not None:
            return tool_drafts.SaveResult(
                worth_saving=True, reason=tool_drafts.REASON_STRIP_SIDECARS
            )
        settings = get_settings()
        begun = context.begun
        return tool_drafts.save_round(
            package_root=context.package_root,
            package_dir_identity=context.package_dir_identity,
            workspace=workspace,
            earlier_root=(
                context.base_version.path
                if begun is None
                else tool_drafts.previous_files(context.package_root, begun)
            ),
            copied_in=context.copied_in,
            draft_round=_draft_round(context, outcome, error),
            start=context.start if begun is None else None,
            previous=(
                None if begun is None else tool_drafts.PreviousDraft(begun, context.copied_out)
            ),
            max_files=settings.tool_draft_max_files,
            max_bytes=settings.tool_draft_max_bytes,
            durability=tool_drafts.Durability(tree=_fsync_tree, directory=_fsync_directory),
        )
    except Exception as exc:
        return tool_drafts.SaveResult(
            worth_saving=True,
            reason=tool_drafts.REASON_UNEXPECTED.format(kind=type(exc).__name__),
        )


def _close_unrecorded(
    context: _RoundDraft,
    saved: tool_drafts.SaveResult,
    outcome: tool_drafts.RoundOutcome,
    error: str | None,
) -> tool_drafts.SaveResult:
    """A continuing round that ran, once its end is known: recorded once.

    What its save recorded is not recorded again. A round nothing recorded yet --
    its workspace not confirmed, a strip that failed, a save that failed before
    it wrote, the save a publish follows that did not save -- is recorded on its
    own, its files not saved, why taken from ``saved``
    (``tool_drafts.record_round``). A first round has no draft to record in.
    Never raises.
    """
    begun = context.begun
    if begun is None or not saved.worth_saving or saved.recorded:
        return saved
    return tool_drafts.record_round(
        context.package_root,
        context.package_dir_identity,
        (begun["draft_id"], begun["generation"]),
        _draft_round(context, outcome, error),
        saved.reason,
    )


def _save_round_draft(
    context: _RoundDraft, outcome: tool_drafts.RoundOutcome | None, error: str | None
) -> tool_drafts.SaveResult:
    """Save the round's workspace as the tool's draft, and close the round; never raises.

    Blocking (runs via ``run_in_threadpool``). ``outcome`` is None for the save a
    publish is about to follow, which closes nothing yet. A save that kept the
    round recorded it, and a failed publish puts its error on that round
    (``tool_drafts.clear_publishing``); one that did not wrote nothing, and a
    continuation's round is then recorded once the publish's result is known
    (``_close_unrecorded``).
    """
    saved = _save_files(context, outcome, error)
    if outcome is None:
        return saved
    return _close_unrecorded(context, saved, outcome, error)


def _holding(saved: tool_drafts.SaveResult) -> bool:
    """Whether a round's staging must stay after its save: there was work worth
    keeping, and no draft is known to hold it -- the files not saved, or the
    manifest that names them not confirmed on disk."""
    return saved.worth_saving and not (saved.saved and saved.durable)


def _round_report(
    saved: tool_drafts.SaveResult,
    context: _RoundDraft,
    *,
    retained: str | None,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """The job's ``draft``: None if nothing was worth keeping.

    ``retained`` is where the work is kept besides the draft, when it is; the
    caller only names a place that was confirmed. A continuation whose round no
    write recorded still names its draft -- as ``begin_round`` left it.
    """
    if not saved.worth_saving:
        return None
    report = saved.report(retained_path=retained, reason=reason)
    begun = context.begun
    if report["draft_id"] is None and begun is not None:
        report.update(
            draft_id=begun["draft_id"],
            generation=begun["generation"],
            attempts=begun["attempt_count"],
        )
    return report


def _workspace_retained(
    context: _RoundDraft, saved: tool_drafts.SaveResult, holding: bool
) -> str | None:
    """The staging workspace's path for the job, when it is kept for the work in it.

    Not when the workspace itself could not be confirmed to be the staging
    directory it was given: what is at that path then is not known."""
    if not holding or saved.reason == tool_drafts.REASON_WORKSPACE:
        return None
    return str(context.build_root.path)


def _log_work_kept(session_root: Path) -> None:
    """Say in the backend log where a revise left work no draft is known to hold."""
    with contextlib.suppress(Exception):
        _logger.warning(
            "Builder workspace kept at %s: no draft is known to hold its work on disk.",
            session_root,
        )


def _log_work_kept_unstripped(session_root: Path) -> None:
    """Say in the backend log where a revise left work no draft is known to hold,
    in a workspace the builder's ``.env`` could not be stripped from."""
    with contextlib.suppress(Exception):
        _logger.warning(
            "Builder workspace kept at %s: no draft is known to hold its work on disk, "
            "and the .env the builder wrote could not be removed from it, so it may "
            "still be there. Take out what is needed, then delete the whole directory.",
            session_root,
        )


def _log_copy_running_kept(session_root: Path) -> None:
    """Say in the backend log where a revise that stopped while its copy into the
    staging still ran left that staging, which the copy may still be writing."""
    with contextlib.suppress(Exception):
        _logger.warning(
            "Builder workspace kept at %s: the revise stopped while the copy into it "
            "was still running, and that copy may still be writing it.",
            session_root,
        )


def _log_publish_running_kept(session_root: Path) -> None:
    """Say in the backend log where a job -- an install or a revise -- that stopped
    while its publish still ran left its staging, which that publish may still be
    writing."""
    with contextlib.suppress(Exception):
        _logger.warning(
            "Builder workspace kept at %s: the job stopped while its publish was "
            "still running, and that publish may still be writing it.",
            session_root,
        )


async def _masked_notes(
    result: ReviseResult | None,
) -> tuple[str | None, dict[str, Any], tools.OperatorFacingError | None]:
    """The model's summary and two notes, masked and capped again; or, when the known
    secrets cannot be had whole, none of them and the error saying so.

    The same choke point run_install uses (``_masked_and_capped``, each field with
    its own cap, so a secret registered since the first mask cannot carry it past
    the cap), and on a worker, since each mask scans the tools directory. The
    notes ride along for display only -- nothing branches on them.
    """
    if result is None:
        return None, {}, None
    try:
        summary = (
            await run_in_threadpool(_masked_and_capped, result.summary, _SUMMARY_CAP)
            if result.summary
            else None
        )
        notes = {
            "handoff": (
                await run_in_threadpool(_masked_and_capped, result.handoff, _HANDOFF_CAP)
                if result.handoff
                else None
            ),
            "operator_action": (
                await run_in_threadpool(
                    _masked_and_capped, result.operator_action, _OPERATOR_ACTION_CAP
                )
                if result.operator_action
                else None
            ),
        }
    except tools.OperatorFacingError as exc:
        return None, {}, exc
    return summary, notes, None


def _changed_since_base(
    version: Path, workspace: tool_drafts.TreeDigest | None, max_entries: int, max_bytes: int
) -> tuple[str, ...] | None:
    """The paths that differ between ``version`` and the workspace as the round
    received it, the version's own ``.afterthread.meta/`` left out; None when
    either side cannot be read whole -- never "no change"."""
    if workspace is None:
        return None
    base = tool_drafts.tree_digest(version, max_entries=max_entries, max_bytes=max_bytes)
    if base is None:
        return None
    meta = tools._META_DIRNAME
    return tuple(
        path
        for path in tool_drafts.changed_paths(base, workspace)
        if path != meta and not path.startswith(meta + "/")
    )


def _has_config_env(workspace: Path) -> bool:
    """Whether the workspace holds a ``config.env`` at its top, which a run never loads."""
    try:
        os.lstat(workspace / tools._CONFIG_FILENAME)
    except OSError:
        return False
    return True


async def run_revise(
    name: str,
    feedback: str | None,
    resolution: tools.Resolved | None,
    *,
    runs: tuple[AttachedRun, ...] = (),
    continuation: tool_drafts.Continuation | None = None,
) -> InstallOutcome:
    """Build and commit one new version, then publish ``current``.

    ``resolution`` is the exact answer whose vid the request compared, not a
    package name to resolve a second time after the background task starts. The
    initially resolved version supplies the build content and identity. The
    package toggle and ``.env`` remain package-layer state throughout.

    ``continuation`` is the draft the round continues, judged after the slot
    (``start_revise_job``): ``feedback`` is then this round's note and the
    draft's own ``feedback`` the request, and ``runs`` what the round carries --
    newly attached first, then what the draft kept. Such a round marks itself in
    the draft (``tool_drafts.begin_round``) and works on a copy of the draft's
    files (``tool_drafts.copy_into_workspace``), both before anything is spent.
    How it is closed follows what is known: one that never began its
    conversation takes its mark back and is not counted; one that ran, its
    commands and writes confirmed ended, is recorded once -- by its save, or on
    its own (``_close_unrecorded``); one whose commands or writes were not
    confirmed ended, or that ends in an exception, leaves the manifest alone, its
    mark for the next round to record as interrupted.

    A round that does not publish keeps its work as the tool's draft
    (``tool_drafts``), and a round about to publish saves it first, so a publish
    that fails cannot take the only copy of the work with it; a publish that goes
    through takes the draft away again. Every save comes after the exit for
    commands or writes not confirmed ended, which leaves the workspace untouched
    and waits on no worker, and after the builder's ``.env`` is stripped. A save
    that fails changes nothing about the round's outcome.

    The staging is removed only once someone holds the work: the draft, with its
    manifest confirmed on disk (and no publish worker still writing the staging);
    a version directory -- the one published, or the one a publish that failed
    part way had moved the work into, which ``current`` does not name; or no one
    needs to -- nothing was worth keeping. Every other end keeps it, a strip of
    the builder's ``.env`` that failed among them: a removal would most likely
    fail on that file the same way -- a workspace the builder made read-only
    fails both -- and so leave the ``.env`` while taking the rest of the work.
    Nor is it removed while the copy the round begins with -- of the version, or of
    the draft it continues -- may still be writing it: a round cancelled while that
    copy runs on its worker leaves the worker going on without it, so the staging is
    kept, and the backend log names it.
    Before the staging is kept, a ``.env`` nothing stripped yet is stripped; when
    that fails, the backend log says the ``.env`` may still be there. The job
    names where the work is when that place is known: the staging workspace, once
    confirmed to be the one the round was given, or that version directory.
    """
    base = tools.tools_dir()
    if base is None:
        return InstallOutcome(ok=False, error=_ERROR_TOOLS_DISABLED)

    if resolution is None:
        return InstallOutcome(ok=False, error=_ERROR_REVISE_NOT_FOUND)
    started_at = _now_iso()
    package_root = resolution.package_root
    current = await run_in_threadpool(tools.resolve_current, package_root)
    if isinstance(current, tools.Unresolved) or current.vid != resolution.vid:
        # The request legitimately queued work for V, but D21 permits the
        # operator to move ``current`` before this task is scheduled. Refuse
        # before copying files or spending an LLM call; following the name to P
        # would make the compared vid and the worked-on vid two spellings.
        return InstallOutcome(ok=False, error=_ERROR_REVISE_TARGET_REPLACED)
    # ONE worker hop does the read AND the dotenv parse (R4-3): the parse is real
    # work on a 64 KiB file and this job shares the loop with every request.
    # VALUES only: the package-layer FILE is never copied or rewritten by revise.
    # Empty values contribute nothing to redaction and are not registered,
    # matching tools._cached_env_values' own "a KEY= line contributes nothing"
    # rule.
    # R10-1: the identity of the package we are about to revise, taken BEFORE the
    # session and re-checked before publication. An operator can delete and reinstall
    # the tool during the minutes a build runs, and a name is not an identity.
    package_identity = await run_in_threadpool(_package_identity, resolution.version_root)
    if package_identity is None:
        # Uncertainty REFUSES here like everywhere else on this path (R11-2). A
        # package whose manifest we cannot even stat is either broken (the
        # resolver admits a directory with no tool.json) or momentarily
        # unreadable, and in both cases we would be starting a session we could
        # never safely publish: with no identity to compare, the pre-publication
        # check would have to either wave stale work through -- exactly the loss R10-1
        # closed -- or refuse after burning the whole build.
        return InstallOutcome(ok=False, error=_ERROR_REVISE_IDENTITY_UNKNOWN)
    # The package directory as this round finds it: the round's draft is written
    # into this directory and no other, never a same-name reinstall.
    package_dir_identity = await run_in_threadpool(
        tool_drafts.directory_identity, package_root.path
    )
    # BEFORE the LLM call, and before anything is registered: a value we would be
    # required to ignore, or one whose own assignment line spells it so that no
    # redactor can match what a ``cat`` of the file prints, cannot be protected by
    # registering it (see the docstring and ``_unmaskable_env_error``). The KEYS go
    # in too, because the spelling that has to vouch for a value is the one on the
    # line that DETERMINES it, never some other line or a comment that merely
    # repeats it (R6-1). ONE worker hop for the read, the parse and the policy
    # (R1-3/R4-3): a 64 KiB ``.env`` is a full walk plus thousands of substring
    # searches. The submit path already ran this same gate (web-v6 §5.2); it runs
    # again here because a queued job can start minutes after its submit, and
    # what counts is the files as they are when the session actually begins.
    revise_env, env_error = await run_in_threadpool(_revise_env_gate, package_root.path)
    if env_error is not None:
        return InstallOutcome(ok=False, error=env_error)
    assert revise_env is not None  # exactly one of the pair is set
    env_values = revise_env.env_values
    registered = [value for value in env_values.values() if value]
    for value in registered:
        tools.register_inflight_secret(value)

    session_root = base / _STAGING_DIRNAME / uuid4().hex
    build_root = tools.BuildRoot(session_root / "build")
    shell_root = session_root / "shell"
    job_id = _CURRENT_JOB_ID.get()
    # The release condition, atom by atom: the ``finally`` below removes the
    # staging only when the commands and writes are confirmed ended
    # (``shells_ended``), no publish worker may still be writing it (not
    # ``publish_running``), no worker copying into it may still be writing it (not
    # ``copy_running``), and no one is left to hold its work for (not ``holding``).
    # ``holding`` is raised when the commands and writes are confirmed ended and
    # lowered only by one of:
    #   * nothing worth keeping (``SaveResult.worth_saving`` False);
    #   * the draft holds the work: its files directory holds this round's files
    #     (``SaveResult.saved``) and the manifest naming them is on disk
    #     (``SaveResult.durable``) -- the two read together by ``_holding``;
    #   * published: the version ``current`` names holds the work;
    #   * moved out: a publish that failed part way moved the work out of this
    #     session's staging (``content`` not within ``session_root``).
    #
    # True until the session starts: an exit before it has no command or write to
    # wait for.
    shells_ended = True
    # Raised in the ``finally`` that gets the stop evidence of the commands and
    # writes, so an exception or a cancellation from there on finds it up. While it
    # is up, the ``finally`` below keeps the staging, stripping the builder's
    # ``.env`` first if nothing did yet, and keeping it all the same when that
    # strip fails.
    holding = False
    # Up once a strip of the builder's ``.env`` went through.
    env_stripped = False
    # Up while the publish's worker may still be writing the staging. The await
    # ending with the worker's result or with an ``Exception`` lowers it -- the
    # worker has returned, or never began -- and a cancellation does not, since
    # the worker goes on without it.
    publish_running = False
    # Up while the worker copying into the staging -- the version a first round
    # starts from, or the draft a continuation works on -- may still be writing it;
    # lowered as ``publish_running`` is, never on a cancellation.
    copy_running = False
    # Up once a BaseException that is not an Exception leaves the try below, set
    # and kept as run_install's is: the finally then keeps the holds on the .env
    # values until the process ends.
    cancelled = False
    try:
        settings = get_settings()
        max_files = settings.tool_draft_max_files
        max_bytes = settings.tool_draft_max_bytes
        begun: dict[str, Any] | None = None
        copied_out: tool_drafts.TreeDigest | None = None
        not_brought_in: tuple[tool_drafts.Omitted, ...] = ()
        if continuation is None:
            copy_running = True
            try:
                copy_error = await run_in_threadpool(
                    _copy_package_into_staging, resolution.version_root, build_root
                )
            except Exception:
                # The worker's own exception, or one before it began: either way
                # nothing of it writes into the staging any more.
                copy_running = False
                raise
            copy_running = False
            if copy_error is not None:
                return InstallOutcome(ok=False, error=copy_error)
        else:
            marked = await run_in_threadpool(
                tool_drafts.begin_round,
                package_root,
                package_dir_identity,
                continuation.expected,
                job_id,
                started_at,
            )
            if isinstance(marked, str):
                return InstallOutcome(
                    ok=False,
                    tool_name=name,
                    error=_ERROR_CONTINUE_NOT_STARTED.format(reason=marked),
                )
            begun = marked
            copy_running = True
            try:
                copied = await run_in_threadpool(
                    functools.partial(
                        tool_drafts.copy_into_workspace,
                        package_root=package_root,
                        package_dir_identity=package_dir_identity,
                        expected=(begun["draft_id"], begun["generation"]),
                        job_id=job_id,
                        workspace=build_root.path,
                        max_files=max_files,
                        max_bytes=max_bytes,
                    )
                )
            except Exception:
                # The worker's own exception, or one before it began: either way
                # nothing of it writes into the staging any more.
                copy_running = False
                raise
            copy_running = False
            if isinstance(copied, str):
                # Never began: its mark is taken back, and no round is counted.
                await run_in_threadpool(
                    tool_drafts.end_unstarted,
                    package_root,
                    package_dir_identity,
                    (begun["draft_id"], begun["generation"]),
                    job_id,
                )
                return InstallOutcome(
                    ok=False,
                    tool_name=name,
                    error=_ERROR_CONTINUE_NOT_COPIED.format(reason=copied),
                )
            copied_out, not_brought_in = copied.copied_out, copied.not_brought_in

        # The workspace as the round receives it, which "the round changed
        # something" is measured against when it ends.
        copied_in = await run_in_threadpool(
            functools.partial(
                tool_drafts.tree_digest,
                build_root.path,
                max_entries=max_files,
                max_bytes=max_bytes,
            )
        )
        # From the workspace: a continuation's draft may have changed it.
        manifest_text = await run_in_threadpool(_current_manifest_text, build_root.path)
        history: _History | None = None
        if begun is not None:
            changed = await run_in_threadpool(
                _changed_since_base, resolution.version_root.path, copied_in, max_files, max_bytes
            )
            history = _History(
                original=begun["feedback"],
                attempts=tuple(begun["attempts"]),
                not_brought_in=not_brought_in,
                changed=changed,
                base_vid=begun["base_vid"],
                workspace_config=await run_in_threadpool(_has_config_env, build_root.path),
            )
        # BOTH prompt builds are BLOCKING work, not the pure string joins they look
        # like (R1-3): each goes through ``tools.redact_known_secrets``, which calls
        # ``known_secret_values`` -- an ``iterdir`` of the whole tools directory plus
        # a ``stat`` per package and, on any cache miss, a full ``.env`` read. A
        # revise runs from a BACKGROUND job that shares this loop with every HTTP
        # request in the process, so both hop onto a worker for the same reason P3a
        # moved ``tool_meta._summary_user_prompt`` off it. A fail-closed redactor
        # failure still propagates out of the ``await`` unchanged, into ``_run_job``'s
        # backstop. The SYSTEM prompt joined them when its interpolated package name
        # became redactable -- it was a pure join until then. The user turn is
        # held to what the conversation budget leaves beside the system prompt.
        system_prompt = await run_in_threadpool(
            functools.partial(
                _revise_system_prompt,
                name,
                attempt=None if begun is None else begun["attempt_count"] + 1,
            )
        )
        config_values = revise_env.config_values
        user_prompt = await run_in_threadpool(
            functools.partial(
                _revise_user_prompt,
                feedback,
                manifest_text,
                revise_env.config_text,
                tuple(sorted(env_values)),
                tuple(sorted(set(config_values) & set(env_values))),
                runs=runs,
                workspace_vid=resolution.vid,
                system_chars=len(system_prompt),
                history=history,
            )
        )
        # The run_shell environment is layered exactly as ``tools._build_tool_env``
        # layers the runtime one -- config.env first, .env on top (web-v6 §4) -- so
        # the builder tests what the tool will actually run with.
        shell_env = {**config_values, **env_values}
        stats = StructuredRunStats()
        result: ReviseResult | None = None
        llm_error: str | None = None
        shells = _BuilderShells()
        shells_ended = False
        try:
            result = await generate_structured(
                system_prompt,
                user_prompt,
                ReviseResult,
                workflow=_WORKFLOW,
                # The whole existing .env goes into run_shell's environment (never
                # into a prompt), so the model can live-test the real API against
                # the tool's own credentials without the file being in staging.
                # config.env rides along under it; its TEXT is in the prompt above.
                tools=_build_meta_tools(
                    build_root.path, secret_env=shell_env or None, shells=shells
                ),
                max_tool_rounds=settings.tool_install_max_rounds,
                timeout_seconds=settings.tool_install_timeout_seconds,
                stats=stats,
            )
        except LLMNotConfiguredError:
            llm_error = "LLM 尚未設定，無法執行修訂。"  # noqa: RUF001
        except LLMUpstreamError as exc:
            # str(exc) is category + fixed reason by construction (see llm.py).
            llm_error = f"AI 修訂工具失敗（{exc}）。"  # noqa: RUF001
        finally:
            # The same stop-and-wait as run_install's: before anything below touches
            # the workspace and before the job gives up its slot.
            shells_ended = await shells.stop_and_wait()
            holding = shells_ended

        llm_log_id = llm_log.last_record_id_for_workflow(_WORKFLOW)
        # How the session ended as the loop measured it, on EVERY outcome from here
        # on -- a failed session's above all, since that is when the operator needs
        # to know whether a limit cut it short, and which one. ``max_rounds`` is the
        # budget the session was given, so the card can name the setting behind it.
        facts: dict[str, Any] = {
            "stopped_by": stats.finalized_by,
            "failure": stats.failure,
            "rounds_used": stats.tool_rounds_used,
            "max_rounds": settings.tool_install_max_rounds,
        }
        if not shells_ended:
            # Given up for good, and this exit waits on no worker thread
            # (``_BuilderShells``) -- not even for the masking below, so it carries
            # none of the model's words, and it writes nothing into the draft. The
            # AI 日誌 record keeps the whole session.
            return InstallOutcome(
                ok=False,
                tool_name=name,
                error=(llm_error or "") + _workspace_kept_error(session_root),
                llm_log_id=llm_log_id,
                **facts,
            )
        # The builder's .env goes first, before anything else reads or keeps the
        # workspace: whatever becomes of the staging from here on, it does not
        # hold that file.
        env_keys, env_strip_error = await run_in_threadpool(_strip_builder_env, build_root, base)
        env_stripped = env_strip_error is None
        # What the model handed back -- its summary and the two notes -- masked and
        # capped again, so every outcome below after a session that produced an
        # answer carries the model's own words, the .env strip refusal's included
        # when they could be masked. The LLM-failure outcome has no answer to carry.
        # The SAME single choke point run_install uses, and for the same reason:
        # the finally below discards the in-flight secrets before this function
        # returns, so redacting at job-update time would be a no-op that leaks.
        summary, notes, unmasked = await _masked_notes(result)
        round_draft = _RoundDraft(
            base=base,
            build_root=build_root,
            package_root=package_root,
            package_dir_identity=package_dir_identity,
            base_version=resolution.version_root,
            copied_in=copied_in,
            start=tool_drafts.DraftStart(base_vid=resolution.vid, feedback=feedback, runs=runs),
            job_id=job_id,
            started_at=started_at,
            facts=facts,
            summary=summary,
            handoff=notes.get("handoff"),
            operator_action=notes.get("operator_action"),
            begun=begun,
            copied_out=copied_out,
            note=feedback if begun is not None else None,
            not_brought_in=not_brought_in,
            attached=continuation.attached if continuation is not None else (),
        )
        if env_strip_error is not None:
            # A workspace that may still hold the builder's .env keeps no files in
            # the draft, and the staging stays all the same: a removal would most
            # likely fail on that file the same way, leaving the .env and taking the
            # rest of the work. The job names the workspace only when it is still the
            # staging it was given.
            closed = await run_in_threadpool(
                _close_unrecorded,
                round_draft,
                tool_drafts.SaveResult(worth_saving=True, reason=tool_drafts.REASON_STRIP_ENV),
                "failed",
                env_strip_error,
            )
            confirmed = await run_in_threadpool(_workspace_confirmed, build_root, base)
            return InstallOutcome(
                ok=False,
                tool_name=name,
                summary=summary,
                error=env_strip_error,
                llm_log_id=llm_log_id,
                env_keys=env_keys,
                draft=_round_report(
                    closed, round_draft, retained=str(build_root.path) if confirmed else None
                ),
                **facts,
                **notes,
            )
        if unmasked is not None:
            # The model's notes cannot be masked: the known secrets are not whole.
            # An end like any other, but one that writes nothing into the draft --
            # not without that list -- and keeps the staging, its .env stripped,
            # naming it only when it is still the staging it was given.
            confirmed = await run_in_threadpool(_workspace_confirmed, build_root, base)
            return InstallOutcome(
                ok=False,
                tool_name=name,
                error=str(unmasked),
                llm_log_id=llm_log_id,
                env_keys=env_keys,
                draft=_round_report(
                    tool_drafts.SaveResult(
                        worth_saving=True, reason=tool_drafts.REASON_NOTES_UNMASKED
                    ),
                    round_draft,
                    retained=str(build_root.path) if confirmed else None,
                ),
                **facts,
            )
        if llm_error is not None:
            kept = await run_in_threadpool(_save_round_draft, round_draft, "failed", llm_error)
            holding = _holding(kept)
            return InstallOutcome(
                ok=False,
                tool_name=name,
                error=llm_error,
                llm_log_id=llm_log_id,
                env_keys=env_keys,
                draft=_round_report(
                    kept, round_draft, retained=_workspace_retained(round_draft, kept, holding)
                ),
                **facts,
            )
        assert result is not None  # exactly one of result/llm_error is set above

        # ``tool_name`` on these outcomes is the package the user addressed, never
        # the model's answer: the job's tool_name is what the FE links to.
        if not result.ready:
            reason = summary or "（AI 未說明原因）"  # noqa: RUF001
            not_ready_error = f"AI 判定修訂尚未完成：{reason}"  # noqa: RUF001
            kept = await run_in_threadpool(
                _save_round_draft, round_draft, "not_published", not_ready_error
            )
            holding = _holding(kept)
            return InstallOutcome(
                ok=False,
                tool_name=name,
                summary=summary,
                error=not_ready_error,
                llm_log_id=llm_log_id,
                env_keys=env_keys,
                not_ready=True,
                draft=_round_report(
                    kept, round_draft, retained=_workspace_retained(round_draft, kept, holding)
                ),
                **facts,
                **notes,
            )
        if result.tool_name != name:
            # D40: ``InstallResult._ready_requires_valid_name`` only checks the
            # SHAPE of the name, never its identity. The destination is NOT at
            # risk -- ``_publish_revised_version`` is handed the typed
            # ``PackageRoot`` resolved at entry, so a renamed result cannot be
            # published anywhere else -- but a result that names a different tool
            # than the one requested breaks the contract every caller reads it
            # under, and describes files it did not build. Refuse, and say which name was
            # attempted so the operator can see what the model tried; the attempted
            # name is run through the redactor first, exactly as ``validate_package``
            # does with the offending path it names, because it is model-authored
            # text and a registered value CAN be a legal package name. On a worker
            # like the others (R1-3): rare is not the same as free, and leaving
            # one instance of the class behind is how it grows back.
            attempted = await run_in_threadpool(tools.redact_known_secrets, result.tool_name)
            rename_error = f"AI 試圖將工具改名為「{attempted}」，修訂已取消。"  # noqa: RUF001
            kept = await run_in_threadpool(_save_round_draft, round_draft, "failed", rename_error)
            holding = _holding(kept)
            return InstallOutcome(
                ok=False,
                tool_name=name,
                summary=summary,
                error=rename_error,
                llm_log_id=llm_log_id,
                env_keys=env_keys,
                draft=_round_report(
                    kept, round_draft, retained=_workspace_retained(round_draft, kept, holding)
                ),
                **facts,
                **notes,
            )

        # About to publish: the work is saved as a draft first -- always as a new
        # copy, which the removal after the publish compares against -- and the
        # manifest then marks it as being published from. A mark that cannot be
        # written does not stop the publish; it only leaves a crash right after
        # the publish to be read as a stale draft rather than a published one. A
        # continuation whose save kept nothing writes no mark: the draft is then
        # not what is being published. Nor has it recorded the round: that is done
        # once the publish's result is known (``_close_unrecorded``), and an
        # exception in the publish leaves the round's mark for the next round to
        # record.
        kept = await run_in_threadpool(_save_round_draft, round_draft, None, None)
        holding = _holding(kept)
        drafted: tuple[str, int] | None = None
        draft_line: str | None = None
        if kept.saved and kept.draft_id is not None and kept.generation is not None:
            marked_generation = await run_in_threadpool(
                tool_drafts.mark_publishing,
                package_root,
                package_dir_identity,
                (kept.draft_id, kept.generation),
                round_draft.job_id,
            )
            drafted = (
                kept.draft_id,
                kept.generation if marked_generation is None else marked_generation,
            )
            draft_line = tool_drafts.provenance_line(kept.draft_id, kept.attempts or 1)
        # The request the version records: a continuation's is its draft's, with
        # every round's note the draft still keeps, this round's last.
        request = feedback
        round_notes: tuple[tuple[int, str], ...] = ()
        if begun is not None:
            request = begun["feedback"]
            round_notes = tuple(
                (attempt["number"], attempt["note"])
                for attempt in begun["attempts"]
                if attempt["note"]
            )
            if feedback:
                round_notes += ((begun["attempt_count"] + 1, feedback),)
        published: _PublishedRevision | None = None
        promote_error: str | None
        content: Path = build_root.path
        try:
            origin_feedback = await run_in_threadpool(
                _provenance_feedback, request, runs, draft_line, round_notes
            )
        except Exception as exc:
            # The masking fails closed (``tools.redact_known_secrets``): a publish
            # that failed before it began, the work where the round left it.
            promote_error = (
                _ERROR_ORIGIN_MASK_INCOMPLETE.format(detail=exc)
                if isinstance(exc, tools.KnownSecretsIncomplete)
                else _ERROR_ORIGIN_MASK.format(kind=type(exc).__name__)
            )
        else:
            # The publisher reads immutable provenance from the version whose
            # identity was captured before the builder session.
            publish_running = True
            try:
                published, promote_error, content = await run_in_threadpool(
                    _publish_revised_version,
                    build_root,
                    shell_root,
                    package_root,
                    resolution,
                    package_identity,
                    origin_feedback,
                )
            except Exception:
                # The worker's own exception, or one before it began: either way
                # nothing of it writes into the staging any more.
                publish_running = False
                raise
            publish_running = False
        if promote_error is not None:
            # The work is where the publish left it -- this session's staging, or a
            # version ``current`` does not name -- and the draft holds it only when
            # the save before the publish kept it and is known to be on disk. Where
            # it is, the job says, unless the publish could not confirm the
            # workspace it was given.
            elsewhere = _holding(kept)
            holding = elsewhere and tools._is_within(session_root, content)
            confirmed = (
                kept.reason != tool_drafts.REASON_WORKSPACE
                and promote_error not in _STAGING_REFUSALS
            )
            retained = str(content) if elsewhere and confirmed else None
            failed_draft: dict[str, Any] | None = None
            if drafted is not None:
                # What was just saved is the work again: ``publishing`` cleared,
                # the failure recorded on the round.
                cleared = await run_in_threadpool(
                    tool_drafts.clear_publishing,
                    package_root,
                    package_dir_identity,
                    drafted,
                    promote_error,
                )
                failed_draft = dataclasses.replace(
                    kept, generation=drafted[1] if cleared is None else cleared
                ).report(retained_path=retained)
            elif kept.worth_saving:
                closed = await run_in_threadpool(
                    _close_unrecorded, round_draft, kept, "failed", promote_error
                )
                failed_draft = _round_report(closed, round_draft, retained=retained)
            return InstallOutcome(
                ok=False,
                tool_name=name,
                summary=summary,
                error=promote_error,
                llm_log_id=llm_log_id,
                env_keys=env_keys,
                draft=failed_draft,
                **facts,
                **notes,
            )
        assert published is not None
        # Published: the version holds the work.
        holding = False
        published_draft: dict[str, Any] | None = None
        if drafted is not None:
            # Compared against the copy the save made, which is all the draft may
            # hold to be taken away.
            removal = await run_in_threadpool(
                tool_drafts.remove_after_publish,
                package_root,
                package_dir_identity,
                drafted,
                kept.copy,
            )
            if not removal.removed:
                # The generation the publish marked, which is what a discard names.
                published_draft = dataclasses.replace(kept, generation=drafted[1]).report(
                    reason=(
                        _DRAFT_NOT_REMOVED
                        if removal.kept is None
                        else _DRAFT_LEFT_REASONS[removal.kept]
                    )
                )
        elif begun is not None and kept.worth_saving:
            # The draft is as the round found it, and stays: say so, naming it.
            closed = await run_in_threadpool(
                _close_unrecorded, round_draft, kept, "published", None
            )
            published_draft = _round_report(
                closed, round_draft, retained=None, reason=_DRAFT_ROUND_NOT_SAVED
            )
        # The revision is LIVE as of the line above; everything after it is
        # decoration and cannot fail the outcome (``generate_and_store_summary``
        # never raises -- see tool_meta). The sidecar was excluded from staging, so
        # the package has NO summary until this regenerates one: the same
        # briefly-absent-sidecar window D40 already accepts right after an install,
        # and the reason the publisher's typed Resolution and origin are carried
        # across rather than dropping back to a name/current lookup. This exact
        # published vid is the one summarized even if D21 moves current meanwhile.
        await tool_meta.generate_and_store_summary(
            published.resolution,
            origin=published.origin,
            builder_summary=summary,
        )
        verification = await _verify_published(name, published.resolution.vid)
        return InstallOutcome(
            ok=True,
            tool_name=name,
            summary=summary,
            llm_log_id=llm_log_id,
            env_keys=env_keys,
            verification=verification,
            draft=published_draft,
            **facts,
            **notes,
        )
    except BaseException as exc:
        cancelled = not isinstance(exc, Exception)
        raise
    finally:
        # The holds on the .env values go first, on a return or an Exception only,
        # as run_install's does: every worker that took the known secrets after they
        # were registered to make something seen or written -- the copy of a draft,
        # the prompts, the masks, the save, the record, the marks of the manifest,
        # the publish, the summary's store, the examples -- was then awaited to its
        # result or to its own exception, or never began. Ended any other way --
        # cancelled, interrupted, exiting -- the round keeps them until the process
        # ends: a worker it handed a copy, a save, a record, a mark, a publish or the
        # summary's store to goes on without it and may still take the known
        # secrets, and the package's .env may no longer hold a value the round was
        # given -- the operator may have changed it while the round ran.
        #
        # Cleanup targets the complete session root, including any unused shell --
        # unless the session's commands or writes could not be confirmed stopped,
        # which keeps it all, unstripped, or the work in it is held by no one, or a
        # worker copying into it or publishing it may still be writing it, any of
        # which keeps it too while the sweep still runs (it never looks inside
        # ``.staging``). Kept for the work, it is first stripped of a .env the
        # builder wrote, when nothing stripped it yet, and kept all the same when
        # that strip fails: the log then says the .env may still be there.
        if not cancelled:
            for value in registered:
                tools.discard_inflight_secret(value)
        if not shells_ended:
            # Kept in place: a command or a write not confirmed stopped may still be
            # using it.
            _log_workspace_kept(session_root)
        else:
            if holding and not env_stripped and not publish_running:
                _late_keys, late_error = await run_in_threadpool(
                    _strip_builder_env, build_root, base
                )
                env_stripped = late_error is None
            if holding or publish_running or copy_running:
                if copy_running:
                    _log_copy_running_kept(session_root)
                elif publish_running:
                    _log_publish_running_kept(session_root)
                elif env_stripped:
                    _log_work_kept(session_root)
                else:
                    _log_work_kept_unstripped(session_root)
                await run_in_threadpool(_sweep_logged, base)
            else:
                await run_in_threadpool(_cleanup_staging, session_root, base)


# --- publishing a draft as it is, without a model ------------------------------

# Why a publish of a draft as it is published nothing and left the draft as it
# was: ``{reason}`` is the words of what stopped it, last since it may be sentences.
_ERROR_PUBLISH_DRAFT_NOT_STARTED = "沒有發布草稿，草稿沒有變動：{reason}"  # noqa: RUF001
# The draft's base is no longer ``current`` when the job begins.
_ERROR_PUBLISH_DRAFT_STALE = "工具目前的版本已不是草稿的基底（草稿已過期）"  # noqa: RUF001
_ERROR_PUBLISH_DRAFT_IDENTITY = "無法確認原工具的內容（`tool.json` 讀取失敗）"  # noqa: RUF001
_ERROR_PUBLISH_DRAFT_NOT_MARKED = "無法在草稿寫入「正在發布」的標記"
# The mark was written, but the write is not confirmed on disk: nothing is
# published, and the job takes its own mark back.
_ERROR_PUBLISH_DRAFT_MARK_UNCONFIRMED = (
    "無法確認草稿的「正在發布」標記已安全寫入磁碟，所以沒有發布。"  # noqa: RUF001
)
# The publisher answered with a failure, ``current`` not moved.
_ERROR_PUBLISH_DRAFT_FAILED = "草稿沒有發布：{reason}"  # noqa: RUF001
# Three of the publisher's own refusals speak of a revise and of sending a request
# again; a publish of a draft has neither, so these say what it does have.
_PUBLISH_DRAFT_REWORDED = {
    _ERROR_REVISE_TARGET_MISSING: "原工具已被刪除。",
    _ERROR_REVISE_TARGET_ALIAS: "原工具目錄已被替換為連結。",
    _ERROR_REVISE_TARGET_REPLACED: (
        "原工具在發布期間被改動或重新安裝（目前的版本已換，或它的 tool.json 被改過），"  # noqa: RUF001
        "請重新整理後確認草稿的狀態。"
    ),
}
# Why a published draft stays when its workspace did not receive all of it as it
# was: the draft holds what the version does not. The paths left out follow it
# when there are any (``tool_drafts.PublishCopy.left_out``). The other reasons a
# published draft stays are the revise's own: ``_DRAFT_LEFT_REASONS`` -- it holds
# more, or another copy, or that could not be told -- and ``_DRAFT_NOT_REMOVED``.
_DRAFT_KEPT_NOT_ALL_PUBLISHED = (
    "已發布，但這次發布的內容與草稿不完全相同（有項目沒帶進發布、連結改寫成相對路徑、"  # noqa: RUF001
    "權限位元不同，或帶出時草稿正在變動），因此保留草稿。"  # noqa: RUF001
    "需要的話先從草稿目錄取出，再到工具列放棄這份草稿。"  # noqa: RUF001
)
# The job's own mark could not be taken back after it published nothing.
_DRAFT_MARK_LEFT = (
    "沒能清除草稿的「正在發布」標記：工具列會把這份草稿顯示為結果未知，只能放棄；"  # noqa: RUF001
    "放棄前可以先從草稿目錄取出需要的內容。"
)


def _draft_publish_report(
    draft_id: str,
    generation: int,
    *,
    removed: bool = False,
    reason: str | None = None,
    retained: str | None = None,
) -> dict[str, Any]:
    """The job's ``draft_publish``: the draft, at the generation the job left it;
    whether ``draft/`` was seen gone after the publish; why the draft stays, or what
    it was left with when it is not as it was; and where the copy the job tried to
    publish is kept, when no draft is known to hold it."""
    return {
        "draft_id": draft_id,
        "generation": generation,
        "removed": removed,
        "reason": reason,
        "retained_path": retained,
    }


def _publish_draft_failed(
    name: str,
    error: str,
    draft: tuple[str, int],
    *,
    marked: bool = False,
    unmarked: int | None = None,
    retained: str | None = None,
) -> InstallOutcome:
    """A publish of a draft that published nothing. ``draft`` names the draft and a
    generation: as judged, when the job wrote nothing to it; otherwise the one its
    mark wrote (``marked``), which the write taking that mark back moves on to
    ``unmarked`` -- None when there was none, and then the mark stays."""
    draft_id, generation = draft
    reason: str | None = None
    if marked:
        if unmarked is None:
            reason = _DRAFT_MARK_LEFT
        else:
            generation = unmarked
    return InstallOutcome(
        ok=False,
        tool_name=name,
        error=error,
        draft_publish=_draft_publish_report(draft_id, generation, reason=reason, retained=retained),
    )


def _log_publish_draft_kept(session_root: Path, *, worker: bool) -> None:
    """Say in the backend log where a publish of a draft left its staging, and why:
    a worker copying the draft into it or publishing it may still be writing it, or
    the draft is not known to hold what it holds."""
    with contextlib.suppress(Exception):
        if worker:
            _logger.warning(
                "Draft publish workspace kept at %s: a worker copying the draft into it "
                "or publishing it may still be writing it.",
                session_root,
            )
        else:
            _logger.warning(
                "Draft publish workspace kept at %s: the draft is not known to hold what it holds.",
                session_root,
            )


async def run_publish_draft(
    name: str, resolution: tools.Resolved, judged: dict[str, Any]
) -> InstallOutcome:
    """Publish the tool's draft as it is: no model, no round, no summary.

    ``resolution`` is the version the request compared, and ``judged`` the draft's
    manifest as ``tool_drafts.judge_draft`` found it after the slot: the draft the
    operator named, continuable, based on ``current``. In order:

    1. the draft's files are copied into a new staging workspace, every fact of the
       copy handed back (``tool_drafts.copy_for_publish``); a copy that found a
       known secret outside the root entries the strip before a save removes, or
       left an entry out because a look at it failed, is not published;
    2. the version's ``origin.feedback`` is made: the draft's request, the notes of
       its rounds, and last, whole, the line naming the draft as published by the
       operator without a model (``tool_drafts.operator_publish_line``), by which
       the tools list reconciles a ``publishing`` mark;
    3. the draft is marked as being published from (``tool_drafts.mark_publishing``)
       and only a mark confirmed on disk lets the publish go on: one that is not is
       taken back, and nothing is published;
    4. the publish (``_publish_revised_version``): the root metadata stripped, the
       content validated, the version committed, ``current`` moved;
    5. published, the draft goes only when the workspace received all of it as it
       was -- ``copied_out`` equal to ``copied_in``, checked here -- and ``draft/``
       holds that directory, unchanged since, and the manifest alone
       (``tool_drafts.remove_after_publish``); otherwise it stays, its mark too,
       and the job says why. The examples then run (``_verify_published``). Not
       published, the publisher having answered, the job takes its own mark back
       (``tool_drafts.unmark_publishing``), the rounds left as they were.

    Nothing asks a model: no builder session, no summary -- the new version has
    none until one is made on request. Every stop before the mark leaves the
    draft's manifest as it was. An exception or a cancellation takes no mark back:
    whether the publish went through is not known then.
    """
    expected: tuple[str, int] = (judged["draft_id"], judged["generation"])
    draft_id = expected[0]
    base = tools.tools_dir()
    if base is None:
        return _publish_draft_failed(
            name, _ERROR_PUBLISH_DRAFT_NOT_STARTED.format(reason=_ERROR_TOOLS_DISABLED), expected
        )
    package_root = resolution.package_root
    current = await run_in_threadpool(tools.resolve_current, package_root)
    if isinstance(current, tools.Unresolved) or current.vid != resolution.vid:
        return _publish_draft_failed(
            name,
            _ERROR_PUBLISH_DRAFT_NOT_STARTED.format(reason=_ERROR_PUBLISH_DRAFT_STALE),
            expected,
        )
    package_identity = await run_in_threadpool(_package_identity, resolution.version_root)
    if package_identity is None:
        return _publish_draft_failed(
            name,
            _ERROR_PUBLISH_DRAFT_NOT_STARTED.format(reason=_ERROR_PUBLISH_DRAFT_IDENTITY),
            expected,
        )
    package_dir_identity = await run_in_threadpool(
        tool_drafts.directory_identity, package_root.path
    )
    settings = get_settings()
    max_files = settings.tool_draft_max_files
    max_bytes = settings.tool_draft_max_bytes
    session_root = base / _STAGING_DIRNAME / uuid4().hex
    build_root = tools.BuildRoot(session_root / "build")
    shell_root = session_root / "shell"
    job_id = _CURRENT_JOB_ID.get()
    # The release condition, atom by atom: the ``finally`` below removes the
    # staging only when no worker may still be writing it -- neither the one copying
    # the draft in (not ``copy_running``) nor the one publishing it (not
    # ``publish_running``) -- and it holds nothing of the work alone (not
    # ``holding``). A worker's flag goes up just before it is awaited and comes down
    # when the await ends with the worker's result or with an ``Exception``, never on
    # a cancellation: the worker goes on without the coroutine. ``holding`` goes up
    # just before the publish starts, the staging being from then on what is
    # published, and comes down only when the version ``current`` names holds the
    # work (published), when the publish moved the work out of this session's
    # staging (``content`` not within ``session_root``), or when the draft is seen to
    # hold what the staging does (``tool_drafts.holds_copy``). Before the publish the
    # staging is a copy of the draft, which the copy only read.
    copy_running = False
    publish_running = False
    holding = False
    try:
        copy_running = True
        try:
            copied = await run_in_threadpool(
                functools.partial(
                    tool_drafts.copy_for_publish,
                    package_root=package_root,
                    package_dir_identity=package_dir_identity,
                    expected=expected,
                    workspace=build_root.path,
                    max_files=max_files,
                    max_bytes=max_bytes,
                )
            )
        except Exception:
            # The worker's own exception, or one before it began: either way it
            # writes the staging no more.
            copy_running = False
            raise
        copy_running = False
        if isinstance(copied, str):
            return _publish_draft_failed(
                name, _ERROR_PUBLISH_DRAFT_NOT_STARTED.format(reason=copied), expected
            )
        if copied.refusal is not None:
            return _publish_draft_failed(
                name, _ERROR_PUBLISH_DRAFT_NOT_STARTED.format(reason=copied.refusal), expected
            )
        # Made before the mark: a mask that fails stops the publish with nothing
        # written to the draft.
        notes = tuple(
            (attempt["number"], attempt["note"])
            for attempt in judged["attempts"]
            if attempt["note"]
        )
        try:
            origin_feedback = await run_in_threadpool(
                _provenance_feedback,
                judged["feedback"],
                (),
                tool_drafts.operator_publish_line(draft_id),
                notes,
            )
        except Exception as exc:
            unmasked = (
                _ERROR_ORIGIN_MASK_INCOMPLETE.format(detail=exc)
                if isinstance(exc, tools.KnownSecretsIncomplete)
                else _ERROR_ORIGIN_MASK.format(kind=type(exc).__name__)
            )
            return _publish_draft_failed(
                name, _ERROR_PUBLISH_DRAFT_NOT_STARTED.format(reason=unmasked), expected
            )
        durable: list[bool] = []
        marked = await run_in_threadpool(
            functools.partial(
                tool_drafts.mark_publishing,
                package_root,
                package_dir_identity,
                expected,
                job_id,
                durable_out=durable,
            )
        )
        if marked is None:
            return _publish_draft_failed(
                name,
                _ERROR_PUBLISH_DRAFT_NOT_STARTED.format(reason=_ERROR_PUBLISH_DRAFT_NOT_MARKED),
                expected,
            )
        marked_as = (draft_id, marked)
        if durable != [True]:
            # Known not to have published: the publisher was never called. The mark
            # is this job's own, so it is taken back; that write proves nothing
            # about the one before it.
            unmarked = await run_in_threadpool(
                tool_drafts.unmark_publishing, package_root, package_dir_identity, marked_as, job_id
            )
            return _publish_draft_failed(
                name,
                _ERROR_PUBLISH_DRAFT_MARK_UNCONFIRMED,
                marked_as,
                marked=True,
                unmarked=unmarked,
            )
        holding = True
        publish_running = True
        try:
            published, promote_error, content = await run_in_threadpool(
                _publish_revised_version,
                build_root,
                shell_root,
                package_root,
                resolution,
                package_identity,
                origin_feedback,
            )
        except Exception:
            # The worker's own exception, or one before it began: either way it
            # writes the staging no more.
            publish_running = False
            raise
        publish_running = False
        if promote_error is not None:
            retained: str | None = None
            if not tools._is_within(session_root, content):
                # Moved into ``versions/`` before it failed: that version directory
                # holds the work, which ``current`` does not name, and it stays.
                holding = False
                retained = str(content)
            else:
                held = await run_in_threadpool(
                    tool_drafts.holds_copy,
                    package_root,
                    package_dir_identity,
                    draft_id,
                    copied,
                    max_files,
                    max_bytes,
                )
                holding = not held
                if holding and promote_error not in _STAGING_REFUSALS:
                    retained = str(content)
            unmarked = await run_in_threadpool(
                tool_drafts.unmark_publishing, package_root, package_dir_identity, marked_as, job_id
            )
            return _publish_draft_failed(
                name,
                _ERROR_PUBLISH_DRAFT_FAILED.format(
                    reason=_PUBLISH_DRAFT_REWORDED.get(promote_error, promote_error)
                ),
                marked_as,
                marked=True,
                unmarked=unmarked,
                retained=retained,
            )
        assert published is not None
        # Published: the version ``current`` names holds the work.
        holding = False
        removed = False
        reason: str | None = None
        if copied.copied_out is None or copied.copied_in is None:
            reason = _DRAFT_KEPT_UNCONFIRMED
        elif not tool_drafts.same_tree(copied.copied_out, copied.copied_in):
            reason = _DRAFT_KEPT_NOT_ALL_PUBLISHED + (copied.left_out or "")
        else:
            removal = await run_in_threadpool(
                tool_drafts.remove_after_publish,
                package_root,
                package_dir_identity,
                marked_as,
                tool_drafts.SavedCopy(copied.files_dir, copied.copied_out, max_files, max_bytes),
            )
            removed = removal.removed
            if not removed:
                reason = (
                    _DRAFT_NOT_REMOVED
                    if removal.kept is None
                    else _DRAFT_LEFT_REASONS[removal.kept]
                )
        verification = await _verify_published(name, published.resolution.vid)
        return InstallOutcome(
            ok=True,
            tool_name=name,
            verification=verification,
            draft_publish=_draft_publish_report(draft_id, marked, removed=removed, reason=reason),
        )
    finally:
        if copy_running or publish_running or holding:
            _log_publish_draft_kept(session_root, worker=copy_running or publish_running)
            await run_in_threadpool(_sweep_logged, base)
        else:
            await run_in_threadpool(_cleanup_staging, session_root, base)


# --- examples: verification and the health check (web-v6 §8.3, §10) --------

_WORKFLOW_VERIFY = "verify"
_WORKFLOW_HEALTH_CHECK = "health_check"


async def _name_secrets() -> list[str] | None:
    """The secrets example names are masked against, or None when the scan fails.

    Scanned on a worker (P5 review r1, J5): the scan walks every package's
    ``.env``, and nothing that touches the filesystem runs on the event loop.
    """
    try:
        return await run_in_threadpool(tools._redaction_secrets)
    except Exception:
        return None


def _verification_dict(
    result: tools.CheckResult, *, finished: bool, secrets: list[str] | None
) -> dict[str, Any]:
    """A check's result as the job reports it: per example, plus the totals.

    Example names are builder-written version content shown to the operator, so
    they pass the redactor like every other such text -- masked, then cut
    (``tools.example_display_name``). ``secrets`` comes from ``_name_secrets``;
    a failed scan (None) must not lose the result, so it costs the names instead.
    """
    runs = []
    for index, example in enumerate(result.results, start=1):
        name = (
            tools.example_display_name(example.name, secrets)
            if secrets is not None
            else f"範例 {index}"
        )
        runs.append({"name": name, "outcome": example.outcome, "run_id": example.run_id})
    not_started = {tools.EXAMPLE_SKIPPED, tools.EXAMPLE_NOT_RUN, tools.EXAMPLE_STOPPED}
    return {
        "status": result.status,
        "message": result.message,
        "vid": result.vid,
        "runs": runs,
        "completed": sum(1 for example in result.results if example.outcome not in not_started),
        "total": result.total,
        "budget_exhausted": result.budget_exhausted,
        "finished": finished,
    }


async def _verify_published(name: str, vid: str) -> dict[str, Any]:
    """Run the just-published version's examples, the fix's own verification.

    Only the version's examples.json is run -- never the inputs attached to the
    fix, which the builder alone replays inside its session (web-v6 §8.5). A
    tool switched off is not run: the execution path refuses it, so the result
    says it was not verified. Labelled with this job's id as ``check_id`` and
    reported on the job as each example finishes.
    """
    job_id = _CURRENT_JOB_ID.get()
    # Progress is reported from the loop as each example ends, so its names are
    # masked against one scan taken up front; the final report scans again.
    progress_secrets = await _name_secrets()

    def _progress(partial: tools.CheckResult) -> None:
        if job_id is not None:
            _update_job(
                job_id,
                verification=_verification_dict(partial, finished=False, secrets=progress_secrets),
            )

    try:
        result = await tools.run_examples(
            name,
            check_id=job_id or uuid4().hex,
            workflow=_WORKFLOW_VERIFY,
            budget_seconds=get_settings().tool_check_budget_seconds,
            expected_vid=vid,
            on_progress=_progress,
        )
    except Exception as exc:
        # The fix is published; a verification that could not even run must
        # not turn that into a failure. It says what happened instead.
        _logger.warning("Verification of %s not run: %s", name, type(exc).__name__)
        result = tools.CheckResult(status="error", vid=vid)
    return _verification_dict(result, finished=True, secrets=await _name_secrets())


# Why a health check cannot start, by the reason ``tools.checkable_tool`` or the
# examples file gives. The operator reads these on the submit, so they say what
# to do.
_CHECK_REFUSALS = {
    "no_examples": "這個版本沒有 examples.json，沒有範例可以跑。修訂時 AI 會建立它。",  # noqa: RUF001
    "disabled": "工具已停用：執行路徑不會執行停用的工具。啟用後再跑。",  # noqa: RUF001
    "invalid": "工具目前的版本不是可執行的工具包。",
    # The two a check can only meet AFTER admission (P5 review r1, J2): the
    # version moved between the submit and the run, or the package went away.
    "version_changed": "送出之後工具換了版本，這次沒有跑範例。請重新整理後再跑。",  # noqa: RUF001
    "not_found": "找不到這個工具（可能已被刪除），沒有跑範例。",  # noqa: RUF001
}


@dataclass(frozen=True, slots=True)
class CheckJobStart:
    """A health-check submit: the job id, or a refusal (and its message)."""

    job_id: str | None = None
    refusal: str | None = None
    message: str | None = None


async def start_health_check_job(name: str, expected_vid: str) -> CheckJobStart:
    """Take the global slot, check the version and its examples, queue the run.

    A health check is a job like an install or a revise (web-v6 §10): it holds
    the single flight -- so every version write is refused meanwhile -- and the
    shared tools lock while it runs. ``expected_vid`` is compared after the slot
    is taken, the order every version-specific write keeps (invariant K). A
    version with no usable examples, or a tool that is off, is refused here, so a
    job never exists that could only report that nothing ran.
    """
    reservation = reserve_sync_operation()
    if reservation is None:
        return CheckJobStart(refusal="job_busy")
    try:
        package_root = await run_in_threadpool(tools._resolve_package_dir_no_alias, name)
        if package_root is None:
            return CheckJobStart(refusal="not_found")
        resolution = await run_in_threadpool(tools.resolve_current, package_root)
        if isinstance(resolution, tools.Unresolved):
            return CheckJobStart(refusal="not_found")
        if resolution.vid != expected_vid:
            return CheckJobStart(refusal="version_mismatch")
        found = await run_in_threadpool(tools.checkable_tool, name)
        if isinstance(found, str):
            refusal = "not_found" if found == "not_found" else "check_unavailable"
            return CheckJobStart(refusal=refusal, message=_CHECK_REFUSALS.get(found))
        loaded = await run_in_threadpool(tools.load_examples, resolution.version_root)
        if loaded.error is not None:
            return CheckJobStart(refusal="check_unavailable", message=loaded.error)
        if not loaded.examples:
            return CheckJobStart(
                refusal="check_unavailable", message=_CHECK_REFUSALS["no_examples"]
            )
        job = _admit_job(reservation_token=reservation, kind="health_check", tool_name=name)
        if job is None:
            return CheckJobStart(refusal="job_busy")
        reservation = ""
        task = asyncio.create_task(_run_check_job(job.job_id, name, expected_vid))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        return CheckJobStart(job_id=job.job_id)
    finally:
        if reservation:
            release_sync_operation(reservation)


async def _run_check_job(job_id: str, name: str, expected_vid: str) -> None:
    """The health check's task body: run the examples, report as they finish."""
    _update_job(job_id, state="running", tool_name=name)
    progress_secrets = await _name_secrets()

    def _progress(partial: tools.CheckResult) -> None:
        _update_job(
            job_id,
            verification=_verification_dict(partial, finished=False, secrets=progress_secrets),
        )

    try:
        result = await tools.run_examples(
            name,
            check_id=job_id,
            workflow=_WORKFLOW_HEALTH_CHECK,
            budget_seconds=get_settings().tool_check_budget_seconds,
            expected_vid=expected_vid,
            on_progress=_progress,
        )
    except Exception as exc:
        _update_job(
            job_id,
            state="failed",
            error=f"跑範例時發生未預期錯誤（{type(exc).__name__}）。",  # noqa: RUF001
            outcome=_OUTCOME_FAILED,
            finished_at=_now_iso(),
        )
        return
    ran = result.status == "ran"
    _update_job(
        job_id,
        state="succeeded" if ran else "failed",
        error=None
        if ran
        else (result.message or _CHECK_REFUSALS.get(result.status, "沒有跑範例。")),
        outcome=None if ran else _OUTCOME_FAILED,
        verification=_verification_dict(result, finished=True, secrets=await _name_secrets()),
        finished_at=_now_iso(),
    )


# --- background jobs ---------------------------------------------------------

# In-memory, process-local, deliberately unpersisted (see the module
# docstring). One lock guards the dict, matching llm_log's pattern; the task
# set only holds strong references so a running job's Task is never
# garbage-collected mid-flight (asyncio keeps only weak refs to tasks). Since
# admission allows only ONE active job at a time (M7, widened to install AND
# revise by D40), _TASKS holds at most that one in-flight task plus any
# not-yet-collected finished ones -- it cannot grow without bound under rapid
# submits.
_MAX_JOBS = 20
_JOBS: dict[str, InstallJob] = {}
# The job whose run is executing in this context, set by ``_run_job``: a revise
# reads it to label and report the verification it runs after publishing.
_CURRENT_JOB_ID: ContextVar[str | None] = ContextVar("_CURRENT_JOB_ID", default=None)
_JOBS_LOCK = threading.Lock()
_TASKS: set[asyncio.Task[None]] = set()

# The SYNCHRONOUS side of the same single-flight (R7-3). A summary regenerate is
# not a job -- it lives inside one request -- but it spans a full LLM round trip
# during which it reads the request-validated version and then writes THAT
# version's ``summary.json``, so it occupies the admission domain for the same
# reason a job does: a revise let through meanwhile publishes a newer version and
# moves ``current``, leaving this paid-for summary on one no longer live. A token
# per holder rather than a flag: release names the reservation it took, so it can
# never drop somebody else's, and the set needs no ``global`` to mutate.
_SYNC_OPS: set[str] = set()

# The zh-TW noun each job kind uses in the "unexpected error" backstop below.
# Parameterizing the NOUN rather than the whole sentence keeps ONE backstop with
# one shape for every kind that runs through it, while leaving the install's
# existing text (which its tests and the FE see) byte-identical.
_ACTION_INSTALL = "安裝"
_ACTION_REVISE = "修訂"
_ACTION_PUBLISH_DRAFT = "發布草稿"


@dataclass(slots=True)
class InstallJob:
    """One tool job's visible state, as polled by the FE.

    Every kind shares this record and the table it lives in, and the FE polls one
    endpoint for all of them; ``kind`` says which one a record is, for the page to
    read it the way that kind needs.
    """

    job_id: str
    state: str  # queued | running | succeeded | failed
    created_at: str
    # install | revise | health_check | publish_draft. An install and a revise share
    # every field; a health check runs a tool's examples and builds nothing; a
    # publish of a draft as it is publishes the draft's files with no session, and
    # says what became of the draft in ``draft_publish``.
    kind: str = "install"
    finished_at: str | None = None
    error: str | None = None
    tool_name: str | None = None
    summary: str | None = None
    llm_log_id: int | None = None
    env_keys: tuple[str, ...] = ()
    # How a TERMINAL job ended, finer than ``state`` (web-v6 §8.3):
    # ``published`` | ``not_published`` (the model's own ready=false) | ``failed``.
    # None while queued/running. ``state`` keeps its two terminal values so an
    # older reader of the poll is not broken; this field only splits its failure.
    outcome: str | None = None
    # A run of the tool's examples (web-v6 §8.3, §10): the health check's whole
    # result, or the verification that follows a published revise. Updated as
    # each example finishes, so a poll shows progress.
    verification: dict[str, Any] | None = None
    # Copied from the terminal ``InstallOutcome`` (see its fields): a revise's
    # session as the loop measured it, and the model's own two notes. None for an
    # install, a health check, a revise that failed before its session ran, and a
    # job that ended in ``_run_job``'s unexpected-error backstop.
    stopped_by: FinalizeTrigger | None = None
    failure: RunFailure | None = None
    rounds_used: int | None = None
    max_rounds: int | None = None
    handoff: str | None = None
    operator_action: str | None = None
    # Copied from the terminal ``InstallOutcome.draft``.
    draft: dict[str, Any] | None = None
    # Copied from the terminal ``InstallOutcome.draft_publish``.
    draft_publish: dict[str, Any] | None = None


# The three terminal ``InstallJob.outcome`` values.
_OUTCOME_PUBLISHED = "published"
_OUTCOME_NOT_PUBLISHED = "not_published"
_OUTCOME_FAILED = "failed"


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
        "env_keys": list(job.env_keys),
        "outcome": job.outcome,
        "kind": job.kind,
        "verification": job.verification,
        "stopped_by": job.stopped_by,
        "failure": job.failure,
        "rounds_used": job.rounds_used,
        "max_rounds": job.max_rounds,
        "handoff": job.handoff,
        "operator_action": job.operator_action,
        "draft": job.draft,
        "draft_publish": job.draft_publish,
    }


# How many containers deep ``_job_text_safe`` follows. The deepest text a job
# holds is an example's name in its verification, three containers down (the
# verification, its ``runs`` list, one run); the fourth level is a margin, and
# the bound itself keeps the walk from following whatever a caller nests.
_JOB_TEXT_DEPTH = 4


def _job_text_safe(value: Any, depth: int = 0) -> Any:
    """``value`` with ``tools._utf8_safe`` applied to every string in it.

    Walks dicts (keys too), lists and tuples, building new ones, down to
    ``_JOB_TEXT_DEPTH`` containers; anything deeper, and every value that is not
    text, is kept as it is.
    """
    if isinstance(value, str):
        return tools._utf8_safe(value)
    if depth >= _JOB_TEXT_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            _job_text_safe(key, depth + 1): _job_text_safe(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_job_text_safe(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_job_text_safe(item, depth + 1) for item in value)
    return value


def _update_job(job_id: str, **changes: Any) -> None:
    """Apply field changes to a job, if it is still tracked.

    The one writer of a job's fields once it is admitted, and so the one place
    their text is made encodable: every string written, inside the containers a
    job holds as well (``verification``, ``env_keys``, ``draft``), has its lone
    surrogates replaced (``_job_text_safe``). A poll encodes the job as UTF-8
    JSON, and one such character fails the whole response. The model's own text
    does not bring them: every text field of a structured answer passes
    ``memory_ai._coerce_str``, which refuses one, so that answer is retried. What
    this guards is text built from what the backend read -- a path, a message
    naming one, an example's name in a verification -- since a file name read on
    Linux can carry them. This covers the job API's way out and nothing else:
    text that leaves by another way -- stored in a draft, sent in a prompt -- is
    that way's to make safe.

    A job evicted past ``_MAX_JOBS`` while its task was still running simply
    stops being visible; the task's later updates land here as no-ops. That is
    the accepted cost of a bounded, unpersisted job table.
    """
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        for key, value in changes.items():
            setattr(job, key, _job_text_safe(value))


async def _run_job(
    job_id: str,
    run: Callable[[], Awaitable[InstallOutcome]],
    action: str,
) -> None:
    """The background task body: run one job, record the outcome.

    ONE body for an install, a revise and a publish of a draft as it is, taking
    the run as a zero-argument factory (the caller has already bound its own
    arguments). Sharing it is the point: the state machine, the field copy-out
    and the backstop below must be identical for every kind that runs through it,
    and a second near-copy is exactly where they would drift. A health check has
    its own body (``_run_check_job``).

    ``run_install``, ``run_revise`` and ``run_publish_draft`` already map every
    EXPECTED failure to a friendly outcome; the except here is the total backstop
    for a genuine bug, because an exception escaping a fire-and-forget task would
    otherwise vanish (leaving the job stuck on "running" forever from the FE's
    point of view). Category only -- a bug's str() could carry anything (and MUST
    NOT: secret values are threaded through the runs, so a leaky str(exc) is
    exactly why this is category-only). The one exception is a failure written for the
    operator (``tools.OperatorFacingError``: a known-secret list that is not
    whole, a tools directory that cannot be resolved), whose message names only
    paths, tool names, type names and what to fix: the job shows that message.
    """
    _update_job(job_id, state="running")
    job_token = _CURRENT_JOB_ID.set(job_id)
    try:
        outcome = await run()
    except Exception as exc:
        if isinstance(exc, tools.OperatorFacingError):
            error = str(exc)
        else:
            error = f"{action}過程發生未預期錯誤（{type(exc).__name__}）。"  # noqa: RUF001
        _update_job(
            job_id,
            state="failed",
            error=error,
            outcome=_OUTCOME_FAILED,
            finished_at=_now_iso(),
        )
        return
    finally:
        _CURRENT_JOB_ID.reset(job_token)
    if outcome.ok:
        terminal = _OUTCOME_PUBLISHED
    elif outcome.not_ready:
        terminal = _OUTCOME_NOT_PUBLISHED
    else:
        terminal = _OUTCOME_FAILED
    # A revise's name was set at admission; an outcome that does not repeat it
    # (a failure before the builder reported anything) must not erase it.
    named = {"tool_name": outcome.tool_name} if outcome.tool_name is not None else {}
    _update_job(
        job_id,
        state="succeeded" if outcome.ok else "failed",
        error=outcome.error,
        summary=outcome.summary,
        llm_log_id=outcome.llm_log_id,
        env_keys=outcome.env_keys,
        outcome=terminal,
        verification=outcome.verification,
        stopped_by=outcome.stopped_by,
        failure=outcome.failure,
        rounds_used=outcome.rounds_used,
        max_rounds=outcome.max_rounds,
        handoff=outcome.handoff,
        operator_action=outcome.operator_action,
        draft=outcome.draft,
        draft_publish=outcome.draft_publish,
        finished_at=_now_iso(),
        **named,
    )


def _single_flight_held() -> bool:
    """True when something already occupies the single flight. LOCK REQUIRED.

    Callers MUST already hold ``_JOBS_LOCK``: every user of this predicate has to
    evaluate it and ACT on the answer under one acquisition, or it is just a
    check-then-act with a lock draped over half of it. ``_JOBS_LOCK`` is a plain
    (non-reentrant) ``threading.Lock``, so this deliberately does not take it
    itself -- that is what lets the three callers below inline it into their own
    critical sections instead of keeping near-copies of it.

    A TERMINAL job never blocks anything: only queued/running work, and a held
    synchronous reservation, do.
    """
    return bool(_SYNC_OPS) or any(job.state in ("queued", "running") for job in _JOBS.values())


def _admit_job(
    *, reservation_token: str | None = None, kind: str = "install", tool_name: str | None = None
) -> InstallJob | None:
    """Register one queued job, or None when another is already active (M7/D40).

    The single-flight admission, written ONCE for both kinds: the active-check
    and the insert happen under the SAME lock acquisition, so there is no
    check-then-start race, and a caller maps None onto its route's 409. ANY
    queued/running job blocks ANY new one -- an install renames a package
    directory into place and a revise renames a version into ``versions/`` and
    then moves ``current``, so letting them overlap would race two writers over
    one package tree (and would make the two sessions' same-workflow log records
    ambiguous to ``last_record_id_for_workflow``). A held SYNCHRONOUS reservation
    blocks one too (R7-3): a regenerate in flight is about to write the summary
    of a version this job may be superseding.

    Eviction keeps the newest ``_MAX_JOBS`` by creation time (job_id as a
    deterministic tiebreak for identical timestamps); a TERMINAL (succeeded/
    failed) job stays pollable until evicted and never blocks a new submit.

    ``tool_name`` is known from the start for a revise and a health check (the
    path names the tool) and set here, so a poll -- the 工具 page following a
    job started elsewhere (``/tools?job=``) -- can say which tool from the
    first response, and a job that ends in the backstop still names it (P4
    review r1, H3). An install learns its name from the builder at the end.
    """
    job = InstallJob(
        job_id=uuid4().hex,
        state="queued",
        created_at=_now_iso(),
        kind=kind,
        tool_name=tool_name,
    )
    with _JOBS_LOCK:
        if reservation_token is None:
            if _single_flight_held():
                return None
        else:
            # A revise first reserves the slot, checks expected_vid without
            # enqueuing anything, then atomically converts that exact lease into
            # its queued job.  A missing/stale token can never steal another
            # operation's slot.
            if reservation_token not in _SYNC_OPS:
                return None
            _SYNC_OPS.remove(reservation_token)
        _JOBS[job.job_id] = job
        while len(_JOBS) > _MAX_JOBS:
            oldest = min(_JOBS.values(), key=lambda j: (j.created_at, j.job_id))
            del _JOBS[oldest.job_id]
    return job


def _launch_job(job: InstallJob, run: Callable[[], Awaitable[InstallOutcome]], action: str) -> str:
    """Spawn an admitted job's background task and return its id.

    Must be called with a running event loop (the async router handler is). The
    strong reference in ``_TASKS`` is what keeps the Task from being
    garbage-collected mid-flight; the done-callback drops it again.
    """
    task = asyncio.create_task(_run_job(job.job_id, run, action))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return job.job_id


def start_install_job(
    openapi_url: str,
    instructions: str,
    *,
    secret_name: str | None = None,
    secret_value: str | None = None,
) -> str | None:
    """Create an install job and launch it; returns the job id, or None when a
    job is ALREADY active (queued|running) -- which the router maps to a 409.

    ``secret_name``/``secret_value`` (D36) are threaded straight to the task and
    on to ``run_install``; they are DELIBERATELY not stored in ``InstallJob`` (so
    the value can never surface in a job/poll response), only passed forward.

    The run is bound as a closure over the module-level ``run_install`` NAME
    (resolved when the task runs, not when this returns), which is also what
    keeps the job tests' monkeypatched ``run_install`` reachable.
    """
    job = _admit_job()
    if job is None:
        return None
    return _launch_job(
        job,
        lambda: run_install(
            openapi_url, instructions, secret_name=secret_name, secret_value=secret_value
        ),
        _ACTION_INSTALL,
    )


# Why a revise that does not name the tool's draft is refused (``draft_pending``),
# by the draft's state (``tool_drafts.draft_state``): one that can be continued
# offers that and the discard; any other offers taking the files out of it, then
# the discard, which is all such a draft allows.
_DRAFT_PENDING_OPEN = "這個工具有一份未完成的修正（草稿）。請接續修正，或在工具列放棄它。"  # noqa: RUF001
_DRAFT_PENDING_BLOCKED = (
    "這個工具有一份未完成的修正（草稿），目前不能接續。需要裡面的檔案就先到草稿路徑取回，"  # noqa: RUF001
    "然後在工具列放棄它，才能送出新的修正。"  # noqa: RUF001
)

# Every way a revise submit is refused before a job exists. The route answers
# each one and is checked to (``routers.tools.revise_tool``): a refusal added here
# without an answer there fails the type check rather than becoming a 500.
ReviseRefusal = Literal[
    "env_policy",
    "job_busy",
    "not_found",
    "version_mismatch",
    "draft_pending",
    "draft_gone",
    "draft_changed",
    "draft_unusable",
    "draft_publishing",
]


@dataclass(frozen=True, slots=True)
class ReviseJobStart:
    """A revise submit result with machine-distinct pre-job refusals.

    ``message`` is set for ``env_policy`` -- the gate's own zh-TW text, which
    names the offending keys and is the refusal whose wording the operator needs
    in order to act -- and for ``draft_pending``, whose words follow the draft's
    state.
    """

    job_id: str | None = None
    refusal: ReviseRefusal | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class DraftRequest:
    """What a revise that continues a draft names: the draft, as the page saw it
    (``draft_id``, ``generation``); what the route read of it before the slot
    (``first_seen``, None when it read nothing, ``tool_drafts.first_read``); and
    the run records the request newly attached."""

    draft_id: str
    generation: int
    first_seen: tuple[str, int] | None
    attached: tuple[AttachedRun, ...] = ()


async def start_revise_job(
    name: str,
    feedback: str | None,
    expected_vid: str,
    *,
    runs: tuple[AttachedRun, ...] = (),
    draft: DraftRequest | None = None,
) -> ReviseJobStart:
    """Check the env policy, take the global slot, compare ``expected_vid``, enqueue.

    The stale path never creates an ``InstallJob`` and never launches a task.
    The reservation is converted into a queued job atomically only after the
    comparison passes, so no other current writer can race that check.

    The ``.env``/``config.env`` gate runs FIRST, before the slot is even taken
    (web-v6 §5.2). Inside the job alone, its refusal arrived seconds later as a
    failed job card -- and a revise submitted from anywhere but the 工具 page may
    never have that card on screen, so the one message that says which key to
    move was effectively silent. Here it is the response to the click. It is a
    read and a policy check, with no version in it, so running it outside the
    slot cannot weaken invariant K: the ``expected_vid`` comparison stays after
    the reservation, exactly where it was. ``run_revise`` repeats the gate on
    whatever the files say when the job really starts.

    ``runs`` are the run records the job carries, which the route read and
    COPIED before calling this: a database read with no version in
    it, so it precedes the slot like the env gate, and the copy is what the job
    keeps -- a purge after this point changes nothing about the revise.

    The draft is judged after the slot and the version comparison, like
    everything that names the package's state, so a draft made or changed while
    the slot was held is one this answer sees. Without ``draft`` the revise is a
    first round, refused while the tool has a draft in any state
    (``draft_pending``, its words by that state, ``tool_drafts.draft_state``).
    With it the revise continues that draft, refused unless
    ``tool_drafts.judge_continuation`` finds it the one named -- the same one the
    route read its records from -- usable, not being published from, and still
    based on ``current``.
    """

    package_root = await run_in_threadpool(tools._resolve_package_dir_no_alias, name)
    if package_root is not None:
        _env, env_error = await run_in_threadpool(_revise_env_gate, package_root.path)
        if env_error is not None:
            return ReviseJobStart(refusal="env_policy", message=env_error)

    reservation = reserve_sync_operation()
    if reservation is None:
        return ReviseJobStart(refusal="job_busy")
    try:
        package_root = await run_in_threadpool(tools._resolve_package_dir_no_alias, name)
        if package_root is None:
            return ReviseJobStart(refusal="not_found")
        resolution = await run_in_threadpool(tools.resolve_current, package_root)
        if isinstance(resolution, tools.Unresolved):
            return ReviseJobStart(refusal="not_found")
        if resolution.vid != expected_vid:
            return ReviseJobStart(refusal="version_mismatch")
        continuation: tool_drafts.Continuation | None = None
        if draft is None:
            state = await run_in_threadpool(
                functools.partial(
                    tool_drafts.draft_state, package_root, resolution, job_active=job_is_active
                )
            )
            if state is not None:
                return ReviseJobStart(
                    refusal="draft_pending",
                    message=_DRAFT_PENDING_OPEN if state == "open" else _DRAFT_PENDING_BLOCKED,
                )
        else:
            judged = await run_in_threadpool(
                tool_drafts.judge_continuation,
                package_root,
                resolution,
                (draft.draft_id, draft.generation),
                draft.first_seen,
            )
            if isinstance(judged, str):
                return ReviseJobStart(refusal=judged)
            continuation = tool_drafts.Continuation(
                expected=(judged["draft_id"], judged["generation"]), attached=draft.attached
            )
        job = _admit_job(reservation_token=reservation, kind="revise", tool_name=name)
        if job is None:
            return ReviseJobStart(refusal="job_busy")
        reservation = ""
        return ReviseJobStart(
            job_id=_launch_job(
                job,
                lambda: run_revise(
                    name, feedback, resolution, runs=runs, continuation=continuation
                ),
                _ACTION_REVISE,
            )
        )
    finally:
        if reservation:
            release_sync_operation(reservation)


# Every way a publish of a draft as it is is refused before a job exists. The
# route answers each one and is checked to (``routers.tools.publish_tool_draft``):
# a refusal added here without an answer there fails the type check rather than
# becoming a 500.
PublishDraftRefusal = Literal[
    "job_busy",
    "not_found",
    "version_mismatch",
    "draft_gone",
    "draft_changed",
    "draft_unusable",
    "draft_publishing",
]


@dataclass(frozen=True, slots=True)
class PublishDraftJobStart:
    """A submit of a draft's publish: the job id, or why no job was made."""

    job_id: str | None = None
    refusal: PublishDraftRefusal | None = None


async def start_publish_draft_job(
    name: str, draft_id: str, generation: int, expected_vid: str
) -> PublishDraftJobStart:
    """Take the global slot, compare ``expected_vid``, judge the draft, enqueue its publish.

    The order a continuation's submit keeps: everything that names the package's
    state is read after the slot is taken, so what it reads holds until the job
    begins but for a hand edit. The draft must be the one named, continuable and
    based on ``current`` (``tool_drafts.judge_draft``); there is no read of it
    before the slot to compare with, since a publish attaches no run records. No
    ``.env`` gate: there is no builder session to hand the package's ``.env`` to.
    The reservation becomes the queued job atomically, so a refused submit has no
    job id and starts nothing.
    """
    reservation = reserve_sync_operation()
    if reservation is None:
        return PublishDraftJobStart(refusal="job_busy")
    try:
        package_root = await run_in_threadpool(tools._resolve_package_dir_no_alias, name)
        if package_root is None:
            return PublishDraftJobStart(refusal="not_found")
        resolution = await run_in_threadpool(tools.resolve_current, package_root)
        if isinstance(resolution, tools.Unresolved):
            return PublishDraftJobStart(refusal="not_found")
        if resolution.vid != expected_vid:
            return PublishDraftJobStart(refusal="version_mismatch")
        judged = await run_in_threadpool(
            tool_drafts.judge_draft, package_root, resolution, (draft_id, generation)
        )
        if isinstance(judged, str):
            return PublishDraftJobStart(refusal=judged)
        job = _admit_job(reservation_token=reservation, kind="publish_draft", tool_name=name)
        if job is None:
            return PublishDraftJobStart(refusal="job_busy")
        reservation = ""
        return PublishDraftJobStart(
            job_id=_launch_job(
                job,
                functools.partial(run_publish_draft, name, resolution, judged),
                _ACTION_PUBLISH_DRAFT,
            )
        )
    finally:
        if reservation:
            release_sync_operation(reservation)


def any_job_active() -> bool:
    """True while ANY job is queued/running or a sync reservation is held (D40).

    The single-flight predicate as a plain QUESTION, for callers that only want
    to describe the state rather than take it. Nothing on a request path asks it
    any more: everything that ACTS on the answer goes through ``_admit_job`` or
    ``reserve_sync_operation``, because those decide and take under one lock
    acquisition, and asking here and acting afterwards is precisely the race
    R7-3 removed from the regenerate route. What it remains is the module's
    statement of what "the single flight is occupied" MEANS -- the one place both
    halves are named together, and where the tests pin that a reservation counts
    exactly as much as a job.

    It reports True for a held reservation as well as for a job, since both mean
    "something else is writing this package tree" -- a job installs a package or
    publishes a new version into one, a reservation writes one version's summary.
    """
    with _JOBS_LOCK:
        return _single_flight_held()


def reserve_sync_operation() -> str | None:
    """Take the single flight for one SYNCHRONOUS operation; None = refused (R7-3).

    The counterpart of ``_admit_job`` for work that is not a job: the summary
    regenerate runs inside a request, but it spends a full LLM round trip between
    reading a version and writing that version's sidecar. The operation remains
    part of the one-at-a-time builder/regenerate policy, so its admission must be
    held for that whole interval. Its old gate was a bare ``any_job_active()``
    read, which released the policy before the await and allowed a builder job to
    be admitted concurrently. Taking a reservation keeps it in the same admission
    domain as the jobs: while one is held ``_admit_job`` refuses.

    The test and the take happen under ONE acquisition of ``_JOBS_LOCK``, exactly
    as ``_admit_job`` does, which is the whole reason this is a function and not
    a pair of them. The caller MUST release in a ``finally``; the token it gets
    back is what it releases, so a stale release from another path is a no-op
    rather than a stolen reservation. Nothing is awaited while the lock is held
    (it is taken and dropped inside this call), so a reservation can block work
    but can never deadlock it.
    """
    token = uuid4().hex
    with _JOBS_LOCK:
        if _single_flight_held():
            return None
        _SYNC_OPS.add(token)
    return token


def release_sync_operation(token: str) -> None:
    """Give back a reservation from ``reserve_sync_operation``; idempotent."""
    with _JOBS_LOCK:
        _SYNC_OPS.discard(token)


def job_is_active(job_id: str) -> bool:
    """Whether ``job_id`` names a queued or running job of this process.

    What a draft's ``running`` and ``publishing`` marks are read against: a mark
    whose job is not active here belongs to a round that ended, or to a process
    that is gone.
    """
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return job is not None and job.state in ("queued", "running")


def get_job(job_id: str) -> dict[str, Any] | None:
    """One job's state as a JSON-ready dict, or None (unknown/evicted/restart)."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return _job_dict(job) if job is not None else None


def _reset_jobs_for_tests() -> None:
    """Drop all tracked jobs AND any held sync reservation so a test starts clean.

    Both halves of the single flight are module state, so both have to be reset
    here or a test that exercised a refusal path would leave the next one unable
    to admit anything (tasks, if any, run out as before).
    """
    with _JOBS_LOCK:
        _JOBS.clear()
        _SYNC_OPS.clear()
