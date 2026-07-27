"""Tests for the web installer (afterthread.services.tool_builder) and its
router (afterthread.routers.tools).

Layers:

* META-TOOLS -- built against a real tmp staging dir and driven directly:
  containment (traversal / absolute / symlink-escape rejection for
  write/read/list), run_shell's timeout kill, output cap and scrubbed env,
  and the InstallResult sanitizer;
* RUN_INSTALL -- ``generate_structured`` is monkeypatched with fakes that
  exercise the REAL meta-tools they are handed (writing a genuinely valid or
  broken package into staging), so the promote/validate/cleanup pipeline runs
  for real: happy path, ready=false, staging-validation failures, name-taken,
  feature-off, fetch failures (a real local HTTP server), oversized body;
* JOBS -- the queued -> running -> succeeded/failed state machine, driven with
  a controllable fake run_install on a real event loop;
* ROUTER -- the five endpoints' 200/202/204/404/422/503 contracts.

Async entry points are driven with ``asyncio.run`` (no pytest-asyncio plugin,
matching the suite). Jobs and the llm_log ring are process-wide singletons, so
an autouse fixture resets both around every test.
"""

import asyncio
import hashlib
import json
import os
import shutil
import socket
import sys
import threading
import time
from collections.abc import Callable, Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from afterthread.config import Settings
from afterthread.services import llm_log, tool_builder, tool_meta, tools
from afterthread.services.llm import LLMNotConfiguredError, LLMUpstreamError
from afterthread.services.tool_builder import (
    _ERROR_NAME_TAKEN,
    _ERROR_OPENAPI_TOO_LARGE,
    _ERROR_SIDECAR_STRIP,
    _ERROR_STAGING_TAMPERED,
    _ERROR_TOOLS_DISABLED,
    InstallOutcome,
    InstallResult,
    _build_meta_tools,
    _resolve_in_staging,
    run_install,
)


@pytest.fixture(autouse=True)
def _reset_singletons() -> Generator[None]:
    """Empty the job table, the llm_log ring, and the process-wide known-secret
    registries around every test (all module-level singletons the installer
    touches), then DRAIN the single-flight client-construction worker.

    The drain is what makes the timing/threading fetch tests deterministic under
    any collection order: a prior test's still-running (or still-queued) fake
    construction occupies the ONE construction worker, and left there it would
    queue behind and skew the NEXT timing test (or make the repeated-timeout
    thread-count assertion race). Draining in teardown guarantees the next test
    starts with an idle worker, whatever ran before it. Tests that block the
    constructor on purpose release it in their own finally, so this drain never
    hangs (its own timeout would fail loudly if one forgot)."""
    tool_builder._reset_jobs_for_tests()
    llm_log._reset_for_tests()
    tools._INFLIGHT_SECRETS.clear()
    tools._ENV_VALUE_CACHE.clear()
    yield
    tool_builder._reset_jobs_for_tests()
    llm_log._reset_for_tests()
    tools._INFLIGHT_SECRETS.clear()
    tools._ENV_VALUE_CACHE.clear()
    tool_builder._drain_setup_worker_for_tests()


def _install_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Settings:
    """Point EVERY settings reader on the install path (tools registry,
    installer, summary generator) at one value, so their view of the tools dir
    and the prompt budget can never disagree mid-install."""
    settings = Settings(**overrides)
    for target in (
        "afterthread.services.tools.get_settings",
        "afterthread.services.tool_builder.get_settings",
        "afterthread.services.tool_meta.get_settings",
    ):
        monkeypatch.setattr(target, lambda settings=settings: settings)
    return settings


def _meta_by_name(staging: Path) -> dict[str, Any]:
    return {t.spec["function"]["name"]: t for t in _build_meta_tools(staging)}


def _call(handler: Any, args: dict[str, Any]) -> str:
    return asyncio.run(handler(args))


# --- meta-tools: containment -------------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    ["../escape.txt", "a/../../escape.txt", "/etc/passwd", "  ", ""],
    ids=["dotdot", "nested-dotdot", "absolute", "blank", "empty"],
)
def test_meta_paths_rejected_for_write_read_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_path: str
) -> None:
    """Every path-shaped argument goes through the same containment gate:
    traversal, absolute and blank paths are rejected by all three file tools,
    and nothing is created outside (or inside) the staging dir."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    staging = tmp_path / "staging"
    staging.mkdir()
    meta = _meta_by_name(staging)

    assert "rejected" in _call(meta["write_file"].handler, {"path": bad_path, "content": "x"})
    assert "rejected" in _call(meta["read_file"].handler, {"path": bad_path})
    if bad_path.strip():  # list_dir treats blank as "the root", which is legal
        assert "rejected" in _call(meta["list_dir"].handler, {"path": bad_path})
    assert not (tmp_path / "escape.txt").exists()
    assert list(staging.iterdir()) == []


def test_meta_write_rejects_symlink_escape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A symlink inside staging pointing outside is followed by resolve() and
    then fails containment -- writing THROUGH it never touches the target."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    staging = tmp_path / "staging"
    staging.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (staging / "link").symlink_to(outside, target_is_directory=True)
    meta = _meta_by_name(staging)

    result = _call(meta["write_file"].handler, {"path": "link/owned.txt", "content": "x"})
    assert "rejected" in result
    assert not (outside / "owned.txt").exists()
    # Reading back through the link is blocked the same way.
    (outside / "secret.txt").write_text("secret")
    assert "rejected" in _call(meta["read_file"].handler, {"path": "link/secret.txt"})


def test_meta_write_read_list_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    staging = tmp_path / "staging"
    staging.mkdir()
    meta = _meta_by_name(staging)

    wrote = _call(meta["write_file"].handler, {"path": "pkg/run.py", "content": "print('hi')\n"})
    assert "wrote" in wrote and "pkg/run.py" in wrote
    assert (staging / "pkg" / "run.py").read_text() == "print('hi')\n"

    assert _call(meta["read_file"].handler, {"path": "pkg/run.py"}) == "print('hi')\n"
    assert "no such file" in _call(meta["read_file"].handler, {"path": "missing.py"})

    listing = _call(meta["list_dir"].handler, {"path": ""})
    assert "pkg/" in listing.splitlines()
    assert "pkg/run.py" in listing.splitlines()


def test_meta_write_rejects_oversized_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Over-cap content is REJECTED, never truncated -- a silently cut source
    file would be corrupt."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    staging = tmp_path / "staging"
    staging.mkdir()
    meta = _meta_by_name(staging)

    result = _call(
        meta["write_file"].handler,
        {"path": "big.txt", "content": "a" * (tool_builder._WRITE_CONTENT_MAX_CHARS + 1)},
    )
    assert result.startswith("write_file rejected")
    assert not (staging / "big.txt").exists()


def test_meta_read_refuses_oversized_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """read_file stat()s the size and REFUSES a file over the output cap rather
    than reading the whole thing into memory to then truncate it (N5): reading
    cap-bytes of a 10GB file would pull the whole file in first. The builder gets
    an actionable error; nothing oversized is read."""
    _install_settings(
        monkeypatch, tools_dir=str(tmp_path / "tools"), llm_tool_output_max_chars=1000
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "big.txt").write_text("a" * 5000)  # 5000 bytes > the 1000-char cap
    meta = _meta_by_name(staging)

    result = _call(meta["read_file"].handler, {"path": "big.txt"})
    assert result.startswith("read_file failed")
    assert "too large" in result


def test_meta_read_reads_file_at_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A file WITHIN the cap is still read back in full -- the N5 refusal is only
    for files over the cap, never a regression for ordinary reads."""
    _install_settings(
        monkeypatch, tools_dir=str(tmp_path / "tools"), llm_tool_output_max_chars=1000
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "ok.txt").write_text("a" * 1000)  # exactly at the cap
    meta = _meta_by_name(staging)

    assert _call(meta["read_file"].handler, {"path": "ok.txt"}) == "a" * 1000


def test_meta_read_refuses_fifo_without_hanging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A FIFO in staging is refused as a non-regular file (F2), never opened.
    read_text() on a FIFO blocks FOREVER waiting for a writer, and the outer
    asyncio timeout only cancels the await -- the threadpool worker would stay
    wedged (a permanent leak). is_file() (S_ISREG) rejects it up front, so the
    call returns at once. The whole thing is bounded by asyncio.wait_for so a
    regression (an actual hang) fails LOUDLY here instead of stalling the suite."""
    _install_settings(
        monkeypatch, tools_dir=str(tmp_path / "tools"), llm_tool_output_max_chars=1000
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    os.mkfifo(staging / "pipe")  # neither a dir nor over-size; read_text would block
    meta = _meta_by_name(staging)

    async def _run() -> str:
        return await asyncio.wait_for(meta["read_file"].handler({"path": "pipe"}), timeout=10)

    result = asyncio.run(_run())
    assert result == "read_file failed: not a regular file"


def test_meta_write_refuses_fifo_without_hanging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A reader-less FIFO in staging makes write_file return its failure PROMPTLY,
    never hanging (F3c). write_text() opens O_WRONLY, and a reader-less FIFO BLOCKS
    that open FOREVER waiting for a reader -- wedging the threadpool worker, since
    the outer asyncio timeout only cancels the await. tools._write_regular_file
    opens O_NONBLOCK, so the reader-less FIFO fails with ENXIO at once. Bounded by
    asyncio.wait_for so a regression (an actual hang) fails LOUDLY here instead of
    stalling the suite."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    staging = tmp_path / "staging"
    staging.mkdir()
    os.mkfifo(staging / "pipe")  # write_text() on this would block forever
    meta = _meta_by_name(staging)

    async def _run() -> str:
        return await asyncio.wait_for(
            meta["write_file"].handler({"path": "pipe", "content": "x"}), timeout=10
        )

    result = asyncio.run(_run())
    assert result == "write_file failed: target is not a regular file"
    assert (staging / "pipe").is_fifo()  # the FIFO was refused, never overwritten


def test_meta_write_refuses_symlink_leaf_to_outside(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A symlink LEAF in staging pointing at an external file is refused and the
    target is left byte-for-byte untouched (jail hardened, F3c): _resolve_in_staging
    follows the leaf with resolve() and fails containment, and
    tools._write_regular_file's O_NOFOLLOW is the write-boundary backstop should a
    link be raced in after the resolve. Either way a generated symlink can never
    redirect a write OUT of staging."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    staging = tmp_path / "staging"
    staging.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    (staging / "leaf").symlink_to(outside)
    meta = _meta_by_name(staging)

    result = _call(meta["write_file"].handler, {"path": "leaf", "content": "clobber"})
    assert "rejected" in result  # _resolve_in_staging refuses the escaping leaf
    assert outside.read_text() == "keep"  # external target never written through


def test_list_dir_caps_entries_during_walk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """list_dir enforces the entry cap DURING the walk (F3): a tree with far more
    than the cap returns promptly with at most cap entries plus a truncation
    notice, instead of materializing and sorting the ENTIRE subtree first. The
    kept entries are the lexicographically smallest, in a deterministic order."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    staging = tmp_path / "staging"
    staging.mkdir()
    cap = tool_builder._LIST_DIR_MAX_ENTRIES
    for i in range(cap + 250):  # far more than the cap
        (staging / f"f{i:05d}.txt").write_text("x")
    meta = _meta_by_name(staging)

    started = time.monotonic()
    listing = _call(meta["list_dir"].handler, {"path": ""})
    elapsed = time.monotonic() - started
    lines = listing.splitlines()

    assert len(lines) == cap + 1  # exactly cap entries + one truncation notice
    assert "truncated" in lines[-1]
    assert elapsed < 5  # a bounded walk, not a full-tree enumerate + sort
    # Deterministic order: the kept entries are the lexicographically smallest.
    kept = lines[:cap]
    assert kept == sorted(kept)
    assert kept[0] == "f00000.txt"


# --- meta-tools: run_shell -----------------------------------------------------


def test_run_shell_happy_and_nonzero_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    staging = tmp_path / "staging"
    staging.mkdir()
    meta = _meta_by_name(staging)

    assert _call(meta["run_shell"].handler, {"command": "echo hello"}) == "hello\n"
    failed = _call(meta["run_shell"].handler, {"command": "echo bad >&2; exit 3"})
    assert failed.startswith("command failed (exit 3): bad")


def test_run_shell_cwd_is_staging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    staging = tmp_path / "staging"
    staging.mkdir()
    meta = _meta_by_name(staging)

    _call(meta["run_shell"].handler, {"command": "echo data > made-here.txt"})
    assert (staging / "made-here.txt").exists()


def test_run_shell_timeout_kills_group(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_settings(
        monkeypatch, tools_dir=str(tmp_path / "tools"), tool_install_shell_timeout_seconds=0.3
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    meta = _meta_by_name(staging)

    started = time.monotonic()
    result = _call(meta["run_shell"].handler, {"command": "sleep 30"})
    assert "timed out" in result
    assert time.monotonic() - started < 5


def test_run_shell_output_capped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_settings(
        monkeypatch, tools_dir=str(tmp_path / "tools"), llm_tool_output_max_chars=1000
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    meta = _meta_by_name(staging)

    result = _call(meta["run_shell"].handler, {"command": "printf 'a%.0s' {1..5000}"})
    assert len(result) == 1000
    assert result.endswith("…[工具輸出過長已截斷]")


def test_run_shell_unbounded_output_killed_promptly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A command whose output NEVER ends (`yes` streams forever) is killed at the
    cap (N5): the bounded reader hits cap+1 after ~one chunk and SIGKILLs the
    process group, so the result is bounded and marked instead of the service
    buffering an unbounded stream to exhaustion."""
    _install_settings(
        monkeypatch, tools_dir=str(tmp_path / "tools"), llm_tool_output_max_chars=1000
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    meta = _meta_by_name(staging)

    started = time.monotonic()
    result = _call(meta["run_shell"].handler, {"command": "yes flood"})
    elapsed = time.monotonic() - started

    marker = "…[工具輸出過長已截斷]"
    assert len(result) <= 1000 + len(marker)  # bounded, never the whole stream
    assert result.endswith(marker)
    assert elapsed < 5  # killed at the cap, not read forever


def test_run_shell_env_scrubbed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The builder shell must never see our LLM credentials: only the
    passthrough allowlist survives from the parent environment."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-do-not-leak")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://secret.internal/v1")
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    staging = tmp_path / "staging"
    staging.mkdir()
    meta = _meta_by_name(staging)

    result = _call(
        meta["run_shell"].handler,
        {"command": 'echo "K=${OPENAI_API_KEY:-none} U=${OPENAI_BASE_URL:-none} P=${PATH:+set}"'},
    )
    assert "K=none" in result
    assert "U=none" in result
    assert "P=set" in result  # the allowlisted PATH did pass through
    assert "sk-secret-do-not-leak" not in result


def test_run_shell_passes_through_tls_no_verify_when_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TLS_NO_VERIFY=1 reaches run_shell's env when the settings flag is on, so a
    builder-tested `curl`/`python3` invocation can honor it too."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"), tls_no_verify=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    meta = _meta_by_name(staging)

    result = _call(meta["run_shell"].handler, {"command": 'echo "T=${TLS_NO_VERIFY:-unset}"'})
    assert "T=1" in result


def test_run_shell_omits_tls_no_verify_when_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default (flag off): TLS_NO_VERIFY is absent from run_shell's env entirely
    -- not "0", simply not set."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    staging = tmp_path / "staging"
    staging.mkdir()
    meta = _meta_by_name(staging)

    result = _call(meta["run_shell"].handler, {"command": 'echo "T=${TLS_NO_VERIFY:-unset}"'})
    assert "T=unset" in result


# --- InstallResult sanitizer ---------------------------------------------------


def test_install_result_ready_requires_valid_name() -> None:
    with pytest.raises(ValidationError):
        InstallResult.model_validate({"tool_name": "Bad Name!", "summary": "s", "ready": True})
    with pytest.raises(ValidationError):
        InstallResult.model_validate({"tool_name": "", "summary": "s", "ready": True})


def test_install_result_not_ready_tolerates_missing_name() -> None:
    result = InstallResult.model_validate({"summary": "could not authenticate", "ready": False})
    assert result.tool_name == ""
    assert result.ready is False


def test_install_result_ready_is_strictly_coerced() -> None:
    # Ambiguous truthiness must never authorize installing executable code.
    assert InstallResult.model_validate({"ready": "yes", "tool_name": "t"}).ready is False
    assert InstallResult.model_validate({"ready": 2, "tool_name": "t"}).ready is False
    assert InstallResult.model_validate({"ready": "true", "tool_name": "t"}).ready is True


def test_install_result_redacts_secret_straddling_summary_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1/H2: the summary is redacted BEFORE the 2000-char slice, so a secret straddling
    the slice edge is masked while the summary is whole -- no unmatchable prefix fragment
    survives the cut. A slice-FIRST sanitizer would keep the part of the secret sitting
    inside the cap (this test's discriminator)."""
    secret = "kb-live-secret-abcdef123456"
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))
    cap = tool_builder._SUMMARY_CAP
    # Secret spans [cap-10, cap-10+len), so a slice-first sanitizer keeps secret[:10].
    summary = "x" * (cap - 10) + secret + "y" * 100
    result = InstallResult.model_validate(
        {"tool_name": "kbsearch", "summary": summary, "ready": True}
    )

    assert secret not in result.summary
    assert secret[:10] not in result.summary  # the fragment a slice-first cut would keep
    assert "•••" in result.summary  # a mask occurred before the cut (marker start survives)
    assert len(result.summary) <= cap


def test_install_result_redacts_before_stripping(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same order one step earlier: the redactor matches the REGISTERED
    value, so a secret carrying edge whitespace (a hand-edited .env with a
    quoted " secret-token ") stops matching the moment strip eats that edge --
    and a strip-FIRST sanitizer would then pass the body through unmasked (this
    test's discriminator)."""
    secret = " kb-live-secret-abcdef "
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))
    result = InstallResult.model_validate(
        {"tool_name": "kbsearch", "summary": secret, "ready": True}
    )
    assert "kb-live-secret-abcdef" not in result.summary
    assert tools._REDACTION_MARKER in result.summary


def test_builder_user_prompt_redacts_openapi_before_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1/D36: the OpenAPI doc is redacted BEFORE the budget cut, so a known secret
    straddling the truncation edge is masked while the doc is whole -- a truncate-first
    order would strand ``prefix + truncation marker``, an INTERIOR fragment neither
    redaction pass could catch. An internal API doc can legitimately embed the very key
    the operator just registered."""
    secret = "kb-live-secret-abcdef123456"  # 27 chars
    # At the cold-start ratio 1.0 the char allowance equals the token budget, so a
    # 4000-token budget gives the same 4000-char cut this test relies on.
    budget = 4000  # the settings floor for llm_prompt_budget_tokens
    _install_settings(monkeypatch, llm_prompt_budget_tokens=budget)
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))
    # Place the secret so it straddles the effective cut (budget - truncation marker): its
    # first 8 chars sit inside the cap and the rest past it, so a truncate-first order would
    # strand secret[:8] ahead of the truncation marker. Redact-first masks it whole (the
    # 13-char marker is shorter than the 27-char secret, so the result fits with no cut).
    openapi = "y" * (budget - 18) + secret
    prompt = tool_builder._builder_user_prompt("do it", openapi)

    assert secret not in prompt
    assert secret[:6] not in prompt  # the 6-char fragment a truncate-first cut would strand
    assert tools._REDACTION_MARKER in prompt


# --- run_install ---------------------------------------------------------------


_GOOD_RUN_PY = "import sys, json\nargs = json.load(sys.stdin)\nprint('ok', args)\n"


def _package_manifest(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": "test-built tool",
        "parameters": {"type": "object", "properties": {}},
        "entry": ["python3", "run.py"],
    }


def _fake_summary_generate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    summary: str = "這個工具會查 KB",
    explode: BaseException | None = None,
    side_effect: Callable[[], None] | None = None,
) -> None:
    """Stub the SUMMARY session's generate_structured (D40).

    A SECOND patch is genuinely required: ``tool_meta`` imported the name into
    its OWN module namespace, so patching tool_builder's reference leaves the
    summary session calling the real thing. Every install that reaches promote
    now runs one, so ``_fake_generate`` installs this by default -- without it
    those tests would fall through to the real LLM path and depend on ambient
    configuration for their (swallowed) failure.

    ``side_effect`` runs INSIDE the stubbed call, which is the only place a test
    can act "while the generation is in flight": the real thing awaits an LLM for
    seconds, and the store-time races (above all a 定版 landing mid-regenerate)
    live in exactly that window."""

    async def fake(
        system_prompt: str,
        user_prompt: str,
        model_cls: type[BaseModel],
        *,
        workflow: str = "unknown",
        tools: Any = None,
        max_tool_rounds: int | None = None,
        timeout_seconds: float | None = None,
    ) -> BaseModel:
        recorder = llm_log.LlmInteractionRecorder(workflow=workflow, model="m")
        recorder.begin_attempt([{"role": "user", "content": "summarize"}])
        recorder.finish(outcome="ok" if explode is None else "error", error=None)
        if side_effect is not None:
            side_effect()
        if explode is not None:
            raise explode
        return model_cls.model_validate({"summary": summary})

    monkeypatch.setattr("afterthread.services.tool_meta.generate_structured", fake)


def _fake_generate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, Any],
    files: dict[str, str] | None = None,
    record_session: bool = True,
    side_effect: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Install a fake generate_structured that builds ``files`` via the REAL
    meta-tools it receives (so containment/threadpool paths run), optionally
    records a genuine tool_install llm_log record (so the outcome's
    llm_log_id wiring is exercised), and returns ``result`` validated through
    the real model. Captures the call's kwargs for assertions.

    Also stubs the post-promote SUMMARY session (see ``_fake_summary_generate``)
    so every install here stays hermetic; a test that wants a different summary
    outcome re-stubs it afterwards.

    ``side_effect`` runs at the TOP of the session -- before the fake writes any
    file -- which is the only place a test can observe the world as the builder
    RECEIVED it: the staging workspace exactly as it was copied (a revise), the
    secret registry mid-session, or a package being deleted under a running
    build. The real thing is a multi-minute session, so this window is otherwise
    unreachable from a test."""
    captured: dict[str, Any] = {}

    async def fake(
        system_prompt: str,
        user_prompt: str,
        model_cls: type[BaseModel],
        *,
        workflow: str = "unknown",
        tools: Any = None,
        max_tool_rounds: int | None = None,
        timeout_seconds: float | None = None,
    ) -> BaseModel:
        captured.update(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            workflow=workflow,
            tools=tools,
            max_tool_rounds=max_tool_rounds,
            timeout_seconds=timeout_seconds,
        )
        if side_effect is not None:
            side_effect()
        by_name = {t.spec["function"]["name"]: t for t in tools or []}
        for path, content in (files or {}).items():
            wrote = await by_name["write_file"].handler({"path": path, "content": content})
            assert wrote.startswith("wrote"), wrote
        if record_session:
            recorder = llm_log.LlmInteractionRecorder(workflow=workflow, model="m")
            recorder.begin_attempt([{"role": "user", "content": "build"}])
            recorder.record_response("[tool_calls] ...")
            recorder.finish(outcome="ok", error=None)
        return model_cls.model_validate(result)

    monkeypatch.setattr("afterthread.services.tool_builder.generate_structured", fake)
    _fake_summary_generate(monkeypatch)
    return captured


def _no_fetch(monkeypatch: pytest.MonkeyPatch, text: str = "{}") -> None:
    async def fake_fetch(url: str) -> tuple[str | None, str | None]:
        return text, None

    monkeypatch.setattr("afterthread.services.tool_builder._fetch_openapi", fake_fetch)


def test_run_install_happy_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The full pipeline: build via meta-tools into staging, validate, move to
    tools_dir, registry sees a valid enabled package, staging cleaned, and the
    outcome links the session's llm_log record."""
    root = tmp_path / "tools"
    settings = _install_settings(monkeypatch, tools_dir=str(root))
    captured = _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "built and tested", "ready": True},
        files={"tool.json": json.dumps(_package_manifest("kbsearch")), "run.py": _GOOD_RUN_PY},
    )
    _no_fetch(monkeypatch)

    outcome = asyncio.run(run_install("http://kb.example/openapi.json", "build a search tool"))

    assert outcome.ok is True
    assert outcome.tool_name == "kbsearch"
    assert outcome.summary == "built and tested"
    assert outcome.llm_log_id is not None  # linked to the genuine session record
    assert (root / "kbsearch" / "tool.json").is_file()
    assert (root / "kbsearch" / "run.py").is_file()
    listed = tools.list_tools()
    assert [(t["name"], t["valid"], t["enabled"]) for t in listed] == [("kbsearch", True, True)]
    assert not (root / ".staging").exists()  # staging fully cleaned
    # The installer's budgets (not the workflow defaults) rode on the call.
    assert captured["workflow"] == "tool_install"
    assert captured["max_tool_rounds"] == settings.tool_install_max_rounds
    assert captured["timeout_seconds"] == settings.tool_install_timeout_seconds
    # The operator's instructions and the fetched document ride in the prompt.
    assert "build a search tool" in captured["user_prompt"]


def test_run_install_writes_the_summary_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D40: a successful install summarizes what it just built into the
    package's own sidecar, capturing the install ORIGIN (the OpenAPI url and the
    user's instructions) -- which is persisted nowhere else, and is what a later
    revise session needs for context."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "built and tested", "ready": True},
        files={"tool.json": json.dumps(_package_manifest("kbsearch")), "run.py": _GOOD_RUN_PY},
    )
    _fake_summary_generate(monkeypatch, summary="這個工具會查 KB")
    _no_fetch(monkeypatch)

    outcome = asyncio.run(run_install("http://kb.example/openapi.json", "build a search tool"))

    assert outcome.ok is True
    meta = tools.read_tool_meta(root / "kbsearch")
    assert meta is not None
    assert meta["summary"] == "這個工具會查 KB"
    assert meta["status"] == "draft"
    assert meta["origin"] == {
        "openapi_url": "http://kb.example" + tool_meta._ORIGIN_URL_TRIMMED_MARKER,
        "instructions": "build a search tool",
    }
    assert meta["llm_log_id"] == llm_log.last_record_id_for_workflow("tool_summary")
    # The sidecar is invisible to the registry: still exactly one valid package.
    assert [(t["name"], t["valid"]) for t in tools.list_tools()] == [("kbsearch", True)]
    # ... and the install outcome still links the BUILDER session, not the
    # summary one -- the two workflows are told apart by name.
    assert outcome.llm_log_id == llm_log.last_record_id_for_workflow("tool_install")


def test_run_install_never_captures_url_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The install URL is reduced to provenance AT CAPTURE (D40 r4/r5).

    The address a user pastes into the install form routinely carries a
    credential -- a presigned document link, HTTP basic userinfo -- that nothing
    ever registered as a known secret, so redaction cannot mask it. It would
    otherwise be persisted in the sidecar (the only copy, read back into every
    later regeneration) and replayed into this very prompt. Sanitizing in the
    hook call keeps the raw form inside ``run_install``, which is the one
    function that legitimately holds it: it is what we FETCHED with, and D40
    already rules nothing downstream re-fetches it. r5 tightened the capture
    further (path dropped too), which is why the assertions below check for
    the PATH's absence as well as the credentials'."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "built and tested", "ready": True},
        files={"tool.json": json.dumps(_package_manifest("kbsearch")), "run.py": _GOOD_RUN_PY},
    )
    seen: dict[str, str] = {}

    async def capture_summary(
        system_prompt: str, user_prompt: str, model_cls: type[BaseModel], **kwargs: Any
    ) -> BaseModel:
        seen["user_prompt"] = user_prompt
        return model_cls.model_validate({"summary": "這個工具會查 KB"})

    monkeypatch.setattr("afterthread.services.tool_meta.generate_structured", capture_summary)
    _no_fetch(monkeypatch)

    outcome = asyncio.run(
        run_install(
            "https://ops:BASIC-CREDENTIAL@kb.example/openapi.json?X-Amz-Signature=PRESIGNED-abcdef",
            "build a search tool",
        )
    )

    assert outcome.ok is True
    meta = tools.read_tool_meta(root / "kbsearch")
    assert meta is not None
    assert meta["origin"]["openapi_url"] == (
        "https://kb.example" + tool_meta._ORIGIN_URL_TRIMMED_MARKER
    )
    sidecar = (root / "kbsearch" / tools._AI_META_FILENAME).read_text(encoding="utf-8")
    assert "openapi.json" not in sidecar  # r5: the path is gone, not just the query
    for credential in ("PRESIGNED-abcdef", "BASIC-CREDENTIAL"):
        assert credential not in sidecar
        assert credential not in seen["user_prompt"]


@pytest.mark.parametrize(
    "explode",
    [RuntimeError("bug"), LLMNotConfiguredError("off")],
    ids=["bug", "llm-not-configured"],
)
def test_run_install_success_survives_a_failing_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explode: BaseException
) -> None:
    """The package is INSTALLED before the summary runs, so a summary failure of
    any kind must never flip the outcome -- it just leaves an empty-summary
    sidecar for the operator to regenerate from."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "built and tested", "ready": True},
        files={"tool.json": json.dumps(_package_manifest("kbsearch")), "run.py": _GOOD_RUN_PY},
    )
    _fake_summary_generate(monkeypatch, explode=explode)
    _no_fetch(monkeypatch)

    outcome = asyncio.run(run_install("http://kb.example/openapi.json", "build"))

    assert outcome.ok is True
    assert outcome.error is None
    assert (root / "kbsearch" / "tool.json").is_file()
    meta = tools.read_tool_meta(root / "kbsearch")
    assert meta is not None
    assert meta["summary"] == ""  # the placeholder a failed generation leaves


def test_run_install_summary_sees_the_installed_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The summary prompt is built from the PROMOTED package (not from staging),
    and the install secret is still masked while it runs."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "built and tested", "ready": True},
        files={"tool.json": json.dumps(_package_manifest("kbsearch")), "run.py": _GOOD_RUN_PY},
    )
    seen: dict[str, str] = {}

    async def capture_summary(
        system_prompt: str, user_prompt: str, model_cls: type[BaseModel], **kwargs: Any
    ) -> BaseModel:
        seen["user_prompt"] = user_prompt
        return model_cls.model_validate({"summary": "ok"})

    monkeypatch.setattr("afterthread.services.tool_meta.generate_structured", capture_summary)
    _no_fetch(monkeypatch)

    secret = "install-form-secret-abcdef"
    outcome = asyncio.run(
        run_install(
            "http://kb.example/openapi.json",
            "build",
            secret_name="KB_API_KEY",
            secret_value=secret,
        )
    )

    assert outcome.ok is True
    assert _GOOD_RUN_PY.strip() in seen["user_prompt"]  # the promoted run.py
    assert "KB_API_KEY" in seen["user_prompt"]  # the .env KEY name
    assert secret not in seen["user_prompt"]  # ... never its value


def test_run_install_ready_false_fails_with_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    _fake_generate(
        monkeypatch,
        result={"summary": "缺少 API key，無法測試", "ready": False},  # noqa: RUF001
    )
    _no_fetch(monkeypatch)

    outcome = asyncio.run(run_install("http://kb.example/openapi.json", "build"))

    assert outcome.ok is False
    assert "缺少 API key" in (outcome.error or "")
    assert outcome.llm_log_id is not None  # failure paths link the trace too
    assert not (root / ".staging").exists()


def test_run_install_rejects_invalid_staged_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "s", "ready": True},
        files={"tool.json": "{not json", "run.py": _GOOD_RUN_PY},
    )
    _no_fetch(monkeypatch)

    outcome = asyncio.run(run_install("http://kb.example/openapi.json", "build"))

    assert outcome.ok is False
    assert (outcome.error or "").startswith("工具包驗證失敗")
    assert not (root / "kbsearch").exists()
    assert not (root / ".staging").exists()


def test_run_install_rejects_manifest_name_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """tool.json's name must equal the reported tool_name (the future directory
    name) -- the same invariant every installed package lives under."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "s", "ready": True},
        files={"tool.json": json.dumps(_package_manifest("other")), "run.py": _GOOD_RUN_PY},
    )
    _no_fetch(monkeypatch)

    outcome = asyncio.run(run_install("http://kb.example/openapi.json", "build"))

    assert outcome.ok is False
    assert (outcome.error or "").startswith("工具包驗證失敗")
    assert not (root / "kbsearch").exists()


def test_run_install_rejects_existing_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "tools"
    (root / "kbsearch").mkdir(parents=True)  # pre-existing package of that name
    _install_settings(monkeypatch, tools_dir=str(root))
    _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "s", "ready": True},
        files={"tool.json": json.dumps(_package_manifest("kbsearch")), "run.py": _GOOD_RUN_PY},
    )
    _no_fetch(monkeypatch)

    outcome = asyncio.run(run_install("http://kb.example/openapi.json", "build"))

    assert outcome.ok is False
    assert outcome.error == _ERROR_NAME_TAKEN
    # The pre-existing package is untouched (nothing nested into it).
    assert list((root / "kbsearch").iterdir()) == []


def test_run_install_feature_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """tools_dir unset fails immediately -- before any fetch or LLM call."""
    _install_settings(monkeypatch, tools_dir="")

    async def must_not_fetch(url: str) -> tuple[str | None, str | None]:
        raise AssertionError("fetch must not run when the feature is off")

    monkeypatch.setattr("afterthread.services.tool_builder._fetch_openapi", must_not_fetch)
    outcome = asyncio.run(run_install("http://kb.example/openapi.json", "build"))
    assert outcome.ok is False
    assert outcome.error == _ERROR_TOOLS_DISABLED
    assert outcome.llm_log_id is None


# --- the forged-sidecar strip (D40 r7 / R7-1) ----------------------------------


def _forged_meta(value: str) -> str:
    """A finalized sidecar carrying ``value`` as its summary -- the forgery."""
    return json.dumps({"summary": value, "status": "final", "updated_at": "2026-01-01T00:00:00Z"})


def test_run_install_strips_a_forged_finalized_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R7-1, the whole attack chain: a builder session can smuggle a secret out
    through a sidecar it writes itself, and every gate on the path HELPS it.

    The builder (a half-trusted actor with a real shell, D21/H3) writes a staging
    ``.env`` holding a value nobody has registered -- ``known_secret_values``
    scans installed packages only and skips the dot-prefixed ``.staging`` shell,
    so the value is unknown for the whole build. It then writes
    ``.ai_meta.json`` = ``{"summary": "<that value>", "status": "final"}``:
    ``validate_package``'s embedded-secret sweep cannot match a secret it does
    not know, so the package passes; promote moves the staging directory whole;
    and then the install hook's own 定版 protection finishes the job --
    ``store_summary_meta`` sees ``final``, refuses to overwrite, and the forged
    sidecar is what every later GET serves, verbatim.

    The sidecar's only legitimate writer is ``write_tool_meta``, so promote now
    deletes the builder's copy BEFORE validation. What must hold afterwards is
    not "no sidecar" but "the BACKEND's sidecar": the hook regenerates one
    through the proper choke point moments later, so the summary panel still
    works and the forged text is nowhere on disk. The secret survives in exactly
    one place -- the ``.env``, which is where a tool's own credential belongs."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    smuggled = "smuggled-kb-value-abcdef123456"
    _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "built and tested", "ready": True},
        files={
            "tool.json": json.dumps(_package_manifest("kbsearch")),
            "run.py": _GOOD_RUN_PY,
            ".env": f"KB_API_KEY={smuggled}\n",
            tools._AI_META_FILENAME: _forged_meta(smuggled),
        },
    )
    _fake_summary_generate(monkeypatch, summary="這個工具會查 KB")
    _no_fetch(monkeypatch)

    outcome = asyncio.run(run_install("http://kb.example/openapi.json", "build a search tool"))

    # The PACKAGE is fine and installs: the sidecar was decoration, not grounds
    # to punish the operator for something the model did unasked.
    assert outcome.ok is True
    pkg = root / "kbsearch"
    assert (pkg / "tool.json").is_file()
    assert (pkg / "run.py").is_file()
    assert (pkg / ".env").is_file()

    # A sidecar EXISTS -- and it is the hook's, written through write_tool_meta:
    # draft (never the forged "final"), and the stubbed generation's text.
    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["status"] == "draft"
    assert meta["summary"] == "這個工具會查 KB"

    # The forged content is nowhere in the installed package: walking every file,
    # the smuggled value appears in the ``.env`` and in nothing else.
    bearers = sorted(
        str(path.relative_to(pkg))
        for path in pkg.rglob("*")
        if path.is_file() and smuggled in path.read_text(encoding="utf-8", errors="replace")
    )
    assert bearers == [".env"]
    assert not (root / ".staging").exists()


def test_run_install_strips_forged_sidecars_at_every_depth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The strip walks the whole staged tree, and covers the temp namespace too.

    A NESTED ``lib/.ai_meta.json`` is inert for ``read_tool_meta`` (which only
    ever reads the package root), so it is not the smuggling path -- but it still
    rides into the installed package, where a later revise copies it into a fresh
    staging build that ``validate_package``'s embedded-secret gate DOES scan
    against the by-then-registered value: a planted nested copy would brick every
    later revise with a rejection naming a file the operator never wrote.

    ``.ai_meta.json.<x>.tmp`` is the namespace ``_write_sidecar_atomic``'s
    ``mkstemp`` publishes through, so a leftover there is a legitimate artifact
    -- and therefore just as legitimate a thing for a builder to imitate.

    The collateral half of the same test: NOTHING else is touched. Every other
    file the builder produced, at every depth, arrives in the package intact."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "built and tested", "ready": True},
        files={
            "tool.json": json.dumps(_package_manifest("kbsearch")),
            "run.py": _GOOD_RUN_PY,
            "lib/helper.py": "VALUE = 1\n",
            "lib/.ai_meta.json": _forged_meta("nested forgery"),
            f"{tools._AI_META_FILENAME}.7f3a.tmp": _forged_meta("temp-namespace forgery"),
        },
    )
    _fake_summary_generate(monkeypatch, summary="這個工具會查 KB")
    _no_fetch(monkeypatch)

    outcome = asyncio.run(run_install("http://kb.example/openapi.json", "build a search tool"))

    assert outcome.ok is True
    pkg = root / "kbsearch"
    assert not (pkg / "lib" / tools._AI_META_FILENAME).exists()
    assert not list(pkg.glob(f"{tools._AI_META_FILENAME}*.tmp"))
    # Everything the builder legitimately produced survived the walk untouched.
    assert (pkg / "lib" / "helper.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (pkg / "run.py").read_text(encoding="utf-8") == _GOOD_RUN_PY
    assert json.loads((pkg / "tool.json").read_text(encoding="utf-8"))["name"] == "kbsearch"
    # ... and the ROOT sidecar is the hook's, not any of the plants.
    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["status"] == "draft"
    assert meta["summary"] == "這個工具會查 KB"


def test_strip_builder_sidecars_matches_the_reserved_name_case_insensitively(
    tmp_path: Path,
) -> None:
    """R10-2: on a case-INSENSITIVE filesystem (the macOS default, a supported
    platform) a builder-written ``.AI_META.JSON`` IS the file ``read_tool_meta``
    later opens as ``.ai_meta.json`` -- so a case-sensitive strip would promote
    the forgery and reopen the choke-point bypass R7-1 closed. The match is
    therefore case-insensitive everywhere; on a case-sensitive filesystem (this
    test's own likely host) the only effect is that a differently-cased name the
    builder had no business writing is removed too, which this pins directly."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / ".AI_META.JSON").write_text('{"status": "final"}', encoding="utf-8")
    (staging / ".Ai_Meta.Json.abc123.TMP").write_text("{}", encoding="utf-8")
    (staging / "run.py").write_text("print(1)", encoding="utf-8")

    assert tool_builder._strip_builder_sidecars(staging) is None

    remaining = sorted(entry.name for entry in staging.iterdir())
    assert remaining == ["run.py"]


def test_strip_builder_sidecars_handles_links_and_directories(tmp_path: Path) -> None:
    """The reserved name belongs to the backend in EVERY form it can take.

    A SYMLINK at the name is unlinked rather than followed, so a
    ``.ai_meta.json -> <somewhere else>`` plant costs its target nothing. (One
    pointing at a DIRECTORY is the awkward shape: ``os.walk`` classifies it as a
    directory, so it has to be pruned from the walk as well as removed.)

    A real DIRECTORY at the name is not a forged sidecar -- ``read_tool_meta``
    refuses a non-regular file -- but leaving it would permanently BRICK the
    package's summary: ``_write_sidecar_atomic``'s lstat gate refuses to publish
    over anything non-regular, so the install hook, every later regenerate and
    定版 would all fail forever, with no API path to repair it.

    Driven directly rather than through ``run_install`` because the meta-tools
    cannot produce these shapes -- only a ``run_shell`` (or an operator) can."""
    staging = tmp_path / "staging"
    (staging / "sub" / "deep").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept.txt").write_text("untouched", encoding="utf-8")

    (staging / tools._AI_META_FILENAME).symlink_to(outside)  # walked as a DIRECTORY
    nested_dir = staging / "sub" / tools._AI_META_FILENAME
    nested_dir.mkdir()
    (nested_dir / "payload.json").write_text("{}", encoding="utf-8")
    (staging / "sub" / "deep" / tools._AI_META_FILENAME).write_text("{}", encoding="utf-8")
    (staging / "sub" / "keep.py").write_text("K = 1\n", encoding="utf-8")

    assert tool_builder._strip_builder_sidecars(staging) is None

    assert not (staging / tools._AI_META_FILENAME).is_symlink()
    assert (outside / "kept.txt").read_text(encoding="utf-8") == "untouched"  # never followed
    assert not nested_dir.exists()
    assert not (staging / "sub" / "deep" / tools._AI_META_FILENAME).exists()
    assert (staging / "sub" / "keep.py").read_text(encoding="utf-8") == "K = 1\n"


def test_run_install_fails_closed_when_the_forged_sidecar_cannot_be_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deletion the filesystem refuses CANCELS the install.

    "We could not remove it" must never degrade into "so we shipped it": the
    forged sidecar reaching the package is the one unacceptable outcome, and it
    is unrecoverable once there (the hook's 定版 refusal makes it permanent). The
    refusal is injected at ``Path.unlink`` -- the real syscall boundary, the same
    style ``os.replace`` is failed at in test_tools -- because weird permissions
    and immutable attributes are not reproducible in a tmp dir.

    The error is category-only: a fixed zh-TW string, never the path that failed,
    since a builder chooses its own filenames and could embed a secret in one."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "built and tested", "ready": True},
        files={
            "tool.json": json.dumps(_package_manifest("kbsearch")),
            "run.py": _GOOD_RUN_PY,
            tools._AI_META_FILENAME: _forged_meta("undeletable forgery"),
        },
    )
    _no_fetch(monkeypatch)
    real_unlink = Path.unlink

    def refuse(self: Path, *, missing_ok: bool = False) -> None:
        if self.name == tools._AI_META_FILENAME:
            raise PermissionError(1, "Operation not permitted")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse)

    outcome = asyncio.run(run_install("http://kb.example/openapi.json", "build a search tool"))

    assert outcome.ok is False
    assert outcome.error == _ERROR_SIDECAR_STRIP
    assert not (root / "kbsearch").exists()  # nothing promoted


# --- staging-root tamper guard (D40 r8 / R8-1) ----------------------------------
#
# _promote_staging is driven DIRECTLY here (not through run_install): the attack
# these tests reproduce is run_shell mv-ing staging aside and planting a symlink at
# its original path, and the meta-tools (the only thing a fake generate_structured
# can drive) are jailed and cannot produce that shape -- only a real shell (or an
# operator) can, exactly as test_strip_builder_sidecars_handles_links_and_directories
# does for the sidecar-strip shapes above.


def test_promote_staging_refuses_symlinked_staging_root(tmp_path: Path) -> None:
    """The exact R8-1 attack: a symlink planted AT the staging leaf, pointing at an
    unrelated directory that happens to hold a planted sidecar. Refused before the
    strip ever runs, so the decoy's file is never touched -- proof the walk never
    started, not just that it would have been harmless."""
    base = tmp_path / "tools"
    staging_parent = base / tool_builder._STAGING_DIRNAME
    staging_parent.mkdir(parents=True)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    decoy_meta = _forged_meta("decoy-should-never-be-touched")
    (decoy / tools._AI_META_FILENAME).write_text(decoy_meta, encoding="utf-8")
    staging = staging_parent / "buildid"
    staging.symlink_to(decoy, target_is_directory=True)

    error = tool_builder._promote_staging(staging, "kbsearch", base)

    assert error == _ERROR_STAGING_TAMPERED
    assert not (base / "kbsearch").exists()
    assert (decoy / tools._AI_META_FILENAME).read_text(encoding="utf-8") == decoy_meta


def test_promote_staging_refuses_staging_replaced_by_symlink_to_real_tools_dir(
    tmp_path: Path,
) -> None:
    """The literal finding: staging renamed aside, and a symlink planted at its
    ORIGINAL path pointing at ``base`` -- the real, live tools directory -- itself.
    Unguarded, the strip's os.walk would land in ``base`` and delete every
    installed package's sidecar; refused here before that walk ever starts, so an
    existing package's finalized sidecar survives untouched."""
    base = tmp_path / "tools"
    base.mkdir()
    existing_pkg = base / "existing-tool"
    existing_pkg.mkdir()
    existing_meta = json.dumps(
        {
            "summary": "already finalized, do not touch",
            "status": "final",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )
    (existing_pkg / tools._AI_META_FILENAME).write_text(existing_meta, encoding="utf-8")
    staging_parent = base / tool_builder._STAGING_DIRNAME
    staging_parent.mkdir()
    # Stands in for "run_shell mv'd the real staging dir aside" -- what matters for
    # this check is only the END STATE at the original path, not how it got there.
    staging = staging_parent / "buildid"
    staging.symlink_to(base, target_is_directory=True)

    error = tool_builder._promote_staging(staging, "kbsearch", base)

    assert error == _ERROR_STAGING_TAMPERED
    assert not (base / "kbsearch").exists()
    assert (existing_pkg / tools._AI_META_FILENAME).read_text(encoding="utf-8") == existing_meta


def test_promote_staging_refuses_ancestor_staging_shell_replaced_by_symlink(
    tmp_path: Path,
) -> None:
    """The case the LEAF is_symlink() check alone cannot catch: the ``.staging``
    SHELL itself (an ancestor of the staging path, not the leaf) is swapped for a
    symlink pointing at ``base``. The leaf entry (matching this build's uuid) then
    has to be an ordinary REAL directory sitting directly under ``base`` for the
    path to resolve at all -- so ``is_symlink()`` on the full staging path reports
    False (confirmed below) even though the fully-resolved path has escaped the
    ``<base>/.staging`` shell entirely. Only the containment half of
    ``_verify_staging_root`` catches this."""
    base = tmp_path / "tools"
    base.mkdir()
    existing_pkg = base / "existing-tool"
    existing_pkg.mkdir()
    existing_meta = json.dumps(
        {
            "summary": "already finalized, do not touch",
            "status": "final",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )
    (existing_pkg / tools._AI_META_FILENAME).write_text(existing_meta, encoding="utf-8")
    (base / tool_builder._STAGING_DIRNAME).symlink_to(base, target_is_directory=True)
    (base / "buildid").mkdir()  # the ordinary directory the swapped ancestor exposes
    staging = base / tool_builder._STAGING_DIRNAME / "buildid"

    assert not staging.is_symlink()  # the leaf alone looks perfectly honest

    error = tool_builder._promote_staging(staging, "kbsearch", base)

    assert error == _ERROR_STAGING_TAMPERED
    assert not (base / "kbsearch").exists()
    assert (existing_pkg / tools._AI_META_FILENAME).read_text(encoding="utf-8") == existing_meta


def test_promote_staging_allows_honest_staging(tmp_path: Path) -> None:
    """Control for R8-1: an ordinary staging directory -- a real directory sitting
    directly under a real ``<base>/.staging`` shell, no symlink anywhere in the
    chain -- still promotes exactly as before. The new re-verification adds a
    check, not a new way to refuse honest work."""
    base = tmp_path / "tools"
    staging = base / tool_builder._STAGING_DIRNAME / "buildid"
    staging.mkdir(parents=True)
    (staging / "tool.json").write_text(json.dumps(_package_manifest("kbsearch")), encoding="utf-8")
    (staging / "run.py").write_text(_GOOD_RUN_PY, encoding="utf-8")

    error = tool_builder._promote_staging(staging, "kbsearch", base)

    assert error is None
    assert (base / "kbsearch" / "run.py").read_text(encoding="utf-8") == _GOOD_RUN_PY
    assert not staging.exists()  # moved, not copied


def test_cleanup_staging_does_not_follow_a_symlinked_staging(tmp_path: Path) -> None:
    """Collateral (R8-1): a refused promote can now leave ``staging`` itself a
    symlink -- exactly the tamper shape the checks above refuse -- and
    run_install's ``finally`` calls ``_cleanup_staging`` on it unconditionally
    either way. ``shutil.rmtree`` REFUSES to operate on a path that is itself a
    symlink (raises "Cannot call rmtree on a symbolic link" -- CPython GH-46010 --
    rather than deleting through it); with ``ignore_errors=True`` that refusal is
    swallowed, so cleanup's rmtree call is a silent no-op: neither the symlink NOR
    (crucially) its target is removed. The symlink is then left behind in
    ``.staging`` (the parent's ``rmdir()`` finds it non-empty and the ENOTEMPTY is
    suppressed) -- an untidy leftover, never a deletion through the link. Verified
    directly against this repo's Python before writing this assertion; pinned here
    because the alternative (rmtree quietly following the link) would be a real
    vulnerability if a future Python or refactor ever changed it."""
    base = tmp_path / "tools"
    base.mkdir()
    existing_pkg = base / "existing-tool"
    existing_pkg.mkdir()
    (existing_pkg / "keep.txt").write_text("do not delete", encoding="utf-8")
    staging_parent = base / tool_builder._STAGING_DIRNAME
    staging_parent.mkdir()
    staging = staging_parent / "buildid"
    staging.symlink_to(base, target_is_directory=True)  # the R8-1 tamper shape

    tool_builder._cleanup_staging(staging, base)

    assert (existing_pkg / "keep.txt").read_text(encoding="utf-8") == "do not delete"
    assert staging.is_symlink()  # left behind, untidy but never destructive


def test_cleanup_staging_refuses_a_symlinked_staging_ancestor(tmp_path: Path) -> None:
    """R9-1: the ancestor-substitution shape the promote-side gate refuses must
    be refused by CLEANUP too, because run_install's ``finally`` reaches it
    unconditionally and rmtree's own protection does NOT cover it: with the
    ``.staging`` shell swapped for a symlink to an EXTERNAL directory holding a
    real ``<uuid>`` subdir, the rmtree LEAF is an ordinary directory (leaf
    ``is_symlink()`` is False), so CPython would happily delete the external
    directory straight through the link. ``_cleanup_staging`` therefore runs the
    SAME ``_verify_staging_root`` gate as ``_promote_staging`` and, on refusal,
    leaves the whole workspace in place -- tampering evidence, not garbage."""
    base = tmp_path / "tools"
    base.mkdir()
    external = tmp_path / "external"
    victim = external / "buildid"
    victim.mkdir(parents=True)
    (victim / "keep.txt").write_text("survive", encoding="utf-8")
    # .staging ITSELF is the symlink; the staging leaf over in external/ is a
    # perfectly real directory, so only resolved containment can catch this.
    (base / tool_builder._STAGING_DIRNAME).symlink_to(external, target_is_directory=True)
    staging = base / tool_builder._STAGING_DIRNAME / "buildid"
    assert staging.is_dir() and not staging.is_symlink()

    tool_builder._cleanup_staging(staging, base)

    assert victim.is_dir()
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "survive"


def test_cleanup_staging_still_removes_an_honest_staging(tmp_path: Path) -> None:
    """Control for the R9-1 gate: an untampered staging is still removed and the
    empty ``.staging`` shell still dropped -- the gate must never turn routine
    cleanup into an accumulating leak."""
    base = tmp_path / "tools"
    base.mkdir()
    staging = base / tool_builder._STAGING_DIRNAME / "buildid"
    staging.mkdir(parents=True)
    (staging / "junk.txt").write_text("x", encoding="utf-8")

    tool_builder._cleanup_staging(staging, base)

    assert not staging.exists()
    assert not (base / tool_builder._STAGING_DIRNAME).exists()


# --- sidecar-strip walk failures fail closed (D40 r8 / R8-2) --------------------


def _permission_tests_unreliable() -> bool:
    """True when POSIX permission bits cannot be trusted to block access: on
    Windows ``chmod`` only toggles a read-only bit (never real POSIX modes), and
    root bypasses permission checks entirely -- ``chmod 000`` would not actually
    block the walk under either, which would make the tests below fail-negative
    rather than exercise anything. ``sys.platform`` is checked FIRST so the ``or``
    short-circuits before ``os.geteuid`` -- which does not exist on Windows -- is
    ever called."""
    return sys.platform == "win32" or os.geteuid() == 0


@pytest.mark.skipif(
    _permission_tests_unreliable(), reason="chmod 000 does not block access (root or non-POSIX)"
)
def test_promote_staging_fails_closed_on_unreadable_subtree_with_sidecar(tmp_path: Path) -> None:
    """R8-2: ``os.walk`` SWALLOWS scandir errors by default (``onerror=None`` means
    "skip silently and keep going"), so a builder that plants a nested sidecar and
    then ``chmod 000``s its parent would otherwise sail through the walk with that
    subtree unvisited -- the strip reports success, and the forged nested sidecar
    ships. The stripper now passes an ``onerror`` that RE-RAISES, so the walk's own
    failure joins the existing fail-closed ``OSError`` path and nothing is moved."""
    base = tmp_path / "tools"
    staging = base / tool_builder._STAGING_DIRNAME / "buildid"
    staging.mkdir(parents=True)
    (staging / "tool.json").write_text(json.dumps(_package_manifest("kbsearch")), encoding="utf-8")
    (staging / "run.py").write_text(_GOOD_RUN_PY, encoding="utf-8")
    sealed = staging / "sealed"
    sealed.mkdir()
    (sealed / tools._AI_META_FILENAME).write_text(_forged_meta("nested forgery"), encoding="utf-8")
    sealed.chmod(0o000)
    try:
        error = tool_builder._promote_staging(staging, "kbsearch", base)
    finally:
        sealed.chmod(0o700)  # restore so tmp_path's own teardown can remove the tree

    assert error == _ERROR_SIDECAR_STRIP
    assert not (base / "kbsearch").exists()  # nothing promoted


@pytest.mark.skipif(
    _permission_tests_unreliable(), reason="chmod 000 does not block access (root or non-POSIX)"
)
def test_promote_staging_fails_closed_on_unreadable_subtree_without_sidecar(tmp_path: Path) -> None:
    """Same refusal even though the sealed subtree carries NO sidecar at all: an
    incomplete traversal cannot distinguish "nothing in there" from "something in
    there we could not see", so fail-closed here is keyed on TRAVERSAL
    COMPLETENESS, not on whether a sidecar was actually found. The one signal
    available (permission denied) is exactly as alarming either way, so both must
    refuse identically."""
    base = tmp_path / "tools"
    staging = base / tool_builder._STAGING_DIRNAME / "buildid"
    staging.mkdir(parents=True)
    (staging / "tool.json").write_text(json.dumps(_package_manifest("kbsearch")), encoding="utf-8")
    (staging / "run.py").write_text(_GOOD_RUN_PY, encoding="utf-8")
    sealed = staging / "sealed"
    sealed.mkdir()  # deliberately empty -- no sidecar planted anywhere inside
    sealed.chmod(0o000)
    try:
        error = tool_builder._promote_staging(staging, "kbsearch", base)
    finally:
        sealed.chmod(0o700)

    assert error == _ERROR_SIDECAR_STRIP
    assert not (base / "kbsearch").exists()


def test_promote_staging_readable_package_unaffected_by_onerror_hook(tmp_path: Path) -> None:
    """The R8-2 ``onerror`` hook only fires on an ACTUAL scandir failure: an
    ordinary, fully readable staged package -- nested directories included --
    promotes exactly as before."""
    base = tmp_path / "tools"
    staging = base / tool_builder._STAGING_DIRNAME / "buildid"
    (staging / "lib").mkdir(parents=True)
    (staging / "tool.json").write_text(json.dumps(_package_manifest("kbsearch")), encoding="utf-8")
    (staging / "run.py").write_text(_GOOD_RUN_PY, encoding="utf-8")
    (staging / "lib" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")

    error = tool_builder._promote_staging(staging, "kbsearch", base)

    assert error is None
    assert (base / "kbsearch" / "lib" / "helper.py").read_text(encoding="utf-8") == "VALUE = 1\n"


# --- install-form secret (D36) -------------------------------------------------


def test_run_shell_receives_injected_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_build_meta_tools(secret_env=...)`` injects the secret ONLY into
    run_shell's env: the builder can use $NAME to live-test the real API. Our own
    OPENAI creds stay absent (the from-scratch base env is preserved)."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    staging = tmp_path / "staging"
    staging.mkdir()
    meta = {
        t.spec["function"]["name"]: t
        for t in _build_meta_tools(staging, secret_env={"KB_API_KEY": "shell-secret-123456"})
    }

    result = _call(meta["run_shell"].handler, {"command": 'echo "K=$KB_API_KEY"'})
    assert "K=shell-secret-123456" in result
    scrubbed = _call(meta["run_shell"].handler, {"command": 'echo "O=${OPENAI_API_KEY:-none}"'})
    assert "O=none" in scrubbed


def test_run_install_secret_injected_into_shell_and_env_never_in_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end (D36): the form secret reaches run_shell's env during the build
    (so the builder can live-test the API) and is written into the promoted
    tool's ``.env`` by the backend, yet the VALUE never appears in the builder
    prompts or the outcome -- only the NAME reaches the system prompt."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    secret_name = "KB_API_KEY"
    secret_value = "topsecretvalue-abcdef123456"
    captured: dict[str, Any] = {}

    async def fake(
        system_prompt: str,
        user_prompt: str,
        model_cls: type[BaseModel],
        *,
        workflow: str = "unknown",
        tools: Any = None,
        max_tool_rounds: int | None = None,
        timeout_seconds: float | None = None,
    ) -> BaseModel:
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        by_name = {t.spec["function"]["name"]: t for t in tools or []}
        await by_name["write_file"].handler(
            {"path": "tool.json", "content": json.dumps(_package_manifest("kbsearch"))}
        )
        await by_name["write_file"].handler({"path": "run.py", "content": _GOOD_RUN_PY})
        # The builder live-tests the API using the injected env var.
        captured["shell"] = await by_name["run_shell"].handler(
            {"command": 'echo "probe=$KB_API_KEY"'}
        )
        return model_cls.model_validate(
            {"tool_name": "kbsearch", "summary": "built and tested", "ready": True}
        )

    monkeypatch.setattr("afterthread.services.tool_builder.generate_structured", fake)
    _no_fetch(monkeypatch)

    outcome = asyncio.run(
        run_install(
            "http://kb.example/openapi.json",
            "build a search tool",
            secret_name=secret_name,
            secret_value=secret_value,
        )
    )

    assert outcome.ok is True
    # run_shell DID inject the secret into its env (the builder can live-test the real
    # API with it), but F1 MASKS any known secret value out of the meta-tool RESULT
    # before it re-enters the builder conversation -- so the raw value never rides
    # back, only the redaction marker where it stood.
    assert secret_value not in captured["shell"]
    assert tools._REDACTION_MARKER in captured["shell"]
    # The promoted tool's .env carries the secret, written by the backend (the .env
    # file is not the conversation, so it holds the raw value).
    env_text = (root / "kbsearch" / ".env").read_text(encoding="utf-8")
    assert f"{secret_name}={secret_value}" in env_text
    # The system prompt names the secret but NEVER its value; neither does the
    # user prompt or the outcome.
    assert secret_name in captured["system_prompt"]
    assert secret_value not in captured["system_prompt"]
    assert secret_value not in captured["user_prompt"]
    assert secret_value not in (outcome.summary or "")
    assert secret_value not in (outcome.error or "")


def test_run_install_registers_and_discards_inflight_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The form secret is registered in the in-flight redaction set DURING the
    build (so llm_log masks it from the session trace, before promote writes it
    into the .env) and discarded afterwards."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    secret_value = "inflight-secret-value-123456"
    seen: dict[str, bool] = {}
    # Captured before the fake, whose ``tools`` parameter (the meta-tool list)
    # shadows the module name inside its body.
    known_secret_values = tools.known_secret_values

    async def fake(
        system_prompt: str,
        user_prompt: str,
        model_cls: type[BaseModel],
        *,
        workflow: str = "unknown",
        tools: Any = None,
        max_tool_rounds: int | None = None,
        timeout_seconds: float | None = None,
    ) -> BaseModel:
        # During the session the value is redactable via the known-secret set.
        seen["registered_during"] = secret_value in known_secret_values()
        by_name = {t.spec["function"]["name"]: t for t in tools or []}
        await by_name["write_file"].handler(
            {"path": "tool.json", "content": json.dumps(_package_manifest("kbsearch"))}
        )
        await by_name["write_file"].handler({"path": "run.py", "content": _GOOD_RUN_PY})
        return model_cls.model_validate({"tool_name": "kbsearch", "summary": "s", "ready": True})

    monkeypatch.setattr("afterthread.services.tool_builder.generate_structured", fake)
    _no_fetch(monkeypatch)
    asyncio.run(
        run_install(
            "http://kb.example/openapi.json",
            "build",
            secret_name="KB_API_KEY",
            secret_value=secret_value,
        )
    )

    assert seen["registered_during"] is True
    # The transient in-flight registration is gone after the install (the .env
    # scan now covers the value instead).
    with tools._INFLIGHT_LOCK:
        assert secret_value not in tools._INFLIGHT_SECRETS


def test_run_install_replaces_disobedient_secret_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the model DISOBEYED and wrote its own ``KB_API_KEY`` line, promote
    REPLACES it with the real value rather than duplicating (D36)."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "s", "ready": True},
        files={
            "tool.json": json.dumps(_package_manifest("kbsearch")),
            "run.py": _GOOD_RUN_PY,
            ".env": "OTHER=keep\nKB_API_KEY=placeholder-the-model-wrote\n",
        },
    )
    _no_fetch(monkeypatch)

    asyncio.run(
        run_install(
            "http://kb.example/openapi.json",
            "build",
            secret_name="KB_API_KEY",
            secret_value="real-secret-value-123456",
        )
    )
    env_text = (root / "kbsearch" / ".env").read_text(encoding="utf-8")
    assert "KB_API_KEY=real-secret-value-123456" in env_text
    assert "placeholder-the-model-wrote" not in env_text
    assert "OTHER=keep" in env_text  # unrelated lines preserved
    assert env_text.count("KB_API_KEY=") == 1  # replaced, not duplicated


def test_run_install_secret_over_env_cap_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If writing the secret into the staged ``.env`` would push it past the
    64KiB cap, the install is refused with a friendly error and nothing is
    promoted -- validate_package passed the pre-secret ``.env``, so this is our
    OWN post-append guard (D36)."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    # A .env just under the cap: passes validate_package, but appending the
    # secret line pushes the total over _ENV_FILE_MAX_BYTES.
    near_cap_env = "PAD=" + "y" * (tools._ENV_FILE_MAX_BYTES - 5)  # <= 64KiB, no newline
    _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "s", "ready": True},
        files={
            "tool.json": json.dumps(_package_manifest("kbsearch")),
            "run.py": _GOOD_RUN_PY,
            ".env": near_cap_env,
        },
    )
    _no_fetch(monkeypatch)

    outcome = asyncio.run(
        run_install(
            "http://kb.example/openapi.json",
            "build",
            secret_name="KB_API_KEY",
            secret_value="v-abcdef",
        )
    )
    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_SECRET_ENV_TOO_LARGE
    assert not (root / "kbsearch").exists()  # nothing promoted
    assert not (root / ".staging").exists()  # staging cleaned


def test_run_install_redacts_secret_from_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F1(c): a disobedient builder that prints the secret VALUE in its summary must
    not leak it into the outcome (-> job state -> poll response). The summary is
    redacted at the source, while the in-flight secret is still registered, so the
    stored summary carries the marker, never the value."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    secret_value = "leaky-secret-value-abcdef123"

    async def fake(
        system_prompt: str,
        user_prompt: str,
        model_cls: type[BaseModel],
        *,
        workflow: str = "unknown",
        tools: Any = None,
        max_tool_rounds: int | None = None,
        timeout_seconds: float | None = None,
    ) -> BaseModel:
        by_name = {t.spec["function"]["name"]: t for t in tools or []}
        await by_name["write_file"].handler(
            {"path": "tool.json", "content": json.dumps(_package_manifest("kbsearch"))}
        )
        await by_name["write_file"].handler({"path": "run.py", "content": _GOOD_RUN_PY})
        # The model DISOBEYS and echoes the secret value in its summary.
        return model_cls.model_validate(
            {"tool_name": "kbsearch", "summary": f"done using {secret_value}", "ready": True}
        )

    monkeypatch.setattr("afterthread.services.tool_builder.generate_structured", fake)
    _no_fetch(monkeypatch)

    outcome = asyncio.run(
        run_install(
            "http://kb.example/openapi.json",
            "build",
            secret_name="KB_API_KEY",
            secret_value=secret_value,
        )
    )
    assert outcome.ok is True
    assert secret_value not in (outcome.summary or "")
    assert tools._REDACTION_MARKER in (outcome.summary or "")


def test_run_install_redacts_secret_from_not_ready_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F1(c): on a ready=false outcome the error is built FROM the summary, so the
    single source redaction keeps the secret out of BOTH the summary and the error."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    secret_value = "leaky-secret-value-abcdef123"
    _fake_generate(
        monkeypatch,
        result={"summary": f"blocked, saw {secret_value}", "ready": False},
    )
    _no_fetch(monkeypatch)

    outcome = asyncio.run(
        run_install(
            "http://kb.example/openapi.json",
            "build",
            secret_name="KB_API_KEY",
            secret_value=secret_value,
        )
    )
    assert outcome.ok is False
    assert secret_value not in (outcome.summary or "")
    assert secret_value not in (outcome.error or "")
    assert tools._REDACTION_MARKER in (outcome.error or "")


def test_run_install_rejects_manifest_embedding_inflight_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """H3 end-to-end: a builder that bakes the in-flight install secret into the staged
    manifest is rejected at validation -- the secret is registered for the whole build,
    so validate_package (which runs BEFORE the finally discard) catches it, the install
    fails with the friendly reason, and nothing is promoted. The value is never echoed."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    secret_value = "topsecretvalue-abcdef123456"

    async def fake(
        system_prompt: str,
        user_prompt: str,
        model_cls: type[BaseModel],
        *,
        workflow: str = "unknown",
        tools: Any = None,
        max_tool_rounds: int | None = None,
        timeout_seconds: float | None = None,
    ) -> BaseModel:
        by_name = {t.spec["function"]["name"]: t for t in tools or []}
        # The builder DISOBEYS and bakes the secret into the manifest description.
        manifest = _package_manifest("kbsearch")
        manifest["description"] = f"searches using {secret_value} to authenticate"
        await by_name["write_file"].handler({"path": "tool.json", "content": json.dumps(manifest)})
        await by_name["write_file"].handler({"path": "run.py", "content": _GOOD_RUN_PY})
        return model_cls.model_validate({"tool_name": "kbsearch", "summary": "s", "ready": True})

    monkeypatch.setattr("afterthread.services.tool_builder.generate_structured", fake)
    _no_fetch(monkeypatch)

    outcome = asyncio.run(
        run_install(
            "http://kb.example/openapi.json",
            "build",
            secret_name="KB_API_KEY",
            secret_value=secret_value,
        )
    )
    assert outcome.ok is False
    assert (outcome.error or "").startswith("工具包驗證失敗")
    assert "不得包含秘密值" in (outcome.error or "")
    assert secret_value not in (outcome.error or "")  # the value is never echoed
    assert not (root / "kbsearch").exists()  # nothing promoted
    assert not (root / ".staging").exists()  # staging cleaned


def test_inject_secret_removes_all_duplicate_name_lines(tmp_path: Path) -> None:
    """F7: python-dotenv is LAST-occurrence-wins, so _inject_secret_into_env drops
    EVERY prior NAME= line (plain, spaced, or ``export ``-prefixed) and appends the
    single real line -- a model-written second NAME= line can never override the
    backend's value. The final .env has exactly one NAME= line and dotenv_values
    resolves NAME to the backend's value; unrelated lines are preserved."""
    from dotenv import dotenv_values

    env_file = tmp_path / ".env"
    env_file.write_text(
        "OTHER=keep\n"
        "KB_API_KEY=first-model-placeholder\n"
        "MIDDLE=alsokeep\n"
        "export KB_API_KEY=second-model-placeholder\n",
        encoding="utf-8",
    )

    error = tool_builder._inject_secret_into_env(env_file, "KB_API_KEY", "real-secret-abcdef")
    assert error is None

    text = env_file.read_text(encoding="utf-8")
    assert text.count("KB_API_KEY=") == 1  # both model lines dropped, one real appended
    resolved = dotenv_values(str(env_file))
    assert resolved["KB_API_KEY"] == "real-secret-abcdef"  # the backend's value wins
    assert resolved["OTHER"] == "keep"  # unrelated lines preserved
    assert resolved["MIDDLE"] == "alsokeep"


@pytest.mark.parametrize(
    "value",
    [
        "secret#value-abcdef",  # inline-comment char
        "secret value abcdef",  # spaces
        "secret'value-abcdef",  # single quote -> double-quoted path
        'secret"value-abcdef',  # double quote -> single-quoted path
        "secret$VALUE-abcdef",  # interpolation char (must stay literal)
        "secret\\value-abcdef",  # backslash
        '"quoted-value-abcdef"',  # leading/trailing quote
        "mix'ab$c-#=`! def",  # single-quote + interp/comment/space mix (no ", now a refusal)
    ],
    ids=[
        "hash",
        "spaces",
        "single-quote",
        "double-quote",
        "dollar",
        "backslash",
        "quoted",
        "mixed",
    ],
)
def test_inject_secret_round_trips_tricky_values(tmp_path: Path, value: str) -> None:
    """F5: a value with dotenv-significant characters is serialized so the runtime loader
    parses it back BYTE-FOR-BYTE. Written into a real .env, then read back with the SAME
    interpolate=False loader the runtime uses (tools._load_tool_dotenv) -- the value the
    redaction registry ends up with is exactly what was submitted, no transform."""
    env_file = tmp_path / ".env"
    error = tool_builder._inject_secret_into_env(env_file, "KB_API_KEY", value)
    assert error is None
    loaded = tools._load_tool_dotenv(tmp_path)
    assert loaded["KB_API_KEY"] == value


def test_inject_secret_tricky_value_reaches_subprocess_env(tmp_path: Path) -> None:
    """F5: the exact submitted value (dotenv-significant chars and all) is what a runtime
    tool's subprocess environment receives, since _build_tool_env layers _load_tool_dotenv
    on top of the passthrough allowlist. Uses a single-quote-bearing value (no ``"``/``\\``
    -- that combo is separately refused), so it serializes verbatim and round-trips."""
    value = "tricky'$#-value abcdef"
    env_file = tmp_path / ".env"
    assert tool_builder._inject_secret_into_env(env_file, "KB_API_KEY", value) is None
    env = tools._build_tool_env(tmp_path)
    assert env["KB_API_KEY"] == value


def test_inject_secret_refuses_non_round_trippable_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F5: when serialization would produce a value the loader does NOT parse back
    verbatim, the round-trip guard REFUSES the install rather than writing a divergent
    value. Forced by stubbing the serializer to emit the RAW (unquoted) form for a value
    dotenv transforms -- a leading double-quote is parsed as a quoted value, stripping
    it. This proves the round-trip check, not the quoting rules, is the guarantee."""
    monkeypatch.setattr(tool_builder, "_dotenv_serialize_value", lambda v: v)
    env_file = tmp_path / ".env"
    error = tool_builder._inject_secret_into_env(env_file, "KB_API_KEY", '"quoted-abcdef"')
    assert error == tool_builder._ERROR_SECRET_ENV_UNSERIALIZABLE
    assert not env_file.exists()  # a transformed value is never written


@pytest.mark.parametrize(
    "value",
    ["has'a\"quote-abcdef", "has'a\\slash-abcdef"],
    ids=["single+double-quote", "single-quote+backslash"],
)
def test_inject_secret_refuses_escape_producing_value(tmp_path: Path, value: str) -> None:
    """F5/D36: a value holding a single quote ALONGSIDE a ``"`` or ``\\`` can only be
    double-quoted with an ESCAPE, and the escaped raw .env spelling (e.g. ``"ab'cd\\"ef"``)
    no longer contains the ORIGINAL value as an exact substring -- a runtime tool catting
    the .env would emit a reversible-but-unredactable form the registry (which holds the
    original value) can never match. Serialization refuses up front: clean install cancel,
    nothing written."""
    env_file = tmp_path / ".env"
    error = tool_builder._inject_secret_into_env(env_file, "KB_API_KEY", value)
    assert error == tool_builder._ERROR_SECRET_ENV_UNSERIALIZABLE
    assert not env_file.exists()


def test_inject_secret_single_quote_value_stays_verbatim_in_raw_env(tmp_path: Path) -> None:
    """F5/D36: a value with a single quote but no ``"``/``\\`` installs, and the RAW .env
    line embeds it VERBATIM as an exact substring -- so if a runtime tool cats the .env,
    the redactors (which register the original value) still match and mask it. This is the
    property the escape-refusal above protects."""
    value = "api'key-abcdef123"
    env_file = tmp_path / ".env"
    assert tool_builder._inject_secret_into_env(env_file, "KB_API_KEY", value) is None
    raw = env_file.read_text(encoding="utf-8")
    assert value in raw  # exact substring -> redactable
    assert tools._load_tool_dotenv(tmp_path)["KB_API_KEY"] == value  # and round-trips


@pytest.mark.parametrize(
    "value",
    [
        "plain-secret-abcdef",  # the unquoted branch: no quoting needed at all
        "secret#value-abcdef",  # inline-comment char -> single-quoted
        "secret value abcdef",  # spaces
        "secret'value-abcdef",  # single quote -> the DOUBLE-quoted branch
        'secret"value-abcdef',  # double quote -> single-quoted
        "secret$VALUE-abcdef",  # interpolation char (stays literal)
        "secret\\value-abcdef",  # backslash
        '"quoted-value-abcdef"',  # leading/trailing quote
        "mix'ab$c-#=`! def",  # single-quote + an ``=`` INSIDE the value
        "  padded-abcdef  ",  # not form-reachable (the schema strips) -- pins the RHS strip
    ],
)
def test_an_env_this_system_wrote_always_passes_the_revise_spelling_gate(
    tmp_path: Path, value: str
) -> None:
    """Every ``.env`` line the INSTALL path can write passes the revise spelling
    gate -- by construction, and pinned here (R6-1).

    The gate refuses a value its assignment line does not spell literally, so the
    one thing it must never do is refuse a package this system itself produced.
    Two properties of ``_inject_secret_into_env`` make that impossible rather than
    lucky: it drops EVERY prior line assigning the name and APPENDS its own, so its
    line is the LAST one for that key (which is the one the gate reads, dotenv
    being last-wins), and ``_dotenv_serialize_value`` only ever emits spellings
    that embed the value verbatim -- bare, single-quoted, or double-quoted with no
    escape able to fire -- refusing the install outright when it cannot. The value
    holding an ``=`` is deliberate: the RHS is everything after the FIRST one.

    The rest of the file is not install's work -- those lines came from the model
    or a hand edit -- and they are governed by the same rule: a reversible spelling
    among them refuses the revise, which is the gate working, not a false positive.
    Seeded here with the disobedient shapes install itself has to survive."""
    env_file = tmp_path / ".env"
    # Every value here clears ``_MIN_SECRET_LEN``: the OTHER gate in this policy is
    # the r1 floor, and a short model-written value refuses for that reason instead.
    env_file.write_text(
        "OTHER=keep-this-one\nKB_API_KEY=first-model-placeholder\n"
        "export KB_API_KEY=second-model-placeholder\n",
        encoding="utf-8",
    )

    assert tool_builder._inject_secret_into_env(env_file, "KB_API_KEY", value) is None

    # Exactly what a later revise would see: the same bounded reader, the same
    # parser, the same non-empty filter.
    text = tools._read_regular_file_capped(env_file, tools._ENV_FILE_MAX_BYTES)
    assert text is not None
    parsed = {key: parsed for key, parsed in tools._parse_dotenv_text(text).items() if parsed}
    assert parsed["KB_API_KEY"] == value  # the backend's line is the one that won
    assert tool_builder._unmaskable_env_error(text, parsed) is None


@pytest.mark.parametrize(
    "value",
    [
        "plain-secret-abcdef",  # bare, single- and double-quoted are all safe
        "secret#value-abcdef",  # needs quoting: bare is NOT vouched for
        "secret value abcdef",  # ... nor is an unquoted value with spaces
        "secret'value-abcdef",  # a single quote rules the single-quoted form out
        'secret"value-abcdef',  # a double quote rules the double-quoted form out
        "secret\\value-abcdef",  # ... and so does a backslash
        '"quoted-value-abcdef"',
        "mix'ab$c-#=`! def",
        "  padded-abcdef  ",
        "both'quotes\"here",  # ' AND " -> the serializer refuses to write it at all
        "back\\slash'and-quote",  # ' AND \ -> likewise
    ],
)
def test_the_vouchable_spellings_are_the_ones_the_serializer_calls_safe(value: str) -> None:
    """``_dotenv_safe_spellings`` is the INSTALL serializer's own judgement, asked
    as a set instead of a preference (R7-1).

    Two properties, and together they are why the revise gate can demand EQUALITY
    without ever refusing a package this system produced:

    * whatever ``_dotenv_serialize_value`` would WRITE is in the set (so an
      installed ``.env`` passes), and the set is EMPTY exactly when it refuses to
      write the value at all (so a spelling install would never emit is never
      vouched for);
    * every admitted spelling parses BACK to the value through the runtime's own
      loader -- which is the security property itself: the raw line a builder can
      ``cat`` holds the registered value and nothing that decodes into it."""
    serialized = tool_builder._dotenv_serialize_value(value)
    spellings = tool_builder._dotenv_safe_spellings(value)

    if serialized is None:
        assert spellings == ()
    else:
        assert serialized in spellings
    for spelling in spellings:
        assert tools._parse_dotenv_text(f"K={spelling}\n") == {"K": value}
        assert value in spelling  # verbatim, which is what a redactor needs


def test_run_install_promotes_tricky_secret_round_trippable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F5 end-to-end: an install whose form secret carries dotenv-significant characters
    succeeds, and the promoted tool's ``.env`` parses the value back byte-for-byte with the
    runtime loader -- so the redaction registry and the tool's subprocess both see exactly
    what the user submitted."""
    root = tmp_path / "tools"
    _install_settings(monkeypatch, tools_dir=str(root))
    secret_value = "tricky'$#-value abcdef"  # single quote, $, #, space -- all significant
    _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "built", "ready": True},
        files={"tool.json": json.dumps(_package_manifest("kbsearch")), "run.py": _GOOD_RUN_PY},
    )
    _no_fetch(monkeypatch)

    outcome = asyncio.run(
        run_install(
            "http://kb.example/openapi.json",
            "build",
            secret_name="KB_API_KEY",
            secret_value=secret_value,
        )
    )
    assert outcome.ok is True
    loaded = tools._load_tool_dotenv(root / "kbsearch")
    assert loaded["KB_API_KEY"] == secret_value  # exact, no dotenv transform


def test_router_install_threads_secret_pair(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A valid secret pair is stripped, threaded to start_install_job, and never
    echoed back in the 202 response."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    seen: dict[str, Any] = {}

    def fake_start(
        openapi_url: str,
        instructions: str,
        *,
        secret_name: str | None = None,
        secret_value: str | None = None,
    ) -> str:
        seen["secret_name"] = secret_name
        seen["secret_value"] = secret_value
        return "job-xyz"

    monkeypatch.setattr("afterthread.services.tool_builder.start_install_job", fake_start)
    response = client.post(
        "/api/tools/install",
        json={
            "openapi_url": "http://kb.example/openapi.json",
            "instructions": "build",
            "secret_name": "  KB_API_KEY  ",
            "secret_value": "  the-secret-value  ",
        },
    )
    assert response.status_code == 202
    assert seen["secret_name"] == "KB_API_KEY"  # stripped
    assert seen["secret_value"] == "the-secret-value"  # stripped
    assert "the-secret-value" not in response.text  # never echoed back


# --- the OpenAPI fetch (a real local HTTP server) ------------------------------


class _DocHandler(BaseHTTPRequestHandler):
    """Serves a configurable body/status for the fetch tests."""

    body: bytes = b"{}"
    status: int = 200

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        if self.body:
            self.wfile.write(self.body)

    def log_message(self, format: str, *args: Any) -> None:
        return  # keep pytest output clean


@pytest.fixture
def doc_server() -> Generator[str]:
    """A local HTTP server; yields its base URL. Configure via _DocHandler."""
    _DocHandler.body = b"{}"
    _DocHandler.status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DocHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_openapi_happy(doc_server: str) -> None:
    _DocHandler.body = json.dumps({"openapi": "3.1.0"}).encode()
    text, error = asyncio.run(tool_builder._fetch_openapi(f"{doc_server}/openapi.json"))
    assert error is None
    assert text is not None and "3.1.0" in text


def test_fetch_openapi_http_error(doc_server: str) -> None:
    _DocHandler.status = 404
    text, error = asyncio.run(tool_builder._fetch_openapi(f"{doc_server}/openapi.json"))
    assert text is None
    assert error == "OpenAPI 文件下載失敗（HTTP 404）。"  # noqa: RUF001


def test_fetch_openapi_rejects_oversized_body(doc_server: str) -> None:
    """The declared Content-Length alone trips the cap -- the body is never
    read into memory, let alone into the prompt."""
    _DocHandler.body = b"x" * (tool_builder._OPENAPI_MAX_BYTES + 1)
    text, error = asyncio.run(tool_builder._fetch_openapi(f"{doc_server}/openapi.json"))
    assert text is None
    assert error == _ERROR_OPENAPI_TOO_LARGE


def test_fetch_openapi_connection_failure() -> None:
    """A connect failure is a friendly category error, never an exception."""
    # Bind-then-close guarantees a currently-unused port on loopback, so the
    # connect is refused immediately rather than timing out.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    text, error = asyncio.run(tool_builder._fetch_openapi(f"http://127.0.0.1:{port}/x"))
    assert text is None
    assert error is not None and error.startswith("OpenAPI 文件下載失敗")


def test_fetch_openapi_total_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole fetch is bounded by a TOTAL wall-clock deadline
    (_FETCH_TOTAL_TIMEOUT_SECONDS): a connection that hangs past it -- even one
    that would never trip httpx2's PER-PHASE inactivity timeout -- fails with the
    friendly fetch outcome rather than hanging the background job forever. Driven
    by a fake client whose stream never resolves, so the ONLY thing that can end
    the call is the asyncio.timeout wiring under test."""

    class _HangingStream:
        async def __aenter__(self) -> Any:
            await asyncio.sleep(3600)  # never resolves within the tiny deadline

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    class _HangingClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            return _HangingStream()

    monkeypatch.setattr(tool_builder.httpx2, "AsyncClient", _HangingClient)
    monkeypatch.setattr(tool_builder, "_FETCH_TOTAL_TIMEOUT_SECONDS", 0.2)

    started = time.monotonic()
    text, error = asyncio.run(tool_builder._fetch_openapi("http://kb.example/openapi.json"))
    elapsed = time.monotonic() - started

    assert text is None
    assert error is not None and error.startswith("OpenAPI 文件下載失敗")
    assert elapsed < 2.0  # bounded by the 0.2s total deadline, not the 3600s hang


def test_fetch_openapi_total_timeout_covers_client_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The total deadline (_FETCH_TOTAL_TIMEOUT_SECONDS) must bound client
    CONSTRUCTION too, not just the request: httpx2.AsyncClient(...) can do real
    synchronous work (TLS context / trust-store initialization), and a version
    that builds the client BEFORE entering asyncio.timeout would let that work
    run for free, silently extending the promised wall-clock bound. This is
    NARROWER than test_fetch_openapi_total_timeout above, which uses a fake
    whose __init__ is instant and so cannot tell "construction happened inside
    the deadline" apart from "construction happened before it" -- entering
    asyncio.timeout and then immediately calling client.__aenter__() looks
    identical from that fake's point of view either way. Only the CONSTRUCTOR
    call itself moved outside the timeout in the regression this pins, so the
    fake here burns real wall-clock time in __init__ (a plain, blocking
    time.sleep -- construction is ordinary synchronous code in production too,
    exactly like the trust-store read it stands in for, so faking it with an
    awaitable would test something asyncio.timeout never actually bounds)."""

    class _HangingStream:
        async def __aenter__(self) -> Any:
            await asyncio.sleep(3600)  # never resolves within the tiny deadline

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    class _SlowInitClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # Stands in for synchronous TLS/trust-store setup cost. Real
            # (blocking) time, on purpose -- see the docstring above.
            time.sleep(0.2)

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            return _HangingStream()

    monkeypatch.setattr(tool_builder.httpx2, "AsyncClient", _SlowInitClient)
    monkeypatch.setattr(tool_builder, "_FETCH_TOTAL_TIMEOUT_SECONDS", 0.3)

    started = time.monotonic()
    text, error = asyncio.run(tool_builder._fetch_openapi("http://kb.example/openapi.json"))
    elapsed = time.monotonic() - started

    assert text is None
    assert error is not None and error.startswith("OpenAPI 文件下載失敗")
    # If construction runs INSIDE the timeout scope (fixed): the 0.2s __init__
    # eats into the 0.3s budget, so the deadline still fires ~0.3s after this
    # call started. If construction runs BEFORE asyncio.timeout is entered (the
    # regression this pins): the deadline does not start counting until AFTER
    # those 0.2s, so elapsed balloons to ~0.5s -- comfortably over this bound.
    assert elapsed < 0.45


def test_fetch_openapi_total_timeout_preempts_client_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P7 review r2: the deadline must be able to PREEMPT construction mid-call,
    not merely start counting before it. A version that calls
    ``httpx2.AsyncClient(...)`` SYNCHRONOUSLY (even inside the asyncio.timeout
    scope) gives asyncio.timeout's scheduled cancellation no await point to
    land on -- the event loop is stuck running the blocking constructor to
    completion regardless of the deadline, so _fetch_openapi would only return
    AFTER it. This is what test_fetch_openapi_total_timeout_covers_client_setup
    above cannot distinguish: its 0.2s constructor is SHORTER than its 0.3s
    deadline, so both a preemptible and a merely-timed construction land on the
    same ~0.3s total. Here the constructor (0.6s, a plain blocking time.sleep --
    real synchronous work, exactly like the trust-store read it stands in for)
    is deliberately LONGER than the deadline (0.2s), so only a construction that
    actually runs on a worker thread -- an awaited call the deadline can cancel
    while the thread keeps blocking in the background -- can return before the
    0.6s is up. The hanging stream is never reached: construction alone already
    exceeds the deadline."""

    class _HangingStream:
        async def __aenter__(self) -> Any:
            await asyncio.sleep(3600)  # never reached

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    class _VerySlowInitClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # Longer than the deadline, on purpose -- see the docstring above.
            time.sleep(0.6)

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            return _HangingStream()

    monkeypatch.setattr(tool_builder.httpx2, "AsyncClient", _VerySlowInitClient)
    monkeypatch.setattr(tool_builder, "_FETCH_TOTAL_TIMEOUT_SECONDS", 0.2)

    started = time.monotonic()
    text, error = asyncio.run(tool_builder._fetch_openapi("http://kb.example/openapi.json"))
    elapsed = time.monotonic() - started

    assert text is None
    assert error is not None and error.startswith("OpenAPI 文件下載失敗")
    # Bounded by the 0.2s deadline, comfortably below the 0.6s constructor --
    # proves setup itself, not just the request after it, is now preemptible.
    assert elapsed < 0.45


def test_fetch_openapi_construction_timeout_is_single_threaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Repeated construction timeouts must NOT grow threads without bound.

    THE FINDING (empirically demonstrated): run_in_threadpool cancels only the
    AWAITER on a construction timeout, never the blocked pool thread, so every
    retry after a construction timeout grabbed a FRESH pool thread -- 5
    consecutive timeouts left 5 live "AnyIO worker thread"s (45 attempts -> 44
    workers, past the 40-token limiter). The single-flight construction worker
    bounds that to ONE dedicated daemon thread for the whole process: a hung
    constructor ties up exactly that worker, and each subsequent fetch QUEUES
    behind it and times out at its own deadline -- zero extra threads.

    Drives 5 consecutive _fetch_openapi calls whose fake client __init__ blocks
    well past the (tiny) total deadline. Asserts every call still returns the
    friendly timeout outcome on time AND that at most ONE new thread was created
    across all of them -- the dedicated worker, identified by its distinctive
    name prefix. THAT is the discriminator: under the reverted run_in_threadpool
    shape this same drive spawns ~5 anonymous pool threads (RED, len==5); here it
    spawns exactly one named worker (GREEN, len==1). The blocking __init__ is
    released in a finally so the worker returns to idle for the autouse drain."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    release = threading.Event()

    class _BlockingInitClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # Blocks PAST the tiny total deadline so construction always times
            # out; released in the finally below so the single worker frees.
            release.wait(timeout=30)

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("construction times out before stream is reached")

    monkeypatch.setattr(tool_builder.httpx2, "AsyncClient", _BlockingInitClient)
    monkeypatch.setattr(tool_builder, "_FETCH_TOTAL_TIMEOUT_SECONDS", 0.2)

    prefix = tool_builder._SETUP_THREAD_NAME
    before_idents = {t.ident for t in threading.enumerate()}

    async def drive() -> list[tuple[str | None, str | None, float]]:
        outcomes: list[tuple[str | None, str | None, float]] = []
        for _ in range(5):
            started = time.monotonic()
            text, error = await tool_builder._fetch_openapi("http://kb.example/openapi.json")
            outcomes.append((text, error, time.monotonic() - started))
        return outcomes

    try:
        outcomes = asyncio.run(drive())
        new_threads = [t for t in threading.enumerate() if t.ident not in before_idents]

        # Every call returns the friendly timeout outcome, bounded by the 0.2s
        # deadline -- never the 30s the constructor would otherwise block for.
        for text, error, elapsed in outcomes:
            assert text is None
            assert error is not None and error.startswith("OpenAPI 文件下載失敗")
            assert elapsed < 2.0

        # THE BOUND: however many constructions timed out, at most ONE new thread
        # was created, and it is the dedicated single-flight worker -- never the
        # unbounded fan-out of pool workers the finding produced.
        assert len(new_threads) <= 1, [t.name for t in new_threads]
        assert all(t.name.startswith(prefix) for t in new_threads), [t.name for t in new_threads]
    finally:
        release.set()


def test_fetch_openapi_queued_construction_cancelled_before_start_never_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fetch whose construction is still QUEUED behind a stuck one when its
    deadline fires has its future cancelled while PENDING, so the worker SKIPS it
    -- the constructor is NEVER called for it (invariant 3). This is what keeps a
    backlog of abandoned retries from constructing anything.

    A construction COUNTER proves it: the first fetch's construction reaches the
    worker and blocks (RUNNING, count 1); a second fetch queues behind it and
    times out while still PENDING (its future cancelled). After releasing the
    stuck one and draining the worker to the second (cancelled) job,
    set_running_or_notify_cancel() returns False for it, so the counter stays 1 --
    the second constructor was skipped, never run. (Under the reverted
    run_in_threadpool shape the second fetch would get its OWN pool thread and
    construct too, so the counter would reach 2: this asserts the queue-behind
    semantics, not per-call fan-out.)"""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    release = threading.Event()
    lock = threading.Lock()
    constructions = 0

    class _CountingBlockingClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            nonlocal constructions
            with lock:
                constructions += 1
            # Only the FIRST (worker-occupying) construction runs __init__; the
            # queued second one is skipped BEFORE __init__ is ever entered.
            release.wait(timeout=30)

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("construction times out before stream is reached")

    monkeypatch.setattr(tool_builder.httpx2, "AsyncClient", _CountingBlockingClient)
    monkeypatch.setattr(tool_builder, "_FETCH_TOTAL_TIMEOUT_SECONDS", 0.2)

    async def drive_two() -> None:
        # First: its construction reaches the worker and blocks (RUNNING).
        first_text, _ = await tool_builder._fetch_openapi("http://kb.example/first.json")
        # Second: its construction QUEUES behind the still-blocked first, and is
        # cancelled while PENDING when its own 0.2s deadline fires.
        second_text, _ = await tool_builder._fetch_openapi("http://kb.example/second.json")
        assert first_text is None and second_text is None

    try:
        asyncio.run(drive_two())
        with lock:
            assert constructions == 1  # only the stuck one ran; second still queued
        # Release the stuck construction and let the worker drain to the second
        # (cancelled) job: set_running_or_notify_cancel() returns False for it, so
        # it is SKIPPED -- the constructor count stays 1, never 2.
        release.set()
        tool_builder._drain_setup_worker_for_tests(timeout=5)
        with lock:
            assert constructions == 1
    finally:
        release.set()


def _capturing_async_client(captured: dict[str, Any]) -> type:
    """A fake httpx2.AsyncClient that records its constructor kwargs into
    ``captured`` and serves a canned ``{}`` 200 response -- exercises
    _fetch_openapi's TLS_NO_VERIFY wiring without a real network connection,
    mirroring test_fetch_openapi_total_timeout's fake-client pattern above."""

    class _FakeResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self.headers: dict[str, str] = {}

        async def aiter_bytes(self) -> Any:
            yield b"{}"

    class _FakeResponseCM:
        async def __aenter__(self) -> Any:
            return _FakeResponse()

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            return _FakeResponseCM()

    return _FakeClient


def test_fetch_openapi_tls_no_verify_off_omits_verify_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (flag off): the client is constructed with NO ``verify`` kwarg at
    all -- byte-identical to the arguments used before this flag existed."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(tool_builder.httpx2, "AsyncClient", _capturing_async_client(captured))
    _install_settings(monkeypatch, tls_no_verify=False)

    text, error = asyncio.run(tool_builder._fetch_openapi("http://kb.example/openapi.json"))

    assert error is None
    assert text == "{}"
    assert "verify" not in captured


def test_fetch_openapi_tls_no_verify_on_passes_verify_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on: the client is constructed with verify=False, on top of the SAME
    other arguments (timeout / follow_redirects / max_redirects) as always."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(tool_builder.httpx2, "AsyncClient", _capturing_async_client(captured))
    _install_settings(monkeypatch, tls_no_verify=True)

    text, error = asyncio.run(tool_builder._fetch_openapi("http://kb.example/openapi.json"))

    assert error is None
    assert text == "{}"
    assert captured.get("verify") is False
    assert captured.get("timeout") == tool_builder._FETCH_TIMEOUT_SECONDS
    assert captured.get("follow_redirects") is True
    assert captured.get("max_redirects") == tool_builder._FETCH_MAX_REDIRECTS


def test_run_install_fetch_failure_is_friendly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed fetch ends the run before staging or any LLM call."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))

    async def fake_fetch(url: str) -> tuple[str | None, str | None]:
        return None, "OpenAPI 文件下載失敗（ConnectError）。"  # noqa: RUF001

    monkeypatch.setattr("afterthread.services.tool_builder._fetch_openapi", fake_fetch)

    async def must_not_generate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the LLM must not be called when the fetch failed")

    monkeypatch.setattr("afterthread.services.tool_builder.generate_structured", must_not_generate)
    outcome = asyncio.run(run_install("http://kb.example/openapi.json", "build"))
    assert outcome.ok is False
    assert "OpenAPI 文件下載失敗" in (outcome.error or "")
    assert not (tmp_path / "tools" / ".staging").exists()


# --- jobs ---------------------------------------------------------------------


def test_job_state_machine_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """queued -> running -> succeeded, with the outcome's fields copied over."""

    async def scenario() -> None:
        release = asyncio.Event()

        async def fake_run_install(
            url: str,
            instructions: str,
            *,
            secret_name: str | None = None,
            secret_value: str | None = None,
        ) -> InstallOutcome:
            await release.wait()
            return InstallOutcome(ok=True, tool_name="kb", summary="done", llm_log_id=7)

        monkeypatch.setattr("afterthread.services.tool_builder.run_install", fake_run_install)
        job_id = tool_builder.start_install_job("http://x/openapi.json", "i")
        assert job_id is not None  # empty table -> this first submit is admitted
        job = tool_builder.get_job(job_id)
        assert job is not None and job["state"] in ("queued", "running")

        await asyncio.sleep(0)  # let the task reach its running update
        job = tool_builder.get_job(job_id)
        assert job is not None and job["state"] == "running"
        assert job["finished_at"] is None

        release.set()
        for _ in range(200):
            job = tool_builder.get_job(job_id)
            assert job is not None
            if job["state"] != "running":
                break
            await asyncio.sleep(0.01)
        assert job["state"] == "succeeded"
        assert job["tool_name"] == "kb"
        assert job["summary"] == "done"
        assert job["llm_log_id"] == 7
        assert job["error"] is None
        assert job["finished_at"] is not None

    asyncio.run(scenario())


def test_job_state_machine_failure_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def fake_run_install(
            url: str,
            instructions: str,
            *,
            secret_name: str | None = None,
            secret_value: str | None = None,
        ) -> InstallOutcome:
            return InstallOutcome(ok=False, error="工具包驗證失敗：missing tool.json")  # noqa: RUF001

        monkeypatch.setattr("afterthread.services.tool_builder.run_install", fake_run_install)
        job_id = tool_builder.start_install_job("http://x/openapi.json", "i")
        assert job_id is not None  # empty table -> this first submit is admitted
        for _ in range(200):
            job = tool_builder.get_job(job_id)
            assert job is not None
            if job["state"] not in ("queued", "running"):
                break
            await asyncio.sleep(0.01)
        assert job["state"] == "failed"
        assert "工具包驗證失敗" in (job["error"] or "")

    asyncio.run(scenario())


def test_job_unexpected_exception_becomes_failed_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug escaping run_install must not leave the job stuck on running --
    the task backstop records a failed state with a category-only error."""

    async def scenario() -> None:
        async def exploding(
            url: str,
            instructions: str,
            *,
            secret_name: str | None = None,
            secret_value: str | None = None,
        ) -> InstallOutcome:
            raise RuntimeError("bug with secrets in str()")

        monkeypatch.setattr("afterthread.services.tool_builder.run_install", exploding)
        job_id = tool_builder.start_install_job("http://x/openapi.json", "i")
        assert job_id is not None  # empty table -> this first submit is admitted
        for _ in range(200):
            job = tool_builder.get_job(job_id)
            assert job is not None
            if job["state"] not in ("queued", "running"):
                break
            await asyncio.sleep(0.01)
        assert job["state"] == "failed"
        assert "RuntimeError" in (job["error"] or "")
        assert "secrets" not in (job["error"] or "")  # category only, never str(exc)

    asyncio.run(scenario())


def test_jobs_bounded_to_most_recent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The table keeps only the newest _MAX_JOBS entries.

    Installs are single-flight (M7), so a run of jobs accumulates SEQUENTIALLY:
    each must reach a terminal state before the next is admitted. Drive
    _MAX_JOBS + 5 to completion and assert the oldest five were evicted while the
    newest stays pollable -- the eviction still matters for a long-lived process
    that runs many installs over its lifetime."""

    async def scenario() -> None:
        async def instant(
            url: str,
            instructions: str,
            *,
            secret_name: str | None = None,
            secret_value: str | None = None,
        ) -> InstallOutcome:
            return InstallOutcome(ok=True, tool_name="t")

        monkeypatch.setattr("afterthread.services.tool_builder.run_install", instant)
        ids: list[str] = []
        for _ in range(tool_builder._MAX_JOBS + 5):
            job_id = tool_builder.start_install_job("http://x/openapi.json", "i")
            assert job_id is not None  # the previous job has finished -> admitted
            ids.append(job_id)
            # Drain this job to terminal before the next submit (single-flight).
            for _ in range(200):
                job = tool_builder.get_job(job_id)
                if job is None or job["state"] not in ("queued", "running"):
                    break
                await asyncio.sleep(0.01)

        with tool_builder._JOBS_LOCK:
            assert len(tool_builder._JOBS) == tool_builder._MAX_JOBS
        # The oldest five fell off; the newest are still pollable.
        assert tool_builder.get_job(ids[0]) is None
        assert tool_builder.get_job(ids[-1]) is not None
        # Drain any spawned tasks so none outlives the test's loop.
        await asyncio.gather(*list(tool_builder._TASKS), return_exceptions=True)

    asyncio.run(scenario())


def test_start_install_job_single_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only ONE install may be queued/running at a time (M7): a second start
    while the first is active is refused (None), and a start is accepted again
    once the first reaches a terminal state."""

    async def scenario() -> None:
        release = asyncio.Event()

        async def fake_run_install(
            url: str,
            instructions: str,
            *,
            secret_name: str | None = None,
            secret_value: str | None = None,
        ) -> InstallOutcome:
            await release.wait()
            return InstallOutcome(ok=True, tool_name="kb")

        monkeypatch.setattr("afterthread.services.tool_builder.run_install", fake_run_install)
        first = tool_builder.start_install_job("http://x/openapi.json", "i")
        assert first is not None
        await asyncio.sleep(0)  # let the task reach its running update

        # A second submit while the first is active is refused outright.
        assert tool_builder.start_install_job("http://x/openapi.json", "i") is None

        release.set()
        job: dict[str, Any] | None = None
        for _ in range(200):
            job = tool_builder.get_job(first)
            assert job is not None
            if job["state"] != "running":
                break
            await asyncio.sleep(0.01)
        assert job is not None and job["state"] == "succeeded"

        # Now the first is terminal, so a fresh submit is accepted again.
        second = tool_builder.start_install_job("http://x/openapi.json", "i")
        assert second is not None and second != first
        await asyncio.gather(*list(tool_builder._TASKS), return_exceptions=True)

    asyncio.run(scenario())


# --- router -------------------------------------------------------------------


def test_router_list_tools(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    pkg = root / "kbsearch"
    pkg.mkdir(parents=True)
    (pkg / "run.py").write_text("print('x')\n")
    (pkg / "tool.json").write_text(json.dumps(_package_manifest("kbsearch")))
    _install_settings(monkeypatch, tools_dir=str(root))

    response = client.get("/api/tools")
    assert response.status_code == 200
    assert response.json() == {
        "tools": [
            {
                "name": "kbsearch",
                "description": "test-built tool",
                "enabled": True,
                "valid": True,
                "error": None,
                # D40: null here means "no readable summary sidecar" -- this
                # hand-made package has none.
                "summary_status": None,
            }
        ]
    }


def test_router_list_tools_carries_summary_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The listing badges every row from its own sidecar, so the 工具 page needs
    no per-tool summary request to render the list."""
    root = tmp_path / "tools"
    pkg = root / "kbsearch"
    pkg.mkdir(parents=True)
    (pkg / "run.py").write_text("print('x')\n")
    (pkg / "tool.json").write_text(json.dumps(_package_manifest("kbsearch")))
    _install_settings(monkeypatch, tools_dir=str(root))
    _write_meta(pkg, summary="說明", status="final")

    body = client.get("/api/tools").json()
    assert [(row["name"], row["summary_status"]) for row in body["tools"]] == [
        ("kbsearch", "final")
    ]


def test_router_list_tools_empty_when_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_settings(monkeypatch, tools_dir="")
    response = client.get("/api/tools")
    assert response.status_code == 200
    assert response.json() == {"tools": []}


def test_router_patch_toggles_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    pkg = root / "kbsearch"
    pkg.mkdir(parents=True)
    (pkg / "run.py").write_text("print('x')\n")
    (pkg / "tool.json").write_text(json.dumps(_package_manifest("kbsearch")))
    _install_settings(monkeypatch, tools_dir=str(root))

    response = client.patch("/api/tools/kbsearch", json={"enabled": False})
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "kbsearch"
    assert body["enabled"] is False
    # Persisted, not just echoed: a fresh GET shows the same state.
    assert client.get("/api/tools").json()["tools"][0]["enabled"] is False


def test_router_patch_unknown_tool_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    response = client.patch("/api/tools/ghost", json={"enabled": True})
    assert response.status_code == 404
    assert response.json() == {"detail": "Tool not found"}


@pytest.mark.parametrize("bad_name", ["UPPER", "bad name", ".hidden"])
def test_router_rejects_invalid_names_as_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_name: str
) -> None:
    """An out-of-shape name fails PATH validation (the registry's regex,
    mirrored at the path layer) before any handler runs."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    assert client.patch(f"/api/tools/{bad_name}", json={"enabled": True}).status_code == 422
    assert client.delete(f"/api/tools/{bad_name}").status_code == 422


def test_router_delete_tool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    pkg = root / "kbsearch"
    pkg.mkdir(parents=True)
    (pkg / "run.py").write_text("print('x')\n")
    (pkg / "tool.json").write_text(json.dumps(_package_manifest("kbsearch")))
    _install_settings(monkeypatch, tools_dir=str(root))

    response = client.delete("/api/tools/kbsearch")
    assert response.status_code == 204
    assert not pkg.exists()
    assert client.delete("/api/tools/kbsearch").status_code == 404


def test_router_install_503_when_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_settings(monkeypatch, tools_dir="")
    response = client.post(
        "/api/tools/install",
        json={"openapi_url": "http://kb.example/openapi.json", "instructions": "build"},
    )
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "tools_not_configured"
    assert "message" in detail


def test_router_install_202_queues_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    seen: dict[str, str] = {}

    def fake_start(
        openapi_url: str,
        instructions: str,
        *,
        secret_name: str | None = None,
        secret_value: str | None = None,
    ) -> str:
        seen["url"] = openapi_url
        seen["instructions"] = instructions
        return "job-abc"

    monkeypatch.setattr("afterthread.services.tool_builder.start_install_job", fake_start)
    response = client.post(
        "/api/tools/install",
        json={"openapi_url": "http://kb.example/openapi.json", "instructions": "  build it  "},
    )
    assert response.status_code == 202
    assert response.json() == {"job_id": "job-abc"}
    assert seen["url"] == "http://kb.example/openapi.json"
    assert seen["instructions"] == "build it"  # request-layer strip applied


def test_router_install_409_when_active(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second install submit while one is already queued/running is a 409 with
    the fixed install_in_progress detail (M7). Driven through the REAL
    start_install_job with a pre-seeded active job, so the single-flight gate
    itself produces the conflict. (The 'accepted again after it finishes' half
    is covered by test_start_install_job_single_flight's real-gate transition.)"""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    with tool_builder._JOBS_LOCK:
        tool_builder._JOBS["active"] = tool_builder.InstallJob(
            job_id="active", state="running", created_at="2026-07-16T00:00:00+00:00"
        )
    response = client.post(
        "/api/tools/install",
        json={"openapi_url": "http://kb.example/openapi.json", "instructions": "build"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "install_in_progress"
    assert "message" in detail


@pytest.mark.parametrize(
    "payload",
    [
        {"openapi_url": "ftp://kb.example/spec", "instructions": "x"},
        {"openapi_url": "not a url", "instructions": "x"},
        {"openapi_url": "http://kb.example/openapi.json", "instructions": "   "},
        {"openapi_url": "http://kb.example/openapi.json", "instructions": "x" * 20001},
        {"openapi_url": "http://kb.example/openapi.json"},
        # D36 secret pair: an invalid env-var name and a half-supplied pair both 422.
        {
            "openapi_url": "http://kb.example/openapi.json",
            "instructions": "x",
            "secret_name": "kb_key",
            "secret_value": "v",
        },
        {
            "openapi_url": "http://kb.example/openapi.json",
            "instructions": "x",
            "secret_name": "KB_KEY",
        },
    ],
    ids=[
        "non-http-scheme",
        "not-a-url",
        "blank-instructions",
        "oversized",
        "missing-field",
        "bad-secret-name",
        "half-secret-pair",
    ],
)
def test_router_install_validates_request(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: dict[str, Any],
) -> None:
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    assert client.post("/api/tools/install", json=payload).status_code == 422


def test_router_install_422_rejects_short_secret_value(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F3: a secret value under the 6-char redactor floor is a 422 with the fixed
    zh-TW message -- accepting one would create a secret the redactor could never
    mask (it skips <6-char values)."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    response = client.post(
        "/api/tools/install",
        json={
            "openapi_url": "http://kb.example/openapi.json",
            "instructions": "build",
            "secret_name": "KB_API_KEY",
            "secret_value": "abc",  # 3 chars, under the floor
        },
    )
    assert response.status_code == 422
    assert "秘密值長度至少 6 字元" in response.text


def test_router_install_422_does_not_echo_secret_value(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F2: a request whose secret pair fails validation must NOT echo secret_value
    back in the 422 body. FastAPI's default handler puts the whole request body in
    each error's ``input`` (a model-level validator's input IS the body); the
    app-level handler strips every ``input`` key, so the value never rides back --
    while loc/msg/type survive so FE error handling is unaffected."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    secret = "topsecret-should-not-echo-abcdef"
    response = client.post(
        "/api/tools/install",
        json={
            "openapi_url": "http://kb.example/openapi.json",
            "instructions": "build",
            "secret_name": "kb_key",  # invalid env-var name -> model validator 422
            "secret_value": secret,
        },
    )
    assert response.status_code == 422
    assert secret not in response.text  # the value never echoed back
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail  # still the default list-of-errors shape
    assert "input" not in json.dumps(detail)  # the echoing key is gone at every depth
    # The rest of each error's shape is intact, so existing FE handling still works.
    assert all("loc" in err and "msg" in err and "type" in err for err in detail)


def test_router_job_status_and_404(client: TestClient) -> None:
    job = tool_builder.InstallJob(
        job_id="job-1",
        state="succeeded",
        created_at="2026-07-16T00:00:00+00:00",
        finished_at="2026-07-16T00:01:00+00:00",
        tool_name="kb",
        summary="done",
        llm_log_id=3,
    )
    with tool_builder._JOBS_LOCK:
        tool_builder._JOBS[job.job_id] = job

    response = client.get("/api/tools/jobs/job-1")
    assert response.status_code == 200
    assert response.json() == {
        "job_id": "job-1",
        "state": "succeeded",
        "created_at": "2026-07-16T00:00:00+00:00",
        "finished_at": "2026-07-16T00:01:00+00:00",
        "error": None,
        "tool_name": "kb",
        "summary": "done",
        "llm_log_id": 3,
    }

    missing = client.get("/api/tools/jobs/ghost")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Tool job not found"}

    # The pre-D40 spelling is GONE, not aliased (FE and backend ship in one
    # wheel, so there is no version skew for an alias to protect): it matches no
    # route at all now, hence Starlette's own 404 rather than the handler's.
    assert client.get("/api/tools/install/job-1").status_code == 404


# --- router: AI summary (D40) --------------------------------------------------


def _write_meta(pkg: Path, **fields: Any) -> bool:
    """``write_tool_meta`` with the ``updated_at`` every real caller supplies.

    The writer refuses a meta without a string ``updated_at`` (it will not invent
    a timestamp on a caller's behalf), so the route tests seed a sidecar through
    here and state only the fields under test."""
    return tools.write_tool_meta(pkg, {"updated_at": "2026-01-01T00:00:00+00:00", **fields})


def _meta(pkg: Path) -> dict[str, Any]:
    """The package's sidecar, asserted present (it is what the test just wrote)."""
    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    return meta


def _seed_package(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str = "kbsearch") -> Path:
    """An installed package (no sidecar) with the settings pointed at it."""
    root = tmp_path / "tools"
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "run.py").write_text("print('x')\n")
    (pkg / "tool.json").write_text(json.dumps(_package_manifest(name)))
    _install_settings(monkeypatch, tools_dir=str(root))
    return pkg


def test_router_get_summary_all_null_without_a_sidecar(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tool with no summary yet is a 200 of nulls, not a 404: the TOOL exists,
    it just has nothing to show, and the page renders 尚無總結 for it."""
    _seed_package(monkeypatch, tmp_path)
    response = client.get("/api/tools/kbsearch/summary")
    assert response.status_code == 200
    assert response.json() == {
        "summary": None,
        "status": None,
        "updated_at": None,
        "llm_log_id": None,
    }


def test_router_get_summary_returns_the_sidecar(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pkg = _seed_package(monkeypatch, tmp_path)
    tools.write_tool_meta(
        pkg,
        {
            "summary": "這個工具會查 KB",
            "status": "draft",
            "updated_at": "2026-07-26T00:00:00+00:00",
            "llm_log_id": 7,
            # The token a real store stamps beside the id (store_summary_meta).
            # Without it the route nulls the link, because a stored id only names
            # a record while the process that minted it is alive -- see the two
            # tests below for both halves of that.
            "llm_log_process": llm_log.process_token(),
            "origin": {"openapi_url": "http://kb.example/o.json", "instructions": "查 KB"},
        },
    )

    response = client.get("/api/tools/kbsearch/summary")
    assert response.status_code == 200
    # The response carries the four display fields ONLY -- `origin` is install
    # context for the next AI session, not something the UI shows.
    assert response.json() == {
        "summary": "這個工具會查 KB",
        "status": "draft",
        "updated_at": "2026-07-26T00:00:00+00:00",
        "llm_log_id": 7,
    }


def test_router_get_summary_drops_a_log_link_a_restart_invalidated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stored ``llm_log_id`` stops being offered once the process that minted it
    is gone -- and everything else about the summary is untouched.

    Log ids are a per-process counter over a ring that is wiped on restart, while
    the sidecar keeps the integer forever. After a restart the SAME id resolves to
    whatever interaction now holds it: a different tool's summary session, or a
    different workflow entirely. Nothing downstream can notice, because the deep
    link SELECTS the row by that id, so the detail and the row agree with each
    other -- the existing started_at staleness hint cannot help.

    The restart is produced honestly rather than by mocking the route:
    ``llm_log._reset_for_tests`` drops the ring, restarts ids at 0 and re-mints
    the process token, which is exactly what a restart is from this module's
    point of view."""
    pkg = _seed_package(monkeypatch, tmp_path)
    _write_meta(
        pkg,
        summary="這個工具會查 KB",
        status="draft",
        llm_log_id=3,
        llm_log_process=llm_log.process_token(),
    )
    assert client.get("/api/tools/kbsearch/summary").json()["llm_log_id"] == 3

    llm_log._reset_for_tests()  # a restart: same sidecar, a new id space

    body = client.get("/api/tools/kbsearch/summary").json()
    assert body["llm_log_id"] is None
    assert body["summary"] == "這個工具會查 KB"  # only the link is withheld
    assert body["status"] == "draft"
    # The id is still on disk -- this is a READ-side judgement, not a rewrite.
    assert _meta(pkg)["llm_log_id"] == 3


def test_router_get_summary_treats_a_tokenless_sidecar_as_foreign(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sidecar with no token at all -- every one written before the field
    existed -- reads as "not from this process".

    That is the conservative direction and the only defensible one: a file
    outliving the process that wrote it is the whole premise, so guessing
    "current" is the answer that produces a wrong link. The next regenerate
    re-stamps id and token together."""
    pkg = _seed_package(monkeypatch, tmp_path)
    _write_meta(pkg, summary="舊格式的側檔", status="draft", llm_log_id=3)

    body = client.get("/api/tools/kbsearch/summary").json()
    assert body["llm_log_id"] is None
    assert body["summary"] == "舊格式的側檔"


def test_router_get_summary_degrades_a_hand_edited_sidecar(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sidecar sits in a package the operator may hand-edit, so out-of-shape
    values render as nulls rather than 500ing the read."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / tools._AI_META_FILENAME).write_text(
        json.dumps({"summary": 12, "status": "published", "updated_at": [], "llm_log_id": "three"}),
        encoding="utf-8",
    )

    response = client.get("/api/tools/kbsearch/summary")
    assert response.status_code == 200
    assert response.json() == {
        "summary": None,
        "status": None,
        "updated_at": None,
        "llm_log_id": None,
    }


def test_router_summary_survives_a_lone_surrogate_in_the_sidecar(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hand-edited ``"\\ud800"`` broke BOTH summary verbs, and neither failure
    was the fixed, honest answer the routes promise.

    ``\\ud800`` is a legal JSON escape, so ``json.loads`` happily produces a
    ``str`` for it -- one that is NOT UTF-8 encodable. On the GET that value rode
    into ``ToolSummaryDetail`` and raised ``UnicodeEncodeError`` inside
    Starlette's strict ``JSONResponse.render``: a 500 out of the one route whose
    whole job is to DEGRADE a corrupt sidecar. On the PATCH the write path's
    byte-size ``.encode("utf-8")`` sat outside the fail-closed try, so the same
    value 500'd a request whose contract is False -> 404.

    Both boundaries scrub now, so the GET renders U+FFFD and the PATCH behaves
    like any other finalize. The read-side scrub is not redundant with the write
    side: this file never passed through our writer."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / tools._AI_META_FILENAME).write_text(
        json.dumps(
            {
                "summary": "a\ud800b",
                "status": "draft",
                "updated_at": "2026-07-26T00:00:00+00:00",
                "llm_log_id": 7,
                "llm_log_process": llm_log.process_token(),
            }
        ),
        encoding="utf-8",
    )

    response = client.get("/api/tools/kbsearch/summary")
    assert response.status_code == 200
    body = response.json()
    # One surrogate scrubs to THREE U+FFFD (surrogatepass gives three bytes, the
    # replace-decode substitutes per byte) -- asserted structurally rather than by
    # a literal count, since the ratio is the helper's business, not the route's.
    assert body["summary"].startswith("a")
    assert body["summary"].endswith("b")
    assert "�" in body["summary"]
    assert "\ud800" not in body["summary"]
    assert body["status"] == "draft"
    assert body["llm_log_id"] == 7

    # ... and the finalize path, whose summary is non-empty after the scrub, is a
    # plain 200 rather than a 500 (or a 409 for a summary that is really there).
    patched = client.patch("/api/tools/kbsearch/summary", json={"status": "final"})
    assert patched.status_code == 200
    assert patched.json()["status"] == "final"
    assert client.get("/api/tools/kbsearch/summary").json()["status"] == "final"
    # The sidecar the PATCH rewrote is now real UTF-8 on disk.
    (pkg / tools._AI_META_FILENAME).read_text(encoding="utf-8").encode("utf-8")


def test_router_get_summary_404_for_unknown_tool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    response = client.get("/api/tools/ghost/summary")
    assert response.status_code == 404
    assert response.json() == {"detail": "Tool not found"}


def test_router_get_summary_404_when_feature_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOOLS_DIR unset resolves every name to None, so the feature being off is
    the SAME 404 -- which is why these routes declare no tools_not_configured."""
    _install_settings(monkeypatch, tools_dir="")
    assert client.get("/api/tools/kbsearch/summary").status_code == 404


def test_router_patch_summary_finalizes_and_unfinalizes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pkg = _seed_package(monkeypatch, tmp_path)
    _write_meta(
        pkg,
        summary="說明",
        status="draft",
        llm_log_id=4,
        llm_log_process=llm_log.process_token(),
    )

    response = client.patch("/api/tools/kbsearch/summary", json={"status": "final"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "final"
    assert body["summary"] == "說明"  # the summary is preserved by the flip
    assert body["llm_log_id"] == 4
    # Persisted, not just echoed -- and visible on the listing too.
    assert client.get("/api/tools/kbsearch/summary").json()["status"] == "final"
    assert client.get("/api/tools").json()["tools"][0]["summary_status"] == "final"

    assert client.patch("/api/tools/kbsearch/summary", json={"status": "draft"}).status_code == 200
    assert client.get("/api/tools/kbsearch/summary").json()["status"] == "draft"


def test_router_patch_summary_409_without_a_sidecar(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing to freeze is a conflict, not a 404 -- the tool itself is fine."""
    _seed_package(monkeypatch, tmp_path)
    response = client.patch("/api/tools/kbsearch/summary", json={"status": "final"})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "summary_missing"
    assert "message" in detail


@pytest.mark.parametrize("summary", ["", "   ", None], ids=["empty", "blank", "null"])
def test_router_patch_summary_409_when_the_summary_is_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, summary: str | None
) -> None:
    """Freezing an absent explanation is the same "nothing there" as having no
    sidecar, so it is the SAME 409 -- and it is not harmless: a finalized empty
    summary then blocks 重新產生 with tool_finalized, so the one action that
    could fill it is refused until the user thinks to un-finalize."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / tools._AI_META_FILENAME).write_text(
        json.dumps({"summary": summary, "status": "draft"}), encoding="utf-8"
    )

    response = client.patch("/api/tools/kbsearch/summary", json={"status": "final"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "summary_missing"
    assert client.get("/api/tools/kbsearch/summary").json()["status"] == "draft"


def test_router_patch_summary_can_always_unfinalize(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """解除定版 is the escape hatch, so it is never gated on content -- including
    for a sidecar finalized empty before that gate existed."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / tools._AI_META_FILENAME).write_text(
        json.dumps({"summary": "", "status": "final"}), encoding="utf-8"
    )

    assert client.patch("/api/tools/kbsearch/summary", json={"status": "draft"}).status_code == 200
    assert client.get("/api/tools/kbsearch/summary").json()["status"] == "draft"


def test_router_patch_summary_404_for_unknown_tool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    response = client.patch("/api/tools/ghost/summary", json={"status": "final"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Tool not found"}


@pytest.mark.parametrize(
    "payload", [{"status": "published"}, {"status": ""}, {}], ids=["unknown", "empty", "missing"]
)
def test_router_patch_summary_422_on_bad_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict[str, Any]
) -> None:
    """The Literal is the gate: an unknown status never reaches the registry."""
    _seed_package(monkeypatch, tmp_path)
    assert client.patch("/api/tools/kbsearch/summary", json=payload).status_code == 422


def test_router_regenerate_summary_stores_and_returns_the_new_summary(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Driven through the REAL tool_meta.regenerate_summary (only the LLM call
    is stubbed), so the whole synchronous path -- prompt, sanitize, sidecar
    write -- runs for this route."""
    pkg = _seed_package(monkeypatch, tmp_path)
    _write_meta(pkg, summary="舊的", status="draft", origin={"instructions": "查 KB"})
    _fake_summary_generate(monkeypatch, summary="新的說明")

    response = client.post("/api/tools/kbsearch/summary/regenerate")
    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == "新的說明"
    assert body["status"] == "draft"
    assert body["llm_log_id"] is not None
    stored = tools.read_tool_meta(pkg)
    assert stored is not None
    assert stored["summary"] == "新的說明"
    # Inherited from the install, in the narrowed shape the writer stores (both
    # known fields, the absent one an explicit null).
    assert stored["origin"] == {"openapi_url": None, "instructions": "查 KB"}


def test_router_regenerate_summary_409_when_finalized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """定版 means the AI stops iterating on this tool: the regenerate is refused
    before the LLM is ever touched."""
    pkg = _seed_package(monkeypatch, tmp_path)
    _write_meta(pkg, summary="定版的說明", status="final")

    async def must_not_generate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a finalized tool must never reach the LLM")

    monkeypatch.setattr("afterthread.services.tool_meta.generate_structured", must_not_generate)

    response = client.post("/api/tools/kbsearch/summary/regenerate")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "tool_finalized"
    assert "message" in detail
    assert _meta(pkg)["summary"] == "定版的說明"  # untouched


def test_router_regenerate_summary_409_when_finalized_mid_generation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The THIRD 409-able outcome, and the one the up-front gate cannot give.

    That gate runs before an await that lasts as long as an LLM round trip; a
    PATCH landing inside the window used to have its frozen summary overwritten
    anyway (the status was preserved, the TEXT was not). The store re-checks and
    refuses, and the route answers the SAME tool_finalized code -- one refusal,
    whichever side of the await the 定版 arrived on.

    The finalize is driven THROUGH THE ROUTE from inside the stubbed generation,
    which is what a concurrent PATCH actually is."""
    pkg = _seed_package(monkeypatch, tmp_path)
    _write_meta(pkg, summary="定版的說明", status="draft")
    frozen: dict[str, bytes] = {}

    def finalize_mid_call() -> None:
        assert (
            client.patch("/api/tools/kbsearch/summary", json={"status": "final"}).status_code == 200
        )
        frozen["bytes"] = (pkg / tools._AI_META_FILENAME).read_bytes()

    _fake_summary_generate(monkeypatch, summary="新的說明", side_effect=finalize_mid_call)

    response = client.post("/api/tools/kbsearch/summary/regenerate")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "tool_finalized"
    # Byte-for-byte what 定版 froze: the in-flight generation left no trace.
    assert (pkg / tools._AI_META_FILENAME).read_bytes() == frozen["bytes"]
    assert _meta(pkg)["summary"] == "定版的說明"


def test_router_regenerate_summary_409_while_a_job_runs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A queued/running job may be moving a package directory into place, so a
    regenerate is refused for the duration. Driven through the REAL
    any_job_active with a pre-seeded active job."""
    _seed_package(monkeypatch, tmp_path)
    with tool_builder._JOBS_LOCK:
        tool_builder._JOBS["active"] = tool_builder.InstallJob(
            job_id="active", state="running", created_at="2026-07-16T00:00:00+00:00"
        )

    async def must_not_generate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no summary session may start while a job is active")

    monkeypatch.setattr("afterthread.services.tool_meta.generate_structured", must_not_generate)

    response = client.post("/api/tools/kbsearch/summary/regenerate")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "tool_job_in_progress"
    assert "message" in detail


def test_router_regenerate_summary_holds_the_single_flight_across_the_llm_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A regenerate in flight REFUSES a revise and an install for its duration
    (R7-3), and gives the reservation back when it finishes.

    The old gate read ``any_job_active()`` and then awaited a full LLM round trip
    -- a check-then-act whose window is one LLM call wide. A revise admitted
    inside it replaced the whole package and wrote its own fresh sidecar, which
    this older generation then overwrote with a summary built from the REPLACED
    package's contents: the newer, correct sidecar silently lost to the older
    one. Now the route TAKES a reservation in the same lock ``_admit_job`` uses,
    so nothing can be admitted while it runs.

    Both submits are driven from INSIDE the stubbed generation, which is exactly
    where the race lived; the assertions after the response pin the release, so
    the reservation cannot wedge the single flight for the life of the process."""
    pkg = _seed_package(monkeypatch, tmp_path)
    _write_meta(pkg, summary="舊的", status="draft")
    refusals: dict[str, Any] = {}

    def submit_work_mid_generation() -> None:
        revise = client.post("/api/tools/kbsearch/revise", json={"feedback": "加上分頁"})
        install = client.post(
            "/api/tools/install",
            json={"openapi_url": "https://kb.example/openapi.json", "instructions": "裝一個"},
        )
        refusals.update(
            revise_status=revise.status_code,
            revise_code=revise.json()["detail"]["code"],
            install_status=install.status_code,
            install_code=install.json()["detail"]["code"],
        )

    _fake_summary_generate(monkeypatch, summary="新的說明", side_effect=submit_work_mid_generation)

    response = client.post("/api/tools/kbsearch/summary/regenerate")

    assert response.status_code == 200
    assert response.json()["summary"] == "新的說明"
    assert refusals["revise_status"] == 409
    assert refusals["revise_code"] == "tool_job_in_progress"
    assert refusals["install_status"] == 409
    assert refusals["install_code"] == "install_in_progress"
    assert tool_builder._JOBS == {}  # neither submit left a job behind
    # Released on the way out, so the next request is admitted normally.
    assert tool_builder.any_job_active() is False
    assert tool_builder._admit_job() is not None


def test_router_regenerate_summary_releases_the_reservation_on_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reservation is released on the EXCEPTION paths too (R7-3).

    An upstream LLM failure leaves the route raising a 502 from inside the ``try``
    -- the one shape where a reservation released only on the happy path would be
    held forever, wedging every install, revise and regenerate for the life of
    the process. The ``finally`` covers it, and the follow-up regenerate below is
    the proof: it is admitted, and it succeeds."""
    pkg = _seed_package(monkeypatch, tmp_path)
    _write_meta(pkg, summary="先前的好總結", status="draft")
    _fake_summary_generate(
        monkeypatch, explode=LLMUpstreamError("APIConnectionError: could not reach the endpoint")
    )

    assert client.post("/api/tools/kbsearch/summary/regenerate").status_code == 502
    assert tool_builder.any_job_active() is False

    _fake_summary_generate(monkeypatch, summary="這次成功了")
    retry = client.post("/api/tools/kbsearch/summary/regenerate")
    assert retry.status_code == 200
    assert retry.json()["summary"] == "這次成功了"
    assert tool_builder.any_job_active() is False


def test_router_regenerate_summary_404_for_unknown_tool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    response = client.post("/api/tools/ghost/summary/regenerate")
    assert response.status_code == 404
    assert response.json() == {"detail": "Tool not found"}


def test_router_regenerate_summary_503_when_llm_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Synchronous AI op, so it degrades like capture/enrich do -- the SHARED
    llm_not_configured 503, and the previous summary survives."""
    pkg = _seed_package(monkeypatch, tmp_path)
    _write_meta(pkg, summary="先前的好總結", status="draft")
    _fake_summary_generate(monkeypatch, explode=LLMNotConfiguredError("off"))

    response = client.post("/api/tools/kbsearch/summary/regenerate")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "llm_not_configured"
    assert "message" in detail
    assert _meta(pkg)["summary"] == "先前的好總結"  # never clobbered


def test_router_regenerate_summary_502_on_upstream_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pkg = _seed_package(monkeypatch, tmp_path)
    _write_meta(pkg, summary="先前的好總結", status="draft")
    _fake_summary_generate(
        monkeypatch,
        explode=LLMUpstreamError("APIConnectionError: could not reach the LLM endpoint"),
    )

    response = client.post("/api/tools/kbsearch/summary/regenerate")
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "llm_upstream_error"
    assert detail["message"].startswith("APIConnectionError: ")
    assert _meta(pkg)["summary"] == "先前的好總結"  # never clobbered


def test_router_summary_routes_reject_invalid_names_as_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The summary routes share the path-layer name regex with the rest."""
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    for bad_name in ("UPPER", "bad name", ".hidden"):
        assert client.get(f"/api/tools/{bad_name}/summary").status_code == 422
        assert (
            client.patch(f"/api/tools/{bad_name}/summary", json={"status": "final"}).status_code
            == 422
        )
        assert client.post(f"/api/tools/{bad_name}/summary/regenerate").status_code == 422


def test_router_summary_routes_refuse_an_internal_alias(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An INTERNAL alias tools/<alias> -> tools/<real> resolves INSIDE the tools
    root, so resolve-then-contain PASSES and every by-name summary operation
    would silently act on the REAL package -- a PATCH freezing its summary, a
    regenerate spending an LLM session rewriting it, a REVISE replacing it
    wholesale. All four refuse (404) and the real sidecar is left byte-for-byte
    as it was.

    This is the same hard-block set_enabled/delete_tool have carried since H3,
    now shared by every summary/revise path through one resolver."""
    pkg = _seed_package(monkeypatch, tmp_path, "real")
    (pkg.parent / "alias").symlink_to(pkg, target_is_directory=True)
    _write_meta(pkg, summary="真的說明", status="draft")
    before = (pkg / tools._AI_META_FILENAME).read_bytes()

    async def must_not_generate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no session may start for an aliased package")

    def must_not_start(name: str, feedback: str) -> str:
        raise AssertionError("no revise job may start for an aliased package")

    monkeypatch.setattr("afterthread.services.tool_meta.generate_structured", must_not_generate)
    monkeypatch.setattr("afterthread.services.tool_builder.start_revise_job", must_not_start)

    assert client.get("/api/tools/alias/summary").status_code == 404
    assert client.patch("/api/tools/alias/summary", json={"status": "final"}).status_code == 404
    assert client.post("/api/tools/alias/summary/regenerate").status_code == 404
    assert client.post("/api/tools/alias/revise", json={"feedback": "改"}).status_code == 404

    assert (pkg / tools._AI_META_FILENAME).read_bytes() == before
    assert _meta(pkg)["status"] == "draft"  # the real package was never finalized


def test_router_regenerate_summary_404_when_the_sidecar_write_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A generation that produced text but persisted NOTHING (a racing delete
    hitting the ghost guard, a FIFO/symlink swapped in for the sidecar, an
    unwritable directory) must not answer 200 with a summary the next GET will
    not find. Same did-not-happen fold the PATCH uses for its failed rewrite."""
    pkg = _seed_package(monkeypatch, tmp_path)
    _write_meta(pkg, summary="先前的好總結", status="draft")
    _fake_summary_generate(monkeypatch, summary="新的說明")
    monkeypatch.setattr(tools, "write_tool_meta", lambda *args, **kwargs: False)

    response = client.post("/api/tools/kbsearch/summary/regenerate")
    assert response.status_code == 404
    assert response.json() == {"detail": "Tool not found"}


def test_router_a_tool_named_jobs_can_serve_its_summary(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`jobs` is a legal package name, and the job poll is `GET /jobs/{job_id}`
    -- so if it were declared FIRST, `/api/tools/jobs/summary` would match the
    JOB route with job_id="summary" and that tool's summary would be unreachable.

    D40's rename MOVED this collision from `install` to `jobs`; it did not remove
    it, since any literal first segment is a name a package could have. The
    summary routes are therefore still declared first, which is safe in both
    directions: a job id is uuid4().hex, so the literal segment "summary" can
    never be one."""
    pkg = _seed_package(monkeypatch, tmp_path, "jobs")
    _write_meta(pkg, summary="名叫 jobs 的工具", status="draft")

    response = client.get("/api/tools/jobs/summary")
    assert response.status_code == 200
    assert response.json()["summary"] == "名叫 jobs 的工具"
    assert response.json()["status"] == "draft"

    # ... and a real (uuid4().hex-shaped) job id still reaches the job handler.
    poll = client.get("/api/tools/jobs/0123456789abcdef0123456789abcdef")
    assert poll.status_code == 404
    assert poll.json() == {"detail": "Tool job not found"}

    # The other two summary verbs address the same tool, not the job route.
    assert client.patch("/api/tools/jobs/summary", json={"status": "final"}).status_code == 200
    assert _meta(pkg)["status"] == "final"


def test_any_job_active_tracks_the_job_table() -> None:
    """The single-flight predicate over the job table: any queued/running job, and
    a terminal one never blocks."""
    assert tool_builder.any_job_active() is False
    with tool_builder._JOBS_LOCK:
        tool_builder._JOBS["done"] = tool_builder.InstallJob(
            job_id="done", state="succeeded", created_at="2026-07-16T00:00:00+00:00"
        )
    assert tool_builder.any_job_active() is False
    with tool_builder._JOBS_LOCK:
        tool_builder._JOBS["live"] = tool_builder.InstallJob(
            job_id="live", state="queued", created_at="2026-07-16T00:01:00+00:00"
        )
    assert tool_builder.any_job_active() is True


def test_a_sync_reservation_occupies_the_same_single_flight() -> None:
    """A held reservation is worth exactly one job, in BOTH directions (R7-3).

    The synchronous regenerate is not a job, but it reads a package and then
    writes that package's sidecar across a full LLM round trip, so it has to
    occupy the same admission domain -- otherwise the two decide independently
    and a revise admitted mid-generation replaces the very package the summary is
    being written about. The check and the take happen under ONE acquisition of
    ``_JOBS_LOCK`` (the reservation function does both), which is what makes this
    a gate rather than a hint.

    Release is by TOKEN and idempotent: a stale second release cannot free
    somebody else's reservation."""
    token = tool_builder.reserve_sync_operation()
    assert token is not None
    assert tool_builder.any_job_active() is True  # a reservation IS activity
    assert tool_builder.reserve_sync_operation() is None  # no second one
    assert tool_builder._admit_job() is None  # ... and no job either
    assert tool_builder._JOBS == {}  # a refused admission records nothing

    tool_builder.release_sync_operation(token)
    tool_builder.release_sync_operation(token)  # idempotent
    assert tool_builder.any_job_active() is False

    job = tool_builder._admit_job()
    assert job is not None
    assert tool_builder.reserve_sync_operation() is None  # and now the reverse

    # Both halves are MODULE state, so the suite's reset has to clear both or a
    # test that ended mid-reservation would leave the next one unable to admit
    # anything at all.
    tool_builder._reset_jobs_for_tests()  # drops the job admitted above ...
    assert tool_builder.reserve_sync_operation() is not None  # ... freeing the flight
    tool_builder._reset_jobs_for_tests()  # ... and this one drops a HELD reservation
    assert tool_builder.any_job_active() is False
    # Left held on purpose: the fixture's teardown reset is what keeps it out of
    # the next test, which is the property the two resets above pin.
    assert tool_builder.reserve_sync_operation() is not None


# --- router: revise (D40) ------------------------------------------------------


def test_router_revise_202_queues_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The submit is 202 + job id, like the installer's -- a revise IS a builder
    session, so it cannot live inside a request. The feedback arrives stripped by
    the request layer."""
    _seed_package(monkeypatch, tmp_path)
    seen: dict[str, str] = {}

    def fake_start(name: str, feedback: str) -> str:
        seen["name"] = name
        seen["feedback"] = feedback
        return "job-rev"

    monkeypatch.setattr("afterthread.services.tool_builder.start_revise_job", fake_start)
    response = client.post("/api/tools/kbsearch/revise", json={"feedback": "  加上分頁參數  "})

    assert response.status_code == 202
    assert response.json() == {"job_id": "job-rev"}
    assert seen == {"name": "kbsearch", "feedback": "加上分頁參數"}


def test_router_revise_404_for_unknown_tool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    response = client.post("/api/tools/ghost/revise", json={"feedback": "改一下"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Tool not found"}


def test_router_revise_409_when_finalized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """定版 freezes AI iteration, so a revise is refused before any job is
    created -- the same code and the same meaning the regenerate route uses."""
    pkg = _seed_package(monkeypatch, tmp_path)
    _write_meta(pkg, summary="已定版的說明", status="final")

    def must_not_start(name: str, feedback: str) -> str:
        raise AssertionError("no revise job may start for a finalized tool")

    monkeypatch.setattr("afterthread.services.tool_builder.start_revise_job", must_not_start)

    response = client.post("/api/tools/kbsearch/revise", json={"feedback": "改一下"})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "tool_finalized"
    assert "message" in detail


def test_router_revise_409_while_a_job_runs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One tool job at a time: an active install refuses a revise submit. Driven
    through the REAL start_revise_job with a pre-seeded active job, so the shared
    admission gate itself produces the conflict."""
    _seed_package(monkeypatch, tmp_path)
    with tool_builder._JOBS_LOCK:
        tool_builder._JOBS["active"] = tool_builder.InstallJob(
            job_id="active", state="running", created_at="2026-07-16T00:00:00+00:00"
        )

    response = client.post("/api/tools/kbsearch/revise", json={"feedback": "改一下"})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "tool_job_in_progress"
    assert "message" in detail


@pytest.mark.parametrize(
    "payload",
    [{"feedback": "   "}, {"feedback": "x" * 20001}, {}],
    ids=["blank", "oversized", "missing-field"],
)
def test_router_revise_validates_request(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: dict[str, Any],
) -> None:
    """The feedback carries the same bound as every other AI free-text input."""
    _seed_package(monkeypatch, tmp_path)
    assert client.post("/api/tools/kbsearch/revise", json=payload).status_code == 422


def test_router_revise_rejects_invalid_names_as_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    for bad_name in ("UPPER", "bad name", ".hidden"):
        response = client.post(f"/api/tools/{bad_name}/revise", json={"feedback": "改"})
        assert response.status_code == 422, bad_name


def test_router_job_poll_serves_install_and_revise_jobs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ONE endpoint for both kinds (D40): jobs created by the install path and by
    the revise path are polled through the SAME renamed route, with the same
    body. Both jobs here are REAL -- created through the real start_* functions
    with stubbed runs on a real loop -- rather than hand-seeded records, so the
    id the FE would poll is the id these produce."""

    async def scenario() -> tuple[str, str]:
        async def fake_run_install(
            url: str,
            instructions: str,
            *,
            secret_name: str | None = None,
            secret_value: str | None = None,
        ) -> InstallOutcome:
            return InstallOutcome(ok=True, tool_name="kb", summary="裝好了", llm_log_id=1)

        async def fake_run_revise(name: str, feedback: str) -> InstallOutcome:
            return InstallOutcome(ok=True, tool_name=name, summary="改好了", llm_log_id=2)

        monkeypatch.setattr("afterthread.services.tool_builder.run_install", fake_run_install)
        monkeypatch.setattr("afterthread.services.tool_builder.run_revise", fake_run_revise)
        ids: list[str] = []
        for start in (
            lambda: tool_builder.start_install_job("http://x/openapi.json", "i"),
            lambda: tool_builder.start_revise_job("kb", "改一下"),
        ):
            job_id = start()
            assert job_id is not None
            ids.append(job_id)
            # Single-flight: drain to terminal before the next submit.
            for _ in range(200):
                job = tool_builder.get_job(job_id)
                assert job is not None
                if job["state"] not in ("queued", "running"):
                    break
                await asyncio.sleep(0.01)
        await asyncio.gather(*list(tool_builder._TASKS), return_exceptions=True)
        return ids[0], ids[1]

    install_id, revise_id = asyncio.run(scenario())

    install_body = client.get(f"/api/tools/jobs/{install_id}")
    assert install_body.status_code == 200
    assert install_body.json()["summary"] == "裝好了"
    assert install_body.json()["state"] == "succeeded"

    revise_body = client.get(f"/api/tools/jobs/{revise_id}")
    assert revise_body.status_code == 200
    assert revise_body.json()["summary"] == "改好了"
    assert revise_body.json()["tool_name"] == "kb"
    assert set(revise_body.json()) == set(install_body.json())  # one shape, both kinds


# --- run_revise (D40) ----------------------------------------------------------

_REVISED_RUN_PY = "import sys, json\nprint('revised', json.load(sys.stdin))\n"

# A ``.env`` whose raw TEXT is the point: an inline ``#``, both quote kinds, a
# trailing space inside the quoted value, trailing whitespace after it, and a
# comment line -- all of which a naive "parse then re-serialize" restore would
# silently rewrite. The revise must put these exact bytes back.
_TRICKY_ENV = "# the tool's own key\nKB_API_KEY=\"live-secret #value 'x' \"  \nOTHER=plain-value\n"
_TRICKY_ENV_VALUE = "live-secret #value 'x' "


def _tree(root: Path) -> set[str]:
    """Every entry under ``root`` as a relative path (hidden names included)."""
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        for name in dirnames + filenames:
            found.add(str((here / name).relative_to(root)))
    return found


def _file_bytes(root: Path) -> dict[str, bytes]:
    """Every regular file under ``root``: relative path -> exact bytes."""
    contents: dict[str, bytes] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        for name in filenames:
            path = here / name
            if path.is_file() and not path.is_symlink():
                contents[str(path.relative_to(root))] = path.read_bytes()
    return contents


def _staging_dir(root: Path) -> Path:
    """The ONE in-flight staging directory (asserted unique)."""
    staged = list((root / ".staging").iterdir())
    assert len(staged) == 1, staged
    return staged[0]


def _leftovers(root: Path) -> list[str]:
    """Hidden backup directories left behind by a swap (there must be none)."""
    return sorted(child.name for child in root.iterdir() if ".bak-" in child.name)


def _revise_result(name: str = "kbsearch", *, summary: str = "改好了") -> dict[str, Any]:
    return {"tool_name": name, "summary": summary, "ready": True}


def test_run_revise_copies_the_package_without_the_root_env_or_any_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The staging copy is the installed package MINUS the ROOT ``.env`` and MINUS
    the sidecar namespace at every depth -- and identical otherwise.

    The root ``.env`` exclusion is not tidiness (D40): its values are in
    ``known_secret_values``, so a copied one would make ``validate_package``'s
    embedded-secret gate reject every revise of the tool. The sidecar namespace is
    backend-authored and regenerated after the swap; a nested one is dropped for
    the reason R7-1 gives -- matched case-INSENSITIVELY, so the ``.AI_META.JSON``
    spelling that IS the same file on macOS cannot ride in either.

    A NESTED ``.env`` is ordinary package content and is COPIED (R2-2). The r1 rule
    excluded every ``.env``-casefolded name at EVERY depth, so a tool that reads its
    own ``config/.env`` -- its entry runs with the package directory as cwd, and
    nothing stops it -- had that file silently DELETED by an unrelated revise, with
    validation then passing on the mutilated package. Pinned on both sides here: the
    builder sees it, and the revised INSTALLED package still has it, byte for byte."""
    pkg = _seed_package(monkeypatch, tmp_path)
    root = pkg.parent
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")
    (pkg / "lib").mkdir()
    (pkg / "lib" / "util.py").write_text("X = 1\n", encoding="utf-8")
    (pkg / "sub").mkdir()
    (pkg / "sub" / ".AI_META.JSON").write_text('{"summary": "forged"}', encoding="utf-8")
    # A second, NESTED .env the tool reads for itself. Nothing in it is a registered
    # secret, so it sails through the embedded-secret gate like any other file.
    (pkg / "config").mkdir()
    nested_env = b"# the tool's own nested config\r\nPAGE_SIZE=25\n"
    (pkg / "config" / ".env").write_bytes(nested_env)
    _write_meta(pkg, summary="舊的說明", status="draft")
    seen: dict[str, set[str]] = {}
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=lambda: seen.update(staged=_tree(_staging_dir(root))),
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    # The workspace the builder received: everything, minus the ROOT .env and every
    # sidecar. ``sub`` survives as an empty directory -- only the NAMES are excluded.
    assert seen["staged"] == {
        "tool.json",
        "run.py",
        "lib",
        "lib/util.py",
        "sub",
        "config",
        "config/.env",
    }
    # ... and the file an unrelated revise used to delete is still there, untouched.
    assert (pkg / "config" / ".env").read_bytes() == nested_env


def test_run_revise_preserves_the_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The installed ``.env`` survives a revise unchanged -- comments, quoting,
    inline ``#``, trailing whitespace and line ORDER included -- and a builder that
    writes its own is overruled, which is the prompt's stated contract.

    Byte-identity holds for ANY ``.env``, not just this utf-8/LF one: the file is
    copied, never re-serialized and never even decoded (R2-1). The CRLF/invalid-byte
    case that proves the general claim is pinned below."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")
    before = (pkg / ".env").read_bytes()
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        # The builder disobeys and writes its own .env; the backend's restore wins.
        files={"run.py": _REVISED_RUN_PY, ".env": "KB_API_KEY=made-up-by-the-model\n"},
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert (pkg / ".env").read_bytes() == before
    assert (pkg / "run.py").read_text(encoding="utf-8") == _REVISED_RUN_PY


def test_run_revise_preserves_a_crlf_env_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``.env`` that is neither LF-terminated nor valid utf-8 survives a revise
    BYTE-IDENTICAL, hashed before and after (R2-1).

    The r1 round-trip read this file to ``str`` through the shared bounded reader
    (``errors="replace"``, universal newlines) and re-encoded it, so exactly this
    file came back with its CRLFs flattened and its invalid bytes replaced by
    U+FFFD. That was adjudicated harmless on the premise that every consumer goes
    through the same lossy decode -- and the premise is false: the runtime invokes
    a tool with its PACKAGE DIRECTORY as the working directory, so the tool's own
    entry can open ``.env`` in BINARY and hash it, diff it or parse CRLF itself. A
    revise about pagination must not rewrite it. ``_preserve_env_file``'s
    ``shutil.copy2`` never decodes anything, and carries the MODE across too --
    pinned below, because a plain ``copyfile`` would silently drop it."""
    pkg = _seed_package(monkeypatch, tmp_path)
    raw = b"# comment\r\nKB_API_KEY=live-secret-value\r\nBLOB=\xff\xfe-not-utf8\r\n"
    (pkg / ".env").write_bytes(raw)
    (pkg / ".env").chmod(0o640)
    digest_before = hashlib.sha256(raw).hexdigest()
    _fake_generate(monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY})

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert (pkg / "run.py").read_text(encoding="utf-8") == _REVISED_RUN_PY  # it really ran
    assert hashlib.sha256((pkg / ".env").read_bytes()).hexdigest() == digest_before
    assert (pkg / ".env").stat().st_mode & 0o777 == 0o640


def test_run_revise_registers_the_live_env_values_for_the_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every value of the existing ``.env`` is registered as an in-flight secret
    for the whole revise, then discarded.

    The registration -- not the on-disk scan -- is what matters: the scan already
    covers the package while it sits there, but it skips dot-directories, and the
    swap parks the old package in one (see the swap-window test below)."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")
    seen: dict[str, Any] = {}
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=lambda: seen.update(
            known=tools.known_secret_values(), inflight=set(tools._INFLIGHT_SECRETS)
        ),
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert _TRICKY_ENV_VALUE in seen["known"]
    assert seen["inflight"] == {_TRICKY_ENV_VALUE, "plain-value"}
    # ... and the transient registration ends with the run (the restored .env is
    # what keeps the values redactable afterwards, via the scan).
    with tools._INFLIGHT_LOCK:
        assert not tools._INFLIGHT_SECRETS


def test_run_revise_keeps_the_env_redactable_across_the_backup_swap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reason D40 requires the in-flight registration, pinned.

    At the instant the swap runs, the old package has been renamed to a
    DOT-prefixed backup -- which ``known_secret_values`` skips (it only reads
    non-hidden package directories) and which ``_scan_all`` skips too -- and the
    revision is not in place yet. Without the registration the tool's own
    credentials would be UNKNOWN to the redactor for exactly that window, while
    the builder session's records are still being written."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")
    seen: dict[str, Any] = {}
    real_rename = os.rename

    def recording_rename(src: Any, dst: Any) -> Any:
        # The PUBLISH rename (its source is the staging dir) -- by which point the
        # old package has already become `.kbsearch.bak-<uuid>` and the revision is
        # not in place yet. The backup rename that precedes it is left alone.
        if tool_builder._STAGING_DIRNAME in str(src):
            seen["known"] = tools.known_secret_values()
            seen["listed"] = [row["name"] for row in tools.list_tools()]
            seen["backups"] = _leftovers(pkg.parent)
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", recording_rename)
    _fake_generate(monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY})

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert _TRICKY_ENV_VALUE in seen["known"]  # still redactable mid-swap
    assert seen["backups"] and seen["backups"][0].startswith(".kbsearch.bak-")
    assert seen["listed"] == []  # the hidden backup is invisible to the registry


def test_run_revise_refuses_a_model_rename(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``tool_name`` addresses WHICH package gets replaced, so a model that
    renames the tool is refused: ``InstallResult`` only validates the SHAPE of
    the name, and promoting under another name would overwrite whatever package
    that name addresses. The original is left exactly as it was."""
    pkg = _seed_package(monkeypatch, tmp_path)
    before = _file_bytes(pkg)
    _fake_generate(
        monkeypatch,
        result=_revise_result("kbsearch2"),
        files={"run.py": _REVISED_RUN_PY},
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.tool_name == "kbsearch"  # never the model's answer
    assert "改名" in (outcome.error or "")
    assert "kbsearch2" in (outcome.error or "")  # names what was attempted
    assert outcome.llm_log_id is not None
    assert _file_bytes(pkg) == before
    assert not (pkg.parent / "kbsearch2").exists()
    assert _leftovers(pkg.parent) == []


def test_run_revise_replaces_the_installed_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The happy path end to end: the revision goes live under the SAME name, the
    registry still sees exactly one valid package, and the swap leaves no hidden
    backup or staging residue behind."""
    pkg = _seed_package(monkeypatch, tmp_path)
    root = pkg.parent
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")
    captured = _fake_generate(
        monkeypatch,
        result=_revise_result(summary="改好了並測過"),
        files={"run.py": _REVISED_RUN_PY, "lib/helper.py": "Y = 2\n"},
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert outcome.tool_name == "kbsearch"
    assert outcome.summary == "改好了並測過"
    assert outcome.llm_log_id == llm_log.last_record_id_for_workflow("tool_install")
    assert (pkg / "run.py").read_text(encoding="utf-8") == _REVISED_RUN_PY
    assert (pkg / "lib" / "helper.py").read_text(encoding="utf-8") == "Y = 2\n"
    assert [(row["name"], row["valid"]) for row in tools.list_tools()] == [("kbsearch", True)]
    assert _leftovers(root) == []
    assert not (root / ".staging").exists()
    # A revise session IS a builder session: same workflow name and same budgets.
    settings = Settings(tools_dir=str(root))
    assert captured["workflow"] == "tool_install"
    assert captured["max_tool_rounds"] == settings.tool_install_max_rounds
    assert captured["timeout_seconds"] == settings.tool_install_timeout_seconds


def _replace_fixture(base: Path) -> tuple[Path, Path]:
    """``(installed, staging)`` for a direct ``_promote_staging_replace`` call: a
    valid installed ``kbsearch`` and a valid staged revision of the same name."""
    installed = base / "kbsearch"
    installed.mkdir(parents=True)
    (installed / "tool.json").write_text(
        json.dumps(_package_manifest("kbsearch")), encoding="utf-8"
    )
    (installed / "run.py").write_text(_GOOD_RUN_PY, encoding="utf-8")
    staging = base / tool_builder._STAGING_DIRNAME / "buildid"
    staging.mkdir(parents=True)
    (staging / "tool.json").write_text(json.dumps(_package_manifest("kbsearch")), encoding="utf-8")
    (staging / "run.py").write_text("print('revised')", encoding="utf-8")
    return installed, staging


def test_promote_replace_refuses_a_finalization_that_landed_mid_session(
    tmp_path: Path,
) -> None:
    """A revise session runs for MINUTES, so a user finalizing DURING one is
    ordinary. The entry gate cannot see that: it ran before the LLM call. Without
    a last-moment re-check the swap would replace the whole package -- and since
    the sidecar is excluded from staging and regenerated as a DRAFT afterwards,
    定版 would be silently undone, frozen text and all, by a session that started
    before it. Mirrors store_summary_meta's own store-time re-check (D40 r2)."""
    base = tmp_path / "tools"
    installed, staging = _replace_fixture(base)
    assert tools.write_tool_meta(
        installed,
        {"summary": "凍結的總結", "status": "final", "updated_at": "2026-07-27T00:00:00+00:00"},
    )

    origin, error = tool_builder._promote_staging_replace(
        staging,
        "kbsearch",
        base,
        env_existed_at_start=False,
        package_identity=tool_builder._package_identity(base / "kbsearch"),
        registered=[],
    )

    assert error == tool_builder._ERROR_REVISE_FINALIZED
    assert origin is None  # a refusal hands nothing back
    # the installed package -- and its frozen summary -- are untouched
    assert (installed / "run.py").read_text(encoding="utf-8") == _GOOD_RUN_PY
    meta = tools.read_tool_meta(installed)
    assert meta is not None
    assert meta["summary"] == "凍結的總結"
    assert meta["status"] == "final"
    assert not any(entry.name.startswith(".kbsearch.bak-") for entry in base.iterdir())


@pytest.mark.skipif(
    _permission_tests_unreliable(), reason="chmod 000 does not block access (root or non-POSIX)"
)
def test_promote_replace_refuses_when_the_sidecar_cannot_be_read(tmp_path: Path) -> None:
    """R3-2: the pre-swap 定版 re-check must fail CLOSED on uncertainty.

    ``tools.summary_status`` folds "no sidecar" and "there IS one but it could
    not be read" into the SAME None, so a gate keyed on an explicit ``"final"``
    was FAIL-OPEN: a user finalizes mid-session, the sidecar then hits a transient
    read error (a chmod, an EIO, a symlink raced in), and the swap proceeded --
    replacing the package, taking the frozen summary with it and regenerating a
    draft. Exactly the destruction this gate exists to prevent, reached through
    the failure of the check rather than around it.

    The sidecar here IS finalized, and the point is that the gate never gets to
    see that: it refuses on "cannot tell" alone. The message is its own, because
    telling this operator to 解除定版 would send them after the wrong thing."""
    base = tmp_path / "tools"
    installed, staging = _replace_fixture(base)
    assert tools.write_tool_meta(
        installed,
        {"summary": "凍結的總結", "status": "final", "updated_at": "2026-07-27T00:00:00+00:00"},
    )
    sidecar = installed / tools._AI_META_FILENAME
    sidecar.chmod(0o000)
    try:
        assert tools.summary_status(installed) is None  # the fail-OPEN input, pinned
        _origin, error = tool_builder._promote_staging_replace(
            staging,
            "kbsearch",
            base,
            env_existed_at_start=False,
            package_identity=tool_builder._package_identity(base / "kbsearch"),
            registered=[],
        )
    finally:
        # Guarded so the ASSERTIONS report a regression: if the gate ever goes
        # fail-open again the swap consumes the package, the sidecar is gone, and
        # an unguarded chmod would mask the real failure with a FileNotFoundError
        # raised out of the teardown.
        if sidecar.exists():
            sidecar.chmod(0o600)

    assert error == tool_builder._ERROR_REVISE_SUMMARY_UNREADABLE
    assert error != tool_builder._ERROR_REVISE_FINALIZED  # a different remedy, a different message
    assert (installed / "run.py").read_text(encoding="utf-8") == _GOOD_RUN_PY  # never swapped
    meta = tools.read_tool_meta(installed)
    assert meta is not None
    assert meta["summary"] == "凍結的總結"  # the frozen text survived
    assert _leftovers(base) == []  # and no .bak- residue from a half-started swap


def test_promote_replace_refuses_a_sidecar_that_is_not_a_regular_file(tmp_path: Path) -> None:
    """The same refusal without any permission bits: a FIFO at the sidecar name is
    refused by the bounded reader on EVERY platform, so "cannot tell" is reached
    the way an unjailed ``run_shell`` (D21) could actually arrange it."""
    base = tmp_path / "tools"
    installed, staging = _replace_fixture(base)
    os.mkfifo(installed / tools._AI_META_FILENAME)

    _origin, error = tool_builder._promote_staging_replace(
        staging,
        "kbsearch",
        base,
        env_existed_at_start=False,
        package_identity=tool_builder._package_identity(base / "kbsearch"),
        registered=[],
    )

    assert error == tool_builder._ERROR_REVISE_SUMMARY_UNREADABLE
    assert (installed / "run.py").read_text(encoding="utf-8") == _GOOD_RUN_PY
    assert _leftovers(base) == []


@pytest.mark.parametrize("status", ["draft", None], ids=["draft", "no-sidecar"])
def test_promote_replace_proceeds_when_the_status_is_knowable(
    tmp_path: Path, status: str | None
) -> None:
    """Fail-closed must not become refuse-everything: a DRAFT sidecar and a
    genuinely ABSENT one are both definite answers of "not finalized", and both
    still publish. ENOENT is the one case where "not finalized" is a fact.

    Both also pin what the gate HANDS BACK (R4-2): the draft's origin comes out of
    the very read that cleared the swap, and a package with no sidecar at all has
    nothing to inherit and says so with None -- the one place where "no origin" is
    a fact rather than a failed look."""
    base = tmp_path / "tools"
    installed, staging = _replace_fixture(base)
    if status is not None:
        assert tools.write_tool_meta(
            installed,
            {
                "summary": "草稿",
                "status": status,
                "origin": {"openapi_url": "https://kb.example", "instructions": "原始安裝指示"},
                "updated_at": "2026-07-27T00:00:00+00:00",
            },
        )

    origin, error = tool_builder._promote_staging_replace(
        staging,
        "kbsearch",
        base,
        env_existed_at_start=False,
        package_identity=tool_builder._package_identity(base / "kbsearch"),
        registered=[],
    )

    assert error is None
    assert (installed / "run.py").read_text(encoding="utf-8") == "print('revised')"
    assert _leftovers(base) == []
    if status is None:
        assert origin is None
    else:
        assert origin == {"openapi_url": "https://kb.example", "instructions": "原始安裝指示"}


def test_promote_replace_refuses_a_finalization_that_lands_during_the_env_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 定版 gate must be the LAST pre-swap step, not merely a late one (R4-1).

    Every other pre-swap step finishes in a bounded moment; the ``.env`` copy does
    not -- an operator chooses the file, so its duration is theirs, not ours. With
    the copy AFTER the gate, a finalization landing while the bytes moved was
    invisible to a check that had already passed, and the swap then replaced the
    package, took the frozen text with it and regenerated a draft. That is not the
    instants-wide check-then-act residual this module accepts; it is a window an
    outside party sets the size of.

    So the copy now runs FIRST (it only writes into staging, which nothing else
    observes) and the gate runs last. Driven by finalizing from INSIDE the copy,
    which is the exact window the finding names: under the old ordering this test
    would swap and the frozen summary would be gone."""
    base = tmp_path / "tools"
    installed, staging = _replace_fixture(base)
    (installed / ".env").write_text("KB_API_KEY=live-secret-value\n", encoding="utf-8")
    real_copy2 = shutil.copy2
    copies: list[str] = []

    def finalize_while_copying(source: Any, destination: Any, **kwargs: Any) -> Any:
        copies.append(str(source))
        result = real_copy2(source, destination, **kwargs)
        assert tools.write_tool_meta(
            installed,
            {"summary": "凍結的總結", "status": "final", "updated_at": "2026-07-27T00:00:00+00:00"},
        )
        return result

    monkeypatch.setattr(shutil, "copy2", finalize_while_copying)
    registered: list[str] = []

    origin, error = tool_builder._promote_staging_replace(
        staging,
        "kbsearch",
        base,
        env_existed_at_start=True,
        package_identity=tool_builder._package_identity(base / "kbsearch"),
        registered=registered,
    )

    assert copies  # the window is real: the copy ran, and 定版 landed inside it
    # The shipped bytes were vetted and their values registered BEFORE the copy
    # (R7-2), so the refusal below happens with the credentials already redactable.
    assert registered == ["live-secret-value"]
    assert error == tool_builder._ERROR_REVISE_FINALIZED
    assert origin is None
    assert (installed / "run.py").read_text(encoding="utf-8") == _GOOD_RUN_PY  # never swapped
    meta = tools.read_tool_meta(installed)
    assert meta is not None
    assert meta["summary"] == "凍結的總結"  # the frozen text survived
    assert meta["status"] == "final"
    assert _leftovers(base) == []


def test_promote_replace_refuses_an_oversized_live_env(tmp_path: Path) -> None:
    """The copy is bounded by the SAME ceiling the rest of the system applies to a
    ``.env`` (R4-1), and refuses BEFORE anything is moved.

    Two reasons, and the second is why an unbounded copy was a defect rather than
    a waste: over the cap the values were never parseable in the first place
    (``_read_env_for_values`` refuses, ``tools._load_tool_dotenv`` degrades the
    tool to no-env at runtime), so copying it would publish a file the rest of the
    system rejects; and the copy is the last thing standing between the gates and
    the swap, so its duration has to be ours to bound."""
    base = tmp_path / "tools"
    installed, staging = _replace_fixture(base)
    (installed / ".env").write_bytes(b"K=" + b"v" * (tools._ENV_FILE_MAX_BYTES - 1))
    assert (installed / ".env").stat().st_size == tools._ENV_FILE_MAX_BYTES + 1
    registered: list[str] = []

    origin, error = tool_builder._promote_staging_replace(
        staging,
        "kbsearch",
        base,
        env_existed_at_start=True,
        package_identity=tool_builder._package_identity(base / "kbsearch"),
        registered=registered,
    )

    assert error == tool_builder._ERROR_REVISE_ENV_TOO_LARGE
    assert origin is None
    assert registered == []  # refused by SIZE before anything was read or registered
    assert not (staging / ".env").exists()  # refused BEFORE the copy, not after it
    assert (installed / "run.py").read_text(encoding="utf-8") == _GOOD_RUN_PY  # never swapped
    assert _leftovers(base) == []


def test_promote_replace_copies_a_live_env_at_the_ceiling_byte_for_byte(tmp_path: Path) -> None:
    """The refusal above is the ceiling EXACTLY, and the ordinary path is
    unchanged: a ``.env`` AT the cap still publishes, byte for byte.

    The gate must not creep past the property it enforces -- a size check that
    quietly refused ordinary files would make a package unrevisable forever, with
    a message that names no key and no value to explain why."""
    base = tmp_path / "tools"
    installed, staging = _replace_fixture(base)
    head = b"# CRLF \xff and a non-utf8 byte survive too\r\nK="
    raw = head + b"v" * (tools._ENV_FILE_MAX_BYTES - len(head))
    assert len(raw) == tools._ENV_FILE_MAX_BYTES  # the cap EXACTLY, on the allowed side
    (installed / ".env").write_bytes(raw)
    registered: list[str] = []

    origin, error = tool_builder._promote_staging_replace(
        staging,
        "kbsearch",
        base,
        env_existed_at_start=True,
        package_identity=tool_builder._package_identity(base / "kbsearch"),
        registered=registered,
    )

    assert error is None
    # A file AT the cap is READ and vetted too, not waved through: its one value
    # is registered, and the promote-side policy re-run passed on it (R7-2).
    assert registered == [raw.split(b"K=", 1)[1].decode("utf-8")]
    assert origin is None  # no sidecar in this fixture: nothing to inherit
    assert (installed / "run.py").read_text(encoding="utf-8") == "print('revised')"  # it swapped
    assert (installed / ".env").read_bytes() == raw  # ... and the credentials came across
    assert _leftovers(base) == []


def test_run_revise_rolls_back_a_failed_publish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the PUBLISH rename fails AFTER the old package was renamed aside, the
    backup is renamed home: the installed tool comes back byte-identical, the
    outcome says so, and nothing hidden is left in the tools directory.

    Only the publish rename is broken here; the roll-back rename still works,
    which is the whole point -- with the publish being a plain ``os.rename``
    (R1-2) a failure moved NOTHING, so the target name is free and the backup can
    always go home. That is what makes ``_ERROR_REVISE_UNRECOVERABLE`` the rare
    outcome its docstring claims."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")
    before = _file_bytes(pkg)
    real_rename = os.rename

    def exploding_publish(src: Any, dst: Any) -> Any:
        if tool_builder._STAGING_DIRNAME in str(src):
            raise OSError("no space left on device")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", exploding_publish)
    _fake_generate(monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY})

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert "置換失敗" in (outcome.error or "")
    assert "已還原" in (outcome.error or "")
    assert "OSError" in (outcome.error or "")  # category only, never str(exc)
    assert "no space left" not in (outcome.error or "")
    assert _file_bytes(pkg) == before  # every file back, byte for byte
    assert _leftovers(pkg.parent) == []
    assert [(row["name"], row["valid"]) for row in tools.list_tools()] == [("kbsearch", True)]


def test_run_revise_publish_is_a_rename_with_no_copy_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The swap NEVER goes through ``shutil.move`` (R1-2).

    ``move`` falls back to copytree-then-delete whenever ``os.rename`` raises --
    it catches every ``OSError``, not just EXDEV -- so a publish through it is not
    atomic: with the old package already parked in the hidden backup, a partial
    copy leaves a half-built directory AT the tool's name, and the roll-back's
    ``os.rename(backup, target)`` then fails because that name is occupied. The
    result is a half-replaced tool plus a hidden backup, which is exactly the
    state the roll-back exists to prevent. Pinned by making any call fatal: this
    passes only while the publish is a plain rename.

    ``copytree`` (the staging copy) is untouched -- the pin is on ``move``
    specifically, which is the only shutil entry point with that fallback."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")

    def forbidden_move(src: Any, dst: Any) -> Any:
        raise AssertionError("the revise publish must not use shutil.move")

    monkeypatch.setattr(shutil, "move", forbidden_move)
    _fake_generate(monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY})

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert (pkg / "run.py").read_text(encoding="utf-8") == _REVISED_RUN_PY
    assert _leftovers(pkg.parent) == []


def test_run_revise_reports_the_original_being_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A delete landing during the build is answered honestly -- the revision is
    NOT installed as a new tool, because 'replace' has nothing to replace."""
    pkg = _seed_package(monkeypatch, tmp_path)
    root = pkg.parent
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=lambda: shutil.rmtree(pkg),
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_TARGET_MISSING
    assert not pkg.exists()
    assert tools.list_tools() == []
    assert _leftovers(root) == []
    assert not (root / ".staging").exists()


def test_run_revise_never_replaces_on_a_validation_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A revision that no longer validates keeps the WORKING tool running: the
    swap is downstream of the same gate a fresh install passes."""
    pkg = _seed_package(monkeypatch, tmp_path)
    before = _file_bytes(pkg)
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"tool.json": "{ not json at all"},
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert "驗證失敗" in (outcome.error or "")
    assert _file_bytes(pkg) == before
    assert _leftovers(pkg.parent) == []


def test_run_revise_refuses_a_finalized_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """定版 is checked HERE too, not only at the route: the two are seconds apart
    and freezing AI iteration is the whole meaning of 定版. No session starts."""
    pkg = _seed_package(monkeypatch, tmp_path)
    _write_meta(pkg, summary="已定版", status="final")

    async def must_not_generate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no builder session may start for a finalized tool")

    monkeypatch.setattr("afterthread.services.tool_builder.generate_structured", must_not_generate)

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_FINALIZED
    assert outcome.llm_log_id is None


def test_run_revise_refuses_an_unreadable_sidecar_before_the_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sidecar that is ALREADY corrupt when the request arrives is refused at the
    entry gate, not after a whole builder session (R6-3).

    The gate used to read through the TOTAL ``summary_status``, where "no sidecar"
    and "there IS one and it cannot be trusted" are the same None -- so this package
    was admitted, spent a full multi-round session holding the global single-flight,
    and was then refused by ``_promote_staging_replace``'s STRICT read at the very
    end. The refusal was never in doubt; only the bill was, and every retry paid it
    again. The job is the thing that ACTS, so the job is the thing that fails
    closed; the ROUTE keeps the cheap total reader on purpose, because a route
    answers about a resource's known state and guessing there costs at most this
    outcome arriving as a job result instead of a 409.

    The message is the one promote uses for the same condition, and it is NOT the
    finalized one: the remedies differ (repair or remove a damaged sidecar vs.
    解除定版), and following the wrong one would leave the operator toggling a state
    that is not the problem."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / tools._AI_META_FILENAME).write_text("{ not json at all", encoding="utf-8")
    before = _file_bytes(pkg)

    async def must_not_generate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no builder session may start for an unreadable sidecar")

    monkeypatch.setattr("afterthread.services.tool_builder.generate_structured", must_not_generate)

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_SUMMARY_UNREADABLE
    assert outcome.error != tool_builder._ERROR_REVISE_FINALIZED  # two remedies, two messages
    assert outcome.llm_log_id is None
    assert _file_bytes(pkg) == before  # the damaged sidecar is left exactly as found
    assert not (pkg.parent / tool_builder._STAGING_DIRNAME).exists()


def test_run_revise_refuses_a_missing_tool_and_a_disabled_feature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both did-not-happen cases are friendly outcomes, never exceptions -- this
    runs in a fire-and-forget task whose only consumer is the polling FE."""

    async def must_not_generate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no builder session may start")

    monkeypatch.setattr("afterthread.services.tool_builder.generate_structured", must_not_generate)
    _install_settings(monkeypatch, tools_dir=str(tmp_path / "tools"))
    missing = asyncio.run(tool_builder.run_revise("ghost", "改一下"))
    assert missing.ok is False
    assert missing.error == tool_builder._ERROR_REVISE_NOT_FOUND

    _install_settings(monkeypatch, tools_dir="")
    off = asyncio.run(tool_builder.run_revise("kbsearch", "改一下"))
    assert off.ok is False
    assert off.error == _ERROR_TOOLS_DISABLED


def test_run_revise_refuses_an_unreadable_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``.env`` that EXISTS but the bounded, O_NOFOLLOW'd reader declines (here
    a symlink) stops the revise instead of degrading to 'no .env'.

    The runtime loader degrades; this one must not. Running the session anyway
    would hand ``run_shell`` a tool whose real credentials the redactor knows
    nothing about, and then swap in a package built in that blind spot."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (tmp_path / "elsewhere.env").write_text("KB_API_KEY=live-secret-value\n", encoding="utf-8")
    (pkg / ".env").symlink_to(tmp_path / "elsewhere.env")
    before = _file_bytes(pkg)

    async def must_not_generate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no builder session may start")

    monkeypatch.setattr("afterthread.services.tool_builder.generate_structured", must_not_generate)

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_UNREADABLE
    assert _file_bytes(pkg) == before
    assert (pkg / ".env").is_symlink()  # left exactly as found


def test_run_revise_refuses_a_stat_failure_on_the_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only ``FileNotFoundError`` means "there is no ``.env``" (R2-3).

    The existence probe used to swallow EVERY ``OSError``, so a transient EIO on a
    failing disk, an ESTALE on an NFS mount, or an EACCES from a directory an
    operator had just chmod'ed all read as ABSENCE -- and the revise then ran a
    whole builder session with the tool's real credentials unregistered and
    unexported, which is the degrade this gate exists to refuse. A failure to LOOK
    is not evidence of absence, so it takes the same unreadable path a symlinked
    ``.env`` takes above."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")
    before = _file_bytes(pkg)
    real_lstat = Path.lstat
    refusing = {"on": True}  # switched off before the package is inspected below

    def flaky_lstat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if refusing["on"] and self.name == ".env":
            raise PermissionError(13, "Permission denied")
        return real_lstat(self, *args, **kwargs)

    async def must_not_generate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no builder session may start")

    monkeypatch.setattr(Path, "lstat", flaky_lstat)
    monkeypatch.setattr("afterthread.services.tool_builder.generate_structured", must_not_generate)

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))
    refusing["on"] = False

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_UNREADABLE
    assert outcome.llm_log_id is None
    assert _file_bytes(pkg) == before
    with tools._INFLIGHT_LOCK:
        assert not tools._INFLIGHT_SECRETS  # nothing was registered either


def test_run_revise_refuses_a_live_env_that_stopped_being_a_regular_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The preserve step guards its SOURCE, and a refusal there aborts BEFORE the
    swap (R2-1).

    The entry read vetted a regular file, but the session runs for minutes and
    ``run_shell`` is unjailed (D21), so the live ``.env`` can be a symlink or a
    FIFO by the time the copy runs. ``shutil.copy2`` would happily read straight
    through it; the ``lstat`` gate refuses instead, mirroring what the bounded
    reader's ``O_NOFOLLOW`` refuses at the front door -- and because the copy is
    the LAST thing before the rename-aside, the working tool is still the working
    tool."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")
    (tmp_path / "elsewhere.env").write_text("KB_API_KEY=someone-elses\n", encoding="utf-8")

    def swap_the_env_for_a_link() -> None:
        (pkg / ".env").unlink()
        (pkg / ".env").symlink_to(tmp_path / "elsewhere.env")

    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=swap_the_env_for_a_link,
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_UNREADABLE
    assert (pkg / "run.py").read_text(encoding="utf-8") == "print('x')\n"  # never swapped
    assert (pkg / ".env").is_symlink()  # and the raced-in link is left alone
    assert _leftovers(pkg.parent) == []
    assert not (pkg.parent / tool_builder._STAGING_DIRNAME).exists()


def test_run_revise_refuses_to_copy_the_env_through_a_planted_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The preserve step guards its DESTINATION too, and that is load-bearing.

    ``shutil.copy2`` FOLLOWS its destination, so a symlink planted at
    ``<staging>/.env`` by the unjailed ``run_shell`` (D21) would take the LIVE
    credentials through it and write them outside staging. The text write this
    copy replaced refused that outright (``tools._write_regular_file`` opens
    ``O_NOFOLLOW``), so the ``lstat`` gate is what keeps the refusal set identical
    rather than quietly widened. The outside file must come back untouched."""
    pkg = _seed_package(monkeypatch, tmp_path)
    root = pkg.parent
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("not ours\n", encoding="utf-8")

    def plant_a_link_at_the_staged_env() -> None:
        os.symlink(outside, _staging_dir(root) / ".env")

    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=plant_a_link_at_the_staged_env,
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_RESTORE
    assert outside.read_text(encoding="utf-8") == "not ours\n"  # nothing written through
    assert (pkg / ".env").read_text(encoding="utf-8") == _TRICKY_ENV
    assert (pkg / "run.py").read_text(encoding="utf-8") == "print('x')\n"  # never swapped
    assert _leftovers(root) == []


_MODEL_ENV = "KB_API_KEY=made-up-by-the-model\n"


def test_run_revise_ships_no_env_when_the_live_one_was_deleted_mid_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R3-1: a live ``.env`` DELETED during the session must not be replaced by the
    model's placeholder.

    The preserve step treated a missing SOURCE as "nothing to preserve" and
    returned success, so whatever the builder wrote at ``<staging>/.env`` shipped.
    The standing acceptance ("a package with no prior ``.env`` may receive a
    builder-written one") covers packages that NEVER had one; it says nothing
    about one that existed when the session started and was removed during it.
    That deletion is a deliberate act on the LIVE package -- the operator pulled
    the credentials -- while the builder's file is an artifact of a prompt that
    told it not to write one at all. So the staged ``.env`` is dropped and the
    published package has none, which is what the deletion asked for.

    Driven from the fake's side effect, which runs after the entry read observed
    the real ``.env`` and before the promote -- the only window the real
    multi-minute session leaves for this."""
    pkg = _seed_package(monkeypatch, tmp_path)
    root = pkg.parent
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY, ".env": _MODEL_ENV},
        side_effect=lambda: (pkg / ".env").unlink(),
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True  # the revision itself is fine; only the .env is dropped
    assert (pkg / "run.py").read_text(encoding="utf-8") == _REVISED_RUN_PY  # it really swapped
    assert not (pkg / ".env").exists()  # the deletion stands
    # ... and the model's placeholder is nowhere on disk, not in a leftover staging
    # dir, not in a backup, not under any other name.
    assert _MODEL_ENV.encode() not in b"".join(_file_bytes(root).values())
    assert _leftovers(root) == []
    assert not (root / tool_builder._STAGING_DIRNAME).exists()


def test_run_revise_treats_a_valueless_env_as_having_existed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ "It existed" comes from the READ, never from the parsed values (R4-3).

    A comment-only ``.env`` parses to ``{}`` -- exactly what a package with NO
    ``.env`` parses to -- yet the two mean opposite things when the file is gone by
    promote time: one is an operator who pulled a credentials file mid-session, the
    other is a package that may receive the builder's own. Now that the reader
    hands back an existence BIT plus the values rather than the text the caller
    tested for None, the bit has to be taken from the read; deriving it from the
    values would silently collapse this case into the never-had-one branch and let
    the placeholder ship."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text("# only a comment, no values at all\n", encoding="utf-8")
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY, ".env": _MODEL_ENV},
        side_effect=lambda: (pkg / ".env").unlink(),
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert (pkg / "run.py").read_text(encoding="utf-8") == _REVISED_RUN_PY  # it really swapped
    assert not (pkg / ".env").exists()  # the deletion stands: no placeholder took its place


def test_run_revise_ships_the_builder_env_when_the_package_never_had_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The standing acceptance, unchanged (R3-1): a package with NO ``.env`` at
    session start still gets the builder-written one, exactly as an install does
    -- otherwise a revise could never act on "store the key in .env" feedback.

    This is what makes the deletion case above a real discrimination rather than
    a blanket "never ship a builder ``.env``".

    Strengthened on two points the acceptance never covered. The file goes out
    BYTE for byte (nothing normalizes what the builder wrote), and its value is
    REGISTERED as an in-flight secret while the post-promote summary session runs
    -- that session reads the freshly published package and writes a sidecar, and
    the registration is what the redactors have to work with in the window where
    the ``.env`` is brand new. Observed inside the summary stub because that is
    the window; the session's ``finally`` discards it, which the last assertion
    pins."""
    pkg = _seed_package(monkeypatch, tmp_path)
    assert not (pkg / ".env").exists()
    at_summary: dict[str, set[str]] = {}
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY, ".env": _MODEL_ENV},
    )

    def observe_while_summarizing() -> None:
        with tools._INFLIGHT_LOCK:
            at_summary["inflight"] = set(tools._INFLIGHT_SECRETS)

    _fake_summary_generate(monkeypatch, side_effect=observe_while_summarizing)

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert (pkg / ".env").read_bytes() == _MODEL_ENV.encode("utf-8")
    assert (pkg / "run.py").read_text(encoding="utf-8") == _REVISED_RUN_PY
    assert at_summary["inflight"] == {"made-up-by-the-model"}
    with tools._INFLIGHT_LOCK:
        assert not tools._INFLIGHT_SECRETS


@pytest.mark.parametrize(
    ("staged_env", "expected_error"),
    [
        ("PIN=1234\n", tool_builder._ERROR_REVISE_ENV_UNMASKABLE),
        ('TOKEN="abcd\\"efgh"\n', tool_builder._ERROR_REVISE_ENV_UNMATCHABLE),
    ],
    ids=["below-the-floor", "reversible-spelling"],
)
def test_run_revise_refuses_a_builder_env_the_redactors_could_not_mask(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, staged_env: str, expected_error: str
) -> None:
    """A builder-WRITTEN ``.env`` must clear the same policy a preserved one does.

    This is the seam between two rules that were each correct alone. The standing
    acceptance says a package that never had a ``.env`` MAY receive one the
    builder wrote; the maskability policy says a ``.env`` value the redactors
    cannot mask must never be handed to this system. Nothing joined them: the
    preserve step returned success without ever parsing the staged file, and
    ``validate_package`` only checks its SIZE and scans for values ALREADY known,
    so it cannot see a new short value or a reversible spelling.

    So a revise could publish ``PIN=1234`` -- which the 6-char floor then
    permanently refuses to mask in live tool results, in the AI 日誌 and in the
    summary -- or ``TOKEN="abcd\\"efgh"``, whose escaped spelling matches no
    registered value when a ``cat`` prints it. The proof that this is a defect
    rather than untidiness: the NEXT revise of that package refuses at its entry
    gate on exactly these two rules, so the system would have produced a state it
    declines to work with.

    Both refusals are the SAME category-only strings the entry gate uses -- same
    condition, same remedy -- and they name neither key nor value. Refusing here
    costs the operator nothing they had: the installed package is untouched, the
    swap never ran, and no hidden backup is left behind."""
    pkg = _seed_package(monkeypatch, tmp_path)
    assert not (pkg / ".env").exists()
    before = _file_bytes(pkg)
    captured = _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY, ".env": staged_env},
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == expected_error
    assert "PIN" not in (outcome.error or "")  # category only: never the key ...
    assert "1234" not in (outcome.error or "")  # ... and never the value
    assert captured  # the session DID run: only the promote could have stopped it
    assert _file_bytes(pkg) == before  # the installed package is exactly as it was
    assert not (pkg / ".env").exists()  # ... including having no .env at all
    assert _leftovers(pkg.parent) == []  # nothing was renamed aside
    assert not (pkg.parent / tool_builder._STAGING_DIRNAME).exists()
    with tools._INFLIGHT_LOCK:
        assert not tools._INFLIGHT_SECRETS


def test_run_revise_keeps_an_env_the_operator_added_mid_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The MIRROR of the deletion case, and it needs no flag: a ``.env`` created
    on the live package DURING the session is copied like any other, because the
    file copied is the one on disk at the instant of the swap (R2-1). "Absent at
    the start" only decides what happens when it is absent at the END too.

    It is also the COMPLIANT half of R7-2: the promote re-runs the whole ``.env``
    policy on the bytes it is about to ship, so this file -- which no entry gate
    ever saw, the package having had none -- is vetted, its value REGISTERED
    before the copy, and then published byte for byte. The registration is
    observed at ``copy2`` time because that is the only instant it is visible: the
    session's ``finally`` discards it again, which the last assertion pins (a
    registration made here and never dropped would outlive the request)."""
    pkg = _seed_package(monkeypatch, tmp_path)
    added = "OPERATOR_KEY=added-during-the-session\n"
    real_copy2 = shutil.copy2
    at_copy: dict[str, set[str]] = {}

    def add_an_env_mid_session() -> None:
        (pkg / ".env").write_text(added, encoding="utf-8")

    def observe_while_copying(source: Any, destination: Any, **kwargs: Any) -> Any:
        with tools._INFLIGHT_LOCK:
            at_copy["inflight"] = set(tools._INFLIGHT_SECRETS)
        return real_copy2(source, destination, **kwargs)

    monkeypatch.setattr(shutil, "copy2", observe_while_copying)
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY, ".env": _MODEL_ENV},
        side_effect=add_an_env_mid_session,
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert (pkg / ".env").read_bytes() == added.encode("utf-8")  # the operator's file won
    # Registered BEFORE the bytes moved, so the post-promote summary/sidecar paths
    # are redacted against a value nothing had ever registered (R7-2) ...
    assert at_copy["inflight"] == {"added-during-the-session"}
    with tools._INFLIGHT_LOCK:
        assert not tools._INFLIGHT_SECRETS  # ... and discarded again by the finally


def test_run_revise_refuses_to_ship_an_env_added_mid_session_that_fails_the_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The spelling/floor/size guards must answer for the bytes that SHIP, not
    just for the snapshot the session opened with (R7-2).

    This package has NO ``.env`` when the revise starts, so every entry gate
    passes vacuously; the operator then creates a non-compliant one while the
    builder session runs. Before this fix ``_preserve_env_file`` copied that
    never-vetted file straight into the published package -- the guards had
    checked a file that no longer existed. Now the promote re-reads the source it
    is about to copy and re-runs the same policy on those exact bytes, so the
    refusal is the same category-only error the entry gate would have given.

    Refusing means the INSTALLED package keeps running unchanged: the revision is
    discarded, the operator's own file is left exactly as they wrote it, and no
    hidden ``.bak-`` backup is left behind, because nothing was ever renamed."""
    pkg = _seed_package(monkeypatch, tmp_path)
    hostile = 'TOKEN="abcd\\"efgh"\n'  # the reversible spelling, arriving mid-session

    def add_an_unmaskable_env_mid_session() -> None:
        (pkg / ".env").write_text(hostile, encoding="utf-8")

    captured = _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=add_an_unmaskable_env_mid_session,
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_UNMATCHABLE
    assert "TOKEN" not in (outcome.error or "")  # category only, as ever
    assert captured  # the session DID run: only the entry gate could have stopped it
    assert (pkg / "run.py").read_text(encoding="utf-8") == "print('x')\n"  # never swapped
    assert (pkg / ".env").read_bytes() == hostile.encode("utf-8")  # the operator's file, untouched
    assert _leftovers(pkg.parent) == []  # nothing was renamed aside
    assert not (pkg.parent / tool_builder._STAGING_DIRNAME).exists()
    with tools._INFLIGHT_LOCK:
        assert not tools._INFLIGHT_SECRETS


def test_run_revise_refuses_when_the_staged_env_cannot_be_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the staged ``.env`` cannot be REMOVED after a mid-session deletion, the
    swap must not run (R3-1).

    "We could not make the revision match what the operator asked for" must never
    resolve to "publish the placeholder anyway". A DIRECTORY at ``<staging>/.env``
    -- which ``os.remove`` will not take -- is the shape an unjailed ``run_shell``
    (D21) can leave there, and refusing beats a destructive traversal on something
    this function neither created nor verified."""
    pkg = _seed_package(monkeypatch, tmp_path)
    root = pkg.parent
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")

    def delete_the_live_env_and_plant_a_directory() -> None:
        (pkg / ".env").unlink()
        (_staging_dir(root) / ".env").mkdir()

    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=delete_the_live_env_and_plant_a_directory,
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_DISCARD
    assert str(root) not in (outcome.error or "")  # category only: no path ...
    assert _TRICKY_ENV_VALUE not in (outcome.error or "")  # ... and no value
    assert (pkg / "run.py").read_text(encoding="utf-8") == "print('x')\n"  # never swapped
    assert _leftovers(root) == []


def test_run_revise_refuses_an_env_value_too_short_to_mask(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hand-edited ``.env`` holding a value UNDER the redactor's floor refuses
    the whole session, before any LLM call (R1-1).

    The floor is deliberate -- masking a 1-5 char value would shred ordinary prose
    -- so registering ``1234`` in flight protects nothing: the redactor is required
    to ignore it. A revise hands every ``.env`` value to ``run_shell``, whose
    output rides verbatim into the next round's prompt, into the AI 日誌 attempt
    bodies, and possibly into the summary -> job poll -> sidecar. So the only
    honest options are "do not run" and "leak". The install FORM already refuses a
    short secret for this exact reason; this is the same rule at the other entry
    point.

    The message names the CONDITION and the remedy and nothing else: naming the
    key or the value in the very error that refuses to expose it would be the leak
    it exists to prevent."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text("PIN=1234\n", encoding="utf-8")
    before = _file_bytes(pkg)
    captured = _fake_generate(
        monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY}
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_UNMASKABLE
    assert "PIN" not in (outcome.error or "")  # never the key
    assert "1234" not in (outcome.error or "")  # never the value
    assert captured == {}  # the builder session never started
    assert outcome.llm_log_id is None
    assert _file_bytes(pkg) == before  # the package is untouched
    assert not (pkg.parent / tool_builder._STAGING_DIRNAME).exists()
    with tools._INFLIGHT_LOCK:
        assert not tools._INFLIGHT_SECRETS  # nothing was registered either


def test_run_revise_allows_env_values_at_the_floor_and_ignores_empty_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The refusal above is the redactor's floor EXACTLY, on both edges (R1-1).

    A value at ``_MIN_SECRET_LEN`` is maskable, so it revises normally -- the gate
    must not creep past the property it enforces. An EMPTY value is not a secret
    at all: ``KEY=`` contributes nothing to ``known_secret_values`` (the same rule
    ``tools._cached_env_values`` applies), so it is never registered and can never
    make a package permanently unrevisable."""
    pkg = _seed_package(monkeypatch, tmp_path)
    assert tools._MIN_SECRET_LEN == 6  # the floor the fixture below is written to
    (pkg / ".env").write_text("EXACT=abcdef\nEMPTY=\n", encoding="utf-8")
    seen: dict[str, Any] = {}
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=lambda: seen.update(inflight=set(tools._INFLIGHT_SECRETS)),
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert seen["inflight"] == {"abcdef"}  # the empty value was not registered
    assert (pkg / ".env").read_text(encoding="utf-8") == "EXACT=abcdef\nEMPTY=\n"


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        # The finding's own example: the escape FIRES, so the raw line spells the
        # value ``ab'cd\"ef`` while dotenv hands us ``ab'cd"ef``.
        ("escaped quote", 'KEY="abcd\'ef\\"ghij"\n'),
        # A ``\n`` escape: the file holds two characters, the value holds a newline.
        ("newline escape", 'KEY="alpha\\nbravo"\n'),
        # ... and a ``\t`` one, so the pin is the CLASS and not one escape.
        ("tab escape", 'KEY="alpha\\tbravo"\n'),
    ],
)
def test_run_revise_refuses_an_env_value_the_file_spells_reversibly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, label: str, raw: str
) -> None:
    """A ``.env`` whose RAW spelling of a value is not the value refuses the whole
    session, before any LLM call (R5-1).

    Registration registers what dotenv PARSED, and every redactor we have matches
    that string and only that string. When the file spells it with escapes that
    FIRE, the two diverge -- and the builder's unjailed ``run_shell`` (D21) can
    ``cat`` the LIVE package's ``.env``, which is outside staging but perfectly
    reachable, putting the raw spelling in front of a redactor that matches
    nothing. From there it rides into the next round's prompt, the AI 日誌 attempt
    bodies, and possibly the summary -> job body -> sidecar.

    The INSTALL path already treats exactly this as a leak and refuses rather than
    write such a spelling (``_dotenv_serialize_value`` returns None once a ``"`` or
    ``\\`` co-occurs with a single quote, backstopped by
    ``_inject_secret_into_env``'s round-trip check). Install controls the spelling
    because it WRITES the file; a revise inherits whatever a hand-edit left, and
    until now inherited it without the guarantee. Same direction as the r1 short-
    value rule: "do not run" beats "leak".

    The message names the CONDITION and the remedy and nothing else -- printing
    the value in the error that refuses to expose it would be the leak itself."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text(raw, encoding="utf-8")
    parsed = tools._parse_dotenv_text(raw)["KEY"]
    assert len(parsed) >= tools._MIN_SECRET_LEN  # not the r1 floor: the SPELLING, label
    assert parsed not in raw  # ... and this is what "reversible" means, concretely
    before = _file_bytes(pkg)
    captured = _fake_generate(
        monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY}
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_UNMATCHABLE
    assert "KEY" not in (outcome.error or "")  # never the key
    assert parsed not in (outcome.error or "")  # never the value
    assert captured == {}  # the builder session never started
    assert outcome.llm_log_id is None
    assert _file_bytes(pkg) == before  # the package is untouched
    assert not (pkg.parent / tool_builder._STAGING_DIRNAME).exists()
    with tools._INFLIGHT_LOCK:
        assert not tools._INFLIGHT_SECRETS  # nothing was registered either


def test_run_revise_refuses_a_shadowed_line_that_spells_the_value_reversibly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A SHADOWED assignment can spell the live credential reversibly while the
    winning one spells it safely, and checking only the deciding line missed it
    (R8-1).

    dotenv is last-wins, so the parsed value comes from the second line and the
    per-key spelling check passes on it. But a ``cat`` of the file shows BOTH
    lines: the redactor masks the second (it equals the registered value) and
    hands the model the first, which encodes the very same credential through an
    escape. The reasoning that let shadowed lines through -- "dotenv discarded it,
    so it holds no registered value" -- was simply false: what dotenv discards can
    still SPELL what dotenv kept. Every assignment line is now shape-checked, and
    a shadowed line spelled plainly (the ordinary "I changed this value" edit)
    still passes, which the sibling test below pins."""
    pkg = _seed_package(monkeypatch, tmp_path)
    raw = 'KEY="abcd\\"efgh"\nKEY=\'abcd"efgh\'\n'
    (pkg / ".env").write_text(raw, encoding="utf-8")
    parsed = tools._parse_dotenv_text(raw)["KEY"]
    assert parsed == 'abcd"efgh'
    # The WINNING line spells it literally -- which is exactly why the per-key
    # check passes and why the shadowed line had to be checked separately.
    assert "'" + parsed + "'" in raw
    before = _file_bytes(pkg)
    captured = _fake_generate(
        monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY}
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_UNMATCHABLE
    assert parsed not in (outcome.error or "")  # never the value
    assert captured == {}  # the builder session never started
    assert _file_bytes(pkg) == before
    with tools._INFLIGHT_LOCK:
        assert not tools._INFLIGHT_SECRETS


def test_run_revise_refuses_a_backslash_escape_inside_single_quotes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """python-dotenv decodes ``\\\\`` inside SINGLE quotes too, so a shadowed
    single-quoted line can spell the winner's credential reversibly (R9-1).

    This is the case that killed the first shape check: it stripped matching
    quotes and treated the remainder as literal, which is true of POSIX shell
    single quotes and NOT true of this parser -- verified against the pinned
    library rather than assumed. The file below holds two backslashes on the
    shadowed line and one on the winning line; both mean the same live value, the
    redactor matches only the winner, and a ``cat`` hands the model the other."""
    pkg = _seed_package(monkeypatch, tmp_path)
    raw = "TOKEN='abc\\\\defghi'\nTOKEN='abc\\defghi'\n"
    (pkg / ".env").write_text(raw, encoding="utf-8")
    parsed = tools._parse_dotenv_text(raw)["TOKEN"]
    assert parsed == "abc\\defghi"  # ONE backslash: the shadowed line is reversible
    before = _file_bytes(pkg)
    captured = _fake_generate(
        monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY}
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_UNMATCHABLE
    assert parsed not in (outcome.error or "")
    assert captured == {}  # refused before the session
    assert _file_bytes(pkg) == before


def test_run_revise_allows_a_single_quoted_backslash_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The R9-1 rule refuses the reversible SPELLING, not backslashes as such: a
    value that genuinely contains one, written the way our own serializer would
    write it, still revises -- otherwise a Windows path in a .env would make a
    package permanently unrevisable."""
    pkg = _seed_package(monkeypatch, tmp_path)
    raw = "TOKEN='abc\\defghi'\n"
    (pkg / ".env").write_text(raw, encoding="utf-8")
    _fake_generate(monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY})

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert (pkg / ".env").read_text(encoding="utf-8") == raw


def test_run_revise_refuses_when_the_package_was_reinstalled_mid_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A package of the same NAME is not the same package (R10-1).

    An operator can delete and reinstall the tool during the minutes a build
    runs. Every earlier gate still passes -- the directory exists, is no symlink,
    has no finalized sidecar -- so without an identity check the revise would
    rename the operator's NEW package aside, publish a revision of the OLD
    snapshot, and then delete the backup: the new package's files gone, silently.
    The identity is the directory's own inode, captured before the session."""
    pkg = _seed_package(monkeypatch, tmp_path)
    base = pkg.parent
    keep = "print('the operator reinstalled this')"

    def reinstall(*_args: object, **_kwargs: object) -> None:
        # Delete-and-reinstall: a NEW directory under the SAME name.
        shutil.rmtree(pkg)
        pkg.mkdir()
        (pkg / "tool.json").write_text(json.dumps(_package_manifest("kbsearch")), encoding="utf-8")
        (pkg / "run.py").write_text(keep, encoding="utf-8")

    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=reinstall,
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_TARGET_REPLACED
    # the operator's reinstalled package is exactly as they left it
    assert (pkg / "run.py").read_text(encoding="utf-8") == keep
    assert not any(entry.name.startswith(".kbsearch.bak-") for entry in base.iterdir())


def test_run_revise_refuses_a_replacement_that_lands_during_the_env_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The identity re-check has to sit AFTER the ``.env`` copy, not before it
    (R11-1).

    The copy reads through the target and takes an operator-influenced amount of
    time, so a check that ran before it left that entire window unguarded: a
    package replaced DURING the copy would still be renamed aside and overwritten
    by a revision of the package that no longer exists."""
    pkg = _seed_package(monkeypatch, tmp_path)
    base = pkg.parent
    (pkg / ".env").write_text("TOKEN=abcdefgh\n", encoding="utf-8")
    keep = "print('installed while the copy ran')"
    real_copy = shutil.copy2

    def replace_during_copy(src: Any, dst: Any, **kwargs: Any) -> Any:
        result = real_copy(src, dst, **kwargs)
        shutil.rmtree(pkg)
        pkg.mkdir()
        (pkg / "tool.json").write_text(json.dumps(_package_manifest("kbsearch")), encoding="utf-8")
        (pkg / "run.py").write_text(keep, encoding="utf-8")
        return result

    _fake_generate(monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY})
    monkeypatch.setattr(shutil, "copy2", replace_during_copy)

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_TARGET_REPLACED
    assert (pkg / "run.py").read_text(encoding="utf-8") == keep
    assert not any(entry.name.startswith(".kbsearch.bak-") for entry in base.iterdir())


def test_run_revise_refuses_a_package_whose_identity_cannot_be_established(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No identity, no session (R11-2): uncertainty refuses here like everywhere
    else on this path.

    The resolver admits a directory with no ``tool.json`` (a broken package), and
    a transient stat failure looks the same. Starting anyway would leave the
    pre-swap check with nothing to compare against -- so it would either wave the
    swap through, which is precisely the loss R10-1 closed, or refuse after the
    whole build had already been paid for."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / "tool.json").unlink()
    captured = _fake_generate(
        monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY}
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_IDENTITY_UNKNOWN
    assert captured == {}  # refused before the session


def test_run_revise_refuses_a_replacement_that_lands_during_the_sidecar_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The identity re-check must be the LAST thing before the first rename
    (R12-1).

    It moved twice for the same reason: r10 placed it before the ``.env`` copy,
    r11 before the sidecar read, and each left a window in which a replacement
    still got overwritten. The sidecar read is the sharpest version of the
    problem -- it can answer ``draft`` from the OLD package's file for the very
    swap it is meant to guard. Driven here by replacing the package from inside
    the status read, so the test pins the ORDER and not merely the check."""
    pkg = _seed_package(monkeypatch, tmp_path)
    base = pkg.parent
    keep = "print('installed while the sidecar was read')"
    real_status = tools.summary_status_or_unknown

    def replace_during_status(directory: Any) -> Any:
        result = real_status(directory)
        if pkg.exists():
            shutil.rmtree(pkg)
            pkg.mkdir()
            (pkg / "tool.json").write_text(
                json.dumps(_package_manifest("kbsearch")), encoding="utf-8"
            )
            (pkg / "run.py").write_text(keep, encoding="utf-8")
        return result

    _fake_generate(monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY})
    monkeypatch.setattr(tools, "summary_status_or_unknown", replace_during_status)

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_TARGET_REPLACED
    assert (pkg / "run.py").read_text(encoding="utf-8") == keep
    assert not any(entry.name.startswith(".kbsearch.bak-") for entry in base.iterdir())


def test_run_revise_allows_a_plainly_spelled_shadowed_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The R8-1 rule is about SPELLING, not about duplicate keys: an ordinary
    edit history (an old value left above the new one, both written plainly) is
    not a leak and must still revise, or the guard would refuse the most common
    hand-edit there is."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text("KEY=oldvalue\nKEY=newvalue\n", encoding="utf-8")
    _fake_generate(monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY})

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert (pkg / ".env").read_text(encoding="utf-8") == "KEY=oldvalue\nKEY=newvalue\n"


def test_run_revise_allows_env_values_the_file_spells_literally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The refusal above is the LITERAL-substring property exactly -- read off the
    ASSIGNMENT LINE (R6-1) -- and ordinary ``.env`` spellings keep revising (R5-1).

    Five shapes whose own raw line embeds the value verbatim, so a ``cat`` of the
    file puts in front of the redactor precisely the string that was registered:
    unquoted, simply double-quoted, single-quoted around a ``"`` (dotenv reads
    single quotes literally -- no escapes can fire), an ``export`` prefix, and
    whitespace around the ``=``. The last two are here because the RHS is taken by
    splitting on the first ``=`` and stripping: a gate that crept past the property
    it enforces would make ordinary packages permanently unrevisable with a message
    naming no key and no value to explain why. A genuinely MULTI-LINE quoted value
    used to be in this list and is now a refusal -- see
    ``test_run_revise_refuses_a_value_its_assignment_line_cannot_vouch_for``."""
    pkg = _seed_package(monkeypatch, tmp_path)
    raw = (
        'PLAIN=abcdef\nQUOTED="ghijkl"\nSINGLE=\'mno"pqr\'\n'
        "export EXPORTED=stuvwx\nSPACED = yzabcd \n"
    )
    (pkg / ".env").write_text(raw, encoding="utf-8")
    seen: dict[str, Any] = {}
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=lambda: seen.update(inflight=set(tools._INFLIGHT_SECRETS)),
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert seen["inflight"] == {"abcdef", "ghijkl", 'mno"pqr', "stuvwx", "yzabcd"}
    assert (pkg / ".env").read_text(encoding="utf-8") == raw
    assert (pkg / "run.py").read_text(encoding="utf-8") == _REVISED_RUN_PY


def test_run_revise_refuses_a_value_only_an_unrelated_line_spells_literally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A COMMENT repeating the value cannot vouch for the assignment that holds it
    (R6-1).

    The r5 gate searched the WHOLE TEXT, which asks "does this string appear
    anywhere" -- and here the answer is yes for a reason that protects nothing. The
    assignment still spells the credential with an escape that FIRES, so the raw
    line matches no registered value; a builder ``cat``-ing the LIVE package's
    ``.env`` (outside staging, perfectly reachable -- D21 leaves ``run_shell``
    unjailed) emits that spelling past every redactor, and the value is trivially
    recoverable from it. The only spelling that can answer for a value is the one
    on the line that DETERMINES it.

    ``parsed in raw`` is asserted first, so this test would have PASSED the r5 gate
    -- that is precisely what makes it the regression."""
    pkg = _seed_package(monkeypatch, tmp_path)
    raw = 'TOKEN="abcd\\"efgh"\n# abcd"efgh\n'
    (pkg / ".env").write_text(raw, encoding="utf-8")
    parsed = tools._parse_dotenv_text(raw)["TOKEN"]
    assert len(parsed) >= tools._MIN_SECRET_LEN  # not the r1 floor: the SPELLING
    assert parsed in raw  # the whole-text search the r5 gate did says "fine"
    assert parsed not in raw.splitlines()[0]  # ... the assignment itself says otherwise
    before = _file_bytes(pkg)
    captured = _fake_generate(
        monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY}
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_UNMATCHABLE
    assert "TOKEN" not in (outcome.error or "")  # never the key
    assert parsed not in (outcome.error or "")  # never the value
    assert captured == {}  # the builder session never started
    assert outcome.llm_log_id is None
    assert _file_bytes(pkg) == before  # the package is untouched
    assert not (pkg.parent / tool_builder._STAGING_DIRNAME).exists()
    with tools._INFLIGHT_LOCK:
        assert not tools._INFLIGHT_SECRETS  # nothing was registered either


def test_run_revise_refuses_a_value_a_comment_on_its_own_line_vouches_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The r6 finding again, moved ONTO the assignment line -- and refused (R7-1).

    r5 searched the whole FILE, so a comment on the next line vouched for the
    credential; r6 narrowed the search to the assignment's raw RHS, and the very
    same trick fits there: a trailing comment repeating the value makes the parsed
    string a substring of that RHS, while the part that actually assigns it still
    spells it with an escape that FIRES. python-dotenv consumes the quoted value
    and the comment separately, so the two halves of one line answer for each
    other and the credential a ``cat`` prints is still unmatchable.

    Partial matching has now been beaten twice at two scales, which is why the
    gate stopped asking "is it in there" and started asking "is this a spelling we
    could have written". ``parsed in rhs`` is asserted first, so this test would
    have PASSED the r6 gate -- that is what makes it the regression."""
    pkg = _seed_package(monkeypatch, tmp_path)
    raw = 'TOKEN="abcd\\"efgh" # abcd"efgh"\n'
    (pkg / ".env").write_text(raw, encoding="utf-8")
    parsed = tools._parse_dotenv_text(raw)["TOKEN"]
    assert len(parsed) >= tools._MIN_SECRET_LEN  # not the r1 floor: the SPELLING
    rhs = tool_builder._env_assignment_rhs(raw)["TOKEN"]
    assert parsed in rhs  # the r6 same-LINE containment check says "fine"
    assert rhs not in tool_builder._dotenv_safe_spellings(parsed)  # equality says otherwise
    before = _file_bytes(pkg)
    captured = _fake_generate(
        monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY}
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_UNMATCHABLE
    assert "TOKEN" not in (outcome.error or "")  # never the key
    assert parsed not in (outcome.error or "")  # never the value
    assert captured == {}  # the builder session never started
    assert outcome.llm_log_id is None
    assert _file_bytes(pkg) == before  # the package is untouched
    assert not (pkg.parent / tool_builder._STAGING_DIRNAME).exists()
    with tools._INFLIGHT_LOCK:
        assert not tools._INFLIGHT_SECRETS  # nothing was registered either


def test_run_revise_refuses_a_plain_value_carrying_a_trailing_comment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``KEY=value # comment`` is legitimate dotenv, and this now REFUSES it
    (R7-1). Pinned deliberately, because it is the accepted cost.

    Nothing is wrong with this line: dotenv drops the comment, the value is
    spelled plainly, and a ``cat`` would put the registered string in front of the
    redactor. It refuses because the gate no longer accepts "the value is in
    there somewhere on the line" -- a rule that admits adjacent text has now been
    defeated twice by adjacent text, once per scale (r5 whole-file, r6 same-line).
    The only spellings that survive are the ones this system can GENERATE, and a
    trailing comment is not one of them; it is also indistinguishable, short of
    re-implementing dotenv's tokenizer, from the escaped line above with a comment
    stapled on.

    The cost is bounded by construction: an install never writes this shape (its
    line is ``KEY=<serialized>`` and nothing else), so no package this system
    produced becomes unrevisable -- and the remedy the message names, write the
    line plainly, is one edit."""
    pkg = _seed_package(monkeypatch, tmp_path)
    raw = "KEY=abcdef # 這一行結尾的註解\n"
    (pkg / ".env").write_text(raw, encoding="utf-8")
    parsed = tools._parse_dotenv_text(raw)["KEY"]
    assert parsed == "abcdef"  # dotenv really does drop the comment
    assert parsed in tool_builder._env_assignment_rhs(raw)["KEY"]  # r6 would have passed it
    before = _file_bytes(pkg)
    captured = _fake_generate(
        monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY}
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_UNMATCHABLE
    assert captured == {}  # refused before the session, like every other .env gate
    assert _file_bytes(pkg) == before
    with tools._INFLIGHT_LOCK:
        assert not tools._INFLIGHT_SECRETS


def test_run_revise_refuses_a_value_its_assignment_line_cannot_vouch_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A MULTI-LINE quoted value has no single assignment line to check, so it
    refuses (R6-1).

    Stated honestly: this one is not a demonstrated leak. The value's newlines are
    real newlines in the file, so a ``cat`` really would put the registered string
    in front of the redactor. It refuses because the check is line-based and a
    single line can never contain a newline -- nothing here can tell this spelling
    apart from one whose first fragment merely happens to sit on the opening line.
    Verifying what we can READ and refusing what we cannot is the direction the
    whole path takes (an unreadable ``.env`` stops the revise; an untrustworthy
    sidecar stops the swap), and it is a deliberate narrowing of what r5 accepted.
    The remedy the message names -- simplify the quoting -- is the right advice
    here too."""
    pkg = _seed_package(monkeypatch, tmp_path)
    raw = 'SPAN="line-one\nline-two"\n'
    (pkg / ".env").write_text(raw, encoding="utf-8")
    parsed = tools._parse_dotenv_text(raw)["SPAN"]
    assert parsed == "line-one\nline-two"  # dotenv really does span the two lines
    before = _file_bytes(pkg)
    captured = _fake_generate(
        monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY}
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_UNMATCHABLE
    assert captured == {}  # the builder session never started
    assert _file_bytes(pkg) == before
    with tools._INFLIGHT_LOCK:
        assert not tools._INFLIGHT_SECRETS


@pytest.mark.parametrize(
    ("label", "raw", "refused"),
    [
        # dotenv is LAST-occurrence-wins, so the LAST line decides the value --
        # but a reversible EARLIER line is refused too, and this expectation was
        # INVERTED by R8-1: r6 pinned it as passing on the reasoning that a
        # shadowed line "holds no registered value", which is false when the
        # shadowed spelling encodes the very value the winner spells safely.
        # Every assignment line is shape-checked now, so this refuses.
        ("shadowed line is reversible", 'KEY="ab\\"cdefg"\nKEY=abcdef\n', True),
        # ... and the winning line is still checked against the parsed value, so a
        # plain earlier line does not excuse the one that wins.
        ("last assignment is reversible", 'KEY=abcdef\nKEY="ab\\"cdefg"\n', True),
        # The ordinary edit history -- both lines plain -- still revises. The rule
        # is about SPELLING, never about duplicate keys.
        ("both lines plain", "KEY=oldvalue\nKEY=abcdef\n", False),
    ],
)
def test_run_revise_checks_the_last_assignment_of_a_duplicated_env_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, label: str, raw: str, refused: bool
) -> None:
    """With a key assigned twice, the gate follows dotenv: the LAST line wins
    (R6-1).

    Both directions are pinned because both are wrong under a whole-text search --
    it would pass the first case for the right reason and the second for the wrong
    one (an earlier, dead line spelling something else literally). The registered
    value comes from the LAST assignment, so that is the line whose spelling has to
    be able to vouch for it; an earlier, shadowed line is ordinary file text with no
    registered secret behind it. ``_env_line_key`` is the same key recognizer
    ``_inject_secret_into_env`` uses to drop prior lines, for this identical
    last-wins reason."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text(raw, encoding="utf-8")
    parsed = tools._parse_dotenv_text(raw)["KEY"]
    assert len(parsed) >= tools._MIN_SECRET_LEN  # never the r1 floor, always the SPELLING
    seen: dict[str, Any] = {}
    captured = _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=lambda: seen.update(inflight=set(tools._INFLIGHT_SECRETS)),
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    if refused:
        assert outcome.ok is False
        assert outcome.error == tool_builder._ERROR_REVISE_ENV_UNMATCHABLE
        assert captured == {}  # the builder session never started
    else:
        assert outcome.ok is True
        assert seen["inflight"] == {parsed}  # the LAST line's value, registered
        assert (pkg / ".env").read_text(encoding="utf-8") == raw


def _case_sensitive_filesystem(root: Path) -> bool:
    """True when ``root`` tells ``.env`` and ``.ENV`` apart.

    PROBED rather than inferred from ``sys.platform``: a Linux host can mount a
    case-insensitive filesystem and macOS can be formatted case-sensitive, and
    what the R5-2 tests need is the behaviour of THIS ``tmp_path``, not the
    platform's usual default."""
    probe = root / ".case-probe"
    probe.write_text("x", encoding="utf-8")
    try:
        return not (root / ".CASE-PROBE").exists()
    finally:
        probe.unlink()


def test_run_revise_keeps_a_root_env_case_variant_that_is_a_different_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a case-SENSITIVE filesystem ``.ENV`` is an ORDINARY package file, so a
    revise copies it and publishes it unchanged (R5-2).

    The r2 rule excluded every root name that casefolded to ``.env``, which is the
    right answer only where the two names ARE one file. Here they are two, and
    ``_preserve_env_file`` only ever restores the exact ``.env`` -- so the variant
    was copied nowhere, restored nowhere, and an unrelated revise DELETED it while
    ``validate_package`` passed the mutilated package. That is the same failure
    R2-2 removed for nested ``.env`` files, surviving at the root under a different
    name. Both sides are pinned: the builder SEES the variant (it is content), the
    managed ``.env`` is still withheld from it, and the published package has
    both, byte for byte."""
    if not _case_sensitive_filesystem(tmp_path):
        pytest.skip("this filesystem folds .env and .ENV into one file")
    pkg = _seed_package(monkeypatch, tmp_path)
    root = pkg.parent
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")
    # Ordinary content, and deliberately NOT holding a registered value -- one that
    # did would be refused by the embedded-secret gate, which is R2-2's stated and
    # accepted consequence for any second file carrying a live credential.
    variant = b"# the tool's own case-variant file\r\nMODE=verbose\n"
    (pkg / ".ENV").write_bytes(variant)
    live = (pkg / ".env").read_bytes()
    seen: dict[str, set[str]] = {}
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=lambda: seen.update(staged=_tree(_staging_dir(root))),
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert ".ENV" in seen["staged"]  # ordinary content: the builder gets to see it
    assert ".env" not in seen["staged"]  # the managed credentials file is withheld
    assert (pkg / ".ENV").read_bytes() == variant  # survived, byte for byte
    assert (pkg / ".env").read_bytes() == live  # ... and the real one came back


def test_run_revise_keeps_a_hard_linked_root_env_case_variant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A HARD-LINKED ``.ENV`` is a second DIRECTORY ENTRY and survives the revise
    (R6-2).

    This is the case the r5 inode rule got wrong. ``.env`` and ``.ENV`` here share
    ``st_dev``/``st_ino``, so "same file" said yes and BOTH names were withheld from
    the copy -- while ``_preserve_env_file`` restores only the exact ``.env``. After
    a successful publish and the backup drop, ``.ENV`` was simply GONE: the very
    failure R5-2 set out to remove, arriving through the test it chose. The
    filesystem's own LISTING answers the real question: it carries BOTH names here,
    so the variant is the package's content and is copied.

    The ``.env`` deliberately holds no registered value. A hard link means the two
    names have the SAME bytes, so a credential-bearing pair would put those
    credentials in staging and be refused by ``validate_package``'s embedded-secret
    gate -- R2-2's stated, accepted consequence for a second file holding a live
    credential, and not the property under test here.

    Both names come through with their bytes; they are independent files afterwards,
    since ``copytree`` has never preserved hard-link identity."""
    if not _case_sensitive_filesystem(tmp_path):
        # There, the two names ARE one entry and ``os.link`` below cannot even be
        # asked for; the branch that covers that world is the unit test after this.
        pytest.skip("this filesystem folds .env and .ENV into one file")
    pkg = _seed_package(monkeypatch, tmp_path)
    root = pkg.parent
    shared = "# two names, one inode, no credentials\nMODE=\n"
    (pkg / ".env").write_text(shared, encoding="utf-8")
    os.link(pkg / ".env", pkg / ".ENV")  # one file, two names
    assert (pkg / ".env").stat().st_ino == (pkg / ".ENV").stat().st_ino
    seen: dict[str, set[str]] = {}
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=lambda: seen.update(staged=_tree(_staging_dir(root))),
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert ".ENV" in seen["staged"]  # a distinct entry: the builder sees it
    assert ".env" not in seen["staged"]  # ... the managed one is still withheld
    assert (pkg / ".ENV").read_text(encoding="utf-8") == shared  # PUBLISHED, not deleted
    assert (pkg / ".env").read_text(encoding="utf-8") == shared  # ... and restored


def test_is_preserved_env_name_excludes_the_variant_a_lone_listing_carries(
    tmp_path: Path,
) -> None:
    """The one shape only a case-INSENSITIVE filesystem produces, tested directly
    (R6-2).

    ``run_revise`` cannot reach it on this host: a case-insensitive filesystem
    carries ONE entry for the pair, which is a listing without ``.env`` where
    ``<root>/.env`` still opens the variant -- and no case-sensitive host can
    produce that combination end to end. Passing ``exact_present=False`` over a
    hard-linked pair reproduces exactly what the callback would see there, so the
    branch that keeps live credentials out of staging on macOS is pinned rather
    than assumed.

    The rest of the matrix goes with it, because the discriminator is the pair of
    conditions and not either half: the exact name always excluded, the variant
    NEVER excluded once the listing also carries ``.env`` (however the two are
    linked), no ``.env`` at all meaning nothing to be the same entry as, and a
    SYMLINK staying ordinary content -- the reason the identity test is ``lstat``
    and not a following stat, since excluding a distinct entry that is never
    restored is the deletion bug all over again."""
    if not _case_sensitive_filesystem(tmp_path):
        # The fixtures below need two entries to exist at once to SIMULATE the one
        # this branch is about; a case-insensitive host cannot hold them.
        pytest.skip("this filesystem folds .env and .ENV into one file")
    root = tmp_path / "pkg"
    root.mkdir()
    (root / ".env").write_text("MODE=\n", encoding="utf-8")
    os.link(root / ".env", root / ".ENV")

    # The exact name is the managed file whatever else the listing holds.
    assert tool_builder._is_preserved_env_name(root, ".env", exact_present=True) is True
    assert tool_builder._is_preserved_env_name(root, ".env", exact_present=False) is True
    # A listing WITHOUT the exact name = the case-insensitive one-entry world.
    assert tool_builder._is_preserved_env_name(root, ".ENV", exact_present=False) is True
    # A listing WITH it = two entries, so the variant is the package's own content.
    assert tool_builder._is_preserved_env_name(root, ".ENV", exact_present=True) is False
    # An unrelated name is never in question.
    assert tool_builder._is_preserved_env_name(root, "env", exact_present=False) is False

    (root / ".ENV").unlink()
    (root / ".env").unlink()
    # Nothing for the variant to BE the same entry as -> ordinary content.
    (root / ".ENV").write_text("MODE=\n", encoding="utf-8")
    assert tool_builder._is_preserved_env_name(root, ".ENV", exact_present=False) is False
    (root / ".ENV").unlink()

    # A SYMLINK is a distinct entry: lstat keeps it one, a following stat would not.
    (root / ".env").write_text("MODE=\n", encoding="utf-8")
    (root / ".ENV").symlink_to(".env")
    assert tool_builder._is_preserved_env_name(root, ".ENV", exact_present=False) is False


def test_run_revise_keeps_a_root_env_case_variant_that_is_a_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``.ENV`` SYMLINK to ``.env`` is a distinct directory entry and survives the
    revise as a link (R5-2).

    Two independent reasons keep it, and that is the point of still having this
    test after R6-2: the LISTING carries both names here, so the variant is content
    before any stat is reached, AND the identity test is ``os.lstat`` rather than
    ``os.stat`` / ``os.path.samefile``, so the link is not conflated with its target
    where the listing cannot help (a lone variant -- see the unit test above).
    Either rule alone gets this right; a following stat plus the r5 inode rule got
    it wrong, and since only the exact ``.env`` is ever restored, the link would
    have been silently deleted. ``copytree`` copies it AS a link
    (``symlinks=True``), so it is dangling inside staging and resolves again the
    moment ``_preserve_env_file`` puts ``.env`` back."""
    if not _case_sensitive_filesystem(tmp_path):
        pytest.skip("this filesystem folds .env and .ENV into one file")
    pkg = _seed_package(monkeypatch, tmp_path)
    root = pkg.parent
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")
    (pkg / ".ENV").symlink_to(".env")
    seen: dict[str, set[str]] = {}
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=lambda: seen.update(staged=_tree(_staging_dir(root))),
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert ".ENV" in seen["staged"]  # the link itself is package content
    assert ".env" not in seen["staged"]
    assert (pkg / ".ENV").is_symlink()  # still a link, not a materialized copy
    assert (pkg / ".ENV").read_text(encoding="utf-8") == _TRICKY_ENV  # ... and it resolves


def test_run_revise_treats_a_lone_env_case_variant_as_having_no_managed_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A package with ``.ENV`` and NO ``.env`` keeps the variant and counts as
    having no managed ``.env`` at all (R5-2).

    Nothing here is the credentials file: ``tools._load_tool_dotenv`` opens the
    exact ``.env`` on this filesystem and finds none, so nothing was registered and
    nothing has to be restored. The standing R3-1 acceptance therefore applies in
    its "never had one" branch -- the builder's own ``.env`` ships, exactly as it
    does on an install -- and it is asserted here because it is the observable
    proof that the variant was not mistaken for the managed file.

    The builder's value is a MASKABLE one on purpose: that branch now runs the
    same ``.env`` policy over what it is about to ship, so a short or reversibly
    spelled value would be refused (see
    ``test_run_revise_refuses_a_builder_env_the_redactors_could_not_mask``) and
    this test would stop being about case variants at all."""
    if not _case_sensitive_filesystem(tmp_path):
        pytest.skip("this filesystem folds .env and .ENV into one file")
    pkg = _seed_package(monkeypatch, tmp_path)
    root = pkg.parent
    variant = b"# ordinary content, no credentials\r\nMODE=verbose\n"
    (pkg / ".ENV").write_bytes(variant)
    written_by_the_model = "WRITTEN_BY_THE_MODEL=yes-it-really-was\n"
    seen: dict[str, set[str]] = {}
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY, ".env": written_by_the_model},
        side_effect=lambda: seen.update(staged=_tree(_staging_dir(root))),
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert ".ENV" in seen["staged"]  # copied like any other file
    assert (pkg / ".ENV").read_bytes() == variant  # ... and published unchanged
    # "never had one" -> the builder's .env ships, which is only true if the
    # variant was NOT read as the managed file.
    assert (pkg / ".env").read_text(encoding="utf-8") == written_by_the_model


def test_run_revise_refuses_an_env_over_the_byte_ceiling_that_fits_in_chars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ENTRY gate measures the ``.env`` in BYTES, the same unit promote does
    (R5-3).

    It used to measure decoded CHARS, and for a CJK-heavy ``.env`` the two answers
    differ by a factor of three: ~30k characters is ~90 KB, so the file cleared the
    entry gate, bought a full multi-round builder session, and was only then
    refused by ``_preserve_env_file``'s byte check at promote -- guaranteed-to-fail
    work, charged in full, on every single retry. Both measurements are asserted
    here so the fixture cannot drift into proving something else."""
    pkg = _seed_package(monkeypatch, tmp_path)
    oversized = "# " + "測" * 30_000 + "\nKB_API_KEY=live-secret-value\n"
    (pkg / ".env").write_text(oversized, encoding="utf-8")
    assert len(oversized) <= tools._ENV_FILE_MAX_BYTES  # under the OLD char rule
    assert (pkg / ".env").stat().st_size > tools._ENV_FILE_MAX_BYTES  # over the real one
    before = _file_bytes(pkg)
    captured = _fake_generate(
        monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY}
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_ENV_TOO_LARGE
    assert captured == {}  # no session was bought for a build promote would refuse
    assert outcome.llm_log_id is None
    assert _file_bytes(pkg) == before
    assert not (pkg.parent / tool_builder._STAGING_DIRNAME).exists()
    with tools._INFLIGHT_LOCK:
        assert not tools._INFLIGHT_SECRETS


def test_run_revise_accepts_an_env_at_the_byte_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The refusal above is the ceiling EXACTLY: a ``.env`` AT
    ``_ENV_FILE_MAX_BYTES`` still revises, and its credentials come back.

    The same edge the promote-side copy is pinned at, at the other end of the
    session -- an entry gate that crept one byte past the property it enforces
    would make a working package unrevisable with a message that names no key and
    no value to explain why."""
    pkg = _seed_package(monkeypatch, tmp_path)
    tail = "KB_API_KEY=live-secret-value\n"
    raw = "#" + "p" * (tools._ENV_FILE_MAX_BYTES - len(tail) - 2) + "\n" + tail
    (pkg / ".env").write_text(raw, encoding="utf-8")
    assert (pkg / ".env").stat().st_size == tools._ENV_FILE_MAX_BYTES  # the cap EXACTLY
    seen: dict[str, Any] = {}
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=lambda: seen.update(inflight=set(tools._INFLIGHT_SECRETS)),
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert seen["inflight"] == {"live-secret-value"}
    assert (pkg / ".env").read_text(encoding="utf-8") == raw  # preserved across the swap


def test_run_revise_prompts_carry_the_feedback_but_never_the_env_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """What the model is told, and what it is never told.

    The system prompt names the package and states the identity rule and the
    withheld ``.env``; the user prompt carries the feedback verbatim and the
    current manifest. No ``.env`` VALUE appears in either -- the values reach the
    build only through run_shell's environment."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")
    captured = _fake_generate(
        monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY}
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "回傳結果要加上分頁參數"))

    assert outcome.ok is True
    system_prompt = captured["system_prompt"]
    user_prompt = captured["user_prompt"]
    assert "REVISION MODE" in system_prompt
    assert '"tool_name" MUST be exactly kbsearch' in system_prompt
    assert "回傳結果要加上分頁參數" in user_prompt
    assert '"name": "kbsearch"' in user_prompt  # the current tool.json rides along
    for value in (_TRICKY_ENV_VALUE, "plain-value"):
        assert value not in system_prompt
        assert value not in user_prompt


def test_revise_system_prompt_redacts_the_name_but_not_its_own_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The revise system prompt redacts its INTERPOLATION and nothing else (R2-4).

    The package name is operator/filesystem-derived, so a registered ``.env`` value
    can equal it -- and unredacted it would ride into the request verbatim while
    every other text in the session is masked. The constant text AROUND it is
    backend-authored open-source prose and is deliberately left alone: masking it
    would be useless (a value equal to published constant text is not something the
    model learns from us) and destructive (a hand-edited ``TOKEN=secret`` clears the
    6-char floor, and our own instructions say "secret" -- whole-prompt masking
    would shred them). Both halves are asserted together, because it is the LINE
    between them that is the decision."""
    _seed_package(monkeypatch, tmp_path)
    tools.register_inflight_secret("kbsearch")  # a .env value equal to the tool's NAME
    tools.register_inflight_secret("secret")  # ... and one equal to a word WE wrote

    prompt = tool_builder._revise_system_prompt("kbsearch")

    assert "kbsearch" not in prompt  # the interpolation is masked
    assert tools._REDACTION_MARKER in prompt
    # ... while our own sentences containing that same value are untouched.
    assert "Never write a secret value into any file." in prompt
    assert "Never print or embed secrets in the summary." in prompt


@pytest.mark.parametrize("model_name", ["kbsearch", "kbsearch2"])
def test_run_revise_never_redacts_or_builds_its_prompt_on_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model_name: str
) -> None:
    """``run_revise``'s prompt builds and every redaction it performs hop onto a
    worker (R1-3, R2-4).

    None of it is the pure string work it looks like: ``redact_known_secrets``
    calls ``known_secret_values``, which ``iterdir``s the whole tools directory,
    ``stat``s every package and READS every ``.env`` on a cache miss. A revise
    runs from a BACKGROUND job that shares this loop with every HTTP request in
    the process, so blocking there stalls all of them -- the same reason P3a moved
    ``tool_meta._summary_user_prompt`` off it.

    The ``.env`` VALUE PARSE is asserted the same way and for the same reason
    (R4-3). The READ was already on a worker, but ``python-dotenv`` was then handed
    the text back on the LOOP -- and a near-64 KiB ``.env`` full of quoting is a
    real parse, not a formality. It is pinned by IDENTITY, not merely by "not the
    loop": the parse must happen on the SAME worker as the read, because "one hop
    that reads and parses" is the fix, and a second hop would be a different (and
    still wrong) design that a not-the-loop assertion alone would accept.

    Asserted by THREAD, following that suite's pattern: ``asyncio.run`` drives the
    loop on this thread, so a different one means the hop really happened. Both
    outcome shapes are covered because the model-renamed refusal branch holds one
    of the redactions. The SYSTEM prompt build joined this list when its
    interpolated package name became redactable (R2-4): it was a pure join before,
    and it is the tools-directory scan now.

    Two calls are deliberately NOT asserted off-loop here: ``InstallResult``'s own
    validator redaction (裁決紀錄 #2's consciously deferred instance, which runs
    inside pydantic validation during ``generate_structured``), and the post-swap
    summary hook, stubbed out below so the summary text's LAST redaction is
    unambiguously ``run_revise``'s own."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text(_TRICKY_ENV, encoding="utf-8")
    loop_thread = threading.current_thread()
    seen: dict[str, Any] = {}
    calls: list[tuple[str, threading.Thread]] = []
    parses: list[tuple[str, threading.Thread]] = []
    real_prompt = tool_builder._revise_user_prompt
    real_redact = tools.redact_known_secrets
    real_env_read = tool_builder._read_env_for_values
    real_parse = tools._parse_dotenv_text

    def prompt_spy(*args: Any, **kwargs: Any) -> str:
        seen["prompt"] = threading.current_thread()
        return real_prompt(*args, **kwargs)

    def redact_spy(text: str) -> str:
        calls.append((text, threading.current_thread()))
        return real_redact(text)

    def env_read_spy(directory: Path) -> tuple[bool, dict[str, str], str, str | None]:
        seen["env_read"] = threading.current_thread()
        return real_env_read(directory)

    def parse_spy(text: str) -> dict[str, str]:
        parses.append((text, threading.current_thread()))
        return real_parse(text)

    async def no_summary_hook(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(tool_builder, "_revise_user_prompt", prompt_spy)
    monkeypatch.setattr(tools, "redact_known_secrets", redact_spy)
    monkeypatch.setattr(tool_builder, "_read_env_for_values", env_read_spy)
    monkeypatch.setattr(tools, "_parse_dotenv_text", parse_spy)
    monkeypatch.setattr(tool_meta, "generate_and_store_summary", no_summary_hook)
    _fake_generate(
        monkeypatch,
        result=_revise_result(model_name, summary="改好了"),
        files={"run.py": _REVISED_RUN_PY},
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is (model_name == "kbsearch")
    assert seen["prompt"] is not loop_thread
    # The session's own ``.env`` read is the FIRST parse of the run (nothing before
    # it touches dotenv), and it happens on the read's OWN worker -- one hop, no
    # dotenv work left on the loop (R4-3).
    assert seen["env_read"] is not loop_thread
    assert parses and parses[0][0] == _TRICKY_ENV
    assert parses[0][1] is seen["env_read"]
    # The system prompt's name redaction -- the only call whose text is the package
    # name (the rename branch below passes the MODEL's name, which differs).
    name_threads = [thread for text, thread in calls if text == "kbsearch"]
    assert name_threads and loop_thread not in name_threads
    summary_threads = [thread for text, thread in calls if text == "改好了"]
    assert summary_threads[-1] is not loop_thread
    if model_name != "kbsearch":
        # The attempted-rename redaction, the third instance in this function.
        # (Not compared to the summary's thread: the pool is free to hand out a
        # different worker per hop -- "not the loop" is the whole property.)
        rename_threads = [thread for text, thread in calls if text == model_name]
        assert rename_threads and loop_thread not in rename_threads


def test_run_revise_exposes_the_env_to_run_shell_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The addendum's promise, verified: the withheld ``.env`` values ARE in
    run_shell's environment (so the builder can still live-test the real API),
    while the meta-tool RESULT that carries them back into the conversation is
    masked -- the same F1 treatment the install-form secret gets."""
    pkg = _seed_package(monkeypatch, tmp_path)
    (pkg / ".env").write_text("KB_API_KEY=live-secret-value-123456\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    async def fake(
        system_prompt: str,
        user_prompt: str,
        model_cls: type[BaseModel],
        *,
        workflow: str = "unknown",
        tools: Any = None,
        max_tool_rounds: int | None = None,
        timeout_seconds: float | None = None,
    ) -> BaseModel:
        by_name = {t.spec["function"]["name"]: t for t in tools or []}
        captured["shell"] = await by_name["run_shell"].handler(
            {"command": 'echo "probe=$KB_API_KEY"; echo "ours=${OPENAI_API_KEY:-none}"'}
        )
        return model_cls.model_validate(_revise_result())

    monkeypatch.setattr("afterthread.services.tool_builder.generate_structured", fake)
    _fake_summary_generate(monkeypatch)

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert "probe=" in captured["shell"]
    assert "live-secret-value-123456" not in captured["shell"]
    assert tools._REDACTION_MARKER in captured["shell"]
    assert "ours=none" in captured["shell"]  # the base env is still from scratch


def test_run_revise_regenerates_the_summary_inheriting_the_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sidecar was excluded from staging, so the revised package has none
    until the post-swap hook writes one: a fresh DRAFT summary carrying the
    ORIGIN of the original install, which lives nowhere else and is read before
    the old package (and its sidecar) is destroyed."""
    pkg = _seed_package(monkeypatch, tmp_path)
    _write_meta(
        pkg,
        summary="舊的說明",
        status="draft",
        origin={"openapi_url": "https://kb.example", "instructions": "原始安裝指示"},
    )
    _fake_generate(monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY})
    _fake_summary_generate(monkeypatch, summary="修訂後的說明")

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    meta = _meta(pkg)
    assert meta["summary"] == "修訂後的說明"
    assert meta["status"] == "draft"
    assert meta["origin"] == {
        "openapi_url": "https://kb.example",
        "instructions": "原始安裝指示",
    }
    assert meta["llm_log_id"] == llm_log.last_record_id_for_workflow("tool_summary")


def test_run_revise_refuses_rather_than_losing_the_origin_to_one_bad_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A SINGLE transient sidecar read failure must not cost the origin (R4-2).

    The origin -- the OpenAPI url and the operator's original install instructions
    -- exists ONLY in this sidecar, and the swap destroys it. It used to be read
    through the TOTAL ``read_tool_meta``, where every failure is None, while the
    pre-swap gate re-read the same file through the STRICT one. So one EIO on the
    first read said "no origin", the second read then said "draft", the swap went
    ahead, and the regenerated sidecar carried the loss forever -- unrecoverably,
    because nothing else persists either field.

    Pinned by making exactly ONE read fail, armed once the session is under way.
    That failure lands on whichever read comes first afterwards: under the old
    code that was the separate origin read (silent loss, revise reports success),
    and under the fix there is no such read -- the origin comes from the gate's own
    parse -- so the failure lands on the GATE, which fails closed. The refusal
    keeping the package AND the origin is the property; that only one read is left
    to fail is how it is achieved."""
    pkg = _seed_package(monkeypatch, tmp_path)
    root = pkg.parent
    origin = {"openapi_url": "https://kb.example", "instructions": "原始安裝指示"}
    _write_meta(pkg, summary="舊的說明", status="draft", origin=origin)
    before = _file_bytes(pkg)
    real_read = tools.read_tool_meta
    state = {"armed": False, "used": False}

    def one_transient_failure(directory: Path) -> dict[str, Any] | None:
        if state["armed"] and not state["used"]:
            state["used"] = True
            return None  # one EIO/ESTALE-shaped read, exactly once
        return real_read(directory)

    monkeypatch.setattr(tools, "read_tool_meta", one_transient_failure)
    _fake_generate(
        monkeypatch,
        result=_revise_result(),
        files={"run.py": _REVISED_RUN_PY},
        side_effect=lambda: state.update(armed=True),
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert state["used"] is True  # the transient failure really happened
    assert outcome.ok is False
    assert outcome.error == tool_builder._ERROR_REVISE_SUMMARY_UNREADABLE
    assert _file_bytes(pkg) == before  # the package never swapped ...
    assert _meta(pkg)["origin"] == origin  # ... and its only copy of the origin stands
    assert _leftovers(root) == []


def test_run_revise_survives_a_failing_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The revision is LIVE before the summary runs, so a summary failure can
    never flip the outcome -- the same best-effort contract the install hook has."""
    pkg = _seed_package(monkeypatch, tmp_path)
    _fake_generate(monkeypatch, result=_revise_result(), files={"run.py": _REVISED_RUN_PY})
    _fake_summary_generate(monkeypatch, explode=LLMUpstreamError("APIConnectionError: nope"))

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is True
    assert (pkg / "run.py").read_text(encoding="utf-8") == _REVISED_RUN_PY


def test_run_revise_ready_false_keeps_the_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A builder that gives up reports the blocker; nothing is promoted."""
    pkg = _seed_package(monkeypatch, tmp_path)
    before = _file_bytes(pkg)
    _fake_generate(
        monkeypatch,
        result={"tool_name": "kbsearch", "summary": "API 沒有分頁參數", "ready": False},
        files={"run.py": _REVISED_RUN_PY},
    )

    outcome = asyncio.run(tool_builder.run_revise("kbsearch", "加上分頁"))

    assert outcome.ok is False
    assert "AI 判定修訂尚未完成" in (outcome.error or "")
    assert "API 沒有分頁參數" in (outcome.error or "")
    assert _file_bytes(pkg) == before


def test_revise_job_records_its_outcome_and_backstops_bugs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revise job walks the same state machine and copies out the same fields
    as an install (one ``_run_job`` body drives both), and a bug escaping
    ``run_revise`` lands as a category-only failure naming the REVISE action."""

    async def scenario() -> None:
        async def fake_run_revise(name: str, feedback: str) -> InstallOutcome:
            return InstallOutcome(ok=True, tool_name=name, summary="改好了", llm_log_id=5)

        monkeypatch.setattr("afterthread.services.tool_builder.run_revise", fake_run_revise)
        job_id = tool_builder.start_revise_job("kbsearch", "加上分頁")
        assert job_id is not None
        for _ in range(200):
            job = tool_builder.get_job(job_id)
            assert job is not None
            if job["state"] not in ("queued", "running"):
                break
            await asyncio.sleep(0.01)
        assert job["state"] == "succeeded"
        assert job["tool_name"] == "kbsearch"
        assert job["summary"] == "改好了"
        assert job["llm_log_id"] == 5
        assert job["finished_at"] is not None

        async def exploding(name: str, feedback: str) -> InstallOutcome:
            raise RuntimeError("bug with secrets in str()")

        monkeypatch.setattr("afterthread.services.tool_builder.run_revise", exploding)
        bug_id = tool_builder.start_revise_job("kbsearch", "再改")
        assert bug_id is not None
        for _ in range(200):
            job = tool_builder.get_job(bug_id)
            assert job is not None
            if job["state"] not in ("queued", "running"):
                break
            await asyncio.sleep(0.01)
        assert job["state"] == "failed"
        assert "修訂過程發生未預期錯誤" in (job["error"] or "")
        assert "RuntimeError" in (job["error"] or "")
        assert "secrets" not in (job["error"] or "")
        await asyncio.gather(*list(tool_builder._TASKS), return_exceptions=True)

    asyncio.run(scenario())


def test_install_and_revise_share_one_single_flight_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ONE tool job at a time, across BOTH kinds (D40): an active install refuses
    a revise and an active revise refuses an install, because either one can be
    moving a package directory into place."""

    async def scenario() -> None:
        release = asyncio.Event()

        async def blocking_install(
            url: str,
            instructions: str,
            *,
            secret_name: str | None = None,
            secret_value: str | None = None,
        ) -> InstallOutcome:
            await release.wait()
            return InstallOutcome(ok=True, tool_name="kb")

        async def blocking_revise(name: str, feedback: str) -> InstallOutcome:
            await release.wait()
            return InstallOutcome(ok=True, tool_name=name)

        monkeypatch.setattr("afterthread.services.tool_builder.run_install", blocking_install)
        monkeypatch.setattr("afterthread.services.tool_builder.run_revise", blocking_revise)

        first = tool_builder.start_install_job("http://x/openapi.json", "i")
        assert first is not None
        await asyncio.sleep(0)
        assert tool_builder.start_revise_job("kb", "改一下") is None  # install blocks revise

        release.set()
        for _ in range(200):
            job = tool_builder.get_job(first)
            assert job is not None
            if job["state"] not in ("queued", "running"):
                break
            await asyncio.sleep(0.01)

        release.clear()
        second = tool_builder.start_revise_job("kb", "改一下")
        assert second is not None
        await asyncio.sleep(0)
        assert tool_builder.start_install_job("http://x/openapi.json", "i") is None  # and back

        release.set()
        await asyncio.gather(*list(tool_builder._TASKS), return_exceptions=True)

    asyncio.run(scenario())


# --- llm_log lookup used by the outcome linkage --------------------------------


def test_last_record_id_for_workflow_returns_newest() -> None:
    for workflow in ("capture", "tool_install", "tool_install", "enrich"):
        recorder = llm_log.LlmInteractionRecorder(workflow=workflow, model="m")
        recorder.begin_attempt([{"role": "user", "content": "x"}])
        recorder.finish(outcome="ok", error=None)

    newest_install = llm_log.last_record_id_for_workflow("tool_install")
    assert newest_install is not None
    summaries = llm_log.list_summaries(10)
    install_ids = [s["id"] for s in summaries if s["workflow"] == "tool_install"]
    assert newest_install == max(install_ids)
    assert llm_log.last_record_id_for_workflow("nope") is None


def test_resolve_in_staging_direct_cases(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    assert _resolve_in_staging(staging, "a/b.txt") == (staging / "a" / "b.txt").resolve()
    assert _resolve_in_staging(staging, "../x") is None
    assert _resolve_in_staging(staging, "/abs") is None
    assert _resolve_in_staging(staging, 123) is None
    assert _resolve_in_staging(staging, "") is None
