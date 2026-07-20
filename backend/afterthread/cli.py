"""Console entry point for packaged (`uvx afterthread` / `afterthread`) mode.

Development never goes through this module: `backend/README.md`'s documented
dev workflow runs `uv run uvicorn afterthread.main:app` directly, so dev
behavior -- CWD-relative `backend/.env`, CWD-relative default `DATABASE_URL`,
no data directory at all -- is completely unchanged by anything below (see
`afterthread/config.py` and D05/D06 in `docs/web-v2-decisions.md`).

This module exists because a `uvx`-installed tool is launched from an
arbitrary CWD, with no project checkout nearby to hold a `.env` or a
CWD-relative SQLite file the way a dev checkout does -- there is no
"correct" directory for either to default to. So packaged-mode defaults are
decided in exactly one place, kept out of `afterthread.config` entirely so
`Settings`'s own defaults (and every existing test around them) are
untouched by packaging:

  1. Resolve a per-user data directory (`--data-dir` / `AFTERTHREAD_DATA_DIR`
     / an XDG-style default) and make sure it exists (created user-only).
  2. `os.chdir` into it: the data dir is packaged mode's home directory.
     Every CWD-relative behavior downstream then resolves inside the data
     dir rather than whatever directory the user happened to launch from --
     pydantic-settings' CWD-relative `env_file=".env"` lookup (config.py),
     and any relative path in a setting value (e.g. a
     `DATABASE_URL=sqlite:///./afterthread.db` copied straight from
     .env.example). That closes two whole classes of surprises: a stray
     `.env` sitting in an arbitrary launch CWD silently configuring the
     server, and relative DB paths scattering database files across launch
     directories.
  3. Load `<data-dir>/.env` into the process environment, if present --
     without overriding a variable that is already set, so an explicit env
     var always wins over the file (the same precedence pydantic-settings
     itself gives env vars over `backend/.env` in dev).
  4. Default `DATABASE_URL` to a file inside that data directory, unless the
     environment already supplies one.
  5. Hand off to `uvicorn.run`, serving the same `afterthread.main:app`
     dev uses.

This module also one-time auto-migrates real data left under the tool's
previous distribution name, as two INDEPENDENT steps with DIFFERENT triggers
(a common miswording is that both skip under an explicit data dir -- only the
first does):

  * Data-dir rename (`<xdg base>/context-memory` -> `<xdg base>/afterthread`):
    ONLY on the XDG-default path -- neither --data-dir nor AFTERTHREAD_DATA_DIR
    given -- and ONLY when the new dir does not yet exist. If BOTH dirs exist
    the old one is left untouched (never merged into the new one). A failed
    rename warns and continues without migrating, leaving the old dir in place.
    On success, any absolute path inside the migrated `.env` that still names
    the old directory is rewritten to the new one, so a `DATABASE_URL` /
    `TOOLS_DIR` / `LLM_LOG_FILE` carrying that path does not dangle.
  * Legacy DB-file rename (`context_memory.db` -> `afterthread.db`): runs
    whenever this module injects its OWN default `DATABASE_URL` -- i.e. only
    when `DATABASE_URL` is absent after the data-dir `.env` is loaded -- and
    only when `afterthread.db` does not yet exist. Deliberately INDEPENDENT of
    --data-dir: `--data-dir <old dir>` still gets its DB renamed. Because a
    SQLite database is a file SET (main + -wal/-shm/-journal), the legacy file
    is first opened to force WAL/journal recovery and checkpointed into the one
    main file, then moved with a no-replace link; ANY failure (a corrupt or
    locked DB, or a sidecar that will not collapse) ABORTS startup rather than
    stranding the user's data beside a fresh empty database.

Each step prints a one-line notice when it fires.
"""

import os
import sqlite3
from importlib import metadata
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import typer
import uvicorn
from dotenv import load_dotenv
from sqlalchemy import make_url
from sqlalchemy.engine import URL

_PACKAGE_NAME = "afterthread"

# add_completion=False: this is a single-command server launcher, not a CLI
# suite worth shell-completion machinery for -- it would only add
# --install-completion/--show-completion noise to --help.
app = typer.Typer(add_completion=False)


def _package_version() -> str:
    """Return the installed distribution version, or "unknown" if absent.

    Falls back instead of raising so `--version` can never crash: the only
    realistic way distribution metadata is missing is an unusual/broken
    install, which is exactly when a clear "unknown" beats a traceback.
    """
    try:
        return metadata.version(_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return "unknown"


def _default_data_dir() -> Path:
    """The XDG-style default data directory.

    `$XDG_DATA_HOME/afterthread`, falling back to
    `~/.local/share/afterthread` when unset. Covers Linux and macOS
    without adding a `platformdirs` dependency for a single-purpose lookup
    (see D05 in docs/web-v2-decisions.md).
    """
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return base / "afterthread"


def _sqlite_url(db_path: Path) -> str:
    """Build a SQLAlchemy SQLite URL for `db_path` that parses back losslessly.

    Constructed via SQLAlchemy's own `URL.create` rather than a hand-rolled
    f-string, and verified against `make_url` -- the exact parser
    `create_db_engine` (afterthread/db.py) later runs on it -- so a data
    dir containing URL-significant characters can never silently truncate or
    corrupt the database filename.

    Most special characters (spaces, "#", "%", non-ASCII) survive the plain
    `sqlite:////abs/path` form verbatim. "?" does not: `render_as_string`
    emits it raw, and `make_url` then reads it as the query-string separator,
    silently cutting the filename short (empirically:
    "/tmp/we?ird/x.db" parses back as database="/tmp/we"). For such paths
    this falls back to SQLite's URI-filename form
    (`sqlite:///file:<percent-encoded path>?uri=true`), where the path
    travels percent-encoded -- inert to the URL parser -- and sqlite itself
    decodes it when opening the file. Both forms were verified end-to-end
    against a real engine (pragma_database_list reports the intended path,
    the file lands where expected); the URI form is additionally covered by
    tests/test_cli.py.
    """
    path_str = str(db_path)
    plain = URL.create(drivername="sqlite", database=path_str).render_as_string(hide_password=False)
    if make_url(plain).database == path_str:
        return plain
    return URL.create(
        drivername="sqlite",
        database=f"file:{quote(path_str, safe='/')}",
        query={"uri": "true"},
    ).render_as_string(hide_password=False)


def _version_callback(value: bool) -> None:
    """Print the version and exit 0 -- typer's eager-option idiom for --version.

    `is_eager=True` on the `--version` option below (see `_serve`) makes this
    callback run before any other option is converted/validated, matching the
    old `argparse` `action="version"`: `--version` alone always works, even if
    some other flag on the same command line would otherwise be invalid.
    """
    if value:
        typer.echo(f"{_PACKAGE_NAME} {_package_version()}")
        raise typer.Exit()


def _rewrite_env_absolute_paths(env_file: Path, old_dir: Path, new_dir: Path) -> None:
    """Repoint absolute paths in a `.env` that moved with a data-dir rename.

    The `.env` travels with the renamed directory, but any ABSOLUTE path inside
    it -- `DATABASE_URL`, `TOOLS_DIR`, `LLM_LOG_FILE`, anything -- still names
    the OLD directory and would dangle. Rewrite every occurrence of the old
    dir's path string to the new one in a single pass, so all such variables
    follow the data to its new home. A relative path needs no fixup: its target
    moved along with the directory.

    A read/decode failure is a warning, not a crash: the directory migration
    itself already succeeded, and a `.env` this cannot read is the operator's
    to check by hand.
    """
    if not env_file.is_file():
        return
    try:
        env_text = env_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(
            f"afterthread: WARNING could not rewrite absolute paths in {env_file} "
            f"({exc}); check it by hand if it still points at {old_dir}",
            flush=True,
        )
        return
    old_dir_str = str(old_dir)
    if old_dir_str not in env_text:
        return
    env_file.write_text(env_text.replace(old_dir_str, str(new_dir)), encoding="utf-8")
    print(
        f"afterthread: rewrote absolute paths in {env_file} ({old_dir} -> {new_dir})",
        flush=True,
    )


def _migrate_legacy_db(legacy_db_path: Path, db_path: Path) -> None:
    """Recover, then move, a pre-rename `context_memory.db` to `afterthread.db`.

    A SQLite database is a FILE SET -- the main file plus transient
    `-wal`/`-shm`/`-journal` sidecars -- so moving only the main file can drop
    committed-but-uncheckpointed WAL transactions or strand a hot rollback
    journal. This first opens the legacy database through the stdlib `sqlite3`
    driver, where a real read forces hot-journal rollback / WAL replay, then
    `PRAGMA wal_checkpoint(TRUNCATE)` folds the WAL back into the single main
    file; if a `-wal`/`-journal` sidecar still holds data afterward the set is
    torn, and this fails rather than moving half of it. Only then is the main
    file moved, via a no-replace `os.link` + `unlink` so an `afterthread.db`
    that appeared since the caller's pre-check is never clobbered (a filesystem
    without hard-link support falls back to the caller's exists()-guarded
    rename).

    ANY failure here -- a corrupt or locked database, or a sidecar that will
    not collapse -- ABORTS startup with `typer.Exit(1)` and a manual remedy,
    NEVER falling through to create a fresh empty `afterthread.db` beside the
    stranded legacy data. This matches the codebase's fail-loudly-at-startup
    convention (see config.py's validation comments).
    """
    try:
        connection = sqlite3.connect(legacy_db_path, timeout=5)
        try:
            # A real read forces recovery: sqlite rolls back a hot journal and
            # replays a WAL before it can answer. Result intentionally unused.
            connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchall()
            # Fold the WAL back into the main file and truncate it away, so the
            # one file moved below is self-contained.
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        for suffix in ("-wal", "-journal"):
            sidecar = legacy_db_path.with_name(legacy_db_path.name + suffix)
            if sidecar.exists() and sidecar.stat().st_size > 0:
                raise RuntimeError(
                    f"{sidecar.name} still holds uncheckpointed data after checkpoint"
                )
        try:
            # No-replace move: os.link never overwrites an existing target, so an
            # afterthread.db that appeared between the caller's exists() pre-check
            # and here is preserved, not clobbered.
            os.link(legacy_db_path, db_path)
        except FileExistsError:
            # Another launch (or a freshly created db) won the race: afterthread.db
            # now exists. Leave the legacy file in place and skip -- the new-named
            # db is authoritative for this launch.
            print(
                f"afterthread: {db_path} already exists; left legacy database "
                f"{legacy_db_path} in place (skipped migration this launch)",
                flush=True,
            )
            return
        except OSError:
            # Hard links unsupported here (both paths share a directory, so this
            # is never cross-device -- e.g. a filesystem without link support, or
            # EPERM). Fall back to the caller's exists()-guarded rename.
            legacy_db_path.rename(db_path)
        else:
            legacy_db_path.unlink()
    except Exception as exc:
        print(
            f"afterthread: ERROR could not migrate legacy database {legacy_db_path} "
            f"-> {db_path}: {exc}\n"
            f"  A SQLite database is a file set (the .db plus any "
            f"-wal/-shm/-journal sidecars). With the server stopped, move the whole "
            f"set to {db_path} yourself, or -- if you do not need the old data -- "
            f"delete {legacy_db_path} and its sidecars. afterthread will not start "
            f"until the legacy file is dealt with, so it never creates an empty "
            f"database beside your real data.",
            flush=True,
        )
        raise typer.Exit(1) from exc
    print(
        f"afterthread: migrated legacy database {legacy_db_path} -> {db_path}",
        flush=True,
    )


@app.command(help="Run the afterthread web app (bundled API + SPA).")
def _serve(
    host: Annotated[
        str,
        typer.Option(
            "--host",
            envvar="AFTERTHREAD_HOST",
            help=(
                "Interface to bind. Left at 127.0.0.1 by default: this is a "
                "local single-user tool, not a service meant to be exposed "
                "on the LAN."
            ),
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            envvar="AFTERTHREAD_PORT",
            help="Port to bind.",
        ),
    ] = 8000,
    data_dir: Annotated[
        str | None,
        typer.Option(
            "--data-dir",
            envvar="AFTERTHREAD_DATA_DIR",
            show_default=False,
            help=(
                "Directory holding the SQLite database and an optional .env "
                "file (default: an XDG-style per-user data directory)."
            ),
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="show program's version number and exit",
        ),
    ] = False,
) -> None:
    """Prepare the data directory/environment, then serve.

    See the module docstring for why this logic lives here rather than in
    `afterthread.config`.
    """
    # `data_dir` is None when neither --data-dir nor AFTERTHREAD_DATA_DIR
    # was given, and "" when AFTERTHREAD_DATA_DIR is set but empty -- both
    # fall back to the XDG-style default here, the same rule
    # `_default_data_dir` itself applies to a set-but-empty XDG_DATA_HOME.
    #
    # Resolve BEFORE the chdir below, so a relative --data-dir is anchored to
    # the directory the user launched from, as they would expect.
    data_dir_path = Path(data_dir or str(_default_data_dir())).expanduser().resolve()

    # Legacy default-dir auto-migration (pre-rename distribution name ->
    # afterthread). ONLY when the XDG default is in play -- i.e. neither
    # --data-dir nor AFTERTHREAD_DATA_DIR was provided (`data_dir` falsy) -- and
    # ONLY when the new default dir does not yet exist, BEFORE the mkdir/chdir
    # below: if the old default dir "<xdg base>/context-memory" is present, move
    # it into place so a user with real single-user data under the previous name
    # keeps it with no manual step. An explicit data dir is skipped entirely (it
    # is the user's own path, unrelated to the rename).
    #
    # No os.link no-clobber dance is needed here the way it is for the DB file
    # below. The guard already requires the new dir to be ABSENT; and even under
    # a TOCTOU race, POSIX rename cannot replace a NON-empty directory (it fails
    # with ENOTEMPTY), so this can never merge into or clobber real data in a
    # pre-existing new dir -- while replacing an empty, just-created new dir
    # loses nothing. On failure (rename raised OSError -- cross-device,
    # permissions, or that ENOTEMPTY) we warn and continue WITHOUT migrating: the
    # old dir is left untouched, and the mkdir below just creates a fresh empty
    # new dir, which loses nothing since a rename that never ran moved nothing.
    if not data_dir and not data_dir_path.exists():
        legacy_data_dir = data_dir_path.parent / "context-memory"
        if legacy_data_dir.is_dir():
            try:
                legacy_data_dir.rename(data_dir_path)
            except OSError as exc:
                print(
                    f"afterthread: WARNING could not migrate legacy data dir "
                    f"{legacy_data_dir} -> {data_dir_path} ({exc}); leaving it in "
                    f"place -- move it there yourself to migrate your data",
                    flush=True,
                )
            else:
                print(
                    f"afterthread: migrated legacy data dir {legacy_data_dir} -> {data_dir_path}",
                    flush=True,
                )
                # The .env moved with the dir; repoint any absolute path inside
                # it that still names the old directory (FIX B).
                _rewrite_env_absolute_paths(data_dir_path / ".env", legacy_data_dir, data_dir_path)

    created = not data_dir_path.exists()
    data_dir_path.mkdir(parents=True, exist_ok=True)
    if created:
        # This directory holds personal memory content (the SQLite database)
        # and, via its .env, possibly an API key -- so a directory this tool
        # itself just created defaults to user-only. An already-existing
        # directory is left untouched: its permissions may be a deliberate
        # choice, and silently rewriting them is not this tool's call.
        data_dir_path.chmod(0o700)

    # The data dir is packaged mode's home directory (see the module
    # docstring, step 2): from here on every CWD-relative behavior --
    # pydantic-settings' env_file=".env" lookup, any relative path in a
    # setting value -- resolves inside the data dir, not wherever the user
    # happened to launch from.
    os.chdir(data_dir_path)

    env_file = data_dir_path / ".env"
    if env_file.is_file():
        # After the chdir above, this is the very file pydantic-settings' own
        # CWD-relative env_file=".env" lookup (config.py) would already find.
        # It is still loaded explicitly, absolute path and all: it puts the
        # packaged-mode contract -- "<data-dir>/.env is loaded" -- in code
        # rather than leaving it an emergent property of the chdir, and it
        # makes the values visible to ANY os.environ reader in the process,
        # not just Settings. override=False: a real environment variable
        # always wins over the file, matching the precedence pydantic-settings
        # itself gives env vars over `backend/.env` in dev.
        load_dotenv(env_file, override=False)

    # Never echo DATABASE_URL itself to stdout: a user-supplied URL can carry
    # credentials (afterthread/db.py's rejection messages already follow
    # this discipline -- see _url_dialect/_url_database there for the threat
    # model), and a startup banner is exactly the kind of line that ends up
    # in terminal scrollback and pasted logs. When this entry point injects
    # its own default, the value is by construction a bare file path, so THAT
    # is printed; when the environment already provides one, only its origin
    # is named, never its content.
    if "DATABASE_URL" not in os.environ:
        db_path = data_dir_path / "afterthread.db"
        legacy_db_path = data_dir_path / "context_memory.db"
        # Legacy DB-file auto-migration (pre-rename "context_memory.db" ->
        # "afterthread.db"), only on the path that injects our OWN default URL
        # below: if the old-named database still sits in this data dir and the
        # new-named one does not, migrate it so the default URL points at the
        # user's real data instead of creating a fresh empty database beside it.
        # This runs REGARDLESS of --data-dir; only DATABASE_URL already being set
        # (a real env var, or a data-dir .env) suppresses it -- an
        # operator-supplied URL is authoritative and no file is then touched. The
        # `not db_path.exists()` check is only a fast path: _migrate_legacy_db
        # recovers the SQLite file set and moves it with a no-replace link,
        # aborting startup on any failure (see its docstring).
        if legacy_db_path.is_file() and not db_path.exists():
            _migrate_legacy_db(legacy_db_path, db_path)
        os.environ["DATABASE_URL"] = _sqlite_url(db_path)
        database_display = str(db_path)
    else:
        database_display = "(from DATABASE_URL environment variable)"

    # Default the tool-package directory into the data dir, exactly as
    # DATABASE_URL above -- a uvx install then has the tool feature ON at a
    # stable per-user location (`<data-dir>/tools`) without the operator setting
    # anything, while an explicit TOOLS_DIR always wins. Settings maps this with
    # no env prefix (see afterthread/config.py's model_config), so the
    # variable name is exactly TOOLS_DIR. Not created here: the directory is made
    # when the first tool is installed, and afterthread/services/tools.py
    # treats a missing dir as "nothing installed yet" (empty list), so injecting
    # a not-yet-existing path is harmless. No secret is involved, so -- unlike
    # DATABASE_URL -- there is nothing to withhold from the banner; it is simply
    # left off to keep that line to the two facts it already prints.
    if "TOOLS_DIR" not in os.environ:
        os.environ["TOOLS_DIR"] = str(data_dir_path / "tools")

    # English; deliberately only ever these two facts -- where data lives,
    # and where the database is (see above). NEVER print OPENAI_* or any
    # other secret-shaped setting here. flush=True: stdout is fully
    # block-buffered once it is not a TTY (i.e. always, once redirected to a
    # log file or piped anywhere), so without an explicit flush this line can
    # sit unseen in Python's buffer for an arbitrarily long time -- confirmed
    # during implementation: it only appeared after the first HTTP request
    # nudged the buffer, well after uvicorn's own (separately flushed)
    # startup lines. A startup banner that shows up late is as good as none.
    print(
        f"afterthread: data dir={data_dir_path} database={database_display}",
        flush=True,
    )

    uvicorn.run("afterthread.main:app", host=host, port=port)


def main() -> None:
    """Console-script entry point (see `[project.scripts]` in pyproject.toml).

    Typer collapses a `Typer()` app with exactly one `@app.command()` (`_serve`
    above, with no separate `@app.callback()`) into a single-command CLI with
    no subcommand name required, so calling `app()` with no arguments here --
    reading `sys.argv` exactly like the `argparse` parser this replaced --
    is the whole shim.
    """
    app()
