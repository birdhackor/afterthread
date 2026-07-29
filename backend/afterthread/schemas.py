"""Pydantic request/response schemas for the afterthread API."""

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

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

from afterthread.config import get_settings
from afterthread.models import STALE_ELIGIBLE_STATUSES, MemoryStage, MemoryStatus


def _ensure_aware(value: datetime) -> datetime:
    """Treat naive datetimes (as returned by SQLite) as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# Upper bounds for the durable CRUD fields (title/tags/section text) a client
# can set directly via POST/PATCH. Without them, a legitimate create/update
# can write an item whose title/tags/sections are large enough that, once
# serialized into an enrich/assist-update prompt header
# (afterthread.services.memory_ai._serialize_item_for_prompt, which renders title and
# tags in full ahead of the per-section budgeting), the header alone crowds
# out or exceeds the prompt's char allowance (llm_prompt_budget_tokens divided by
# the live chars<->tokens ratio). Declared here -- not on the shared
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
# call. Mirrors the per-section output cap in afterthread.services.memory_ai.
_MAX_AI_INPUT_CHARS = 20000


def _stripped_non_empty(value: str, field: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} must not be empty")
    return stripped


class LLMTokenRatio(BaseModel):
    """The live char<->token ratio estimator's public view (P4).

    Surfaced on ``LLMStatus.token_ratio`` from
    afterthread.services.token_budget.snapshot(). ``samples`` is how many
    recent (chars, tokens) observations the rolling window holds; and
    ``tokens_per_char`` is the learned ratio rounded to 4dp, genuinely null while
    the window is still below the minimum-sample threshold (the cold-start default
    is in force and there is no observed ratio yet), so its nullability is honest.
    Carries no config value -- purely the internal estimator state.
    """

    samples: int
    tokens_per_char: float | None


class LLMStatus(BaseModel):
    """Whether an LLM endpoint is configured, the model name, and the ratio state.

    Deliberately excludes the base URL and API key -- only the boolean, the
    non-secret model name, and the char<->token ratio estimator's state are
    surfaced. ``model`` is genuinely null exactly when unconfigured, so its
    nullability is honest rather than a schema artifact. ``token_ratio`` is always
    present (the estimator always has a state to report, even cold-started).
    """

    configured: bool
    model: str | None
    token_ratio: LLMTokenRatio


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


# --- LLM interaction log (see afterthread.services.llm_log) --------------
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
    """Newest-first page of LLM interaction summaries.

    ``process_token`` is the ANSWERING process's opaque identity for the id space
    every ``id`` on this page belongs to (``llm_log.process_token()``). Log ids
    are a per-process counter over a ring that dies with the process, so an id a
    client is HOLDING -- a ``?log=<id>`` deep link built from a job response the
    browser cached before a restart -- may name a completely different
    interaction here. Only the minting process's token can tell those apart, and
    this is the reading the AI 日誌 page compares a link's claim against.

    It rides on the ENVELOPE rather than on the rows: it is a property of the
    process that answered, not of any record, so ``LlmLogBase`` (shared by the
    row and the detail, one entry per RECORD) would be both the wrong shape and
    50 copies of one string. The list is also the response that arrives BEFORE
    the page decides what a deep link points at, while the detail is fetched
    lazily per record -- an in-list link would have had to open the row first to
    learn whether it should have.
    """

    logs: list[LlmLogSummary]
    process_token: str


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
    is true the moment ANY stored text on this attempt -- a request message, the
    response, or an advertised tool name -- was cut for size; the FE shows a
    small badge for it.
    ``tools_advertised`` is the NAMES of the tools this attempt offered the
    model, or null when it sent no ``tools`` parameter at all (see
    ``LlmAttempt`` in services/llm_log.py) -- declaring it here is what keeps it
    on the wire, since pydantic's default ``extra="ignore"`` would otherwise
    drop the key silently at the router's ``model_validate``. Like every other
    stored text here it has been through the log's redaction choke point, since
    a tool NAME can itself equal a registered secret value.
    """

    request_messages: list[LlmLogMessage]
    request_chars: int
    response_content: str | None
    response_chars: int | None
    error: str | None
    usage: LlmLogUsage | None
    truncated: bool
    tools_advertised: list[str] | None = None


class LlmLogDetail(LlmLogBase):
    """A full log record: every attempt's request messages and response body."""

    attempts: list[LlmLogAttempt]


# --- tools + installer (see afterthread.services.tools / tool_builder) ----
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

    The summary text itself is fetched per tool on demand
    (``ToolSummaryDetail``), since it is far too long for a list row.
    """

    name: str
    description: str | None
    enabled: bool
    valid: bool
    error: str | None
    current_vid: str | None
    lineage: Literal["sole", "usable", "broken"]


class ToolListResponse(BaseModel):
    """Every installed tool package, in name order."""

    tools: list[ToolSummary]


class ToolDeleteResponse(BaseModel):
    """Whether DELETE removed the files or retained a hidden parked tree.

    ``retained_path`` is the exact operator-facing path when ``outcome`` is
    ``retained``; it is null after physical removal.
    """

    outcome: Literal["removed", "retained"]
    retained_path: str | None
    retention_reason: Literal["durability_unconfirmed", "cleanup_failed"] | None


class ToolDiscardResponse(BaseModel):
    """Whether discard removed the former version or retained its files."""

    outcome: Literal["removed", "retained"]
    retained_path: str | None
    retention_reason: Literal["durability_unconfirmed", "cleanup_failed"] | None


class ToolSummaryDetail(BaseModel):
    """One tool's version summary
    (``versions/<vid>/.afterthread.meta/summary.json``), as the 工具 page reads it.

    The three sidecar fields are nullable and all three are null together for the
    common, non-exceptional case of a tool with no sidecar: a hand-made package,
    or one whose summary generation has not run (or failed) yet. That is a 200,
    not a 404 -- the TOOL exists, it just has no summary -- so the page renders
    尚無總結 plus a 重新產生 action rather than an error. ``current_vid`` is
    always present and identifies the version that was actually read, so a
    response that crossed a pointer change cannot poison another version's cache.

    ``llm_log_id`` links to the summary session's AI 日誌 record (the
    ``tool_summary`` workflow, distinct from the builder's ``tool_install``), so
    a wrong or missing summary is debuggable from the UI. It is null whenever the
    id on disk was minted by a PREVIOUS process: log ids are a per-process counter
    over a ring that is wiped on restart, so a persisted id only means something
    while that process lives (see ``routers.tools._summary_detail``, which nulls
    it rather than adding a fifth field -- a null here already means "no record to
    link", and the FE already renders exactly that).
    """

    summary: str | None
    updated_at: str | None
    llm_log_id: int | None
    current_vid: str


class ToolUpdateRequest(BaseModel):
    """PATCH payload for a tool: only the enabled toggle is mutable."""

    enabled: bool


_TOOL_VID_PATTERN = r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$"


class ToolExpectedVersionRequest(BaseModel):
    """Required optimistic identity for a version-specific synchronous write."""

    expected_vid: str = Field(pattern=_TOOL_VID_PATTERN)


# An install-form secret NAME must be a valid environment-variable name: it
# becomes one, both in the run_shell live-test env and in the finished tool's
# .env (see afterthread.services.tool_builder). Uppercase-first, then
# uppercase/digit/underscore, <=64 chars total -- the conventional env-var shape.
_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# The redactor (llm_log._redact / tools.redact_known_secrets) SKIPS values shorter
# than 6 chars -- masking a 1-5 char value would shred ordinary prose. Accepting a
# shorter secret would therefore create one that can NEVER be masked out of the AI
# 日誌 or a live tool result, so the schema rejects it up front. Mirrors
# tools._MIN_SECRET_LEN, kept a plain literal here rather than importing the tool
# subsystem into the schema layer.
_SECRET_VALUE_MIN_LEN = 6

# zh-TW 422 messages for the install-form secret pair (rendered by the 工具
# page; pinned by tests). Fullwidth punctuation is authentic zh-TW typography.
_SECRET_PAIR_INCOMPLETE = "秘密名稱與秘密值必須同時提供，或同時留空"  # noqa: RUF001
_SECRET_NAME_INVALID = "秘密名稱須以大寫字母開頭，僅能包含大寫字母、數字與底線，且不超過 64 字"  # noqa: RUF001
_SECRET_VALUE_MULTILINE = "秘密值不可包含換行字元"
_SECRET_VALUE_TOO_SHORT = "秘密值長度至少 6 字元"


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
        pair is inherently cross-field). A half-supplied pair, an invalid name, a
        multiline value, or a too-short value each raises ValueError -> 422 with the
        zh-TW message above. On success the stripped forms are written back (or None
        when absent), so every downstream consumer sees a clean pair and never a stray
        ``""``/whitespace-only value -- and a newline in the value is rejected up
        front because it would otherwise corrupt the single-line ``KEY=VALUE``
        ``.env`` entry the value becomes.

        Stripping the value's surrounding whitespace and validating the RESULT (rather
        than 422-ing on a trailing newline) is the DELIBERATE contract, not an
        oversight: it absorbs clipboard artifacts, and normalizing ONCE here means
        every downstream consumer -- the run_shell live-test env, the ``.env`` write,
        and the redaction registry -- sees the byte-identical value; only INTERIOR
        newlines still 422 (they alone would break the single-line ``.env`` entry).

        The >= 6-char floor mirrors the redactor's own skip threshold
        (tools._MIN_SECRET_LEN): a shorter value could never be masked out of the AI
        日誌 / live tool results, so accepting one would create an unredactable secret.
        """
        name = (self.secret_name or "").strip()
        value = (self.secret_value or "").strip()
        if bool(name) != bool(value):
            raise ValueError(_SECRET_PAIR_INCOMPLETE)
        if name and not _SECRET_NAME_RE.match(name):
            raise ValueError(_SECRET_NAME_INVALID)
        if value and ("\n" in value or "\r" in value):
            raise ValueError(_SECRET_VALUE_MULTILINE)
        if value and len(value) < _SECRET_VALUE_MIN_LEN:
            raise ValueError(_SECRET_VALUE_TOO_SHORT)
        self.secret_name = name or None
        self.secret_value = value or None
        return self


class ToolReviseRequest(BaseModel):
    """Payload for an AI revise job (D40): what the user wants changed.

    ``feedback`` carries the SAME bound every other AI free-text input has
    (20000 chars, stripped-non-empty) and becomes the builder session's user
    turn. ``expected_vid`` is the required optimistic identity checked only
    after global single-flight admission. Which tool is being revised remains
    the PATH's job: the shared path regex validates its name.
    """

    feedback: str = Field(min_length=1, max_length=_MAX_AI_INPUT_CHARS)
    expected_vid: str = Field(pattern=_TOOL_VID_PATTERN)

    @field_validator("feedback")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _stripped_non_empty(value, "feedback")


class ToolInstallAccepted(BaseModel):
    """202 body for a queued install or revise: the id to poll."""

    job_id: str


class ToolJobStatus(BaseModel):
    """One install/revise job's visible state, as polled by the 工具 page.

    ``state`` walks queued -> running -> succeeded | failed. ``error`` is the
    friendly zh-TW failure text (failed only); ``tool_name``/``summary`` are
    what the builder reported; ``llm_log_id`` links to the builder session's
    AI 日誌 record whenever a session actually ran, so both success and failure
    are debuggable from the UI. Jobs are process-local and unpersisted: after a
    backend restart every previous job id is a 404.

    ``llm_log_process`` names the process whose id space that ``llm_log_id``
    belongs to -- the same both-or-neither pairing the sidecar keeps on disk
    (``tools.store_summary_meta``), for the same reason: log ids restart from
    zero in every process, so an id that OUTLIVES its process resolves to
    whatever now occupies the number. A job never outlives its process (the 404
    above), but a browser TAB does: the 工具 page stops polling a terminal job
    and keeps its response cached, so the outcome card -- and the ``查看 AI 日誌``
    link on it -- can still be on screen after a restart. The token travels with
    the link so the AI 日誌 page can refuse to resolve it (R9-1); the backend
    cannot catch this one alone, because it never SERVES a stale id.

    One model for BOTH job kinds (D40 renamed it from ``ToolInstallJobStatus``
    without touching a field): an install writes a package into the tools
    directory and a revise adds a version to one and moves ``current``, and both
    report through one job table and one poll endpoint. ``env_keys`` names
    assignments from a builder-written ``.env`` that the backend stripped;
    values never enter the result.
    """

    job_id: str
    state: str
    created_at: str
    finished_at: str | None
    error: str | None
    tool_name: str | None
    summary: str | None
    llm_log_id: int | None
    llm_log_process: str | None
    env_keys: list[str]
