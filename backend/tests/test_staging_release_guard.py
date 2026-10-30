"""Every value that can count towards removing a revise's staging is registered here.

``tool_builder.run_revise`` removes a round's staging only on its release
condition, written out atom by atom in its comment on ``holding``: the commands
confirmed stopped, no publish worker still writing, and ``holding`` lowered --
nothing worth keeping, the draft holding the work (``saved`` and ``durable``),
published, or moved out. Two kinds of code produce those atoms:

* a save's result -- each keyword ``saved``, ``durable`` or ``worth_saving``
  handed a value that can count towards the removal (anything but the constant
  ``False``, or for ``worth_saving`` anything but ``True``), in
  ``tool_drafts.py`` and ``tool_builder.py``;
* ``run_revise`` itself -- each statement binding ``holding``, ``shells_ended``
  or ``publish_running``.

Each one found by reading the syntax tree must be registered below, with what
the value claims about which object and the observation it rests on. A new one
fails the guard until it is registered; a registered one that is gone fails it
too, so the register stays the code as it is.

The register is written by hand: the guard checks that every producer is
registered, not that the observation a row names is made. It covers the two
files named and the forms the scanner knows (the tables of forms below).
"""

import ast
from collections import Counter
from pathlib import Path

import pytest

from afterthread.services import tool_builder, tool_drafts

# A save's result: each keyword, and the one constant that counts for keeping.
_KEEPING = {"saved": False, "durable": False, "worth_saving": True}
# The names ``run_revise``'s release condition is made of.
_ATOMS = ("holding", "shells_ended", "publish_running")


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


def _atom_bindings(source: str, module: str, function: str) -> list[tuple[str, str]]:
    """Every statement in ``function`` of ``source`` that binds one of ``_ATOMS``, in
    any binding form: (``module.function``, the statement's first line)."""
    tree = ast.parse(source)
    target = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function
        ),
        None,
    )
    assert target is not None, f"no function {function} to scan"
    parents = {
        child: parent for parent in ast.walk(target) for child in ast.iter_child_nodes(parent)
    }
    found: list[tuple[str, str]] = []
    for node in ast.walk(target):
        if isinstance(node, ast.Name) and node.id in _ATOMS and isinstance(node.ctx, ast.Store):
            statement: ast.AST = node
            while not isinstance(statement, ast.stmt):
                statement = parents[statement]
            found.append((f"{module}.{function}", ast.unparse(statement).splitlines()[0]))
    return found


def _producers() -> list[tuple[str, str]]:
    drafts = Path(tool_drafts.__file__).read_text(encoding="utf-8")
    builder = Path(tool_builder.__file__).read_text(encoding="utf-8")
    return [
        *_result_claims(drafts, "tool_drafts"),
        *_result_claims(builder, "tool_builder"),
        *_atom_bindings(builder, "tool_builder", "run_revise"),
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
        "tool_builder.run_revise",
        "shells_ended = True",
        "no session yet: no command was started, so none can be using the staging, which "
        "holds only the copy the round was given",
    ),
    (
        "tool_builder.run_revise",
        "shells_ended = False",
        "the session starts: commands may run from here, the claim withdrawn until the "
        "stop evidence",
    ),
    (
        "tool_builder.run_revise",
        "shells_ended = await shells.stop_and_wait()",
        "the shells_ended atom: every run_shell command's stop evidence "
        "(_BuilderShells.stop_and_wait)",
    ),
    (
        "tool_builder.run_revise",
        "holding = False",
        "before the commands are confirmed stopped there is no work to hold: the staging "
        "holds only the copy the round was given",
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
]


def test_every_value_that_counts_towards_removing_a_revise_s_staging_is_registered() -> None:
    found = Counter(_producers())
    registered = Counter((where, form) for where, form, _claim in _REGISTERED)
    assert found, "the scan found nothing at all: it is not reading the two files"
    assert all(claim.strip() for _where, _form, claim in _REGISTERED)
    unregistered = sorted((found - registered).elements())
    stale = sorted((registered - found).elements())
    assert not unregistered, f"producers with no registered claim: {unregistered}"
    assert not stale, f"registered producers no longer in the code: {stale}"


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
    ("def inner():\n    nonlocal holding\n    holding = True\n", "holding = True"),
]


@pytest.mark.parametrize(("source", "line"), _BOUND)
def test_the_staging_release_scanner_finds_each_binding_form_of_an_atom(
    source: str, line: str
) -> None:
    found = _atom_bindings(f"async def run_revise():\n{_indented(source)}", "m", "run_revise")
    assert found == [("m.run_revise", line)]


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


def _indented(source: str) -> str:
    """``source`` as the body of a function."""
    return "".join(f"    {line}\n" for line in source.splitlines())
