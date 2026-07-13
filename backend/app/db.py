"""Database engine, schema initialisation and session dependency."""

from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  # ensure tables are registered on metadata
from app.config import get_settings

_settings = get_settings()

# check_same_thread is only meaningful (and valid) for SQLite.
_connect_args: dict[str, object] = (
    {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}
)

engine = create_engine(_settings.database_url, connect_args=_connect_args)


def init_db() -> None:
    """Create all tables that do not yet exist."""
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session]:
    """FastAPI dependency yielding a database session."""
    with Session(engine) as session:
        yield session
