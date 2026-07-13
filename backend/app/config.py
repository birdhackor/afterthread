"""Application settings, loaded from backend/.env."""

from functools import lru_cache
from pathlib import Path

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

    database_url: str = "sqlite:///./context_memory.db"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
