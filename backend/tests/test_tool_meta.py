"""Tests for the tool-summary generator (afterthread.services.tool_meta).

Two layers:

* the PROMPT -- built against a real tmp package so the file inventory runs for
  real: what rides in it (the manifest, the implementation files, the ``.env``
  KEY names, the install origin) and, more importantly, what must NOT (``.env``
  VALUES, the sidecar's own previous output, any known secret);
* the STORE -- ``generate_structured`` is monkeypatched (at the name imported
  INTO tool_meta, which is a different module reference from tool_builder's own)
  so the sidecar-writing contract runs for real: a success writes a draft
  sidecar linked to its session's log record, a failure never clobbers a good
  summary, the install hook never raises whatever goes wrong, and the
  synchronous regenerate surfaces LLM failures instead of swallowing them.

Async entry points are driven with ``asyncio.run`` (no pytest-asyncio plugin,
matching the suite). The llm_log ring and the known-secret registries are
process-wide singletons, so an autouse fixture resets them around every test.
"""

import asyncio
import json
import sys
import threading
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from afterthread.config import Settings
from afterthread.services import llm_log, tool_meta, tools
from afterthread.services.llm import LLMNotConfiguredError, LLMUpstreamError
from afterthread.services.tool_meta import (
    ToolSummaryResult,
    generate_and_store_summary,
    regenerate_summary,
)


@pytest.fixture(autouse=True)
def _reset_singletons() -> Generator[None]:
    """Empty the llm_log ring and the process-wide known-secret registries
    around every test (all module-level singletons this module touches)."""
    llm_log._reset_for_tests()
    tools._INFLIGHT_SECRETS.clear()
    tools._ENV_VALUE_CACHE.clear()
    yield
    llm_log._reset_for_tests()
    tools._INFLIGHT_SECRETS.clear()
    tools._ENV_VALUE_CACHE.clear()


def _summary_settings(monkeypatch: pytest.MonkeyPatch, root: Path, **overrides: Any) -> Settings:
    """Point BOTH settings readers (the registry and the summary generator) at
    one value, so their view of the tools dir / prompt budget never disagrees."""
    settings = Settings(tools_dir=str(root), **overrides)
    for target in (
        "afterthread.services.tools.get_settings",
        "afterthread.services.tool_meta.get_settings",
    ):
        monkeypatch.setattr(target, lambda settings=settings: settings)
    return settings


def _write_meta(pkg: Path, **fields: Any) -> bool:
    """``write_tool_meta`` with the ``updated_at`` every real caller supplies.

    The writer refuses a meta without a string ``updated_at`` (it will not invent
    a timestamp on a caller's behalf), so tests seed a sidecar through here and
    state only the fields under test."""
    return tools.write_tool_meta(pkg, {"updated_at": "2026-01-01T00:00:00+00:00", **fields})


def _package(
    root: Path,
    name: str = "kbsearch",
    *,
    run_py: str = "import sys\nsys.stdout.write('x')\n",
    dotenv: str | None = None,
) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "run.py").write_text(run_py, encoding="utf-8")
    (pkg / "tool.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": "searches the KB",
                "parameters": {"type": "object", "properties": {}},
                "entry": [sys.executable, "run.py"],
            }
        ),
        encoding="utf-8",
    )
    if dotenv is not None:
        (pkg / ".env").write_text(dotenv, encoding="utf-8")
    return pkg


def _fake_generate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    summary: str = "這個工具會查 KB",
    explode: BaseException | None = None,
    record_session: bool = True,
    side_effect: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Stub the SUMMARY session's generate_structured and capture its kwargs.

    Patches the name imported INTO tool_meta (not llm's own), which is the
    reference the module actually calls. ``record_session`` writes a genuine
    llm_log record for the workflow it was called with, so the sidecar's
    llm_log_id wiring is exercised against the real ring rather than a stub id.

    ``side_effect`` runs INSIDE the stubbed call, which is the only place a test
    can act "while the generation is in flight": the real thing awaits an LLM for
    seconds, and every store-time race (a concurrent 定版, a racing delete) lives
    in exactly that window.
    """
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
        if record_session:
            recorder = llm_log.LlmInteractionRecorder(workflow=workflow, model="m")
            recorder.begin_attempt([{"role": "user", "content": "summarize"}])
            recorder.record_response(summary)
            recorder.finish(outcome="ok" if explode is None else "error", error=None)
        if side_effect is not None:
            side_effect()
        if explode is not None:
            raise explode
        return model_cls.model_validate({"summary": summary})

    monkeypatch.setattr("afterthread.services.tool_meta.generate_structured", fake)
    return captured


# --- the result model ---------------------------------------------------------


def test_summary_result_sanitizes_and_caps() -> None:
    result = ToolSummaryResult.model_validate({"summary": "  spaced  "})
    assert result.summary == "spaced"
    long = ToolSummaryResult.model_validate({"summary": "y" * (tool_meta._TOOL_SUMMARY_CAP + 500)})
    assert len(long.summary) == tool_meta._TOOL_SUMMARY_CAP


def test_summary_result_redacts_before_capping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redact-then-cap: a secret straddling the slice edge must be masked while
    the text is still whole, or the cut leaves an unmatchable fragment."""
    secret = "live-secret-abcdef"
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))
    padding = "y" * (tool_meta._TOOL_SUMMARY_CAP - 4)
    result = ToolSummaryResult.model_validate({"summary": padding + secret})
    assert secret not in result.summary
    assert secret[:8] not in result.summary


def test_summary_result_redacts_before_stripping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redact-then-strip: the redactor matches the REGISTERED value, so a secret
    carrying edge whitespace (a hand-edited .env with a quoted " secret-token ")
    stops matching the moment strip eats that edge -- and the body would then
    ride through unmasked."""
    secret = " secret-token-abcdef "
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))
    result = ToolSummaryResult.model_validate({"summary": secret})
    assert "secret-token-abcdef" not in result.summary
    assert tools._REDACTION_MARKER in result.summary


@pytest.mark.parametrize("value", ["", "   ", None], ids=["empty", "blank", "missing"])
def test_summary_result_rejects_empty_summary(value: str | None) -> None:
    """An empty summary is indistinguishable from the placeholder a FAILED
    generation writes, so it is rejected -- which is what triggers
    generate_structured's one corrective retry."""
    with pytest.raises(ValidationError):
        ToolSummaryResult.model_validate({"summary": value})


def test_summary_result_rejects_non_object() -> None:
    with pytest.raises(ValidationError):
        ToolSummaryResult.model_validate(["not", "an", "object"])


# --- the prompt ---------------------------------------------------------------


def test_user_prompt_carries_the_package_but_never_env_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The prompt is built from the files on disk: the manifest and every
    implementation file ride in it, the ``.env``'s KEY names ride in it, and its
    VALUES never do."""
    root = tmp_path / "tools"
    secret = "kb-live-secret-abcdef"
    pkg = _package(
        root,
        run_py="import os\nprint(os.environ['KB_API_KEY'])\n",
        dotenv=f"KB_API_KEY={secret}\nKB_BASE=https://kb.example\n",
    )
    _summary_settings(monkeypatch, root)

    prompt = tool_meta._summary_user_prompt(
        "kbsearch",
        pkg,
        origin={"openapi_url": "http://kb.example/openapi.json", "instructions": "查 KB"},
        builder_summary="built and tested",
    )

    assert "searches the KB" in prompt  # tool.json content
    assert "os.environ['KB_API_KEY']" in prompt  # run.py content
    assert "KB_API_KEY" in prompt and "KB_BASE" in prompt  # .env KEY names
    assert secret not in prompt  # ... never its values
    assert "http://kb.example/openapi.json" in prompt  # install origin
    assert "查 KB" in prompt
    assert "built and tested" in prompt


def test_user_prompt_skips_the_sidecar_and_dot_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A summary must never be fed its own previous output as if it were source
    (nor any other backend-internal dot-file)."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    _write_meta(pkg, summary="PREVIOUS-SUMMARY-TEXT", status="draft")
    (pkg / ".hidden-note").write_text("HIDDEN-FILE-TEXT", encoding="utf-8")

    prompt = tool_meta._summary_user_prompt("kbsearch", pkg, origin=None, builder_summary=None)

    assert "PREVIOUS-SUMMARY-TEXT" not in prompt
    assert "HIDDEN-FILE-TEXT" not in prompt
    assert tools._AI_META_FILENAME not in prompt


def test_user_prompt_redacts_known_secrets_in_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A value another tool registered must be masked out of THIS package's
    files before they reach the model."""
    root = tmp_path / "tools"
    secret = "another-tools-live-secret-abcdef"
    _package(root, "other", dotenv=f"OTHER_KEY={secret}\n")
    pkg = _package(root, "kbsearch", run_py=f"TOKEN = '{secret}'\n")
    _summary_settings(monkeypatch, root)

    prompt = tool_meta._summary_user_prompt("kbsearch", pkg, origin=None, builder_summary=None)
    assert secret not in prompt
    assert tools._REDACTION_MARKER in prompt


def test_user_prompt_redacts_the_operator_supplied_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The install context is OPERATOR text and was riding in UNMASKED.

    An install URL is routinely ``.../openapi.json?token=<the form secret>``,
    and the installer REGISTERS that value for the duration of the build -- so
    the redactor knows it and simply was not asked. Same for the instructions
    the operator typed next to it."""
    root = tmp_path / "tools"
    secret = "install-form-secret-abcdef"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))

    prompt = tool_meta._summary_user_prompt(
        "kbsearch",
        pkg,
        origin={
            "openapi_url": f"https://kb.example/openapi.json?token={secret}",
            "instructions": f"用 {secret} 認證",
        },
        builder_summary=f"tested against {secret}",
    )

    assert secret not in prompt
    assert tools._REDACTION_MARKER in prompt
    # Masked, not dropped: the rest of the URL is still context for the model.
    assert "https://kb.example/openapi.json?token=" in prompt


def test_user_prompt_redacts_a_filename_carrying_a_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file's CONTENT was masked but the path header naming it was not -- and a
    builder can create a file whose NAME embeds an expanded $SECRET (the vector
    tool_builder's list_dir already masks)."""
    root = tmp_path / "tools"
    secret = "path-secret-abcdef"
    pkg = _package(root)
    (pkg / f"{secret}.py").write_text("print('x')\n", encoding="utf-8")
    _summary_settings(monkeypatch, root)
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))

    prompt = tool_meta._summary_user_prompt("kbsearch", pkg, origin=None, builder_summary=None)
    assert secret not in prompt
    assert tools._REDACTION_MARKER in prompt


def test_user_prompt_final_pass_masks_a_field_no_call_site_redacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The class-closer: the ASSEMBLED prompt is redacted once more, so a piece
    that no individual call masks is covered anyway -- which is what makes the
    next field added to this prompt safe even if its author forgets.

    The package NAME is the live example: it is interpolated straight into the
    opening line by no redaction at all, and it is a legal package name that a
    registered value could equal."""
    root = tmp_path / "tools"
    secret = "kbsearch-abcdef"  # matches _NAME_RE, so it is a legal package name
    pkg = _package(root, name=secret)
    _summary_settings(monkeypatch, root)
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({secret}))

    prompt = tool_meta._summary_user_prompt(secret, pkg, origin=None, builder_summary=None)
    assert secret not in prompt
    assert tools._REDACTION_MARKER in prompt


def test_user_prompt_bounded_by_the_prompt_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Files are capped per file AND the whole prompt is capped by the
    operator's own llm_prompt_budget_tokens allowance."""
    root = tmp_path / "tools"
    pkg = _package(root, run_py="z" * (tool_meta._FILE_CONTENT_CAP * 2))
    (pkg / "extra.py").write_text("w" * (tool_meta._FILE_CONTENT_CAP * 2), encoding="utf-8")
    _summary_settings(monkeypatch, root, llm_prompt_budget_tokens=4_000)

    prompt = tool_meta._summary_user_prompt("kbsearch", pkg, origin=None, builder_summary=None)
    assert len(prompt) <= 4_000


def test_user_prompt_budget_cut_eats_context_not_the_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ORDER is the third bound, and the only one that decides WHAT survives.

    4 000 is the LEGAL FLOOR of ``llm_prompt_budget_tokens`` (config's ``ge``),
    and the install form admits 20 000 chars of instructions -- so this is an
    ordinary configuration, not a pathological one. With the context emitted
    FIRST (what this did before), the final whole-prompt truncation cut inside
    the instructions and the model received ZERO package content, then invented
    documentation from the instructions alone -- which we then persisted as the
    tool's explanation, under a system prompt whose central rule is "describe
    ONLY what the files actually show".

    Subject first, context last: the cut now eats background and the package
    truth survives. Both halves are asserted -- the tail that got cut AND the
    head that did not -- so this fails if the order regresses in either
    direction, rather than merely if the prompt got shorter."""
    root = tmp_path / "tools"
    pkg = _package(root, run_py="# RUNPY-BODY-MARKER\n" + "z" * 600)
    _summary_settings(monkeypatch, root, llm_prompt_budget_tokens=4_000)
    # Exactly _CONTEXT_CAP, so the inner per-field cap is NOT what removes the
    # tail -- the whole-prompt budget cut is.
    instructions = "B" * (tool_meta._CONTEXT_CAP - 24) + "INSTRUCTIONS-TAIL-MARKER"
    assert len(instructions) == tool_meta._CONTEXT_CAP

    prompt = tool_meta._summary_user_prompt(
        "kbsearch",
        pkg,
        origin={"openapi_url": "http://kb.example/o.json", "instructions": instructions},
        builder_summary=None,
    )

    assert len(prompt) <= 4_000  # the budget really did bite
    # The SUBJECT survives: the manifest and the first implementation file.
    assert "searches the KB" in prompt  # tool.json content
    assert "RUNPY-BODY-MARKER" in prompt  # run.py content
    # The CONTEXT is what got cut -- present, but truncated from the end.
    assert "The user's original install instructions:" in prompt
    assert "INSTRUCTIONS-TAIL-MARKER" not in prompt


def test_user_prompt_bounds_the_file_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "tools"
    pkg = _package(root)
    for index in range(tool_meta._MAX_FILES + 10):
        (pkg / f"file{index:03d}.py").write_text(f"# {index}\n", encoding="utf-8")
    _summary_settings(monkeypatch, root)

    assert len(tool_meta._package_files(pkg)) == tool_meta._MAX_FILES


# --- generation + storage -----------------------------------------------------


def test_generate_and_store_summary_writes_a_draft_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The happy path: a draft sidecar carrying the summary, the origin, and a
    link to the summary session's own AI 日誌 record."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    captured = _fake_generate(monkeypatch, summary="這個工具會查 KB")

    asyncio.run(
        generate_and_store_summary(
            "kbsearch",
            origin={"openapi_url": "http://kb.example/openapi.json", "instructions": "查 KB"},
            builder_summary="built it",
        )
    )

    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["summary"] == "這個工具會查 KB"
    assert meta["status"] == "draft"
    assert meta["origin"] == {
        "openapi_url": "http://kb.example/openapi.json",
        "instructions": "查 KB",
    }
    assert meta["updated_at"]
    # Linked to the genuine record of THIS session, found by its own workflow name.
    assert meta["llm_log_id"] == llm_log.last_record_id_for_workflow("tool_summary")
    assert meta["llm_log_id"] is not None
    # The summary session runs under its OWN workflow, with no tools and the
    # default timeout (it only reads what the prompt already carries).
    assert captured["workflow"] == "tool_summary"
    assert captured["tools"] is None
    assert captured["timeout_seconds"] is None
    assert captured["max_tool_rounds"] is None


def test_summary_workflow_never_collides_with_the_install_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The two sessions of one install job are told apart by NAME: an install
    record written first must stay the newest `tool_install` record after the
    summary session finishes, or the install outcome's llm_log_id would point at
    the summary instead of the build."""
    root = tmp_path / "tools"
    _package(root)
    _summary_settings(monkeypatch, root)
    recorder = llm_log.LlmInteractionRecorder(workflow="tool_install", model="m")
    recorder.begin_attempt([{"role": "user", "content": "build"}])
    recorder.finish(outcome="ok", error=None)
    install_record = llm_log.last_record_id_for_workflow("tool_install")

    _fake_generate(monkeypatch)
    asyncio.run(generate_and_store_summary("kbsearch"))

    assert llm_log.last_record_id_for_workflow("tool_install") == install_record
    assert llm_log.last_record_id_for_workflow("tool_summary") != install_record


def test_generate_and_store_summary_preserves_status_and_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A regeneration keeps the status it had and inherits the origin the
    INSTALL captured (the only copy of the OpenAPI url / instructions)."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    origin = {"openapi_url": "http://kb.example/openapi.json", "instructions": "查 KB"}
    _write_meta(pkg, summary="舊的", status="draft", origin=origin)

    _fake_generate(monkeypatch, summary="新的")
    asyncio.run(generate_and_store_summary("kbsearch", origin=None))

    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["summary"] == "新的"
    assert meta["status"] == "draft"  # preserved, never reset by the write
    assert meta["origin"] == origin  # inherited, never erased


def test_store_meta_refuses_a_finalized_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A finalized summary is never rewritten -- not even by the install hook.

    _store_meta used to PRESERVE the "final" status while still overwriting the
    summary TEXT, which satisfies the letter of 定版 and breaks its meaning: the
    operator froze one explanation and would get a different one back. The store
    re-reads the status and refuses outright."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    origin = {"openapi_url": "http://kb.example/openapi.json", "instructions": "查 KB"}
    _write_meta(pkg, summary="定版的說明", status="final", origin=origin)
    before = (pkg / tools._AI_META_FILENAME).read_bytes()

    assert (
        tool_meta._store_meta(pkg, summary="新的", origin=None, llm_log_id=9)
        is tool_meta.StoreRefusal.FINALIZED
    )
    assert (pkg / tools._AI_META_FILENAME).read_bytes() == before  # byte-for-byte

    # And the install hook swallows that refusal like every other store outcome:
    # it must never fail an install that already succeeded.
    _fake_generate(monkeypatch, summary="新的")
    asyncio.run(generate_and_store_summary("kbsearch", origin=None))  # must not raise
    assert (pkg / tools._AI_META_FILENAME).read_bytes() == before


def test_generate_and_store_summary_writes_placeholder_when_no_sidecar_yet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed FIRST generation still leaves a sidecar (empty summary + origin
    + the failed session's log id), so the page can offer 重新產生."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    _fake_generate(monkeypatch, explode=LLMNotConfiguredError("nope"))

    asyncio.run(generate_and_store_summary("kbsearch", origin={"instructions": "查 KB"}))

    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["summary"] == ""
    assert meta["status"] == "draft"
    # Narrowed on the way to disk: both known fields, the absent one null.
    assert meta["origin"] == {"openapi_url": None, "instructions": "查 KB"}


def test_generate_and_store_summary_never_clobbers_a_good_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transient LLM failure must leave the previous summary untouched -- the
    one thing the best-effort path must never do is blank a good summary."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    _write_meta(pkg, summary="先前的好總結", status="draft")

    _fake_generate(monkeypatch, explode=LLMUpstreamError("APIConnectionError: unreachable"))
    asyncio.run(generate_and_store_summary("kbsearch"))

    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["summary"] == "先前的好總結"


@pytest.mark.parametrize(
    "explode",
    [RuntimeError("bug"), LLMNotConfiguredError("off"), LLMUpstreamError("Timeout: slow")],
    ids=["bug", "not-configured", "upstream"],
)
def test_generate_and_store_summary_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explode: BaseException
) -> None:
    """It runs AFTER the package is already installed, so nothing it hits may
    escape and flip a successful install to failed."""
    root = tmp_path / "tools"
    _package(root)
    _summary_settings(monkeypatch, root)
    _fake_generate(monkeypatch, explode=explode)

    asyncio.run(generate_and_store_summary("kbsearch"))  # must not raise


def test_generate_and_store_summary_never_raises_when_prompt_building_explodes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The backstop covers the non-LLM half too: a failing secret provider makes
    the fail-closed redactor raise while the prompt is being built."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)

    def boom() -> frozenset[str]:
        raise RuntimeError("provider down")

    monkeypatch.setattr(tools, "known_secret_values", boom)
    _fake_generate(monkeypatch)

    asyncio.run(generate_and_store_summary("kbsearch"))  # must not raise
    # Nothing could be written either (the sidecar write is fail-closed too).
    assert not (pkg / tools._AI_META_FILENAME).exists()


def test_generate_and_store_summary_ignores_a_refused_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The install hook sees the same "did the write land?" signal the
    synchronous route now acts on, and DELIBERATELY ignores it: it runs after
    the package is already promoted, so a sidecar it could not write must not
    escape and flip a successful install to failed."""
    root = tmp_path / "tools"
    _package(root)
    _summary_settings(monkeypatch, root)
    _fake_generate(monkeypatch, summary="這個工具會查 KB")
    monkeypatch.setattr(tools, "write_tool_meta", lambda *args, **kwargs: False)

    asyncio.run(generate_and_store_summary("kbsearch"))  # must not raise


def test_generate_and_store_summary_silent_when_package_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A racing delete between promote and summary: nothing to summarize, and
    nothing is written (no ghost package directory is resurrected)."""
    root = tmp_path / "tools"
    root.mkdir()
    _summary_settings(monkeypatch, root)

    async def must_not_generate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no session may start for a package that is gone")

    monkeypatch.setattr("afterthread.services.tool_meta.generate_structured", must_not_generate)
    asyncio.run(generate_and_store_summary("ghost"))
    assert list(root.iterdir()) == []


def test_generate_and_store_summary_redacts_into_the_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A model that echoed a live secret into its summary never reaches disk
    with it -- the sidecar would otherwise brick every later revise of this
    package on the embedded-secret gate."""
    root = tmp_path / "tools"
    secret = "kb-live-secret-abcdef"
    pkg = _package(root, dotenv=f"KB_API_KEY={secret}\n")
    _summary_settings(monkeypatch, root)
    _fake_generate(monkeypatch, summary=f"it authenticates with {secret}")

    asyncio.run(generate_and_store_summary("kbsearch"))

    stored = (pkg / tools._AI_META_FILENAME).read_text(encoding="utf-8")
    assert secret not in stored
    assert tools._REDACTION_MARKER in stored


# --- the synchronous regenerate -----------------------------------------------


def test_regenerate_summary_returns_the_fresh_meta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    _write_meta(pkg, summary="舊的", status="draft", origin={"instructions": "查 KB"})
    _fake_generate(monkeypatch, summary="新的說明")

    meta = asyncio.run(regenerate_summary("kbsearch"))

    assert isinstance(meta, dict)
    assert meta["summary"] == "新的說明"
    assert meta["status"] == "draft"
    # Carried back from the install, in the narrowed shape the writer stores
    # (both known fields, the absent one explicitly null).
    assert meta["origin"] == {"openapi_url": None, "instructions": "查 KB"}
    assert tools.read_tool_meta(pkg) == meta  # what it returned IS what it stored


def test_regenerate_summary_feeds_the_stored_origin_back_into_the_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The install's URL and instructions are the first-hand account of what this
    package was meant to be, and the sidecar is their only copy -- a regeneration
    that dropped them would explain the files with strictly LESS context than the
    install had. The .env values still never ride along."""
    root = tmp_path / "tools"
    secret = "kb-live-secret-abcdef"
    pkg = _package(root, dotenv=f"KB_API_KEY={secret}\n")
    _summary_settings(monkeypatch, root)
    _write_meta(
        pkg,
        summary="舊的",
        status="draft",
        origin={
            "openapi_url": "http://kb.example/ORIGIN-URL-MARKER.json",
            "instructions": "ORIGIN-INSTRUCTIONS-MARKER 只查內部 KB",
        },
    )
    captured = _fake_generate(monkeypatch, summary="新的說明")

    meta = asyncio.run(regenerate_summary("kbsearch"))

    assert "ORIGIN-INSTRUCTIONS-MARKER 只查內部 KB" in captured["user_prompt"]
    assert "ORIGIN-URL-MARKER" in captured["user_prompt"]
    assert secret not in captured["user_prompt"]
    assert isinstance(meta, dict)
    assert meta["origin"]["instructions"] == "ORIGIN-INSTRUCTIONS-MARKER 只查內部 KB"


def test_regenerate_summary_ignores_an_unusable_stored_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sidecar is hand-editable, so an origin we cannot make sense of never
    reaches the prompt as if the backend had written it -- and the regeneration
    never ERASES what it could not read either: _store_meta's inheritance carries
    the stored origin forward untouched.

    Written by HAND here, because the writer itself narrows an origin now: an
    unusable one only survives on disk if the operator put it there."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    origin = {"openapi_url": 12, "junk": "JUNK-MARKER"}
    (pkg / tools._AI_META_FILENAME).write_text(
        json.dumps({"summary": "舊的", "status": "draft", "origin": origin}), encoding="utf-8"
    )
    captured = _fake_generate(monkeypatch, summary="新的說明")

    meta = asyncio.run(regenerate_summary("kbsearch"))

    assert "JUNK-MARKER" not in captured["user_prompt"]
    assert isinstance(meta, dict)
    # Inherited (never overwritten by {}), then narrowed on the way to disk: the
    # junk key is dropped and the unreadable field lands as an explicit null.
    assert meta["origin"] == {"openapi_url": None, "instructions": None}
    assert tools.read_tool_meta(pkg) == meta


def test_regenerate_summary_refuses_an_internal_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """tools/<alias> -> tools/<real> resolves inside the root, so without the
    shared hard-block a regenerate addressed at the alias would spend an LLM
    session rewriting the REAL package's sidecar."""
    root = tmp_path / "tools"
    pkg = _package(root, "real")
    (root / "alias").symlink_to(root / "real", target_is_directory=True)
    _summary_settings(monkeypatch, root)
    _write_meta(pkg, summary="真的說明", status="draft")

    async def must_not_generate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no session may start for an aliased package")

    monkeypatch.setattr("afterthread.services.tool_meta.generate_structured", must_not_generate)

    assert asyncio.run(regenerate_summary("alias")) is None
    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["summary"] == "真的說明"  # the real sidecar is untouched


@pytest.mark.parametrize(
    "explode, expected",
    [
        (LLMNotConfiguredError("off"), LLMNotConfiguredError),
        (LLMUpstreamError("Timeout: slow"), LLMUpstreamError),
    ],
    ids=["not-configured", "upstream"],
)
def test_regenerate_summary_propagates_llm_failures_without_clobbering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    explode: BaseException,
    expected: type[BaseException],
) -> None:
    """Someone is waiting on this request, so the failure must reach them as a
    503/502 -- while the previous summary survives untouched."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    _write_meta(pkg, summary="先前的好總結", status="draft")
    _fake_generate(monkeypatch, explode=explode)

    with pytest.raises(expected):
        asyncio.run(regenerate_summary("kbsearch"))

    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["summary"] == "先前的好總結"


def test_regenerate_summary_signals_nothing_stored_when_package_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The route already 404s a missing tool; this is the race backstop, and it
    must not write a sidecar into a deleted package's path.

    None, not ``{}``: the route turns "nothing was stored" into its 404, so the
    signal has to be distinguishable from a meta dict that merely happens to be
    empty."""
    root = tmp_path / "tools"
    root.mkdir()
    _summary_settings(monkeypatch, root)
    _fake_generate(monkeypatch)

    assert asyncio.run(regenerate_summary("ghost")) is None
    assert list(root.iterdir()) == []


def test_regenerate_summary_signals_nothing_stored_when_the_write_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A generation that produced text but could not persist it did NOT happen.

    Returning the composed meta here would have the synchronous route answer 200
    with a summary that is nowhere on disk and vanishes on the next GET."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    _write_meta(pkg, summary="先前的好總結", status="draft")
    _fake_generate(monkeypatch, summary="新的說明")
    monkeypatch.setattr(tools, "write_tool_meta", lambda *args, **kwargs: False)

    assert asyncio.run(regenerate_summary("kbsearch")) is None


def test_regenerate_summary_refuses_a_finalize_that_lands_mid_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The finalize TOCTOU: the route's 定版 gate runs BEFORE an await that lasts
    as long as an LLM round trip, so a PATCH landing inside that window used to
    have its frozen summary overwritten anyway (the status was preserved; the
    TEXT was not). The generation flips the sidecar itself here, which is exactly
    what a concurrent PATCH does.

    The refusal is its own signal, NOT the None a failed write gives: "you cannot
    do this" and "it did not work" are different answers to the user, and the
    route turns them into a 409 and a 404 respectively."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    _write_meta(pkg, summary="定版的說明", status="draft")

    frozen: dict[str, bytes] = {}

    def finalize_mid_call() -> None:
        assert tools.set_summary_status("kbsearch", "final") == "ok"
        frozen["bytes"] = (pkg / tools._AI_META_FILENAME).read_bytes()

    _fake_generate(monkeypatch, summary="新的說明", side_effect=finalize_mid_call)

    assert asyncio.run(regenerate_summary("kbsearch")) is tool_meta.StoreRefusal.FINALIZED
    # Byte-for-byte what 定版 froze -- the generation that was already in flight
    # left no trace, not even a refreshed updated_at.
    assert (pkg / tools._AI_META_FILENAME).read_bytes() == frozen["bytes"]
    stored = tools.read_tool_meta(pkg)
    assert stored is not None
    assert stored["summary"] == "定版的說明"
    assert stored["status"] == "final"


class _ContendedLock:
    """A ``threading.Lock`` that REPORTS when an acquirer finds it already held.

    Substituted for ``tools._META_LOCK`` so the interleave test below can be
    deterministic instead of sleep-timed: the ``blocked`` event fires at the
    exact moment a second thread tries to enter the critical section and cannot,
    which IS the mutual exclusion under test. It also makes the test fail loudly
    (rather than flakily pass) if the lock is ever removed -- with no lock there
    is no contention to observe, so ``blocked`` never fires and the parked
    finalize times out."""

    def __init__(self, blocked: threading.Event) -> None:
        self._inner = threading.Lock()
        self._blocked = blocked

    def __enter__(self) -> _ContendedLock:
        if not self._inner.acquire(blocking=False):
            self._blocked.set()
            self._inner.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._inner.release()


def test_regenerate_summary_cannot_undo_a_finalize_holding_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The finalize re-check is a READ-then-WRITE, and by itself it guards nothing.

    Both sequences run on THREADPOOL workers in production -- the PATCH's
    ``set_summary_status`` on one, a regenerate's store on another -- so they
    genuinely execute in parallel. Unserialized, both read ``"draft"``, the PATCH
    writes ``"final"``, and the store then writes its OWN composed meta carrying
    the stale ``"draft"`` plus the new summary: the operator's 定版 silently
    undone, and the frozen text replaced by exactly the generation the freeze
    existed to stop. The re-check only means something while nothing can land
    between the read and the write.

    Driven through the REAL lock with two real threads, sequenced by events
    rather than sleeps: the finalize parks INSIDE its own hold until the store
    has demonstrably contended for the lock, then completes. The store must
    therefore observe ``final`` and refuse."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    _write_meta(pkg, summary="定版的說明", status="draft")

    blocked = threading.Event()  # set when the store finds the lock already held
    inside = threading.Event()  # set once the finalize is inside its hold
    monkeypatch.setattr(tools, "_META_LOCK", _ContendedLock(blocked))

    frozen: dict[str, bytes] = {}
    failures: list[BaseException] = []
    real_write = tools.write_tool_meta

    def write_then_park(directory: Path, meta: dict[str, Any]) -> bool:
        # Runs INSIDE set_summary_status's lock hold. Parking here is what forces
        # the store to arrive while the finalize is mid-sequence -- the exact
        # window the r2 code lost the race in.
        if meta.get("status") == "final":
            inside.set()
            assert blocked.wait(timeout=5), "the store never contended for _META_LOCK"
        return real_write(directory, meta)

    monkeypatch.setattr(tools, "write_tool_meta", write_then_park)

    def finalize() -> None:
        try:
            assert tools.set_summary_status("kbsearch", "final") == "ok"
            frozen["bytes"] = (pkg / tools._AI_META_FILENAME).read_bytes()
        except BaseException as exc:  # reported to the main thread, never swallowed
            failures.append(exc)

    finalizer = threading.Thread(target=finalize)

    def start_finalize_mid_call() -> None:
        # "While the generation is in flight" -- the only place a concurrent
        # PATCH can actually land in production.
        finalizer.start()
        assert inside.wait(timeout=10), "the finalize never reached its lock hold"

    _fake_generate(monkeypatch, summary="新的說明", side_effect=start_finalize_mid_call)

    outcome = asyncio.run(regenerate_summary("kbsearch"))

    finalizer.join(timeout=10)
    assert not finalizer.is_alive()
    assert not failures, failures
    assert outcome is tool_meta.StoreRefusal.FINALIZED
    # Byte-for-byte what 定版 froze: the in-flight generation left no trace, not
    # even a refreshed updated_at.
    assert (pkg / tools._AI_META_FILENAME).read_bytes() == frozen["bytes"]
    stored = tools.read_tool_meta(pkg)
    assert stored is not None
    assert stored["summary"] == "定版的說明"
    assert stored["status"] == "final"


def test_regenerate_summary_builds_the_prompt_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The prompt build walks the whole package, opens every file it keeps, and
    parses the .env -- blocking filesystem work that used to run INLINE on the
    event loop, stalling every other request in the process until it finished.

    ``_MAX_FILES`` is not the bound people assume: it caps what is KEPT, not what
    ``os.walk`` must ENUMERATE, so a package a run_shell exploded a node_modules
    into is unbounded work. Asserted by THREAD, which is the actual property --
    asyncio.run drives the loop on this thread, so a different one means the
    threadpool hop really happened."""
    root = tmp_path / "tools"
    _package(root)
    _summary_settings(monkeypatch, root)
    loop_thread = threading.current_thread()
    seen: dict[str, Any] = {}
    real_prompt = tool_meta._summary_user_prompt

    def spy(*args: Any, **kwargs: Any) -> str:
        seen["thread"] = threading.current_thread()
        return real_prompt(*args, **kwargs)

    monkeypatch.setattr(tool_meta, "_summary_user_prompt", spy)
    _fake_generate(monkeypatch, summary="新的說明")

    asyncio.run(regenerate_summary("kbsearch"))

    assert seen["thread"] is not loop_thread


@pytest.mark.parametrize(
    "entry_point",
    ["regenerate", "install-hook"],
)
def test_sidecar_io_never_runs_on_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry_point: str
) -> None:
    """The resolve, the sidecar read and the STORE are blocking work, and both
    entry points hop them onto a worker.

    The store is the one that matters most now: it holds ``tools._META_LOCK``
    across a read-write-read, and a lock taken on the event loop parks the WHOLE
    process (every other request in flight) behind one package's sidecar I/O
    rather than one threadpool worker. The install hook is included because it
    runs from a background task that shares the same loop -- its blocking work is
    exactly as unwelcome there as a route's.

    Asserted by THREAD, like the prompt-build test above: asyncio.run drives the
    loop on this thread, so a different one means the hop really happened."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    _write_meta(pkg, summary="舊的", status="draft")
    loop_thread = threading.current_thread()
    seen: dict[str, Any] = {}
    real_store = tool_meta._store_meta
    real_resolve = tools._resolve_package_dir_no_alias
    real_read = tools.read_tool_meta

    def store_spy(*args: Any, **kwargs: Any) -> Any:
        seen["store"] = threading.current_thread()
        return real_store(*args, **kwargs)

    def resolve_spy(*args: Any, **kwargs: Any) -> Any:
        seen["resolve"] = threading.current_thread()
        return real_resolve(*args, **kwargs)

    def read_spy(*args: Any, **kwargs: Any) -> Any:
        seen["read"] = threading.current_thread()
        return real_read(*args, **kwargs)

    monkeypatch.setattr(tool_meta, "_store_meta", store_spy)
    monkeypatch.setattr(tools, "_resolve_package_dir_no_alias", resolve_spy)
    monkeypatch.setattr(tools, "read_tool_meta", read_spy)
    _fake_generate(monkeypatch, summary="新的說明")

    if entry_point == "regenerate":
        asyncio.run(regenerate_summary("kbsearch"))
    else:
        asyncio.run(generate_and_store_summary("kbsearch"))

    assert seen["resolve"] is not loop_thread
    assert seen["read"] is not loop_thread
    assert seen["store"] is not loop_thread
