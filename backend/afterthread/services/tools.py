"""Discover, validate, and execute versioned local tool packages.

An installed package owns package-layer ``.env`` and
``.afterthread.meta/{state.json,current}``; executable content and its
``origin.json``/``summary.json`` live under ``versions/<vid>/``. ``current`` is
the only resolver: invalid pointers never guess another version.

Tools are operator-authorized local code, not a sandbox boundary. Child
environments are nevertheless built from a small allowlist plus the package
``.env``, and execution has timeout/output bounds. Advertisement captures one
version identity; a call runs that exact version with its directory as cwd.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import errno
import fcntl
import io
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, ClassVar, Literal
from uuid import uuid4

from dotenv import dotenv_values
from starlette.concurrency import run_in_threadpool

from afterthread import utf8
from afterthread.config import get_settings
from afterthread.services import llm_log
from afterthread.services.llm import LlmTool, current_interaction, tool_exception_text

# Under the "afterthread" namespace main attaches its console handler to. The one
# line this module logs is a run record that could not be written -- its type
# only, never a body (see ``_record_run``).
_logger = logging.getLogger("afterthread.tools")

# A package (and directory) name: lowercase alnum start, then up to 63 more of
# alnum/underscore/hyphen. Admits no ".", "/", or whitespace, so a traversal
# name (``".."``, ``"../evil"``, ``"/etc/x"``) can never match -- the regex is
# the first line of the path-traversal defense, backed by the check in
# ``_resolve_package_dir_no_alias`` that what the resolve gave is exactly
# ``<tools_dir>/<name>``.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_VID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
_META_DIRNAME = ".afterthread.meta"
_VERSIONS_DIRNAME = "versions"
_CURRENT_FILENAME = "current"
_PACKAGE_STATE_FILENAME = "state.json"
_ORIGIN_FILENAME = "origin.json"
_SUMMARY_FILENAME = "summary.json"
# A version's regression inputs (web-v6 §10): tool content the builder writes,
# read by the backend only to run them.
_EXAMPLES_FILENAME = "examples.json"
_EXAMPLES_MAX_BYTES = 64 * 1024
EXAMPLES_MAX = 10
_EXAMPLE_NAME_MAX_CHARS = 200
_CURRENT_MAX_BYTES = 64
_TOOLS_LOCK_FILENAME = ".afterthread-tools.lck"

# The ONLY parent-environment variables a tool subprocess inherits VERBATIM.
# Everything else -- above all OPENAI_API_KEY / OPENAI_BASE_URL -- is withheld by
# building the child env from scratch (see the module docstring's security
# stance). PATH lets the child find its interpreter; HOME/LANG/LC_ALL/TMPDIR keep
# ordinary tooling (python, locale-aware libs, temp files) behaving normally.
# TLS_NO_VERIFY is DELIBERATELY NOT in this tuple: it is not a raw parent-env
# passthrough but a settings-derived injection (see ``_build_tool_env``), so its
# child-visible value is always the normalized ``"1"``/absent pair, never
# whatever string the OPERATOR's own shell happened to export it as.
_PASSTHROUGH_ENV: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")

# Uniform cap on a tool's description (applied once at scan time, so list_tools
# and the advertised OpenAI spec always agree). A package with a longer
# description is still VALID -- only the stored/advertised copy is trimmed -- so
# an over-long description never bloats the tools array sent to the model.
_DESCRIPTION_CAP = 1000

# Hard ceiling on the tool.json FILE size. tool.json is re-read on EVERY registry
# scan (list_tools / enabled_llm_tools), so an unbounded one is a
# per-scan I/O + memory hazard; a manifest over this is listed invalid rather than
# parsed. The scan enforces it through the bounded
# ``_read_regular_file_capped`` read (at most cap+1 chars ever enter memory, then a
# len check), so an oversized manifest is never slurped whole at ANY read entry --
# and that same read's O_NONBLOCK+S_ISREG gate is what refuses a FIFO swapped in for
# tool.json. 64 KiB is orders of magnitude above any sane manifest (name +
# description + a JSON-Schema parameters block).
_MANIFEST_MAX_BYTES = 64 * 1024

# Tighter ceiling on the parameters JSON Schema specifically: unlike the rest of
# the manifest, this block is re-serialized into the tools array of EVERY LLM
# request of EVERY workflow that has this tool enabled (see _build_llm_tool), so
# an oversized schema is a recurring token/prompt-bloat + DoS hazard, not just a
# one-off read. Measured as ``len(json.dumps(parameters))``; a schema over this
# is listed invalid. 16 KiB comfortably fits a rich real-world argument schema.
_PARAMETERS_SCHEMA_MAX_BYTES = 16 * 1024

# Hard ceiling on a tool's optional ``.env`` FILE size. Unlike the manifest,
# ``.env`` is re-read on EVERY tool call (``_load_tool_dotenv`` runs inside the
# per-call ``_build_tool_env``), so an unbounded one is a per-CALL memory hazard,
# not a per-scan one; and the parsed dict becomes the child's environment BLOCK,
# where an enormous env can also hit the kernel's E2BIG limit at exec. At runtime
# ``_load_tool_dotenv`` reads through the bounded ``_read_regular_file_capped``
# (at most cap+1 chars, then a len check) and degrades to "no extra env" ({}, the
# same contract a malformed ``.env`` gets); the installer stat-gates it and refuses
# the package outright (``validate_tool_content``). 64 KiB dwarfs any real secrets file
# (a handful of KEY=VALUE lines).
_ENV_FILE_MAX_BYTES = 64 * 1024

# The package-layer SETTINGS file (web-v6 §4): operator-owned overrides of the
# defaults a version's code carries, beside the package's ``.env``. The split is
# the whole point. Every ``.env`` value is a SECRET to the redaction pipeline,
# so a setting parked there is either too short to mask (and blocks every revise)
# or long enough to be masked out of every tool result and AI log. Values here are
# deliberately NOT secrets: they never enter ``known_secret_values``, are shown to
# the builder in full, and carry no length floor. Read exactly like ``.env``
# (same bounded reader, same 64 KiB cap, same ``interpolate=False`` parse), and
# layered UNDER it so a secret can never be shadowed by a public value.
_CONFIG_FILENAME = "config.env"

# Legacy flat-layout name retained for the offline migration reader and for
# stripping builder-authored forgeries. New summaries live at
# ``versions/<vid>/.afterthread.meta/summary.json``.
_AI_META_FILENAME = ".ai_meta.json"

# Hard ceiling on the sidecar, enforced on BOTH sides: ``read_tool_meta`` refuses
# anything longer, and ``write_tool_meta`` refuses to PRODUCE anything longer.
# The symmetry is the whole point and it is a correctness property, not tidiness:
# the sidecar used to be read at ``_MANIFEST_MAX_BYTES`` (64 KiB) while the writer
# checked NOTHING, so a payload the writer happily produced could read back as
# None forever -- the write reporting success while the summary silently vanished
# from every later GET, with nothing anywhere reporting a failure.
#
# What overruns 64 KiB is worth stating precisely, because the obvious arithmetic
# does not get there and a wrong number here would send the next reader hunting
# the wrong thing. The reader's cap is compared against the DECODED text's
# length, so raw CJK is not the problem: a max-length 20 000-char
# ``origin.instructions`` plus an 8 000-char ``summary`` is ~30 000 CHARACTERS
# however many bytes it occupies. SERIALIZATION is the problem. Control
# characters are legal in that free-text install field (``str.strip`` does not
# remove them and the schema only bounds length), and ``json.dumps`` escapes each
# one to a 6-char ``\uXXXX`` -- so a legal payload serializes to six times its
# length. Redaction is the second expander, on a different axis: each masked
# value collapses to a 13-char / 35-byte marker, so text alternating 6-char
# secrets with separators roughly doubles in chars and nearly triples in bytes.
#
# Sized off those, not off a round number: worst legal serialization is
# ~120 KB (instructions) + ~48 KB (summary) + ~2 KB (URL) + keys ~= 170 KB, and
# the redaction path lands in the same range rather than multiplying with it (a
# marker is not itself escapable). 256 KiB clears that with headroom and still
# bounds the per-scan cost -- ``list_tools`` reads one sidecar per package on
# every scan, which is why an UNBOUNDED read was never an option either.
#
# NB the two sides count different UNITS and that is deliberate: the reader's
# check is on CHARS (the bounded reader is a text read), the writer's is on the
# serialized payload's UTF-8 BYTES. UTF-8 never encodes a char in less than one
# byte, so ``bytes <= cap`` implies ``chars <= cap`` -- the writer is the
# STRICTER side, which is the only direction that keeps the invariant true:
# anything the writer accepts, the reader can read back. The cost of that slack
# is bounded and accepted: a hand-written all-CJK file can make the reader buffer
# up to ~3x the cap transiently before the length check rejects it, the same
# property (at the same ratio) every other capped read in this module has.
_AI_META_MAX_BYTES = 256 * 1024

# The shape ``datetime.now(UTC).isoformat()`` gives: the ``updated_at`` a summary
# sidecar is written with (``store_summary_meta``).
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00\Z")

# Legacy flat-layout state name retained for migration and builder-content policy.
# The live toggle now belongs to package-layer
# ``.afterthread.meta/state.json`` and never falls back to ``tool.json``.
_STATE_FILENAME = ".afterthread-state.json"

# The OWNERSHIP MARKER, and the half of P1R5-1 that does the work. Our writer puts
# this key/value at the front of every state file it publishes, and every reader
# requires it before treating the document as a statement of ANYTHING: a file at
# our name that does not carry it is somebody else's and is answered exactly as a
# missing one is (disabled), never overwritten by ``set_enabled``.
#
# Why a marker at all when the name already says "afterthread": an operator or a
# future tool can still create a file under ANY name we pick, and the name alone
# gives the reader no way to tell that from our own. The marker is the difference
# between "we assume this is ours" and "this file says it is". The pair is spelled
# as two constants rather than one literal dict so the writer and the recognizer
# cannot drift, which is the same argument ``_RESERVED_PACKAGE_FILENAMES`` makes
# about the names themselves.
_STATE_MARKER_KEY = "afterthread"
_STATE_MARKER_VALUE = "tool-state"

# Hard ceiling on the state file, far tighter than the manifest's or the sidecar's
# and for a reason that is theirs inverted: this file is read on EVERY scan of
# EVERY package (``scan_installed``, so every ``list_tools`` AND every advertisement
# for every AI request) and it carries ONE boolean. Our writer emits ~20 bytes, so
# 4 KiB is three orders of magnitude of headroom for a hand-edited file with
# generous whitespace, while anything past it is by definition not our shape --
# and is refused as UNREADABLE rather than parsed (see ``_read_enabled_state``).
_STATE_MAX_BYTES = 4 * 1024

# Every filename at a PACKAGE ROOT that belongs to the backend rather than to the
# package's content. This tuple must never be applied to a BuildRoot or VersionRoot:
# v5 puts all tool content there, and only ``.afterthread.meta/`` is backend-owned
# at that scope. In particular, migration deliberately carries a FOREIGN legacy
# state file into the first version byte-for-byte; treating this package-root
# namespace as a recursive or version-root namespace makes the next revise omit
# ordinary tool content from the newly published version.
#
# A tuple rather than separate literals still keeps the two legacy package-layer
# names together for code that reasons about that old namespace.
_RESERVED_PACKAGE_FILENAMES: tuple[str, ...] = (_AI_META_FILENAME, _STATE_FILENAME)

# The mode floor every published sidecar carries (R11). The backend MUST be able
# to read back what it just wrote -- that is the same writer-accepts-implies-
# reader-reads-back invariant _AI_META_MAX_BYTES states for SIZE, now stated for
# PERMISSIONS. Two holes made it not hold: mkstemp's 0o600 is masked by the
# process umask, so a service started under a umask that strips owner bits (an
# operator's 0o277, a wrapper's 0o777) published a sidecar the very next
# read_tool_meta could not open -- write_tool_meta returning True while every
# later GET answered "no summary", and every regenerate spending a whole LLM call
# to rewrite a file it would then fail to read; and inheriting an existing file's
# mode verbatim (R7-3) propagated the same hole once a sidecar had ever landed
# without owner-read. So the fd is fchmod'd to (inherited | this) before publish:
# the operator's GROUP/OTHER customization is honored exactly as R7-3 intended,
# while owner read+write is not negotiable -- this file is backend-owned state,
# not operator content.
_OWNER_RW = 0o600

# Cap on the STORED summary text (D40). Generous next to InstallResult's 2000-char
# progress note because this text is the tool's user-facing DOCUMENTATION -- what
# it does, how it runs, its inputs/outputs and limits -- and it is rendered on
# demand in one panel, not carried in any prompt or tool spec.
#
# It lives HERE, next to the sidecar it bounds, rather than in ``tool_meta`` where
# it started, because the CUT does (see ``store_summary_meta``): the LLM result
# model used to redact-and-cap inside its pydantic validator, which runs on the
# EVENT LOOP, and the redaction half of that pair reads the filesystem. Both
# halves moved to the storage boundary together -- they are one ordered operation
# (redact while the text is whole, THEN slice) and splitting them across two
# modules would be splitting an invariant nobody can then verify by reading
# either. tool_meta is the importer of this module, so the constant could not
# live there and be used here without a cycle.
_TOOL_SUMMARY_CAP = 8000

# Appended when a tool's stdout (or a failure's stderr) is cut for size. Mirrors
# the truncation markers in memory_ai / llm_log so an operator who has seen those
# recognizes this one; the distinct wording ("工具輸出" = tool output) tells it
# apart from a truncated item section or a truncated log body.
_OUTPUT_TRUNCATION_MARKER = "…[工具輸出過長已截斷]"

# The one time limit of the teardown in ``_communicate_bounded``, counted from the
# moment it starts. With ``on_reaped``, it bounds the re-sent group kill, the watch
# for the group to be gone, and the join of the reader/writer threads; the reap of
# the leader has no bound. One limit is enough because once the group is gone no
# member holds a write end of the pipes, so the readers hit EOF and the join
# completes at once -- the waits need no adding up. Without ``on_reaped`` it bounds
# only the join, and each thread is given the whole limit, purely so a wedged pipe
# can never turn cleanup itself into a hang.
_REAP_TIMEOUT_SECONDS = 5.0

# Whether one SIGKILL to a process group can miss a member forked while it is
# being sent. XNU's ``killpg`` first collects the group's members and then
# signals them one by one, so a child that a member forks meanwhile is on no list
# and is never signalled. Linux misses none: ``copy_process`` holds a group signal
# that arrives during a fork for the new child as well. Where a miss is possible,
# the teardown in ``_communicate_bounded`` sends the kill again
# (``_resend_group_kill``).
_GROUP_KILL_CAN_MISS_A_FORK = sys.platform == "darwin"

# The pause between those re-sent kills. A send reaches every member alive when it
# is sent, so another is needed only for those forked while the one before it was
# being delivered -- a count the pause does little to change. What the pause sets
# is how long the sends take, and what a loop that runs to the time limit costs --
# a member stuck in an uninterruptible wait in the kernel keeps answering signal 0
# however often it is sent SIGKILL. At one millisecond the first is a few
# milliseconds, and the second one round of two cheap system calls for each
# millisecond of the limit.
_GROUP_KILL_RESEND_SECONDS = 0.001

# Poll interval for the NON-reaping wait that watches the tool leader for exit
# (see _communicate_bounded). We cannot block in ``proc.wait`` there: reaping the
# leader frees its pid and lets ``os.killpg(proc.pid)`` race a REUSED group, so we
# instead poll ``os.waitid`` with WNOWAIT (detect exit WITHOUT reaping) under our
# own deadline. The only cost of polling over blocking is up to ONE interval of
# added latency detecting a natural exit; 25 ms is imperceptible for a local tool
# call yet keeps the poll loop's CPU wake-ups negligible. The teardown's watch for
# the group to be gone (``_group_seen_gone_by``) polls at the same pace.
_WAIT_POLL_SECONDS = 0.025

# Chunk size for the bounded incremental pipe reads (see _communicate_bounded).
# 64 KiB is large enough that draining a normal tool's output is one or two
# reads, and small enough that a runaway stream is caught within roughly one
# chunk past the cap -- so a stream far over the cap is stopped after buffering
# at most ``cap + one chunk`` transiently, never the whole unbounded output.
_READ_CHUNK_CHARS = 64 * 1024


@dataclass(slots=True)
class _PackageScan:
    """The result of validating one candidate package directory.

    One ``Resolution`` owns every version-derived field in the row. ``valid`` gates
    advertisement and execution; ``enabled`` is package-layer state and is false
    whenever resolution or state is unusable. ``identity`` vouches for the
    resolved version's manifest, while ``notice`` carries the non-fatal FOREIGN
    state-file diagnostic.
    """

    name: str
    package_root: PackageRoot
    resolution: Resolution
    valid: bool
    enabled: bool
    description: str | None
    error: str | None
    parameters: dict[str, Any] | None
    entry: list[str] | None
    identity: tuple[int, int, int] | None
    advertisement_generation: _AdvertisementGeneration | None = None
    notice: str | None = None


@dataclass(frozen=True, slots=True)
class PackageLayoutRoot:
    """A package-shaped layout that may still be staging or already retired."""

    path: Path


@dataclass(frozen=True, slots=True)
class PackageRoot(PackageLayoutRoot):
    """A real installed package directory, never a version or staging build."""


@dataclass(frozen=True, slots=True)
class VersionRoot:
    """One committed installed version directory."""

    path: Path


@dataclass(frozen=True, slots=True)
class BuildRoot:
    """Tool content that has not been installed yet."""

    path: Path


@dataclass(frozen=True, slots=True)
class PreviousAbsent:
    """An ``origin.json`` that did not carry the required lineage field."""


@dataclass(frozen=True, slots=True)
class PreviousNull:
    """An explicit JSON null: this version has no predecessor."""


@dataclass(frozen=True, slots=True)
class PreviousValue:
    """The JSON value written at ``previous`` (shape validation belongs to lineage)."""

    value: object


PREVIOUS_ABSENT = PreviousAbsent()
PREVIOUS_NULL = PreviousNull()
type Previous = PreviousAbsent | PreviousNull | PreviousValue


@dataclass(frozen=True, slots=True)
class Resolved:
    """A package whose ``current`` names one committed version.

    ``previous`` is captured from that version's commit marker during the same
    resolution.  List rows and discard therefore cannot describe one current
    version while deriving lineage from a second pointer read.  The tagged
    value preserves missing, explicit null, and present JSON values as three
    different states; a publisher-authored marker always has the field.
    """

    package_root: PackageRoot
    version_root: VersionRoot
    vid: str
    previous: Previous


@dataclass(frozen=True, slots=True)
class Unresolved:
    """An installed package with no usable current version."""

    package_root: PackageRoot
    reason: str


type Resolution = Resolved | Unresolved


@dataclass(frozen=True, slots=True)
class PackageLayoutResolved:
    """A package-shaped layout whose ``current`` names a committed version."""

    package_root: PackageLayoutRoot
    version_root: VersionRoot
    vid: str
    previous: Previous


@dataclass(frozen=True, slots=True)
class PackageLayoutUnresolved:
    """A package-shaped layout with no usable current version."""

    package_root: PackageLayoutRoot
    reason: str


type PackageLayoutResolution = PackageLayoutResolved | PackageLayoutUnresolved


@dataclass(frozen=True, slots=True)
class _ContentScan:
    """The shared manifest/entry/content-policy result for a build or version."""

    name: str
    directory: Path
    valid: bool
    description: str
    error: str | None
    parameters: dict[str, Any] | None
    entry: list[str] | None
    identity: tuple[int, int, int] | None


# --- discovery / validation ------------------------------------------------


def tools_dir() -> Path | None:
    """The canonical configured tools directory, or None when disabled.

    ``settings.tools_dir`` empty ("") means OFF -- None here makes every reader
    (``list_tools`` / ``enabled_llm_tools`` / the mutators) a no-op. Mirrors
    ``llm_log``'s settings-driven pattern; packaged mode's cli.py injects
    ``<data-dir>/tools`` so a uvx install has it set without operator action.

    Canonicalizing the BASE here gives every downstream ``PackageRoot`` and
    ``VersionRoot`` one spelling. It deliberately does not resolve a package
    child: package/version symlink refusals still inspect their own final
    components before anything follows them.

    Resolved with errors raised, except where a look on the way sees nothing
    there. Components which do not exist yet are kept as written
    (``ALLOW_MISSING``), so a tools directory not created yet still has its
    path. A component that is a file (``ENOTDIR``) means as surely that no
    directory is there: the configured path is returned as it is, made absolute
    only -- not resolved, its ``..`` kept -- so every look under it sees nothing
    there too. Any other failure of a look on the way -- a directory above it
    that cannot be searched, a link loop, an I/O error -- raises
    ``ToolsDirUnresolvedError``: a lookup that skipped it would read a link it
    could not see as no link, fold the ``..`` after it as text, and answer with
    another directory. It is never turned into None, which means the feature is
    off.
    """
    raw = get_settings().tools_dir.strip()
    if not raw:
        return None
    configured = os.path.expanduser(raw)
    try:
        try:
            return Path(os.path.realpath(configured, strict=os.path.ALLOW_MISSING))
        except NotADirectoryError:
            return Path(configured).absolute()
    except OSError as exc:
        raise ToolsDirUnresolvedError(configured, type(exc).__name__) from exc


class OperatorFacingError(Exception):
    """A failure whose message is written for the operator, to be shown as it is.

    The message names only paths, tool names, failure type names and what to do
    next: never a value, never a failure's own text. The API answers one as a 500
    carrying that message under ``code`` (``main``), and an install or revise job
    that ends on one shows it as the job's error (``tool_builder._run_job``).
    """

    code: ClassVar[str]


class ToolsDirUnresolvedError(OperatorFacingError):
    """The configured tools directory's path could not be looked up (``tools_dir``).

    ``path`` is the configured path, ``error`` the failure's type name, never its
    text. The message names those two and what to do next.
    """

    code = "tools_dir_unresolved"

    def __init__(self, path: str, error: str) -> None:
        self.path = path
        self.error = error
        super().__init__(
            f"無法解析工具目錄的設定路徑 {_utf8_safe(path)}（{error}）。"  # noqa: RUF001
            "請檢查這個路徑上層各目錄的權限、連結與磁碟狀態後重試，"  # noqa: RUF001
            "或清空 TOOLS_DIR 設定（工具功能隨之關閉）。"  # noqa: RUF001
        )


_REQUEST_TOOLS_LOCK_FD: ContextVar[int | None] = ContextVar("_REQUEST_TOOLS_LOCK_FD", default=None)


class ToolsLockUnavailableError(RuntimeError):
    """The persistent lock path cannot be used by this backend process."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(
            f"Tools lock {path} is unavailable: {reason}. "
            "Repair ownership and owner read/write permissions on this existing inode; "
            "do not delete or recreate it."
        )


def _repair_existing_tools_lock_mode(lock_path: Path) -> None:
    """Restore the owner-rw floor before an O_RDWR open can reject the inode."""

    try:
        info = os.stat(lock_path, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise ToolsLockUnavailableError(lock_path, f"cannot inspect it: {reason}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ToolsLockUnavailableError(lock_path, "the reserved path is not a regular file")

    current_mode = stat.S_IMODE(info.st_mode)
    safe_mode = current_mode | _OWNER_RW
    if safe_mode == current_mode:
        return
    effective_uid = os.geteuid()
    if info.st_uid != effective_uid:
        raise ToolsLockUnavailableError(
            lock_path,
            f"it is owned by uid {info.st_uid}, but the backend runs as uid {effective_uid}",
        )
    try:
        os.chmod(lock_path, safe_mode, follow_symlinks=False)
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise ToolsLockUnavailableError(
            lock_path,
            f"owner read/write mode repair failed: {reason}",
        ) from exc


def _open_tools_lock(base: Path) -> int:
    """Open the one persistent lock inode shared by every cooperating operation.

    The file is intentionally never unlinked. Deleting it by hand and recreating
    it produces a different inode: holders of the old inode and checkers of the
    new one no longer conflict, so protection lapses silently. ``O_NOFOLLOW`` and
    the regular-file gate fail closed if the reserved name is replaced.

    ``flock`` coordinates only code that checks this inode. Every checker in this
    subsystem is ours; it is not a sandbox against an operator or tool that edits
    package files directly.
    """

    lock_path = base / _TOOLS_LOCK_FILENAME
    # A pre-existing owner-read-only/no-access inode rejects O_RDWR before an
    # opened descriptor exists to fchmod. Repair the backend-owned inode in place
    # first; a foreign owner or an unusable reserved path fails with its own
    # actionable condition rather than masquerading as lock contention.
    _repair_existing_tools_lock_mode(lock_path)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise ToolsLockUnavailableError(lock_path, f"open failed: {reason}") from exc
    try:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            reason = exc.strerror or str(exc)
            raise ToolsLockUnavailableError(
                lock_path, f"descriptor inspection failed: {reason}"
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            raise ToolsLockUnavailableError(lock_path, "the opened inode is not a regular file")
        # open(2)'s mode is filtered through the process umask. Without this
        # owner-rw floor, a restrictive umask can create the persistent inode
        # owner-read-only, so every later O_RDWR acquisition fails forever after
        # this creating descriptor closes. Preserve any group/other bits while
        # making every backend-owned lock inode reopenable by its owner.
        current_mode = stat.S_IMODE(info.st_mode)
        safe_mode = current_mode | _OWNER_RW
        if safe_mode != current_mode:
            try:
                os.fchmod(fd, safe_mode)
            except OSError as exc:
                reason = exc.strerror or str(exc)
                raise ToolsLockUnavailableError(
                    lock_path,
                    f"descriptor mode repair failed: {reason}",
                ) from exc
    except BaseException:
        os.close(fd)
        raise
    return fd


def acquire_shared_tools_lock() -> int | None:
    """Take the request-wide shared lock, blocking only behind a destroyer.

    ``None`` means the feature is unconfigured or its root does not exist, in
    which case no installed tool can be advertised. A coroutine takes it through
    ``acquire_shared_tools_lock_async``: the router does so before
    scanning/advertising and holds the returned descriptor through the complete
    LLM request.
    """

    base = tools_dir()
    if base is None or not base.is_dir():
        return None
    fd = _open_tools_lock(base)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
    except BaseException:
        os.close(fd)
        raise
    return fd


def release_tools_lock(fd: int | None) -> None:
    """Close one parent reference; the kernel releases on the last reference.

    A close never waits behind another holder, so a coroutine calls it directly
    in its ``finally``: an await there can be stopped before it runs by a
    cancellation still in force (an anyio scope's), which would leave the
    reference open.
    """

    if fd is not None:
        os.close(fd)


class _LockHandover:
    """Where a worker thread hands a lock descriptor to the coroutine waiting for it.

    Whichever side comes second closes it: the worker, when the waiter has
    already given up by the time it has the descriptor; the waiter, when it
    gives up after the worker handed it over. Both answer under one
    ``threading.Lock``, so exactly one of them closes it, in either order.
    ``close`` is how (``release_tools_lock``).
    """

    def __init__(self, close: Callable[[int], None] = release_tools_lock) -> None:
        self._guard = threading.Lock()
        self._abandoned = False
        self._handed: int | None = None
        self._close = close

    def hand_over(self, fd: int | None) -> int | None:
        """The worker's side: ``fd`` for the waiter, or None, having closed ``fd``,
        when the waiter has already given up."""
        if fd is None:
            return None
        with self._guard:
            if not self._abandoned:
                self._handed = fd
                return fd
        self._close(fd)
        return None

    def abandon(self) -> None:
        """The waiter's side, giving up: nothing is handed over from now on, and a
        descriptor already handed over is closed."""
        with self._guard:
            self._abandoned = True
            fd, self._handed = self._handed, None
        if fd is not None:
            self._close(fd)


async def acquire_shared_tools_lock_async() -> int | None:
    """``acquire_shared_tools_lock`` for a coroutine: taken on a worker thread, and
    handed back however the wait ends.

    The worker and the waiter meet in a ``_LockHandover``, and whichever comes
    second closes the descriptor. A wait that ends before the worker hands it
    over -- a plain ``Task.cancel`` ends it at once, the worker perhaps still
    waiting for the lock -- leaves the worker to close what it takes; a wait that
    ends after closes it itself. A cancellation that waits for the worker (an
    anyio scope's) does not end the wait: the descriptor is returned, and the
    caller's next await raises inside the ``try`` that releases it. A failure of
    the acquisition itself goes out as it is, with no descriptor: the acquisition
    closes its own.

    The caller owns a returned descriptor: it enters the ``try`` whose
    ``finally`` calls ``release_tools_lock`` before any other await.
    """
    handover = _LockHandover()

    def _acquire() -> int | None:
        return handover.hand_over(acquire_shared_tools_lock())

    try:
        return await run_in_threadpool(_acquire)
    except BaseException:
        handover.abandon()
        raise


@contextlib.contextmanager
def bind_request_tools_lock(fd: int | None) -> Iterator[None]:
    """Expose the request's descriptor while its advertised handlers are built."""

    token = _REQUEST_TOOLS_LOCK_FD.set(fd)
    try:
        yield
    finally:
        _REQUEST_TOOLS_LOCK_FD.reset(token)


@contextlib.contextmanager
def exclusive_tools_lock(base: Path | None = None) -> Iterator[bool]:
    """Try the destroyer lock once; yield False instead of waiting.

    Delete, discard, stale cleanup, and migration all use this same helper. Any
    genuine contention yields False for the retryable HTTP 409. An unusable lock
    path or another flock failure raises ``ToolsLockUnavailableError`` so callers
    cannot misreport a permanent operator/configuration problem as active work.
    """

    root = base if base is not None else tools_dir()
    if root is None or not root.is_dir():
        yield False
        return
    fd = _open_tools_lock(root)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        except OSError as exc:
            lock_path = root / _TOOLS_LOCK_FILENAME
            reason = exc.strerror or str(exc)
            raise ToolsLockUnavailableError(
                lock_path,
                f"exclusive flock failed: {reason}",
            ) from exc
        yield True
    finally:
        # Close rather than unlink: this releases our open-file reference while
        # preserving the one inode every future checker must lock.
        os.close(fd)


def legacy_preflight_accepts_shape(package: Path) -> bool:
    """Whether migration will treat this root as legacy rather than target/damaged.

    This deliberately answers only the namespace-shape gate shared by runtime
    diagnostics and migration preflight. Migration still validates the legacy
    manifest, special files, state, sidecar, and dotenv before accepting it.
    """

    for reserved in (_VERSIONS_DIRNAME, _META_DIRNAME):
        try:
            os.lstat(package / reserved)
        except FileNotFoundError:
            continue
        except OSError:
            return False
        else:
            return False
    return True


def _valid_entry(entry: Any) -> bool:
    """True when ``entry`` is a non-empty argv list of non-empty strings."""
    return (
        isinstance(entry, list)
        and len(entry) > 0
        and all(isinstance(part, str) and part for part in entry)
    )


def _is_within(base: Path, candidate: Path) -> bool:
    """True when the RESOLVED ``candidate`` lies at or under the RESOLVED ``base``.

    ``relative_to`` raises ValueError when it does not.
    """
    try:
        candidate.relative_to(base)
    except ValueError:
        return False
    return True


def _entry_file_exists(directory: Path, entry: list[str]) -> bool:
    """True when some ``entry`` element names an existing file inside ``directory``.

    ``entry`` is argv, so its elements mix an interpreter (``python3`` -- found
    on PATH, not a file here), flags, and the script (``run.py`` -- a real file
    in the package). Rather than guess which element is the script, treat EACH as
    a candidate path relative to the package and require at least one to resolve
    (with containment) to an existing regular file. That accepts
    ``["python3","run.py"]`` and ``["./run.sh"]`` alike, while an absolute or
    ``..``-laden element resolves OUTSIDE the package and is correctly ignored --
    so a bare ``["python3"]`` (no script shipped) is reported as a missing entry
    file rather than silently "valid".
    """
    base = directory.resolve()
    for part in entry:
        candidate = (directory / part).resolve()
        if _is_within(base, candidate) and candidate.is_file():
            return True
    return False


def _read_regular_file_capped(path: Path, cap: int) -> str | None:
    """Read at most ``cap + 1`` chars of ``path``, but ONLY if it is a regular file.

    The ONE bounded-regular-file read the whole tool subsystem funnels through --
    ``read_file`` (tool_builder), ``.env`` loading, and the ``tool.json`` scan all
    call it, so the jail-hardening below is enforced identically everywhere instead
    of re-derived per call site. Returns the text (<= ``cap + 1`` chars, so the
    caller can tell "over the cap" from a ``len > cap`` check) or None for EVERY
    refusal/failure -- the caller maps None onto its own error or degrade.

    Each open flag defends a distinct vector:

    * ``O_NONBLOCK`` -- opening a FIFO for read normally BLOCKS until a writer
      appears; a tool (or the builder via run_shell) could ``mkfifo`` a path in
      place of a real file and wedge the reading threadpool worker FOREVER (the
      outer asyncio timeout only cancels the await, never the wedged worker). With
      O_NONBLOCK the open returns at once and the S_ISREG gate below rejects it.
      For a regular file O_NONBLOCK is a no-op, so ordinary reads are unaffected;
    * ``O_NOFOLLOW`` -- refuses a symlinked FINAL component (ELOOP -> None). This
      is the enforced boundary of the staging jail (D21): even if a symlink is
      swapped in AFTER a caller resolved the path (a TOCTOU race), the open itself
      declines to follow it, so a link to a file outside the package/staging is
      never read through.

    The ``fstat`` after open is the HARD regular-file gate (S_ISREG): a socket,
    device or directory is refused even though it opened. ``fdopen`` takes
    OWNERSHIP of the fd, so once it succeeds its context manager owns the close;
    ``fd_owned`` tracks the handoff so the ``finally`` closes the raw fd on exactly
    the paths that never reached ``fdopen`` (never a double close).

    The read itself is ``_read_regular_text_capped``, which also says why there is
    no text; this keeps only the text, so missing, refused and failed are all None.
    """
    return _read_regular_text_capped(path, cap).text


@dataclass(frozen=True, slots=True)
class _CappedText:
    """One ``_read_regular_text_capped``: the text, or why there is none.

    ``text`` is None exactly when ``refusal`` is set:

    * ``"absent"`` -- the open found nothing there (``ENOENT``, ``ENOTDIR``);
    * ``"not_regular"`` -- what is there is not read: the final component is a
      link the open would not follow (a ``lstat`` saw the link itself), or
      ``fstat`` of what opened is not a regular file;
    * ``"failed"`` -- any other failure to open, look at or read it, the
      ``ELOOP`` of a link loop above it included; ``error`` is its type name.

    ``identity`` is ``(st_dev, st_ino, st_mtime_ns, st_ctime_ns, st_size)`` from
    the ``fstat`` of the descriptor the text was read from.
    """

    text: str | None
    refusal: Literal["absent", "not_regular", "failed"] | None = None
    error: str | None = None
    identity: tuple[int, int, int, int, int] | None = None


def _is_link_itself(path: Path) -> bool:
    """Whether a ``lstat`` of ``path`` sees a link; False when the look fails."""
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError:
        return False


def _read_regular_text_capped(path: Path, cap: int) -> _CappedText:
    """``_read_regular_file_capped``'s read, saying why it has no text (``_CappedText``).

    The same open flags, the same regular-file gate, the same decoding and the
    same ``cap + 1`` bound; only the answer is fuller. An ``ELOOP`` from the open
    is a link the open refused to follow only when a ``lstat`` sees the final
    component is one; a loop among the directories above it is a failure.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError, NotADirectoryError:
        return _CappedText(None, "absent")
    except OSError as exc:
        # A symlinked leaf O_NOFOLLOW refused (ELOOP), a FIFO with no writer on
        # some platforms, a permission error, ...: never raised into the caller.
        if exc.errno == errno.ELOOP and _is_link_itself(path):
            return _CappedText(None, "not_regular")
        return _CappedText(None, "failed", type(exc).__name__)
    fd_owned = True  # we own the raw fd until fdopen takes it over
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return _CappedText(None, "not_regular")
        identity = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
        handle = os.fdopen(fd, encoding="utf-8", errors="replace")
        fd_owned = False  # fdopen now owns fd; closing the handle closes it
        with handle:
            return _CappedText(handle.read(cap + 1), identity=identity)
    except OSError as exc:
        return _CappedText(None, "failed", type(exc).__name__)
    finally:
        if fd_owned:
            os.close(fd)


def _read_regular_bytes_capped(path: Path, cap: int) -> bytes | None:
    """Read at most ``cap + 1`` bytes from one real file without following it."""

    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    fd_owned = True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb") as handle:
            fd_owned = False
            return handle.read(cap + 1)
    except OSError:
        return None
    finally:
        if fd_owned:
            os.close(fd)


def _write_regular_file(path: Path, content: str) -> bool:
    """Write ``content`` as utf-8 to ``path``, but ONLY if it is a regular file.

    The WRITE-side mirror of ``_read_regular_file_capped`` -- ``write_file`` and
    ``write_secret`` (tool_builder) funnel through it, so the same jail-hardening
    holds on every un-published write the tool subsystem does instead of being
    re-derived per call site (the backend's OWN package files go through
    ``_write_package_file_atomic`` instead, which needs a publish rather than a
    truncating open). Returns True on success; False (never raises)
    on ANY ``OSError``, so a caller degrades cleanly onto its own error string /
    False contract rather than 500ing.

    Each open flag defends the write-side of a distinct vector:

    * ``O_NONBLOCK`` -- opening a reader-less FIFO write-only returns ENXIO
      IMMEDIATELY (POSIX) instead of BLOCKING until a reader appears; this is the
      write-side of the exact hazard ``_read_regular_file_capped``'s O_NONBLOCK
      closes on the read side (a tool, or the builder via run_shell, can
      ``mkfifo`` a path in place of a real file and wedge the writing threadpool
      worker FOREVER -- the outer asyncio timeout only cancels the await, never the
      wedged worker). For a regular file O_NONBLOCK is a no-op, so ordinary writes
      are unaffected;
    * ``O_NOFOLLOW`` -- refuses a symlinked FINAL component (ELOOP -> False): a
      generated symlink must never redirect a write OUT of staging / its package,
      hardening the D21-enforced jail exactly as the read helper does on the read
      side (a backstop against a symlink raced in AFTER a caller resolved the path);
    * ``O_CREAT | O_TRUNC`` with mode ``0o600`` -- create-or-overwrite with
      owner-only permissions (tool packages already run under the service uid, so
      there is no reason to widen the mode on a file we author).

    Parent directories are created BEFORE the open (the meta-tool contract
    auto-creates them for ``write_file``; for ``write_secret``'s ``.env`` the
    parent already exists, so ``makedirs(exist_ok=True)`` is a no-op). The
    ``fstat`` after open is the HARD regular-file gate (S_ISREG): a pre-existing
    device/socket/FIFO that somehow opened is still refused before a single byte is
    written. ``fdopen`` takes OWNERSHIP of the fd, so ``fd_owned`` tracks the
    handoff and the ``finally`` closes the raw fd on exactly the paths that never
    reached ``fdopen`` (never a double close) -- mirroring the read helper.
    """
    try:
        os.makedirs(path.parent, exist_ok=True)
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NONBLOCK | os.O_NOFOLLOW,
            0o600,
        )
    except OSError:
        # ENXIO (reader-less FIFO), ELOOP (symlinked leaf), EPERM, ENOTDIR (a parent
        # component that is not a directory), ... -- every open/makedirs failure
        # degrades to False rather than raising into the caller.
        return False
    fd_owned = True  # we own the raw fd until fdopen takes it over
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd_owned = False  # fdopen now owns fd; closing the handle closes it
        with handle:
            handle.write(content)
        return True
    except OSError:
        return False
    finally:
        if fd_owned:
            os.close(fd)


# The operator-facing reason a backend-owned state slot cannot be read. It is
# category-only and never exposes a filesystem error string.
_STATE_UNREADABLE_ERROR = ".afterthread.meta/state.json exists but is not readable"

# The operator-facing note for a file at our name that is NOT ours (P1R5-1): it has
# no ownership marker, so it is somebody else's -- a tool's own cursor/cache/
# settings, or a hand-written file that omitted the marker. Content may stay valid,
# but the package is disabled and PATCH refuses; the backend never touches the
# foreign file.
_STATE_FOREIGN_NOTICE = (
    ".afterthread.meta/state.json is not this backend's state file (no "
    f'"{_STATE_MARKER_KEY}": "{_STATE_MARKER_VALUE}" marker) and is being left '
    "alone: the package is disabled and its toggle cannot be changed. Add the "
    "marker, or move that file to another name."
)


@dataclass(frozen=True, slots=True)
class _EnabledState:
    """One package-layer state result.

    OURS with a boolean is authoritative. ABSENT and FOREIGN are disabled;
    UNREADABLE is disabled and invalid. Keeping ownership separate prevents a
    foreign document from being overwritten while eliminating the legacy
    manifest fallback.
    """

    ours: bool
    enabled: bool
    error: str | None
    notice: str | None


# The three CONSTANT answers, minted once rather than per call: this runs for every
# package on every scan and twice per tool call, and the class is frozen, so there
# is nothing a shared instance can be mutated into. Only the fourth shape (ours, and
# readable) carries a value and has to be built.
_ABSENT_STATE = _EnabledState(ours=False, enabled=False, error=None, notice=None)
_UNREADABLE_STATE = _EnabledState(
    ours=True, enabled=False, error=_STATE_UNREADABLE_ERROR, notice=None
)
_FOREIGN_STATE = _EnabledState(ours=False, enabled=False, error=None, notice=_STATE_FOREIGN_NOTICE)


def _read_enabled_state(package_root: PackageRoot) -> _EnabledState:
    """Read package-layer ``state.json``. Total: never raises, always answers.

    ABSENT, FOREIGN and UNREADABLE are DIFFERENT QUESTIONS and are answered
    differently (see ``_EnabledState``), so this cannot go through
    ``_read_regular_file_capped`` alone -- that helper folds "no such file" and
    "refused to read it" into the same None. The ``lstat`` above it is what tells
    those two apart, and its error handling copies an adjudication this subsystem
    has already made once, for ``.env`` at promote time (D40 P3b r2-3):
    ``FileNotFoundError`` is the ONLY evidence of absence; every other ``OSError``
    (EIO, ESTALE, EACCES, a parent that is not a directory) is a failure to LOOK,
    and treating a failure to look as "there is nothing here" is what would
    silently re-enable a disabled tool. A non-regular file at the name (a symlink
    aimed out of the package, a FIFO, a directory) is likewise UNREADABLE rather
    than absent.

    The MARKER is what tells FOREIGN from ours, and it is checked before a single
    field is believed (P1R5-1). A document we could read that does not carry
    ``{"afterthread": "tool-state"}`` is not ours -- whether it is a tool's own
    JSON settings, a plain-text cursor, or a hand-written ``{"enabled": false}``
    that omitted the marker -- and "not ours" is answered as ABSENT is, with a
    ``notice`` the listing can show. Nothing here writes, so the file is untouched
    either way; ``set_enabled`` is where the refusal to overwrite it lives.

    What stays UNREADABLE rather than becoming FOREIGN is the set we cannot read AT
    ALL: refused by the OS, non-regular, past ``_STATE_MAX_BYTES``. There is no
    marker to find in a file we never decoded, and at a name that says "afterthread"
    the fail-closed reading -- ours, broken, listed invalid and switched off (R2) --
    is the one that cannot re-enable something by accident. A marker-bearing
    document with a missing or non-``bool`` ``enabled`` joins them for the R2 reason
    unchanged: it is OUR file failing to say anything about the operator's intent,
    which is exactly as silent as a truncated one.

    ``RecursionError`` is caught next to ``ValueError`` for the reason
    ``read_tool_meta`` documents: this runs once per package on EVERY scan, so a
    single hand-edited file escaping as an exception would break the whole 工具
    page and every AI request's advertisement, not just its own row. A document
    that will not parse is FOREIGN rather than unreadable, because "not JSON at
    all" is the shape a tool's own cursor or cache file has.
    """
    path = package_root.path / _META_DIRNAME / _PACKAGE_STATE_FILENAME
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return _ABSENT_STATE
    except OSError:
        return _UNREADABLE_STATE
    if not stat.S_ISREG(info.st_mode):
        return _UNREADABLE_STATE
    text = _read_regular_file_capped(path, _STATE_MAX_BYTES)
    if text is None or len(text) > _STATE_MAX_BYTES:
        return _UNREADABLE_STATE
    try:
        raw = json.loads(text)
    except ValueError, RecursionError:
        return _FOREIGN_STATE
    if not isinstance(raw, dict) or raw.get(_STATE_MARKER_KEY) != _STATE_MARKER_VALUE:
        return _FOREIGN_STATE
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        return _UNREADABLE_STATE
    return _EnabledState(ours=True, enabled=enabled, error=None, notice=None)


def _effective_enabled(state: _EnabledState) -> bool:
    """Return the package-layer toggle, failing closed for every non-OURS state.

    Migration is all-or-nothing, so there is no legacy manifest fallback in the
    versioned reader.  In particular, an absent or foreign state file cannot turn a
    half-published package on merely because package-layer ``tool.json`` is absent.
    """

    return state.enabled if state.ours else False


def package_enabled(package_root: PackageRoot) -> bool:
    """Read the package-layer toggle, failing closed for every unusable shape."""
    if package_root.path.is_symlink():
        # O_NOFOLLOW protects only the final state-file component; refusing the
        # package symlink first prevents parent traversal and agrees with scanning.
        return False
    return _effective_enabled(_read_enabled_state(package_root))


def _origin_document(version_root: VersionRoot) -> dict[str, Any] | None:
    """Read and validate the committed-version marker without guessing.

    ``previous`` is deliberately not shape-validated here.  The marker still
    commits this version when an operator hand-edits only that field into a bad
    value; lineage then becomes ``broken`` and discard can answer the actionable
    ``lineage_unavailable`` conflict.  Treating that edit as an uncommitted
    current version would erase ``current_vid`` and collapse two different
    repair paths into the unresolved-row state.
    """

    data = _read_regular_bytes_capped(
        version_root.path / _META_DIRNAME / _ORIGIN_FILENAME, _AI_META_MAX_BYTES
    )
    if data is None or len(data) > _AI_META_MAX_BYTES:
        return None
    try:
        raw = json.loads(data.decode("utf-8"))
    except UnicodeError, ValueError, RecursionError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("source"), str):
        return None
    for key in ("openapi_url", "instructions", "feedback"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            return None
    return _utf8_safe_meta(raw)


def _resolve_version_target(
    package_root: PackageLayoutRoot, vid: object
) -> tuple[VersionRoot, dict[str, Any]] | None:
    """Resolve one committed version target by the exact rule ``current`` uses.

    The vid syntax, real-directory check, and committed ``origin.json`` check
    live here once so ``current``, lineage, and discard cannot drift into three
    subtly different definitions of a usable version.
    """

    if not isinstance(vid, str) or not _VID_RE.fullmatch(vid):
        return None
    version_path = package_root.path / _VERSIONS_DIRNAME / vid
    try:
        version_info = os.lstat(version_path)
    except OSError:
        return None
    if not stat.S_ISDIR(version_info.st_mode):
        return None
    version_root = VersionRoot(version_path)
    origin = _origin_document(version_root)
    if origin is None:
        return None
    return version_root, origin


def version_exists(package_root: PackageRoot, vid: str) -> bool:
    """Whether ``vid`` is still one of the package's committed versions.

    A run record outlives a discarded version (web-v6 §6.4); this is how the
    page learns the version it ran is gone. The rule is the one ``current`` and
    discard accept a version by, so the three cannot disagree.
    """
    return _resolve_version_target(package_root, vid) is not None


def _resolve_current_data(
    package_root: PackageLayoutRoot,
) -> tuple[VersionRoot, str, Previous] | str:
    """Resolve one package-shaped pointer without deciding that it is installed."""

    package = package_root.path
    try:
        package_info = os.lstat(package)
    except OSError:
        return "package directory is not readable"
    if not stat.S_ISDIR(package_info.st_mode):
        return "package directory must be a real directory"

    data = _read_regular_bytes_capped(
        package / _META_DIRNAME / _CURRENT_FILENAME, _CURRENT_MAX_BYTES
    )
    if data is None:
        try:
            os.lstat(package / _META_DIRNAME)
        except FileNotFoundError:
            # Name the migration only for the SAME namespace shape its preflight
            # sends to the legacy inspector. In particular, a target package
            # whose metadata directory was moved aside still owns ``versions/``;
            # migration refuses that ambiguous shape, so the runtime must not
            # prescribe a command guaranteed to refuse it.
            try:
                with os.scandir(package) as entries:
                    if next(entries, None) is not None:
                        if legacy_preflight_accepts_shape(package):
                            return (
                                "unmigrated tool layout; "
                                "run `python -m afterthread.migrate_tools_v5`"
                            )
                        return "tool layout is damaged or ambiguous; inspect it before migration"
            except OSError:
                pass
        except OSError:
            pass
        return "current is missing or unreadable"
    if len(data) > _CURRENT_MAX_BYTES:
        return "current is too large"
    if data.endswith(b"\n"):
        data = data[:-1]
    try:
        vid = data.decode("ascii")
    except UnicodeError:
        return "current has invalid syntax"
    if not _VID_RE.fullmatch(vid):
        return "current has invalid syntax"

    target = _resolve_version_target(package_root, vid)
    if target is None:
        version_path = package / _VERSIONS_DIRNAME / vid
        try:
            version_info = os.lstat(version_path)
        except OSError:
            return "current points to a missing version"
        if not stat.S_ISDIR(version_info.st_mode):
            return "current version must be a real directory"
        return "current points to an uncommitted version"
    version_root, origin = target
    if "previous" not in origin:
        previous: Previous = PREVIOUS_ABSENT
    elif origin["previous"] is None:
        previous = PREVIOUS_NULL
    else:
        previous = PreviousValue(origin["previous"])
    return version_root, vid, previous


def resolve_current(package_root: PackageRoot) -> Resolution:
    """Resolve exactly one installed package's bounded ``current`` pointer."""

    result = _resolve_current_data(package_root)
    if isinstance(result, str):
        return Unresolved(package_root, result)
    version_root, vid, previous = result
    return Resolved(package_root, version_root, vid, previous)


def resolve_layout_current(package_root: PackageLayoutRoot) -> PackageLayoutResolution:
    """Resolve ``current`` without claiming a staging or retired layout is installed."""

    result = _resolve_current_data(package_root)
    if isinstance(result, str):
        return PackageLayoutUnresolved(package_root, result)
    version_root, vid, previous = result
    return PackageLayoutResolved(package_root, version_root, vid, previous)


def _manifest_identity(directory: Path) -> tuple[int, int, int] | None:
    """The manifest identity shared by build validation and installed scans."""

    try:
        info = os.lstat(directory / "tool.json")
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_ctime_ns)


# Why a manifest holding such a string is invalid (``_holds_unencodable_text``).
_MANIFEST_UNENCODABLE = (
    "tool.json has a string that cannot be encoded as UTF-8 (an unpaired \\uD800-\\uDFFF escape)"
)


def _holds_unencodable_text(value: object) -> bool:
    """Whether a parsed JSON value holds a string -- a key or a value, at any depth
    -- that cannot be encoded as UTF-8.

    ``json.loads`` turns an unpaired ``\\uD800``-``\\uDFFF`` escape into a lone
    surrogate: a legal ``str`` that a strict UTF-8 encode refuses, so one of them in
    a manifest fails whatever it rides in -- the tools listing, the tool spec of
    every AI request that advertises the tool. A text read from a file decodes
    with replacement and holds none; only such an escape brings one. The value is
    walked with a list of what is left to look at, so its depth costs no
    recursion.
    """
    pending: list[object] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _scan_tool_content(root: BuildRoot | VersionRoot, expected_name: str) -> _ContentScan:
    """One implementation of manifest, entry, containment, and content rules.

    The scan of an installed version (``scan_installed``) and the check of a build
    before it is published (``validate_tool_content``) are both this function, so
    a manifest one refuses the other refuses too: among others, one holding a
    string that cannot be encoded as UTF-8 (``_holds_unencodable_text``), whose
    description is then not taken.
    """

    directory = root.path

    def invalid(error: str) -> _ContentScan:
        return _ContentScan(
            name=expected_name,
            directory=directory,
            valid=False,
            description="",
            error=error,
            parameters=None,
            entry=None,
            identity=None,
        )

    try:
        info = os.lstat(directory)
    except OSError:
        return invalid("tool content directory is not readable")
    if not stat.S_ISDIR(info.st_mode):
        return invalid("tool content directory must be a real directory")

    tool_json = directory / "tool.json"
    try:
        manifest_info = os.lstat(tool_json)
    except OSError:
        return invalid("missing tool.json")
    if not stat.S_ISREG(manifest_info.st_mode):
        return invalid("tool.json must be a readable real file")
    identity = _manifest_identity(directory)
    text = _read_regular_file_capped(tool_json, _MANIFEST_MAX_BYTES)
    if text is None:
        return invalid("tool.json is not a readable regular file")
    if len(text) > _MANIFEST_MAX_BYTES:
        return invalid("tool.json is too large")
    try:
        raw = json.loads(text)
    except ValueError, RecursionError:
        return invalid("tool.json is not valid JSON")
    if not isinstance(raw, dict):
        return invalid("tool.json is not a JSON object")
    if _holds_unencodable_text(raw):
        return invalid(_MANIFEST_UNENCODABLE)

    name_field = raw.get("name")
    if not isinstance(name_field, str) or not _NAME_RE.fullmatch(name_field):
        return invalid("name is missing or not a valid tool name")
    if name_field != expected_name:
        return invalid("name does not match the package name")
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        return invalid("description is missing or empty")
    parameters = raw.get("parameters")
    if not isinstance(parameters, dict):
        return invalid("parameters is not a JSON Schema object")
    if len(json.dumps(parameters)) > _PARAMETERS_SCHEMA_MAX_BYTES:
        return invalid("parameters schema is too large")
    entry = raw.get("entry")
    if not _valid_entry(entry):
        return invalid("entry is not a non-empty list of command strings")
    if not _entry_file_exists(directory, entry):
        return invalid("entry does not reference a file inside the tool package")

    return _ContentScan(
        name=expected_name,
        directory=directory,
        valid=True,
        description=description.strip()[:_DESCRIPTION_CAP],
        error=None,
        parameters=parameters,
        entry=list(entry),
        identity=identity,
    )


def scan_installed(package_root: PackageRoot) -> _PackageScan:
    """Resolve one installed package once, then scan only that resolved version."""

    name = package_root.path.name
    resolution, advertisement_generation = _resolve_and_register_advertisement(package_root)
    if isinstance(resolution, Unresolved):
        return _PackageScan(
            name=name,
            package_root=package_root,
            resolution=resolution,
            valid=False,
            enabled=False,
            description=None,
            error=resolution.reason,
            parameters=None,
            entry=None,
            identity=None,
        )

    state = _read_enabled_state(package_root)
    enabled = _effective_enabled(state)
    if state.error is not None:
        return _PackageScan(
            name=name,
            package_root=package_root,
            resolution=resolution,
            valid=False,
            enabled=False,
            description="",
            error=state.error,
            parameters=None,
            entry=None,
            identity=None,
            advertisement_generation=advertisement_generation,
            notice=state.notice,
        )
    content = _scan_tool_content(resolution.version_root, name)
    return _PackageScan(
        name=name,
        package_root=package_root,
        resolution=resolution,
        valid=content.valid,
        enabled=enabled,
        description=content.description,
        error=content.error,
        parameters=content.parameters,
        entry=content.entry,
        identity=content.identity,
        advertisement_generation=advertisement_generation,
        notice=state.notice,
    )


def _scan_all() -> list[_PackageScan]:
    """Scan every immediate subdirectory of ``tools_dir`` in name order.

    A missing/unset dir yields [] (feature off, or nothing installed yet).
    Sorted so ``list_tools`` and ``enabled_llm_tools`` are deterministic. Stray
    non-directory entries are skipped, and so are HIDDEN directories (a leading
    "."): a dot can never begin a valid package name (see ``_NAME_RE``), and the
    installer parks its in-progress builds under ``<tools_dir>/.staging/`` --
    without this skip, every install in flight would surface in the tools list
    as a phantom broken package named ".staging".
    """
    base = tools_dir()
    if base is None or not base.is_dir():
        return []
    return [
        scan_installed(PackageRoot(child))
        for child in sorted(base.iterdir())
        if child.is_dir() and not child.name.startswith(".")
    ]


# --- embedded-secret scan ------------------------------------------------------

# How many characters of a file one read of the embedded-secret scan takes. Each
# read is searched together with the end of the read before it, so this bounds
# the memory a file costs the scan, not what the scan can find in it.
_SECRET_SCAN_CHUNK_CHARS = 64 * 1024

# Where in a tree a known secret was found: a regular file's content, an entry's
# relative path (its own name, or a directory above it), or a symlink's target
# string. Hits on one path are listed in this order.
type SecretHitKind = Literal["content", "name", "link_target"]
_SECRET_HIT_KIND_ORDER: dict[str, int] = {"content": 0, "name": 1, "link_target": 2}

# Why the scan did not search an entry's content (or a symlink's target):
# ``not_regular`` -- neither a directory, a link nor a regular file, such as a FIFO,
# socket or device, which is never read -- or ``unreadable``, when an ``OSError``
# stopped the open or the read.
type SecretScanSkipReason = Literal["not_regular", "unreadable"]


@dataclass(frozen=True, slots=True)
class SecretHit:
    """One place a known secret value was found, by path relative to the root."""

    path: str
    kind: SecretHitKind


@dataclass(frozen=True, slots=True)
class SecretScanSkip:
    """An entry whose content, or symlink target, the scan did not search."""

    path: str
    reason: SecretScanSkipReason


@dataclass(frozen=True, slots=True)
class SecretScanDone:
    """A scan that went through the whole tree: every hit, and every skipped entry.

    No hits means no searched place holds a known secret. ``skipped`` is what was
    not searched; whether that is acceptable is the caller's call.
    """

    hits: tuple[SecretHit, ...]
    skipped: tuple[SecretScanSkip, ...]


@dataclass(frozen=True, slots=True)
class SecretScanFailed:
    """A scan that could not finish, so nothing is known about the tree.

    It deliberately has no ``hits``: a caller must tell it apart from
    ``SecretScanDone`` before it can read any, so a failed scan cannot pass for a
    clean one. ``reason`` is the exception's class name only, because an error's
    text can carry a path and a path can embed a secret.
    """

    reason: str


type SecretScan = SecretScanDone | SecretScanFailed


def scan_embedded_secrets(
    root: Path | BuildRoot, secrets: Iterable[str] | None = None
) -> SecretScan:
    """Which paths in the tree under ``root`` embed a known secret value.

    A builder session has a real shell (run_shell), so it can read another tool's
    ``.env`` -- or any file the service uid can -- and bake a live key into what it
    produces: into a file, a file or directory name, or a symlink's target. This
    finds every such place. ``secrets`` defaults to the values known now
    (``_redaction_secrets``). Whichever list it is, it is looked for in its match
    forms (``_match_forms``): a value shorter than ``_MIN_SECRET_LEN``, the
    redactor's floor, is never looked for, and a value at the floor is looked for
    as given and with its line breaks normalized, whatever the length of that
    form.

    What it checks:

    * every entry's relative path, which carries its own name and every directory
      above it, so an entry under a directory named after a secret is a hit too;
    * every symlink's target string, read with ``os.readlink``; the link is never
      followed, opened or descended into;
    * the whole content of every regular file. Each ``_SECRET_SCAN_CHUNK_CHARS``
      read is searched together with the last "longest form looked for, minus
      one" characters before it, so a value split across two reads is still found
      whole, while memory stays at one read plus that overlap however large the
      file is. Files are opened like ``_read_regular_file_capped`` opens them
      (``O_NOFOLLOW``, ``O_NONBLOCK``, ``S_ISREG`` on the descriptor), without
      its cap. With no secret to look for, no file is opened.

    What it returns: ``SecretScanDone`` with every hit, sorted by path and then
    kind so callers and messages are stable, and every entry whose content went
    unsearched -- ``not_regular`` for one that is neither a directory, a link nor
    a regular file (a FIFO, socket or device, never read), ``unreadable`` when an
    ``OSError`` stopped the open or read of a file or the read of a link. A
    ``SecretScanFailed`` instead when the scan could not finish: the known-secret
    lookup raised, or the walk did -- a directory that cannot be listed, an entry
    that cannot be classified, a read failing with anything other than
    ``OSError``.

    Content is decoded as UTF-8 with replacement and universal newlines, as
    ``_read_regular_file_capped`` decodes it: a value holding a carriage return is
    found there through its normalized form, and content that differs from a value
    only in its line breaks counts as holding it.

    What it does not cover: a value stored in another encoding is not found; the
    root itself is listed as given, link or not; and the tree can change after the
    scan returns -- keeping writers away is the caller's job.
    """
    path = root.path if isinstance(root, BuildRoot) else root
    try:
        values = _redaction_secrets() if secrets is None else list(secrets)
        return _scan_tree(path, _match_forms(values))
    except Exception as exc:
        return SecretScanFailed(type(exc).__name__)


def _scan_tree(root: Path, secrets: list[str]) -> SecretScanDone:
    """The walk behind ``scan_embedded_secrets``. Raises whatever fails the scan.

    A directory that cannot be listed raises out of ``os.scandir``; an ``OSError``
    on one file or link is caught where it happens and recorded as a skip.
    """
    hits: list[SecretHit] = []
    skipped: list[SecretScanSkip] = []
    overlap = max((len(secret) for secret in secrets), default=1) - 1
    pending: list[tuple[str, str]] = [(os.fspath(root), "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as listing:
            entries = sorted(listing, key=lambda entry: entry.name)
        for entry in entries:
            relative = prefix + entry.name
            if any(secret in relative for secret in secrets):
                hits.append(SecretHit(relative, "name"))
            if entry.is_symlink():
                if not secrets:
                    continue
                try:
                    target = os.readlink(entry.path)
                except OSError:
                    skipped.append(SecretScanSkip(relative, "unreadable"))
                    continue
                if any(secret in target for secret in secrets):
                    hits.append(SecretHit(relative, "link_target"))
            elif entry.is_dir(follow_symlinks=False):
                pending.append((entry.path, relative + "/"))
            elif not entry.is_file(follow_symlinks=False):
                skipped.append(SecretScanSkip(relative, "not_regular"))
            elif secrets:
                found = _search_file(entry.path, secrets, overlap)
                if isinstance(found, bool):
                    if found:
                        hits.append(SecretHit(relative, "content"))
                else:
                    skipped.append(SecretScanSkip(relative, found))
    hits.sort(key=lambda hit: (hit.path, _SECRET_HIT_KIND_ORDER[hit.kind]))
    skipped.sort(key=lambda skip: skip.path)
    return SecretScanDone(tuple(hits), tuple(skipped))


def _search_file(path: str, secrets: list[str], overlap: int) -> bool | SecretScanSkipReason:
    """Whether the regular file at ``path`` holds one of ``secrets``, read to the end.

    Each read is searched with the last ``overlap`` characters of the previous one
    in front of it. A file that turns out not to be regular once open, or an
    ``OSError`` on the open or a read, comes back as the reason it was skipped;
    any other exception propagates and fails the scan.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return "unreadable"
    fd_owned = True  # until fdopen takes the descriptor over
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "not_regular"
        handle = os.fdopen(fd, encoding="utf-8", errors="replace")
        fd_owned = False
        with handle:
            carried = ""
            while chunk := handle.read(_SECRET_SCAN_CHUNK_CHARS):
                window = carried + chunk
                if any(secret in window for secret in secrets):
                    return True
                carried = window[max(len(window) - overlap, 0) :]
        return False
    except OSError:
        return "unreadable"
    finally:
        if fd_owned:
            os.close(fd)


def _secret_scan_failed_error(reason: str) -> str:
    """The publish refusal for a scan that could not finish; it never says "secret found"."""
    return f"秘密掃描失敗（{reason}），無法確認工具包裡沒有已知的秘密值。"  # noqa: RUF001


def validate_tool_content(build_root: BuildRoot, expected_name: str) -> str | None:
    """Validate not-yet-installed tool content; None means valid.

    ``expected_name`` supplies the package identity because a staging build has a
    session-generated directory name. Manifest, entry, containment, and content
    policy are shared with installed scanning without touching package metadata,
    ``current``, or the toggle.

    The .env size gate is deliberately NOT part of installed scanning: an
    oversized build ``.env`` must BLOCK a fresh install
    here, but a package whose ``.env`` is MUTATED oversized AFTER install should
    keep running under the runtime degrade (``_load_tool_dotenv`` -> {}), not
    vanish from the registry as invalid. So this gate lives on the installer's
    pre-move path only. Being stricter than the scan preserves the invariant above
    (passing here still implies passing the scan); only the reverse loosens.

    The embedded-secret gate (H3) is likewise install-only: it must BLOCK a package
    that baked a known key into any file, but is never re-run on installed packages
    (whose ``.env`` legitimately holds the injected secret post-install). It runs
    ``scan_embedded_secrets``, so it reads every file whole and checks every path
    and symlink target. A scan that could not finish refuses the package too, and
    so does an entry it could not read, and a known-secret list some ``.env``
    could not be read into (``KnownSecretsIncomplete``, whose message names it and
    what to check); each message says what went unchecked, never that a secret
    was found.
    """
    error = _scan_tool_content(build_root, expected_name).error
    if error is not None:
        return error
    # Install-only .env size gate (see _ENV_FILE_MAX_BYTES). Mirrors
    # _load_tool_dotenv's own is_file()-then-stat() shape; a stat failure is
    # treated as "not oversized" (the scan above already vetted the package, and
    # the runtime degrade remains the post-install defense).
    env_file = build_root.path / ".env"
    try:
        oversized = env_file.is_file() and env_file.stat().st_size > _ENV_FILE_MAX_BYTES
    except OSError:
        oversized = False
    if oversized:
        return "`.env` is too large"
    # Install-only embedded-secret gate: reject a package that embeds a known secret
    # value anywhere -- a file's content, a path, a symlink target -- naming the first
    # offending relative path, never the value. The path is masked before it lands in
    # the message, because a builder can write a file whose very NAME is an expanded
    # ``$SECRET``. One snapshot of the known secrets serves the scan and the mask, so
    # the value a hit was found by is the one masked out of its path. The path is
    # shown as ``_masked_as_shown`` shows a text: masked first, then its lone
    # surrogates replaced -- a name that is not UTF-8 is read back with them, and the
    # message must encode to be shown -- and then checked again, since the three
    # U+FFFD the scrub writes for one can complete a known value.
    try:
        secrets = _redaction_secrets()
    except KnownSecretsIncomplete as exc:
        # Which ``.env`` could not be read, and what to check: the operator's next step.
        return f"無法確認工具包裡沒有已知的秘密值。{exc}"
    except Exception as exc:
        return _secret_scan_failed_error(type(exc).__name__)
    scan = scan_embedded_secrets(build_root, secrets)
    if isinstance(scan, SecretScanFailed):
        return _secret_scan_failed_error(scan.reason)
    if scan.hits:
        offender = _masked_as_shown(scan.hits[0].path, secrets)
        return f"{offender} 不得包含秘密值（請改由環境變數讀取）"  # noqa: RUF001
    # An entry the scan could not read is content nobody checked: refused like a
    # failed scan, naming the first one -- whatever it holds would be published
    # unseen, and nothing after this reads it again. A FIFO, socket or device
    # (``not_regular``) is not refused: it has no stored content to hold a value,
    # and its name was checked like every other entry's. Its path is shown the same
    # way.
    unread = [skip.path for skip in scan.skipped if skip.reason == "unreadable"]
    if unread:
        unchecked = _masked_as_shown(unread[0], secrets)
        return f"{unchecked} 無法讀取，無法確認裡面沒有已知的秘密值。"  # noqa: RUF001
    return None


# --- AI summary sidecar (D40) ------------------------------------------------


# Text made encodable as UTF-8: the definition the API schemas use too
# (``afterthread.utf8.encodable``, also what ``BackendPath`` checks), so a path
# written here and a path a response states come out alike. Version metadata
# needs it because ``origin.json`` and ``summary.json`` are plain JSON the
# operator may hand-edit, and ``"\\ud800"`` in one loads as a lone surrogate:
# left alone it fails the UTF-8 encode of the next write (a 500 out of a PATCH
# that should have answered False -> 404) and of the summary response that serves
# it (a 500 on a GET that exists to degrade corrupt metadata).
_utf8_safe = utf8.encodable


def _utf8_safe_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Every string a sidecar read hands back, surrogate-scrubbed. Depth-bounded.

    Applied to the parsed sidecar on the way OUT of ``read_tool_meta``, because a
    hand-edited file bypasses our writer entirely: the write side can scrub what
    IT produces, but only this covers a surrogate an operator typed straight into
    the file. Without it a ``"\\ud800"`` in ``summary`` or ``updated_at`` reaches
    ``ToolSummaryDetail`` and 500s the GET at response-encode time (see
    ``_utf8_safe``).

    Bounded at DEPTH 2 -- top-level values plus one level inside a dict value --
    on purpose, not for lack of ambition. The published summary and origin
    documents are both flat, so this covers every field any consumer reads plus
    one defensive hand-edit level. A general recursive walk would re-open the hazard
    ``read_tool_meta``'s ``RecursionError`` guard exists to close: a hand-edited
    file that PARSES (json.loads recursing in C, on the C stack) can still be
    nested far deeper than a Python-frame recursion can follow, which would turn
    a corrupt sidecar back into a 500 for the whole 工具 page.

    KEYS are deliberately left alone. A key carrying a surrogate can never equal
    one of either document's schema names, so it is dropped by every consumer AND
    by the next writer (which builds files from its own literals) -- it can
    therefore never reach a response or a re-serialization.
    """
    scrubbed: dict[str, Any] = {}
    for key, value in meta.items():
        if isinstance(value, str):
            scrubbed[key] = _utf8_safe(value)
        elif isinstance(value, dict):
            scrubbed[key] = {
                sub_key: _utf8_safe(sub_value) if isinstance(sub_value, str) else sub_value
                for sub_key, sub_value in value.items()
            }
        else:
            scrubbed[key] = value
    return scrubbed


def read_origin_meta(version_root: VersionRoot) -> dict[str, Any] | None:
    """Return one committed version's typed, bounded origin document."""

    return _origin_document(version_root)


def read_tool_meta(version_root: VersionRoot) -> dict[str, Any] | None:
    """Parse a version's ``summary.json`` and combine it with immutable origin.

    Deliberately TOTAL: a missing sidecar, a FIFO/symlink swapped in for one, an
    oversized one, invalid JSON, PATHOLOGICALLY NESTED JSON, and a JSON value
    that is not an object ALL come back None -- the one "no usable summary
    metadata" answer every caller already has to handle, since a freshly
    installed tool legitimately has no sidecar until its summary generation
    finishes. A corrupt sidecar therefore degrades the summary panel to empty; it
    never breaks a tool listing or a mutation.

    That totality is load-bearing well beyond the summary panel, which is why
    ``RecursionError`` is caught next to ``ValueError`` rather than left to
    escape (the same pairing ``llm._parse_json_object`` documents, for the same
    reason): thousands of nested brackets exhaust the interpreter's recursion
    limit INSIDE ``json.loads``. A hand-edited or malicious sidecar must degrade
    its own summary panel rather than escaping as an exception.

    Reads through the ONE bounded, FIFO/symlink-hardened helper -- the cap+1 read
    plus the ``len > cap`` check is how an oversized sidecar is refused without
    ever slurping it whole. The cap is the sidecar's OWN
    ``_AI_META_MAX_BYTES``, and it is the same number ``write_tool_meta``
    refuses to exceed: ANYTHING THE WRITER ACCEPTS, THIS READS BACK. Sharing the
    manifest's 64 KiB cap while the writer checked nothing is exactly how a legal
    sidecar became permanently unreadable (see ``_AI_META_MAX_BYTES``).

    Every string that survives is surrogate-scrubbed on the way out
    (``_utf8_safe_meta``): a hand-edited ``"\\ud800"`` is JSON-legal but not
    UTF-8 encodable, and this is the boundary where a file our writer never
    touched becomes safe to serialize into a response.
    """
    text = _read_regular_file_capped(
        version_root.path / _META_DIRNAME / _SUMMARY_FILENAME, _AI_META_MAX_BYTES
    )
    if text is None or len(text) > _AI_META_MAX_BYTES:
        return None
    try:
        raw = json.loads(text)
    except ValueError, RecursionError:
        return None
    if not isinstance(raw, dict):
        return None
    summary = _utf8_safe_meta(raw)
    summary["origin"] = read_origin_meta(version_root)
    return summary


def _meta_str(value: Any) -> str | None:
    """One origin string field, narrowed: a ``str`` survives, anything else is None.

    Used only in ``origin.json``, whose nullable URL/instruction fields are
    meaningful: that immutable document is the install provenance copy, and "we
    do not have one" must be representable. ``summary`` deliberately does NOT go
    through here -- see ``write_tool_meta`` for why a non-string summary refuses
    the write instead of degrading to None.
    """
    return value if isinstance(value, str) else None


def _written_literal(text: str) -> str:
    """``text`` as a JSON file this backend writes holds it, without its quotes.

    The string literal ``json.dumps(..., ensure_ascii=False)`` makes, which is how
    every such file is encoded; an ``indent`` changes only the whitespace between
    tokens, never a literal, so this one form serves the indented manifest and the
    flat sidecars alike.
    """
    return json.dumps(text, ensure_ascii=False)[1:-1]


def _holds_a_secret(text: str, secrets: list[str]) -> bool:
    """Whether ``_mask_known_secrets`` would change ``text``: the mask's own matcher.

    So the same three things count as a hit: a whole known value, a trailing
    prefix of one at least ``_MIN_SECRET_LEN`` long, and more matches than the
    mask collects.
    """
    return _mask_known_secrets(text, secrets) != text


def _spells_a_secret(text: str, secrets: list[str]) -> bool:
    """Whether ``text``, written as a JSON string literal, holds a known secret."""
    return _holds_a_secret(_written_literal(text), secrets)


def _redacted(value: str | None, secrets: list[str], cap: int | None = None) -> str | None:
    """One free-text VALUE of a JSON file, as it is written; None stays None.

    The field check of every JSON file this module and ``tool_drafts`` write with
    text in them (``summary.json``, ``origin.json``, a draft's manifest): masked
    with ``secrets``, cut to ``cap`` when there is one, surrogate-scrubbed, and
    then checked with the mask's own matcher in both forms the value takes. A
    reader decodes the file and has the text itself -- the tool summary API hands
    it on as it is -- so the text is checked (``_holds_a_secret``); the file holds
    it encoded, so that form is checked too (``_spells_a_secret``). The mask
    worked on the text before the scrub and the encoding, and each form can hold
    what the masked text did not:

    * the text: the scrub leaves three U+FFFD for one lone surrogate, and with the
      characters beside them they can complete a known value that holds them;
    * the file: the encoder writes a line break as ``\\n``, a quote as ``\\"``, a
      backslash as ``\\\\``, a control character as ``\\uXXXX``. A PEM registered
      from a single-quoted ``.env`` line keeps its ``\\n`` as those two
      characters, so a text holding that key with real line breaks is written as
      exactly the secret's bytes.

    Neither form answers for the other: a known value of three U+FFFD and a quote
    can be whole in the text and absent from the file, which writes the quote as
    ``\\"``, and an escape is only in the file. A value that holds a secret in
    either form is written as the redaction marker, whole: masking it in place
    would mean cutting into escapes.

    What it does not cover: a difference of representation an exact match cannot
    see -- a secret registered with a real line break that a text spells as the
    two characters ``\\n``, a value encoded another way (base64, say), a ``.env``
    value whose undecodable bytes were replaced when it was read.

    Masking comes before the scrub: the mask must match the REGISTERED value
    against untouched text. The scrub is what keeps the write path's own encode
    from raising: an LLM reply or a hand-edited field can carry a lone surrogate,
    which is a legal ``str`` that ``.encode("utf-8")`` refuses. Scrubbing rather
    than REFUSING is the adjudicated choice -- one bad code point is a
    display-level defect in a summary that is otherwise a genuine, useful
    explanation, and refusing would lose the whole generation over it (and, on
    the install hook's placeholder path, leave the operator with no sidecar and no
    重新產生 button at all). U+FFFD is exactly what the reader would show anyway,
    so scrubbing makes the file agree with the render. The read side scrubs
    INDEPENDENTLY regardless (see ``_utf8_safe_meta``), because a hand-edited file
    never passes here.
    """
    if value is None:
        return None
    masked = _mask_known_secrets(value, secrets)
    text = _utf8_safe(masked if cap is None else masked[:cap])
    if _holds_a_secret(text, secrets) or _spells_a_secret(text, secrets):
        return _REDACTION_MARKER
    return text


def _masked_as_shown(text: str, secrets: list[str], cap: int | None = None) -> str:
    """``text`` as a message or a screen shows it: masked, cut to ``cap`` when there
    is one, made encodable, and checked after the last of these.

    The mask works on the text as given, before the scrub: a known value can hold
    a lone surrogate itself, and the scrub would hide the text's copy of it from
    the mask. The scrub writes three U+FFFD for one lone surrogate, and those can
    complete a known value the text did not hold, so what is shown is checked
    once more after it, with the mask's own matcher (``_holds_a_secret``): one
    that holds a known value is shown as the redaction marker, whole, as
    ``_redacted`` writes a field. What a caller puts beside the answer afterwards
    -- a template, a separator -- is outside this check.
    """
    masked = _mask_known_secrets(text, secrets)
    shown = _utf8_safe(masked if cap is None else masked[:cap])
    return _REDACTION_MARKER if _holds_a_secret(shown, secrets) else shown


@dataclass(frozen=True, slots=True)
class _Fixed:
    """A value written as fixed content, whole, for ``_encode_spans``.

    Everything inside is fixed: a string with its quotes, a number, a list or a
    mapping with its keys. It marks what the backend decides, never text it was
    handed.
    """

    value: Any


@dataclass(frozen=True, slots=True)
class _Record:
    """A mapping keyed by the backend's own literals, for ``_encode_spans``: its keys
    are fixed content, and each value is marked by what it is."""

    fields: dict[str, Any]


def _encode_spans(value: Any, indent: int | None) -> tuple[str, list[tuple[int, int]]]:
    """``value`` as ``json.dumps(value, ensure_ascii=False, indent=indent)`` writes it,
    and where in that text the free text is.

    ``value`` is JSON data with parts marked (``_Fixed``, ``_Record``). The text is
    the same, character for character, as ``json.dumps`` writes the data with the
    marks taken off: the same separators (``", "`` and ``": "``, or ``","`` and a
    new line indented by ``indent`` per level), ``[]`` and ``{}`` for an empty
    container, and each string and number as ``json.dumps`` writes it alone.

    Free text is what a check of the written text has to take as coming from
    outside: what is between the quotes of a string, escapes as written, and the
    digits of a number, unless it is inside a ``_Fixed``; and the keys of a
    mapping that is not a ``_Record``. Everything else is fixed content:
    punctuation and white space, the quotes of every string, ``null``, ``true``
    and ``false`` (they hold no character of anything given), the keys of a
    ``_Record``, and all of a ``_Fixed``. The free text comes back as
    ``(start, end)`` ranges of the text, in order and apart.

    A key that is not a string raises ``TypeError``, as a value of a type JSON has
    no form for does.
    """
    pieces: list[str] = []
    free: list[tuple[int, int]] = []
    size = 0

    def put(text: str, start: int = 0, end: int = 0) -> None:
        # ``text[start:end]`` is free text.
        nonlocal size
        if start < end:
            free.append((size + start, size + end))
        pieces.append(text)
        size += len(text)

    def layout(level: int) -> tuple[str, str, str]:
        # What opens, separates and closes the items of a container at ``level``.
        if indent is None:
            return "", ", ", ""
        inner = "\n" + " " * (indent * (level + 1))
        return inner, "," + inner, "\n" + " " * (indent * level)

    def walk(item: Any, level: int, fixed: bool) -> None:
        if isinstance(item, _Fixed):
            walk(item.value, level, True)
        elif isinstance(item, _Record):
            mapping(item.fields, level, fixed, True)
        elif isinstance(item, dict):
            mapping(item, level, fixed, fixed)
        elif isinstance(item, (list, tuple)):
            if not item:
                put("[]")
                return
            opening, separator, closing = layout(level)
            put("[" + opening)
            for index, element in enumerate(item):
                if index:
                    put(separator)
                walk(element, level + 1, fixed)
            put(closing + "]")
        elif item is None or item is True or item is False:
            put(json.dumps(item))
        elif isinstance(item, str):
            literal = json.dumps(item, ensure_ascii=False)
            put(literal, 1, 0 if fixed else len(literal) - 1)
        elif isinstance(item, (int, float)):
            literal = json.dumps(item)
            put(literal, 0, 0 if fixed else len(literal))
        else:
            raise TypeError(f"Object of type {type(item).__name__} is not JSON serializable")

    def mapping(fields: dict[Any, Any], level: int, fixed: bool, keys_fixed: bool) -> None:
        if not fields:
            put("{}")
            return
        opening, separator, closing = layout(level)
        put("{" + opening)
        for index, (key, item) in enumerate(fields.items()):
            if not isinstance(key, str):
                raise TypeError(f"keys must be str, not {type(key).__name__}")
            if index:
                put(separator)
            literal = json.dumps(key, ensure_ascii=False)
            put(literal, 1, 0 if keys_fixed else len(literal) - 1)
            put(": ")
            walk(item, level + 1, fixed)
        put(closing + "}")

    walk(value, 0, False)
    return "".join(pieces), free


def _touches_free_text(text: str, free: list[tuple[int, int]], secrets: list[str]) -> bool:
    """Whether a known value occurs in ``text`` where the occurrence takes in free text.

    ``free`` is where the free text is, as ``_encode_spans`` gives it. Every
    occurrence of every value counts, overlapping ones too; one that lies wholly
    outside ``free`` -- in fixed content -- passes, since the backend's own
    characters spell it. Only whole occurrences count: the text was not cut.

    After an occurrence that passes, the search goes on from the first place an
    occurrence could reach the next free range: those it skips lie in fixed
    content, and every occurrence that takes in free text is still seen. Nothing
    found is kept, and the first occurrence that takes in free text ends the
    search; per value, the work is one pass over the text and one search for each
    free range at most.
    """
    ends = [end for _, end in free]
    for value in secrets:
        if not value:
            # Found everywhere and never a secret: no match form is empty.
            continue
        size = len(value)
        at = text.find(value)
        while at != -1:
            index = bisect.bisect_right(ends, at)
            if index == len(ends):
                break
            start = free[index][0]
            if start < at + size:
                return True
            at = text.find(value, start - size + 1)
    return False


@dataclass(frozen=True, slots=True)
class _Writing:
    """How a builder hands over the values of one JSON file: as they are, or marked.

    A builder of such a file (``write_tool_meta``'s, ``write_origin_meta``'s,
    ``tool_drafts._document``) hands a value over through one of these methods by
    what it is, so the check of the written text (``_encode_checked``) learns what
    the file is made of from the same code that makes it:

    * ``fixed`` -- a value whose shape a check fixed (a vid, a timestamp, an id, a
      number, a word of a closed vocabulary): as it is. Nothing such a check
      passes can carry text, and a mask could only corrupt it;
    * ``record`` -- a mapping keyed by the builder's own literals, its keys fixed;
    * ``text`` -- free text: through ``_redacted``, or the redaction marker when
      ``degraded``.

    ``marked`` hands the values to ``_encode_spans``: what ``fixed`` and ``record``
    give is marked (``_Fixed``, ``_Record``), and so is the marker ``text`` gives
    when ``degraded``, which is fixed content. A text otherwise is free text, and
    so is a value a builder writes without one of these methods -- as
    ``write_tool_meta`` writes ``llm_log_process`` --, which no marker ever
    replaces: nothing vouched for it, and the marker cannot either. Unmarked,
    every value is plain data, which is how ``tool_drafts.read_manifest`` reads a
    manifest back.
    """

    secrets: list[str]
    degraded: bool = False
    marked: bool = False

    def fixed(self, value: Any) -> Any:
        return _Fixed(value) if self.marked else value

    def record(self, fields: dict[str, Any]) -> Any:
        return _Record(fields) if self.marked else fields

    def text(self, value: str | None, cap: int | None = None) -> str | _Fixed | None:
        if value is None:
            return None
        if self.degraded:
            return _Fixed(_REDACTION_MARKER) if self.marked else _REDACTION_MARKER
        return _redacted(value, self.secrets, cap)


def _encode_checked(
    build: Callable[[_Writing], Any],
    secrets: list[str],
    *,
    indent: int | None = None,
    end: str = "",
) -> str | None:
    """The file ``build`` makes, as written; None when a known value in it takes in free text.

    The text is ``json.dumps(..., ensure_ascii=False, indent=indent)`` of what
    ``build`` hands over, followed by ``end``. ``_encode_spans`` makes it from the
    values ``build`` marks (``_Writing``), so the check looks at exactly the text
    that is written and knows which of it is fixed content: keys, punctuation and
    white space, ``end``, what a shape check fixed, ``null``, ``true`` and
    ``false``. Every occurrence of every known value counts, overlapping ones too
    (``_touches_free_text``). One wholly in fixed content passes -- an identifier
    is written as it is. One that takes in any free text sends every text to the
    redaction marker, which is fixed content, and the file is made and checked
    again; one that still takes in free text takes in a value no check vouched
    for, and nothing is written.

    The field check (``_redacted``) runs first, inside ``build``. This is the
    floor behind it, and sees what reached the file some other way, across a key,
    a quote or the boundary between two fields as well as inside one value.
    """
    for degraded in (False, True):
        writing = _Writing(secrets, degraded=degraded, marked=True)
        text, free = _encode_spans(build(writing), indent)
        text += end
        if not _touches_free_text(text, free, secrets):
            return text
    return None


def _still_the_expected_package(
    version_root: VersionRoot, expected: tuple[int, int, int] | None
) -> bool:
    """Is the package at ``directory`` still the one ``expected`` names?

    The ONE spelling of "nothing swapped under us", shared by the three places
    that act irreversibly on a package they looked at earlier: the runtime just
    before ``Popen`` (``_run_tool_subprocess``), the sidecar just before
    ``os.replace`` (``_write_package_file_atomic``), and ``_make_handler``'s refusal
    before it does any work at all. Each of those used to check somewhere
    EARLIER and then do real work -- a ``.env`` read, an argument serialization, a
    redactor sweep, an mkstemp+fsync -- between the check and the act it was
    guarding, which is a window rather than the check-then-act INSTANT this
    module accepts elsewhere. One helper does not make them one call site, but it
    does make "the check is the line above the act" the recognizable shape.

    ``expected`` of None is FALSE, never a pass: the caller could not establish an
    identity, and a check that cannot speak must not vouch (D40 P3b r11). The
    identity is the MANIFEST's (``package_identity``) because the question is
    "same advertised content?". The former directory identity and execution
    registry are gone: destruction safety is now the tools flock's job.
    """
    return expected is not None and package_identity(version_root) == expected


class _PackageReplaced(Exception):
    """Raised INSIDE ``_write_package_file_atomic`` when the guard refuses the publish.

    Never escapes that function, and exists only so the refusal leaves through
    the same ``except`` that already unlinks the temp file: the alternative is a
    second copy of that cleanup, kept in step by hand, on the one path that runs
    when something is already going wrong.
    """


def _write_package_file_atomic(
    directory: Path,
    filename: str,
    data: bytes,
    expected_identity: tuple[int, int, int] | None,
    *,
    default_mode: int = _OWNER_RW,
    identity_root: VersionRoot | None = None,
    durability_out: list[bool] | None = None,
) -> bool:
    """Atomically publish one backend-owned file and preserve its safe mode.

    The publisher refuses non-regular existing targets, writes and fsyncs a temp
    file in the destination directory, checks an optional version identity at the
    last instant, replaces the target, and fsyncs the directory. Its bool reports
    whether publication happened, for every caller. The optional one-item out
    parameter also reports whether that directory fsync succeeded -- whether the
    replaced name is confirmed durable -- for a caller that acts on it, such as
    ``publish_current`` (for discard) and ``tool_drafts._write_manifest`` (for a
    draft save, which keeps what it would otherwise remove when it is not).
    """
    path = directory / filename
    # The mode to publish under. There is ALWAYS one now (R11): a fresh file
    # gets _OWNER_RW explicitly rather than mkstemp's umask-masked default (or the
    # caller's ``default_mode``, normalized by the same rule), and an inherited
    # mode is OR'd with it below.
    preserve_mode: int = (default_mode & 0o777) | _OWNER_RW
    try:
        existing_mode = os.lstat(path).st_mode
    except FileNotFoundError:
        pass  # no file yet -- the ordinary first-write case, not a refusal
    except OSError:
        return False
    else:
        # lstat does NOT follow the final component, so a symlinked target shows
        # up as one here (S_ISLNK), exactly as O_NOFOLLOW used to refuse it.
        if not stat.S_ISREG(existing_mode):
            return False
        # R7-3: the SAME lstat that refused a symlink supplies the bits to keep.
        # Low 9 only -- setuid/setgid/sticky are not inherited (see docstring).
        # OR'd with _OWNER_RW (R11): the operator's group/other customization is
        # honored, but OWNER read+write is not negotiable -- see that constant.
        preserve_mode = (existing_mode & 0o777) | _OWNER_RW
    try:
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f"{filename}.", suffix=".tmp")
    except OSError:
        # A vanished/unwritable package directory (the ghost-guard race, EACCES,
        # a read-only mount): nothing was created, nothing to clean up.
        return False
    tmp_path = Path(tmp_name)
    fd_owned = True  # we own the raw fd until fdopen takes it over
    try:
        # On the FD, before publish: the replacement must already carry its final
        # mode at the instant the name flips, so no reader ever sees the temp
        # file's umask-dependent creation mode under the sidecar's name.
        os.fchmod(fd, preserve_mode)
        handle = os.fdopen(fd, "wb")
        fd_owned = False  # fdopen now owns fd; closing the handle closes it
        with handle:
            # BINARY, and the bytes were encoded ONCE by the caller: the size
            # check and the file must be measured on the identical payload, and a
            # text handle would re-encode (and could raise) at flush time instead.
            # BufferedWriter loops over short writes internally, so `write` +
            # `flush` is the complete-write guarantee cli.py's `_write_all` spells
            # out by hand over raw os.write.
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # The LAST instant: one lstat, then the publish. Nothing but the compare
        # separates them, so what a swap can still reach is the syscall PAIR this
        # module accepts by name elsewhere -- and even that lands harmlessly, since
        # the temp file was minted inside the directory that moved and the rename
        # below would then fail with ENOENT (measured; False, nothing published).
        # None is skipped rather than refused, because at THIS layer it means "the
        # caller asserted nothing" -- the callers that have an identity to assert
        # refuse their own None long before they get here.
        if expected_identity is not None and (
            identity_root is None
            or not _still_the_expected_package(identity_root, expected_identity)
        ):
            raise _PackageReplaced
        os.replace(tmp_path, path)
    except OSError, _PackageReplaced:
        if fd_owned:
            with contextlib.suppress(OSError):
                os.close(fd)
        # Safe unconditionally: tmp_path is a name mkstemp invented for THIS call
        # alone, never a path a caller passed in (cli.py's same argument). By PATH,
        # so it finds nothing when the package DIRECTORY was renamed aside under us
        # -- that ENOENT is the suppressed case, and what happens to the file
        # instead is measured in the docstring (it rides into the collected
        # namespace with the directory). Not worth a directory fd: the only way to
        # reach that branch is a rename that already hands the file to a collector.
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        return False
    # The rename made durable (see docstring). OUTSIDE the try on purpose: the
    # publish has already happened, so no failure here may turn it into a False --
    # it would unlink a temp file that no longer exists and report a toggle that
    # DID take effect as "did not happen". O_DIRECTORY so a path that is somehow
    # not a directory is refused rather than fsynced as whatever it is.
    durable = False
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        durable = True
    except OSError:
        pass
    if durability_out is not None:
        durability_out.append(durable)
    return True


def write_tool_meta(
    version_root: VersionRoot,
    meta: dict[str, Any],
    *,
    expected_identity: tuple[int, int, int] | None = None,
) -> bool:
    """Publish typed, bounded, sanitized ``summary.json``. Returns success.

    This does NOT serialize ``meta``. It reads the four fields it understands out
    of ``meta``, coerces each to the shape the sidecar's contract promises, and
    writes THAT -- so every KEY on disk is a literal from this function and every
    VALUE is one this function chose or refused:

    * ``summary`` -- a ``str``; None becomes ``""`` (the placeholder a failed
      generation leaves, and the shape a hand-edited ``"summary": null`` should
      collapse to rather than sneaking past as "not a string, but not refused
      either"). Any OTHER type refuses the write: ``summary`` is a string by
      contract, the read path renders anything else as "no summary", and writing
      one would make the file lie about whether a summary exists;
    * ``updated_at`` -- a timestamp in the shape ``datetime.now(UTC).isoformat()``
      gives (``_UTC_TIMESTAMP_RE``), else the write is REFUSED. Every caller stamps
      its own, so a missing or out-of-shape one is a caller bug, and inventing a
      timestamp on their behalf would put a fact in the file that nothing actually
      observed;
    * ``llm_log_id`` -- an ``int`` (``bool`` excluded, since it is an ``int``
      subclass and a stray ``true`` would render as a link to log record 1), else
      None;
    * ``llm_log_process`` -- a ``str``, else None: the ``llm_log.process_token()``
      of the process that MINTED the id above. The AI log's ids are a per-process
      counter and its ring dies with the process, while this file keeps the
      integer forever -- so after a restart a stored id resolves to whatever
      interaction now holds it, a different tool's summary or a different workflow
      altogether, and nothing downstream can tell (the row really was selected by
      that id). The token is what lets a reader ask "is this id still mine?"; see
      ``store_summary_meta`` for why it is minted next to the id rather than here,
      and ``routers.tools._summary_detail`` for what a foreign or absent one costs;

    Building rather than copying is the FIX for a real leak, not a tidiness
    preference. The previous version redacted the whole caller structure with a
    generic walk that masked dict KEYS as well as values, which turned the
    redactor's own success into corruption: register a secret whose VALUE
    happens to equal a schema key (``secret_value="summary"`` clears the 6-char
    floor), and the write "succeeded" with the fixed key rewritten to the
    redaction marker -- a file that no longer parses as our schema, so the
    summary silently disappeared from every later read. The same walk also had
    no case for a tuple, which JSON serializes as an array perfectly happily, so
    a tuple of strings rode to disk UNMASKED. Both are gone by construction
    here: there are no caller-supplied keys and no caller-supplied containers to
    walk.

    The redaction that remains is FAIL-CLOSED, and that is the load-bearing part
    of this helper. The summary is LLM output about a package whose ``.env`` holds
    live values, and this file is served back to the UI -- the same class
    ``redact_known_secrets`` closes everywhere raw model/tool text enters
    persisted state.

    It is also the ONLY thing standing between a secret and this file, which is
    why the fail-closed part is stated so loudly. An earlier version of this
    docstring named a second line of defence -- a revise copies the installed
    package into a staging build, and ``validate_package``'s embedded-secret gate
    scans EVERY file there -- and that has not been true since the revise flow
    took its final shape: ``_strip_builder_sidecars`` deletes any sidecar found in
    staging BEFORE validation runs, so no sidecar ever reaches that gate. The
    correction matters rather than being pedantic: a
    maintainer who believed a downstream gate would catch an unmasked value could
    reasonably relax the refusal below into "write it unmasked", and nothing
    downstream would catch anything.

    So a failure to read the known secrets (``known_secret_values`` raising -- the
    LIVE path deliberately propagates rather than degrading to unmasked, see
    ``redact_known_secrets``) writes NOTHING and returns False. The field check
    (``_redacted``) is applied to ``summary``, the one field that carries
    operator/LLM text; the check of the written text (``_encode_checked``) to the
    whole file, in which the keys, ``updated_at`` and ``llm_log_id`` -- shapes
    checked above -- are fixed content. ``llm_log_process`` is written as the
    caller gives it, so it is free text that no marker replaces: a known value
    that takes in any of it writes nothing.

    The serialized payload is bounded by ``_AI_META_MAX_BYTES``, the SAME cap
    ``read_tool_meta`` refuses past, so this can never produce a file that reads
    back as None. Every other failure (an unwritable path, a symlinked/FIFO
    sidecar refused by ``_write_package_file_atomic``'s lstat gate) is False too, so
    callers get one "did-not-happen" answer and never an exception -- summary
    metadata is best-effort by design.

    The serialize + encode + size check live INSIDE the fail-closed ``try``, and
    that placement is load-bearing rather than tidy: a lone surrogate reaching a
    string field would otherwise raise ``UnicodeEncodeError`` straight out of this
    function -- turning a PATCH that should have answered False (-> 404) into a
    500. ``_redacted`` scrubs the summary before ``dumps`` sees it and
    ``updated_at`` is refused unless it is a timestamp, so for those two the
    surrogate case is HANDLED rather than merely caught; the guard still covers
    ``llm_log_process``, the one caller-supplied string this function passes
    through verbatim.

    The write itself is ATOMIC (``_write_package_file_atomic``): the previous sidecar
    survives every failure and the file is never observable half-written. See
    that helper for why the truncate-then-write shape was a real data-loss
    vector for an existing summary.

    ``expected_identity`` is carried STRAIGHT THROUGH to that helper, which checks
    it on the line above ``os.replace``; this function does not read it, and the
    redaction/encode/cap work below deliberately runs BEFORE the check rather than
    after it (that ordering is the whole point -- see ``_write_package_file_atomic``).
    It defaults to "assert nothing" so a caller that just created the package
    itself need not invent one; every production caller passes the identity its
    own resolve captured, and both of them refuse a None identity in their own
    vocabulary before they get here.

    CONSEQUENCE, stated so it is not rediscovered as a bug: extra keys a
    hand-edited sidecar carries are DROPPED by the next write. Round-tripping
    them was never a contract -- it was a side effect of serializing the caller's
    dict, and it is precisely what let a stray key/value carry unmasked text into
    the file. The four fields above are the sidecar.

    ``llm_log_id`` and ``llm_log_process`` are carried through from ``meta``
    rather than re-derived. Stamping the current token here would forge freshness
    onto a stale id, so minting happens at exactly one call site, next to the id
    itself (``store_summary_meta``).

    The ``is_dir`` precondition is kept as the EXPLICIT answer to a racing
    ``delete_tool``: a summary landing just after one must not re-create the
    deleted package's directory holding nothing but a sidecar, which the registry
    would then list as a broken package named after the tool the user just
    removed. It was load-bearing when the sidecar rode ``_write_regular_file``,
    which CREATES missing parents (the meta-tool contract needs that);
    ``_write_package_file_atomic``'s ``mkstemp`` in the package directory now fails
    with ENOENT instead, so the check states the rule rather than being the only
    thing enforcing it. It narrows, but cannot close, that window (the delete can
    still land between this check and the write); the remaining race is the same
    single-user local-tool edge install/delete already accepts (D21/D40).
    """
    if not version_root.path.is_dir():
        return False
    summary = meta.get("summary")
    if summary is None:
        summary = ""
    if not isinstance(summary, str):
        return False
    updated_at = meta.get("updated_at")
    if not isinstance(updated_at, str) or not _UTC_TIMESTAMP_RE.fullmatch(updated_at):
        return False
    log_id = meta.get("llm_log_id")
    if not isinstance(log_id, int) or isinstance(log_id, bool):
        log_id = None
    log_process = meta.get("llm_log_process")
    if not isinstance(log_process, str):
        log_process = None

    def build(writing: _Writing) -> Any:
        return writing.record(
            {
                "summary": writing.text(summary),
                "updated_at": writing.fixed(updated_at),
                "llm_log_id": writing.fixed(log_id),
                # Written as the caller gives it: free text no marker replaces.
                "llm_log_process": log_process,
            }
        )

    try:
        # ensure_ascii=False keeps CJK readable in the file (and is what the byte
        # cap below is measured against). Every value is a str/int/None we just
        # built, so the JSON text cannot fail on an unserializable type -- but the
        # ENCODE can still raise on a surrogate-bearing ``llm_log_process``, which
        # is why both steps sit inside this guard (see the docstring). The cap is
        # checked on the ENCODED length, because that is the unit the writer's half
        # of the read/write symmetry is stated in (see _AI_META_MAX_BYTES).
        text = _encode_checked(build, _redaction_secrets())
        if text is None:
            return False
        data = text.encode("utf-8")
    except Exception:
        return False
    if len(data) > _AI_META_MAX_BYTES:
        return False
    meta_root = version_root.path / _META_DIRNAME
    if not meta_root.is_dir():
        return False
    return _write_package_file_atomic(
        meta_root,
        _SUMMARY_FILENAME,
        data,
        expected_identity,
        identity_root=version_root,
    )


def write_origin_meta(build_root: BuildRoot, origin: dict[str, Any]) -> bool:
    """Publish immutable, sanitized ``origin.json`` before a version is installed.

    Its four text fields go through the field check (``_redacted``) and the
    written file through the check of its text (``_encode_checked``), in which the
    keys and the vid of the version before it -- the one field whose shape a
    check fixes -- are fixed content. Every False writes nothing, among them a
    failure to read the known secrets.
    """

    source = origin.get("source")
    previous = origin.get("previous")
    if not isinstance(source, str):
        return False
    if previous is not None and (not isinstance(previous, str) or not _VID_RE.fullmatch(previous)):
        return False

    def build(writing: _Writing) -> Any:
        return writing.record(
            {
                "source": writing.text(source),
                "openapi_url": writing.text(_meta_str(origin.get("openapi_url"))),
                "instructions": writing.text(_meta_str(origin.get("instructions"))),
                "feedback": writing.text(_meta_str(origin.get("feedback"))),
                "previous": writing.fixed(previous),
            }
        )

    try:
        text = _encode_checked(build, _redaction_secrets(), end="\n")
        if text is None:
            return False
        data = text.encode("utf-8")
    except Exception:
        return False
    if len(data) > _AI_META_MAX_BYTES:
        return False
    meta_root = build_root.path / _META_DIRNAME
    if not meta_root.is_dir():
        return False
    return _write_package_file_atomic(meta_root, _ORIGIN_FILENAME, data, None)


@dataclass(frozen=True, slots=True)
class CurrentPublication:
    """The one publication result whose durability a caller is allowed to read.

    Truthiness means the ``current`` name was replaced, preserving the existing
    ``if not publish_current(...)`` call shape.  ``durable`` is intentionally a
    separate fact: discard succeeds on publication, but may destroy the previous
    target only when the containing-directory fsync was confirmed.
    """

    published: bool
    durable: bool

    def __bool__(self) -> bool:
        return self.published


def publish_current(package_root: PackageLayoutRoot, vid: str) -> CurrentPublication:
    """Publish one syntactically valid vid into a package-shaped layout."""

    if not _VID_RE.fullmatch(vid):
        return CurrentPublication(published=False, durable=False)
    meta_root = package_root.path / _META_DIRNAME
    if not meta_root.is_dir():
        return CurrentPublication(published=False, durable=False)
    durability: list[bool] = []
    published = _write_package_file_atomic(
        meta_root,
        _CURRENT_FILENAME,
        f"{vid}\n".encode("ascii"),
        None,
        durability_out=durability,
    )
    return CurrentPublication(
        published=published,
        durable=published and bool(durability) and durability[0],
    )


def write_package_state(
    package_root: PackageLayoutRoot, enabled: bool, *, default_mode: int = _OWNER_RW
) -> bool:
    """Atomically publish the owned toggle into a package-shaped layout."""
    # Trailing newline so the file is a well-formed text line like every other
    # small file this project publishes (cli.py's .env, set_enabled's old manifest
    # rewrite). ensure_ascii is irrelevant to a bool but is passed for uniformity
    # with the sidecar's encode. The payload is three ASCII tokens, so neither the
    # dumps nor the encode can raise and neither needs a guard. The marker is FIRST
    # so an operator opening the file reads whose it is before what it says.
    document = {_STATE_MARKER_KEY: _STATE_MARKER_VALUE, "enabled": enabled}
    data = (json.dumps(document, ensure_ascii=False) + "\n").encode("utf-8")
    meta_root = package_root.path / _META_DIRNAME
    if not meta_root.is_dir():
        return False
    return _write_package_file_atomic(
        meta_root,
        _PACKAGE_STATE_FILENAME,
        data,
        None,
        default_mode=default_mode,
    )


def store_summary_meta(
    version_root: VersionRoot,
    *,
    summary: str,
    llm_log_id: int | None,
    expected_identity: tuple[int, int, int] | None,
) -> tuple[str, dict[str, Any] | None]:
    """Publish a version summary without modifying immutable ``origin.json``.

    The version identity captured before generation is checked immediately before
    publication. The post-write read returns exactly what the next GET serves.
    """
    try:
        summary = redact_known_secrets(summary).strip()[:_TOOL_SUMMARY_CAP]
    except Exception:
        return ("not_stored", None)
    meta: dict[str, Any] = {
        "summary": summary,
        "updated_at": datetime.now(UTC).isoformat(),
        "llm_log_id": llm_log_id,
        "llm_log_process": llm_log.process_token() if llm_log_id is not None else None,
    }
    if expected_identity is None:
        return ("not_stored", None)
    if not write_tool_meta(version_root, meta, expected_identity=expected_identity):
        return ("not_stored", None)
    stored = read_tool_meta(version_root)
    return ("ok", stored) if stored is not None else ("not_stored", None)


def resolution_lineage(
    resolution: Resolved | PackageLayoutResolved,
) -> Literal["sole", "usable", "broken"]:
    """Return ``sole``/``usable``/``broken`` from this exact resolution.

    Missing is not null: every committed origin published by this service has
    the field, so omission means the document is not a complete marker of ours.
    A malformed present value and a self-loop are likewise broken.  Keeping all
    three decisions beside target validation prevents listing, migration and
    discard from inventing different lineage semantics.
    """

    previous = resolution.previous
    if isinstance(previous, PreviousNull):
        return "sole"
    if isinstance(previous, PreviousAbsent):
        return "broken"
    value = previous.value
    if value == resolution.vid:
        return "broken"
    return (
        "usable"
        if _resolve_version_target(resolution.package_root, value) is not None
        else "broken"
    )


def list_tools(
    *, draft_view: Callable[[PackageRoot, Resolution], dict[str, Any] | None] | None = None
) -> list[dict[str, Any]]:
    """List every installed package as a UI-facing summary.

    A broken package is included with ``valid=False`` and a safe error reason,
    but is never executable; a missing or unset tools directory yields [].

    With ``draft_view`` each row also carries ``draft``: what it answers for the
    package and the resolution this same scan made. The caller hands it in
    (``tool_drafts.draft_view``, bound to the job table), because reading a draft
    needs to know which jobs are running, and that is not this module's to know.
    ``tool_drafts.draft_view`` reads a manifest only where there is one.
    """
    rows = []
    for scan in _scan_all():
        row: dict[str, Any] = {
            # The directory's own name, made encodable: read on Linux, a name holds a
            # lone surrogate for each byte that does not decode as UTF-8, and one of
            # them fails the encode of the whole listing. Such a name is never a tool name
            # (``_NAME_RE``), so its row never scans valid -- the manifest's name must
            # equal it -- and a route addressed by it is refused before it runs (the
            # routes take ``_NAME_RE`` names only); the replacement characters cannot
            # make it one.
            "name": _utf8_safe(scan.name),
            "description": scan.description,
            "enabled": scan.enabled,
            "valid": scan.valid,
            "error": scan.error if scan.error is not None else scan.notice,
            "current_vid": scan.resolution.vid if isinstance(scan.resolution, Resolved) else None,
            "lineage": (
                resolution_lineage(scan.resolution)
                if isinstance(scan.resolution, Resolved)
                else "broken"
            ),
        }
        if draft_view is not None:
            row["draft"] = draft_view(scan.package_root, scan.resolution)
        rows.append(row)
    return rows


def package_identity(version_root: VersionRoot) -> tuple[int, int, int] | None:
    """Return one version manifest's ``(dev, ino, ctime_ns)`` identity.

    Scanning binds this identity to the advertised specification. Execution and
    summary publication compare it again so hand edits or replacement cannot make
    an operation composed for one manifest land on another.
    """
    try:
        info = os.lstat(version_root.path / "tool.json")
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_ctime_ns)


def _build_llm_tool(scan: _PackageScan) -> LlmTool:
    """Turn a valid ``_PackageScan`` into an executable ``LlmTool``.

    Callers pass only ``valid`` scans, so ``parameters`` / ``entry`` are present;
    the assertions document that precondition and keep the type checker happy
    without an ``Any`` escape hatch.

    The identity handed to ``_make_handler`` is the one ``_scan_tool_content`` took
    immediately before the manifest read (``scan.identity``), NOT a fresh ``lstat``
    taken here -- and that is the point rather than an optimization. This
    function does not run until ``_scan_all`` has returned, i.e. until EVERY
    package has been scanned, so an ``lstat`` here would be separated from the
    read it vouches for by an unbounded number of file reads: an operator package
    replacement landing in that gap pinned the NEW identity against the OLD spec, and
    every downstream guard -- this handler's own, and the one above ``Popen`` --
    then compared EQUAL and ran it. Pairing them at the read leaves a window of
    exactly one ``lstat``/``open`` pair, and leaves it on the side that REFUSES
    (see ``_scan_tool_content`` for the measurement of both orderings).

    A TOGGLE landing in that same gap was the OTHER half of R7-1's finding, and it
    is no longer this pairing's to catch: since web-v5 P1 a toggle moves nothing,
    so the stale ``scan.enabled`` this function reads can advertise a tool that was
    switched off a moment ago. That is answered where it now belongs -- the handler
    re-derives the effective toggle at CALL time (``package_enabled``) and refuses
    (``_make_handler``).

    A ``scan.identity`` of None still reaches ``_make_handler``, which refuses
    every call rather than treating "cannot say" as "unchanged" (D40 P3b r11).
    """
    assert scan.parameters is not None
    assert scan.entry is not None
    assert isinstance(scan.resolution, Resolved)
    assert scan.advertisement_generation is not None
    spec: dict[str, Any] = {
        "type": "function",
        "function": {
            # scan.description is already stripped and capped at scan time.
            "name": scan.name,
            "description": scan.description,
            "parameters": scan.parameters,
        },
    }
    return LlmTool(
        spec=spec,
        handler=_make_handler(
            scan.resolution,
            scan.entry,
            scan.identity,
            scan.advertisement_generation,
        ),
    )


def enabled_llm_tools() -> list[LlmTool]:
    """Every valid AND enabled package as an ``LlmTool``, for ``generate_structured``.

    This is what the AI workflows advertise to the model. An empty result (no
    tools dir, nothing installed, or nothing valid+enabled) means the workflows
    pass no tools and their prompts stay byte-identical to the tool-less build.
    """
    return [_build_llm_tool(scan) for scan in _scan_all() if scan.valid and scan.enabled]


# --- known-secret registry (feeds llm_log's redactor, D36) ------------------
#
# llm_log redacts every KNOWN secret VALUE out of the prompt/response bodies it
# stores, at its storage choke point. But llm_log is a leaf observability module
# and must not import THIS one -- so it takes a provider CALLABLE and main wires
# in ``known_secrets_for_log`` below (the same leaf/provider inversion llm_log's
# own logger config uses). The inversion is about THAT direction only: this
# module imports llm_log directly (for ``process_token``, see
# ``store_summary_meta``), which is the direction that keeps llm_log a leaf.
# The set is computed at CALL time, never cached as a
# static value, because it genuinely CHANGES at runtime: an install writes a new
# tool ``.env``, and an in-flight install registers its form secret before that
# ``.env`` even exists.
#
# Every collection says whether it is whole (``_collect_known_secrets``): a
# ``.env`` that could not be read contributes no values, and a list missing them
# must not be taken for the whole list. The masks refuse to work from one
# (``known_secret_values`` raises ``KnownSecretsIncomplete``); llm_log stores, in
# place of a body checked against one, a marker saying the body was not recorded
# (``llm_log._UNCHECKED_MARKER``).

# In-flight secrets: values that must stay redactable for the DURATION of one
# operation, independently of what the ``.env`` scan below can see AT THE MOMENT
# a body is recorded. Three kinds of holder register here:
#
# * a web-installer form secret (``secret_value``), for the whole install:
#   ``tool_builder.run_install`` registers it before the build and releases it
#   when the install returns or ends in an ``Exception``. It covers the window
#   BEFORE promote writes the value into the package ``.env``, which the
#   per-package ``.env`` scan below covers from then on. An install cancelled --
#   the event loop shutting down -- or ended by an interrupt or an exit keeps it
#   until the process ends: a promote's worker it started goes on without it, and
#   still scans the build and writes ``origin.json`` with the known secrets before
#   the value is in an installed package's ``.env`` -- and a promote that fails
#   never installs one;
# * a revise round's ``.env`` values, held by ``tool_builder.run_revise`` from
#   before its builder session to its end, released and kept the same way: a
#   worker the round handed a save, a record or a publish to may still take the
#   known secrets after it, and the package's ``.env`` may no longer hold a value
#   the round was given;
# * the ``.env`` values ONE TOOL CALL was handed. ``_build_tool_env`` copies the
#   package's ``.env`` into the child's environment, and the child can echo any
#   of it back -- but the redaction of that output happens when the child
#   FINISHES, and by then the file may say something else entirely (an operator
#   rotating a credential or directly editing/moving package state). The scan
#   would then only know the NEW value and the OLD one would ride into the
#   role:"tool" message, the log and the JSONL sink unmasked. So the values a
#   child actually RECEIVED are held here for
#   exactly as long as that call can still produce output to mask.
#
# COUNTED rather than flagged because overlapping holders can carry the same
# value:
# holders overlap (two workflows calling the same tool, a revise running while an
# ordinary conversation calls that same tool -- ordinary AI workflows are outside
# the tool job's single-flight), and they can hold the SAME value, since it is the
# same ``.env``. With a plain set the first holder to finish would strip the
# protection the survivor still needs.
#
# This registry lives HERE, not in tool_builder, on purpose: ``known_secret_values``
# must read it, and tool_builder already imports us, so putting it in tool_builder
# would force a reverse import (a cycle) -- the SAME inversion llm_log applies to
# us. Its own lock guards it because ``known_secret_values`` can run on a
# threadpool recorder thread while an install mutates the mapping on the event loop.
_INFLIGHT_SECRETS: dict[str, int] = {}
_INFLIGHT_LOCK = threading.Lock()

# Cache of one package's parsed ``.env`` VALUES, keyed by the ``.env`` path and
# tagged with a FILE IDENTITY, so ``_collect_known_secrets`` re-parses a package
# only when its ``.env`` actually changed. Only a read that gave an answer is
# stored, tagged with the ``fstat`` of the file it read; a look or a read that
# failed is never stored, and it drops the entry, so an entry can never stand in
# for a file that could not be looked at. This matters
# because the provider runs once PER STORED LOG BODY (every request message +
# every response of every attempt), and a busy installer session records the
# whole GROWING conversation on each of dozens of rounds -- without the cache,
# every one of those records would re-read every installed tool's ``.env`` from
# disk.
#
# The tag is ``(st_dev, st_ino, st_mtime_ns, st_ctime_ns, st_size)`` and it used
# to be the ``st_mtime_ns`` alone, which describes WHEN a path was last written
# rather than WHICH FILE is there now -- and mtime is the one timestamp userspace
# can set to anything (``utime``), while several perfectly ordinary ways of
# replacing a file preserve it on purpose. Measured on this repo's filesystem
# (ext4) rather than assumed, each against a cached entry:
#
# * ``shutil.copy2`` over the existing path changes ONLY ``st_ctime_ns``;
# * a rewrite followed by ``os.utime`` restoring the old stamps (a
#   timestamp-preserving restore, a backup rollout, ``cp -p``) changes ONLY
#   ``st_ctime_ns``, even when the new content is the same LENGTH;
# * write-to-temp + rename with the mtime carried over (``rsync -t``) changes
#   ``st_ino`` and ``st_ctime_ns``;
# * ``unlink`` + recreate REUSES the inode here, so ``st_ino`` alone would not
#   have caught the case above either -- which is why ctime is in the tag and not
#   just the inode;
# * renaming the parent DIRECTORY changes nothing about the file, so moving an
#   unchanged package tree away and back is still a cache HIT, correctly.
#
# Under the old tag every one of those left the redactor serving the PREVIOUS
# values: the tool then emits the new secret and neither the live tool-result
# redactor, nor llm_log, nor the summary writer masks it. ``st_ctime_ns`` is the
# field that carries the weight (no syscall sets it backwards; every content or
# metadata change moves it), with dev/ino catching a replacement that reuses the
# timestamps and size catching one that reuses the tick. The residual, stated
# rather than implied: the file-timestamp clock advances in ~1 ms steps here
# (measured), so a same-inode, same-size, mtime-preserving replacement landing
# INSIDE the millisecond of the cached read is still a hit. That is one syscall
# pair wide -- the same check-then-act instant this subsystem accepts everywhere
# else -- and it cannot be closed by any stat-based tag, only by re-reading every
# ``.env`` on every log body, which is the cost this cache exists to avoid.
_ENV_VALUE_CACHE: dict[str, tuple[tuple[int, int, int, int, int], frozenset[str]]] = {}
_ENV_VALUE_CACHE_LOCK = threading.Lock()


def register_inflight_secret(value: str) -> None:
    """Take ONE hold on ``value`` being redactable (see ``_INFLIGHT_SECRETS``).

    Every call must be matched by exactly one ``discard_inflight_secret`` from a
    ``finally``: the count is what lets overlapping holders of the same value
    (two calls to the same tool, a revise session and a tool call reading the
    same ``.env``) each release only their OWN hold. One exception: a builder job
    (``tool_builder.run_install``, ``tool_builder.run_revise``) that a
    ``BaseException`` other than an ``Exception`` ends -- the event loop's
    shutdown cancelling it, an interrupt, an exit -- keeps its holds until the
    process ends, since a worker it handed on goes on without it and may still
    take the known secrets.
    """
    if not value:
        return
    with _INFLIGHT_LOCK:
        _INFLIGHT_SECRETS[value] = _INFLIGHT_SECRETS.get(value, 0) + 1


def discard_inflight_secret(value: str) -> None:
    """Release ONE hold on ``value``; it stays redactable while others remain.

    The count is dropped to zero by REMOVING the key, so ``known_secret_values``
    can read the mapping as a plain set of values and a stale zero can never
    keep a value in it. Releasing a value nobody holds is a no-op (never a
    negative count), which keeps this as forgiving as the ``set.discard`` it
    replaced for a caller whose registration was skipped.
    """
    with _INFLIGHT_LOCK:
        remaining = _INFLIGHT_SECRETS.get(value, 0) - 1
        if remaining > 0:
            _INFLIGHT_SECRETS[value] = remaining
        else:
            _INFLIGHT_SECRETS.pop(value, None)


@contextlib.contextmanager
def _inflight_secrets(values: frozenset[str]) -> Iterator[None]:
    """Hold ``values`` redactable for the body, and release them again.

    Execution lifetime is now protected by the inherited flock, not a registry.
    This independent context manager remains because secret-redaction lifetime
    is a different question: a handler that raises, times out, or is cancelled
    mid-call must not leave a hold behind. A leaked one would keep masking a
    value forever, which is not visible until redaction starts eating ordinary
    prose.

    Registration is INSIDE the try, so a failure part-way through the loop still
    releases the holds already taken (the release of a value never registered is
    a no-op, so releasing them all is safe).
    """
    try:
        for value in values:
            register_inflight_secret(value)
        yield
    finally:
        for value in values:
            discard_inflight_secret(value)


@dataclass(frozen=True, slots=True)
class SecretSourceGap:
    """One place the known secrets could not be read from.

    ``tool`` is the package directory's name, None for the tools directory
    itself; ``path`` is the ``.env`` that could not be read -- for a package
    whose directory could not be looked at, the ``.env`` it would hold -- or the
    tools directory; ``error`` is the failure's type name, never its text.
    """

    tool: str | None
    path: str
    error: str


@dataclass(frozen=True, slots=True)
class KnownSecrets:
    """One collection of the known secret values, and whether it is whole.

    ``gaps`` names every source that could not be read; ``values`` then lacks
    whatever those hold. ``complete`` only when there are none: an empty set
    that is complete means there are no secrets, one that is not means nothing.
    """

    values: frozenset[str]
    gaps: tuple[SecretSourceGap, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.gaps


def _incomplete_message(gaps: tuple[SecretSourceGap, ...]) -> str:
    """What the operator is told when the known secrets cannot all be read.

    Names the first place, says how many more there are and what to check. It
    holds no value and no failure text, only the failure's type name, and it never
    says a secret was found: none was looked for.
    """
    first = gaps[0]
    where = _utf8_safe(first.path)
    if first.tool is None:
        text = (
            f"已知秘密的清單不完整：讀不到工具目錄 {where}（{first.error}），"  # noqa: RUF001
            "無法得知各工具 .env 裡的值。"
        )
        subject = "這個目錄"
    else:
        text = (
            f"已知秘密的清單不完整：讀不到工具「{_utf8_safe(first.tool)}」的 "  # noqa: RUF001
            f"{where}（{first.error}）。"  # noqa: RUF001
        )
        subject = "這個檔案"
    if len(gaps) > 1:
        text += f"另有 {len(gaps) - 1} 處也讀不到。"
    return text + f"請檢查{subject}與它上層目錄的權限、磁碟狀態後重試。"


class KnownSecretsIncomplete(OperatorFacingError):
    """The known secrets could not all be read, so no mask can be whole.

    Raised by ``known_secret_values`` -- and so by ``_redaction_secrets`` and
    ``redact_known_secrets`` -- instead of answering with the values it did read.
    ``gaps`` is every source that could not be read (``SecretSourceGap``); the
    message is ``_incomplete_message``'s.
    """

    code = "known_secrets_incomplete"

    def __init__(self, gaps: tuple[SecretSourceGap, ...]) -> None:
        self.gaps = gaps
        super().__init__(_incomplete_message(gaps))


def _forget_env_values(key: str) -> None:
    with _ENV_VALUE_CACHE_LOCK:
        _ENV_VALUE_CACHE.pop(key, None)


def _cached_env_values(package_root: PackageRoot) -> frozenset[str] | SecretSourceGap:
    """The redactable VALUES of one package's ``.env``, or where it could not be read.

    Read the way the runtime reads it (``_read_package_dotenv``, the loader
    ``_load_tool_dotenv`` uses): a link is not followed and a FIFO, a directory or
    any other file that is not regular is not read, so neither holds a value
    anyone is given. Each answer, by what is seen:

    * not there -- ``ENOENT`` or ``ENOTDIR`` from the ``lstat`` or the open: none;
    * a ``lstat``, or the ``fstat`` of what opened, that sees something not a
      regular file, a link above all: none. An ``ELOOP`` from the open counts as a
      link only when a ``lstat`` sees the ``.env`` itself is one; a loop above it
      is a failure;
    * text over the loader's cap, judged on the text as read: none;
    * text within it: exactly the non-empty values the parser gives, which may be
      some of the lines, or none of them;
    * any other failure to look at, open or read it: the ``SecretSourceGap``.

    The path is the cache KEY and the file's identity is the TAG (see
    ``_ENV_VALUE_CACHE`` for what is in it and for the measurements that chose
    the fields): a path answers "whose ``.env`` is this", and only the identity
    can answer "is it still the same file with the same contents". The entry is
    looked up by the ``lstat`` of the path, stored with the ``fstat`` of the file
    read, and only for a read that gave an answer; every other answer drops it.
    Empty values are dropped so a ``KEY=`` line contributes nothing.
    """
    env_file = package_root.path / ".env"
    key = str(env_file)
    try:
        info = os.lstat(env_file)
    except FileNotFoundError, NotADirectoryError:
        _forget_env_values(key)
        return frozenset()
    except OSError as exc:
        _forget_env_values(key)
        return SecretSourceGap(package_root.path.name, key, type(exc).__name__)
    if not stat.S_ISREG(info.st_mode):
        _forget_env_values(key)
        return frozenset()
    identity = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
    with _ENV_VALUE_CACHE_LOCK:
        cached = _ENV_VALUE_CACHE.get(key)
        if cached is not None and cached[0] == identity:
            return cached[1]
    # Read OUTSIDE the lock (it does file I/O); the (path, identity) key makes a
    # concurrent double-parse harmless -- both produce the identical set.
    read = _read_package_dotenv(env_file)
    if read.unread is not None:
        _forget_env_values(key)
        return SecretSourceGap(package_root.path.name, key, read.unread)
    values = frozenset(value for value in read.values.values() if value)
    if read.identity is None:
        # Gone, or no regular file, since the ``lstat``: nothing to keep.
        _forget_env_values(key)
        return values
    with _ENV_VALUE_CACHE_LOCK:
        _ENV_VALUE_CACHE[key] = (read.identity, values)
    return values


def _collect_package_values(base: Path, values: set[str], gaps: list[SecretSourceGap]) -> None:
    """Add every package's ``.env`` values under ``base`` to ``values``; record the gaps.

    The packages are what they have always been here: each entry not hidden that
    is a directory once links are followed. A listing or a look at an entry that
    fails other than with "not there" is a gap, never a package without values.
    """
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError, NotADirectoryError:
        return
    except OSError as exc:
        gaps.append(SecretSourceGap(None, os.fspath(base), type(exc).__name__))
        return
    for name in names:
        if name.startswith("."):
            continue
        child = base / name
        try:
            info = os.stat(child)
        except FileNotFoundError, NotADirectoryError:
            continue
        except OSError as exc:
            gaps.append(SecretSourceGap(name, os.fspath(child / ".env"), type(exc).__name__))
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        found = _cached_env_values(PackageRoot(child))
        if isinstance(found, SecretSourceGap):
            gaps.append(found)
        else:
            values |= found


def _collect_known_secrets() -> KnownSecrets:
    """Every known secret value, and whether that is all of them (``KnownSecrets``).

    The union of three sources:

    * our own ``openai_api_key`` when set -- belt-and-braces: the secret-free
      invariant already keeps it out of records STRUCTURALLY (llm.py never reads
      it into a body), but redacting it too means even a tool that somehow echoed
      it back cannot surface it in the log;
    * every VALUE in every installed tool package's ``.env`` (a KB API key etc.),
      cached per (path, file identity) so a call per record stays cheap
      (``_cached_env_values`` says what each ``.env`` answers);
    * every value currently HELD in-flight (see ``_INFLIGHT_SECRETS``) -- an
      install's form secret before promote writes it, a revise session's values,
      and the values a running tool call handed its child. Each covers a window
      the directory scan above cannot answer for, because the file it reads is
      whatever is on disk NOW rather than what the holder was given.

    A tools directory that is not configured, not there, or under a component
    that is a file holds no packages. One whose path cannot be resolved
    (``ToolsDirUnresolvedError``), or whose listing fails, is a gap, and so is
    every package or ``.env`` that cannot be looked at, opened or read: those
    failures are answered here, never raised. Private on purpose: a caller must
    not take ``values`` without ``complete`` -- ``known_secret_values`` and
    ``known_secrets_for_log`` are the ways in.
    """
    values: set[str] = set()
    gaps: list[SecretSourceGap] = []
    api_key = get_settings().openai_api_key.strip()
    if api_key:
        values.add(api_key)
    try:
        base = tools_dir()
    except ToolsDirUnresolvedError as exc:
        base = None
        gaps.append(SecretSourceGap(None, exc.path, exc.error))
    if base is not None:
        _collect_package_values(base, values, gaps)
    with _INFLIGHT_LOCK:
        values |= set(_INFLIGHT_SECRETS)
    return KnownSecrets(frozenset(values), tuple(gaps))


def known_secret_values() -> frozenset[str]:
    """Every known secret value, only when that is all of them.

    Raises ``KnownSecretsIncomplete`` when some source could not be read
    (``_collect_known_secrets``): a mask built from the rest would leave what that
    source holds in plain sight while looking whole.
    """
    collected = _collect_known_secrets()
    if not collected.complete:
        raise KnownSecretsIncomplete(collected.gaps)
    return collected.values


def known_secrets_for_log() -> tuple[frozenset[str], bool]:
    """llm_log's secret provider: ``(values, complete)``. Never raises.

    Wired by main. llm_log is an observer and must not fail the call it records,
    so a collection that is not whole, or that failed, is answered as
    ``(frozenset(), False)`` rather than raised; llm_log then stores, in place of
    each body, the marker saying it was not recorded
    (``llm_log._checked_as_stored_and_written``). Goes through
    ``known_secret_values``, so the same answer serves both. It hands over the
    values themselves: llm_log puts them in their match forms with its own copy of
    ``_match_forms``.
    """
    try:
        return known_secret_values(), True
    except Exception:
        return frozenset(), False


# The exact-match marker every redacted secret VALUE is replaced with, in BOTH the
# LIVE tool/meta-tool results (``redact_known_secrets`` below) and the STORED AI 日誌
# bodies (``llm_log._redact``). The two must render IDENTICALLY -- a secret masked in
# a live tool result and one masked in the log have to be indistinguishable -- so this
# is a byte-for-byte copy of ``llm_log._REDACTION_MARKER``.
#
# It is DUPLICATED here rather than imported, and NOT hoisted into a shared home that
# both modules import, on purpose: ``llm_log`` is a leaf observability module (it must
# not import this capability module), and making a shared constant live in THIS module
# for llm_log to import would invert that layering; a third tiny module for one string
# buys nothing over the marker-mirroring convention this codebase already follows
# (see ``_OUTPUT_TRUNCATION_MARKER``'s "Mirrors ..." note, and llm_log's own
# "Mirrors ... memory_ai's markers"). So each module owns an equal literal, and
# ``tests/test_tools.py`` pins the two equal so they can never silently drift.
_REDACTION_MARKER = "•••[秘密已遮蔽]•••"

# Values shorter than this are NEVER redacted -- the SAME floor ``llm_log._redact``
# uses: masking a 1-5 char value would shred ordinary prose (imagine redacting every
# "1234"), and a real API key/token is never that short. F3 makes the install-form
# secret schema reject a <6-char value up front precisely so a legitimately-supplied
# secret is always long enough to be redactable here. The floor is a value's own: the
# newline-normalized form of a value at it is matched whatever its length
# (``_match_forms``).
_MIN_SECRET_LEN = 6

# Hard ceiling on how many mask ranges a single call collects before giving up on
# precision and replacing the WHOLE text with the marker alone (D36 round-5). Without
# it, a legitimate secret at the _MIN_SECRET_LEN floor (e.g. "aaaaaa") matched against
# a large low-entropy document -- an un-truncated OpenAPI body that happens to repeat
# that pattern -- still collects one range per len(value)-char step even after the
# cursor fix below: a 2MiB same-char document divided into 6-char steps is still
# ~350K ranges, hundreds of MB once multiplied across every registered secret. Over-
# redaction is always SAFE (a text this saturated with secret matches has no
# legitimate readable content worth preserving), so collection STOPS the instant it
# would cross this cap and the function returns the marker alone for the entire text
# instead -- turning a pathological allocation into a fixed, O(1)-sized result.
# Mirrors llm_log._MAX_MASK_RANGES (same value, same rationale; not shared for the
# same leaf/layering reason as _REDACTION_MARKER -- a test pins the two equal).
_MAX_MASK_RANGES = 10_000


def _mask_known_secrets(text: str, secrets: list[str]) -> str:
    """Mask every known-secret RANGE in ``text`` with one marker each (D36/H1).

    The shared shape BOTH redactors run: ``tools.redact_known_secrets`` (the LIVE
    conversation path) and ``llm_log._redact`` (the STORED-body path) call an IDENTICAL
    copy of this. It is DUPLICATED in each module, not hoisted into a shared home, for
    the same leaf/layering reason the marker is (see ``_REDACTION_MARKER``); the two
    copies MUST stay byte-for-byte in LOCKSTEP so a value masked in a live tool result
    and one masked in the log are indistinguishable. ``secrets`` are the known values in
    their match forms (``_match_forms``: each value at ``_MIN_SECRET_LEN`` or above, and
    its newline-normalized form, which may be shorter) -- each caller produces that list
    under its OWN failure direction (tools lets a provider error propagate/fail-closed;
    llm_log swallows one and stores, in place of the body, the marker saying it was not
    recorded), then hands the masking here.

    Every mask range is computed against the PRISTINE ``text`` and only THEN applied,
    which is load-bearing (H1). The older order -- full-value ``str.replace`` FIRST, then
    inspect the MUTATED text for a trailing fragment -- let the full-value pass destroy
    the evidence the fragment guard needed: with ``ABCDEFGHIJKLmnop`` and ``GHIJKL`` both
    registered and a text ending ``ABCDEFGHIJKL`` (a 12-char prefix of the long secret),
    masking the short ``GHIJKL`` occurrence rewrote the tail so the guard no longer saw
    the long secret's prefix, leaking ``ABCDEF``. Computing all ranges up front on the
    original text and merging them removes that ordering hazard: iteration order (kept
    longest-first only for determinism) is NO LONGER load-bearing for correctness.

    Three steps: (1) collect ranges -- every occurrence of each full value, plus the
    single LONGEST trailing PREFIX fragment (>= _MIN_SECRET_LEN) the text ends with (a
    secret cut by an EARLIER truncation boundary leaves a prefix the full-value match can
    never catch; a false positive only masks a tail of already-truncated text, and
    INTERIOR fragments are prevented at their sources -- the summary/openapi slices in
    tool_builder and the tool-args preview in llm.py all redact BEFORE they cut) -- a
    form shorter than the floor only ever matches whole; (2) merge overlapping/adjacent
    ranges; (3) build the output in ONE pass, emitting exactly one marker per merged
    range.

    Collecting every occurrence in step (1) is BOUNDED two ways (D36 round-5), because a
    legitimate secret can itself be adversarial input to this search. First, the find
    cursor advances by ``len(value)`` on every hit rather than by 1, so overlapping
    occurrences of the SAME repeated secret text -- an ``"aaaaaa"``-style secret against
    a same-character document, the reported vector once an un-truncated OpenAPI body
    happens to look like that -- step past each other instead of registering one range
    per shifted start. This can still leave a residual of up to ``len(value) - 1``
    characters at a repeated run's tail (a strict remainder of the run length modulo
    ``len(value)``); for a value at the ``_MIN_SECRET_LEN`` floor -- exactly the reported
    case -- that residual is itself under the floor, and for a normalized form shorter
    than the floor it is shorter still, exactly as unredactable as any other short
    substring by the SAME rationale that floor already encodes, and a residual landing at
    the very END of ``text`` is still separately caught by the trailing-fragment guard
    above regardless of length. Second,
    stepping is not sufficient by itself: a sufficiently large low-entropy document can
    still cross ``_MAX_MASK_RANGES`` one ``len(value)``-sized step at a time, so
    collection STOPS the instant it would and this returns the marker ALONE for the
    entire text instead (see that constant) -- over-redaction is always safe, so the cap
    turns a pathological allocation into a fixed-size O(1) result. With ``ranges`` bounded
    before it is ever sorted, the merge in step (2) is O(n log n) on that bounded n
    rather than on the size of ``text``, and step (3)'s output build remains the single
    pass it always was.

    What it checks is the text it is given. Text a caller puts beside the answer
    afterwards -- a heading, a prefix, a separator, a truncation marker -- is outside
    it, and with the text next to it can spell a known value that holds that text; so
    can a marker this puts in, with the text beside it. A caller that needs its text
    checked as it is finally used checks it there.
    """
    if not secrets:
        return text
    secrets = sorted(secrets, key=len, reverse=True)
    # (1) ranges on the PRISTINE text -- every occurrence of every full value, cursor
    # STEPPING by len(value) on each hit (D36 round-5): overlapping occurrences of the
    # SAME secret collapse into non-overlapping, stepping matches rather than one range
    # per shifted start (see the docstring's residual/cap analysis).
    ranges: list[tuple[int, int]] = []
    for value in secrets:
        step = len(value)
        idx = text.find(value)
        while idx != -1:
            if len(ranges) >= _MAX_MASK_RANGES:
                # Over-redaction is always safe; a text this saturated with secret
                # matches has no legitimate readable content worth preserving.
                return _REDACTION_MARKER
            ranges.append((idx, idx + step))
            idx = text.find(value, idx + step)
    # ... plus the LONGEST trailing prefix fragment (>= _MIN_SECRET_LEN) the text ends
    # with. ``best`` only grows, so each secret is probed only for a fragment longer than
    # the best found so far (the k range floor is ``best``).
    best = 0
    for value in secrets:
        for k in range(min(len(value), len(text)), max(best, _MIN_SECRET_LEN - 1), -1):
            if text.endswith(value[:k]):
                best = k
                break
    if best >= _MIN_SECRET_LEN:
        if len(ranges) >= _MAX_MASK_RANGES:
            return _REDACTION_MARKER
        ranges.append((len(text) - best, len(text)))
    if not ranges:
        return text
    # (2) merge overlapping/adjacent ranges (``<=`` folds a touching range into the
    # previous one, so abutting occurrences collapse to a single marker). ``ranges`` is
    # bounded by _MAX_MASK_RANGES above, so this sort is O(n log n) on a bounded n, never
    # on the size of ``text``.
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    # (3) one pass, one marker per merged range.
    out: list[str] = []
    prev = 0
    for start, end in merged:
        out.append(text[prev:start])
        out.append(_REDACTION_MARKER)
        prev = end
    out.append(text[prev:])
    return "".join(out)


def _match_forms(values: Iterable[str]) -> list[str]:
    """The forms known values are matched and masked in, sorted: each value at the
    ``_MIN_SECRET_LEN`` floor or above, and that value with its line breaks
    normalized -- each CRLF, then each lone CR, made an LF.

    Text read in universal-newline mode -- a file read as text, the output of a
    subprocess -- has its line breaks normalized that way before anything matches
    or masks it, so a value holding a CR is found there only in its normalized
    form. The floor is the value's own: a value under it adds no form at all, and
    the normalized form of a value at it is kept whatever its length -- a derived
    form may be shorter than the floor. Text that differs from a value only in its
    line breaks counts as holding it.

    Passing the answer through again gives the same answer: a normalized form under
    the floor is there beside the value it came from, and a normalized form
    normalizes to itself. A list that went through this once can go through it again
    without losing a form.
    """
    held = {value for value in values if len(value) >= _MIN_SECRET_LEN}
    return sorted(held | {value.replace("\r\n", "\n").replace("\r", "\n") for value in held})


def _redaction_secrets() -> list[str]:
    """``known_secret_values`` in their match forms (``_match_forms``): the list
    every mask, scan and check as written takes.

    Unguarded, like the scan it wraps: a caller that must not fail on it says so
    itself (``_record_bodies``); every other caller wants the failure to propagate.
    That failure includes ``KnownSecretsIncomplete``, a list some ``.env`` could
    not be read into: never answered with the rest.
    """
    return _match_forms(known_secret_values())


def redact_known_secrets(text: str) -> str:
    """Replace every known secret VALUE in ``text`` with the redaction marker (D36).

    The LIVE-conversation counterpart of ``llm_log._redact``. llm_log scrubs secrets
    out of the bodies it STORES, but a disobedient tool/builder (``echo "$KB_API_KEY"``)
    can put a secret value into a tool RESULT, which the llm loop wraps VERBATIM into
    the next round's ``role:"tool"`` message -- where the model can read it and copy it
    onward (into an install ``summary``, another tool call, ...). Masking every known
    value at each boundary where raw tool output enters the conversation/state closes
    that class for the text it masks: a known value in it reaches the model as the
    marker, never as itself. What a caller attaches beside the answer afterwards -- a
    heading, a prefix, a separator, a truncation marker -- is outside this check, and
    with the masked text it can spell a known value that holds that text.

    Values come from ``known_secret_values`` (our key + every installed tool's .env
    values + the in-flight install secret); in their match forms (``_match_forms``:
    those at least ``_MIN_SECRET_LEN`` long, and each one's newline-normalized form)
    they are handed to the shared ``_mask_known_secrets`` (byte-identical to
    ``llm_log``'s copy), which computes every mask range on the PRISTINE text and
    merges them -- see that helper for why the range-based order is load-bearing (H1)
    and how the trailing fragment guard fits in.

    Unlike ``llm_log._redact``, this is on the LIVE path, so it does NOT swallow a
    provider failure into "return the text unmasked" -- that would leak the very value
    it exists to hide. ``known_secret_values`` is called WITHOUT a guard here, so any
    surprise it raised PROPAGATES to the caller's fail-closed handling (the llm loop's
    and meta-tools' own ``except`` degrade it into a safe failed-tool-result string),
    never a silent leak. A list some ``.env`` could not be read into is such a
    failure (``KnownSecretsIncomplete``), not a shorter list.
    """
    secrets = _redaction_secrets()
    # Bare call, deliberately unguarded (see the docstring above): a failure here must
    # PROPAGATE fail-closed, the opposite of llm_log._redact's guarded degrade-to-
    # unredacted direction (D36 round-5).
    return _mask_known_secrets(text, secrets)


def redact_known_secrets_as_shown(text: str) -> str:
    """``text`` as the model or a screen is shown it, with every known secret VALUE
    masked: ``_masked_as_shown`` with the secrets known now.

    For live-path text that can hold a lone surrogate: a file name or path read
    from disk -- on Linux, the bytes of a name that do not decode as UTF-8 come
    back as one each -- and a message built from one. ``redact_known_secrets`` leaves such a
    character where it is, and one of them fails the UTF-8 encode of the whole
    request or response the text rides in. Here each becomes three U+FFFD, and
    because those can complete a known value the text did not hold, the answer is
    checked once more with the mask's own matcher: one that holds a known value is
    the redaction marker, whole.

    The secrets and the failure direction are ``redact_known_secrets``'s: the list
    is ``_redaction_secrets``, collected without a guard, so a failure to collect
    it -- ``KnownSecretsIncomplete`` among them -- propagates to the caller. What a
    caller puts beside the answer afterwards is outside the check, as there.
    """
    return _masked_as_shown(text, _redaction_secrets())


# --- execution -------------------------------------------------------------


def _parse_dotenv_text(text: str) -> dict[str, str]:
    """Parse already-read ``.env`` TEXT into a plain env dict, interpolation OFF.

    The ONE dotenv parse the tool subsystem funnels through, so the runtime env load
    (``_load_tool_dotenv``) and the installer's post-write round-trip check
    (``tool_builder._inject_secret_into_env``) apply the IDENTICAL parser to the SAME
    bytes -- which is what lets that round-trip check guarantee the value the runtime
    later registers equals the value the user submitted. ``interpolate=False`` is
    LOAD-BEARING (see ``_load_tool_dotenv``): it keeps a ``${OPENAI_API_KEY}`` line
    literal instead of resolving it from our parent env. Bare keys (value None) and any
    parse failure are dropped defensively so a malformed ``.env`` degrades to {}.
    """
    try:
        values = dotenv_values(stream=io.StringIO(text), interpolate=False)
    except Exception:
        return {}
    return {key: value for key, value in values.items() if isinstance(value, str)}


def _load_tool_dotenv(package_root: PackageRoot) -> dict[str, str]:
    """Parse the package's optional ``.env`` into a plain env dict.

    Uses ``dotenv_values`` (already a project dependency, used by cli.py) to
    parse WITHOUT touching the parent ``os.environ`` -- these values go only into
    the child's environment, never ours. Bare keys (value None) and any parse
    failure are dropped defensively so a malformed ``.env`` degrades to "no extra
    env" rather than breaking the tool call.

    ``interpolate=False`` is LOAD-BEARING, not a style choice: python-dotenv's
    default POSIX-style interpolation resolves a ``${VAR}`` reference against the
    PARENT process environment (see resolve_variables, which folds os.environ
    into the lookup context). A tool ``.env`` line like ``LEAK=${OPENAI_API_KEY}``
    would then resolve to our real backend key -- reinjecting into the child the
    very credential the from-scratch env exists to EXCLUDE. With interpolation
    off the value is kept as the literal string ``${OPENAI_API_KEY}``, so a
    generated tool can never exfiltrate a parent secret through its own manifest.
    """
    return _load_package_dotenv(package_root.path / ".env")


def _load_tool_config(package_root: PackageRoot) -> dict[str, str]:
    """Parse the package's optional ``config.env`` (web-v6 §4) into a plain dict.

    The same read and the same parse as ``.env``, for the same reasons -- a FIFO,
    a symlink or a runaway file degrades to {} -- because the two files feed the
    same child environment and must not differ in how a line is understood. What
    differs is only what the CALLER does with the values: ``_build_tool_env``
    never hands these to the redactor.
    """
    return _load_package_dotenv(package_root.path / _CONFIG_FILENAME)


# Substrings that make a ``config.env`` KEY NAME look like a credential (web-v6
# §4). A reminder only, shown beside the key: it reads names, never values,
# because no rule can tell a secret from a setting by its value (``PIN=1234``).
_SECRET_LIKE_KEY_PARTS: tuple[str, ...] = ("KEY", "TOKEN", "SECRET", "PASSWORD")

# The migration sources (``migrate_tools_v5``) that mark a package as built
# before 0.4.0, when the builder was told to put secrets -- and in practice its
# settings too -- into ``.env`` (web-v6 §1 A5).
_PRE_V5_ORIGIN_SOURCES = frozenset({"legacy-ai-meta", "pre-existing/unknown"})


def _dotenv_file_state(env_file: Path) -> tuple[dict[str, str], bool]:
    """``(values, readable)`` for a package env file, for DISPLAY only.

    Missing is readable-and-empty (an absent file is a normal state); a file that
    exists but cannot be read the bounded way is ``readable=False``, so the page
    can say so instead of showing an empty list that looks like "no settings".
    """
    try:
        os.lstat(env_file)
    except FileNotFoundError:
        return {}, True
    except OSError:
        return {}, False
    text = _read_regular_file_capped(env_file, _ENV_FILE_MAX_BYTES)
    if text is None or len(text) > _ENV_FILE_MAX_BYTES:
        return {}, False
    return _parse_dotenv_text(text), True


def _built_before_v5(package_root: PackageRoot) -> bool:
    """True when any committed version's ``origin.json`` came from the v5 migration."""
    try:
        names = os.listdir(package_root.path / _VERSIONS_DIRNAME)
    except OSError:
        return False
    for name in names:
        resolved = _resolve_version_target(package_root, name)
        if resolved is not None and resolved[1].get("source") in _PRE_V5_ORIGIN_SOURCES:
            return True
    return False


def package_settings(package_root: PackageRoot) -> dict[str, Any]:
    """The package-layer settings view the 工具 page shows (web-v6 §9).

    Blocking. ``.env`` contributes KEY NAMES only; its values are read solely to
    flag conflicts and never leave this function. ``config.env`` contributes its
    pairs, but a value that CONTAINS a known secret -- one of the known values'
    match forms (``_match_forms``: a registered value at the redactor's floor, or
    its newline-normalized form, since the file is read with universal newlines) as
    an exact substring -- is flagged ``contains_secret`` and served as the marker
    alone: the file holds settings by the operator's choice, and a secret pasted
    into it by mistake is exactly what must not be served back.

    Exact containment, deliberately NOT ``redact_known_secrets``' own answer (P1
    review C1): that redactor also masks a text that merely ENDS with the first six
    characters of a secret -- a guard for text cut mid-secret by a cap, which a
    whole config value never is. Asking it here flagged an innocent
    ``https://host/myproj`` red because some token began ``myproj-``, sending the
    operator to hunt for a secret that is not there, the very confusion this view
    exists to remove. Key names still go through the full mask, the rule every
    operator-derived text in this subsystem follows; over-masking a name costs
    nothing. The known-secret set is computed ONCE here (review C2): the redactor
    recomputes it, a whole tools-directory scan, on every call.
    """
    secrets = _redaction_secrets()
    env_values, env_readable = _dotenv_file_state(package_root.path / ".env")
    config_values, config_readable = _dotenv_file_state(package_root.path / _CONFIG_FILENAME)
    config: list[dict[str, Any]] = []
    for key, value in config_values.items():
        contains_secret = any(secret in value for secret in secrets)
        config.append(
            {
                "key": _mask_known_secrets(key, secrets),
                "value": _REDACTION_MARKER if contains_secret else value,
                "overridden_by_env": key in env_values,
                "secret_like_name": any(part in key.upper() for part in _SECRET_LIKE_KEY_PARTS),
                "contains_secret": contains_secret,
            }
        )
    return {
        "env_keys": [_mask_known_secrets(key, secrets) for key in env_values],
        "env_readable": env_readable,
        "config": config,
        "config_readable": config_readable,
        "built_before_v5": _built_before_v5(package_root),
    }


@dataclass(frozen=True, slots=True)
class _DotenvRead:
    """One package env file as ``_read_package_dotenv`` read it.

    ``values`` is what the parse gave, {} unless the file was read and within the
    cap. ``unread`` is the type name of a failure that left the file unread (a
    look, an open or a read that failed other than with "not there"); None when
    the answer is definite, values or none. ``identity`` is the ``fstat`` identity
    of the file the text came from (``_CappedText``), when there was text.
    """

    values: dict[str, str]
    unread: str | None = None
    identity: tuple[int, int, int, int, int] | None = None


def _read_package_dotenv(env_file: Path) -> _DotenvRead:
    """The bounded read + interpolate=False parse both package env files share."""
    # Read through the ONE bounded-regular-file read (F3a): its O_NONBLOCK+S_ISREG
    # gate refuses a FIFO an operator (or a post-install mutation) could `mkfifo` in
    # place of .env -- which dotenv_values(path) would reopen and BLOCK on forever --
    # its O_NOFOLLOW refuses a symlinked .env, and its cap+1 read bounds the per-CALL
    # memory the parse would otherwise slurp whole into the child's exec env block.
    # No text (missing / FIFO / symlink / read error) and an over-cap length BOTH
    # give {} -- the SAME degrade-to-no-extra-env contract the malformed-.env
    # fallback below gives -- so a runaway or mutated .env can never bloat per call.
    # The over-cap test is on the text as read, the loader's own unit, never the
    # file's size.
    read = _read_regular_text_capped(env_file, _ENV_FILE_MAX_BYTES)
    if read.text is None:
        return _DotenvRead({}, unread=read.error if read.refusal == "failed" else None)
    if len(read.text) > _ENV_FILE_MAX_BYTES:
        return _DotenvRead({}, identity=read.identity)
    # Parse the ALREADY-READ text (never dotenv_values(path)): handing python-dotenv the
    # path would make it REOPEN the file -- a second, UNBOUNDED read that also re-follows a
    # symlink -- defeating the bounded, O_NOFOLLOW'd read above. ``_parse_dotenv_text`` is
    # the shared interpolate=False parser (see there); both reads must be the same bytes
    # and the same regular-file decision.
    return _DotenvRead(_parse_dotenv_text(read.text), identity=read.identity)


def _load_package_dotenv(env_file: Path) -> dict[str, str]:
    """The env file's values, {} whenever there are none to give (``_read_package_dotenv``).

    The runtime's degrade: a file that cannot be read gives no extra env, the same
    as one that is not there.
    """
    return _read_package_dotenv(env_file).values


def _build_tool_env(package_root: PackageRoot) -> tuple[dict[str, str], frozenset[str]]:
    """Build the child environment FROM SCRATCH: passthrough allowlist + tool .env.

    The parent environment is NEVER copied wholesale -- see the module docstring.
    Only ``_PASSTHROUGH_ENV`` is carried over from the parent; the tool's own
    ``.env`` is layered on top (so a tool may set, and even override PATH for,
    its own needs), but our backend secrets (OPENAI_API_KEY, ...) are structurally
    absent because they were never copied in.

    ``TLS_NO_VERIFY=1`` is injected between the two -- present ONLY when
    ``settings.tls_no_verify`` is on, ABSENT (never ``"0"``) when it is off, and
    itself still overridable by the tool's own ``.env`` -- so an installed tool
    MAY skip TLS certificate verification the same way this backend's own
    outbound connections do (see config.py), without that trust decision being
    silently forced on every tool regardless of the operator's setting.

    The package's ``config.env`` (web-v6 §4) sits between ``TLS_NO_VERIFY`` and
    ``.env``: it may override the injected flag exactly as ``.env`` always could,
    and ``.env`` overrides IT, so a key present in both resolves to the secret.

    Returns the env AND the (non-empty) ``.env`` VALUES that went into it, from
    the SAME read -- which is the whole point of handing them back rather than
    letting the caller re-read the file: a second read could see a rotated
    ``.env`` and register values the child never got, leaving the ones it DID get
    unmaskable. The passthrough names are deliberately NOT in that set: PATH and
    HOME are not secrets, and masking them would shred every result that mentions
    a path. Neither are the ``config.env`` values, for the reason that file
    exists at all. The values are handed to ``_inflight_secrets`` by
    ``_make_handler``.
    """
    env = {name: os.environ[name] for name in _PASSTHROUGH_ENV if name in os.environ}
    if get_settings().tls_no_verify:
        env["TLS_NO_VERIFY"] = "1"
    env.update(_load_tool_config(package_root))
    dotenv = _load_tool_dotenv(package_root)
    env.update(dotenv)
    # Empty values are dropped for the SAME reason ``_cached_env_values`` drops
    # them: a ``KEY=`` line contributes no secret to anything.
    return env, frozenset(value for value in dotenv.values() if value)


def _cap_output(text: str, cap: int) -> str:
    """Trim ``text`` to at most ``cap`` chars, marking it when cut.

    Mirrors ``memory_ai._truncate_to`` / ``llm_log._stored_body``, including the
    edge case where ``cap`` is too small to even hold the marker (the marker is
    dropped and the text hard-cut) -- unreachable given the setting's ge=1000
    floor, but kept correct independent of it.
    """
    if len(text) <= cap:
        return text
    marker_len = len(_OUTPUT_TRUNCATION_MARKER)
    if cap <= marker_len:
        return text[:cap]
    return text[: cap - marker_len] + _OUTPUT_TRUNCATION_MARKER


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the child's whole process group (it is a group leader).

    Every child drained by ``_communicate_bounded`` is started with
    ``start_new_session=True``, which made it a session/group leader: its group
    id is its pid, and one ``killpg`` takes down the child AND any grandchildren
    it spawned -- a tool that forked a runaway helper cannot survive the timeout.
    ESRCH and EPERM, the answers of a group with nothing left this process may
    signal, are ignored.

    Sent to ``proc.pid`` as it is: no ``getpgid``, which macOS answers ESRCH for
    an exited leader, and no ``proc.kill()``, whose ``poll()`` would reap that
    leader -- the teardown alone reaps it, and the group id stays pinned until
    then.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


@dataclass(slots=True)
class _BoundedOutput:
    """One bounded subprocess read's result (see ``_communicate_bounded``).

    ``stdout``/``stderr`` hold at most ``cap + 1`` chars each; the ``*_overflow``
    flags say a stream ran PAST the cap (and so the process was killed for it),
    which the callers use to decide between "here is the truncated output" and
    "the tool failed". ``timed_out`` is the wall-clock expiry, kept distinct so
    a caller can render the timeout message instead of an output. ``aborted``
    says the caller's stop signal ended the wait first (only a caller that
    passed one can see it set).
    """

    stdout: str
    stderr: str
    stdout_overflow: bool
    stderr_overflow: bool
    timed_out: bool
    aborted: bool = False


class _CappedReader:
    """Drains ONE text pipe on its own thread, bounded to ``cap + 1`` chars.

    The building block that replaces ``subprocess.communicate``'s unbounded
    slurp: each output pipe gets one of these on its own thread (reading two
    pipes sequentially would DEADLOCK -- a child that fills the OTHER pipe's
    buffer blocks on write while we block reading the first). It reads in
    ``_READ_CHUNK_CHARS`` chunks and, the moment the running total passes
    ``cap``, marks ``overflow`` and calls ``kill``: the result is already
    truncated, so there is nothing to gain by reading on -- kill the process
    group at once (mirroring the timeout path's SIGKILL) and stop. Never raises
    into its thread: a read on a killed/closed pipe just ends the drain.
    """

    __slots__ = ("_cap", "_kill", "_stream", "overflow", "text")

    def __init__(self, stream: IO[str], cap: int, kill: Callable[[], None]) -> None:
        self._stream = stream
        self._cap = cap
        self._kill = kill
        self.text = ""
        self.overflow = False

    def run(self) -> None:
        collected: list[str] = []
        total = 0
        try:
            while True:
                chunk = self._stream.read(_READ_CHUNK_CHARS)
                if not chunk:
                    break
                collected.append(chunk)
                total += len(chunk)
                if total > self._cap:
                    self.overflow = True
                    self._kill()
                    break
        except Exception:
            # A read on a pipe we just SIGKILLed (or one closed under us) must
            # never crash the reader thread -- the partial text already collected
            # is the truncated result, exactly as intended.
            pass
        finally:
            with contextlib.suppress(Exception):
                self._stream.close()
        joined = "".join(collected)
        # Retain at most cap+1 even if the final chunk overshot the cap by a whole
        # chunk: the caller's _cap_output then trims cap+1 down to the marked cap.
        self.text = joined[: self._cap + 1] if self.overflow else joined


def _process_group_is_gone(pgid: int) -> bool:
    """Whether no process group ``pgid`` exists any more.

    Asked with signal 0, which delivers nothing, so asking after the leader was
    reaped cannot reach another process. A group id taken up again since then
    reads as still there: the answer can be a needless "no", never a wrong "yes".
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _resend_group_kill(pgid: int, deadline: float) -> None:
    """Send SIGKILL to group ``pgid`` again until nothing in it is left to kill.

    For a platform whose group kill can miss a fork (``_GROUP_KILL_CAN_MISS_A_FORK``):
    ``_communicate_bounded``'s teardown calls it after its first SIGKILL and before
    it reaps the leader, with the leader unreaped (``proc.returncode`` is None) --
    the unreaped leader pins the group id, so every send here reaches this group
    and no other. Each round asks with signal 0 first and stops once that raises
    -- EPERM, macOS's answer when no live member is left to signal (exited ones
    waiting for their reaper count as none), or ESRCH -- or once ``deadline`` has
    passed. Whether the leader itself has exited is no reason to stop: a command
    that ended on its own can leave members running, forking as they go.

    Its end is not evidence of anything. EPERM is also the answer when every live
    member left is one this process may not signal, and stopping early then is
    right: such a member keeps the group in existence, which the check after the
    reap sees, and no further send could reach it.
    """
    while True:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(_GROUP_KILL_RESEND_SECONDS)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)


def _group_seen_gone_by(pgid: int, deadline: float) -> bool:
    """Whether group ``pgid`` is seen gone (``_process_group_is_gone``) by ``deadline``.

    Asked at once, then every ``_WAIT_POLL_SECONDS``: a member sent SIGKILL leaves
    the group only once its reaper (init, launchd) has collected it, so the first
    answer may come too early. Only signal 0 is ever sent, so asking after the
    leader's reap reaches no other process.
    """
    while not _process_group_is_gone(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_WAIT_POLL_SECONDS, remaining))
    return True


def _communicate_bounded(
    proc: subprocess.Popen[str],
    *,
    input_text: str | None,
    cap: int,
    timeout: float,
    abort: threading.Event | None = None,
    on_reaped: Callable[[], None] | None = None,
) -> _BoundedOutput:
    """``communicate`` with a hard per-stream memory cap and a wall-clock timeout.

    ``subprocess.communicate`` reads stdout/stderr UNBOUNDED into memory before
    any cap is applied -- a runaway tool could OOM the service before
    ``_cap_output`` ever runs. This drains each present pipe with its own
    ``_CappedReader`` thread (concurrent, so a full pipe can never deadlock the
    other), writes ``input_text`` to stdin on ITS OWN thread (so a large stdin
    can never deadlock against a child that writes before it reads), and bounds
    the whole read+wait by ``timeout`` -- killing the process group on expiry,
    the same contract the old ``communicate(timeout=...)`` carried. Every stream
    is capped at ``cap + 1`` chars; a stream over the cap kills the group at once
    (see ``_CappedReader``). Threads are the mechanism, not asyncio: this runs in
    a ``run_in_threadpool`` worker, and ``file.read`` releases the GIL while it
    blocks, so the reads and the ``wait`` genuinely proceed in parallel.

    ``abort`` is an optional stop signal set from another thread. Once it is set
    while the leader still runs, the wait ends at the next poll, and the group is
    sent SIGKILL and its leader reaped by the same teardown the deadline uses,
    under the same lock -- so a stop never needs a kill from outside, which could
    reach a pid already reaped and reused. The result then has ``aborted`` set.
    Without ``abort``, the wait is the deadline-only one it always was.

    Every way out passes through that teardown -- a return, and an exception
    too (a reader thread that cannot start, say) -- so no exit skips the kill and
    the reap. ``on_reaped``, when given, is called from the teardown itself, at
    the one point where it is known to have worked: ``proc.wait()`` has left a
    return code, and after that the group is seen gone -- signal 0 to it raises
    ESRCH. The group is asked at once after the reap, then again until the
    teardown's time limit (``_REAP_TIMEOUT_SECONDS``, counted from the teardown's
    start, so a slow reap leaves less of it). What a send answers is never that
    point: a SIGKILL delivered is not a member exited, macOS's group kill can miss
    a member forked while it is sent, and EPERM may mean only that the live
    members left are ones this process may not signal.
    Where the kill can miss a fork, it is sent again before the reap
    (``_resend_group_kill``); never after it, when the group id is no longer
    pinned. ``on_reaped`` is called the same way whether the function then
    returns or raises, and not at all when the reap failed or the group was still
    there at the limit; a caller that must know the process is gone relies on it,
    never on the return. It must not raise. Without it, the teardown sends one
    SIGKILL, watches nothing, and gives each thread's join the whole limit.

    There is no ``on_reaped`` to be had -- the process stays possibly running for
    the caller -- when, among others:

    * a member SIGKILL cannot end within the limit: one stuck in an
      uninterruptible wait in the kernel, or one this process may not signal (a
      setuid program) -- either keeps the group in existence;
    * the service runs as PID 1 on Linux with no init: an orphaned member is
      reaped by nobody, and its zombie keeps the group in existence, so a process
      that leaves any member besides its leader is never confirmed.
    """
    threads: list[threading.Thread] = []
    # `teardown_lock` + `reaped` make the overflow-kill callback below and the
    # teardown reap further down MUTUALLY EXCLUSIVE -- see `kill`'s comment for
    # the reused-pid hazard this closes.
    teardown_lock = threading.Lock()
    reaped = False

    def kill() -> None:
        # Called by a _CappedReader the instant its stream passes the cap. Guarded
        # so it can NEVER run once the teardown below has begun reaping: after
        # proc.wait() frees the leader's pid, _kill_process_group's killpg(proc.pid)
        # here could reach a group that has REUSED that id. The lock makes "kill"
        # and "reap" mutually exclusive; the `reaped` flag makes a kill attempt
        # after teardown a no-op. While NOT reaped (teardown not begun), the leader
        # is un-reaped -- the teardown is the only thing that reaps it -- so
        # proc.pid still names THIS group and the early overflow kill is valid.
        with teardown_lock:
            if reaped:
                return
            _kill_process_group(proc)

    timed_out = False
    aborted = False
    stdout_reader: _CappedReader | None = None
    stderr_reader: _CappedReader | None = None
    # The process exists from here on, so every way out of this function -- the
    # normal one, or an exception such as a reader thread that cannot start --
    # passes through the teardown in the ``finally``: none of them leaves the
    # group running or its leader unreaped.
    try:
        if input_text is not None and proc.stdin is not None:
            stdin = proc.stdin

            def _write_stdin() -> None:
                # Best-effort: a child that exits or closes stdin early makes this
                # raise BrokenPipeError -- not our failure. A SIGKILL later unblocks a
                # write stalled on a full pipe, so this thread always ends.
                with contextlib.suppress(Exception):
                    stdin.write(input_text)
                with contextlib.suppress(Exception):
                    stdin.close()

            # daemon=True on every reader/writer thread is a BACKSTOP, not the
            # mechanism: the unconditional group kill before the reap below is what
            # normally reaps a thread whose pipe is held open by a surviving
            # descendant. But should even that fail (a descendant that escaped the
            # process group entirely -- see the teardown comment), a NON-daemon
            # thread stuck in a blocking read/write would keep the interpreter alive at
            # shutdown; daemon makes that impossible -- at worst one FD leaks until
            # process exit, never a hung interpreter.
            writer = threading.Thread(target=_write_stdin, daemon=True)
            writer.start()
            threads.append(writer)

        stdout_reader = _CappedReader(proc.stdout, cap, kill) if proc.stdout is not None else None
        stderr_reader = _CappedReader(proc.stderr, cap, kill) if proc.stderr is not None else None
        for reader in (stdout_reader, stderr_reader):
            if reader is not None:
                # daemon=True for the same backstop reason as the writer above.
                thread = threading.Thread(target=reader.run, daemon=True)
                thread.start()
                threads.append(thread)

        # Watch the leader for exit WITHOUT reaping it, under the wall-clock deadline.
        # We must NOT ``proc.wait`` here: reaping frees the leader's pid, and the
        # UNCONDITIONAL group kill below (``os.killpg(proc.pid)``) would then race a
        # kernel that had already REUSED that pid as another process's group id --
        # signalling the wrong group. ``os.waitid(..., WEXITED | WNOWAIT | WNOHANG)``
        # instead DETECTS the exit and leaves the leader a zombie; a zombie's pid (and
        # therefore its process-GROUP id) is pinned un-reusable until we ``wait`` it,
        # which is the load-bearing invariant that makes the kill race-free. WNOHANG
        # makes each probe non-blocking so we poll under our own deadline.
        deadline = time.monotonic() + timeout
        while True:
            try:
                info = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            except ChildProcessError:
                # Already reaped. Nothing here reaps the leader before the teardown
                # does, so this is defensive: stop polling and fall through to it.
                break
            if info is not None:
                break  # leader exited; the zombie is retained, pinning the PGID
            if abort is not None and abort.is_set():
                aborted = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(_WAIT_POLL_SECONDS)
    finally:
        # Tear the WHOLE group down and reap, atomically w.r.t. the overflow-kill
        # callback above (teardown_lock): setting `reaped` before releasing the lock
        # guarantees no _CappedReader can send a kill to the pid we are about to free
        # below. Within the lock, the kill+reap still run UNCONDITIONALLY --
        # whether the leader exited on its own, we timed out, or the caller's stop
        # signal ended the wait. This is the fix for two holes the old "escalate only
        # if a reader thread is still alive" teardown carried:
        #  (a) a descendant that CLOSED its inherited stdout/stderr but kept running
        #      left the readers at EOF and their threads dead, so no escalation ever
        #      fired and the detached daemon LEAKED. An unconditional kill reaches it;
        #  (b) killing AFTER ``proc.wait`` reaped the leader targeted a pid the kernel
        #      had already freed -- a REUSED-pgid hazard. Here the kill comes before
        #      the reap: the leader is a zombie on natural exit, or alive on timeout
        #      or stop, so ``proc.pid`` still names THIS group (``start_new_session=True``
        #      made the leader its own group leader, so the group id EQUALS its pid).
        # A leader that exited NATURALLY is already a zombie, so this SIGKILL cannot
        # disturb its recorded exit status -- ``proc.wait`` below still returns the
        # tool's REAL exit code. Whether the send succeeds says nothing about whether
        # the group is gone (see the docstring); with ``on_reaped`` that is asked of
        # the group itself, after the reap. A descendant that double-fork/setsid'd
        # OUT of the group escapes even this and is beyond v1's non-container stance
        # (D21); the daemon flag on every reader/writer thread is the honest backstop.
        #
        # One limit, counted from here (``_REAP_TIMEOUT_SECONDS``), bounds the
        # re-sent kill, the watch after the reap, and the join. The reap itself
        # has no bound.
        deadline = time.monotonic() + _REAP_TIMEOUT_SECONDS
        with teardown_lock:
            if not reaped:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                # A defence, not a path anything here takes: a leader reaped by
                # anything before the teardown no longer pins the group id, so
                # nothing is sent to that id again.
                if (
                    on_reaped is not None
                    and _GROUP_KILL_CAN_MISS_A_FORK
                    and proc.returncode is None
                ):
                    _resend_group_kill(proc.pid, deadline)

                # Reap the leader: this frees the zombie (releasing the pinned pid)
                # and, on a natural exit, returns the tool's REAL returncode --
                # preserving exit-code semantics for ordinary tools. No timeout: a
                # natural-exit zombie is collected at once, and a leader just sent
                # SIGKILL dies promptly (SIGKILL is uncatchable).
                with contextlib.suppress(Exception):
                    proc.wait()
                reaped = True

        # The evidence ``on_reaped`` stands for, asked of the group itself now that its
        # leader is reaped: gone at once, or within what is left of the limit. Only
        # signal 0 is sent from here on, so it is asked OUTSIDE teardown_lock.
        if (
            on_reaped is not None
            and proc.returncode is not None
            and _group_seen_gone_by(proc.pid, deadline)
        ):
            on_reaped()

        # Once the group is gone no member holds a write end of the pipes, so the
        # (daemon) reader/writer threads hit EOF and the join completes at once. With
        # ``on_reaped`` the join gets what is left of the one limit: a group still
        # there at the limit has left the caller without evidence anyway. Without it
        # each thread gets the whole limit, purely so a pathologically wedged pipe can
        # never turn cleanup itself into a hang.
        # OUTSIDE teardown_lock (released above): a reader thread still inside `kill`,
        # blocked waiting on the lock, must be free to acquire it and no-op before this
        # join can observe that thread finished -- joining while still holding the lock
        # could deadlock against it.
        for thread in threads:
            if on_reaped is None:
                thread.join(timeout=_REAP_TIMEOUT_SECONDS)
            else:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))

    return _BoundedOutput(
        stdout=stdout_reader.text if stdout_reader is not None else "",
        stderr=stderr_reader.text if stderr_reader is not None else "",
        stdout_overflow=stdout_reader.overflow if stdout_reader is not None else False,
        stderr_overflow=stderr_reader.overflow if stderr_reader is not None else False,
        timed_out=timed_out,
        aborted=aborted,
    )


# Returned INSTEAD of running anything when the package is no longer the one
# whose schema was advertised (see ``_make_handler``). Category only -- no name,
# no path, no identity values -- because this string goes straight into the
# conversation as a role:"tool" message, the same discipline every other outcome
# string in this module follows.
_TOOL_REPLACED_RESULT = "tool not run: this tool's package changed after it was offered"

# Returned INSTEAD of running anything when the tool was switched OFF between the
# advertisement and the call (see ``_make_handler``). Its OWN string rather than a
# second use of the one above, because that one asserts something that would not
# be true here: nothing about the package CHANGED -- the manifest is byte-identical
# and its identity has not moved (that is the whole of web-v5 P1) -- the operator
# simply revoked the tool. Telling the model the package was replaced would send it
# to re-read a spec that is still exactly what it was given. Category only, same
# discipline as its sibling: this string goes straight into the conversation.
_TOOL_DISABLED_RESULT = "tool not run: this tool was disabled after it was offered"


# --- run records (web-v6 §6) -------------------------------------------------

# How one call ended, as its run record states it. ``refused`` is the tool never
# running (disabled, replaced, identity changed); ``error`` is the BACKEND failing
# the call -- the secret scan behind the redaction, the tools lock -- whether or
# not the tool had run. One that raised the model reads as ``tool execution
# failed: <type>``; a tool that ran and whose output could not be masked, as
# ``output_withheld_text``, which says it ran.
# Every other non-ok value is a way a tool that DID start can fail.
# ``output_overflow`` is the one the model's string cannot tell apart: stderr past
# the cap kills the process group, so the model sees ``tool failed (exit -9)`` --
# the record says what really happened.
RUN_OK = "ok"
RUN_FAILED = "failed"
RUN_OUTPUT_OVERFLOW = "output_overflow"
RUN_TIMEOUT = "timeout"
RUN_START_FAILED = "start_failed"
RUN_REFUSED = "refused"
RUN_ERROR = "error"

# The outcomes a fix of the tool's code can address: it started and failed. The
# one-click fix is offered for these alone (web-v6 §8.5), and they are what an
# AI request reports beside its result (§8.2).
RUN_FAILURE_OUTCOMES = frozenset({RUN_FAILED, RUN_OUTPUT_OVERFLOW, RUN_TIMEOUT, RUN_START_FAILED})


def output_withheld_text(what: str, exit_code: int | None) -> str:
    """The result of a command that ran but whose output could not be checked.

    ``what`` is "tool" (an installed tool) or "command" (the builder's
    ``run_shell``). Says that it ran and how it ended, so that it is not taken
    for a call that never happened, and that its output is withheld because it
    could not be checked for known secrets -- none of it, nor why in more detail,
    is in the text.
    """
    return (
        f"{what} ran (exit {exit_code}), but its output is withheld: "
        "it could not be checked for known secrets"
    )


@dataclass(frozen=True, slots=True)
class ToolRunResult:
    """One call's outcome: the string the model gets, plus what its record keeps.

    ``text`` is the agent-visible result, the same whether or not a recorder is
    wired (invariant F) -- the record is an observer and must not change the
    conversation. ``args``/``stdout``/``stderr``
    are set only for a run that started and failed, already REDACTED and not yet
    CUT: the recorder applies its own caps afterwards, the redact->cap order.
    """

    text: str
    outcome: str
    exit_code: int | None = None
    truncated: bool = False
    args: str | None = None
    stdout: str | None = None
    stderr: str | None = None


@dataclass(frozen=True, slots=True)
class ToolRunRecord:
    """Everything one run record stores (web-v6 §6.1), as handed to the recorder."""

    tool_name: str
    vid: str
    started_at: datetime
    duration_ms: int
    outcome: str
    exit_code: int | None
    truncated: bool
    workflow: str
    check_id: str | None
    llm_log_id: int | None
    llm_log_process: str | None
    message: str | None
    args: str | None
    stdout: str | None
    stderr: str | None


@dataclass(frozen=True, slots=True)
class ToolFailure:
    """One failed call of an installed tool during the current AI request (web-v6 §8.2).

    ``run_id`` is its run record, or None when the record was not written --
    the failure is reported either way, and the page then offers only a link.
    """

    run_id: int | None
    tool_name: str
    vid: str
    outcome: str


@dataclass(slots=True)
class CheckContext:
    """A run of a tool's examples (web-v6 §10) that the calls in it belong to.

    Set by ``run_examples`` around its calls: their records carry ``workflow``
    (``health_check`` or ``verify``) and ``check_id`` -- the check whose verdict
    (``tool_check``) they are the evidence of -- and ``runs`` collects each
    call's ``(run_id, outcome)`` in order, which is how the runner learns an
    outcome the handler's string does not carry.
    """

    check_id: str
    workflow: str
    runs: list[tuple[int | None, str]] = field(default_factory=list)


_CHECK_CONTEXT: ContextVar[CheckContext | None] = ContextVar("_CHECK_CONTEXT", default=None)


# The failures of the AI request running in this context, collected for its
# response (``collect_tool_failures``). None outside such a request: a direct
# service caller collects nothing. A ContextVar rather than a parameter for the
# reason ``llm._CURRENT_INTERACTION`` is one: the handler signature is fixed, and
# the handlers run in the request's own task, so two concurrent requests each
# see only their own list (invariant Q).
_REQUEST_TOOL_FAILURES: ContextVar[list[ToolFailure] | None] = ContextVar(
    "_REQUEST_TOOL_FAILURES", default=None
)


@contextlib.contextmanager
def collect_tool_failures() -> Iterator[list[ToolFailure]]:
    """Collect this context's tool failures into the yielded list."""
    failures: list[ToolFailure] = []
    token = _REQUEST_TOOL_FAILURES.set(failures)
    try:
        yield failures
    finally:
        _REQUEST_TOOL_FAILURES.reset(token)


# The run recorder, injected by main (``tool_runs.record``) exactly as llm_log's
# secret provider is: this module stays free of the database, and a test can
# swap the recorder for a list. None means "record nothing" -- the state of every
# process that never wired one.
_run_recorder: Callable[[ToolRunRecord], int | None] | None = None


def set_run_recorder(recorder: Callable[[ToolRunRecord], int | None] | None) -> None:
    """Install (or, with None, remove) the callable that persists run records."""
    global _run_recorder
    _run_recorder = recorder


@dataclass(frozen=True, slots=True)
class ToolCheckRecord:
    """The verdict of one run of a version's examples, as ``tool_check`` keeps it."""

    check_id: str
    tool_name: str
    vid: str
    workflow: str
    finished_at: datetime
    total: int
    passed: int
    failed: int
    stopped: str | None


# The verdict recorder, injected by main (``tool_runs.record_check``) the way the
# run recorder is, for the same reasons; None records nothing.
_check_recorder: Callable[[ToolCheckRecord], int | None] | None = None


def set_check_recorder(recorder: Callable[[ToolCheckRecord], int | None] | None) -> None:
    """Install (or, with None, remove) the callable that persists check verdicts."""
    global _check_recorder
    _check_recorder = recorder


def _record_bodies(
    args_json: str, stdout: str | None, stderr: str | None
) -> tuple[str | None, str | None, str | None]:
    """Redacted copies of a failed run's input and output, for its record ONLY.

    Used on the paths whose model-facing string carries no output (a timeout, a
    process that never started), so the redaction here is the record's alone and
    must not be allowed to change the call: a failing secret scan yields no
    bodies instead of an exception. The path whose string DOES carry output (a
    non-zero exit) redacts fail-closed for the model and reuses that result.
    """
    try:
        secrets = _redaction_secrets()
        return (
            _mask_known_secrets(args_json, secrets),
            None if stdout is None else _mask_known_secrets(stdout, secrets),
            None if stderr is None else _mask_known_secrets(stderr, secrets),
        )
    except Exception:
        return None, None, None


def _run_tool_subprocess(
    entry: list[str],
    advertised: Resolved,
    expected_identity: tuple[int, int, int] | None,
    advertisement_generation: _AdvertisementGeneration,
    tools_lock_fd: int,
    env: dict[str, str],
    args_json: str,
    timeout: float,
    output_cap: int,
) -> ToolRunResult:
    """Run the advertised version; return the agent-visible result and its record.

    The final ordering is deliberate and compact: refuse a version retired by a
    completed discard, read the PackageRoot toggle, require that ``current`` still
    resolves to some usable version, verify the ADVERTISED VersionRoot identity,
    then call ``Popen`` with nothing between that identity check and process
    creation.  Resolving ``current`` does not compare vids: a normal revise may
    move it while this call must remain bound to the schema it was shown.

    ``ToolRunResult.text`` is exactly the string this function returned before
    run records existed (web-v6 §6.3); everything else on the result is for the
    record.
    """
    version_root = advertised.version_root
    if _advertisement_retired(advertisement_generation):
        return ToolRunResult(_TOOL_REPLACED_RESULT, RUN_REFUSED)
    package_root = advertised.package_root
    if not package_enabled(package_root):
        return ToolRunResult(_TOOL_DISABLED_RESULT, RUN_REFUSED)
    if isinstance(resolve_current(package_root), Unresolved):
        return ToolRunResult(_TOOL_REPLACED_RESULT, RUN_REFUSED)
    # This identity check is deliberately LAST. No state read, lineage resolve,
    # retired-marker lookup, or other fallible operation may be inserted between
    # its answer and Popen's cwd resolution.
    if not _still_the_expected_package(version_root, expected_identity):
        return ToolRunResult(_TOOL_REPLACED_RESULT, RUN_REFUSED)
    try:
        proc = subprocess.Popen(
            entry,
            cwd=version_root.path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            # New session/group so the timeout path can kill the whole tree.
            start_new_session=True,
            # The tool must never close this inherited descriptor itself. This
            # is the only place mutual exclusion depends on tool behaviour: the
            # child's reference keeps the request's shared flock alive even if
            # the backend is killed and its own descriptor disappears.
            pass_fds=(tools_lock_fd,),
        )
    except OSError as exc:
        # FileNotFoundError (bad interpreter/script), PermissionError (no exec
        # bit), ... -- the tool never ran. Category only, no raw error string.
        args, _stdout, _stderr = _record_bodies(args_json, None, None)
        return ToolRunResult(
            f"tool failed to start: {type(exc).__name__}", RUN_START_FAILED, args=args
        )

    output = _communicate_bounded(proc, input_text=args_json, cap=output_cap, timeout=timeout)
    if output.timed_out:
        # The model is told only that time ran out; the record keeps what the
        # tool had printed by then, which is usually where it was stuck.
        args, stdout, stderr = _record_bodies(args_json, output.stdout, output.stderr)
        return ToolRunResult(
            f"tool timed out after {timeout:g} seconds",
            RUN_TIMEOUT,
            args=args,
            stdout=stdout,
            stderr=stderr,
        )
    # F1/D36: mask any known secret VALUE the child wrote to stdout/stderr BEFORE the
    # size cap. Redact-before-truncate is load-bearing (the same order llm_log's stored
    # -body pipeline uses): a secret straddling the cap edge must be replaced while the
    # text is still whole, or half of it would survive the cut. This is the boundary
    # where raw child output first enters the app -- the llm loop wraps this string
    # VERBATIM into the next round's role:"tool" message -- so masking here keeps a
    # disobedient tool's echoed secret out of the LIVE conversation, not just the log.
    # Applied on the taken branch only, so ``known_secret_values`` is computed once.
    # STDOUT over the cap is the success result, already truncated -- return it
    # regardless of the (kill-induced, negative) exit code the overflow kill left
    # behind: the tool produced its answer, we just stopped reading it. _cap_output
    # trims the retained cap+1 chars down to the cap behind the marker.
    try:
        if output.stdout_overflow:
            return ToolRunResult(
                _cap_output(redact_known_secrets(output.stdout), output_cap),
                RUN_OK,
                truncated=True,
            )
        if proc.returncode != 0:
            # One snapshot of the known secrets serves the model's string AND the
            # record, so this branch still scans once. Fail-closed exactly as
            # ``redact_known_secrets`` is: these are the same two calls it makes.
            secrets = _redaction_secrets()
            stderr = _mask_known_secrets(output.stderr, secrets)
            # ``output_overflow`` only when the overflow KILL ended it: a tool that
            # wrote past the cap and exited with its own code first is a plain failure
            # (its code is what the model was told). Exit 0 never reaches here.
            killed_for_output = output.stderr_overflow and proc.returncode == -signal.SIGKILL
            return ToolRunResult(
                f"tool failed (exit {proc.returncode}): {_cap_output(stderr.strip(), output_cap)}",
                RUN_OUTPUT_OVERFLOW if killed_for_output else RUN_FAILED,
                exit_code=proc.returncode,
                args=_mask_known_secrets(args_json, secrets),
                stdout=_mask_known_secrets(output.stdout, secrets),
                stderr=stderr,
            )
        return ToolRunResult(_cap_output(redact_known_secrets(output.stdout), output_cap), RUN_OK)
    except Exception:
        # The tool RAN; only what it wrote could not be checked against the known
        # secrets (a ``.env`` that cannot be read, say), so none of it is handed
        # over, to the model or to the record. Said as it is: told the call failed,
        # a model may run it again, and the tool may act on something real.
        return ToolRunResult(output_withheld_text("tool", proc.returncode), RUN_ERROR)


@dataclass(slots=True, weakref_slot=True)
class _AdvertisementGeneration:
    """One cohort of handlers offered for the same VersionRoot between discards."""

    retired: bool = False


# Retirement belongs to the ADVERTISEMENT, not to any recreatable filesystem
# attribute. Every handler closure holds its cohort strongly; this weak map only
# lets discard find the CURRENT cohort for a VersionRoot. Discard pops that cohort
# before marking it, so a legitimately restored/current version receives a fresh
# cohort while every old handler keeps its own retired object forever.
#
# The map is bounded by live, not-yet-discarded advertisement cohorts: when the
# last handler for a cohort is collected, WeakValueDictionary removes its entry;
# discarded cohorts are removed eagerly. It therefore cannot grow with vid,
# directory, inode, or discard history.
_ADVERTISEMENT_GENERATIONS: weakref.WeakValueDictionary[VersionRoot, _AdvertisementGeneration] = (
    weakref.WeakValueDictionary()
)
_ADVERTISEMENT_LOCK = threading.Lock()


def _advertisement_generation_locked(
    version_root: VersionRoot,
) -> _AdvertisementGeneration:
    """Return/create a cohort while the caller holds ``_ADVERTISEMENT_LOCK``."""

    generation = _ADVERTISEMENT_GENERATIONS.get(version_root)
    if generation is None:
        generation = _AdvertisementGeneration()
        _ADVERTISEMENT_GENERATIONS[version_root] = generation
    return generation


def _advertisement_generation(version_root: VersionRoot) -> _AdvertisementGeneration:
    """Return the live handler cohort for one advertised VersionRoot."""

    with _ADVERTISEMENT_LOCK:
        return _advertisement_generation_locked(version_root)


def _resolve_and_register_advertisement(
    package_root: PackageRoot,
) -> tuple[Resolution, _AdvertisementGeneration | None]:
    """Capture a resolution together with the cohort a discard must retire."""

    with _ADVERTISEMENT_LOCK:
        resolution = resolve_current(package_root)
        if isinstance(resolution, Unresolved):
            return resolution, None
        # Registration shares the discard publication lock, so the guard exists
        # from the instant this scan captures V: discard either retires this exact
        # cohort afterward, or publishes first and the scan captures its successor.
        return resolution, _advertisement_generation_locked(resolution.version_root)


def _publish_discard_and_retire(
    package_root: PackageRoot,
    previous_vid: str,
    discarded: VersionRoot,
) -> CurrentPublication:
    """Commit one discard and retire every scan that captured its old current."""

    with _ADVERTISEMENT_LOCK:
        publication = publish_current(package_root, previous_vid)
        if publication:
            generation = _ADVERTISEMENT_GENERATIONS.pop(discarded, None)
            if generation is not None:
                generation.retired = True
        return publication


def _advertisement_retired(generation: _AdvertisementGeneration) -> bool:
    """Read one process-local handler generation under its mutation lock."""

    with _ADVERTISEMENT_LOCK:
        return generation.retired


# The STALE-CLEANUP namespace: where a package goes when it must stop being a
# package NOW but its best-effort recursive cleanup may fail. The old execution
# registry parked running packages here; mutual exclusion makes that path
# unreachable. ``.{name}.stale-<token>`` remains the only shape
# ``tool_builder._sweep_stale_backups`` will ever remove, so an interrupted or
# permission-blocked delete can be retried without exposing a phantom package.
#
# ``delete_tool`` mints this name and tool_builder's sweep recognizes it. The
# shared spelling lives here because tool_builder already imports this module;
# duplicating the on-disk contract would let writer and collector drift.
#
# Dot-prefixed is load-bearing, not cosmetic: ``_scan_all`` and
# ``known_secret_values`` both skip hidden directories, so stale remains are
# inert litter rather than a phantom package. ``\Z`` rather than ``$`` because
# the pattern gates an ``rmtree``: ``$`` also matches before a trailing newline,
# and a filename may legally contain one.
#
# The identifiers keep the word BACKUP even though a delete's remains are not a
# backup of anything, and that is a decision: the on-disk name -- the actual
# contract, the thing one side writes and the other reads -- says ``stale``,
# which is true of both writers, and renaming the Python symbols would leave the
# D40 r3 addendum pointing at names that no longer exist.
_STALE_BACKUP_RE = re.compile(r"^\.[a-z0-9][a-z0-9_-]{0,63}\.stale-[0-9a-f]{32}\Z")


def _stale_backup_path(base: Path, name: str, token: str) -> Path:
    """Where a package goes to await collection (see ``_STALE_BACKUP_RE``).

    The mate of that pattern: what this writes, the sweep must recognize, so a
    test pins the pair (including at the longest legal package name).
    """
    return base / f".{name}.stale-{token}"


async def _record_run(
    advertised: Resolved, result: ToolRunResult, *, started_at: datetime, duration_ms: int
) -> int | None:
    """Hand one finished call to the run recorder; never raise, never alter the call.

    The no-observer-failure invariant (web-v6 §6.3, invariant F): whatever the
    recorder does -- raise, find the database locked, not be wired at all -- the
    tool's result goes back to the model unchanged. A failure leaves one WARNING
    line naming only the exception's type: a database error's text carries the
    statement and its parameters, which here are the run's bodies.

    A failure of the tool also goes to the current request's collector, if
    there is one -- whether or not its record was written (web-v6 §8.2).
    """
    run_id = await _write_record(advertised, result, started_at=started_at, duration_ms=duration_ms)
    check = _CHECK_CONTEXT.get()
    if check is not None:
        check.runs.append((run_id, result.outcome))
    failures = _REQUEST_TOOL_FAILURES.get()
    if failures is not None and result.outcome in RUN_FAILURE_OUTCOMES:
        failures.append(
            ToolFailure(
                run_id=run_id,
                tool_name=advertised.package_root.path.name,
                vid=advertised.vid,
                outcome=result.outcome,
            )
        )
    return run_id


async def _write_record(
    advertised: Resolved, result: ToolRunResult, *, started_at: datetime, duration_ms: int
) -> int | None:
    recorder = _run_recorder
    if recorder is None:
        return None
    interaction = current_interaction()
    check = _CHECK_CONTEXT.get()
    if check is not None:
        workflow = check.workflow
    elif interaction is not None:
        workflow = interaction.workflow
    else:
        workflow = "unknown"
    record = ToolRunRecord(
        tool_name=advertised.package_root.path.name,
        vid=advertised.vid,
        started_at=started_at,
        duration_ms=duration_ms,
        outcome=result.outcome,
        exit_code=result.exit_code,
        truncated=result.truncated,
        workflow=workflow,
        check_id=check.check_id if check is not None else None,
        llm_log_id=interaction.record_id if interaction is not None else None,
        llm_log_process=interaction.process if interaction is not None else None,
        message=None if result.outcome == RUN_OK else result.text,
        args=result.args,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    try:
        return await run_in_threadpool(recorder, record)
    except Exception as exc:
        _logger.warning("Tool run record not written: %s", type(exc).__name__)
        return None


# Tool calls whose request may already be gone (``_make_handler``): strong
# references, so a call still running is never garbage-collected mid-flight.
_CALL_TASKS: set[asyncio.Task[ToolRunResult]] = set()


def _forget_call_task(task: asyncio.Task[ToolRunResult]) -> None:
    _CALL_TASKS.discard(task)
    # Retrieved so a call that failed after its request had gone is not logged
    # as "Task exception was never retrieved": the failure already has its
    # record (``error``), which is everything that can be done with it.
    if not task.cancelled():
        task.exception()


def _make_handler(
    advertised: Resolved,
    entry: list[str],
    identity: tuple[int, int, int] | None,
    advertisement_generation: _AdvertisementGeneration,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Bind one advertisement to its package and exact resolved version.

    The request-wide shared lock is captured with the advertisement and inherited
    by any child. A direct service caller that did not enter the HTTP request
    scope takes a shared lock for this handler invocation as a fail-safe; normal
    capture/enrich/assist-update requests already hold one across scan, every
    model round, every tool call, and error cleanup.

    Each call runs as a task of its own, shielded from the caller (P4 review r1,
    H1). The interaction's deadline (``llm.generate_structured``) cancels the
    request with a tool in flight, and asyncio cancellation cannot stop the
    worker thread that runs the tool: before this, the request then went on
    without the call -- no record, and the call's ``.env`` values released
    while its output still had to be masked. Shielded, the call ends the way
    every call does: masked while its values are held, recorded, its lock
    reference closed. That reference is the call's OWN (a ``dup`` of the
    request's, taken while the request's is certainly open), so the request
    closing its descriptor neither drops the lock under a running tool nor
    leaves the call a descriptor number the process may already have reused.
    """

    request_lock_fd = _REQUEST_TOOLS_LOCK_FD.get()

    async def _execute(arguments: dict[str, Any], tools_lock_fd: int) -> ToolRunResult:
        package_root = advertised.package_root
        # Recheck inside the hold, then once more immediately before Popen.
        if not _still_the_expected_package(advertised.version_root, identity):
            return ToolRunResult(_TOOL_REPLACED_RESULT, RUN_REFUSED)
        # Toggle and .env are PackageRoot state, intentionally read at call
        # time. Missing, foreign, or unreadable state all fail closed.
        if not package_enabled(package_root):
            return ToolRunResult(_TOOL_DISABLED_RESULT, RUN_REFUSED)
        settings = get_settings()
        env, env_secrets = _build_tool_env(package_root)
        args_json = json.dumps(arguments, ensure_ascii=False)
        # The ``.env`` VALUES this child is about to be handed, held so they
        # stay redactable no matter what the file says by the time the child
        # finishes. The scope has to reach past the subprocess: the masking of
        # the child's output happens INSIDE ``_run_tool_subprocess`` (F1/D36),
        # so releasing on the child's exit would still be too early. It ends
        # where the awaited call returns, which is after that redaction -- and
        # after the redaction of the run record's bodies, which happens there
        # too -- and before the string is handed to the llm loop: everything
        # downstream (the role:"tool" message, the log, the JSONL sink, the run
        # record) sees the masked copy.
        with _inflight_secrets(env_secrets):
            return await run_in_threadpool(
                _run_tool_subprocess,
                list(entry),
                advertised,
                identity,
                advertisement_generation,
                tools_lock_fd,
                env,
                args_json,
                settings.llm_tool_timeout_seconds,
                settings.llm_tool_output_max_chars,
            )

    async def _call(arguments: dict[str, Any], call_lock_fd: int | None) -> ToolRunResult:
        """One call and its record, start to finish; closes ``call_lock_fd``."""
        started_at = datetime.now(UTC)
        started = time.monotonic()
        owned_lock_fd = call_lock_fd
        try:
            try:
                tools_lock_fd = owned_lock_fd
                if identity is not None and tools_lock_fd is None:
                    owned_lock_fd = await acquire_shared_tools_lock_async()
                    tools_lock_fd = owned_lock_fd
                # Timed from here: a direct caller may have waited for a destroyer
                # to let go of the lock, and that wait is not the tool's duration.
                started_at = datetime.now(UTC)
                started = time.monotonic()
                if identity is None or tools_lock_fd is None:
                    result = ToolRunResult(_TOOL_REPLACED_RESULT, RUN_REFUSED)
                else:
                    result = await _execute(arguments, tools_lock_fd)
            except Exception as exc:
                # The call fails here and the llm loop turns the exception into
                # ``tool_exception_text`` for the model; the record stores those
                # same words, then the exception goes on unchanged (invariant F).
                await _record_run(
                    advertised,
                    ToolRunResult(tool_exception_text(exc), RUN_ERROR),
                    started_at=started_at,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise
            # Written while the shared lock is still held, so a delete (which
            # needs it exclusively, and clears the tool's records) cannot land
            # between this run and its record. A refusal above may hold no lock
            # to write under -- and ran nothing.
            await _record_run(
                advertised,
                result,
                started_at=started_at,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return result
        finally:
            release_tools_lock(owned_lock_fd)

    async def _handler(arguments: dict[str, Any]) -> str:
        try:
            call_lock_fd = os.dup(request_lock_fd) if request_lock_fd is not None else None
        except OSError as exc:
            # The backend failing the call before it began (EMFILE): recorded
            # like every other backend failure, then raised for the llm loop.
            await _record_run(
                advertised,
                ToolRunResult(tool_exception_text(exc), RUN_ERROR),
                started_at=datetime.now(UTC),
                duration_ms=0,
            )
            raise
        try:
            task = asyncio.ensure_future(_call(arguments, call_lock_fd))
        except BaseException:
            release_tools_lock(call_lock_fd)
            raise
        _CALL_TASKS.add(task)
        task.add_done_callback(_forget_call_task)
        return (await asyncio.shield(task)).text

    return _handler


# --- examples.json: a version's regression inputs (web-v6 §10) ---------------


@dataclass(frozen=True, slots=True)
class ToolExample:
    """One input the tool must keep handling: a label and the JSON it is given."""

    name: str
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExamplesFile:
    """A version's examples, or why they cannot be run (``error``, zh-TW).

    No file is no examples and no error. A file that is there but unusable is
    an error and runs NOTHING: a half-understood list would report a partial
    check as a clean one.
    """

    examples: tuple[ToolExample, ...] = ()
    error: str | None = None


def load_examples(version_root: VersionRoot) -> ExamplesFile:
    """Read and validate one version's ``examples.json``.

    Read through the bounded, FIFO/symlink-hardened reader every other package
    file goes through. The shape is ``{"examples": [{"name": str, "args": {...}},
    ...]}`` with at most ``EXAMPLES_MAX`` entries; each ``args`` is a JSON object,
    because that is what a tool is called with.

    No examples only when ``examples.json`` is seen not to exist (``_seen_gone``).
    A look at it that fails reads on, so the read reports a file it could not
    read instead of passing for no examples.
    """
    path = version_root.path / _EXAMPLES_FILENAME
    if _seen_gone(path):
        return ExamplesFile()
    text = _read_regular_file_capped(path, _EXAMPLES_MAX_BYTES)
    if text is None:
        return ExamplesFile(error="examples.json 無法讀取（不是一般檔案，或沒有權限）。")  # noqa: RUF001
    if len(text) > _EXAMPLES_MAX_BYTES:
        return ExamplesFile(error="examples.json 超過 64 KiB。")
    try:
        data = json.loads(text)
    except ValueError, RecursionError:
        return ExamplesFile(error="examples.json 不是合法的 JSON。")
    entries = data.get("examples") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return ExamplesFile(error="examples.json 需要一個 examples 陣列。")
    if len(entries) > EXAMPLES_MAX:
        too_many = f"examples.json 最多 {EXAMPLES_MAX} 個範例，目前有 {len(entries)} 個。"  # noqa: RUF001
        return ExamplesFile(error=too_many)
    examples = []
    for index, entry in enumerate(entries, start=1):
        name = entry.get("name") if isinstance(entry, dict) else None
        args = entry.get("args") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name.strip() or not isinstance(args, dict):
            return ExamplesFile(
                error=f"examples.json 的第 {index} 個範例需要非空的 name 與一個 args 物件。"
            )
        # JSON object keys are always strings; the comprehension says so to the
        # type checker as well.
        examples.append(
            ToolExample(
                name=_utf8_safe(name.strip()),
                args={str(key): value for key, value in args.items()},
            )
        )
    return ExamplesFile(examples=tuple(examples))


def example_display_name(name: str, secrets: list[str]) -> str:
    """An example's name as the operator is shown it: masked, THEN cut.

    The name is builder-written version content. Redact before the cut, the
    order every other slice here keeps (P5 review r1, J4): a cut first can leave
    a registered value's head too short for the trailing-fragment guard.
    """
    return _mask_known_secrets(name, secrets)[:_EXAMPLE_NAME_MAX_CHARS]


@dataclass(frozen=True, slots=True)
class ExampleResult:
    """How one example went: a run outcome, or how it was never started.

    ``skipped``, ``not_run`` or ``stopped`` -- see ``EXAMPLE_SKIPPED``.
    """

    name: str
    outcome: str
    run_id: int | None


# An example not started because an earlier one timed out (the API is most
# likely down for all of them), because the budget ran out, or because an
# earlier one was refused -- the tool was switched off or replaced mid-run, so
# every call after it would be refused too (P5 review r1, J3).
EXAMPLE_SKIPPED = "skipped"
EXAMPLE_NOT_RUN = "not_run"
EXAMPLE_STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One run of a tool's examples.

    ``status`` is ``ran`` when the examples were run (however they went), else
    why nothing ran: ``no_examples``, ``examples_invalid`` (``message`` says
    how), ``disabled`` (the execution path refuses a disabled tool, so it is not
    verified), ``invalid`` (the package does not validate), ``not_found``, or
    ``version_changed`` (``current`` moved off the version to check).
    """

    status: str
    vid: str | None = None
    results: tuple[ExampleResult, ...] = ()
    total: int = 0
    budget_exhausted: bool = False
    message: str | None = None

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.outcome == RUN_OK)

    @property
    def failed(self) -> int:
        """Examples the TOOL failed; a refusal or a backend error is not one."""
        return sum(1 for result in self.results if result.outcome in RUN_FAILURE_OUTCOMES)

    @property
    def stopped(self) -> str | None:
        """Why the run ended short of the list, if not by a failure: see ToolCheck.

        Interrupted by a refusal wherever it fell -- the last example refused
        leaves no ``stopped`` row behind it, yet the check was cut all the same.
        """
        if self.budget_exhausted:
            return "budget"
        if any(result.outcome in (RUN_REFUSED, EXAMPLE_STOPPED) for result in self.results):
            return "interrupted"
        return None


def checkable_tool(name: str) -> tuple[Resolved, LlmTool] | str:
    """The tool ``name`` exactly as an AI request would be handed it, or why not.

    One package scanned and built by the same functions ``enabled_llm_tools``
    uses -- so a check runs through the same identity checks, redaction and
    records as a conversation's call (invariant P). Call it with the request
    tools lock bound: the handler captures it.
    """
    package_root = _resolve_package_dir_no_alias(name)
    if package_root is None:
        return "not_found"
    scan = scan_installed(package_root)
    if not scan.valid or not isinstance(scan.resolution, Resolved):
        return "invalid"
    if not scan.enabled:
        return "disabled"
    return scan.resolution, _build_llm_tool(scan)


async def run_examples(
    name: str,
    *,
    check_id: str,
    workflow: str,
    budget_seconds: float,
    expected_vid: str | None = None,
    on_progress: Callable[[CheckResult], None] | None = None,
) -> CheckResult:
    """Run the current version's examples through the normal tool path (web-v6 §10).

    Takes the shared tools lock for the whole run, as an AI request does: a
    delete or discard meanwhile is refused (invariant T), and the budget is the
    bound on that wait. Each example is one handler call -- identity recheck,
    redaction, a run record carrying ``workflow`` and ``check_id``. Passing is
    exit 0 within the tool's own timeout; output is not compared, the API is
    real. After the first timeout the rest are skipped (one API down is usually
    all of them down), and once ``budget_seconds`` is spent the rest are not run
    (invariant S). ``on_progress`` sees the result after every example.
    """
    fd = await acquire_shared_tools_lock_async()
    if fd is None:
        return CheckResult(status="not_found")
    try:
        with bind_request_tools_lock(fd):
            found = checkable_tool(name)
            if isinstance(found, str):
                return CheckResult(status=found)
            resolution, tool = found
            if expected_vid is not None and resolution.vid != expected_vid:
                return CheckResult(status="version_changed", vid=resolution.vid)
            loaded = await run_in_threadpool(load_examples, resolution.version_root)
            if loaded.error is not None:
                return CheckResult(
                    status="examples_invalid", vid=resolution.vid, message=loaded.error
                )
            if not loaded.examples:
                return CheckResult(status="no_examples", vid=resolution.vid)
            result = await _run_loaded_examples(
                tool,
                resolution.vid,
                loaded.examples,
                CheckContext(check_id=check_id, workflow=workflow),
                budget_seconds,
                on_progress,
            )
            # Under the same lock as the run records: a delete, which clears
            # the tool's verdicts with them, cannot land in between.
            await _write_check(name, check_id, workflow, result)
            return result
    finally:
        release_tools_lock(fd)


async def _write_check(name: str, check_id: str, workflow: str, result: CheckResult) -> None:
    """Hand a check's verdict to the recorder; never raise (like ``_write_record``)."""
    recorder = _check_recorder
    if recorder is None or result.vid is None:
        return
    record = ToolCheckRecord(
        check_id=check_id,
        tool_name=name,
        vid=result.vid,
        workflow=workflow,
        finished_at=datetime.now(UTC),
        total=result.total,
        passed=result.passed,
        failed=result.failed,
        stopped=result.stopped,
    )
    try:
        await run_in_threadpool(recorder, record)
    except Exception as exc:
        _logger.warning("Tool check verdict not written: %s", type(exc).__name__)


async def _run_loaded_examples(
    tool: LlmTool,
    vid: str,
    examples: tuple[ToolExample, ...],
    context: CheckContext,
    budget_seconds: float,
    on_progress: Callable[[CheckResult], None] | None,
) -> CheckResult:
    started = time.monotonic()
    results: list[ExampleResult] = []
    stop_reason: str | None = None
    budget_exhausted = False

    def _snapshot() -> CheckResult:
        return CheckResult(
            status="ran",
            vid=vid,
            results=tuple(results),
            total=len(examples),
            budget_exhausted=budget_exhausted,
        )

    token = _CHECK_CONTEXT.set(context)
    try:
        for example in examples:
            if stop_reason is None and time.monotonic() - started >= budget_seconds:
                stop_reason = EXAMPLE_NOT_RUN
                budget_exhausted = True
            if stop_reason is not None:
                results.append(ExampleResult(example.name, stop_reason, None))
                continue
            before = len(context.runs)
            # The handler records even a call the backend fails (``error``), then
            # raises; the check reports that outcome and moves on.
            with contextlib.suppress(Exception):
                await tool.handler(example.args)
            run_id, outcome = context.runs[-1] if len(context.runs) > before else (None, RUN_ERROR)
            results.append(ExampleResult(example.name, outcome, run_id))
            if outcome == RUN_TIMEOUT:
                stop_reason = EXAMPLE_SKIPPED
            elif outcome == RUN_REFUSED:
                stop_reason = EXAMPLE_STOPPED
            if on_progress is not None:
                on_progress(_snapshot())
    finally:
        _CHECK_CONTEXT.reset(token)
    return _snapshot()


# --- mutation (name-validated + containment-checked) -----------------------


def _resolve_package_dir_no_alias(name: str) -> PackageRoot | None:
    """The package directory ``<tools_dir>/<name>`` itself, never one reached through a link.

    Every route and service that addresses a package BY NAME goes through here,
    so that none acts on another package through an alias: an internal link
    ``tools/alias -> tools/real`` resolves inside the tools root and would hand
    ``real`` to whatever the caller does next -- a discard by the alias's name
    removing ``real``'s draft or retiring its current version, a regenerate
    rewriting its summary. ``_delete_tool_locked`` alone looks at the name
    itself first, with a strict ``lstat``, and then asks here.

    In order: the name pattern and the feature being on; ``is_symlink`` on
    ``<tools_dir>/<name>``, which sees an alias without following it; the resolve
    of that path; the check on what the resolve gave, which must be exactly
    ``<tools_dir>/<name>`` with the base as ``tools_dir`` spelled it, not resolved
    again; and ``is_dir``, something installed there. The check on the result is
    what refuses an alias: ``is_symlink`` answers "not a link" when its look
    fails, while a resolve that follows the link gives another path -- the
    target, wherever it is, a directory of the same name under another parent in
    the root included. The check makes no query of its own, so it has no failure
    to misread, and a path equal to ``<tools_dir>/<name>`` is inside the root by
    construction. It also refuses a base from ``tools_dir``'s ``ENOTDIR`` branch,
    a configured path that runs through a file with its ``..`` kept: the resolve
    folds every ``..`` and lands in a directory the configured path cannot
    reach. The base is taken once, so the resolve and the check work from one
    root even if ``tools_dir`` would answer with another a moment later.

    A failed ``is_symlink`` look does not refuse a real package -- it stays a
    pre-check rather than a strict ``lstat``, and a resolve that cannot look at a
    component keeps it as written -- but a failed ``is_dir`` look gives None.
    The record purges do not read its None: they look at the name themselves
    (``tool_runs._package_seen_gone``).

    What it does not cover: an alias whose looks all fail, ``is_symlink``'s and
    the resolve's, is handed out as the alias path itself, never its target. What
    acts on a package looks at its directory without following a link and
    refuses it (``resolve_current``, ``tool_drafts._own_meta``), or looks before
    it asks here (``_delete_tool_locked``'s ``lstat``, which goes no further when
    that look fails or sees a link); the settings and the run records' version
    state are read through it. Nor does it cover a link put in place meanwhile:
    after this answer, or during it in the root's own path, which gives None.
    """
    base = tools_dir()
    if base is None or not _NAME_RE.match(name):
        return None
    package = base / name
    if package.is_symlink():
        return None
    directory = package.resolve()
    if directory != package or not directory.is_dir():
        return None
    return PackageRoot(directory)


def set_enabled(name: str, enabled: bool) -> bool:
    """Atomically update package-layer ``state.json`` for one real package.

    The manifest and every version directory remain untouched. FOREIGN state is
    never overwritten; unsafe names, aliases, missing packages, and failed writes
    return False.
    """
    base = tools_dir()
    if base is None or not _NAME_RE.match(name):
        return False
    # The lookup refuses a link at the name, wherever it points: its own look
    # sees the link, and where that look fails, what a resolve that follows the
    # link gives is not exactly ``<tools_dir>/<name>``. An alias whose looks all
    # fail, ``is_symlink``'s and the resolve's, is handed back as the alias path
    # itself, never its target, and ``resolve_current`` below refuses it: its
    # ``lstat`` of the package directory takes neither a failed look nor a link
    # for a package directory. So the toggle needs no strict ``lstat`` of its
    # own the way deletion does: deletion must be able to remove a package that
    # ``resolve_current`` refuses, and this toggle refuses one.
    package_root = _resolve_package_dir_no_alias(name)
    if package_root is None:
        return False
    # The one write is atomic: a torn
    # state file is an UNREADABLE one, and an unreadable one takes the package out
    # of the registry (see ``_read_enabled_state``), so publishing this by
    # truncate-then-write would make a failed toggle strictly worse than no toggle.
    # P2 intentionally has no state-publication lock: revise now publishes only
    # ``versions/<vid>`` plus ``current``, so version publication never replaces
    # this package-layer state. The old lock had lost its second participant.
    #
    # Recheck containment immediately before publication. The publisher refuses
    # a symlink only at its final filename; this also protects its ancestors.
    if _resolve_package_dir_no_alias(name) != package_root:
        return False
    # An unresolved package has no version whose row this toggle could describe.
    # Its sole recovery action is whole-package deletion; allowing PATCH here
    # would mutate hidden state behind a row whose current_vid is null and whose
    # version-specific actions must all refuse.
    if isinstance(resolve_current(package_root), Unresolved):
        return False
    # NOT OURS, NOT OVERWRITTEN (P1R5-1). One read, on the line above the publish:
    # what remains after it is the publisher's own lstat/mkstemp/replace. Only a
    # FOREIGN file stops the write -- an unreadable one is read as ours and
    # publishing over it is the documented repair, and an absent one is ordinary.
    if _read_enabled_state(package_root).notice is not None:
        return False
    return write_package_state(package_root, enabled)


def _seen_gone(path: Path) -> bool:
    """Whether ``path`` is seen not to exist: its ``lstat`` says so, and nothing else.

    An ``lstat`` that fails any other way -- a directory above it denies search,
    say -- says nothing about it, so it is not gone as far as anyone can tell.
    """
    try:
        os.lstat(path)
    except FileNotFoundError, NotADirectoryError:
        return True
    except OSError:
        return False
    return False


type ToolRemovalOutcome = Literal["removed", "retained"]
type ToolRetentionReason = Literal["durability_unconfirmed", "cleanup_failed"]


@dataclass(frozen=True, slots=True)
class ToolRemovalResult:
    """The observable filesystem result after a tool or version leaves current use."""

    outcome: ToolRemovalOutcome
    retained_path: Path | None
    retention_reason: ToolRetentionReason | None = None


type DiscardOutcome = (
    ToolRemovalResult
    | Literal["not_found", "version_mismatch", "lineage_unavailable", "ai_job_in_progress"]
)


def discard_version(package_root: PackageRoot, expected_vid: str) -> DiscardOutcome:
    """Discard exactly the expected current version, preserving the safe order.

    The caller owns the global single-flight reservation, while this boundary
    owns the filesystem lock. ``current`` must be resolved and compared only
    after that lock is acquired: otherwise a request can validate V, wait for a
    running tool, then overwrite a D21 edit that moved ``current`` to Q.
    """

    with exclusive_tools_lock(package_root.path.parent) as acquired:
        if not acquired:
            return "ai_job_in_progress"
        return _discard_version_locked(package_root, expected_vid)


def _discard_version_locked(package_root: PackageRoot, expected_vid: str) -> DiscardOutcome:
    """Resolve and discard after the exclusive tools lock has been acquired."""

    resolution = resolve_current(package_root)
    if isinstance(resolution, Unresolved):
        return "not_found"
    # This comparison belongs under the same lock as publication and removal.
    # A lock taken only after a route-level comparison would serialize the write
    # but leave its authorization stale, allowing old V -> P lineage to overwrite
    # a newer ``current`` and then remove a version that is no longer current.
    if resolution.vid != expected_vid:
        return "version_mismatch"
    current = resolution.version_root
    previous = resolution.previous

    # A version-addressed DELETE is never an entrance to whole-package deletion.
    # Null may have appeared after the UI rendered a light "go back" confirmation,
    # and missing is an incomplete marker rather than null; both must return the
    # same actionable conflict before any package-layer file can be touched.
    if not isinstance(previous, PreviousValue):
        return "lineage_unavailable"
    previous_vid = previous.value

    # The previous pointer is operator-editable provenance.  Refuse a self-loop
    # and apply the exact target validator used by current before touching either
    # the pointer or a version directory.
    if previous_vid == resolution.vid:
        return "lineage_unavailable"
    target = _resolve_version_target(package_root, previous_vid)
    if target is None:
        return "lineage_unavailable"
    assert isinstance(previous_vid, str)

    # Everything above was resolved under the exclusive lock, which excludes every
    # OTHER backend operation, and the whole discard runs in well under a second.
    # What it does NOT exclude is the operator's own editor, and rounds 21-23 spent
    # three attempts trying to: each guard closed one window and revealed a smaller
    # one inside it, because the state to compare lives across several files and
    # POSIX has no rename conditioned on more than one of them.  The operator ruled
    # that class out of scope (裁決 #15): hand-editing a version pointer DURING the
    # sub-second window of one's own discard is not a sequence this single user
    # produces.  A stale REQUEST is a different matter and is still refused above,
    # against ``expected_vid``, under the lock.
    publication = _publish_discard_and_retire(package_root, previous_vid, current)
    if not publication:
        return "not_found"

    # Discard is complete at publication. Everything below is best-effort
    # cleanup, but destructive cleanup is forbidden unless the directory fsync
    # confirmed that the new pointer survives a crash.
    #
    # The retention reason is part of the API: this path must NOT be presented as
    # manually cleanable, because a power loss may restore ``current`` to this
    # version until the parent directory entry is known durable.
    if not publication.durable:
        return ToolRemovalResult("retained", current.path, "durability_unconfirmed")

    # The old execution judgement/park-because-running branch is gone. The
    # exclusive flock proves no request or orphaned child holds the shared lock,
    # independent of package/version moves. This rename remains only to put a
    # cleanup failure in the sweep's inert ``.discarded`` namespace.
    parked = current.path.with_name(f"{resolution.vid}.discarded")
    try:
        os.rename(current.path, parked)
    except Exception:
        return ToolRemovalResult("retained", current.path, "cleanup_failed")

    with contextlib.suppress(Exception):
        shutil.rmtree(parked)
    if not _seen_gone(parked):
        return ToolRemovalResult("retained", parked, "cleanup_failed")
    return ToolRemovalResult("removed", None, None)


type DeleteToolOutcome = ToolRemovalOutcome
type DeleteToolResult = ToolRemovalResult | Literal["ai_job_in_progress"]


def delete_tool(name: str) -> DeleteToolResult | None:
    """Delete a package (``rmtree``), or unlink an alias; return its outcome.

    Name-validated, then one strict ``lstat`` of its own, then
    ``_resolve_package_dir_no_alias`` (the traversal hard-block is what makes an
    ``rmtree`` here safe), so it can only ever remove a directory that sits
    exactly at ``<tools_dir>/<name>``. None when the name is unsafe, the tools
    directory is not seen as a directory (not there, or a look at it that fails),
    the package is absent, the look at this name fails, what the name resolves
    to is not the package directory itself, or it could not be taken out of the
    registry at all. The tools directory is looked at before any lock is tried:
    ``exclusive_tools_lock`` answers False for a root that is not a directory,
    which would read as an AI job in progress.

    Destruction occurs only under the non-blocking exclusive tools lock. An
    ordinary request holds the shared side from advertisement through response,
    and its subprocess inherits the same descriptor, so package moves cannot
    make running work invisible. Contention returns ``ai_job_in_progress`` and
    leaves the package untouched.

    Rename-before-rmtree is retained for atomic registry disappearance and
    retryable cleanup failure, not as an execution-safety mechanism. A failed
    rmtree remains hidden under the stale namespace for the later exclusive
    sweep; no local generation/provenance judgement participates.
    """
    base = tools_dir()
    if base is None or not _NAME_RE.match(name) or not base.is_dir():
        return None
    with exclusive_tools_lock(base) as acquired:
        if not acquired:
            return "ai_job_in_progress"
        return _delete_tool_locked(base, name)


def _delete_tool_locked(base: Path, name: str) -> ToolRemovalResult | None:
    """Delete one validated name while the exclusive tools lock is held."""

    # Two guards stand between the name and the ``rmtree`` below.
    #
    # First, one ``lstat`` that raises decides what the name is: a link, a
    # directory, or anything else, which is refused. A look that fails cannot
    # tell a link from a directory, so it touches nothing, and the answer is
    # None, as for a missing package. Taken for "not a link" -- what
    # ``is_symlink`` answers when its look fails -- an alias
    # ``tools/<name> -> tools/real`` would go on as a package directory, and
    # what becomes of it would be left to later looks that can fail the same
    # way: the lookup below refuses ``real``, where a resolve that follows the
    # alias lands, but when its looks fail too, ``is_symlink``'s and the
    # resolve's, it hands back the link itself, to be renamed in place of a
    # package and reported retained.
    candidate = base / name
    try:
        info = os.lstat(candidate)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        # ``unlink`` removes only the link: what it points at, inside the root
        # or out, is never touched. A link to a directory is listed as an
        # invalid row and the UI offers 刪除 on it, so this removes what the
        # operator saw and reports removed.
        try:
            candidate.unlink()
        except OSError:
            return None
        return ToolRemovalResult("removed", None, None)
    if not stat.S_ISDIR(info.st_mode):
        return None
    # Second, the package lookup hands back only ``<tools_dir>/<name>`` itself,
    # never a path reached through a link: what its resolve gave must be exactly
    # that path. The look above sees only its own moment, so this is what keeps
    # a directory replaced by a link after it from handing this deletion the
    # package the link points at.
    package_root = _resolve_package_dir_no_alias(name)
    if package_root is None:
        return None
    directory = package_root.path
    # ``directory.parent`` rather than ``base``: the lookup's check proved the
    # package is exactly ``<tools_dir>/<name>``, so its parent is the tools root
    # -- and taking it FROM the path being renamed is what makes "the new name
    # lands in the same directory" true by construction rather than by
    # re-deriving it. A rename inside one directory is also the only shape that
    # cannot cross a filesystem, and the only one that is atomic.
    stale = _stale_backup_path(directory.parent, name, uuid4().hex)
    try:
        os.rename(directory, stale)
    except OSError:
        # The package is untouched and still listed, so this is the honest "did
        # not happen" -- the same answer a failed removal gave before.
        return None
    # Best-effort from here: the tool is already gone as far as every registry
    # reader is concerned. Removed only once the stale path is seen gone; what is
    # not -- partial removal, or remains a look cannot see -- is reported as
    # retained with its path and retried by the exclusive stale sweep.
    shutil.rmtree(stale, ignore_errors=True)
    if not _seen_gone(stale):
        return ToolRemovalResult("retained", stale, "cleanup_failed")
    return ToolRemovalResult("removed", None, None)
