"""End-to-end AI degradation tests through the full stack.

Unlike the per-endpoint tests that monkeypatch ``generate_structured``, these
configure a working LLM and stub only the low-level client (``_get_client``),
so the *real* generate_structured (its schema-guided parse, ONE corrective
retry, and error taxonomy) runs inside the router. They prove the whole chain:
a configured-but-failing or malformed-output endpoint degrades to 502 with a
safe, config-free detail and no partial rows; a model that corrects itself on
the retry flows through to success; and a realistic fenced-JSON success flows
through to 201.

The old forgiving scavenger is gone, so the scavenger cases that once had to be
salvaged (arrays of objects, juxtaposed objects, garbled arrays, prose-wrapped
objects, scalar junk) are re-expressed here against the strict contract:
bad-then-bad is a 502, bad-then-good succeeds on the retry.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import openai
import pytest
from fastapi.testclient import TestClient

from context_memory.config import Settings

_SECRET_URL = "http://llm.internal.example/v1"
_SECRET_KEY = "sk-secret-do-not-leak"

# Sentinel distinguishing "no raw completion supplied" from an explicit None.
_UNSET = object()

_DRAFT: dict[str, Any] = {
    "title": "端到端草稿",
    "snapshot": "透過 stub client 的完整流程測試。",
    "suggested_status": "capture-quick",
    "tags": ["e2e"],
    "questions": ["需要確認範圍嗎?"],
}
_DRAFT_JSON = json.dumps(_DRAFT, ensure_ascii=False)


class _StubCompletions:
    def __init__(
        self,
        *,
        content: str | None = None,
        contents: list[str] | None = None,
        exc: Exception | None = None,
        completion: Any = _UNSET,
        delay: float = 0.0,
    ) -> None:
        self._content = content
        # A scripted per-call sequence (last element held once exhausted) models
        # a model that corrects itself on the retry, or keeps failing.
        self._contents = list(contents) if contents is not None else None
        self._exc = exc
        # A verbatim completion object models a nonconforming-but-200 body from a
        # merely OpenAI-compatible gateway (missing message, null content, ...).
        self._completion = completion
        # Simulates a slow-drip endpoint that never returns within the caller's
        # wall-clock deadline.
        self._delay = delay
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        if self._completion is not _UNSET:
            return self._completion
        if self._contents is not None:
            content = self._contents[0] if len(self._contents) == 1 else self._contents.pop(0)
        else:
            content = self._content
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _StubClient:
    def __init__(self, **completion_kwargs: Any) -> None:
        self.chat = SimpleNamespace(completions=_StubCompletions(**completion_kwargs))

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.chat.completions.calls


def _install_client(monkeypatch: pytest.MonkeyPatch, stub: _StubClient) -> _StubClient:
    monkeypatch.setattr("context_memory.services.llm._get_client", lambda: stub)
    return stub


def _total(client: TestClient) -> int:
    return client.get("/api/items").json()["total"]


# The scavenger-behaviour cases, re-expressed as bad completions the strict
# contract rejects. Each old scavenger test's shape appears here so the coverage
# is traceable: a bad-then-bad run is a 502 with no rows written.
_BAD_OUTPUTS: list[tuple[str, str]] = [
    ("plain-refusal", "I'm sorry, I cannot help with that."),
    ("array-of-objects", json.dumps([_DRAFT, {**_DRAFT, "title": "second"}], ensure_ascii=False)),
    ("prose-wrapped-array", f"Here are two drafts, pick one:\n{_DRAFT_JSON}\nLet me know!"),
    ("juxtaposed-objects", f"{_DRAFT_JSON} {_DRAFT_JSON}"),
    ("garbled-array", f"[{_DRAFT_JSON}, {{bad}}]"),
    ("trailing-comma-array", f"[{_DRAFT_JSON},]"),
    ("prose-wrapped-object", f"Here is the draft: {_DRAFT_JSON}\nHope this helps!"),
]


def test_capture_end_to_end_upstream_failure_returns_502_no_rows_no_leak(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_llm(base_url=_SECRET_URL, model="m", api_key=_SECRET_KEY)
    # The SDK error text embeds URL/key-like values; none may reach the client.
    leaky = openai.OpenAIError(f"connect {_SECRET_URL} using {_SECRET_KEY} failed")
    stub = _install_client(monkeypatch, _StubClient(exc=leaky))

    response = client.post("/api/capture", json={"raw_text": "raw discussion"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    body = response.text
    assert _SECRET_KEY not in body
    assert _SECRET_URL not in body
    assert "llm.internal.example" not in body
    assert _total(client) == 0
    # A transport failure is NOT retried.
    assert len(stub.calls) == 1


def test_capture_end_to_end_wall_clock_timeout_returns_502_within_bound(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow-drip endpoint that never completes within openai_timeout_seconds
    degrades to 502 close to that configured deadline, not after however long the
    stubbed call takes: the asyncio.timeout around the whole attempt loop in
    generate_structured is a genuine wall-clock bound.
    """
    configure_llm(base_url=_SECRET_URL, model="m", openai_timeout_seconds=0.05)
    _install_client(monkeypatch, _StubClient(delay=2.0, content="unused, never reached"))

    started = time.monotonic()
    response = client.post("/api/capture", json={"raw_text": "raw discussion"})
    elapsed = time.monotonic() - started

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert response.json()["detail"]["message"].startswith("Timeout: ")
    # Bounded by the configured 0.05s deadline, not the stub's 2s delay.
    assert elapsed < 1.0
    assert _total(client) == 0


@pytest.mark.parametrize("bad", [c for _, c in _BAD_OUTPUTS], ids=[n for n, _ in _BAD_OUTPUTS])
def test_capture_end_to_end_invalid_output_retried_then_502_no_rows(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
    bad: str,
) -> None:
    """Every non-conforming shape (a refusal, an array of drafts, juxtaposed
    objects, a garbled/trailing-comma array, a prose-wrapped object) is rejected
    by the strict parser, corrected-prompted once, and -- when the second reply
    is just as bad -- degrades to 502 with NO row written. Pre-refactor these
    shapes were variously scavenged into a silent single-draft persist or a
    bespoke WrongShape 502; now they share one contract. Two create() calls
    prove the retry ran.
    """
    configure_llm(base_url=_SECRET_URL, model="m")
    stub = _install_client(monkeypatch, _StubClient(contents=[bad, bad]))

    response = client.post("/api/capture", json={"raw_text": "raw discussion"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0
    assert len(stub.calls) == 2


def test_capture_end_to_end_bad_then_good_retry_succeeds_201(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that first wraps its object in prose (rejected) and then returns a
    bare conforming object on the corrective retry flows through to 201 with one
    row -- the strict contract's self-correction path, replacing the scavenger's
    prose/scalar-junk salvage. The retry request carries the corrective turn.
    """
    configure_llm(base_url=_SECRET_URL, model="m")
    prose_first = f"Sure! Here is your draft:\n{_DRAFT_JSON}\nAnything else?"
    stub = _install_client(monkeypatch, _StubClient(contents=[prose_first, _DRAFT_JSON]))

    response = client.post("/api/capture", json={"raw_text": "raw discussion"})
    assert response.status_code == 201, response.text
    assert response.json()["item"]["title"] == "端到端草稿"
    assert _total(client) == 1
    assert len(stub.calls) == 2
    # The retry echoed the bad reply and appended a corrective user turn.
    retry_messages = stub.calls[1]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": prose_first}
    assert "corrected JSON object" in retry_messages[-1]["content"]


def test_capture_end_to_end_bare_object_with_bracket_string_returns_201(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single bare object whose string field literally contains "[{}]" parses
    cleanly in ONE attempt: json.loads is string-aware, so the brackets inside a
    string value never confuse the parser (the regression the old scavenger's
    bracket counter got wrong, now moot under strict json.loads).
    """
    configure_llm(base_url=_SECRET_URL, model="m")
    draft = {**_DRAFT, "snapshot": "see the pattern [{}] in the logs"}
    stub = _install_client(monkeypatch, _StubClient(content=json.dumps(draft, ensure_ascii=False)))

    response = client.post("/api/capture", json={"raw_text": "raw discussion"})
    assert response.status_code == 201, response.text
    assert response.json()["item"]["title"] == "端到端草稿"
    assert response.json()["item"]["snapshot"] == "see the pattern [{}] in the logs"
    assert _total(client) == 1
    assert len(stub.calls) == 1  # a clean bare object needs no retry


def test_capture_end_to_end_surrogate_then_clean_retry_succeeds_201(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first reply carrying a lone surrogate in a section fails the CaptureDraft
    sanitizer (_coerce_str -> ValueError -> ValidationError) -- an output-shape
    failure now eligible for the corrective retry. A clean second reply then
    succeeds (201). This is the sanitizer family becoming retryable now that
    validation lives inside generate_structured.
    """
    configure_llm(base_url=_SECRET_URL, model="m")
    # Build valid JSON, then inject a lone surrogate into the snapshot value so
    # json.loads still accepts it but _coerce_str rejects it as non-UTF-8.
    bad = json.dumps({**_DRAFT, "snapshot": "PLACEHOLDER"}, ensure_ascii=False).replace(
        "PLACEHOLDER", "討論內容\ud800結尾"
    )
    stub = _install_client(monkeypatch, _StubClient(contents=[bad, _DRAFT_JSON]))

    response = client.post("/api/capture", json={"raw_text": "raw discussion"})
    assert response.status_code == 201, response.text
    assert response.json()["item"]["title"] == "端到端草稿"
    assert _total(client) == 1
    assert len(stub.calls) == 2


def test_capture_end_to_end_empty_completion_returns_502_no_rows(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_llm(base_url=_SECRET_URL, model="m")
    stub = _install_client(monkeypatch, _StubClient(content="   "))

    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 502
    assert _total(client) == 0
    # An empty completion is transport-shaped, not output-shaped: no retry.
    assert len(stub.calls) == 1


def test_capture_end_to_end_fenced_json_succeeds(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_llm(base_url=_SECRET_URL, model="m")
    fenced = f"```json\n{_DRAFT_JSON}\n```"
    stub = _install_client(monkeypatch, _StubClient(content=fenced))

    response = client.post("/api/capture", json={"raw_text": "raw discussion"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["item"]["title"] == "端到端草稿"
    assert body["item"]["source"] == "llm-capture"
    assert body["questions"] == ["需要確認範圍嗎?"]
    assert _total(client) == 1
    assert len(stub.calls) == 1  # a fenced bare object is accepted, not retried


def test_enrich_end_to_end_upstream_failure_returns_502_and_item_unchanged(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = client.post("/api/items", json={"title": "Keep", "snapshot": "keep me"})
    item_id = created.json()["id"]
    configure_llm(base_url=_SECRET_URL, model="m", api_key=_SECRET_KEY)
    _install_client(monkeypatch, _StubClient(exc=openai.OpenAIError(f"boom {_SECRET_URL}")))

    response = client.post(f"/api/items/{item_id}/enrich", json={"additional_context": "ctx"})
    assert response.status_code == 502
    assert _SECRET_URL not in response.text
    detail = client.get(f"/api/items/{item_id}").json()
    assert detail["snapshot"] == "keep me"
    assert [entry["note"] for entry in detail["progress"]] == ["建立項目"]


def test_enrich_end_to_end_all_empty_then_good_retry_succeeds_200(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An all-empty first result fails EnrichResult._require_signal (a
    ValidationError) and is corrected on the retry: a meaningful second result
    flows through to 200 and is written. The empty-result validator family is
    now retryable end to end.
    """
    created = client.post("/api/items", json={"title": "Keep", "snapshot": "keep me"})
    item_id = created.json()["id"]
    configure_llm(base_url=_SECRET_URL, model="m")
    good = json.dumps(
        {"sections": {"decisions": "採用方案A"}, "progress_note": "補充決策"}, ensure_ascii=False
    )
    stub = _install_client(monkeypatch, _StubClient(contents=["{}", good]))

    response = client.post(f"/api/items/{item_id}/enrich", json={"additional_context": "ctx"})
    assert response.status_code == 200, response.text
    assert response.json()["item"]["decisions"] == "採用方案A"
    assert len(stub.calls) == 2


def test_capture_end_to_end_malformed_endpoint_returns_503_no_rows_no_leak(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A syntactically malformed endpoint URL (invalid port) makes AsyncOpenAI
    raise at CONSTRUCTION -- httpx.InvalidURL, whose text embeds the bad port.
    The real _get_client/_build_client run here (NOT stubbed) so construction
    genuinely fails; that misconfiguration degrades to 503 with no row written,
    and no fragment of the URL appears in the response body or the logs.
    """
    configure_llm(base_url="http://internal-llm:8o80/v1", model="m", api_key=_SECRET_KEY)
    with caplog.at_level(logging.DEBUG):
        response = client.post("/api/capture", json={"raw_text": "raw discussion"})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "llm_not_configured"
    for sink in (response.text, caplog.text):
        assert "8o80" not in sink
        assert _SECRET_KEY not in sink
        assert "internal-llm" not in sink
    assert _total(client) == 0


@pytest.mark.parametrize(
    "completion",
    [
        SimpleNamespace(choices=None),
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices=[SimpleNamespace()]),
        SimpleNamespace(choices=[SimpleNamespace(message=None)]),
    ],
    ids=["choices-none", "choices-empty", "choice-without-message", "message-none"],
)
def test_capture_end_to_end_nonconforming_200_returns_502_no_rows(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
    completion: Any,
) -> None:
    """A compatible gateway returning a 200 whose body violates the SDK shape
    (choices None/empty/non-list, a choice with no message, a null message) must
    degrade to 502 with no row written -- not crash with an AttributeError 500.
    """
    configure_llm(base_url=_SECRET_URL, model="m")
    _install_client(monkeypatch, _StubClient(completion=completion))

    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0


def test_capture_end_to_end_deeply_nested_brackets_returns_502_no_rows(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content of tens of thousands of nested ``[`` makes json.loads raise
    RecursionError inside the strict parser. That is caught as an output-shape
    failure (retried, then a repeated failure) and mapped to a 502 with no row
    written, never an unhandled 500.
    """
    configure_llm(base_url=_SECRET_URL, model="m")
    deep = "[" * 50000 + "]" * 50000
    stub = _install_client(monkeypatch, _StubClient(content=deep))

    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0
    assert len(stub.calls) == 2
