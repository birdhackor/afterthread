"""Tests for engine construction in app.db: the SQLite-only guard and its
dialect-only (never host/database/query-string) rejection message -- including
the fallback for a URL too malformed to parse at all -- the in-memory SQLite
rejection via the runtime `pragma_database_list` probe (the sole gate on
non-persistent URLs, which catches every in-memory URL spelling by asking
SQLite itself what it opened, rather than re-deriving the answer from the URL
text) -- (an in-memory database has no legitimate use in this server -- it
cannot survive a restart, and safely sharing one across threads would require
StaticPool, which defeats transaction isolation between concurrent sessions) --
the in-memory-SQLite rejection message's own database-only (never query-string)
redaction -- and foreign-key enforcement.
"""

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select

from app.config import Settings
from app.db import create_db_engine, get_engine
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


# The runtime `pragma_database_list` probe is the sole gate on non-persistent
# SQLite URLs: it asks SQLite itself whether the database it actually opened is
# backed by a real file, so it rejects every in-memory spelling by
# construction, however the URL was written. The rejection cases below are a
# representative handful (an explicit `:memory:`, a URI-mode truthy flag,
# `mode=memory`, `vfs=memdb`, and a percent-encoded `:memory:`) -- each raises
# the same shared message fragment through that one probe -- plus the
# database-only redaction of that message and the accepted file/URI-file cases.


def test_memory_sqlite_url_rejected() -> None:
    """An explicit `:memory:` database is rejected: this is a persistence
    app, so the server has no legitimate in-memory mode.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///:memory:")


def test_non_persistent_sqlite_error_omits_query_string() -> None:
    """The in-memory-SQLite rejection message must never echo the URL's query
    string, only the parsed `database`/filename component. SQLite's
    URI-filename form (see `test_sqlite_uri_mode_memory_rejected` below) lets
    an encrypted-SQLite driver (e.g. SQLCipher) carry a passphrase in a
    `key=` query parameter -- unlike a URL's `password` *component*, which
    `render_as_string(hide_password=True)` masks, a query parameter is not
    covered by that masking, so a naive full-URL render would leak it
    straight into this error message (and from there, into logs). The
    `database` component itself (`file:mem` here) remains fine to include:
    this URL is already confirmed to be SQLite, which has no
    username/password/host/port component to begin with.
    """
    with pytest.raises(RuntimeError) as exc_info:
        create_db_engine("sqlite:///file:mem?mode=memory&uri=true&key=s3cret")
    message = str(exc_info.value)
    assert "s3cret" not in message
    assert "key=" not in message
    assert "file:mem" in message


def test_sqlite_uri_mode_memory_rejected() -> None:
    """SQLite's URI-filename form (`file:name?mode=memory...`, see
    https://www.sqlite.org/uri.html) parses with a non-empty `database`
    (`file:memdb1`) and contains no literal `:memory:` substring anywhere in
    the URL, yet `mode=memory` in its query string still opens an in-memory
    (optionally named, shared-cache) database. The probe rejects it exactly
    like the plain `:memory:` spelling.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:memdb1?mode=memory&cache=shared&uri=true")


def test_sqlite_uri_empty_filename_rejected() -> None:
    """`sqlite:///file:?uri=true` -- nothing between `file:` and `?` -- is
    SQLite's documented spelling for a private, anonymous on-disk database
    scoped to the single connection that opens it: the same
    unsafe-to-share-across-requests problem as an explicit `:memory:`, just
    reached through the URI form with an empty filename. This is the
    representative URI-mode (`uri=...` truthy) spelling; the probe catches
    every truthy variant (`uri=1`, `uri=True`, `uri=y`, `uri=t`, a duplicated
    `uri=false&uri=false`, an authority-form `file://`) identically, since it
    reads what SQLite opened rather than how the flag was spelled.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:?uri=true")


def test_sqlite_uri_vfs_memdb_rejected() -> None:
    """`vfs=memdb` in a URI-form SQLite URL selects SQLite's in-memory VFS
    outright, regardless of what the filename portion says -- e.g.
    `sqlite:///file:mem1?vfs=memdb&uri=true` opens an in-memory database
    named `mem1`, not a file called `mem1`. The probe rejects it exactly like
    `mode=memory`.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:mem1?vfs=memdb&uri=true")


def test_sqlite_uri_percent_encoded_memory_path_rejected() -> None:
    """`file:%3Amemory%3A` percent-encodes `:memory:` (`%3A` is `:`) as the
    URI path, so the literal string appears only in its *encoded* form in the
    URL text. Percent-decoding happens only once SQLite's own URI-filename
    parser runs, at connection time (see https://www.sqlite.org/uri.html),
    resolving the path to the literal string `:memory:` -- which SQLite
    special-cases as a private, temporary in-memory database. Catching this
    from the URL text alone would require decoding it first; the runtime probe
    catches it for free, since `pragma_database_list` reports an empty `file`
    for whatever SQLite ultimately opened.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:%3Amemory%3A?uri=true")


def test_sqlite_filename_containing_memory_substring_is_allowed(tmp_path: Path) -> None:
    """A genuine on-disk file whose name merely *contains* the text
    ":memory:" -- e.g. "notes:memory:.db" -- must remain allowed: SQLite
    treats only the literal, exact filename ":memory:" as its special
    in-memory database, not any path that happens to contain that substring.
    The runtime probe confirms this file is real (it reports a non-empty
    `file` path), so the engine is accepted and the file is created.
    """
    db_path = tmp_path / "notes:memory:.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        SQLModel.metadata.create_all(engine)
        assert db_path.exists()
    finally:
        engine.dispose()


def test_sqlite_uri_file_backed_without_mode_memory_is_allowed(tmp_path: Path) -> None:
    """A URI-form SQLite filename *without* `mode=memory` genuinely addresses
    a durable file and must remain allowed. Uses an absolute path inside
    `tmp_path` (rather than relying on the process's cwd) so the database
    file cannot land in the repo. This URL has `uri=true` in effect and a
    non-empty filename, so the probe reports a non-empty `file` path for it
    and accepts it.
    """
    db_path = tmp_path / "realfile.db"
    engine = create_db_engine(f"sqlite:///file:{db_path}?uri=true")
    try:
        SQLModel.metadata.create_all(engine)
        assert db_path.exists()
    finally:
        engine.dispose()


def test_pragma_database_list_reports_empty_file_for_memdb_vfs() -> None:
    """Empirical verification of the mechanism the runtime probe relies on:
    its single "is the `file` column empty" check catches `vfs=memdb` on its
    own. SQLite's `memdb` VFS opens a database that is *addressable* by the
    name in its URI, so multiple connections can share it -- which might
    suggest `pragma_database_list` could report that name back as a "file" --
    but empirically it does not. A `memdb`-VFS database is never backed by a
    real path on disk, so SQLite reports the same empty `file` column for it
    as for `:memory:` or an anonymous temp database. That confirms no second
    runtime signal (e.g. `PRAGMA journal_mode`) is needed alongside the
    probe's empty-path check.
    """
    engine = create_engine(
        "sqlite:///file:probetest?vfs=memdb&uri=true",
        connect_args={"check_same_thread": False},
    )
    try:
        with engine.connect() as connection:
            main_file = connection.execute(
                text("SELECT file FROM pragma_database_list WHERE name = 'main'")
            ).scalar()
        assert main_file == ""
    finally:
        engine.dispose()


def test_default_style_relative_sqlite_url_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact URL shape `Settings.database_url` defaults to --
    `sqlite:///./context_memory.db`, a relative, non-URI path -- must be
    accepted end-to-end, including by the runtime probe: `pragma_database_list`
    must report a non-empty (absolute) path for it, not just "no exception
    was raised". cwd is redirected into `tmp_path` so the relative
    `./context_memory.db` resolves there rather than into the repo.
    """
    monkeypatch.chdir(tmp_path)
    engine = create_db_engine("sqlite:///./context_memory.db")
    try:
        assert (tmp_path / "context_memory.db").exists()
    finally:
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


def test_importing_app_creates_no_database_file(tmp_path: Path) -> None:
    """Importing app.main / app.db must have NO filesystem side effect. The
    engine is created lazily (see app.db.get_engine), not at module import, so
    a read-only checkout can be imported -- e.g. during pytest collection --
    without create_db_engine's persistence probe materialising a database file
    in the process cwd. Run in a subprocess whose cwd is an empty tmp_path, so
    any stray relative-path DB file would land -- and be caught -- there rather
    than in the repo. Regression guard for the import-time
    `engine = create_db_engine(...)` this replaced.
    """
    backend_dir = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(backend_dir)}
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.glob("*.db")) == []


def test_app_lifespan_creates_configured_database_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persistence probe / file creation now happens at STARTUP -- via the
    app lifespan (init_db -> get_engine) -- not at import. Point the lazy engine
    at a tmp_path file, confirm merely constructing/importing the app leaves it
    absent, then run the lifespan (TestClient used as a context manager) and
    confirm the file now exists under the configured path.

    get_engine is lru_cached, so its cache is cleared before (to pick up the
    patched settings) and after (so later tests never inherit this tmp engine).
    """
    db_path = tmp_path / "lifespan.db"
    settings = Settings(database_url=f"sqlite:///{db_path}")
    monkeypatch.setattr("app.db.get_settings", lambda: settings)
    get_engine.cache_clear()
    try:
        from app.main import app

        # Importing/constructing the app must not have created the file yet.
        assert not db_path.exists()
        # Entering the context manager runs the lifespan startup -> init_db ->
        # get_engine, which builds the engine, runs the probe, and creates the
        # file under the configured path.
        with TestClient(app):
            assert db_path.exists()
    finally:
        get_engine().dispose()
        get_engine.cache_clear()
