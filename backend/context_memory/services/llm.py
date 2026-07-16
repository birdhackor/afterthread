"""LLM client service: lazy client factory, a schema-guided structured-output
contract, and the error taxonomy.

This module is the single boundary between the app and the configured
OpenAI-compatible endpoint. Everything above it (routers, workflows) speaks in
terms of ``generate_structured`` and the two exceptions defined here, never in
terms of the OpenAI SDK or the endpoint's URL/key.

``generate_structured`` replaces the old ``generate_json`` + external-validate
split with ONE strict contract: the caller's pydantic model is turned into a
JSON Schema, injected into the system prompt, and the model is told to emit
EXACTLY one conforming JSON object. Output that fails to parse as an object, or
fails the model's own validation, earns ONE corrective retry (the model is
shown its own bad reply and asked again); a second failure is a 502. There is
no forgiving prose/array scavenger any more -- the contract is stated up front
and re-stated on deviation.

Two hard rules shape this file:

* NO import-time side effects. Constructing ``AsyncOpenAI`` is deferred to the
  first call (see ``_build_client`` / ``_get_client``); importing this module
  never touches the network or reads a client into being.
* NO secret leakage. ``openai_base_url`` and ``openai_api_key`` must never
  appear in an exception message or a log line. ``LLMUpstreamError`` messages
  are built from the exception's *category* (its class name) plus a fixed
  short reason -- never ``str(exc)`` of an SDK error (which can carry the URL
  or a full response body) and never a config value. The final
  ``InvalidStructuredOutput`` message is likewise a fixed literal: the
  json/pydantic details that drove the rejection are fed back to the LLM in the
  retry request, never into our API error.
"""

import asyncio
import json
from functools import lru_cache
from typing import Any

import httpx
from openai import AsyncOpenAI, OpenAIError
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from context_memory.config import Settings, get_settings
from context_memory.services import llm_log

# Placeholder passed as the API key when the operator has not configured one.
# Some OpenAI-compatible servers (e.g. a local gateway) require no key, but the
# SDK's client still refuses to construct without *some* key, reading os.environ
# and raising OpenAIError if it finds nothing. A fixed non-secret placeholder
# lets the client build; it is never a real credential and never logged.
_UNSET_API_KEY_PLACEHOLDER = "not-required"

# At most this many attempts per structured call: the first try plus ONE
# corrective retry. Deliberately small -- an interactive tool would rather
# surface a 502 the user can retry from the UI than silently burn several
# upstream round-trips (and their latency) behind a single request.
_MAX_ATTEMPTS = 2


class LLMNotConfiguredError(RuntimeError):
    """Raised when an AI workflow runs but no LLM endpoint is configured.

    Maps to HTTP 503 at the router. Carries only a fixed, config-free message.
    """


class LLMUpstreamError(RuntimeError):
    """Raised when the upstream LLM call fails or returns unusable output.

    Maps to HTTP 502 at the router. Covers every failure past a valid config:
    an SDK/API error, a timeout, an empty completion, or output that -- even
    after one corrective retry -- cannot be parsed and validated into the
    requested model. Messages are deliberately safe -- an exception *category*
    plus a short reason, or a fixed literal -- and never contain the configured
    base URL / API key, a full upstream response body, or the json/pydantic
    details of a rejected structured output.
    """


# The fixed, config-free reason embedded in every SDK-error-driven
# LLMUpstreamError message ("<SDKClassName>: <reason>"). Shared with
# context_memory.routers.ai, whose OpenAPI 502 example is built from it, so the example's
# reason substring can never drift from what runtime messages actually carry
# (the category prefix varies with the failing SDK error's class and is only
# illustrative in the example).
_UPSTREAM_REASON = "the upstream LLM request failed"

# The safe, config-free message for the terminal structured-output failure:
# the LLM's reply could not be parsed and validated into the requested model,
# even after one corrective retry. Deliberately carries NO json/pydantic detail
# (those were fed back to the LLM in the retry request, not leaked here) so a
# rejected output body can never ride along into our API error.
_INVALID_STRUCTURED_OUTPUT = (
    "InvalidStructuredOutput: the LLM did not return a valid structured result"
)

# Appended (after the shared strict-output rule) to every workflow's system
# prompt, followed by the model's own JSON Schema. Naming the exact schema and
# demanding a single bare object is what replaces the old forgiving scavenger:
# the contract is stated, and any deviation is corrected via retry rather than
# scraped out of prose.
_STRICT_OUTPUT_RULE = (
    "Respond with EXACTLY one JSON object and nothing else -- no prose, no code "
    "fences. It must conform to this JSON Schema:"
)


def normalized_model(settings: Settings) -> str:
    """Return the configured model name with surrounding whitespace stripped.

    The one normalization every caller must share, so none of them can ever
    disagree over a whitespace-padded override: ``llm_configured``'s gate, the
    actual request ``generate_structured`` sends, and (via
    ``context_memory.routers.ai.llm_status``) what ``/llm/status`` reports back to a
    client all read the model through this single helper.
    """
    return settings.openai_model.strip()


def llm_configured() -> bool:
    """True only when a usable endpoint URL and a model name are configured.

    The API key is intentionally NOT part of this check: a keyless
    OpenAI-compatible gateway is legitimate (see ``_UNSET_API_KEY_PLACEHOLDER``),
    so requiring a key would wrongly report a working endpoint as unconfigured.
    Both fields are stripped (the model via ``normalized_model``) so a
    whitespace-only override still reads as unset.

    "Usable" also means syntactically constructible: a base URL that
    ``AsyncOpenAI`` would reject at construction (e.g. an invalid port like
    ``http://host:8o80/v1``, which the SDK parses with ``httpx.URL``) is not a
    working endpoint, so this reparses it the same way and reports unconfigured
    on failure. It further requires the parsed URL to carry an ``http``/``https``
    scheme AND a non-empty host, rejecting a scheme-less authority
    (``localhost:8000/v1``), an empty host (``http:///v1``), or a non-HTTP scheme
    (``ftp://h/v1``) -- each parses but no OpenAI-compatible request could reach
    it. Without that, ``llm_configured`` would answer True while every workflow
    using the same config degrades to 503 -- the status endpoint would disagree
    with actual usability. It also rejects a numerically out-of-range port
    (``http://host:99999/v1``) or port ``0`` (``http://host:0/v1``): unlike the
    non-numeric ``8o80`` above, ``httpx.URL`` parses either of these without
    error and ``AsyncOpenAI`` builds a client from the result happily, but no
    TCP connect can ever target a port outside 1..65535 -- so every real call
    against it would fail as a 502 instead of the config-error 503 this
    function exists to produce. A URL with no port at all (``url.port is
    None``, meaning "use the scheme's default port") is unaffected by this
    check. The URL is only parsed, never echoed, so no fragment of it leaks
    out of this function. The stripped value is the same one ``_get_client``
    builds the client from, so status and runtime agree even for a
    whitespace-padded override.
    """
    settings = get_settings()
    base_url = settings.openai_base_url.strip()
    model = normalized_model(settings)
    if not (base_url and model):
        return False
    try:
        url = httpx.URL(base_url)
    except Exception:
        return False
    # Parseable is not usable: httpx.URL accepts a scheme-less authority
    # ("localhost:8000/v1" parses with scheme "localhost"), an empty host
    # ("http:///v1"), a non-HTTP scheme ("ftp://h/v1"), AND a numerically
    # out-of-range or zero port ("http://host:99999/v1", "http://host:0/v1") --
    # each parses without error, so each needs an explicit check here rather
    # than relying on the try/except above. Require an http/https scheme, a
    # non-empty host, and (when a port is present at all) a port in 1..65535,
    # so this can never claim an endpoint the workflows would then fail to
    # reach.
    if url.scheme not in ("http", "https") or not url.host:
        return False
    return url.port is None or 1 <= url.port <= 65535


@lru_cache(maxsize=8)
def _build_client(base_url: str, api_key: str, timeout: float) -> AsyncOpenAI:
    """Construct (and cache) an AsyncOpenAI client for the given config triple.

    Cached rather than rebuilt per call so the SDK's connection pool is reused,
    but keyed on the config values themselves so that changing configuration
    (in production via a restart, in tests via a settings override) yields a
    fresh client instead of a stale one bound to the old endpoint.

    ``max_retries=0`` disables the SDK's automatic transport retries: a retry
    would silently wait out the whole per-attempt timeout again (plus
    exponential backoff between attempts), so a "1s timeout" quietly becomes
    several seconds before the caller sees a 502 -- unacceptable for an
    interactive tool where the user would rather retry from the UI. One request,
    one timeout, no hidden multiplier. This is deliberately distinct from
    ``generate_structured``'s ONE corrective retry, which re-prompts only on a
    bad-SHAPE output, never on a transport error.

    That alone does NOT make ``openai_timeout_seconds`` an end-to-end
    wall-clock bound, though: the ``timeout`` passed here only configures the
    SDK/httpx client's PER-PHASE (connect/read/write) inactivity timeout, not a
    cap on the total request duration. A slow-drip endpoint that sends a byte
    just before every read timeout could otherwise hold the request -- and its
    connection -- open indefinitely, even with retries disabled. The genuine
    end-to-end deadline is the ``asyncio.timeout`` wrapped around the whole
    attempt loop in ``generate_structured``; this client-level timeout stays in
    place alongside it as an inner belt that still fast-fails a dead
    connect/read leg without waiting for the outer deadline.
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
    NOT an ``OpenAIError`` and so slips past ``generate_structured``'s upstream
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
            # Strip to the SAME normalized base URL `llm_configured` validated,
            # so a whitespace-padded override that reads as configured is the
            # exact value the client is built from (status and runtime agree).
            settings.openai_base_url.strip(),
            settings.openai_api_key,
            settings.openai_timeout_seconds,
        )
    except Exception:
        raise LLMNotConfiguredError("the configured LLM endpoint is invalid") from None


def _strip_code_fences(text: str) -> str:
    """Remove a leading/trailing Markdown code fence, if present.

    Models frequently wrap JSON in a ```json ... ``` block. Strip only the
    outer fence lines; the strict parser below then json.loads the remainder.
    This is the ONLY forgiveness left in the parse path -- a fenced bare object
    is a common, unambiguous shape, so it is accepted rather than burning a
    corrective retry on it.
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


def _parse_json_object(text: str) -> dict[str, Any]:
    """Parse ``text`` as EXACTLY one JSON object (a dict), strictly.

    Strips an optional code fence (see ``_strip_code_fences``), strips
    surrounding whitespace, then ``json.loads``. Anything that is not a single
    top-level JSON object -- a bare array, a scalar, prose wrapping an object,
    two juxtaposed objects (``json.loads`` rejects the trailing data) -- fails
    here and is treated by ``generate_structured`` as an output-shape failure
    eligible for one corrective retry.

    Raises:
        json.JSONDecodeError: the text is not well-formed JSON.
        ValueError: well-formed JSON that is not an object, OR a value
            ``json.loads`` itself rejects without being a decode error -- most
            notably a numeric literal with more digits than
            ``sys.int_max_str_digits`` (a 5000-digit integer raises a bare
            ``ValueError``, not a ``JSONDecodeError``). Both must be caught.
        RecursionError: pathologically nested brackets exhaust the interpreter's
            recursion limit inside ``json.loads`` -- never a legitimate answer,
            so it is caught alongside the parse errors rather than escaping as an
            unhandled 500.

    The forgiving prose/array scavenger this replaces (a real-decoder walk with
    exactly-one and array-element-context rules) is deliberately gone: rather
    than guess which embedded object the caller "meant", the model is told the
    exact schema up front and asked again on any deviation.
    """
    cleaned = _strip_code_fences(text).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("the LLM output was not a single JSON object")
    return parsed


def _extract_content(completion: Any) -> str:
    """Defensively pull ``choices[0].message.content`` (a non-empty str) from a
    completion, mapping every nonconforming-but-200 shape to a 502.

    A conformant response is choices=[choice, ...] with choice.message.content a
    string. A merely OpenAI-*compatible* gateway can return a 200 whose body
    violates that shape (choices missing / None / empty / not a list, a choice
    with no message, a null message/content) without the SDK rejecting it. A
    naive ``choices[0].message.content`` would then raise AttributeError/
    TypeError -- an unhandled 500. Validate each hop instead and map every
    unusable shape onto the 502 taxonomy; the messages carry only a category,
    never the (attacker/gateway-controlled) body.

    An empty/malformed completion is NOT retried -- that is a transport-shaped
    failure, and ``generate_structured`` retries only output-SHAPE failures --
    so raising ``LLMUpstreamError`` here surfaces straight through the loop.
    """
    choices = getattr(completion, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise LLMUpstreamError("EmptyResponse: the LLM returned no choices")
    message = getattr(choices[0], "message", None)
    if message is None:
        raise LLMUpstreamError("MalformedResponse: the LLM choice had no message")
    content = getattr(message, "content", None)
    if content is None or not isinstance(content, str) or not content.strip():
        raise LLMUpstreamError("EmptyResponse: the LLM returned empty content")
    return content


def _schema_guided_system_prompt(system_prompt: str, model_cls: type[BaseModel]) -> str:
    """Append the shared strict-output rule and ``model_cls``'s JSON Schema.

    The schema is DERIVED from the model (``model_json_schema``), so the contract
    the LLM is shown can never drift from the model the output is validated
    against -- there is no second, hand-maintained copy of the field list to keep
    in sync. ``ensure_ascii=False`` keeps any non-ASCII field metadata readable
    rather than escaped.
    """
    schema = json.dumps(model_cls.model_json_schema(), ensure_ascii=False)
    return f"{system_prompt}\n\n{_STRICT_OUTPUT_RULE}\n{schema}"


def _corrective_user_message(exc: Exception) -> str:
    """Build the attempt-2 corrective user turn from the attempt-1 failure.

    Carries a COMPACT summary of WHY the previous reply was rejected -- the json
    error string, or the first few pydantic errors -- so the model can fix the
    specific defect. Echoing the model's own error back to it is fine. This text
    only ever rides in the RETRY REQUEST sent to the LLM; it is never folded into
    an ``LLMUpstreamError``, so no json/pydantic fragment leaks into our API
    error (the terminal 502 is the fixed ``_INVALID_STRUCTURED_OUTPUT`` literal).
    """
    if isinstance(exc, ValidationError):
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors()[:3]
        )
        summary = (
            f"it failed schema validation ({problems})"
            if problems
            else "it failed schema validation"
        )
    else:
        summary = f"it was not a single valid JSON object ({exc})"
    return (
        "Your previous reply was rejected: "
        f"{summary}. "
        "Reply with ONLY the corrected JSON object -- no prose, no code fences -- "
        "conforming exactly to the JSON Schema in the system instructions."
    )


async def _create_completion(
    client: AsyncOpenAI,
    settings: Settings,
    messages: list[ChatCompletionMessageParam],
) -> Any:
    """Send ONE chat-completion request, threading ``max_tokens`` only when set.

    Two explicit call shapes rather than a spread ``**kwargs`` dict: the type
    checker can verify each keyword against the SDK's typed ``create`` (a spread
    dict fails ``ty``'s overload match), and -- crucially -- the unset branch
    OMITS ``max_tokens`` entirely, so the outgoing kwargs stay byte-identical to
    the pre-existing call (the pinned llm-service tests assert the key is
    absent). Sending the SDK's ``omit`` sentinel instead would still surface a
    ``max_tokens`` key in a stubbed call's recorded kwargs.

    ``openai_max_output_tokens`` defaults to None because some reasoning-style
    endpoints reject an explicit ``max_tokens`` (a 400 -> 502); an operator sets
    it only to raise a gateway's small default completion cap. No fixed
    temperature is ever sent, for the same reason (some endpoints 400 on it). The
    model is the shared ``normalized_model`` value ``llm_configured`` and
    ``/llm/status`` also use, so runtime and status can never disagree.
    """
    model = normalized_model(settings)
    if settings.openai_max_output_tokens is None:
        return await client.chat.completions.create(model=model, messages=messages)
    return await client.chat.completions.create(
        model=model, messages=messages, max_tokens=settings.openai_max_output_tokens
    )


def _classify_upstream_outcome(exc: LLMUpstreamError) -> str:
    """Map an ``LLMUpstreamError`` to the llm_log outcome vocabulary.

    Reads only the SAFE message the service itself built (never a config value):
    the fixed ``InvalidStructuredOutput`` literal is a bad-output failure, the
    ``Timeout:`` category is the wall-clock deadline, and everything else (an SDK
    category, an empty/malformed body) is a generic upstream failure.
    """
    message = str(exc)
    if message == _INVALID_STRUCTURED_OUTPUT:
        return "invalid_output"
    if message.startswith("Timeout: "):
        return "timeout"
    return "upstream_error"


async def _run_structured[ModelT: BaseModel](
    recorder: llm_log.LlmInteractionRecorder,
    settings: Settings,
    system_prompt: str,
    user_prompt: str,
    model_cls: type[ModelT],
) -> ModelT:
    """The config gate + attempt loop, feeding ``recorder`` as it goes.

    Split out from ``generate_structured`` so ALL recorder finalization happens
    at the outer boundary, OUTSIDE this function's ``asyncio.timeout`` -- the
    sinks' I/O then never runs under the wall-clock deadline, and every exit (a
    return here, or any raise) maps to exactly one ``recorder.finish`` in the
    wrapper. The control flow and the exact create() kwargs / message list are
    otherwise unchanged from the pre-logging version.
    """
    if not llm_configured():
        # `from None` severs any context: this gate also fields a syntactically
        # invalid base_url (llm_configured reparses it and reports unconfigured),
        # and suppressing context keeps that path's traceback as free of a URL
        # fragment as the _get_client construction path already is.
        raise LLMNotConfiguredError("The LLM endpoint is not configured.") from None

    client = _get_client()

    # The conversation accumulates across attempts: attempt 2 appends the
    # assistant's rejected reply plus a corrective user turn (built below).
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": _schema_guided_system_prompt(system_prompt, model_cls)},
        {"role": "user", "content": user_prompt},
    ]

    try:
        # ONE asyncio.timeout spans the WHOLE attempt loop, so the R7 wall-clock
        # contract now bounds first-try-plus-retry end to end, not each attempt
        # separately -- a slow retry cannot quietly double the budget. Unlike the
        # client-level timeout (see _build_client), which only bounds per-phase
        # inactivity, this is a genuine deadline on total duration.
        async with asyncio.timeout(settings.openai_timeout_seconds):
            for attempt in range(_MAX_ATTEMPTS):
                # Snapshot the messages ACTUALLY sent for this attempt (the
                # recorder copies them, since `messages` is rebuilt for the
                # corrective retry below).
                recorder.begin_attempt(messages)
                try:
                    completion = await _create_completion(client, settings, messages)
                except LLMNotConfiguredError:
                    # Our own taxonomy carries its own HTTP mapping (503 stays
                    # 503, an already-shaped 502 stays 502). Re-raise UNCHANGED so
                    # the broad handlers below can never rewrap one into a generic
                    # upstream 502. This is a TRANSPORT-boundary failure, not an
                    # output-shape one, so it is never retried. NB: kept as two
                    # single-type clauses rather than a multi-type clause: with
                    # target-version 3.14 ruff-format normalizes
                    # `except (A, B):` to PEP 758's unparenthesized
                    # `except A, B:` (valid on this repo's >=3.14 floor, but a
                    # trap for tooling/readers assuming older syntax rules);
                    # two plain clauses read the same everywhere and are stable
                    # under the formatter.
                    raise
                except LLMUpstreamError:
                    raise
                except TimeoutError:
                    # A genuine builtin TimeoutError raised at the SDK boundary
                    # (not asyncio.timeout's own expiry, which arrives as a
                    # CancelledError -- a BaseException the clauses here do not
                    # catch -- and is converted to TimeoutError by the async-with
                    # exit). Re-raise so the outer handler maps it to the "Timeout"
                    # category rather than letting `except Exception` mislabel it.
                    raise
                except OpenAIError as exc:
                    # Category only (the SDK error's class name) plus the fixed
                    # shared reason. Never str(exc): APIConnectionError chains the
                    # target URL, APIStatusError carries the response body. `from
                    # None` severs the cause chain so neither can ride along in
                    # __cause__ into a traceback-logging sink. Transport failure ->
                    # no retry. The same safe category is recorded on the attempt.
                    category = f"{type(exc).__name__}: {_UPSTREAM_REASON}"
                    recorder.fail_current_attempt(category)
                    raise LLMUpstreamError(category) from None
                except Exception as exc:
                    # Total boundary. A merely OpenAI-*compatible* endpoint can
                    # return a 2xx whose body is broken/empty JSON; the SDK parses
                    # that body INTERNALLY and raises json.JSONDecodeError /
                    # ValueError -- NEITHER an OpenAIError -- so without this arm
                    # such output escapes as an unhandled 500. Carry only the
                    # category (never str(exc), which could embed a response body)
                    # and sever the chain. Transport-shaped failure -> no retry.
                    category = f"{type(exc).__name__}: {_UPSTREAM_REASON}"
                    recorder.fail_current_attempt(category)
                    raise LLMUpstreamError(category) from None

                # Capture usage from whatever completion carried it (last wins).
                recorder.record_usage(completion)
                try:
                    content = _extract_content(completion)
                except LLMUpstreamError as exc:
                    # An empty/malformed-but-200 body: transport-shaped, not
                    # retried. Record the safe category on the attempt (its
                    # response_content stays None) and re-raise the SAME object.
                    recorder.fail_current_attempt(str(exc))
                    raise
                recorder.record_response(content)

                try:
                    parsed = _parse_json_object(content)
                    # Every sanitizer/validator on model_cls still runs here --
                    # defense in depth against untrusted output.
                    return model_cls.model_validate(parsed)
                except (json.JSONDecodeError, ValueError, RecursionError, ValidationError) as exc:
                    # OUTPUT-SHAPE failure -- the ONLY thing eligible for a
                    # corrective retry. ValueError covers both a non-object result
                    # and json.loads' huge-integer rejection; RecursionError covers
                    # pathological nesting; ValidationError covers the model's own
                    # sanitizers (empty title, all-empty result, lone surrogate,
                    # ...). Record only the error's CLASS on the attempt (the
                    # rejected body is already recorded above it; the full
                    # pydantic detail goes to the LLM in the corrective turn,
                    # never here). On the last attempt, fail with a FIXED,
                    # config-free message.
                    recorder.fail_current_attempt(type(exc).__name__)
                    if attempt + 1 >= _MAX_ATTEMPTS:
                        raise LLMUpstreamError(_INVALID_STRUCTURED_OUTPUT) from None
                    messages = [
                        *messages,
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": _corrective_user_message(exc)},
                    ]
    except TimeoutError:
        # asyncio.timeout's deadline expiry (converted from the CancelledError it
        # injects) OR a re-raised builtin TimeoutError from the SDK boundary --
        # both land here with the distinct "Timeout" category. `from None` severs
        # the cause chain, same as every other arm.
        raise LLMUpstreamError(f"Timeout: {_UPSTREAM_REASON}") from None

    # Unreachable at runtime -- the loop always returns a validated instance or
    # raises -- but present so every path provably returns/raises (satisfying the
    # type checker) and a future change to the loop bound cannot fall through to
    # an implicit `None`.
    raise LLMUpstreamError(_INVALID_STRUCTURED_OUTPUT)


async def generate_structured[ModelT: BaseModel](
    system_prompt: str,
    user_prompt: str,
    model_cls: type[ModelT],
    *,
    workflow: str = "unknown",
) -> ModelT:
    """Call the configured LLM under a strict, schema-guided contract and return
    a validated ``model_cls`` instance, with ONE corrective retry on bad output.

    The system prompt is augmented with a fixed single-object rule and
    ``model_cls``'s own JSON Schema (see ``_schema_guided_system_prompt``), so
    the model is told the exact shape to emit. The completion is parsed as
    EXACTLY one JSON object (``_parse_json_object``) and validated through
    ``model_cls`` (whose before-validators sanitize the untrusted output). On an
    output-shape failure -- a parse error or a ``ValidationError`` -- the model
    is shown its own reply plus a corrective note and asked once more; a second
    failure raises the fixed ``InvalidStructuredOutput`` 502.

    ``workflow`` names the caller (``capture`` / ``enrich`` / ``assist_update``,
    or the default ``unknown`` for the direct test callers) and is recorded on
    the interaction log (context_memory.services.llm_log). An
    ``LlmInteractionRecorder`` is built at the very START -- before the config
    gate -- so EVERY exit path (ok, invalid output, upstream error, timeout, not
    configured) finalizes exactly ONE record carrying its workflow: the
    try/except below maps each outcome and calls ``finish`` once. The recorder is
    side-effect-safe (``finish`` swallows its own failures), so logging can never
    mask or replace the real LLM exception, and its ``model`` field is the
    non-secret name ``/llm/status`` exposes -- the base URL and key are never
    read into a record or a log line.

    Raises:
        LLMNotConfiguredError: if no endpoint/model is configured (checked
            first, before any client construction or network call). Maps to 503.
        LLMUpstreamError: on any SDK/API error, empty completion, a timeout, or
            output that fails parse+validation even after the corrective retry.
            Maps to 502. The message is a safe category + short reason, or the
            fixed InvalidStructuredOutput literal; it never contains the base
            URL, API key, a full response body, or json/pydantic detail.
    """
    settings = get_settings()
    recorder = llm_log.LlmInteractionRecorder(workflow=workflow, model=normalized_model(settings))
    try:
        result = await _run_structured(recorder, settings, system_prompt, user_prompt, model_cls)
    except LLMNotConfiguredError as exc:
        recorder.finish(outcome="not_configured", error=str(exc))
        raise
    except LLMUpstreamError as exc:
        recorder.finish(outcome=_classify_upstream_outcome(exc), error=str(exc))
        raise
    else:
        recorder.finish(outcome="ok", error=None)
        return result
