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
   ``get_record`` for the "AI 日誌" page and for tests;
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
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from context_memory.config import get_settings

# The one logger the whole feature uses. Named under the app's package so an
# operator can raise/lower just this feature's verbosity independently, and so
# the test that asserts no secret ever reaches logging has a single, stable
# target. It only ever emits the fixed INFO summary in `_log_summary` and the
# file-sink WARNING in `_write_file_sink` -- never a body, never a config value.
logger = logging.getLogger("context_memory.llm")


@dataclass(slots=True)
class LlmAttempt:
    """One request/response round of a single interaction.

    ``request_messages`` is the EXACT list sent to ``chat.completions.create``
    for this attempt (system prompt with the injected JSON Schema, the user
    prompt, and -- on the corrective retry -- the echoed bad reply plus the
    correction), snapshotted so a later rebuild of the running message list can
    never rewrite an already-recorded attempt. ``response_content`` is the raw
    completion text (or None when the attempt produced no usable content or
    failed before one). ``error`` is a SAFE category string when the attempt
    itself failed (a transport category, an empty-response category, or the
    parse/validation error's class name) -- never a config value, and never the
    full pydantic detail (the response body it was judged against is already
    recorded above it).
    """

    request_messages: list[dict[str, str]]
    response_content: str | None = None
    error: str | None = None


@dataclass(slots=True)
class LlmInteractionRecord:
    """One finished LLM interaction, as appended to the ring.

    ``id`` is a monotonic per-process integer assigned when the interaction
    STARTS (so it is stable for the whole call), independent of the ring's
    finish-ordered eviction. ``usage`` is ``{prompt_tokens, completion_tokens,
    total_tokens}`` read defensively from the last completion that carried a
    usage object (any field a merely-compatible gateway omits or malforms is
    None), or None when no completion reported usage at all. ``error`` is the
    SAFE terminal category for a failed outcome, or None on success.
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


def _message_text(content: Any) -> str:
    """Coerce a message ``content`` to UTF-8-safe text for the record.

    Every message this codebase sends carries a plain string content, so the
    isinstance branch is normally an identity; it coerces defensively only so
    a future non-string content can never make snapshotting an attempt raise
    into the LLM call. ``_utf8_safe`` is applied unconditionally here because
    an attempt's request_messages can themselves carry raw LLM output -- the
    corrective retry echoes the model's own (possibly malformed) previous
    reply back as an "assistant" message -- not just our own prompts.
    """
    text = content if isinstance(content, str) else str(content)
    return _utf8_safe(text)


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
        "_usage",
        "_workflow",
    )

    def __init__(self, *, workflow: str, model: str) -> None:
        self._id = _allocate_id()
        self._workflow = workflow
        self._model = model
        self._started_at = datetime.now(UTC).isoformat()
        self._started_monotonic = time.monotonic()
        self._attempts: list[LlmAttempt] = []
        self._usage: dict[str, int | None] | None = None
        self._finished = False

    def begin_attempt(self, messages: list[Any]) -> None:
        """Snapshot the messages ACTUALLY sent for a new attempt.

        Copied field-by-field (not aliased) because the caller rebuilds its
        running message list for the corrective retry; without the copy a later
        rebuild could rewrite an attempt already recorded here.
        """
        self._attempts.append(
            LlmAttempt(
                request_messages=[
                    {"role": str(msg.get("role", "")), "content": _message_text(msg.get("content"))}
                    for msg in messages
                ]
            )
        )

    def record_usage(self, completion: Any) -> None:
        """Capture token usage from ``completion`` when it reports any.

        Keeps the LAST completion that carried usage (a corrective retry's
        completion supersedes the first attempt's), and leaves the prior value
        untouched for a completion that reports none.
        """
        usage = _extract_usage(completion)
        if usage is not None:
            self._usage = usage

    def record_response(self, content: str) -> None:
        """Record the raw completion text on the current attempt, UTF-8-safe.

        ``content`` is straight from the LLM (see ``_extract_content`` in
        context_memory.services.llm) and has not passed through any pydantic
        sanitizer; ``_utf8_safe`` is this method's own choke point against the
        same lone-surrogate hazard ``_message_text`` guards for request bodies.
        """
        if self._attempts:
            self._attempts[-1].response_content = _utf8_safe(content)

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
                usage=self._usage,
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
    never disagree on the on-the-wire shape.
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
                "response_content": attempt.response_content,
                "error": attempt.error,
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


def _write_file_sink(record: LlmInteractionRecord) -> None:
    """Append the full record as one JSON line to ``settings.llm_log_file``.

    Off entirely when the setting is empty (the default). Opened/written/closed
    per record -- simple and robust against an operator truncating or rotating
    the file between calls. A write failure must NEVER break the LLM call, so
    the catch below is ``Exception``, not just ``OSError``: the stated contract
    is "sink failures never break the LLM call", full stop, and an OSError-only
    catch does not actually keep it -- e.g. a body that ever reached here
    without going through ``_utf8_safe`` could raise ``UnicodeEncodeError`` (a
    ``ValueError`` subclass, not an ``OSError``) on the ``.write`` below, and a
    future non-JSON-safe field could raise from ``json.dumps`` itself. The
    filename is the operator's own config (not a secret) so naming it is fine
    and useful, but only its CATEGORY is logged, never ``str(exc)`` (which could
    carry additional path detail without adding diagnostic value). ``ensure_ascii
    =False`` keeps CJK memory content readable in the file rather than escaped.
    """
    path = get_settings().llm_log_file.strip()
    if not path:
        return
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
    name (the one ``/llm/status`` already reports). No message body and, because
    none of these is derived from the endpoint config, no base URL or key can
    ride along -- which is what keeps the caplog no-leak test passing now that
    the app logs at all.
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
