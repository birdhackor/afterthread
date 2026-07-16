"""Tests for the LLM interaction log (context_memory.services.llm_log) and its
integration into ``generate_structured``.

Two layers:

* the log store itself -- ring eviction at maxlen, body-free summaries, and the
  optional JSONL file sink (including that a write failure never breaks the
  caller) -- driven directly through ``LlmInteractionRecorder``;
* the recorder woven into ``generate_structured`` -- driven with the same
  stubbed-client pattern as test_llm_service, proving each outcome (ok with
  usage+attempts, a corrective retry recording two attempts, timeout, upstream
  error, invalid output, not configured) records exactly one correct record,
  plus the hard no-leak invariant: with a configured fake base URL / key, no log
  line and no API payload (summary or full record) carries either secret.

The module-level ring is process-wide, so an autouse fixture resets it (and the
id counter) around every test. Settings that the store reads -- ``llm_log_max_
entries`` for the ring, ``llm_log_file`` for the sink -- are overridden by
monkeypatching ``llm_log.get_settings`` where a test needs a specific value.
"""

import asyncio
import json
import logging
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import openai
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from context_memory.config import Settings
from context_memory.services import llm_log
from context_memory.services.llm import (
    LLMNotConfiguredError,
    LLMUpstreamError,
    generate_structured,
)

_URL = "http://llm.internal.example/v1"
_KEY = "sk-super-secret-do-not-leak"
_MODEL = "glm-5.2-test"


class _Sample(BaseModel):
    """Minimal target model, independent of the memory_ai workflow models."""

    model_config = ConfigDict(extra="ignore")

    title: str
    count: int = 0


_SAMPLE_JSON = json.dumps({"title": "Draft", "count": 2})


@pytest.fixture(autouse=True)
def _reset_log() -> Generator[None]:
    """Empty the ring and the id counter around every test (it is a singleton)."""
    llm_log._reset_for_tests()
    yield
    llm_log._reset_for_tests()


# --- direct store drivers --------------------------------------------------


def _record(
    *,
    workflow: str = "capture",
    model: str = "m",
    messages: list[dict[str, str]] | None = None,
    response: str | None = "out",
    outcome: str = "ok",
    error: str | None = None,
) -> llm_log.LlmInteractionRecorder:
    """Build, populate and finalize one record straight through the recorder."""
    recorder = llm_log.LlmInteractionRecorder(workflow=workflow, model=model)
    recorder.begin_attempt(messages or [{"role": "user", "content": "hi"}])
    if response is not None:
        recorder.record_response(response)
    recorder.finish(outcome=outcome, error=error)
    return recorder


def test_ring_evicts_oldest_past_maxlen(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ring keeps at most llm_log_max_entries records, dropping the oldest;
    list_summaries returns what remains newest-first."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=3))
    llm_log._reset_for_tests()
    for i in range(5):
        _record(workflow=f"w{i}")
    summaries = llm_log.list_summaries(100)
    assert len(summaries) == 3
    # Newest first, with the two oldest (w0, w1) evicted.
    assert [summary["workflow"] for summary in summaries] == ["w4", "w3", "w2"]


def test_summaries_exclude_attempt_bodies() -> None:
    """A summary carries the attempt COUNT, never the bodies."""
    _record(workflow="capture", response="a long response body")
    summaries = llm_log.list_summaries(10)
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["attempts"] == 1
    assert "request_messages" not in summary
    # No body content rides along anywhere in the summary payload.
    blob = json.dumps(summaries, ensure_ascii=False)
    assert "a long response body" not in blob
    assert "request_messages" not in blob


def test_get_record_includes_full_bodies() -> None:
    """The by-id detail carries every attempt's full request/response bodies."""
    _record(
        workflow="enrich",
        messages=[{"role": "system", "content": "SYS"}, {"role": "user", "content": "USR"}],
        response="the full reply",
    )
    log_id = llm_log.list_summaries(1)[0]["id"]
    record = llm_log.get_record(log_id)
    assert record is not None
    assert record["attempts"][0]["request_messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USR"},
    ]
    assert record["attempts"][0]["response_content"] == "the full reply"


def test_get_record_unknown_id_returns_none() -> None:
    _record()
    assert llm_log.get_record(999999) is None


def test_file_sink_writes_valid_jsonl(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """With llm_log_file set, every finished record is appended as one valid JSON
    line carrying the full bodies (ensure_ascii=False keeps CJK readable)."""
    log_file = tmp_path / "llm.jsonl"
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_file=str(log_file), llm_log_max_entries=50),
    )
    llm_log._reset_for_tests()
    _record(workflow="capture", response="回覆內容一")
    _record(workflow="enrich", response="回覆內容二")

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert [obj["workflow"] for obj in parsed] == ["capture", "enrich"]
    # Full body present, and non-ASCII kept verbatim rather than \u-escaped.
    assert parsed[0]["attempts"][0]["response_content"] == "回覆內容一"
    assert "回覆內容一" in lines[0]


def test_file_sink_write_failure_does_not_break_the_call(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A sink write into a nonexistent directory fails, but finish() swallows it:
    no exception escapes, and the record still lands in the ring."""
    unwritable = tmp_path / "nope" / "llm.jsonl"  # parent dir missing -> OSError
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_file=str(unwritable), llm_log_max_entries=50),
    )
    llm_log._reset_for_tests()
    with caplog.at_level(logging.WARNING):
        _record(workflow="capture")  # must not raise
    # The record survived (it is appended before the sink write).
    assert len(llm_log.list_summaries(10)) == 1
    # A one-line warning naming the (non-secret) filename was emitted.
    assert "llm log file sink write failed" in caplog.text


# --- recorder integration through generate_structured ----------------------


class _StubCompletions:
    """Scripted ``chat.completions`` returning content and an optional usage.

    ``content`` returns the same string every call; ``contents`` scripts a
    per-call sequence (last element held once exhausted). ``usage`` (when given)
    is attached to every returned completion so the recorder can capture it.
    """

    def __init__(
        self,
        *,
        content: str | None = None,
        contents: list[str] | None = None,
        exc: Exception | None = None,
        usage: Any = None,
        delay: float = 0.0,
    ) -> None:
        self._content = content
        self._contents = list(contents) if contents is not None else None
        self._exc = exc
        self._usage = usage
        self._delay = delay
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        if self._contents is not None:
            content = self._contents[0] if len(self._contents) == 1 else self._contents.pop(0)
        else:
            content = self._content
        message = SimpleNamespace(content=content)
        completion = SimpleNamespace(choices=[SimpleNamespace(message=message)])
        if self._usage is not None:
            completion.usage = self._usage
        return completion


class _StubClient:
    def __init__(self, **completion_kwargs: Any) -> None:
        self.chat = SimpleNamespace(completions=_StubCompletions(**completion_kwargs))


def _install(
    monkeypatch: pytest.MonkeyPatch, stub: _StubClient, *, timeout: float = 60.0
) -> _StubClient:
    """Point the service AND the log store at a fully-configured fake endpoint.

    llm_log_file is forced empty so no test writes to disk, and both modules see
    the same Settings so nothing depends on ambient env or a stray backend/.env.
    """
    settings = Settings(
        openai_base_url=_URL,
        openai_api_key=_KEY,
        openai_model=_MODEL,
        openai_timeout_seconds=timeout,
        llm_log_file="",
        llm_log_max_entries=50,
    )
    monkeypatch.setattr("context_memory.services.llm.get_settings", lambda: settings)
    monkeypatch.setattr("context_memory.services.llm._get_client", lambda: stub)
    monkeypatch.setattr("context_memory.services.llm_log.get_settings", lambda: settings)
    return stub


def _only_summary() -> dict[str, Any]:
    summaries = llm_log.list_summaries(10)
    assert len(summaries) == 1
    return summaries[0]


def test_ok_path_records_usage_and_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean single-attempt success records outcome ok, the usage tokens, and
    the one attempt's full request messages + response body."""
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18)
    stub = _install(monkeypatch, _StubClient(content=_SAMPLE_JSON, usage=usage))
    asyncio.run(generate_structured("SYS", "USR", _Sample, workflow="capture"))

    summary = _only_summary()
    assert summary["workflow"] == "capture"
    assert summary["outcome"] == "ok"
    assert summary["error"] is None
    assert summary["model"] == _MODEL
    assert summary["attempts"] == 1
    assert summary["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert isinstance(summary["duration_ms"], int)

    record = llm_log.get_record(summary["id"])
    assert record is not None
    attempt = record["attempts"][0]
    assert attempt["response_content"] == _SAMPLE_JSON
    assert attempt["error"] is None
    assert attempt["request_messages"][0]["role"] == "system"
    assert "SYS" in attempt["request_messages"][0]["content"]  # schema-injected prompt
    assert attempt["request_messages"][1] == {"role": "user", "content": "USR"}
    # A single create() call was made (no spurious retry).
    assert len(stub.chat.completions.calls) == 1


def test_corrective_retry_records_two_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad-then-good records both attempts: attempt 1 keeps its bad body and the
    parse error's class, attempt 2 the good body, and attempt 2's request echoes
    the corrective turn."""
    _install(monkeypatch, _StubClient(contents=["not json", _SAMPLE_JSON]))
    asyncio.run(generate_structured("SYS", "USR", _Sample, workflow="enrich"))

    record = llm_log.get_record(_only_summary()["id"])
    assert record is not None
    assert record["outcome"] == "ok"
    assert len(record["attempts"]) == 2
    assert record["attempts"][0]["response_content"] == "not json"
    assert record["attempts"][0]["error"] == "JSONDecodeError"
    assert record["attempts"][1]["response_content"] == _SAMPLE_JSON
    assert record["attempts"][1]["error"] is None
    # Attempt 2 sent system + user + the echoed bad reply + the corrective turn.
    assert len(record["attempts"][1]["request_messages"]) == 4
    assert record["attempts"][1]["request_messages"][2] == {
        "role": "assistant",
        "content": "not json",
    }


def test_timeout_outcome_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow endpoint that trips the wall-clock deadline records outcome timeout
    with the safe Timeout category as its error."""
    stub = _install(monkeypatch, _StubClient(content=_SAMPLE_JSON, delay=1.0), timeout=0.05)
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_structured("s", "u", _Sample, workflow="capture"))
    summary = _only_summary()
    assert summary["outcome"] == "timeout"
    assert summary["error"].startswith("Timeout: ")
    # The attempt was begun before the deadline cut it off.
    assert len(stub.chat.completions.calls) == 1


def test_upstream_error_outcome_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """An SDK error records outcome upstream_error, with the safe category on both
    the record and the failed attempt (whose response stays None)."""
    _install(monkeypatch, _StubClient(exc=openai.OpenAIError("boom")))
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_structured("s", "u", _Sample, workflow="assist_update"))
    summary = _only_summary()
    assert summary["outcome"] == "upstream_error"
    assert summary["error"].startswith("OpenAIError")
    record = llm_log.get_record(summary["id"])
    assert record is not None
    assert record["attempts"][0]["error"].startswith("OpenAIError")
    assert record["attempts"][0]["response_content"] is None


def test_invalid_output_outcome_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad-then-bad exhausts the retry and records outcome invalid_output over two
    attempts."""
    _install(monkeypatch, _StubClient(contents=["not json", "still not json"]))
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_structured("s", "u", _Sample, workflow="enrich"))
    summary = _only_summary()
    assert summary["outcome"] == "invalid_output"
    assert summary["attempts"] == 2


def test_not_configured_outcome_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconfigured endpoint records outcome not_configured with the workflow
    and zero attempts -- no request was ever built or sent."""
    settings = Settings(openai_base_url="", openai_api_key="", openai_model="")
    monkeypatch.setattr("context_memory.services.llm.get_settings", lambda: settings)
    monkeypatch.setattr("context_memory.services.llm_log.get_settings", lambda: settings)

    def _must_not_build() -> Any:
        raise AssertionError("_get_client must not run when unconfigured")

    monkeypatch.setattr("context_memory.services.llm._get_client", _must_not_build)
    with pytest.raises(LLMNotConfiguredError):
        asyncio.run(generate_structured("s", "u", _Sample, workflow="capture"))

    summary = _only_summary()
    assert summary["outcome"] == "not_configured"
    assert summary["workflow"] == "capture"
    assert summary["attempts"] == 0
    assert summary["model"] == ""
    assert summary["usage"] is None


def test_no_secret_leak_in_logs_or_api_payloads(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The hard invariant: with a configured fake base URL / key, neither the
    stdlib log line NOR any API payload (summary or full record) contains either
    secret. Bodies legitimately carry the prompt/response content; they just
    never carry the endpoint config."""
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    stub = _install(monkeypatch, _StubClient(content=_SAMPLE_JSON, usage=usage))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            generate_structured("system prompt", "user prompt", _Sample, workflow="capture")
        )

    # The INFO summary line WAS emitted (positive check) and carries no secret.
    assert "workflow=capture" in caplog.text
    assert "outcome=ok" in caplog.text
    for secret in (_URL, "llm.internal.example", _KEY):
        assert secret not in caplog.text

    summary = _only_summary()
    record = llm_log.get_record(summary["id"])
    payloads = (json.dumps(summary, ensure_ascii=False), json.dumps(record, ensure_ascii=False))
    for payload in payloads:
        for secret in (_URL, "llm.internal.example", _KEY):
            assert secret not in payload
    # The stub confirms the request really went through the configured client.
    assert stub.chat.completions.calls[0]["model"] == _MODEL


# --- router endpoints ------------------------------------------------------


def test_router_lists_summaries_newest_first(client: TestClient) -> None:
    _record(workflow="capture", response="body-one")
    _record(workflow="enrich", response="body-two")
    response = client.get("/api/llm/logs")
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert [log["workflow"] for log in logs] == ["enrich", "capture"]
    # Summaries expose the attempt count, never the bodies.
    assert logs[0]["attempts"] == 1
    assert "body-two" not in json.dumps(logs)


def test_router_limit_is_applied(client: TestClient) -> None:
    for i in range(3):
        _record(workflow=f"w{i}")
    logs = client.get("/api/llm/logs?limit=2").json()["logs"]
    assert len(logs) == 2


@pytest.mark.parametrize("limit", [0, 501, -1])
def test_router_limit_out_of_range_rejected(client: TestClient, limit: int) -> None:
    assert client.get(f"/api/llm/logs?limit={limit}").status_code == 422


def test_router_detail_returns_full_record(client: TestClient) -> None:
    _record(
        workflow="capture",
        messages=[{"role": "system", "content": "SYS"}, {"role": "user", "content": "USR"}],
        response="the full body",
    )
    log_id = client.get("/api/llm/logs").json()["logs"][0]["id"]
    detail = client.get(f"/api/llm/logs/{log_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == log_id
    assert body["attempts"][0]["response_content"] == "the full body"
    assert body["attempts"][0]["request_messages"][0] == {"role": "system", "content": "SYS"}


def test_router_detail_unknown_id_404(client: TestClient) -> None:
    response = client.get("/api/llm/logs/999999")
    assert response.status_code == 404
    assert response.json()["detail"] == "LLM log not found"
