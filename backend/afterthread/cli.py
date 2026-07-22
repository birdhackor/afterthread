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

import contextlib
import errno
import os
import tempfile
from importlib import metadata, resources
from pathlib import Path
from typing import Annotated, NoReturn
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
        # FIX-2: this chmod is defense in depth, not the ONLY defense -- the
        # .env FILE itself also carries its own enforced 0600 mode (see
        # `_write_env_tempfile`'s `os.fchmod` below, shared by both the
        # fresh and --force publish paths), independent of whatever this
        # directory's permissions end up being. So an already-existing, more
        # permissive data dir (0755, say -- the "leave it alone" branch just
        # above) still can never leave .env itself group/other-readable:
        # that guarantee is never delegated to this directory chmod alone.
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


def _parse_port(raw: str, ctx: typer.Context) -> int:
    """Convert the raw --port/AFTERTHREAD_PORT string to int -- BARE-SERVE ONLY.

    FIX-3 (loud invariant): this conversion must NEVER run before Click has
    decided whether a subcommand is even being dispatched. `port` used to be
    typed `int` directly on the `typer.Option` in `_callback` below, and
    Click/Typer convert EVERY top-level option's value during the Group's own
    parameter-resolution pass -- which runs UNCONDITIONALLY, before
    `_callback`'s body, and therefore before Click has even looked at whether
    a subcommand follows. An `int` conversion can FAIL (a non-numeric
    AFTERTHREAD_PORT), and when it did, it aborted the WHOLE process before a
    subcommand -- e.g. `init-env`, which never reads `port` at all -- ever got
    a chance to run: empirically, `AFTERTHREAD_PORT=bad afterthread init-env
    --help` used to exit 2 without ever reaching `init-env`. A serve-only
    configuration value must never be able to block a subcommand that does
    not use it.

    The fix: `port` is typed `str` on the option itself (see `_callback`), so
    Click's own conversion pass can never fail on it (a str "conversion" is
    the identity function), and ALL int conversion is deferred to this
    function -- called from EXACTLY ONE place, `_callback`'s body, and ONLY
    on the branch already confirmed to be bare-serve (`ctx.invoked_subcommand
    is None` -- see the call site). The subcommand path never calls this at
    all, which is the whole fix.

    The observable failure mode for bare-serve itself is UNCHANGED: same exit
    code (2, via `typer.BadParameter`, a `click.UsageError` subclass -- see
    `_reject_explicit_top_level_options_before_subcommand` below for the same
    exit-2 contract from a hand-raised BadParameter), same "not a valid
    integer" wording Click's own `IntParamType` used to produce for a bad
    --port. Only WHEN the conversion happens moved; what it accepts and how
    it reports failure did not.
    """
    try:
        return int(raw)
    except ValueError:
        raise typer.BadParameter(
            f"{raw!r} is not a valid integer.", ctx=ctx, param_hint="'--port'"
        ) from None


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
    # FIX-3 (loud invariant): raw str, NOT int -- see `_parse_port`'s
    # docstring for the bug this closes (a stale AFTERTHREAD_PORT used to
    # block every subcommand, including ones that never read `port`) and the
    # one call site, in this function's body below, that now owns the
    # str->int conversion -- and ONLY on the confirmed bare-serve branch.
    port: Annotated[
        str,
        typer.Option(
            "--port",
            envvar="AFTERTHREAD_PORT",
            metavar="INTEGER",
            help="Port to bind (an integer; validated only when actually serving).",
        ),
    ] = "8000",
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

    FIX-3 (loud invariant): "parsed on the subcommand path too" above used to
    be more dangerous for `port` specifically than for `host`/`data_dir`,
    because historically ONLY `port` carried an `int` type on its
    `typer.Option` -- and Click converts a top-level option's value during
    ITS OWN parameter-resolution pass, unconditionally, before this
    function's body ever runs, let alone before it knows whether a
    subcommand follows. An `int` conversion can FAIL (a non-numeric
    AFTERTHREAD_PORT); when it did, it aborted the whole process before a
    subcommand that never reads `port` -- e.g. `init-env` -- ever got to run.
    `host` and `data_dir` never had this hazard and need NO analogous change:
    both are typed `str | None`, and a `str` "conversion" is the identity
    function -- it cannot fail on ANY input, so there is nothing for a
    subcommand-blocking bug to hide in. `port` is now ALSO typed `str` here,
    for exactly that reason, deferring its int conversion to `_parse_port`
    (see its docstring), called from this function's body below ONLY on the
    confirmed bare-serve branch.
    """
    if ctx.invoked_subcommand is not None:
        # An explicitly-typed --host/--port/--data-dir would otherwise be
        # silently discarded by this early return -- that is a user error to
        # surface loudly, never to ignore. See the docstring above and
        # `_reject_explicit_top_level_options_before_subcommand` itself.
        _reject_explicit_top_level_options_before_subcommand(ctx)
        return
    # FIX-3: int-convert --port/AFTERTHREAD_PORT HERE, and only here -- this
    # line is the confirmed bare-serve branch (the `is not None` case above
    # already returned), so a malformed port value can now only ever block
    # bare-serve, never a subcommand. See `_parse_port`'s docstring.
    _run_serve(host=host, port=_parse_port(port, ctx), data_dir=data_dir)


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


def _refuse_symlinked_env(env_path: Path) -> NoReturn:
    """Print the symlink refusal and exit 1 (FIX-1a / FIX-1b's TOCTOU backstop).

    The ONE outcome both call sites resolve to -- the up-front
    `env_path.is_symlink()` check in `_init_env` (FIX-1a; runs before either
    publish primitive, force or not) and, for the rare TOCTOU race where a
    symlink appears AFTER that check but the no-`--force` path is taken, the
    `EEXIST`-handling branch inside `_publish_env_file_fresh` -- so the two
    can never drift into two differently-worded refusals for what is, to the
    user, the exact same situation. Factored out rather than a shared string
    constant because both call sites also need the `typer.Exit(1)` control
    flow, not just the text.

    Symlinked, valid OR broken, forced or not: afterthread will never write
    through it. Managing that symlink is the operator's own setup, not
    something this command tries to interpret or repair.
    """
    typer.echo(
        f"錯誤：{env_path} 是符號連結，afterthread 不支援透過符號連結寫入 .env，"  # noqa: RUF001
        "這屬於您自行管理的設定，我們絕不會透過符號連結寫入設定檔，"  # noqa: RUF001
        "即使加上 --force 也一樣拒絕。",
        err=True,
    )
    raise typer.Exit(1)


def _write_all(fd: int, content: bytes) -> None:
    """Write every byte of `content` to `fd` (FIX-4): loop over `os.write`'s short-write contract.

    `os.write(fd, data)` is a thin wrapper over the `write(2)` syscall, and
    POSIX explicitly permits `write(2)` to transfer FEWER bytes than were
    requested, returning that smaller count rather than raising -- this is
    ordinary, expected behavior, not a rare edge case: a disk-full or
    per-user-quota condition reached partway through, an EINTR retry the C
    library resumes at a byte offset Python itself never sees, or the kernel
    simply choosing to service one large write in more than one chunk under
    I/O pressure can all produce a short return value on an entirely healthy
    fd. A single, unchecked `os.write(fd, content)` call -- this helper's
    entire reason to exist, and the exact shape of the empirically confirmed
    bug it replaces -- silently treats a SHORT write as a COMPLETE one:
    whatever runs next (`os.fsync`, `os.close`) still succeeds, so the caller
    reports success while only a PREFIX of `content` actually reached the
    file. For `.env` -- a file that can hold a real `OPENAI_API_KEY` -- a
    silently truncated write is strictly worse than an honest failure, so
    every call here loops, reissuing exactly the UNWRITTEN remainder (via a
    `memoryview` slice -- no bytes are ever copied on a retry) until nothing
    is left.

    A single `os.write` call returning 0 while bytes still remain is NOT a
    "keep looping" signal -- POSIX's `write(2)` returns 0 for a non-empty
    request only when it could make NO progress whatsoever (a short POSITIVE
    count, by contrast, always means SOME bytes genuinely landed, and is a
    normal, expected iteration of this loop) -- so zero progress raises
    immediately instead of spinning forever on a call that will never do any
    better. The raised `OSError` is tagged `errno.EIO`: there is no more
    specific errno for "the syscall itself reported zero progress without
    raising", and EIO still round-trips cleanly through this module's
    errno-keyed handling in `_publish_env_file_fresh` (which only
    special-cases EEXIST; anything else, including this, is an honest,
    uncaught failure -- exactly this file's existing contract for any OTHER
    unexpected OSError).
    """
    remaining = memoryview(content)
    while remaining:
        written = os.write(fd, remaining)
        if not written:
            # Zero-progress guard: without this check, a bare
            # `while remaining: os.write(...)` loop would spin forever the
            # moment the kernel legitimately returns 0 (e.g. a full or a
            # read-only-remounted filesystem) -- this turns that into an
            # immediate, honest OSError instead of a hung CLI process.
            raise OSError(
                errno.EIO,
                f"os.write made no progress with {len(remaining)} byte(s) still unwritten",
            )
        remaining = remaining[written:]


def _write_env_tempfile(env_path: Path, content: bytes) -> Path:
    """Write `content` to a fresh, private temp file beside `env_path`; return it, closed.

    The ONE writer shared by both publish primitives below --
    `_publish_env_file_fresh` (no `--force`) and `_publish_env_file_force`
    (`--force`) -- so `.env`'s byte-fidelity, permission, and fsync
    discipline is expressed exactly once instead of twice, and the two
    publish paths can never quietly drift apart on any of the three. This
    unifies what used to be two independent writers -- one inlined into each
    of the now-deleted `_create_env_file_exclusive` and
    `_replace_env_file_atomically` -- that happened to agree by
    construction, not by sharing code, which is exactly how the
    restrictive-umask gap below went unnoticed on the fresh-create path for
    as long as it did.

    `mkstemp` in the SAME directory as `env_path` (never a system temp dir,
    and never `env_path` itself) is what makes BOTH publish primitives below
    atomic: `os.link`/`os.replace` are only atomic within one
    filesystem/mount, and a temp file elsewhere risks a cross-device
    link/rename (EXDEV) instead of an instant one. The name is also
    unguessable, so nothing could pre-plant a symlink at it -- and, being a
    brand-new name `mkstemp` itself invented, it can never collide with, or
    need to reason about, whatever currently does or doesn't exist at
    `env_path`.

    `os.fchmod(fd, 0o600)` runs immediately after open, before a single byte
    of `content` is written -- and this is the actual fix for the confirmed
    MEDIUM finding this function closes. The mode argument `os.open`/
    `mkstemp` themselves accept (also 0o600) is masked by the process umask
    like any other `open(2)` call: under a restrictive umask (0o777, the
    legal-but-pathological extreme) a "0600" open request lands as mode 000
    on disk -- created successfully, reported as success, yet unreadable and
    unwritable by anyone, including the user who is about to be told to go
    edit it. The OLD fresh-create path (`_create_env_file_exclusive`, now
    deleted) requested its mode this way and ONLY this way, so despite its
    own docstring's "regardless of the process umask" claim, it was exposed
    to exactly this gap. `fchmod(2)`, by contrast, sets a file's mode
    directly on an already-open descriptor and is NEVER masked by umask --
    it is the sole mode guarantee this module now relies on, for both
    publish paths alike. (tests/test_cli.py exercises the 0o777 direction
    explicitly; a permissive-umask test alone -- the only kind that existed
    before -- can never distinguish "genuinely umask-independent" from
    "happened to pass because this run's umask was never restrictive enough
    to matter".)

    On ANY failure below -- `fchmod`, `_write_all`'s write loop, or `fsync`
    -- the fd is closed if still open and OUR temp file is unlinked, both
    best-effort (`contextlib.suppress(OSError)`: a cleanup-time failure must
    never mask the original one), before re-raising the original `OSError`
    unchanged. This doubles as the fix for the confirmed HIGH finding: the
    path `mkstemp` returns is process-private and provably unique -- nothing
    else on the system ever had, or will have, a reason to name it -- so
    failure cleanup HERE can never remove anything another process placed at
    `env_path` itself. The OLD fresh-create path's rollback instead deleted
    `env_path` BY PATH after the fact, which was only ever safe as long as
    O_EXCL's "we own this name" proof from open() time still held -- a
    guarantee already stale by the time a LATER write/fsync failure
    triggered that rollback: if this process stalled between the create and
    the failure, and a CONCURRENT `init-env --force` (or a human editor)
    atomically published a complete, correct `.env` into that same path in
    the meantime, the old rollback would delete THEIR successful file --
    silent loss of a config another process had already reported as
    written. Writing to a private temp name first, and only ever touching
    `env_path` via the atomic, all-or-nothing publish primitives below,
    removes that window entirely: this function never creates, and
    therefore never has to clean up, anything at `env_path` itself.
    """
    fd, tmp_name = tempfile.mkstemp(dir=env_path.parent, prefix=".env.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    fd_open = True  # mirrors tools.py's fd_owned idiom: tracks whether `fd` still needs closing
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, content)
        os.fsync(fd)
        os.close(fd)
        fd_open = False
    except OSError:
        if fd_open:
            with contextlib.suppress(OSError):
                os.close(fd)
        # Safe unconditionally: `tmp_path` is a name `mkstemp` invented for
        # THIS call alone, never anything a caller passed in or another
        # process could ever be relying on -- see the docstring above.
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    return tmp_path


def _cleanup_published_tempfile(tmp_path: Path) -> None:
    """Best-effort unlink of `tmp_path` for a publish that ALREADY succeeded.

    Shared by `_publish_env_file_fresh`'s and `_publish_env_file_force`'s
    success paths only -- a FAILED publish suppresses its own unlink error
    inline instead, before its own exception/`typer.Exit` propagates (see
    both callers' `except` blocks). This split is the fix for a confirmed
    review finding: the old code unlinked `tmp_path` in one bare
    `finally: tmp_path.unlink(missing_ok=True)` shared by success AND
    failure alike, so a coincidental unlink error (permissions flipped on
    the data dir, a transient filesystem error, ...) could do two different
    kinds of damage depending on which side of the publish it landed on --
    turn a genuinely SUCCESSFUL publish into a reported failure (the exact
    scenario this function now owns), or replace whichever exception a
    FAILED publish was already raising with an unrelated one, hiding the
    real reason from the user.

    By the time this runs, `env_path` already holds the published file --
    for the fresh path, a second name for the same inode `tmp_path` names;
    for `--force`, the rename target `tmp_path` was already renamed AWAY
    from. Either way `tmp_path` itself is pure debris now, not data: a
    leftover 0600 file sitting in the same private, user-only data
    directory as `.env` is harmless, and never worth failing an
    already-completed command over. So this never raises -- it prints one
    zh-TW warning naming the leftover path, for the user to remove by hand,
    and returns either way.
    """
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        typer.echo(
            f"警告：{tmp_path} 是 .env 發布後殘留的暫存檔，自動清理失敗，"  # noqa: RUF001
            "請自行刪除；.env 本身已寫入成功，不受此警告影響。",  # noqa: RUF001
            err=True,
        )


def _publish_env_file_fresh(tmp_path: Path, env_path: Path) -> None:
    """Publish `tmp_path` to `env_path` via a no-clobber hard link -- the no-`--force` path.

    Replaces BOTH the old O_EXCL-based exclusive create in the now-deleted
    `_create_env_file_exclusive` AND its except-driven, path-based rollback
    (see `_write_env_tempfile`'s docstring for the race that rollback could
    lose): here, `env_path` is never touched until `tmp_path` -- written by
    `_write_env_tempfile` above -- already holds a complete, fsynced,
    correctly-moded file. Nothing incomplete is EVER visible at `env_path`,
    so there is nothing to roll back if anything below fails.

    `os.link` is both atomic and no-clobber: it fails with `EEXIST` if
    ANYTHING already has that name -- a regular file, a symlink, or a file
    some other process raced into place a moment ago -- and, critically,
    unlike a plain rename it never follows a symlink AT THE DESTINATION to
    write through it (nor does an `EEXIST` on an existing symlink there
    ever get silently "resolved" through the link first -- the directory
    entry itself is what `link(2)` checks). So an `EEXIST` here is always a
    CORRECT refusal, never a deletion hazard: at worst it costs the loser of
    a race one polite "already exists, use --force" message, never someone
    else's already-published file.

    The up-front `env_path.is_symlink()` check in `_init_env` already ran
    before this call, but a symlink raced into place in the TOCTOU window
    between that check and this one is still attributed correctly here:
    `EEXIST` itself already proves SOMETHING now occupies `env_path`, and
    this `is_symlink()` recheck only decides which of the two zh-TW messages
    describes it accurately (`_refuse_symlinked_env`'s vs. the generic
    "already exists" below) -- it is not itself what makes the refusal
    race-free; `os.link`'s own atomicity is. There is no separate ELOOP
    backstop here the way the old O_NOFOLLOW-based create needed one:
    `os.link` never follows a symlink at `env_path` to begin with, so there
    is no O_NOFOLLOW-shaped errno quirk to special-case -- EEXIST is the
    only outcome a pre-existing name of any kind can produce.

    Any OTHER `OSError` (e.g. `EPERM` on a filesystem that refuses hard
    links -- accepted as out of scope: every mainstream local filesystem on
    Linux/macOS supports them, and this is a single-user tool, not worth a
    fallback publish strategy for) propagates unchanged: a single-shot CLI
    command has no in-flight request to protect, so a Python traceback here
    is an acceptable, honest failure rather than something worth masking.

    Cleanup of `tmp_path` is asymmetric now, not one shared `finally` (see
    `_cleanup_published_tempfile`'s docstring for the confirmed review
    finding this closes): the `except` branch below suppresses its OWN
    unlink error unconditionally, before either branch decides what to
    raise -- `env_path` itself was never touched on this path (by
    construction of `link(2)`), so the temp file is the only cleanup owed,
    and a failure cleaning it up must never outrank the refusal, or the
    original `OSError`, as what the user actually sees. The success path
    instead routes through `_cleanup_published_tempfile`, which degrades a
    cleanup failure to a stderr warning instead of an exit-code flip.

    REVIEW RULING (durability vs. atomicity, adversarial review): `os.link`
    above guarantees atomic VISIBILITY -- no reader ever observes a
    half-written `.env` -- not durability of that directory entry across a
    power loss. Neither this function nor `_publish_env_file_force` fsyncs
    `env_path`'s parent directory afterward, deliberately: `init-env` is a
    re-runnable bootstrap command, not a one-shot unrecoverable write, so a
    crash in that narrow window costs nothing worse than running it again --
    the same call `afterthread/services/tools.py`'s own hardened writer
    already makes for every file IT writes (no directory fsync there
    either).
    """
    try:
        os.link(tmp_path, env_path)
    except OSError as exc:
        # The publish did not happen -- see above -- so a coincidental
        # unlink failure here must never be allowed to outrank WHY: always
        # suppressed, before either branch below decides what propagates.
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        if exc.errno == errno.EEXIST:
            # link(2)'s own EEXIST already proves SOMETHING sits at
            # env_path; this lstat only decides which refusal describes it.
            if env_path.is_symlink():
                _refuse_symlinked_env(env_path)
            # Refuse rather than silently overwrite: an existing .env may
            # already hold a real OPENAI_API_KEY or other tuned setting, and
            # clobbering it on a re-run (e.g. someone re-running init-env
            # out of habit) would be a silent, unrecoverable data-loss
            # footgun. --force is the explicit, deliberate opt-in to
            # overwrite -- the file is left byte-for-byte untouched
            # otherwise.
            typer.echo(
                f"錯誤：{env_path} 已存在，不會覆寫既有設定檔。如需覆寫請加上 --force。",  # noqa: RUF001
                err=True,
            )
            raise typer.Exit(1) from exc
        raise
    else:
        _cleanup_published_tempfile(tmp_path)


def _publish_env_file_force(tmp_path: Path, env_path: Path) -> None:
    """Publish `tmp_path` onto `env_path`, replacing whatever is there -- the `--force` path.

    Unchanged in mechanism from the old `_replace_env_file_atomically`
    (renamed here only to sit alongside `_publish_env_file_fresh` as the two
    publish primitives sharing one writer, `_write_env_tempfile`, instead of
    each inlining its own): `os.replace` is the atomic swap onto an existing
    name -- `.env` is never observed half-written or truncated by a
    concurrent reader -- and, being a rename onto an existing name, it
    replaces the OLD file's inode (and therefore its mode) wholesale with
    the temp file's: an existing world-readable `.env` being overwritten by
    `--force` ends this call at 0600, never at its old, wider mode.
    (Verified separately: a rename/`os.replace` onto a symlink destination
    replaces the LINK itself, never writes through it to the target -- but
    `_init_env` still refuses ANY symlinked `.env` up front, before `force`
    is even consulted, precisely so this function is never even asked to
    touch one.)

    Cleanup mirrors `_publish_env_file_fresh` exactly -- see its docstring,
    and `_cleanup_published_tempfile`'s, for the full review finding and the
    durability ruling both publishers share. On failure, `os.replace` never
    renamed anything onto `env_path`, so the `tmp_path` unlink below is
    best-effort, any error suppressed, before the original exception
    propagates unchanged -- exactly like a half-finished `--force` always
    left no stray temp file behind. On success, unlinking `tmp_path` is
    ordinarily a pure no-op -- `os.replace` already renamed that name away,
    onto `env_path`, so there is nothing left even for `missing_ok=True` to
    swallow -- but routing it through `_cleanup_published_tempfile` costs
    nothing and means an unlikely failure here (e.g. a directory permission
    pulled out from under this call between the two syscalls) degrades to
    the same one stderr warning instead of flipping an already-successful
    `--force` publish to a nonzero exit.
    """
    try:
        os.replace(tmp_path, env_path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise
    else:
        _cleanup_published_tempfile(tmp_path)


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

    FIX-1/FIX-2 (adversarial review) hardened the actual write, replacing a
    plain `env_path.exists()` guard followed by `write_text()`. That old
    shape had three independent holes: (1) `exists()` FOLLOWS a symlink and
    reports False for a BROKEN one, so a broken-symlink `.env` sailed
    straight past the refusal and `write_text` created the link's TARGET --
    outside the data dir entirely -- while a VALID symlink plus `--force`
    clobbered whatever the link pointed at, wherever that was; (2) the check
    and the write were two separate syscalls, a plain TOCTOU race even for an
    ordinary regular file; (3) `write_text` truncates in place before writing
    the new bytes, so a disk-full condition or an interrupt mid-write could
    leave an EMPTY or partial `.env` parked at the real path. See
    `_refuse_symlinked_env` above for how the symlink hole is closed --
    styled after, and citing the kinship with, the same hardened-write
    discipline `afterthread/services/tools.py` already applies to every file
    that module writes. None of this is hardening for its own sake: `.env`
    can hold a real `OPENAI_API_KEY`, so "write it correctly or refuse" is
    the only acceptable contract.

    A LATER review round found the first fix for holes (2)/(3) -- separate
    O_EXCL-based exclusive-create and mkstemp-based atomic-replace helpers,
    one per branch below -- still had two gaps of its own, both closed by
    UNIFYING the two branches behind one shared temp-file writer and two
    atomic publish primitives (all three defined just above this command):
    (a) HIGH -- the exclusive-create branch's failure cleanup unlinked
    `env_path` BY PATH, which was safe only as long as O_EXCL's open-time
    ownership proof still held; a slow write/fsync racing against a
    concurrent successful `--force` (or a human editor) could make this
    process delete a file ANOTHER process had already finished publishing;
    (b) MEDIUM -- that same branch's `0o600` mode was an `os.open` argument
    and therefore, unlike `_write_env_tempfile`'s `os.fchmod` (never
    masked), silently narrowed by a restrictive process umask, so
    "regardless of umask" was not actually true for it. See
    `_write_env_tempfile`, `_publish_env_file_fresh`, and
    `_publish_env_file_force` above for the closed design: a private temp
    file is always fully written, fsynced, and mode-fixed BEFORE either
    branch below ever touches `env_path`, so there is never anything
    incomplete at `env_path` for a rollback to have to undo.
    """
    data_dir_path = _resolve_data_dir(data_dir)
    _ensure_data_dir(data_dir_path)

    env_path = data_dir_path / ".env"
    # FIX-1a: refuse OUTRIGHT when .env is a symlink, valid or broken, even
    # with --force -- checked BEFORE the --force branch below, and via
    # `is_symlink()` (backed by `os.lstat`, which inspects the link's OWN
    # directory entry rather than following it) rather than `exists()`
    # (which FOLLOWS the link and returns False for a dangling target -- the
    # exact old bug: a broken symlink used to pass this refusal, and the
    # write below would then have created the link's TARGET instead, an
    # arbitrary path outside the data dir).
    if env_path.is_symlink():
        _refuse_symlinked_env(env_path)

    # Read the packaged template as BYTES once, and write bytes throughout
    # (FIX-1d): no text-mode newline translation between the template on disk
    # and the file this command writes -- tests pin the two byte-for-byte.
    template_bytes = resources.files(_PACKAGE_NAME).joinpath(_ENV_TEMPLATE_RESOURCE).read_bytes()

    # One pipeline, both branches: write once to a private temp file (never
    # env_path itself -- see _write_env_tempfile), then hand off to whichever
    # publish primitive matches --force. Neither primitive can ever observe
    # or publish an incomplete file, so there is nothing left for _init_env
    # itself to catch or roll back here -- each primitive owns its own
    # errno handling (EEXIST -> the polite refusal, for the fresh path) and
    # either returns having published `.env`, or raises/exits on its own.
    tmp_path = _write_env_tempfile(env_path, template_bytes)
    if force:
        _publish_env_file_force(tmp_path, env_path)
    else:
        _publish_env_file_fresh(tmp_path, env_path)

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
