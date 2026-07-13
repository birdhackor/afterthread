"""Tests asserting the AI system prompts encode the methodology rules.

These pin the wording so a refactor cannot silently drop a rule the workflows
depend on (confidence honesty, max-3-questions, supersede-not-delete, bullets,
language-follows-content).
"""

from app.services.memory_ai import (
    CAPTURE_SYSTEM_PROMPT,
    ENRICH_SYSTEM_PROMPT,
    UPDATE_SYSTEM_PROMPT,
)

# The honesty / bullets / language rules are shared across all three workflows.
_ALL_PROMPTS = (CAPTURE_SYSTEM_PROMPT, ENRICH_SYSTEM_PROMPT, UPDATE_SYSTEM_PROMPT)
# Supersede-not-delete applies only to the enrich/update workflows.
_HISTORY_PROMPTS = (ENRICH_SYSTEM_PROMPT, UPDATE_SYSTEM_PROMPT)


def test_capture_prompt_has_confidence_honesty_rule() -> None:
    prompt = CAPTURE_SYSTEM_PROMPT
    assert "Known" in prompt
    assert "Inferred" in prompt
    assert "Unknown" in prompt
    assert "never invent" in prompt.lower()


def test_capture_prompt_has_max_three_questions_rule() -> None:
    assert "at most 3" in CAPTURE_SYSTEM_PROMPT
    assert "follow-up questions" in CAPTURE_SYSTEM_PROMPT


def test_capture_prompt_prefers_bullets() -> None:
    assert "short factual bullets" in CAPTURE_SYSTEM_PROMPT


def test_capture_prompt_follows_content_language() -> None:
    assert "same language" in CAPTURE_SYSTEM_PROMPT.lower()


def test_all_prompts_have_confidence_honesty_rule() -> None:
    for prompt in _ALL_PROMPTS:
        assert "Known" in prompt
        assert "Inferred" in prompt
        assert "Unknown" in prompt
        assert "never invent" in prompt.lower()


def test_all_prompts_prefer_bullets() -> None:
    for prompt in _ALL_PROMPTS:
        assert "short factual bullets" in prompt


def test_all_prompts_follow_content_language() -> None:
    for prompt in _ALL_PROMPTS:
        assert "same language" in prompt.lower()


def test_enrich_and_update_prompts_have_supersede_rule() -> None:
    for prompt in _HISTORY_PROMPTS:
        assert "never delete" in prompt.lower()
        assert "superseded" in prompt.lower()


def test_capture_prompt_has_no_supersede_rule() -> None:
    # Supersede-not-delete is an enrich/update concern; quick capture creates a
    # new item, so it should not carry that rule.
    assert "superseded" not in CAPTURE_SYSTEM_PROMPT.lower()
