"""Dynamic character<->token ratio estimation for the two LLM prompt budgets
(v3 P4).

The model's real constraint -- and the number the OpenAI-compatible endpoint
reports back in ``usage.prompt_tokens`` -- is TOKENS, but this codebase bounds
prompt content in CHARACTERS: the item snapshot in
afterthread.services.memory_ai, the OpenAPI document in
afterthread.services.tool_builder, and the live tool-loop conversation in
afterthread.services.llm are all cut with char-denominated cutters. Chars were
always a proxy for tokens; this module closes the gap WITHOUT a tokenizer
dependency by keeping a rolling window of (chars we sent, tokens the endpoint
counted) pairs from recent completions and deriving a live chars<->tokens ratio
from them, so a TOKEN-denominated budget can be converted into the CHAR
allowance those existing cutters consume (``char_allowance``).

Deliberately NO tokenizer: a real tokenizer would pin the app to one model
family, add a heavy dependency, and STILL only estimate the endpoint's own
counting. The endpoint already reports the ground truth (``prompt_tokens``) for
free on every completion, so the ratio is LEARNED from that rather than modelled.

Module style mirrors afterthread.services.llm_log: one module-level
``threading.Lock`` guards the shared window -- the same process-wide "outlives
any one request" lifetime the lru_cache'd client and the llm_log ring already
have -- and ``_reset_for_tests`` drops the state so a test starts cold.

The estimator is a pure OBSERVER of the LLM call, exactly like the recorder:
``observe`` NEVER raises, so a malformed usage report can never break the call
that produced it (see that function).
"""

import threading
from collections import deque
from typing import Any

# The rolling window of (chars_sent, prompt_tokens) observations feeding the
# ratio. Bounded (maxlen) so old interactions age out and the ratio tracks the
# CURRENT model/content mix rather than the whole process history; 50 smooths
# per-call noise while still turning over within a session. A fixed module
# constant, NOT a settings knob -- unlike the llm_log ring, whose maxlen is an
# operator-facing durability choice -- because this is an internal smoothing
# detail, so the window is built once at import (its maxlen never depends on
# settings, so there is no reason to defer its construction the way llm_log's
# ring does).
_WINDOW_MAXLEN = 50
_WINDOW: deque[tuple[int, int]] = deque(maxlen=_WINDOW_MAXLEN)

# One lock guards the window. Async request handlers and run_in_threadpool DB
# segments both run in this one process and both reach generate_structured, so
# concurrent observe()/tokens_per_char() calls are serialized here -- the same
# reason llm_log guards its ring with a single module-level lock.
_LOCK = threading.Lock()

# Below this many observations the window is too small to trust, so
# tokens_per_char returns the cold-start default rather than a ratio computed
# from one or two noisy samples.
_MIN_SAMPLES = 3

# The cold-start ratio, in force until _MIN_SAMPLES observations exist. 1.0 (one
# token per char) is the CJK WORST CASE on purpose: English is ~0.25 tok/char,
# code ~0.3-0.5, and even dense CJK ~0.6-1.2, so 1.0 OVER-estimates token cost
# for essentially all real content. Over-estimating token cost UNDER-fills a
# token budget (fewer chars admitted) -- the SAFE direction, a prompt that fits
# rather than one that overflows the context window. It also makes day-one
# behavior char-for-char what the old char budgets were: a 200k-token budget at
# ratio 1.0 admits 200k chars, exactly the old 200k-char default.
_DEFAULT_RATIO = 1.0

# Clamp bounds on the observed ratio. The FLOOR is LOAD-BEARING as the absolute
# char backstop: at ratio 0.1 a T-token budget admits at most 10*T chars, so the
# OLD char-ceiling SCALE is preserved no matter how skewed the observations get
# -- a run of unusually token-light content (an ASCII-heavy dump) can never
# inflate a token budget into an unbounded char allowance. The CEILING guards the
# other direction: an absurd usage report (a gateway wildly over-counting tokens)
# must not shrink a budget to nothing; 2.0 tok/char is already past any real text
# (even dense CJK), so clamping there discards the nonsense while still bounding
# the allowance to something usable.
_RATIO_FLOOR = 0.1
_RATIO_CEIL = 2.0

# The minimum char allowance any token budget maps to. Even a tiny budget, or a
# ceiling-clamped ratio, must leave room for a usable prompt -- a budget that
# collapsed to a few hundred chars would 502 every call with an unanswerable stub
# -- so char_allowance never returns below this.
_ALLOWANCE_FLOOR = 1000

# The sanity ceiling on a single observation, applied to BOTH sides of the pair.
# A completion against even a 1M-context model can never legitimately report
# more than ten million prompt tokens; a report above that is a broken or
# hostile gateway (``_extract_usage`` only checks "positive int", not
# "plausible") and must be rejected before it ever reaches the window -- once
# an astronomically large int sits in _WINDOW, Σtokens / Σchars can raise
# OverflowError converting the ratio to a float (see _ratio_locked), which
# would break every LLM completion and GET /llm/status on every call until the
# offending samples age out -- and since the window only rolls forward on NEW
# completions, a call that raises here can never produce one, so it would never
# recover on its own. ``chars`` is guarded by the same constant belt-and-braces:
# it comes from our own _conversation_chars and is budget-bounded in practice,
# but the check is one comparison and costs nothing.
_MAX_OBSERVED_TOKENS = 10_000_000


def _ratio_locked() -> float | None:
    """The clamped observed ratio, or None below _MIN_SAMPLES. Caller holds _LOCK.

    The one place the aggregate Σtokens / Σchars is computed and clamped, shared
    by ``tokens_per_char`` and ``snapshot`` so the two can never disagree on the
    number. Aggregate (sum over the window) rather than a mean of per-call ratios
    so a few tiny prompts cannot swing it as much as the bulk of the content --
    the totals weight each observation by its size, which is exactly what a budget
    conversion cares about. Σchars is a sum of positive ints (``observe`` rejects
    non-positive), so at >= _MIN_SAMPLES it is never zero.

    The ceiling/floor clamp is checked BEFORE the division, as plain integer
    comparisons on the totals, rather than dividing first and clamping the
    float result: Python promotes int/int true division to float, and float()
    on an astronomically large quotient raises OverflowError. ``observe`` now
    rejects any single observation above _MAX_OBSERVED_TOKENS, but this
    ordering is defense in depth for a huge value that reaches the window by
    some future path -- an OverflowError here would raise out of
    tokens_per_char/snapshot on EVERY call while the bad samples sit in the
    window (up to _WINDOW_MAXLEN calls, since the window only rolls forward on
    NEW completions and a call that raises here can never produce one),
    breaking every LLM completion and GET /llm/status until they age out on
    their own. Comparing the plain ints first can never overflow.
    """
    if len(_WINDOW) < _MIN_SAMPLES:
        return None
    total_chars = sum(chars for chars, _ in _WINDOW)
    total_tokens = sum(tokens for _, tokens in _WINDOW)
    # total_tokens / total_chars >= _RATIO_CEIL, i.e. total_tokens >=
    # total_chars * 2 -- the "* 2" is only correct because _RATIO_CEIL == 2.0
    # (pinned by test_ratio_ceil_and_floor_pin_the_integer_comparison_literals).
    # >= matches the old min/max clamp's boundary: exactly-at-ceiling clamps too.
    if total_tokens >= total_chars * 2:
        return _RATIO_CEIL
    # total_tokens / total_chars <= _RATIO_FLOOR, i.e. total_tokens * 10 <=
    # total_chars -- the "* 10" is only correct because _RATIO_FLOOR == 0.1
    # (same pinning test). <= matches the old clamp's boundary likewise.
    if total_tokens * 10 <= total_chars:
        return _RATIO_FLOOR
    # Neither branch fired, so the true ratio lies strictly inside
    # (_RATIO_FLOOR, _RATIO_CEIL): the two totals are within ~20x of each other,
    # so the float division is safe (no realistic window sum overflows at that
    # ratio). The min/max clamp is now a no-op given the two branches above;
    # kept as a free extra safety net rather than a bare division.
    return min(max(total_tokens / total_chars, _RATIO_FLOOR), _RATIO_CEIL)


def observe(chars: int, tokens: int) -> None:
    """Record one (chars_sent, prompt_tokens) observation for the ratio window.

    Called from the LLM tool loop after a completion reports usage: ``chars`` is
    the char size of the messages ACTUALLY sent for that attempt and ``tokens``
    is the endpoint's own ``prompt_tokens`` count, so the pair is exactly one
    ground-truth chars<->tokens sample.

    NEVER raises. The estimator is a pure OBSERVER of the LLM call (like the
    llm_log recorder): a bad usage report must degrade to "contribute nothing",
    never turn a successful completion into a crash. Non-positive values are
    ignored defensively (they are not usable observations and would corrupt the
    ratio); so is anything above _MAX_OBSERVED_TOKENS (an implausibly large
    report from a broken/hostile gateway -- see that constant for why letting it
    into the window is dangerous beyond just skewing the ratio), on either side
    of the pair. The whole body is wrapped so even an unexpected input the
    annotation forbids -- a ``None`` slipping through, making ``None <= 0`` raise
    a TypeError -- is swallowed rather than propagated into the LLM path.
    """
    try:
        if chars <= 0 or tokens <= 0:
            return
        if chars > _MAX_OBSERVED_TOKENS or tokens > _MAX_OBSERVED_TOKENS:
            return
        with _LOCK:
            _WINDOW.append((chars, tokens))
    except Exception:
        # Observer-adjacent: never propagate into the LLM path. Silence is
        # correct -- a dropped observation only makes the ratio very slightly
        # staler, never wrong.
        return


def tokens_per_char() -> float:
    """The current chars->tokens ratio: observed tokens per sent char.

    Below _MIN_SAMPLES observations returns the cold-start _DEFAULT_RATIO (1.0);
    otherwise the clamped aggregate ratio (see ``_ratio_locked``). Always a
    usable positive float, so ``char_allowance`` can divide by it unconditionally.
    """
    with _LOCK:
        ratio = _ratio_locked()
    return _DEFAULT_RATIO if ratio is None else ratio


def char_allowance(budget_tokens: int) -> int:
    """Convert a TOKEN budget into the CHAR allowance the char-based cutters use.

    ``int(budget_tokens / tokens_per_char())`` -- how many chars fit in
    ``budget_tokens`` at the current ratio -- floored at _ALLOWANCE_FLOOR so a
    budget can never collapse below a usable prompt. At the cold-start ratio 1.0
    this is just ``budget_tokens`` itself, so a fresh process behaves exactly as
    the old char budgets did.
    """
    allowance = int(budget_tokens / tokens_per_char())
    return max(allowance, _ALLOWANCE_FLOOR)


def snapshot() -> dict[str, Any]:
    """A small, JSON-ready view of the estimator for GET /api/llm/status.

    ``samples`` is the current window size; ``tokens_per_char`` is the live
    ratio rounded to 4dp, or None while below _MIN_SAMPLES (the cold-start
    default is in force and there is no OBSERVED ratio to report yet), so a
    reader can tell "still cold-starting" from "learned a ratio".
    """
    with _LOCK:
        samples = len(_WINDOW)
        ratio = _ratio_locked()
    return {"samples": samples, "tokens_per_char": None if ratio is None else round(ratio, 4)}


def _reset_for_tests() -> None:
    """Drop the observation window so a test starts from the cold-start default.

    Not used at runtime. Mirrors llm_log._reset_for_tests / tool_builder's own
    test resets: the window is process-wide state that would otherwise leak
    observations (and thus a shifted ratio -- most visibly the /llm/status
    token_ratio) across tests.
    """
    with _LOCK:
        _WINDOW.clear()
