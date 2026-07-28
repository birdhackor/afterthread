"""One-time, offline migration of installed tools to the web-v5 layout.

The journal is the authority for every migration-owned sibling.  A deterministic
name by itself never grants permission to overwrite or remove anything.  With no
journal, preflight refuses ``<name>.at-migrated``,
``<name>.at-premigrate``, ``.<name>.at-premigrate-tombstone``, and
``.<name>.at-migration-shell`` as operator-owned.

Each package has five ordered actions.  ``completed`` is the number of actions
whose durable ``done`` record was published, and ``pending`` is either null or the
ONE action whose write-ahead record was published before its filesystem mutation:

1. ``assemble`` -- build the versioned shell, with ``current`` written last;
2. ``copy_env`` -- carry the package-level ``.env`` byte-for-byte and mode-for-mode;
3. ``publish_shell`` -- rename the shell to ``<name>.at-migrated``;
4. ``park_old`` -- rename ``<name>`` to ``<name>.at-premigrate``;
5. ``activate_new`` -- rename ``<name>.at-migrated`` to ``<name>``.

The reconciliation table below is executable policy, not an informal summary.
“Journal says not done” means the action is the journal's one write-ahead
``pending`` action.  An action with neither ``done`` nor ``pending`` is
unannounced: seeing its disk effect would mean the journal is behind the disk,
which this protocol forbids, so that separate case is a refusal.

| journal says | disk shows | action |
|---|---|---|
| done | done | verify and continue |
| done | not done | stop: the durable journal contradicts the disk |
| not done (pending) | done | publish ``done``; do not repeat the mutation |
| not done (pending) | not done | repeat the idempotent action, then publish ``done`` |
| unannounced | done | stop: the journal is behind, violating write-ahead |
| unannounced | not done | publish ``pending``, perform the action, publish ``done`` |

For the three rename actions, “disk shows done” is monotonic across later steps:
the new shell may be at the shell sibling, migrated sibling, or live package;
the old package remains at ``.at-premigrate`` through commit.  A missing rename
source is therefore accepted only when the expected destination/layout proves
that the rename already happened.  It is never accepted merely because the
source name is absent.

Before ``committed``, an ordinary I/O failure first publishes ``rolling_back``
and then restores old package names using rename only.  Rollback can be resumed
after any interruption and never needs to delete an old or sole copy.  Removal
of migration-owned shells is a later, explicitly journaled maintenance phase.
After every package is active, ``committed`` is published atomically and durably
before the first ``.at-premigrate`` cleanup rename. Once read, ``committed``
permits cleanup only; no error path may attempt rollback. Cleanup first records
the verified old package's directory identity durably, then renames it to its
tombstone. Rename preserves that identity, so the journal can still prove
ownership while ``rmtree`` progressively removes the package's own identifying
files.

Tests inject failures through :class:`MigrationOperations`, at both sides of
every journal publish and filesystem action.  The indirection is deliberately
local to this module: tests name the semantic mutation they interrupt instead
of globally monkeypatching ``os.rename`` and accidentally perturbing pytest,
``shutil``, journal publication, or unrelated code.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from afterthread.config import get_settings
from afterthread.services.tools import (
    _AI_META_FILENAME,
    _AI_META_MAX_BYTES,
    _MANIFEST_MAX_BYTES,
    _STATE_FILENAME,
    _STATE_MARKER_KEY,
    _STATE_MARKER_VALUE,
    _STATE_MAX_BYTES,
    PackageRoot,
    PreviousNull,
    Resolved,
    resolution_lineage,
    resolve_current,
)

_JOURNAL_FILENAME = ".afterthread-migration.json"
_LEGACY_JOURNAL_VERSION = 1
_JOURNAL_VERSION = 2
_JOURNAL_MAX_BYTES = 1024 * 1024
_META_DIRNAME = ".afterthread.meta"
_OWNERSHIP_FILENAME = "migration-owner.json"
_OWNERSHIP_MARKER = "tools-v5-migration-owner"
_VERSIONS_DIRNAME = "versions"
_SHELL_SUFFIX = ".at-migration-shell"
_MIGRATED_SUFFIX = ".at-migrated"
_PREMIGRATE_SUFFIX = ".at-premigrate"
_PREMIGRATE_TOMBSTONE_SUFFIX = ".at-premigrate-tombstone"
_ACTIONS = ("assemble", "copy_env", "publish_shell", "park_old", "activate_new")
_STATUS_VALUES = frozenset({"running", "rolling_back", "rolled_back", "committed"})
_VID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
_PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SIBLING_RE = re.compile(r"^(.+)\.at-(?:migrated|premigrate)$")
_SHELL_RE = re.compile(r"^\.(.+)\.at-migration-shell$")
_PREMIGRATE_TOMBSTONE_RE = re.compile(r"^\.[a-z0-9][a-z0-9_-]{0,63}\.at-premigrate-tombstone$")
_ENV_KEY_RE = re.compile(rb"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=")


class MigrationRefused(Exception):
    """A category-only refusal safe to show to the operator."""


class MigrationOperations:
    """Narrow mutation seam used by the real runner and fault-injection tests.

    Subclasses normally override only :meth:`checkpoint`.  Raising before a
    label simulates failure before the mutation; raising after it simulates a
    process that observed the mutation as complete and then died.
    """

    def checkpoint(self, point: str) -> None:
        """Observe a named side of a mutation; production does nothing."""

    def mutate(self, label: str, action: Callable[[], None]) -> None:
        """Run one semantic mutation between independently injectable boundaries."""

        self.checkpoint(f"before:{label}")
        action()
        self.checkpoint(f"after:{label}")


@dataclass(frozen=True, slots=True)
class LegacyPackage:
    """Everything preflight proved about one flat package."""

    name: str
    root: Path
    manifest: dict[str, Any]
    manifest_mode: int
    state_kind: str
    enabled: bool
    origin: dict[str, Any]
    summary: dict[str, Any] | None
    env_exists: bool
    env_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Preflight:
    """The complete read-only answer for a fresh root."""

    legacy: tuple[LegacyPackage, ...]
    migrated_names: tuple[str, ...]
    package_reports: tuple[PackageReport, ...]
    problems: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PackageReport:
    """Operator-visible classifications, including packages that are refused."""

    name: str
    state_kind: str
    ai_kind: str
    enabled: bool | None
    env_keys: tuple[str, ...]
    migrated: bool = False


def _path_kind(path: Path) -> str:
    """Return a non-secret category for diagnostics without following symlinks."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"
    if stat.S_ISREG(info.st_mode):
        return "file"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    return "special file"


def _read_regular_bytes(path: Path, *, cap: int | None = None) -> bytes:
    """Read a real regular file without following its final component."""

    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise MigrationRefused(f"{path.name} is not readable") from exc
    fd_owned = True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MigrationRefused(f"{path.name} is not a regular file")
        with os.fdopen(fd, "rb") as handle:
            fd_owned = False
            if cap is None:
                return handle.read()
            data = handle.read(cap + 1)
            if len(data) > cap:
                raise MigrationRefused(f"{path.name} exceeds its supported size")
            return data
    except OSError as exc:
        raise MigrationRefused(f"{path.name} is not readable") from exc
    finally:
        if fd_owned:
            os.close(fd)


def _read_json_object(path: Path, *, cap: int) -> tuple[dict[str, Any], int]:
    """Read one strict UTF-8 JSON object and return it with its permission mode."""

    try:
        info = os.lstat(path)
    except OSError as exc:
        raise MigrationRefused(f"{path.name} is not readable") from exc
    data = _read_regular_bytes(path, cap=cap)
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise MigrationRefused(f"{path.name} is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise MigrationRefused(f"{path.name} is not a JSON object")
    return raw, stat.S_IMODE(info.st_mode)


def _env_keys(path: Path) -> tuple[str, ...]:
    """Extract assignment names from raw dotenv lines without decoding values."""

    data = _read_regular_bytes(path)
    names: set[str] = set()
    for line in data.splitlines():
        match = _ENV_KEY_RE.match(line)
        if match is not None:
            names.add(match.group(1).decode("ascii"))
    return tuple(sorted(names))


def _inspect_state(package: Path) -> tuple[str, bool | None, str | None]:
    """Classify ABSENT/OURS/FOREIGN/UNREADABLE exactly as the legacy reader does."""

    path = package / _STATE_FILENAME
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "ABSENT", None, None
    except OSError:
        return "UNREADABLE", None, f"{_STATE_FILENAME} cannot be inspected"
    if not stat.S_ISREG(info.st_mode):
        return "UNREADABLE", None, f"{_STATE_FILENAME} is not a regular file"
    try:
        data = _read_regular_bytes(path, cap=_STATE_MAX_BYTES)
    except MigrationRefused as exc:
        return "UNREADABLE", None, str(exc)
    try:
        raw = json.loads(data.decode("utf-8", errors="replace"))
    except ValueError, RecursionError:
        # The shipped reader treats readable non-JSON as ordinary tool content.
        return "FOREIGN", None, None
    if not isinstance(raw, dict) or raw.get(_STATE_MARKER_KEY) != _STATE_MARKER_VALUE:
        return "FOREIGN", None, None
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        return "UNREADABLE", None, f"{_STATE_FILENAME} has no boolean enabled value"
    return "OURS", enabled, None


def _minimal_origin() -> dict[str, Any]:
    """Committed provenance for a package whose pre-migration source is unknown."""

    return {
        "source": "pre-existing/unknown",
        "openapi_url": None,
        "instructions": None,
        "previous": None,
    }


def _inspect_ai_meta(
    package: Path,
) -> tuple[str, dict[str, Any], dict[str, Any] | None, str | None]:
    """Classify the legacy AI sidecar and split its two target documents."""

    path = package / _AI_META_FILENAME
    try:
        os.lstat(path)
    except FileNotFoundError:
        return "ABSENT", _minimal_origin(), None, None
    except OSError:
        return "UNREADABLE", {}, None, f"{_AI_META_FILENAME} cannot be inspected"
    try:
        raw, _mode = _read_json_object(path, cap=_AI_META_MAX_BYTES)
    except MigrationRefused as exc:
        return "UNREADABLE", {}, None, str(exc)

    summary = raw.get("summary")
    updated_at = raw.get("updated_at")
    log_id = raw.get("llm_log_id")
    log_process = raw.get("llm_log_process")
    origin_raw = raw.get("origin")
    valid_log_id = log_id is None or (isinstance(log_id, int) and not isinstance(log_id, bool))
    valid_origin = origin_raw is None or (
        isinstance(origin_raw, dict)
        and (
            origin_raw.get("openapi_url") is None or isinstance(origin_raw.get("openapi_url"), str)
        )
        and (
            origin_raw.get("instructions") is None
            or isinstance(origin_raw.get("instructions"), str)
        )
    )
    if (
        not isinstance(summary, str)
        or not isinstance(updated_at, str)
        or not valid_log_id
        or (log_process is not None and not isinstance(log_process, str))
        or not valid_origin
    ):
        return (
            "UNREADABLE",
            {},
            None,
            f"{_AI_META_FILENAME} does not match the legacy sidecar schema",
        )

    if isinstance(origin_raw, dict):
        origin = {
            "source": "legacy-ai-meta",
            "openapi_url": origin_raw.get("openapi_url"),
            "instructions": origin_raw.get("instructions"),
            "previous": None,
        }
    else:
        origin = _minimal_origin()
    summary_document = {
        "summary": summary,
        "updated_at": updated_at,
        "llm_log_id": log_id,
        "llm_log_process": log_process,
    }
    return "VALID", origin, summary_document, None


def _tree_problems(package: Path) -> list[str]:
    """Find filesystem nodes a faithful, bounded backup/copy cannot reproduce."""

    problems: list[str] = []

    def onerror(error: OSError) -> None:
        problems.append(f"{package.name}: package tree is not fully readable")

    for dirpath, dirnames, filenames in os.walk(package, followlinks=False, onerror=onerror):
        here = Path(dirpath)
        for name in [*dirnames, *filenames]:
            path = here / name
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                problems.append(f"{package.name}: {path.relative_to(package)} cannot be inspected")
                continue
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                problems.append(
                    f"{package.name}: {path.relative_to(package)} is a special file and cannot "
                    "be migrated safely"
                )
    return problems


def _inspect_legacy_package(
    package: Path,
) -> tuple[LegacyPackage | None, list[str], PackageReport]:
    """Inspect one flat package while collecting independent problems."""

    problems: list[str] = []
    manifest: dict[str, Any] | None = None
    manifest_mode = 0
    try:
        manifest, manifest_mode = _read_json_object(package / "tool.json", cap=_MANIFEST_MAX_BYTES)
    except MigrationRefused as exc:
        problems.append(f"{package.name}: {exc}")

    state_kind, state_enabled, state_error = _inspect_state(package)
    if state_error is not None:
        problems.append(f"{package.name}: {state_error}")

    ai_kind, origin, summary, ai_error = _inspect_ai_meta(package)
    if ai_error is not None:
        problems.append(f"{package.name}: {ai_error}; original bytes were not changed")

    env_path = package / ".env"
    try:
        os.lstat(env_path)
    except FileNotFoundError:
        env_exists = False
        keys: tuple[str, ...] = ()
    except OSError:
        env_exists = False
        keys = ()
        problems.append(f"{package.name}: .env cannot be inspected")
    else:
        env_exists = True
        try:
            keys = _env_keys(env_path)
        except MigrationRefused as exc:
            keys = ()
            problems.append(f"{package.name}: {exc}")

    problems.extend(_tree_problems(package))
    if manifest is not None:
        manifest_name = manifest.get("name")
        if not isinstance(manifest_name, str) or manifest_name != package.name:
            problems.append(f"{package.name}: tool.json name does not match its directory")
        if not _PACKAGE_NAME_RE.fullmatch(package.name):
            problems.append(f"{package.name}: package name is not valid")

    legacy_enabled = True
    if manifest is not None:
        candidate = manifest.get("enabled", True)
        legacy_enabled = candidate if isinstance(candidate, bool) else True
    enabled = (
        state_enabled if state_kind == "OURS" and state_enabled is not None else legacy_enabled
    )
    report = PackageReport(
        name=package.name,
        state_kind=state_kind,
        ai_kind=ai_kind,
        enabled=enabled if manifest is not None or state_enabled is not None else None,
        env_keys=keys,
    )

    if problems or manifest is None:
        return None, problems, report
    return (
        LegacyPackage(
            name=package.name,
            root=package,
            manifest=manifest,
            manifest_mode=manifest_mode,
            state_kind=state_kind,
            enabled=enabled,
            origin=origin,
            summary=summary,
            env_exists=env_exists,
            env_keys=keys,
        ),
        [],
        report,
    )


def _has_our_state_marker(package: Path) -> bool:
    """Recognize the package-level marker used by the target layout."""

    try:
        raw, _mode = _read_json_object(package / _META_DIRNAME / "state.json", cap=_STATE_MAX_BYTES)
    except MigrationRefused:
        return False
    return raw.get(_STATE_MARKER_KEY) == _STATE_MARKER_VALUE and isinstance(
        raw.get("enabled"), bool
    )


def _is_new_package_at(path: Path, vid: str | None = None) -> bool:
    """Return whether ``path`` is a complete, committed target-layout package.

    Runtime resolution is the authority for ``current`` syntax, its one optional
    newline, the real version-directory requirement, and the committed
    ``origin.json`` shape. Migration adds only its ownership/state marker and
    target-layout manifest constraints below.
    """

    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode) or not _has_our_state_marker(path):
        return False
    resolution = resolve_current(PackageRoot(path))
    if not isinstance(resolution, Resolved) or (vid is not None and resolution.vid != vid):
        return False
    try:
        manifest, _manifest_mode = _read_json_object(
            resolution.version_root.path / "tool.json", cap=_MANIFEST_MAX_BYTES
        )
    except MigrationRefused:
        return False
    shell_match = _SHELL_RE.fullmatch(path.name)
    sibling_match = _SIBLING_RE.fullmatch(path.name)
    expected_name = (
        shell_match.group(1)
        if shell_match is not None
        else sibling_match.group(1)
        if sibling_match is not None
        else path.name
    )
    # Journal reconciliation names the one migration-minted vid, whose origin is
    # necessarily the explicit-null initial version. Fresh preflight also sees
    # packages legitimately revised after migration; a usable real predecessor
    # remains a complete target layout. Missing/invalid lineage is broken in both
    # modes and must never be mistaken for an explicit null.
    lineage_complete = (
        isinstance(resolution.previous, PreviousNull)
        if vid is not None
        else resolution_lineage(resolution) in {"sole", "usable"}
    )
    return lineage_complete and "enabled" not in manifest and manifest.get("name") == expected_name


def _fresh_preflight(root: Path) -> Preflight:
    """Walk every package read-only and collect every independently visible problem."""

    legacy: list[LegacyPackage] = []
    migrated: list[str] = []
    package_reports: list[PackageReport] = []
    problems: list[str] = []
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return Preflight((), (), (), (f"{root}: tools directory is not readable",))

    # With no journal, these exact names prove nothing about ownership.  Check them
    # before skipping hidden/non-package entries so an old crash artifact cannot be
    # silently adopted by a fresh run.
    for child in children:
        if (
            _SIBLING_RE.fullmatch(child.name)
            or _SHELL_RE.fullmatch(child.name)
            or _PREMIGRATE_TOMBSTONE_RE.fullmatch(child.name)
        ):
            problems.append(
                f"{child.name}: migration sibling exists without {_JOURNAL_FILENAME}; "
                "it is treated as operator-owned"
            )

    for child in children:
        if child.name.startswith(".") or _SIBLING_RE.fullmatch(child.name):
            continue
        try:
            info = os.lstat(child)
        except OSError:
            problems.append(f"{child.name}: package entry cannot be inspected")
            continue
        if stat.S_ISLNK(info.st_mode):
            # A symlink to a directory is visible to the current scanner, but this
            # migration cannot safely rename/copy through it.
            if child.is_dir():
                problems.append(f"{child.name}: package directory is a symlink")
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue

        # ``exists()`` follows the last component and answers False for a broken
        # symlink. Ownership is about the directory ENTRY at this reserved name,
        # not whether its target currently resolves, so use the lstat-derived kind.
        owns_versions = _path_kind(child / _VERSIONS_DIRNAME) != "absent"
        owns_meta = _path_kind(child / _META_DIRNAME) != "absent"
        if owns_versions or owns_meta:
            if _is_new_package_at(child):
                migrated.append(child.name)
                try:
                    os.lstat(child / ".env")
                except FileNotFoundError:
                    keys: tuple[str, ...] = ()
                except OSError:
                    keys = ()
                    problems.append(f"{child.name}: .env cannot be inspected")
                else:
                    try:
                        keys = _env_keys(child / ".env")
                    except MigrationRefused as exc:
                        keys = ()
                        problems.append(f"{child.name}: {exc}")
                package_reports.append(
                    PackageReport(
                        name=child.name,
                        state_kind="TARGET",
                        ai_kind="TARGET",
                        enabled=None,
                        env_keys=keys,
                        migrated=True,
                    )
                )
            else:
                marker_note = (
                    "our state marker is absent"
                    if not _has_our_state_marker(child)
                    else "our marker exists but the target layout is incomplete"
                )
                problems.append(
                    f"{child.name}: owns {_VERSIONS_DIRNAME}/ or {_META_DIRNAME}/; "
                    f"{marker_note}, so it will not be touched"
                )
            continue

        package, package_problems, report = _inspect_legacy_package(child)
        package_reports.append(report)
        problems.extend(package_problems)
        if package is not None:
            legacy.append(package)

    return Preflight(
        tuple(legacy),
        tuple(migrated),
        tuple(package_reports),
        tuple(problems),
    )


def _print_preflight(preflight: Preflight, output: Callable[[str], None]) -> None:
    """Print classifications and dotenv key names, never dotenv values."""

    legacy_by_name = {package.name: package for package in preflight.legacy}
    migrated = set(preflight.migrated_names)
    output("Preflight report:")
    for report in preflight.package_reports:
        name = report.name
        package = legacy_by_name.get(name)
        if package is not None or not report.migrated:
            enabled = str(report.enabled).lower() if report.enabled is not None else "unknown"
            output(
                f"  {name}: state={report.state_kind}, ai_meta={report.ai_kind}, enabled={enabled}"
            )
        elif name in migrated:
            output(f"  {name}: already migrated")
        key_text = ", ".join(report.env_keys) if report.env_keys else "(none detected)"
        output(f"    .env key names: {key_text}")
    output(
        "  Note: these .env keys now override defaults in the tool's code; "
        "delete stale keys you no longer want."
    )
    if preflight.problems:
        output("Preflight refused the migration:")
        for problem in preflight.problems:
            output(f"  - {problem}")


def _fsync_directory(path: Path) -> None:
    """Persist directory entries at one already-created directory."""

    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(root: Path) -> None:
    """Make copied regular files and their containing directory entries durable."""

    directories: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        directories.append(here)
        for filename in filenames:
            path = here / filename
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                continue
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _write_shell_file(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write one deterministic shell file durably; retry safely truncates it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    fd_owned = True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MigrationRefused(f"{path}: migration destination is not a regular file")
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd_owned = False
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd_owned:
            os.close(fd)
    _fsync_directory(path.parent)


def _json_bytes(document: dict[str, Any]) -> bytes:
    """Stable, human-readable JSON for every target-layout metadata file."""

    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _copy_content_path(source: Path, destination: Path) -> None:
    """Copy one ordinary tool-content entry without following symlinks."""

    info = os.lstat(source)
    if stat.S_ISDIR(info.st_mode):
        shutil.copytree(
            source,
            destination,
            symlinks=True,
            copy_function=shutil.copy2,
        )
    elif stat.S_ISLNK(info.st_mode):
        destination.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
        with contextlib.suppress(NotImplementedError, OSError):
            shutil.copystat(source, destination, follow_symlinks=False)
    elif stat.S_ISREG(info.st_mode):
        shutil.copy2(source, destination, follow_symlinks=False)
    else:
        raise MigrationRefused(f"{source}: special file cannot be copied safely")


def _shell_path(root: Path, name: str) -> Path:
    return root / f".{name}{_SHELL_SUFFIX}"


def _migrated_path(root: Path, name: str) -> Path:
    return root / f"{name}{_MIGRATED_SUFFIX}"


def _premigrate_path(root: Path, name: str) -> Path:
    return root / f"{name}{_PREMIGRATE_SUFFIX}"


def _premigrate_tombstone_path(root: Path, name: str) -> Path:
    return root / f".{name}{_PREMIGRATE_TOMBSTONE_SUFFIX}"


def _directory_identity(path: Path) -> tuple[int, int] | None:
    """Identify one real directory without following its final component."""

    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    return info.st_dev, info.st_ino


def _has_ownership_marker(path: Path, *, name: str, vid: str, role: str) -> bool:
    """Recognize one journal-bound marker without following a foreign entry."""

    try:
        raw, _mode = _read_json_object(
            path / _META_DIRNAME / _OWNERSHIP_FILENAME,
            cap=_STATE_MAX_BYTES,
        )
    except MigrationRefused:
        return False
    return raw == {
        "afterthread": _OWNERSHIP_MARKER,
        "name": name,
        "role": role,
        "vid": vid,
    }


def _ensure_ownership_marker(path: Path, *, name: str, vid: str, role: str) -> None:
    """Create one marker, or accept only the exact marker this journal owns."""

    marker = path / _META_DIRNAME / _OWNERSHIP_FILENAME
    if marker.exists() or marker.is_symlink():
        if _has_ownership_marker(path, name=name, vid=vid, role=role):
            return
        raise MigrationRefused(f"{path.name}: migration ownership marker is foreign")
    _write_shell_file(
        marker,
        _json_bytes(
            {
                "afterthread": _OWNERSHIP_MARKER,
                "name": name,
                "role": role,
                "vid": vid,
            }
        ),
    )


def _record_directory_identity(
    root: Path,
    journal: dict[str, Any],
    package_record: dict[str, Any],
    field: str,
    path: Path,
    *,
    label: str,
    operations: MigrationOperations,
) -> tuple[int, int]:
    """Publish the inode proof that licenses later removal of one directory."""

    identity = _directory_identity(path)
    if identity is None:
        raise MigrationRefused(f"{path.name}: migration cannot identify its directory")
    package_record[field] = list(identity)
    _atomic_publish_journal(root, journal, label=label, operations=operations)
    return identity


def _assemble_shell(
    root: Path,
    package: LegacyPackage,
    package_record: dict[str, Any],
    journal: dict[str, Any],
    operations: MigrationOperations,
) -> None:
    """Build a complete hidden shell; ``current`` is its final completion marker."""

    shell = _shell_path(root, package.name)
    vid = package_record["vid"]
    if shell.exists() or shell.is_symlink():
        if _is_new_package_at(shell, vid):
            if package_record["shell_identity"] is None:
                _record_directory_identity(
                    root,
                    journal,
                    package_record,
                    "shell_identity",
                    shell,
                    label=f"{package.name}:assemble:ownership",
                    operations=operations,
                )
            return
        if not _has_ownership_marker(shell, name=package.name, vid=vid, role="shell"):
            raise MigrationRefused(f"{shell.name}: incomplete shell has no journal ownership proof")
        shell_identity = package_record["shell_identity"]
        if shell_identity is None:
            shell_identity = _record_directory_identity(
                root,
                journal,
                package_record,
                "shell_identity",
                shell,
                label=f"{package.name}:assemble:ownership",
                operations=operations,
            )
        _remove_owned_tree(
            shell,
            require_identity=tuple(shell_identity),
            require_marker=(package.name, vid, "shell"),
        )
    shell.mkdir(mode=0o700)
    _ensure_ownership_marker(shell, name=package.name, vid=vid, role="shell")
    _record_directory_identity(
        root,
        journal,
        package_record,
        "shell_identity",
        shell,
        label=f"{package.name}:assemble:ownership",
        operations=operations,
    )
    version = shell / _VERSIONS_DIRNAME / vid
    version.mkdir(parents=True)

    excluded = {".env", _AI_META_FILENAME}
    if package.state_kind == "OURS":
        excluded.add(_STATE_FILENAME)
    for source in sorted(package.root.iterdir(), key=lambda path: path.name):
        if source.name in excluded:
            continue
        _copy_content_path(source, version / source.name)

    manifest = dict(package.manifest)
    manifest.pop("enabled", None)
    _write_shell_file(version / "tool.json", _json_bytes(manifest), package.manifest_mode)

    version_meta = version / _META_DIRNAME
    version_meta.mkdir()
    _write_shell_file(version_meta / "origin.json", _json_bytes(package.origin))
    if package.summary is not None:
        _write_shell_file(version_meta / "summary.json", _json_bytes(package.summary))

    package_meta = shell / _META_DIRNAME
    state_document = {
        _STATE_MARKER_KEY: _STATE_MARKER_VALUE,
        "enabled": package.enabled,
    }
    _write_shell_file(package_meta / "state.json", _json_bytes(state_document))

    # Everything the pointer names, including origin.json (the committed marker),
    # is durable before current becomes visible inside the shell.
    _fsync_tree(shell)
    _write_shell_file(package_meta / "current", f"{vid}\n".encode())
    _fsync_tree(shell)


def _copy_env_file(source_root: Path, destination_root: Path) -> None:
    """Carry ``.env`` byte-for-byte and preserve exactly its permission bits."""

    source = source_root / ".env"
    try:
        source_info = os.lstat(source)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise MigrationRefused(".env cannot be inspected for copying") from exc
    if not stat.S_ISREG(source_info.st_mode):
        raise MigrationRefused(".env is not a regular file")

    destination = destination_root / ".env"
    try:
        destination_info = os.lstat(destination)
    except FileNotFoundError:
        destination_info = None
    except OSError as exc:
        raise MigrationRefused("destination .env cannot be inspected") from exc
    if destination_info is not None and not stat.S_ISREG(destination_info.st_mode):
        raise MigrationRefused("destination .env is not a regular file")

    source_flags = os.O_RDONLY | os.O_NONBLOCK
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
        destination_flags |= os.O_NOFOLLOW
    source_fd = os.open(source, source_flags)
    destination_fd = -1
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise MigrationRefused(".env changed into a non-regular file")
        mode = stat.S_IMODE(source_info.st_mode)
        destination_fd = os.open(destination, destination_flags, mode)
        if not stat.S_ISREG(os.fstat(destination_fd).st_mode):
            raise MigrationRefused("destination .env changed into a non-regular file")
        os.fchmod(destination_fd, mode)
        while chunk := os.read(source_fd, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
    finally:
        os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)
    _fsync_directory(destination_root)


def _atomic_publish_journal(
    root: Path,
    journal: dict[str, Any],
    *,
    label: str,
    operations: MigrationOperations,
) -> None:
    """Atomically publish and durably persist one complete journal generation."""

    data = _json_bytes(journal)
    if len(data) > _JOURNAL_MAX_BYTES:
        raise MigrationRefused("migration journal would exceed its size limit")
    path = root / _JOURNAL_FILENAME

    def publish() -> None:
        fd, tmp_name = tempfile.mkstemp(
            dir=root,
            prefix=f"{_JOURNAL_FILENAME}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        tmp_info = os.fstat(fd)
        tmp_identity = tmp_info.st_dev, tmp_info.st_ino
        fd_owned = True
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd_owned = False
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            _fsync_directory(root)
        finally:
            if fd_owned:
                os.close(fd)
            try:
                remaining = os.lstat(tmp_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise MigrationRefused(
                    f"{tmp_path.name}: journal temporary cannot be inspected for cleanup"
                ) from exc
            else:
                if (
                    not stat.S_ISREG(remaining.st_mode)
                    or (remaining.st_dev, remaining.st_ino) != tmp_identity
                ):
                    raise MigrationRefused(
                        f"{tmp_path.name}: journal temporary cleanup target is foreign"
                    )
                tmp_path.unlink()

    operations.mutate(f"journal:{label}", publish)


def _validate_journal(raw: dict[str, Any], root: Path) -> dict[str, Any]:
    """Validate the complete versioned schema before trusting any sibling name."""

    expected_keys = {"version", "status", "tools_dir", "started_at", "backup", "packages"}
    if set(raw) != expected_keys:
        raise MigrationRefused("migration journal has an unknown schema")
    version = raw.get("version")
    if version not in {_LEGACY_JOURNAL_VERSION, _JOURNAL_VERSION}:
        raise MigrationRefused("migration journal has an unknown version")
    if raw.get("status") not in _STATUS_VALUES:
        raise MigrationRefused("migration journal has an unknown status")
    if raw.get("tools_dir") != str(root):
        raise MigrationRefused("migration journal belongs to a different tools directory")
    if not isinstance(raw.get("started_at"), str):
        raise MigrationRefused("migration journal has an invalid start time")
    backup = raw.get("backup")
    if not isinstance(backup, str) or not Path(backup).is_dir():
        raise MigrationRefused("migration journal's full backup is missing")
    packages = raw.get("packages")
    if not isinstance(packages, list) or not packages:
        raise MigrationRefused("migration journal has no package plan")
    names: set[str] = set()
    first_incomplete: int | None = None
    pending_count = 0
    for package in packages:
        legacy_keys = {
            "name",
            "vid",
            "completed",
            "pending",
        }
        current_keys = legacy_keys | {"shell_identity", "premigrate_identity"}
        expected_package_keys = legacy_keys if version == _LEGACY_JOURNAL_VERSION else current_keys
        if not isinstance(package, dict) or set(package) != expected_package_keys:
            raise MigrationRefused("migration journal has an invalid package record")
        name = package.get("name")
        vid = package.get("vid")
        completed = package.get("completed")
        pending = package.get("pending")
        shell_identity = package.get("shell_identity")
        premigrate_identity = package.get("premigrate_identity")

        def valid_identity(value: object) -> bool:
            return value is None or (
                isinstance(value, list)
                and len(value) == 2
                and all(
                    isinstance(part, int) and not isinstance(part, bool) and part >= 0
                    for part in value
                )
            )

        if (
            not isinstance(name, str)
            or not _PACKAGE_NAME_RE.fullmatch(name)
            or name in names
            or not isinstance(vid, str)
            or not _VID_RE.fullmatch(vid)
            or not isinstance(completed, int)
            or isinstance(completed, bool)
            or completed < 0
            or completed > len(_ACTIONS)
            or (pending is not None and pending not in _ACTIONS)
            or not valid_identity(shell_identity)
            or not valid_identity(premigrate_identity)
        ):
            raise MigrationRefused("migration journal has an invalid package value")
        if pending is not None and (completed >= len(_ACTIONS) or pending != _ACTIONS[completed]):
            raise MigrationRefused("migration journal pending action is out of order")
        if pending is not None:
            pending_count += 1
        if completed < len(_ACTIONS) and first_incomplete is None:
            first_incomplete = len(names)
        elif first_incomplete is not None and (completed != 0 or pending is not None):
            # Forward execution is package-serial. Once one package is
            # incomplete, every later package must still be wholly untouched.
            raise MigrationRefused("migration journal package progress is out of order")
        if version == _LEGACY_JOURNAL_VERSION:
            package["shell_identity"] = None
            package["premigrate_identity"] = None
        names.add(name)
    if pending_count > 1:
        raise MigrationRefused("migration journal is more than one action ahead")
    if raw["status"] == "committed" and first_incomplete is not None:
        raise MigrationRefused("committed migration journal has incomplete packages")
    raw["version"] = _JOURNAL_VERSION
    return raw


def _read_journal(root: Path) -> dict[str, Any]:
    """Read a journal fail-closed; corruption never falls through to a fresh run."""

    path = root / _JOURNAL_FILENAME
    try:
        raw, _mode = _read_json_object(path, cap=_JOURNAL_MAX_BYTES)
    except MigrationRefused as exc:
        raise MigrationRefused(f"{_JOURNAL_FILENAME} is unreadable: {exc}") from exc
    return _validate_journal(raw, root)


def _old_package_at(path: Path) -> bool:
    """Recognize enough of a flat package for guarded rollback/cleanup."""

    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not _has_our_state_marker(path)
        and _path_kind(path / "tool.json") == "file"
    )


def _new_location(root: Path, name: str, vid: str) -> Path | None:
    """Find the unique target-layout copy across the three forward locations."""

    candidates = (
        _shell_path(root, name),
        _migrated_path(root, name),
        root / name,
    )
    matches = [path for path in candidates if _is_new_package_at(path, vid)]
    return matches[0] if len(matches) == 1 else None


def _same_env(old_root: Path, new_root: Path) -> bool:
    """Measure invariant L directly from the two real files."""

    old = old_root / ".env"
    new = new_root / ".env"
    old_kind = _path_kind(old)
    new_kind = _path_kind(new)
    if old_kind == "absent":
        return new_kind == "absent"
    if old_kind != "file" or new_kind != "file":
        return False
    try:
        old_info = os.lstat(old)
        new_info = os.lstat(new)
        return stat.S_IMODE(old_info.st_mode) == stat.S_IMODE(
            new_info.st_mode
        ) and _read_regular_bytes(old) == _read_regular_bytes(new)
    except OSError, MigrationRefused:
        return False


def _copy_env_is_absent_noop(root: Path, package: dict[str, Any]) -> bool:
    """Return whether copy_env has no disk effect because both files are absent."""

    name = package["name"]
    old_live = root / name
    premigrate = _premigrate_path(root, name)
    old = premigrate if _old_package_at(premigrate) else old_live
    new = _new_location(root, name, package["vid"])
    return (
        new is not None
        and _old_package_at(old)
        and _path_kind(old / ".env") == "absent"
        and _path_kind(new / ".env") == "absent"
    )


def _disk_step_done(root: Path, package: dict[str, Any], action: str) -> bool:
    """Return the monotonic disk predicate for one journal action."""

    name = package["name"]
    vid = package["vid"]
    live = root / name
    migrated = _migrated_path(root, name)
    premigrate = _premigrate_path(root, name)
    new = _new_location(root, name, vid)

    if action == "assemble":
        return new is not None
    if action == "copy_env":
        old = premigrate if _old_package_at(premigrate) else live
        return new is not None and _old_package_at(old) and _same_env(old, new)
    if action == "publish_shell":
        return _is_new_package_at(migrated, vid) or _is_new_package_at(live, vid)
    if action == "park_old":
        return _old_package_at(premigrate) and (
            _is_new_package_at(migrated, vid) or _is_new_package_at(live, vid)
        )
    if action == "activate_new":
        return _is_new_package_at(live, vid) and _old_package_at(premigrate)
    raise AssertionError(f"unknown migration action: {action}")


def _load_legacy_for_resume(root: Path, name: str) -> LegacyPackage:
    """Re-read a still-flat source when an interrupted assembly/copy must resume."""

    package, problems, _report = _inspect_legacy_package(root / name)
    if package is None:
        detail = "; ".join(problems) if problems else "flat package is missing"
        raise MigrationRefused(f"{name}: cannot resume before swap: {detail}")
    return package


def _rename_durable(source: Path, destination: Path, root: Path) -> None:
    """Rename one sibling and persist the containing directory entry."""

    os.rename(source, destination)
    _fsync_directory(root)


def _perform_action(
    root: Path,
    journal: dict[str, Any],
    package_record: dict[str, Any],
    action: str,
    *,
    plans: dict[str, LegacyPackage],
    operations: MigrationOperations,
) -> None:
    """Perform one idempotent action after its pending record is durable."""

    name = package_record["name"]
    vid = package_record["vid"]
    live = root / name
    shell = _shell_path(root, name)
    migrated = _migrated_path(root, name)
    premigrate = _premigrate_path(root, name)

    if action == "assemble":
        plan = plans.get(name) or _load_legacy_for_resume(root, name)
        operations.mutate(
            f"assemble:{name}",
            lambda: _assemble_shell(root, plan, package_record, journal, operations),
        )
        return
    if action == "copy_env":
        plan = plans.get(name) or _load_legacy_for_resume(root, name)
        operations.mutate(
            f"copy_env:{name}",
            lambda: _copy_env_file(plan.root, shell),
        )
        return
    if action == "publish_shell":
        if _is_new_package_at(migrated, vid):
            return
        if not _is_new_package_at(shell, vid) or migrated.exists() or migrated.is_symlink():
            raise MigrationRefused(f"{name}: cannot reconcile shell publication")
        operations.mutate(
            f"rename:{name}:publish_shell",
            lambda: _rename_durable(shell, migrated, root),
        )
        return
    if action == "park_old":
        if _old_package_at(premigrate):
            return
        if not _old_package_at(live) or premigrate.exists() or premigrate.is_symlink():
            raise MigrationRefused(f"{name}: cannot reconcile old-package parking")
        operations.mutate(
            f"rename:{name}:park_old",
            lambda: _rename_durable(live, premigrate, root),
        )
        return
    if action == "activate_new":
        if _is_new_package_at(live, vid) and _old_package_at(premigrate):
            return
        if (
            live.exists()
            or live.is_symlink()
            or not _is_new_package_at(migrated, vid)
            or not _old_package_at(premigrate)
        ):
            raise MigrationRefused(f"{name}: cannot reconcile new-package activation")
        operations.mutate(
            f"rename:{name}:activate_new",
            lambda: _rename_durable(migrated, live, root),
        )
        return
    raise AssertionError(f"unknown migration action: {action}")


def _publish_pending(
    root: Path,
    journal: dict[str, Any],
    package: dict[str, Any],
    action: str,
    operations: MigrationOperations,
) -> None:
    package["pending"] = action
    _atomic_publish_journal(
        root,
        journal,
        label=f"{package['name']}:{action}:pending",
        operations=operations,
    )


def _publish_done(
    root: Path,
    journal: dict[str, Any],
    package: dict[str, Any],
    action: str,
    operations: MigrationOperations,
) -> None:
    package["completed"] += 1
    package["pending"] = None
    _atomic_publish_journal(
        root,
        journal,
        label=f"{package['name']}:{action}:done",
        operations=operations,
    )


def _resume_forward(
    root: Path,
    journal: dict[str, Any],
    *,
    plans: dict[str, LegacyPackage],
    operations: MigrationOperations,
) -> None:
    """Reconcile every action according to the module-level table, then commit."""

    for package in journal["packages"]:
        completed = package["completed"]
        for index in range(completed):
            action = _ACTIONS[index]
            if not _disk_step_done(root, package, action):
                raise MigrationRefused(
                    f"{package['name']}: journal says {action} is done but disk does not"
                )

        while package["completed"] < len(_ACTIONS):
            action = _ACTIONS[package["completed"]]
            pending = package["pending"]
            disk_done = _disk_step_done(root, package, action)
            if pending is None:
                # copy_env for a package with no .env is intentionally a no-op, so
                # ONLY measured absence on both sides excuses a completed disk
                # predicate. Equal files are a real copy and therefore evidence
                # that the disk is ahead of its missing pending journal record.
                no_op_env = action == "copy_env" and _copy_env_is_absent_noop(root, package)
                if disk_done and not no_op_env:
                    raise MigrationRefused(
                        f"{package['name']}: disk is ahead of the journal at {action}"
                    )
                _publish_pending(root, journal, package, action, operations)
            elif pending != action:
                raise MigrationRefused(f"{package['name']}: journal pending action is out of order")

            if not disk_done:
                _perform_action(
                    root,
                    journal,
                    package,
                    action,
                    plans=plans,
                    operations=operations,
                )
                if not _disk_step_done(root, package, action):
                    raise MigrationRefused(
                        f"{package['name']}: {action} returned without its disk effect"
                    )
            _publish_done(root, journal, package, action, operations)

    journal["status"] = "committed"
    _atomic_publish_journal(root, journal, label="committed", operations=operations)


def _publish_rollback_status(
    root: Path,
    journal: dict[str, Any],
    operations: MigrationOperations,
) -> None:
    """Durably choose rollback before moving any live name backward."""

    journal["status"] = "rolling_back"
    _atomic_publish_journal(root, journal, label="rolling_back", operations=operations)


def _rollback_one(
    root: Path,
    package: dict[str, Any],
    operations: MigrationOperations,
) -> None:
    """Restore one flat name using rename only, idempotently."""

    name = package["name"]
    vid = package["vid"]
    live = root / name
    migrated = _migrated_path(root, name)
    premigrate = _premigrate_path(root, name)

    if _is_new_package_at(live, vid):
        if migrated.exists() or migrated.is_symlink():
            raise MigrationRefused(f"{name}: rollback destination already exists")
        operations.mutate(
            f"rollback:{name}:park_new",
            lambda: _rename_durable(live, migrated, root),
        )
    if _old_package_at(premigrate):
        if live.exists() or live.is_symlink():
            raise MigrationRefused(f"{name}: rollback live name is occupied")
        operations.mutate(
            f"rollback:{name}:restore_old",
            lambda: _rename_durable(premigrate, live, root),
        )
    if not _old_package_at(live):
        raise MigrationRefused(f"{name}: rollback could not restore the flat package")


def _resume_rollback(
    root: Path,
    journal: dict[str, Any],
    operations: MigrationOperations,
) -> None:
    """Finish rollback in reverse package order, then record that names are safe."""

    for package in reversed(journal["packages"]):
        _rollback_one(root, package, operations)
    journal["status"] = "rolled_back"
    _atomic_publish_journal(root, journal, label="rolled_back", operations=operations)


def _remove_owned_tree(
    path: Path,
    *,
    require_new_vid: str | None = None,
    require_identity: tuple[int, int] | None = None,
    require_marker: tuple[str, str, str] | None = None,
) -> None:
    """Remove only a real directory whose journal-proven role was re-verified."""

    if not path.exists() and not path.is_symlink():
        return
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise MigrationRefused(f"{path.name}: cleanup target is not a real directory")
    if require_new_vid is None and require_identity is None:
        raise MigrationRefused(f"{path.name}: cleanup target has no journal ownership proof")
    identity_matches = require_identity == (info.st_dev, info.st_ino)
    vid_matches = require_new_vid is not None and _is_new_package_at(path, require_new_vid)
    marker_matches = require_marker is None or _has_ownership_marker(
        path,
        name=require_marker[0],
        vid=require_marker[1],
        role=require_marker[2],
    )
    if not (identity_matches and marker_matches) and not vid_matches:
        if require_identity is None:
            raise MigrationRefused(f"{path.name}: cleanup target is not the journal's new package")
        raise MigrationRefused(
            f"{path.name}: cleanup target does not match its journal ownership proof"
        )
    shutil.rmtree(path)


def _remove_owned_journal(root: Path, journal: dict[str, Any]) -> None:
    """Unlink only the validated journal generation that authorized cleanup."""

    persisted = _read_journal(root)
    if persisted != journal:
        raise MigrationRefused("migration journal changed before cleanup")
    journal_path = root / _JOURNAL_FILENAME
    info = os.lstat(journal_path)
    if not stat.S_ISREG(info.st_mode):
        raise MigrationRefused("migration journal cleanup target is not a regular file")
    journal_path.unlink()
    _fsync_directory(root)


def _cleanup_committed(
    root: Path,
    journal: dict[str, Any],
    operations: MigrationOperations,
) -> None:
    """Retryable maintenance after commit; never calls rollback."""

    for package in journal["packages"]:
        name = package["name"]
        premigrate = _premigrate_path(root, name)
        tombstone = _premigrate_tombstone_path(root, name)

        # ``committed`` chooses cleanup, but does not identify whatever occupies a
        # deterministic tombstone name. Record the verified source directory's
        # inode BEFORE rename. The inode survives both rename and partial rmtree,
        # while a foreign replacement at either spelling fails the proof.
        recorded_identity = package["premigrate_identity"]
        if recorded_identity is None:
            if tombstone.exists() or tombstone.is_symlink():
                raise MigrationRefused(
                    f"{tombstone.name}: cleanup target has no journal ownership proof"
                )
            if not _old_package_at(premigrate):
                raise MigrationRefused(
                    f"{premigrate.name}: committed cleanup target no longer looks like "
                    "the old package"
                )
            _ensure_ownership_marker(
                premigrate,
                name=name,
                vid=package["vid"],
                role="premigrate",
            )
            identity = _record_directory_identity(
                root,
                journal,
                package,
                "premigrate_identity",
                premigrate,
                label=f"{name}:premigrate:ownership",
                operations=operations,
            )
        else:
            identity = tuple(recorded_identity)

        premigrate_exists = premigrate.exists() or premigrate.is_symlink()
        tombstone_exists = tombstone.exists() or tombstone.is_symlink()
        if premigrate_exists and _directory_identity(premigrate) != identity:
            raise MigrationRefused(
                f"{premigrate.name}: cleanup target does not match its journal ownership proof"
            )
        if tombstone_exists and _directory_identity(tombstone) != identity:
            raise MigrationRefused(
                f"{tombstone.name}: cleanup target does not match its journal ownership proof"
            )
        owned_path = premigrate if premigrate_exists else tombstone
        if (premigrate_exists or tombstone_exists) and not _has_ownership_marker(
            owned_path,
            name=name,
            vid=package["vid"],
            role="premigrate",
        ):
            raise MigrationRefused(
                f"{owned_path.name}: cleanup target does not match its journal ownership proof"
            )
        if premigrate_exists and tombstone_exists:
            raise MigrationRefused(f"{tombstone.name}: cleanup destination is already occupied")
        if premigrate_exists:
            if not _old_package_at(premigrate):
                raise MigrationRefused(
                    f"{premigrate.name}: committed cleanup target no longer looks like "
                    "the old package"
                )
            operations.mutate(
                f"cleanup:{name}:premigrate:park",
                lambda source=premigrate, target=tombstone: _rename_durable(source, target, root),
            )
            tombstone_exists = True
        if tombstone_exists:
            operations.mutate(
                f"cleanup:{name}:premigrate:delete",
                lambda path=tombstone, expected=identity, vid=package["vid"], owner=name: (
                    _remove_owned_tree(
                        path,
                        require_identity=expected,
                        require_marker=(owner, vid, "premigrate"),
                    )
                ),
            )
        # These should normally be absent after activation.  A crash can leave a
        # shell only before commit, but validating/removing them here makes cleanup
        # idempotent if a future action arrangement ever leaves one.
        for artifact in (_shell_path(root, name), _migrated_path(root, name)):
            if artifact.exists() or artifact.is_symlink():
                shell_identity = package["shell_identity"]
                operations.mutate(
                    f"cleanup:{name}:{artifact.name}",
                    lambda path=artifact, vid=package["vid"], identity=shell_identity, owner=name: (
                        _remove_owned_tree(
                            path,
                            require_new_vid=vid,
                            require_identity=tuple(identity) if identity is not None else None,
                            require_marker=(owner, vid, "shell"),
                        )
                    ),
                )

    def remove_journal() -> None:
        _remove_owned_journal(root, journal)

    operations.mutate("cleanup:journal", remove_journal)


def _cleanup_rolled_back(
    root: Path,
    journal: dict[str, Any],
    operations: MigrationOperations,
) -> None:
    """Remove only migration-owned new artifacts after every old name is restored."""

    for package in journal["packages"]:
        name = package["name"]
        vid = package["vid"]
        for artifact in (_shell_path(root, name), _migrated_path(root, name)):
            if artifact.exists() or artifact.is_symlink():
                shell_identity = package["shell_identity"]
                # The exact vid proves a complete shell; its recorded inode also
                # proves an incomplete or partially removed one. A failed proof
                # stops cleanup rather than disabling either check.
                operations.mutate(
                    f"cleanup_rollback:{name}:{artifact.name}",
                    lambda path=artifact, expected=vid, identity=shell_identity, owner=name: (
                        _remove_owned_tree(
                            path,
                            require_new_vid=expected,
                            require_identity=tuple(identity) if identity is not None else None,
                            require_marker=(owner, expected, "shell"),
                        )
                    ),
                )
        premigrate = _premigrate_path(root, name)
        if premigrate.exists() or premigrate.is_symlink():
            raise MigrationRefused(
                f"{premigrate.name}: rollback cleanup found an unrestored old package"
            )

    def remove_journal() -> None:
        _remove_owned_journal(root, journal)

    operations.mutate("cleanup_rollback:journal", remove_journal)


def _take_full_backup(
    root: Path,
    *,
    stamp: str,
    token_hex: Callable[[int], str],
    operations: MigrationOperations,
) -> Path:
    """Copy the entire tools directory and durably publish it before journaling."""

    while True:
        suffix = token_hex(3)
        if re.fullmatch(r"[0-9a-f]{6}", suffix):
            backup = root.with_name(f"{root.name}.afterthread-v5-backup-{stamp}-{suffix}")
            if not backup.exists() and not backup.is_symlink():
                break
    partial = backup.with_name(f".{backup.name}.partial")
    if partial.exists() or partial.is_symlink():
        raise MigrationRefused(f"{partial}: backup staging name already exists")

    def copy_backup() -> None:
        shutil.copytree(root, partial, symlinks=True, copy_function=shutil.copy2)
        _fsync_tree(partial)
        os.rename(partial, backup)
        _fsync_directory(root.parent)

    operations.mutate("backup", copy_backup)
    return backup


def _new_journal(
    root: Path,
    packages: tuple[LegacyPackage, ...],
    *,
    started_at: datetime,
    backup: Path,
    token_hex: Callable[[int], str],
) -> dict[str, Any]:
    """Mint one vid per package from the run start plus six random hex digits."""

    stamp = started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    records: list[dict[str, Any]] = []
    for package in packages:
        while True:
            suffix = token_hex(3)
            vid = f"{stamp}-{suffix}"
            if _VID_RE.fullmatch(vid):
                break
        records.append(
            {
                "name": package.name,
                "vid": vid,
                "completed": 0,
                "pending": None,
                "shell_identity": None,
                "premigrate_identity": None,
            }
        )
    return {
        "version": _JOURNAL_VERSION,
        "status": "running",
        "tools_dir": str(root),
        "started_at": started_at.astimezone(UTC).isoformat(),
        "backup": str(backup),
        "packages": records,
    }


def _handle_existing_journal(
    root: Path,
    journal: dict[str, Any],
    *,
    operations: MigrationOperations,
    output: Callable[[str], None],
) -> int:
    """Resume the only phase the durable journal permits."""

    status = journal["status"]
    output(f"Found migration journal in {status!r} state; reconciling it.")
    if status == "committed":
        try:
            _cleanup_committed(root, journal, operations)
        except Exception as exc:
            output(f"Committed migration is intact; cleanup will be retried: {exc}")
            return 1
        output(f"Migration cleanup completed. Full backup: {journal['backup']}")
        return 0
    if status == "rolling_back":
        try:
            _resume_rollback(root, journal, operations)
            _cleanup_rolled_back(root, journal, operations)
        except Exception as exc:
            output(f"Rollback remains retryable and incomplete: {exc}")
            return 1
        output("The interrupted migration was rolled back; the flat layout is restored.")
        return 1
    if status == "rolled_back":
        try:
            _cleanup_rolled_back(root, journal, operations)
        except Exception as exc:
            output(f"Rollback is complete; artifact cleanup will be retried: {exc}")
            return 1
        output("The interrupted migration was rolled back; the flat layout is restored.")
        return 1

    try:
        _resume_forward(root, journal, plans={}, operations=operations)
    except Exception as exc:
        # The exception may be from the AFTER side of the committed journal
        # publish.  Re-read the authority before even considering rollback.
        try:
            persisted = _read_journal(root)
        except MigrationRefused as journal_exc:
            output(f"Migration stopped and journal cannot be reconciled: {journal_exc}")
            return 1
        if persisted["status"] == "committed":
            output(f"Migration committed; no rollback attempted after error: {exc}")
            return 1
        try:
            _publish_rollback_status(root, persisted, operations)
            _resume_rollback(root, persisted, operations)
            _cleanup_rolled_back(root, persisted, operations)
        except Exception as rollback_exc:
            output(
                "Migration stopped before commit; the migration journal remains available "
                "for retry: "
                f"{rollback_exc}"
            )
            return 1
        output(f"Migration failed and all package names were rolled back: {exc}")
        return 1

    try:
        _cleanup_committed(root, journal, operations)
    except Exception as exc:
        output(f"Migration committed; cleanup will be retried and no rollback was attempted: {exc}")
        return 1
    output(f"Migration completed. Full backup: {journal['backup']}")
    return 0


def migrate_tools(
    root: Path,
    *,
    dry_run: bool = False,
    operations: MigrationOperations | None = None,
    output: Callable[[str], None] = print,
    started_at: datetime | None = None,
    token_hex: Callable[[int], str] = secrets.token_hex,
    confirm: Callable[[], bool] = lambda: _confirm_on_stdin(input),
) -> int:
    """Migrate one configured tools root; return a process-style exit status."""

    operations = operations or MigrationOperations()
    root = root.expanduser().resolve()
    journal_path = root / _JOURNAL_FILENAME

    if journal_path.exists() or journal_path.is_symlink():
        if dry_run:
            try:
                journal = _read_journal(root)
            except MigrationRefused as exc:
                output(f"Refusing unreadable migration journal: {exc}")
                return 1
            output(
                f"Dry run: migration journal is {journal['status']!r}; "
                "a real run would reconcile it."
            )
            return 0
        try:
            journal = _read_journal(root)
        except MigrationRefused as exc:
            output(f"Refusing unreadable migration journal: {exc}")
            return 1
        return _handle_existing_journal(
            root,
            journal,
            operations=operations,
            output=output,
        )

    if not root.exists():
        output(f"No tools directory exists at {root}; nothing to migrate.")
        return 0
    if not root.is_dir():
        output(f"Configured tools path is not a directory: {root}")
        return 1

    preflight = _fresh_preflight(root)
    _print_preflight(preflight, output)
    if preflight.problems:
        return 1
    if not preflight.legacy:
        output("All installed packages already use the versioned layout; nothing to do.")
        return 0
    output(
        "Plan: migrate "
        + ", ".join(package.name for package in preflight.legacy)
        + " with one generated vid per package."
    )
    if dry_run:
        output("Dry run complete; no backup, journal, or package file was written.")
        return 0

    # The LAST thing before the first write, and the reason the `.env` key listing
    # above is worth printing at all: migration is the one moment every existing
    # `.env` is in front of an operator who is sitting at the terminal, and those
    # values will shadow any default the tool's own code carries from here on.  A
    # report the operator only reads AFTER the migration has run cannot be acted
    # on, so the confirmation is what turns it from a notice into a decision.
    if not confirm():
        output("Aborted before any write; the tools directory is untouched.")
        return 1

    run_start = started_at or datetime.now(UTC)
    stamp = run_start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        backup = _take_full_backup(
            root,
            stamp=stamp,
            token_hex=token_hex,
            operations=operations,
        )
    except Exception as exc:
        output(f"Could not create the full backup; packages were not changed: {exc}")
        return 1
    output(f"Full backup created before migration writes: {backup}")

    journal = _new_journal(
        root,
        preflight.legacy,
        started_at=run_start,
        backup=backup,
        token_hex=token_hex,
    )
    try:
        _atomic_publish_journal(root, journal, label="initialize", operations=operations)
        _resume_forward(
            root,
            journal,
            plans={package.name: package for package in preflight.legacy},
            operations=operations,
        )
    except Exception as exc:
        # As in resume, an AFTER-commit failure must be classified from the
        # persisted record, never from where the Python stack happened to stop.
        if not journal_path.exists() and not journal_path.is_symlink():
            output(
                "Migration journal initialization failed; package names were not changed "
                f"and the full backup remains at {backup}: {exc}"
            )
            return 1
        try:
            persisted = _read_journal(root)
        except MigrationRefused as journal_exc:
            output(f"Migration stopped; journal requires operator inspection: {journal_exc}")
            return 1
        if persisted["status"] == "committed":
            output(f"Migration committed; no rollback attempted after error: {exc}")
            return 1
        try:
            _publish_rollback_status(root, persisted, operations)
            _resume_rollback(root, persisted, operations)
            _cleanup_rolled_back(root, persisted, operations)
        except Exception as rollback_exc:
            output(
                "Migration stopped before commit; the migration journal remains available "
                "for retry: "
                f"{rollback_exc}"
            )
            return 1
        output(f"Migration failed and all package names were rolled back: {exc}")
        return 1

    try:
        _cleanup_committed(root, journal, operations)
    except Exception as exc:
        output(f"Migration committed; cleanup will be retried and no rollback was attempted: {exc}")
        return 1
    output(f"Migration completed. Full backup: {backup}")
    return 0


def _confirm_on_stdin(read_line: Callable[[str], str]) -> bool:
    """Ask on stdin, and treat anything but an explicit yes as no.

    ``EOFError`` -- a piped or closed stdin -- is a NO rather than a default yes:
    a run that cannot ask must not migrate silently, which is the same fail-closed
    direction every refusal in this module takes.  ``--yes`` is how a scripted run
    says it already decided.
    """

    try:
        answer = read_line("Proceed with the migration? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``python -m afterthread.migrate_tools_v5``."""

    parser = argparse.ArgumentParser(
        description="One-time offline migration of installed tools to the web-v5 layout."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run read-only preflight and print the plan without writing anything",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt (for a run whose plan was already reviewed)",
    )
    args = parser.parse_args(argv)
    configured = get_settings().tools_dir.strip()
    if not configured:
        print("TOOLS_DIR is not configured; refusing to guess a tools directory.")
        return 1
    return migrate_tools(
        Path(configured),
        dry_run=args.dry_run,
        confirm=(lambda: True) if args.yes else (lambda: _confirm_on_stdin(input)),
    )


if __name__ == "__main__":
    raise SystemExit(main())
