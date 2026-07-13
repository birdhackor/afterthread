"""Tests for engine construction in app.db: the SQLite-only guard, the
credential-masking error message for rejected non-SQLite URLs (including the
fallback for a URL too malformed to parse at all), and the in-memory SQLite
rejection -- including SQLite's URI-filename `mode=memory` spelling -- (an
in-memory database has no legitimate use in this server -- it cannot survive
a restart, and safely sharing one across threads would require StaticPool,
which defeats transaction isolation between concurrent sessions).
"""

import threading
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select

from app.db import create_db_engine
from app.models import MemoryItem


def test_non_sqlite_url_rejected() -> None:
    with pytest.raises(RuntimeError, match="Only SQLite database URLs are supported"):
        create_db_engine("postgresql://user:pass@localhost/db")


def test_non_sqlite_url_error_message_includes_url() -> None:
    with pytest.raises(RuntimeError, match="postgres://example/db"):
        create_db_engine("postgres://example/db")


def test_non_sqlite_url_error_message_masks_password() -> None:
    """A misconfigured postgres URL must not leak its password into logs."""
    with pytest.raises(RuntimeError) as exc_info:
        create_db_engine("postgresql://user:s3cret@h/db")
    message = str(exc_info.value)
    assert "s3cret" not in message
    assert "user" in message
    assert "h/db" in message


def test_unparseable_url_error_message_leaks_nothing() -> None:
    """A DSN with no `://` separator at all (e.g. a `postgresql:` URL where
    someone forgot the slashes) cannot be parsed by SQLAlchemy's `make_url`.
    The `_mask_db_url` fallback for that case must be a fixed placeholder
    that echoes nothing from the input -- a naive `scheme = url.split("://",
    1)[0]` fallback would treat the *entire* unparseable string, including
    any embedded credentials, as the "scheme" and echo it straight back.
    """
    with pytest.raises(RuntimeError) as exc_info:
        create_db_engine("postgresql:user:s3cret@host/db")
    message = str(exc_info.value)
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
    """
    db_path = tmp_path / "realfile.db"
    engine = create_db_engine(f"sqlite:///file:{db_path}?uri=true")
    try:
        SQLModel.metadata.create_all(engine)
        assert db_path.exists()
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
