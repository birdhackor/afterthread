"""Tool run records (web-v6 §6): what the handler records, and the reads.

Invariants (design §11): F -- recording never changes what the model gets, and a
failing recorder never fails the call; G -- stored bodies hold no known secret,
on the timeout and stderr-overflow paths too; H -- a successful run stores no
bodies; I -- bodies are masked again on read; U -- a lone surrogate or a locked
database costs the record at most, never the call; W -- route declaration order.
"""

import asyncio
import json
import logging
import sqlite3
import sys
import time
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, col, select

from afterthread.config import Settings
from afterthread.db import get_session
from afterthread.main import app
from afterthread.models import ToolRun
from afterthread.services import llm, llm_log, tool_runs, tools
from afterthread.services.tools import enabled_llm_tools, set_enabled

_TEST_VID = "20260728T010203Z-abcdef"
_OTHER_VID = "20260729T010203Z-123456"
_SECRET = "kb-secret-0123456789"
_MARKER = tools._REDACTION_MARKER
_CUT = tools._OUTPUT_TRUNCATION_MARKER


@pytest.fixture(autouse=True)
def _reset_state() -> Generator[None]:
    """No recorder, no advertisement generations, an empty AI log -- before and after."""
    llm_log._reset_for_tests()
    tools._ADVERTISEMENT_GENERATIONS.clear()
    tools.set_run_recorder(None)
    yield
    tools.set_run_recorder(None)
    tools._ADVERTISEMENT_GENERATIONS.clear()
    llm_log._reset_for_tests()


def _make_tool(
    tools_root: Path,
    name: str,
    run_py: str,
    *,
    vid: str = _TEST_VID,
    entry: list[str] | None = None,
    dotenv: str | None = None,
) -> Path:
    """One installed package with one committed version; returns the package dir."""
    package = tools_root / name
    version = package / tools._VERSIONS_DIRNAME / vid
    version.mkdir(parents=True)
    (version / "run.py").write_text(run_py, encoding="utf-8")
    manifest = {
        "name": name,
        "description": "test tool",
        "parameters": {"type": "object", "properties": {}},
        "entry": entry if entry is not None else [sys.executable, "run.py"],
    }
    (version / "tool.json").write_text(json.dumps(manifest), encoding="utf-8")
    (version / tools._META_DIRNAME).mkdir()
    (version / tools._META_DIRNAME / tools._ORIGIN_FILENAME).write_text(
        json.dumps(
            {
                "source": "test-fixture",
                "openapi_url": None,
                "instructions": None,
                "feedback": None,
                "previous": None,
            }
        ),
        encoding="utf-8",
    )
    package_meta = package / tools._META_DIRNAME
    package_meta.mkdir()
    (package_meta / tools._CURRENT_FILENAME).write_text(f"{vid}\n", encoding="ascii")
    (package_meta / tools._PACKAGE_STATE_FILENAME).write_text(
        json.dumps({tools._STATE_MARKER_KEY: tools._STATE_MARKER_VALUE, "enabled": True}),
        encoding="utf-8",
    )
    if dotenv is not None:
        (package / ".env").write_text(dotenv, encoding="utf-8")
    return package


def _install_tools(monkeypatch: pytest.MonkeyPatch, root: Path, **overrides: Any) -> Settings:
    settings = Settings(tools_dir=str(root), **overrides)
    monkeypatch.setattr("afterthread.services.tools.get_settings", lambda: settings)
    return settings


def _call(arguments: dict[str, Any] | None = None) -> str:
    """Advertise the one installed tool and call it once."""
    [tool] = enabled_llm_tools()
    return asyncio.run(tool.handler(arguments or {}))


@pytest.fixture
def records() -> list[tools.ToolRunRecord]:
    """A recorder that keeps what it is handed, ids from 1."""
    kept: list[tools.ToolRunRecord] = []

    def _record(run: tools.ToolRunRecord) -> int:
        kept.append(run)
        return len(kept)

    tools.set_run_recorder(_record)
    return kept


@pytest.fixture
def stored(session: Session) -> Callable[[], list[ToolRun]]:
    """The real recorder, writing into the test database; returns a row reader."""
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    tools.set_run_recorder(partial(tool_runs.record, engine=engine))

    def _rows() -> list[ToolRun]:
        session.expire_all()
        return list(session.exec(select(ToolRun).order_by(col(ToolRun.id))))

    return _rows


# --- F: the model's string is unchanged; the recorder is an observer ---------


def test_ok_run_returns_the_same_string_and_records_metadata_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, records: list[tools.ToolRunRecord]
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "echo", "import sys\nsys.stdout.write('ECHO:' + sys.stdin.read())\n")
    _install_tools(monkeypatch, root)

    assert _call({"q": "hi"}) == 'ECHO:{"q": "hi"}'

    [run] = records
    assert run.tool_name == "echo"
    assert run.vid == _TEST_VID
    assert run.outcome == tools.RUN_OK
    assert run.exit_code is None
    assert run.truncated is False
    # H: a success keeps no bodies and no message.
    assert (run.args, run.stdout, run.stderr, run.message) == (None, None, None, None)
    assert run.workflow == "unknown"  # no interaction around a direct call
    assert run.llm_log_id is None
    assert run.started_at.tzinfo is not None
    assert run.duration_ms >= 0


def test_ok_run_cut_at_the_output_cap_is_recorded_as_truncated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, records: list[tools.ToolRunRecord]
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "big", "import sys\nsys.stdout.write('a' * 5000)\n")
    _install_tools(monkeypatch, root, llm_tool_output_max_chars=1000)

    assert _call() == "a" * (1000 - len(_CUT)) + _CUT

    [run] = records
    assert (run.outcome, run.truncated) == (tools.RUN_OK, True)
    assert run.stdout is None


def test_failed_run_returns_the_same_string_and_keeps_the_bodies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, records: list[tools.ToolRunRecord]
) -> None:
    root = tmp_path / "tools"
    _make_tool(
        root,
        "boom",
        "import sys\nsys.stdout.write('partial')\nsys.stderr.write('bad things')\nsys.exit(3)\n",
    )
    _install_tools(monkeypatch, root)

    assert _call({"q": "x"}) == "tool failed (exit 3): bad things"

    [run] = records
    assert (run.outcome, run.exit_code) == (tools.RUN_FAILED, 3)
    assert run.args == '{"q": "x"}'
    assert run.stdout == "partial"
    assert run.stderr == "bad things"
    assert run.message == "tool failed (exit 3): bad things"


def test_stderr_overflow_says_exit_minus_nine_but_records_output_overflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, records: list[tools.ToolRunRecord]
) -> None:
    """The model's string cannot tell an overflow kill from a crash; the record can."""
    root = tmp_path / "tools"
    _make_tool(
        root,
        "noisy",
        "import sys\nwhile True:\n    sys.stderr.write('e' * 4096)\n    sys.stderr.flush()\n",
    )
    _install_tools(monkeypatch, root, llm_tool_output_max_chars=1000)

    result = _call()

    assert result == "tool failed (exit -9): " + "e" * (1000 - len(_CUT)) + _CUT
    [run] = records
    assert (run.outcome, run.exit_code) == (tools.RUN_OUTPUT_OVERFLOW, -9)
    assert run.stderr == "e" * 1001  # what was read: cap + 1, uncut until the recorder


def test_timeout_returns_the_same_string_and_keeps_what_was_printed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, records: list[tools.ToolRunRecord]
) -> None:
    root = tmp_path / "tools"
    _make_tool(
        root,
        "slow",
        "import sys, time\nsys.stderr.write('waiting on api')\nsys.stderr.flush()\n"
        "time.sleep(30)\n",
    )
    _install_tools(monkeypatch, root, llm_tool_timeout_seconds=0.5)

    assert _call() == "tool timed out after 0.5 seconds"

    [run] = records
    assert run.outcome == tools.RUN_TIMEOUT
    assert run.exit_code is None
    assert run.stderr == "waiting on api"
    assert run.args == "{}"


def test_start_failure_returns_the_same_string_and_keeps_the_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, records: list[tools.ToolRunRecord]
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "nointerp", "print('x')\n", entry=[str(tmp_path / "no-such-python"), "run.py"])
    _install_tools(monkeypatch, root)

    assert _call({"q": "x"}) == "tool failed to start: FileNotFoundError"

    [run] = records
    assert run.outcome == tools.RUN_START_FAILED
    assert run.args == '{"q": "x"}'
    assert (run.stdout, run.stderr) == (None, None)


def test_refusals_return_the_same_strings_and_record_no_bodies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, records: list[tools.ToolRunRecord]
) -> None:
    root = tmp_path / "tools"
    package = _make_tool(root, "toggled", "print('ok')\n")
    _install_tools(monkeypatch, root)
    [tool] = enabled_llm_tools()

    assert set_enabled("toggled", False) is True
    assert asyncio.run(tool.handler({"q": "x"})) == tools._TOOL_DISABLED_RESULT

    assert set_enabled("toggled", True) is True
    manifest_path = package / tools._VERSIONS_DIRNAME / _TEST_VID / "tool.json"
    replacement = manifest_path.with_name("tool.json.new")
    replacement.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
    replacement.replace(manifest_path)
    assert asyncio.run(tool.handler({"q": "x"})) == tools._TOOL_REPLACED_RESULT

    assert [run.outcome for run in records] == [tools.RUN_REFUSED, tools.RUN_REFUSED]
    assert [run.message for run in records] == [
        tools._TOOL_DISABLED_RESULT,
        tools._TOOL_REPLACED_RESULT,
    ]
    assert all((run.args, run.stdout, run.stderr) == (None, None, None) for run in records)


def test_unreadable_identity_is_refused_and_still_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, records: list[tools.ToolRunRecord]
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "unknown", "print('ok')\n")
    _install_tools(monkeypatch, root)
    monkeypatch.setattr(tools, "package_identity", lambda _directory: None)

    assert _call() == tools._TOOL_REPLACED_RESULT
    assert [run.outcome for run in records] == [tools.RUN_REFUSED]


def test_a_recorder_that_raises_changes_nothing_and_logs_no_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "boom", "import sys\nsys.stderr.write('bad things')\nsys.exit(1)\n")
    _install_tools(monkeypatch, root)

    def _broken(_run: tools.ToolRunRecord) -> int:
        raise RuntimeError("bad things in the recorder")

    tools.set_run_recorder(_broken)
    with caplog.at_level(logging.WARNING, logger="afterthread.tools"):
        assert _call({"q": "private"}) == "tool failed (exit 1): bad things"

    assert [record.getMessage() for record in caplog.records] == [
        "Tool run record not written: RuntimeError"
    ]
    assert all(record.exc_info is None for record in caplog.records)


def test_no_recorder_means_no_record_and_the_same_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "echo", "import sys\nsys.stdout.write('ok')\n")
    _install_tools(monkeypatch, root)

    assert _call() == "ok"


def test_a_call_the_backend_fails_is_recorded_as_error_and_still_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, records: list[tools.ToolRunRecord]
) -> None:
    """Every call is recorded -- also one that dies in the backend after the tool ran.

    The fail-closed redaction is the realistic case: the tool exited non-zero and
    the secret scan masking its stderr raised. The exception still reaches the
    llm loop, which tells the model what it always did; the record stores the
    same words and keeps no bodies.
    """
    root = tmp_path / "tools"
    _make_tool(root, "boom", "import sys\nsys.stderr.write('bad')\nsys.exit(1)\n")
    _install_tools(monkeypatch, root)
    [tool] = enabled_llm_tools()

    def _broken_scan() -> frozenset[str]:
        raise OSError("tools directory unreadable")

    monkeypatch.setattr(tools, "known_secret_values", _broken_scan)

    with pytest.raises(OSError):
        asyncio.run(tool.handler({"q": "x"}))
    model_text = asyncio.run(llm._invoke_tool_handler(tool, '{"q": "x"}'))

    assert model_text == "tool execution failed: OSError"
    assert [run.outcome for run in records] == [tools.RUN_ERROR, tools.RUN_ERROR]
    assert records[0].message == model_text
    assert (records[0].args, records[0].stdout, records[0].stderr) == (None, None, None)


def test_stderr_overflow_that_exits_on_its_own_is_a_plain_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, records: list[tools.ToolRunRecord]
) -> None:
    """``output_overflow`` claims the kill; a tool that exited with its own code first
    failed with that code, and the model was told so."""
    root = tmp_path / "tools"
    _make_tool(root, "noisy", "import sys\nsys.stderr.write('e' * 10)\nsys.exit(3)\n")
    _install_tools(monkeypatch, root)
    real = tools._communicate_bounded

    def _overflowed(*args: Any, **kwargs: Any) -> Any:
        output = real(*args, **kwargs)
        output.stderr_overflow = True  # past the cap, but the exit beat the kill
        return output

    monkeypatch.setattr(tools, "_communicate_bounded", _overflowed)

    assert _call() == "tool failed (exit 3): eeeeeeeeee"
    [run] = records
    assert (run.outcome, run.exit_code) == (tools.RUN_FAILED, 3)


# --- the AI log link -----------------------------------------------------------


class _Out(BaseModel):
    title: str


def test_a_run_inside_an_interaction_links_to_its_log_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, records: list[tools.ToolRunRecord]
) -> None:
    """generate_structured tells the handlers which log record they run under."""
    root = tmp_path / "tools"
    _make_tool(root, "echo", "import sys\nsys.stdout.write('ok')\n")
    _install_tools(monkeypatch, root)
    [tool] = enabled_llm_tools()

    def _completion(*, tool_call: bool) -> SimpleNamespace:
        calls = (
            [
                SimpleNamespace(
                    id="call_1",
                    type="function",
                    function=SimpleNamespace(name="echo", arguments="{}"),
                )
            ]
            if tool_call
            else None
        )
        content = None if tool_call else json.dumps({"title": "done"})
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))]
        )

    scripted = [_completion(tool_call=True), _completion(tool_call=False)]

    async def _create(**_kwargs: Any) -> Any:
        return scripted.pop(0)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
    llm_settings = Settings(
        openai_base_url="https://llm.example.test/v1", openai_api_key="k" * 12, openai_model="m"
    )
    monkeypatch.setattr("afterthread.services.llm.get_settings", lambda: llm_settings)
    monkeypatch.setattr("afterthread.services.llm._get_client", lambda: client)

    asyncio.run(llm.generate_structured("system", "user", _Out, workflow="capture", tools=[tool]))

    [run] = records
    [summary] = llm_log.list_summaries(10)
    assert run.workflow == "capture"
    assert run.llm_log_id == summary["id"]
    assert run.llm_log_process == llm_log.process_token()
    assert llm.current_interaction() is None  # reset once the interaction ended


# --- G: no secret reaches a stored body ------------------------------------------


_LEAKY_RUN_PY = (
    "import os, sys\n"
    "sys.stdout.write('out ' + os.environ['KB_API_KEY'])\n"
    "sys.stderr.write('Traceback: token=' + os.environ['KB_API_KEY'] + ' rejected')\n"
    "sys.exit(2)\n"
)


def test_secrets_in_args_and_output_are_masked_in_the_stored_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stored: Callable[[], list[ToolRun]]
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "leaky", _LEAKY_RUN_PY, dotenv=f"KB_API_KEY={_SECRET}\n")
    _install_tools(monkeypatch, root)

    result = _call({"token": _SECRET})

    assert _SECRET not in result
    [row] = stored()
    for body in (row.args, row.stdout, row.stderr, row.message):
        assert body is not None
        assert _SECRET not in body
        assert _MARKER in body


def test_secrets_are_masked_on_the_timeout_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stored: Callable[[], list[ToolRun]]
) -> None:
    root = tmp_path / "tools"
    _make_tool(
        root,
        "stuck",
        "import os, sys, time\n"
        "sys.stdout.write('calling with ' + os.environ['KB_API_KEY'])\nsys.stdout.flush()\n"
        "sys.stderr.write('auth ' + os.environ['KB_API_KEY'])\nsys.stderr.flush()\n"
        "time.sleep(30)\n",
        dotenv=f"KB_API_KEY={_SECRET}\n",
    )
    _install_tools(monkeypatch, root, llm_tool_timeout_seconds=0.5)

    assert _call({"token": _SECRET}) == "tool timed out after 0.5 seconds"

    [row] = stored()
    assert row.outcome == tools.RUN_TIMEOUT
    for body in (row.args, row.stdout, row.stderr):
        assert body is not None
        assert _SECRET not in body
        assert _MARKER in body


def test_secrets_are_masked_on_the_stderr_overflow_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stored: Callable[[], list[ToolRun]]
) -> None:
    root = tmp_path / "tools"
    _make_tool(
        root,
        "flood",
        "import os, sys\n"
        "sys.stderr.write('key ' + os.environ['KB_API_KEY'] + ' ')\n"
        "while True:\n    sys.stderr.write('e' * 4096)\n    sys.stderr.flush()\n",
        dotenv=f"KB_API_KEY={_SECRET}\n",
    )
    _install_tools(monkeypatch, root, llm_tool_output_max_chars=1000)

    _call()

    [row] = stored()
    assert row.outcome == tools.RUN_OUTPUT_OVERFLOW
    assert row.stderr is not None
    assert _SECRET not in row.stderr
    assert _MARKER in row.stderr


# --- the writer: caps, surrogates, a locked database -------------------------------


def _record(**overrides: Any) -> tools.ToolRunRecord:
    fields: dict[str, Any] = {
        "tool_name": "kb",
        "vid": _TEST_VID,
        "started_at": datetime(2026, 10, 27, 1, 2, 3, tzinfo=UTC),
        "duration_ms": 12,
        "outcome": tools.RUN_FAILED,
        "exit_code": 1,
        "truncated": False,
        "workflow": "capture",
        "check_id": None,
        "llm_log_id": None,
        "llm_log_process": None,
        "message": "tool failed (exit 1): boom",
        "args": "{}",
        "stdout": "",
        "stderr": "boom",
    }
    fields.update(overrides)
    return tools.ToolRunRecord(**fields)


def test_record_cuts_each_body_to_its_cap_from_the_right_end(session: Session) -> None:
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    run_id = tool_runs.record(
        _record(
            args="A" * 9000 + "END",
            stdout="S" * 5000 + "END",
            stderr="START" + "E" * 20000 + "LAST LINE",
            message="M" * 900,
        ),
        engine=engine,
    )

    assert run_id is not None
    row = session.get(ToolRun, run_id)
    assert row is not None
    assert row.args is not None and row.stdout is not None
    assert row.stderr is not None and row.message is not None
    assert len(row.args) == tool_runs.ARGS_MAX_CHARS
    assert row.args.startswith("AAA") and row.args.endswith("…[已截斷]")
    assert len(row.stdout) == tool_runs.STDOUT_HEAD_MAX_CHARS
    assert row.stdout.startswith("SSS")
    # stderr keeps its END: the line a traceback names the failure on.
    assert len(row.stderr) == tool_runs.STDERR_TAIL_MAX_CHARS
    assert row.stderr.startswith("[前段已截斷]…") and row.stderr.endswith("LAST LINE")
    assert len(row.message) == tool_runs.MESSAGE_MAX_CHARS


def test_a_lone_surrogate_in_the_args_is_stored_as_a_replacement_character(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stored: Callable[[], list[ToolRun]]
) -> None:
    """U: the model's arguments can carry a lone surrogate, which sqlite3 refuses."""
    root = tmp_path / "tools"
    _make_tool(root, "boom", "import sys\nsys.stderr.write('bad')\nsys.exit(1)\n")
    _install_tools(monkeypatch, root)

    assert _call({"q": "a\ud800b"}) == "tool failed (exit 1): bad"

    [row] = stored()
    assert row.args is not None
    row.args.encode("utf-8")  # storable: no surrogate survived
    assert row.args == tools._utf8_safe('{"q": "a\ud800b"}')
    assert "\ufffd" in row.args


def test_a_locked_database_costs_the_record_within_the_short_timeout(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """U: a writer holding the lock does not stall the tool loop for sqlite's 5 s."""
    database = tmp_path / "runs.db"
    url = f"sqlite:///{database}"
    engine = tool_runs.build_recorder_engine(url)
    SQLModel.metadata.create_all(engine)
    holder = sqlite3.connect(database, timeout=0, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="afterthread.tool_runs"):
            assert tool_runs.record(_record(stderr="secret-ish body"), engine=engine) is None
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        engine.dispose()

    assert elapsed < 4
    assert [record.getMessage() for record in caplog.records] == [
        "Tool run record not written: OperationalError"
    ]


# --- reads: the API -------------------------------------------------------------


def _seed_rows(session: Session, *rows: tools.ToolRunRecord) -> list[int]:
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    ids = [tool_runs.record(row, engine=engine) for row in rows]
    assert all(run_id is not None for run_id in ids)
    return [run_id for run_id in ids if run_id is not None]


def test_runs_list_is_newest_first_without_bodies(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "kb", "print('x')\n")
    _install_tools(monkeypatch, root)
    first, second = _seed_rows(
        session,
        _record(
            outcome=tools.RUN_OK, exit_code=None, message=None, args=None, stdout=None, stderr=None
        ),
        _record(),
    )

    response = client.get("/api/tools/kb/runs")

    assert response.status_code == 200
    body = response.json()
    assert body["current_vid"] == _TEST_VID
    assert body["total"] == 2
    assert [run["id"] for run in body["runs"]] == [second, first]
    newest = body["runs"][0]
    assert newest["has_details"] is True
    assert newest["fixable"] is True
    assert newest["vid_state"] == "current"
    assert newest["started_at"] == "2026-10-27T01:02:03Z"
    assert "stderr" not in newest and "args" not in newest
    assert body["runs"][1]["has_details"] is False
    assert body["runs"][1]["fixable"] is False


def test_runs_list_honors_the_limit_and_404s_for_a_missing_tool(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "kb", "print('x')\n")
    _install_tools(monkeypatch, root)
    _seed_rows(session, _record(), _record(), _record())

    limited = client.get("/api/tools/kb/runs", params={"limit": 2}).json()
    assert (len(limited["runs"]), limited["total"]) == (2, 3)
    assert client.get("/api/tools/nope/runs").status_code == 404
    assert client.get("/api/tools/kb/runs", params={"limit": 0}).status_code == 422


@pytest.mark.parametrize(
    ("outcome", "vid", "fixable", "vid_state"),
    [
        (tools.RUN_FAILED, _TEST_VID, True, "current"),
        (tools.RUN_OUTPUT_OVERFLOW, _TEST_VID, True, "current"),
        (tools.RUN_TIMEOUT, _TEST_VID, True, "current"),
        (tools.RUN_START_FAILED, _TEST_VID, True, "current"),
        (tools.RUN_REFUSED, _TEST_VID, False, "current"),
        (tools.RUN_OK, _TEST_VID, False, "current"),
        (tools.RUN_FAILED, _OTHER_VID, False, "gone"),
    ],
)
def test_fixable_is_a_failure_of_the_current_version(
    client: TestClient,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: str,
    vid: str,
    fixable: bool,
    vid_state: str,
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "kb", "print('x')\n")
    _install_tools(monkeypatch, root)
    [run_id] = _seed_rows(session, _record(outcome=outcome, vid=vid))

    [run] = client.get("/api/tools/kb/runs").json()["runs"]
    assert (run["fixable"], run["vid_state"]) == (fixable, vid_state)
    [row] = client.get("/api/tools").json()["tools"]
    assert row["last_run"] == {
        "id": run_id,
        "outcome": outcome,
        "started_at": "2026-10-27T01:02:03Z",
        "vid": vid,
        "fixable": fixable,
    }


def test_an_older_version_that_still_exists_is_older_not_gone(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    package = _make_tool(root, "kb", "print('x')\n", vid=_OTHER_VID)
    # A second committed version, made current: _OTHER_VID stays on disk.
    older = package / tools._VERSIONS_DIRNAME / _OTHER_VID
    newer = package / tools._VERSIONS_DIRNAME / _TEST_VID
    newer.mkdir()
    for name in ("run.py", "tool.json"):
        (newer / name).write_bytes((older / name).read_bytes())
    (newer / tools._META_DIRNAME).mkdir()
    (newer / tools._META_DIRNAME / tools._ORIGIN_FILENAME).write_bytes(
        (older / tools._META_DIRNAME / tools._ORIGIN_FILENAME).read_bytes()
    )
    (package / tools._META_DIRNAME / tools._CURRENT_FILENAME).write_text(
        f"{_TEST_VID}\n", encoding="ascii"
    )
    _install_tools(monkeypatch, root)
    _seed_rows(session, _record(vid=_OTHER_VID))

    [run] = client.get("/api/tools/kb/runs").json()["runs"]
    assert (run["vid_state"], run["fixable"]) == ("older", False)


def test_run_detail_has_the_bodies_and_delete_removes_it(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "kb", "print('x')\n")
    _install_tools(monkeypatch, root)
    [run_id] = _seed_rows(session, _record(args='{"q": 1}', stdout="out", stderr="err"))

    detail = client.get(f"/api/tool-runs/{run_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert (body["args"], body["stdout"], body["stderr"]) == ('{"q": 1}', "out", "err")
    assert body["fixable"] is True

    assert client.delete(f"/api/tool-runs/{run_id}").status_code == 204
    assert client.get(f"/api/tool-runs/{run_id}").status_code == 404
    assert client.delete(f"/api/tool-runs/{run_id}").status_code == 404
    assert client.get("/api/tool-runs/0").status_code == 422


def test_a_record_whose_package_is_gone_still_reads_as_gone(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    root.mkdir()
    _install_tools(monkeypatch, root)
    [run_id] = _seed_rows(session, _record(tool_name="vanished"))

    body = client.get(f"/api/tool-runs/{run_id}").json()
    assert (body["vid_state"], body["fixable"]) == ("gone", False)


def test_a_log_link_from_another_process_is_not_served(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Log ids restart with the process; the record outlives it."""
    root = tmp_path / "tools"
    _make_tool(root, "kb", "print('x')\n")
    _install_tools(monkeypatch, root)
    _seed_rows(
        session,
        _record(llm_log_id=7, llm_log_process="a-previous-process"),
        _record(llm_log_id=8, llm_log_process=llm_log.process_token()),
    )

    runs = client.get("/api/tools/kb/runs").json()["runs"]
    assert [(run["llm_log_id"], run["llm_log_process"]) for run in runs] == [
        (8, llm_log.process_token()),
        (None, None),
    ]


def test_bodies_are_masked_again_on_read(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """I: a value moved into a .env after the run was written is hidden on read."""
    root = tmp_path / "tools"
    package = _make_tool(root, "kb", "print('x')\n")
    _install_tools(monkeypatch, root)
    later = "later-secret-value-42"
    [run_id] = _seed_rows(
        session,
        _record(
            args=json.dumps({"token": later}),
            stdout=f"out {later}",
            stderr=f"err {later}",
            message=f"tool failed (exit 1): err {later}",
        ),
    )
    (package / ".env").write_text(f"KB_API_KEY={later}\n", encoding="utf-8")

    detail = client.get(f"/api/tool-runs/{run_id}").json()
    for field in ("args", "stdout", "stderr", "message"):
        assert later not in detail[field]
        assert _MARKER in detail[field]
    [listed] = client.get("/api/tools/kb/runs").json()["runs"]
    assert later not in listed["message"]


def test_a_failing_secret_scan_fails_the_read_instead_of_serving_unchecked_text(
    session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "kb", "print('x')\n")
    _install_tools(monkeypatch, root)
    [run_id] = _seed_rows(session, _record())

    def _broken() -> frozenset[str]:
        raise OSError("scan failed")

    monkeypatch.setattr(tools, "known_secret_values", _broken)
    app.dependency_overrides[get_session] = lambda: session
    try:
        failing = TestClient(app, raise_server_exceptions=False)
        assert failing.get(f"/api/tool-runs/{run_id}").status_code == 500
        assert failing.get("/api/tools/kb/runs").status_code == 500
    finally:
        app.dependency_overrides.clear()


# --- W: route declaration order ---------------------------------------------------


def test_a_tool_named_jobs_has_its_runs_read_not_a_job_poll(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "jobs", "print('x')\n")
    _install_tools(monkeypatch, root)

    response = client.get("/api/tools/jobs/runs")

    assert response.status_code == 200
    assert response.json() == {"current_vid": _TEST_VID, "total": 0, "runs": []}
