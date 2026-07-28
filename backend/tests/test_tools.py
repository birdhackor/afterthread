"""Tests for the tool-calling loop (afterthread.services.llm) and the tool
runtime (afterthread.services.tools), plus the memory_ai workflow wiring.

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
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from afterthread.config import Settings
from afterthread.services import llm_log, tools
from afterthread.services.llm import (
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
from afterthread.services.memory_ai import (
    _TOOLS_RULE,
    CAPTURE_SYSTEM_PROMPT,
    ENRICH_SYSTEM_PROMPT,
    UPDATE_SYSTEM_PROMPT,
    assist_update,
    capture_draft,
    enrich_item,
)
from afterthread.services.tools import (
    _AI_META_MAX_BYTES,
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
_TEST_VID = "20260728T010203Z-abcdef"


@pytest.fixture(autouse=True)
def _reset_log() -> Generator[None]:
    """Empty process-local singleton state around every test."""
    llm_log._reset_for_tests()
    tools._ADVERTISEMENT_GENERATIONS.clear()
    yield
    llm_log._reset_for_tests()
    tools._ADVERTISEMENT_GENERATIONS.clear()


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
    monkeypatch.setattr("afterthread.services.llm.get_settings", lambda: settings)
    monkeypatch.setattr("afterthread.services.llm._get_client", lambda: client)
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
    so the AI 日誌 shows the agentic step -- and the same attempt names the tools
    it advertised, which no message body could show (tool specs ride as a
    create() parameter)."""
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
    assert record["attempts"][0]["tools_advertised"] == ["echo"]


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
    the budget-exhausted nudge, and makes ONE final tools-free create().

    The AI 日誌 must be able to tell those rounds apart on its own, so each
    attempt's ``tools_advertised`` is pinned alongside the create() kwargs: the
    names on the two tool rounds, None on the forced finalize."""
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
    # ...and the record says the same thing, attempt by attempt.
    record = llm_log.get_record(llm_log.list_summaries(10)[0]["id"])
    assert record is not None
    assert [a["tools_advertised"] for a in record["attempts"]] == [["echo"], ["echo"], None]


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
    tool-less build: no ``tools`` key at all -- and the attempt records None
    (not []) for what it advertised, matching that absence."""
    client = _install(monkeypatch, _ScriptedClient([_content_completion(_SAMPLE_JSON)]))
    _run()
    assert "tools" not in _calls(client)[0]
    record = llm_log.get_record(llm_log.list_summaries(10)[0]["id"])
    assert record is not None
    assert record["attempts"][0]["tools_advertised"] is None


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
    budget's char allowance, the loop stops advertising tools and makes ONE final
    tools-free completion -- it does NOT keep looping to max_tool_rounds. The D27
    llm_log budget bounds only what is RECORDED; this bounds what is SENT each
    round.

    The budget is now TOKEN-denominated (P4); the scripted completions carry no
    usage, so the estimator stays at the cold-start ratio 1.0 and a 50k-token
    budget yields a 50k-char allowance. Each tool round returns a large result
    (30k chars); across two rounds the accumulated conversation crosses that 50k
    allowance, so the THIRD create() is the finalize (tools-free, budget-exhausted
    nudge appended), even though the default round budget (8) is nowhere near
    spent."""
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
        settings=_settings(llm_tool_conversation_budget_tokens=50_000),
    )
    result = _run(tools=[LlmTool(spec=_tool_spec("big"), handler=handler)])

    assert result.title == "Draft"
    calls = _calls(client)
    # Two tool rounds accumulated ~60k > the 50k allowance; the THIRD create is the
    # tools-free finalize -- NOT a spin to the default max_tool_rounds (8).
    assert len(calls) == 3
    assert "tools" in calls[0] and "tools" in calls[1]
    assert "tools" not in calls[2]
    assert any(m.get("content") == _TOOL_BUDGET_EXHAUSTED for m in calls[2]["messages"])
    assert calls_seen == 2  # exactly the two tool rounds ran before the budget tripped


def test_tool_conversation_budget_default_does_not_trip_normal_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default budget (500_000 tokens -> a 500k-char allowance at the
    cold-start ratio 1.0) leaves an ordinary small-output tool loop untouched: a
    modest result across a couple of rounds never trips F1, so tools stay
    advertised until the model answers on its own."""
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
    package = tools_root / name
    pkg = package / tools._VERSIONS_DIRNAME / _TEST_VID
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
    version_meta = pkg / tools._META_DIRNAME
    version_meta.mkdir()
    (version_meta / tools._ORIGIN_FILENAME).write_text(
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
    (package_meta / tools._CURRENT_FILENAME).write_text(f"{_TEST_VID}\n", encoding="ascii")
    (package_meta / tools._PACKAGE_STATE_FILENAME).write_text(
        json.dumps(_state_document(enabled)), encoding="utf-8"
    )
    if dotenv is not None:
        (package / ".env").write_text(dotenv, encoding="utf-8")
    return pkg


def _package_path(version: Path) -> Path:
    return version.parents[1]


def _package_root(version: Path) -> tools.PackageRoot:
    return tools.PackageRoot(_package_path(version))


def _version_root(version: Path) -> tools.VersionRoot:
    return tools.VersionRoot(version)


def _state_path(version: Path) -> Path:
    return _package_path(version) / tools._META_DIRNAME / tools._PACKAGE_STATE_FILENAME


def _summary_path(version: Path) -> Path:
    return version / tools._META_DIRNAME / tools._SUMMARY_FILENAME


def _resolved_version(package: Path) -> Path:
    vid = (
        (package / tools._META_DIRNAME / tools._CURRENT_FILENAME)
        .read_text(encoding="ascii")
        .rstrip("\n")
    )
    return package / tools._VERSIONS_DIRNAME / vid


def _state_document(enabled: bool) -> dict[str, Any]:
    """The document ``tools.write_package_state`` publishes -- ownership marker included.

    Spelled from the module's own constants rather than a literal, so a test that
    hand-writes a state file is writing whatever the publisher would (P1R5-1): the
    marker is what makes the readers treat the file as OURS at all, and a fixture
    that omitted it would be exercising the FOREIGN path by accident.
    """
    return {tools._STATE_MARKER_KEY: tools._STATE_MARKER_VALUE, "enabled": enabled}


@pytest.mark.parametrize(
    "fault",
    ["absent", "empty", "syntax", "missing", "uncommitted", "version-symlink"],
)
def test_invariant_b_every_bad_current_is_unresolved_without_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str
) -> None:
    """Every specified pointer failure invalidates the whole installed package."""
    root = tmp_path / "tools"
    version = _make_tool(root, "echo", "import sys\nsys.stdout.write('ok')\n")
    package = _package_path(version)
    current = package / tools._META_DIRNAME / tools._CURRENT_FILENAME
    other_vid = "20260728T020304Z-fedcba"

    if fault == "absent":
        current.unlink()
    elif fault == "empty":
        current.write_bytes(b"")
    elif fault == "syntax":
        current.write_text("../../.staging/session\n", encoding="ascii")
    elif fault == "missing":
        current.write_text(f"{other_vid}\n", encoding="ascii")
    elif fault == "uncommitted":
        (version / tools._META_DIRNAME / tools._ORIGIN_FILENAME).unlink()
    else:
        (package / tools._VERSIONS_DIRNAME / other_vid).symlink_to(
            version, target_is_directory=True
        )
        current.write_text(f"{other_vid}\n", encoding="ascii")

    _install_tools(monkeypatch, root)
    resolution = tools.resolve_current(tools.PackageRoot(package))
    assert isinstance(resolution, tools.Unresolved)
    row = list_tools()[0]
    assert row["valid"] is False
    assert row["enabled"] is False
    assert enabled_llm_tools() == []


def test_invariant_b_current_accepts_at_most_one_trailing_newline(tmp_path: Path) -> None:
    version = _make_tool(tmp_path / "tools", "echo", "import sys\n")
    package = _package_path(version)
    current = package / tools._META_DIRNAME / tools._CURRENT_FILENAME
    current.write_text(f"{_TEST_VID}\n\n", encoding="ascii")

    assert isinstance(tools.resolve_current(tools.PackageRoot(package)), tools.Unresolved)


def _add_committed_version(package: Path, vid: str, *, description: str, output: str) -> Path:
    previous = _resolved_version(package)
    version = package / tools._VERSIONS_DIRNAME / vid
    shutil.copytree(previous, version)
    manifest = json.loads((version / "tool.json").read_text(encoding="utf-8"))
    manifest["description"] = description
    (version / "tool.json").write_text(json.dumps(manifest), encoding="utf-8")
    (version / "run.py").write_text(f"import sys\nsys.stdout.write({output!r})\n", encoding="utf-8")
    (version / tools._META_DIRNAME / tools._ORIGIN_FILENAME).write_text(
        json.dumps(
            {
                "source": "test-fixture",
                "openapi_url": None,
                "instructions": None,
                "feedback": "revision",
                "previous": _TEST_VID,
            }
        ),
        encoding="utf-8",
    )
    return version


def test_advertisement_binds_the_vid_while_requiring_some_usable_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A normal revise keeps the package usable without redirecting old handlers."""
    root = tmp_path / "tools"
    first = _make_tool(root, "echo", "import sys\nsys.stdout.write('FIRST')\n")
    package = _package_path(first)
    second_vid = "20260728T020304Z-fedcba"
    _add_committed_version(package, second_vid, description="second", output="SECOND")
    _install_tools(monkeypatch, root)

    handler = enabled_llm_tools()[0].handler
    assert tools.publish_current(tools.PackageRoot(package), second_vid)

    assert asyncio.run(handler({})) == "FIRST"
    assert _resolved_version(package).name == second_vid


def test_advertised_handler_refuses_when_current_becomes_unresolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Invariant B is checked again at call time, before the final identity guard."""
    root = tmp_path / "tools"
    sentinel = tmp_path / "ran"
    version = _make_tool(
        root,
        "echo",
        f"import sys\nopen({str(sentinel)!r}, 'w').write('x')\nsys.stdout.write('ok')\n",
    )
    package = _package_path(version)
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler

    (package / tools._META_DIRNAME / tools._CURRENT_FILENAME).unlink()

    assert asyncio.run(handler({})) == tools._TOOL_REPLACED_RESULT
    assert not sentinel.exists()


def test_invariant_a_running_judgement_enumerates_discarded_version_directories(
    tmp_path: Path,
) -> None:
    version = _make_tool(tmp_path / "tools", "echo", "import sys\n")
    package = _package_path(version)
    discarded = version.with_name(f"{version.name}.discarded")
    os.rename(version, discarded)
    identity = tools.directory_identity(tools.VersionRoot(discarded))
    assert identity is not None

    with tools._inflight_execution(identity):
        assert tools.package_execution_in_flight(tools.PackageRoot(package)) is True


def test_invariant_g_list_resolves_current_once_and_keeps_one_version_per_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    first = _make_tool(
        root,
        "echo",
        "import sys\n",
        tool_json={
            "name": "echo",
            "description": "first description",
            "parameters": {"type": "object", "properties": {}},
            "entry": [sys.executable, "run.py"],
        },
    )
    package = _package_path(first)
    second_vid = "20260728T020304Z-fedcba"
    _add_committed_version(package, second_vid, description="second description", output="SECOND")
    _install_tools(monkeypatch, root)
    real_resolve = tools.resolve_current
    calls = 0

    def resolve_then_switch(package_root: tools.PackageRoot) -> tools.Resolution:
        nonlocal calls
        calls += 1
        resolution = real_resolve(package_root)
        assert tools.publish_current(package_root, second_vid)
        return resolution

    monkeypatch.setattr(tools, "resolve_current", resolve_then_switch)
    row = list_tools()[0]

    assert calls == 1
    assert row == {
        "name": "echo",
        "description": "first description",
        "enabled": True,
        "valid": True,
        "error": None,
        "current_vid": _TEST_VID,
        "lineage": "sole",
    }
    assert _resolved_version(package).name == second_vid


def test_list_reports_all_three_lineage_states_including_a_self_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    first = _make_tool(root, "echo", "import sys\n")
    package = _package_path(first)
    second_vid = "20260728T020304Z-fedcba"
    second = _add_committed_version(package, second_vid, description="second", output="SECOND")
    _install_tools(monkeypatch, root)

    sole = list_tools()[0]
    assert (sole["current_vid"], sole["lineage"]) == (_TEST_VID, "sole")
    sole_resolution = tools.resolve_current(tools.PackageRoot(package))
    assert isinstance(sole_resolution, tools.Resolved)
    assert isinstance(sole_resolution.previous, tools.PreviousNull)

    first_origin = first / tools._META_DIRNAME / tools._ORIGIN_FILENAME
    document = json.loads(first_origin.read_text(encoding="utf-8"))
    document.pop("previous")
    first_origin.write_text(json.dumps(document), encoding="utf-8")
    absent = list_tools()[0]
    assert (absent["current_vid"], absent["lineage"]) == (_TEST_VID, "broken")
    absent_resolution = tools.resolve_current(tools.PackageRoot(package))
    assert isinstance(absent_resolution, tools.Resolved)
    assert isinstance(absent_resolution.previous, tools.PreviousAbsent)
    document["previous"] = None
    first_origin.write_text(json.dumps(document), encoding="utf-8")

    assert tools.publish_current(tools.PackageRoot(package), second_vid)
    usable = list_tools()[0]
    assert (usable["current_vid"], usable["lineage"]) == (second_vid, "usable")
    usable_resolution = tools.resolve_current(tools.PackageRoot(package))
    assert isinstance(usable_resolution, tools.Resolved)
    assert isinstance(usable_resolution.previous, tools.PreviousValue)
    assert usable_resolution.previous.value == _TEST_VID

    origin = second / tools._META_DIRNAME / tools._ORIGIN_FILENAME
    document = json.loads(origin.read_text(encoding="utf-8"))
    document["previous"] = second_vid
    origin.write_text(json.dumps(document), encoding="utf-8")
    broken = list_tools()[0]
    assert (broken["current_vid"], broken["lineage"]) == (second_vid, "broken")


def test_unresolved_row_is_nullable_broken_and_cannot_be_toggled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    version = _make_tool(root, "echo", "import sys\n")
    package = _package_path(version)
    (package / tools._META_DIRNAME / tools._CURRENT_FILENAME).unlink()
    _install_tools(monkeypatch, root)

    row = list_tools()[0]
    assert row["description"] is None
    assert row["current_vid"] is None
    assert row["lineage"] == "broken"
    assert row["valid"] is False
    assert set_enabled("echo", False) is False


def test_version_discard_refuses_null_lineage_without_calling_whole_tool_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    version = _make_tool(tmp_path / "tools", "echo", "import sys\n")
    resolution = tools.resolve_current(_package_root(version))
    assert isinstance(resolution, tools.Resolved)
    called: list[str] = []
    monkeypatch.setattr(
        tools,
        "delete_tool",
        lambda name: called.append(name) is None or True,
    )

    assert tools.discard_version(resolution) == "lineage_unavailable"
    assert called == []
    assert _package_path(version).is_dir()


def test_invariant_e_discard_succeeds_when_old_version_removal_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    first = _make_tool(root, "echo", "import sys\n")
    package = _package_path(first)
    second_vid = "20260728T020304Z-fedcba"
    second = _add_committed_version(package, second_vid, description="second", output="SECOND")
    assert tools.publish_current(tools.PackageRoot(package), second_vid)
    resolution = tools.resolve_current(tools.PackageRoot(package))
    assert isinstance(resolution, tools.Resolved)
    asked: list[tools.PackageRoot] = []
    monkeypatch.setattr(
        tools,
        "package_execution_in_flight",
        lambda root: asked.append(root) is not None,
    )
    real_rmtree = tools.shutil.rmtree
    parked = second.with_name(f"{second_vid}.discarded")

    def fail_old_version(path: Path, *args: Any, **kwargs: Any) -> None:
        if Path(path) == parked:
            raise PermissionError("injected removal failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(tools.shutil, "rmtree", fail_old_version)

    assert tools.discard_version(resolution) == "ok"
    assert _resolved_version(package) == first
    assert not second.exists()
    assert parked.is_dir()
    assert asked == [tools.PackageRoot(package)]
    # Parking removed the advertised path, so its own identity guard is enough;
    # even failed destruction of the parked directory must not leave a marker.
    assert not tools._ADVERTISEMENT_GENERATIONS


def test_discard_parks_before_running_check_then_removes_the_idle_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rename is the gate, so its order is part of the safety property.

    Once V has moved, a handler still trying to start through V's advertised
    path refuses on its own identity check. Only then may the shared running
    judgement say whether the directory we hold can be removed.
    """

    root = tmp_path / "tools"
    first = _make_tool(root, "echo", "import sys\n")
    package = _package_path(first)
    second_vid = "20260728T020304Z-fedcba"
    second = _add_committed_version(package, second_vid, description="second", output="SECOND")
    assert tools.publish_current(tools.PackageRoot(package), second_vid)
    resolution = tools.resolve_current(tools.PackageRoot(package))
    assert isinstance(resolution, tools.Resolved)
    parked = second.with_name(f"{second_vid}.discarded")
    events: list[str] = []
    real_rename = tools.os.rename
    real_rmtree = tools.shutil.rmtree

    def observe_rename(source: Path, target: Path) -> None:
        assert Path(source) == second
        assert Path(target) == parked
        assert second.is_dir()
        events.append("rename")
        real_rename(source, target)

    def nobody_running(package_root: tools.PackageRoot) -> bool:
        assert package_root.path == package
        assert not second.exists()
        assert parked.is_dir()
        events.append("running-check")
        return False

    def observe_remove(path: Path, *args: Any, **kwargs: Any) -> None:
        assert Path(path) == parked
        events.append("remove")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(tools.os, "rename", observe_rename)
    monkeypatch.setattr(tools, "package_execution_in_flight", nobody_running)
    monkeypatch.setattr(tools.shutil, "rmtree", observe_remove)

    assert tools.discard_version(resolution) == "ok"
    assert events == ["rename", "running-check", "remove"]
    assert _resolved_version(package) == first
    assert not parked.exists()
    assert not tools._ADVERTISEMENT_GENERATIONS


def test_discard_rename_failure_is_success_and_leaves_the_version_in_place(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    first = _make_tool(root, "echo", "import sys\n")
    package = _package_path(first)
    second_vid = "20260728T020304Z-fedcba"
    second = _add_committed_version(package, second_vid, description="second", output="SECOND")
    assert tools.publish_current(tools.PackageRoot(package), second_vid)
    resolution = tools.resolve_current(tools.PackageRoot(package))
    assert isinstance(resolution, tools.Resolved)

    def fail_rename(_source: Path, _target: Path) -> None:
        raise PermissionError("injected parking failure")

    monkeypatch.setattr(tools.os, "rename", fail_rename)
    monkeypatch.setattr(
        tools,
        "package_execution_in_flight",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("a failed rename must not license a running check")
        ),
    )

    assert tools.discard_version(resolution) == "ok"
    assert _resolved_version(package) == first
    assert second.is_dir()
    assert not second.with_name(f"{second_vid}.discarded").exists()


def test_invariant_e_unconfirmed_current_durability_leaves_old_version_intact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    first = _make_tool(root, "echo", "import sys\n")
    package = _package_path(first)
    second_vid = "20260728T020304Z-fedcba"
    second = _add_committed_version(package, second_vid, description="second", output="SECOND")
    assert tools.publish_current(tools.PackageRoot(package), second_vid)
    resolution = tools.resolve_current(tools.PackageRoot(package))
    assert isinstance(resolution, tools.Resolved)

    before = {
        str(path.relative_to(second)): (
            stat.S_IMODE(path.lstat().st_mode),
            path.read_bytes() if path.is_file() else None,
        )
        for path in [second, *sorted(second.rglob("*"))]
    }
    meta_info = os.stat(package / tools._META_DIRNAME)
    real_fsync = os.fsync

    def fail_current_directory_fsync(fd: int) -> None:
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (
            meta_info.st_dev,
            meta_info.st_ino,
        ):
            raise OSError("injected current directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(tools.os, "fsync", fail_current_directory_fsync)
    monkeypatch.setattr(
        tools,
        "package_execution_in_flight",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("durability gate must precede the running check")
        ),
    )

    assert tools.discard_version(resolution) == "ok"
    assert _resolved_version(package) == first
    after = {
        str(path.relative_to(second)): (
            stat.S_IMODE(path.lstat().st_mode),
            path.read_bytes() if path.is_file() else None,
        )
        for path in [second, *sorted(second.rglob("*"))]
    }
    assert after == before
    assert not second.with_name(f"{second_vid}.discarded").exists()


@pytest.mark.parametrize("cleanup_failure", ["parking", "durability"])
def test_retired_advertised_version_cannot_run_when_discard_cannot_park_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cleanup_failure: str
) -> None:
    """A successful pointer publication retires V before either cleanup exit."""
    root = tmp_path / "tools"
    sentinel = tmp_path / f"ran-{cleanup_failure}"
    first = _make_tool(root, "echo", "import sys\nsys.stdout.write('FIRST')\n")
    package = _package_path(first)
    package_root = tools.PackageRoot(package)
    second_vid = "20260728T020304Z-fedcba"
    second = _add_committed_version(package, second_vid, description="second", output="SECOND")
    second.joinpath("run.py").write_text(
        f"import sys\nopen({str(sentinel)!r}, 'w').write('x')\nsys.stdout.write('SECOND')\n",
        encoding="utf-8",
    )
    assert tools.publish_current(package_root, second_vid)
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler
    resolution = tools.resolve_current(package_root)
    assert isinstance(resolution, tools.Resolved)
    if cleanup_failure == "parking":

        def fail_rename(source: Path, _target: Path) -> None:
            assert Path(source) == second
            raise PermissionError("injected parking failure")

        monkeypatch.setattr(tools.os, "rename", fail_rename)
    else:
        real_publish = tools.publish_current

        def publish_without_confirmed_durability(
            root: tools.PackageRoot, vid: str
        ) -> tools.CurrentPublication:
            publication = real_publish(root, vid)
            assert publication.published
            return tools.CurrentPublication(published=True, durable=False)

        monkeypatch.setattr(tools, "publish_current", publish_without_confirmed_durability)

    assert tools.discard_version(resolution) == "ok"
    assert _resolved_version(package) == first
    assert second.is_dir()
    assert asyncio.run(handler({})) == tools._TOOL_REPLACED_RESULT
    assert not sentinel.exists()
    # Discard removed the cohort from the lookup table; the old handler itself
    # owns the retired generation for as long as the conversation owns it.
    assert tools.VersionRoot(second) not in tools._ADVERTISEMENT_GENERATIONS


def test_restored_backup_of_retired_vid_executes_in_the_same_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A restored V gets a fresh advertisement generation in the same process.

    Force the unconfirmed-durability outcome so V remains at the advertised path
    and its already-created handler is retired. Restoring a pre-discard backup
    creates another directory identity at that same path; a fresh advertisement
    must run, while the old handler remains retired independently of both
    manifest and directory identities.
    """
    root = tmp_path / "tools"
    first = _make_tool(root, "echo", "import sys\nsys.stdout.write('FIRST')\n")
    package = _package_path(first)
    package_root = tools.PackageRoot(package)
    second_vid = "20260728T020304Z-fedcba"
    second = _add_committed_version(package, second_vid, description="second", output="SECOND")
    assert tools.publish_current(package_root, second_vid)
    backup = tmp_path / "second-backup"
    shutil.copytree(second, backup)
    retired_identity = tools.directory_identity(tools.VersionRoot(second))
    backup_identity = tools.directory_identity(tools.VersionRoot(backup))
    assert retired_identity is not None
    assert backup_identity is not None
    assert backup_identity != retired_identity

    _install_tools(monkeypatch, root)
    stale_handler = enabled_llm_tools()[0].handler
    resolution = tools.resolve_current(package_root)
    assert isinstance(resolution, tools.Resolved)
    real_publish = tools.publish_current

    def publish_without_confirmed_durability(
        target_root: tools.PackageRoot, vid: str
    ) -> tools.CurrentPublication:
        publication = real_publish(target_root, vid)
        assert publication.published
        return tools.CurrentPublication(published=True, durable=False)

    monkeypatch.setattr(tools, "publish_current", publish_without_confirmed_durability)

    assert tools.discard_version(resolution) == "ok"
    shutil.rmtree(second)
    os.rename(backup, second)
    restored_identity = tools.directory_identity(tools.VersionRoot(second))
    assert restored_identity == backup_identity
    assert restored_identity != retired_identity
    assert real_publish(package_root, second_vid)

    fresh_handler = enabled_llm_tools()[0].handler
    assert asyncio.run(fresh_handler({})) == "SECOND"
    assert asyncio.run(stale_handler({})) == tools._TOOL_REPLACED_RESULT


def test_retired_advertisement_survives_rename_aside_and_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No filesystem identity can revive an old handler or poison a fresh one.

    V2's first discard cannot park it, so its advertised handler is retired while
    the directory stays put. Rename that exact directory aside, discard V1, then
    rename V2 back and make it current. The inode and manifest identity are both
    unchanged: the old handler must still refuse because its process-local
    advertisement generation is retired, while a newly advertised handler over
    those SAME filesystem attributes must execute.
    """

    root = tmp_path / "tools"
    first = _make_tool(root, "echo", "import sys\nsys.stdout.write('FIRST')\n")
    package = _package_path(first)
    package_root = tools.PackageRoot(package)
    second_vid = "20260728T020304Z-fedcba"
    second = _add_committed_version(package, second_vid, description="second", output="SECOND")
    third_vid = "20260728T030405Z-acdeff"
    third = package / tools._VERSIONS_DIRNAME / third_vid
    shutil.copytree(first, third)
    first_origin = first / tools._META_DIRNAME / tools._ORIGIN_FILENAME
    first_origin_document = json.loads(first_origin.read_text(encoding="utf-8"))
    first_origin_document["previous"] = third_vid
    first_origin.write_text(json.dumps(first_origin_document), encoding="utf-8")
    assert tools.publish_current(package_root, second_vid)

    _install_tools(monkeypatch, root)
    stale_handler = enabled_llm_tools()[0].handler
    second_identity = tools.directory_identity(tools.VersionRoot(second))
    second_manifest_identity = tools.package_identity(tools.VersionRoot(second))
    assert second_identity is not None
    assert second_manifest_identity is not None

    real_rename = tools.os.rename

    def fail_discard_parking(source: Path, target: Path) -> None:
        assert Path(source) in {first, second}
        assert Path(target).name.endswith(".discarded")
        raise PermissionError("injected parking failure")

    monkeypatch.setattr(tools.os, "rename", fail_discard_parking)

    second_resolution = tools.resolve_current(package_root)
    assert isinstance(second_resolution, tools.Resolved)
    assert tools.discard_version(second_resolution) == "ok"
    assert _resolved_version(package) == first

    aside = second.with_name(f".{second_vid}.aside")
    real_rename(second, aside)
    first_resolution = tools.resolve_current(package_root)
    assert isinstance(first_resolution, tools.Resolved)
    assert tools.discard_version(first_resolution) == "ok"
    assert _resolved_version(package) == third

    real_rename(aside, second)
    assert tools.directory_identity(tools.VersionRoot(second)) == second_identity
    assert tools.package_identity(tools.VersionRoot(second)) == second_manifest_identity
    assert tools.publish_current(package_root, second_vid)

    fresh_handler = enabled_llm_tools()[0].handler
    assert asyncio.run(stale_handler({})) == tools._TOOL_REPLACED_RESULT
    assert asyncio.run(fresh_handler({})) == "SECOND"


def _write_state_file(pkg: Path, enabled: bool) -> None:
    """Hand-write a state file the backend will recognize as its own."""
    _state_path(pkg).write_text(json.dumps(_state_document(enabled)), encoding="utf-8")


def _edit_manifest_in_place(pkg: Path) -> tuple[int, int, int]:
    """Rewrite ``tool.json`` byte-identically until its manifest identity MOVES.

    The stand-in for the one writer that still rewrites an installed manifest now
    that the enabled toggle does not: an operator editing a package's spec by hand
    (a supported action, D21). Byte-identical content on purpose -- the identity is
    about the FILE, not its bytes, which is precisely the reading D40 adjudicated.

    The LOOP is not paranoia, it is a measured property of the filesystem: ext4
    here stamps ``st_ctime_ns`` at ~1 ms granularity (measured), so a rewrite
    landing in the same millisecond as the previous one leaves the tuple
    unchanged. Retrying until the tick advances makes every test that depends on
    "this edit moved the identity" deterministic instead of dependent on how long
    the lines above it happened to take. Returns the NEW identity.
    """
    manifest = pkg / "tool.json"
    raw = manifest.read_text(encoding="utf-8")
    before = tools.package_identity(_version_root(pkg))
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        manifest.write_text(raw, encoding="utf-8")
        after = tools.package_identity(_version_root(pkg))
        assert after is not None
        if after != before:
            assert manifest.read_text(encoding="utf-8") == raw  # the BYTES never changed
            return after
    raise AssertionError("the manifest identity never moved across an in-place rewrite")


def _install_tools(monkeypatch: pytest.MonkeyPatch, tools_root: Path, **overrides: Any) -> Settings:
    settings = Settings(tools_dir=str(tools_root), **overrides)
    monkeypatch.setattr("afterthread.services.tools.get_settings", lambda: settings)
    return settings


def _wait_for(condition: Callable[[], bool], *, timeout: float = 30.0) -> None:
    """Poll until ``condition`` holds, or fail loudly -- never hang the suite."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached in time")


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


def test_runtime_refuses_a_package_replaced_after_it_was_advertised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tool whose package was REPLACED between being advertised and being called
    refuses instead of running, and the unchanged package still runs normally.

    ``enabled_llm_tools()`` snapshots each tool's schema at the START of an AI
    workflow, but the handler resolves and executes from the PATH minutes later --
    and an ordinary capture/enrich is NOT inside the tool job's single-flight, so a
    revise can swap the package underneath a conversation that already advertised
    the old one. The model would then be answering against a schema the running
    entry no longer implements.

    The swap here is revise-shaped: a fresh ``tool.json`` written into place (a
    different inode, and a different ctime even if it were not), leaving the name
    and the entry file alone -- so nothing but the manifest identity can tell.
    The sentinel is what proves the refusal happened INSTEAD of a run rather than
    alongside it: the entry writes that file the moment it executes."""
    root = tmp_path / "tools"
    sentinel = tmp_path / "ran"
    pkg = _make_tool(
        root,
        "swapped",
        f"import sys\nopen({str(sentinel)!r}, 'w').write('x')\nsys.stdout.write('ok')\n",
    )
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler

    # Unchanged package: executes exactly as before.
    assert asyncio.run(handler({})) == "ok"
    assert sentinel.exists()
    sentinel.unlink()

    manifest = json.loads((pkg / "tool.json").read_text(encoding="utf-8"))
    replacement = pkg / "tool.json.new"
    replacement.write_text(json.dumps(manifest), encoding="utf-8")
    os.replace(replacement, pkg / "tool.json")

    assert asyncio.run(handler({})) == tools._TOOL_REPLACED_RESULT
    assert not sentinel.exists()  # nothing was started


def test_runtime_refuses_when_the_advertised_identity_could_not_be_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ "Cannot say" is a REFUSAL, never a pass.

    ``package_identity`` answers None on any lstat failure, and ``None == None``
    would otherwise read as "identity matches" -- the exact hole the revise flow
    closed by refusing up front when it cannot establish an identity (D40 P3b
    r11). Patched at the helper so BOTH the advertise-time capture and the
    execute-time check return None, which is the state a transient stat failure
    produces."""
    root = tmp_path / "tools"
    sentinel = tmp_path / "ran"
    _make_tool(
        root,
        "unknown",
        f"import sys\nopen({str(sentinel)!r}, 'w').write('x')\nsys.stdout.write('ok')\n",
    )
    _install_tools(monkeypatch, root)
    monkeypatch.setattr(tools, "package_identity", lambda _directory: None)

    assert asyncio.run(enabled_llm_tools()[0].handler({})) == tools._TOOL_REPLACED_RESULT
    assert not sentinel.exists()


def test_runtime_refuses_a_tool_disabled_after_it_was_offered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R4: the refusal that used to be an ACCIDENT, made explicit.

    Before web-v5 P1 a toggle rewrote ``tool.json``, so a tool switched off
    mid-conversation was refused by the IDENTITY check -- it saw the manifest's
    ctime move and reported the package as replaced. The toggle no longer touches
    that file, so that accident is gone; without a check of its own, a disabled
    tool would simply RUN (overall-r7 finding O7-1(a), reintroduced).

    The handler therefore re-reads ``.afterthread-state.json`` at CALL time, and the refusal
    carries its OWN string: nothing about the package changed, so telling the
    model it was replaced would send it to re-read a spec that is still exactly
    what it was given."""
    root = tmp_path / "tools"
    sentinel = tmp_path / "ran"
    _make_tool(
        root,
        "toggled",
        f"import sys\nopen({str(sentinel)!r}, 'w').write('x')\nsys.stdout.write('ok')\n",
    )
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler
    assert asyncio.run(handler({})) == "ok"
    sentinel.unlink()

    assert set_enabled("toggled", False) is True

    assert asyncio.run(handler({})) == tools._TOOL_DISABLED_RESULT
    assert asyncio.run(handler({})) != tools._TOOL_REPLACED_RESULT  # a DIFFERENT answer
    assert not sentinel.exists()  # nothing was started


def test_an_off_then_on_toggle_no_longer_refuses_the_rest_of_the_conversation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The FALSE POSITIVE web-v5 P1 removes, pinned so it cannot creep back.

    D40 accepted, and pinned, that toggling a tool during a conversation that had
    already advertised it made every LATER call refuse -- including an off-then-on
    that leaves the tool exactly as it was found -- because the toggle rewrote
    ``tool.json`` and the identity check could not tell an enabled-flip from a
    wholesale replacement. Moving the toggle out of the manifest is what retires
    that whole reading: the switch ends up back ON, nothing was replaced, and the
    tool runs."""
    root = tmp_path / "tools"
    _make_tool(root, "toggled", "import sys\nsys.stdout.write('ok')\n")
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler
    assert asyncio.run(handler({})) == "ok"

    assert set_enabled("toggled", False) is True
    assert set_enabled("toggled", True) is True

    assert asyncio.run(handler({})) == "ok"


def test_runtime_refuses_after_a_hand_edit_rewrites_the_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The identity check's live example of drift, now that no toggle supplies one.

    Hand-editing a package's files is a SUPPORTED operator action (D21), and an
    in-place rewrite of ``tool.json`` still moves its ctime -- so a spec edited
    during a conversation that already advertised the old spec makes every later
    call in that conversation refuse, even for an edit-and-revert that leaves the
    bytes identical. That is the same conservative direction D40 adjudicated, just
    reached by the only writer that still reaches it: the alternative reading
    ("the manifest was merely rewritten, carry on") cannot tell an edit from a
    wholesale replacement."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "edited", "import sys\nsys.stdout.write('ok')\n")
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler
    assert asyncio.run(handler({})) == "ok"

    _edit_manifest_in_place(pkg)  # byte-identical, and STILL a rewrite

    assert asyncio.run(handler({})) == tools._TOOL_REPLACED_RESULT


def test_a_toggle_during_the_scan_cannot_advertise_the_tool_it_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The window the identity capture had to move INTO ``_scan_package`` to close.

    ``enabled_llm_tools`` materializes ``_scan_all()`` in full before the FIRST
    handler is built, so with the identity taken at BUILD time the gap between
    reading tool A's manifest and lstat'ing it spanned the scan of every other
    package -- an unbounded number of file reads, not a syscall pair. A toggle
    landing in there paired A's stale ``enabled``/spec/entry with the identity of
    the manifest the toggle had ALREADY rewritten, so every downstream guard
    compared equal and the disabled tool ran.

    Driven from INSIDE the scan rather than from a racing thread, so it is the
    window itself that is pinned and the test cannot flake: ``_scan_all`` walks
    sorted names, so a toggle performed while "zzz" is being scanned is exactly
    "after A was scanned, before any handler exists".

    Advertising the stale row is NOT what this fixes (the scan genuinely read
    ``enabled: true``, and D40 accepts a one-request-stale registry); what it
    fixes is that the call is refused rather than run.

    WHICH refusal changed with web-v5 P1, and the new one is the load-bearing
    half. R7-1's fix worked by capturing the identity early, so the toggle's
    manifest rewrite made the call MISMATCH. A toggle no longer rewrites anything,
    so this window is now closed by the handler's own execution-time read of
    ``.afterthread-state.json`` (R4) -- the same conservative direction, reached by the check
    that is actually about the question being asked."""
    root = tmp_path / "tools"
    sentinel = tmp_path / "ran"
    _make_tool(
        root,
        "aaa",
        f"import sys\nopen({str(sentinel)!r}, 'w').write('x')\nsys.stdout.write('ok')\n",
    )
    _make_tool(root, "zzz", "import sys\nsys.stdout.write('z')\n")
    _install_tools(monkeypatch, root)

    real_scan = tools.scan_installed

    def scan_then_toggle(package_root: tools.PackageRoot) -> tools._PackageScan:
        scan = real_scan(package_root)
        if package_root.path.name == "zzz":
            assert set_enabled("aaa", False) is True
        return scan

    monkeypatch.setattr(tools, "scan_installed", scan_then_toggle)
    advertised = {tool.spec["function"]["name"]: tool.handler for tool in enabled_llm_tools()}

    assert asyncio.run(advertised["aaa"]({})) == tools._TOOL_DISABLED_RESULT
    assert not sentinel.exists()  # the disabled tool was never started
    # The control: capturing A's identity EARLIER must not make an untouched
    # package in the same scan refuse. Nothing rewrote zzz's manifest, so it runs.
    assert asyncio.run(advertised["zzz"]({})) == "z"


def test_a_deleted_state_file_falls_back_to_a_manifest_that_disables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1R2-1: at CALL time, ABSENT is an ANSWER -- the manifest's -- not a silence.

    The check used to refuse only on ``state.present and not state.enabled``, on
    the reasoning that the identity check above it had just proven ``tool.json``
    unmoved, so a package with no state file still said what it said when it was
    advertised. The premise is false in one word: the package did not HAVE "no
    state file" when it was advertised -- it had one saying True, and the file was
    DELETED since. The identity check speaks for ``tool.json``; it never looks at
    ``.afterthread-state.json``.

    Every step below is a documented operation. A package installed before web-v5
    P1 carries its toggle in the manifest and here it says OFF; a PATCH switches it
    on (writing the state file, R1's migration); the tool is advertised and this
    handler built; then the operator deletes the state file, which is the repair
    both READMEs give for a corrupt one. The effective state by the precedence rule
    is the manifest's False again, and this is a stable state -- not a
    check-then-act instant -- so a partial check would start the subprocess for as
    long as that conversation lasts."""
    root = tmp_path / "tools"
    sentinel = tmp_path / "ran"
    pkg = _make_tool(
        root,
        "legacy",
        f"import sys\nopen({str(sentinel)!r}, 'w').write('x')\nsys.stdout.write('ok')\n",
        enabled=False,  # a PRE-MIGRATION manifest, and it says off
    )
    _install_tools(monkeypatch, root)
    assert enabled_llm_tools() == []  # ... so the fallback keeps it off the wire

    assert set_enabled("legacy", True) is True
    handler = enabled_llm_tools()[0].handler
    assert asyncio.run(handler({})) == "ok"  # advertised and runnable, on the state file
    sentinel.unlink()

    _state_path(pkg).unlink()  # the documented repair, mid-conversation

    assert asyncio.run(handler({})) == tools._TOOL_DISABLED_RESULT
    assert asyncio.run(handler({})) != tools._TOOL_REPLACED_RESULT  # nothing was replaced
    assert not sentinel.exists()  # nothing was started
    # ... and the listing agrees, because both now ask the same one rule.
    assert list_tools()[0]["enabled"] is False


def test_invariant_c_a_deleted_state_file_disables_even_when_manifest_enables_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing package-layer state never falls back to manifest ``enabled``."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "legacy", "import sys\nsys.stdout.write('ok')\n", enabled=True)
    _install_tools(monkeypatch, root)
    assert set_enabled("legacy", True) is True  # migrates it: the file now exists
    assert _state_path(pkg).is_file()
    handler = enabled_llm_tools()[0].handler

    _state_path(pkg).unlink()

    assert asyncio.run(handler({})) == tools._TOOL_DISABLED_RESULT
    assert list_tools()[0]["enabled"] is False


def test_runtime_refuses_a_tool_whose_state_file_became_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The third state, at CALL time: unknown intent refuses, exactly as it lists.

    R2 already pins that an unreadable state file makes a package invalid AND
    switched off in the listing. The execution path has to reach the same answer
    through the same rule -- a tool that cannot say whether it may run must not run
    -- and it must reach it whichever way ``package_enabled`` is spelled, which is
    why this is pinned at the handler rather than only at the scan.

    The file here is OURS and unusable (the marker is there, the answer is not),
    which is what "unreadable" narrowed to in P1R5-1: a document with no marker is
    somebody else's and is answered from the manifest instead."""
    root = tmp_path / "tools"
    sentinel = tmp_path / "ran"
    pkg = _make_tool(
        root,
        "torn",
        f"import sys\nopen({str(sentinel)!r}, 'w').write('x')\nsys.stdout.write('ok')\n",
    )
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler
    assert asyncio.run(handler({})) == "ok"
    sentinel.unlink()

    _state_path(pkg).write_text(
        json.dumps({tools._STATE_MARKER_KEY: tools._STATE_MARKER_VALUE, "enabled": None}),
        encoding="utf-8",
    )

    assert asyncio.run(handler({})) == tools._TOOL_DISABLED_RESULT
    assert not sentinel.exists()


def test_a_reader_that_opened_the_state_file_sees_one_whole_published_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1R4-3: the property the execution path actually has, stated as a test.

    Reading ``.afterthread-state.json`` LAST does NOT make the value provably current at the
    moment it is acted on, and a docstring next door used to say it did.
    ``_read_regular_file_capped`` selects the version at its ``open``, not at its
    ``read``, and the publisher swaps the file in with ``os.replace`` -- so a PATCH
    that lands after the reader's ``open`` leaves the reader answering out of an
    inode that is no longer the published one. Closing that would take a lock on
    the execution path, serializing every tool call against every toggle, which
    this subsystem deliberately does not do.

    What IS true is pinned here instead, and it is the reason the residual is
    acceptable: a reader gets ONE whole published version, never a torn file and
    never half of each. The held fd stands in for a runtime that opened the old
    inode a syscall before the PATCH; it still reads the complete previous document
    afterwards, while the next reader sees the new one."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "swapped-under", "import sys\n")
    _install_tools(monkeypatch, root)
    assert set_enabled("swapped-under", True) is True
    state_path = _state_path(pkg)

    fd = os.open(state_path, os.O_RDONLY)  # the version is chosen HERE
    try:
        assert set_enabled("swapped-under", False) is True  # published mid-read
        with os.fdopen(fd, encoding="utf-8") as handle:
            fd = -1  # fdopen owns it now
            held = handle.read()
    finally:
        if fd >= 0:
            os.close(fd)

    assert json.loads(held) == _state_document(True)  # whole, parseable, the OLD version
    fresh = tools._read_enabled_state(_package_root(pkg))
    assert (fresh.ours, fresh.enabled, fresh.error) == (True, False, None)


def test_invariant_c_absent_state_never_reads_the_legacy_manifest_toggle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The removed legacy fallback is absent from advertisement and execution."""
    root = tmp_path / "tools"
    pkg = _make_tool(
        root,
        "busy",
        "import sys\nsys.stdout.write('ok')\n",
        enabled=True,
    )
    _install_tools(monkeypatch, root)
    _state_path(pkg).unlink()
    assert not _state_path(pkg).exists()

    assert tools.package_enabled(_package_root(pkg)) is False
    assert enabled_llm_tools() == []


def test_execution_rechecks_toggle_and_current_without_scanning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Call time rechecks the package toggle and invariant B without a full scan.

    ``resolve_current`` intentionally follows the toggle read now: listing a
    package as broken/disabled while an old handler can still run violates the
    invariant. The advertised vid is not compared here, and the manifest identity
    check remains the final operation before Popen in the ordering test below.
    """
    root = tmp_path / "tools"
    _make_tool(root, "traced", "import sys\nsys.stdout.write('ok')\n")
    _install_tools(monkeypatch, root)
    assert set_enabled("traced", True) is True  # give it a state file
    handler = enabled_llm_tools()[0].handler  # advertisement scans; probes go on after

    trace: list[str] = []
    real_state = tools._read_enabled_state
    real_scan = tools.scan_installed
    real_resolve = tools.resolve_current
    real_popen = subprocess.Popen

    def traced_state(directory: tools.PackageRoot) -> tools._EnabledState:
        trace.append("state")
        return real_state(directory)

    def traced_scan(directory: tools.PackageRoot) -> tools._PackageScan:
        trace.append("scan")
        return real_scan(directory)

    def traced_resolve(directory: tools.PackageRoot) -> tools.Resolution:
        trace.append("resolve")
        return real_resolve(directory)

    def traced_popen(*args: Any, **kwargs: Any) -> Any:
        trace.append("popen")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(tools, "_read_enabled_state", traced_state)
    monkeypatch.setattr(tools, "scan_installed", traced_scan)
    monkeypatch.setattr(tools, "resolve_current", traced_resolve)
    monkeypatch.setattr(subprocess, "Popen", traced_popen)

    assert asyncio.run(handler({})) == "ok"

    assert trace[-3:] == ["state", "resolve", "popen"]
    assert "scan" not in trace
    assert trace.count("state") == 2  # once per site (handler entry, pre-Popen)
    assert trace.count("resolve") == 1


def test_the_scan_and_the_execution_check_answer_the_one_rule_identically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One spelling, demonstrated rather than asserted (P1R3-1).

    ``package_enabled`` stopped BEING ``_scan_package(directory).enabled``, so
    "they cannot disagree" stopped being true by construction and became true by
    both calling ``_effective_enabled`` with the reads they are already holding.
    That is only worth having if it is checked, so this walks every shape the two
    can be asked about -- the four states of the state file, both directions of
    the manifest fallback behind an ABSENT one and behind a FOREIGN one, and each
    way the manifest itself can fail to offer a legacy key -- and requires the same
    answer from both.

    FOREIGN is the shape P1R5-1 added, and it is here in BOTH manifest directions
    for the reason ABSENT is: the point is not that the answer is True or False, it
    is that the file at our name did not get a vote either way.

    The invalid rows are included deliberately: ``enabled`` is reported for a
    package that is not runnable too (the listing shows the switch), so a rule that
    diverged only on those would diverge exactly where nobody looks.

    The SYMLINKED directory is the ninth shape and the only one neither side sends
    through ``_effective_enabled``: both REFUSE TO LOOK before joining a name onto
    such a path, so the equality here is the only thing keeping those two hard-coded
    answers in step. It was added in P1R4-2 together with the answer itself, which
    flipped from True to False: True was justified by "the row is invalid, so
    nothing that consults ``valid`` runs it", and the EXECUTION path does not
    consult ``valid`` (see
    ``test_a_symlinked_package_directory_cannot_run_a_tool_disabled_through_the_api``
    for the sequence that ran a disabled tool through one). A refusal to look now
    answers the way every other refusal to look in this module answers: closed."""
    root = tmp_path / "tools"
    absent = _make_tool(root, "absent", "import sys\n")
    present_on = _make_tool(root, "present-on", "import sys\n", enabled=True)
    present_off = _make_tool(root, "present-off", "import sys\n", enabled=False)
    unreadable = _make_tool(root, "unreadable", "import sys\n")
    foreign = _make_tool(root, "foreign", "import sys\n")
    _state_path(absent).unlink()
    _state_path(unreadable).write_text(
        json.dumps(
            {
                tools._STATE_MARKER_KEY: tools._STATE_MARKER_VALUE,
                "enabled": "yes",
            }
        ),
        encoding="utf-8",
    )
    _state_path(foreign).write_text('{"enabled": true}', encoding="utf-8")
    _install_tools(monkeypatch, root)

    expected = {
        absent: False,
        present_on: True,
        present_off: False,
        unreadable: False,
        foreign: False,
    }
    for version, answer in expected.items():
        package_root = _package_root(version)
        assert tools.scan_installed(package_root).enabled is answer, version
        assert tools.package_enabled(package_root) is answer, version

    linked = root / "linked"
    linked.symlink_to(_package_path(present_on), target_is_directory=True)
    linked_root = tools.PackageRoot(linked)
    assert tools.scan_installed(linked_root).enabled is False
    assert tools.package_enabled(linked_root) is False


def test_the_identity_check_is_the_last_thing_before_the_subprocess_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1R4-1: two checks want the slot above ``Popen`` and only one can have it.

    Round 3 left the order ``identity -> toggle -> Popen``, which put a state-file
    read -- and on the ABSENT path a manifest read as well -- between the identity
    ANSWER and the exec that acts on it. ``cwd`` is resolved by the KERNEL from the
    PATH at exec time, so a revise landing in that gap does not merely race: the
    child starts inside the NEW package carrying the OLD entry argv, the old
    schema's arguments and the old package's ``.env`` values, and nothing afterwards
    shows it (an attempt records the NAME, which did not change) -- the hazard D40's
    overall-r6 O6-1 was rated P1 for.

    The toggle and invariant-B checks both precede identity. A switch flipped
    inside those bounded reads can still land in the accepted check-then-act
    instant; putting either after identity would reopen the more severe redirect
    window because ``cwd`` is resolved from a path at Popen.

    The HANDLER keeps the opposite order, and the full trace pins that too: neither
    of its checks is adjacent to anything (a ``.env`` read, a serialization and a
    queue wait follow both), so nothing is bought by being second there, while
    identity-first means an already-replaced package is reported as replaced instead
    of having its successor's toggle consulted. Pinned as ORDER because these are
    exactly the lines a refactor moves as though they were free."""
    root = tmp_path / "tools"
    _make_tool(root, "ordered", "import sys\nsys.stdout.write('ok')\n")
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler  # the advertisement scans; probe after it

    trace: list[str] = []
    real_enabled = tools.package_enabled
    real_resolve = tools.resolve_current
    real_identity = tools._still_the_expected_package
    real_popen = subprocess.Popen

    def traced_enabled(directory: tools.PackageRoot) -> bool:
        trace.append("enabled")
        return real_enabled(directory)

    def traced_identity(
        directory: tools.VersionRoot, expected: tuple[int, int, int] | None
    ) -> bool:
        trace.append("identity")
        return real_identity(directory, expected)

    def traced_resolve(directory: tools.PackageRoot) -> tools.Resolution:
        trace.append("resolve")
        return real_resolve(directory)

    def traced_popen(*args: Any, **kwargs: Any) -> Any:
        trace.append("popen")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(tools, "package_enabled", traced_enabled)
    monkeypatch.setattr(tools, "resolve_current", traced_resolve)
    monkeypatch.setattr(tools, "_still_the_expected_package", traced_identity)
    monkeypatch.setattr(subprocess, "Popen", traced_popen)

    assert asyncio.run(handler({})) == "ok"

    assert trace[-3:] == ["resolve", "identity", "popen"]  # identity is the last word
    # ... and the handler's own pair is deliberately the other way round.
    assert trace == ["identity", "enabled", "enabled", "resolve", "identity", "popen"]


def test_a_revise_landing_between_the_toggle_read_and_popen_is_refused_not_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The order above, driven rather than traced: the gap must REFUSE a swap.

    The probe replaces the package the way ``_promote_staging_replace`` does (rename
    the live one aside, rename the new one in) from inside the pre-``Popen`` toggle
    read -- i.e. after the handler's checks and strictly before the identity check
    that now follows it. With the identity check second the swap is caught and the
    call answers ``_TOOL_REPLACED_RESULT``; with round 3's order it was the identity
    check that ran first and this exact probe measured ``'NEW'`` coming back to the
    model, the replacement's code executed under the old contract.

    The state file is created up front so ``package_enabled`` takes its PRESENT path
    and reads once per site -- the ABSENT path reads twice, which would land the
    probe at the handler instead of at ``Popen``."""
    root = tmp_path / "tools"
    sentinel = tmp_path / "new-ran"
    _make_tool(root, "swapped", "import sys\nsys.stdout.write('OLD')\n")
    replacement_version = _make_tool(
        tmp_path / "staging",
        "swapped",
        f"import sys\nopen({str(sentinel)!r}, 'w').write('x')\nsys.stdout.write('NEW')\n",
    )
    _install_tools(monkeypatch, root)
    assert set_enabled("swapped", True) is True  # a state file: ONE read per site
    pkg = root / "swapped"
    handler = enabled_llm_tools()[0].handler

    real_state = tools._read_enabled_state
    reads: list[Path] = []

    def swap_inside_the_gap(directory: tools.PackageRoot) -> tools._EnabledState:
        reads.append(directory.path)
        if len(reads) == 2:  # the pre-``Popen`` site, not the handler's entry
            os.rename(pkg, root / ".swapped.bak-probe")
            os.rename(_package_path(replacement_version), pkg)
        return real_state(directory)

    monkeypatch.setattr(tools, "_read_enabled_state", swap_inside_the_gap)

    assert asyncio.run(handler({})) == tools._TOOL_REPLACED_RESULT
    assert (
        _resolved_version(pkg).joinpath("run.py").read_text(encoding="utf-8").endswith("'NEW')\n")
    )  # it DID swap
    assert not sentinel.exists()  # ... and the new package never ran


def test_a_symlinked_package_directory_cannot_run_a_tool_disabled_through_the_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1R4-2: the execution path never consults ``valid``, so the row cannot guard it.

    ``package_enabled`` refused to LOOK into a symlinked package directory and
    answered True, on the grounds that such a row is INVALID and nothing that
    consults ``valid`` advertises or runs it. The handler is the counter-example: it
    was BUILT while the package was a real directory, and it holds a path. So this
    walks the whole documented sequence -- advertise, switch OFF through the API,
    move the directory aside, plant a link of the same name -- and every check that
    was supposed to stop it passed: ``package_identity`` follows PARENT symlinks, so
    it still saw the same ``tool.json`` inode (asserted below, because that is WHY
    the identity check cannot answer this one), the toggle check answered True, and
    ``Popen`` followed the link and ran a disabled tool.

    Both contracts were broken at once -- the operator's switch and the standing
    "a symlinked package is invalid and is never executed" rule -- so the refusal
    goes where the ``lstat`` already is, and it answers the way this module answers
    every other refusal to look: closed."""
    root = tmp_path / "tools"
    sentinel = tmp_path / "ran"
    _make_tool(
        root,
        "sneaky",
        f"import sys\nopen({str(sentinel)!r}, 'w').write('x')\nsys.stdout.write('ok')\n",
    )
    _install_tools(monkeypatch, root)
    pkg = root / "sneaky"
    identity_before = tools.package_identity(_version_root(pkg))
    handler = enabled_llm_tools()[0].handler
    assert asyncio.run(handler({})) == "ok"  # a legitimate package still runs
    sentinel.unlink()

    assert set_enabled("sneaky", False) is True
    assert asyncio.run(handler({})) == tools._TOOL_DISABLED_RESULT

    moved = root / "sneaky-moved"
    os.rename(pkg, moved)
    pkg.symlink_to(moved, target_is_directory=True)

    # The identity check is UNMOVED across the swap -- it lstats <dir>/tool.json and
    # the link leads straight back to the same inode -- which is exactly why it is
    # not the check that can answer this.
    assert pkg.is_symlink()
    assert tools.package_identity(_version_root(pkg)) == identity_before

    assert asyncio.run(handler({})) == tools._TOOL_DISABLED_RESULT
    assert not sentinel.exists()  # nothing was started


def test_a_promote_during_the_scan_cannot_run_the_new_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same window with a replace-mode promote, which is the redirect hazard.

    A ``cwd`` is resolved by the kernel from the PATH at exec, so pairing A's old
    entry argv and old parameter schema with the identity of the package that
    replaced A does not merely race -- it runs the NEW package's files under the
    OLD contract, with the new package's ``.env``, and the AI log records only the
    NAME, which did not change. The swap is spelled the way ``_promote_staging_
    replace`` spells it (rename the old package aside, rename the new one in), and
    it is performed from inside a later package's scan for the same determinism as
    the toggle above."""
    root = tmp_path / "tools"
    _make_tool(root, "aaa", "import sys\nsys.stdout.write('OLD')\n")
    _make_tool(root, "zzz", "import sys\nsys.stdout.write('z')\n")
    replacement = _package_path(
        _make_tool(tmp_path / "staging", "aaa", "import sys\nsys.stdout.write('NEW')\n")
    )
    _install_tools(monkeypatch, root)

    pkg = root / "aaa"
    real_scan = tools.scan_installed

    def scan_then_promote(package_root: tools.PackageRoot) -> tools._PackageScan:
        scan = real_scan(package_root)
        if package_root.path.name == "zzz":
            os.rename(pkg, root / ".aaa.bak-r7")
            os.rename(replacement, pkg)
        return scan

    monkeypatch.setattr(tools, "scan_installed", scan_then_promote)
    advertised = {tool.spec["function"]["name"]: tool.handler for tool in enabled_llm_tools()}

    assert asyncio.run(advertised["aaa"]({})) == tools._TOOL_REPLACED_RESULT
    assert (_resolved_version(pkg) / "run.py").read_text(encoding="utf-8").endswith("'NEW')\n")


def test_the_identity_is_taken_before_the_manifest_read_so_the_gap_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ORDER inside ``_scan_package``, which is the whole of R7-1 rather than
    a detail of it -- and the one thing the two tests above cannot see, since a
    swap landing after the whole scan is caught under either ordering.

    The swap is driven into the residual gap itself: it happens the instant the
    manifest READ returns, so it falls strictly between the two syscalls that
    remain paired. lstat-then-read (what ships) pins the OLD identity against the
    OLD spec here and against a NEW spec when the swap lands one syscall earlier
    -- either way the call refuses. read-then-lstat pins the OLD spec against the
    NEW identity, which compares EQUAL at execution and runs the replacement
    under the old contract: moving that one line below the read turns this
    assertion into ``'NEW'``."""
    root = tmp_path / "tools"
    _make_tool(root, "aaa", "import sys\nsys.stdout.write('OLD')\n")
    replacement = _package_path(
        _make_tool(tmp_path / "staging", "aaa", "import sys\nsys.stdout.write('NEW')\n")
    )
    _install_tools(monkeypatch, root)

    pkg = root / "aaa"
    real_read = tools._read_regular_file_capped
    swapped = False

    def read_then_promote(path: Path, cap: int) -> str | None:
        nonlocal swapped
        text = real_read(path, cap)
        if not swapped and path == _resolved_version(pkg) / "tool.json":
            swapped = True
            os.rename(pkg, root / ".aaa.bak-r7")
            os.rename(replacement, pkg)
        return text

    monkeypatch.setattr(tools, "_read_regular_file_capped", read_then_promote)
    handler = enabled_llm_tools()[0].handler
    assert swapped  # the swap really landed in the gap, not somewhere harmless

    assert asyncio.run(handler({})) == tools._TOOL_REPLACED_RESULT


def test_runtime_identity_survives_a_summary_sidecar_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Writing the AI summary sidecar does NOT trip the identity check.

    ``.ai_meta.json`` is a different file, so the manifest's own ctime is
    untouched -- which matters because a summary is generated (and regenerated)
    while conversations are live, and a check that fired on it would refuse every
    tool in the vicinity of an unrelated feature."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "summarized", "import sys\nsys.stdout.write('ok')\n")
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler

    assert tools.write_tool_meta(
        _version_root(pkg),
        {"summary": "what it does", "updated_at": "2026-07-27T00:00:00+00:00"},
    )

    assert asyncio.run(handler({})) == "ok"


def test_the_two_identities_answer_two_different_questions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The measurements the split rests on, pinned so neither can be "simplified"
    into the other.

    ``package_identity`` answers "is this still the same PACKAGE?" and
    ``directory_identity`` answers "is anything still running out of these
    FILES?", and each is WRONG for the other's question:

    * an in-place manifest rewrite MOVES the manifest identity -- which is what
      makes it a package identity, and what makes it useless as an execution key:
      a running child would vanish from the registry. Since web-v5 P1 the only
      writer that still reaches it mid-life is an operator's HAND-EDIT (D21), so
      that is what this measures;
    * a delete-and-recreate REUSES the directory inode (the measurement D40 P3b
      r10 settled the package question on), so the directory identity must never
      be read as "the same package";
    * a rename carries the directory identity -- the property both deferral
      writers depend on, since they rename first and ask afterwards.

    The split OUTLIVES the case that forced it, which is why it is still here: R5
    introduced it because ``set_enabled`` moved the manifest identity under a
    running child, and the toggle no longer does -- but a hand-edit does, so the
    two questions still need two answers."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    manifest_before, directory_before = (
        tools.package_identity(_version_root(pkg)),
        tools.directory_identity(_version_root(pkg)),
    )
    assert manifest_before is not None and directory_before is not None

    assert _edit_manifest_in_place(pkg) != manifest_before  # the package "changed"
    assert (
        tools.directory_identity(_version_root(pkg)) == directory_before
    )  # the files did not move

    package = _package_path(pkg)
    renamed_package = root / ".echo.stale-x"
    os.rename(package, renamed_package)
    renamed = _resolved_version(renamed_package)
    assert (
        tools.directory_identity(_version_root(renamed)) == directory_before
    )  # the name moved, not the inode
    assert (
        tools.directory_identity(_version_root(pkg)) is None
    )  # ... and nothing answers for the old name

    shutil.rmtree(renamed_package)
    reinstalled = _make_tool(root, "echo", "import sys\nsys.stdout.write('y')\n")
    assert (
        tools.package_identity(_version_root(reinstalled)) != manifest_before
    )  # a NEW package, always
    # The directory inode is routinely REUSED here, which is exactly why the
    # question above cannot be answered with it. Asserted as "may be equal" rather
    # than "is equal" because inode allocation is the filesystem's business: the
    # claim being pinned is that this tuple does not distinguish packages.
    assert tools.directory_identity(_version_root(reinstalled)) is not None


def test_runtime_registers_the_execution_for_as_long_as_the_child_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The identity check answers for the START of a call; this registration is
    what covers its DURATION.

    A replace-mode promote renames the old package aside -- which a running child
    does not even notice, its cwd being the inode -- and then DELETES it, which is
    what pulls the files out from under the child. So the runtime publishes "a
    subprocess is executing this package" for exactly as long as one can still be
    reading it, and ``tool_builder``'s promote consults that before dropping its
    backup. Asserted against a real child that blocks until this test releases it
    (and that announces itself first), so the True is genuinely concurrent with a
    running process rather than inferred from the handler having been entered.

    Published under the DIRECTORY's identity, which is the one the removal
    threatens and the one every consumer re-derives from the directory it holds
    (see ``tools.directory_identity``)."""
    root = tmp_path / "tools"
    marker, gate = tmp_path / "started", tmp_path / "go"
    pkg = _make_tool(
        root,
        "slow",
        "import os, sys, time\n"
        f"open({str(marker)!r}, 'w').write('x')\n"
        f"while not os.path.exists({str(gate)!r}):\n"
        "    time.sleep(0.01)\n"
        "sys.stdout.write('ok')\n",
    )
    _install_tools(monkeypatch, root)
    identity = tools.directory_identity(_version_root(pkg))
    assert identity is not None
    handler = enabled_llm_tools()[0].handler
    assert tools.directory_execution_in_flight(identity) is False

    result: dict[str, str] = {}
    caller = threading.Thread(target=lambda: result.update(out=asyncio.run(handler({}))))
    caller.start()
    try:
        _wait_for(marker.exists)
        assert tools.directory_execution_in_flight(identity) is True
    finally:
        gate.write_text("go", encoding="utf-8")
        caller.join(timeout=30)

    assert result["out"] == "ok"
    assert tools.directory_execution_in_flight(identity) is False  # released with the call


def test_inflight_execution_is_counted_and_released_on_every_exit() -> None:
    """The registry counts rather than flags, and comes back down on every exit.

    COUNTED because two workflows can call the same tool at once and the first to
    finish must not cancel the second one's protection. RELEASED in a ``finally``
    because a leaked entry would make every future promote defer that package's
    backup forever -- so a handler that raises, and an AI request cancelled
    mid-call (the shape asyncio uses on a timeout), must both come back out. The
    key is REMOVED at zero, so a stale count can never read as "still in use"."""
    identity = (1, 2)
    with tools._inflight_execution(identity):
        with tools._inflight_execution(identity):
            assert tools.directory_execution_in_flight(identity) is True
        # the outer call still holds it
        assert tools.directory_execution_in_flight(identity) is True
    assert tools.directory_execution_in_flight(identity) is False

    for exc in (RuntimeError, asyncio.CancelledError):
        with pytest.raises(exc), tools._inflight_execution(identity):
            raise exc()
        assert tools.directory_execution_in_flight(identity) is False
    assert tools._INFLIGHT_EXECUTIONS == {}  # nothing left behind, not even a zero


def test_a_call_holds_the_dotenv_it_was_given_until_its_output_is_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The values a child RECEIVED stay maskable for the whole call, whatever the
    file says by the time it finishes.

    ``_build_tool_env`` copies the package's ``.env`` into the child's environment,
    but the masking of that child's output happens when it EXITS -- and the
    redactor reads whatever is on disk AT THAT MOMENT. Rotate the credential while
    a long call is in flight and the child's echo of the OLD one would sail past a
    redactor that has only ever heard of the new one, into the role:"tool" message,
    the in-memory log and the JSONL sink.

    The rotation is measured, not assumed: the scan's own view of the package
    (``_cached_env_values``) is asserted to have already forgotten the old value at
    the instant ``known_secret_values`` still reports it -- so the hold, and
    nothing else, is what covers the gap."""
    root = tmp_path / "tools"
    old, new = "old-secret-abcdef", "new-secret-ghijkl"
    marker, gate = tmp_path / "started", tmp_path / "go"
    pkg = _make_tool(
        root,
        "rotating",
        "import os, sys, time\n"
        f"open({str(marker)!r}, 'w').write('x')\n"
        f"while not os.path.exists({str(gate)!r}):\n"
        "    time.sleep(0.01)\n"
        "sys.stdout.write('SECRET=' + os.environ['KB_KEY'])\n",
        dotenv=f"KB_KEY={old}\n",
    )
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler

    result: dict[str, str] = {}
    caller = threading.Thread(target=lambda: result.update(out=asyncio.run(handler({}))))
    caller.start()
    try:
        _wait_for(marker.exists)  # the CHILD is running with the old value in its env
        assert old in tools.known_secret_values()
        (_package_path(pkg) / ".env").write_text(
            f"KB_KEY={new}\n", encoding="utf-8"
        )  # rotated mid-call
        assert tools._cached_env_values(_package_root(pkg)) == frozenset({new})
        assert old in tools.known_secret_values()  # ... the call's own hold did not
    finally:
        gate.write_text("go", encoding="utf-8")
        caller.join(timeout=30)

    assert result["out"] == f"SECRET={tools._REDACTION_MARKER}"  # masked on the way out
    assert tools._INFLIGHT_SECRETS == {}  # released with the call, not left behind
    assert old not in tools.known_secret_values()


def test_two_overlapping_calls_keep_a_shared_dotenv_value_maskable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two calls holding the SAME value: the first to finish must not strip the
    protection the second still needs.

    Reachable rather than exotic -- two AI workflows can call one tool at once,
    and they read the one ``.env`` -- which is why the registry counts holds
    instead of flagging values. The ``.env`` is removed once both children are
    running, so from that instant the registry is the ONLY thing that can mask
    either child's output; a set-shaped registry would leave the survivor's echo
    in the clear."""
    root = tmp_path / "tools"
    shared = "shared-secret-abcdef"
    gate_a, gate_b = tmp_path / "a", tmp_path / "b"
    pkg = _make_tool(
        root,
        "shared",
        "import json, os, sys, time\n"
        "gate = json.loads(sys.stdin.read())['gate']\n"
        "open(gate + '.started', 'w').write('x')\n"
        "while not os.path.exists(gate):\n"
        "    time.sleep(0.01)\n"
        "sys.stdout.write('SECRET=' + os.environ['KB_KEY'])\n",
        dotenv=f"KB_KEY={shared}\n",
    )
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler

    out: dict[str, str] = {}

    def _call(key: str, gate: Path) -> threading.Thread:
        thread = threading.Thread(
            target=lambda: out.__setitem__(key, asyncio.run(handler({"gate": str(gate)})))
        )
        thread.start()
        return thread

    first, second = _call("a", gate_a), _call("b", gate_b)
    try:
        _wait_for(lambda: all(Path(f"{g}.started").exists() for g in (gate_a, gate_b)))
        (_package_path(pkg) / ".env").unlink()  # from here only the holds can answer for this value
        assert shared in tools.known_secret_values()
        gate_a.write_text("go", encoding="utf-8")
        first.join(timeout=30)
        assert not first.is_alive()
        assert shared in tools.known_secret_values()  # one hold released, one remains
    finally:
        gate_b.write_text("go", encoding="utf-8")
        second.join(timeout=30)

    assert out["a"] == out["b"] == f"SECRET={tools._REDACTION_MARKER}"
    assert tools._INFLIGHT_SECRETS == {}  # ... and the last release cleared the key
    assert shared not in tools.known_secret_values()


def test_inflight_secrets_are_counted_and_released_on_every_exit() -> None:
    """The secret registry's own contract, at the unit the holders share.

    COUNTED so overlapping holders (two calls to one tool, a revise session and a
    call reading the same ``.env``) each release only their own hold. RELEASED in
    a ``finally`` because a leaked hold masks a value FOREVER -- a failure that
    shows up as a redactor eating ordinary prose, long after the call that leaked
    it. The key is REMOVED at zero so ``known_secret_values`` can read the mapping
    as a plain set."""
    value = "held-secret-abcdef"
    with tools._inflight_secrets(frozenset({value})):
        with tools._inflight_secrets(frozenset({value})):
            assert value in tools.known_secret_values()
        assert value in tools.known_secret_values()  # the outer hold still stands
    assert tools._INFLIGHT_SECRETS == {}

    for exc in (RuntimeError, asyncio.CancelledError):
        with pytest.raises(exc), tools._inflight_secrets(frozenset({value})):
            raise exc()
        assert tools._INFLIGHT_SECRETS == {}  # not even a zero, on either shape

    # Releasing a value nobody holds is a no-op, never a negative count -- the
    # forgiveness the ``set.discard`` this replaced used to give for free.
    tools.discard_inflight_secret(value)
    assert tools._INFLIGHT_SECRETS == {}


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

    env, _ = tools._build_tool_env(_package_root(pkg))
    assert env["LEAK"] == "${OPENAI_API_KEY}"  # literal, not the resolved parent key
    assert "sk-secret-should-not-leak" not in env.values()


def test_build_tool_env_passes_through_tls_no_verify_when_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TLS_NO_VERIFY=1 is injected into the from-scratch child env when the
    settings flag is on, so an installed tool MAY skip TLS verification too."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "envtool", "import sys\nsys.stdout.write('ok')\n")
    _install_tools(monkeypatch, root, tls_no_verify=True)

    env, _ = tools._build_tool_env(_package_root(pkg))
    assert env["TLS_NO_VERIFY"] == "1"


def test_build_tool_env_omits_tls_no_verify_when_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default (flag off): TLS_NO_VERIFY is absent from the child env entirely --
    not "0", simply not a key -- matching the passthrough allowlist's own
    "absent means not set" convention."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "envtool", "import sys\nsys.stdout.write('ok')\n")
    _install_tools(monkeypatch, root)  # tls_no_verify defaults False

    env, _ = tools._build_tool_env(_package_root(pkg))
    assert "TLS_NO_VERIFY" not in env


def test_build_tool_env_tool_dotenv_can_override_tls_no_verify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tool's own .env is layered ON TOP of the settings-derived injection
    (same override convention as PATH/HOME/...), so a tool that explicitly wants
    verification back on for its own calls can still set that in its .env."""
    root = tmp_path / "tools"
    pkg = _make_tool(
        root,
        "envtool",
        "import sys\nsys.stdout.write('ok')\n",
        dotenv="TLS_NO_VERIFY=0\n",
    )
    _install_tools(monkeypatch, root, tls_no_verify=True)

    env, _ = tools._build_tool_env(_package_root(pkg))
    assert env["TLS_NO_VERIFY"] == "0"  # the tool's own .env wins


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
    env, _ = tools._build_tool_env(_package_root(pkg))
    advertised = tools.resolve_current(_package_root(pkg))
    assert isinstance(advertised, tools.Resolved)

    before = threading.active_count()
    try:
        started = time.monotonic()
        # Drive the blocking runner DIRECTLY (not via the threadpool handler) so
        # the reader/writer threads run in this thread's context and
        # active_count() is a clean before/after measure with no threadpool-worker
        # confound.
        result = tools._run_tool_subprocess(
            entry,
            advertised,
            tools.package_identity(_version_root(pkg)),
            tools._advertisement_generation(advertised.version_root),
            env,
            "{}",
            30.0,
            1000,
        )
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
    env, _ = tools._build_tool_env(_package_root(pkg))
    advertised = tools.resolve_current(_package_root(pkg))
    assert isinstance(advertised, tools.Resolved)

    try:
        started = time.monotonic()
        result = tools._run_tool_subprocess(
            entry,
            advertised,
            tools.package_identity(_version_root(pkg)),
            tools._advertisement_generation(advertised.version_root),
            env,
            "{}",
            30.0,
            1000,
        )
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
    os.mkfifo(
        _package_path(pkg) / ".env"
    )  # a writer-less FIFO -- an ordinary read would block forever
    _install_tools(monkeypatch, root)

    box: dict[str, dict[str, str]] = {}
    worker = threading.Thread(
        target=lambda: box.__setitem__("env", tools._build_tool_env(_package_root(pkg))[0]),
        daemon=True,
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
    pkg = _staged_pkg(
        tmp_path,
        tool_json={
            "name": "big",
            "description": "test tool",
            "parameters": {"type": "object", "properties": {}},
            "entry": [sys.executable, "run.py"],
        },
        run_py="import sys\nsys.stdout.write('x')\n",
    )
    (pkg / ".env").write_text("K=" + "y" * tools._ENV_FILE_MAX_BYTES + "\n", encoding="utf-8")

    # Build validation owns build-local dotenv policy; installed scans read the
    # package-layer dotenv separately.
    error = tools.validate_tool_content(tools.BuildRoot(pkg), "big")
    assert error is not None
    assert "too large" in error
    # With the .env removed the same package validates clean -- pinning that it
    # was the .env size, not some other defect, that failed it.
    (pkg / ".env").unlink()
    assert tools.validate_tool_content(tools.BuildRoot(pkg), "big") is None


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


def test_scan_split_uses_one_content_rules_body_with_distinct_root_types(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build = _staged_pkg(
        tmp_path / "session",
        tool_json=_kbsearch_manifest(),
        run_py="import sys\n",
    )
    installed = _make_tool(tmp_path / "tools", "echo", "import sys\n")
    real_scan = tools._scan_tool_content
    roots: list[tools.BuildRoot | tools.VersionRoot] = []

    def watched(
        root: tools.BuildRoot | tools.VersionRoot, expected_name: str
    ) -> tools._ContentScan:
        roots.append(root)
        return real_scan(root, expected_name)

    monkeypatch.setattr(tools, "_scan_tool_content", watched)

    assert tools.validate_tool_content(tools.BuildRoot(build), "staged") is None
    assert tools.scan_installed(_package_root(installed)).valid is True
    assert isinstance(roots[0], tools.BuildRoot)
    assert isinstance(roots[1], tools.VersionRoot)
    assert not (build / tools._META_DIRNAME).exists()


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
    error = tools.validate_tool_content(tools.BuildRoot(pkg), "staged")
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
    error = tools.validate_tool_content(tools.BuildRoot(pkg), "staged")
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
    assert tools.validate_tool_content(tools.BuildRoot(pkg), "staged") is None


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
    error = tools.validate_tool_content(tools.BuildRoot(pkg), "staged")
    assert error is not None
    assert "不得包含秘密值" in error
    assert secret not in error  # neither content nor filename echoes the value
    assert tools._REDACTION_MARKER in error  # the offending path is named, masked


# --- known-secret redaction (F1 / F4, D36) ---------------------------------


def test_known_secret_values_sees_a_same_mtime_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``.env`` whose CONTENT changed without its mtime advancing must invalidate
    the redactor's per-package cache.

    The tag used to be the ``st_mtime_ns`` alone, and mtime is the one timestamp
    userspace can set to anything: a timestamp-preserving restore (``cp -p``, a
    backup rollout, ``shutil.copy2`` -- which is exactly what the revise flow uses
    to put a ``.env`` back) leaves it untouched. The cache then kept serving the
    OLD value, so the tool emitted the NEW secret and nothing masked it -- not the
    live tool-result redactor, not llm_log, not the summary writer.

    Driven the way the hazard actually arrives: write, populate the cache, rewrite
    with different content, then force the ORIGINAL stamps back with ``os.utime``.
    ``st_ctime_ns`` is what catches this one (no syscall can set it backwards),
    which is why the tag is a file identity rather than a timestamp."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "kb", "import sys\n", dotenv="KB_API_KEY=first-secret-abcdef\n")
    _install_tools(monkeypatch, root)
    tools._ENV_VALUE_CACHE.clear()

    assert "first-secret-abcdef" in tools.known_secret_values()  # cache populated

    env_file = _package_path(pkg) / ".env"
    before = env_file.stat()
    env_file.write_text("KB_API_KEY=second-secret-abcdef\n", encoding="utf-8")
    os.utime(env_file, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert env_file.stat().st_mtime_ns == before.st_mtime_ns  # the premise of the test

    values = tools.known_secret_values()
    assert "second-secret-abcdef" in values  # the value that would leak today
    assert "first-secret-abcdef" not in values


def test_known_secret_values_still_caches_an_unchanged_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """... and it is still a CACHE, not a no-op that re-reads every time.

    The provider runs once per stored log body -- every request message and every
    response of every attempt -- so a busy installer session would otherwise
    re-read every installed tool's ``.env`` from disk dozens of times per round.
    Widening the tag must not cost that: an untouched file has the identical
    identity, so the second call parses nothing."""
    root = tmp_path / "tools"
    _make_tool(root, "kb", "import sys\n", dotenv="KB_API_KEY=cached-secret-abcdef\n")
    _install_tools(monkeypatch, root)
    tools._ENV_VALUE_CACHE.clear()

    parses: list[Path] = []
    real_loader = tools._load_tool_dotenv

    def counting_loader(directory: tools.PackageRoot) -> dict[str, str]:
        parses.append(directory.path)
        return real_loader(directory)

    monkeypatch.setattr(tools, "_load_tool_dotenv", counting_loader)

    assert "cached-secret-abcdef" in tools.known_secret_values()
    assert len(parses) == 1  # the miss
    assert "cached-secret-abcdef" in tools.known_secret_values()
    assert len(parses) == 1  # ... and the hit, with no second read


def test_redaction_marker_matches_llm_log() -> None:
    """tools._REDACTION_MARKER and llm_log._REDACTION_MARKER MUST be byte-identical:
    a secret masked in a live tool result and one masked in the AI 日誌 have to be
    indistinguishable. They are deliberately duplicated (llm_log stays a leaf
    observability module -- see the note on tools._REDACTION_MARKER), so this pins
    them equal against silent drift."""
    assert tools._REDACTION_MARKER == llm_log._REDACTION_MARKER


def test_max_mask_ranges_matches_llm_log() -> None:
    """tools._MAX_MASK_RANGES and llm_log._MAX_MASK_RANGES MUST be the same value
    (D36 round-5): ``_mask_known_secrets`` looks the cap up BY NAME from its own
    module's globals, so even byte-for-byte identical function source would still
    behave differently under a pathological input if the two modules disagreed on
    where the cap kicks in."""
    assert tools._MAX_MASK_RANGES == llm_log._MAX_MASK_RANGES


def test_mask_known_secrets_source_identical_in_both_modules() -> None:
    """tools._mask_known_secrets and llm_log._mask_known_secrets MUST be byte-for-byte
    identical (see either docstring's LOCKSTEP note). Mirrors
    test_redaction_marker_matches_llm_log's marker-only pin, but for the FULL function
    body, so a future edit applied to one copy and not mirrored to the other fails
    loudly here instead of silently drifting the two redactors apart."""
    assert inspect.getsource(tools._mask_known_secrets) == inspect.getsource(
        llm_log._mask_known_secrets
    )


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


def test_redact_known_secrets_ranges_computed_on_original_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1/H1: mask ranges are computed on the PRISTINE text, so the full-value pass can
    never destroy the evidence the trailing-fragment guard needs. With a LONG secret and a
    SHORT secret that is an interior substring of the long secret's truncated prefix, text
    ending in a 12-char prefix of the LONG secret must not leak a 6+-char run: the old
    replace-first order masked the ``GHIJKL`` occurrence and blinded the guard, stranding
    ``ABCDEF``."""
    monkeypatch.setattr(
        tools,
        "known_secret_values",
        lambda: frozenset({"ABCDEFGHIJKLmnop", "GHIJKL"}),
    )
    out = tools.redact_known_secrets("tail=ABCDEFGHIJKL")  # 12-char prefix of the long secret
    assert "ABCDEF" not in out  # no 6+-char run of the long secret's chars survives
    assert tools._REDACTION_MARKER in out
    assert out == "tail=" + tools._REDACTION_MARKER


def test_redact_known_secrets_overlapping_occurrences_merge_one_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1/H1: two DISTINCT secrets whose occurrences OVERLAP in the text collapse into a
    single merged range -- one marker, with no secret bytes leaking between them."""
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({"abcXYZ", "XYZdef"}))
    out = tools.redact_known_secrets("p abcXYZdef q")  # abcXYZ (0-6) overlaps XYZdef (3-9)
    assert "abcXYZ" not in out
    assert "XYZdef" not in out
    assert out.count(tools._REDACTION_MARKER) == 1
    assert out == "p " + tools._REDACTION_MARKER + " q"


def test_redact_known_secrets_degenerate_repeated_secret_completes_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D36 round-5: a legitimate 6-char secret entirely composed of one repeated
    character, matched against a large same-char document (an un-truncated OpenAPI
    body that happens to look like this is exactly the reported memory-amplification
    vector: the pre-fix find-loop stored one range per OVERLAPPING occurrence), must
    not blow up. The stepping find-cursor collapses the run into a handful of merged
    ranges, so this completes in well under a second, and -- bar a residual strictly
    shorter than the secret at the very tail, itself under the redaction floor -- the
    secret's repeated character never survives as a 6+ run anywhere in the output."""
    secret = "aaaaaa"
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))
    text = "a" * 100_000
    start = time.perf_counter()
    out = tools.redact_known_secrets(text)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0
    assert secret not in out  # no 6+ run of "a" survives anywhere in the output


def test_redact_known_secrets_max_ranges_cap_replaces_whole_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D36 round-5: once collecting mask ranges would exceed _MAX_MASK_RANGES, the
    function stops collecting immediately and gives up precision entirely -- the
    WHOLE text is replaced by a single marker rather than merging a huge range list.
    A separator between occurrences keeps the matches from touching/merging into one
    contiguous range on their own, so a bare single marker here can ONLY come from the
    cap firing, never from ordinary adjacent-range merging (over-redaction is always
    safe, which is what makes giving up precision like this a safe fail-safe)."""
    secret = "abcdef"
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))
    text = (secret + "|") * (tools._MAX_MASK_RANGES + 1)
    out = tools.redact_known_secrets(text)
    assert out == tools._REDACTION_MARKER


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


def test_a_toggle_leaves_the_manifest_byte_identical_and_its_identity_unmoved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """THE property web-v5 P1 exists for, measured rather than asserted.

    ``enabled`` used to live in ``tool.json``, so flipping a switch rewrote the
    manifest and MOVED its ``package_identity`` -- which is this subsystem's answer
    to "is the package at this path still the one I looked at?". Four separately
    reviewed defects came out of that single fact (overall-r5 O5-1, r7 O7-2, r8
    O8-1, and 裁決紀錄 #8): a toggle looked exactly like a revise or a reinstall to
    every guard that consults it.

    Everything else in this phase follows from the two assertions below. They are
    checked across BOTH directions and a repeat of the same direction, because the
    old behaviour moved the ctime on every write regardless of what it wrote."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n", enabled=True)
    _install_tools(monkeypatch, root)
    manifest = pkg / "tool.json"
    before_bytes = manifest.read_bytes()
    before_identity = tools.package_identity(_version_root(pkg))
    assert before_identity is not None

    for value in (False, False, True):
        assert set_enabled("echo", value) is True
        assert manifest.read_bytes() == before_bytes
        assert tools.package_identity(_version_root(pkg)) == before_identity

    # ... and the toggle really did take effect, so this is not a no-op passing by
    # doing nothing at all.
    assert list_tools()[0]["enabled"] is True
    assert set_enabled("echo", False) is True
    assert list_tools()[0]["enabled"] is False
    assert manifest.read_bytes() == before_bytes
    assert tools.package_identity(_version_root(pkg)) == before_identity


def test_the_state_file_wins_over_a_manifest_that_disagrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R1's precedence: a state file of OURS, readable, is AUTHORITATIVE.

    The manifest's ``enabled`` key survives on disk deliberately -- rewriting a
    manifest to tidy away a legacy field would move the very identity this split
    exists to hold still -- so the two files can and will disagree. The state file
    is the answer, in both directions.

    "Of ours" is carried by the ownership MARKER (P1R5-1) and the fixture writes it,
    so this is the unchanged behaviour of a recognized file. The same bytes WITHOUT
    the marker lose every one of these assertions -- that is the sibling test,
    ``test_a_foreign_file_at_the_state_files_name_is_answered_as_absent``."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n", enabled=True)
    _install_tools(monkeypatch, root)

    _write_state_file(pkg, False)
    assert list_tools()[0]["enabled"] is False
    assert enabled_llm_tools() == []

    _write_state_file(pkg, True)
    # The manifest now says the opposite of the state file in the other direction.
    manifest = pkg / "tool.json"
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["enabled"] = False
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    assert list_tools()[0]["enabled"] is True
    assert len(enabled_llm_tools()) == 1


def test_invariant_c_a_package_with_no_state_file_is_disabled_without_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent state is disabled; reads do not synthesize package metadata."""
    root = tmp_path / "tools"
    off = _make_tool(root, "off", "import sys\nsys.stdout.write('x')\n", enabled=False)
    on = _make_tool(root, "on", "import sys\nsys.stdout.write('x')\n", enabled=True)
    _install_tools(monkeypatch, root)

    _state_path(off).unlink()
    _state_path(on).unlink()
    manifest_before = (off / "tool.json").read_bytes()

    listed = {t["name"]: t for t in list_tools()}
    assert listed["off"]["enabled"] is False
    assert listed["on"]["enabled"] is False
    assert enabled_llm_tools() == []
    assert not _state_path(off).exists()
    assert not _state_path(on).exists()

    # An explicit toggle may create our missing package-layer state file.
    assert set_enabled("off", True) is True
    assert json.loads(_state_path(off).read_text(encoding="utf-8")) == _state_document(True)
    assert (off / "tool.json").read_bytes() == manifest_before  # legacy key left in place
    assert {t["name"]: t["enabled"] for t in list_tools()}["off"] is True


@pytest.mark.parametrize(
    "content",
    [
        "x" * (tools._STATE_MAX_BYTES + 1),  # past the cap: nothing to look for a marker in
        json.dumps({tools._STATE_MARKER_KEY: tools._STATE_MARKER_VALUE}),  # ours, no answer
        json.dumps({tools._STATE_MARKER_KEY: tools._STATE_MARKER_VALUE, "enabled": "yes"}),
        json.dumps({tools._STATE_MARKER_KEY: tools._STATE_MARKER_VALUE, "enabled": 1}),
    ],
)
def test_an_unreadable_state_file_disables_rather_than_defaulting_to_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str
) -> None:
    """R2: ABSENT and UNREADABLE are different questions, answered differently.

    Absent is the ordinary pre-migration case and falls back to the manifest. OUR
    file failing to say anything usable means the operator's intent is unknown --
    and defaulting that to ``enabled: True`` would hand the model a tool somebody
    deliberately switched off, which is the one direction this subsystem never errs
    in.

    The cases are the ones that are still OURS after P1R5-1 narrowed this set: a
    file we could not read at all (so there is no marker to look for), and a
    marker-bearing document whose ``enabled`` is missing or not a bool. A document
    we CAN read that carries no marker is a different question with a different
    answer -- see
    ``test_a_foreign_file_at_the_state_files_name_is_answered_as_absent``.

    Both halves of the answer are pinned: the row is INVALID with the listing's own
    ``error`` channel carrying why (so the operator is told, rather than watching a
    tool silently vanish from the model's reach), AND ``enabled`` is False -- so
    ``enabled_llm_tools``' ``valid AND enabled`` filter refuses on either half
    alone. The manifest here says ``enabled: true``, so a fallback would have
    advertised it."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n", enabled=True)
    _state_path(pkg).write_text(content, encoding="utf-8")
    _install_tools(monkeypatch, root)

    listed = list_tools()[0]
    assert listed["valid"] is False
    assert listed["enabled"] is False
    assert listed["error"] == tools._STATE_UNREADABLE_ERROR
    assert enabled_llm_tools() == []

    # Repairable through the API without a delete: an unreadable file is read as
    # OURS, so the toggle publishes a clean one straight over it. (A FOREIGN one is
    # the opposite -- refused rather than overwritten; see the test named above.)
    assert set_enabled("echo", True) is True
    repaired = list_tools()[0]
    assert repaired["valid"] is True and repaired["enabled"] is True


def test_a_state_file_that_cannot_be_looked_at_is_not_read_as_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failure to LOOK is not evidence of absence (the D40 P3b r2-3 rule).

    ``_read_regular_file_capped`` folds "no such file" and "refused to read it"
    into one None, so the reader takes its own ``lstat`` first: only
    ``FileNotFoundError`` means absent. A DIRECTORY at the name is the shape that
    proves the distinction is real -- it exists, nothing can parse it, and reading
    it as "there is no state file" would fall back to the manifest's ``true``."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n", enabled=True)
    _state_path(pkg).unlink()
    _state_path(pkg).mkdir()
    _install_tools(monkeypatch, root)

    state = tools._read_enabled_state(_package_root(pkg))
    assert state.ours is True  # NOT absent, and read as ours-and-broken
    assert state.enabled is False and state.error is not None
    assert state.notice is None  # ... which is not the same answer a FOREIGN file gets
    assert enabled_llm_tools() == []


@pytest.mark.parametrize(
    "content",
    [
        '{"cursor": 41}',  # the tool's own JSON settings
        '{"enabled": false}',  # ... or a hand-written one that omitted the marker
        '{"afterthread": "something-else", "enabled": false}',  # marker, wrong value
        "cursor=41\n",  # a plain-text cursor: not JSON at all
        "",  # empty
        "[]",  # legal JSON, not an object
    ],
)
def test_a_foreign_file_at_the_state_files_name_is_answered_as_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str
) -> None:
    """P1R5-1: the directory is the PACKAGE's, so a file at our name may not be ours.

    ``.state.json`` was not a reserved name before web-v5 P1, so a tool could
    perfectly well already own one -- a cursor, a cache, its own settings. Reading
    any file at that path as the operator's toggle produced three silent harms at
    once: a package whose own file happened to say ``enabled: true`` was
    re-advertised and EXECUTED with nobody having touched the switch; one whose file
    had no boolean ``enabled`` was listed INVALID and stopped working; and the first
    publish over it destroyed whatever the tool kept there.

    The name now declares whose file it is, which makes the collision implausible.
    The MARKER is what makes it detectable, and that is the half that closes it: an
    operator or a future tool can still create a file under any name we pick. A
    document without the marker is answered exactly as a MISSING one is -- the
    manifest decides -- and the file is neither read for a value, nor overwritten,
    nor deleted.

    Pinned here: the answer comes from the manifest (both directions), the package
    stays VALID and runnable, the file is byte-identical after a scan, a listing and
    an execution, and the listing carries something the operator can act on."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('ok')\n", enabled=True)
    _state_path(pkg).write_text(content, encoding="utf-8")
    before = _state_path(pkg).read_bytes()
    _install_tools(monkeypatch, root)

    assert tools.package_enabled(_package_root(pkg)) is False
    listed = list_tools()[0]
    assert listed["valid"] is True  # a foreign file does not break a working tool
    assert listed["enabled"] is False
    assert listed["error"] == tools._STATE_FOREIGN_NOTICE  # ... and says so, actionably
    assert enabled_llm_tools() == []

    # Legacy manifest state has no effect in either direction.
    manifest = pkg / "tool.json"
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["enabled"] = False
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    assert tools.package_enabled(_package_root(pkg)) is False
    assert list_tools()[0]["enabled"] is False
    assert enabled_llm_tools() == []

    assert _state_path(pkg).read_bytes() == before  # nothing touched it


def test_set_enabled_refuses_rather_than_destroying_a_foreign_state_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1R5-1's write side: the ONE writer of that name will not write over a
    file that is not ours.

    ``set_enabled`` is the only thing that publishes there, so "we never overwrite
    somebody else's file" is true only if it refuses. It answers False -- a 404 from
    the route -- rather than succeeding silently: the read side would go on answering
    from the manifest, so a "success" would be a toggle that provably did not take
    effect. The refusal is also where the operator MEETS the problem, which is why
    the listing's note names both repairs.

    The contrast is the point of the last block: the moment the operator moves their
    file out of the way, the same call behaves exactly as it always has."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n", enabled=True)
    theirs = b'{"cursor": 41}\n'
    _state_path(pkg).write_bytes(theirs)
    _install_tools(monkeypatch, root)

    assert set_enabled("echo", False) is False  # "did not happen" -> 404
    assert _state_path(pkg).read_bytes() == theirs
    assert list_tools()[0]["enabled"] is False  # ... and it really did not happen
    # No temp file left behind either: the refusal is before the publish, not inside it.
    assert not list(pkg.glob(f"{tools._STATE_FILENAME}.*"))

    _state_path(pkg).unlink()  # the operator moves their file aside
    assert set_enabled("echo", False) is True
    assert json.loads(_state_path(pkg).read_text(encoding="utf-8")) == _state_document(False)
    assert list_tools()[0]["enabled"] is False


def test_delete_tool_removes_package(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With nothing executing against it, a delete still DESTROYS the package on
    the spot -- the deferral below is the exception, not the new normal, and it
    leaves no hidden remains for a sweep to find."""
    root = tmp_path / "tools"
    _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)

    assert delete_tool("echo") is True
    assert list_tools() == []
    assert list(root.iterdir()) == []  # destroyed by the time it returned, not left marked
    assert delete_tool("echo") is False  # already gone


def test_delete_during_an_execution_defers_the_removal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A delete landing mid-call must not pull the files out from under the child.

    The same MEASURED asymmetry the replace-mode promote turns on (see
    ``tools._INFLIGHT_EXECUTIONS``): a running child's cwd is a reference to the
    INODE, so renaming the package aside disturbs nothing, while the ``rmtree``
    makes every later relative open ENOENT. The child here opens ``data.txt`` by
    relative path only AFTER the delete has returned, so a destroyed package
    would come back as a tool FAILURE -- handed to the model as the answer to an
    action whose external side effect may already have happened, and which it may
    then retry.

    Nothing the caller can observe changes: the delete reports success and the
    tool is gone from every registry path AT ONCE. Its ``.env`` goes with it --
    which is exactly why the call's own hold on those values is what still masks
    the child's echo of one."""
    root = tmp_path / "tools"
    secret = "kb-secret-abcdef"
    marker, gate = tmp_path / "started", tmp_path / "go"
    pkg = _make_tool(
        root,
        "busy",
        "import os, sys, time\n"
        f"open({str(marker)!r}, 'w').write('x')\n"
        f"while not os.path.exists({str(gate)!r}):\n"
        "    time.sleep(0.01)\n"
        "sys.stdout.write(open('data.txt').read() + ':' + os.environ['KB_KEY'])\n",
        dotenv=f"KB_KEY={secret}\n",
    )
    (pkg / "data.txt").write_text("PAYLOAD", encoding="utf-8")
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler

    result: dict[str, str] = {}
    caller = threading.Thread(target=lambda: result.update(out=asyncio.run(handler({}))))
    caller.start()
    try:
        _wait_for(marker.exists)  # the CHILD is running, not merely queued
        assert delete_tool("busy") is True  # the route still reports success (204)
        assert list_tools() == []  # ... and it is gone from the registry at once
        assert enabled_llm_tools() == []
        assert not pkg.exists()  # the NAME is free again
        assert tools._cached_env_values(_package_root(pkg)) == frozenset()
        assert secret in tools.known_secret_values()  # only the call's hold answers now
        # The rename runs BEFORE the registry is consulted, which is what makes the
        # set of executions that can exist against this directory closed: an
        # already-advertised handler entered afterwards can only refuse, because
        # the identity of the now-absent path is None.
        assert asyncio.run(handler({})) == tools._TOOL_REPLACED_RESULT
    finally:
        gate.write_text("go", encoding="utf-8")
        caller.join(timeout=30)

    # The child read a package file AFTER the delete, and its echoed secret was
    # still masked even though the ``.env`` behind it is no longer scannable.
    assert result["out"] == f"PAYLOAD:{tools._REDACTION_MARKER}"
    remains = [child.name for child in root.iterdir()]
    assert len(remains) == 1 and tools._STALE_BACKUP_RE.match(remains[0])
    assert secret not in tools.known_secret_values()  # the hold ended with the call


def test_delete_after_a_manifest_edit_still_defers_a_running_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An in-place manifest rewrite mid-call must not make a running child invisible.

    O5-1's guarantee, kept under test with the writer that still reaches it. The
    registry used to be keyed on the MANIFEST identity -- so a handler that
    registered before that file was rewritten was looked up afterwards under a
    tuple it had never registered, the delete concluded nothing was running, and
    ``rmtree`` took the files out from under a live subprocess. That split is not
    the syscall-pair instant this subsystem accepts elsewhere: it lasts from the
    rewrite until the child exits.

    ``set_enabled`` was the writer O5-1 found it through, and since web-v5 P1 the
    toggle does not touch ``tool.json`` at all (its own sibling test pins that a
    toggle now disturbs nothing here). An operator HAND-EDITING the spec of a tool
    that is running is a supported action (D21) and moves the same identity the
    same way, so the guarantee still needs this test.

    Keyed on the DIRECTORY, both sides agree again -- an edit to a file inside a
    directory changes nothing about the directory's own ``(st_dev, st_ino)``, and
    a rename carries it (both measured; see ``tools.directory_identity``). The
    child reads its data file by relative path only AFTER the delete has returned,
    so a destroyed package would come back as a tool FAILURE rather than its
    payload. The collection still happens, one sweep later."""
    root = tmp_path / "tools"
    marker, gate = tmp_path / "started", tmp_path / "go"
    pkg = _make_tool(
        root,
        "toggled",
        "import os, sys, time\n"
        f"open({str(marker)!r}, 'w').write('x')\n"
        f"while not os.path.exists({str(gate)!r}):\n"
        "    time.sleep(0.01)\n"
        "sys.stdout.write(open('data.txt').read())\n",
    )
    (pkg / "data.txt").write_text("PAYLOAD", encoding="utf-8")
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler
    before = tools.package_identity(_version_root(pkg))

    result: dict[str, str] = {}
    caller = threading.Thread(target=lambda: result.update(out=asyncio.run(handler({}))))
    caller.start()
    try:
        _wait_for(marker.exists)  # the CHILD is running, not merely queued
        # The premise, measured in place rather than assumed: the rewrite DID move
        # the manifest identity (so a manifest-keyed lookup would miss) while the
        # directory identity the call registered under is unchanged.
        assert _edit_manifest_in_place(pkg) != before
        assert delete_tool("toggled") is True
    finally:
        gate.write_text("go", encoding="utf-8")
        caller.join(timeout=30)

    assert result["out"] == "PAYLOAD"  # the files survived the delete, as intended
    remains = [child for child in root.iterdir()]
    assert len(remains) == 1 and tools._STALE_BACKUP_RE.match(remains[0].name)
    assert (_resolved_version(remains[0]) / "data.txt").exists()  # deferred, not destroyed


def test_a_toggle_mid_call_moves_neither_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other side of the test above: a toggle mid-call now disturbs NOTHING.

    O5-1's failure needed a toggle to move the manifest identity under a running
    child. After web-v5 P1 it moves neither identity, so the delete's deferral
    question and the handler's registration are answering about the same package
    they were before -- the finding's precondition is gone rather than handled.

    The child is deliberately left running across the toggle, which is exactly the
    state that used to strand the registration."""
    root = tmp_path / "tools"
    marker, gate = tmp_path / "started", tmp_path / "go"
    pkg = _make_tool(
        root,
        "toggled",
        "import os, sys, time\n"
        f"open({str(marker)!r}, 'w').write('x')\n"
        f"while not os.path.exists({str(gate)!r}):\n"
        "    time.sleep(0.01)\n"
        "sys.stdout.write(open('data.txt').read())\n",
    )
    (pkg / "data.txt").write_text("PAYLOAD", encoding="utf-8")
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler
    manifest_before = tools.package_identity(_version_root(pkg))
    directory_before = tools.directory_identity(_version_root(pkg))
    manifest_bytes = (pkg / "tool.json").read_bytes()

    result: dict[str, str] = {}
    caller = threading.Thread(target=lambda: result.update(out=asyncio.run(handler({}))))
    caller.start()
    try:
        _wait_for(marker.exists)  # the CHILD is running, not merely queued
        assert set_enabled("toggled", False) is True
        assert tools.package_identity(_version_root(pkg)) == manifest_before
        assert tools.directory_identity(_version_root(pkg)) == directory_before
        assert (pkg / "tool.json").read_bytes() == manifest_bytes
        assert delete_tool("toggled") is True
    finally:
        gate.write_text("go", encoding="utf-8")
        caller.join(timeout=30)

    assert result["out"] == "PAYLOAD"  # the running child was still protected


def test_a_delete_landing_while_a_call_prepares_is_registered_for_and_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The window R6-1 is about: after the handler read the directory identity and
    BEFORE the child starts.

    Two things had to be true for a call to be safe there, and neither was:

    * the execution had to be REGISTERED before anything else, so a delete landing
      in the gap defers the removal instead of taking it. The registration used to
      come last, after the ``.env`` read and the argument serialization -- so the
      delete saw nothing in flight and destroyed a package this call was about to
      run out of;
    * the "still the same package?" question had to be asked again at the END. The
      handler asked it once, before all that work.

    The delete is driven from inside ``_build_tool_env`` -- literally the ``.env``
    read the finding names -- so it lands in the gap deterministically rather than
    by racing a thread. What must hold: the deferred remains prove the
    registration was already published, and the call refuses rather than starting
    a child against a name that no longer belongs to the package it was offered
    for."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "busy", "import sys\nsys.stdout.write('OLD')\n")
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler
    real_build_env = tools._build_tool_env

    def delete_then_build(
        directory: tools.PackageRoot,
    ) -> tuple[dict[str, str], frozenset[str]]:
        assert delete_tool("busy") is True
        return real_build_env(directory)

    monkeypatch.setattr(tools, "_build_tool_env", delete_then_build)

    assert asyncio.run(handler({})) == tools._TOOL_DISABLED_RESULT

    # The delete DEFERRED: it found this call already registered, which it could
    # only do if the registration preceded the ``.env`` read it was driven from.
    remains = [child for child in root.iterdir()]
    assert len(remains) == 1 and tools._STALE_BACKUP_RE.match(remains[0].name)
    assert (_resolved_version(remains[0]) / "run.py").exists()  # deferred, not destroyed
    assert not _package_path(pkg).exists()  # ... and the NAME went at once, as promised


def test_a_toggle_landing_while_a_call_prepares_still_refuses_before_popen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R4 at the WIDTH the accident used to cover, not just at the handler.

    The window R6-1 named is real for this question too: between the handler's own
    checks and the ``Popen`` sit a ``.env`` read, an argument serialization and a
    threadpool queue wait of unbounded length. Before web-v5 P1 a toggle landing
    anywhere in there rewrote ``tool.json``, so the identity check on the line
    above ``Popen`` caught it. Restoring the handler's check ALONE would have
    quietly narrowed that guarantee -- the tool would start.

    Driven from inside ``_build_tool_env``, the very ``.env`` read that sits in the
    gap, so it lands there deterministically rather than by racing a thread. The
    child writes a sentinel as its first act, so "nothing was started" is a fact on
    disk rather than an inference from the returned string."""
    root = tmp_path / "tools"
    sentinel = tmp_path / "ran"
    _make_tool(
        root,
        "busy",
        f"import sys\nopen({str(sentinel)!r}, 'w').write('x')\nsys.stdout.write('ok')\n",
    )
    _install_tools(monkeypatch, root)
    handler = enabled_llm_tools()[0].handler
    real_build_env = tools._build_tool_env

    def toggle_then_build(
        directory: tools.PackageRoot,
    ) -> tuple[dict[str, str], frozenset[str]]:
        assert set_enabled("busy", False) is True
        return real_build_env(directory)

    monkeypatch.setattr(tools, "_build_tool_env", toggle_then_build)

    assert asyncio.run(handler({})) == tools._TOOL_DISABLED_RESULT
    assert not sentinel.exists()  # no child was ever started


def test_a_state_file_deleted_while_a_call_prepares_still_refuses_before_popen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1R2-1 at the second site, where the partial check was the same partial check.

    The handler's own check passes here -- the state file still says True when it
    runs -- so this pins the line above ``Popen`` and nothing else: it must answer
    the precedence rule too, or a pre-migration package whose manifest disables it
    starts its child after all. Driven from inside ``_build_tool_env``, the ``.env``
    read that sits in the window between the two checks, so the deletion lands there
    deterministically rather than by racing a thread."""
    root = tmp_path / "tools"
    sentinel = tmp_path / "ran"
    pkg = _make_tool(
        root,
        "busy",
        f"import sys\nopen({str(sentinel)!r}, 'w').write('x')\nsys.stdout.write('ok')\n",
        enabled=False,  # pre-migration, and the manifest says off
    )
    _install_tools(monkeypatch, root)
    assert set_enabled("busy", True) is True
    handler = enabled_llm_tools()[0].handler
    real_build_env = tools._build_tool_env

    def delete_state_then_build(
        directory: tools.PackageRoot,
    ) -> tuple[dict[str, str], frozenset[str]]:
        _state_path(pkg).unlink()
        return real_build_env(directory)

    monkeypatch.setattr(tools, "_build_tool_env", delete_state_then_build)

    assert asyncio.run(handler({})) == tools._TOOL_DISABLED_RESULT
    assert not sentinel.exists()  # no child was ever started


def test_deferred_delete_remains_are_invisible_to_every_registry_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """What a deferral leaves behind is inert litter, never a phantom package.

    Pinned directly on the marked name rather than through a live call, because
    the property has to hold for remains that OUTLIVE the process that made them
    (a deferral, then an exit before the sweep). Every path that walks the tools
    directory skips it for the one reason the name is dot-prefixed: the scan
    behind ``list_tools`` / ``enabled_llm_tools``, and the redactor's own
    per-package ``.env`` sweep."""
    root = tmp_path / "tools"
    _make_tool(root, "alive", "import sys\nsys.stdout.write('x')\n", dotenv="A=live-abcdef\n")
    remains = tools._stale_backup_path(root, "ghost", "0123456789abcdef" * 2)
    remains.mkdir()
    (remains / "tool.json").write_text(
        json.dumps(
            {
                "name": "ghost",
                "description": "test tool",
                "parameters": {"type": "object", "properties": {}},
                "entry": [sys.executable, "run.py"],
            }
        ),
        encoding="utf-8",
    )
    (remains / ".env").write_text("A=ghost-secret-abcdef\n", encoding="utf-8")
    _install_tools(monkeypatch, root)

    assert [row["name"] for row in list_tools()] == ["alive"]
    assert [tool.spec["function"]["name"] for tool in enabled_llm_tools()] == ["alive"]
    known = tools.known_secret_values()
    assert "live-abcdef" in known and "ghost-secret-abcdef" not in known
    assert delete_tool("ghost") is False  # not addressable by name either


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
    filter follows the link, so this is the guard that keeps it out (H3).

    The row is also switched OFF (P1R4-2), and that is a decision rather than a
    detail: R2 already adjudicated that a package which cannot answer "may I run?"
    is listed invalid AND disabled, so that no later refactor of either half of
    ``enabled_llm_tools``'s ``valid AND enabled`` filter can put it back in front of
    the model. Refusing to LOOK is exactly such a package, and answering the
    permissive default here is what let the execution path run one."""
    root = tmp_path / "tools"
    root.mkdir()
    # A real, valid package OUTSIDE the tools dir, reached only via a symlink.
    _make_tool(tmp_path, "real", "import sys\nsys.stdout.write('x')\n")
    (root / "evil").symlink_to(tmp_path / "real", target_is_directory=True)
    _install_tools(monkeypatch, root)

    listed = {t["name"]: t for t in list_tools()}
    assert listed["evil"]["valid"] is False
    assert listed["evil"]["enabled"] is False  # ... and the switch reads OFF, not on
    assert "real directory" in (listed["evil"]["error"] or "")
    assert enabled_llm_tools() == []  # never advertised or executable


def test_symlinked_manifest_listed_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A package whose tool.json is a SYMLINK is listed invalid: a scan must
    never read a manifest through a link that could point outside the package."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "linky", "import sys\nsys.stdout.write('x')\n")
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
    (pkg / "tool.json").unlink()
    (pkg / "tool.json").symlink_to(real_manifest)
    _install_tools(monkeypatch, root)

    listed = {t["name"]: t for t in list_tools()}
    assert listed["linky"]["valid"] is False
    assert "real file" in (listed["linky"]["error"] or "")


def test_set_enabled_never_writes_through_a_symlinked_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The H3 guarantee this used to need a containment check for, now structural.

    A symlinked ``tool.json`` could once have redirected ``set_enabled``'s rewrite
    onto a file outside the package (``write_text`` follows links), so the write
    boundary carried its own resolve-then-contain refusal. Since web-v5 P1 the
    toggle does not open the manifest AT ALL, so the foreign file is untouched by
    construction rather than by a check that could be forgotten.

    The toggle itself now SUCCEEDS, and that is the deliberate half: it writes the
    package's own ``.afterthread-state.json``. The package stays invalid for the symlinked
    manifest (so it is never advertised or executed either way), and an operator
    can now switch a broken package OFF -- which is exactly when they want to."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
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
    (pkg / "tool.json").unlink()
    (pkg / "tool.json").symlink_to(outside)
    _install_tools(monkeypatch, root)

    assert set_enabled("echo", False) is True
    assert outside.read_text() == original  # foreign file untouched (never opened)
    assert _state_path(pkg).is_file()  # the toggle went to its OWN file
    listed = {t["name"]: t for t in list_tools()}["echo"]
    assert listed["valid"] is False and listed["enabled"] is False


def test_set_enabled_refuses_a_symlinked_state_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The write-boundary refusal, moved to the file the toggle actually writes.

    ``.afterthread-state.json`` is now the only thing a toggle touches, so it inherits the
    hazard: a symlink planted at that name (by an unjailed builder, by an
    operator) must not carry the write out of the package. The publish's pre-write
    ``lstat`` refuses any non-regular target outright -- and even reaching
    ``os.replace`` would only have replaced the LINK -- so the foreign file is
    untouched and the toggle honestly reports "did not happen"."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    _state_path(pkg).unlink()
    _state_path(pkg).symlink_to(outside)
    _install_tools(monkeypatch, root)

    assert set_enabled("echo", False) is False
    assert outside.read_text(encoding="utf-8") == "keep"  # never written through
    assert _state_path(pkg).is_symlink()  # the link itself survives
    # And the READ side agrees: a non-regular state file is UNREADABLE, which is
    # a disabled, invalid row -- never a silent fallback to "on".
    listed = {t["name"]: t for t in list_tools()}["echo"]
    assert listed["valid"] is False and listed["enabled"] is False
    assert listed["error"] == tools._STATE_UNREADABLE_ERROR


def test_a_publish_whose_package_becomes_a_symlink_at_the_boundary_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The last containment re-check refuses an ancestor swapped for a symlink.

    The publisher's own pre-write ``lstat`` protects the FINAL component but not
    its ancestors: it -- like ``mkstemp(dir=...)`` and ``os.replace`` -- follows
    directories above the state filename. ``set_enabled`` therefore resolves the
    package once to identify it, then checks containment again immediately before
    calling the publisher.

    Driven at exactly that instant rather than by timing a filesystem race: the
    re-check is replaced by a wrapper that swaps the package before delegating.

    What must hold is BOTH halves -- the toggle reports "did not happen" (the route
    turns that into a 404) and the link's target is left without so much as a temp
    file in it."""
    root = tmp_path / "tools"
    version = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n", enabled=True)
    pkg = _package_path(version)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _install_tools(monkeypatch, root)
    aside = root / ".moved-aside"
    before = (pkg / tools._META_DIRNAME / tools._PACKAGE_STATE_FILENAME).read_bytes()
    real_resolve = tools._resolve_package_dir_no_alias

    def swap_before_recheck(name: str) -> tools.PackageRoot | None:
        os.rename(pkg, aside)  # the package the toggle resolved, moved away
        pkg.symlink_to(elsewhere, target_is_directory=True)
        return real_resolve(name)

    monkeypatch.setattr(tools, "_resolve_package_dir_no_alias", swap_before_recheck)

    assert set_enabled("echo", False) is False

    assert list(elsewhere.iterdir()) == []  # nothing written THROUGH the link
    assert (
        aside / tools._META_DIRNAME / tools._PACKAGE_STATE_FILENAME
    ).read_bytes() == before  # nor into the real package
    assert (root / "echo").is_symlink()  # the planted link is untouched too


def test_a_non_regular_state_file_is_not_repaired_by_the_toggle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The documented repair splits in two, and this is the half it does NOT cover.

    A regular file holding garbage IS repaired by pressing the toggle again -- the
    publish does not read the file, it replaces it (pinned by
    ``test_an_unreadable_state_file_disables_rather_than_defaulting_to_on``). A
    NON-REGULAR entry at that name is not: ``_write_package_file_atomic``'s
    pre-write ``lstat`` refuses any non-regular target outright, which is a
    deliberate write-boundary property and not something to relax so a README
    sentence comes true. So the PATCH fails, the route answers 404, the entry sits
    there untouched, and the only repair is the operator removing it by hand (D21).

    A DIRECTORY is the shape pinned here; the other two the README enumerates have
    tests of their own for reasons of their own -- a symlink because it must not be
    written THROUGH (``test_set_enabled_refuses_a_symlinked_state_file``), a FIFO
    because it must not HANG (``test_set_enabled_with_a_fifo_manifest_does_not_hang``)."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n", enabled=True)
    _state_path(pkg).unlink()
    _state_path(pkg).mkdir()
    (_state_path(pkg) / "keep.txt").write_text("operator's", encoding="utf-8")
    _install_tools(monkeypatch, root)

    assert set_enabled("echo", False) is False  # what the route turns into a 404
    assert set_enabled("echo", True) is False  # neither direction repairs it

    entry = _state_path(pkg)
    assert entry.is_dir() and (entry / "keep.txt").read_text(encoding="utf-8") == "operator's"
    assert sorted(child.name for child in _state_path(pkg).parent.iterdir()) == [
        tools._CURRENT_FILENAME,
        tools._PACKAGE_STATE_FILENAME,
    ]  # no temp file left behind by the refusal either
    listed = {t["name"]: t for t in list_tools()}["echo"]
    assert listed["valid"] is False and listed["enabled"] is False
    assert listed["error"] == tools._STATE_UNREADABLE_ERROR

    # The hand repair the README now names -- and only it -- puts the tool back.
    shutil.rmtree(entry)
    assert set_enabled("echo", False) is True
    assert {t["name"]: t["enabled"] for t in list_tools()}["echo"] is False


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
    real_version = _resolved_version(root / "real")
    assert (real_version / "tool.json").is_file()
    assert (real_version / "run.py").is_file()
    listed = {t["name"]: t for t in list_tools()}
    assert listed["real"]["valid"] is True
    assert "alias" not in listed  # the phantom invalid row is gone


def test_set_enabled_refuses_internal_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """set_enabled must never act on a package THROUGH an internal alias:
    tools/<alias> -> tools/<real> resolves inside the root, so absent the
    pre-resolve symlink block a toggle on the alias would land the state file
    INSIDE the REAL package -- switching off a tool the operator addressed by
    another name. The alias PATCH is refused (False) and the real package is left
    byte-for-byte untouched, state file included."""
    root = tmp_path / "tools"
    real_version = _make_tool(root, "real", "import sys\nsys.stdout.write('x')\n", enabled=True)
    (root / "alias").symlink_to(root / "real", target_is_directory=True)
    _install_tools(monkeypatch, root)

    before = (real_version / "tool.json").read_bytes()
    assert set_enabled("alias", False) is False
    assert (real_version / "tool.json").read_bytes() == before  # real manifest untouched
    assert _state_path(real_version).is_file()  # and package state was untouched
    real = {t["name"]: t for t in list_tools()}["real"]
    assert real["enabled"] is True  # the real package's flag never flipped


def test_set_enabled_toggles_a_package_whose_manifest_is_oversized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two manifest-cap refusals retire together, and the file stays byte-identical.

    ``set_enabled`` used to refuse an OVERSIZED manifest, and separately to refuse
    one whose pretty-printed (``indent=2``) re-serialization would cross the cap --
    a real hazard back when a mere toggle REWROTE the file and could expand it past
    the ceiling, flipping the tool invalid. There is no rewrite and no
    re-serialization now, so both refusals are gone with the thing they guarded.

    What is left is strictly better: an operator can switch OFF a package whose
    manifest is broken (exactly when they most want to), the manifest is not
    touched, and the package stays invalid -- so nothing became runnable that was
    not runnable before."""
    root = tmp_path / "tools"
    big_version = _make_tool(
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

    before = (big_version / "tool.json").read_bytes()
    assert set_enabled("big", False) is True
    assert (big_version / "tool.json").read_bytes() == before  # untouched
    listed = {t["name"]: t for t in list_tools()}["big"]
    assert listed["valid"] is False and listed["enabled"] is False
    assert enabled_llm_tools() == []


def test_set_enabled_with_a_fifo_manifest_does_not_hang(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The F3b hang hazard, re-measured on the path that still exists.

    A writer-less FIFO at ``tool.json`` would block a plain ``read_text()``
    FOREVER, wedging the PATCH worker. ``set_enabled`` no longer opens the manifest
    on any path, so the hazard is structurally absent rather than caught -- and the
    toggle succeeds, leaving the FIFO exactly as it found it.

    The same is pinned for the file the toggle DOES open: a FIFO at
    ``.afterthread-state.json`` is refused by the publish's ``lstat`` S_ISREG gate (and by the
    read side's, which is why the row below reports unreadable), neither of which
    can block. Both halves run on a WATCHED daemon thread so a regression fails
    LOUDLY here instead of wedging the whole suite."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "fifotool", "import sys\nsys.stdout.write('x')\n")
    (pkg / "tool.json").unlink()
    os.mkfifo(pkg / "tool.json")  # a writer-less FIFO -- read_text() would block forever
    _install_tools(monkeypatch, root)

    def toggle(name: str) -> bool:
        box: dict[str, bool] = {}
        worker = threading.Thread(
            target=lambda: box.__setitem__("ok", set_enabled(name, False)), daemon=True
        )
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), "set_enabled hung on a FIFO (F3b regression)"
        return box["ok"]

    assert toggle("fifotool") is True  # the manifest is never opened at all
    assert (pkg / "tool.json").is_fifo()  # still the FIFO, never overwritten

    fifo_state = _make_tool(root, "fifostate", "import sys\nsys.stdout.write('x')\n")
    _state_path(fifo_state).unlink()
    os.mkfifo(_state_path(fifo_state))

    assert toggle("fifostate") is False  # the publish refuses a non-regular target
    assert _state_path(fifo_state).is_fifo()
    listed = {t["name"]: t for t in list_tools()}["fifostate"]
    assert listed["valid"] is False and listed["enabled"] is False


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
    big_version = _make_tool(
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
    assert (big_version / "tool.json").stat().st_size < _MANIFEST_MAX_BYTES


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
    monkeypatch.setattr("afterthread.services.tools.get_settings", lambda: Settings(tools_dir=""))
    assert tools_dir() is None
    assert list_tools() == []
    assert enabled_llm_tools() == []
    assert set_enabled("x", True) is False
    assert delete_tool("x") is False


# --- AI summary sidecar (D40) ----------------------------------------------


def _sidecar(pkg: Path) -> Path:
    meta = pkg / tools._META_DIRNAME
    meta.mkdir(exist_ok=True)
    return meta / tools._SUMMARY_FILENAME


def _write_meta(pkg: Path, **fields: Any) -> bool:
    """``write_tool_meta`` with the ``updated_at`` every real caller supplies.

    The writer REFUSES a meta without a string ``updated_at`` (it will not invent
    a timestamp on a caller's behalf -- see write_tool_meta), and both production
    callers stamp their own. So tests state the fields actually under test and
    inherit a valid stamp here, instead of repeating a timestamp literal
    everywhere or -- worse -- passing a shape no caller ever passes and getting a
    False that hides the reason the test meant to exercise."""
    if not pkg.is_dir():
        return False
    (pkg / tools._META_DIRNAME).mkdir(exist_ok=True)
    origin = fields.pop("origin", None)
    if isinstance(origin, dict) and not tools.write_origin_meta(
        tools.BuildRoot(pkg),
        {
            "source": "test-fixture",
            "openapi_url": origin.get("openapi_url"),
            "instructions": origin.get("instructions"),
            "feedback": origin.get("feedback"),
            "previous": origin.get("previous"),
        },
    ):
        return False
    return tools.write_tool_meta(
        _version_root(pkg),
        {"updated_at": "2026-01-01T00:00:00+00:00", **fields},
    )


def test_tool_meta_round_trips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """write_tool_meta -> read_tool_meta returns the same dict, and the sidecar
    is a real file inside the package (so delete_tool's rmtree takes it)."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)

    origin = {
        "source": "test-fixture",
        "openapi_url": "http://kb.example/openapi.json",
        "instructions": "build",
        "feedback": None,
        "previous": None,
    }
    assert tools.write_origin_meta(tools.BuildRoot(pkg), origin)
    summary = {
        "summary": "這個工具會查 KB",
        "updated_at": "2026-07-26T00:00:00+00:00",
        "llm_log_id": 12,
        "llm_log_process": "process-token-from-whoever-wrote-this",
    }
    assert tools.write_tool_meta(_version_root(pkg), summary) is True
    assert _sidecar(pkg).is_file()
    assert tools.read_tool_meta(_version_root(pkg)) == {**summary, "origin": origin}


def test_read_tool_meta_degrades_on_missing_corrupt_and_non_object(tmp_path: Path) -> None:
    """Every unusable sidecar reads as "no metadata", never an exception: a
    corrupt one must empty the summary panel, not break the tools list."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert tools.read_tool_meta(_version_root(pkg)) is None  # absent

    _sidecar(pkg).write_text("{not json", encoding="utf-8")
    assert tools.read_tool_meta(_version_root(pkg)) is None  # unparseable

    _sidecar(pkg).write_text('["a list"]', encoding="utf-8")
    assert tools.read_tool_meta(_version_root(pkg)) is None  # valid JSON, wrong shape


def test_read_tool_meta_refuses_oversized_sidecar(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    padding = "x" * (_AI_META_MAX_BYTES + 100)
    _sidecar(pkg).write_text(json.dumps({"summary": padding}), encoding="utf-8")
    assert tools.read_tool_meta(_version_root(pkg)) is None


def test_read_tool_meta_survives_pathological_nesting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deeply nested sidecar exhausts the stack INSIDE json.loads, which raises
    RecursionError -- not the ValueError the parse guard used to catch alone.

    It degrades to None like any other unusable sidecar, and the row still
    lists."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    # The depth is ASSERTED, not assumed. CPython 3.14 raises on genuine stack
    # exhaustion rather than at sys.getrecursionlimit() (setrecursionlimit does
    # not move it), and shallower nesting simply PARSES -- which would leave this
    # test green for the wrong reason: the result would degrade to None on the
    # not-a-JSON-object check instead of on the guard under test.
    depth = 120_000
    nested = "[" * depth + "]" * depth
    assert len(nested.encode("utf-8")) < _AI_META_MAX_BYTES  # not the size guard refusing it
    with pytest.raises(RecursionError):
        json.loads(nested)
    _sidecar(pkg).write_text(nested, encoding="utf-8")

    assert tools.read_tool_meta(_version_root(pkg)) is None
    listed = list_tools()
    assert [row["name"] for row in listed] == ["echo"]


def test_write_tool_meta_redacts_the_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A secret that reached the summary is masked BEFORE it lands on disk --
    the sidecar rides into a later revise's staging copy, where the
    embedded-secret gate would reject the whole package over it."""
    root = tmp_path / "tools"
    secret = "another-tools-live-secret-abcdef"
    _make_tool(root, "other", "import sys\nsys.stdout.write('x')\n", dotenv=f"OTHER_KEY={secret}\n")
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)

    assert _write_meta(pkg, summary=f"it authenticates with {secret}") is True
    stored = tools.read_tool_meta(_version_root(pkg))
    assert stored is not None
    assert secret not in stored["summary"]
    assert tools._REDACTION_MARKER in stored["summary"]
    assert secret not in _sidecar(pkg).read_text(encoding="utf-8")


def test_write_tool_meta_redacts_every_string_not_just_the_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The summary was never the only operator/LLM-influenced text in here.

    ``origin.openapi_url`` is the install form's URL, which routinely carries
    the form secret as a query token, and ``origin.instructions`` is free
    operator text -- both landed on disk verbatim while only ``summary`` was
    masked. They are now masked BY NAME, which is the whole redaction surface:
    every other field on disk is a literal or a number the writer chose."""
    secret = "install-form-secret-abcdef"
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))

    assert (
        _write_meta(
            pkg,
            summary="沒有秘密的說明",
            origin={
                "openapi_url": f"https://kb.example/openapi.json?token={secret}",
                "instructions": f"用 {secret} 認證",
            },
        )
        is True
    )

    raw = _sidecar(pkg).read_text(encoding="utf-8")
    assert secret not in raw
    stored = tools.read_tool_meta(_version_root(pkg))
    assert stored is not None
    assert tools._REDACTION_MARKER in stored["origin"]["openapi_url"]
    assert tools._REDACTION_MARKER in stored["origin"]["instructions"]
    # Only the secret is rewritten -- the rest of the URL survives, so the
    # sidecar stays useful as the install's only record of where it came from.
    assert "https://kb.example/openapi.json?token=" in stored["origin"]["openapi_url"]


def test_write_tool_meta_keys_survive_a_secret_that_equals_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The r1 whole-tree redaction masked dict KEYS as well as values, and that
    turned the redactor's own success into corruption.

    An install-form ``secret_value`` of ``"summary"`` clears the 6-char floor, so
    it was a registered secret like any other -- and the walk duly rewrote the
    KEY ``"summary"`` into the redaction marker. The write returned True, the
    file was valid JSON, and every later read found no ``summary`` field at all:
    the panel silently emptied with nothing reporting a failure. Keys are now
    literals this module writes, so there is no key left to mask."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for key in ("summary", "origin", "instructions", "updated_at"):
        monkeypatch.setattr(tools, "known_secret_values", lambda key=key: frozenset({key}))
        assert (
            _write_meta(
                pkg,
                summary="這個工具會查 KB",
                llm_log_id=7,
                origin={"openapi_url": "http://kb.example/o.json", "instructions": "查 KB"},
            )
            is True
        )
        stored = tools.read_tool_meta(_version_root(pkg))
        assert stored is not None, f"a secret equal to the key {key!r} broke the schema"
        assert stored["summary"] == "這個工具會查 KB"
        assert stored["llm_log_id"] == 7
        assert stored["origin"]["openapi_url"] == "http://kb.example/o.json"


def test_write_tool_meta_drops_unknown_keys_and_containers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sidecar IS the five schema fields; anything else a caller (or a
    hand-edited file read back in) carries is dropped by the next write.

    Round-tripping extra keys was never a contract -- it was a side effect of
    serializing the caller's dict, and it is exactly what let unmasked text onto
    disk: the r1 walk had no case for a TUPLE, which json serializes as an array
    perfectly happily, so a tuple of strings rode through unredacted. Nothing
    caller-shaped reaches the file now, so the whole class is gone rather than
    one container type being added to a walk."""
    secret = "tuple-borne-secret-abcdef"
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))

    assert (
        _write_meta(
            pkg,
            summary="沒有秘密的說明",
            notes=("plain", secret),  # a tuple: JSON-serializable, never walked
            deep={"nested": {"deeper": [secret]}},
        )
        is True
    )

    raw = _sidecar(pkg).read_text(encoding="utf-8")
    assert secret not in raw
    stored = tools.read_tool_meta(_version_root(pkg))
    assert stored is not None
    assert set(stored) == {
        "summary",
        "updated_at",
        "llm_log_id",
        "llm_log_process",
        "origin",
    }


@pytest.mark.parametrize(
    "origin, expected",
    [
        (None, None),
        ("not a dict", None),
        (("openapi_url", "http://x"), None),
        (
            {},
            {
                "source": "test-fixture",
                "openapi_url": None,
                "instructions": None,
                "feedback": None,
                "previous": None,
            },
        ),
        (
            {"openapi_url": 12, "junk": "dropped"},
            {
                "source": "test-fixture",
                "openapi_url": None,
                "instructions": None,
                "feedback": None,
                "previous": None,
            },
        ),
        (
            {"openapi_url": "http://kb.example/o.json", "instructions": "查 KB", "junk": "dropped"},
            {
                "source": "test-fixture",
                "openapi_url": "http://kb.example/o.json",
                "instructions": "查 KB",
                "feedback": None,
                "previous": None,
            },
        ),
    ],
    ids=["none", "scalar", "tuple", "empty", "wrong-types", "narrowed"],
)
def test_write_tool_meta_narrows_the_origin(
    tmp_path: Path, origin: Any, expected: dict[str, Any] | None
) -> None:
    """``origin`` is the install's ONLY record of where a package came from, so
    it is kept -- and it is free operator text, so it is kept NARROW: the two
    fields we understand, as strings or None, and nothing else."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert _write_meta(pkg, summary="s", origin=origin) is True
    stored = tools.read_tool_meta(_version_root(pkg))
    assert stored is not None
    assert stored["origin"] == expected


def test_write_tool_meta_fails_closed_when_redaction_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The redaction is the gate, so a failing secret provider writes NOTHING
    (rather than an unmasked sidecar) and reports failure -- for ANY of the three
    text fields, not just the summary."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()

    def boom() -> frozenset[str]:
        raise RuntimeError("provider down")

    monkeypatch.setattr(tools, "known_secret_values", boom)
    assert _write_meta(pkg, summary="anything") is False
    assert not _sidecar(pkg).exists()
    assert _write_meta(pkg, summary="", origin={"instructions": "anything"}) is False
    assert not _sidecar(pkg).exists()
    assert _write_meta(pkg, summary="", origin={"openapi_url": "http://x"}) is False
    assert not _sidecar(pkg).exists()


def test_write_tool_meta_refuses_a_non_string_summary(tmp_path: Path) -> None:
    """``summary`` is a STRING by contract: the read path renders any other type
    as "no summary", so writing one would make the sidecar lie about whether a
    summary exists.

    A JSON ``null`` is the ONE exception, and it is a coercion rather than a
    refusal: it means the same thing as the empty placeholder a failed generation
    writes, so it lands as ``""`` instead of slipping past the type check as
    "not a string, but not refused either" (which is how a hand-written
    ``{"summary": null}`` used to become a malformed empty value)."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for bad in ({"nested": "object"}, 12, ["a"], True):
        assert _write_meta(pkg, summary=bad) is False
        assert not _sidecar(pkg).exists()

    assert _write_meta(pkg, summary=None) is True
    stored = tools.read_tool_meta(_version_root(pkg))
    assert stored is not None
    assert stored["summary"] == ""


def test_write_tool_meta_requires_a_string_updated_at(tmp_path: Path) -> None:
    """Every real caller stamps its own, so a missing/out-of-shape one is a
    caller bug -- and inventing a timestamp on their behalf would put a fact in
    the file that nothing actually observed."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert tools.write_tool_meta(_version_root(pkg), {"summary": "s"}) is False
    assert tools.write_tool_meta(_version_root(pkg), {"summary": "s", "updated_at": 12}) is False
    assert not _sidecar(pkg).exists()


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("llm_log_id", "three", None),
        ("llm_log_id", True, None),  # bool is an int subclass; never a log link
        ("llm_log_id", 7, 7),
    ],
    ids=[
        "str-id",
        "bool-id",
        "id",
    ],
)
def test_write_tool_meta_coerces_the_scalar_fields(
    tmp_path: Path, field: str, value: Any, expected: Any
) -> None:
    """A non-int ``llm_log_id`` must never render as a link."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert _write_meta(pkg, summary="s", **{field: value}) is True
    stored = tools.read_tool_meta(_version_root(pkg))
    assert stored is not None
    assert stored[field] == expected


def test_write_tool_meta_and_read_tool_meta_share_one_size_cap(tmp_path: Path) -> None:
    """The asymmetry that made a LEGAL sidecar permanently unreadable.

    The reader refused anything past the MANIFEST's cap while the writer checked
    nothing at all, so a sidecar the writer happily produced could read back as
    None forever -- the write reporting success while the summary silently
    vanished from every later GET. Both sides now answer to the sidecar's own
    _AI_META_MAX_BYTES: anything the writer accepts, the reader reads back.

    The overrun is a JSON-SERIALIZATION effect, which is worth stating because
    the raw character counts do not get you there: the reader's cap is compared
    against the decoded text's LENGTH, and 20 000 chars of instructions (the
    install form's own max_length) plus an 8 000-char summary is only ~30 000
    characters however many bytes they occupy. What blows past it is escaping --
    control characters are legal in that free-text field and each becomes a
    6-char ``\\uXXXX`` escape, so a legal payload serializes to six times its
    length."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()

    legal = _write_meta(pkg, summary="說" * 8_000)
    assert legal is True
    serialized = _sidecar(pkg).read_text(encoding="utf-8")
    assert len(serialized.encode("utf-8")) < _AI_META_MAX_BYTES
    stored = tools.read_tool_meta(_version_root(pkg))
    assert stored is not None
    assert stored["summary"] == "說" * 8_000

    # Past the shared cap the WRITER refuses, so the reader is never handed a
    # file it would have to answer None for.
    assert _write_meta(pkg, summary="x" * (_AI_META_MAX_BYTES + 10)) is False
    assert tools.read_tool_meta(_version_root(pkg)) == stored  # the previous sidecar is untouched


def test_write_tool_meta_refuses_to_resurrect_a_deleted_package(tmp_path: Path) -> None:
    """A write into a package directory that is not there is refused, and
    creates NOTHING -- otherwise a summary landing after a racing delete would
    re-create the package as a directory holding only a sidecar, which the
    registry would then list as a ghost broken package."""
    gone = tmp_path / "nope" / "gone"
    assert _write_meta(gone, summary="s") is False
    assert not gone.exists()
    assert not gone.parent.exists()


def test_write_tool_meta_refuses_symlinked_sidecar(tmp_path: Path) -> None:
    """A symlinked sidecar is refused, so a link raced into the package can never
    redirect the write out of it.

    Enforced by the atomic writer's pre-write ``lstat`` since r3 (it does not
    follow the final component, exactly as the old ``O_NOFOLLOW`` open did not).
    The refusal had to be carried over deliberately: ``os.replace`` onto a
    symlink would replace the LINK rather than write through it -- not an escape,
    but a silent change to what this contract promises."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    _sidecar(pkg).symlink_to(outside)

    assert _write_meta(pkg, summary="s") is False
    assert outside.read_text(encoding="utf-8") == "{}"  # target untouched
    assert _sidecar(pkg).is_symlink()  # not silently replaced by a regular file
    assert not list(pkg.glob(f"{tools._AI_META_FILENAME}.*"))  # no temp left behind


def test_write_tool_meta_refuses_a_non_regular_sidecar(tmp_path: Path) -> None:
    """The S_ISREG half of the same gate: a FIFO (or device/directory) sitting at
    the sidecar name is refused rather than replaced.

    ``_write_regular_file``'s ``fstat`` used to enforce this; the atomic path's
    ``lstat`` is where it lives now. Without it a FIFO an operator (or a
    ``run_shell``) put there would be silently swapped for a regular file."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    os.mkfifo(_sidecar(pkg))

    assert _write_meta(pkg, summary="s") is False
    assert stat.S_ISFIFO(os.lstat(_sidecar(pkg)).st_mode)  # still the FIFO
    assert not list(pkg.glob(f"{tools._AI_META_FILENAME}.*"))


def test_write_tool_meta_keeps_the_old_sidecar_when_the_publish_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The r2 writer opened the TARGET with O_TRUNC, so the previous sidecar was
    already destroyed by the time any failure could be reported.

    That is real data loss, not a lost update: on ENOSPC/quota/an I/O error the
    caller was told False -- "nothing happened" -- while the existing summary
    had been truncated to nothing, and the sidecar is the ONLY copy of both that
    summary and the install's ``origin``. Writing to a
    temp file and publishing with ``os.replace`` means the old content survives
    every failure mode, and the file is never observable half-written.

    The failure is injected at ``os.replace`` -- the last step, after the temp
    file is fully written and fsynced -- because that is the strictest version of
    the claim: even a failure at the very END leaves the previous file intact."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert _write_meta(pkg, summary="原本的說明") is True
    before = _sidecar(pkg).read_bytes()

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    assert _write_meta(pkg, summary="新的說明") is False

    assert _sidecar(pkg).read_bytes() == before  # byte-identical, not truncated
    # ... and nothing was left lying around in the package: a stray temp file
    # would be scanned by every later validate_package embedded-secret sweep.
    assert not list(pkg.glob(f"{tools._AI_META_FILENAME}.*"))
    assert sorted(child.name for child in pkg.iterdir()) == [tools._META_DIRNAME]


def test_the_publish_fsyncs_the_package_directory_after_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1R3-3: the rename is made DURABLE, because one payload's loss fails OPEN.

    This publisher's "no directory fsync" ruling was inherited from cli.py's
    ``.env`` and justified by a worst case of one REGENERABLE summary. The same
    publisher now carries the enabled toggle, which is not regenerable and whose
    loss goes the wrong way: a legacy manifest saying ``enabled: true``, an
    operator's successful PATCH to false, then power loss after ``os.replace``
    returned but before the directory entry is durable, and the fallback re-enables
    a tool that was deliberately switched off.

    Asserted at the mechanism (a crash cannot be staged in a unit test): after the
    rename, the DIRECTORY holding the published name is fsynced, and it is fsynced
    for the sidecar as well -- one discipline for every file this function
    publishes, rather than a flag the next backend-authored file has to remember to
    set."""
    pkg = tmp_path / "pkg"
    (pkg / tools._META_DIRNAME).mkdir(parents=True)
    synced: list[tuple[bool, int]] = []
    real_fsync = os.fsync

    def watched(fd: int) -> None:
        info = os.fstat(fd)
        synced.append((stat.S_ISDIR(info.st_mode), info.st_ino))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", watched)

    assert tools.write_package_state(tools.PackageRoot(pkg), False) is True
    assert synced[0][0] is False  # the temp FILE's contents first ...
    assert synced[-1] == (
        True,
        (pkg / tools._META_DIRNAME).stat().st_ino,
    )  # ... then the name that flipped

    synced.clear()
    assert _write_meta(pkg, summary="說明") is True
    assert synced[-1] == (
        True,
        (pkg / tools._META_DIRNAME).stat().st_ino,
    )  # the sidecar publish too


def test_a_failed_directory_fsync_does_not_unpublish_a_written_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The durability step may not turn a publish that HAPPENED into a False.

    It runs after ``os.replace`` has returned, so by then the file is published and
    the toggle has taken effect. Reporting "did not happen" there would be a lie the
    caller acts on -- ``set_enabled`` maps False to a 404, so the operator would be
    told their switch did not land while the tool really is switched off. A failing
    (or unsupported) directory fsync therefore leaves exactly the pre-P1R3-3
    guarantee: atomic, not durable."""
    pkg = tmp_path / "pkg"
    (pkg / tools._META_DIRNAME).mkdir(parents=True)
    real_fsync = os.fsync

    def refuse_directories(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(22, "Invalid argument")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", refuse_directories)

    assert tools.write_package_state(tools.PackageRoot(pkg), False) is True
    state_path = pkg / tools._META_DIRNAME / tools._PACKAGE_STATE_FILENAME
    assert json.loads(state_path.read_text(encoding="utf-8")) == _state_document(False)
    assert not list(state_path.parent.glob(f"{tools._PACKAGE_STATE_FILENAME}.*"))


def test_write_tool_meta_publishes_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent reader never observes a half-written sidecar.

    The property ``os.replace`` buys over open-and-truncate, asserted at the
    mechanism rather than by racing threads: the bytes are complete and fsynced
    in the temp file BEFORE the name flips, so the sidecar path only ever holds
    the old file or the whole new one."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert _write_meta(pkg, summary="舊的") is True
    seen: dict[str, Any] = {}
    real_replace = os.replace

    def watched(src: Any, dst: Any, **kwargs: Any) -> None:
        # At swap time the OLD file is still whole under the sidecar name, and
        # the NEW content is already complete in a file of its own.
        seen["old"] = json.loads(Path(dst).read_text(encoding="utf-8"))["summary"]
        seen["new"] = json.loads(Path(src).read_text(encoding="utf-8"))["summary"]
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", watched)
    assert _write_meta(pkg, summary="新的") is True

    assert seen == {"old": "舊的", "new": "新的"}
    assert json.loads(_sidecar(pkg).read_text(encoding="utf-8"))["summary"] == "新的"


def test_write_tool_meta_preserves_an_operator_set_mode(tmp_path: Path) -> None:
    """R7-3: an operator's ``chmod`` on the sidecar survives the atomic publish.

    A regression the atomicity fix carried in silently: ``os.replace`` publishes
    the TEMP file, so the temp file's mode becomes the sidecar's. An operator who
    had widened theirs to ``0o640`` (their own group reads the summaries) got it
    narrowed back to mkstemp's ``0o600`` on the next PATCH or regenerate, with
    nothing reporting the change and nothing to notice until the group's reads
    started failing. The pre-write ``lstat`` already has ``st_mode`` in hand for
    the symlink refusal, so one ``fchmod`` on the temp fd carries it across."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert _write_meta(pkg, summary="舊的") is True
    os.chmod(_sidecar(pkg), 0o640)

    assert _write_meta(pkg, summary="新的") is True

    assert stat.S_IMODE(os.stat(_sidecar(pkg)).st_mode) == 0o640
    assert json.loads(_sidecar(pkg).read_text(encoding="utf-8"))["summary"] == "新的"


def test_write_tool_meta_keeps_the_default_mode_for_a_fresh_sidecar(tmp_path: Path) -> None:
    """Preserving means not silently CHANGING what an operator set -- so a
    sidecar with no predecessor to inherit from keeps the writer's own default.

    Pinned RELATIVELY (create one, compare against it) rather than against a
    ``0o600`` literal: the process umask decides the exact bits, so a literal
    would pin the test environment instead of the behavior. What this asserts is
    the property the fix promises -- the fresh-file path is UNCHANGED, and a
    rewrite of a sidecar nobody chmod'd does not drift away from it either."""
    first = tmp_path / "first"
    first.mkdir()
    assert _write_meta(first, summary="s") is True
    default_mode = stat.S_IMODE(os.stat(_sidecar(first)).st_mode)

    second = tmp_path / "second"
    second.mkdir()
    assert _write_meta(second, summary="s") is True
    assert stat.S_IMODE(os.stat(_sidecar(second)).st_mode) == default_mode

    assert _write_meta(first, summary="s2") is True
    assert stat.S_IMODE(os.stat(_sidecar(first)).st_mode) == default_mode


def test_write_tool_meta_publishes_a_readable_sidecar_under_a_hostile_umask(
    tmp_path: Path,
) -> None:
    """R11: writer-accepts must imply reader-reads-BACK for PERMISSIONS too.

    ``mkstemp``'s ``0o600`` is masked by the process umask, so a service started
    under a umask that strips owner bits published a sidecar the very next
    ``read_tool_meta`` could not open: the write reported success, every later GET
    answered "no summary", and every regenerate burned a whole LLM call rewriting
    a file it would then fail to read. The explicit ``fchmod`` to ``_OWNER_RW``
    makes the published mode independent of the umask. Simulated by actually
    setting the process umask (restored in ``finally``), because that is the exact
    mechanism -- a mocked mkstemp would test the mock."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    _sidecar(pkg).parent.mkdir(exist_ok=True)
    previous = os.umask(0o277)  # strips owner write AND every group/other bit
    try:
        assert _write_meta(pkg, summary="總結") is True
    finally:
        os.umask(previous)

    assert stat.S_IMODE(os.stat(_sidecar(pkg)).st_mode) & 0o600 == 0o600
    assert tools.read_tool_meta(_version_root(pkg)) is not None  # the invariant this exists for


def test_write_tool_meta_adds_owner_rw_to_an_inherited_mode(tmp_path: Path) -> None:
    """The same floor applies to an INHERITED mode (R7-3 + R11 composed): a
    sidecar that ever landed without owner-read -- an operator's chmod, or one
    published under a hostile umask before this floor existed -- must not
    propagate that state forward forever. Group/other bits the operator chose are
    still honored exactly as R7-3 promised; only the owner bits are forced."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert _write_meta(pkg, summary="舊的") is True
    os.chmod(_sidecar(pkg), 0o040)  # group-read only: owner cannot read it back

    assert _write_meta(pkg, summary="新的") is True

    mode = stat.S_IMODE(os.stat(_sidecar(pkg)).st_mode)
    assert mode & 0o600 == 0o600  # owner rw restored
    assert mode & 0o040 == 0o040  # the operator's group-read choice survives
    assert tools.read_tool_meta(_version_root(pkg)) is not None


def test_write_tool_meta_replaces_a_read_only_sidecar(tmp_path: Path) -> None:
    """The one semantic the atomic publish deliberately CHANGED, pinned so it
    reads as a decision rather than being rediscovered as a regression.

    ``_write_regular_file``'s ``O_TRUNC`` open needed write permission on the
    FILE, so a ``chmod 0o400`` sidecar refused the write with EACCES. Publishing
    by ``os.replace`` needs write permission on the DIRECTORY, so it now
    SUCCEEDS. That EACCES was incidental to the open rather than a designed
    contract: the package DIRECTORY is the protection boundary this subsystem
    supports -- it is what ``delete_tool`` removes wholesale and what every
    containment check is stated against -- and a read-only file under a writable
    package directory was never a promise we made (D40 r7 addendum).

    What the operator's read-only setting now yields is ``0o600``, not ``0o400``:
    the R11 owner-rw floor (``_OWNER_RW``) is OR'd into every inherited mode,
    because the backend must be able to read back the state it owns. Only the
    OWNER bits are forced -- a group/other choice still rides across untouched
    (pinned separately by the inherited-mode test above)."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert _write_meta(pkg, summary="舊的") is True
    os.chmod(_sidecar(pkg), 0o400)

    assert _write_meta(pkg, summary="新的") is True

    assert json.loads(_sidecar(pkg).read_text(encoding="utf-8"))["summary"] == "新的"
    assert stat.S_IMODE(os.stat(_sidecar(pkg)).st_mode) == 0o600  # R11 floor


# --- UTF-8 safety at both sidecar boundaries (D40 r3) ------------------------

# What ONE lone surrogate scrubs to. Named rather than inlined because the
# arithmetic is counter-intuitive: `surrogatepass` encodes a lone surrogate to
# THREE bytes, and decoding those back with `errors="replace"` substitutes one
# U+FFFD per undecodable BYTE -- so the scrub is 1 char in, 3 out, not 1-for-1.
_SURROGATE_FFFD = "�" * 3


def test_utf8_safe_matches_llm_log() -> None:
    """tools._utf8_safe and llm_log._utf8_safe MUST agree on every input.

    Deliberately duplicated rather than imported (llm_log stays a leaf
    observability module that must not import the tool runtime, and the reverse
    edge would create exactly the coupling both docstrings forbid -- the same
    duplication-with-rationale the redaction marker carries). Pinned equal on a
    probe set, mirroring test_redaction_marker_matches_llm_log, so the two can
    never silently drift into disagreeing about what a corrupt code point
    becomes."""
    probes = [
        "",
        "plain ascii",
        "說明 with CJK",
        "\ud800",  # a lone HIGH surrogate
        "\udfff",  # a lone LOW surrogate
        "a\ud800b\udc00c",
        "emoji 🙂 and a surrogate \ud800",
        "\U0001f600",  # a legitimate astral char (an ENCODED surrogate PAIR)
    ]
    for probe in probes:
        assert tools._utf8_safe(probe) == llm_log._utf8_safe(probe), probe
    # ONE lone surrogate becomes THREE U+FFFD, not one: surrogatepass encodes it
    # to three bytes and the replace-decode substitutes per undecodable BYTE.
    # Pinned literally, because "one bad char in, one out" is the natural (wrong)
    # assumption and every expectation below is built on the real ratio.
    assert tools._utf8_safe("a\ud800b") == "a" + _SURROGATE_FFFD + "b"
    # ... and the point of it: what comes out is always UTF-8 encodable.
    for probe in probes:
        tools._utf8_safe(probe).encode("utf-8")


def test_read_tool_meta_scrubs_lone_surrogates(tmp_path: Path) -> None:
    """A hand-edited sidecar can carry ``"\\ud800"`` -- a JSON-LEGAL escape that
    json.loads accepts and produces a str for, which is NOT UTF-8 encodable.

    Left unscrubbed it reached the GET response and blew up in Starlette's strict
    render encode: a 500 out of the one route whose entire job is to DEGRADE a
    corrupt sidecar. The write side cannot cover this -- the file never passed
    through our writer -- so the read is its own boundary."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    origin_path = pkg / tools._META_DIRNAME / tools._ORIGIN_FILENAME
    origin_path.parent.mkdir()
    origin_path.write_text(
        json.dumps(
            {
                "source": "hand-edited",
                "openapi_url": "http://kb.example/\ud800.json",
                "instructions": None,
                "feedback": None,
                "previous": None,
            }
        ),
        encoding="utf-8",
    )
    # ensure_ascii=True: this is what a hand-edit looks like on disk -- six ASCII
    # characters, a perfectly valid JSON file.
    _sidecar(pkg).write_text(
        json.dumps(
            {
                "summary": "a\ud800b",
                "updated_at": "2026-01-01T00:00:00+00:00\udfff",
                "llm_log_id": 3,
            }
        ),
        encoding="utf-8",
    )

    meta = tools.read_tool_meta(_version_root(pkg))
    assert meta is not None
    assert meta["summary"] == "a" + _SURROGATE_FFFD + "b"
    assert meta["updated_at"].endswith(_SURROGATE_FFFD)
    assert meta["origin"]["openapi_url"] == "http://kb.example/" + _SURROGATE_FFFD + ".json"
    assert meta["origin"]["instructions"] is None  # non-strings pass through
    assert meta["llm_log_id"] == 3
    # The property that matters: every string it hands back can be serialized.
    json.dumps(meta).encode("utf-8")
    for value in (meta["summary"], meta["updated_at"], meta["origin"]["openapi_url"]):
        value.encode("utf-8")


def test_write_tool_meta_scrubs_a_surrogate_bearing_summary(tmp_path: Path) -> None:
    """A surrogate in the three redacted TEXT fields is scrubbed, not refused.

    Adjudicated in that direction on purpose (see ``_redacted``): one bad code
    point is a display-level defect in a summary that is otherwise a genuine
    explanation, and refusing would throw the whole generation away -- or, on the
    install hook's placeholder path, leave the operator with no sidecar and no
    重新產生 button. U+FFFD is what the reader renders anyway."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()

    assert (
        _write_meta(
            pkg,
            summary="這個工具會查 KB\ud800",
            origin={"openapi_url": "http://kb.example/o.json\ud800", "instructions": "查 KB\udfff"},
        )
        is True
    )

    stored = tools.read_tool_meta(_version_root(pkg))
    assert stored is not None
    assert stored["summary"] == "這個工具會查 KB" + _SURROGATE_FFFD
    assert stored["origin"]["openapi_url"] == "http://kb.example/o.json" + _SURROGATE_FFFD
    assert stored["origin"]["instructions"] == "查 KB" + _SURROGATE_FFFD
    # On disk as real UTF-8, so the file round-trips through any reader.
    assert _SURROGATE_FFFD in _sidecar(pkg).read_text(encoding="utf-8")


def test_write_tool_meta_refuses_a_surrogate_bearing_updated_at(tmp_path: Path) -> None:
    """``updated_at`` is the one caller string that rides through verbatim, so it
    is the field the fail-closed guard still has to cover.

    The serialize+encode used to sit OUTSIDE that try, so this raised
    UnicodeEncodeError straight out of write_tool_meta -- a PATCH answering 500
    where its contract says False (-> 404). Scrubbing it would only paper over a
    caller bug (every real caller stamps a machine timestamp), so the refusal is
    the honest answer -- and the previous sidecar survives it."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert _write_meta(pkg, summary="好的說明") is True
    before = _sidecar(pkg).read_bytes()

    assert (
        tools.write_tool_meta(_version_root(pkg), {"summary": "s", "updated_at": "2026\ud800"})
        is False
    )

    assert _sidecar(pkg).read_bytes() == before
    assert not list(pkg.glob(f"{tools._AI_META_FILENAME}.*"))


# --- summary sidecar storage --------------------------------------------------


def test_store_summary_meta_outcomes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The store distinguishes a successful publish from a refused one."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    identity = tools.package_identity(_version_root(pkg))

    # ok: origin inherited from disk, the summary replaced.
    origin = {"openapi_url": "http://kb.example/o.json", "instructions": "查 KB"}
    assert _write_meta(pkg, summary="舊的", origin=origin) is True
    outcome, meta = tools.store_summary_meta(
        _version_root(pkg), summary="新的", origin=None, llm_log_id=9, expected_identity=identity
    )
    assert outcome == "ok"
    assert meta is not None
    assert meta["summary"] == "新的"
    assert meta["origin"] == {
        "source": "test-fixture",
        "openapi_url": "http://kb.example/o.json",
        "instructions": "查 KB",
        "feedback": None,
        "previous": None,
    }
    assert tools.read_tool_meta(_version_root(pkg)) == meta  # what it returned IS what it stored

    # not_stored: the write was refused (here, the ghost guard on a missing dir).
    gone = tmp_path / "nope" / "gone"
    assert tools.store_summary_meta(
        tools.VersionRoot(gone), summary="s", origin=None, llm_log_id=None, expected_identity=None
    ) == (
        "not_stored",
        None,
    )
    assert not gone.exists()


def test_store_summary_meta_stamps_the_minting_process_beside_the_log_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stored ``llm_log_id`` always carries the token of the process that minted
    it, and an absent id carries no token.

    The id is a per-process counter over a ring that dies with the process, but
    the sidecar keeps the integer forever -- so the token is the only thing that
    can later say whether the id still names the interaction it was written for.
    Stamped at the store (never passed in), so no arrangement of arguments can
    pair an id with someone else's token."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    identity = tools.package_identity(_version_root(pkg))

    outcome, meta = tools.store_summary_meta(
        _version_root(pkg), summary="說明", origin=None, llm_log_id=7, expected_identity=identity
    )
    assert outcome == "ok"
    assert meta is not None
    assert meta["llm_log_id"] == 7
    assert meta["llm_log_process"] == llm_log.process_token()

    outcome, meta = tools.store_summary_meta(
        _version_root(pkg), summary="說明", origin=None, llm_log_id=None, expected_identity=identity
    )
    assert outcome == "ok"
    assert meta is not None
    assert meta["llm_log_id"] is None
    assert meta["llm_log_process"] is None  # no id, nothing to vouch for


def test_store_summary_meta_strips_and_caps_the_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cosmetic half: edge whitespace goes, and an over-long summary is cut
    to _TOOL_SUMMARY_CAP by a bare slice -- no truncation marker, exactly as the
    validator did it before."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    identity = tools.package_identity(_version_root(pkg))

    outcome, meta = tools.store_summary_meta(
        _version_root(pkg),
        summary="  spaced  ",
        origin=None,
        llm_log_id=1,
        expected_identity=identity,
    )
    assert outcome == "ok"
    assert meta is not None
    assert meta["summary"] == "spaced"

    long_text = "y" * (tools._TOOL_SUMMARY_CAP + 500)
    outcome, meta = tools.store_summary_meta(
        _version_root(pkg), summary=long_text, origin=None, llm_log_id=1, expected_identity=identity
    )
    assert outcome == "ok"
    assert meta is not None
    assert meta["summary"] == "y" * tools._TOOL_SUMMARY_CAP


def test_store_summary_meta_redacts_before_capping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Redact while the text is WHOLE, then slice.

    The discriminating case is a secret straddling the cap edge by FEWER than
    ``_MIN_SECRET_LEN`` characters. Cutting first leaves a 4-char head that no
    later pass will ever mask -- ``write_tool_meta``'s own redaction cannot match
    a full value that is no longer there, and the trailing-fragment guard has a
    6-char floor by design. Masking while the text is whole removes the value
    before the slice can strand a piece of it."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    identity = tools.package_identity(_version_root(pkg))
    secret = "ZZTOP-live-secret-abcdef"
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))

    padding = "y" * (tools._TOOL_SUMMARY_CAP - 4)
    outcome, meta = tools.store_summary_meta(
        _version_root(pkg),
        summary=padding + secret,
        origin=None,
        llm_log_id=1,
        expected_identity=identity,
    )

    assert outcome == "ok"
    assert meta is not None
    assert secret not in meta["summary"]
    # The 4 characters a cut-first order would strand past the redactor's floor.
    assert secret[:4] not in meta["summary"]
    assert secret[:4] not in _sidecar(pkg).read_text(encoding="utf-8")


def test_store_summary_meta_redacts_before_stripping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Redact BEFORE the strip, not after.

    The redactor matches the REGISTERED value against untouched text, so a secret
    carrying edge whitespace -- a hand-edited ``.env`` with a quoted
    ``" secret-token "``, which python-dotenv keeps verbatim -- stops matching the
    moment ``strip`` eats that edge, and the rest of the value rides to disk
    unmasked. This is why the strip moved OUT of the result validator with the
    redaction rather than staying behind as a harmless tidy-up."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    identity = tools.package_identity(_version_root(pkg))
    secret = " secret-token-abcdef "
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))

    outcome, meta = tools.store_summary_meta(
        _version_root(pkg), summary=secret, origin=None, llm_log_id=1, expected_identity=identity
    )

    assert outcome == "ok"
    assert meta is not None
    assert "secret-token-abcdef" not in meta["summary"]
    assert tools._REDACTION_MARKER in meta["summary"]
    assert "secret-token-abcdef" not in _sidecar(pkg).read_text(encoding="utf-8")


def _swap_the_package_once(
    monkeypatch: pytest.MonkeyPatch, root: Path, name: str, replacement_run_py: str
) -> Callable[[], Path]:
    """Arm a same-name package swap INSIDE the sidecar writer's own steps.

    The window R6-2/R6-3 are about is not the caller's: it opens after the
    caller's last look and closes at ``os.replace``, and everything in it belongs
    to ``write_tool_meta`` -- a redactor sweep of the whole tools directory, a
    JSON encode, an mkstemp, a write, an fsync. So the swap is driven from one of
    those steps (``_redacted``, the first) rather than from a thread race, which
    makes it deterministic AND pins the window to exactly where the finding puts
    it. Delete-then-reinstall-the-same-name is the swap that costs no job slot at
    all (D40 r5 O5-3), so it is the one a summary can genuinely lose a race to.

    Fires ONCE (the writer redacts three fields), and returns an accessor for the
    replacement package."""
    root_pkg = root / name
    state: dict[str, Path] = {}
    real_redacted = tools._redacted

    def swap_then_redact(value: str | None) -> str | None:
        if "pkg" not in state:
            shutil.rmtree(root_pkg)
            state["pkg"] = _make_tool(root, name, replacement_run_py)
        return real_redacted(value)

    monkeypatch.setattr(tools, "_redacted", swap_then_redact)
    return lambda: state["pkg"]


def test_store_summary_meta_refuses_a_package_swapped_inside_the_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A's summary must not be published into a B that took A's NAME mid-write.

    The identity check used to be the last line of ``store_summary_meta``, which
    reads as "the last instant" but is not one: ``write_tool_meta`` still had a
    redactor sweep, an encode, an mkstemp, a write and an fsync ahead of it, and
    NOTHING serializes a promote or a delete against that. So a package
    swapped inside that window received A's summary AND A's origin -- which every
    later revise of B then reads back as its first-hand context.

    The check now sits on the line above ``os.replace``. What must hold: nothing
    is published, the answer is the did-not-happen one the route already maps,
    and the writer leaves no temp file behind in the package that did nothing
    wrong."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    identity = tools.package_identity(_version_root(pkg))
    replacement = _swap_the_package_once(
        monkeypatch, root, "echo", "import sys\nsys.stdout.write('B')\n"
    )

    assert tools.store_summary_meta(
        _version_root(pkg),
        summary="A 這個工具會查 KB",
        origin={"openapi_url": "http://a.example/o.json", "instructions": "A 的指示"},
        llm_log_id=7,
        expected_identity=identity,
    ) == ("not_stored", None)

    swapped = replacement()
    assert tools.package_identity(_version_root(swapped)) != identity  # the swap really happened
    assert tools.read_tool_meta(_version_root(swapped)) is None  # ... and B has no sidecar at all
    # The temp file was minted in B's directory (the swap lands before mkstemp),
    # so the refusal has to clean it up: a stray one would be scanned by every
    # later embedded-secret sweep of that package.
    assert sorted(child.name for child in swapped.iterdir()) == [
        tools._META_DIRNAME,
        "run.py",
        "tool.json",
    ]


def test_store_summary_meta_fails_closed_on_a_redaction_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A provider failure is the did-not-happen answer, never an exception: the
    regenerate route MAPS this return, so a raised error would turn a 404 into a
    500 -- and the previous sidecar must survive untouched either way."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    identity = tools.package_identity(_version_root(pkg))
    assert _write_meta(pkg, summary="舊的") is True
    before = _sidecar(pkg).read_bytes()

    def explode() -> Any:
        raise OSError("secret provider is down")

    monkeypatch.setattr(tools, "known_secret_values", explode)

    assert tools.store_summary_meta(
        _version_root(pkg), summary="新的", origin=None, llm_log_id=1, expected_identity=identity
    ) == (
        "not_stored",
        None,
    )
    assert _sidecar(pkg).read_bytes() == before


def test_sidecar_never_listed_as_a_package(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The dot-prefixed sidecar is invisible to the registry scan: it is neither
    a phantom row nor a reason for the real package to look broken."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    _write_meta(pkg, summary="s")

    listed = list_tools()
    assert [row["name"] for row in listed] == ["echo"]
    assert listed[0]["valid"] is True


def test_delete_tool_takes_the_sidecar_with_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Summary and tool share one lifetime -- the reason the metadata lives in
    the package instead of in SQLite."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    _write_meta(pkg, summary="s")
    assert _sidecar(pkg).is_file()

    assert delete_tool("echo") is True
    assert not pkg.exists()


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

    monkeypatch.setattr("afterthread.services.memory_ai.generate_structured", _fake_gen)
    return captured


@pytest.mark.parametrize("run_call, base_prompt, good", _WORKFLOW_CASES, ids=_WORKFLOW_IDS)
def test_workflow_appends_tools_rule_when_active(
    monkeypatch: pytest.MonkeyPatch,
    run_call: Any,
    base_prompt: str,
    good: dict[str, Any],
) -> None:
    captured = _capture_generate_structured(monkeypatch, good)
    monkeypatch.setattr("afterthread.services.tools.enabled_llm_tools", lambda: [_FAKE_TOOL])
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
    monkeypatch.setattr("afterthread.services.tools.enabled_llm_tools", lambda: [])
    run_call()

    assert captured["system_prompt"] == base_prompt  # byte-identical to the pinned constant
    assert captured["tools"] is None
