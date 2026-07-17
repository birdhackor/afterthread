"""Tests for GET /api/llm/status."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from context_memory.config import Settings
from context_memory.services import token_budget

_SECRET_KEY = "sk-do-not-leak"
_SECRET_URL = "http://llm.internal.example/v1"

# The cold-start token_ratio (P4): with the estimator's window reset around every
# test (conftest autouse) and no LLM interaction run here, /llm/status always
# reports 0 samples and a null ratio.
_COLD_TOKEN_RATIO = {"samples": 0, "tokens_per_char": None}


def _expected(configured: bool, model: str | None) -> dict[str, object]:
    """The full /llm/status body: configured + model + the cold-start token_ratio.

    Used in place of the literal expected dict so the exact-equality assertions
    still pin that NO extra field (e.g. a base URL fragment) ever leaks, now that
    the response additionally carries the always-present token_ratio object.
    """
    return {"configured": configured, "model": model, "token_ratio": _COLD_TOKEN_RATIO}


def test_status_reports_configured_with_model_name(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    configure_llm(base_url=_SECRET_URL, model="gpt-test", api_key=_SECRET_KEY)
    response = client.get("/api/llm/status")
    assert response.status_code == 200
    assert response.json() == _expected(True, "gpt-test")


def test_status_returns_stripped_model_name(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    # llm_configured()/generate_json normalize the model via a shared strip
    # helper; the status endpoint must report that SAME normalized value, not
    # the raw configured string, or a whitespace-padded override would make
    # the status endpoint lie about what a real request actually sends.
    configure_llm(base_url=_SECRET_URL, model="  test-model  ")
    response = client.get("/api/llm/status")
    assert response.status_code == 200
    assert response.json() == _expected(True, "test-model")


def test_status_reports_unconfigured(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    configure_llm(base_url="", model="")
    response = client.get("/api/llm/status")
    assert response.status_code == 200
    assert response.json() == _expected(False, None)


def test_status_unconfigured_when_only_base_url_set(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    # A half-configured endpoint (URL but no model) is not usable, so it must
    # report unconfigured and must not surface the model.
    configure_llm(base_url=_SECRET_URL, model="")
    assert client.get("/api/llm/status").json() == _expected(False, None)


def test_status_never_leaks_base_url_or_api_key(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    configure_llm(base_url=_SECRET_URL, model="gpt-test", api_key=_SECRET_KEY)
    body = client.get("/api/llm/status").text
    assert _SECRET_KEY not in body
    assert _SECRET_URL not in body
    assert "llm.internal.example" not in body


def test_status_unconfigured_when_base_url_syntactically_invalid(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    # An invalid port makes the endpoint impossible to construct, so every
    # workflow using it degrades to 503. The status endpoint must agree: report
    # unconfigured rather than claiming a working endpoint, and leak no fragment.
    configure_llm(base_url="http://internal-llm:8o80/v1", model="m")
    response = client.get("/api/llm/status")
    assert response.json() == _expected(False, None)
    assert "8o80" not in response.text
    assert "internal-llm" not in response.text


def test_status_and_capture_agree_on_malformed_url(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    # The whole point of finding 5: status and the workflows tell the same story.
    configure_llm(base_url="http://internal-llm:8o80/v1", model="m")
    assert client.get("/api/llm/status").json()["configured"] is False
    capture = client.post("/api/capture", json={"raw_text": "raw"})
    assert capture.status_code == 503
    assert capture.json()["detail"]["code"] == "llm_not_configured"


def test_status_configured_when_base_url_syntactically_valid(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    configure_llm(base_url=_SECRET_URL, model="gpt-test")
    assert client.get("/api/llm/status").json() == _expected(True, "gpt-test")


@pytest.mark.parametrize(
    "base_url",
    ["localhost:8000/v1", "http:///v1", "ftp://h/v1"],
    ids=["scheme-less-authority", "empty-host", "non-http-scheme"],
)
def test_status_and_capture_agree_on_parseable_but_unusable_url(
    client: TestClient, configure_llm: Callable[..., Settings], base_url: str
) -> None:
    # Each URL parses under httpx.URL but is unreachable (no http/https scheme,
    # or no host), so it must read as unconfigured everywhere: the status
    # endpoint reports False and the capture workflow degrades to 503, never
    # claiming a working endpoint the request would then fail to reach.
    configure_llm(base_url=base_url, model="m")
    assert client.get("/api/llm/status").json() == _expected(False, None)
    capture = client.post("/api/capture", json={"raw_text": "raw"})
    assert capture.status_code == 503
    assert capture.json()["detail"]["code"] == "llm_not_configured"


@pytest.mark.parametrize(
    "base_url",
    ["http://internal-llm:99999/v1", "http://internal-llm:0/v1"],
    ids=["port-above-65535", "port-zero"],
)
def test_status_and_capture_agree_on_out_of_range_port(
    client: TestClient, configure_llm: Callable[..., Settings], base_url: str
) -> None:
    # httpx.URL parses an out-of-range port (99999, beyond the 16-bit TCP
    # range) or port 0 WITHOUT error -- unlike a non-numeric port, which fails
    # at construction -- so each needs its own explicit range check. Without
    # it, status would report configured while capture could only ever fail as
    # a 502 at the TCP layer instead of the config-error 503.
    configure_llm(base_url=base_url, model="m")
    assert client.get("/api/llm/status").json() == _expected(False, None)
    capture = client.post("/api/capture", json={"raw_text": "raw"})
    assert capture.status_code == 503
    assert capture.json()["detail"]["code"] == "llm_not_configured"


def test_status_configured_true_for_explicit_valid_port(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    # A normal, in-range explicit port must remain configured -- the new port
    # check rejects only 0 and values above 65535, never an ordinary port.
    configure_llm(base_url="http://internal-llm:8000/v1", model="m")
    assert client.get("/api/llm/status").json() == _expected(True, "m")


def test_status_reports_learned_token_ratio(
    client: TestClient, configure_llm: Callable[..., Settings]
) -> None:
    # Once the estimator has enough observations, /llm/status reports the LEARNED
    # ratio (P4) instead of the cold-start null. Three observations of 100 chars ->
    # 60 tokens each give an aggregate tokens_per_char of 0.6.
    configure_llm(base_url=_SECRET_URL, model="gpt-test")
    for _ in range(3):
        token_budget.observe(100, 60)
    body = client.get("/api/llm/status").json()
    assert body["token_ratio"] == {"samples": 3, "tokens_per_char": 0.6}
