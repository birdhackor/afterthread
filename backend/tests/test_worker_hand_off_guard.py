"""Every call in ``tool_builder`` that hands work to another thread, in a form the
scanner knows, is classified here: what the worker does, what it still does once
the coroutine that handed it over is cancelled or stops waiting, and what keeps it
from racing anything else.

The scanner reads the syntax tree of ``tool_builder.py``, every function in it --
module functions, methods and nested functions alike -- and finds two kinds of
hand-off:

* a primitive one (``_PRIMITIVES``): ``run_in_threadpool``, ``asyncio.to_thread``,
  anyio's ``run_sync``, ``run_in_executor``, an executor's ``submit``, a
  ``threading.Thread``, and a job put on ``_SETUP_QUEUE``, the construction
  thread's queue. The callable handed over is read with every
  ``functools.partial`` around it taken off;
* one made through a helper, one call away: a function that hands one of its own
  parameters to a primitive -- itself, or through a function nested in it that
  calls the parameter -- hands over, at each of its calls, what that call passes
  for the parameter. A method is matched by its name on any attribute call, a
  module function by its name on any plain call.

Each hand-off is keyed by the function it is in, how it hands over (the primitive,
or the helper's qualified name), the callable handed over, and what its result is
bound to -- never by line. A found hand-off with no row fails the guard, and so does
a row no hand-off matches, so the register stays the code as it is. Rows with the
same key are interchangeable and registered once each.

The meta-tools, the work a builder session hands out on the model's word, allow two
ways only: through the session (a ``_BuilderShells`` method, whose stop waits for
the worker), or one of the readers named in ``_META_TOOL_READ_ONLY``. What a handler
hands over counts there with what a module function it calls by name hands over,
one call away.

Awaited calls into the backend's other service modules are registered as
boundaries (``_BOUNDARIES``): the workers beyond them are not in this register, and
their lifetime is not managed by ``tool_builder``.

What it does not read: a primitive reached through another name (an alias, a
variable holding it), a helper two calls away, a hand-off made outside a function
call expression. The register is written by hand: the guard checks that every
hand-off has a row and every row a hand-off, not that what a row says is true.

One more check runs the same scanner over every module of the backend, not only
``tool_builder``: no tools-lock function (``_LOCK_FUNCTIONS``) is handed to a
thread by name.
"""

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from afterthread.services import tool_builder

# Each primitive form: the name it is called by -- a plain name, or an attribute's
# -- and where among its arguments it takes the callable it hands over: a position,
# and a keyword that takes precedence when given.
_PRIMITIVES: dict[str, tuple[int, str | None]] = {
    "run_in_threadpool": (0, None),
    "to_thread": (0, None),
    "run_sync": (0, None),
    "run_in_executor": (1, None),
    "submit": (0, None),
    "Thread": (1, "target"),
}
# The construction thread's queue: each job put on it is a (callable, future) pair.
_SETUP_QUEUE = "_SETUP_QUEUE"
# How a hand-off made through the session reads: the helper is a method of this class.
_SESSION = "_BuilderShells."
# The meta-tool readers that may hand their work to a thread themselves: (the
# handler, the callable it hands over).
_META_TOOL_READ_ONLY = frozenset(
    {
        ("_build_meta_tools.read_file", "_read"),
        ("_build_meta_tools.list_dir", "_list"),
    }
)


@dataclass(frozen=True, slots=True)
class HandOff:
    """One call that hands work to another thread.

    ``where`` is the function the call is in, qualified (``Class.method``,
    ``outer.inner``); ``how`` the primitive, or the qualified name of the helper the
    call goes through; ``worker`` the callable handed over, as written, with every
    ``partial`` taken off; ``role`` what the call's result is bound to: the target of
    an assignment, the name a ``with`` item binds, ``return``, ``-`` for a result
    left unused, or the kind of any other statement it is in.
    """

    where: str
    how: str
    worker: str
    role: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.where, self.how, self.worker, self.role)


def _callee(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ast.unparse(func)


def _unwrapped(expr: ast.expr) -> ast.expr:
    """``expr`` with every ``partial(...)`` around it taken off."""
    while isinstance(expr, ast.Call) and _callee(expr.func) == "partial" and expr.args:
        expr = expr.args[0]
    return expr


def _argument(call: ast.Call, index: int, keyword: str | None) -> ast.expr | None:
    if keyword is not None:
        for item in call.keywords:
            if item.arg == keyword:
                return item.value
    return call.args[index] if len(call.args) > index else None


def _primitive(call: ast.Call) -> tuple[str, ast.expr] | None:
    """The primitive form ``call`` is and the callable it hands over; None for any
    other call."""
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "put"
        and isinstance(func.value, ast.Name)
        and func.value.id == _SETUP_QUEUE
    ):
        job = call.args[0] if call.args else None
        if isinstance(job, ast.Tuple) and job.elts:
            return f"{_SETUP_QUEUE}.put", _unwrapped(job.elts[0])
        return None
    name = _callee(func)
    if name not in _PRIMITIVES:
        return None
    handed = _argument(call, *_PRIMITIVES[name])
    return None if handed is None else (name, _unwrapped(handed))


type _Function = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True, slots=True)
class _Tree:
    """One module's syntax tree, read once: every node's parent, and every function
    with its qualified name and whether it is a method."""

    tree: ast.Module
    parents: dict[ast.AST, ast.AST]
    names: dict[_Function, str]
    methods: frozenset[_Function]

    @classmethod
    def of(cls, source: str) -> _Tree:
        tree = ast.parse(source)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        names: dict[_Function, str] = {}
        methods: set[_Function] = set()

        def visit(node: ast.AST, prefix: str, in_class: bool) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    visit(child, f"{prefix}{child.name}.", True)
                elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    names[child] = f"{prefix}{child.name}"
                    if in_class:
                        methods.add(child)
                    visit(child, f"{prefix}{child.name}.", False)
                else:
                    visit(child, prefix, in_class)

        visit(tree, "", False)
        return cls(tree, parents, names, frozenset(methods))

    def where(self, node: ast.AST) -> str:
        """The qualified name of the function ``node`` is in."""
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                return self.names[node]
        return "<module>"

    def role(self, node: ast.AST) -> str:
        """What the result of ``node`` is bound to (``HandOff.role``)."""
        child = node
        while not isinstance(child, ast.stmt):
            parent = self.parents[child]
            if isinstance(parent, ast.withitem) and child is parent.context_expr:
                return "with" if parent.optional_vars is None else ast.unparse(parent.optional_vars)
            child = parent
        if isinstance(child, ast.Assign):
            return " = ".join(ast.unparse(target) for target in child.targets)
        if isinstance(child, ast.AnnAssign | ast.AugAssign):
            return ast.unparse(child.target)
        if isinstance(child, ast.Return):
            return "return"
        if isinstance(child, ast.Expr):
            return "-"
        return type(child).__name__.lower()


@dataclass(frozen=True, slots=True)
class _Forward:
    """A helper handing one of its parameters to a thread: its qualified name,
    whether it is a method, and the parameter by name and by position among the
    arguments a call passes."""

    helper: str
    method: bool
    parameter: str
    index: int


def _parameters(function: _Function, method: bool) -> list[str]:
    arguments = function.args
    names = [item.arg for item in [*arguments.posonlyargs, *arguments.args]]
    return names[1:] if method else names


def _calls_one_of(function: _Function, names: list[str]) -> str | None:
    """The first of ``names`` that ``function`` calls by name, if any."""
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names:
            return node.func.id
    return None


def _forwards(read: _Tree, sites: list[tuple[ast.Call, HandOff]]) -> dict[str, list[_Forward]]:
    """Every helper that hands one of its own parameters to a primitive -- itself, or
    through a function nested in it that calls the parameter -- by its plain name."""
    found: dict[str, list[_Forward]] = {}
    for function, name in read.names.items():
        method = function in read.methods
        parameters = _parameters(function, method)
        nested = {
            child.name: child
            for child in ast.iter_child_nodes(function)
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for _call, hand_off in sites:
            if hand_off.where != name:
                continue
            forwarded: str | None = None
            if hand_off.worker in parameters:
                forwarded = hand_off.worker
            elif hand_off.worker in nested:
                forwarded = _calls_one_of(nested[hand_off.worker], parameters)
            if forwarded is not None:
                found.setdefault(function.name, []).append(
                    _Forward(name, method, forwarded, parameters.index(forwarded))
                )
    return found


def _hand_offs(source: str) -> list[HandOff]:
    """Every hand-off to another thread in ``source``, in the forms the scanner knows."""
    read = _Tree.of(source)
    calls = [node for node in ast.walk(read.tree) if isinstance(node, ast.Call)]
    sites: list[tuple[ast.Call, HandOff]] = []
    for call in calls:
        primitive = _primitive(call)
        if primitive is not None:
            how, worker = primitive
            hand_off = HandOff(read.where(call), how, ast.unparse(worker), read.role(call))
            sites.append((call, hand_off))
    forwards = _forwards(read, sites)
    through: list[HandOff] = []
    for call in calls:
        attribute = isinstance(call.func, ast.Attribute)
        if not attribute and not isinstance(call.func, ast.Name):
            continue
        for forward in forwards.get(_callee(call.func), []):
            if forward.method != attribute:
                continue
            handed = _argument(call, forward.index, forward.parameter)
            if handed is not None:
                worker = ast.unparse(_unwrapped(handed))
                through.append(HandOff(read.where(call), forward.helper, worker, read.role(call)))
    return [hand_off for _call, hand_off in sites] + through


def _meta_tool_hand_offs(source: str) -> list[HandOff]:
    """What each meta-tool handler of ``_build_meta_tools`` hands to a thread: its own
    hand-offs, its nested functions' included, and those of a module function it
    calls by name, one call away, each read as made by the handler through that
    function."""
    read = _Tree.of(source)
    hand_offs = _hand_offs(source)
    builder = next(
        node
        for node in read.tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "_build_meta_tools"
    )
    handlers = [
        item.value.id
        for node in ast.walk(builder)
        if isinstance(node, ast.Call) and _callee(node.func) == "LlmTool"
        for item in node.keywords
        if item.arg == "handler" and isinstance(item.value, ast.Name)
    ]
    nested = {
        child.name: child
        for child in ast.iter_child_nodes(builder)
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    module_functions = {
        node.name
        for node in read.tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    def inside(hand_off: HandOff, function: str) -> bool:
        return hand_off.where == function or hand_off.where.startswith(f"{function}.")

    found: list[HandOff] = []
    for handler in handlers:
        where = f"_build_meta_tools.{handler}"
        found += [hand_off for hand_off in hand_offs if inside(hand_off, where)]
        called = sorted(
            {
                node.func.id
                for node in ast.walk(nested[handler])
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in module_functions
            }
        )
        for function in called:
            found += [
                HandOff(where, function, hand_off.worker, hand_off.role)
                for hand_off in hand_offs
                if inside(hand_off, function)
            ]
    return found


def _third_ways(source: str) -> list[HandOff]:
    """What a meta-tool hands to a thread neither through the session nor as one of
    the named readers."""
    return [
        hand_off
        for hand_off in _meta_tool_hand_offs(source)
        if not hand_off.how.startswith(_SESSION)
        and (hand_off.where, hand_off.worker) not in _META_TOOL_READ_ONLY
    ]


def _boundaries(source: str) -> list[tuple[str, str]]:
    """Every awaited call in ``source`` into another of the backend's service modules:
    (the function it is in, the call as written)."""
    read = _Tree.of(source)
    modules: set[str] = set()
    imported: set[str] = set()
    for node in read.tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if node.module == "afterthread.services":
            modules |= {alias.asname or alias.name for alias in node.names}
        elif node.module.startswith("afterthread.services."):
            imported |= {alias.asname or alias.name for alias in node.names}
    found: list[tuple[str, str]] = []
    for node in ast.walk(read.tree):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
        ):
            found.append((read.where(node), f"{func.value.id}.{func.attr}"))
        elif isinstance(func, ast.Name) and func.id in imported:
            found.append((read.where(node), func.id))
    return found


def _unmatched[T](found: list[T], registered: list[T]) -> tuple[list[T], list[T]]:
    """What is found more often than registered, and what is registered more often
    than found -- each as often as it is over."""
    found_count, registered_count = Counter(found), Counter(registered)
    over = sorted((found_count - registered_count).elements(), key=repr)
    gone = sorted((registered_count - found_count).elements(), key=repr)
    return over, gone


def _builder_source() -> str:
    return Path(tool_builder.__file__).read_text(encoding="utf-8")


# What a reader still does once nothing waits for it, and what keeps it apart.
_READ_ON = "finishes its read; its answer is dropped, and nothing waits for it"
_WRITES_NOTHING = "nothing needed: it writes nothing"
_FILLS_THE_CACHE = (
    "nothing needed: it writes no file, and the .env value cache it fills is kept under "
    "its own lock, each entry tagged with its file's identity"
)
# What a reader that masks with the known secrets reads besides.
_READS_THE_SECRETS = "every package's .env, for the known secrets"

# A submit's reads before its job is admitted: what each still does once the request
# is gone, and what keeps it apart.
_REQUEST_READ_ON = (
    "finishes its read; the request's answer is dropped, and the request's finally gives "
    "its reservation of the slot back"
)
_UNDER_THE_RESERVATION = (
    "nothing needed: it writes no file, and the reservation it reads under keeps the other "
    "jobs and the synchronous operations out until the job is admitted"
)

# The end of a job's finally: what it still does past a second cancellation, and what
# keeps the sweep apart.
_FINALLY_RUNS_ON = (
    "a second cancellation of the finally leaves it running; nothing reads its answer"
)
_SWEEP_DOES = (
    "sweeps the tools directory -- marked packages, discarded versions, draft leftovers -- "
)
_SWEEP_APART = (
    "the exclusive tools lock, taken without blocking: it skips while anything holds the "
    "shared lock; it never looks inside .staging"
)
_CLEANUP_DOES = (
    "removes the session root once it passes _verify_staging_root, drops .staging when "
    "empty, then sweeps the tools directory"
)

# Each row: (where, how, the callable handed over, what its result is bound to, what
# the worker does, what it still does once the coroutine that handed it over is
# cancelled or stops waiting, what keeps it from racing).
_CLASSIFIED: list[tuple[str, str, str, str, str, str, str]] = [
    # --- the builder session ----------------------------------------------------
    (
        "_BuilderShells.run",
        "run_in_threadpool",
        "worker",
        "task",
        "runs one run_shell command in the workspace (_run_shell_subprocess): bash in a "
        "process group of its own, with the service's rights, so it can write anywhere the "
        "service can; tools._communicate_bounded drains its output on reader threads of its "
        "own, joined in its teardown within what is left of its limit",
        "runs on to its own end or its timeout; once the stop is set, the drain kills and "
        "reaps its group at its next poll, and the worker reports whether that was confirmed",
        "the session: _BuilderShells.stop_and_wait waits for the worker's report within the "
        "one deadline; unconfirmed, the workspace is kept, and nothing strips, saves, "
        "publishes or removes it",
    ),
    (
        "_BuilderShells.write",
        "run_in_threadpool",
        "worker",
        "task",
        "runs one write_file write (the _build_meta_tools.write_file row says what it "
        "writes); a worker that enters after the stop writes nothing",
        "runs on to the end of the write, then reports from its own finally that it returned",
        "the session: _BuilderShells.stop_and_wait waits for that report within the one "
        "deadline; without it, the workspace is kept, and nothing strips, saves, publishes "
        "or removes it",
    ),
    (
        "_build_meta_tools.write_file",
        "_BuilderShells.write",
        "_write",
        "return",
        "creates the parent directories, then truncates and writes one file in the "
        "workspace (tools._write_regular_file)",
        "runs on to the end of its write -- at the deadline the file may be cut short, and "
        "the worker goes on writing it -- unless its worker entered after the stop, and then "
        "it writes nothing",
        "the session: the write's worker is waited for before the workspace is stripped, "
        "saved, published or removed",
    ),
    (
        "_build_meta_tools.read_file",
        "run_in_threadpool",
        "_read",
        "return",
        f"reads one file of the workspace, and {_READS_THE_SECRETS} to mask it",
        _READ_ON,
        _FILLS_THE_CACHE + "; a read beside a save or a removal changes neither",
    ),
    (
        "_build_meta_tools.list_dir",
        "run_in_threadpool",
        "_list",
        "return",
        f"walks a directory of the workspace, and reads {_READS_THE_SECRETS} to mask the listing",
        _READ_ON,
        _FILLS_THE_CACHE + "; a walk beside a save or a removal changes neither",
    ),
    # --- the construction thread --------------------------------------------------
    (
        "_ensure_setup_worker",
        "Thread",
        "_setup_worker_loop",
        "worker",
        "starts the one construction thread, which builds the OpenAPI fetch's HTTP clients "
        "from its queue for the life of the process; it writes no file",
        "lives on for the process: a job it has begun is finished, its client dropped when "
        "the waiter gave up, and a job cancelled while queued is skipped",
        "_SETUP_WORKER_LOCK starts one such thread at most; it touches no file and no job state",
    ),
    (
        "_run_in_setup_worker",
        "_SETUP_QUEUE.put",
        "make_client",
        "-",
        "puts its caller's construction on the construction thread's queue; what is handed "
        "over is classified at each caller's row",
        "a construction still queued is skipped once its future is cancelled; one already "
        "running is finished and its client dropped",
        "the construction thread runs one job at a time",
    ),
    (
        "_fetch_openapi",
        "_run_in_setup_worker",
        "_client_cm",
        "client",
        "builds the fetch's httpx2 client (its TLS context and trust store); it writes no file",
        "a construction already running is finished and the client dropped unopened; one "
        "still queued never runs",
        _WRITES_NOTHING,
    ),
    (
        "_drain_setup_worker_for_tests",
        "_SETUP_QUEUE.put",
        "lambda: None",
        "-",
        "tests only: a job that does nothing, to show the construction thread has caught up",
        "nothing cancels it: its caller is no coroutine, and blocks on its result with a timeout",
        _WRITES_NOTHING,
    ),
    # --- the install ------------------------------------------------------------
    (
        "run_install",
        "run_in_threadpool",
        "session_root.mkdir",
        "-",
        "creates the session's root under the tools directory's .staging, and .staging when "
        "it is missing",
        "finishes; the await comes before the try whose finally cleans up, so what it made "
        "stays in .staging",
        "a fresh random name: no other job creates, reads or removes it",
    ),
    (
        "run_install",
        "run_in_threadpool",
        "build_root.path.mkdir",
        "-",
        "creates the session's build directory",
        "finishes; the await comes before the try whose finally cleans up, like the root's",
        "a fresh random name: no other job creates, reads or removes it",
    ),
    (
        "run_install",
        "run_in_threadpool",
        "_strip_builder_env",
        "(env_keys, env_strip_error)",
        "reads the key names of the root .env the builder wrote, then removes that entry "
        "from the workspace, once the staging root is verified again",
        "finishes, beside the finally's removal of the whole session root: both only "
        "remove, the removal passes over an entry already gone, and nothing reads the "
        "strip's answer",
        "the session's stop evidence came first, so no command or write of the session is "
        "left in the workspace; the workspace is this session's own",
    ),
    (
        "run_install",
        "run_in_threadpool",
        "_promote_staging",
        "(published, promote_error)",
        "strips the build's root metadata and validates it, assembles the package in the "
        "session's shell -- the build moved into versions/, origin, state, current, the "
        "form's secret in .env -- and renames the shell into the tools directory under the "
        "tool's name; it holds no tools lock",
        "runs on, and may install the package after the job is gone",
        "publish_running keeps the finally from removing the staging under it; the job "
        "slot keeps the other jobs and the synchronous operations out while the job lasts, "
        "not after; the name is checked free before the assembly and again just before the "
        "rename; delete_tool, which takes only the exclusive lock, can run beside it, and "
        "finds the package only once that rename is done",
    ),
    (
        "run_install",
        "run_in_threadpool",
        "_sweep_logged",
        "-",
        _SWEEP_DOES + "beside a staging kept for a promote that may still be writing it",
        _FINALLY_RUNS_ON,
        _SWEEP_APART,
    ),
    (
        "run_install",
        "run_in_threadpool",
        "_cleanup_staging",
        "-",
        _CLEANUP_DOES,
        _FINALLY_RUNS_ON,
        "the session root is the job's own, and its commands, writes and promote are known "
        "ended; " + _SWEEP_APART,
    ),
    # --- the model's notes, masked again --------------------------------------------
    *[
        (
            "_masked_notes",
            "run_in_threadpool",
            "_masked_and_capped",
            role,
            f"masks and cuts one of the model's texts, reading {_READS_THE_SECRETS}",
            _READ_ON,
            _FILLS_THE_CACHE,
        )
        for role in ("summary", "notes", "notes")
    ],
    # --- the revise -------------------------------------------------------------
    (
        "run_revise",
        "run_in_threadpool",
        "tools.resolve_current",
        "current",
        "reads the package's current pointer",
        _READ_ON,
        _WRITES_NOTHING,
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "_package_identity",
        "package_identity",
        "reads the identity of the version's tool.json",
        _READ_ON,
        _WRITES_NOTHING,
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "tool_drafts.directory_identity",
        "package_dir_identity",
        "reads the package directory's identity",
        _READ_ON,
        _WRITES_NOTHING,
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "_revise_env_gate",
        "(revise_env, env_error)",
        "reads the package's .env and config.env; a refusal masks key names, reading "
        + _READS_THE_SECRETS,
        _READ_ON,
        _FILLS_THE_CACHE,
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "_copy_package_into_staging",
        "copy_error",
        "copies the version into the session's new staging, .staging made when missing "
        "(copytree, links as links); it holds no tools lock",
        "runs on, and may still be writing the staging",
        "copy_running keeps the finally from removing the staging under it, and the staging "
        "is a fresh random name; the job slot keeps the other jobs and the version discard "
        "from changing the version it reads, and delete_tool, which takes only the exclusive "
        "lock, can remove it under the copy",
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "tool_drafts.begin_round",
        "marked",
        "writes the draft's manifest: records a round left unclosed as interrupted, drops "
        "kept names seen gone, marks this job running and moves the generation on",
        "finishes the write; nothing records the round, so the running mark stays for the "
        "next round to record as interrupted",
        "the shared tools lock, under which it matches the package's identity and the "
        "draft's id and generation; the job slot keeps the other jobs and the draft discard "
        "out",
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "tool_drafts.copy_into_workspace",
        "copied",
        "copies the draft's files into the session's new staging, judging and scanning the "
        "copy again, under the shared tools lock",
        "runs on, and may still be writing the staging",
        "copy_running keeps the finally from removing the staging under it; the shared tools "
        "lock, under which it matches the package, the draft and this job's running mark, "
        "guards the draft it reads",
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "tool_drafts.end_unstarted",
        "-",
        "takes this round's running mark back out of the draft's manifest",
        "finishes the write; nothing reads its answer",
        "the shared tools lock, under which it matches the package, the draft and this "
        "job's running mark",
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "tool_drafts.tree_digest",
        "copied_in",
        "reads the whole workspace to take its digest",
        _READ_ON,
        _WRITES_NOTHING,
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "_current_manifest_text",
        "manifest_text",
        "reads the workspace's tool.json",
        _READ_ON,
        _WRITES_NOTHING,
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "_changed_since_base",
        "changed",
        "reads the version's tree to compare it with the workspace's digest",
        _READ_ON,
        _WRITES_NOTHING,
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "_has_config_env",
        "history",
        "looks for a config.env at the top of the workspace",
        _READ_ON,
        _WRITES_NOTHING,
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "_revise_system_prompt",
        "system_prompt",
        f"builds the system prompt, masking the package's name, reading {_READS_THE_SECRETS}",
        _READ_ON,
        _FILLS_THE_CACHE,
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "_revise_user_prompt",
        "user_prompt",
        f"builds the user prompt, masking each text in it, reading {_READS_THE_SECRETS}",
        _READ_ON,
        _FILLS_THE_CACHE,
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "_strip_builder_env",
        "(env_keys, env_strip_error)",
        "reads the key names of the root .env the builder wrote, then removes that entry "
        "from the workspace, once the staging root is verified again",
        "finishes the removal; holding is already up, so the finally keeps the staging and, "
        "no strip having answered, strips again beside it -- both remove the same entry",
        "the session's stop evidence came first, so no command or write of the session is "
        "left in the workspace",
    ),
    *[
        (
            "run_revise",
            "run_in_threadpool",
            "_close_unrecorded",
            "closed",
            "records the round in the draft's manifest when nothing recorded it yet "
            "(tool_drafts.record_round); a first round writes nothing",
            "finishes the write; nothing reads its answer",
            "the shared tools lock, under which it matches the package, the draft, the "
            "generation begin_round wrote and this job's running mark",
        )
        for _ in range(3)
    ],
    *[
        (
            "run_revise",
            "run_in_threadpool",
            "_workspace_confirmed",
            "confirmed",
            "looks at the workspace to confirm it is still the staging it was given",
            _READ_ON,
            _WRITES_NOTHING,
        )
        for _ in range(2)
    ],
    *[
        (
            "run_revise",
            "run_in_threadpool",
            "_save_round_draft",
            "kept",
            "strips the workspace's root .afterthread.meta/, then saves the workspace as the "
            "draft -- a new files directory, the manifest, what becomes of the previous files "
            "directory -- or records the round alone (tool_drafts.save_round, "
            "_close_unrecorded)",
            "runs on: holding stays up, so the finally keeps the staging, and the save may "
            "still finish and commit",
            "the shared tools lock for the copy and the commit, under which it matches the "
            "package and the draft; the job slot keeps the other jobs and the draft discard "
            "out; holding keeps the staging until a save is seen to hold the work",
        )
        for _ in range(4)
    ],
    (
        "run_revise",
        "run_in_threadpool",
        "tools.redact_known_secrets",
        "attempted",
        f"masks the name the model tried, reading {_READS_THE_SECRETS}",
        _READ_ON,
        _FILLS_THE_CACHE,
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "tool_drafts.mark_publishing",
        "marked_generation",
        "writes publishing into the draft's manifest and moves the generation on",
        "finishes the write; the publish does not follow, so the mark stays and the list "
        "reads the draft's outcome as unknown until it is discarded",
        "the shared tools lock, under which it matches the package and the draft's id and "
        "generation; the job slot",
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "_provenance_feedback",
        "origin_feedback",
        f"builds the version's origin feedback, masking it, reading {_READS_THE_SECRETS}",
        _READ_ON,
        _FILLS_THE_CACHE,
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "_publish_revised_version",
        "(published, promote_error, content)",
        "strips the build's root metadata and validates it, moves it to the session's "
        "shell, writes origin.json, moves it into versions/<vid> and publishes current; it "
        "holds no tools lock",
        "runs on, and may publish after the job is gone",
        "publish_running keeps the finally from removing the staging under it; the "
        "package's identity and current are checked again before the first move and again "
        "just before current is published; the job slot keeps the other jobs out while the "
        "job lasts, not after; delete_tool, which takes only the exclusive lock, can run "
        "beside it",
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "tool_drafts.clear_publishing",
        "cleared",
        "clears publishing in the draft's manifest and records the publish's failure on the "
        "latest round",
        "finishes the write; nothing reads its answer",
        "the shared tools lock, under which it matches the package and the generation the "
        "mark wrote",
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "tool_drafts.remove_after_publish",
        "removal",
        "renames the draft away and removes it, once it holds only the manifest and the "
        "copy the save made, unchanged",
        "finishes; the draft may be removed after the job is gone",
        "the shared tools lock, under which it matches the package and the draft; "
        "delete_tool and the sweep need the exclusive lock and stay out meanwhile",
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "_strip_builder_env",
        "(_late_keys, late_error)",
        "the strip before the staging is kept: removes the root .env the builder wrote, "
        "once the staging root is verified again",
        "a second cancellation of the finally leaves it running; the staging is kept either way",
        "it runs only after the session's stop evidence and not while publish_running is "
        "up; a first strip still running removes the same entry",
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "_sweep_logged",
        "-",
        _SWEEP_DOES + "beside a staging that is kept",
        _FINALLY_RUNS_ON,
        _SWEEP_APART,
    ),
    (
        "run_revise",
        "run_in_threadpool",
        "_cleanup_staging",
        "-",
        _CLEANUP_DOES,
        _FINALLY_RUNS_ON,
        "the session root is the job's own, and its release condition holds -- commands and "
        "writes known ended, no copy or publish worker running, nothing held; " + _SWEEP_APART,
    ),
    # --- the publish of a draft as it is ------------------------------------------
    (
        "run_publish_draft",
        "run_in_threadpool",
        "tools.resolve_current",
        "current",
        "reads the package's current pointer",
        _READ_ON,
        _WRITES_NOTHING,
    ),
    (
        "run_publish_draft",
        "run_in_threadpool",
        "_package_identity",
        "package_identity",
        "reads the identity of the version's tool.json",
        _READ_ON,
        _WRITES_NOTHING,
    ),
    (
        "run_publish_draft",
        "run_in_threadpool",
        "tool_drafts.directory_identity",
        "package_dir_identity",
        "reads the package directory's identity",
        _READ_ON,
        _WRITES_NOTHING,
    ),
    (
        "run_publish_draft",
        "run_in_threadpool",
        "tool_drafts.copy_for_publish",
        "copied",
        "copies the draft's files into the session's new staging, judging and scanning the "
        "copy again and taking its digest, under the shared tools lock",
        "runs on, and may still be writing the staging",
        "copy_running keeps the finally from removing the staging under it; the shared tools "
        "lock, under which it matches the package and the draft's id and generation, guards "
        "the draft it reads",
    ),
    (
        "run_publish_draft",
        "run_in_threadpool",
        "_provenance_feedback",
        "origin_feedback",
        f"builds the version's origin feedback, masking it, reading {_READS_THE_SECRETS}",
        _READ_ON,
        _FILLS_THE_CACHE,
    ),
    (
        "run_publish_draft",
        "run_in_threadpool",
        "tool_drafts.mark_publishing",
        "marked",
        "writes publishing into the draft's manifest, says whether that write is confirmed "
        "on disk, and moves the generation on",
        "finishes the write; the publish does not follow, so the mark stays and the list "
        "reads the draft's outcome as unknown until it is discarded",
        "the shared tools lock, under which it matches the package and the draft's id and "
        "generation; the job slot",
    ),
    *[
        (
            "run_publish_draft",
            "run_in_threadpool",
            "tool_drafts.unmark_publishing",
            "unmarked",
            "takes this job's own publishing mark back out of the draft's manifest",
            "finishes the write; nothing reads its answer",
            "the shared tools lock, under which it matches the package, the draft, the "
            "mark's generation and this job",
        )
        for _ in range(2)
    ],
    (
        "run_publish_draft",
        "run_in_threadpool",
        "_publish_revised_version",
        "(published, promote_error, content)",
        "strips the copy's root metadata and validates it, moves it to the session's shell, "
        "writes origin.json, moves it into versions/<vid> and publishes current; it holds "
        "no tools lock",
        "runs on, and may publish after the job is gone",
        "publish_running and holding keep the finally from removing the staging under it; "
        "the package's identity and current are checked again before the first move and "
        "again just before current is published; the job slot keeps the other jobs out "
        "while the job lasts, not after; delete_tool, which takes only the exclusive lock, "
        "can run beside it",
    ),
    (
        "run_publish_draft",
        "run_in_threadpool",
        "tool_drafts.holds_copy",
        "held",
        "reads the draft's files directory under the shared tools lock and compares its "
        "digest with the copy's",
        "finishes its read; its answer is dropped and holding stays up, so the staging is kept",
        _WRITES_NOTHING,
    ),
    (
        "run_publish_draft",
        "run_in_threadpool",
        "tool_drafts.remove_after_publish",
        "removal",
        "renames the draft away and removes it, once it holds only the manifest and the "
        "files directory it was copied from, unchanged",
        "finishes; the draft may be removed after the job is gone",
        "the shared tools lock, under which it matches the package and the draft; "
        "delete_tool and the sweep need the exclusive lock and stay out meanwhile",
    ),
    (
        "run_publish_draft",
        "run_in_threadpool",
        "_sweep_logged",
        "-",
        _SWEEP_DOES + "beside a staging that is kept",
        _FINALLY_RUNS_ON,
        _SWEEP_APART,
    ),
    (
        "run_publish_draft",
        "run_in_threadpool",
        "_cleanup_staging",
        "-",
        _CLEANUP_DOES,
        _FINALLY_RUNS_ON,
        "the session root is the job's own, no copy or publish worker may still be writing "
        "it, and nothing of the work is held in it alone; " + _SWEEP_APART,
    ),
    # --- the examples -------------------------------------------------------------
    (
        "_name_secrets",
        "run_in_threadpool",
        "tools._redaction_secrets",
        "return",
        f"reads {_READS_THE_SECRETS}, to mask the examples' names",
        _READ_ON,
        _FILLS_THE_CACHE,
    ),
    # --- the submits, before a job is admitted --------------------------------------
    *[
        (
            "start_health_check_job",
            "run_in_threadpool",
            worker,
            role,
            does,
            _REQUEST_READ_ON,
            _UNDER_THE_RESERVATION,
        )
        for worker, role, does in (
            ("tools._resolve_package_dir_no_alias", "package_root", "looks the package up"),
            ("tools.resolve_current", "resolution", "reads the package's current pointer"),
            (
                "tools.checkable_tool",
                "found",
                "scans the package as an AI request would be handed it, and builds its tool",
            ),
            ("tools.load_examples", "loaded", "reads the version's examples.json"),
        )
    ],
    (
        "start_revise_job",
        "run_in_threadpool",
        "tools._resolve_package_dir_no_alias",
        "package_root",
        "looks the package up for the env gate, before the slot is reserved",
        "finishes its read; the request's answer is dropped",
        _WRITES_NOTHING,
    ),
    (
        "start_revise_job",
        "run_in_threadpool",
        "_revise_env_gate",
        "(_env, env_error)",
        "reads the package's .env and config.env, before the slot is reserved; a refusal "
        "masks key names, reading " + _READS_THE_SECRETS,
        "finishes its read; the request's answer is dropped",
        _FILLS_THE_CACHE,
    ),
    *[
        (
            "start_revise_job",
            "run_in_threadpool",
            worker,
            role,
            does,
            _REQUEST_READ_ON,
            _UNDER_THE_RESERVATION,
        )
        for worker, role, does in (
            ("tools._resolve_package_dir_no_alias", "package_root", "looks the package up"),
            ("tools.resolve_current", "resolution", "reads the package's current pointer"),
            ("tool_drafts.draft_state", "state", "reads the draft's manifest for its state"),
            (
                "tool_drafts.judge_continuation",
                "judged",
                "reads the draft's manifest to judge the continuation",
            ),
        )
    ],
    *[
        (
            "start_publish_draft_job",
            "run_in_threadpool",
            worker,
            role,
            does,
            _REQUEST_READ_ON,
            _UNDER_THE_RESERVATION,
        )
        for worker, role, does in (
            ("tools._resolve_package_dir_no_alias", "package_root", "looks the package up"),
            ("tools.resolve_current", "resolution", "reads the package's current pointer"),
            ("tool_drafts.judge_draft", "judged", "reads the draft's manifest to judge it"),
        )
    ],
]

# Each awaited call from tool_builder into another service module: (where, the call,
# what lies beyond it). Their workers are theirs: tool_builder neither tracks nor
# waits for them.
_BOUNDARIES: list[tuple[str, str, str]] = [
    *[
        (
            where,
            "generate_structured",
            "the builder session's model loop (llm): the code of llm and of its AI log hands "
            "no work to a thread; it runs the model's tool calls one at a time, awaiting the "
            "handlers the _build_meta_tools rows classify, and its deadline cancels only a "
            "handler's await",
        )
        for where in ("run_install", "run_revise")
    ],
    *[
        (
            where,
            "tool_meta.generate_and_store_summary",
            "the version's summary: tool_meta builds its prompt from the version and writes "
            "summary.json -- a placeholder, then the summary -- on threads of its own "
            "(tools.store_summary_meta, an atomic replace checked against the version's "
            "identity just before it), holding no tools lock; a cancellation leaves a write "
            "running",
        )
        for where in ("run_install", "run_revise")
    ],
    *[
        (
            where,
            "tools.run_examples",
            "the examples: tools takes the shared tools lock for the run, reads them "
            "(load_examples), runs each through the runtime tool's handler -- its "
            "subprocess, shielded, and its run record -- writes the check's verdict and "
            "gives the lock back, each on a thread of its own; a cancellation gives the lock "
            "back while an example already running runs on",
        )
        for where in ("_verify_published", "_run_check_job")
    ],
]


def test_each_hand_off_to_a_thread_in_tool_builder_in_a_known_form_is_classified() -> None:
    found = [hand_off.key for hand_off in _hand_offs(_builder_source())]
    registered = [row[:4] for row in _CLASSIFIED]
    assert found, "the scan found no hand-off at all: it is not reading tool_builder"
    assert all(text.strip() for row in _CLASSIFIED for text in row[4:])
    unclassified, gone = _unmatched(found, registered)
    assert not unclassified, f"hand-offs with no row: {unclassified}"
    assert not gone, f"rows no hand-off matches: {gone}"


def test_each_awaited_call_from_tool_builder_into_another_service_module_is_registered() -> None:
    found = _boundaries(_builder_source())
    registered = [(where, call) for where, call, _beyond in _BOUNDARIES]
    assert found, "the scan found no call into another service module"
    assert all(beyond.strip() for _where, _call, beyond in _BOUNDARIES)
    unregistered, gone = _unmatched(found, registered)
    assert not unregistered, f"calls into another service module with no row: {unregistered}"
    assert not gone, f"boundary rows no call matches: {gone}"


def test_a_meta_tool_hands_work_only_to_its_session_or_to_a_named_reader() -> None:
    source = _builder_source()
    handed = _meta_tool_hand_offs(source)
    assert handed, "the scan found no meta-tool hand-off: it is not reading _build_meta_tools"
    assert _third_ways(source) == []
    named = {(hand_off.where, hand_off.worker) for hand_off in handed}
    assert named >= _META_TOOL_READ_ONLY, "a named reader no longer hands anything over"


@pytest.mark.parametrize(
    ("found", "registered", "unclassified", "gone"),
    [
        ([("a",), ("b",)], [("a",)], [("b",)], []),
        ([("a",)], [("a",), ("c",)], [], [("c",)]),
        ([("a",), ("a",)], [("a",)], [("a",)], []),
        ([("a",)], [("a",), ("a",)], [], [("a",)]),
    ],
    ids=["a-hand-off-with-no-row", "a-row-with-no-hand-off", "one-more-found", "one-more-row"],
)
def test_the_register_check_names_a_hand_off_with_no_row_and_a_row_with_no_hand_off(
    found: list[tuple[str]],
    registered: list[tuple[str]],
    unclassified: list[tuple[str]],
    gone: list[tuple[str]],
) -> None:
    assert _unmatched(found, registered) == (unclassified, gone)


# A module shaped like tool_builder's meta-tools: a session class whose ``write``
# hands a write to a thread through a closure, and four handlers, each handing its
# work over the way the real ones do.
_META_TOOLS = """
class _BuilderShells:
    async def write(self, perform):
        def worker():
            return perform()

        task = asyncio.ensure_future(run_in_threadpool(worker))
        return await asyncio.shield(task)


def _build_meta_tools(staging, *, shells):
    async def write_file(args):
        def _write():
            return "wrote"

        return await shells.write(_write)

    async def read_file(args):
        def _read():
            return "read"

        return await run_in_threadpool(_read)

    async def list_dir(args):
        def _list():
            return "listed"

        return await run_in_threadpool(_list)

    async def run_shell(args):
        return await shells.run(args)

    return [
        LlmTool(handler=write_file),
        LlmTool(handler=read_file),
        LlmTool(handler=list_dir),
        LlmTool(handler=run_shell),
    ]
"""
_SESSION_WRITE = "        return await shells.write(_write)\n"
_NAMED_READ = "        return await run_in_threadpool(_read)\n"
_TOOL_LIST = "        LlmTool(handler=run_shell),\n"
_WRITE_FILE = "_build_meta_tools.write_file"


def _with(*changes: tuple[str, str], helpers: str = "") -> str:
    """``_META_TOOLS`` with each (old, new) change made once, and ``helpers`` added as
    module functions."""
    source = _META_TOOLS
    for old, new in changes:
        assert source.count(old) == 1, old
        source = source.replace(old, new)
    return source + helpers


# Each way a meta-tool can hand work to a thread that is neither the session nor a
# named reader, and one hand-off the check must name for it: (source, (where, how,
# worker)).
_THIRD_WAYS: dict[str, tuple[str, tuple[str, str, str]]] = {
    "the-pool-directly": (
        _with((_SESSION_WRITE, "        return await run_in_threadpool(_write)\n")),
        (_WRITE_FILE, "run_in_threadpool", "_write"),
    ),
    "asyncio-to-thread": (
        _with((_SESSION_WRITE, "        return await asyncio.to_thread(_write)\n")),
        (_WRITE_FILE, "to_thread", "_write"),
    ),
    "a-thread-of-its-own": (
        _with((_SESSION_WRITE, "        threading.Thread(target=_write).start()\n")),
        (_WRITE_FILE, "Thread", "_write"),
    ),
    "a-helper-handing-its-parameter-over": (
        _with(
            (_SESSION_WRITE, "        return await _off_loop(_write)\n"),
            helpers=(
                "\n\nasync def _off_loop(perform):\n    return await run_in_threadpool(perform)\n"
            ),
        ),
        (_WRITE_FILE, "_off_loop", "_write"),
    ),
    "a-helper-with-a-worker-of-its-own": (
        _with(
            (_SESSION_WRITE, "        return await _store(args)\n"),
            helpers=(
                "\n\nasync def _store(args):\n"
                "    return await run_in_threadpool(_write_to_disk, args)\n"
            ),
        ),
        (_WRITE_FILE, "_store", "_write_to_disk"),
    ),
    "a-new-handler": (
        _with(
            (
                "    async def run_shell(args):\n",
                "    async def delete_file(args):\n"
                "        return await run_in_threadpool(_delete, args)\n\n"
                "    async def run_shell(args):\n",
            ),
            (_TOOL_LIST, _TOOL_LIST + "        LlmTool(handler=delete_file),\n"),
        ),
        ("_build_meta_tools.delete_file", "run_in_threadpool", "_delete"),
    ),
    "a-named-reader-handing-over-a-second-worker": (
        _with((_NAMED_READ, "        await run_in_threadpool(_touch)\n" + _NAMED_READ)),
        ("_build_meta_tools.read_file", "run_in_threadpool", "_touch"),
    ),
}


@pytest.mark.parametrize("case", list(_THIRD_WAYS))
def test_the_meta_tool_check_names_a_third_way_of_handing_work_to_a_thread(case: str) -> None:
    source, expected = _THIRD_WAYS[case]
    named = {(hand_off.where, hand_off.how, hand_off.worker) for hand_off in _third_ways(source)}
    assert expected in named


def test_the_meta_tool_check_passes_the_session_and_the_named_readers() -> None:
    assert _third_ways(_META_TOOLS) == []
    assert {hand_off.key for hand_off in _meta_tool_hand_offs(_META_TOOLS)} == {
        (_WRITE_FILE, "_BuilderShells.write", "_write", "return"),
        ("_build_meta_tools.read_file", "run_in_threadpool", "_read", "return"),
        ("_build_meta_tools.list_dir", "run_in_threadpool", "_list", "return"),
    }


# Each form the scanner must find, as a module: (source, every hand-off it holds).
_FOUND: dict[str, tuple[str, list[tuple[str, str, str, str]]]] = {
    "the-pool": (
        "async def where():\n    await run_in_threadpool(f, a)\n",
        [("where", "run_in_threadpool", "f", "-")],
    ),
    "functools-partial": (
        "async def where():\n    x = await run_in_threadpool(functools.partial(f, a, k=1))\n",
        [("where", "run_in_threadpool", "f", "x")],
    ),
    "partial-in-partial": (
        "async def where():\n    x = await run_in_threadpool(partial(partial(f, a), b))\n",
        [("where", "run_in_threadpool", "f", "x")],
    ),
    "the-pool-by-attribute": (
        "async def where():\n    return await concurrency.run_in_threadpool(f)\n",
        [("where", "run_in_threadpool", "f", "return")],
    ),
    "asyncio-to-thread": (
        "async def where():\n    x = await asyncio.to_thread(f, a)\n",
        [("where", "to_thread", "f", "x")],
    ),
    "run-in-executor": (
        "async def where():\n    x = await loop.run_in_executor(None, f, a)\n",
        [("where", "run_in_executor", "f", "x")],
    ),
    "executor-submit": (
        "def where():\n    future = pool.submit(f, a)\n",
        [("where", "submit", "f", "future")],
    ),
    "thread-target": (
        "def where():\n    thread = threading.Thread(target=f, daemon=True)\n",
        [("where", "Thread", "f", "thread")],
    ),
    "thread-target-by-position": (
        "def where():\n    thread = Thread(None, f)\n",
        [("where", "Thread", "f", "thread")],
    ),
    "anyio-run-sync": (
        "async def where():\n    x = await anyio.to_thread.run_sync(f)\n",
        [("where", "run_sync", "f", "x")],
    ),
    "the-construction-queue": (
        "def where():\n    _SETUP_QUEUE.put((f, fut))\n",
        [("where", "_SETUP_QUEUE.put", "f", "-")],
    ),
    "inside-a-task": (
        "async def where():\n    task = asyncio.ensure_future(run_in_threadpool(worker))\n",
        [("where", "run_in_threadpool", "worker", "task")],
    ),
    "inside-an-expression": (
        "async def where():\n    x = {'k': await run_in_threadpool(f) if c else None}\n",
        [("where", "run_in_threadpool", "f", "x")],
    ),
    "in-a-nested-function": (
        "async def outer():\n    async def inner():\n        await run_in_threadpool(f)\n",
        [("outer.inner", "run_in_threadpool", "f", "-")],
    ),
    "in-a-method": (
        "class C:\n    async def m(self):\n        await run_in_threadpool(f)\n",
        [("C.m", "run_in_threadpool", "f", "-")],
    ),
    "a-lambda": (
        "async def where():\n    await run_in_threadpool(lambda: f(a))\n",
        [("where", "run_in_threadpool", "lambda: f(a)", "-")],
    ),
    "a-helper-handing-its-parameter-over": (
        "async def helper(g):\n    return await run_in_threadpool(g)\n\n\n"
        "async def caller():\n    x = await helper(f)\n",
        [("helper", "run_in_threadpool", "g", "return"), ("caller", "helper", "f", "x")],
    ),
    "a-helper-handing-its-parameter-over-through-a-closure": (
        "async def helper(g):\n    def worker():\n        return g()\n\n"
        "    return await run_in_threadpool(worker)\n\n\n"
        "async def caller():\n    x = await helper(f)\n",
        [("helper", "run_in_threadpool", "worker", "return"), ("caller", "helper", "f", "x")],
    ),
    "a-method-handing-its-parameter-over-through-a-closure": (
        "class S:\n    async def write(self, perform):\n        def worker():\n"
        "            return perform()\n\n"
        "        task = asyncio.ensure_future(run_in_threadpool(worker))\n"
        "        return await asyncio.shield(task)\n\n\n"
        "async def caller(s):\n    return await s.write(f)\n",
        [("S.write", "run_in_threadpool", "worker", "task"), ("caller", "S.write", "f", "return")],
    ),
    "a-helper-handed-its-parameter-by-keyword": (
        "async def helper(g):\n    return await run_in_threadpool(g)\n\n\n"
        "async def caller():\n    await helper(g=f)\n",
        [("helper", "run_in_threadpool", "g", "return"), ("caller", "helper", "f", "-")],
    ),
    "a-helper-in-a-with-item": (
        "async def helper(g):\n    return await run_in_threadpool(g)\n\n\n"
        "async def caller():\n    async with await helper(f) as client:\n        pass\n",
        [("helper", "run_in_threadpool", "g", "return"), ("caller", "helper", "f", "client")],
    ),
    # A helper that hands over a worker of its own: the hand-off is the helper's,
    # and its caller hands nothing over.
    "a-helper-with-a-worker-of-its-own": (
        "async def helper(x):\n    return await run_in_threadpool(work, x)\n\n\n"
        "async def caller():\n    await helper(f)\n",
        [("helper", "run_in_threadpool", "work", "return")],
    ),
}


@pytest.mark.parametrize("form", list(_FOUND))
def test_the_hand_off_scanner_finds_each_listed_form(form: str) -> None:
    source, expected = _FOUND[form]
    assert sorted(hand_off.key for hand_off in _hand_offs(source)) == sorted(expected)


# Calls that hand nothing to another thread: none is found.
_NOT_FOUND: dict[str, str] = {
    "another-name": "async def where():\n    await run_in_threadpool_later(f)\n",
    "a-task-on-the-loop": "async def where():\n    asyncio.create_task(coro())\n",
    "a-wait-on-a-future": "async def where():\n    await asyncio.wrap_future(fut)\n",
    "back-to-the-loop": "def where():\n    loop.call_soon_threadsafe(callback, value)\n",
    "another-queue": "def where():\n    other_queue.put((f, fut))\n",
    "a-helper-calling-its-parameter-on-the-spot": (
        "def apply(g):\n    return g()\n\n\ndef caller():\n    apply(f)\n"
    ),
}


@pytest.mark.parametrize("form", list(_NOT_FOUND))
def test_the_hand_off_scanner_passes_over_what_hands_off_nothing(form: str) -> None:
    assert _hand_offs(_NOT_FOUND[form]) == []


# --- the tools lock, handed to a thread by name, anywhere in the backend ----------

# The tools-lock functions no coroutine hands to a thread by name. A coroutine takes
# the shared lock through ``tools.acquire_shared_tools_lock_async``, which hands back
# what its worker takes however the wait ends, and releases it by calling
# ``tools.release_tools_lock`` itself, which no cancellation can skip.
_LOCK_FUNCTIONS = frozenset({"acquire_shared_tools_lock", "release_tools_lock"})


def _named(worker: str) -> str | None:
    """The name a hand-off's worker is written as: a plain name, or the last part of an
    attribute; None for anything else (a lambda, a call)."""
    expression = ast.parse(worker, mode="eval").body
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return expression.attr
    return None


def _lock_functions_handed_over_by_name(source: str) -> list[HandOff]:
    return [
        hand_off for hand_off in _hand_offs(source) if _named(hand_off.worker) in _LOCK_FUNCTIONS
    ]


def test_no_backend_module_hands_a_tools_lock_function_to_a_thread_by_name() -> None:
    """Checks: every module under ``afterthread/``, each hand-off the scanner finds whose
    worker is a tools-lock function by name or as an attribute, ``partial`` taken off.
    Not covered: the function reached through an alias, a lambda, or a nested def that
    calls it -- the way ``acquire_shared_tools_lock_async`` hands over its own worker."""
    package = Path(tool_builder.__file__).parents[1]
    sources = sorted(package.rglob("*.py"))
    assert sources
    found = [
        (str(path.relative_to(package.parent)), *hand_off.key)
        for path in sources
        for hand_off in _lock_functions_handed_over_by_name(path.read_text(encoding="utf-8"))
    ]
    assert found == []


# Each spelling of a lock function handed over, found once by the step that reads the
# worker's name.
_LOCK_HANDED_OVER: dict[str, str] = {
    "acquire-by-name": (
        "async def f():\n    x = await run_in_threadpool(acquire_shared_tools_lock)\n"
    ),
    "acquire-as-an-attribute": (
        "async def f():\n    x = await run_in_threadpool(tools.acquire_shared_tools_lock)\n"
    ),
    "acquire-in-a-partial": (
        "async def f():\n    x = await run_in_threadpool(partial(acquire_shared_tools_lock))\n"
    ),
    "release-by-name": "async def f():\n    await run_in_threadpool(release_tools_lock, fd)\n",
    "release-as-an-attribute": (
        "async def f():\n    await run_in_threadpool(tools.release_tools_lock, fd)\n"
    ),
    "release-in-a-partial": (
        "async def f():\n    await run_in_threadpool(partial(release_tools_lock, fd))\n"
    ),
}


@pytest.mark.parametrize("form", list(_LOCK_HANDED_OVER))
def test_the_lock_check_reads_each_spelling_of_a_lock_function_handed_over(form: str) -> None:
    assert len(_lock_functions_handed_over_by_name(_LOCK_HANDED_OVER[form])) == 1
