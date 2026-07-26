"""Tests for schema-level contracts in afterthread.schemas."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from afterthread.main import app
from afterthread.schemas import ToolInstallRequest
from afterthread.services.memory_ai import SECTION_FIELD_ORDER


def _allows_null(prop: dict[str, Any]) -> bool:
    """True if an OpenAPI property schema admits a JSON `null`."""
    if prop.get("type") == "null":
        return True
    return any(variant.get("type") == "null" for variant in prop.get("anyOf", []))


def test_memory_item_update_openapi_has_no_nullable_properties() -> None:
    """Every MemoryItemUpdate property must be a plain, non-nullable type.

    Previously every field was typed `X | None`, so OpenAPI advertised each
    property as nullable (an `anyOf` with a `null` member) while a runtime
    validator rejected explicit `null` with a 422 -- a client generated
    honestly from the schema could legally send `null` and be rejected.
    Fields are now their ordinary non-nullable types, so the schema and the
    runtime behaviour agree: `null` fails ordinary type validation instead.
    """
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    properties = schema["components"]["schemas"]["MemoryItemUpdate"]["properties"]

    assert properties, "expected MemoryItemUpdate to have properties"
    for name, prop in properties.items():
        assert not _allows_null(prop), f"{name} still allows null: {prop}"


def test_memory_item_update_openapi_properties_have_no_default_key() -> None:
    """No MemoryItemUpdate property may advertise a `default` in OpenAPI.

    Every field is declared with `Field(default_factory=...)` rather than a
    literal default (e.g. `title: str = ""`) precisely because pydantic v2
    omits `default_factory`-sourced values from the generated JSON schema.
    The router never applies these defaults -- `update_item` always calls
    `model_dump(exclude_unset=True)` -- so a literal default would be a pure
    schema artifact: an OpenAPI client that honestly materialises declared
    defaults would send e.g. `title=""` (or `status="capture-quick"`) on
    every PATCH, tripping the not-blank validator or silently resetting
    fields the caller never meant to touch. Asserting no property carries a
    `default` key keeps that failure mode from silently coming back.
    """
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    properties = schema["components"]["schemas"]["MemoryItemUpdate"]["properties"]

    assert properties, "expected MemoryItemUpdate to have properties"
    for name, prop in properties.items():
        assert "default" not in prop, f"{name} still has a default: {prop}"


def _assert_title_tags_section_bounds(props: dict[str, Any]) -> None:
    """title max_length 300, tags max 20 items of 1..100 chars, every section
    text field max_length 20000 -- the CRUD-side bound that keeps a
    legitimate POST/PATCH from writing an item whose title/tags/sections are
    large enough to blow the enrich/assist-update prompt header budget (see
    afterthread.services.memory_ai._serialize_item_for_prompt and its _CRUD_* mirror
    constants in afterthread.schemas).
    """
    assert props["title"]["maxLength"] == 300
    assert props["tags"]["maxItems"] == 20
    assert props["tags"]["items"]["minLength"] == 1
    assert props["tags"]["items"]["maxLength"] == 100
    for field in SECTION_FIELD_ORDER:
        assert props[field]["maxLength"] == 20000, field


def test_memory_item_create_declares_title_tags_and_section_bounds() -> None:
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    props = schema["components"]["schemas"]["MemoryItemCreate"]["properties"]
    _assert_title_tags_section_bounds(props)


def test_memory_item_update_declares_title_tags_and_section_bounds() -> None:
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    props = schema["components"]["schemas"]["MemoryItemUpdate"]["properties"]
    _assert_title_tags_section_bounds(props)


def test_progress_entry_create_declares_note_length_bound() -> None:
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    prop = schema["components"]["schemas"]["ProgressEntryCreate"]["properties"]["note"]
    assert prop["maxLength"] == 20000


# --- ToolInstallRequest install-form secret pair (D36) ----------------------


def _install_req(**overrides: Any) -> ToolInstallRequest:
    base: dict[str, Any] = {
        "openapi_url": "http://kb.example/openapi.json",
        "instructions": "build",
    }
    base.update(overrides)
    return ToolInstallRequest.model_validate(base)


def test_tool_install_request_accepts_no_secret() -> None:
    req = _install_req()
    assert req.secret_name is None
    assert req.secret_value is None


def test_tool_install_request_accepts_and_strips_valid_secret_pair() -> None:
    req = _install_req(secret_name="  KB_API_KEY  ", secret_value="  the-value  ")
    assert req.secret_name == "KB_API_KEY"
    assert req.secret_value == "the-value"


def test_tool_install_request_empty_secret_strings_normalize_to_none() -> None:
    """Whitespace-only for BOTH is 'no secret' (None), not a half-pair error."""
    req = _install_req(secret_name="   ", secret_value="")
    assert req.secret_name is None
    assert req.secret_value is None


def test_tool_install_request_accepts_six_char_value() -> None:
    """Exactly 6 chars is the floor (>= 6), so it is accepted (F3)."""
    req = _install_req(secret_name="KB_API_KEY", secret_value="abcdef")
    assert req.secret_value == "abcdef"


def test_tool_install_request_rejects_short_value_with_message() -> None:
    """F3: a value under the 6-char floor 422s with the fixed zh-TW message -- the
    redactor skips <6-char values, so accepting one would be unredactable."""
    with pytest.raises(ValidationError) as excinfo:
        _install_req(secret_name="KB_API_KEY", secret_value="abc")
    assert "秘密值長度至少 6 字元" in str(excinfo.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"secret_name": "KB_API_KEY"},  # value missing (half a pair)
        {"secret_value": "v"},  # name missing (half a pair)
        {"secret_name": "kb_key", "secret_value": "v"},  # lowercase-start name
        {"secret_name": "1KEY", "secret_value": "v"},  # digit-start name
        {"secret_name": "A" * 65, "secret_value": "v"},  # over 64 chars
        {"secret_name": "KB KEY", "secret_value": "v"},  # space in name
        {"secret_name": "KB_KEY", "secret_value": "line1\nline2"},  # multiline value
        {"secret_name": "KB_KEY", "secret_value": "abc"},  # under the 6-char floor (F3)
    ],
    ids=[
        "value-missing",
        "name-missing",
        "lowercase-start",
        "digit-start",
        "too-long",
        "space-in-name",
        "multiline-value",
        "short-value",
    ],
)
def test_tool_install_request_rejects_bad_secret_pair(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _install_req(**overrides)


def test_only_by_id_routes_declare_404() -> None:
    """Every by-id route -- and only those -- must declare 404 in OpenAPI so
    generated clients and docs match runtime. That is the four item routes
    (GET/PATCH/DELETE /api/items/{item_id} and POST /api/items/{item_id}/progress),
    the two by-id AI routes (POST /api/items/{item_id}/enrich and /assist-update),
    the by-id LLM log route (GET /api/llm/logs/{log_id}, which 404s for an
    unknown/evicted id), and the seven by-name/by-id tool routes (PATCH/DELETE
    /api/tools/{name} for a missing package, GET /api/tools/jobs/{job_id}
    for an unknown/evicted/post-restart job, the three AI-summary routes
    under /api/tools/{name}/summary -- D40 -- which 404 when the named tool does
    not exist, TOOLS_DIR being unset included, and POST /api/tools/{name}/revise,
    which is by-name and 404s on the same gate). The collection, review, health,
    status, capture, log-list, tool-list and install-submit routes cannot 404
    and must not declare it.

    The job poll is `/api/tools/jobs/{job_id}` since D40 renamed it from
    `/api/tools/install/{job_id}` (one endpoint now serves install AND revise
    jobs); the old path is gone, not aliased, so a stale entry here would be a
    route nothing serves.
    """
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    declaring_404 = {
        (path, method)
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if "404" in operation.get("responses", {})
    }
    assert declaring_404 == {
        ("/api/items/{item_id}", "get"),
        ("/api/items/{item_id}", "patch"),
        ("/api/items/{item_id}", "delete"),
        ("/api/items/{item_id}/progress", "post"),
        ("/api/items/{item_id}/enrich", "post"),
        ("/api/items/{item_id}/assist-update", "post"),
        ("/api/llm/logs/{log_id}", "get"),
        ("/api/tools/{name}", "patch"),
        ("/api/tools/{name}", "delete"),
        ("/api/tools/jobs/{job_id}", "get"),
        ("/api/tools/{name}/summary", "get"),
        ("/api/tools/{name}/summary", "patch"),
        ("/api/tools/{name}/summary/regenerate", "post"),
        ("/api/tools/{name}/revise", "post"),
    }
