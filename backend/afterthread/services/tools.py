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
  rewrite or ``rmtree`` a path outside the tools directory;
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
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from dotenv import dotenv_values
from starlette.concurrency import run_in_threadpool

from afterthread.config import get_settings
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
# * ``delete_tool``'s ``rmtree`` takes it with the package -- summary and tool
#   share one lifetime, which is exactly why this lives in the package rather
#   than in SQLite;
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

# The only two summary statuses that mean anything. "draft" is what generation
# writes; "final" (定版) is the operator freezing AI iteration on this tool --
# revise and regenerate both refuse a finalized package until it is un-finalized.
# Anything else on disk (a hand-edited sidecar, a future/older shape) reads as
# "no usable status" rather than being trusted, so the freeze can never be
# bypassed by writing a garbage value into the file.
_SUMMARY_STATUSES: tuple[str, ...] = ("draft", "final")

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
    """
    text = _read_regular_file_capped(directory / _AI_META_FILENAME, _AI_META_MAX_BYTES)
    if text is None or len(text) > _AI_META_MAX_BYTES:
        return None
    try:
        raw = json.loads(text)
    except ValueError, RecursionError:
        return None
    return raw if isinstance(raw, dict) else None


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
    """One sidecar string VALUE, masked; None passes through as None.

    The whole redaction surface of the sidecar, now that ``write_tool_meta``
    builds the file from a fixed schema instead of serializing a caller's dict:
    exactly three string VALUES can carry operator/LLM text, and this is applied
    to each of them by name.

    Raises whatever ``redact_known_secrets`` raises: the LIVE redactor
    deliberately propagates a provider failure rather than degrading to
    unmasked, and ``write_tool_meta`` turns that into its fail-closed refusal.
    """
    return None if value is None else redact_known_secrets(value)


def write_tool_meta(directory: Path, meta: dict[str, Any]) -> bool:
    """Write the sidecar from the KNOWN SCHEMA, redacting its text. Returns success.

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
    of this helper. Two independent reasons a secret must never reach this file:

    * the summary is LLM output about a package whose ``.env`` holds live
      values, and this file is served back to the UI -- the same class
      ``redact_known_secrets`` closes everywhere raw model/tool text enters
      persisted state;
    * a future revise (D40) copies the installed package into a staging build,
      and ``validate_package``'s embedded-secret gate scans EVERY file there. A
      sidecar carrying an unredacted value would therefore make every later
      revise of that tool fail validation -- bricking the feature for that
      package with a rejection naming a file the user never wrote.

    So a redaction failure (``known_secret_values`` raising -- the LIVE path
    deliberately propagates rather than degrading to unmasked, see
    ``redact_known_secrets``) writes NOTHING and returns False. It is applied to
    the three fields that can carry operator/LLM text and to nothing else,
    because nothing else CAN: ``status`` is one of two literals chosen above,
    ``llm_log_id`` is an int, and ``updated_at`` is a machine timestamp its
    caller just produced (no on-disk one ever round-trips -- both callers
    overwrite it).

    The serialized payload is bounded by ``_AI_META_MAX_BYTES``, the SAME cap
    ``read_tool_meta`` refuses past, so this can never produce a file that reads
    back as None. Every other failure (an unwritable path, a symlinked/FIFO
    sidecar refused by ``_write_regular_file``'s O_NOFOLLOW + O_NONBLOCK +
    S_ISREG gate) is False too, so callers get one "did-not-happen" answer and
    never an exception -- summary metadata is best-effort by design.

    CONSEQUENCE, stated so it is not rediscovered as a bug: extra keys a
    hand-edited sidecar carries are DROPPED by the next write. Round-tripping
    them was never a contract -- it was a side effect of serializing the caller's
    dict, and it is precisely what let a stray key/value carry unmasked text into
    the file. The five fields above are the sidecar.

    The ``is_dir`` precondition matters more than it looks: ``_write_regular_file``
    CREATES missing parents (the meta-tool contract needs that), so without it a
    summary landing just after a racing ``delete_tool`` would re-create the
    deleted package's directory holding nothing but a sidecar -- and the registry
    would then list that ghost as a broken package named after the tool the user
    just removed. It narrows, but cannot close, that window (the delete can still
    land between this check and the open); the remaining race is the same
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
    origin_raw = meta.get("origin")
    try:
        payload: dict[str, Any] = {
            "summary": redact_known_secrets(summary),
            "status": status,
            "updated_at": updated_at,
            "llm_log_id": log_id,
            "origin": (
                {
                    "openapi_url": _redacted(_meta_str(origin_raw.get("openapi_url"))),
                    "instructions": _redacted(_meta_str(origin_raw.get("instructions"))),
                }
                if isinstance(origin_raw, dict)
                else None
            ),
        }
    except Exception:
        return False
    # ensure_ascii=False keeps CJK readable in the file (and is what the byte cap
    # below is measured against). Every value is a str/int/None we just built, so
    # dumps cannot fail on an unserializable type -- but the cap is checked on the
    # ENCODED length, because that is the unit the writer's half of the
    # read/write symmetry is stated in (see _AI_META_MAX_BYTES).
    text = json.dumps(payload, ensure_ascii=False)
    if len(text.encode("utf-8")) > _AI_META_MAX_BYTES:
        return False
    return _write_regular_file(directory / _AI_META_FILENAME, text)


def summary_status(directory: Path) -> str | None:
    """The package's summary status (``"draft"``/``"final"``), or None.

    None covers every "no trustworthy status" case at once: no sidecar, an
    unreadable/corrupt one, or a ``status`` that is not one of the two known
    values (see ``_SUMMARY_STATUSES`` for why an unknown value must not be
    trusted rather than passed through). Called once per package by
    ``list_tools`` so the 工具 page can badge every row without an N+1 of
    per-tool summary requests.
    """
    meta = read_tool_meta(directory)
    if meta is None:
        return None
    status = meta.get("status")
    return status if isinstance(status, str) and status in _SUMMARY_STATUSES else None


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
    * ``"ok"`` -- the sidecar was rewritten with the new status and a fresh
      ``updated_at``.

    The emptiness gate applies ONLY when the target is ``"final"``. Setting
    ``"draft"`` stays unconditional on purpose: 解除定版 is the escape hatch out
    of a frozen state, and an escape hatch that can itself be refused is not one.

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
    """
    directory = _resolve_package_dir_no_alias(name)
    if directory is None:
        return "not_found"
    meta = read_tool_meta(directory)
    if meta is None:
        return "no_meta"
    if status == "final":
        summary = meta.get("summary")
        if not (isinstance(summary, str) and summary.strip()):
            return "no_meta"
    meta["status"] = status
    meta["updated_at"] = datetime.now(UTC).isoformat()
    return "ok" if write_tool_meta(directory, meta) else "not_found"


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
    ``tool.json``; a package with no (or a corrupt) sidecar reports None.
    """
    return [
        {
            "name": scan.name,
            "description": scan.description,
            "enabled": scan.enabled,
            "valid": scan.valid,
            "error": scan.error,
            "summary_status": summary_status(scan.directory),
        }
        for scan in _scan_all()
    ]


def _build_llm_tool(scan: _PackageScan) -> LlmTool:
    """Turn a valid ``_PackageScan`` into an executable ``LlmTool``.

    Callers pass only ``valid`` scans, so ``parameters`` / ``entry`` are present;
    the assertions document that precondition and keep the type checker happy
    without an ``Any`` escape hatch.
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
    return LlmTool(spec=spec, handler=_make_handler(scan.directory, scan.entry))


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
# own logger config uses). The set is computed at CALL time, never cached as a
# static value, because it genuinely CHANGES at runtime: an install writes a new
# tool ``.env``, and an in-flight install registers its form secret before that
# ``.env`` even exists.

# In-flight install secrets. A web-installer form secret (secret_value) is
# registered here for the DURATION of one install (tool_builder add/discards
# around run_install), so it is already redactable during the builder session --
# the window BEFORE promote writes it into the package ``.env``, after which the
# per-package ``.env`` scan below is what covers it. This registry lives HERE,
# not in tool_builder, on purpose: ``known_secret_values`` must read it, and
# tool_builder already imports us, so putting it in tool_builder would force a
# reverse import (a cycle) -- the SAME inversion llm_log applies to us. Its own
# lock guards it because ``known_secret_values`` can run on a threadpool recorder
# thread while an install mutates the set on the event loop.
_INFLIGHT_SECRETS: set[str] = set()
_INFLIGHT_LOCK = threading.Lock()

# Cache of one package's parsed ``.env`` VALUES, keyed by the ``.env`` path and
# tagged with the file's ``mtime_ns``, so ``known_secret_values`` re-parses a
# package only when its ``.env`` actually changed. This matters because the
# provider runs once PER STORED LOG BODY (every request message + every response
# of every attempt), and a busy installer session records the whole GROWING
# conversation on each of dozens of rounds -- without the cache, every one of
# those records would re-read every installed tool's ``.env`` from disk.
_ENV_VALUE_CACHE: dict[str, tuple[int, frozenset[str]]] = {}
_ENV_VALUE_CACHE_LOCK = threading.Lock()


def register_inflight_secret(value: str) -> None:
    """Mark ``value`` redactable for the current install (see ``_INFLIGHT_SECRETS``)."""
    if not value:
        return
    with _INFLIGHT_LOCK:
        _INFLIGHT_SECRETS.add(value)


def discard_inflight_secret(value: str) -> None:
    """Drop ``value`` from the in-flight set once its install ends (try/finally)."""
    with _INFLIGHT_LOCK:
        _INFLIGHT_SECRETS.discard(value)


def _cached_env_values(directory: Path) -> frozenset[str]:
    """The redactable VALUES of one package's ``.env``, cached per (path, mtime).

    Reuses the ONE bounded ``.env`` loader (``_load_tool_dotenv``) rather than
    hand-rolling a second parser, so the FIFO/symlink/oversize hardening and the
    drop-bare-keys behavior are inherited unchanged. Empty values are dropped so
    a ``KEY=`` line contributes nothing (``_redact`` also guards empties, but not
    seeding them keeps the set tidy).
    """
    env_file = directory / ".env"
    key = str(env_file)
    try:
        mtime = env_file.stat().st_mtime_ns
    except OSError:
        # No ``.env`` (the common case) or an unstattable path: nothing to
        # contribute. Forget any stale cache entry so a LATER-created ``.env`` is
        # picked up fresh next time.
        with _ENV_VALUE_CACHE_LOCK:
            _ENV_VALUE_CACHE.pop(key, None)
        return frozenset()
    with _ENV_VALUE_CACHE_LOCK:
        cached = _ENV_VALUE_CACHE.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    # Parse OUTSIDE the lock (it does file I/O); the (path, mtime) key makes a
    # concurrent double-parse harmless -- both produce the identical set.
    values = frozenset(value for value in _load_tool_dotenv(directory).values() if value)
    with _ENV_VALUE_CACHE_LOCK:
        _ENV_VALUE_CACHE[key] = (mtime, values)
    return values


def known_secret_values() -> frozenset[str]:
    """Every value llm_log should redact out of stored prompt/response bodies (D36).

    The union of three sources:

    * our own ``openai_api_key`` when set -- belt-and-braces: the secret-free
      invariant already keeps it out of records STRUCTURALLY (llm.py never reads
      it into a body), but redacting it too means even a tool that somehow echoed
      it back cannot surface it in the log;
    * every VALUE in every installed tool package's ``.env`` (a KB API key etc.),
      cached per (path, mtime) so a call per record stays cheap;
    * the in-flight install secrets registered above (the pre-promote window).

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


def _build_tool_env(directory: Path) -> dict[str, str]:
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
    """
    env = {name: os.environ[name] for name in _PASSTHROUGH_ENV if name in os.environ}
    if get_settings().tls_no_verify:
        env["TLS_NO_VERIFY"] = "1"
    env.update(_load_tool_dotenv(directory))
    return env


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


def _run_tool_subprocess(
    entry: list[str],
    directory: Path,
    env: dict[str, str],
    args_json: str,
    timeout: float,
    output_cap: int,
) -> str:
    """Run one tool entry to completion (or timeout) and return its result STRING.

    Blocking; the async handler runs it via ``run_in_threadpool``. Every outcome
    is an agent-visible string, never an exception:

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
    """
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


def _make_handler(directory: Path, entry: list[str]) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Build the async handler an ``LlmTool`` runs, closing over its package.

    Settings (timeout, output cap) and the child environment are read at CALL
    time, not build time, so a settings override or an edited ``.env`` is
    honored on the next invocation. The blocking subprocess is off-loaded to the
    threadpool (the house pattern), so a slow tool never sits on the event loop.
    The handler honors ``LlmTool``'s no-raise contract: ``_run_tool_subprocess``
    turns every failure into a string, and the llm loop's own ``except`` is the
    final backstop.
    """

    async def _handler(arguments: dict[str, Any]) -> str:
        settings = get_settings()
        env = _build_tool_env(directory)
        args_json = json.dumps(arguments, ensure_ascii=False)
        return await run_in_threadpool(
            _run_tool_subprocess,
            list(entry),
            directory,
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
    the name is unsafe, the package is absent, or the removal fails.
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
    try:
        shutil.rmtree(directory)
    except OSError:
        return False
    return True
