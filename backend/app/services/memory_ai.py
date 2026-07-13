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

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.models import MemoryStatus, utcnow
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


# Max nesting depth _coerce_str will descend into a list/tuple before refusing.
# LLM output is untrusted and can be pathologically nested; a list ~1000 deep
# blows Python's recursion limit, and an unbounded _coerce_str would then raise
# RecursionError from inside the pydantic before-validators below -- which
# pydantic does NOT convert to a ValidationError, so it would escape as an
# unhandled 500. Bounding the descent to a small constant makes such input raise
# ValueError instead, which pydantic DOES fold into a ValidationError (mapped to
# 502). Legitimate capture/enrich fields nest at most one level (a flat list of
# strings), so this ceiling is never reached by real output.
_MAX_COERCE_DEPTH = 8


def _coerce_str(value: object, _depth: int = 0) -> str:
    """Coerce arbitrary JSON-decoded input to a string.

    LLM output is untrusted: a field the prompt asked to be a string may come
    back as a number, bool, or list. Map each to a reasonable string rather
    than letting pydantic reject the whole draft over one stray type.

    Raises ValueError on ONE input only: a list/tuple nested beyond
    ``_MAX_COERCE_DEPTH`` (see that constant), so a pathologically nested payload
    degrades to a 502 rather than a RecursionError-driven 500. ``_depth`` is
    internal recursion bookkeeping; callers never pass it.
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
        if _depth >= _MAX_COERCE_DEPTH:
            raise ValueError("nested list/tuple exceeds the maximum coercion depth")
        return "\n".join(_coerce_str(item, _depth + 1) for item in value if item is not None)
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
    ``from None`` (not ``from exc``) severs the cause chain as well: a pydantic
    ``ValidationError`` embeds the offending input -- raw LLM output / memory
    content -- in its ``str`` and ``.errors()``, which would otherwise ride
    along in ``__cause__`` into any traceback-logging sink. The safe category
    prefix keeps diagnosis possible without that leak.
    """
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise LLMUpstreamError(
            f"{type(exc).__name__}: the LLM output failed schema validation"
        ) from None


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


# --- section whitelist (untrusted enrich/update output) --------------------

# The anti-vaporization section fields an enrich/update may write, in checklist
# order. This is the whitelist: any other key the model returns (id, status,
# source, tags, or a hallucinated field) is dropped before anything is written,
# so a sanitized section dict can be setattr'd straight onto the item. A test
# pins that every entry is a real MemoryItem field.
SECTION_FIELD_ORDER: tuple[str, ...] = (
    "snapshot",
    "why_matters",
    "known",
    "inferred",
    "unknown",
    "decisions",
    "alternatives",
    "rationale",
    "consequences",
    "constraints",
    "assumptions",
    "risks",
    "evidence",
    "open_questions",
    "next_actions",
    "recovery_keywords",
    "recovery_people",
    "recovery_files",
    "resume_trigger",
)
SECTION_FIELDS = frozenset(SECTION_FIELD_ORDER)

# The history-bearing sections. Overwriting any of these would erase the
# reasoning trail the methodology's supersede-not-delete rule exists to
# protect, so the server MERGES rather than replaces them (see
# `merge_with_supersede`); every other section is a current-state field an
# enrich/update may legitimately replace wholesale. A subset of SECTION_FIELDS.
HISTORY_SECTIONS: frozenset[str] = frozenset(
    {"decisions", "rationale", "alternatives", "consequences"}
)


def _normalize_ws(text: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends.

    Used only to compare two section values for containment, so a model that
    reflows whitespace while keeping the words still reads as having preserved
    the old text (and is not needlessly superseded).
    """
    return " ".join(text.split())


def merge_with_supersede(old: str, new: str) -> str:
    """Losslessly fold an existing history-section value into its replacement.

    Enforces supersede-not-delete on the SERVER (a prompt rule alone cannot
    guarantee it): when the model's ``new`` value already contains the existing
    ``old`` text (compared whitespace-normalized) it is kept as-is; otherwise
    ``old`` is appended below ``new`` behind a dated 'superseded' marker so no
    prior decision or rationale is ever silently dropped. An empty/whitespace
    ``old`` (nothing to preserve) yields ``new`` unchanged, and vice versa.

    Pure apart from the UTC date it stamps into the marker; it reads only its
    two string arguments and never touches configuration.
    """
    old = old or ""
    new = new or ""
    if not old.strip():
        return new
    if not new.strip():
        return old
    if _normalize_ws(old) in _normalize_ws(new):
        return new
    marker = f"\n\n--- (superseded {utcnow().date().isoformat()}) ---\n"
    return new + marker + old


# Defensive count cap on the returned checklist gaps.
_MAX_GAPS = 20


def _coerce_bool(value: object) -> bool:
    """Coerce untrusted JSON to a bool (true/yes/1/complete/done -> True)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "complete", "done", "y"}
    return False


def _clean_sections(value: object) -> dict[str, str]:
    """Keep only whitelisted, non-empty, capped section fields from a mapping.

    Non-whitelisted keys are dropped (so the model cannot set id/status/etc via
    a section), and empty values are dropped too -- an enrich/update must never
    blank an existing section, only add to it or replace it with real content.
    """
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or key not in SECTION_FIELDS:
            continue
        text = _clean_text(raw)
        if text:
            cleaned[key] = text
    return cleaned


def _extract_sections(data: dict[str, Any]) -> dict[str, str]:
    """Pull sanitized sections from a "sections" object, tolerating top-level.

    The prompt asks for a nested ``sections`` object; if the model instead
    places section fields at the top level, recover them there rather than
    silently returning nothing.
    """
    sections = _clean_sections(data.get("sections"))
    if not sections:
        sections = _clean_sections({k: v for k, v in data.items() if k in SECTION_FIELDS})
    return sections


def _render_item_fields(item_fields: Mapping[str, Any]) -> str:
    """Render the current item (title + every section) for the user prompt."""
    lines = [f"title: {_coerce_str(item_fields.get('title')).strip() or '(empty)'}"]
    for field in SECTION_FIELD_ORDER:
        value = _coerce_str(item_fields.get(field)).strip()
        lines.append(f"{field}: {value or '(empty)'}")
    return "\n".join(lines)


_RULE_SUPERSEDE = (
    "Preserve history: never delete an existing decision or its rationale. "
    "When a decision changes, keep the previous rationale and append a "
    "'superseded' marker noting what replaced it and why."
)


# --- full enrichment -------------------------------------------------------


ENRICH_SYSTEM_PROMPT = "\n".join(
    [
        "You are the Context Memory enrichment assistant. Given an existing "
        "memory item and new context, fill the anti-vaporization checklist "
        "(background, stakeholders, current state, desired outcome, decisions, "
        "rationale, alternatives, constraints, assumptions, risks, evidence, "
        "next actions, recovery cues) so a future reader can recover the full "
        "reasoning.",
        _RULE_HONESTY,
        _RULE_SUPERSEDE,
        _RULE_BULLETS,
        _RULE_LANGUAGE,
        "Only include a section when you are adding to or improving it. Omit "
        "sections you would leave unchanged, and never blank an existing section.",
        "Respond with a single JSON object and nothing else. Use exactly these keys:",
        '- "sections": an object whose keys are any of ['
        + ", ".join(SECTION_FIELD_ORDER)
        + "], each value a string.",
        '- "checklist_complete": true only when the item is materially complete '
        "(boolean). Marking it complete promotes a still-capturing item to the "
        "active status.",
        '- "remaining_gaps": checklist items still missing (array of strings).',
        '- "progress_note": a short note describing what you added (string).',
    ]
)


class EnrichResult(BaseModel):
    """Sanitized enrichment result: whitelisted section updates + metadata.

    ``sections`` carries only the fields the model chose to add or improve, so
    the router merges them onto the item (untouched fields stay as they were).
    """

    model_config = ConfigDict(extra="ignore")

    sections: dict[str, str] = Field(default_factory=dict)
    checklist_complete: bool = False
    remaining_gaps: list[str] = Field(default_factory=list)
    progress_note: str = ""

    @model_validator(mode="before")
    @classmethod
    def _sanitize(cls, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return {
            "sections": _extract_sections(data),
            "checklist_complete": _coerce_bool(data.get("checklist_complete")),
            "remaining_gaps": _clean_str_list(
                data.get("remaining_gaps"), max_items=_MAX_GAPS, item_cap=_PER_SECTION_CAP
            ),
            "progress_note": _clean_text(data.get("progress_note")),
        }

    @model_validator(mode="after")
    def _require_signal(self) -> Self:
        """Reject an all-empty result as upstream garbage.

        After sanitization an enrichment must carry at least one meaningful
        signal -- a section, a gap, a completion flag, or a progress note --
        otherwise the model returned nothing usable. Raising here yields a
        ValidationError, which ``_validate`` maps to the 502 upstream-error path
        so the router writes nothing (no progress entry, no bumped `updated`).
        """
        if not (
            self.sections or self.remaining_gaps or self.checklist_complete or self.progress_note
        ):
            raise ValueError("enrichment result carries no usable signal")
        return self


def _enrich_user_prompt(item_fields: Mapping[str, Any], additional_context: str) -> str:
    return "\n\n".join(
        [
            "Existing memory item:",
            _render_item_fields(item_fields),
            "New context to integrate:",
            additional_context,
        ]
    )


async def enrich_item(item_fields: Mapping[str, Any], additional_context: str) -> EnrichResult:
    """Run full enrichment: prompt the LLM and validate the sanitized result."""
    raw = await generate_json(
        ENRICH_SYSTEM_PROMPT, _enrich_user_prompt(item_fields, additional_context)
    )
    return _validate(EnrichResult, raw)


# --- assisted update -------------------------------------------------------


UPDATE_SYSTEM_PROMPT = "\n".join(
    [
        "You are the Context Memory update assistant. Given an existing memory "
        "item and a progress note, refresh the current state, next actions, and "
        "open questions, and record what changed.",
        _RULE_HONESTY,
        _RULE_SUPERSEDE,
        _RULE_BULLETS,
        _RULE_LANGUAGE,
        "Only include a section when you are changing it. Omit unchanged "
        "sections, and never blank an existing section.",
        "Respond with a single JSON object and nothing else. Use exactly these keys:",
        '- "sections": an object whose keys are any of ['
        + ", ".join(SECTION_FIELD_ORDER)
        + "], each value a string.",
        '- "progress_note": a short note describing the update (string).',
    ]
)


class UpdateResult(BaseModel):
    """Sanitized assisted-update result: whitelisted section updates + note."""

    model_config = ConfigDict(extra="ignore")

    sections: dict[str, str] = Field(default_factory=dict)
    progress_note: str = ""

    @model_validator(mode="before")
    @classmethod
    def _sanitize(cls, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return {
            "sections": _extract_sections(data),
            "progress_note": _clean_text(data.get("progress_note")),
        }

    @model_validator(mode="after")
    def _require_signal(self) -> Self:
        """Reject an all-empty result as upstream garbage.

        An assisted update must carry at least one non-empty section or a
        progress note; otherwise the model returned nothing usable. As with
        EnrichResult, the raised ValidationError maps to the 502 path and the
        router writes nothing.
        """
        if not (self.sections or self.progress_note):
            raise ValueError("update result carries no usable signal")
        return self


def _update_user_prompt(item_fields: Mapping[str, Any], note: str) -> str:
    return "\n\n".join(
        [
            "Existing memory item:",
            _render_item_fields(item_fields),
            "Progress note to record:",
            note,
        ]
    )


async def assist_update(item_fields: Mapping[str, Any], note: str) -> UpdateResult:
    """Run assisted update: prompt the LLM and validate the sanitized result."""
    raw = await generate_json(UPDATE_SYSTEM_PROMPT, _update_user_prompt(item_fields, note))
    return _validate(UpdateResult, raw)
