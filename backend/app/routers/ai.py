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
from app.services.llm import (
    _UPSTREAM_REASON,
    LLMNotConfiguredError,
    LLMUpstreamError,
    llm_configured,
)
from app.services.memory_ai import (
    HISTORY_SECTIONS,
    SECTION_FIELD_ORDER,
    assist_update,
    capture_draft,
    enrich_item,
    merge_with_supersede,
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

# Error contract. Codes and messages are fixed and config-free. The `code`
# fields (and the 503 message) in the OpenAPI examples below are built from the
# very same constants the handlers raise, so those cannot drift. The 502
# message is only PARTLY constant at runtime -- "<category>: <reason>", where
# the category is the failing SDK error's class name and varies per failure --
# so its example pairs one realistic, illustrative category
# ("APIConnectionError") with the reason substring, which does derive from the
# same _UPSTREAM_REASON constant the service embeds in every SDK-error message.
_LLM_NOT_CONFIGURED_CODE = "llm_not_configured"
_LLM_UPSTREAM_CODE = "llm_upstream_error"
_LLM_NOT_CONFIGURED_MESSAGE = "The LLM endpoint is not configured."

# Optimistic-concurrency conflict (enrich / assist-update only): the item's
# `updated` timestamp changed between the pre-LLM snapshot and the post-LLM
# re-fetch, so another writer touched the row while the model was running. The
# message is a fixed zh-TW literal -- it carries no config value and no item
# content -- and the code mirrors the constant-derivation style of the 502/503
# examples so the OpenAPI 409 example below cannot drift from what is raised.
_CONFLICT_CODE = "conflict"
_CONFLICT_MESSAGE = "項目在 AI 處理期間已被其他變更修改。請重新載入後再試一次。"

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
                        "message": f"APIConnectionError: {_UPSTREAM_REASON}",
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

# 409 applies ONLY to the two by-id write workflows (enrich/assist-update),
# which snapshot the item, await the LLM, then re-fetch: capture creates a new
# row and has nothing to conflict with.
_CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    409: {
        "description": "Item changed during AI processing (optimistic concurrency)",
        "content": {
            "application/json": {
                "example": {
                    "detail": {
                        "code": _CONFLICT_CODE,
                        "message": _CONFLICT_MESSAGE,
                    }
                }
            }
        },
    }
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


def _conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": _CONFLICT_CODE, "message": _CONFLICT_MESSAGE},
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
# await) and 409 (item changed during the await) on top of the 503/502 AI
# failure modes. Sharing this one entry declares 409 on exactly those two
# operations and nowhere else.
_AI_BY_ID_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_NOT_FOUND_RESPONSE,
    **_CONFLICT_RESPONSE,
    **_AI_ERROR_RESPONSES,
}


@router.post(
    "/items/{item_id}/enrich",
    response_model=EnrichResponse,
    responses=_AI_BY_ID_RESPONSES,
)
async def enrich(item_id: ItemId, payload: EnrichRequest, session: SessionDep) -> EnrichResponse:
    """Full-enrich an item: merge whitelisted section updates and append progress.

    Order (constraint): 404 first, then snapshot the item fields (and its
    `updated` timestamp), then run the LLM strictly OUTSIDE any transaction (the
    read transaction is released before the await). On re-fetch, a changed
    `updated` means another writer touched the row mid-await, so respond 409 and
    write NOTHING. Otherwise writes begin -- merge the returned sections
    (untouched fields stay as they were; history-bearing sections are superseded,
    never overwritten), flip stage to full when the checklist is complete, append
    the progress entry, bump `updated`. The response snapshot is built after
    flush, before commit; the flush is race-wrapped so a concurrent delete
    resolves to 404, not a 500. A 503/502/409 (or an item deleted mid-await)
    leaves the row unchanged.
    """
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    item_fields = {name: getattr(item, name) for name in _AI_ITEM_FIELDS}
    # Snapshot `updated` BEFORE the await so a concurrent write during the LLM
    # call can be detected on re-fetch (optimistic concurrency, below).
    original_updated = item.updated
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
    # Optimistic concurrency: if another writer bumped `updated` while the model
    # was running, the sections we enriched against are stale. Write NOTHING and
    # return 409 so the caller re-reads and retries.
    if item.updated != original_updated:
        raise _conflict()
    for key, value in result.sections.items():
        # Supersede-not-delete for the history-bearing sections: merge losslessly
        # so a prior decision/rationale is never overwritten (see
        # memory_ai.merge_with_supersede). Other sections replace wholesale.
        if key in HISTORY_SECTIONS:
            value = merge_with_supersede(getattr(item, key), value)
        setattr(item, key, value)
    if result.checklist_complete:
        item.stage = MemoryStage.full
    note = result.progress_note or _ENRICH_NOTE
    # Append via an explicit, FK-bearing ProgressEntry -- mirroring
    # items.add_progress -- NOT via item.entries.append(...). Touching the
    # relationship lazy-loads `entries`, and with the item already dirty (the
    # setattrs above) that lazy load AUTOFLUSHES first: the item's UPDATE is
    # emitted right here, BEFORE the race-wrapped flush below, so a concurrent
    # delete would surface its StaleDataError outside the try/except -- an
    # undeclared 500 instead of the 404 this route promises. session.add of a
    # standalone entry touches no relationship and loads nothing, keeping every
    # write inside the wrapped flush.
    session.add(ProgressEntry(item_id=item_id, note=note))
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

    Same discipline as enrich (404 first, snapshot including `updated`, LLM
    outside any transaction, 409 on a concurrent mid-await write, supersede-not-
    delete on history sections, writes only after success, snapshot after flush
    before commit, race-wrapped flush -> 404), but records a progress note
    without touching stage or returning gaps.
    """
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    item_fields = {name: getattr(item, name) for name in _AI_ITEM_FIELDS}
    # Snapshot `updated` before the await for the same optimistic-concurrency
    # check enrich performs (see there).
    original_updated = item.updated
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
    if item.updated != original_updated:
        raise _conflict()
    for key, value in result.sections.items():
        # Supersede-not-delete for history-bearing sections, exactly as enrich.
        if key in HISTORY_SECTIONS:
            value = merge_with_supersede(getattr(item, key), value)
        setattr(item, key, value)
    note = result.progress_note or _UPDATE_NOTE
    # Explicit FK-bearing entry, not item.entries.append(...): the relationship
    # touch would lazy-load + autoflush the dirty item's UPDATE before the
    # race-wrapped flush below -- see the identical comment in `enrich`.
    session.add(ProgressEntry(item_id=item_id, note=note))
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
