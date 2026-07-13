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
