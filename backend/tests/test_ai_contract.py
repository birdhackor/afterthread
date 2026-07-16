"""OpenAPI contract tests for the AI routes: the declared responses must match
runtime exactly -- 503/502 on precisely the three AI workflow operations (and
nowhere else), the by-id AI operations declaring 404, honest error detail
shapes, an honestly-nullable status model, and declared request bounds.
"""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from context_memory.config import Settings
from context_memory.main import app
from context_memory.services.llm import _UPSTREAM_REASON

# The three AI workflow operations that call the LLM and can degrade.
_THREE_AI_OPS = {
    ("/api/capture", "post"),
    ("/api/items/{item_id}/enrich", "post"),
    ("/api/items/{item_id}/assist-update", "post"),
}

# The one NON-LLM operation that also declares a 503: the web installer's
# submit, gated on TOOLS_DIR being configured (code `tools_not_configured`,
# see routers/tools.py). A different feature being unconfigured, under its own
# code -- the LLM-degradation contract this module pins (llm_not_configured /
# llm_upstream_error on exactly the three workflow ops) is unchanged, so the
# 503 enumeration below is extended rather than weakened, and a dedicated test
# pins the new op's distinct code.
_TOOLS_INSTALL_OP = ("/api/tools/install", "post")


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


def test_503_declared_on_exactly_the_ai_operations_and_tools_install() -> None:
    # Still an EXACT set: the three LLM workflows plus the installer submit,
    # and nothing else, may declare 503 (see _TOOLS_INSTALL_OP above).
    assert _declaring(_openapi(), 503) == _THREE_AI_OPS | {_TOOLS_INSTALL_OP}


def test_502_declared_on_exactly_the_three_ai_operations() -> None:
    assert _declaring(_openapi(), 502) == _THREE_AI_OPS


def test_tools_install_503_carries_its_own_code() -> None:
    """The installer's 503 is `tools_not_configured` -- NOT `llm_not_configured`
    -- so the two unconfigured features stay distinguishable to clients."""
    path, method = _TOOLS_INSTALL_OP
    example = _openapi()["paths"][path][method]["responses"]["503"]["content"]["application/json"][
        "example"
    ]
    assert example["detail"]["code"] == "tools_not_configured"
    assert "message" in example["detail"]


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


def test_runtime_502_message_and_declared_example_share_reason_constant(
    client: TestClient,
    configure_llm: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """502-message honesty, at the honest granularity: the runtime message is
    "<category>: <reason>" where the category is the failing SDK error's class
    name, so the declared example cannot promise the whole string -- but both
    must share the same reason substring, the _UPSTREAM_REASON constant the
    service embeds in every SDK-error message. Driven through the REAL
    generate_json (only the low-level client is stubbed) so the message the
    runtime 502 carries is the one the service genuinely builds.
    """
    configure_llm(base_url="http://llm.internal.example/v1", model="m")
    request = httpx.Request("POST", "http://llm.internal.example/v1/chat/completions")

    class _FailingCompletions:
        async def create(self, **kwargs: Any) -> Any:
            raise openai.APIConnectionError(request=request)

    stub = SimpleNamespace(chat=SimpleNamespace(completions=_FailingCompletions()))
    monkeypatch.setattr("context_memory.services.llm._get_client", lambda: stub)

    runtime = client.post("/api/capture", json={"raw_text": "raw"})
    assert runtime.status_code == 502
    message = runtime.json()["detail"]["message"]
    assert message.endswith(_UPSTREAM_REASON)
    # Category prefix is the SDK error's class name, as documented.
    assert message.startswith("APIConnectionError: ")

    declared = _openapi()["paths"]["/api/capture"]["post"]["responses"]["502"]["content"][
        "application/json"
    ]["example"]
    assert _UPSTREAM_REASON in declared["detail"]["message"]
