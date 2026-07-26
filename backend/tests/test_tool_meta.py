"""Tests for the tool-summary generator (afterthread.services.tool_meta).

Two layers:

* the PROMPT -- built against a real tmp package so the file inventory runs for
  real: what rides in it (the manifest, the implementation files, the ``.env``
  KEY names, the install origin) and, more importantly, what must NOT (``.env``
  VALUES, the sidecar's own previous output, any known secret);
* the STORE -- ``generate_structured`` is monkeypatched (at the name imported
  INTO tool_meta, which is a different module reference from tool_builder's own)
  so the sidecar-writing contract runs for real: a success writes a draft
  sidecar linked to its session's log record, a failure never clobbers a good
  summary, the install hook never raises whatever goes wrong, and the
  synchronous regenerate surfaces LLM failures instead of swallowing them.

Async entry points are driven with ``asyncio.run`` (no pytest-asyncio plugin,
matching the suite). The llm_log ring and the known-secret registries are
process-wide singletons, so an autouse fixture resets them around every test.
"""

import asyncio
import json
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from afterthread.config import Settings
from afterthread.services import llm_log, tool_meta, tools
from afterthread.services.llm import LLMNotConfiguredError, LLMUpstreamError
from afterthread.services.tool_meta import (
    ToolSummaryResult,
    generate_and_store_summary,
    regenerate_summary,
)


@pytest.fixture(autouse=True)
def _reset_singletons() -> Generator[None]:
    """Empty the llm_log ring and the process-wide known-secret registries
    around every test (all module-level singletons this module touches)."""
    llm_log._reset_for_tests()
    tools._INFLIGHT_SECRETS.clear()
    tools._ENV_VALUE_CACHE.clear()
    yield
    llm_log._reset_for_tests()
    tools._INFLIGHT_SECRETS.clear()
    tools._ENV_VALUE_CACHE.clear()


def _summary_settings(monkeypatch: pytest.MonkeyPatch, root: Path, **overrides: Any) -> Settings:
    """Point BOTH settings readers (the registry and the summary generator) at
    one value, so their view of the tools dir / prompt budget never disagrees."""
    settings = Settings(tools_dir=str(root), **overrides)
    for target in (
        "afterthread.services.tools.get_settings",
        "afterthread.services.tool_meta.get_settings",
    ):
        monkeypatch.setattr(target, lambda settings=settings: settings)
    return settings


def _package(
    root: Path,
    name: str = "kbsearch",
    *,
    run_py: str = "import sys\nsys.stdout.write('x')\n",
    dotenv: str | None = None,
) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "run.py").write_text(run_py, encoding="utf-8")
    (pkg / "tool.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": "searches the KB",
                "parameters": {"type": "object", "properties": {}},
                "entry": [sys.executable, "run.py"],
            }
        ),
        encoding="utf-8",
    )
    if dotenv is not None:
        (pkg / ".env").write_text(dotenv, encoding="utf-8")
    return pkg


def _fake_generate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    summary: str = "這個工具會查 KB",
    explode: BaseException | None = None,
    record_session: bool = True,
) -> dict[str, Any]:
    """Stub the SUMMARY session's generate_structured and capture its kwargs.

    Patches the name imported INTO tool_meta (not llm's own), which is the
    reference the module actually calls. ``record_session`` writes a genuine
    llm_log record for the workflow it was called with, so the sidecar's
    llm_log_id wiring is exercised against the real ring rather than a stub id.
    """
    captured: dict[str, Any] = {}

    async def fake(
        system_prompt: str,
        user_prompt: str,
        model_cls: type[BaseModel],
        *,
        workflow: str = "unknown",
        tools: Any = None,
        max_tool_rounds: int | None = None,
        timeout_seconds: float | None = None,
    ) -> BaseModel:
        captured.update(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            workflow=workflow,
            tools=tools,
            max_tool_rounds=max_tool_rounds,
            timeout_seconds=timeout_seconds,
        )
        if record_session:
            recorder = llm_log.LlmInteractionRecorder(workflow=workflow, model="m")
            recorder.begin_attempt([{"role": "user", "content": "summarize"}])
            recorder.record_response(summary)
            recorder.finish(outcome="ok" if explode is None else "error", error=None)
        if explode is not None:
            raise explode
        return model_cls.model_validate({"summary": summary})

    monkeypatch.setattr("afterthread.services.tool_meta.generate_structured", fake)
    return captured


# --- the result model ---------------------------------------------------------


def test_summary_result_sanitizes_and_caps() -> None:
    result = ToolSummaryResult.model_validate({"summary": "  spaced  "})
    assert result.summary == "spaced"
    long = ToolSummaryResult.model_validate({"summary": "y" * (tool_meta._TOOL_SUMMARY_CAP + 500)})
    assert len(long.summary) == tool_meta._TOOL_SUMMARY_CAP


def test_summary_result_redacts_before_capping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redact-then-cap: a secret straddling the slice edge must be masked while
    the text is still whole, or the cut leaves an unmatchable fragment."""
    secret = "live-secret-abcdef"
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))
    padding = "y" * (tool_meta._TOOL_SUMMARY_CAP - 4)
    result = ToolSummaryResult.model_validate({"summary": padding + secret})
    assert secret not in result.summary
    assert secret[:8] not in result.summary


@pytest.mark.parametrize("value", ["", "   ", None], ids=["empty", "blank", "missing"])
def test_summary_result_rejects_empty_summary(value: str | None) -> None:
    """An empty summary is indistinguishable from the placeholder a FAILED
    generation writes, so it is rejected -- which is what triggers
    generate_structured's one corrective retry."""
    with pytest.raises(ValidationError):
        ToolSummaryResult.model_validate({"summary": value})


def test_summary_result_rejects_non_object() -> None:
    with pytest.raises(ValidationError):
        ToolSummaryResult.model_validate(["not", "an", "object"])


# --- the prompt ---------------------------------------------------------------


def test_user_prompt_carries_the_package_but_never_env_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The prompt is built from the files on disk: the manifest and every
    implementation file ride in it, the ``.env``'s KEY names ride in it, and its
    VALUES never do."""
    root = tmp_path / "tools"
    secret = "kb-live-secret-abcdef"
    pkg = _package(
        root,
        run_py="import os\nprint(os.environ['KB_API_KEY'])\n",
        dotenv=f"KB_API_KEY={secret}\nKB_BASE=https://kb.example\n",
    )
    _summary_settings(monkeypatch, root)

    prompt = tool_meta._summary_user_prompt(
        "kbsearch",
        pkg,
        origin={"openapi_url": "http://kb.example/openapi.json", "instructions": "查 KB"},
        builder_summary="built and tested",
    )

    assert "searches the KB" in prompt  # tool.json content
    assert "os.environ['KB_API_KEY']" in prompt  # run.py content
    assert "KB_API_KEY" in prompt and "KB_BASE" in prompt  # .env KEY names
    assert secret not in prompt  # ... never its values
    assert "http://kb.example/openapi.json" in prompt  # install origin
    assert "查 KB" in prompt
    assert "built and tested" in prompt


def test_user_prompt_skips_the_sidecar_and_dot_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A summary must never be fed its own previous output as if it were source
    (nor any other backend-internal dot-file)."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    tools.write_tool_meta(pkg, {"summary": "PREVIOUS-SUMMARY-TEXT", "status": "draft"})
    (pkg / ".hidden-note").write_text("HIDDEN-FILE-TEXT", encoding="utf-8")

    prompt = tool_meta._summary_user_prompt("kbsearch", pkg, origin=None, builder_summary=None)

    assert "PREVIOUS-SUMMARY-TEXT" not in prompt
    assert "HIDDEN-FILE-TEXT" not in prompt
    assert tools._AI_META_FILENAME not in prompt


def test_user_prompt_redacts_known_secrets_in_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A value another tool registered must be masked out of THIS package's
    files before they reach the model."""
    root = tmp_path / "tools"
    secret = "another-tools-live-secret-abcdef"
    _package(root, "other", dotenv=f"OTHER_KEY={secret}\n")
    pkg = _package(root, "kbsearch", run_py=f"TOKEN = '{secret}'\n")
    _summary_settings(monkeypatch, root)

    prompt = tool_meta._summary_user_prompt("kbsearch", pkg, origin=None, builder_summary=None)
    assert secret not in prompt
    assert tools._REDACTION_MARKER in prompt


def test_user_prompt_bounded_by_the_prompt_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Files are capped per file AND the whole prompt is capped by the
    operator's own llm_prompt_budget_tokens allowance."""
    root = tmp_path / "tools"
    pkg = _package(root, run_py="z" * (tool_meta._FILE_CONTENT_CAP * 2))
    (pkg / "extra.py").write_text("w" * (tool_meta._FILE_CONTENT_CAP * 2), encoding="utf-8")
    _summary_settings(monkeypatch, root, llm_prompt_budget_tokens=4_000)

    prompt = tool_meta._summary_user_prompt("kbsearch", pkg, origin=None, builder_summary=None)
    assert len(prompt) <= 4_000


def test_user_prompt_bounds_the_file_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "tools"
    pkg = _package(root)
    for index in range(tool_meta._MAX_FILES + 10):
        (pkg / f"file{index:03d}.py").write_text(f"# {index}\n", encoding="utf-8")
    _summary_settings(monkeypatch, root)

    assert len(tool_meta._package_files(pkg)) == tool_meta._MAX_FILES


# --- generation + storage -----------------------------------------------------


def test_generate_and_store_summary_writes_a_draft_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The happy path: a draft sidecar carrying the summary, the origin, and a
    link to the summary session's own AI 日誌 record."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    captured = _fake_generate(monkeypatch, summary="這個工具會查 KB")

    asyncio.run(
        generate_and_store_summary(
            "kbsearch",
            origin={"openapi_url": "http://kb.example/openapi.json", "instructions": "查 KB"},
            builder_summary="built it",
        )
    )

    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["summary"] == "這個工具會查 KB"
    assert meta["status"] == "draft"
    assert meta["origin"] == {
        "openapi_url": "http://kb.example/openapi.json",
        "instructions": "查 KB",
    }
    assert meta["updated_at"]
    # Linked to the genuine record of THIS session, found by its own workflow name.
    assert meta["llm_log_id"] == llm_log.last_record_id_for_workflow("tool_summary")
    assert meta["llm_log_id"] is not None
    # The summary session runs under its OWN workflow, with no tools and the
    # default timeout (it only reads what the prompt already carries).
    assert captured["workflow"] == "tool_summary"
    assert captured["tools"] is None
    assert captured["timeout_seconds"] is None
    assert captured["max_tool_rounds"] is None


def test_summary_workflow_never_collides_with_the_install_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The two sessions of one install job are told apart by NAME: an install
    record written first must stay the newest `tool_install` record after the
    summary session finishes, or the install outcome's llm_log_id would point at
    the summary instead of the build."""
    root = tmp_path / "tools"
    _package(root)
    _summary_settings(monkeypatch, root)
    recorder = llm_log.LlmInteractionRecorder(workflow="tool_install", model="m")
    recorder.begin_attempt([{"role": "user", "content": "build"}])
    recorder.finish(outcome="ok", error=None)
    install_record = llm_log.last_record_id_for_workflow("tool_install")

    _fake_generate(monkeypatch)
    asyncio.run(generate_and_store_summary("kbsearch"))

    assert llm_log.last_record_id_for_workflow("tool_install") == install_record
    assert llm_log.last_record_id_for_workflow("tool_summary") != install_record


def test_generate_and_store_summary_preserves_status_and_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A regeneration keeps the status it had and inherits the origin the
    INSTALL captured (the only copy of the OpenAPI url / instructions)."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    origin = {"openapi_url": "http://kb.example/openapi.json", "instructions": "查 KB"}
    tools.write_tool_meta(pkg, {"summary": "舊的", "status": "final", "origin": origin})

    _fake_generate(monkeypatch, summary="新的")
    asyncio.run(generate_and_store_summary("kbsearch", origin=None))

    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["summary"] == "新的"
    assert meta["status"] == "final"  # preserved, never reset to draft
    assert meta["origin"] == origin  # inherited, never erased


def test_generate_and_store_summary_writes_placeholder_when_no_sidecar_yet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed FIRST generation still leaves a sidecar (empty summary + origin
    + the failed session's log id), so the page can offer 重新產生."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    _fake_generate(monkeypatch, explode=LLMNotConfiguredError("nope"))

    asyncio.run(generate_and_store_summary("kbsearch", origin={"instructions": "查 KB"}))

    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["summary"] == ""
    assert meta["status"] == "draft"
    assert meta["origin"] == {"instructions": "查 KB"}


def test_generate_and_store_summary_never_clobbers_a_good_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transient LLM failure must leave the previous summary untouched -- the
    one thing the best-effort path must never do is blank a good summary."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    tools.write_tool_meta(pkg, {"summary": "先前的好總結", "status": "draft"})

    _fake_generate(monkeypatch, explode=LLMUpstreamError("APIConnectionError: unreachable"))
    asyncio.run(generate_and_store_summary("kbsearch"))

    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["summary"] == "先前的好總結"


@pytest.mark.parametrize(
    "explode",
    [RuntimeError("bug"), LLMNotConfiguredError("off"), LLMUpstreamError("Timeout: slow")],
    ids=["bug", "not-configured", "upstream"],
)
def test_generate_and_store_summary_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explode: BaseException
) -> None:
    """It runs AFTER the package is already installed, so nothing it hits may
    escape and flip a successful install to failed."""
    root = tmp_path / "tools"
    _package(root)
    _summary_settings(monkeypatch, root)
    _fake_generate(monkeypatch, explode=explode)

    asyncio.run(generate_and_store_summary("kbsearch"))  # must not raise


def test_generate_and_store_summary_never_raises_when_prompt_building_explodes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The backstop covers the non-LLM half too: a failing secret provider makes
    the fail-closed redactor raise while the prompt is being built."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)

    def boom() -> frozenset[str]:
        raise RuntimeError("provider down")

    monkeypatch.setattr(tools, "known_secret_values", boom)
    _fake_generate(monkeypatch)

    asyncio.run(generate_and_store_summary("kbsearch"))  # must not raise
    # Nothing could be written either (the sidecar write is fail-closed too).
    assert not (pkg / tools._AI_META_FILENAME).exists()


def test_generate_and_store_summary_silent_when_package_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A racing delete between promote and summary: nothing to summarize, and
    nothing is written (no ghost package directory is resurrected)."""
    root = tmp_path / "tools"
    root.mkdir()
    _summary_settings(monkeypatch, root)

    async def must_not_generate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no session may start for a package that is gone")

    monkeypatch.setattr("afterthread.services.tool_meta.generate_structured", must_not_generate)
    asyncio.run(generate_and_store_summary("ghost"))
    assert list(root.iterdir()) == []


def test_generate_and_store_summary_redacts_into_the_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A model that echoed a live secret into its summary never reaches disk
    with it -- the sidecar would otherwise brick every later revise of this
    package on the embedded-secret gate."""
    root = tmp_path / "tools"
    secret = "kb-live-secret-abcdef"
    pkg = _package(root, dotenv=f"KB_API_KEY={secret}\n")
    _summary_settings(monkeypatch, root)
    _fake_generate(monkeypatch, summary=f"it authenticates with {secret}")

    asyncio.run(generate_and_store_summary("kbsearch"))

    stored = (pkg / tools._AI_META_FILENAME).read_text(encoding="utf-8")
    assert secret not in stored
    assert tools._REDACTION_MARKER in stored


# --- the synchronous regenerate -----------------------------------------------


def test_regenerate_summary_returns_the_fresh_meta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    tools.write_tool_meta(
        pkg,
        {"summary": "舊的", "status": "draft", "origin": {"instructions": "查 KB"}},
    )
    _fake_generate(monkeypatch, summary="新的說明")

    meta = asyncio.run(regenerate_summary("kbsearch"))

    assert meta["summary"] == "新的說明"
    assert meta["status"] == "draft"
    assert meta["origin"] == {"instructions": "查 KB"}  # inherited from the install
    assert tools.read_tool_meta(pkg) == meta  # what it returned IS what it stored


@pytest.mark.parametrize(
    "explode, expected",
    [
        (LLMNotConfiguredError("off"), LLMNotConfiguredError),
        (LLMUpstreamError("Timeout: slow"), LLMUpstreamError),
    ],
    ids=["not-configured", "upstream"],
)
def test_regenerate_summary_propagates_llm_failures_without_clobbering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    explode: BaseException,
    expected: type[BaseException],
) -> None:
    """Someone is waiting on this request, so the failure must reach them as a
    503/502 -- while the previous summary survives untouched."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    tools.write_tool_meta(pkg, {"summary": "先前的好總結", "status": "draft"})
    _fake_generate(monkeypatch, explode=explode)

    with pytest.raises(expected):
        asyncio.run(regenerate_summary("kbsearch"))

    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["summary"] == "先前的好總結"


def test_regenerate_summary_empty_when_package_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The route already 404s a missing tool; this is the race backstop, and it
    must not write a sidecar into a deleted package's path."""
    root = tmp_path / "tools"
    root.mkdir()
    _summary_settings(monkeypatch, root)
    _fake_generate(monkeypatch)

    assert asyncio.run(regenerate_summary("ghost")) == {}
    assert list(root.iterdir()) == []
