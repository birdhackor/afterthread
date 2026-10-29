"""Tests for ``StructuredRunStats``: what ``generate_structured`` measures about
how one call ended, and that measuring never changes the call.

Stubbed like the tool-loop tests in test_tools.py: a scripted client replaces
``_get_client`` and ``get_settings`` is overridden, so nothing reaches the
network. Each call is driven with ``asyncio.run`` (no pytest-asyncio plugin).
"""

import asyncio
import copy
import json
import time
import types
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal, Union, get_args, get_origin

import httpx
import openai
import pytest
from pydantic import BaseModel, ConfigDict

from afterthread.config import Settings
from afterthread.schemas import ToolJobStatus
from afterthread.services import llm, llm_log, token_budget
from afterthread.services.llm import (
    _MAX_TOOL_CALLS_ACCEPTED,
    _MAX_TOOL_CALLS_TOTAL_BYTES,
    _TOOL_BUDGET_EXHAUSTED,
    _UPSTREAM_REASON,
    LLMNotConfiguredError,
    LlmTool,
    LLMUpstreamError,
    StructuredRunStats,
    generate_structured,
)

_URL = "http://llm.internal.example/v1"
_ANSWER = json.dumps({"title": "Draft", "count": 2})


class _Sample(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    count: int = 0


@pytest.fixture(autouse=True)
def _reset_log() -> Generator[None]:
    llm_log._reset_for_tests()
    yield
    llm_log._reset_for_tests()


def _tool_round(calls: int = 1) -> SimpleNamespace:
    """A reply asking for ``calls`` calls of the ``echo`` tool (content null)."""
    tool_calls = [
        SimpleNamespace(
            id=f"call_{index}",
            type="function",
            function=SimpleNamespace(name="echo", arguments=json.dumps({"n": index})),
        )
        for index in range(calls)
    ]
    message = SimpleNamespace(content=None, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _oversized_tool_round() -> SimpleNamespace:
    """A reply of ONE ``echo`` call -- well under the count bound -- whose arguments
    alone pass the size bound."""
    tool_call = SimpleNamespace(
        id="call_0",
        type="function",
        function=SimpleNamespace(
            name="echo", arguments=json.dumps({"pad": "x" * _MAX_TOOL_CALLS_TOTAL_BYTES})
        ),
    )
    message = SimpleNamespace(content=None, tool_calls=[tool_call])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _answer(content: str | None) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _ScriptedCompletions:
    """``chat.completions`` replaying a script: each step is a completion to return
    or an exception to raise; the last step repeats once the script runs out.

    Every request is deep-copied as it arrives -- the loop keeps appending to the
    very list it sent -- so ``calls`` is what was actually sent each time."""

    def __init__(self, steps: list[Any], *, delay: float) -> None:
        self._steps = steps
        self._delay = delay
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(copy.deepcopy(kwargs))
        if self._delay:
            await asyncio.sleep(self._delay)
        step = self._steps[0] if len(self._steps) == 1 else self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


@dataclass
class _Scenario:
    """One scripted call: the replies, plus whatever settings, arguments or timings
    make the path under test reachable. ``steps`` is a factory, so every run gets
    fresh reply and exception objects. ``delay`` holds every request,
    ``tool_delay`` every run of the tool; ``deadline_floor`` replaces the time
    that must be left for the loop to start a tool at all, so a short deadline can
    still fire while a tool runs."""

    steps: Callable[[], list[Any]]
    settings: dict[str, Any] = field(default_factory=dict)
    kwargs: dict[str, Any] = field(default_factory=dict)
    delay: float = 0.0
    tool_output: str = "ok"
    tool_delay: float = 0.0
    deadline_floor: float | None = None


def _request(method: str = "POST") -> httpx.Request:
    return httpx.Request(method, f"{_URL}/chat/completions")


_SCENARIOS: dict[str, _Scenario] = {
    "answers-directly": _Scenario(lambda: [_answer(_ANSWER)]),
    "tool-round-then-answer": _Scenario(lambda: [_tool_round(), _answer(_ANSWER)]),
    "rounds-exhausted": _Scenario(
        lambda: [_tool_round(), _tool_round(), _answer(_ANSWER)], kwargs={"max_tool_rounds": 2}
    ),
    "rounds-exhausted-then-empty": _Scenario(
        lambda: [_tool_round(), _answer(None)], kwargs={"max_tool_rounds": 1}
    ),
    "conversation-budget": _Scenario(
        lambda: [_tool_round(), _tool_round(), _answer(_ANSWER)],
        settings={"llm_tool_conversation_budget_tokens": 50_000},
        tool_output="x" * 30_000,
    ),
    "corrected-output": _Scenario(lambda: [_answer("not json"), _answer(_ANSWER)]),
    "invalid-output": _Scenario(lambda: [_answer("not json"), _answer("still not json")]),
    "upstream-error": _Scenario(lambda: [openai.APIConnectionError(request=_request())]),
    "sdk-request-timeout": _Scenario(lambda: [openai.APITimeoutError(request=_request())]),
    "request-timeout": _Scenario(lambda: [TimeoutError("the request timed out")]),
    "deadline": _Scenario(lambda: [_answer(_ANSWER)], kwargs={"timeout_seconds": 0.05}, delay=1.0),
    "deadline-inside-a-tool-round": _Scenario(
        lambda: [_tool_round(), _answer(_ANSWER)],
        kwargs={"timeout_seconds": 0.1},
        tool_delay=1.0,
        deadline_floor=0.0,
    ),
    "flood": _Scenario(lambda: [_tool_round(_MAX_TOOL_CALLS_ACCEPTED + 1)]),
    "oversized": _Scenario(lambda: [_oversized_tool_round()]),
    "not-configured": _Scenario(lambda: [_answer(_ANSWER)], settings={"openai_base_url": ""}),
}


@dataclass
class _Observed:
    stats: StructuredRunStats | None
    requests: list[dict[str, Any]]
    handler_calls: list[dict[str, Any]]
    outcome: tuple[Any, ...]
    records: list[dict[str, Any]]


def _observe(
    monkeypatch: pytest.MonkeyPatch, scenario: _Scenario, *, stats: StructuredRunStats | None
) -> _Observed:
    """Run ``scenario`` once from a clean slate and collect everything the call did."""
    llm_log._reset_for_tests()
    token_budget._reset_for_tests()
    settings = Settings(
        **{
            "openai_base_url": _URL,
            "openai_api_key": "sk-test",
            "openai_model": "test-model",
            **scenario.settings,
        }
    )
    completions = _ScriptedCompletions(scenario.steps(), delay=scenario.delay)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr("afterthread.services.llm.get_settings", lambda: settings)
    monkeypatch.setattr("afterthread.services.llm._get_client", lambda: client)
    if scenario.deadline_floor is not None:
        monkeypatch.setattr(llm, "_TOOL_DEADLINE_FLOOR_SECONDS", scenario.deadline_floor)
    handler_calls: list[dict[str, Any]] = []

    async def echo(args: dict[str, Any]) -> str:
        handler_calls.append(args)
        if scenario.tool_delay:
            await asyncio.sleep(scenario.tool_delay)
        return scenario.tool_output

    tool = LlmTool(
        spec={
            "type": "function",
            "function": {"name": "echo", "description": "d", "parameters": {"type": "object"}},
        },
        handler=echo,
    )
    kwargs: dict[str, Any] = {"tools": [tool], **scenario.kwargs}
    if stats is not None:
        kwargs["stats"] = stats
    outcome: tuple[Any, ...]
    try:
        result = asyncio.run(generate_structured("system", "user", _Sample, **kwargs))
    except (LLMUpstreamError, LLMNotConfiguredError) as exc:
        outcome = (
            "raised",
            type(exc).__name__,
            str(exc),
            exc.__cause__ is None,
            exc.__suppress_context__,
        )
    else:
        outcome = ("returned", result.model_dump())
    records = []
    for summary in llm_log.list_summaries(10):
        record = llm_log.get_record(summary["id"])
        assert record is not None
        for volatile in ("id", "started_at", "finished_at", "duration_ms"):
            record.pop(volatile)
        records.append(record)
    return _Observed(stats, completions.calls, handler_calls, outcome, records)


def _run(monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[StructuredRunStats, _Observed]:
    stats = StructuredRunStats()
    return stats, _observe(monkeypatch, _SCENARIOS[name], stats=stats)


# --- measuring changes nothing ---------------------------------------------------


@pytest.mark.parametrize("name", list(_SCENARIOS))
def test_stats_never_changes_what_the_call_does(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """The same replies, with and without a stats object: the same requests (the
    exact messages and tools sent each time), the same tool-handler calls, the same
    result or exception (type, message, severed cause), and the same AI log
    record."""
    scenario = _SCENARIOS[name]
    plain = _observe(monkeypatch, scenario, stats=None)
    measured = _observe(monkeypatch, scenario, stats=StructuredRunStats())

    assert measured.requests == plain.requests
    assert measured.handler_calls == plain.handler_calls
    assert measured.outcome == plain.outcome
    assert measured.records == plain.records


def test_stats_scenarios_reach_every_run_failure_and_finalize_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scenarios the test above compares are not a sample: between them they end
    in every ``RunFailure`` and every ``FinalizeTrigger`` (and in neither), so a new
    way to end has to bring a scenario before measuring it is called harmless."""
    failures: set[Any] = set()
    triggers: set[Any] = set()
    for scenario in _SCENARIOS.values():
        stats = StructuredRunStats()
        _observe(monkeypatch, scenario, stats=stats)
        failures.add(stats.failure)
        triggers.add(stats.finalized_by)

    expected_failures = set(get_args(llm.RunFailure))
    expected_triggers = set(get_args(llm.FinalizeTrigger))
    assert expected_failures and expected_triggers
    assert failures == expected_failures | {None}
    assert triggers == expected_triggers | {None}


# --- what each ending records ------------------------------------------------------


def test_stats_answered_directly_records_no_budget_and_no_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stats, observed = _run(monkeypatch, "tool-round-then-answer")

    assert observed.outcome[0] == "returned"
    assert stats == StructuredRunStats(
        tool_rounds_used=1, completions=2, finalized_by=None, failure=None
    )


def test_stats_rounds_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The round budget ends the tool phase: the finalize request is the one that
    carries the budget-exhausted nudge and no tools, and the answer still parses."""
    stats, observed = _run(monkeypatch, "rounds-exhausted")

    assert observed.outcome[0] == "returned"
    assert "tools" not in observed.requests[-1]
    assert observed.requests[-1]["messages"][-1]["content"] == _TOOL_BUDGET_EXHAUSTED
    assert stats == StructuredRunStats(
        tool_rounds_used=2, completions=3, finalized_by="rounds", failure=None
    )


def test_stats_conversation_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """The conversation size ends the tool phase with the round budget unspent. The
    model is shown the same nudge as for rounds; only the stats tell them apart."""
    stats, observed = _run(monkeypatch, "conversation-budget")

    assert observed.outcome[0] == "returned"
    assert observed.requests[-1]["messages"][-1]["content"] == _TOOL_BUDGET_EXHAUSTED
    assert stats.finalized_by == "conversation"
    assert stats.tool_rounds_used == 2  # far under the default round budget
    assert stats.completions == 3
    assert stats.failure is None


def test_stats_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """A final reply with no content, with no budget involved."""
    stats = StructuredRunStats()
    observed = _observe(monkeypatch, _Scenario(lambda: [_answer(None)]), stats=stats)

    assert observed.outcome[1:3] == (
        "LLMUpstreamError",
        "EmptyResponse: the LLM returned empty content",
    )
    assert stats == StructuredRunStats(
        tool_rounds_used=0, completions=1, finalized_by=None, failure="empty_response"
    )


def test_stats_rounds_then_empty_finalize_keeps_both_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The round budget forced the finalize AND the finalize came back empty: both
    are recorded, neither replaces the other."""
    stats, observed = _run(monkeypatch, "rounds-exhausted-then-empty")

    assert observed.outcome[0] == "raised"
    assert stats == StructuredRunStats(
        tool_rounds_used=1, completions=2, finalized_by="rounds", failure="empty_response"
    )


def test_stats_invalid_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Still not an object after the one corrective retry -- unlike a reply the
    retry fixed, which is no failure at all."""
    stats, observed = _run(monkeypatch, "invalid-output")
    assert observed.outcome[0] == "raised"
    assert stats.failure == "invalid_output"
    assert stats.completions == 2

    corrected, observed = _run(monkeypatch, "corrected-output")
    assert observed.outcome[0] == "returned"
    assert corrected.failure is None


def test_stats_upstream_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """An SDK error other than its request timeout, and a tool_calls flood, are
    both ``upstream``."""
    connection, _ = _run(monkeypatch, "upstream-error")
    assert connection.failure == "upstream"

    flood, observed = _run(monkeypatch, "flood")
    assert observed.outcome[2].startswith("tool_calls flood")
    assert flood.failure == "upstream"
    assert flood.completions == 1


def test_stats_only_the_sdk_timeout_is_a_request_timeout_not_its_parent_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK's own request timeout is one request timing out; a connection that
    failed is not, although the timeout class derives from the connection-error
    class -- a test for the parent would sweep both into ``request_timeout``.
    What is raised stays the same safe category message for each."""
    assert issubclass(openai.APITimeoutError, openai.APIConnectionError)

    timed_out, observed = _run(monkeypatch, "sdk-request-timeout")
    assert observed.outcome[1:3] == ("LLMUpstreamError", f"APITimeoutError: {_UPSTREAM_REASON}")
    assert timed_out == StructuredRunStats(
        tool_rounds_used=0, completions=0, finalized_by=None, failure="request_timeout"
    )

    refused, observed = _run(monkeypatch, "upstream-error")
    assert observed.outcome[1:3] == (
        "LLMUpstreamError",
        f"APIConnectionError: {_UPSTREAM_REASON}",
    )
    assert refused.failure == "upstream"


def test_stats_a_tool_reply_refused_at_entry_is_no_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool reply the loop refuses on arrival -- too many calls, or too many
    bytes -- was received but never accepted: it is a completion, not a round, and
    no tool ran."""
    for name, prefix in (("flood", "tool_calls flood"), ("oversized", "tool_calls oversized")):
        stats, observed = _run(monkeypatch, name)

        assert observed.outcome[1] == "LLMUpstreamError", name
        assert observed.outcome[2].startswith(prefix), name
        assert observed.handler_calls == [], name
        assert stats == StructuredRunStats(
            tool_rounds_used=0, completions=1, finalized_by=None, failure="upstream"
        ), name


def test_stats_deadline_inside_a_tool_round_counts_that_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline fires while the first round's tool is still running: that
    round was accepted, so it is counted. The tool started and the next request
    was never sent, so the deadline did fire inside the round."""
    stats, observed = _run(monkeypatch, "deadline-inside-a-tool-round")

    assert observed.outcome[1:3] == ("LLMUpstreamError", f"Timeout: {_UPSTREAM_REASON}")
    assert observed.handler_calls == [{"n": 0}]
    assert len(observed.requests) == 1
    assert stats == StructuredRunStats(
        tool_rounds_used=1, completions=1, finalized_by=None, failure="deadline"
    )


def test_stats_first_request_failing_counts_no_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``completions`` counts replies received, not requests sent: a call whose
    very first request fails received none."""
    stats, observed = _run(monkeypatch, "upstream-error")

    assert len(observed.requests) == 1  # the request WAS sent
    assert stats == StructuredRunStats(
        tool_rounds_used=0, completions=0, finalized_by=None, failure="upstream"
    )


def test_stats_request_timeout_is_not_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A builtin TimeoutError raised by the request while the interaction still had
    time is ``request_timeout``. The raised error is unchanged: the same single
    ``Timeout`` category the deadline raises."""
    stats, observed = _run(monkeypatch, "request-timeout")

    assert observed.outcome[1:3] == ("LLMUpstreamError", f"Timeout: {_UPSTREAM_REASON}")
    assert stats == StructuredRunStats(
        tool_rounds_used=0, completions=0, finalized_by=None, failure="request_timeout"
    )


def test_stats_deadline_is_not_a_request_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The interaction's own deadline firing mid-request is ``deadline``, raised
    as the very same ``Timeout`` error a request timeout raises."""
    stats, observed = _run(monkeypatch, "deadline")

    assert observed.outcome[1:3] == ("LLMUpstreamError", f"Timeout: {_UPSTREAM_REASON}")
    assert stats == StructuredRunStats(
        tool_rounds_used=0, completions=0, finalized_by=None, failure="deadline"
    )


def test_stats_timeout_kind_follows_the_deadline_firing_not_the_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request that blocks past the session's time without ever yielding, then
    raises TimeoutError: the deadline never got to fire, so it is a request timeout,
    although a clock read afterwards is past the deadline. Deciding by the clock
    instead would make the label depend on scheduling at the boundary."""

    class _BlockingTimeout(_ScriptedCompletions):
        async def create(self, **kwargs: Any) -> Any:
            self.calls.append(copy.deepcopy(kwargs))
            time.sleep(0.15)  # never yields: the loop cannot run the deadline
            raise TimeoutError("the request timed out")

    settings = Settings(openai_base_url=_URL, openai_api_key="sk-test", openai_model="test-model")
    client = SimpleNamespace(chat=SimpleNamespace(completions=_BlockingTimeout([], delay=0.0)))
    monkeypatch.setattr("afterthread.services.llm.get_settings", lambda: settings)
    monkeypatch.setattr("afterthread.services.llm._get_client", lambda: client)
    stats = StructuredRunStats()

    with pytest.raises(LLMUpstreamError) as excinfo:
        asyncio.run(
            generate_structured("system", "user", _Sample, timeout_seconds=0.05, stats=stats)
        )

    assert str(excinfo.value) == f"Timeout: {_UPSTREAM_REASON}"
    assert stats.failure == "request_timeout"


def test_stats_deadline_after_a_tool_round_keeps_what_came_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline firing in the SECOND request: the round and the completion that
    came before it are still on record."""

    class _SlowSecond(_ScriptedCompletions):
        async def create(self, **kwargs: Any) -> Any:
            if self.calls:
                self._delay = 1.0
            return await super().create(**kwargs)

    settings = Settings(openai_base_url=_URL, openai_api_key="sk-test", openai_model="test-model")
    completions = _SlowSecond([_tool_round(), _answer(_ANSWER)], delay=0.0)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr("afterthread.services.llm.get_settings", lambda: settings)
    monkeypatch.setattr("afterthread.services.llm._get_client", lambda: client)

    async def echo(args: dict[str, Any]) -> str:
        return "ok"

    tool = LlmTool(
        spec={
            "type": "function",
            "function": {"name": "echo", "description": "d", "parameters": {"type": "object"}},
        },
        handler=echo,
    )
    stats = StructuredRunStats()
    with pytest.raises(LLMUpstreamError):
        asyncio.run(
            generate_structured(
                "system", "user", _Sample, tools=[tool], timeout_seconds=0.3, stats=stats
            )
        )

    assert stats == StructuredRunStats(
        tool_rounds_used=1, completions=1, finalized_by=None, failure="deadline"
    )


def test_stats_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    stats, observed = _run(monkeypatch, "not-configured")

    assert observed.outcome[1] == "LLMNotConfiguredError"
    assert observed.requests == []
    assert stats == StructuredRunStats(
        tool_rounds_used=0, completions=0, finalized_by=None, failure="not_configured"
    )


# --- the job schema speaks the same vocabulary ------------------------------------


def _literal_values(annotation: Any) -> set[Any]:
    """Every value a ``Literal[...]`` (possibly inside ``X | None``) admits."""
    if get_origin(annotation) is Literal:
        return set(get_args(annotation))
    if get_origin(annotation) in (Union, types.UnionType):
        return set().union(*(_literal_values(arg) for arg in get_args(annotation)))
    return set()


def test_tool_job_status_admits_every_run_failure_and_finalize_trigger() -> None:
    """``ToolJobStatus`` spells ``RunFailure`` and ``FinalizeTrigger`` out rather than
    importing them; a value the loop can record but the schema rejects would fail
    the job poll."""
    failures = set(get_args(llm.RunFailure))
    triggers = set(get_args(llm.FinalizeTrigger))
    assert failures and triggers
    fields = ToolJobStatus.model_fields
    assert _literal_values(fields["stopped_by"].annotation) == triggers
    assert _literal_values(fields["failure"].annotation) == failures
