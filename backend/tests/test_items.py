"""Tests for the memory item CRUD and progress endpoints."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, event
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import Session, col, select

from app.models import MemoryItem, ProgressEntry
from app.routers.items import SQLITE_MAX_INT

# One past SQLite's signed-64-bit INTEGER ceiling (2**63): FastAPI parses this
# fine as a Python int, but binding it into a SQLite query raises OverflowError
# from the driver -- id/offset values this large must be rejected by FastAPI's
# own path/query validation (422) before ever reaching the database.
_OUT_OF_SQLITE_RANGE = SQLITE_MAX_INT + 1


def _create(client: TestClient, **fields: object) -> dict:
    payload = {"title": "Sample"} | fields
    response = client.post("/api/items", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _hard_delete_item_via_raw_connection(session: Session, item_id: int | None) -> None:
    """Delete a `memory_item` row directly through the shared DBAPI
    connection, bypassing the ORM session entirely and committing
    immediately at the DBAPI level. `item_id=None` deletes whichever row
    currently has the highest id -- for callers (see
    `test_create_response_survives_row_deleted_immediately_after_commit`
    below) that don't know the id up front because it's assigned by the
    very request this runs inside of. `ProgressEntry.item_id`'s
    `ondelete="CASCADE"` (see models.py) removes its progress entries too,
    since SQLite enforces that at the database level once `PRAGMA
    foreign_keys=ON` is active for the connection (see
    `app.db.enable_sqlite_foreign_keys`), regardless of how the DELETE is
    issued.

    Used from `after_commit` session-event hooks below to simulate a
    concurrent delete landing in the instant after a handler's own commit --
    i.e. exactly where a post-commit `session.refresh()` used to run next.
    At that point the session itself is in SQLAlchemy's post-commit
    "committed" state and cannot emit SQL through the ORM
    (`session.connection()` raises `InvalidRequestError` there -- "no
    further SQL can be emitted within this transaction"), so this reaches
    the underlying connection directly instead, through the engine's
    StaticPool -- which the `session`/`client` fixtures always use (see
    conftest.py) -- making it genuinely the same connection/database rather
    than an unrelated one.
    """
    bind = session.get_bind()
    # The session/client fixtures (see conftest.py) always bind Session
    # directly to an Engine, never a Connection -- only Engine has
    # raw_connection(), which is what makes reaching the shared StaticPool
    # connection below possible.
    assert isinstance(bind, Engine)
    raw_connection = bind.raw_connection()
    try:
        cursor = raw_connection.cursor()
        try:
            if item_id is None:
                cursor.execute(
                    "DELETE FROM memory_item WHERE id = (SELECT MAX(id) FROM memory_item)"
                )
            else:
                cursor.execute("DELETE FROM memory_item WHERE id = ?", (item_id,))
        finally:
            cursor.close()
        raw_connection.commit()
    finally:
        raw_connection.close()


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


def test_create_response_survives_row_deleted_immediately_after_commit(
    client: TestClient, session: Session
) -> None:
    """create_item must snapshot its response before `session.commit()` and
    return that snapshot with no further session/DB access afterward -- not
    call `session.refresh()` the way it used to. Proven by deleting the new
    row through an `after_commit` hook, i.e. the instant after this
    handler's own commit lands: the old `session.refresh(item)` call at
    that point would hit `InvalidRequestError` ("Could not refresh
    instance") against the now-missing row and turn a create that already
    succeeded into a 500. With `refresh()` gone, the response is built from
    already-in-memory attributes and is unaffected by what happens to the
    row afterward.

    See `_hard_delete_item_via_raw_connection` for why this reaches the
    database directly rather than through the session: at `after_commit`
    time the session itself cannot emit SQL (it is in SQLAlchemy's
    post-commit "committed" state).
    """

    def _delete_after_commit(commit_session: Session) -> None:
        _hard_delete_item_via_raw_connection(commit_session, None)

    event.listen(session, "after_commit", _delete_after_commit)
    try:
        response = client.post("/api/items", json={"title": "Vanishes immediately"})
    finally:
        event.remove(session, "after_commit", _delete_after_commit)

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Vanishes immediately"

    # The row is genuinely gone -- confirms this test would have caught the
    # old session.refresh()-based bug, not exercised a no-op hook.
    assert client.get(f"/api/items/{body['id']}").status_code == 404


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


def test_list_offset_over_sqlite_max_rejected(client: TestClient) -> None:
    """`offset` beyond SQLite's signed-64-bit range must be rejected by
    FastAPI's own query validation (422) before it ever reaches the
    database: binding a Python int this large as the OFFSET parameter raises
    OverflowError from the driver, which would otherwise surface as an
    unhandled 500.
    """
    response = client.get("/api/items", params={"offset": _OUT_OF_SQLITE_RANGE})
    assert response.status_code == 422


def test_list_offset_at_sqlite_max_returns_empty_items_and_correct_total(
    client: TestClient,
) -> None:
    """The maximum in-range offset (SQLite's signed-64-bit ceiling itself)
    must pass FastAPI's query validation and reach the database, where it is
    simply an offset past the end of the result set: an empty `items` page,
    with `total` still reporting the real row count -- unaffected by
    pagination.
    """
    for index in range(3):
        _create(client, title=f"Item {index}")

    response = client.get("/api/items", params={"offset": SQLITE_MAX_INT})
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 3


def test_list_derives_total_and_page_from_single_query(
    client: TestClient, session: Session
) -> None:
    """list_items must derive `total` and the page rows from ONE query (a
    COUNT(*) window), not a separate COUNT followed by a separate SELECT. Two
    queries can straddle a concurrent write and contradict each other -- e.g.
    report total=1 alongside two returned items. A single statement makes the
    page and its total one atomic snapshot. Proven by counting the
    memory_item SELECTs issued while serving a (non-empty) list request:
    exactly one.
    """
    for index in range(3):
        _create(client, title=f"Item {index}")

    bind = session.get_bind()
    assert isinstance(bind, Engine)
    memory_item_selects: list[str] = []

    def _record_memory_item_selects(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: object,
    ) -> None:
        normalized = " ".join(statement.split()).lower()
        if normalized.startswith("select") and "from memory_item" in normalized:
            memory_item_selects.append(normalized)

    event.listen(bind, "after_cursor_execute", _record_memory_item_selects)
    try:
        body = client.get("/api/items", params={"limit": 2, "offset": 0}).json()
    finally:
        event.remove(bind, "after_cursor_execute", _record_memory_item_selects)

    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert len(memory_item_selects) == 1, memory_item_selects


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


def test_get_item_id_over_sqlite_max_rejected(client: TestClient) -> None:
    """`item_id` beyond SQLite's signed-64-bit range must be rejected by
    FastAPI's own path validation (422) before ever reaching the database --
    binding a Python int this large into a SQLite query raises OverflowError
    from the driver, which would otherwise surface as an unhandled 500.
    """
    response = client.get(f"/api/items/{_OUT_OF_SQLITE_RANGE}")
    assert response.status_code == 422


def test_get_item_id_at_sqlite_max_in_range_returns_404(client: TestClient) -> None:
    """The maximum in-range id (SQLite's signed-64-bit ceiling itself) must
    pass FastAPI's path validation and reach the database, where -- absent
    any item with that id -- it is a normal 404, not a validation error.
    """
    response = client.get(f"/api/items/{SQLITE_MAX_INT}")
    assert response.status_code == 404


def test_get_item_loads_progress_in_single_statement(client: TestClient, session: Session) -> None:
    """get_item must load the item and its progress entries in ONE statement
    (joinedload), not a session.get() followed by a lazy load of `entries`.
    Two separate SELECTs leave a gap a concurrent committed DELETE can slip
    into: the item SELECT returns the row, the delete lands, and the entries
    SELECT then comes back empty -- yielding 200 with progress=[], a phantom
    state (creation always seeds one entry). Folding entries into the item
    query removes that gap entirely: the read yields either the full
    item+entries or a 404, never an item with empty progress.

    Proven both structurally -- no standalone `progress_entry` SELECT is
    emitted while serving the request, so there is no second statement for a
    concurrent delete to race -- and behaviorally: the seeded and appended
    entries come back, oldest first (the relationship's order_by).
    """
    item = _create(client)
    item_id = item["id"]
    client.post(f"/api/items/{item_id}/progress", json={"note": "second entry"})

    bind = session.get_bind()
    assert isinstance(bind, Engine)
    entry_lazy_loads: list[str] = []

    def _record_entry_selects(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: object,
    ) -> None:
        normalized = " ".join(statement.split()).lower()
        # A lazy load reads entries with a standalone `... FROM progress_entry
        # WHERE ...`; joinedload instead folds them into the item query as
        # `... JOIN progress_entry ...`, so no such statement should appear.
        if normalized.startswith("select") and "from progress_entry" in normalized:
            entry_lazy_loads.append(normalized)

    event.listen(bind, "after_cursor_execute", _record_entry_selects)
    try:
        response = client.get(f"/api/items/{item_id}")
    finally:
        event.remove(bind, "after_cursor_execute", _record_entry_selects)

    assert response.status_code == 200
    assert [entry["note"] for entry in response.json()["progress"]] == [
        "建立項目",
        "second entry",
    ]
    assert entry_lazy_loads == [], f"unexpected lazy load of entries: {entry_lazy_loads}"


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


def test_patch_item_id_over_sqlite_max_rejected(client: TestClient) -> None:
    """`item_id` beyond SQLite's signed-64-bit range must be rejected by
    FastAPI's own path validation (422) before ever reaching the database --
    binding a Python int this large into a SQLite query raises OverflowError
    from the driver, which would otherwise surface as an unhandled 500.
    """
    response = client.patch(f"/api/items/{_OUT_OF_SQLITE_RANGE}", json={"snapshot": "x"})
    assert response.status_code == 422


def test_patch_item_id_at_sqlite_max_in_range_returns_404(client: TestClient) -> None:
    """The maximum in-range id (SQLite's signed-64-bit ceiling itself) must
    pass FastAPI's path validation and reach the database, where -- absent
    any item with that id -- it is a normal 404, not a validation error.
    """
    response = client.patch(f"/api/items/{SQLITE_MAX_INT}", json={"snapshot": "x"})
    assert response.status_code == 404


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


def test_patch_response_survives_row_deleted_immediately_after_commit(
    client: TestClient, session: Session
) -> None:
    """update_item must snapshot its response before `session.commit()` and
    return that snapshot with no further session/DB access afterward -- not
    call `session.refresh()` the way it used to. Proven the same way as
    `test_create_response_survives_row_deleted_immediately_after_commit`
    above: deleting the row via an `after_commit` hook, right where the old
    `session.refresh(item)` call used to run next and would have hit
    `InvalidRequestError` against the now-missing row.
    """
    item = _create(client)
    item_id = item["id"]

    def _delete_after_commit(commit_session: Session) -> None:
        _hard_delete_item_via_raw_connection(commit_session, item_id)

    event.listen(session, "after_commit", _delete_after_commit)
    try:
        response = client.patch(f"/api/items/{item_id}", json={"snapshot": "still returned"})
    finally:
        event.remove(session, "after_commit", _delete_after_commit)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == item_id
    assert body["snapshot"] == "still returned"

    # The row is genuinely gone -- confirms this test would have caught the
    # old session.refresh()-based bug, not exercised a no-op hook.
    assert client.get(f"/api/items/{item_id}").status_code == 404


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


def test_delete_then_create_does_not_reuse_id(client: TestClient) -> None:
    """SQLite's default ROWID assignment for an `INTEGER PRIMARY KEY` column
    reuses the id of the just-deleted highest-id row on the next insert: it
    is recomputed as `max(id) + 1` each time, not drawn from a persistent
    sequence. Delete the item with the current-highest id, then create a new
    one, and -- without `sqlite_autoincrement` (see `MemoryItem.__table_args__`
    in models.py) -- the new row would silently get the *same* id back. That
    is dangerous here: a client holding a stale reference to the deleted
    item (an open tab, a bookmark, an in-flight PATCH/DELETE built before the
    delete) would then land on the new, unrelated item instead of getting
    the 404 it should. `sqlite_autoincrement` makes SQLite track the
    historical maximum in its internal `sqlite_sequence` table instead, so a
    new id must always be strictly greater than every id ever used before.
    """
    first = _create(client, title="First")
    first_id = first["id"]
    assert client.delete(f"/api/items/{first_id}").status_code == 204

    second = _create(client, title="Second")
    assert second["id"] > first_id


def test_delete_missing_returns_404(client: TestClient) -> None:
    assert client.delete("/api/items/9999").status_code == 404


def test_delete_item_id_over_sqlite_max_rejected(client: TestClient) -> None:
    """`item_id` beyond SQLite's signed-64-bit range must be rejected by
    FastAPI's own path validation (422) before ever reaching the database --
    binding a Python int this large into a SQLite query raises OverflowError
    from the driver, which would otherwise surface as an unhandled 500.
    """
    response = client.delete(f"/api/items/{_OUT_OF_SQLITE_RANGE}")
    assert response.status_code == 422


def test_delete_item_id_at_sqlite_max_in_range_returns_404(client: TestClient) -> None:
    """The maximum in-range id (SQLite's signed-64-bit ceiling itself) must
    pass FastAPI's path validation and reach the database, where -- absent
    any item with that id -- it is a normal 404, not a validation error.
    """
    response = client.delete(f"/api/items/{SQLITE_MAX_INT}")
    assert response.status_code == 404


def test_delete_races_with_concurrent_delete_returns_204(
    client: TestClient, session: Session
) -> None:
    """If the item is deleted+committed by another session between this
    handler's `session.get()` and its `commit()`, this session's own DELETE
    for `item` matches zero rows -- but, unlike the UPDATE-based races (see
    `test_progress_add_races_with_concurrent_delete_returns_404` and
    `test_patch_races_with_concurrent_delete_returns_404`), SQLAlchemy's
    unit of work does NOT raise for a zero-row-matched DELETE today unless
    the mapper has a `version_id_col` configured, which `MemoryItem` does
    not: without one, a mismatch here only emits a warning (see the ORM
    `Mapper`'s `confirm_deleted_rows` parameter docs -- "the warning may be
    changed to an exception in a future release"). So `session.commit()`
    simply succeeds and the handler falls through to its normal 204
    response -- which is correct, idempotent-DELETE behaviour: asking to
    delete a resource that is already gone is a no-op success, not an
    error. (See
    `test_delete_races_with_manufactured_stale_data_error_returns_404` for
    the handler's StaleDataError->404 translation, which this real-world
    race does not actually exercise.)

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
        response = client.delete(f"/api/items/{item_id}")
    finally:
        event.remove(session, "before_flush", _delete_item_before_flush)

    assert response.status_code == 204

    # The handler's normal (non-exceptional) commit path must leave the
    # shared session usable for later requests too.
    assert client.get("/api/items").status_code == 200


def test_delete_races_with_manufactured_stale_data_error_returns_404(
    client: TestClient, session: Session
) -> None:
    """Defensive-only: in the real world today, the race this simulates does
    NOT produce a StaleDataError at all -- see
    `test_delete_races_with_concurrent_delete_returns_204` above, which
    drives the exact same underlying race through the real (non-raising)
    DELETE path and gets 204, by design (idempotent-DELETE semantics).
    SQLAlchemy's unit of work does not raise for a zero-row-matched DELETE
    unless the mapper has a `version_id_col` configured, which `MemoryItem`
    does not: without one, a mismatch here only emits a warning (see the
    ORM `Mapper`'s `confirm_deleted_rows` parameter docs -- "the warning may
    be changed to an exception in a future release"). This test exists only
    to exercise the handler's StaleDataError->404 translation anyway, kept
    as hardening for that future/alternate SQLAlchemy behaviour (or a
    future `version_id_col` addition) rather than a path reachable today --
    so it manufactures the exception directly: the `before_flush` hook
    performs the same real concurrent-delete simulation used by the races
    above, then raises StaleDataError itself to exercise the handler's
    translation to 404, rather than relying on today's (non-raising) real
    DELETE path.
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


def test_progress_entry_delete_then_create_does_not_reuse_id(
    client: TestClient, session: Session
) -> None:
    """Consistency companion to test_delete_then_create_does_not_reuse_id
    above: ProgressEntry also sets `sqlite_autoincrement` (see
    `ProgressEntry.__table_args__` in models.py), even though no endpoint
    today deletes a single entry by id -- guarding in advance against the
    same id-reuse footgun should an id-targeted entry endpoint (edit/delete
    a single entry) ever be added. Exercised directly at the session level,
    since there is no API to delete a single progress entry today: deleting
    the highest-id entry without deleting its parent item, then appending a
    new one, must not hand back the id that was just freed.
    """
    item = _create(client)
    item_id = item["id"]
    client.post(f"/api/items/{item_id}/progress", json={"note": "second entry"})

    highest = session.exec(
        select(ProgressEntry)
        .where(col(ProgressEntry.item_id) == item_id)
        .order_by(col(ProgressEntry.id).desc())
    ).first()
    assert highest is not None
    highest_id = highest.id
    session.delete(highest)
    session.commit()

    response = client.post(f"/api/items/{item_id}/progress", json={"note": "third entry"})
    assert response.status_code == 201
    assert response.json()["id"] > highest_id


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


def test_progress_item_id_over_sqlite_max_rejected(client: TestClient) -> None:
    """`item_id` beyond SQLite's signed-64-bit range must be rejected by
    FastAPI's own path validation (422) before ever reaching the database --
    binding a Python int this large into a SQLite query raises OverflowError
    from the driver, which would otherwise surface as an unhandled 500.
    """
    response = client.post(f"/api/items/{_OUT_OF_SQLITE_RANGE}/progress", json={"note": "x"})
    assert response.status_code == 422


def test_progress_item_id_at_sqlite_max_in_range_returns_404(client: TestClient) -> None:
    """The maximum in-range id (SQLite's signed-64-bit ceiling itself) must
    pass FastAPI's path validation and reach the database, where -- absent
    any item with that id -- it is a normal 404, not a validation error.
    """
    response = client.post(f"/api/items/{SQLITE_MAX_INT}/progress", json={"note": "x"})
    assert response.status_code == 404


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


def test_progress_add_response_survives_item_deleted_immediately_after_commit(
    client: TestClient, session: Session
) -> None:
    """add_progress must snapshot its response before `session.commit()` and
    return that snapshot with no further session/DB access afterward -- not
    call `session.refresh()` the way it used to. Proven the same way as
    `test_create_response_survives_row_deleted_immediately_after_commit`
    above: deleting the item (which cascades to the entry just inserted by
    this same request) via an `after_commit` hook, right where the old
    `session.refresh(entry)` call used to run next and would have hit
    `InvalidRequestError` against the now-missing row.
    """
    item = _create(client)
    item_id = item["id"]

    def _delete_after_commit(commit_session: Session) -> None:
        _hard_delete_item_via_raw_connection(commit_session, item_id)

    event.listen(session, "after_commit", _delete_after_commit)
    try:
        response = client.post(f"/api/items/{item_id}/progress", json={"note": "still returned"})
    finally:
        event.remove(session, "after_commit", _delete_after_commit)

    assert response.status_code == 201
    body = response.json()
    assert body["note"] == "still returned"
    assert body["item_id"] == item_id

    # The item (and this new entry, via cascade) is genuinely gone --
    # confirms this test would have caught the old session.refresh()-based
    # bug, not exercised a no-op hook.
    assert client.get(f"/api/items/{item_id}").status_code == 404
