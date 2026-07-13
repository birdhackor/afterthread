"""Database engine, schema initialisation and session dependency."""

from collections.abc import Generator

from sqlalchemy import Engine, make_url
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  # ensure tables are registered on metadata
from app.config import get_settings


def _mask_db_url(url: str) -> str:
    """Render `url` with any embedded password hidden, for use in error messages.

    A misconfigured URL (e.g. `postgresql://user:s3cret@host/db`) must never
    have its credentials echoed back in a raised exception, since that text
    tends to end up in logs or error-tracking services. Falls back to just
    the URL scheme if `url` cannot be parsed at all.
    """
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        scheme = url.split("://", 1)[0]
        return f"{scheme}://..."


def _is_memory_sqlite_url(url: str) -> bool:
    """True for SQLite URLs that do not address a durable, file-backed database.

    Covers an explicit `:memory:` database, the bare `sqlite://` DSN (no
    database at all), and `sqlite:///` with an empty path -- SQLite treats a
    missing/empty filename as a private, anonymous on-disk database that
    exists only for the connection that opened it, which is just as unsafe
    to share across threads/requests as `:memory:` is.
    """
    if ":memory:" in url:
        return True
    return not make_url(url).database


def create_db_engine(database_url: str) -> Engine:
    """Build the SQLAlchemy engine for `database_url`.

    SQLite is the only supported backend: item tag filtering relies on
    SQLite's `json_each` table-valued function (see
    `routers/items.py::_tag_filter`), which has no portable equivalent, so
    any other backend is rejected here rather than silently behaving
    differently at query time.

    In-memory SQLite is also rejected: this is a persistence app, so the
    server has no legitimate in-memory mode (data must survive restarts).
    A single shared in-memory database also requires SQLAlchemy's
    StaticPool -- one DBAPI connection reused by every thread -- which
    defeats transaction isolation between concurrent sessions (one
    session's commit can commit another session's pending writes). Tests
    that want an isolated, ephemeral database should build their own engine
    directly (see `tests/conftest.py`) rather than calling this function.
    """
    if not database_url.startswith("sqlite"):
        raise RuntimeError(
            f"Only SQLite database URLs are supported (got: {_mask_db_url(database_url)})"
        )

    if _is_memory_sqlite_url(database_url):
        raise RuntimeError(
            "In-memory SQLite database URLs are not supported "
            f"(got: {_mask_db_url(database_url)}). This server requires a "
            "file-backed SQLite path so data survives restarts, e.g. "
            "'sqlite:///./context_memory.db'. Tests that need an isolated, "
            "ephemeral database may build their own engine directly instead "
            "of calling create_db_engine()."
        )

    # check_same_thread is a pysqlite-specific flag; safe unconditionally now
    # that non-SQLite and in-memory URLs are rejected above -- every
    # remaining connection opens the same on-disk file, so SQLAlchemy's
    # default pool for file-based SQLite already shares one database across
    # threads without needing StaticPool.
    connect_args: dict[str, object] = {"check_same_thread": False}
    return create_engine(database_url, connect_args=connect_args)


engine = create_db_engine(get_settings().database_url)


def init_db() -> None:
    """Create all tables that do not yet exist."""
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session]:
    """FastAPI dependency yielding a database session."""
    with Session(engine) as session:
        yield session
