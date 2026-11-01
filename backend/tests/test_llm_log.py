"""Tests for the LLM interaction log (afterthread.services.llm_log) and its
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
import errno
import json
import logging
import os
import re
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import openai
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from afterthread.config import Settings
from afterthread.services import llm_log, tool_builder
from afterthread.services.llm import (
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
    tools_advertised: list[str] | None = None,
) -> llm_log.LlmInteractionRecorder:
    """Build, populate and finalize one record straight through the recorder.

    ``tools_advertised`` None means the kwarg is NOT passed to ``begin_attempt``
    at all (exercising the default every pre-existing caller relies on), rather
    than passed explicitly as None.
    """
    recorder = llm_log.LlmInteractionRecorder(workflow=workflow, model=model)
    attempt_messages = messages or [{"role": "user", "content": "hi"}]
    if tools_advertised is None:
        recorder.begin_attempt(attempt_messages)
    else:
        recorder.begin_attempt(attempt_messages, tools_advertised=tools_advertised)
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


def test_attempt_without_tools_kwarg_records_none() -> None:
    """An attempt begun the way every tool-less caller begins one -- no
    ``tools_advertised`` kwarg at all -- carries None end to end, not [], so
    "this round advertised nothing" stays distinguishable in the detail
    payload."""
    _record()
    log_id = llm_log.list_summaries(1)[0]["id"]
    record = llm_log.get_record(log_id)
    assert record is not None
    assert record["attempts"][0]["tools_advertised"] is None


def test_attempt_tools_advertised_is_recorded_as_a_copy() -> None:
    """The advertised names reach the detail payload, and the recorder holds a
    COPY: the caller derives one list per interaction and reuses it across every
    round, so mutating it afterwards must not rewrite an already-recorded
    attempt."""
    names = ["alpha", "beta"]
    recorder = llm_log.LlmInteractionRecorder(workflow="capture", model="m")
    recorder.begin_attempt([{"role": "user", "content": "hi"}], tools_advertised=names)
    recorder.record_response("out")
    recorder.finish(outcome="ok", error=None)

    names.append("gamma")
    names[0] = "MUTATED"

    log_id = llm_log.list_summaries(1)[0]["id"]
    record = llm_log.get_record(log_id)
    assert record is not None
    assert record["attempts"][0]["tools_advertised"] == ["alpha", "beta"]


# --- stored-body size cap (_stored_body) ------------------------------------


def test_stored_body_at_or_under_cap_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body at or under llm_log_body_max_chars round-trips byte-for-byte,
    untruncated -- the cap must never touch a body that already fits."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_body_max_chars=1000))
    text = "x" * 1000
    stored, truncated = llm_log._stored_body(text, llm_log._SecretSnapshot([], True))
    assert stored == text
    assert truncated is False


def test_stored_body_over_cap_is_hard_cut_with_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body over the cap is cut to EXACTLY the cap, with the truncation
    marker appended in place of the last characters (not merely a bare
    slice), and reports truncated=True."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_body_max_chars=1000))
    stored, truncated = llm_log._stored_body("y" * 5000, llm_log._SecretSnapshot([], True))
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
    stored, truncated = llm_log._stored_body("z" * 100, llm_log._SecretSnapshot([], True))
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
    # "sys" (3 chars) plus the capped user message (1000 chars). The aggregate
    # budget (F5) leaves this untouched: collapsing the 3-char "sys" into a
    # ~20-char marker would ENLARGE the record, so no elision happens.
    assert attempt["request_chars"] == len("sys") + 1000


def test_begin_attempt_bounds_total_request_size_with_elision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-body cap does not bound the AGGREGATE, so begin_attempt applies the
    same knob (llm_log_body_max_chars) as a TOTAL budget across an attempt's
    request_messages (F5): an agentic call records the whole accumulated
    conversation every round, which would otherwise grow the record quadratically.
    Older messages past the budget collapse into ONE synthetic leading marker; the
    newest message is kept verbatim and the attempt is flagged truncated."""
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_body_max_chars=1000, llm_log_max_entries=50),
    )
    llm_log._reset_for_tests()
    # Six 400-char messages: sum 2400 >> the 1000 total budget, yet each is under
    # the per-body cap, so ONLY the aggregate bound can catch this.
    messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": chr(ord("a") + i) * 400}
        for i in range(6)
    ]
    recorder = llm_log.LlmInteractionRecorder(workflow="capture", model="m")
    recorder.begin_attempt(messages)
    recorder.finish(outcome="ok", error=None)

    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    attempt = record["attempts"][0]
    rms = attempt["request_messages"]
    # A single synthetic leading system marker replaces the elided older messages.
    assert rms[0]["role"] == "system"
    assert "省略" in rms[0]["content"]
    # The newest message is kept verbatim (most useful for debugging THIS attempt).
    assert rms[-1] == messages[-1]
    # Aggregate stored size is bounded by the budget plus that one marker entry.
    assert sum(len(message["content"]) for message in rms) <= 1000 + len(rms[0]["content"])
    # The marker names how many messages it elided, and elision sets truncated.
    elided = len(messages) - (len(rms) - 1)  # total minus (kept, excluding the marker)
    assert str(elided) in rms[0]["content"]
    assert attempt["truncated"] is True
    assert attempt["request_chars"] == sum(len(message["content"]) for message in rms)


def test_begin_attempt_small_conversation_stored_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conversation whose stored bodies sum UNDER the total budget is stored
    EXACTLY as before -- byte for byte, no marker, zero behavior change (F5). This
    is the "budget untouched" case the quadratic-growth fix must never disturb."""
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_body_max_chars=1000, llm_log_max_entries=50),
    )
    llm_log._reset_for_tests()
    messages = [
        {"role": "system", "content": "S" * 100},
        {"role": "user", "content": "U" * 100},
        {"role": "assistant", "content": "A" * 100},
    ]
    recorder = llm_log.LlmInteractionRecorder(workflow="capture", model="m")
    recorder.begin_attempt(messages)
    recorder.finish(outcome="ok", error=None)

    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    attempt = record["attempts"][0]
    assert attempt["request_messages"] == messages  # byte-identical, no marker
    assert attempt["request_chars"] == 300
    assert attempt["truncated"] is False


# --- known-value secret redaction (_redact, provider-injected, D36) ---------


def test_redaction_masks_known_secret_in_request_and_response(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered secret value is replaced by the redaction marker in BOTH a
    request message and the response, in the ring AND on the way to the JSONL
    sink: the ONE choke point (_stored_body) covers every stored body, and the
    JSONL line is written from that stored record, so the secret never lands in
    memory OR on disk."""
    secret = "kb-live-key-abcdef123456"
    log_file = tmp_path / "llm.jsonl"
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_file=str(log_file), llm_log_max_entries=50),
    )
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({secret}, True))
    llm_log._reset_for_tests()

    _record(
        workflow="tool_install",
        messages=[
            {"role": "system", "content": "build a tool"},
            {"role": "user", "content": f"the key is {secret} use it"},
        ],
        response=f"tested with {secret} and it worked",
    )

    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    attempt = record["attempts"][0]
    user_message = attempt["request_messages"][1]["content"]
    assert secret not in user_message
    assert llm_log._REDACTION_MARKER in user_message
    assert attempt["response_content"] is not None
    assert secret not in attempt["response_content"]
    assert llm_log._REDACTION_MARKER in attempt["response_content"]
    # The JSONL line holds the marker too -- the secret never on disk.
    file_text = log_file.read_text(encoding="utf-8")
    assert secret not in file_text
    assert llm_log._REDACTION_MARKER in file_text


def test_a_body_that_spells_a_secret_only_as_written_reaches_no_byte_of_the_jsonl_file(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PEM registered from a single-quoted ``.env`` line keeps ``\\n`` as two
    characters; the bodies hold the key with a real line break, which a JSON line
    writes as exactly those two characters. Each such body is stored as the
    marker, and no byte of the file holds the secret."""
    secret = "kb-pem-head\\nkb-pem-body"
    text = "it signs with kb-pem-head\nkb-pem-body"
    log_file = tmp_path / "llm.jsonl"
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_file=str(log_file), llm_log_max_entries=50),
    )
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({secret}, True))
    llm_log._reset_for_tests()

    _record(messages=[{"role": "user", "content": text}], response=text)

    assert secret.encode("utf-8") not in log_file.read_bytes()
    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    attempt = record["attempts"][0]
    assert attempt["request_messages"][0]["content"] == llm_log._REDACTION_MARKER
    assert attempt["response_content"] == llm_log._REDACTION_MARKER


def _decoded_strings(value: Any) -> list[str]:
    """Every string in a decoded JSON value, the keys of its objects included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for key, item in value.items() for s in (key, *_decoded_strings(item))]
    if isinstance(value, list):
        return [s for item in value for s in _decoded_strings(item)]
    return []


@pytest.mark.parametrize("entry", ["response", "request message", "advertised name"])
def test_a_text_that_spells_a_secret_only_as_stored_reaches_no_string_the_detail_api_serves(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """The text as received holds a lone surrogate where the known value has three
    U+FFFD, so masking has nothing to find; ``_utf8_safe`` then makes the stored
    string exactly the value. Its JSON literal writes the value's quote as ``\\"``,
    so a check of the literal alone passes it. A response, a request message and an
    advertised name go through the same choke point, and each is stored as the
    redaction marker: no string the detail API serves holds the value."""
    secret = 'abc\ufffd\ufffd\ufffd"'
    text = 'abc\udcff"'
    assert secret not in text
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({secret}, True))
    llm_log._reset_for_tests()

    if entry == "response":
        _record(response=text)
    elif entry == "request message":
        _record(messages=[{"role": "user", "content": text}])
    else:
        _record(tools_advertised=[text])

    log_id = client.get("/api/llm/logs").json()["logs"][0]["id"]
    served = client.get(f"/api/llm/logs/{log_id}")
    assert served.status_code == 200
    detail = served.json()
    attempt = detail["attempts"][0]
    if entry == "response":
        assert attempt["response_content"] == llm_log._REDACTION_MARKER
    elif entry == "request message":
        assert attempt["request_messages"][0]["content"] == llm_log._REDACTION_MARKER
    else:
        assert attempt["tools_advertised"] == [llm_log._REDACTION_MARKER]
    # Decoded first: the response's own text escapes the quote, as a JSON literal
    # does, so it would not hold the value even when a string served is the value.
    assert [s for s in _decoded_strings(detail) if secret in s] == []


def test_advertised_tool_names_go_through_the_same_redaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A tool NAME that equals a registered secret value is masked in the record
    and in the detail payload, exactly like a body.

    The names used to be copied straight into the attempt, so they were the one
    stored field that bypassed the redaction choke point -- and a name CAN
    legitimately be a registered value (a hand-edited ``.env`` holding
    ``TOKEN=kbsearch`` while ``kbsearch`` is an installed tool). Ordinary names
    are untouched, and the JSONL line holds the masked name too."""
    secret = "kbsearch-tool"  # also the name of an installed tool
    log_file = tmp_path / "llm.jsonl"
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_file=str(log_file), llm_log_max_entries=50),
    )
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({secret}, True))
    llm_log._reset_for_tests()

    _record(workflow="capture", tools_advertised=[secret, "notes"])

    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    names = record["attempts"][0]["tools_advertised"]
    assert names == [llm_log._REDACTION_MARKER, "notes"]
    # The JSONL line holds the masked name too -- the value never on disk.
    file_text = log_file.read_text(encoding="utf-8")
    assert secret not in file_text


def test_advertised_tool_names_keep_none_and_empty_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routing the names through ``_stored_body`` must not blur the two answers
    that mean different things: None (no ``tools`` parameter rode on this attempt
    at all) stays None, and [] (a tools parameter rode, but nothing in it carried
    a readable name) stays []."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({"kb-live-key-abcdef"}, True))
    llm_log._reset_for_tests()

    _record(workflow="capture", tools_advertised=[])
    _record(workflow="enrich")  # the kwarg is not passed at all

    summaries = llm_log.list_summaries(2)
    without_kwarg = llm_log.get_record(summaries[0]["id"])
    empty_list = llm_log.get_record(summaries[1]["id"])
    assert without_kwarg is not None and empty_list is not None
    assert without_kwarg["attempts"][0]["tools_advertised"] is None
    assert empty_list["attempts"][0]["tools_advertised"] == []


def test_one_secret_sweep_per_attempt_however_many_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The known-secret provider is asked ONCE per recorded step, not once per
    string.

    The provider is ``tools.known_secret_values``: an ``iterdir`` plus a ``stat``
    per installed package, plus a ``.env`` read on every cache miss -- and this
    runs on the EVENT LOOP, before the request goes out. One sweep per name made
    an attempt advertising N tools do N sweeps over N packages, quadratic
    filesystem work to mask a handful of short names. Counted rather than timed,
    because the count is the property; the masking itself is asserted in the same
    breath so the cheaper shape cannot be mistaken for a weaker one."""
    calls: list[int] = []
    secret = "kbsearch-tool"  # also the name of an installed tool

    def counting_provider() -> tuple[set[str], bool]:
        calls.append(1)
        return {secret}, True

    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))
    monkeypatch.setattr(llm_log, "_secret_provider", counting_provider)
    llm_log._reset_for_tests()

    recorder = llm_log.LlmInteractionRecorder(workflow="capture", model="m")
    recorder.begin_attempt(
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USR"}],
        tools_advertised=[secret, *[f"tool{index}" for index in range(20)]],
    )
    assert calls == [1]  # 2 messages + 21 names, ONE sweep

    recorder.record_response("out")
    assert len(calls) == 2  # the response is a separate step, a whole round trip later
    recorder.finish(outcome="ok", error=None)

    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    names = record["attempts"][0]["tools_advertised"]
    assert names[0] == llm_log._REDACTION_MARKER  # still masked off the shared snapshot
    assert names[1] == "tool0"


def test_advertised_names_are_bounded_and_the_record_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one field of a record that used to bypass every size discipline.

    Nothing caps how many packages a tools directory holds, so the array could be
    arbitrarily long in the ring AND the JSONL sink while every body beside it was
    capped twice. Both ceilings are pinned -- the COUNT (a name may store zero
    characters, which no character budget can bound) and the aggregate CHARACTER
    budget (``llm_log_body_max_chars``, the same knob ``_apply_total_budget``
    re-uses for the message bodies) -- and so is the thing that makes a cut
    honest: a trailing marker naming how many were dropped, plus the attempt's own
    ``truncated`` flag. Silently short lists are how a reader concludes the model
    was offered three tools when it was offered three hundred."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))
    llm_log._reset_for_tests()

    def _last_attempt() -> dict[str, Any]:
        record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
        assert record is not None
        return record["attempts"][0]

    over_count = [f"tool{index}" for index in range(llm_log._MAX_TOOLS_ADVERTISED + 7)]
    _record(workflow="capture", tools_advertised=over_count)
    attempt = _last_attempt()
    names = attempt["tools_advertised"]
    assert names[: llm_log._MAX_TOOLS_ADVERTISED] == over_count[: llm_log._MAX_TOOLS_ADVERTISED]
    assert names[-1] == llm_log._names_elision_marker(7)  # kept in advertisement order
    assert len(names) == llm_log._MAX_TOOLS_ADVERTISED + 1
    assert attempt["truncated"] is True

    # The character half, at a name length production can actually produce
    # (``tools._NAME_RE`` admits at most 64 characters). 20 of these exhaust the
    # budget EXACTLY, which is the case that used to publish an over-budget
    # record: the marker was appended after the last character was already spent,
    # so the stored total came to budget + len(marker). The bound is asserted on
    # the SUM of what was stored, marker included -- the number the ring and the
    # JSONL sink actually pay -- rather than on the shape of the list.
    budget = 1000
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_body_max_chars=budget, llm_log_max_entries=50),
    )
    llm_log._reset_for_tests()

    real_names = [f"kb-search-{index:02d}-{'a' * 37}" for index in range(25)]
    assert {len(name) for name in real_names} == {50}
    _record(workflow="capture", tools_advertised=real_names)
    attempt = _last_attempt()
    names = attempt["tools_advertised"]
    assert sum(len(name) for name in names) <= budget
    assert names[:-1] == real_names[:19]  # one name given back to make room
    assert names[-1] == llm_log._names_elision_marker(6)  # ... and the count says so
    assert attempt["truncated"] is True

    # A single name longer than the whole budget still gets stored (``_stored_body``
    # capped it at the budget itself, so the first entry always fits) -- a length no
    # package name can have, kept only because that invariant is real code.
    llm_log._reset_for_tests()
    _record(workflow="capture", tools_advertised=["a" * 900, "b" * 900, "c" * 900])
    attempt = _last_attempt()
    assert attempt["tools_advertised"] == ["a" * 900, llm_log._names_elision_marker(2)]
    assert sum(len(name) for name in attempt["tools_advertised"]) <= budget
    assert attempt["truncated"] is True

    # An ordinary list is stored EXACTLY as before -- no marker, no flag.
    _record(workflow="capture", tools_advertised=["alpha", "beta"])
    attempt = _last_attempt()
    assert attempt["tools_advertised"] == ["alpha", "beta"]
    assert attempt["truncated"] is False


def test_advertised_tool_name_cut_for_size_sets_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name cut by the size stage folds into the attempt's own ``truncated``
    flag rather than being dropped silently.

    Unreachable in production -- ``tools._NAME_RE`` bounds a name to 64 chars and
    the cap's own floor is 1000 -- which is exactly why it is pinned here: a
    signal discarded because it "cannot fire" is one that later fires unnoticed.
    Driven by lowering the cap, the same way the body-truncation tests do."""
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_body_max_chars=1000, llm_log_max_entries=50),
    )
    llm_log._reset_for_tests()

    _record(workflow="capture", tools_advertised=["short"])
    unflagged = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert unflagged is not None
    assert unflagged["attempts"][0]["truncated"] is False

    _record(workflow="capture", tools_advertised=["n" * 2000])
    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    attempt = record["attempts"][0]
    assert attempt["truncated"] is True
    assert attempt["tools_advertised"][0].endswith(llm_log._BODY_TRUNCATION_MARKER)


def test_process_token_is_stable_and_re_minted_on_a_simulated_restart() -> None:
    """The token names THIS process's id space, so it must be the same string on
    every read within a process and a DIFFERENT one once that id space restarts.

    ``_reset_for_tests`` is what a restart looks like to this module (the ring is
    dropped and ids go back to 0), so re-minting there is a correctness property,
    not a testing convenience: a token that survived the reset would vouch for
    ids it no longer describes."""
    before = llm_log.process_token()
    assert llm_log.process_token() == before
    assert before  # opaque, but never empty

    llm_log._reset_for_tests()
    assert llm_log.process_token() != before


def test_redaction_skips_values_shorter_than_min(monkeypatch: pytest.MonkeyPatch) -> None:
    """A too-short (<6 char) provider value is NEVER redacted: masking a 1-5 char
    value would shred ordinary prose, and a real key is never that short."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({"abc", "12345"}, True))  # both < 6
    llm_log._reset_for_tests()

    _record(response="abc appears here and 12345 too")
    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    body = record["attempts"][0]["response_content"]
    assert body == "abc appears here and 12345 too"  # untouched
    assert llm_log._REDACTION_MARKER not in body


def test_redaction_adds_no_form_for_a_value_under_the_floor_though_it_holds_a_crlf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ab\\r\\nc`` is five characters, under the floor: it adds no form -- not
    itself, and not its normalized form -- so a body holding either is stored as
    it is."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({"ab\r\nc"}, True))
    llm_log._reset_for_tests()

    _record(response="x ab\r\nc y ab\nc z")
    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    assert record["attempts"][0]["response_content"] == "x ab\r\nc y ab\nc z"


def test_redaction_masks_a_value_and_its_normalized_form_in_one_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ab\\r\\nab`` and its normalized form ``ab\\nab`` overlap in the body: the
    value's occurrence ends where the form's begins. Both are found on the body as
    it is and their ranges merged, so one marker covers the two, and the body's
    other line breaks stay as they were."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({"ab\r\nab"}, True))
    llm_log._reset_for_tests()

    _record(response="head\r\nab\r\nab\nab tail\r\n")
    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    body = record["attempts"][0]["response_content"]
    assert body == f"head\r\n{llm_log._REDACTION_MARKER} tail\r\n"


def test_redaction_runs_before_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redaction runs BEFORE the size cut, so a secret straddling the truncation
    edge is fully masked and never half-survives. Positioned so that -- had
    truncation run first -- a recognizable 10-char prefix of the secret would sit
    inside the cap and leak; redact-first removes the whole secret up front."""
    secret = "SECRETABCDEFGHIJ"  # 16 chars; its first 10 = "SECRETABCD"
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_body_max_chars=1000, llm_log_max_entries=50),
    )
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({secret}, True))
    llm_log._reset_for_tests()
    # 990 filler + the 16-char secret + trailing: the secret spans indices
    # 990..1006, so truncate-first would keep secret[:10] inside the 1000 cap.
    _record(response="x" * 990 + secret + "y" * 500)

    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    body = record["attempts"][0]["response_content"]
    assert secret not in body
    assert "SECRETABCD" not in body  # not even the prefix truncate-first would leak


def test_a_provider_that_raises_does_not_break_the_recording_and_stores_the_body_unrecorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that RAISES must never break recording (the no-observer-failure
    invariant): the interaction is still recorded, with its outcome. But a failure
    to ask says nothing about what the secrets are, so the body is not checked
    against nothing as if there were none: it is stored as the marker saying it
    was not recorded."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))

    def _boom() -> tuple[set[str], bool]:
        raise RuntimeError("secret provider exploded")

    monkeypatch.setattr(llm_log, "_secret_provider", _boom)
    llm_log._reset_for_tests()

    _record(response="a normal body with sk-secret-value inside")  # must not raise
    summary = llm_log.list_summaries(1)[0]
    record = llm_log.get_record(summary["id"])
    assert record is not None
    assert summary["outcome"] == "ok"
    assert record["attempts"][0]["response_content"] == llm_log._UNCHECKED_MARKER


def test_a_provider_whose_values_raise_midyield_stores_the_body_unrecorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider values are materialized INSIDE the snapshot's guard, so a
    LAZY iterable that raises PARTWAY through iteration -- not at call time --
    does not break the recording. What was yielded before it raised is not the
    whole list either, so the body is stored as the marker saying it was not
    recorded."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))

    def _values() -> Any:
        yield "first-secret-value-abcdef"
        raise RuntimeError("iterator exploded mid-yield")

    monkeypatch.setattr(llm_log, "_secret_provider", lambda: (_values(), True))
    llm_log._reset_for_tests()

    _record(response="body with first-secret-value-abcdef inside")  # must not raise
    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    assert record["attempts"][0]["response_content"] == llm_log._UNCHECKED_MARKER


def test_redaction_filters_non_string_provider_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """F5: a provider yielding non-str values (a misconfigured provider) must not raise
    in the ``in``/``replace`` -- non-strings are filtered out, and the str values are
    still masked."""
    secret = "real-secret-value-abcdef"
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ([None, 123456, secret], True))
    llm_log._reset_for_tests()

    _record(response=f"body with {secret} inside")  # must not raise
    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    body = record["attempts"][0]["response_content"]
    assert secret not in body
    assert llm_log._REDACTION_MARKER in body


def test_redaction_longest_first_leaves_no_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """F4: overlapping secrets are replaced LONGEST-first, so masking the shorter one
    (a prefix of the longer) never leaves the longer's suffix exposed. Had the shorter
    run first, 'XYZ789' would survive after 'abcdef' was masked inside the longer."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({"abcdef", "abcdefXYZ789"}, True))
    llm_log._reset_for_tests()

    _record(response="key=abcdefXYZ789 end")
    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    body = record["attempts"][0]["response_content"]
    assert "abcdefXYZ789" not in body
    assert "XYZ789" not in body  # the suffix a shorter-first pass would have leaked
    assert body.count(llm_log._REDACTION_MARKER) == 1


def test_redaction_masks_trailing_prefix_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    """F1: a stored body ENDING with a >=6-char prefix of a known secret -- a secret cut
    by an upstream boundary before _redact runs, so the full value never appears -- is
    masked at the tail, mirroring tools.redact_known_secrets."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({"abcdefGHIJKLMN"}, True))
    llm_log._reset_for_tests()

    _record(response="body ends with abcdefGHIJ")  # first 10 chars of the secret
    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    body = record["attempts"][0]["response_content"]
    assert "abcdefGHIJ" not in body
    assert body.endswith(llm_log._REDACTION_MARKER)


def test_redaction_ranges_computed_on_original_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """F1/H1: mask ranges are computed on the PRISTINE body (mirroring
    tools.redact_known_secrets), so the full-value pass cannot blind the trailing-fragment
    guard. With a SHORT secret sitting inside a LONG secret's truncated prefix, a body
    ending in a 12-char prefix of the long secret must not leak a 6+-char run -- the old
    replace-first order masked the ``GHIJKL`` occurrence and stranded ``ABCDEF``."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({"ABCDEFGHIJKLmnop", "GHIJKL"}, True))
    llm_log._reset_for_tests()

    _record(response="tail=ABCDEFGHIJKL")  # 12-char prefix of the long secret
    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    body = record["attempts"][0]["response_content"]
    assert "ABCDEF" not in body  # no 6+-char run of the long secret's chars survives
    assert body.endswith(llm_log._REDACTION_MARKER)


def test_a_masking_failure_does_not_break_the_recording_and_stores_the_body_unrecorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_mask_known_secrets`` runs inside guards -- a MemoryError (or
    any other surprise) from the masking itself must not escape into the recorder.
    ``_redact`` keeps the text when it fails; the last check, of the text as
    stored and as written, fails the same way, and a body it could not check is
    stored as the marker saying it was not recorded: no secret was found, none was
    looked for."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({"real-secret-value-abcdef"}, True))

    def _boom(text: str, secrets: list[str]) -> str:
        raise MemoryError("mask blew up")

    monkeypatch.setattr(llm_log, "_mask_known_secrets", _boom)
    llm_log._reset_for_tests()

    _record(response="a normal body, must not raise")  # must not raise
    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    assert record["attempts"][0]["response_content"] == llm_log._UNCHECKED_MARKER


def test_a_list_that_is_not_whole_stores_every_body_unrecorded_and_keeps_the_rest(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider says the values it gives are not all of them -- a ``.env`` could
    not be read. Every body is stored as the marker saying it was not recorded: the
    request messages, the advertised names and the response, in the ring and in
    the JSONL file, and none of their text reaches either. What the record says
    about the call itself is kept: its outcome, its usage, how many attempts."""
    log_file = tmp_path / "llm.jsonl"
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_file=str(log_file), llm_log_max_entries=50),
    )
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({"kb-live-key-abcdef"}, False))
    llm_log._reset_for_tests()

    recorder = llm_log.LlmInteractionRecorder(workflow="capture", model="m")
    recorder.begin_attempt([{"role": "user", "content": "plain words"}], tools_advertised=["notes"])
    recorder.record_usage(
        SimpleNamespace(usage={"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8})
    )
    recorder.record_response("plain answer")
    recorder.finish(outcome="ok", error=None)

    summary = llm_log.list_summaries(1)[0]
    record = llm_log.get_record(summary["id"])
    assert record is not None
    attempt = record["attempts"][0]
    assert attempt["request_messages"][0]["content"] == llm_log._UNCHECKED_MARKER
    assert attempt["tools_advertised"] == [llm_log._UNCHECKED_MARKER]
    assert attempt["response_content"] == llm_log._UNCHECKED_MARKER
    assert (summary["outcome"], summary["attempts"]) == ("ok", 1)
    assert summary["usage"] == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}
    file_text = log_file.read_text(encoding="utf-8")
    assert "plain words" not in file_text
    assert "plain answer" not in file_text


def test_a_whole_list_with_no_secrets_keeps_every_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """No known secrets, and that is all of them: nothing to mask, nothing
    withheld. The empty list that is whole is not the empty list that is not. Not
    red at HEAD, which kept every body; red for a marker stored on any empty list."""
    monkeypatch.setattr(llm_log, "get_settings", lambda: Settings(llm_log_max_entries=50))
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: (set(), True))
    llm_log._reset_for_tests()

    _record(messages=[{"role": "user", "content": "plain words"}], response="plain answer")

    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    attempt = record["attempts"][0]
    assert attempt["request_messages"][0]["content"] == "plain words"
    assert attempt["response_content"] == "plain answer"


def test_the_last_check_stores_a_body_unrecorded_for_a_list_that_is_not_whole() -> None:
    """The last stage on its own: a body that holds nothing a partial list knows is
    still not called clean, since the values the list lacks are unknown. The
    recorder tests above show the same through the whole pipeline."""
    whole = llm_log._SecretSnapshot(["kb-live-key-abcdef"], True)
    partial = llm_log._SecretSnapshot(["kb-live-key-abcdef"], False)
    check = llm_log._checked_as_stored_and_written

    assert check("plain words", whole) == "plain words"
    assert check("plain words", partial) == llm_log._UNCHECKED_MARKER
    assert check("", partial) == llm_log._UNCHECKED_MARKER


def test_the_last_check_keeps_a_body_only_when_neither_form_holds_a_known_value() -> None:
    """The last stage on its own, with a whole list. The string as stored is what
    the ring, the detail API and the AI 日誌 page hand on; its JSON literal is what
    a JSONL line writes. A body that holds a known value in only one of the two is
    the redaction marker either way, and one that holds none in either is kept."""
    # Three U+FFFD and a quote. The literal writes the quote as \", so of the two
    # forms of a body that is this value, only the string as stored holds it.
    known_as_stored = 'abc\ufffd\ufffd\ufffd"'
    # A backslash and an n, which is how the literal writes a real line break, so
    # of a body holding that line break, only the literal holds this value.
    known_as_written = "kb-pem-head\\nkb-pem-body"
    secrets = llm_log._SecretSnapshot(sorted([known_as_stored, known_as_written]), True)
    check = llm_log._checked_as_stored_and_written

    def literal(text: str) -> str:
        return json.dumps(text, ensure_ascii=False)[1:-1]

    stored_only = known_as_stored
    written_only = "it signs with kb-pem-head\nkb-pem-body"
    assert known_as_stored not in literal(stored_only)
    assert known_as_written not in written_only and known_as_written in literal(written_only)

    assert check(stored_only, secrets) == llm_log._REDACTION_MARKER
    assert check(written_only, secrets) == llm_log._REDACTION_MARKER
    assert check("plain words", secrets) == "plain words"


def test_the_last_check_counts_the_start_of_a_known_value_ending_the_stored_string() -> None:
    """The string as stored is checked with the same matcher as its literal, so the
    start of a known value at its end counts there too: the end of a body may be
    where a value was cut off. This body ends with the first seven characters of the
    value, a quote among them, which the literal writes as ``\\"``, so only the
    string as stored ends with them; the value is not whole in either."""
    known = 'abc\ufffd\ufffd\ufffd"-and-the-rest'
    body = 'it ends abc\ufffd\ufffd\ufffd"'
    secrets = llm_log._SecretSnapshot([known], True)
    literal = json.dumps(body, ensure_ascii=False)[1:-1]
    assert known not in body
    assert llm_log._mask_known_secrets(literal, secrets.values) == literal

    assert llm_log._checked_as_stored_and_written(body, secrets) == llm_log._REDACTION_MARKER


def test_a_secret_found_and_a_body_not_checked_are_told_apart_in_the_log(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two recordings of the same body. With the whole list, the body spells a
    known secret as written, so it is stored as the marker saying a secret was
    found. With a list that is not whole, nothing was found -- nothing could be
    looked for -- so it is stored as the marker saying it was not recorded, which
    a reader cannot take for a secret having been there. Red before the fix: the
    list that was not whole stored the marker saying a secret was found."""
    secret = "kb-pem-head\\nkb-pem-body"
    text = "it signs with kb-pem-head\nkb-pem-body"
    log_file = tmp_path / "llm.jsonl"
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_file=str(log_file), llm_log_max_entries=50),
    )
    stored: list[str] = []
    for complete in (True, False):
        monkeypatch.setattr(llm_log, "_secret_provider", lambda c=complete: ({secret}, c))
        llm_log._reset_for_tests()
        _record(response=text)
        record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
        assert record is not None
        stored.append(record["attempts"][0]["response_content"])

    found, unchecked = stored
    assert found == llm_log._REDACTION_MARKER
    assert unchecked != llm_log._REDACTION_MARKER
    assert unchecked == llm_log._UNCHECKED_MARKER
    assert secret.encode("utf-8") not in log_file.read_bytes()


@pytest.mark.parametrize("name", ["_REDACTION_MARKER", "_UNCHECKED_MARKER"])
def test_no_key_or_token_can_spell_a_marker(name: str) -> None:
    """Each marker is a CJK body in the •••[…]••• fence, and no run of ASCII
    characters in it is as long as the redactor's floor: a value of ASCII
    characters, as keys and tokens are, can neither equal a marker nor be found
    in one, so masking never cuts into a marker and a marker is never read as a
    value. Red before the fix for the marker saying a body was not recorded, which
    did not exist; the redaction marker was already written this way."""
    marker = getattr(llm_log, name)

    assert marker.startswith("•••[") and marker.endswith("]•••")
    assert max(map(len, re.findall(r"[\x00-\x7f]+", marker)), default=0) < llm_log._MIN_SECRET_LEN


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


# --- a JSONL line checked as it is appended ---------------------------------


def _file_sink_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Turn the JSONL sink on at a file under ``tmp_path`` and start an empty log."""
    log_file = tmp_path / "llm.jsonl"
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(llm_log_file=str(log_file), llm_log_max_entries=50),
    )
    llm_log._reset_for_tests()
    return log_file


def _a_record(**overrides: Any) -> llm_log.LlmInteractionRecord:
    """A finished record built by hand, every field set: two attempts, the second
    with no response, escapes and non-ASCII in the texts."""
    fields: dict[str, Any] = {
        "id": 7,
        "workflow": "capture",
        "model": "gpt-x",
        "started_at": "2026-10-31T01:02:03.456789+00:00",
        "finished_at": "2026-10-31T01:02:04.000001+00:00",
        "duration_ms": 543,
        "outcome": "upstream_error",
        "error": "APIConnectionError: the upstream LLM request failed",
        "usage": {"prompt_tokens": 123456, "completion_tokens": 5, "total_tokens": 123461},
        "attempts": [
            llm_log.LlmAttempt(
                request_messages=[
                    {"role": "system", "content": 'sys "引號"\n'},
                    {"role": "user", "content": "使用者\t\\"},
                ],
                request_chars=999,  # not the sum: the line keeps what the record holds
                response_content="回覆\x01",
                response_chars=3,
                error="ValidationError",
                usage={"prompt_tokens": 1, "completion_tokens": None, "total_tokens": None},
                truncated=True,
                tools_advertised=["notes", "kb"],
            ),
            llm_log.LlmAttempt(request_messages=[{"role": "user", "content": "again"}]),
        ],
    }
    fields.update(overrides)
    return llm_log.LlmInteractionRecord(**fields)


def test_a_jsonl_line_in_which_nothing_known_occurs_is_the_detail_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Nothing known occurs: the line is ``json.dumps`` of the detail and a new
    line, byte for byte, every field as the ring holds it -- counts that do not
    add up, a missing response, names, errors and usage included."""
    log_file = _file_sink_at(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({"kb-live-key-abcdef"}, True))
    record = _a_record()

    llm_log._write_file_sink(record)

    expected = json.dumps(llm_log._record_detail(record), ensure_ascii=False) + "\n"
    assert log_file.read_bytes() == expected.encode("utf-8")


def test_a_known_value_only_in_a_timestamp_or_a_count_leaves_the_jsonl_line_as_the_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The known values lie wholly in fixed content: a stretch of the start time,
    and a token count with its key. The line holds them, and is the detail byte
    for byte: no text of it is the marker."""
    log_file = _file_sink_at(monkeypatch, tmp_path)
    known = {"2026-10-31T01:02", '"prompt_tokens": 123456'}
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: (known, True))
    record = _a_record()

    llm_log._write_file_sink(record)

    expected = json.dumps(llm_log._record_detail(record), ensure_ascii=False) + "\n"
    assert log_file.read_bytes() == expected.encode("utf-8")


def test_a_known_value_from_a_key_into_a_body_leaves_markers_in_the_jsonl_line_not_the_ring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The value begins in the key and quote before a message body and runs into
    the body: the body alone does not hold it, the line as appended does. Every
    text of that line is the marker -- the bodies, the response, the names, the
    model -- its counts are the marker's, and the backend's own words stay. The
    ring and the detail API keep the record as it was stored."""
    secret = 'content": "DO-NOT-STORE-12345'
    body = "DO-NOT-STORE-12345 more"
    log_file = _file_sink_at(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({secret}, True))

    _record(
        workflow="capture",
        model="gpt-x",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": body}],
        response="plain answer",
        tools_advertised=["notes"],
    )

    assert secret.encode("utf-8") not in log_file.read_bytes()
    line = json.loads(log_file.read_text(encoding="utf-8"))
    marker = llm_log._REDACTION_MARKER
    attempt = line["attempts"][0]
    assert [m["role"] for m in attempt["request_messages"]] == ["system", "user"]
    assert [m["content"] for m in attempt["request_messages"]] == [marker, marker]
    assert (attempt["response_content"], attempt["tools_advertised"]) == (marker, [marker])
    assert (attempt["request_chars"], attempt["response_chars"]) == (2 * len(marker), len(marker))
    assert (line["workflow"], line["model"], line["outcome"]) == ("capture", marker, "ok")
    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    kept = record["attempts"][0]
    assert [m["content"] for m in kept["request_messages"]] == ["sys", body]
    assert kept["request_chars"] == len("sys") + len(body)
    assert (kept["response_content"], record["model"]) == ("plain answer", "gpt-x")


def test_a_known_value_ending_in_the_new_line_after_the_jsonl_line_reaches_no_byte_of_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The line is checked with the new line the sink appends after it. The value
    runs from the end of the last advertised name through the closing brackets
    into that new line -- three characters of the name, fewer than the mask's
    trailing guard covers -- so only a check of the line with its new line sees
    it, and the name is the marker in the line."""
    secret = 'abc"]}]}\n'
    log_file = _file_sink_at(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({secret}, True))

    _record(tools_advertised=["tool-abc"])

    assert secret.encode("utf-8") not in log_file.read_bytes()
    line = json.loads(log_file.read_text(encoding="utf-8"))
    assert line["attempts"][0]["tools_advertised"] == [llm_log._REDACTION_MARKER]
    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    assert record["attempts"][0]["tools_advertised"] == ["tool-abc"]


def test_a_jsonl_line_whose_known_value_takes_in_a_field_nothing_vouched_for_is_not_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A field the line does not know -- one a later change adds to the detail --
    is free text no marker replaces. A known value in it is still there once every
    text is the marker, so the line is not written, and one warning says so with
    the record's id and nothing it holds. The ring keeps the record."""
    secret = "kb-extra-field-secret-abcdef"
    log_file = _file_sink_at(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_log, "_secret_provider", lambda: ({secret}, True))
    detail = llm_log._record_detail
    monkeypatch.setattr(
        llm_log, "_record_detail", lambda record: {**detail(record), "extra": f"holds {secret}"}
    )

    with caplog.at_level(logging.WARNING):
        _record(messages=[{"role": "user", "content": "plain words"}])

    assert not log_file.exists() or log_file.read_bytes() == b""
    assert "llm log file sink left out record" in caplog.text
    assert secret not in caplog.text
    assert "plain words" not in caplog.text
    assert len(llm_log.list_summaries(10)) == 1


def test_the_jsonl_line_is_checked_with_every_value_the_record_knew(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The value is known while the attempt and its response are recorded, and no
    longer when the line is written. The line is checked with it still: the seam
    it makes with a key is found, and the body is the marker in the line."""
    secret = 'content": "DO-NOT-STORE-12345'
    calls: list[int] = []

    def provider() -> tuple[set[str], bool]:
        calls.append(1)
        return ({secret} if len(calls) <= 2 else set()), True

    log_file = _file_sink_at(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_log, "_secret_provider", provider)

    _record(messages=[{"role": "user", "content": "DO-NOT-STORE-12345 more"}], response="ok")

    assert len(calls) == 3  # the attempt, the response, the line
    assert secret.encode("utf-8") not in log_file.read_bytes()
    line = json.loads(log_file.read_text(encoding="utf-8"))
    assert line["attempts"][0]["request_messages"][0]["content"] == llm_log._REDACTION_MARKER


def test_a_list_not_whole_when_the_jsonl_line_is_written_keeps_every_text_of_the_line_unrecorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Whole while the steps are recorded, not whole when the line is written: no
    text of the line can be called clean. Every text of it is the marker saying it
    was not recorded -- the model and the error too -- while the backend's words,
    the usage and the counts of what the line holds stay. The ring keeps every
    body as it was stored."""
    calls: list[int] = []

    def provider() -> tuple[set[str], bool]:
        calls.append(1)
        return {"kb-live-key-abcdef"}, len(calls) <= 2

    log_file = _file_sink_at(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_log, "_secret_provider", provider)

    _record(
        model="gpt-x",
        messages=[{"role": "user", "content": "plain words"}],
        response="plain answer",
        outcome="upstream_error",
        error="APIConnectionError: the upstream LLM request failed",
    )

    unchecked = llm_log._UNCHECKED_MARKER
    line = json.loads(log_file.read_text(encoding="utf-8"))
    attempt = line["attempts"][0]
    assert (line["model"], line["error"]) == (unchecked, unchecked)
    assert (line["workflow"], line["outcome"]) == ("capture", "upstream_error")
    assert attempt["request_messages"] == [{"role": "user", "content": unchecked}]
    assert attempt["response_content"] == unchecked
    assert (attempt["request_chars"], attempt["response_chars"]) == (len(unchecked),) * 2
    record = llm_log.get_record(llm_log.list_summaries(1)[0]["id"])
    assert record is not None
    assert record["attempts"][0]["request_messages"][0]["content"] == "plain words"
    assert record["attempts"][0]["response_content"] == "plain answer"


# --- JSONL file-sink rotation (D34) -----------------------------------------


def test_file_sink_rotates_when_oversized(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the JSONL file exceeds llm_log_file_max_bytes, the next write renames
    it aside with a UTC-timestamp suffix and appends the new record to a FRESH
    file at the original path (D34)."""
    log_file = tmp_path / "llm.jsonl"
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(
            llm_log_file=str(log_file), llm_log_file_max_bytes=1_000_000, llm_log_max_entries=50
        ),
    )
    llm_log._reset_for_tests()
    # Pre-fill PAST the cap so the next append triggers exactly one rotation.
    old_content = "x" * 1_000_050 + "\n"
    log_file.write_text(old_content, encoding="utf-8")

    _record(workflow="capture", response="fresh line after rotation")

    # The original path now holds ONLY the new record (a fresh file).
    fresh = log_file.read_text(encoding="utf-8").splitlines()
    assert len(fresh) == 1
    assert "fresh line after rotation" in fresh[0]
    # Exactly one rotated segment exists, carrying the old oversized content...
    rotated = [p for p in tmp_path.iterdir() if p.name.startswith("llm.jsonl.")]
    assert len(rotated) == 1
    assert rotated[0].read_text(encoding="utf-8") == old_content
    # ...named with the UTC-timestamp suffix YYYYMMDD-HHMMSSZ.
    suffix = rotated[0].name[len("llm.jsonl.") :]
    assert re.fullmatch(r"\d{8}-\d{6}Z", suffix)


def test_file_sink_rotation_failure_still_appends(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotation whose rename fails is fully suppressed (same never-break-the-
    caller contract as the sink): the record is still appended -- onto the
    oversized file -- and no exception escapes."""
    log_file = tmp_path / "llm.jsonl"
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(
            llm_log_file=str(log_file), llm_log_file_max_bytes=1_000_000, llm_log_max_entries=50
        ),
    )
    llm_log._reset_for_tests()
    log_file.write_text("x" * 1_000_050 + "\n", encoding="utf-8")

    def _boom_rename(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("rename not permitted")

    monkeypatch.setattr(llm_log.os, "rename", _boom_rename)

    _record(workflow="capture", response="appended despite rotation failure")  # must not raise

    # No rotated file was created; the new line went onto the same file.
    rotated = [p for p in tmp_path.iterdir() if p.name.startswith("llm.jsonl.")]
    assert rotated == []
    assert "appended despite rotation failure" in log_file.read_text(encoding="utf-8")
    # The record also survived in the ring.
    assert len(llm_log.list_summaries(10)) == 1


def test_file_sink_rotation_same_second_uses_unique_targets(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F8(b): two rotations within the SAME second must not clobber each other. The
    first renames to ``<stamp>``; the second finds that name taken and falls back to
    ``<stamp>-1``. Driven deterministically by PINNING the timestamp (monkeypatching
    _rotation_stamp), not by racing threads, so the uniqueness fallback is exercised
    reliably. Two distinct rotated files result, holding the two DIFFERENT old
    contents -- no clobber."""
    log_file = tmp_path / "llm.jsonl"
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(
            llm_log_file=str(log_file), llm_log_file_max_bytes=1_000_000, llm_log_max_entries=50
        ),
    )
    monkeypatch.setattr(llm_log, "_rotation_stamp", lambda: "20260716-120000Z")
    llm_log._reset_for_tests()

    # First oversized file -> first rotation -> <stamp> (holds the "x" content).
    log_file.write_text("x" * 1_000_050 + "\n", encoding="utf-8")
    _record(workflow="capture", response="first fresh line")
    # Make the fresh file oversized again -> second rotation at the SAME pinned
    # stamp -> the -1 fallback (holds the "y" content).
    log_file.write_text("y" * 1_000_050 + "\n", encoding="utf-8")
    _record(workflow="enrich", response="second fresh line")

    rotated = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("llm.jsonl."))
    assert rotated == ["llm.jsonl.20260716-120000Z", "llm.jsonl.20260716-120000Z-1"]
    # No clobber: each rotated segment kept its own distinct old content.
    assert (tmp_path / "llm.jsonl.20260716-120000Z").read_text(encoding="utf-8").startswith("x")
    assert (tmp_path / "llm.jsonl.20260716-120000Z-1").read_text(encoding="utf-8").startswith("y")
    # The live file holds only the newest record.
    assert "second fresh line" in log_file.read_text(encoding="utf-8")


def test_file_sink_rotation_that_cannot_look_at_its_target_name_rotates_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier segment holds the stamped name, and the ``lstat`` of that name
    fails with EIO: whether anything is there is not known, so this rotation does
    not happen. The earlier segment keeps its content, nothing else is renamed, and
    the record is appended to the oversized file."""
    log_file = tmp_path / "llm.jsonl"
    monkeypatch.setattr(
        llm_log,
        "get_settings",
        lambda: Settings(
            llm_log_file=str(log_file), llm_log_file_max_bytes=1_000_000, llm_log_max_entries=50
        ),
    )
    monkeypatch.setattr(llm_log, "_rotation_stamp", lambda: "20260716-120000Z")
    llm_log._reset_for_tests()
    earlier = tmp_path / "llm.jsonl.20260716-120000Z"
    earlier.write_text("earlier segment\n", encoding="utf-8")
    log_file.write_text("x" * 1_000_050 + "\n", encoding="utf-8")
    real_lstat = os.lstat

    def lstat(target: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if os.fspath(target) == os.fspath(earlier):
            raise OSError(errno.EIO, os.strerror(errno.EIO), os.fspath(target))
        return real_lstat(target, *args, **kwargs)

    monkeypatch.setattr(llm_log.os, "lstat", lstat)

    _record(workflow="capture", response="appended without a rotation")

    assert earlier.read_text(encoding="utf-8") == "earlier segment\n"
    rotated = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("llm.jsonl."))
    assert rotated == ["llm.jsonl.20260716-120000Z"]
    appended = log_file.read_text(encoding="utf-8")
    assert appended.startswith("x" * 1_000_050 + "\n")
    assert "appended without a rotation" in appended


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
    monkeypatch.setattr("afterthread.services.llm.get_settings", lambda: settings)
    monkeypatch.setattr("afterthread.services.llm._get_client", lambda: stub)
    monkeypatch.setattr("afterthread.services.llm_log.get_settings", lambda: settings)
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
    monkeypatch.setattr("afterthread.services.llm.get_settings", lambda: settings)
    monkeypatch.setattr("afterthread.services.llm_log.get_settings", lambda: settings)

    def _must_not_build() -> Any:
        raise AssertionError("_get_client must not run when unconfigured")

    monkeypatch.setattr("afterthread.services.llm._get_client", _must_not_build)
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


def test_router_list_carries_this_process_id_space_token(client: TestClient) -> None:
    """R9-1: the list says WHOSE id space its rows' ids belong to.

    A client can be holding an id from a previous backend run -- a `?log=` deep
    link built from a tool job the browser cached and stops refetching once the
    job is terminal -- and every one of those ids resolves here, to an unrelated
    interaction that merely reuses the number. The token is what lets the page
    tell that apart, and it rides on the ENVELOPE (one property of the answering
    process) rather than on each row.

    The restart is produced the way ``_reset_for_tests`` produces one, the same
    way O2-2's sidecar tests do it, rather than by mocking the route."""
    _record(workflow="capture")
    body = client.get("/api/llm/logs").json()
    assert body["process_token"] == llm_log.process_token()
    assert body["process_token"]  # opaque, but never empty

    llm_log._reset_for_tests()
    after_restart = client.get("/api/llm/logs").json()
    assert after_restart["process_token"] != body["process_token"]


def test_router_job_status_names_the_process_that_minted_its_log_id(client: TestClient) -> None:
    """The job's half of the same pairing: an id and the token of its id space.

    Stamped when the job is SERVED rather than stored on it, which holds
    structurally: the job table is in-process memory that dies with the ring and
    its counter, so any id a job can still be served with was minted here. The
    both-or-neither rule the sidecar keeps is kept here too -- no id, no token --
    so the pair can never disagree about whether there is a link to vouch for."""
    linked = tool_builder.InstallJob(
        job_id="job-linked",
        state="succeeded",
        created_at="2026-07-27T00:00:00+00:00",
        finished_at="2026-07-27T00:01:00+00:00",
        tool_name="kb",
        summary="done",
        llm_log_id=7,
    )
    unlinked = tool_builder.InstallJob(
        job_id="job-unlinked",
        state="failed",
        created_at="2026-07-27T00:00:00+00:00",
        finished_at="2026-07-27T00:01:00+00:00",
        error="安裝失敗",
    )
    with tool_builder._JOBS_LOCK:
        tool_builder._JOBS[linked.job_id] = linked
        tool_builder._JOBS[unlinked.job_id] = unlinked
    try:
        with_link = client.get("/api/tools/jobs/job-linked").json()
        without_link = client.get("/api/tools/jobs/job-unlinked").json()
    finally:
        tool_builder._reset_jobs_for_tests()

    assert with_link["llm_log_id"] == 7
    assert with_link["llm_log_process"] == llm_log.process_token()
    assert without_link["llm_log_id"] is None
    assert without_link["llm_log_process"] is None


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


def test_router_detail_exposes_tools_advertised(client: TestClient) -> None:
    """The per-attempt tool names survive the router's response model.

    This is the guard on schemas.LlmLogAttempt: the detail route builds its
    response with ``LlmLogDetail.model_validate(record)``, and pydantic's default
    ``extra="ignore"`` would drop an undeclared key SILENTLY -- the store would
    keep recording the names while the API quietly stopped serving them. Both
    shapes are pinned: null for a round that advertised nothing, and the exact
    list for one that did."""
    _record(workflow="capture")
    _record(workflow="enrich", tools_advertised=["alpha", "beta"])
    logs = client.get("/api/llm/logs").json()["logs"]

    with_tools = client.get(f"/api/llm/logs/{logs[0]['id']}")
    assert with_tools.status_code == 200
    assert with_tools.json()["attempts"][0]["tools_advertised"] == ["alpha", "beta"]

    without_tools = client.get(f"/api/llm/logs/{logs[1]['id']}")
    assert without_tools.status_code == 200
    attempt = without_tools.json()["attempts"][0]
    assert "tools_advertised" in attempt  # present as an explicit null, not omitted
    assert attempt["tools_advertised"] is None


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
