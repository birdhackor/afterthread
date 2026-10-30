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
copy of the work with it; a successful publish removes it, but only while it
still holds just what that save made (``remove_after_publish``). One draft per
tool: while ``draft/`` exists in any state, a revise that does not name it is
refused (``draft_state``), and one that names it continues it
(``judge_continuation``). A continuing round marks itself in the manifest
(``begin_round``), copies the draft's files into its own new workspace with the
same pipeline a save copies with (``copy_into_workspace``), and is recorded when
it ends -- by its save, or on its own (``record_round``); one that never began
takes its mark back (``end_unstarted``).

The draft lives at the package layer, so it goes wherever the package goes: a
deleted tool takes its draft along, a same-name reinstall starts without one,
and ``.staging`` keeps its meaning of one session's scratch space. Nothing here
removes anything from ``.staging``; the one write into it is a continuation's
copy of the draft into its new, empty workspace, and a workspace there is
otherwise only read.

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
  What cannot be looked at is taken as there by a read and left alone by a
  write: such a draft is listed and gates a revise, and a discard of it removes
  nothing and answers that it may still be there (``_seen_own_draft``).
* TEXT. Every free-text string written into the manifest is masked against the
  secrets known at that moment, cut to its cap, made encodable
  (``tools._utf8_safe``: a file name read from disk can carry lone surrogates,
  which the UTF-8 encode of the whole document would refuse), and then checked
  in the form the file holds it, a JSON string literal, with the mask's own
  matcher (``tools._redacted``): an escape the encoder writes, or the replacement
  characters the scrub leaves, can spell a known value the text did not, and
  such a string is written as the redaction marker, whole. A value whose shape a
  check here fixes -- an identifier, a timestamp this module wrote, a number, a
  vocabulary word -- is checked against that shape instead of masked; a mask
  could only corrupt it. What a run record hands over is free text. The keys
  are this module's literals. Before anything is written, the whole file is
  checked the same way, less what a shape check fixed and less the keys
  (``tools._encode_checked``), so a text that reached it some other way is seen
  too: when that check finds a secret, every free text is written as the
  marker, and when it still finds one, nothing is written
  (``REASON_MANIFEST_SECRET``). What it does not cover: a secret registered with
  a real line break that a text spells as the two characters ``\\n``, a value
  encoded another way (base64, say), a ``.env`` value whose undecodable bytes
  were replaced when it was read -- the match is exact. Reading makes every
  string encodable again, since a hand edit bypasses the writer, and what goes
  to the API is masked once more: a secret registered after the write must not
  come back unmasked.
* SECRETS. The embedded-secret scanner (``tools.scan_embedded_secrets``) runs on
  two trees, with the secrets known when the save starts. On the workspace,
  before anything is copied, it decides what is copied: a path whose name holds
  a secret is not; one whose content or link target does is replaced by its
  earlier version -- the one the round started from -- when that is a regular
  file, or a link LINKS keeps. On the copy, once every entry is in it and its
  links are judged, before it is made durable, it decides what is kept: every
  path whose content, name or stored link target holds a secret is removed and
  recorded as not kept; one it could not search is removed too (LOOKS). The
  second scan is what keeps known secrets out of the files the draft keeps,
  because the copy is not the workspace: an earlier version is put back without
  being scanned, and an absolute link target inside the workspace is stored
  relative, behind a ``../`` for each directory the link is in: added text,
  which with what follows it can spell a secret the target as written did not.
  A scan of either tree that cannot finish keeps no files. Content is never
  rewritten with redaction markers, which could break the program.
* LINKS. A link is kept only when its target, resolved as a string from the
  link's own directory, stays inside the tree, and no ``..`` in it follows a
  component that is itself a link (``stored_link_target`` says why) or that
  cannot be looked at (``unreadable``, LOOKS). The rule is applied twice: on the
  workspace, to plan the copy, and on the copy once every link is in it, before
  it is made durable, to every link of the copy. The copy differs from the
  workspace wherever an earlier version was put back, and the rule holds only on
  the tree it was applied to. Every link is judged before any is removed; what
  fails is removed and recorded as not kept. Then each link the copy still holds
  is resolved the way the operating system resolves it, errors raised, and kept
  only when it lands inside the copy or resolves nowhere at a point inside it
  (``_links_failing_resolution``): a check of the property itself that rests on
  nothing the rule decides and changes no stored target. An absolute target is
  inside the workspace only when it begins with the workspace's own path, as
  given or as resolved, followed by a separator or by nothing -- the resolved
  path only when it can be had with errors raised; without it, a target that
  does not begin with the given path is ``unreadable``. It is stored relative, as
  what follows that path behind a ``..`` for each directory the link is in,
  every component as written but the empty ones a link at the top would begin
  with, and judged by the same walk as a relative target. Folding ``x/..`` away
  first, as a path normalizer would, can hide the link ``x`` the ``..`` follows.
  Not covered: a kept link whose way runs through an entry the copy does not
  keep -- a link that failed, a FIFO -- dangles in the copy and is not recorded
  on its own: dangling is safe, and what it runs through is recorded as not
  kept, itself or a directory it is in.
* LOOKS. A save leaves an entry of the workspace out by a decision about it -- a
  known secret, a link not kept, an entry seen to be no regular file, directory
  or link -- or because a look at it failed: the scan could not search it, a
  look on a link's way or at where it resolves failed (``LookFailed``), the
  earlier version to put back could not be looked at. The second keeps no files
  at all (``REASON_UNLOOKED``): a copy without the entry, committed, could let
  the previous files directory and the staging go while they still held it, and
  the entry would be nowhere. A copy into a continuation's workspace leaves such
  an entry out, recorded as not brought in; the draft still holds it.
* DURABILITY. A new copy is written under a temporary name, made durable and
  renamed into place before the manifest names it, and the manifest is replaced
  atomically. A continuation's previous copy is named in ``kept`` by the same
  write that names the new one, and removed on the spot only once that manifest
  is confirmed on disk, the workspace received all of the copy as it was when
  the round began, and nobody changed it since; otherwise it stays, listed. A
  name leaves ``kept`` only when a later rewrite sees it gone. The sweep never
  takes a ``files-<hex>`` the manifest does not name: a crash can bring an older
  manifest back, and the copy it no longer names may be the one the operator
  was shown and edited.
"""

import contextlib
import errno
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
from afterthread.services.tool_runs import RUNS_PER_REVISE, AttachedRun

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
# A round's operator note: the request's own cap on ``feedback``, so a note is
# kept whole.
NOTE_CAP = 20_000
OMITTED_KEPT = 50
_PATH_CAP = 300
# The run records a draft keeps: as many as one revise carries, its next round's.
_RUNS_KEPT = RUNS_PER_REVISE
_RUN_TEXT_CAP = 200
# The list shows the last summary as an excerpt.
_LAST_SUMMARY_CAP = 300
_READ_CHUNK = 1024 * 1024

DraftState = Literal["running", "unusable", "publishing", "unknown", "published", "stale", "open"]
# ``interrupted``: a round marked running that left no record of its end, recorded
# by the next round that began; its ``finished_at`` is null.
RoundOutcome = Literal["published", "not_published", "failed", "interrupted"]
# Why a path of the workspace is not in the draft, or a path of the draft is not
# in a continuing round's workspace. ``env_file`` and ``backend_meta`` are why a
# continuation does not bring in a root entry the strip before the save removes
# (``STRIPPED_ROOT_NAMES``).
OmitReason = Literal[
    "secret_name",
    "secret_content",
    "secret_link_target",
    "link_outside",
    "link_ambiguous",
    "unreadable",
    "not_regular",
    "env_file",
    "backend_meta",
]
_OMIT_REASONS = frozenset(get_args(OmitReason))
# The entries at the root of a round's workspace that are stripped before its work
# is saved: the builder's ``.env`` (``tool_builder._strip_builder_env``) and the
# backend's metadata directory (``tool_builder._strip_builder_sidecars``), each
# strip finding what it removes by its name here; a strip of another root name
# takes its name from here too. A continuation's copy of the draft withholds each,
# under the reason it is keyed by, and any name that is the same entry spelled in
# another case (``_not_brought_in``): brought in, it would be stripped before the
# save -- through a name here, which on a filesystem that does not tell case apart
# reaches the alias too -- and the new files directory would lack it, while the
# previous one, brought in whole and unchanged, would go.
STRIPPED_ROOT_NAMES: dict[OmitReason, str] = {
    "env_file": ".env",
    "backend_meta": tools._META_DIRNAME,
}
_OUTCOMES = frozenset(get_args(RoundOutcome))
_STOPPED_BY = frozenset(get_args(llm.FinalizeTrigger))
_FAILURES = frozenset(get_args(llm.RunFailure))

# Why a round's files were not saved, or a continuation did not begin:
# user-facing (zh-TW, rendered by the 工具 page) and category-only, never a value,
# and a path only in the one reason that must name its entries (``REASON_UNLOOKED``).
REASON_STRIP_ENV = "無法清除建置器寫入的 .env"
REASON_STRIP_SIDECARS = "無法清除工作區裡的後端中繼資料"
# The workspace could not be confirmed to be the staging directory it was given:
# where it is now is not known, so no path is claimed for it.
REASON_WORKSPACE = "無法確認暫存工作區仍在原本的路徑"
REASON_TOO_MANY_FILES = "檔案數超過上限（TOOL_DRAFT_MAX_FILES）"  # noqa: RUF001
REASON_TOO_LARGE = "總大小超過上限（TOOL_DRAFT_MAX_BYTES）"  # noqa: RUF001
REASON_UNREADABLE = "無法讀取暫存工作區"
REASON_SCAN_FAILED = "秘密掃描失敗（{reason}），無法確認檔案裡沒有已知的秘密值"  # noqa: RUF001
# The known secrets could not all be read: ``detail`` is their own message, which
# names the ``.env`` and what to check (``tools.KnownSecretsIncomplete``).
REASON_SECRETS_INCOMPLETE = "無法確認檔案裡沒有已知的秘密值。{detail}"
# The model's notes could not be masked, the known secrets not being whole: the
# round ends there, nothing saved and the manifest left as it is.
REASON_NOTES_UNMASKED = "已知的秘密值清單不完整，無法遮蔽模型交回的筆記"  # noqa: RUF001
REASON_LOCK = "無法取得工具目錄的鎖"
REASON_PACKAGE_GONE = "工具已被刪除或替換"
REASON_DRAFT_EXISTS = "這個工具已經有草稿"
REASON_DRAFT_CHANGED = "草稿在這一輪進行中被改動過"
# A continuation that changed nothing, whose files directory is not seen to be what
# the round copied out of it: the directory is left as it is, and the round's
# files are not in the draft.
REASON_FILES_CHANGED = (
    "草稿的檔案目錄在這一輪進行中被改過、被移走，或無法確認沒有變動，"  # noqa: RUF001
    "所以這一輪的檔案沒有存進草稿"
)
REASON_COPY = "無法複製檔案到草稿（{kind}）"  # noqa: RUF001
# A save that would leave out an entry because a look at it failed, not because
# of a decision about it: whether the entry may be kept is not known, so no files
# are saved. ``{paths}`` names the first ``_UNLOOKED_LISTED`` of them, masked and
# cut like any text the API serves, and how many there are when there are more.
REASON_UNLOOKED = "有項目查詢失敗，無法確認能否存進草稿，所以這一輪的檔案沒有存：{paths}"  # noqa: RUF001
_UNLOOKED_LISTED = 5
REASON_DURABILITY = "無法確認草稿已安全寫入磁碟"
REASON_MANIFEST = "無法寫入草稿的說明檔"
REASON_MANIFEST_SECRET = "草稿的說明檔寫下去仍會含有已知的秘密值，沒有寫入"  # noqa: RUF001
REASON_UNEXPECTED = "保存時發生未預期錯誤（{kind}）"  # noqa: RUF001
# The ``reason`` of a round recorded as ``interrupted``.
REASON_INTERRUPTED = "這一輪沒有留下結束紀錄，它的進度沒有存進草稿"  # noqa: RUF001
# Why a continuation did not begin: nothing was spent, and no round is counted.
START_UNSEEN = "看不到這個工具的草稿"
START_CHANGED = "草稿在送出之後已經變更"
START_PUBLISHING = "草稿正在發布、已發布或結果未知"
START_KEPT_FULL = (
    "草稿保留的舊檔案目錄已達上限（{max} 份）：請刪掉草稿目錄裡不需要的舊目錄，"  # noqa: RUF001
    "或取回需要的內容後放棄草稿"
)
REASON_FILES_MISSING = "草稿的檔案目錄不在"
REASON_DRAFT_UNREADABLE = "無法讀取草稿的檔案目錄"
REASON_COPY_IN = "無法把草稿的檔案複製到暫存工作區（{kind}）"  # noqa: RUF001
# A save's note on a previous files directory it did not remove (``SaveResult.reason``
# of a round that was saved): where it is, and why it stays.
NOTE_PREVIOUS_KEPT = "上一份檔案目錄沒有刪除，留在 {path}：{why}"  # noqa: RUF001
KEPT_NOT_DURABLE = "這一輪的說明檔沒有確認寫入磁碟"
KEPT_NOT_ALL_BROUGHT_IN = "它有內容沒有帶進這一輪的工作區，或無法確認帶進來的與它相同"  # noqa: RUF001
KEPT_CHANGED = "它在這一輪進行中被改過，或無法確認沒有被改過"  # noqa: RUF001

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
    # The operator's note for this round: a continuation's own words (a first
    # round's are the draft's ``feedback``).
    note: str | None = None
    # A continuation's: the paths of the draft its workspace did not receive, and
    # why (``copy_into_workspace``), and the run records it newly attached, which
    # join the manifest's ``runs`` when the round is recorded.
    not_brought_in: tuple[Omitted, ...] = ()
    attached: tuple[AttachedRun, ...] = ()

    def has_notes(self) -> bool:
        """Whether the model handed back any of its three notes."""
        return bool(self.summary or self.handoff or self.operator_action)


@dataclass(frozen=True, slots=True)
class PreviousDraft:
    """The draft a continuing round copied its workspace from.

    ``manifest`` is the manifest as the round's own ``begin_round`` wrote it --
    ``running`` naming the round's job -- which every later write of the round
    is matched against. ``copied_out`` is ``tree_digest`` of its files directory
    taken just before that directory was copied into the workspace
    (``copy_into_workspace``): what the save compares that directory against --
    before it removes it, once a new one is committed, and before it keeps it as
    the draft's for a round that changed nothing (``_still_as_copied_out``).
    """

    manifest: dict[str, Any]
    copied_out: TreeDigest | None


@dataclass(frozen=True, slots=True)
class Durability:
    """The fsync helpers a save uses, handed in by its caller (``tool_builder``)."""

    tree: Callable[[Path], bool]
    directory: Callable[[Path], bool]


@dataclass(frozen=True, slots=True)
class SavedCopy:
    """The files directory one save made, and ``tree_digest`` of it as made.

    ``digest`` is taken of the copy once it is durable, before it is renamed into
    place, with the limits the save counted it against (``max_entries``,
    ``max_bytes``); None when it could not be read whole. What a removal after a
    publish compares the draft against (``remove_after_publish``): the copy, not
    the workspace it came from, which may hold entries the copy does not.
    """

    files_dir: str
    digest: TreeDigest | None
    max_entries: int
    max_bytes: int


@dataclass(frozen=True, slots=True)
class SaveResult:
    """What one save did.

    ``worth_saving`` False: nothing to keep (no change, no notes), which is not a
    failure. Otherwise three things, every branch that writes the manifest
    filling in all of them:

    * ``saved``: the draft's files directory holds every entry of this round's
      workspace but those left out by a decision about them (a known secret, a
      link not kept, an entry seen to be no regular file, directory or link) -- a
      new one the save committed, which it does only when no entry was left out
      because a look at it failed (``REASON_UNLOOKED``), or, for a continuation
      that changed nothing, the one it already had, once it is seen still to be
      what the round copied out of it (``_still_as_copied_out``);
    * ``recorded``: the manifest records this round (and no longer marks it
      running). A save that did not save often recorded the round still, with
      why -- never the save a publish follows (``save_round``);
    * ``durable``: the write that recorded it is confirmed on disk. Never retried:
      an fsync that failed and then succeeds proves nothing about the write before.

    ``reason`` is why the files were not saved, or, when they were, a note -- a
    previous files directory left where it is, and why. ``copy`` is the files
    directory a save that copied made (``SavedCopy``).
    """

    worth_saving: bool
    saved: bool = False
    draft_id: str | None = None
    generation: int | None = None
    attempts: int | None = None
    reason: str | None = None
    copy: SavedCopy | None = None
    recorded: bool = False
    durable: bool = False

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
    either cannot be looked at for another reason, which leaves open whether it
    is a link. What cannot be seen is taken one way by a read and the other way
    by a write: a read takes it as a draft that is there (``_own_draft``, behind
    ``has_draft`` and ``draft_view``); a write, a discard included, does nothing
    to it (``_seen_own_draft``; a save and the sweep go on only with a real
    directory).

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
    """The package's own ``draft/`` as a read sees it, and its ``lstat``; None when it has none.

    Reached through ``_own_meta``. The ``lstat`` is None when ``draft/``, or a
    directory above it, cannot be looked at: for a read, such a draft is there
    (``has_draft``). Not for a write, which leaves what it cannot see alone
    (``_seen_own_draft``).
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


def _seen_own_draft(package_root: tools.PackageRoot) -> tuple[Path, os.stat_result] | None:
    """The package's own ``draft/`` and its ``lstat``, for a write; None unless both are seen.

    How the marks around a publish, the removal after one and a discard reach
    the draft they act on (``_locked_in_package``, ``discard``): ``_own_meta``
    must give a real directory, and the ``lstat`` of ``draft/`` must succeed.
    Whatever cannot be looked at is left alone: a metadata directory that cannot
    may be a link to another package's, and a ``draft/`` that cannot is not known
    to be there. A save and the sweep make their own demands of what they reach
    (``_save_locked``, ``sweep_package``).
    """
    meta = _own_meta(package_root)
    if not isinstance(meta, Path):
        return None
    draft_dir = meta / DRAFT_DIRNAME
    try:
        return draft_dir, os.lstat(draft_dir)
    except OSError:
        return None


def has_draft(package_root: tools.PackageRoot) -> bool:
    """Whether the package has a draft: ``draft/`` exists, whatever its state.

    Its own draft only (``_own_draft``): a package whose directory or metadata
    directory is a link has none. A ``draft/`` without a readable manifest still
    counts: it may hold the complete ``files-*`` a crash left before its manifest
    was written, the only copy of that work. It is a read, so what cannot be
    looked at counts as present. A write takes the same draft the other way: a
    discard of it, for one, removes nothing and answers that the draft may still
    be there (``_seen_own_draft``).
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


def _free(value: Any, cap: int, writing: tools._Writing) -> str | None:
    """A free-text value of the manifest, as ``writing`` writes it (``tools._redacted``)."""
    if value is not None and not isinstance(value, str):
        raise _BadShape
    return writing.text(value, cap)


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


def _run_entry(raw: Any, writing: tools._Writing) -> Any:
    run = _mapping(raw)
    exit_code = None if run.get("exit_code") is None else _signed(run.get("exit_code"))
    return writing.record(
        {
            "id": writing.fixed(_count(run.get("id"), minimum=1)),
            "started_at": _free(run.get("started_at"), _RUN_TEXT_CAP, writing),
            "workflow": _free(run.get("workflow"), _RUN_TEXT_CAP, writing),
            "vid": _free(run.get("vid"), _RUN_TEXT_CAP, writing),
            "outcome": _free(run.get("outcome"), _RUN_TEXT_CAP, writing),
            "exit_code": writing.fixed(exit_code),
        }
    )


def _signed(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _BadShape
    return value


def _omitted_entry(raw: Any, writing: tools._Writing) -> Any:
    entry = _mapping(raw)
    if entry.get("path") is None:
        raise _BadShape
    return writing.record(
        {
            "path": _free(entry.get("path"), _PATH_CAP, writing),
            "reason": writing.fixed(_vocabulary(entry.get("reason"), _OMIT_REASONS)),
        }
    )


def _omitted_listing(
    attempt: dict[str, Any], key: str, count_key: str, writing: tools._Writing
) -> tuple[list[Any], int]:
    """One of a round's two path lists, at most ``OMITTED_KEPT`` long, and its full count."""
    entries = [_omitted_entry(item, writing) for item in _listing(attempt.get(key), OMITTED_KEPT)]
    count = _count(attempt.get(count_key))
    if count < len(entries):
        raise _BadShape
    return entries, count


def _attempt_entry(raw: Any, writing: tools._Writing) -> Any:
    attempt = _mapping(raw)
    omitted, omitted_count = _omitted_listing(attempt, "omitted", "omitted_count", writing)
    # A manifest written before continuations existed has no such list: none.
    if "not_brought_in" not in attempt and "not_brought_in_count" not in attempt:
        attempt = {**attempt, "not_brought_in": [], "not_brought_in_count": 0}
    not_brought_in, not_brought_in_count = _omitted_listing(
        attempt, "not_brought_in", "not_brought_in_count", writing
    )
    outcome = _vocabulary(attempt.get("outcome"), _OUTCOMES)
    finished_at = _optional_matching(attempt.get("finished_at"), _TIMESTAMP_RE)
    # Only a round that left no record of its end has no end time, and it has none.
    if (finished_at is None) != (outcome == "interrupted"):
        raise _BadShape
    return writing.record(
        {
            "number": writing.fixed(_count(attempt.get("number"), minimum=1)),
            "job_id": writing.fixed(_optional_matching(attempt.get("job_id"), _ID_RE)),
            "started_at": writing.fixed(
                _optional_matching(attempt.get("started_at"), _TIMESTAMP_RE)
            ),
            "finished_at": writing.fixed(finished_at),
            "outcome": writing.fixed(outcome),
            "stopped_by": writing.fixed(_vocabulary(attempt.get("stopped_by"), _STOPPED_BY)),
            "failure": writing.fixed(_vocabulary(attempt.get("failure"), _FAILURES)),
            "rounds_used": writing.fixed(_optional_count(attempt.get("rounds_used"))),
            "max_rounds": writing.fixed(_optional_count(attempt.get("max_rounds"))),
            "error": _free(attempt.get("error"), ERROR_CAP, writing),
            "summary": _free(attempt.get("summary"), SUMMARY_CAP, writing),
            "handoff": _free(attempt.get("handoff"), HANDOFF_CAP, writing),
            "operator_action": _free(attempt.get("operator_action"), OPERATOR_ACTION_CAP, writing),
            "note": _free(attempt.get("note"), NOTE_CAP, writing),
            "saved": writing.fixed(_flag(attempt.get("saved"))),
            "reason": _free(attempt.get("reason"), ERROR_CAP, writing),
            "omitted": omitted,
            "omitted_count": writing.fixed(omitted_count),
            "not_brought_in": not_brought_in,
            "not_brought_in_count": writing.fixed(not_brought_in_count),
        }
    )


def _marker(raw: Any, time_key: str, writing: tools._Writing) -> Any:
    """``running`` or ``publishing``: which job, since when."""
    if raw is None:
        return None
    marker = _mapping(raw)
    stamp = _optional_matching(marker.get(time_key), _TIMESTAMP_RE)
    if stamp is None:
        raise _BadShape
    return writing.record(
        {
            "job_id": writing.fixed(_optional_matching(marker.get("job_id"), _ID_RE)),
            time_key: writing.fixed(stamp),
        }
    )


def _document(raw: Any, writing: tools._Writing) -> Any:
    """The manifest built from ``raw`` field by field, every value checked.

    The ONE description of the manifest's shape, and every value goes to
    ``writing`` (``tools._Writing``) as what it is: fixed by a shape check, or
    free text. The writer runs it over what it is about to write -- for the
    file, with the secrets known now, so every text is masked, cut and checked as
    written, and for the probe its backstop looks at -- and the reader over what
    it read back (with no secrets, which still makes every text encodable and
    cut). Keys are this function's literals, never the input's. Raises
    ``_BadShape`` on anything out of shape.
    """
    doc = _mapping(raw)
    files_dir = _matching(doc.get("files_dir"), _FILES_RE)
    kept = [_matching(name, _FILES_RE) for name in _listing(doc.get("kept"), _KEPT_MAX)]
    if files_dir in kept or len(set(kept)) != len(kept):
        raise _BadShape
    raw_attempts = _listing(doc.get("attempts"), ATTEMPTS_KEPT)
    attempts = [_attempt_entry(item, writing) for item in raw_attempts]
    # Taken from what was just checked: what ``writing`` gives back may be a probe.
    numbers = [item["number"] for item in raw_attempts]
    attempt_count = _count(doc.get("attempt_count"), minimum=1)
    if (
        not attempts
        or numbers != sorted(set(numbers))
        or numbers[-1] != attempt_count
        or attempt_count < len(attempts)
    ):
        raise _BadShape
    return writing.record(
        {
            "draft_id": writing.fixed(_matching(doc.get("draft_id"), _ID_RE)),
            "generation": writing.fixed(_count(doc.get("generation"), minimum=1)),
            "files_dir": writing.fixed(files_dir),
            "kept": writing.fixed(kept),
            "base_vid": writing.fixed(_matching(doc.get("base_vid"), tools._VID_RE)),
            "feedback": _free(doc.get("feedback"), FEEDBACK_CAP, writing),
            "runs": [_run_entry(item, writing) for item in _listing(doc.get("runs"), _RUNS_KEPT)],
            "attempt_count": writing.fixed(attempt_count),
            "attempts": attempts,
            "running": _marker(doc.get("running"), "started_at", writing),
            "publishing": _marker(doc.get("publishing"), "at", writing),
        }
    )


def _manifest_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _manifest_data(doc: dict[str, Any], known: list[str]) -> bytes | str:
    """The manifest file for ``doc``, or the reason there is none.

    ``REASON_MANIFEST_SECRET`` when it would hold a known secret even with every
    free text written as the marker (``tools._encode_checked``);
    ``REASON_MANIFEST`` when it is out of shape or too large.
    """
    try:
        text = tools._encode_checked(lambda writing: _document(doc, writing), known, _manifest_text)
    except _BadShape, TypeError, ValueError:
        return REASON_MANIFEST
    if text is None:
        return REASON_MANIFEST_SECRET
    data = text.encode("utf-8")
    if len(data) > _MANIFEST_MAX_BYTES:
        return REASON_MANIFEST
    return data


def manifest_bytes(doc: dict[str, Any], known: list[str]) -> bytes | None:
    """The manifest file for ``doc``; None when there is none to write (``_manifest_data``)."""
    data = _manifest_data(doc, known)
    return data if isinstance(data, bytes) else None


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
        document = _document(raw, tools._Writing([]))
    except _BadShape:
        return None
    return document if isinstance(document, dict) else None


@dataclass(frozen=True, slots=True)
class _ManifestWrite:
    """One replacement of the manifest: whether it happened, whether it is on disk, why not."""

    replaced: bool
    durable: bool = False
    reason: str = REASON_MANIFEST


def _write_manifest(draft_dir: Path, doc: dict[str, Any], known: list[str]) -> _ManifestWrite:
    """Replace the manifest atomically."""
    data = _manifest_data(doc, known)
    if not isinstance(data, bytes):
        return _ManifestWrite(replaced=False, reason=data)
    durable: list[bool] = []
    replaced = tools._write_package_file_atomic(
        draft_dir, MANIFEST_FILENAME, data, None, durability_out=durable
    )
    return _ManifestWrite(replaced=replaced, durable=replaced and bool(durable) and durable[0])


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
        "omitted": _path_list(omitted),
        "omitted_count": len(omitted),
        "not_brought_in": _path_list(draft_round.not_brought_in),
        "not_brought_in_count": len(draft_round.not_brought_in),
    }


def _path_list(paths: list[Omitted] | tuple[Omitted, ...]) -> list[dict[str, str]]:
    """The first ``OMITTED_KEPT`` of ``paths`` as the manifest lists them."""
    return [{"path": item.path, "reason": item.reason} for item in paths[:OMITTED_KEPT]]


def _interrupted_entry(marker: dict[str, Any], number: int) -> dict[str, Any]:
    """The round a ``running`` mark stands for, when the round that set it left no
    record of its end: when it began and which job ran it, and nothing else."""
    return {
        "number": number,
        "job_id": marker["job_id"],
        "started_at": marker["started_at"],
        "finished_at": None,
        "outcome": "interrupted",
        "stopped_by": None,
        "failure": None,
        "rounds_used": None,
        "max_rounds": None,
        "error": None,
        "summary": None,
        "handoff": None,
        "operator_action": None,
        "note": None,
        "saved": False,
        "reason": REASON_INTERRUPTED,
        "omitted": [],
        "omitted_count": 0,
        "not_brought_in": [],
        "not_brought_in_count": 0,
    }


def _run_record(run: AttachedRun) -> dict[str, Any]:
    """One attached run record as the manifest's ``runs`` keeps it: a summary only."""
    return {
        "id": run.id,
        "started_at": run.started_at.isoformat(),
        "workflow": run.workflow,
        "vid": run.vid,
        "outcome": run.outcome,
        "exit_code": run.exit_code,
    }


def _merged_runs(attached: tuple[AttachedRun, ...], recorded: list[Any]) -> list[Any]:
    """The manifest's ``runs`` once a continuing round is recorded: what the round
    newly attached first, then what was recorded before, each id once, the last
    ``_RUNS_KEPT`` attached. The order is the order of attaching, not of running."""
    merged: list[Any] = []
    seen: set[int] = set()
    for entry in [*(_run_record(run) for run in attached), *recorded]:
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        merged.append(entry)
    return merged[:_RUNS_KEPT]


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


def _entry_identity(path: Path, *, with_change: bool) -> list[Any] | None:
    """What ``lstat`` says of ``path``; ``["absent"]`` when it is seen not to be
    there (``ENOENT``, ``ENOTDIR``), None when it cannot be looked at."""
    try:
        info = os.lstat(path)
    except FileNotFoundError, NotADirectoryError:
        return ["absent"]
    except OSError:
        return None
    identity: list[Any] = [stat.S_IFMT(info.st_mode), info.st_dev, info.st_ino]
    if with_change:
        identity += [info.st_ctime_ns, info.st_size]
    return identity


def _entries_identity(draft_dir: Path) -> list[Any] | None:
    """The names of ``draft/``'s own entries, sorted, each with its ``_entry_identity``;
    ``["absent"]`` when ``draft/`` is seen to hold none (``ENOENT``, ``ENOTDIR``),
    None when it or an entry cannot be looked at."""
    try:
        with os.scandir(draft_dir) as listing:
            names = sorted(entry.name for entry in listing)
    except FileNotFoundError, NotADirectoryError:
        return ["absent"]
    except OSError:
        return None
    entries: list[Any] = []
    for name in names:
        identity = _entry_identity(draft_dir / name, with_change=False)
        if identity is None:
            return None
        entries.append([name, *identity])
    return entries


def _observed_token(draft_dir: Path) -> tuple[str, bool]:
    """``discard_token``'s value, and whether every look it rests on succeeded.

    Verified: the value stands for the draft only when each look succeeded or saw
    nothing there -- a look that fails the same way twice says nothing of whether
    the draft between them is the same one. Not covered: what ``discard_token``
    names as matching anyway, an empty ``draft/`` made again on the old inode.
    """
    manifest = _entry_identity(draft_dir / MANIFEST_FILENAME, with_change=True)
    observed: list[Any] = [_entry_identity(draft_dir, with_change=False), manifest]
    if manifest is None or manifest == ["absent"]:
        observed.append(_entries_identity(draft_dir))
    token = hashlib.sha256(json.dumps(observed).encode("ascii")).hexdigest()
    return token, None not in observed


def discard_token(draft_dir: Path) -> str:
    """The opaque value that stands for this draft as it is now, for a discard.

    Taken from what ``lstat`` says of ``draft/`` and of its manifest, so it exists
    whatever the manifest holds -- unreadable, out of shape, missing, not a file.
    A look that fails other than by seeing nothing there (``ENOENT``,
    ``ENOTDIR``) leaves the value for display only: a discard takes its own look
    under the lock and removes nothing unless each look in it succeeds or sees
    nothing there (``_observed_token``).

    With a manifest, the manifest's inode, ctime and size carry it: every rewrite
    of the manifest replaces it with a new file, and a draft made again after a
    discard writes a new one, so a token taken earlier no longer matches. The
    ctime is what carries that where a filesystem hands a freed inode number to
    the next file.

    Without one -- no entry by that name, or one that cannot be looked at -- the
    names and identities of ``draft/``'s own entries carry it too. Short of
    removing the draft, the only backend write that changes them while there is
    no manifest is the first round making that ``draft/``, which ends by writing
    one or by taking back what it made, unless the process stops first; a
    discard never runs beside it (the caller holds the slot). A
    later first round refuses a ``draft/`` that exists, and a continuation, the
    marks around a publish and the sweep each need a manifest they can read. A
    save names what it leaves in ``draft/`` at random (``files-<hex>``,
    ``.incoming-<hex>``), so a ``draft/`` made again with anything in it does not
    match, even in a directory with the old inode number. What it does not
    cover: an empty ``draft/`` made again with the old inode number matches; it
    holds nothing.

    With a manifest the entries are left out, and the directory's own times
    always are: the sweep and a save in progress change them without changing
    the draft.
    """
    return _observed_token(draft_dir)[0]


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


def _running_now(manifest: dict[str, Any], job_active: Callable[[str], bool]) -> bool:
    running = manifest["running"]
    return running is not None and running["job_id"] is not None and job_active(running["job_id"])


def _state_of(
    manifest: dict[str, Any],
    draft_dir: Path,
    resolution: tools.Resolution,
    job_active: Callable[[str], bool],
) -> DraftState:
    """The state of a draft whose manifest reads back in shape (``draft_state``)."""
    if _running_now(manifest, job_active):
        return "running"
    if not _is_real_dir(draft_dir / manifest["files_dir"]):
        return "unusable"
    if manifest["publishing"] is not None:
        publishing_job = manifest["publishing"]["job_id"]
        if publishing_job is not None and job_active(publishing_job):
            return "publishing"
        return "published" if _published_from(manifest, resolution) else "unknown"
    if not isinstance(resolution, tools.Resolved) or resolution.vid != manifest["base_vid"]:
        return "stale"
    return "open"


def _seen_manifest(
    package_root: tools.PackageRoot,
) -> tuple[Path, os.stat_result | None, dict[str, Any] | None] | None:
    """The package's own ``draft/`` as a read sees it, its ``lstat``, and its manifest
    when that reads back in shape; None when it has no draft (``_own_draft``)."""
    found = _own_draft(package_root)
    if found is None:
        return None
    draft_dir, info = found
    manifest = read_manifest(draft_dir) if info is not None and stat.S_ISDIR(info.st_mode) else None
    return draft_dir, info, manifest


def draft_state(
    package_root: tools.PackageRoot,
    resolution: tools.Resolution,
    *,
    job_active: Callable[[str], bool],
) -> DraftState | None:
    """The state of the package's draft, or None when it has none.

    In this order: ``running`` (a continuation of this process is on it),
    ``unusable`` (``draft/`` cannot be looked at, the manifest cannot be used, or
    its files directory is not a directory), ``publishing``/``published``/
    ``unknown`` (a publish from it was started: still going in this process, put
    live -- ``current`` names it as its origin -- or neither known), ``stale``
    (``current`` moved off its base), ``open``. The one judgment of it: the list
    shows it (``draft_view``), and a revise that does not name the draft is told
    by it what it can do (``tool_builder.start_revise_job``). A read: a draft
    that cannot be looked at is there, and unusable.
    """
    seen = _seen_manifest(package_root)
    if seen is None:
        return None
    draft_dir, _info, manifest = seen
    if manifest is None:
        return "unusable"
    return _state_of(manifest, draft_dir, resolution, job_active)


def _kept_paths(draft_dir: Path, manifest: dict[str, Any]) -> list[str]:
    """The previous files directories ``kept`` names, but those seen gone."""
    return [str(draft_dir / name) for name in manifest["kept"] if not _seen_gone(draft_dir / name)]


def draft_view(
    package_root: tools.PackageRoot,
    resolution: tools.Resolution,
    *,
    job_active: Callable[[str], bool],
    known: Callable[[], list[str]],
) -> dict[str, Any] | None:
    """The package's draft as the tools list shows it, or None when it has none.

    ``state`` is ``draft_state``'s. ``interrupted``: a round marked as running is
    no longer running. ``path`` is its files directory (``draft/`` itself when it
    is unusable), ``draft_dir`` all of ``draft/``, which may hold more than the
    list names, and ``kept`` the previous files directories a continuation left
    -- every name ``kept`` holds that is not seen gone.

    ``job_active`` answers for a job id of this process; ``known`` gives the
    secrets the text is masked with, and lets its failure through: the list fails
    rather than serve text it could not mask. Only the package's own draft is
    shown (``_own_draft``).
    """
    seen = _seen_manifest(package_root)
    if seen is None:
        return None
    draft_dir, _info, manifest = seen
    token = discard_token(draft_dir)
    if manifest is None:
        return {
            "draft_id": None,
            "generation": None,
            "attempts": None,
            "state": "unusable",
            "interrupted": False,
            "base_vid": None,
            "path": str(draft_dir),
            "draft_dir": str(draft_dir),
            "kept": [],
            "discard_token": token,
            "last": None,
        }
    files = draft_dir / manifest["files_dir"]
    state = _state_of(manifest, draft_dir, resolution, job_active)
    secrets = known()
    last = manifest["attempts"][-1]
    return {
        "draft_id": manifest["draft_id"],
        "generation": manifest["generation"],
        "attempts": manifest["attempt_count"],
        "state": state,
        "interrupted": manifest["running"] is not None and not _running_now(manifest, job_active),
        "base_vid": manifest["base_vid"],
        "path": str(draft_dir if state == "unusable" else files),
        "draft_dir": str(draft_dir),
        "kept": _kept_paths(draft_dir, manifest),
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


# --- continuing a draft: what a revise that names it is judged by -----------------


@dataclass(frozen=True, slots=True)
class FirstRead:
    """What the revise route reads of a draft before it takes the slot: which
    draft, at which generation, and the run records its manifest keeps (``runs``,
    summaries only), for the route to read again from the database."""

    draft_id: str
    generation: int
    runs: tuple[dict[str, Any], ...]


def first_read(package_root: tools.PackageRoot) -> FirstRead | None:
    """The route's look at the package's draft, before the slot; None when it reads nothing.

    Only for the records to read again with the request's database session: what
    it reads decides nothing until the judgment after the slot
    (``judge_continuation``) compares it with what it reads then. A draft that
    cannot be read is answered as nothing read.
    """
    seen = _seen_manifest(package_root)
    if seen is None or seen[2] is None:
        return None
    manifest = seen[2]
    return FirstRead(manifest["draft_id"], manifest["generation"], tuple(manifest["runs"]))


ContinueRefusal = Literal[
    "draft_gone", "draft_unusable", "draft_changed", "draft_publishing", "version_mismatch"
]


def judge_continuation(
    package_root: tools.PackageRoot,
    resolution: tools.Resolution,
    expected: tuple[str, int],
    first_seen: tuple[str, int] | None,
) -> dict[str, Any] | ContinueRefusal:
    """Whether a revise naming ``expected`` -- ``(draft_id, generation)`` -- may continue
    the package's draft: its manifest when it may, else why not.

    Called after the slot is taken, so what it reads holds until the job begins
    (only a hand edit changes it meanwhile, which ``begin_round`` checks again).
    The first of these that holds answers:

    1. ``draft/`` is seen not to exist, or the package has no draft of its own
       (``_own_draft``): ``draft_gone``;
    2. ``draft/`` cannot be looked at, is not a directory, or its manifest cannot
       be read or is out of shape: ``draft_unusable``;
    3. another ``draft_id``: ``draft_gone`` -- discarded and made again;
    4. another generation: ``draft_changed``;
    5. its files directory is not a real directory: ``draft_unusable``;
    6. ``publishing`` is set -- publishing, published or not known; the origin is
       not read: ``draft_publishing``;
    7. its ``base_vid`` is not ``current``, whatever the request compared:
       ``version_mismatch``;
    8. ``first_seen``, what the route read before the slot (None: it read
       nothing), is not this draft at this generation: ``draft_changed`` -- the
       records the round would carry are those that manifest kept.
    """
    seen = _seen_manifest(package_root)
    if seen is None:
        return "draft_gone"
    draft_dir, _info, manifest = seen
    if manifest is None:
        return "draft_unusable"
    if manifest["draft_id"] != expected[0]:
        return "draft_gone"
    if manifest["generation"] != expected[1]:
        return "draft_changed"
    if not _is_real_dir(draft_dir / manifest["files_dir"]):
        return "draft_unusable"
    if manifest["publishing"] is not None:
        return "draft_publishing"
    if not isinstance(resolution, tools.Resolved) or resolution.vid != manifest["base_vid"]:
        return "version_mismatch"
    if first_seen != expected:
        return "draft_changed"
    return manifest


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


class LookFailed(Exception):
    """A look at an entry failed other than by seeing nothing there, so what it was
    to decide is not known. Raised where the look is made: what the caller then
    leaves out is left out because of a failed look, never because of a decision
    about the entry, and a save keeps no files for it (``REASON_UNLOOKED``)."""


def _unlooked_reason(paths: list[str], known: list[str]) -> str:
    """``REASON_UNLOOKED`` naming ``paths``: the first few, masked and cut, and how
    many there are when there are more."""
    ordered = sorted(set(paths))
    shown = "、".join(_shown(path, known, _PATH_CAP) or "" for path in ordered[:_UNLOOKED_LISTED])
    if len(ordered) > _UNLOOKED_LISTED:
        shown += f" 等 {len(ordered)} 項"
    return REASON_UNLOOKED.format(paths=shown)


def _is_link(path: Path) -> bool:
    """Whether ``path`` is a link, by its ``lstat``.

    Seen not to exist (``ENOENT``), or under a component that is no directory
    (``ENOTDIR``), it is no link: the operating system's resolution fails there
    too. Any other error says nothing about it and is raised (``OSError``):
    ``os.path.islink`` answers False for it, taking a link it could not see for
    something else.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError, NotADirectoryError:
        return False
    return stat.S_ISLNK(info.st_mode)


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

    A relative target comes back as it was. An absolute one is inside ``root``
    only when it begins with root's own path, as given or as resolved, ending at
    a separator or at the end of the target: that prefix is the one part read as
    text, since through it the operating system reaches ``root`` however its
    parents are reached. What follows the prefix comes back behind one ``..`` for
    each directory the link is in, every component as written -- ``.``, ``..``
    and empty ones too, since folding a ``..`` away before the walk would hide the
    link it follows -- and is walked like a relative target; ``.`` when nothing is
    left. The one change: for a link at the top of ``root``, the empty components
    it would begin with are dropped, since written first they would make it
    absolute. One that begins with neither path is ``link_outside``.

    What cannot be looked at raises ``LookFailed``, and the caller keeps no link:
    a component before a ``..`` whose ``lstat`` fails other than by not being
    there (``_is_link``), and an absolute target that does not begin with the path
    as given while the resolved path cannot be had with errors raised -- a
    resolution that swallows them can hand back a path ``root`` is not at.
    Verified: every answer rests on a look that succeeded or saw nothing there.
    Not covered: a component that changes between this look and the
    operating system's.
    """
    link_dir = list(parts[:-1])
    relative = target
    if os.path.isabs(target):
        spellings = [os.fspath(root)]
        unresolved: OSError | None = None
        try:
            spellings.append(os.path.realpath(root, strict=True))
        except OSError as exc:
            unresolved = exc
        inside: list[str] | None = None
        for spelling in dict.fromkeys(spellings):
            prefix = spelling.rstrip(os.sep)
            if target == prefix or target.startswith(prefix + os.sep):
                inside = target[len(prefix) :].split(os.sep)[1:]
                break
        if inside is None:
            if unresolved is not None:
                raise LookFailed from unresolved
            return None, "link_outside"
        if not link_dir:
            while inside and not inside[0]:
                inside.pop(0)
        relative = "/".join([".."] * len(link_dir) + inside) or "."
    stack = list(link_dir)
    for component in relative.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            if not stack:
                return None, "link_outside"
            try:
                is_link = _is_link(root.joinpath(*stack))
            except OSError as exc:
                raise LookFailed from exc
            if is_link:
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
    omitted: list[Omitted] = field(default_factory=list)
    # The paths the plan leaves out because a look failed -- the scan could not
    # search them, a look on a link's way failed, the earlier version to put back
    # could not be looked at -- each also in ``omitted``. A save keeps no files
    # when there is one (``REASON_UNLOOKED``); a copy into a continuation's
    # workspace leaves them out like the rest.
    unlooked: list[str] = field(default_factory=list)


def _earlier_entry(
    base_root: Path | Literal["unseen"] | None, parts: tuple[str, ...]
) -> Path | None:
    """The same path in the earlier version, reached through real directories only.

    None when there is seen to be none: no earlier version (``base_root`` None),
    or a directory on the way seen not to be there or not to be one. Raises
    ``LookFailed`` when one of them cannot be looked at, or when the earlier
    version could not be reached at all (``_UNSEEN``).
    """
    if base_root is None:
        return None
    if not isinstance(base_root, Path):
        raise LookFailed
    directories = [base_root]
    for name in parts[:-1]:
        directories.append(directories[-1] / name)
    for directory in directories:
        try:
            info = os.lstat(directory)
        except FileNotFoundError, NotADirectoryError:
            return None
        except OSError as exc:
            raise LookFailed from exc
        if not stat.S_ISDIR(info.st_mode):
            return None
    return directories[-1] / parts[-1]


def _keep_earlier(
    plan: _CopyPlan,
    workspace: Path,
    base_root: Path | Literal["unseen"] | None,
    relative: str,
    parts: tuple[str, ...],
    reason: str,
) -> None:
    """Plan the earlier version of a path whose own content or target holds a secret.

    The path is recorded as not kept either way: this round's version of it is
    not in the draft, whether the earlier one takes its place or nothing does.
    Only a regular file or a link is put back; a link only if the link rule keeps
    it on the workspace, and, like every link, it is judged again on the copy.
    Whether the earlier version holds a secret itself is not looked at here: the
    scan of the copy (``_carry_out``) decides that, on what was copied. Nothing is
    put back where the earlier version is seen to have no such path, or to hold
    something else there; where a look at it fails -- other than by seeing nothing
    there -- nothing is put back either, and the path goes in ``plan.unlooked``:
    whether it had an earlier version to keep is not known.
    """
    plan.omitted.append(Omitted(relative, reason))
    try:
        source = _earlier_entry(base_root, parts)
        if source is None:
            return
        try:
            info = os.lstat(source)
        except FileNotFoundError, NotADirectoryError:
            return
        if stat.S_ISREG(info.st_mode):
            plan.files.append((relative, source))
        elif stat.S_ISLNK(info.st_mode):
            stored, _why = stored_link_target(workspace, parts, os.readlink(source))
            if stored is not None:
                plan.links.append((relative, stored))
    except OSError, LookFailed:
        plan.unlooked.append(relative)


def _plan_copy(
    workspace: Path,
    scan: tools.SecretScanDone,
    base_root: Path | Literal["unseen"] | None,
    max_entries: int,
    max_bytes: int,
    *,
    left_out: dict[str, OmitReason] | None = None,
) -> _CopyPlan:
    """Decide, entry by entry, what the copy takes of the tree at ``workspace``.

    ``workspace`` is the source root: a round's workspace when a save copies it
    into the draft, the draft's files directory when a continuation copies it into
    its own workspace (``copy_into_workspace``). ``base_root`` is the earlier
    version a path whose own version holds a secret is put back from: None when
    there is none, ``_UNSEEN`` when it could not be reached. ``left_out`` names
    entries at the top of the source the copy does not take, with why. What the
    copy then keeps of it is decided on the copy (``_carry_out``). An entry the
    scan could not search, or whose link cannot be judged for a failed look, is
    left out and in ``plan.unlooked``, as is one whose earlier version could not
    be looked at (``_keep_earlier``). Raises ``OSError`` when the source cannot be
    walked.
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
            if not parts and left_out and entry.name in left_out:
                plan.omitted.append(Omitted(relative, left_out[entry.name]))
                continue
            if relative in named:
                # A secret in a directory's name is in every path under it too.
                plan.omitted.append(Omitted(relative, "secret_name"))
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                if relative in skipped:
                    # The scan could not read its target.
                    plan.omitted.append(Omitted(relative, "unreadable"))
                    plan.unlooked.append(relative)
                elif relative in in_target:
                    _keep_earlier(plan, workspace, base_root, relative, here, "secret_link_target")
                else:
                    target = os.readlink(entry.path)
                    try:
                        stored, why = stored_link_target(workspace, here, target)
                    except LookFailed:
                        plan.omitted.append(Omitted(relative, "unreadable"))
                        plan.unlooked.append(relative)
                    else:
                        if stored is None:
                            plan.omitted.append(Omitted(relative, why or "link_outside"))
                        else:
                            plan.links.append((relative, stored))
            elif stat.S_ISDIR(info.st_mode):
                plan.directories.append((relative, stat.S_IMODE(info.st_mode)))
                pending.append((entry.path, relative + "/", here))
            elif stat.S_ISREG(info.st_mode):
                if relative in skipped:
                    # A regular file the scan could not search, whatever it says why.
                    plan.omitted.append(Omitted(relative, skipped[relative]))
                    plan.unlooked.append(relative)
                elif relative in in_content:
                    _keep_earlier(plan, workspace, base_root, relative, here, "secret_content")
                else:
                    plan.files.append((relative, Path(entry.path)))
            else:
                # Seen to be no regular file, directory or link: a FIFO, say.
                plan.omitted.append(Omitted(relative, "not_regular"))
    plan.omitted.sort(key=lambda item: item.path)
    return plan


class _CopyNotKept(Exception):
    """Raised while a copy is made, once it cannot be kept: ``reason`` is why, for the round."""

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
            raise _CopyNotKept(REASON_TOO_MANY_FILES)

    def add_bytes(self, length: int) -> None:
        self.data -= length
        if self.data < 0:
            raise _CopyNotKept(REASON_TOO_LARGE)


def _copy_regular(source: Path, destination: Path, budget: _Budget) -> None:
    """Copy one regular file's bytes, mode and times, never through a link.

    The copy is the draft's own file -- not a link, not a clone of the inode -- so
    writing to the source afterwards changes nothing here. Setuid, setgid and
    sticky bits are dropped; the owner keeps read and write, so a later copy or
    removal of the draft is never refused by its own files. Every chunk is taken
    from ``budget`` before it is written, so the copy stops at the chunk that
    would take it past the budget (``_CopyNotKept``).
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


# What a check of the copy leaves out, and the paths among it left out because a
# look failed (``LookFailed``, a scan that could not search) rather than by a
# decision about them.
_Judged = tuple[list[Omitted], list[str]]


def _links_failing_on(root: Path) -> _Judged:
    """Every link under ``root`` that ``stored_link_target``, judged on ``root``, does not keep.

    Walks ``root`` without following a link. Only judges: the caller removes
    what fails once every link has been judged, so no answer depends on the order
    of the walk. A link whose judgment fails for a look (``LookFailed``) fails as
    ``unreadable``, and its path is among the unlooked. Raises ``OSError`` when
    ``root`` cannot be walked.
    """
    failing: list[Omitted] = []
    unlooked: list[str] = []
    pending: list[tuple[str, str, tuple[str, ...]]] = [(os.fspath(root), "", ())]
    while pending:
        directory, prefix, parts = pending.pop()
        with os.scandir(directory) as listing:
            children = sorted(listing, key=lambda entry: entry.name)
        for entry in children:
            relative = prefix + entry.name
            here = (*parts, entry.name)
            if entry.is_symlink():
                target = os.readlink(entry.path)
                try:
                    stored, why = stored_link_target(root, here, target)
                except LookFailed:
                    failing.append(Omitted(relative, "unreadable"))
                    unlooked.append(relative)
                    continue
                if stored is None:
                    failing.append(Omitted(relative, why or "link_outside"))
            elif entry.is_dir(follow_symlinks=False):
                pending.append((entry.path, relative + "/", here))
    return failing, unlooked


# The errors ``os.path.realpath(..., strict=True)`` raises where the operating
# system's own resolution fails too: a name that is not there, a component that
# is no directory, a cycle of links. Any other error -- a denied search, which
# depends on who asks; a name too long as the resolved string spells it,
# though not as the link does; an I/O error -- leaves the answer unknown.
_RESOLVES_NOWHERE = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


def _within_path(path: str, real_root: str) -> bool:
    """Whether the resolved ``path`` is ``real_root`` or lies under it."""
    return path == real_root or path.startswith(real_root.rstrip(os.sep) + os.sep)


def _resolution_failure(link: str, real_root: str | None) -> OmitReason | None:
    """Why the link at ``link`` is not kept, judged by where the operating system
    resolves it; None when it is kept. ``real_root`` is the tree's resolved path,
    None when that could not be had. Raises ``LookFailed`` when where it lands is
    not known: no ``real_root``, or a resolution failing other than by resolving
    nowhere."""
    if real_root is None:
        raise LookFailed
    try:
        landed = os.path.realpath(link, strict=True)
    except OSError as exc:
        if exc.errno not in _RESOLVES_NOWHERE or not isinstance(exc.filename, str):
            raise LookFailed from exc
        # Where the resolution fails: built from the resolved path, like ``landed``.
        landed = exc.filename
    return None if _within_path(landed, real_root) else "link_outside"


def _links_failing_resolution(root: Path) -> _Judged:
    """Every link under ``root`` whose resolution is not seen to stay inside it.

    The copy's second check of its links, after the link rule
    (``_links_failing_on``) and resting on nothing it decides: each link is
    resolved with ``os.path.realpath(..., strict=True)``, which raises its errors
    where the default swallows them. Kept: one that lands inside ``root``'s
    resolved path, and one that resolves nowhere (``_RESOLVES_NOWHERE``) at a
    point inside it, since the copy only loses entries after the check. One that
    lands outside is ``link_outside``, and so is one that resolves nowhere at a
    point outside: what is written there later -- the draft's manifest, in a
    first round -- would make it resolve. Any other error, or a ``root`` whose own
    resolution fails, leaves it unconfirmed: ``unreadable``, its path among the
    unlooked (``LookFailed``). Only judges, every link before any is removed, and
    changes no stored target. Raises ``OSError`` when ``root`` cannot be walked.

    Verified: every link it keeps resolves inside ``root`` or nowhere, as the
    tree is when checked. Not covered: a way out of ``root`` and back into it,
    which lands inside when checked and may lead elsewhere once what lies
    outside changes; the link rule refuses every such way.
    """
    try:
        real_root: str | None = os.path.realpath(root, strict=True)
    except OSError:
        real_root = None
    failing: list[Omitted] = []
    unlooked: list[str] = []
    pending: list[tuple[str, str]] = [(os.fspath(root), "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as listing:
            children = sorted(listing, key=lambda entry: entry.name)
        for entry in children:
            relative = prefix + entry.name
            if entry.is_symlink():
                try:
                    why = _resolution_failure(entry.path, real_root)
                except LookFailed:
                    failing.append(Omitted(relative, "unreadable"))
                    unlooked.append(relative)
                    continue
                if why is not None:
                    failing.append(Omitted(relative, why))
            elif entry.is_dir(follow_symlinks=False):
                pending.append((entry.path, relative + "/"))
    return failing, unlooked


# What the copy records a path as when the embedded-secret scan finds a secret in it.
_HIT_REASONS: dict[tools.SecretHitKind, OmitReason] = {
    "content": "secret_content",
    "name": "secret_name",
    "link_target": "secret_link_target",
}


def _within(relative: str, removed: set[str]) -> bool:
    """Whether ``relative`` is one of ``removed`` or lies under one of them."""
    parts = relative.split("/")
    return any("/".join(parts[:end]) in removed for end in range(1, len(parts) + 1))


def _remove_secrets(root: Path, known: list[str]) -> _Judged:
    """Scan the copy under ``root`` as it was written, and remove what the scan does not clear.

    ``tools.scan_embedded_secrets`` with ``known``: every path whose content,
    name or link target holds one of them, or that the scan could not search, is
    removed -- a directory with everything under it -- and returned as not kept,
    once, under the first reason the scan gives for it. A path removed because
    the scan could not search it, whatever reason the scan gives, is among the
    unlooked; one inside a path already removed for a hit is not. Raises
    ``_CopyNotKept`` when the scan cannot finish, ``OSError`` when a path cannot
    be removed.
    """
    scan = tools.scan_embedded_secrets(root, known)
    if isinstance(scan, tools.SecretScanFailed):
        raise _CopyNotKept(REASON_SCAN_FAILED.format(reason=scan.reason))
    found = [(Omitted(hit.path, _HIT_REASONS[hit.kind]), False) for hit in scan.hits]
    found += [(Omitted(skip.path, skip.reason), True) for skip in scan.skipped]
    removed: list[Omitted] = []
    unlooked: list[str] = []
    gone: set[str] = set()
    # A stable sort: a path keeps its reasons in the order the scan gave them,
    # its hits before its skips.
    for item, unsearched in sorted(found, key=lambda pair: pair[0].path):
        if _within(item.path, gone):
            continue
        path = root / item.path
        if stat.S_ISDIR(os.lstat(path).st_mode):
            shutil.rmtree(path)
        else:
            os.unlink(path)
        gone.add(item.path)
        removed.append(item)
        if unsearched:
            unlooked.append(item.path)
    return removed, unlooked


def _carry_out(plan: _CopyPlan, incoming: Path, known: list[str]) -> _Judged:
    """Build the copy under ``incoming`` (which exists); returns what was not kept,
    and the paths the checks of the copy left out because a look failed (``_Judged``;
    the plan's own are ``plan.unlooked``).

    Every entry and every byte of it is counted against the plan's limits as it
    is made (``_Budget``): the check before the scan measured the workspace
    alone. The copy is not the workspace -- an earlier version was put back in
    some paths without being scanned, and an absolute link target inside the
    workspace is stored rewritten, relative -- so once every entry is in place,
    what was judged on the workspace is judged again on the copy: every link by
    the link rule (``_links_failing_on``), all judged before any that fails is
    removed, since the rule holds only on the tree it was applied to; then every
    link left by where it resolves, the way the operating system resolves it
    (``_links_failing_resolution``), a check that does not rest on the rule; then
    the whole copy by the embedded-secret scan with ``known``, the secrets the
    save started with (``_remove_secrets``). What any of them removes is recorded
    as not kept, a path already recorded only once. Raises ``_CopyNotKept`` once
    the copy would hold more than the limits allow or its scan cannot finish,
    ``OSError`` when it cannot be made.
    """
    budget = _Budget(entries=plan.max_entries, data=plan.max_bytes)
    for relative, _mode in plan.directories:
        budget.add_entry()
        os.mkdir(incoming / relative)
    for relative, source in plan.files:
        budget.add_entry()
        _copy_regular(source, incoming / relative, budget)
    for relative, target in plan.links:
        budget.add_entry()
        os.symlink(target, incoming / relative)
    # All judged before any is removed: a removal would change the later answers.
    failing, failing_unlooked = _links_failing_on(incoming)
    for item in failing:
        os.unlink(incoming / item.path)
    # Then what the rule kept, by where the operating system resolves each link;
    # again all judged first.
    unresolved, unresolved_unlooked = _links_failing_resolution(incoming)
    for item in unresolved:
        os.unlink(incoming / item.path)
    # Each directory takes its workspace mode, the owner keeping full access for
    # the same reason the files keep read and write: neither the removals below
    # nor a later removal of the draft is refused by a directory of its own.
    for relative, mode in reversed(plan.directories):
        os.chmod(incoming / relative, mode | 0o700)
    holding, holding_unlooked = _remove_secrets(incoming, known)
    omitted = list(plan.omitted)
    recorded = {item.path for item in omitted}
    for item in (*failing, *unresolved, *holding):
        if item.path not in recorded:
            recorded.add(item.path)
            omitted.append(item)
    omitted.sort(key=lambda item: item.path)
    # Taken apart from ``omitted``, where a path is recorded once under its first
    # reason: a path left out for a secret may then be one whose put-back version
    # the copy's scan could not search.
    unlooked = sorted({*failing_unlooked, *unresolved_unlooked, *holding_unlooked})
    return omitted, unlooked


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
    first round, the previous files directory for a continuation
    (``previous_files``). A round whose ``outcome`` is None is the save a publish
    follows.

    The order is fixed: worth saving (a first round with no change and no notes
    keeps nothing; a continuation with no change records the round only -- as
    saved while its files directory is seen still to be what the round copied
    out of it, as not saved otherwise -- unless a publish follows: that save
    always copies, so the removal after the publish has a copy this save made to
    compare against), the known secrets, the size
    limits on the workspace, the secret scan of the workspace, the plan of the
    copy, which judges its links on the workspace; then, under the shared tools
    lock and only in the same package, the copy to ``.incoming-<hex>`` -- counted
    against the same limits as it is made, the earlier versions put back
    included, then its links judged again and the whole of it scanned again, on
    the copy itself (``_carry_out``) -- made durable and renamed to
    ``files-<hex>``, the manifest, and what becomes of the previous files
    directory (``_copy_and_commit``). The plan and the checks of the copy leave an
    entry out by a decision about it -- a known secret, a link not kept, an entry
    seen to be no regular file, directory or link -- or because a look at it
    failed (``LookFailed``, an entry the scan could not search, an earlier
    version to put back that could not be looked at); the second fails the save
    like a size limit does (``REASON_UNLOOKED``), the copy, when one was made,
    removed: committed without the entry, it could let the previous files
    directory and the staging go while they still held it. A first round that
    fails removes what it made, an empty ``draft/`` included; a continuation that
    fails records the round where it can, its files directory unchanged. It
    cannot when the known secrets could not all be read -- nothing is written at
    all -- nor when the lock is not had, the package is not the one the round
    began in, or the draft is no longer the one the round began on
    (``begin_round``'s manifest, its ``running`` this round's job): nothing is
    written into it. Nor, of course, when that write fails. A continuation's save
    a publish follows writes nothing when it fails, even where it could:
    ``running`` stays, for the round to be recorded once the publish's result is
    known (``record_round``). ``SaveResult.recorded`` says which it was.
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
    # A first round always copies (a draft needs its files), and so does the save
    # a publish follows; a continuation that changed nothing otherwise keeps its
    # files directory and records the round only.
    copies = changed or previous is None or draft_round.outcome is None

    try:
        known = tools._redaction_secrets()
    except tools.KnownSecretsIncomplete as exc:
        # The known secrets are not whole: no file is checked, and no text written.
        return _not_saved(REASON_SECRETS_INCOMPLETE.format(detail=exc))
    except Exception as exc:
        return _not_saved(REASON_SCAN_FAILED.format(reason=type(exc).__name__))
    reason: str | None = None
    plan: _CopyPlan | None = None
    if copies:
        reason = _over_limits(workspace, max_files, max_bytes)
    if reason is None and copies:
        scan = tools.scan_embedded_secrets(workspace, known)
        if isinstance(scan, tools.SecretScanFailed):
            reason = REASON_SCAN_FAILED.format(reason=scan.reason)
        else:
            # A save always has an earlier version: without its path, the earlier
            # version could not be reached (``previous_files``).
            base = _UNSEEN if earlier_root is None else earlier_root
            try:
                plan = _plan_copy(workspace, scan, base, max_files, max_bytes)
            except OSError:
                reason = REASON_UNREADABLE
            else:
                if plan.unlooked:
                    # An entry left out for a failed look: no files are saved.
                    reason = _unlooked_reason(plan.unlooked, known)
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
            copied_in=copied_in,
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
    copied_in: TreeDigest | None,
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
        current = _running_draft(
            draft_dir,
            (previous.manifest["draft_id"], previous.manifest["generation"]),
            draft_round.job_id,
        )
        if current is None:
            # Not the draft the round began on, or not marked as this round's:
            # write nothing into it.
            return _not_saved(REASON_DRAFT_CHANGED)
        if reason is None and plan is None:
            # A continuation that changed nothing: only the round is recorded, and
            # its files directory stays the draft's. That directory holds the round's
            # files only if it is seen, now, to be what the round copied out of it;
            # edited or moved during the round, or not to be told, it is left as it
            # is and the round is recorded below as not saved.
            if _still_as_copied_out(
                draft_dir / current["files_dir"], previous, max_files, max_bytes
            ):
                entry = _round_entry(
                    draft_round, current["attempt_count"] + 1, saved=True, reason=None, omitted=[]
                )
                return _record(draft_dir, _recorded(current, entry, draft_round), known, saved=True)
            reason = REASON_FILES_CHANGED

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
            copied_in,
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
    if draft_round.outcome is None:
        # The save a publish follows writes nothing: ``running`` stays, for the
        # round to be recorded with the publish's end once that is known
        # (``record_round``).
        return _not_saved(failed)
    entry = _round_entry(
        draft_round, current["attempt_count"] + 1, saved=False, reason=failed, omitted=[]
    )
    return _record(
        draft_dir, _recorded(current, entry, draft_round), known, saved=False, reason=failed
    )


def _recorded(
    current: dict[str, Any], entry: dict[str, Any], draft_round: DraftRound
) -> dict[str, Any]:
    """``current`` with a continuing round recorded: ``entry`` its latest round, no
    ``running``, the records the round attached joined to ``runs``."""
    return {
        **_with_round(current, entry),
        "runs": _merged_runs(draft_round.attached, current["runs"]),
    }


def _record(
    draft_dir: Path,
    doc: dict[str, Any],
    known: list[str],
    *,
    saved: bool,
    reason: str | None = None,
) -> SaveResult:
    """Write ``doc``, which records a round, and say what became of it.

    ``reason`` is the round's own, as it was recorded: why its files were not
    saved. Not written, the result keeps that reason -- the write's own only when
    there is none -- and carries no draft identity: nothing of the round is in the
    manifest.
    """
    written = _write_manifest(draft_dir, doc, known)
    if not written.replaced:
        return _not_saved(reason or written.reason)
    return SaveResult(
        worth_saving=True,
        saved=saved,
        draft_id=doc["draft_id"],
        generation=doc["generation"],
        attempts=doc["attempt_count"],
        reason=reason,
        recorded=True,
        durable=written.durable,
    )


def _undo(draft_dir: Path, made: Path | None, made_draft_dir: bool) -> None:
    """Take back what a failed first save made: its copy, then an empty ``draft/``."""
    if made is not None:
        remove_entry(made)
    if made_draft_dir:
        with contextlib.suppress(OSError):
            os.rmdir(draft_dir)


def _still_as_copied_out(
    files: Path, previous: PreviousDraft, max_files: int, max_bytes: int
) -> bool:
    """Whether the files directory ``files`` is seen, now, to be what the round
    copied out of it: its digest, taken now with the limits the copy counted
    against, equal to ``copied_out``. Unknown either way -- not taken when the
    round began, or not to be taken now (moved, not read whole) -- is not the
    same.

    Not covered: a change after this look and before the caller acts on it (a
    removal, a manifest naming the directory as the draft's). Only a hand edit
    makes one in that window, as in every look-then-act on the draft; and the
    digest describes a tree that holds still while it is taken (``tree_digest``).
    """
    return same_tree(
        previous.copied_out, tree_digest(files, max_entries=max_files, max_bytes=max_bytes)
    )


def _why_previous_stays(
    old: Path,
    previous: PreviousDraft,
    copied_in: TreeDigest | None,
    durable: bool,
    max_files: int,
    max_bytes: int,
) -> str | None:
    """Why a continuation's previous files directory, once named in ``kept``, is not
    removed; None when each of the three is seen: the manifest naming the new one
    is confirmed on disk; the workspace received all of it as it was when the
    round began (``copied_out`` equal to ``copied_in``: an entry not brought in, a
    link rewritten, a file swapped and swapped back during the copy all make them
    differ); and it is still as it was then (``_still_as_copied_out``). An unknown
    digest is never the same."""
    if not durable:
        return KEPT_NOT_DURABLE
    if not same_tree(previous.copied_out, copied_in):
        return KEPT_NOT_ALL_BROUGHT_IN
    if not _still_as_copied_out(old, previous, max_files, max_bytes):
        return KEPT_CHANGED
    return None


def _copy_and_commit(
    draft_dir: Path,
    workspace: Path,
    plan: _CopyPlan,
    known: list[str],
    draft_round: DraftRound,
    start: DraftStart | None,
    current: dict[str, Any] | None,
    previous: PreviousDraft | None,
    copied_in: TreeDigest | None,
    max_files: int,
    max_bytes: int,
    durability: Durability,
) -> tuple[Path | None, SaveResult | str]:
    """Steps 9 to 11. Returns what it made that a failure must remove, and the
    result -- or the reason it failed.

    A continuation's previous files directory is named in ``kept`` by the same
    write that names the new one (whether that write is on disk is only known
    once it is done), and removed only when ``_why_previous_stays`` finds nothing;
    otherwise the result's ``reason`` says where it is and why it stays. Names in
    ``kept`` seen gone are dropped from it on the way.
    """
    incoming = draft_dir / f".incoming-{token_hex(16)}"
    try:
        os.mkdir(incoming)
    except OSError as exc:
        return None, REASON_COPY.format(kind=type(exc).__name__)
    try:
        omitted, unlooked = _carry_out(plan, incoming, known)
    except _CopyNotKept as refused:
        return incoming, refused.reason
    except OSError as exc:
        return incoming, REASON_COPY.format(kind=type(exc).__name__)
    if unlooked:
        # The copy lacks what a failed look left out, so it may not stand for the
        # round: committed, the previous files directory and the staging could go
        # with that entry still in them.
        return incoming, _unlooked_reason(unlooked, known)
    with contextlib.suppress(OSError):
        os.chmod(incoming, stat.S_IMODE(os.lstat(workspace).st_mode) | 0o700)
    if not durability.tree(incoming):
        return incoming, REASON_DURABILITY
    # The copy as made, before any name points at it: what a removal after the
    # publish compares the draft against. Renaming the directory changes nothing
    # it covers, since every entry is named relative to the copy.
    copied = tree_digest(incoming, max_entries=max_files, max_bytes=max_bytes)
    files_name = f"files-{token_hex(16)}"
    files = draft_dir / files_name
    try:
        os.rename(incoming, files)
    except OSError as exc:
        return incoming, REASON_COPY.format(kind=type(exc).__name__)
    if not durability.directory(draft_dir):
        return files, REASON_DURABILITY

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
            "runs": [_run_record(run) for run in start.runs[:_RUNS_KEPT]],
            "attempt_count": 1,
            "attempts": [_round_entry(draft_round, 1, saved=True, reason=None, omitted=omitted)],
            "running": None,
            "publishing": None,
        }
    else:
        entry = _round_entry(
            draft_round, current["attempt_count"] + 1, saved=True, reason=None, omitted=omitted
        )
        # The previous copy is named before anything decides whether it goes; a
        # name already seen gone is dropped, and so is the previous one if it is.
        named = [*current["kept"], current["files_dir"]]
        doc = {
            **_recorded(current, entry, draft_round),
            "files_dir": files_name,
            "kept": [name for name in named if not _seen_gone(draft_dir / name)],
        }
    written = _write_manifest(draft_dir, doc, known)
    if not written.replaced:
        return files, written.reason
    note: str | None = None
    if current is not None and previous is not None and current["files_dir"] in doc["kept"]:
        old = draft_dir / current["files_dir"]
        why = _why_previous_stays(old, previous, copied_in, written.durable, max_files, max_bytes)
        if why is None:
            # Its name stays in ``kept`` until a later rewrite sees it gone.
            remove_entry(old)
        else:
            note = NOTE_PREVIOUS_KEPT.format(path=old, why=why)
    return None, SaveResult(
        worth_saving=True,
        saved=True,
        draft_id=doc["draft_id"],
        generation=doc["generation"],
        attempts=doc["attempt_count"],
        reason=note,
        copy=SavedCopy(files_name, copied, max_files, max_bytes),
        recorded=True,
        durable=written.durable,
    )


# --- after the save: publishing, removal, discard -------------------------------


def _locked_in_package(
    package_root: tools.PackageRoot,
    package_dir_identity: tuple[int, int] | None,
    act: Callable[[Path], Any],
) -> Any:
    """Run ``act(draft_dir)`` under the shared tools lock in the round's own package.

    None, without running it, when the lock cannot be had, the package is no
    longer the one the round began in, or it has no ``draft/`` of its own that is
    seen (``_seen_own_draft``).
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
        found = _seen_own_draft(package_root)
        if found is None:
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
        written = _write_manifest(draft_dir, doc, tools._redaction_secrets())
        return doc["generation"] if written.replaced else None

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
        written = _write_manifest(draft_dir, doc, tools._redaction_secrets())
        return doc["generation"] if written.replaced else None

    try:
        return _locked_in_package(package_root, package_dir_identity, act)
    except Exception:
        return None


# --- a continuing round: its mark, its copy, its record ----------------------------


@dataclass(frozen=True, slots=True)
class Continuation:
    """What a revise job continues: the draft as judged after the slot, ``expected``
    its ``(draft_id, generation)``, and the run records the request newly attached,
    which join the manifest's ``runs`` when the round is recorded."""

    expected: tuple[str, int]
    attached: tuple[AttachedRun, ...] = ()


def _in_own_package(
    package_root: tools.PackageRoot,
    package_dir_identity: tuple[int, int] | None,
    act: Callable[[Path], Any],
) -> Any:
    """``act(draft_dir)`` under the shared tools lock in the round's own package, or
    why it did not run: ``REASON_LOCK``, ``REASON_PACKAGE_GONE`` (not the directory
    the round began in), ``START_UNSEEN`` (no ``draft/`` of its own that is seen,
    ``_seen_own_draft``). Raises what ``act`` raises."""
    try:
        lock = tools.acquire_shared_tools_lock()
    except tools.ToolsLockUnavailableError:
        lock = None
    if lock is None:
        return REASON_LOCK
    try:
        if not _still_the_package(package_root, package_dir_identity):
            return REASON_PACKAGE_GONE
        found = _seen_own_draft(package_root)
        if found is None:
            return START_UNSEEN
        return act(found[0])
    finally:
        tools.release_tools_lock(lock)


def _running_draft(
    draft_dir: Path, expected: tuple[str, int], job_id: str | None
) -> dict[str, Any] | None:
    """The manifest when it is still the one a round's ``begin_round`` wrote: the same
    draft at the same generation, marked running by that round's job. None
    otherwise, and then nothing of the round is written: the latest manifest is
    not read again to be written to instead."""
    current = _matching_draft(draft_dir, expected)
    if current is None or current["running"] is None or current["running"]["job_id"] != job_id:
        return None
    return current


def begin_round(
    package_root: tools.PackageRoot,
    package_dir_identity: tuple[int, int] | None,
    expected: tuple[str, int],
    job_id: str | None,
    started_at: str,
) -> dict[str, Any] | str:
    """Mark the draft as having this round running: the manifest as written, or why
    not -- and then the round does not begin. Never raises.

    Under the shared tools lock, in the round's own package, on the draft judged
    after the slot (``expected``) and only while it is not being published from.
    The names in ``kept`` seen gone are dropped first; when as many as ``kept``
    may hold are left, the round does not begin (``START_KEPT_FULL``): a round
    that commits a new files directory names one more. A ``running`` left by a
    round that recorded no end is recorded as a round of its own, ``interrupted``,
    with no end time: that round may have run commands, and nothing shows they
    stopped. One generation on, ``running`` names this round's job; every later
    write of the round is matched against the manifest returned
    (``_running_draft``).
    """
    try:
        known = tools._redaction_secrets()
    except tools.KnownSecretsIncomplete as exc:
        return REASON_SECRETS_INCOMPLETE.format(detail=exc)
    except Exception as exc:
        return REASON_SCAN_FAILED.format(reason=type(exc).__name__)

    def act(draft_dir: Path) -> dict[str, Any] | str:
        current = _matching_draft(draft_dir, expected)
        if current is None:
            return START_CHANGED
        if current["publishing"] is not None:
            return START_PUBLISHING
        kept = [name for name in current["kept"] if not _seen_gone(draft_dir / name)]
        if len(kept) >= _KEPT_MAX:
            return START_KEPT_FULL.format(max=_KEPT_MAX)
        doc = {
            **current,
            "generation": current["generation"] + 1,
            "kept": kept,
            "running": {"job_id": job_id, "started_at": started_at},
        }
        if current["running"] is not None:
            entry = _interrupted_entry(current["running"], current["attempt_count"] + 1)
            doc["attempt_count"] = entry["number"]
            doc["attempts"] = [*current["attempts"], entry][-ATTEMPTS_KEPT:]
        written = _write_manifest(draft_dir, doc, known)
        return doc if written.replaced else written.reason

    try:
        return _in_own_package(package_root, package_dir_identity, act)
    except Exception as exc:
        return REASON_UNEXPECTED.format(kind=type(exc).__name__)


def end_unstarted(
    package_root: tools.PackageRoot,
    package_dir_identity: tuple[int, int] | None,
    expected: tuple[str, int],
    job_id: str | None,
) -> bool:
    """Take back the ``running`` mark of a round that never began its conversation.

    No round is counted and ``runs`` is left as it is. Only while the manifest is
    still the one ``begin_round`` returned (``expected`` its identity) and marked
    by this job (``_running_draft``). Returns whether that write was made; when it
    was not, ``running`` stays and the next round records it. Never raises.
    """

    def act(draft_dir: Path) -> bool:
        current = _running_draft(draft_dir, expected, job_id)
        if current is None:
            return False
        doc = {**current, "generation": current["generation"] + 1, "running": None}
        return _write_manifest(draft_dir, doc, tools._redaction_secrets()).replaced

    try:
        return _in_own_package(package_root, package_dir_identity, act) is True
    except Exception:
        return False


def record_round(
    package_root: tools.PackageRoot,
    package_dir_identity: tuple[int, int] | None,
    expected: tuple[str, int],
    draft_round: DraftRound,
    reason: str | None,
) -> SaveResult:
    """Record a continuing round its save did not record: only the round, its files
    not saved, ``reason`` why, ``running`` cleared, and the records it attached
    joined to ``runs``. Never raises.

    For every exit of a round that ran -- its commands confirmed stopped -- where
    nothing recorded it yet: a workspace that could not be confirmed, a strip that
    failed, a save that failed before it wrote or whose write failed, one that
    ended in an exception it caught, and the save a publish followed that did not
    save, once the publish's result is known -- ``draft_round`` then the
    publish's end, ``reason`` the save's. Only while the manifest is still
    the one ``begin_round`` returned (``expected``) and marked by this round's job
    (``_running_draft``); otherwise nothing is written, and ``running`` stays for
    the next round to record. Nor with a known-secret list that is not whole.
    """

    def act(draft_dir: Path) -> SaveResult:
        current = _running_draft(draft_dir, expected, draft_round.job_id)
        if current is None:
            return _not_saved(reason or REASON_DRAFT_CHANGED)
        entry = _round_entry(
            draft_round, current["attempt_count"] + 1, saved=False, reason=reason, omitted=[]
        )
        return _record(
            draft_dir,
            _recorded(current, entry, draft_round),
            tools._redaction_secrets(),
            saved=False,
            reason=reason,
        )

    try:
        done = _in_own_package(package_root, package_dir_identity, act)
    except Exception:
        return _not_saved(reason or REASON_MANIFEST)
    return done if isinstance(done, SaveResult) else _not_saved(reason or done)


def previous_files(package_root: tools.PackageRoot, manifest: dict[str, Any]) -> Path | None:
    """The files directory ``manifest`` names, reached through the package's own
    metadata directory (``_own_meta``); None when that is not a real directory.

    What a continuing round's save puts back where its own version of a path
    holds a secret: the version the round began from.
    """
    meta = _own_meta(package_root)
    if not isinstance(meta, Path):
        return None
    return meta / DRAFT_DIRNAME / manifest["files_dir"]


def _same_entry(first: Path, second: Path) -> bool | None:
    """Whether two names are one entry, by ``(st_dev, st_ino)``; None when either
    look fails other than by seeing nothing there, which leaves it unknown."""
    try:
        one = os.lstat(first)
        other = os.lstat(second)
    except FileNotFoundError, NotADirectoryError:
        return False
    except OSError:
        return None
    return (one.st_dev, one.st_ino) == (other.st_dev, other.st_ino)


def _not_brought_in(files: Path) -> dict[str, OmitReason]:
    """The entries at the top of a draft's files a continuation's workspace does not
    receive, with why: each name in ``STRIPPED_ROOT_NAMES``, under the reason it is
    keyed by, and any name that is the same entry spelled in another case. A
    ``.env``'s values are no known secret either, so nothing would mask them once
    ``read_file`` put them in the conversation. Another case of a listed name when
    the name itself is at the top too is another entry -- a filesystem that tells
    case apart -- and is brought in like any file. When whether it is the same
    entry cannot be told, it is not brought in (``unreadable``). Raises ``OSError``
    when the top cannot be listed.
    """
    with os.scandir(files) as listing:
        names = [entry.name for entry in listing]
    left_out: dict[str, OmitReason] = {}
    for reason, stripped in STRIPPED_ROOT_NAMES.items():
        exact = stripped in names
        for name in names:
            if name == stripped:
                left_out[name] = reason
            elif name.casefold() == stripped.casefold() and not exact:
                same = _same_entry(files / name, files / stripped)
                if same is None:
                    left_out[name] = "unreadable"
                elif same:
                    left_out[name] = reason
    return left_out


@dataclass(frozen=True, slots=True)
class CopiedIn:
    """What a continuation's copy of the draft into its workspace did.

    ``copied_out`` is ``tree_digest`` of the files directory taken just before
    the copy; ``not_brought_in`` every path of it the workspace did not receive,
    and why, in full.
    """

    copied_out: TreeDigest | None
    not_brought_in: tuple[Omitted, ...]


def copy_into_workspace(
    *,
    package_root: tools.PackageRoot,
    package_dir_identity: tuple[int, int] | None,
    expected: tuple[str, int],
    job_id: str | None,
    workspace: Path,
    max_files: int,
    max_bytes: int,
) -> CopiedIn | str:
    """Copy the draft's files into a continuing round's new workspace; or why not,
    and then the round does not begin. Never raises.

    The pipeline a save copies with, the draft's files directory as the source
    and ``workspace`` -- which must not exist yet -- as where the copy is made:
    its links judged on the source and stored relative, every entry and byte
    counted against the limits as it is made, the links judged again on the copy
    and resolved there, the whole copy scanned for the known secrets
    (``_plan_copy``, ``_carry_out``). What holds a secret is not brought in, and
    no earlier version takes its place. Nor are the entries ``_not_brought_in``
    names. Modes are kept as the copy keeps them -- each file at least owner
    read and write, each directory owner rwx, as a save already made them -- so
    for a draft the backend saved, the workspace's digest equals ``copied_out``.

    Under the shared tools lock, in the round's own package, on the manifest
    ``begin_round`` returned (``expected``, marked by ``job_id``):
    ``copied_out`` is taken before anything is copied, then the size limits are
    checked on the files directory, which only refuses early -- the counting as
    the copy is made is what holds the limits. A copy that cannot be made whole
    leaves what it made in the workspace, which the job removes with the rest of
    its session.
    """
    try:
        known = tools._redaction_secrets()
    except tools.KnownSecretsIncomplete as exc:
        return REASON_SECRETS_INCOMPLETE.format(detail=exc)
    except Exception as exc:
        return REASON_SCAN_FAILED.format(reason=type(exc).__name__)

    def act(draft_dir: Path) -> CopiedIn | str:
        current = _running_draft(draft_dir, expected, job_id)
        if current is None:
            return START_CHANGED
        files = draft_dir / current["files_dir"]
        if not _is_real_dir(files):
            return REASON_FILES_MISSING
        copied_out = tree_digest(files, max_entries=max_files, max_bytes=max_bytes)
        too_big = _over_limits(files, max_files, max_bytes)
        if too_big is not None:
            return REASON_DRAFT_UNREADABLE if too_big == REASON_UNREADABLE else too_big
        scan = tools.scan_embedded_secrets(files, known)
        if isinstance(scan, tools.SecretScanFailed):
            return REASON_SCAN_FAILED.format(reason=scan.reason)
        try:
            plan = _plan_copy(
                files, scan, None, max_files, max_bytes, left_out=_not_brought_in(files)
            )
        except OSError:
            return REASON_DRAFT_UNREADABLE
        try:
            workspace.mkdir(parents=True)
            # What a failed look left out -- here or in the plan -- is not brought
            # in, recorded like the rest; the draft still holds it.
            omitted, _unlooked = _carry_out(plan, workspace, known)
            os.chmod(workspace, stat.S_IMODE(os.lstat(files).st_mode) | 0o700)
        except _CopyNotKept as refused:
            return refused.reason
        except OSError as exc:
            return REASON_COPY_IN.format(kind=type(exc).__name__)
        return CopiedIn(copied_out, tuple(omitted))

    try:
        return _in_own_package(package_root, package_dir_identity, act)
    except Exception as exc:
        return REASON_UNEXPECTED.format(kind=type(exc).__name__)


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

    ``removed`` False with ``draft/`` itself as ``residual_path``: ``draft/`` is
    not seen gone, so the draft may still be there -- the removal ran and
    ``draft/`` is still there or cannot be looked at, or a discard could not look
    at it whole, or at the directories above it, and left it as it was. False
    with no ``residual_path``: nothing was taken away.

    ``kept`` is set only by ``remove_after_publish`` when it did not begin to
    remove, and says why the draft was left: ``"changed"``, it holds something
    other than what the save before the publish made; ``"unconfirmed"``, that
    could not be told.
    """

    removed: bool
    residual_path: Path | None
    kept: Literal["changed", "unconfirmed"] | None = None


def _removal(draft_dir: Path) -> Removal:
    """Take ``draft/`` away and say what became of it, from what is seen afterwards."""
    left = _take_away(draft_dir)
    if not _seen_gone(draft_dir):
        return Removal(False, draft_dir)
    return Removal(True, left)


def _as_saved(
    draft_dir: Path, current: dict[str, Any], saved: SavedCopy | None
) -> Literal["same", "changed", "unconfirmed"]:
    """Whether ``draft/`` holds only what the save described by ``saved`` made.

    What a removal takes is all of ``draft/``, so all of it is compared: the
    manifest's ``files_dir`` must still be the copy that save made, ``draft/``
    must hold that directory and the manifest and nothing else, and ``tree_digest``
    of the copy now must equal the one taken as it was made. A listing or a
    digest that cannot be had whole, or a save that left none, is
    ``"unconfirmed"`` -- unknown is never the same, nor shown to differ.
    """
    if saved is None or saved.digest is None:
        return "unconfirmed"
    if current["files_dir"] != saved.files_dir:
        return "changed"
    try:
        names = set(os.listdir(draft_dir))
    except OSError:
        return "unconfirmed"
    if names != {MANIFEST_FILENAME, saved.files_dir}:
        return "changed"
    now = tree_digest(
        draft_dir / saved.files_dir, max_entries=saved.max_entries, max_bytes=saved.max_bytes
    )
    if now is None:
        return "unconfirmed"
    return "same" if same_tree(saved.digest, now) else "changed"


def remove_after_publish(
    package_root: tools.PackageRoot,
    package_dir_identity: tuple[int, int] | None,
    expected: tuple[str, int],
    saved: SavedCopy | None,
) -> Removal:
    """Remove the draft a publish just put live, if it holds nothing more. Never raises.

    Only the draft the publish came from, at the generation it marked, and only
    while it holds just what the save before the publish made: ``saved``, the
    copy that save made with its fingerprint (``SavedCopy``), not the workspace
    the publish went from, which may hold entries the copy does not keep. Under
    the lock, after the manifest is matched, ``_as_saved`` compares all of
    ``draft/``, since the removal takes all of it. Anything else is left alone,
    with ``kept`` saying why: ``"changed"`` -- the copy differs, the manifest names
    another one, or ``draft/`` holds more, a directory of the operator's say;
    ``"unconfirmed"`` -- it could not be told, or the draft could not even be
    reached and matched, so the comparison never ran. Neither says the removal
    failed: it was not attempted. Not removed, the draft stays with
    ``publishing`` set, and the list reconciles it as published.

    Not covered: a change made after the comparison, before the rename that
    takes ``draft/`` away; and one made during the comparison to an entry it has
    already read -- ``tree_digest`` walks by path, so it describes a tree that
    holds still while it runs. This is no atomic snapshot of the draft. Nor a
    change made to the copy before its fingerprint was taken, while the save was
    still making it durable: that one is part of what is compared against.
    """
    started = [False]

    def act(draft_dir: Path) -> Removal:
        current = _matching_draft(draft_dir, expected)
        if current is None:
            return Removal(False, None, kept="unconfirmed")
        verdict = _as_saved(draft_dir, current, saved)
        if verdict != "same":
            return Removal(False, None, kept=verdict)
        started[0] = True
        return _removal(draft_dir)

    try:
        done = _locked_in_package(package_root, package_dir_identity, act)
    except Exception:
        # Before the removal began, nothing of the draft was taken; after, it may
        # have been, and ``draft/`` is not seen gone.
        if started[0]:
            return Removal(False, draft_path(package_root))
        return Removal(False, None, kept="unconfirmed")
    return done if isinstance(done, Removal) else Removal(False, None, kept="unconfirmed")


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
    own draft, and only once it is seen (``_seen_own_draft``). Through a link --
    the package directory or its metadata directory being one -- or where
    ``draft/`` is seen not to exist, there is nothing to discard (``not_found``).
    A draft that cannot be looked at, or whose package or metadata directory
    cannot, is left as it is and answered as not removed, with ``draft/`` as what
    is left: it may still be there. So is a draft named by ``token`` when the look
    this takes under the lock is not whole (``_observed_token``), and any draft
    when the lock is not taken while the tools directory is not seen gone:
    without the lock, ``not_found`` only when no tools directory is configured
    or it is seen not to exist, its path running through a file included; a
    path that cannot be resolved (``tools.ToolsDirUnresolvedError``) leaves the
    draft as one that may be there. ``Removal.removed`` only once ``draft/`` is
    seen gone (``_seen_gone``); one that cannot be looked at afterwards is not
    removed either.

    Raises ``tools.ToolsLockUnavailableError`` when the lock path is unusable.
    """
    try:
        lock = tools.acquire_shared_tools_lock()
        if lock is None:
            # No lock: no tools directory is configured, or it is not seen to be
            # a directory, a look at it that fails included. Verified: "no draft"
            # only when none is configured or it is seen gone. Not covered: a
            # tools path that is there but no directory holds no draft, yet is
            # answered as one that may.
            base = tools.tools_dir()
            if base is None or _seen_gone(base):
                return "not_found"
            return Removal(False, draft_path(package_root))
    except tools.ToolsDirUnresolvedError:
        # Its path could not be resolved: not seen gone.
        return Removal(False, draft_path(package_root))
    try:
        found = _seen_own_draft(package_root)
        if found is None:
            # Nothing to act on. Asked as a read asks it: may a draft be there?
            if has_draft(package_root):
                return Removal(False, draft_path(package_root))
            return "not_found"
        draft_dir, _info = found
        if token is not None:
            now, whole = _observed_token(draft_dir)
            if not whole:
                return Removal(False, draft_dir)
            same = now == token
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
    module makes under ``<package>/.afterthread.meta/``: what an authorized
    removal left (``draft.discarded-<hex>``: a discard, a removal after a
    publish), and inside ``draft/`` the ``.incoming-<hex>`` a crash left half
    made, a name never shown to anyone. The second kind only when the manifest
    can be trusted: it reads back in shape, its files directory is there, and
    both it and ``draft/`` are fsynced first. Any doubt leaves all of ``draft/``
    alone. A ``files-<hex>`` the manifest neither names nor keeps is never taken:
    not being named is no proof it may go -- a crash before the manifest naming
    it was on disk brings the older one back, and the operator may have edited
    the copy the list had shown -- so it stays until the draft is discarded.
    Nothing is followed through a link: the metadata directory is reached
    through ``_own_meta``.
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
        with os.scandir(draft_dir) as listing:
            names = sorted(entry.name for entry in listing)
        for name in names:
            if _INCOMING_RE.fullmatch(name) is not None and _is_real_dir(draft_dir / name):
                shutil.rmtree(draft_dir / name, ignore_errors=True)
