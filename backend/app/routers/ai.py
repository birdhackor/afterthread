"""AI-assisted memory workflows: LLM status and quick capture.

These endpoints wrap ``app.services.memory_ai``. The methodology rules
(confidence honesty, supersede-not-delete, max-3-questions, bullets,
language-follows-content) live in the service-layer prompts; this router owns
the HTTP contract:

* the error taxonomy -- 503 (llm_not_configured) and 502 (llm_upstream_error),
  each with a fixed, config-free ``{code, message}`` detail;
* the transaction discipline -- the LLM call runs strictly OUTSIDE any DB
  transaction, writes begin only after a successful response, the response
  snapshot is built after flush but before commit, and there is no
  ``session.refresh``;
* OpenAPI ``responses=`` declarations that match that runtime behaviour, so
  generated clients and docs never overstate or understate what can happen.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.models import MemoryItem, MemoryStage, ProgressEntry, utcnow
from app.routers.items import _NOT_FOUND, _NOT_FOUND_RESPONSE, ItemId
from app.schemas import (
    AssistUpdateRequest,
    AssistUpdateResponse,
    CaptureRequest,
    CaptureResponse,
    EnrichRequest,
    EnrichResponse,
    LLMStatus,
    MemoryItemRead,
)
from app.services.llm import LLMNotConfiguredError, LLMUpstreamError, llm_configured
from app.services.memory_ai import (
    SECTION_FIELD_ORDER,
    assist_update,
    capture_draft,
    enrich_item,
)

router = APIRouter(tags=["ai"])

SessionDep = Annotated[Session, Depends(get_session)]

# Seed / fallback notes for the append-only log. The enrich/update fallbacks
# are used only when the model returns an empty progress_note.
_CAPTURE_NOTE = "AI 快速捕捉"
_ENRICH_NOTE = "AI 全面補充"
_UPDATE_NOTE = "AI 協助更新"

# Item fields snapshotted (before the LLM call) to build the enrich/update
# prompt: the title plus every whitelisted section.
_AI_ITEM_FIELDS: tuple[str, ...] = ("title", *SECTION_FIELD_ORDER)

# Error contract. Codes and messages are fixed and config-free; the OpenAPI
# examples below are built from the very same constants so the declared shape
# cannot drift from what the handlers raise.
_LLM_NOT_CONFIGURED_CODE = "llm_not_configured"
_LLM_UPSTREAM_CODE = "llm_upstream_error"
_LLM_NOT_CONFIGURED_MESSAGE = "The LLM endpoint is not configured."

_LLM_UNCONFIGURED_RESPONSE: dict[int | str, dict[str, Any]] = {
    503: {
        "description": "LLM endpoint is not configured",
        "content": {
            "application/json": {
                "example": {
                    "detail": {
                        "code": _LLM_NOT_CONFIGURED_CODE,
                        "message": _LLM_NOT_CONFIGURED_MESSAGE,
                    }
                }
            }
        },
    }
}

_LLM_UPSTREAM_RESPONSE: dict[int | str, dict[str, Any]] = {
    502: {
        "description": "LLM upstream call failed or returned unusable output",
        "content": {
            "application/json": {
                "example": {
                    "detail": {
                        "code": _LLM_UPSTREAM_CODE,
                        "message": "LLMUpstreamError: the upstream LLM request failed",
                    }
                }
            }
        },
    }
}

# Both AI failure modes apply to every AI workflow endpoint (capture, and the
# by-id enrich/assist-update added later).
_AI_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_LLM_UNCONFIGURED_RESPONSE,
    **_LLM_UPSTREAM_RESPONSE,
}


def _service_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"code": _LLM_NOT_CONFIGURED_CODE, "message": _LLM_NOT_CONFIGURED_MESSAGE},
    )


def _bad_gateway(exc: LLMUpstreamError) -> HTTPException:
    # str(exc) is safe by construction (see app.services.llm): an exception
    # category plus a short reason, never a config value or a response body.
    return HTTPException(
        status_code=502,
        detail={"code": _LLM_UPSTREAM_CODE, "message": str(exc)},
    )


@router.get("/llm/status", response_model=LLMStatus)
def llm_status() -> LLMStatus:
    """Report whether an LLM endpoint is configured, and the model name only.

    Never returns the base URL or API key -- only the boolean and the
    non-secret model name (null when unconfigured).
    """
    configured = llm_configured()
    model = get_settings().openai_model if configured else None
    return LLMStatus(configured=configured, model=model)


@router.post(
    "/capture",
    response_model=CaptureResponse,
    status_code=201,
    responses=_AI_ERROR_RESPONSES,
)
async def capture(payload: CaptureRequest, session: SessionDep) -> CaptureResponse:
    """Quick-capture raw text into a structured memory item.

    The LLM call runs first, entirely outside any DB transaction. Only after a
    successful draft does ONE transaction create the item (source "llm-capture",
    stage quick, status from the model's suggestion) and seed its progress log.
    A 502 (unparseable or failed draft) therefore leaves the database untouched
    -- no partial rows -- because no write has happened yet.
    """
    try:
        draft = await capture_draft(payload.raw_text)
    except LLMNotConfiguredError as exc:
        raise _service_unavailable() from exc
    except LLMUpstreamError as exc:
        raise _bad_gateway(exc) from exc

    item = MemoryItem(
        title=draft.title,
        source="llm-capture",
        status=draft.suggested_status,
        stage=MemoryStage.quick,
        tags=draft.tags,
        snapshot=draft.snapshot,
        why_matters=draft.why_matters,
        known=draft.known,
        inferred=draft.inferred,
        unknown=draft.unknown,
        next_actions=draft.next_actions,
        recovery_keywords=draft.recovery_keywords,
        recovery_people=draft.recovery_people,
        recovery_files=draft.recovery_files,
        resume_trigger=draft.resume_trigger,
    )
    item.entries.append(ProgressEntry(note=_CAPTURE_NOTE))  # ty: ignore[missing-argument]
    session.add(item)
    # Flush to assign id/DB defaults, snapshot the response BEFORE commit (no
    # post-commit session.refresh), then commit -- mirroring items.create_item.
    session.flush()
    result = MemoryItemRead.model_validate(item)
    session.commit()
    return CaptureResponse(item=result, questions=draft.questions)


# The two by-id AI routes can 404 (unknown item, or an item deleted during the
# await) on top of the 503/502 AI failure modes.
_AI_BY_ID_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_NOT_FOUND_RESPONSE,
    **_AI_ERROR_RESPONSES,
}


@router.post(
    "/items/{item_id}/enrich",
    response_model=EnrichResponse,
    responses=_AI_BY_ID_RESPONSES,
)
async def enrich(item_id: ItemId, payload: EnrichRequest, session: SessionDep) -> EnrichResponse:
    """Full-enrich an item: merge whitelisted section updates and append progress.

    Order (constraint): 404 first, then snapshot the item fields, then run the
    LLM strictly OUTSIDE any transaction (the read transaction is released
    before the await). Only after a successful result do writes begin -- merge
    the returned sections (untouched fields stay as they were), flip stage to
    full when the checklist is complete, append the progress entry, bump
    `updated`. The response snapshot is built after flush, before commit; the
    flush is race-wrapped so a concurrent delete resolves to 404, not a 500.
    A 503/502 (or an item deleted mid-await) leaves the row unchanged.
    """
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    item_fields = {name: getattr(item, name) for name in _AI_ITEM_FIELDS}
    # Release the read transaction so the LLM call holds no DB transaction open.
    session.rollback()

    try:
        result = await enrich_item(item_fields, payload.additional_context)
    except LLMNotConfiguredError as exc:
        raise _service_unavailable() from exc
    except LLMUpstreamError as exc:
        raise _bad_gateway(exc) from exc

    # Re-fetch: the item may have been deleted during the await.
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    for key, value in result.sections.items():
        setattr(item, key, value)
    if result.checklist_complete:
        item.stage = MemoryStage.full
    note = result.progress_note or _ENRICH_NOTE
    item.entries.append(ProgressEntry(note=note))  # ty: ignore[missing-argument]
    item.updated = utcnow()
    session.add(item)
    try:
        session.flush()
    except (StaleDataError, IntegrityError) as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from exc
    result_read = MemoryItemRead.model_validate(item)
    session.commit()
    return EnrichResponse(item=result_read, gaps=result.remaining_gaps)


@router.post(
    "/items/{item_id}/assist-update",
    response_model=AssistUpdateResponse,
    responses=_AI_BY_ID_RESPONSES,
)
async def assist_update_item(
    item_id: ItemId, payload: AssistUpdateRequest, session: SessionDep
) -> AssistUpdateResponse:
    """Assist an update: merge whitelisted section refreshes and append progress.

    Same discipline as enrich (404 first, snapshot, LLM outside any
    transaction, writes only after success, snapshot after flush before commit,
    race-wrapped flush -> 404), but records a progress note without touching
    stage or returning gaps.
    """
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    item_fields = {name: getattr(item, name) for name in _AI_ITEM_FIELDS}
    session.rollback()

    try:
        result = await assist_update(item_fields, payload.note)
    except LLMNotConfiguredError as exc:
        raise _service_unavailable() from exc
    except LLMUpstreamError as exc:
        raise _bad_gateway(exc) from exc

    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    for key, value in result.sections.items():
        setattr(item, key, value)
    note = result.progress_note or _UPDATE_NOTE
    item.entries.append(ProgressEntry(note=note))  # ty: ignore[missing-argument]
    item.updated = utcnow()
    session.add(item)
    try:
        session.flush()
    except (StaleDataError, IntegrityError) as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from exc
    result_read = MemoryItemRead.model_validate(item)
    session.commit()
    return AssistUpdateResponse(item=result_read)
