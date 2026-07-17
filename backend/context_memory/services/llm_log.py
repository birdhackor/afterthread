"""In-process structured logging of every LLM interaction (see D09 in
docs/web-v2-decisions.md).

The backend otherwise does no logging at all, on purpose: the ONE hard,
test-pinned invariant it inherits is that ``openai_base_url`` /
``openai_api_key`` never appear in a log line, an exception, or an API response
(context_memory.services.llm builds every error message from a category, never a
config value). This module is the single place that logging is introduced, so
it is written to keep that invariant by CONSTRUCTION rather than by luck:

* The prompt/response BODIES it stores ARE user content (the exact messages sent
  and the model's replies) and are legitimately kept -- in the memory-only ring
  and the opt-in JSONL file -- but they NEVER flow through stdlib logging. The
  only line stdlib ``logging`` ever emits per interaction is a fixed-shape INFO
  summary of non-secret scalars (workflow, outcome, attempt count, duration,
  token totals, the non-secret model name) -- no bodies, and structurally no
  base URL / key, since none of those scalars is derived from either.
* ``model`` is the ONE endpoint-config value that appears anywhere here, and it
  is deliberately the non-secret one ``/llm/status`` already exposes; the base
  URL and key are never read into a record or a log line.

Three sinks, in decreasing durability, all fed from one finished record:

1. a bounded in-memory ring (``deque(maxlen=settings.llm_log_max_entries)``),
   process-wide and dying with the process -- surfaced via ``list_summaries`` /
   ``get_record`` for the "AI 日誌" page and for tests. Bounded on BOTH axes:
   the ring caps entry COUNT, and each entry's own request/response bodies are
   independently capped in SIZE at ``settings.llm_log_body_max_chars`` (see
   ``_stored_body``) -- without the latter, a broken/hostile gateway returning
   multi-MB bodies (kept per attempt, and echoed into a corrective retry's own
   request) could inflate a "bounded" 50-entry ring to hundreds of MB;
2. an OPTIONAL JSONL file (``settings.llm_log_file``), off by default because a
   record carries personal memory content and landing it on disk must be an
   operator choice, not a default;
3. the single stdlib INFO summary line above, which is what the uvicorn console
   shows live.

Concurrency: async request handlers and ``run_in_threadpool`` DB segments both
run in the same process, and ``generate_structured`` is awaited from either, so
the shared ring + id counter are guarded by one module-level ``threading.Lock``
-- the same "module-level, process-wide, outlives any one request" lifetime the
lru_cache'd OpenAI client in context_memory.services.llm already has. File I/O and
the INFO log are done OUTSIDE that lock so a slow disk never serializes every
concurrent interaction behind one writer.
"""

import contextlib
import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from context_memory.config import get_settings

# The one logger the whole feature uses. Named under the app's package so an
# operator can raise/lower just this feature's verbosity independently, and so
# the test that asserts no secret ever reaches logging has a single, stable
# target. It only ever emits the fixed INFO summary in `_log_summary` and the
# file-sink WARNING in `_write_file_sink` -- never a body, never a config value.
# This module deliberately does NOT attach a handler or set a level on it --
# that is an APPLICATION decision, not a library one, and is made exactly once
# for the whole "context_memory" namespace (this logger's parent) by
# context_memory.main._configure_app_logging. This module only NAMES the
# logger and picks what it emits; where those records end up is that other
# module's job.
logger = logging.getLogger("context_memory.llm")


@dataclass(slots=True)
class LlmAttempt:
    """One request/response round of a single interaction.

    ``request_messages`` is the EXACT list sent to ``chat.completions.create``
    for this attempt (system prompt with the injected JSON Schema, the user
    prompt, and -- on the corrective retry -- the echoed bad reply plus the
    correction), snapshotted so a later rebuild of the running message list can
    never rewrite an already-recorded attempt. Every message's content here has
    already passed through ``_stored_body`` (UTF-8-safe, then size-capped at
    ``settings.llm_log_body_max_chars``), so ``request_messages`` is exactly
    what a reader of the ring/JSONL/detail API sees -- never the raw original.

    ``response_content`` is the raw completion text, likewise passed through
    ``_stored_body`` (or None when the attempt produced no usable content or
    failed before one). ``request_chars``/``response_chars`` are the STORED
    (post-``_stored_body``) lengths, answering "how much does the record
    actually hold" rather than "how much did the caller send" --
    ``response_chars`` is None exactly when ``response_content`` is (no
    response ever landed on this attempt), never 0 for that case, so the two
    fields can never disagree about whether a response happened at all.

    ``usage`` is THIS attempt's own ``{prompt_tokens, completion_tokens,
    total_tokens}`` read defensively from ITS OWN completion (see
    ``LlmInteractionRecorder.record_usage`` / ``_extract_usage``) -- None when
    that completion reported no usage object, or when the attempt failed
    before any completion came back. The interaction-level total in
    ``LlmInteractionRecord.usage`` is the SUM of every attempt's usage here,
    not any single attempt's number (see ``_aggregate_usage``).

    ``error`` is a SAFE category string when the attempt itself failed (a
    transport category, an empty-response category, or the parse/validation
    error's class name) -- never a config value, and never the full pydantic
    detail (the response body it was judged against is already recorded above
    it).

    ``truncated`` is True the moment ANY body on this attempt -- a request
    message or the response -- was cut by ``_stored_body``, so a reader can
    tell "this record is honest but incomplete" apart from "this is
    everything" without diffing lengths against the configured cap by hand.
    """

    request_messages: list[dict[str, str]]
    request_chars: int = 0
    response_content: str | None = None
    response_chars: int | None = None
    error: str | None = None
    usage: dict[str, int | None] | None = None
    truncated: bool = False


@dataclass(slots=True)
class LlmInteractionRecord:
    """One finished LLM interaction, as appended to the ring.

    ``id`` is a monotonic per-process integer assigned when the interaction
    STARTS (so it is stable for the whole call), independent of the ring's
    finish-ordered eviction. ``usage`` is the INTERACTION-level total: the
    per-field SUM of every attempt's own usage (see ``LlmAttempt.usage``) that
    reported one at all (see ``_aggregate_usage``), or None when NO attempt
    reported any usage. This is deliberately a SUM, not "whichever completion
    reported last" -- a 150-token failed attempt followed by a 260-token
    successful retry cost 410 tokens end to end, and reporting only 260 would
    understate the real cost of the interaction. ``error`` is the SAFE
    terminal category for a failed outcome, or None on success.
    """

    id: int
    workflow: str
    model: str
    started_at: str
    finished_at: str | None = None
    duration_ms: int | None = None
    outcome: str | None = None
    error: str | None = None
    usage: dict[str, int | None] | None = None
    attempts: list[LlmAttempt] = field(default_factory=list)


# --- shared, process-wide state -------------------------------------------

# One lock guards BOTH the ring and the id counter. The ring is created lazily
# (not at import) so its maxlen reflects the CURRENT settings -- matching the
# lru_cache'd client's "rebuilds when config changes" behaviour and letting a
# test that overrides llm_log_max_entries get a ring of that size after
# `_reset_for_tests()`. Everything touching `_ring`/`_last_id` does so under
# `_LOCK`; the I/O in `finish` happens outside it.
_LOCK = threading.Lock()
_ring: deque[LlmInteractionRecord] | None = None
_last_id = 0


def _get_ring() -> deque[LlmInteractionRecord]:
    """Return the process-wide ring, building it once from current settings.

    Callers MUST hold ``_LOCK``. Built lazily rather than at import so the
    maxlen is read from whatever ``get_settings()`` returns the first time an
    interaction finishes -- in production a constant, in tests whatever the
    override supplies after ``_reset_for_tests()``.
    """
    global _ring
    if _ring is None:
        _ring = deque(maxlen=get_settings().llm_log_max_entries)
    return _ring


def _allocate_id() -> int:
    """Assign the next monotonic per-process interaction id (under ``_LOCK``)."""
    global _last_id
    with _LOCK:
        _last_id += 1
        return _last_id


def _reset_for_tests() -> None:
    """Drop the ring and id counter so a test starts from an empty log.

    Not used at runtime. Lets a test pick a fresh ``llm_log_max_entries`` (via a
    settings override) and have the next ``_get_ring()`` build a ring of that
    size, and keeps the monotonic id space from leaking assertions across tests.
    """
    global _ring, _last_id
    with _LOCK:
        _ring = None
        _last_id = 0


# --- defensive usage extraction -------------------------------------------


def _usage_field(usage: Any, name: str) -> int | None:
    """Read one integer token count from a completion's ``usage``, or None.

    ``usage`` may be an SDK object (attribute access) or a plain dict from a
    merely-compatible gateway (item access); either shape, or a missing/
    non-integer/boolean field, yields None rather than raising. ``bool`` is
    excluded explicitly because it is an ``int`` subclass and a stray ``True``
    is not a token count.
    """
    value = usage.get(name) if isinstance(usage, Mapping) else getattr(usage, name, None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _extract_usage(completion: Any) -> dict[str, int | None] | None:
    """Pull ``{prompt_tokens, completion_tokens, total_tokens}`` from a
    completion, or None when it carries no usage object at all.

    Defensive by design: a conformant OpenAI response has a ``usage`` object,
    but a compatible gateway may omit it (``None`` here) or return one missing
    or malforming any field (that field is None, the others still captured). A
    present-but-all-None usage object still returns a dict, so the record
    honestly shows "the endpoint reported usage but with no usable numbers"
    rather than being indistinguishable from "no usage reported".
    """
    usage = getattr(completion, "usage", None)
    if usage is None:
        return None
    return {
        "prompt_tokens": _usage_field(usage, "prompt_tokens"),
        "completion_tokens": _usage_field(usage, "completion_tokens"),
        "total_tokens": _usage_field(usage, "total_tokens"),
    }


_USAGE_FIELDS: tuple[str, ...] = ("prompt_tokens", "completion_tokens", "total_tokens")


def _aggregate_usage(attempts: list[LlmAttempt]) -> dict[str, int | None] | None:
    """Sum each usage field across every attempt that reported usage at all.

    Replaces "last completion wins": a failed-then-retried interaction (a
    150-token rejected attempt plus a 260-token successful retry) must report
    the FULL cost of the call -- 410 -- not merely the winning attempt's 260,
    so this sums ``LlmAttempt.usage`` over every attempt rather than reading
    only the final one. A gateway may omit usage on some attempts entirely (a
    transport failure never reaches a completion, a timeout aborts before
    one, or a merely-compatible endpoint sometimes skips the usage object) --
    the sum is over REPORTING attempts only, never padded with 0 for the
    rest, so None here means NO attempt reported any usage, not "usage was
    zero". Each field is summed independently and skips a per-attempt None
    for THAT field (mirroring ``_extract_usage``'s own per-field
    defensiveness), so one attempt's malformed ``total_tokens`` cannot blank
    out another attempt's good ``prompt_tokens``; a field is None in the
    result only when EVERY reporting attempt itself left that field None.
    """
    reporting = [attempt.usage for attempt in attempts if attempt.usage is not None]
    if not reporting:
        return None
    aggregate: dict[str, int | None] = {}
    # `field_name`, not `field`: this module also imports dataclasses.field for
    # LlmInteractionRecord.attempts' default_factory, and shadowing that import
    # with a loop variable in the very same module -- even function-local and
    # harmless at runtime -- is exactly the kind of thing that makes a reader
    # (or `ty`) do a double-take over which `field` is meant.
    for field_name in _USAGE_FIELDS:
        values = [usage[field_name] for usage in reporting if usage.get(field_name) is not None]
        aggregate[field_name] = sum(values) if values else None
    return aggregate


# --- known-value secret redaction (provider-injected, D36) ------------------

# The exact-match marker every redacted secret value is replaced with. Its CJK
# body ("秘密已遮蔽" = secret has been masked) and the •••…••• fence make a
# redaction obvious to a reader of the AI 日誌 / JSONL sink, and distinct from
# the truncation/elision markers above (those mean "cut for size", this means
# "removed for secrecy"). ``tools._REDACTION_MARKER`` keeps a byte-for-byte equal
# literal for the LIVE-conversation redactor (see the note there on why it is
# duplicated rather than shared -- llm_log stays leaf); a test pins the two equal.
_REDACTION_MARKER = "•••[秘密已遮蔽]•••"

# Values shorter than this are NEVER redacted (see `_redact`): a 1-5 char value
# would shred ordinary prose (imagine redacting every "1234"), and a real API
# key / token is never that short -- so the floor costs no real coverage while
# removing the false-positive hazard entirely.
_MIN_SECRET_LEN = 6

# The known-secret provider. A CALLABLE, deliberately not a static set, because
# the secret set CHANGES at runtime: installing a tool adds its .env values, and
# an in-flight install registers its form secret before that .env even exists
# (see tools.known_secret_values). llm_log is a leaf OBSERVABILITY module and
# must not import the tool subsystem to learn where secrets come from, so it
# takes this hook and the application entrypoint (main._configure_secret_
# redaction) injects tools.known_secret_values once at startup -- the same
# leaf/provider inversion _configure_app_logging uses for the log handler.
_secret_provider: Callable[[], Iterable[str]] | None = None


def set_secret_provider(provider: Callable[[], Iterable[str]] | None) -> None:
    """Install the callable that yields the values `_redact` masks out.

    Idempotent overwrite (last writer wins) -- main calls it once at startup, and
    a test may swap in its own provider and restore it. Passing None disables
    redaction entirely (the default state before wiring), which is exactly the
    byte-identical "no known secrets" behavior every path had before D36.
    """
    global _secret_provider
    _secret_provider = provider


def _redact(text: str) -> str:
    """Replace every exact occurrence of each known secret value in ``text``.

    The FIRST stage of `_stored_body` (before `_utf8_safe`, before the size cut):
    a secret must be scrubbed BEFORE truncation could split it, or half of a key
    straddling the cap edge would survive in the record. Substring `str.replace`,
    not a regex, so a value containing regex metacharacters is matched literally.

    The provider is called INSIDE a broad `except Exception` because of the
    no-observer-failure invariant: the recorder is a pure observer of the LLM
    call, so a broken/misconfigured secret provider must degrade to "record
    unredacted", never raise into `begin_attempt`/`record_response`/the sink and
    turn a successful call into a logging crash. Guards per value: empty/None is
    skipped (``str.replace("", m)`` would splice the marker between every
    character), and anything under `_MIN_SECRET_LEN` is skipped (see that
    constant). The `in` pre-check just avoids allocating a new string for a
    secret that is not present (the common case: most known secrets appear in no
    given body).
    """
    provider = _secret_provider
    if provider is None:
        return text
    try:
        # Materialize the values INSIDE the guard (F5): the provider may be a lazy
        # iterable that raises at YIELD time, not just at call time, so ``list()`` must
        # be inside this try or such a failure would escape into the recorder. Keep
        # only ``str`` values in the same pass -- a non-str would raise in the ``in`` /
        # ``replace`` below -- so any failure (call, iteration, or a bad element) still
        # degrades to "record unredacted", never raises (the no-observer-failure
        # invariant).
        values = [value for value in provider() if isinstance(value, str)]
    except Exception:
        return text
    redacted = text
    # F4: replace the LONGEST values first. With both "abcdef" and "abcdefXYZ"
    # registered, masking the shorter first would replace it INSIDE the longer one and
    # leave "XYZ" exposed; longest-first masks the superset value before its own prefix.
    for value in sorted(values, key=len, reverse=True):
        if not value or len(value) < _MIN_SECRET_LEN:
            continue
        if value in redacted:
            redacted = redacted.replace(value, _REDACTION_MARKER)
    return redacted


# --- stored-body safety: redact, THEN UTF-8-safe, THEN size-capped ----------


def _utf8_safe(text: str) -> str:
    """Replace any lone (unpaired) Unicode surrogate in ``text`` with U+FFFD.

    Attempt bodies are RAW, untrusted LLM output that -- unlike every other
    string this codebase stores -- never passes through a pydantic model's
    sanitizers: context_memory.services.memory_ai's ``_coerce_str`` gates the
    very same hazard (a lone surrogate is valid per ``json.loads`` but not
    UTF-8 encodable) at the model-validation boundary, but it REJECTS there
    (raises, folded into a 502) because that data has not been written
    anywhere yet. An attempt here has already happened -- rejecting is not an
    option, the interaction must be recorded regardless -- so this is the same
    gate applied at a boundary where REPLACING is the only sound choice. Left
    unfixed, the surrogate would sail into the ring/JSONL sink untouched and
    only blow up LATER as an uncaught ``UnicodeEncodeError`` -- from the detail
    API's response serialization (Starlette's ``JSONResponse.render`` calls a
    strict ``.encode("utf-8")``) or the JSONL sink's file write -- far from
    where the bad code point actually entered.

    Plain ``text.encode("utf-8", errors="replace")`` does NOT yield U+FFFD:
    Python's "replace" error handler substitutes an ASCII "?" on ENCODE, and
    reserves U+FFFD for DECODE. Encoding with ``errors="surrogatepass"``
    instead lets a lone surrogate through as its raw, invalid-UTF-8 byte
    sequence, and decoding THAT back with ``errors="replace"`` is what
    actually produces U+FFFD -- and can never raise, since decoding with
    "replace" has no failure mode.
    """
    return text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")


# Appended when ``_stored_body`` cuts a body for size. Mirrors the naming and
# punctuation of memory_ai's own truncation markers (``_TRUNCATION_MARKER`` =
# "…[內容過長已截斷]", ``_HISTORY_TRUNCATION_MARKER``) so an operator who has
# already learned that convention from item content recognizes this one on
# sight; the distinct wording ("紀錄" = record, vs "內容" = content) still lets
# either marker be told apart from the other if both ever appear near each
# other (e.g. an oversized item section echoed back verbatim inside a stored
# LLM reply).
_BODY_TRUNCATION_MARKER = "…[紀錄過長已截斷]"


def _stored_body(text: str) -> tuple[str, bool]:
    """Make ``text`` safe AND small enough to store; return (stored, truncated).

    The single choke point every request-message/response body passes through
    before it is written into an attempt, in a THREE-stage order that each
    guards a distinct hazard: ``_redact`` FIRST, then ``_utf8_safe``, then the
    hard cut to ``settings.llm_log_body_max_chars`` (with
    ``_BODY_TRUNCATION_MARKER`` appended when a cut happens).

    Redaction MUST run first -- before both the UTF-8 pass and the size cut. The
    size cut is the load-bearing reason: a secret value straddling the
    truncation edge must not be half-scrubbed and half-survive, so it has to be
    replaced while the body is still whole. (Against ``_utf8_safe`` the order is
    harmless either way -- API keys/tokens are plain ASCII, which the surrogate
    pass never alters -- but redact-first is the correct, edge-independent order
    regardless.) Because this is the ONE choke point, redaction reaches ALL
    stored bodies -- every request message and every response -- and both sinks
    (the ring and the JSONL file) see the SAME already-redacted record.

    ``_utf8_safe`` must in turn run before the slice: it never enlarges the text
    (one surrogate becomes one U+FFFD), and once it has run the string holds
    only valid Unicode scalar values, so slicing it by Python's
    code-point-based indexing can never land inside what used to be a lone
    surrogate. Truncating first would risk cutting a bare surrogate at the
    boundary and handing ``_utf8_safe`` a different (index-shifted) string than
    the one actually stored.

    Without the size cap, a broken or hostile OpenAI-*compatible* gateway
    returning a multi-MB body would be kept in full (per attempt, and echoed
    into a corrective retry's OWN next request, compounding the size) --
    multiplied by ``llm_log_max_entries``, that turns a "bounded" ring into
    hundreds of MB from a single pathological interaction. Mirrors
    ``memory_ai._truncate_to``'s edge case for a cap too small to even hold
    the marker: the marker is dropped and the text hard-cut to exactly `cap`.
    ``llm_log_body_max_chars``'s own ``ge=1_000`` floor makes that
    unreachable in practice, but the guard keeps this function correct
    independent of that floor rather than relying on it.
    """
    safe = _utf8_safe(_redact(text))
    cap = get_settings().llm_log_body_max_chars
    if len(safe) <= cap:
        return safe, False
    marker_len = len(_BODY_TRUNCATION_MARKER)
    if cap <= marker_len:
        return safe[:cap], True
    return safe[: cap - marker_len] + _BODY_TRUNCATION_MARKER, True


def _message_text(content: Any) -> tuple[str, bool]:
    """Coerce a message ``content`` to its STORED (safe, size-capped) text.

    Every message this codebase sends carries a plain string content, so the
    isinstance branch is normally an identity; it coerces defensively only so
    a future non-string content can never make snapshotting an attempt raise
    into the LLM call. ``_stored_body`` is applied unconditionally here
    because an attempt's request_messages can themselves carry raw LLM output
    -- the corrective retry echoes the model's own (possibly malformed,
    possibly oversized) previous reply back as an "assistant" message -- not
    just our own prompts. Returns ``(stored_text, truncated)`` so the caller
    (``LlmInteractionRecorder.begin_attempt``) can fold the per-message flag
    into the attempt's own single ``truncated`` bit.
    """
    text = content if isinstance(content, str) else str(content)
    return _stored_body(text)


def _elision_marker(count: int) -> str:
    """The synthetic leading entry's text when older messages are elided.

    Distinct wording from ``_BODY_TRUNCATION_MARKER`` ("紀錄過長" -- a single body
    was cut): here whole earlier MESSAGES were dropped, not one body trimmed, so a
    reader of the AI 日誌 can tell "this attempt's oldest turns were summarized
    away" apart from "one body was truncated". "較早 N 則訊息" = the earlier N
    messages.
    """
    return f"…[較早 {count} 則訊息已省略以控制紀錄大小]"


def _apply_total_budget(
    stored: list[dict[str, str]],
) -> tuple[list[dict[str, str]], int, bool]:
    """Bound ONE attempt's stored request_messages to a TOTAL char budget.

    Each body is already per-message capped (``_stored_body`` at
    ``llm_log_body_max_chars``), but that does NOT bound their SUM: an agentic
    interaction records the FULL accumulated conversation on EVERY tool round, so
    a record's size otherwise grows QUADRATICALLY with rounds (24 installer rounds
    x up to 16 tool results x tens of KB each -> hundreds of MB in ONE of the
    ring's 50 records). This applies the SAME knob (``llm_log_body_max_chars``) a
    second time, now as an AGGREGATE ceiling per attempt.

    Walk NEWEST -> OLDEST keeping messages verbatim while the running total stays
    within budget -- the newest turns matter most for debugging the CURRENT
    attempt, so they are the ones kept whole. The first message that would push
    the total over budget, and every message OLDER than it, are collapsed into ONE
    synthetic leading ``system`` marker (``_elision_marker``) naming how many were
    dropped. Each stored body is itself <= budget (same knob), so the newest
    message always fits and at least one real message is always kept.

    The collapse is SKIPPED when it would not actually save space -- when the tail
    being elided is no larger than the marker that would replace it, keeping those
    messages verbatim is both smaller and simpler. That is what makes a typical
    small conversation (nothing over budget) store EXACTLY as before, byte for
    byte, and also stops a 2-message record whose one oversized body already fills
    the budget from being "optimized" into a LARGER record by eliding a tiny
    system prompt into a longer marker.

    Returns ``(request_messages, elided_count, truncated)``; ``truncated`` is True
    only when an elision actually happened, so the caller ORs it into the
    attempt's existing flag and the FE badge lights up.
    """
    budget = get_settings().llm_log_body_max_chars
    kept_reversed: list[dict[str, str]] = []
    used = 0
    cut_index: int | None = None  # index in `stored` of the first (newest) elided message
    for i in range(len(stored) - 1, -1, -1):
        length = len(stored[i]["content"])
        if used + length <= budget:
            kept_reversed.append(stored[i])
            used += length
        else:
            # Budget exhausted here: this message and everything OLDER (0..i) is
            # the elision tail. Stop -- we do NOT keep hunting for smaller older
            # messages that might still fit, per "collapse ALL remaining older".
            cut_index = i
            break
    if cut_index is None:
        return stored, 0, False  # never exceeded budget -> unchanged
    tail = stored[: cut_index + 1]
    marker = _elision_marker(len(tail))
    if sum(len(message["content"]) for message in tail) <= len(marker):
        return stored, 0, False  # collapsing would not shrink the record
    kept = list(reversed(kept_reversed))  # back to oldest-first
    return [{"role": "system", "content": marker}, *kept], len(tail), True


# --- the recorder ----------------------------------------------------------


class LlmInteractionRecorder:
    """Accumulates one interaction and finalizes it into the sinks exactly once.

    Constructed at the very START of ``generate_structured`` (before the config
    gate), so even a ``not_configured`` exit records its workflow. Every method
    is side-effect-safe: an exception inside logging must never mask or replace
    the real LLM exception, so ``finish`` swallows all its own failures, and the
    in-loop mutators do only trivial in-memory work that cannot raise into the
    caller. ``finish`` is idempotent -- the first call wins and later calls are
    no-ops -- so a normal explicit ``finish`` plus a defensive one are safe.
    """

    __slots__ = (
        "_attempts",
        "_finished",
        "_id",
        "_model",
        "_started_at",
        "_started_monotonic",
        "_workflow",
    )

    def __init__(self, *, workflow: str, model: str) -> None:
        self._id = _allocate_id()
        self._workflow = workflow
        self._model = model
        self._started_at = datetime.now(UTC).isoformat()
        self._started_monotonic = time.monotonic()
        self._attempts: list[LlmAttempt] = []
        self._finished = False

    def begin_attempt(self, messages: list[Any]) -> None:
        """Snapshot the messages ACTUALLY sent for a new attempt.

        Copied field-by-field (not aliased) because the caller rebuilds its
        running message list for the corrective retry; without the copy a later
        rebuild could rewrite an attempt already recorded here. Each message's
        content goes through ``_message_text`` (UTF-8-safe, then size-capped),
        then ``_apply_total_budget`` bounds their AGGREGATE size (the per-body cap
        alone does not -- an agentic call records the whole accumulated
        conversation every round, so a record would otherwise grow quadratically
        with rounds; see ``_apply_total_budget``). ``request_chars`` sums the
        STORED length actually kept -- of the FINAL messages, marker included, not
        the originals -- and ``truncated`` is set the moment ANY message was cut
        for size OR older messages were elided for the aggregate budget;
        ``record_response`` below may OR a response-side cut into the same flag.
        """
        stored: list[dict[str, str]] = []
        truncated = False
        for msg in messages:
            content, was_truncated = _message_text(msg.get("content"))
            stored.append({"role": str(msg.get("role", "")), "content": content})
            truncated = truncated or was_truncated
        request_messages, _elided, budget_truncated = _apply_total_budget(stored)
        request_chars = sum(len(message["content"]) for message in request_messages)
        self._attempts.append(
            LlmAttempt(
                request_messages=request_messages,
                request_chars=request_chars,
                truncated=truncated or budget_truncated,
            )
        )

    def record_usage(self, completion: Any) -> None:
        """Capture THIS ATTEMPT's own token usage, when the completion reports any.

        Usage is no longer interaction-level "last completion wins": each
        attempt keeps only what ITS OWN completion reported (None when that
        completion carried no usage object -- see ``_extract_usage``), and
        ``finish`` sums these per-field across every reporting attempt into
        the interaction-level total (see ``_aggregate_usage``). A failed
        attempt (raised before a completion ever came back -- a transport
        error or a timeout) never calls this method, so its ``usage`` stays
        the dataclass default of None.
        """
        if self._attempts:
            self._attempts[-1].usage = _extract_usage(completion)

    def record_response(self, content: str) -> None:
        """Record the raw completion text on the current attempt, safely stored.

        ``content`` is straight from the LLM (see ``_extract_content`` in
        context_memory.services.llm) and has not passed through any pydantic
        sanitizer; ``_stored_body`` is this method's own choke point against
        both the lone-surrogate hazard ``_message_text`` guards for request
        bodies AND the oversized-body hazard a broken/hostile gateway can
        return. ``response_chars`` mirrors the STORED length -- never 0 for
        "no response" (that stays None, matching ``response_content``'s own
        shape) -- and a cut here is OR'd into the attempt's ``truncated`` flag
        rather than overwriting it, so a request-side cut already recorded by
        ``begin_attempt`` is never lost.
        """
        if self._attempts:
            attempt = self._attempts[-1]
            stored, was_truncated = _stored_body(content)
            attempt.response_content = stored
            attempt.response_chars = len(stored)
            attempt.truncated = attempt.truncated or was_truncated

    def fail_current_attempt(self, category: str) -> None:
        """Record a SAFE failure category on the current attempt."""
        if self._attempts:
            self._attempts[-1].error = category

    def finish(self, *, outcome: str, error: str | None) -> None:
        """Finalize the interaction into every sink, exactly once.

        Wrapped end to end in ``except Exception`` so a broken sink (a full
        disk, a logging misconfiguration) can never turn a successful -- or a
        legitimately-failed -- LLM call into a different exception. ``Exception``
        (not ``BaseException``) so an ``asyncio.CancelledError`` from the
        surrounding deadline still propagates and is never swallowed here.
        ``finish`` must be UNCONDITIONALLY unable to raise, not merely unable
        to raise from its happy path: the fallback WARNING log below is itself
        wrapped in a second, unconditional ``contextlib.suppress(Exception)``
        (see the comment there), so even a pathological logging Handler/Filter
        that raises inside its own emit()/filter() cannot escape this method.
        """
        if self._finished:
            return
        self._finished = True
        try:
            record = LlmInteractionRecord(
                id=self._id,
                workflow=self._workflow,
                model=self._model,
                started_at=self._started_at,
                finished_at=datetime.now(UTC).isoformat(),
                duration_ms=int((time.monotonic() - self._started_monotonic) * 1000),
                outcome=outcome,
                error=error,
                # Interaction-level usage is computed HERE, once, from every
                # attempt's own reading -- not accumulated incrementally as
                # attempts are recorded -- so it can never disagree with what
                # `record.attempts` itself shows (see _aggregate_usage).
                usage=_aggregate_usage(self._attempts),
                attempts=self._attempts,
            )
            with _LOCK:
                _get_ring().append(record)
            # Both sinks below run OUTSIDE the lock: a slow disk or logging
            # handler must not serialize concurrent interactions behind it.
            _write_file_sink(record)
            _log_summary(record)
        except Exception:
            # Last-resort guard: the record is best-effort. Emit one safe line
            # (no bodies, no config) and drop it rather than surface anything.
            # Absolute last resort, via a bare `contextlib.suppress(Exception)`
            # (behaviourally an `except Exception: pass`; ruff's SIM105 prefers
            # this spelling): even this WARNING can raise, if a pathological
            # logging Handler/Filter does not protect its own emit()/filter()
            # the way a well-behaved stdlib handler (e.g. StreamHandler)
            # protects its own. Letting that escape would turn a successful --
            # or legitimately failed -- LLM call into a logging crash purely
            # because of how logging happens to be configured. The recorder is
            # a pure OBSERVER of the call; the ONLY wrong outcome here is
            # affecting the call it observes, so this one spot swallows
            # literally everything and does nothing -- absolute silence is
            # correct BECAUSE no safer action remains.
            with contextlib.suppress(Exception):
                logger.warning("llm interaction record finalization failed", exc_info=False)


# --- sink helpers ----------------------------------------------------------


def _record_detail(record: LlmInteractionRecord) -> dict[str, Any]:
    """Full record as a JSON-ready dict, attempt bodies included.

    Shared by ``get_record`` (the detail API) and the JSONL sink, so the two can
    never disagree on the on-the-wire shape. Per-attempt ``request_chars`` /
    ``response_chars`` / ``usage`` / ``truncated`` are this attempt's OWN
    figures (see ``LlmAttempt``) -- distinct from the top-level ``usage``
    above, which is the INTERACTION-level sum across every attempt.
    """
    return {
        "id": record.id,
        "workflow": record.workflow,
        "model": record.model,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "duration_ms": record.duration_ms,
        "outcome": record.outcome,
        "error": record.error,
        "usage": record.usage,
        "attempts": [
            {
                "request_messages": attempt.request_messages,
                "request_chars": attempt.request_chars,
                "response_content": attempt.response_content,
                "response_chars": attempt.response_chars,
                "error": attempt.error,
                "usage": attempt.usage,
                "truncated": attempt.truncated,
            }
            for attempt in record.attempts
        ],
    }


def _record_summary(record: LlmInteractionRecord) -> dict[str, Any]:
    """Record WITHOUT attempt bodies -- the list view's per-row payload.

    ``attempts`` is the COUNT here (not the list): the "AI 日誌" list shows how
    many attempts a call took, while the full bodies (tens of KB each) load only
    when a row is expanded, via ``get_record``.
    """
    return {
        "id": record.id,
        "workflow": record.workflow,
        "model": record.model,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "duration_ms": record.duration_ms,
        "outcome": record.outcome,
        "error": record.error,
        "usage": record.usage,
        "attempts": len(record.attempts),
    }


# Serializes the ENTIRE stat -> maybe-rotate -> append sequence of the JSONL sink
# (F8). Deliberately NOT the ring's ``_LOCK``: the design keeps slow file I/O OUT of
# the ring's critical section (see the module docstring -- both sinks run outside
# ``_LOCK`` so a slow disk never serializes concurrent interactions behind the ring),
# but rotation is a read-then-modify (stat the file, rename it aside, then append), so
# two interactions finishing at once could both see the file oversized and both rename
# it -- the second clobbering the first's rotated segment, or racing the fresh-file
# append. Its OWN lock makes that sequence atomic w.r.t. other sink writers while
# holding NONE of the ring lock, so the ring's fast path stays uncontended.
_FILE_SINK_LOCK = threading.Lock()


def _rotation_stamp() -> str:
    """The UTC ``YYYYMMDD-HHMMSSZ`` suffix a rotated sink file is named with.

    Factored out of ``_rotate_file_sink`` so a test can monkeypatch it to force two
    rotations onto the SAME second and prove the uniqueness fallback (F8) picks
    distinct targets, without racing wall-clock time.
    """
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%SZ")


def _rotate_file_sink(path: str) -> None:
    """Rename the sink file aside when it has outgrown ``llm_log_file_max_bytes``.

    Run once before every append (see ``_write_file_sink``), so the JSONL sink
    can never grow ONE file without bound (D34). The mechanics are deliberately
    tiny and zero-dependency: stat the current file; if it is over the cap,
    ``os.rename`` it to ``<path>.<UTC YYYYMMDD-HHMMSSZ>`` -- the next append then
    opens a FRESH file at the original path. This keeps ``_write_file_sink``'s
    per-write open/close semantics exactly as they were (that per-write reopen is
    the deliberate operator-robustness choice, and rotation slots in AHEAD of it
    rather than holding a long-lived handle).

    Every failure is fully suppressed -- the same never-break-the-caller contract
    the sink itself carries. A missing file (the first-ever write) makes
    ``getsize`` raise ``OSError`` and simply means "nothing to rotate"; a failed
    rename leaves the file in place and the append proceeds onto it (oversized,
    but recorded), never surfacing an error. The broad final ``except Exception``
    backstops even a pathological ``get_settings``/``strftime``, because this
    helper runs OUTSIDE ``_write_file_sink``'s own try and MUST NOT be the thing
    that breaks a recording. NO retention/deletion of the rotated segments is
    done here: pruning old files is the operator's call (config comment says so).
    """
    try:
        if os.path.getsize(path) <= get_settings().llm_log_file_max_bytes:
            return
    except OSError:
        # Missing file (nothing written yet) or an unstattable path: nothing to
        # rotate. Any other stat failure likewise degrades to "leave it alone".
        return
    except Exception:
        # Defensive: rotation is best-effort and must never raise into the sink.
        return
    stamp = _rotation_stamp()
    with contextlib.suppress(Exception):
        # F8(b): two rotations within the SAME second would collide on the timestamped
        # name (and, absent the _FILE_SINK_LOCK serialization, could clobber each
        # other). Find a free target: ``<stamp>``, then ``<stamp>-1``, ``-2``, ... --
        # ``lexists`` so a broken symlink or any pre-existing entry still counts as
        # taken. Bounded: after 100 tries, GIVE UP rotating and return (the append that
        # follows lands on the still-oversized file, one write past the cap, which the
        # next write rotates) -- all inside the suppress, since rotation is best-effort
        # and must never break the sink.
        target = f"{path}.{stamp}"
        suffix = 0
        while os.path.lexists(target):
            suffix += 1
            if suffix > 100:
                return
            target = f"{path}.{stamp}-{suffix}"
        os.rename(path, target)


def _write_file_sink(record: LlmInteractionRecord) -> None:
    """Append the full record as one JSON line to ``settings.llm_log_file``.

    Off entirely when the setting is empty (the default). Opened/written/closed
    per record -- simple and robust against an operator truncating or rotating
    the file between calls. Before each append, ``_rotate_file_sink`` renames the
    file aside if it has grown past ``llm_log_file_max_bytes`` (D34), so the
    still-per-write open below lands on a fresh file after a rotation. A write
    failure must NEVER break the LLM call, so the catch below is ``Exception``,
    not just ``OSError``: the stated contract is "sink failures never break the
    LLM call", full stop, and an OSError-only catch does not actually keep it --
    e.g. a body that ever reached here without going through ``_utf8_safe`` could
    raise ``UnicodeEncodeError`` (a ``ValueError`` subclass, not an ``OSError``)
    on the ``.write`` below, and a future non-JSON-safe field could raise from
    ``json.dumps`` itself. The filename is the operator's own config (not a
    secret) so naming it is fine and useful, but only its CATEGORY is logged,
    never ``str(exc)`` (which could carry additional path detail without adding
    diagnostic value). ``ensure_ascii=False`` keeps CJK memory content readable
    in the file rather than escaped.
    """
    path = get_settings().llm_log_file.strip()
    if not path:
        return
    # F8(a): serialize the ENTIRE rotate + append under the dedicated _FILE_SINK_LOCK
    # (never the ring's _LOCK -- see that lock's comment) so two interactions
    # finishing at once cannot both rotate-and-clobber or interleave their appends.
    with _FILE_SINK_LOCK:
        _rotate_file_sink(path)
        try:
            line = json.dumps(_record_detail(record), ensure_ascii=False)
            with open(path, "a", encoding="utf-8") as handle:
                # A failure mid-write (e.g. disk full) can still leave a truncated,
                # unparsable final line even though the whole write sits inside
                # this try -- the OS may already have flushed a partial chunk.
                # Adjudicated as acceptable for an opt-in debug sink: not worth a
                # transactional write (temp file + atomic rename) here, so a JSONL
                # consumer should tolerate/skip a trailing line that fails to
                # json.loads rather than assume every line is well-formed.
                handle.write(line + "\n")
        except Exception as exc:
            logger.warning("llm log file sink write failed (%s): %s", path, type(exc).__name__)


def _log_summary(record: LlmInteractionRecord) -> None:
    """Emit the single non-secret INFO summary line for this interaction.

    Every field here is a scalar that is safe by construction -- workflow,
    outcome, attempt count, duration, total tokens, and the non-secret model
    name (the one ``/llm/status`` already reports). ``tokens`` is the
    AGGREGATE total across every attempt (``record.usage``, see
    ``_aggregate_usage``), not any single attempt's number -- a corrective
    retry's full cost, not just its winning attempt's. No message body and,
    because none of these is derived from the endpoint config, no base URL or
    key can ride along -- which is what keeps the caplog no-leak test passing
    now that the app actually has a console handler configured for it (see
    context_memory.main._configure_app_logging).
    """
    total_tokens = record.usage.get("total_tokens") if record.usage else None
    logger.info(
        "llm interaction workflow=%s outcome=%s attempts=%d duration_ms=%s tokens=%s model=%s",
        record.workflow,
        record.outcome,
        len(record.attempts),
        record.duration_ms,
        total_tokens,
        record.model,
    )


# --- read API (backs the /api/llm/logs endpoints) --------------------------


def list_summaries(limit: int) -> list[dict[str, Any]]:
    """Return up to ``limit`` finished records, NEWEST first, without bodies.

    The ring appends newest at the right, so newest-first is a reverse of a
    locked snapshot; the (body-free) summary mapping is built outside the lock.
    """
    with _LOCK:
        records = list(_get_ring())
    return [_record_summary(record) for record in reversed(records)][:limit]


def get_record(log_id: int) -> dict[str, Any] | None:
    """Return the full record (attempt bodies included) for ``log_id``, or None.

    Searches a locked snapshot of the ring; a record evicted by maxlen, or an
    id belonging to an interaction still in flight (not yet finished into the
    ring), is simply absent -> None -> the router's 404.
    """
    with _LOCK:
        records = list(_get_ring())
    for record in records:
        if record.id == log_id:
            return _record_detail(record)
    return None


def last_record_id_for_workflow(workflow: str) -> int | None:
    """Id of the NEWEST finished record for ``workflow``, or None if there is none.

    Exists for the tool installer (context_memory.services.tool_builder), which
    wants to link its job result to the AI 日誌 record of the builder session it
    just ran. ``generate_structured`` deliberately returns only the caller's
    validated model -- threading a log id through its signature (or returning a
    tuple) would rewrite the one boundary every workflow and test is built
    against, for the benefit of a single consumer -- so that consumer instead
    reads the newest record for its OWN workflow name immediately after its
    call returns; ``generate_structured`` finalizes the record before
    returning/raising, so the record is guaranteed to be in the ring by then.
    Known, accepted imprecision: if TWO interactions of the same workflow
    overlap, whichever finished last wins, so a concurrent install's job could
    link the sibling install's log. That worst case is a debugging link to a
    simultaneous builder session in this single-user local tool -- still
    useful, never harmful -- and does not justify rewriting the
    generate_structured contract.
    """
    with _LOCK:
        records = list(_get_ring())
    for record in reversed(records):
        if record.workflow == workflow:
            return record.id
    return None
