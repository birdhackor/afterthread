"""Review endpoint grouping non-terminal items that need attention."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session, col, select

from afterthread.db import get_session
from afterthread.models import MemoryItem, MemoryStatus
from afterthread.schemas import MemoryItemRead, ReviewResponse

router = APIRouter(prefix="/review", tags=["review"])

SessionDep = Annotated[Session, Depends(get_session)]

# Maps each non-terminal status to the review bucket it belongs to. `done`
# and `superseded` are intentionally absent: they're terminal and excluded
# from review entirely.
_STATUS_TO_GROUP: dict[MemoryStatus, str] = {
    MemoryStatus.capture_quick: "needs_enrichment",
    MemoryStatus.needs_enrichment: "needs_enrichment",
    MemoryStatus.active: "active",
    MemoryStatus.waiting: "waiting",
    MemoryStatus.parked: "parked",
}


@router.get("", response_model=ReviewResponse)
def review(session: SessionDep) -> ReviewResponse:
    """Group non-terminal items into disjoint buckets, oldest first per group.

    The reviewable statuses are exactly the five non-terminal ones
    (models.STALE_ELIGIBLE_STATUSES); ``done`` and ``superseded`` are terminal
    and excluded entirely. A single query fetches every reviewable item so the
    buckets reflect one consistent snapshot. Building each bucket from its own
    query would let a status PATCH race between them and double-place or
    misplace an item (and the ORM identity map could hand back stale state for
    a row re-read across queries). Staleness is surfaced per item via
    ``MemoryItemRead.is_stale`` rather than as a separate group.
    """
    stmt = (
        select(MemoryItem)
        .where(col(MemoryItem.status).in_(_STATUS_TO_GROUP.keys()))
        .order_by(col(MemoryItem.updated).asc(), col(MemoryItem.id).asc())
    )

    groups: dict[str, list[MemoryItemRead]] = {
        "needs_enrichment": [],
        "active": [],
        "waiting": [],
        "parked": [],
    }
    for item in session.exec(stmt).all():
        groups[_STATUS_TO_GROUP[item.status]].append(MemoryItemRead.model_validate(item))

    return ReviewResponse(**groups)
