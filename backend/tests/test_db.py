"""Tests for engine construction in app.db: the SQLite-only guard and its
dialect-only (never host/database/query-string) rejection message -- including
the fallback for a URL too malformed to parse at all -- the in-memory SQLite
rejection -- including SQLite's URI-filename `mode=memory` spelling -- (an
in-memory database has no legitimate use in this server -- it cannot survive
a restart, and safely sharing one across threads would require StaticPool,
which defeats transaction isolation between concurrent sessions) -- and
foreign-key enforcement.
"""

import threading
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select

from app.db import create_db_engine
from app.models import MemoryItem, ProgressEntry


def test_non_sqlite_url_rejected() -> None:
    with pytest.raises(RuntimeError, match="Only SQLite database URLs are supported"):
        create_db_engine("postgresql://user:pass@localhost/db")


def test_non_sqlite_url_error_message_includes_dialect() -> None:
    with pytest.raises(RuntimeError, match='got dialect: "postgres"'):
        create_db_engine("postgres://example/db")


def test_sqlite_prefixed_non_sqlite_dialect_rejected() -> None:
    """A URL like `sqlitefoo://...` shares the literal `"sqlite"` prefix but
    parses to a distinct, unsupported dialect. A `str.startswith("sqlite")`
    guard would wrongly accept it and let it reach `create_engine()` with
    SQLite-specific `connect_args` and the PRAGMA foreign-key listener
    attached to a foreign dialect. The guard must instead compare the exact
    parsed dialect (`make_url(...).get_backend_name() == "sqlite"`), and the
    resulting error must mention only that parsed dialect -- no other URL
    component.
    """
    with pytest.raises(RuntimeError) as exc_info:
        create_db_engine("sqlitefoo://x")
    message = str(exc_info.value)
    assert 'got dialect: "sqlitefoo"' in message
    assert "x" not in message


def test_non_sqlite_url_error_message_leaks_no_url_components() -> None:
    """A misconfigured URL for another backend must not leak into logs: not
    just its password component, but nothing at all beyond the dialect name.
    In particular, a password hidden in a query parameter (e.g.
    `?sslpassword=...`, or a `PWD=` buried inside an ODBC
    `odbc_connect=...` connection string) must not leak either --
    `render_as_string(hide_password=True)` alone would not catch those, since
    it only masks a URL's own `password` component.
    """
    with pytest.raises(RuntimeError) as exc_info:
        create_db_engine("postgresql://user:s3cret@h/db?sslpassword=qs3cret")
    message = str(exc_info.value)
    assert "postgresql" in message
    assert "s3cret" not in message
    assert "qs3cret" not in message
    assert "h" not in message
    assert "db" not in message


def test_unparseable_url_error_message_leaks_nothing() -> None:
    """A DSN with no `://` separator at all (e.g. a `postgresql:` URL where
    someone forgot the slashes) cannot be parsed by SQLAlchemy's `make_url`.
    The `_url_dialect` fallback for that case must be a fixed placeholder
    that echoes nothing from the input -- a naive `scheme = url.split("://",
    1)[0]` fallback would treat the *entire* unparseable string, including
    any embedded credentials, as the "scheme" and echo it straight back.
    """
    with pytest.raises(RuntimeError) as exc_info:
        create_db_engine("postgresql:user:s3cret@host/db")
    message = str(exc_info.value)
    assert "<unparseable database URL>" in message
    assert "s3cret" not in message
    assert "host" not in message


def test_memory_sqlite_url_rejected() -> None:
    """An explicit `:memory:` database is rejected: this is a persistence
    app, so the server has no legitimate in-memory mode.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///:memory:")


def test_bare_sqlite_url_rejected() -> None:
    """The bare `sqlite://` DSN (no database at all) is just as unsafe as an
    explicit `:memory:` URL and must be rejected the same way.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite://")


def test_empty_path_sqlite_url_rejected() -> None:
    """`sqlite:///` with an empty path is SQLite's "private, anonymous
    on-disk database" -- each connection gets its own, so it shares the same
    cross-connection isolation problem as `:memory:` and must be rejected.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///")


def test_sqlite_uri_mode_memory_rejected() -> None:
    """SQLite's URI-filename form (`file:name?mode=memory...`, see
    https://www.sqlite.org/uri.html) parses with a non-empty `database`
    (`file:memdb1`) and contains no literal `:memory:` substring anywhere in
    the URL, yet `mode=memory` in its query string still opens an in-memory
    (optionally named, shared-cache) database. It must be rejected exactly
    like the plain `:memory:` spelling.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:memdb1?mode=memory&cache=shared&uri=true")


def test_sqlite_uri_file_backed_without_mode_memory_is_allowed(tmp_path: Path) -> None:
    """A URI-form SQLite filename *without* `mode=memory` genuinely addresses
    a durable file and must remain allowed. Uses an absolute path inside
    `tmp_path` (rather than relying on the process's cwd) so the database
    file cannot land in the repo.

    This also covers the "`file:real.db?uri=true` still accepted" case for
    the `uri=true`-gated empty-filename and `vfs=memdb` checks below: this
    URL has `uri=true` in effect, a non-empty filename, and no `vfs=memdb`,
    so neither new check should fire.
    """
    db_path = tmp_path / "realfile.db"
    engine = create_db_engine(f"sqlite:///file:{db_path}?uri=true")
    try:
        SQLModel.metadata.create_all(engine)
        assert db_path.exists()
    finally:
        engine.dispose()


def test_sqlite_uri_empty_filename_rejected() -> None:
    """`sqlite:///file:?uri=true` -- nothing between `file:` and `?` -- is
    SQLite's documented spelling for a private, anonymous on-disk database
    scoped to the single connection that opens it: the same
    unsafe-to-share-across-requests problem as the empty-path
    (`sqlite:///`) case above, just reached through the URI form with an
    empty filename instead of a bare empty path.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:?uri=true")


def test_sqlite_uri_vfs_memdb_rejected() -> None:
    """`vfs=memdb` in a URI-form SQLite URL selects SQLite's in-memory VFS
    outright, regardless of what the filename portion says -- e.g.
    `sqlite:///file:mem1?vfs=memdb&uri=true` opens an in-memory database
    named `mem1`, not a file called `mem1`. It must be rejected exactly like
    `mode=memory`.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:mem1?vfs=memdb&uri=true")


def test_sqlite_file_prefixed_literal_filename_without_uri_flag_is_allowed() -> None:
    """Without `uri=true` present in the query string, pysqlite never
    enables SQLite's URI-filename parsing at all (see the
    `_pysqlite_uri_connections` section of
    `sqlalchemy.dialects.sqlite.pysqlite`): a `file:`-prefixed `database` --
    even one whose query string contains `vfs=memdb`, which would otherwise
    be rejected as selecting an in-memory VFS -- is passed to
    `sqlite3.connect()` as a literal (if oddly named) filename, and the
    query string is simply never passed to the driver at all. Such a URL is
    therefore genuinely file-backed and must remain allowed, proving the
    two checks above are correctly gated on `uri=true` rather than firing on
    `vfs=memdb`/an empty filename unconditionally.

    Deliberately does not open a real connection (e.g. via
    `SQLModel.metadata.create_all()`): without `uri=true`, pysqlite resolves
    a literal filename with `os.path.abspath()` relative to the process's
    current working directory, not any `tmp_path` this test could control,
    so actually opening the connection risks creating a stray file inside
    the repo. Asserting `create_db_engine()` itself accepts the URL without
    raising -- the entire extent of what `_is_memory_sqlite_url` governs --
    is sufficient to prove the acceptance behaviour under test without
    touching the filesystem at all.
    """
    engine = create_db_engine("sqlite:///file:mem1?vfs=memdb")
    engine.dispose()


def test_file_sqlite_url_does_not_use_static_pool(tmp_path: Path) -> None:
    # File-backed SQLite is the only URL shape create_db_engine accepts, and
    # it must never use StaticPool: StaticPool means a single shared DBAPI
    # connection, which defeats transaction isolation between sessions.
    db_path = tmp_path / "file.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        assert engine.pool.__class__ is not StaticPool
    finally:
        engine.dispose()


def test_file_sqlite_engine_shares_database_across_threads(tmp_path: Path) -> None:
    """File-backed SQLite naturally shares one database across threads:
    every connection, regardless of which thread opens it, reads and writes
    the same on-disk file. This proves create_all() on the main thread
    (as the lifespan does) is visible to a session opened on a worker thread
    (as a request handler does) without needing StaticPool -- unlike the
    rejected in-memory case, where each thread would otherwise see its own
    empty database.
    """
    db_path = tmp_path / "shared.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        SQLModel.metadata.create_all(engine)

        result: dict[str, object] = {}

        def worker() -> None:
            try:
                with Session(engine) as session:
                    result["items"] = session.exec(select(MemoryItem)).all()
            except Exception as exc:  # pragma: no cover - failure path only
                result["error"] = exc

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        assert "error" not in result, result.get("error")
        assert result["items"] == []
    finally:
        engine.dispose()


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    """Every connection from create_db_engine() must enforce foreign keys:
    SQLite does not do so by default, so without `PRAGMA foreign_keys=ON`
    this insert would silently succeed instead of raising -- e.g. a `POST
    /api/items/{id}/progress` racing a committed `DELETE /api/items/{id}`
    could insert a `ProgressEntry` referencing an already-deleted
    `MemoryItem`, an orphan row no API call could ever remove.
    """
    db_path = tmp_path / "fk.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(ProgressEntry(item_id=9999, note="orphan"))
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        engine.dispose()
