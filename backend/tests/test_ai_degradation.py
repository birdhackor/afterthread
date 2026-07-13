"""End-to-end AI degradation tests through the full stack.

Unlike the per-endpoint tests that monkeypatch ``generate_json``, these
configure a working LLM and stub only the low-level client (``_get_client``),
so the *real* generate_json (its JSON extraction and error taxonomy) runs
inside the router. They prove the whole chain: a configured-but-failing or
garbage-returning endpoint degrades to 502 with a safe, config-free detail and
no partial rows, while a realistic fenced-JSON success flows through to 201.
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

from app.config import Settings

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


class _StubCompletions:
    def __init__(
        self,
        *,
        content: str | None = None,
        exc: Exception | None = None,
        completion: Any = _UNSET,
        delay: float = 0.0,
    ) -> None:
        self._content = content
        self._exc = exc
        # A verbatim completion object models a nonconforming-but-200 body from a
        # merely OpenAI-compatible gateway (missing message, null content, ...).
        self._completion = completion
        # Simulates a slow-drip endpoint that never returns within the caller's
        # wall-clock deadline -- see
        # test_capture_end_to_end_wall_clock_timeout_returns_502_within_bound.
        self._delay = delay

    async def create(self, **kwargs: Any) -> Any:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        if self._completion is not _UNSET:
            return self._completion
        message = SimpleNamespace(content=self._content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _StubClient:
    def __init__(self, **completion_kwargs: Any) -> None:
        self.chat = SimpleNamespace(completions=_StubCompletions(**completion_kwargs))


def _install_client(monkeypatch: pytest.MonkeyPatch, stub: _StubClient) -> None:
    monkeypatch.setattr("app.services.llm._get_client", lambda: stub)


def _total(client: TestClient) -> int:
    return client.get("/api/items").json()["total"]


def test_capture_end_to_end_upstream_failure_returns_502_no_rows_no_leak(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_llm(base_url=_SECRET_URL, model="m", api_key=_SECRET_KEY)
    # The SDK error text embeds URL/key-like values; none may reach the client.
    leaky = openai.OpenAIError(f"connect {_SECRET_URL} using {_SECRET_KEY} failed")
    _install_client(monkeypatch, _StubClient(exc=leaky))

    response = client.post("/api/capture", json={"raw_text": "raw discussion"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    body = response.text
    assert _SECRET_KEY not in body
    assert _SECRET_URL not in body
    assert "llm.internal.example" not in body
    assert _total(client) == 0


def test_capture_end_to_end_wall_clock_timeout_returns_502_within_bound(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow-drip endpoint that never completes within openai_timeout_seconds
    must still degrade to 502 close to that configured deadline, not after
    however long the stubbed upstream call actually takes: the asyncio.timeout
    wrapped around the SDK call in generate_json is a genuine wall-clock bound,
    unlike the SDK/httpx client-level timeout alone (per-phase inactivity; see
    app.services.llm._build_client).
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


def test_capture_end_to_end_unparseable_returns_502_no_rows(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_llm(base_url=_SECRET_URL, model="m")
    _install_client(monkeypatch, _StubClient(content="I'm sorry, I cannot help with that."))

    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0


def test_capture_end_to_end_array_of_objects_returns_502_no_rows(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A top-level JSON array of objects (each one individually shaped like a
    plausible draft) must not let the balanced-brace fallback inside
    _extract_json_object silently extract and persist just the first element.
    The real generate_json runs here (only _get_client is stubbed), so this
    proves the whole chain rejects the array as a whole: 502, no row written.
    """
    configure_llm(base_url=_SECRET_URL, model="m")
    array_content = json.dumps([_DRAFT, {**_DRAFT, "title": "second element"}], ensure_ascii=False)
    _install_client(monkeypatch, _StubClient(content=array_content))

    response = client.post("/api/capture", json={"raw_text": "raw discussion"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0


def test_capture_end_to_end_prose_wrapped_array_of_objects_returns_502_no_rows(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The array-of-objects rejection above must hold even when the array is
    wrapped in prose rather than being the model's whole answer: prose makes
    the full-text parse fail inside the real generate_json, so this exercises
    the candidate-scan fallback rather than the top-level-shape check --
    proving the whole chain still rejects the array as a whole instead of the
    fallback silently extracting and persisting just its first element.
    """
    configure_llm(base_url=_SECRET_URL, model="m")
    array_json = json.dumps([_DRAFT, {**_DRAFT, "title": "second element"}], ensure_ascii=False)
    prose = f"Here are two drafts, pick one:\n{array_json}\nLet me know!"
    _install_client(monkeypatch, _StubClient(content=prose))

    response = client.post("/api/capture", json={"raw_text": "raw discussion"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0


def test_capture_end_to_end_prose_with_innocent_bracket_before_object_returns_201(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An innocent scalar bracket ahead of the real object in prose (e.g. a
    footnote-style "[1]") must not block the real generate_json from finding
    the object that follows it: the candidate scan skips the scalar array and
    the object flows through to a normal 201, one row written.
    """
    configure_llm(base_url=_SECRET_URL, model="m")
    draft_json = json.dumps(_DRAFT, ensure_ascii=False)
    prose = f"Answer[1]: {draft_json}"
    _install_client(monkeypatch, _StubClient(content=prose))

    response = client.post("/api/capture", json={"raw_text": "raw discussion"})
    assert response.status_code == 201, response.text
    assert response.json()["item"]["title"] == "端到端草稿"
    assert _total(client) == 1


def test_capture_end_to_end_empty_completion_returns_502_no_rows(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_llm(base_url=_SECRET_URL, model="m")
    _install_client(monkeypatch, _StubClient(content="   "))

    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 502
    assert _total(client) == 0


def test_capture_end_to_end_fenced_json_succeeds(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_llm(base_url=_SECRET_URL, model="m")
    fenced = f"```json\n{json.dumps(_DRAFT, ensure_ascii=False)}\n```"
    _install_client(monkeypatch, _StubClient(content=fenced))

    response = client.post("/api/capture", json={"raw_text": "raw discussion"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["item"]["title"] == "端到端草稿"
    assert body["item"]["source"] == "llm-capture"
    assert body["questions"] == ["需要確認範圍嗎?"]
    assert _total(client) == 1


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


def test_capture_end_to_end_malformed_endpoint_returns_503_no_rows_no_leak(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A syntactically malformed endpoint URL (invalid port) makes AsyncOpenAI
    raise at CONSTRUCTION -- httpx.InvalidURL, whose text embeds the bad port.
    The real _get_client/_build_client run here (NOT stubbed) so construction
    genuinely fails; that misconfiguration must degrade to 503 with no row
    written, and no fragment of the URL may appear in the response body or the
    logs. Pre-fix, the InvalidURL escaped generate_json's OpenAIError handler as
    an unhandled 500 whose traceback logged "Invalid port: '8o80'".
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
    RecursionError inside the real _extract_json_object. That must map to the
    unparseable 502 with no row written, never escape as an unhandled 500.
    """
    configure_llm(base_url=_SECRET_URL, model="m")
    deep = "[" * 50000 + "]" * 50000
    _install_client(monkeypatch, _StubClient(content=deep))

    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0
