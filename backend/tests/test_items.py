"""Tests for the memory item CRUD and progress endpoints."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models import MemoryItem, ProgressEntry


def _create(client: TestClient, **fields: object) -> dict:
    payload = {"title": "Sample"} | fields
    response = client.post("/api/items", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_applies_defaults(client: TestClient) -> None:
    item = _create(client)
    assert item["id"] > 0
    assert item["source"] == "manual"
    assert item["confidence"] == "mixed"
    assert item["tags"] == []
    assert item["status"] == "capture-quick"
    assert item["stage"] == "quick"
    assert item["snapshot"] == ""
    assert item["is_stale"] is False
    assert item["created"] is not None
    assert item["updated"] is not None


def test_create_strips_title(client: TestClient) -> None:
    item = _create(client, title="  Padded Title  ")
    assert item["title"] == "Padded Title"


def test_create_first_progress_entry_seeded(client: TestClient) -> None:
    item = _create(client)
    detail = client.get(f"/api/items/{item['id']}").json()
    assert [entry["note"] for entry in detail["progress"]] == ["建立項目"]


def test_create_empty_title_rejected(client: TestClient) -> None:
    assert client.post("/api/items", json={"title": ""}).status_code == 422
    assert client.post("/api/items", json={"title": "   "}).status_code == 422


def test_create_invalid_status_rejected(client: TestClient) -> None:
    assert client.post("/api/items", json={"title": "x", "status": "bogus"}).status_code == 422


def test_create_invalid_stage_rejected(client: TestClient) -> None:
    assert client.post("/api/items", json={"title": "x", "stage": "bogus"}).status_code == 422


def test_list_pagination_and_total(client: TestClient) -> None:
    for index in range(5):
        _create(client, title=f"Item {index}")

    first_page = client.get("/api/items", params={"limit": 2, "offset": 0}).json()
    assert first_page["total"] == 5
    assert len(first_page["items"]) == 2

    last_page = client.get("/api/items", params={"limit": 2, "offset": 4}).json()
    assert last_page["total"] == 5
    assert len(last_page["items"]) == 1


def test_list_sorted_updated_desc(client: TestClient) -> None:
    first = _create(client, title="First")
    second = _create(client, title="Second")
    items = client.get("/api/items").json()["items"]
    # Newest first.
    assert items[0]["id"] == second["id"]
    assert items[1]["id"] == first["id"]


def test_list_limit_out_of_range_rejected(client: TestClient) -> None:
    assert client.get("/api/items", params={"limit": 0}).status_code == 422
    assert client.get("/api/items", params={"limit": 201}).status_code == 422


def test_filter_by_status(client: TestClient) -> None:
    _create(client, title="Active one", status="active")
    _create(client, title="Parked one", status="parked")
    result = client.get("/api/items", params={"status": "active"}).json()
    assert result["total"] == 1
    assert result["items"][0]["title"] == "Active one"


def test_filter_by_stage(client: TestClient) -> None:
    _create(client, title="Quick one", stage="quick")
    _create(client, title="Full one", stage="full")
    result = client.get("/api/items", params={"stage": "full"}).json()
    assert result["total"] == 1
    assert result["items"][0]["title"] == "Full one"


def test_filter_by_tag(client: TestClient) -> None:
    _create(client, title="Work item", tags=["work", "urgent"])
    _create(client, title="Home item", tags=["home"])
    # A tag that is a superstring must not match ("work" != "homework").
    _create(client, title="Homework item", tags=["homework"])
    result = client.get("/api/items", params={"tag": "work"}).json()
    assert result["total"] == 1
    assert result["items"][0]["title"] == "Work item"


def test_q_search_matches_title(client: TestClient) -> None:
    _create(client, title="Findable Alpha")
    _create(client, title="Unrelated Beta")
    # Case-insensitive substring.
    result = client.get("/api/items", params={"q": "alpha"}).json()
    assert result["total"] == 1
    assert result["items"][0]["title"] == "Findable Alpha"


def test_q_search_matches_snapshot(client: TestClient) -> None:
    _create(client, title="Snap", snapshot="A distinctive phrase here")
    _create(client, title="Other")
    result = client.get("/api/items", params={"q": "distinctive phrase"}).json()
    assert result["total"] == 1
    assert result["items"][0]["title"] == "Snap"


def test_q_search_matches_recovery_keywords(client: TestClient) -> None:
    _create(client, title="Keyword holder", recovery_keywords="needle in haystack")
    _create(client, title="Other")
    result = client.get("/api/items", params={"q": "needle"}).json()
    assert result["total"] == 1
    assert result["items"][0]["title"] == "Keyword holder"


def test_get_missing_returns_404(client: TestClient) -> None:
    assert client.get("/api/items/9999").status_code == 404


def test_patch_partial_update_bumps_updated(client: TestClient, session: Session) -> None:
    item = _create(client)
    stored = session.get(MemoryItem, item["id"])
    assert stored is not None
    old = datetime.now(UTC) - timedelta(days=5)
    stored.updated = old
    session.add(stored)
    session.commit()

    response = client.patch(f"/api/items/{item['id']}", json={"snapshot": "updated text"})
    assert response.status_code == 200
    body = response.json()
    assert body["snapshot"] == "updated text"
    assert datetime.fromisoformat(body["updated"]) > old


def test_empty_patch_does_not_bump_updated(client: TestClient, session: Session) -> None:
    item = _create(client)
    stored = session.get(MemoryItem, item["id"])
    assert stored is not None
    fixed = datetime(2020, 1, 1, tzinfo=UTC)
    stored.updated = fixed
    session.add(stored)
    session.commit()

    response = client.patch(f"/api/items/{item['id']}", json={})
    assert response.status_code == 200
    assert datetime.fromisoformat(response.json()["updated"]) == fixed


def test_patch_invalid_enum_rejected(client: TestClient) -> None:
    item = _create(client)
    response = client.patch(f"/api/items/{item['id']}", json={"status": "nope"})
    assert response.status_code == 422


def test_patch_explicit_null_snapshot_rejected(client: TestClient) -> None:
    item = _create(client)
    response = client.patch(f"/api/items/{item['id']}", json={"snapshot": None})
    assert response.status_code == 422


def test_patch_explicit_null_status_rejected(client: TestClient) -> None:
    item = _create(client)
    response = client.patch(f"/api/items/{item['id']}", json={"status": None})
    assert response.status_code == 422


def test_patch_explicit_null_tags_rejected(client: TestClient) -> None:
    item = _create(client)
    response = client.patch(f"/api/items/{item['id']}", json={"tags": None})
    assert response.status_code == 422


def test_patch_explicit_null_does_not_mutate_item(client: TestClient) -> None:
    # A rejected null-payload must not partially apply: the item stays as-is.
    item = _create(client, snapshot="original")
    response = client.patch(f"/api/items/{item['id']}", json={"snapshot": None})
    assert response.status_code == 422

    unchanged = client.get(f"/api/items/{item['id']}").json()
    assert unchanged["snapshot"] == "original"
    assert unchanged["updated"] == item["updated"]


def test_patch_normal_partial_update_leaves_other_fields_untouched(client: TestClient) -> None:
    item = _create(client, source="import", tags=["work"], status="active")
    response = client.patch(f"/api/items/{item['id']}", json={"snapshot": "x"})
    assert response.status_code == 200
    body = response.json()
    assert body["snapshot"] == "x"
    assert body["title"] == item["title"]
    assert body["source"] == "import"
    assert body["tags"] == ["work"]
    assert body["status"] == "active"


def test_patch_missing_returns_404(client: TestClient) -> None:
    assert client.patch("/api/items/9999", json={"status": "active"}).status_code == 404


def test_delete_cascades_progress_entries(client: TestClient, session: Session) -> None:
    item = _create(client)
    item_id = item["id"]
    client.post(f"/api/items/{item_id}/progress", json={"note": "second entry"})

    before = session.exec(select(ProgressEntry).where(ProgressEntry.item_id == item_id)).all()
    assert len(before) == 2  # 建立項目 + second entry

    assert client.delete(f"/api/items/{item_id}").status_code == 204

    session.expire_all()
    after = session.exec(select(ProgressEntry).where(ProgressEntry.item_id == item_id)).all()
    assert after == []
    assert session.get(MemoryItem, item_id) is None


def test_delete_missing_returns_404(client: TestClient) -> None:
    assert client.delete("/api/items/9999").status_code == 404


def test_progress_append_bumps_item_updated(client: TestClient, session: Session) -> None:
    item = _create(client)
    item_id = item["id"]
    stored = session.get(MemoryItem, item_id)
    assert stored is not None
    old = datetime.now(UTC) - timedelta(days=3)
    stored.updated = old
    session.add(stored)
    session.commit()

    response = client.post(f"/api/items/{item_id}/progress", json={"note": "made progress"})
    assert response.status_code == 201
    entry = response.json()
    assert entry["note"] == "made progress"
    assert entry["item_id"] == item_id

    refreshed = client.get(f"/api/items/{item_id}").json()
    assert datetime.fromisoformat(refreshed["updated"]) > old


def test_progress_empty_note_rejected(client: TestClient) -> None:
    item = _create(client)
    response = client.post(f"/api/items/{item['id']}/progress", json={"note": "   "})
    assert response.status_code == 422


def test_progress_missing_item_returns_404(client: TestClient) -> None:
    assert client.post("/api/items/9999/progress", json={"note": "x"}).status_code == 404
