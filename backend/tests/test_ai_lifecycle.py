"""Tests for the enrichment lifecycle: a complete checklist graduates a still-
capturing item to active (and to stage full), while non-capture statuses keep
their status. LLM interaction is mocked at the service boundary.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from app.services.llm import LLMUpstreamError
from app.services.memory_ai import ENRICH_SYSTEM_PROMPT


def _create(client: TestClient, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"title": "Sample"} | fields
    response = client.post("/api/items", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _patch_generate_structured(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> None:
    """Stub the structured-output boundary: skip the network/parse and run the
    workflow's real model validation on ``result``, mirroring generate_structured
    (a validation failure maps to the same 502 upstream error, no retry needed
    for a fixed canned result).
    """

    async def _fake(system: str, user: str, model_cls: type[BaseModel]) -> BaseModel:
        try:
            return model_cls.model_validate(result)
        except ValidationError:
            raise LLMUpstreamError(
                "InvalidStructuredOutput: the LLM did not return a valid structured result"
            ) from None

    monkeypatch.setattr("app.services.memory_ai.generate_structured", _fake)


def _enrich(client: TestClient, item_id: int) -> dict[str, Any]:
    response = client.post(f"/api/items/{item_id}/enrich", json={"additional_context": "ctx"})
    assert response.status_code == 200, response.text
    return response.json()["item"]


def test_complete_checklist_promotes_capture_quick_to_active(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client)  # defaults to capture-quick
    assert item["status"] == "capture-quick"
    _patch_generate_structured(
        monkeypatch,
        {"sections": {"decisions": "定案"}, "checklist_complete": True, "progress_note": "done"},
    )
    updated = _enrich(client, item["id"])
    assert updated["status"] == "active"
    assert updated["stage"] == "full"

    # And it now surfaces in the review "active" bucket, not needs_enrichment.
    review = client.get("/api/review").json()
    assert item["id"] in [i["id"] for i in review["active"]]
    assert item["id"] not in [i["id"] for i in review["needs_enrichment"]]


def test_complete_checklist_promotes_needs_enrichment_to_active(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, status="needs-enrichment")
    _patch_generate_structured(
        monkeypatch,
        {"sections": {"decisions": "d"}, "checklist_complete": True, "progress_note": "n"},
    )
    updated = _enrich(client, item["id"])
    assert updated["status"] == "active"
    assert updated["stage"] == "full"


def test_complete_checklist_leaves_waiting_status_untouched(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, status="waiting")
    _patch_generate_structured(
        monkeypatch,
        {"sections": {"decisions": "d"}, "checklist_complete": True, "progress_note": "n"},
    )
    updated = _enrich(client, item["id"])
    # Status is preserved; only stage still flips to full as before.
    assert updated["status"] == "waiting"
    assert updated["stage"] == "full"

    review = client.get("/api/review").json()
    assert item["id"] in [i["id"] for i in review["waiting"]]


def test_complete_checklist_leaves_parked_status_untouched(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client, status="parked")
    _patch_generate_structured(
        monkeypatch,
        {"sections": {"decisions": "d"}, "checklist_complete": True, "progress_note": "n"},
    )
    updated = _enrich(client, item["id"])
    assert updated["status"] == "parked"


def test_incomplete_checklist_does_not_promote(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _create(client)  # capture-quick
    _patch_generate_structured(
        monkeypatch,
        {"sections": {"decisions": "d"}, "checklist_complete": False, "progress_note": "n"},
    )
    updated = _enrich(client, item["id"])
    assert updated["status"] == "capture-quick"
    assert updated["stage"] == "quick"


def test_ambiguous_checklist_complete_does_not_promote(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An out-of-spec checklist_complete value ("yes") must NOT be read as
    # true: strict coercion (memory_ai._coerce_bool) maps anything other than
    # bool/0/1/"true"/"false" to False, so a still-capturing item is not
    # wrongly promoted to active/full the way a lenient `bool("yes")`-style
    # coercion would have.
    item = _create(client)  # capture-quick
    assert item["status"] == "capture-quick"
    _patch_generate_structured(
        monkeypatch,
        {"sections": {"decisions": "d"}, "checklist_complete": "yes", "progress_note": "n"},
    )
    updated = _enrich(client, item["id"])
    assert updated["status"] == "capture-quick"
    assert updated["stage"] == "quick"


def test_contradictory_complete_with_gaps_does_not_promote(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A result that flags checklist_complete=true while still listing
    # remaining_gaps is contradictory. The EnrichResult normalization forces
    # completion False, so the still-capturing item is NOT promoted -- stage and
    # status stay put -- and the gaps flow back to the caller.
    item = _create(client)  # capture-quick / stage quick
    assert item["status"] == "capture-quick"
    _patch_generate_structured(
        monkeypatch,
        {
            "sections": {"decisions": "d"},
            "checklist_complete": True,
            "remaining_gaps": ["缺少 stakeholders"],
            "progress_note": "n",
        },
    )
    response = client.post(f"/api/items/{item['id']}/enrich", json={"additional_context": "ctx"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["item"]["status"] == "capture-quick"
    assert body["item"]["stage"] == "quick"
    assert body["gaps"] == ["缺少 stakeholders"]


def test_enrich_prompt_states_completion_promotes_to_active() -> None:
    assert "active" in ENRICH_SYSTEM_PROMPT.lower()
    assert "promotes" in ENRICH_SYSTEM_PROMPT.lower()


def test_enrich_prompt_states_gaps_and_completion_are_mutually_exclusive() -> None:
    prompt = ENRICH_SYSTEM_PROMPT
    assert "mutually exclusive" in prompt.lower()
    assert "remaining_gaps" in prompt
    assert "checklist_complete" in prompt
