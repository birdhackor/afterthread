"""Application settings, loaded from backend/.env."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """Runtime configuration for the Context Memory backend."""

    model_config = SettingsConfigDict(env_file=_ENV_FILE, extra="ignore")

    # OpenAI-compatible endpoint. Intentionally left empty here; the user
    # will fill in the real URL later via backend/.env. Never invent one.
    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = ""

    # Per-request timeout (seconds) for the OpenAI-compatible endpoint, passed
    # straight to AsyncOpenAI(timeout=...) in app/services/llm.py. Bounded at
    # construction (0 < value <= 600) so a nonsensical override -- a zero/
    # negative timeout the SDK would reject, or an absurdly large one that
    # would hang a request for hours -- fails loudly at startup via
    # pydantic-settings, rather than surfacing only when the first AI call is
    # made. Ten minutes is a generous ceiling for a single completion.
    openai_timeout_seconds: float = Field(default=60, gt=0, le=600)

    # Hard ceiling on the number of characters the serialized item snapshot may
    # occupy in an enrich / assist-update prompt (see
    # app/services/memory_ai.py). Without it, an item with 19 sections of up to
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
