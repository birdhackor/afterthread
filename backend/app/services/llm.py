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

import asyncio
import json
from functools import lru_cache
from typing import Any

import httpx
from openai import AsyncOpenAI, OpenAIError

from app.config import Settings, get_settings

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


# The fixed, config-free reason embedded in every SDK-error-driven
# LLMUpstreamError message ("<SDKClassName>: <reason>"). Shared with
# app.routers.ai, whose OpenAPI 502 example is built from it, so the example's
# reason substring can never drift from what runtime messages actually carry
# (the category prefix varies with the failing SDK error's class and is only
# illustrative in the example).
_UPSTREAM_REASON = "the upstream LLM request failed"


def normalized_model(settings: Settings) -> str:
    """Return the configured model name with surrounding whitespace stripped.

    The one normalization every caller must share, so none of them can ever
    disagree over a whitespace-padded override: ``llm_configured``'s gate, the
    actual request ``generate_json`` sends, and (via
    ``app.routers.ai.llm_status``) what ``/llm/status`` reports back to a
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

    ``max_retries=0`` disables the SDK's automatic retries: a retry would
    silently wait out the whole per-attempt timeout again (plus exponential
    backoff between attempts), so a "1s timeout" quietly becomes several
    seconds before the caller sees a 502 -- unacceptable for an interactive
    tool where the user would rather retry from the UI. One request, one
    timeout, no hidden multiplier.

    That alone does NOT make ``openai_timeout_seconds`` an end-to-end
    wall-clock bound, though: the ``timeout`` passed here only configures the
    SDK/httpx client's PER-PHASE (connect/read/write) inactivity timeout, not a
    cap on the total request duration. A slow-drip endpoint that sends a byte
    just before every read timeout could otherwise hold the request -- and its
    connection -- open indefinitely, even with retries disabled. The genuine
    end-to-end deadline is the ``asyncio.timeout`` wrapped around the call in
    ``generate_json``; this client-level timeout stays in place alongside it as
    an inner belt that still fast-fails a dead connect/read leg without waiting
    for the outer deadline.
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
    outer fence lines; the JSON-decoder fallback below handles anything else
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


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object out of raw completion text, robustly.

    Tries the fence-stripped text directly first (the common case). If that
    full text parses as JSON at all, its shape is FINAL and no fallback is
    attempted: a dict is returned as-is, while anything else -- a top-level
    array, string, number, bool, or null -- raises LLMUpstreamError
    immediately. That fallthrough is deliberately not taken: a top-level array
    such as ``[{"title": "A"}, {"title": "B"}]`` parses cleanly, and a naive
    fallback would happily find and return its first embedded object --
    silently persisting one arbitrary element of a shape the caller never
    asked for, instead of surfacing the true "wrong shape" failure as a 502.

    The fallback below (recovering an object embedded in surrounding prose,
    e.g. "Here is the draft: {..}. Hope this helps!") is only ever attempted
    when the full text FAILS to parse as JSON at all. It does NOT hand-roll a
    bracket scanner -- a depth counter that tracks ``{``/``[`` cannot tell a
    real bracket from one sitting inside a JSON string literal, so a legitimate
    object whose value contains ``"[{}]"`` once grew a phantom array candidate
    and produced a false 502. Instead the walk asks the REAL parser: starting
    at each ``{``/``[``, one reused ``json.JSONDecoder`` tries to ``raw_decode``
    a value there; a ``JSONDecodeError`` means "no JSON begins here" (advance one
    character), while success yields the value and the index just past it (jump
    to ``end``, so nested brackets inside a decoded value are never rescanned).
    Being string/escape-aware by construction, the decoder consumes a bracket
    inside a string as part of that string -- the phantom-candidate bug cannot
    recur.

    Each decoded value is classified in order of appearance:

    * a dict-bearing list -- a list containing at least one dict -- is the same
      "ambiguous multi-draft output" the top-level-array check above guards
      against, just wrapped in prose (e.g. ``Here are two drafts: [{"title":
      "A"}, {"title": "B"}]``). It raises LLMUpstreamError THE INSTANT it is
      found, rather than let one of the array's own elements be mistaken for the
      answer;
    * a dict is USABLE, but its immediate left context is inspected first: if
      the last non-whitespace character before it is ``[`` or ``,`` the object
      is syntactically an ELEMENT of an array that -- since the full-text parse
      failed -- did not parse as a whole (a garbled multi-draft array like
      ``[{"title": "A"}, {bad}]``, or one with a trailing comma). Promoting the
      single element that happens to parse would silently persist an arbitrary
      draft, so this raises WrongShape too. DELIBERATE TRADEOFF: innocent prose
      ending in ``,`` or ``[`` right before a lone object ("as shown, {json}")
      also trips this and 502s -- accepted, because a 502 merely triggers a
      retry while a silent element-pick misleads, and a conforming model emits a
      bare single object anyway;
    * any other dict is collected (not returned on sight) so a second or third
      top-level object juxtaposed with it (``{"title": "A"} {"title": "B"}``,
      which fails the full-text parse as "extra data") is noticed rather than
      silently dropped;
    * anything else -- a scalar, a scalar list such as ``[1]``, or a bracket
      that begins no valid JSON at all -- is skippable junk, letting an innocent
      scalar bracket before OR after the real object ("Answer[1]: {...}") fall
      through to it.

    Once the walk finishes without an earlier raise, the usable-dict count is
    the whole decision: zero raises UnparseableOutput (the workflows require an
    object to map onto their pydantic models), exactly one returns it, and more
    than one raises WrongShape ("multiple JSON objects") -- the juxtaposition
    case this rule closes.

    Pathologically nested input (tens of thousands of ``[``) makes the decoder
    exhaust the interpreter's recursion limit and raise ``RecursionError``
    rather than ``JSONDecodeError``; such nesting is never legitimate, so the
    first time it happens -- on the full-text attempt (caught alongside the
    ordinary parse errors) or on any ``raw_decode`` in the walk -- the whole
    text is rejected as unparseable (502), never left to escape as an unhandled
    500 and never allowed to drive an O(n*depth) re-descent. The walk is
    otherwise linear: ``raw_decode`` fails at the first character of an unmatched
    ``{`` (so a run of them is O(1) per position), jumps past every value it does
    parse (so the parsed spans are mutually disjoint), and the left-context
    lookback only scans the whitespace immediately before each object, itself
    disjoint across objects.
    """
    cleaned = _strip_code_fences(text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError, ValueError, RecursionError:
        pass
    else:
        if isinstance(parsed, dict):
            return parsed
        # Well-formed JSON, but not an object: this is the LLM's whole answer,
        # not prose wrapping an object, so it fails here and now rather than
        # falling through to the candidate walk below (which could otherwise
        # extract and silently persist one embedded object out of a top-level
        # array).
        raise LLMUpstreamError("WrongShape: the LLM returned a non-object JSON value")

    # Fallback: the full text is not valid JSON on its own, so an object may be
    # embedded in surrounding prose. One decoder instance is reused across every
    # attempt below (it holds no per-call state).
    decoder = json.JSONDecoder()
    # Every usable dict the walk finds, in order of appearance. Collected rather
    # than returned on first sight so a second (or third) top-level object
    # juxtaposed with the first is noticed instead of silently dropped; see the
    # "exactly one" rule in the docstring above.
    usable_dicts: list[dict[str, Any]] = []
    index = 0
    length = len(cleaned)
    while index < length:
        if cleaned[index] not in "{[":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(cleaned, index)
        except json.JSONDecodeError:
            # No JSON value begins here (a bare "{" with no valid object after
            # it, a bracket sitting in prose). Advance one character and keep
            # scanning; because the decoder is string-aware, a bracket inside a
            # string literal is never mistaken for a candidate of its own.
            index += 1
            continue
        except RecursionError:
            # Pathologically nested input exhausted the recursion limit. Such
            # nesting is never a legitimate answer, so reject the whole text at
            # once rather than let a RecursionError escape as an unhandled 500
            # or drive an O(n*depth) walk.
            raise LLMUpstreamError(
                "UnparseableOutput: could not parse a JSON object from the LLM output"
            ) from None
        if isinstance(value, dict):
            # Array-element-context rule: an object whose last non-whitespace
            # neighbour on the left is "[" or "," is syntactically an element of
            # an array that -- since the full-text parse failed -- did not parse
            # as a whole. Promoting the one element that happens to parse would
            # silently persist an arbitrary draft, so reject the ambiguity.
            # DELIBERATE TRADEOFF: innocent prose ending in "," or "[" right
            # before a lone object ("as shown, {json}") also trips this and 502s
            # -- accepted, because a 502 merely triggers a retry while a silent
            # element-pick misleads, and a conforming model emits a bare single
            # object anyway.
            before = index - 1
            while before >= 0 and cleaned[before].isspace():
                before -= 1
            if before >= 0 and cleaned[before] in "[,":
                raise LLMUpstreamError(
                    "WrongShape: the LLM returned an ambiguous array of draft objects"
                )
            usable_dicts.append(value)
        elif isinstance(value, list) and any(isinstance(item, dict) for item in value):
            # Same "ambiguous multi-draft output" as the top-level-array check
            # above, just embedded in prose. Raise immediately rather than let
            # one of the array's own elements be mistaken for the answer.
            raise LLMUpstreamError(
                "WrongShape: the LLM returned an ambiguous array of draft objects"
            )
        # Anything else -- a scalar, or a list with no dict in it (e.g. [1]) --
        # is not usable. Jump past whatever was decoded and keep scanning; this
        # is what lets an innocent scalar bracket before or after the real
        # object (e.g. "Answer[1]: {...}") fall through instead of being
        # mistaken for the answer.
        index = end

    if len(usable_dicts) > 1:
        # The juxtaposition case this rule closes: more than one top-level
        # object was found and none of them is privileged over another, so
        # silently picking the first would drop the rest exactly like the bug
        # this replaces. Same safe WrongShape message family as the
        # dict-bearing-list case above.
        raise LLMUpstreamError("WrongShape: the LLM returned multiple JSON objects")
    if usable_dicts:
        return usable_dicts[0]
    raise LLMUpstreamError("UnparseableOutput: could not parse a JSON object from the LLM output")


async def generate_json(system: str, user: str) -> dict[str, Any]:
    """Call the configured LLM with a system+user prompt and return parsed JSON.

    Raises:
        LLMNotConfiguredError: if no endpoint/model is configured (checked
            first, before any client construction or network call).
        LLMUpstreamError: on any SDK/API error, empty completion, output that
            does not parse to a JSON object, or a timeout -- either the SDK's
            own (a per-phase inactivity timeout expiring) or the call
            exceeding ``openai_timeout_seconds`` as a genuine wall-clock
            deadline (see the ``asyncio.timeout`` below). The message is a
            safe category + short reason; it never contains the base URL, API
            key, or a full response body.
    """
    if not llm_configured():
        # `from None` severs any context: this gate now also fields a
        # syntactically invalid base_url (llm_configured reparses it and reports
        # unconfigured), and suppressing context keeps that path's traceback as
        # free of a URL fragment as the _get_client construction path already is.
        raise LLMNotConfiguredError("The LLM endpoint is not configured.") from None

    settings = get_settings()
    client = _get_client()
    try:
        # asyncio.timeout enforces a genuine WALL-CLOCK deadline around the
        # entire call -- unlike the client-level timeout (see _build_client),
        # which only bounds per-phase inactivity and would otherwise let a
        # slow-drip endpoint hold the request open past openai_timeout_seconds
        # by sending a byte just before each read timeout.
        async with asyncio.timeout(settings.openai_timeout_seconds):
            completion = await client.chat.completions.create(
                # Same normalized_model helper llm_configured and /llm/status use,
                # so the model sent at runtime is exactly the one status validated
                # and reported back to the client.
                model=normalized_model(settings),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                # No fixed temperature: some OpenAI-compatible endpoints --
                # reasoning-style models in particular -- reject the parameter
                # outright, which would otherwise turn every call against them
                # into a 400 -> 502. Omit it and let the model/endpoint apply
                # its own default.
            )
    except LLMNotConfiguredError, LLMUpstreamError:
        # Our own taxonomy carries its own HTTP mapping (503 stays 503, an
        # already-shaped 502 stays 502). Re-raise it UNCHANGED so the total
        # catch-all below can never rewrap one of these into a generic upstream
        # 502 and destroy its real status. Listed first so it wins over the
        # broad `except Exception` (both are Exception subclasses).
        raise
    except TimeoutError:
        # asyncio.timeout's own deadline expiry (see the comment above) --
        # distinguished from the total boundary below with its own "Timeout"
        # category. An SDK-raised openai.APITimeoutError is an unrelated class
        # (an OpenAIError, not a builtin TimeoutError) and still falls through
        # to the OpenAIError arm, independent of this clause's position.
        raise LLMUpstreamError(f"Timeout: {_UPSTREAM_REASON}") from None
    except OpenAIError as exc:
        # Category only (the SDK error's class name) plus the fixed shared
        # reason. Never str(exc): APIConnectionError chains the target URL,
        # APIStatusError carries the response body -- either would leak past
        # this boundary. `from None` (not `from exc`) severs the cause chain so
        # the original SDK error -- whose str/args can embed base_url, api_key,
        # or a raw response body -- cannot ride along in __cause__ into a
        # traceback-logging sink; the safe category prefix keeps diagnosis
        # possible.
        raise LLMUpstreamError(f"{type(exc).__name__}: {_UPSTREAM_REASON}") from None
    except Exception as exc:
        # Total boundary. A merely OpenAI-*compatible* endpoint can return a 2xx
        # whose body is broken or empty JSON; the SDK parses that body INTERNALLY
        # and raises json.JSONDecodeError / ValueError (a ValueError subclass) --
        # NEITHER an OpenAIError -- so without this arm such output escapes as an
        # unhandled 500 instead of the intended 502. Map every remaining
        # non-taxonomy failure at this call onto the same upstream taxonomy,
        # carrying only the exception category (never str(exc), which could embed
        # a response body) and severing the chain with `from None`.
        raise LLMUpstreamError(f"{type(exc).__name__}: {_UPSTREAM_REASON}") from None

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
