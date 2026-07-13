"""OpenAPI contract tests for the AI routes: the declared responses must match
runtime exactly -- 503/502 on precisely the three AI workflow operations (and
nowhere else), the by-id AI operations declaring 404, honest error detail
shapes, an honestly-nullable status model, and declared request bounds.
"""

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app

# The three AI workflow operations that call the LLM and can degrade.
_THREE_AI_OPS = {
    ("/api/capture", "post"),
    ("/api/items/{item_id}/enrich", "post"),
    ("/api/items/{item_id}/assist-update", "post"),
}


def _openapi() -> dict[str, Any]:
    return TestClient(app).get("/openapi.json").json()


def _declaring(schema: dict[str, Any], status: int) -> set[tuple[str, str]]:
    return {
        (path, method)
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if str(status) in operation.get("responses", {})
    }


def _allows_null(prop: dict[str, Any]) -> bool:
    if prop.get("type") == "null":
        return True
    return any(variant.get("type") == "null" for variant in prop.get("anyOf", []))


def test_503_declared_on_exactly_the_three_ai_operations() -> None:
    assert _declaring(_openapi(), 503) == _THREE_AI_OPS


def test_502_declared_on_exactly_the_three_ai_operations() -> None:
    assert _declaring(_openapi(), 502) == _THREE_AI_OPS


def test_404_declared_on_the_two_by_id_ai_operations_only() -> None:
    declaring_404 = _declaring(_openapi(), 404)
    assert ("/api/items/{item_id}/enrich", "post") in declaring_404
    assert ("/api/items/{item_id}/assist-update", "post") in declaring_404
    # Status and capture have no by-id lookup and must not declare 404.
    assert ("/api/llm/status", "get") not in declaring_404
    assert ("/api/capture", "post") not in declaring_404


def test_status_declares_no_error_responses() -> None:
    responses = _openapi()["paths"]["/api/llm/status"]["get"]["responses"]
    for status in ("404", "502", "503"):
        assert status not in responses


def test_declared_503_detail_shape() -> None:
    for path, _method in _THREE_AI_OPS:
        example = _openapi()["paths"][path]["post"]["responses"]["503"]["content"][
            "application/json"
        ]["example"]
        assert example["detail"]["code"] == "llm_not_configured"
        assert "message" in example["detail"]


def test_declared_502_detail_shape() -> None:
    for path, _method in _THREE_AI_OPS:
        example = _openapi()["paths"][path]["post"]["responses"]["502"]["content"][
            "application/json"
        ]["example"]
        assert example["detail"]["code"] == "llm_upstream_error"
        assert "message" in example["detail"]


def test_runtime_503_matches_declared_shape(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    """Contract honesty: the runtime 503 body has exactly the declared shape."""
    configure_llm(base_url="", model="")
    runtime = client.post("/api/capture", json={"raw_text": "raw"}).json()
    declared = _openapi()["paths"]["/api/capture"]["post"]["responses"]["503"]["content"][
        "application/json"
    ]["example"]
    assert runtime.keys() == declared.keys()
    assert runtime["detail"].keys() == declared["detail"].keys()
    assert runtime["detail"]["code"] == declared["detail"]["code"]


def test_llm_status_model_is_honestly_nullable_and_required() -> None:
    schema = _openapi()["components"]["schemas"]["LLMStatus"]
    props = schema["properties"]
    # `model` is genuinely null when unconfigured, so nullability is honest.
    assert _allows_null(props["model"])
    # No phantom defaults; both fields are always present in the response.
    assert "default" not in props["configured"]
    assert "default" not in props["model"]
    assert set(schema.get("required", [])) == {"configured", "model"}


def test_ai_request_fields_declare_length_bounds() -> None:
    schemas = _openapi()["components"]["schemas"]
    cases = [
        ("CaptureRequest", "raw_text"),
        ("EnrichRequest", "additional_context"),
        ("AssistUpdateRequest", "note"),
    ]
    for schema_name, field in cases:
        prop = schemas[schema_name]["properties"][field]
        assert prop["minLength"] == 1
        assert prop["maxLength"] == 20000
        # Required, not nullable, no phantom default.
        assert not _allows_null(prop)
        assert "default" not in prop
        assert field in schemas[schema_name].get("required", [])
