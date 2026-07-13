"""Tests for engine construction in app.db: the SQLite-only guard and its
dialect-only (never host/database/query-string) rejection message -- including
the fallback for a URL too malformed to parse at all -- the in-memory SQLite
rejection -- including SQLite's URI-filename `mode=memory` spelling, and the
runtime `pragma_database_list` probe that independently catches every other
URI-filename spelling the static check does not (or cannot) recognise -- (an
in-memory database has no legitimate use in this server -- it cannot survive
a restart, and safely sharing one across threads would require StaticPool,
which defeats transaction isolation between concurrent sessions) -- and
foreign-key enforcement.
"""

import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select

from app.db import create_db_engine
from app.models import MemoryItem, ProgressEntry


def test_non_sqlite_url_rejected() -> None:
    with pytest.raises(RuntimeError, match="Only SQLite database URLs are supported"):
        create_db_engine("postgresql://user:pass@localhost/db")


def test_non_sqlite_url_error_message_includes_dialect() -> None:
    with pytest.raises(RuntimeError, match='got dialect: "postgres"'):
        create_db_engine("postgres://example/db")


def test_sqlite_prefixed_non_sqlite_dialect_rejected() -> None:
    """A URL like `sqlitefoo://...` shares the literal `"sqlite"` prefix but
    parses to a distinct, unsupported dialect. A `str.startswith("sqlite")`
    guard would wrongly accept it and let it reach `create_engine()` with
    SQLite-specific `connect_args` and the PRAGMA foreign-key listener
    attached to a foreign dialect. The guard must instead compare the exact
    parsed dialect (`make_url(...).get_backend_name() == "sqlite"`), and the
    resulting error must mention only that parsed dialect -- no other URL
    component.
    """
    with pytest.raises(RuntimeError) as exc_info:
        create_db_engine("sqlitefoo://x")
    message = str(exc_info.value)
    assert 'got dialect: "sqlitefoo"' in message
    assert "x" not in message


def test_non_sqlite_url_error_message_leaks_no_url_components() -> None:
    """A misconfigured URL for another backend must not leak into logs: not
    just its password component, but nothing at all beyond the dialect name.
    In particular, a password hidden in a query parameter (e.g.
    `?sslpassword=...`, or a `PWD=` buried inside an ODBC
    `odbc_connect=...` connection string) must not leak either --
    `render_as_string(hide_password=True)` alone would not catch those, since
    it only masks a URL's own `password` component.
    """
    with pytest.raises(RuntimeError) as exc_info:
        create_db_engine("postgresql://user:s3cret@h/db?sslpassword=qs3cret")
    message = str(exc_info.value)
    assert "postgresql" in message
    assert "s3cret" not in message
    assert "qs3cret" not in message
    assert "h" not in message
    assert "db" not in message


def test_unparseable_url_error_message_leaks_nothing() -> None:
    """A DSN with no `://` separator at all (e.g. a `postgresql:` URL where
    someone forgot the slashes) cannot be parsed by SQLAlchemy's `make_url`.
    The `_url_dialect` fallback for that case must be a fixed placeholder
    that echoes nothing from the input -- a naive `scheme = url.split("://",
    1)[0]` fallback would treat the *entire* unparseable string, including
    any embedded credentials, as the "scheme" and echo it straight back.
    """
    with pytest.raises(RuntimeError) as exc_info:
        create_db_engine("postgresql:user:s3cret@host/db")
    message = str(exc_info.value)
    assert "<unparseable database URL>" in message
    assert "s3cret" not in message
    assert "host" not in message


def test_memory_sqlite_url_rejected() -> None:
    """An explicit `:memory:` database is rejected: this is a persistence
    app, so the server has no legitimate in-memory mode.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///:memory:")


def test_bare_sqlite_url_rejected() -> None:
    """The bare `sqlite://` DSN (no database at all) is just as unsafe as an
    explicit `:memory:` URL and must be rejected the same way.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite://")


def test_empty_path_sqlite_url_rejected() -> None:
    """`sqlite:///` with an empty path is SQLite's "private, anonymous
    on-disk database" -- each connection gets its own, so it shares the same
    cross-connection isolation problem as `:memory:` and must be rejected.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///")


def test_sqlite_uri_mode_memory_rejected() -> None:
    """SQLite's URI-filename form (`file:name?mode=memory...`, see
    https://www.sqlite.org/uri.html) parses with a non-empty `database`
    (`file:memdb1`) and contains no literal `:memory:` substring anywhere in
    the URL, yet `mode=memory` in its query string still opens an in-memory
    (optionally named, shared-cache) database. It must be rejected exactly
    like the plain `:memory:` spelling.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:memdb1?mode=memory&cache=shared&uri=true")


def test_sqlite_filename_containing_memory_substring_is_allowed(tmp_path: Path) -> None:
    """The static in-memory check must compare the *exact* parsed `database`
    component against ":memory:", not search for it as a substring of the
    whole URL: SQLite treats only the literal, exact filename ":memory:" as
    its special in-memory database, so a genuine on-disk file whose name
    merely *contains* that text -- e.g. "notes:memory:.db" -- must remain
    allowed. A prior version of this guard used `":memory:" in url`, which
    would have wrongly rejected this URL, since that substring does appear
    inside the filename even though the filename as a whole does not equal
    ":memory:".
    """
    db_path = tmp_path / "notes:memory:.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        SQLModel.metadata.create_all(engine)
        assert db_path.exists()
    finally:
        engine.dispose()


def test_sqlite_uri_file_backed_without_mode_memory_is_allowed(tmp_path: Path) -> None:
    """A URI-form SQLite filename *without* `mode=memory` genuinely addresses
    a durable file and must remain allowed. Uses an absolute path inside
    `tmp_path` (rather than relying on the process's cwd) so the database
    file cannot land in the repo.

    This also covers the "`file:real.db?uri=true` still accepted" case for
    the `uri=true`-gated empty-filename and `vfs=memdb` checks below: this
    URL has `uri=true` in effect, a non-empty filename, and no `vfs=memdb`,
    so neither new check should fire.
    """
    db_path = tmp_path / "realfile.db"
    engine = create_db_engine(f"sqlite:///file:{db_path}?uri=true")
    try:
        SQLModel.metadata.create_all(engine)
        assert db_path.exists()
    finally:
        engine.dispose()


def test_sqlite_uri_empty_filename_rejected() -> None:
    """`sqlite:///file:?uri=true` -- nothing between `file:` and `?` -- is
    SQLite's documented spelling for a private, anonymous on-disk database
    scoped to the single connection that opens it: the same
    unsafe-to-share-across-requests problem as the empty-path
    (`sqlite:///`) case above, just reached through the URI form with an
    empty filename instead of a bare empty path.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:?uri=true")


def test_sqlite_uri_numeric_true_flag_rejected() -> None:
    """`uri=1` is SQLAlchemy's numeric spelling of true (see
    `sqlalchemy.util.asbool`, which SQLAlchemy's pysqlite dialect actually
    uses to parse this query parameter) and must enable URI-filename parsing
    exactly like the literal string `uri=true` does -- not just that one
    exact spelling.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:?uri=1")


def test_sqlite_uri_vfs_memdb_rejected() -> None:
    """`vfs=memdb` in a URI-form SQLite URL selects SQLite's in-memory VFS
    outright, regardless of what the filename portion says -- e.g.
    `sqlite:///file:mem1?vfs=memdb&uri=true` opens an in-memory database
    named `mem1`, not a file called `mem1`. It must be rejected exactly like
    `mode=memory`.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:mem1?vfs=memdb&uri=true")


def test_sqlite_uri_titlecase_true_flag_rejected() -> None:
    """`uri=True` (title-cased, as e.g. Python's own `str(True)` would spell
    it) must be recognised as enabling URI-filename parsing
    case-insensitively, exactly like the lowercase `uri=true` spelling --
    proven here via the `vfs=memdb` check, which a case-sensitive
    `"true" in query.get("uri", [])` match would wrongly let slip through
    unrejected for this spelling.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:mem1?vfs=memdb&uri=True")


# The checks above are a static, best-effort reimplementation of pysqlite's
# own `uri=` query-string parsing rules -- and every review of them finds a
# new spelling they don't yet know to reject. The tests below prove several
# such spellings slip past every static check above unrejected, yet are
# still caught -- by construction, not by being individually taught -- by
# `create_db_engine`'s runtime `pragma_database_list` probe, which asks
# SQLite itself whether the database it actually opened is backed by a real
# file rather than re-deriving the answer from the URL text.


def test_sqlite_uri_short_truthy_flag_y_rejected() -> None:
    """`uri=y` is one of `sqlalchemy.util.asbool`'s single-letter truthy
    spellings (`"y"` and `"t"`, distinct from the words/numeral
    `_URI_TRUE_VALUES` above already recognises). `_uri_mode_enabled` does
    not recognise it, so `_is_memory_sqlite_url` does not either, and
    `sqlite:///file:mem?mode=memory&uri=y` slips past the static check
    unrejected -- even though pysqlite's real `asbool("y")` is `True`, and
    it opens exactly the same shared in-memory database as the
    already-covered `uri=true` spelling does. Confirmed empirically:
    `pragma_database_list` reports an empty `file` for it. Only the runtime
    probe catches this spelling.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:mem?mode=memory&uri=y")


def test_sqlite_uri_short_truthy_flag_t_rejected() -> None:
    """`uri=t` is `asbool`'s other single-letter truthy spelling; see
    `test_sqlite_uri_short_truthy_flag_y_rejected` immediately above -- same
    reasoning, only caught by the runtime probe, not the static check.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:mem?mode=memory&uri=t")


def test_sqlite_uri_duplicated_false_flag_still_rejected() -> None:
    """A duplicated `uri=false&uri=false` is, perhaps surprisingly, still
    rejected -- and for a subtler reason than the other spellings in this
    block: the static check does not reject it either, but not because it
    was fooled the same way. `_uri_mode_enabled` above parses each `uri=`
    value individually via `parse_qs`, sees two literal `"false"` strings,
    and -- correctly, taken alone -- decides URI-filename parsing is not
    enabled, so `_is_memory_sqlite_url` lets this URL through. But that is
    not what pysqlite's *own* coercion actually does: SQLAlchemy's
    `URL.query` stores a repeated key as a tuple of its values (here,
    `("false", "false")`), and `coerce_kw_type`'s use of `asbool`
    (`sqlalchemy.util.langhelpers`) only special-cases `str` values --  a
    non-`str` value like this tuple instead falls through to a bare
    `bool(...)` call, which is `True` for any non-empty tuple regardless of
    the strings inside it. So pysqlite actually opens this URL *with*
    URI-filename parsing enabled despite both values reading "false", and
    an empty URI filename (nothing between `file:` and `?`) is SQLite's
    private, anonymous on-disk database. Confirmed empirically:
    `pragma_database_list` reports an empty `file` for it, with
    `journal_mode` `"delete"` rather than `"memory"` (it is a private
    on-disk temp file deleted on close, not a RAM-resident database) --
    which is exactly why the probe keys off an empty *file path* rather
    than any memory-specific signal: it does not need to know *why* SQLite
    considers a database non-persistent, only that it does.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:?uri=false&uri=false")


def test_sqlite_uri_percent_encoded_memory_path_rejected() -> None:
    """`file:%3Amemory%3A` percent-encodes `:memory:` (`%3A` is `:`) as the
    URI path. `_is_memory_sqlite_url`'s literal `":memory:" in url` check
    operates on the raw URL string, which contains the *encoded* form, not
    the literal substring, so it does not match; `make_url(url).database`
    (`'file:%3Amemory%3A'`) does not decode it either, so none of the other
    static comparisons (`database == "file:"`, `mode=memory`, `vfs=memdb`)
    match it. Percent-decoding only happens once SQLite's own URI-filename
    parser actually runs, at connection time (see
    https://www.sqlite.org/uri.html), resolving the path to the literal
    string `:memory:` -- which SQLite special-cases as a private, temporary
    in-memory database. Confirmed empirically: `pragma_database_list`
    reports an empty `file` for it. The static check cannot see this
    without decoding the URL itself; the runtime probe catches it for free.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file:%3Amemory%3A?uri=true")


def test_sqlite_uri_empty_authority_form_rejected() -> None:
    """`file://` (two slashes after the scheme, then nothing) is yet
    another spelling of an empty URI filename, distinct from the bare
    `file:` the static check's `database == "file:"` comparison recognises:
    `make_url(...).database` for this URL is the literal string
    `'file://'`, which does not equal `'file:'`, so it slips past that
    check too. SQLite's URI parser treats the empty authority/path the same
    as the other empty-filename spellings above -- another private,
    anonymous on-disk database. Confirmed empirically: `pragma_database_list`
    reports an empty `file` for it. Only the runtime probe catches it.
    """
    with pytest.raises(RuntimeError, match="In-memory SQLite"):
        create_db_engine("sqlite:///file://?uri=true")


def test_pragma_database_list_reports_empty_file_for_memdb_vfs() -> None:
    """Empirical verification that the runtime probe's single "is the `file`
    column empty" check, by itself, would independently catch `vfs=memdb`
    even without the static check above (`test_sqlite_uri_vfs_memdb_rejected`)
    also rejecting it: SQLite's `memdb` VFS opens a database that is
    *addressable* by the name in its URI, so multiple connections can share
    it -- which might suggest `pragma_database_list` could report that name
    back as a "file" -- but empirically it does not. A `memdb`-VFS database
    is never backed by a real path on disk, so SQLite reports the same
    empty `file` column for it as for `:memory:` or an anonymous temp
    database. That confirms no second runtime signal (e.g. `PRAGMA
    journal_mode`) is needed alongside the probe's empty-path check.
    """
    engine = create_engine(
        "sqlite:///file:probetest?vfs=memdb&uri=true",
        connect_args={"check_same_thread": False},
    )
    try:
        with engine.connect() as connection:
            main_file = connection.execute(
                text("SELECT file FROM pragma_database_list WHERE name = 'main'")
            ).scalar()
        assert main_file == ""
    finally:
        engine.dispose()


def test_sqlite_file_prefixed_literal_filename_without_uri_flag_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `uri=true` present in the query string, pysqlite never
    enables SQLite's URI-filename parsing at all (see the
    `_pysqlite_uri_connections` section of
    `sqlalchemy.dialects.sqlite.pysqlite`): a `file:`-prefixed `database` --
    even one whose query string contains `vfs=memdb`, which would otherwise
    be rejected as selecting an in-memory VFS -- is passed to
    `sqlite3.connect()` as a literal (if oddly named) filename, and the
    query string is simply never passed to the driver at all. Such a URL is
    therefore genuinely file-backed and must remain allowed, proving the
    two checks above are correctly gated on `uri=true` rather than firing on
    `vfs=memdb`/an empty filename unconditionally.

    Without `uri=true`, pysqlite resolves a literal filename with
    `os.path.abspath()` relative to the process's current working
    directory -- and `create_db_engine`'s runtime persistence probe (see
    its comment, above) now always opens a real connection as part of
    accepting *any* URL, so this test redirects the process cwd into
    `tmp_path` first (via `monkeypatch`) to keep the resulting file out of
    the repo, and then asserts it landed there -- positive proof this URL
    is genuinely file-backed, not just an absence of a raised exception.
    """
    monkeypatch.chdir(tmp_path)
    engine = create_db_engine("sqlite:///file:mem1?vfs=memdb")
    engine.dispose()
    assert (tmp_path / "file:mem1").exists()


def test_sqlite_mode_memory_text_without_uri_flag_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sqlite:///file:x.db?mode=memory` -- with no `uri` key in the query
    string at all -- must be allowed: without `uri` parsing as true (see
    `test_sqlite_file_prefixed_literal_filename_without_uri_flag_is_allowed`
    above for why pysqlite never enables URI-filename parsing in that case),
    `mode=memory` is inert text, not a live in-memory directive -- this URL
    is a literal, persistent filename that merely happens to contain that
    substring. The `mode=memory` check must be gated on `uri` parsing as
    true exactly like the `vfs=memdb` and empty-filename checks are, not
    fire unconditionally -- which is exactly the bug this test guards
    against: a prior version of this guard checked `mode=memory` before
    (unconditionally on) the `uri=true` gate, so it wrongly rejected this
    URL even though pysqlite would have opened it as a normal file.

    Without `uri=true`, pysqlite resolves this as a literal filename via
    `os.path.abspath()` relative to the process's cwd -- and, like the test
    above, the runtime persistence probe now always opens a real connection
    as part of accepting *any* URL, so the cwd is redirected into
    `tmp_path` first to keep the resulting file out of the repo, and the
    test asserts it landed there.
    """
    monkeypatch.chdir(tmp_path)
    engine = create_db_engine("sqlite:///file:x.db?mode=memory")
    engine.dispose()
    assert (tmp_path / "file:x.db").exists()


def test_default_style_relative_sqlite_url_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact URL shape `Settings.database_url` defaults to --
    `sqlite:///./context_memory.db`, a relative, non-URI path -- must be
    accepted end-to-end, including by the runtime probe: `pragma_database_list`
    must report a non-empty (absolute) path for it, not just "no exception
    was raised". cwd is redirected into `tmp_path` so the relative
    `./context_memory.db` resolves there rather than into the repo.
    """
    monkeypatch.chdir(tmp_path)
    engine = create_db_engine("sqlite:///./context_memory.db")
    try:
        assert (tmp_path / "context_memory.db").exists()
    finally:
        engine.dispose()


def test_file_sqlite_url_does_not_use_static_pool(tmp_path: Path) -> None:
    # File-backed SQLite is the only URL shape create_db_engine accepts, and
    # it must never use StaticPool: StaticPool means a single shared DBAPI
    # connection, which defeats transaction isolation between sessions.
    db_path = tmp_path / "file.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        assert engine.pool.__class__ is not StaticPool
    finally:
        engine.dispose()


def test_file_sqlite_engine_shares_database_across_threads(tmp_path: Path) -> None:
    """File-backed SQLite naturally shares one database across threads:
    every connection, regardless of which thread opens it, reads and writes
    the same on-disk file. This proves create_all() on the main thread
    (as the lifespan does) is visible to a session opened on a worker thread
    (as a request handler does) without needing StaticPool -- unlike the
    rejected in-memory case, where each thread would otherwise see its own
    empty database.
    """
    db_path = tmp_path / "shared.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        SQLModel.metadata.create_all(engine)

        result: dict[str, object] = {}

        def worker() -> None:
            try:
                with Session(engine) as session:
                    result["items"] = session.exec(select(MemoryItem)).all()
            except Exception as exc:  # pragma: no cover - failure path only
                result["error"] = exc

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        assert "error" not in result, result.get("error")
        assert result["items"] == []
    finally:
        engine.dispose()


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    """Every connection from create_db_engine() must enforce foreign keys:
    SQLite does not do so by default, so without `PRAGMA foreign_keys=ON`
    this insert would silently succeed instead of raising -- e.g. a `POST
    /api/items/{id}/progress` racing a committed `DELETE /api/items/{id}`
    could insert a `ProgressEntry` referencing an already-deleted
    `MemoryItem`, an orphan row no API call could ever remove.
    """
    db_path = tmp_path / "fk.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(ProgressEntry(item_id=9999, note="orphan"))
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        engine.dispose()
