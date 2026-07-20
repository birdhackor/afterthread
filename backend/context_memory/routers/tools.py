"""Tool management + web-installer endpoints (see D21, Phase 5c).

Thin HTTP shell over ``context_memory.services.tools`` (the registry) and
``context_memory.services.tool_builder`` (the installer). This router owns only
the HTTP contract:

* the tool NAME is validated at the PATH layer against the same regex the
  registry enforces (an out-of-shape name is a 422 before any handler runs);
  the registry then independently re-validates name + resolved-path containment
  on every mutation, so the path validator is UX, not the security boundary;
* the installer's unavailability (``TOOLS_DIR`` unset) is a 503 with a fixed
  ``{code, message}`` detail, mirroring the ``llm_not_configured`` discipline --
  but under its own ``tools_not_configured`` code, since it is a different
  feature being unconfigured (test_ai_contract enumerates this operation
  separately from the three LLM-degradable workflows);
* every registry call runs via ``run_in_threadpool`` (filesystem scans and
  writes are blocking work that must not sit on the event loop -- the house
  pattern), while ``start_install_job`` runs inline BECAUSE it needs the
  running event loop to spawn its background task.
"""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException
from fastapi import Path as PathParam
from starlette.concurrency import run_in_threadpool

from context_memory.schemas import (
    ToolInstallAccepted,
    ToolInstallJobStatus,
    ToolInstallRequest,
    ToolListResponse,
    ToolSummary,
    ToolUpdateRequest,
)
from context_memory.services import tool_builder
from context_memory.services import tools as tools_service
from context_memory.services.tools import _NAME_RE

router = APIRouter(prefix="/tools", tags=["tools"])

# Path-layer mirror of the registry's package-name regex: a name that could
# never be a package ("..", "a/b", uppercase) is a 422 straight from request
# validation. The pattern is READ FROM the registry's own compiled regex, so
# the two layers can never drift apart.
ToolName = Annotated[str, PathParam(pattern=_NAME_RE.pattern)]

# Fixed, config-free error details, following items.py's `_NOT_FOUND` pattern
# (the OpenAPI examples derive from the same constants the handlers raise).
_TOOL_NOT_FOUND = "Tool not found"
_JOB_NOT_FOUND = "Install job not found"

_TOOLS_NOT_CONFIGURED_CODE = "tools_not_configured"
_TOOLS_NOT_CONFIGURED_MESSAGE = "The tools directory is not configured."

# Only one install may be queued/running at a time (M7); a second submit while
# one is active is a 409 with this fixed {code, message}, mirroring the AI 409
# conflict discipline. The message is USER-facing (rendered by the 工具 page),
# hence zh-TW -- unlike the (operator-facing) tools_not_configured message above.
_INSTALL_IN_PROGRESS_CODE = "install_in_progress"
_INSTALL_IN_PROGRESS_MESSAGE = "已有安裝正在進行中，請等待其完成"  # noqa: RUF001

_TOOL_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {
        "description": _TOOL_NOT_FOUND,
        "content": {"application/json": {"example": {"detail": _TOOL_NOT_FOUND}}},
    }
}

_JOB_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {
        "description": _JOB_NOT_FOUND,
        "content": {"application/json": {"example": {"detail": _JOB_NOT_FOUND}}},
    }
}

_TOOLS_NOT_CONFIGURED_RESPONSE: dict[int | str, dict[str, Any]] = {
    503: {
        "description": "Tool feature is not configured (TOOLS_DIR unset)",
        "content": {
            "application/json": {
                "example": {
                    "detail": {
                        "code": _TOOLS_NOT_CONFIGURED_CODE,
                        "message": _TOOLS_NOT_CONFIGURED_MESSAGE,
                    }
                }
            }
        },
    }
}

_INSTALL_IN_PROGRESS_RESPONSE: dict[int | str, dict[str, Any]] = {
    409: {
        "description": "Another install is already queued or running",
        "content": {
            "application/json": {
                "example": {
                    "detail": {
                        "code": _INSTALL_IN_PROGRESS_CODE,
                        "message": _INSTALL_IN_PROGRESS_MESSAGE,
                    }
                }
            }
        },
    }
}


@router.get("", response_model=ToolListResponse)
async def list_installed_tools() -> ToolListResponse:
    """List every installed tool package (valid or broken), in name order.

    With the feature off (TOOLS_DIR unset) or nothing installed this is an
    empty list, NOT an error: the 工具 page distinguishes "feature off" via the
    install endpoint's 503, while an empty listing is a normal state.
    """
    listed = await run_in_threadpool(tools_service.list_tools)
    return ToolListResponse.model_validate({"tools": listed})


@router.patch("/{name}", response_model=ToolSummary, responses=_TOOL_NOT_FOUND_RESPONSE)
async def update_tool(name: ToolName, payload: ToolUpdateRequest) -> ToolSummary:
    """Toggle a tool's ``enabled`` flag; returns the updated row.

    ``set_enabled`` returning False collapses every did-not-happen case --
    missing package, unreadable/unwritable manifest, feature off -- into one
    404 with a fixed detail: from the caller's view the addressable tool
    resource does not (usably) exist. The response is re-read from a fresh
    registry scan so it reports exactly what the next GET would.
    """
    changed = await run_in_threadpool(tools_service.set_enabled, name, payload.enabled)
    if not changed:
        raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)
    for row in await run_in_threadpool(tools_service.list_tools):
        if row["name"] == name:
            return ToolSummary.model_validate(row)
    # The write landed but the package vanished before the re-scan (a racing
    # delete). The resource is gone, so the honest answer is the same 404.
    raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)


@router.delete("/{name}", status_code=204, responses=_TOOL_NOT_FOUND_RESPONSE)
async def delete_installed_tool(name: ToolName) -> None:
    """Delete a tool package (its whole directory).

    The registry's ``delete_tool`` re-validates the name AND resolved-path
    containment under the tools dir before its rmtree (the path-traversal /
    symlink-escape hard-block lives THERE, not in this router), and returns
    False for a missing package -> 404.
    """
    removed = await run_in_threadpool(tools_service.delete_tool, name)
    if not removed:
        raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)


@router.post(
    "/install",
    response_model=ToolInstallAccepted,
    status_code=202,
    responses={**_TOOLS_NOT_CONFIGURED_RESPONSE, **_INSTALL_IN_PROGRESS_RESPONSE},
)
async def install_tool(payload: ToolInstallRequest) -> ToolInstallAccepted:
    """Queue a web-installer job; poll its state via the endpoint below.

    202 + job id: the build is a long LLM session (minutes), far past any sane
    request timeout, so it runs as a background task. The 503 gate mirrors
    ``llm_not_configured``'s shape under its own code -- checked HERE, before a
    job is created, so a misconfigured deployment fails the submit itself
    rather than minting a job doomed to fail asynchronously. The LLM being
    unconfigured is deliberately NOT pre-checked: that state is already
    represented as a failed job with a friendly error (and its own AI 日誌
    record), and duplicating the gate here would just create two sources of
    truth for it.

    409 when an install is already active: ``start_install_job`` admits only ONE
    queued/running install at a time (M7) and returns None when one is in
    flight, which maps to a fixed ``install_in_progress`` conflict here.
    """
    if tools_service.tools_dir() is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": _TOOLS_NOT_CONFIGURED_CODE,
                "message": _TOOLS_NOT_CONFIGURED_MESSAGE,
            },
        )
    # The optional secret pair (D36) is threaded straight through to the builder;
    # it is validated (both-or-neither + env-var name) by ToolInstallRequest, and
    # secret_value never enters the LLM conversation, any job/poll response, or a
    # log line (see tool_builder / llm_log redaction).
    job_id = tool_builder.start_install_job(
        str(payload.openapi_url),
        payload.instructions,
        secret_name=payload.secret_name,
        secret_value=payload.secret_value,
    )
    if job_id is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": _INSTALL_IN_PROGRESS_CODE,
                "message": _INSTALL_IN_PROGRESS_MESSAGE,
            },
        )
    return ToolInstallAccepted(job_id=job_id)


@router.get(
    "/install/{job_id}",
    response_model=ToolInstallJobStatus,
    responses=_JOB_NOT_FOUND_RESPONSE,
)
async def install_job_status(job_id: str) -> ToolInstallJobStatus:
    """One install job's state. 404: unknown id, evicted, or a backend restart
    (jobs are process-local and unpersisted -- see tool_builder)."""
    job = tool_builder.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND)
    return ToolInstallJobStatus.model_validate(job)
