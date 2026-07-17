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


# `openai_timeout_seconds` is passed straight to AsyncOpenAI(timeout=...) AND is
# the end-to-end wall-clock budget for a call plus its one corrective retry. Its
# Field(gt=0, le=1800) bound makes a nonsensical override -- a zero/negative
# timeout the SDK would reject, or an hours-long one -- fail at startup via
# pydantic-settings, rather than only when the first AI request is made. The
# default (120s) and ceiling (1800s) were raised from 60/600 for GLM5.2, a
# long-context model that legitimately generates for longer than a small-context
# model did (see D10 in docs/web-v2-decisions.md).


def test_openai_timeout_seconds_default_is_one_twenty() -> None:
    assert Settings().openai_timeout_seconds == 120


def test_openai_timeout_seconds_small_positive_accepted() -> None:
    assert Settings(openai_timeout_seconds=0.5).openai_timeout_seconds == 0.5


def test_openai_timeout_seconds_upper_bound_accepted() -> None:
    assert Settings(openai_timeout_seconds=1800).openai_timeout_seconds == 1800


def test_openai_timeout_seconds_above_upper_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(openai_timeout_seconds=1801)


def test_openai_timeout_seconds_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(openai_timeout_seconds=0)


def test_openai_timeout_seconds_negative_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(openai_timeout_seconds=-1)


# `openai_max_output_tokens` is sent as `max_tokens` on the completion call ONLY
# when set; None (the default) omits the parameter entirely (some reasoning
# endpoints reject it). Its Field(gt=0, le=1_000_000) bound makes a zero/negative
# override fail at startup rather than on the first AI call.


def test_openai_max_output_tokens_default_is_none() -> None:
    assert Settings().openai_max_output_tokens is None


def test_openai_max_output_tokens_positive_accepted() -> None:
    assert Settings(openai_max_output_tokens=4096).openai_max_output_tokens == 4096


def test_openai_max_output_tokens_upper_bound_accepted() -> None:
    assert Settings(openai_max_output_tokens=1_000_000).openai_max_output_tokens == 1_000_000


def test_openai_max_output_tokens_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(openai_max_output_tokens=0)


def test_openai_max_output_tokens_above_upper_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(openai_max_output_tokens=1_000_001)


# `llm_prompt_budget_chars` caps the serialized item snapshot in an enrich /
# assist-update prompt (see context_memory.services.memory_ai). Its Field(ge=4000,
# le=2_000_000) bound makes a nonsensical override fail at startup via
# pydantic-settings, matching the openai_timeout_seconds / stale_after_days
# convention, rather than only when the first AI prompt is built. The default
# (200000) and ceiling (2_000_000) are sized for a 1M-token-context model like
# GLM5.2 (see D10 in docs/web-v2-decisions.md).


def test_llm_prompt_budget_chars_default_is_200000() -> None:
    assert Settings().llm_prompt_budget_chars == 200000


def test_llm_prompt_budget_chars_lower_bound_accepted() -> None:
    assert Settings(llm_prompt_budget_chars=4000).llm_prompt_budget_chars == 4000


def test_llm_prompt_budget_chars_upper_bound_accepted() -> None:
    assert Settings(llm_prompt_budget_chars=2_000_000).llm_prompt_budget_chars == 2_000_000


def test_llm_prompt_budget_chars_below_lower_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_prompt_budget_chars=3999)


def test_llm_prompt_budget_chars_above_upper_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_prompt_budget_chars=2_000_001)


# `llm_log_max_entries` sizes the in-memory LLM interaction ring (see
# context_memory.services.llm_log). Bounded to [1, 1000] so the ring's worst-case
# RAM (records carrying full prompt/response bodies) stays bounded.


def test_llm_log_max_entries_default_is_fifty() -> None:
    assert Settings().llm_log_max_entries == 50


def test_llm_log_max_entries_lower_bound_accepted() -> None:
    assert Settings(llm_log_max_entries=1).llm_log_max_entries == 1


def test_llm_log_max_entries_upper_bound_accepted() -> None:
    assert Settings(llm_log_max_entries=1000).llm_log_max_entries == 1000


def test_llm_log_max_entries_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_log_max_entries=0)


def test_llm_log_max_entries_above_upper_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_log_max_entries=1001)


# `llm_log_file` is the optional JSONL sink path; empty (the default) disables
# it, since a record carries personal memory content and landing it on disk is
# an opt-in privacy decision.


def test_llm_log_file_default_is_empty() -> None:
    assert Settings().llm_log_file == ""


# `llm_log_file_max_bytes` is the rotation threshold for the JSONL sink (D34):
# once the file grows past it, the sink renames it aside with a UTC-timestamp
# suffix and starts fresh. Its Field(ge=1_000_000, le=1_000_000_000) bound makes
# a nonsensical override fail at startup via pydantic-settings, matching the
# other llm_log knobs' convention. Default 50MB.


def test_llm_log_file_max_bytes_default_is_fifty_million() -> None:
    assert Settings().llm_log_file_max_bytes == 50_000_000


def test_llm_log_file_max_bytes_lower_bound_accepted() -> None:
    assert Settings(llm_log_file_max_bytes=1_000_000).llm_log_file_max_bytes == 1_000_000


def test_llm_log_file_max_bytes_upper_bound_accepted() -> None:
    assert Settings(llm_log_file_max_bytes=1_000_000_000).llm_log_file_max_bytes == 1_000_000_000


def test_llm_log_file_max_bytes_below_lower_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_log_file_max_bytes=999_999)


def test_llm_log_file_max_bytes_above_upper_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_log_file_max_bytes=1_000_000_001)


# `llm_tool_conversation_budget_chars` caps the SERIALIZED size of the live
# tool-loop conversation actually SENT to the model each round (see
# context_memory.services.llm) -- the SENT-side companion to the recorded-side
# llm_log body budget. Its Field(ge=50_000, le=8_000_000) bound makes a nonsensical
# override fail at startup via pydantic-settings, matching the other LLM knobs'
# convention, rather than only when the tool loop first overruns.


def test_llm_tool_conversation_budget_chars_default_is_one_million() -> None:
    assert Settings().llm_tool_conversation_budget_chars == 1_000_000


def test_llm_tool_conversation_budget_chars_lower_bound_accepted() -> None:
    settings = Settings(llm_tool_conversation_budget_chars=50_000)
    assert settings.llm_tool_conversation_budget_chars == 50_000


def test_llm_tool_conversation_budget_chars_upper_bound_accepted() -> None:
    settings = Settings(llm_tool_conversation_budget_chars=8_000_000)
    assert settings.llm_tool_conversation_budget_chars == 8_000_000


def test_llm_tool_conversation_budget_chars_below_lower_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_tool_conversation_budget_chars=49_999)


def test_llm_tool_conversation_budget_chars_above_upper_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_tool_conversation_budget_chars=8_000_001)
