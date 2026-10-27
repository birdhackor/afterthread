"""Tool run records (web-v6 §6, §7): counts, purge, and one record by id.

A prefix of its own rather than routes under ``/api/tools``: every literal first
segment under ``/api/tools/`` is a name a package could have (the reason the
job poll there must stay declared last), so an operation that is not about one
named package lives where no package name can reach it. The per-tool list is
the one exception, because it IS about one package: ``GET /api/tools/{name}/runs``.

The routes are plain ``def``, run in the threadpool by FastAPI, like the items
routes: a database read plus, for the detail, a filesystem resolve of the
package and the tools-directory scan behind the second mask.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParam
from sqlmodel import Session

from afterthread.db import get_session
from afterthread.models import SQLITE_MAX_INT
from afterthread.schemas import (
    ToolRunDetail,
    ToolRunPurgeRequest,
    ToolRunPurgeResult,
    ToolRunStats,
)
from afterthread.services import tool_runs
from afterthread.services import tools as tools_service

router = APIRouter(prefix="/tool-runs", tags=["tools"])

SessionDep = Annotated[Session, Depends(get_session)]
# Capped like every request int bound into SQLite (models.SQLITE_MAX_INT).
RunId = Annotated[int, PathParam(ge=1, le=SQLITE_MAX_INT)]

_RUN_NOT_FOUND = "Tool run not found"

_RUN_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {
        "description": _RUN_NOT_FOUND,
        "content": {"application/json": {"example": {"detail": _RUN_NOT_FOUND}}},
    }
}

# Every literal GET under this prefix (``/stats``) is declared ABOVE
# ``/{run_id}``: FastAPI matches in declaration order, and an id route declared
# first answers ``/stats`` with a 422 for a non-integer id.


@router.get("/stats", response_model=ToolRunStats)
def tool_run_stats(session: SessionDep) -> ToolRunStats:
    """Record counts for the 工具 page header: total, not-ok, orphans, per tool."""
    return ToolRunStats.model_validate(tool_runs.stats(session))


@router.post("/purge", response_model=ToolRunPurgeResult)
def purge_tool_runs(payload: ToolRunPurgeRequest, session: SessionDep) -> ToolRunPurgeResult:
    """Preview (``dry_run``, the default) or delete the records a scope matches.

    The deleting call carries the preview's ``max_id`` and removes nothing newer
    (web-v6 §7, invariant J). Nothing is ever deleted automatically: this route,
    the single-record DELETE and deleting a tool are the only ways records go.
    """
    scope = tool_runs.PurgeScope(
        tool=payload.tool,
        outcome=payload.outcome,
        before=payload.before,
        orphans_only=payload.orphans_only,
    )
    return ToolRunPurgeResult.model_validate(
        tool_runs.purge(session, scope, dry_run=payload.dry_run, max_id=payload.max_id)
    )


@router.get("/{run_id}", response_model=ToolRunDetail, responses=_RUN_NOT_FOUND_RESPONSE)
def get_tool_run(run_id: RunId, session: SessionDep) -> ToolRunDetail:
    """One run record with its bodies, masked again against today's secrets.

    404 for an id that never existed or was deleted. A record whose package is
    gone still answers -- it is exactly what the orphan purge scope shows --
    with ``vid_state`` ``gone`` and nothing fixable.
    """
    row = tool_runs.get(session, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=_RUN_NOT_FOUND)
    versions = tool_runs.PackageVersions.of(row.tool_name)
    secrets = tools_service._redaction_secrets()
    return ToolRunDetail.model_validate(tool_runs.view(row, versions, secrets, bodies=True))


@router.delete("/{run_id}", status_code=204, responses=_RUN_NOT_FOUND_RESPONSE)
def delete_tool_run(run_id: RunId, session: SessionDep) -> None:
    """Delete one run record -- for one whose contents the operator does not want kept."""
    if not tool_runs.delete(session, run_id):
        raise HTTPException(status_code=404, detail=_RUN_NOT_FOUND)
