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
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.models import MemoryItem, MemoryStage, ProgressEntry
from app.schemas import CaptureRequest, CaptureResponse, LLMStatus, MemoryItemRead
from app.services.llm import LLMNotConfiguredError, LLMUpstreamError, llm_configured
from app.services.memory_ai import capture_draft

router = APIRouter(tags=["ai"])

SessionDep = Annotated[Session, Depends(get_session)]

# Seed note for the append-only log when an item is born from quick capture.
_CAPTURE_NOTE = "AI 快速捕捉"

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
