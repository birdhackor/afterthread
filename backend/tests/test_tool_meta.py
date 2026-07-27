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
import os
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


def test_summary_result_validates_shape_and_keeps_the_text_verbatim() -> None:
    """The validator coerces and checks; it does not REWRITE.

    Redaction, the strip and the ``_TOOL_SUMMARY_CAP`` cut all moved to the store
    step (see ``tools.store_summary_meta``), and they moved TOGETHER because they
    are one ordered operation -- leaving the strip behind here would have run it
    before the redaction, which is the leak the order exists to prevent."""
    result = ToolSummaryResult.model_validate({"summary": "  spaced  "})
    assert result.summary == "  spaced  "
    long_text = "y" * (tools._TOOL_SUMMARY_CAP + 500)
    assert ToolSummaryResult.model_validate({"summary": long_text}).summary == long_text
    # Still coerced to a string: a model that answered with a number is a shape
    # this validator repairs rather than rejects.
    assert ToolSummaryResult.model_validate({"summary": 12}).summary == "12"


def test_summary_result_validation_touches_no_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    """``model_validate`` runs on the EVENT LOOP, inside generate_structured.

    It used to call ``redact_known_secrets`` there, whose provider sweeps the
    tools directory -- an ``iterdir`` plus a ``stat`` per package, and a ``.env``
    read on every cache miss -- so every summary generation did blocking
    filesystem work on the loop that carries every other request in the process.
    Both registry entry points are booby-trapped here: validation must not reach
    either."""

    def explode() -> Any:
        raise AssertionError("validation must not read the filesystem")

    monkeypatch.setattr(tools, "known_secret_values", explode)
    monkeypatch.setattr(tools, "tools_dir", explode)

    assert (
        ToolSummaryResult.model_validate({"summary": "這個工具會查 KB"}).summary
        == "這個工具會查 KB"
    )


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


# --- the origin URL is reduced to provenance ----------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (
            "https://kb.example/openapi.json",
            "https://kb.example" + tool_meta._ORIGIN_URL_TRIMMED_MARKER,
        ),
        (
            "https://kb.example/openapi.json?token=abc123",
            "https://kb.example" + tool_meta._ORIGIN_URL_TRIMMED_MARKER,
        ),
        (
            "https://user:pass@kb.example:8443/o.json",
            "https://kb.example:8443" + tool_meta._ORIGIN_URL_TRIMMED_MARKER,
        ),
        (
            "https://kb.example/o.json#section",
            "https://kb.example" + tool_meta._ORIGIN_URL_TRIMMED_MARKER,
        ),
        (
            "http://[::1]:8080/o.json?a=b",
            "http://[::1]:8080" + tool_meta._ORIGIN_URL_TRIMMED_MARKER,
        ),
        # R5-1: a matrix parameter is just PATH to urlsplit (it has no concept
        # of ";params" -- that is urlparse's, and even there a historical
        # HTTP-only convention), so a capability token riding there is exactly
        # as unmatchable by redaction as one in the query.
        (
            "https://kb.example/openapi.json;jsessionid=CAPABILITY-TOKEN",
            "https://kb.example" + tool_meta._ORIGIN_URL_TRIMMED_MARKER,
        ),
        # R5-1, same shape in a different position: a percent-encoded token
        # living in a PATH segment rather than the query.
        (
            "https://kb.example/reports/token%3DCAPABILITY-abcdef/openapi.json",
            "https://kb.example" + tool_meta._ORIGIN_URL_TRIMMED_MARKER,
        ),
        # No path at all: nothing to cut, so no marker either.
        ("https://kb.example", "https://kb.example"),
        # A lone "/" carries no information of its own -- pinned to the SAME
        # no-marker bucket as no-path-at-all (see the function docstring),
        # not the marker bucket a real path gets.
        ("https://kb.example/", "https://kb.example"),
        # Scheme is case-insensitive and normalized to lower on the way out.
        (
            "HTTPS://kb.example/x",
            "https://kb.example" + tool_meta._ORIGIN_URL_TRIMMED_MARKER,
        ),
        (
            "https://[::1]:8443/x",
            "https://[::1]:8443" + tool_meta._ORIGIN_URL_TRIMMED_MARKER,
        ),
        # A real-world IPv6 literal with hex-letter groups and no IPv4-mapped
        # tail -- R6-1's tightened bracket whitelist (hex digits, ":", ".")
        # must keep admitting this shape, not just the degenerate "::1".
        (
            "https://[2001:db8::1]:8443/x",
            "https://[2001:db8::1]:8443" + tool_meta._ORIGIN_URL_TRIMMED_MARKER,
        ),
        # R5-2: urlsplit does not validate netloc characters, so this parses
        # to a non-empty netloc ("Bearer SECRET") exactly like a real host --
        # "it parsed" is not "it is a host", and the raw credential-shaped
        # string must never be the return value.
        ("https://Bearer SECRET/openapi", ""),
        # R6-1: the SAME "it parsed" trap, one character class later. A
        # balanced bracket pair is not by itself a shape check -- CPython's
        # urlsplit tolerates RFC 3986's IPvFuture form without validating what
        # follows "v1.", so this parsed into netloc "[v1.Bearer SECRET]" under
        # the old any-non-"]" bracket branch exactly as readily as a real IPv6
        # literal, carrying the credential-shaped text straight into the host
        # slot.
        ("https://[v1.Bearer SECRET]/openapi", ""),
        # R6-1: an IPv6 zone ID (RFC 6874, "%25<zone>") is likewise excluded by
        # the tightened whitelist -- vanishingly rare for an OpenAPI host, and
        # the old bracket branch admitted it with the same "any non-']' char"
        # laxity as the credential-shaped case above.
        ("https://[fe80::1%25eth0]/x", ""),
        ("ftp://kb.example/x", ""),  # not http/https
        # A non-numeric port is now a netloc that fails the same shape check
        # as any other -- no longer waved through as "cosmetic" (r4's stance).
        ("http://kb.example:notaport/o.json", ""),
        ("http://[::1/o.json", ""),  # urlsplit raises: unparseable
        ("kb.example/openapi.json", ""),  # no scheme
        ("https:///o.json", ""),  # no host
        ("not a url at all", ""),
        ("", ""),
        ("   ", ""),
    ],
    ids=[
        "path",
        "query",
        "userinfo",
        "fragment",
        "ipv6-query",
        "jsessionid-path",
        "percent-encoded-path",
        "bare-host",
        "root-path",
        "scheme-case",
        "ipv6-port",
        "ipv6-hex-port",
        "netloc-not-a-host",
        "bracket-netloc-not-a-host",
        "ipv6-zone-id",
        "non-http-scheme",
        "odd-port",
        "unparseable",
        "no-scheme",
        "no-host",
        "garbage",
        "empty",
        "blank",
    ],
)
def test_sanitized_origin_url(raw: str, expected: str) -> None:
    """scheme://host[:port] survives; userinfo, PATH, query and fragment do not.

    r5 closes the class r4 started: path joins userinfo/query/fragment on the
    cut list (a matrix parameter or an unregistered token living in a path
    segment is exactly as unmatchable by redaction as one in the query), and a
    netloc that merely PARSES is validated before being emitted rather than
    trusted (``urlsplit`` never checks its characters, so "Bearer SECRET"
    parses into a netloc just as readily as a real host). Anything
    unparseable, non-http(s), or not a real host[:port] degrades to "", never
    to the raw value."""
    assert tool_meta._sanitized_origin_url(raw) == expected
    # Idempotency, checked for every row rather than one illustrative case:
    # the marker is glued directly onto the bare host now (no path left to
    # provide a natural "/" delimiter the way r4 had), so this is the one
    # property that would silently regress if a future edit moved the peel
    # step to after validation instead of before parsing.
    assert tool_meta._sanitized_origin_url(expected) == expected


def test_sanitized_origin_url_is_idempotent() -> None:
    """Three call sites sanitize (capture, prompt, sidecar read-back), so a
    value may pass through more than once. With the path gone, the marker
    lands glued directly onto the bare host -- no "/" left to delimit it from
    a real path the way r4 had -- so a naive re-parse would read the marker's
    own text as part of the netloc and fail host validation. The function
    peels a trailing marker off before parsing instead, which is what keeps
    this a no-op rather than a refusal."""
    once = tool_meta._sanitized_origin_url("https://u@kb.example/o.json?token=x#f")
    assert once == "https://kb.example" + tool_meta._ORIGIN_URL_TRIMMED_MARKER
    assert tool_meta._sanitized_origin_url(once) == once
    assert once.count(tool_meta._ORIGIN_URL_TRIMMED_MARKER) == 1


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
    assert "http://kb.example" in prompt  # install origin (host-only, r5)
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
    # The URL's HOST survives -- the model still learns which host this was
    # built from -- while the PATH and the query that held the token are both
    # gone entirely (r4 kept the path; r5 drops it too, see
    # test_user_prompt_drops_url_credentials_redaction_cannot_reach for the
    # path-borne-token case that motivated it).
    assert "https://kb.example" in prompt
    assert "openapi.json" not in prompt
    assert "token=" not in prompt
    assert tool_meta._ORIGIN_URL_TRIMMED_MARKER in prompt


@pytest.mark.parametrize(
    "url, needle",
    [
        (
            "https://kb.example/openapi.json?X-Amz-Signature=UNREGISTERED-PRESIGNED-abcdef",
            "UNREGISTERED-PRESIGNED-abcdef",
        ),
        ("https://kb.example/openapi.json?token=abc123%2B%2FXYZ", "abc123%2B%2FXYZ"),
        ("https://ops:UNREGISTERED-BASIC-abcdef@kb.example/openapi.json", "UNREGISTERED-BASIC"),
    ],
    ids=["presigned-unknown", "known-but-percent-encoded", "userinfo"],
)
def test_user_prompt_drops_url_credentials_redaction_cannot_reach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str, needle: str
) -> None:
    """Value-matching redaction is necessary and NOT sufficient for a URL.

    Two failures it cannot cover, both ordinary: a credential nobody registered
    (a presigned document link, HTTP basic userinfo pasted into the address), and
    a REGISTERED one the URL carries in another encoding -- ``abc123+/XYZ`` is
    what the installer knows, ``abc123%2B%2FXYZ`` is what the URL says, and an
    exact substring match sees two different strings. So the credential-bearing
    parts are dropped structurally instead."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    # The registered form of the second case -- known to the redactor, and still
    # unmatchable against the percent-encoded text in the URL.
    monkeypatch.setattr(tools, "known_secret_values", lambda: frozenset({"abc123+/XYZ"}))

    prompt = tool_meta._summary_user_prompt(
        "kbsearch", pkg, origin={"openapi_url": url}, builder_summary=None
    )

    assert needle not in prompt
    assert "https://kb.example" in prompt  # the provenance survives (host-only, r5)


@pytest.mark.parametrize(
    "url",
    ["RAW-JUNK-not-a-url", "http://[::1/RAW-JUNK.json", "   "],
    ids=["garbage", "unparseable", "blank"],
)
def test_user_prompt_omits_an_unusable_origin_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str
) -> None:
    """A URL we cannot parse is not echoed on the theory that it is probably a
    URL: the whole line is absent, rather than a label followed by the raw value
    (or by nothing). The sidecar is hand-editable, so these shapes reach the
    prompt for real."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)

    prompt = tool_meta._summary_user_prompt(
        "kbsearch", pkg, origin={"openapi_url": url}, builder_summary=None
    )

    assert "It was built from this OpenAPI document" not in prompt
    assert "RAW-JUNK" not in prompt


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX filename byte-encoding only")
def test_user_prompt_scrubs_a_non_utf8_filename_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R6-3: a filename that is not valid UTF-8 must not reach the LLM request as
    a lone surrogate -- the SAME filesystem-boundary failure ``tools._utf8_safe``
    closes for a hand-edited sidecar (r3), one boundary over. A builder's
    ``run_shell`` can write a file under a name that is raw, non-UTF-8 bytes
    (``b"note-\\xff.txt"``); ``os.walk`` decodes that name through the OS's own
    surrogateescape convention into a ``str`` carrying a LONE surrogate, which no
    substring-based mask touches and which a strict UTF-8 encode (what the LLM
    request ultimately performs) refuses outright."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    raw_name = b"note-\xff.txt"
    fd = os.open(os.fsencode(pkg) + b"/" + raw_name, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, b"hello\n")
    os.close(fd)
    decoded_name = os.fsdecode(raw_name)
    assert "\udcff" in decoded_name  # sanity: this OS really does surrogateescape it

    prompt = tool_meta._summary_user_prompt("kbsearch", pkg, origin=None, builder_summary=None)

    prompt.encode("utf-8")  # the real proof: a lone surrogate would raise here
    assert "\udcff" not in prompt
    # The header shows the SAME text tools._utf8_safe would produce for this
    # name -- U+FFFD standing in for the byte that was never valid UTF-8.
    assert f"{tools._utf8_safe(decoded_name)}:\n" in prompt


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

    identity = tools.package_identity(pkg)
    assert identity is not None
    assert (
        tool_meta._store_meta(pkg, summary="新的", origin=None, llm_log_id=9, identity=identity)
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
    # The failed session's TRACE is linked too -- and that is what makes this a
    # placeholder rather than the origin-only file the same call already wrote
    # before the round trip (O8-1). Left to the "only when no sidecar exists"
    # precondition alone, the early write would have suppressed this one and the
    # operator would have lost the 查看 AI 日誌 link to the failure.
    assert meta["llm_log_id"] == llm_log.last_record_id_for_workflow("tool_summary")
    assert meta["llm_log_id"] is not None


def _seed_previous_summary_session() -> int:
    """A finished ``tool_summary`` record for SOME OTHER tool, in the ring.

    The placeholder path stamps ``last_record_id_for_workflow("tool_summary")``,
    so without a previous record of that workflow the two failure modes (stamping
    my own session vs. stamping whatever was newest) are indistinguishable: both
    would be None. This is the previous tool's trace a regression would borrow.
    """
    recorder = llm_log.LlmInteractionRecorder(workflow="tool_summary", model="m")
    recorder.begin_attempt([{"role": "user", "content": "summarize the OTHER tool"}])
    recorder.record_response("the other tool's summary")
    recorder.finish(outcome="ok", error=None)
    previous = llm_log.last_record_id_for_workflow("tool_summary")
    assert previous is not None
    return previous


def test_placeholder_carries_no_log_id_when_the_prompt_build_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R9-3: a failure BEFORE any session started stamps None, not the last one.

    ``_generate_summary`` builds its prompt first -- the fail-closed redactor, the
    ``.env`` parse, a walk over every file in the package -- and only then calls
    ``generate_structured``, which is what opens a session. A failure in that
    first half leaves the workflow's newest record exactly as it was: the
    PREVIOUS summary session, belonging to whatever tool was summarized last. The
    caller's ``except`` covers both halves and used to stamp that id onto this
    tool's placeholder, so the UI offered another tool's full prompt/response as
    this one's failure trace.

    The failure is spelled as the prompt builder raising, which is the same shape
    the fail-closed redactor produces (see the existing prompt-building test) but
    leaves the SIDECAR writer working -- that test keeps the secret provider down,
    so no placeholder is written at all and this path stayed uncovered."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    previous = _seed_previous_summary_session()

    def boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("prompt build failed")

    async def must_not_generate(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the prompt never got built, so no session may start")

    monkeypatch.setattr(tool_meta, "_summary_user_prompt", boom)
    monkeypatch.setattr("afterthread.services.tool_meta.generate_structured", must_not_generate)

    asyncio.run(generate_and_store_summary("kbsearch", origin={"instructions": "查 KB"}))

    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["summary"] == ""  # the placeholder really was written ...
    assert meta["origin"] == {"openapi_url": None, "instructions": "查 KB"}
    assert meta["llm_log_id"] is None  # ... with no trace, rather than someone else's
    # The other tool's record is still the workflow's newest, so a regression
    # here borrows THAT id visibly rather than silently having nothing to take.
    assert llm_log.last_record_id_for_workflow("tool_summary") == previous


def test_placeholder_carries_its_own_session_when_the_llm_call_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other side of the same rule: a session that DID start is linked.

    A failure raised from the LLM call has a record of its own -- created by
    ``generate_structured`` before it raises -- so the placeholder stamps that,
    not None and not the previous tool's."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    previous = _seed_previous_summary_session()
    _fake_generate(monkeypatch, explode=LLMUpstreamError("Timeout: slow"))

    asyncio.run(generate_and_store_summary("kbsearch", origin={"instructions": "查 KB"}))

    meta = tools.read_tool_meta(pkg)
    assert meta is not None
    assert meta["llm_log_id"] == llm_log.last_record_id_for_workflow("tool_summary")
    assert meta["llm_log_id"] != previous  # this call's own session, not the older one


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
    install had. The .env values still never ride along.

    The distinguishing marker lives in the HOST (a subdomain) rather than the
    path this time: r5 drops the path entirely, so a marker placed there (as
    this test used before r5) would never reach the prompt at all -- the host
    is the only part of a stored URL guaranteed to survive."""
    root = tmp_path / "tools"
    secret = "kb-live-secret-abcdef"
    pkg = _package(root, dotenv=f"KB_API_KEY={secret}\n")
    _summary_settings(monkeypatch, root)
    _write_meta(
        pkg,
        summary="舊的",
        status="draft",
        origin={
            "openapi_url": "http://origin-url-marker.kb.example/o.json",
            "instructions": "ORIGIN-INSTRUCTIONS-MARKER 只查內部 KB",
        },
    )
    captured = _fake_generate(monkeypatch, summary="新的說明")

    meta = asyncio.run(regenerate_summary("kbsearch"))

    assert "ORIGIN-INSTRUCTIONS-MARKER 只查內部 KB" in captured["user_prompt"]
    assert "origin-url-marker.kb.example" in captured["user_prompt"]
    assert secret not in captured["user_prompt"]
    assert isinstance(meta, dict)
    assert meta["origin"]["instructions"] == "ORIGIN-INSTRUCTIONS-MARKER 只查內部 KB"


def test_regenerate_summary_sanitizes_a_legacy_origin_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sidecar written BEFORE the URL was reduced (or hand-edited since) is the
    other door the credential comes back through.

    The install sanitizes at capture, but the sidecar on disk is the only copy of
    the origin and regenerate reads it back -- into the prompt, and then back onto
    disk when the store rewrites the origin it was handed. Both are covered here,
    which is also what heals the file: no migration, the first regeneration
    rewrites the origin in the reduced form."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    # Written by hand: a legacy raw URL is exactly what our writer no longer
    # produces, so this is the only way to get one onto disk.
    (pkg / tools._AI_META_FILENAME).write_text(
        json.dumps(
            {
                "summary": "舊的",
                "status": "draft",
                "origin": {
                    "openapi_url": "https://ops:LEGACY-BASIC@kb.example/o.json?token=LEGACY-TOKEN",
                    "instructions": "ORIGIN-INSTRUCTIONS-MARKER",
                },
            }
        ),
        encoding="utf-8",
    )
    captured = _fake_generate(monkeypatch, summary="新的說明")

    meta = asyncio.run(regenerate_summary("kbsearch"))

    assert "LEGACY-TOKEN" not in captured["user_prompt"]
    assert "LEGACY-BASIC" not in captured["user_prompt"]
    assert "https://kb.example" in captured["user_prompt"]
    assert "o.json" not in captured["user_prompt"]  # r5: the path is gone too
    # ... and the rewritten sidecar carries the reduced URL, not the raw one.
    assert isinstance(meta, dict)
    stored_url = meta["origin"]["openapi_url"]
    assert stored_url == "https://kb.example" + tool_meta._ORIGIN_URL_TRIMMED_MARKER
    assert meta["origin"]["instructions"] == "ORIGIN-INSTRUCTIONS-MARKER"
    assert "LEGACY-TOKEN" not in (pkg / tools._AI_META_FILENAME).read_text(encoding="utf-8")


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


def _replace_package(root: Path, name: str = "kbsearch") -> Path:
    """Delete ``name`` and install a DIFFERENT package under the same name.

    What an operator can do inside one summary generation without touching the
    job domain at all: the tool job single-flight covers install/revise/regenerate,
    never a plain delete followed by an install. The replacement carries its own
    ``tool.json`` -- which is what makes it a different package identity -- and its
    own sidecar, so a write that landed here would be visible as BOTH a lost
    summary and a stolen one."""
    import shutil

    shutil.rmtree(root / name)
    replacement = _package(root, name, run_py="import sys\nsys.stdout.write('B')\n")
    _write_meta(replacement, summary="新工具自己的總結", status="draft")
    return replacement


def test_regenerate_summary_writes_nothing_when_the_package_was_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A summary belongs to the package it was generated FROM, not to the name.

    The generation resolves a directory, spends an LLM round trip, then writes --
    and a delete plus a same-name install inside that window is not blocked by
    anything (the job single-flight does not cover it). Without the identity the
    resolve captured, package A's summary AND A's origin would be persisted into
    package B, over B's own.

    The answer is the same None a vanished package gives, because from the
    caller's side both mean "the regeneration did not happen" -- and the route
    folds that into its 404 rather than reporting a summary that is nowhere."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    _write_meta(pkg, summary="A 的總結", status="draft")
    replaced: dict[str, Path] = {}

    def replace_mid_call() -> None:
        replaced["pkg"] = _replace_package(root)

    _fake_generate(monkeypatch, summary="A 的新說明", side_effect=replace_mid_call)

    assert asyncio.run(regenerate_summary("kbsearch")) is None

    stored = tools.read_tool_meta(replaced["pkg"])
    assert stored is not None
    assert stored["summary"] == "新工具自己的總結"  # B's own sidecar, untouched


def test_generate_and_store_summary_writes_nothing_when_the_package_was_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The install hook takes the same guard, and swallows the refusal like every
    other store outcome -- an install that already succeeded must never be failed
    by its summary.

    Both of its writes are covered, because both carry this session's origin and
    log id: the SUMMARY here, and the PLACEHOLDER (below) which is written when the
    generation failed. A placeholder landing in package B would attach A's install
    origin -- the URL and the operator's instructions -- to a package B that was
    installed from something else entirely, and every later revise of B would read
    that origin back as first-hand context."""
    root = tmp_path / "tools"
    _package(root)
    _summary_settings(monkeypatch, root)

    def replace_mid_call() -> None:
        _replace_package(root)

    _fake_generate(monkeypatch, summary="A 的說明", side_effect=replace_mid_call)

    asyncio.run(generate_and_store_summary("kbsearch", origin={"instructions": "查 A"}))

    stored = tools.read_tool_meta(root / "kbsearch")
    assert stored is not None
    assert stored["summary"] == "新工具自己的總結"
    assert stored["origin"] is None  # A's install context never reached B


def test_generate_and_store_summary_placeholder_respects_the_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The FAILURE path writes too, so it is guarded too.

    A failed generation leaves an empty summary plus the origin and the failed
    session's log id -- the same misattribution with a shorter body if it lands in
    the wrong package. Driven with a replacement that has NO sidecar, so the
    placeholder's own "only when there is nothing there" precondition is met at
    the target and only the identity can stop the write."""
    root = tmp_path / "tools"
    _package(root)
    _summary_settings(monkeypatch, root)

    def replace_then_fail() -> None:
        import shutil

        shutil.rmtree(root / "kbsearch")
        _package(root, "kbsearch", run_py="import sys\nsys.stdout.write('B')\n")

    _fake_generate(
        monkeypatch,
        explode=LLMUpstreamError("upstream is down"),
        side_effect=replace_then_fail,
    )

    asyncio.run(generate_and_store_summary("kbsearch", origin={"instructions": "查 A"}))

    assert tools.read_tool_meta(root / "kbsearch") is None  # B got no sidecar at all


def test_the_install_origin_reaches_disk_before_the_llm_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The un-regenerable half of the sidecar is written BEFORE the generation.

    ``origin`` -- the OpenAPI url and the operator's instructions -- is captured
    nowhere else in the system (``tool_builder.run_install`` says so at the hook
    call), while the summary TEXT can be regenerated from the files at any time.
    Persisting the origin at the resolve is what makes the round trip risk the
    regenerable half alone. Observed from INSIDE the stubbed generation, which is
    exactly the window the real round trip occupies."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    origin = {"openapi_url": "http://kb.example", "instructions": "查 KB"}
    seen: dict[str, Any] = {}

    def look_at_the_sidecar() -> None:
        seen["meta"] = tools.read_tool_meta(pkg)

    _fake_generate(monkeypatch, summary="這個工具會查 KB", side_effect=look_at_the_sidecar)

    asyncio.run(generate_and_store_summary("kbsearch", origin=origin, builder_summary="built it"))

    early = seen["meta"]
    assert early is not None
    assert early["origin"] == origin
    # ORIGIN ONLY: nothing has been generated yet, and no summary session has
    # finished, so there is no log id to vouch for either.
    assert early["summary"] == ""
    assert early["status"] == "draft"
    assert early["llm_log_id"] is None
    assert early["llm_log_process"] is None

    # ...and the ordinary path still ends with the FULL meta. The early write is
    # an ADDITION, not a replacement: it costs no field of the final one.
    final = tools.read_tool_meta(pkg)
    assert final is not None
    assert final["summary"] == "這個工具會查 KB"
    assert final["status"] == "draft"
    assert final["origin"] == origin
    assert final["llm_log_id"] == llm_log.last_record_id_for_workflow("tool_summary")
    assert final["llm_log_process"] == llm_log.process_token()


def test_an_enabled_toggle_during_the_generation_now_costs_nothing_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """O8-1's whole scenario, re-measured after web-v5 P1: nothing is lost any more.

    THE HISTORY, because this test's assertions are the inversion of what it used
    to pin. ``PATCH /api/tools/{name}`` takes no admission reservation and no
    per-package guard, and ``set_enabled`` USED TO rewrite ``tool.json`` in place --
    which MOVED the manifest identity this hook captured at its resolve (D40 r5).
    The sidecar write at the end was then correctly REFUSED, the install hook
    swallowed the refusal as it swallows every store outcome, and the job still
    reported success. r8's fix was to persist the un-regenerable half (the
    ``origin``) BEFORE the round trip, so a toggle cost the summary TEXT -- which
    any later regeneration rebuilds -- instead of the OpenAPI url and the
    operator's instructions, which are captured nowhere else in the system.

    web-v5 P1 removes the mechanism rather than the instance: a toggle writes
    ``.afterthread-state.json`` and leaves the manifest byte-identical, so the identity the
    hook is holding does not move, the guard has nothing to refuse, and the FULL
    sidecar lands. r8's early write stays exactly where it is -- it closes the same
    window against a genuine mid-round-trip REPLACEMENT, which still moves the
    identity and still must be refused.

    Driven from INSIDE the generation, not by racing a thread, so it is the window
    itself that is pinned."""
    root = tmp_path / "tools"
    pkg = _package(root)
    _summary_settings(monkeypatch, root)
    origin = {"openapi_url": "http://kb.example", "instructions": "查 KB"}
    manifest_before = (pkg / "tool.json").read_bytes()
    identity_before = tools.package_identity(pkg)

    def toggle_mid_call() -> None:
        assert tools.set_enabled("kbsearch", False) is True

    _fake_generate(monkeypatch, summary="這個工具會查 KB", side_effect=toggle_mid_call)
    asyncio.run(generate_and_store_summary("kbsearch", origin=origin))

    # The premise, measured in place: the toggle really happened, and really moved
    # nothing the sidecar's identity guard looks at.
    assert {t["name"]: t["enabled"] for t in tools.list_tools()}["kbsearch"] is False
    assert (pkg / "tool.json").read_bytes() == manifest_before
    assert tools.package_identity(pkg) == identity_before

    after = tools.read_tool_meta(pkg)
    assert after is not None
    assert after["summary"] == "這個工具會查 KB"  # the regenerable half: no longer lost
    assert after["origin"] == origin  # the un-regenerable half: still safe (r8)

    # And the recovery path really can read it back -- which is the whole reason
    # the origin is worth saving: a later regeneration feeds it into its own prompt
    # as first-hand context and keeps it in the sidecar it rewrites.
    captured = _fake_generate(monkeypatch, summary="重新產生的說明")
    meta = asyncio.run(regenerate_summary("kbsearch"))
    assert isinstance(meta, dict)
    assert meta["summary"] == "重新產生的說明"
    assert meta["origin"] == origin
    assert "http://kb.example" in captured["user_prompt"]
    assert "查 KB" in captured["user_prompt"]


def test_summary_paths_refuse_a_package_with_no_manifest_before_the_llm_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No identity, no write -- and the refusal happens at the DOOR.

    "A check that cannot speak must not vouch" (D40 P3b r11) applies to the store
    the same way it applies to the revise, so a directory whose ``tool.json``
    cannot be read cannot have a summary stored against it. Refusing at the
    resolve rather than after the round trip is the same call r11 made for the
    same reason: burning a whole LLM session to then throw the answer away helps
    nobody, and such a package is invalid -- it cannot be executed or revised
    either. Pinned by the generation never being CALLED."""
    root = tmp_path / "tools"
    pkg = _package(root)
    (pkg / "tool.json").unlink()
    _summary_settings(monkeypatch, root)
    captured = _fake_generate(monkeypatch, summary="不該被產生")

    assert asyncio.run(regenerate_summary("kbsearch")) is None
    asyncio.run(generate_and_store_summary("kbsearch", origin=None))  # must not raise

    assert captured == {}  # no LLM session was started by either path
    assert tools.read_tool_meta(pkg) is None


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

    def write_then_park(directory: Path, meta: dict[str, Any], **kwargs: Any) -> bool:
        # Runs INSIDE set_summary_status's lock hold. Parking here is what forces
        # the store to arrive while the finalize is mid-sequence -- the exact
        # window the r2 code lost the race in. ``**kwargs`` keeps this double
        # TRANSPARENT to the identity the caller now hands the writer (R6-3): this
        # test is about the lock, and a double that dropped that argument would be
        # testing a call shape production no longer makes.
        if meta.get("status") == "final":
            inside.set()
            assert blocked.wait(timeout=5), "the store never contended for _META_LOCK"
        return real_write(directory, meta, **kwargs)

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
