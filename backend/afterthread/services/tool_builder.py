"""Build, install, and revise versioned tool packages.

Both entry points run the same LLM builder session inside
``<tools_dir>/.staging/<uuid>/build`` and reserve ``shell`` beside it for the
assembled artifact. A fresh install assembles an entire package shell and makes
it visible with one rename. A revise copies the resolved version into ``build``,
commits one new version in ``shell``, renames that version under ``versions/``,
and only then atomically publishes ``current``.

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
import logging
import os
import queue
import secrets
import shutil
import stat
import subprocess
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx2
from pydantic import BaseModel, ConfigDict, model_validator
from starlette.concurrency import run_in_threadpool

from afterthread.config import get_settings
from afterthread.services import llm_log, token_budget, tool_meta, tools
from afterthread.services.llm import (
    LLMNotConfiguredError,
    LlmTool,
    LLMUpstreamError,
    generate_structured,
)
from afterthread.services.memory_ai import _coerce_bool, _coerce_str, _truncate_to
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

# D40 revise-only outcomes. Category-only by the same construction as the install
# ones above: a fixed zh-TW string, never a path and never a value.
_ERROR_REVISE_NOT_FOUND = "找不到要修訂的工具（可能已被刪除）。"  # noqa: RUF001
# Package ``.env`` values must be readable before a revise session because they
# are exported to run_shell and registered with both redactors.
_ERROR_REVISE_ENV_UNREADABLE = "無法讀取既有工具包的 .env，修訂已取消。"  # noqa: RUF001
_ERROR_REVISE_ENV_TOO_LARGE = "既有工具包的 .env 超過大小上限，修訂已取消。"  # noqa: RUF001
# A hand-edited ``.env`` holding a value BELOW the redactor's floor (R1-1). Names
# the CONDITION and the two remedies, never the key and never the value -- the
# whole point is that this value cannot be masked, so it must not be echoed by the
# very message that refuses to expose it. See ``run_revise``'s own gate for why
# refusing beats running.
_ERROR_REVISE_ENV_UNMASKABLE = (
    "既有工具包的 .env 內有值過短、無法遮蔽，修訂已取消：請加長該值，或將它從 .env 移除。"  # noqa: RUF001
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
# would be worse than the refusal. Category-only like the rest: the condition and the
# remedy, never the key and never the value -- printing the value in the message that
# refuses to expose it would BE the leak.
_ERROR_REVISE_ENV_UNMATCHABLE = (
    "既有工具包的 .env 內有值的寫法無法對應到實際值、無法遮蔽，修訂已取消："  # noqa: RUF001
    "請把該行的值寫成最單純的形式（原值直接寫，或整段用引號包住），"  # noqa: RUF001
    "並移除行尾註解與多餘的跳脫。"
)
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
    """Persist regular files and only real directories in this tree, bottom-up."""

    try:
        directories = [root]
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
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
the defaults remain part of this version. Do NOT write a `.env` file. If the \
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
2. Write run.py and tool.json. Keep configuration defaults in code; never write \
.env.
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


def _builder_system_prompt(secret_name: str | None) -> str:
    """The builder system prompt, plus the secret addendum when one was supplied.

    Only the NAME is interpolated (never the value). ``str.replace`` (not
    ``str.format``) so the many literal ``$``/``{`` characters in the shell
    examples above are left untouched -- only the ``{name}`` token is swapped.
    """
    if not secret_name:
        return _BUILDER_SYSTEM_PROMPT
    return _BUILDER_SYSTEM_PROMPT + _SECRET_PROMPT_ADDENDUM.replace("{name}", secret_name)


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

Three facts about this workspace that differ from a fresh build:
- The package's operator-owned `.env` remains outside this version workspace. \
Do not write one, do not invent placeholder values, and do not make your changes \
depend on rewriting it -- any `.env` you write is stripped before publication. \
Its values are \
already exported into your run_shell environment under their own names, so you \
can still live-test the real API (e.g. `echo '{"query":"test"}' | python3 \
run.py`) without ever seeing them. If the user's feedback asks to CHANGE a \
secret value, say so in your summary -- that is done by hand, not here.
- Never write a secret value into any file. A package that embeds a known \
secret is REFUSED, and that refusal discards your whole revision.
- The backend-owned root `.afterthread.meta/` directory is missing from this \
workspace and is rebuilt after you finish; do not create or edit it. Any legacy \
flat `.ai_meta.json` or `.afterthread-state.json` file you find here is this \
version's tool content: leave it alone unless the feedback asks you to change it.

Finish exactly as described above, with one added rule: "tool_name" MUST be \
exactly {name}, because it names the installed package this revision updates \
-- any other value is treated as an attempt to rename the tool and the revision \
is discarded. "summary" reports what you CHANGED and how you verified it."""


def _revise_system_prompt(name: str) -> str:
    """The builder system prompt, reframed for revising the package ``name``.

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
    """
    return _BUILDER_SYSTEM_PROMPT + _REVISE_PROMPT_ADDENDUM.replace(
        "{name}", tools.redact_known_secrets(name)
    )


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
        # F1/D36: redact known secret values out of the summary BEFORE the 2000-char
        # slice (redact->cap, the same order the whole pipeline uses). A secret
        # straddling the slice edge must be masked while the summary is still WHOLE, or
        # the cut would leave an unmatchable prefix fragment -- an INTERIOR fragment the
        # trailing-fragment guard cannot catch. The in-flight install secret is
        # registered for the whole build (run_install), so it is redactable here inside
        # generate_structured's model_validate. run_install ALSO redacts the summary at
        # outcome construction (the belt that additionally covers the non-_sanitize
        # value used to build the not-ready error); double-redaction is a no-op on the
        # already-masked marker.
        # The STRIP likewise runs AFTER the mask, for the same reason one step earlier:
        # a registered value carrying edge whitespace (a hand-edited .env with a quoted
        # " secret-token") stops matching once strip eats that edge, so stripping first
        # would hand the redactor a body it no longer recognizes. Untouched text in,
        # redaction, THEN the cosmetic trim and the slice.
        return {
            "tool_name": _coerce_str(data.get("tool_name")).strip()[:_TOOL_NAME_MAX],
            "summary": tools.redact_known_secrets(_coerce_str(data.get("summary"))).strip()[
                :_SUMMARY_CAP
            ],
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
    # Names extracted from a builder-written .env before that file was stripped.
    # Values are intentionally not representable in the outcome or job schema.
    env_keys: tuple[str, ...] = ()


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


def _run_shell_subprocess(
    staging: Path,
    command: str,
    timeout: float,
    cap: int,
    extra_env: dict[str, str] | None = None,
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
    """
    env = {name: os.environ[name] for name in tools._PASSTHROUGH_ENV if name in os.environ}
    if get_settings().tls_no_verify:
        env["TLS_NO_VERIFY"] = "1"
    if extra_env:
        env.update(extra_env)
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
    if output.stdout_overflow:
        return tools._cap_output(tools.redact_known_secrets(output.stdout), cap)
    if proc.returncode != 0:
        body = tools._cap_output(tools.redact_known_secrets(output.stdout).strip(), cap)
        return f"command failed (exit {proc.returncode}): {body}"
    return tools._cap_output(tools.redact_known_secrets(output.stdout), cap)


def _build_meta_tools(staging: Path, secret_env: dict[str, str] | None = None) -> list[LlmTool]:
    """The four meta-tools, every handler a closure over this session's staging.

    All handlers follow the LlmTool no-raise contract: filesystem/subprocess
    failures come back as descriptive error TEXT the model can react to (the
    loop's own except is only the last backstop). Settings-bound limits are
    read at call time, mirroring the runtime tools' handlers.

    ``secret_env`` is the env addition injected into run_shell's environment so
    the builder can live-test the real API -- the install form's single
    ``{<NAME>: <value>}`` (D36), or a revise's whole preserved package ``.env``
    (D40) -- passed straight to ``_run_shell_subprocess``. It touches ONLY
    run_shell (the file meta-tools never see it) and never any prompt.
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
            return await run_in_threadpool(_write)
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
        return await run_in_threadpool(
            _run_shell_subprocess,
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


def _revise_user_prompt(feedback: str, manifest_text: str | None) -> str:
    """The revise session's user turn: the feedback + the package's manifest.

    Deliberately NOT a re-fetch of the OpenAPI document (D40): the revision is
    driven by the user's words and by the code already in the workspace, the
    original URL is kept only as provenance (and is reduced to a host on
    capture), and re-fetching would spend a network round trip on a document the
    model can no longer be told is current.

    ``tool.json`` is quoted even though the model can `read_file` it, because it
    is the ONE file whose shape the feedback is most often about (the
    description and parameters an assistant sees) -- having it in the opening
    turn saves a round and keeps the model from revising against a guess.

    Both parts are redacted BEFORE the budget cut, the same redact-then-cap
    order every prompt site here uses: a secret straddling the cut would become
    an unmatchable interior fragment. The feedback is redacted where the
    install's instructions are not -- it is written AFTER the tool has a live
    ``.env``, so a user quoting their own key is a value we now genuinely know.
    Each part carries the same per-part budget ``_builder_user_prompt`` gives the
    document; both are separately request/manifest-bounded already, so the cut is
    a backstop rather than the usual case.
    """
    budget = token_budget.char_allowance(get_settings().llm_prompt_budget_tokens)
    parts = [
        "Revise the existing tool package as described below.",
        "User feedback:",
        _truncate_to(tools.redact_known_secrets(feedback), budget),
    ]
    if manifest_text is not None:
        parts += [
            "The package's current tool.json:",
            _truncate_to(tools.redact_known_secrets(manifest_text), budget),
        ]
    return "\n\n".join(parts)


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
    is destructive and the builder has unjailed shell access.
    """

    root_error = _verify_staging_root(build_root.path, base)
    if root_error is not None:
        return (), root_error
    env_file = build_root.path / ".env"
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
    """
    metadata = staging / tools._META_DIRNAME
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

    Two independent layers, mirroring the resolve-then-contain house pattern
    (``tools._resolve_package_dir_no_alias``'s is_symlink-before-resolve
    composition):

    * ``is_symlink()`` on the path ITSELF, no resolve: one cheap lstat catches
      the attack above outright -- a symlink planted AT the staging leaf;
    * containment under the RESOLVED ``<base>/.staging`` shell
      (``tools._is_within``, the same predicate ``_resolve_in_staging`` and
      ``tools._resolve_package_dir`` use): catches ANCESTOR substitution the leaf
      check alone cannot -- e.g. ``.staging`` itself swapped for a symlink, which
      leaves the staging LEAF a perfectly ordinary directory (so ``is_symlink()``
      on the full path reports False) while the fully RESOLVED path lands outside
      the shell entirely.

    This narrows a window rather than closing one, and is the SAME accepted
    residual class ``_promote_staging``'s own docstring names for its
    check-then-move exists-check (a race against a second concurrent actor with
    the service's uid) -- just one step earlier: check-then-REMOVE instead of
    check-then-move. A swap landing in the instant between this check and the
    strip's ``lstat`` syscall is still possible in principle; what this
    closes is the window the finding actually reported -- the ENTIRE builder
    session, start to finish -- down to that one syscall gap.
    """
    if staging.is_symlink():
        return _ERROR_STAGING_TAMPERED
    staging_root = base.resolve() / _STAGING_DIRNAME
    if not tools._is_within(staging_root, staging.resolve()):
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
) -> tuple[_PublishedRevision | None, str | None]:
    """Install one durable version and only then publish it through ``current``."""

    base = package_root.path.parent
    root_error = _verify_staging_root(build_root.path, base)
    if root_error is not None:
        return None, root_error
    if _entry_exists(shell_root):
        return None, _ERROR_STAGING_TAMPERED
    strip_error = _strip_builder_sidecars(build_root.path)
    if strip_error is not None:
        return None, strip_error
    error = tools.validate_tool_content(build_root, expected_name=package_root.path.name)
    if error is not None:
        return None, f"工具包驗證失敗：{error}"  # noqa: RUF001

    try:
        package_info = os.lstat(package_root.path)
        versions_info = os.lstat(package_root.path / tools._VERSIONS_DIRNAME)
    except FileNotFoundError:
        return None, _ERROR_REVISE_TARGET_MISSING
    except OSError:
        return None, _ERROR_REVISE_TARGET_ALIAS
    if not stat.S_ISDIR(package_info.st_mode) or not stat.S_ISDIR(versions_info.st_mode):
        return None, _ERROR_REVISE_TARGET_ALIAS
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
        return None, _ERROR_REVISE_TARGET_REPLACED

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
        (shell_root / tools._META_DIRNAME).mkdir()
    except OSError as exc:
        return None, f"無法組裝工具版本（{type(exc).__name__}）。"  # noqa: RUF001
    if not tools.write_origin_meta(tools.BuildRoot(shell_root), origin):
        return None, _ERROR_ORIGIN_WRITE
    if not _fsync_tree(shell_root):
        return None, _ERROR_DURABILITY

    versions = package_root.path / tools._VERSIONS_DIRNAME
    for _ in range(_VID_RETRY_LIMIT):
        vid = _choose_unused_vid(versions)
        if vid is None:
            return None, _ERROR_VERSION_ID_WRITE
        target = versions / vid
        if _entry_exists(target):
            continue
        try:
            os.rename(shell_root, target)
        except FileExistsError:
            continue
        except OSError as exc:
            return None, f"無法安裝工具版本（{type(exc).__name__}）。"  # noqa: RUF001
        published_version = tools.VersionRoot(target)
        if not _fsync_directory(versions):
            return None, _ERROR_DURABILITY

        # Re-check AFTER the rename and its directory fsync, immediately before
        # publishing ``current``. The earlier placement guarded creation of the
        # version entry but left that rename+fsync gap free for a D21 hand edit
        # that publication would overwrite. Check the manifest fact first, then
        # resolve ``current`` LAST so no other filesystem read sits between the
        # pointer answer and publish_current.
        if tools.package_identity(previous.version_root) != expected_identity:
            return None, _ERROR_REVISE_TARGET_REPLACED
        current = tools.resolve_current(package_root)
        if isinstance(current, tools.Unresolved) or current.vid != previous.vid:
            return None, _ERROR_REVISE_TARGET_REPLACED
        if not tools.publish_current(package_root, vid):
            return None, _ERROR_CURRENT_WRITE
        # Construct the typed target from the exact directory just renamed and
        # vid just published. Re-resolving by package name here or in the summary
        # hook would let a later current edit redirect the new version's work.
        published = tools.Resolved(
            package_root,
            published_version,
            vid,
            tools.PreviousValue(previous.vid),
        )
        return _PublishedRevision(published, origin), None
    return None, _ERROR_VERSION_ID_WRITE


def _sweep_stale_backups(base: Path) -> None:
    """Best-effort collection of marked packages and discarded versions.

    One non-blocking exclusive lock replaces the old local-provenance and
    observed-running judgements. If any request or orphaned tool child still
    holds the shared side, the whole sweep is skipped and a later tool job
    retries. Hidden entries outside the marked namespace and symlinks are never
    traversed.
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
                if (
                    not tools._NAME_RE.fullmatch(child.name)
                    or child.is_symlink()
                    or not child.is_dir()
                ):
                    continue
                versions = child / tools._VERSIONS_DIRNAME
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
    passes the SAME ``_verify_staging_root`` gate ``_promote_staging`` applies
    (R9-1). This is load-bearing, not symmetry for its own sake: ``run_install``'s
    ``finally`` reaches here UNCONDITIONALLY -- including after promote just
    REFUSED the workspace as tampered -- and CPython's rmtree protection only
    refuses a path that is ITSELF a symlink. With the ``.staging`` ANCESTOR
    swapped for a symlink to an external directory holding a real ``<uuid>``
    subdir, ``staging.is_dir()`` follows the link and the rmtree LEAF is an
    ordinary directory, so without this gate the external directory would be
    deleted straight through the link. A workspace that fails the gate is left
    IN PLACE, parent shell included: it is evidence of tampering (or of a race
    worth seeing), not garbage -- and the orphan it leaves is the same accepted
    residue class as the refused-cleanup orphan symlink pinned by the r8 tests.
    The check-to-rmtree instant remains the accepted check-then-act residual
    window ``_verify_staging_root``'s own docstring names.

    The sweep rides HERE rather than at the head of the next promote, and the
    reason is reach: this is the one step EVERY tool job passes through
    unconditionally -- installs as well as revises, failures as well as successes
    -- so cleanup-failure remains do not wait for another revise to succeed before
    anything looks at them. The non-blocking exclusive flock makes the same
    sweep safe after a restart: an inherited child keeps its shared reference,
    so the new process skips instead of mistaking an empty local registry for
    proof. This placement also keeps the promote's pre-publication sequence (which
    r4/r10/r11/r12 spent four rounds ordering) free of a new destructive
    traversal. It hangs off a ``finally`` because it is INDEPENDENT of everything
    above it: a tampered workspace returns early -- deliberately, see above --
    and that says nothing about whether marked remains elsewhere in ``base`` are
    collectable. The staging logic itself is untouched by this addition.
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
    # Register the VALUE as redactable for the WHOLE build window (D36): llm_log
    # then masks it out of every builder-session prompt/response it records --
    # covering the gap BEFORE promote writes it into the package .env (after which
    # the .env scan in known_secret_values keeps covering it; the discard in the
    # finally just ends this transient window). Guarded by the try/finally below.
    if secret_value:
        tools.register_inflight_secret(secret_value)

    try:
        settings = get_settings()
        result: InstallResult | None = None
        llm_error: str | None = None
        try:
            result = await generate_structured(
                _builder_system_prompt(secret_name),
                _builder_user_prompt(instructions, openapi_text),
                InstallResult,
                workflow=_WORKFLOW,
                tools=_build_meta_tools(build_root.path, secret_env=secret_env),
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

        # The builder session ran (even a not-configured exit records one), so
        # link its AI 日誌 record to the outcome NOW -- error paths included.
        llm_log_id = llm_log.last_record_id_for_workflow(_WORKFLOW)
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
        # disobedient builder could. Redact the summary ONCE here -- the single choke
        # point for the outcome -- and derive every InstallOutcome below from the
        # masked value. It MUST happen HERE, not at job-update time: run_install's
        # finally (below) discards the in-flight secret before this function returns,
        # and a FAILED install also deletes the staging .env, so by the time _run_job
        # stores the outcome ``known_secret_values`` would no longer carry the value --
        # redacting then would be a no-op that leaks. The other outcome ``error`` fields
        # are safe by construction: the not-ready error is built from this masked
        # summary, and the llm/promote errors are backend/category strings that never
        # carry the value (str(exc) is category-only; promote errors are fixed zh-TW).
        summary = tools.redact_known_secrets(result.summary) if result.summary else None

        if not result.ready:
            reason = summary or "（AI 未說明原因）"  # noqa: RUF001
            return InstallOutcome(
                ok=False,
                summary=summary,
                error=f"AI 判定工具尚未完成：{reason}",  # noqa: RUF001
                llm_log_id=llm_log_id,
                env_keys=env_keys,
            )

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
    finally:
        # Discard the in-flight secret first (its .env now carries it post-move,
        # so redaction continues via the .env scan), then clean staging. A
        # successful move consumed the staging dir; cleanup then only drops the
        # (possibly empty) .staging shell. Every other exit removes the build.
        if secret_value:
            tools.discard_inflight_secret(secret_value)
        await run_in_threadpool(_cleanup_staging, session_root, base)


# --- the revise run -----------------------------------------------------------


def _is_preserved_env_name(root: Path, filename: str, *, exact_present: bool) -> bool:
    """Identify the exact package-root ``.env`` entry withheld from a build.

    Case variants are ordinary version content when the filesystem carries both
    names. A lone case-folded alias is withheld only when it is the same entry.
    """
    if filename == ".env":
        return True
    if exact_present or filename.casefold() != ".env":
        return False
    try:
        candidate = os.lstat(root / filename)
        managed = os.lstat(root / ".env")
    except OSError:
        return False
    return (candidate.st_dev, candidate.st_ino) == (managed.st_dev, managed.st_ino)


def _revise_copy_ignore(root: Path, source_dir: Any, names: list[str]) -> set[str]:
    """Exclude backend metadata and the package ``.env`` from a version copy.

    The source root is a VersionRoot, not a PackageRoot. At this scope the two
    legacy package-reserved filenames are ordinary tool content: migration may
    have put a FOREIGN ``.afterthread-state.json`` here byte-for-byte, and
    ``.ai_meta.json`` has no v5 reader here either. Applying the package-root
    reserved-name tuple here silently omitted those files from the next current
    version.

    Only the root ``.afterthread.meta/`` is backend-owned version data. The exact
    package-layer ``.env`` is withheld as well; nested names and distinct
    case-variants remain tool content.
    """
    at_root = Path(source_dir) == root
    exact_env_present = ".env" in names
    return {
        name
        for name in names
        if at_root
        and (
            name == tools._META_DIRNAME
            or _is_preserved_env_name(root, name, exact_present=exact_env_present)
        )
    }


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
    revise loudly here (裁決紀錄 #4 noted this path would surface exactly that).
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


def _env_assignment_rhs_all(text: str) -> list[str]:
    """EVERY assignment line's raw RHS, shadowed ones included (R8-1).

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
        line.split("=", 1)[1].strip()
        for line in text.split("\n")
        if _env_line_key(line) is not None
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
      deliberately ignore everything under ``tools._MIN_SECRET_LEN``, because
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
    check already chose), and the messages name the CONDITION and the REMEDY only
    -- never the key, never the value.
    """
    if any(len(value) < tools._MIN_SECRET_LEN for value in values.values()):
        return _ERROR_REVISE_ENV_UNMASKABLE
    spelled = _env_assignment_rhs(text)
    for key, value in values.items():
        rhs = spelled.get(key)
        if rhs is None or rhs not in _dotenv_safe_spellings(value):
            return _ERROR_REVISE_ENV_UNMATCHABLE
    # ... and every OTHER assignment line, shadowed ones included (R8-1), for its
    # SHAPE alone: `_plainly_spelled` asks whether a line holds its own text
    # literally, which is answerable without knowing which value it was meant to
    # spell -- and it is exactly what a shadowed line needs to be innocent.
    # `spelled` cannot serve here: it keeps only the WINNER per key, which is the
    # one line this check does not need.
    if any(not _plainly_spelled(rhs) for rhs in _env_assignment_rhs_all(text)):
        return _ERROR_REVISE_ENV_UNMATCHABLE
    return None


def _current_manifest_text(directory: Path) -> str | None:
    """The installed package's ``tool.json`` text for the revise prompt, or None.

    Blocking, and best-effort: a manifest we cannot read is a package the model
    will simply have to ``read_file`` for itself (or that is already broken), not
    a reason to refuse the revise the user asked for.
    """
    return tools._read_regular_file_capped(directory / "tool.json", tools._MANIFEST_MAX_BYTES)


async def run_revise(
    name: str,
    feedback: str,
    resolution: tools.Resolved | None,
) -> InstallOutcome:
    """Build and commit one new version, then publish ``current``.

    ``resolution`` is the exact answer whose vid the request compared, not a
    package name to resolve a second time after the background task starts. The
    initially resolved version supplies the build content and identity. The
    package toggle and ``.env`` remain package-layer state throughout.
    """
    base = tools.tools_dir()
    if base is None:
        return InstallOutcome(ok=False, error=_ERROR_TOOLS_DISABLED)

    if resolution is None:
        return InstallOutcome(ok=False, error=_ERROR_REVISE_NOT_FOUND)
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
    _env_existed, env_values, env_text, env_error = await run_in_threadpool(
        _read_env_for_values, package_root.path
    )
    if env_error is not None:
        return InstallOutcome(ok=False, error=env_error)
    registered_by_key = {key: value for key, value in env_values.items() if value}
    registered = list(registered_by_key.values())
    # BEFORE the LLM call, and before anything is registered: a value we would be
    # required to ignore, or one whose own assignment line spells it so that no
    # redactor can match what a ``cat`` of the file prints, cannot be protected by
    # registering it (see the docstring and ``_unmaskable_env_error``). The KEYS go
    # in too, because the spelling that has to vouch for a value is the one on the
    # line that DETERMINES it, never some other line or a comment that merely
    # repeats it (R6-1). Only the non-empty values are passed, so a ``KEY=`` line --
    # which contributes no secret to anything -- can never trip either half. On a
    # worker like every other scan in this function (R1-3): a 64 KiB ``.env`` is a
    # full walk plus thousands of substring searches.
    mask_error = await run_in_threadpool(_unmaskable_env_error, env_text, registered_by_key)
    if mask_error is not None:
        return InstallOutcome(ok=False, error=mask_error)
    for value in registered:
        tools.register_inflight_secret(value)

    session_root = base / _STAGING_DIRNAME / uuid4().hex
    build_root = tools.BuildRoot(session_root / "build")
    shell_root = session_root / "shell"
    try:
        copy_error = await run_in_threadpool(
            _copy_package_into_staging, resolution.version_root, build_root
        )
        if copy_error is not None:
            return InstallOutcome(ok=False, error=copy_error)

        settings = get_settings()
        manifest_text = await run_in_threadpool(
            _current_manifest_text, resolution.version_root.path
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
        # became redactable (R2-4) -- it was a pure join until then.
        system_prompt = await run_in_threadpool(_revise_system_prompt, name)
        user_prompt = await run_in_threadpool(_revise_user_prompt, feedback, manifest_text)
        result: InstallResult | None = None
        llm_error: str | None = None
        try:
            result = await generate_structured(
                system_prompt,
                user_prompt,
                InstallResult,
                workflow=_WORKFLOW,
                # The whole existing .env goes into run_shell's environment (never
                # into a prompt), so the model can live-test the real API against
                # the tool's own credentials without the file being in staging.
                tools=_build_meta_tools(build_root.path, secret_env=env_values or None),
                max_tool_rounds=settings.tool_install_max_rounds,
                timeout_seconds=settings.tool_install_timeout_seconds,
            )
        except LLMNotConfiguredError:
            llm_error = "LLM 尚未設定，無法執行修訂。"  # noqa: RUF001
        except LLMUpstreamError as exc:
            # str(exc) is category + fixed reason by construction (see llm.py).
            llm_error = f"AI 修訂工具失敗（{exc}）。"  # noqa: RUF001

        llm_log_id = llm_log.last_record_id_for_workflow(_WORKFLOW)
        env_keys, env_strip_error = await run_in_threadpool(_strip_builder_env, build_root, base)
        if env_strip_error is not None:
            return InstallOutcome(
                ok=False,
                tool_name=name,
                error=env_strip_error,
                llm_log_id=llm_log_id,
                env_keys=env_keys,
            )
        if llm_error is not None:
            return InstallOutcome(
                ok=False,
                tool_name=name,
                error=llm_error,
                llm_log_id=llm_log_id,
                env_keys=env_keys,
            )
        assert result is not None  # exactly one of result/llm_error is set above

        # The SAME single choke point run_install uses, and for the same reason:
        # the finally below discards the in-flight secrets before this function
        # returns, so redacting at job-update time would be a no-op that leaks.
        # On a WORKER for the same reason the prompt build above is (R1-3) -- this
        # is the identical tools-directory scan, just after the LLM call instead of
        # before it. Every ``redact_known_secrets`` call this function makes is off
        # the loop; the third one is in the rename refusal below.
        summary = (
            await run_in_threadpool(tools.redact_known_secrets, result.summary)
            if result.summary
            else None
        )
        # ``tool_name`` on these outcomes is the package the user addressed, never
        # the model's answer: the job's tool_name is what the FE links to.
        if not result.ready:
            reason = summary or "（AI 未說明原因）"  # noqa: RUF001
            return InstallOutcome(
                ok=False,
                tool_name=name,
                summary=summary,
                error=f"AI 判定修訂尚未完成：{reason}",  # noqa: RUF001
                llm_log_id=llm_log_id,
                env_keys=env_keys,
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
            # like the other two (R1-3): rare is not the same as free, and leaving
            # one instance of the class behind is how it grows back.
            attempted = await run_in_threadpool(tools.redact_known_secrets, result.tool_name)
            return InstallOutcome(
                ok=False,
                tool_name=name,
                summary=summary,
                error=f"AI 試圖將工具改名為「{attempted}」，修訂已取消。",  # noqa: RUF001
                llm_log_id=llm_log_id,
                env_keys=env_keys,
            )

        # The publisher reads immutable provenance from the version whose identity
        # was captured before the builder session.
        published, promote_error = await run_in_threadpool(
            _publish_revised_version,
            build_root,
            shell_root,
            package_root,
            resolution,
            package_identity,
            feedback,
        )
        if promote_error is not None:
            return InstallOutcome(
                ok=False,
                tool_name=name,
                summary=summary,
                error=promote_error,
                llm_log_id=llm_log_id,
                env_keys=env_keys,
            )
        assert published is not None
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
        return InstallOutcome(
            ok=True,
            tool_name=name,
            summary=summary,
            llm_log_id=llm_log_id,
            env_keys=env_keys,
        )
    finally:
        # The package .env was untouched, so its live scan keeps covering these
        # values after the temporary registrations are dropped. Cleanup always
        # targets the complete session root, including any unused shell.
        for value in registered:
            tools.discard_inflight_secret(value)
        await run_in_threadpool(_cleanup_staging, session_root, base)


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
# one shape for both kinds, while leaving the install's existing text (which its
# tests and the FE see) byte-identical.
_ACTION_INSTALL = "安裝"
_ACTION_REVISE = "修訂"


@dataclass(slots=True)
class InstallJob:
    """One install/revise job's visible state, as polled by the FE.

    Both kinds share this record (and the table it lives in) because they are
    the same thing to a poller: a builder session that ends in a tool being
    installed or replaced. The KIND is deliberately not a field -- nothing in the
    contract branches on it, and the FE polls the same endpoint for both.
    """

    job_id: str
    state: str  # queued | running | succeeded | failed
    created_at: str
    finished_at: str | None = None
    error: str | None = None
    tool_name: str | None = None
    summary: str | None = None
    llm_log_id: int | None = None
    env_keys: tuple[str, ...] = ()


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


async def _run_job(
    job_id: str,
    run: Callable[[], Awaitable[InstallOutcome]],
    action: str,
) -> None:
    """The background task body: run one builder session, record the outcome.

    ONE body for both job kinds, taking the run as a zero-argument factory (the
    caller has already bound its own arguments). Sharing it is the point: the
    state machine, the field copy-out and the backstop below must be identical
    for an install and a revise, and a second near-copy is exactly where they
    would drift.

    ``run_install``/``run_revise`` already map every EXPECTED failure to a
    friendly outcome; the except here is the total backstop for a genuine bug,
    because an exception escaping a fire-and-forget task would otherwise vanish
    (leaving the job stuck on "running" forever from the FE's point of view).
    Category only -- a bug's str() could carry anything (and MUST NOT: secret
    values are threaded through both runs, so a leaky str(exc) is exactly why
    this is category-only).
    """
    _update_job(job_id, state="running")
    try:
        outcome = await run()
    except Exception as exc:
        _update_job(
            job_id,
            state="failed",
            error=f"{action}過程發生未預期錯誤（{type(exc).__name__}）。",  # noqa: RUF001
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
        env_keys=outcome.env_keys,
        finished_at=_now_iso(),
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


def _admit_job(*, reservation_token: str | None = None) -> InstallJob | None:
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
    """
    job = InstallJob(job_id=uuid4().hex, state="queued", created_at=_now_iso())
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


@dataclass(frozen=True, slots=True)
class ReviseJobStart:
    """A revise submit result with machine-distinct pre-job refusals."""

    job_id: str | None = None
    refusal: str | None = None


async def start_revise_job(name: str, feedback: str, expected_vid: str) -> ReviseJobStart:
    """Take the global slot, compare ``expected_vid``, then enqueue a revise.

    The stale path never creates an ``InstallJob`` and never launches a task.
    The reservation is converted into a queued job atomically only after the
    comparison passes, so no other current writer can race that check.
    """

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
        job = _admit_job(reservation_token=reservation)
        if job is None:
            return ReviseJobStart(refusal="job_busy")
        reservation = ""
        return ReviseJobStart(
            job_id=_launch_job(
                job,
                lambda: run_revise(name, feedback, resolution),
                _ACTION_REVISE,
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
