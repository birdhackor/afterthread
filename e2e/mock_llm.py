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
``app/services/memory_ai.py``. Each prompt carries a distinctive marker:

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

Modes (``--mode``):
  * good    -> valid JSON for the matched workflow (the normal happy path).
  * garbage -> always a prose junk reply (no JSON object), for the 502 path.
  * slow    -> sleeps ``--slow-seconds`` before replying with otherwise-good
               JSON, so a backend with a tiny OPENAI_TIMEOUT_SECONDS times out
               first (exercising the 502 ``Timeout`` path).

Every request is logged as one line to stderr.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# --- workflow selection ----------------------------------------------------

# Distinctive substrings from the three system prompts in
# app/services/memory_ai.py (CAPTURE_/ENRICH_/UPDATE_SYSTEM_PROMPT). Matching on
# these -- rather than the whole prompt -- keeps the mock robust to the schema
# block and strict-output rule that app/services/llm.py appends after them.
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


class _Handler(BaseHTTPRequestHandler):
    # Set on the class from CLI args before the server starts serving.
    mode: str = "good"
    slow_seconds: float = 10.0

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress BaseHTTPRequestHandler's default access log; we emit our own
        # concise line per request in _handle so the workflow/variant is visible.
        return

    def _log(self, line: str) -> None:
        sys.stderr.write(f"[mock_llm] {line}\n")
        sys.stderr.flush()

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

        if self.mode == "garbage":
            workflow, variant, content, kind = "*", "-", _GARBAGE_PROSE, "garbage"
        else:
            workflow, variant, content = _select(system_text, user_text)
            kind = "garbage" if workflow == "unknown" else "json"

        self._log(
            f"POST {self.path} mode={self.mode} workflow={workflow} "
            f"variant={variant} reply={kind} bytes={len(content)}"
        )
        self._send_json(200, _completion(content, model))


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAI-compatible mock LLM for the e2e smoke.")
    parser.add_argument("--port", type=int, default=8900, help="TCP port to listen on.")
    parser.add_argument(
        "--mode",
        choices=("good", "garbage", "slow"),
        default="good",
        help="good: valid JSON; garbage: prose junk (502); slow: sleep past a short timeout.",
    )
    parser.add_argument(
        "--slow-seconds",
        type=float,
        default=10.0,
        help="Seconds to sleep before replying in --mode slow (default 10).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
    args = parser.parse_args()

    _Handler.mode = args.mode
    _Handler.slow_seconds = args.slow_seconds

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    sys.stderr.write(
        f"[mock_llm] listening on http://{args.host}:{args.port} "
        f"mode={args.mode} slow_seconds={args.slow_seconds}\n"
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
