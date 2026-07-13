"""CRUD endpoints for memory items and their append-only progress log."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, col, select

from app.db import get_session
from app.models import MemoryItem, MemoryStage, MemoryStatus, ProgressEntry, utcnow
from app.schemas import (
    ItemListResponse,
    MemoryItemCreate,
    MemoryItemRead,
    MemoryItemReadWithProgress,
    MemoryItemUpdate,
    ProgressEntryCreate,
    ProgressEntryRead,
)

router = APIRouter(prefix="/items", tags=["items"])

SessionDep = Annotated[Session, Depends(get_session)]

_NOT_FOUND = "Memory item not found"

# Escape character for user-built LIKE/ILIKE patterns. It must be escaped
# first in `_like_escape` so a literal backslash in the input round-trips.
_LIKE_ESCAPE = "\\"


def _like_escape(value: str) -> str:
    """Escape LIKE metacharacters so `value` matches only as a literal substring."""
    return (
        value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )


def _tag_filter(tag: str) -> ColumnElement[bool]:
    """Exact JSON-array membership test: does `tag` appear as an element of tags?

    tags is a JSON array persisted as text, and SQLAlchemy's default JSON
    serializer uses ensure_ascii=True, so non-ASCII tags (e.g. Chinese) are
    stored as \\uXXXX escapes. A LIKE match over that serialized text can
    never find them. json_each unpacks the array server-side so this compares
    decoded element values instead: exact membership, Unicode-correct, and a
    tag like "工" cannot match an item tagged "工作".
    """
    # json_each is SQLite-only; app.db.create_db_engine rejects any non-SQLite
    # database_url at engine creation, so that precondition is guaranteed here.
    element = func.json_each(col(MemoryItem.tags)).table_valued("value")
    return select(1).select_from(element).where(element.c.value == tag).exists()


@router.post("", response_model=MemoryItemRead, status_code=201)
def create_item(payload: MemoryItemCreate, session: SessionDep) -> MemoryItem:
    """Create an item and seed its history with the first progress entry."""
    item = MemoryItem(**payload.model_dump())
    # Seed the append-only history so it starts at creation time. The cascade
    # relationship assigns the foreign key on commit, so item_id is unset here.
    item.entries.append(ProgressEntry(note="建立項目"))  # ty: ignore[missing-argument]
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@router.get("", response_model=ItemListResponse)
def list_items(
    session: SessionDep,
    status: MemoryStatus | None = None,
    stage: MemoryStage | None = None,
    tag: str | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ItemListResponse:
    """List items (newest first) with optional filtering and pagination."""
    filters: list[ColumnElement[bool]] = []
    if status is not None:
        filters.append(col(MemoryItem.status) == status)
    if stage is not None:
        filters.append(col(MemoryItem.stage) == stage)
    if tag is not None:
        filters.append(_tag_filter(tag))
    if q is not None:
        like = f"%{_like_escape(q)}%"
        filters.append(
            or_(
                col(MemoryItem.title).ilike(like, escape=_LIKE_ESCAPE),
                col(MemoryItem.snapshot).ilike(like, escape=_LIKE_ESCAPE),
                col(MemoryItem.recovery_keywords).ilike(like, escape=_LIKE_ESCAPE),
            )
        )

    count_stmt = select(func.count()).select_from(MemoryItem)
    list_stmt = select(MemoryItem)
    for condition in filters:
        count_stmt = count_stmt.where(condition)
        list_stmt = list_stmt.where(condition)

    total = session.exec(count_stmt).one()
    list_stmt = (
        list_stmt.order_by(col(MemoryItem.updated).desc(), col(MemoryItem.id).desc())
        .offset(offset)
        .limit(limit)
    )
    items = session.exec(list_stmt).all()
    return ItemListResponse(
        items=[MemoryItemRead.model_validate(item) for item in items],
        total=total,
    )


@router.get("/{item_id}", response_model=MemoryItemReadWithProgress)
def get_item(item_id: int, session: SessionDep) -> MemoryItemReadWithProgress:
    """Return a single item with its full progress history (oldest first)."""
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    result = MemoryItemReadWithProgress.model_validate(item)
    result.progress = [
        ProgressEntryRead.model_validate(entry)
        for entry in sorted(item.entries, key=lambda e: (e.date, e.id or 0))
    ]
    return result


@router.patch("/{item_id}", response_model=MemoryItemRead)
def update_item(item_id: int, payload: MemoryItemUpdate, session: SessionDep) -> MemoryItem:
    """Apply a partial update; bump `updated` only when a field is provided."""
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    changes = payload.model_dump(exclude_unset=True)
    if changes:
        for key, value in changes.items():
            setattr(item, key, value)
        item.updated = utcnow()
        session.add(item)
        session.commit()
        session.refresh(item)
    return item


@router.delete("/{item_id}", status_code=204)
def delete_item(item_id: int, session: SessionDep) -> None:
    """Delete an item; its progress entries are removed via cascade."""
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    session.delete(item)
    session.commit()


@router.post("/{item_id}/progress", response_model=ProgressEntryRead, status_code=201)
def add_progress(item_id: int, payload: ProgressEntryCreate, session: SessionDep) -> ProgressEntry:
    """Append a progress entry and bump the item's `updated` timestamp."""
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    entry = ProgressEntry(item_id=item_id, note=payload.note)
    item.updated = utcnow()
    session.add(entry)
    session.add(item)
    try:
        session.commit()
    except IntegrityError as exc:
        # The item existed at the session.get() above but was deleted (and
        # that delete committed) by another session before this flush -- the
        # FK constraint on `entry` now rejects it. Translate that race into
        # the same 404 a simple not-found lookup would give, instead of
        # letting the IntegrityError surface as an unhandled 500.
        session.rollback()
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from exc
    session.refresh(entry)
    return entry
