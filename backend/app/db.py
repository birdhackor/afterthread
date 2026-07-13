"""Database engine, schema initialisation and session dependency."""

from collections.abc import Generator
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import Engine, event, make_url, text
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  # ensure tables are registered on metadata
from app.config import get_settings


def _mask_db_url(url: str) -> str:
    """Render `url` with any embedded password hidden, for use in error messages.

    Used only for the in-memory-SQLite rejection message below. That URL has
    already been confirmed to start with `sqlite`, so -- unlike an arbitrary
    rejected non-SQLite URL (see `_url_dialect`) -- it cannot be a full
    production connection string for another backend, which makes masking
    (rather than omitting entirely) an acceptable tradeoff for keeping the
    path useful for debugging. Falls back to a fixed placeholder -- echoing
    nothing from `url` -- if it cannot be parsed at all: a malformed DSN may
    have no `://` separator at all (e.g. `postgresql:user:s3cret@host/db`),
    in which case naively splitting on `://` yields the *entire* string,
    credentials included, as the "scheme".
    """
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable database URL>"


def _url_dialect(url: str) -> str:
    """Return only the parsed dialect/backend name of `url` (e.g. "postgresql"),
    for use in the non-SQLite rejection error message below.

    `render_as_string(hide_password=True)` (see `_mask_db_url` above) only
    masks a URL's `password` *component*. It does nothing about credentials
    embedded elsewhere, which several real drivers do use -- e.g. a `PWD=`
    buried inside an ODBC `odbc_connect=...` connection string, or a
    `?sslpassword=...` query parameter -- so it is not safe here: the
    rejected `database_url` could be a full, credential-bearing production
    connection string for another database entirely, and this error tends to
    end up in logs or error-tracking services. The dialect name is the only
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


_URI_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _uri_mode_enabled(query: dict[str, list[str]]) -> bool:
    """Whether `query`'s `uri` parameter turns on SQLite's URI-filename mode.

    Mirrors `sqlalchemy.util.asbool`, case-insensitively: `"1"`, `"true"`,
    `"yes"`, `"on"` enable it; `"0"`, `"false"`, `"no"`, `"off"`, an absent
    `uri` key, or any other spelling all leave it disabled. (The real
    `asbool` also recognises a couple of single-letter synonyms and raises
    `ValueError` for a spelling it does not recognise at all, rather than
    treating it as false; neither distinction matters here, since a value
    this function cannot make sense of is still caught for real -- loudly,
    via that same `ValueError` -- by pysqlite's own `asbool` call the first
    time a connection is actually attempted. This function only has to
    decide, ahead of that, whether `_is_memory_sqlite_url`'s URI-only checks
    below apply.) A URL with more than one `uri=` value (e.g.
    `uri=false&uri=true`) is treated as enabled if *any* value is truthy,
    erring toward applying the extra rejection checks rather than skipping
    them.
    """
    return any(value.strip().lower() in _URI_TRUE_VALUES for value in query.get("uri", []))


def _is_memory_sqlite_url(url: str) -> bool:
    """True for SQLite URLs that do not address a durable, file-backed database.

    Covers an explicit `:memory:` database, the bare `sqlite://` DSN (no
    database at all), and `sqlite:///` with an empty path -- SQLite treats a
    missing/empty filename as a private, anonymous on-disk database that
    exists only for the connection that opened it, which is just as unsafe
    to share across threads/requests as `:memory:` is.

    Also covers SQLite's URI-filename form (`sqlite:///file:name?...`, see
    https://www.sqlite.org/uri.html): a `database` of e.g. `file:memdb1`
    looks file-backed, but `mode=memory` in its query string still opens an
    in-memory database (optionally a named, shared-cache one), even though
    no literal `:memory:` substring appears anywhere in the URL, e.g.
    `sqlite:///file:memdb1?mode=memory&cache=shared&uri=true`. A URI-form
    filename *without* `mode=memory` (e.g. `sqlite:///file:real.db?uri=true`)
    genuinely is file-backed and must remain allowed.

    The three checks below (empty URI filename, `vfs=memdb`, `mode=memory`)
    apply only once SQLite's URI-filename parsing is actually *in effect*,
    per `_uri_mode_enabled` above. SQLAlchemy's pysqlite dialect decides
    whether to pass `uri=True` to `sqlite3.connect()` -- activating
    https://www.sqlite.org/uri.html syntax -- by running the URL's `uri`
    query value through `sqlalchemy.util.asbool` (see the
    `_pysqlite_uri_connections` section of
    `sqlalchemy.dialects.sqlite.pysqlite`, and `coerce_kw_type`'s use of it
    in `create_connect_args`), which accepts far more spellings than just
    the literal string `"true"` -- `uri=1`, `uri=True`, `uri=on` all enable
    it too, case-insensitively. Gating only on the literal `"true"` would
    reopen exactly the hole these checks exist to close: e.g.
    `sqlite:///file:mem1?vfs=memdb&uri=True` is opened by pysqlite as an
    in-memory database (capitalised `True` still satisfies `asbool`) but
    would slip past a literal-`"true"` check unrejected. Conversely, when
    `uri` is genuinely absent or falsy, pysqlite treats the whole `file:...`
    string as a literal on-disk filename (odd-looking, but a real,
    persistent, single file) and never even looks at the rest of the query
    string -- so a `mode=memory` or `vfs=memdb` there is inert text, and
    such a URL must stay allowed, e.g. `sqlite:///file:x.db?mode=memory`
    with no `uri` key at all is a literal, persistent filename that merely
    happens to contain that substring -- the checks below must not fire for
    it:

    - An empty URI filename, e.g. `sqlite:///file:?uri=true` (nothing
      between `file:` and `?`): SQLite documents this as opening a private,
      anonymous on-disk database scoped to the single connection that opened
      it -- the same unsafe-to-share problem as the empty-path case above,
      just spelled through the URI form instead.
    - `vfs=memdb` in the query, e.g.
      `sqlite:///file:mem1?vfs=memdb&uri=true`: this selects SQLite's
      in-memory VFS explicitly, opening a memory-backed database regardless
      of what the filename portion says.
    - `mode=memory` in the query, e.g.
      `sqlite:///file:memdb1?mode=memory&cache=shared&uri=true`: opens an
      in-memory (optionally named, shared-cache) database; see the class
      docstring above.
    """
    # Compare the exact parsed `database` component, not a substring search
    # over the whole URL string: SQLite treats only the literal, *exact*
    # filename ":memory:" as its special in-memory database, so a substring
    # check (`":memory:" in url`) would wrongly reject a genuine, file-backed
    # path that merely *contains* that text, e.g.
    # "sqlite:///./notes:memory:.db" is a real on-disk file named
    # "notes:memory:.db", not an in-memory database. The runtime
    # `pragma_database_list` probe in `create_db_engine` below remains the
    # authoritative backstop for exotic spellings this static, best-effort
    # check does not (or cannot) recognise -- see that function's docstring.
    database = make_url(url).database
    if database == ":memory:":
        return True
    if not database:
        return True
    if database.startswith("file:"):
        query = parse_qs(urlsplit(url).query)
        if _uri_mode_enabled(query):
            if database == "file:":
                return True
            if "memdb" in query.get("vfs", []):
                return True
            if "memory" in query.get("mode", []):
                return True
    return False


def _non_persistent_sqlite_error(database_url: str) -> RuntimeError:
    """Build the RuntimeError raised for a SQLite URL that does not durably
    persist to a file.

    Shared by the static `_is_memory_sqlite_url` check and the runtime
    `pragma_database_list` probe in `create_db_engine` below, so both raise
    an identical, "same style" error regardless of which one catches a
    given URL -- see `create_db_engine`'s docstring for why there are two.
    """
    return RuntimeError(
        "In-memory SQLite database URLs are not supported "
        f"(got: {_mask_db_url(database_url)}). This server requires a "
        "file-backed SQLite path so data survives restarts, e.g. "
        "'sqlite:///./context_memory.db'. Tests that need an isolated, "
        "ephemeral database may build their own engine directly instead "
        "of calling create_db_engine()."
    )


def _set_sqlite_foreign_keys_pragma(
    dbapi_connection: DBAPIConnection, connection_record: ConnectionPoolEntry
) -> None:
    """Connect-event listener: turn on SQLite foreign-key enforcement.

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
    """
    previous_autocommit = dbapi_connection.autocommit
    dbapi_connection.autocommit = True
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()
    dbapi_connection.autocommit = previous_autocommit


def enable_sqlite_foreign_keys(engine: Engine) -> None:
    """Register `_set_sqlite_foreign_keys_pragma` on `engine`'s "connect" event.

    Attached per-engine rather than globally on the `Engine` class, so only
    engines that opt in are affected. Shared between `create_db_engine`
    below and `tests/conftest.py`'s isolated test engine -- which cannot go
    through `create_db_engine` itself, see that function's docstring -- so
    both run under the same foreign-key semantics as production.
    """
    event.listen(engine, "connect", _set_sqlite_foreign_keys_pragma)


def create_db_engine(database_url: str) -> Engine:
    """Build the SQLAlchemy engine for `database_url`.

    SQLite is the only supported backend: item tag filtering relies on
    SQLite's `json_each` table-valued function (see
    `routers/items.py::_tag_filter`), which has no portable equivalent, so
    any other backend is rejected here rather than silently behaving
    differently at query time.

    In-memory SQLite is also rejected, in two layers: a static URL-shape
    check (`_is_memory_sqlite_url`) raises a friendly, fast-fail error for
    common misspellings, and a runtime `pragma_database_list` probe (see the
    comment above that check, below) independently re-verifies against
    SQLite itself once the engine exists, catching every remaining spelling
    the static check was not taught to recognise. Rejected either way: this
    is a persistence app, so the server has no legitimate in-memory mode
    (data must survive restarts). A single shared in-memory database also
    requires SQLAlchemy's StaticPool -- one DBAPI connection reused by every
    thread -- which defeats transaction isolation between concurrent
    sessions (one session's commit can commit another session's pending
    writes). Tests that want an isolated, ephemeral database should build
    their own engine directly (see `tests/conftest.py`) rather than calling
    this function.
    """
    dialect = _url_dialect(database_url)
    if dialect != "sqlite":
        raise RuntimeError(f'Only SQLite database URLs are supported (got dialect: "{dialect}")')

    if _is_memory_sqlite_url(database_url):
        raise _non_persistent_sqlite_error(database_url)

    # check_same_thread is a pysqlite-specific flag; safe unconditionally now
    # that non-SQLite and in-memory URLs are rejected above -- every
    # remaining connection opens the same on-disk file, so SQLAlchemy's
    # default pool for file-based SQLite already shares one database across
    # threads without needing StaticPool.
    connect_args: dict[str, object] = {"check_same_thread": False}
    engine = create_engine(database_url, connect_args=connect_args)
    enable_sqlite_foreign_keys(engine)

    # `database_url` comes from the operator's own `.env`. The static check
    # above is a fast-fail UX nicety: a friendly, SQLite-specific error for
    # common misspellings, raised before ever touching the filesystem. But
    # it is necessarily a *reimplementation* of pysqlite's own `uri=`
    # query-string parsing rules, and review keeps finding new spellings it
    # doesn't yet know to reject -- e.g. SQLAlchemy's real `uri=` coercion
    # accepts far more truthy spellings than `_uri_mode_enabled` above
    # replicates (`uri=y`, `uri=t`); a duplicated `uri=false&uri=false` is
    # coerced to *true* by SQLAlchemy's own `coerce_kw_type`/`asbool`
    # through a non-empty tuple (`bool(("false", "false"))` is `True`,
    # regardless of the strings it contains), even though neither value is
    # truthy on its own; and a percent-encoded `file:%3Amemory%3A` or
    # authority-form `file://` filename looks like a non-empty, unremarkable
    # path to `_is_memory_sqlite_url`'s string comparisons, only resolving
    # to an in-memory/anonymous-temp database once SQLite's own URI parser
    # actually runs, at connection time. Rather than keep chasing individual
    # spellings, this probe asks SQLite itself what it opened: per
    # https://www.sqlite.org/pragma.html#pragma_database_list, any database
    # that is not backed by a real on-disk file -- in-memory,
    # private/anonymous temp, or opened via SQLite's `memdb` VFS -- always
    # reports an empty `file` column, however its URL was spelled. This is
    # the authoritative backstop that closes every URI spelling, known or
    # not, by construction; the static check above only ever gets to be a
    # friendly early error for the common cases.
    with engine.connect() as connection:
        main_file = connection.execute(
            text("SELECT file FROM pragma_database_list WHERE name = 'main'")
        ).scalar()
    if not main_file:
        engine.dispose()
        raise _non_persistent_sqlite_error(database_url)

    return engine


engine = create_db_engine(get_settings().database_url)


def init_db() -> None:
    """Create all tables that do not yet exist."""
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session]:
    """FastAPI dependency yielding a database session."""
    with Session(engine) as session:
        yield session
