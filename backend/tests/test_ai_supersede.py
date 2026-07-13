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
    _HISTORY_STORE_CAP,
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


def test_merge_line_level_containment_normalizes_whitespace_within_a_line() -> None:
    # A whole old line, reflowed with different internal whitespace, still counts
    # as a preserved COMPLETE line of `new` -- no marker, no duplication. This is
    # line-level containment, so the old line must match a whole new line (here
    # the middle one), not merely sit as a substring inside a longer line.
    old = "決策  A\tB"
    new = "序言\n決策 A B\n結尾"
    assert merge_with_supersede(old, new) == new
    assert "superseded" not in merge_with_supersede(old, new)


def test_merge_substring_within_a_line_now_supersedes() -> None:
    # A rewrite that merely CONTAINS the old text as a substring of a line -- the
    # meaning was changed -- must supersede: history counts as preserved only at
    # the line level, never on a substring match. Losslessly appended behind the
    # marker so nothing is dropped.
    old = "成本低"
    new = "成本低估，實際昂貴"  # noqa: RUF001 (fullwidth comma is authentic zh-TW punctuation)
    merged = merge_with_supersede(old, new)
    assert "superseded" in merged
    assert old in merged
    assert new in merged


def test_merge_multiline_partial_retention_supersedes() -> None:
    # Every non-empty old line must survive as a complete new line. If even one is
    # dropped (here 論點2, rewritten into 新論點), history is NOT fully preserved,
    # so the whole old block is appended behind the marker.
    old = "論點1\n論點2"
    new = "論點1\n新論點"
    merged = merge_with_supersede(old, new)
    assert "superseded" in merged
    # The dropped line survives behind the marker; nothing is lost.
    assert "論點2" in merged


def test_merge_multiline_full_retention_skips_marker() -> None:
    # When every old line reappears as a complete new line (even interleaved with
    # a fresh line), history is fully preserved and no redundant marker is added.
    old = "論點1\n論點2"
    new = "論點1\n新論點\n論點2"
    assert merge_with_supersede(old, new) == new
    assert "superseded" not in merge_with_supersede(old, new)


def test_merge_marker_carries_no_config_or_item_metadata() -> None:
    # The only dynamic content in the marker is a UTC date; assert the marker
    # shape and that it never interpolates anything else.
    merged = merge_with_supersede("old-value", "new-value")
    assert "--- (superseded " in merged
    assert merged.count("superseded") == 1


# --- storage cap on accumulated history (bounded, oldest-first trim) ------


def test_merge_below_cap_is_unaffected_by_the_storage_cap() -> None:
    # The finding's own example: ~20k existing + ~20k new is a normal, honest
    # history well under the cap, so nothing is trimmed and the ordinary
    # new+marker+old shape (asserted elsewhere above) still holds exactly.
    old = "a" * 20000
    new = "b" * 20000
    merged = merge_with_supersede(old, new)
    assert len(merged) < _HISTORY_STORE_CAP
    assert merged.startswith(new)
    assert merged.endswith(old)
    assert "superseded" in merged
    assert "已截斷" not in merged


def test_merge_exceeding_cap_trims_oldest_first_and_keeps_current_and_newer() -> None:
    # Oldest generation: its unique tag sits at its OWN tail. Tail-trimming
    # eats a partially-kept block from its end inward, so placing the tag
    # there proves the oldest content is genuinely gone, not just reordered.
    oldest = "z" * 29990 + "OLDEST-TAG"
    merged_r1 = merge_with_supersede("", oldest)

    # Newer (still superseded, but the more recent of the two): its unique tag
    # sits at its OWN head, which -- since trimming keeps a PREFIX of the
    # whole string -- survives as long as any of this generation survives.
    newer = "NEWER-TAG" + "y" * 19991
    merged_r2 = merge_with_supersede(merged_r1, newer)
    assert len(merged_r2) < _HISTORY_STORE_CAP  # sanity: no trim yet

    current = "CURRENT-TAG" + "x" * 19989
    merged_r3 = merge_with_supersede(merged_r2, current)

    assert len(merged_r3) <= _HISTORY_STORE_CAP
    assert current in merged_r3  # current content intact
    assert "superseded" in merged_r3  # dated supersede marker present
    assert "NEWER-TAG" in merged_r3  # newer superseded block survives
    assert "OLDEST-TAG" not in merged_r3  # oldest block is gone
    assert "已截斷" in merged_r3


def test_merge_repeated_rounds_of_15k_updates_stay_within_cap() -> None:
    # Five successive rounds of a real enrich/assist-update-sized (~15k)
    # disjoint update, simulating a long-lived item whose history keeps
    # growing. Every round's result must stay bounded, not just the last one.
    merged = ""
    for round_index in range(5):
        tag = f"ROUND{round_index}:"
        update = tag + "x" * (15000 - len(tag))
        merged = merge_with_supersede(merged, update)
        assert len(merged) <= _HISTORY_STORE_CAP

    # The latest round is always the current content, so it must survive.
    assert "ROUND4:" in merged


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
    # The model genuinely keeps the old rationale as a COMPLETE line and adds a
    # new one below it, so line-level containment holds and no marker is added.
    item = _create(client, rationale="因為 X")
    _patch_generate_json(
        monkeypatch, {"sections": {"rationale": "因為 X\n也因為 Y"}, "progress_note": "n"}
    )
    updated = client.post(
        f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"}
    ).json()["item"]
    assert updated["rationale"] == "因為 X\n也因為 Y"
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
