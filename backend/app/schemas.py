"""Pydantic request/response schemas for the Context Memory API."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
)

from app.config import get_settings
from app.models import STALE_ELIGIBLE_STATUSES, MemoryStage, MemoryStatus


def _ensure_aware(value: datetime) -> datetime:
    """Treat naive datetimes (as returned by SQLite) as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# Upper bounds for the durable CRUD fields (title/tags/section text) a client
# can set directly via POST/PATCH. Without them, a legitimate create/update
# can write an item whose title/tags/sections are large enough that, once
# serialized into an enrich/assist-update prompt header
# (app.services.memory_ai._serialize_item_for_prompt, which renders title and
# tags in full ahead of the per-section budgeting), the header alone crowds
# out or exceeds llm_prompt_budget_chars. Declared here -- not on the shared
# MemoryItemContent base -- so only MemoryItemCreate/Update enforce them;
# MemoryItemRead stays unbounded so it can still honestly represent a
# pre-existing row written before these bounds existed (that row is protected
# instead by the defensive per-field cap in _serialize_item_for_prompt).
# title/section caps mirror memory_ai's own _TITLE_MAX / _PER_SECTION_CAP -- a
# human should not be able to create content the AI pipeline could never
# itself produce. Tags are looser here (20 x 100) than the AI sanitizer's
# self-imposed cap (10 x 50): a person may reasonably curate more/longer tags
# than the model is allowed to invent, and _serialize_item_for_prompt caps the
# rendered tags line independently, so this looser bound still cannot blow
# the prompt budget.
_CRUD_TITLE_MAX = 300
_CRUD_MAX_TAGS = 20
_CRUD_TAG_MAX = 100
_CRUD_SECTION_MAX = 20000

# A single CRUD tag: stripped, then bounded to 1..100 chars post-strip, so a
# blank or whitespace-only tag is rejected (422) rather than silently stored.
_CrudTag = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=_CRUD_TAG_MAX)
]


class MemoryItemContent(BaseModel):
    """Durable content fields shared by create and read schemas."""

    title: str
    source: str = "manual"
    confidence: str = "mixed"
    tags: list[str] = Field(default_factory=list)
    status: MemoryStatus = MemoryStatus.capture_quick
    stage: MemoryStage = MemoryStage.quick

    snapshot: str = ""
    why_matters: str = ""
    known: str = ""
    inferred: str = ""
    unknown: str = ""
    decisions: str = ""
    alternatives: str = ""
    rationale: str = ""
    consequences: str = ""
    constraints: str = ""
    assumptions: str = ""
    risks: str = ""
    evidence: str = ""
    open_questions: str = ""
    next_actions: str = ""
    recovery_keywords: str = ""
    recovery_people: str = ""
    recovery_files: str = ""
    resume_trigger: str = ""


class MemoryItemCreate(MemoryItemContent):
    """Payload for creating a memory item. Only ``title`` is mandatory.

    title/tags/every section text field carry explicit upper bounds (see the
    ``_CRUD_*`` constants above) that MemoryItemContent itself does not, so
    MemoryItemRead is unaffected -- this is the source-side half of bounding
    the enrich/assist-update prompt header; the other half is the defensive
    per-field cap in ``memory_ai._serialize_item_for_prompt``.
    """

    title: str = Field(max_length=_CRUD_TITLE_MAX)
    tags: list[_CrudTag] = Field(default_factory=list, max_length=_CRUD_MAX_TAGS)

    snapshot: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    why_matters: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    known: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    inferred: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    unknown: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    decisions: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    alternatives: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    rationale: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    consequences: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    constraints: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    assumptions: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    risks: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    evidence: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    open_questions: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    next_actions: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    recovery_keywords: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    recovery_people: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    recovery_files: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    resume_trigger: str = Field(default="", max_length=_CRUD_SECTION_MAX)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be empty")
        return stripped


class MemoryItemUpdate(BaseModel):
    """Partial update payload; every field is optional via omission.

    Every field is typed as its ordinary, non-nullable type with a
    `default_factory` that is never actually applied: the router
    (`update_item`) always calls `model_dump(exclude_unset=True)`, so an
    omitted field's default is never read or written, and pydantic v2 does
    not validate defaults unless `validate_default` is set (it isn't here).
    Using `default_factory` rather than a literal default is deliberate:
    pydantic v2 omits `default_factory`-sourced values from the generated
    JSON schema entirely, so every property below is optional but carries
    neither a `default` key nor `null` in its type. A literal default (e.g.
    `title: str = ""`) would instead surface as `"default": ""` in the
    OpenAPI schema; a client that honestly materialises schema defaults
    would then send `title=""` on every PATCH, tripping the not-blank
    validator or silently resetting fields the caller never meant to touch.
    The payoff is that the generated OpenAPI schema advertises each field's
    real type -- not nullable, no phantom default -- so an explicit JSON
    `null`, which no field here legitimately accepts, now fails ordinary
    type validation (422) instead of requiring a bespoke validator to catch
    it after the fact.
    """

    title: str = Field(default_factory=str, max_length=_CRUD_TITLE_MAX)
    source: str = Field(default_factory=str)
    confidence: str = Field(default_factory=str)
    tags: list[_CrudTag] = Field(default_factory=list, max_length=_CRUD_MAX_TAGS)
    status: MemoryStatus = Field(default_factory=lambda: MemoryStatus.capture_quick)
    stage: MemoryStage = Field(default_factory=lambda: MemoryStage.quick)

    snapshot: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    why_matters: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    known: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    inferred: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    unknown: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    decisions: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    alternatives: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    rationale: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    consequences: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    constraints: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    assumptions: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    risks: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    evidence: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    open_questions: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    next_actions: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    recovery_keywords: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    recovery_people: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    recovery_files: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    resume_trigger: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        # Only runs when the client actually sends `title`; an omitted title
        # keeps its never-applied default and this validator is skipped.
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be empty")
        return stripped


class MemoryItemRead(MemoryItemContent):
    """A memory item as returned by the API, including derived staleness."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created: datetime
    updated: datetime

    @field_validator("created", "updated", mode="before")
    @classmethod
    def _coerce_aware(cls, value: datetime) -> datetime:
        return _ensure_aware(value)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_stale(self) -> bool:
        """True for a stale-eligible (non-terminal) item whose `updated` is
        past the configured threshold; always False for terminal items.

        Stale-eligibility is the shared models.STALE_ELIGIBLE_STATUSES set
        (all five non-terminal statuses), not a schema-local subset.
        """
        if self.status not in STALE_ELIGIBLE_STATUSES:
            return False
        threshold = timedelta(days=get_settings().stale_after_days)
        return datetime.now(UTC) - self.updated > threshold


class ProgressEntryRead(BaseModel):
    """A single progress-log entry."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    item_id: int
    date: datetime
    note: str

    @field_validator("date", mode="before")
    @classmethod
    def _coerce_aware(cls, value: datetime) -> datetime:
        return _ensure_aware(value)


class MemoryItemReadWithProgress(MemoryItemRead):
    """A memory item plus its full progress history (oldest first)."""

    progress: list[ProgressEntryRead] = Field(default_factory=list)


class ProgressEntryCreate(BaseModel):
    """Payload for appending a progress-log entry."""

    note: str = Field(max_length=_CRUD_SECTION_MAX)

    @field_validator("note")
    @classmethod
    def _note_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("note must not be empty")
        return stripped


class ItemListResponse(BaseModel):
    """Paginated list of memory items."""

    items: list[MemoryItemRead]
    total: int


class ReviewResponse(BaseModel):
    """Disjoint groups of items that need attention, oldest first per group."""

    needs_enrichment: list[MemoryItemRead]
    active: list[MemoryItemRead]
    waiting: list[MemoryItemRead]
    parked: list[MemoryItemRead]


# Upper bound on every AI free-text request field. Stripped-non-empty is
# enforced per field below; the length is validated on the raw input (a 20001
# character body is 422) so an oversized payload is rejected before any LLM
# call. Mirrors the per-section output cap in app.services.memory_ai.
_MAX_AI_INPUT_CHARS = 20000


def _stripped_non_empty(value: str, field: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} must not be empty")
    return stripped


class LLMStatus(BaseModel):
    """Whether an LLM endpoint is configured, and the model name only.

    Deliberately excludes the base URL and API key -- only the boolean and the
    non-secret model name are surfaced. ``model`` is genuinely null exactly
    when unconfigured, so its nullability is honest rather than a schema
    artifact.
    """

    configured: bool
    model: str | None


class CaptureRequest(BaseModel):
    """Payload for AI quick capture: the raw discussion text to structure."""

    raw_text: str = Field(min_length=1, max_length=_MAX_AI_INPUT_CHARS)

    @field_validator("raw_text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _stripped_non_empty(value, "raw_text")


class CaptureResponse(BaseModel):
    """AI quick-capture result: the created item plus follow-up questions."""

    item: MemoryItemRead
    questions: list[str]


class EnrichRequest(BaseModel):
    """Payload for AI full enrichment: new context to integrate."""

    additional_context: str = Field(min_length=1, max_length=_MAX_AI_INPUT_CHARS)

    @field_validator("additional_context")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _stripped_non_empty(value, "additional_context")


class EnrichResponse(BaseModel):
    """AI enrichment result: the updated item plus remaining checklist gaps."""

    item: MemoryItemRead
    gaps: list[str]


class AssistUpdateRequest(BaseModel):
    """Payload for AI assisted update: a progress note to record."""

    note: str = Field(min_length=1, max_length=_MAX_AI_INPUT_CHARS)

    @field_validator("note")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _stripped_non_empty(value, "note")


class AssistUpdateResponse(BaseModel):
    """AI assisted-update result: the updated item."""

    item: MemoryItemRead
