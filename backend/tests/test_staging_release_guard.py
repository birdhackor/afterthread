"""What an install, a revise and a publish of a draft as it is do as they end,
registered here and checked against the syntax tree: when each removes its
staging, the flag two of them keep their secret holds by, and every call and
binding in each ``finally`` clause of the three.

**The staging's release.** ``tool_builder.run_install`` removes its staging only
when its commands and writes are confirmed ended (``shells_ended``) and no worker
of its promote may still be writing it (not ``publish_running``).
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

Each one found must be registered (``_REGISTERED``), with what the value claims
about which object and the observation it rests on. So must the condition each
of the three functions calls ``_cleanup_staging`` under (``_RELEASES``).

**The flag the secret holds are kept by.** ``run_install`` and ``run_revise``
release the holds they took on secret values (``tools.register_inflight_secret``)
only when ``cancelled`` is down; each statement binding it in either is
registered (``_HOLD_FLAG``), as the atoms above are.

**Every finally clause of the three functions** -- the job's own, and the builder
session's inside it -- read where it is in the function itself, not in a
definition nested in it. Every call in one, awaited or not -- a helper called by
name, or one handed by name to a thread in the call's arguments -- every
``await`` of something other than a call, and every statement that binds or
unbinds a name, an attribute or a subscript is registered (``_CLOSING``): with
the condition it runs under, what it releases or rewrites, and which worker may
still be using that, or ``none`` and why. A helper is registered by its call as
written, and its row says what it does: the scan does not go into it. A ``def``,
``class`` or ``import`` there, or an ``except`` clause's name, binds without the
scan reading it, and what runs inside a definition made there is not read.

A condition is read as the ``if`` statements and conditional expressions around
a node make it, outermost first, joined by ``and``; ``True`` when none encloses
it. A condition made some other way -- a ``return`` or ``raise`` before the
node, a loop, a ``try``, a ``match``, a short-circuit ``and`` or ``or`` -- is not
read.

A new one fails the guard until it is registered; a registered one that is gone
fails it too, so the registers stay the code as it is. They are written by hand:
the guard checks that every one found is registered and every row is found --
not that what a row says is true, nor that every way out of a job by an
exception is safe. It covers the two files and the three functions named, and
the forms the scanners know (the tables of forms below).
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
# The flag ``run_install`` and ``run_revise`` keep their holds on secret values by.
_HOLD_ATOMS = ("cancelled",)
# What removes a job's staging.
_CLEANUP = "_cleanup_staging"
# The three jobs whose finally clauses are registered.
_JOBS = ("run_install", "run_revise", "run_publish_draft")
# A definition: what it defines does not run where it is defined.
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


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


def _looser_than_and(test: ast.expr) -> bool:
    """Whether ``test`` as written binds looser than ``and``: joined with another
    condition, it then needs parentheses to read as itself."""
    return isinstance(test, ast.IfExp | ast.Lambda) or (
        isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or)
    )


def _condition(node: ast.AST, target: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """The condition ``node`` is reached under in ``target``, as the enclosing ``if``
    statements and conditional expressions make it, outermost first, joined by
    ``and``; ``True`` when none encloses it. A node in an ``if``'s own test is not
    under that test. A test met true that binds looser than ``and`` is put in
    parentheses when it is joined with another."""
    held: list[tuple[ast.expr, bool]] = []
    child: ast.AST = node
    while child is not target:
        parent = parents[child]
        if isinstance(parent, ast.If | ast.IfExp) and child is not parent.test:
            in_body = child in parent.body if isinstance(parent, ast.If) else (child is parent.body)
            held.append((parent.test, in_body))
        child = parent
    held.reverse()
    parts = [
        _negated(test)
        if not in_body
        else f"({ast.unparse(test)})"
        if len(held) > 1 and _looser_than_and(test)
        else ast.unparse(test)
        for test, in_body in held
    ]
    return " and ".join(parts) or "True"


def _release_conditions(source: str, module: str, function: str) -> list[tuple[str, str]]:
    """The condition each mention of ``_CLEANUP`` in ``function`` of ``source`` is
    reached under (``_condition``): (``module.function``, the condition)."""
    target = _function(source, function)
    parents = _parents(target)
    found: list[tuple[str, str]] = []
    for node in ast.walk(target):
        named = (isinstance(node, ast.Name) and node.id == _CLEANUP) or (
            isinstance(node, ast.Attribute) and node.attr == _CLEANUP
        )
        if named:
            found.append((f"{module}.{function}", _condition(node, target, parents)))
    return found


def _here(node: ast.AST) -> list[ast.AST]:
    """``node`` and every node under it, but nothing under a definition: the body of
    a function, a lambda or a class does not run where it is defined."""
    found: list[ast.AST] = []
    pending = [node]
    while pending:
        current = pending.pop()
        found.append(current)
        if not isinstance(current, _DEFINITIONS):
            pending.extend(ast.iter_child_nodes(current))
    return found


def _finally_clauses(target: ast.FunctionDef | ast.AsyncFunctionDef) -> list[list[ast.stmt]]:
    """The body of each ``finally`` clause in ``target`` itself, however deep, but not
    in a definition nested in it."""
    return [
        node.finalbody
        for statement in target.body
        for node in _here(statement)
        if isinstance(node, ast.Try | ast.TryStar) and node.finalbody
    ]


def _statement(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.stmt:
    """The statement ``node`` is part of."""
    statement = node
    while not isinstance(statement, ast.stmt):
        statement = parents[statement]
    return statement


def _closing_actions(source: str, module: str, function: str) -> list[tuple[str, str, str]]:
    """What each ``finally`` clause of ``function`` in ``source`` does: every call, every
    ``await`` of something other than a call, and every statement binding or
    unbinding a name, an attribute or a subscript -- (``module.function``, the call or
    the ``await`` as written, or the statement's first line, the condition it runs
    under as ``_condition`` reads it). A node in a finally clause inside another is
    found once, and so is a statement binding several names."""
    target = _function(source, function)
    parents = _parents(target)
    where = f"{module}.{function}"
    found: list[tuple[str, str, str]] = []
    seen: set[ast.AST] = set()
    bound: set[ast.stmt] = set()
    for body in _finally_clauses(target):
        for statement in body:
            for node in _here(statement):
                if node in seen:
                    continue
                seen.add(node)
                if isinstance(node, ast.Call) or (
                    isinstance(node, ast.Await) and not isinstance(node.value, ast.Call)
                ):
                    found.append((where, ast.unparse(node), _condition(node, target, parents)))
                elif isinstance(node, ast.Name | ast.Attribute | ast.Subscript) and isinstance(
                    node.ctx, ast.Store | ast.Del
                ):
                    binding = _statement(node, parents)
                    if binding not in bound:
                        bound.add(binding)
                        line = ast.unparse(binding).splitlines()[0]
                        found.append((where, line, _condition(binding, target, parents)))
    return found


def _indented(source: str) -> str:
    """``source`` as the body of a function."""
    return "".join(f"    {line}\n" for line in source.splitlines())


def _finally(body: str) -> str:
    """A function body that is a ``try`` whose ``finally`` clause is ``body``."""
    return "try:\n    pass\nfinally:\n" + _indented(body)


def _unmatched[T](found: list[T], registered: list[T]) -> tuple[list[T], list[T]]:
    """What is found more often than registered, and what is registered more often
    than found -- each as often as it is over."""
    found_count, registered_count = Counter(found), Counter(registered)
    over = sorted((found_count - registered_count).elements(), key=repr)
    gone = sorted((registered_count - found_count).elements(), key=repr)
    return over, gone


def _builder_source() -> str:
    return Path(tool_builder.__file__).read_text(encoding="utf-8")


def _closing(source: str) -> list[tuple[str, str, str]]:
    """What each finally clause of the three jobs in ``source`` does (``_closing_actions``)."""
    return [action for job in _JOBS for action in _closing_actions(source, "tool_builder", job)]


def _producers() -> list[tuple[str, str]]:
    drafts = Path(tool_drafts.__file__).read_text(encoding="utf-8")
    builder = _builder_source()
    return [
        *_result_claims(drafts, "tool_drafts"),
        *_result_claims(builder, "tool_builder"),
        *_atom_bindings(builder, "tool_builder", "run_install", _INSTALL_ATOMS),
        *_atom_bindings(builder, "tool_builder", "run_revise"),
        *_atom_bindings(builder, "tool_builder", "run_publish_draft", _PUBLISH_ATOMS),
    ]


def _releases() -> list[tuple[str, str]]:
    builder = _builder_source()
    return [
        *_release_conditions(builder, "tool_builder", "run_install"),
        *_release_conditions(builder, "tool_builder", "run_revise"),
        *_release_conditions(builder, "tool_builder", "run_publish_draft"),
    ]


def _hold_flag_bindings() -> list[tuple[str, str]]:
    builder = _builder_source()
    return [
        *_atom_bindings(builder, "tool_builder", "run_install", _HOLD_ATOMS),
        *_atom_bindings(builder, "tool_builder", "run_revise", _HOLD_ATOMS),
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
    assert all(claim.strip() for _where, _under, claim in _RELEASES)
    unregistered = sorted((found - registered).elements())
    stale = sorted((registered - found).elements())
    assert not unregistered, f"removals under a condition not registered: {unregistered}"
    assert not stale, f"registered removal conditions no longer in the code: {stale}"


# Each statement binding the flag ``run_install`` and ``run_revise`` keep their
# holds on secret values by: (where, form, what the value claims and the observation
# it rests on).
_HOLD_FLAG: list[tuple[str, str, str]] = [
    row
    for where in ("tool_builder.run_install", "tool_builder.run_revise")
    for row in (
        (
            where,
            "cancelled = False",
            "nothing has left the job's try yet: a return keeps it down, and the finally then "
            "releases the job's holds",
        ),
        (
            where,
            "cancelled = not isinstance(exc, Exception)",
            "the except clause on the job's try, which runs before any step of the finally: up "
            "for a BaseException that is not an Exception -- the event loop's shutdown "
            "cancelling the job, an interrupt, an exit, each the process ending -- and down "
            "for an Exception; no step of the finally binds it again",
        ),
    )
]


def test_each_binding_of_the_flag_the_secret_holds_are_kept_by_is_registered() -> None:
    found = Counter(_hold_flag_bindings())
    registered = Counter((where, form) for where, form, _claim in _HOLD_FLAG)
    assert found, "the scan found no binding of the flag: it is not reading the two functions"
    assert all(claim.strip() for _where, _form, claim in _HOLD_FLAG)
    unregistered = sorted((found - registered).elements())
    stale = sorted((registered - found).elements())
    assert not unregistered, f"bindings of the flag with no registered claim: {unregistered}"
    assert not stale, f"registered bindings of the flag no longer in the code: {stale}"


def _log_line(helper: str, why: str) -> str:
    """What one of the log helpers a finally clause calls does."""
    return (
        f"{helper}: writes one warning in the backend log naming the session root, which "
        f"stays: {why}; it releases and rewrites nothing"
    )


# Shared texts of the rows below.
_TOUCHES_NOTHING = "none: it touches no file and no state a worker uses"
_A_LOCAL = "none: a local of this frame"
_STOP_AND_WAIT = (
    "_BuilderShells.stop_and_wait: sets the session's stop signal, then waits, within one "
    "deadline, for every run_shell command's stop evidence and every write_file write's word "
    "that its worker returned; it releases nothing"
)
_STOPPED_BY_IT = (
    "the session's commands and writes: the stop is what ends them, and one not confirmed "
    "stopped leaves shells_ended down, which keeps the workspace whole"
)
_SWEEP = (
    "_sweep_logged: sweeps the tools directory (_sweep_stale_backups) under the exclusive "
    "tools lock taken without blocking -- marked packages, discarded versions, draft "
    "leftovers (.incoming-*, draft.discarded-*), never anything inside .staging -- each "
    "failure logged; the staging stays"
)
_CLEANUP_DOES = (
    "_cleanup_staging: removes the session root once it passes _verify_staging_root, and "
    ".staging once that left it empty, then sweeps as _sweep_logged does"
)
_NONE_LEFT_TO_USE_THE_HOLDS = (
    "none: on a return or an Exception every worker that took the known secrets after the "
    "{held} taken, to make something seen or written -- {workers} -- was awaited to its "
    "result or to its own exception, or never began; what may still run is a command or a "
    "write not confirmed stopped, or a read_file or list_dir, and no one receives what they "
    "make"
)

# Each call and each binding in a finally clause of the three jobs: (where, the call
# or binding as the scan reports it, the condition it runs under, what it releases or
# rewrites, which worker may still be using that -- or none, and why). A form that
# occurs twice in a function under one condition is registered twice.
_CLOSING: list[tuple[str, str, str, str, str]] = [
    # --- run_install: the builder session's finally ----------------------------------
    ("tool_builder.run_install", "shells.stop_and_wait()", "True", _STOP_AND_WAIT, _STOPPED_BY_IT),
    (
        "tool_builder.run_install",
        "shells_ended = await shells.stop_and_wait()",
        "True",
        "rewrites shells_ended, an atom of the staging's release (_REGISTERED)",
        _A_LOCAL,
    ),
    # --- run_install: the job's finally --------------------------------------------------
    (
        "tool_builder.run_install",
        "tools.discard_inflight_secret(secret_value)",
        "secret_value and (not cancelled)",
        "releases this job's own hold on the form's secret: the value stays a known secret "
        "only while another holder holds it or a package's .env does",
        _NONE_LEFT_TO_USE_THE_HOLDS.format(
            held="hold was", workers="the promote, the summary's store"
        ),
    ),
    (
        "tool_builder.run_install",
        "_log_workspace_kept(session_root)",
        "not shells_ended",
        _log_line("_log_workspace_kept", "its commands or writes are not confirmed stopped"),
        _TOUCHES_NOTHING,
    ),
    (
        "tool_builder.run_install",
        "_log_publish_running_kept(session_root)",
        "shells_ended and publish_running",
        _log_line("_log_publish_running_kept", "the job stopped while its promote still ran"),
        _TOUCHES_NOTHING,
    ),
    (
        "tool_builder.run_install",
        "run_in_threadpool(_sweep_logged, base)",
        "shells_ended and publish_running",
        _SWEEP,
        "the promote's worker, the job having stopped while it ran: it writes this session's "
        "staging and renames its shell to the tool's name, neither of which the sweep collects",
    ),
    (
        "tool_builder.run_install",
        "run_in_threadpool(_cleanup_staging, session_root, base)",
        "shells_ended and not (publish_running)",
        _CLEANUP_DOES,
        "the strip of the builder's .env, when the job was cancelled while it ran: both only "
        "remove, the removal passes over an entry already gone, and nothing reads the strip's "
        "answer; a summary's store still running writes into the installed version, not the "
        "staging; no command, write or promote worker -- the condition says so",
    ),
    # --- run_revise: the builder session's finally -----------------------------------
    ("tool_builder.run_revise", "shells.stop_and_wait()", "True", _STOP_AND_WAIT, _STOPPED_BY_IT),
    (
        "tool_builder.run_revise",
        "shells_ended = await shells.stop_and_wait()",
        "True",
        "rewrites shells_ended, an atom of the staging's release (_REGISTERED)",
        _A_LOCAL,
    ),
    (
        "tool_builder.run_revise",
        "holding = shells_ended",
        "True",
        "rewrites holding, an atom of the staging's release (_REGISTERED)",
        _A_LOCAL,
    ),
    # --- run_revise: the job's finally ---------------------------------------------------
    (
        "tool_builder.run_revise",
        "for value in registered:",
        "not cancelled",
        "rewrites value, the loop's own local",
        _A_LOCAL,
    ),
    (
        "tool_builder.run_revise",
        "tools.discard_inflight_secret(value)",
        "not cancelled",
        "releases this job's own hold on each .env value it registered: a value stays a known "
        "secret only while another holder holds it or a package's .env does",
        _NONE_LEFT_TO_USE_THE_HOLDS.format(
            held="holds were",
            workers="the copy of a draft, the prompts, the masks, the save, the record, the "
            "marks of the manifest, the publish, the summary's store, the examples",
        ),
    ),
    (
        "tool_builder.run_revise",
        "_log_workspace_kept(session_root)",
        "not shells_ended",
        _log_line("_log_workspace_kept", "its commands or writes are not confirmed stopped"),
        _TOUCHES_NOTHING,
    ),
    (
        "tool_builder.run_revise",
        "run_in_threadpool(_strip_builder_env, build_root, base)",
        "shells_ended and holding and (not env_stripped) and (not publish_running)",
        "_strip_builder_env: removes the root .env the builder wrote from the staging, once "
        "the staging root is verified again, reading only its key names",
        "the first strip's worker, when the round was cancelled while it ran: both remove the "
        "same entry; the masking of the notes, the record or the look at the workspace may "
        "still run beside it, and none of them reads the root .env; no save -- one starts "
        "only after a strip went through, which raises env_stripped",
    ),
    (
        "tool_builder.run_revise",
        "_late_keys, late_error = await run_in_threadpool(_strip_builder_env, build_root, base)",
        "shells_ended and holding and (not env_stripped) and (not publish_running)",
        "rewrites _late_keys and late_error, locals of this frame",
        "none: locals of this frame",
    ),
    (
        "tool_builder.run_revise",
        "env_stripped = late_error is None",
        "shells_ended and holding and (not env_stripped) and (not publish_running)",
        "rewrites env_stripped, which picks the log line below",
        _A_LOCAL,
    ),
    (
        "tool_builder.run_revise",
        "_log_copy_running_kept(session_root)",
        "shells_ended and (holding or publish_running or copy_running) and copy_running",
        _log_line("_log_copy_running_kept", "the copy into it may still be writing it"),
        _TOUCHES_NOTHING,
    ),
    (
        "tool_builder.run_revise",
        "_log_publish_running_kept(session_root)",
        "shells_ended and (holding or publish_running or copy_running) and not (copy_running) "
        "and publish_running",
        _log_line("_log_publish_running_kept", "the job stopped while its publish still ran"),
        _TOUCHES_NOTHING,
    ),
    (
        "tool_builder.run_revise",
        "_log_work_kept(session_root)",
        "shells_ended and (holding or publish_running or copy_running) and not (copy_running) "
        "and not (publish_running) and env_stripped",
        _log_line("_log_work_kept", "no draft is known to hold its work"),
        _TOUCHES_NOTHING,
    ),
    (
        "tool_builder.run_revise",
        "_log_work_kept_unstripped(session_root)",
        "shells_ended and (holding or publish_running or copy_running) and not (copy_running) "
        "and not (publish_running) and not (env_stripped)",
        _log_line(
            "_log_work_kept_unstripped",
            "no draft is known to hold its work, and the builder's .env may still be in it",
        ),
        _TOUCHES_NOTHING,
    ),
    (
        "tool_builder.run_revise",
        "run_in_threadpool(_sweep_logged, base)",
        "shells_ended and (holding or publish_running or copy_running)",
        _SWEEP,
        "the copy of a draft, a save, a record and a mark of the manifest touch the draft "
        "only under the shared tools lock, which keeps the sweep out -- it skips when it "
        "cannot take the exclusive one; the copy of a version and the publish hold no lock, "
        "and write only this session's staging and versions/<vid>, neither of which the "
        "sweep collects",
    ),
    (
        "tool_builder.run_revise",
        "run_in_threadpool(_cleanup_staging, session_root, base)",
        "shells_ended and not (holding or publish_running or copy_running)",
        _CLEANUP_DOES,
        "none writing the staging: what may still run either reads the workspace and has its "
        "answer dropped -- its digest, its tool.json, whether it holds a config.env, for a "
        "round cancelled before its session -- or works outside it -- a write of the "
        "manifest, the removal of the draft, the summary's store, the examples; no command, "
        "write, copy or publish worker, and no work held -- the condition says so",
    ),
    # --- run_publish_draft: the job's finally --------------------------------------------
    (
        "tool_builder.run_publish_draft",
        "_log_publish_draft_kept(session_root, worker=copy_running or publish_running)",
        "copy_running or publish_running or holding",
        _log_line(
            "_log_publish_draft_kept",
            "a worker copying the draft in or publishing it may still be writing it, or the "
            "draft is not known to hold what it holds",
        ),
        _TOUCHES_NOTHING,
    ),
    (
        "tool_builder.run_publish_draft",
        "run_in_threadpool(_sweep_logged, base)",
        "copy_running or publish_running or holding",
        _SWEEP,
        "the copy of the draft, the marks of the manifest and the look at the draft touch the "
        "draft only under the shared tools lock, which keeps the sweep out -- it skips when it "
        "cannot take the exclusive one; the publish holds no lock, and writes only this "
        "session's staging and versions/<vid>, neither of which the sweep collects",
    ),
    (
        "tool_builder.run_publish_draft",
        "run_in_threadpool(_cleanup_staging, session_root, base)",
        "not (copy_running or publish_running or holding)",
        _CLEANUP_DOES,
        "none writing the staging: what may still run works outside it -- the mask of the "
        "origin's feedback, a mark of the manifest, the removal of the draft, the examples; no "
        "copy or publish worker, and nothing held -- the condition says so",
    ),
]


def test_each_call_and_binding_in_each_finally_clause_of_the_three_jobs_is_registered() -> None:
    source = _builder_source()
    for function in _JOBS:
        assert _finally_clauses(_function(source, function)), f"no finally clause in {function}"
    assert all(text.strip() for row in _CLOSING for text in row[3:])
    unregistered, stale = _unmatched(_closing(source), [row[:3] for row in _CLOSING])
    assert not unregistered, f"calls or bindings in a finally clause with no row: {unregistered}"
    assert not stale, f"rows no call or binding in a finally clause matches: {stale}"


def _with_a_call_in_each_finally(source: str, function: str, call: str) -> str:
    """``source`` with ``call`` added as the last statement of each finally clause of
    ``function``."""
    tree = ast.parse(source)
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function
    )
    for body in _finally_clauses(target):
        body.append(ast.parse(call).body[0])
    return ast.unparse(tree)


@pytest.mark.parametrize("function", _JOBS)
def test_a_call_added_to_each_finally_of_a_job_is_named_as_having_no_row(function: str) -> None:
    added = "tools.release_something(session_root)"
    source = _with_a_call_in_each_finally(_builder_source(), function, added)
    clauses = _finally_clauses(_function(source, function))
    assert clauses
    unregistered, stale = _unmatched(_closing(source), [row[:3] for row in _CLOSING])
    assert unregistered == [(f"tool_builder.{function}", added, "True")] * len(clauses)
    assert stale == []


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
    ("if a or b:\n    if c:\n        _cleanup_staging(root)\n", "(a or b) and c"),
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


# Each form the finally scanner must find, as a function's body: (source, each
# (call or binding, condition) it holds).
_FOUND_IN_A_FINALLY: list[tuple[str, list[tuple[str, str]]]] = [
    (_finally("f(x)\n"), [("f(x)", "True")]),
    (
        _finally("tools.discard_inflight_secret(value)\n"),
        [("tools.discard_inflight_secret(value)", "True")],
    ),
    (
        _finally("await run_in_threadpool(_sweep_logged, base)\n"),
        [("run_in_threadpool(_sweep_logged, base)", "True")],
    ),
    (_finally("h(g())\n"), [("h(g())", "True"), ("g()", "True")]),
    (_finally("await fut\n"), [("await fut", "True")]),
    (_finally("f(x=1)\n"), [("f(x=1)", "True")]),
    (_finally("if a:\n    f()\n"), [("f()", "a")]),
    (_finally("if a:\n    pass\nelse:\n    f()\n"), [("f()", "not (a)")]),
    (_finally("if not a:\n    f()\nelif b:\n    g()\n"), [("f()", "not a"), ("g()", "a and b")]),
    (_finally("if a or b:\n    if c:\n        f()\n"), [("f()", "(a or b) and c")]),
    (_finally("if f():\n    g()\n"), [("f()", "True"), ("g()", "f()")]),
    (_finally("x = f() if a else None\n"), [("x = f() if a else None", "True"), ("f()", "a")]),
    (_finally("if a:\n    x = f()\n"), [("x = f()", "a"), ("f()", "a")]),
    (_finally("x = 1\n"), [("x = 1", "True")]),
    (_finally("x: bool = y\n"), [("x: bool = y", "True")]),
    (_finally("x |= y\n"), [("x |= y", "True")]),
    (
        _finally("a, b = await run_in_threadpool(f)\n"),
        [("a, b = await run_in_threadpool(f)", "True"), ("run_in_threadpool(f)", "True")],
    ),
    (_finally("self.flag = True\n"), [("self.flag = True", "True")]),
    (_finally("d[k] = v\n"), [("d[k] = v", "True")]),
    (_finally("del x\n"), [("del x", "True")]),
    (_finally("for v in xs:\n    f(v)\n"), [("for v in xs:", "True"), ("f(v)", "True")]),
    (_finally("with f() as x:\n    pass\n"), [("with f() as x:", "True"), ("f()", "True")]),
    (_finally("if (x := f()):\n    pass\n"), [("if (x := f()):", "True"), ("f()", "True")]),
    (_finally("g = lambda: f()\n"), [("g = lambda: f()", "True")]),
    (
        "try:\n    try:\n        pass\n    finally:\n        f()\nfinally:\n    g()\n",
        [("f()", "True"), ("g()", "True")],
    ),
    (_finally("try:\n    pass\nfinally:\n    f()\n"), [("f()", "True")]),
    ("try:\n    pass\nexcept* ValueError:\n    pass\nfinally:\n    f()\n", [("f()", "True")]),
]


@pytest.mark.parametrize(("source", "found"), _FOUND_IN_A_FINALLY)
def test_the_finally_scanner_finds_each_listed_form(
    source: str, found: list[tuple[str, str]]
) -> None:
    actions = _closing_actions(f"async def where():\n{_indented(source)}", "m", "where")
    assert sorted(actions) == sorted(("m.where", action, under) for action, under in found)


# What runs in no finally clause, or binds and calls nothing: not found.
_NOT_IN_A_FINALLY: list[str] = [
    "try:\n    f()\n    x = 1\nfinally:\n    pass\n",
    "try:\n    pass\nexcept Exception:\n    f()\n    x = 1\nfinally:\n    pass\n",
    "try:\n    pass\nexcept Exception:\n    pass\nelse:\n    f()\n",
    "f()\nx = 1\n",
    _finally("if a and not b:\n    pass\n"),
    _finally("def inner():\n    f()\n    x = 1\n"),
    "def inner():\n    try:\n        pass\n    finally:\n        f()\n",
]


@pytest.mark.parametrize("source", _NOT_IN_A_FINALLY)
def test_the_finally_scanner_passes_over_what_no_finally_clause_runs(source: str) -> None:
    assert _closing_actions(f"async def where():\n{_indented(source)}", "m", "where") == []
