"""examples.json, 跑一次範例 and the verification after a fix (web-v6 §8.3, §10).

Invariants (design §11): P -- the health check and the verification run through
the AI request's own execution path and run only the examples; a disabled tool
is not run and is reported unverified. S -- they have a total budget: after the
first timeout the rest are skipped, and the run ends within the budget. T --
while a check runs, delete and discard are refused and touch nothing.
"""

import asyncio
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Generator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from afterthread.config import Settings
from afterthread.models import ToolRun
from afterthread.services import llm_log, tool_builder, tool_runs, tools

_VID = "20260728T010203Z-abcdef"


@pytest.fixture(autouse=True)
def _reset_state() -> Generator[None]:
    llm_log._reset_for_tests()
    tools._ADVERTISEMENT_GENERATIONS.clear()
    tools.set_run_recorder(None)
    tool_builder._reset_jobs_for_tests()
    yield
    tools.set_run_recorder(None)
    tools._ADVERTISEMENT_GENERATIONS.clear()
    tool_builder._reset_jobs_for_tests()
    llm_log._reset_for_tests()


def _make_tool(
    root: Path,
    name: str,
    run_py: str,
    *,
    examples: Any = None,
    enabled: bool = True,
) -> Path:
    """One installed package; ``examples`` is written as examples.json when given."""
    package = root / name
    version = package / tools._VERSIONS_DIRNAME / _VID
    version.mkdir(parents=True)
    (version / "run.py").write_text(run_py, encoding="utf-8")
    (version / "tool.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": "test tool",
                "parameters": {"type": "object", "properties": {}},
                "entry": [sys.executable, "run.py"],
            }
        ),
        encoding="utf-8",
    )
    if examples is not None:
        text = examples if isinstance(examples, str) else json.dumps(examples)
        (version / "examples.json").write_text(text, encoding="utf-8")
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
    meta = package / tools._META_DIRNAME
    meta.mkdir()
    (meta / tools._CURRENT_FILENAME).write_text(f"{_VID}\n", encoding="ascii")
    (meta / tools._PACKAGE_STATE_FILENAME).write_text(
        json.dumps({tools._STATE_MARKER_KEY: tools._STATE_MARKER_VALUE, "enabled": enabled}),
        encoding="utf-8",
    )
    return package


def _settings(monkeypatch: pytest.MonkeyPatch, root: Path, **overrides: Any) -> Settings:
    settings = Settings(tools_dir=str(root), **overrides)
    monkeypatch.setattr("afterthread.services.tools.get_settings", lambda: settings)
    monkeypatch.setattr("afterthread.services.tool_builder.get_settings", lambda: settings)
    return settings


def _examples(*names: str) -> dict[str, Any]:
    return {"examples": [{"name": name, "args": {"q": name}} for name in names]}


@pytest.fixture
def stored(session: Session) -> Callable[[], list[ToolRun]]:
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    tools.set_run_recorder(partial(tool_runs.record, engine=engine))

    def _rows() -> list[ToolRun]:
        session.expire_all()
        return list(session.exec(select(ToolRun).order_by(col(ToolRun.id))))

    return _rows


def _check(name: str, **kwargs: Any) -> tools.CheckResult:
    return asyncio.run(
        tools.run_examples(
            name,
            check_id=kwargs.pop("check_id", "check-1"),
            workflow=kwargs.pop("workflow", "health_check"),
            budget_seconds=kwargs.pop("budget_seconds", 60.0),
            **kwargs,
        )
    )


# --- examples.json -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "error_part"),
    [
        ("{not json", "不是合法的 JSON"),
        ('{"cases": []}', "examples 陣列"),
        (json.dumps({"examples": [{"name": "a", "args": {}}] * 11}), "最多 10 個"),
        ('{"examples": [{"name": "", "args": {}}]}', "第 1 個範例"),
        ('{"examples": [{"name": "a", "args": [1]}]}', "第 1 個範例"),
    ],
)
def test_an_unusable_examples_file_is_an_error_not_a_partial_list(
    tmp_path: Path, content: str, error_part: str
) -> None:
    package = _make_tool(tmp_path, "kb", "print('x')\n", examples=content)
    loaded = tools.load_examples(tools.VersionRoot(package / "versions" / _VID))
    assert loaded.examples == ()
    assert loaded.error is not None and error_part in loaded.error


def test_examples_load_in_order_and_a_missing_file_is_none(tmp_path: Path) -> None:
    with_file = _make_tool(
        tmp_path, "kb", "print('x')\n", examples=_examples("一般查詢", "查無結果")
    )
    without = _make_tool(tmp_path, "web", "print('x')\n")

    loaded = tools.load_examples(tools.VersionRoot(with_file / "versions" / _VID))
    assert [(example.name, example.args) for example in loaded.examples] == [
        ("一般查詢", {"q": "一般查詢"}),
        ("查無結果", {"q": "查無結果"}),
    ]
    assert (
        tools.load_examples(tools.VersionRoot(without / "versions" / _VID)) == tools.ExamplesFile()
    )


# --- P: the same path, only the examples ---------------------------------------------


def test_a_check_runs_each_example_through_the_tool_path_and_records_the_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stored: Callable[[], list[ToolRun]]
) -> None:
    root = tmp_path / "tools"
    _make_tool(
        root,
        "kb",
        "import sys, json\nargs = json.load(sys.stdin)\nsys.exit(1 if args['q'] == 'bad' else 0)\n",
        examples=_examples("good", "bad"),
    )
    _settings(monkeypatch, root)

    result = _check("kb", check_id="batch-7")

    assert result.status == "ran"
    assert [(example.name, example.outcome) for example in result.results] == [
        ("good", tools.RUN_OK),
        ("bad", tools.RUN_FAILED),
    ]
    rows = stored()
    assert [(row.workflow, row.check_id, row.outcome) for row in rows] == [
        ("health_check", "batch-7", "ok"),
        ("health_check", "batch-7", "failed"),
    ]
    assert rows[1].args == '{"q": "bad"}'
    assert [example.run_id for example in result.results] == [rows[0].id, rows[1].id]


def test_a_disabled_tool_is_not_run_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stored: Callable[[], list[ToolRun]]
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "kb", "print('x')\n", examples=_examples("a"), enabled=False)
    _settings(monkeypatch, root)

    assert _check("kb").status == "disabled"
    assert stored() == []


def test_a_check_against_a_version_that_is_no_longer_current_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stored: Callable[[], list[ToolRun]]
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "kb", "print('x')\n", examples=_examples("a"))
    _settings(monkeypatch, root)

    result = _check("kb", expected_vid="20260101T000000Z-000000")

    assert (result.status, result.vid) == ("version_changed", _VID)
    assert stored() == []


# --- S: the budget ----------------------------------------------------------------


def test_after_the_first_timeout_the_rest_are_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stored: Callable[[], list[ToolRun]]
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "kb", "import time\ntime.sleep(30)\n", examples=_examples("a", "b", "c"))
    _settings(monkeypatch, root, llm_tool_timeout_seconds=0.3)

    started = time.monotonic()
    result = _check("kb")
    elapsed = time.monotonic() - started

    assert [example.outcome for example in result.results] == ["timeout", "skipped", "skipped"]
    assert elapsed < 5
    assert len(stored()) == 1  # the skipped ones never ran


def test_past_the_budget_the_remaining_examples_are_not_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stored: Callable[[], list[ToolRun]]
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "kb", "import time\ntime.sleep(0.5)\n", examples=_examples("a", "b", "c"))
    _settings(monkeypatch, root)

    result = _check("kb", budget_seconds=0.8)

    assert [example.outcome for example in result.results] == ["ok", "ok", "not_run"]
    assert result.budget_exhausted is True
    assert len(stored()) == 2


# --- the health-check job -----------------------------------------------------------


async def _wait_for_job(job_id: str) -> dict[str, Any]:
    for _ in range(1000):
        job = tool_builder.get_job(job_id)
        assert job is not None
        if job["state"] not in ("queued", "running"):
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("the job did not finish")


def test_the_health_check_job_reports_each_example(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stored: Callable[[], list[ToolRun]]
) -> None:
    root = tmp_path / "tools"
    _make_tool(
        root,
        "kb",
        "import sys, json\nsys.exit(0 if json.load(sys.stdin)['q'] == 'good' else 2)\n",
        examples=_examples("good", "bad"),
    )
    _settings(monkeypatch, root)

    async def scenario() -> dict[str, Any]:
        started = await tool_builder.start_health_check_job("kb", _VID)
        assert started.job_id is not None
        return await _wait_for_job(started.job_id)

    job = asyncio.run(scenario())

    assert (job["kind"], job["state"], job["tool_name"]) == ("health_check", "succeeded", "kb")
    verification = job["verification"]
    assert verification["finished"] is True
    assert (verification["completed"], verification["total"]) == (2, 2)
    assert [(run["name"], run["outcome"]) for run in verification["runs"]] == [
        ("good", "ok"),
        ("bad", "failed"),
    ]
    assert {row.check_id for row in stored()} == {job["job_id"]}


@pytest.mark.parametrize(
    ("examples", "enabled", "message_part"),
    [
        (None, True, "沒有 examples.json"),
        ("{bad", True, "不是合法的 JSON"),
        (_examples("a"), False, "工具已停用"),
    ],
)
def test_the_health_check_is_refused_when_there_is_nothing_to_run(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    examples: Any,
    enabled: bool,
    message_part: str,
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "kb", "print('x')\n", examples=examples, enabled=enabled)
    _settings(monkeypatch, root)

    response = client.post("/api/tools/kb/check", json={"expected_vid": _VID})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "check_unavailable"
    assert message_part in response.json()["detail"]["message"]
    assert tool_builder._JOBS == {}
    assert not tool_builder._SYNC_OPS


def test_the_health_check_route_checks_version_and_existence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "kb", "print('x')\n", examples=_examples("a"))
    _settings(monkeypatch, root)

    stale = client.post("/api/tools/kb/check", json={"expected_vid": "20260101T000000Z-000000"})
    missing = client.post("/api/tools/nope/check", json={"expected_vid": _VID})

    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "version_mismatch"
    assert missing.status_code == 404


# --- T: no delete or discard while a check runs ----------------------------------------


def test_delete_and_discard_are_refused_while_a_check_runs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    gate = tmp_path / "running"
    release = tmp_path / "release"
    package = _make_tool(
        root,
        "kb",
        "import os, time\n"
        f"open({str(gate)!r}, 'w').close()\n"
        f"while not os.path.exists({str(release)!r}):\n    time.sleep(0.02)\n",
        examples=_examples("a"),
    )
    _settings(monkeypatch, root)
    before = sorted(str(path) for path in package.rglob("*"))
    result: dict[str, tools.CheckResult] = {}

    runner = threading.Thread(target=lambda: result.update(check=_check("kb")))
    runner.start()
    try:
        deadline = time.monotonic() + 10
        while not gate.exists():
            assert time.monotonic() < deadline, "the example never started"
            time.sleep(0.02)
        deleted = client.delete("/api/tools/kb")
        assert tools.delete_tool("kb") == "ai_job_in_progress"
    finally:
        release.touch()
        runner.join(timeout=30)

    assert deleted.status_code == 409
    assert deleted.json()["detail"]["code"] == "ai_job_in_progress"
    assert sorted(str(path) for path in package.rglob("*")) == before
    assert result["check"].status == "ran"


def test_a_running_health_check_holds_the_slot_against_a_discard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "kb", "import time\ntime.sleep(0.5)\n", examples=_examples("a"))
    _settings(monkeypatch, root)

    async def scenario() -> tuple[str | None, str | None]:
        started = await tool_builder.start_health_check_job("kb", _VID)
        busy = tool_builder.reserve_sync_operation()
        if busy is not None:
            tool_builder.release_sync_operation(busy)
        assert started.job_id is not None
        await _wait_for_job(started.job_id)
        return started.job_id, busy

    job_id, reservation = asyncio.run(scenario())
    assert job_id is not None
    assert reservation is None  # a discard reserves the same slot first: job_busy


# --- the verification after a published fix -----------------------------------------


def test_verification_state_follows_the_newest_batch_of_the_current_version(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stored: Callable[[], list[ToolRun]],
) -> None:
    root = tmp_path / "tools"
    package = _make_tool(
        root,
        "kb",
        "import sys, json\nsys.exit(0 if json.load(sys.stdin)['q'] != 'bad' else 1)\n",
        examples=_examples("a", "bad"),
    )
    _settings(monkeypatch, root)

    def state() -> dict[str, Any]:
        return client.get("/api/tools/kb/runs").json()["verification"]

    assert state()["state"] == "never"
    _check("kb", check_id="first")
    failed = state()
    assert (failed["state"], failed["passed"], failed["total"]) == ("failed", 1, 2)

    (package / "versions" / _VID / "examples.json").write_text(
        json.dumps(_examples("a", "b", "c")), encoding="utf-8"
    )
    _check("kb", check_id="second", budget_seconds=60.0)
    passed = state()
    assert (passed["state"], passed["passed"], passed["ran"]) == ("passed", 3, 3)
    assert passed["workflow"] == "health_check"

    (package / "versions" / _VID / "examples.json").write_text(
        json.dumps(_examples("a", "b", "c", "d")), encoding="utf-8"
    )
    assert state()["state"] == "incomplete"  # the newest batch ran 3 of 4
    os.remove(package / "versions" / _VID / "examples.json")
    assert state()["state"] == "no_examples"
