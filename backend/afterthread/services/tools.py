"""Tool runtime: discover, validate, and execute installed tool packages
(see D21 in docs/web-v2-decisions.md).

A tool package is a directory ``<tools_dir>/<name>/`` holding:

* ``tool.json`` -- ``{"name", "description", "parameters", "entry", "enabled"?}``
  where ``name`` MUST equal the directory name and match
  ``^[a-z0-9][a-z0-9_-]{0,63}$``, ``description`` is a nonempty string,
  ``parameters`` is a JSON-Schema object for the arguments, ``entry`` is an argv
  list (e.g. ``["python3", "run.py"]``), and ``enabled`` (optional, default
  true) gates whether the tool is advertised to the model;
* the implementation files ``entry`` runs;
* an OPTIONAL ``.env`` (``KEY=VALUE`` lines) holding THAT tool's own secrets
  (e.g. a KB API key), which are injected into the subprocess environment;
* an OPTIONAL ``.ai_meta.json`` sidecar (D40) holding the AI-written summary of
  the package and its draft/final status. It is backend-authored metadata, NOT
  part of the executable contract: nothing in the runtime reads it, so a missing
  or corrupt one only empties the summary panel (see ``_AI_META_FILENAME``).

Execution contract (``_run_tool_subprocess``): the runner invokes ``entry`` with
``cwd`` = the tool directory, writes the arguments JSON to the child's STDIN,
reads STDOUT as the tool result (capped at ``settings.llm_tool_output_max_chars``
behind a truncation marker), and enforces ``settings.llm_tool_timeout_seconds``
by killing the child's whole PROCESS GROUP on expiry. A non-zero exit becomes an
agent-visible ``"tool failed (exit N): <stderr>"`` result -- a failure the model
can react to, never an exception that 502s the interaction.

SECURITY STANCE (D21, v1). These are the operator's OWN local tools and shell
capability is an explicit requirement (same nature as a Claude Code skill), so
v1 does NOT sandbox with a container. What it DOES guarantee:

* the subprocess environment is built FROM SCRATCH -- only PATH/HOME/LANG/LC_ALL/
  TMPDIR are passed through from the parent, plus the tool's own ``.env``. The
  parent environment is NEVER inherited wholesale, because it carries
  ``OPENAI_API_KEY`` (and any other backend secret): a generated, possibly
  half-trusted tool must not be able to read our LLM credentials out of its own
  ``os.environ``. This is the single most important guarantee in this module.
  Be honest about its reach, though: scrubbing prevents ACCIDENTAL leakage (the
  key is simply not in the child's ``os.environ``), but a same-UID subprocess
  can in principle read ``/proc/<ppid>/environ`` of the parent, so this is not
  adversarial isolation -- the real trust boundary is the operator only
  installing tool instructions they trust (D21), not the scrub;
* every mutating entry point (``set_enabled`` / ``delete_tool``) validates the
  name against the package-name regex AND re-checks resolved-path containment
  under ``tools_dir`` before touching the filesystem, so a traversal name like
  ``"../.."`` (or a symlinked package escaping the tools dir) can never make us
  rewrite, rename or ``rmtree`` a path outside the tools directory;
* runtime and output are bounded (timeout + process-group kill, output cap), so
  a hung or runaway tool cannot pin the interaction or blow the prompt/log.

Settings-driven, exactly like ``llm_log``: ``tools_dir`` unset ("") means the
whole feature is OFF -- ``list_tools()`` / ``enabled_llm_tools()`` return empty,
so the AI workflows advertise no tools and their prompts stay byte-identical to
the tool-less build.
"""

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
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
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
# scan (list_tools / enabled_llm_tools / every mutation), so an unbounded one is a
# per-scan I/O + memory hazard; a manifest over this is listed invalid rather than
# parsed. BOTH the scan AND set_enabled enforce it through the bounded
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
# the package outright (``validate_package``). 64 KiB dwarfs any real secrets file
# (a handful of KEY=VALUE lines).
_ENV_FILE_MAX_BYTES = 64 * 1024

# The per-package AI sidecar (D40): the summary an LLM writes about the package
# after a successful install, plus its draft/final status. DOT-PREFIXED on
# purpose -- that single leading character is what makes the sidecar fit the
# EXISTING conventions instead of needing four new special cases:
#
# * ``_scan_all`` skips hidden DIRECTORIES, and ``_scan_package`` reads only
#   ``tool.json``, so a sidecar can never surface as (or break) a registry row;
# * ``known_secret_values`` and the installer's ``.staging`` shell already use
#   "a leading dot means internal to the backend" (see ``_STAGING_DIRNAME``), so
#   an operator browsing a package reads it the same way;
# * ``delete_tool`` takes it with the package -- summary and tool share one
#   lifetime, which is exactly why this lives in the package rather than in
#   SQLite (a delete DEFERRED past a running call carries the sidecar into the
#   hidden name with everything else, so the two still die together);
# * the summary generator skips every dot-file when it feeds the package to the
#   model, so the sidecar never feeds itself back into its own next prompt.
#
# It is read with the SAME bounded reader the manifest gets, but at its OWN cap
# (``_AI_META_MAX_BYTES``) -- see there for why sharing the manifest's cap was a
# read/write asymmetry rather than a saving.
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

# The only two summary statuses that mean anything. "draft" is what generation
# writes; "final" (定版) is the operator freezing AI iteration on this tool --
# revise and regenerate both refuse a finalized package until it is un-finalized.
# Anything else on disk (a hand-edited sidecar, a future/older shape) reads as
# "no usable status" rather than being trusted, so the freeze can never be
# bypassed by writing a garbage value into the file.
_SUMMARY_STATUSES: tuple[str, ...] = ("draft", "final")

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

    ``valid`` gates execution: an invalid package is listed (so the UI can show
    WHY, via ``error``) but never advertised to the model or executed.
    ``parameters`` / ``entry`` are populated only when ``valid`` is True.
    ``enabled`` is read as early as possible (right after the JSON parses) so
    even an otherwise-invalid package reports the toggle state the operator set.
    """

    name: str
    directory: Path
    valid: bool
    enabled: bool
    description: str
    error: str | None
    parameters: dict[str, Any] | None
    entry: list[str] | None


# --- discovery / validation ------------------------------------------------


def tools_dir() -> Path | None:
    """The configured tools directory, or None when the feature is disabled.

    ``settings.tools_dir`` empty ("") means OFF -- None here makes every reader
    (``list_tools`` / ``enabled_llm_tools`` / the mutators) a no-op. Mirrors
    ``llm_log``'s settings-driven pattern; packaged mode's cli.py injects
    ``<data-dir>/tools`` so a uvx install has it set without operator action.
    """
    raw = get_settings().tools_dir.strip()
    if not raw:
        return None
    return Path(raw).expanduser()


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


def _write_regular_file(path: Path, content: str) -> bool:
    """Write ``content`` as utf-8 to ``path``, but ONLY if it is a regular file.

    The WRITE-side mirror of ``_read_regular_file_capped`` -- ``write_file``
    (tool_builder) and ``set_enabled``'s manifest write both funnel through it, so
    the same jail-hardening holds on every write the tool subsystem does instead of
    being re-derived per call site. Returns True on success; False (never raises)
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
    auto-creates them for ``write_file``; for ``set_enabled``'s manifest write the
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


def _scan_package(directory: Path, expected_name: str | None = None) -> _PackageScan:
    """Validate one candidate directory into a ``_PackageScan``.

    Every failure path returns ``valid=False`` with a short, safe reason (never
    a raw filesystem error string), so a single broken package can never break
    the scan of the others and is never executable.

    ``expected_name`` is the name the manifest's ``name`` field must equal; it
    defaults to the directory's own name (the installed-package invariant). The
    installer's staging validation passes the FUTURE name instead -- its staging
    directory is a throwaway uuid, but the manifest must already carry the name
    the package is about to be installed under (see ``validate_package``).
    """
    name = expected_name if expected_name is not None else directory.name

    def invalid(error: str, *, enabled: bool = True) -> _PackageScan:
        return _PackageScan(
            name=name,
            directory=directory,
            valid=False,
            enabled=enabled,
            description="",
            error=error,
            parameters=None,
            entry=None,
        )

    # A package directory that is itself a SYMLINK is refused (listed invalid,
    # never executed): _scan_all's ``is_dir()`` filter FOLLOWS the link, so
    # without this a symlink to any real directory would be scanned -- and
    # potentially run -- as a package. Real directories only (H3 / D21). Staging
    # validation is unaffected: its directory is a real ``mkdir``ed uuid dir.
    if directory.is_symlink():
        return invalid("package directory must be a real directory (not a symlink)")

    tool_json = directory / "tool.json"
    # tool.json must be a REAL file too: a symlinked manifest is refused (listed
    # invalid) so a scan can never READ -- and set_enabled can never REWRITE --
    # a file outside the package through it (H3). Checked BEFORE ``is_file()``,
    # which follows the link and would otherwise accept it.
    if tool_json.is_symlink():
        return invalid("tool.json must be a real file (not a symlink)")
    if not tool_json.is_file():
        return invalid("missing tool.json")
    # Read the manifest through the ONE bounded-regular-file helper (F3b): its
    # fstat gate refuses a FIFO/socket/device swapped in for tool.json and its
    # O_NOFOLLOW backstops the is_symlink() fast path above against a symlink
    # raced in after it, while the cap+1 read means an oversized manifest is never
    # slurped whole into memory (the per-scan hazard _MANIFEST_MAX_BYTES exists
    # for). None is every refusal/read failure; a len past the cap is "too large".
    text = _read_regular_file_capped(tool_json, _MANIFEST_MAX_BYTES)
    if text is None:
        return invalid("tool.json is not a readable regular file")
    if len(text) > _MANIFEST_MAX_BYTES:
        return invalid("tool.json is too large")
    try:
        raw = json.loads(text)
    except ValueError:
        return invalid("tool.json is not valid JSON")
    if not isinstance(raw, dict):
        return invalid("tool.json is not a JSON object")

    # Read `enabled` as soon as the JSON is a dict so even an invalid package
    # (name mismatch, bad schema) still reports the operator's intended toggle.
    enabled_raw = raw.get("enabled", True)
    enabled = enabled_raw if isinstance(enabled_raw, bool) else True

    name_field = raw.get("name")
    if not isinstance(name_field, str) or not _NAME_RE.match(name_field):
        return invalid("name is missing or not a valid tool name", enabled=enabled)
    if name_field != name:
        return invalid("name does not match the package name", enabled=enabled)

    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        return invalid("description is missing or empty", enabled=enabled)

    parameters = raw.get("parameters")
    if not isinstance(parameters, dict):
        return invalid("parameters is not a JSON Schema object", enabled=enabled)
    # Bound the parameters schema (see _PARAMETERS_SCHEMA_MAX_BYTES): it is
    # re-serialized into the tools array of EVERY LLM request that has this tool
    # enabled, so an oversized one is a recurring token/prompt-bloat hazard, not
    # just a big file. ``parameters`` came from json.loads, so json.dumps of it
    # cannot raise.
    if len(json.dumps(parameters)) > _PARAMETERS_SCHEMA_MAX_BYTES:
        return invalid("parameters schema is too large", enabled=enabled)

    entry = raw.get("entry")
    if not _valid_entry(entry):
        return invalid("entry is not a non-empty list of command strings", enabled=enabled)
    if not _entry_file_exists(directory, entry):
        return invalid("entry does not reference a file inside the tool package", enabled=enabled)

    return _PackageScan(
        name=name,
        directory=directory,
        valid=True,
        enabled=enabled,
        description=description.strip()[:_DESCRIPTION_CAP],
        error=None,
        parameters=parameters,
        entry=list(entry),
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
        _scan_package(child)
        for child in sorted(base.iterdir())
        if child.is_dir() and not child.name.startswith(".")
    ]


def _find_embedded_secret_file(directory: Path) -> str | None:
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
    base = directory.resolve()
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


def validate_package(directory: Path, expected_name: str) -> str | None:
    """Validate a candidate package OUTSIDE the tools dir; None means valid.

    The installer's pre-move gate: ``directory`` is its staging build (a uuid
    directory name, hence the explicit ``expected_name`` -- the name the package
    is about to be installed under, which the manifest must already carry).
    Runs the exact same checks an installed package faces on every scan
    (manifest shape, name regex + match, entry-file containment), so a package
    that passes here can never turn up ``valid=False`` after the move -- PLUS the
    STRICTER install-only gates below.

    The .env size gate is deliberately NOT part of ``_scan_package`` (which the
    registry runs on every scan): an oversized ``.env`` must BLOCK a fresh install
    here, but a package whose ``.env`` is MUTATED oversized AFTER install should
    keep running under the runtime degrade (``_load_tool_dotenv`` -> {}), not
    vanish from the registry as invalid. So this gate lives on the installer's
    pre-move path only. Being stricter than the scan preserves the invariant above
    (passing here still implies passing the scan); only the reverse loosens.

    The embedded-secret gate (H3) is likewise install-only: it must BLOCK a package
    that baked a known key into any file, but is never re-run on installed packages
    (whose ``.env`` legitimately holds the injected secret post-install).
    """
    error = _scan_package(directory, expected_name=expected_name).error
    if error is not None:
        return error
    # Install-only .env size gate (see _ENV_FILE_MAX_BYTES). Mirrors
    # _load_tool_dotenv's own is_file()-then-stat() shape; a stat failure is
    # treated as "not oversized" (the scan above already vetted the package, and
    # the runtime degrade remains the post-install defense).
    env_file = directory / ".env"
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
    offender = _find_embedded_secret_file(directory)
    if offender is not None:
        return f"{redact_known_secrets(offender)} 不得包含秘密值（請改由環境變數讀取）"  # noqa: RUF001
    return None


# --- AI summary sidecar (D40) ------------------------------------------------

# Serializes every COMPOUND sidecar operation -- the read-check-write sequences
# ``set_summary_status`` and ``store_summary_meta`` run. Modeled on
# ``llm_log._FILE_SINK_LOCK``: a lock whose ONLY job is making one
# read-modify-write FILE sequence atomic w.r.t. other writers of that same file,
# deliberately NOT any other lock in this module (``_INFLIGHT_LOCK`` /
# ``_ENV_VALUE_CACHE_LOCK`` guard in-memory registries read on hot paths) and not
# one borrowed from another module.
#
# The race it closes is not theoretical, and the finalize re-check inside
# ``store_summary_meta`` does NOT close it on its own. Both compound operations
# run on THREADPOOL workers (every route hops through ``run_in_threadpool``), so
# a PATCH's ``set_summary_status`` genuinely runs in parallel with a regenerate's
# store on another worker: both read ``"draft"``, the PATCH writes ``"final"``,
# and the regenerate then writes its own composed meta carrying the STALE
# ``"draft"`` plus the new summary -- 定版 silently undone, with the operator's
# frozen text replaced by the very generation the freeze was meant to stop. Two
# in-place writers interleaving on the same file is the second half of the same
# hazard. Holding this lock across BOTH sequences is what makes the re-check
# mean something: nothing can land between the read and the write.
#
# Two invariants keep it safe rather than merely present:
#
# * it is NEVER held across an ``await`` -- both acquirers are plain synchronous
#   functions that callers reach through ``run_in_threadpool``, so the event loop
#   is never parked on it;
# * it NEVER nests. ``write_tool_meta`` / ``read_tool_meta`` stay lock-FREE and
#   are called from INSIDE a hold; only the two compound entry points acquire, and
#   neither calls the other.
_META_LOCK = threading.Lock()


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

    Why the sidecar needs it at all: ``.ai_meta.json`` is a plain JSON file the
    operator is explicitly allowed to hand-edit, and ``"\\ud800"`` is a
    JSON-LEGAL escape that ``json.loads`` accepts happily -- producing a ``str``
    that is NOT UTF-8 encodable. Left alone it breaks both boundaries: on the
    WRITE side ``json.dumps(...).encode("utf-8")`` raises ``UnicodeEncodeError``
    (a 500 out of a PATCH that should have answered False -> 404), and on the
    READ side it sails into the summary response and blows up in Starlette's
    strict ``JSONResponse.render`` encode -- a 500 on a GET that exists to
    DEGRADE a corrupt sidecar, not to die on one.
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


def read_tool_meta(directory: Path) -> dict[str, Any] | None:
    """Parse the package's ``.ai_meta.json`` sidecar, or None if there is none.

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
    limit INSIDE ``json.loads``, and ``summary_status`` runs this once per
    package on every ``list_tools`` scan -- so a single hand-edited or malicious
    sidecar escaping as an exception would 500 the whole 工具 page, not just its
    own row.

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
    text = _read_regular_file_capped(directory / _AI_META_FILENAME, _AI_META_MAX_BYTES)
    if text is None or len(text) > _AI_META_MAX_BYTES:
        return None
    try:
        raw = json.loads(text)
    except ValueError, RecursionError:
        return None
    return _utf8_safe_meta(raw) if isinstance(raw, dict) else None


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


def _still_the_expected_package(directory: Path, expected: tuple[int, int, int] | None) -> bool:
    """Is the package at ``directory`` still the one ``expected`` names?

    The ONE spelling of "nothing swapped under us", shared by the three places
    that act irreversibly on a package they looked at earlier: the runtime just
    before ``Popen`` (``_run_tool_subprocess``), the sidecar just before
    ``os.replace`` (``_write_sidecar_atomic``), and ``_make_handler``'s refusal
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
    return expected is not None and package_identity(directory) == expected


class _PackageReplaced(Exception):
    """Raised INSIDE ``_write_sidecar_atomic`` when the guard refuses the publish.

    Never escapes that function, and exists only so the refusal leaves through
    the same ``except`` that already unlinks the temp file: the alternative is a
    second copy of that cleanup, kept in step by hand, on the one path that runs
    when something is already going wrong.
    """


def _write_sidecar_atomic(
    directory: Path, data: bytes, expected_identity: tuple[int, int, int] | None
) -> bool:
    """Publish ``data`` as the package's sidecar ATOMICALLY. Returns success.

    Why the sidecar does NOT ride ``_write_regular_file`` any more: that helper
    opens the TARGET with ``O_CREAT | O_TRUNC``, so the previous file is
    destroyed the instant the open succeeds and only THEN is the new content
    written. Every failure past that point -- ENOSPC, a quota hit, an I/O error,
    a UnicodeEncodeError partway through -- returns False with the sidecar
    already truncated: the caller is told "did not happen" while a FINALIZED
    summary the operator explicitly froze is gone. Nothing anywhere can restore
    it, because the sidecar is the only copy of both the summary and the
    install's ``origin``.

    Write-to-temp + ``os.replace`` removes the whole class rather than the
    reported instance: the old content survives EVERY failure mode, and the
    sidecar is never observable half-written (``os.replace`` is atomic within one
    filesystem, and the temp file is created in the SAME directory precisely so
    it is one filesystem -- a system temp dir would risk EXDEV). House precedent
    is ``cli.py``'s init-env publish path (``_write_env_tempfile`` +
    ``_publish_env_file_force``); this mirrors its mechanics at the smaller scale
    a best-effort sidecar needs.

    What is deliberately NOT claimed, so nobody assumes it: the DIRECTORY is not
    fsynced, so the rename is not durable against power loss (the file's own
    contents are). That is the same ruling cli.py records for ``.env`` -- an
    ordering guarantee against failure, not a crash-consistency one -- and it is
    even easier to accept here, where the worst case is one regenerable summary.

    PERMISSIONS are PRESERVED across the publish (R7-3) -- which the plain
    write-to-temp shape does NOT do on its own, and that was a real regression
    hiding inside the atomicity fix. ``mkstemp`` creates the temp file at
    ``0o600`` (umask-masked, like any ``open(2)``) and ``os.replace`` carries the
    TEMP file's mode onto the published name, so an operator who had chmod'd
    their sidecar ``0o640`` to let their own group read it got it silently
    narrowed back to ``0o600`` on the next PATCH or regenerate, with nothing
    anywhere reporting the change. The pre-write ``lstat`` below already holds
    the existing ``st_mode`` for the symlink refusal, so carrying its permission
    bits onto the temp fd costs one ``fchmod`` and not a single extra stat.

    Only the low 9 bits are copied, deliberately NOT ``S_IMODE``'s 0o7777:
    setuid/setgid/sticky are not bits a best-effort metadata file we author has
    any business inheriting. When there is NO existing sidecar nothing is copied
    and mkstemp's default stands -- preserving is about not silently CHANGING
    what an operator set, so inventing a wider default for a fresh file would be
    the opposite mistake (cli.py fchmods ``.env`` because a human is told to go
    edit it; nobody is told to edit this).

    A READ-ONLY sidecar is a semantic that DID change here, deliberately, and is
    not being restored. ``_write_regular_file``'s ``O_TRUNC`` open needed write
    permission on the FILE, so a ``0o400`` sidecar refused the write with EACCES;
    publishing by ``os.replace`` needs write permission on the DIRECTORY, so it
    now SUCCEEDS (and preserves the ``0o400`` on the new file). That EACCES was
    incidental to the open rather than a designed contract: the package DIRECTORY
    is the protection boundary this subsystem actually supports -- it is what
    ``delete_tool`` removes wholesale and what every containment check is stated
    against -- and a read-only file under a writable package directory was never
    a promise we made. Recorded in the D40 r7 addendum so it is not re-derived
    later as a regression.

    The pre-write ``lstat`` keeps the refusal semantics ``_write_regular_file``'s
    ``O_NOFOLLOW`` + ``S_ISREG`` gate gave us: an existing sidecar that is a
    SYMLINK (a link raced into the package, aimed out of it) or any non-regular
    file (a FIFO/device/directory at that name) is refused outright rather than
    replaced. ``os.replace`` would otherwise happily swap a link or a FIFO for
    our regular file -- which is not an escape (a rename replaces the LINK, never
    writes through it), but IS a silent change of the refusal contract those
    tests pin. ENOENT is the ordinary first-write case and is not a refusal.

    ``expected_identity`` is the package this content was composed FOR, and it is
    verified on the line above ``os.replace`` -- the last instant that exists here
    (R6-2/R6-3). It lives at the publish rather than in the callers because that
    is where the guarantee is: everything between a caller's own check and this
    line is real work (a sidecar read, a redactor sweep of the whole tools
    directory, a JSON encode, mkstemp, write, fsync), and a package can be
    replaced inside it -- after which A's summary, A's origin or A's frozen status
    would be published into B. Both compound operations above take their identity
    from the resolve that produced ``directory`` and hand it down here unchanged,
    so the comparison spans the whole operation rather than the last few lines of
    it. None means the caller asserts NO identity and the publish is unguarded --
    the pre-R6 behaviour, kept for callers that just created the package
    themselves (test seeding) and never taken by a production one: both of those
    refuse a None identity of their own accord, in their own vocabulary, before
    they reach the write.

    Every failure is False, never an exception: summary metadata is best-effort,
    and the caller has exactly one "did-not-happen" answer to map. The temp file
    is unlinked on every failure path (best-effort, suppressed -- a cleanup error
    must not mask the original), so a failed write leaves nothing behind in the
    package -- including the refusal above, which is a failure like any other from
    the caller's side. That matters more here than for a generic temp file: a stray
    ``.ai_meta.json.*.tmp`` sitting in a package would be scanned by every later
    ``validate_package`` embedded-secret sweep. It is dot-prefixed for the same
    family of reasons the sidecar itself is -- the summary prompt's file
    inventory skips dot-files, so even a leftover temp can never feed a summary
    back into its own next prompt.
    """
    path = directory / _AI_META_FILENAME
    # The mode to publish under. There is ALWAYS one now (R11): a fresh sidecar
    # gets _OWNER_RW explicitly rather than mkstemp's umask-masked default, and an
    # inherited mode is OR'd with it below.
    preserve_mode: int = _OWNER_RW
    try:
        existing_mode = os.lstat(path).st_mode
    except FileNotFoundError:
        pass  # no sidecar yet -- the ordinary first-write case, not a refusal
    except OSError:
        return False
    else:
        # lstat does NOT follow the final component, so a symlinked sidecar shows
        # up as one here (S_ISLNK), exactly as O_NOFOLLOW used to refuse it.
        if not stat.S_ISREG(existing_mode):
            return False
        # R7-3: the SAME lstat that refused a symlink supplies the bits to keep.
        # Low 9 only -- setuid/setgid/sticky are not inherited (see docstring).
        # OR'd with _OWNER_RW (R11): the operator's group/other customization is
        # honored, but OWNER read+write is not negotiable -- see that constant.
        preserve_mode = (existing_mode & 0o777) | _OWNER_RW
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=directory, prefix=f"{_AI_META_FILENAME}.", suffix=".tmp"
        )
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
        if expected_identity is not None and not _still_the_expected_package(
            directory, expected_identity
        ):
            raise _PackageReplaced
        os.replace(tmp_path, path)
    except OSError, _PackageReplaced:
        if fd_owned:
            with contextlib.suppress(OSError):
                os.close(fd)
        # Safe unconditionally: tmp_path is a name mkstemp invented for THIS call
        # alone, never a path a caller passed in (cli.py's same argument).
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        return False
    return True


def write_tool_meta(
    directory: Path, meta: dict[str, Any], *, expected_identity: tuple[int, int, int] | None = None
) -> bool:
    """Write the sidecar from the KNOWN SCHEMA, redacting its text. Returns success.

    This does NOT serialize ``meta``. It reads the six fields it understands out
    of ``meta``, coerces each to the shape the sidecar's contract promises, and
    writes THAT -- so every KEY on disk is a literal from this function and every
    VALUE is one this function chose or refused:

    * ``summary`` -- a ``str``; None becomes ``""`` (the placeholder a failed
      generation leaves, and the shape a hand-edited ``"summary": null`` should
      collapse to rather than sneaking past as "not a string, but not refused
      either"). Any OTHER type refuses the write: ``summary`` is a string by
      contract, the read path renders anything else as "no summary", and writing
      one would make the file lie about whether a summary exists;
    * ``status`` -- ``"draft"`` or ``"final"``, anything else ``"draft"``. An
      unknown value must never survive a write; ``summary_status`` already
      refuses to trust one on the read side, so persisting it would only keep a
      dead value alive;
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
    because nothing else CAN: ``status`` is one of two literals chosen above,
    ``llm_log_id`` is an int, ``llm_log_process`` is an opaque token this backend
    generated for itself, and ``updated_at`` is a machine timestamp its caller
    just produced (no on-disk one ever round-trips -- both callers overwrite it).

    The serialized payload is bounded by ``_AI_META_MAX_BYTES``, the SAME cap
    ``read_tool_meta`` refuses past, so this can never produce a file that reads
    back as None. Every other failure (an unwritable path, a symlinked/FIFO
    sidecar refused by ``_write_sidecar_atomic``'s lstat gate) is False too, so
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

    The write itself is ATOMIC (``_write_sidecar_atomic``): the previous sidecar
    survives every failure and the file is never observable half-written. See
    that helper for why the truncate-then-write shape was a real data-loss
    vector for a FINALIZED summary.

    ``expected_identity`` is carried STRAIGHT THROUGH to that helper, which checks
    it on the line above ``os.replace``; this function does not read it, and the
    redaction/encode/cap work below deliberately runs BEFORE the check rather than
    after it (that ordering is the whole point -- see ``_write_sidecar_atomic``).
    It defaults to "assert nothing" so a caller that just created the package
    itself need not invent one; every production caller passes the identity its
    own resolve captured, and both of them refuse a None identity in their own
    vocabulary before they get here.

    CONSEQUENCE, stated so it is not rediscovered as a bug: extra keys a
    hand-edited sidecar carries are DROPPED by the next write. Round-tripping
    them was never a contract -- it was a side effect of serializing the caller's
    dict, and it is precisely what let a stray key/value carry unmasked text into
    the file. The six fields above are the sidecar.

    ``llm_log_id`` and ``llm_log_process`` are carried through from ``meta``
    rather than re-derived, and that is load-bearing for ``set_summary_status``:
    its round-trip hands back what it just READ, so a 定版 in a LATER process must
    keep the id's original (now foreign) token. Stamping the current token here
    would forge freshness onto a stale id -- precisely the confusion the token
    exists to prevent -- so minting happens at exactly one call site, next to the
    id itself (``store_summary_meta``).

    The ``is_dir`` precondition is kept as the EXPLICIT answer to a racing
    ``delete_tool``: a summary landing just after one must not re-create the
    deleted package's directory holding nothing but a sidecar, which the registry
    would then list as a broken package named after the tool the user just
    removed. It was load-bearing when the sidecar rode ``_write_regular_file``,
    which CREATES missing parents (the meta-tool contract needs that);
    ``_write_sidecar_atomic``'s ``mkstemp`` in the package directory now fails
    with ENOENT instead, so the check states the rule rather than being the only
    thing enforcing it. It narrows, but cannot close, that window (the delete can
    still land between this check and the write); the remaining race is the same
    single-user local-tool edge install/delete already accepts (D21/D40).
    """
    if not directory.is_dir():
        return False
    summary = meta.get("summary")
    if summary is None:
        summary = ""
    if not isinstance(summary, str):
        return False
    updated_at = meta.get("updated_at")
    if not isinstance(updated_at, str):
        return False
    status = meta.get("status")
    if not (isinstance(status, str) and status in _SUMMARY_STATUSES):
        status = "draft"
    log_id = meta.get("llm_log_id")
    if not isinstance(log_id, int) or isinstance(log_id, bool):
        log_id = None
    log_process = meta.get("llm_log_process")
    if not isinstance(log_process, str):
        log_process = None
    origin_raw = meta.get("origin")
    try:
        payload: dict[str, Any] = {
            "summary": _redacted(summary),
            "status": status,
            "updated_at": updated_at,
            "llm_log_id": log_id,
            "llm_log_process": log_process,
            "origin": (
                {
                    "openapi_url": _redacted(_meta_str(origin_raw.get("openapi_url"))),
                    "instructions": _redacted(_meta_str(origin_raw.get("instructions"))),
                }
                if isinstance(origin_raw, dict)
                else None
            ),
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
    return _write_sidecar_atomic(directory, data, expected_identity)


def _narrowed_summary_status(meta: dict[str, Any] | None) -> str | None:
    """The trustworthy ``status`` inside an ALREADY-READ sidecar meta, or None.

    Pure, and shared by both readers below so the narrowing exists ONCE: a meta we
    could not read at all, one without a ``status``, and one whose ``status`` is
    not in ``_SUMMARY_STATUSES`` all collapse to None here, and each reader then
    decides what that None MEANS to it. Extracted (R4-2) so
    ``summary_status_or_unknown`` can hand its caller the very meta it judged
    without reading the file a second time to get it.
    """
    if meta is None:
        return None
    status = meta.get("status")
    return status if isinstance(status, str) and status in _SUMMARY_STATUSES else None


def summary_status(directory: Path) -> str | None:
    """The package's summary status (``"draft"``/``"final"``), or None.

    None covers every "no trustworthy status" case at once: no sidecar, an
    unreadable/corrupt one, or a ``status`` that is not one of the two known
    values (see ``_SUMMARY_STATUSES`` for why an unknown value must not be
    trusted rather than passed through). Called once per package by
    ``list_tools`` so the 工具 page can badge every row without an N+1 of
    per-tool summary requests.
    """
    return _narrowed_summary_status(read_tool_meta(directory))


# ``summary_status_or_unknown``'s third answer: "there IS a sidecar and we could
# not get a trustworthy status out of it". Deliberately NOT a member of
# ``_SUMMARY_STATUSES``, so it can never be mistaken for something a sidecar
# actually holds -- every read path narrows to that tuple, so a hand-edited
# ``"status": "unknown"`` is refused by the narrowing and reaches a caller as this
# sentinel only because the file could not be trusted, never because it said so.
# (The two therefore agree anyway: both mean "cannot tell".)
_SUMMARY_STATUS_UNKNOWN = "unknown"


def summary_status_or_unknown(directory: Path) -> tuple[str | None, dict[str, Any] | None]:
    """``(status, meta)`` for the callers that must not guess (R3-2, R4-2, R6-3).

    The same two real answers (``"draft"`` / ``"final"``), but the None that
    ``summary_status`` folds every failure into is SPLIT in two:

    * None -- there is DEFINITIVELY no sidecar (the ``lstat`` said ENOENT). This
      is the only case where "not finalized" is a fact rather than a guess;
    * ``_SUMMARY_STATUS_UNKNOWN`` -- a sidecar IS there and we cannot trust what
      it says: the bounded reader declined it (a symlink, a FIFO, ``chmod 000``,
      EIO on a failing disk), it is over the cap, it is not JSON, it is not an
      object, or its ``status`` is not one of ``_SUMMARY_STATUSES``.
      ``write_tool_meta`` produces NONE of those shapes, so every one of them
      means the file was hand-edited or damaged -- and "we could not read it" is
      not evidence of "not finalized".

    ``summary_status``'s own contract is deliberately UNCHANGED, and this is an
    ADDITIONAL reader rather than a replacement. Its remaining callers genuinely
    want the total, degrade-to-None behaviour: ``list_tools`` badges a row (a
    corrupt sidecar must degrade one badge, never 500 the whole 工具 page) and the
    summary routes -- INCLUDING the revise submit -- decide whether to answer 409
    without queueing anything (there, guessing "not finalized" costs a regenerate
    that would REPLACE the unreadable file anyway, or a job that refuses itself).
    A ROUTE answers about a resource's KNOWN state, and may cheaply guess.

    The two callers here are the two that ACT on the answer:
    ``_promote_staging_replace``, whose wrong guess is DESTRUCTIVE -- it goes on to
    delete the very package the sidecar lives in, frozen text included -- and
    ``tool_builder.run_revise``'s entry gate (R6-3), whose wrong guess is
    EXPENSIVE: it admits a package whose already-corrupt sidecar makes the promote
    refusal a foregone conclusion, then spends a full multi-round builder session
    holding the global single-flight before collecting it, once per retry. Both
    therefore fail CLOSED on uncertainty, and they report it with the SAME two
    error strings so one condition never grows two vocabularies.

    The ENOENT-vs-every-other-``OSError`` discrimination is the same one
    ``tool_builder._read_env_for_values`` makes about the ``.env`` (R2-3), for the
    same reason: a failure to LOOK is not evidence of absence.

    The ``lstat`` and the read are two syscalls, so a sidecar deleted BETWEEN them
    answers UNKNOWN rather than None. That direction is the safe one (it refuses),
    it takes a concurrent delete of the package to reach at all, and the opposite
    direction -- a sidecar created in between -- simply reports the real status.
    Both sit in the check-then-act residual class D40 already accepts.

    The SECOND element is the meta this call actually parsed, and it exists so the
    caller does not have to read the same file again to use its other contents
    (R4-2). ``_promote_staging_replace`` needs the sidecar's ``origin`` -- the only
    copy of the OpenAPI url and the operator's install instructions -- and it needs
    it from a read it can TRUST: a second, total read (``read_tool_meta`` alone,
    which answers None for absent and unreadable alike) turned a transient EIO into
    ``origin=None``, after which the swap destroyed the sidecar and the
    regenerated one carried the loss forever. Handing back what was just judged
    makes that impossible by construction rather than by a second discrimination
    kept in step with this one. The entry gate DISCARDS it, and should: minutes
    pass before the swap acts, so the origin must come from the read the swap
    itself makes, not from this one.

    The meta is None on BOTH failure answers, deliberately: for ENOENT there is
    nothing to hand back, and for UNKNOWN the whole verdict is "this file cannot be
    trusted" -- returning its contents anyway would invite a caller to use what
    this function just refused to believe.
    """
    try:
        os.lstat(directory / _AI_META_FILENAME)
    except FileNotFoundError:
        return None, None
    except OSError:
        return _SUMMARY_STATUS_UNKNOWN, None
    meta = read_tool_meta(directory)
    status = _narrowed_summary_status(meta)
    if status is None:
        return _SUMMARY_STATUS_UNKNOWN, None
    return status, meta


def set_summary_status(name: str, status: str) -> str:
    """Set the sidecar's ``status`` (定版 / 解除定版). Returns the outcome code.

    Three outcomes, mapped by the router onto three HTTP answers:

    * ``"not_found"`` -- the name is unsafe, the package is missing, the
      directory is an internal ALIAS, the feature is off (all of them
      ``_resolve_package_dir_no_alias`` -> None), OR the rewrite itself failed.
      That last one is folded in DELIBERATELY, exactly as ``set_enabled`` folds
      every did-not-happen case into one False: from the caller's view the
      addressable resource did not (usably) change;
    * ``"no_meta"`` -- there is nothing to freeze. TWO cases, deliberately one
      answer: the package has no readable sidecar at all, OR it has one whose
      ``summary`` is absent/empty/whitespace. 定版 means "freeze THIS
      explanation"; freezing an absent explanation is the same "there is nothing
      there" as having no sidecar, and it is not harmless -- a finalized empty
      summary then blocks ``regenerate`` with ``tool_finalized``, so the one
      action that could FILL it is refused until the user thinks to un-finalize.
      A distinct 409 rather than a 404: the TOOL exists either way;
    * ``"ok"`` -- the sidecar now HAS the requested status. Either it was
      rewritten (a real transition), or it already held that status and nothing
      was written at all -- see the idempotent-retry rule below.

    IDEMPOTENT RETRY (R7-2): a request whose target status is ALREADY the status
    on disk returns ``"ok"`` without touching the file, in BOTH directions
    (final->final and draft->draft). The client that resends a PATCH after a lost
    response must not be punished for it, and a same-status rewrite has literally
    nothing legitimate to do -- the write only ever carries ``status`` (already
    equal) and ``updated_at`` (a fact nobody asked to change).

    What it did instead was violate "finalized text is immutable while
    finalized". The rewrite goes back through ``write_tool_meta``, which
    re-redacts every text field against TODAY's secret set -- and that set GROWS
    (another tool's install registers a new ``.env`` value). So a second
    final->final PATCH could rewrite the frozen summary the moment some unrelated
    value happened to appear inside it: the operator froze one explanation and a
    retry silently replaced part of it with a redaction marker. Refreshing
    ``updated_at`` on a no-op was the same lie in miniature. The write is
    RESERVED for actual transitions; both directions are short-circuited for
    symmetry, since draft->draft has exactly the same nothing to do.

    Ordering is deliberate: the short-circuit sits AFTER the emptiness gate, so a
    finalized-but-empty sidecar still answers ``"no_meta"`` to a repeat 定版
    rather than being newly promoted to ``"ok"``. That answer is what this
    function gives today (the gate already returns before any write), and this
    fix is about removing a WRITE, not about relaxing a gate.

    The emptiness gate applies ONLY when the target is ``"final"``. Setting
    ``"draft"`` stays unconditional on purpose: 解除定版 is the escape hatch out
    of a frozen state, and an escape hatch that can itself be refused is not one.

    That unconditional-ness had a hole (R6-2): ``write_tool_meta`` refuses to
    write ANY non-``str`` ``summary`` (its own contract), and a hand-corrupted
    sidecar -- ``{"summary": 123, "status": "final"}``, the sidecar being a
    plain JSON file the operator is explicitly allowed to hand-edit -- carried
    that refusal straight through a 解除定版 attempt: the rewrite below got a
    ``meta`` whose ``summary`` was still the bare ``int``, ``write_tool_meta``
    returned False, and THIS function's own not-a-real-change fold turned
    "un-finalize a broken sidecar" into ``"not_found"`` -- 404ing the one
    mutation that exists to recover from exactly this corruption, with no
    other way to reach it through the API at all. A non-``str`` summary is now
    coerced to ``""`` in the rewrite payload before it ever reaches
    ``write_tool_meta``: the sidecar is ours to rebuild, so a corrupt TYPE
    degrades to "no summary yet" (the same shape a missing one gets) rather
    than bricking the STATUS mutation. The escape hatch outranks type
    strictness for the one field the user cannot repair through the API any
    other way. This cannot reopen 定版 as a back door: the emptiness gate
    above already requires a non-empty ``str`` summary BEFORE this coercion
    ever runs (and returns ``"no_meta"`` first if that fails), so a
    corrupt-typed summary coerced to ``""`` still cannot be finalized -- it
    re-hits ``"no_meta"`` on the very next 定版 attempt -- only 解除定版 was
    ever blocked, and only 解除定版 is fixed. The coercion is also a
    structural no-op for every ALREADY-valid sidecar (a real ``str`` summary,
    corrupt or not, is left exactly as read), so this changes nothing for the
    byte-unchanged common case.

    ``status`` is trusted to be one of ``_SUMMARY_STATUSES``: the PATCH schema's
    ``Literal`` is the gate, the same way ``set_enabled`` trusts its bool. The
    rewrite goes through ``write_tool_meta``, so the sidecar is rebuilt from the
    known schema and re-redacted on the way back out -- a status flip can never
    un-mask a value that a newly registered secret would now match. It also means
    any EXTRA key a hand-edited sidecar carried is dropped by this write; see
    ``write_tool_meta``'s consequence note (round-tripping them was never a
    contract).

    Resolved through ``_resolve_package_dir_no_alias``: a 定版 addressed at
    ``tools/alias`` must not freeze the REAL package's summary (see that
    helper).

    The read-check-write runs entirely under ``_META_LOCK``, which is what makes
    the emptiness gate and the flip one decision instead of two: this runs on a
    threadpool worker, and a regenerate's store runs on ANOTHER one, so without
    the hold the two interleave and one of them writes a state neither ever saw.
    The RESOLVE stays outside the hold -- it is ordinary path work, not part of
    the sidecar's read-modify-write, so there is no reason to serialize it.

    What ``_META_LOCK`` does NOT serialize is a revise's promote or a delete: both
    are DIRECTORY operations that take no sidecar lock (see that lock's own
    comment for why a file-sequence lock is the wrong instrument for them). So the
    package this PATCH resolved can be swapped for a same-named one while the read
    above is in flight, and A's meta -- its text, its status, its origin -- would
    then be written into B and frozen there, after which B's own summary hook
    refuses to update the sidecar it does not recognize. The identity is therefore
    CAPTURED at the resolve, next to the path it describes, and re-checked at the
    only instant that settles it, the line above ``os.replace``
    (``_write_sidecar_atomic``). A mismatch is a failed write, which this function
    already folds into ``"not_found"`` -- honest either way: the tool the caller
    addressed is not the tool at that path any more.

    An unreadable identity (no ``tool.json`` to lstat) is likewise ``"not_found"``,
    the same "a check that cannot speak must not vouch" rule ``store_summary_meta``
    and ``_make_handler`` apply. Stated cost, and how it sits with the escape
    hatch above: 解除定版 stays unconditional on the SIDECAR -- an empty, corrupt
    or corruptly-TYPED summary can still be un-frozen, which is the whole class
    that gate was written for -- but a package whose MANIFEST cannot be read is
    not addressable at all, and that has always been a ``"not_found"`` here (the
    resolve above answers the same way for a missing directory). Such a package is
    invalid on the listing, cannot be executed, cannot be revised, and cannot have
    a summary generated for it (D40 r5 O5-3 accepted that last one); the frozen
    text on it describes a tool that will not run either way, and deleting it
    still works.
    """
    directory = _resolve_package_dir_no_alias(name)
    if directory is None:
        return "not_found"
    identity = package_identity(directory)
    if identity is None:
        return "not_found"
    with _META_LOCK:
        meta = read_tool_meta(directory)
        if meta is None:
            return "no_meta"
        if status == "final":
            summary = meta.get("summary")
            if not (isinstance(summary, str) and summary.strip()):
                return "no_meta"
        # R7-2: the sidecar already holds the requested status, so this is an
        # idempotent retry and there is nothing to write. Returning BEFORE the
        # payload is built is the point: the rewrite would re-redact the frozen
        # text against a secret set that has grown since finalization, and would
        # refresh updated_at for a change nobody made (see the docstring).
        if meta.get("status") == status:
            return "ok"
        # R6-2: coerce a non-str summary to "" before it ever reaches
        # write_tool_meta, whose own contract refuses to write one. A no-op
        # on the "final" branch above (that check already forced summary to a
        # non-empty str, or returned "no_meta" first) -- see the docstring's
        # escape-hatch note for why this must fire unconditionally on every
        # other status rather than only when the write would otherwise fail.
        if not isinstance(meta.get("summary"), str):
            meta["summary"] = ""
        meta["status"] = status
        meta["updated_at"] = datetime.now(UTC).isoformat()
        # The identity goes with the write, not before it: the guard belongs on
        # the line above the publish (see the docstring and _write_sidecar_atomic).
        return "ok" if write_tool_meta(directory, meta, expected_identity=identity) else "not_found"


def store_summary_meta(
    directory: Path,
    *,
    summary: str,
    origin: dict[str, Any] | None,
    llm_log_id: int | None,
    expected_identity: tuple[int, int, int] | None,
) -> tuple[str, dict[str, Any] | None]:
    """Merge a freshly generated summary into the sidecar. Returns ``(outcome, meta)``.

    The OTHER compound sidecar operation, and ``set_summary_status``'s
    counterpart: the whole read (status/origin inheritance) + finalize refusal +
    write + post-write re-read happens under ONE ``_META_LOCK`` hold, so a 定版
    can neither land inside it nor be undone by it. It lives HERE rather than in
    ``tool_meta`` because the lock and the sidecar helpers do
    (``tool_meta._store_meta`` is now a thin mapping wrapper); putting the
    critical section next to the file it protects is what keeps "every compound
    sidecar operation is serialized" checkable by reading one module.

    Three outcomes, so the caller can tell "you cannot do this" from "it did not
    work" -- the distinction the route turns into a 409 vs a 404:

    * ``("finalized", None)`` -- the status on disk is ``final``. NOTHING is
      written. Both regeneration callers check 定版 UP FRONT, but that check
      happens before an ``await`` that lasts as long as an LLM round trip and a
      PATCH can finalize inside that window, so the up-front gate is a courtesy
      and THIS is the one that holds. Preserving the ``final`` status while still
      overwriting the summary TEXT (what this did before) satisfied the letter of
      定版 and broke its meaning: the operator froze an explanation and got a
      different one;
    * ``("not_stored", None)`` -- the write was refused (the package is no longer
      the one this summary was generated for, a fail-closed redaction, the ghost
      guard on a racing delete, a symlinked/non-regular sidecar, a payload past
      the size cap), or it landed and the re-read still found nothing (a delete
      racing in behind it). One did-not-happen answer, because from the caller's
      view nothing usable is on disk either way;
    * ``("ok", meta)`` -- the sidecar AS IT NOW READS BACK. Re-reading rather
      than returning the composed dict is not belt-and-braces: ``write_tool_meta``
      rebuilds the file from its OWN schema (a narrowed ``origin``, coerced
      scalars, redacted text), so what we asked for and what landed genuinely
      differ in shape, and the synchronous route promises "what the next GET
      would show".

    Past the finalize gate, two fields are PRESERVED from what is already on disk
    rather than reset:

    * ``status`` -- a regenerate on a 草稿 stays 草稿. With ``final`` refused
      above, ``"draft"`` is the only value that can actually survive today; the
      preserve-known-else-draft shape is kept anyway so this stays correct if the
      vocabulary ever grows, and so an unknown value on disk is never persisted;
    * ``origin`` -- only the install captures the OpenAPI URL and instructions,
      so a caller with nothing to pass (``origin=None``) must inherit them
      instead of erasing the only copy.

    ``summary`` is REDACTED, stripped and capped HERE, and this is the only place
    that happens (D40 r4). It used to happen in ``ToolSummaryResult``'s pydantic
    validator -- i.e. inside ``generate_structured``'s ``model_validate``, which
    runs ON THE EVENT LOOP, while ``redact_known_secrets``'s provider sweeps the
    tools directory (an ``iterdir`` + a ``stat`` per package, plus a ``.env`` read
    on every cache miss). That is blocking filesystem work on the loop, in a
    feature whose every other filesystem step is deliberately hopped onto a
    threadpool worker; THIS function is already on one, and already the sidecar's
    write path. The ORDER is preserved exactly as it was: redact while the text is
    still WHOLE, then strip, then slice. Each step earns its place --
    * redact first, because a value straddling the slice edge must be masked
      before the cut, or the cut leaves an interior fragment no later pass can
      match;
    * strip after the redaction, not before, because the redactor matches the
      REGISTERED value against untouched text: a secret whose value carries edge
      whitespace (a hand-edited ``.env`` with a quoted ``" secret-token "``)
      stops matching the moment ``strip`` eats that edge, and the rest of the
      value would ride to disk unmasked;
    * the cut last, a bare slice with no marker, exactly as before.

    ``write_tool_meta`` still redacts every text value it writes and remains the
    LAST choke point for other callers (``set_summary_status``'s round-trip, any
    future programmatic write); running over already-masked text is a no-op, since
    the marker carries nothing to match. A redaction FAILURE here is the same
    fail-closed refusal it is there -- ``("not_stored", None)``, never an
    exception -- because ``regenerate_summary``'s route maps this return, and a
    raised provider error would turn a 404 into a 500.

    ``expected_identity`` is the caller's ``package_identity`` of ``directory``,
    taken when it RESOLVED that directory, and carried down to the line above
    ``os.replace``. It is required rather than defaulted because the whole
    hazard is a caller forgetting it: a summary generation resolves the package,
    spends an LLM round trip, and then writes -- so a delete plus a reinstall of
    the same NAME inside that window would otherwise persist package A's summary,
    and A's origin, into package B. Refusing is ``("not_stored", None)``: the
    generation did not happen as far as any package on disk is concerned, which
    is the answer a vanished package already produces and which both callers
    already handle (the install hook swallows it, the synchronous route folds it
    into its did-not-happen 404). None is likewise a refusal -- the same
    "a check that cannot speak must not vouch" rule the revise flow (D40 P3b r11)
    and ``_make_handler`` apply to this identity, and one that costs nothing real:
    a package with no readable ``tool.json`` cannot be executed or revised either.

    Where the COMPARISON happens moved in R6-2: it used to be the last line of
    this function, which read as "the last instant" but is not one --
    ``write_tool_meta`` still had a redactor sweep of the tools directory, a JSON
    encode, an mkstemp, a write and an fsync to do before it published anything,
    and a promote takes no ``_META_LOCK``, so a package swapped inside THAT window
    received this summary. The identity is now handed down and checked on the line
    above ``os.replace``. What stays here is the None refusal: "the caller has no
    identity" is a property of the call rather than a race, so it is answered
    where the vocabulary for it lives, and in the same slot it always occupied so
    the outcome ORDER is unchanged (a finalized package still answers
    ``finalized``, a redaction failure still answers ``not_stored``).

    The stated cost of that order: if the package that TOOK the name is itself
    finalized, the caller is told ``finalized`` rather than ``not_stored``.
    Nothing is written either way, and both mean "your summary was not stored";
    only the code differs, and it is honest about the package that now holds the
    name.

    ``llm_log_process`` is stamped HERE, and only here, from
    ``llm_log.process_token()``. It is the token of the process whose id space
    ``llm_log_id`` was drawn from, and taking it at the store rather than
    accepting it as a parameter is what makes that structurally true instead of
    merely conventional: the caller read the id from ``last_record_id_for_workflow``
    microseconds ago, in THIS process, so there is no arrangement of arguments
    that can pair an id with someone else's token. It is stored only WITH an id
    -- no id, no token -- so the two fields can never disagree about whether
    there is a link to vouch for.
    """
    with _META_LOCK:
        existing = read_tool_meta(directory) or {}
        if existing.get("status") == "final":
            return ("finalized", None)
        # AFTER the finalize gate, so a refusal stays a refusal (the 409 outranks
        # a redaction failure's 404) and a frozen package costs no sweep at all.
        # Inside the hold introduces NO new locking property: write_tool_meta runs
        # the identical redaction under this same lock a few lines down.
        try:
            summary = redact_known_secrets(summary).strip()[:_TOOL_SUMMARY_CAP]
        except Exception:
            # Fail-closed, matching write_tool_meta's own guard: nothing is
            # written rather than something unmasked (see redact_known_secrets --
            # the LIVE path propagates a provider failure instead of degrading).
            return ("not_stored", None)
        status = existing.get("status")
        if not (isinstance(status, str) and status in _SUMMARY_STATUSES):
            status = "draft"
        meta: dict[str, Any] = {
            "summary": summary,
            "status": status,
            "updated_at": datetime.now(UTC).isoformat(),
            "llm_log_id": llm_log_id,
            "llm_log_process": llm_log.process_token() if llm_log_id is not None else None,
            "origin": origin if origin is not None else existing.get("origin"),
        }
        # "Nothing to assert" is refused here; "is it still the same package" is
        # asserted at the publish, where the answer cannot go stale before it is
        # used (see this function's docstring and _write_sidecar_atomic).
        if expected_identity is None:
            return ("not_stored", None)
        if not write_tool_meta(directory, meta, expected_identity=expected_identity):
            return ("not_stored", None)
        stored = read_tool_meta(directory)
        return ("ok", stored) if stored is not None else ("not_stored", None)


def _listed_summary_status(directory: Path) -> str | None:
    """``summary_status`` for one LISTED row -- None when the row is an ALIAS.

    ``tools/<alias> -> tools/<real>`` is an internal symlink, and every by-name
    summary route refuses it (``_resolve_package_dir_no_alias``: a GET/PATCH/
    regenerate addressed at the alias must not read or freeze the REAL package's
    sidecar). The LISTING was the one place still reading through it: the scan
    resolves nothing, so ``summary_status(scan.directory)`` followed the link and
    reported REAL's status on the alias row -- a row whose 已定版 badge no summary
    request can then reproduce, since every one of them 404s the name. The badge
    was describing a different package than the row it sat on.

    None instead, which is exactly what the row would report if the alias were an
    ordinary package with no sidecar -- the same "nothing to show here" the
    refusing routes give. The row itself is unaffected and stays ``valid=False``
    (``_scan_package`` refuses a symlinked package directory outright), so this
    only removes the read-through, not the row.
    """
    if directory.is_symlink():
        return None
    return summary_status(directory)


def list_tools() -> list[dict[str, Any]]:
    """List every installed package as a UI-facing summary.

    Each entry is ``{name, description, enabled, valid, error, summary_status}``.
    A broken package (bad JSON, name mismatch, bad schema shape, missing entry
    file) is included with ``valid=False`` and a safe ``error`` reason, and is
    never executable; a missing/unset tools dir yields [].

    ``summary_status`` (D40) is read from each package's sidecar HERE rather
    than through a per-tool request, so the 工具 page can badge 草稿 / 已定版 on
    every row from the one listing call it already makes. It costs one small
    bounded read per package, on the same scan that already reads every
    ``tool.json``; a package with no (or a corrupt) sidecar reports None, and so
    does an internal ALIAS row (see ``_listed_summary_status``).
    """
    return [
        {
            "name": scan.name,
            "description": scan.description,
            "enabled": scan.enabled,
            "valid": scan.valid,
            "error": scan.error,
            "summary_status": _listed_summary_status(scan.directory),
        }
        for scan in _scan_all()
    ]


def package_identity(directory: Path) -> tuple[int, int, int] | None:
    """The package's MANIFEST identity: ``(st_dev, st_ino, st_ctime_ns)`` of its
    ``tool.json``, or None when it cannot be read.

    ONE of TWO identities this module keeps, and the one that answers "is the
    package at this path still the ONE I looked at?" -- see ``directory_identity``
    below for the other question ("is anything still running out of these
    files?"), which needs a different answer and therefore a different tuple.

    Three callers, all asking THIS question across a window they do not hold a
    lock over:

    * ``tool_builder.run_revise`` takes it when the session reads the package and
      again immediately before the swap, so a multi-minute build cannot publish
      itself over a package the operator replaced meanwhile;
    * ``_make_handler`` takes it when a package is turned into an ``LlmTool``
      (i.e. when its schema is ADVERTISED to the model) and again before the
      subprocess starts, so a revise that replaced the package mid-conversation
      cannot have the model answer against the old schema while the NEW entry
      runs;
    * ``store_summary_meta`` takes it when the directory a summary is being
      generated FOR is resolved, and again in the instant before the sidecar is
      written, so an LLM round trip cannot end with one package's summary landing
      in another package that took its name meanwhile.

    Two spellings of the same tuple would be two chances to drift, which is why
    this lives here (``tool_builder`` already imports this module, so this is the
    direction that does not create a cycle) rather than being restated per caller.

    The MANIFEST rather than the directory, and that choice is the whole design.
    Two weaker readings were measured and discarded:

    * the directory's inode alone does not answer "is this the same package": a
      delete-and-reinstall of the same name REUSES the inode on an ordinary Linux
      filesystem (measured here, not assumed -- the first version of this check
      was written against the opposite assumption and silently passed the exact
      scenario it exists to refuse);
    * the directory's inode plus its ctime/mtime DOES catch the reinstall, but it
      also fires on any change to the directory's CONTENTS -- and that contradicts
      an earlier adjudication the revise flow already implements: an operator
      deleting the package's ``.env`` mid-session is HONORED (D40 r3), not
      refused. A check that cannot tell "replaced" from "edited" would have to
      break one of the two.

    ``tool.json`` separates them cleanly: every install and reinstall WRITES it (it
    is the one file ``validate_package`` requires), so a package that was replaced
    carries a different one; deleting or editing some OTHER file in the package
    leaves it untouched. Editing the manifest ITSELF in place is then treated as a
    replacement, which is the right side to err on -- that is the file a revision
    rewrites, and the file ``set_enabled`` rewrites for the enabled toggle (see
    ``_make_handler`` for what that costs an in-flight conversation).

    ``lstat``, so a manifest swapped for a symlink compares different rather than
    reporting on its target. None on any error: every caller treats "cannot say"
    as "this check cannot speak", never as "identity matches".
    """
    try:
        info = os.lstat(directory / "tool.json")
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_ctime_ns)


def directory_identity(directory: Path) -> tuple[int, int] | None:
    """The DIRECTORY's own identity: ``(st_dev, st_ino)``, or None when unreadable.

    The other half of ``package_identity``, and a DIFFERENT question on purpose:
    that one asks "is this still the same PACKAGE?", this one asks "is anything
    still running out of these FILES?". One tuple cannot answer both, because the
    two must survive different things:

    * the manifest identity MUST move when ``tool.json`` is rewritten -- that is
      exactly how a revision, a reinstall, and (accepted, stated in
      ``_make_handler``) an ``enabled`` toggle are told apart from "unchanged";
    * an execution's lifetime must survive EVERY edit inside the directory, that
      toggle included. Keying ``_INFLIGHT_EXECUTIONS`` on the manifest made a
      handler that registered before a toggle invisible to the delete or promote
      that queried after it -- and the removal each of them defers for exactly
      this reason then landed on files a child was still reading. The split
      lasted from the toggle until the child exited, not a syscall pair.

    MEASURED here rather than assumed (Linux/ext4, this repo's own filesystem):

    * ``set_enabled``'s in-place manifest rewrite leaves this tuple UNCHANGED
      while the manifest identity moves (the rewrite pushes ``tool.json``'s
      ctime);
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
        info = os.lstat(directory)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _build_llm_tool(scan: _PackageScan) -> LlmTool:
    """Turn a valid ``_PackageScan`` into an executable ``LlmTool``.

    Callers pass only ``valid`` scans, so ``parameters`` / ``entry`` are present;
    the assertions document that precondition and keep the type checker happy
    without an ``Any`` escape hatch.

    The package's manifest identity is captured HERE, in the same call that
    freezes the spec, because this is the moment the schema below becomes a
    PROMISE to the model -- see ``_make_handler`` for what is done with it. The
    instant between ``_scan_package``'s READ of ``tool.json`` and this ``lstat``
    of it is the one thing the pairing cannot cover (a swap landing exactly there
    pins the NEW identity against the OLD spec); that is one syscall pair wide and
    the same check-then-act residual this subsystem accepts throughout.
    """
    assert scan.parameters is not None
    assert scan.entry is not None
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
        handler=_make_handler(scan.directory, scan.entry, package_identity(scan.directory)),
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


def _cached_env_values(directory: Path) -> frozenset[str]:
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
    env_file = directory / ".env"
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
    values = frozenset(value for value in _load_tool_dotenv(directory).values() if value)
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
                secrets |= _cached_env_values(child)
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


def _load_tool_dotenv(directory: Path) -> dict[str, str]:
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
    env_file = directory / ".env"
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


def _build_tool_env(directory: Path) -> tuple[dict[str, str], frozenset[str]]:
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
    dotenv = _load_tool_dotenv(directory)
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


def _run_tool_subprocess(
    entry: list[str],
    directory: Path,
    expected_identity: tuple[int, int, int] | None,
    env: dict[str, str],
    args_json: str,
    timeout: float,
    output_cap: int,
) -> str:
    """Run one tool entry to completion (or timeout) and return its result STRING.

    Blocking; the async handler runs it via ``run_in_threadpool``. Every outcome
    is an agent-visible string, never an exception:

    * the package at ``directory`` is no longer ``expected_identity`` ->
      ``_TOOL_REPLACED_RESULT``, nothing is started;
    * cannot even start (bad interpreter/entry, no exec bit) -> a "failed to
      start" category;
    * exceeded ``timeout`` -> the process GROUP is SIGKILLed and a "timed out"
      message returned;
    * non-zero exit -> ``"tool failed (exit N): <stderr>"`` (stderr size-capped);
    * success -> stdout, size-capped at ``output_cap``.

    ``shell=False`` (argv list, never a shell string) so nothing in the model's
    arguments or a tool name can be shell-injected. ``errors="replace"`` on the
    text pipes keeps a tool that emits invalid UTF-8 from crashing the reader.
    Output is drained by ``_communicate_bounded`` (NOT ``communicate``), which
    caps each stream in memory as it reads rather than slurping it whole first --
    a runaway tool is killed at the cap instead of OOMing the service.

    ``expected_identity`` is the manifest identity of the package whose schema the
    model was shown, and it is verified HERE, on the line above ``Popen``, rather
    than only in the handler that queued this call (R6-1). ``cwd`` is resolved by
    the KERNEL, at exec time, from the PATH -- so a revise that publishes a new
    package at that path while this call is still queuing or reading its ``.env``
    would start the NEW package's entry file carrying the OLD entry argv, the old
    schema's arguments and the old package's environment values. The handler's own
    check cannot answer for that: between it and this line sit a ``.env`` read, a
    JSON serialization and a threadpool queue wait of unbounded length. What
    remains after this check is the lstat/exec pair -- the check-then-act instant
    this module accepts by name -- and the execution registration the handler took
    BEFORE this call means the package cannot have been destroyed in it, only
    renamed (measured: a rename is invisible to a running child).
    """
    if not _still_the_expected_package(directory, expected_identity):
        return _TOOL_REPLACED_RESULT
    try:
        proc = subprocess.Popen(
            entry,
            cwd=directory,
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


# In-flight tool EXECUTIONS, keyed by the DIRECTORY identity ``_make_handler``
# reads as the FIRST thing a call does and registers on the next line
# (``directory_identity``), and COUNTED rather than flagged: two workflows can
# call the same tool at once, and the first to finish must not cancel the
# second one's protection.
#
# The DIRECTORY and not the manifest, and that is the whole reason this key has
# its own function. Both questions used to be answered by ``package_identity``,
# and ``set_enabled`` splits them apart: it rewrites ``tool.json`` in place, so a
# handler that registered under the pre-toggle manifest identity was INVISIBLE to
# a delete or a promote reading the post-toggle one -- which then destroyed the
# package while that child was still running out of it. That split persists from
# the toggle until the child exits, so it is not the syscall-pair instant this
# module accepts elsewhere. A registration is a claim about FILES, and it has to
# outlive every edit to a file inside them (measured: it does).
#
# Why the identity check alone is not enough: it protects the START of a call,
# not its DURATION. The child then runs for up to ``llm_tool_timeout_seconds``
# with its cwd on the package directory, and a revise promoted during that
# window renames that directory aside and then DELETES it. MEASURED rather than
# assumed (Linux/ext4, a child holding cwd in a directory that is renamed and
# then removed under it):
#
# * the rename-aside disturbs NOTHING -- a running process's cwd is a reference
#   to the INODE, so relative opens and lazy imports keep working, they just
#   resolve under the backup's hidden name;
# * the ``rmtree`` that follows is the whole hazard -- after it, every relative
#   open fails with ENOENT (so does ``getcwd``), and only descriptors the child
#   had ALREADY opened keep reading.
#
# So the fix is not to make the promote wait (a revise the operator asked for
# must never be blocked by a tool call) but to keep the removal off a package
# something is still executing against: ``tool_builder._promote_staging_replace``
# consults ``directory_execution_in_flight`` below and defers the backup to a
# later sweep when the answer is yes. The backup name is dot-prefixed and
# invisible to ``_scan_all``, so a deferred one is inert litter, never a phantom
# package.
#
# Its own lock, exactly like ``_INFLIGHT_SECRETS``: registration happens on the
# event loop while the query runs on a threadpool worker (the promote's own hop).
_INFLIGHT_EXECUTIONS: dict[tuple[int, int], int] = {}
_EXECUTION_LOCK = threading.Lock()


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
    """True while a tool subprocess may still be reading THAT DIRECTORY's files.

    The one question the three destructive callers ask before removing a
    directory: ``tool_builder._promote_staging_replace`` before dropping the
    backup it renamed aside, ``delete_tool`` before removing what the operator
    asked it to, and ``tool_builder._sweep_stale_backups`` before collecting
    either one's deferred remains (see ``_INFLIGHT_EXECUTIONS`` for what a removal
    does to a running child, and why the rename before it does not).

    Keyed on the DIRECTORY identity, which is what makes the two sides line up
    without sharing a path or a lock: each caller re-derives it from the directory
    it is HOLDING -- after the rename, from disk, possibly in a later process --
    and a rename carries ``(st_dev, st_ino)`` unchanged (measured), so the tuple
    the handler registered is still the tuple that names those files. The
    manifest identity cannot do this job: ``set_enabled`` moves it under a running
    child, and the two sides would then be comparing different answers to
    different questions.
    """
    with _EXECUTION_LOCK:
        return identity in _INFLIGHT_EXECUTIONS


# The DEFERRED-REMOVAL namespace: where a package goes when it must stop being a
# package NOW but cannot be destroyed yet, because a subprocess is still reading
# it. ``.{name}.stale-<token>`` means "superseded, collect when idle", and it is
# the only shape ``tool_builder._sweep_stale_backups`` will ever remove.
#
# TWO writers mint this name, for the same reason and with the same guarantee:
# ``tool_builder._promote_staging_replace`` (a revise published, its backup is
# litter) and ``delete_tool`` below (the operator removed the tool). Both would
# otherwise ``rmtree`` a directory a child has its cwd on. It lives HERE rather
# than in tool_builder -- where the sweep and the ``.bak-`` rescue name still
# live -- for the ONE reason ``_INFLIGHT_SECRETS`` does: this module cannot
# import tool_builder (tool_builder imports us), so a shared definition can only
# sit on this side, and two spellings of a name one side WRITES and the other
# READS OFF DISK is exactly the drift that would make the sweep stop finding its
# own litter.
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
    directory: Path, entry: list[str], identity: tuple[int, int, int] | None
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Build the async handler an ``LlmTool`` runs, closing over its package.

    Settings (timeout, output cap) and the child environment are read at CALL
    time, not build time, so a settings override or an edited ``.env`` is
    honored on the next invocation. The blocking subprocess is off-loaded to the
    threadpool (the house pattern), so a slow tool never sits on the event loop.
    The handler honors ``LlmTool``'s no-raise contract: ``_run_tool_subprocess``
    turns every failure into a string, and the llm loop's own ``except`` is the
    final backstop.

    ``identity`` is that package's manifest identity as of the moment its schema
    was advertised (``_build_llm_tool`` -> ``package_identity``), and the FIRST
    thing this handler does is take it again and REFUSE on any difference. What
    that closes: ``enabled_llm_tools()`` snapshots each tool's name, description,
    parameters and entry at the START of a capture / enrich / assist-update, but
    resolution and execution happen from the PATH, minutes later, when the model
    finally calls it -- and the ordinary AI workflows are not inside the tool
    job's single-flight, so a revise can replace the package underneath a
    conversation that already advertised the old one. The model would then be
    answering against a schema the running entry no longer implements, and the
    replace-mode promote's backup cleanup could delete files the process it just
    started is still reading. Neither is visible after the fact: an attempt's
    ``tools_advertised`` records NAMES, and the name did not change.

    Refusing is the only honest answer, and a refusal STRING is the only shape
    allowed here (``LlmTool``'s handler contract is no-raise): the model gets a
    category-only sentence, is free to try something else, and nothing runs.

    ORDER, which is the whole of R6-1: the DIRECTORY identity is read first and
    registered on the very next line, then everything else happens INSIDE that
    registration -- the manifest check, the ``.env`` read, the argument
    serialization, the threadpool hop. It used to be the other way round (check,
    read, serialize, and only then register), which left a window holding a file
    read and an unbounded JSON encode between "this is the directory I looked at"
    and "this directory is protected". A promote landing in it saw no registration,
    published its new package at the path and dropped the backup -- and this call
    then started a child whose ``cwd`` resolved to the NEW package while carrying
    the OLD entry, schema and environment values. Registering first inverts that:
    the hold is published before anything can be observed to be missing, and every
    check that has to answer "still the same package?" is re-taken after it,
    ending on the line above ``Popen`` (``_run_tool_subprocess``).

    The price of registering before the checks, stated rather than discovered: a
    call that goes on to REFUSE holds an execution registration for the length of
    those checks, so a promote or delete racing it may DEFER its removal instead
    of taking it. That costs one marked, hidden directory collected by the next
    tool job's sweep (see ``_STALE_BACKUP_RE``) -- inert litter, never a phantom
    package, and the trade is deliberate: the opposite mistake destroys a package
    a child is reading.

    The manifest check still sits BEFORE ``_build_tool_env``. That read pulls the
    package's ``.env`` -- live credentials -- and there is no reason to load a
    replaced package's secrets into a call we are about to refuse; it also means
    the ``.env`` this handler exports belongs to the package the check accepted.

    A None ``identity`` -- the manifest could not be lstat'ed when the tool was
    advertised -- is a REFUSAL, never a pass, matching the revise flow's own
    "cannot establish identity" rule (D40 P3b r11): a check that cannot speak
    must not vouch. It is answered before the registration because it is a fact
    about the ADVERTISEMENT, not about the directory: no lstat can change it.

    Those checks answer for the START of a call. The RUN is covered by two
    registrations, released by a ``finally`` so no failure shape -- exception,
    timeout, cancellation -- can leak either one:

    * this package's DIRECTORY identity as an in-flight EXECUTION, so a promote
      (or a ``delete_tool``) landing mid-run keeps the files this child is still
      reading instead of deleting them under it (see ``_INFLIGHT_EXECUTIONS`` for
      what was measured about the rename and the removal). A SECOND identity, not
      the one checked above, and the difference is the point: the manifest
      identity answers "same package?" and therefore MOVES when ``set_enabled``
      rewrites ``tool.json``, which would strand this registration under a key
      nobody looks up. It is read HERE rather than captured with the advertised
      one because it is not a snapshot to compare against -- it is the key the
      protection is published under, so it must name the directory this call is
      about to run in, not the one that was there when the schema went out. Not
      being able to read it is a REFUSAL for the same reason the check above is:
      a hold nobody can see is a child nobody will defer for;
    * the ``.env`` VALUES ``_build_tool_env`` just handed the child as in-flight
      SECRETS, so the redaction that runs when the child finishes still knows the
      values it was given even if the file has been rotated, replaced or carried
      off by a deferred delete since (see ``_INFLIGHT_SECRETS``).

    FALSE POSITIVES, stated rather than discovered later: ``set_enabled`` rewrites
    ``tool.json`` IN PLACE to flip ``enabled``, which moves its ctime. So toggling
    a tool during a conversation that already advertised it makes every later call
    to that tool in that conversation refuse -- including a toggle OFF-then-ON that
    leaves the bytes identical. That is the conservative direction and it is
    cheap: an AI workflow is one request, the operator can re-run it, and the
    alternative reading ("the manifest was only rewritten, carry on") is exactly
    the one that cannot tell an enabled-flip from a wholesale replacement. Toggling
    a tool OFF mid-conversation and having it then refuse is arguably the more
    correct behaviour anyway -- today the snapshot happily runs a tool the operator
    just disabled. Writes that do NOT trip it: the summary sidecar
    (``.ai_meta.json`` is a different file, so the manifest's own ctime is
    untouched) and anything the tool itself writes into its package. What the
    false positive must NOT reach is the execution registration below -- a toggle
    that made a running child invisible to the promote about to delete its files
    would not be a costed refusal, it would be a broken call (see
    ``directory_identity``).
    """

    async def _handler(arguments: dict[str, Any]) -> str:
        if identity is None:
            return _TOOL_REPLACED_RESULT
        # FIRST, and registered on the very next line: the directory this call is
        # about to run OUT OF. Reading it here rather than at advertisement time is
        # deliberate (it is the key a protection is PUBLISHED under, not a snapshot
        # to compare), and nothing is allowed between the read and the hold -- see
        # the docstring's ORDER paragraph for what used to sit in that gap. Not
        # being able to read it is a refusal: a hold nobody can see is a child
        # nobody will defer for.
        running = directory_identity(directory)
        if running is None:
            return _TOOL_REPLACED_RESULT
        # The EXECUTION registration, under the DIRECTORY's identity, so a promote
        # or a delete landing from here on keeps the files this child may be
        # reading instead of removing them under it. It covers the checks below,
        # the ``.env`` read, the serialization AND the threadpool queue wait, and
        # it is ordered strictly before the ``Popen`` it protects: a promote that
        # observes no registration cannot have a child of ours running against the
        # package it is dropping.
        with _inflight_execution(running):
            # Re-taken INSIDE the hold: whatever moved in the instant before it was
            # published is caught here rather than carried into a call. The last
            # word on this question belongs to the line above ``Popen``.
            if not _still_the_expected_package(directory, identity):
                return _TOOL_REPLACED_RESULT
            settings = get_settings()
            env, env_secrets = _build_tool_env(directory)
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
                    directory,
                    identity,
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


def _resolve_package_dir_no_alias(name: str) -> Path | None:
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
    return directory if directory is not None and directory.is_dir() else None


def set_enabled(name: str, enabled: bool) -> bool:
    """Flip a package's ``enabled`` flag in its ``tool.json``. Returns success.

    False when the name is unsafe (see ``_resolve_package_dir``), the package
    directory is an alias (see below), the package or its ``tool.json`` is
    missing/unreadable/oversized, or the write fails -- so the caller (a future
    PATCH route) maps a bad name and a missing package alike to a clean "did not
    happen" rather than a 500.
    """
    base = tools_dir()
    if base is None or not _NAME_RE.match(name):
        return False
    # INTERNAL-alias hard-block (H3), BEFORE resolve: an internal symlink
    # ``tools/<name> -> tools/real`` RESOLVES inside the tools root, so the
    # resolve-then-contain check below would PASS and this toggle would rewrite
    # the REAL package's manifest THROUGH the alias. ``is_symlink`` does not
    # follow the final component, so it detects the alias itself; refuse outright
    # -- set_enabled has no safe action on an alias (reading or writing a manifest
    # through it is exactly the escape we are stopping), unlike delete_tool which
    # can at least drop just the dangling link. Checked before resolve precisely
    # because ``resolve()`` erases the alias/real distinction. External-symlink
    # escapes are still separately caught by the containment check further down.
    if (base / name).is_symlink():
        return False
    directory = _resolve_package_dir(name)
    if directory is None or not directory.is_dir():
        return False
    tool_json = directory / "tool.json"
    # Re-verify containment at the WRITE boundary: ``directory`` is already the
    # RESOLVED package dir, but ``tool.json`` could be a SYMLINK pointing outside
    # it, and ``write_text`` follows symlinks -- so a symlinked manifest would
    # otherwise let this rewrite an arbitrary file the service can reach. Require
    # the RESOLVED manifest path to stay inside the resolved package dir before
    # touching it (mirrors _resolve_package_dir's own resolve-then-contain, H3).
    # The scan already lists such a package invalid, but set_enabled must not
    # trust that -- it is reached directly by the mutation API.
    if not _is_within(directory, tool_json.resolve()):
        return False
    # Read the manifest through the ONE bounded-regular-file helper (F3b), exactly
    # as the scan does -- this is the FIFO fix for the write path. A plain
    # ``read_text()`` here would ``open(O_RDONLY)`` the manifest, and a FIFO
    # ``mkfifo``'d in place of tool.json (a tool package can ship one; run_shell can
    # create one) BLOCKS that open until a writer appears -- wedging the PATCH
    # worker FOREVER. The helper's O_NONBLOCK+S_ISREG gate refuses the FIFO at once,
    # its O_NOFOLLOW backstops the is_symlink() fast path above against a symlink
    # raced in after it, and its cap+1 read SUBSUMES the old manual stat size-cap:
    # an oversized manifest comes back longer than the cap and is refused here, the
    # SAME observable refusal in the SAME char unit the scan uses -- so no separate
    # stat is needed. None (every refusal/read failure) and an over-cap length BOTH
    # map to set_enabled's "did not happen" False contract.
    text = _read_regular_file_capped(tool_json, _MANIFEST_MAX_BYTES)
    if text is None or len(text) > _MANIFEST_MAX_BYTES:
        return False
    try:
        raw = json.loads(text)
    except ValueError:
        return False
    if not isinstance(raw, dict):
        return False
    raw["enabled"] = enabled
    # Refuse to WRITE if the re-serialized manifest would exceed the cap (N2).
    # ``indent=2`` pretty-prints, which EXPANDS a compact-but-legal manifest: a
    # manifest that sat just under the cap in its compact on-disk form can cross
    # it once pretty-printed, which would flip the tool ``valid=False`` on the very
    # next scan after a mere enable/disable toggle. Measure the ENCODED size (UTF-8
    # bytes, the on-disk unit _MANIFEST_MAX_BYTES bounds) and, when it would not fit,
    # leave the file byte-for-byte untouched and report failure rather than corrupt
    # the row.
    new_text = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    if len(new_text.encode("utf-8")) > _MANIFEST_MAX_BYTES:
        return False
    # Write the manifest back through the symmetric bounded WRITE helper (F3c). The
    # read above just confirmed tool_json is a regular file, so the static FIFO
    # hazard is already closed on this path; converting the WRITE too is symmetry +
    # defense in depth -- its O_NONBLOCK makes a reader-less FIFO raced into the path
    # fail with ENXIO instead of blocking, and its O_NOFOLLOW refuses a symlinked
    # leaf, so this rewrite can never escape the package. Its bool (True written /
    # False on any refusal) IS set_enabled's success/"did not happen" contract.
    return _write_regular_file(tool_json, new_text)


def delete_tool(name: str) -> bool:
    """Delete a package (``rmtree``), or an alias (``unlink``). Returns success.

    Name-validated and containment-checked exactly like ``set_enabled`` (the
    traversal hard-block is what makes an ``rmtree`` here safe), so it can only
    ever remove a directory that genuinely sits inside ``tools_dir``. False when
    the name is unsafe, the package is absent, or it could not be taken out of
    the registry at all.

    A package a subprocess is STILL EXECUTING against is left in the
    deferred-removal namespace instead of being destroyed, which is the same
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

    The remains are collected by the SAME sweep the promote's deferrals go
    through (``tool_builder._sweep_stale_backups``, at the end of every tool job),
    which re-derives everything it needs from disk. Residuals, stated rather than
    discovered later, and identical to the promote's: a pathologically long call
    postpones the collection to a later job, and a process that exits in between
    leaves the marked directory for the next run to sweep. Both are hidden, inert
    litter, never a phantom package.
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
    # registered, and it is the only one an ``enabled`` toggle mid-call cannot
    # move (see ``directory_identity``).
    #
    # "Cannot read it" DEFERS, the same direction the sweep takes and the
    # opposite of the manifest read this used to do. The old reasoning -- a
    # package whose manifest cannot be lstat'ed is one no handler can have
    # registered -- does not survive the move: an lstat failure on a directory we
    # just renamed successfully says nothing about what is running inside it, only
    # that we cannot name it. Nothing observable changes either way (the tool is
    # already out of the registry and this still returns True); what is left
    # behind is marked remains the next tool job's sweep re-derives from disk.
    identity = directory_identity(deferred)
    if identity is None or directory_execution_in_flight(identity):
        return True
    # Best-effort from here: the tool is already gone as far as everything that
    # reads this directory is concerned, so a removal that fails part-way must not
    # be reported as "did not happen" -- it leaves MARKED remains the sweep retries
    # on every later tool job, which is a self-healing residue rather than the
    # half-deleted, still-listed package a failing ``rmtree`` used to leave.
    shutil.rmtree(deferred, ignore_errors=True)
    return True
