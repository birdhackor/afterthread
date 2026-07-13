"""Tests for engine construction in app.db: the SQLite-only guard and the
in-memory pooling fix (StaticPool so lifespan and request threads share one
in-memory database instead of each thread getting its own empty one).
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


def test_memory_sqlite_url_uses_static_pool() -> None:
    engine = create_db_engine("sqlite:///:memory:")
    assert engine.pool.__class__ is StaticPool


def test_bare_sqlite_url_uses_static_pool() -> None:
    engine = create_db_engine("sqlite://")
    assert engine.pool.__class__ is StaticPool


def test_file_sqlite_url_does_not_use_static_pool(tmp_path: Path) -> None:
    # File-based SQLite must be unaffected by the memory-only StaticPool fix.
    db_path = tmp_path / "file.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        assert engine.pool.__class__ is not StaticPool
    finally:
        engine.dispose()


def test_memory_sqlite_engine_shares_database_across_threads() -> None:
    """This is the bug from the code review: under the default
    SingletonThreadPool, a table created on one thread is invisible to a
    query issued from another thread (e.g. lifespan's create_all() vs. a
    request thread), raising "no such table". StaticPool fixes it by
    handing every thread the same connection.
    """
    engine = create_db_engine("sqlite:///:memory:")
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
