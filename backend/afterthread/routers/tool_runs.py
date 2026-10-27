"""Tool run records by id (web-v6 §6, §7): read one with its bodies, delete one.

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
from afterthread.schemas import ToolRunDetail
from afterthread.services import tool_runs
from afterthread.services import tools as tools_service

router = APIRouter(prefix="/tool-runs", tags=["tools"])

SessionDep = Annotated[Session, Depends(get_session)]
RunId = Annotated[int, PathParam(ge=1)]

_RUN_NOT_FOUND = "Tool run not found"

_RUN_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {
        "description": _RUN_NOT_FOUND,
        "content": {"application/json": {"example": {"detail": _RUN_NOT_FOUND}}},
    }
}

# Any literal GET added under this prefix (the purge's ``/stats``) must be
# declared ABOVE ``/{run_id}``: FastAPI matches in declaration order, and an id
# route declared first answers ``/stats`` with a 422 for a non-integer id.


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
