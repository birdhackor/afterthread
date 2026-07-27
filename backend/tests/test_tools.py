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
    before = tools.package_identity(pkg)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        manifest.write_text(raw, encoding="utf-8")
        after = tools.package_identity(pkg)
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

    The handler therefore re-reads ``.state.json`` at CALL time, and the refusal
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
    ``.state.json`` (R4) -- the same conservative direction, reached by the check
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

    real_scan = tools._scan_package

    def scan_then_toggle(directory: Path, expected_name: str | None = None) -> tools._PackageScan:
        scan = real_scan(directory, expected_name=expected_name)
        if directory.name == "zzz":
            assert set_enabled("aaa", False) is True
        return scan

    monkeypatch.setattr(tools, "_scan_package", scan_then_toggle)
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
    ``.state.json``.

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

    (pkg / tools._STATE_FILENAME).unlink()  # the documented repair, mid-conversation

    assert asyncio.run(handler({})) == tools._TOOL_DISABLED_RESULT
    assert asyncio.run(handler({})) != tools._TOOL_REPLACED_RESULT  # nothing was replaced
    assert not sentinel.exists()  # nothing was started
    # ... and the listing agrees, because both now ask the same one rule.
    assert list_tools()[0]["enabled"] is False


def test_a_deleted_state_file_still_runs_a_tool_whose_manifest_enables_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other direction of the same fallback, so the fix cannot be "refuse ABSENT".

    Answering the precedence rule means the manifest gets to say YES as well as no.
    A package whose legacy key is True, toggled (which creates the state file) and
    then stripped of that file mid-conversation, is ENABLED -- refusing it would
    take a tool away from a conversation on the strength of a file the operator is
    explicitly allowed to delete (D21)."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "legacy", "import sys\nsys.stdout.write('ok')\n", enabled=True)
    _install_tools(monkeypatch, root)
    assert set_enabled("legacy", True) is True  # migrates it: the file now exists
    assert (pkg / tools._STATE_FILENAME).is_file()
    handler = enabled_llm_tools()[0].handler

    (pkg / tools._STATE_FILENAME).unlink()

    assert asyncio.run(handler({})) == "ok"
    assert list_tools()[0]["enabled"] is True


def test_runtime_refuses_a_tool_whose_state_file_became_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The third state, at CALL time: unknown intent refuses, exactly as it lists.

    R2 already pins that an unreadable state file makes a package invalid AND
    switched off in the listing. The execution path has to reach the same answer
    through the same rule -- a tool that cannot say whether it may run must not run
    -- and it must reach it whichever way ``package_enabled`` is spelled, which is
    why this is pinned at the handler rather than only at the scan."""
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

    (pkg / tools._STATE_FILENAME).write_text("{", encoding="utf-8")  # a torn write

    assert asyncio.run(handler({})) == tools._TOOL_DISABLED_RESULT
    assert not sentinel.exists()


def test_a_reader_that_opened_the_state_file_sees_one_whole_published_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1R4-3: the property the execution path actually has, stated as a test.

    Reading ``.state.json`` LAST does NOT make the value provably current at the
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
    state_path = pkg / tools._STATE_FILENAME

    fd = os.open(state_path, os.O_RDONLY)  # the version is chosen HERE
    try:
        assert set_enabled("swapped-under", False) is True  # published mid-read
        with os.fdopen(fd, encoding="utf-8") as handle:
            fd = -1  # fdopen owns it now
            held = handle.read()
    finally:
        if fd >= 0:
            os.close(fd)

    assert json.loads(held) == {"enabled": True}  # whole, parseable, the OLD version
    fresh = tools._read_enabled_state(pkg)
    assert (fresh.present, fresh.enabled, fresh.error) == (True, False, None)


def test_a_toggle_landing_between_the_state_read_and_popen_is_still_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1R3-1: the check's ANSWER has to be as close to ``Popen`` as the check is.

    Round 2 put ``package_enabled`` on the line above ``Popen`` but spelled it as a
    whole ``_scan_package``, which reads ``.state.json`` FIRST and then parses a
    manifest, resolves the entry and stats it -- ~0.5 ms of file operations after
    the value it returns was read. A PATCH landing in that tail shipped a tool the
    operator had just switched off: the identity check above cannot help (it is
    older still) and the handler's own check is older again.

    The rule now reads the state file LAST, so on the only path where anything at
    all separates that read from the ``Popen`` -- no state file, so the manifest's
    legacy key has to be fetched -- the state is read AGAIN afterwards. That read
    is the whole fix and this drives a PATCH straight into it: the toggle is
    performed from inside ``_read_manifest_object``, which is the one step in that
    gap, and only on the SECOND call so that it lands at the ``Popen`` site rather
    than at the handler's entry check.

    Without the re-read the manifest's ``enabled: true`` wins and the child starts;
    with it the freshly published ``false`` does. The sentinel makes "nothing was
    started" a fact on disk rather than an inference from the returned string."""
    root = tmp_path / "tools"
    sentinel = tmp_path / "ran"
    pkg = _make_tool(
        root,
        "busy",
        f"import sys\nopen({str(sentinel)!r}, 'w').write('x')\nsys.stdout.write('ok')\n",
        enabled=True,  # ... and NO state file: the fallback is what answers
    )
    _install_tools(monkeypatch, root)
    assert not (pkg / tools._STATE_FILENAME).exists()
    handler = enabled_llm_tools()[0].handler
    real_read_manifest = tools._read_manifest_object
    calls: list[Path] = []

    def toggle_inside_the_gap(directory: Path) -> dict[str, Any] | None:
        calls.append(directory)
        if len(calls) == 2:  # the ``Popen`` site, not the handler's entry
            assert set_enabled("busy", False) is True
        return real_read_manifest(directory)

    monkeypatch.setattr(tools, "_read_manifest_object", toggle_inside_the_gap)

    assert asyncio.run(handler({})) == tools._TOOL_DISABLED_RESULT
    assert len(calls) == 2  # the probe really fired at the second site
    assert not sentinel.exists()  # no child was ever started


def test_the_execution_toggle_check_reads_the_state_file_last_and_scans_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1R3-1's adjacency, pinned as call ORDER because so little sits in the gap.

    The sibling test above drives a PATCH through the one step that can separate
    the toggle read from ``Popen``. On the other path -- a package that HAS a state
    file -- there is nothing to drive from, so what has to be pinned is how little
    is there: the last file the runtime READS before starting the child is
    ``.state.json``, and it does not run a package SCAN to get there.

    The last file READ, not the last thing done: since P1R4-1 the identity check
    stands between that read and ``Popen``, because only one of the two can be
    adjacent and a redirect is the worse failure (see
    ``test_the_identity_check_is_the_last_thing_before_the_subprocess_starts``,
    which pins that order). It reads nothing -- one ``lstat`` of ``tool.json`` --
    so this assertion is about READS and is exactly as strong as it was.

    Both halves matter and neither implies the other. A scan would answer the same
    question correctly (it is where the rule is defined) while re-introducing the
    ~0.5 ms of manifest parsing and entry-file resolving between the read and the
    act -- which is exactly what round 2 shipped."""
    root = tmp_path / "tools"
    _make_tool(root, "traced", "import sys\nsys.stdout.write('ok')\n")
    _install_tools(monkeypatch, root)
    assert set_enabled("traced", True) is True  # give it a state file
    handler = enabled_llm_tools()[0].handler  # advertisement scans; probes go on after

    trace: list[str] = []
    real_state = tools._read_enabled_state
    real_scan = tools._scan_package
    real_popen = subprocess.Popen

    def traced_state(directory: Path) -> tools._EnabledState:
        trace.append("state")
        return real_state(directory)

    def traced_scan(directory: Path, expected_name: str | None = None) -> tools._PackageScan:
        trace.append("scan")
        return real_scan(directory, expected_name=expected_name)

    def traced_popen(*args: Any, **kwargs: Any) -> Any:
        trace.append("popen")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(tools, "_read_enabled_state", traced_state)
    monkeypatch.setattr(tools, "_scan_package", traced_scan)
    monkeypatch.setattr(subprocess, "Popen", traced_popen)

    assert asyncio.run(handler({})) == "ok"

    assert trace[-2:] == ["state", "popen"]  # the toggle read is the LAST READ before it
    assert "scan" not in trace  # ... and getting it cost no scan at all
    assert trace.count("state") == 2  # once per site (handler entry, pre-Popen)


def test_the_scan_and_the_execution_check_answer_the_one_rule_identically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One spelling, demonstrated rather than asserted (P1R3-1).

    ``package_enabled`` stopped BEING ``_scan_package(directory).enabled``, so
    "they cannot disagree" stopped being true by construction and became true by
    both calling ``_effective_enabled`` with the reads they are already holding.
    That is only worth having if it is checked, so this walks every shape the two
    can be asked about -- the three states of the state file, both directions of
    the manifest fallback behind an ABSENT one, and each way the manifest itself
    can fail to offer a legacy key -- and requires the same answer from both.

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
    absent = _make_tool(root, "absent", "import sys\n", enabled=True)
    legacy_off = _make_tool(root, "legacy-off", "import sys\n", enabled=False)
    present_on = _make_tool(root, "present-on", "import sys\n", enabled=False)
    present_off = _make_tool(root, "present-off", "import sys\n", enabled=True)
    unreadable = _make_tool(root, "unreadable", "import sys\n", enabled=True)
    not_json = _make_tool(root, "not-json", "import sys\n", enabled=False)
    oversized = _make_tool(root, "oversized", "import sys\n", enabled=False)
    _install_tools(monkeypatch, root)
    assert set_enabled("present-on", True) is True  # state file DISAGREES with each
    assert set_enabled("present-off", False) is True
    (unreadable / tools._STATE_FILENAME).write_text("{", encoding="utf-8")
    (not_json / "tool.json").write_text("{not json", encoding="utf-8")
    (oversized / "tool.json").write_text("x" * (_MANIFEST_MAX_BYTES + 1), encoding="utf-8")
    no_manifest = root / "no-manifest"
    no_manifest.mkdir()
    # A REAL, otherwise-perfectly-enabled package, reachable only through a link --
    # so nothing but the refusal to look can produce the answer below.
    _make_tool(tmp_path, "linked-real", "import sys\n", enabled=True)
    linked = root / "linked"
    linked.symlink_to(tmp_path / "linked-real", target_is_directory=True)

    expected = {
        absent: True,  # ABSENT -> the manifest's legacy key
        legacy_off: False,  # ... in the other direction
        present_on: True,  # PRESENT wins over a manifest that disagrees
        present_off: False,
        unreadable: False,  # PRESENT-but-unreadable is disabled, never the default
        not_json: True,  # no legacy key to offer -> the same default the scan gives
        oversized: True,
        no_manifest: True,
        linked: False,  # a refusal to LOOK, answered closed on BOTH sides (P1R4-2)
    }
    for directory, answer in expected.items():
        assert tools._scan_package(directory).enabled is answer, directory
        assert tools.package_enabled(directory) is answer, directory


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

    What the toggle gives up by moving first is one ``lstat``, the identity check's
    own syscall: a switch flipped inside it starts the tool the operator turned off
    microseconds earlier -- its own code, its own contract, its own ``.env``. That
    is the smaller wrong and it is the check-then-act instant this module accepts by
    name everywhere else.

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
    real_identity = tools._still_the_expected_package
    real_popen = subprocess.Popen

    def traced_enabled(directory: Path) -> bool:
        trace.append("enabled")
        return real_enabled(directory)

    def traced_identity(directory: Path, expected: tuple[int, int, int] | None) -> bool:
        trace.append("identity")
        return real_identity(directory, expected)

    def traced_popen(*args: Any, **kwargs: Any) -> Any:
        trace.append("popen")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(tools, "package_enabled", traced_enabled)
    monkeypatch.setattr(tools, "_still_the_expected_package", traced_identity)
    monkeypatch.setattr(subprocess, "Popen", traced_popen)

    assert asyncio.run(handler({})) == "ok"

    assert trace[-3:] == ["enabled", "identity", "popen"]  # identity is the last word
    # ... and the handler's own pair is deliberately the other way round.
    assert trace == ["identity", "enabled", "enabled", "identity", "popen"]


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
    replacement = _make_tool(
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

    def swap_inside_the_gap(directory: Path) -> tools._EnabledState:
        reads.append(directory)
        if len(reads) == 2:  # the pre-``Popen`` site, not the handler's entry
            os.rename(pkg, root / ".swapped.bak-probe")
            os.rename(replacement, pkg)
        return real_state(directory)

    monkeypatch.setattr(tools, "_read_enabled_state", swap_inside_the_gap)

    assert asyncio.run(handler({})) == tools._TOOL_REPLACED_RESULT
    assert (pkg / "run.py").read_text(encoding="utf-8").endswith("'NEW')\n")  # it DID swap
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
    identity_before = tools.package_identity(pkg)
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
    assert tools.package_identity(pkg) == identity_before

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
    replacement = _make_tool(tmp_path / "staging", "aaa", "import sys\nsys.stdout.write('NEW')\n")
    _install_tools(monkeypatch, root)

    pkg = root / "aaa"
    real_scan = tools._scan_package

    def scan_then_promote(directory: Path, expected_name: str | None = None) -> tools._PackageScan:
        scan = real_scan(directory, expected_name=expected_name)
        if directory.name == "zzz":
            os.rename(pkg, root / ".aaa.bak-r7")
            os.rename(replacement, pkg)
        return scan

    monkeypatch.setattr(tools, "_scan_package", scan_then_promote)
    advertised = {tool.spec["function"]["name"]: tool.handler for tool in enabled_llm_tools()}

    assert asyncio.run(advertised["aaa"]({})) == tools._TOOL_REPLACED_RESULT
    assert (pkg / "run.py").read_text(encoding="utf-8").endswith("'NEW')\n")  # it IS the new one


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
    replacement = _make_tool(tmp_path / "staging", "aaa", "import sys\nsys.stdout.write('NEW')\n")
    _install_tools(monkeypatch, root)

    pkg = root / "aaa"
    real_read = tools._read_regular_file_capped
    swapped = False

    def read_then_promote(path: Path, cap: int) -> str | None:
        nonlocal swapped
        text = real_read(path, cap)
        if not swapped and path == pkg / "tool.json":
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
        pkg,
        {"summary": "what it does", "status": "draft", "updated_at": "2026-07-27T00:00:00+00:00"},
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
    manifest_before, directory_before = tools.package_identity(pkg), tools.directory_identity(pkg)
    assert manifest_before is not None and directory_before is not None

    assert _edit_manifest_in_place(pkg) != manifest_before  # the package "changed"
    assert tools.directory_identity(pkg) == directory_before  # the files did not move

    renamed = root / ".echo.stale-x"
    os.rename(pkg, renamed)
    assert tools.directory_identity(renamed) == directory_before  # the name moved, not the inode
    assert tools.directory_identity(pkg) is None  # ... and nothing answers for the old name

    shutil.rmtree(renamed)
    reinstalled = _make_tool(root, "echo", "import sys\nsys.stdout.write('y')\n")
    assert tools.package_identity(reinstalled) != manifest_before  # a NEW package, always
    # The directory inode is routinely REUSED here, which is exactly why the
    # question above cannot be answered with it. Asserted as "may be equal" rather
    # than "is equal" because inode allocation is the filesystem's business: the
    # claim being pinned is that this tuple does not distinguish packages.
    assert tools.directory_identity(reinstalled) is not None


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
    identity = tools.directory_identity(pkg)
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
        (pkg / ".env").write_text(f"KB_KEY={new}\n", encoding="utf-8")  # rotated mid-call
        assert tools._cached_env_values(pkg) == frozenset({new})  # the scan moved on...
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
        (pkg / ".env").unlink()  # from here only the holds can answer for this value
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

    env, _ = tools._build_tool_env(pkg)
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

    env, _ = tools._build_tool_env(pkg)
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

    env, _ = tools._build_tool_env(pkg)
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

    env, _ = tools._build_tool_env(pkg)
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
    env, _ = tools._build_tool_env(pkg)

    before = threading.active_count()
    try:
        started = time.monotonic()
        # Drive the blocking runner DIRECTLY (not via the threadpool handler) so
        # the reader/writer threads run in this thread's context and
        # active_count() is a clean before/after measure with no threadpool-worker
        # confound.
        result = tools._run_tool_subprocess(
            entry, pkg, tools.package_identity(pkg), env, "{}", 30.0, 1000
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
    env, _ = tools._build_tool_env(pkg)

    try:
        started = time.monotonic()
        result = tools._run_tool_subprocess(
            entry, pkg, tools.package_identity(pkg), env, "{}", 30.0, 1000
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
    os.mkfifo(pkg / ".env")  # a writer-less FIFO -- an ordinary read would block forever
    _install_tools(monkeypatch, root)

    box: dict[str, dict[str, str]] = {}
    worker = threading.Thread(
        target=lambda: box.__setitem__("env", tools._build_tool_env(pkg)[0]), daemon=True
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

    env_file = pkg / ".env"
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

    def counting_loader(directory: Path) -> dict[str, str]:
        parses.append(directory)
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
    before_identity = tools.package_identity(pkg)
    assert before_identity is not None

    for value in (False, False, True):
        assert set_enabled("echo", value) is True
        assert manifest.read_bytes() == before_bytes
        assert tools.package_identity(pkg) == before_identity

    # ... and the toggle really did take effect, so this is not a no-op passing by
    # doing nothing at all.
    assert list_tools()[0]["enabled"] is True
    assert set_enabled("echo", False) is True
    assert list_tools()[0]["enabled"] is False
    assert manifest.read_bytes() == before_bytes
    assert tools.package_identity(pkg) == before_identity


def test_the_state_file_wins_over_a_manifest_that_disagrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R1's precedence: ``.state.json`` present and readable is AUTHORITATIVE.

    The manifest's ``enabled`` key survives on disk deliberately -- rewriting a
    manifest to tidy away a legacy field would move the very identity this split
    exists to hold still -- so the two files can and will disagree. The state file
    is the answer, in both directions."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n", enabled=True)
    _install_tools(monkeypatch, root)

    (pkg / tools._STATE_FILENAME).write_text('{"enabled": false}', encoding="utf-8")
    assert list_tools()[0]["enabled"] is False
    assert enabled_llm_tools() == []

    (pkg / tools._STATE_FILENAME).write_text('{"enabled": true}', encoding="utf-8")
    # The manifest now says the opposite of the state file in the other direction.
    manifest = pkg / "tool.json"
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["enabled"] = False
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    assert list_tools()[0]["enabled"] is True
    assert len(enabled_llm_tools()) == 1


def test_a_package_with_no_state_file_falls_back_to_its_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R1's migration, and it is the whole of the migration: there is no pass.

    A package installed before web-v5 P1 has ``enabled`` in its ``tool.json`` and
    no ``.state.json``. It keeps reporting exactly what it reported before, and --
    the part that matters -- the READ does not write: the state file is still
    absent afterwards, so a scan can never mutate a package. The first toggle is
    what migrates it, and the manifest is left alone even then."""
    root = tmp_path / "tools"
    off = _make_tool(root, "off", "import sys\nsys.stdout.write('x')\n", enabled=False)
    on = _make_tool(root, "on", "import sys\nsys.stdout.write('x')\n", enabled=True)
    _install_tools(monkeypatch, root)

    listed = {t["name"]: t for t in list_tools()}
    assert listed["off"]["enabled"] is False
    assert listed["on"]["enabled"] is True
    assert [t.spec["function"]["name"] for t in enabled_llm_tools()] == ["on"]
    # Reads only: neither package grew a state file, in either direction.
    assert not (off / tools._STATE_FILENAME).exists()
    assert not (on / tools._STATE_FILENAME).exists()

    # The FIRST toggle is the migration, and it adds a file rather than editing one.
    manifest_before = (off / "tool.json").read_bytes()
    assert set_enabled("off", True) is True
    assert json.loads((off / tools._STATE_FILENAME).read_text(encoding="utf-8")) == {
        "enabled": True
    }
    assert (off / "tool.json").read_bytes() == manifest_before  # legacy key left in place
    assert json.loads(manifest_before)["enabled"] is False  # ... and still saying the old thing
    assert {t["name"]: t["enabled"] for t in list_tools()}["off"] is True


@pytest.mark.parametrize(
    "content",
    [
        "",  # truncated to nothing
        "{",  # a torn write
        "null",  # legal JSON, not an object
        "[]",
        "{}",  # an object with no answer in it
        '{"enabled": "yes"}',  # an answer that is not a bool
        '{"enabled": 1}',
        "x" * (tools._STATE_MAX_BYTES + 1),  # past the cap
    ],
)
def test_an_unreadable_state_file_disables_rather_than_defaulting_to_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str
) -> None:
    """R2: ABSENT and UNREADABLE are different questions, answered differently.

    Absent is the ordinary pre-migration case and falls back to the manifest. A
    file that EXISTS but cannot be read as our shape means the operator's intent is
    unknown -- and defaulting that to ``enabled: True`` would hand the model a tool
    somebody deliberately switched off, which is the one direction this subsystem
    never errs in.

    Both halves of the answer are pinned: the row is INVALID with the listing's own
    ``error`` channel carrying why (so the operator is told, rather than watching a
    tool silently vanish from the model's reach), AND ``enabled`` is False -- so
    ``enabled_llm_tools``' ``valid AND enabled`` filter refuses on either half
    alone. The manifest here says ``enabled: true``, so a fallback would have
    advertised it."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n", enabled=True)
    (pkg / tools._STATE_FILENAME).write_text(content, encoding="utf-8")
    _install_tools(monkeypatch, root)

    listed = list_tools()[0]
    assert listed["valid"] is False
    assert listed["enabled"] is False
    assert listed["error"] == tools._STATE_UNREADABLE_ERROR
    assert enabled_llm_tools() == []

    # Repairable through the API without a delete: the toggle does not READ this
    # file, so it publishes a clean one straight over it.
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
    (pkg / tools._STATE_FILENAME).mkdir()
    _install_tools(monkeypatch, root)

    state = tools._read_enabled_state(pkg)
    assert state.present is True  # NOT absent
    assert state.enabled is False and state.error is not None
    assert enabled_llm_tools() == []


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
        assert tools._cached_env_values(pkg) == frozenset()  # its ``.env`` went with it
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
    before = tools.package_identity(pkg)

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
    assert (remains[0] / "data.txt").exists()  # deferred, not destroyed


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
    manifest_before = tools.package_identity(pkg)
    directory_before = tools.directory_identity(pkg)
    manifest_bytes = (pkg / "tool.json").read_bytes()

    result: dict[str, str] = {}
    caller = threading.Thread(target=lambda: result.update(out=asyncio.run(handler({}))))
    caller.start()
    try:
        _wait_for(marker.exists)  # the CHILD is running, not merely queued
        assert set_enabled("toggled", False) is True
        assert tools.package_identity(pkg) == manifest_before
        assert tools.directory_identity(pkg) == directory_before
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

    def delete_then_build(directory: Path) -> tuple[dict[str, str], frozenset[str]]:
        assert delete_tool("busy") is True
        return real_build_env(directory)

    monkeypatch.setattr(tools, "_build_tool_env", delete_then_build)

    assert asyncio.run(handler({})) == tools._TOOL_REPLACED_RESULT

    # The delete DEFERRED: it found this call already registered, which it could
    # only do if the registration preceded the ``.env`` read it was driven from.
    remains = [child for child in root.iterdir()]
    assert len(remains) == 1 and tools._STALE_BACKUP_RE.match(remains[0].name)
    assert (remains[0] / "run.py").exists()  # deferred, not destroyed
    assert not pkg.exists()  # ... and the NAME went at once, as the route promises


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

    def toggle_then_build(directory: Path) -> tuple[dict[str, str], frozenset[str]]:
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

    def delete_state_then_build(directory: Path) -> tuple[dict[str, str], frozenset[str]]:
        (pkg / tools._STATE_FILENAME).unlink()
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
    package's own ``.state.json``. The package stays invalid for the symlinked
    manifest (so it is never advertised or executed either way), and an operator
    can now switch a broken package OFF -- which is exactly when they want to."""
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

    assert set_enabled("echo", False) is True
    assert outside.read_text() == original  # foreign file untouched (never opened)
    assert (pkg / tools._STATE_FILENAME).is_file()  # the toggle went to its OWN file
    listed = {t["name"]: t for t in list_tools()}["echo"]
    assert listed["valid"] is False and listed["enabled"] is False


def test_set_enabled_refuses_a_symlinked_state_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The write-boundary refusal, moved to the file the toggle actually writes.

    ``.state.json`` is now the only thing a toggle touches, so it inherits the
    hazard: a symlink planted at that name (by an unjailed builder, by an
    operator) must not carry the write out of the package. The publish's pre-write
    ``lstat`` refuses any non-regular target outright -- and even reaching
    ``os.replace`` would only have replaced the LINK -- so the foreign file is
    untouched and the toggle honestly reports "did not happen"."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    (pkg / tools._STATE_FILENAME).symlink_to(outside)
    _install_tools(monkeypatch, root)

    assert set_enabled("echo", False) is False
    assert outside.read_text(encoding="utf-8") == "keep"  # never written through
    assert (pkg / tools._STATE_FILENAME).is_symlink()  # the link itself survives
    # And the READ side agrees: a non-regular state file is UNREADABLE, which is
    # a disabled, invalid row -- never a silent fallback to "on".
    listed = {t["name"]: t for t in list_tools()}["echo"]
    assert listed["valid"] is False and listed["enabled"] is False
    assert listed["error"] == tools._STATE_UNREADABLE_ERROR


def test_set_enabled_publishes_under_the_state_publish_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The toggle's half of the mutual exclusion (web-v5 P1, R1-1).

    A revise's swap replaces the WHOLE package directory, so it carries the live
    toggle into staging first -- and a PATCH landing between that read and the
    rename is silently REVERTED, with both operations reporting success. Since P1 a
    toggle no longer moves the manifest identity, so the swap's identity re-check
    (which used to refuse that case by accident) passes and ships the stale value.
    Ordering cannot close the window on its own, so both sides take
    ``_STATE_PUBLISH_LOCK``; this pins THIS side, and
    ``test_a_toggle_arriving_during_the_swap_waits_for_it_and_still_wins`` pins the
    swap's.

    Asserted from inside the publish rather than by racing the swap:
    ``acquire(blocking=False)`` fails on a held non-reentrant lock even for its own
    holder, so the hold is measured in one thread. The RESOLVE stays outside the
    hold deliberately -- a toggle that resolves before a swap and publishes after
    one lands on the package that now owns the name, which is the right answer."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n", enabled=True)
    _install_tools(monkeypatch, root)
    real_write = tools.write_package_state
    held: list[bool] = []

    def write_and_report_the_hold(directory: Path, enabled: bool, **kwargs: Any) -> bool:
        held.append(not tools._STATE_PUBLISH_LOCK.acquire(blocking=False))
        return real_write(directory, enabled, **kwargs)

    monkeypatch.setattr(tools, "write_package_state", write_and_report_the_hold)

    assert set_enabled("echo", False) is True

    assert held == [True]  # the write happened INSIDE the hold, not beside it
    assert tools._STATE_PUBLISH_LOCK.acquire(blocking=False) is True  # released again
    tools._STATE_PUBLISH_LOCK.release()
    assert json.loads((pkg / tools._STATE_FILENAME).read_text(encoding="utf-8")) == {
        "enabled": False
    }


def test_a_publish_that_wakes_to_a_symlinked_package_directory_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1R3-2: the containment re-check P1 retired was not structurally replaced.

    P1 dropped ``set_enabled``'s write-boundary containment check on the grounds
    that the publisher's own pre-write ``lstat`` enforces it. That is true of the
    FINAL component and false of every ANCESTOR: ``lstat`` does not follow a
    symlinked ``.state.json``, but it -- like ``mkstemp(dir=...)`` and
    ``os.replace`` -- follows the directories above it. The resolved path is a
    STRING re-interpreted at each of those syscalls, and ``set_enabled`` resolves
    it BEFORE waiting on ``_STATE_PUBLISH_LOCK``, a wait that can last a whole
    revise tail.

    Driven at exactly that instant rather than by racing a revise: the lock is
    replaced by a context manager that performs the swap as it is entered, which is
    "the directory was replaced while this toggle waited" with no timing in it.

    What must hold is BOTH halves -- the toggle reports "did not happen" (the route
    turns that into a 404) and the link's target is left without so much as a temp
    file in it. The hazard itself is 裁決紀錄 #5's class (an actor who can plant
    that symlink already runs as the service uid), which is why this test exists
    for the JUSTIFICATION rather than for the threat: a retired guard whose stated
    replacement does not exist is what gets budgeted for and is not there.

    The legitimate re-interpretation this must NOT break -- a toggle that wakes
    after a real revise swap and lands on the newly published package -- is pinned
    by ``test_a_toggle_arriving_during_the_swap_waits_for_it_and_still_wins`` in
    test_tool_builder.py, against the real swap rather than a stand-in."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n", enabled=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _install_tools(monkeypatch, root)
    aside = root / ".moved-aside"
    real_lock = tools._STATE_PUBLISH_LOCK

    class _SwapWhileTheToggleWaits:
        def __enter__(self) -> None:
            os.rename(pkg, aside)  # the package the toggle resolved, moved away
            pkg.symlink_to(elsewhere, target_is_directory=True)
            real_lock.acquire()

        def __exit__(self, *_exc: object) -> None:
            real_lock.release()

    monkeypatch.setattr(tools, "_STATE_PUBLISH_LOCK", _SwapWhileTheToggleWaits())

    assert set_enabled("echo", False) is False

    assert list(elsewhere.iterdir()) == []  # nothing written THROUGH the link
    assert not (aside / tools._STATE_FILENAME).exists()  # nor into the real package
    assert (root / "echo").is_symlink()  # the planted link is untouched too
    assert real_lock.acquire(blocking=False) is True  # and the hold was released
    real_lock.release()


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
    (pkg / tools._STATE_FILENAME).mkdir()
    (pkg / tools._STATE_FILENAME / "keep.txt").write_text("operator's", encoding="utf-8")
    _install_tools(monkeypatch, root)

    assert set_enabled("echo", False) is False  # what the route turns into a 404
    assert set_enabled("echo", True) is False  # neither direction repairs it

    entry = pkg / tools._STATE_FILENAME
    assert entry.is_dir() and (entry / "keep.txt").read_text(encoding="utf-8") == "operator's"
    assert sorted(child.name for child in pkg.iterdir()) == [
        tools._STATE_FILENAME,
        "run.py",
        "tool.json",
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
    assert (root / "real" / "tool.json").is_file()
    assert (root / "real" / "run.py").is_file()
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
    _make_tool(root, "real", "import sys\nsys.stdout.write('x')\n", enabled=True)
    (root / "alias").symlink_to(root / "real", target_is_directory=True)
    _install_tools(monkeypatch, root)

    before = (root / "real" / "tool.json").read_bytes()
    assert set_enabled("alias", False) is False
    assert (root / "real" / "tool.json").read_bytes() == before  # real manifest untouched
    assert not (root / "real" / tools._STATE_FILENAME).exists()  # and no state written
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
    assert set_enabled("big", False) is True
    assert (root / "big" / "tool.json").read_bytes() == before  # untouched
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
    ``.state.json`` is refused by the publish's ``lstat`` S_ISREG gate (and by the
    read side's, which is why the row below reports unreadable), neither of which
    can block. Both halves run on a WATCHED daemon thread so a regression fails
    LOUDLY here instead of wedging the whole suite."""
    root = tmp_path / "tools"
    pkg = root / "fifotool"
    pkg.mkdir(parents=True)
    (pkg / "run.py").write_text("import sys\nsys.stdout.write('x')\n", encoding="utf-8")
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

    fifo_state = root / "fifostate"
    _make_tool(root, "fifostate", "import sys\nsys.stdout.write('x')\n")
    (fifo_state / tools._STATE_FILENAME).unlink(missing_ok=True)
    os.mkfifo(fifo_state / tools._STATE_FILENAME)

    assert toggle("fifostate") is False  # the publish refuses a non-regular target
    assert (fifo_state / tools._STATE_FILENAME).is_fifo()
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
    monkeypatch.setattr("afterthread.services.tools.get_settings", lambda: Settings(tools_dir=""))
    assert tools_dir() is None
    assert list_tools() == []
    assert enabled_llm_tools() == []
    assert set_enabled("x", True) is False
    assert delete_tool("x") is False


# --- AI summary sidecar (D40) ----------------------------------------------


def _sidecar(pkg: Path) -> Path:
    return pkg / tools._AI_META_FILENAME


def _write_meta(pkg: Path, **fields: Any) -> bool:
    """``write_tool_meta`` with the ``updated_at`` every real caller supplies.

    The writer REFUSES a meta without a string ``updated_at`` (it will not invent
    a timestamp on a caller's behalf -- see write_tool_meta), and both production
    callers stamp their own. So tests state the fields actually under test and
    inherit a valid stamp here, instead of repeating a timestamp literal
    everywhere or -- worse -- passing a shape no caller ever passes and getting a
    False that hides the reason the test meant to exercise."""
    return tools.write_tool_meta(pkg, {"updated_at": "2026-01-01T00:00:00+00:00", **fields})


def test_tool_meta_round_trips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """write_tool_meta -> read_tool_meta returns the same dict, and the sidecar
    is a real file inside the package (so delete_tool's rmtree takes it)."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)

    meta = {
        "summary": "這個工具會查 KB",
        "status": "draft",
        "updated_at": "2026-07-26T00:00:00+00:00",
        "llm_log_id": 12,
        # Carried through verbatim, never re-derived: set_summary_status's
        # round-trip must keep a token minted by an EARLIER process (see
        # write_tool_meta).
        "llm_log_process": "process-token-from-whoever-wrote-this",
        "origin": {"openapi_url": "http://kb.example/openapi.json", "instructions": "build"},
    }
    assert tools.write_tool_meta(pkg, meta) is True
    assert _sidecar(pkg).is_file()
    assert tools.read_tool_meta(pkg) == meta


def test_read_tool_meta_degrades_on_missing_corrupt_and_non_object(tmp_path: Path) -> None:
    """Every unusable sidecar reads as "no metadata", never an exception: a
    corrupt one must empty the summary panel, not break the tools list."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert tools.read_tool_meta(pkg) is None  # absent

    _sidecar(pkg).write_text("{not json", encoding="utf-8")
    assert tools.read_tool_meta(pkg) is None  # unparseable

    _sidecar(pkg).write_text('["a list"]', encoding="utf-8")
    assert tools.read_tool_meta(pkg) is None  # valid JSON, wrong shape


def test_read_tool_meta_refuses_oversized_sidecar(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    padding = "x" * (_AI_META_MAX_BYTES + 100)
    _sidecar(pkg).write_text(json.dumps({"summary": padding}), encoding="utf-8")
    assert tools.read_tool_meta(pkg) is None


def test_read_tool_meta_survives_pathological_nesting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deeply nested sidecar exhausts the stack INSIDE json.loads, which raises
    RecursionError -- not the ValueError the parse guard used to catch alone.

    It is not the summary panel that pays for that: ``summary_status`` runs this
    once per package on every ``list_tools`` scan, so one hand-edited (or
    malicious) sidecar escaping as an exception 500s the whole 工具 page and
    takes every OTHER tool's row down with it. Degrades to None like any other
    unusable sidecar, and the row still lists."""
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

    assert tools.read_tool_meta(pkg) is None
    assert tools.summary_status(pkg) is None
    listed = list_tools()
    assert [(row["name"], row["summary_status"]) for row in listed] == [("echo", None)]


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
    stored = tools.read_tool_meta(pkg)
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
    stored = tools.read_tool_meta(pkg)
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
    for key in ("summary", "status", "origin", "instructions", "updated_at"):
        monkeypatch.setattr(tools, "known_secret_values", lambda key=key: frozenset({key}))
        assert (
            _write_meta(
                pkg,
                summary="這個工具會查 KB",
                status="final",
                llm_log_id=7,
                origin={"openapi_url": "http://kb.example/o.json", "instructions": "查 KB"},
            )
            is True
        )
        stored = tools.read_tool_meta(pkg)
        assert stored is not None, f"a secret equal to the key {key!r} broke the schema"
        assert stored["summary"] == "這個工具會查 KB"
        assert stored["status"] == "final"
        assert stored["llm_log_id"] == 7
        assert stored["origin"]["openapi_url"] == "http://kb.example/o.json"
        assert tools.summary_status(pkg) == "final"


def test_write_tool_meta_drops_unknown_keys_and_containers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sidecar IS the six schema fields; anything else a caller (or a
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
    stored = tools.read_tool_meta(pkg)
    assert stored is not None
    assert set(stored) == {
        "summary",
        "status",
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
        ({}, {"openapi_url": None, "instructions": None}),
        ({"openapi_url": 12, "junk": "dropped"}, {"openapi_url": None, "instructions": None}),
        (
            {"openapi_url": "http://kb.example/o.json", "instructions": "查 KB", "junk": "dropped"},
            {"openapi_url": "http://kb.example/o.json", "instructions": "查 KB"},
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
    stored = tools.read_tool_meta(pkg)
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
    ``{"summary": null}`` used to become a finalizable nothing)."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for bad in ({"nested": "object"}, 12, ["a"], True):
        assert _write_meta(pkg, summary=bad) is False
        assert not _sidecar(pkg).exists()

    assert _write_meta(pkg, summary=None) is True
    stored = tools.read_tool_meta(pkg)
    assert stored is not None
    assert stored["summary"] == ""


def test_write_tool_meta_requires_a_string_updated_at(tmp_path: Path) -> None:
    """Every real caller stamps its own, so a missing/out-of-shape one is a
    caller bug -- and inventing a timestamp on their behalf would put a fact in
    the file that nothing actually observed."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert tools.write_tool_meta(pkg, {"summary": "s"}) is False
    assert tools.write_tool_meta(pkg, {"summary": "s", "updated_at": 12}) is False
    assert not _sidecar(pkg).exists()


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("status", "published", "draft"),
        ("status", ["draft"], "draft"),
        ("status", None, "draft"),
        ("status", "final", "final"),
        ("llm_log_id", "three", None),
        ("llm_log_id", True, None),  # bool is an int subclass; never a log link
        ("llm_log_id", 7, 7),
    ],
    ids=[
        "unknown-status",
        "wrong-type-status",
        "absent-status",
        "final",
        "str-id",
        "bool-id",
        "id",
    ],
)
def test_write_tool_meta_coerces_the_scalar_fields(
    tmp_path: Path, field: str, value: Any, expected: Any
) -> None:
    """An unknown status must never survive a write (``summary_status`` already
    refuses to trust one, so persisting it only keeps a dead value alive), and a
    non-int ``llm_log_id`` must never render as a link."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert _write_meta(pkg, summary="s", **{field: value}) is True
    stored = tools.read_tool_meta(pkg)
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

    # The worst payload the install schema admits: a max-length instructions of
    # characters that each escape to 6, plus a max-length CJK summary.
    legal = _write_meta(
        pkg,
        summary="說" * 8_000,
        origin={"openapi_url": "http://kb.example/o.json", "instructions": "\x01" * 20_000},
    )
    assert legal is True
    serialized = _sidecar(pkg).read_text(encoding="utf-8")
    # Past the OLD cap on BOTH counts -- the bytes it is named for and the chars
    # the reader actually compares -- so the old reader answered None for it.
    assert len(serialized) > _MANIFEST_MAX_BYTES
    assert len(serialized.encode("utf-8")) > _MANIFEST_MAX_BYTES
    stored = tools.read_tool_meta(pkg)
    assert stored is not None
    assert stored["summary"] == "說" * 8_000
    assert stored["origin"]["instructions"] == "\x01" * 20_000

    # Past the shared cap the WRITER refuses, so the reader is never handed a
    # file it would have to answer None for.
    assert _write_meta(pkg, summary="x" * (_AI_META_MAX_BYTES + 10)) is False
    assert tools.read_tool_meta(pkg) == stored  # the previous sidecar is untouched


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
    caller was told False -- "nothing happened" -- while a FINALIZED summary the
    operator explicitly froze had been truncated to nothing, and the sidecar is
    the ONLY copy of both that summary and the install's ``origin``. Writing to a
    temp file and publishing with ``os.replace`` means the old content survives
    every failure mode, and the file is never observable half-written.

    The failure is injected at ``os.replace`` -- the last step, after the temp
    file is fully written and fsynced -- because that is the strictest version of
    the claim: even a failure at the very END leaves the previous file intact."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert _write_meta(pkg, summary="定版的說明", status="final") is True
    before = _sidecar(pkg).read_bytes()

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    assert _write_meta(pkg, summary="新的說明") is False

    assert _sidecar(pkg).read_bytes() == before  # byte-identical, not truncated
    # ... and nothing was left lying around in the package: a stray temp file
    # would be scanned by every later validate_package embedded-secret sweep.
    assert not list(pkg.glob(f"{tools._AI_META_FILENAME}.*"))
    assert sorted(child.name for child in pkg.iterdir()) == [tools._AI_META_FILENAME]


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
    pkg.mkdir()
    synced: list[tuple[bool, int]] = []
    real_fsync = os.fsync

    def watched(fd: int) -> None:
        info = os.fstat(fd)
        synced.append((stat.S_ISDIR(info.st_mode), info.st_ino))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", watched)

    assert tools.write_package_state(pkg, False) is True
    assert synced[0][0] is False  # the temp FILE's contents first ...
    assert synced[-1] == (True, pkg.stat().st_ino)  # ... then the name that flipped

    synced.clear()
    assert _write_meta(pkg, summary="說明") is True
    assert synced[-1] == (True, pkg.stat().st_ino)  # the sidecar publish too


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
    pkg.mkdir()
    real_fsync = os.fsync

    def refuse_directories(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(22, "Invalid argument")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", refuse_directories)

    assert tools.write_package_state(pkg, False) is True  # the truth, not a 404
    assert json.loads((pkg / tools._STATE_FILENAME).read_text(encoding="utf-8")) == {
        "enabled": False
    }
    assert not list(pkg.glob(f"{tools._STATE_FILENAME}.*"))  # no temp file orphaned


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
    previous = os.umask(0o277)  # strips owner write AND every group/other bit
    try:
        assert _write_meta(pkg, summary="總結") is True
    finally:
        os.umask(previous)

    assert stat.S_IMODE(os.stat(_sidecar(pkg)).st_mode) & 0o600 == 0o600
    assert tools.read_tool_meta(pkg) is not None  # the invariant this exists for


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
    assert tools.read_tool_meta(pkg) is not None


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
    # ensure_ascii=True: this is what a hand-edit looks like on disk -- six ASCII
    # characters, a perfectly valid JSON file.
    _sidecar(pkg).write_text(
        json.dumps(
            {
                "summary": "a\ud800b",
                "status": "draft",
                "updated_at": "2026-01-01T00:00:00+00:00\udfff",
                "llm_log_id": 3,
                "origin": {"openapi_url": "http://kb.example/\ud800.json", "instructions": None},
            }
        ),
        encoding="utf-8",
    )

    meta = tools.read_tool_meta(pkg)
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

    stored = tools.read_tool_meta(pkg)
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

    assert tools.write_tool_meta(pkg, {"summary": "s", "updated_at": "2026\ud800"}) is False

    assert _sidecar(pkg).read_bytes() == before
    assert not list(pkg.glob(f"{tools._AI_META_FILENAME}.*"))


# --- compound sidecar operations are serialized (_META_LOCK, D40 r3) ----------


def test_set_summary_status_and_store_summary_meta_serialize(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A thread hammer over the two compound sidecar operations.

    Both are read-check-write sequences and both run on THREADPOOL workers in
    production (every route hops through run_in_threadpool), so they genuinely
    execute in parallel here too. What this pins is the OBSERVABLE file: under
    real contention the sidecar is always a complete, legal sidecar with a legal
    status, every operation returns rather than raising, and no temp file is left
    behind.

    Scope, stated precisely because the two guarantees are easy to conflate: the
    watcher thread is a TORN-WRITE detector and it is the ATOMIC publish
    (``os.replace``) that satisfies it -- reverting to the old truncate-in-place
    writer makes this fail with a JSONDecodeError on an empty read, verified.
    It does NOT by itself prove mutual exclusion; a lost update leaves a
    perfectly legal file. The deterministic proof that ``_META_LOCK`` serializes
    a finalize against a store lives in test_tool_meta.py
    (``test_regenerate_summary_cannot_undo_a_finalize_holding_the_lock``)."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    identity = tools.package_identity(pkg)
    assert _write_meta(pkg, summary="說明", status="draft") is True

    errors: list[BaseException] = []
    stop = threading.Event()

    def flipper() -> None:
        try:
            for index in range(20):
                tools.set_summary_status("echo", "final" if index % 2 else "draft")
        except BaseException as exc:  # reported to the main thread, never swallowed
            errors.append(exc)

    def storer() -> None:
        try:
            for index in range(20):
                tools.store_summary_meta(
                    pkg,
                    summary=f"生成 {index}",
                    origin=None,
                    llm_log_id=index,
                    expected_identity=identity,
                )
        except BaseException as exc:  # reported to the main thread, never swallowed
            errors.append(exc)

    def reader() -> None:
        # The torn-write detector: every observation of the sidecar must be a
        # complete, parseable file with a legal status -- never a prefix.
        try:
            while not stop.is_set():
                raw = _sidecar(pkg).read_text(encoding="utf-8")
                parsed = json.loads(raw)
                assert parsed["status"] in tools._SUMMARY_STATUSES
                assert isinstance(parsed["summary"], str)
        except BaseException as exc:  # reported to the main thread, never swallowed
            errors.append(exc)

    workers = [threading.Thread(target=flipper), threading.Thread(target=storer)]
    watcher = threading.Thread(target=reader)
    watcher.start()
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert not worker.is_alive()
    stop.set()
    watcher.join(timeout=30)
    assert not watcher.is_alive()
    assert not errors, errors

    # Whatever order they landed in, the file is a legal sidecar.
    final = tools.read_tool_meta(pkg)
    assert final is not None
    assert final["status"] in tools._SUMMARY_STATUSES
    assert not list(pkg.glob(f"{tools._AI_META_FILENAME}.*"))


def test_store_summary_meta_outcomes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The three outcome codes tool_meta maps onto dict / StoreRefusal / None."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    identity = tools.package_identity(pkg)

    # ok: status and origin inherited from disk, the summary replaced.
    origin = {"openapi_url": "http://kb.example/o.json", "instructions": "查 KB"}
    assert _write_meta(pkg, summary="舊的", status="draft", origin=origin) is True
    outcome, meta = tools.store_summary_meta(
        pkg, summary="新的", origin=None, llm_log_id=9, expected_identity=identity
    )
    assert outcome == "ok"
    assert meta is not None
    assert meta["summary"] == "新的"
    assert meta["status"] == "draft"
    assert meta["origin"] == origin
    assert tools.read_tool_meta(pkg) == meta  # what it returned IS what it stored

    # finalized: nothing is written, and the answer is NOT the failure code.
    assert tools.set_summary_status("echo", "final") == "ok"
    before = _sidecar(pkg).read_bytes()
    assert tools.store_summary_meta(
        pkg, summary="更新的", origin=None, llm_log_id=1, expected_identity=identity
    ) == (
        "finalized",
        None,
    )
    assert _sidecar(pkg).read_bytes() == before

    # not_stored: the write was refused (here, the ghost guard on a missing dir).
    gone = tmp_path / "nope" / "gone"
    assert tools.store_summary_meta(
        gone, summary="s", origin=None, llm_log_id=None, expected_identity=None
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
    identity = tools.package_identity(pkg)

    outcome, meta = tools.store_summary_meta(
        pkg, summary="說明", origin=None, llm_log_id=7, expected_identity=identity
    )
    assert outcome == "ok"
    assert meta is not None
    assert meta["llm_log_id"] == 7
    assert meta["llm_log_process"] == llm_log.process_token()

    outcome, meta = tools.store_summary_meta(
        pkg, summary="說明", origin=None, llm_log_id=None, expected_identity=identity
    )
    assert outcome == "ok"
    assert meta is not None
    assert meta["llm_log_id"] is None
    assert meta["llm_log_process"] is None  # no id, nothing to vouch for


def test_set_summary_status_keeps_a_foreign_process_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """定版 / 解除定版 carries the id AND its token through untouched.

    The round-trip rebuilds the sidecar from what it just READ, so a summary
    generated before a restart keeps its (now foreign) token instead of being
    re-stamped as current -- re-stamping would forge freshness onto a stale id,
    which is precisely the confusion the token exists to prevent."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    monkeypatch.setattr(tools, "_resolve_package_dir_no_alias", lambda _name: pkg)

    assert (
        _write_meta(pkg, summary="上一個行程寫的", llm_log_id=4, llm_log_process="an-older-process")
        is True
    )

    assert tools.set_summary_status("echo", "final") == "ok"

    stored = tools.read_tool_meta(pkg)
    assert stored is not None
    assert stored["status"] == "final"
    assert stored["llm_log_id"] == 4
    assert stored["llm_log_process"] == "an-older-process"
    assert stored["llm_log_process"] != llm_log.process_token()


# The SUMMARY's redact -> strip -> cap, at its one choke point (D40 r4). The
# three properties below used to be pinned on ToolSummaryResult's pydantic
# validator, which ran them on the EVENT LOOP (the redaction sweeps the tools
# directory). They moved here as one ordered operation -- splitting them would
# have put the strip before the redaction, which is itself a leak.


def test_store_summary_meta_strips_and_caps_the_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cosmetic half: edge whitespace goes, and an over-long summary is cut
    to _TOOL_SUMMARY_CAP by a bare slice -- no truncation marker, exactly as the
    validator did it before."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    identity = tools.package_identity(pkg)

    outcome, meta = tools.store_summary_meta(
        pkg, summary="  spaced  ", origin=None, llm_log_id=1, expected_identity=identity
    )
    assert outcome == "ok"
    assert meta is not None
    assert meta["summary"] == "spaced"

    long_text = "y" * (tools._TOOL_SUMMARY_CAP + 500)
    outcome, meta = tools.store_summary_meta(
        pkg, summary=long_text, origin=None, llm_log_id=1, expected_identity=identity
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
    identity = tools.package_identity(pkg)
    secret = "ZZTOP-live-secret-abcdef"
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))

    padding = "y" * (tools._TOOL_SUMMARY_CAP - 4)
    outcome, meta = tools.store_summary_meta(
        pkg,
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
    identity = tools.package_identity(pkg)
    secret = " secret-token-abcdef "
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))

    outcome, meta = tools.store_summary_meta(
        pkg, summary=secret, origin=None, llm_log_id=1, expected_identity=identity
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
    NOTHING serializes a promote or a delete against that (``_META_LOCK`` is a
    sidecar-file lock, and neither of those touches the sidecar). So a package
    swapped inside that window received A's summary AND A's origin -- which every
    later revise of B then reads back as its first-hand context.

    The check now sits on the line above ``os.replace``. What must hold: nothing
    is published, the answer is the did-not-happen one the route already maps,
    and the writer leaves no temp file behind in the package that did nothing
    wrong."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    identity = tools.package_identity(pkg)
    replacement = _swap_the_package_once(
        monkeypatch, root, "echo", "import sys\nsys.stdout.write('B')\n"
    )

    assert tools.store_summary_meta(
        pkg,
        summary="A 這個工具會查 KB",
        origin={"openapi_url": "http://a.example/o.json", "instructions": "A 的指示"},
        llm_log_id=7,
        expected_identity=identity,
    ) == ("not_stored", None)

    swapped = replacement()
    assert tools.package_identity(swapped) != identity  # the swap really happened
    assert tools.read_tool_meta(swapped) is None  # ... and B has no sidecar at all
    # The temp file was minted in B's directory (the swap lands before mkstemp),
    # so the refusal has to clean it up: a stray one would be scanned by every
    # later embedded-secret sweep of that package.
    assert sorted(child.name for child in swapped.iterdir()) == ["run.py", "tool.json"]


def test_set_summary_status_cannot_finalize_onto_a_package_swapped_inside_the_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other official API call in that window: a PATCH must not freeze A's
    meta onto B.

    ``set_summary_status`` read A's sidecar, and the rewrite it built from it went
    to whatever package answered to the name by the time the bytes landed -- so a
    revise (or a delete + same-name reinstall) landing between the read and the
    publish left B holding A's text, A's origin and ``status: "final"``. B's own
    summary hook then REFUSES to update a finalized sidecar, so the wrong
    explanation is frozen in front of the right implementation until someone
    thinks to un-finalize it.

    The identity is captured at the resolve and checked above ``os.replace``; the
    refusal folds into the ``"not_found"`` this function already answers for a
    write that did not happen."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    assert _write_meta(pkg, summary="A 的說明", status="draft") is True
    replacement = _swap_the_package_once(
        monkeypatch, root, "echo", "import sys\nsys.stdout.write('B')\n"
    )

    assert tools.set_summary_status("echo", "final") == "not_found"

    swapped = replacement()
    assert tools.read_tool_meta(swapped) is None  # B was never written into
    assert tools.summary_status(swapped) is None  # ... and certainly never frozen


def test_store_summary_meta_fails_closed_on_a_redaction_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A provider failure is the did-not-happen answer, never an exception: the
    regenerate route MAPS this return, so a raised error would turn a 404 into a
    500 -- and the previous sidecar must survive untouched either way."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    identity = tools.package_identity(pkg)
    assert _write_meta(pkg, summary="舊的", status="draft") is True
    before = _sidecar(pkg).read_bytes()

    def explode() -> Any:
        raise OSError("secret provider is down")

    monkeypatch.setattr(tools, "known_secret_values", explode)

    assert tools.store_summary_meta(
        pkg, summary="新的", origin=None, llm_log_id=1, expected_identity=identity
    ) == (
        "not_stored",
        None,
    )
    assert _sidecar(pkg).read_bytes() == before

    # ... and a FINALIZED package still answers "finalized", not "not_stored":
    # the redaction runs after the freeze gate, so "you cannot do this" (409)
    # keeps outranking "it did not work" (404) even with the provider down.
    monkeypatch.setattr(tools, "known_secret_values", frozenset)  # restore, to finalize
    assert tools.set_summary_status("echo", "final") == "ok"
    monkeypatch.setattr(tools, "known_secret_values", explode)
    assert tools.store_summary_meta(
        pkg, summary="新的", origin=None, llm_log_id=1, expected_identity=identity
    ) == (
        "finalized",
        None,
    )


@pytest.mark.parametrize(
    "meta, expected",
    [
        ({"summary": "s", "status": "draft"}, "draft"),
        ({"summary": "s", "status": "final"}, "final"),
        ({"summary": "s", "status": "published"}, None),
        ({"summary": "s", "status": ["draft"]}, None),
        ({"summary": "s"}, None),
    ],
    ids=["draft", "final", "unknown-value", "wrong-type", "absent"],
)
def test_summary_status_only_trusts_the_two_known_values(
    tmp_path: Path, meta: dict[str, Any], expected: str | None
) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    _sidecar(pkg).write_text(json.dumps(meta), encoding="utf-8")
    assert tools.summary_status(pkg) == expected


def test_summary_status_none_without_sidecar(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert tools.summary_status(pkg) is None


# --- the strict reader: UNREADABLE is not ABSENT (D40 r3 / R3-2) ---------------


@pytest.mark.parametrize(
    "meta, expected",
    [
        ({"summary": "s", "status": "draft"}, "draft"),
        ({"summary": "s", "status": "final"}, "final"),
    ],
    ids=["draft", "final"],
)
def test_summary_status_or_unknown_agrees_on_a_readable_sidecar(
    tmp_path: Path, meta: dict[str, Any], expected: str
) -> None:
    """A sidecar we CAN read gives the strict reader and the total one the same
    answer -- the split is only about what the failures mean.

    The strict reader also hands the meta it just parsed back (R4-2), which is how
    its one caller gets the ``origin`` without reading the file a second time. The
    meta is the WHOLE sidecar, not a re-read of it: asserted by content."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    _sidecar(pkg).write_text(json.dumps(meta), encoding="utf-8")
    assert tools.summary_status_or_unknown(pkg) == (expected, meta)
    assert tools.summary_status(pkg) == expected


def test_summary_status_or_unknown_none_only_when_the_sidecar_is_really_absent(
    tmp_path: Path,
) -> None:
    """None means ENOENT and nothing else: the one case where "not finalized" is
    a fact rather than a guess. There is no meta to hand back either -- an absent
    sidecar has no origin to inherit, which is a fact and not a failed look."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    assert tools.summary_status_or_unknown(pkg) == (None, None)


def test_summary_status_or_unknown_reports_unknown_for_an_unreadable_sidecar(
    tmp_path: Path,
) -> None:
    """A FIFO at the sidecar name: it EXISTS, and the bounded reader refuses it
    (O_NONBLOCK + the S_ISREG gate). ``summary_status`` folds that into the same
    None a missing sidecar gives; the strict reader must not, because its caller
    would read that None as "safe to destroy this package"."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    os.mkfifo(_sidecar(pkg))

    assert tools.summary_status(pkg) is None  # the total reader still degrades ...
    assert tools.summary_status_or_unknown(pkg) == (
        tools._SUMMARY_STATUS_UNKNOWN,
        None,  # ... this does not, and it hands back nothing it just refused to trust
    )


@pytest.mark.parametrize(
    "content",
    ["not json at all", "[1, 2, 3]", '{"summary": "s"}', '{"summary": "s", "status": "published"}'],
    ids=["invalid-json", "not-an-object", "no-status", "unknown-status"],
)
def test_summary_status_or_unknown_reports_unknown_for_a_corrupt_sidecar(
    tmp_path: Path, content: str
) -> None:
    """Every "the file is there but says nothing we trust" shape is UNKNOWN too.

    ``write_tool_meta`` writes one of exactly two status literals into a JSON
    object every time, so each of these is a hand-edited or damaged file -- and a
    status we refused to trust is not evidence that the summary is unfrozen.

    The meta is withheld on every one of them (R4-2): a file whose status we
    refuse to believe must not have its other fields handed on as if we did."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    _sidecar(pkg).write_text(content, encoding="utf-8")

    assert tools.summary_status(pkg) is None
    assert tools.summary_status_or_unknown(pkg) == (tools._SUMMARY_STATUS_UNKNOWN, None)


def test_summary_status_or_unknown_reports_unknown_for_an_oversized_sidecar(
    tmp_path: Path,
) -> None:
    """Over the cap is refused by the reader, so it is UNKNOWN rather than absent
    -- an oversized sidecar could hold a finalized summary just as easily."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    padding = "x" * tools._AI_META_MAX_BYTES
    _sidecar(pkg).write_text(json.dumps({"summary": padding, "status": "final"}), encoding="utf-8")

    assert tools.summary_status(pkg) is None
    assert tools.summary_status_or_unknown(pkg) == (tools._SUMMARY_STATUS_UNKNOWN, None)


def test_summary_status_unknown_sentinel_is_not_a_real_status(tmp_path: Path) -> None:
    """The sentinel can never be confused with something a sidecar HOLDS: it is
    outside ``_SUMMARY_STATUSES``, so the narrowing refuses it on the way in -- a
    hand-edited ``"status": "unknown"`` reaches a caller as the sentinel only
    because the file was not trustworthy, which is the same thing it means."""
    assert tools._SUMMARY_STATUS_UNKNOWN not in tools._SUMMARY_STATUSES
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    _sidecar(pkg).write_text(
        json.dumps({"summary": "s", "status": tools._SUMMARY_STATUS_UNKNOWN}), encoding="utf-8"
    )
    assert tools.summary_status(pkg) is None  # never passed through as a status
    assert _write_meta(pkg, summary="s", status=tools._SUMMARY_STATUS_UNKNOWN) is True
    assert tools.summary_status(pkg) == "draft"  # ... and the writer coerces it away too


def test_set_summary_status_finalizes_and_preserves_the_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """定版 flips only status + updated_at; the summary and origin survive, and
    the flip is reversible."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    _write_meta(
        pkg,
        summary="說明",
        status="draft",
        llm_log_id=5,
        origin={"openapi_url": "http://kb.example/o.json", "instructions": "i"},
    )

    assert tools.set_summary_status("echo", "final") == "ok"
    stored = tools.read_tool_meta(pkg)
    assert stored is not None
    assert stored["status"] == "final"
    assert stored["summary"] == "說明"
    assert stored["llm_log_id"] == 5
    assert stored["origin"]["instructions"] == "i"
    assert stored["updated_at"] != "2026-01-01T00:00:00+00:00"  # refreshed
    assert tools.summary_status(pkg) == "final"

    # Reversible: 解除定版 puts it back to draft.
    assert tools.set_summary_status("echo", "draft") == "ok"
    assert tools.summary_status(pkg) == "draft"


def test_set_summary_status_no_meta_when_sidecar_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    assert tools.set_summary_status("echo", "final") == "no_meta"


@pytest.mark.parametrize("summary", ["", "   ", None, 12], ids=["empty", "blank", "null", "int"])
def test_set_summary_status_refuses_to_finalize_an_empty_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, summary: Any
) -> None:
    """定版 means "freeze THIS explanation", and there is none here.

    Left alone, this was a trap rather than a harmless no-op: a hand-written
    ``{"summary": null, "status": "draft"}`` finalized with a 200, and the
    finalized nothing then blocked 重新產生 with ``tool_finalized`` -- the one
    action that could have filled it. Same "nothing there to freeze" answer as a
    package with no sidecar at all, so it reuses ``no_meta`` (the 409
    ``summary_missing``)."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    # Written by hand: the WRITER coerces null->"" and refuses an int, so these
    # shapes only ever reach the finalize gate from a hand-edited file.
    _sidecar(pkg).write_text(json.dumps({"summary": summary, "status": "draft"}), encoding="utf-8")

    assert tools.set_summary_status("echo", "final") == "no_meta"
    assert tools.summary_status(pkg) == "draft"  # nothing was rewritten


def test_set_summary_status_draft_is_never_gated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """解除定版 is the escape hatch out of a frozen state, and an escape hatch
    that can itself be refused is not one -- so the emptiness gate above applies
    ONLY to the "final" direction. This is what un-sticks a sidecar that was
    finalized empty before the gate existed."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    _sidecar(pkg).write_text(json.dumps({"summary": "", "status": "final"}), encoding="utf-8")

    assert tools.set_summary_status("echo", "draft") == "ok"
    assert tools.summary_status(pkg) == "draft"


def test_set_summary_status_draft_survives_a_corrupt_typed_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R6-2: the escape hatch must survive a corruption ``write_tool_meta``
    itself would refuse to write. A hand-edited ``{"summary": 123, ...}`` used
    to ride unchanged into the rewrite, whose own non-str-summary refusal
    returned False -- which this function folded into ``"not_found"``, 404ing
    the ONE mutation (解除定版) that exists to recover from exactly this
    corruption, with no other way to reach it through the API. The summary is
    now coerced to "" before the write, so 解除定版 always succeeds; a
    corrupt-typed summary still cannot be finalized afterward -- the emptiness
    gate re-triggers ``no_meta`` on the very next 定版 attempt."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    _sidecar(pkg).write_text(json.dumps({"summary": 123, "status": "final"}), encoding="utf-8")

    assert tools.set_summary_status("echo", "draft") == "ok"
    stored = tools.read_tool_meta(pkg)
    assert stored is not None
    assert stored["status"] == "draft"
    assert stored["summary"] == ""  # corrupt TYPE degrades to "no summary yet"

    # The coercion cannot reopen 定版 as a back door: the emptiness gate still
    # runs first and still refuses an (effectively) empty summary.
    assert tools.set_summary_status("echo", "final") == "no_meta"


def test_set_summary_status_still_reports_not_found_on_a_real_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R6-2 collateral: the new coercion only ever touches the INPUT payload
    (a non-str ``summary``); it must not change what happens when
    ``write_tool_meta`` fails for a genuine reason with a perfectly good ``str``
    summary already on disk -- that must still read as ``"not_found"``, exactly
    as before this fix."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    _write_meta(pkg, summary="說明", status="draft")
    monkeypatch.setattr(tools, "write_tool_meta", lambda *args, **kwargs: False)

    assert tools.set_summary_status("echo", "final") == "not_found"


def test_set_summary_status_final_to_final_never_rewrites_the_frozen_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R7-2: a repeated 定版 is an idempotent RETRY, and must not touch the file.

    The obvious way to get one is a lost response -- the client resends the same
    PATCH. That used to run the whole rewrite: back through ``write_tool_meta``,
    which re-redacts every text field against TODAY's known-secret set. And that
    set GROWS: installing any other tool registers its ``.env`` values. So a
    value that appears inside text an operator froze WEEKS ago starts matching,
    and the retry silently replaces part of the frozen explanation with a
    redaction marker -- "finalized text is immutable while finalized" broken by
    the one request that asked for no change at all. Refreshing ``updated_at``
    was the same lie in miniature.

    Staged with a REAL second package rather than a stubbed registry, because the
    registry's growth is the whole mechanism: the value only becomes a secret
    because a later install put it in a ``.env``."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    frozen = "這個工具會查 KB，範例參數寫成 kb-live-token-abcdef"  # noqa: RUF001
    assert _write_meta(pkg, summary=frozen, status="draft") is True
    assert tools.set_summary_status("echo", "final") == "ok"
    before = _sidecar(pkg).read_bytes()
    assert frozen in before.decode("utf-8")  # frozen whole, nothing masked yet

    # A LATER install registers that exact string: another tool's new .env value.
    _make_tool(root, "kb", "import sys\n", dotenv="KB_API_KEY=kb-live-token-abcdef\n")
    assert "kb-live-token-abcdef" in tools.known_secret_values()

    assert tools.set_summary_status("echo", "final") == "ok"  # the retry is answered

    assert _sidecar(pkg).read_bytes() == before  # ... byte-identical: no write at all
    stored = tools.read_tool_meta(pkg)
    assert stored is not None
    assert stored["summary"] == frozen  # the frozen text was never re-redacted
    assert tools._REDACTION_MARKER not in stored["summary"]
    assert stored["updated_at"] == json.loads(before.decode("utf-8"))["updated_at"]


def test_set_summary_status_draft_to_draft_is_a_no_op_but_a_transition_still_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The symmetry half of R7-2, and its boundary.

    draft->draft is short-circuited too: a same-status rewrite has nothing
    legitimate to do in EITHER direction (it only ever carries ``status``, which
    already matches, and ``updated_at``, which nobody asked to change), and one
    direction behaving differently from the other would be a rule nobody can
    remember. The second half is the collateral that matters more: the
    short-circuit fires on EXACT status equality only, so a real transition on
    the very same sidecar still writes."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    assert _write_meta(pkg, summary="說明", status="draft") is True
    before = _sidecar(pkg).read_bytes()

    assert tools.set_summary_status("echo", "draft") == "ok"
    assert _sidecar(pkg).read_bytes() == before

    assert tools.set_summary_status("echo", "final") == "ok"  # a REAL transition
    assert _sidecar(pkg).read_bytes() != before
    assert tools.summary_status(pkg) == "final"


@pytest.mark.parametrize(
    "on_disk",
    [{"summary": "說明"}, {"summary": "說明", "status": "frozen"}],
    ids=["status-absent", "status-unknown"],
)
def test_set_summary_status_short_circuits_only_on_exact_equality(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, on_disk: dict[str, Any]
) -> None:
    """R7-2 collateral: nothing but an exact status match skips the write.

    A hand-edited sidecar with no ``status`` at all, or one carrying a value
    ``_SUMMARY_STATUSES`` does not recognize, must still be REWRITTEN into a
    legal state by a PATCH -- those are exactly the files a status mutation
    exists to repair, and folding them into the no-op would strand them."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    _sidecar(pkg).write_text(json.dumps(on_disk), encoding="utf-8")

    assert tools.set_summary_status("echo", "draft") == "ok"
    assert tools.summary_status(pkg) == "draft"  # rewritten, not short-circuited


@pytest.mark.parametrize(
    "name", ["ghost", "../escape", "UPPER"], ids=["missing", "traversal", "regex"]
)
def test_set_summary_status_not_found_for_unknown_or_unsafe_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    root = tmp_path / "tools"
    _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    assert tools.set_summary_status(name, "final") == "not_found"


def test_set_summary_status_not_found_when_feature_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("afterthread.services.tools.get_settings", lambda: Settings(tools_dir=""))
    assert tools.set_summary_status("echo", "final") == "not_found"


def test_set_summary_status_refuses_internal_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same INTERNAL-alias hard-block set_enabled carries (H3), on the
    summary path: tools/<alias> -> tools/<real> resolves inside the root, so
    without it a 定版 addressed at the alias would freeze the REAL package's
    summary. Refused, and the real sidecar is left exactly as it was."""
    root = tmp_path / "tools"
    real = _make_tool(root, "real", "import sys\nsys.stdout.write('x')\n")
    (root / "alias").symlink_to(root / "real", target_is_directory=True)
    _install_tools(monkeypatch, root)
    _write_meta(real, summary="說明", status="draft")
    before = (real / tools._AI_META_FILENAME).read_bytes()

    assert tools.set_summary_status("alias", "final") == "not_found"
    assert (real / tools._AI_META_FILENAME).read_bytes() == before
    assert tools.summary_status(real) == "draft"


def test_list_tools_reports_summary_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every row carries the sidecar's status (null when there is none), so the
    page badges the whole list from one request."""
    root = tmp_path / "tools"
    finalized = _make_tool(root, "aaa", "import sys\nsys.stdout.write('x')\n")
    _make_tool(root, "bbb", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    _write_meta(finalized, summary="s", status="final")

    listed = {row["name"]: row["summary_status"] for row in list_tools()}
    assert listed == {"aaa": "final", "bbb": None}


def test_list_tools_reports_no_summary_status_for_an_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An internal alias row must not badge the REAL package's summary.

    ``tools/alias -> tools/real`` is listed (invalid, so it can be deleted), and
    the listing was the LAST place still reading through the link: the row showed
    已定版 because REAL's sidecar says so, while every by-name summary route --
    GET, PATCH, regenerate -- 404s the name. A badge no request can reproduce,
    describing a different package than the row it sits on."""
    root = tmp_path / "tools"
    real = _make_tool(root, "real", "import sys\nsys.stdout.write('x')\n")
    (root / "alias").symlink_to(root / "real", target_is_directory=True)
    _install_tools(monkeypatch, root)
    assert _write_meta(real, summary="說明", status="final") is True

    rows = {row["name"]: row for row in list_tools()}

    assert rows["alias"]["summary_status"] is None
    assert rows["alias"]["valid"] is False  # unchanged: a symlinked package is refused
    assert rows["real"]["summary_status"] == "final"


def test_list_tools_row_describes_one_instance_when_a_promote_lands_mid_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R9-2: a row is A's manifest OR B's, never A's description with B's badge.

    The two reads a row is built from -- the manifest scan and the sidecar read --
    used to be joined by PATH alone, so a revise promote landing between them
    stitched half of each package into one row. The 工具 page derives a row's
    instance identity from name + description, so the torn row keeps the panel
    (and any unsent 修訂意見) mounted against A while the badge beside it reports
    B: the operator sees nothing change and submits the feedback against B.

    Driven from INSIDE the read rather than by racing a thread (the r6/r7/r8
    window tests' method): the swap lands in the instant BEFORE the sidecar read,
    i.e. strictly between the two reads a row is made of. The row must then be
    fully B -- B is what the name resolves to by the time the sidecar was read,
    so it is the honest answer, and it is certainly not A's description beside
    B's badge, which is what ships without the pairing (measured: the row comes
    back ``("test tool", "final")``)."""
    root = tmp_path / "tools"
    old = _make_tool(root, "kb", "import sys\nsys.stdout.write('OLD')\n")
    _write_meta(old, summary="舊工具的說明", status="draft")
    replacement = _make_tool(
        tmp_path / "staging",
        "kb",
        "import sys\nsys.stdout.write('NEW')\n",
        tool_json={
            "name": "kb",
            "description": "the replacement",
            "parameters": {"type": "object", "properties": {}},
            "entry": [sys.executable, "run.py"],
            "enabled": True,
        },
    )
    _write_meta(replacement, summary="新工具的說明", status="final")
    _install_tools(monkeypatch, root)

    pkg = root / "kb"
    real_status = tools.summary_status
    swapped = False

    def promote_then_read_status(directory: Path) -> str | None:
        nonlocal swapped
        if not swapped:
            swapped = True
            os.rename(pkg, root / ".kb.bak-r9")
            os.rename(replacement, pkg)
        return real_status(directory)

    monkeypatch.setattr(tools, "summary_status", promote_then_read_status)
    rows = {row["name"]: row for row in list_tools()}

    assert swapped  # the swap really landed in the gap
    row = rows["kb"]
    assert (row["description"], row["summary_status"]) == ("the replacement", "final")


def test_list_tools_pairs_each_row_without_rescanning_an_undisturbed_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ordinary listing is what it was: one scan and one sidecar read per
    package, plus the identity re-check that pairs them.

    The pairing costs a re-scan only when a package really moved, so an untouched
    tools directory pays exactly one ``_scan_package`` per row -- the assertion
    that keeps a retry loop from quietly becoming the normal path."""
    root = tmp_path / "tools"
    first = _make_tool(root, "aaa", "import sys\nsys.stdout.write('x')\n")
    _make_tool(root, "bbb", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    _write_meta(first, summary="s", status="final")

    scanned: list[str] = []
    real_scan = tools._scan_package

    def counting_scan(directory: Path, expected_name: str | None = None) -> tools._PackageScan:
        scanned.append(directory.name)
        return real_scan(directory, expected_name=expected_name)

    monkeypatch.setattr(tools, "_scan_package", counting_scan)
    rows = {row["name"]: row["summary_status"] for row in list_tools()}

    assert rows == {"aaa": "final", "bbb": None}
    assert scanned == ["aaa", "bbb"]


def test_list_tools_reports_no_status_when_the_two_reads_never_agree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A package replaced on EVERY attempt gets no badge rather than a foreign one.

    The bound exists so one listing cannot spin forever; the answer it degrades to
    is the same None a missing or corrupt sidecar already produces, not a new
    vocabulary -- and the row's other fields still come from one scan."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "kb", "import sys\nsys.stdout.write('x')\n")
    _write_meta(pkg, summary="s", status="final")
    _install_tools(monkeypatch, root)

    real_status = tools.summary_status
    manifest = pkg / "tool.json"

    def status_then_replace_the_manifest(directory: Path) -> str | None:
        status = real_status(directory)
        # A REPLACED manifest, which is what every install writes (mkstemp then
        # os.replace) -- a NEW inode, so the identity moves on every attempt no
        # matter how fast the loop runs. An in-place rewrite would move only the
        # ctime, and two rewrites inside one timestamp tick are indistinguishable
        # (the ABA `package_identity` has always accepted), which made this test
        # pass or fail by timing.
        fresh = directory / "tool.json.next"
        fresh.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
        os.replace(fresh, manifest)
        return status

    monkeypatch.setattr(tools, "summary_status", status_then_replace_the_manifest)
    rows = {row["name"]: row for row in list_tools()}

    assert rows["kb"]["summary_status"] is None
    assert rows["kb"]["valid"] is True  # the rest of the row is still one scan's


def test_list_tools_still_reports_none_for_a_corrupt_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pairing must not change what an unreadable sidecar reports: the row is
    the same one instance, and None is already its answer."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "kb", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    _sidecar(pkg).write_text("{not json", encoding="utf-8")

    rows = {row["name"]: row for row in list_tools()}
    assert rows["kb"]["summary_status"] is None
    assert rows["kb"]["valid"] is True


def test_sidecar_never_listed_as_a_package(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The dot-prefixed sidecar is invisible to the registry scan: it is neither
    a phantom row nor a reason for the real package to look broken."""
    root = tmp_path / "tools"
    pkg = _make_tool(root, "echo", "import sys\nsys.stdout.write('x')\n")
    _install_tools(monkeypatch, root)
    _write_meta(pkg, summary="s", status="draft")

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
    _write_meta(pkg, summary="s", status="final")
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
