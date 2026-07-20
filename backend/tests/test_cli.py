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
import shutil
import sqlite3
from pathlib import Path

import pytest
import typer
from sqlalchemy import create_engine, make_url, text
from typer.testing import CliRunner

from afterthread.cli import (
    _default_data_dir,
    _migrate_legacy_db,
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
    assert "--host" in result.stdout
    assert "--port" in result.stdout
    assert "--data-dir" in result.stdout


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


# --- legacy auto-migration (old distribution name -> afterthread) ------------
#
# Every test here monkeypatches uvicorn.run to a no-op (no server, no DB
# engine) and pre-clears DATABASE_URL/TOOLS_DIR so monkeypatch's teardown
# restores the real environment even though _serve, not the test, is what sets
# them. The default-dir tests also pin XDG_DATA_HOME at a tmp_path and clear
# AFTERTHREAD_DATA_DIR, so `_default_data_dir()` resolves to
# `<tmp_path>/afterthread` and the pre-rename default is `<tmp_path>/context-memory`.
#
# The DB-migration tests build REAL SQLite databases (not write_text fakes):
# FIX A opens + recovers + checkpoints the legacy db before moving it, so
# garbage bytes now correctly ABORT rather than migrate. The one exception is
# the DATABASE_URL-is-set test, where migration never even looks at the file.


def _read_rows(db_path: Path) -> list[str]:
    """Return the `t.x` column of a real SQLite db, opened fresh via sqlite3."""
    connection = sqlite3.connect(db_path)
    try:
        return [row[0] for row in connection.execute("SELECT x FROM t ORDER BY x")]
    finally:
        connection.close()


def _make_legacy_db(path: Path, rows: list[str], *, wal: bool = False) -> None:
    """Create a REAL SQLite database at `path` holding `rows` in a table `t(x)`.

    Plain mode (`wal=False`) leaves a single self-contained file. `wal=True`
    instead snapshots a LIVE WAL file set (db + -wal + -shm) with automatic
    checkpointing disabled and the committed rows still sitting in a non-empty
    `-wal`, so the result genuinely needs WAL recovery before the main file
    alone is complete -- exactly the integrity hazard FIX A guards. It is built
    in a scratch location and copied into place while the connection is STILL
    OPEN (the only way the -wal stays populated), after which the scratch set is
    removed.
    """
    if not wal:
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE t (x TEXT)")
            connection.executemany("INSERT INTO t (x) VALUES (?)", [(r,) for r in rows])
            connection.commit()
        finally:
            connection.close()
        return

    scratch = path.parent / f".{path.name}.building"
    connection = sqlite3.connect(scratch)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE t (x TEXT)")
        connection.executemany("INSERT INTO t (x) VALUES (?)", [(r,) for r in rows])
        connection.commit()
        # Snapshot the live set while the connection is OPEN, so the -wal still
        # holds the committed rows (closing first would checkpoint them away).
        for suffix in ("", "-wal", "-shm"):
            src = scratch.with_name(scratch.name + suffix)
            if src.exists():
                shutil.copy(src, path.with_name(path.name + suffix))
    finally:
        connection.close()
    for suffix in ("", "-wal", "-shm"):
        scratch.with_name(scratch.name + suffix).unlink(missing_ok=True)


def test_default_dir_migration_renames_legacy_dir_when_new_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # (i) Old default dir present, new default absent, XDG default in play (no
    # --data-dir / AFTERTHREAD_DATA_DIR): the old dir is renamed into place and
    # its contents survive intact.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TOOLS_DIR", raising=False)
    monkeypatch.delenv("AFTERTHREAD_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("afterthread.cli.uvicorn.run", lambda *a, **k: None)

    legacy_dir = tmp_path / "context-memory"
    legacy_dir.mkdir()
    (legacy_dir / "keepme.txt").write_text("real single-user data")
    # An .env with no old-dir absolute path in it is carried across verbatim (the
    # FIX B rewrite is a no-op when there is nothing pointing at the old dir).
    (legacy_dir / ".env").write_text("# legacy config\n")
    new_dir = tmp_path / "afterthread"

    original_cwd = os.getcwd()
    try:
        result = runner.invoke(app, [], prog_name="afterthread")
        assert result.exit_code == 0, result.output
        # Old dir moved into place as the new default; contents preserved.
        assert not legacy_dir.exists()
        assert new_dir.is_dir()
        assert (new_dir / "keepme.txt").read_text() == "real single-user data"
        assert (new_dir / ".env").read_text() == "# legacy config\n"
        assert "migrated legacy data dir" in result.stdout
        assert "rewrote absolute paths" not in result.stdout
    finally:
        os.chdir(original_cwd)


def test_default_dir_migration_skips_when_both_dirs_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # (ii) Both dirs exist: neither is touched (never merge/clobber) and the
    # new default is the one actually used.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TOOLS_DIR", raising=False)
    monkeypatch.delenv("AFTERTHREAD_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("afterthread.cli.uvicorn.run", lambda *a, **k: None)

    legacy_dir = tmp_path / "context-memory"
    legacy_dir.mkdir()
    (legacy_dir / "legacy-marker.txt").write_text("old")
    new_dir = tmp_path / "afterthread"
    new_dir.mkdir()
    (new_dir / "new-marker.txt").write_text("new")

    original_cwd = os.getcwd()
    try:
        result = runner.invoke(app, [], prog_name="afterthread")
        assert result.exit_code == 0, result.output
        # Both dirs untouched; the legacy one was not merged into the new one.
        assert (legacy_dir / "legacy-marker.txt").read_text() == "old"
        assert (new_dir / "new-marker.txt").read_text() == "new"
        assert not (new_dir / "legacy-marker.txt").exists()
        assert os.getcwd() == str(new_dir.resolve())
        assert "migrated legacy data dir" not in result.stdout
    finally:
        os.chdir(original_cwd)


def test_default_dir_migration_rename_failure_aborts_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # (FIX 1) If the legacy-dir rename fails (Path.rename patched to raise
    # OSError), startup FAILS CLOSED: it prints an ERROR with the manual remedy
    # and aborts (non-zero exit) BEFORE the mkdir below, rather than booting an
    # empty app on a fresh empty data dir while the user's real data sits under
    # the old name. Symmetric with the DB-file migration's abort. The old dir is
    # left untouched and NO new data dir is created.
    base = tmp_path.resolve()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TOOLS_DIR", raising=False)
    monkeypatch.delenv("AFTERTHREAD_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(base))
    monkeypatch.setattr("afterthread.cli.uvicorn.run", lambda *a, **k: None)

    legacy_dir = base / "context-memory"
    legacy_dir.mkdir()
    (legacy_dir / "keepme.txt").write_text("real single-user data")
    new_dir = base / "afterthread"

    def _boom(self: Path, target: object) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(Path, "rename", _boom)

    original_cwd = os.getcwd()
    try:
        result = runner.invoke(app, [], prog_name="afterthread")
        assert result.exit_code != 0
        # Old dir untouched; NO new data dir created -- abort happens before mkdir.
        assert legacy_dir.is_dir()
        assert (legacy_dir / "keepme.txt").read_text() == "real single-user data"
        assert not new_dir.exists()
        assert "ERROR could not migrate legacy data dir" in result.stdout
    finally:
        os.chdir(original_cwd)


def test_default_dir_migration_rewrites_absolute_paths_in_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # (FIX B) A legacy dir whose .env carries ABSOLUTE paths back into that old
    # dir: after the rename, those paths are repointed into the new dir so
    # nothing dangles. Both DATABASE_URL (an absolute sqlite URL) and a second
    # var (TOOLS_DIR) are rewritten; the DB the URL names moved with the dir, so
    # startup ends up pointed at a real file at the NEW location.
    base = tmp_path.resolve()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TOOLS_DIR", raising=False)
    monkeypatch.delenv("AFTERTHREAD_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(base))
    monkeypatch.setattr("afterthread.cli.uvicorn.run", lambda *a, **k: None)

    legacy_dir = base / "context-memory"
    legacy_dir.mkdir()
    new_dir = base / "afterthread"
    legacy_db = legacy_dir / "context_memory.db"
    _make_legacy_db(legacy_db, ["row-1", "row-2"])
    (legacy_dir / ".env").write_text(
        f"DATABASE_URL={_sqlite_url(legacy_db)}\nTOOLS_DIR={legacy_dir / 'tools'}\n",
        encoding="utf-8",
    )

    original_cwd = os.getcwd()
    try:
        result = runner.invoke(app, [], prog_name="afterthread")
        assert result.exit_code == 0, result.output
        assert not legacy_dir.exists()
        assert new_dir.is_dir()
        # The migrated .env text points into the NEW dir, not the old one.
        env_text = (new_dir / ".env").read_text(encoding="utf-8")
        assert str(legacy_dir) not in env_text
        assert str(new_dir) in env_text
        # DATABASE_URL loaded from that .env resolves to a real db at the new
        # location (the file moved with the dir) -- no dangling path.
        migrated_db_url = os.environ["DATABASE_URL"]
        assert migrated_db_url == _sqlite_url(new_dir / "context_memory.db")
        migrated_db = Path(make_url(migrated_db_url).database or "")
        assert migrated_db == new_dir / "context_memory.db"
        assert _read_rows(migrated_db) == ["row-1", "row-2"]
        # The second absolute var was rewritten in the same pass.
        assert os.environ["TOOLS_DIR"] == str(new_dir / "tools")
        # The DB-file migration was skipped (DATABASE_URL came from the .env), so
        # the db keeps its original filename at the new location.
        assert not (new_dir / "afterthread.db").exists()
        assert "rewrote absolute paths" in result.stdout
    finally:
        os.chdir(original_cwd)


def test_explicit_data_dir_skips_dir_migration_but_still_migrates_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # (iii) An explicit --data-dir skips the DIR migration entirely -- even with
    # a pre-rename default dir sitting right where the XDG default would land --
    # but the DB-FILE migration STILL applies inside the explicit dir. That is
    # the documented contract: the DB migration keys off "this module injects its
    # own default DATABASE_URL", not off which dir was chosen, so
    # `--data-dir <old dir>` still gets its context_memory.db renamed.
    base = tmp_path.resolve()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TOOLS_DIR", raising=False)
    monkeypatch.delenv("AFTERTHREAD_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(base))
    monkeypatch.setattr("afterthread.cli.uvicorn.run", lambda *a, **k: None)

    legacy_default = base / "context-memory"
    legacy_default.mkdir()
    (legacy_default / "marker.txt").write_text("untouched")
    explicit = base / "mydata"
    explicit.mkdir()
    _make_legacy_db(explicit / "context_memory.db", ["one", "two"])

    original_cwd = os.getcwd()
    try:
        result = runner.invoke(app, ["--data-dir", str(explicit)], prog_name="afterthread")
        assert result.exit_code == 0, result.output
        # Dir migration skipped: the XDG-default legacy dir is left alone.
        assert legacy_default.is_dir()
        assert (legacy_default / "marker.txt").read_text() == "untouched"
        assert "migrated legacy data dir" not in result.stdout
        # DB migration applied inside the explicit dir.
        assert not (explicit / "context_memory.db").exists()
        assert _read_rows(explicit / "afterthread.db") == ["one", "two"]
        assert os.getcwd() == str(explicit.resolve())
        assert "migrated legacy database" in result.stdout
    finally:
        os.chdir(original_cwd)


def test_legacy_db_file_renamed_when_default_url_injected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # (iv) When _serve injects its own default DATABASE_URL, a plain (real)
    # pre-rename context_memory.db in the data dir is migrated to afterthread.db:
    # every row is readable via sqlite3 at the new path, no old-name file is left
    # behind, and the injected URL points at the new filename.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TOOLS_DIR", raising=False)
    monkeypatch.setattr("afterthread.cli.uvicorn.run", lambda *a, **k: None)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _make_legacy_db(data_dir / "context_memory.db", ["one", "two"])

    original_cwd = os.getcwd()
    try:
        result = runner.invoke(app, ["--data-dir", str(data_dir)], prog_name="afterthread")
        assert result.exit_code == 0, result.output
        assert not (data_dir / "context_memory.db").exists()
        assert _read_rows(data_dir / "afterthread.db") == ["one", "two"]
        assert not list(data_dir.glob("context_memory.db*"))
        db_url = os.environ["DATABASE_URL"]
        assert "afterthread.db" in db_url
        assert "context_memory.db" not in db_url
        assert "migrated legacy database" in result.stdout
    finally:
        os.chdir(original_cwd)


def test_wal_legacy_db_set_is_recovered_and_fully_migrated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # (FIX A, WAL) A legacy WAL file set (db + non-empty -wal + -shm) whose
    # committed rows still live in the -wal: moving the main file ALONE would
    # lose them. Recovery + checkpoint runs first, so every committed row lands
    # at the new path and no context_memory.db* file is left behind.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TOOLS_DIR", raising=False)
    monkeypatch.setattr("afterthread.cli.uvicorn.run", lambda *a, **k: None)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rows = ["alpha", "beta", "gamma"]
    _make_legacy_db(data_dir / "context_memory.db", rows, wal=True)
    # Precondition: the committed rows are genuinely still in a non-empty -wal,
    # i.e. this set really needs recovery.
    assert (data_dir / "context_memory.db-wal").stat().st_size > 0

    original_cwd = os.getcwd()
    try:
        result = runner.invoke(app, ["--data-dir", str(data_dir)], prog_name="afterthread")
        assert result.exit_code == 0, result.output
        # Every committed row survived recovery at the new path.
        assert _read_rows(data_dir / "afterthread.db") == rows
        # The whole legacy file set is gone -- main plus any -wal/-shm sidecars.
        assert not list(data_dir.glob("context_memory.db*"))
        assert "migrated legacy database" in result.stdout
    finally:
        os.chdir(original_cwd)


def test_corrupt_legacy_db_aborts_startup_and_leaves_it_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # (FIX A, corrupt) A legacy context_memory.db that is not a real SQLite file
    # must ABORT startup (non-zero exit) rather than migrate garbage or -- worse
    # -- create a fresh empty afterthread.db beside it. The legacy file is left
    # byte-for-byte untouched so the user can recover or remove it by hand.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TOOLS_DIR", raising=False)
    monkeypatch.setattr("afterthread.cli.uvicorn.run", lambda *a, **k: None)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    garbage = b"this is not a sqlite database at all\x00\x01\x02"
    (data_dir / "context_memory.db").write_bytes(garbage)

    original_cwd = os.getcwd()
    try:
        result = runner.invoke(app, ["--data-dir", str(data_dir)], prog_name="afterthread")
        assert result.exit_code != 0
        # Legacy file untouched byte-for-byte; no empty db created beside it.
        assert (data_dir / "context_memory.db").read_bytes() == garbage
        assert not (data_dir / "afterthread.db").exists()
        assert "could not migrate legacy database" in result.stdout
    finally:
        os.chdir(original_cwd)


def test_db_file_untouched_when_database_url_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # (v) When DATABASE_URL is already set (real env var or data-dir .env), the
    # db-file migration is skipped entirely -- it never even opens the file, so a
    # write_text fake is fine here: no file is touched and the URL is honored.
    monkeypatch.delenv("TOOLS_DIR", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./operator-chosen.db")
    monkeypatch.setattr("afterthread.cli.uvicorn.run", lambda *a, **k: None)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "context_memory.db").write_text("real sqlite bytes")

    original_cwd = os.getcwd()
    try:
        result = runner.invoke(app, ["--data-dir", str(data_dir)], prog_name="afterthread")
        assert result.exit_code == 0, result.output
        # Old db left in place; no new-named db created; the URL is untouched.
        assert (data_dir / "context_memory.db").read_text() == "real sqlite bytes"
        assert not (data_dir / "afterthread.db").exists()
        assert os.environ["DATABASE_URL"] == "sqlite:///./operator-chosen.db"
        assert "migrated legacy database" not in result.stdout
    finally:
        os.chdir(original_cwd)


def test_migrate_legacy_db_missing_file_aborts_and_creates_nothing(tmp_path: Path) -> None:
    # (FIX 2) The caller guards with is_file(), but the file can vanish before
    # _migrate_legacy_db opens it. The recovery open uses mode=rw -- a URI open
    # that CANNOT create the file -- so a legacy file missing at recovery time
    # RAISES and aborts, instead of springing an empty DB into existence that
    # would then be "migrated". Unit-called directly with a nonexistent legacy
    # path (the exact is_file()-passed-then-vanished window).
    missing = tmp_path / "context_memory.db"
    db_path = tmp_path / "afterthread.db"
    assert not missing.exists()

    with pytest.raises(typer.Exit):
        _migrate_legacy_db(missing, db_path)

    # Neither path was created: mode=rw did not spring the legacy file into
    # existence, and the move step was never reached.
    assert not missing.exists()
    assert not db_path.exists()


def test_busy_checkpoint_aborts_and_leaves_legacy_set_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # (FIX 3) Another process still holding the legacy DB open must ABORT the
    # migration, not move a half-collapsed file set. Simulated by holding a
    # second connection's open read transaction on a real WAL legacy db while
    # migration runs: PRAGMA wal_checkpoint(TRUNCATE) then cannot reset the WAL,
    # reporting busy / frames-remaining, which _migrate_legacy_db now reads and
    # fails on. The legacy set is left in place and no afterthread.db is created.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TOOLS_DIR", raising=False)
    monkeypatch.setattr("afterthread.cli.uvicorn.run", lambda *a, **k: None)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    legacy_db = data_dir / "context_memory.db"
    _make_legacy_db(legacy_db, ["alpha", "beta"], wal=True)
    # Precondition: committed rows still live in a non-empty -wal.
    assert (data_dir / "context_memory.db-wal").stat().st_size > 0

    # Hold an open read transaction on the legacy db from a second connection, so
    # the migration's TRUNCATE checkpoint cannot reset the WAL. isolation_level
    # =None puts this connection in manual-transaction mode: the explicit BEGIN +
    # SELECT takes a read mark that stays held until we close in teardown.
    holder = sqlite3.connect(legacy_db)
    holder.isolation_level = None
    holder.execute("BEGIN")
    holder.execute("SELECT x FROM t").fetchall()

    original_cwd = os.getcwd()
    try:
        result = runner.invoke(app, ["--data-dir", str(data_dir)], prog_name="afterthread")
        assert result.exit_code != 0
        # Legacy set left in place; nothing collapsed or moved; no empty db beside it.
        assert legacy_db.exists()
        assert (data_dir / "context_memory.db-wal").exists()
        assert not (data_dir / "afterthread.db").exists()
        assert "could not migrate legacy database" in result.stdout
    finally:
        holder.close()
        os.chdir(original_cwd)


def test_env_rewrite_write_failure_is_tolerated_and_env_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # (FIX 4) If the migrated .env cannot be rewritten (here: the data dir is made
    # unwritable, so the atomic temp-file write / os.replace fails), the dir
    # migration itself already succeeded and startup does NOT abort on it: it
    # WARNS and leaves the .env byte-for-byte unchanged (the atomic write never
    # truncates the good file). A stale .env would only fail later at DB connect,
    # which is out of scope here since uvicorn.run is a no-op.
    base = tmp_path.resolve()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TOOLS_DIR", raising=False)
    monkeypatch.delenv("AFTERTHREAD_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(base))
    monkeypatch.setattr("afterthread.cli.uvicorn.run", lambda *a, **k: None)

    legacy_dir = base / "context-memory"
    legacy_dir.mkdir()
    # An .env that DOES carry an absolute path back into the old dir, so the
    # rewrite is actually attempted. DATABASE_URL is set here too so the DB-file
    # migration is skipped -- startup never tries to open a db in the read-only
    # dir, isolating the test on the .env-rewrite failure path.
    env_before = f"DATABASE_URL={_sqlite_url(legacy_dir / 'context_memory.db')}\n"
    (legacy_dir / ".env").write_text(env_before, encoding="utf-8")
    new_dir = base / "afterthread"

    # os.replace needs write permission on the containing DIR. chmod the legacy
    # dir 0o500 before the run: the rename carries these perms onto the new data
    # dir, so the rewrite's temp write / replace fails there. (Same-parent rename
    # needs no perms on the dir itself, so the rename still succeeds.)
    legacy_dir.chmod(0o500)

    original_cwd = os.getcwd()
    try:
        result = runner.invoke(app, [], prog_name="afterthread")
        # Startup continued past the rewrite (uvicorn.run mocked); the warning
        # fired and the .env content is byte-for-byte unchanged.
        assert "WARNING could not rewrite absolute paths" in result.stdout
        assert (new_dir / ".env").read_text(encoding="utf-8") == env_before
    finally:
        # Restore write perms so tmp_path teardown can remove the tree.
        for d in (new_dir, legacy_dir):
            if d.exists():
                d.chmod(0o700)
        os.chdir(original_cwd)
