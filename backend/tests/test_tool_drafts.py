"""The unfinished-revision store (``services/tool_drafts.py``) on its own.

The revise job's use of it -- which rounds save, the publish that follows a save,
the routes -- is in ``tests/test_tool_builder.py``, next to the rest of the revise
tests. Here: the content digest, the link rule, the manifest, the discard token,
the state a listing shows, the sweep, and the save itself driven directly, a save
into a draft that already exists included.
"""

import dataclasses
import errno
import json
import os
import re
import shutil
import stat
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args

import pytest

from afterthread.config import Settings
from afterthread.schemas import ToolDraft, ToolDraftLast
from afterthread.services import tool_builder, tool_drafts, tools

_VID = "20260728T010203Z-abcdef"
_OTHER_VID = "20260729T010203Z-123456"
# The job a continuing round runs in, as ``begin_round`` marks it.
_JOB = "a" * 32
# Registered through the package's own .env, as a revise's values are.
_SECRET = "kb-live-secret-0123456789"


@pytest.fixture(autouse=True)
def _clean_registries() -> Generator[None]:
    """The known-secret registries are module state; start and end each test empty."""
    tools._INFLIGHT_SECRETS.clear()
    tools._ENV_VALUE_CACHE.clear()
    yield
    tools._INFLIGHT_SECRETS.clear()
    tools._ENV_VALUE_CACHE.clear()


def _settings(monkeypatch: pytest.MonkeyPatch, root: Path, **overrides: Any) -> Settings:
    settings = Settings(tools_dir=str(root), **overrides)
    for target in (
        "afterthread.services.tools.get_settings",
        "afterthread.services.tool_builder.get_settings",
        "afterthread.services.tool_meta.get_settings",
    ):
        monkeypatch.setattr(target, lambda settings=settings: settings)
    return settings


def _make_package(root: Path, name: str = "kb", vid: str = _VID) -> Path:
    """One installed package whose current version is ``vid``; returns the package dir."""
    package = root / name
    version = package / tools._VERSIONS_DIRNAME / vid
    version.mkdir(parents=True)
    (version / "tool.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": "test tool",
                "parameters": {"type": "object", "properties": {}},
                "entry": ["python3", "run.py"],
            }
        ),
        encoding="utf-8",
    )
    (version / "run.py").write_text("print('base')\n", encoding="utf-8")
    version_meta = version / tools._META_DIRNAME
    version_meta.mkdir()
    (version_meta / tools._ORIGIN_FILENAME).write_text(
        json.dumps(
            {
                "source": "test-fixture",
                "openapi_url": None,
                "instructions": None,
                "feedback": None,
                "previous": None,
            }
        ),
        encoding="utf-8",
    )
    meta = package / tools._META_DIRNAME
    meta.mkdir()
    (meta / tools._CURRENT_FILENAME).write_text(f"{vid}\n", encoding="ascii")
    (meta / tools._PACKAGE_STATE_FILENAME).write_text(
        json.dumps({tools._STATE_MARKER_KEY: tools._STATE_MARKER_VALUE, "enabled": True}),
        encoding="utf-8",
    )
    return package


def _package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides: Any
) -> tools.PackageRoot:
    """A package under a tools directory the settings point at, its .env holding ``_SECRET``."""
    root = (tmp_path / "tools").resolve()
    root.mkdir()
    package = _make_package(root)
    (package / ".env").write_text(f"KB_KEY={_SECRET}\n", encoding="utf-8")
    _settings(monkeypatch, root, **overrides)
    return tools.PackageRoot(package)


def _version(package_root: tools.PackageRoot, vid: str = _VID) -> Path:
    return package_root.path / tools._VERSIONS_DIRNAME / vid


def _workspace(tmp_path: Path, package_root: tools.PackageRoot) -> Path:
    """A workspace copied from the current version, as a first round receives one."""
    workspace = tmp_path / "work" / "build"
    shutil.copytree(
        _version(package_root),
        workspace,
        symlinks=True,
        ignore=lambda directory, names: (
            {tools._META_DIRNAME} if Path(directory) == _version(package_root) else set()
        ),
    )
    return workspace


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _durability() -> tool_drafts.Durability:
    return tool_drafts.Durability(
        tree=tool_builder._fsync_tree, directory=tool_builder._fsync_directory
    )


def _save(
    package_root: tools.PackageRoot,
    workspace: Path,
    copied_in: tool_drafts.TreeDigest | None,
    *,
    previous: tool_drafts.PreviousDraft | None = None,
    earlier_root: Path | None = None,
    draft_round: tool_drafts.DraftRound | None = None,
    max_files: int = 2000,
    max_bytes: int = 50_000_000,
    identity: tuple[int, int] | None | Literal["now"] = "now",
) -> tool_drafts.SaveResult:
    """One save as the revise job makes it; by default in the package as it is now.

    The version a path falls back to is the base for a first round and the draft's
    files for a continuation, as the job hands them in.
    """
    if earlier_root is None:
        earlier_root = (
            _version(package_root)
            if previous is None
            else _draft_dir(package_root) / previous.manifest["files_dir"]
        )
    return tool_drafts.save_round(
        package_root=package_root,
        package_dir_identity=(
            tool_drafts.directory_identity(package_root.path)
            if isinstance(identity, str)
            else identity
        ),
        workspace=workspace,
        earlier_root=earlier_root,
        copied_in=copied_in,
        draft_round=draft_round
        or tool_drafts.DraftRound(
            finished_at=_now(),
            outcome="not_published",
            summary="改到一半",
            job_id=None if previous is None else _JOB,
        ),
        start=None
        if previous is not None
        else tool_drafts.DraftStart(base_vid=_VID, feedback="加上分頁"),
        previous=previous,
        max_files=max_files,
        max_bytes=max_bytes,
        durability=_durability(),
    )


def _draft_dir(package_root: tools.PackageRoot) -> Path:
    return tool_drafts.draft_path(package_root)


def _manifest(package_root: tools.PackageRoot) -> dict[str, Any]:
    manifest = tool_drafts.read_manifest(_draft_dir(package_root))
    assert manifest is not None
    return manifest


def _files(package_root: tools.PackageRoot) -> Path:
    return _draft_dir(package_root) / _manifest(package_root)["files_dir"]


def _first_save(
    tmp_path: Path, package_root: tools.PackageRoot
) -> tuple[Path, tool_drafts.SaveResult]:
    """Save one first round that changed ``run.py``; returns its workspace and the save."""
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "run.py").write_text("print('first round')\n", encoding="utf-8")
    saved = _save(package_root, workspace, copied_in)
    assert saved.saved
    return workspace, saved


def _first_draft(tmp_path: Path, package_root: tools.PackageRoot) -> Path:
    """Save one first round that changed ``run.py``; returns its workspace."""
    return _first_save(tmp_path, package_root)[0]


def _begin(package_root: tools.PackageRoot, *, job_id: str | None = _JOB) -> dict[str, Any] | str:
    """``begin_round`` on the draft as it is now, in the package as it is now."""
    return tool_drafts.begin_round(
        package_root,
        tool_drafts.directory_identity(package_root.path),
        _identity(package_root),
        job_id,
        _now(),
    )


def _copy_in(
    package_root: tools.PackageRoot,
    begun: dict[str, Any],
    workspace: Path,
    *,
    job_id: str | None = _JOB,
    max_files: int = 2000,
    max_bytes: int = 50_000_000,
) -> tool_drafts.CopiedIn | str:
    """``copy_into_workspace`` for the round ``begun`` marked."""
    return tool_drafts.copy_into_workspace(
        package_root=package_root,
        package_dir_identity=tool_drafts.directory_identity(package_root.path),
        expected=(begun["draft_id"], begun["generation"]),
        job_id=job_id,
        workspace=workspace,
        max_files=max_files,
        max_bytes=max_bytes,
    )


def _continuing(
    tmp_path: Path, package_root: tools.PackageRoot
) -> tuple[Path, tool_drafts.PreviousDraft, tool_drafts.TreeDigest | None]:
    """A continuing round's start, as the job makes it: the draft marked running,
    its files copied into a new workspace, the workspace's digest taken."""
    begun = _begin(package_root)
    assert isinstance(begun, dict), begun
    workspace = tmp_path / "work2" / "build"
    copied = _copy_in(package_root, begun, workspace)
    assert isinstance(copied, tool_drafts.CopiedIn), copied
    previous = tool_drafts.PreviousDraft(manifest=begun, copied_out=copied.copied_out)
    return workspace, previous, tool_drafts.tree_digest(workspace)


def _all_bytes_under(root: Path) -> list[str]:
    """Every name, link target and file content under ``root``, decoded leniently."""
    seen: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            seen.append(name)
            if path.is_symlink():
                seen.append(os.readlink(path))
            elif path.is_file():
                seen.append(path.read_bytes().decode("utf-8", errors="replace"))
    return seen


def _refuse(
    patch: pytest.MonkeyPatch,
    name: str,
    hit: Callable[[str], bool],
    code: int,
    *,
    times: int | None = None,
) -> list[str]:
    """``os.<name>`` fails with ``code`` for each path ``hit`` accepts -- the first
    ``times`` of them, or every one -- and works as before for any other path.
    Returns the paths it failed, in order."""
    real = getattr(os, name)
    failed: list[str] = []

    def fake(path: Any = ".", *args: Any, **kwargs: Any) -> Any:
        spelled = os.fspath(path) if isinstance(path, str | os.PathLike) else None
        if spelled is not None and hit(spelled) and (times is None or len(failed) < times):
            failed.append(spelled)
            raise OSError(code, os.strerror(code), spelled)
        return real(path, *args, **kwargs)

    patch.setattr(os, name, fake)
    return failed


def _in_the_copy(name: str) -> Callable[[str], bool]:
    """A test of whether a path is ``name`` at the top of a copy being made
    (``.incoming-<hex>``)."""
    pattern = re.compile(rf"/\.incoming-[0-9a-f]{{32}}/{re.escape(name)}\Z")
    return lambda path: pattern.search(path) is not None


# --- the content digest -------------------------------------------------------


def test_tree_digest_sees_content_modes_and_links_but_not_times(tmp_path: Path) -> None:
    """Two copies compare equal; a changed byte, an added execute bit, an added file
    and a changed link target each make them differ, and a touch does not."""
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("b\n", encoding="utf-8")
    os.symlink("sub/b.txt", root / "link")
    first = tool_drafts.tree_digest(root)
    copy = tmp_path / "copy"
    shutil.copytree(root, copy, symlinks=True)
    assert tool_drafts.same_tree(first, tool_drafts.tree_digest(copy))

    os.utime(root / "a.py", (0, 0))
    assert tool_drafts.same_tree(first, tool_drafts.tree_digest(root))

    (root / "a.py").write_text("x = 2\n", encoding="utf-8")
    changed = tool_drafts.tree_digest(root)
    assert not tool_drafts.same_tree(first, changed)
    assert changed is not None and first is not None
    assert tool_drafts.changed_paths(first, changed) == ("a.py",)

    os.chmod(copy / "a.py", 0o755)
    assert not tool_drafts.same_tree(first, tool_drafts.tree_digest(copy))
    os.chmod(copy / "a.py", stat.S_IMODE((root / "sub" / "b.txt").stat().st_mode))

    os.unlink(copy / "link")
    os.symlink("a.py", copy / "link")
    assert not tool_drafts.same_tree(first, tool_drafts.tree_digest(copy))


def test_tree_digest_knows_nothing_of_a_tree_it_cannot_read_whole(tmp_path: Path) -> None:
    """Over a budget, missing, or not a directory: None, and None is never the same
    as anything, itself included -- unknown counts as changed."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "big.bin").write_bytes(b"x" * 1000)
    assert tool_drafts.tree_digest(root, max_bytes=999) is None
    assert tool_drafts.tree_digest(root, max_entries=0) is None
    assert tool_drafts.tree_digest(tmp_path / "missing") is None
    assert tool_drafts.tree_digest(root / "big.bin") is None
    assert not tool_drafts.same_tree(None, None)
    assert tool_drafts.tree_digest(root, max_bytes=1000) is not None


# --- links --------------------------------------------------------------------


def test_a_link_through_a_link_and_up_is_not_kept_although_the_string_stays_inside(
    tmp_path: Path,
) -> None:
    """``x -> .`` and ``l -> x/../files-old/f``: as a string, ``l`` points inside the
    workspace; the operating system follows ``x`` first and goes up from where it
    points, out of the copy and into the directory beside it."""
    draft = tmp_path / "draft"
    workspace = draft / "files-new"
    (draft / "files-old").mkdir(parents=True)
    (draft / "files-old" / "f").write_text("OLD\n", encoding="utf-8")
    workspace.mkdir()
    os.symlink(".", workspace / "x")
    os.symlink("x/../files-old/f", workspace / "l")
    assert (workspace / "l").read_text(encoding="utf-8") == "OLD\n"

    assert tool_drafts.stored_link_target(workspace, ("l",), "x/../files-old/f") == (
        None,
        "link_ambiguous",
    )
    assert tool_drafts.stored_link_target(workspace, ("x",), ".") == (".", None)


def test_links_inside_stay_relative_and_those_outside_are_not_kept(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "sub" / "real.txt").write_text("r\n", encoding="utf-8")
    absolute_inside = os.fspath(workspace / "sub" / "real.txt")

    assert tool_drafts.stored_link_target(workspace, ("sub", "l"), "real.txt") == ("real.txt", None)
    assert tool_drafts.stored_link_target(workspace, ("sub", "l"), "../sub/real.txt") == (
        "../sub/real.txt",
        None,
    )
    assert tool_drafts.stored_link_target(workspace, ("sub", "l"), absolute_inside) == (
        "../sub/real.txt",
        None,
    )
    assert tool_drafts.stored_link_target(workspace, ("l",), "../outside") == (None, "link_outside")
    assert tool_drafts.stored_link_target(workspace, ("l",), "/etc/passwd") == (
        None,
        "link_outside",
    )


@pytest.mark.parametrize("code", [errno.EIO, errno.EACCES], ids=["EIO", "EACCES"])
def test_a_dot_dot_after_a_component_that_cannot_be_looked_at_keeps_no_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int
) -> None:
    """Whether a ``..`` follows a link turns on what the component before it is.
    ``x`` cannot be looked at, so that is not known: the judgment raises
    ``LookFailed`` -- not judged as if ``x`` were a directory. Here ``x`` is a link
    into ``d/sub``, so ``x/../f`` reaches ``d/f``, not the top's ``f``."""
    root = tmp_path / "work"
    (root / "d" / "sub").mkdir(parents=True)
    os.symlink("d/sub", root / "x")
    refused = _refuse(monkeypatch, "lstat", lambda path: path == os.fspath(root / "x"), code)

    with pytest.raises(tool_drafts.LookFailed):
        tool_drafts.stored_link_target(root, ("l",), "x/../f")
    assert refused == [os.fspath(root / "x")]


@pytest.mark.parametrize(
    "target", ["missing/../f", "plain.txt/x/../../f"], ids=["missing", "under-a-file"]
)
def test_a_dot_dot_after_a_component_seen_not_to_be_there_is_walked_as_the_string_reads(
    tmp_path: Path, target: str
) -> None:
    """A component seen not to exist, or seen under a regular file, is no link:
    its ``lstat`` fails with ``ENOENT`` or ``ENOTDIR``, and the operating system
    fails there too. The ``..`` after it is walked as the string reads, and the
    link is kept as it was written."""
    root = tmp_path / "work"
    root.mkdir()
    (root / "plain.txt").write_text("p\n", encoding="utf-8")

    assert tool_drafts.stored_link_target(root, ("l",), target) == (target, None)


def test_an_absolute_target_matching_only_a_resolved_path_that_cannot_be_confirmed_is_not_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tree's resolved path is asked for with its errors raised. When that
    fails, the path a resolution that swallows its errors hands back may not be
    where the tree is, so only the path as given is matched: a target beginning
    with the other path raises ``LookFailed``, and one beginning with the given
    path is kept."""
    work = tmp_path / "work"
    work.mkdir()
    given = tmp_path / "given"
    os.symlink(work, given)
    elsewhere = tmp_path / "elsewhere"
    real_realpath = os.path.realpath

    def realpath(path: Any, *, strict: Any = False) -> str:
        if os.fspath(path) == os.fspath(given):
            if strict:
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), os.fspath(path))
            return os.fspath(elsewhere)
        return real_realpath(path, strict=strict)

    monkeypatch.setattr(os.path, "realpath", realpath)

    with pytest.raises(tool_drafts.LookFailed):
        tool_drafts.stored_link_target(given, ("l",), f"{elsewhere}/data.txt")
    assert tool_drafts.stored_link_target(given, ("l",), f"{given}/data.txt") == (
        "data.txt",
        None,
    )


# --- the manifest -------------------------------------------------------------


def _document(*, attempt_fields: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    """A manifest in shape, one round long, with ``overrides`` on top."""
    attempt = {
        "number": 1,
        "job_id": None,
        "started_at": None,
        "finished_at": _now(),
        "outcome": "not_published",
        "stopped_by": None,
        "failure": None,
        "rounds_used": 3,
        "max_rounds": 24,
        "error": None,
        "summary": "改到一半",
        "handoff": None,
        "operator_action": None,
        "note": None,
        "saved": True,
        "reason": None,
        "omitted": [],
        "omitted_count": 0,
    }
    document = {
        "draft_id": "a" * 32,
        "generation": 1,
        "files_dir": "files-" + "b" * 32,
        "kept": [],
        "base_vid": _VID,
        "feedback": None,
        "runs": [],
        "attempt_count": 1,
        "attempts": [attempt],
        "running": None,
        "publishing": None,
    }
    attempt.update(attempt_fields or {})
    document.update(overrides)
    return document


def test_the_manifest_masks_its_text_and_keeps_identifiers_as_they_are() -> None:
    """Free text is masked, cut and made encodable; ids and the base vid are not
    masked even when a known value is part of them -- a mask would break them.
    An occurrence wholly inside an id is fixed content, so the texts that hold no
    known value are written as they are, not as the marker."""
    secret = "aaaaaaaa"  # is a substring of the draft id
    data = tool_drafts.manifest_bytes(
        _document(
            feedback=f"key {_SECRET} here",
            attempt_fields={
                "summary": f"used {_SECRET}",
                "handoff": "沒有秘密的交接",
                "omitted": [{"path": f"{_SECRET}.txt", "reason": "secret_name"}],
                "omitted_count": 1,
            },
        ),
        [_SECRET, secret],
    )
    assert data is not None
    text = data.decode("utf-8")
    assert _SECRET not in text
    assert tools._REDACTION_MARKER in text
    assert json.loads(text)["draft_id"] == "a" * 32
    assert json.loads(text)["attempts"][0]["handoff"] == "沒有秘密的交接"


@pytest.mark.parametrize(
    ("secret", "feedback"),
    [
        ('feedback": "DO-NOT-STORE-12345', "DO-NOT-STORE-12345 more"),
        ('abc",\n  "runs": [', "abc"),
    ],
    ids=["from a key into the text", "from the text into the next key"],
)
def test_a_known_value_across_a_key_and_a_text_reaches_no_manifest_byte(
    secret: str, feedback: str
) -> None:
    """The value runs between a key with its punctuation and the feedback, one way
    or the other: neither the text nor the key holds it, the indented file as
    written does. The feedback is written as the marker, and the manifest is
    written without the value."""
    data = tool_drafts.manifest_bytes(_document(feedback=feedback), [secret])

    assert data is not None
    assert secret.encode("utf-8") not in data
    assert json.loads(data)["feedback"] == tools._REDACTION_MARKER


def test_a_known_value_across_two_rounds_reaches_no_manifest_byte() -> None:
    """The value runs from the end of one round's reason, through every fixed
    field between, into the start of the next round's error: two records of the
    attempts list. The whole file as written holds it, and both texts are
    written as the marker. It begins with fewer characters of the reason than
    the mask's trailing guard covers, so no check of one field can see it."""
    base = _document()["attempts"][0]
    first = {**base, "number": 1, "reason": "first-round-reason-tail"}
    second = {**base, "number": 2, "error": "second-round-error-head"}
    document = _document(attempt_count=2, attempts=[first, second])
    plain = tool_drafts.manifest_bytes(document, [])
    assert plain is not None
    text = plain.decode("utf-8")
    start = text.index("first-round-reason-tail") + len("first-round-reason-tail") - 3
    secret = text[start : text.index("second-round") + len("second-round")]

    data = tool_drafts.manifest_bytes(document, [secret])

    assert data is not None
    assert secret.encode("utf-8") not in data
    attempts = json.loads(data)["attempts"]
    assert attempts[0]["reason"] == attempts[1]["error"] == tools._REDACTION_MARKER


def test_a_known_value_ending_on_a_texts_first_character_is_caught_after_an_exempt_one() -> None:
    """``y": "r`` lies wholly in fixed content in ``"stopped_by": "rounds"``. It
    occurs once more, from the end of the ``summary`` key into the first character
    of the summary ``rounds ran out``, and nowhere after that. No check of the
    summary alone sees it, since the value only ends in it. The search past the
    exempt occurrence still finds that one: the summary is written as the marker,
    and the manifest holds the value only where fixed content does."""
    secret = 'y": "r'
    document = _document(attempt_fields={"stopped_by": "rounds", "summary": "rounds ran out"})
    plain = tool_drafts.manifest_bytes(document, [])
    assert plain is not None
    assert plain.decode("utf-8").count(secret) == 2

    data = tool_drafts.manifest_bytes(document, [secret])

    assert data is not None
    text = data.decode("utf-8")
    assert '"stopped_by": "rounds"' in text
    assert text.count(secret) == 1
    assert json.loads(text)["attempts"][0]["summary"] == tools._REDACTION_MARKER


def test_a_manifest_in_which_no_known_value_takes_in_a_text_is_json_dumps_byte_for_byte() -> None:
    """One known value is nowhere in it, and six spaces lie only in the
    indentation: the manifest is ``json.dumps`` indented by two and a new line,
    byte for byte, every text as written."""
    document = _text_in_the_listed_fields('說明 "引號"\n第二行\t\\')
    known = ["kb-live-key-abcdef", " " * 6]

    data = tool_drafts.manifest_bytes(document, known)

    assert data is not None
    written = tool_drafts._document(document, tools._Writing(known))
    expected = json.dumps(written, ensure_ascii=False, indent=2) + "\n"
    assert data == expected.encode("utf-8")


def test_a_path_the_api_shows_is_checked_again_after_its_surrogates_are_replaced() -> None:
    """``kb-\\udcff-key`` holds no known value as it is read. Making it encodable
    writes three U+FFFD for the lone surrogate, which complete ``kb-���-key``.
    What is shown is checked after that: the path is the marker, alone and in a
    reason that names it."""
    spelled = "kb-���-key"
    known = tools._match_forms([spelled])

    assert tool_drafts._shown("kb-\udcff-key", known) == tools._REDACTION_MARKER
    reason = tool_drafts._unlooked_reason(["kb-\udcff-key"], known)
    assert spelled not in reason
    assert reason == tool_drafts.REASON_UNLOOKED.format(paths=tools._REDACTION_MARKER)


# (secret, text): the text does not hold the secret, but the JSON the text is
# written as does -- an escape the encoder writes, or, for a lone surrogate, the
# three replacement characters the scrub leaves.
_SPELLED_AS_WRITTEN = {
    "newline": ("kb-pem-head\\nkb-pem-body", "key kb-pem-head\nkb-pem-body here"),
    "quote": ('kb-quote-left\\"kb-quote-right', 'say kb-quote-left"kb-quote-right ok'),
    "backslash": ("kb-back-left\\\\kb-back-right", "at kb-back-left\\kb-back-right ok"),
    "tab": ("kb-tab-left\\tkb-tab-right", "key kb-tab-left\tkb-tab-right ok"),
    "control": ("kb-ctl-left\\u0001kb-ctl-right", "key kb-ctl-left\x01kb-ctl-right ok"),
    "tail": ("abcde\\nxyz123", "it ends with abcde\n"),
    "replacement": ("kb-���-key", "name kb-\udcff-key here"),
}


_PLAIN_NOTE = "a plain note"


def _text_in_the_listed_fields(text: str) -> dict[str, Any]:
    """A manifest in shape with ``text`` in each free-text field listed here, and
    ``_PLAIN_NOTE`` in ``note``."""
    return _document(
        feedback=text,
        runs=[
            {
                "id": 1,
                "started_at": text,
                "workflow": text,
                "vid": text,
                "outcome": text,
                "exit_code": None,
            }
        ],
        attempt_fields={
            "error": text,
            "summary": text,
            "handoff": text,
            "operator_action": text,
            "note": _PLAIN_NOTE,
            "reason": text,
            "omitted": [{"path": text, "reason": "secret_name"}],
            "omitted_count": 1,
        },
    )


def _holds(secret: str, data: bytes) -> bool:
    """Whether ``data`` holds ``secret``, or ends a JSON string with a prefix of it
    as long as the mask's trailing guard covers."""
    encoded = secret.encode("utf-8", "surrogatepass")
    return encoded in data or any(
        encoded[:end] + b'"' in data for end in range(tools._MIN_SECRET_LEN, len(encoded))
    )


def _reads_back(tmp_path: Path, data: bytes) -> dict[str, Any] | None:
    draft = tmp_path / "draft"
    draft.mkdir()
    (draft / tool_drafts.MANIFEST_FILENAME).write_bytes(data)
    return tool_drafts.read_manifest(draft)


@pytest.mark.parametrize("case", list(_SPELLED_AS_WRITTEN))
def test_text_spelling_a_secret_only_as_written_reaches_no_manifest_byte_from_the_listed_fields(
    tmp_path: Path, case: str
) -> None:
    """Each field is checked as it is written: the ones that spell the secret are
    the marker, and the note that does not is kept as it is -- the whole-file
    check, which would mask the note too, never had to step in."""
    secret, text = _SPELLED_AS_WRITTEN[case]
    assert secret not in text

    data = tool_drafts.manifest_bytes(_text_in_the_listed_fields(text), [secret])

    assert data is not None
    assert not _holds(secret, data)
    read = _reads_back(tmp_path, data)
    assert read is not None
    last = read["attempts"][-1]
    assert (last["summary"], last["note"]) == (tools._REDACTION_MARKER, _PLAIN_NOTE)


def test_text_spelling_a_secret_only_as_decoded_reads_back_from_the_manifest_as_the_marker(
    tmp_path: Path,
) -> None:
    """A lone surrogate where the known value has three U+FFFD: the mask finds
    nothing, the scrub makes the text exactly the value, and the manifest writes
    its quote as ``\\"``. Read back, the field is the marker and the plain note is
    kept: the field check caught it, which the whole-file check cannot, since the
    file holds the quote escaped."""
    secret = 'abc\ufffd\ufffd\ufffd"'
    text = 'abc\udcff"'
    assert secret not in text

    data = tool_drafts.manifest_bytes(
        _document(attempt_fields={"summary": text, "note": _PLAIN_NOTE}), [secret]
    )

    assert data is not None
    read = _reads_back(tmp_path, data)
    assert read is not None
    last = read["attempts"][-1]
    assert (last["summary"], last["note"]) == (tools._REDACTION_MARKER, _PLAIN_NOTE)


def test_text_a_field_check_lets_through_is_caught_by_the_whole_file_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The field check passes every text as it is; the check of the whole file as
    it is written still sees the secret its JSON spells inside the summary. Every
    free text is then written as the marker -- the plain note too -- and the
    manifest is written without the secret."""
    secret, text = _SPELLED_AS_WRITTEN["newline"]
    monkeypatch.setattr(tools, "_redacted", lambda value, *rest: value)

    data = tool_drafts.manifest_bytes(
        _document(attempt_fields={"summary": text, "handoff": "a plain note"}), [secret]
    )

    assert data is not None
    assert secret.encode("utf-8") not in data
    read = _reads_back(tmp_path, data)
    assert read is not None
    last = read["attempts"][-1]
    assert (last["summary"], last["handoff"]) == (tools._REDACTION_MARKER,) * 2


def test_the_manifest_makes_a_path_with_lone_surrogates_encodable() -> None:
    """A file name read on Linux can carry lone surrogates; the UTF-8 encode of the
    whole manifest would refuse it. It is written with replacement characters."""
    raw = "bad-\udcff-name"
    data = tool_drafts.manifest_bytes(
        _document(
            attempt_fields={
                "omitted": [{"path": raw, "reason": "not_regular"}],
                "omitted_count": 1,
            }
        ),
        [],
    )
    assert data is not None
    stored = json.loads(data)["attempts"][0]["omitted"][0]["path"]
    assert stored == tools._utf8_safe(raw)
    assert stored.startswith("bad-\ufffd")


@pytest.mark.parametrize(
    "overrides",
    [
        {"draft_id": "not-hex"},
        {"generation": 0},
        {"files_dir": "../elsewhere"},
        {"kept": ["files-" + "b" * 32]},  # the files directory itself
        {"base_vid": "nope"},
        {"attempt_count": 2},  # the latest round must be number attempt_count
        {"attempts": []},
        {"attempt_fields": {"outcome": "won"}},
        {"attempt_fields": {"failure": "gremlins"}},
        {"attempt_fields": {"finished_at": "yesterday"}},
        {"attempt_fields": {"omitted_count": -1}},
        {"publishing": {"job_id": "x", "at": _now()}},
    ],
)
def test_a_manifest_out_of_shape_is_unusable(tmp_path: Path, overrides: dict[str, Any]) -> None:
    draft = tmp_path / "draft"
    draft.mkdir()
    (draft / tool_drafts.MANIFEST_FILENAME).write_text(
        json.dumps(_document(**overrides)), encoding="utf-8"
    )
    assert tool_drafts.read_manifest(draft) is None


def test_a_manifest_in_shape_reads_back(tmp_path: Path) -> None:
    """The document the cases above each change one field of, and its text cleaned."""
    draft = tmp_path / "draft"
    draft.mkdir()
    (draft / tool_drafts.MANIFEST_FILENAME).write_text(
        json.dumps(_document(feedback="a\ud800b")), encoding="utf-8"
    )
    manifest = tool_drafts.read_manifest(draft)
    assert manifest is not None
    assert manifest["feedback"] == tools._utf8_safe("a\ud800b")
    manifest["feedback"].encode("utf-8")


def test_the_manifest_caps_match_the_builder_s_note_caps() -> None:
    """A stored note is cut only when its mask made it longer."""
    assert tool_drafts.SUMMARY_CAP == tool_builder._SUMMARY_CAP
    assert tool_drafts.HANDOFF_CAP == tool_builder._HANDOFF_CAP
    assert tool_drafts.OPERATOR_ACTION_CAP == tool_builder._OPERATOR_ACTION_CAP


# --- saving a first round -------------------------------------------------------


def test_a_first_round_with_no_change_and_no_notes_keeps_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)

    quiet = tool_drafts.DraftRound(finished_at=_now(), outcome="failed")
    result = _save(package_root, workspace, copied_in, draft_round=quiet)

    assert result == tool_drafts.SaveResult(worth_saving=False)
    assert not tool_drafts.has_draft(package_root)

    # Notes alone are worth a draft: its files are the unchanged workspace.
    result = _save(package_root, workspace, copied_in)
    assert result.saved
    assert (_files(package_root) / "run.py").read_text(encoding="utf-8") == "print('base')\n"


def test_a_saved_first_round_is_a_complete_copy_with_its_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    os.chmod(workspace / "run.sh", 0o755)
    (workspace / "lib").mkdir()
    (workspace / "lib" / "util.py").write_text("X = 1\n", encoding="utf-8")
    os.symlink("lib/util.py", workspace / "util_link.py")

    result = _save(package_root, workspace, copied_in)

    assert result.saved and result.worth_saving
    assert (result.generation, result.attempts) == (1, 1)
    manifest = _manifest(package_root)
    assert manifest["draft_id"] == result.draft_id
    assert manifest["base_vid"] == _VID
    assert manifest["feedback"] == "加上分頁"
    assert manifest["attempts"][0]["summary"] == "改到一半"
    assert manifest["attempts"][0]["outcome"] == "not_published"
    assert manifest["running"] is None and manifest["publishing"] is None
    files = _files(package_root)
    assert tool_drafts.same_tree(tool_drafts.tree_digest(workspace), tool_drafts.tree_digest(files))
    assert os.access(files / "run.sh", os.X_OK)
    assert os.readlink(files / "util_link.py") == "lib/util.py"
    assert sorted(p.name for p in _draft_dir(package_root).iterdir()) == sorted(
        [tool_drafts.MANIFEST_FILENAME, manifest["files_dir"]]
    )


def test_writes_to_the_workspace_after_the_save_do_not_reach_the_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The draft is a copy, not the workspace moved or hard-linked: a process still
    writing into the workspace changes nothing the draft holds."""
    package_root = _package(monkeypatch, tmp_path)
    workspace = _first_draft(tmp_path, package_root)
    files = _files(package_root)
    before = tool_drafts.tree_digest(files)

    with (workspace / "run.py").open("a", encoding="utf-8") as handle:
        handle.write("print('written after the save')\n")
    (workspace / "late.txt").write_text("late\n", encoding="utf-8")

    assert tool_drafts.same_tree(before, tool_drafts.tree_digest(files))
    assert (workspace / "run.py").stat().st_ino != (files / "run.py").stat().st_ino


def test_a_secret_at_the_end_of_a_long_file_keeps_the_base_version_of_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Past the first 64 KiB, where a head-only read would never look."""
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "run.py").write_text("#" * 200_000 + f"\nKEY = '{_SECRET}'\n", encoding="utf-8")

    result = _save(package_root, workspace, copied_in)

    assert result.saved
    assert (_files(package_root) / "run.py").read_text(encoding="utf-8") == "print('base')\n"
    omitted = _manifest(package_root)["attempts"][-1]["omitted"]
    assert omitted == [{"path": "run.py", "reason": "secret_content"}]


def test_a_path_whose_earlier_version_holds_the_secret_too_is_not_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    (_version(package_root) / "conf.py").write_text(f"KEY = '{_SECRET}'\n", encoding="utf-8")
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "conf.py").write_text(f"KEY = '{_SECRET}'  # still\n", encoding="utf-8")

    assert _save(package_root, workspace, copied_in).saved

    assert not (_files(package_root) / "conf.py").exists()
    assert {"path": "conf.py", "reason": "secret_content"} in _manifest(package_root)["attempts"][
        -1
    ]["omitted"]


@pytest.mark.parametrize(
    ("earlier", "path"),
    [
        ("no-entry", "conf.py"),
        ("no-directory", "new/conf.py"),
        ("a-file-for-a-directory", "sub/conf.py"),
        ("a-directory", "conf.py"),
        ("a-link-the-rule-does-not-keep", "conf.py"),
    ],
)
def test_a_round_still_saves_where_the_earlier_version_is_seen_to_have_nothing_to_put_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, earlier: str, path: str
) -> None:
    """Seeing that the earlier version has nothing to put back for a path holding
    the secret -- no such entry, a directory on the way missing or not a directory,
    a directory where the path is, a link the link rule does not keep there -- is
    a decision about the path, not a failed look: it is left out and the round
    saves."""
    package_root = _package(monkeypatch, tmp_path)
    base = _version(package_root)
    if earlier == "a-file-for-a-directory":
        (base / "sub").write_text("a file\n", encoding="utf-8")
    elif earlier == "a-directory":
        (base / "conf.py").mkdir()
        (base / "conf.py" / "inner.txt").write_text("inside\n", encoding="utf-8")
    elif earlier == "a-link-the-rule-does-not-keep":
        os.symlink("../../outside.py", base / "conf.py")
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    for taken in (workspace / "sub", workspace / "conf.py"):
        if taken.is_symlink() or taken.is_file():
            taken.unlink()
        elif taken.is_dir():
            shutil.rmtree(taken)
    (workspace / path).parent.mkdir(exist_ok=True)
    (workspace / path).write_text(f"KEY = '{_SECRET}'\n", encoding="utf-8")

    result = _save(package_root, workspace, copied_in)

    assert result.saved, result.reason
    assert not os.path.lexists(_files(package_root) / path)
    latest = _manifest(package_root)["attempts"][-1]
    assert latest["saved"] is True
    assert {"path": path, "reason": "secret_content"} in latest["omitted"]


def test_secrets_in_names_and_link_targets_reach_no_byte_under_the_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file and a directory named after the secret, a link whose target spells it,
    and a long file holding it at its end: none of it is kept, the manifest records
    each path with the secret masked, and no name, link target or file byte under
    ``draft/`` holds the value."""
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / f"{_SECRET}.txt").write_text("named after it\n", encoding="utf-8")
    (workspace / f"dir-{_SECRET}").mkdir()
    (workspace / f"dir-{_SECRET}" / "inner.txt").write_text("inside\n", encoding="utf-8")
    os.symlink(f"target-{_SECRET}", workspace / "pointer")
    (workspace / "notes.md").write_text("x" * 70_000 + _SECRET, encoding="utf-8")

    assert _save(package_root, workspace, copied_in).saved

    under = _all_bytes_under(_draft_dir(package_root))
    assert not [piece for piece in under if _SECRET in piece]
    reasons = {item["reason"] for item in _manifest(package_root)["attempts"][-1]["omitted"]}
    assert reasons == {"secret_name", "secret_link_target", "secret_content"}
    assert _manifest(package_root)["attempts"][-1]["omitted_count"] == 4


# A secret a link target spells only once it is stored relative: it begins in the
# ``../`` added in front of the target for the directory the link is in.
_UP_SECRET = "../kb-up-secret"


def test_a_link_whose_target_spells_a_secret_only_as_stored_is_not_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The round's link ``sub/shortcut`` points at ``<workspace>/kb-up-secret``,
    which does not hold the secret ``../kb-up-secret``; stored relative, from
    ``sub/``, its target is ``../kb-up-secret``, which does. The link is not kept,
    it is recorded under its link target, and no name, link target or file byte
    under ``draft/`` holds the secret."""
    package_root = _package(monkeypatch, tmp_path)
    tools.register_inflight_secret(_UP_SECRET)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "sub").mkdir()
    target = f"{workspace}/kb-up-secret"
    os.symlink(target, workspace / "sub" / "shortcut")
    assert _UP_SECRET not in target
    assert tool_drafts.stored_link_target(workspace, ("sub", "shortcut"), target) == (
        _UP_SECRET,
        None,
    )

    assert _save(package_root, workspace, copied_in).saved

    assert not os.path.lexists(_files(package_root) / "sub" / "shortcut")
    assert _manifest(package_root)["attempts"][-1]["omitted"] == [
        {"path": "sub/shortcut", "reason": "secret_link_target"}
    ]
    under = _all_bytes_under(_draft_dir(package_root))
    assert not [piece for piece in under if _UP_SECRET in piece]


def test_an_earlier_link_put_back_whose_target_spells_a_secret_only_as_stored_is_not_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The round pointed ``sub/x`` at a target holding the secret, so the base's
    ``sub/x`` is put back in its place. That earlier link points at
    ``<workspace>/kb-up-secret``: clean as written, ``../kb-up-secret`` -- the
    secret -- once stored relative from ``sub/``. Neither version of ``sub/x`` is
    kept, ``sub/x`` is recorded once, and no name, link target or file byte under
    ``draft/`` holds the secret."""
    package_root = _package(monkeypatch, tmp_path)
    tools.register_inflight_secret(_UP_SECRET)
    workspace = tmp_path / "work" / "build"
    earlier_target = f"{workspace}/kb-up-secret"
    (_version(package_root) / "sub").mkdir()
    os.symlink(earlier_target, _version(package_root) / "sub" / "x")
    assert _workspace(tmp_path, package_root) == workspace
    copied_in = tool_drafts.tree_digest(workspace)
    os.unlink(workspace / "sub" / "x")
    os.symlink(f"run-{_UP_SECRET}.py", workspace / "sub" / "x")
    assert _UP_SECRET not in earlier_target

    assert _save(package_root, workspace, copied_in).saved

    assert not os.path.lexists(_files(package_root) / "sub" / "x")
    assert _manifest(package_root)["attempts"][-1]["omitted"] == [
        {"path": "sub/x", "reason": "secret_link_target"}
    ]
    under = _all_bytes_under(_draft_dir(package_root))
    assert not [piece for piece in under if _UP_SECRET in piece]


def _scan_of_the_copy(
    patch: pytest.MonkeyPatch, answer: tools.SecretScanDone | tools.SecretScanFailed
) -> None:
    """The embedded-secret scan answers ``answer`` for a copy being made
    (``.incoming-<hex>``), and scans every other tree as it is."""
    real_scan = tools.scan_embedded_secrets

    def scan(root: Any, secrets: Any = None) -> tools.SecretScan:
        if tool_drafts._INCOMING_RE.fullmatch(Path(root).name):
            return answer
        return real_scan(root, secrets)

    patch.setattr(tools, "scan_embedded_secrets", scan)


@pytest.mark.parametrize("round_kind", ["first", "continuing"])
def test_a_scan_of_the_copy_that_cannot_finish_keeps_no_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, round_kind: str
) -> None:
    """The workspace scan finishes; the scan of the copy does not. The round's
    files are not saved: a first round leaves no draft, a continuing one records
    the round as not saved with its files directory unchanged, and no copy is
    left under ``draft/``."""
    package_root = _package(monkeypatch, tmp_path)
    previous: tool_drafts.PreviousDraft | None = None
    first: dict[str, Any] | None = None
    if round_kind == "first":
        workspace = _workspace(tmp_path, package_root)
        copied_in = tool_drafts.tree_digest(workspace)
    else:
        _first_draft(tmp_path, package_root)
        first = _manifest(package_root)
        workspace, previous, copied_in = _continuing(tmp_path, package_root)
    (workspace / "run.py").write_text("print('changed again')\n", encoding="utf-8")
    failed = tool_drafts.REASON_SCAN_FAILED.format(reason="OSError")

    with monkeypatch.context() as patch:
        _scan_of_the_copy(patch, tools.SecretScanFailed("OSError"))
        result = _save(package_root, workspace, copied_in, previous=previous)

    assert (result.saved, result.reason) == (False, failed)
    if first is None:
        assert not tool_drafts.has_draft(package_root)
        return
    second = _manifest(package_root)
    assert second["files_dir"] == first["files_dir"]
    assert (second["attempts"][-1]["saved"], second["attempts"][-1]["reason"]) == (False, failed)
    assert sorted(p.name for p in _draft_dir(package_root).iterdir()) == sorted(
        [tool_drafts.MANIFEST_FILENAME, first["files_dir"]]
    )


def test_a_path_the_scan_of_the_copy_could_not_search_keeps_no_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The scan of the copy could not search ``run.py``: whether it may be kept is
    not known, so the save keeps no files and says which; a first round leaves no
    draft."""
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "run.py").write_text("print('changed')\n", encoding="utf-8")
    unsearched = tools.SecretScanDone(
        hits=(), skipped=(tools.SecretScanSkip("run.py", "unreadable"),)
    )

    with monkeypatch.context() as patch:
        _scan_of_the_copy(patch, unsearched)
        result = _save(package_root, workspace, copied_in)

    assert (result.worth_saving, result.saved, result.reason) == (
        True,
        False,
        tool_drafts.REASON_UNLOOKED.format(paths="run.py"),
    )
    assert not tool_drafts.has_draft(package_root)


def test_a_manifest_that_would_hold_a_secret_with_every_text_masked_is_not_saved_and_says_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A text written past the field check altogether -- as a value a builder
    hands over without saying what it is -- still holds the secret once every
    free text is the marker. Nothing is written: the first round keeps no draft,
    and its reason says the manifest would hold a known secret."""
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "run.py").write_text("print('changed')\n", encoding="utf-8")
    monkeypatch.setattr(tools._Writing, "text", lambda self, value, cap=None: value)

    result = _save(
        package_root,
        workspace,
        copied_in,
        draft_round=tool_drafts.DraftRound(
            finished_at=_now(), outcome="not_published", summary=f"used {_SECRET}"
        ),
    )

    assert (result.saved, result.reason) == (False, tool_drafts.REASON_MANIFEST_SECRET)
    assert not tool_drafts.has_draft(package_root)


def test_a_scan_that_cannot_finish_keeps_no_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "run.py").write_text("print('changed')\n", encoding="utf-8")
    monkeypatch.setattr(
        tools, "scan_embedded_secrets", lambda root, secrets=None: tools.SecretScanFailed("OSError")
    )

    result = _save(package_root, workspace, copied_in)

    assert not result.saved
    assert result.reason == tool_drafts.REASON_SCAN_FAILED.format(reason="OSError")
    assert not tool_drafts.has_draft(package_root)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can look into a mode-000 directory")
def test_a_save_while_another_tools_env_cannot_be_read_keeps_no_files_and_says_which(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Another tool's directory cannot be looked at, so its ``.env`` -- and the
    value in it -- is not in the known list, while the workspace holds that value.
    The list is not whole: nothing is saved, no draft is made, and the reason says
    which ``.env`` could not be read and what to check. Red before the fix: the
    list was taken as whole, and the file with the other tool's value was saved."""
    package_root = _package(monkeypatch, tmp_path)
    other = package_root.path.parent / "other"
    other.mkdir()
    (other / ".env").write_text("OTHER_KEY=other-secret-abcdef\n", encoding="utf-8")
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "run.py").write_text("print('other-secret-abcdef')\n", encoding="utf-8")
    other.chmod(0)
    try:
        result = _save(package_root, workspace, copied_in)
    finally:
        other.chmod(0o700)

    assert not result.saved
    assert not tool_drafts.has_draft(package_root)
    assert result.reason is not None
    assert result.reason.startswith(tool_drafts.REASON_SECRETS_INCOMPLETE.format(detail=""))
    assert "「other」" in result.reason
    assert os.fspath(other / ".env") in result.reason
    assert "權限" in result.reason


@pytest.mark.parametrize(
    ("max_files", "max_bytes", "reason"),
    [
        (1, 50_000_000, tool_drafts.REASON_TOO_MANY_FILES),
        (2000, 10, tool_drafts.REASON_TOO_LARGE),
    ],
    ids=["files", "bytes"],
)
def test_a_workspace_over_the_limits_keeps_no_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, max_files: int, max_bytes: int, reason: str
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "run.py").write_text("print('changed')\n", encoding="utf-8")

    result = _save(package_root, workspace, copied_in, max_files=max_files, max_bytes=max_bytes)

    assert (result.saved, result.reason) == (False, reason)
    assert not tool_drafts.has_draft(package_root)


_TEN_MB = b"x" * 10_000_000


def test_a_first_round_whose_earlier_version_put_back_is_over_the_byte_limit_keeps_no_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The base holds a 10 MB file; the round turned it into a few bytes holding
    the secret, so the workspace is far under a 1 MB limit. The save would put
    the base's 10 MB back in its place: the limit counts what the draft would
    hold, so there is no draft."""
    package_root = _package(monkeypatch, tmp_path)
    (_version(package_root) / "data.bin").write_bytes(_TEN_MB)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "data.bin").write_text(f"KEY = '{_SECRET}'\n", encoding="utf-8")

    result = _save(package_root, workspace, copied_in, max_bytes=1_000_000)

    assert (result.saved, result.reason) == (False, tool_drafts.REASON_TOO_LARGE)
    assert not tool_drafts.has_draft(package_root)


def test_a_continuing_round_whose_earlier_version_put_back_is_over_the_byte_limit_keeps_its_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same with the draft's own files as the earlier version: the round is
    recorded as not saved, the files directory stays the previous one, and the
    copy that went over the limit is not left behind."""
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "data.bin").write_bytes(_TEN_MB)
    assert _save(package_root, workspace, copied_in).saved
    first = _manifest(package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    (workspace / "data.bin").write_text(f"KEY = '{_SECRET}'\n", encoding="utf-8")

    result = _save(package_root, workspace, copied_in, previous=previous, max_bytes=1_000_000)

    assert (result.saved, result.reason) == (False, tool_drafts.REASON_TOO_LARGE)
    second = _manifest(package_root)
    assert second["files_dir"] == first["files_dir"]
    assert (second["attempts"][-1]["saved"], second["attempts"][-1]["reason"]) == (
        False,
        tool_drafts.REASON_TOO_LARGE,
    )
    assert sorted(p.name for p in _draft_dir(package_root).iterdir()) == sorted(
        [tool_drafts.MANIFEST_FILENAME, first["files_dir"]]
    )


@pytest.mark.parametrize("growth", ["earlier-file", "workspace-entries"])
def test_the_limits_count_the_sizes_the_copy_sees_not_those_seen_when_it_was_planned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, growth: str
) -> None:
    """A file of the earlier version that grows past the byte limit after the save
    planned it -- the operator may edit it meanwhile -- or entries that appear in
    the workspace after it was measured: measured beforehand, both fit; counted as
    the copy reaches them, neither does, and there is no draft. Growth while one
    file is being read is not part of this."""
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    max_files, max_bytes = 2000, 50_000_000
    if growth == "earlier-file":
        (workspace / "run.py").write_text(f"KEY = '{_SECRET}'\n", encoding="utf-8")
        real_lock = tools.acquire_shared_tools_lock

        def lock_after_an_edit() -> Any:
            with (_version(package_root) / "run.py").open("ab") as handle:
                handle.write(b"#" * 2_000_000)
            return real_lock()

        monkeypatch.setattr(tools, "acquire_shared_tools_lock", lock_after_an_edit)
        max_bytes = 1_000_000
        reason = tool_drafts.REASON_TOO_LARGE
    else:
        (workspace / "run.py").write_text("print('changed')\n", encoding="utf-8")
        measured = tool_drafts.tree_digest(workspace)
        assert measured is not None
        real_scan = tools.scan_embedded_secrets

        def scan_after_more_appeared(root: Path, secrets: Any = None) -> Any:
            for index in range(5):
                (workspace / f"late-{index}.txt").write_text("late\n", encoding="utf-8")
            return real_scan(root, secrets)

        monkeypatch.setattr(tools, "scan_embedded_secrets", scan_after_more_appeared)
        max_files = len(measured)
        reason = tool_drafts.REASON_TOO_MANY_FILES

    result = _save(package_root, workspace, copied_in, max_files=max_files, max_bytes=max_bytes)

    assert (result.saved, result.reason) == (False, reason)
    assert not tool_drafts.has_draft(package_root)


def test_links_that_leave_the_workspace_are_not_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    os.symlink("/etc/hosts", workspace / "abs_out")
    os.symlink("../../outside", workspace / "rel_out")
    os.symlink(".", workspace / "here")
    os.symlink("here/../beside", workspace / "through_link")
    os.symlink(os.fspath(workspace / "run.py"), workspace / "abs_in")

    assert _save(package_root, workspace, copied_in).saved

    files = _files(package_root)
    assert os.readlink(files / "abs_in") == "run.py"
    assert os.readlink(files / "here") == "."
    for gone in ("abs_out", "rel_out", "through_link"):
        assert not os.path.lexists(files / gone)
    omitted = {
        item["path"]: item["reason"] for item in _manifest(package_root)["attempts"][-1]["omitted"]
    }
    assert omitted == {
        "abs_out": "link_outside",
        "rel_out": "link_outside",
        "through_link": "link_ambiguous",
    }


@pytest.mark.parametrize(
    ("x_target", "reached"),
    [("d/sub", "d/data.txt"), (".", "../data.txt")],
    ids=["x-into-a-subdirectory", "x-at-the-top"],
)
def test_an_absolute_target_going_up_from_a_link_is_not_kept_and_is_recorded_ambiguous(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, x_target: str, reached: str
) -> None:
    """``shortcut -> <workspace>/x/../data.txt``: the operating system follows ``x``
    first and goes up from where it points -- to ``d/data.txt`` when
    ``x -> d/sub``, out of the workspace when ``x -> .`` -- while the same path
    with ``x/..`` folded away names the top's ``data.txt``, another file. Judged
    like the same path written relative, it is not kept and is recorded as
    ambiguous."""
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "d" / "sub").mkdir(parents=True)
    (workspace / "d" / "data.txt").write_text("in d\n", encoding="utf-8")
    (workspace / "data.txt").write_text("at the top\n", encoding="utf-8")
    (tmp_path / "work" / "data.txt").write_text("outside\n", encoding="utf-8")
    os.symlink(x_target, workspace / "x")
    target = f"{workspace}/x/../data.txt"
    os.symlink(target, workspace / "shortcut")
    landed = os.path.realpath(workspace / "shortcut", strict=True)
    assert os.path.relpath(landed, os.path.realpath(workspace)) == reached
    for written in (target, "x/../data.txt"):
        assert tool_drafts.stored_link_target(workspace, ("shortcut",), written) == (
            None,
            "link_ambiguous",
        )

    assert _save(package_root, workspace, copied_in).saved

    files = _files(package_root)
    assert not os.path.lexists(files / "shortcut")
    assert os.readlink(files / "x") == x_target
    assert _manifest(package_root)["attempts"][-1]["omitted"] == [
        {"path": "shortcut", "reason": "link_ambiguous"}
    ]


def test_an_absolute_target_going_up_from_a_real_directory_is_stored_as_written_and_reads_the_same(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``..`` right after a real directory goes where the string says, so such
    a link is kept: stored as what follows the workspace's path, every component
    as written -- ``.``, ``..`` and an empty one too -- behind a ``..`` for each
    directory the link is in, and the copy reads what the workspace did."""
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "d").mkdir()
    (workspace / "links").mkdir()
    (workspace / "data.txt").write_text("at the top\n", encoding="utf-8")
    links = {
        "up": (f"{workspace}/d/../data.txt", "d/../data.txt"),
        "links/up": (f"{workspace}/./d//../data.txt", ".././d//../data.txt"),
    }
    for path, (target, _stored) in links.items():
        os.symlink(target, workspace / path)

    assert _save(package_root, workspace, copied_in).saved

    files = _files(package_root)
    for path, (_target, stored) in links.items():
        assert os.readlink(files / path) == stored
        assert (files / path).read_text(encoding="utf-8") == "at the top\n"
        assert (workspace / path).read_text(encoding="utf-8") == "at the top\n"
    assert _manifest(package_root)["attempts"][-1]["omitted"] == []


def _resolved_in(root: Path, link: Path) -> str | None:
    """Where the operating system resolves ``link``, relative to ``root``'s real
    path -- ``..`` first when outside it -- or None when it resolves nowhere."""
    try:
        os.stat(link)
        landed = os.path.realpath(link, strict=True)
    except OSError:
        return None
    return os.path.relpath(landed, os.path.realpath(root))


def _resolution_kind(where: str | None) -> str:
    if where is None:
        return "dangling"
    return "outside" if where == ".." or where.startswith("../") else "inside"


def _links_under(root: Path) -> list[str]:
    """Every link under ``root``, as a path relative to it, never followed."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                found.append(path.relative_to(root).as_posix())
    return sorted(found)


def test_listed_link_shapes_resolve_in_the_copy_as_in_the_workspace_or_are_recorded_not_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every link below is resolved by the operating system in the workspace, and
    after the save in the copy. One the system resolved inside resolves in the
    copy to the same place, unless it is a shape the rule declines although the
    system resolves it inside -- a ``..`` right after a link, a path that climbs
    above the workspace and comes back, one that begins with the workspace's path
    spelled another way -- which is not kept and is recorded under that reason.
    One that resolved nowhere resolves nowhere in the copy either, or is recorded
    as not kept. One that resolved outside is not in the copy and is recorded as
    not kept. Nothing in the workspace holds a known secret, so no earlier version
    is put back. The workspace is reached through a link, so its path as given and
    as resolved differ, and absolute targets are written both ways."""
    package_root = _package(monkeypatch, tmp_path)
    _workspace(tmp_path, package_root)
    os.symlink("work", tmp_path / "alias")
    workspace = tmp_path / "alias" / "build"
    copied_in = tool_drafts.tree_digest(workspace)
    given, real = os.fspath(workspace), os.path.realpath(workspace)
    assert given != real
    (workspace / "d" / "sub").mkdir(parents=True)
    (workspace / "d" / "data.txt").write_text("in d\n", encoding="utf-8")
    (workspace / "d" / "sub" / "deep.txt").write_text("deep\n", encoding="utf-8")
    (workspace / "data.txt").write_text("at the top\n", encoding="utf-8")
    (workspace / "links").mkdir()
    (tmp_path / "work" / "outside.txt").write_text("outside\n", encoding="utf-8")
    (tmp_path / "work" / "build-other").mkdir()
    (tmp_path / "work" / "build-other" / "data.txt").write_text("beside\n", encoding="utf-8")
    # link: (target, where the system resolves it, why the rule declines it if it does)
    roster: dict[str, tuple[str, str, str | None]] = {
        # the links the others go through
        "to_d": ("d", "inside", None),
        "to_sub": ("d/sub", "inside", None),
        "to_top": (".", "inside", None),
        # relative
        "rel": ("data.txt", "inside", None),
        "rel_dot": ("./data.txt", "inside", None),
        "rel_empty": ("d//data.txt", "inside", None),
        "rel_through_dirs": ("d/sub/deep.txt", "inside", None),
        "rel_up_from_dir": ("d/../data.txt", "inside", None),
        "rel_through_link": ("to_d/data.txt", "inside", None),
        "rel_up_from_link": ("to_sub/../data.txt", "inside", "link_ambiguous"),
        "rel_up_from_top_link": ("to_top/../outside.txt", "outside", None),
        "rel_top": (".", "inside", None),
        "rel_out": ("../outside.txt", "outside", None),
        "rel_out_and_back": ("../build/data.txt", "inside", "link_outside"),
        "rel_missing": ("missing.txt", "dangling", None),
        "rel_up_from_missing": ("missing/../data.txt", "dangling", None),
        "rel_file_as_dir": ("data.txt/", "dangling", None),
        "links/rel_up": ("../data.txt", "inside", None),
        "links/rel_top": ("..", "inside", None),
        "links/rel_out": ("../../outside.txt", "outside", None),
        # absolute, the workspace's path as given
        "abs": (f"{given}/data.txt", "inside", None),
        "abs_dot": (f"{given}/./data.txt", "inside", None),
        "abs_empty": (f"{given}//data.txt", "inside", None),
        "abs_through_dirs": (f"{given}/d/sub/deep.txt", "inside", None),
        "abs_up_from_dir": (f"{given}/d/../data.txt", "inside", None),
        "abs_through_link": (f"{given}/to_d/data.txt", "inside", None),
        "abs_up_from_link": (f"{given}/to_sub/../data.txt", "inside", "link_ambiguous"),
        "abs_up_from_top_link": (f"{given}/to_top/../outside.txt", "outside", None),
        "abs_top": (given, "inside", None),
        "abs_top_slash": (f"{given}/", "inside", None),
        "abs_missing": (f"{given}/missing.txt", "dangling", None),
        "abs_file_as_dir": (f"{given}/data.txt/", "dangling", None),
        "abs_out": (f"{os.path.dirname(given)}/outside.txt", "outside", None),
        "abs_beside": (f"{given}-other/data.txt", "outside", None),
        "abs_out_and_back": (f"{given}/../build/data.txt", "inside", "link_outside"),
        "abs_double_slash": (f"/{given}/data.txt", "inside", "link_outside"),
        # absolute, the workspace's path as resolved
        "abs_resolved": (f"{real}/data.txt", "inside", None),
        "abs_resolved_up_from_link": (f"{real}/to_sub/../data.txt", "inside", "link_ambiguous"),
        "abs_resolved_beside": (f"{real}-other/data.txt", "outside", None),
        # absolute, from a directory below the top
        "links/abs": (f"{given}/data.txt", "inside", None),
        "links/abs_empty": (f"{given}//data.txt", "inside", None),
        "links/abs_up_from_dir": (f"{real}/d/../data.txt", "inside", None),
        "links/abs_top": (given, "inside", None),
        "links/abs_top_slash": (f"{real}/", "inside", None),
        "links/abs_up_from_link": (f"{given}/to_sub/../data.txt", "inside", "link_ambiguous"),
        "links/abs_out": (f"{real}/../outside.txt", "outside", None),
    }
    for path, (target, _kind, _declined) in roster.items():
        os.symlink(target, workspace / path)
    assert _links_under(workspace) == sorted(roster)
    before = {path: _resolved_in(workspace, workspace / path) for path in roster}
    for path, (_target, kind, _declined) in roster.items():
        assert _resolution_kind(before[path]) == kind, path

    assert _save(package_root, workspace, copied_in).saved

    files = _files(package_root)
    omitted = {
        item["path"]: item["reason"] for item in _manifest(package_root)["attempts"][-1]["omitted"]
    }
    for path, (_target, kind, declined) in roster.items():
        copied = files / path
        if kind == "inside" and declined is None:
            assert os.path.islink(copied), path
            assert _resolved_in(files, copied) == before[path], path
        elif kind == "dangling":
            assert not os.path.lexists(copied) or _resolved_in(files, copied) is None, path
        else:
            assert not os.path.lexists(copied), path
            assert path in omitted, path
            if declined is not None:
                assert omitted[path] == declined, path
    # Recorded as not kept: exactly the listed links the copy does not have.
    assert sorted(omitted) == sorted(set(roster) - set(_links_under(files)))


@pytest.mark.parametrize("put_back", ["file-for-link", "link-for-link"])
def test_a_link_that_leaves_the_copy_once_an_earlier_version_is_put_back_is_not_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, put_back: str
) -> None:
    """On the workspace ``l`` stays inside; in the copy, ``x`` holds the earlier
    version put back in its place -- a link where the round left a regular file,
    or a link with another target than the round's -- and through it ``l`` reaches
    the draft's own manifest, outside the files directory. Judged again on the
    copy, ``l`` is not kept and is recorded as not kept; ``x`` keeps its earlier
    version."""
    package_root = _package(monkeypatch, tmp_path)
    version = _version(package_root)
    if put_back == "file-for-link":
        os.symlink(".", version / "x")
        target = "x/../manifest.json"
        x_reason = "secret_content"
    else:
        (version / "b").mkdir()
        os.symlink(".", version / "b" / "y")
        os.symlink(".", version / "z")
        os.symlink("b", version / "x")
        target = "x/y/../z/../manifest.json"
        x_reason = "secret_link_target"
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    os.unlink(workspace / "x")
    if put_back == "file-for-link":
        (workspace / "x").write_text(f"KEY = '{_SECRET}'\n", encoding="utf-8")
    else:
        os.symlink(f"gone-{_SECRET}", workspace / "x")
    os.symlink(target, workspace / "l")
    assert tool_drafts.stored_link_target(workspace, ("l",), target) == (target, None)

    assert _save(package_root, workspace, copied_in).saved

    files = _files(package_root)
    assert not os.path.lexists(files / "l")
    assert os.readlink(files / "x") == os.readlink(version / "x")
    omitted = {
        item["path"]: item["reason"] for item in _manifest(package_root)["attempts"][-1]["omitted"]
    }
    assert omitted == {"l": "link_ambiguous", "x": x_reason}


def test_an_earlier_link_put_back_that_stays_inside_the_copy_is_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The round pointed ``x`` at a target holding the secret, and the base's
    ``x`` points at ``run.py``. Judged again on the copy, that earlier link stays
    inside, so the draft keeps it; only this round's version of ``x`` is recorded
    as not kept."""
    package_root = _package(monkeypatch, tmp_path)
    os.symlink("run.py", _version(package_root) / "x")
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    os.unlink(workspace / "x")
    os.symlink(f"run-{_SECRET}.py", workspace / "x")

    assert _save(package_root, workspace, copied_in).saved

    files = _files(package_root)
    assert os.readlink(files / "x") == "run.py"
    assert (files / "x").read_text(encoding="utf-8") == "print('base')\n"
    assert _manifest(package_root)["attempts"][-1]["omitted"] == [
        {"path": "x", "reason": "secret_link_target"}
    ]


def _omitted(package_root: tools.PackageRoot) -> dict[str, str]:
    """What the draft's last round records as not kept, by path."""
    return {
        item["path"]: item["reason"] for item in _manifest(package_root)["attempts"][-1]["omitted"]
    }


def _up_through_a_put_back_link(workspace: Path) -> None:
    """In a workspace whose earlier version holds ``x -> .``: ``x`` becomes a file
    holding the secret, and ``l -> x/../manifest.json``. On the workspace ``l``
    stays inside; in the copy ``x`` is the earlier link put back, and ``l`` goes
    up through it to where the draft's manifest is, outside the files directory."""
    os.unlink(workspace / "x")
    (workspace / "x").write_text(f"KEY = '{_SECRET}'\n", encoding="utf-8")
    os.symlink("x/../manifest.json", workspace / "l")


def test_a_link_judged_on_the_copy_past_a_component_that_cannot_be_looked_at_keeps_no_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the copy's ``l -> x/../manifest.json`` is judged, ``x`` cannot be
    looked at, so whether the ``..`` follows a link is not known -- not judged as
    if ``x`` were a directory: the save keeps no files and names ``l``."""
    package_root = _package(monkeypatch, tmp_path)
    os.symlink(".", _version(package_root) / "x")
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    _up_through_a_put_back_link(workspace)
    refused = _refuse(monkeypatch, "lstat", _in_the_copy("x"), errno.EIO, times=1)

    result = _save(package_root, workspace, copied_in)

    assert len(refused) == 1
    assert (result.saved, result.reason) == (
        False,
        tool_drafts.REASON_UNLOOKED.format(paths="l"),
    )
    assert not tool_drafts.has_draft(package_root)


@pytest.mark.parametrize("round_kind", ["first", "continuing"])
def test_a_link_that_leaves_the_copy_is_not_kept_even_where_the_link_rule_misses_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, round_kind: str
) -> None:
    """With the link rule's judgment of the copy taken out, as if it had missed
    ``l``, the operating system's resolution of each link left still decides.
    ``l -> x/../manifest.json`` goes up through ``x -> .`` to the draft's
    manifest: there already when a draft is continued; written after the check in
    a first round, where ``l`` resolves nowhere when checked, at a point outside
    the copy. Either way it is not kept and is recorded as outside."""
    package_root = _package(monkeypatch, tmp_path)
    os.symlink(".", _version(package_root) / "x")
    previous = None
    if round_kind == "first":
        workspace = _workspace(tmp_path, package_root)
        copied_in = tool_drafts.tree_digest(workspace)
    else:
        _first_draft(tmp_path, package_root)
        workspace, previous, copied_in = _continuing(tmp_path, package_root)
    _up_through_a_put_back_link(workspace)
    monkeypatch.setattr(tool_drafts, "_links_failing_on", lambda root: ([], []))

    assert _save(package_root, workspace, copied_in, previous=previous).saved

    files = _files(package_root)
    assert not os.path.lexists(files / "l")
    assert os.readlink(files / "x") == "."
    assert _omitted(package_root) == {"l": "link_outside", "x": "secret_content"}


def test_a_link_seen_to_resolve_nowhere_inside_the_copy_is_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Links the operating system resolves nowhere, failing at a point inside the
    copy -- a missing name, a name under a regular file, two links naming each
    other -- are kept as written: the copy only loses entries after the check, so
    they resolve nowhere in the saved draft too."""
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    links = {
        "missing": "missing.txt",
        "under_a_file": "run.py/x",
        "loop_a": "loop_b",
        "loop_b": "loop_a",
    }
    for name, target in links.items():
        os.symlink(target, workspace / name)

    assert _save(package_root, workspace, copied_in).saved

    files = _files(package_root)
    for name, target in links.items():
        assert os.readlink(files / name) == target
        assert _resolved_in(files, files / name) is None
    assert _omitted(package_root) == {}


def test_a_link_whose_resolution_cannot_be_told_keeps_no_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``l -> run.py``: while the copy's links are resolved, ``run.py`` cannot be
    looked at, so where ``l`` lands is not known. The save keeps no files and
    names ``l``."""
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    os.symlink("run.py", workspace / "l")
    real_realpath = os.path.realpath
    resolving = [False]

    def realpath(path: Any, *, strict: Any = False) -> str:
        resolving[0] = True
        try:
            return real_realpath(path, strict=strict)
        finally:
            resolving[0] = False

    monkeypatch.setattr(os.path, "realpath", realpath)
    in_the_copy = _in_the_copy("run.py")
    refused = _refuse(
        monkeypatch, "lstat", lambda path: resolving[0] and in_the_copy(path), errno.EIO, times=1
    )

    result = _save(package_root, workspace, copied_in)

    assert (result.saved, result.reason) == (
        False,
        tool_drafts.REASON_UNLOOKED.format(paths="l"),
    )
    assert not tool_drafts.has_draft(package_root)
    assert len(refused) == 1


def test_a_fifo_in_the_workspace_is_not_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    os.mkfifo(workspace / "pipe")

    assert _save(package_root, workspace, copied_in).saved

    assert not os.path.lexists(_files(package_root) / "pipe")
    assert _manifest(package_root)["attempts"][-1]["omitted"] == [
        {"path": "pipe", "reason": "not_regular"}
    ]


def test_a_name_that_is_not_utf8_is_recorded_encodable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    try:
        os.mkfifo(os.fsencode(workspace) + b"/bad-\xff-name")
    except OSError as exc:
        if exc.errno != errno.EILSEQ:
            raise
        pytest.skip("this filesystem refuses file names that are not UTF-8")

    assert _save(package_root, workspace, copied_in).saved

    assert _manifest(package_root)["attempts"][-1]["omitted"] == [
        {"path": tools._utf8_safe("bad-\udcff-name"), "reason": "not_regular"}
    ]


def test_a_failed_first_save_takes_back_what_it_made(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The copy fails after ``draft/`` and its incoming directory exist: both are
    removed, so no unusable draft is left to block the next revise."""
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "run.py").write_text("print('changed')\n", encoding="utf-8")
    real_copy = tool_drafts._copy_regular

    def failing_copy(source: Path, destination: Path, budget: Any) -> None:
        real_copy(source, destination, budget)
        raise OSError("disk full")

    monkeypatch.setattr(tool_drafts, "_copy_regular", failing_copy)

    result = _save(package_root, workspace, copied_in)

    assert not result.saved
    assert result.reason == tool_drafts.REASON_COPY.format(kind="OSError")
    assert not tool_drafts.has_draft(package_root)


def test_a_failed_durability_check_of_the_new_copy_keeps_no_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "run.py").write_text("print('changed')\n", encoding="utf-8")

    result = tool_drafts.save_round(
        package_root=package_root,
        package_dir_identity=tool_drafts.directory_identity(package_root.path),
        workspace=workspace,
        earlier_root=_version(package_root),
        copied_in=copied_in,
        draft_round=tool_drafts.DraftRound(finished_at=_now(), summary="s"),
        start=tool_drafts.DraftStart(base_vid=_VID, feedback=None),
        previous=None,
        max_files=2000,
        max_bytes=50_000_000,
        durability=tool_drafts.Durability(
            tree=lambda _root: False, directory=tool_builder._fsync_directory
        ),
    )

    assert (result.saved, result.reason) == (False, tool_drafts.REASON_DURABILITY)
    assert not tool_drafts.has_draft(package_root)


@pytest.mark.parametrize("round_kind", ["first", "continuing"])
def test_a_save_whose_copy_cannot_be_listed_whole_while_made_durable_is_not_saved_or_committed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, round_kind: str
) -> None:
    """While the new copy is made durable (``tool_builder._fsync_tree``), a
    directory in it cannot be listed, so what is under it was never synced: the
    save is not reported saved, and no manifest names the copy -- a first round
    leaves no draft, a continuation keeps the files directory it had."""
    package_root = _package(monkeypatch, tmp_path)
    previous = None
    before = None
    if round_kind == "first":
        workspace = _workspace(tmp_path, package_root)
        copied_in = tool_drafts.tree_digest(workspace)
    else:
        _first_draft(tmp_path, package_root)
        before = _manifest(package_root)["files_dir"]
        workspace, previous, copied_in = _continuing(tmp_path, package_root)
    (workspace / "sub").mkdir()
    (workspace / "sub" / "notes.txt").write_text("under a directory\n", encoding="utf-8")
    real_tree = tool_builder._fsync_tree
    syncing = [False]

    def tree(root: Path) -> bool:
        syncing[0] = True
        try:
            return real_tree(root)
        finally:
            syncing[0] = False

    monkeypatch.setattr(tool_builder, "_fsync_tree", tree)
    in_the_copy = _in_the_copy("sub")
    refused = _refuse(
        monkeypatch, "scandir", lambda path: syncing[0] and in_the_copy(path), errno.EIO
    )

    result = _save(package_root, workspace, copied_in, previous=previous)

    assert len(refused) == 1
    assert (result.saved, result.reason) == (False, tool_drafts.REASON_DURABILITY)
    if before is None:
        assert not tool_drafts.has_draft(package_root)
    else:
        assert _manifest(package_root)["files_dir"] == before


def test_a_package_made_again_under_its_name_gets_nothing_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The package the round began in was deleted and another installed under the
    same name: the save writes nothing into the new one, although a package of
    that name exists. The old directory is held open while it is deleted, so its
    inode number cannot be handed straight to the new one (ext4 reuses them) and
    the two directories really differ, as they do when the backend reinstalls."""
    package_root = _package(monkeypatch, tmp_path)
    identity = tool_drafts.directory_identity(package_root.path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "run.py").write_text("print('changed')\n", encoding="utf-8")
    holder = os.open(package_root.path, os.O_RDONLY)
    try:
        shutil.rmtree(package_root.path)
        _make_package(package_root.path.parent)
        assert tool_drafts.directory_identity(package_root.path) != identity

        result = _save(package_root, workspace, copied_in, identity=identity)
    finally:
        os.close(holder)

    assert (result.saved, result.reason) == (False, tool_drafts.REASON_PACKAGE_GONE)
    assert not tool_drafts.has_draft(package_root)


def test_a_deleted_package_is_not_made_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    identity = tool_drafts.directory_identity(package_root.path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "run.py").write_text("print('changed')\n", encoding="utf-8")
    shutil.rmtree(package_root.path)

    result = _save(package_root, workspace, copied_in, identity=identity)

    assert (result.saved, result.reason) == (False, tool_drafts.REASON_PACKAGE_GONE)
    assert not os.path.lexists(package_root.path)


# --- saving a continuing round --------------------------------------------------


def test_a_continuing_round_replaces_the_files_and_removes_the_previous_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    first = _manifest(package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    (workspace / "run.py").write_text("print('second round')\n", encoding="utf-8")

    result = _save(
        package_root, workspace, copied_in, previous=previous, earlier_root=_files(package_root)
    )

    assert result.saved
    second = _manifest(package_root)
    assert (second["draft_id"], second["generation"], second["attempt_count"]) == (
        first["draft_id"],
        previous.manifest["generation"] + 1,
        2,
    )
    assert [attempt["number"] for attempt in second["attempts"]] == [1, 2]
    # Named in the same write that commits the new copy, then removed: the name
    # goes once a later write sees it gone, and the list shows none meanwhile.
    assert (second["kept"], second["running"]) == ([first["files_dir"]], None)
    assert not (_draft_dir(package_root) / first["files_dir"]).exists()
    assert (result.reason, result.recorded, result.durable) == (None, True, True)
    assert (_files(package_root) / "run.py").read_text(
        encoding="utf-8"
    ) == "print('second round')\n"


def test_a_continuing_round_that_changed_nothing_records_the_round_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    first = _manifest(package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)

    result = _save(package_root, workspace, copied_in, previous=previous)

    assert result.saved
    second = _manifest(package_root)
    assert second["files_dir"] == first["files_dir"]
    assert second["attempt_count"] == 2


def test_an_unchanged_round_whose_files_were_not_digested_when_copied_out_is_not_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The files directory's digest could not be taken when the round copied it
    out: whether the directory is still what the round received is not known, so
    the round that changed nothing is recorded as not saved, and the directory is
    left as it is."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    first = _manifest(package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    unknown = tool_drafts.PreviousDraft(manifest=previous.manifest, copied_out=None)

    result = _save(package_root, workspace, copied_in, previous=unknown)

    second = _manifest(package_root)
    assert (result.saved, result.recorded, result.reason) == (
        False,
        True,
        tool_drafts.REASON_FILES_CHANGED,
    )
    assert (second["files_dir"], second["kept"]) == (first["files_dir"], [])
    latest = second["attempts"][-1]
    assert (latest["saved"], latest["reason"]) == (False, tool_drafts.REASON_FILES_CHANGED)


def test_the_previous_copy_stays_when_the_new_manifest_is_not_confirmed_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The manifest was replaced but its directory's fsync failed: a crash could
    bring the old manifest back, naming the old copy, so the old copy stays."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    first = _manifest(package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    (workspace / "run.py").write_text("print('second round')\n", encoding="utf-8")
    real_write = tools._write_package_file_atomic

    def replaced_not_durable(
        directory: Path, filename: str, data: bytes, expected: Any, **kwargs: Any
    ) -> bool:
        out = kwargs.pop("durability_out", None)
        published = real_write(directory, filename, data, expected, **kwargs)
        if out is not None:
            out.append(False)
        return published

    monkeypatch.setattr(tools, "_write_package_file_atomic", replaced_not_durable)

    assert _save(package_root, workspace, copied_in, previous=previous).saved

    second = _manifest(package_root)
    assert second["generation"] == previous.manifest["generation"] + 1
    assert second["kept"] == [first["files_dir"]]
    assert (_draft_dir(package_root) / first["files_dir"]).is_dir()


def test_a_previous_copy_changed_during_the_round_is_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The operator edited the draft's files while the round ran: that copy is not
    removed but listed in ``kept``, and the sweep leaves it alone."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    first = _manifest(package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    (workspace / "run.py").write_text("print('second round')\n", encoding="utf-8")
    (_draft_dir(package_root) / first["files_dir"] / "by_hand.txt").write_text(
        "mine\n", encoding="utf-8"
    )

    assert _save(package_root, workspace, copied_in, previous=previous).saved

    second = _manifest(package_root)
    assert second["kept"] == [first["files_dir"]]
    tool_drafts.sweep_package(package_root.path, fsync_directory=tool_builder._fsync_directory)
    assert (_draft_dir(package_root) / first["files_dir"] / "by_hand.txt").is_file()


def test_a_half_made_incoming_copy_does_not_stop_the_next_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A save stopped part-way leaves its ``.incoming-<hex>`` behind, as a crash
    would; the next save of the same round still goes through, under a name of
    its own."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    (workspace / "run.py").write_text("print('second round')\n", encoding="utf-8")
    real_carry_out = tool_drafts._carry_out

    def crash(plan: Any, incoming: Path, known: list[str]) -> Any:
        real_carry_out(plan, incoming, known)
        raise RuntimeError("the process died here")

    monkeypatch.setattr(tool_drafts, "_carry_out", crash)
    assert not _save(package_root, workspace, copied_in, previous=previous).saved
    [half_made] = [
        p.name for p in _draft_dir(package_root).iterdir() if p.name.startswith(".incoming-")
    ]
    monkeypatch.setattr(tool_drafts, "_carry_out", real_carry_out)

    result = _save(package_root, workspace, copied_in, previous=previous)

    assert result.saved
    assert (_files(package_root) / "run.py").read_text(
        encoding="utf-8"
    ) == "print('second round')\n"
    assert (_draft_dir(package_root) / half_made).is_dir()


def test_a_failed_continuing_save_still_records_the_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    first = _manifest(package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    (workspace / "run.py").write_text("print('second round')\n", encoding="utf-8")

    result = _save(package_root, workspace, copied_in, previous=previous, max_files=1)

    assert (result.saved, result.reason) == (False, tool_drafts.REASON_TOO_MANY_FILES)
    second = _manifest(package_root)
    assert second["files_dir"] == first["files_dir"]
    assert second["generation"] == previous.manifest["generation"] + 1
    assert second["running"] is None
    assert (second["attempts"][-1]["saved"], second["attempts"][-1]["reason"]) == (
        False,
        tool_drafts.REASON_TOO_MANY_FILES,
    )


@pytest.mark.parametrize("fails_at", ["limit", "scan", "plan", "copy", "manifest"])
def test_a_failed_continuing_save_a_publish_follows_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fails_at: str
) -> None:
    """The save a publish follows fails at a step where a save that no publish
    follows records the round: over the limits, the scan, the plan, the copy, or
    the write naming the new copy -- refused once, so a second write would go
    through. Nothing is written: the manifest stays as the round's start wrote
    it, ``running`` still marking the round, and the draft directory holds what
    it held. The result says the round is not recorded, and why the files were
    not saved."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    (workspace / "run.py").write_text("print('second round')\n", encoding="utf-8")
    manifest_file = _draft_dir(package_root) / tool_drafts.MANIFEST_FILENAME
    before = manifest_file.read_bytes()
    entries = sorted(os.listdir(_draft_dir(package_root)))
    max_files = 2000

    def refused(*args: Any) -> Any:
        raise OSError("refused")

    if fails_at == "limit":
        max_files = 1
        expected = tool_drafts.REASON_TOO_MANY_FILES
    elif fails_at == "scan":
        monkeypatch.setattr(
            tools, "scan_embedded_secrets", lambda *args: tools.SecretScanFailed("OSError")
        )
        expected = tool_drafts.REASON_SCAN_FAILED.format(reason="OSError")
    elif fails_at == "plan":
        monkeypatch.setattr(tool_drafts, "_plan_copy", refused)
        expected = tool_drafts.REASON_UNREADABLE
    elif fails_at == "copy":
        monkeypatch.setattr(tool_drafts, "_carry_out", refused)
        expected = tool_drafts.REASON_COPY.format(kind="OSError")
    else:
        real_write = tool_drafts._write_manifest
        writes: list[dict[str, Any]] = []

        def refused_once(draft_dir: Path, doc: dict[str, Any], known: list[str]) -> Any:
            writes.append(doc)
            if len(writes) == 1:
                return tool_drafts._ManifestWrite(replaced=False)
            return real_write(draft_dir, doc, known)

        monkeypatch.setattr(tool_drafts, "_write_manifest", refused_once)
        expected = tool_drafts.REASON_MANIFEST

    result = _save(
        package_root,
        workspace,
        copied_in,
        previous=previous,
        draft_round=tool_drafts.DraftRound(finished_at=_now(), job_id=_JOB),
        max_files=max_files,
    )

    assert (result.worth_saving, result.saved, result.recorded, result.reason) == (
        True,
        False,
        False,
        expected,
    )
    assert manifest_file.read_bytes() == before
    assert _manifest(package_root)["running"]["job_id"] == _JOB
    assert sorted(os.listdir(_draft_dir(package_root))) == entries


def test_a_continuing_save_writes_nothing_into_a_draft_changed_meanwhile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    manifest = _manifest(package_root)
    marked = tool_drafts.mark_publishing(
        package_root,
        tool_drafts.directory_identity(package_root.path),
        (manifest["draft_id"], manifest["generation"]),
        None,
    )
    assert marked == manifest["generation"] + 1

    result = _save(package_root, workspace, copied_in, previous=previous)

    assert (result.saved, result.reason) == (False, tool_drafts.REASON_DRAFT_CHANGED)
    assert _manifest(package_root)["generation"] == marked


# --- discard ------------------------------------------------------------------


def _identity(package_root: tools.PackageRoot) -> tuple[str, int]:
    manifest = _manifest(package_root)
    return manifest["draft_id"], manifest["generation"]


def test_a_discard_naming_an_old_generation_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    draft_id, generation = _identity(package_root)
    assert tool_drafts.mark_publishing(
        package_root,
        tool_drafts.directory_identity(package_root.path),
        (draft_id, generation),
        None,
    )

    assert tool_drafts.discard(package_root, draft_id=draft_id, generation=generation) == "changed"
    assert tool_drafts.has_draft(package_root)


def test_a_draft_made_again_after_a_discard_refuses_the_old_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tab A shows draft 1 at generation 1; tab B discards it and a new round makes
    another draft, at generation 1 too. A's confirmation must not delete it."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    seen = _identity(package_root)
    removed = tool_drafts.discard(package_root, draft_id=seen[0], generation=seen[1])
    assert isinstance(removed, tool_drafts.Removal) and removed.removed
    shutil.rmtree(tmp_path / "work")
    _first_draft(tmp_path, package_root)
    assert _identity(package_root)[1] == seen[1]

    assert tool_drafts.discard(package_root, draft_id=seen[0], generation=seen[1]) == "changed"
    assert tool_drafts.has_draft(package_root)


def test_a_discard_removes_the_draft_and_reports_no_residue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    draft_id, generation = _identity(package_root)

    outcome = tool_drafts.discard(package_root, draft_id=draft_id, generation=generation)

    assert outcome == tool_drafts.Removal(removed=True, residual_path=None)
    assert sorted(p.name for p in (package_root.path / tools._META_DIRNAME).iterdir()) == [
        tools._CURRENT_FILENAME,
        tools._PACKAGE_STATE_FILENAME,
    ]
    assert (
        tool_drafts.discard(package_root, draft_id=draft_id, generation=generation) == "not_found"
    )


def test_a_discard_that_cannot_remove_everything_names_what_is_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    draft_id, generation = _identity(package_root)
    with monkeypatch.context() as patch:
        patch.setattr(tool_drafts, "remove_entry", lambda path: None)
        outcome = tool_drafts.discard(package_root, draft_id=draft_id, generation=generation)

    assert isinstance(outcome, tool_drafts.Removal)
    assert outcome.removed
    assert not tool_drafts.has_draft(package_root)
    assert outcome.residual_path is not None
    assert outcome.residual_path.parent == _draft_dir(package_root).parent
    assert tool_drafts._DISCARDED_RE.fullmatch(outcome.residual_path.name)
    _sweep(package_root)
    assert not os.path.lexists(outcome.residual_path)


def _draft_out_of_sight(
    patch: pytest.MonkeyPatch, draft: Path, *, once_renamed: bool = False
) -> None:
    """``draft/`` cannot be looked at or renamed, as when a directory above it
    denies search: its ``lstat`` and a rename of it raise ``PermissionError`` --
    the ``lstat`` only from the first rename of it on, with ``once_renamed``."""
    real_lstat, real_rename = os.lstat, os.rename
    hidden = [not once_renamed]

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if hidden[0] and os.fspath(path) == os.fspath(draft):
            raise PermissionError(errno.EACCES, "Permission denied", os.fspath(path))
        return real_lstat(path, *args, **kwargs)

    def rename(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        if os.fspath(source) == os.fspath(draft):
            hidden[0] = True
            raise PermissionError(errno.EACCES, "Permission denied", os.fspath(source))
        real_rename(source, destination, *args, **kwargs)

    patch.setattr(os, "lstat", lstat)
    patch.setattr(os, "rename", rename)


def test_a_discard_that_cannot_see_the_draft_gone_does_not_report_it_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``draft/`` cannot be looked at: nothing shows that it is gone, so the
    discard does not report it removed, and it names ``draft/`` itself as what is
    left -- which is still there."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    draft = _draft_dir(package_root)
    with monkeypatch.context() as patch:
        _draft_out_of_sight(patch, draft)
        token = tool_drafts.discard_token(draft)
        outcome = tool_drafts.discard(package_root, token=token)

    assert outcome == tool_drafts.Removal(removed=False, residual_path=draft)
    assert tool_drafts.has_draft(package_root)


def test_a_removal_after_a_publish_that_cannot_see_the_draft_gone_does_not_report_it_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The draft a publish came from is there and matches, but its rename is
    refused and from then on ``draft/`` cannot be looked at: the removal does not
    report it removed -- the revise then asks the operator to discard it -- and it
    names ``draft/`` as what is left."""
    package_root = _package(monkeypatch, tmp_path)
    _workspace_used, saved = _first_save(tmp_path, package_root)
    draft = _draft_dir(package_root)
    seen = _identity(package_root)
    identity = tool_drafts.directory_identity(package_root.path)
    with monkeypatch.context() as patch:
        _draft_out_of_sight(patch, draft, once_renamed=True)
        removal = tool_drafts.remove_after_publish(package_root, identity, seen, saved.copy)

    assert removal == tool_drafts.Removal(removed=False, residual_path=draft)
    assert tool_drafts.has_draft(package_root)


def test_a_removal_after_a_publish_that_cannot_list_the_draft_leaves_it_unconfirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The copy is as the save made it, but ``draft/`` itself cannot be listed, so
    whether it holds anything besides the copy and the manifest is not known:
    nothing is removed, and the removal says it could not confirm -- not that it
    found a change, nor that it failed. Before the fix the removal took no copy to
    compare and listed nothing; this is red for one that, when the listing fails,
    goes on as if ``draft/`` held only the copy and the manifest."""
    package_root = _package(monkeypatch, tmp_path)
    _workspace_used, saved = _first_save(tmp_path, package_root)
    draft = _draft_dir(package_root)
    seen = _identity(package_root)
    identity = tool_drafts.directory_identity(package_root.path)
    with monkeypatch.context() as patch:
        failed = _refuse(patch, "listdir", lambda path: path == os.fspath(draft), errno.EACCES)
        removal = tool_drafts.remove_after_publish(package_root, identity, seen, saved.copy)

    assert tool_drafts.has_draft(package_root)
    assert failed
    assert removal == tool_drafts.Removal(False, None, kept="unconfirmed")


def test_an_unusable_draft_is_discarded_by_its_token_until_it_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    manifest_path = _draft_dir(package_root) / tool_drafts.MANIFEST_FILENAME
    manifest_path.write_text("{not json", encoding="utf-8")
    token = tool_drafts.discard_token(_draft_dir(package_root))
    assert token == tool_drafts.discard_token(_draft_dir(package_root))

    # Rewritten since the token was taken: a different file.
    replacement = manifest_path.with_name("replacement")
    replacement.write_text("{still not json", encoding="utf-8")
    os.replace(replacement, manifest_path)
    assert tool_drafts.discard(package_root, token=token) == "changed"

    fresh = tool_drafts.discard_token(_draft_dir(package_root))
    outcome = tool_drafts.discard(package_root, token=fresh)
    assert isinstance(outcome, tool_drafts.Removal) and outcome.removed


def test_a_discard_token_is_there_for_a_draft_with_no_manifest(tmp_path: Path) -> None:
    draft = tmp_path / "draft"
    draft.mkdir()
    token = tool_drafts.discard_token(draft)
    assert len(token) == 64
    (draft / tool_drafts.MANIFEST_FILENAME).write_text("{}", encoding="utf-8")
    assert tool_drafts.discard_token(draft) != token


def test_a_draft_without_a_manifest_made_again_on_its_inode_refuses_the_old_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``draft/`` whose manifest is not there holds ``files-<a>``; it is
    discarded, and a ``draft/`` like it holding ``files-<b>`` is made where it
    was, with the inode the first one had -- a filesystem may hand a freed inode
    number to the next directory. The token taken of the first does not match the
    second, and a discard naming the first by it is refused, leaving the second."""
    package_root = _package(monkeypatch, tmp_path)
    draft = _draft_dir(package_root)
    draft.mkdir()
    (draft / ("files-" + "a" * 32)).mkdir()
    first = os.lstat(draft)
    old = tool_drafts.discard_token(draft)
    removed = tool_drafts.discard(package_root, token=old)
    assert isinstance(removed, tool_drafts.Removal) and removed.removed
    draft.mkdir()
    (draft / ("files-" + "b" * 32)).mkdir()
    real_lstat = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        info = real_lstat(path, *args, **kwargs)
        if os.fspath(path) != os.fspath(draft):
            return info
        return os.stat_result((info.st_mode, first.st_ino, first.st_dev, *list(info)[3:]))

    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", lstat)
        assert (os.lstat(draft).st_dev, os.lstat(draft).st_ino) == (first.st_dev, first.st_ino)
        assert tool_drafts.discard_token(draft) != old
        assert tool_drafts.discard(package_root, token=old) == "changed"

    assert (draft / ("files-" + "b" * 32)).is_dir()


@pytest.mark.parametrize("unseen", ["manifest", "listing", "entry"])
def test_a_discard_by_a_token_of_a_draft_not_seen_whole_removes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unseen: str
) -> None:
    """``draft/`` holds a copy and no manifest; its manifest, its listing or an
    entry in it cannot be looked at. A token taken then is only for display: two
    looks that both fail say nothing about whether the draft is the same one. A
    discard by it removes nothing and names ``draft/`` as what is left."""
    package_root = _package(monkeypatch, tmp_path)
    draft = _draft_dir(package_root)
    draft.mkdir()
    copy = draft / ("files-" + "a" * 32)
    copy.mkdir()
    name, unseen_path = {
        "manifest": ("lstat", draft / tool_drafts.MANIFEST_FILENAME),
        "listing": ("scandir", draft),
        "entry": ("lstat", copy),
    }[unseen]
    with monkeypatch.context() as patch:
        refused = _refuse(patch, name, lambda path: path == os.fspath(unseen_path), errno.EIO)
        shown = tool_drafts.discard_token(draft)
        outcome = tool_drafts.discard(package_root, token=shown)

    assert refused
    assert outcome == tool_drafts.Removal(removed=False, residual_path=draft)
    assert copy.is_dir()


def test_a_draft_that_is_a_file_is_discarded_by_its_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``draft`` is a regular file: its manifest and its entries are seen not to
    be there (``ENOTDIR``), a definite answer, so its token stands for it and a
    discard by it removes it."""
    package_root = _package(monkeypatch, tmp_path)
    draft = _draft_dir(package_root)
    draft.write_text("not a directory\n", encoding="utf-8")
    token = tool_drafts.discard_token(draft)

    assert tool_drafts.discard(package_root, token=token) == tool_drafts.Removal(
        removed=True, residual_path=None
    )
    assert not os.path.lexists(draft)


@pytest.mark.parametrize("code", [errno.EIO, errno.EACCES], ids=["EIO", "EACCES"])
def test_a_discard_that_cannot_see_the_tools_directory_does_not_answer_there_is_no_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int
) -> None:
    """The shared lock is not taken because the tools directory cannot be looked
    at: resolving its path raises (``tools.ToolsDirUnresolvedError``). Nothing
    shows that the draft is not there, so the answer is that it may still be --
    ``draft/`` as what is left -- not that there is none."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    draft = _draft_dir(package_root)
    token = tool_drafts.discard_token(draft)
    base = os.fspath(package_root.path.parent)
    with monkeypatch.context() as patch:
        for name in ("stat", "lstat"):
            _refuse(patch, name, lambda path: path == base, code)
        with pytest.raises(tools.ToolsDirUnresolvedError):
            tools.acquire_shared_tools_lock()
        outcome = tool_drafts.discard(package_root, token=token)

    assert outcome == tool_drafts.Removal(removed=False, residual_path=draft)
    assert tool_drafts.has_draft(package_root)


def test_a_discard_whose_second_look_cannot_resolve_the_tools_dir_says_the_draft_may_be_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No lock was taken -- the tools directory was not seen to be a directory --
    and the look that tells "gone" from "not seen" cannot resolve its path.
    Nothing shows that the draft is not there, so the answer is that it may still
    be, not a failure out of the discard. Red before the fix: the second look's
    failure escaped the discard as ``PermissionError``."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    draft = _draft_dir(package_root)
    token = tool_drafts.discard_token(draft)

    def unresolvable(path: Any, *args: Any, **kwargs: Any) -> str:
        raise PermissionError(errno.EACCES, "Permission denied", os.fspath(path))

    with monkeypatch.context() as patch:
        patch.setattr(tools, "acquire_shared_tools_lock", lambda: None)
        patch.setattr(os.path, "realpath", unresolvable)
        outcome = tool_drafts.discard(package_root, token=token)

    assert outcome == tool_drafts.Removal(removed=False, residual_path=draft)
    assert tool_drafts.has_draft(package_root)


@pytest.mark.parametrize("gone", ["removed", "under-a-file"])
def test_a_discard_whose_tools_directory_is_seen_not_to_be_there_answers_there_is_no_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gone: str
) -> None:
    """The shared lock is not taken because the tools directory is seen not to
    exist -- removed (``ENOENT``), or under a path component that is a regular
    file (``ENOTDIR``, its path kept as configured): there is no draft to
    discard."""
    if gone == "removed":
        package_root = _package(monkeypatch, tmp_path)
        _first_draft(tmp_path, package_root)
        token = tool_drafts.discard_token(_draft_dir(package_root))
        shutil.rmtree(package_root.path.parent)
        assert tools.acquire_shared_tools_lock() is None
    else:
        (tmp_path / "plain").write_text("a file\n", encoding="utf-8")
        base = tmp_path / "plain" / "tools"
        _settings(monkeypatch, base)
        package_root = tools.PackageRoot(base / "kb")
        token = "0" * 64
        assert tools.tools_dir() == base
        assert tools.acquire_shared_tools_lock() is None

    assert tool_drafts.discard(package_root, token=token) == "not_found"


def test_a_draft_with_a_manifest_keeps_its_token_while_the_sweep_takes_its_leftovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sweep collects a half-made copy and leaves a copy the manifest does not
    name; the draft the operator saw has not changed, and its token still matches."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    draft = _draft_dir(package_root)
    _plant(draft, "files-" + "e" * 32)
    _plant(draft, ".incoming-" + "f" * 32)
    token = tool_drafts.discard_token(draft)

    _sweep(package_root)

    assert sorted(p.name for p in draft.iterdir()) == sorted(
        [tool_drafts.MANIFEST_FILENAME, _manifest(package_root)["files_dir"], "files-" + "e" * 32]
    )
    assert tool_drafts.discard_token(draft) == token


# --- the state a listing shows --------------------------------------------------


def _view(
    package_root: tools.PackageRoot,
    *,
    active: frozenset[str] = frozenset(),
    known: list[str] | None = None,
) -> dict[str, Any] | None:
    return tool_drafts.draft_view(
        package_root,
        tools.resolve_current(package_root),
        job_active=lambda job_id: job_id in active,
        known=lambda: known if known is not None else tools._redaction_secrets(),
    )


def test_no_draft_lists_as_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_root = _package(monkeypatch, tmp_path)
    assert _view(package_root) is None


def test_a_saved_draft_lists_open_with_its_last_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)

    view = _view(package_root)

    assert view is not None
    assert view["state"] == "open"
    assert (view["attempts"], view["generation"], view["base_vid"]) == (1, 1, _VID)
    assert view["path"] == str(_files(package_root))
    assert view["last"]["summary"] == "改到一半"
    assert view["last"]["outcome"] == "not_published"
    assert view["discard_token"] == tool_drafts.discard_token(_draft_dir(package_root))


def test_text_masked_on_the_way_in_is_masked_again_on_the_way_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A value registered only after the save is masked when the draft is listed."""
    package_root = _package(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    late = "registered-after-the-save"
    noted = tool_drafts.DraftRound(
        finished_at=_now(), outcome="not_published", summary=f"tried {late}", operator_action=late
    )
    assert _save(package_root, workspace, copied_in, draft_round=noted).saved
    assert late in (_draft_dir(package_root) / tool_drafts.MANIFEST_FILENAME).read_text(
        encoding="utf-8"
    )

    tools.register_inflight_secret(late)
    view = _view(package_root)

    assert view is not None
    assert late not in json.dumps(view, ensure_ascii=False)
    assert tools._REDACTION_MARKER in view["last"]["summary"]


def test_a_listing_whose_secret_scan_fails_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)

    def broken() -> list[str]:
        raise OSError("tools directory unreadable")

    with pytest.raises(OSError):
        tool_drafts.draft_view(
            package_root,
            tools.resolve_current(package_root),
            job_active=lambda _id: False,
            known=broken,
        )


def test_a_draft_whose_manifest_cannot_be_read_is_unusable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    (_draft_dir(package_root) / tool_drafts.MANIFEST_FILENAME).write_text("[]", encoding="utf-8")

    view = _view(package_root)

    assert view is not None
    assert (view["state"], view["draft_id"], view["last"]) == ("unusable", None, None)
    assert view["path"] == str(_draft_dir(package_root))
    assert files.is_dir()


def test_a_draft_whose_files_are_gone_is_unusable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    shutil.rmtree(_files(package_root))

    view = _view(package_root)

    assert view is not None and view["state"] == "unusable"


def test_a_draft_whose_base_is_no_longer_current_is_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    shutil.copytree(_version(package_root), _version(package_root, _OTHER_VID))
    (package_root.path / tools._META_DIRNAME / tools._CURRENT_FILENAME).write_text(
        f"{_OTHER_VID}\n", encoding="ascii"
    )

    view = _view(package_root)

    assert view is not None and view["state"] == "stale"


def _rewrite(package_root: tools.PackageRoot, **changes: Any) -> None:
    manifest = {**_manifest(package_root), **changes}
    data = tool_drafts.manifest_bytes(manifest, [])
    assert data is not None
    (_draft_dir(package_root) / tool_drafts.MANIFEST_FILENAME).write_bytes(data)


def test_publishing_states_follow_the_job_and_the_current_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A publish marked on the draft: in progress while its job runs here; once it
    does not, published if ``current``'s origin names this draft as its source and
    the base as its predecessor, and unknown otherwise."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    draft_id = _manifest(package_root)["draft_id"]
    job = "c" * 32
    _rewrite(package_root, publishing={"job_id": job, "at": _now()})

    view = _view(package_root, active=frozenset({job}))
    assert view is not None and view["state"] == "publishing"
    view = _view(package_root)
    assert view is not None and view["state"] == "unknown"

    published = _version(package_root, _OTHER_VID)
    shutil.copytree(_version(package_root), published)
    origin_file = published / tools._META_DIRNAME / tools._ORIGIN_FILENAME

    def write_origin(previous: str) -> None:
        origin_file.write_text(
            json.dumps(
                {
                    "source": "builder-revise",
                    "openapi_url": None,
                    "instructions": None,
                    "feedback": "加上分頁\n\n" + tool_drafts.provenance_line(draft_id, 1),
                    "previous": previous,
                }
            ),
            encoding="utf-8",
        )

    (package_root.path / tools._META_DIRNAME / tools._CURRENT_FILENAME).write_text(
        f"{_OTHER_VID}\n", encoding="ascii"
    )
    write_origin(previous=_OTHER_VID)  # names the draft, but not the base as its predecessor
    view = _view(package_root)
    assert view is not None and view["state"] == "unknown"
    write_origin(previous=_VID)
    view = _view(package_root)
    assert view is not None and view["state"] == "published"


def test_a_round_marked_running_is_running_while_its_job_is_and_interrupted_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    job = "d" * 32
    _rewrite(package_root, running={"job_id": job, "started_at": _now()})

    view = _view(package_root, active=frozenset({job}))
    assert view is not None and (view["state"], view["interrupted"]) == ("running", False)
    view = _view(package_root)
    assert view is not None and (view["state"], view["interrupted"]) == ("open", True)


def test_the_list_schema_admits_every_draft_state_and_round_outcome() -> None:
    """``ToolDraft`` and ``ToolDraftLast`` spell this module's ``DraftState`` and
    ``RoundOutcome`` out rather than importing them; a value the store reports
    but the schema rejects would fail the tools list."""
    states = set(get_args(tool_drafts.DraftState))
    outcomes = set(get_args(tool_drafts.RoundOutcome))
    assert states and outcomes
    assert set(get_args(ToolDraft.model_fields["state"].annotation)) == states
    [admitted, none] = get_args(ToolDraftLast.model_fields["outcome"].annotation)
    assert none is type(None)
    assert set(get_args(admitted)) == outcomes


# --- the sweep ------------------------------------------------------------------


def _sweep(package_root: tools.PackageRoot) -> None:
    tool_drafts.sweep_package(package_root.path, fsync_directory=tool_builder._fsync_directory)


def _plant(directory: Path, name: str) -> Path:
    path = directory / name
    path.mkdir()
    (path / "f.txt").write_text("x\n", encoding="utf-8")
    return path


def test_the_sweep_takes_only_half_made_copies_and_discard_leftovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A half-made ``.incoming-<hex>`` and what a discard left are taken; a
    ``files-<hex>`` the manifest does not name stays -- not being named is no proof
    it may go -- and so do names this module never makes and a link."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    draft = _draft_dir(package_root)
    meta = draft.parent
    unnamed = _plant(draft, "files-" + "e" * 32)
    half_made = _plant(draft, ".incoming-" + "f" * 32)
    unknown = _plant(draft, "operator-notes")
    leftover = _plant(meta, "draft.discarded-" + "1" * 32)
    not_ours = _plant(meta, "draft.discarded-by-hand")
    linked = draft / ("files-" + "2" * 32)
    os.symlink(unknown, linked)

    _sweep(package_root)

    assert not half_made.exists() and not leftover.exists()
    assert unnamed.is_dir() and _files(package_root).is_dir()
    assert unknown.is_dir() and not_ours.is_dir()
    assert os.path.islink(linked)


def test_the_sweep_leaves_a_draft_alone_whose_manifest_cannot_be_trusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken manifest beside two files directories: either may be the work, so
    the sweep takes neither."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    draft = _draft_dir(package_root)
    named = _files(package_root)
    other = _plant(draft, "files-" + "e" * 32)
    half_made = _plant(draft, ".incoming-" + "f" * 32)
    (draft / tool_drafts.MANIFEST_FILENAME).write_text("{broken", encoding="utf-8")

    _sweep(package_root)

    assert named.is_dir() and other.is_dir() and half_made.is_dir()


def test_the_sweep_takes_nothing_from_draft_when_the_manifest_fsync_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    other = _plant(_draft_dir(package_root), "files-" + "e" * 32)
    monkeypatch.setattr(tool_drafts, "_fsync_file", lambda path: False)

    _sweep(package_root)

    assert other.is_dir()


def test_the_sweep_never_follows_a_draft_directory_that_is_a_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``draft/`` is a link to a directory holding a whole draft and an unnamed
    copy: nothing behind it is collected. The fsync handed in here would follow
    the link, so only the sweep's own check stands in the way."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    elsewhere = tmp_path / "elsewhere"
    shutil.move(_draft_dir(package_root), elsewhere)
    _plant(elsewhere, "files-" + "e" * 32)
    os.symlink(elsewhere, _draft_dir(package_root))

    tool_drafts.sweep_package(package_root.path, fsync_directory=lambda path: True)

    assert (elsewhere / ("files-" + "e" * 32)).is_dir()


# --- has_draft ------------------------------------------------------------------


def test_a_draft_directory_without_a_manifest_still_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = _package(monkeypatch, tmp_path)
    assert not tool_drafts.has_draft(package_root)
    _plant(_draft_dir(package_root).parent, tool_drafts.DRAFT_DIRNAME)
    assert tool_drafts.has_draft(package_root)


# --- whose draft ----------------------------------------------------------------


@pytest.mark.parametrize("alias", ["metadata-directory", "package-directory"])
def test_through_a_link_no_draft_is_listed_counted_saved_marked_taken_away_or_swept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alias: str
) -> None:
    """Tool B has a draft; tool A's ``.afterthread.meta``, or A's whole directory,
    is a link to B's. A has no draft of its own: ``draft_view`` lists none and
    ``has_draft`` counts none; ``save_round`` into A, a first round or one
    continuing B's draft, writes nothing; ``discard`` of A -- by the token or the
    id of B's draft -- finds nothing; ``mark_publishing``, ``clear_publishing``,
    ``remove_after_publish`` and ``sweep_package`` do not reach through A. B's
    draft is left exactly as it was."""
    package_b = _package(monkeypatch, tmp_path)
    _workspace_b, saved_b = _first_save(tmp_path, package_b)
    linked = package_b.path.parent / "kc"
    if alias == "metadata-directory":
        _make_package(package_b.path.parent, name="kc")
        shutil.rmtree(linked / tools._META_DIRNAME)
        os.symlink(package_b.path / tools._META_DIRNAME, linked / tools._META_DIRNAME)
    else:
        os.symlink(package_b.path, linked)
    package_a = tools.PackageRoot(linked)
    identity_a = tool_drafts.directory_identity(linked)
    first_workspace = tmp_path / "work-a" / "build"
    shutil.copytree(
        _version(package_b),
        first_workspace,
        ignore=lambda directory, names: (
            {tools._META_DIRNAME} if Path(directory) == _version(package_b) else set()
        ),
    )
    first_in = tool_drafts.tree_digest(first_workspace)
    (first_workspace / "run.py").write_text("print('through a link')\n", encoding="utf-8")
    # B's own round, begun on B's draft: what is compared below is B's draft from here.
    continuing, previous, continuing_in = _continuing(tmp_path, package_b)
    (continuing / "run.py").write_text("print('through a link')\n", encoding="utf-8")
    seen = _identity(package_b)
    token = tool_drafts.discard_token(_draft_dir(package_b))
    before = tool_drafts.tree_digest(_draft_dir(package_b))

    assert _view(package_a) is None
    assert not tool_drafts.has_draft(package_a)
    gone = (False, tool_drafts.REASON_PACKAGE_GONE)
    first = _save(package_a, first_workspace, first_in, identity=identity_a)
    assert (first.saved, first.reason) == gone
    later = _save(
        package_a,
        continuing,
        continuing_in,
        previous=previous,
        earlier_root=_files(package_b),
        identity=identity_a,
    )
    assert (later.saved, later.reason) == gone
    assert tool_drafts.discard(package_a, token=token) == "not_found"
    assert tool_drafts.discard(package_a, draft_id=seen[0], generation=seen[1]) == "not_found"
    assert tool_drafts.mark_publishing(package_a, identity_a, seen, None) is None
    assert tool_drafts.clear_publishing(package_a, identity_a, seen, "failed") is None
    # With B's own copy as what was saved: reaching B's draft through A would take it.
    assert tool_drafts.remove_after_publish(
        package_a, identity_a, seen, saved_b.copy
    ) == tool_drafts.Removal(False, None, kept="unconfirmed")
    _plant(_draft_dir(package_b), "files-" + "e" * 32)
    tool_drafts.sweep_package(linked, fsync_directory=tool_builder._fsync_directory)

    assert (_draft_dir(package_b) / ("files-" + "e" * 32)).is_dir()
    shutil.rmtree(_draft_dir(package_b) / ("files-" + "e" * 32))
    assert tool_drafts.same_tree(before, tool_drafts.tree_digest(_draft_dir(package_b)))
    assert _identity(package_b) == seen


@pytest.mark.parametrize("named_by", ["draft-id", "token"])
def test_a_discard_that_cannot_see_whether_the_metadata_directory_is_a_link_leaves_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, named_by: str
) -> None:
    """Tool A's ``.afterthread.meta`` is a link to tool B's, and its own ``lstat``
    fails with EIO, so nothing shows whether it is a link. A discard of A naming
    B's draft -- by its id or its token -- leaves B's draft exactly as it was, and
    does not report a removal: it names A's ``draft/`` path as what may still be
    there."""
    package_b = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_b)
    linked = _make_package(package_b.path.parent, name="kc")
    shutil.rmtree(linked / tools._META_DIRNAME)
    os.symlink(package_b.path / tools._META_DIRNAME, linked / tools._META_DIRNAME)
    package_a = tools.PackageRoot(linked)
    seen = _identity(package_b)
    token = tool_drafts.discard_token(_draft_dir(package_b))
    before = tool_drafts.tree_digest(_draft_dir(package_b))
    unseen = os.fspath(linked / tools._META_DIRNAME)
    real_lstat = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if os.fspath(path) == unseen:
            raise OSError(errno.EIO, "Input/output error", unseen)
        return real_lstat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", lstat)
        if named_by == "token":
            outcome = tool_drafts.discard(package_a, token=token)
        else:
            outcome = tool_drafts.discard(package_a, draft_id=seen[0], generation=seen[1])

    assert outcome == tool_drafts.Removal(removed=False, residual_path=_draft_dir(package_a))
    assert tool_drafts.same_tree(before, tool_drafts.tree_digest(_draft_dir(package_b)))
    assert _identity(package_b) == seen


# --- continuing a draft: its mark, its copy into a workspace, the previous files ---


def _first_round_with(
    tmp_path: Path, package_root: tools.PackageRoot, change: Callable[[Path], None]
) -> None:
    """Save one first round whose workspace ``change`` made what it is."""
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    change(workspace)
    assert _save(package_root, workspace, copied_in).saved


def _kept_note(old: Path, why: str) -> str:
    return tool_drafts.NOTE_PREVIOUS_KEPT.format(path=old, why=why)


def test_a_continuation_keeps_modes_and_its_previous_files_go_once_all_three_are_seen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The manifest naming the new copy is on disk, the workspace received the
    previous copy whole -- an executable kept executable, so the two digests
    agree -- and nobody changed it during the round: it is removed. Its name stays
    in ``kept`` until a later write sees it gone, and the list shows none."""
    package_root = _package(monkeypatch, tmp_path)

    def change(workspace: Path) -> None:
        (workspace / "run.py").write_text("print('first round')\n", encoding="utf-8")
        script = workspace / "run.sh"
        script.write_text("#!/bin/sh\nexec python3 run.py\n", encoding="utf-8")
        script.chmod(0o755)

    _first_round_with(tmp_path, package_root, change)
    old = _files(package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    assert stat.S_IMODE((workspace / "run.sh").stat().st_mode) == 0o755
    (workspace / "run.py").write_text("print('second round')\n", encoding="utf-8")

    result = _save(package_root, workspace, copied_in, previous=previous)

    assert (result.saved, result.reason, result.durable) == (True, None, True)
    assert not old.exists()
    assert _manifest(package_root)["kept"] == [old.name]
    view = _view(package_root)
    assert view is not None and view["kept"] == []
    assert stat.S_IMODE((_files(package_root) / "run.sh").stat().st_mode) == 0o755


def test_the_previous_files_stay_when_the_manifest_naming_the_new_ones_is_not_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Received whole and unchanged, but the fsync after the manifest's replace
    failed: a crash could bring back the manifest naming the previous copy. It
    stays, named in ``kept``, and the save -- still saved -- says where and why."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    old = _files(package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    assert tool_drafts.same_tree(previous.copied_out, copied_in)
    (workspace / "run.py").write_text("print('second round')\n", encoding="utf-8")
    real_write = tools._write_package_file_atomic

    def not_durable(
        directory: Path, filename: str, data: bytes, expected: Any, **kwargs: Any
    ) -> bool:
        out = kwargs.pop("durability_out", None)
        published = real_write(directory, filename, data, expected, **kwargs)
        if out is not None:
            out.append(False)
        return published

    monkeypatch.setattr(tools, "_write_package_file_atomic", not_durable)

    result = _save(package_root, workspace, copied_in, previous=previous)

    assert (result.saved, result.recorded, result.durable) == (True, True, False)
    assert result.reason == _kept_note(old, tool_drafts.KEPT_NOT_DURABLE)
    assert old.is_dir()
    assert _manifest(package_root)["kept"] == [old.name]


def test_the_previous_files_stay_when_the_workspace_did_not_receive_all_of_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The operator put a ``.env`` into the draft's files; it is not brought into
    the workspace, so what the round began with is not the previous copy. That copy
    is unchanged and the manifest is on disk, and it still stays -- with the
    ``.env`` in it."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    old = _files(package_root)
    (old / ".env").write_text("HAND_PLACED=value-of-the-operator\n", encoding="utf-8")
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    assert not (workspace / ".env").exists()
    (workspace / "run.py").write_text("print('second round')\n", encoding="utf-8")

    result = _save(package_root, workspace, copied_in, previous=previous)

    assert (result.saved, result.durable) == (True, True)
    assert result.reason == _kept_note(old, tool_drafts.KEPT_NOT_ALL_BROUGHT_IN)
    assert (old / ".env").read_text(encoding="utf-8") == "HAND_PLACED=value-of-the-operator\n"
    assert _manifest(package_root)["kept"] == [old.name]


@pytest.mark.parametrize("afterwards", ["swapped-back", "left-changed"])
def test_a_draft_file_changed_while_it_is_copied_in_keeps_the_previous_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, afterwards: str
) -> None:
    """A draft file changes after ``copied_out`` is taken and before it is copied,
    so the workspace receives the changed bytes. Swapped back once copied, the
    previous copy is as it was at the start and only the comparison of what was
    copied out with what came in shows the difference; left changed, the
    workspace and the previous copy agree with each other but not with what the
    round began on. Either way the previous copy stays."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    old = _files(package_root)
    original = (old / "run.py").read_bytes()
    begun = _begin(package_root)
    assert isinstance(begun, dict)
    real_carry_out = tool_drafts._carry_out

    def swapping(plan: Any, incoming: Path, known: list[str]) -> Any:
        (old / "run.py").write_bytes(b"print('swapped in')\n")
        try:
            return real_carry_out(plan, incoming, known)
        finally:
            if afterwards == "swapped-back":
                (old / "run.py").write_bytes(original)

    monkeypatch.setattr(tool_drafts, "_carry_out", swapping)
    workspace = tmp_path / "work2" / "build"
    copied = _copy_in(package_root, begun, workspace)
    monkeypatch.setattr(tool_drafts, "_carry_out", real_carry_out)
    assert isinstance(copied, tool_drafts.CopiedIn)
    copied_in = tool_drafts.tree_digest(workspace)
    assert (workspace / "run.py").read_bytes() == b"print('swapped in')\n"
    (workspace / "more.py").write_text("print('more')\n", encoding="utf-8")

    result = _save(
        package_root,
        workspace,
        copied_in,
        previous=tool_drafts.PreviousDraft(manifest=begun, copied_out=copied.copied_out),
    )

    assert (result.saved, result.durable) == (True, True)
    assert result.reason == _kept_note(old, tool_drafts.KEPT_NOT_ALL_BROUGHT_IN)
    assert old.is_dir()
    assert _manifest(package_root)["kept"] == [old.name]


def test_the_previous_files_changed_during_the_round_stay_and_are_listed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Received whole, the manifest on disk, but the operator edited the previous
    copy while the round ran: it stays, the save says so, and the list shows it."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    old = _files(package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    assert tool_drafts.same_tree(previous.copied_out, copied_in)
    (workspace / "run.py").write_text("print('second round')\n", encoding="utf-8")
    (old / "run.py").write_text("print('edited by hand')\n", encoding="utf-8")

    result = _save(package_root, workspace, copied_in, previous=previous)

    assert (result.saved, result.durable) == (True, True)
    assert result.reason == _kept_note(old, tool_drafts.KEPT_CHANGED)
    assert (old / "run.py").read_text(encoding="utf-8") == "print('edited by hand')\n"
    view = _view(package_root)
    assert view is not None and view["kept"] == [str(old)]


def test_a_round_that_saves_no_new_files_leaves_the_files_and_the_kept_copies_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A draft whose earlier continuation left a copy in ``kept``. A round that
    changed nothing records itself only, and a round whose save fails records its
    failure: neither touches the files directory or what ``kept`` holds."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    old = _files(package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    (workspace / "run.py").write_text("print('second round')\n", encoding="utf-8")
    (old / "by_hand.txt").write_text("mine\n", encoding="utf-8")
    assert _save(package_root, workspace, copied_in, previous=previous).saved
    files = _files(package_root)
    before = (tool_drafts.tree_digest(old), tool_drafts.tree_digest(files))

    for limit in (2000, 1):
        workspace, previous, copied_in = _continuing(tmp_path / f"limit-{limit}", package_root)
        result = _save(package_root, workspace, copied_in, previous=previous, max_files=limit)
        manifest = _manifest(package_root)
        assert result.saved is (limit == 2000)
        assert result.recorded is True
        assert (manifest["files_dir"], manifest["kept"]) == (files.name, [old.name])
        assert tool_drafts.same_tree(before[0], tool_drafts.tree_digest(old))
        assert tool_drafts.same_tree(before[1], tool_drafts.tree_digest(files))


def test_kept_names_go_only_once_seen_gone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``kept`` names one copy removed by hand and one whose look fails. A round
    beginning drops the first and keeps the second -- a failed look says nothing
    about it -- the list shows the second, and the save that commits new files
    keeps it too, adding the previous copy."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    draft = _draft_dir(package_root)
    gone = "files-" + "1" * 32
    unseen = _plant(draft, "files-" + "2" * 32)
    _rewrite(package_root, kept=[gone, unseen.name])
    # Every look at it fails, as under a directory that denies search.
    failed = _refuse(monkeypatch, "lstat", lambda path: path == os.fspath(unseen), errno.EACCES)
    _refuse(monkeypatch, "stat", lambda path: path == os.fspath(unseen), errno.EACCES)

    begun = _begin(package_root)

    assert isinstance(begun, dict)
    assert begun["kept"] == [unseen.name]
    assert _manifest(package_root)["kept"] == [unseen.name]
    view = _view(package_root)
    assert view is not None and view["kept"] == [str(unseen)]
    assert failed
    old = _files(package_root)
    workspace = tmp_path / "work2" / "build"
    copied = _copy_in(package_root, begun, workspace)
    assert isinstance(copied, tool_drafts.CopiedIn)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "run.py").write_text("print('second round')\n", encoding="utf-8")
    previous = tool_drafts.PreviousDraft(manifest=begun, copied_out=copied.copied_out)
    assert _save(package_root, workspace, copied_in, previous=previous).saved
    assert _manifest(package_root)["kept"] == [unseen.name, old.name]
    assert _files(package_root) != old


@pytest.mark.parametrize("one_seen_gone", [False, True])
def test_a_round_does_not_begin_while_kept_is_full(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, one_seen_gone: bool
) -> None:
    """``kept`` at its limit, every copy there: the round does not begin and the
    manifest is as it was. With one of them removed by hand, the name seen gone is
    dropped first, and the round begins."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    draft = _draft_dir(package_root)
    names = [f"files-{index:032x}" for index in range(1, tool_drafts._KEPT_MAX + 1)]
    for name in names:
        (draft / name).mkdir()
    _rewrite(package_root, kept=names)
    if one_seen_gone:
        (draft / names[0]).rmdir()
    before = _manifest(package_root)

    begun = _begin(package_root)

    if one_seen_gone:
        assert isinstance(begun, dict)
        assert begun["kept"] == names[1:]
    else:
        assert begun == tool_drafts.START_KEPT_FULL.format(max=tool_drafts._KEPT_MAX)
        assert _manifest(package_root) == before


def test_the_save_takes_the_generation_its_round_began_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``begin_round`` returns the manifest as it wrote it -- one generation on,
    marked as this job's -- and the save matched against it goes through. The
    manifest as judged before the mark is no longer the draft: a save matched
    against it writes nothing."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    judged = _manifest(package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    (workspace / "run.py").write_text("print('second round')\n", encoding="utf-8")

    assert previous.manifest == _manifest(package_root)
    assert previous.manifest["generation"] == judged["generation"] + 1
    assert previous.manifest["running"]["job_id"] == _JOB
    stale = _save(
        package_root,
        workspace,
        copied_in,
        previous=tool_drafts.PreviousDraft(manifest=judged, copied_out=previous.copied_out),
    )
    assert (stale.saved, stale.recorded, stale.reason) == (
        False,
        False,
        tool_drafts.REASON_DRAFT_CHANGED,
    )
    assert _manifest(package_root) == previous.manifest
    assert _save(package_root, workspace, copied_in, previous=previous).saved


def test_a_mark_another_job_set_is_neither_cleared_nor_written_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The draft is marked as another job's round: taking the mark back, recording
    a round and saving one, each as this job, write nothing."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    (workspace / "run.py").write_text("print('second round')\n", encoding="utf-8")
    marked = _manifest(package_root)
    expected = (marked["draft_id"], marked["generation"])
    identity = tool_drafts.directory_identity(package_root.path)
    other = "b" * 32
    other_round = tool_drafts.DraftRound(finished_at=_now(), outcome="failed", job_id=other)

    assert tool_drafts.end_unstarted(package_root, identity, expected, other) is False
    recorded = tool_drafts.record_round(package_root, identity, expected, other_round, "失敗")
    saved = _save(package_root, workspace, copied_in, previous=previous, draft_round=other_round)

    assert (recorded.recorded, saved.recorded, saved.reason) == (
        False,
        False,
        tool_drafts.REASON_DRAFT_CHANGED,
    )
    assert _manifest(package_root) == marked


def test_a_round_that_left_its_mark_is_recorded_interrupted_by_the_next_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A round marked running left no record of its end. The next round to begin
    records it first: its job and start from the mark, ``interrupted``, no end
    time. The manifest reads back, and the list and its schema show it so."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    left = _begin(package_root, job_id="c" * 32)
    assert isinstance(left, dict)

    begun = _begin(package_root)

    assert isinstance(begun, dict)
    entry = begun["attempts"][-1]
    assert (entry["number"], entry["job_id"], entry["started_at"]) == (
        2,
        "c" * 32,
        left["running"]["started_at"],
    )
    assert (entry["outcome"], entry["finished_at"], entry["saved"]) == ("interrupted", None, False)
    assert begun["attempt_count"] == 2
    assert begun["running"]["job_id"] == _JOB
    assert _manifest(package_root) == begun
    view = _view(package_root)
    assert view is not None
    assert (view["last"]["outcome"], view["last"]["finished_at"]) == ("interrupted", None)
    ToolDraft.model_validate(view)


def test_a_manifest_written_before_continuations_reads_back_and_can_be_continued(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A round recorded with no list of paths it was not brought -- as every
    manifest written before continuations is -- reads back with an empty one, is
    listed, and a round can begin on it."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    manifest = _manifest(package_root)
    written = _document(
        draft_id=manifest["draft_id"], files_dir=manifest["files_dir"], generation=4
    )
    assert "not_brought_in" not in written["attempts"][0]
    (_draft_dir(package_root) / tool_drafts.MANIFEST_FILENAME).write_text(
        json.dumps(written), encoding="utf-8"
    )

    read = _manifest(package_root)

    attempt = read["attempts"][0]
    assert (attempt["not_brought_in"], attempt["not_brought_in_count"]) == ([], 0)
    view = _view(package_root)
    assert view is not None and view["state"] == "open"
    ToolDraft.model_validate(view)
    assert isinstance(_begin(package_root), dict)


def test_the_workspace_a_continuation_receives_is_the_draft_as_it_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The base has ``old.py``; the first round deleted it and changed ``run.py``;
    the operator then edited ``run.py`` and added a file in the draft. The new
    workspace has exactly that: ``old.py`` does not come back from the base, the
    edit is not undone, the added file is there, and nothing is reported left
    out."""
    package_root = _package(monkeypatch, tmp_path)
    (_version(package_root) / "old.py").write_text("print('old')\n", encoding="utf-8")

    def change(workspace: Path) -> None:
        (workspace / "old.py").unlink()
        (workspace / "run.py").write_text("print('first round')\n", encoding="utf-8")

    _first_round_with(tmp_path, package_root, change)
    files = _files(package_root)
    (files / "run.py").write_text("print('edited by hand')\n", encoding="utf-8")
    (files / "notes").mkdir()
    (files / "notes" / "mine.txt").write_text("the operator's\n", encoding="utf-8")
    begun = _begin(package_root)
    assert isinstance(begun, dict)
    workspace = tmp_path / "work2" / "build"

    copied = _copy_in(package_root, begun, workspace)

    assert isinstance(copied, tool_drafts.CopiedIn)
    assert copied.not_brought_in == ()
    assert sorted(
        os.path.relpath(os.path.join(directory, name), workspace)
        for directory, dirnames, filenames in os.walk(workspace)
        for name in dirnames + filenames
    ) == ["notes", "notes/mine.txt", "run.py", "tool.json"]
    assert (workspace / "run.py").read_text(encoding="utf-8") == "print('edited by hand')\n"
    assert tool_drafts.same_tree(copied.copied_out, tool_drafts.tree_digest(workspace))


def test_a_link_into_the_draft_is_brought_in_relative_and_one_out_of_it_is_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An absolute link to a file of the draft's own copy is brought in rewritten
    as relative, resolving to the workspace's file; an absolute link to a file
    outside the draft is not brought in, and is recorded as such."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    outside = tmp_path / "outside.txt"
    outside.write_text("not the draft's\n", encoding="utf-8")
    os.symlink(files / "run.py", files / "inside.py")
    os.symlink(outside, files / "out.txt")
    begun = _begin(package_root)
    assert isinstance(begun, dict)
    workspace = tmp_path / "work2" / "build"

    copied = _copy_in(package_root, begun, workspace)

    assert isinstance(copied, tool_drafts.CopiedIn)
    assert os.readlink(workspace / "inside.py") == "run.py"
    assert (workspace / "inside.py").resolve() == (workspace / "run.py").resolve()
    assert not os.path.lexists(workspace / "out.txt")
    assert copied.not_brought_in == (tool_drafts.Omitted("out.txt", "link_outside"),)


def test_a_draft_s_env_and_metadata_are_not_brought_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``.env`` and ``.afterthread.meta/`` at the top of the draft's files are not
    brought in, and are recorded; a ``.env`` deeper down is content like any file."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    (files / ".env").write_text("HAND=placed-by-the-operator\n", encoding="utf-8")
    (files / tools._META_DIRNAME).mkdir()
    (files / tools._META_DIRNAME / "origin.json").write_text("{}", encoding="utf-8")
    (files / "sub").mkdir()
    (files / "sub" / ".env").write_text("NESTED=1\n", encoding="utf-8")
    begun = _begin(package_root)
    assert isinstance(begun, dict)
    workspace = tmp_path / "work2" / "build"

    copied = _copy_in(package_root, begun, workspace)

    assert isinstance(copied, tool_drafts.CopiedIn)
    assert not os.path.lexists(workspace / ".env")
    assert not os.path.lexists(workspace / tools._META_DIRNAME)
    assert (workspace / "sub" / ".env").is_file()
    assert copied.not_brought_in == (
        tool_drafts.Omitted(".afterthread.meta", "backend_meta"),
        tool_drafts.Omitted(".env", "env_file"),
    )


def test_an_env_spelled_another_way_is_not_brought_in_when_it_may_be_the_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``.ENV`` alone at the top of the draft's files. Where it is the same entry as
    ``.env`` -- a filesystem that does not tell case apart -- it is not brought in
    (``env_file``); where it is another entry it is content. When the look that
    tells them apart fails, it is not brought in either (``unreadable``)."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    (files / ".ENV").write_text("HAND=placed-by-the-operator\n", encoding="utf-8")
    same_entry = os.path.lexists(files / ".env")
    begun = _begin(package_root)
    assert isinstance(begun, dict)

    seen = _copy_in(package_root, begun, tmp_path / "seen" / "build")
    with monkeypatch.context() as patch:
        failed = _refuse(patch, "lstat", lambda path: path == os.fspath(files / ".env"), errno.EIO)
        unseen = _copy_in(package_root, begun, tmp_path / "unseen" / "build")

    assert isinstance(seen, tool_drafts.CopiedIn) and isinstance(unseen, tool_drafts.CopiedIn)
    if same_entry:
        assert seen.not_brought_in == (tool_drafts.Omitted(".ENV", "env_file"),)
        assert not os.path.lexists(tmp_path / "seen" / "build" / ".ENV")
    else:
        assert seen.not_brought_in == ()
    assert failed
    assert unseen.not_brought_in == (tool_drafts.Omitted(".ENV", "unreadable"),)
    assert not os.path.lexists(tmp_path / "unseen" / "build" / ".ENV")


def test_a_metadata_directory_spelled_another_way_is_not_brought_in_when_it_may_be_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``.AFTERTHREAD.META/`` alone at the top of the draft's files, holding the
    operator's notes. Where it is the same entry as ``.afterthread.meta/`` -- a
    filesystem that does not tell case apart, where the strip before the save
    would remove it -- it is not brought in (``backend_meta``); where it is another
    entry it is content. When whether it is the same entry cannot be told, it is not
    brought in either (``unreadable``)."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    (files / ".AFTERTHREAD.META").mkdir()
    (files / ".AFTERTHREAD.META" / "notes.txt").write_text("mine\n", encoding="utf-8")
    same_entry = os.path.lexists(files / tools._META_DIRNAME)
    begun = _begin(package_root)
    assert isinstance(begun, dict)

    seen = _copy_in(package_root, begun, tmp_path / "seen" / "build")
    with monkeypatch.context() as patch:
        patch.setattr(tool_drafts, "_same_entry", lambda first, second: None)
        unseen = _copy_in(package_root, begun, tmp_path / "unseen" / "build")

    assert isinstance(seen, tool_drafts.CopiedIn) and isinstance(unseen, tool_drafts.CopiedIn)
    if same_entry:
        assert seen.not_brought_in == (tool_drafts.Omitted(".AFTERTHREAD.META", "backend_meta"),)
        assert not os.path.lexists(tmp_path / "seen" / "build" / ".AFTERTHREAD.META")
    else:
        assert seen.not_brought_in == ()
        notes = tmp_path / "seen" / "build" / ".AFTERTHREAD.META" / "notes.txt"
        assert notes.read_text(encoding="utf-8") == "mine\n"
    assert unseen.not_brought_in == (tool_drafts.Omitted(".AFTERTHREAD.META", "unreadable"),)
    assert not os.path.lexists(tmp_path / "unseen" / "build" / ".AFTERTHREAD.META")


def test_each_name_in_stripped_root_names_and_a_case_alias_of_it_is_not_brought_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every root name ``tool_drafts.STRIPPED_ROOT_NAMES`` lists is withheld from a
    continuation's workspace under the reason it is keyed by: spelled as listed,
    and spelled in another case where that is the same entry -- the look that
    tells is answered yes here, as a filesystem that does not tell case apart
    answers it -- while the other listed names are at the top as listed."""
    stripped = {name: reason for reason, name in tool_drafts.STRIPPED_ROOT_NAMES.items()}
    assert stripped
    # Neither as listed nor all in capitals: ``.Env``.
    aliases = {name: name.title() for name in stripped}
    assert all(alias not in (name, name.upper()) for name, alias in aliases.items())
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    begun = _begin(package_root)
    assert isinstance(begun, dict)
    monkeypatch.setattr(tool_drafts, "_same_entry", lambda first, second: True)

    def withheld(top: dict[str, tool_drafts.OmitReason], into: str) -> dict[str, str]:
        for name in [*stripped, *aliases.values()]:
            (files / name).unlink(missing_ok=True)
        for name in top:
            (files / name).write_text("placed by hand\n", encoding="utf-8")
        copied = _copy_in(package_root, begun, tmp_path / into / "build")
        assert isinstance(copied, tool_drafts.CopiedIn)
        assert [name for name in top if os.path.lexists(tmp_path / into / "build" / name)] == []
        return {item.path: item.reason for item in copied.not_brought_in}

    assert withheld(stripped, "listed") == stripped
    for name, reason in stripped.items():
        top = {other: why for other, why in stripped.items() if other != name}
        top[aliases[name]] = reason
        assert withheld(top, f"aliased-{reason}") == top


@pytest.mark.parametrize("answer", [True, None], ids=["same-entry", "cannot-tell"])
def test_a_case_variant_listed_with_its_exact_name_is_another_entry_whose_identity_is_not_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answer: bool | None
) -> None:
    """A listing that holds a name ``tool_drafts.STRIPPED_ROOT_NAMES`` lists and,
    beside it, that name spelled in another case holds two entries -- only a
    filesystem that tells case apart can list both -- so ``withheld_root_names``
    leaves out the name and not the variant, which is the tool's own content. Nor
    does it ask the variant's identity: not when the look would answer that the two
    are one entry, nor when it would answer that it cannot tell."""
    stripped = {name: reason for reason, name in tool_drafts.STRIPPED_ROOT_NAMES.items()}
    assert stripped
    asked: list[tuple[str, str]] = []

    def same_entry(first: Path, second: Path) -> bool | None:
        asked.append((first.name, second.name))
        return answer

    monkeypatch.setattr(tool_drafts, "_same_entry", same_entry)

    for name, reason in stripped.items():
        variant = name.upper()
        assert variant != name
        assert tool_drafts.withheld_root_names(tmp_path, (name, variant)) == {name: reason}
    assert asked == []


def test_a_draft_file_that_grows_while_it_is_copied_in_is_stopped_by_the_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The draft's files fit the byte limit when measured; then a file grows before
    its bytes are copied. The bytes the copy actually writes are counted, so the
    copy stops at the limit and the round does not begin."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    begun = _begin(package_root)
    assert isinstance(begun, dict)
    real_plan = tool_drafts._plan_copy

    def plan_then_grow(*args: Any, **kwargs: Any) -> Any:
        plan = real_plan(*args, **kwargs)
        with (files / "run.py").open("a", encoding="utf-8") as grown:
            grown.write("#" * 5000)
        return plan

    monkeypatch.setattr(tool_drafts, "_plan_copy", plan_then_grow)

    refused = _copy_in(package_root, begun, tmp_path / "work2" / "build", max_bytes=2000)

    assert refused == tool_drafts.REASON_TOO_LARGE
    assert _manifest(package_root) == begun


def test_a_draft_over_the_limits_is_refused_before_anything_is_copied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The draft's files are measured before the copy: over the limit, the round
    does not begin and no workspace is made."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    begun = _begin(package_root)
    assert isinstance(begun, dict)
    workspace = tmp_path / "work2" / "build"

    refused = _copy_in(package_root, begun, workspace, max_files=1)

    assert refused == tool_drafts.REASON_TOO_MANY_FILES
    assert not os.path.lexists(workspace)


def test_a_draft_file_holding_a_known_secret_is_not_brought_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file of the draft holds a known secret -- put there by hand after the save:
    it is not brought into the workspace, nothing takes its place, and it is
    recorded."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    (_files(package_root) / "notes.txt").write_text(f"token {_SECRET}\n", encoding="utf-8")
    begun = _begin(package_root)
    assert isinstance(begun, dict)
    workspace = tmp_path / "work2" / "build"

    copied = _copy_in(package_root, begun, workspace)

    assert isinstance(copied, tool_drafts.CopiedIn)
    assert copied.not_brought_in == (tool_drafts.Omitted("notes.txt", "secret_content"),)
    assert not os.path.lexists(workspace / "notes.txt")
    assert not any(_SECRET in text for text in _all_bytes_under(workspace))


@pytest.mark.parametrize("scanned", ["draft", "copy"])
def test_a_draft_file_the_scan_cannot_search_is_left_out_and_the_round_begins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scanned: str
) -> None:
    """Copying the draft into a continuation's workspace, the scan cannot search
    ``notes.txt`` -- in the draft's files, or in the copy made of them. Unlike a
    save, the copy goes on: ``notes.txt`` is not brought in and is recorded as
    unreadable, and the draft still holds it."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    (files / "notes.txt").write_text("mine\n", encoding="utf-8")
    begun = _begin(package_root)
    assert isinstance(begun, dict)
    workspace = tmp_path / "work2" / "build"
    where = files if scanned == "draft" else workspace
    real_search = tools._search_file

    def search(path: str, secrets: list[str], overlap: int) -> Any:
        if Path(path) == where / "notes.txt":
            return "unreadable"
        return real_search(path, secrets, overlap)

    monkeypatch.setattr(tools, "_search_file", search)

    copied = _copy_in(package_root, begun, workspace)

    assert isinstance(copied, tool_drafts.CopiedIn)
    assert copied.not_brought_in == (tool_drafts.Omitted("notes.txt", "unreadable"),)
    assert not os.path.lexists(workspace / "notes.txt")
    assert (files / "notes.txt").read_text(encoding="utf-8") == "mine\n"


def test_a_round_s_note_is_kept_whole_up_to_the_request_s_own_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A note as long as a request may send is recorded as it is."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    workspace, previous, copied_in = _continuing(tmp_path, package_root)
    note = "n" * 20_000

    assert _save(
        package_root,
        workspace,
        copied_in,
        previous=previous,
        draft_round=tool_drafts.DraftRound(
            finished_at=_now(), outcome="not_published", job_id=_JOB, note=note
        ),
    ).saved
    assert _manifest(package_root)["attempts"][-1]["note"] == note


# --- publishing the draft as it is: its copy, what may stop it, its mark ----------


def _publish_copy(
    package_root: tools.PackageRoot,
    workspace: Path,
    *,
    expected: tuple[str, int] | None = None,
    max_files: int = 2000,
    max_bytes: int = 50_000_000,
) -> tool_drafts.PublishCopy | str:
    """``copy_for_publish`` of the draft as it is now, or as ``expected`` names it,
    in the package as it is now."""
    return tool_drafts.copy_for_publish(
        package_root=package_root,
        package_dir_identity=tool_drafts.directory_identity(package_root.path),
        expected=_identity(package_root) if expected is None else expected,
        workspace=workspace,
        max_files=max_files,
        max_bytes=max_bytes,
    )


def _copied_for_publish(
    package_root: tools.PackageRoot, workspace: Path
) -> tool_drafts.PublishCopy:
    copied = _publish_copy(package_root, workspace)
    assert isinstance(copied, tool_drafts.PublishCopy), copied
    return copied


def _as_it_is(package_root: tools.PackageRoot) -> tuple[bytes, tool_drafts.TreeDigest]:
    """The draft's manifest, byte for byte, and the digest of its files."""
    digest = tool_drafts.tree_digest(_files(package_root))
    assert digest is not None
    return (_draft_dir(package_root) / tool_drafts.MANIFEST_FILENAME).read_bytes(), digest


def _case_told_apart(root: Path) -> bool:
    """Whether the filesystem under ``root`` tells two names that differ only in
    case apart: probed there, not inferred from the platform."""
    probe = root / ".case-probe"
    probe.write_text("x", encoding="utf-8")
    try:
        return not os.path.lexists(root / ".CASE-PROBE")
    finally:
        probe.unlink()


def _unsearchable(patch: pytest.MonkeyPatch, path: Path, answer: Any = "unreadable") -> None:
    """The embedded-secret scan cannot search the file at ``path``: its search of
    that one file answers ``answer``, or raises it when it is an exception."""
    real_search = tools._search_file

    def search(searched: str, secrets: list[str], overlap: int) -> Any:
        if Path(searched) == path:
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return real_search(searched, secrets, overlap)

    patch.setattr(tools, "_search_file", search)


def test_a_publish_is_judged_as_a_continuation_is_but_for_the_read_before_the_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``judge_draft`` answers the draft named, at its generation and based on
    ``current``, with its manifest. A continuation judged on the same draft is
    refused when the route read nothing before the slot; a publish makes no such
    read and is not. Another generation, or another draft, is refused alike."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    resolution = tools.resolve_current(package_root)
    draft_id, generation = _identity(package_root)
    named = (draft_id, generation)

    assert tool_drafts.judge_draft(package_root, resolution, named) == _manifest(package_root)
    assert tool_drafts.judge_continuation(package_root, resolution, named, None) == (
        "draft_changed"
    )
    assert tool_drafts.judge_draft(package_root, resolution, (draft_id, generation + 1)) == (
        "draft_changed"
    )
    assert tool_drafts.judge_draft(package_root, resolution, ("f" * 32, generation)) == (
        "draft_gone"
    )


def test_a_publish_copy_of_a_draft_the_backend_saved_receives_all_of_it_and_only_reads_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The operator edited ``run.py`` in the draft and added a module. The workspace
    receives exactly the draft's files, ``copied_in`` equal to ``copied_out``; the
    copy names nothing left out, nothing unlooked, no secret, and does not stop the
    publish. The draft's manifest and files are as they were."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    (files / "run.py").write_text("print('finished by hand')\n", encoding="utf-8")
    (files / "lib").mkdir()
    (files / "lib" / "util.py").write_text("X = 1\n", encoding="utf-8")
    before = _as_it_is(package_root)
    workspace = tmp_path / "publish" / "build"

    copied = _copied_for_publish(package_root, workspace)

    assert copied.refusal is None
    assert copied.left_out is None
    assert copied.not_brought_in == ()
    assert copied.unlooked == ()
    assert (copied.secret_paths, copied.withheld_secret_paths) == ((), ())
    assert copied.files_dir == _manifest(package_root)["files_dir"]
    assert tool_drafts.same_tree(copied.copied_out, copied.copied_in)
    assert tool_drafts.same_tree(copied.copied_out, before[1])
    assert (workspace / "run.py").read_text(encoding="utf-8") == "print('finished by hand')\n"
    assert _as_it_is(package_root) == before


def test_a_publish_copy_withholds_the_root_env_and_metadata_and_their_secrets_stop_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The operator put a ``.env`` and a ``.afterthread.meta/`` at the top of the
    draft's files, each holding a known secret. Neither reaches the workspace, so
    neither reaches a version: the secrets in them are named apart and do not stop
    the publish. The two are named as left out, and the workspace differs from
    the draft by them."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    (files / ".env").write_text(f"KB_KEY={_SECRET}\n", encoding="utf-8")
    (files / tools._META_DIRNAME).mkdir()
    (files / tools._META_DIRNAME / "notes.txt").write_text(f"token {_SECRET}\n", encoding="utf-8")
    workspace = tmp_path / "publish" / "build"

    copied = _copied_for_publish(package_root, workspace)

    assert copied.refusal is None
    assert copied.secret_paths == ()
    assert copied.withheld_secret_paths == (".afterthread.meta/notes.txt", ".env")
    assert copied.unlooked == ()
    assert copied.not_brought_in == (
        tool_drafts.Omitted(".afterthread.meta", "backend_meta"),
        tool_drafts.Omitted(".env", "env_file"),
    )
    assert copied.left_out == tool_drafts.NOTE_NOT_BROUGHT_IN.format(
        paths=".afterthread.meta、.env"
    )
    assert not tool_drafts.same_tree(copied.copied_out, copied.copied_in)
    assert not os.path.lexists(workspace / ".env")
    assert not os.path.lexists(workspace / tools._META_DIRNAME)
    assert not any(_SECRET in text for text in _all_bytes_under(workspace))


@pytest.mark.parametrize(
    "path",
    ["notes.txt", "sub/.env", "sub/.afterthread.meta/notes.txt", "l"],
    ids=["a-file", "an-env-below-the-top", "metadata-below-the-top", "a-link-s-target"],
)
def test_a_publish_copy_is_refused_for_a_known_secret_outside_the_withheld_root_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str
) -> None:
    """A known secret in a file, in a ``.env`` or a metadata directory below the
    top -- content like any file there -- or in a link's target. The publish is
    refused naming the path; the secret reaches neither the workspace nor the
    reason, and the draft is as it was."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    if path == "l":
        os.symlink(f"/nowhere/{_SECRET}", files / path)
    else:
        (files / path).parent.mkdir(parents=True, exist_ok=True)
        (files / path).write_text(f"KB_KEY={_SECRET}\n", encoding="utf-8")
    before = _as_it_is(package_root)
    workspace = tmp_path / "publish" / "build"

    copied = _copied_for_publish(package_root, workspace)

    assert copied.secret_paths == (path,)
    assert copied.withheld_secret_paths == ()
    assert copied.refusal == tool_drafts.REASON_PUBLISH_SECRETS.format(paths=path)
    assert not any(_SECRET in text for text in _all_bytes_under(workspace))
    assert _as_it_is(package_root) == before


def test_a_publish_copy_refused_for_a_secret_in_a_name_does_not_spell_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file of the draft is named after a known secret: the publish is refused,
    and the reason names the path masked."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    name = f"key-{_SECRET}.txt"
    (_files(package_root) / name).write_text("plain\n", encoding="utf-8")
    workspace = tmp_path / "publish" / "build"

    copied = _copied_for_publish(package_root, workspace)

    assert copied.secret_paths == (name,)
    assert copied.refusal is not None
    assert copied.refusal.startswith(tool_drafts.REASON_PUBLISH_SECRETS.format(paths=""))
    assert _SECRET not in copied.refusal
    assert not os.path.lexists(workspace / name)


def test_a_publish_copy_goes_on_without_what_the_link_rule_or_its_kind_leaves_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A FIFO, a link out of the draft, and a link whose ``..`` follows another link
    are left out of the workspace for what they are, not for a failed look or a
    secret: the publish is not refused for them. They are named as left out, and
    the workspace differs from the draft by them."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    os.mkfifo(files / "pipe")
    outside = tmp_path / "outside.txt"
    outside.write_text("not the draft's\n", encoding="utf-8")
    os.symlink(outside, files / "out.txt")
    (files / "sub").mkdir()
    os.symlink("sub", files / "to_sub")
    os.symlink("to_sub/../run.py", files / "around.py")
    workspace = tmp_path / "publish" / "build"

    copied = _copied_for_publish(package_root, workspace)

    assert copied.refusal is None
    assert copied.unlooked == ()
    assert copied.secret_paths == ()
    assert copied.not_brought_in == (
        tool_drafts.Omitted("around.py", "link_ambiguous"),
        tool_drafts.Omitted("out.txt", "link_outside"),
        tool_drafts.Omitted("pipe", "not_regular"),
    )
    assert copied.left_out == tool_drafts.NOTE_NOT_BROUGHT_IN.format(
        paths="around.py、out.txt、pipe"
    )
    assert not tool_drafts.same_tree(copied.copied_out, copied.copied_in)
    assert os.readlink(workspace / "to_sub") == "sub"


@pytest.mark.parametrize(
    ("unsearchable", "refused"),
    [(".env", False), (".afterthread.meta/notes.txt", False), ("sub/.env", True)],
    ids=["the-root-env", "in-the-root-metadata", "an-env-below-the-top"],
)
def test_only_a_root_entry_the_copy_withholds_may_be_one_the_scan_cannot_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unsearchable: str, refused: bool
) -> None:
    """The scan cannot search one file. In the root ``.env``, or in the root
    metadata directory, which never reach a version, that stops nothing. The same
    name below the top is content: it is left out for a failed look, and the
    publish is refused naming it."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    (files / unsearchable).parent.mkdir(parents=True, exist_ok=True)
    (files / unsearchable).write_text("KEY=value\n", encoding="utf-8")
    _unsearchable(monkeypatch, files / unsearchable)

    copied = _copied_for_publish(package_root, tmp_path / "publish" / "build")

    if refused:
        assert copied.unlooked == (unsearchable,)
        assert copied.refusal == tool_drafts.REASON_PUBLISH_UNLOOKED.format(paths=unsearchable)
    else:
        assert copied.unlooked == ()
        assert copied.refusal is None


@pytest.mark.parametrize("where", ["draft", "copy"])
def test_a_publish_copy_is_refused_when_the_scan_cannot_search_a_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, where: str
) -> None:
    """The scan cannot search ``notes.txt`` -- in the draft's files, or in the copy
    made of them. A continuation would go on without it; a publish is refused
    naming it, since whether it may be published is not known. The draft is as it
    was."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    (files / "notes.txt").write_text("mine\n", encoding="utf-8")
    before = _as_it_is(package_root)
    workspace = tmp_path / "publish" / "build"
    _unsearchable(monkeypatch, (files if where == "draft" else workspace) / "notes.txt")

    copied = _copied_for_publish(package_root, workspace)

    assert copied.not_brought_in == (tool_drafts.Omitted("notes.txt", "unreadable"),)
    assert copied.unlooked == ("notes.txt",)
    assert copied.secret_paths == ()
    assert copied.refusal == tool_drafts.REASON_PUBLISH_UNLOOKED.format(paths="notes.txt")
    assert _as_it_is(package_root) == before


@pytest.mark.parametrize("where", ["draft", "copy"])
def test_a_publish_copy_is_refused_when_a_look_on_a_link_s_way_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, where: str
) -> None:
    """``l -> sub/../run.py``: whether ``sub`` is a link -- which decides where the
    ``..`` goes -- cannot be looked at, in the draft's files or in the copy made of
    them. ``l`` is left out for a failed look, and the publish is refused naming
    it. The draft is as it was."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    (files / "sub").mkdir()
    os.symlink("sub/../run.py", files / "l")
    before = _as_it_is(package_root)
    workspace = tmp_path / "publish" / "build"
    looked_at = (files if where == "draft" else workspace) / "sub"

    with monkeypatch.context() as patch:
        failed = _refuse(patch, "lstat", lambda path: Path(path) == looked_at, errno.EIO)
        copied = _copied_for_publish(package_root, workspace)

    assert failed
    assert copied.not_brought_in == (tool_drafts.Omitted("l", "unreadable"),)
    assert copied.unlooked == ("l",)
    assert copied.refusal == tool_drafts.REASON_PUBLISH_UNLOOKED.format(paths="l")
    assert _as_it_is(package_root) == before


@pytest.mark.parametrize("stopped_by", ["scan-of-the-draft", "scan-of-the-copy", "the-limits"])
def test_a_publish_copy_that_cannot_be_judged_whole_names_why_and_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stopped_by: str
) -> None:
    """The scan of the draft's files, or of the copy made of them, cannot finish;
    or the draft is over the limits. No copy is handed back, only why, and the
    draft is as it was."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    (files / "notes.txt").write_text("mine\n", encoding="utf-8")
    before = _as_it_is(package_root)
    workspace = tmp_path / "publish" / "build"
    max_files = 2000
    if stopped_by == "the-limits":
        max_files = 1
    else:
        where = files if stopped_by == "scan-of-the-draft" else workspace
        _unsearchable(monkeypatch, where / "notes.txt", RuntimeError("scan broke"))

    answer = _publish_copy(package_root, workspace, max_files=max_files)

    assert answer == (
        tool_drafts.REASON_TOO_MANY_FILES
        if stopped_by == "the-limits"
        else tool_drafts.REASON_SCAN_FAILED.format(reason="RuntimeError")
    )
    assert _as_it_is(package_root) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root can look into a mode-000 directory")
def test_a_publish_copy_is_refused_while_the_known_secrets_are_not_all_known(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Another tool's directory cannot be looked at, so the known secrets the copy
    would be scanned for are not whole: nothing is copied, and why says so."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    before = _as_it_is(package_root)
    other = package_root.path.parent / "other"
    other.mkdir()
    (other / ".env").write_text("OTHER_KEY=other-secret-abcdef\n", encoding="utf-8")
    workspace = tmp_path / "publish" / "build"
    other.chmod(0)
    try:
        answer = _publish_copy(package_root, workspace)
    finally:
        other.chmod(0o700)

    assert isinstance(answer, str)
    assert answer.startswith(tool_drafts.REASON_SECRETS_INCOMPLETE.format(detail=""))
    assert not os.path.lexists(workspace)
    assert _as_it_is(package_root) == before


@pytest.mark.parametrize(
    ("same_entry", "holds_secret", "answer"),
    [
        (True, True, "withheld"),
        (False, True, "secret"),
        (False, False, "brought-in"),
        (None, False, "unlooked"),
        (None, True, "secret"),
    ],
    ids=[
        "the-env-holding-a-secret",
        "another-file-holding-a-secret",
        "another-file",
        "unknown",
        "unknown-holding-a-secret",
    ],
)
def test_a_root_env_spelled_another_way_is_withheld_only_when_it_is_seen_to_be_the_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    same_entry: bool | None,
    holds_secret: bool,
    answer: str,
) -> None:
    """``.Env`` alone at the top of the draft's files. The look that tells whether
    it is ``.env`` is answered here as a filesystem answers it: yes where case is
    not told apart, no where it is, or not at all. Seen to be the ``.env``, it is
    withheld like it, and a secret in it stops nothing. Seen to be another file,
    it is content: a secret in it stops the publish, and without one it is brought
    in. Not known, it is left out, and the publish is refused -- for the secret
    in it when there is one, else for the failed look."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    value = _SECRET if holds_secret else "placed-by-the-operator"
    (files / ".Env").write_text(f"KB_KEY={value}\n", encoding="utf-8")
    monkeypatch.setattr(tool_drafts, "_same_entry", lambda first, second: same_entry)
    workspace = tmp_path / "publish" / "build"

    copied = _copied_for_publish(package_root, workspace)

    if answer == "brought-in":
        assert copied.refusal is None
        assert copied.not_brought_in == ()
        assert (workspace / ".Env").read_text(encoding="utf-8") == f"KB_KEY={value}\n"
        return
    assert not os.path.lexists(workspace / ".Env")
    if answer == "withheld":
        assert copied.refusal is None
        assert copied.not_brought_in == (tool_drafts.Omitted(".Env", "env_file"),)
        assert copied.withheld_secret_paths == (".Env",)
    elif answer == "secret":
        assert copied.secret_paths == (".Env",)
        assert copied.refusal == tool_drafts.REASON_PUBLISH_SECRETS.format(paths=".Env")
    else:
        assert copied.unlooked == (".Env",)
        assert copied.not_brought_in == (tool_drafts.Omitted(".Env", "unreadable"),)
        assert copied.refusal == tool_drafts.REASON_PUBLISH_UNLOOKED.format(paths=".Env")


def test_a_root_env_alias_where_case_is_not_told_apart_is_withheld_with_its_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a filesystem that does not tell case apart, ``.ENV`` is the ``.env``: it
    is withheld like it, and the known secret in it does not stop the publish."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    if _case_told_apart(files):
        pytest.skip("this filesystem tells case apart: .ENV is another file here, not the .env")
    (files / ".ENV").write_text(f"KB_KEY={_SECRET}\n", encoding="utf-8")
    workspace = tmp_path / "publish" / "build"

    copied = _copied_for_publish(package_root, workspace)

    assert copied.refusal is None
    assert copied.not_brought_in == (tool_drafts.Omitted(".ENV", "env_file"),)
    assert copied.withheld_secret_paths == (".ENV",)
    assert not any(_SECRET in text for text in _all_bytes_under(workspace))


@pytest.mark.parametrize("beside", [False, True], ids=["alone", "beside-the-env"])
def test_a_root_env_spelled_another_way_where_case_is_told_apart_is_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, beside: bool
) -> None:
    """On a filesystem that tells case apart, ``.ENV`` -- alone, or beside a
    ``.env`` -- is another file: a known secret in it stops the publish."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    if not _case_told_apart(files):
        pytest.skip("this filesystem does not tell case apart: .ENV is the .env here")
    if beside:
        (files / ".env").write_text("HAND=placed-by-the-operator\n", encoding="utf-8")
    (files / ".ENV").write_text(f"KB_KEY={_SECRET}\n", encoding="utf-8")

    copied = _copied_for_publish(package_root, tmp_path / "publish" / "build")

    assert copied.secret_paths == (".ENV",)
    assert copied.refusal == tool_drafts.REASON_PUBLISH_SECRETS.format(paths=".ENV")


@pytest.mark.parametrize(
    ("case", "why"),
    [
        ("another-generation", tool_drafts.START_CHANGED),
        ("another-draft", tool_drafts.START_CHANGED),
        ("marked", tool_drafts.START_PUBLISHING),
        ("files-missing", tool_drafts.REASON_FILES_MISSING),
    ],
)
def test_a_publish_copy_of_a_draft_not_as_it_was_judged_copies_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str, why: str
) -> None:
    """The draft is no longer the one judged -- another generation, another draft,
    a publish from it marked meanwhile -- or its files directory is gone: no
    workspace is made, and the manifest is as it was."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    draft_id, generation = _identity(package_root)
    expected = (draft_id, generation)
    if case == "another-generation":
        expected = (draft_id, generation + 1)
    elif case == "another-draft":
        expected = ("f" * 32, generation)
    elif case == "marked":
        marked = tool_drafts.mark_publishing(
            package_root, tool_drafts.directory_identity(package_root.path), expected, _JOB
        )
        assert marked is not None
        expected = (draft_id, marked)
    else:
        shutil.rmtree(_files(package_root))
    manifest = (_draft_dir(package_root) / tool_drafts.MANIFEST_FILENAME).read_bytes()
    workspace = tmp_path / "publish" / "build"

    answer = _publish_copy(package_root, workspace, expected=expected)

    assert answer == why
    assert not os.path.lexists(workspace)
    assert (_draft_dir(package_root) / tool_drafts.MANIFEST_FILENAME).read_bytes() == manifest


def test_mark_publishing_says_whether_the_mark_it_wrote_is_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``durable_out`` receives one answer for the mark written: True when the write
    is confirmed on disk, False when the manifest was replaced but the fsync of
    ``draft/`` after it is not confirmed; nothing when no mark was written."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    identity = tool_drafts.directory_identity(package_root.path)
    draft_id, generation = _identity(package_root)

    on_disk: list[bool] = []
    marked = tool_drafts.mark_publishing(
        package_root, identity, (draft_id, generation), _JOB, durable_out=on_disk
    )
    unwritten: list[bool] = []
    stale = tool_drafts.mark_publishing(
        package_root, identity, (draft_id, generation), _JOB, durable_out=unwritten
    )
    real_write = tools._write_package_file_atomic

    def replaced_not_durable(
        directory: Path, filename: str, data: bytes, expected: Any, **kwargs: Any
    ) -> bool:
        out = kwargs.pop("durability_out", None)
        published = real_write(directory, filename, data, expected, **kwargs)
        if out is not None:
            out.append(False)
        return published

    unconfirmed: list[bool] = []
    with monkeypatch.context() as patch:
        patch.setattr(tools, "_write_package_file_atomic", replaced_not_durable)
        again = tool_drafts.mark_publishing(
            package_root, identity, (draft_id, generation + 1), _JOB, durable_out=unconfirmed
        )

    assert (marked, on_disk) == (generation + 1, [True])
    assert (stale, unwritten) == (None, [])
    assert (again, unconfirmed) == (generation + 2, [False])
    assert _manifest(package_root)["publishing"]["job_id"] == _JOB


def test_unmark_publishing_takes_back_only_its_own_mark_and_leaves_the_rounds_as_they_were(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A mark another job wrote, or a mark at another generation, is not taken back.
    The job's own is: one generation on, ``publishing`` cleared, and everything
    else -- the rounds above all -- as before the mark. With no mark left, there is
    nothing to take back."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    identity = tool_drafts.directory_identity(package_root.path)
    before = _manifest(package_root)
    draft_id = before["draft_id"]
    marked = tool_drafts.mark_publishing(package_root, identity, _identity(package_root), _JOB)
    assert marked is not None

    by_another = tool_drafts.unmark_publishing(package_root, identity, (draft_id, marked), "b" * 32)
    at_another = tool_drafts.unmark_publishing(package_root, identity, (draft_id, marked + 1), _JOB)
    still_marked = _manifest(package_root)
    unmarked = tool_drafts.unmark_publishing(package_root, identity, (draft_id, marked), _JOB)
    after = _manifest(package_root)
    nothing_left = tool_drafts.unmark_publishing(
        package_root, identity, (draft_id, marked + 1), _JOB
    )

    assert (by_another, at_another) == (None, None)
    assert still_marked["publishing"]["job_id"] == _JOB
    assert unmarked == marked + 1
    assert after == {**before, "generation": marked + 1}
    assert nothing_left is None
    assert _manifest(package_root) == after


def test_holds_copy_only_while_the_draft_is_seen_to_hold_all_the_copy_took(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The draft holds what its copy took: so it is, whatever generation the
    publish's own marks moved it to. Not for another draft; not for a copy whose
    two digests differ or are unknown -- both unknown included, the draft itself
    unreadable too; not for a copy naming another files directory; not once the
    draft's files differ, held again when they are as they were; not once the
    draft is gone."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    copied = _copied_for_publish(package_root, tmp_path / "publish" / "build")
    identity = tool_drafts.directory_identity(package_root.path)
    draft_id = _identity(package_root)[0]
    files = _files(package_root)
    run_py = (files / "run.py").read_text(encoding="utf-8")

    def holds(copy: tool_drafts.PublishCopy = copied, *, named: str = draft_id) -> bool:
        return tool_drafts.holds_copy(package_root, identity, named, copy, 2000, 50_000_000)

    assert copied.copied_in is not None
    assert holds()
    assert tool_drafts.mark_publishing(package_root, identity, _identity(package_root), _JOB)
    assert holds()
    assert not holds(named="f" * 32)
    assert not holds(dataclasses.replace(copied, copied_in=None))
    assert not holds(dataclasses.replace(copied, copied_out=None))
    assert not holds(
        dataclasses.replace(copied, copied_in={**copied.copied_in, "extra": ("d", 0o755)})
    )
    with monkeypatch.context() as patch:
        patch.setattr(tool_drafts, "_file_digest", lambda path, budget: None)
        assert not holds(dataclasses.replace(copied, copied_out=None, copied_in=None))
    assert not holds(dataclasses.replace(copied, files_dir="files-" + "e" * 32))
    (files / "run.py").write_text("print('changed after the copy')\n", encoding="utf-8")
    assert not holds()
    (files / "run.py").write_text(run_py, encoding="utf-8")
    assert holds()
    shutil.rmtree(_draft_dir(package_root))
    assert not holds()


@pytest.mark.parametrize("alias", ["metadata-directory", "package-directory"])
def test_through_a_link_no_draft_is_copied_for_a_publish_unmarked_or_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alias: str
) -> None:
    """Tool A reaches tool B's draft only through a link -- A's metadata directory,
    or A's whole directory. A publish of A's draft copies nothing, taking A's mark
    back writes nothing, and B's draft is never seen to hold a copy for A, though
    it is for B. B's draft is left exactly as it was."""
    package_b = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_b)
    copied_b = _copied_for_publish(package_b, tmp_path / "b" / "build")
    identity_b = tool_drafts.directory_identity(package_b.path)
    draft_id, generation = _identity(package_b)
    marked = tool_drafts.mark_publishing(package_b, identity_b, (draft_id, generation), _JOB)
    assert marked is not None
    linked = package_b.path.parent / "kc"
    if alias == "metadata-directory":
        _make_package(package_b.path.parent, name="kc")
        shutil.rmtree(linked / tools._META_DIRNAME)
        os.symlink(package_b.path / tools._META_DIRNAME, linked / tools._META_DIRNAME)
    else:
        os.symlink(package_b.path, linked)
    package_a = tools.PackageRoot(linked)
    identity_a = tool_drafts.directory_identity(linked)
    before = tool_drafts.tree_digest(_draft_dir(package_b))
    workspace_a = tmp_path / "a" / "build"

    copied_a = tool_drafts.copy_for_publish(
        package_root=package_a,
        package_dir_identity=identity_a,
        expected=(draft_id, generation),
        workspace=workspace_a,
        max_files=2000,
        max_bytes=50_000_000,
    )

    assert isinstance(copied_a, str)
    assert not os.path.lexists(workspace_a)
    assert tool_drafts.unmark_publishing(package_a, identity_a, (draft_id, marked), _JOB) is None
    assert not tool_drafts.holds_copy(package_a, identity_a, draft_id, copied_b, 2000, 50_000_000)
    assert tool_drafts.holds_copy(package_b, identity_b, draft_id, copied_b, 2000, 50_000_000)
    assert tool_drafts.same_tree(before, tool_drafts.tree_digest(_draft_dir(package_b)))
    assert _manifest(package_b)["publishing"]["job_id"] == _JOB


@pytest.mark.parametrize(
    ("withheld", "lookalike"),
    [(".env", ".env.local"), (tools._META_DIRNAME, f"{tools._META_DIRNAME}.bak")],
    ids=["beside-the-env", "beside-the-metadata"],
)
def test_a_root_name_that_only_begins_with_a_withheld_name_is_not_exempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, withheld: str, lookalike: str
) -> None:
    """The draft holds a root ``.env``, or a root ``.afterthread.meta/``, which the
    copy withholds; beside it, an entry whose name only begins with that name --
    ``.env.local``, ``.afterthread.meta.bak`` -- holds a known secret. That entry is
    another one, content like any file: the publish is refused naming it, and
    only the withheld entry is left out for its name."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    if withheld == ".env":
        (files / withheld).write_text("HAND=placed-by-the-operator\n", encoding="utf-8")
    else:
        (files / withheld).mkdir()
        (files / withheld / "notes.txt").write_text("mine\n", encoding="utf-8")
    (files / lookalike).write_text(f"KB_KEY={_SECRET}\n", encoding="utf-8")
    withheld_as = {name: why for why, name in tool_drafts.STRIPPED_ROOT_NAMES.items()}[withheld]

    copied = _copied_for_publish(package_root, tmp_path / "publish" / "build")

    assert {item.path: item.reason for item in copied.not_brought_in} == {
        withheld: withheld_as,
        lookalike: "secret_content",
    }
    assert copied.secret_paths == (lookalike,)
    assert copied.withheld_secret_paths == ()
    assert copied.refusal == tool_drafts.REASON_PUBLISH_SECRETS.format(paths=lookalike)


def test_a_secret_only_the_scan_of_the_copy_finds_stops_the_publish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``notes.txt`` is plain when the draft's files are scanned, and a known secret
    is written into it by hand before the copy reads it: only the scan of the copy
    finds the secret. The copy leaves the file out for it, and the publish is
    refused naming it -- not published without the file."""
    package_root = _package(monkeypatch, tmp_path)
    _first_draft(tmp_path, package_root)
    files = _files(package_root)
    (files / "notes.txt").write_text("plain\n", encoding="utf-8")
    workspace = tmp_path / "publish" / "build"
    real_scan = tools.scan_embedded_secrets
    scans: list[tuple[Path, Any]] = []

    def scan_then_edit(root: Any, secrets: Any = None) -> Any:
        scan = real_scan(root, secrets)
        scans.append((Path(root), scan))
        if Path(root) == files:
            (files / "notes.txt").write_text(f"token {_SECRET}\n", encoding="utf-8")
        return scan

    monkeypatch.setattr(tools, "scan_embedded_secrets", scan_then_edit)

    copied = _copied_for_publish(package_root, workspace)

    [(first_root, first_scan), (second_root, _second_scan)] = scans
    assert (first_root, second_root) == (files, workspace)
    assert isinstance(first_scan, tools.SecretScanDone) and first_scan.hits == ()
    assert copied.not_brought_in == (tool_drafts.Omitted("notes.txt", "secret_content"),)
    assert copied.secret_paths == ("notes.txt",)
    assert copied.refusal == tool_drafts.REASON_PUBLISH_SECRETS.format(paths="notes.txt")
    assert not any(_SECRET in text for text in _all_bytes_under(workspace))


def test_an_independent_case_variant_of_a_withheld_root_name_is_not_exempt() -> None:
    """The judgment on its own, so it holds on any filesystem: the copy withheld
    ``.env`` and nothing else, and the scan found a known secret in ``.ENV`` beside
    it -- an entry of its own, as on a filesystem that tells case apart. ``.ENV``
    is not exempt for spelling ``.env`` in another case: the publish is refused
    naming it."""
    copied = tool_drafts._DraftCopy(
        copied_out=None,
        scan=tools.SecretScanDone(hits=(tools.SecretHit(".ENV", "content"),), skipped=()),
        left_out={".env": "env_file"},
        omitted=[
            tool_drafts.Omitted(".ENV", "secret_content"),
            tool_drafts.Omitted(".env", "env_file"),
        ],
        unlooked=[],
    )

    judged = tool_drafts._publish_copy("files-" + "0" * 32, copied, None, [_SECRET])

    assert judged.secret_paths == (".ENV",)
    assert judged.withheld_secret_paths == ()
    assert judged.refusal == tool_drafts.REASON_PUBLISH_SECRETS.format(paths=".ENV")
