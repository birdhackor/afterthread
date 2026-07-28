"""Fault-injection matrix for the one-time web-v5 tool migration.

The implementation's reconciliation table is exercised as real filesystem
states, not as mocked return values:

| journal says | disk shows | fixture that pins the action |
|---|---|---|
| done | done | crash after every ``*:done`` journal publish, then rerun |
| done | not done | remove a completed shell, then rerun and refuse/rollback |
| pending | done | crash after every filesystem action or before its ``done`` publish |
| pending | not done | crash after every ``*:pending`` publish, then rerun |
| unannounced | done | impossible through the mutation API; an inconsistent disk is refused |
| unannounced | not done | crash before every ``*:pending`` publish, then rerun |

Every journal publication is interrupted on BOTH sides.  The forward matrix
covers initialize, pending/done for all five actions, and committed.  The
rollback-only matrix covers rolling_back and rolled_back.  The three rename
rows additionally interrupt immediately AFTER the real rename, as invariant I
requires.  All fixtures are real directories and files under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from afterthread import migrate_tools_v5 as migration
from afterthread.services import tools

_START = datetime(2026, 7, 28, 1, 2, 3, tzinfo=UTC)
_VID = "20260728T010203Z-abcdef"
_JOURNAL_LABELS = (
    "journal:initialize",
    "journal:alpha:assemble:pending",
    "journal:alpha:assemble:done",
    "journal:alpha:copy_env:pending",
    "journal:alpha:copy_env:done",
    "journal:alpha:publish_shell:pending",
    "journal:alpha:publish_shell:done",
    "journal:alpha:park_old:pending",
    "journal:alpha:park_old:done",
    "journal:alpha:activate_new:pending",
    "journal:alpha:activate_new:done",
    "journal:committed",
)


class InjectedCrash(BaseException):
    """A process-loss simulation deliberately not caught by ``except Exception``."""


class PointFailureOperations(migration.MigrationOperations):
    """Fail exactly once at one semantic mutation boundary."""

    def __init__(
        self,
        failures: dict[str, BaseException],
        *,
        seen: list[str] | None = None,
    ) -> None:
        self.failures = dict(failures)
        self.seen = seen if seen is not None else []

    def checkpoint(self, point: str) -> None:
        self.seen.append(point)
        failure = self.failures.pop(point, None)
        if failure is not None:
            raise failure


def _manifest(name: str, *, enabled: bool = True) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"{name} tool",
        "parameters": {"type": "object", "properties": {}},
        "entry": ["python3", "run.py"],
        "enabled": enabled,
    }


def _make_package(
    root: Path,
    name: str = "alpha",
    *,
    enabled: bool = True,
    state: str = "absent",
    ai_meta: str = "absent",
    env: bytes | None = b"API_KEY=secret-value\r\nMODE=legacy\r\n",
    env_mode: int = 0o640,
) -> Path:
    package = root / name
    package.mkdir(parents=True)
    (package / "tool.json").write_text(
        json.dumps(_manifest(name, enabled=enabled), ensure_ascii=False),
        encoding="utf-8",
    )
    (package / "run.py").write_bytes(b'print("ok")\n')
    (package / "data").mkdir()
    (package / "data" / "payload.bin").write_bytes(b"\x00\xfftool-content")
    if env is not None:
        (package / ".env").write_bytes(env)
        (package / ".env").chmod(env_mode)

    state_path = package / migration._STATE_FILENAME
    if state == "ours":
        state_path.write_text(
            json.dumps(
                {
                    migration._STATE_MARKER_KEY: migration._STATE_MARKER_VALUE,
                    "enabled": False,
                }
            ),
            encoding="utf-8",
        )
    elif state == "foreign":
        state_path.write_bytes(b"foreign cursor bytes\x00\n")
        state_path.chmod(0o604)
    elif state == "unreadable":
        state_path.mkdir()
    elif state != "absent":
        raise AssertionError(f"unknown state fixture: {state}")

    ai_path = package / migration._AI_META_FILENAME
    if ai_meta == "valid":
        ai_path.write_text(
            json.dumps(
                {
                    "summary": "A generated summary",
                    "updated_at": "2026-07-27T10:20:30+00:00",
                    "llm_log_id": 17,
                    "llm_log_process": "process-token",
                    "origin": {
                        "openapi_url": "https://kb.example/openapi.json",
                        "instructions": "Install the KB lookup tool.",
                    },
                }
            ),
            encoding="utf-8",
        )
    elif ai_meta == "invalid":
        ai_path.write_bytes(b'{"origin": ')
    elif ai_meta == "unreadable":
        ai_path.mkdir()
    elif ai_meta != "absent":
        raise AssertionError(f"unknown ai-meta fixture: {ai_meta}")
    return package


def _run(
    root: Path,
    *,
    operations: migration.MigrationOperations | None = None,
    dry_run: bool = False,
    output: list[str] | None = None,
    token_hex: Callable[[int], str] = lambda _size: "abcdef",
    confirm: Callable[[], bool] = lambda: True,
) -> int:
    lines = output if output is not None else []
    return migration.migrate_tools(
        root,
        dry_run=dry_run,
        operations=operations,
        output=lines.append,
        started_at=_START,
        token_hex=token_hex,
        confirm=confirm,
    )


def _current_vid(root: Path, name: str = "alpha") -> str:
    return (root / name / ".afterthread.meta" / "current").read_text(encoding="utf-8").strip()


def _assert_migrated(root: Path, name: str = "alpha") -> Path:
    package = root / name
    vid = _current_vid(root, name)
    assert re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}", vid)
    version = package / "versions" / vid
    manifest = json.loads((version / "tool.json").read_text(encoding="utf-8"))
    assert manifest["name"] == name
    assert "enabled" not in manifest
    origin = json.loads((version / ".afterthread.meta" / "origin.json").read_text(encoding="utf-8"))
    assert origin["previous"] is None
    state = json.loads((package / ".afterthread.meta" / "state.json").read_text(encoding="utf-8"))
    assert state[migration._STATE_MARKER_KEY] == migration._STATE_MARKER_VALUE
    assert not (root / f"{name}.at-premigrate").exists()
    assert not (root / f"{name}.at-migrated").exists()
    assert not (root / f".{name}.at-migration-shell").exists()
    assert not (root / migration._JOURNAL_FILENAME).exists()
    return version


def _snapshot_tree(root: Path) -> tuple[tuple[str, str, int, bytes | str], ...]:
    """Snapshot content/type/mode, intentionally excluding read-mutated atimes."""

    rows: list[tuple[str, str, int, bytes | str]] = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        info = os.lstat(path)
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            rows.append((relative, "symlink", mode, os.readlink(path)))
        elif stat.S_ISDIR(info.st_mode):
            rows.append((relative, "directory", mode, b""))
        else:
            rows.append((relative, "file", mode, path.read_bytes()))
    return tuple(rows)


def test_happy_path_writes_target_layout_full_backup_and_minimal_origin(tmp_path: Path) -> None:
    root = tmp_path / "tools"
    old = _make_package(root, enabled=False, env_mode=0o640)
    old_snapshot = _snapshot_tree(old)
    output: list[str] = []

    assert _run(root, output=output) == 0

    version = _assert_migrated(root)
    assert _current_vid(root) == _VID
    assert (version / "run.py").read_bytes() == b'print("ok")\n'
    assert (version / "data" / "payload.bin").read_bytes() == b"\x00\xfftool-content"
    origin = json.loads((version / ".afterthread.meta" / "origin.json").read_text(encoding="utf-8"))
    assert origin == {
        "source": "pre-existing/unknown",
        "openapi_url": None,
        "instructions": None,
        "previous": None,
    }
    assert not (version / ".afterthread.meta" / "summary.json").exists()
    assert (
        json.loads(
            (root / "alpha" / ".afterthread.meta" / "state.json").read_text(encoding="utf-8")
        )["enabled"]
        is False
    )

    backups = list(tmp_path.glob("tools.afterthread-v5-backup-*"))
    assert len(backups) == 1
    assert _snapshot_tree(backups[0] / "alpha") == old_snapshot
    assert str(backups[0]) in "\n".join(output)


def test_dry_run_reports_env_key_names_never_values_and_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "tools"
    _make_package(root, env=b"API_KEY=do-not-print\nexport MODE = old\n")
    before = _snapshot_tree(tmp_path)
    output: list[str] = []

    assert _run(root, dry_run=True, output=output) == 0

    text = "\n".join(output)
    assert "API_KEY" in text
    assert "MODE" in text
    assert "do-not-print" not in text
    assert "override defaults" in text
    assert _snapshot_tree(tmp_path) == before


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("label", _JOURNAL_LABELS)
def test_each_forward_journal_write_side_recovers(tmp_path: Path, label: str, side: str) -> None:
    """Every forward journal generation survives process loss on either side."""

    root = tmp_path / "tools"
    _make_package(root)
    operations = PointFailureOperations({f"{side}:{label}": InjectedCrash()})

    with pytest.raises(InjectedCrash):
        _run(root, operations=operations)

    # A failure before initialize has a complete backup but no journal.  Use real
    # randomness on that fresh second run so it cannot collide with the first
    # backup's deliberately deterministic suffix.
    token_hex = (
        migration.secrets.token_hex
        if label == "journal:initialize" and side == "before"
        else (lambda _size: "abcdef")
    )
    assert _run(root, token_hex=token_hex) == 0
    _assert_migrated(root)


@pytest.mark.parametrize(
    "point",
    [
        "after:rename:alpha:publish_shell",
        "after:rename:alpha:park_old",
        "after:rename:alpha:activate_new",
    ],
)
def test_interruption_after_each_rename_rerun_completes(tmp_path: Path, point: str) -> None:
    root = tmp_path / "tools"
    _make_package(root)

    with pytest.raises(InjectedCrash):
        _run(root, operations=PointFailureOperations({point: InjectedCrash()}))

    assert _run(root) == 0
    _assert_migrated(root)


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize(
    "label",
    [
        "assemble:alpha",
        "copy_env:alpha",
        "rename:alpha:publish_shell",
        "rename:alpha:park_old",
        "rename:alpha:activate_new",
    ],
)
def test_every_filesystem_step_is_individually_interruptible(
    tmp_path: Path, label: str, side: str
) -> None:
    root = tmp_path / "tools"
    _make_package(root)

    with pytest.raises(InjectedCrash):
        _run(
            root,
            operations=PointFailureOperations({f"{side}:{label}": InjectedCrash()}),
        )

    assert _run(root) == 0
    _assert_migrated(root)


@pytest.mark.parametrize(
    ("journal_point", "rerun_result"),
    [
        ("before:journal:rolling_back", 0),
        ("after:journal:rolling_back", 1),
        ("before:journal:rolled_back", 1),
        ("after:journal:rolled_back", 1),
    ],
)
def test_each_rollback_journal_write_side_recovers(
    tmp_path: Path, journal_point: str, rerun_result: int
) -> None:
    """Conditional rollback records get the same two-sided crash matrix."""

    root = tmp_path / "tools"
    _make_package(root, "alpha")
    _make_package(root, "bravo")
    operations = PointFailureOperations(
        {
            "before:copy_env:bravo": OSError("injected copy failure"),
            journal_point: InjectedCrash(),
        }
    )

    with pytest.raises(InjectedCrash):
        _run(root, operations=operations)

    assert _run(root) == rerun_result
    if rerun_result == 0:
        _assert_migrated(root, "alpha")
        _assert_migrated(root, "bravo")
    else:
        for name in ("alpha", "bravo"):
            assert (root / name / "tool.json").is_file()
            assert not (root / name / "versions").exists()
        assert not (root / migration._JOURNAL_FILENAME).exists()


def test_committed_cleanup_failure_never_attempts_rollback_and_retries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tools"
    _make_package(root)
    first = PointFailureOperations(
        {"before:cleanup:alpha:premigrate": OSError("injected cleanup failure")}
    )

    assert _run(root, operations=first) == 1

    journal = json.loads((root / migration._JOURNAL_FILENAME).read_text(encoding="utf-8"))
    assert journal["status"] == "committed"
    assert migration._is_new_package_at(root / "alpha")
    assert (root / "alpha.at-premigrate").is_dir()

    seen: list[str] = []
    guard = PointFailureOperations({}, seen=seen)
    assert _run(root, operations=guard) == 0
    assert not any("rollback" in point for point in seen)
    _assert_migrated(root)


def test_env_bytes_and_nondefault_mode_are_identical_after_migration(tmp_path: Path) -> None:
    root = tmp_path / "tools"
    content = b"\xffbinary\r\nAPI_KEY='literal bytes'\r\n"
    _make_package(root, env=content, env_mode=0o604)

    assert _run(root) == 0

    carried = root / "alpha" / ".env"
    assert carried.read_bytes() == content
    assert stat.S_IMODE(carried.stat().st_mode) == 0o604


def test_injected_env_copy_failure_rolls_every_package_back_to_flat_layout(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tools"
    alpha = _make_package(root, "alpha", env=b"ALPHA=one\n")
    bravo = _make_package(root, "bravo", env=b"BRAVO=two\n")
    alpha_before = _snapshot_tree(alpha)
    bravo_before = _snapshot_tree(bravo)
    operations = PointFailureOperations(
        {"before:copy_env:bravo": OSError("injected .env copy failure")}
    )

    assert _run(root, operations=operations) == 1

    assert _snapshot_tree(root / "alpha") == alpha_before
    assert _snapshot_tree(root / "bravo") == bravo_before
    assert not (root / migration._JOURNAL_FILENAME).exists()
    assert not list(root.glob("*.at-premigrate"))
    assert not list(root.glob("*.at-migrated"))


@pytest.mark.parametrize(
    ("state", "manifest_enabled", "expected_enabled", "foreign_survives"),
    [
        ("ours", True, False, False),
        ("foreign", False, False, True),
        ("absent", True, True, False),
    ],
)
def test_state_ours_foreign_and_absent_choose_the_exact_enabled_precedence(
    tmp_path: Path,
    state: str,
    manifest_enabled: bool,
    expected_enabled: bool,
    foreign_survives: bool,
) -> None:
    root = tmp_path / "tools"
    package = _make_package(root, state=state, enabled=manifest_enabled)
    original_foreign = (
        (package / migration._STATE_FILENAME).read_bytes() if foreign_survives else None
    )

    assert _run(root) == 0

    version = _assert_migrated(root)
    state_document = json.loads(
        (root / "alpha" / ".afterthread.meta" / "state.json").read_text(encoding="utf-8")
    )
    assert state_document["enabled"] is expected_enabled
    foreign_target = version / migration._STATE_FILENAME
    if foreign_survives:
        assert foreign_target.read_bytes() == original_foreign
        assert stat.S_IMODE(foreign_target.stat().st_mode) == 0o604
    else:
        assert not foreign_target.exists()


def test_unreadable_state_is_refused_before_any_write(tmp_path: Path) -> None:
    root = tmp_path / "tools"
    _make_package(root, state="unreadable")
    before = _snapshot_tree(tmp_path)
    output: list[str] = []

    assert _run(root, output=output) == 1

    assert _snapshot_tree(tmp_path) == before
    assert "state=UNREADABLE" in "\n".join(output)


def test_valid_ai_meta_is_split_into_origin_and_summary(tmp_path: Path) -> None:
    root = tmp_path / "tools"
    _make_package(root, ai_meta="valid")

    assert _run(root) == 0

    version = _assert_migrated(root)
    origin = json.loads((version / ".afterthread.meta" / "origin.json").read_text(encoding="utf-8"))
    assert origin == {
        "source": "legacy-ai-meta",
        "openapi_url": "https://kb.example/openapi.json",
        "instructions": "Install the KB lookup tool.",
        "previous": None,
    }
    summary = json.loads(
        (version / ".afterthread.meta" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary == {
        "summary": "A generated summary",
        "updated_at": "2026-07-27T10:20:30+00:00",
        "llm_log_id": 17,
        "llm_log_process": "process-token",
    }
    assert not (version / migration._AI_META_FILENAME).exists()


@pytest.mark.parametrize("ai_meta", ["invalid", "unreadable"])
def test_unparseable_or_unreadable_ai_meta_is_refused_untouched(
    tmp_path: Path, ai_meta: str
) -> None:
    root = tmp_path / "tools"
    _make_package(root, ai_meta=ai_meta)
    before = _snapshot_tree(tmp_path)
    output: list[str] = []

    assert _run(root, output=output) == 1

    assert _snapshot_tree(tmp_path) == before
    assert "ai_meta=UNREADABLE" in "\n".join(output)


@pytest.mark.parametrize("owned_name", ["versions", ".afterthread.meta"])
def test_operator_owned_target_layout_name_is_refused_without_writes(
    tmp_path: Path, owned_name: str
) -> None:
    root = tmp_path / "tools"
    package = _make_package(root)
    (package / owned_name).mkdir()
    (package / owned_name / "operator.txt").write_text("mine", encoding="utf-8")
    before = _snapshot_tree(tmp_path)

    assert _run(root) == 1

    assert _snapshot_tree(tmp_path) == before


@pytest.mark.parametrize("owned_name", ["versions", ".afterthread.meta"])
def test_broken_symlink_at_target_layout_name_is_still_operator_owned(
    tmp_path: Path, owned_name: str
) -> None:
    root = tmp_path / "tools"
    package = _make_package(root)
    (package / owned_name).symlink_to(tmp_path / "missing-operator-target")
    before = _snapshot_tree(tmp_path)

    assert _run(root) == 1

    assert _snapshot_tree(tmp_path) == before


@pytest.mark.parametrize(
    "sibling_name",
    ["alpha.at-migrated", "alpha.at-premigrate", ".alpha.at-migration-shell"],
)
def test_deterministic_sibling_without_journal_is_operator_owned_and_refused(
    tmp_path: Path, sibling_name: str
) -> None:
    root = tmp_path / "tools"
    _make_package(root)
    sibling = root / sibling_name
    sibling.mkdir()
    (sibling / "operator.txt").write_text("mine", encoding="utf-8")
    before = _snapshot_tree(tmp_path)

    assert _run(root) == 1

    assert _snapshot_tree(tmp_path) == before


def test_second_run_over_already_migrated_root_is_zero_write_noop(tmp_path: Path) -> None:
    root = tmp_path / "tools"
    _make_package(root)
    assert _run(root) == 0
    before = _snapshot_tree(tmp_path)
    output: list[str] = []

    assert _run(root, output=output) == 0

    assert _snapshot_tree(tmp_path) == before
    assert "nothing to do" in "\n".join(output)


def test_previous_absent_null_and_vid_have_distinct_runtime_and_migration_meanings(
    tmp_path: Path,
) -> None:
    """Fresh preflight accepts real history; journal reconciliation accepts only its null root."""
    root = tmp_path / "tools"
    _make_package(root)
    assert _run(root) == 0
    package = root / "alpha"
    first_vid = _current_vid(root)
    first = package / tools._VERSIONS_DIRNAME / first_vid
    origin_path = first / tools._META_DIRNAME / tools._ORIGIN_FILENAME
    origin = json.loads(origin_path.read_text(encoding="utf-8"))

    explicit_null = tools.resolve_current(tools.PackageRoot(package))
    assert isinstance(explicit_null, tools.Resolved)
    assert isinstance(explicit_null.previous, tools.PreviousNull)
    assert tools.resolution_lineage(explicit_null) == "sole"
    assert migration._is_new_package_at(package) is True
    assert migration._is_new_package_at(package, first_vid) is True

    origin.pop("previous")
    origin_path.write_text(json.dumps(origin), encoding="utf-8")
    absent = tools.resolve_current(tools.PackageRoot(package))
    assert isinstance(absent, tools.Resolved)
    assert isinstance(absent.previous, tools.PreviousAbsent)
    assert tools.resolution_lineage(absent) == "broken"
    assert migration._is_new_package_at(package) is False
    assert migration._is_new_package_at(package, first_vid) is False

    origin["previous"] = None
    origin_path.write_text(json.dumps(origin), encoding="utf-8")
    second_vid = "20260728T020304Z-fedcba"
    second = package / tools._VERSIONS_DIRNAME / second_vid
    shutil.copytree(first, second)
    second_origin_path = second / tools._META_DIRNAME / tools._ORIGIN_FILENAME
    second_origin = json.loads(second_origin_path.read_text(encoding="utf-8"))
    second_origin["previous"] = first_vid
    second_origin_path.write_text(json.dumps(second_origin), encoding="utf-8")
    assert tools.publish_current(tools.PackageRoot(package), second_vid)

    real_vid = tools.resolve_current(tools.PackageRoot(package))
    assert isinstance(real_vid, tools.Resolved)
    assert isinstance(real_vid.previous, tools.PreviousValue)
    assert real_vid.previous.value == first_vid
    assert tools.resolution_lineage(real_vid) == "usable"
    assert migration._is_new_package_at(package) is True
    # An in-progress journal names the migration-created initial vid and must not
    # reconcile a later revision as that exact disk effect.
    assert migration._is_new_package_at(package, second_vid) is False


@pytest.mark.parametrize("damage", ["two-newlines", "origin-without-source"])
def test_target_layout_recognition_agrees_with_runtime_resolution(
    tmp_path: Path, damage: str
) -> None:
    """Migration cannot call a package complete when runtime calls it unresolved."""

    root = tmp_path / "tools"
    _make_package(root)
    assert _run(root) == 0
    package = root / "alpha"
    current = package / tools._META_DIRNAME / tools._CURRENT_FILENAME
    vid = current.read_text(encoding="ascii").rstrip("\n")
    if damage == "two-newlines":
        current.write_text(f"{vid}\n\n", encoding="ascii")
    else:
        origin_path = (
            package / tools._VERSIONS_DIRNAME / vid / tools._META_DIRNAME / tools._ORIGIN_FILENAME
        )
        origin = json.loads(origin_path.read_text(encoding="utf-8"))
        origin.pop("source")
        origin_path.write_text(json.dumps(origin), encoding="utf-8")

    runtime = tools.resolve_current(tools.PackageRoot(package))
    migration_answer = migration._is_new_package_at(package)

    assert isinstance(runtime, tools.Unresolved)
    assert migration_answer is isinstance(runtime, tools.Resolved)
    before = _snapshot_tree(tmp_path)
    assert _run(root) == 1
    assert _snapshot_tree(tmp_path) == before


def test_one_package_preflight_failure_means_nothing_anywhere_is_written(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tools"
    _make_package(root, "alpha")
    _make_package(root, "bravo", ai_meta="invalid")
    before = _snapshot_tree(tmp_path)
    output: list[str] = []

    assert _run(root, output=output) == 1

    assert _snapshot_tree(tmp_path) == before
    assert "alpha" in "\n".join(output)
    assert "bravo" in "\n".join(output)
    assert not list(tmp_path.glob("tools.afterthread-v5-backup-*"))


@pytest.mark.parametrize(
    "journal_bytes",
    [
        b'{"version": ',
        json.dumps(
            {
                "version": 999,
                "status": "running",
                "tools_dir": "ignored",
                "started_at": "ignored",
                "backup": "ignored",
                "packages": [],
            }
        ).encode(),
    ],
)
def test_corrupt_or_unknown_version_journal_stops_without_writes(
    tmp_path: Path, journal_bytes: bytes
) -> None:
    root = tmp_path / "tools"
    _make_package(root)
    (root / migration._JOURNAL_FILENAME).write_bytes(journal_bytes)
    before = _snapshot_tree(tmp_path)

    assert _run(root) == 1

    assert _snapshot_tree(tmp_path) == before
    assert not list(tmp_path.glob("tools.afterthread-v5-backup-*"))


def test_unknown_journal_version_alone_is_enough_to_refuse(tmp_path: Path) -> None:
    """The version gate must refuse on its OWN, not behind another guard.

    The parametrized case above pairs version 999 with placeholder ``tools_dir``,
    ``started_at`` and ``backup`` values, so removing the version check entirely
    still leaves that journal refused by a LATER guard -- the assertion passes
    either way and pins nothing. Measured: with the ``version`` comparison
    disabled, the whole file still passed. This payload is valid in every other
    respect (it is the resumable shape the two-pending test builds from, with one
    package legitimately mid-step), so the version is the only thing left to
    refuse it, and a mutant that drops the gate fails HERE.
    """

    root = tmp_path / "tools"
    _make_package(root, "alpha")
    backup = tmp_path / "full-backup"
    backup.mkdir()
    payload = {
        "version": migration._JOURNAL_VERSION + 1,
        "status": "running",
        "tools_dir": str(root.resolve()),
        "started_at": _START.isoformat(),
        "backup": str(backup),
        "packages": [{"name": "alpha", "vid": _VID, "completed": 0, "pending": "assemble"}],
    }
    (root / migration._JOURNAL_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    before = _snapshot_tree(tmp_path)

    assert _run(root) == 1

    assert _snapshot_tree(tmp_path) == before


def test_committed_journal_with_an_incomplete_package_is_refused(tmp_path: Path) -> None:
    """``committed`` is what licenses "clean up only, never roll back".

    That licence is only sound while committed genuinely implies every package
    swapped. A journal claiming committed with a package still mid-swap would
    send the resume down the cleanup path while that package is still in the old
    flat layout -- the partially migrated root the whole all-or-nothing rule
    exists to prevent, and the one state the new readers cannot describe.
    Measured: removing the refusal left all 68 tests passing, so nothing pinned
    it. Forward execution cannot produce this journal; a hand edit or a restored
    older copy can, which is exactly when fail-closed has to hold.
    """

    root = tmp_path / "tools"
    _make_package(root, "alpha")
    backup = tmp_path / "full-backup"
    backup.mkdir()
    payload = {
        "version": migration._JOURNAL_VERSION,
        "status": "committed",
        "tools_dir": str(root.resolve()),
        "started_at": _START.isoformat(),
        "backup": str(backup),
        "packages": [{"name": "alpha", "vid": _VID, "completed": 2, "pending": None}],
    }
    (root / migration._JOURNAL_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    before = _snapshot_tree(tmp_path)

    assert _run(root) == 1

    assert _snapshot_tree(tmp_path) == before


def test_journal_with_two_pending_actions_is_refused_as_more_than_one_step_ahead(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tools"
    _make_package(root, "alpha")
    _make_package(root, "bravo")
    backup = tmp_path / "full-backup"
    backup.mkdir()
    payload = {
        "version": 1,
        "status": "running",
        "tools_dir": str(root.resolve()),
        "started_at": _START.isoformat(),
        "backup": str(backup),
        "packages": [
            {
                "name": "alpha",
                "vid": _VID,
                "completed": 0,
                "pending": "assemble",
            },
            {
                "name": "bravo",
                "vid": _VID,
                "completed": 0,
                "pending": "assemble",
            },
        ],
    }
    (root / migration._JOURNAL_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    before = _snapshot_tree(tmp_path)

    assert _run(root) == 1

    assert _snapshot_tree(tmp_path) == before


def test_done_journal_with_missing_disk_effect_is_not_silently_replayed(tmp_path: Path) -> None:
    """The done/not-done reconciliation cell refuses, then safely rolls back."""

    root = tmp_path / "tools"
    _make_package(root)
    crash = PointFailureOperations({"after:journal:alpha:assemble:done": InjectedCrash()})
    with pytest.raises(InjectedCrash):
        _run(root, operations=crash)
    shell = root / ".alpha.at-migration-shell"
    assert shell.is_dir()
    # Test-owned destruction constructs the otherwise impossible contradiction:
    # durable done record, absent disk effect.
    shutil.rmtree(shell)

    assert _run(root) == 1
    assert (root / "alpha" / "tool.json").is_file()
    assert not (root / "alpha" / "versions").exists()


def test_declining_the_confirmation_writes_nothing_at_all(tmp_path: Path) -> None:
    """The `.env` key listing is only useful if it can still be acted on.

    Printing the keys and then migrating regardless would put the report AFTER
    the decision point: the operator would learn which values now shadow their
    tools' code defaults only once that was already true. So the prompt is the
    last thing before the first write, and declining leaves the tree byte-identical
    -- no backup, no journal, no shell, nothing.
    """

    root = tmp_path / "tools"
    _make_package(root)
    before = _snapshot_tree(tmp_path)

    assert _run(root, confirm=lambda: False) == 1

    assert _snapshot_tree(tmp_path) == before
    assert not list(tmp_path.glob("tools.afterthread-v5-backup-*"))


def test_an_unaskable_stdin_is_a_no_not_a_default_yes() -> None:
    """A piped or closed stdin must refuse, not proceed.

    Same fail-closed direction as every other refusal here: a run that cannot ask
    has not been authorised, and `--yes` is how a scripted run says it decided.
    """

    def _eof(_prompt: str) -> str:
        raise EOFError

    assert migration._confirm_on_stdin(_eof) is False
    assert migration._confirm_on_stdin(lambda _prompt: "  Y \n") is True
    assert migration._confirm_on_stdin(lambda _prompt: "yes") is True
    assert migration._confirm_on_stdin(lambda _prompt: "") is False
    assert migration._confirm_on_stdin(lambda _prompt: "no") is False


def test_cli_reads_tools_dir_from_application_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _make_package(root)
    monkeypatch.setattr(
        migration,
        "get_settings",
        lambda: SimpleNamespace(tools_dir=str(root)),
    )

    assert migration.main(["--dry-run"]) == 0
    assert not (root / migration._JOURNAL_FILENAME).exists()
