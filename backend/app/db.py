"""Database engine, schema initialisation and session dependency."""

from collections.abc import Generator

from sqlalchemy import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  # ensure tables are registered on metadata
from app.config import get_settings


def _is_memory_sqlite_url(url: str) -> bool:
    """True for SQLite URLs that address a private, in-memory database."""
    return ":memory:" in url or url == "sqlite://"


def create_db_engine(database_url: str) -> Engine:
    """Build the SQLAlchemy engine for `database_url`.

    SQLite is the only supported backend: item tag filtering relies on
    SQLite's `json_each` table-valued function (see
    `routers/items.py::_tag_filter`), which has no portable equivalent, so
    any other backend is rejected here rather than silently behaving
    differently at query time.
    """
    if not database_url.startswith("sqlite"):
        raise RuntimeError(f"Only SQLite database URLs are supported (got: {database_url!r})")

    # check_same_thread is a pysqlite-specific flag; safe unconditionally now
    # that non-SQLite URLs are rejected above.
    connect_args: dict[str, object] = {"check_same_thread": False}

    if _is_memory_sqlite_url(database_url):
        # SQLAlchemy's default pool for an in-memory SQLite URL is
        # SingletonThreadPool, which hands each *thread* its own connection --
        # and a `:memory:` database only exists within the connection that
        # created it. The lifespan's create_all() runs on one thread while
        # request handlers run on others, so without StaticPool (a single
        # connection shared by every thread) requests would hit a freshly
        # created, empty database ("no such table"). File-based SQLite is
        # unaffected: every connection opens the same file, so its default
        # pool already shares one database.
        return create_engine(database_url, connect_args=connect_args, poolclass=StaticPool)
    return create_engine(database_url, connect_args=connect_args)


engine = create_db_engine(get_settings().database_url)


def init_db() -> None:
    """Create all tables that do not yet exist."""
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session]:
    """FastAPI dependency yielding a database session."""
    with Session(engine) as session:
        yield session
