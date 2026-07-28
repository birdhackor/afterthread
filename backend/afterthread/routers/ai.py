"""AI-assisted memory workflows: LLM status and quick capture.

These endpoints wrap ``afterthread.services.memory_ai``. The methodology rules
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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import Session, col
from starlette.concurrency import run_in_threadpool

from afterthread.config import get_settings
from afterthread.db import get_session
from afterthread.models import MemoryItem, MemoryStage, MemoryStatus, ProgressEntry, utcnow
from afterthread.routers.items import _NOT_FOUND, _NOT_FOUND_RESPONSE, ItemId
from afterthread.schemas import (
    AssistUpdateRequest,
    AssistUpdateResponse,
    CaptureRequest,
    CaptureResponse,
    EnrichRequest,
    EnrichResponse,
    LlmLogDetail,
    LlmLogListResponse,
    LLMStatus,
    LLMTokenRatio,
    MemoryItemRead,
)
from afterthread.services import llm_log, token_budget, tools
from afterthread.services.llm import (
    _UPSTREAM_REASON,
    LLMNotConfiguredError,
    LLMUpstreamError,
    llm_configured,
    normalized_model,
)
from afterthread.services.memory_ai import (
    _PER_SECTION_CAP,
    HISTORY_SECTIONS,
    SECTION_FIELD_ORDER,
    EnrichResult,
    UpdateResult,
    _truncate_to,
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
# prompt: the metadata header (title/status/stage/tags, rendered in full by the
# budgeted serializer) plus every whitelisted section.
_AI_ITEM_FIELDS: tuple[str, ...] = ("title", "status", "stage", "tags", *SECTION_FIELD_ORDER)

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
    # str(exc) is safe by construction (see afterthread.services.llm): an exception
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


@asynccontextmanager
async def _advertised_tools_lock() -> AsyncIterator[None]:
    """Hold the shared tools lock across one complete tool-advertising request.

    Acquisition runs off-loop because a destroyer that won the race may hold the
    exclusive side briefly. The synchronous binding lets ``enabled_llm_tools``
    capture this exact descriptor into every handler. The ``finally`` covers
    normal output, LLM/config errors, validation errors, and request
    cancellation; a subprocess has its own inherited reference if it outlives
    the backend-side close.
    """

    fd = await run_in_threadpool(tools.acquire_shared_tools_lock)
    try:
        with tools.bind_request_tools_lock(fd):
            yield
    finally:
        await run_in_threadpool(tools.release_tools_lock, fd)


def _conditional_update(
    session: Session, item_id: int, snapshot_updated: datetime, values: dict[str, Any]
) -> int:
    """Apply ``values`` to the item via ONE UPDATE guarded on ``snapshot_updated``.

    This is the optimistic-concurrency check closed BY CONSTRUCTION: the WHERE
    pins the item's pre-await ``updated``, so a PATCH committing anytime between
    the snapshot and this statement moves ``updated`` and the row no longer
    matches -- there is no separate compare-then-flush step a competing write
    could slip through and be silently overwritten. Returns the affected row
    count: 1 when the guard matched (the write landed and
    ``synchronize_session="evaluate"`` folded ``values`` back onto the in-session
    item, so a response snapshot needs no re-read), 0 when a concurrent writer
    moved ``updated`` or deleted the row. ``col(...)`` yields real column
    expressions for the typed WHERE; ``exec`` returns a ``CursorResult`` for a
    DML statement, whose ``rowcount`` is the number of rows the guard matched.
    """
    result = session.exec(
        update(MemoryItem)
        .where(col(MemoryItem.id) == item_id, col(MemoryItem.updated) == snapshot_updated)
        .values(**values),
        execution_options={"synchronize_session": "evaluate"},
    )
    return result.rowcount


@router.get("/llm/status", response_model=LLMStatus)
def llm_status() -> LLMStatus:
    """Report whether an LLM endpoint is configured, the model name, and the
    char<->token ratio estimator's state.

    Never returns the base URL or API key -- only the boolean, the non-secret
    model name (null when unconfigured), and the ratio snapshot. The model is run
    through the same `normalized_model` helper `generate_json` uses for the actual
    request, so a whitespace-padded override is never echoed back padded while the
    real call underneath sends the trimmed value. ``token_ratio`` is additive
    observability (P4): the estimator's window size and learned ratio (see
    afterthread.services.token_budget), carrying no config value.
    """
    settings = get_settings()
    configured = llm_configured()
    model = normalized_model(settings) if configured else None
    return LLMStatus(
        configured=configured,
        model=model,
        token_ratio=LLMTokenRatio.model_validate(token_budget.snapshot()),
    )


# The read-only LLM interaction log (D09). Both endpoints stay in the /llm
# namespace and deliberately declare NO 502/503: they never call the LLM, they
# only read the in-memory ring, so both stay OUT of the two exact sets
# test_ai_contract pins -- which is the claim this comment makes, and the only
# one it is entitled to. Those sets are no longer "exactly three AI operations":
# D40's synchronous tool-summary regenerate is a FOURTH request-time LLM call
# and joins BOTH (it raises the shared llm_not_configured / llm_upstream_error
# defined here), and the installer's submit joins the 503 set alone under its own
# tools_not_configured code. Adjusting either endpoint below to restore a
# three-operation contract would strip declarations those operations genuinely
# need. The detail endpoint can 404, declared below so generated clients know it.
_LLM_LOG_NOT_FOUND = "LLM log not found"

_LLM_LOG_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {
        "description": "No LLM interaction log with that id",
        "content": {"application/json": {"example": {"detail": _LLM_LOG_NOT_FOUND}}},
    }
}


@router.get("/llm/logs", response_model=LlmLogListResponse)
def llm_logs(limit: Annotated[int, Query(ge=1, le=500)] = 50) -> LlmLogListResponse:
    """List recent LLM interaction summaries, newest first, WITHOUT bodies.

    Each summary carries only non-secret scalars (workflow, model name, timing,
    outcome, token usage) plus the attempt COUNT; the full prompt/response
    bodies load lazily via the by-id endpoint below. ``limit`` is bounded to
    [1, 500] at the query layer so an oversized page cannot be requested. The
    ring is process-wide and dies with the process, so an empty list is the
    normal state right after a restart.

    That restart is also why the page needs ``process_token`` (see
    ``LlmLogListResponse``): the ids here are a per-process counter, and a client
    holding an OLDER one -- a ``?log=<id>`` deep link built from a cached job
    response -- must be able to find out that its id was re-issued rather than
    have this page open whatever now answers to the number.
    """
    return LlmLogListResponse.model_validate(
        {"logs": llm_log.list_summaries(limit), "process_token": llm_log.process_token()}
    )


@router.get(
    "/llm/logs/{log_id}",
    response_model=LlmLogDetail,
    responses=_LLM_LOG_NOT_FOUND_RESPONSE,
)
def llm_log_detail(log_id: int) -> LlmLogDetail:
    """Return one full LLM interaction record, attempt bodies included.

    404 (fixed, config-free ``{"detail": "LLM log not found"}``) when no record
    with that id is in the ring -- it was evicted past ``llm_log_max_entries``,
    the interaction is still in flight (recorded only on finish), or the id
    never existed. The bodies here can run to tens of KB per attempt (the full
    schema-injected prompt and the model's reply), which is why they are behind
    this by-id fetch rather than in the list above.
    """
    record = llm_log.get_record(log_id)
    if record is None:
        raise HTTPException(status_code=404, detail=_LLM_LOG_NOT_FOUND)
    return LlmLogDetail.model_validate(record)


def _capture_persist(session: Session, item: MemoryItem) -> MemoryItemRead:
    """Persist the new capture item and snapshot the response (sync, off-loop).

    The DB transaction segment: flush to assign id/DB defaults, snapshot the
    response BEFORE commit (no post-commit session.refresh), then commit --
    mirroring items.create_item. Run via run_in_threadpool so this blocking
    SQLite work does not sit on the event loop.
    """
    session.add(item)
    session.flush()
    result = MemoryItemRead.model_validate(item)
    session.commit()
    return result


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
    -- no partial rows -- because no write has happened yet. The blocking write
    itself runs in a threadpool (see _capture_persist), off the event loop.
    """
    async with _advertised_tools_lock():
        try:
            draft = await capture_draft(payload.raw_text)
        except LLMNotConfiguredError as exc:
            raise _service_unavailable() from exc
        except LLMUpstreamError as exc:
            raise _bad_gateway(exc) from exc

    # Building the ORM object is pure in-memory work (no SQL until flush), so it
    # stays on the loop; only the flush/commit segment is handed to the threadpool.
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
        # CaptureDraft has no open_questions field of its own; draft.questions
        # (the model's follow-up questions -- already server-capped at 3
        # items, each up to _PER_SECTION_CAP chars, by CaptureDraft._sanitize)
        # is the SOLE source for this section, so methodology.md's "open
        # questions" minimum durable quick-capture field is actually
        # persisted, not only echoed in the response below. "\n".join of an
        # empty list is "", so a questionless draft leaves this at its
        # ordinary default. Bullet-joining up to 3 items each up to
        # _PER_SECTION_CAP chars can itself run to ~60008 chars -- past the
        # per-section bound every OTHER section respects (enforced inside
        # CaptureDraft._sanitize) and past what a later client PATCH of this
        # same field would accept (schemas._CRUD_SECTION_MAX, the same
        # 20000) -- so the joined string is capped again here, sharing
        # _PER_SECTION_CAP and the truncation-marker convention (_truncate_to)
        # rather than hardcoding a second copy of the bound. The response's
        # `questions` list below is untouched: it is already per-item
        # bounded, not rejoined into one blob.
        open_questions=_truncate_to(
            "\n".join(f"- {question}" for question in draft.questions), _PER_SECTION_CAP
        ),
        next_actions=draft.next_actions,
        recovery_keywords=draft.recovery_keywords,
        recovery_people=draft.recovery_people,
        recovery_files=draft.recovery_files,
        resume_trigger=draft.resume_trigger,
    )
    item.entries.append(ProgressEntry(note=_CAPTURE_NOTE))  # ty: ignore[missing-argument]
    result = await run_in_threadpool(_capture_persist, session, item)
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


def _snapshot_item_for_ai(session: Session, item_id: int) -> tuple[dict[str, Any], datetime]:
    """Pre-await read segment shared by enrich and assist-update (sync, off-loop).

    404 first, snapshot the prompt fields plus the `updated` timestamp the
    optimistic-concurrency guard pins, then release the read transaction so the
    LLM call that follows holds no DB transaction open. Runs via run_in_threadpool
    so this blocking read never sits on the event loop; a 404 raised here
    propagates out of the awaited call to FastAPI unchanged.
    """
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    item_fields = {name: getattr(item, name) for name in _AI_ITEM_FIELDS}
    # Snapshot `updated` BEFORE the await so a concurrent write during the LLM
    # call can be detected by the conditional UPDATE's guard (below).
    original_updated = item.updated
    # Release the read transaction so the LLM call holds no DB transaction open.
    session.rollback()
    return item_fields, original_updated


def _enrich_persist(
    session: Session, item_id: int, original_updated: datetime, result: EnrichResult
) -> MemoryItemRead:
    """Post-await write segment for enrich (sync, off-loop).

    Re-fetch (a mid-await delete -> 404), merge the returned sections (history
    sections superseded, never overwritten; a complete checklist flips stage to
    full and graduates a still-capturing item to active), then land everything
    through the SINGLE guarded conditional UPDATE (rowcount 0 -> 404 or 409),
    append the FK-bearing progress entry in a race-wrapped flush, snapshot the
    response before commit, and commit. Identical logic to the pre-threadpool
    handler -- only relocated here so it runs off the event loop.
    """
    # Re-fetch: the item may have been deleted during the await. This is also the
    # merge base for history sections; it is NOT mutated, so the item stays clean
    # and the conditional UPDATE below is the only write (no autoflush ahead of it).
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    values: dict[str, Any] = {}
    for key, section_value in result.sections.items():
        # Supersede-not-delete for the history-bearing sections: merge losslessly
        # so a prior decision/rationale is never overwritten (see
        # memory_ai.merge_with_supersede). Other sections replace wholesale.
        if key in HISTORY_SECTIONS:
            section_value = merge_with_supersede(getattr(item, key), section_value)
        values[key] = section_value
    if result.checklist_complete:
        values["stage"] = MemoryStage.full
        # A completed checklist means the item is materially recoverable, so an
        # item still in a capture status graduates to active. Terminal and
        # explicitly-parked/waiting statuses are deliberately left untouched.
        if item.status in (MemoryStatus.capture_quick, MemoryStatus.needs_enrichment):
            values["status"] = MemoryStatus.active
    values["updated"] = utcnow()

    # Optimistic concurrency closed by construction (see _conditional_update): a
    # single guarded UPDATE, so a PATCH committing after the snapshot cannot be
    # silently overwritten.
    if _conditional_update(session, item_id, original_updated, values) == 0:
        session.rollback()
        # rowcount 0 is a concurrent DELETE (row gone -> 404, this route's
        # delete-during-await contract) or a concurrent UPDATE (row present,
        # `updated` moved -> 409). The rollback expired the identity map, so this
        # re-fetch genuinely re-queries to tell the two apart.
        if session.get(MemoryItem, item_id) is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        raise _conflict()

    note = result.progress_note or _ENRICH_NOTE
    # Append via an explicit, FK-bearing ProgressEntry -- mirroring
    # items.add_progress -- NOT via item.entries.append(...), whose relationship
    # touch would lazy-load `entries` and could autoflush outside the wrapped
    # flush. `item` is clean (the conditional UPDATE synced it in place), so this
    # flush emits only the entry INSERT; a delete landing in the remaining window
    # trips the FK -> IntegrityError -> 404.
    session.add(ProgressEntry(item_id=item_id, note=note))
    try:
        session.flush()
    except (StaleDataError, IntegrityError) as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from exc
    result_read = MemoryItemRead.model_validate(item)
    session.commit()
    return result_read


@router.post(
    "/items/{item_id}/enrich",
    response_model=EnrichResponse,
    responses=_AI_BY_ID_RESPONSES,
)
async def enrich(item_id: ItemId, payload: EnrichRequest, session: SessionDep) -> EnrichResponse:
    """Full-enrich an item: merge whitelisted section updates and append progress.

    Order (constraint): 404 first, then snapshot the item fields (and its
    `updated` timestamp), then run the LLM strictly OUTSIDE any transaction (the
    read transaction is released before the await). Writes then land through a
    SINGLE conditional UPDATE guarded on the pre-await `updated`: merge the
    returned sections (untouched fields stay as they were; history-bearing
    sections are superseded, never overwritten) and, on a complete checklist,
    flip stage to full and graduate a still-capturing item
    (capture-quick/needs-enrichment) to active. If that UPDATE matches no row a
    writer touched the item mid-await and NOTHING is written -- a concurrent
    UPDATE (`updated` moved, row present) is 409, a concurrent DELETE (row gone)
    is 404. Closing the check with the write (rather than comparing `updated`
    then flushing) leaves no window a competing PATCH could slip through. On a
    match the progress entry is appended and the flush is race-wrapped so a
    delete landing in the remaining window still resolves to 404, not a 500. The
    response snapshot is built (after flush, before commit) from the values just
    written -- not a re-read. A 503/502/409 (or an item deleted mid-await) leaves
    the row unchanged. The two blocking DB segments run in a threadpool (see
    _snapshot_item_for_ai / _enrich_persist) so they never sit on the event loop;
    the session created by the dependency is used sequentially, never concurrently.
    """
    item_fields, original_updated = await run_in_threadpool(_snapshot_item_for_ai, session, item_id)

    async with _advertised_tools_lock():
        try:
            result = await enrich_item(item_fields, payload.additional_context)
        except LLMNotConfiguredError as exc:
            raise _service_unavailable() from exc
        except LLMUpstreamError as exc:
            raise _bad_gateway(exc) from exc

    result_read = await run_in_threadpool(
        _enrich_persist, session, item_id, original_updated, result
    )
    return EnrichResponse(item=result_read, gaps=result.remaining_gaps)


def _assist_persist(
    session: Session, item_id: int, original_updated: datetime, result: UpdateResult
) -> MemoryItemRead:
    """Post-await write segment for assist-update (sync, off-loop).

    Same race-closing discipline as _enrich_persist (re-fetch -> 404, merge with
    supersede-not-delete on history sections, ONE guarded conditional UPDATE ->
    404/409, race-wrapped progress insert, snapshot before commit), but records a
    progress note without touching stage or returning gaps. Identical logic to the
    pre-threadpool handler -- only relocated here so it runs off the event loop.
    """
    # Re-fetch for the merge base + a possible mid-await delete; not mutated, so
    # `item` stays clean and the conditional UPDATE is the sole write.
    item = session.get(MemoryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    values: dict[str, Any] = {}
    for key, section_value in result.sections.items():
        # Supersede-not-delete for history-bearing sections, exactly as enrich.
        if key in HISTORY_SECTIONS:
            section_value = merge_with_supersede(getattr(item, key), section_value)
        values[key] = section_value
    values["updated"] = utcnow()

    # Same race-closing guarded UPDATE as enrich (see _conditional_update): a
    # mid-await PATCH's value survives (rowcount 0 -> 409, or 404 if the row was
    # deleted) with no check-then-write gap.
    if _conditional_update(session, item_id, original_updated, values) == 0:
        session.rollback()
        if session.get(MemoryItem, item_id) is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        raise _conflict()

    note = result.progress_note or _UPDATE_NOTE
    # Explicit FK-bearing entry, not item.entries.append(...); `item` is clean
    # after the synced UPDATE so this flush emits only the entry INSERT (see the
    # identical reasoning in `enrich`).
    session.add(ProgressEntry(item_id=item_id, note=note))
    try:
        session.flush()
    except (StaleDataError, IntegrityError) as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from exc
    result_read = MemoryItemRead.model_validate(item)
    session.commit()
    return result_read


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
    outside any transaction, then a SINGLE conditional UPDATE guarded on the
    pre-await `updated` that resolves a mid-await writer to 409 or a delete to
    404, supersede-not-delete on history sections, writes only on a matching
    UPDATE, a response snapshot built from the written values after a race-wrapped
    flush before commit), but records a progress note without touching stage or
    returning gaps. The two blocking DB segments run in a threadpool (see
    _snapshot_item_for_ai / _assist_persist), off the event loop, using the
    dependency's session sequentially.
    """
    item_fields, original_updated = await run_in_threadpool(_snapshot_item_for_ai, session, item_id)

    async with _advertised_tools_lock():
        try:
            result = await assist_update(item_fields, payload.note)
        except LLMNotConfiguredError as exc:
            raise _service_unavailable() from exc
        except LLMUpstreamError as exc:
            raise _bad_gateway(exc) from exc

    result_read = await run_in_threadpool(
        _assist_persist, session, item_id, original_updated, result
    )
    return AssistUpdateResponse(item=result_read)
