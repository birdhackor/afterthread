"""The KB web installer: one LLM "tool builder" session that writes, tests and
installs a tool package (see D21 in docs/web-v2-decisions.md, Phase 5c).

Two entry points, one builder session shape. ``run_install`` builds a NEW
package from an OpenAPI document; ``run_revise`` (D40) hands the ALREADY
INSTALLED package back to the same kind of session together with the user's
feedback and REPLACES the installed copy with the result. They share the
prompts, the meta-tools, the staging discipline, the friendly-outcome contract,
the job table and the llm_log workflow name (``tool_install``: both are builder
sessions, and the AI 日誌 shows them as one kind).

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
4. on a ``ready`` result, first re-verify staging itself has not been moved or
   replaced by a symlink (``_verify_staging_root`` -- the builder's run_shell
   runs unjailed, D21), then strip any builder-written AI sidecar from staging
   (``_strip_builder_sidecars`` -- that file is backend-authored, and a forged
   one bypasses the whole ``write_tool_meta`` choke point), validate the staged
   package with the SAME checks the registry applies to installed packages
   (``tools.validate_package``) and move it into ``<tools_dir>/<name>``; on
   anything else, fail with a friendly error. Staging is always cleaned up;
5. once installed, hand the package to ``tool_meta.generate_and_store_summary``
   for its AI summary sidecar (D40). Strictly best-effort and it cannot raise:
   the install is already a success by then, so a failed summary must never
   flip the outcome.

``run_revise(name, feedback)`` reuses steps 2-5 with three differences, each of
which exists because the package it is editing is ALREADY LIVE (D40):

* staging is populated by COPYING the installed package (no fetch), MINUS the
  package ROOT's ``.env`` and MINUS the backend's own sidecar namespace. The
  ``.env`` exclusion is not tidiness: its values are in ``known_secret_values``,
  so copying it in would make ``validate_package``'s embedded-secret gate reject
  every revise. The LIVE file is copied back into staging BYTE-FOR-BYTE after
  validation (``_preserve_env_file``), exactly where an install injects its form
  secret;
* every value of that ``.env`` is registered as an in-flight secret for the
  WHOLE revise window, because ``known_secret_values`` skips dot-directories and
  the swap below parks the old package in one;
* promotion REPLACES rather than creates (``_promote_staging_replace``), and the
  model is not allowed to redirect it: ``tool_name`` must equal the package it
  was handed.

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
import os
import queue
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
# D40/R7-1: raised when a builder-written ``.ai_meta.json`` (or a file in that
# reserved temp namespace) cannot be REMOVED from staging before validation. The
# strip is fail-closed -- shipping a forged sidecar is the one unacceptable
# outcome -- so a deletion the filesystem refuses cancels the whole install.
# Category-only by construction: a fixed string, never a path or a value, since
# the very name that failed to delete could have been chosen to embed a secret.
_ERROR_SIDECAR_STRIP = "無法清除工具包內的 AI 總結側檔，安裝已取消。"  # noqa: RUF001
# D40/R8-1: raised when staging itself fails ``_verify_staging_root``'s re-check --
# either the path IS a symlink, or its RESOLVED location no longer sits inside the
# resolved ``<tools_dir>/.staging`` shell (an ancestor swapped for a symlink). A
# TRUE sibling of ``_ERROR_SIDECAR_STRIP`` (same suffix, same category-only shape):
# naming what staging turned OUT to be would be naming attacker-controlled content.
_ERROR_STAGING_TAMPERED = "暫存工作區已被移動或替換，安裝已取消。"  # noqa: RUF001

# D40 revise-only outcomes. Category-only by the same construction as the install
# ones above: a fixed zh-TW string, never a path and never a value.
_ERROR_REVISE_NOT_FOUND = "找不到要修訂的工具（可能已被刪除）。"  # noqa: RUF001
_ERROR_REVISE_FINALIZED = "總結已定版，請先解除定版再送出修訂。"  # noqa: RUF001
# The pre-swap 定版 re-check could not get an ANSWER out of the sidecar (R3-2).
# A DISTINCT string rather than reusing ``_ERROR_REVISE_FINALIZED``, because that
# one carries an INSTRUCTION -- 解除定版 -- which is exactly wrong here: nothing
# may be finalized at all, so following it would leave the operator toggling a
# state that is not the problem while every retry refuses again for a reason the
# message never named. The two refusals have different remedies (repair or remove
# a damaged sidecar vs. un-freeze a summary), so they need different messages;
# this is the same "same refusal point, different actionable cause" split
# ``_ERROR_REVISE_TARGET_MISSING`` and ``_ERROR_REVISE_TARGET_ALIAS`` already are.
# Category-only like the rest: it names what could not be determined, never the
# path and never what the file contained.
_ERROR_REVISE_SUMMARY_UNREADABLE = "無法確認總結是否已定版（AI 總結側檔讀取失敗），修訂已取消。"  # noqa: RUF001
# The ``.env`` is READ before the build -- for its VALUES, which have to be
# registered and exported -- and the FILE itself is copied back into staging after
# validation (R2-1). Both failure modes refuse the whole revise. UNREADABLE: a
# `.env` whose values we cannot enumerate is one whose secrets the redactor does
# not know while ``run_shell`` is running, which is the leak R1-1 already refuses
# to take. TOO_LARGE: the bounded reader returns cap+1 chars, so parsing it yields
# a TRUNCATED set of values -- the ones past the cap would go unregistered while
# the copied file still carries them; the COPY refuses on the same ceiling too
# (R4-1), because an over-cap ``.env`` is one the runtime loader degrades to
# nothing anyway and copying it would be the one unbounded step left before the
# swap. RESTORE: the byte copy into staging failed, so the revision would ship
# without the credentials it inherited.
_ERROR_REVISE_ENV_UNREADABLE = "無法讀取既有工具包的 .env，修訂已取消。"  # noqa: RUF001
_ERROR_REVISE_ENV_TOO_LARGE = "既有工具包的 .env 超過大小上限，修訂已取消。"  # noqa: RUF001
_ERROR_REVISE_ENV_RESTORE = "無法還原工具包的 .env，修訂已取消。"  # noqa: RUF001
# DISCARD: the live ``.env`` was DELETED during the session, so the builder's own
# ``.env`` must not ship in its place (R3-1) -- and removing it from staging
# failed, so the revision cannot be published in the shape that deletion asked
# for. Refusing keeps the operator's package as it is; publishing anyway would
# put a placeholder where credentials used to be.
_ERROR_REVISE_ENV_DISCARD = "無法移除修訂產生的 .env（原檔已於修訂期間刪除），修訂已取消。"  # noqa: RUF001
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
# The replace-mode promote's two "the package is no longer what we resolved"
# refusals. Both are races against a concurrent delete/replace by an actor with
# the service's uid -- the SAME accepted residual class ``_promote_staging``'s
# docstring names for its own check-then-move window.
_ERROR_REVISE_TARGET_MISSING = "原工具已被刪除，修訂結果未安裝。"  # noqa: RUF001
_ERROR_REVISE_TARGET_ALIAS = "原工具目錄已被替換為連結，修訂已取消。"  # noqa: RUF001
# R10-1: the package of that NAME is still there, but it is not the one this
# session copied from -- an operator deleted and reinstalled it, or replaced it
# wholesale, during the minutes the build ran. Publishing the old snapshot's
# revision over it would rename the new package aside and then delete it. The
# remedy is to re-send the feedback against what is installed now, which is what
# the message says; like every outcome error here it names the condition only.
_ERROR_REVISE_TARGET_REPLACED = "原工具在修訂期間被改動或重新安裝，請確認現況後重新送出意見。"  # noqa: RUF001


def _package_identity(directory: Path) -> tuple[int, int, int] | None:
    """The package's MANIFEST identity: ``(st_dev, st_ino, st_ctime_ns)`` of its
    ``tool.json``, or None when it cannot be read.

    Taken TWICE per revise -- once when the session reads the package, once before
    the swap -- so both calls must mean the same thing; that is why it is one
    helper rather than two inline ``lstat`` calls.

    The MANIFEST rather than the directory, and that choice is the whole design.
    Two weaker readings were measured and discarded:

    * the directory's inode alone does not answer "is this the same package": a
      delete-and-reinstall of the same name REUSES the inode on an ordinary Linux
      filesystem (measured here, not assumed -- the first version of this check
      was written against the opposite assumption and silently passed the exact
      scenario it exists to refuse);
    * the directory's inode plus its ctime/mtime DOES catch the reinstall, but it
      also fires on any change to the directory's CONTENTS -- and that contradicts
      an earlier adjudication this module already implements: an operator deleting
      the package's ``.env`` mid-session is HONORED (D40 r3), not refused. A check
      that cannot tell "replaced" from "edited" would have to break one of the two.

    ``tool.json`` separates them cleanly: every install and reinstall WRITES it (it
    is the one file ``validate_package`` requires), so a package that was replaced
    carries a different one; deleting or editing some OTHER file in the package
    leaves it untouched. Editing the manifest ITSELF in place during a revise is
    then treated as a replacement, which is the right side to err on -- that is
    the file whose contents the revision is rewriting.

    ``lstat``, so a manifest swapped for a symlink compares different rather than
    reporting on its target. None on any error: the caller treats "cannot say" as
    "this check cannot speak", never as "identity matches".
    """
    try:
        info = os.lstat(directory / "tool.json")
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_ctime_ns)


# The swap failed AND the roll-back failed too: the only state where the
# operator has to act. It names the SHAPE of the rescue (a hidden backup
# directory beside the tool) rather than the path, keeping the category-only
# rule intact while still being actionable.
_ERROR_REVISE_UNRECOVERABLE = (
    "工具包置換失敗且無法還原，原工具已保留為工具目錄下的隱藏備份目錄，請手動處理。"  # noqa: RUF001
)

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
# * the base prompt tells the model to `. ./.env` when testing. There IS no
#   ``.env`` in a revise workspace (it is withheld precisely because its values
#   would trip the embedded-secret gate), so the addendum says where those values
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
- The package's `.env` has been WITHHELD from your workspace and the backend \
restores the original after you finish. Do not write one, do not invent \
placeholder values, and do not make your changes depend on rewriting it -- if \
this package has a `.env`, any you write is REPLACED by the preserved original. \
Those values are \
already exported into your run_shell environment under their own names, so you \
can still live-test the real API (e.g. `echo '{"query":"test"}' | python3 \
run.py`) without ever seeing them. If the user's feedback asks to CHANGE a \
secret value, say so in your summary -- that is done by hand, not here.
- Never write a secret value into any file. A package that embeds a known \
secret is REFUSED, and that refusal discards your whole revision.
- The AI summary file for this tool is written by the backend; do not create or \
edit one.

Finish exactly as described above, with one added rule: "tool_name" MUST be \
exactly {name}, because it names the installed package this revision replaces \
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


def _is_reserved_sidecar_name(filename: str) -> bool:
    """True for a filename inside the AI sidecar's RESERVED namespace (D40).

    Two shapes, and the temp one is not padding: ``tools._write_sidecar_atomic``
    publishes through ``mkstemp(prefix=f"{_AI_META_FILENAME}.", suffix=".tmp")``,
    so a leftover from an interrupted publish is a legitimate inhabitant of this
    namespace -- and therefore just as legitimate a thing for a builder to
    imitate. Matching the prefix+suffix pair (rather than the exact name only)
    means the strip covers the whole namespace the backend claims, not just the
    one filename an attacker would have to be naive enough to use.

    Matched CASE-INSENSITIVELY (R10-2), which is not pedantry on a project that
    supports macOS: the default macOS filesystem is case-INSENSITIVE, so a
    builder writing ``.AI_META.JSON`` creates the very file a later
    ``read_tool_meta`` opens as ``.ai_meta.json`` -- while a case-SENSITIVE
    match here would sail right past it and promote the forgery, reopening the
    exact choke-point bypass R7-1 closed. The comparison must therefore be at
    least as loose as the loosest filesystem this can run on; on a
    case-sensitive filesystem the only cost is deleting a differently-cased
    name a builder had no business writing either. ``casefold`` (not ``lower``)
    because it is the Unicode-correct full-case-folding operation, and these
    names are compared, never displayed.
    """
    folded = filename.casefold()
    reserved = tools._AI_META_FILENAME.casefold()
    return folded == reserved or (folded.startswith(reserved) and folded.endswith(".tmp"))


def _remove_reserved_sidecar_path(path: Path) -> None:
    """Delete ONE reserved-name entry, whatever kind of thing it turned out to be.

    ``unlink`` is the whole answer for the cases that matter (a regular file, a
    symlink of any target) and it removes the LINK rather than following it, so a
    ``.ai_meta.json -> /etc/passwd`` planted in staging costs its target nothing.
    ``missing_ok`` covers the entry vanishing between the walk and here.

    A real DIRECTORY at the reserved name needs ``rmtree`` (``unlink`` answers
    EISDIR). It is not a forged sidecar -- ``read_tool_meta`` refuses a
    non-regular file -- but it would permanently BRICK the package's summary:
    ``_write_sidecar_atomic``'s lstat gate refuses to publish over anything
    non-regular, so the install hook, every later regenerate, and 定版 would all
    fail forever on a package the operator has no API path to repair. The name is
    the backend's; nothing of the builder's may occupy it in any form.

    Raises ``OSError`` on a refusal, which is what makes the caller fail-closed.
    """
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    path.unlink(missing_ok=True)


def _reraise_walk_error(exc: OSError) -> None:
    """``os.walk``'s ``onerror`` callback, wired to make a scan failure FATAL (R8-2).

    ``os.walk`` defaults to ``onerror=None``, which means "SWALLOW any
    ``scandir``/``listdir`` failure and just skip that subtree" -- so a builder that
    ``chmod 000``s a directory holding a nested sidecar would otherwise sail through
    the walk with that subtree silently unvisited: the strip reports success, and
    the forged nested sidecar rides into the installed package. Passing this
    callback as ``onerror`` makes ``os.walk`` re-raise instead, so the surrounding
    ``try``/``except OSError`` in ``_strip_builder_sidecars`` sees it and fails
    closed. An unreadable subtree in a builder-produced package is either sabotage
    or breakage -- both are install-stopping, and silence was the only wrong
    answer.
    """
    raise exc


def _strip_builder_sidecars(staging: Path) -> str | None:
    """Delete every builder-written AI sidecar from staging; None = ok (R7-1).

    Blocking (runs inside ``_promote_staging``). The sidecar is a BACKEND-AUTHORED
    artifact whose only legitimate writer is ``tools.write_tool_meta``; a builder
    session has real shell capability (D21), so it can write one too, and doing so
    is a complete bypass of that choke point rather than a cosmetic liberty:

    * the builder writes a staging ``.env`` holding a value NOBODY has registered
      yet -- ``known_secret_values`` scans installed packages only (it skips the
      dot-prefixed ``.staging`` shell), so the value is unknown for the whole
      build window;
    * it writes ``.ai_meta.json`` = ``{"summary": "<that same value>", "status":
      "final"}``. ``validate_package``'s embedded-secret sweep cannot see a secret
      it does not know, so the package passes;
    * promote moves the whole staging directory, sidecar included;
    * and then the install hook's OWN protections finish the job for the
      attacker: ``store_summary_meta`` reads ``status == "final"``, honors 定版,
      and declines to overwrite -- so the forged, finalized, secret-bearing
      sidecar is what every later GET/list serves, verbatim, forever.

    DELETION, not rejection, is the adjudicated answer, and the distinction is
    real: the PACKAGE may be perfectly good work. The sidecar is decoration the
    install hook regenerates through the proper choke point seconds later, so
    dropping it costs the operator nothing, while failing the install would
    punish them for something the model did unasked.

    It runs BEFORE ``validate_package`` because validation must judge exactly what
    will SHIP -- a gate that vets a file the promote then deletes (or, worse,
    keeps) is describing a package that never existed.

    EVERY DEPTH, via ``os.walk``, not just the package root. A nested
    ``sub/.ai_meta.json`` is inert for ``read_tool_meta`` (which only ever reads
    the package ROOT), so this is not the smuggling path -- but it still rides
    into the installed package, where a future revise copies it into a fresh
    staging build that ``validate_package``'s embedded-secret gate DOES scan
    against the by-then-registered value: a planted nested copy would brick every
    later revise of that tool with a rejection naming a file the operator never
    wrote. The walk is a handful of stats over a small tree; the sidecar's name
    belongs to the backend at every depth, and saying so once here is cheaper
    than a caveat every future reader has to re-derive.

    Fail-closed: any ``OSError`` (weird perms, an immutable attribute, a
    directory that will not empty) cancels the install with a category-only
    zh-TW error. Shipping the forged file is the one unacceptable outcome, and
    "we could not delete it" must never degrade into "so we kept it". This now
    ALSO covers the WALK itself failing to scan a subtree (R8-2): ``os.walk``'s
    default ``onerror=None`` swallows a ``scandir``/``listdir`` OSError and just
    skips that subtree, so an unreadable directory (permissions, or anything
    else that blocks listing it) would otherwise vanish from the walk entirely --
    the loop finishes, this function returns None, and any sidecar hidden inside
    ships unexamined. ``_reraise_walk_error`` turns that default silence into the
    same fatal ``OSError`` a failed deletion already raises below, so incomplete
    traversal and failed deletion share one fail-closed outcome.
    """
    try:
        for dirpath, dirnames, filenames in os.walk(staging, onerror=_reraise_walk_error):
            here = Path(dirpath)
            # dirnames is walked over a COPY and pruned in place: a symlink-to-
            # directory at the reserved name lands here (os.walk classifies by a
            # following is_dir), and a pruned entry must not then be descended
            # into after it has been removed.
            for dirname in list(dirnames):
                if _is_reserved_sidecar_name(dirname):
                    dirnames.remove(dirname)
                    _remove_reserved_sidecar_path(here / dirname)
            for filename in filenames:
                if _is_reserved_sidecar_name(filename):
                    _remove_reserved_sidecar_path(here / filename)
    except OSError:
        return _ERROR_SIDECAR_STRIP
    return None


def _verify_staging_root(staging: Path, base: Path) -> str | None:
    """Re-verify ``staging`` is still the real, contained directory it was given;
    None = ok (R8-1).

    Called FIRST in ``_promote_staging``, before ANY destructive traversal --
    including the sidecar strip's ``os.walk`` -- and therefore before validate and
    move too. The builder's ``run_shell`` runs with the SERVICE'S OWN uid and
    merely STARTS in staging (D21: deliberately unjailed, unlike the file
    meta-tools), so between the LLM session ending and promote running, a builder
    command can ``mv`` staging ASIDE and plant a SYMLINK at the original staging
    path pointing at ``base`` (the real, live tools directory) itself.
    ``_strip_builder_sidecars``'s ``os.walk`` never checked its OWN root for a
    symlink -- R7-1 only prunes a symlink found DURING the walk -- so unchecked it
    would walk every INSTALLED package and delete its ``.ai_meta.json``,
    destroying finalized summaries and their only origin copies, before
    ``validate_package`` ever ran.

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
    the service's uid) -- just one step earlier: check-then-WALK instead of
    check-then-move. A swap landing in the instant between this check and the
    strip's first ``os.walk`` syscall is still possible in principle; what this
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
    staging: Path,
    name: str,
    base: Path,
    secret_name: str | None = None,
    secret_value: str | None = None,
) -> str | None:
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

    ``staging`` is re-verified FIRST of all (``_verify_staging_root``, R8-1),
    before even the sidecar strip: the builder's ``run_shell`` can rename staging
    away and plant a symlink at its original path pointing at ``base`` itself, and
    the strip's ``os.walk`` would then delete every installed package's sidecar
    before validation ever ran. This is the SAME check-then-act residual this
    docstring already accepts above for the exists-check -- see
    ``_verify_staging_root`` -- just one step earlier.

    The form secret (D36) is written into the staged ``.env`` AFTER
    ``validate_package`` (which judges exactly what the BUILDER produced) and
    AFTER the name-free check (so we never touch a package we will not install),
    but BEFORE the move -- with ``_inject_secret_into_env``'s own post-append
    size guard, since ``validate_package`` never saw that line. This ordering is
    what keeps ``validate_package``'s view consistent: it always vets the
    LLM-authored package as-is, and the secret is a backend addition layered on
    top and gated separately.

    The AI sidecar is stripped NEXT, ahead of validation (R7-1): it is
    backend-authored metadata, so a builder-written one is a forgery that bypasses
    the ``write_tool_meta`` choke point entirely -- see ``_strip_builder_sidecars``
    for the full attack chain and for why validation must run on exactly what will
    ship. This is the ONLY file the promote removes; every other file the builder
    produced rides into the package untouched, exactly as before.
    """
    root_error = _verify_staging_root(staging, base)
    if root_error is not None:
        return root_error
    strip_error = _strip_builder_sidecars(staging)
    if strip_error is not None:
        return strip_error
    error = tools.validate_package(staging, expected_name=name)
    if error is not None:
        return f"工具包驗證失敗：{error}"  # noqa: RUF001
    target = base / name
    if target.exists():
        return _ERROR_NAME_TAKEN
    if secret_name and secret_value:
        inject_error = _inject_secret_into_env(staging / ".env", secret_name, secret_value)
        if inject_error is not None:
            return inject_error
    try:
        base.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(target))
    except OSError as exc:
        return f"工具包搬移失敗（{type(exc).__name__}）。"  # noqa: RUF001
    return None


def _preserve_env_file(
    target: Path, staging: Path, *, existed_at_start: bool, registered: list[str]
) -> str | None:
    """Copy the LIVE package's ``.env`` into ``staging`` BYTE-FOR-BYTE; None = ok (D40).

    Blocking (runs inside ``_promote_staging_replace``). This IS the whole
    preservation mechanism, and it is a FILE COPY rather than the text round-trip
    r1 kept (R2-1). That round-trip's justification was that every consumer reads
    this file through the same lossy decode, so the normalization it performed was
    unobservable -- and that premise is simply FALSE: the runtime invokes a tool
    with its PACKAGE DIRECTORY as the working directory, so the tool's own entry
    can ``open(".env", "rb")`` and hash it, diff it, or parse CRLF itself. A revise
    about pagination has no business rewriting a file it was never asked to touch.
    ``shutil.copy2`` never decodes anything: the bytes, the mode and the mtime all
    survive, so a CRLF-authored or non-utf-8 ``.env`` comes through unchanged.

    The direction is LIVE -> staging, which is why this is cheap to be wrong
    about: the live package is only READ here, and everything it writes lands in
    STAGING, which nothing outside this build observes -- so a swap that fails
    afterwards costs nothing, and the original file is still the original file
    rather than a rewrite of it. It must run AFTER the target existence/symlink
    checks, because it reads through ``target`` and the caller has to have
    established that ``target`` is the real installed directory first. It now runs
    BEFORE the 定版 re-check, which is the point of R4-1: this copy is UNBOUNDED IN
    TIME (an operator can put an arbitrarily large regular file at ``.env``
    mid-session), so with it last, a finalization landing DURING the copy was
    missed by a gate that had already run, and the swap then overwrote a finalized
    package. The accepted check-then-act residual covers INSTANTS, not a copy of
    unbounded duration -- moving this ahead of the gate is what keeps the two the
    same size, and the size ceiling below is what keeps THIS step's own duration
    bounded (see there).

    Both ends are guarded by an ``lstat``, mirroring the pre-write ``lstat``
    ``tools._write_sidecar_atomic`` makes and the ``O_NOFOLLOW`` + ``S_ISREG`` gate
    of the shared file helpers:

    * SOURCE -- ``FileNotFoundError`` is the ONLY "there is no ``.env``" answer
      (R2-3). Every other ``OSError`` (EIO, ESTALE, EACCES, ...) is a failure to
      LOOK, not evidence of absence, and treating it as absence would ship a
      revision whose credentials silently vanished. A non-regular source (a
      symlink or FIFO raced in after the entry read vetted a regular file) is
      refused rather than copied through, exactly as the bounded reader's
      ``O_NOFOLLOW`` refuses it. The same ``lstat``'s ``st_size`` also CAPS the
      copy at ``tools._ENV_FILE_MAX_BYTES`` (R4-1) -- the ceiling
      ``_read_env_for_values`` already applies to this very file at the session's
      start, and the one ``tools.validate_package`` applies to a staged ``.env``.
      An over-cap ``.env`` could never have been parsed for its values anyway
      (``tools._load_tool_dotenv`` degrades it to no-env at runtime, so the tool is
      not even reading it), so copying it would publish a file the rest of the
      system rejects, and would spend an unbounded stretch of the pre-swap window
      doing it. Bounding it leaves only the instant between this ``lstat`` and the
      ``copy2`` -- a swap-in of a bigger file right there is the ordinary
      check-then-act residual the standing adjudication accepts, the same size as
      the ones the target checks above carry;
    * DESTINATION -- ENOENT is the ordinary case (the copy excluded it) and a
      regular file is the builder's own ``.env``, which the prompt's contract says
      we overwrite. Anything else refuses, and that check is load-bearing rather
      than symmetry: ``copy2`` FOLLOWS its destination, so a symlink planted at
      ``<staging>/.env`` by the unjailed ``run_shell`` (D21) would take the live
      credentials through it and out of staging, and a DIRECTORY there would send
      the file to ``<staging>/.env/.env`` and ship a package with no ``.env`` at
      all. This is exactly the refusal set ``tools._write_regular_file``'s
      ``O_NOFOLLOW`` + ``S_ISREG`` gate gave the text write this replaces -- no
      wider (a hardlink passes both, then and now, and is D21's accepted
      same-uid residual), no narrower.

    Every failure is a category-only string that aborts BEFORE the swap, so a
    package we could not preserve the ``.env`` of is never published.

    The file copied is the one on disk at THIS instant, not the snapshot the
    session's entry read took. That is deliberate: an operator who edits the live
    ``.env`` mid-session keeps their edit instead of having it reverted by a
    revise. But it also means the ENTRY gates -- the spelling policy, the
    redactor's length floor, the byte ceiling -- vetted a file that may no longer
    exist, so this re-reads the source and re-runs ``_unmaskable_env_error`` on
    the bytes it is ACTUALLY about to ship (R7-2). Without it, a package with no
    ``.env`` (or a compliant one) at the session's start could gain a
    non-compliant one mid-session and we would PUBLISH it, having never applied
    the policy to it at all: the entry gate answers for a file, not for a path,
    and this is the second place the same question has to be asked because it is
    the place the answer is acted on. A refusal here is the same category-only
    string the entry gate returns, and it aborts before the swap like every other
    failure below.

    The values found by that re-read are REGISTERED as in-flight secrets before
    the copy, and appended to ``registered`` -- the session's own list, whose
    ``finally`` discards every value it holds. Registering at the point of
    discovery and recording it in the SAME breath is what makes the registration
    leak-proof: no return path, error or otherwise, can lose track of a value we
    have made the process redact. A value the operator added mid-session was never
    exported into ``run_shell`` BY US (only the entry-read values were) -- though a
    builder that read the file for itself has already seen it, which is the
    residual below -- but it IS about to be shipped, and everything after the swap --
    the outcome summary, the regenerated sidecar, the AI 日誌 record of that
    summary session -- is written while the old package sits in a DOT-prefixed
    backup that ``known_secret_values`` skips. That is the same masking gap the
    entry registration exists to cover, just entered from the other end.

    What this does NOT fix is the IN-SESSION read: the builder's unjailed
    ``run_shell`` (D21) can ``cat`` a credentials file the operator creates DURING
    the session, and ``llm_log`` redacts at STORAGE time, so a value that becomes
    known here cannot retroactively mask records already written. That is recorded
    as an accepted, unpreventable residual (裁決紀錄 #6) rather than papered over:
    the closable half is "never SHIP what the guards would refuse", and this is it.

    ``existed_at_start`` is what the session OBSERVED when it read the values
    (``_read_env_for_values``), and it is threaded all the way down here because
    "there is no ``.env`` right now" is TWO different situations (R3-1):

    * it never had one -- the builder's own ``.env`` SHIPS, unchanged, exactly as
      it does on an install where writing ``.env`` from the operator's
      instructions is part of the contract. Deleting it would leave a revise
      unable to act on "store the key in .env" feedback, and it smuggles nothing:
      a value nobody registered is one the embedded-secret gate has nothing to
      compare against either way. This is the standing acceptance, and it stays;
    * it HAD one and it is gone now -- the operator deleted the credentials file
      DURING the session. That deletion is a deliberate act on the live package,
      while the builder's file is an artifact of a prompt that told it not to
      write one at all; letting a placeholder silently take a deleted credential
      file's place is precisely the failure this branch exists to stop. So the
      staged ``.env`` is REMOVED and the published package has none, which is what
      the deletion asked for. If that removal fails -- including a directory or
      anything else ``os.remove`` will not take at that name -- this returns a
      category-only error and the swap never runs, because "we could not make the
      revision match" must not resolve to "publish the placeholder anyway".

    The MIRROR case needs no flag and gets none: a ``.env`` that did NOT exist at
    the start but is there now was created by the operator mid-session, and the
    ordinary copy below preserves it -- the same "the file on disk at THIS instant
    wins" rule the paragraph above states, applied in the other direction.
    """
    source = target / ".env"
    source_stat: os.stat_result | None
    try:
        source_stat = os.lstat(source)
    except FileNotFoundError:
        source_stat = None  # nothing to copy -- but WHY decides what happens next
    except OSError:
        return _ERROR_REVISE_ENV_UNREADABLE
    if source_stat is None:
        if not existed_at_start:
            return None  # never had one: the builder's .env ships (see the docstring)
        try:
            os.remove(staging / ".env")
        except FileNotFoundError:
            return None  # the builder wrote none either -- nothing to undo
        except OSError:
            # ``os.remove`` never follows a symlink, so a planted link is unlinked
            # rather than followed; a DIRECTORY at that name lands here instead,
            # and refusing beats a destructive traversal on something this function
            # neither created nor verified (the same rule the roll-back applies).
            return _ERROR_REVISE_ENV_DISCARD
        return None
    if not stat.S_ISREG(source_stat.st_mode):
        return _ERROR_REVISE_ENV_UNREADABLE
    if source_stat.st_size > tools._ENV_FILE_MAX_BYTES:
        # Same ceiling, same file, same session (see the docstring): over it, the
        # values were never parseable and the copy would be the one unbounded step
        # left before the swap.
        return _ERROR_REVISE_ENV_TOO_LARGE
    # The SAME policy the session's entry applied, re-asked of the bytes that are
    # actually about to ship (R7-2). The reader, the cap and the non-empty filter
    # are the entry read's, so the two gates cannot answer differently about one
    # file; only the FILE can have changed. The read and the ``copy2`` below are
    # two opens an instant apart -- the ordinary check-then-act residual this
    # module accepts, the same one the ``lstat`` above already carries, and not
    # something a single lossy decode could close (this text is decoded with
    # ``errors="replace"``, so writing it back would not be a byte copy).
    text = tools._read_regular_file_capped(source, tools._ENV_FILE_MAX_BYTES)
    if text is None:
        return _ERROR_REVISE_ENV_UNREADABLE
    if len(text) > tools._ENV_FILE_MAX_BYTES:
        # Grew between the ``lstat`` and this read -- the same miniature TOCTOU
        # backstop ``_read_env_for_values`` keeps for the same reason.
        return _ERROR_REVISE_ENV_TOO_LARGE
    values = {key: value for key, value in tools._parse_dotenv_text(text).items() if value}
    mask_error = _unmaskable_env_error(text, values)
    if mask_error is not None:
        return mask_error
    # Registered BEFORE the copy and recorded in the session's own list in the
    # same breath, so the ``finally`` that ends the revise discards exactly what
    # was registered (see the docstring). ``register_inflight_secret`` and the
    # discard are both idempotent, so a value the entry read already registered
    # costs nothing here.
    for value in values.values():
        tools.register_inflight_secret(value)
        registered.append(value)
    destination = staging / ".env"
    try:
        destination_mode: int | None = os.lstat(destination).st_mode
    except FileNotFoundError:
        destination_mode = None  # nothing there yet: the ordinary first-write case
    except OSError:
        return _ERROR_REVISE_ENV_RESTORE
    if destination_mode is not None and not stat.S_ISREG(destination_mode):
        return _ERROR_REVISE_ENV_RESTORE
    try:
        shutil.copy2(source, destination)
    except OSError:
        return _ERROR_REVISE_ENV_RESTORE
    return None


def _promote_staging_replace(
    staging: Path,
    name: str,
    base: Path,
    *,
    env_existed_at_start: bool,
    package_identity: tuple[int, int, int] | None,
    registered: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Swap a revised build in for the INSTALLED ``<base>/<name>`` (D40).

    Returns ``(origin, error)``: the error is the usual category-only zh-TW string
    (None = the revision is live), and the origin is the OLD sidecar's
    ``origin`` block, read here rather than by the caller for the reason R4-2
    gives -- see the 定版 gate below, which is the read it comes from. Both are
    None on a refusal, and ``(None, None)`` on a success whose package simply had
    no origin to inherit; the caller only consults the origin after the error is
    None. The ``(value, error)`` shape mirrors ``_read_env_for_values``, the
    module's other "do a thing, hand back what only that thing could learn".

    Blocking (runs via ``run_in_threadpool``). A SEPARATE function rather than a
    flag on ``_promote_staging``, because the two have opposite preconditions on
    the same path: install requires the target to be ABSENT (its exists-check is
    load-bearing -- ``shutil.move`` onto an existing directory NESTS instead of
    failing), revise requires it to be PRESENT (there is nothing to revise
    otherwise, and creating it here would turn a racing delete into a silent
    reinstall). Folding both into one function would mean a boolean deciding
    which of two contradictory checks runs -- and the gates they share are shared
    by CALLING them, which is what this does:

    1. ``_verify_staging_root`` FIRST of all (R8-1), before any destructive
       traversal -- the builder's run_shell is unjailed, so the same "staging
       moved aside, symlink planted at its path" attack applies here;
    2. ``_strip_builder_sidecars`` (R7-1) -- the sidecar is backend-authored, and
       this path regenerates one moments later anyway;
    3. ``tools.validate_package`` -- the revised package must clear exactly the
       gates a fresh install does. A revision that no longer validates NEVER
       reaches the swap below, so the installed tool keeps running.

    Then the two checks that are specific to replacing something:

    * the target must still be a DIRECTORY -- a delete landing during the build
      is answered with ``_ERROR_REVISE_TARGET_MISSING``, never by installing the
      revision as a new tool;
    * and it must not be a SYMLINK. ``is_symlink`` does not follow the final
      component, mirroring ``tools._resolve_package_dir_no_alias``'s refusal (the
      resolve that admitted this name erases the alias/real distinction): the
      rename below would otherwise move the LINK aside and leave the real
      package orphaned under a name nothing addresses.

    The live ``.env`` is then copied into staging (``_preserve_env_file``) AFTER
    validation and BEFORE the 定版 gate -- the same slot, for the same reason, as
    the install's ``_inject_secret_into_env``: ``validate_package`` judges what the
    BUILDER produced (and its embedded-secret gate would reject the live values on
    sight), and the backend's own bytes are layered on top of a package that has
    already passed. It overwrites whatever ``.env`` the builder wrote, which is
    the prompt's stated contract. It is a BYTE copy of the live file, not a
    re-encoding of text read earlier (R2-1) -- see that helper for why the file's
    exact bytes are observable and therefore not ours to normalize, and for why
    reading from the live package here is safe. The copy is size-gated there
    against the SAME ceiling the session's entry read and ``validate_package`` use
    (R4-1); the earlier claim that no size check was needed rested on the live
    ``.env`` still being the file whose values we parsed at the start, and a
    mid-session replacement is exactly what this function must survive. That same
    reasoning is why the helper now re-runs the WHOLE ``.env`` policy on the bytes
    it is about to copy (R7-2): the entry gates answered for a file that may have
    been replaced since, and this is the step that decides what SHIPS. It also
    registers what it finds there, appending to ``registered`` -- the caller's own
    in-flight list, which is why that list is threaded down instead of the values
    being handed back: a value is made redactable and recorded in the same breath,
    so no error path can leave one registered with nobody to discard it.

    It runs BEFORE the 定版 gate rather than after it, and the order is the
    decision (R4-1): the copy is the only pre-swap step whose duration is
    UNBOUNDED FROM OUTSIDE -- the ceiling above bounds the bytes, but an operator
    still chooses them -- so with it last, a finalization landing WHILE it ran was
    invisible to a gate that had already passed, and the swap then destroyed a
    finalized package. Putting it first costs nothing, because everything it does
    lands in STAGING, which no other actor reads: if the gate refuses afterwards,
    the staged ``.env`` dies with the abandoned build. What it depends on --
    ``target`` being a real, non-symlink installed directory -- is established by
    the two checks ABOVE it, which stay where they are.

    A package that NEVER had a ``.env`` preserves nothing, and then a
    builder-written one SHIPS -- deliberately, and identically to an install,
    where writing ``.env`` from the user's instructions is part of the contract.
    A package that HAD one which vanished mid-session is the opposite case and is
    told apart by ``env_existed_at_start``, which the caller carries down from its
    own entry read: there the staged ``.env`` is DROPPED instead, so a placeholder
    never takes a deleted credential file's place (R3-1, see
    ``_preserve_env_file``).

    The swap itself is rename-aside, move-in, drop-the-backup:

    * the old package is renamed to a DOT-prefixed sibling
      ``.{name}.bak-<uuid>``. Dot-prefixed is load-bearing, not cosmetic:
      ``tools._scan_all`` skips hidden entries, so the backup can never surface
      in ``GET /api/tools`` as a phantom duplicate during the swap window (and
      the same convention keeps ``known_secret_values`` from re-reading it --
      which is exactly why ``run_revise`` registers those values in-flight);
    * a plain ``os.rename`` then puts the revision at the now-free name.
      DELIBERATELY not ``shutil.move`` (R1-2): move falls back to a
      copytree-then-delete whenever ``os.rename`` raises -- not only across
      devices, since it catches every ``OSError`` -- so the publish would not be
      atomic. With the old package already parked in the backup, a partial copy
      leaves a HALF-BUILT directory at the tool's name, and the roll-back's
      ``os.rename(backup, target)`` then fails because the name is occupied: a
      half-replaced tool plus a hidden backup, the exact state
      ``_ERROR_REVISE_UNRECOVERABLE`` exists to make rare. The same-filesystem
      assumption ``rename`` needs is STRUCTURAL here, not a hope: staging is
      ``<tools_dir>/.staging/<uuid>`` and the target is ``<tools_dir>/<name>``,
      and ``_verify_staging_root`` above has already PROVEN staging resolves
      inside that same ``<tools_dir>`` shell -- so the two live under one root and
      EXDEV cannot arise from the paths themselves (only from a mount an operator
      planted inside their own tools directory, which fails loudly and rolls back
      like any other error);
    * a publish failure is ROLLED BACK by renaming the backup home. If even that
      fails, the operator is told a hidden backup is what to rescue
      (``_ERROR_REVISE_UNRECOVERABLE``) -- the one outcome that needs a human;
    * success drops the backup with ``ignore_errors`` (the revision is live by
      then; a leftover backup is litter, not a failure).

    Every failure is a category-only zh-TW string -- never a path, never a value
    -- like every other outcome error in this module. The check-then-act windows
    (between the target checks and the rename, and between the two renames) are
    the SAME accepted residual ``_promote_staging``'s docstring already names for
    its exists-check: a race against a second actor holding the service's uid, on
    a single-user local tool, and no new standard is invented for it here. Every
    one of them is an INSTANT between two syscalls, which is the property R4-1
    restored by moving the one step that was not (the ``.env`` copy) off the end.
    """
    root_error = _verify_staging_root(staging, base)
    if root_error is not None:
        return None, root_error
    strip_error = _strip_builder_sidecars(staging)
    if strip_error is not None:
        return None, strip_error
    error = tools.validate_package(staging, expected_name=name)
    if error is not None:
        return None, f"工具包驗證失敗：{error}"  # noqa: RUF001
    target = base / name
    if target.is_symlink():
        return None, _ERROR_REVISE_TARGET_ALIAS
    if not target.is_dir():
        return None, _ERROR_REVISE_TARGET_MISSING
    # ... and it must still be the SAME directory the session copied from (R10-1).
    # "A directory of that name exists" is not the question: an operator can delete
    # and reinstall -- or atomically replace -- the package during the MINUTES an
    # LLM session runs, and publishing a revision of the old snapshot over the new
    # package would rename the new one aside and then delete it, silently taking
    # whatever the operator just put there. The identity is the directory's own
    # (st_dev, st_ino) captured when the session read the package; a replacement is
    # a NEW directory and compares different, while ordinary in-place edits keep
    # the inode and still revise (refusing those would make revise unusable, and
    # they are not the loss this guards). None means the caller could not capture
    # one, and then this check cannot speak -- the other gates still do.
    if package_identity is not None and _package_identity(target) != package_identity:
        return None, _ERROR_REVISE_TARGET_REPLACED
    # The live ``.env`` is copied into staging BEFORE the 定版 gate below, and the
    # order is the fix R4-1 asked for: this is the only pre-swap step that can take
    # an operator-chosen amount of TIME, and a gate that runs before it cannot see a
    # finalization that lands during it. Everything it writes goes into STAGING, so
    # running it early is free -- an abandoned build takes the staged ``.env`` with
    # it. See ``_preserve_env_file`` for the size ceiling that bounds it and for
    # why the target checks above are all it depends on.
    env_error = _preserve_env_file(
        target, staging, existed_at_start=env_existed_at_start, registered=registered
    )
    if env_error is not None:
        return None, env_error
    # 定版 is re-checked HERE, at the last moment before the swap, not only at
    # the entry gates -- the same store-time re-check ``tools.store_summary_meta``
    # makes for regenerate (D40 r2), and for the same reason at a much longer
    # timescale: a revise session runs for MINUTES, so a user finalizing during
    # one is ordinary, not exotic. Without this the swap would replace the whole
    # package -- including the finalized sidecar's frozen text, which the revise
    # does not even carry forward (the sidecar is excluded from staging and
    # regenerated as a draft afterwards), so 定版 would be silently undone by a
    # session that started before it. The two earlier checks stay where they are:
    # the ROUTE's (on the TOTAL reader) is what answers 409 without queueing a job
    # at all, and ``run_revise``'s entry gate (on THIS strict one, R6-3) is what
    # stops an already-corrupt sidecar from buying a whole session before arriving
    # here to be refused anyway.
    #
    # Through ``summary_status_or_unknown``, not ``summary_status``, and the whole
    # point is the difference (R3-2): ``summary_status`` folds "no sidecar" and
    # "there IS one but it is unreadable/corrupt" into the SAME None, so a gate
    # keyed on an explicit "final" is FAIL-OPEN -- a user finalizes mid-session,
    # the sidecar then hits a transient read error, and this gate waves the swap
    # through and destroys the frozen text it exists to protect. UNKNOWN therefore
    # refuses exactly like "final". The two get DIFFERENT messages because their
    # remedies differ (see ``_ERROR_REVISE_SUMMARY_UNREADABLE``).
    #
    # It is the LAST pre-swap step (R4-1): everything after it is a rename, so the
    # window between deciding "not finalized" and acting on it is the instant
    # between two syscalls -- the residual class this module already accepts, and
    # the size a check-then-act gate has to be to mean anything.
    #
    # The SAME read also yields the origin (R4-2), which is why this reader hands
    # its meta back. The origin -- the OpenAPI url and the operator's original
    # install instructions -- lives ONLY in this sidecar, and the swap below
    # destroys it; the post-swap summary inherits it so a revised tool still knows
    # what it was built from. Reading it separately through the TOTAL
    # ``read_tool_meta`` (all errors -> None) is what R4-2 found: a transient EIO
    # answered "no origin", the strict gate then read the file fine, and the swap
    # published a regenerated sidecar with the only copy of that context gone for
    # good. One trusted read cannot disagree with itself. There is deliberately no
    # unreadable-sidecar branch on the origin side either: this gate has already
    # refused every shape in which the file could not be read, so by the line that
    # narrows the origin there is nothing left to discriminate.
    status, meta = tools.summary_status_or_unknown(target)
    if status == "final":
        return None, _ERROR_REVISE_FINALIZED
    if status == tools._SUMMARY_STATUS_UNKNOWN:
        return None, _ERROR_REVISE_SUMMARY_UNREADABLE
    origin = _existing_origin(meta)
    backup = base / f".{name}.bak-{uuid4().hex}"
    try:
        os.rename(target, backup)
    except OSError as exc:
        return None, f"工具包置換失敗（{type(exc).__name__}）。"  # noqa: RUF001
    try:
        os.rename(staging, target)
    except Exception as exc:
        # TOTAL, unlike the OSError catches everywhere else in this module, and
        # for one reason: between the rename above and this one the tool DOES NOT
        # EXIST. Anything that escapes here leaves the operator's working tool
        # gone with only a hidden backup to show for it, so the roll-back has to
        # run for EVERY failure shape, not just the filesystem-shaped ones.
        # str(exc) is never surfaced -- the message stays category-only.
        #
        # The roll-back does NOT clear ``target`` first, and that is a decision,
        # not an omission (R1-2): a rename that RAISED moved nothing, so the name
        # is free -- the half-written target that made clearing it look necessary
        # was ``shutil.move``'s copy fallback, which is exactly what the line above
        # no longer is. The ONLY way ``target`` exists here is that a concurrent
        # actor with the service's uid created it in the instant since our own
        # rename-aside, and then ``rmtree``-ing it would be a destructive traversal
        # on a directory this function neither created nor verified (the
        # ``_verify_staging_root`` gate proves things about STAGING, nothing about
        # this path) -- destroying a third party's data to reclaim a name. The
        # rename below fails with ENOTEMPTY instead and the operator is told where
        # their backup is, which is the honest answer to "two writers, one name".
        try:
            os.rename(backup, target)
        except Exception:
            return None, _ERROR_REVISE_UNRECOVERABLE
        return None, f"工具包置換失敗（{type(exc).__name__}），原工具已還原。"  # noqa: RUF001
    shutil.rmtree(backup, ignore_errors=True)
    return origin, None


def _cleanup_staging(staging: Path, base: Path) -> None:
    """Remove the session's staging dir (if the move did not consume it) and
    drop the ``.staging`` shell when this was the last build in flight.

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
    """
    with contextlib.suppress(Exception):
        if _verify_staging_root(staging, base) is not None:
            return
        if staging.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
    with contextlib.suppress(OSError):
        staging.parent.rmdir()


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

    staging = base / _STAGING_DIRNAME / uuid4().hex
    try:
        await run_in_threadpool(staging.mkdir, parents=True)
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
                tools=_build_meta_tools(staging, secret_env=secret_env),
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

        if llm_error is not None:
            return InstallOutcome(ok=False, error=llm_error, llm_log_id=llm_log_id)
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
            )

        promote_error = await run_in_threadpool(
            _promote_staging, staging, result.tool_name, base, secret_name, secret_value
        )
        if promote_error is not None:
            return InstallOutcome(
                ok=False,
                tool_name=result.tool_name,
                summary=summary,
                error=promote_error,
                llm_log_id=llm_log_id,
            )
        # D40: the package is INSTALLED as of the line above -- everything from
        # here on is decoration. Generate its AI summary sidecar while we still
        # hold the install's context (the OpenAPI url and instructions are
        # captured NOWHERE else, so this is the only chance to persist them for
        # a later revise session), and while the in-flight secret is still
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
            result.tool_name,
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
        )
    finally:
        # Discard the in-flight secret first (its .env now carries it post-move,
        # so redaction continues via the .env scan), then clean staging. A
        # successful move consumed the staging dir; cleanup then only drops the
        # (possibly empty) .staging shell. Every other exit removes the build.
        if secret_value:
            tools.discard_inflight_secret(secret_value)
        await run_in_threadpool(_cleanup_staging, staging, base)


# --- the revise run -----------------------------------------------------------


def _is_preserved_env_name(root: Path, filename: str, *, exact_present: bool) -> bool:
    """True when ``<root>/<filename>`` IS the managed ``<root>/.env`` (D40).

    The exact name always is. A CASEFOLD variant (``.ENV``, ``.Env``) is excluded
    only when the DIRECTORY LISTING it came from does NOT also carry the exact
    ``.env`` (``exact_present``) AND it is the same entry ``<root>/.env`` opens.

    The LISTING is the discriminator (R6-2), and the inode alone is NOT. The r5
    rule asked only "same ``st_dev``/``st_ino`` as ``<root>/.env``", and on a
    case-SENSITIVE filesystem ``.env`` and ``.ENV`` can be HARD LINKS: two
    distinct directory entries sharing one inode. Both were then held back from
    the copy while ``_preserve_env_file`` restores only the exact ``.env``, so
    after a successful publish and the backup drop ``.ENV`` was simply GONE --
    which is the very failure R5-2 set out to remove, reappearing through the test
    it chose. The question was never "same inode"; it is "is this the same
    DIRECTORY ENTRY", and the listing answers it outright:

    * a case-INSENSITIVE filesystem cannot hold two entries differing only in
      case, so its listing carries exactly ONE of them (in whatever case it is
      stored) and ``<root>/.env`` opens that one. ``exact_present`` is False, the
      same-entry test is True, and the live credentials stay out of staging --
      copying them in would make ``validate_package``'s embedded-secret gate
      reject every revise of that tool, permanently, naming a file the operator
      never wrote;
    * a case-SENSITIVE one holding both carries BOTH names in the listing, hard
      link or not. ``exact_present`` is True, so the variant is ORDINARY package
      content and is copied -- a tool whose entry runs with the package directory
      as its cwd may read it for its own reasons -- while the exact ``.env`` is
      still withheld by the branch above. A blind casefold match (r1) and the
      inode test (r5) both dropped it, and since only the exact ``.env`` is ever
      restored, an unrelated revise DELETED it and then validated the mutilated
      package as fine: the failure R2-2 removed for nested ``.env`` files,
      surviving at the root under a different name;
    * a case-SENSITIVE one holding ONLY the variant has no ``<root>/.env`` to be
      the same entry as, so it is copied and the package counts as having no
      managed ``.env`` at all -- which is exactly what ``tools._load_tool_dotenv``
      finds when it opens the exact name there.

    ``os.lstat`` rather than ``os.stat``/``os.path.samefile`` on purpose, and it
    stays that way (R5-2's reason survives R6-2's change): a SYMLINK named ``.ENV``
    pointing at ``.env`` compares EQUAL under a FOLLOWING stat, and excluding a
    distinct entry that is never restored is this whole finding in miniature. lstat
    keeps the link a link, ``copytree(symlinks=True)`` carries it across as one,
    and it resolves again the moment ``_preserve_env_file`` puts ``.env`` back.
    What the stat still buys, now that the listing decides the case question, is
    the confirmation that ``<root>/.env`` -- the exact path every other part of
    this system opens -- really does resolve to THIS entry before we withhold it.

    Both stats are guarded: a name that vanished between ``scandir`` and here, or
    a root with no ``.env`` at all, is simply NOT the same file, so it is copied.
    That is the safe direction -- the alternative is deleting package content on
    the strength of a stat that failed -- and the credential file itself cannot
    slip through it, because the exact-name branch above never stats anything.

    ``exact_present`` is a REQUIRED keyword-only argument for the same reason
    ``_preserve_env_file``'s ``existed_at_start`` is (R3-1): a default would be a
    silent wrong branch waiting for the first caller that forgets to pass it.

    Only IDENTITY is decided here; WHERE it counts is ``_revise_copy_ignore``'s
    job, and it counts only at the package root (R2-2).
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
    """``copytree``'s ignore callback: the names a revise copy must leave behind.

    Bound to the package ``root`` by the caller (``partial``), because the two
    namespaces it drops have DIFFERENT depths:

    * the ROOT ``.env`` only -- MANDATORY there, and identified by this very
      LISTING plus a non-following same-entry test rather than by spelling
      (``_is_preserved_env_name``, R6-2 narrowing R5-2). That one file's values
      are in ``known_secret_values``, so copying it would be rejected outright by
      ``validate_package``'s embedded-secret gate, and the backend copies the live
      file back after validation instead (``_preserve_env_file``);
    * the sidecar's reserved namespace at EVERY depth -- a backend-authored
      namespace no package content may inhabit, regenerated through the
      ``write_tool_meta`` choke point after the swap, and one
      ``_strip_builder_sidecars`` would delete from staging anyway.

    A NESTED ``.env`` is ordinary package content and is COPIED (R2-2). The r1
    rule dropped every ``.env``-casefolded name at every depth on the theory that
    only the root one is ever loaded -- true of the RUNTIME's env injection, and
    irrelevant to the tool's own code, which runs with the package directory as
    its cwd and may perfectly well ``open("config/.env")`` for its own reasons. A
    revise about pagination silently deleting that file, and then VALIDATING the
    mutilated package as fine, is a worse failure than anything the rule bought.
    The consequence is stated rather than hidden: if a nested file embeds a
    REGISTERED secret value, the copy trips ``validate_package``'s embedded-secret
    gate and the promote is refused with that gate's existing message naming the
    file. That is the gate working -- an operator who put a live credential in a
    second file learns about it -- and it is strictly better than shipping a
    package with a file quietly removed.

    ``source_dir`` is the directory being visited; ``shutil`` passes it through
    ``os.fspath``, so it arrives as a str and is compared as a ``Path`` (children
    are built by joining onto ``root``, so only the top-level call can equal it).

    ``names`` is not just the thing being filtered, it is EVIDENCE (R6-2): whether
    the exact ``.env`` appears in this listing is what tells a case-INSENSITIVE
    ``.ENV`` (one entry, ours, withhold it) from a case-SENSITIVE one (two
    entries, the package's, copy it). It is computed ONCE per directory rather
    than per name, because ``names`` is a list and a package root may hold many
    files.
    """
    at_root = Path(source_dir) == root
    exact_env_present = at_root and ".env" in names
    return {
        name
        for name in names
        if (at_root and _is_preserved_env_name(root, name, exact_present=exact_env_present))
        or _is_reserved_sidecar_name(name)
    }


def _copy_package_into_staging(source: Path, staging: Path) -> str | None:
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
    revise loudly here (裁決紀錄 #4 noted this path would surface exactly that,
    where the install-side ``os.walk`` gate stays as adjudicated).
    """
    try:
        shutil.copytree(source, staging, symlinks=True, ignore=partial(_revise_copy_ignore, source))
    except OSError as exc:
        return f"無法複製既有工具包到暫存工作區（{type(exc).__name__}）。"  # noqa: RUF001
    return None


def _read_env_for_values(directory: Path) -> tuple[bool, dict[str, str], str, str | None]:
    """``(existed, values, text, error)``: the package's ``.env`` VALUES (D40).

    Blocking, and the PARSE happens here rather than in the caller (R4-3). The
    read was moved onto a threadpool worker long ago, but ``python-dotenv`` was
    still handed the text back on the EVENT LOOP -- and a near-64 KiB ``.env``
    full of quoting is a real parse, not a formality, while this runs from a
    background job that shares the loop with every HTTP request in the process.
    Read and parse are one worker hop now, so no dotenv work is left on the loop;
    the caller still gets exactly what it needs, an existence bit and a values
    dict -- plus the RAW TEXT those values came out of.

    The text is returned so ``_unmaskable_env_error`` can compare each parsed value
    against the spelling the FILE actually holds -- specifically the raw right-hand
    side of the line that DETERMINED it (R5-1, narrowed by R6-1), which is why the
    whole text rather than a per-value verdict crosses back: the guard has to be
    able to find that line. It has to come from THIS
    read rather than a second one for the same reason the values do: a ``.env`` an
    unjailed ``run_shell`` can rewrite mid-session could read back differently, and
    a guard that vetted a different revision of the file than the one whose values
    were registered would be vetting nothing. It is ``""`` on every error path and
    whenever there is no ``.env`` -- with no values there is no spelling to check.

    ``existed`` is False with no error only when the package simply has no
    ``.env`` -- the common case, and the one where there are no values to register
    or export. It is the OBSERVATION "this package had no ``.env`` when the session
    started", and the caller carries exactly that bit down to
    ``_preserve_env_file`` (R3-1). It is taken from the READ, not from the parsed
    values: an EMPTY ``.env`` (or one that is all comments) parses to ``{}`` and
    must still count as EXISTING, because what it decides is whether a
    builder-written ``.env`` may take a deleted one's place. On any error the
    values are empty and the caller refuses the whole revise, so the bit is
    meaningless there and is reported False.

    This read is about VALUES ONLY, and that separation is deliberate (R2-1): the
    FILE is never round-tripped through this text -- ``_preserve_env_file`` copies
    it byte-for-byte at promote time -- so the lossy decode the shared bounded
    reader performs (utf-8 with ``errors="replace"``, universal newlines) touches
    nothing that lands on disk. The parse is ``tools._parse_dotenv_text``, the one
    parser ``tools._load_tool_dotenv`` itself delegates to, so the values
    registered as in-flight secrets and exported into ``run_shell`` are exactly the
    values the RUNTIME will hand the revised tool. Values are parsed from this ONE
    read rather than from a second one for the ordinary reason: two reads could
    disagree.

    Deliberately stricter than the runtime's own loader, which degrades an
    unreadable or oversized ``.env`` to "no extra env" and keeps the tool running.
    Here the same degrade would run a whole builder session -- ``run_shell``
    included -- with the tool's real credentials UNKNOWN to the redactor, which is
    the leak R1-1's floor check refuses to take by the other route. So a ``.env``
    that exists but that the bounded, O_NOFOLLOW'd reader declines (a symlink, a
    FIFO, an unreadable mode), or that comes back over the cap with its tail of
    values cut off, stops the revise before the session starts.

    The ``lstat`` answers TWO questions now -- is there a ``.env`` here, and is it
    within the ceiling -- and only ``FileNotFoundError`` answers "no" to the first
    (R2-3). Every other ``OSError`` (EIO on a failing disk, ESTALE on an NFS mount,
    EACCES on a directory an operator just chmod'ed) is a failure to LOOK, and
    reading it as absence would start a session whose ``.env`` values are
    unregistered while ``run_shell`` still receives nothing at all -- the degrade
    this function exists to refuse.

    The ceiling is measured in BYTES, off that same ``lstat``'s ``st_size`` (R5-3).
    It used to be measured in decoded CHARS, and the two are not the same ceiling:
    a CJK-heavy ``.env`` of ~30k characters is ~90 KB, so it sailed through this
    gate, spent a full multi-round builder session, and was then refused by
    ``_preserve_env_file``'s BYTE check at promote -- guaranteed-to-fail work,
    charged in full, on every retry. Entry and promote now apply the SAME ceiling
    to the SAME file in the SAME unit, which is also the unit
    ``tools.validate_package`` gates a staged ``.env`` in, so the answer no longer
    depends on which of the three asks. The promote-side check STAYS: the file can
    be swapped for a bigger one mid-session, which is exactly the R4-1 hazard, and
    an entry gate cannot see that. The CHAR check below stays too, for the same
    reason in miniature -- a file that GREW between this ``lstat`` and the read
    comes back at cap+1 chars and is refused there. What is deliberately NOT
    touched is ``tools._load_tool_dotenv``'s own char-based cap: that is a
    SEPARATE, pre-existing bound on what the RUNTIME will load (and it degrades to
    no-env rather than refusing), and nothing here changes it.
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
    verify what we can READ and refuse what we cannot, which is the direction
    ``_read_env_for_values`` (an unreadable ``.env`` stops the revise) and the
    pre-swap 定版 gate (an untrustworthy sidecar stops the swap) already take. It
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


def _existing_origin(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """The install ORIGIN inside an ALREADY-READ sidecar meta, or None.

    Pure. It takes the meta rather than a directory (R4-2) because the origin must
    come from the SAME read that decided the package was not 已定版, not from a
    second one of its own. That sidecar is the ONLY copy of the OpenAPI url and
    the operator's original instructions (nothing else persists them), the swap
    deletes it along with the old package, and the post-revise summary inherits it
    so a revised tool keeps knowing what it was built from -- so a read that
    answers "no origin" when it means "I could not look" loses that context
    IRREVERSIBLY. The old shape did exactly that: ``tools.read_tool_meta`` folds
    every failure into None, so one transient EIO was indistinguishable from a
    package that never had an origin, while the strict gate's own read then
    succeeded and let the swap proceed.

    There is no unreadable-vs-absent branch HERE, and that is not an omission: the
    caller's 定版 gate refuses on every unreadable shape before this is reached, so
    a None meta arriving here can only mean the sidecar was definitively ABSENT --
    a package with genuinely nothing to inherit, which proceeds with no origin.
    Narrowed and re-sanitized by ``tool_meta._stored_origin``, the same reader the
    synchronous regenerate uses.
    """
    return tool_meta._stored_origin(meta)


async def run_revise(name: str, feedback: str) -> InstallOutcome:
    """Run one whole revise: copy, build in staging, validate, REPLACE (D40).

    Same contract as ``run_install`` in every externally visible way -- it runs
    inside a fire-and-forget background job, so every failure is a FRIENDLY
    OUTCOME (zh-TW ``error``) and never an exception, and ``llm_log_id`` is
    captured right after the builder call on success and failure alike.

    The two gates before any work: the package must resolve through
    ``tools._resolve_package_dir_no_alias`` (the shared by-name resolver, so an
    internal alias is refused here exactly as it is by every summary route), and
    its sidecar must yield a TRUSTWORTHY status that is not 已定版 -- the STRICT
    ``summary_status_or_unknown``, refusing an unreadable/corrupt sidecar exactly
    as it refuses a finalized one (R6-3). The router checks the finalized case too;
    re-checking is defence in depth against a 定版 that landed between the two, and
    it costs one sidecar read. A 定版 landing LATER -- mid-session, after this check
    -- IS caught, by ``_promote_staging_replace``'s own re-check at the last moment
    before the swap. (This paragraph used to claim the opposite -- that a
    mid-session 定版 was an accepted residual -- which stopped being true when the
    P3b self-review added that re-check, and the stale text survived the fix.
    Corrected alongside R3-2, which makes the same re-check fail CLOSED.)

    Strict HERE and total at the ROUTE is the whole point, not an inconsistency
    (R6-3): a route answers about a resource's KNOWN state and may cheaply guess,
    but this job is what ACTS -- it spends a multi-round builder session and holds
    the single-flight while it does -- and an already-corrupt sidecar was certain to
    be refused by the pre-swap gate at the END of that session anyway. Paying in
    full for a foregone refusal, on every retry, is what the strict entry gate
    removes; the refusal itself was never in doubt.

    The ``.env`` is read for its VALUES (and refused if unreadable, or over the
    BYTE ceiling promote itself applies -- R5-3, so a ``.env`` promote would refuse
    never buys a whole session first) BEFORE anything else, and every value in it
    is registered as an in-flight secret for the WHOLE window, discarded value by
    value in the ``finally``. That
    read is NOT how the file is preserved -- the FILE is copied byte-for-byte at
    promote time (``_preserve_env_file``, R2-1), and the two concerns stay apart:
    values are parsed, the file is copied. D40 requires the registration
    for a window that install does not have: the swap parks the old package in a
    DOT-prefixed backup, and ``known_secret_values`` skips dot-directories, so
    for the length of that swap the tool's own credentials would otherwise be
    unknown to the redactor -- while the builder conversation is still being
    recorded. Registering them up front also makes them redactable in every
    prompt/response of the session, and makes the embedded-secret gate refuse a
    revision that copied them into a file.

    That same policy is re-applied at PROMOTE, to the bytes actually being
    published (R7-2): this entry gate answers for the file it read, and an
    operator can put a different one there while the session runs -- including on
    a package that had no ``.env`` at all when it started. See
    ``_preserve_env_file`` for the half of that hazard which IS closable, and
    裁決紀錄 #6 for the half which is not.

    A value the redactors could not mask refuses the whole session, whether it is
    too SHORT for them to look at (R1-1) or SPELLED by its own assignment line in a
    way this system would not write (R5-1, narrowed to that LINE by R6-1 -- a
    comment elsewhere in the file repeating the value vouched for nothing -- and to
    EXACT equality with a spelling we can generate ourselves by R7-1, after a
    comment on the SAME line vouched for one too) --
    one policy, one gate,
    ``_unmaskable_env_error``. Registration is not protection on its own, and this
    path hands every one of those values to ``run_shell``, whose output is wrapped
    verbatim into the next round's prompt, recorded into the AI 日誌 attempt
    bodies, and can reach ``InstallResult.summary`` -> the job poll -> the sidecar.
    So the only two honest options are "do not run" and "leak". Both halves are the
    SAME rule the INSTALL path already applies at its own entry -- the form's
    ``schemas._SECRET_VALUE_MIN_LEN`` floor, and ``_dotenv_serialize_value``'s
    refusal to write a spelling whose escapes fire -- with the difference that
    install WRITES the file and a revise INHERITS whatever a hand-edit left in it.
    INSTALL is structurally clear of both: its staging is built EMPTY
    (``staging.mkdir``) and ``_promote_staging`` refuses a name that already
    exists, so it never inherits a pre-existing package's ``.env``, and the only
    value it puts there is the form secret the schema floored and the serializer
    round-trip-checked.
    """
    base = tools.tools_dir()
    if base is None:
        return InstallOutcome(ok=False, error=_ERROR_TOOLS_DISABLED)

    directory = await run_in_threadpool(tools._resolve_package_dir_no_alias, name)
    if directory is None:
        return InstallOutcome(ok=False, error=_ERROR_REVISE_NOT_FOUND)
    # Through the STRICT reader, and refusing on UNKNOWN as well as on final
    # (R6-3): a sidecar that is ALREADY corrupt when the request arrives used
    # to pass this gate on ``summary_status``'s total None, burn a whole multi-round
    # builder session, and then be refused by ``_promote_staging_replace``'s strict
    # read at the end -- guaranteed-to-fail work charged in full, on every retry,
    # while the global single-flight was held. This job is the thing that ACTS, so
    # it is the thing that must fail closed; the two error strings are the SAME two
    # promote uses, so the operator sees one refusal per remedy (repair/remove a
    # damaged sidecar vs. 解除定版) wherever it was decided.
    #
    # The ROUTER's own admission gate deliberately STAYS on the total
    # ``summary_status``: a route answers about the resource's KNOWN state, and
    # guessing "not finalized" there costs at most this refusal, arriving as a job
    # outcome instead of a 409. The authoritative, fail-closed check belongs to the
    # job that is about to act -- here, and again at the last moment before the swap.
    #
    # The meta this read parsed is dropped on purpose: the origin must come from the
    # read the PRE-SWAP gate makes, minutes later, because that is the one whose
    # answer the swap acts on (R4-2). Carrying this one down would reintroduce the
    # two-reads-can-disagree bug at a much longer timescale.
    status, _ = await run_in_threadpool(tools.summary_status_or_unknown, directory)
    if status == "final":
        return InstallOutcome(ok=False, error=_ERROR_REVISE_FINALIZED)
    if status == tools._SUMMARY_STATUS_UNKNOWN:
        return InstallOutcome(ok=False, error=_ERROR_REVISE_SUMMARY_UNREADABLE)

    # ONE worker hop does the read AND the dotenv parse (R4-3): the parse is real
    # work on a 64 KiB file and this job shares the loop with every request.
    # ``env_existed_at_start`` is the READ's answer, not the parse's -- an empty or
    # comment-only ``.env`` parses to {} and still EXISTED -- and it is carried down
    # to the promote because "there is no ``.env`` now" is ambiguous by then: a
    # package that never had one may ship the builder's, but one whose ``.env`` the
    # operator DELETED mid-session must not have a placeholder put in its place
    # (R3-1). VALUES only: the FILE is copied at promote time. Empty values
    # contribute nothing to redaction and are not registered, matching
    # tools._cached_env_values' own "a KEY= line contributes nothing" rule.
    # R10-1: the identity of the package we are about to revise, taken BEFORE the
    # session and re-checked before the swap. An operator can delete and reinstall
    # the tool during the minutes a build runs, and a name is not an identity.
    package_identity = await run_in_threadpool(_package_identity, directory)
    env_existed_at_start, env_values, env_text, env_error = await run_in_threadpool(
        _read_env_for_values, directory
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

    staging = base / _STAGING_DIRNAME / uuid4().hex
    try:
        copy_error = await run_in_threadpool(_copy_package_into_staging, directory, staging)
        if copy_error is not None:
            return InstallOutcome(ok=False, error=copy_error)

        settings = get_settings()
        manifest_text = await run_in_threadpool(_current_manifest_text, directory)
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
                tools=_build_meta_tools(staging, secret_env=env_values or None),
                max_tool_rounds=settings.tool_install_max_rounds,
                timeout_seconds=settings.tool_install_timeout_seconds,
            )
        except LLMNotConfiguredError:
            llm_error = "LLM 尚未設定，無法執行修訂。"  # noqa: RUF001
        except LLMUpstreamError as exc:
            # str(exc) is category + fixed reason by construction (see llm.py).
            llm_error = f"AI 修訂工具失敗（{exc}）。"  # noqa: RUF001

        llm_log_id = llm_log.last_record_id_for_workflow(_WORKFLOW)
        if llm_error is not None:
            return InstallOutcome(ok=False, error=llm_error, llm_log_id=llm_log_id)
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
            )
        if result.tool_name != name:
            # D40: ``InstallResult._ready_requires_valid_name`` only checks the
            # SHAPE of the name, never its identity -- so a model that decided to
            # "fix" the name would otherwise have its work promoted over whatever
            # package that other name addresses. Refuse, and say which name was
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
            )

        # The origin comes back FROM the promote, not from a read of our own
        # (R4-2): it has to be the sidecar the 定版 gate just vetted, because a
        # separate total read turns a transient failure into "no origin" and the
        # swap then destroys the only copy. See ``_existing_origin``.
        origin, promote_error = await run_in_threadpool(
            _promote_staging_replace,
            staging,
            name,
            base,
            env_existed_at_start=env_existed_at_start,
            package_identity=package_identity,
            # The promote re-vets the ``.env`` it is about to ship and registers
            # whatever it finds there (R7-2). It appends to THIS list, so the
            # ``finally`` below discards those values too -- the alternative,
            # handing them back through the return, would leak a registration on
            # any path that did not reach the return.
            registered=registered,
        )
        if promote_error is not None:
            return InstallOutcome(
                ok=False,
                tool_name=name,
                summary=summary,
                error=promote_error,
                llm_log_id=llm_log_id,
            )
        # The revision is LIVE as of the line above; everything after it is
        # decoration and cannot fail the outcome (``generate_and_store_summary``
        # never raises -- see tool_meta). The sidecar was excluded from staging, so
        # the package has NO summary until this regenerates one: the same
        # briefly-absent-sidecar window D40 already accepts right after an install,
        # and the reason the origin above is carried across rather than re-derived.
        await tool_meta.generate_and_store_summary(name, origin=origin, builder_summary=summary)
        return InstallOutcome(ok=True, tool_name=name, summary=summary, llm_log_id=llm_log_id)
    finally:
        # Mirror run_install's order: drop the in-flight secrets first (the
        # preserved .env carries them again, so known_secret_values covers them
        # through its own scan), then clean staging. A successful swap consumed
        # the staging dir; every other exit removes the build. The list may have
        # GROWN since it was built -- the promote appends whatever the shipped
        # ``.env`` turned out to hold (R7-2) -- which is the point of passing it
        # down rather than returning those values.
        for value in registered:
            tools.discard_inflight_secret(value)
        await run_in_threadpool(_cleanup_staging, staging, base)


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
# during which it reads a package and then writes that package's sidecar, so it
# occupies the admission domain for exactly the same reason a job does. A token
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


def _admit_job() -> InstallJob | None:
    """Register one queued job, or None when another is already active (M7/D40).

    The single-flight admission, written ONCE for both kinds: the active-check
    and the insert happen under the SAME lock acquisition, so there is no
    check-then-start race, and a caller maps None onto its route's 409. ANY
    queued/running job blocks ANY new one -- an install and a revise both end in
    a package directory being moved into place, so letting them overlap would
    race a directory being replaced (and would make the two sessions'
    same-workflow log records ambiguous to ``last_record_id_for_workflow``). A
    held SYNCHRONOUS reservation blocks one too (R7-3): a regenerate in flight is
    about to write the sidecar of a package this job may be replacing.

    Eviction keeps the newest ``_MAX_JOBS`` by creation time (job_id as a
    deterministic tiebreak for identical timestamps); a TERMINAL (succeeded/
    failed) job stays pollable until evicted and never blocks a new submit.
    """
    job = InstallJob(job_id=uuid4().hex, state="queued", created_at=_now_iso())
    with _JOBS_LOCK:
        if _single_flight_held():
            return None
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


def start_revise_job(name: str, feedback: str) -> str | None:
    """Create a revise job and launch it; returns the job id, or None when a job
    is ALREADY active (queued|running) -- which the router maps to a 409 (D40).

    Same table, same single-flight admission and same task machinery as an
    install: from here down the two kinds are indistinguishable, which is what
    lets one poll endpoint serve both. ``name`` has already been validated
    (path-layer regex) and resolved (the route's existence gate); ``run_revise``
    re-resolves it anyway, because the route's check and this task are seconds
    apart.
    """
    job = _admit_job()
    if job is None:
        return None
    return _launch_job(job, lambda: run_revise(name, feedback), _ACTION_REVISE)


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
    "a package directory or its sidecar is being written by something else".
    """
    with _JOBS_LOCK:
        return _single_flight_held()


def reserve_sync_operation() -> str | None:
    """Take the single flight for one SYNCHRONOUS operation; None = refused (R7-3).

    The counterpart of ``_admit_job`` for work that is not a job: the summary
    regenerate runs inside a request, but it spends a full LLM round trip between
    reading a package and writing that package's sidecar. Its old gate was a bare
    ``any_job_active()`` read, which is a check-then-act across an await that
    lasts as long as an LLM call -- long enough for a revise to be admitted,
    replace the whole package and write a fresh sidecar, which the older
    regenerate then OVERWROTE with a summary describing the package that no
    longer exists. Taking a reservation puts it in the same admission domain as
    the jobs: while one is held ``_admit_job`` refuses, so the revise never
    starts.

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
