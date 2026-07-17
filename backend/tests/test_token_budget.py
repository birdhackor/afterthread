"""Unit tests for the char<->token ratio estimator
(context_memory.services.token_budget).

The estimator keeps a bounded rolling window of (chars_sent, prompt_tokens)
observations and derives a live tokens-per-char ratio used to convert the two
TOKEN prompt budgets into CHAR allowances. These pin the cold-start default
below _MIN_SAMPLES, the aggregate window math, clamping in both directions, the
defensive handling of non-positive / malformed observations (observe NEVER
raises), the char_allowance floor, the snapshot shape, and the reset helper. The
window is process-wide state reset around every test by the conftest autouse
fixture, so each test here starts from the cold-start default.
"""

from typing import Any

import pytest

from context_memory.services import token_budget


def test_cold_start_ratio_is_default_below_min_samples() -> None:
    # No observations -> the CJK-worst-case cold-start default (1.0), so a token
    # budget maps char-for-char to the old char budget on day one.
    assert token_budget.tokens_per_char() == 1.0
    # One and two observations are still too few to trust: the default holds.
    token_budget.observe(100, 20)
    assert token_budget.tokens_per_char() == 1.0
    token_budget.observe(100, 20)
    assert token_budget.tokens_per_char() == 1.0


def test_ratio_is_aggregate_over_the_window() -> None:
    # At >= _MIN_SAMPLES: Sum(tokens) / Sum(chars), NOT a mean of per-call ratios,
    # so a large sample weighs proportionally more than a tiny one.
    token_budget.observe(100, 20)  # 0.2 tok/char
    token_budget.observe(100, 20)  # 0.2 tok/char
    token_budget.observe(800, 400)  # 0.5 tok/char, but 8x the chars
    # aggregate = (20 + 20 + 400) / (100 + 100 + 800) = 440 / 1000 = 0.44
    assert token_budget.tokens_per_char() == pytest.approx(0.44)


def test_ratio_clamped_to_ceiling() -> None:
    # An absurd usage report (tokens >> chars) must not shrink a budget to
    # nothing: the ratio is capped at _RATIO_CEIL (2.0).
    for _ in range(3):
        token_budget.observe(10, 1000)  # 100 tok/char
    assert token_budget.tokens_per_char() == 2.0


def test_ratio_clamped_to_floor() -> None:
    # Token-light content (ASCII-heavy) must not inflate a token budget into an
    # unbounded char allowance: the ratio is floored at _RATIO_FLOOR (0.1), the
    # load-bearing absolute char backstop.
    for _ in range(3):
        token_budget.observe(1000, 1)  # 0.001 tok/char
    assert token_budget.tokens_per_char() == 0.1


def test_non_positive_observations_are_ignored() -> None:
    token_budget.observe(0, 100)
    token_budget.observe(100, 0)
    token_budget.observe(-5, 100)
    token_budget.observe(100, -5)
    assert list(token_budget._WINDOW) == []
    assert token_budget.tokens_per_char() == 1.0


def test_observe_never_raises_on_bad_input() -> None:
    # Observer-adjacent contract: a malformed report (a None slipping past the
    # int annotation -- typed Any here so the call models a misbehaving caller)
    # degrades to "contribute nothing", never raises into the LLM path.
    bad: Any = None
    token_budget.observe(bad, 100)
    token_budget.observe(100, bad)
    assert list(token_budget._WINDOW) == []


def test_window_is_bounded() -> None:
    # The window is maxlen-bounded, so old observations age out and the ratio
    # tracks the CURRENT content mix rather than the whole process history.
    for _ in range(token_budget._WINDOW_MAXLEN + 10):
        token_budget.observe(100, 50)
    assert len(token_budget._WINDOW) == token_budget._WINDOW_MAXLEN


def test_char_allowance_at_cold_start_equals_budget() -> None:
    # ratio 1.0 -> allowance == the token budget counted in chars (day-one parity
    # with the old char budgets).
    assert token_budget.char_allowance(200_000) == 200_000


def test_char_allowance_scales_with_ratio() -> None:
    for _ in range(3):
        token_budget.observe(100, 50)  # ratio 0.5
    # 8000 tokens / 0.5 = 16000 chars.
    assert token_budget.char_allowance(8000) == 16000


def test_char_allowance_has_a_floor() -> None:
    # Even a tiny budget (or a ceiling-clamped ratio) never collapses below a
    # usable prompt.
    for _ in range(3):
        token_budget.observe(10, 1000)  # ratio clamps to 2.0
    # 100 tokens / 2.0 = 50 chars -> floored to _ALLOWANCE_FLOOR (1000).
    assert token_budget.char_allowance(100) == token_budget._ALLOWANCE_FLOOR


def test_snapshot_shape_cold_start() -> None:
    assert token_budget.snapshot() == {"samples": 0, "tokens_per_char": None}
    token_budget.observe(100, 50)
    # Below _MIN_SAMPLES: samples counts up but the ratio is still null (the
    # cold-start default is in force, there is no OBSERVED ratio to report yet).
    assert token_budget.snapshot() == {"samples": 1, "tokens_per_char": None}


def test_snapshot_reports_rounded_ratio() -> None:
    for _ in range(3):
        token_budget.observe(300, 100)
    # 300 / 900 = 0.3333... -> rounded to 4dp.
    snap = token_budget.snapshot()
    assert snap["samples"] == 3
    assert snap["tokens_per_char"] == 0.3333


def test_reset_clears_the_window() -> None:
    for _ in range(3):
        token_budget.observe(100, 50)
    assert token_budget.snapshot()["samples"] == 3
    token_budget._reset_for_tests()
    assert token_budget.snapshot() == {"samples": 0, "tokens_per_char": None}
