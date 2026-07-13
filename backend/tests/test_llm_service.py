"""Tests for the LLM client service (app.services.llm).

All network I/O is stubbed: a fake client whose ``chat.completions.create`` is
driven per-test replaces the real ``_get_client`` (via monkeypatch), and
``get_settings`` is overridden so ``llm_configured`` reports the desired state.
No real ``AsyncOpenAI`` is ever constructed and nothing reaches the network.

``generate_json`` is async; each call is driven with ``asyncio.run`` so no
pytest-asyncio plugin is required (none is a project dependency).
"""

import asyncio
import json
import traceback
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app.config import Settings
from app.services.llm import (
    LLMNotConfiguredError,
    LLMUpstreamError,
    _balanced_brace_slice,
    _build_client,
    _extract_json_object,
    generate_json,
    llm_configured,
)

_CONFIGURED_BASE_URL = "http://llm.internal.example/v1"
_CONFIGURED_KEY = "sk-super-secret-key"
_CONFIGURED_MODEL = "test-model"

_SAMPLE_OBJECT: dict[str, Any] = {"title": "Draft", "count": 2}
_SAMPLE_JSON = json.dumps(_SAMPLE_OBJECT)


class _StubCompletions:
    """Stand-in for ``client.chat.completions`` with a scripted ``create``."""

    def __init__(
        self,
        *,
        content: str | None = None,
        exc: Exception | None = None,
        empty_choices: bool = False,
    ) -> None:
        self._content = content
        self._exc = exc
        self._empty_choices = empty_choices
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        if self._empty_choices:
            return SimpleNamespace(choices=[])
        message = SimpleNamespace(content=self._content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _StubClient:
    def __init__(self, **completion_kwargs: Any) -> None:
        self.chat = SimpleNamespace(completions=_StubCompletions(**completion_kwargs))


def _settings(*, base_url: str, model: str, api_key: str = _CONFIGURED_KEY) -> Settings:
    # Every LLM field is set explicitly so the result never depends on ambient
    # environment or a stray backend/.env.
    return Settings(
        openai_base_url=base_url,
        openai_api_key=api_key,
        openai_model=model,
    )


def _install(monkeypatch: pytest.MonkeyPatch, settings: Settings, stub: _StubClient) -> _StubClient:
    monkeypatch.setattr("app.services.llm.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.llm._get_client", lambda: stub)
    return stub


def _configured(monkeypatch: pytest.MonkeyPatch, **completion_kwargs: Any) -> _StubClient:
    settings = _settings(base_url=_CONFIGURED_BASE_URL, model=_CONFIGURED_MODEL)
    return _install(monkeypatch, settings, _StubClient(**completion_kwargs))


class _RawCompletions:
    """``client.chat.completions`` whose ``create`` returns a caller-supplied
    completion object VERBATIM -- to model a nonconforming-but-200 body from a
    merely OpenAI-compatible gateway (missing/None message, null content, etc.).
    """

    def __init__(self, completion: Any) -> None:
        self._completion = completion

    async def create(self, **kwargs: Any) -> Any:
        return self._completion


class _RawClient:
    def __init__(self, completion: Any) -> None:
        self.chat = SimpleNamespace(completions=_RawCompletions(completion))


def _install_raw(monkeypatch: pytest.MonkeyPatch, completion: Any) -> None:
    settings = _settings(base_url=_CONFIGURED_BASE_URL, model=_CONFIGURED_MODEL)
    monkeypatch.setattr("app.services.llm.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.llm._get_client", lambda: _RawClient(completion))


# --- llm_configured -------------------------------------------------------


def test_llm_configured_true_when_url_and_model_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url=_CONFIGURED_BASE_URL, model=_CONFIGURED_MODEL),
    )
    assert llm_configured() is True


def test_llm_configured_false_when_base_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url="", model=_CONFIGURED_MODEL),
    )
    assert llm_configured() is False


def test_llm_configured_false_when_model_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url=_CONFIGURED_BASE_URL, model=""),
    )
    assert llm_configured() is False


def test_llm_configured_false_when_whitespace_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url="   ", model="  "),
    )
    assert llm_configured() is False


def test_llm_configured_true_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # A keyless OpenAI-compatible gateway is legitimate: config depends on
    # URL + model only, never on the key.
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url=_CONFIGURED_BASE_URL, model=_CONFIGURED_MODEL, api_key=""),
    )
    assert llm_configured() is True


def test_llm_configured_false_when_base_url_syntactically_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A base URL AsyncOpenAI would reject at construction (invalid port) is not a
    # usable endpoint. llm_configured reparses it with httpx.URL, the same way
    # the SDK does, and reports unconfigured -- so the status endpoint agrees
    # with the 503 the workflows would raise for the identical config.
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url="http://h:8o80/v1", model=_CONFIGURED_MODEL),
    )
    assert llm_configured() is False


# --- generate_json: config gate ------------------------------------------


def test_generate_json_unconfigured_raises_before_building_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config gate runs first: no client is built and nothing is sent."""
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url="", model=""),
    )

    def _must_not_build() -> _StubClient:
        raise AssertionError("_get_client must not run when unconfigured")

    monkeypatch.setattr("app.services.llm._get_client", _must_not_build)
    with pytest.raises(LLMNotConfiguredError):
        asyncio.run(generate_json("system", "user"))


# --- generate_json: happy parsing ----------------------------------------


def test_generate_json_plain_object(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, content=_SAMPLE_JSON)
    assert asyncio.run(generate_json("system", "user")) == _SAMPLE_OBJECT


def test_generate_json_strips_json_code_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    fenced = f"```json\n{_SAMPLE_JSON}\n```"
    _configured(monkeypatch, content=fenced)
    assert asyncio.run(generate_json("system", "user")) == _SAMPLE_OBJECT


def test_generate_json_strips_bare_code_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    fenced = f"```\n{_SAMPLE_JSON}\n```"
    _configured(monkeypatch, content=fenced)
    assert asyncio.run(generate_json("system", "user")) == _SAMPLE_OBJECT


def test_generate_json_extracts_object_from_prose(monkeypatch: pytest.MonkeyPatch) -> None:
    prose = f"Sure, here is the draft:\n{_SAMPLE_JSON}\nLet me know if you want changes."
    _configured(monkeypatch, content=prose)
    assert asyncio.run(generate_json("system", "user")) == _SAMPLE_OBJECT


def test_generate_json_passes_model_and_low_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _configured(monkeypatch, content=_SAMPLE_JSON)
    asyncio.run(generate_json("SYSTEM PROMPT", "USER PROMPT"))
    call = stub.chat.completions.calls[0]
    assert call["model"] == _CONFIGURED_MODEL
    assert call["temperature"] == 0.2
    assert call["messages"] == [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "USER PROMPT"},
    ]


# --- generate_json: failure paths (all -> LLMUpstreamError) ---------------


def test_generate_json_invalid_output_raises_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, content="this is not json at all, sorry")
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_json("system", "user"))


def test_generate_json_array_output_raises_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bare JSON array parses but is not a usable object for the workflows.
    _configured(monkeypatch, content="[1, 2, 3]")
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_json("system", "user"))


def test_generate_json_empty_content_raises_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, content="   ")
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_json("system", "user"))


def test_generate_json_none_content_raises_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, content=None)
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_json("system", "user"))


def test_generate_json_empty_choices_raises_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, empty_choices=True)
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_json("system", "user"))


def test_generate_json_api_error_raises_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, exc=openai.OpenAIError("simulated api failure"))
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_json("system", "user"))


def test_generate_json_timeout_raises_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", f"{_CONFIGURED_BASE_URL}/chat/completions")
    _configured(monkeypatch, exc=openai.APITimeoutError(request=request))
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_json("system", "user"))


def test_upstream_error_message_never_leaks_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even when the SDK error text embeds URL/key-like values, the safe
    LLMUpstreamError message must carry only the exception category, never the
    configured base URL or API key.
    """
    leaky = openai.OpenAIError(
        f"connect to {_CONFIGURED_BASE_URL} failed using key {_CONFIGURED_KEY}"
    )
    _configured(monkeypatch, exc=leaky)
    with pytest.raises(LLMUpstreamError) as excinfo:
        asyncio.run(generate_json("system", "user"))

    message = str(excinfo.value)
    assert "OpenAIError" in message
    assert _CONFIGURED_BASE_URL not in message
    assert "llm.internal.example" not in message
    assert _CONFIGURED_KEY not in message


# --- pure extraction helpers ---------------------------------------------


def test_balanced_brace_slice_ignores_braces_inside_strings() -> None:
    text = 'prefix {"a": "has a } brace", "b": {"c": 1}} suffix'
    sliced = _balanced_brace_slice(text)
    assert sliced is not None
    assert json.loads(sliced) == {"a": "has a } brace", "b": {"c": 1}}


def test_balanced_brace_slice_none_without_object() -> None:
    assert _balanced_brace_slice("no object here") is None


def test_extract_json_object_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    nested = '{"outer": {"inner": [1, 2]}, "flag": true}'
    assert _extract_json_object(nested) == {"outer": {"inner": [1, 2]}, "flag": True}


def test_extract_json_object_invalid_raises_upstream() -> None:
    with pytest.raises(LLMUpstreamError):
        _extract_json_object("definitely not json")


def test_extract_json_object_deeply_nested_brackets_raises_upstream() -> None:
    """Tens of thousands of nested ``[`` make json.loads raise RecursionError,
    not JSONDecodeError. That must be caught and mapped to the same unparseable
    502 as any other bad output -- never left to escape as an unhandled 500.
    """
    deep = "[" * 50000 + "]" * 50000
    with pytest.raises(LLMUpstreamError):
        _extract_json_object(deep)


# --- generate_json: nonconforming-but-200 upstream bodies (finding 2) ------


@pytest.mark.parametrize(
    "completion",
    [
        SimpleNamespace(choices=None),
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices=[SimpleNamespace()]),
        SimpleNamespace(choices=[SimpleNamespace(message=None)]),
    ],
    ids=["choices-none", "choices-empty", "choice-without-message", "message-none"],
)
def test_generate_json_nonconforming_200_raises_upstream(
    monkeypatch: pytest.MonkeyPatch, completion: Any
) -> None:
    """A compatible gateway can return a 200 whose body violates the SDK's
    expected shape (choices None/empty/non-list, a choice with no message, a
    null message). The lenient SDK does not reject it, so the service must:
    every such shape becomes an LLMUpstreamError (502), never an AttributeError
    or TypeError surfacing as a 500.
    """
    _install_raw(monkeypatch, completion)
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_json("system", "user"))


# --- generate_json: malformed endpoint config (finding 1) ------------------


def test_generate_json_malformed_endpoint_raises_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A syntactically invalid endpoint (bad port) makes AsyncOpenAI raise at
    CONSTRUCTION -- an httpx.InvalidURL, not an OpenAIError -- which the config
    gate must catch and remap to LLMNotConfiguredError (503). The real
    _get_client/_build_client run here (not stubbed) so construction genuinely
    fails, and the raised error must carry neither the URL fragment nor the key.
    """
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url="http://h:8o80/v1", model=_CONFIGURED_MODEL),
    )
    with pytest.raises(LLMNotConfiguredError) as excinfo:
        asyncio.run(generate_json("system", "user"))

    message = str(excinfo.value)
    assert "8o80" not in message
    assert _CONFIGURED_KEY not in message
    # `from None` clears __cause__ and sets __suppress_context__, so the
    # httpx.InvalidURL (whose text carries the URL fragment) is suppressed from
    # any rendered traceback -- exactly what a logger's exc_info would emit.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    assert "8o80" not in rendered
    assert _CONFIGURED_KEY not in rendered


# --- retry policy (finding 6) ---------------------------------------------


def test_build_client_disables_automatic_retries() -> None:
    """max_retries=0 so the configured openai_timeout_seconds is the true
    end-to-end latency bound: a retry would silently wait out the whole timeout
    again (plus backoff), multiplying real-failure latency for an interactive
    tool. Asserted on the constructed client's own attribute, not via wall-clock
    timing, so it is deterministic.
    """
    client = _build_client("http://retry-policy.example/v1", "k", 1.0)
    assert client.max_retries == 0
