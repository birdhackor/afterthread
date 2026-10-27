"""Tool run records (web-v6 §6): one row per installed-tool call, and the reads.

``record`` is the writer ``services.tools`` calls after every run of an installed
tool. It is injected (``tools.set_run_recorder``, wired by main's lifespan once
the table exists) rather than imported by the tools module, the same inversion
``llm_log.set_secret_provider`` uses: the execution path stays free of the
database, and a process that never wired a recorder simply records nothing.

What a row holds is decided upstream: ``_run_tool_subprocess`` hands over the
bodies of a FAILED run already redacted and nothing at all for a successful one
(web-v6 §6.2). This module only makes them storable -- surrogate-scrubbed, then
cut to the caps below -- and masks them once more on the way out, so a secret
added to a ``.env`` after the run was written is hidden too (invariant I).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import ColumnElement, Engine, case, func
from sqlalchemy import delete as sql_delete
from sqlmodel import Session, col, create_engine, select

from afterthread.config import get_settings
from afterthread.db import enable_sqlite_foreign_keys
from afterthread.models import ToolRun
from afterthread.services import llm_log, tools

_logger = logging.getLogger("afterthread.tool_runs")

# Per-body caps (web-v6 §6.2). The arguments and stdout keep their HEAD (what
# was asked, and how the output began); stderr keeps its TAIL, because a
# traceback's last lines are the ones that name the failure.
ARGS_MAX_CHARS = 8_000
STDERR_TAIL_MAX_CHARS = 16_000
STDOUT_HEAD_MAX_CHARS = 4_000
# The string the model got, for a run that did not succeed: its head is enough
# for "tool failed (exit 1): <first line>" or a refusal, and the output part of
# it is kept whole in ``stderr`` anyway (web-v6 §6.1).
MESSAGE_MAX_CHARS = 500
_HEAD_CUT_MARKER = "\n…[已截斷]"
_TAIL_CUT_MARKER = "[前段已截斷]…\n"

# How long a write waits on a locked database before giving up, in seconds.
# sqlite3's own default is 5: a purge or an item write holding the lock would
# hold up the LLM tool loop that long for a record nobody is waiting on. The
# record is an observer, so it is the one that yields (web-v6 §6.3).
_BUSY_TIMEOUT_SECONDS = 1.5


def _head(text: str, cap: int) -> str:
    """``text`` cut to its first ``cap`` characters, marked when cut."""
    if len(text) <= cap:
        return text
    return text[: cap - len(_HEAD_CUT_MARKER)] + _HEAD_CUT_MARKER


def _tail(text: str, cap: int) -> str:
    """``text`` cut to its last ``cap`` characters, marked when cut."""
    if len(text) <= cap:
        return text
    return _TAIL_CUT_MARKER + text[len(text) - cap + len(_TAIL_CUT_MARKER) :]


def _storable(text: str | None, cut: Any, cap: int) -> str | None:
    """A redacted body made safe to store: surrogates scrubbed, then cut.

    The redaction already happened, on the WHOLE text (redact -> cut, the
    project-wide order): a cut can then only ever split a marker, never expose
    the piece of a secret a cut-then-redact order could leave behind. The scrub
    is what lets sqlite3 take the text at all -- a model's tool arguments can
    carry a lone surrogate, which it refuses to encode.
    """
    if text is None:
        return None
    return cut(tools._utf8_safe(text), cap)


def build_recorder_engine(database_url: str) -> Engine:
    """The engine run records are written through, with the short busy timeout.

    Its own engine (its own pool) rather than the app's, so the timeout is a
    connection setting of this engine alone and no pooled connection the rest of
    the app uses ever carries it. Same foreign-key and search setup as every
    other engine (``enable_sqlite_foreign_keys``). The URL was already vetted --
    the lifespan wires the recorder only after ``init_db`` built the app's engine
    from the same URL, whose probe rejects anything but a file-backed SQLite.
    """
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": _BUSY_TIMEOUT_SECONDS},
    )
    enable_sqlite_foreign_keys(engine)
    return engine


@lru_cache
def _recorder_engine() -> Engine:
    return build_recorder_engine(get_settings().database_url)


def _row(run: tools.ToolRunRecord) -> ToolRun:
    return ToolRun(
        tool_name=run.tool_name,
        vid=run.vid,
        started_at=run.started_at,
        duration_ms=run.duration_ms,
        outcome=run.outcome,
        exit_code=run.exit_code,
        truncated=run.truncated,
        workflow=run.workflow,
        check_id=run.check_id,
        llm_log_id=run.llm_log_id,
        llm_log_process=run.llm_log_process,
        message=_storable(run.message, _head, MESSAGE_MAX_CHARS),
        args=_storable(run.args, _head, ARGS_MAX_CHARS),
        stdout=_storable(run.stdout, _head, STDOUT_HEAD_MAX_CHARS),
        stderr=_storable(run.stderr, _tail, STDERR_TAIL_MAX_CHARS),
    )


def record(run: tools.ToolRunRecord, *, engine: Engine | None = None) -> int | None:
    """Write one run record in its own short transaction; its id, or None.

    Never raises: a record that cannot be written (the database locked past the
    short timeout, the disk full) costs the record and nothing else. The
    warning names the exception's TYPE only -- a database error's text quotes
    the statement's parameters, which are the run's bodies. ``engine`` is for
    tests; production writes through ``_recorder_engine``.
    """
    try:
        row = _row(run)
        with Session(engine if engine is not None else _recorder_engine()) as session:
            session.add(row)
            session.flush()
            run_id = row.id
            session.commit()
        return run_id
    except Exception as exc:
        _logger.warning("Tool run record not written: %s", type(exc).__name__)
        return None


# --- reads ----------------------------------------------------------------

# The outcomes a one-click fix is offered for (web-v6 §8.5): the tool started and
# failed. ``refused`` never ran, so there is nothing for the builder to fix.
FIXABLE_OUTCOMES = frozenset(
    {tools.RUN_FAILED, tools.RUN_OUTPUT_OVERFLOW, tools.RUN_TIMEOUT, tools.RUN_START_FAILED}
)


def is_fixable(outcome: str, vid: str, current_vid: str | None) -> bool:
    """Whether the page may offer a one-click fix for this run (web-v6 §8.5).

    A failure of the version that is current NOW. An older version's failure
    may already be fixed, and once a fix is published the run's version stops
    being current, so the button disappears by itself instead of paying twice.
    """
    return outcome in FIXABLE_OUTCOMES and current_vid is not None and vid == current_vid


class PackageVersions:
    """Where a run's version stands in its package today; each vid asked once."""

    def __init__(self, package_root: tools.PackageRoot | None, current_vid: str | None) -> None:
        self.package_root = package_root
        self.current_vid = current_vid
        self._exists: dict[str, bool] = {}

    @classmethod
    def of(cls, tool_name: str) -> PackageVersions:
        """The package a record's name addresses now, if it still exists."""
        package_root = tools._resolve_package_dir_no_alias(tool_name)
        if package_root is None:
            return cls(None, None)
        resolution = tools.resolve_current(package_root)
        current_vid = resolution.vid if isinstance(resolution, tools.Resolved) else None
        return cls(package_root, current_vid)

    def state(self, vid: str) -> str:
        if self.current_vid is not None and vid == self.current_vid:
            return "current"
        if self.package_root is None:
            return "gone"
        if vid not in self._exists:
            self._exists[vid] = tools.version_exists(self.package_root, vid)
        return "older" if self._exists[vid] else "gone"


def _masked(text: str | None, secrets: list[str]) -> str | None:
    return None if text is None else tools._mask_known_secrets(text, secrets)


def view(
    row: ToolRun, versions: PackageVersions, secrets: list[str], *, bodies: bool
) -> dict[str, Any]:
    """One record as the API serves it; ``bodies`` adds the args/stdout/stderr.

    Every text is masked again against ``secrets`` -- TODAY's, from
    ``tools._redaction_secrets`` -- the second mask of invariant I: the write
    masked what was known then, this covers a value moved into a ``.env`` since.
    The caller takes that snapshot without a guard, so a failing secret scan
    fails the request instead of serving text it could not check.

    The log link is kept only when this process minted it: the record outlives
    the log ring and its id counter, so an id from an earlier process would
    resolve to whatever interaction holds that number now
    (``routers.tools._summary_detail`` nulls a stale summary link the same way).
    """
    from_this_process = (
        row.llm_log_id is not None and row.llm_log_process == llm_log.process_token()
    )
    data: dict[str, Any] = {
        "id": row.id,
        "tool_name": row.tool_name,
        "vid": row.vid,
        "vid_state": versions.state(row.vid),
        "started_at": row.started_at,
        "duration_ms": row.duration_ms,
        "outcome": row.outcome,
        "exit_code": row.exit_code,
        "truncated": row.truncated,
        "workflow": row.workflow,
        "llm_log_id": row.llm_log_id if from_this_process else None,
        "llm_log_process": row.llm_log_process if from_this_process else None,
        "message": _masked(row.message, secrets),
        "has_details": any(body is not None for body in (row.args, row.stdout, row.stderr)),
        "fixable": is_fixable(row.outcome, row.vid, versions.current_vid),
    }
    if bodies:
        data["args"] = _masked(row.args, secrets)
        data["stdout"] = _masked(row.stdout, secrets)
        data["stderr"] = _masked(row.stderr, secrets)
    return data


def last_run_view(row: ToolRun, current_vid: str | None) -> dict[str, Any]:
    """A list row's ``last_run``: no text at all, so nothing to mask."""
    return {
        "id": row.id,
        "outcome": row.outcome,
        "started_at": row.started_at,
        "vid": row.vid,
        "fixable": is_fixable(row.outcome, row.vid, current_vid),
    }


def list_for_tool(session: Session, tool_name: str, *, limit: int) -> list[ToolRun]:
    """The newest ``limit`` records of one package name, newest first."""
    statement = (
        select(ToolRun)
        .where(ToolRun.tool_name == tool_name)
        .order_by(col(ToolRun.id).desc())
        .limit(limit)
    )
    return list(session.exec(statement))


def count_for_tool(session: Session, tool_name: str) -> int:
    """How many records one package name has, in total."""
    statement = select(func.count()).select_from(ToolRun).where(ToolRun.tool_name == tool_name)
    return int(session.exec(statement).one())


def get(session: Session, run_id: int) -> ToolRun | None:
    return session.get(ToolRun, run_id)


def delete(session: Session, run_id: int) -> bool:
    """Delete one record; False when there was none to delete."""
    row = session.get(ToolRun, run_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def last_runs_by_name(session: Session, names: list[str]) -> dict[str, ToolRun]:
    """The newest record of each named package, for the list rows."""
    if not names:
        return {}
    newest = (
        select(func.max(ToolRun.id))
        .where(col(ToolRun.tool_name).in_(names))
        .group_by(ToolRun.tool_name)
    )
    statement = select(ToolRun).where(col(ToolRun.id).in_(newest))
    return {row.tool_name: row for row in session.exec(statement)}


def delete_for_tool(session: Session, tool_name: str) -> bool:
    """Delete every record of one package name; False when that failed (web-v6 §6.4).

    Called after the package itself was deleted. A failure here must not turn a
    finished deletion into an error, so it is reported, not raised: the route
    answers ``runs_deleted: false`` and the records stay reachable through the
    purge's orphan scope.
    """
    try:
        session.exec(
            sql_delete(ToolRun)
            .where(col(ToolRun.tool_name) == tool_name)
            .execution_options(synchronize_session=False)
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        _logger.warning("Tool run records not deleted: %s", type(exc).__name__)
        return False
    return True


# --- purge (web-v6 §7) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PurgeScope:
    """Which records a purge addresses; every condition narrows, none widens.

    ``outcome`` is ``all``, ``ok`` (successes) or ``not_ok`` (everything else: a
    refused call is not a success either, and nobody keeps one for its own
    sake). ``before`` is an absolute instant rather than an age, so the preview
    and the confirmation judge the same records: an age measured twice moves
    with the clock, and a record crossing the line between the two calls would
    be deleted without having been counted.
    """

    tool: str | None = None
    outcome: str = "all"
    before: datetime | None = None
    orphans_only: bool = False


def orphan_names(session: Session) -> list[str]:
    """The record names whose package no longer exists, sorted.

    Empty when the tools directory is not configured OR not there (an unmounted
    volume, a renamed directory): then NO name resolves, and calling every
    record an orphan would hand a purge the whole table for a condition the
    operator may fix in a minute. The same "missing root is the feature off"
    reading ``tools._scan_all`` and the tools lock apply.
    """
    base = tools.tools_dir()
    if base is None or not base.is_dir():
        return []
    names = session.exec(select(ToolRun.tool_name).distinct()).all()
    return sorted(name for name in names if tools._resolve_package_dir_no_alias(name) is None)


def _conditions(session: Session, scope: PurgeScope) -> list[ColumnElement[bool]] | None:
    """The WHERE terms of a scope, or None when it can match nothing at all."""
    conditions: list[ColumnElement[bool]] = []
    if scope.tool is not None:
        conditions.append(col(ToolRun.tool_name) == scope.tool)
    if scope.outcome == "ok":
        conditions.append(col(ToolRun.outcome) == tools.RUN_OK)
    elif scope.outcome == "not_ok":
        conditions.append(col(ToolRun.outcome) != tools.RUN_OK)
    if scope.before is not None:
        conditions.append(col(ToolRun.started_at) < _naive_utc(scope.before))
    if scope.orphans_only:
        orphans = orphan_names(session)
        if not orphans:
            return None
        conditions.append(col(ToolRun.tool_name).in_(orphans))
    return conditions


def _naive_utc(value: datetime) -> datetime:
    # The column holds naive UTC (SQLite keeps no offset); compare like with like.
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def purge(
    session: Session, scope: PurgeScope, *, dry_run: bool, max_id: int | None = None
) -> dict[str, Any]:
    """Count, or delete, the records a scope matches (web-v6 §7).

    A dry run answers ``matched`` and the highest matching id. The real run
    deletes only up to the ``max_id`` that preview returned, so a record written
    after the operator saw the count -- another tab's tool call failing in
    between -- is never swept along with it (invariant J). Ids only grow
    (AUTOINCREMENT), which is what makes the bound exact.
    """
    conditions = _conditions(session, scope)
    if conditions is None:
        return {"matched": 0, "deleted": 0, "max_id": None}
    if dry_run:
        matched, highest = session.exec(
            select(func.count(), func.max(ToolRun.id)).where(*conditions)
        ).one()
        return {"matched": int(matched), "deleted": 0, "max_id": highest}
    if max_id is None:
        raise ValueError("a purge that deletes needs the max_id of its preview")
    # synchronize_session=False: the session is the request's, and the default
    # ("evaluate") would re-run this WHERE in Python over every ToolRun it holds
    # -- comparing their aware ``started_at`` with the naive cutoff raises.
    # Nothing in these sessions needs syncing, and ``rowcount`` is unaffected.
    result = session.exec(
        sql_delete(ToolRun)
        .where(*conditions, col(ToolRun.id) <= max_id)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    deleted = int(result.rowcount or 0)
    return {"matched": deleted, "deleted": deleted, "max_id": max_id}


def stats(session: Session) -> dict[str, Any]:
    """How many records there are, per package name, and how many are orphans."""
    not_ok = func.sum(case((col(ToolRun.outcome) != tools.RUN_OK, 1), else_=0))
    rows = session.exec(
        select(ToolRun.tool_name, func.count(), not_ok)
        .group_by(ToolRun.tool_name)
        .order_by(ToolRun.tool_name)
    ).all()
    per_tool = [
        {"tool_name": name, "total": int(total), "not_ok": int(failed or 0)}
        for name, total, failed in rows
    ]
    orphans = set(orphan_names(session))
    return {
        "total": sum(entry["total"] for entry in per_tool),
        "not_ok": sum(entry["not_ok"] for entry in per_tool),
        "orphans": sum(entry["total"] for entry in per_tool if entry["tool_name"] in orphans),
        "tools": per_tool,
    }


# --- attaching records to a revise (web-v6 §8.3) ---------------------------------


@dataclass(frozen=True, slots=True)
class AttachedRun:
    """One run record as a revise job carries it: copied, and masked, at submit.

    The copy is the point (invariant L): it is taken before the job exists, so a
    purge of the record while the revise is queued or running changes nothing
    about what the builder is shown. The texts are masked against the secrets
    known at that moment, the same second mask a read applies.
    """

    id: int
    started_at: datetime
    workflow: str
    vid: str
    outcome: str
    exit_code: int | None
    message: str | None
    args: str | None
    stdout: str | None
    stderr: str | None


@dataclass(frozen=True, slots=True)
class AttachedRuns:
    """The records to attach, in the order asked for, or why they cannot be."""

    runs: tuple[AttachedRun, ...] = ()
    # ``run_not_found`` (no such id, or purged) | ``run_not_for_tool``
    refusal: str | None = None


def attach_for_revise(session: Session, tool_name: str, run_ids: list[int]) -> AttachedRuns:
    """Load and copy the records a revise of ``tool_name`` attaches.

    Every id must exist and belong to this package name; otherwise nothing is
    attached and the caller refuses the submit before any job, slot or LLM call
    exists (invariant M). Fail-closed on the secret scan like every read.
    """
    rows = {
        row.id: row for row in session.exec(select(ToolRun).where(col(ToolRun.id).in_(run_ids)))
    }
    if any(run_id not in rows for run_id in run_ids):
        return AttachedRuns(refusal="run_not_found")
    if any(rows[run_id].tool_name != tool_name for run_id in run_ids):
        return AttachedRuns(refusal="run_not_for_tool")
    secrets = tools._redaction_secrets()
    attached = []
    for run_id in run_ids:
        row = rows[run_id]
        attached.append(
            AttachedRun(
                id=run_id,
                started_at=_naive_utc(row.started_at).replace(tzinfo=UTC),
                workflow=row.workflow,
                vid=row.vid,
                outcome=row.outcome,
                exit_code=row.exit_code,
                message=_masked(row.message, secrets),
                args=_masked(row.args, secrets),
                stdout=_masked(row.stdout, secrets),
                stderr=_masked(row.stderr, secrets),
            )
        )
    return AttachedRuns(runs=tuple(attached))
