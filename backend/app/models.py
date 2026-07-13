"""SQLModel table definitions for Context Memory.

The persistence layer follows the canonical SQLModel base-class pattern: a
non-table ``MemoryItemBase`` holds every durable content field, and the table
model ``MemoryItem`` adds identity, timestamps and the progress relationship.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON
from sqlmodel import Column, Field, Relationship, SQLModel


class MemoryStatus(StrEnum):
    """Lifecycle status of a memory item (see docs/methodology.md)."""

    capture_quick = "capture-quick"
    needs_enrichment = "needs-enrichment"
    active = "active"
    waiting = "waiting"
    parked = "parked"
    done = "done"
    superseded = "superseded"


class MemoryStage(StrEnum):
    """Two-stage capture workflow marker."""

    quick = "quick"
    full = "full"


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class MemoryItemBase(SQLModel):
    """Durable content fields shared by the table model and API schemas."""

    title: str
    source: str = "manual"
    confidence: str = "mixed"
    # tags is a JSON array column; sa_column is declared here on the non-table
    # base and is only ever attached to the single MemoryItem table below.
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: MemoryStatus = MemoryStatus.capture_quick
    stage: MemoryStage = MemoryStage.quick

    # Capture snapshot + anti-vaporization sections. All optional, default "".
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


class MemoryItem(MemoryItemBase, table=True):
    """A single unit of stored context."""

    __tablename__ = "memory_item"

    id: int | None = Field(default=None, primary_key=True)
    created: datetime = Field(default_factory=utcnow)
    updated: datetime = Field(default_factory=utcnow)

    # Quotes are required: ProgressEntry is defined below and SQLModel needs a
    # runtime forward reference (a real name, not PEP 563 lazy strings).
    entries: list["ProgressEntry"] = Relationship(  # noqa: UP037
        back_populates="item",
        # Deleting an item deletes its progress history (ORM-level cascade).
        # order_by keeps the append-only log in chronological order.
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "order_by": "ProgressEntry.date, ProgressEntry.id",
        },
    )


class ProgressEntry(SQLModel, table=True):
    """Append-only progress log entry belonging to a memory item."""

    __tablename__ = "progress_entry"

    id: int | None = Field(default=None, primary_key=True)
    item_id: int = Field(foreign_key="memory_item.id", index=True)
    date: datetime = Field(default_factory=utcnow)
    note: str

    item: MemoryItem | None = Relationship(back_populates="entries")
