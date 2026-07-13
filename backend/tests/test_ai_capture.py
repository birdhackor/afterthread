"""Tests for POST /api/capture (AI quick capture).

All LLM interaction is mocked at the service boundary: ``generate_json`` (the
function ``capture_draft`` calls) is monkeypatched to return canned JSON or
raise, so the real sanitizer/validation and the router's transaction discipline
run without any network. Row counts are checked through the public list API to
avoid cross-thread session reads.
"""

import traceback
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.services.llm import LLMUpstreamError
from app.services.memory_ai import CaptureDraft, _validate

# A full, well-formed draft. ``questions`` deliberately has 5 entries to prove
# the server truncates to 3; ``suggested_status`` is one of the two allowed.
_DRAFT: dict[str, Any] = {
    "title": "付款流程重構",
    "snapshot": "討論如何重構付款流程以支援新金流商。",
    "why_matters": "影響結帳轉換率。",
    "known": "- 目前使用舊版 API",
    "inferred": "- 推測需要新的 webhook(推論)",
    "unknown": "- 尚未確認金流商",
    "next_actions": "- 向金流商索取報價",
    "recovery_keywords": "payment, checkout",
    "recovery_people": "Alice",
    "recovery_files": "src/payment.py",
    "resume_trigger": "當金流商回覆報價時",
    "tags": ["payment", "refactor"],
    "suggested_status": "needs-enrichment",
    "questions": ["q1", "q2", "q3", "q4", "q5"],
}


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


def _total(client: TestClient) -> int:
    return client.get("/api/items").json()["total"]


def test_capture_happy_path_maps_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_generate_json(monkeypatch, result=_DRAFT)
    response = client.post("/api/capture", json={"raw_text": "some raw discussion"})
    assert response.status_code == 201, response.text
    body = response.json()
    item = body["item"]

    assert item["title"] == "付款流程重構"
    assert item["source"] == "llm-capture"
    assert item["stage"] == "quick"
    assert item["status"] == "needs-enrichment"
    assert item["snapshot"] == "討論如何重構付款流程以支援新金流商。"
    assert item["inferred"] == "- 推測需要新的 webhook(推論)"
    assert item["tags"] == ["payment", "refactor"]
    assert item["recovery_keywords"] == "payment, checkout"
    assert item["resume_trigger"] == "當金流商回覆報價時"


def test_capture_truncates_questions_to_three(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_generate_json(monkeypatch, result=_DRAFT)
    body = client.post("/api/capture", json={"raw_text": "raw"}).json()
    assert body["questions"] == ["q1", "q2", "q3"]


def test_capture_seeds_ai_progress_entry_and_one_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_generate_json(monkeypatch, result=_DRAFT)
    body = client.post("/api/capture", json={"raw_text": "raw"}).json()
    assert _total(client) == 1
    detail = client.get(f"/api/items/{body['item']['id']}").json()
    assert [entry["note"] for entry in detail["progress"]] == ["AI 快速捕捉"]


def test_capture_coerces_disallowed_status_to_capture_quick(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model is only allowed to suggest capture-quick / needs-enrichment;
    # any other value (here "active") collapses to capture-quick server-side.
    _patch_generate_json(monkeypatch, result={**_DRAFT, "suggested_status": "active"})
    body = client.post("/api/capture", json={"raw_text": "raw"}).json()
    assert body["item"]["status"] == "capture-quick"


def test_capture_caps_tags_at_ten(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_generate_json(monkeypatch, result={**_DRAFT, "tags": [f"t{i}" for i in range(15)]})
    body = client.post("/api/capture", json={"raw_text": "raw"}).json()
    assert len(body["item"]["tags"]) == 10


def test_capture_truncates_oversized_section(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_generate_json(monkeypatch, result={**_DRAFT, "snapshot": "x" * 25000})
    body = client.post("/api/capture", json={"raw_text": "raw"}).json()
    assert len(body["item"]["snapshot"]) == 20000


def test_capture_upstream_error_returns_502_and_no_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_generate_json(monkeypatch, exc=LLMUpstreamError("UnparseableOutput: garbage"))
    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0


def test_capture_invalid_draft_returns_502_and_no_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Well-formed JSON but an empty title fails CaptureDraft validation, which
    # the workflow maps to LLMUpstreamError -> 502, and no row is written.
    _patch_generate_json(monkeypatch, result={"title": "   ", "snapshot": "x"})
    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0


def test_validate_failure_severs_chain_and_leaks_no_raw_content() -> None:
    """memory_ai._validate maps a pydantic ValidationError to LLMUpstreamError
    with `from None`, so the raw LLM output the error embeds (its str and
    .errors() carry the offending input) cannot ride along in __cause__ into a
    rendered traceback. Driven directly on _validate with a draft whose blank
    title fails the CaptureDraft sanitizer while a secret rides in another field.
    """
    secret = "SECRET-MEMORY-CONTENT-do-not-leak-9f3a"
    with pytest.raises(LLMUpstreamError) as excinfo:
        _validate(CaptureDraft, {"title": "  ", "snapshot": secret})

    exc = excinfo.value
    assert "ValidationError" in str(exc)
    assert secret not in str(exc)
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert secret not in rendered


def test_capture_unconfigured_returns_503_and_no_rows(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    # No generate_json patch: the real workflow reaches the real config gate.
    configure_llm(base_url="", model="")
    response = client.post("/api/capture", json={"raw_text": "raw discussion"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "llm_not_configured"
    assert _total(client) == 0


def test_capture_empty_raw_text_rejected(client: TestClient) -> None:
    assert client.post("/api/capture", json={"raw_text": ""}).status_code == 422


def test_capture_whitespace_raw_text_rejected(client: TestClient) -> None:
    assert client.post("/api/capture", json={"raw_text": "   "}).status_code == 422


def test_capture_oversized_raw_text_rejected(client: TestClient) -> None:
    assert client.post("/api/capture", json={"raw_text": "a" * 20001}).status_code == 422


def test_capture_max_length_raw_text_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_generate_json(monkeypatch, result=_DRAFT)
    assert client.post("/api/capture", json={"raw_text": "a" * 20000}).status_code == 201


def test_capture_deeply_nested_field_returns_502_and_no_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture field returned as a pathologically nested list must degrade to
    502 with no row written. The draft is a valid JSON object, so it clears
    generate_json; the danger is the sanitizer's _coerce_str, which recursed
    unboundedly pre-fix -- a ~1500-deep list blew the stack (RecursionError,
    which pydantic does NOT wrap) and escaped as a 500. Depth-bounding it makes
    the deep list raise ValueError, which pydantic folds into a ValidationError
    the workflow maps to 502.
    """
    deep: Any = "x"
    for _ in range(1500):
        deep = [deep]
    _patch_generate_json(monkeypatch, result={**_DRAFT, "snapshot": deep})

    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0
