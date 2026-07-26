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

from pathlib import Path
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
    ToolInstallAccepted,
    ToolInstallRequest,
    ToolJobStatus,
    ToolListResponse,
    ToolReviseRequest,
    ToolSummary,
    ToolSummaryDetail,
    ToolSummaryStatusUpdate,
    ToolUpdateRequest,
)
from afterthread.services import tool_builder, tool_meta
from afterthread.services import tools as tools_service
from afterthread.services.llm import LLMNotConfiguredError, LLMUpstreamError
from afterthread.services.tools import _NAME_RE, _SUMMARY_STATUSES

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

# The three summary conflicts (D40). All USER-facing (rendered by the 工具
# page), hence zh-TW, and all distinct CODES so the FE can branch without
# string-matching a message:
#
# * `summary_missing` -- PATCH tried to 定版 a tool that has no sidecar yet.
#   Deliberately a 409, not a 404: the TOOL exists, there is just nothing to
#   freeze, and the fix (產生總結) is a different action from "wrong tool";
# * `tool_finalized` -- a regenerate or a revise on a 已定版 package. Freezing
#   AI iteration is exactly what 定版 means, so the answer is a conflict telling
#   the user to unfreeze first, never a silent regeneration or revision;
# * `tool_job_in_progress` -- an install/revise job is queued or running. A
#   promote MOVES a package directory into place, so touching a package's
#   sidecar across that swap races a directory being replaced. A NEW code
#   rather than reusing `install_in_progress`: that one is the install FORM's
#   conflict with its own pinned FE branch, and this one can be raised by a job
#   the user did not start from this control.
_SUMMARY_MISSING_CODE = "summary_missing"
_SUMMARY_MISSING_MESSAGE = "尚無總結可定版"

_TOOL_FINALIZED_CODE = "tool_finalized"
_TOOL_FINALIZED_MESSAGE = "總結已定版，請先解除定版再重新產生"  # noqa: RUF001

_TOOL_JOB_IN_PROGRESS_CODE = "tool_job_in_progress"
_TOOL_JOB_IN_PROGRESS_MESSAGE = "已有工具任務正在進行中，請等待完成"  # noqa: RUF001


def _conflict_response(
    code: str, message: str, description: str
) -> dict[int | str, dict[str, Any]]:
    """One 409 declaration, built FROM the constants the handler raises."""
    return {
        409: {
            "description": description,
            "content": {
                "application/json": {"example": {"detail": {"code": code, "message": message}}}
            },
        }
    }


_SUMMARY_MISSING_RESPONSE = _conflict_response(
    _SUMMARY_MISSING_CODE, _SUMMARY_MISSING_MESSAGE, "The tool has no summary to finalize"
)

# ONE 409 slot in OpenAPI, two runtime codes: both AI-iteration operations
# (regenerate and revise) can conflict either way, and both examples cannot
# occupy the same status key -- so the declared example names the finalized case
# and the description names both. Shared by the two routes because it is
# literally the same pair of answers: "this tool is frozen" and "a tool job is
# already running".
_AI_ITERATION_CONFLICT_RESPONSE = _conflict_response(
    _TOOL_FINALIZED_CODE,
    _TOOL_FINALIZED_MESSAGE,
    "The summary is finalized (tool_finalized), or a tool job is running (tool_job_in_progress)",
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


# --- AI summary + revise (D40) -----------------------------------------------
#
# Declared BEFORE `GET /jobs/{job_id}` (which sits at the BOTTOM of this module
# for exactly that reason -- see the note there). "jobs" is itself a legal
# package name, so with the job route first `/api/tools/jobs/summary` would match
# IT, with job_id="summary", and a tool genuinely named "jobs" could never have
# its summary read.


def _existing_package_dir(name: str) -> Path | None:
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
    return tools_service._resolve_package_dir_no_alias(name)


def _summary_detail(meta: dict[str, Any] | None) -> ToolSummaryDetail:
    """Build the response from a sidecar dict; every field degrades to null.

    The sidecar is backend-authored, but it is a plain JSON file sitting inside
    a package the operator is explicitly allowed to hand-edit (see the README's
    manual-editing note) -- so nothing here TRUSTS its types. A hand-written
    ``"llm_log_id": "three"`` must render as a null link, not a 500 on the read
    path, exactly as ``read_tool_meta`` treats an unparseable sidecar as no
    sidecar. ``status`` is filtered against the registry's own vocabulary so an
    unknown value can never reach the FE's badge (or, worse, read as 定版).
    """
    data = meta or {}
    summary = data.get("summary")
    status = data.get("status")
    updated_at = data.get("updated_at")
    log_id = data.get("llm_log_id")
    return ToolSummaryDetail(
        summary=summary if isinstance(summary, str) else None,
        status=status if isinstance(status, str) and status in _SUMMARY_STATUSES else None,
        updated_at=updated_at if isinstance(updated_at, str) else None,
        # `bool` is an `int` subclass; excluding it keeps a stray `true` from
        # rendering as a link to log record 1.
        llm_log_id=log_id if isinstance(log_id, int) and not isinstance(log_id, bool) else None,
    )


@router.get(
    "/{name}/summary",
    response_model=ToolSummaryDetail,
    responses=_TOOL_NOT_FOUND_RESPONSE,
)
async def get_tool_summary(name: ToolName) -> ToolSummaryDetail:
    """One tool's AI summary and its draft/final status.

    A tool with no sidecar is an all-null 200, NOT a 404 (see
    ``ToolSummaryDetail``): the resource being addressed is the tool's summary,
    and "this tool has no summary yet" is a normal state with its own UI. The
    404 is reserved for the tool itself not existing -- which, with TOOLS_DIR
    unset, is also what the whole feature being off looks like, and which an
    internal symlink alias is deliberately folded into (a summary must be read
    from the package it names, never from an aliased one).
    """
    directory = await run_in_threadpool(_existing_package_dir, name)
    if directory is None:
        raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)
    return _summary_detail(await run_in_threadpool(tools_service.read_tool_meta, directory))


@router.patch(
    "/{name}/summary",
    response_model=ToolSummaryDetail,
    responses={**_TOOL_NOT_FOUND_RESPONSE, **_SUMMARY_MISSING_RESPONSE},
)
async def update_tool_summary_status(
    name: ToolName, payload: ToolSummaryStatusUpdate
) -> ToolSummaryDetail:
    """定版 / 解除定版 one tool's summary; returns the updated sidecar.

    The three registry outcomes map straight onto the three answers:
    ``not_found`` (bad name, missing package, an internal symlink alias, feature
    off, or the rewrite failed) -> the same fixed 404 every other tool route
    uses; ``no_meta`` -> 409 ``summary_missing``, since the tool exists but has
    nothing to freeze; ``ok`` -> the sidecar re-read from disk, so the response
    reports exactly what the next GET would rather than an optimistic echo
    (mirroring ``update_tool``'s re-scan discipline, including its
    racing-delete 404).

    "Nothing to freeze" covers BOTH no sidecar and a sidecar whose summary is
    absent/empty -- one 409, because they are the same answer to the user.
    Finalizing an empty summary is not a harmless no-op: it then blocks
    ``regenerate`` with ``tool_finalized``, so the tool ends up frozen around
    text that was never written. 解除定版 (``status: "draft"``) is never gated
    this way; the escape hatch has to work unconditionally.
    """
    outcome = await run_in_threadpool(tools_service.set_summary_status, name, payload.status)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)
    if outcome == "no_meta":
        raise HTTPException(
            status_code=409,
            detail={"code": _SUMMARY_MISSING_CODE, "message": _SUMMARY_MISSING_MESSAGE},
        )
    directory = await run_in_threadpool(_existing_package_dir, name)
    if directory is None:
        # The write landed but the package vanished before the re-read (a racing
        # delete). The resource is gone, so the honest answer is the same 404.
        raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)
    return _summary_detail(await run_in_threadpool(tools_service.read_tool_meta, directory))


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
async def regenerate_tool_summary(name: ToolName) -> ToolSummaryDetail:
    """Re-run the summary generation for one tool and return the new sidecar.

    SYNCHRONOUS, unlike the installer: this is one short single-turn generation
    (no tool loop, no shell, ordinary timeout), so it fits a request the way
    捕捉/補齊 do -- and it therefore follows THEIR error contract, declaring and
    raising the shared 503 ``llm_not_configured`` / 502 ``llm_upstream_error``
    from ``routers.ai``. The installer's asynchronous, job-shaped contract stays
    what it is precisely because a builder session cannot fit in a request.

    Two gates run before the LLM is touched, both cheap and both refusing rather
    than doing something surprising: a 已定版 summary is frozen by definition
    (409 ``tool_finalized``), and a queued/running job means a package directory
    may be swapped underneath us mid-promote (409 ``tool_job_in_progress``). The
    job gate is a check-then-act against a job that could start a microsecond
    later -- accepted, exactly as the installer accepts its own promote races
    (D21/D40): this is a single-user local tool, and the loser is one summary,
    never the package.

    The finalize gate is NOT left at check-then-act, because there the loser
    would be the frozen summary itself: a PATCH landing while the generation
    awaits the LLM used to see its text overwritten anyway. ``_store_meta``
    re-checks at write time and answers ``StoreRefusal.FINALIZED``, which maps
    HERE to the SAME 409 ``tool_finalized`` -- one refusal, one code, whichever
    side of the await the user's 定版 arrived on.

    A generation that produced text but STORED nothing (``regenerate_summary``
    -> None: the package vanished mid-request, or the sidecar write was refused)
    is a 404 rather than a 200, so this route can never report a summary that
    the next GET will not find. That is the same fold the PATCH above applies to
    its own failed rewrite.
    """
    directory = await run_in_threadpool(_existing_package_dir, name)
    if directory is None:
        raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)
    if await run_in_threadpool(tools_service.summary_status, directory) == "final":
        raise HTTPException(
            status_code=409,
            detail={"code": _TOOL_FINALIZED_CODE, "message": _TOOL_FINALIZED_MESSAGE},
        )
    if tool_builder.any_job_active():
        raise HTTPException(
            status_code=409,
            detail={"code": _TOOL_JOB_IN_PROGRESS_CODE, "message": _TOOL_JOB_IN_PROGRESS_MESSAGE},
        )
    try:
        meta = await tool_meta.regenerate_summary(name)
    except LLMNotConfiguredError:
        raise _service_unavailable() from None
    except LLMUpstreamError as exc:
        raise _bad_gateway(exc) from None
    if meta is tool_meta.StoreRefusal.FINALIZED:
        # 定版 landed while we were awaiting the LLM. The generated text was
        # deliberately NOT written (see _store_meta), so the answer is the same
        # conflict the up-front gate gives -- not the 404 below, which would tell
        # the user their tool disappeared.
        raise HTTPException(
            status_code=409,
            detail={"code": _TOOL_FINALIZED_CODE, "message": _TOOL_FINALIZED_MESSAGE},
        )
    if meta is None:
        # The generation ran but nothing was stored: the package vanished under
        # us (a racing delete), or the sidecar write was refused (the ghost
        # guard, a FIFO/symlink swapped in for it, an unwritable directory). The
        # summary in hand exists nowhere on disk and would vanish on the next
        # GET, so answering 200 with it would be a lie -- fold it into the same
        # did-not-happen 404 the PATCH above uses for its own failed rewrite.
        raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)
    return _summary_detail(meta)


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

    Three refusals, in the order that spends the least:

    * 404 -- the tool does not exist, resolved through the SAME helper every
      summary route uses, so an internal symlink alias is refused here too (a
      revise addressed through an alias would REPLACE the real package);
    * 409 ``tool_finalized`` -- 定版 freezes AI iteration on the tool by
      definition, so a revise is refused until it is unfrozen, exactly as a
      regenerate is. ``run_revise`` re-checks this itself: this gate is a
      microsecond stale by the time the job starts;
    * 409 ``tool_job_in_progress`` -- taken from ``start_revise_job`` returning
      None rather than from an ``any_job_active()`` pre-check. Both express the
      same rule (one tool job at a time), but the None return decides it INSIDE
      the admission lock, so two simultaneous submits cannot both be admitted.
    """
    directory = await run_in_threadpool(_existing_package_dir, name)
    if directory is None:
        raise HTTPException(status_code=404, detail=_TOOL_NOT_FOUND)
    if await run_in_threadpool(tools_service.summary_status, directory) == "final":
        raise HTTPException(
            status_code=409,
            detail={"code": _TOOL_FINALIZED_CODE, "message": _TOOL_FINALIZED_MESSAGE},
        )
    job_id = tool_builder.start_revise_job(name, payload.feedback)
    if job_id is None:
        raise HTTPException(
            status_code=409,
            detail={"code": _TOOL_JOB_IN_PROGRESS_CODE, "message": _TOOL_JOB_IN_PROGRESS_MESSAGE},
        )
    return ToolInstallAccepted(job_id=job_id)


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
    """
    job = tool_builder.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND)
    return ToolJobStatus.model_validate(job)
