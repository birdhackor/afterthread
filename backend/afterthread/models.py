"""SQLModel table definitions for afterthread.

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


# The five non-terminal statuses -- every status except the terminal `done`
# and `superseded`. An item in any of these is eligible to go stale once its
# `updated` timestamp passes the configured threshold (see
# schemas.MemoryItemRead.is_stale). Defined here, next to MemoryStatus, as the
# single source of truth for stale-eligibility: it is the repo's methodology to
# enrich and keep context warm *before* the topic goes cold, which applies just
# as much to a still-unenriched capture-quick / needs-enrichment item as to an
# active one -- so all five, not only active/waiting/parked, are included.
STALE_ELIGIBLE_STATUSES = frozenset(
    {
        MemoryStatus.capture_quick,
        MemoryStatus.needs_enrichment,
        MemoryStatus.active,
        MemoryStatus.waiting,
        MemoryStatus.parked,
    }
)


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
    # Without this, SQLite's default ROWID assignment for an INTEGER PRIMARY
    # KEY column reuses the id of the just-deleted highest-id row on the next
    # insert (it is recomputed as max(id) + 1 each time, not drawn from a
    # persistent sequence): delete the item with the current-highest id, then
    # create a new one, and the new row silently gets the *same* id back. A
    # client holding a stale reference to the deleted item -- an open tab, a
    # bookmark, an in-flight PATCH/DELETE request built before the delete --
    # would then land on the new, unrelated item instead of getting the 404
    # it should. sqlite_autoincrement makes SQLite track the historical
    # maximum in its internal sqlite_sequence table instead, so a new id is
    # always strictly greater than every id ever used before, even after the
    # highest-id row is gone (see
    # https://www.sqlite.org/autoinc.html). See
    # tests/test_items.py::test_delete_then_create_does_not_reuse_id.
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    created: datetime = Field(default_factory=utcnow)
    updated: datetime = Field(default_factory=utcnow)

    # Quotes are required: ProgressEntry is defined below and SQLModel needs a
    # runtime forward reference (a real name, not PEP 563 lazy strings).
    entries: list["ProgressEntry"] = Relationship(  # noqa: UP037
        back_populates="item",
        # Deleting an item deletes its progress history. The FK's
        # ondelete="CASCADE" (see ProgressEntry.item_id below) makes SQLite
        # itself responsible for removing the child rows; passive_deletes
        # tells SQLAlchemy not to first SELECT the entries collection into
        # memory and issue an individual DELETE per row. That preload is what
        # created a race: a session that already loaded `entries` and then
        # commits its DELETE would not see a ProgressEntry inserted by a
        # concurrent session in between, so that untouched row still
        # references this item, and the parent DELETE hits the FK
        # constraint instead of silently dropping it (see
        # tests/test_items.py::test_delete_cascades_progress_entries and
        # afterthread/db.py::_set_sqlite_foreign_keys_pragma). Deferring to the
        # database's own cascade closes that window: the DELETE and the
        # removal of every row currently referencing it happen as one
        # statement. cascade="all, delete-orphan" is kept for the ORM-level
        # case of an entry being detached from `entries` while the item
        # itself is not deleted. order_by keeps the append-only log in
        # chronological order.
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "passive_deletes": True,
            "order_by": "ProgressEntry.date, ProgressEntry.id",
        },
    )


class ProgressEntry(SQLModel, table=True):
    """Append-only progress log entry belonging to a memory item."""

    __tablename__ = "progress_entry"
    # No endpoint currently mutates an entry by id (see routers/items.py --
    # only POST .../progress, which always creates), so the stale-reference
    # footgun this guards against on MemoryItem (see its __table_args__
    # comment) is not reachable through today's API. Applied anyway for
    # consistency: entry ids are already exposed in API responses
    # (ProgressEntryRead.id), the cost is a single DDL keyword, and it
    # removes the footgun in advance of any future id-targeted endpoint
    # (e.g. an edit/delete-single-entry route) rather than waiting to
    # rediscover the same bug there.
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    # ondelete="CASCADE" pushes deletion of orphaned entries down to SQLite
    # itself; see the passive_deletes note on MemoryItem.entries above for
    # why that -- rather than the ORM-level cascade alone -- is required.
    item_id: int = Field(foreign_key="memory_item.id", ondelete="CASCADE", index=True)
    date: datetime = Field(default_factory=utcnow)
    note: str

    item: MemoryItem | None = Relationship(back_populates="entries")
