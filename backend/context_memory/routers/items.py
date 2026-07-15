"""CRUD endpoints for memory items and their append-only progress log."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, col, select

from context_memory.db import get_session
from context_memory.models import MemoryItem, MemoryStage, MemoryStatus, ProgressEntry, utcnow
from context_memory.schemas import (
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

# OpenAPI declaration for the 404 that the four by-id routes below can return
# (GET/PATCH/DELETE /items/{item_id} and POST /items/{item_id}/progress).
# Applied via each of those decorators' `responses=` so the generated schema
# matches runtime; the collection routes (POST/GET /items, GET /review) cannot
# 404 and deliberately do not carry it. Both the description and the example
# derive from `_NOT_FOUND`, so neither can drift from the detail the handlers
# actually raise.
_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {
        "description": _NOT_FOUND,
        "content": {"application/json": {"example": {"detail": _NOT_FOUND}}},
    }
}

# SQLite's INTEGER storage class is a signed 64-bit two's-complement integer
# (https://www.sqlite.org/datatype3.html#the_integer_datatypes): 2**63 - 1 is
# the largest value it can hold. FastAPI's `int` path/query converters accept
# any Python int, which is unbounded, so a value beyond this -- e.g.
# `item_id=9223372036854775808` (2**63) -- parses fine at the routing layer
# but raises OverflowError from the pysqlite driver the moment it is bound
# into a query, which FastAPI does not catch: it propagates as an unhandled
# 500 instead of a client error. Every `int` path/query parameter that is
# bound into a SQLite query below must be capped at this constant so
# out-of-range values are instead rejected with 422 during FastAPI's own
# validation, before ever reaching the database.
SQLITE_MAX_INT = 9223372036854775807  # 2**63 - 1

# Shared path type for `item_id`: applied to every route below that looks up
# a MemoryItem by id (GET/PATCH/DELETE /{item_id} and POST
# /{item_id}/progress) so all four enforce the same bounds. Lower-bounded at
# 1 since SQLite's `INTEGER PRIMARY KEY` rowids -- MemoryItem.id here -- are
# always positive; upper-bounded at SQLITE_MAX_INT for the OverflowError
# reason above.
ItemId = Annotated[int, Path(ge=1, le=SQLITE_MAX_INT)]

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
    # json_each is SQLite-only; context_memory.db.create_db_engine rejects any non-SQLite
    # database_url at engine creation, so that precondition is guaranteed here.
    element = func.json_each(col(MemoryItem.tags)).table_valued("value")
    return select(1).select_from(element).where(element.c.value == tag).exists()


@router.post("", response_model=MemoryItemRead, status_code=201)
def create_item(payload: MemoryItemCreate, session: SessionDep) -> MemoryItemRead:
    """Create an item and seed its history with the first progress entry."""
    item = MemoryItem(**payload.model_dump())
    # Seed the append-only history so it starts at creation time. The cascade
    # relationship assigns the foreign key on commit, so item_id is unset here.
    item.entries.append(ProgressEntry(note="建立項目"))  # ty: ignore[missing-argument]
    session.add(item)
    # Flush (not commit) to populate item.id and other DB-assigned defaults
    # while the instance's attributes are still loaded in memory, then
    # snapshot the response into a plain pydantic model *before* committing.
    # A post-commit session.refresh(item) -- the previous approach -- issues
    # a fresh SELECT against the row; if another session's DELETE for this
    # same item lands and commits in the gap between our commit and that
    # refresh, the SELECT finds nothing and refresh() raises
    # InvalidRequestError, turning an already-successful create into a 500.
    # Returning a detached snapshot instead means nothing after commit ever
    # touches the database again, so that race cannot affect the response.
    session.flush()
    result = MemoryItemRead.model_validate(item)
    session.commit()
    return result


# Bound on how many times list_items re-reads an EMPTY page whose standalone
# COUNT disagrees with it (total > offset -- see the coherence contract in
# list_items). One initial read plus at most this many re-reads, so a
# concurrent-write storm cannot make the empty-page path spin here forever.
_EMPTY_PAGE_MAX_RETRIES = 2


@router.get("", response_model=ItemListResponse)
def list_items(
    session: SessionDep,
    status: MemoryStatus | None = None,
    stage: MemoryStage | None = None,
    tag: str | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=SQLITE_MAX_INT)] = 0,
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
        # SQLite's ilike() lowercases only ASCII, so a title of "École" would
        # not match a query of "école". Fold both the column and the (already
        # LIKE-escaped) pattern through py_casefold -- full Unicode case
        # folding (see context_memory/db.py) -- then match with a plain LIKE, keeping the
        # same escape char. The escape char and wildcards (\, %, _) are
        # uncased, so folding the pattern leaves the LIKE escaping intact.
        pattern = func.py_casefold(f"%{_like_escape(q)}%")
        filters.append(
            or_(
                func.py_casefold(col(MemoryItem.title)).like(pattern, escape=_LIKE_ESCAPE),
                func.py_casefold(col(MemoryItem.snapshot)).like(pattern, escape=_LIKE_ESCAPE),
                func.py_casefold(col(MemoryItem.recovery_keywords)).like(
                    pattern, escape=_LIKE_ESCAPE
                ),
            )
        )

    # Coherence contract for the (items, total) pair, stated honestly:
    #
    #   * A NON-EMPTY page is single-snapshot BY CONSTRUCTION. Its rows and
    #     their true (pre-pagination) total come from ONE query: a COUNT(*)
    #     window that reuses the same `filters` and rides on every returned row
    #     (read from the first). No concurrent write can wedge between the count
    #     and the rows, so `total` can never disagree with `items` here (two
    #     separate SELECTs could return e.g. total=1 alongside 2 rows; this
    #     cannot).
    #
    #   * An EMPTY page carries no window-count row, so its total needs a
    #     standalone COUNT -- and that COUNT runs in a DIFFERENT SQLite snapshot
    #     than the page query (SELECTs execute in pysqlite's autocommit mode; no
    #     BEGIN spans the two, and adding one is out of scope). A concurrent
    #     insert landing between them could otherwise yield the incoherent
    #     `items=[] total>offset`: a row that belongs on THIS very page, yet is
    #     absent from it. So empty pages CONVERGE via bounded retry instead:
    #     when the COUNT reports rows that should fall on this page
    #     (total > offset), the state moved between the two statements, so
    #     re-read both (page + count). When total <= offset the pair is already
    #     coherent (the offset is genuinely at/past the end of the result set)
    #     -- return it as-is. The retry count is bounded
    #     (_EMPTY_PAGE_MAX_RETRIES), so unrelenting concurrent churn cannot spin
    #     here forever; if the bound is reached while still incoherent, we
    #     return the final iteration's OWN page and its OWN count -- an
    #     internally consistent pair drawn from a single loop turn -- rather
    #     than pairing rows and a total read at different times.
    list_stmt = (
        select(MemoryItem, func.count().over())
        .where(*filters)
        .order_by(col(MemoryItem.updated).desc(), col(MemoryItem.id).desc())
        .offset(offset)
        .limit(limit)
    )
    count_stmt = select(func.count()).select_from(MemoryItem).where(*filters)
    items: list[MemoryItem] = []
    total = 0
    for _attempt in range(_EMPTY_PAGE_MAX_RETRIES + 1):
        rows = session.exec(list_stmt).all()
        if rows:
            # Non-empty page: items and total are one atomic snapshot.
            total = rows[0][1]
            items = [row[0] for row in rows]
            break
        # Empty page: derive the true total from a standalone COUNT.
        total = session.exec(count_stmt).one()
        items = []
        if total <= offset:
            # Offset at/beyond the end of the result set: an empty page with
            # this total is coherent. Done.
            break
        # total > offset: a row should sit on this page but the page query did
        # not see it -- the DB moved between the two statements. Loop to re-read
        # both; the bound above guarantees this terminates.
    return ItemListResponse(
        items=[MemoryItemRead.model_validate(item) for item in items],
        total=total,
    )


@router.get(
    "/{item_id}",
    response_model=MemoryItemReadWithProgress,
    responses=_NOT_FOUND_RESPONSE,
)
def get_item(item_id: ItemId, session: SessionDep) -> MemoryItemReadWithProgress:
    """Return a single item with its full progress history (oldest first)."""
    # Load the item and its entries in ONE statement (joinedload), not a
    # session.get() followed by a lazy load of `entries`: two SELECTs with a
    # gap a concurrent committed DELETE can slip into, returning 200 with an
    # empty progress list -- a phantom state, since creation always seeds one
    # entry. A single statement yields either the full item+entries or a 404.
    # session.get()'s options path applies the joinedload and handles the
    # eager collection's row uniquing internally.
    entries_loader = joinedload(MemoryItem.entries)  # ty: ignore[invalid-argument-type]
    item = session.get(MemoryItem, item_id, options=(entries_loader,))
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    result = MemoryItemReadWithProgress.model_validate(item)
    # entries arrive already ordered by the relationship's order_by
    # (ProgressEntry.date, ProgressEntry.id in models.py) -- the single source
    # of truth for progress ordering -- so iterate directly, no re-sort here.
    result.progress = [ProgressEntryRead.model_validate(entry) for entry in item.entries]
    return result


@router.patch("/{item_id}", response_model=MemoryItemRead, responses=_NOT_FOUND_RESPONSE)
def update_item(item_id: ItemId, payload: MemoryItemUpdate, session: SessionDep) -> MemoryItemRead:
    """Apply a partial update; bump `updated` only when a field is provided."""
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return MemoryItemRead.model_validate(item)
    for key, value in changes.items():
        setattr(item, key, value)
    item.updated = utcnow()
    session.add(item)
    try:
        # Flush (not commit) so a zero-row-matched UPDATE still raises
        # StaleDataError here, before we build the response snapshot below.
        session.flush()
    except StaleDataError as exc:
        # The item existed at the session.get() above but was deleted
        # (and that delete committed) by another session before this
        # flush -- the UPDATE this handler issues for `item` now matches
        # zero rows, which SQLAlchemy reports as StaleDataError rather
        # than silently doing nothing. Translate that race into the same
        # 404 a simple not-found lookup would give, instead of letting
        # it surface as an unhandled 500.
        session.rollback()
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from exc
    # Snapshot the response into a plain pydantic model *before* committing,
    # while item's attributes are still loaded in memory. A post-commit
    # session.refresh(item) -- the previous approach -- issues a fresh
    # SELECT against the row; if another session's DELETE for this same
    # item lands and commits in the gap between our commit and that
    # refresh, the SELECT finds nothing and refresh() raises
    # InvalidRequestError, turning an already-successful update into a 500.
    # Returning a detached snapshot instead means nothing after commit ever
    # touches the database again, so that race cannot affect the response.
    result = MemoryItemRead.model_validate(item)
    session.commit()
    return result


@router.delete("/{item_id}", status_code=204, responses=_NOT_FOUND_RESPONSE)
def delete_item(item_id: ItemId, session: SessionDep) -> None:
    """Delete an item; its progress entries are removed via cascade."""
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    session.delete(item)
    try:
        session.commit()
    except StaleDataError as exc:
        # Delete-race contract, stated honestly: a mid-commit race -- the item
        # existed at session.get() above but was deleted (and that delete
        # committed) by another session before this commit -- resolves to 204
        # TODAY, and would resolve to 404 only IF a future SQLAlchemy changed
        # how a zero-row DELETE is handled. This is NOT a claim that
        # delete-after-delete is deliberately idempotent "no-op success"
        # semantics; it is simply that SQLAlchemy does not check a DELETE's
        # matched-row count. Without a `version_id_col` on the mapper
        # (`MemoryItem` has none), a zero-row-matched DELETE only emits a
        # warning, not StaleDataError (see Mapper's confirm_deleted_rows docs:
        # "the warning may be changed to an exception in a future release"), so
        # session.commit() above just succeeds and the handler falls through to
        # its normal 204 -- meaning this except clause is unreachable in the
        # real world today. It is retained purely as forward-compatible
        # hardening: if a future/alternate SQLAlchemy (or a mapper reconfigured
        # with `version_id_col`) does raise StaleDataError for a zero-row
        # DELETE, this translates that into the same 404 a not-found lookup
        # gives, kept symmetric with the UPDATE-based races above (which do
        # raise today). See
        # tests/test_items.py::test_delete_races_with_concurrent_delete_returns_204
        # for the reachable 204 path and
        # ::test_delete_races_with_manufactured_stale_data_error_returns_404
        # for this clause's defensive-only coverage.
        session.rollback()
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from exc


@router.post(
    "/{item_id}/progress",
    response_model=ProgressEntryRead,
    status_code=201,
    responses=_NOT_FOUND_RESPONSE,
)
def add_progress(
    item_id: ItemId, payload: ProgressEntryCreate, session: SessionDep
) -> ProgressEntryRead:
    """Append a progress entry and bump the item's `updated` timestamp."""
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    entry = ProgressEntry(item_id=item_id, note=payload.note)
    item.updated = utcnow()
    session.add(entry)
    session.add(item)
    try:
        # Flush (not commit) so the StaleDataError/IntegrityError races
        # below still surface here, before we build the response snapshot.
        session.flush()
    except (StaleDataError, IntegrityError) as exc:
        # The item existed at the session.get() above but was deleted (and
        # that delete committed) by another session before this flush.
        # Which exception surfaces depends on flush ordering, which this
        # handler does not control: if the flush emits `item`'s UPDATE
        # (bumping `updated`) first, that UPDATE now matches zero rows and
        # SQLAlchemy raises StaleDataError; if it emits `entry`'s INSERT
        # first instead, the FK constraint on `entry.item_id` rejects it
        # with IntegrityError. Either way the item is gone, so both
        # translate to the same 404 a simple not-found lookup would give,
        # instead of letting either surface as an unhandled 500.
        session.rollback()
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from exc
    # Snapshot the response into a plain pydantic model *before* committing,
    # while entry's attributes (including the id the flush above just
    # assigned) are still loaded in memory. A post-commit
    # session.refresh(entry) -- the previous approach -- issues a fresh
    # SELECT against the row; if another session's DELETE for this entry's
    # item cascades into deleting this entry too and commits in the gap
    # between our commit and that refresh, the SELECT finds nothing and
    # refresh() raises InvalidRequestError, turning an already-successful
    # append into a 500. Returning a detached snapshot instead means
    # nothing after commit ever touches the database again, so that race
    # cannot affect the response.
    result = ProgressEntryRead.model_validate(entry)
    session.commit()
    return result
