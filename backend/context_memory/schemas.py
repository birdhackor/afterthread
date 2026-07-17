"""Pydantic request/response schemas for the Context Memory API."""

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from context_memory.config import get_settings
from context_memory.models import STALE_ELIGIBLE_STATUSES, MemoryStage, MemoryStatus


def _ensure_aware(value: datetime) -> datetime:
    """Treat naive datetimes (as returned by SQLite) as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# Upper bounds for the durable CRUD fields (title/tags/section text) a client
# can set directly via POST/PATCH. Without them, a legitimate create/update
# can write an item whose title/tags/sections are large enough that, once
# serialized into an enrich/assist-update prompt header
# (context_memory.services.memory_ai._serialize_item_for_prompt, which renders title and
# tags in full ahead of the per-section budgeting), the header alone crowds
# out or exceeds llm_prompt_budget_chars. Declared here -- not on the shared
# MemoryItemContent base -- so only MemoryItemCreate/Update enforce them;
# MemoryItemRead stays unbounded so it can still honestly represent a
# pre-existing row written before these bounds existed (that row is protected
# instead by the defensive per-field cap in _serialize_item_for_prompt).
# title/section caps mirror memory_ai's own _TITLE_MAX / _PER_SECTION_CAP -- a
# human should not be able to create content the AI pipeline could never
# itself produce. Tags are looser here (20 x 100) than the AI sanitizer's
# self-imposed cap (10 x 50): a person may reasonably curate more/longer tags
# than the model is allowed to invent, and _serialize_item_for_prompt caps the
# rendered tags line independently, so this looser bound still cannot blow
# the prompt budget.
_CRUD_TITLE_MAX = 300
_CRUD_MAX_TAGS = 20
_CRUD_TAG_MAX = 100
_CRUD_SECTION_MAX = 20000

# A single CRUD tag: stripped, then bounded to 1..100 chars post-strip, so a
# blank or whitespace-only tag is rejected (422) rather than silently stored.
_CrudTag = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=_CRUD_TAG_MAX)
]


class MemoryItemContent(BaseModel):
    """Durable content fields shared by create and read schemas."""

    title: str
    source: str = "manual"
    confidence: str = "mixed"
    tags: list[str] = Field(default_factory=list)
    status: MemoryStatus = MemoryStatus.capture_quick
    stage: MemoryStage = MemoryStage.quick

    snapshot: str = ""
    why_matters: str = ""
    known: str = ""
    inferred: str = ""
    unknown: str = ""
    decisions: str = ""
    alternatives: str = ""
    rationale: str = ""
    consequences: str = ""
    constraints: str = ""
    assumptions: str = ""
    risks: str = ""
    evidence: str = ""
    open_questions: str = ""
    next_actions: str = ""
    recovery_keywords: str = ""
    recovery_people: str = ""
    recovery_files: str = ""
    resume_trigger: str = ""


class MemoryItemCreate(MemoryItemContent):
    """Payload for creating a memory item. Only ``title`` is mandatory.

    title/tags/every section text field carry explicit upper bounds (see the
    ``_CRUD_*`` constants above) that MemoryItemContent itself does not, so
    MemoryItemRead is unaffected -- this is the source-side half of bounding
    the enrich/assist-update prompt header; the other half is the defensive
    per-field cap in ``memory_ai._serialize_item_for_prompt``.
    """

    title: str = Field(max_length=_CRUD_TITLE_MAX)
    tags: list[_CrudTag] = Field(default_factory=list, max_length=_CRUD_MAX_TAGS)

    snapshot: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    why_matters: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    known: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    inferred: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    unknown: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    decisions: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    alternatives: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    rationale: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    consequences: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    constraints: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    assumptions: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    risks: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    evidence: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    open_questions: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    next_actions: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    recovery_keywords: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    recovery_people: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    recovery_files: str = Field(default="", max_length=_CRUD_SECTION_MAX)
    resume_trigger: str = Field(default="", max_length=_CRUD_SECTION_MAX)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be empty")
        return stripped


class MemoryItemUpdate(BaseModel):
    """Partial update payload; every field is optional via omission.

    Every field is typed as its ordinary, non-nullable type with a
    `default_factory` that is never actually applied: the router
    (`update_item`) always calls `model_dump(exclude_unset=True)`, so an
    omitted field's default is never read or written, and pydantic v2 does
    not validate defaults unless `validate_default` is set (it isn't here).
    Using `default_factory` rather than a literal default is deliberate:
    pydantic v2 omits `default_factory`-sourced values from the generated
    JSON schema entirely, so every property below is optional but carries
    neither a `default` key nor `null` in its type. A literal default (e.g.
    `title: str = ""`) would instead surface as `"default": ""` in the
    OpenAPI schema; a client that honestly materialises schema defaults
    would then send `title=""` on every PATCH, tripping the not-blank
    validator or silently resetting fields the caller never meant to touch.
    The payoff is that the generated OpenAPI schema advertises each field's
    real type -- not nullable, no phantom default -- so an explicit JSON
    `null`, which no field here legitimately accepts, now fails ordinary
    type validation (422) instead of requiring a bespoke validator to catch
    it after the fact.
    """

    title: str = Field(default_factory=str, max_length=_CRUD_TITLE_MAX)
    source: str = Field(default_factory=str)
    confidence: str = Field(default_factory=str)
    tags: list[_CrudTag] = Field(default_factory=list, max_length=_CRUD_MAX_TAGS)
    status: MemoryStatus = Field(default_factory=lambda: MemoryStatus.capture_quick)
    stage: MemoryStage = Field(default_factory=lambda: MemoryStage.quick)

    snapshot: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    why_matters: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    known: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    inferred: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    unknown: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    decisions: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    alternatives: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    rationale: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    consequences: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    constraints: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    assumptions: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    risks: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    evidence: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    open_questions: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    next_actions: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    recovery_keywords: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    recovery_people: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    recovery_files: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)
    resume_trigger: str = Field(default_factory=str, max_length=_CRUD_SECTION_MAX)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        # Only runs when the client actually sends `title`; an omitted title
        # keeps its never-applied default and this validator is skipped.
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be empty")
        return stripped


class MemoryItemRead(MemoryItemContent):
    """A memory item as returned by the API, including derived staleness."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created: datetime
    updated: datetime

    @field_validator("created", "updated", mode="before")
    @classmethod
    def _coerce_aware(cls, value: datetime) -> datetime:
        return _ensure_aware(value)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_stale(self) -> bool:
        """True for a stale-eligible (non-terminal) item whose `updated` is
        past the configured threshold; always False for terminal items.

        Stale-eligibility is the shared models.STALE_ELIGIBLE_STATUSES set
        (all five non-terminal statuses), not a schema-local subset.
        """
        if self.status not in STALE_ELIGIBLE_STATUSES:
            return False
        threshold = timedelta(days=get_settings().stale_after_days)
        return datetime.now(UTC) - self.updated > threshold


class ProgressEntryRead(BaseModel):
    """A single progress-log entry."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    item_id: int
    date: datetime
    note: str

    @field_validator("date", mode="before")
    @classmethod
    def _coerce_aware(cls, value: datetime) -> datetime:
        return _ensure_aware(value)


class MemoryItemReadWithProgress(MemoryItemRead):
    """A memory item plus its full progress history (oldest first)."""

    progress: list[ProgressEntryRead] = Field(default_factory=list)


class ProgressEntryCreate(BaseModel):
    """Payload for appending a progress-log entry."""

    note: str = Field(max_length=_CRUD_SECTION_MAX)

    @field_validator("note")
    @classmethod
    def _note_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("note must not be empty")
        return stripped


class ItemListResponse(BaseModel):
    """Paginated list of memory items."""

    items: list[MemoryItemRead]
    total: int


class ReviewResponse(BaseModel):
    """Disjoint groups of items that need attention, oldest first per group."""

    needs_enrichment: list[MemoryItemRead]
    active: list[MemoryItemRead]
    waiting: list[MemoryItemRead]
    parked: list[MemoryItemRead]


# Upper bound on every AI free-text request field. Stripped-non-empty is
# enforced per field below; the length is validated on the raw input (a 20001
# character body is 422) so an oversized payload is rejected before any LLM
# call. Mirrors the per-section output cap in context_memory.services.memory_ai.
_MAX_AI_INPUT_CHARS = 20000


def _stripped_non_empty(value: str, field: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} must not be empty")
    return stripped


class LLMStatus(BaseModel):
    """Whether an LLM endpoint is configured, and the model name only.

    Deliberately excludes the base URL and API key -- only the boolean and the
    non-secret model name are surfaced. ``model`` is genuinely null exactly
    when unconfigured, so its nullability is honest rather than a schema
    artifact.
    """

    configured: bool
    model: str | None


class CaptureRequest(BaseModel):
    """Payload for AI quick capture: the raw discussion text to structure."""

    raw_text: str = Field(min_length=1, max_length=_MAX_AI_INPUT_CHARS)

    @field_validator("raw_text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _stripped_non_empty(value, "raw_text")


class CaptureResponse(BaseModel):
    """AI quick-capture result: the created item plus follow-up questions."""

    item: MemoryItemRead
    questions: list[str]


class EnrichRequest(BaseModel):
    """Payload for AI full enrichment: new context to integrate."""

    additional_context: str = Field(min_length=1, max_length=_MAX_AI_INPUT_CHARS)

    @field_validator("additional_context")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _stripped_non_empty(value, "additional_context")


class EnrichResponse(BaseModel):
    """AI enrichment result: the updated item plus remaining checklist gaps."""

    item: MemoryItemRead
    gaps: list[str]


class AssistUpdateRequest(BaseModel):
    """Payload for AI assisted update: a progress note to record."""

    note: str = Field(min_length=1, max_length=_MAX_AI_INPUT_CHARS)

    @field_validator("note")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _stripped_non_empty(value, "note")


class AssistUpdateResponse(BaseModel):
    """AI assisted-update result: the updated item."""

    item: MemoryItemRead


# --- LLM interaction log (see context_memory.services.llm_log) --------------
#
# These mirror the dicts llm_log.list_summaries / get_record return, so the
# "AI 日誌" page has a typed, documented contract. Deliberately WITHOUT any
# base-URL/key field: a record only ever carries the non-secret model name,
# non-secret timing/outcome scalars, and the prompt/response bodies (which are
# user content, not endpoint config). Each token count is nullable because a
# merely-compatible gateway may omit or malform usage. ``LlmLogBase.usage`` is
# the INTERACTION-level total -- the per-field SUM across every attempt that
# reported usage (see llm_log._aggregate_usage) -- while ``LlmLogAttempt.usage``
# below is that SAME attempt's own, unaggregated reading; a corrective retry's
# interaction-level total is therefore its attempts' SUM, not its last
# attempt's number. Request/response bodies are the STORED form (UTF-8-safe,
# then size-capped at llm_log_body_max_chars -- see llm_log._stored_body), and
# ``LlmLogAttempt.truncated`` says whether that capping actually cut anything
# on that attempt.


class LlmLogUsage(BaseModel):
    """Token usage read defensively from a completion; any field may be null."""

    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


class LlmLogBase(BaseModel):
    """Fields shared by the list-summary and full-detail log views."""

    id: int
    workflow: str
    model: str
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    outcome: str | None
    error: str | None
    usage: LlmLogUsage | None


class LlmLogSummary(LlmLogBase):
    """One log row WITHOUT attempt bodies: ``attempts`` is the count only.

    The list view shows how many attempts a call took; the full bodies (tens of
    KB each) load only on row expansion via the detail endpoint.
    """

    attempts: int


class LlmLogListResponse(BaseModel):
    """Newest-first page of LLM interaction summaries."""

    logs: list[LlmLogSummary]


class LlmLogMessage(BaseModel):
    """One request message exactly as sent (role + full content)."""

    role: str
    content: str


class LlmLogAttempt(BaseModel):
    """One request/response round: the full messages sent, the raw reply (or
    null), this attempt's OWN token usage, and a SAFE failure category (or
    null) if that attempt failed.

    ``request_chars``/``response_chars`` are the STORED (post ``_stored_body``:
    UTF-8-safe, then size-capped) lengths, not the original size --
    ``response_chars`` is null exactly when ``response_content`` is (no
    response ever landed on this attempt), never 0 for that case. ``usage`` is
    THIS attempt's own reading, distinct from ``LlmLogBase.usage``'s
    interaction-level aggregate (see the module comment above). ``truncated``
    is true the moment ANY body on this attempt -- a request message or the
    response -- was cut for size; the FE shows a small badge for it.
    """

    request_messages: list[LlmLogMessage]
    request_chars: int
    response_content: str | None
    response_chars: int | None
    error: str | None
    usage: LlmLogUsage | None
    truncated: bool


class LlmLogDetail(LlmLogBase):
    """A full log record: every attempt's request messages and response body."""

    attempts: list[LlmLogAttempt]


# --- tools + installer (see context_memory.services.tools / tool_builder) ----
#
# These mirror the dicts tools.list_tools / tool_builder.get_job return. Like
# the LLM log schemas above, they deliberately carry NO secret-shaped field:
# a tool row is name/description/flags, and a job row is states, safe error
# text, and the AI 日誌 link id -- a tool's own .env values never appear in
# either service's output in the first place.


class ToolSummary(BaseModel):
    """One installed tool package as the 工具 page lists it.

    ``valid=False`` rows carry the safe ``error`` reason from the registry scan
    (bad manifest, name mismatch, missing entry file); such a package is listed
    so it can be deleted, but is never advertised to the model or executable.
    """

    name: str
    description: str
    enabled: bool
    valid: bool
    error: str | None


class ToolListResponse(BaseModel):
    """Every installed tool package, in name order."""

    tools: list[ToolSummary]


class ToolUpdateRequest(BaseModel):
    """PATCH payload for a tool: only the enabled toggle is mutable."""

    enabled: bool


# An install-form secret NAME must be a valid environment-variable name: it
# becomes one, both in the run_shell live-test env and in the finished tool's
# .env (see context_memory.services.tool_builder). Uppercase-first, then
# uppercase/digit/underscore, <=64 chars total -- the conventional env-var shape.
_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# zh-TW 422 messages for the install-form secret pair (rendered by the 工具
# page; pinned by tests). Fullwidth punctuation is authentic zh-TW typography.
_SECRET_PAIR_INCOMPLETE = "秘密名稱與秘密值必須同時提供，或同時留空"  # noqa: RUF001
_SECRET_NAME_INVALID = "秘密名稱須以大寫字母開頭，僅能包含大寫字母、數字與底線，且不超過 64 字"  # noqa: RUF001
_SECRET_VALUE_MULTILINE = "秘密值不可包含換行字元"


class ToolInstallRequest(BaseModel):
    """Payload for the web installer: where the API lives, and what to build.

    ``openapi_url`` is a pydantic HttpUrl, so a non-http(s) scheme or a
    hostless string is a 422 before any job starts. ``instructions`` shares the
    AI input bound every other AI free-text field carries (20000 chars,
    stripped-non-empty).

    ``secret_name``/``secret_value`` are the OPTIONAL install-form secret (D36):
    a credential the user would otherwise paste into ``instructions`` (where it
    is recorded verbatim into the builder's AI-log prompts). Supplied HERE, the
    VALUE never enters the LLM conversation at all -- the backend injects it into
    run_shell's env so the builder can live-test the real API, and writes it into
    the finished tool's ``.env`` at install time (see tool_builder); llm_log
    redacts it besides. Both-or-neither, and the name must be a valid env-var
    name (it becomes one). ``secret_value`` is NEVER echoed back in any response
    (this is a request-only model, and no response schema carries the field).
    """

    openapi_url: HttpUrl
    instructions: str = Field(min_length=1, max_length=_MAX_AI_INPUT_CHARS)
    secret_name: str | None = None
    secret_value: str | None = None

    @field_validator("instructions")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _stripped_non_empty(value, "instructions")

    @model_validator(mode="after")
    def _validate_secret_pair(self) -> ToolInstallRequest:
        """Enforce both-or-neither + a valid env-var name, and normalize.

        Runs AFTER field validation so both raw values are present together (the
        pair is inherently cross-field). A half-supplied pair, an invalid name,
        or a multiline value each raises ValueError -> 422 with the zh-TW message
        above. On success the stripped forms are written back (or None when
        absent), so every downstream consumer sees a clean pair and never a stray
        ``""``/whitespace-only value -- and a newline in the value is rejected up
        front because it would otherwise corrupt the single-line ``KEY=VALUE``
        ``.env`` entry the value becomes.
        """
        name = (self.secret_name or "").strip()
        value = (self.secret_value or "").strip()
        if bool(name) != bool(value):
            raise ValueError(_SECRET_PAIR_INCOMPLETE)
        if name and not _SECRET_NAME_RE.match(name):
            raise ValueError(_SECRET_NAME_INVALID)
        if value and ("\n" in value or "\r" in value):
            raise ValueError(_SECRET_VALUE_MULTILINE)
        self.secret_name = name or None
        self.secret_value = value or None
        return self


class ToolInstallAccepted(BaseModel):
    """202 body for a queued install: the id to poll."""

    job_id: str


class ToolInstallJobStatus(BaseModel):
    """One install job's visible state, as polled by the 工具 page.

    ``state`` walks queued -> running -> succeeded | failed. ``error`` is the
    friendly zh-TW failure text (failed only); ``tool_name``/``summary`` are
    what the builder reported; ``llm_log_id`` links to the builder session's
    AI 日誌 record whenever a session actually ran, so both success and failure
    are debuggable from the UI. Jobs are process-local and unpersisted: after a
    backend restart every previous job id is a 404.
    """

    job_id: str
    state: str
    created_at: str
    finished_at: str | None
    error: str | None
    tool_name: str | None
    summary: str | None
    llm_log_id: int | None
