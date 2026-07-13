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
import time
import traceback
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app.config import Settings
from app.services.llm import (
    _UPSTREAM_REASON,
    LLMNotConfiguredError,
    LLMUpstreamError,
    _balanced_brace_slice,
    _build_client,
    _extract_json_object,
    _get_client,
    _json_candidates,
    generate_json,
    llm_configured,
    normalized_model,
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
        delay: float = 0.0,
    ) -> None:
        self._content = content
        self._exc = exc
        self._empty_choices = empty_choices
        # Simulates an upstream call that never returns within the caller's
        # wall-clock deadline (a slow-drip endpoint) -- see
        # test_generate_json_wall_clock_timeout_raises_upstream_with_timeout_category.
        self._delay = delay
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
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


# --- normalized_model ------------------------------------------------------


def test_normalized_model_strips_surrounding_whitespace() -> None:
    settings = _settings(base_url=_CONFIGURED_BASE_URL, model="  test-model  ")
    assert normalized_model(settings) == "test-model"


def test_normalized_model_matches_llm_configured_and_generate_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same helper backs llm_configured's gate and generate_json's actual
    # request, so a whitespace-padded model can never make them disagree.
    settings = _settings(base_url=_CONFIGURED_BASE_URL, model="  padded-model  ")
    stub = _install(monkeypatch, settings, _StubClient(content=_SAMPLE_JSON))
    assert normalized_model(settings) == "padded-model"
    assert llm_configured() is True
    asyncio.run(generate_json("system", "user"))
    assert stub.chat.completions.calls[0]["model"] == "padded-model"


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


def test_generate_json_passes_model_and_omits_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    """No fixed temperature is sent: some OpenAI-compatible endpoints --
    reasoning-style models in particular -- reject the parameter outright,
    which would otherwise turn every call against them into a 400 -> 502.
    Omitting it lets the model/endpoint apply its own default.
    """
    stub = _configured(monkeypatch, content=_SAMPLE_JSON)
    asyncio.run(generate_json("SYSTEM PROMPT", "USER PROMPT"))
    call = stub.chat.completions.calls[0]
    assert call["model"] == _CONFIGURED_MODEL
    assert "temperature" not in call
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


def test_generate_json_array_of_objects_raises_upstream_not_first_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A top-level array of OBJECTS -- unlike a bare array of scalars above --
    once let the balanced-brace fallback inside _extract_json_object silently
    extract and return just the FIRST element, quietly persisting a shape the
    caller never asked for instead of surfacing the true "wrong shape"
    failure. The full array must still be rejected as a whole: 502, and the
    (never reached) first element is not returned.
    """
    array_content = json.dumps([{"title": "A"}, {"title": "B"}])
    _configured(monkeypatch, content=array_content)
    with pytest.raises(LLMUpstreamError) as excinfo:
        asyncio.run(generate_json("system", "user"))
    assert "WrongShape" in str(excinfo.value)


def test_generate_json_prose_wrapped_array_raises_upstream_wrong_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prose-wrapped counterpart to the regression above: the array is
    embedded in prose instead of being the whole completion, so the full-text
    parse FAILS and this exercises the candidate-scan fallback inside
    _extract_json_object instead of the top-level-shape check. It must still
    reject the array as a whole rather than let the fallback extract and
    return just its first element.
    """
    array_json = json.dumps([{"title": "A"}, {"title": "B"}])
    _configured(monkeypatch, content=f"Sure, here you go: {array_json}")
    with pytest.raises(LLMUpstreamError) as excinfo:
        asyncio.run(generate_json("system", "user"))
    assert "WrongShape" in str(excinfo.value)


def test_generate_json_juxtaposed_objects_raises_upstream_wrong_shape_not_first_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10: two objects juxtaposed (not wrapped in an array, not
    comma-separated) fail the full-text parse as "extra data" and reach the
    candidate-scan fallback, which pre-fix returned on the first usable dict
    it found -- silently persisting "A" and dropping "B". The exactly-one
    rule must reject the pair as a whole instead.
    """
    _configured(monkeypatch, content='{"title": "A"} {"title": "B"}')
    with pytest.raises(LLMUpstreamError) as excinfo:
        asyncio.run(generate_json("system", "user"))
    assert "WrongShape" in str(excinfo.value)


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
    """The SDK raising its own timeout error immediately (as opposed to the
    call simply hanging -- see the wall-clock deadline test below) must still
    map to LLMUpstreamError via the ordinary OpenAIError arm.
    """
    request = httpx.Request("POST", f"{_CONFIGURED_BASE_URL}/chat/completions")
    _configured(monkeypatch, exc=openai.APITimeoutError(request=request))
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_json("system", "user"))


# --- generate_json: wall-clock deadline (finding: asyncio.timeout) --------


def test_generate_json_wall_clock_timeout_raises_upstream_with_timeout_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asyncio.timeout wraps the whole SDK call with a genuine wall-clock
    deadline. A stub that never returns within the configured
    openai_timeout_seconds must still raise LLMUpstreamError -- with the
    distinct "Timeout" category -- well before the stub's own (much longer)
    delay elapses. This is exactly what the client-level timeout alone cannot
    guarantee (see _build_client's docstring): a slow-drip endpoint that keeps
    sending a byte just before each read timeout would otherwise hold the
    request open indefinitely.
    """
    settings = Settings(
        openai_base_url=_CONFIGURED_BASE_URL,
        openai_api_key=_CONFIGURED_KEY,
        openai_model=_CONFIGURED_MODEL,
        openai_timeout_seconds=0.05,
    )
    stub = _install(monkeypatch, settings, _StubClient(content=_SAMPLE_JSON, delay=1.0))

    started = time.monotonic()
    with pytest.raises(LLMUpstreamError) as excinfo:
        asyncio.run(generate_json("system", "user"))
    elapsed = time.monotonic() - started

    message = str(excinfo.value)
    assert message.startswith("Timeout: ")
    assert _UPSTREAM_REASON in message
    # `from None` severs the cause chain, same as every other arm in this
    # boundary.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    # Bounded by the configured deadline, not the stub's 1s delay -- proves
    # this is a genuine wall-clock cap around the whole call, not merely the
    # SDK/httpx per-phase inactivity timer (irrelevant here anyway, since this
    # stub bypasses the real client entirely).
    assert elapsed < 0.5

    # The request was still genuinely attempted (with the model/messages, no
    # temperature) before the deadline cut it off.
    assert stub.chat.completions.calls[0]["model"] == _CONFIGURED_MODEL


# --- generate_json: total SDK-call boundary (finding 1) -------------------


def test_generate_json_sdk_json_decode_error_raises_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merely OpenAI-*compatible* endpoint can return a 2xx whose body is
    broken JSON; the SDK parses that body INTERNALLY and raises
    json.JSONDecodeError -- NOT an OpenAIError. The total call boundary must map
    it to LLMUpstreamError (502), never let it escape as an unhandled 500.
    """
    _configured(monkeypatch, exc=json.JSONDecodeError("Expecting value", "", 0))
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_json("system", "user"))


def test_generate_json_sdk_value_error_raises_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare ValueError from the SDK boundary (an empty-body parse) is likewise
    mapped onto the 502 taxonomy rather than surfacing as a 500.
    """
    _configured(monkeypatch, exc=ValueError("could not parse response body"))
    with pytest.raises(LLMUpstreamError):
        asyncio.run(generate_json("system", "user"))


def test_generate_json_sdk_parse_error_message_carries_only_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mapped message is the safe category + fixed reason -- config-free and
    chain-severed -- exactly as for an OpenAIError, so a broken response body can
    never ride along in __cause__ into a traceback sink.
    """
    _configured(monkeypatch, exc=json.JSONDecodeError("Expecting value", "raw body", 0))
    with pytest.raises(LLMUpstreamError) as excinfo:
        asyncio.run(generate_json("system", "user"))
    message = str(excinfo.value)
    assert "JSONDecodeError" in message
    assert _UPSTREAM_REASON in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_generate_json_not_configured_error_from_create_stays_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 stays 503: if the SDK call itself surfaces one of our own taxonomy
    errors, the total boundary re-raises it UNCHANGED -- it must never rewrap an
    LLMNotConfiguredError into a generic 502 and destroy its real status.
    """
    _configured(monkeypatch, exc=LLMNotConfiguredError("still unconfigured"))
    with pytest.raises(LLMNotConfiguredError):
        asyncio.run(generate_json("system", "user"))


def test_generate_json_upstream_error_from_create_passes_through_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-shaped LLMUpstreamError raised at the SDK call is re-raised as
    the SAME object (502 stays 502), never double-wrapped by the catch-all.
    """
    original = LLMUpstreamError("EmptyResponse: the LLM returned no choices")
    _configured(monkeypatch, exc=original)
    with pytest.raises(LLMUpstreamError) as excinfo:
        asyncio.run(generate_json("system", "user"))
    assert excinfo.value is original


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

    # `from None` severs the cause chain: the original SDK error (whose text
    # embeds the URL and key above) must not ride along in __cause__ into a
    # rendered traceback -- exactly what a logger's exc_info would emit.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    assert _CONFIGURED_BASE_URL not in rendered
    assert "llm.internal.example" not in rendered
    assert _CONFIGURED_KEY not in rendered


# --- llm_configured: parseable-but-unusable URLs (finding: strict endpoint) --


@pytest.mark.parametrize(
    "base_url",
    ["localhost:8000/v1", "http:///v1", "ftp://h/v1"],
    ids=["scheme-less-authority", "empty-host", "non-http-scheme"],
)
def test_llm_configured_false_for_parseable_but_unusable_url(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """A URL that parses under httpx.URL but carries no http/https scheme or no
    host is not a reachable OpenAI-compatible endpoint: llm_configured must
    report unconfigured, and generate_json must gate to LLMNotConfiguredError
    (503) before any client is built or request sent.
    """
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url=base_url, model=_CONFIGURED_MODEL),
    )
    assert llm_configured() is False

    def _must_not_build() -> _StubClient:
        raise AssertionError("_get_client must not run when unconfigured")

    monkeypatch.setattr("app.services.llm._get_client", _must_not_build)
    with pytest.raises(LLMNotConfiguredError):
        asyncio.run(generate_json("system", "user"))


@pytest.mark.parametrize(
    "base_url",
    ["http://host.example/v1", "https://host.example/v1"],
    ids=["http", "https"],
)
def test_llm_configured_true_for_http_and_https_with_host(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url=base_url, model=_CONFIGURED_MODEL),
    )
    assert llm_configured() is True


# --- llm_configured: out-of-range port (finding: port range) --------------


@pytest.mark.parametrize(
    "base_url",
    ["http://host.example:99999/v1", "http://host.example:0/v1"],
    ids=["port-above-65535", "port-zero"],
)
def test_llm_configured_false_for_out_of_range_port(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """httpx.URL parses a numerically out-of-range port (99999, beyond the
    16-bit TCP range) or port 0 WITHOUT error -- unlike a non-numeric port such
    as "8o80", which fails at the httpx.URL/AsyncOpenAI construction stage --
    and AsyncOpenAI builds a client from it happily. But no TCP connect can
    ever target such a port, so every real call would fail as a 502 instead of
    the config-error 503 this function exists to produce. llm_configured must
    report unconfigured, and generate_json must gate to LLMNotConfiguredError
    (503) before any client is built or request sent.
    """
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url=base_url, model=_CONFIGURED_MODEL),
    )
    assert llm_configured() is False

    def _must_not_build() -> _StubClient:
        raise AssertionError("_get_client must not run when unconfigured")

    monkeypatch.setattr("app.services.llm._get_client", _must_not_build)
    with pytest.raises(LLMNotConfiguredError):
        asyncio.run(generate_json("system", "user"))


def test_llm_configured_true_for_explicit_valid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal, in-range explicit port must remain configured -- the new port
    check rejects only 0 and values above 65535, never an ordinary port.
    """
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url="http://host.example:8000/v1", model=_CONFIGURED_MODEL),
    )
    assert llm_configured() is True


def test_configured_and_get_client_agree_on_whitespace_padded_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-padded URL reads as configured (llm_configured strips before
    validating) AND the real client is built from the SAME stripped value, so
    status and runtime never disagree over stray whitespace. _get_client runs
    for real here (not stubbed) so construction genuinely uses the stripped URL.
    """
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url="  http://spaced.example/v1  ", model=_CONFIGURED_MODEL),
    )
    assert llm_configured() is True
    rendered = str(_get_client().base_url)
    assert "spaced.example" in rendered
    assert " " not in rendered


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


def test_json_candidates_orders_outer_array_before_nested_objects() -> None:
    """An array's span starts before any object nested inside it (nesting
    implies the outer bracket opens first), so ordering by start position
    alone guarantees the outer array is the FIRST candidate considered --
    this is what lets _extract_json_object reject the whole array before ever
    reaching one of its own elements.
    """
    array_json = json.dumps([{"title": "A"}, {"title": "B"}])
    text = f"prose {array_json} more prose"
    candidates = _json_candidates(text)
    assert candidates[0] == array_json


def test_json_candidates_finds_both_bracket_types_in_order_of_appearance() -> None:
    """A scalar array and a later object are both found, in the order they
    appear in the text -- regardless of bracket type -- so a caller scanning
    candidates left to right reaches the object right after the array.
    """
    text = f"Answer[1]: {_SAMPLE_JSON}"
    assert _json_candidates(text) == ["[1]", _SAMPLE_JSON]


def test_json_candidates_none_without_brackets() -> None:
    assert _json_candidates("no brackets here at all") == []


# --- _extract_json_object: non-object top level (finding 1) ---------------


@pytest.mark.parametrize(
    "value",
    ['[{"title": "A"}, {"title": "B"}]', '"just a string"', "42", "true", "null"],
    ids=["array-of-objects", "string", "number", "bool", "null"],
)
def test_extract_json_object_top_level_non_object_raises_upstream_wrong_shape(value: str) -> None:
    """The full text parsing successfully as JSON but NOT as an object -- most
    notably a top-level array of objects, whose first element the
    balanced-brace fallback could otherwise mistake for a valid embedded
    object -- must raise immediately rather than fall through to that
    fallback. See the array-of-objects regression at the generate_json level:
    test_generate_json_array_of_objects_raises_upstream_not_first_element.
    """
    with pytest.raises(LLMUpstreamError) as excinfo:
        _extract_json_object(value)
    assert "WrongShape" in str(excinfo.value)


def test_extract_json_object_prose_wrapped_still_falls_back_to_brace_slice() -> None:
    """Full text that FAILS to parse as JSON at all (prose is not valid JSON)
    is unaffected by the new top-level-shape check above: this must still fall
    through to the balanced-brace fallback and recover the embedded object.
    """
    prose = 'Sure, here is the draft: {"title": "A"} Hope this helps!'
    assert _extract_json_object(prose) == {"title": "A"}


def test_extract_json_object_prose_wrapped_array_raises_upstream_wrong_shape() -> None:
    """The array-of-objects rejection must also hold when the array is
    embedded in prose rather than being the LLM's whole answer: prose makes
    the full-text parse FAIL, so this exercises the candidate-scan fallback,
    not the top-level-shape check. Without this, a naive fallback would
    extract just the array's first element and silently persist a shape the
    caller never asked for.
    """
    array_json = json.dumps([{"title": "A"}, {"title": "B"}])
    prose = f"Here are two drafts: {array_json} Take your pick!"
    with pytest.raises(LLMUpstreamError) as excinfo:
        _extract_json_object(prose)
    assert "WrongShape" in str(excinfo.value)


def test_extract_json_object_juxtaposed_objects_raises_upstream_wrong_shape() -> None:
    """Two top-level objects juxtaposed rather than wrapped in an array or
    comma-separated (``{"title": "A"} {"title": "B"}``) make the full-text
    parse fail as "extra data" -- neither the top-level-shape check (not valid
    JSON at all) nor the old array check (there is no array here) catches
    this. Pre-fix, the candidate-scan fallback returned on the FIRST usable
    dict it found, silently dropping the second. The exactly-one rule closes
    this: the scan finds two usable dicts, so it must reject the pair as a
    whole -- 502 WrongShape, not a silent pick of "A".
    """
    prose = '{"title": "A"} {"title": "B"}'
    with pytest.raises(LLMUpstreamError) as excinfo:
        _extract_json_object(prose)
    assert "WrongShape" in str(excinfo.value)
    assert "multiple JSON objects" in str(excinfo.value)


def test_extract_json_object_three_juxtaposed_objects_raises_upstream_wrong_shape() -> None:
    """The exactly-one rule is a COUNT, not a special case for exactly two:
    three juxtaposed objects must be rejected the same way as two.
    """
    prose = '{"title": "A"} {"title": "B"} {"title": "C"}'
    with pytest.raises(LLMUpstreamError) as excinfo:
        _extract_json_object(prose)
    assert "WrongShape" in str(excinfo.value)
    assert "multiple JSON objects" in str(excinfo.value)


def test_extract_json_object_skips_innocent_scalar_bracket_before_object() -> None:
    """A scalar array that appears before the real object in prose (e.g. a
    footnote-style "[1]") must not be mistaken for the answer, and must not
    block the scan from reaching the object that follows it: it parses as a
    list with no dict inside, so the scan skips it and keeps going.
    """
    prose = f"Answer[1]: {_SAMPLE_JSON} (see footnote 1 for caveats)"
    assert _extract_json_object(prose) == _SAMPLE_OBJECT


def test_extract_json_object_skips_innocent_scalar_brackets_before_and_after_object() -> None:
    """The exactly-one rule counts USABLE DICTS only: scalar-array junk both
    before AND after the real object must neither be mistaken for a second
    usable object nor block the scan from finishing. This is what proves the
    new walk-to-completion behaviour (needed to catch a second dict anywhere
    in the text) does not turn trailing scalar noise into a false
    "multiple objects" rejection.
    """
    prose = f"Answer[1]: {_SAMPLE_JSON} (see footnote [2] for caveats)"
    assert _extract_json_object(prose) == _SAMPLE_OBJECT


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


# --- candidate scan: bounded, no pathological rescans (prose-wrapped array) -


def test_extract_json_object_bounded_with_many_unmatched_brace_opens() -> None:
    """A long run of unmatched ``{`` with no closing ``}`` at all must not
    trigger an O(n^2) "retry every position independently" scan: the
    candidate scan performs one linear pass per bracket type regardless of
    how many opens never find a match, so this must stay fast even at a size
    where a quadratic implementation would visibly stall (tens of seconds or
    more, versus low milliseconds here).
    """
    text = "{" * 30000 + " not valid json, just noise"
    started = time.monotonic()
    with pytest.raises(LLMUpstreamError) as excinfo:
        _extract_json_object(text)
    elapsed = time.monotonic() - started
    assert "UnparseableOutput" in str(excinfo.value)
    assert elapsed < 2.0


def test_extract_json_object_bounded_with_many_skippable_candidates() -> None:
    """Many innocent scalar-array candidates ahead of the real object -- each
    individually cheap to reject -- must not add up to quadratic work:
    finding every candidate is two linear passes over the whole text (see
    _json_candidates), so this must stay fast even with thousands of them
    ahead of the object the scan is really looking for.
    """
    noise = "".join(f"[{i}]" for i in range(5000))
    prose = f"{noise} {_SAMPLE_JSON}"
    started = time.monotonic()
    result = _extract_json_object(prose)
    elapsed = time.monotonic() - started
    assert result == _SAMPLE_OBJECT
    assert elapsed < 2.0


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
    """max_retries=0 so a retry never silently waits out the whole per-attempt
    timeout again (plus backoff), multiplying real-failure latency for an
    interactive tool -- one piece of what keeps openai_timeout_seconds close to
    an end-to-end bound; the genuine wall-clock deadline is the asyncio.timeout
    wrapped around the call in generate_json (see
    test_generate_json_wall_clock_timeout_raises_upstream_with_timeout_category).
    Asserted on the constructed client's own attribute, not via wall-clock
    timing, so it is deterministic.
    """
    client = _build_client("http://retry-policy.example/v1", "k", 1.0)
    assert client.max_retries == 0
