"""AI memory workflows: prompts, untrusted-output sanitizers, and validation.

Each workflow (``capture_draft``, and in a later commit ``enrich_item`` /
``assist_update``) builds a system prompt that encodes the context-memory
methodology, calls ``generate_json``, and validates the result through a
pydantic model whose *sanitizers treat the LLM output as untrusted input*:
types are coerced defensively, unknown keys dropped, and every field is capped
(per-section length, tag/question counts, title length) so nothing oversized
or unexpected can reach the database. The system prompts are module-level
constants so their methodology rules can be asserted directly in tests.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.models import MemoryStatus
from app.services.llm import LLMUpstreamError, generate_json

# --- caps (constraint: bound everything; LLM output is untrusted) ----------

# Per free-text section, matching the request-side input bound.
_PER_SECTION_CAP = 20000
_TITLE_MAX = 300
_MAX_TAGS = 10
_TAG_MAX = 50
# The methodology's hard ceiling on quick-capture follow-up questions.
_MAX_QUESTIONS = 3


# --- defensive coercion helpers -------------------------------------------


def _coerce_str(value: object) -> str:
    """Coerce arbitrary JSON-decoded input to a string, never raising.

    LLM output is untrusted: a field the prompt asked to be a string may come
    back as a number, bool, or list. Map each to a reasonable string rather
    than letting pydantic reject the whole draft over one stray type.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "\n".join(_coerce_str(item) for item in value if item is not None)
    return str(value)


def _clean_text(value: object, cap: int = _PER_SECTION_CAP) -> str:
    """Coerce to string, strip surrounding whitespace, and hard-truncate."""
    return _coerce_str(value).strip()[:cap]


def _clean_str_list(value: object, *, max_items: int, item_cap: int) -> list[str]:
    """Coerce to a list of non-empty, per-item-capped strings, count-bounded.

    A scalar is treated as a single-element list. Empty/whitespace items are
    dropped; the result is capped at ``max_items`` (e.g. tags at 10, questions
    at 3) -- the server-side hard cap, independent of what the model returns.
    """
    if value is None:
        return []
    raw_items: list[object] = list(value) if isinstance(value, (list, tuple)) else [value]
    cleaned: list[str] = []
    for item in raw_items:
        text = _coerce_str(item).strip()[:item_cap]
        if text:
            cleaned.append(text)
        if len(cleaned) >= max_items:
            break
    return cleaned


# Quick capture may only ever suggest one of these two statuses; anything else
# the model returns (active/done/garbage/empty) collapses to capture-quick.
_CAPTURE_STATUSES: tuple[MemoryStatus, ...] = (
    MemoryStatus.capture_quick,
    MemoryStatus.needs_enrichment,
)


def _clean_capture_status(value: object) -> MemoryStatus:
    text = _coerce_str(value).strip()
    for status in _CAPTURE_STATUSES:
        if text == status.value:
            return status
    return MemoryStatus.capture_quick


def _validate[ModelT: BaseModel](model: type[ModelT], raw: dict[str, Any]) -> ModelT:
    """Validate raw LLM JSON through ``model``, mapping failure to 502.

    Only the exception *category* is surfaced (never ``str(exc)``, which can
    echo the offending output) so the message stays safe and config-free.
    """
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise LLMUpstreamError(
            f"{type(exc).__name__}: the LLM output failed schema validation"
        ) from exc


# --- methodology rule fragments (asserted verbatim in prompt tests) --------

_RULE_HONESTY = (
    "Separate your confidence honestly. Mark each point as Known (directly "
    "stated or observed), Inferred (a plausible interpretation, which you MUST "
    "label as inferred), or Unknown (important but missing). Never invent "
    "facts to fill an Unknown gap."
)
_RULE_MAX_QUESTIONS = (
    "Ask at most 3 follow-up questions, and only for details that are likely to evaporate soon."
)
_RULE_BULLETS = "Prefer short factual bullets over polished prose."
_RULE_LANGUAGE = (
    "Write your output in the same language as the user content (for example, "
    "Traditional Chinese input yields Traditional Chinese output). Keep the "
    "JSON keys in English."
)


# --- quick capture ---------------------------------------------------------


CAPTURE_SYSTEM_PROMPT = "\n".join(
    [
        "You are the Context Memory quick-capture assistant. Turn a raw "
        "discussion into a structured draft so that future readers, or an AI "
        "agent, can resume the topic without replaying the conversation. "
        "Capture with minimum friction; the first draft may be imperfect.",
        _RULE_HONESTY,
        _RULE_MAX_QUESTIONS,
        _RULE_BULLETS,
        _RULE_LANGUAGE,
        "Respond with a single JSON object and nothing else. Use exactly these keys:",
        '- "title": a concise title (string).',
        '- "snapshot": a one-paragraph snapshot of the topic (string).',
        '- "why_matters": why this matters (string).',
        '- "known": bullet points directly stated or observed (string).',
        '- "inferred": bullet points you inferred, each labeled as inferred (string).',
        '- "unknown": important but missing details (string).',
        '- "next_actions": the next action or resume trigger (string).',
        '- "recovery_keywords": keywords future-you would search for (string).',
        '- "recovery_people": people who know about this (string).',
        '- "recovery_files": files, systems, or links involved (string).',
        '- "resume_trigger": what should make someone pick this up again (string).',
        '- "tags": up to 10 short tags (array of strings).',
        '- "suggested_status": either "capture-quick" or "needs-enrichment" (string).',
        '- "questions": at most 3 follow-up questions (array of strings).',
    ]
)


class CaptureDraft(BaseModel):
    """Sanitized quick-capture draft mapped onto a new MemoryItem.

    The ``mode="before"`` validator is the sanitizer: it rebuilds the payload
    from scratch with defensive coercion and caps, so only whitelisted,
    bounded, correctly-typed fields survive. Unknown keys are dropped;
    ``suggested_status`` is constrained to the two capture statuses; a missing
    or blank title fails (mapped to 502 upstream) rather than creating a
    title-less item.
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    snapshot: str = ""
    why_matters: str = ""
    known: str = ""
    inferred: str = ""
    unknown: str = ""
    next_actions: str = ""
    recovery_keywords: str = ""
    recovery_people: str = ""
    recovery_files: str = ""
    resume_trigger: str = ""
    tags: list[str] = Field(default_factory=list)
    suggested_status: MemoryStatus = MemoryStatus.capture_quick
    questions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _sanitize(cls, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        title = _clean_text(data.get("title"), cap=_TITLE_MAX)
        if not title:
            raise ValueError("title must not be empty")
        return {
            "title": title,
            "snapshot": _clean_text(data.get("snapshot")),
            "why_matters": _clean_text(data.get("why_matters")),
            "known": _clean_text(data.get("known")),
            "inferred": _clean_text(data.get("inferred")),
            "unknown": _clean_text(data.get("unknown")),
            "next_actions": _clean_text(data.get("next_actions")),
            "recovery_keywords": _clean_text(data.get("recovery_keywords")),
            "recovery_people": _clean_text(data.get("recovery_people")),
            "recovery_files": _clean_text(data.get("recovery_files")),
            "resume_trigger": _clean_text(data.get("resume_trigger")),
            "tags": _clean_str_list(data.get("tags"), max_items=_MAX_TAGS, item_cap=_TAG_MAX),
            "suggested_status": _clean_capture_status(data.get("suggested_status")),
            "questions": _clean_str_list(
                data.get("questions"), max_items=_MAX_QUESTIONS, item_cap=_PER_SECTION_CAP
            ),
        }


def _capture_user_prompt(raw_text: str) -> str:
    return "Raw discussion to capture into a memory item:\n\n" + raw_text


async def capture_draft(raw_text: str) -> CaptureDraft:
    """Run quick capture: prompt the LLM and validate the sanitized draft."""
    raw = await generate_json(CAPTURE_SYSTEM_PROMPT, _capture_user_prompt(raw_text))
    return _validate(CaptureDraft, raw)
