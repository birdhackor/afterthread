"""Unit tests for the packaged-mode entry point helpers in context_memory.cli.

`_sqlite_url` is the guard against a data-dir path corrupting the database
URL: the string it returns is later re-parsed by `make_url` inside
`create_db_engine` (context_memory/db.py), so every test here round-trips
through that exact parser. The "?" case is the one a hand-rolled f-string
gets wrong SILENTLY -- the parser reads "?" as the query-string separator
and truncates the filename -- which is why it gets an engine-level test that
proves the file really lands at the intended path.
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, make_url, text

from context_memory.cli import _default_data_dir, _sqlite_url

# --- _sqlite_url -----------------------------------------------------------


def test_sqlite_url_plain_absolute_path_round_trips() -> None:
    path = Path("/data/context-memory/context_memory.db")
    parsed = make_url(_sqlite_url(path))
    assert parsed.get_backend_name() == "sqlite"
    assert parsed.database == str(path)


def test_sqlite_url_path_with_spaces_round_trips() -> None:
    path = Path("/data/my context memory/context_memory.db")
    parsed = make_url(_sqlite_url(path))
    assert parsed.get_backend_name() == "sqlite"
    assert parsed.database == str(path)


def test_sqlite_url_path_with_percent_and_hash_round_trips() -> None:
    path = Path("/data/100% memory#1/context_memory.db")
    parsed = make_url(_sqlite_url(path))
    assert parsed.get_backend_name() == "sqlite"
    assert parsed.database == str(path)


def test_sqlite_url_question_mark_path_parses_without_truncation() -> None:
    # The plain `sqlite:////abs/path` form cannot express "?" (make_url reads
    # it as the query-string separator and silently truncates the filename),
    # so _sqlite_url falls back to SQLite's URI-filename form for such paths:
    # the path travels percent-encoded and the uri=true flag tells the
    # sqlite3 driver to decode it. The parsed database is therefore the
    # file:-form -- NOT the raw path -- but it must be stable under
    # make_url (no truncation) and must not leak the "?" into the URL query.
    path = Path("/data/we?ird/context_memory.db")
    url = _sqlite_url(path)
    parsed = make_url(url)
    assert parsed.get_backend_name() == "sqlite"
    assert parsed.query == {"uri": "true"}
    assert parsed.database is not None
    assert parsed.database.startswith("file:")
    assert "?" not in parsed.database  # percent-encoded, inert to the parser
    assert "%3F" in parsed.database


def test_sqlite_url_question_mark_path_opens_the_intended_file(tmp_path: Path) -> None:
    # The gold-standard assertion for the fallback form: a real engine built
    # from the URL must open (and create) the database at the intended
    # filesystem path -- SQLite's own pragma_database_list reports the fully
    # decoded file it actually opened, the same probe create_db_engine uses.
    weird_dir = tmp_path / "we?ird dir"
    weird_dir.mkdir()
    db_path = weird_dir / "context_memory.db"

    engine = create_engine(_sqlite_url(db_path))
    try:
        with engine.connect() as connection:
            main_file = connection.execute(
                text("SELECT file FROM pragma_database_list WHERE name = 'main'")
            ).scalar()
    finally:
        engine.dispose()

    assert main_file == str(db_path)
    assert db_path.exists()


# --- _default_data_dir -----------------------------------------------------


def test_default_data_dir_honors_xdg_data_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "/custom/xdg-data")
    assert _default_data_dir() == Path("/custom/xdg-data/context-memory")


def test_default_data_dir_falls_back_to_local_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert _default_data_dir() == Path.home() / ".local" / "share" / "context-memory"


def test_default_data_dir_treats_empty_xdg_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # The XDG basedir spec: an empty XDG_DATA_HOME means "unset", so it must
    # fall back rather than produce a path under the filesystem root.
    monkeypatch.setenv("XDG_DATA_HOME", "")
    assert _default_data_dir() == Path.home() / ".local" / "share" / "context-memory"
