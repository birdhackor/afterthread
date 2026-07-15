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
     / an XDG-style default) and make sure it exists.
  2. Load `<data-dir>/.env` into the process environment, if present --
     without overriding a variable that is already set, so an explicit env
     var always wins over the file (the same precedence pydantic-settings
     itself gives env vars over `backend/.env` in dev).
  3. Default `DATABASE_URL` to a file inside that data directory, unless the
     environment already supplies one.
  4. Hand off to `uvicorn.run`, serving the same `context_memory.main:app`
     dev uses.
"""

import argparse
import os
from importlib import metadata
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

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

    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    env_file = data_dir / ".env"
    if env_file.is_file():
        # override=False: a real environment variable always wins over the
        # file, matching the precedence pydantic-settings itself gives env
        # vars over `backend/.env` in dev.
        load_dotenv(env_file, override=False)

    if "DATABASE_URL" not in os.environ:
        db_path = data_dir / "context_memory.db"
        # Three literal slashes in the f-string plus db_path's own leading
        # "/" (it is always absolute -- see `.resolve()` above) makes four
        # total: SQLAlchemy's spelling for an absolute SQLite path, versus
        # `sqlite:///relative/path`'s three. Confirmed against a real engine
        # during implementation and exercised end-to-end by e2e/wheel_smoke.sh.
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

    # English; deliberately only ever these two facts -- where data lives,
    # and where the database file is. NEVER print OPENAI_* or any other
    # secret-shaped setting here. flush=True: stdout is fully block-buffered
    # once it is not a TTY (i.e. always, once redirected to a log file or
    # piped anywhere), so without an explicit flush this line can sit
    # unseen in Python's buffer for an arbitrarily long time -- confirmed
    # during implementation: it only appeared after the first HTTP request
    # nudged the buffer, well after uvicorn's own (separately flushed)
    # startup lines. A startup banner that shows up late is as good as none.
    print(
        f"Context Memory: data dir={data_dir} database={os.environ['DATABASE_URL']}",
        flush=True,
    )

    uvicorn.run("context_memory.main:app", host=args.host, port=args.port)
