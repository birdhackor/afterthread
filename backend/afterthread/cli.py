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
untouched by packaging.

The CLI is two things sharing one Typer `app`, not one:

  * A bare invocation (`afterthread`, optionally with --host/--port/
    --data-dir/--version) serves the app -- see `_run_serve` below for the
    five-step data-dir/.env/DATABASE_URL/TOOLS_DIR dance. This is still the
    ONLY thing most users ever type; see `_callback`'s docstring for how it
    stays that way even though the app now has a second command.
  * `afterthread init-env` materializes `<data-dir>/.env` from the packaged
    `afterthread/env.example` template (see `_init_env`) -- for a
    `uvx`/`pip`-installed user who has no checkout to copy the template from
    by hand, unlike a dev-from-source `git clone`.

Serving, specifically, does:

  1. Resolve a per-user data directory (`--data-dir` / `AFTERTHREAD_DATA_DIR`
     / an XDG-style default) and make sure it exists (created user-only).
  2. `os.chdir` into it: the data dir is packaged mode's home directory.
     Every CWD-relative behavior downstream then resolves inside the data
     dir rather than whatever directory the user happened to launch from --
     pydantic-settings' CWD-relative `env_file=".env"` lookup (config.py),
     and any relative path in a setting value (e.g. a
     `DATABASE_URL=sqlite:///./afterthread.db` copied straight from
     `afterthread/env.example`). That closes two whole classes of surprises:
     a stray `.env` sitting in an arbitrary launch CWD silently configuring
     the server, and relative DB paths scattering database files across
     launch directories.
  3. Load `<data-dir>/.env` into the process environment, if present --
     without overriding a variable that is already set, so an explicit env
     var always wins over the file (the same precedence pydantic-settings
     itself gives env vars over `backend/.env` in dev).
  4. Default `DATABASE_URL` to a file inside that data directory, unless the
     environment already supplies one.
  5. Hand off to `uvicorn.run`, serving the same `afterthread.main:app`
     dev uses.

`init-env` shares step 1's data-dir resolution (`_resolve_data_dir` /
`_ensure_data_dir` below, so it can never disagree with `serve` about which
directory a given --data-dir/AFTERTHREAD_DATA_DIR/default means) but does
none of steps 2-5: it only ever writes one file and exits. A `<data-dir>/.env`
it just materialized is inert until the NEXT bare `afterthread` invocation
loads it via step 3.
"""

import os
from importlib import metadata, resources
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import typer
import uvicorn
from dotenv import load_dotenv
from sqlalchemy import make_url
from sqlalchemy.engine import URL

_PACKAGE_NAME = "afterthread"

# The packaged env-var template `init-env` copies from -- a real file at
# afterthread/env.example (see hatchling's default wheel-content rule in
# backend/pyproject.toml: every TRACKED non-.py file under the package dir
# ships as-is, no `artifacts=` entry needed, unlike afterthread/static/).
_ENV_TEMPLATE_RESOURCE = "env.example"

# add_completion=False: this is a small, two-command server-launcher CLI
# (bare invocation to serve, `init-env` to seed a config file) -- not a CLI
# suite worth shell-completion machinery, which would only add
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


def _resolve_data_dir(data_dir: str | None) -> Path:
    """Resolve a --data-dir value to an absolute, expanded, NOT-yet-created path.

    Shared by every subcommand that touches the data directory (today:
    bare-invocation serve via `_run_serve`, and `init-env` via `_init_env`) so
    all of them agree on exactly one directory for a given
    --data-dir/AFTERTHREAD_DATA_DIR/default combination -- `init-env` writing
    a `.env` that `serve` then resolves to a DIFFERENT directory would defeat
    the entire point of the command.

    `data_dir` is None when neither --data-dir nor AFTERTHREAD_DATA_DIR was
    given, and "" when AFTERTHREAD_DATA_DIR is set but empty -- both fall
    back to the XDG-style default here, the same rule `_default_data_dir`
    itself applies to a set-but-empty XDG_DATA_HOME.

    Callers resolve BEFORE any `os.chdir`, so a relative --data-dir is
    anchored to the directory the user launched from, as they would expect.
    """
    return Path(data_dir or str(_default_data_dir())).expanduser().resolve()


def _ensure_data_dir(data_dir_path: Path) -> None:
    """Create `data_dir_path` if missing, chmod'd user-only; leave it alone otherwise.

    Shared for the same reason as `_resolve_data_dir`: `init-env --data-dir
    ~/somewhere-new` may be the FIRST thing to ever touch a given data
    directory, exactly as validly as `serve` being first -- both need the
    identical "create it, and if WE created it, lock it down" contract.
    """
    created = not data_dir_path.exists()
    data_dir_path.mkdir(parents=True, exist_ok=True)
    if created:
        # This directory holds personal memory content (the SQLite database)
        # and, via its .env, possibly an API key -- so a directory this tool
        # itself just created defaults to user-only. An already-existing
        # directory is left untouched: its permissions may be a deliberate
        # choice, and silently rewriting them is not this tool's call.
        data_dir_path.chmod(0o700)


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

    `is_eager=True` on the `--version` option below (see `_callback`) makes
    this callback run before any other option is converted/validated,
    matching the old `argparse` `action="version"`: `--version` alone always
    works, even if some other flag on the same command line would otherwise
    be invalid -- and even if a subcommand name is also present, since eager
    options are processed before Click ever resolves one.
    """
    if value:
        typer.echo(f"{_PACKAGE_NAME} {_package_version()}")
        raise typer.Exit()


# Shared --data-dir option spec for bare-invocation serve (`_callback`) and
# `init-env` (`_init_env`): identical flag/envvar/help/default, so the two
# commands can never quietly drift into describing two different directories
# to the user (see `_resolve_data_dir`'s docstring for why that would be a
# real bug, not just a wording inconsistency). Reusing this `Annotated` alias
# across two command signatures is safe -- typer builds an independent
# `click.Option` object per command from it; nothing here is mutated.
_DataDirOption = Annotated[
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
]

# Python-parameter-name -> the flag spelling shown to the user, for every
# option `_callback` parses that must NEVER be silently dropped on the
# subcommand path (`--version` excluded: `is_eager=True` exits the process
# before Click ever resolves a subcommand -- see `_version_callback` --
# so there is no subcommand path for it to be dropped on). Feeds
# `_reject_explicit_top_level_options_before_subcommand` below.
_TOP_LEVEL_OPTION_FLAGS: dict[str, str] = {
    "host": "--host",
    "port": "--port",
    "data_dir": "--data-dir",
}


def _reject_explicit_top_level_options_before_subcommand(ctx: typer.Context) -> None:
    """Fail loudly if --host/--port/--data-dir was explicitly typed before a subcommand.

    THE TRAP this closes (empirically demonstrated before this guard
    existed): `afterthread --data-dir /custom init-env` used to exit 0,
    print a plausible "已建立 .../.env" success line, and write that file
    into the DEFAULT data dir -- NOT `/custom`. Root cause: Click always
    calls this Group's callback (`_callback`) first, unconditionally, even
    when a subcommand follows (see its docstring) -- so --data-dir WAS
    parsed -- but the callback then took its early-return path without
    passing that value anywhere, while `init-env` went on to resolve its
    OWN, separate `--data-dir` (unset, since the user placed the flag
    before `init-env`, not after it) against that same default. A value
    the user explicitly typed was accepted, then silently thrown away.

    The fix distinguishes "the user actually typed this flag" from "this
    is just the parameter's default" via `ctx.get_parameter_source`.
    IMPORTANT: do NOT compare its return value against
    `click.core.ParameterSource` from the separately pip-installed `click`
    package (tempting -- and how an early draft of this guard read). Typer
    0.26 vendors its OWN forked copy of click under `typer._click`, so at
    runtime `ctx` here is a `typer._click.core.Context`, and
    `get_parameter_source` returns a `typer._click.core.ParameterSource`
    member: a DIFFERENT enum class from `click.core.ParameterSource`
    despite identical member names, so `==`/`is` against the pip-installed
    package's enum is silently always False -- verified live against this
    project's actual installed typer==0.26.8 (a `source is
    click.core.ParameterSource.COMMANDLINE` check never fires for a real
    typer-managed option, no matter how the command line is typed, which
    would have left this guard's condition permanently False -- dead code
    that still let the original trap through). Comparing by `.name`
    instead sidesteps which physical Enum class is in play.

    An environment variable (AFTERTHREAD_DATA_DIR / AFTERTHREAD_HOST /
    AFTERTHREAD_PORT) is NOT this trap and is left alone: it legitimately
    configures either the bare-serve path or a subcommand's own resolution
    identically (`init-env` reads AFTERTHREAD_DATA_DIR itself, via this
    very same `_DataDirOption`), so ParameterSource.ENVIRONMENT here is
    fine, never an error. Only a value placed explicitly on the command
    line BEFORE the subcommand name -- ParameterSource.COMMANDLINE -- is
    refused.
    """
    # Precondition (guaranteed by the only caller, `_callback`, which checks
    # this before invoking us): a subcommand was actually named, so this is
    # never None in practice.
    subcommand = ctx.invoked_subcommand
    for param_name, flag in _TOP_LEVEL_OPTION_FLAGS.items():
        source = ctx.get_parameter_source(param_name)
        if source is not None and source.name == "COMMANDLINE":
            raise typer.BadParameter(
                f"{flag} 是頂層選項，只有在沒有子指令（直接啟動伺服器）時才會生效；"  # noqa: RUF001
                f"寫在子指令 {subcommand} 之前並不會套用到它，為避免這種靜默遺漏，"  # noqa: RUF001
                f"直接視為錯誤。若 {subcommand} 支援這個選項，請改成放在子指令之後，"  # noqa: RUF001
                f"例如：afterthread {subcommand} {flag} ...；若不支援，請移除這個選項。",  # noqa: RUF001
                ctx=ctx,
            )


@app.callback(
    invoke_without_command=True,
    help="Run the afterthread web app (bundled API + SPA).",
)
def _callback(
    ctx: typer.Context,
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
    data_dir: _DataDirOption = None,
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
    """The app's ONE `@app.callback()` -- why --host/--port/--data-dir/
    --version live here rather than on a `serve` subcommand of their own.

    Typer collapses a `Typer()` app into a bare, subcommand-less CLI ONLY
    when it has exactly one `@app.command()` and no `@app.callback()` at
    all (see `main` below for what that meant before this module had
    `init-env`). Adding a second command breaks that collapse outright --
    with no callback at all, bare `afterthread ...` (no subcommand token)
    would become a "Missing command" usage error instead of serving.
    `invoke_without_command=True` here is what keeps the bare form legal, and
    `ctx.invoked_subcommand` is how this function tells the two cases apart:
    it is None exactly when no subcommand was named on the command line.

    Click always calls this callback first regardless -- even when a
    subcommand IS given, `ctx.invoked_subcommand` is set to that
    subcommand's name and THIS function still runs before it. So when a
    subcommand was named, this function's ONLY job is to have already parsed
    the four options above (which is why they stay declared here, not moved
    onto `_run_serve`); it must do NOTHING else side-effectful -- no mkdir,
    no chdir, no dotenv load, no uvicorn.run. Subcommands (today: `init-env`)
    own their own world from that point on.

    INVARIANT: --host/--port/--data-dir are consumed ONLY for the bare-serve
    path (`ctx.invoked_subcommand is None`). They are still PARSED on the
    subcommand path too (Click leaves this function no choice -- see above),
    so a value the user explicitly typed HERE, before the subcommand name,
    must never just fall on the floor when this function returns early --
    see `_reject_explicit_top_level_options_before_subcommand`, called right
    before that early return, for the trap this closes.
    """
    if ctx.invoked_subcommand is not None:
        # An explicitly-typed --host/--port/--data-dir would otherwise be
        # silently discarded by this early return -- that is a user error to
        # surface loudly, never to ignore. See the docstring above and
        # `_reject_explicit_top_level_options_before_subcommand` itself.
        _reject_explicit_top_level_options_before_subcommand(ctx)
        return
    _run_serve(host=host, port=port, data_dir=data_dir)


def _run_serve(host: str, port: int, data_dir: str | None) -> None:
    """Prepare the data directory/environment, then serve.

    The implementation behind bare `afterthread ...`, called from
    `_callback` above -- see the module docstring for the five-step rundown
    of what this does and why, and `_callback`'s docstring for why this is a
    plain function invoked from a callback rather than a command in its own
    right (Click never sees a "serve" subcommand token; there isn't one).
    """
    # Resolve BEFORE the chdir below, so a relative --data-dir is anchored to
    # the directory the user launched from, as they would expect (see
    # `_resolve_data_dir`'s own docstring).
    data_dir_path = _resolve_data_dir(data_dir)
    _ensure_data_dir(data_dir_path)

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


@app.command(
    "init-env",
    help="Write <data-dir>/.env from the packaged env.example template.",
)
def _init_env(
    data_dir: _DataDirOption = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite <data-dir>/.env if it already exists.",
        ),
    ] = False,
) -> None:
    """Materialize `<data-dir>/.env` from the packaged `env.example` template.

    Deliberately does none of `_run_serve`'s other four steps -- no chdir, no
    dotenv LOADING (only writing a fresh file), no DATABASE_URL/TOOLS_DIR
    defaulting, no uvicorn.run. This command's entire job is making one file
    exist so a LATER `afterthread` invocation has something to load; running
    it changes nothing about the CURRENT process's environment or behavior.

    The template is read via `importlib.resources` rather than a hand-built
    filesystem path (e.g. `Path(__file__).parent / "env.example"`) because it
    must resolve identically from an installed wheel (which may be served
    from inside a zip, never unpacked to a real directory on disk) and from
    an editable/dev install -- `importlib.resources` is the stdlib API that
    abstracts over both, whereas a raw `Path(__file__)`-relative lookup only
    works for the on-disk case.
    """
    data_dir_path = _resolve_data_dir(data_dir)
    _ensure_data_dir(data_dir_path)

    env_path = data_dir_path / ".env"
    if env_path.exists() and not force:
        # Refuse rather than silently overwrite: an existing .env may already
        # hold a real OPENAI_API_KEY or other tuned setting, and clobbering it
        # on a re-run (e.g. someone re-running init-env out of habit) would be
        # a silent, unrecoverable data-loss footgun. --force is the explicit,
        # deliberate opt-in to overwrite -- the file is left byte-for-byte
        # untouched otherwise.
        typer.echo(
            f"錯誤：{env_path} 已存在，不會覆寫既有設定檔。如需覆寫請加上 --force。",  # noqa: RUF001
            err=True,
        )
        raise typer.Exit(1)

    template_text = (
        resources.files(_PACKAGE_NAME).joinpath(_ENV_TEMPLATE_RESOURCE).read_text(encoding="utf-8")
    )
    env_path.write_text(template_text, encoding="utf-8")
    typer.echo(f"已建立 {env_path}，可依需要編輯後再啟動 afterthread。")  # noqa: RUF001


def main() -> None:
    """Console-script entry point (see `[project.scripts]` in pyproject.toml).

    A bare `Typer()` app with exactly one `@app.command()` and NO
    `@app.callback()` collapses into a single-command CLI needing no
    subcommand name -- but this app has a second command (`init-env`) AND a
    callback (`_callback`, `invoke_without_command=True`), so that collapse
    does not apply: without the callback, adding `init-env` alone would have
    turned every bare `afterthread ...` invocation into a "Missing command"
    usage error. `invoke_without_command=True` is what keeps `afterthread`
    (no subcommand token) meaning "serve" instead -- see `_callback`'s
    docstring for the full mechanics. Calling `app()` with no arguments here
    -- reading `sys.argv` exactly like the `argparse` parser this replaced --
    is the whole shim regardless of which shape (bare serve, or a named
    subcommand) the actual command line turns out to be.
    """
    app()
