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
