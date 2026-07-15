"""Tests for POST /api/capture (AI quick capture).

All LLM interaction is mocked at the service boundary: ``generate_structured``
(the function ``capture_draft`` calls) is monkeypatched to run the workflow's
real model validation on canned output or to raise, so the real
sanitizer/validation and the router's transaction discipline run without any
network. Row counts are checked through the public list API to avoid
cross-thread session reads.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from context_memory.config import Settings
from context_memory.services.llm import LLMUpstreamError
from context_memory.services.memory_ai import _coerce_str

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


def _patch_generate_structured(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, Any] | None = None,
    exc: Exception | None = None,
) -> None:
    """Stub the structured-output boundary (``generate_structured``): skip the
    network/parse and run the workflow's real model validation on ``result``,
    mirroring generate_structured so a canned draft that fails the CaptureDraft
    sanitizer maps to the same 502 upstream error the real path would raise
    after its corrective retry.
    """

    async def _fake(system: str, user: str, model_cls: type[BaseModel]) -> BaseModel:
        if exc is not None:
            raise exc
        assert result is not None
        try:
            return model_cls.model_validate(result)
        except ValidationError:
            raise LLMUpstreamError(
                "InvalidStructuredOutput: the LLM did not return a valid structured result"
            ) from None

    monkeypatch.setattr("context_memory.services.memory_ai.generate_structured", _fake)


def _total(client: TestClient) -> int:
    return client.get("/api/items").json()["total"]


def test_capture_happy_path_maps_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_generate_structured(monkeypatch, result=_DRAFT)
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
    _patch_generate_structured(monkeypatch, result=_DRAFT)
    body = client.post("/api/capture", json={"raw_text": "raw"}).json()
    assert body["questions"] == ["q1", "q2", "q3"]


def test_capture_persists_questions_as_open_questions_bullet_lines(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # docs/methodology.md lists "open questions" as a minimum durable
    # quick-capture field; CaptureDraft has no open_questions field of its
    # own, so the created item's open_questions must be derived from the
    # model's questions, not left empty while the response merely echoes them.
    _patch_generate_structured(monkeypatch, result={**_DRAFT, "questions": ["問題一?", "問題二?"]})
    response = client.post("/api/capture", json={"raw_text": "raw"})
    body = response.json()
    assert body["questions"] == ["問題一?", "問題二?"]
    assert body["item"]["open_questions"] == "- 問題一?\n- 問題二?"

    # Durable, not just a one-shot response echo: re-fetching the item still
    # carries both questions as "- " bullet lines.
    detail = client.get(f"/api/items/{body['item']['id']}").json()
    assert detail["open_questions"] == "- 問題一?\n- 問題二?"


def test_capture_zero_questions_leaves_open_questions_at_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_generate_structured(monkeypatch, result={**_DRAFT, "questions": []})
    body = client.post("/api/capture", json={"raw_text": "raw"}).json()
    assert body["questions"] == []
    assert body["item"]["open_questions"] == ""


def test_capture_seeds_ai_progress_entry_and_one_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_generate_structured(monkeypatch, result=_DRAFT)
    body = client.post("/api/capture", json={"raw_text": "raw"}).json()
    assert _total(client) == 1
    detail = client.get(f"/api/items/{body['item']['id']}").json()
    assert [entry["note"] for entry in detail["progress"]] == ["AI 快速捕捉"]


def test_capture_coerces_disallowed_status_to_capture_quick(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model is only allowed to suggest capture-quick / needs-enrichment;
    # any other value (here "active") collapses to capture-quick server-side.
    _patch_generate_structured(monkeypatch, result={**_DRAFT, "suggested_status": "active"})
    body = client.post("/api/capture", json={"raw_text": "raw"}).json()
    assert body["item"]["status"] == "capture-quick"


def test_capture_caps_tags_at_ten(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_generate_structured(monkeypatch, result={**_DRAFT, "tags": [f"t{i}" for i in range(15)]})
    body = client.post("/api/capture", json={"raw_text": "raw"}).json()
    assert len(body["item"]["tags"]) == 10


def test_capture_truncates_oversized_section(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_generate_structured(monkeypatch, result={**_DRAFT, "snapshot": "x" * 25000})
    body = client.post("/api/capture", json={"raw_text": "raw"}).json()
    assert len(body["item"]["snapshot"]) == 20000


def test_capture_upstream_error_returns_502_and_no_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_generate_structured(monkeypatch, exc=LLMUpstreamError("UnparseableOutput: garbage"))
    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0


def test_capture_invalid_draft_returns_502_and_no_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Well-formed JSON but an empty title fails CaptureDraft validation, which
    # the workflow maps to LLMUpstreamError -> 502, and no row is written.
    _patch_generate_structured(monkeypatch, result={"title": "   ", "snapshot": "x"})
    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0


def test_capture_unconfigured_returns_503_and_no_rows(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    # No generate_structured patch: the real workflow reaches the real config gate.
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
    _patch_generate_structured(monkeypatch, result=_DRAFT)
    assert client.post("/api/capture", json={"raw_text": "a" * 20000}).status_code == 201


def test_capture_deeply_nested_field_returns_502_and_no_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture field returned as a pathologically nested list must degrade to
    502 with no row written. The draft is a valid JSON object, so it parses
    cleanly; the danger is the sanitizer's _coerce_str, which recursed
    unboundedly pre-fix -- a ~1500-deep list blew the stack (RecursionError,
    which pydantic does NOT wrap) and escaped as a 500. Depth-bounding it makes
    the deep list raise ValueError, which pydantic folds into a ValidationError
    the workflow maps to 502.
    """
    deep: Any = "x"
    for _ in range(1500):
        deep = [deep]
    _patch_generate_structured(monkeypatch, result={**_DRAFT, "snapshot": deep})

    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0


def test_coerce_str_rejects_lone_surrogate() -> None:
    """_coerce_str is the single choke point every sanitized string passes
    through (_clean_text, _clean_str_list, and -- via recursion -- each item
    of a nested list). A lone (unpaired) Unicode surrogate is valid per
    ``json.loads`` -- it accepts it without complaint -- but not UTF-8
    encodable. Left unchecked it would sail through every cap/strip and pass
    pydantic untouched, only to blow up LATER as an uncaught
    ``UnicodeEncodeError`` from SQLite text binding or FastAPI response
    serialization, possibly after a write already committed. It must raise
    ValueError here instead, so pydantic folds it into the same
    ValidationError -> 502 path as any other malformed input, before any
    write happens.
    """
    with pytest.raises(ValueError, match="UTF-8"):
        _coerce_str("開頭正常\ud800結尾正常")


def test_coerce_str_accepts_cjk_and_emoji() -> None:
    """Regression for the rejection above: ordinary CJK and emoji content --
    fully valid and UTF-8 encodable -- must still pass through unchanged,
    including an emoji decoded from a genuine (PAIRED) surrogate pair, as a
    compatible endpoint emitting UTF-16-style JSON escapes would send it.
    ``json.loads`` combines a valid high+low surrogate pair into the single
    non-surrogate code point it represents -- not a lone surrogate -- so this
    must not be mistaken for the malformed case above.
    """
    cjk = "正體中文測試內容"
    assert _coerce_str(cjk) == cjk
    paired_emoji = json.loads('"\\ud83d\\ude00"')
    assert paired_emoji == "😀"
    assert _coerce_str(paired_emoji) == paired_emoji


def test_capture_rejects_lone_surrogate_in_section_returns_502_and_no_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lone surrogate embedded in an ordinary free-text section (mid-string,
    # not the whole value) must be caught by the sanitizer before any write,
    # not slip through to crash later at the DB/response boundary.
    _patch_generate_structured(
        monkeypatch, result={**_DRAFT, "snapshot": "討論內容包含異常字元\ud800后續段落"}
    )
    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0


def test_capture_rejects_lone_surrogate_in_tag_returns_502_and_no_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tags go through the very same _coerce_str choke point (via
    # _clean_str_list), so a lone surrogate there must 502 too.
    _patch_generate_structured(monkeypatch, result={**_DRAFT, "tags": ["payment", "帶\ud800標籤"]})
    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "llm_upstream_error"
    assert _total(client) == 0


def test_capture_accepts_cjk_and_emoji_content(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression for the two lone-surrogate rejections above: ordinary CJK
    # and emoji content across section, tags, and questions -- well-formed
    # and fully encodable -- must still sail through untouched end to end.
    draft = {
        **_DRAFT,
        "snapshot": "完成付款流程討論 🎉",
        "tags": ["付款", "😀重構"],
        "questions": ["進度如何? 💡"],
    }
    _patch_generate_structured(monkeypatch, result=draft)
    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 201, response.text
    item = response.json()["item"]
    assert item["snapshot"] == "完成付款流程討論 🎉"
    assert item["tags"] == ["付款", "😀重構"]
    assert item["open_questions"] == "- 進度如何? 💡"


def test_capture_caps_joined_open_questions_at_per_section_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each of the (at most 3) questions is independently capped at 20000
    # chars by CaptureDraft._sanitize, but the bullet-joined string persisted
    # to open_questions is a NEW, larger blob built by the router -- 3
    # max-sized questions join to ~60008 chars, bypassing the 20000-char
    # section cap every OTHER section respects. It must be truncated again to
    # that same cap, marked, exactly like any other oversized section.
    oversized = ["q" * 20000, "w" * 20000, "e" * 20000]
    _patch_generate_structured(monkeypatch, result={**_DRAFT, "questions": oversized})
    response = client.post("/api/capture", json={"raw_text": "raw"})
    assert response.status_code == 201, response.text
    body = response.json()
    # The response's questions list stays untouched -- it is already
    # per-item bounded and is not rejoined into one blob.
    assert body["questions"] == oversized
    open_questions = body["item"]["open_questions"]
    assert len(open_questions) == 20000
    assert open_questions.endswith("…[內容過長已截斷]")

    # Durable: re-fetching the item shows the same capped value, not the
    # original ~60008-char join.
    detail = client.get(f"/api/items/{body['item']['id']}").json()
    assert detail["open_questions"] == open_questions


def test_capture_capped_open_questions_patch_round_trips(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before the fix, persisting the untruncated ~60008-char join meant a
    # later client PATCH round-tripping that same value straight back would
    # 422 against schemas._CRUD_SECTION_MAX (20000). The capture-side cap
    # keeps the persisted value within that same bound, so the round trip
    # succeeds.
    oversized = ["q" * 20000, "w" * 20000, "e" * 20000]
    _patch_generate_structured(monkeypatch, result={**_DRAFT, "questions": oversized})
    item = client.post("/api/capture", json={"raw_text": "raw"}).json()["item"]

    response = client.patch(
        f"/api/items/{item['id']}", json={"open_questions": item["open_questions"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["open_questions"] == item["open_questions"]
