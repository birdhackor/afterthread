"""Shared fixtures: an isolated in-memory database and a client bound to it."""

from collections.abc import Callable, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings
from app.db import enable_sqlite_foreign_keys, get_session
from app.main import app


@pytest.fixture
def session() -> Generator[Session]:
    """A fresh in-memory SQLite database, isolated per test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # This engine is built directly rather than via create_db_engine() (which
    # rejects in-memory SQLite -- see its docstring), so it must opt into the
    # same foreign-key enforcement production gets, or tests would run under
    # laxer FK semantics than the app they're testing.
    enable_sqlite_foreign_keys(engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session
    engine.dispose()


@pytest.fixture
def client(session: Session) -> Generator[TestClient]:
    """A TestClient whose get_session dependency uses the test session.

    TestClient is intentionally not used as a context manager so the app's
    lifespan (which would initialise the real on-disk database) never runs.
    """

    def override_get_session() -> Generator[Session]:
        yield session

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def configure_llm(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Settings]:
    """Force the LLM configuration seen by both the service and the router.

    ``llm_configured`` / ``generate_json`` read ``app.services.llm.get_settings``
    while the status endpoint reads ``app.routers.ai.get_settings``; both module
    references are overridden together so their view of configuration never
    disagrees. Explicit empty defaults make "unconfigured" hermetic -- it never
    depends on ambient environment or a stray backend/.env.
    """

    def _configure(
        *,
        base_url: str = "",
        model: str = "",
        api_key: str = "",
        openai_timeout_seconds: float = 60,
    ) -> Settings:
        settings = Settings(
            openai_base_url=base_url,
            openai_api_key=api_key,
            openai_model=model,
            openai_timeout_seconds=openai_timeout_seconds,
        )
        for target in ("app.services.llm.get_settings", "app.routers.ai.get_settings"):
            monkeypatch.setattr(target, lambda settings=settings: settings)
        return settings

    return _configure
