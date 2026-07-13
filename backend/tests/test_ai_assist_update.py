"""Tests for POST /api/items/{item_id}/assist-update (AI assisted update).

LLM interaction is mocked at the service boundary (``generate_json``).
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlmodel import Session

from app.config import Settings
from app.services.llm import LLMUpstreamError


def _create(client: TestClient, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"title": "Sample"} | fields
    response = client.post("/api/items", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _hard_delete_item_via_raw_connection(session: Session, item_id: int) -> None:
    """Delete the item (and its progress entries, mirroring what a real
    `DELETE /api/items/{id}` leaves behind) directly through the shared DBAPI
    connection, committing immediately -- the same technique as
    tests/test_items.py, see `_hard_delete_item_via_raw_connection` there for
    why this reaches the StaticPool connection instead of going through the
    ORM session (whose identity map must not see the delete, exactly as it
    would not see another session's independently committed DELETE).
    """
    bind = session.get_bind()
    assert isinstance(bind, Engine)
    raw_connection = bind.raw_connection()
    try:
        cursor = raw_connection.cursor()
        try:
            cursor.execute("DELETE FROM progress_entry WHERE item_id = ?", (item_id,))
            cursor.execute("DELETE FROM memory_item WHERE id = ?", (item_id,))
        finally:
            cursor.close()
        raw_connection.commit()
    finally:
        raw_connection.close()


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


def test_assist_update_races_with_concurrent_delete_after_llm_returns_404(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent delete landing between the post-await re-fetch and this
    handler's own UPDATE must resolve to the declared 404, never a 500.

    Same deterministic ambush as
    test_ai_enrich.py::test_enrich_races_with_concurrent_delete_after_llm_returns_404
    (see there for the full mechanics): an engine-level `before_cursor_execute`
    hook hard-deletes the row the instant the handler's `UPDATE memory_item`
    is about to execute. Pre-fix, `item.entries.append(...)` autoflushed that
    UPDATE outside the race-wrapped flush (the stubbed result carries a section
    so the item is dirty at the append), so the StaleDataError escaped as an
    unhandled 500; post-fix every write sits inside the wrapped flush -> 404.
    """
    item = _create(client, next_actions="keep")
    item_id = item["id"]
    _patch_generate_json(
        monkeypatch, result={"sections": {"next_actions": "next"}, "progress_note": "n"}
    )

    bind = session.get_bind()
    assert isinstance(bind, Engine)
    ambushed = {"done": False}

    def _delete_item_before_its_update(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: object,
    ) -> None:
        normalized = " ".join(statement.split()).lower()
        if normalized.startswith("update memory_item") and not ambushed["done"]:
            ambushed["done"] = True
            _hard_delete_item_via_raw_connection(session, item_id)

    event.listen(bind, "before_cursor_execute", _delete_item_before_its_update)
    try:
        response = client.post(f"/api/items/{item_id}/assist-update", json={"note": "n"})
    finally:
        event.remove(bind, "before_cursor_execute", _delete_item_before_its_update)

    assert ambushed["done"] is True
    assert response.status_code == 404
    assert response.json()["detail"] == "Memory item not found"

    # The handler's session.rollback() must leave the shared session usable,
    # and the concurrently deleted item is genuinely gone.
    assert client.get("/api/items").status_code == 200
    assert client.get(f"/api/items/{item_id}").status_code == 404


def test_assist_update_empty_note_rejected(client: TestClient) -> None:
    item = _create(client)
    response = client.post(f"/api/items/{item['id']}/assist-update", json={"note": "  "})
    assert response.status_code == 422


def test_assist_update_oversized_note_rejected(client: TestClient) -> None:
    item = _create(client)
    response = client.post(f"/api/items/{item['id']}/assist-update", json={"note": "a" * 20001})
    assert response.status_code == 422
