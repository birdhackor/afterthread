"""Unfinished revisions (drafts): what a revise round produced, kept with the tool.

A revise round that ends without publishing -- the builder's own ``ready=false``,
a timeout, an upstream error, a refused rename -- keeps its work in the package
itself, under ``<tools_dir>/<name>/.afterthread.meta/draft/``:

* ``files-<hex>/``: a copy of the round's workspace, taken after its commands
  are confirmed stopped and the builder's ``.env`` and root ``.afterthread.meta/``
  are stripped. A copy, never a move or a hard link, so a process still writing
  into the workspace writes into the staging directory;
* ``manifest.json``: how the last rounds ended, the model's notes, and which
  paths were not kept and why.

A round about to publish saves one too, so a failed publish cannot take the only
copy of the work with it; a successful publish removes it. One draft per tool:
while ``draft/`` exists in any state, a new revise is refused (``has_draft``).

The draft lives at the package layer, so it goes wherever the package goes: a
deleted tool takes its draft along, a same-name reinstall starts without one,
and ``.staging`` keeps its meaning of one session's scratch space. Nothing here
writes into ``.staging`` or removes anything from it; a workspace there is only
read.

What holds, and why:

* LOCKS. Every write a round makes into its package's draft holds the shared
  tools lock (``tools.acquire_shared_tools_lock``) and, inside it, checks that
  ``<tools_dir>/<name>`` is still the directory the round started with, by its
  ``(st_dev, st_ino)``: ``delete_tool`` needs the lock exclusively, so it answers
  409 instead of racing the write, and a write never lands in another package.
  A discard holds the same lock and works on the package as it finds it.
  The draft directory is made with a non-recursive ``os.mkdir`` under the
  existing metadata directory, so a deleted package is never recreated. Each
  lock is taken and released inside one blocking call; nothing that runs under it
  takes the exclusive lock, and the sweep, which runs under the exclusive lock,
  takes no lock at all. Those two rules keep this from deadlocking on itself: a
  second open of the lock file in the same process conflicts with the first
  exactly as another process would.
* WHOSE DRAFT. Every read and write of ``draft/`` reaches it through
  ``_own_meta``: the package directory and its ``.afterthread.meta``, each a
  real directory, neither a link. Through a link, ``draft/`` would be another
  package's, so a package whose directory or metadata directory is a link has
  no draft of its own: none is listed, none gates a revise, none is discarded.
* TEXT. Every free-text string written into the manifest is masked against the
  secrets known at that moment, then cut to its cap, then made encodable
  (``tools._utf8_safe``): a file name read from disk can carry lone surrogates,
  which the UTF-8 encode of the whole document would refuse. Identifiers,
  timestamps and vocabulary values are backend-made and fixed in shape, so they
  are checked against that shape instead of masked; a mask could only corrupt
  them. The keys are this module's literals. Reading makes every string
  encodable again, since a hand edit bypasses the writer, and what goes to the
  API is masked once more: a secret registered after the write must not come
  back unmasked.
* SECRETS. The workspace is scanned with the full embedded-secret scanner before
  anything is copied. A path whose name holds a known secret is not kept; one
  whose content or link target does keeps the version the round started from, if
  that version is clean -- a link, if it also passes LINKS on the copy -- and is
  not kept at all otherwise. A scan that cannot finish keeps no files. Content is
  never rewritten with redaction markers, which could break the program.
* LINKS. A link is kept only when its target, resolved as a string from the
  link's own directory, stays inside the tree, and no ``..`` in it follows a
  component that is itself a link: that is the one case where the string and the
  operating system resolve differently. The rule is applied twice: on the
  workspace, to plan the copy, and on the copy once every link is in it, before
  it is made durable, to every link of the copy. The copy differs from the
  workspace wherever an earlier version was put back, and the rule holds only on
  the tree it was applied to. Every link is judged before any is removed; what
  fails is removed and recorded as not kept. An absolute target inside the
  workspace is stored relative.
* DURABILITY. A new copy is written under a temporary name, made durable and
  renamed into place before the manifest names it, and the manifest is replaced
  atomically. A previous copy is removed only once the new manifest is confirmed
  on disk and only if nobody changed it during the round; one that changed stays,
  listed in ``kept``.
"""

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
from typing import Any, Literal, get_args

from afterthread.services import llm, tools
from afterthread.services.tool_runs import AttachedRun

DRAFT_DIRNAME = "draft"
MANIFEST_FILENAME = "manifest.json"

# Every name the backend makes in and beside ``draft/`` carries 32 random hex
# digits, and these patterns are the whole of what the sweep and the readers
# accept: an anchored ``\Z``, because the patterns gate an ``rmtree`` and ``$``
# also matches before a trailing newline.
_HEX = "[0-9a-f]{32}"
_FILES_RE = re.compile(rf"^files-{_HEX}\Z")
_INCOMING_RE = re.compile(rf"^\.incoming-{_HEX}\Z")
_DISCARDED_RE = re.compile(rf"^{DRAFT_DIRNAME}\.discarded-{_HEX}\Z")
# A draft id is minted here; a job id is ``uuid4().hex``. Both are 32 hex digits.
_ID_RE = re.compile(rf"^{_HEX}\Z")
# The same shape, for the route that takes a draft id in its query.
DRAFT_ID_PATTERN = rf"^{_HEX}$"
# The only timestamp this module writes: ``datetime.now(UTC).isoformat()``.
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00\Z")
# ``discard_token``'s shape: a sha256 hex digest.
DISCARD_TOKEN_PATTERN = r"^[0-9a-f]{64}$"

# The manifest's caps. Text is cut in code points after its mask, so the bytes
# are bounded by six per code point (a control character escaped as \uXXXX):
# under a megabyte for every field at its cap, which this leaves room for.
_MANIFEST_MAX_BYTES = 2 * 1024 * 1024
ATTEMPTS_KEPT = 5
_KEPT_MAX = 100
FEEDBACK_CAP = 20_000
# The model's three notes keep the caps ``tool_builder`` gives them, so a stored
# note is cut only when a mask made it longer (tests pin the two sets equal).
SUMMARY_CAP = 2000
HANDOFF_CAP = 4000
OPERATOR_ACTION_CAP = 1000
ERROR_CAP = 2000
NOTE_CAP = 2000
OMITTED_KEPT = 50
_PATH_CAP = 300
_RUNS_KEPT = 3
_RUN_TEXT_CAP = 200
# The list shows the last summary as an excerpt.
_LAST_SUMMARY_CAP = 300
_READ_CHUNK = 1024 * 1024

DraftState = Literal["running", "unusable", "publishing", "unknown", "published", "stale", "open"]
RoundOutcome = Literal["published", "not_published", "failed"]
# Why a path of the workspace is not in the draft.
OmitReason = Literal[
    "secret_name",
    "secret_content",
    "secret_link_target",
    "link_outside",
    "link_ambiguous",
    "unreadable",
    "not_regular",
]
_OMIT_REASONS = frozenset(get_args(OmitReason))
_OUTCOMES = frozenset(get_args(RoundOutcome))
_STOPPED_BY = frozenset(get_args(llm.FinalizeTrigger))
_FAILURES = frozenset(get_args(llm.RunFailure))

# Why a round's files were not saved: user-facing (zh-TW, rendered by the 工具
# page) and category-only, never a path or a value.
REASON_STRIP_ENV = "無法清除建置器寫入的 .env"
REASON_STRIP_SIDECARS = "無法清除工作區裡的後端中繼資料"
REASON_WORKSPACE = "暫存工作區已被移動或替換"
REASON_TOO_MANY_FILES = "檔案數超過上限（TOOL_DRAFT_MAX_FILES）"  # noqa: RUF001
REASON_TOO_LARGE = "總大小超過上限（TOOL_DRAFT_MAX_BYTES）"  # noqa: RUF001
REASON_UNREADABLE = "無法讀取暫存工作區"
REASON_SCAN_FAILED = "秘密掃描失敗（{reason}），無法確認要保存的檔案裡沒有已知的秘密值"  # noqa: RUF001
REASON_LOCK = "無法取得工具目錄的鎖"
REASON_PACKAGE_GONE = "工具已被刪除或替換"
REASON_DRAFT_EXISTS = "這個工具已經有草稿"
REASON_DRAFT_CHANGED = "草稿在這一輪進行中被改動過"
REASON_COPY = "無法複製檔案到草稿（{kind}）"  # noqa: RUF001
REASON_DURABILITY = "無法確認草稿已安全寫入磁碟"
REASON_MANIFEST = "無法寫入草稿的說明檔"
REASON_UNEXPECTED = "保存時發生未預期錯誤（{kind}）"  # noqa: RUF001

# A relative path -> the fingerprint ``tree_digest`` takes of that entry.
TreeDigest = dict[str, tuple[Any, ...]]


@dataclass(frozen=True, slots=True)
class Omitted:
    """One path of the workspace the draft does not keep, and why."""

    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class DraftStart:
    """What only the round that creates a draft records: where it started."""

    base_vid: str
    feedback: str | None
    runs: tuple[AttachedRun, ...] = ()


@dataclass(frozen=True, slots=True)
class DraftRound:
    """How one round ended, as the manifest records it.

    ``outcome`` is None while it is not known yet: a round saved just before it
    publishes. The notes are the model's own words, kept for display and for
    whoever continues; nothing here reads them to decide anything.
    """

    finished_at: str
    job_id: str | None = None
    started_at: str | None = None
    outcome: RoundOutcome | None = None
    stopped_by: str | None = None
    failure: str | None = None
    rounds_used: int | None = None
    max_rounds: int | None = None
    error: str | None = None
    summary: str | None = None
    handoff: str | None = None
    operator_action: str | None = None
    note: str | None = None

    def has_notes(self) -> bool:
        """Whether the model handed back any of its three notes."""
        return bool(self.summary or self.handoff or self.operator_action)


@dataclass(frozen=True, slots=True)
class PreviousDraft:
    """The draft a continuing round copied its workspace from.

    ``manifest`` is the manifest as read when the round started; ``copied_out``
    is ``tree_digest`` of its files directory at that moment, what the save
    compares before removing that directory.
    """

    manifest: dict[str, Any]
    copied_out: TreeDigest | None


@dataclass(frozen=True, slots=True)
class Durability:
    """The fsync helpers a save uses, handed in by its caller (``tool_builder``)."""

    tree: Callable[[Path], bool]
    directory: Callable[[Path], bool]


@dataclass(frozen=True, slots=True)
class SaveResult:
    """What one save did.

    ``worth_saving`` False: nothing to keep (no change, no notes), which is not a
    failure. Otherwise ``saved`` says whether the draft now holds this round's
    files, and ``reason`` why not.
    """

    worth_saving: bool
    saved: bool = False
    draft_id: str | None = None
    generation: int | None = None
    attempts: int | None = None
    reason: str | None = None

    def report(
        self,
        *,
        saved: bool | None = None,
        reason: str | None = None,
        retained_path: str | None = None,
    ) -> dict[str, Any]:
        """The job's ``draft`` field for this save, with what the caller learned since."""
        return {
            "saved": self.saved if saved is None else saved,
            "draft_id": self.draft_id,
            "generation": self.generation,
            "attempts": self.attempts,
            "reason": self.reason if reason is None else reason,
            "retained_path": retained_path,
        }


def _not_saved(reason: str, *, draft: dict[str, Any] | None = None) -> SaveResult:
    if draft is None:
        return SaveResult(worth_saving=True, reason=reason)
    return SaveResult(
        worth_saving=True,
        draft_id=draft["draft_id"],
        generation=draft["generation"],
        attempts=draft["attempt_count"],
        reason=reason,
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def draft_path(package_root: tools.PackageRoot) -> Path:
    """Where a package keeps its draft.

    The path only: every read and write of it in this module goes through
    ``_own_meta``, which refuses to reach it through a link.
    """
    return package_root.path / tools._META_DIRNAME / DRAFT_DIRNAME


# What ``_own_meta`` answers for a directory it cannot look at.
_UNSEEN: Literal["unseen"] = "unseen"


def _own_meta(package_root: tools.PackageRoot) -> Path | Literal["unseen"] | None:
    """The package's own metadata directory: the one way this module reaches ``draft/``.

    The package directory and its ``.afterthread.meta`` are each looked at without
    following a link, and both must be real directories: through a link,
    ``draft/`` would be another package's. None when either is missing, a link or
    not a directory -- the package then has no draft of its own; ``_UNSEEN`` when
    either cannot be looked at for another reason. ``has_draft``, ``draft_view``
    and ``discard`` take that as a ``draft/`` they cannot look at (``_own_draft``),
    as they take one whose own ``lstat`` fails; a save, the marks around a
    publish and the sweep write nothing.

    What it does not cover: either directory replaced by a link between this look
    and the caller's use of the path, which only a hand edit does.
    """
    meta = package_root.path / tools._META_DIRNAME
    for directory in (package_root.path, meta):
        try:
            info = os.lstat(directory)
        except FileNotFoundError, NotADirectoryError:
            return None
        except OSError:
            return _UNSEEN
        if not stat.S_ISDIR(info.st_mode):
            return None
    return meta


def _own_draft(package_root: tools.PackageRoot) -> tuple[Path, os.stat_result | None] | None:
    """The package's own ``draft/`` and its ``lstat``, or None when it has none.

    Reached through ``_own_meta``. The ``lstat`` is None when ``draft/``, or a
    directory above it, cannot be looked at: such a draft is there as far as
    anyone can tell (``has_draft``).
    """
    meta = _own_meta(package_root)
    if meta is None:
        return None
    draft_dir = draft_path(package_root)
    if not isinstance(meta, Path):
        return draft_dir, None
    try:
        return draft_dir, os.lstat(draft_dir)
    except FileNotFoundError, NotADirectoryError:
        return None
    except OSError:
        return draft_dir, None


def has_draft(package_root: tools.PackageRoot) -> bool:
    """Whether the package has a draft: ``draft/`` exists, whatever its state.

    Its own draft only (``_own_draft``): a package whose directory or metadata
    directory is a link has none. A ``draft/`` without a readable manifest still
    counts. Treated as no draft, the next save would write a manifest beside it
    and the sweep would then take the complete ``files-*`` a crash left there for
    an orphan. An entry that cannot be looked at counts as present too: the
    answer gates a new revise, and an operator who sees a draft can discard it.
    """
    return _own_draft(package_root) is not None


def directory_identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of the real directory at ``path``, or None."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    return (info.st_dev, info.st_ino)


def _is_real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


# --- the content digest -------------------------------------------------------


def _file_digest(path: str, budget: int | None) -> tuple[str, int] | None:
    """The sha256 and length of one regular file, read without following it.

    None when it cannot be read whole, is not a regular file once open, or holds
    more than ``budget`` bytes.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        digest = hashlib.sha256()
        length = 0
        while chunk := os.read(fd, _READ_CHUNK):
            length += len(chunk)
            if budget is not None and length > budget:
                return None
            digest.update(chunk)
        return digest.hexdigest(), length
    except OSError:
        return None
    finally:
        os.close(fd)


def tree_digest(
    root: Path, *, max_entries: int | None = None, max_bytes: int | None = None
) -> TreeDigest | None:
    """A fingerprint of every entry under ``root``, by relative path.

    Directories give ``("d", mode)``, regular files ``("f", mode, length,
    sha256)``, links ``("l", target)``, anything else ``("o", file type)``.
    Timestamps are left out (a copy keeps them, a ``touch`` changes nothing that
    matters); permission bits are in (``chmod +x`` does). Nothing is followed.
    The relative paths are spelled like the embedded-secret scanner spells them.

    None when the tree cannot be read whole -- ``root`` is not a real directory,
    a directory cannot be listed, a file cannot be read -- or holds more than
    ``max_entries`` entries or ``max_bytes`` bytes of file content: then nothing
    is known about it, and ``same_tree`` says so.

    It walks by path, so it describes a tree that holds still while it runs.
    """
    if not _is_real_dir(root):
        return None
    entries: TreeDigest = {}
    total = 0
    pending: list[tuple[str, str]] = [(os.fspath(root), "")]
    try:
        while pending:
            directory, prefix = pending.pop()
            with os.scandir(directory) as listing:
                children = sorted(listing, key=lambda entry: entry.name)
            for entry in children:
                if max_entries is not None and len(entries) >= max_entries:
                    return None
                relative = prefix + entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    entries[relative] = ("l", os.readlink(entry.path))
                elif stat.S_ISDIR(info.st_mode):
                    entries[relative] = ("d", stat.S_IMODE(info.st_mode))
                    pending.append((entry.path, relative + "/"))
                elif stat.S_ISREG(info.st_mode):
                    budget = None if max_bytes is None else max_bytes - total
                    found = _file_digest(entry.path, budget)
                    if found is None:
                        return None
                    sha, length = found
                    total += length
                    entries[relative] = ("f", stat.S_IMODE(info.st_mode), length, sha)
                else:
                    entries[relative] = ("o", stat.S_IFMT(info.st_mode))
    except OSError:
        return None
    return entries


def same_tree(first: TreeDigest | None, second: TreeDigest | None) -> bool:
    """Whether two digests describe the same tree. Unknown is never the same."""
    return first is not None and second is not None and first == second


def changed_paths(first: TreeDigest, second: TreeDigest) -> tuple[str, ...]:
    """The relative paths whose entry differs, appeared or went away, sorted."""
    return tuple(
        sorted(path for path in first.keys() | second.keys() if first.get(path) != second.get(path))
    )


# --- the manifest -------------------------------------------------------------


class _BadShape(Exception):
    """Raised inside the manifest checks; never leaves this module."""


def _text(value: Any, cap: int, known: list[str]) -> str | None:
    """A free-text value for the manifest: masked, then cut, then made encodable."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _BadShape
    return tools._utf8_safe(tools._mask_known_secrets(value, known)[:cap])


def _matching(value: Any, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _BadShape
    return value


def _optional_matching(value: Any, pattern: re.Pattern[str]) -> str | None:
    return None if value is None else _matching(value, pattern)


def _count(value: Any, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _BadShape
    return value


def _optional_count(value: Any) -> int | None:
    return None if value is None else _count(value)


def _vocabulary(value: Any, allowed: frozenset[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise _BadShape
    return value


def _flag(value: Any) -> bool:
    if not isinstance(value, bool):
        raise _BadShape
    return value


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _BadShape
    return value


def _listing(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise _BadShape
    return value


def _run_entry(raw: Any, known: list[str]) -> dict[str, Any]:
    run = _mapping(raw)
    return {
        "id": _count(run.get("id"), minimum=1),
        "started_at": _text(run.get("started_at"), _RUN_TEXT_CAP, known),
        "workflow": _text(run.get("workflow"), _RUN_TEXT_CAP, known),
        "vid": _text(run.get("vid"), _RUN_TEXT_CAP, known),
        "outcome": _text(run.get("outcome"), _RUN_TEXT_CAP, known),
        "exit_code": None if run.get("exit_code") is None else _signed(run.get("exit_code")),
    }


def _signed(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _BadShape
    return value


def _omitted_entry(raw: Any, known: list[str]) -> dict[str, Any]:
    entry = _mapping(raw)
    path = _text(entry.get("path"), _PATH_CAP, known)
    if path is None:
        raise _BadShape
    return {"path": path, "reason": _vocabulary(entry.get("reason"), _OMIT_REASONS)}


def _attempt_entry(raw: Any, known: list[str]) -> dict[str, Any]:
    attempt = _mapping(raw)
    omitted = [
        _omitted_entry(item, known) for item in _listing(attempt.get("omitted"), OMITTED_KEPT)
    ]
    omitted_count = _count(attempt.get("omitted_count"))
    if omitted_count < len(omitted):
        raise _BadShape
    finished_at = _optional_matching(attempt.get("finished_at"), _TIMESTAMP_RE)
    if finished_at is None:
        raise _BadShape
    return {
        "number": _count(attempt.get("number"), minimum=1),
        "job_id": _optional_matching(attempt.get("job_id"), _ID_RE),
        "started_at": _optional_matching(attempt.get("started_at"), _TIMESTAMP_RE),
        "finished_at": finished_at,
        "outcome": _vocabulary(attempt.get("outcome"), _OUTCOMES),
        "stopped_by": _vocabulary(attempt.get("stopped_by"), _STOPPED_BY),
        "failure": _vocabulary(attempt.get("failure"), _FAILURES),
        "rounds_used": _optional_count(attempt.get("rounds_used")),
        "max_rounds": _optional_count(attempt.get("max_rounds")),
        "error": _text(attempt.get("error"), ERROR_CAP, known),
        "summary": _text(attempt.get("summary"), SUMMARY_CAP, known),
        "handoff": _text(attempt.get("handoff"), HANDOFF_CAP, known),
        "operator_action": _text(attempt.get("operator_action"), OPERATOR_ACTION_CAP, known),
        "note": _text(attempt.get("note"), NOTE_CAP, known),
        "saved": _flag(attempt.get("saved")),
        "reason": _text(attempt.get("reason"), ERROR_CAP, known),
        "omitted": omitted,
        "omitted_count": omitted_count,
    }


def _marker(raw: Any, time_key: str) -> dict[str, Any] | None:
    """``running`` or ``publishing``: which job, since when."""
    if raw is None:
        return None
    marker = _mapping(raw)
    stamp = _optional_matching(marker.get(time_key), _TIMESTAMP_RE)
    if stamp is None:
        raise _BadShape
    return {"job_id": _optional_matching(marker.get("job_id"), _ID_RE), time_key: stamp}


def _document(raw: Any, known: list[str]) -> dict[str, Any]:
    """The manifest built from ``raw`` field by field, every value checked.

    The ONE description of the manifest's shape: the writer runs it over what it
    is about to write (with the secrets known now, so every text is masked and
    cut on the way in) and the reader over what it read back (with no secrets,
    which still makes every text encodable and cut). Keys are this function's
    literals, never the input's. Raises ``_BadShape`` on anything out of shape.
    """
    doc = _mapping(raw)
    files_dir = _matching(doc.get("files_dir"), _FILES_RE)
    kept = [_matching(name, _FILES_RE) for name in _listing(doc.get("kept"), _KEPT_MAX)]
    if files_dir in kept or len(set(kept)) != len(kept):
        raise _BadShape
    attempts = [
        _attempt_entry(item, known) for item in _listing(doc.get("attempts"), ATTEMPTS_KEPT)
    ]
    attempt_count = _count(doc.get("attempt_count"), minimum=1)
    numbers = [attempt["number"] for attempt in attempts]
    if (
        not attempts
        or numbers != sorted(set(numbers))
        or numbers[-1] != attempt_count
        or attempt_count < len(attempts)
    ):
        raise _BadShape
    return {
        "draft_id": _matching(doc.get("draft_id"), _ID_RE),
        "generation": _count(doc.get("generation"), minimum=1),
        "files_dir": files_dir,
        "kept": kept,
        "base_vid": _matching(doc.get("base_vid"), tools._VID_RE),
        "feedback": _text(doc.get("feedback"), FEEDBACK_CAP, known),
        "runs": [_run_entry(item, known) for item in _listing(doc.get("runs"), _RUNS_KEPT)],
        "attempt_count": attempt_count,
        "attempts": attempts,
        "running": _marker(doc.get("running"), "started_at"),
        "publishing": _marker(doc.get("publishing"), "at"),
    }


def manifest_bytes(doc: dict[str, Any], known: list[str]) -> bytes | None:
    """The manifest file for ``doc``, or None when it is out of shape or too large."""
    try:
        payload = _document(doc, known)
        data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    except _BadShape, TypeError, ValueError:
        return None
    if len(data) > _MANIFEST_MAX_BYTES:
        return None
    return data


def read_manifest(draft_dir: Path) -> dict[str, Any] | None:
    """The manifest of the draft at ``draft_dir``, checked, or None when unusable.

    Read like every other backend file of a package (``_read_regular_file_capped``:
    regular files only, never through a link, never past the cap). Every string
    is made encodable; the text is not masked here -- a caller that shows it masks
    it with the secrets it holds.
    """
    text = tools._read_regular_file_capped(draft_dir / MANIFEST_FILENAME, _MANIFEST_MAX_BYTES)
    if text is None or len(text) > _MANIFEST_MAX_BYTES:
        return None
    try:
        raw = json.loads(text)
    except ValueError, RecursionError:
        return None
    try:
        return _document(raw, [])
    except _BadShape:
        return None


def _write_manifest(draft_dir: Path, doc: dict[str, Any], known: list[str]) -> tuple[bool, bool]:
    """Replace the manifest atomically: (replaced, confirmed durable)."""
    data = manifest_bytes(doc, known)
    if data is None:
        return False, False
    durable: list[bool] = []
    replaced = tools._write_package_file_atomic(
        draft_dir, MANIFEST_FILENAME, data, None, durability_out=durable
    )
    return replaced, replaced and bool(durable) and durable[0]


def _round_entry(
    draft_round: DraftRound,
    number: int,
    *,
    saved: bool,
    reason: str | None,
    omitted: list[Omitted],
) -> dict[str, Any]:
    return {
        "number": number,
        "job_id": draft_round.job_id,
        "started_at": draft_round.started_at,
        "finished_at": draft_round.finished_at,
        "outcome": draft_round.outcome,
        "stopped_by": draft_round.stopped_by,
        "failure": draft_round.failure,
        "rounds_used": draft_round.rounds_used,
        "max_rounds": draft_round.max_rounds,
        "error": draft_round.error,
        "summary": draft_round.summary,
        "handoff": draft_round.handoff,
        "operator_action": draft_round.operator_action,
        "note": draft_round.note,
        "saved": saved,
        "reason": reason,
        "omitted": [{"path": item.path, "reason": item.reason} for item in omitted[:OMITTED_KEPT]],
        "omitted_count": len(omitted),
    }


def _with_round(doc: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """``doc`` one generation on, with ``entry`` as its latest round and no ``running``."""
    return {
        **doc,
        "generation": doc["generation"] + 1,
        "attempt_count": entry["number"],
        "attempts": [*doc["attempts"], entry][-ATTEMPTS_KEPT:],
        "running": None,
    }


def provenance_line(draft_id: str, attempts: int) -> str:
    """The line ``origin.feedback`` carries for a version published from a draft.

    The reconciliation in ``draft_view`` looks for the draft id in it.
    """
    return f"草稿 {draft_id}，經過 {attempts} 輪"  # noqa: RUF001


# --- discard token ------------------------------------------------------------


def _entry_identity(path: Path, *, with_change: bool) -> list[Any]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return ["absent"]
    except OSError as exc:
        return ["error", exc.errno]
    identity: list[Any] = [stat.S_IFMT(info.st_mode), info.st_dev, info.st_ino]
    if with_change:
        identity += [info.st_ctime_ns, info.st_size]
    return identity


def discard_token(draft_dir: Path) -> str:
    """The opaque value that stands for this draft as it is now, for a discard.

    Taken from what ``lstat`` says of ``draft/`` and of its manifest, so it exists
    whatever the manifest holds -- unreadable, out of shape, missing, not a file.
    Every rewrite of the manifest replaces it with a new file (a new inode and
    ctime), and a draft made again after a discard is a new directory, so a token
    taken earlier no longer matches either. The manifest's ctime is what carries
    that: a filesystem may hand a freed inode number to the next file. The
    directory's own times are left out, because a sweep or a save in progress
    changes them without changing the draft.
    """
    identity = [
        _entry_identity(draft_dir, with_change=False),
        _entry_identity(draft_dir / MANIFEST_FILENAME, with_change=True),
    ]
    return hashlib.sha256(json.dumps(identity).encode("ascii")).hexdigest()


# --- what the list shows --------------------------------------------------------


def _shown(text: str | None, known: list[str], cap: int | None = None) -> str | None:
    """Read text as the API serves it: masked with the secrets known now, then cut."""
    if text is None:
        return None
    masked = tools._mask_known_secrets(text, known)
    return tools._utf8_safe(masked if cap is None else masked[:cap])


def _published_from(manifest: dict[str, Any], resolution: tools.Resolution) -> bool:
    """Whether ``current`` is the version this draft's publish put live."""
    if not isinstance(resolution, tools.Resolved):
        return False
    origin = tools.read_origin_meta(resolution.version_root)
    if origin is None:
        return False
    feedback = origin.get("feedback")
    return (
        origin.get("previous") == manifest["base_vid"]
        and isinstance(feedback, str)
        and manifest["draft_id"] in feedback
    )


def draft_view(
    package_root: tools.PackageRoot,
    resolution: tools.Resolution,
    *,
    job_active: Callable[[str], bool],
    known: Callable[[], list[str]],
) -> dict[str, Any] | None:
    """The package's draft as the tools list shows it, or None when it has none.

    The state, in this order: ``running`` (a continuation of this process is on
    it), ``unusable`` (the manifest cannot be used, or its files directory is not
    a directory), ``publishing``/``published``/``unknown`` (a publish from it was
    started: still going in this process, put live -- ``current`` names it as its
    origin -- or neither known), ``stale`` (``current`` moved off its base),
    ``open``. ``interrupted``: a round marked as running is no longer running.

    ``job_active`` answers for a job id of this process; ``known`` gives the
    secrets the text is masked with, and lets its failure through: the list fails
    rather than serve text it could not mask. Only the package's own draft is
    shown (``_own_draft``).
    """
    found = _own_draft(package_root)
    if found is None:
        return None
    draft_dir, info = found
    token = discard_token(draft_dir)
    manifest = read_manifest(draft_dir) if info is not None and stat.S_ISDIR(info.st_mode) else None
    if manifest is None:
        return {
            "draft_id": None,
            "generation": None,
            "attempts": None,
            "state": "unusable",
            "interrupted": False,
            "base_vid": None,
            "path": str(draft_dir),
            "discard_token": token,
            "last": None,
        }
    files = draft_dir / manifest["files_dir"]
    running = manifest["running"]
    running_now = (
        running is not None and running["job_id"] is not None and job_active(running["job_id"])
    )
    state: DraftState
    if running_now:
        state = "running"
    elif not _is_real_dir(files):
        state = "unusable"
    elif manifest["publishing"] is not None:
        publishing_job = manifest["publishing"]["job_id"]
        if publishing_job is not None and job_active(publishing_job):
            state = "publishing"
        elif _published_from(manifest, resolution):
            state = "published"
        else:
            state = "unknown"
    elif not isinstance(resolution, tools.Resolved) or resolution.vid != manifest["base_vid"]:
        state = "stale"
    else:
        state = "open"
    secrets = known()
    last = manifest["attempts"][-1]
    return {
        "draft_id": manifest["draft_id"],
        "generation": manifest["generation"],
        "attempts": manifest["attempt_count"],
        "state": state,
        "interrupted": running is not None and not running_now,
        "base_vid": manifest["base_vid"],
        "path": str(draft_dir if state == "unusable" else files),
        "discard_token": token,
        "last": {
            "stopped_by": last["stopped_by"],
            "failure": last["failure"],
            "outcome": last["outcome"],
            "finished_at": last["finished_at"],
            "operator_action": _shown(last["operator_action"], secrets),
            "summary": _shown(last["summary"], secrets, _LAST_SUMMARY_CAP),
            "omitted": last["omitted_count"],
        },
    }


# --- saving a round -------------------------------------------------------------


def _over_limits(root: Path, max_entries: int, max_bytes: int) -> str | None:
    """Why the tree is too big to keep, from its metadata alone; None when it fits."""
    count = 0
    total = 0
    pending = [os.fspath(root)]
    try:
        while pending:
            with os.scandir(pending.pop()) as listing:
                for entry in listing:
                    count += 1
                    if count > max_entries:
                        return REASON_TOO_MANY_FILES
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(entry.path)
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
                        if total > max_bytes:
                            return REASON_TOO_LARGE
    except OSError:
        return REASON_UNREADABLE
    return None


def stored_link_target(
    root: Path, parts: tuple[str, ...], target: str
) -> tuple[str | None, str | None]:
    """The target to store for the link at ``parts`` under ``root``, or why it is not kept.

    ``root`` is the tree the link is judged in -- the workspace while the copy is
    planned, the copy itself once it is made -- and ``parts`` the link's path in
    it, its own name last. The target is resolved as a string, from the link's
    directory: it must stay inside ``root``, and no ``..`` may come right after a
    component that is itself a link in ``root``, because there the operating
    system resolves the link first and goes up from where it points, which the
    string cannot see. A tree whose every link is kept by this rule, judged on
    that tree, has every link resolve inside it or nowhere: each link the
    resolution passes through is held to the same rule. It holds only for the
    tree the rule was applied to, which is why the copy is judged again
    (``_links_failing_on``).

    An absolute target inside ``root`` comes back relative to the link's
    directory; a relative one comes back as it was.
    """
    link_dir = list(parts[:-1])
    relative = target
    if os.path.isabs(target):
        normalized = os.path.normpath(target)
        inside: list[str] | None = None
        for spelling in dict.fromkeys((os.fspath(root), os.path.realpath(root))):
            if normalized == spelling:
                inside = []
                break
            if normalized.startswith(spelling.rstrip(os.sep) + os.sep):
                inside = normalized[len(spelling.rstrip(os.sep)) + 1 :].split(os.sep)
                break
        if inside is None:
            return None, "link_outside"
        relative = "/".join([".."] * len(link_dir) + inside) or "."
    stack = list(link_dir)
    for component in relative.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            if not stack:
                return None, "link_outside"
            if os.path.islink(root.joinpath(*stack)):
                return None, "link_ambiguous"
            stack.pop()
        else:
            stack.append(component)
    return relative, None


@dataclass(slots=True)
class _CopyPlan:
    """What a save copies, decided before the lock is taken, and what the copy may hold."""

    # The limits the copy is counted against as it is made (``_Budget``).
    max_entries: int
    max_bytes: int
    # (relative path, mode), each parent before its children.
    directories: list[tuple[str, int]] = field(default_factory=list)
    # (relative path, where its bytes come from).
    files: list[tuple[str, Path]] = field(default_factory=list)
    # (relative path, target to store).
    links: list[tuple[str, str]] = field(default_factory=list)
    # Relative paths whose bytes come from the earlier version: checked again once copied.
    earlier: list[str] = field(default_factory=list)
    omitted: list[Omitted] = field(default_factory=list)


def _earlier_entry(base_root: Path | None, parts: tuple[str, ...]) -> Path | None:
    """The same path in the earlier version, reached through real directories only."""
    if base_root is None or not _is_real_dir(base_root):
        return None
    here = base_root
    for name in parts[:-1]:
        here = here / name
        if not _is_real_dir(here):
            return None
    return here / parts[-1]


def _keep_earlier(
    plan: _CopyPlan,
    workspace: Path,
    base_root: Path | None,
    relative: str,
    parts: tuple[str, ...],
    reason: str,
    known: list[str],
) -> None:
    """Plan the earlier version of a path whose own content or target holds a secret.

    The path is recorded as not kept either way: this round's version of it is
    not in the draft, whether the earlier one takes its place or nothing does. An
    earlier link is judged here on the workspace and, like every link, again on
    the copy (``_carry_out``), where what it now stands beside can differ.
    """
    plan.omitted.append(Omitted(relative, reason))
    source = _earlier_entry(base_root, parts)
    try:
        info = os.lstat(source) if source is not None else None
    except OSError:
        info = None
    if source is not None and info is not None:
        if stat.S_ISREG(info.st_mode):
            plan.files.append((relative, source))
            plan.earlier.append(relative)
            return
        if stat.S_ISLNK(info.st_mode):
            try:
                target = os.readlink(source)
            except OSError:
                target = None
            if target is not None and not any(secret in target for secret in known):
                stored, _why = stored_link_target(workspace, parts, target)
                if stored is not None:
                    plan.links.append((relative, stored))


def _plan_copy(
    workspace: Path,
    scan: tools.SecretScanDone,
    base_root: Path | None,
    known: list[str],
    max_entries: int,
    max_bytes: int,
) -> _CopyPlan:
    """Decide, entry by entry, what the draft keeps of the workspace.

    Raises ``OSError`` when the workspace cannot be walked.
    """
    named = {hit.path for hit in scan.hits if hit.kind == "name"}
    in_content = {hit.path for hit in scan.hits if hit.kind == "content"}
    in_target = {hit.path for hit in scan.hits if hit.kind == "link_target"}
    skipped = {skip.path: skip.reason for skip in scan.skipped}
    plan = _CopyPlan(max_entries=max_entries, max_bytes=max_bytes)
    pending: list[tuple[str, str, tuple[str, ...]]] = [(os.fspath(workspace), "", ())]
    while pending:
        directory, prefix, parts = pending.pop()
        with os.scandir(directory) as listing:
            children = sorted(listing, key=lambda entry: entry.name)
        for entry in children:
            relative = prefix + entry.name
            here = (*parts, entry.name)
            if relative in named:
                # A secret in a directory's name is in every path under it too.
                plan.omitted.append(Omitted(relative, "secret_name"))
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                if relative in skipped:
                    plan.omitted.append(Omitted(relative, "unreadable"))
                elif relative in in_target:
                    _keep_earlier(
                        plan, workspace, base_root, relative, here, "secret_link_target", known
                    )
                else:
                    stored, why = stored_link_target(workspace, here, os.readlink(entry.path))
                    if stored is None:
                        plan.omitted.append(Omitted(relative, why or "link_outside"))
                    else:
                        plan.links.append((relative, stored))
            elif stat.S_ISDIR(info.st_mode):
                plan.directories.append((relative, stat.S_IMODE(info.st_mode)))
                pending.append((entry.path, relative + "/", here))
            elif stat.S_ISREG(info.st_mode):
                if relative in skipped:
                    plan.omitted.append(Omitted(relative, skipped[relative]))
                elif relative in in_content:
                    _keep_earlier(
                        plan, workspace, base_root, relative, here, "secret_content", known
                    )
                else:
                    plan.files.append((relative, Path(entry.path)))
            else:
                plan.omitted.append(Omitted(relative, "not_regular"))
    plan.omitted.sort(key=lambda item: item.path)
    return plan


class _OverLimit(Exception):
    """Raised while a copy is made, once it would hold more than its limits allow."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class _Budget:
    """What a copy may still hold: how many entries, how many bytes of file content.

    Taken from as the copy is made, so what counts is what the draft would hold:
    the earlier version put back in a path's place included, at the size it has
    when it is read, since it is read from where the operator may edit it.
    """

    entries: int
    data: int

    def add_entry(self) -> None:
        self.entries -= 1
        if self.entries < 0:
            raise _OverLimit(REASON_TOO_MANY_FILES)

    def add_bytes(self, length: int) -> None:
        self.data -= length
        if self.data < 0:
            raise _OverLimit(REASON_TOO_LARGE)


def _copy_regular(source: Path, destination: Path, budget: _Budget) -> None:
    """Copy one regular file's bytes, mode and times, never through a link.

    The copy is the draft's own file -- not a link, not a clone of the inode -- so
    writing to the source afterwards changes nothing here. Setuid, setgid and
    sticky bits are dropped; the owner keeps read and write, so a later copy or
    removal of the draft is never refused by its own files. Every chunk is taken
    from ``budget`` before it is written, so the copy stops at the chunk that
    would take it past the budget (``_OverLimit``).
    """
    fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        reader = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with reader, open(destination, "xb") as writer:
        while chunk := reader.read(_READ_CHUNK):
            budget.add_bytes(len(chunk))
            writer.write(chunk)
    os.chmod(destination, (info.st_mode & 0o777) | 0o600)
    os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns))


def _links_failing_on(root: Path) -> list[Omitted]:
    """Every link under ``root`` that ``stored_link_target``, judged on ``root``, does not keep.

    Walks ``root`` without following a link. Only judges: the caller removes
    what fails once every link has been judged, so no answer depends on the order
    of the walk. Raises ``OSError`` when ``root`` cannot be walked.
    """
    failing: list[Omitted] = []
    pending: list[tuple[str, str, tuple[str, ...]]] = [(os.fspath(root), "", ())]
    while pending:
        directory, prefix, parts = pending.pop()
        with os.scandir(directory) as listing:
            children = sorted(listing, key=lambda entry: entry.name)
        for entry in children:
            relative = prefix + entry.name
            here = (*parts, entry.name)
            if entry.is_symlink():
                stored, why = stored_link_target(root, here, os.readlink(entry.path))
                if stored is None:
                    failing.append(Omitted(relative, why or "link_outside"))
            elif entry.is_dir(follow_symlinks=False):
                pending.append((entry.path, relative + "/", here))
    return failing


def _carry_out(plan: _CopyPlan, incoming: Path, known: list[str]) -> list[Omitted]:
    """Build the copy under ``incoming`` (which exists); returns what was not kept.

    Every entry and every byte of it is counted against the plan's limits as it
    is made (``_Budget``): the check before the scan measured the workspace
    alone. Once every link is in place, every link of the copy is judged again on
    the copy (``_links_failing_on``) and those that fail are removed: the copy
    differs from the workspace wherever an earlier version was put back -- a link
    where the round left a regular file, a link with another target -- and the
    link rule holds only on the tree it was applied to. A path already recorded
    as not kept is not recorded twice. Raises ``_OverLimit`` once the copy would
    hold more than the limits allow, ``OSError`` when it cannot be made.
    """
    budget = _Budget(entries=plan.max_entries, data=plan.max_bytes)
    for relative, _mode in plan.directories:
        budget.add_entry()
        os.mkdir(incoming / relative)
    for relative, source in plan.files:
        budget.add_entry()
        _copy_regular(source, incoming / relative, budget)
    overlap = max((len(secret) for secret in known), default=1) - 1
    for relative in plan.earlier:
        # The earlier version is read from where the operator may edit it, so it
        # is checked as copied, in this directory nothing else writes to. The
        # path is already recorded as not kept (``_keep_earlier``).
        copied = incoming / relative
        if tools._search_file(os.fspath(copied), known, overlap) is not False:
            copied.unlink()
    for relative, target in plan.links:
        budget.add_entry()
        os.symlink(target, incoming / relative)
    # All judged before any is removed: a removal would change the later answers.
    failing = _links_failing_on(incoming)
    for item in failing:
        os.unlink(incoming / item.path)
    recorded = {item.path for item in plan.omitted}
    omitted = [*plan.omitted, *(item for item in failing if item.path not in recorded)]
    omitted.sort(key=lambda item: item.path)
    # Last, so a directory that refuses writes still received its entries. The
    # owner keeps full access for the same reason the files keep read and write.
    for relative, mode in reversed(plan.directories):
        os.chmod(incoming / relative, mode | 0o700)
    return omitted


def remove_entry(path: Path) -> None:
    """Remove a directory tree or a single entry, never following a link. Best effort."""
    try:
        info = os.lstat(path)
    except OSError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _still_the_package(package_root: tools.PackageRoot, identity: tuple[int, int] | None) -> bool:
    """Is ``<tools_dir>/<name>`` still the directory the round started with?

    By ``(st_dev, st_ino)``, never by whether the path exists: a tool deleted and
    installed again under the same name is a different package. What it does not
    cover: a filesystem that hands the freed inode number of a deleted package to
    the next directory made (ext4 can) leaves a same-name package made by hand in
    that window looking the same. Through the backend that cannot happen while a
    job runs, since installing needs the slot the job holds.
    """
    return identity is not None and directory_identity(package_root.path) == identity


def save_round(
    *,
    package_root: tools.PackageRoot,
    package_dir_identity: tuple[int, int] | None,
    workspace: Path,
    earlier_root: Path | None,
    copied_in: TreeDigest | None,
    draft_round: DraftRound,
    start: DraftStart | None,
    previous: PreviousDraft | None,
    max_files: int,
    max_bytes: int,
    durability: Durability,
) -> SaveResult:
    """Keep this round's workspace in the package's draft: steps 4 to 11 of a save.

    The caller has done the rest: the round's commands are confirmed stopped, the
    builder's ``.env`` and root ``.afterthread.meta/`` are stripped, and
    ``workspace`` was checked to be the staging directory it should be. It runs on
    a worker and never raises.

    ``start`` for the round that creates the draft, ``previous`` for one that
    continues it; exactly one is given. ``copied_in`` is ``tree_digest`` of the
    workspace as the round received it, and ``earlier_root`` the version a path
    falls back to when its own content holds a secret: the base version for a
    first round, the previous files directory for a continuation.

    The order is fixed: worth saving (a first round with no change and no notes
    keeps nothing; a continuation with no change records the round only), the
    size limits on the workspace, the links, the secret scan; then, under the
    shared tools lock and only in the same package, the copy to
    ``.incoming-<hex>`` -- counted against the same limits as it is made, the
    earlier versions put back included -- made durable and renamed to
    ``files-<hex>``, the manifest, and the removal of the previous files
    directory. A first round that fails removes what it made, an empty
    ``draft/`` included; a continuation that fails still records the round, its
    files directory unchanged.
    """
    try:
        return _save_round(
            package_root=package_root,
            package_dir_identity=package_dir_identity,
            workspace=workspace,
            earlier_root=earlier_root,
            copied_in=copied_in,
            draft_round=draft_round,
            start=start,
            previous=previous,
            max_files=max_files,
            max_bytes=max_bytes,
            durability=durability,
        )
    except Exception as exc:
        return _not_saved(REASON_UNEXPECTED.format(kind=type(exc).__name__))


def _save_round(
    *,
    package_root: tools.PackageRoot,
    package_dir_identity: tuple[int, int] | None,
    workspace: Path,
    earlier_root: Path | None,
    copied_in: TreeDigest | None,
    draft_round: DraftRound,
    start: DraftStart | None,
    previous: PreviousDraft | None,
    max_files: int,
    max_bytes: int,
    durability: Durability,
) -> SaveResult:
    if (start is None) == (previous is None):
        raise ValueError("a save is either the first round (start) or a continuation (previous)")
    changed = not same_tree(
        copied_in, tree_digest(workspace, max_entries=max_files, max_bytes=max_bytes)
    )
    if previous is None and not changed and not draft_round.has_notes():
        return SaveResult(worth_saving=False)
    # A first round always copies (a draft needs its files); a continuation that
    # changed nothing keeps its files directory and records the round only.
    copies = changed or previous is None

    reason: str | None = None
    known: list[str] = []
    plan: _CopyPlan | None = None
    try:
        known = tools._redaction_secrets()
    except Exception as exc:
        reason = REASON_SCAN_FAILED.format(reason=type(exc).__name__)
    if reason is None and copies:
        reason = _over_limits(workspace, max_files, max_bytes)
    if reason is None and copies:
        scan = tools.scan_embedded_secrets(workspace, known)
        if isinstance(scan, tools.SecretScanFailed):
            reason = REASON_SCAN_FAILED.format(reason=scan.reason)
        else:
            try:
                plan = _plan_copy(workspace, scan, earlier_root, known, max_files, max_bytes)
            except OSError:
                reason = REASON_UNREADABLE
    if previous is None and reason is not None:
        # Nothing exists yet to record the round in.
        return _not_saved(reason)

    try:
        lock = tools.acquire_shared_tools_lock()
    except tools.ToolsLockUnavailableError:
        lock = None
    if lock is None:
        return _not_saved(REASON_LOCK)
    try:
        if not _still_the_package(package_root, package_dir_identity):
            return _not_saved(REASON_PACKAGE_GONE)
        return _save_locked(
            package_root=package_root,
            workspace=workspace,
            plan=plan,
            reason=reason,
            known=known,
            draft_round=draft_round,
            start=start,
            previous=previous,
            max_files=max_files,
            max_bytes=max_bytes,
            durability=durability,
        )
    finally:
        tools.release_tools_lock(lock)


def _save_locked(
    *,
    package_root: tools.PackageRoot,
    workspace: Path,
    plan: _CopyPlan | None,
    reason: str | None,
    known: list[str],
    draft_round: DraftRound,
    start: DraftStart | None,
    previous: PreviousDraft | None,
    max_files: int,
    max_bytes: int,
    durability: Durability,
) -> SaveResult:
    """Steps 8 to 11, under the shared tools lock, in the package the round began in."""
    meta = _own_meta(package_root)
    if not isinstance(meta, Path):
        return _not_saved(REASON_PACKAGE_GONE)
    draft_dir = meta / DRAFT_DIRNAME
    made_draft_dir = False
    current: dict[str, Any] | None = None
    if previous is None:
        try:
            os.mkdir(draft_dir)
        except FileExistsError:
            return _not_saved(REASON_DRAFT_EXISTS)
        except OSError as exc:
            return _not_saved(REASON_COPY.format(kind=type(exc).__name__))
        made_draft_dir = True
        if not durability.directory(meta):
            _undo(draft_dir, None, made_draft_dir)
            return _not_saved(REASON_DURABILITY)
    else:
        current = read_manifest(draft_dir) if _is_real_dir(draft_dir) else None
        if current is None or (current["draft_id"], current["generation"]) != (
            previous.manifest["draft_id"],
            previous.manifest["generation"],
        ):
            # Not the draft the round started from: write nothing into it.
            return _not_saved(REASON_DRAFT_CHANGED)
        if reason is None and plan is None:
            # A continuation that changed nothing: only the round is recorded.
            entry = _round_entry(
                draft_round, current["attempt_count"] + 1, saved=True, reason=None, omitted=[]
            )
            return _record(draft_dir, _with_round(current, entry), known, saved=True)

    if reason is None and plan is not None:
        made, outcome = _copy_and_commit(
            draft_dir,
            workspace,
            plan,
            known,
            draft_round,
            start,
            current,
            previous,
            max_files,
            max_bytes,
            durability,
        )
        if isinstance(outcome, SaveResult):
            return outcome
        reason = outcome
        if made is not None:
            remove_entry(made)
    failed = reason or REASON_UNEXPECTED.format(kind="plan")
    if current is None:
        _undo(draft_dir, None, made_draft_dir)
        return _not_saved(failed)
    entry = _round_entry(
        draft_round, current["attempt_count"] + 1, saved=False, reason=failed, omitted=[]
    )
    recorded = _record(draft_dir, _with_round(current, entry), known, saved=False)
    return _not_saved(failed, draft=_as_draft(recorded))


def _as_draft(result: SaveResult) -> dict[str, Any] | None:
    if result.draft_id is None or result.generation is None or result.attempts is None:
        return None
    return {
        "draft_id": result.draft_id,
        "generation": result.generation,
        "attempt_count": result.attempts,
    }


def _record(draft_dir: Path, doc: dict[str, Any], known: list[str], *, saved: bool) -> SaveResult:
    replaced, _durable = _write_manifest(draft_dir, doc, known)
    if not replaced:
        return _not_saved(REASON_MANIFEST)
    return SaveResult(
        worth_saving=True,
        saved=saved,
        draft_id=doc["draft_id"],
        generation=doc["generation"],
        attempts=doc["attempt_count"],
    )


def _undo(draft_dir: Path, made: Path | None, made_draft_dir: bool) -> None:
    """Take back what a failed first save made: its copy, then an empty ``draft/``."""
    if made is not None:
        remove_entry(made)
    if made_draft_dir:
        with contextlib.suppress(OSError):
            os.rmdir(draft_dir)


def _copy_and_commit(
    draft_dir: Path,
    workspace: Path,
    plan: _CopyPlan,
    known: list[str],
    draft_round: DraftRound,
    start: DraftStart | None,
    current: dict[str, Any] | None,
    previous: PreviousDraft | None,
    max_files: int,
    max_bytes: int,
    durability: Durability,
) -> tuple[Path | None, SaveResult | str]:
    """Steps 9 to 11. Returns what it made that a failure must remove, and the
    result -- or the reason it failed."""
    incoming = draft_dir / f".incoming-{token_hex(16)}"
    try:
        os.mkdir(incoming)
    except OSError as exc:
        return None, REASON_COPY.format(kind=type(exc).__name__)
    try:
        omitted = _carry_out(plan, incoming, known)
    except _OverLimit as over:
        return incoming, over.reason
    except OSError as exc:
        return incoming, REASON_COPY.format(kind=type(exc).__name__)
    with contextlib.suppress(OSError):
        os.chmod(incoming, stat.S_IMODE(os.lstat(workspace).st_mode) | 0o700)
    if not durability.tree(incoming):
        return incoming, REASON_DURABILITY
    files_name = f"files-{token_hex(16)}"
    files = draft_dir / files_name
    try:
        os.rename(incoming, files)
    except OSError as exc:
        return incoming, REASON_COPY.format(kind=type(exc).__name__)
    if not durability.directory(draft_dir):
        return files, REASON_DURABILITY

    keep_previous = False
    kept: list[str] = []
    if current is not None and previous is not None:
        kept = list(current["kept"])
        # The previous copy goes only if nobody changed it while the round ran.
        old = draft_dir / current["files_dir"]
        keep_previous = not same_tree(
            previous.copied_out, tree_digest(old, max_entries=max_files, max_bytes=max_bytes)
        )
        if keep_previous and current["files_dir"] not in kept:
            kept.append(current["files_dir"])
    doc: dict[str, Any]
    if current is None:
        assert start is not None
        doc = {
            "draft_id": token_hex(16),
            "generation": 1,
            "files_dir": files_name,
            "kept": [],
            "base_vid": start.base_vid,
            "feedback": start.feedback,
            "runs": [
                {
                    "id": run.id,
                    "started_at": run.started_at.isoformat(),
                    "workflow": run.workflow,
                    "vid": run.vid,
                    "outcome": run.outcome,
                    "exit_code": run.exit_code,
                }
                for run in start.runs[:_RUNS_KEPT]
            ],
            "attempt_count": 1,
            "attempts": [_round_entry(draft_round, 1, saved=True, reason=None, omitted=omitted)],
            "running": None,
            "publishing": None,
        }
    else:
        entry = _round_entry(
            draft_round, current["attempt_count"] + 1, saved=True, reason=None, omitted=omitted
        )
        doc = {**_with_round(current, entry), "files_dir": files_name, "kept": kept}
    replaced, durable = _write_manifest(draft_dir, doc, known)
    if not replaced:
        return files, REASON_MANIFEST
    if current is not None and not keep_previous and durable:
        # Only once the new manifest is known to be on disk; otherwise the sweep
        # takes it later, after its own fsync of the manifest.
        remove_entry(draft_dir / current["files_dir"])
    return None, SaveResult(
        worth_saving=True,
        saved=True,
        draft_id=doc["draft_id"],
        generation=doc["generation"],
        attempts=doc["attempt_count"],
    )


# --- after the save: publishing, removal, discard -------------------------------


def _locked_in_package(
    package_root: tools.PackageRoot,
    package_dir_identity: tuple[int, int] | None,
    act: Callable[[Path], Any],
) -> Any:
    """Run ``act(draft_dir)`` under the shared tools lock in the round's own package.

    None, without running it, when the lock cannot be had, the package is no
    longer the one the round began in, or it has no ``draft/`` of its own that
    can be looked at (``_own_draft``).
    """
    try:
        lock = tools.acquire_shared_tools_lock()
    except tools.ToolsLockUnavailableError:
        return None
    if lock is None:
        return None
    try:
        if not _still_the_package(package_root, package_dir_identity):
            return None
        found = _own_draft(package_root)
        if found is None or found[1] is None:
            return None
        return act(found[0])
    finally:
        tools.release_tools_lock(lock)


def _matching_draft(draft_dir: Path, expected: tuple[str, int]) -> dict[str, Any] | None:
    current = read_manifest(draft_dir) if _is_real_dir(draft_dir) else None
    if current is None or (current["draft_id"], current["generation"]) != expected:
        return None
    return current


def mark_publishing(
    package_root: tools.PackageRoot,
    package_dir_identity: tuple[int, int] | None,
    expected: tuple[str, int],
    job_id: str | None,
) -> int | None:
    """Record in the manifest that this job is about to publish from the draft.

    What lets the list tell, after a restart, a publish that went through from one
    that did not (``draft_view``). Returns the new generation, None when nothing
    was written. Never raises.
    """

    def act(draft_dir: Path) -> int | None:
        current = _matching_draft(draft_dir, expected)
        if current is None:
            return None
        doc = {
            **current,
            "generation": current["generation"] + 1,
            "publishing": {"job_id": job_id, "at": _now_iso()},
        }
        replaced, _durable = _write_manifest(draft_dir, doc, tools._redaction_secrets())
        return doc["generation"] if replaced else None

    try:
        return _locked_in_package(package_root, package_dir_identity, act)
    except Exception:
        return None


def clear_publishing(
    package_root: tools.PackageRoot,
    package_dir_identity: tuple[int, int] | None,
    expected: tuple[str, int],
    error: str,
) -> int | None:
    """After a publish that failed: the draft is the work again, with the error.

    Clears ``publishing`` and records the failure on the latest round. Returns the
    new generation, None when nothing was written. Never raises.
    """

    def act(draft_dir: Path) -> int | None:
        current = _matching_draft(draft_dir, expected)
        if current is None:
            return None
        attempts = [*current["attempts"]]
        attempts[-1] = {**attempts[-1], "outcome": "failed", "error": error}
        doc = {
            **current,
            "generation": current["generation"] + 1,
            "attempts": attempts,
            "publishing": None,
        }
        replaced, _durable = _write_manifest(draft_dir, doc, tools._redaction_secrets())
        return doc["generation"] if replaced else None

    try:
        return _locked_in_package(package_root, package_dir_identity, act)
    except Exception:
        return None


def _discard_name(meta: Path) -> Path:
    return meta / f"{DRAFT_DIRNAME}.discarded-{token_hex(16)}"


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


def _take_away(draft_dir: Path) -> Path | None:
    """Rename ``draft/`` aside, then remove it; returns what is not seen gone.

    The rename makes the draft disappear in one step; what its removal leaves is
    under a name only the sweep collects. When even the rename is refused, the
    draft is removed where it is, as far as that goes. A path counts as removed
    only once it is seen gone (``_seen_gone``).
    """
    aside = _discard_name(draft_dir.parent)
    try:
        os.rename(draft_dir, aside)
    except OSError:
        remove_entry(draft_dir)
        return None if _seen_gone(draft_dir) else draft_dir
    remove_entry(aside)
    return None if _seen_gone(aside) else aside


@dataclass(frozen=True, slots=True)
class Removal:
    """A draft taken away, or not: ``removed`` once ``draft/`` is seen gone, and what is left.

    ``removed`` False with ``draft/`` itself as ``residual_path``: the removal ran
    and ``draft/`` is not seen gone, so the draft may still be there. False with
    no ``residual_path``: nothing was taken away.
    """

    removed: bool
    residual_path: Path | None


def _removal(draft_dir: Path) -> Removal:
    """Take ``draft/`` away and say what became of it, from what is seen afterwards."""
    left = _take_away(draft_dir)
    if not _seen_gone(draft_dir):
        return Removal(False, draft_dir)
    return Removal(True, left)


def remove_after_publish(
    package_root: tools.PackageRoot,
    package_dir_identity: tuple[int, int] | None,
    expected: tuple[str, int],
) -> Removal:
    """Remove the draft a publish just put live. Never raises.

    Only the draft the publish came from, at the generation it marked: anything
    else is left alone. Not removed, it stays with ``publishing`` set, and the
    list reconciles it as published.
    """

    def act(draft_dir: Path) -> Removal:
        if _matching_draft(draft_dir, expected) is None:
            return Removal(False, None)
        return _removal(draft_dir)

    try:
        done = _locked_in_package(package_root, package_dir_identity, act)
    except Exception:
        return Removal(False, None)
    return done if isinstance(done, Removal) else Removal(False, None)


DiscardOutcome = Removal | Literal["not_found", "changed"]


def discard(
    package_root: tools.PackageRoot,
    *,
    draft_id: str | None = None,
    generation: int | None = None,
    token: str | None = None,
) -> DiscardOutcome:
    """Discard the package's draft, if it is still the one the caller saw.

    The caller holds the slot, which keeps every job, and so every save and the
    sweep, away; this takes the shared tools lock, which keeps ``delete_tool``
    away. The draft is compared by ``(draft_id, generation)`` or by ``token``
    (``discard_token``, which every draft has, whatever its manifest holds); a
    mismatch is ``changed``. Any state can be discarded, but only the package's
    own draft (``_own_draft``): through a link to another package's metadata
    there is nothing to discard. ``Removal.removed`` only once ``draft/`` is seen
    gone (``_seen_gone``); one that cannot be looked at afterwards is not removed,
    and is what is left.

    Raises ``tools.ToolsLockUnavailableError`` when the lock path is unusable.
    """
    lock = tools.acquire_shared_tools_lock()
    if lock is None:
        return "not_found"
    try:
        if not _is_real_dir(package_root.path):
            return "not_found"
        found = _own_draft(package_root)
        if found is None:
            return "not_found"
        draft_dir, _info = found
        if token is not None:
            same = discard_token(draft_dir) == token
        else:
            same = (
                draft_id is not None
                and generation is not None
                and _matching_draft(draft_dir, (draft_id, generation)) is not None
            )
        if not same:
            return "changed"
        return _removal(draft_dir)
    finally:
        tools.release_tools_lock(lock)


# --- the sweep -------------------------------------------------------------------


def _fsync_file(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        os.fsync(fd)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def sweep_package(package_dir: Path, *, fsync_directory: Callable[[Path], bool]) -> None:
    """Collect one package's draft leftovers. Best effort, never raises.

    Called by ``tool_builder._sweep_stale_backups`` for each package, under its
    exclusive tools lock, and takes no lock itself. It touches only names this
    module makes under ``<package>/.afterthread.meta/``: what a discard left
    (``draft.discarded-<hex>``), and inside ``draft/`` the ``files-<hex>`` the
    manifest neither names nor keeps and the ``.incoming-<hex>`` a crash left. The
    second kind only when the manifest can be trusted: it reads back in shape, its
    files directory is there, and both it and ``draft/`` are fsynced first, so
    that nothing it names can be taken back by a crash after the collection. Any
    doubt leaves all of ``draft/`` alone. Nothing is followed through a link: the
    metadata directory is reached through ``_own_meta``.
    """
    with contextlib.suppress(Exception):
        meta = _own_meta(tools.PackageRoot(package_dir))
        if not isinstance(meta, Path):
            return
        with os.scandir(meta) as listing:
            leftovers = [entry.name for entry in listing if _DISCARDED_RE.fullmatch(entry.name)]
        for name in sorted(leftovers):
            remove_entry(meta / name)
        draft_dir = meta / DRAFT_DIRNAME
        if not _is_real_dir(draft_dir):
            return
        manifest = read_manifest(draft_dir)
        if manifest is None or not _is_real_dir(draft_dir / manifest["files_dir"]):
            return
        if not _fsync_file(draft_dir / MANIFEST_FILENAME) or not fsync_directory(draft_dir):
            return
        keep = {manifest["files_dir"], *manifest["kept"]}
        with os.scandir(draft_dir) as listing:
            names = sorted(entry.name for entry in listing)
        for name in names:
            unnamed_copy = _FILES_RE.fullmatch(name) is not None and name not in keep
            half_made = _INCOMING_RE.fullmatch(name) is not None
            if (unnamed_copy or half_made) and _is_real_dir(draft_dir / name):
                shutil.rmtree(draft_dir / name, ignore_errors=True)
