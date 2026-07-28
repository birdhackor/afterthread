"""Generate and publish summaries for installed versioned tools.

Prompt content comes from one resolved VersionRoot, while package-layer ``.env``
contributes key names only. Immutable provenance remains in ``origin.json``; this
module atomically replaces only that version's ``summary.json``. Install-time
generation is best-effort, while explicit regeneration surfaces LLM failures.
"""

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, model_validator
from starlette.concurrency import run_in_threadpool

from afterthread.config import get_settings
from afterthread.services import llm_log, token_budget, tools
from afterthread.services.llm import generate_structured
from afterthread.services.memory_ai import _coerce_str, _truncate_to

# The llm_log workflow name for every summary session. DISTINCT from
# tool_builder's ``tool_install`` on purpose -- see the module docstring's
# last_record_id_for_workflow note; that separation is the whole reason this is
# a named constant rather than an inline string.
_SUMMARY_WORKFLOW = "tool_summary"


class VersionMismatchError(Exception):
    """The validated current version changed before explicit regeneration began."""


# What a sanitized origin URL says INSTEAD of the parts it dropped (see
# ``_sanitized_origin_url``). Fixed and visible on purpose: the sidecar is the
# only record of where a package came from, so a silently shortened URL would
# read as the whole truth -- an operator comparing it against the address they
# typed has to be able to tell "this URL had nothing beyond its host" from
# "something was removed". One marker for every dropped part (userinfo, path,
# query, fragment) because the reader's question is whether anything was
# removed, not which -- path joined the list in round 5, after round 4 shipped
# with userinfo/query/fragment; the SAME marker is reused rather than a new
# one, since the reader's question has not changed.
# Worded like the module's other markers (…[…] , zh-TW). Idempotency no longer
# rides on the marker's own text alone (see the function): with no path left
# to glue it to, a plain textual re-application would land the marker
# directly on the bare host, so the function peels a trailing marker off
# before parsing rather than relying solely on the marker excluding
# "?", "#", "@" and "/".
_ORIGIN_URL_TRIMMED_MARKER = "…[查詢字串與認證資訊已移除]"

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
    """The summary session's structured close: the explanation text, SHAPE-checked.

    What this validator does NOT do is the load-bearing part. It runs inside
    ``generate_structured``'s ``model_validate``, which happens ON THE EVENT LOOP
    -- so it coerces the value to a string and decides whether there is a summary
    at all, and it touches NOTHING ELSE. It used to also call
    ``tools.redact_known_secrets``, whose provider walks the tools directory
    (``iterdir`` + a ``stat`` per package, and a ``.env`` read on every cache
    miss): blocking filesystem work on the loop, in a module that hops every
    other filesystem step onto a threadpool worker for exactly that reason.

    Redaction and the ``_TOOL_SUMMARY_CAP`` cut are STORAGE-boundary concerns and
    now live at the storage boundary -- ``tools.store_summary_meta``, already on a
    worker, already the sidecar's write path -- in
    the SAME order they ran here (redact while the text is whole, then strip, then
    slice). One choke point instead of two; see that function for why each step
    sits where it does.

    The STRIP went with them rather than staying behind, and that is the subtle
    half. It has to run AFTER the redaction: the redactor matches the REGISTERED
    value against untouched text, so a secret whose value carries edge whitespace
    (a hand-edited ``.env`` with a quoted ``" secret-token "``) stops matching the
    moment ``strip`` eats that edge -- leaving the rest of the value to ride on
    unmasked. Keeping a "harmless" strip here would therefore have quietly
    reopened that leak from the other side. What is left is the EMPTINESS
    decision, which reads ``.strip()`` without rewriting the value.

    An EMPTY (or whitespace-only) summary is rejected rather than stored. The
    model returning nothing usable is exactly what ``generate_structured``'s one
    corrective retry exists for, and an empty string is not a summary -- it is
    indistinguishable from the placeholder a FAILED generation writes, so
    accepting it would make the sidecar lie about whether a summary was ever
    produced.

    A reply carrying a lone Unicode surrogate is REJECTED here too (``_coerce_str``
    encodes strictly), and that stays deliberate: data that has not landed yet is
    refused at validation -- one corrective retry, then a 502 -- while data that
    has already happened is scrubbed at its recording/write boundary
    (``llm_log._utf8_safe``, ``tools._redacted``). Same rule as every other
    LLM-facing model in the repo; adjudicated, not incidental.
    """

    model_config = ConfigDict(extra="ignore")

    summary: str = ""

    @model_validator(mode="before")
    @classmethod
    def _sanitize(cls, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        # SHAPE only -- no redaction, no strip, no cap (see the class docstring):
        # this runs on the event loop, and all three belong to the store step.
        return {"summary": _coerce_str(data.get("summary"))}

    @model_validator(mode="after")
    def _summary_required(self) -> ToolSummaryResult:
        if not self.summary.strip():
            raise ValueError("summary must be a non-empty explanation of the tool package")
        return self


# The host[:port] text (userinfo already stripped) must match this SHAPE
# before ``_sanitized_origin_url`` will emit it -- R5-2's fix. ``urlsplit``
# performs no validation of netloc characters, so a credential-shaped string
# like "Bearer SECRET" parses into a non-empty netloc exactly as readily as a
# real hostname does; "it parsed" and "it is a host" are different claims,
# and only the second is safe to return. Two accepted shapes: an IPv6
# literal in its RFC 3986 bracket form, or a label built from the characters
# an actual DNS name / IPv4 literal can contain; either may be followed by
# ":" and an all-digit port. Anything else -- a space, a header-shaped
# token, an empty string -- fails the match, and the caller returns ""
# rather than the raw text.
#
# R6-1: the bracket branch used to accept ANY non-"]" character (``\[[^\]]+\]``)
# on the theory that a balanced bracket pair was itself the shape check --
# it is not. ``urlsplit`` raising on an UNBALANCED bracket says nothing about
# what a BALANCED one contains, and CPython's ``urlsplit`` additionally
# tolerates RFC 3986's IPvFuture shape (``"[" "v" 1*HEXDIG "." 1*(...)  "]"``)
# without validating the part after the dot -- so
# ``https://[v1.Bearer SECRET]/openapi`` parses into the netloc
# ``"[v1.Bearer SECRET]"`` exactly as readily as a real IPv6 literal does.
# That is the SAME "it parsed" trap R5-2 closed for the unbracketed branch,
# reopened one character class later, and it reaches this function's caller
# with the credential-shaped text sitting in the host slot. The inside must
# be shape-checked like everything else, so the bracket branch is now a
# WHITELIST of the characters an IPv6 literal (including an embedded
# IPv4-mapped tail like "::ffff:1.2.3.4") actually contains: hex digits,
# ":", ".". A zone ID (RFC 6874, "%25eth0") and IPvFuture are both excluded
# by that whitelist -- both are vanishingly rare for an OpenAPI host, and
# fail the match into the SAME "" refusal every other unrecognized shape
# gets here, rather than risking a second hole in the same branch by trying
# to enumerate what else to admit.
_HOST_PORT_RE = re.compile(r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]+)?")


def _sanitized_origin_url(url: str) -> str:
    """The install's OpenAPI URL cut down to PROVENANCE: ``scheme://host[:port]``.

    Userinfo, PATH, query and fragment are all dropped now (a marker records
    that something was) -- path is round 5's addition to a list r4 started
    with query/userinfo/fragment, because it turned out to be the same
    structural failure in a new position. A URL is operator text, and any
    part of it beyond the bare authority can carry a value nothing else here
    can catch:

    * the credential is UNKNOWN to us. A presigned document URL
      (``?X-Amz-Signature=...``), a matrix parameter
      (``;jsessionid=<capability-token>`` -- ``urlsplit`` has no concept of
      ``;params``, so this is just PATH to it, and r4's path-preserving
      sanitizer let it straight through), a one-off share link, a token the
      operator pasted but never registered as the install-form secret: no
      redactor can mask a value nobody told it about, and this URL is
      persisted in the sidecar AND replayed into every later regeneration's
      prompt;
    * the credential is KNOWN but TRANSFORMED. A secret ``abc123+/XYZ`` is
      registered verbatim while the URL carries it percent-encoded, in the
      query OR in a path segment -- an exact substring match sees two
      different strings and passes it straight through either way. Chasing
      encodings inside the redactor is a losing game (percent, base64url,
      double-encoding, ...), so the fix stays structural: do not carry a
      part that can hold one.

    r4 kept the path on the theory that it is routing information, not a
    credential; r5 is the correction, and closes the class rather than
    patching this one shape -- there is no syntactic marker that tells a
    general-purpose sanitizer "this path segment is safe, that one is a
    capability token", so nothing past the host survives. Nothing
    FUNCTIONAL is lost: D40 already rules that revise and regenerate never
    re-fetch this URL (the install fetched it once; everything after reads
    the promoted FILES), so the stored value is provenance DISPLAY -- "this
    came from kb.example's OpenAPI document" -- and the host alone says that
    in full. A bare ``scheme://host`` with nothing after it, or with only a
    lone ``/`` (which carries no information of its own), needs no marker:
    there was nothing to cut.

    Refusals degrade to "" (the caller then simply has no URL to show or
    store), never to the raw value:

    * unparseable (``urlsplit`` raises on a malformed IPv6 host), no scheme,
      no host, or a scheme other than http/https (case-insensitive; the
      surviving scheme is normalized to lower);
    * a netloc that PARSES but is not a real host[:port] -- R5-2's finding:
      ``urlsplit`` never validates netloc characters, so
      ``https://Bearer SECRET/openapi`` yields a non-empty netloc
      ("Bearer SECRET") exactly as if it were a hostname, and emitting it
      unexamined would be indistinguishable from the raw-value leak this
      function exists to close. ``_HOST_PORT_RE`` is the check "it parsed"
      was standing in for;
    * a port that is present but not all-digits -- still routing
      information when it IS numeric (the one thing besides the host this
      function keeps), but a non-numeric port is not a cosmetic oddity to
      wave through (r4's stance); it is a netloc that fails the same shape
      check as any other;
    * empty/whitespace, the ordinary "no URL captured" case.

    The rebuild uses the netloc's own TEXT after the last ``@`` rather than
    ``parts.hostname``/``parts.port``: it keeps IPv6 brackets and the
    original host spelling intact, and ``.port`` raises on a non-numeric
    port, which would turn a shape failure into an exception instead of the
    plain "" every other refusal here degrades to.

    Idempotent, so the three call sites (capture, prompt, sidecar
    read-back) can run over an already-sanitized value without stacking
    markers -- but a DIFFERENT mechanism from r4's, and this is the subtle
    part. r4's marker always landed after a real path, so a re-parse read
    it back as harmless path text. With the path gone, the marker would
    glue directly onto the bare host with no ``/`` to separate them -- a
    naive re-parse would read the marker's own characters as part of the
    netloc and fail ``_HOST_PORT_RE``, turning a second pass into a refusal
    instead of a no-op. So a trailing marker is peeled off the input
    BEFORE parsing (and remembered, so it is always reattached to whatever
    the now-clean remainder resolves to) -- which is what keeps a second,
    third, or Nth pass equal to the first.
    """
    stripped = url.strip()
    if not stripped:
        return ""
    already_marked = stripped.endswith(_ORIGIN_URL_TRIMMED_MARKER)
    if already_marked:
        stripped = stripped[: -len(_ORIGIN_URL_TRIMMED_MARKER)]
        if not stripped:
            return ""
    try:
        parts = urlsplit(stripped)
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return ""
    # Everything after the LAST "@" is the host[:port]; a userinfo section (even a
    # malformed one carrying its own "@") is left behind by construction.
    host = parts.netloc.rpartition("@")[2]
    if not host or not _HOST_PORT_RE.fullmatch(host):
        return ""
    base = urlunsplit((scheme, host, "", "", ""))
    trimmed = (
        already_marked
        or parts.netloc != host
        or bool(parts.query)
        or bool(parts.fragment)
        or parts.path not in ("", "/")
    )
    return base + _ORIGIN_URL_TRIMMED_MARKER if trimmed else base


def _package_files(directory: Path) -> list[tuple[str, str]]:
    """Every readable implementation file as ``(relative path, content)``.

    Skips, in this order of importance:

    * every DOT-prefixed name, file or directory -- which is what excludes both
      the tool's ``.env`` (its values are live secrets; only the key NAMES ride
      in the prompt, see ``_env_key_names``) and the version's
      ``.afterthread.meta/`` directory (a summary must never be fed its own
      previous output as if it were source);
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


def _env_key_names(package_root: tools.PackageRoot) -> list[str]:
    """The package ``.env``'s KEY names -- never its values.

    The names are what makes the explanation useful ("it reads KB_API_KEY from
    its environment"); the values are exactly what must never enter a prompt, so
    the file is parsed through the shared bounded loader and only ``.keys()`` is
    taken. A missing or malformed ``.env`` degrades to [] via that loader's own
    contract.
    """
    return sorted(tools._load_tool_dotenv(package_root))


def _summary_user_prompt(
    name: str,
    package_root: tools.PackageRoot,
    version_root: tools.VersionRoot,
    *,
    origin: dict[str, Any] | None,
    builder_summary: str | None,
) -> str:
    """The session's user turn: the package itself, plus its install context.

    Bounded THREE ways, and the third is the ORDER of the sections below.

    The per-file cap above is the INNER bound (no single file may crowd the
    others out of the inventory) and the operator's ONE
    ``llm_prompt_budget_tokens`` knob -- converted to a CHAR allowance through
    the live chars<->tokens ratio and applied to the assembled text behind the
    shared truncation marker -- is the OUTER one (the whole prompt stays inside
    the budget however many files there are).

    Neither of those decides WHAT survives a cut, which is why the SUBJECT is
    emitted before the CONTEXT: header, then ``tool.json``, then the
    implementation files, then the ``.env`` key names, and only THEN the install
    URL / instructions / builder report. A final truncation eats from the END, so
    this order makes it eat background first and package truth last. With the
    context leading (what this did before), a LEGAL floor budget --
    ``llm_prompt_budget_tokens=4000`` is a valid setting, and instructions may be
    20 000 chars by the install schema -- cut inside the context itself and the
    model received ZERO package content, then dutifully invented documentation
    from the instructions alone, which we persisted as the tool's explanation.
    That is the worst possible failure for a summary whose system prompt's
    central rule is "describe ONLY what the files show". Truncation may degrade
    HELPFULNESS; it must never remove the subject.

    EVERY piece is redacted on its UNTOUCHED text, before any strip or cut.
    ``origin`` is what the install captured (the OpenAPI URL and the user's
    instructions -- neither is persisted anywhere else), and ``builder_summary``
    is the builder's own report of what it did; both are CONTEXT for reading the
    files, capped tightly (``_CONTEXT_CAP``) so they can never displace the files
    themselves -- the cap bounds their SIZE, the ordering bounds what they can
    displace when the budget bites anyway.

    The origin fields are OPERATOR-supplied and were the hole here: an install
    URL is routinely ``https://api.example/openapi.json?token=<the form
    secret>``, and that value is registered as an in-flight secret at install
    time -- so the redactor KNOWS it and simply was not asked. Same for a
    relative path (a builder can create a file whose NAME embeds an expanded
    ``$SECRET``, the vector ``tool_builder``'s list_dir already masks) and the
    ``.env`` key-name line. And strip-before-redact is its own leak: a
    registered value carrying edge whitespace stops matching once ``strip`` eats
    that edge, so the strips run AFTER the mask, never before it.

    Asking the redactor was necessary and not SUFFICIENT, which is why the URL
    is reduced to ``scheme://host[:port]`` before it is masked: the path or
    the query can hold a credential the redactor was never told about (a
    presigned link, a capability token riding in a path segment), or one it
    was told about in another encoding (``abc+/`` registered,
    ``token=abc%2B%2F`` in the URL). See ``_sanitized_origin_url``. The same
    limit still applies INSIDE the free-text instructions, and is accepted
    there rather than chased: they are prose, not a structure with a
    droppable part.
    """
    budget = token_budget.char_allowance(get_settings().llm_prompt_budget_tokens)
    origin_data = origin or {}
    parts = [
        f"Explain the installed tool package `{name}`.",
    ]
    # --- the SUBJECT first (see the ordering note above) ---
    directory = version_root.path
    manifest = tools._read_regular_file_capped(directory / "tool.json", _FILE_CONTENT_CAP)
    if manifest is not None:
        parts.append(
            "tool.json:\n" + _truncate_to(tools.redact_known_secrets(manifest), _FILE_CONTENT_CAP)
        )
    for relative_path, content in _package_files(directory):
        # The CONTENT is already masked by _package_files; the header carrying
        # the path is not, and a filename can embed a value just as a file body
        # can. R6-3: the header can also carry something no MASK fixes -- a
        # POSIX filename that is not valid UTF-8 (e.g. one a builder's
        # run_shell wrote as raw bytes) is decoded by ``os.walk`` through the
        # OS's own surrogateescape convention into a ``str`` carrying a LONE
        # SURROGATE, which no substring-based redaction touches and which
        # then dies at the LLM request's strict UTF-8 serialization -- the
        # SAME filesystem-boundary failure r3's ``tools._utf8_safe`` exists to
        # close (there, a hand-edited sidecar; here, a hand-edited/generated
        # filename). Applied AFTER the redaction, mirroring ``tools._redacted``'s
        # own order: the redactor matches a REGISTERED value against untouched
        # text, and the scrub only ever replaces a surrogate with U+FFFD, which
        # carries nothing to mask -- so running it second changes nothing the
        # redaction pass would otherwise catch.
        safe_relative_path = tools._utf8_safe(tools.redact_known_secrets(relative_path))
        parts.append(f"{safe_relative_path}:\n{content}")
    keys = _env_key_names(package_root)
    if keys:
        # NAMES only -- see _env_key_names. Stated as environment variables
        # because that is how the runtime hands them to the tool.
        parts.append(
            "The package has a .env providing these environment variables "
            "(values withheld): " + tools.redact_known_secrets(", ".join(keys))
        )
    # --- then the CONTEXT, which is what a budget cut is allowed to eat ---
    openapi_url = origin_data.get("openapi_url")
    if isinstance(openapi_url, str):
        # SANITIZE first, then redact (belt and braces): the sanitizer drops the
        # credential-bearing parts no redactor could match -- an unregistered
        # presigned token, a KNOWN secret that appears percent-encoded -- and the
        # redactor still masks a registered value that survived in the host or
        # path. The emptiness test is on the SANITIZED value, so an unparseable or
        # absent URL simply contributes no line rather than an empty label.
        sanitized_url = _sanitized_origin_url(openapi_url)
        if sanitized_url:
            parts.append(
                "It was built from this OpenAPI document: "
                + tools.redact_known_secrets(sanitized_url)
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

    The URL is re-sanitized on the way back IN, not merely trusted. Installs
    sanitize at capture (``tool_builder`` hands us a host-only URL: no path,
    userinfo, query or fragment), but a sidecar written BEFORE r4/r5 -- or
    hand-edited since -- can hold the raw ``?token=...`` form or a
    path-borne capability token, and this is the door either would come
    back through: into the regeneration's prompt, and then back onto disk
    when the store rewrites the origin it was given. Sanitizing here closes
    both, and heals the file in passing: the first regeneration of a legacy
    package rewrites its origin in the reduced form. Re-sanitizing an
    already-sanitized URL is a no-op, so this costs nothing on the normal
    path.
    """
    if meta is None:
        return None
    origin = meta.get("origin")
    if not isinstance(origin, dict):
        return None
    kept: dict[str, Any] = {}
    for key, value in origin.items():
        if not isinstance(value, str):
            continue
        if key == "openapi_url":
            # ALWAYS the sanitized form, "" included: a legacy raw URL must be
            # replaced by what we can safely keep, never inherited around this.
            kept[key] = _sanitized_origin_url(value)
        elif key == "instructions":
            kept[key] = value
    return kept or None


def _resolve_package(
    name: str,
) -> tuple[tools.PackageRoot, tools.VersionRoot, tuple[int, int, int]] | None:
    """Resolve a package by name AND take its identity, in ONE blocking hop.

    The pair, never one without the other, because every summary path here has
    the same shape: resolve a directory, spend an LLM round trip, write into that
    directory. Between those two moments the operator can delete the package and
    install a DIFFERENT one under the same name -- and a write addressed by path
    alone would then persist package A's summary, and A's origin, into package B.
    So the identity of what was resolved is captured here, at the resolve, and
    carried to the store, which re-checks it in the instant before it writes (see
    ``tools.store_summary_meta``).

    None means "no package to summarize", and now covers one more case than the
    resolve alone: a directory whose ``tool.json`` cannot be lstat'ed has no
    identity, and the store would refuse it at the end anyway (a check that cannot
    speak must not vouch -- D40 P3b r11). Answering that here rather than there is
    the same call the revise flow makes for the same reason: refuse at the door
    rather than after burning a whole LLM session on it. It costs nothing real --
    such a package is invalid, so it cannot be executed or revised either.

    The alias-refusing resolve (``tools._resolve_package_dir_no_alias``) is shared
    with every other by-name summary path, so "every summary path refuses an
    internal alias" stays true by construction.

    BLOCKING: filesystem work, so callers reach it through ``run_in_threadpool``.
    """
    package_root = tools._resolve_package_dir_no_alias(name)
    if package_root is None:
        return None
    resolution = tools.resolve_current(package_root)
    if isinstance(resolution, tools.Unresolved):
        return None
    identity = tools.package_identity(resolution.version_root)
    if identity is None:
        return None
    return package_root, resolution.version_root, identity


def _store_meta(
    version_root: tools.VersionRoot,
    *,
    summary: str,
    origin: dict[str, Any] | None,
    llm_log_id: int | None,
    identity: tuple[int, int, int],
) -> dict[str, Any] | None:
    """Store the new summary and return the sidecar that landed.

    The registry writes through the summary schema/redaction boundary and then
    reads the version metadata back, combining immutable ``origin.json`` with the
    new ``summary.json``. This wrapper narrows ``"not_stored"`` to None.

    ``identity`` is what ``_resolve_package`` saw when it resolved ``directory``,
    threaded through unchanged: the store re-checks it against the path in the
    instant before it writes, so a package swapped out during the LLM round trip
    gets ``"not_stored"`` (-> None) instead of receiving another package's
    summary. It is a required argument all the way down for that reason -- there
    is no call shape in which "I did not think about the identity" is spellable.

    BLOCKING: this does filesystem I/O, so every caller reaches it through
    ``run_in_threadpool`` -- never inline on the event loop.
    """
    _outcome, meta = tools.store_summary_meta(
        version_root,
        summary=summary,
        origin=origin,
        llm_log_id=llm_log_id,
        expected_identity=identity,
    )
    return meta


async def _generate_summary(
    name: str,
    package_root: tools.PackageRoot,
    version_root: tools.VersionRoot,
    *,
    origin: dict[str, Any] | None,
    builder_summary: str | None,
) -> str:
    """Run ONE summary session and return its validated text.

    The prompt is built on a THREADPOOL worker, not inline. ``_summary_user_prompt``
    looks like a pure string builder, but it walks the whole package with
    ``os.walk``, opens and reads every file it keeps, and parses the ``.env`` --
    blocking filesystem work, which is exactly what ``run_in_threadpool`` is for
    everywhere else in this codebase (the registry calls in ``routers.tools``,
    the installer's promote/cleanup). The bound that is NOT there is the one
    people assume: ``_MAX_FILES`` caps how many files are KEPT, not how many
    ``os.walk`` must ENUMERATE, so a package directory a ``run_shell`` exploded a
    ``node_modules`` into stalls the event loop -- and therefore every other
    request in the process -- for the whole enumeration before the LLM call even
    starts.

    ``_summary_user_prompt`` itself stays synchronous and pure so its (many)
    tests keep calling it directly; the hop belongs here, at the one async
    boundary that has an event loop to protect.

    ``tools=None``: this session only reads the package we already handed it in
    the prompt -- it has no business calling the installed tools (or any other),
    and passing None keeps the request byte-identical to a plain structured call.
    The default timeout applies (unlike the builder's, this is one short
    single-turn generation, not a multi-round build).

    Raises ``LLMNotConfiguredError`` / ``LLMUpstreamError`` unchanged; each
    caller decides whether that is swallowed (the install hook) or surfaced (the
    synchronous regenerate route). A failure inside the prompt build (the
    fail-closed redactor, say) surfaces out of the ``await`` the same way it used
    to surface out of the direct call.
    """
    user_prompt = await run_in_threadpool(
        _summary_user_prompt,
        name,
        package_root,
        version_root,
        origin=origin,
        builder_summary=builder_summary,
    )
    result = await generate_structured(
        _SUMMARY_SYSTEM_PROMPT,
        user_prompt,
        ToolSummaryResult,
        workflow=_SUMMARY_WORKFLOW,
        tools=None,
    )
    return result.summary


def _summary_session_log_id(before: int | None) -> int | None:
    """The log id of a summary session THIS call ran, or None if none ever started.

    ``last_record_id_for_workflow`` answers "the newest ``tool_summary`` record",
    which is only the same question as "the record of the call I just made" when
    that call actually opened a session. ``_generate_summary`` builds its prompt
    FIRST -- the fail-closed redactor, the ``.env`` parse, a walk over every file
    in the package -- and only then calls ``generate_structured``, which is where
    a record is created. A failure in the build half therefore leaves the reading
    exactly as it was: the PREVIOUS summary session's id, belonging to whatever
    tool was summarized last. Stamping that onto this tool's failure placeholder
    presents another tool's prompt and response as this one's failure trace.

    So the reading taken BEFORE the generation is what gives the one after it a
    meaning: unchanged means no session of mine exists -> None, which
    ``llm_log_id`` already spells (the sidecar writes null, the FE renders no
    ``查看 AI 日誌`` anchor) and which needs no new vocabulary.

    Asking the generation itself would be more direct, and is deliberately NOT
    done: ``last_record_id_for_workflow``'s own docstring adjudicates that
    threading a log id back through ``generate_structured`` -- the boundary every
    workflow and test is built against -- is not worth rewriting for a single
    consumer, and this is that same consumer asking the same favour.

    Inherits that docstring's stated imprecision unchanged: a CONCURRENT summary
    session finishing inside this window moves the reading, so its id is what a
    failed build stamps. That is the adjudicated worst case (裁決紀錄 #3) -- a
    debugging link to a simultaneous session of the SAME workflow, one keypress
    apart in a single-user local tool -- and is strictly better than the previous
    behaviour it replaces, which linked a session that had already finished
    before this call began.
    """
    current = llm_log.last_record_id_for_workflow(_SUMMARY_WORKFLOW)
    return None if current == before else current


async def generate_and_store_summary(
    target: tools.Resolved,
    *,
    origin: dict[str, Any] | None = None,
    builder_summary: str | None = None,
) -> None:
    """Best-effort summary publication for one just-published version.

    ``origin.json`` already committed with the version and is never rewritten
    here. A placeholder may record this summary attempt; every write is bound to
    the VersionRoot identity, and no failure can undo the install or revise.

    Install and revise pass the exact :class:`Resolved` their publishers
    constructed, so this hook cannot follow a later ``current`` edit onto a
    different version.
    """
    try:
        # Keep the publisher's typed work->hook identity intact. Capturing the
        # manifest identity from THAT VersionRoot makes every later write fail
        # closed if the published version itself is edited or removed;
        # ``current`` is intentionally irrelevant here.
        package_root = target.package_root
        version_root = target.version_root
        identity = await run_in_threadpool(tools.package_identity, version_root)
        if identity is None:
            return
        name = package_root.path.name
        # origin.json is already the commit marker. When the caller supplies that
        # context, create an empty summary placeholder before the LLM round trip;
        # no previous workflow id is attached because this session has not run.
        placeholder_stored = origin is not None and isinstance(
            await run_in_threadpool(
                _store_meta,
                version_root,
                summary="",
                origin=origin,
                llm_log_id=None,
                identity=identity,
            ),
            dict,
        )
        # Read BEFORE the generation, because the placeholder below can only tell
        # "the session I just ran" from "whatever was already newest" by the
        # difference (see _summary_session_log_id). Cheap: one locked pass over
        # the in-memory ring.
        log_id_before = llm_log.last_record_id_for_workflow(_SUMMARY_WORKFLOW)
        try:
            summary = await _generate_summary(
                name,
                package_root,
                version_root,
                origin=origin,
                builder_summary=builder_summary,
            )
        except Exception:
            # ``placeholder_stored`` short-circuits the read because the sidecar it
            # would find is the one written above -- an empty summary this call
            # authored, not a good one worth protecting. Nothing else can have
            # replaced it meanwhile: a running install/revise job holds the single
            # flight, so a synchronous regenerate cannot be admitted inside this
            # window (``tool_builder._admit_job`` / ``reserve_sync_operation``).
            if (
                placeholder_stored
                or await run_in_threadpool(tools.read_tool_meta, version_root) is None
            ):
                # Best-effort, so the write result is DELIBERATELY ignored here
                # and below: a refused sidecar must never fail an install that
                # already succeeded (see this function's contract). The PLACEHOLDER
                # is identity-guarded exactly like the real summary and carries
                # only this attempt's log id.
                await run_in_threadpool(
                    _store_meta,
                    version_root,
                    summary="",
                    origin=origin,
                    llm_log_id=_summary_session_log_id(log_id_before),
                    identity=identity,
                )
            return
        await run_in_threadpool(
            _store_meta,
            version_root,
            summary=summary,
            origin=origin,
            llm_log_id=llm_log.last_record_id_for_workflow(_SUMMARY_WORKFLOW),
            identity=identity,
        )
    except Exception:
        # Total backstop: a bug in prompt building, the sidecar read, or the
        # filesystem must not turn a SUCCESSFUL install into a failed job.
        return


async def regenerate_summary(
    name: str,
    resolution: tools.Resolved | None,
) -> dict[str, Any] | None:
    """Regenerate exactly the request-validated version's summary synchronously.

    LLM configuration and upstream failures propagate to the route. Filesystem or
    identity refusals return None, the previous summary is never blanked on failure,
    and immutable origin context is read from that same VersionRoot. ``resolution``
    is never re-derived from ``name``: its vid is the one the route compared and
    will put in a successful response.
    """
    if resolution is None:
        return None
    current = await run_in_threadpool(tools.resolve_current, resolution.package_root)
    if isinstance(current, tools.Unresolved) or current.vid != resolution.vid:
        # D21 permits hand-editing ``current`` between the route's comparison
        # and this coroutine being entered. Following the name to the new vid
        # would spend the request on a version it never authorized.
        raise VersionMismatchError
    package_root = resolution.package_root
    version_root = resolution.version_root
    identity = await run_in_threadpool(tools.package_identity, version_root)
    if identity is None:
        return None
    origin_document = await run_in_threadpool(tools.read_origin_meta, version_root)
    origin = _stored_origin({"origin": origin_document} if origin_document is not None else None)
    summary = await _generate_summary(
        name,
        package_root,
        version_root,
        origin=origin,
        builder_summary=None,
    )
    return await run_in_threadpool(
        _store_meta,
        version_root,
        summary=summary,
        origin=origin,
        llm_log_id=llm_log.last_record_id_for_workflow(_SUMMARY_WORKFLOW),
        identity=identity,
    )
