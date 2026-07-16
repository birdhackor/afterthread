#!/usr/bin/env python3
"""OpenAI-compatible mock LLM server for the Context Memory e2e smoke harness.

STDLIB ONLY (http.server / json / argparse / re / time / sys) so it runs under a
bare ``python3`` with no venv, independent of the backend's uv environment.

It speaks just enough of the OpenAI Chat Completions API for the backend's
``openai`` SDK client to talk to it: it accepts ``POST <base>/chat/completions``
(the SDK posts to ``<base_url>/chat/completions``; with a base URL of
``http://127.0.0.1:<port>/v1`` that is ``/v1/chat/completions``) and returns a
well-formed chat-completion object whose ``choices[0].message.content`` is a
single JSON string.

Which JSON it returns is decided by inspecting the request's *system* message,
whose text is one of the three workflow prompts defined verbatim in
``context_memory/services/memory_ai.py``. Each prompt carries a distinctive marker:

  * "quick-capture assistant" -> capture  -> a ``CaptureDraft`` object
  * "enrichment assistant"    -> enrich   -> an ``EnrichResult`` object
  * "update assistant"        -> update   -> an ``UpdateResult`` object

Any other system prompt is unknown and, like ``--mode garbage``, yields a
deliberate non-JSON prose reply so the backend's strict parser rejects it
(exercising the 502 ``llm_upstream_error`` path).

Within enrich/update the *user* message is additionally scanned for a
``#variant:<name>`` directive so a single running mock can return different
canned results on successive calls (the smoke uses this to drive the
supersede-then-complete enrichment sequence). Without a directive the workflow's
"default" variant is used. All content is realistic Traditional Chinese.

One special directive, ``#variant:tool_roundtrip``, exercises the tool-calling
loop (D14): the first reply (no tool result in the conversation yet) is a
``tool_calls`` completion calling a tool named ``echo``, and the reply after the
backend feeds that tool's result back is the workflow's normal good JSON. It is
a CAPABILITY for manual / pytest-adjacent use and is NOT asserted by smoke.sh --
with the smoke's tools_dir unset the backend advertises no tools, so nothing
drives it there.

Modes (``--mode``):
  * good    -> valid JSON for the matched workflow (the normal happy path).
  * garbage -> always a prose junk reply (no JSON object), for the 502 path.
  * slow    -> sleeps ``--slow-seconds`` before replying with otherwise-good
               JSON, so a backend with a tiny OPENAI_TIMEOUT_SECONDS times out
               first (exercising the 502 ``Timeout`` path).
  * hold    -> logs "holding response" then blocks until ``--release-file``
               appears (polling every 0.1s, hard-capped at 60s so a forgotten
               release can't hang the process), then replies with otherwise-
               good JSON. Turns a race (e.g. "does a concurrent PATCH land
               before an in-flight enrich's LLM call returns?") into a
               barrier: the caller only lets go of the release once whatever
               must happen first has definitely happened.

Every request is logged as one line to stderr. At startup the server also
logs the port it actually bound (see ``--port 0`` below) in a distinctive
line a caller can wait on and parse.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# --- workflow selection ----------------------------------------------------

# Distinctive substrings from the three system prompts in
# context_memory/services/memory_ai.py (CAPTURE_/ENRICH_/UPDATE_SYSTEM_PROMPT). Matching on
# these -- rather than the whole prompt -- keeps the mock robust to the schema
# block and strict-output rule that context_memory/services/llm.py appends after them.
_CAPTURE_MARKER = "quick-capture assistant"
_ENRICH_MARKER = "enrichment assistant"
_UPDATE_MARKER = "update assistant"

# Optional per-call selector the smoke embeds in the user text (additional
# context / progress note) so one mock can return different canned results
# across calls of the same workflow.
_VARIANT_RE = re.compile(r"#variant:([A-Za-z0-9_]+)")


# --- canned Traditional-Chinese responses ----------------------------------


def _capture_draft() -> dict[str, Any]:
    """A full CaptureDraft: every field populated, two follow-up questions."""
    return {
        "title": "重構付款流程以支援新金流商",
        "snapshot": "團隊討論如何重構付款流程,以支援新的金流服務商並提升結帳體驗。",
        "why_matters": "直接影響結帳轉換率與營收。",
        "known": "- 目前使用舊版付款 API\n- 已有兩家金流商候選",
        "inferred": "- 推測需要新增 webhook 處理(推論)",
        "unknown": "- 尚未確認最終金流商\n- 上線時程未定",
        "next_actions": "- 向候選金流商索取技術文件",
        "recovery_keywords": "付款, 金流, 結帳, webhook",
        "recovery_people": "Alice(PM), Bob(後端)",
        "recovery_files": "src/payment/service.py",
        "resume_trigger": "當金流商回覆技術文件時",
        "tags": ["付款", "重構", "金流"],
        "suggested_status": "needs-enrichment",
        "questions": ["需要優先支援哪些金流商?", "預計的上線時程為何?"],
    }


# Enrich variants. "seed" writes an initial decision + gaps (incomplete, so no
# promotion); "complete" REWRITES that decision (so the server must supersede
# the prior one) and marks the checklist complete (promoting a still-capturing
# item to active/full). "default" is a generic incomplete enrichment.
_ENRICH_VARIANTS: dict[str, dict[str, Any]] = {
    "seed": {
        "sections": {
            "decisions": "採用方案A:直接串接金流商 API",
            "why_matters": "付款流程直接影響結帳轉換率與營收",
            "known": "- 已確認兩家金流商候選",
        },
        "checklist_complete": False,
        "remaining_gaps": ["缺少 stakeholders 名單", "缺少上線時程"],
        "progress_note": "補充初步決策與背景資訊",
    },
    "complete": {
        "sections": {
            "decisions": "改採方案B:改用第三方支付聚合服務",
            "risks": "第三方服務中斷將影響結帳",
        },
        "checklist_complete": True,
        "remaining_gaps": [],
        "progress_note": "checklist 完成,決策改為方案B",
    },
    "default": {
        "sections": {"snapshot": "已整理的主題快照", "next_actions": "- 待辦事項"},
        "checklist_complete": False,
        "remaining_gaps": ["尚有未補齊的缺口"],
        "progress_note": "一般全面補充",
    },
}

# Update variants. The default refreshes next actions + open questions and
# records a note (the assist-update workflow has no checklist/gaps).
_UPDATE_VARIANTS: dict[str, dict[str, Any]] = {
    "default": {
        "sections": {
            "next_actions": "- 與第三方支付商確認串接文件\n- 安排測試環境",
            "open_questions": "- 是否需要沙盒帳號?",
        },
        "progress_note": "更新後續行動與待答問題",
    },
}

# A deliberately non-JSON reply. Used for --mode garbage and for any unknown
# system prompt: the backend's strict single-JSON-object parser rejects it,
# retries once, and then maps it to a 502 llm_upstream_error.
_GARBAGE_PROSE = (
    "抱歉,我目前無法將這段內容整理成結構化的 JSON。"
    "以下只是一段純文字說明,並不是有效的 JSON 物件,也沒有任何欄位。"
)

# --- hold-mode barrier -------------------------------------------------------

# --mode hold polls for the release file at this interval, capped at this
# many seconds total so a caller that forgets to release (e.g. a smoke bug)
# can't hang the mock -- and thus the whole harness -- forever.
_HOLD_POLL_SECONDS = 0.1
_HOLD_TIMEOUT_SECONDS = 60.0


def _variant(user_text: str) -> str:
    match = _VARIANT_RE.search(user_text)
    return match.group(1) if match else "default"


def _select(system_text: str, user_text: str) -> tuple[str, str, str]:
    """Return (workflow_label, variant_label, message_content) for a good reply.

    ``message_content`` is a JSON string for a matched workflow, or the garbage
    prose for an unknown system prompt.
    """
    if _CAPTURE_MARKER in system_text:
        return "capture", "-", json.dumps(_capture_draft(), ensure_ascii=False)
    if _ENRICH_MARKER in system_text:
        variant = _variant(user_text)
        payload = _ENRICH_VARIANTS.get(variant, _ENRICH_VARIANTS["default"])
        return "enrich", variant, json.dumps(payload, ensure_ascii=False)
    if _UPDATE_MARKER in system_text:
        variant = _variant(user_text)
        payload = _UPDATE_VARIANTS.get(variant, _UPDATE_VARIANTS["default"])
        return "update", variant, json.dumps(payload, ensure_ascii=False)
    return "unknown", "-", _GARBAGE_PROSE


def _completion(content: str, model: str) -> dict[str, Any]:
    """Wrap ``content`` in a minimal, well-formed chat.completion envelope."""
    return {
        "id": "chatcmpl-mock-0001",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "mock",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# --- tool_roundtrip capability (D14) ----------------------------------------

# The tool the tool_roundtrip capability asks the backend to call. Any installed
# tool package named "echo" satisfies it; the mock only needs the NAME to match
# what such a package advertises. This is a CAPABILITY for manual / pytest-
# adjacent use -- smoke.sh does not drive it (and with the smoke's tools_dir
# unset the backend advertises no tools, so a tool_calls reply would go unused).
_TOOL_ROUNDTRIP_TOOL = "echo"


def _has_tool_result(messages: list[dict[str, Any]]) -> bool:
    """True once a tool result rides in the conversation.

    The backend appends a ``role:"tool"`` message for every executed tool call
    before it calls us again, so this tells the FIRST tool_roundtrip request
    (none yet -> reply with tool_calls) from the SECOND (present -> reply with
    the workflow's normal good JSON).
    """
    return any(isinstance(m, dict) and m.get("role") == "tool" for m in messages)


def _tool_calls_completion(model: str) -> dict[str, Any]:
    """A chat.completion asking to call the ``echo`` tool (content null).

    The exact shape a real endpoint returns for a tool-call turn -- content
    None, one function tool_call, ``finish_reason="tool_calls"`` -- which the
    backend's tool loop echoes back before executing the tool. The arguments are
    a small JSON object so the installed echo tool has something to return.
    """
    return {
        "id": "chatcmpl-mock-tool-0001",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "mock",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_mock_0001",
                            "type": "function",
                            "function": {
                                "name": _TOOL_ROUNDTRIP_TOOL,
                                "arguments": json.dumps({"text": "ping"}, ensure_ascii=False),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class _Handler(BaseHTTPRequestHandler):
    # Set on the class from CLI args before the server starts serving.
    mode: str = "good"
    slow_seconds: float = 10.0
    release_file: str | None = None

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress BaseHTTPRequestHandler's default access log; we emit our own
        # concise line per request in _handle so the workflow/variant is visible.
        return

    def _log(self, line: str) -> None:
        sys.stderr.write(f"[mock_llm] {line}\n")
        sys.stderr.flush()

    def _await_release(self) -> None:
        """Block until ``self.release_file`` exists, or the hard cap elapses.

        Polls every _HOLD_POLL_SECONDS. The timeout is a safety valve, not
        part of the intended flow: a caller driving --mode hold is expected
        to always touch the release file once whatever must happen first
        (e.g. a concurrent PATCH) has happened.
        """
        if not self.release_file:
            self._log("mode=hold but no --release-file configured; not holding")
            return
        deadline = time.monotonic() + _HOLD_TIMEOUT_SECONDS
        while not os.path.exists(self.release_file):
            if time.monotonic() >= deadline:
                self._log(
                    f"hold timed out after {_HOLD_TIMEOUT_SECONDS}s waiting for "
                    f"release file {self.release_file}"
                )
                return
            time.sleep(_HOLD_POLL_SECONDS)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            # In slow mode the backend may have already timed out and closed the
            # connection; writing the (late) reply then fails harmlessly.
            self._log("write skipped: client closed connection")

    def do_GET(self) -> None:
        # A trivial liveness endpoint so the smoke can poll for readiness.
        self._log(f"GET {self.path}")
        self._send_json(200, {"status": "ok"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        if not self.path.endswith("/chat/completions"):
            self._log(f"POST {self.path} -> 404 (unrecognized path)")
            self._send_json(404, {"error": {"message": "not found"}})
            return

        try:
            request = json.loads(raw.decode("utf-8"))
        # UnicodeDecodeError is a subclass of ValueError, so this one clause
        # covers both a bad UTF-8 body and malformed JSON.
        except ValueError:
            self._log(f"POST {self.path} -> 400 (unparseable request body)")
            self._send_json(400, {"error": {"message": "invalid JSON body"}})
            return

        messages = request.get("messages") or []
        model = request.get("model") or "mock"
        system_text = "\n".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "system"
        )
        user_text = "\n".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "user"
        )

        if self.mode == "slow":
            self._log(f"POST {self.path} mode=slow sleeping {self.slow_seconds}s")
            time.sleep(self.slow_seconds)

        if self.mode == "hold":
            self._log(f"POST {self.path} mode=hold holding response")
            self._await_release()

        if self.mode == "garbage":
            workflow, variant, content, kind = "*", "-", _GARBAGE_PROSE, "garbage"
        else:
            # tool_roundtrip capability (D14): a #variant:tool_roundtrip directive
            # in the user text drives ONE agentic round -- the first reply (no
            # tool result in the messages yet) is a tool_calls completion, and
            # the second (once the backend has fed the tool result back) is the
            # workflow's normal good JSON. Only meaningful when the backend has an
            # "echo" tool installed and thus advertises tools; inert otherwise.
            if _variant(user_text) == "tool_roundtrip" and not _has_tool_result(messages):
                self._log(
                    f"POST {self.path} mode={self.mode} variant=tool_roundtrip reply=tool_calls"
                )
                self._send_json(200, _tool_calls_completion(model))
                return
            workflow, variant, content = _select(system_text, user_text)
            kind = "garbage" if workflow == "unknown" else "json"

        self._log(
            f"POST {self.path} mode={self.mode} workflow={workflow} "
            f"variant={variant} reply={kind} bytes={len(content)}"
        )
        self._send_json(200, _completion(content, model))


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAI-compatible mock LLM for the e2e smoke.")
    parser.add_argument(
        "--port",
        type=int,
        default=8900,
        help="TCP port to listen on; 0 asks the OS for a free ephemeral port (see docstring).",
    )
    parser.add_argument(
        "--mode",
        choices=("good", "garbage", "slow", "hold"),
        default="good",
        help=(
            "good: valid JSON; garbage: prose junk (502); slow: sleep past a "
            "short timeout; hold: block until --release-file appears, then "
            "reply with valid JSON (for barrier-based conflict tests)."
        ),
    )
    parser.add_argument(
        "--slow-seconds",
        type=float,
        default=10.0,
        help="Seconds to sleep before replying in --mode slow (default 10).",
    )
    parser.add_argument(
        "--release-file",
        default=None,
        help="Path whose existence releases a --mode hold response (required with --mode hold).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
    args = parser.parse_args()
    if args.mode == "hold" and not args.release_file:
        parser.error("--mode hold requires --release-file")

    _Handler.mode = args.mode
    _Handler.slow_seconds = args.slow_seconds
    _Handler.release_file = args.release_file

    # Bind first -- port 0 asks the OS for a free ephemeral port, so there is
    # no free()-then-bind TOCTOU window between picking a port and this
    # process owning it -- then read back whatever the OS actually assigned
    # via the now-bound socket.
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    actual_port = server.server_address[1]
    sys.stderr.write(
        f"[mock_llm] mock-llm listening on port {actual_port} "
        f"(http://{args.host}:{actual_port} mode={args.mode} "
        f"slow_seconds={args.slow_seconds})\n"
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
