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


# --- stored-body size cap (_stored_body) ------------------------------------


def test_stored_body_at_or_under_cap_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body at or under llm_log_body_max_chars round-trips byte-for-byte,
    untruncated -- the cap must never touch a body that already fits."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_body_max_chars=1000))
    text = "x" * 1000
    stored, truncated = llm_log._stored_body(text)
    assert stored == text
    assert truncated is False


def test_stored_body_over_cap_is_hard_cut_with_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body over the cap is cut to EXACTLY the cap, with the truncation
    marker appended in place of the last characters (not merely a bare
    slice), and reports truncated=True."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_body_max_chars=1000))
    stored, truncated = llm_log._stored_body("y" * 5000)
    assert truncated is True
    assert len(stored) == 1000
    assert stored.endswith(llm_log._BODY_TRUNCATION_MARKER)
    assert stored.startswith("y")


def test_stored_body_cap_smaller_than_marker_hard_cuts_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``llm_log_body_max_chars``'s own ge=1_000 floor makes a cap smaller than
    the marker's length unreachable via a real ``Settings`` instance (it would
    raise a pydantic ValidationError before ``_stored_body`` ever ran) -- so
    this defensive branch is exercised here via a duck-typed stand-in (
    ``_stored_body`` only ever reads the one attribute off whatever
    ``get_settings()`` returns) rather than weakened by skipping the test.
    Confirms the marker is DROPPED (not partially appended) and the result is
    a bare hard cut to exactly ``cap`` chars."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: SimpleNamespace(llm_log_body_max_chars=5))
    stored, truncated = llm_log._stored_body("z" * 100)
    assert truncated is True
    assert stored == "zzzzz"


def test_oversized_request_and_response_are_truncated_and_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized request message AND an oversized response are each stored
    hard-capped at llm_log_body_max_chars with the marker, the attempt's
    `truncated` flag is set from EITHER side, and the ring holds only the
    bounded (capped) bytes rather than the original oversized ones -- proving
    a multi-MB gateway reply cannot inflate the ring regardless of
    llm_log_max_entries."""
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_body_max_chars=1000, llm_log_max_entries=50),
    )
    llm_log._reset_for_tests()

    huge_prompt = "u" * 5000
    huge_response = "r" * 5000
    recorder = llm_log.LlmInteractionRecorder(workflow="capture", model="m")
    recorder.begin_attempt(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": huge_prompt}]
    )
    recorder.record_response(huge_response)
    recorder.finish(outcome="ok", error=None)

    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    attempt = record["attempts"][0]
    assert attempt["truncated"] is True

    # Both bodies land at EXACTLY the configured cap (1000), never the
    # original 5000 -- the ring's per-body size is bounded regardless of how
    # oversized the source text was, so the ring itself "stays small".
    user_message = attempt["request_messages"][1]["content"]
    assert len(user_message) == 1000
    assert user_message.endswith(llm_log._BODY_TRUNCATION_MARKER)
    assert attempt["response_content"] is not None
    assert len(attempt["response_content"]) == 1000
    assert attempt["response_content"].endswith(llm_log._BODY_TRUNCATION_MARKER)
    assert attempt["response_chars"] == 1000
    # request_chars sums the STORED length of every message: the untouched
    # "sys" (3 chars) plus the capped user message (1000 chars).
    assert attempt["request_chars"] == len("sys") + 1000


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


def test_lone_surrogate_response_round_trips_through_file_sink(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A response body carrying a lone (unpaired) surrogate -- as a merely-
    compatible gateway emitting a malformed UTF-16 escape could produce -- is
    replaced with U+FFFD by _utf8_safe at the point record_response stores it,
    so the JSONL line the sink later writes is already surrogate-free: the
    line is valid UTF-8 (the file write never raises) and valid JSON
    (json.loads succeeds), and carries U+FFFD rather than the raw surrogate."""
    log_file = tmp_path / "llm.jsonl"
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_file=str(log_file), llm_log_max_entries=50),
    )
    llm_log._reset_for_tests()
    _record(workflow="capture", response="開頭正常\ud800結尾正常")

    lines = log_file.read_text(encoding="utf-8").splitlines()  # must not raise
    assert len(lines) == 1
    parsed = json.loads(lines[0])  # must not raise
    response_content = parsed["attempts"][0]["response_content"]
    assert "\ufffd" in response_content
    assert "\ud800" not in response_content
    assert "開頭正常" in response_content
    assert "結尾正常" in response_content


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


def test_file_sink_unicode_encode_error_does_not_break_the_call(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The sink's catch is Exception-wide, not OSError-only: a write-path
    failure that raises UnicodeEncodeError -- the exact error a lone surrogate
    would cause if one ever reached this far (see _utf8_safe, which normally
    prevents that upstream, at the point a body is STORED into the record) --
    is swallowed the same way an OSError is. This also proves the sink
    recovers INTERNALLY rather than leaning on finish()'s own outer catch: the
    specific per-sink warning fires (not the generic "finalization failed"
    one), and the INFO summary for the interaction still logs, because control
    returns to finish()'s try block afterward instead of skipping the rest of
    it."""
    log_file = tmp_path / "llm.jsonl"
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_file=str(log_file), llm_log_max_entries=50),
    )
    llm_log._reset_for_tests()

    def _boom(*_args: Any, **_kwargs: Any) -> str:
        raise UnicodeEncodeError("utf-8", "x", 0, 1, "simulated failure")

    monkeypatch.setattr(llm_log.json, "dumps", _boom)

    with caplog.at_level(logging.INFO):
        _record(workflow="capture")  # must not raise
    assert len(llm_log.list_summaries(10)) == 1
    assert "llm log file sink write failed" in caplog.text
    # _log_summary still ran: the failure was contained inside
    # _write_file_sink rather than skipping the rest of finish()'s try block.
    assert "llm interaction workflow=capture" in caplog.text


class _RaisingHandler(logging.Handler):
    """A pathological logging handler that raises straight out of emit().

    Unlike a well-behaved stdlib handler (e.g. StreamHandler.emit, which wraps
    its own body in ``except Exception: self.handleError(record)``), this
    handler does no such thing, so a caller's ``logger.warning(...)`` really
    does propagate whatever emit() raises -- the scenario finish()'s inner
    ``contextlib.suppress(Exception)`` fallback exists to survive.
    """

    def emit(self, record: logging.LogRecord) -> None:
        raise RuntimeError("handler emit exploded")


def test_finish_swallows_a_raising_logging_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """finish() must be UNCONDITIONALLY unable to raise: even when its own
    fallback WARNING log reaches a pathological logging Handler that raises
    inside emit(), the call must complete without the caller (here, _record's
    finish()) ever seeing an exception. Forces the main try block to fail
    (via a broken _log_summary) so the fallback WARNING path -- and thus the
    raising handler -- is actually exercised."""

    def _boom_summary(_record: llm_log.LlmInteractionRecord) -> None:
        raise RuntimeError("summary logging exploded")

    monkeypatch.setattr(llm_log, "_log_summary", _boom_summary)

    handler = _RaisingHandler()
    llm_log.logger.addHandler(handler)
    try:
        _record(workflow="capture")  # must not raise
    finally:
        llm_log.logger.removeHandler(handler)
    # The ring append happens before the (broken) summary log, so the record
    # still survived despite finalization otherwise failing end to end.
    assert len(llm_log.list_summaries(10)) == 1


# --- recorder integration through generate_structured ----------------------


class _StubCompletions:
    """Scripted ``chat.completions`` returning content and an optional usage.

    ``content`` returns the same string every call; ``contents`` scripts a
    per-call sequence (last element held once exhausted). ``usage`` (when
    given) is attached to EVERY returned completion, unchanged from before.
    ``usages`` is the per-attempt sibling of ``contents`` -- a sequence
    scripting a DIFFERENT usage object per call (also holding its last element
    once exhausted) -- so a corrective-retry test can prove the recorder keeps
    each attempt's OWN usage rather than the old "last completion wins"
    number. A ``None`` entry in ``usages`` scripts "this completion reported no
    usage object at all" for that specific call, distinct from omitting
    ``usages``/``usage`` entirely (which never sets `.usage` on any
    completion).
    """

    def __init__(
        self,
        *,
        content: str | None = None,
        contents: list[str] | None = None,
        exc: Exception | None = None,
        usage: Any = None,
        usages: list[Any] | None = None,
        delay: float = 0.0,
    ) -> None:
        self._content = content
        self._contents = list(contents) if contents is not None else None
        self._exc = exc
        self._usage = usage
        self._usages = list(usages) if usages is not None else None
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
        if self._usages is not None:
            usage = self._usages[0] if len(self._usages) == 1 else self._usages.pop(0)
            if usage is not None:
                completion.usage = usage
        elif self._usage is not None:
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
    # A single-attempt interaction's own usage/chars equal the interaction-level
    # aggregate above (nothing to sum with), and nothing here was oversized.
    assert attempt["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert attempt["response_chars"] == len(_SAMPLE_JSON)
    assert attempt["request_chars"] == sum(
        len(message["content"]) for message in attempt["request_messages"]
    )
    assert attempt["truncated"] is False
    # A single create() call was made (no spurious retry).
    assert len(stub.chat.completions.calls) == 1


def test_corrective_retry_records_two_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad-then-good records both attempts: attempt 1 keeps its bad body and the
    parse error's class, attempt 2 the good body, and attempt 2's request echoes
    the corrective turn. Each attempt also carries its OWN usage/chars, and the
    interaction-level usage is the per-field SUM across both -- 150+260=410 --
    proving the old "last completion wins" number (which would report only
    260) no longer applies."""
    usage_1 = SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    usage_2 = SimpleNamespace(prompt_tokens=200, completion_tokens=60, total_tokens=260)
    stub = _install(
        monkeypatch,
        _StubClient(contents=["not json", _SAMPLE_JSON], usages=[usage_1, usage_2]),
    )
    asyncio.run(generate_structured("SYS", "USR", _Sample, workflow="enrich"))

    summary = _only_summary()
    assert summary["usage"] == {
        "prompt_tokens": 300,
        "completion_tokens": 110,
        "total_tokens": 410,
    }

    record = llm_log.get_record(summary["id"])
    assert record is not None
    assert record["outcome"] == "ok"
    assert len(record["attempts"]) == 2
    attempt_1, attempt_2 = record["attempts"]
    assert attempt_1["response_content"] == "not json"
    assert attempt_1["error"] == "JSONDecodeError"
    assert attempt_2["response_content"] == _SAMPLE_JSON
    assert attempt_2["error"] is None
    # Attempt 2 sent system + user + the echoed bad reply + the corrective turn.
    assert len(attempt_2["request_messages"]) == 4
    assert attempt_2["request_messages"][2] == {
        "role": "assistant",
        "content": "not json",
    }

    # Per-attempt usage: each attempt keeps its OWN completion's numbers, not
    # the interaction-level aggregate computed above.
    assert attempt_1["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
    }
    assert attempt_2["usage"] == {
        "prompt_tokens": 200,
        "completion_tokens": 60,
        "total_tokens": 260,
    }
    # Per-attempt chars are the STORED (post-_stored_body) length of the
    # response, and neither attempt was actually oversized here, so neither
    # is flagged truncated.
    assert attempt_1["response_chars"] == len("not json")
    assert attempt_2["response_chars"] == len(_SAMPLE_JSON)
    assert attempt_1["request_chars"] > 0
    # Attempt 2's request is strictly longer: it carries everything attempt 1
    # did (system + user) PLUS the echoed bad reply and the corrective turn.
    assert attempt_2["request_chars"] > attempt_1["request_chars"]
    assert attempt_1["truncated"] is False
    assert attempt_2["truncated"] is False
    assert stub.chat.completions.calls[0]["model"] == _MODEL


def test_timeout_outcome_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow endpoint that trips the wall-clock deadline records outcome timeout
    with the safe Timeout category as its error, AND classifies the in-flight
    attempt itself with the same "Timeout" category. Without the explicit
    recorder.fail_current_attempt call in the TimeoutError handler, the attempt
    would keep its error=None default forever: asyncio.timeout's deadline
    expiry arrives as a CancelledError, a BaseException none of the attempt
    loop's own except clauses catch, so control never reaches any of them --
    it jumps straight past to the outer handler -- even though the record's
    own outcome/error correctly reads timeout."""
    stub = _install(monkeypatch, _StubClient(content=_SAMPLE_JSON, delay=1.0), timeout=0.05)
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_structured("s", "u", _Sample, workflow="capture"))
    summary = _only_summary()
    assert summary["outcome"] == "timeout"
    assert summary["error"].startswith("Timeout: ")
    # The attempt was begun before the deadline cut it off.
    assert len(stub.chat.completions.calls) == 1
    record = llm_log.get_record(summary["id"])
    assert record is not None
    assert len(record["attempts"]) == 1
    assert record["attempts"][0]["error"] == "Timeout"
    assert record["attempts"][0]["response_content"] is None


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


def test_router_detail_sanitizes_lone_surrogate_in_response(client: TestClient) -> None:
    """A response body carrying a lone (unpaired) surrogate must not 500 the
    detail endpoint: Starlette's JSONResponse.render encodes strictly
    (.encode("utf-8"), no errors= override), so an unsanitized surrogate would
    raise UnicodeEncodeError while building the HTTP response. _utf8_safe
    replaces it with U+FFFD at record_response time, well before this request
    ever runs, so the fetch succeeds (200) and reads back the replacement
    character rather than the raw surrogate."""
    _record(workflow="capture", response="開頭正常\ud800結尾正常")
    log_id = client.get("/api/llm/logs").json()["logs"][0]["id"]
    detail = client.get(f"/api/llm/logs/{log_id}")
    assert detail.status_code == 200
    body = detail.json()["attempts"][0]["response_content"]
    assert "\ufffd" in body
    assert "\ud800" not in body
    assert "開頭正常" in body
    assert "結尾正常" in body
