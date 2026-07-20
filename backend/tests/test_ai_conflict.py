"""Optimistic-concurrency (409) tests for the by-id AI write workflows.

Each handler snapshots the item's `updated` before the LLM await and re-checks
it after. If another writer bumped `updated` while the model was running, the
sections were enriched against stale state, so the handler must write NOTHING
and return 409. The conflict is injected deterministically: the mocked
`generate_structured` mutates the row through the shared DBAPI connection (a
stand-in for a second session committing mid-await) before returning its result.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import Engine, event
from sqlmodel import Session

from afterthread.main import app


def _create(client: TestClient, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"title": "Sample"} | fields
    response = client.post("/api/items", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _bump_updated_via_raw_connection(session: Session, item_id: int) -> None:
    """Set the item's `updated` to a distinctly different value directly through
    the shared DBAPI connection, committing immediately -- the same raw-connection
    technique tests/test_items.py uses to model an independently committed write
    another session's rollback-and-refetch will observe. The value is written in
    SQLAlchemy's SQLite datetime string format so it round-trips to a datetime.
    """
    bind = session.get_bind()
    assert isinstance(bind, Engine)
    raw_connection = bind.raw_connection()
    try:
        cursor = raw_connection.cursor()
        try:
            cursor.execute(
                "UPDATE memory_item SET updated = ? WHERE id = ?",
                ("2000-01-01 00:00:00.000000", item_id),
            )
        finally:
            cursor.close()
        raw_connection.commit()
    finally:
        raw_connection.close()


def _commit_competing_write(session: Session, item_id: int, field: str, value: str) -> None:
    """Commit a competing writer's change to one field AND a distinct `updated`
    directly through the shared DBAPI connection (see
    _bump_updated_via_raw_connection). Bumping `updated` is what a real PATCH
    does, and it is exactly what the handler's conditional UPDATE guards on, so
    this stands in for a second session's PATCH committing mid-request.
    """
    bind = session.get_bind()
    assert isinstance(bind, Engine)
    raw_connection = bind.raw_connection()
    try:
        cursor = raw_connection.cursor()
        try:
            cursor.execute(
                f"UPDATE memory_item SET {field} = ?, updated = ? WHERE id = ?",
                (value, "2001-02-03 04:05:06.000000", item_id),
            )
        finally:
            cursor.close()
        raw_connection.commit()
    finally:
        raw_connection.close()


def _progress_notes(client: TestClient, item_id: int) -> list[str]:
    detail = client.get(f"/api/items/{item_id}").json()
    return [entry["note"] for entry in detail["progress"]]


def test_enrich_conflict_during_await_returns_409_and_writes_nothing(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, snapshot="keep", decisions="原決策")
    item_id = item["id"]

    async def _fake(
        system: str, user: str, model_cls: type[BaseModel], **_kwargs: object
    ) -> BaseModel:
        # A concurrent writer bumps `updated` while the model is "running".
        _bump_updated_via_raw_connection(session, item_id)
        return model_cls.model_validate(
            {"sections": {"decisions": "新決策"}, "progress_note": "應被丟棄"}
        )

    monkeypatch.setattr("afterthread.services.memory_ai.generate_structured", _fake)

    response = client.post(f"/api/items/{item_id}/enrich", json={"additional_context": "ctx"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "conflict"
    assert "message" in response.json()["detail"]

    # Nothing was written: sections untouched, no progress entry appended.
    detail = client.get(f"/api/items/{item_id}").json()
    assert detail["snapshot"] == "keep"
    assert detail["decisions"] == "原決策"
    assert _progress_notes(client, item_id) == ["建立項目"]


def test_assist_update_conflict_during_await_returns_409_and_writes_nothing(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, next_actions="keep")
    item_id = item["id"]

    async def _fake(
        system: str, user: str, model_cls: type[BaseModel], **_kwargs: object
    ) -> BaseModel:
        _bump_updated_via_raw_connection(session, item_id)
        return model_cls.model_validate(
            {"sections": {"next_actions": "changed"}, "progress_note": "應被丟棄"}
        )

    monkeypatch.setattr("afterthread.services.memory_ai.generate_structured", _fake)

    response = client.post(f"/api/items/{item_id}/assist-update", json={"note": "n"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "conflict"

    detail = client.get(f"/api/items/{item_id}").json()
    assert detail["next_actions"] == "keep"
    assert _progress_notes(client, item_id) == ["建立項目"]


def test_enrich_no_conflict_still_succeeds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client)

    async def _fake(
        system: str, user: str, model_cls: type[BaseModel], **_kwargs: object
    ) -> BaseModel:
        return model_cls.model_validate({"sections": {"decisions": "d"}, "progress_note": "n"})

    monkeypatch.setattr("afterthread.services.memory_ai.generate_structured", _fake)
    response = client.post(f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"})
    assert response.status_code == 200
    assert "d" in response.json()["item"]["decisions"]


def test_assist_update_no_conflict_still_succeeds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client)

    async def _fake(
        system: str, user: str, model_cls: type[BaseModel], **_kwargs: object
    ) -> BaseModel:
        return model_cls.model_validate(
            {"sections": {"next_actions": "n"}, "progress_note": "note"}
        )

    monkeypatch.setattr("afterthread.services.memory_ai.generate_structured", _fake)
    response = client.post(f"/api/items/{item['id']}/assist-update", json={"note": "n"})
    assert response.status_code == 200


def test_conflict_message_carries_no_config_or_item_content(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, snapshot="secret-snapshot-value")
    item_id = item["id"]

    async def _fake(
        system: str, user: str, model_cls: type[BaseModel], **_kwargs: object
    ) -> BaseModel:
        _bump_updated_via_raw_connection(session, item_id)
        return model_cls.model_validate({"sections": {"decisions": "x"}, "progress_note": "y"})

    monkeypatch.setattr("afterthread.services.memory_ai.generate_structured", _fake)
    body = client.post(f"/api/items/{item_id}/enrich", json={"additional_context": "ctx"}).text
    # The 409 body must not echo item content (nor any config, which is never
    # in scope of this message at all).
    assert "secret-snapshot-value" not in body


def test_enrich_conflict_between_guard_and_update_preserves_competing_write(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PATCH committing in the instant between the handler's optimistic guard
    and its own write must not be silently overwritten. The guard and the write
    are ONE conditional UPDATE, so a competing commit landing right before that
    UPDATE (injected via a before_cursor_execute hook -- the same deterministic
    ambush the delete-race tests use) moves `updated`, the UPDATE matches zero
    rows -> 409, and the competing value survives. Pre-fix (compare `updated`,
    then flush a PK-keyed UPDATE) this exact window produced a 200 that clobbered
    the competing write.
    """
    item = _create(client, decisions="原決策")
    item_id = item["id"]

    async def _fake(
        system: str, user: str, model_cls: type[BaseModel], **_kwargs: object
    ) -> BaseModel:
        return model_cls.model_validate(
            {"sections": {"decisions": "AI 決策"}, "progress_note": "AI note"}
        )

    monkeypatch.setattr("afterthread.services.memory_ai.generate_structured", _fake)

    bind = session.get_bind()
    assert isinstance(bind, Engine)
    ambushed = {"done": False}

    def _competing_write_before_update(
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
            _commit_competing_write(session, item_id, "decisions", "他人決策")

    event.listen(bind, "before_cursor_execute", _competing_write_before_update)
    try:
        response = client.post(f"/api/items/{item_id}/enrich", json={"additional_context": "ctx"})
    finally:
        event.remove(bind, "before_cursor_execute", _competing_write_before_update)

    assert ambushed["done"] is True
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "conflict"

    # The competing writer's value survives; the AI enrich wrote nothing.
    detail = client.get(f"/api/items/{item_id}").json()
    assert detail["decisions"] == "他人決策"
    assert "AI 決策" not in detail["decisions"]
    assert _progress_notes(client, item_id) == ["建立項目"]


def test_assist_update_conflict_between_guard_and_update_preserves_competing_write(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assist-update shares the same guarded UPDATE (see the enrich twin): a
    competing PATCH landing right before that UPDATE resolves to 409 and its
    value survives, with no check-then-write window to lose it through.
    """
    item = _create(client, next_actions="原下一步")
    item_id = item["id"]

    async def _fake(
        system: str, user: str, model_cls: type[BaseModel], **_kwargs: object
    ) -> BaseModel:
        return model_cls.model_validate(
            {"sections": {"next_actions": "AI 下一步"}, "progress_note": "AI note"}
        )

    monkeypatch.setattr("afterthread.services.memory_ai.generate_structured", _fake)

    bind = session.get_bind()
    assert isinstance(bind, Engine)
    ambushed = {"done": False}

    def _competing_write_before_update(
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
            _commit_competing_write(session, item_id, "next_actions", "他人下一步")

    event.listen(bind, "before_cursor_execute", _competing_write_before_update)
    try:
        response = client.post(f"/api/items/{item_id}/assist-update", json={"note": "n"})
    finally:
        event.remove(bind, "before_cursor_execute", _competing_write_before_update)

    assert ambushed["done"] is True
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "conflict"

    detail = client.get(f"/api/items/{item_id}").json()
    assert detail["next_actions"] == "他人下一步"
    assert "AI 下一步" not in detail["next_actions"]
    assert _progress_notes(client, item_id) == ["建立項目"]


# --- OpenAPI contract -----------------------------------------------------


def _declaring(
    schema: dict[str, Any], status: int, *, code: str | None = None
) -> set[tuple[str, str]]:
    """Every (path, method) declaring a ``status`` response.

    When ``code`` is given, only operations whose declared example carries that
    ``{detail: {code}}`` count -- so a same-status response with a DIFFERENT code
    (e.g. the installer's ``install_in_progress`` 409, which is a single-flight
    guard, NOT the AI optimistic-lock ``conflict``) is excluded.
    """
    result: set[tuple[str, str]] = set()
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            response = operation.get("responses", {}).get(str(status))
            if response is None:
                continue
            if code is not None:
                example = response.get("content", {}).get("application/json", {}).get("example", {})
                detail = example.get("detail") if isinstance(example, dict) else None
                if not (isinstance(detail, dict) and detail.get("code") == code):
                    continue
            result.add((path, method))
    return result


def test_409_declared_on_exactly_the_two_by_id_ai_operations() -> None:
    schema = TestClient(app).get("/openapi.json").json()
    # The optimistic-lock CONFLICT 409 is declared on exactly the two by-id AI
    # operations. The installer's /tools/install also declares a 409, but under a
    # DIFFERENT code (install_in_progress, a single-flight guard), so scoping by
    # code keeps this contract test focused on the conflict semantics it guards.
    assert _declaring(schema, 409, code="conflict") == {
        ("/api/items/{item_id}/enrich", "post"),
        ("/api/items/{item_id}/assist-update", "post"),
    }


def test_declared_409_detail_shape_matches_runtime() -> None:
    schema = TestClient(app).get("/openapi.json").json()
    for path in ("/api/items/{item_id}/enrich", "/api/items/{item_id}/assist-update"):
        example = schema["paths"][path]["post"]["responses"]["409"]["content"]["application/json"][
            "example"
        ]
        assert example["detail"]["code"] == "conflict"
        assert "message" in example["detail"]
