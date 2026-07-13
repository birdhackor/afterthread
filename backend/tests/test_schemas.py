"""Tests for schema-level contracts in app.schemas."""

from typing import Any

from fastapi.testclient import TestClient

from app.main import app


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
