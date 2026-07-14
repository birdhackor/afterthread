"""Tests that an all-empty AI result is rejected as upstream garbage (502),
leaving the item untouched, while a minimal-but-meaningful result still 200s.

The rejection lives in the EnrichResult / UpdateResult model validators, so it
is asserted both directly (the models raise) and end-to-end through the two
by-id endpoints (mocked at the service boundary).
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from app.services.llm import LLMUpstreamError
from app.services.memory_ai import EnrichResult, UpdateResult, _coerce_bool


def _create(client: TestClient, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"title": "Sample"} | fields
    response = client.post("/api/items", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _patch_generate_structured(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> None:
    """Stub the structured-output boundary: skip the network/parse and run the
    workflow's real model validation on ``result``, mirroring generate_structured
    (a validation failure maps to the same 502 upstream error).
    """

    async def _fake(system: str, user: str, model_cls: type[BaseModel]) -> BaseModel:
        try:
            return model_cls.model_validate(result)
        except ValidationError:
            raise LLMUpstreamError(
                "InvalidStructuredOutput: the LLM did not return a valid structured result"
            ) from None

    monkeypatch.setattr("app.services.memory_ai.generate_structured", _fake)


def _progress_notes(client: TestClient, item_id: int) -> list[str]:
    detail = client.get(f"/api/items/{item_id}").json()
    return [entry["note"] for entry in detail["progress"]]


# --- model-level validators -----------------------------------------------


def test_enrich_result_rejects_all_empty() -> None:
    with pytest.raises(ValidationError):
        EnrichResult.model_validate({})


def test_update_result_rejects_all_empty() -> None:
    with pytest.raises(ValidationError):
        UpdateResult.model_validate({})


def test_enrich_result_accepts_only_checklist_complete() -> None:
    result = EnrichResult.model_validate({"checklist_complete": True})
    assert result.checklist_complete is True


def test_enrich_result_accepts_only_a_gap() -> None:
    result = EnrichResult.model_validate({"remaining_gaps": ["missing X"]})
    assert result.remaining_gaps == ["missing X"]


def test_enrich_result_gaps_force_checklist_incomplete() -> None:
    # Contradictory input -- complete AND gaps remaining -- is normalized
    # deterministically: the gaps win, checklist_complete is forced False, so a
    # sloppy model can never both flag completion and list what is still missing.
    result = EnrichResult.model_validate(
        {"checklist_complete": True, "remaining_gaps": ["still missing X"]}
    )
    assert result.checklist_complete is False
    assert result.remaining_gaps == ["still missing X"]


def test_enrich_result_complete_with_no_gaps_stays_complete() -> None:
    result = EnrichResult.model_validate({"checklist_complete": True, "remaining_gaps": []})
    assert result.checklist_complete is True


def test_update_result_accepts_only_a_progress_note() -> None:
    result = UpdateResult.model_validate({"progress_note": "just a note"})
    assert result.progress_note == "just a note"


# --- checklist_complete strict coercion -------------------------------------
#
# _coerce_bool feeds EnrichResult.checklist_complete, which drives a real
# state transition (a True reading promotes a still-capturing item to active
# -- see routers.ai._enrich_persist). Out-of-spec LLM output must never be
# silently read as true: only bool-as-is, the exact ints 0/1, and the
# case-insensitive strings "true"/"false" are accepted -- everything else
# (any other number, any other string, a list, a dict, None) coerces to
# False, never True.

# Values the OLD lenient coercion (`bool(value)` / a loose string allow-list)
# would have wrongly accepted as true; the strict version must reject all of
# them. "1" (string) is deliberately distinct from the int 1 below -- the old
# code's string allow-list included "1", "yes", "complete", "done", "y".
_AMBIGUOUS_VALUES = [2, -1, "yes", [], {}, 1.5, "1", "complete", "done", "y", "no", None]
_TRUE_VALUES = [True, 1, "true", "True", " TRUE "]
_FALSE_VALUES = [False, 0, "false", "False"]


@pytest.mark.parametrize("value", _AMBIGUOUS_VALUES)
def test_coerce_bool_rejects_ambiguous_values(value: Any) -> None:
    assert _coerce_bool(value) is False


@pytest.mark.parametrize("value", _TRUE_VALUES)
def test_coerce_bool_accepts_only_exact_true_forms(value: Any) -> None:
    assert _coerce_bool(value) is True


@pytest.mark.parametrize("value", _FALSE_VALUES)
def test_coerce_bool_accepts_exact_false_forms(value: Any) -> None:
    assert _coerce_bool(value) is False


# --- end-to-end through the endpoints -------------------------------------


def test_enrich_empty_result_returns_502_and_leaves_item_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, snapshot="keep")
    before = client.get(f"/api/items/{item['id']}").json()["updated"]
    _patch_generate_structured(monkeypatch, {})

    response = client.post(f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"

    detail = client.get(f"/api/items/{item['id']}").json()
    assert detail["snapshot"] == "keep"
    # No progress entry appended and `updated` not bumped.
    assert _progress_notes(client, item["id"]) == ["建立項目"]
    assert detail["updated"] == before


def test_assist_update_empty_result_returns_502_and_leaves_item_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, next_actions="keep")
    before = client.get(f"/api/items/{item['id']}").json()["updated"]
    _patch_generate_structured(monkeypatch, {})

    response = client.post(f"/api/items/{item['id']}/assist-update", json={"note": "n"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"

    detail = client.get(f"/api/items/{item['id']}").json()
    assert detail["next_actions"] == "keep"
    assert _progress_notes(client, item["id"]) == ["建立項目"]
    assert detail["updated"] == before


def test_enrich_minimal_meaningful_result_still_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only checklist_complete is meaningful enough to accept.
    item = _create(client)
    _patch_generate_structured(monkeypatch, {"checklist_complete": True})
    response = client.post(f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"})
    assert response.status_code == 200
    # And it flowed through the lifecycle promotion.
    assert response.json()["item"]["status"] == "active"


def test_assist_update_minimal_meaningful_result_still_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client)
    _patch_generate_structured(monkeypatch, {"progress_note": "只記錄一句"})
    response = client.post(f"/api/items/{item['id']}/assist-update", json={"note": "n"})
    assert response.status_code == 200
    assert "只記錄一句" in _progress_notes(client, item["id"])
