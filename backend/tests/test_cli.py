"""Unit tests for the packaged-mode entry point helpers in afterthread.cli.

`_sqlite_url` is the guard against a data-dir path corrupting the database
URL: the string it returns is later re-parsed by `make_url` inside
`create_db_engine` (afterthread/db.py), so every test here round-trips
through that exact parser. The "?" case is the one a hand-rolled f-string
gets wrong SILENTLY -- the parser reads "?" as the query-string separator
and truncates the filename -- which is why it gets an engine-level test that
proves the file really lands at the intended path.

The CLI-surface tests further down drive the typer `app` directly through
`typer.testing.CliRunner`, exactly as
https://typer.tiangolo.com/tutorial/testing/ recommends -- never `main()`,
which is just the console-script shim (`app()` reading `sys.argv`) and gives
a test no way to capture argv/exit code/output. `uvicorn.run` is always
monkeypatched: no test here ever binds a real socket or starts a real
server.
"""

import os
from pathlib import Path

import pytest
from click import unstyle
from sqlalchemy import create_engine, make_url, text
from typer.testing import CliRunner

from afterthread.cli import (
    _default_data_dir,
    _package_version,
    _sqlite_url,
    app,
)

# --- _sqlite_url -----------------------------------------------------------


def test_sqlite_url_plain_absolute_path_round_trips() -> None:
    path = Path("/data/afterthread/afterthread.db")
    parsed = make_url(_sqlite_url(path))
    assert parsed.get_backend_name() == "sqlite"
    assert parsed.database == str(path)


def test_sqlite_url_path_with_spaces_round_trips() -> None:
    path = Path("/data/my afterthread dir/afterthread.db")
    parsed = make_url(_sqlite_url(path))
    assert parsed.get_backend_name() == "sqlite"
    assert parsed.database == str(path)


def test_sqlite_url_path_with_percent_and_hash_round_trips() -> None:
    path = Path("/data/100% memory#1/afterthread.db")
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
    path = Path("/data/we?ird/afterthread.db")
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
    db_path = weird_dir / "afterthread.db"

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
    assert _default_data_dir() == Path("/custom/xdg-data/afterthread")


def test_default_data_dir_falls_back_to_local_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert _default_data_dir() == Path.home() / ".local" / "share" / "afterthread"


def test_default_data_dir_treats_empty_xdg_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # The XDG basedir spec: an empty XDG_DATA_HOME means "unset", so it must
    # fall back rather than produce a path under the filesystem root.
    monkeypatch.setenv("XDG_DATA_HOME", "")
    assert _default_data_dir() == Path.home() / ".local" / "share" / "afterthread"


# --- CLI surface (typer) -----------------------------------------------------
#
# `prog_name="afterthread"` on every invoke(): without it, CliRunner
# derives the usage line's program name from the command function itself
# (`_serve` -> a stray "-serve"), since there is no real argv[0] to read
# outside the installed console script. Passing it explicitly makes the
# tests reflect the real `afterthread ...` invocation.

runner = CliRunner()


def test_version_flag_exits_zero_and_prints_version_line() -> None:
    result = runner.invoke(app, ["--version"], prog_name="afterthread")
    assert result.exit_code == 0
    assert result.stdout.strip() == f"afterthread {_package_version()}"


def test_help_flag_exits_zero_and_mentions_the_three_options() -> None:
    result = runner.invoke(app, ["--help"], prog_name="afterthread")
    assert result.exit_code == 0
    # Rich inserts ANSI style boundaries inside option names when TERM enables
    # color (for example, between the two dashes in ``--host`` on CI runners).
    # Assert the user-visible text rather than the terminal control stream.
    help_text = unstyle(result.stdout)
    assert "--host" in help_text
    assert "--port" in help_text
    assert "--data-dir" in help_text


def test_invalid_port_cli_value_exits_nonzero() -> None:
    # Click converts --port's value to int before _serve ever runs, so this
    # never touches the filesystem/chdir -- no cwd restore needed.
    result = runner.invoke(app, ["--port", "not-an-int"], prog_name="afterthread")
    assert result.exit_code != 0


def test_invalid_port_envvar_exits_nonzero_and_never_starts_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Semantic difference from the pre-typer `_int_env`, which raised a
    # SystemExit(str) (process exit status 1) at parser-BUILD time with a
    # hand-written message. typer's envvar conversion instead fails during
    # click's own parameter resolution (before _serve runs at all), via
    # click's BadParameter/UsageError -- a different exit status (2) and a
    # click-authored message, but the same contract that actually matters:
    # loud failure, non-zero exit, no server start. Confirmed by asserting
    # uvicorn.run is never called below.
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        "afterthread.cli.uvicorn.run",
        lambda *a, **k: calls.append((a, k)),
    )
    monkeypatch.setenv("AFTERTHREAD_PORT", "not-an-int")

    result = runner.invoke(app, ["--data-dir", str(tmp_path / "data")], prog_name="afterthread")

    assert result.exit_code != 0
    assert calls == []


def test_explicit_port_flag_overrides_envvar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Proves CLI > env precedence without ever launching uvicorn: uvicorn.run
    # is monkeypatched to just capture its kwargs. DATABASE_URL/TOOLS_DIR are
    # pre-cleared via monkeypatch (even though _serve, not the test, is what
    # actually sets them) so monkeypatch's teardown still restores the real
    # environment afterward -- monkeypatch reverts a key to whatever it
    # recorded when FIRST asked about that key, regardless of what changed it
    # in between, but only for keys it was told about at least once.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TOOLS_DIR", raising=False)
    monkeypatch.setenv("AFTERTHREAD_PORT", "9999")

    captured: dict[str, object] = {}

    def fake_run(app_path: str, host: str, port: int) -> None:
        captured["app_path"] = app_path
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr("afterthread.cli.uvicorn.run", fake_run)

    data_dir = tmp_path / "data"
    original_cwd = os.getcwd()
    try:
        result = runner.invoke(
            app,
            ["--port", "1234", "--data-dir", str(data_dir)],
            prog_name="afterthread",
        )
        assert result.exit_code == 0, result.output
        assert captured["port"] == 1234  # explicit --port wins over AFTERTHREAD_PORT=9999
        assert captured["host"] == "127.0.0.1"
        assert captured["app_path"] == "afterthread.main:app"
        assert os.getcwd() == str(data_dir.resolve())
    finally:
        os.chdir(original_cwd)
