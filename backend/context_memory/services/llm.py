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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, cast

import httpx
from openai import AsyncOpenAI, OpenAIError
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from pydantic import BaseModel, ValidationError

from context_memory.config import Settings, get_settings
from context_memory.services import llm_log

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

# The final user turn appended once the tool-round budget (llm_tool_rounds_max,
# or a per-call override) is spent: the loop stops advertising tools and makes
# ONE last tools-free completion, so a model that keeps asking to call tools is
# forced to commit to its answer instead of spinning the interaction. Phrased so
# the model knows the tool phase is over AND what it must produce now (the same
# single JSON object the strict-output rule in the system prompt already
# describes), rather than a bare "stop".
_TOOL_BUDGET_EXHAUSTED = (
    "The tool-call budget for this request is exhausted; do not request any more "
    "tool calls. Produce your final answer now as the single JSON object required "
    "by the system instructions."
)

# How many characters of a tool call's raw arguments JSON ride into the synthetic
# response body recorded for a tool_calls attempt (see _summarize_tool_calls).
# Small on purpose: the AI 日誌 only needs to show WHICH tools were called with
# roughly what, not the full argument blob (which reappears verbatim inside the
# very next attempt's request_messages anyway), and llm_log._stored_body caps the
# whole synthetic line again regardless.
_TOOL_ARGS_PREVIEW_CHARS = 200

# Hard cap on how many tool calls from ONE assistant reply are actually EXECUTED.
# A single reply can legitimately carry a handful of parallel calls, but a broken
# or hostile model could emit thousands in one turn; executing them all would
# fan out that many handler runs (subprocesses, network) for one round. Calls
# past the cap are NOT executed -- each still gets a role:"tool" result (the
# rejection text below) keyed to its own id, because the endpoint requires one
# result per tool_call in the echoed assistant turn, so the pairing must hold
# even for the rejected ones.
_MAX_TOOL_CALLS_PER_REPLY = 16

# The role:"tool" result content for a call rejected WITHOUT execution because it
# was past _MAX_TOOL_CALLS_PER_REPLY. Fed back so the model learns why, and so
# the assistant turn's tool_calls entry still has its matching result.
_TOO_MANY_TOOL_CALLS = "tool call rejected: too many tool calls in one reply"

# Hard ceiling on how many tool calls in ONE reply the loop will even ENTER the
# tool round for. Distinct from _MAX_TOOL_CALLS_PER_REPLY above, which bounds
# EXECUTION but NOT resource consumption: a reply carrying, say, 100k tool_calls
# would still be fully summarized (_summarize_tool_calls), echoed verbatim in the
# assistant turn, and answered with ~100k rejected-result messages -- unbounded
# memory/prompt growth plus a long pre-yield synchronous stretch, all to service
# one already-abusive reply. So a reply with MORE than this many calls is not
# entered at all: it is treated as a malformed/abusive upstream reply and raised
# on the SAME LLMUpstreamError (502) taxonomy an unparseable reply uses (see the
# entry check in _run_structured). Calls 17..64 keep the existing
# rejected-with-message behavior; only 65+ trips this whole-reply refusal. 64 is
# comfortably above any legitimate burst of parallel calls a model makes in one
# turn, and (being small) also bounds the per-round result-building loop so it can
# never itself become the long synchronous stretch this exists to prevent.
_MAX_TOOL_CALLS_ACCEPTED = 64

# Prefix of the safe, config-free LLMUpstreamError message raised for a reply
# whose tool_calls count exceeds _MAX_TOOL_CALLS_ACCEPTED. Built like the other
# category messages (a short reason, no str(exc), no config value) and classified
# as a generic upstream failure by _classify_upstream_outcome. The runtime message
# appends the offending count -- "tool_calls flood: <N> calls in one reply".
_TOOL_CALLS_FLOOD_PREFIX = "tool_calls flood"

# Companion to _MAX_TOOL_CALLS_ACCEPTED that bounds SIZE, not COUNT. The count cap
# alone is not enough: 64 calls (under the count cap) each carrying a huge
# ``arguments`` string still get summarized, echoed verbatim in the assistant turn,
# json.loads'd, and fed into the NEXT round's request -- unbounded OUTBOUND
# memory/prompt growth for one reply. This caps the aggregate serialized size of a
# single reply's tool_calls. Distinct from the tool RESULTS, which are separately
# bounded by ``settings.llm_tool_output_max_chars``; that knob never sees the
# model's outbound call payload, which is what this guards. 256 KiB is far above
# any legitimate parallel-call burst (even 64 calls with rich arguments) yet small
# enough that the echoed assistant turn and the recorded trace stay bounded.
_MAX_TOOL_CALLS_TOTAL_BYTES = 256 * 1024

# Prefix of the safe, config-free LLMUpstreamError message raised for a reply whose
# tool_calls aggregate size exceeds _MAX_TOOL_CALLS_TOTAL_BYTES. Sibling to
# _TOOL_CALLS_FLOOD_PREFIX -- same taxonomy, same _classify_upstream_outcome
# "upstream_error" bucket; the runtime message appends the measured size --
# "tool_calls oversized: <N> bytes in one reply".
_TOOL_CALLS_OVERSIZED_PREFIX = "tool_calls oversized"


@dataclass(slots=True)
class LlmTool:
    """One tool ``generate_structured`` may let the model call mid-completion.

    ``spec`` is the OpenAI tools-array entry verbatim -- ``{"type": "function",
    "function": {"name", "description", "parameters"}}`` -- passed straight to
    ``chat.completions.create(tools=[...])``. ``handler`` is an async callable
    that receives the ALREADY-PARSED arguments object (a dict) the model emitted
    and returns the tool-result STRING fed back to the model as that call's
    result.

    Contract on ``handler``: it must NOT raise for ordinary domain failures --
    an unreachable KB, a 404, a bad query -- it returns descriptive error TEXT
    instead, because that text is the most useful thing to hand the model (it
    can adjust and try again). If it nonetheless raises, the loop catches
    ``Exception`` and feeds a safe ``"tool execution failed: <ClassName>"``
    string back as that call's result: one broken tool degrades to a failed
    tool result the model can react to, never a 502 that sinks the whole
    interaction. The arguments dict is produced by the loop's own defensive
    ``json.loads`` (malformed arguments never reach the handler -- the model
    gets an error string for that call instead), so a handler can assume it was
    handed a real dict.
    """

    spec: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[str]]


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


def _completion_content(completion: Any) -> str | None:
    """Best-effort read of ``choices[0].message.content`` (a str), or None.

    Distinct from ``_extract_content``: this NEVER raises and treats a
    missing/None/non-str content as None, because it feeds the ``content`` field
    of the assistant message we echo back alongside its ``tool_calls`` -- where
    None is the ordinary, valid shape for a pure tool-call turn (a model that
    only asked to call a tool sends no prose). ``_extract_content``'s 502 is for
    the FINAL-answer path, where empty content genuinely is an unusable reply.
    """
    choices = getattr(completion, "choices", None)
    if not isinstance(choices, list) or not choices:
        return None
    message = getattr(choices[0], "message", None)
    if message is None:
        return None
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else None


def _extract_tool_calls(completion: Any) -> list[Any]:
    """Return ``choices[0].message.tool_calls`` as a list, or [] if there are none.

    Defensive at every hop, exactly like ``_extract_content``: a merely
    OpenAI-*compatible* gateway can shape ``choices`` / ``message`` / ``tool_calls``
    in any broken way without the SDK objecting, and this must never raise
    AttributeError/TypeError into the loop. Returning [] for anything that is not
    a non-empty list routes the completion to the normal content-extraction path
    (which then maps a genuinely empty/malformed body to its own 502) -- so a
    broken body is never mistaken for "the model wanted to call a tool".
    """
    choices = getattr(completion, "choices", None)
    if not isinstance(choices, list) or not choices:
        return []
    message = getattr(choices[0], "message", None)
    if message is None:
        return []
    tool_calls = getattr(message, "tool_calls", None)
    if not isinstance(tool_calls, list) or not tool_calls:
        return []
    return tool_calls


def _tool_call_fields(tool_call: Any) -> tuple[str, str, str]:
    """Pull ``(id, function.name, function.arguments)`` from a tool-call object.

    Every field is read defensively and falls back to "" so a malformed tool
    call from a compatible gateway can never raise here: a missing name later
    resolves to "no such tool", missing/blank arguments parse to an empty dict,
    and the (possibly empty) id is used verbatim for BOTH the echoed assistant
    ``tool_calls`` entry and the matching ``role:"tool"`` result, so the two
    always pair up however degenerate the source was.
    """
    tc_id = getattr(tool_call, "id", None)
    function = getattr(tool_call, "function", None)
    name = getattr(function, "name", None)
    arguments = getattr(function, "arguments", None)
    return (
        tc_id if isinstance(tc_id, str) else "",
        name if isinstance(name, str) else "",
        arguments if isinstance(arguments, str) else "",
    )


def _index_tools(tools: list[LlmTool]) -> dict[str, LlmTool]:
    """Map each tool's advertised function name to the tool, for O(1) dispatch.

    Reads the name out of the spec the same way the model sees it
    (``spec["function"]["name"]``), defensively, so a spec missing that path is
    simply not dispatchable (its name never enters the map) rather than a crash.
    On a duplicate name the last wins -- registries upstream (tools.py) dedupe by
    directory name, so this is only a last-resort tie-break.
    """
    indexed: dict[str, LlmTool] = {}
    for tool in tools:
        function = tool.spec.get("function") if isinstance(tool.spec, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            indexed[name] = tool
    return indexed


def _summarize_tool_calls(tool_calls: list[Any]) -> str:
    """Build the compact synthetic response body recorded for a tool_calls attempt.

    Shape: ``[tool_calls] name1({args...}), name2({...})`` -- enough for the AI
    日誌 to show the agentic trace (which tools were called, roughly with what)
    without the full argument blobs, which reappear verbatim in the very next
    attempt's request_messages. Arguments are previewed to
    ``_TOOL_ARGS_PREVIEW_CHARS`` and the whole line is size-capped again by
    llm_log's own ``_stored_body``.
    """
    parts: list[str] = []
    for tool_call in tool_calls:
        _, name, arguments = _tool_call_fields(tool_call)
        preview = arguments[:_TOOL_ARGS_PREVIEW_CHARS]
        if len(arguments) > _TOOL_ARGS_PREVIEW_CHARS:
            preview += "…"
        parts.append(f"{name or '<unnamed>'}({preview})")
    return "[tool_calls] " + ", ".join(parts)


def _assistant_tool_call_message(
    content: str | None, tool_calls: list[Any]
) -> ChatCompletionMessageParam:
    """Rebuild the assistant turn (content + tool_calls) to echo back to the model.

    The conversation MUST carry the assistant's tool-call request before the
    matching ``role:"tool"`` results, or the endpoint rejects the follow-up
    request. Rebuilt from scratch as the SDK's message-param dict shape (rather
    than passing the response object through) so only the fields the API expects
    travel back, each field defensively normalized via ``_tool_call_fields``.
    ``content`` is None for a pure tool-call turn -- the valid, ordinary shape.
    """
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": tc_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
            for tc_id, name, arguments in (_tool_call_fields(tc) for tc in tool_calls)
        ],
    }
    return cast(ChatCompletionMessageParam, message)


async def _invoke_tool_handler(tool: LlmTool, arguments_json: str) -> str:
    """Parse the model's arguments and run one tool handler, never raising.

    The two failure modes below both feed a descriptive string back as the tool
    RESULT (the model can then correct itself), never an exception:

    * malformed arguments -- ``json.loads`` fails, or the parsed value is not a
      JSON object -- so the handler (which is promised a dict) is never called
      with junk;
    * the handler itself raising despite its no-raise contract -- caught here as
      a safe category so one broken tool cannot 502 the whole interaction.

    ``RecursionError`` is caught alongside the parse errors for the same reason
    ``_parse_json_object`` catches it: pathologically nested arguments must
    degrade to an error result, not an unhandled 500.
    """
    try:
        parsed = json.loads(arguments_json) if arguments_json.strip() else {}
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        return f"tool call rejected: arguments were not valid JSON ({exc})"
    if not isinstance(parsed, dict):
        return "tool call rejected: arguments were not a JSON object"
    try:
        result = await tool.handler(parsed)
    except Exception as exc:
        return f"tool execution failed: {type(exc).__name__}"
    return result if isinstance(result, str) else str(result)


async def _tool_result_message(
    tool_call: Any, tools_by_name: dict[str, LlmTool]
) -> ChatCompletionMessageParam:
    """Execute one tool call and wrap its result as a ``role:"tool"`` message.

    An unknown tool name (a model hallucinating a tool we never advertised)
    yields an error result rather than a crash, keyed to the SAME tool_call_id
    the assistant turn used so the endpoint can still pair request to result.
    """
    tc_id, name, arguments_json = _tool_call_fields(tool_call)
    tool = tools_by_name.get(name)
    if tool is None:
        result = f"tool execution failed: no tool named {name!r} is available"
    else:
        result = await _invoke_tool_handler(tool, arguments_json)
    message: dict[str, Any] = {"role": "tool", "tool_call_id": tc_id, "content": result}
    return cast(ChatCompletionMessageParam, message)


def _rejected_tool_result_message(tool_call: Any) -> ChatCompletionMessageParam:
    """A role:"tool" result for a call REJECTED without execution (over the
    _MAX_TOOL_CALLS_PER_REPLY cap), keyed to the call's own id.

    No handler runs -- the id pairing is the whole point: the assistant turn we
    echo back carries EVERY tool_call from the reply, and the endpoint rejects
    the follow-up unless each one has a matching result, so a capped call still
    needs a result message (this fixed rejection text) even though nothing ran.
    """
    tc_id, _, _ = _tool_call_fields(tool_call)
    message: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": tc_id,
        "content": _TOO_MANY_TOOL_CALLS,
    }
    return cast(ChatCompletionMessageParam, message)


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
    tool_specs: list[ChatCompletionToolParam] | None,
) -> Any:
    """Send ONE chat-completion request, threading ``max_tokens``/``tools`` only when set.

    Four explicit call shapes rather than a spread ``**kwargs`` dict: the type
    checker can verify each keyword against the SDK's typed ``create`` (a spread
    dict fails ``ty``'s overload match), and -- crucially -- each unset branch
    OMITS its keyword entirely. ``tool_specs=None`` (the tools-disabled path,
    which is EVERY call in the tool-less build) sends no ``tools`` kwarg at all,
    so the outgoing kwargs stay byte-identical to the pre-tool-loop call -- the
    pinned llm-service tests assert ``tools`` (and ``max_tokens``) are absent.
    Sending the SDK's ``omit`` sentinel instead would still surface those keys in
    a stubbed call's recorded kwargs. The 2x2 (max_tokens set/unset x tools
    set/unset) is spelled out flat for that byte-identity, not folded into a dict.

    ``openai_max_output_tokens`` defaults to None because some reasoning-style
    endpoints reject an explicit ``max_tokens`` (a 400 -> 502); an operator sets
    it only to raise a gateway's small default completion cap. No fixed
    temperature is ever sent, for the same reason (some endpoints 400 on it). The
    model is the shared ``normalized_model`` value ``llm_configured`` and
    ``/llm/status`` also use, so runtime and status can never disagree.
    """
    model = normalized_model(settings)
    max_tokens = settings.openai_max_output_tokens
    if tool_specs is None:
        if max_tokens is None:
            return await client.chat.completions.create(model=model, messages=messages)
        return await client.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens
        )
    if max_tokens is None:
        return await client.chat.completions.create(
            model=model, messages=messages, tools=tool_specs
        )
    return await client.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens, tools=tool_specs
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
    *,
    tools: list[LlmTool] | None,
    max_tool_rounds: int,
    timeout_seconds: float,
) -> ModelT:
    """The config gate + agentic attempt loop, feeding ``recorder`` as it goes.

    Split out from ``generate_structured`` so ALL recorder finalization happens
    at the outer boundary, OUTSIDE this function's ``asyncio.timeout`` -- the
    sinks' I/O then never runs under the wall-clock deadline, and every exit (a
    return here, or any raise) maps to exactly one ``recorder.finish`` in the
    wrapper.

    Two phases share ONE deadline and ONE message list:

    * a TOOL phase (only when ``tools`` is non-empty): while the model replies
      with tool_calls and the round budget (``max_tool_rounds``) is unspent, its
      calls are executed, their results appended, and the loop continues. Each
      such round is one create()/recorder attempt whose recorded response is the
      compact synthetic ``[tool_calls] ...`` trace;
    * a FINAL-answer phase: the first completion that is NOT a tool round (the
      model answered directly, or the tool budget ran out and the last create()
      was made WITHOUT tools) goes through the exact same strict parse + ONE
      corrective retry as the tool-less build.

    With ``tools`` None/empty NOTHING about the tool phase runs and the create()
    kwargs are byte-identical to the pre-tool-loop call, so the pinned
    llm-service/llm-log tests are untouched.
    """
    if not llm_configured():
        # `from None` severs any context: this gate also fields a syntactically
        # invalid base_url (llm_configured reparses it and reports unconfigured),
        # and suppressing context keeps that path's traceback as free of a URL
        # fragment as the _get_client construction path already is.
        raise LLMNotConfiguredError("The LLM endpoint is not configured.") from None

    client = _get_client()

    # Normalize the tool set ONCE. An empty list is treated exactly like None
    # (no tools advertised on ANY create(), kwargs byte-identical to the
    # tool-less call), so a caller passing `tools=[]` can never diverge from
    # `tools=None`. `tool_specs` is what rides on create(); `tools_by_name`
    # dispatches an executed call back to its handler.
    tool_specs = [cast(ChatCompletionToolParam, tool.spec) for tool in tools] if tools else None
    tools_by_name = _index_tools(tools) if tools else {}

    # The conversation accumulates across the whole loop: tool rounds append the
    # assistant tool-call turn plus one result per call; the final phase's
    # corrective retry appends the rejected reply plus a corrective user turn.
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": _schema_guided_system_prompt(system_prompt, model_cls)},
        {"role": "user", "content": user_prompt},
    ]

    tool_rounds_used = 0
    # The single corrective retry is a FINAL-answer mechanism; once it is spent
    # (or once we are otherwise finalizing) tools are never advertised again, so
    # a corrective turn can never re-open the tool phase and loop past the
    # single-retry contract.
    correction_used = False

    try:
        # ONE asyncio.timeout spans the WHOLE loop -- every tool round plus the
        # final try-and-retry -- so the wall-clock budget (the possibly-overridden
        # ``timeout_seconds``; the installer uses a much larger one) bounds the
        # entire agentic interaction end to end, not each create() separately.
        # Unlike the client-level timeout (see _build_client), which only bounds
        # per-phase inactivity, this is a genuine deadline on total duration.
        async with asyncio.timeout(timeout_seconds):
            while True:
                # Advertise tools only while the round budget is unspent AND we
                # are not finalizing (see `correction_used`). Once spent, the
                # next create() is tools-free -- that is the "one final create()
                # without tools" the budget-exhausted nudge below sets up.
                advertise_tools = (
                    tool_specs is not None
                    and tool_rounds_used < max_tool_rounds
                    and not correction_used
                )
                # Snapshot the messages ACTUALLY sent for this attempt (the
                # recorder copies them, since `messages` is rebuilt each round /
                # for the corrective retry below).
                recorder.begin_attempt(messages)
                try:
                    completion = await _create_completion(
                        client, settings, messages, tool_specs if advertise_tools else None
                    )
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

                # Capture usage from whatever completion carried it (per attempt).
                recorder.record_usage(completion)

                # TOOL BRANCH, CHECKED FIRST: a tool_calls reply legitimately
                # carries content=None, which the final path's _extract_content
                # (correctly, for a non-tool reply) 502s as empty -- so tool_calls
                # must be resolved before any content extraction runs. Only
                # consulted when tools were actually advertised this round; a
                # stray tool_calls on a tools-free create() is ignored and falls
                # through to the strict parse (the model was told not to).
                if advertise_tools:
                    tool_calls = _extract_tool_calls(completion)
                    if tool_calls:
                        # ENTRY bound (N1): a reply carrying more than
                        # _MAX_TOOL_CALLS_ACCEPTED calls is abusive/malformed. Refuse
                        # it here, BEFORE any O(N) work -- no _summarize_tool_calls,
                        # no echoed assistant turn, no per-call rejected results (each
                        # of which grows memory/prompt with N). Treat it exactly like
                        # an unparseable upstream reply: record the safe category on
                        # the in-flight attempt (its response stays None, as on the
                        # empty-content 502 path above) and raise the SAME
                        # LLMUpstreamError taxonomy, which the wrapper classifies as
                        # an upstream failure and finalizes on the record. No handler
                        # runs. `from None` severs context, like every other arm.
                        count = len(tool_calls)
                        if count > _MAX_TOOL_CALLS_ACCEPTED:
                            flood = f"{_TOOL_CALLS_FLOOD_PREFIX}: {count} calls in one reply"
                            recorder.fail_current_attempt(flood)
                            raise LLMUpstreamError(flood) from None
                        # SIZE bound (F4), the sibling guard at the SAME entry point:
                        # a reply UNDER the count cap can still carry a huge aggregate
                        # ``arguments`` payload, which would be summarized, echoed in
                        # the assistant turn, and fed to the next round -- unbounded
                        # outbound memory/prompt. Measure the serialized size the same
                        # strings that ride back on the wire contribute -- id + name +
                        # arguments, exactly what _tool_call_fields extracts and
                        # _assistant_tool_call_message echoes -- summed WITHOUT a
                        # re-serialization pass. Over the cap is treated identically to
                        # the count flood: record the safe category and raise the SAME
                        # upstream 502 taxonomy, BEFORE any O(N) work and with no
                        # handler run. `from None` severs context, like every arm.
                        total_bytes = sum(
                            len(tc_id) + len(name) + len(arguments)
                            for tc_id, name, arguments in (
                                _tool_call_fields(tc) for tc in tool_calls
                            )
                        )
                        if total_bytes > _MAX_TOOL_CALLS_TOTAL_BYTES:
                            oversized = (
                                f"{_TOOL_CALLS_OVERSIZED_PREFIX}: {total_bytes} bytes in one reply"
                            )
                            recorder.fail_current_attempt(oversized)
                            raise LLMUpstreamError(oversized) from None
                        # The synthetic trace is THIS attempt's recorded response;
                        # the tool RESULTS then appear inside the next attempt's
                        # request_messages naturally (no recorder schema change).
                        recorder.record_response(_summarize_tool_calls(tool_calls))
                        # In-place appends (not `messages = [*messages, ...]`): the
                        # recorder ALREADY snapshotted this attempt's messages at
                        # begin_attempt above -- it copies them field-by-field into
                        # a fresh list (see llm_log.begin_attempt), so mutating
                        # `messages` after that point can never rewrite the recorded
                        # attempt. Aliasing is therefore safe, and the next round's
                        # begin_attempt copies the grown list afresh.
                        messages.append(
                            _assistant_tool_call_message(
                                _completion_content(completion), tool_calls
                            )
                        )
                        for index, tool_call in enumerate(tool_calls):
                            # Execute only the first _MAX_TOOL_CALLS_PER_REPLY calls;
                            # the rest are rejected WITHOUT execution but still get a
                            # result (so every tool_call in the echoed assistant turn
                            # pairs with a role:"tool" message the endpoint requires).
                            if index < _MAX_TOOL_CALLS_PER_REPLY:
                                messages.append(
                                    await _tool_result_message(tool_call, tools_by_name)
                                )
                            else:
                                messages.append(_rejected_tool_result_message(tool_call))
                            # Yield to the event loop once per processed call: the
                            # unknown-tool and the capped-reject paths never await
                            # anything, so a run of them would otherwise execute as
                            # one uninterruptible block and starve the asyncio.timeout
                            # deadline (which fires only when the coroutine yields).
                            # The N1 entry cap already bounds this loop to at most
                            # _MAX_TOOL_CALLS_ACCEPTED iterations, so this is now a
                            # small, defensive checkpoint rather than the sole guard
                            # against an unbounded synchronous stretch -- but it keeps
                            # the round responsive to the deadline regardless.
                            await asyncio.sleep(0)
                        tool_rounds_used += 1
                        # If that spent the last permitted round, append the
                        # finalize nudge NOW so the next (tools-free) create() is
                        # explicitly told to stop calling tools and answer.
                        if tool_rounds_used >= max_tool_rounds:
                            messages.append({"role": "user", "content": _TOOL_BUDGET_EXHAUSTED})
                        continue

                # FINAL-ANSWER PATH: no tool round happened (tools not advertised,
                # or advertised but the model answered directly). Parse strictly.
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
                    # never here). Once the single corrective retry is spent, fail
                    # with a FIXED, config-free message.
                    recorder.fail_current_attempt(type(exc).__name__)
                    if correction_used:
                        raise LLMUpstreamError(_INVALID_STRUCTURED_OUTPUT) from None
                    correction_used = True
                    messages = [
                        *messages,
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": _corrective_user_message(exc)},
                    ]
    except TimeoutError:
        # asyncio.timeout's deadline expiry (converted from the CancelledError it
        # injects) OR a re-raised builtin TimeoutError from the SDK boundary --
        # both land here with the distinct "Timeout" category. `from None` severs
        # the cause chain, same as every other arm. The in-flight attempt (begun
        # via recorder.begin_attempt above) never reaches any of the loop's own
        # except clauses on THIS path -- asyncio.timeout's CancelledError is a
        # BaseException none of them catch -- so without this call its error
        # would stay None forever even though the record's own outcome/error
        # correctly reads "timeout": every attempt in a finished record must
        # carry a classification.
        recorder.fail_current_attempt("Timeout")
        raise LLMUpstreamError(f"Timeout: {_UPSTREAM_REASON}") from None

    # Unreachable at runtime -- the loop always returns a validated instance or
    # raises -- but present so every path provably returns/raises (satisfying the
    # type checker) and a future change to the loop bounds cannot fall through to
    # an implicit `None`.
    raise LLMUpstreamError(_INVALID_STRUCTURED_OUTPUT)


async def generate_structured[ModelT: BaseModel](
    system_prompt: str,
    user_prompt: str,
    model_cls: type[ModelT],
    *,
    workflow: str = "unknown",
    tools: list[LlmTool] | None = None,
    max_tool_rounds: int | None = None,
    timeout_seconds: float | None = None,
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

    ``tools`` (optional) enables an agentic TOOL phase before that final answer:
    while the model replies with tool_calls and the round budget is unspent,
    each call's handler runs and its result is fed back, then the model is asked
    again (see ``_run_structured``). ``tools=None`` or ``[]`` disables the phase
    entirely and keeps the create() kwargs byte-identical to the tool-less call,
    so every existing caller and test is unaffected. ``max_tool_rounds`` (None ->
    ``settings.llm_tool_rounds_max``) caps those rounds; ``timeout_seconds``
    (None -> ``settings.openai_timeout_seconds``) overrides the ONE wall-clock
    deadline wrapped around the whole interaction -- the installer (Phase 5c)
    passes a much larger budget for its long tool-building session.

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
    # Resolve the two per-call overrides against settings here, at the recorder
    # boundary, so _run_structured receives concrete values and the default path
    # (both None) is exactly the pre-tool-loop behavior.
    resolved_rounds = settings.llm_tool_rounds_max if max_tool_rounds is None else max_tool_rounds
    resolved_timeout = (
        settings.openai_timeout_seconds if timeout_seconds is None else timeout_seconds
    )
    try:
        result = await _run_structured(
            recorder,
            settings,
            system_prompt,
            user_prompt,
            model_cls,
            tools=tools,
            max_tool_rounds=resolved_rounds,
            timeout_seconds=resolved_timeout,
        )
    except LLMNotConfiguredError as exc:
        recorder.finish(outcome="not_configured", error=str(exc))
        raise
    except LLMUpstreamError as exc:
        recorder.finish(outcome=_classify_upstream_outcome(exc), error=str(exc))
        raise
    else:
        recorder.finish(outcome="ok", error=None)
        return result
