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
import json
import os
import socket
import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from afterthread.config import Settings
from afterthread.services import llm_log, tool_builder, tools
from afterthread.services.tool_builder import (
    _ERROR_NAME_TAKEN,
    _ERROR_OPENAPI_TOO_LARGE,
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
    touches)."""
    tool_builder._reset_jobs_for_tests()
    llm_log._reset_for_tests()
    tools._INFLIGHT_SECRETS.clear()
    tools._ENV_VALUE_CACHE.clear()
    yield
    tool_builder._reset_jobs_for_tests()
    llm_log._reset_for_tests()
    tools._INFLIGHT_SECRETS.clear()
    tools._ENV_VALUE_CACHE.clear()


def _install_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Settings:
    """Point BOTH settings readers (tools registry + installer) at one value."""
    settings = Settings(**overrides)
    monkeypatch.setattr("afterthread.services.tools.get_settings", lambda: settings)
    monkeypatch.setattr("afterthread.services.tool_builder.get_settings", lambda: settings)
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


def _fake_generate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, Any],
    files: dict[str, str] | None = None,
    record_session: bool = True,
) -> dict[str, Any]:
    """Install a fake generate_structured that builds ``files`` via the REAL
    meta-tools it receives (so containment/threadpool paths run), optionally
    records a genuine tool_install llm_log record (so the outcome's
    llm_log_id wiring is exercised), and returns ``result`` validated through
    the real model. Captures the call's kwargs for assertions."""
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
    that would never trip httpx's PER-PHASE inactivity timeout -- fails with the
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

    monkeypatch.setattr(tool_builder.httpx, "AsyncClient", _HangingClient)
    monkeypatch.setattr(tool_builder, "_FETCH_TOTAL_TIMEOUT_SECONDS", 0.2)

    started = time.monotonic()
    text, error = asyncio.run(tool_builder._fetch_openapi("http://kb.example/openapi.json"))
    elapsed = time.monotonic() - started

    assert text is None
    assert error is not None and error.startswith("OpenAPI 文件下載失敗")
    assert elapsed < 2.0  # bounded by the 0.2s total deadline, not the 3600s hang


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
            }
        ]
    }


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

    response = client.get("/api/tools/install/job-1")
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

    missing = client.get("/api/tools/install/ghost")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Install job not found"}


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
