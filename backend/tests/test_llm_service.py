"""Tests for the LLM client service (app.services.llm).

All network I/O is stubbed: a fake client whose ``chat.completions.create`` is
driven per-test replaces the real ``_get_client`` (via monkeypatch), and
``get_settings`` is overridden so ``llm_configured`` reports the desired state.
No real ``AsyncOpenAI`` is ever constructed and nothing reaches the network.

``generate_structured`` is async; each call is driven with ``asyncio.run`` so no
pytest-asyncio plugin is required (none is a project dependency).

The old forgiving JSON scavenger (``_extract_json_object`` and its exactly-one /
array-element-context rules) is gone. In its place is a strict schema-guided
contract: the model is shown the target JSON Schema and must emit EXACTLY one
conforming object; any deviation earns ONE corrective retry, and a second
failure is a fixed ``InvalidStructuredOutput`` 502. The scavenger's unit tests
are deleted; the behaviours they pinned (prose-wrapped objects, juxtaposed
objects, bare/nested/garbled arrays, scalar junk, top-level scalars) are
re-expressed here as retry-contract tests.
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
from pydantic import BaseModel, ConfigDict

from app.config import Settings
from app.services.llm import (
    _INVALID_STRUCTURED_OUTPUT,
    _STRICT_OUTPUT_RULE,
    _UPSTREAM_REASON,
    LLMNotConfiguredError,
    LLMUpstreamError,
    _build_client,
    _get_client,
    generate_structured,
    llm_configured,
    normalized_model,
)
from app.services.memory_ai import (
    CAPTURE_SYSTEM_PROMPT,
    ENRICH_SYSTEM_PROMPT,
    UPDATE_SYSTEM_PROMPT,
    CaptureDraft,
    EnrichResult,
    UpdateResult,
)

_CONFIGURED_BASE_URL = "http://llm.internal.example/v1"
_CONFIGURED_KEY = "sk-super-secret-key"
_CONFIGURED_MODEL = "test-model"


class _Sample(BaseModel):
    """Minimal target model for the parse/retry mechanics tests, independent of
    the memory_ai workflow models (which have their own richer sanitizers)."""

    model_config = ConfigDict(extra="ignore")

    title: str
    count: int = 0


_SAMPLE_OBJECT: dict[str, Any] = {"title": "Draft", "count": 2}
_SAMPLE_JSON = json.dumps(_SAMPLE_OBJECT)


class _StubCompletions:
    """Stand-in for ``client.chat.completions`` with a scripted ``create``.

    ``content`` returns the same string every call; ``contents`` scripts a
    per-call sequence (the last element is held once the script is exhausted, so
    a bad-then-bad retry keeps failing identically). Every call's kwargs -- most
    importantly ``messages`` -- are recorded in ``self.calls`` so a test can
    assert how many attempts ran and what the retry request carried.
    """

    def __init__(
        self,
        *,
        content: str | None = None,
        contents: list[str] | None = None,
        exc: Exception | None = None,
        empty_choices: bool = False,
        delay: float = 0.0,
    ) -> None:
        self._content = content
        self._contents = list(contents) if contents is not None else None
        self._exc = exc
        self._empty_choices = empty_choices
        # Simulates an upstream call that never returns within the caller's
        # wall-clock deadline (a slow-drip endpoint) -- see the timeout tests.
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
        if self._contents is not None:
            content = self._contents[0] if len(self._contents) == 1 else self._contents.pop(0)
        else:
            content = self._content
        message = SimpleNamespace(content=content)
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


def _run(system: str = "system", user: str = "user") -> _Sample:
    """Drive generate_structured for the local _Sample model."""
    return asyncio.run(generate_structured(system, user, _Sample))


def _calls(stub: _StubClient) -> list[dict[str, Any]]:
    return stub.chat.completions.calls


class _RawCompletions:
    """``client.chat.completions`` whose ``create`` returns a caller-supplied
    completion object VERBATIM -- to model a nonconforming-but-200 body from a
    merely OpenAI-compatible gateway (missing/None message, null content, etc.).
    """

    def __init__(self, completion: Any) -> None:
        self._completion = completion
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._completion


class _RawClient:
    def __init__(self, completion: Any) -> None:
        self.chat = SimpleNamespace(completions=_RawCompletions(completion))


def _install_raw(monkeypatch: pytest.MonkeyPatch, completion: Any) -> _RawClient:
    settings = _settings(base_url=_CONFIGURED_BASE_URL, model=_CONFIGURED_MODEL)
    monkeypatch.setattr("app.services.llm.get_settings", lambda: settings)
    client = _RawClient(completion)
    monkeypatch.setattr("app.services.llm._get_client", lambda: client)
    return client


# The scavenger-behaviour cases, re-expressed as bad completions the strict
# contract must reject (each is either not valid JSON, or valid JSON that is not
# a single object). These drive the retry-contract tests below: bad-then-good ->
# success in two attempts, bad-then-bad -> 502 in two attempts.
_BAD_SHAPES: list[tuple[str, str]] = [
    ("prose-wrapped-object", f"Sure, here is the draft:\n{_SAMPLE_JSON}\nHope that helps!"),
    ("bare-array", "[1, 2, 3]"),
    ("array-of-objects", json.dumps([{"title": "A"}, {"title": "B"}])),
    ("prose-wrapped-array", f"Here you go: {json.dumps([{'title': 'A'}, {'title': 'B'}])}"),
    ("nested-array-then-object", '[[{"title": "A"}]] {"title": "B"}'),
    ("garbled-array", '[{bad}, note: {"title": "A"}]'),
    ("juxtaposed-objects", '{"title": "A"} {"title": "B"}'),
    ("scalar-junk", "Answer[1]: no object here at all"),
    ("top-level-string", '"just a string"'),
    ("top-level-number", "42"),
    ("top-level-bool", "true"),
    ("top-level-null", "null"),
    ("not-json", "definitely not json"),
    ("huge-integer", "1" * 5000),
]
_BAD_IDS = [name for name, _ in _BAD_SHAPES]


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


def test_normalized_model_matches_llm_configured_and_generate_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same helper backs llm_configured's gate and generate_structured's
    # actual request, so a whitespace-padded model can never make them disagree.
    settings = _settings(base_url=_CONFIGURED_BASE_URL, model="  padded-model  ")
    stub = _install(monkeypatch, settings, _StubClient(content=_SAMPLE_JSON))
    assert normalized_model(settings) == "padded-model"
    assert llm_configured() is True
    _run()
    assert _calls(stub)[0]["model"] == "padded-model"


# --- generate_structured: config gate ------------------------------------


def test_generate_structured_unconfigured_raises_before_building_client(
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
        _run()


# --- generate_structured: happy parsing ----------------------------------


def test_generate_structured_returns_validated_model_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single conforming object is parsed AND validated into the target model
    (not returned as a bare dict), in exactly one attempt."""
    stub = _configured(monkeypatch, content=_SAMPLE_JSON)
    result = _run()
    assert isinstance(result, _Sample)
    assert result.title == "Draft"
    assert result.count == 2
    assert len(_calls(stub)) == 1


def test_generate_structured_strips_json_code_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _configured(monkeypatch, content=f"```json\n{_SAMPLE_JSON}\n```")
    assert _run().title == "Draft"
    assert len(_calls(stub)) == 1  # a fenced bare object is accepted, not retried


def test_generate_structured_strips_bare_code_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _configured(monkeypatch, content=f"```\n{_SAMPLE_JSON}\n```")
    assert _run().title == "Draft"
    assert len(_calls(stub)) == 1


def test_generate_structured_passes_model_omits_temperature_and_injects_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fixed temperature is sent (some reasoning-style endpoints reject it,
    turning every call into a 400 -> 502). The system message is the caller's
    prompt augmented with the strict-output rule and the model's schema; the
    user message is passed through untouched.
    """
    stub = _configured(monkeypatch, content=_SAMPLE_JSON)
    _run("SYSTEM PROMPT", "USER PROMPT")
    call = _calls(stub)[0]
    assert call["model"] == _CONFIGURED_MODEL
    assert "temperature" not in call
    messages = call["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].startswith("SYSTEM PROMPT")
    assert _STRICT_OUTPUT_RULE in messages[0]["content"]
    # The _Sample schema (its "title"/"count" properties) rides in the system msg.
    assert '"title"' in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "USER PROMPT"}


# --- generate_structured: schema injection for the three workflows --------


@pytest.mark.parametrize(
    "prompt, model_cls, distinctive_field, good",
    [
        (CAPTURE_SYSTEM_PROMPT, CaptureDraft, "recovery_keywords", {"title": "t"}),
        (ENRICH_SYSTEM_PROMPT, EnrichResult, "checklist_complete", {"checklist_complete": True}),
        (UPDATE_SYSTEM_PROMPT, UpdateResult, "progress_note", {"progress_note": "n"}),
    ],
    ids=["capture", "enrich", "update"],
)
def test_generate_structured_injects_each_workflow_schema_and_strict_rule(
    monkeypatch: pytest.MonkeyPatch,
    prompt: str,
    model_cls: type[BaseModel],
    distinctive_field: str,
    good: dict[str, Any],
) -> None:
    """For all three workflows, the outgoing system prompt carries the strict
    single-object rule AND the model's own JSON Schema (spot-checked via a
    distinctive field name), appended after the methodology prompt.
    """
    stub = _configured(monkeypatch, content=json.dumps(good))
    asyncio.run(generate_structured(prompt, "user", model_cls))
    system_message = _calls(stub)[0]["messages"][0]["content"]
    assert system_message.startswith(prompt)  # methodology rules preserved verbatim
    assert _STRICT_OUTPUT_RULE in system_message
    assert distinctive_field in system_message  # the model's schema is present


# --- generate_structured: corrective retry (replaces the scavenger) -------


@pytest.mark.parametrize("content", [c for _, c in _BAD_SHAPES], ids=_BAD_IDS)
def test_generate_structured_retries_bad_then_good_succeeds(
    monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """Every shape the old scavenger tried to salvage (prose-wrapped object,
    juxtaposed objects, bare/nested/garbled arrays, scalar junk, top-level
    scalars, a 5000-digit integer) is now rejected on the first attempt and
    corrected on the second: bad-then-good yields the validated model in EXACTLY
    two create() calls, and the retry request echoes the bad reply plus a
    corrective instruction.
    """
    stub = _configured(monkeypatch, contents=[content, _SAMPLE_JSON])
    result = _run()
    assert isinstance(result, _Sample)
    assert result.title == "Draft"

    calls = _calls(stub)
    assert len(calls) == 2
    # First attempt: just system + user, no corrective turn yet.
    assert len(calls[0]["messages"]) == 2
    # Second attempt: system, user, the echoed bad assistant reply, the corrective.
    retry_messages = calls[1]["messages"]
    assert len(retry_messages) == 4
    assert retry_messages[2] == {"role": "assistant", "content": content}
    corrective = retry_messages[3]
    assert corrective["role"] == "user"
    assert "previous reply was rejected" in corrective["content"]
    assert "corrected JSON object" in corrective["content"]


@pytest.mark.parametrize("content", [c for _, c in _BAD_SHAPES], ids=_BAD_IDS)
def test_generate_structured_bad_then_bad_raises_invalid_structured_output(
    monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """Two bad replies exhaust the single retry: a fixed InvalidStructuredOutput
    502 in EXACTLY two create() calls."""
    stub = _configured(monkeypatch, contents=[content, content])
    with pytest.raises(LLMUpstreamError) as excinfo:
        _run()
    assert str(excinfo.value) == _INVALID_STRUCTURED_OUTPUT
    assert len(_calls(stub)) == 2


def test_generate_structured_validation_failure_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A well-formed object that fails the target model's validation (here a
    missing required field) is an output-shape failure too: it is retried, and a
    good second reply succeeds. This is what makes the memory_ai validator
    families (empty title, all-empty result, lone surrogate) retryable now that
    validation lives inside generate_structured.
    """
    stub = _configured(monkeypatch, contents=['{"count": 5}', _SAMPLE_JSON])
    result = _run()
    assert result.title == "Draft"
    assert len(_calls(stub)) == 2


def test_generate_structured_good_first_reply_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conforming first reply must NOT trigger a spurious second call."""
    stub = _configured(monkeypatch, contents=[_SAMPLE_JSON, _SAMPLE_JSON])
    _run()
    assert len(_calls(stub)) == 1


def test_generate_structured_huge_integer_is_parse_failure_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-12 carryover: a 5000-digit integer makes json.loads raise a bare
    ValueError (not JSONDecodeError, and not an unhandled 500). It is caught as
    an output-shape failure -- retried, then a repeated failure is a clean 502.
    """
    stub = _configured(monkeypatch, contents=["1" * 5000, "1" * 5000])
    with pytest.raises(LLMUpstreamError) as excinfo:
        _run()
    assert str(excinfo.value) == _INVALID_STRUCTURED_OUTPUT
    assert len(_calls(stub)) == 2


def test_generate_structured_invalid_output_message_leaks_no_internals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secret hygiene for the terminal structured-output 502: the message is the
    fixed InvalidStructuredOutput literal and carries NO json/pydantic detail
    (those went to the LLM in the corrective request, not into our API error),
    and the cause chain is severed so nothing rides along in __cause__.
    Replaces the deleted _validate chain-severing test.
    """
    secret = "SECRET-MEMORY-CONTENT-do-not-leak-9f3a"
    bad = json.dumps({"snapshot": secret})  # valid JSON object, but no _Sample.title
    stub = _configured(monkeypatch, contents=[bad, bad])
    with pytest.raises(LLMUpstreamError) as excinfo:
        _run()
    exc = excinfo.value
    message = str(exc)
    assert message == _INVALID_STRUCTURED_OUTPUT
    for leaked in (secret, "ValidationError", "JSONDecodeError", "validation error", "title"):
        assert leaked not in message
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert secret not in rendered
    assert len(_calls(stub)) == 2


# --- generate_structured: empty/malformed content (no retry) --------------


def test_generate_structured_empty_content_raises_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _configured(monkeypatch, content="   ")
    with pytest.raises(LLMUpstreamError):
        _run()
    # An empty completion is a transport-shaped failure, not an output-shape one:
    # it is NOT retried.
    assert len(_calls(stub)) == 1


def test_generate_structured_none_content_raises_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _configured(monkeypatch, content=None)
    with pytest.raises(LLMUpstreamError):
        _run()
    assert len(_calls(stub)) == 1


def test_generate_structured_empty_choices_raises_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _configured(monkeypatch, empty_choices=True)
    with pytest.raises(LLMUpstreamError):
        _run()
    assert len(_calls(stub)) == 1


# --- generate_structured: transport failures (no retry) -------------------


def test_generate_structured_api_error_raises_upstream_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OpenAIError is a transport failure: mapped to 502 and NEVER retried
    (the corrective retry re-prompts only on bad-SHAPE output)."""
    stub = _configured(monkeypatch, exc=openai.OpenAIError("simulated api failure"))
    with pytest.raises(LLMUpstreamError):
        _run()
    assert len(_calls(stub)) == 1


def test_generate_structured_timeout_error_from_sdk_raises_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK raising its own timeout error immediately (an APITimeoutError, an
    OpenAIError subclass) maps via the OpenAIError arm to 502, one call."""
    request = httpx.Request("POST", f"{_CONFIGURED_BASE_URL}/chat/completions")
    stub = _configured(monkeypatch, exc=openai.APITimeoutError(request=request))
    with pytest.raises(LLMUpstreamError):
        _run()
    assert len(_calls(stub)) == 1


def test_generate_structured_sdk_json_decode_error_raises_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merely OpenAI-*compatible* endpoint can return a 2xx whose body is
    broken JSON; the SDK parses that body INTERNALLY and raises
    json.JSONDecodeError -- NOT an OpenAIError. The total call boundary maps it to
    502, one call (a transport-boundary failure, not retried).
    """
    stub = _configured(monkeypatch, exc=json.JSONDecodeError("Expecting value", "", 0))
    with pytest.raises(LLMUpstreamError):
        _run()
    assert len(_calls(stub)) == 1


def test_generate_structured_sdk_value_error_raises_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _configured(monkeypatch, exc=ValueError("could not parse response body"))
    with pytest.raises(LLMUpstreamError):
        _run()
    assert len(_calls(stub)) == 1


def test_generate_structured_sdk_parse_error_message_carries_only_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mapped message is the safe category + fixed reason -- config-free and
    chain-severed -- so a broken response body can never ride along in __cause__.
    """
    _configured(monkeypatch, exc=json.JSONDecodeError("Expecting value", "raw body", 0))
    with pytest.raises(LLMUpstreamError) as excinfo:
        _run()
    message = str(excinfo.value)
    assert "JSONDecodeError" in message
    assert _UPSTREAM_REASON in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_generate_structured_not_configured_error_from_create_stays_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 stays 503: if the SDK call itself surfaces one of our own taxonomy
    errors, the boundary re-raises it UNCHANGED (never a generic 502)."""
    _configured(monkeypatch, exc=LLMNotConfiguredError("still unconfigured"))
    with pytest.raises(LLMNotConfiguredError):
        _run()


def test_generate_structured_upstream_error_from_create_passes_through_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-shaped LLMUpstreamError raised at the SDK call is re-raised as
    the SAME object (502 stays 502), never double-wrapped and never retried."""
    original = LLMUpstreamError("EmptyResponse: the LLM returned no choices")
    stub = _configured(monkeypatch, exc=original)
    with pytest.raises(LLMUpstreamError) as excinfo:
        _run()
    assert excinfo.value is original
    assert len(_calls(stub)) == 1


def test_upstream_error_message_never_leaks_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even when the SDK error text embeds URL/key-like values, the safe
    LLMUpstreamError message carries only the exception category, never the
    configured base URL or API key.
    """
    leaky = openai.OpenAIError(
        f"connect to {_CONFIGURED_BASE_URL} failed using key {_CONFIGURED_KEY}"
    )
    _configured(monkeypatch, exc=leaky)
    with pytest.raises(LLMUpstreamError) as excinfo:
        _run()

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


# --- generate_structured: wall-clock deadline (spans the retry) -----------


def test_generate_structured_wall_clock_timeout_raises_upstream_with_timeout_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asyncio.timeout wraps the WHOLE attempt loop with a genuine wall-clock
    deadline. A stub that never returns within the configured
    openai_timeout_seconds must raise LLMUpstreamError -- with the distinct
    "Timeout" category -- well before the stub's own (much longer) delay elapses.
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
        _run()
    elapsed = time.monotonic() - started

    message = str(excinfo.value)
    assert message.startswith("Timeout: ")
    assert _UPSTREAM_REASON in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    # Bounded by the configured deadline, not the stub's 1s delay.
    assert elapsed < 0.5
    # The request was still genuinely attempted before the deadline cut it off.
    assert _calls(stub)[0]["model"] == _CONFIGURED_MODEL


def test_generate_structured_deadline_spans_both_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wall-clock budget is ONE deadline around the whole loop, not reset per
    attempt. Two attempts that each individually fit the budget but together
    exceed it must Timeout during the second -- proving the retry does not get a
    fresh budget. Each attempt returns bad (so, absent the deadline, the run
    would end in InvalidStructuredOutput, not Timeout).
    """
    settings = Settings(
        openai_base_url=_CONFIGURED_BASE_URL,
        openai_api_key=_CONFIGURED_KEY,
        openai_model=_CONFIGURED_MODEL,
        openai_timeout_seconds=0.30,
    )
    # Each create sleeps 0.20: attempt 1 (~0.20) fits under 0.30 and returns bad,
    # attempt 2 pushes the cumulative time past 0.30 and is cut off mid-call.
    stub = _install(
        monkeypatch, settings, _StubClient(contents=["not json", "not json"], delay=0.20)
    )
    with pytest.raises(LLMUpstreamError) as excinfo:
        _run()
    assert str(excinfo.value).startswith("Timeout: ")
    # Both attempts were entered -- the failure is the budget spanning them, not a
    # single slow call.
    assert len(_calls(stub)) == 2


# --- llm_configured: parseable-but-unusable URLs --------------------------


@pytest.mark.parametrize(
    "base_url",
    ["localhost:8000/v1", "http:///v1", "ftp://h/v1"],
    ids=["scheme-less-authority", "empty-host", "non-http-scheme"],
)
def test_llm_configured_false_for_parseable_but_unusable_url(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """A URL that parses under httpx.URL but carries no http/https scheme or no
    host is not a reachable OpenAI-compatible endpoint: llm_configured reports
    unconfigured, and generate_structured gates to LLMNotConfiguredError (503)
    before any client is built or request sent.
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
        _run()


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


@pytest.mark.parametrize(
    "base_url",
    ["http://host.example:99999/v1", "http://host.example:0/v1"],
    ids=["port-above-65535", "port-zero"],
)
def test_llm_configured_false_for_out_of_range_port(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """httpx.URL parses a numerically out-of-range port (99999) or port 0 WITHOUT
    error and AsyncOpenAI builds a client from it happily, but no TCP connect can
    ever target such a port. llm_configured reports unconfigured, and
    generate_structured gates to LLMNotConfiguredError (503) before any request.
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
        _run()


def test_llm_configured_true_for_explicit_valid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal, in-range explicit port must remain configured -- the port check
    rejects only 0 and values above 65535, never an ordinary port."""
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
    status and runtime never disagree over stray whitespace.
    """
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url="  http://spaced.example/v1  ", model=_CONFIGURED_MODEL),
    )
    assert llm_configured() is True
    rendered = str(_get_client().base_url)
    assert "spaced.example" in rendered
    assert " " not in rendered


# --- generate_structured: nonconforming-but-200 upstream bodies -----------


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
def test_generate_structured_nonconforming_200_raises_upstream(
    monkeypatch: pytest.MonkeyPatch, completion: Any
) -> None:
    """A compatible gateway can return a 200 whose body violates the SDK's
    expected shape. The service maps every such shape to LLMUpstreamError (502),
    never an AttributeError/TypeError 500.
    """
    _install_raw(monkeypatch, completion)
    with pytest.raises(LLMUpstreamError):
        _run()


# --- generate_structured: malformed endpoint config -----------------------


def test_generate_structured_malformed_endpoint_raises_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A syntactically invalid endpoint (bad port) makes AsyncOpenAI raise at
    CONSTRUCTION -- an httpx.InvalidURL, not an OpenAIError -- which the config
    gate catches and remaps to LLMNotConfiguredError (503). The real
    _get_client/_build_client run here (not stubbed); the raised error carries
    neither the URL fragment nor the key.
    """
    monkeypatch.setattr(
        "app.services.llm.get_settings",
        lambda: _settings(base_url="http://h:8o80/v1", model=_CONFIGURED_MODEL),
    )
    with pytest.raises(LLMNotConfiguredError) as excinfo:
        _run()

    message = str(excinfo.value)
    assert "8o80" not in message
    assert _CONFIGURED_KEY not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    assert "8o80" not in rendered
    assert _CONFIGURED_KEY not in rendered


# --- retry policy ---------------------------------------------------------


def test_build_client_disables_automatic_retries() -> None:
    """max_retries=0 so an SDK transport retry never silently waits out the whole
    per-attempt timeout again (plus backoff). This is distinct from
    generate_structured's ONE corrective retry, which re-prompts only on bad
    output. Asserted on the constructed client's own attribute, not via timing.
    """
    client = _build_client("http://retry-policy.example/v1", "k", 1.0)
    assert client.max_retries == 0
