"""Tests for GET /api/llm/status."""

from collections.abc import Callable

from fastapi.testclient import TestClient

from app.config import Settings

_SECRET_KEY = "sk-do-not-leak"
_SECRET_URL = "http://llm.internal.example/v1"


def test_status_reports_configured_with_model_name(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    configure_llm(base_url=_SECRET_URL, model="gpt-test", api_key=_SECRET_KEY)
    response = client.get("/api/llm/status")
    assert response.status_code == 200
    assert response.json() == {"configured": True, "model": "gpt-test"}


def test_status_reports_unconfigured(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    configure_llm(base_url="", model="")
    response = client.get("/api/llm/status")
    assert response.status_code == 200
    assert response.json() == {"configured": False, "model": None}


def test_status_unconfigured_when_only_base_url_set(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    # A half-configured endpoint (URL but no model) is not usable, so it must
    # report unconfigured and must not surface the model.
    configure_llm(base_url=_SECRET_URL, model="")
    assert client.get("/api/llm/status").json() == {"configured": False, "model": None}


def test_status_never_leaks_base_url_or_api_key(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    configure_llm(base_url=_SECRET_URL, model="gpt-test", api_key=_SECRET_KEY)
    body = client.get("/api/llm/status").text
    assert _SECRET_KEY not in body
    assert _SECRET_URL not in body
    assert "llm.internal.example" not in body
