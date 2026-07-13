"""Tests asserting the AI system prompts encode the methodology rules.

These pin the wording so a refactor cannot silently drop a rule the workflows
depend on (confidence honesty, max-3-questions, supersede-not-delete, bullets,
language-follows-content).
"""

from app.services.memory_ai import CAPTURE_SYSTEM_PROMPT


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
