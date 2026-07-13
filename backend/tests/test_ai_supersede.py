"""Tests for server-side supersede-not-delete on history-bearing sections.

The methodology forbids deleting a decision or its rationale; a prompt rule
alone cannot guarantee that, so the server merges history sections losslessly.
`merge_with_supersede` is exercised directly as a pure helper, and both by-id
AI endpoints are driven (LLM mocked at the service boundary) to prove the merge
is enforced in the handlers, not just advertised in the prompt.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services.memory_ai import (
    HISTORY_SECTIONS,
    SECTION_FIELDS,
    merge_with_supersede,
)


def _create(client: TestClient, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"title": "Sample"} | fields
    response = client.post("/api/items", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _patch_generate_json(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> None:
    async def _fake(system: str, user: str) -> dict[str, Any]:
        return result

    monkeypatch.setattr("app.services.memory_ai.generate_json", _fake)


# --- pure helper ----------------------------------------------------------


def test_history_sections_are_a_subset_of_the_whitelist() -> None:
    assert sorted(HISTORY_SECTIONS) == ["alternatives", "consequences", "decisions", "rationale"]
    assert HISTORY_SECTIONS.issubset(SECTION_FIELDS)


def test_merge_old_empty_returns_new() -> None:
    assert merge_with_supersede("", "新內容") == "新內容"


def test_merge_old_whitespace_only_returns_new() -> None:
    assert merge_with_supersede("   \n\t ", "新內容") == "新內容"


def test_merge_new_empty_returns_old() -> None:
    # Defensive: the sanitizer drops empty sections, so this cannot arise from a
    # real result, but the helper must still never lose the old text.
    assert merge_with_supersede("原內容", "") == "原內容"


def test_merge_new_contains_old_returns_new_as_is() -> None:
    old = "決策A"
    new = "決策A\n決策B"
    assert merge_with_supersede(old, new) == new


def test_merge_disjoint_appends_old_behind_marker() -> None:
    old = "舊決策"
    new = "新決策"
    merged = merge_with_supersede(old, new)
    assert merged.startswith("新決策")
    assert merged.endswith("舊決策")
    assert "superseded" in merged
    # Nothing is lost: both the old and the new text survive.
    assert "舊決策" in merged
    assert "新決策" in merged


def test_merge_uses_whitespace_normalized_containment() -> None:
    # `old` appears inside `new` but with different internal whitespace, so it
    # counts as preserved -- no marker, no duplication.
    old = "決策  A\tB"
    new = "序言 決策 A B 結尾"
    assert merge_with_supersede(old, new) == new
    assert "superseded" not in merge_with_supersede(old, new)


def test_merge_marker_carries_no_config_or_item_metadata() -> None:
    # The only dynamic content in the marker is a UTC date; assert the marker
    # shape and that it never interpolates anything else.
    merged = merge_with_supersede("old-value", "new-value")
    assert "--- (superseded " in merged
    assert merged.count("superseded") == 1


# --- enforced in the enrich handler ---------------------------------------


def test_enrich_supersedes_existing_decision_losslessly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, decisions="原決策 採用方案A")
    _patch_generate_json(
        monkeypatch, {"sections": {"decisions": "改採方案B"}, "progress_note": "n"}
    )
    updated = client.post(
        f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"}
    ).json()["item"]
    # The replacement is present AND the prior decision is preserved.
    assert "改採方案B" in updated["decisions"]
    assert "原決策 採用方案A" in updated["decisions"]
    assert "superseded" in updated["decisions"]


def test_enrich_no_supersede_when_model_keeps_old_rationale(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, rationale="因為 X")
    _patch_generate_json(
        monkeypatch, {"sections": {"rationale": "因為 X 也因為 Y"}, "progress_note": "n"}
    )
    updated = client.post(
        f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"}
    ).json()["item"]
    assert updated["rationale"] == "因為 X 也因為 Y"
    assert "superseded" not in updated["rationale"]


def test_enrich_non_history_section_is_replaced_not_merged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # snapshot is a current-state field, not history-bearing: it is overwritten.
    item = _create(client, snapshot="舊快照")
    _patch_generate_json(monkeypatch, {"sections": {"snapshot": "新快照"}, "progress_note": "n"})
    updated = client.post(
        f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"}
    ).json()["item"]
    assert updated["snapshot"] == "新快照"
    assert "舊快照" not in updated["snapshot"]


def test_enrich_supersedes_all_four_history_sections(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(
        client,
        decisions="舊決策",
        rationale="舊理由",
        alternatives="舊備案",
        consequences="舊後果",
    )
    _patch_generate_json(
        monkeypatch,
        {
            "sections": {
                "decisions": "新決策",
                "rationale": "新理由",
                "alternatives": "新備案",
                "consequences": "新後果",
            },
            "progress_note": "n",
        },
    )
    updated = client.post(
        f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"}
    ).json()["item"]
    for field, old, new in (
        ("decisions", "舊決策", "新決策"),
        ("rationale", "舊理由", "新理由"),
        ("alternatives", "舊備案", "新備案"),
        ("consequences", "舊後果", "新後果"),
    ):
        assert old in updated[field], field
        assert new in updated[field], field


# --- enforced in the assist-update handler --------------------------------


def test_assist_update_supersedes_existing_history_section(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, consequences="原後果")
    _patch_generate_json(
        monkeypatch, {"sections": {"consequences": "新後果"}, "progress_note": "n"}
    )
    updated = client.post(f"/api/items/{item['id']}/assist-update", json={"note": "n"}).json()[
        "item"
    ]
    assert "新後果" in updated["consequences"]
    assert "原後果" in updated["consequences"]
    assert "superseded" in updated["consequences"]
