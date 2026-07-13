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
