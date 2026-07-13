"""End-to-end AI degradation tests through the full stack.

Unlike the per-endpoint tests that monkeypatch ``generate_json``, these
configure a working LLM and stub only the low-level client (``_get_client``),
so the *real* generate_json (its JSON extraction and error taxonomy) runs
inside the router. They prove the whole chain: a configured-but-failing or
garbage-returning endpoint degrades to 502 with a safe, config-free detail and
no partial rows, while a realistic fenced-JSON success flows through to 201.
"""

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import openai
import pytest
from fastapi.testclient import TestClient

from app.config import Settings

_SECRET_URL = "http://llm.internal.example/v1"
_SECRET_KEY = "sk-secret-do-not-leak"

_DRAFT: dict[str, Any] = {
    "title": "端到端草稿",
    "snapshot": "透過 stub client 的完整流程測試。",
    "suggested_status": "capture-quick",
    "tags": ["e2e"],
    "questions": ["需要確認範圍嗎?"],
}


class _StubCompletions:
    def __init__(self, *, content: str | None = None, exc: Exception | None = None) -> None:
        self._content = content
        self._exc = exc

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        if self._exc is not None:
            raise self._exc
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
