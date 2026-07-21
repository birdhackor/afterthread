"""Shared fixtures: an isolated in-memory database and a client bound to it."""

from collections.abc import Callable, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from afterthread.config import Settings
from afterthread.db import enable_sqlite_foreign_keys, get_session
from afterthread.main import app
from afterthread.services import token_budget


@pytest.fixture(autouse=True)
def _reset_token_budget() -> Generator[None]:
    """Clear the char<->token ratio window around every test.

    token_budget's rolling window is module-level, process-wide state (like
    llm_log's ring), so an observation fed by one test would otherwise leak into
    another's ratio -- most visibly the /llm/status token_ratio the status tests
    assert exactly, or the char allowance the budget-enforcement tests depend on.
    Reset before AND after so every test starts from the cold-start default and
    leaves nothing behind, independent of collection order.
    """
    token_budget._reset_for_tests()
    yield
    token_budget._reset_for_tests()


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

    ``llm_configured`` / ``generate_json`` read ``afterthread.services.llm.get_settings``
    while the status endpoint reads ``afterthread.routers.ai.get_settings``; both module
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
        for target in (
            "afterthread.services.llm.get_settings",
            "afterthread.routers.ai.get_settings",
        ):
            monkeypatch.setattr(target, lambda settings=settings: settings)
        return settings

    return _configure
