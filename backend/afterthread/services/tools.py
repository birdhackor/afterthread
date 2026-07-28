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

import contextlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import weakref
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal
from uuid import uuid4

from dotenv import dotenv_values
from starlette.concurrency import run_in_threadpool

from afterthread.config import get_settings
from afterthread.services import llm_log
from afterthread.services.llm import LlmTool

# A package (and directory) name: lowercase alnum start, then up to 63 more of
# alnum/underscore/hyphen. Admits no ".", "/", or whitespace, so a traversal
# name (``".."``, ``"../evil"``, ``"/etc/x"``) can never match -- the regex is
# the first line of the path-traversal defense, backed by the resolved-path
# containment check in ``_resolve_package_dir``.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_VID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
_META_DIRNAME = ".afterthread.meta"
_VERSIONS_DIRNAME = "versions"
_CURRENT_FILENAME = "current"
_PACKAGE_STATE_FILENAME = "state.json"
_ORIGIN_FILENAME = "origin.json"
_SUMMARY_FILENAME = "summary.json"
_CURRENT_MAX_BYTES = 64

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
# namespace as a recursive or version-root namespace makes the next revise delete
# ordinary tool content.
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

# How long the bounded reader is given to drain pipes and reap AFTER the process
# group has already been SIGKILLed on timeout. The group is dead, so this
# returns effectively immediately; it exists only so a wedged pipe can never
# turn cleanup itself into a hang.
_REAP_TIMEOUT_SECONDS = 5.0

# Poll interval for the NON-reaping wait that watches the tool leader for exit
# (see _communicate_bounded). We cannot block in ``proc.wait`` there: reaping the
# leader frees its pid and lets ``os.killpg(proc.pid)`` race a REUSED group, so we
# instead poll ``os.waitid`` with WNOWAIT (detect exit WITHOUT reaping) under our
# own deadline. The only cost of polling over blocking is up to ONE interval of
# added latency detecting a natural exit; 25 ms is imperceptible for a local tool
# call yet keeps the poll loop's CPU wake-ups negligible.
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
    """
    raw = get_settings().tools_dir.strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _valid_entry(entry: Any) -> bool:
    """True when ``entry`` is a non-empty argv list of non-empty strings."""
    return (
        isinstance(entry, list)
        and len(entry) > 0
        and all(isinstance(part, str) and part for part in entry)
    )


def _is_within(base: Path, candidate: Path) -> bool:
    """True when the RESOLVED ``candidate`` lies at or under the RESOLVED ``base``.

    ``relative_to`` raises ValueError when it does not -- the containment test
    used both to validate an entry file (it must live inside its package) and to
    hard-block a package path that escapes ``tools_dir`` (a symlink, a traversal
    name that somehow reached here).
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
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        # Missing (ENOENT), a symlinked leaf O_NOFOLLOW refused (ELOOP), a FIFO
        # with no writer on some platforms, a permission error, ... -- every open
        # failure degrades to None rather than raising into the caller.
        return None
    fd_owned = True  # we own the raw fd until fdopen takes it over
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        handle = os.fdopen(fd, encoding="utf-8", errors="replace")
        fd_owned = False  # fdopen now owns fd; closing the handle closes it
        with handle:
            return handle.read(cap + 1)
    except OSError:
        return None
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


def _scan_tool_content(root: BuildRoot | VersionRoot, expected_name: str) -> _ContentScan:
    """One implementation of manifest, entry, containment, and content rules."""

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


def _find_embedded_secret_file(build_root: BuildRoot) -> str | None:
    """Relative path of the first staged file that embeds a known secret VALUE, or None.

    The install-only H3 gate. A builder session has real shell capability (run_shell,
    D21), so it could read another tool's ``.env`` -- or any file the service uid can --
    and bake a live key into the package it is producing. The manifest is the
    persistence-and-broadcast vector (served via /api/tools and re-sent in EVERY future
    LLM tool spec), and every implementation file is a persisted plaintext copy, so
    before promotion EVERY regular file is scanned for any known secret value (>=
    _MIN_SECRET_LEN, the redactor floor). A hit REJECTS the install (the caller names the
    offending relative path) rather than redacting: an embedded key is malformed by
    design, and masking would silently alter the schema the model built. The in-flight
    install secret is registered for the whole build (run_install), so it is covered here
    too -- and validate runs BEFORE promote writes the secret into the package ``.env``,
    so this never trips on the backend's own later injection.

    Each file is read through the ONE bounded, FIFO/symlink-hardened helper (cap =
    _MANIFEST_MAX_BYTES); a non-regular/unreadable file comes back None and is skipped.
    ``os.walk`` does not follow directory symlinks and the read's O_NOFOLLOW refuses a
    symlinked leaf, so the scan can never be walked out of the package. Files are visited
    in a deterministic sorted order so the named offender is stable. Cost is one substring
    pass per known secret per file -- fine for the few small files a package holds.
    """
    secrets = [value for value in known_secret_values() if len(value) >= _MIN_SECRET_LEN]
    if not secrets:
        return None
    base = build_root.path.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        filenames.sort()
        here = Path(dirpath)
        for filename in filenames:
            path = here / filename
            text = _read_regular_file_capped(path, _MANIFEST_MAX_BYTES)
            if text is None:
                continue
            if any(secret in text for secret in secrets):
                return str(path.relative_to(base))
    return None


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
    (whose ``.env`` legitimately holds the injected secret post-install).
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
    # Install-only embedded-secret gate (H3): reject a package that baked a known secret
    # value into any file, naming the offending relative path (never the value itself).
    # The path is itself run through the redactor before it lands in the message, in case
    # a builder wrote a file whose very NAME embeds an expanded ``$SECRET`` -- so the
    # rejection can never echo the value even via the path.
    offender = _find_embedded_secret_file(build_root)
    if offender is not None:
        return f"{redact_known_secrets(offender)} 不得包含秘密值（請改由環境變數讀取）"  # noqa: RUF001
    return None


# --- AI summary sidecar (D40) ------------------------------------------------


def _utf8_safe(text: str) -> str:
    """Replace any lone (unpaired) Unicode surrogate in ``text`` with U+FFFD.

    A three-line duplicate of ``llm_log._utf8_safe``, which is the CANONICAL
    twin -- read its docstring for why ``surrogatepass``-encode + ``replace``-
    decode is the only pairing that actually yields U+FFFD. Duplicated rather
    than imported for the SAME leaf/layering reason ``_REDACTION_MARKER`` is
    (llm_log is a leaf observability module that must not import this capability
    module, and the reverse edge would create exactly the coupling both modules'
    docstrings forbid); ``tests/test_tools.py`` pins the two behaviorally equal
    on a probe set so they cannot silently drift.

    Why version metadata needs it at all: ``origin.json`` and ``summary.json``
    are plain JSON files the operator is explicitly allowed to hand-edit, and
    ``"\\ud800"`` is a JSON-LEGAL escape that ``json.loads`` accepts happily --
    producing a ``str`` that is NOT UTF-8 encodable. Left alone it breaks both
    boundaries: on the WRITE side ``json.dumps(...).encode("utf-8")`` raises
    ``UnicodeEncodeError`` (a 500 out of a PATCH that should have answered False
    -> 404), and on the READ side it sails into the summary response and blows
    up in Starlette's strict ``JSONResponse.render`` encode -- a 500 on a GET
    that exists to DEGRADE corrupt metadata, not to die on it.
    """
    return text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")


def _utf8_safe_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Every string a sidecar read hands back, surrogate-scrubbed. Depth-bounded.

    Applied to the parsed sidecar on the way OUT of ``read_tool_meta``, because a
    hand-edited file bypasses our writer entirely: the write side can scrub what
    IT produces, but only this covers a surrogate an operator typed straight into
    the file. Without it a ``"\\ud800"`` in ``summary`` or ``updated_at`` reaches
    ``ToolSummaryDetail`` and 500s the GET at response-encode time (see
    ``_utf8_safe``).

    Bounded at DEPTH 2 -- top-level values plus one level inside a dict value --
    on purpose, not for lack of ambition. That is the entire sidecar schema (four
    scalars plus ``origin``'s two strings), it covers every field any consumer
    reads, and a general recursive walk would re-open precisely the hazard
    ``read_tool_meta``'s ``RecursionError`` guard exists to close: a hand-edited
    file that PARSES (json.loads recursing in C, on the C stack) can still be
    nested far deeper than a Python-frame recursion can follow, which would turn
    a corrupt sidecar back into a 500 for the whole 工具 page.

    KEYS are deliberately left alone. A key carrying a surrogate can never equal
    one of the five schema names, so it is dropped by every consumer AND by the
    next ``write_tool_meta`` (which builds the file from its own literals) -- it
    can therefore never reach a response or a re-serialization.
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
    """One sidecar string field, narrowed: a ``str`` survives, anything else is None.

    Used only on the ``origin`` sub-fields, whose absence is meaningful (the
    sidecar is the ONLY copy of the install's URL/instructions, and "we do not
    have one" has to be representable). ``summary`` deliberately does NOT go
    through here -- see ``write_tool_meta`` for why a non-string summary refuses
    the write instead of degrading to None.
    """
    return value if isinstance(value, str) else None


def _redacted(value: str | None) -> str | None:
    """One sidecar string VALUE, masked THEN surrogate-scrubbed; None stays None.

    The whole redaction surface of the sidecar, now that ``write_tool_meta``
    builds the file from a fixed schema instead of serializing a caller's dict:
    exactly three string VALUES can carry operator/LLM text, and this is applied
    to each of them by name.

    Redact BEFORE the scrub, the same order ``llm_log._stored_body`` runs its two
    passes in: the redactor must match the REGISTERED value against untouched
    text, and the scrub must not be undone afterwards (it only ever replaces a
    lone surrogate with U+FFFD, which carries nothing to mask).

    The scrub is what keeps the write path's own encode from raising: an LLM
    reply or a hand-edited field can carry a lone surrogate, which is a legal
    ``str`` that ``.encode("utf-8")`` refuses. Scrubbing rather than REFUSING is
    the adjudicated choice -- one bad code point is a display-level defect in a
    summary that is otherwise a genuine, useful explanation, and refusing would
    lose the whole generation over it (and, on the install hook's placeholder
    path, leave the operator with no sidecar and no 重新產生 button at all).
    U+FFFD is exactly what the reader would show anyway, so scrubbing makes the
    file agree with the render. The read side scrubs INDEPENDENTLY regardless
    (see ``_utf8_safe_meta``), because a hand-edited file never passes here.

    Raises whatever ``redact_known_secrets`` raises: the LIVE redactor
    deliberately propagates a provider failure rather than degrading to
    unmasked, and ``write_tool_meta`` turns that into its fail-closed refusal.
    """
    return None if value is None else _utf8_safe(redact_known_secrets(value))


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
    "same PACKAGE?"; the other identity (``directory_identity``) answers "same
    FILES?" and is not interchangeable -- see both functions.
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
    last instant, replaces the target, and fsyncs the directory. Its bool contract
    is shared by state, current, origin, and summary callers and reports whether
    publication happened; the optional one-item out parameter exists solely so
    ``publish_current`` can expose confirmed directory durability to discard.
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

    This does NOT serialize ``meta``. It reads the five fields it understands out
    of ``meta``, coerces each to the shape the sidecar's contract promises, and
    writes THAT -- so every KEY on disk is a literal from this function and every
    VALUE is one this function chose or refused:

    * ``summary`` -- a ``str``; None becomes ``""`` (the placeholder a failed
      generation leaves, and the shape a hand-edited ``"summary": null`` should
      collapse to rather than sneaking past as "not a string, but not refused
      either"). Any OTHER type refuses the write: ``summary`` is a string by
      contract, the read path renders anything else as "no summary", and writing
      one would make the file lie about whether a summary exists;
    * ``updated_at`` -- a ``str``, else the write is REFUSED. Every caller stamps
      its own (``_now_iso`` / ``datetime.now``), so a missing or out-of-shape one
      is a caller bug, and inventing a timestamp on their behalf would put a
      fact in the file that nothing actually observed;
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
    * ``origin`` -- None, or the two fields we understand narrowed to ``str``/
      None. It is the install's only record of where the package came from, so
      it is kept; it is also free operator text, so it is kept NARROW.

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
    took its final shape: ``tool_builder._revise_copy_ignore`` withholds the
    sidecar's reserved namespace at EVERY depth, and ``_strip_builder_sidecars``
    deletes any sidecar found in staging BEFORE validation runs, so no sidecar
    ever reaches that gate. The correction matters rather than being pedantic: a
    maintainer who believed a downstream gate would catch an unmasked value could
    reasonably relax the refusal below into "write it unmasked", and nothing
    downstream would catch anything.

    So a redaction failure (``known_secret_values`` raising -- the LIVE path
    deliberately propagates rather than degrading to unmasked, see
    ``redact_known_secrets``) writes NOTHING and returns False. It is applied to
    the three fields that can carry operator/LLM text and to nothing else,
    because nothing else CAN: ``llm_log_id`` is an int,
    ``llm_log_process`` is an opaque token this backend
    generated for itself, and ``updated_at`` is a machine timestamp its caller
    just produced (no on-disk one ever round-trips -- both callers overwrite it).

    The serialized payload is bounded by ``_AI_META_MAX_BYTES``, the SAME cap
    ``read_tool_meta`` refuses past, so this can never produce a file that reads
    back as None. Every other failure (an unwritable path, a symlinked/FIFO
    sidecar refused by ``_write_package_file_atomic``'s lstat gate) is False too, so
    callers get one "did-not-happen" answer and never an exception -- summary
    metadata is best-effort by design.

    The serialize + encode + size check live INSIDE the fail-closed ``try``, and
    that placement is load-bearing rather than tidy. They used to sit outside it,
    so a lone surrogate reaching any string field raised ``UnicodeEncodeError``
    straight out of this function -- turning a PATCH that should have answered
    False (-> 404) into a 500. ``_redacted`` now scrubs the three text fields
    before ``dumps`` sees them, so the surrogate case is HANDLED rather than
    merely caught; the guard still covers ``updated_at``, the one caller-supplied
    string this function passes through verbatim (every real caller stamps a
    machine timestamp, so scrubbing it would only paper over a caller bug -- a
    refusal is the honest answer there).

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
    the file. The five fields above are the sidecar.

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
    if not isinstance(updated_at, str):
        return False
    log_id = meta.get("llm_log_id")
    if not isinstance(log_id, int) or isinstance(log_id, bool):
        log_id = None
    log_process = meta.get("llm_log_process")
    if not isinstance(log_process, str):
        log_process = None
    try:
        payload: dict[str, Any] = {
            "summary": _redacted(summary),
            "updated_at": updated_at,
            "llm_log_id": log_id,
            "llm_log_process": log_process,
        }
        # ensure_ascii=False keeps CJK readable in the file (and is what the byte
        # cap below is measured against). Every value is a str/int/None we just
        # built, so dumps cannot fail on an unserializable type -- but the ENCODE
        # can still raise on a surrogate-bearing ``updated_at``, which is why both
        # steps sit inside this guard (see the docstring). The cap is checked on
        # the ENCODED length, because that is the unit the writer's half of the
        # read/write symmetry is stated in (see _AI_META_MAX_BYTES).
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
    """Publish immutable, sanitized ``origin.json`` before a version is installed."""

    source = origin.get("source")
    previous = origin.get("previous")
    if not isinstance(source, str):
        return False
    if previous is not None and (not isinstance(previous, str) or not _VID_RE.fullmatch(previous)):
        return False
    try:
        payload = {
            "source": _redacted(source),
            "openapi_url": _redacted(_meta_str(origin.get("openapi_url"))),
            "instructions": _redacted(_meta_str(origin.get("instructions"))),
            "feedback": _redacted(_meta_str(origin.get("feedback"))),
            "previous": previous,
        }
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
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
    origin: dict[str, Any] | None,
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


def list_tools() -> list[dict[str, Any]]:
    """List every installed package as a UI-facing summary.

    A broken package is included with ``valid=False`` and a safe error reason,
    but is never executable; a missing or unset tools directory yields [].
    """
    return [
        {
            "name": scan.name,
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
        for scan in _scan_all()
    ]


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


def directory_identity(version_root: VersionRoot) -> tuple[int, int] | None:
    """The DIRECTORY's own identity: ``(st_dev, st_ino)``, or None when unreadable.

    The other half of ``package_identity``, and a DIFFERENT question on purpose:
    that one asks "is this still the same PACKAGE?", this one asks "is anything
    still running out of these FILES?". One tuple cannot answer both, because the
    two must survive different things:

    * the manifest identity MUST move when ``tool.json`` is rewritten -- that is
      exactly how a revision, a reinstall, and an operator's hand-edit of the
      spec (accepted, stated in ``_make_handler``) are told apart from
      "unchanged";
    * an execution's lifetime must survive EVERY edit inside the directory, spec
      edits included. Keying ``_INFLIGHT_EXECUTIONS`` on the manifest made a
      handler that registered before such an edit invisible to the delete or
      promote that queried after it -- and the removal each of them defers for
      exactly this reason then landed on files a child was still reading. The
      split lasted from the edit until the child exited, not a syscall pair.

    The SPLIT STAYS even though the case that forced it is gone (web-v5 P1: the
    ``enabled`` toggle no longer rewrites ``tool.json``, so the commonest way to
    move the manifest identity under a running child no longer exists). Its own
    reason is enough on its own and always was: a hand-edit of ``tool.json`` is a
    SUPPORTED operator action (D21) that moves the manifest identity while a
    subprocess is still reading the directory, so a registration keyed on the
    manifest would still be stranded. Collapsing the two back into one tuple is a
    later phase's decision, once immutable versions make "same package?" answerable
    without a stat at all; doing it here would entangle two changes.

    MEASURED here rather than assumed (Linux/ext4, this repo's own filesystem):

    * an in-place rewrite of ``tool.json`` (an operator's edit; before web-v5 P1,
      also every ``set_enabled``) leaves this tuple UNCHANGED while the manifest
      identity moves (the rewrite pushes ``tool.json``'s ctime);
    * the replace-mode promote's rename-aside and ``delete_tool``'s rename into
      the deferred namespace both CARRY it -- a rename moves the name, not the
      inode, which is the same fact that makes a rename invisible to a running
      child;
    * a delete followed by a reinstall of the same name REUSES the directory
      inode (5/5 rounds measured), which is precisely why this must never be read
      as "the same package" -- ``package_identity`` records the adjudication that
      settled that question on the manifest.

    Two SHAPES, not two spellings of one tuple: 2 elements here, 3 there, so a
    call site reaching for the wrong identity is a type error rather than a
    silent mismatch.

    ``lstat``, like its sibling: an executable package directory is never a
    symlink (``_scan_package`` refuses one, so nothing symlinked can be running),
    and both deferral writers unlink or refuse a symlink long before they ask.
    None on any error, with each caller stating which way it takes "cannot say".
    """
    try:
        info = os.lstat(version_root.path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _build_llm_tool(scan: _PackageScan) -> LlmTool:
    """Turn a valid ``_PackageScan`` into an executable ``LlmTool``.

    Callers pass only ``valid`` scans, so ``parameters`` / ``entry`` are present;
    the assertions document that precondition and keep the type checker happy
    without an ``Any`` escape hatch.

    The identity handed to ``_make_handler`` is the one ``_scan_package`` took on
    the line above the manifest read (``scan.identity``), NOT a fresh ``lstat``
    taken here -- and that is the point rather than an optimization. This
    function does not run until ``_scan_all`` has returned, i.e. until EVERY
    package has been scanned, so an ``lstat`` here would be separated from the
    read it vouches for by an unbounded number of file reads: a promote landing
    in that gap pinned the NEW identity against the OLD spec, and
    every downstream guard -- this handler's own, and the one above ``Popen`` --
    then compared EQUAL and ran it. Pairing them at the read leaves a window of
    exactly one ``lstat``/``open`` pair, and leaves it on the side that REFUSES
    (see ``_scan_package`` for the measurement of both orderings).

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
# in ``known_secret_values`` below (the same leaf/provider inversion llm_log's
# own logger config uses). The inversion is about THAT direction only: this
# module imports llm_log directly (for ``process_token``, see
# ``store_summary_meta``), which is the direction that keeps llm_log a leaf.
# The set is computed at CALL time, never cached as a
# static value, because it genuinely CHANGES at runtime: an install writes a new
# tool ``.env``, and an in-flight install registers its form secret before that
# ``.env`` even exists.

# In-flight secrets: values that must stay redactable for the DURATION of one
# operation, independently of what the ``.env`` scan below can see AT THE MOMENT
# a body is recorded. Three kinds of holder register here:
#
# * a web-installer form secret (``secret_value``), for the whole install
#   (tool_builder registers/discards around ``run_install``) -- the window BEFORE
#   promote writes it into the package ``.env``, after which the per-package
#   ``.env`` scan below is what covers it;
# * a revise session's ``.env`` values, for the whole builder session, plus
#   whatever the promote re-vets on its way out;
# * the ``.env`` values ONE TOOL CALL was handed. ``_build_tool_env`` copies the
#   package's ``.env`` into the child's environment, and the child can echo any
#   of it back -- but the redaction of that output happens when the child
#   FINISHES, and by then the file may say something else entirely (an operator
#   rotating a credential, a revise publishing a new package, this module's own
#   deferred ``delete_tool``). The scan would then only know the NEW value and
#   the OLD one would ride into the role:"tool" message, the log and the JSONL
#   sink unmasked. So the values a child actually RECEIVED are held here for
#   exactly as long as that call can still produce output to mask.
#
# COUNTED rather than flagged, for the same reason ``_INFLIGHT_EXECUTIONS`` is:
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
# tagged with a FILE IDENTITY taken from its ``stat``, so ``known_secret_values``
# re-parses a package only when its ``.env`` actually changed. This matters
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
# * ``shutil.copy2`` over the existing path -- which is what the revise flow
#   itself uses to put a ``.env`` back -- changes ONLY ``st_ctime_ns``;
# * a rewrite followed by ``os.utime`` restoring the old stamps (a
#   timestamp-preserving restore, a backup rollout, ``cp -p``) changes ONLY
#   ``st_ctime_ns``, even when the new content is the same LENGTH;
# * write-to-temp + rename with the mtime carried over (``rsync -t``, and the
#   revise flow's own publish) changes ``st_ino`` and ``st_ctime_ns``;
# * ``unlink`` + recreate REUSES the inode here, so ``st_ino`` alone would not
#   have caught the case above either -- which is why ctime is in the tag and not
#   just the inode;
# * renaming the parent DIRECTORY (the last step of a revise publish) changes
#   nothing about the file, so a roll-back that puts the original package back is
#   still a cache HIT, correctly.
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
    same ``.env``) each release only their OWN hold.
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

    The mate of ``_inflight_execution`` for the OTHER thing one tool call needs
    to outlive itself, and a context manager for the same reason: the ``finally``
    is the point. A handler that raises, times out, or is cancelled mid-call must
    not leave a hold behind -- a leaked one would keep masking a value forever,
    which is not a leak that shows up anywhere until a redaction starts eating
    ordinary prose.

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


def _cached_env_values(package_root: PackageRoot) -> frozenset[str]:
    """The redactable VALUES of one package's ``.env``, cached per (path, identity).

    Reuses the ONE bounded ``.env`` loader (``_load_tool_dotenv``) rather than
    hand-rolling a second parser, so the FIFO/symlink/oversize hardening and the
    drop-bare-keys behavior are inherited unchanged. Empty values are dropped so
    a ``KEY=`` line contributes nothing (``_redact`` also guards empties, but not
    seeding them keeps the set tidy).

    The path is the cache KEY and the file's identity is the TAG (see
    ``_ENV_VALUE_CACHE`` for what is in it and for the measurements that chose
    the fields): a path answers "whose ``.env`` is this", and only the identity
    can answer "is it still the same file with the same contents". Both come out
    of the one ``stat`` this function already made.
    """
    env_file = package_root.path / ".env"
    key = str(env_file)
    try:
        info = env_file.stat()
    except OSError:
        # No ``.env`` (the common case) or an unstattable path: nothing to
        # contribute. Forget any stale cache entry so a LATER-created ``.env`` is
        # picked up fresh next time.
        with _ENV_VALUE_CACHE_LOCK:
            _ENV_VALUE_CACHE.pop(key, None)
        return frozenset()
    identity = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
    with _ENV_VALUE_CACHE_LOCK:
        cached = _ENV_VALUE_CACHE.get(key)
        if cached is not None and cached[0] == identity:
            return cached[1]
    # Parse OUTSIDE the lock (it does file I/O); the (path, identity) key makes a
    # concurrent double-parse harmless -- both produce the identical set.
    values = frozenset(value for value in _load_tool_dotenv(package_root).values() if value)
    with _ENV_VALUE_CACHE_LOCK:
        _ENV_VALUE_CACHE[key] = (identity, values)
    return values


def known_secret_values() -> frozenset[str]:
    """Every value llm_log should redact out of stored prompt/response bodies (D36).

    The union of three sources:

    * our own ``openai_api_key`` when set -- belt-and-braces: the secret-free
      invariant already keeps it out of records STRUCTURALLY (llm.py never reads
      it into a body), but redacting it too means even a tool that somehow echoed
      it back cannot surface it in the log;
    * every VALUE in every installed tool package's ``.env`` (a KB API key etc.),
      cached per (path, file identity) so a call per record stays cheap;
    * every value currently HELD in-flight (see ``_INFLIGHT_SECRETS``) -- an
      install's form secret before promote writes it, a revise session's values,
      and the values a running tool call handed its child. Each covers a window
      the directory scan above cannot answer for, because the file it reads is
      whatever is on disk NOW rather than what the holder was given.

    Wired as llm_log's secret provider by main. llm_log calls this INSIDE its own
    ``except Exception`` guard (the no-observer-failure invariant), so a hiccup
    here only ever degrades to "record unredacted", never breaks a recording.
    """
    secrets: set[str] = set()
    api_key = get_settings().openai_api_key.strip()
    if api_key:
        secrets.add(api_key)
    base = tools_dir()
    if base is not None and base.is_dir():
        for child in sorted(base.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                secrets |= _cached_env_values(PackageRoot(child))
    with _INFLIGHT_LOCK:
        secrets |= set(_INFLIGHT_SECRETS)
    return frozenset(secrets)


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
# secret is always long enough to be redactable here.
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
    and one masked in the log are indistinguishable. ``secrets`` are the known values
    already filtered to ``>= _MIN_SECRET_LEN`` -- each caller produces that list under
    its OWN failure direction (tools lets a provider error propagate/fail-closed; llm_log
    swallows one and records unredacted), then hands the masking here.

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
    tool_builder and the tool-args preview in llm.py all redact BEFORE they cut); (2)
    merge overlapping/adjacent ranges; (3) build the output in ONE pass, emitting exactly
    one marker per merged range.

    Collecting every occurrence in step (1) is BOUNDED two ways (D36 round-5), because a
    legitimate secret can itself be adversarial input to this search. First, the find
    cursor advances by ``len(value)`` on every hit rather than by 1, so overlapping
    occurrences of the SAME repeated secret text -- an ``"aaaaaa"``-style secret against
    a same-character document, the reported vector once an un-truncated OpenAPI body
    happens to look like that -- step past each other instead of registering one range
    per shifted start. This can still leave a residual of up to ``len(value) - 1``
    characters at a repeated run's tail (a strict remainder of the run length modulo
    ``len(value)``); at the ``_MIN_SECRET_LEN`` floor -- the shortest value ever reaches
    here, and exactly the reported case -- that residual is itself under the floor,
    exactly as unredactable as any other short substring by the SAME rationale that
    floor already encodes, and a residual landing at the very END of ``text`` is still
    separately caught by the trailing-fragment guard above regardless of length. Second,
    stepping is not sufficient by itself: a sufficiently large low-entropy document can
    still cross ``_MAX_MASK_RANGES`` one ``len(value)``-sized step at a time, so
    collection STOPS the instant it would and this returns the marker ALONE for the
    entire text instead (see that constant) -- over-redaction is always safe, so the cap
    turns a pathological allocation into a fixed-size O(1) result. With ``ranges`` bounded
    before it is ever sorted, the merge in step (2) is O(n log n) on that bounded n
    rather than on the size of ``text``, and step (3)'s output build remains the single
    pass it always was.
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


def redact_known_secrets(text: str) -> str:
    """Replace every known secret VALUE in ``text`` with the redaction marker (D36).

    The LIVE-conversation counterpart of ``llm_log._redact``. llm_log scrubs secrets
    out of the bodies it STORES, but a disobedient tool/builder (``echo "$KB_API_KEY"``)
    can put a secret value into a tool RESULT, which the llm loop wraps VERBATIM into
    the next round's ``role:"tool"`` message -- where the model can read it and copy it
    onward (into an install ``summary``, another tool call, ...). Masking every known
    value at each boundary where raw tool output enters the conversation/state closes
    that class: the model only ever sees the marker, never the value.

    Values come from ``known_secret_values`` (our key + every installed tool's .env
    values + the in-flight install secret); those at least ``_MIN_SECRET_LEN`` long are
    handed to the shared ``_mask_known_secrets`` (byte-identical to ``llm_log``'s copy),
    which computes every mask range on the PRISTINE text and merges them -- see that
    helper for why the range-based order is load-bearing (H1) and how the trailing
    fragment guard fits in.

    Unlike ``llm_log._redact``, this is on the LIVE path, so it does NOT swallow a
    provider failure into "return the text unmasked" -- that would leak the very value
    it exists to hide. ``known_secret_values`` is called WITHOUT a guard here, so any
    surprise it raised PROPAGATES to the caller's fail-closed handling (the llm loop's
    and meta-tools' own ``except`` degrade it into a safe failed-tool-result string),
    never a silent leak.
    """
    secrets = [value for value in known_secret_values() if len(value) >= _MIN_SECRET_LEN]
    # Bare call, deliberately unguarded (see the docstring above): a failure here must
    # PROPAGATE fail-closed, the opposite of llm_log._redact's guarded degrade-to-
    # unredacted direction (D36 round-5).
    return _mask_known_secrets(text, secrets)


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
    env_file = package_root.path / ".env"
    # Read through the ONE bounded-regular-file helper (F3a): its O_NONBLOCK+S_ISREG
    # gate refuses a FIFO an operator (or a post-install mutation) could `mkfifo` in
    # place of .env -- which dotenv_values(path) would reopen and BLOCK on forever --
    # its O_NOFOLLOW refuses a symlinked .env, and its cap+1 read bounds the per-CALL
    # memory the parse would otherwise slurp whole into the child's exec env block.
    # None (missing / FIFO / symlink / read error) and an over-cap length BOTH
    # degrade to {} -- the SAME degrade-to-no-extra-env contract the malformed-.env
    # fallback below gives -- so a runaway or mutated .env can never bloat per call.
    text = _read_regular_file_capped(env_file, _ENV_FILE_MAX_BYTES)
    if text is None or len(text) > _ENV_FILE_MAX_BYTES:
        return {}
    # Parse the ALREADY-READ text (never dotenv_values(path)): handing python-dotenv the
    # path would make it REOPEN the file -- a second, UNBOUNDED read that also re-follows a
    # symlink -- defeating the bounded, O_NOFOLLOW'd read above. ``_parse_dotenv_text`` is
    # the shared interpolate=False parser (see there); both reads must be the same bytes
    # and the same regular-file decision.
    return _parse_dotenv_text(text)


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

    Returns the env AND the (non-empty) ``.env`` VALUES that went into it, from
    the SAME read -- which is the whole point of handing them back rather than
    letting the caller re-read the file: a second read could see a rotated
    ``.env`` and register values the child never got, leaving the ones it DID get
    unmaskable. The passthrough names are deliberately NOT in that set: PATH and
    HOME are not secrets, and masking them would shred every result that mentions
    a path. The values are handed to ``_inflight_secrets`` by ``_make_handler``.
    """
    env = {name: os.environ[name] for name in _PASSTHROUGH_ENV if name in os.environ}
    if get_settings().tls_no_verify:
        env["TLS_NO_VERIFY"] = "1"
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

    ``start_new_session=True`` made the child a session/group leader, so
    ``os.getpgid(pid) == pid`` and one ``killpg`` takes down the child AND any
    grandchildren it spawned -- a tool that forked a runaway helper cannot
    survive the timeout. Falls back to killing just the child if the group
    signal cannot be delivered (already reaped, or a platform edge), and never
    raises.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError, PermissionError, OSError:
        with contextlib.suppress(Exception):
            proc.kill()


@dataclass(slots=True)
class _BoundedOutput:
    """One bounded subprocess read's result (see ``_communicate_bounded``).

    ``stdout``/``stderr`` hold at most ``cap + 1`` chars each; the ``*_overflow``
    flags say a stream ran PAST the cap (and so the process was killed for it),
    which the callers use to decide between "here is the truncated output" and
    "the tool failed". ``timed_out`` is the wall-clock expiry, kept distinct so
    a caller can render the timeout message instead of an output.
    """

    stdout: str
    stderr: str
    stdout_overflow: bool
    stderr_overflow: bool
    timed_out: bool


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


def _communicate_bounded(
    proc: subprocess.Popen[str], *, input_text: str | None, cap: int, timeout: float
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
        # proc.wait() frees the leader's pid, os.getpgid(proc.pid) here could read a
        # REUSED pid's group and _kill_process_group would signal the wrong group. The
        # lock makes "kill" and "reap" mutually exclusive; the `reaped` flag makes a
        # kill attempt after teardown a no-op. While NOT reaped (teardown not begun),
        # the leader is un-reaped so proc.pid still names THIS group and the early
        # overflow kill is valid.
        with teardown_lock:
            if reaped:
                return
            _kill_process_group(proc)

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
    timed_out = False
    deadline = time.monotonic() + timeout
    while True:
        try:
            info = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except ChildProcessError:
            # Already reaped somehow (we are the only waiter, so this is defensive):
            # stop polling and fall through to the kill, which no-ops on a gone group.
            break
        if info is not None:
            break  # leader exited; the zombie is retained, pinning the PGID
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(_WAIT_POLL_SECONDS)

    # Tear the WHOLE group down and reap, atomically w.r.t. the overflow-kill
    # callback above (teardown_lock): setting `reaped` before releasing the lock
    # guarantees no _CappedReader can call getpgid/killpg on the pid we are about
    # to free below. Within the lock, the kill+reap still run UNCONDITIONALLY --
    # whether the leader exited on its own or we timed out. This is the fix for
    # two holes the old "escalate only if a reader thread is still alive"
    # teardown carried:
    #  (a) a descendant that CLOSED its inherited stdout/stderr but kept running
    #      left the readers at EOF and their threads dead, so no escalation ever
    #      fired and the detached daemon LEAKED. An unconditional kill reaps it;
    #  (b) killing AFTER ``proc.wait`` reaped the leader targeted a pid the kernel
    #      had already freed -- a REUSED-pgid hazard. Here the leader is still
    #      un-reaped (a zombie on natural exit, or alive on timeout), so ``proc.pid``
    #      provably still names THIS group (``start_new_session=True`` made the
    #      leader its own group leader, so the group id EQUALS its pid).
    # A leader that exited NATURALLY is already a dead zombie, so this SIGKILL is a
    # no-op for it and cannot disturb its recorded exit status -- ``proc.wait`` below
    # still returns the tool's REAL exit code. A descendant that double-fork/setsid'd
    # OUT of the group escapes even this and is beyond v1's non-container stance
    # (D21); the daemon flag on every reader/writer thread is the honest backstop.
    with teardown_lock:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)

        # Reap the leader now that its group is SIGKILLed: this frees the zombie
        # (releasing the pinned pid) and, on a natural exit, returns the tool's REAL
        # returncode -- preserving exit-code semantics for ordinary tools. No timeout
        # is needed: the group is dead, so a natural-exit zombie is collected at once
        # and a timed-out leader was just killed (SIGKILL is uncatchable, so it dies
        # promptly).
        with contextlib.suppress(Exception):
            proc.wait()
        reaped = True

    # The group is dead, so every inherited pipe write end is now closed and the
    # (daemon) reader/writer threads hit EOF and finish. Join them under the short
    # reap bound purely so a pathologically wedged pipe can never turn cleanup
    # itself into a hang; with the group gone this join is guaranteed to complete.
    # OUTSIDE teardown_lock (released above): a reader thread still inside `kill`,
    # blocked waiting on the lock, must be free to acquire it and no-op before this
    # join can observe that thread finished -- joining while still holding the lock
    # could deadlock against it.
    for thread in threads:
        thread.join(timeout=_REAP_TIMEOUT_SECONDS)

    return _BoundedOutput(
        stdout=stdout_reader.text if stdout_reader is not None else "",
        stderr=stderr_reader.text if stderr_reader is not None else "",
        stdout_overflow=stdout_reader.overflow if stdout_reader is not None else False,
        stderr_overflow=stderr_reader.overflow if stderr_reader is not None else False,
        timed_out=timed_out,
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


def _run_tool_subprocess(
    entry: list[str],
    advertised: Resolved,
    expected_identity: tuple[int, int, int] | None,
    advertisement_generation: _AdvertisementGeneration,
    env: dict[str, str],
    args_json: str,
    timeout: float,
    output_cap: int,
) -> str:
    """Run the advertised version and return one agent-visible result string.

    The final ordering is deliberate and compact: refuse a version retired by a
    completed discard, read the PackageRoot toggle, require that ``current`` still
    resolves to some usable version, verify the ADVERTISED VersionRoot identity,
    then call ``Popen`` with nothing between that identity check and process
    creation.  Resolving ``current`` does not compare vids: a normal revise may
    move it while this call must remain bound to the schema it was shown.
    """
    version_root = advertised.version_root
    if _advertisement_retired(advertisement_generation):
        return _TOOL_REPLACED_RESULT
    package_root = advertised.package_root
    if not package_enabled(package_root):
        return _TOOL_DISABLED_RESULT
    if isinstance(resolve_current(package_root), Unresolved):
        return _TOOL_REPLACED_RESULT
    # This identity check is deliberately LAST. No state read, lineage resolve,
    # retired-marker lookup, or other fallible operation may be inserted between
    # its answer and Popen's cwd resolution.
    if not _still_the_expected_package(version_root, expected_identity):
        return _TOOL_REPLACED_RESULT
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
        )
    except OSError as exc:
        # FileNotFoundError (bad interpreter/script), PermissionError (no exec
        # bit), ... -- the tool never ran. Category only, no raw error string.
        return f"tool failed to start: {type(exc).__name__}"

    output = _communicate_bounded(proc, input_text=args_json, cap=output_cap, timeout=timeout)
    if output.timed_out:
        return f"tool timed out after {timeout:g} seconds"
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
    if output.stdout_overflow:
        return _cap_output(redact_known_secrets(output.stdout), output_cap)
    if proc.returncode != 0:
        stderr = _cap_output(redact_known_secrets(output.stderr).strip(), output_cap)
        return f"tool failed (exit {proc.returncode}): {stderr}"
    return _cap_output(redact_known_secrets(output.stdout), output_cap)


# In-flight executions are keyed by VersionRoot directory identity, not manifest
# identity. Hand-editing ``tool.json`` may change the latter while a child still
# needs the same directory files. The count protects overlapping calls, and the
# lock covers only registry updates and lookups.
_INFLIGHT_EXECUTIONS: dict[tuple[int, int], int] = {}
# This second process-local fact is what lets absence mean idle for a generation
# this process CREATED.  Absence from ``_INFLIGHT_EXECUTIONS`` alone means
# UNKNOWN, not idle: a generation that pre-dates this backend may still be in use
# by a start_new_session child orphaned by the previous backend. Collapsing that
# third state into idle was the bug that let delete/discard rmtree such a child.
_LOCAL_EXECUTION_GENERATIONS: set[tuple[int, int]] = set()
# A parked tree is eligible for automatic collection only when THIS process saw
# it while an execution was registered. A backend hard restart empties both
# registries while start_new_session children may survive; therefore an unknown
# stale/discarded tree is evidence for the operator, not proof of idleness.
_DEFERRED_EXECUTION_CLEANUPS: set[tuple[int, int]] = set()
_EXECUTION_LOCK = threading.Lock()
type ExecutionJudgement = Literal["running", "locally-proven-idle", "unknown"]


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


@contextlib.contextmanager
def _inflight_execution(identity: tuple[int, int]) -> Iterator[None]:
    """Count one execution against that DIRECTORY (``directory_identity``) in, and
    back out again.

    A context manager rather than a register/discard pair at the call site
    because the ``finally`` is the point: a handler that raises, or an AI request
    cancelled mid-call, must not leave a registration behind -- one leaked entry
    would defer that package's backup on every future promote, forever. The lock
    is taken for the two dict updates ONLY, never held across the ``yield`` (which
    spans an ``await`` and the whole subprocess run).

    The count is dropped to zero by REMOVING the key, so ``in`` is the whole
    query and a stale zero can never read as "still in use".
    """
    with _EXECUTION_LOCK:
        _INFLIGHT_EXECUTIONS[identity] = _INFLIGHT_EXECUTIONS.get(identity, 0) + 1
    try:
        yield
    finally:
        with _EXECUTION_LOCK:
            remaining = _INFLIGHT_EXECUTIONS.get(identity, 0) - 1
            if remaining > 0:
                _INFLIGHT_EXECUTIONS[identity] = remaining
            else:
                _INFLIGHT_EXECUTIONS.pop(identity, None)


def directory_execution_in_flight(identity: tuple[int, int]) -> bool:
    """Return only the positive process-local running fact for one VersionRoot."""
    with _EXECUTION_LOCK:
        return identity in _INFLIGHT_EXECUTIONS


def remember_local_execution_generation(version_root: VersionRoot) -> None:
    """Record a generation this process created before returning it as installed.

    Such a generation cannot have a child left by an earlier backend process.
    Therefore, and only therefore, a zero local execution count proves it idle.
    """

    identity = directory_identity(version_root)
    if identity is None:
        return
    with _EXECUTION_LOCK:
        _LOCAL_EXECUTION_GENERATIONS.add(identity)


def package_execution_judgement(
    package_root: PackageLayoutRoot,
) -> ExecutionJudgement:
    """Judge all real version generations as running, locally idle, or unknown.

    Every directory in ``versions/`` is considered, including a future
    ``<vid>.discarded`` parking name. ``locally-proven-idle`` requires positive
    evidence that THIS process created every generation and has no execution
    registered against any of them. A missing registry entry for a generation
    that pre-dates this process is ``unknown``: a detached child may have survived
    the restart. Filesystem answers we cannot establish are unknown for the same
    fail-closed reason.
    """

    versions = package_root.path / _VERSIONS_DIRNAME
    try:
        versions_info = os.lstat(versions)
    except FileNotFoundError:
        return "locally-proven-idle"
    except OSError:
        return "unknown"
    if not stat.S_ISDIR(versions_info.st_mode):
        return "unknown"
    try:
        entries = list(os.scandir(versions))
    except OSError:
        return "unknown"
    identities: list[tuple[int, int]] = []
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            return "unknown"
        identity = directory_identity(VersionRoot(Path(entry.path)))
        if identity is None:
            return "unknown"
        identities.append(identity)
    with _EXECUTION_LOCK:
        if any(identity in _INFLIGHT_EXECUTIONS for identity in identities):
            return "running"
        if all(identity in _LOCAL_EXECUTION_GENERATIONS for identity in identities):
            return "locally-proven-idle"
    return "unknown"


def remember_running_tree_for_cleanup(path: Path) -> None:
    """Record that this process observed ``path`` parked while work was running."""

    identity = directory_identity(VersionRoot(path))
    if identity is None:
        return
    with _EXECUTION_LOCK:
        _DEFERRED_EXECUTION_CLEANUPS.add(identity)


def deferred_tree_observed_idle(
    path: Path,
    execution_scope: PackageLayoutRoot,
) -> bool:
    """Consume this process's permission to collect a parked tree.

    The first gate is deliberately process-local. An empty in-flight registry
    after restart does not mean a start_new_session child is gone; only the
    process that observed the parked tree as running may later observe the same
    execution scope as idle and remove it.
    """

    identity = directory_identity(VersionRoot(path))
    if identity is None:
        return False
    with _EXECUTION_LOCK:
        if identity not in _DEFERRED_EXECUTION_CLEANUPS:
            return False
    if package_execution_judgement(execution_scope) != "locally-proven-idle":
        return False
    with _EXECUTION_LOCK:
        # Consume before rmtree. If removal itself fails, the remains stay for
        # the operator rather than letting inode reuse replay this permission.
        if identity not in _DEFERRED_EXECUTION_CLEANUPS:
            return False
        _DEFERRED_EXECUTION_CLEANUPS.remove(identity)
    return True


# The DEFERRED-REMOVAL namespace: where a package goes when it must stop being a
# package NOW but cannot be destroyed yet, because a subprocess is still reading
# it. ``.{name}.stale-<token>`` means "superseded, collect when idle", and it is
# the only shape ``tool_builder._sweep_stale_backups`` will ever remove.
#
# ``delete_tool`` mints this name and tool_builder's sweep recognizes it. The
# shared spelling lives here because tool_builder already imports this module;
# duplicating the on-disk contract would let writer and collector drift.
#
# Dot-prefixed is load-bearing, not cosmetic: ``_scan_all`` and
# ``known_secret_values`` both skip hidden directories, so deferred remains are
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


def _make_handler(
    advertised: Resolved,
    entry: list[str],
    identity: tuple[int, int, int] | None,
    advertisement_generation: _AdvertisementGeneration,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Bind one advertisement to its package and exact resolved version.

    Directory registration begins before later checks so delete and stale cleanup
    see the version throughout the call. Package ``.env`` and toggle are read at
    call time, while manifest identity and subprocess cwd remain VersionRoot data.
    """

    advertised_directory_identity = directory_identity(advertised.version_root)

    async def _handler(arguments: dict[str, Any]) -> str:
        package_root = advertised.package_root
        version_root = advertised.version_root
        if identity is None or advertised_directory_identity is None:
            return _TOOL_REPLACED_RESULT
        # Register the exact VersionRoot before any queued work. A hold nobody can
        # identify would be invisible to package-wide destructive cleanup.
        running = directory_identity(version_root)
        if running is None:
            return _TOOL_REPLACED_RESULT
        with _inflight_execution(running):
            # Recheck inside the hold, then once more immediately before Popen.
            if not _still_the_expected_package(version_root, identity):
                return _TOOL_REPLACED_RESULT
            # Toggle and .env are PackageRoot state, intentionally read at call
            # time. Missing, foreign, or unreadable state all fail closed.
            if not package_enabled(package_root):
                return _TOOL_DISABLED_RESULT
            settings = get_settings()
            env, env_secrets = _build_tool_env(package_root)
            args_json = json.dumps(arguments, ensure_ascii=False)
            # The ``.env`` VALUES this child is about to be handed, held so they
            # stay redactable no matter what the file says by the time the child
            # finishes. The scope has to reach past the subprocess: the masking of
            # the child's output happens INSIDE ``_run_tool_subprocess`` (F1/D36),
            # so releasing on the child's exit would still be too early. It ends
            # where the awaited call returns, which is after that redaction and
            # before the string is handed to the llm loop -- everything downstream
            # (the role:"tool" message, the log, the JSONL sink) sees the masked
            # copy.
            with _inflight_secrets(env_secrets):
                return await run_in_threadpool(
                    _run_tool_subprocess,
                    list(entry),
                    advertised,
                    identity,
                    advertisement_generation,
                    env,
                    args_json,
                    settings.llm_tool_timeout_seconds,
                    settings.llm_tool_output_max_chars,
                )

    return _handler


# --- mutation (name-validated + containment-checked) -----------------------


def _resolve_package_dir(name: str) -> Path | None:
    """Resolve ``<tools_dir>/<name>`` for a mutation, or None if it is unsafe.

    The path-traversal hard-block, in two independent layers:

    1. ``name`` must match ``_NAME_RE`` -- which admits no ".", "/", or
       whitespace, so ``".."`` / ``"../evil"`` / ``"/etc/passwd"`` never even
       reach the filesystem;
    2. the RESOLVED candidate must still be contained under the RESOLVED
       ``tools_dir``. Redundant with (1) for a plain name, but it is what
       actually stops a package that is a SYMLINK pointing outside the tools dir
       (or any future loosening of the regex) from letting ``delete_tool``
       ``rmtree`` an arbitrary path. Attack blocked: a request to
       ``set_enabled("../../etc")`` or a symlinked ``evil`` package escaping the
       directory returns None here and the caller no-ops.

    None also covers the feature being off (``tools_dir`` unset).
    """
    base = tools_dir()
    if base is None or not _NAME_RE.match(name):
        return None
    candidate = (base / name).resolve()
    if not _is_within(base.resolve(), candidate):
        return None
    return candidate


def _resolve_package_dir_no_alias(name: str) -> PackageRoot | None:
    """``_resolve_package_dir`` PLUS the INTERNAL-alias refusal, in one helper.

    ``_resolve_package_dir`` resolves ``tools/<name>`` and then checks
    containment -- which an INTERNAL symlink ``tools/alias -> tools/real``
    PASSES, because it resolves to a path genuinely inside the tools root. The
    alias/real distinction is erased by that ``resolve()``, so a caller handed
    the result cannot tell it was addressed through an alias, and every
    by-name operation on it silently acts on the REAL package instead
    (``PATCH /api/tools/alias/summary`` finalizing REAL's sidecar, a regenerate
    spending an LLM session rewriting REAL's summary). ``set_enabled`` calls
    that out at length and hard-blocks it before its own resolve, for exactly
    this reason (see its H3 comment); ``delete_tool`` carries its own variant
    because it has a SAFE alias action (unlink just the link).

    Every route/service that addresses a package BY NAME with no such special
    case goes through here instead of composing the three steps itself, so the
    hard-block cannot be forgotten by the next one added: name regex + tools_dir
    gate, then ``is_symlink`` (which does NOT follow the final component, so it
    sees the alias itself) BEFORE the resolve, then the containment resolve, and
    finally the "is anything actually installed there" ``is_dir``. None is the
    single did-not-happen answer for all four.

    ``_resolve_package_dir`` is deliberately left alone: ``delete_tool`` must
    keep reaching an alias to unlink it, so the refusal belongs in this
    composition, not in the shared resolve.
    """
    base = tools_dir()
    if base is None or not _NAME_RE.match(name):
        return None
    if (base / name).is_symlink():
        return None
    directory = _resolve_package_dir(name)
    return PackageRoot(directory) if directory is not None and directory.is_dir() else None


def set_enabled(name: str, enabled: bool) -> bool:
    """Atomically update package-layer ``state.json`` for one real package.

    The manifest and every version directory remain untouched. FOREIGN state is
    never overwritten; unsafe names, aliases, missing packages, and failed writes
    return False.
    """
    base = tools_dir()
    if base is None or not _NAME_RE.match(name):
        return False
    # INTERNAL-alias hard-block (H3), BEFORE resolve: an internal symlink
    # ``tools/<name> -> tools/real`` RESOLVES inside the tools root, so the
    # resolve-then-contain check below would PASS and this toggle would write the
    # state file INTO the REAL package THROUGH the alias -- switching off a tool
    # the operator addressed by another name. ``is_symlink`` does not follow the
    # final component, so it detects the alias itself; refuse outright -- set_enabled
    # has no safe action on an alias, unlike delete_tool which can at least drop
    # just the dangling link. Checked before resolve precisely because
    # ``resolve()`` erases the alias/real distinction. External-symlink escapes are
    # separately caught by ``_resolve_package_dir``'s containment check.
    if (base / name).is_symlink():
        return False
    directory = _resolve_package_dir(name)
    if directory is None or not directory.is_dir():
        return False
    package_root = PackageRoot(directory)
    # The one write is atomic: a torn
    # state file is an UNREADABLE one, and an unreadable one takes the package out
    # of the registry (see ``_read_enabled_state``), so publishing this by
    # truncate-then-write would make a failed toggle strictly worse than no toggle.
    # P2 intentionally has no state-publication lock: revise now publishes only
    # ``versions/<vid>`` plus ``current``, so no promote tail can replace this
    # package-layer state.  The old lock had lost its second participant.
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


type DiscardOutcome = Literal["ok", "not_found", "lineage_unavailable"]


def discard_version(resolution: Resolved) -> DiscardOutcome:
    """Discard exactly the resolved current version, preserving the safe order.

    The caller owns the global single-flight reservation and has already
    compared its expected vid with ``resolution.vid``.  This function therefore
    performs only the transition itself, with no second ``current`` resolution
    that could switch versions underneath that comparison.
    """

    package_root = resolution.package_root
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
    publication = _publish_discard_and_retire(package_root, previous_vid, current)
    if not publication:
        return "not_found"

    # Discard is complete at publication. Everything below is best-effort
    # cleanup, but destructive cleanup is forbidden unless the directory fsync
    # confirmed that the new pointer survives a crash.
    #
    # Publication and retirement occurred under the same lock scan capture uses.
    # The marker lives in each advertised handler's closure, so no rename,
    # replacement, delete, or inode reuse can clear it; a later advertisement
    # gets a new cohort.
    if not publication.durable:
        return "ok"

    # RENAME FIRST, then decide -- the same order whole-package deletion uses,
    # for the same structural reason. Asking the registry before the rename
    # leaves a window in which a handler can pass its identity check, register,
    # and start a child while ``rmtree`` is already walking this version. Once
    # parked, every later handler refuses because its captured VersionRoot no
    # longer exists at that name; the query below therefore sees the complete
    # set of children that could still be using the directory we now hold.
    #
    # Parking is best-effort cleanup after the durable ``current`` publication,
    # so a failed rename cannot turn a completed discard into a failure or
    # license an ``rmtree`` against the still-live spelling.
    parked = current.path.with_name(f"{resolution.vid}.discarded")
    try:
        os.rename(current.path, parked)
    except Exception:
        return "ok"

    try:
        execution = package_execution_judgement(package_root)
    except Exception:
        # The shared helper is fail-closed already; this backstop keeps cleanup
        # just as conservative if a test double or future implementation raises.
        execution = "unknown"
    if execution == "running":
        remember_running_tree_for_cleanup(parked)
        return "ok"
    if execution == "unknown":
        return "ok"

    with contextlib.suppress(Exception):
        shutil.rmtree(parked)
    return "ok"


def delete_tool(name: str) -> bool:
    """Delete a package (``rmtree``), or an alias (``unlink``). Returns success.

    Name-validated and containment-checked exactly like ``set_enabled`` (the
    traversal hard-block is what makes an ``rmtree`` here safe), so it can only
    ever remove a directory that genuinely sits inside ``tools_dir``. False when
    the name is unsafe, the package is absent, or it could not be taken out of
    the registry at all.

    A package still executing, or inherited from before this process and thus
    UNKNOWN, is left in the deferred-removal namespace instead of being
    destroyed. The running case is the same
    treatment ``tool_builder._promote_staging_replace`` gives the backup it can no
    longer drop, for the same measured reason (see ``_INFLIGHT_EXECUTIONS``): a
    running child's cwd is a reference to the INODE, so the rename disturbs
    NOTHING, while the ``rmtree`` turns every later relative open -- a lazily
    imported helper, a data file -- into ENOENT. A tool call failing halfway is
    worse than it sounds: the model sees a failure for an action whose external
    side effect may already have happened, and may simply retry it.

    Deferring changes nothing the caller can observe. The tool is gone from the
    registry the instant this returns EITHER WAY: the new name is dot-prefixed,
    which is precisely what ``_scan_all`` (and so ``list_tools`` /
    ``enabled_llm_tools``) and ``known_secret_values`` skip. The route's contract
    is untouched -- True is a genuine deletion of what the user saw, False still
    folds every "did not happen" into one 404.

    The remains are offered to the SAME sweep
    (``tool_builder._sweep_stale_backups``, at the end of every tool job), but
    only this backend process may collect a tree it personally observed while an
    execution was registered, whose generations this process created, and later
    observed idle. A hard restart empties both positive facts while a
    start_new_session child may survive, so the next process leaves unknown
    remains for the operator. Both forms are hidden, inert litter, never a
    phantom package.
    """
    base = tools_dir()
    if base is None or not _NAME_RE.match(name):
        return False
    # INTERNAL-alias hard-block (H3), BEFORE resolve: an internal symlink
    # ``tools/<name> -> tools/real`` RESOLVES inside the tools root, so the
    # resolve-then-contain check below would PASS and ``rmtree`` would recurse
    # through the alias and DELETE THE REAL PACKAGE -- user-triggerable data loss,
    # since the scan lists the alias as an invalid row and the UI offers 刪除 on
    # invalid rows. ``is_symlink`` does not follow the final component, so it
    # detects the alias itself; ``unlink`` removes ONLY the link (its target,
    # internal OR external, is never touched), so the phantom row disappears and
    # the real package survives. This is a genuine deletion of what the user saw
    # (the alias row), so it returns True. It runs before resolve precisely
    # because ``resolve()`` would erase the alias/real distinction; the
    # resolve-then-contain check below still guards a non-symlink external escape.
    candidate = base / name
    if candidate.is_symlink():
        try:
            candidate.unlink()
        except OSError:
            return False
        return True
    directory = _resolve_package_dir(name)
    if directory is None or not directory.is_dir():
        return False
    # RENAME FIRST, then decide -- the exact order ``_promote_staging_replace``
    # uses, and for a reason that is structural rather than stylistic. Asking the
    # registry first and removing second leaves a window: a handler that passes
    # its own identity check an instant after our answer registers and starts a
    # child against files the ``rmtree`` is already walking. Moving the rename
    # ahead closes that window instead of narrowing it, because the rename is what
    # makes every later handler REFUSE on its own -- ``package_identity`` of the
    # now-empty path answers None, and a None identity is a refusal (see
    # ``_make_handler``). After it, the only executions that can exist against
    # this directory are ones that were already registered, which is exactly the
    # set the query below can see. The rename itself is the operation MEASURED to
    # be invisible to a running child (see ``_INFLIGHT_EXECUTIONS``).
    #
    # ``directory.parent`` rather than ``base``: the containment check above
    # already proved the resolved package sits directly under the resolved tools
    # root, so this is that root -- and taking it FROM the path being renamed is
    # what makes "the new name lands in the same directory" true by construction
    # rather than by re-deriving it. A rename inside one directory is also the
    # only shape that cannot cross a filesystem, and the only one that is atomic.
    deferred = _stale_backup_path(directory.parent, name, uuid4().hex)
    try:
        os.rename(directory, deferred)
    except OSError:
        # The package is untouched and still listed, so this is the honest "did
        # not happen" -- the same answer a failed removal gave before.
        return False
    # The identity is taken from what we now HOLD rather than from the name we
    # were given, so it describes the very files a child could still be reading
    # (a rename carries a directory's ``(st_dev, st_ino)`` -- measured). The
    # DIRECTORY's identity and not its manifest's: that is what the handler
    # registered, and it is the only one an in-place manifest rewrite mid-call
    # cannot move (see ``directory_identity``).
    #
    # "Cannot read it" DEFERS, the same direction the sweep takes and the
    # opposite of the manifest read this used to do. The old reasoning -- a
    # package whose manifest cannot be lstat'ed is one no handler can have
    # registered -- does not survive the move: an lstat failure on a directory we
    # just renamed successfully says nothing about what is running inside it, only
    # that we cannot name it. Nothing observable changes either way (the tool is
    # already out of the registry and this still returns True); what is left
    # behind is marked remains. The current process records that it saw the tree
    # running; a later sweep may collect it after observing idle, while a fresh
    # process deliberately has no such authority.
    try:
        execution = package_execution_judgement(PackageLayoutRoot(deferred))
    except Exception:
        # The package is already hidden, so an unexpected judgement failure has
        # the same safe outcome as unknown: retain it for the operator.
        execution = "unknown"
    if execution == "running":
        remember_running_tree_for_cleanup(deferred)
        return True
    if execution == "unknown":
        return True
    # Best-effort from here: the tool is already gone as far as everything that
    # reads this directory is concerned, so a removal that fails part-way must not
    # be reported as "did not happen". Because this path was never observed
    # running it receives no later automatic-cleanup permission; marked remains
    # are hidden evidence for the operator rather than a restart-unsafe retry.
    shutil.rmtree(deferred, ignore_errors=True)
    return True
