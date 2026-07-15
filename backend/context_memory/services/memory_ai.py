"""AI memory workflows: prompts and untrusted-output sanitizers.

Each workflow (``capture_draft`` / ``enrich_item`` / ``assist_update``) builds a
system prompt that encodes the context-memory methodology and delegates to
``generate_structured`` with the workflow's pydantic model. That model both
guides the LLM (its JSON Schema is injected into the prompt) and validates the
reply: its *sanitizers treat the LLM output as untrusted input* -- types are
coerced defensively, unknown keys dropped, and every field capped (per-section
length, tag/question counts, title length) so nothing oversized or unexpected
can reach the database. A validation failure is retried once and otherwise
mapped to a 502 inside ``generate_structured``; the workflows here no longer
validate separately. The system prompts are module-level constants so their
methodology rules can be asserted directly in tests.
"""

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from context_memory.config import get_settings
from context_memory.models import MemoryStatus, utcnow
from context_memory.services.llm import generate_structured

# --- caps (constraint: bound everything; LLM output is untrusted) ----------

# Per free-text section, matching the request-side input bound.
_PER_SECTION_CAP = 20000
_TITLE_MAX = 300
_MAX_TAGS = 10
_TAG_MAX = 50
# The methodology's hard ceiling on quick-capture follow-up questions.
_MAX_QUESTIONS = 3


# --- defensive coercion helpers -------------------------------------------


# Max nesting depth _coerce_str will descend into a list/tuple before refusing.
# LLM output is untrusted and can be pathologically nested; a list ~1000 deep
# blows Python's recursion limit, and an unbounded _coerce_str would then raise
# RecursionError from inside the pydantic before-validators below -- which
# pydantic does NOT convert to a ValidationError, so it would escape as an
# unhandled 500. Bounding the descent to a small constant makes such input raise
# ValueError instead, which pydantic DOES fold into a ValidationError (mapped to
# 502). Legitimate capture/enrich fields nest at most one level (a flat list of
# strings), so this ceiling is never reached by real output.
_MAX_COERCE_DEPTH = 8


def _coerce_str(value: object, _depth: int = 0) -> str:
    """Coerce arbitrary JSON-decoded input to a string.

    LLM output is untrusted: a field the prompt asked to be a string may come
    back as a number, bool, or list. Map each to a reasonable string rather
    than letting pydantic reject the whole draft over one stray type.

    Raises ValueError on two shapes of malformed input -- both accepted
    without complaint by ``json.loads`` itself, so this is where they must
    be caught:

    * a list/tuple nested beyond ``_MAX_COERCE_DEPTH`` (see that constant),
      so a pathologically nested payload degrades to a 502 rather than a
      RecursionError-driven 500;
    * a string that is not UTF-8 encodable -- concretely, one carrying an
      unpaired Unicode surrogate (e.g. ``"\\ud800"`` with no partner). A JSON
      parser may hand that back as an ordinary-looking ``str``, and it would
      then sail through every cap/strip below and pass pydantic untouched,
      only to blow up LATER as an uncaught ``UnicodeEncodeError`` -- from
      SQLite text binding or FastAPI response serialization -- possibly
      AFTER a write has already committed. Checking it here, the single
      choke point every sanitized string passes through (directly, or via
      recursion for a list item), routes it into the same pre-write
      ``ValidationError`` -> 502 path as every other malformed-input case
      instead.

    ``_depth`` is internal recursion bookkeeping; callers never pass it.
    """
    if value is None:
        result = ""
    elif isinstance(value, str):
        result = value
    elif isinstance(value, bool):
        result = "true" if value else "false"
    elif isinstance(value, (int, float)):
        result = str(value)
    elif isinstance(value, (list, tuple)):
        if _depth >= _MAX_COERCE_DEPTH:
            raise ValueError("nested list/tuple exceeds the maximum coercion depth")
        result = "\n".join(_coerce_str(item, _depth + 1) for item in value if item is not None)
    else:
        result = str(value)
    try:
        result.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("string is not valid UTF-8 (e.g. an unpaired surrogate)") from exc
    return result


def _clean_text(value: object, cap: int = _PER_SECTION_CAP) -> str:
    """Coerce to string, strip surrounding whitespace, and hard-truncate."""
    return _coerce_str(value).strip()[:cap]


def _clean_str_list(value: object, *, max_items: int, item_cap: int) -> list[str]:
    """Coerce to a list of non-empty, per-item-capped strings, count-bounded.

    A scalar is treated as a single-element list. Empty/whitespace items are
    dropped; the result is capped at ``max_items`` (e.g. tags at 10, questions
    at 3) -- the server-side hard cap, independent of what the model returns.
    """
    if value is None:
        return []
    raw_items: list[object] = list(value) if isinstance(value, (list, tuple)) else [value]
    cleaned: list[str] = []
    for item in raw_items:
        text = _coerce_str(item).strip()[:item_cap]
        if text:
            cleaned.append(text)
        if len(cleaned) >= max_items:
            break
    return cleaned


# Quick capture may only ever suggest one of these two statuses; anything else
# the model returns (active/done/garbage/empty) collapses to capture-quick.
_CAPTURE_STATUSES: tuple[MemoryStatus, ...] = (
    MemoryStatus.capture_quick,
    MemoryStatus.needs_enrichment,
)


def _clean_capture_status(value: object) -> MemoryStatus:
    text = _coerce_str(value).strip()
    for status in _CAPTURE_STATUSES:
        if text == status.value:
            return status
    return MemoryStatus.capture_quick


# --- methodology rule fragments (asserted verbatim in prompt tests) --------

_RULE_HONESTY = (
    "Separate your confidence honestly. Mark each point as Known (directly "
    "stated or observed), Inferred (a plausible interpretation, which you MUST "
    "label as inferred), or Unknown (important but missing). Never invent "
    "facts to fill an Unknown gap."
)
_RULE_MAX_QUESTIONS = (
    "Ask at most 3 follow-up questions, and only for details that are likely to evaporate soon."
)
_RULE_BULLETS = "Prefer short factual bullets over polished prose."
_RULE_LANGUAGE = (
    "Write your output in the same language as the user content (for example, "
    "Traditional Chinese input yields Traditional Chinese output). Keep the "
    "JSON keys in English."
)


# --- quick capture ---------------------------------------------------------


CAPTURE_SYSTEM_PROMPT = "\n".join(
    [
        "You are the Context Memory quick-capture assistant. Turn a raw "
        "discussion into a structured draft so that future readers, or an AI "
        "agent, can resume the topic without replaying the conversation. "
        "Capture with minimum friction; the first draft may be imperfect.",
        _RULE_HONESTY,
        _RULE_MAX_QUESTIONS,
        _RULE_BULLETS,
        _RULE_LANGUAGE,
        "Use exactly these keys (the injected JSON Schema is the binding contract):",
        '- "title": a concise title (string).',
        '- "snapshot": a one-paragraph snapshot of the topic (string).',
        '- "why_matters": why this matters (string).',
        '- "known": bullet points directly stated or observed (string).',
        '- "inferred": bullet points you inferred, each labeled as inferred (string).',
        '- "unknown": important but missing details (string).',
        '- "next_actions": the next action or resume trigger (string).',
        '- "recovery_keywords": keywords future-you would search for (string).',
        '- "recovery_people": people who know about this (string).',
        '- "recovery_files": files, systems, or links involved (string).',
        '- "resume_trigger": what should make someone pick this up again (string).',
        '- "tags": up to 10 short tags (array of strings).',
        '- "suggested_status": either "capture-quick" or "needs-enrichment" (string).',
        '- "questions": at most 3 follow-up questions (array of strings).',
    ]
)


class CaptureDraft(BaseModel):
    """Sanitized quick-capture draft mapped onto a new MemoryItem.

    The ``mode="before"`` validator is the sanitizer: it rebuilds the payload
    from scratch with defensive coercion and caps, so only whitelisted,
    bounded, correctly-typed fields survive. Unknown keys are dropped;
    ``suggested_status`` is constrained to the two capture statuses; a missing
    or blank title fails (mapped to 502 upstream) rather than creating a
    title-less item.

    There is no ``open_questions`` field here: ``questions`` (below) is its
    SOLE source. The router (``routers.ai.capture``) derives the created
    item's ``open_questions`` section from ``questions`` directly, as bullet
    lines, in addition to returning them in the one-shot response.
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    snapshot: str = ""
    why_matters: str = ""
    known: str = ""
    inferred: str = ""
    unknown: str = ""
    next_actions: str = ""
    recovery_keywords: str = ""
    recovery_people: str = ""
    recovery_files: str = ""
    resume_trigger: str = ""
    tags: list[str] = Field(default_factory=list)
    suggested_status: MemoryStatus = MemoryStatus.capture_quick
    questions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _sanitize(cls, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        title = _clean_text(data.get("title"), cap=_TITLE_MAX)
        if not title:
            raise ValueError("title must not be empty")
        return {
            "title": title,
            "snapshot": _clean_text(data.get("snapshot")),
            "why_matters": _clean_text(data.get("why_matters")),
            "known": _clean_text(data.get("known")),
            "inferred": _clean_text(data.get("inferred")),
            "unknown": _clean_text(data.get("unknown")),
            "next_actions": _clean_text(data.get("next_actions")),
            "recovery_keywords": _clean_text(data.get("recovery_keywords")),
            "recovery_people": _clean_text(data.get("recovery_people")),
            "recovery_files": _clean_text(data.get("recovery_files")),
            "resume_trigger": _clean_text(data.get("resume_trigger")),
            "tags": _clean_str_list(data.get("tags"), max_items=_MAX_TAGS, item_cap=_TAG_MAX),
            "suggested_status": _clean_capture_status(data.get("suggested_status")),
            "questions": _clean_str_list(
                data.get("questions"), max_items=_MAX_QUESTIONS, item_cap=_PER_SECTION_CAP
            ),
        }


def _capture_user_prompt(raw_text: str) -> str:
    return "Raw discussion to capture into a memory item:\n\n" + raw_text


async def capture_draft(raw_text: str) -> CaptureDraft:
    """Run quick capture: prompt the LLM for a schema-guided, sanitized draft."""
    return await generate_structured(
        CAPTURE_SYSTEM_PROMPT, _capture_user_prompt(raw_text), CaptureDraft
    )


# --- section whitelist (untrusted enrich/update output) --------------------

# The anti-vaporization section fields an enrich/update may write, in checklist
# order. This is the whitelist: any other key the model returns (id, status,
# source, tags, or a hallucinated field) is dropped before anything is written,
# so a sanitized section dict can be setattr'd straight onto the item. A test
# pins that every entry is a real MemoryItem field.
SECTION_FIELD_ORDER: tuple[str, ...] = (
    "snapshot",
    "why_matters",
    "known",
    "inferred",
    "unknown",
    "decisions",
    "alternatives",
    "rationale",
    "consequences",
    "constraints",
    "assumptions",
    "risks",
    "evidence",
    "open_questions",
    "next_actions",
    "recovery_keywords",
    "recovery_people",
    "recovery_files",
    "resume_trigger",
)
SECTION_FIELDS = frozenset(SECTION_FIELD_ORDER)

# The history-bearing sections. Overwriting any of these would erase the
# reasoning trail the methodology's supersede-not-delete rule exists to
# protect, so the server MERGES rather than replaces them (see
# `merge_with_supersede`); every other section is a current-state field an
# enrich/update may legitimately replace wholesale. A subset of SECTION_FIELDS.
HISTORY_SECTIONS: frozenset[str] = frozenset(
    {"decisions", "rationale", "alternatives", "consequences"}
)


def _normalize_ws(text: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends.

    Used only to compare two section values line-by-line, so a model that reflows
    a line's internal whitespace while keeping its words still reads as having
    preserved that line (and is not needlessly superseded).
    """
    return " ".join(text.split())


def _history_lines_preserved(old: str, new: str) -> bool:
    """True only if EVERY non-empty line of ``old`` survives as a COMPLETE line of
    ``new`` (both whitespace-normalized).

    Deliberately line-level, NOT substring: an old line rewritten into a longer
    line (``成本低`` -> ``成本低估``, cost "low" turned into "underestimated")
    shares a substring but no whole line, and its meaning has changed -- so it
    does NOT count as preserved. The
    asymmetry is intentional and load-bearing: a redundant 'superseded' marker is
    lossless, but a MISSED marker silently destroys the reasoning trail the
    supersede-not-delete rule exists to protect, so we err toward marking whenever
    an old line is not reproduced verbatim.
    """
    new_lines = {_normalize_ws(line) for line in new.splitlines() if line.strip()}
    return all(_normalize_ws(line) in new_lines for line in old.splitlines() if line.strip())


# Storage cap for the history-bearing sections, applied AFTER
# merge_with_supersede concatenates -- a SECOND, LARGER tier above
# _PER_SECTION_CAP (20000 chars, the per-input bound on what a single LLM
# response may contribute -- see the top of this module). A history section
# legitimately accumulates a superseded block on every enrich/assist-update
# round that changes it (that is the whole point of supersede-not-delete), so
# two rounds of maximal input alone already reach ~40000 -- past the per-input
# cap but still a normal, honest history. Left unbounded, repeated rounds grow
# it without limit, so the AI path would end up persisting content the CRUD
# schemas' own _CRUD_SECTION_MAX (20000) would reject outright on a direct
# write. This cap instead bounds the ACCUMULATED, SERVER-GENERATED value --
# built only by this module's own concatenation, never a single untrusted
# blob -- so it can safely be looser than the per-input cap while still being
# bounded.
_HISTORY_STORE_CAP = 60000

# Appended once accumulated history is trimmed to fit _HISTORY_STORE_CAP, so a
# reader can tell that older superseded content was dropped rather than never
# having existed -- mirroring _TRUNCATION_MARKER's role for a single
# over-budget section (see _serialize_item_for_prompt below) but for this
# module's own storage cap rather than the prompt budget.
_HISTORY_TRUNCATION_MARKER = "\n\n…[更早的歷史已截斷]"


def _bound_history_store(merged: str, *, protected_len: int) -> str:
    """Trim ``merged`` to at most ``_HISTORY_STORE_CAP`` chars, oldest-first.

    ``merged`` is newest-first (``new``, then a dated marker, then ``old`` --
    and ``old`` may itself nest earlier rounds the same way), so the OLDEST
    superseded content always sits at the very tail. Cutting the tail off
    therefore drops the oldest block(s) first while the current content and
    its most recently superseded neighbours survive, behind a final
    truncation marker. ``protected_len`` (``new`` plus its marker) floors how
    much of the head is kept, so ``new`` itself is never trimmed below its own
    sanitized form -- in practice this floor is never binding, since
    ``_PER_SECTION_CAP`` leaves ``new`` at most a third of
    ``_HISTORY_STORE_CAP``.
    """
    if len(merged) <= _HISTORY_STORE_CAP:
        return merged
    keep = max(protected_len, _HISTORY_STORE_CAP - len(_HISTORY_TRUNCATION_MARKER))
    return merged[:keep] + _HISTORY_TRUNCATION_MARKER


def merge_with_supersede(old: str, new: str) -> str:
    """Losslessly fold an existing history-section value into its replacement.

    Enforces supersede-not-delete on the SERVER (a prompt rule alone cannot
    guarantee it): ``new`` is kept as-is ONLY when every non-empty line of ``old``
    reappears as a complete whitespace-normalized line of ``new`` (see
    ``_history_lines_preserved`` -- line-level containment, not substring, so a
    rewritten line is not mistaken for a preserved one); otherwise ``old`` is
    appended below ``new`` behind a dated 'superseded' marker so no prior decision
    or rationale is dropped without a visible trace. An empty/whitespace ``old``
    (nothing to preserve) yields ``new`` unchanged, and vice versa.

    The concatenation is then bounded to ``_HISTORY_STORE_CAP`` chars (see that
    constant for the two-tier rationale): once accumulated history exceeds it,
    the OLDEST superseded content is trimmed from the tail behind a final
    truncation marker, while ``new`` itself always survives intact (see
    ``_bound_history_store``). So the lossless guarantee is bounded, not
    unlimited -- content is ever dropped only past the storage cap, and even
    then loudly (a marker), never silently.

    Pure apart from the UTC date it stamps into the marker; it reads only its
    two string arguments and never touches configuration.
    """
    old = old or ""
    new = new or ""
    if not old.strip():
        return new
    if not new.strip():
        return old
    if _history_lines_preserved(old, new):
        return new
    marker = f"\n\n--- (superseded {utcnow().date().isoformat()}) ---\n"
    head = new + marker
    return _bound_history_store(head + old, protected_len=len(head))


# Defensive count cap on the returned checklist gaps.
_MAX_GAPS = 20


def _coerce_bool(value: object) -> bool:
    """Strictly coerce untrusted JSON to a bool; anything ambiguous -> False.

    ``checklist_complete`` drives a real state transition -- a True reading
    promotes a still-capturing item to active (see
    routers.ai._enrich_persist) -- so an out-of-spec value must never be
    silently read as true. Only three shapes are accepted as true: the bool
    ``True`` itself, the exact int ``1``, and the case-insensitive string
    ``"true"``. Only three shapes are accepted as false: ``False``, the exact
    int ``0``, and the case-insensitive string ``"false"``. Every other input
    -- any other number (2, -1, 1.5, ...), any other string ("yes", "1",
    "complete", ...), a list, a dict, None -- coerces to False rather than
    raising, matching every other sanitizer helper in this module (defensive,
    never a 500), but conservatively: ambiguous input never promotes.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return isinstance(value, str) and value.strip().lower() == "true"


def _clean_sections(value: object) -> dict[str, str]:
    """Keep only whitelisted, non-empty, capped section fields from a mapping.

    Non-whitelisted keys are dropped (so the model cannot set id/status/etc via
    a section), and empty values are dropped too -- an enrich/update must never
    blank an existing section, only add to it or replace it with real content.
    """
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or key not in SECTION_FIELDS:
            continue
        text = _clean_text(raw)
        if text:
            cleaned[key] = text
    return cleaned


def _extract_sections(data: dict[str, Any]) -> dict[str, str]:
    """Pull sanitized sections from a "sections" object, tolerating top-level.

    The prompt asks for a nested ``sections`` object; if the model instead
    places section fields at the top level, recover them there rather than
    silently returning nothing.
    """
    sections = _clean_sections(data.get("sections"))
    if not sections:
        sections = _clean_sections({k: v for k, v in data.items() if k in SECTION_FIELDS})
    return sections


# --- budgeted item serialization (bound the enrich/update prompt) ----------

# Characters each non-empty section is guaranteed before the remaining budget is
# shared out proportionally, so a section is never dropped to nothing while a
# larger one keeps everything. Only lowered below this when the budget cannot
# afford the floor for every competing section (see _allocate_section_budget).
_SECTION_MIN_KEEP = 400
# Appended to a section value that had to be cut to fit the budget, so the model
# can tell the content was truncated rather than genuinely ending there.
_TRUNCATION_MARKER = "…[內容過長已截斷]"

# Defensive per-field caps applied when rendering the header, independent of
# any upstream schema bound: an item predating schemas.MemoryItemCreate/
# Update's own title/tags bounds, or written directly against the database,
# could otherwise carry a title or tags list large enough to blow the header
# past the whole prompt budget even after every section is truncated to
# nothing. Matches memory_ai's own _TITLE_MAX; tags are capped on their
# rendered (comma-joined) line rather than per-tag, since it is the joined
# string that competes for header space.
_HEADER_TAGS_MAX = 2500

# The short metadata rendered at the top of the serialized item, in order.
# status/stage are tiny by construction (StrEnum members); title and tags are
# defensively truncated to _TITLE_MAX / _HEADER_TAGS_MAX (see
# _render_header_value) so the header is always small and fixed-size
# regardless of what the source row actually contains -- only the free-text
# sections are budgeted against what remains.
_HEADER_FIELD_ORDER: tuple[str, ...] = ("title", "status", "stage", "tags")
_HEADER_FIELD_CAPS: dict[str, int] = {"title": _TITLE_MAX, "tags": _HEADER_TAGS_MAX}


def _render_header_value(value: object, cap: int | None = None) -> str:
    """Render one header field to a single-line string (tags comma-joined),
    truncated to ``cap`` chars (behind the shared truncation marker, via
    ``_truncate_to``) when a cap is given.
    """
    if isinstance(value, (list, tuple)):
        parts = [_coerce_str(item).strip() for item in value]
        rendered = ", ".join(part for part in parts if part)
    else:
        rendered = _coerce_str(value).strip()
    return _truncate_to(rendered, cap) if cap is not None else rendered


def _allocate_section_budget(lengths: list[int], budget: int) -> list[int]:
    """Deterministically cap each section's length so the total fits ``budget``.

    Returns a per-section character cap. When the sections already fit, each cap
    is its own full length (no truncation). Otherwise every section is floored at
    ``min(length, min(_SECTION_MIN_KEEP, budget // n))`` -- a short section is kept
    whole and none is dropped -- and the leftover budget is then handed out in a
    single pass proportional to each section's unmet demand. The result always
    sums to ``<= budget`` and each cap is ``<=`` its section's own length, so the
    caller can never exceed the budget however the values are truncated.
    """
    n = len(lengths)
    if n == 0:
        return []
    if sum(lengths) <= budget:
        return list(lengths)
    floor = min(_SECTION_MIN_KEEP, budget // n)
    caps = [min(length, floor) for length in lengths]
    remaining = budget - sum(caps)
    unmet_total = sum(length - cap for length, cap in zip(lengths, caps, strict=True))
    if remaining > 0 and unmet_total > 0:
        for index, (length, cap) in enumerate(zip(lengths, caps, strict=True)):
            unmet = length - cap
            if unmet <= 0:
                continue
            caps[index] += min(unmet, unmet * remaining // unmet_total)
    return caps


def _truncate_to(value: str, cap: int) -> str:
    """Return ``value`` cut to at most ``cap`` chars, marked when it was cut."""
    if len(value) <= cap:
        return value
    marker_len = len(_TRUNCATION_MARKER)
    if cap <= marker_len:
        return value[:cap]
    return value[: cap - marker_len] + _TRUNCATION_MARKER


def _serialize_item_for_prompt(item_fields: Mapping[str, Any], budget: int) -> str:
    """Render the item (header + every section) into at most ``budget`` chars.

    The header (title/status/stage/tags) is rendered first: status/stage are
    tiny by construction (StrEnum members), while title and tags are
    defensively capped at a small fixed size (_TITLE_MAX / _HEADER_TAGS_MAX,
    truncated behind the same marker as an oversized section -- see
    _render_header_value) regardless of what the source row actually
    contains. That holds even for a row that predates
    schemas.MemoryItemCreate/Update's own title/tags bounds, so the header can
    never itself consume the whole budget. The free-text sections then share
    whatever budget remains: an item whose sections fit is rendered verbatim,
    while an oversized one (up to 19 sections x 20k chars = ~380k) is
    truncated section-by-section behind an explicit marker. This keeps the whole
    snapshot bounded so a small-context model is never handed an unusable ~380k
    prompt that would fail every enrich/update as a permanent 502. Deterministic:
    the same item and budget always serialize identically.
    """

    def _header_line(field: str) -> str:
        rendered = _render_header_value(item_fields.get(field), _HEADER_FIELD_CAPS.get(field))
        return f"{field}: {rendered or '(empty)'}"

    header_lines = [_header_line(field) for field in _HEADER_FIELD_ORDER]
    section_values = {
        field: _coerce_str(item_fields.get(field)).strip() for field in SECTION_FIELD_ORDER
    }
    nonempty = [(field, value) for field, value in section_values.items() if value]

    # Charge the fixed scaffolding (header, every section label, "(empty)"
    # placeholders, and all the joining newlines) against the budget first, so
    # what remains is exactly the room available for the section VALUES. Because
    # the final string only adds each value into its already-counted label line,
    # header + labels + sum(values) is the true total -- keeping sum(values) under
    # value_budget keeps the whole output at or under budget.
    scaffold_lines = [*header_lines]
    scaffold_lines += [
        f"{field}: " if value else f"{field}: (empty)" for field, value in section_values.items()
    ]
    value_budget = max(0, budget - len("\n".join(scaffold_lines)))

    caps = _allocate_section_budget([len(value) for _, value in nonempty], value_budget)
    capped = {
        field: _truncate_to(value, cap) for (field, value), cap in zip(nonempty, caps, strict=True)
    }

    lines = [*header_lines]
    for field in SECTION_FIELD_ORDER:
        value = section_values[field]
        lines.append(f"{field}: {capped[field]}" if value else f"{field}: (empty)")
    return "\n".join(lines)


_RULE_SUPERSEDE = (
    "Preserve history: never delete an existing decision or its rationale. "
    "When a decision changes, keep the previous rationale and append a "
    "'superseded' marker noting what replaced it and why."
)


# --- full enrichment -------------------------------------------------------


ENRICH_SYSTEM_PROMPT = "\n".join(
    [
        "You are the Context Memory enrichment assistant. Given an existing "
        "memory item and new context, fill the anti-vaporization checklist "
        "(background, stakeholders, current state, desired outcome, decisions, "
        "rationale, alternatives, constraints, assumptions, risks, evidence, "
        "next actions, recovery cues) so a future reader can recover the full "
        "reasoning.",
        _RULE_HONESTY,
        _RULE_SUPERSEDE,
        _RULE_BULLETS,
        _RULE_LANGUAGE,
        "Only include a section when you are adding to or improving it. Omit "
        "sections you would leave unchanged, and never blank an existing section.",
        "Use exactly these keys (the injected JSON Schema is the binding contract):",
        '- "sections": an object whose keys are any of ['
        + ", ".join(SECTION_FIELD_ORDER)
        + "], each value a string.",
        '- "checklist_complete": true only when the item is materially complete '
        "(boolean). Marking it complete promotes a still-capturing item to the "
        "active status.",
        '- "remaining_gaps": checklist items still missing (array of strings).',
        '- "progress_note": a short note describing what you added (string).',
        "checklist_complete and remaining_gaps are mutually exclusive: if any "
        "checklist gap remains, the item is not complete -- leave "
        "checklist_complete false.",
    ]
)


class EnrichResult(BaseModel):
    """Sanitized enrichment result: whitelisted section updates + metadata.

    ``sections`` carries only the fields the model chose to add or improve, so
    the router merges them onto the item (untouched fields stay as they were).
    """

    model_config = ConfigDict(extra="ignore")

    sections: dict[str, str] = Field(default_factory=dict)
    checklist_complete: bool = False
    remaining_gaps: list[str] = Field(default_factory=list)
    progress_note: str = ""

    @model_validator(mode="before")
    @classmethod
    def _sanitize(cls, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return {
            "sections": _extract_sections(data),
            "checklist_complete": _coerce_bool(data.get("checklist_complete")),
            "remaining_gaps": _clean_str_list(
                data.get("remaining_gaps"), max_items=_MAX_GAPS, item_cap=_PER_SECTION_CAP
            ),
            "progress_note": _clean_text(data.get("progress_note")),
        }

    @model_validator(mode="after")
    def _gaps_block_completion(self) -> Self:
        """Gaps and completion are mutually exclusive.

        A non-empty ``remaining_gaps`` means the checklist is not materially
        complete, so a model that returns both is contradictory. Resolve it
        deterministically here (a prompt rule alone cannot guarantee it): the
        gaps win, forcing ``checklist_complete`` False, so a contradictory result
        never promotes a still-capturing item to active. Forgiving to a sloppy
        model that sets both rather than rejecting the whole enrichment.
        """
        if self.remaining_gaps:
            self.checklist_complete = False
        return self

    @model_validator(mode="after")
    def _require_signal(self) -> Self:
        """Reject an all-empty result as upstream garbage.

        After sanitization an enrichment must carry at least one meaningful
        signal -- a section, a gap, a completion flag, or a progress note --
        otherwise the model returned nothing usable. Raising here yields a
        ValidationError, which ``generate_structured`` retries once and otherwise
        maps to the 502 upstream-error path so the router writes nothing (no
        progress entry, no bumped `updated`).
        """
        if not (
            self.sections or self.remaining_gaps or self.checklist_complete or self.progress_note
        ):
            raise ValueError("enrichment result carries no usable signal")
        return self


def _enrich_user_prompt(item_fields: Mapping[str, Any], additional_context: str) -> str:
    budget = get_settings().llm_prompt_budget_chars
    return "\n\n".join(
        [
            "Existing memory item:",
            _serialize_item_for_prompt(item_fields, budget),
            "New context to integrate:",
            additional_context,
        ]
    )


async def enrich_item(item_fields: Mapping[str, Any], additional_context: str) -> EnrichResult:
    """Run full enrichment: prompt the LLM for a schema-guided, sanitized result."""
    return await generate_structured(
        ENRICH_SYSTEM_PROMPT, _enrich_user_prompt(item_fields, additional_context), EnrichResult
    )


# --- assisted update -------------------------------------------------------


UPDATE_SYSTEM_PROMPT = "\n".join(
    [
        "You are the Context Memory update assistant. Given an existing memory "
        "item and a progress note, refresh the current state, next actions, and "
        "open questions, and record what changed.",
        _RULE_HONESTY,
        _RULE_SUPERSEDE,
        _RULE_BULLETS,
        _RULE_LANGUAGE,
        "Only include a section when you are changing it. Omit unchanged "
        "sections, and never blank an existing section.",
        "Use exactly these keys (the injected JSON Schema is the binding contract):",
        '- "sections": an object whose keys are any of ['
        + ", ".join(SECTION_FIELD_ORDER)
        + "], each value a string.",
        '- "progress_note": a short note describing the update (string).',
    ]
)


class UpdateResult(BaseModel):
    """Sanitized assisted-update result: whitelisted section updates + note."""

    model_config = ConfigDict(extra="ignore")

    sections: dict[str, str] = Field(default_factory=dict)
    progress_note: str = ""

    @model_validator(mode="before")
    @classmethod
    def _sanitize(cls, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return {
            "sections": _extract_sections(data),
            "progress_note": _clean_text(data.get("progress_note")),
        }

    @model_validator(mode="after")
    def _require_signal(self) -> Self:
        """Reject an all-empty result as upstream garbage.

        An assisted update must carry at least one non-empty section or a
        progress note; otherwise the model returned nothing usable. As with
        EnrichResult, the raised ValidationError maps to the 502 path and the
        router writes nothing.
        """
        if not (self.sections or self.progress_note):
            raise ValueError("update result carries no usable signal")
        return self


def _update_user_prompt(item_fields: Mapping[str, Any], note: str) -> str:
    budget = get_settings().llm_prompt_budget_chars
    return "\n\n".join(
        [
            "Existing memory item:",
            _serialize_item_for_prompt(item_fields, budget),
            "Progress note to record:",
            note,
        ]
    )


async def assist_update(item_fields: Mapping[str, Any], note: str) -> UpdateResult:
    """Run assisted update: prompt the LLM for a schema-guided, sanitized result."""
    return await generate_structured(
        UPDATE_SYSTEM_PROMPT, _update_user_prompt(item_fields, note), UpdateResult
    )
