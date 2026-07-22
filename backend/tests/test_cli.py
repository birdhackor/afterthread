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
import stat
from importlib import resources
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
# `prog_name="afterthread"` on every invoke(): without it, CliRunner falls
# back to a placeholder program name (empirically "root", for this
# Group-shaped, callback+subcommand app) instead of the real `afterthread`
# users type, since there is no real argv[0] to read outside the installed
# console script. Passing it explicitly makes the tests reflect the real
# `afterthread ...` invocation.

runner = CliRunner()


def test_version_flag_exits_zero_and_prints_version_line() -> None:
    result = runner.invoke(app, ["--version"], prog_name="afterthread")
    assert result.exit_code == 0
    assert result.stdout.strip() == f"afterthread {_package_version()}"


def test_help_flag_exits_zero_and_mentions_the_three_options_and_init_env() -> None:
    result = runner.invoke(app, ["--help"], prog_name="afterthread")
    assert result.exit_code == 0
    # Rich inserts ANSI style boundaries inside option names when TERM enables
    # color (for example, between the two dashes in ``--host`` on CI runners).
    # Assert the user-visible text rather than the terminal control stream.
    help_text = unstyle(result.stdout)
    assert "--host" in help_text
    assert "--port" in help_text
    assert "--data-dir" in help_text
    # Pins the callback+subcommand restructure: the top-level options above
    # must survive living on `@app.callback()` instead of the old sole
    # `@app.command()`, AND the app must now list `init-env` as a real
    # subcommand -- not just accept it silently.
    assert "init-env" in help_text


def test_invalid_port_cli_value_exits_nonzero() -> None:
    # Click converts --port's value to int before _callback's body (let alone
    # _run_serve) ever runs, so this never touches the filesystem/chdir -- no
    # cwd restore needed.
    result = runner.invoke(app, ["--port", "not-an-int"], prog_name="afterthread")
    assert result.exit_code != 0


def test_invalid_port_envvar_exits_nonzero_and_never_starts_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Semantic difference from the pre-typer `_int_env`, which raised a
    # SystemExit(str) (process exit status 1) at parser-BUILD time with a
    # hand-written message. typer's envvar conversion instead fails during
    # click's own parameter resolution (before _callback's body -- let alone
    # _run_serve -- runs at all), via click's BadParameter/UsageError -- a
    # different exit status (2) and a click-authored message, but the same
    # contract that actually matters: loud failure, non-zero exit, no server
    # start. Confirmed by asserting uvicorn.run is never called below.
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
    # pre-cleared via monkeypatch (even though _run_serve, not the test, is
    # what actually sets them) so monkeypatch's teardown still restores the real
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


# --- CLI surface (typer): init-env ------------------------------------------
#
# Unlike every test above, none of these monkeypatch
# `afterthread.cli.uvicorn.run` or assert on os.getcwd()/DATABASE_URL/
# TOOLS_DIR -- that absence IS the coverage: `init-env` deliberately does
# none of `_run_serve`'s other four steps (see cli.py's module docstring), so
# a regression that made it chdir, load dotenv, or touch DATABASE_URL/
# TOOLS_DIR would leak into the surrounding test process with no monkeypatch
# to catch it, which is exactly the failure mode these tests are written to
# be sensitive to.


def test_init_env_creates_env_file_matching_the_packaged_template(tmp_path: Path) -> None:
    # Also covers "creates the data dir when missing" and its user-only mode:
    # data_dir does not exist before this call, and _ensure_data_dir is the
    # only thing `init-env` could have used to create it.
    data_dir = tmp_path / "fresh" / "data"
    assert not data_dir.exists()

    result = runner.invoke(app, ["init-env", "--data-dir", str(data_dir)], prog_name="afterthread")

    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700

    template_bytes = resources.files("afterthread").joinpath("env.example").read_bytes()
    assert (data_dir / ".env").read_bytes() == template_bytes


def test_init_env_respects_data_dir_envvar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "envvar-data"
    monkeypatch.setenv("AFTERTHREAD_DATA_DIR", str(data_dir))

    result = runner.invoke(app, ["init-env"], prog_name="afterthread")

    assert result.exit_code == 0, result.output
    assert (data_dir / ".env").is_file()


def test_init_env_refuses_to_overwrite_an_existing_env_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_path = data_dir / ".env"
    env_path.write_text("EXISTING=1\n", encoding="utf-8")

    result = runner.invoke(app, ["init-env", "--data-dir", str(data_dir)], prog_name="afterthread")

    assert result.exit_code == 1
    assert str(env_path) in result.output  # refusal names the existing path
    assert env_path.read_text(encoding="utf-8") == "EXISTING=1\n"  # left untouched


def test_init_env_force_overwrites_an_existing_env_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_path = data_dir / ".env"
    env_path.write_text("EXISTING=1\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["init-env", "--data-dir", str(data_dir), "--force"],
        prog_name="afterthread",
    )

    assert result.exit_code == 0, result.output
    template_text = (
        resources.files("afterthread").joinpath("env.example").read_text(encoding="utf-8")
    )
    assert env_path.read_text(encoding="utf-8") == template_text


def test_env_example_template_ships_inside_the_afterthread_package() -> None:
    # Packaging pin: catches the template silently vanishing from what gets
    # distributed (e.g. a pyproject.toml packaging regression) -- this is the
    # exact resource `init-env` reads via importlib.resources at runtime, so
    # this must hold in the editable/dev install pytest itself runs under;
    # e2e/wheel_smoke.sh separately re-proves it against a real built wheel.
    assert resources.files("afterthread").joinpath("env.example").is_file()


# --- CLI surface (typer): top-level options before a subcommand ------------
#
# Regression coverage for `_reject_explicit_top_level_options_before_subcommand`
# (cli.py). The trap it closes was empirically demonstrated BEFORE the guard
# existed: `afterthread --data-dir X init-env` used to exit 0, print a
# plausible "已建立 .../.env" success line, and write .env into the DEFAULT
# data dir instead of X -- `_callback` parsed --data-dir and then just
# dropped it on the subcommand path. Every test below drives the real `app`
# through `CliRunner`, exactly like the sections above; none of them stub out
# `ctx.get_parameter_source` or the guard itself -- proving the guard fires
# (or correctly does NOT fire) is only convincing against the real
# Click/typer parameter-source machinery, not a stand-in for it.
#
# `.replace("\n", "")` on the unstyled output below is deliberate, not
# cosmetic: typer/rich wraps BadParameter's message in a bordered panel at a
# fixed 80-column test width (see typer.testing's `FORCED_WIDTH = 80`), which
# can insert a hard line break in the middle of a Chinese-text run (there is
# no ASCII whitespace to hint safe break points the way there is between
# English words). Stripping newlines before substring-matching makes these
# assertions robust to exactly where the panel happens to wrap.


def test_explicit_data_dir_before_subcommand_is_rejected_not_silently_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The exact scenario the audit demonstrated: --data-dir explicitly placed
    # BEFORE `init-env` must be refused, loudly, rather than silently parsed
    # and discarded while init-env resolves an unrelated default. XDG_DATA_HOME
    # is monkeypatched to a tmp dir (never the real default_dir) purely so this
    # test can assert that default dir, too, was left untouched -- it is not
    # itself under test.
    default_dir = tmp_path / "default"
    monkeypatch.setenv("XDG_DATA_HOME", str(default_dir))
    target_dir = tmp_path / "custom"

    result = runner.invoke(
        app, ["--data-dir", str(target_dir), "init-env"], prog_name="afterthread"
    )

    assert result.exit_code == 2
    output = unstyle(result.output).replace("\n", "")
    assert "--data-dir" in output
    # Names the fix, not just the flag: the corrected invocation shape.
    assert "afterthread init-env --data-dir" in output
    # Neither the explicit target nor the (monkeypatched) default dir was
    # ever created -- the guard raises inside the Group callback, before
    # Click invokes `init-env` at all, so its body (the only code that would
    # ever call _resolve_data_dir/_ensure_data_dir/write .env) never runs.
    assert not target_dir.exists()
    assert not default_dir.exists()


def test_explicit_port_before_subcommand_is_rejected(tmp_path: Path) -> None:
    # Same trap, different top-level option: --port has no meaning for
    # init-env at all, but the point is identical -- an explicitly-typed
    # top-level value must never be silently swallowed by the callback's
    # early-return. A CORRECTLY-placed --data-dir after init-env is included
    # to prove the guard still fires on --port alone, and that init-env's own
    # (validly placed) option never gets a chance to run either.
    target_dir = tmp_path / "custom"

    result = runner.invoke(
        app,
        ["--port", "9999", "init-env", "--data-dir", str(target_dir)],
        prog_name="afterthread",
    )

    assert result.exit_code == 2
    output = unstyle(result.output).replace("\n", "")
    assert "--port" in output
    assert "afterthread init-env --port" in output
    assert not target_dir.exists()


def test_top_level_option_envvars_are_not_flagged_as_a_trap_by_init_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # AFTERTHREAD_HOST/AFTERTHREAD_PORT/AFTERTHREAD_DATA_DIR legitimately
    # configure EITHER the bare-serve path or a subcommand's own resolution
    # (init-env reads AFTERTHREAD_DATA_DIR itself, via the same _DataDirOption)
    # -- so, unlike the same options placed explicitly on the command line
    # before the subcommand (the two tests above), env-var-sourced values must
    # NOT trip the guard. All three are set at once here (stronger than
    # exercising --data-dir's envvar alone, which test_init_env_respects_data_dir_envvar
    # above already covers) to pin ParameterSource.ENVIRONMENT as fine for
    # every guarded option, not just one of them.
    data_dir = tmp_path / "envvar-data"
    monkeypatch.setenv("AFTERTHREAD_HOST", "0.0.0.0")
    monkeypatch.setenv("AFTERTHREAD_PORT", "9999")
    monkeypatch.setenv("AFTERTHREAD_DATA_DIR", str(data_dir))

    result = runner.invoke(app, ["init-env"], prog_name="afterthread")

    assert result.exit_code == 0, result.output
    assert (data_dir / ".env").is_file()


# --- CLI surface (typer): init-env hardened .env write (adversarial review) -
#
# Regression coverage for the FIX-1/FIX-2 hardening of `_init_env`'s actual
# write (cli.py: `_refuse_symlinked_env` / `_create_env_file_exclusive` /
# `_replace_env_file_atomically`), which replaced a plain `env_path.exists()`
# guard followed by `write_text()`. See cli.py's FIX-1/FIX-2 comments for the
# full rationale; these tests pin the OBSERVABLE contract: a symlinked .env
# (valid or broken) is always refused, a pre-existing regular .env is refused
# via the race-free exclusive create, every written .env ends at mode 0600
# regardless of umask/data-dir permissions, and --force never leaves a stray
# temp file behind.


@pytest.mark.parametrize(
    "kind, force",
    [
        ("valid", False),
        ("valid", True),
        ("broken", False),
        ("broken", True),
    ],
    ids=["valid-no-force", "valid-force", "broken-no-force", "broken-force"],
)
def test_init_env_refuses_a_symlinked_env_file(kind: str, force: bool, tmp_path: Path) -> None:
    # THE bug FIX-1 closes: the old guard was `env_path.exists()`, which
    # FOLLOWS a symlink and returns False for a BROKEN one -- so a broken
    # symlink used to sail straight past the refusal and the write would have
    # created the link's TARGET, outside the data dir entirely. A VALID
    # symlink plus --force used to clobber whatever the link pointed at. This
    # proves BOTH kinds are refused -- with or without --force, exit 1 -- and
    # that neither the symlink itself nor whatever it points at (the "valid"
    # case) is ever touched.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_path = data_dir / ".env"

    if kind == "valid":
        target = tmp_path / "outside-target.txt"
        target.write_bytes(b"ORIGINAL-EXTERNAL-CONTENT")
        target_before = target.read_bytes()
    else:
        target = tmp_path / "does-not-exist-anywhere"
        target_before = None
    env_path.symlink_to(target)
    link_target_before = os.readlink(env_path)

    args = ["init-env", "--data-dir", str(data_dir)]
    if force:
        args.append("--force")
    result = runner.invoke(app, args, prog_name="afterthread")

    assert result.exit_code == 1
    assert str(env_path) in result.output
    assert "符號連結" in result.output  # the symlink-specific refusal, not the generic one

    # The symlink itself is untouched: still a symlink, still pointing at the
    # exact same (possibly nonexistent) target -- checksummed via a direct
    # byte-content comparison, which is strictly stronger than a hash.
    assert env_path.is_symlink()
    assert os.readlink(env_path) == link_target_before
    if kind == "valid":
        assert target.read_bytes() == target_before
    else:
        assert not target.exists()  # broken symlink's target must stay absent


def test_init_env_exclusive_create_refuses_pre_existing_regular_file(tmp_path: Path) -> None:
    # Pins the NEW mechanism specifically, not just old behavior parity: the
    # refusal now comes from _create_env_file_exclusive's O_CREAT|O_EXCL open
    # failing with EEXIST, not a separate env_path.exists() check followed by
    # a write -- so the exclusive create must never have opened (let alone
    # truncated) the pre-existing file. A distinctive pre-set mode (0640,
    # deliberately neither the data dir's own 0700 default nor the file's own
    # eventual 0600 target) proves the file was genuinely never touched, not
    # merely left with byte-identical content by coincidence.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_path = data_dir / ".env"
    env_path.write_text("EXISTING=1\n", encoding="utf-8")
    env_path.chmod(0o640)
    before = env_path.read_bytes()

    result = runner.invoke(app, ["init-env", "--data-dir", str(data_dir)], prog_name="afterthread")

    assert result.exit_code == 1
    assert str(env_path) in result.output
    assert env_path.read_bytes() == before  # byte-unchanged
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o640  # mode untouched too


def test_init_env_fresh_create_is_mode_0600_regardless_of_umask(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    env_path = data_dir / ".env"

    old_umask = os.umask(0o000)  # maximally permissive -- proves 0600 is not umask-dependent
    try:
        result = runner.invoke(
            app, ["init-env", "--data-dir", str(data_dir)], prog_name="afterthread"
        )
    finally:
        os.umask(old_umask)

    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_init_env_force_overwrite_ends_at_mode_0600_even_in_permissive_dir(
    tmp_path: Path,
) -> None:
    # FIX-2: the FILE carries its own 0600 guarantee, never delegated to the
    # data dir's permissions -- a 0755 dir (world-traversable) and a 0644
    # pre-existing .env (world-readable) must still end at 0600 with the NEW
    # content after --force, proving the guarantee is not merely "whatever
    # the directory happened to allow". The permissive umask (0) proves the
    # same independence FIX-2 requires of the fresh-create path above.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    data_dir.chmod(0o755)
    env_path = data_dir / ".env"
    env_path.write_text("OLD=1\n", encoding="utf-8")
    env_path.chmod(0o644)

    old_umask = os.umask(0o000)
    try:
        result = runner.invoke(
            app,
            ["init-env", "--data-dir", str(data_dir), "--force"],
            prog_name="afterthread",
        )
    finally:
        os.umask(old_umask)

    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    template_bytes = resources.files("afterthread").joinpath("env.example").read_bytes()
    assert env_path.read_bytes() == template_bytes


def test_init_env_force_leaves_no_stray_temp_file_in_the_data_dir(tmp_path: Path) -> None:
    # Atomicity smoke test for `_replace_env_file_atomically`'s mkstemp +
    # os.replace pair: whatever happens along the way, the data dir must
    # contain EXACTLY `.env` afterwards -- never a leftover `.env.<rand>.tmp`
    # sibling from a half-finished write.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_path = data_dir / ".env"
    env_path.write_text("OLD=1\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["init-env", "--data-dir", str(data_dir), "--force"],
        prog_name="afterthread",
    )

    assert result.exit_code == 0, result.output
    remaining = sorted(p.name for p in data_dir.iterdir())
    assert remaining == [".env"]


# --- CLI surface (typer): serve-only AFTERTHREAD_PORT must never block a
# subcommand that does not read it (FIX-3, adversarial review) ---------------


def test_bad_port_envvar_does_not_block_init_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # THE bug FIX-3 closes: AFTERTHREAD_PORT is serve-only configuration, but
    # used to be int-converted by Click BEFORE _callback's body ever ran --
    # i.e. before Click even knew init-env (which never reads `port`) was the
    # target -- so a bad value used to block EVERY invocation, subcommand or
    # not.
    monkeypatch.setenv("AFTERTHREAD_PORT", "bad")
    data_dir = tmp_path / "data"

    result = runner.invoke(app, ["init-env", "--data-dir", str(data_dir)], prog_name="afterthread")

    assert result.exit_code == 0, result.output
    assert (data_dir / ".env").is_file()


def test_bad_port_envvar_does_not_block_init_env_help(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same bug, the exact empirical repro named in the fix: `AFTERTHREAD_PORT=
    # bad afterthread init-env --help` used to exit 2 without ever reaching
    # init-env's own --help handling.
    monkeypatch.setenv("AFTERTHREAD_PORT", "bad")

    result = runner.invoke(app, ["init-env", "--help"], prog_name="afterthread")

    assert result.exit_code == 0, result.output
    assert "--force" in unstyle(result.stdout)


def test_bare_serve_bad_port_envvar_still_exits_2_with_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The FLIP side of the two tests above: bare-serve (no subcommand) DOES
    # read `port`, so a bad AFTERTHREAD_PORT must still fail it loudly --
    # FIX-3 only moves WHEN the conversion happens, never removes it from the
    # path that actually needs it.
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        pytest.fail("uvicorn.run must not be called for a bad --port")

    monkeypatch.setattr("afterthread.cli.uvicorn.run", _fail_if_called)
    monkeypatch.setenv("AFTERTHREAD_PORT", "bad")

    result = runner.invoke(app, ["--data-dir", str(tmp_path / "data")], prog_name="afterthread")

    assert result.exit_code == 2
    assert "not a valid integer" in unstyle(result.output)


def test_bare_serve_honors_port_envvar_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Confirms the OTHER existing-coverage question FIX-3 raises: with no
    # --port flag at all, AFTERTHREAD_PORT alone must still reach uvicorn.run
    # as a real int (not just "does not crash"). Mirrors
    # test_explicit_port_flag_overrides_envvar's monkeypatch/chdir-restore
    # idiom, minus the competing --port flag.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TOOLS_DIR", raising=False)
    monkeypatch.setenv("AFTERTHREAD_PORT", "8123")

    captured: dict[str, object] = {}

    def fake_run(app_path: str, host: str, port: int) -> None:
        captured["port"] = port

    monkeypatch.setattr("afterthread.cli.uvicorn.run", fake_run)

    data_dir = tmp_path / "data"
    original_cwd = os.getcwd()
    try:
        result = runner.invoke(app, ["--data-dir", str(data_dir)], prog_name="afterthread")
        assert result.exit_code == 0, result.output
        assert captured["port"] == 8123
        assert isinstance(captured["port"], int)
    finally:
        os.chdir(original_cwd)
