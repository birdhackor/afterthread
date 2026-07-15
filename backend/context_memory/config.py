"""Application settings, loaded from a CWD-relative `.env`.

Dev runs from `backend/`, so that `.env` is `backend/.env`. Packaged mode
(see `context_memory/cli.py`) loads `<data-dir>/.env` into the process
environment before this module is ever imported, so the CWD-relative lookup
below simply finds nothing there and every setting falls back to its
already-loaded env var / field default -- see D06 in docs/web-v2-decisions.md.
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
    # config loading is `cli.py`'s job instead -- it loads `<data-dir>/.env`
    # into the process environment (without overriding real env vars) before
    # this class is ever instantiated; see D06 in docs/web-v2-decisions.md.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # OpenAI-compatible endpoint. Intentionally left empty here; the user
    # will fill in the real URL later via backend/.env. Never invent one.
    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = ""

    # Per-request timeout (seconds) for the OpenAI-compatible endpoint, passed
    # straight to AsyncOpenAI(timeout=...) in context_memory/services/llm.py. Bounded at
    # construction (0 < value <= 600) so a nonsensical override -- a zero/
    # negative timeout the SDK would reject, or an absurdly large one that
    # would hang a request for hours -- fails loudly at startup via
    # pydantic-settings, rather than surfacing only when the first AI call is
    # made. Ten minutes is a generous ceiling for a single completion.
    openai_timeout_seconds: float = Field(default=60, gt=0, le=600)

    # Hard ceiling on the number of characters the serialized item snapshot may
    # occupy in an enrich / assist-update prompt (see
    # context_memory/services/memory_ai.py). Without it, an item with 19 sections of up to
    # 20k characters each serializes to a ~380k-character prompt that a
    # small-context model rejects on every call -- surfacing as a permanent 502
    # rather than a config error. Bounded to [4000, 200000] (startup-validated
    # via pydantic-settings, matching the openai_timeout_seconds /
    # stale_after_days convention): below 4k the header alone crowds out the
    # sections, and 200k is already generous for any real context window.
    llm_prompt_budget_chars: int = Field(default=32000, ge=4000, le=200000)

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
