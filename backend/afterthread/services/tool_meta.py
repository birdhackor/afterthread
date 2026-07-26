"""The AI summary of an INSTALLED tool package (see D40 in docs/web-v4-decisions.md).

The installer (``tool_builder``) answers "did it build?"; this module answers
"what did it build, and how does it work?" -- one short LLM session that READS
the promoted package (manifest + implementation files) and writes a user-facing
explanation into the package's own ``.ai_meta.json`` sidecar
(``tools.read_tool_meta`` / ``tools.write_tool_meta``).

Three properties are deliberate, and each has a failure mode behind it:

* **A separate workflow name** (``tool_summary``, never ``tool_install``). Both
  sessions run inside the SAME install job, one after the other, and both link
  their record via ``llm_log.last_record_id_for_workflow`` -- which resolves by
  NAME. Sharing a name would make the install outcome's ``llm_log_id`` point at
  the summary session instead of the build it is supposed to explain.
* **Best-effort, never fatal** (``generate_and_store_summary`` cannot raise).
  It runs AFTER the package has already been promoted, so a summary failure
  must never flip a genuinely successful install to failed -- the same
  no-observer-failure stance ``llm_log``'s recorder takes. A failed generation
  still leaves a sidecar (empty summary + origin) so the operator can hit
  regenerate; the ONE thing it must never do is overwrite a GOOD summary with
  an empty one.
* **Fed from the files, not from memory.** The prompt carries the actual
  package contents, so the summary describes what is genuinely installed --
  including a package the operator later hand-edited (README documents editing
  a tool in place). ``.env`` VALUES never enter the prompt (only the key
  NAMES), the sidecar itself is skipped so a summary can never feed itself back
  in, and every piece -- files, paths, the operator's own install context --
  is redacted BEFORE it is stripped or cut, behind one final pass over the
  whole assembled prompt: the redact-then-cap order the codebase uses, closed
  as a property of the PROMPT rather than of each field in it.

``regenerate_summary`` is the same generation behind the synchronous
``POST /api/tools/{name}/summary/regenerate`` route, and is the ONE entry point
that lets the LLM failures propagate -- a user who pressed 重新產生 is waiting
for an answer, so "the LLM is not configured" must reach them as a 503/502
rather than being swallowed into a silent no-op.
"""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from afterthread.config import get_settings
from afterthread.services import llm_log, token_budget, tools
from afterthread.services.llm import generate_structured
from afterthread.services.memory_ai import _coerce_str, _truncate_to

# The llm_log workflow name for every summary session. DISTINCT from
# tool_builder's ``tool_install`` on purpose -- see the module docstring's
# last_record_id_for_workflow note; that separation is the whole reason this is
# a named constant rather than an inline string.
_SUMMARY_WORKFLOW = "tool_summary"

# Cap on the stored summary. Generous next to InstallResult's 2000-char progress
# note (_SUMMARY_CAP) because this text is the tool's user-facing DOCUMENTATION
# -- what it does, how it runs, its inputs/outputs and limits -- and it is
# rendered on demand in one panel, not carried in any prompt or tool spec.
_TOOL_SUMMARY_CAP = 8000

# Per-FILE slice of the package that may ride in the prompt. A fixed cap (rather
# than a proportional split) keeps the shape predictable and stops ONE huge
# generated file from crowding every other file out of the inventory: each file
# contributes at most this much, and the whole prompt is then bounded again by
# the operator's own prompt budget below. 20k chars is far more than any sane
# generated run.py, so the cap is invisible on a normal package.
_FILE_CONTENT_CAP = 20_000

# How many implementation files are listed at all. A pathological package (a
# builder whose run_shell exploded a node_modules into staging) must not turn
# one prompt into thousands of file sections; mirrors _LIST_DIR_MAX_ENTRIES's
# reasoning in tool_builder, at the smaller size a real tool package needs.
_MAX_FILES = 50

# Cap on the two free-text CONTEXT fields (the install-time instructions and the
# builder's own report). They are context, not the subject: the package files
# are what the summary must describe, so neither may dominate the prompt.
_CONTEXT_CAP = 4_000

# The summary session's system prompt. English, like every prompt in this
# codebase; only the OUTPUT language follows the source content, via the same
# rule memory_ai's workflows carry (_RULE_LANGUAGE) -- an operator who installed
# a tool with zh-TW instructions must not get an English explanation back.
#
# The honesty clause is the important half: this model is handed real source
# files and asked to explain them, which is exactly the setting where a
# plausible-sounding invention (an endpoint the tool does not call, a parameter
# it does not accept) is indistinguishable from a fact for the reader. The
# summary is documentation the operator will trust when deciding whether to keep
# or revise the tool, so "describe only what the files show" is a hard rule.
_SUMMARY_SYSTEM_PROMPT = """\
You are a tool explainer. You are given every file of ONE installed tool \
package (a small program an AI assistant can call). Write a clear explanation \
of this tool for the person who installed it.

Cover, in this order:
1. What the tool does -- the capability it gives the AI assistant, in one short \
paragraph.
2. How it works -- how the entry command runs, what it calls out to (which API, \
which endpoints), and how it authenticates.
3. Inputs and outputs -- the arguments it accepts (from the tool.json parameter \
schema) and what its result looks like to the AI assistant.
4. Limits and caveats -- what it does NOT handle, error behaviour, rate/size \
limits, required environment variables, and anything the reader should watch \
out for.

Describe ONLY what the files actually show. If something is not visible in the \
files (an API's real behaviour, a value you were not given, a limit that is not \
written down), say so plainly instead of guessing -- an invented detail is worse \
than an acknowledged gap. Never print secret values, even if one appears in the \
files; refer to a secret by its variable name only.

Write your output in the same language as the source content and instructions \
(for example, Traditional Chinese instructions yield a Traditional Chinese \
explanation). Keep the JSON keys in English. Plain prose with short headings; \
no code fences around the whole answer."""


class ToolSummaryResult(BaseModel):
    """The summary session's structured close: the explanation text, sanitized.

    Sanitized exactly like every other LLM-facing model (untrusted output):
    ``redact_known_secrets`` FIRST, then strip, then the cap -- the
    redact-then-cap order ``InstallResult._sanitize`` documents at length, and
    for the same reason: a secret straddling the slice edge must be masked while
    the text is still WHOLE, or the cut leaves an interior fragment no later
    pass can match.

    The STRIP belongs after the redaction for the same "match the value while
    the text is untouched" reason, one step earlier: a registered secret whose
    value carries edge whitespace (a hand-edited ``.env`` with a quoted
    ``" secret-token"``) stops matching the moment ``strip`` eats that edge, so
    stripping first would hand the redactor a body it no longer recognizes and
    leak the rest of the value. Untouched text in, redaction, THEN the cosmetic
    trims and cuts.

    An EMPTY summary is rejected rather than stored. The model returning nothing
    usable is exactly what ``generate_structured``'s one corrective retry exists
    for, and an empty string is not a summary -- it is indistinguishable from the
    placeholder a FAILED generation writes, so accepting it would make the
    sidecar lie about whether a summary was ever produced.
    """

    model_config = ConfigDict(extra="ignore")

    summary: str = ""

    @model_validator(mode="before")
    @classmethod
    def _sanitize(cls, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return {
            "summary": tools.redact_known_secrets(_coerce_str(data.get("summary"))).strip()[
                :_TOOL_SUMMARY_CAP
            ]
        }

    @model_validator(mode="after")
    def _summary_required(self) -> ToolSummaryResult:
        if not self.summary:
            raise ValueError("summary must be a non-empty explanation of the tool package")
        return self


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _package_files(directory: Path) -> list[tuple[str, str]]:
    """Every readable implementation file as ``(relative path, content)``.

    Skips, in this order of importance:

    * every DOT-prefixed name, file or directory -- which is what excludes both
      the tool's ``.env`` (its values are live secrets; only the key NAMES ride
      in the prompt, see ``_env_key_names``) and this feature's own
      ``.ai_meta.json`` sidecar (a summary must never be fed its own previous
      output as if it were source);
    * ``tool.json``, rendered separately as the manifest section;
    * anything the bounded reader refuses (a FIFO, a symlinked leaf, an
      unreadable or oversized file) -- it simply does not appear.

    Each file is read through the ONE bounded, FIFO/symlink-hardened helper, then
    redacted and cut per file. ``os.walk`` does not follow directory symlinks, so
    the inventory can never be walked out of the package. Sorted for a
    deterministic prompt, and bounded by ``_MAX_FILES``.
    """
    collected: list[tuple[str, str]] = []
    base = directory.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        here = Path(dirpath)
        for filename in sorted(filenames):
            if filename.startswith(".") or (here == base and filename == "tool.json"):
                continue
            if len(collected) >= _MAX_FILES:
                return collected
            text = tools._read_regular_file_capped(here / filename, _FILE_CONTENT_CAP)
            if text is None:
                continue
            # Redact BEFORE the cut (redact-then-cap, as everywhere else): a value
            # straddling the slice edge must be masked while the text is whole.
            collected.append(
                (
                    str((here / filename).relative_to(base)),
                    _truncate_to(tools.redact_known_secrets(text), _FILE_CONTENT_CAP),
                )
            )
    return collected


def _env_key_names(directory: Path) -> list[str]:
    """The package ``.env``'s KEY names -- never its values.

    The names are what makes the explanation useful ("it reads KB_API_KEY from
    its environment"); the values are exactly what must never enter a prompt, so
    the file is parsed through the shared bounded loader and only ``.keys()`` is
    taken. A missing or malformed ``.env`` degrades to [] via that loader's own
    contract.
    """
    return sorted(tools._load_tool_dotenv(directory))


def _summary_user_prompt(
    name: str,
    directory: Path,
    *,
    origin: dict[str, Any] | None,
    builder_summary: str | None,
) -> str:
    """The session's user turn: the package itself, plus its install context.

    Bounded the same way every other prompt in this codebase is: the operator's
    ONE ``llm_prompt_budget_tokens`` knob, converted to a CHAR allowance through
    the live chars<->tokens ratio, applied to the assembled text behind the
    shared truncation marker. The per-file cap above is the inner bound (no
    single file may crowd out the inventory); this is the outer one (the whole
    prompt stays inside the operator's budget however many files there are).

    EVERY piece is redacted on its UNTOUCHED text, before any strip or cut.
    ``origin`` is what the install captured (the OpenAPI URL and the user's
    instructions -- neither is persisted anywhere else), and ``builder_summary``
    is the builder's own report of what it did; both are CONTEXT for reading the
    files, capped tightly so they can never displace the files themselves.

    The origin fields are OPERATOR-supplied and were the hole here: an install
    URL is routinely ``https://api.example/openapi.json?token=<the form
    secret>``, and that value is registered as an in-flight secret at install
    time -- so the redactor KNOWS it and simply was not asked. Same for a
    relative path (a builder can create a file whose NAME embeds an expanded
    ``$SECRET``, the vector ``tool_builder``'s list_dir already masks) and the
    ``.env`` key-name line. And strip-before-redact is its own leak: a
    registered value carrying edge whitespace stops matching once ``strip`` eats
    that edge, so the strips run AFTER the mask, never before it.
    """
    budget = token_budget.char_allowance(get_settings().llm_prompt_budget_tokens)
    origin_data = origin or {}
    parts = [
        f"Explain the installed tool package `{name}`.",
    ]
    openapi_url = origin_data.get("openapi_url")
    if isinstance(openapi_url, str) and openapi_url.strip():
        parts.append(
            "It was built from this OpenAPI document: "
            + tools.redact_known_secrets(openapi_url).strip()
        )
    instructions = origin_data.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        parts.append(
            "The user's original install instructions:\n"
            + _truncate_to(tools.redact_known_secrets(instructions).strip(), _CONTEXT_CAP)
        )
    if builder_summary and builder_summary.strip():
        parts.append(
            "What the builder reported after building it:\n"
            + _truncate_to(tools.redact_known_secrets(builder_summary).strip(), _CONTEXT_CAP)
        )
    manifest = tools._read_regular_file_capped(directory / "tool.json", _FILE_CONTENT_CAP)
    if manifest is not None:
        parts.append(
            "tool.json:\n" + _truncate_to(tools.redact_known_secrets(manifest), _FILE_CONTENT_CAP)
        )
    for relative_path, content in _package_files(directory):
        # The CONTENT is already masked by _package_files; the header carrying
        # the path is not, and a filename can embed a value just as a file body can.
        parts.append(f"{tools.redact_known_secrets(relative_path)}:\n{content}")
    keys = _env_key_names(directory)
    if keys:
        # NAMES only -- see _env_key_names. Stated as environment variables
        # because that is how the runtime hands them to the tool.
        parts.append(
            "The package has a .env providing these environment variables "
            "(values withheld): " + tools.redact_known_secrets(", ".join(keys))
        )
    # One FINAL pass over the fully assembled text, and it is the class-closer:
    # every field above is masked individually, but the next field somebody adds
    # to this prompt will be masked whether or not its author remembers to. It
    # runs BEFORE the budget cut for the usual redact-then-cap reason (a value
    # straddling the cut must be masked while the text is whole), and is a no-op
    # over everything already masked -- the marker carries no secret to match.
    return _truncate_to(tools.redact_known_secrets("\n\n".join(parts)), budget)


def _stored_origin(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """The sidecar's ``origin``, narrowed to the two fields we understand.

    Only the INSTALL ever captures the OpenAPI URL and the operator's
    instructions, so the sidecar is the only copy -- and it is exactly the
    first-hand context a regeneration wants back in its prompt (why the tool was
    built, from which document), which is why this is read rather than
    regenerating from the files alone.

    Defensive because the sidecar is a plain JSON file the operator is allowed
    to hand-edit (the README says so): a non-dict ``origin``, a non-string
    ``openapi_url``, an extra key -- none of them may reach the prompt as if the
    backend had written it. What survives is the two known string fields.

    Returning None when NOTHING survives is load-bearing, not tidiness: None is
    ``_store_meta``'s "inherit whatever is on disk" signal, so a sidecar whose
    origin we cannot make sense of keeps its only copy instead of having it
    overwritten by an empty dict.
    """
    if meta is None:
        return None
    origin = meta.get("origin")
    if not isinstance(origin, dict):
        return None
    kept = {
        key: value
        for key, value in origin.items()
        if key in ("openapi_url", "instructions") and isinstance(value, str)
    }
    return kept or None


def _store_meta(
    directory: Path,
    *,
    summary: str,
    origin: dict[str, Any] | None,
    llm_log_id: int | None,
) -> dict[str, Any] | None:
    """Merge the new summary into the package's sidecar and write it back.

    Two fields are PRESERVED from whatever is already on disk rather than reset:

    * ``status`` -- a regenerate on a 草稿 stays 草稿. (A finalized tool never
      reaches here: both callers of a regeneration refuse a ``final`` package
      up front, which is what 定版 MEANS. Preserving rather than forcing "draft"
      is the safe direction if that gate is ever bypassed by a race.)
    * ``origin`` -- only the install captures the OpenAPI URL and instructions,
      so a caller with nothing to pass (``origin=None``) must inherit them
      instead of erasing the only copy. A regeneration normally passes the
      narrowed origin it just read back in, which lands on the same value; the
      inheritance is what covers the sidecar that has none we can use.

    Returns the meta dict as composed WHEN THE WRITE LANDED, and None when
    ``write_tool_meta`` refused it (a fail-closed redaction, the ghost guard on
    a racing delete, a FIFO/symlink swapped in for the sidecar, an unwritable
    directory). The None matters because the synchronous route answers WITH this
    dict: returning the composed meta regardless would have it report a summary
    that is nowhere on disk and vanishes on the next GET -- a wrong answer, not
    a filesystem detail. The install hook ignores the distinction on purpose
    (see its own call sites); the route folds it into its did-not-happen 404.
    """
    existing = tools.read_tool_meta(directory) or {}
    status = existing.get("status")
    if not (isinstance(status, str) and status in tools._SUMMARY_STATUSES):
        status = "draft"
    meta: dict[str, Any] = {
        "summary": summary,
        "status": status,
        "updated_at": _now_iso(),
        "llm_log_id": llm_log_id,
        "origin": origin if origin is not None else existing.get("origin"),
    }
    return meta if tools.write_tool_meta(directory, meta) else None


async def _generate_summary(
    name: str,
    directory: Path,
    *,
    origin: dict[str, Any] | None,
    builder_summary: str | None,
) -> str:
    """Run ONE summary session and return its validated text.

    ``tools=None``: this session only reads the package we already handed it in
    the prompt -- it has no business calling the installed tools (or any other),
    and passing None keeps the request byte-identical to a plain structured call.
    The default timeout applies (unlike the builder's, this is one short
    single-turn generation, not a multi-round build).

    Raises ``LLMNotConfiguredError`` / ``LLMUpstreamError`` unchanged; each
    caller decides whether that is swallowed (the install hook) or surfaced (the
    synchronous regenerate route).
    """
    result = await generate_structured(
        _SUMMARY_SYSTEM_PROMPT,
        _summary_user_prompt(name, directory, origin=origin, builder_summary=builder_summary),
        ToolSummaryResult,
        workflow=_SUMMARY_WORKFLOW,
        tools=None,
    )
    return result.summary


async def generate_and_store_summary(
    name: str,
    *,
    origin: dict[str, Any] | None = None,
    builder_summary: str | None = None,
) -> None:
    """Summarize a just-installed package into its sidecar. NEVER raises.

    Called from inside the install job AFTER the package has been promoted, so
    the package is already installed and the job is already a success by the
    time this runs: every failure -- the LLM being unconfigured, an upstream
    error, a bug in here -- is swallowed, because none of them makes the
    installed tool any less installed. This is the same no-observer-failure
    stance ``llm_log``'s recorder takes, and the reason for the total
    ``except Exception`` backstop rather than a list of expected LLM errors.

    A failed generation still leaves a sidecar carrying the ``origin`` and the
    failed session's log id with an EMPTY summary, so the 工具 page can show
    "尚無總結" with a working 重新產生 button and the operator can read the
    trace. It writes that placeholder ONLY when no sidecar exists yet: on a
    later regeneration the previous, GOOD summary must survive a transient LLM
    failure rather than being blanked by it.
    """
    try:
        # The alias-refusing resolve, shared with every other by-name summary
        # path (see tools._resolve_package_dir_no_alias). The install hook
        # cannot actually reach an alias -- it was handed the name it just
        # promoted -- but going through the ONE helper costs nothing and keeps
        # "every summary path refuses an alias" true by construction rather
        # than by inspection.
        directory = tools._resolve_package_dir_no_alias(name)
        if directory is None:
            # The package vanished (a racing delete) between promote and here.
            # Nothing to summarize and nowhere to write; silence is correct.
            return
        try:
            summary = await _generate_summary(
                name, directory, origin=origin, builder_summary=builder_summary
            )
        except Exception:
            if tools.read_tool_meta(directory) is None:
                # Best-effort, so the write result is DELIBERATELY ignored here
                # and below: a refused sidecar must never fail an install that
                # already succeeded (see this function's contract).
                _store_meta(
                    directory,
                    summary="",
                    origin=origin,
                    llm_log_id=llm_log.last_record_id_for_workflow(_SUMMARY_WORKFLOW),
                )
            return
        _store_meta(
            directory,
            summary=summary,
            origin=origin,
            llm_log_id=llm_log.last_record_id_for_workflow(_SUMMARY_WORKFLOW),
        )
    except Exception:
        # Total backstop: a bug in prompt building, the sidecar read, or the
        # filesystem must not turn a SUCCESSFUL install into a failed job.
        return


async def regenerate_summary(name: str) -> dict[str, Any] | None:
    """Regenerate one package's summary SYNCHRONOUSLY; returns the fresh meta.

    The user-driven counterpart of the install hook, and the deliberate mirror
    image of its error handling: ``LLMNotConfiguredError`` / ``LLMUpstreamError``
    PROPAGATE so the route can answer 503/502: someone is waiting on this
    request, and silently returning the old summary would be a lie about what
    just happened.

    None is the SAME kind of honesty for the non-LLM failures: the package
    vanished under us, or the sidecar write was refused. Both mean the
    regeneration did not happen, and the route folds them into its
    did-not-happen 404 exactly as ``set_summary_status`` folds its own failed
    rewrite into ``not_found``. Answering 200 with a summary that is nowhere on
    disk would leave the user reading text that disappears on their next visit.

    What it does NOT change is the no-clobber rule -- the sidecar is only
    rewritten after a successful generation, so a failed regenerate leaves the
    previous summary exactly as it was.

    The STORED origin is read first and fed back into the prompt. The install's
    OpenAPI URL and instructions are the first-hand account of what this package
    was supposed to be, they exist nowhere but this sidecar, and a regeneration
    that dropped them would explain the files with strictly less context than
    the install did -- while ``_store_meta`` would separately have to inherit
    them anyway. Passing the same narrowed origin back through the store keeps
    the returned meta equal to what is on disk; a sidecar with no usable origin
    yields None and the store's inheritance still covers it.

    The caller (the route) has already checked that the tool exists, is not
    finalized, and that no job is mid-promote; the resolve here is a race
    backstop -- and, via the shared alias-refusing helper, the same hard-block
    every other by-name summary path runs.
    """
    directory = tools._resolve_package_dir_no_alias(name)
    if directory is None:
        return None
    origin = _stored_origin(tools.read_tool_meta(directory))
    summary = await _generate_summary(name, directory, origin=origin, builder_summary=None)
    return _store_meta(
        directory,
        summary=summary,
        origin=origin,
        llm_log_id=llm_log.last_record_id_for_workflow(_SUMMARY_WORKFLOW),
    )
