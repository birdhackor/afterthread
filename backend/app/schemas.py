"""Pydantic request/response schemas for the Context Memory API."""

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from app.config import get_settings
from app.models import MemoryStage, MemoryStatus

# Statuses considered "in progress" and therefore eligible to go stale.
_IN_PROGRESS = {MemoryStatus.active, MemoryStatus.waiting, MemoryStatus.parked}


def _ensure_aware(value: datetime) -> datetime:
    """Treat naive datetimes (as returned by SQLite) as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


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
    """Payload for creating a memory item. Only ``title`` is mandatory."""

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be empty")
        return stripped


class MemoryItemUpdate(BaseModel):
    """Partial update payload; every field is optional."""

    title: str | None = None
    source: str | None = None
    confidence: str | None = None
    tags: list[str] | None = None
    status: MemoryStatus | None = None
    stage: MemoryStage | None = None

    snapshot: str | None = None
    why_matters: str | None = None
    known: str | None = None
    inferred: str | None = None
    unknown: str | None = None
    decisions: str | None = None
    alternatives: str | None = None
    rationale: str | None = None
    consequences: str | None = None
    constraints: str | None = None
    assumptions: str | None = None
    risks: str | None = None
    evidence: str | None = None
    open_questions: str | None = None
    next_actions: str | None = None
    recovery_keywords: str | None = None
    recovery_people: str | None = None
    recovery_files: str | None = None
    resume_trigger: str | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str | None) -> str | None:
        # Only runs when the client actually sends `title`; an omitted title
        # keeps its default and is skipped. Reject blanks and explicit null.
        if value is None:
            raise ValueError("title must not be null")
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
        """True for in-progress items whose `updated` is past the threshold."""
        if self.status not in _IN_PROGRESS:
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

    note: str

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
