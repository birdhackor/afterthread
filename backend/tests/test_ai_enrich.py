"""Tests for POST /api/items/{item_id}/enrich (AI full enrichment).

LLM interaction is mocked at the service boundary (``generate_json``), so the
real sanitizer, the whitelist merge, and the router's transaction discipline
run without any network.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlmodel import Session

from app.config import Settings
from app.models import MemoryItem
from app.services.llm import LLMUpstreamError
from app.services.memory_ai import SECTION_FIELDS


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


def test_section_fields_are_all_memory_item_fields() -> None:
    """The whitelist must be a subset of real MemoryItem fields (so a sanitized
    section can be setattr'd safely) and must exclude identity/metadata fields.
    """
    model_fields = set(MemoryItem.model_fields)
    assert SECTION_FIELDS.issubset(model_fields)
    assert "title" in model_fields
    for protected in ("id", "status", "source", "stage", "tags", "created", "updated"):
        assert protected not in SECTION_FIELDS


def test_enrich_merges_sections_and_appends_progress(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, snapshot="original snap")
    _patch_generate_json(
        monkeypatch,
        result={
            "sections": {"decisions": "採用方案A", "risks": "風險X", "not_a_field": "drop me"},
            "checklist_complete": False,
            "remaining_gaps": ["缺少 stakeholders"],
            "progress_note": "補充了決策與風險",
        },
    )
    response = client.post(
        f"/api/items/{item['id']}/enrich", json={"additional_context": "新的背景資訊"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    updated = body["item"]

    assert updated["decisions"] == "採用方案A"
    assert updated["risks"] == "風險X"
    # Merged: a field the model did not return stays as it was.
    assert updated["snapshot"] == "original snap"
    # Unknown key was dropped: it is not a MemoryItem field at all.
    assert "not_a_field" not in updated
    assert body["gaps"] == ["缺少 stakeholders"]
    assert updated["stage"] == "quick"  # checklist not complete
    assert "補充了決策與風險" in _progress_notes(client, item["id"])


def test_enrich_flips_stage_when_checklist_complete(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client)
    _patch_generate_json(
        monkeypatch,
        result={
            "sections": {"decisions": "定案"},
            "checklist_complete": True,
            "remaining_gaps": [],
            "progress_note": "全面補充完成",
        },
    )
    body = client.post(f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"}).json()
    assert body["item"]["stage"] == "full"


def test_enrich_drops_unknown_and_protected_keys(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only whitelisted section keys are applied; id/status/source and any
    # hallucinated key are dropped, so the model cannot mutate identity/metadata.
    item = _create(client, status="active", source="manual")
    _patch_generate_json(
        monkeypatch,
        result={
            "sections": {
                "decisions": "d",
                "status": "done",
                "id": 999,
                "source": "hacked",
                "made_up": "x",
            },
            "progress_note": "note",
        },
    )
    updated = client.post(
        f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"}
    ).json()["item"]
    assert updated["decisions"] == "d"
    assert updated["status"] == "active"
    assert updated["source"] == "manual"
    assert updated["id"] == item["id"]


def test_enrich_truncates_oversized_section(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client)
    _patch_generate_json(
        monkeypatch,
        result={"sections": {"snapshot": "y" * 25000}, "progress_note": "n"},
    )
    updated = client.post(
        f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"}
    ).json()["item"]
    assert len(updated["snapshot"]) == 20000


def test_enrich_bumps_updated(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client)
    stored = session.get(MemoryItem, item["id"])
    assert stored is not None
    old = datetime.now(UTC) - timedelta(days=10)
    stored.updated = old
    session.add(stored)
    session.commit()

    _patch_generate_json(monkeypatch, result={"sections": {"decisions": "d"}, "progress_note": "n"})
    body = client.post(f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"}).json()
    assert datetime.fromisoformat(body["item"]["updated"]) > old


def test_enrich_empty_sections_still_records_progress(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A result with no section changes still appends a progress entry (using
    # the model note) without erasing anything.
    item = _create(client, snapshot="keep")
    _patch_generate_json(
        monkeypatch,
        result={"sections": {}, "remaining_gaps": ["still missing X"], "progress_note": "看過了"},
    )
    body = client.post(f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"}).json()
    assert body["item"]["snapshot"] == "keep"
    assert body["gaps"] == ["still missing X"]
    assert "看過了" in _progress_notes(client, item["id"])


def test_enrich_prompt_is_budgeted_for_a_huge_item(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A huge item must not blow up the enrich prompt. The serialized item
    snapshot is capped at llm_prompt_budget_chars, so the mocked LLM receives a
    bounded prompt instead of the ~unbounded one the raw sections would produce
    (which a small-context model would permanently 502 on).
    """
    # Three sections that, serialized naively, would be ~18k characters.
    big = "字" * 6000
    item = _create(client, snapshot=big, known=big, decisions=big)

    captured: dict[str, str] = {}

    async def _fake(system: str, user: str) -> dict[str, Any]:
        captured["user"] = user
        return {"sections": {"snapshot": "s"}, "progress_note": "n"}

    monkeypatch.setattr("app.services.memory_ai.generate_json", _fake)
    # Force a small budget so the bound is unmistakable.
    monkeypatch.setattr(
        "app.services.memory_ai.get_settings",
        lambda: Settings(llm_prompt_budget_chars=4000),
    )

    response = client.post(f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"})
    assert response.status_code == 200, response.text

    user = captured["user"]
    # Whole prompt = a small fixed preamble + the <=4000-char item snapshot + the
    # (separately bounded) context -- far under the ~18k the raw sections would be.
    assert len(user) < 4000 + 200
    # Truncation actually happened: the marker is present in the serialized item.
    assert "內容過長已截斷" in user


def test_enrich_runs_end_to_end_through_the_threadpool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression pin for the threadpool refactor (finding 4): the pre-await read
    and the post-await write both run off the event loop via run_in_threadpool,
    reusing the dependency's session sequentially. This drives that path end to
    end -- snapshot read, LLM, guarded conditional UPDATE, progress insert,
    response snapshot, commit -- and asserts the merge, the untouched field, and
    the progress log all land exactly as before. (The conflict/race/lifecycle
    suites cover the behavior in depth; this is the simple smoke test the finding
    asks for -- the whole suite staying green is the main evidence.)
    """
    item = _create(client, snapshot="orig snap")
    _patch_generate_json(
        monkeypatch,
        result={"sections": {"decisions": "採用方案 A"}, "progress_note": "透過執行緒池補充"},
    )
    response = client.post(f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"})
    assert response.status_code == 200, response.text
    updated = response.json()["item"]
    assert updated["decisions"] == "採用方案 A"
    # A field the model did not return is preserved through the threadpool write.
    assert updated["snapshot"] == "orig snap"
    assert "透過執行緒池補充" in _progress_notes(client, item["id"])


def test_enrich_missing_returns_404(client: TestClient) -> None:
    response = client.post("/api/items/9999/enrich", json={"additional_context": "ctx"})
    assert response.status_code == 404


def test_enrich_unconfigured_returns_503_and_item_unchanged(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    item = _create(client)
    configure_llm(base_url="", model="")
    response = client.post(f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "llm_not_configured"
    # No partial write: only the seeded entry remains, stage untouched.
    detail = client.get(f"/api/items/{item['id']}").json()
    assert [entry["note"] for entry in detail["progress"]] == ["建立項目"]
    assert detail["stage"] == "quick"


def test_enrich_upstream_error_returns_502_and_item_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, snapshot="keep me")
    _patch_generate_json(monkeypatch, exc=LLMUpstreamError("UnparseableOutput: garbage"))
    response = client.post(f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    detail = client.get(f"/api/items/{item['id']}").json()
    assert detail["snapshot"] == "keep me"
    assert [entry["note"] for entry in detail["progress"]] == ["建立項目"]


def test_enrich_races_with_concurrent_delete_after_llm_returns_404(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent delete landing between the post-await re-fetch and this
    handler's own UPDATE must resolve to the declared 404, never a 500.

    Deterministic ambush via an engine-level `before_cursor_execute` hook: the
    instant the handler's `UPDATE memory_item` is about to execute, the row is
    hard-deleted (and committed) through the shared DBAPI connection -- exactly
    a concurrent `DELETE /api/items/{id}` winning the race. The UPDATE then
    matches zero rows and SQLAlchemy raises StaleDataError.

    This pins WHERE that UPDATE is emitted. Pre-fix, the handler appended the
    progress note via `item.entries.append(...)`: touching the relationship
    lazy-loaded `entries`, and -- the item being already dirty from the section
    setattrs (the stubbed result carries a section for exactly that reason) --
    the lazy load AUTOFLUSHED the UPDATE right there, before the race-wrapped
    `session.flush()`, so the StaleDataError escaped the try/except as an
    unhandled 500. Post-fix the entry is session.add'ed with an explicit
    item_id (no relationship touch), every write happens inside the wrapped
    flush, and the same ambush lands as 404.
    """
    item = _create(client, snapshot="keep")
    item_id = item["id"]
    _patch_generate_json(monkeypatch, result={"sections": {"decisions": "d"}, "progress_note": "n"})

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
        response = client.post(f"/api/items/{item_id}/enrich", json={"additional_context": "ctx"})
    finally:
        event.remove(bind, "before_cursor_execute", _delete_item_before_its_update)

    assert ambushed["done"] is True
    assert response.status_code == 404
    assert response.json()["detail"] == "Memory item not found"

    # The handler's session.rollback() must leave the shared session usable,
    # and the concurrently deleted item is genuinely gone.
    assert client.get("/api/items").status_code == 200
    assert client.get(f"/api/items/{item_id}").status_code == 404


def test_enrich_empty_context_rejected(client: TestClient) -> None:
    item = _create(client)
    response = client.post(f"/api/items/{item['id']}/enrich", json={"additional_context": "  "})
    assert response.status_code == 422


def test_enrich_oversized_context_rejected(client: TestClient) -> None:
    item = _create(client)
    response = client.post(
        f"/api/items/{item['id']}/enrich", json={"additional_context": "a" * 20001}
    )
    assert response.status_code == 422
