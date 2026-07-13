"""End-to-end AI degradation tests through the full stack.

Unlike the per-endpoint tests that monkeypatch ``generate_json``, these
configure a working LLM and stub only the low-level client (``_get_client``),
so the *real* generate_json (its JSON extraction and error taxonomy) runs
inside the router. They prove the whole chain: a configured-but-failing or
garbage-returning endpoint degrades to 502 with a safe, config-free detail and
no partial rows, while a realistic fenced-JSON success flows through to 201.
"""

import json
import logging
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
    ) -> None:
        self._content = content
        self._exc = exc
        # A verbatim completion object models a nonconforming-but-200 body from a
        # merely OpenAI-compatible gateway (missing message, null content, ...).
        self._completion = completion

    async def create(self, **kwargs: Any) -> Any:
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
