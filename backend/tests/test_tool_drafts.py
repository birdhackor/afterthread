"""The unfinished-revision store (``services/tool_drafts.py``) on its own.

The revise job's use of it -- which rounds save, the publish that follows a save,
the routes -- is in ``tests/test_tool_builder.py``, next to the rest of the revise
tests. Here: the content digest, the link rule, the manifest, the discard token,
the state a listing shows, the sweep, and the save itself driven directly, a save
into a draft that already exists included.
"""

import errno
import json
import os
import shutil
import stat
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args

import pytest

from afterthread.config import Settings
from afterthread.schemas import ToolDraft, ToolDraftLast
from afterthread.services import tool_builder, tool_drafts, tools

_VID = "20260728T010203Z-abcdef"
_OTHER_VID = "20260729T010203Z-123456"
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
        or tool_drafts.DraftRound(finished_at=_now(), outcome="not_published", summary="改到一半"),
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


def _first_draft(tmp_path: Path, package_root: tools.PackageRoot) -> Path:
    """Save one first round that changed ``run.py``; returns its workspace."""
    workspace = _workspace(tmp_path, package_root)
    copied_in = tool_drafts.tree_digest(workspace)
    (workspace / "run.py").write_text("print('first round')\n", encoding="utf-8")
    assert _save(package_root, workspace, copied_in).saved
    return workspace


def _continuing(
    tmp_path: Path, package_root: tools.PackageRoot
) -> tuple[Path, tool_drafts.PreviousDraft, tool_drafts.TreeDigest | None]:
    """A continuing round's start: a workspace copied from the draft's files."""
    manifest = _manifest(package_root)
    files = _draft_dir(package_root) / manifest["files_dir"]
    previous = tool_drafts.PreviousDraft(
        manifest=manifest, copied_out=tool_drafts.tree_digest(files)
    )
    workspace = tmp_path / "work2" / "build"
    shutil.copytree(files, workspace, symlinks=True)
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
    masked even when a known value is part of them -- a mask would break them."""
    secret = "aaaaaaaa"  # is a substring of the draft id
    data = tool_drafts.manifest_bytes(
        _document(
            feedback=f"key {_SECRET} here",
            attempt_fields={
                "summary": f"used {_SECRET}",
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

    def failing_copy(source: Path, destination: Path) -> None:
        real_copy(source, destination)
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
        first["generation"] + 1,
        2,
    )
    assert [attempt["number"] for attempt in second["attempts"]] == [1, 2]
    assert not (_draft_dir(package_root) / first["files_dir"]).exists()
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

    assert _manifest(package_root)["generation"] == first["generation"] + 1
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
    assert second["generation"] == first["generation"] + 1
    assert (second["attempts"][-1]["saved"], second["attempts"][-1]["reason"]) == (
        False,
        tool_drafts.REASON_TOO_MANY_FILES,
    )


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


def test_the_sweep_takes_only_unnamed_copies_half_made_ones_and_discard_leftovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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

    assert not unnamed.exists() and not half_made.exists() and not leftover.exists()
    assert _files(package_root).is_dir()
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
