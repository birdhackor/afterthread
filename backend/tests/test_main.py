"""Regression tests for afterthread.main's app lifespan.

Covers the ONE operator-visible signal for ``Settings.tls_no_verify`` (a
security-relevant opt-in, see config.py): ``lifespan`` emits a single WARNING
on logger "afterthread.main" when it is set, and stays silent when it is not
(the default). Driven directly via ``asyncio.run`` (no pytest-asyncio plugin,
matching the suite) rather than TestClient, since TestClient is forbidden in
this repo (see e.g. test_tool_builder.py's own docstring for the same async
convention).
"""

import asyncio
import logging
from pathlib import Path

import pytest

from afterthread.config import Settings
from afterthread.db import get_engine
from afterthread.main import app, lifespan


def _run_lifespan_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, tls_no_verify: bool
) -> None:
    """Enter and exit ``lifespan`` once, against an isolated tmp_path database.

    Both ``afterthread.main.get_settings`` (the warning check) and
    ``afterthread.db.get_settings`` (``init_db`` -> ``get_engine``'s URL) are
    patched to the SAME Settings instance -- the two-target pattern
    conftest.py's ``configure_llm`` fixture uses for services/llm.py +
    routers/ai.py -- so the module-level bindings each file got from `from
    afterthread.config import get_settings` never disagree. The database_url
    points at a tmp_path file (matching test_db.py's
    ``test_app_lifespan_creates_configured_database_file``): ``get_engine``
    rejects in-memory SQLite, and init_db() -- lifespan's other startup
    action -- must not touch the real on-disk database this suite runs
    against. ``get_engine``'s lru_cache is cleared before (to pick up the
    patched settings) and after (so later tests never inherit this tmp
    engine).
    """
    settings = Settings(
        tls_no_verify=tls_no_verify,
        database_url=f"sqlite:///{tmp_path / 'lifespan.db'}",
    )
    monkeypatch.setattr("afterthread.main.get_settings", lambda: settings)
    monkeypatch.setattr("afterthread.db.get_settings", lambda: settings)
    get_engine.cache_clear()
    try:

        async def _enter_and_exit() -> None:
            async with lifespan(app):
                pass

        asyncio.run(_enter_and_exit())
    finally:
        get_engine().dispose()
        get_engine.cache_clear()


def test_lifespan_tls_no_verify_true_emits_one_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """TLS_NO_VERIFY on -> exactly one WARNING on "afterthread.main", pinned
    on a stable substring so a future refactor cannot silently drop the only
    operator-visible signal that outbound TLS verification is disabled."""
    with caplog.at_level(logging.WARNING, logger="afterthread.main"):
        _run_lifespan_startup(monkeypatch, tmp_path, tls_no_verify=True)

    records = [r for r in caplog.records if r.name == "afterthread.main"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "TLS_NO_VERIFY" in records[0].getMessage()


def test_lifespan_tls_no_verify_false_emits_no_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Default configuration (verification stays ON) must never emit the
    warning."""
    with caplog.at_level(logging.WARNING, logger="afterthread.main"):
        _run_lifespan_startup(monkeypatch, tmp_path, tls_no_verify=False)

    assert [r for r in caplog.records if r.name == "afterthread.main"] == []
