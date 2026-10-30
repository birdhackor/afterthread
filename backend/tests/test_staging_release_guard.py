"""Every value that can count towards removing the staging of an install, of a
revise, or of a publish of a draft as it is, is registered here, and so is the
condition each of the three removes it under.

``tool_builder.run_install`` removes its staging only when its commands and
writes are confirmed ended (``shells_ended``) and no worker of its promote may
still be writing it (not ``publish_running``).
``tool_builder.run_revise`` removes a round's staging only on its release
condition, written out atom by atom in its comment on ``holding``: the commands
and writes confirmed ended, no publish worker and no worker copying into it still
writing, and ``holding`` lowered -- nothing worth keeping, the draft holding the
work (``saved`` and ``durable``), published, or moved out.
``tool_builder.run_publish_draft`` removes its staging
only on its own, written out in its comment above its flags: no worker copying
the draft in or publishing it still writing, and ``holding`` lowered --
published, moved out, or the draft seen to hold what the staging does. Four
kinds of code produce those atoms:

* a save's result -- each keyword ``saved``, ``durable`` or ``worth_saving``
  handed a value that can count towards the removal (anything but the constant
  ``False``, or for ``worth_saving`` anything but ``True``), in
  ``tool_drafts.py`` and ``tool_builder.py``;
* ``run_install`` itself -- each statement binding ``shells_ended`` or
  ``publish_running``;
* ``run_revise`` itself -- each statement binding ``holding``, ``shells_ended``,
  ``publish_running`` or ``copy_running``;
* ``run_publish_draft`` itself -- each statement binding ``copy_running``,
  ``publish_running`` or ``holding``.

Each one found by reading the syntax tree must be registered below, with what
the value claims about which object and the observation it rests on. So must
the condition each of the three functions calls ``_cleanup_staging`` under, as
the ``if`` statements and conditional expressions around the call make it. A new
one fails the guard until it is registered; a registered one that is gone fails
it too, so the register stays the code as it is.

The register is written by hand: the guard checks that every producer and each
condition is registered, not that the observation a row names is made. It
covers the two files and the three functions named, and the forms the scanners
know (the tables of forms below). A condition made some other way -- a
``return`` or ``raise`` before the call, a ``match``, a loop -- is not read.
"""

import ast
from collections import Counter
from pathlib import Path

import pytest

from afterthread.services import tool_builder, tool_drafts

# A save's result: each keyword, and the one constant that counts for keeping.
_KEEPING = {"saved": False, "durable": False, "worth_saving": True}
# The names ``run_revise``'s release condition is made of.
_ATOMS = ("holding", "shells_ended", "publish_running", "copy_running")
# The names ``run_publish_draft``'s release condition is made of.
_PUBLISH_ATOMS = ("copy_running", "publish_running", "holding")
# The names ``run_install``'s release condition is made of.
_INSTALL_ATOMS = ("shells_ended", "publish_running")
# What removes a job's staging.
_CLEANUP = "_cleanup_staging"


def _callee(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ast.unparse(func)


def _counts_for_keeping(value: ast.expr, keyword: str) -> bool:
    return isinstance(value, ast.Constant) and value.value is _KEEPING[keyword]


def _result_claims(source: str, module: str) -> list[tuple[str, str]]:
    """Every call in ``source`` that hands a save's result a value which can count
    towards the removal: (``module.function``, ``callee(keyword=value)``)."""
    found: list[tuple[str, str]] = []

    def visit(node: ast.AST, where: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = where
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner = f"{where}.{child.name}"
            if isinstance(child, ast.Call):
                for keyword in child.keywords:
                    if keyword.arg in _KEEPING and not _counts_for_keeping(
                        keyword.value, keyword.arg
                    ):
                        value = ast.unparse(keyword.value)
                        found.append((inner, f"{_callee(child.func)}({keyword.arg}={value})"))
            visit(child, inner)

    visit(ast.parse(source), module)
    return found


def _function(source: str, function: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    target = next(
        (
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function
        ),
        None,
    )
    assert target is not None, f"no function {function} to scan"
    return target


def _parents(target: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(target) for child in ast.iter_child_nodes(parent)}


def _atom_bindings(
    source: str, module: str, function: str, atoms: tuple[str, ...] = _ATOMS
) -> list[tuple[str, str]]:
    """Every statement in ``function`` of ``source`` that binds one of ``atoms``, in
    any binding form: (``module.function``, the statement's first line)."""
    target = _function(source, function)
    parents = _parents(target)
    found: list[tuple[str, str]] = []
    for node in ast.walk(target):
        if isinstance(node, ast.Name) and node.id in atoms and isinstance(node.ctx, ast.Store):
            statement: ast.AST = node
            while not isinstance(statement, ast.stmt):
                statement = parents[statement]
            found.append((f"{module}.{function}", ast.unparse(statement).splitlines()[0]))
    return found


def _negated(test: ast.expr) -> str:
    """``test`` as false: ``x`` for ``not x``, ``not (...)`` otherwise."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return ast.unparse(test.operand)
    return f"not ({ast.unparse(test)})"


def _release_conditions(source: str, module: str, function: str) -> list[tuple[str, str]]:
    """The condition each mention of ``_CLEANUP`` in ``function`` of ``source`` is
    reached under, as the enclosing ``if`` statements and conditional expressions
    make it, outermost first, joined by ``and``; ``True`` when none encloses it:
    (``module.function``, the condition)."""
    target = _function(source, function)
    parents = _parents(target)
    found: list[tuple[str, str]] = []
    for node in ast.walk(target):
        named = (isinstance(node, ast.Name) and node.id == _CLEANUP) or (
            isinstance(node, ast.Attribute) and node.attr == _CLEANUP
        )
        if not named:
            continue
        held: list[str] = []
        child: ast.AST = node
        while child is not target:
            parent = parents[child]
            if isinstance(parent, ast.If | ast.IfExp) and child is not parent.test:
                in_body = (
                    child in parent.body if isinstance(parent, ast.If) else (child is parent.body)
                )
                held.append(ast.unparse(parent.test) if in_body else _negated(parent.test))
            child = parent
        found.append((f"{module}.{function}", " and ".join(reversed(held)) or "True"))
    return found


def _producers() -> list[tuple[str, str]]:
    drafts = Path(tool_drafts.__file__).read_text(encoding="utf-8")
    builder = Path(tool_builder.__file__).read_text(encoding="utf-8")
    return [
        *_result_claims(drafts, "tool_drafts"),
        *_result_claims(builder, "tool_builder"),
        *_atom_bindings(builder, "tool_builder", "run_install", _INSTALL_ATOMS),
        *_atom_bindings(builder, "tool_builder", "run_revise"),
        *_atom_bindings(builder, "tool_builder", "run_publish_draft", _PUBLISH_ATOMS),
    ]


def _releases() -> list[tuple[str, str]]:
    builder = Path(tool_builder.__file__).read_text(encoding="utf-8")
    return [
        *_release_conditions(builder, "tool_builder", "run_install"),
        *_release_conditions(builder, "tool_builder", "run_revise"),
        *_release_conditions(builder, "tool_builder", "run_publish_draft"),
    ]


# The release condition this register's rows refer to by its atoms is written
# once, in ``run_revise``'s comment on ``holding``. Each row: (where, form, what
# the value claims about which object, and the observation it rests on). A form
# that occurs twice in a function is registered twice.
_REGISTERED: list[tuple[str, str, str]] = [
    (
        "tool_drafts._write_manifest",
        "_ManifestWrite(durable=replaced and bool(durable) and durable[0])",
        "the durable atom at its source: the manifest file is on disk -- the atomic "
        "replace happened, and _write_package_file_atomic reported the fsync of draft/ "
        "after it (durability_out)",
    ),
    (
        "tool_drafts._save_round",
        "SaveResult(worth_saving=False)",
        "the worth_saving atom: a first round left nothing to keep -- its workspace seen "
        "unchanged (same_tree of copied_in and its digest now, both taken) and no notes "
        "handed back",
    ),
    (
        "tool_drafts._save_locked",
        "_round_entry(saved=True)",
        "the saved atom, as the manifest records it: an unchanged continuation's files "
        "directory holds the round's files -- seen under the lock, after the manifest is "
        "matched, still to be what the round copied out of it (_still_as_copied_out), "
        "the workspace seen unchanged since it received that copy",
    ),
    (
        "tool_drafts._save_locked",
        "_record(saved=True)",
        "the same claim and observation as the _round_entry beside it, for the save's result",
    ),
    (
        "tool_drafts._record",
        "SaveResult(saved=saved)",
        "passes on its caller's claim; each caller's is registered at its own call",
    ),
    (
        "tool_drafts._record",
        "SaveResult(durable=written.durable)",
        "the durable atom, passed on from the manifest write that recorded the round",
    ),
    (
        "tool_drafts._copy_and_commit",
        "_round_entry(saved=True)",
        "the saved atom for a first round, as the manifest records it: the new files "
        "directory holds the round's files -- the copy made from the workspace under the "
        "lock (_carry_out), with no entry left out because a look at it failed (the "
        "plan's unlooked and _carry_out's both empty: the save stopped earlier otherwise), "
        "made durable (Durability.tree) and renamed into place, which the manifest being "
        "written names",
    ),
    (
        "tool_drafts._copy_and_commit",
        "_round_entry(saved=True)",
        "the same for a continuation's new files directory",
    ),
    (
        "tool_drafts._copy_and_commit",
        "SaveResult(saved=True)",
        "the same, once the manifest naming the new copy replaced the old (written.replaced)",
    ),
    (
        "tool_drafts._copy_and_commit",
        "SaveResult(durable=written.durable)",
        "the durable atom, passed on from the manifest write that names the new copy",
    ),
    (
        "tool_builder.run_install",
        "shells_ended = True",
        "no session yet: no command or write was started, so none can be using the staging",
    ),
    (
        "tool_builder.run_install",
        "shells_ended = False",
        "the session starts: commands and writes may run from here, the claim withdrawn "
        "until the stop evidence",
    ),
    (
        "tool_builder.run_install",
        "shells_ended = await shells.stop_and_wait()",
        "the shells_ended atom: every run_shell command's stop evidence and every "
        "write_file write's word that its worker returned (_BuilderShells.stop_and_wait)",
    ),
    (
        "tool_builder.run_install",
        "publish_running = False",
        "no promote worker has started",
    ),
    (
        "tool_builder.run_install",
        "publish_running = True",
        "the promote's worker is about to start writing the staging",
    ),
    (
        "tool_builder.run_install",
        "publish_running = False",
        "the promote ended in an exception, the worker's own or one before it began: no "
        "worker writes the staging any more",
    ),
    (
        "tool_builder.run_install",
        "publish_running = False",
        "the promote's worker returned its result: it writes the staging no more",
    ),
    (
        "tool_builder.run_revise",
        "shells_ended = True",
        "no session yet: no command or write was started, so none can be using the "
        "staging; a worker that may still be copying the round's start into it is "
        "copy_running's",
    ),
    (
        "tool_builder.run_revise",
        "shells_ended = False",
        "the session starts: commands and writes may run from here, the claim withdrawn "
        "until the stop evidence",
    ),
    (
        "tool_builder.run_revise",
        "shells_ended = await shells.stop_and_wait()",
        "the shells_ended atom: every run_shell command's stop evidence and every "
        "write_file write's word that its worker returned (_BuilderShells.stop_and_wait)",
    ),
    (
        "tool_builder.run_revise",
        "holding = False",
        "before the commands and writes are confirmed ended there is no work to hold: the "
        "staging holds only the copy the round was given",
    ),
    (
        "tool_builder.run_revise",
        "holding = shells_ended",
        "raised with the stop evidence, so an exception or a cancellation from there on "
        "finds it up",
    ),
    *[
        (
            "tool_builder.run_revise",
            "holding = _holding(kept)",
            "the worth_saving, saved and durable atoms of the save just made, read together "
            "by _holding; each of their producers is registered above",
        )
        for _ in range(4)
    ],
    (
        "tool_builder.run_revise",
        "holding = elsewhere and tools._is_within(session_root, content)",
        "the moved-out atom: the failed publish's content -- where it last moved the work, "
        "advanced only after each rename succeeded -- is outside this session's staging; "
        "elsewhere is _holding of the save before the publish",
    ),
    (
        "tool_builder.run_revise",
        "holding = False",
        "the published atom: _publish_revised_version handed back a published version, "
        "the version current names holding the work",
    ),
    (
        "tool_builder.run_revise",
        "publish_running = False",
        "no publish worker has started",
    ),
    (
        "tool_builder.run_revise",
        "publish_running = True",
        "a publish worker is about to start writing the staging",
    ),
    (
        "tool_builder.run_revise",
        "publish_running = False",
        "the publish ended in an exception, the worker's own or one before it began: no "
        "worker writes the staging any more",
    ),
    (
        "tool_builder.run_revise",
        "publish_running = False",
        "the worker returned its result: it writes the staging no more",
    ),
    (
        "tool_builder.run_revise",
        "copy_running = False",
        "no copy worker has started",
    ),
    *[
        row
        for copied in (
            "the version a first round starts from",
            "the draft a continuation works on",
        )
        for row in (
            (
                "tool_builder.run_revise",
                "copy_running = True",
                f"the worker copying {copied} into the staging is about to start writing it",
            ),
            (
                "tool_builder.run_revise",
                "copy_running = False",
                "the copy ended in an exception, the worker's own or one before it began: no "
                "worker writes the staging any more",
            ),
            (
                "tool_builder.run_revise",
                "copy_running = False",
                "the copy worker returned its result: it writes the staging no more",
            ),
        )
    ],
    (
        "tool_builder.run_publish_draft",
        "copy_running = False",
        "no copy worker has started",
    ),
    (
        "tool_builder.run_publish_draft",
        "copy_running = True",
        "the worker copying the draft into the staging is about to start writing it",
    ),
    (
        "tool_builder.run_publish_draft",
        "copy_running = False",
        "the copy ended in an exception, the worker's own or one before it began: no "
        "worker writes the staging any more",
    ),
    (
        "tool_builder.run_publish_draft",
        "copy_running = False",
        "the copy worker returned its result: it writes the staging no more",
    ),
    (
        "tool_builder.run_publish_draft",
        "publish_running = False",
        "no publish worker has started",
    ),
    (
        "tool_builder.run_publish_draft",
        "publish_running = True",
        "a publish worker is about to start writing the staging",
    ),
    (
        "tool_builder.run_publish_draft",
        "publish_running = False",
        "the publish ended in an exception, the worker's own or one before it began: no "
        "worker writes the staging any more",
    ),
    (
        "tool_builder.run_publish_draft",
        "publish_running = False",
        "the publish worker returned its result: it writes the staging no more",
    ),
    (
        "tool_builder.run_publish_draft",
        "holding = False",
        "before the publish the staging holds a copy of the draft, which the copy only "
        "read: the draft holds all of it",
    ),
    (
        "tool_builder.run_publish_draft",
        "holding = True",
        "raised just before the publish starts, from when the staging is what is "
        "published, so an exception or a cancellation from there on finds it up",
    ),
    (
        "tool_builder.run_publish_draft",
        "holding = False",
        "the moved-out atom: the failed publish's content -- where it last moved the work, "
        "advanced only after each rename succeeded -- is not within this session's staging",
    ),
    (
        "tool_builder.run_publish_draft",
        "holding = not held",
        "the draft-holds-it atom: tool_drafts.holds_copy saw, under the lock and after the "
        "manifest of the same draft is matched by its id and files directory, the copy's "
        "two digests equal and the files directory's digest now equal to them",
    ),
    (
        "tool_builder.run_publish_draft",
        "holding = False",
        "the published atom: _publish_revised_version handed back a published version, "
        "the version current names holding the work",
    ),
]

# The condition each function calls ``_cleanup_staging`` under: (where, the
# condition, what it claims and where each of its atoms is registered).
_RELEASES: list[tuple[str, str, str]] = [
    (
        "tool_builder.run_install",
        "shells_ended and not (publish_running)",
        "the staging is removed only when every run_shell command is confirmed stopped "
        "and every write_file write has ended, and no worker of the promote may still be "
        "writing it; the bindings of each atom are registered above",
    ),
    (
        "tool_builder.run_revise",
        "shells_ended and not (holding or publish_running or copy_running)",
        "the staging is removed only when every run_shell command is confirmed stopped "
        "and every write_file write has ended, neither a publish worker nor a worker "
        "copying into it may still be writing it, and nothing in it is held for the "
        "operator; the bindings of each atom are registered above",
    ),
    (
        "tool_builder.run_publish_draft",
        "not (copy_running or publish_running or holding)",
        "the staging is removed only when neither the worker copying the draft in nor the "
        "one publishing it may still be writing it, and nothing in it is held for the "
        "operator; the bindings of each atom are registered above",
    ),
]


def test_each_value_counting_towards_an_install_revise_or_draft_publish_cleanup_is_registered() -> (
    None
):
    found = Counter(_producers())
    registered = Counter((where, form) for where, form, _claim in _REGISTERED)
    assert found, "the scan found nothing at all: it is not reading the two files"
    assert all(claim.strip() for _where, _form, claim in _REGISTERED)
    unregistered = sorted((found - registered).elements())
    stale = sorted((registered - found).elements())
    assert not unregistered, f"producers with no registered claim: {unregistered}"
    assert not stale, f"registered producers no longer in the code: {stale}"


def test_the_condition_an_install_revise_or_draft_publish_cleans_up_under_is_registered() -> None:
    found = Counter(_releases())
    registered = Counter((where, condition) for where, condition, _claim in _RELEASES)
    assert found, "the scan found no removal at all: it is not reading the three functions"
    assert all(claim.strip() for _where, _condition, claim in _RELEASES)
    unregistered = sorted((found - registered).elements())
    stale = sorted((registered - found).elements())
    assert not unregistered, f"removals under a condition not registered: {unregistered}"
    assert not stale, f"registered removal conditions no longer in the code: {stale}"


# One row per form the scanner must find: (source, what it reports for it).
_FOUND: list[tuple[str, list[tuple[str, str]]]] = [
    ("SaveResult(worth_saving=True, saved=True)\n", [("m.where", "SaveResult(saved=True)")]),
    ("tool_drafts.SaveResult(saved=True)\n", [("m.where", "SaveResult(saved=True)")]),
    (
        "_round_entry(r, 1, saved=True, reason=None, omitted=[])\n",
        [("m.where", "_round_entry(saved=True)")],
    ),
    ("_record(d, doc, known, saved=seen)\n", [("m.where", "_record(saved=seen)")]),
    ("dataclasses.replace(kept, saved=True)\n", [("m.where", "replace(saved=True)")]),
    ("kept.report(saved=True)\n", [("m.where", "report(saved=True)")]),
    ("SaveResult(worth_saving=False)\n", [("m.where", "SaveResult(worth_saving=False)")]),
    ("SaveResult(worth_saving=changed)\n", [("m.where", "SaveResult(worth_saving=changed)")]),
    (
        "SaveResult(worth_saving=True, durable=written.durable)\n",
        [("m.where", "SaveResult(durable=written.durable)")],
    ),
    (
        "_ManifestWrite(replaced=True, durable=True)\n",
        [("m.where", "_ManifestWrite(durable=True)")],
    ),
]


@pytest.mark.parametrize(("source", "reported"), _FOUND)
def test_the_staging_release_scanner_finds_each_listed_result_form(
    source: str, reported: list[tuple[str, str]]
) -> None:
    assert _result_claims(f"def where():\n{_indented(source)}", "m") == reported


# Each binding form of an atom the scanner must find: (source, its first line).
_BOUND: list[tuple[str, str]] = [
    ("holding = x\n", "holding = x"),
    ("holding: bool = x\n", "holding: bool = x"),
    ("holding |= x\n", "holding |= x"),
    ("holding, other = a, b\n", "holding, other = (a, b)"),
    ("other = holding = x\n", "other = holding = x"),
    ("if (holding := x):\n    pass\n", "if (holding := x):"),
    ("for holding in xs:\n    pass\n", "for holding in xs:"),
    ("with f() as holding:\n    pass\n", "with f() as holding:"),
    (
        "shells_ended = await shells.stop_and_wait()\n",
        "shells_ended = await shells.stop_and_wait()",
    ),
    ("publish_running = True\n", "publish_running = True"),
    ("copy_running = True\n", "copy_running = True"),
    ("try:\n    pass\nexcept Exception:\n    copy_running = False\n", "copy_running = False"),
    ("def inner():\n    nonlocal holding\n    holding = True\n", "holding = True"),
]


@pytest.mark.parametrize(("source", "line"), _BOUND)
def test_the_staging_release_scanner_finds_each_binding_form_of_an_atom(
    source: str, line: str
) -> None:
    found = _atom_bindings(f"async def run_revise():\n{_indented(source)}", "m", "run_revise")
    assert found == [("m.run_revise", line)]


# Each binding form of a publish's atom the scanner must find, the forms
# ``run_publish_draft`` binds them in among them: (source, its first line).
_BOUND_IN_A_PUBLISH: list[tuple[str, str]] = [
    ("copy_running = True\n", "copy_running = True"),
    ("copy_running: bool = x\n", "copy_running: bool = x"),
    ("publish_running = False\n", "publish_running = False"),
    ("holding = not held\n", "holding = not held"),
    ("other, copy_running = a, b\n", "other, copy_running = (a, b)"),
    ("try:\n    pass\nexcept Exception:\n    copy_running = False\n", "copy_running = False"),
]


@pytest.mark.parametrize(("source", "line"), _BOUND_IN_A_PUBLISH)
def test_the_staging_release_scanner_finds_each_binding_form_of_a_publish_s_atom(
    source: str, line: str
) -> None:
    found = _atom_bindings(
        f"async def run_publish_draft():\n{_indented(source)}",
        "m",
        "run_publish_draft",
        _PUBLISH_ATOMS,
    )
    assert found == [("m.run_publish_draft", line)]


# Each form of a removal the condition scanner must read: (source, the condition).
_RELEASED: list[tuple[str, str]] = [
    ("_cleanup_staging(root)\n", "True"),
    ("await run_in_threadpool(_cleanup_staging, root, base)\n", "True"),
    ("builder._cleanup_staging(root)\n", "True"),
    ("if a:\n    _cleanup_staging(root)\n", "a"),
    ("if a or b:\n    pass\nelse:\n    _cleanup_staging(root)\n", "not (a or b)"),
    (
        "if not a:\n    pass\nelse:\n    if b:\n        pass\n    else:\n"
        "        _cleanup_staging(root)\n",
        "a and not (b)",
    ),
    ("if a:\n    pass\nelif b:\n    _cleanup_staging(root)\n", "not (a) and b"),
    ("try:\n    pass\nfinally:\n    if a:\n        _cleanup_staging(root)\n", "a"),
    ("x = _cleanup_staging if a else None\n", "a"),
    ("x = None if a else _cleanup_staging\n", "not (a)"),
]


@pytest.mark.parametrize(("source", "condition"), _RELEASED)
def test_the_staging_release_scanner_reads_the_condition_of_each_removal_form(
    source: str, condition: str
) -> None:
    found = _release_conditions(f"async def where():\n{_indented(source)}", "m", "where")
    assert found == [("m.where", condition)]


# Mentions that remove nothing: not found.
_NOT_RELEASED: list[str] = [
    "_cleanup_staging_later(root)\n",
    "cleanup_staging(root)\n",
    "if _cleanup_staging_done:\n    pass\n",
]


@pytest.mark.parametrize("source", _NOT_RELEASED)
def test_the_staging_release_scanner_passes_over_what_removes_nothing(source: str) -> None:
    assert _release_conditions(f"async def where():\n{_indented(source)}", "m", "where") == []


# Forms that count for keeping the staging, or bind nothing it is made of: not found.
_PASSED: list[str] = [
    "SaveResult(saved=False)\n",
    "SaveResult(worth_saving=True, reason=why)\n",
    "SaveResult(worth_saving=True, durable=False)\n",
    "_round_entry(r, 1, saved=False, reason=why, omitted=[])\n",
    "if holding:\n    pass\n",
    "other = holding\n",
    "keeping = True\n",
    "f(holding=True)\n",
]


@pytest.mark.parametrize("source", _PASSED)
def test_the_staging_release_scanner_lets_keeping_and_reading_forms_through(source: str) -> None:
    module = f"async def run_revise():\n{_indented(source)}"
    assert _result_claims(module, "m") == []
    assert _atom_bindings(module, "m", "run_revise") == []


# A publish's atoms read, or another name bound: not found.
_PASSED_IN_A_PUBLISH: list[str] = [
    "if copy_running or publish_running or holding:\n    pass\n",
    "other = copy_running\n",
    "f(copy_running=True)\n",
    "shells_ended = True\n",
]


@pytest.mark.parametrize("source", _PASSED_IN_A_PUBLISH)
def test_the_staging_release_scanner_lets_a_publish_s_reading_forms_through(source: str) -> None:
    module = f"async def run_publish_draft():\n{_indented(source)}"
    assert _atom_bindings(module, "m", "run_publish_draft", _PUBLISH_ATOMS) == []


def _indented(source: str) -> str:
    """``source`` as the body of a function."""
    return "".join(f"    {line}\n" for line in source.splitlines())
