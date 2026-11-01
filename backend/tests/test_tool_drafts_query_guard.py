"""``services/tool_drafts.py`` asks the filesystem only in ways that raise their errors.

A query that swallows its error answers with a fixed value -- False, None, a
default -- and that value can go on to grant an action: keep a link, save,
remove, report removed. Here every such form in ``tool_drafts.py`` is found by
reading its syntax tree, and each one found must be listed below with why its
fixed value grants nothing (a refusal, a definite answer, or best-effort cleanup
whose result nothing relies on). A new one fails the guard until it is reasoned
about and listed; a listed one that is gone fails it too, so the list stays the
module as it is.

Covers ``tool_drafts.py`` only, and only the forms the scanner knows (the
table of forms below); a query reached through a helper in another module is
that module's.
"""

import ast
from collections import Counter
from pathlib import Path

import pytest

from afterthread.services import tool_drafts

# ``os.path`` predicates that answer False for a path they cannot look at.
_PATH_PREDICATES = frozenset(
    {"exists", "lexists", "isdir", "isfile", "islink", "ismount", "isjunction", "isdevdrive"}
)
# ``Path`` (and ``DirEntry``) methods of the same kind, matched by name: the
# receiver's type is not in the syntax tree.
_PATH_METHODS = frozenset(
    {
        "exists",
        "is_dir",
        "is_file",
        "is_symlink",
        "is_mount",
        "is_junction",
        "is_block_device",
        "is_char_device",
        "is_fifo",
        "is_socket",
    }
)
# What a handler may catch and still be no swallow: an answer about the path.
_DEFINITE = frozenset({"FileNotFoundError", "NotADirectoryError", "FileExistsError"})
# What a handler catches that covers an error saying nothing about the path.
_BROAD = frozenset(
    {
        "OSError",
        "IOError",
        "EnvironmentError",
        "Exception",
        "BaseException",
        "PermissionError",
        "IsADirectoryError",
        "InterruptedError",
        "BlockingIOError",
        "TimeoutError",
    }
)


def _imported_names(tree: ast.Module) -> dict[str, str]:
    """Each name the module binds by import, as the dotted name it stands for."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is None:
                    head = alias.name.split(".")[0]
                    names[head] = head
                else:
                    names[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _dotted(node: ast.expr, names: dict[str, str]) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([names.get(node.id, node.id), *reversed(parts)])


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((keyword.value for keyword in call.keywords if keyword.arg == name), None)


def _is_constant(node: ast.expr | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _caught(handler: ast.ExceptHandler) -> set[str] | None:
    if handler.type is None:
        return None
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in nodes
        if isinstance(node, ast.Name | ast.Attribute)
    }


def _always_raises(handler: ast.ExceptHandler) -> bool:
    """Whether the handler ends in ``raise`` with no way out before it."""
    pending: list[ast.AST] = list(handler.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            continue
        if isinstance(node, ast.Return | ast.Continue | ast.Break | ast.Yield | ast.YieldFrom):
            return False
        pending.extend(ast.iter_child_nodes(node))
    return bool(handler.body) and isinstance(handler.body[-1], ast.Raise)


def _form(node: ast.AST, names: dict[str, str]) -> str | None:
    """Which swallowing form ``node`` is, or None."""
    if isinstance(node, ast.ExceptHandler):
        caught = _caught(node)
        if _always_raises(node):
            return None
        if caught is None:
            return "bare except"
        return "broad except" if caught & _BROAD else None
    if not isinstance(node, ast.Call):
        return None
    name = _dotted(node.func, names)
    if name is not None:
        module, _, leaf = name.rpartition(".")
        if module in {"os.path", "posixpath", "genericpath"} and leaf in _PATH_PREDICATES:
            return "os.path predicate"
        if name == "os.access":
            return "os.access"
        if module in {"os.path", "posixpath"} and leaf == "realpath":
            return None if _is_constant(_keyword(node, "strict"), True) else "realpath not strict"
        if name == "shutil.rmtree":
            ignore = _keyword(node, "ignore_errors")
            if ignore is None and len(node.args) > 1:
                ignore = node.args[1]
            if ignore is None or _is_constant(ignore, False):
                return None
            return "rmtree ignoring errors"
        if name == "contextlib.suppress":
            return "contextlib.suppress"
        if name in {"os.walk", "os.fwalk"}:
            onerror = _keyword(node, "onerror")
            if onerror is None or _is_constant(onerror, None):
                return "walk without onerror"
            return None
        if name in {"glob.glob", "glob.iglob"}:
            return "glob"
    if isinstance(node.func, ast.Attribute):
        attr = node.func.attr
        if attr in _PATH_METHODS:
            return "path predicate method"
        if attr == "resolve":
            return None if _is_constant(_keyword(node, "strict"), True) else "resolve not strict"
        if attr == "walk" and _keyword(node, "on_error") is None:
            return "Path.walk without on_error"
        if attr in {"glob", "rglob"}:
            return "Path.glob"
    return None


def _snippet(node: ast.AST) -> str:
    if isinstance(node, ast.ExceptHandler):
        caught = ast.unparse(node.type) if node.type is not None else ""
        first = ast.unparse(node.body[0]).splitlines()[0] if node.body else ""
        return f"except {caught}: {first}".replace("except : ", "except: ")
    return ast.unparse(node).splitlines()[0]


def _swallowing(source: str) -> list[tuple[str, str, str]]:
    """Every swallowing form in ``source``: (enclosing function, form, snippet)."""
    tree = ast.parse(source)
    names = _imported_names(tree)
    found: list[tuple[str, str, str]] = []

    def visit(node: ast.AST, where: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = where
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner = child.name if where == "<module>" else f"{where}.{child.name}"
            form = _form(child, names)
            if form is not None:
                found.append((inner, form, _snippet(child)))
            visit(child, inner)

    visit(tree, "<module>")
    return found


# Every swallowing form in tool_drafts.py: (function, snippet, why its fixed value
# grants nothing). A snippet that occurs twice in a function is listed twice.
_REASONED: list[tuple[str, str, str]] = [
    (
        "_own_meta",
        "except OSError: return _UNSEEN",
        "a directory that cannot be looked at may be a link: reads take the draft as "
        "there, writes leave it alone",
    ),
    (
        "_own_draft",
        "except OSError: return (draft_dir, None)",
        "read side: a draft/ that cannot be looked at counts as there, which blocks a revise",
    ),
    (
        "_seen_own_draft",
        "except OSError: return None",
        "write side: what cannot be looked at is not acted on",
    ),
    (
        "directory_identity",
        "except OSError: return None",
        "no identity: the package check and the save's workspace check refuse on None",
    ),
    (
        "_is_real_dir",
        "except OSError: return False",
        "not seen as a real directory: no continuation or sweep acts on it",
    ),
    (
        "_file_digest",
        "except OSError: return None",
        "no digest: same_tree never calls an unknown tree the same, so the round is saved "
        "and a previous copy is kept",
    ),
    (
        "_file_digest",
        "except OSError: return None",
        "as above, for a read that fails part way",
    ),
    (
        "tree_digest",
        "except OSError: return None",
        "no digest: same_tree never calls an unknown tree the same",
    ),
    (
        "_entry_identity",
        "except OSError: return None",
        "the token's look is not whole: a discard by it removes nothing",
    ),
    (
        "_entries_identity",
        "except OSError: return None",
        "the token's look is not whole: a discard by it removes nothing",
    ),
    (
        "_over_limits",
        "except OSError: return REASON_UNREADABLE",
        "the workspace cannot be measured: the save keeps no files",
    ),
    (
        "stored_link_target",
        "except OSError: unresolved = exc",
        "the resolved spelling is not had: only the given one is matched, and a target "
        "not matching it raises LookFailed -- no link kept, and a save keeps no files",
    ),
    (
        "_keep_earlier",
        "except (OSError, LookFailed): plan.unlooked.append(relative)",
        "the earlier version cannot be looked at: nothing is put back, and the path is "
        "unlooked -- a save keeps no files",
    ),
    (
        "_links_failing_on",
        "entry.is_symlink()",
        "DirEntry.is_symlink() swallows only FileNotFoundError, an entry gone since the "
        "listing; any other error raises and the save fails",
    ),
    (
        "_links_failing_on",
        "entry.is_dir(follow_symlinks=False)",
        "DirEntry.is_dir() swallows only FileNotFoundError: nothing left to walk into",
    ),
    (
        "_resolution_failure",
        "except OSError: if exc.errno not in _RESOLVES_NOWHERE or not "
        "isinstance(exc.filename, str):",
        "classifies the error: resolving nowhere at a point inside the copy keeps the "
        "link, any other failure raises LookFailed -- not kept, and a save keeps no files",
    ),
    (
        "_links_failing_resolution",
        "except OSError: real_root = None",
        "the copy's own path is not had: every link's resolution raises LookFailed -- "
        "not kept, and a save keeps no files",
    ),
    (
        "_links_failing_resolution",
        "entry.is_symlink()",
        "DirEntry.is_symlink() swallows only FileNotFoundError; any other error raises",
    ),
    (
        "_links_failing_resolution",
        "entry.is_dir(follow_symlinks=False)",
        "DirEntry.is_dir() swallows only FileNotFoundError: nothing left to walk into",
    ),
    (
        "remove_entry",
        "except OSError: return",
        "removal is best effort and reports nothing; what it leaves is judged by a "
        "later look (tools._seen_gone) or collected by the sweep",
    ),
    (
        "remove_entry",
        "shutil.rmtree(path, ignore_errors=True)",
        "as above",
    ),
    (
        "remove_entry",
        "contextlib.suppress(OSError)",
        "as above",
    ),
    (
        "save_round",
        "except Exception: return _not_saved(REASON_UNEXPECTED.format(kind=type(exc).__name__))",
        "the save never raises: a failure it did not expect is not saved",
    ),
    (
        "_save_round",
        "except Exception: return _not_saved(REASON_SCAN_FAILED.format(reason=type(exc).__name__))",
        "the known secrets cannot be had: nothing is saved and nothing is written",
    ),
    (
        "_save_round",
        "except OSError: reason = REASON_UNREADABLE",
        "the copy cannot be planned: the save keeps no files",
    ),
    (
        "_save_locked",
        "except OSError: return _not_saved(REASON_COPY.format(kind=type(exc).__name__))",
        "draft/ cannot be made: not saved",
    ),
    (
        "_undo",
        "contextlib.suppress(OSError)",
        "taking back an empty draft/ is best effort: rmdir only removes an empty directory",
    ),
    (
        "_copy_and_commit",
        "except OSError: return (None, REASON_COPY.format(kind=type(exc).__name__))",
        "the copy's directory cannot be made: not saved",
    ),
    (
        "_copy_and_commit",
        "except OSError: return (incoming, REASON_COPY.format(kind=type(exc).__name__))",
        "the copy cannot be made: not saved, and the half-made copy is removed",
    ),
    (
        "_copy_and_commit",
        "except OSError: return (incoming, REASON_COPY.format(kind=type(exc).__name__))",
        "the copy cannot be renamed into place: not saved, and it is removed",
    ),
    (
        "_copy_and_commit",
        "contextlib.suppress(OSError)",
        "the copy's top takes the workspace's mode best effort; a failure leaves the "
        "mode it was made with and decides nothing",
    ),
    (
        "mark_publishing",
        "except Exception: return None",
        "no mark is claimed: a round's publish goes on unmarked by design, and the draft "
        "reads as stale rather than published; a publish of the draft as it is does not "
        "go on at all",
    ),
    (
        "clear_publishing",
        "except Exception: return None",
        "no change is claimed written",
    ),
    (
        "unmark_publishing",
        "except Exception: return None",
        "no change is claimed written: the mark stays, and the list reads the draft's "
        "publish as not known",
    ),
    (
        "_take_away",
        "except OSError: remove_entry(draft_dir)",
        "the rename is refused: draft/ is removed in place as far as that goes, and only "
        "a look seeing it gone reports it removed",
    ),
    (
        "_as_saved",
        "except OSError: return 'unconfirmed'",
        "a draft/ that cannot be listed is not known to hold only the copy: nothing is removed",
    ),
    (
        "remove_after_publish",
        "except Exception: if started[0]:",
        "nothing is reported taken away: before the removal began the draft is left as "
        "unconfirmed, after it draft/ is named as what may be left",
    ),
    (
        "_fsync_file",
        "except OSError: return False",
        "not confirmed on disk: the sweep leaves all of draft/ alone",
    ),
    (
        "_fsync_file",
        "except OSError: return False",
        "as above, for an fsync that fails",
    ),
    (
        "sweep_package",
        "contextlib.suppress(Exception)",
        "any failure stops the sweep where it is; it removes only after its own checks",
    ),
    (
        "sweep_package",
        "shutil.rmtree(draft_dir / name, ignore_errors=True)",
        "removing a half-made .incoming copy, seen as a real directory beside a trusted "
        "manifest, is best effort",
    ),
    (
        "_same_entry",
        "except OSError: return None",
        "whether two names are one entry is not known: the name is left out of the "
        "workspace (withheld_root_names) -- a copy of the draft records it as unreadable, "
        "and a first round's copy of the version leaves it there",
    ),
    (
        "begin_round",
        "except Exception: return REASON_SCAN_FAILED.format(reason=type(exc).__name__)",
        "the known secrets cannot be had: the round does not begin",
    ),
    (
        "begin_round",
        "except Exception: return REASON_UNEXPECTED.format(kind=type(exc).__name__)",
        "a failure it did not expect: the round does not begin",
    ),
    (
        "end_unstarted",
        "except Exception: return False",
        "no write is claimed: running stays, for the next round to record",
    ),
    (
        "record_round",
        "except Exception: return _not_saved(reason or REASON_MANIFEST)",
        "nothing is claimed recorded: running stays, for the next round to record",
    ),
    (
        "copy_into_workspace",
        "except Exception: return REASON_SCAN_FAILED.format(reason=type(exc).__name__)",
        "the known secrets cannot be had: nothing is copied and the round does not begin",
    ),
    (
        "copy_into_workspace",
        "except Exception: return REASON_UNEXPECTED.format(kind=type(exc).__name__)",
        "a failure it did not expect: the round does not begin",
    ),
    (
        "_copy_draft_files",
        "except OSError: return REASON_DRAFT_UNREADABLE",
        "the draft's files cannot be listed: no copy is handed back, so the round does "
        "not begin and the publish does not go on",
    ),
    (
        "_copy_draft_files",
        "except OSError: return REASON_COPY_IN.format(kind=type(exc).__name__)",
        "the workspace cannot be made whole: no copy is handed back, so the round does "
        "not begin and the publish does not go on",
    ),
    (
        "copy_for_publish",
        "except Exception: return REASON_SCAN_FAILED.format(reason=type(exc).__name__)",
        "the known secrets cannot be had: nothing is copied and nothing is published",
    ),
    (
        "copy_for_publish",
        "except Exception: return REASON_COPY_IN.format(kind=type(exc).__name__)",
        "a failure it did not expect: no copy is handed back, and nothing is published",
    ),
    (
        "holds_copy",
        "except Exception: return False",
        "not seen to hold the copy: the publish's staging is kept",
    ),
]


def test_tool_drafts_py_has_no_error_swallowing_query_outside_its_reasoned_list() -> None:
    source = Path(tool_drafts.__file__).read_text(encoding="utf-8")
    found = Counter((where, snippet) for where, _form_name, snippet in _swallowing(source))
    listed = Counter((where, snippet) for where, snippet, _why in _REASONED)
    assert found, "the scan found nothing at all: it is not reading tool_drafts.py"
    assert all(why.strip() for _where, _snippet_text, why in _REASONED)
    unlisted = sorted((found - listed).elements())
    stale = sorted((listed - found).elements())
    assert not unlisted, f"swallowing forms with no reason listed: {unlisted}"
    assert not stale, f"listed forms no longer in tool_drafts.py: {stale}"


# One row per form the scanner must catch: (form it reports, source).
_CAUGHT: list[tuple[str, str]] = [
    *[
        ("os.path predicate", f"import os\nos.path.{name}(p)\n")
        for name in sorted(_PATH_PREDICATES)
    ],
    ("os.path predicate", "import os.path as osp\nosp.islink(p)\n"),
    ("os.path predicate", "from os import path\npath.isdir(p)\n"),
    ("os.path predicate", "from os.path import exists\nexists(p)\n"),
    ("os.access", "import os\nos.access(p, os.R_OK)\n"),
    *[("path predicate method", f"p.{name}()\n") for name in sorted(_PATH_METHODS)],
    ("path predicate method", "p.exists(follow_symlinks=False)\n"),
    ("resolve not strict", "p.resolve()\n"),
    ("resolve not strict", "p.resolve(strict=False)\n"),
    ("realpath not strict", "import os\nos.path.realpath(p)\n"),
    ("realpath not strict", "import os\nos.path.realpath(p, strict=False)\n"),
    ("realpath not strict", "from os.path import realpath\nrealpath(p)\n"),
    ("rmtree ignoring errors", "import shutil\nshutil.rmtree(p, ignore_errors=True)\n"),
    ("rmtree ignoring errors", "import shutil\nshutil.rmtree(p, True)\n"),
    ("rmtree ignoring errors", "from shutil import rmtree\nrmtree(p, ignore_errors=True)\n"),
    ("contextlib.suppress", "import contextlib\nwith contextlib.suppress(OSError):\n    f()\n"),
    ("contextlib.suppress", "from contextlib import suppress\nwith suppress(OSError):\n    f()\n"),
    ("contextlib.suppress", "import contextlib as c\nwith c.suppress(Exception):\n    f()\n"),
    ("walk without onerror", "import os\nfor _ in os.walk(p):\n    pass\n"),
    ("walk without onerror", "import os\nfor _ in os.fwalk(p):\n    pass\n"),
    ("Path.walk without on_error", "for _ in p.walk():\n    pass\n"),
    ("glob", "import glob\nglob.glob(p)\n"),
    ("Path.glob", "p.rglob('*')\n"),
    ("broad except", "try:\n    f()\nexcept OSError:\n    return None\n"),
    ("broad except", "try:\n    f()\nexcept OSError as exc:\n    return ['error', exc.errno]\n"),
    ("broad except", "try:\n    f()\nexcept (OSError, ValueError):\n    return False\n"),
    ("broad except", "try:\n    f()\nexcept FileNotFoundError, PermissionError:\n    x = 0\n"),
    ("broad except", "try:\n    f()\nexcept Exception:\n    pass\n"),
    ("broad except", "for _ in y:\n    try:\n        f()\n    except OSError:\n        continue\n"),
    (
        "broad except",
        "try:\n    f()\nexcept OSError as exc:\n"
        "    if exc.errno == 5:\n        return None\n    raise\n",
    ),
    ("bare except", "try:\n    f()\nexcept:\n    x = 0\n"),
]


@pytest.mark.parametrize(("form", "source"), _CAUGHT)
def test_the_tool_drafts_query_guard_catches_each_listed_form(form: str, source: str) -> None:
    found = _swallowing(f"def where():\n{_indented(source)}")
    assert [name for _where, name, _snippet in found] == [form]


# Forms that raise their errors, or answer only a definite "not there": not caught.
_PASSED: list[str] = [
    "p.resolve(strict=True)\n",
    "import os\nos.path.realpath(p, strict=True)\n",
    "import shutil\nshutil.rmtree(p)\n",
    "import shutil\nshutil.rmtree(p, ignore_errors=False)\n",
    "import os\nos.lstat(p)\n",
    "import os\nos.path.isabs(p)\nos.path.join(p, 'x')\n",
    "import os\nfor _ in os.walk(p, onerror=fail):\n    pass\n",
    "for _ in p.walk(on_error=fail):\n    pass\n",
    "try:\n    f()\nexcept FileNotFoundError, NotADirectoryError:\n    return False\n",
    "try:\n    f()\nexcept FileExistsError:\n    return None\n",
    "try:\n    f()\nexcept OSError:\n    cleanup()\n    raise\n",
    "try:\n    f()\nexcept OSError as exc:\n    raise RuntimeError('x') from exc\n",
    "try:\n    f()\nexcept ValueError:\n    return None\n",
]


@pytest.mark.parametrize("source", _PASSED)
def test_the_tool_drafts_query_guard_lets_raising_and_definite_forms_through(source: str) -> None:
    assert _swallowing(f"def where():\n{_indented(source)}") == []


def _indented(source: str) -> str:
    """``source`` as the body of a function; its imports stay inside the body too."""
    return "".join(f"    {line}\n" for line in source.splitlines())
