"""Tests for the memory item CRUD and progress endpoints."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, event
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import Session, col, select

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


def test_filter_by_tag_chinese_exact_membership(client: TestClient) -> None:
    # ensure_ascii JSON serialization must not break matching non-ASCII tags,
    # and membership must stay exact (no LIKE-style substring over-matching).
    _create(client, title="Chinese tagged", tags=["工作", "付款"])
    _create(client, title="Ascii tagged", tags=["ascii-tag"])

    hit = client.get("/api/items", params={"tag": "工作"}).json()
    assert hit["total"] == 1
    assert hit["items"][0]["title"] == "Chinese tagged"

    # "工" is a substring of "工作" but must NOT match as a distinct tag.
    miss = client.get("/api/items", params={"tag": "工"}).json()
    assert miss["total"] == 0

    ascii_result = client.get("/api/items", params={"tag": "ascii-tag"}).json()
    assert ascii_result["total"] == 1
    assert ascii_result["items"][0]["title"] == "Ascii tagged"


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


def test_q_search_escapes_like_metacharacters(client: TestClient) -> None:
    _create(client, title="Has foo_bar token")
    # "_" is a single-character LIKE wildcard; must not match "X" here.
    _create(client, title="Has fooXbar token")
    # "%" is a multi-character LIKE wildcard; an unescaped q="%" would match
    # every row instead of only rows with a literal "%".
    _create(client, title="Has 100% literal percent")

    underscore_result = client.get("/api/items", params={"q": "foo_bar"}).json()
    assert underscore_result["total"] == 1
    assert underscore_result["items"][0]["title"] == "Has foo_bar token"

    percent_result = client.get("/api/items", params={"q": "%"}).json()
    assert percent_result["total"] == 1
    assert percent_result["items"][0]["title"] == "Has 100% literal percent"


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


def test_patch_races_with_concurrent_delete_returns_404(
    client: TestClient, session: Session
) -> None:
    """If the item is deleted+committed by another session between this
    handler's `session.get()` and its `commit()`, this handler's own UPDATE
    for `item` (setting the changed fields and bumping `updated`) now
    matches zero rows -- the row is already gone. SQLAlchemy reports that as
    StaleDataError, not silence, so the API must translate it into 404
    rather than crash with an unhandled 500.

    Made deterministic without real threads via a `before_flush` *session*
    event (see `test_progress_add_races_with_concurrent_delete_returns_404`
    for why session-level `before_flush` -- firing before this flush has
    emitted any of its own SQL -- is what reproduces genuine ordering,
    versus a mapper-level event that could run after other work in the same
    flush). The delete goes through `session.connection()`, the session's
    own in-transaction connection, bypassing the session/identity map
    exactly as another session's independently committed DELETE would be
    invisible to this one.
    """
    item = _create(client)
    item_id = item["id"]

    def _delete_item_before_flush(
        flush_session: Session, flush_context: object, instances: object
    ) -> None:
        connection = flush_session.connection()
        connection.execute(delete(ProgressEntry).where(col(ProgressEntry.item_id) == item_id))
        connection.execute(delete(MemoryItem).where(col(MemoryItem.id) == item_id))

    event.listen(session, "before_flush", _delete_item_before_flush)
    try:
        response = client.patch(f"/api/items/{item_id}", json={"snapshot": "late"})
    finally:
        event.remove(session, "before_flush", _delete_item_before_flush)

    assert response.status_code == 404
    assert response.json()["detail"] == "Memory item not found"

    # The handler's session.rollback() must leave the shared session usable
    # for later requests, not stuck raising PendingRollbackError.
    assert client.get("/api/items").status_code == 200


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


def test_delete_races_with_concurrent_delete_returns_404(
    client: TestClient, session: Session
) -> None:
    """If the item is deleted+committed by another session between this
    handler's `session.get()` and its `commit()`, this session's own DELETE
    for `item` matches zero rows -- the row is already gone, so
    delete-after-delete should surface as the same 404 a simple not-found
    lookup would give.

    Unlike the UPDATE-based races (see
    `test_progress_add_races_with_concurrent_delete_returns_404` and
    `test_patch_races_with_concurrent_delete_returns_404`), SQLAlchemy's
    unit of work does not raise for a zero-row-matched DELETE unless the
    mapper has a `version_id_col` configured, which `MemoryItem` does not:
    without one, a mismatch here only emits a warning (see the ORM
    `Mapper`'s `confirm_deleted_rows` parameter docs -- "the warning may be
    changed to an exception in a future release"). The handler is hardened
    against StaleDataError anyway, symmetric with the UPDATE-based races and
    forward-compatible with that future behaviour, so this test drives it
    directly: the `before_flush` hook performs the same real
    concurrent-delete simulation used by the races above, then raises
    StaleDataError itself to exercise the handler's translation to 404,
    rather than relying on today's (non-raising) real DELETE path.
    """
    item = _create(client)
    item_id = item["id"]

    def _delete_item_before_flush(
        flush_session: Session, flush_context: object, instances: object
    ) -> None:
        connection = flush_session.connection()
        connection.execute(delete(ProgressEntry).where(col(ProgressEntry.item_id) == item_id))
        connection.execute(delete(MemoryItem).where(col(MemoryItem.id) == item_id))
        raise StaleDataError(
            "DELETE statement on table 'memory_item' expected to delete 1 row(s); 0 were matched."
        )

    event.listen(session, "before_flush", _delete_item_before_flush)
    try:
        response = client.delete(f"/api/items/{item_id}")
    finally:
        event.remove(session, "before_flush", _delete_item_before_flush)

    assert response.status_code == 404
    assert response.json()["detail"] == "Memory item not found"

    # The handler's session.rollback() must leave the shared session usable
    # for later requests, not stuck raising PendingRollbackError.
    assert client.get("/api/items").status_code == 200


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


def test_progress_add_races_with_concurrent_delete_returns_404(
    client: TestClient, session: Session
) -> None:
    """If the item is deleted+committed by another session between this
    handler's `session.get()` and its `commit()`, this session's own flush
    now runs against a row that is already gone. This handler bumps
    `item.updated`, and that UPDATE is what the flush emits first -- before
    the INSERT for the new `entry` -- so an UPDATE matching zero rows is
    what actually happens, which SQLAlchemy reports as StaleDataError. The
    API must translate that into 404, not crash with an unhandled 500 (and
    must still do so on the rarer ordering where the INSERT goes first
    instead, which the FK constraint on `entry.item_id` rejects with
    IntegrityError -- see the handler's `except` clause).

    Made deterministic without real threads via a `before_flush` *session*
    event rather than a mapper-level `before_insert` event: `before_flush`
    fires once, before this flush has emitted any of its own SQL, whereas
    `before_insert` only fires right before its own target's INSERT and so
    can run *after* an unrelated UPDATE earlier in the same flush already
    went out -- too late to reproduce the UPDATE-sees-zero-rows ordering
    this test is for. Deleting the item (and its existing progress entries,
    mirroring what a real concurrent `DELETE /api/items/{id}` leaves
    behind) through `session.connection()` -- the session's own
    in-transaction connection -- bypasses the session/identity map exactly
    as another session's independently committed DELETE would be invisible
    to this one, so the flush's own UPDATE for `item` then matches zero
    rows exactly as if that concurrent delete had already landed.
    """
    item = _create(client)
    item_id = item["id"]

    def _delete_item_before_flush(
        flush_session: Session, flush_context: object, instances: object
    ) -> None:
        connection = flush_session.connection()
        connection.execute(delete(ProgressEntry).where(col(ProgressEntry.item_id) == item_id))
        connection.execute(delete(MemoryItem).where(col(MemoryItem.id) == item_id))

    event.listen(session, "before_flush", _delete_item_before_flush)
    try:
        response = client.post(f"/api/items/{item_id}/progress", json={"note": "late"})
    finally:
        event.remove(session, "before_flush", _delete_item_before_flush)

    assert response.status_code == 404
    assert response.json()["detail"] == "Memory item not found"

    # The handler's session.rollback() must leave the shared session usable
    # for later requests, not stuck raising PendingRollbackError.
    assert client.get("/api/items").status_code == 200
