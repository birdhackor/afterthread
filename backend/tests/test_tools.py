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
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from context_memory.config import Settings
from context_memory.services import llm_log, tools
from context_memory.services.llm import (
    _MAX_TOOL_CALLS_ACCEPTED,
    _MAX_TOOL_CALLS_PER_REPLY,
    _MAX_TOOL_CALLS_TOTAL_BYTES,
    _TOO_MANY_TOOL_CALLS,
    _TOOL_BUDGET_EXHAUSTED,
    _TOOL_DEADLINE_REACHED,
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


def _tool_calls_completion(
    *tool_calls: SimpleNamespace, content: str | None = None
) -> SimpleNamespace:
    """A completion whose assistant message carries tool_calls (content is null
    unless a test needs to also inject a ``content`` string, e.g. to exercise
    the F4 byte cap's content-counts-too rule)."""
    message = SimpleNamespace(content=content, tool_calls=list(tool_calls))
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


def test_tool_calls_oversized_rejected_as_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reply whose tool_calls COUNT is legal (<= _MAX_TOOL_CALLS_ACCEPTED) but whose
    aggregate serialized SIZE exceeds _MAX_TOOL_CALLS_TOTAL_BYTES is refused at the
    SAME entry point as the count flood (F4). The count cap alone would let a handful
    of calls carrying huge ``arguments`` blobs through to be summarized, echoed in the
    assistant turn and parsed -- unbounded OUTBOUND memory/prompt. Refused up front on
    the SAME 502 taxonomy: no handler runs, and the recorder finalizes the interaction
    as an upstream failure carrying the safe oversized category."""
    seen: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any]) -> str:
        seen.append(args)
        return "ran"

    # A FEW calls, each with a large arguments string, summing PAST the byte cap while
    # the COUNT stays far under _MAX_TOOL_CALLS_ACCEPTED -- so it is the SIZE gate, not
    # the count gate, that trips.
    chunk = "a" * (_MAX_TOOL_CALLS_TOTAL_BYTES // 4 + 1)
    big = [_tc("echo", chunk, tc_id=f"c{i}") for i in range(4)]
    assert len(big) <= _MAX_TOOL_CALLS_ACCEPTED  # the count cap is NOT what trips
    _install(monkeypatch, _ScriptedClient([_tool_calls_completion(*big)]))

    with pytest.raises(LLMUpstreamError) as excinfo:
        _run(tools=[LlmTool(spec=_tool_spec("echo"), handler=handler)])

    assert str(excinfo.value).startswith("tool_calls oversized")
    assert seen == []  # the reply never entered the tool round -- no handler ran
    summaries = llm_log.list_summaries(10)
    assert summaries
    record = llm_log.get_record(summaries[0]["id"])
    assert record is not None
    assert record["outcome"] == "upstream_error"
    assert (record["error"] or "").startswith("tool_calls oversized")
    assert (record["attempts"][0]["error"] or "").startswith("tool_calls oversized")


def test_tool_calls_content_counts_toward_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """F4's byte cap must count the echoed assistant ``content`` too, not just
    id+name+arguments. Before this test's fix, only _tool_call_fields' three
    strings were summed, so a reply with a SINGLE tiny tool call (well under
    both the count cap and, on tool-call fields alone, the byte cap) but a
    GIANT ``content`` string sailed under total_bytes -- yet
    _assistant_tool_call_message(_completion_content(completion), tool_calls)
    still echoes that unbounded content into the next round's messages,
    reintroducing the unbounded prompt/memory growth F4 exists to stop. Same
    entry point, same taxonomy as test_tool_calls_oversized_rejected_as_upstream_error
    -- only WHAT overflows differs (content instead of arguments)."""
    seen: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any]) -> str:
        seen.append(args)
        return "ran"

    huge_content = "a" * (_MAX_TOOL_CALLS_TOTAL_BYTES + 1)
    tiny_call = _tc("echo", "{}", tc_id="c0")  # id+name+args are a few bytes
    _install(
        monkeypatch, _ScriptedClient([_tool_calls_completion(tiny_call, content=huge_content)])
    )

    with pytest.raises(LLMUpstreamError) as excinfo:
        _run(tools=[LlmTool(spec=_tool_spec("echo"), handler=handler)])

    assert str(excinfo.value).startswith("tool_calls oversized")
    assert seen == []  # the reply never entered the tool round -- no handler ran
    summaries = llm_log.list_summaries(10)
    assert summaries
    record = llm_log.get_record(summaries[0]["id"])
    assert record is not None
    assert record["outcome"] == "upstream_error"
    assert (record["error"] or "").startswith("tool_calls oversized")
    assert (record["attempts"][0]["error"] or "").startswith("tool_calls oversized")


def test_tool_conversation_budget_stops_advertising_and_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1: once the live conversation ACTUALLY SENT to the model exceeds the
    budget, the loop stops advertising tools and makes ONE final tools-free
    completion -- it does NOT keep looping to max_tool_rounds. The D27 llm_log
    budget bounds only what is RECORDED; this bounds what is SENT each round.

    Each tool round returns a large result (30k chars); across two rounds the
    accumulated conversation crosses a small 50k budget, so the THIRD create() is
    the finalize (tools-free, budget-exhausted nudge appended), even though the
    default round budget (8) is nowhere near spent."""
    calls_seen = 0

    async def handler(args: dict[str, Any]) -> str:
        nonlocal calls_seen
        calls_seen += 1
        return "x" * 30_000

    client = _install(
        monkeypatch,
        _ScriptedClient(
            [
                _tool_calls_completion(_tc("big", "{}")),
                _tool_calls_completion(_tc("big", "{}")),
                _content_completion(_SAMPLE_JSON),
            ]
        ),
        settings=_settings(llm_tool_conversation_budget_chars=50_000),
    )
    result = _run(tools=[LlmTool(spec=_tool_spec("big"), handler=handler)])

    assert result.title == "Draft"
    calls = _calls(client)
    # Two tool rounds accumulated ~60k > the 50k budget; the THIRD create is the
    # tools-free finalize -- NOT a spin to the default max_tool_rounds (8).
    assert len(calls) == 3
    assert "tools" in calls[0] and "tools" in calls[1]
    assert "tools" not in calls[2]
    assert any(m.get("content") == _TOOL_BUDGET_EXHAUSTED for m in calls[2]["messages"])
    assert calls_seen == 2  # exactly the two tool rounds ran before the budget tripped


def test_tool_conversation_budget_default_does_not_trip_normal_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default budget (1_000_000) leaves an ordinary small-output tool loop
    untouched: a modest result across a couple of rounds never trips F1, so tools
    stay advertised until the model answers on its own."""
    client = _install(
        monkeypatch,
        _ScriptedClient(
            [
                _tool_calls_completion(_tc("echo", "{}")),
                _content_completion(_SAMPLE_JSON),
            ]
        ),
    )
    result = _run(tools=[_echo_tool()])

    assert result.title == "Draft"
    calls = _calls(client)
    assert len(calls) == 2
    assert "tools" in calls[0] and "tools" in calls[1]  # never stopped advertising


def test_tool_deadline_skips_new_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2: with too little wall-clock budget left, the loop does NOT START tool
    handlers -- each call in the reply gets the deadline-reached rejection and no
    handler runs. asyncio.timeout cannot preempt a threadpool tool already in
    flight, so declining to START new ones is what bounds the overrun.

    Driven with a tiny timeout BELOW _TOOL_DEADLINE_FLOOR_SECONDS (1.0s), so from
    the very first per-call check the remaining budget is already under the floor
    and EVERY call in the reply is skipped -- a deterministic assertion of the
    invariant (new tools not started once the deadline is spent) that never leans
    on real elapsed time crossing a threshold mid-round. The 0.5s deadline is
    still ample for the handler-free work here, so the interaction FINALIZES on
    the scripted content reply rather than timing out."""
    seen: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any]) -> str:
        seen.append(args)
        return "ran"

    client = _install(
        monkeypatch,
        _ScriptedClient(
            [
                _tool_calls_completion(
                    _tc("echo", "{}", tc_id="c0"),
                    _tc("echo", "{}", tc_id="c1"),
                    _tc("echo", "{}", tc_id="c2"),
                ),
                _content_completion(_SAMPLE_JSON),
            ]
        ),
    )
    result = _run(tools=[LlmTool(spec=_tool_spec("echo"), handler=handler)], timeout_seconds=0.5)

    assert result.title == "Draft"
    assert seen == []  # no handler was started once the deadline was spent
    tool_msgs = _tool_messages(_calls(client)[1])
    assert len(tool_msgs) == 3  # every call still paired with a result...
    assert all(m["content"] == _TOOL_DEADLINE_REACHED for m in tool_msgs)  # ...a deadline rejection
    # the round WAS entered: the assistant tool-call turn is still echoed back,
    # so this is genuinely "tools skipped", not "the round never ran".
    assert any(
        m.get("role") == "assistant" and m.get("tool_calls") for m in _calls(client)[1]["messages"]
    )


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


def test_runtime_overflow_output_with_exiting_leader_stays_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression for the overflow-kill / reap race in _communicate_bounded:
    _CappedReader.run calls its ``kill`` callback the INSTANT a stream passes
    the cap, from its own thread, concurrently with the main thread's
    natural-exit-triggered teardown+reap. Unlike
    test_runtime_unbounded_output_killed_promptly's tool (an infinite loop,
    which can ONLY ever be stopped by the overflow kill), this tool writes
    past the cap and then exits ON ITS OWN -- so on every run there is a
    genuine race between the reader thread's overflow kill and the main
    thread's waitid-detected natural exit. test_runtime_output_is_capped has
    this same write-then-exit shape but exercises it only once; repeating it
    gives that race many more chances to land on either interleaving. This
    test does not assert (or need) which one wins -- only that the result is
    invariant either way: `_communicate_bounded`'s teardown_lock makes the
    overflow callback a no-op once the main thread has started reaping,
    instead of letting it call getpgid/killpg on a pid the reap may have just
    freed (and the kernel could have reused)."""
    root = tmp_path / "tools"
    _make_tool(root, "big", "import sys\nsys.stdout.write('a' * 5000)\n")
    _install_tools(monkeypatch, root, llm_tool_output_max_chars=1000)
    handler = enabled_llm_tools()[0].handler

    marker = "…[工具輸出過長已截斷]"
    started = time.monotonic()
    for _ in range(20):
        result = asyncio.run(handler({}))
        assert len(result) == 1000
        assert result.endswith(marker)
    assert time.monotonic() - started < 10  # 20 clean teardowns, never a hang


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
    parent environment is never inherited wholesale. The .env value IS injected
    (so it is not "None"), but F1 masks any known secret VALUE out of the RESULT
    before it reaches the conversation, so the raw value never rides back."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-should-not-leak")
    root = tmp_path / "tools"
    _make_tool(
        root,
        "envtool",
        "import os, sys\n"
        "sys.stdout.write('OPENAI=' + str(os.environ.get('OPENAI_API_KEY')) + "
        "';SECRET=' + str(os.environ.get('TOOL_SECRET')))\n",
        dotenv="TOOL_SECRET=from-dotenv-abcdef\n",
    )
    _install_tools(monkeypatch, root)

    result = asyncio.run(enabled_llm_tools()[0].handler({}))
    assert "OPENAI=None" in result  # our key was never in the child env
    # The .env secret WAS injected -- "SECRET=None" would mean it was absent -- but
    # F1 masks the known value in the result, so it comes back as the marker.
    assert "SECRET=None" not in result
    assert f"SECRET={tools._REDACTION_MARKER}" in result
    assert "from-dotenv-abcdef" not in result  # the raw value never rides back
    assert "sk-secret-should-not-leak" not in result


def test_runtime_dotenv_does_not_interpolate_parent_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tool .env value of ``${OPENAI_API_KEY}`` must reach the child as that
    LITERAL string, NOT the real key: python-dotenv's default POSIX interpolation
    would resolve it from the parent os.environ -- reinjecting the very credential
    the from-scratch env exists to exclude (interpolate=False).

    Asserted on the BUILT child env directly rather than through the subprocess
    RESULT, because F1 now masks any known secret value out of that result -- and the
    literal ``${OPENAI_API_KEY}`` is itself a registered .env value, so a
    result-level check could no longer tell the literal from the resolved key (both
    would come back masked). The env dict is the precise unit under test and is
    unaffected by the result-side redaction."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-should-not-leak")
    root = tmp_path / "tools"
    pkg = _make_tool(
        root,
        "envtool",
        "import os, sys\nsys.stdout.write('LEAK=' + str(os.environ.get('LEAK')))\n",
        dotenv="LEAK=${OPENAI_API_KEY}\n",
    )
    _install_tools(monkeypatch, root)

    env = tools._build_tool_env(pkg)
    assert env["LEAK"] == "${OPENAI_API_KEY}"  # literal, not the resolved parent key
    assert "sk-secret-should-not-leak" not in env.values()


def test_runtime_background_descendant_reaped_no_thread_leak(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tool that spawns a background descendant INHERITING stdout and exits at
    once must not wedge the bounded reader (F1). ``proc.wait()`` returns on the
    LEADER's exit, but the descendant still holds the stdout pipe's write end, so
    the reader sees no EOF and would block forever -- leaking the (daemon) thread,
    its FD and the descendant. The post-wait group-kill escalation SIGKILLs the
    whole session, closing the inherited pipe so the reader hits EOF and finishes:
    the call returns its output promptly, no reader thread leaks, and the
    descendant's process group is dead afterwards."""
    root = tmp_path / "tools"
    # A marker UNIQUE to this process+moment, so a leak from a different run (or a
    # deliberately-broken build) can never pollute this run's pgrep check.
    marker = f"cm_f1_desc_{os.getpid()}_{time.monotonic_ns()}"
    # Spawn a python descendant that sleeps 60s carrying that marker, inheriting
    # our stdout (so it holds the pipe past our exit) with its own stderr silenced
    # (so ONLY the stdout reader is left blocking). Then exit. 60s >> the ~5s reap
    # join, so a survivor is unambiguously still alive at the pgrep check below
    # rather than having exited on its own.
    run_py = (
        "import subprocess, sys\n"
        "subprocess.Popen(\n"
        f"    [sys.executable, '-c', 'import time; time.sleep(60)', {marker!r}],\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "sys.stdout.write('STARTED')\n"
    )
    pkg = _make_tool(root, "descendant", run_py)
    entry = [sys.executable, "run.py"]
    env = tools._build_tool_env(pkg)

    before = threading.active_count()
    try:
        started = time.monotonic()
        # Drive the blocking runner DIRECTLY (not via the threadpool handler) so
        # the reader/writer threads run in this thread's context and
        # active_count() is a clean before/after measure with no threadpool-worker
        # confound.
        result = tools._run_tool_subprocess(entry, pkg, env, "{}", 30.0, 1000)
        elapsed = time.monotonic() - started

        assert "STARTED" in result  # the leader's own output survived the escalation
        # Bounded by the reap join + escalation, NOT waited out for the 60s sleep.
        assert elapsed < 20
        # No reader/writer thread leak: the group-kill unblocked the pipe-held
        # reader, so every thread was joined before the call returned. Poll briefly
        # to avoid racing the final join's own return.
        for _ in range(200):
            if threading.active_count() <= before:
                break
            time.sleep(0.01)
        assert threading.active_count() <= before
        # The descendant's process group is dead: no lingering marker process.
        if shutil.which("pgrep"):
            found = subprocess.run(["pgrep", "-f", marker], capture_output=True, check=False)
            assert found.returncode != 0, "the background descendant survived the escalation"
    finally:
        # Insurance: if a regression ever DID leak the descendant, don't leave it
        # sleeping for a minute polluting the host.
        if shutil.which("pkill"):
            subprocess.run(["pkill", "-9", "-f", marker], check=False)


def test_runtime_detached_child_closing_pipes_is_killed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tool whose entry spawns a child that CLOSES the inherited stdout/stderr
    (redirects both to /dev/null) and then sleeps, while the LEADER exits at once,
    must still have that child killed (F1, the round-3 (a) hole). Because the child
    holds NONE of the leader's pipes, the readers hit EOF the instant the leader
    exits and their threads die -- so round-3's "escalate the group kill ONLY if a
    reader thread is still alive" never fired and the detached child LEAKED,
    sleeping on. The new teardown kills the whole process group UNCONDITIONALLY
    (before reaping the leader, so proc.pid still pins the group), reaping the
    child regardless of pipe/thread state. Conceptually this FAILS against the
    round-3 logic: with the readers already at EOF, nothing there would signal the
    surviving child.

    Robust leak detection is a unique-argv marker + pgrep, NOT threading.active_count
    (which the (a) hole doesn't even perturb -- the threads exit on EOF)."""
    root = tmp_path / "tools"
    marker = f"cm_f1_closed_{os.getpid()}_{time.monotonic_ns()}"
    # The child silences BOTH its stdout and stderr (so it inherits neither of the
    # leader's pipes) and sleeps 60s carrying the marker; the leader then exits
    # IMMEDIATELY. 60s >> the reap window, so a survivor is unambiguously alive at
    # the pgrep check rather than having exited on its own.
    run_py = (
        "import subprocess, sys\n"
        "subprocess.Popen(\n"
        f"    [sys.executable, '-c', 'import time; time.sleep(60)', {marker!r}],\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "sys.stdout.write('LEADER_DONE')\n"
    )
    pkg = _make_tool(root, "detached", run_py)
    entry = [sys.executable, "run.py"]
    env = tools._build_tool_env(pkg)

    try:
        started = time.monotonic()
        result = tools._run_tool_subprocess(entry, pkg, env, "{}", 30.0, 1000)
        elapsed = time.monotonic() - started

        assert "LEADER_DONE" in result  # the leader's own output survived
        assert elapsed < 20  # returned on the leader's prompt exit, not the 60s sleep
        # The detached child shared the leader's process group; the unconditional
        # pre-reap group kill SIGKILLed it. Round-3 would have left it sleeping.
        if shutil.which("pgrep"):
            found = subprocess.run(["pgrep", "-f", marker], capture_output=True, check=False)
            assert found.returncode != 0, "the detached child survived (round-3 (a) hole)"
    finally:
        if shutil.which("pkill"):
            subprocess.run(["pkill", "-9", "-f", marker], check=False)


def test_runtime_oversized_dotenv_degrades_to_no_extra_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A .env larger than _ENV_FILE_MAX_BYTES is dropped at RUNTIME (F4): the
    child sees NO extra env -- the same degrade-to-{} contract a malformed .env
    already gets -- so a runaway or post-install-mutated .env can never bloat the
    per-call exec env. The package still lists VALID: the registry scan
    deliberately does NOT reject on .env size, leaving the runtime degrade as the
    post-install defense."""
    root = tmp_path / "tools"
    oversized = "TOOL_SECRET=" + "x" * tools._ENV_FILE_MAX_BYTES + "\n"  # > the 64 KiB cap
    _make_tool(
        root,
        "envtool",
        "import os, sys\nsys.stdout.write('SECRET=' + str(os.environ.get('TOOL_SECRET')))\n",
        dotenv=oversized,
    )
    _install_tools(monkeypatch, root)

    assert list_tools()[0]["valid"] is True  # scan does NOT reject an oversized .env
    result = asyncio.run(enabled_llm_tools()[0].handler({}))
    assert result == "SECRET=None"  # the oversized .env was dropped -> key absent


def test_runtime_fifo_dotenv_degrades_without_hanging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A FIFO swapped in for ``.env`` degrades to no-extra-env WITHOUT hanging (F3a):
    ``dotenv_values(path)`` would REOPEN and read the path, and an ordinary open of a
    writer-less FIFO BLOCKS FOREVER -- wedging the caller. The shared helper opens
    O_NONBLOCK + S_ISREG-gates, so the FIFO is refused at once and the load degrades
    to {} -- the same contract a malformed/oversized ``.env`` gets. The env build is
    driven on a WATCHED daemon thread so a regression (a blocking reopen) fails
    LOUDLY here instead of wedging the whole suite."""
    root = tmp_path / "tools"
    pkg = _make_tool(
        root,
        "envtool",
        "import os, sys\nsys.stdout.write('SECRET=' + str(os.environ.get('TOOL_SECRET')))\n",
    )
    os.mkfifo(pkg / ".env")  # a writer-less FIFO -- an ordinary read would block forever
    _install_tools(monkeypatch, root)

    box: dict[str, dict[str, str]] = {}
    worker = threading.Thread(
        target=lambda: box.__setitem__("env", tools._build_tool_env(pkg)), daemon=True
    )
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "reading a FIFO .env hung (F3a regression)"
    assert "TOOL_SECRET" not in box["env"]  # the FIFO .env degraded to no extra env

    # End to end: the child's env carries no TOOL_SECRET (reaching here also proves
    # the synchronous env build in the handler did not hang).
    result = asyncio.run(enabled_llm_tools()[0].handler({}))
    assert result == "SECRET=None"


def test_validate_package_flags_oversized_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The installer refuses a package whose .env exceeds the cap (F4): an
    otherwise-valid staged package is reported invalid so it can never be
    INSTALLED in that state, even though the same package would still RUN
    (degraded) if the .env were mutated oversized AFTER install."""
    root = tmp_path / "tools"
    pkg = _make_tool(
        root,
        "big",
        "import sys\nsys.stdout.write('x')\n",
        dotenv="K=" + "y" * tools._ENV_FILE_MAX_BYTES + "\n",
    )
    _install_tools(monkeypatch, root)

    # The scan-equivalent checks pass; it is the install-only .env gate that trips.
    error = tools.validate_package(pkg, "big")
    assert error is not None
    assert "too large" in error
    # With the .env removed the same package validates clean -- pinning that it
    # was the .env size, not some other defect, that failed it.
    (pkg / ".env").unlink()
    assert tools.validate_package(pkg, "big") is None


def _staged_pkg(parent: Path, *, tool_json: dict[str, Any], run_py: str) -> Path:
    """A staging-style package (files at the package ROOT) for validate_package tests."""
    pkg = parent / "staged"
    pkg.mkdir(parents=True)
    (pkg / "run.py").write_text(run_py, encoding="utf-8")
    (pkg / "tool.json").write_text(json.dumps(tool_json), encoding="utf-8")
    return pkg


def _kbsearch_manifest(description: str = "searches the KB") -> dict[str, Any]:
    return {
        "name": "staged",
        "description": description,
        "parameters": {"type": "object", "properties": {}},
        "entry": [sys.executable, "run.py"],
    }


def test_validate_package_rejects_manifest_with_secret_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """H3: a staged MANIFEST that embeds a known secret value is rejected at install
    validation. The manifest is served via /api/tools and re-sent in every LLM tool
    spec, so an embedded key is malformed by design -- rejected (naming the file),
    never silently redacted. The offending VALUE is never echoed in the reason."""
    root = tmp_path / "tools"
    # An installed tool contributes its .env secret to known_secret_values.
    secret = "another-tools-live-secret-abcdef"
    _make_tool(root, "other", "import sys\nsys.stdout.write('x')\n", dotenv=f"OTHER_KEY={secret}\n")
    _install_tools(monkeypatch, root)

    pkg = _staged_pkg(
        tmp_path / "stage",
        tool_json=_kbsearch_manifest(description=f"uses {secret} to authenticate"),
        run_py="import sys\nsys.stdout.write('x')\n",
    )
    error = tools.validate_package(pkg, "staged")
    assert error is not None
    assert error.startswith("tool.json ")
    assert "不得包含秘密值" in error
    assert secret not in error  # the value itself is never echoed back


def test_validate_package_rejects_impl_file_with_secret_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """H3: the scan covers EVERY staged file, not just the manifest -- an implementation
    file that hardcoded a known key is a persisted plaintext copy and is rejected too,
    named by its relative path."""
    root = tmp_path / "tools"
    secret = "another-tools-live-secret-abcdef"
    _make_tool(root, "other", "import sys\nsys.stdout.write('x')\n", dotenv=f"OTHER_KEY={secret}\n")
    _install_tools(monkeypatch, root)

    pkg = _staged_pkg(
        tmp_path / "stage",
        tool_json=_kbsearch_manifest(),  # manifest is clean
        run_py=f"API_KEY = '{secret}'\nimport sys\nsys.stdout.write('x')\n",
    )
    error = tools.validate_package(pkg, "staged")
    assert error is not None
    assert error.startswith("run.py ")
    assert "不得包含秘密值" in error
    assert secret not in error


def test_validate_package_clean_of_secrets_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """H3: a package embedding NO known secret validates clean -- the gate never trips
    on an ordinary build even while other tools' secrets are registered."""
    root = tmp_path / "tools"
    _make_tool(
        root,
        "other",
        "import sys\nsys.stdout.write('x')\n",
        dotenv="OTHER_KEY=another-tools-live-secret-abcdef\n",
    )
    _install_tools(monkeypatch, root)

    pkg = _staged_pkg(
        tmp_path / "stage",
        tool_json=_kbsearch_manifest(),
        run_py="import os, sys\nsys.stdout.write(os.environ.get('OTHER_KEY', ''))\n",
    )
    assert tools.validate_package(pkg, "staged") is None


def test_validate_package_rejection_never_echoes_secret_in_filename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """H3: even when a secret is embedded in a FILE NAME (a builder that ran
    `> "$SECRET.txt"` under run_shell) whose content also carries it, the rejection names
    the file with the value REDACTED -- the reason never echoes the secret via the path."""
    root = tmp_path / "tools"
    secret = "another-tools-live-secret-abcdef"
    _make_tool(root, "other", "import sys\nsys.stdout.write('x')\n", dotenv=f"OTHER_KEY={secret}\n")
    _install_tools(monkeypatch, root)

    pkg = _staged_pkg(
        tmp_path / "stage",
        tool_json=_kbsearch_manifest(),
        run_py="import sys\nsys.stdout.write('x')\n",
    )
    # A stray file whose NAME and CONTENT both carry the secret (sorts first: 'a...').
    (pkg / f"{secret}.txt").write_text(f"leaked {secret}", encoding="utf-8")
    error = tools.validate_package(pkg, "staged")
    assert error is not None
    assert "不得包含秘密值" in error
    assert secret not in error  # neither content nor filename echoes the value
    assert tools._REDACTION_MARKER in error  # the offending path is named, masked


# --- known-secret redaction (F1 / F4, D36) ---------------------------------


def test_redaction_marker_matches_llm_log() -> None:
    """tools._REDACTION_MARKER and llm_log._REDACTION_MARKER MUST be byte-identical:
    a secret masked in a live tool result and one masked in the AI 日誌 have to be
    indistinguishable. They are deliberately duplicated (llm_log stays a leaf
    observability module -- see the note on tools._REDACTION_MARKER), so this pins
    them equal against silent drift."""
    assert tools._REDACTION_MARKER == llm_log._REDACTION_MARKER


def test_redact_known_secrets_masks_and_skips_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """redact_known_secrets masks every known value and SKIPS values under the
    6-char floor (mirroring llm_log._redact), replacing with the shared marker."""
    monkeypatch.setattr(
        tools, "known_secret_values", lambda: frozenset({"live-secret-abcdef", "short"})
    )
    out = tools.redact_known_secrets("a live-secret-abcdef b short c")
    assert "live-secret-abcdef" not in out
    assert tools._REDACTION_MARKER in out
    assert "short" in out  # < 6 chars -> never redacted (would shred prose)


def test_redact_known_secrets_longest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """F4: overlapping values are replaced LONGEST-first, so masking a shorter value
    that is a PREFIX of a longer one never leaves the longer's suffix exposed."""
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({"abcdef", "abcdefXYZ789"}))
    out = tools.redact_known_secrets("key=abcdefXYZ789 end")
    assert "abcdefXYZ789" not in out
    assert "XYZ789" not in out  # the suffix a shorter-first pass would have leaked
    assert out.count(tools._REDACTION_MARKER) == 1


def test_redact_known_secrets_masks_trailing_prefix_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1: text ENDING with a >=6-char PREFIX of a known secret -- a secret cut by an
    earlier truncation boundary (the bounded pipe reader's cap+1), so the full value
    never appears -- is masked at the tail, even though the full-value replace can't
    match it."""
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({"abcdefGHIJKLMN"}))
    out = tools.redact_known_secrets("result=abcdefGHIJ")  # first 10 chars of the secret
    assert "abcdefGHIJ" not in out
    assert out.startswith("result=")
    assert out.endswith(tools._REDACTION_MARKER)


def test_redact_known_secrets_masks_longest_trailing_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1: the LONGEST secret prefix the text ends with is masked, so masking a short
    fragment never leaves earlier secret bytes exposed."""
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({"abcdefGHIJKLMN"}))
    out = tools.redact_known_secrets("x=abcdefGHIJKL")  # 12 of the 14 secret chars
    assert "abcdefGH" not in out  # not just the last 6 masked -- the whole fragment is
    assert out == "x=" + tools._REDACTION_MARKER


def test_redact_known_secrets_ignores_short_trailing_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1: a trailing fragment UNDER the 6-char floor is left alone (same floor as full
    values -- masking a <6-char tail would shred ordinary prose)."""
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({"abcdefGHIJKLMN"}))
    out = tools.redact_known_secrets("value=abcde")  # only 5 chars of the secret prefix
    assert out == "value=abcde"  # untouched
    assert tools._REDACTION_MARKER not in out


def test_runtime_tool_output_masks_known_env_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F1(a): a runtime tool that echoes its OWN .env secret to stdout has that value
    masked in the RESULT the model sees -- the llm loop wraps this string verbatim
    into the next round, so masking it here keeps the secret out of the LIVE
    conversation, consistent with the AI 日誌 redaction."""
    root = tmp_path / "tools"
    _make_tool(
        root,
        "leak",
        "import os, sys\nsys.stdout.write('key=' + str(os.environ.get('TOOL_SECRET')))\n",
        dotenv="TOOL_SECRET=kb-live-key-abcdef123456\n",
    )
    _install_tools(monkeypatch, root)

    result = asyncio.run(enabled_llm_tools()[0].handler({}))
    assert "kb-live-key-abcdef123456" not in result
    assert tools._REDACTION_MARKER in result


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


def test_set_enabled_fifo_manifest_refused_without_hanging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A FIFO swapped in for tool.json makes set_enabled return False PROMPTLY,
    never hanging (F3b): a plain read_text() would open(O_RDONLY) the FIFO and BLOCK
    the PATCH worker FOREVER waiting for a writer. set_enabled now reads the manifest
    through the shared O_NONBLOCK+S_ISREG helper, so the FIFO is refused at once and
    the write is never reached. Driven on a WATCHED daemon thread so a regression (a
    blocking reopen) fails LOUDLY here instead of wedging the whole suite."""
    root = tmp_path / "tools"
    pkg = root / "fifotool"
    pkg.mkdir(parents=True)
    (pkg / "run.py").write_text("import sys\nsys.stdout.write('x')\n", encoding="utf-8")
    os.mkfifo(pkg / "tool.json")  # a writer-less FIFO -- read_text() would block forever
    _install_tools(monkeypatch, root)

    box: dict[str, bool] = {}
    worker = threading.Thread(
        target=lambda: box.__setitem__("ok", set_enabled("fifotool", False)), daemon=True
    )
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "set_enabled on a FIFO tool.json hung (F3b regression)"
    assert box["ok"] is False  # the FIFO manifest was refused, not rewritten
    assert (pkg / "tool.json").is_fifo()  # still the FIFO, never overwritten


def test_write_regular_file_refuses_symlink_leaf(tmp_path: Path) -> None:
    """_write_regular_file's O_NOFOLLOW refuses a symlinked FINAL component
    (ELOOP -> False), so a generated symlink can never redirect a write OUT of the
    staging jail / a package: the write returns False and the external target is
    left byte-for-byte untouched. This is the write-side of
    _read_regular_file_capped's O_NOFOLLOW backstop -- the resolve-then-contain gate
    (_resolve_in_staging / set_enabled) catches the non-race escape; this hardens
    the leaf open itself against a symlink raced in after the resolve."""
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(outside)

    assert tools._write_regular_file(link, "clobber") is False
    assert outside.read_text(encoding="utf-8") == "keep"  # target untouched, not followed
    assert link.is_symlink()  # O_CREAT never replaced the link with a regular file


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
