"""Shared fixtures: an isolated in-memory database and a client bound to it."""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session
from app.main import app


@pytest.fixture
def session() -> Generator[Session]:
    """A fresh in-memory SQLite database, isolated per test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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
