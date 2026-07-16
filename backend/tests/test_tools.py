"""Tests for the tool-calling loop (context_memory.services.llm) and the tool
runtime (context_memory.services.tools), plus the memory_ai workflow wiring.

Three layers:

* the TOOL LOOP inside ``generate_structured`` -- driven with the same stubbed
  ``chat.completions.create`` pattern as test_llm_service, scripting a sequence
  of completions (some carrying tool_calls, some a final content reply) so a
  round-trip, a raising handler, malformed arguments, the round cap, the
  timeout override, and the tools-disabled byte-identity are each exercised
  without any network or subprocess;
* the RUNTIME -- real tiny tool packages written to a tmp dir whose ``entry``
  echoes stdin, exits non-zero, sleeps past the timeout, over-produces, or
  prints its environment, plus the name-regex / path-traversal hard-blocks on
  ``set_enabled`` / ``delete_tool`` and the invalid-package handling;
* the WIRING -- with a stub registry, ``capture``/``enrich``/``assist_update``
  append the tools rule (and pass the tools) exactly when a tool is active, and
  are byte-identical to the pinned prompt constants when it is not.

``generate_structured`` is async; each call is driven with ``asyncio.run`` (no
pytest-asyncio plugin, matching the rest of the suite). The module-level llm_log
ring is process-wide, so an autouse fixture resets it around every test.
"""

import asyncio
import json
import sys
import time
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from context_memory.config import Settings
from context_memory.services import llm_log
from context_memory.services.llm import (
    _MAX_TOOL_CALLS_ACCEPTED,
    _MAX_TOOL_CALLS_PER_REPLY,
    _TOO_MANY_TOOL_CALLS,
    _TOOL_BUDGET_EXHAUSTED,
    LlmTool,
    LLMUpstreamError,
    generate_structured,
)
from context_memory.services.memory_ai import (
    _TOOLS_RULE,
    CAPTURE_SYSTEM_PROMPT,
    ENRICH_SYSTEM_PROMPT,
    UPDATE_SYSTEM_PROMPT,
    assist_update,
    capture_draft,
    enrich_item,
)
from context_memory.services.tools import (
    _MANIFEST_MAX_BYTES,
    _PARAMETERS_SCHEMA_MAX_BYTES,
    delete_tool,
    enabled_llm_tools,
    list_tools,
    set_enabled,
    tools_dir,
)

_URL = "http://llm.internal.example/v1"
_KEY = "sk-super-secret-do-not-leak"
_MODEL = "test-model"


@pytest.fixture(autouse=True)
def _reset_log() -> Generator[None]:
    """Empty the ring and id counter around every test (it is a singleton)."""
    llm_log._reset_for_tests()
    yield
    llm_log._reset_for_tests()


# --- target model + tool-loop stubs ----------------------------------------


class _Sample(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    count: int = 0


_SAMPLE_JSON = json.dumps({"title": "Draft", "count": 2})


def _tc(name: str, arguments: str, *, tc_id: str = "call_1") -> SimpleNamespace:
    """A response-side tool-call object (SDK-shaped: id/type/function.*)."""
    return SimpleNamespace(
        id=tc_id, type="function", function=SimpleNamespace(name=name, arguments=arguments)
    )


def _tool_calls_completion(*tool_calls: SimpleNamespace) -> SimpleNamespace:
    """A completion whose assistant message carries tool_calls and null content."""
    message = SimpleNamespace(content=None, tool_calls=list(tool_calls))
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _content_completion(content: str | None) -> SimpleNamespace:
    """A normal completion: assistant message with content and no tool_calls."""
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _ScriptedCompletions:
    """A ``chat.completions`` whose ``create`` returns a scripted sequence.

    Records every call's kwargs (so a test can assert what messages / tools rode
    on each round) and holds the LAST scripted completion once exhausted.
    """

    def __init__(self, completions: list[Any], *, delay: float = 0.0) -> None:
        self._completions = list(completions)
        self._delay = delay
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if len(self._completions) == 1:
            return self._completions[0]
        return self._completions.pop(0)


class _ScriptedClient:
    def __init__(self, completions: list[Any], *, delay: float = 0.0) -> None:
        self.chat = SimpleNamespace(completions=_ScriptedCompletions(completions, delay=delay))


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "openai_base_url": _URL,
        "openai_api_key": _KEY,
        "openai_model": _MODEL,
    }
    base.update(overrides)
    return Settings(**base)


def _install(
    monkeypatch: pytest.MonkeyPatch, client: _ScriptedClient, *, settings: Settings | None = None
) -> _ScriptedClient:
    settings = settings or _settings()
    monkeypatch.setattr("context_memory.services.llm.get_settings", lambda: settings)
    monkeypatch.setattr("context_memory.services.llm._get_client", lambda: client)
    return client


def _run(**kwargs: Any) -> _Sample:
    return asyncio.run(generate_structured("system", "user", _Sample, **kwargs))


def _tool_spec(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": "d", "parameters": {"type": "object"}},
    }


def _echo_tool() -> LlmTool:
    async def handler(args: dict[str, Any]) -> str:
        return "TOOL_RESULT:" + json.dumps(args, sort_keys=True)

    return LlmTool(spec=_tool_spec("echo"), handler=handler)


def _calls(client: _ScriptedClient) -> list[dict[str, Any]]:
    return client.chat.completions.calls


def _tool_messages(call: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in call["messages"] if m.get("role") == "tool"]


# --- tool loop -------------------------------------------------------------


def test_tool_call_roundtrip_feeds_result_back_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool_calls reply is executed, its result appended, and the model asked
    again; the tool RESULT rides in the second create()'s messages and the final
    JSON parses -- all under one interaction."""
    client = _install(
        monkeypatch,
        _ScriptedClient(
            [
                _tool_calls_completion(_tc("echo", json.dumps({"q": "term"}))),
                _content_completion(_SAMPLE_JSON),
            ]
        ),
    )
    result = _run(tools=[_echo_tool()])

    assert isinstance(result, _Sample)
    assert result.title == "Draft"
    calls = _calls(client)
    assert len(calls) == 2
    assert "tools" in calls[0]  # first round advertised tools
    tool_msgs = _tool_messages(calls[1])
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == "TOOL_RESULT:" + json.dumps({"q": "term"}, sort_keys=True)
    # the assistant tool-call turn was echoed back before the tool result
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in calls[1]["messages"])


def test_tool_round_records_synthetic_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool_calls attempt's recorded response is the compact synthetic trace,
    so the AI 日誌 shows the agentic step."""
    _install(
        monkeypatch,
        _ScriptedClient(
            [
                _tool_calls_completion(_tc("echo", json.dumps({"q": "x"}))),
                _content_completion(_SAMPLE_JSON),
            ]
        ),
    )
    _run(tools=[_echo_tool()])

    summaries = llm_log.list_summaries(10)
    assert summaries
    record = llm_log.get_record(summaries[0]["id"])
    assert record is not None
    assert record["attempts"][0]["response_content"].startswith("[tool_calls] echo(")


def test_tool_handler_raise_feeds_safe_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handler that raises (against its contract) does NOT 502 the interaction:
    a safe ``tool execution failed: <ClassName>`` result is fed back instead."""

    async def boom(args: dict[str, Any]) -> str:
        raise RuntimeError("kaboom")

    client = _install(
        monkeypatch,
        _ScriptedClient(
            [_tool_calls_completion(_tc("boom", "{}")), _content_completion(_SAMPLE_JSON)]
        ),
    )
    result = _run(tools=[LlmTool(spec=_tool_spec("boom"), handler=boom)])

    assert result.title == "Draft"
    assert _tool_messages(_calls(client)[1])[0]["content"] == "tool execution failed: RuntimeError"


def test_tool_malformed_arguments_feeds_error_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed arguments JSON never reaches the handler: an error result is fed
    back for that call instead, and the loop keeps going."""
    seen: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any]) -> str:
        seen.append(args)
        return "should not run"

    client = _install(
        monkeypatch,
        _ScriptedClient(
            [_tool_calls_completion(_tc("echo", "not-json{")), _content_completion(_SAMPLE_JSON)]
        ),
    )
    result = _run(tools=[LlmTool(spec=_tool_spec("echo"), handler=handler)])

    assert result.title == "Draft"
    assert _tool_messages(_calls(client)[1])[0]["content"].startswith("tool call rejected")
    assert seen == []  # handler never saw the junk arguments


def test_tool_rounds_cap_forces_final_create_without_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the round budget is spent the loop stops advertising tools, appends
    the budget-exhausted nudge, and makes ONE final tools-free create()."""
    client = _install(
        monkeypatch,
        _ScriptedClient(
            [
                _tool_calls_completion(_tc("echo", "{}")),
                _tool_calls_completion(_tc("echo", "{}")),
                _content_completion(_SAMPLE_JSON),
            ]
        ),
    )
    result = _run(tools=[_echo_tool()], max_tool_rounds=2)

    assert result.title == "Draft"
    calls = _calls(client)
    assert len(calls) == 3
    assert "tools" in calls[0] and "tools" in calls[1]
    # the forced final create carries NO tools and the budget-exhausted nudge
    assert "tools" not in calls[2]
    assert any(m.get("content") == _TOOL_BUDGET_EXHAUSTED for m in calls[2]["messages"])


def test_tools_available_but_model_answers_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model that answers directly (no tool_calls) while tools are advertised
    finishes in ONE create -- the tool phase is optional."""
    client = _install(monkeypatch, _ScriptedClient([_content_completion(_SAMPLE_JSON)]))
    result = _run(tools=[_echo_tool()])

    assert result.title == "Draft"
    calls = _calls(client)
    assert len(calls) == 1
    assert "tools" in calls[0]


def test_tools_none_sends_no_tools_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    """tools=None (the default) keeps the create() kwargs byte-identical to the
    tool-less build: no ``tools`` key at all."""
    client = _install(monkeypatch, _ScriptedClient([_content_completion(_SAMPLE_JSON)]))
    _run()
    assert "tools" not in _calls(client)[0]


def test_tools_empty_list_sends_no_tools_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty tool list is treated exactly like None -- still no ``tools`` kwarg."""
    client = _install(monkeypatch, _ScriptedClient([_content_completion(_SAMPLE_JSON)]))
    _run(tools=[])
    assert "tools" not in _calls(client)[0]


def test_timeout_override_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    """timeout_seconds overrides settings.openai_timeout_seconds for the ONE
    wall-clock deadline around the whole loop."""
    client = _install(monkeypatch, _ScriptedClient([_content_completion(_SAMPLE_JSON)], delay=1.0))
    started = time.monotonic()
    with pytest.raises(LLMUpstreamError) as excinfo:
        _run(timeout_seconds=0.05)
    elapsed = time.monotonic() - started

    assert str(excinfo.value).startswith("Timeout: ")
    assert elapsed < 0.5  # bounded by the override, not the stub's 1s delay
    assert _calls(client)[0]["model"] == _MODEL  # the request was genuinely attempted


def test_tool_calls_capped_per_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the first _MAX_TOOL_CALLS_PER_REPLY calls in one reply are EXECUTED;
    the rest -- up to _MAX_TOOL_CALLS_ACCEPTED, the whole-reply ceiling -- are
    rejected WITHOUT running but still get a role:tool result (so every tool_call
    in the echoed assistant turn pairs with a result). Driven AT the accepted
    ceiling to pin the boundary: a reply of exactly _MAX_TOOL_CALLS_ACCEPTED calls
    still enters the round; only 65+ is refused outright (see
    test_tool_calls_flood_rejected_as_upstream_error)."""
    seen: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any]) -> str:
        seen.append(args)
        return "ran"

    over = _MAX_TOOL_CALLS_ACCEPTED  # the largest reply the round still enters
    many = [_tc("echo", "{}", tc_id=f"call_{i}") for i in range(over)]
    client = _install(
        monkeypatch,
        _ScriptedClient([_tool_calls_completion(*many), _content_completion(_SAMPLE_JSON)]),
    )
    result = _run(tools=[LlmTool(spec=_tool_spec("echo"), handler=handler)])

    assert result.title == "Draft"
    assert len(seen) == _MAX_TOOL_CALLS_PER_REPLY  # only the first N ran
    tool_msgs = _tool_messages(_calls(client)[1])
    assert len(tool_msgs) == over  # every call still paired with a result
    rejected = [m for m in tool_msgs if m["content"] == _TOO_MANY_TOOL_CALLS]
    assert len(rejected) == over - _MAX_TOOL_CALLS_PER_REPLY


def test_tool_calls_flood_rejected_as_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reply carrying MORE than _MAX_TOOL_CALLS_ACCEPTED tool_calls is abusive:
    the loop refuses to ENTER the tool round at all -- no summarize, no echoed
    assistant turn, no per-call rejected results (each O(N)) -- and instead raises
    on the SAME LLMUpstreamError (502) taxonomy an unparseable reply uses. NO
    handler runs, and the recorder finalizes the interaction as a failure.

    This supersedes the older sleep(0)-starvation test: the entry cap now bounds
    the reply BEFORE the result-building loop, so a 5000-call reply can no longer
    reach that loop to be timed out -- it is refused up front instead."""
    seen: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any]) -> str:
        seen.append(args)
        return "ran"

    flood = [_tc("echo", "{}", tc_id=f"c{i}") for i in range(_MAX_TOOL_CALLS_ACCEPTED + 1)]
    _install(monkeypatch, _ScriptedClient([_tool_calls_completion(*flood)]))

    with pytest.raises(LLMUpstreamError) as excinfo:
        _run(tools=[LlmTool(spec=_tool_spec("echo"), handler=handler)])

    assert str(excinfo.value).startswith("tool_calls flood")
    assert seen == []  # the reply never entered the tool round -- no handler ran
    # The recorder captured the failure: a generic upstream outcome carrying the
    # safe flood category, at both the interaction and attempt level.
    summaries = llm_log.list_summaries(10)
    assert summaries
    record = llm_log.get_record(summaries[0]["id"])
    assert record is not None
    assert record["outcome"] == "upstream_error"
    assert (record["error"] or "").startswith("tool_calls flood")
    assert (record["attempts"][0]["error"] or "").startswith("tool_calls flood")


# --- runtime: real tool packages -------------------------------------------


def _make_tool(
    tools_root: Path,
    name: str,
    run_py: str,
    *,
    tool_json: dict[str, Any] | None = None,
    dotenv: str | None = None,
    enabled: bool = True,
) -> Path:
    """Write a tool package (tool.json + run.py, optional .env) under ``tools_root``.

    The default entry uses ``sys.executable`` (an absolute interpreter path) so
    the subprocess never depends on ``python3`` being resolvable on the scrubbed
    child PATH -- the entry FILE (run.py) still lives inside the package, so
    validation's containment check passes.
    """
    pkg = tools_root / name
    pkg.mkdir(parents=True)
    (pkg / "run.py").write_text(run_py, encoding="utf-8")
    manifest = tool_json
    if manifest is None:
        manifest = {
            "name": name,
            "description": "test tool",
            "parameters": {"type": "object", "properties": {}},
            "entry": [sys.executable, "run.py"],
            "enabled": enabled,
        }
    (pkg / "tool.json").write_text(json.dumps(manifest), encoding="utf-8")
    if dotenv is not None:
        (pkg / ".env").write_text(dotenv, encoding="utf-8")
    return pkg


def _install_tools(monkeypatch: pytest.MonkeyPatch, tools_root: Path, **overrides: Any) -> Settings:
    settings = Settings(tools_dir=str(tools_root), **overrides)
    monkeypatch.setattr("context_memory.services.tools.get_settings", lambda: settings)
    return settings


def test_runtime_happy_path_echoes_stdin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "echo", "import sys\nsys.stdout.write('ECHO:' + sys.stdin.read())\n")
    _install_tools(monkeypatch, root)

    active = enabled_llm_tools()
    assert len(active) == 1
    result = asyncio.run(active[0].handler({"q": "hi"}))
    assert result == "ECHO:" + json.dumps({"q": "hi"}, ensure_ascii=False)


def test_runtime_nonzero_exit_becomes_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "boom", "import sys\nsys.stderr.write('bad things')\nsys.exit(1)\n")
    _install_tools(monkeypatch, root)

    result = asyncio.run(enabled_llm_tools()[0].handler({}))
    assert result == "tool failed (exit 1): bad things"


def test_runtime_timeout_kills_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "slow", "import time\ntime.sleep(30)\n")
    _install_tools(monkeypatch, root, llm_tool_timeout_seconds=0.3)

    started = time.monotonic()
    result = asyncio.run(enabled_llm_tools()[0].handler({}))
    elapsed = time.monotonic() - started

    assert "timed out" in result
    assert elapsed < 5  # killed on expiry, not waited out for 30s


def test_runtime_output_is_capped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "big", "import sys\nsys.stdout.write('a' * 5000)\n")
    _install_tools(monkeypatch, root, llm_tool_output_max_chars=1000)

    result = asyncio.run(enabled_llm_tools()[0].handler({}))
    assert len(result) == 1000
    assert result.endswith("…[工具輸出過長已截斷]")


def test_runtime_unbounded_output_killed_promptly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tool that streams FAR past the cap and never exits on its own is killed
    at the cap (N5): the bounded incremental reader keeps at most ~cap chars and
    SIGKILLs the process group on overflow, so the service never buffers the whole
    runaway stream. The result is the truncated output with the marker."""
    root = tmp_path / "tools"
    # An unbounded writer -- flushes each block so the pipe fills at once, and
    # never exits, so ONLY the overflow kill can stop it.
    _make_tool(
        root,
        "flood",
        "import sys\nwhile True:\n    sys.stdout.write('a' * 4096)\n    sys.stdout.flush()\n",
    )
    _install_tools(monkeypatch, root, llm_tool_output_max_chars=1000)

    started = time.monotonic()
    result = asyncio.run(enabled_llm_tools()[0].handler({}))
    elapsed = time.monotonic() - started

    marker = "…[工具輸出過長已截斷]"
    assert len(result) <= 1000 + len(marker)  # bounded, never the whole stream
    assert result.endswith(marker)
    assert elapsed < 5  # killed at the cap, not read to exhaustion


def test_description_capped_uniformly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An over-long description keeps the package VALID but is capped to 1000
    chars in BOTH the list view and the advertised OpenAI spec."""
    root = tmp_path / "tools"
    _make_tool(
        root,
        "big",
        "import sys\nsys.stdout.write('x')\n",
        tool_json={
            "name": "big",
            "description": "x" * 5000,
            "parameters": {"type": "object"},
            "entry": [sys.executable, "run.py"],
        },
    )
    _install_tools(monkeypatch, root)

    listed = list_tools()[0]
    assert listed["valid"] is True
    assert len(listed["description"]) == 1000
    assert len(enabled_llm_tools()[0].spec["function"]["description"]) == 1000


def test_runtime_env_scrubbed_but_dotenv_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The child sees the tool's own .env secret but NOT OPENAI_API_KEY -- the
    parent environment is never inherited wholesale."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-should-not-leak")
    root = tmp_path / "tools"
    _make_tool(
        root,
        "envtool",
        "import os, sys\n"
        "sys.stdout.write('OPENAI=' + str(os.environ.get('OPENAI_API_KEY')) + "
        "';SECRET=' + str(os.environ.get('TOOL_SECRET')))\n",
        dotenv="TOOL_SECRET=from-dotenv\n",
    )
    _install_tools(monkeypatch, root)

    result = asyncio.run(enabled_llm_tools()[0].handler({}))
    assert "OPENAI=None" in result
    assert "SECRET=from-dotenv" in result
    assert "sk-secret-should-not-leak" not in result


def test_runtime_dotenv_does_not_interpolate_parent_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tool .env value of ``${OPENAI_API_KEY}`` must reach the child as that
    LITERAL string, NOT the real key: python-dotenv's default POSIX
    interpolation would resolve it from the parent os.environ -- reinjecting the
    very credential the from-scratch env exists to exclude (interpolate=False)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-should-not-leak")
    root = tmp_path / "tools"
    _make_tool(
        root,
        "envtool",
        "import os, sys\nsys.stdout.write('LEAK=' + str(os.environ.get('LEAK')))\n",
        dotenv="LEAK=${OPENAI_API_KEY}\n",
    )
    _install_tools(monkeypatch, root)

    result = asyncio.run(enabled_llm_tools()[0].handler({}))
    assert result == "LEAK=${OPENAI_API_KEY}"  # literal, not the resolved key
    assert "sk-secret-should-not-leak" not in result


# --- runtime: validation / listing -----------------------------------------


def test_invalid_packages_listed_invalid_and_excluded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "good", "import sys\nsys.stdout.write('x')\n")

    bad_json = root / "badjson"
    bad_json.mkdir()
    (bad_json / "tool.json").write_text("{not valid json")

    _make_tool(
        root,
        "mism",
        "import sys\nsys.stdout.write('x')\n",
        tool_json={
            "name": "other",  # != directory name
            "description": "d",
            "parameters": {"type": "object"},
            "entry": ["python3", "run.py"],
        },
    )

    noentry = root / "noentry"
    noentry.mkdir()
    (noentry / "tool.json").write_text(
        json.dumps(
            {
                "name": "noentry",
                "description": "d",
                "parameters": {"type": "object"},
                "entry": ["python3", "run.py"],  # run.py not shipped
            }
        )
    )
    _install_tools(monkeypatch, root)

    listed = {t["name"]: t for t in list_tools()}
    assert listed["good"]["valid"] is True
    for broken in ("badjson", "mism", "noentry"):
        assert listed[broken]["valid"] is False
        assert listed[broken]["error"]
    # only the valid+enabled package is executable
    assert [t.spec["function"]["name"] for t in enabled_llm_tools()] == ["good"]


def test_disabled_valid_tool_excluded_from_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "off", "import sys\nsys.stdout.write('x')\n", enabled=False)
    _install_tools(monkeypatch, root)

    listed = list_tools()
    assert listed[0]["valid"] is True
    assert listed[0]["enabled"] is False
    assert enabled_llm_tools() == []


def test_set_enabled_toggles_and_is_reflected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n", enabled=True)
    _install_tools(monkeypatch, root)

    assert len(enabled_llm_tools()) == 1
    assert set_enabled("echo", False) is True
    assert enabled_llm_tools() == []
    assert list_tools()[0]["enabled"] is False
    assert set_enabled("echo", True) is True
    assert len(enabled_llm_tools()) == 1


def test_delete_tool_removes_package(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)

    assert delete_tool("echo") is True
    assert list_tools() == []
    assert delete_tool("echo") is False  # already gone


@pytest.mark.parametrize(
    "bad_name",
    ["../outside", "../../etc", "bad name", "UPPER", ".hidden", "", "a/b"],
)
def test_mutators_reject_bad_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_name: str
) -> None:
    """A name that is not a plain package name (traversal, slash, space, upper,
    empty) is hard-blocked by both mutators before any filesystem op."""
    root = tmp_path / "tools"
    _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)

    assert set_enabled(bad_name, False) is False
    assert delete_tool(bad_name) is False
    assert list_tools()[0]["name"] == "echo"  # the real package is untouched


def test_delete_symlink_escape_unlinks_only_the_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A regex-valid package name whose directory is a SYMLINK escaping the tools
    dir is unlinked at the ALIAS itself (H3, N3), never followed: unlink drops
    only the link, so rmtree can never recurse into the external target. Deleting
    the alias the user saw succeeds (True) and the escaped target is untouched --
    strictly safer than the old resolve-then-contain refusal, which left the
    dangling alias in place."""
    root = tmp_path / "tools"
    root.mkdir()
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("important")
    (root / "evil").symlink_to(precious, target_is_directory=True)
    _install_tools(monkeypatch, root)

    assert delete_tool("evil") is True  # the alias row is removed...
    assert not (root / "evil").exists()  # ...the link itself is gone...
    assert not (root / "evil").is_symlink()
    assert precious.exists()  # ...but the external target was never followed
    assert (precious / "keep.txt").exists()


def test_symlinked_package_dir_listed_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A package directory that is itself a SYMLINK (even to a real, otherwise-
    valid package) is listed invalid and never executed -- _scan_all's is_dir()
    filter follows the link, so this is the guard that keeps it out (H3)."""
    root = tmp_path / "tools"
    root.mkdir()
    # A real, valid package OUTSIDE the tools dir, reached only via a symlink.
    _make_tool(tmp_path, "real", "import sys\nsys.stdout.write('x')\n")
    (root / "evil").symlink_to(tmp_path / "real", target_is_directory=True)
    _install_tools(monkeypatch, root)

    listed = {t["name"]: t for t in list_tools()}
    assert listed["evil"]["valid"] is False
    assert "real directory" in (listed["evil"]["error"] or "")
    assert enabled_llm_tools() == []  # never advertised or executable


def test_symlinked_manifest_listed_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A package whose tool.json is a SYMLINK is listed invalid: a scan must
    never read a manifest through a link that could point outside the package."""
    root = tmp_path / "tools"
    pkg = root / "linky"
    pkg.mkdir(parents=True)
    (pkg / "run.py").write_text("import sys\nsys.stdout.write('x')\n")
    real_manifest = tmp_path / "real_tool.json"
    real_manifest.write_text(
        json.dumps(
            {
                "name": "linky",
                "description": "d",
                "parameters": {"type": "object"},
                "entry": [sys.executable, "run.py"],
            }
        )
    )
    (pkg / "tool.json").symlink_to(real_manifest)
    _install_tools(monkeypatch, root)

    listed = {t["name"]: t for t in list_tools()}
    assert listed["linky"]["valid"] is False
    assert "real file" in (listed["linky"]["error"] or "")


def test_set_enabled_rejects_symlinked_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """set_enabled must not rewrite a file OUTSIDE the package through a
    symlinked tool.json: the resolved-path containment check refuses the write,
    and the foreign file is left byte-for-byte untouched (H3)."""
    root = tmp_path / "tools"
    pkg = root / "echo"
    pkg.mkdir(parents=True)
    (pkg / "run.py").write_text("import sys\nsys.stdout.write('x')\n")
    outside = tmp_path / "outside.json"
    original = json.dumps(
        {
            "name": "echo",
            "description": "d",
            "parameters": {"type": "object"},
            "entry": [sys.executable, "run.py"],
            "enabled": True,
        }
    )
    outside.write_text(original)
    (pkg / "tool.json").symlink_to(outside)
    _install_tools(monkeypatch, root)

    assert set_enabled("echo", False) is False  # write refused
    assert outside.read_text() == original  # foreign file untouched (no rewrite)


def test_delete_internal_alias_removes_only_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An INTERNAL alias tools/<alias> -> tools/<real> resolves INSIDE the root,
    so resolve-then-contain would PASS and rmtree would recurse into and delete
    the REAL package (data loss). delete_tool must unlink ONLY the alias link:
    the real package survives intact and still lists valid, and only the phantom
    alias row disappears."""
    root = tmp_path / "tools"
    _make_tool(root, "real", "import sys\nsys.stdout.write('x')\n")
    (root / "alias").symlink_to(root / "real", target_is_directory=True)
    _install_tools(monkeypatch, root)

    assert delete_tool("alias") is True
    assert not (root / "alias").exists()  # the alias link is gone
    assert not (root / "alias").is_symlink()
    # The real package was never followed: its files survive and it still lists.
    assert (root / "real" / "tool.json").is_file()
    assert (root / "real" / "run.py").is_file()
    listed = {t["name"]: t for t in list_tools()}
    assert listed["real"]["valid"] is True
    assert "alias" not in listed  # the phantom invalid row is gone


def test_set_enabled_refuses_internal_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """set_enabled must never read or write a manifest THROUGH an internal alias:
    tools/<alias> -> tools/<real> resolves inside the root, so absent the
    pre-resolve symlink block a toggle on the alias would rewrite the REAL
    package's tool.json. The alias PATCH is refused (False) and the real manifest
    is left byte-for-byte untouched."""
    root = tmp_path / "tools"
    _make_tool(root, "real", "import sys\nsys.stdout.write('x')\n", enabled=True)
    (root / "alias").symlink_to(root / "real", target_is_directory=True)
    _install_tools(monkeypatch, root)

    before = (root / "real" / "tool.json").read_bytes()
    assert set_enabled("alias", False) is False
    assert (root / "real" / "tool.json").read_bytes() == before  # real manifest untouched
    real = {t["name"]: t for t in list_tools()}["real"]
    assert real["enabled"] is True  # the real package's flag never flipped


def test_set_enabled_refuses_oversized_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The manifest cap must hold at the WRITE entry too (N2): set_enabled
    stat-checks tool.json against _MANIFEST_MAX_BYTES BEFORE reading it, so an
    oversized manifest is refused (False) without being loaded into memory --
    matching the scan, which already lists it invalid."""
    root = tmp_path / "tools"
    _make_tool(
        root,
        "big",
        "import sys\nsys.stdout.write('x')\n",
        tool_json={
            "name": "big",
            "description": "x" * (_MANIFEST_MAX_BYTES + 100),
            "parameters": {"type": "object"},
            "entry": [sys.executable, "run.py"],
            "enabled": True,
        },
    )
    _install_tools(monkeypatch, root)

    before = (root / "big" / "tool.json").read_bytes()
    assert set_enabled("big", False) is False
    assert (root / "big" / "tool.json").read_bytes() == before  # untouched


def test_set_enabled_refuses_when_pretty_form_would_exceed_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A manifest whose COMPACT on-disk form sits under the cap but whose
    PRETTY (indent=2) re-serialization would cross it is refused, and the file is
    left byte-identical (N2). Without the post-build size check, a mere
    enable/disable toggle would EXPAND the manifest past the cap and flip the tool
    invalid on the next scan."""
    root = tmp_path / "tools"
    manifest = {
        "name": "swell",
        "description": "d",
        # A big array expands far more pretty-printed (a newline + 6-space indent
        # per element) than compact, so it can straddle the cap between the two
        # forms without any single field being individually oversized.
        "parameters": {"type": "object", "filler": ["v"] * 12000},
        "entry": [sys.executable, "run.py"],
        "enabled": True,
    }
    # Pin the premise: the compact form (what _make_tool writes) fits under the
    # cap, but the pretty form set_enabled would write does not.
    compact = json.dumps(manifest)
    assert len(compact.encode("utf-8")) < _MANIFEST_MAX_BYTES
    pretty = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    assert len(pretty.encode("utf-8")) > _MANIFEST_MAX_BYTES
    _make_tool(root, "swell", "import sys\nsys.stdout.write('x')\n", tool_json=manifest)
    _install_tools(monkeypatch, root)

    before = (root / "swell" / "tool.json").read_bytes()
    assert set_enabled("swell", False) is False  # pretty form would overflow the cap
    assert (root / "swell" / "tool.json").read_bytes() == before  # byte-identical


def test_manifest_over_size_limit_listed_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tool.json larger than the file-size bound is listed invalid -- rejected
    by stat() BEFORE the oversized file is ever read into memory."""
    root = tmp_path / "tools"
    _make_tool(
        root,
        "big",
        "import sys\nsys.stdout.write('x')\n",
        tool_json={
            "name": "big",
            # A huge description pushes the whole FILE past _MANIFEST_MAX_BYTES.
            "description": "x" * (_MANIFEST_MAX_BYTES + 100),
            "parameters": {"type": "object"},
            "entry": [sys.executable, "run.py"],
        },
    )
    _install_tools(monkeypatch, root)

    listed = {t["name"]: t for t in list_tools()}
    assert listed["big"]["valid"] is False
    assert "too large" in (listed["big"]["error"] or "")
    assert enabled_llm_tools() == []


def test_parameters_schema_over_size_limit_listed_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A parameters schema whose json.dumps exceeds the schema bound is listed
    invalid, even though the whole manifest FILE stays well under the file bound
    -- the schema is re-sent to the model on every request, so it is capped
    tighter than the manifest as a whole."""
    root = tmp_path / "tools"
    big_schema = {
        "type": "object",
        "properties": {"q": {"description": "y" * (_PARAMETERS_SCHEMA_MAX_BYTES + 100)}},
    }
    _make_tool(
        root,
        "bigschema",
        "import sys\nsys.stdout.write('x')\n",
        tool_json={
            "name": "bigschema",
            "description": "d",
            "parameters": big_schema,
            "entry": [sys.executable, "run.py"],
        },
    )
    _install_tools(monkeypatch, root)

    listed = {t["name"]: t for t in list_tools()}
    assert listed["bigschema"]["valid"] is False
    assert "schema is too large" in (listed["bigschema"]["error"] or "")
    # It's the SCHEMA bound that tripped, not the file bound: the file is small.
    assert (root / "bigschema" / "tool.json").stat().st_size < _MANIFEST_MAX_BYTES


def test_hidden_directories_never_listed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A dot-directory is never a package: the installer stages in-progress
    builds under <tools_dir>/.staging, and listing that as a broken package
    would surface every install in flight as a phantom row."""
    root = tmp_path / "tools"
    _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    (root / ".staging" / "abc123").mkdir(parents=True)
    _install_tools(monkeypatch, root)

    assert [t["name"] for t in list_tools()] == ["echo"]


def test_feature_off_when_tools_dir_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "context_memory.services.tools.get_settings", lambda: Settings(tools_dir="")
    )
    assert tools_dir() is None
    assert list_tools() == []
    assert enabled_llm_tools() == []
    assert set_enabled("x", True) is False
    assert delete_tool("x") is False


# --- workflow wiring -------------------------------------------------------


async def _noop_handler(_args: dict[str, Any]) -> str:
    return ""


_FAKE_TOOL = LlmTool(spec=_tool_spec("faketool"), handler=_noop_handler)

_WORKFLOW_CASES = [
    (lambda: asyncio.run(capture_draft("raw text")), CAPTURE_SYSTEM_PROMPT, {"title": "t"}),
    (
        lambda: asyncio.run(enrich_item({"title": "T"}, "ctx")),
        ENRICH_SYSTEM_PROMPT,
        {"progress_note": "n"},
    ),
    (
        lambda: asyncio.run(assist_update({"title": "T"}, "note")),
        UPDATE_SYSTEM_PROMPT,
        {"progress_note": "n"},
    ),
]
_WORKFLOW_IDS = ["capture", "enrich", "update"]


def _capture_generate_structured(
    monkeypatch: pytest.MonkeyPatch, good: dict[str, Any]
) -> dict[str, Any]:
    """Replace memory_ai.generate_structured with a capturing stub; return the
    dict it fills with the outgoing system_prompt and tools."""
    captured: dict[str, Any] = {}

    async def _fake_gen(
        system_prompt: str,
        user_prompt: str,
        model_cls: type[BaseModel],
        *,
        workflow: str = "unknown",
        tools: Any = None,
        **_kwargs: Any,
    ) -> BaseModel:
        captured["system_prompt"] = system_prompt
        captured["tools"] = tools
        return model_cls.model_validate(good)

    monkeypatch.setattr("context_memory.services.memory_ai.generate_structured", _fake_gen)
    return captured


@pytest.mark.parametrize("run_call, base_prompt, good", _WORKFLOW_CASES, ids=_WORKFLOW_IDS)
def test_workflow_appends_tools_rule_when_active(
    monkeypatch: pytest.MonkeyPatch,
    run_call: Any,
    base_prompt: str,
    good: dict[str, Any],
) -> None:
    captured = _capture_generate_structured(monkeypatch, good)
    monkeypatch.setattr("context_memory.services.tools.enabled_llm_tools", lambda: [_FAKE_TOOL])
    run_call()

    assert captured["system_prompt"] == base_prompt + "\n" + _TOOLS_RULE
    assert _TOOLS_RULE in captured["system_prompt"]
    assert captured["tools"] == [_FAKE_TOOL]


@pytest.mark.parametrize("run_call, base_prompt, good", _WORKFLOW_CASES, ids=_WORKFLOW_IDS)
def test_workflow_prompt_byte_identical_without_tools(
    monkeypatch: pytest.MonkeyPatch,
    run_call: Any,
    base_prompt: str,
    good: dict[str, Any],
) -> None:
    captured = _capture_generate_structured(monkeypatch, good)
    monkeypatch.setattr("context_memory.services.tools.enabled_llm_tools", lambda: [])
    run_call()

    assert captured["system_prompt"] == base_prompt  # byte-identical to the pinned constant
    assert captured["tools"] is None
