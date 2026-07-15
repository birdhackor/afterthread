"""Database engine, schema initialisation and session dependency."""

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, event, make_url, text
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry
from sqlmodel import Session, SQLModel, create_engine

from context_memory import models  # noqa: F401  # ensure tables are registered on metadata
from context_memory.config import get_settings


def _url_database(url: str) -> str:
    """Return only the parsed `database` (filename/URI-path) component of
    `url`, for use in the in-memory-SQLite rejection message below.

    Used only for that message. That URL has already been confirmed to
    parse with dialect "sqlite" (see `create_db_engine`), so -- unlike an
    arbitrary rejected non-SQLite URL (see `_url_dialect`) -- it cannot be a
    full production connection string for another backend, and has no
    username/password/host/port component to begin with. Its *query
    string* is not automatically safe, though: SQLite's URI-filename form
    (https://www.sqlite.org/uri.html) lets encrypted-SQLite drivers (e.g.
    SQLCipher) carry a passphrase in a `key=` parameter, e.g.
    `sqlite:///file:app.db?uri=true&key=s3cret`. A full masked render via
    `render_as_string(hide_password=True)` would leak that straight into
    this error message -- that call only ever masks a URL's own `password`
    *component*, and does nothing for a secret sitting in an arbitrary
    query parameter instead. Returning only the parsed `database` component
    (here, `file:app.db`) sidesteps the problem entirely: the query string
    is never included, secret or not. Falls back to a fixed placeholder --
    echoing nothing from `url` -- if it cannot be parsed at all: a malformed
    DSN may have no `://` separator at all (e.g.
    `postgresql:user:s3cret@host/db`), in which case naively splitting on
    `://` yields the *entire* string, credentials included, as the "scheme".
    """
    try:
        return make_url(url).database or ""
    except Exception:
        return "<unparseable database URL>"


def _url_dialect(url: str) -> str:
    """Return only the parsed dialect/backend name of `url` (e.g. "postgresql"),
    for use in the non-SQLite rejection error message below.

    A masked render via `render_as_string(hide_password=True)` only masks a
    URL's `password` *component* (see `_url_database` above, which sidesteps
    this altogether by never rendering the query string, rather than by
    masking it). It does nothing about credentials embedded elsewhere, which
    several real drivers do use -- e.g. a `PWD=` buried inside an ODBC
    `odbc_connect=...` connection string, or a `?sslpassword=...` query
    parameter -- so it is not safe here: the rejected `database_url` could
    be a full, credential-bearing production connection string for another
    database entirely, and this error tends to end up in logs or
    error-tracking services. The dialect name is the only
    component that is always safe to echo back. Falls back to a fixed
    placeholder -- echoing nothing from `url` -- if it cannot be parsed at
    all: a malformed DSN may have no `://` separator at all (e.g.
    `postgresql:user:s3cret@host/db`), in which case naively splitting on
    `://` yields the *entire* string, credentials included, as the "scheme".
    """
    try:
        return make_url(url).get_backend_name()
    except Exception:
        return "<unparseable database URL>"


def _non_persistent_sqlite_error(database_url: str) -> RuntimeError:
    """Build the RuntimeError raised for a SQLite URL that does not durably
    persist to a file.

    Raised by the runtime `pragma_database_list` probe in `create_db_engine`
    below -- the sole gate on non-persistent SQLite URLs -- which asks SQLite
    itself whether the database it opened is backed by a real on-disk file.
    """
    return RuntimeError(
        "In-memory SQLite database URLs are not supported "
        f"(got: {_url_database(database_url)}). This server requires a "
        "file-backed SQLite path so data survives restarts, e.g. "
        "'sqlite:///./context_memory.db'. Tests that need an isolated, "
        "ephemeral database may build their own engine directly instead "
        "of calling create_db_engine()."
    )


def _py_casefold(value: str | None) -> str | None:
    """Backing implementation of the `py_casefold` SQLite function registered
    below.

    Returns `str.casefold()` -- full Unicode case folding, so accented and
    other non-ASCII letters compare case-insensitively (SQLite's built-in
    `lower()` and LIKE fold only ASCII, so e.g. "École" would never match a
    query of "école"). NULL-safe: a NULL column value arrives here as `None`
    and must map back to `None` (returned unchanged) rather than raising, so
    the function can be applied to nullable columns without crashing.
    """
    if value is None:
        return None
    return value.casefold()


def _set_sqlite_foreign_keys_pragma(
    dbapi_connection: DBAPIConnection, connection_record: ConnectionPoolEntry
) -> None:
    """Connect-event listener: enable FK enforcement and register `py_casefold`.

    SQLite parses `FOREIGN KEY` clauses but does not enforce them unless
    `PRAGMA foreign_keys = ON` is issued on *every* connection -- it is off
    by default, and is a per-connection setting rather than one a database
    file can persist. Without it, e.g. a `POST /api/items/{id}/progress`
    racing a committed `DELETE /api/items/{id}` can insert a `ProgressEntry`
    whose `item_id` references an already-deleted `MemoryItem`: nothing
    rejects the insert, leaving an orphan row no API call can ever remove.

    This is SQLAlchemy's documented recipe for SQLite FK enforcement (see
    https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#foreign-key-support),
    including the temporary `autocommit` flip: SQLite treats `PRAGMA
    foreign_keys` as a no-op while a transaction is open, and the sqlite3
    driver's default "legacy" transaction-control mode can leave one open on
    a freshly made connection, which would otherwise silently swallow this.

    The same connect event also registers `py_casefold` (see `_py_casefold`)
    as a deterministic, single-argument SQLite function, so `routers/items.py`'s
    `q` search can match titles/snapshots/keywords case-insensitively over the
    full Unicode range rather than ASCII-only. Registered here, on the one
    connect event every engine shares (production and the test engine alike,
    via `enable_sqlite_foreign_keys`), so the function exists on every
    connection the app opens.
    """
    previous_autocommit = dbapi_connection.autocommit
    dbapi_connection.autocommit = True
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()
    dbapi_connection.autocommit = previous_autocommit
    # deterministic=True: `py_casefold` is a pure function of its input, so
    # SQLite may cache/reuse its result freely. One argument; None-safe.
    dbapi_connection.create_function("py_casefold", 1, _py_casefold, deterministic=True)


def enable_sqlite_foreign_keys(engine: Engine) -> None:
    """Register `_set_sqlite_foreign_keys_pragma` on `engine`'s "connect" event.

    Attaches the per-connection SQLite setup this app relies on -- foreign-key
    enforcement plus the `py_casefold` case-folding search function (see
    `_set_sqlite_foreign_keys_pragma`). Attached per-engine rather than
    globally on the `Engine` class, so only engines that opt in are affected.
    Shared between `create_db_engine` below and `tests/conftest.py`'s isolated
    test engine -- which cannot go through `create_db_engine` itself, see that
    function's docstring -- so both run under the same foreign-key and search
    semantics as production.
    """
    event.listen(engine, "connect", _set_sqlite_foreign_keys_pragma)


def create_db_engine(database_url: str) -> Engine:
    """Build the SQLAlchemy engine for `database_url`.

    SQLite is the only supported backend: item tag filtering relies on
    SQLite's `json_each` table-valued function (see
    `routers/items.py::_tag_filter`), which has no portable equivalent, so
    any other backend is rejected here rather than silently behaving
    differently at query time.

    In-memory SQLite is also rejected, by a single runtime
    `pragma_database_list` probe (see the comment above that check, below):
    once the engine exists, it asks SQLite itself whether the database it
    opened is backed by a real on-disk file, catching every in-memory URL
    spelling by construction rather than re-deriving the answer from the URL
    text. Rejected because this is a persistence app, so the server has no
    legitimate in-memory mode (data must survive restarts). A single shared
    in-memory database also requires SQLAlchemy's StaticPool -- one DBAPI
    connection reused by every thread -- which defeats transaction isolation
    between concurrent sessions (one session's commit can commit another
    session's pending writes). Tests that want an isolated, ephemeral
    database should build their own engine directly (see `tests/conftest.py`)
    rather than calling this function.
    """
    dialect = _url_dialect(database_url)
    if dialect != "sqlite":
        raise RuntimeError(f'Only SQLite database URLs are supported (got dialect: "{dialect}")')

    # check_same_thread is a pysqlite-specific flag; safe unconditionally
    # because every URL create_db_engine ultimately *returns* an engine for is
    # file-backed SQLite (non-SQLite is rejected above; in-memory is rejected
    # by the probe below) -- each such connection opens the same on-disk file,
    # so SQLAlchemy's default pool for file-based SQLite already shares one
    # database across threads without needing StaticPool.
    connect_args: dict[str, object] = {"check_same_thread": False}
    engine = create_engine(database_url, connect_args=connect_args)
    enable_sqlite_foreign_keys(engine)

    # `database_url` comes from the operator's own `.env`. Rather than
    # statically re-deriving from the URL text whether it addresses a durable
    # file -- a fragile reimplementation of pysqlite's own `uri=` query-string
    # parsing rules, where review kept finding new in-memory spellings it did
    # not yet reject (truthy `uri=` variants like `uri=y`/`uri=t`/`uri=True`; a
    # duplicated `uri=false&uri=false` that SQLAlchemy's own `coerce_kw_type`
    # still coerces to *true* through a non-empty tuple, even though neither
    # value is truthy on its own; a percent-encoded `file:%3Amemory%3A`; an
    # authority-form `file://`), all of which only resolve to an
    # in-memory/anonymous-temp database once SQLite's own URI parser runs at
    # connection time -- this probe asks SQLite itself what it opened: per
    # https://www.sqlite.org/pragma.html#pragma_database_list, any database
    # that is not backed by a real on-disk file -- in-memory,
    # private/anonymous temp, or opened via SQLite's `memdb` VFS -- always
    # reports an empty `file` column, however its URL was spelled. That makes
    # this the sole gate on non-persistent SQLite URLs, closing every spelling
    # (known or not) by construction.
    with engine.connect() as connection:
        main_file = connection.execute(
            text("SELECT file FROM pragma_database_list WHERE name = 'main'")
        ).scalar()
    if not main_file:
        engine.dispose()
        raise _non_persistent_sqlite_error(database_url)

    return engine


@lru_cache
def get_engine() -> Engine:
    """Return the process-wide database engine, created lazily on first use.

    Deliberately NOT a module-level `engine = create_db_engine(...)`: that
    variant connects at IMPORT time, because `create_db_engine` runs its
    persistence probe immediately (see its docstring), and for a file-backed
    URL that probe creates the database file. Importing `context_memory.main`/`context_memory.db`
    then had a filesystem side effect -- it materialised `./context_memory.db`
    in the process's cwd -- which broke read-only checkouts at test-collection
    time and left a real DB file behind even in tests that override
    `get_session` and never run the app lifespan.

    Deferring construction to the first call moves that probe to STARTUP:
    `init_db()`, invoked from the app lifespan, is the first caller (see
    `context_memory/main.py`), so the probe runs once, before any request is handled, and
    never at import. `lru_cache` makes this a per-process singleton -- the
    engine and its one-time probe are created exactly once and reused -- so the
    probe is a startup cost, NOT a per-request one. Tests that need an
    isolated engine still build one directly via `create_db_engine` /
    `create_engine` (see `tests/test_db.py`, `tests/conftest.py`) and are
    unaffected by this accessor.
    """
    return create_db_engine(get_settings().database_url)


def init_db() -> None:
    """Create all tables that do not yet exist.

    Called from the app lifespan at startup. As the first `get_engine()`
    caller it triggers the lazy engine's creation -- and thus its persistence
    probe -- here, before any request handling, rather than at import time.
    """
    SQLModel.metadata.create_all(get_engine())


def get_session() -> Generator[Session]:
    """FastAPI dependency yielding a database session."""
    with Session(get_engine()) as session:
        yield session
