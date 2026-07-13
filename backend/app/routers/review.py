"""Review endpoint grouping in-progress items that need attention."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session, col, select

from app.db import get_session
from app.models import MemoryItem, MemoryStatus
from app.schemas import MemoryItemRead, ReviewResponse

router = APIRouter(prefix="/review", tags=["review"])

SessionDep = Annotated[Session, Depends(get_session)]


def _fetch(session: Session, statuses: list[MemoryStatus]) -> list[MemoryItemRead]:
    """Fetch items in the given statuses, oldest first (needs attention first)."""
    stmt = (
        select(MemoryItem)
        .where(col(MemoryItem.status).in_(statuses))
        .order_by(col(MemoryItem.updated).asc(), col(MemoryItem.id).asc())
    )
    return [MemoryItemRead.model_validate(item) for item in session.exec(stmt).all()]


@router.get("", response_model=ReviewResponse)
def review(session: SessionDep) -> ReviewResponse:
    """Group in-progress items into disjoint buckets, oldest first per group.

    ``done`` and ``superseded`` are excluded entirely; staleness is surfaced
    per item via ``MemoryItemRead.is_stale`` rather than as a separate group.
    """
    return ReviewResponse(
        needs_enrichment=_fetch(
            session, [MemoryStatus.capture_quick, MemoryStatus.needs_enrichment]
        ),
        active=_fetch(session, [MemoryStatus.active]),
        waiting=_fetch(session, [MemoryStatus.waiting]),
        parked=_fetch(session, [MemoryStatus.parked]),
    )
