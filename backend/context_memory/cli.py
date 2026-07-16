"""Console entry point for packaged (`uvx context-memory` / `context-memory`) mode.

Development never goes through this module: `backend/README.md`'s documented
dev workflow runs `uv run uvicorn context_memory.main:app` directly, so dev
behavior -- CWD-relative `backend/.env`, CWD-relative default `DATABASE_URL`,
no data directory at all -- is completely unchanged by anything below (see
`context_memory/config.py` and D05/D06 in `docs/web-v2-decisions.md`).

This module exists because a `uvx`-installed tool is launched from an
arbitrary CWD, with no project checkout nearby to hold a `.env` or a
CWD-relative SQLite file the way a dev checkout does -- there is no
"correct" directory for either to default to. So packaged-mode defaults are
decided in exactly one place, kept out of `context_memory.config` entirely so
`Settings`'s own defaults (and every existing test around them) are
untouched by packaging:

  1. Resolve a per-user data directory (`--data-dir` / `CONTEXT_MEMORY_DATA_DIR`
     / an XDG-style default) and make sure it exists (created user-only).
  2. `os.chdir` into it: the data dir is packaged mode's home directory.
     Every CWD-relative behavior downstream then resolves inside the data
     dir rather than whatever directory the user happened to launch from --
     pydantic-settings' CWD-relative `env_file=".env"` lookup (config.py),
     and any relative path in a setting value (e.g. a
     `DATABASE_URL=sqlite:///./context_memory.db` copied straight from
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
  5. Hand off to `uvicorn.run`, serving the same `context_memory.main:app`
     dev uses.
"""

import argparse
import os
from importlib import metadata
from pathlib import Path
from urllib.parse import quote

import uvicorn
from dotenv import load_dotenv
from sqlalchemy import make_url
from sqlalchemy.engine import URL

_PACKAGE_NAME = "context-memory"


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

    `$XDG_DATA_HOME/context-memory`, falling back to
    `~/.local/share/context-memory` when unset. Covers Linux and macOS
    without adding a `platformdirs` dependency for a single-purpose lookup
    (see D05 in docs/web-v2-decisions.md).
    """
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return base / "context-memory"


def _sqlite_url(db_path: Path) -> str:
    """Build a SQLAlchemy SQLite URL for `db_path` that parses back losslessly.

    Constructed via SQLAlchemy's own `URL.create` rather than a hand-rolled
    f-string, and verified against `make_url` -- the exact parser
    `create_db_engine` (context_memory/db.py) later runs on it -- so a data
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


def _int_env(name: str, default: int) -> int:
    """Read an int-valued env var, falling back to `default` when unset/empty.

    A non-integer value is a configuration mistake, not a runtime condition,
    so it fails loudly right here -- a clear `SystemExit` naming the
    offending variable -- rather than surfacing several frames later as a
    bare `ValueError` out of argparse's own default handling.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name}={raw!r} is not a valid integer") from None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PACKAGE_NAME,
        description="Run the Context Memory web app (bundled API + SPA).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("CONTEXT_MEMORY_HOST", "127.0.0.1"),
        help=(
            "Interface to bind (default: %(default)s; env CONTEXT_MEMORY_HOST). "
            "Left at 127.0.0.1 by default: this is a local single-user tool, "
            "not a service meant to be exposed on the LAN."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_int_env("CONTEXT_MEMORY_PORT", 8000),
        help="Port to bind (default: %(default)s; env CONTEXT_MEMORY_PORT).",
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("CONTEXT_MEMORY_DATA_DIR") or str(_default_data_dir()),
        help=(
            "Directory holding the SQLite database and an optional .env file "
            "(default: %(default)s; env CONTEXT_MEMORY_DATA_DIR)."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{_PACKAGE_NAME} {_package_version()}",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse args, prepare the data directory/environment, then serve.

    See the module docstring for why this logic lives here rather than in
    `context_memory.config`.
    """
    args = _build_parser().parse_args(argv)

    # Resolve BEFORE the chdir below, so a relative --data-dir is anchored to
    # the directory the user launched from, as they would expect.
    data_dir = Path(args.data_dir).expanduser().resolve()
    created = not data_dir.exists()
    data_dir.mkdir(parents=True, exist_ok=True)
    if created:
        # This directory holds personal memory content (the SQLite database)
        # and, via its .env, possibly an API key -- so a directory this tool
        # itself just created defaults to user-only. An already-existing
        # directory is left untouched: its permissions may be a deliberate
        # choice, and silently rewriting them is not this tool's call.
        data_dir.chmod(0o700)

    # The data dir is packaged mode's home directory (see the module
    # docstring, step 2): from here on every CWD-relative behavior --
    # pydantic-settings' env_file=".env" lookup, any relative path in a
    # setting value -- resolves inside the data dir, not wherever the user
    # happened to launch from.
    os.chdir(data_dir)

    env_file = data_dir / ".env"
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
    # credentials (context_memory/db.py's rejection messages already follow
    # this discipline -- see _url_dialect/_url_database there for the threat
    # model), and a startup banner is exactly the kind of line that ends up
    # in terminal scrollback and pasted logs. When this entry point injects
    # its own default, the value is by construction a bare file path, so THAT
    # is printed; when the environment already provides one, only its origin
    # is named, never its content.
    if "DATABASE_URL" not in os.environ:
        db_path = data_dir / "context_memory.db"
        os.environ["DATABASE_URL"] = _sqlite_url(db_path)
        database_display = str(db_path)
    else:
        database_display = "(from DATABASE_URL environment variable)"

    # Default the tool-package directory into the data dir, exactly as
    # DATABASE_URL above -- a uvx install then has the tool feature ON at a
    # stable per-user location (`<data-dir>/tools`) without the operator setting
    # anything, while an explicit TOOLS_DIR always wins. Settings maps this with
    # no env prefix (see context_memory/config.py's model_config), so the
    # variable name is exactly TOOLS_DIR. Not created here: the directory is made
    # when the first tool is installed, and context_memory/services/tools.py
    # treats a missing dir as "nothing installed yet" (empty list), so injecting
    # a not-yet-existing path is harmless. No secret is involved, so -- unlike
    # DATABASE_URL -- there is nothing to withhold from the banner; it is simply
    # left off to keep that line to the two facts it already prints.
    if "TOOLS_DIR" not in os.environ:
        os.environ["TOOLS_DIR"] = str(data_dir / "tools")

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
        f"Context Memory: data dir={data_dir} database={database_display}",
        flush=True,
    )

    uvicorn.run("context_memory.main:app", host=args.host, port=args.port)
