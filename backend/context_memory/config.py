"""Application settings, loaded from a CWD-relative `.env`.

Dev runs from `backend/`, so that `.env` is `backend/.env`. Packaged mode
(see `context_memory/cli.py`) chdirs into the data dir and loads
`<data-dir>/.env` into the process environment before this module is ever
imported, so the CWD-relative lookup below lands on that same data-dir file
-- and since every value in it is already in the environment, which
pydantic-settings gives precedence over any env file, the lookup can only
ever agree with what cli.py loaded. See D06 in docs/web-v2-decisions.md.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the Context Memory backend."""

    # CWD-relative, not `Path(__file__)`-relative: running dev from `backend/`
    # (this repo's documented workflow) is byte-identical to the old
    # behavior, since `backend/` is already the CWD. The old __file__-relative
    # path broke once this package could be installed as a wheel: it resolved
    # into site-packages, where a user's `.env` never lives, so a packaged
    # install could never see it no matter what the user set. Packaged-mode
    # config loading is `cli.py`'s job instead -- it chdirs into the data dir
    # (making this CWD-relative lookup point at `<data-dir>/.env`) and loads
    # that same file into the process environment (without overriding real
    # env vars) before this class is ever instantiated; see D06 in
    # docs/web-v2-decisions.md.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # OpenAI-compatible endpoint. Intentionally left empty here; the user
    # will fill in the real URL later via backend/.env. Never invent one.
    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = ""

    # Per-request timeout (seconds) for the OpenAI-compatible endpoint, passed
    # straight to AsyncOpenAI(timeout=...) in context_memory/services/llm.py. Bounded at
    # construction (0 < value <= 1800) so a nonsensical override -- a zero/
    # negative timeout the SDK would reject, or an absurdly large one that
    # would hang a request for hours -- fails loudly at startup via
    # pydantic-settings, rather than surfacing only when the first AI call is
    # made. This is ALSO the end-to-end wall-clock budget for a call plus its
    # one corrective retry (the asyncio.timeout around the whole attempt loop in
    # generate_structured), not merely a per-connection inactivity timeout. The
    # default is 120s and the ceiling 1800s (30 min): a long-context model like
    # GLM5.2 generating against a large prompt is legitimately slow, and the old
    # 60s/600s bounds were calibrated for a small-context model that answered in
    # seconds. Still deliberately bounded so a hung endpoint cannot pin a request
    # (and its connection) open indefinitely.
    openai_timeout_seconds: float = Field(default=120, gt=0, le=1800)

    # Optional hard cap on completion length, sent as `max_tokens` on the
    # chat.completions.create call in context_memory/services/llm.py ONLY when set.
    # None (the default) means the parameter is omitted entirely, preserving the
    # historical behavior byte-for-byte: some reasoning-style endpoints reject an
    # explicit max_tokens outright (turning every call into a 400 -> 502), so the
    # safe default is to send nothing and let the endpoint apply its own limit.
    # It exists because the opposite failure mode is just as real: some gateways
    # default to a SMALL completion cap (e.g. a few hundred tokens) that silently
    # truncates a long structured reply into invalid JSON -- for those, this is
    # the operator's knob to raise the ceiling. Bounded to [1, 1_000_000] so a
    # zero/negative value (which the SDK would reject) fails at startup instead
    # of on the first AI call.
    openai_max_output_tokens: int | None = Field(default=None, gt=0, le=1_000_000)

    # Hard ceiling on the number of characters the serialized item snapshot may
    # occupy in an enrich / assist-update prompt (see
    # context_memory/services/memory_ai.py). Without it, an item with 19 sections of up to
    # 20k characters each serializes to a ~380k-character prompt; on a
    # small-context model that is a permanent 502 (rejected on every call) rather
    # than a config error. Bounded to [4000, 2_000_000] (startup-validated via
    # pydantic-settings, matching the openai_timeout_seconds / stale_after_days
    # convention): below 4k the header alone crowds out the sections. The upper
    # bound and the default (200000) are sized for a large-context model such as
    # GLM5.2 (1M-token context): the practical worst-case item serializes to
    # ~500k chars (<=19 sections x 20k + 60k history), so 200k already lets the
    # vast majority of items through whole, and an operator with unusually large
    # items can raise it toward the 2M ceiling. char != token, but the budget is
    # deliberately measured in chars (no tokenizer dependency): for CJK text the
    # ratio is roughly 1 char per token, the least forgiving case, so a char
    # budget under a 1M-token context window stays comfortably within it.
    llm_prompt_budget_chars: int = Field(default=200000, ge=4000, le=2_000_000)

    # Bounded in-memory ring of the most recent LLM interaction records (see
    # context_memory/services/llm_log.py): each carries the workflow, model,
    # timing, outcome, token usage and the FULL prompt/response bodies of every
    # attempt, surfaced via GET /api/llm/logs for the "AI 日誌" page. The ring is
    # process-wide and dies with the process -- it holds personal memory content
    # (prompts and completions), so it is deliberately memory-only unless the
    # operator opts into the file sink below. Bounded to [1, 1000]: together
    # with llm_log_body_max_chars below (which caps each STORED body's own
    # size, independent of this count), a full ring of maximal records is
    # bounded RAM on both axes, and these two caps are what keep it so.
    llm_log_max_entries: int = Field(default=50, ge=1, le=1000)

    # Hard ceiling on how many characters ANY single stored request-message or
    # response body may occupy (see context_memory/services/llm_log.py's
    # _stored_body -- applied UTF-8-safe first, then cut, with a truncation
    # marker appended). This bounds RAM the same way llm_log_max_entries does,
    # but on the ORTHOGONAL axis: max_entries caps how many interactions the
    # ring holds, this caps how large any ONE body within an entry may be.
    # Without it, a broken or hostile OpenAI-*compatible* gateway returning a
    # multi-MB body -- kept per attempt, and echoed into a corrective retry's
    # OWN next request, compounding the size across attempts -- could inflate
    # a "bounded" 50-entry ring to hundreds of MB from a single pathological
    # interaction. The default (200000) deliberately matches the SAME scale as
    # llm_prompt_budget_chars's own default: an ordinary interaction's prompt
    # is already budget-capped to roughly that size before it is ever sent, so
    # a normal request/response pair is never truncated by this independent
    # cap -- only a genuinely oversized body (an echoed prior reply plus an
    # oversized new one, or a misbehaving endpoint) is. Bounded to
    # [1000, 2_000_000] (startup-validated via pydantic-settings, matching the
    # llm_prompt_budget_chars convention): the floor keeps even a deliberately
    # small override from truncating virtually every legitimate reply, and the
    # ceiling matches llm_prompt_budget_chars's own ceiling since there is no
    # reason this cap would ever need to exceed the prompt budget it is
    # protecting bodies of the same scale as.
    llm_log_body_max_chars: int = Field(default=200_000, ge=1_000, le=2_000_000)

    # Optional path to a JSONL file that every finished LLM interaction record is
    # appended to (one JSON object per line, full bodies, ensure_ascii=False).
    # Empty (the default) disables the file sink entirely -- landing records on
    # disk is a PRIVACY decision, since they carry personal memory content that
    # the memory-only ring above never persists, so it must be opt-in. A write
    # failure never breaks the LLM call (it is caught and dropped with a one-line
    # WARNING); the operator-chosen filename is not a secret and may appear in
    # that warning, unlike openai_base_url / openai_api_key which never do.
    llm_log_file: str = ""

    # Rotation threshold (BYTES) for the optional JSONL sink above (see
    # context_memory/services/llm_log.py's _write_file_sink -> _rotate_file_sink,
    # D34). Before each append the sink stats the file; once it exceeds this cap
    # the current file is renamed aside with a UTC-timestamp suffix and a fresh
    # one is started, so the sink can never grow one file without bound. This
    # guards the DISK only -- RAM is the ring's job (llm_log_max_entries +
    # llm_log_body_max_chars), a separate axis entirely -- and it is DELIBERATELY
    # only relevant when llm_log_file is set (the sink is opt-in). No
    # retention/deletion of the rotated segments is done: pruning old files is the
    # operator's call, not ours. Bounded to [1_000_000, 1_000_000_000]
    # (startup-validated via pydantic-settings, matching the other llm_log knobs'
    # convention): the 1MB floor keeps a deliberately tiny override from rotating
    # on essentially every write, and the 1GB ceiling keeps one segment's disk
    # footprint sane. Default 50MB holds a long debugging session's worth of
    # records before the first rotation.
    llm_log_file_max_bytes: int = Field(default=50_000_000, ge=1_000_000, le=1_000_000_000)

    # Hard ceiling on how many TOOL ROUNDS a single ``generate_structured`` call
    # may take before it is forced to produce its final JSON (see
    # context_memory/services/llm.py). One round == one create() whose reply
    # carried tool_calls that were executed and fed back. Once this many rounds
    # have run the loop stops advertising tools, nudges the model to finalize,
    # and makes ONE last tools-free completion -- so a model that gets stuck
    # calling tools forever cannot spin the interaction (each round is a real
    # upstream round-trip, and its latency/cost is charged to the one request).
    # Bounded to [1, 64] (startup-validated via pydantic-settings, matching the
    # convention of the other LLM knobs): the floor guarantees at least one
    # genuine tool round is possible, and the ceiling keeps even a hostile
    # settings override from turning one interaction into 64+ upstream calls.
    # Default 8 gives an agent room to look up several internal terms in one
    # pass while staying well short of the ceiling; the KB installer (Phase 5c)
    # overrides it (and the timeout) with a much larger budget of its own.
    llm_tool_rounds_max: int = Field(default=8, ge=1, le=64)

    # Directory holding installed tool packages, one per subdirectory
    # (``<tools_dir>/<name>/`` with a tool.json + implementation files + an
    # optional .env), scanned by context_memory/services/tools.py. Empty (the
    # default) turns the whole tool feature OFF: list_tools() returns [],
    # enabled_llm_tools() returns [], the three AI workflows pass NO tools, and
    # their prompts stay byte-identical to the tool-less build (so the pinned
    # prompt tests and the e2e mock's marker routing are untouched). Packaged
    # mode's cli.py injects ``<data-dir>/tools`` when TOOLS_DIR is unset --
    # mirroring the DATABASE_URL default-injection pattern -- so a uvx install
    # gets a stable per-user tools location without the user configuring one;
    # dev opts in explicitly via backend/.env. No env prefix is configured (see
    # model_config), so the environment variable name is exactly ``TOOLS_DIR``.
    tools_dir: str = ""

    # Wall-clock budget (seconds) for a SINGLE tool subprocess invocation (see
    # context_memory/services/tools.py). On expiry the tool's whole process
    # GROUP is killed, so a hung or runaway tool cannot pin the interaction that
    # called it. This is PER tool call, nested inside the interaction-level
    # asyncio.timeout in generate_structured -- a tool round that overruns is
    # reported back to the model as a failed tool result, not a crash. Bounded
    # to (0, 600] (startup-validated, matching openai_timeout_seconds's own
    # convention): the exclusive floor rejects a zero/negative value the
    # subprocess machinery could not honor, and the ceiling stops one tool from
    # holding a slot for more than ten minutes.
    llm_tool_timeout_seconds: float = Field(default=60, gt=0, le=600)

    # Hard cap on how many characters of a tool's STDOUT are used as its result
    # (see context_memory/services/tools.py). The tool result is fed straight
    # back into the conversation -- it rides into the NEXT create()'s prompt and
    # into the LLM log -- so an unbounded dump would blow both the prompt budget
    # and the bounded log ring. Output past the cap is truncated behind the
    # marker "…[工具輸出過長已截斷]" (mirroring memory_ai/llm_log's own truncation
    # markers). Bounded to [1000, 500000]: the floor keeps a deliberately small
    # override from truncating virtually every useful tool reply, and the
    # ceiling keeps a single tool result well under the default prompt budget it
    # will be folded into. Default 50000 comfortably fits a realistic KB lookup.
    llm_tool_output_max_chars: int = Field(default=50_000, ge=1_000, le=500_000)

    # Hard ceiling on the SERIALIZED size (in characters) of the LIVE tool-loop
    # conversation actually SENT to the model on each round of a single
    # ``generate_structured`` call (see context_memory/services/llm.py). This is
    # the SENT-side companion to the D27 llm_log aggregate budget, which bounds
    # only what is RECORDED, not what rides on the wire: each tool round appends up
    # to _MAX_TOOL_CALLS_PER_REPLY (16) tool results of up to
    # llm_tool_output_max_chars each, across up to llm_tool_rounds_max (or the
    # installer's tool_install_max_rounds) rounds -- a rounds x calls x output
    # product that, with maxed knobs, reaches hundreds of MB, risking a
    # context-length rejection or a huge transient allocation. Once the running
    # conversation exceeds this, the loop stops advertising tools and makes its
    # final tools-free completion (the same finalize the round budget triggers).
    # Default 1_000_000 chars is generous enough for a legitimate multi-round
    # tool/installer build yet caps that pathological product. A conversation this
    # large may still be rejected by the model for context length -- but that is a
    # clean, handled 502, not an unbounded allocation, and bounding memory is the
    # PRIMARY goal here. Bounded to [50_000, 8_000_000] (startup-validated via
    # pydantic-settings, matching the other LLM knobs' convention): the floor keeps
    # a deliberately small override from finalizing before even one real tool round
    # can accumulate, and the ceiling keeps a hostile override from re-opening the
    # unbounded growth this exists to cap.
    llm_tool_conversation_budget_chars: int = Field(default=1_000_000, ge=50_000, le=8_000_000)

    # Tool-round budget for ONE installer session (the web installer's
    # "tool builder" LLM call in context_memory/services/tool_builder.py),
    # passed as generate_structured's max_tool_rounds override. Building a tool
    # legitimately takes many rounds -- write files, run a test, read the
    # failure, fix, re-test -- so the default (24) is far above the workflows'
    # llm_tool_rounds_max (8). Bounded to [4, 64]: under 4 rounds no
    # write-test-fix cycle can complete even once, and the ceiling matches
    # llm_tool_rounds_max's own so a hostile override cannot exceed what the
    # loop itself permits anywhere else.
    tool_install_max_rounds: int = Field(default=24, ge=4, le=64)

    # Wall-clock budget (seconds) for ONE whole installer session, passed as
    # generate_structured's timeout_seconds override -- the single
    # asyncio.timeout around every builder round plus the final JSON. Default
    # 900 (15 min): a session is dozens of model round-trips plus real shell
    # runs against the target API, an order of magnitude beyond an ordinary
    # workflow call's openai_timeout_seconds. Bounded to (0, 3600] so a runaway
    # session still cannot pin its background job for more than an hour.
    tool_install_timeout_seconds: float = Field(default=900, gt=0, le=3600)

    # Wall-clock budget (seconds) for ONE run_shell meta-tool command inside an
    # installer session (process-group-killed on expiry, exactly like
    # llm_tool_timeout_seconds for installed tools). Deliberately FAR below
    # tool_install_timeout_seconds: a hung shell command must burn one round,
    # not the whole session budget -- per the stage-1 nested-timeout note, the
    # outer deadline cancels the coroutine but cannot interrupt the threadpool
    # thread the subprocess runs on, so this inner bound is what actually
    # frees that thread. Bounded to (0, 600], mirroring
    # llm_tool_timeout_seconds's convention.
    tool_install_shell_timeout_seconds: float = Field(default=120, gt=0, le=600)

    database_url: str = "sqlite:///./context_memory.db"

    # An item in any stale-eligible status (models.STALE_ELIGIBLE_STATUSES --
    # all five non-terminal statuses) is considered stale once its `updated`
    # timestamp is older than this many days (surfaced via
    # MemoryItemRead.is_stale). Bounded to [0, 36500] (~a century) so a
    # nonsensical value fails at startup via pydantic-settings, rather than
    # overflowing timedelta inside is_stale at read time and 500ing every read
    # or commit that touches a stale-eligible item.
    stale_after_days: int = Field(default=14, ge=0, le=36500)


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
