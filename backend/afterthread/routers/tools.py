"""Tool management + web-installer endpoints (see D21, Phase 5c).

Thin HTTP shell over ``afterthread.services.tools`` (the registry) and
``afterthread.services.tool_builder`` (the installer). This router owns only
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
  pattern), while ``start_install_job`` / ``start_revise_job`` run inline
  BECAUSE they need the running event loop to spawn their background task.

The AI-summary and revise routes (D40) add one wrinkle to that taxonomy. They
deliberately do NOT declare ``tools_not_configured``: with ``TOOLS_DIR`` unset
every name resolves to None, so "the feature is off" and "no such tool" are
already the same 404, and adding a second 503 for it would widen the contract
for nothing. The synchronous regenerate DOES declare the LLM pair (503/502),
whose codes, messages and helpers are IMPORTED from ``routers.ai`` rather than
re-spelled -- one definition of ``llm_not_configured`` / ``llm_upstream_error``,
so a client handling the AI workflows' degradation handles this one identically.
The revise submit declares NEITHER, for the same reason the install submit does
not: it queues a background job, so its LLM failures are job state, not a
response status.
"""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException
from fastapi import Path as PathParam
from starlette.concurrency import run_in_threadpool

from afterthread.routers.ai import (
    _LLM_UNCONFIGURED_RESPONSE,
    _LLM_UPSTREAM_RESPONSE,
    _bad_gateway,
    _service_unavailable,
)
from afterthread.schemas import (
    ToolDeleteResponse,
    ToolDiscardResponse,
    ToolExpectedVersionRequest,
    ToolInstallAccepted,
    ToolInstallRequest,
    ToolJobStatus,
    ToolListResponse,
    ToolReviseRequest,
    ToolSummary,
    ToolSummaryDetail,
    ToolUpdateRequest,
)
from afterthread.services import llm_log, tool_builder, tool_meta
from afterthread.services import tools as tools_service
from afterthread.services.llm import LLMNotConfiguredError, LLMUpstreamError
from afterthread.services.tools import _NAME_RE

router = APIRouter(prefix="/tools", tags=["tools"])

# Path-layer mirror of the registry's package-name regex: a name that could
# never be a package ("..", "a/b", uppercase) is a 422 straight from request
# validation. The pattern is READ FROM the registry's own compiled regex, so
# the two layers can never drift apart.
ToolName = Annotated[str, PathParam(pattern=_NAME_RE.pattern)]
ToolVersionId = Annotated[str, PathParam(pattern=tools_service._VID_RE.pattern)]

# Fixed, config-free error details, following items.py's `_NOT_FOUND` pattern
# (the OpenAPI examples derive from the same constants the handlers raise).
_TOOL_NOT_FOUND = "Tool not found"
# "Tool job", not "Install job" (R7-4): ONE poll endpoint serves installs AND
# revises (D40), so a revise whose id has been evicted was being told its INSTALL
# was missing -- a message naming a workflow the caller never started.
_JOB_NOT_FOUND = "Tool job not found"

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

# Version-specific writes need three machine-distinct conflicts.  The messages
# are for people; clients branch only on these codes.
_VERSION_MISMATCH_CODE = "version_mismatch"
_VERSION_MISMATCH_MESSAGE = "工具版本已變更，請重新整理後再試"  # noqa: RUF001
_JOB_BUSY_CODE = "job_busy"
_JOB_BUSY_MESSAGE = "已有工具任務正在進行中，請等待完成"  # noqa: RUF001
_LINEAGE_UNAVAILABLE_CODE = "lineage_unavailable"
_LINEAGE_UNAVAILABLE_MESSAGE = "前一版已不存在或版本關係已損壞，請刪除整個工具"  # noqa: RUF001
_AI_JOB_IN_PROGRESS_CODE = "ai_job_in_progress"
_AI_JOB_IN_PROGRESS_MESSAGE = "AI 任務進行中，請稍後再試"  # noqa: RUF001


def _conflict_response(
    *conflicts: tuple[str, str, str],
) -> dict[int | str, dict[str, Any]]:
    """One OpenAPI 409 response with machine-distinct named examples."""

    return {
        409: {
            "description": "Version-specific tool operation conflict",
            "content": {
                "application/json": {
                    "examples": {
                        code: {
                            "summary": description,
                            "value": {"detail": {"code": code, "message": message}},
                        }
                        for code, message, description in conflicts
                    }
                }
            },
        }
    }


_AI_ITERATION_CONFLICT_RESPONSE = _conflict_response(
    (_VERSION_MISMATCH_CODE, _VERSION_MISMATCH_MESSAGE, "The requested version is stale"),
    (_JOB_BUSY_CODE, _JOB_BUSY_MESSAGE, "Another tool operation holds the global slot"),
)
_DISCARD_CONFLICT_RESPONSE = _conflict_response(
    (_VERSION_MISMATCH_CODE, _VERSION_MISMATCH_MESSAGE, "The requested version is stale"),
    (_JOB_BUSY_CODE, _JOB_BUSY_MESSAGE, "Another tool operation holds the global slot"),
    (
        _LINEAGE_UNAVAILABLE_CODE,
        _LINEAGE_UNAVAILABLE_MESSAGE,
        "The previous-version lineage cannot be used",
    ),
    (
        _AI_JOB_IN_PROGRESS_CODE,
        _AI_JOB_IN_PROGRESS_MESSAGE,
        "An AI request or inherited tool process holds the shared tools lock",
    ),
)
_DELETE_CONFLICT_RESPONSE = _conflict_response(
    (
        _AI_JOB_IN_PROGRESS_CODE,
        _AI_JOB_IN_PROGRESS_MESSAGE,
        "An AI request or inherited tool process holds the shared tools lock",
    ),
)


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

    ``set_enabled`` returning False collapses every did-not-happen case into one
    404 with a fixed detail: from the caller's view the addressable tool resource
    does not (usably) exist. Those cases are the feature being off, a name that
    resolves to nothing installed (or out of the tools dir, or through an internal
    alias), and a failed publish of the package's ``.afterthread.meta/state.json`` -- something
    that is not a regular file sitting at that name, an unwritable package
    directory, a package replaced between the resolve and the write.

    The MANIFEST is not among them any more, and the difference is visible to a
    caller: since web-v5 P1 the toggle writes ``.afterthread.meta/state.json`` and never opens
    ``tool.json``, so a package whose manifest is a FIFO, oversized or unreadable
    now toggles successfully with a 200 while its row stays ``valid: false``. That
    is the intended behaviour (an operator can switch OFF a broken package, which
    is when they most want to) and it is what backend/README.md documents; a broken
    package is never advertised to the model either way.

    The response is re-read from a fresh registry scan so it reports exactly what
    the next GET would.
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


@router.delete(
    "/{name}",
    response_model=ToolDeleteResponse,
    responses={**_TOOL_NOT_FOUND_RESPONSE, **_DELETE_CONFLICT_RESPONSE},
)
async def delete_installed_tool(name: ToolName) -> ToolDeleteResponse:
    """Delete a tool package (its whole directory).

    The registry's ``delete_tool`` re-validates the name AND resolved-path
    containment under the tools dir before it touches anything (the
    path-traversal / symlink-escape hard-block lives THERE, not in this router),
    and returns None for a missing package -> 404.

    A request or inherited child holding the shared tools lock produces the
    distinct retryable ``ai_job_in_progress`` 409 without renaming or deleting
    anything. Otherwise the 200 response says ``removed`` after physical
    deletion or ``retained`` with the exact cleanup-failure path.
    """
    result = await run_in_threadpool(tools_service.delete_tool, name)
    if result is None:
        raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)
    if result == "ai_job_in_progress":
        raise HTTPException(
            status_code=409,
            detail={
                "code": _AI_JOB_IN_PROGRESS_CODE,
                "message": _AI_JOB_IN_PROGRESS_MESSAGE,
            },
        )
    return ToolDeleteResponse(
        outcome=result.outcome,
        retained_path=str(result.retained_path) if result.retained_path is not None else None,
        retention_reason=result.retention_reason,
    )


@router.delete(
    "/{name}/versions/{vid}",
    response_model=ToolDiscardResponse,
    responses={**_TOOL_NOT_FOUND_RESPONSE, **_DISCARD_CONFLICT_RESPONSE},
)
async def discard_tool_version(name: ToolName, vid: ToolVersionId) -> ToolDiscardResponse:
    """Discard the exact current version named by ``vid``.

    The global slot is taken before the expected-version comparison.  That
    ordering makes the comparison and the subsequent ``current`` publication
    one serialized action rather than another check-then-write race. The response
    reports whether the former version's files were removed or retained, with the
    exact operator cleanup path in the latter case.
    """

    reservation = tool_builder.reserve_sync_operation()
    if reservation is None:
        raise HTTPException(
            status_code=409,
            detail={"code": _JOB_BUSY_CODE, "message": _JOB_BUSY_MESSAGE},
        )
    try:
        resolved = await run_in_threadpool(_existing_package_dir, name)
        if resolved is None:
            raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)
        if resolved.vid != vid:
            raise HTTPException(
                status_code=409,
                detail={"code": _VERSION_MISMATCH_CODE, "message": _VERSION_MISMATCH_MESSAGE},
            )
        outcome = await run_in_threadpool(tools_service.discard_version, resolved)
        if outcome == "lineage_unavailable":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": _LINEAGE_UNAVAILABLE_CODE,
                    "message": _LINEAGE_UNAVAILABLE_MESSAGE,
                },
            )
        if outcome == "ai_job_in_progress":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": _AI_JOB_IN_PROGRESS_CODE,
                    "message": _AI_JOB_IN_PROGRESS_MESSAGE,
                },
            )
        if outcome == "not_found":
            raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)
        return ToolDiscardResponse(
            outcome=outcome.outcome,
            retained_path=(
                str(outcome.retained_path) if outcome.retained_path is not None else None
            ),
            retention_reason=outcome.retention_reason,
        )
    finally:
        tool_builder.release_sync_operation(reservation)


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


# --- AI summary + revise (D40) -----------------------------------------------
#
# Declared BEFORE `GET /jobs/{job_id}` (which sits at the BOTTOM of this module
# for exactly that reason -- see the note there). "jobs" is itself a legal
# package name, so with the job route first `/api/tools/jobs/summary` would match
# IT, with job_id="summary", and a tool genuinely named "jobs" could never have
# its summary read.


def _existing_package_dir(name: str) -> tools_service.Resolved | None:
    """The package's resolved directory, or None if it is not (usably) there.

    One blocking helper for the "does this tool exist?" gate every summary route
    opens with, so each route makes ONE threadpool hop instead of two. It is a
    thin pass-through to ``_resolve_package_dir_no_alias``, which folds the name
    regex, the INTERNAL-alias refusal, the resolved-path containment check, the
    feature-off case and "is anything actually installed there" into its own
    None -- the same helper the registry's own summary mutations use, so a route
    and the service it calls can never disagree about which directory a name
    addresses.
    """
    package_root = tools_service._resolve_package_dir_no_alias(name)
    if package_root is None:
        return None
    resolution = tools_service.resolve_current(package_root)
    return resolution if isinstance(resolution, tools_service.Resolved) else None


def _summary_detail(meta: dict[str, Any] | None, current_vid: str) -> ToolSummaryDetail:
    """Build the response from a sidecar dict; every field degrades to null.

    The sidecar is backend-authored, but it is a plain JSON file sitting inside
    a package the operator is explicitly allowed to hand-edit (see the README's
    manual-editing note) -- so nothing here TRUSTS its types. A hand-written
    ``"llm_log_id": "three"`` must render as a null link, not a 500 on the read
    path, exactly as ``read_tool_meta`` treats an unparseable sidecar as no
    sidecar.

    ``llm_log_id`` is additionally filtered by WHOSE id it is. The AI log's ids
    are a per-process counter and its ring is wiped on restart, while the sidecar
    keeps the integer forever -- so after a restart a stored id resolves to
    whatever interaction now occupies it: a different tool's summary session, or
    a different workflow entirely. The existing ``started_at`` staleness hint on
    the log page cannot help, because the deep link SELECTS the row by that id,
    so the detail and the row agree with each other. Only the writer's process
    identity can answer it, so ``store_summary_meta`` records
    ``llm_log_process`` beside the id and this compares it to
    ``llm_log.process_token()``.

    NULLING the id rather than adding an "is it still valid" boolean, for three
    reasons that all point the same way: ``ToolSummaryDetail`` is already a
    three-field all-nullable shape whose null ``llm_log_id`` means exactly "there
    is no record to link", the FE already renders precisely that (no 查看 AI 日誌
    anchor), and shipping the integer alongside a false flag would hand a client
    an id that resolves -- to the wrong interaction -- and make every consumer
    read two fields to answer one question. A null here is NOT the same as the
    ring having evicted a record: an id from THIS process survives eviction as a
    link that 404s honestly ("that record has aged out"), which is the existing,
    benign case.

    A sidecar with NO token -- every one written before this field existed -- is
    treated as foreign, which is the conservative reading: those ids were minted
    by a process that has since exited by definition of the file outliving it, and
    guessing "current" is the one answer that produces a wrong link. The next
    ``regenerate`` re-stamps both fields together.
    """
    data = meta or {}
    summary = data.get("summary")
    updated_at = data.get("updated_at")
    log_id = data.get("llm_log_id")
    from_this_process = data.get("llm_log_process") == llm_log.process_token()
    return ToolSummaryDetail(
        summary=summary if isinstance(summary, str) else None,
        updated_at=updated_at if isinstance(updated_at, str) else None,
        # `bool` is an `int` subclass; excluding it keeps a stray `true` from
        # rendering as a link to log record 1.
        llm_log_id=(
            log_id
            if from_this_process and isinstance(log_id, int) and not isinstance(log_id, bool)
            else None
        ),
        current_vid=current_vid,
    )


@router.get(
    "/{name}/summary",
    response_model=ToolSummaryDetail,
    responses=_TOOL_NOT_FOUND_RESPONSE,
)
async def get_tool_summary(name: ToolName) -> ToolSummaryDetail:
    """One tool's AI summary.

    A tool with no sidecar is an all-null 200, NOT a 404 (see
    ``ToolSummaryDetail``): the resource being addressed is the tool's summary,
    and "this tool has no summary yet" is a normal state with its own UI. The
    404 is reserved for the tool itself not existing -- which, with TOOLS_DIR
    unset, is also what the whole feature being off looks like, and which an
    internal symlink alias is deliberately folded into (a summary must be read
    from the package it names, never from an aliased one).
    """
    resolved = await run_in_threadpool(_existing_package_dir, name)
    if resolved is None:
        raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)
    return _summary_detail(
        await run_in_threadpool(tools_service.read_tool_meta, resolved.version_root),
        resolved.vid,
    )


@router.post(
    "/{name}/summary/regenerate",
    response_model=ToolSummaryDetail,
    responses={
        **_TOOL_NOT_FOUND_RESPONSE,
        **_AI_ITERATION_CONFLICT_RESPONSE,
        **_LLM_UNCONFIGURED_RESPONSE,
        **_LLM_UPSTREAM_RESPONSE,
    },
)
async def regenerate_tool_summary(
    name: ToolName, payload: ToolExpectedVersionRequest
) -> ToolSummaryDetail:
    """Re-run the summary generation for one tool and return the new sidecar.

    SYNCHRONOUS, unlike the installer: this is one short single-turn generation
    (no tool loop, no shell, ordinary timeout), so it fits a request the way
    捕捉/補齊 do -- and it therefore follows THEIR error contract, declaring and
    raising the shared 503 ``llm_not_configured`` / 502 ``llm_upstream_error``
    from ``routers.ai``. The installer's asynchronous, job-shaped contract stays
    what it is precisely because a builder session cannot fit in a request.

    Before the LLM is touched, a queued/running job is refused because a package
    directory may be swapped underneath us mid-promote (409
    ``job_busy``).  After taking that slot, ``expected_vid`` is compared with the
    resolved current version; a stale caller gets ``version_mismatch`` without an
    LLM request.

    The job gate TAKES a reservation rather than merely asking (R7-3), and the
    difference is what makes it a gate at all: this handler then awaits a full
    LLM round trip, and a bare ``any_job_active()`` read left that whole window
    open -- a revise could be admitted inside it, replace the package, write its
    own sidecar, and have this older generation overwrite it with a summary of
    the package that no longer exists. ``reserve_sync_operation`` decides and
    takes under the SAME lock ``_admit_job`` uses, so the revise is refused for
    the duration instead; the reservation is released in the ``finally`` below on
    every path, success or exception.

    A generation that produced text but STORED nothing (``regenerate_summary``
    -> None: the package vanished mid-request, or the sidecar write was refused)
    is a 404 rather than a 200, so this route can never report a summary that
    the next GET will not find. That is the same fold the PATCH above applies to
    its own failed rewrite.
    """
    reservation = tool_builder.reserve_sync_operation()
    if reservation is None:
        raise HTTPException(
            status_code=409,
            detail={"code": _JOB_BUSY_CODE, "message": _JOB_BUSY_MESSAGE},
        )
    try:
        resolved = await run_in_threadpool(_existing_package_dir, name)
        if resolved is None:
            raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)
        if resolved.vid != payload.expected_vid:
            raise HTTPException(
                status_code=409,
                detail={"code": _VERSION_MISMATCH_CODE, "message": _VERSION_MISMATCH_MESSAGE},
            )
        meta = await tool_meta.regenerate_summary(name, resolved)
    except tool_meta.VersionMismatchError:
        raise HTTPException(
            status_code=409,
            detail={"code": _VERSION_MISMATCH_CODE, "message": _VERSION_MISMATCH_MESSAGE},
        ) from None
    except LLMNotConfiguredError:
        raise _service_unavailable() from None
    except LLMUpstreamError as exc:
        raise _bad_gateway(exc) from None
    finally:
        # Held across the LLM round trip and the sidecar write, released on every
        # exit including the two raises above -- a reservation that outlived its
        # request would wedge the single flight for the life of the process.
        tool_builder.release_sync_operation(reservation)
    if meta is None:
        # The generation ran but nothing was stored: the package vanished under
        # us (a racing delete), or the sidecar write was refused (the ghost
        # guard, a FIFO/symlink swapped in for it, an unwritable directory). The
        # summary in hand exists nowhere on disk and would vanish on the next
        # GET, so answering 200 with it would be a lie -- fold it into the same
        # did-not-happen 404 the PATCH above uses for its own failed rewrite.
        raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)
    return _summary_detail(meta, resolved.vid)


@router.post(
    "/{name}/revise",
    response_model=ToolInstallAccepted,
    status_code=202,
    responses={**_TOOL_NOT_FOUND_RESPONSE, **_AI_ITERATION_CONFLICT_RESPONSE},
)
async def revise_tool(name: ToolName, payload: ToolReviseRequest) -> ToolInstallAccepted:
    """Queue an AI revise job for one installed tool; poll it via `/jobs/{id}`.

    202 + job id, exactly like the installer's submit and for the same reason: a
    revise IS a builder session (minutes of tool rounds and shell tests), so it
    cannot live inside a request. It therefore declares NEITHER 502 NOR 503: an
    LLM that is unconfigured or failing is represented as a FAILED job carrying a
    friendly error and its own AI 日誌 link, so the submit itself has no LLM
    outcome to report -- the same contract split test_ai_contract pins for the
    install submit (which does declare a 503, but for TOOLS_DIR, not the LLM).
    That 503 has no counterpart here: with TOOLS_DIR unset no name resolves, so
    this route's existence gate already answers 404.

    ``start_revise_job`` first reserves the global slot, then resolves the package
    and compares the required ``expected_vid``.  Missing/aliased packages return
    404, a stale version returns ``version_mismatch``, and an occupied slot returns
    ``job_busy``.  Only a matching version is converted into a queued job, so a
    stale submit has neither an LLM charge nor even a pollable job id.
    """
    started = await tool_builder.start_revise_job(name, payload.feedback, payload.expected_vid)
    if started.refusal == "not_found":
        raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)
    if started.refusal == "version_mismatch":
        raise HTTPException(
            status_code=409,
            detail={"code": _VERSION_MISMATCH_CODE, "message": _VERSION_MISMATCH_MESSAGE},
        )
    if started.refusal == "job_busy":
        raise HTTPException(
            status_code=409,
            detail={"code": _JOB_BUSY_CODE, "message": _JOB_BUSY_MESSAGE},
        )
    assert started.job_id is not None
    return ToolInstallAccepted(job_id=started.job_id)


# --- tool job poll -----------------------------------------------------------
#
# LAST on purpose, and it must STAY last. `GET /jobs/{job_id}` and
# `GET /{name}/summary` are both two-segment GETs and FastAPI matches in
# DECLARATION order, so whichever is declared first wins the strings that
# satisfy both. Declaring the summary routes first is safe in BOTH directions:
# a job id is ``uuid4().hex``, so the literal segment "summary" can never be
# one (no job poll is stolen), while "jobs" IS a legal package name, so a tool
# named "jobs" can still serve `/api/tools/jobs/summary` instead of having it
# swallowed as a job poll with job_id="summary".
#
# D40's rename from `/install/{job_id}` MOVED this collision (it used to be a
# tool named "install"); it did not remove it, because any literal first segment
# is a name a package could have. So the ordering stays load-bearing.


@router.get(
    "/jobs/{job_id}",
    response_model=ToolJobStatus,
    responses=_JOB_NOT_FOUND_RESPONSE,
)
async def tool_job_status(job_id: str) -> ToolJobStatus:
    """One install/revise job's state. 404: unknown id, evicted, or a backend
    restart (jobs are process-local and unpersisted -- see tool_builder).

    ONE endpoint for both kinds (D40): they share a job table, a single-flight
    admission and a response shape, so a second endpoint would only give the FE
    two identical pollers to keep in sync. The old `/install/{job_id}` spelling
    is REMOVED rather than aliased -- the FE and the backend ship in the same
    wheel, so there is no version skew for an alias to protect.

    ``llm_log_process`` is stamped HERE rather than stored on the job, and that
    is sound STRUCTURALLY rather than by convention: the job table is in-process
    memory that dies with the log ring and its id counter (a restart 404s every
    job above), so any ``llm_log_id`` a job can still be served with was minted
    in THIS process. The sidecar needs its token written down because the FILE
    outlives the process; a job cannot. Both keep the same both-or-neither rule
    -- no id, no token -- so the pair can never disagree about whether there is a
    link worth vouching for (``ToolJobStatus``, ``tools.store_summary_meta``).
    """
    job = tool_builder.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND)
    token = llm_log.process_token() if job.get("llm_log_id") is not None else None
    return ToolJobStatus.model_validate({**job, "llm_log_process": token})
