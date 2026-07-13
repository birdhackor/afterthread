"""Tests for POST /api/items/{item_id}/assist-update (AI assisted update).

LLM interaction is mocked at the service boundary (``generate_json``).
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.services.llm import LLMUpstreamError


def _create(client: TestClient, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"title": "Sample"} | fields
    response = client.post("/api/items", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _patch_generate_json(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, Any] | None = None,
    exc: Exception | None = None,
) -> None:
    async def _fake(system: str, user: str) -> dict[str, Any]:
        if exc is not None:
            raise exc
        assert result is not None
        return result

    monkeypatch.setattr("app.services.memory_ai.generate_json", _fake)


def _progress_notes(client: TestClient, item_id: int) -> list[str]:
    detail = client.get(f"/api/items/{item_id}").json()
    return [entry["note"] for entry in detail["progress"]]


def test_assist_update_refreshes_sections_and_appends_progress(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, next_actions="old action")
    _patch_generate_json(
        monkeypatch,
        result={
            "sections": {"next_actions": "下一步B", "open_questions": "問題C"},
            "progress_note": "今天完成了X",
        },
    )
    response = client.post(f"/api/items/{item['id']}/assist-update", json={"note": "完成了 X"})
    assert response.status_code == 200, response.text
    updated = response.json()["item"]

    assert updated["next_actions"] == "下一步B"
    assert updated["open_questions"] == "問題C"
    assert "今天完成了X" in _progress_notes(client, item["id"])


def test_assist_update_uses_fallback_note_when_model_note_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client)
    _patch_generate_json(
        monkeypatch, result={"sections": {"next_actions": "x"}, "progress_note": ""}
    )
    client.post(f"/api/items/{item['id']}/assist-update", json={"note": "note"})
    assert "AI 協助更新" in _progress_notes(client, item["id"])


def test_assist_update_drops_unknown_keys(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, status="active")
    _patch_generate_json(
        monkeypatch,
        result={
            "sections": {"next_actions": "n", "status": "done", "bogus": "x"},
            "progress_note": "p",
        },
    )
    updated = client.post(f"/api/items/{item['id']}/assist-update", json={"note": "note"}).json()[
        "item"
    ]
    assert updated["next_actions"] == "n"
    assert updated["status"] == "active"


def test_assist_update_missing_returns_404(client: TestClient) -> None:
    response = client.post("/api/items/9999/assist-update", json={"note": "n"})
    assert response.status_code == 404


def test_assist_update_unconfigured_returns_503_and_item_unchanged(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    item = _create(client, next_actions="keep")
    configure_llm(base_url="", model="")
    response = client.post(f"/api/items/{item['id']}/assist-update", json={"note": "n"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "llm_not_configured"
    detail = client.get(f"/api/items/{item['id']}").json()
    assert detail["next_actions"] == "keep"
    assert [entry["note"] for entry in detail["progress"]] == ["建立項目"]


def test_assist_update_upstream_error_returns_502_and_item_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, next_actions="keep")
    _patch_generate_json(monkeypatch, exc=LLMUpstreamError("boom"))
    response = client.post(f"/api/items/{item['id']}/assist-update", json={"note": "n"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    detail = client.get(f"/api/items/{item['id']}").json()
    assert detail["next_actions"] == "keep"
    assert [entry["note"] for entry in detail["progress"]] == ["建立項目"]


def test_assist_update_empty_note_rejected(client: TestClient) -> None:
    item = _create(client)
    response = client.post(f"/api/items/{item['id']}/assist-update", json={"note": "  "})
    assert response.status_code == 422


def test_assist_update_oversized_note_rejected(client: TestClient) -> None:
    item = _create(client)
    response = client.post(f"/api/items/{item['id']}/assist-update", json={"note": "a" * 20001})
    assert response.status_code == 422
