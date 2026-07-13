"""LLM client service: lazy client factory, JSON extraction, and error taxonomy.

This module is the single boundary between the app and the configured
OpenAI-compatible endpoint. Everything above it (routers, workflows) speaks in
terms of ``generate_json`` and the two exceptions defined here, never in terms
of the OpenAI SDK or the endpoint's URL/key.

Two hard rules shape this file:

* NO import-time side effects. Constructing ``AsyncOpenAI`` is deferred to the
  first call (see ``_build_client`` / ``_get_client``); importing this module
  never touches the network or reads a client into being.
* NO secret leakage. ``openai_base_url`` and ``openai_api_key`` must never
  appear in an exception message or a log line. ``LLMUpstreamError`` messages
  are built from the exception's *category* (its class name) plus a fixed
  short reason -- never ``str(exc)`` of an SDK error (which can carry the URL
  or a full response body) and never a config value.
"""

import json
from functools import lru_cache
from typing import Any

from openai import AsyncOpenAI, OpenAIError

from app.config import get_settings

# Placeholder passed as the API key when the operator has not configured one.
# Some OpenAI-compatible servers (e.g. a local gateway) require no key, but the
# SDK's client still refuses to construct without *some* key, reading os.environ
# and raising OpenAIError if it finds nothing. A fixed non-secret placeholder
# lets the client build; it is never a real credential and never logged.
_UNSET_API_KEY_PLACEHOLDER = "not-required"


class LLMNotConfiguredError(RuntimeError):
    """Raised when an AI workflow runs but no LLM endpoint is configured.

    Maps to HTTP 503 at the router. Carries only a fixed, config-free message.
    """


class LLMUpstreamError(RuntimeError):
    """Raised when the upstream LLM call fails or returns unusable output.

    Maps to HTTP 502 at the router. Covers every failure past a valid config:
    an SDK/API error, a timeout, an empty completion, or output that cannot be
    parsed into a JSON object. Messages are deliberately safe -- an exception
    *category* plus a short reason -- and never contain the configured base
    URL / API key or a full upstream response body.
    """


def llm_configured() -> bool:
    """True only when both an endpoint URL and a model name are configured.

    The API key is intentionally NOT part of this check: a keyless
    OpenAI-compatible gateway is legitimate (see ``_UNSET_API_KEY_PLACEHOLDER``),
    so requiring a key would wrongly report a working endpoint as unconfigured.
    Both fields are stripped so a whitespace-only override still reads as unset.
    """
    settings = get_settings()
    return bool(settings.openai_base_url.strip() and settings.openai_model.strip())


@lru_cache(maxsize=8)
def _build_client(base_url: str, api_key: str, timeout: float) -> AsyncOpenAI:
    """Construct (and cache) an AsyncOpenAI client for the given config triple.

    Cached rather than rebuilt per call so the SDK's connection pool is reused,
    but keyed on the config values themselves so that changing configuration
    (in production via a restart, in tests via a settings override) yields a
    fresh client instead of a stale one bound to the old endpoint.

    ``max_retries=0`` disables the SDK's automatic retries so that
    ``openai_timeout_seconds`` is the true end-to-end latency bound. With a
    retry budget, a genuine failure waits out the full timeout on every attempt
    (plus exponential backoff between them), so a "1s timeout" quietly becomes
    several seconds before the caller sees a 502 -- unacceptable for an
    interactive tool where the user would rather retry from the UI. One request,
    one timeout, no hidden multiplier.
    """
    return AsyncOpenAI(
        base_url=base_url,
        api_key=api_key or _UNSET_API_KEY_PLACEHOLDER,
        timeout=timeout,
        max_retries=0,
    )


def _get_client() -> AsyncOpenAI:
    """Return the cached client for the current settings, building it lazily.

    Construction is wrapped because a syntactically malformed endpoint -- an
    invalid port in ``openai_base_url`` such as ``http://host:8o80/v1`` -- makes
    ``AsyncOpenAI`` raise at CONSTRUCTION time (an ``httpx.InvalidURL``, which is
    NOT an ``OpenAIError`` and so slips past ``generate_json``'s upstream
    handler), which would otherwise surface as an unhandled 500 whose traceback
    echoes the offending URL fragment (``Invalid port: '8o80'``). That is
    operator misconfiguration, not an upstream failure, so it is mapped to
    ``LLMNotConfiguredError`` -> 503 here.

    ``except Exception`` because the SDK does not promise which exception type a
    malformed config raises. ``from None`` deliberately severs the original
    exception: its ``str`` carries a fragment of the configured URL, and
    chaining it would let that fragment ride along in any traceback this error
    later reached. The message is a fixed, config-free literal -- it names
    neither the URL nor ``str(exc)`` -- so nothing config-derived survives past
    this boundary, independent of whether the caller happens to log it.
    """
    settings = get_settings()
    try:
        return _build_client(
            settings.openai_base_url,
            settings.openai_api_key,
            settings.openai_timeout_seconds,
        )
    except Exception:
        raise LLMNotConfiguredError("the configured LLM endpoint is invalid") from None


def _strip_code_fences(text: str) -> str:
    """Remove a leading/trailing Markdown code fence, if present.

    Models frequently wrap JSON in a ```json ... ``` block. Strip only the
    outer fence lines; the balanced-brace pass below handles anything else
    (prose around the object, trailing commentary).
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    # Drop the opening fence line (``` or ```json).
    lines = lines[1:]
    # Drop the closing fence line if present.
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _balanced_brace_slice(text: str) -> str | None:
    """Return the first balanced ``{...}`` substring, or None if there is none.

    Scans from the first ``{`` tracking brace depth, ignoring braces that sit
    inside JSON string literals (respecting backslash escapes) so a ``}`` in a
    value does not close the object early. This is what recovers a JSON object
    embedded in surrounding prose ("Here is the draft: {..}. Hope this helps!").
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object out of raw completion text, robustly.

    Tries the fence-stripped text directly first (the common case), then falls
    back to the first balanced-brace slice (prose-wrapped output). Anything
    that is not, in the end, a JSON *object* raises LLMUpstreamError -- the
    workflows require an object to map onto their pydantic models, and a bare
    array/scalar is as unusable as unparseable garbage.

    Pathologically nested input (tens of thousands of ``[``) makes ``json.loads``
    exhaust the interpreter's recursion limit and raise ``RecursionError`` rather
    than ``JSONDecodeError``; that is caught alongside the ordinary parse errors
    and treated as unparseable (502), never left to escape as an unhandled 500.
    """
    cleaned = _strip_code_fences(text)
    candidates = [cleaned]
    brace = _balanced_brace_slice(cleaned)
    if brace is not None and brace != cleaned:
        candidates.append(brace)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError, ValueError, RecursionError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise LLMUpstreamError("UnparseableOutput: could not parse a JSON object from the LLM output")


async def generate_json(system: str, user: str) -> dict[str, Any]:
    """Call the configured LLM with a system+user prompt and return parsed JSON.

    Raises:
        LLMNotConfiguredError: if no endpoint/model is configured (checked
            first, before any client construction or network call).
        LLMUpstreamError: on any SDK/API error, timeout, empty completion, or
            output that does not parse to a JSON object. The message is a safe
            category + short reason; it never contains the base URL, API key,
            or a full response body.
    """
    if not llm_configured():
        raise LLMNotConfiguredError("The LLM endpoint is not configured.")

    settings = get_settings()
    client = _get_client()
    try:
        completion = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
    except OpenAIError as exc:
        # Category only (the SDK error's class name) plus a fixed reason. Never
        # str(exc): APIConnectionError chains the target URL, APIStatusError
        # carries the response body -- either would leak past this boundary.
        raise LLMUpstreamError(f"{type(exc).__name__}: the upstream LLM request failed") from exc

    # A conformant response is choices=[choice, ...] with choice.message.content
    # a string. A merely OpenAI-*compatible* gateway can return a 200 whose body
    # violates that shape without the SDK rejecting it: choices missing / None /
    # empty / not a list, a choice with no message, or a null message/content.
    # The SDK models these leniently, so a naive choices[0].message.content would
    # raise AttributeError/TypeError here -- an unhandled 500 -- on such
    # nonconforming-but-200 output. Validate each hop defensively instead and map
    # every unusable shape onto the same 502 taxonomy as any other bad output;
    # the messages carry only a category, never the (attacker/gateway-controlled)
    # body.
    choices = getattr(completion, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise LLMUpstreamError("EmptyResponse: the LLM returned no choices")
    message = getattr(choices[0], "message", None)
    if message is None:
        raise LLMUpstreamError("MalformedResponse: the LLM choice had no message")
    content = getattr(message, "content", None)
    if content is None or not isinstance(content, str) or not content.strip():
        raise LLMUpstreamError("EmptyResponse: the LLM returned empty content")
    return _extract_json_object(content)
