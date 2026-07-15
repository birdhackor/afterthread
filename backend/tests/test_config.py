"""Tests for Settings validation in context_memory.config.

`stale_after_days` feeds `timedelta(days=...)` inside
`schemas.MemoryItemRead.is_stale`. An unbounded value (e.g. a fat-fingered
`STALE_AFTER_DAYS=99999999999`) boots fine but then raises `OverflowError`
from that timedelta on every read/commit touching a stale-eligible item.
Bounding the field at construction makes a bad value fail loudly at startup,
via pydantic-settings, instead.
"""

import pytest
from pydantic import ValidationError

from context_memory.config import Settings


def test_stale_after_days_lower_bound_accepted() -> None:
    assert Settings(stale_after_days=0).stale_after_days == 0


def test_stale_after_days_upper_bound_accepted() -> None:
    assert Settings(stale_after_days=36500).stale_after_days == 36500


def test_stale_after_days_above_upper_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(stale_after_days=36501)


def test_stale_after_days_negative_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(stale_after_days=-1)


# `openai_timeout_seconds` is passed straight to AsyncOpenAI(timeout=...). Its
# Field(gt=0, le=600) bound makes a nonsensical override -- a zero/negative
# timeout the SDK would reject, or an hours-long one -- fail at startup via
# pydantic-settings, rather than only when the first AI request is made.


def test_openai_timeout_seconds_default_is_sixty() -> None:
    assert Settings().openai_timeout_seconds == 60


def test_openai_timeout_seconds_small_positive_accepted() -> None:
    assert Settings(openai_timeout_seconds=0.5).openai_timeout_seconds == 0.5


def test_openai_timeout_seconds_upper_bound_accepted() -> None:
    assert Settings(openai_timeout_seconds=600).openai_timeout_seconds == 600


def test_openai_timeout_seconds_above_upper_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(openai_timeout_seconds=601)


def test_openai_timeout_seconds_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(openai_timeout_seconds=0)


def test_openai_timeout_seconds_negative_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(openai_timeout_seconds=-1)


# `llm_prompt_budget_chars` caps the serialized item snapshot in an enrich /
# assist-update prompt (see context_memory.services.memory_ai). Its Field(ge=4000,
# le=200000) bound makes a nonsensical override fail at startup via
# pydantic-settings, matching the openai_timeout_seconds / stale_after_days
# convention, rather than only when the first AI prompt is built.


def test_llm_prompt_budget_chars_default_is_32000() -> None:
    assert Settings().llm_prompt_budget_chars == 32000


def test_llm_prompt_budget_chars_lower_bound_accepted() -> None:
    assert Settings(llm_prompt_budget_chars=4000).llm_prompt_budget_chars == 4000


def test_llm_prompt_budget_chars_upper_bound_accepted() -> None:
    assert Settings(llm_prompt_budget_chars=200000).llm_prompt_budget_chars == 200000


def test_llm_prompt_budget_chars_below_lower_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_prompt_budget_chars=3999)


def test_llm_prompt_budget_chars_above_upper_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_prompt_budget_chars=200001)
