#!/usr/bin/env bash
#
# End-to-end smoke harness for the Context Memory web stack.
#
# Run from the repo root:   ./e2e/smoke.sh
# Prerequisites:            uv (backend), pnpm (frontend), python3 (mock + JSON
#                           assertions -- the mock server is stdlib-only python3).
#
# It boots the real FastAPI backend (via `uv run uvicorn`) against throwaway
# SQLite files and drives the real HTTP API, plus a stdlib-only OpenAI-compatible
# mock (e2e/mock_llm.py) standing in for the LLM endpoint. Four phases:
#
#   A  unconfigured backend  -> health / llm-status / capture-503 / full CRUD
#                               lifecycle / input-bound rejects
#   B  backend + good mock   -> real capture / enrich (merge, gaps, lifecycle,
#                               supersede) / assist-update
#   C  backend + degraded    -> garbage mock (502, no row) and slow mock (502
#                               Timeout within the configured deadline)
#   D  frontend build        -> `pnpm build` + `pnpm preview` serve the SPA shell
#
# Every assertion prints PASS/FAIL; the script exits non-zero if any FAIL.
# Servers and the temp dir are always cleaned up via an EXIT trap.
#
# PREVIEW / PROXY LIMITATION (documented per the phase-5 brief):
#   `pnpm preview` serves the *production* build. In this repo's vite 8 setup the
#   preview server INHERITS `server.proxy` from vite.config.js, so it forwards
#   `/api/*` to a HARD-CODED http://localhost:8000 -- NOT to the dynamically
#   ported backend this harness boots. (The brief assumed preview does not proxy
#   at all; vite 8 actually does, but to a fixed port we don't use.) Either way,
#   preview cannot reach our backend, so the full-stack smoke drives the BACKEND
#   API directly (phases A-C) and phase D only verifies that the built SPA shell
#   is served for `/` and a client route (`/items`). No API call is proxied
#   through preview.

set -euo pipefail

# UTF-8 everywhere so the python JSON/assertion helpers handle Traditional
# Chinese and emoji from argv and heredocs regardless of the ambient locale.
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# --- locations -------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
E2E_DIR="$SCRIPT_DIR"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"
MOCK="$E2E_DIR/mock_llm.py"

TMPDIR_E2E="$(mktemp -d "${TMPDIR:-/tmp}/cm-e2e.XXXXXX")"

# --- server bookkeeping (for teardown) -------------------------------------

# Parallel arrays of session-leader PIDs and their ports for every server this
# harness starts, so stop_servers can kill each process group and free its port.
declare -a SIDS=()
declare -a PORTS=()

# --- assertion counters ----------------------------------------------------

PASS_COUNT=0
FAIL_COUNT=0
CURRENT_PHASE="init"
declare -A PHASE_PASS=()
declare -A PHASE_FAIL=()

pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    PHASE_PASS[$CURRENT_PHASE]=$(( ${PHASE_PASS[$CURRENT_PHASE]:-0} + 1 ))
    printf '  PASS: %s\n' "$1"
}

fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    PHASE_FAIL[$CURRENT_PHASE]=$(( ${PHASE_FAIL[$CURRENT_PHASE]:-0} + 1 ))
    printf '  FAIL: %s\n' "$1"
}

# assert_eq LABEL ACTUAL EXPECTED
assert_eq() {
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (got '$2', want '$3')"; fi
}

# assert_true LABEL JSON_FILE PY_EXPR
# Evaluates PY_EXPR against the parsed JSON bound to `d` (plus json/len/str/any/
# all/sorted). Any error or falsy result is a FAIL, never an abort.
assert_true() {
    local out
    out="$(python3 - "$2" "$3" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:  # unparseable / missing body
    print(f"ERR:{exc}")
    raise SystemExit(0)
ns = {"d": d, "json": json, "len": len, "str": str, "any": any, "all": all,
      "sorted": sorted, "isinstance": isinstance, "int": int, "bool": bool}
try:
    print("True" if bool(eval(sys.argv[2], {"__builtins__": {}}, ns)) else "False")
except Exception as exc:
    print(f"ERR:{exc}")
PY
)"
    if [ "$out" = "True" ]; then pass "$1"; else fail "$1 [$out]"; fi
}

# assert_file_contains LABEL FILE FIXED_STRING
assert_file_contains() {
    if grep -qF -- "$3" "$2" 2>/dev/null; then pass "$1"; else fail "$1 (missing '$3')"; fi
}

# jget FILE PY_EXPR -> prints str(value); empty string on any error.
jget() {
    python3 - "$1" "$2" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    ns = {"d": d, "json": json, "len": len, "str": str}
    print(eval(sys.argv[2], {"__builtins__": {}}, ns))
except Exception:
    print("")
PY
}

phase_banner() {
    CURRENT_PHASE="$1"
    printf '\n========== PHASE %s ==========\n' "$1"
}

# --- HTTP helpers ----------------------------------------------------------

# req METHOD URL OUTFILE [INLINE_JSON] -> prints HTTP status code.
req() {
    local method=$1 url=$2 out=$3 data=${4:-}
    if [ -n "$data" ]; then
        curl -s --max-time 30 -o "$out" -w '%{http_code}' \
            -X "$method" -H 'Content-Type: application/json' -d "$data" "$url"
    else
        curl -s --max-time 30 -o "$out" -w '%{http_code}' -X "$method" "$url"
    fi
}

# reqf METHOD URL OUTFILE JSON_FILE -> prints HTTP status code (unicode-safe body).
reqf() {
    curl -s --max-time 30 -o "$3" -w '%{http_code}' \
        -X "$1" -H 'Content-Type: application/json' --data-binary @"$4" "$2"
}

# --- process management ----------------------------------------------------

free_port() {
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

# wait_200 URL [TRIES] -> 0 once URL answers 200, else 1 after TRIES*0.25s.
wait_200() {
    local url=$1 tries=${2:-160} i
    for ((i = 1; i <= tries; i++)); do
        if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null)" = "200" ]; then
            return 0
        fi
        sleep 0.25
    done
    return 1
}

# start_mock PORT MODE SLOW_SECONDS LOGFILE
start_mock() {
    setsid python3 "$MOCK" --port "$1" --mode "$2" --slow-seconds "$3" >"$4" 2>&1 &
    local sid=$!
    SIDS+=("$sid")
    PORTS+=("$1")
    if ! wait_200 "http://127.0.0.1:$1/health" 80; then
        echo "  (mock failed to start; log:)"; sed 's/^/    /' "$4" || true
        return 1
    fi
}

# start_backend PORT DB_FILE LOGFILE [ENV=VAL ...]
# Always scrubs ambient OPENAI_* so "unconfigured" is hermetic; extra ENV=VAL
# args (e.g. the OPENAI_* triple) are layered on for the configured phases.
start_backend() {
    local port=$1 db=$2 logf=$3
    shift 3
    (
        cd "$BACKEND_DIR" &&
            setsid env -u OPENAI_BASE_URL -u OPENAI_API_KEY -u OPENAI_MODEL -u OPENAI_TIMEOUT_SECONDS \
                DATABASE_URL="sqlite:///$db" "$@" \
                uv run uvicorn app.main:app --host 127.0.0.1 --port "$port" >"$logf" 2>&1 &
        echo $! >"$TMPDIR_E2E/last.sid"
    )
    local sid
    sid="$(cat "$TMPDIR_E2E/last.sid")"
    SIDS+=("$sid")
    PORTS+=("$port")
    if ! wait_200 "http://127.0.0.1:$port/api/health" 200; then
        echo "  (backend failed to start; log tail:)"; tail -n 15 "$logf" | sed 's/^/    /' || true
        return 1
    fi
}

# start_preview PORT LOGFILE
start_preview() {
    (
        cd "$FRONTEND_DIR" &&
            setsid pnpm preview --host 127.0.0.1 --port "$1" --strictPort >"$2" 2>&1 &
        echo $! >"$TMPDIR_E2E/last.sid"
    )
    local sid
    sid="$(cat "$TMPDIR_E2E/last.sid")"
    SIDS+=("$sid")
    PORTS+=("$1")
    if ! wait_200 "http://127.0.0.1:$1/" 120; then
        echo "  (preview failed to start; log tail:)"; tail -n 15 "$2" | sed 's/^/    /' || true
        return 1
    fi
}

# Kill every tracked server (process group + a port sweep) and reset the arrays.
stop_servers() {
    local sid p
    for sid in "${SIDS[@]:-}"; do
        [ -n "$sid" ] && kill -TERM -"$sid" 2>/dev/null || true
    done
    sleep 0.3 || true
    for sid in "${SIDS[@]:-}"; do
        [ -n "$sid" ] && kill -KILL -"$sid" 2>/dev/null || true
    done
    for p in "${PORTS[@]:-}"; do
        lsof -ti tcp:"$p" 2>/dev/null | xargs -r kill -KILL 2>/dev/null || true
    done
    SIDS=()
    PORTS=()
}

teardown() {
    local ec=$?
    stop_servers
    [ -n "${TMPDIR_E2E:-}" ] && rm -rf "$TMPDIR_E2E" 2>/dev/null || true
    exit "$ec"
}
trap teardown EXIT

# ===========================================================================
# PHASE A -- unconfigured backend: probes, CRUD lifecycle, input bounds
# ===========================================================================
phase_a() {
    phase_banner "A (unconfigured backend: probes + CRUD)"
    local port db log body
    port="$(free_port)"
    db="$TMPDIR_E2E/a.sqlite"
    log="$TMPDIR_E2E/a-backend.log"
    body="$TMPDIR_E2E/a-body.json"
    start_backend "$port" "$db" "$log"
    local base="http://127.0.0.1:$port/api"

    # -- liveness + llm status ------------------------------------------------
    assert_eq "health returns 200" "$(req GET "$base/health" "$body")" "200"
    assert_true "health body is {status: ok}" "$body" 'd["status"] == "ok"'

    req GET "$base/llm/status" "$body" >/dev/null
    assert_true "llm/status configured=false, model=null" "$body" \
        'd["configured"] is False and d["model"] is None'

    # -- capture is 503 when unconfigured ------------------------------------
    assert_eq "capture 503 when unconfigured" \
        "$(req POST "$base/capture" "$body" '{"raw_text":"未設定 LLM 時的原始討論內容"}')" "503"
    assert_true "capture 503 code llm_not_configured" "$body" \
        'd["detail"]["code"] == "llm_not_configured"'

    # -- create -> list total ------------------------------------------------
    local create="$TMPDIR_E2E/a-create.json"
    assert_eq "create item 201" \
        "$(req POST "$base/items" "$create" '{"title":"測試項目","snapshot":"初始快照"}')" "201"
    local id
    id="$(jget "$create" 'd["id"]')"
    assert_true "created item has integer id" "$create" 'isinstance(d["id"], int) and d["id"] >= 1'

    req GET "$base/items" "$body" >/dev/null
    assert_true "list total == 1 after first create" "$body" 'd["total"] == 1'

    # -- get with seeded progress -------------------------------------------
    req GET "$base/items/$id" "$body" >/dev/null
    assert_true "get item seeds one progress entry '建立項目'" "$body" \
        '[e["note"] for e in d["progress"]] == ["建立項目"]'

    # -- patch status (and updated bumped) ----------------------------------
    local patched="$TMPDIR_E2E/a-patch.json"
    assert_eq "patch status -> active 200" \
        "$(req PATCH "$base/items/$id" "$patched" '{"status":"active"}')" "200"
    assert_true "patched status is active" "$patched" 'd["status"] == "active"'
    local before after bumped
    before="$(jget "$create" 'd["updated"]')"
    after="$(jget "$patched" 'd["updated"]')"
    bumped="$(python3 -c "from datetime import datetime as t; print('yes' if t.fromisoformat('$after') > t.fromisoformat('$before') else 'no')")"
    assert_eq "patch bumps updated timestamp" "$bumped" "yes"

    # -- progress append -----------------------------------------------------
    assert_eq "append progress 201" \
        "$(req POST "$base/items/$id/progress" "$body" '{"note":"手動新增進度"}')" "201"
    req GET "$base/items/$id" "$body" >/dev/null
    assert_true "progress log now has seed + appended note" "$body" \
        '[e["note"] for e in d["progress"]] == ["建立項目", "手動新增進度"]'

    # -- review grouping -----------------------------------------------------
    req GET "$base/review" "$body" >/dev/null
    assert_true "review: active item in 'active' bucket only" "$body" \
        "$id in [i[\"id\"] for i in d[\"active\"]] and $id not in [i[\"id\"] for i in d[\"needs_enrichment\"]]"

    # -- unicode / emoji title round-trip ------------------------------------
    local upayload="$TMPDIR_E2E/a-unicode.json"
    python3 - "$upayload" <<'PY'
import json, sys
json.dump({"title": "重點 🚀 記憶 測試 ✅ café", "snapshot": "多語系內容 🎉"},
          open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False)
PY
    local ucreate="$TMPDIR_E2E/a-unicode-create.json"
    assert_eq "create unicode/emoji item 201" "$(reqf POST "$base/items" "$ucreate" "$upayload")" "201"
    local uid
    uid="$(jget "$ucreate" 'd["id"]')"
    local uget="$TMPDIR_E2E/a-unicode-get.json"
    req GET "$base/items/$uid" "$uget" >/dev/null
    local rt
    rt="$(python3 - "$upayload" "$uget" <<'PY'
import json, sys
sent = json.load(open(sys.argv[1], encoding="utf-8"))["title"]
got = json.load(open(sys.argv[2], encoding="utf-8"))["title"]
print("yes" if (sent == got and "🚀" in got and "café" in got) else "no")
PY
)"
    assert_eq "unicode/emoji title round-trips byte-for-byte" "$rt" "yes"

    # -- delete -> 404 -------------------------------------------------------
    assert_eq "delete item 204" "$(req DELETE "$base/items/$id" "$body")" "204"
    assert_eq "get deleted item 404" "$(req GET "$base/items/$id" "$body")" "404"

    # -- input-bound rejects (422) ------------------------------------------
    local bigtitle="$TMPDIR_E2E/a-bigtitle.json" bigtags="$TMPDIR_E2E/a-bigtags.json"
    python3 - "$bigtitle" <<'PY'
import json, sys
json.dump({"title": "x" * 301}, open(sys.argv[1], "w", encoding="utf-8"))
PY
    python3 - "$bigtags" <<'PY'
import json, sys
json.dump({"title": "ok", "tags": [f"t{i}" for i in range(21)]},
          open(sys.argv[1], "w", encoding="utf-8"))
PY
    assert_eq "reject title > 300 chars (422)" "$(reqf POST "$base/items" "$body" "$bigtitle")" "422"
    assert_eq "reject > 20 tags (422)" "$(reqf POST "$base/items" "$body" "$bigtags")" "422"

    stop_servers
}

# ===========================================================================
# PHASE B -- backend + good mock: capture, enrich, supersede, assist-update
# ===========================================================================
phase_b() {
    phase_banner "B (good mock: capture / enrich / supersede / assist-update)"
    local mport bport db mlog blog body
    mport="$(free_port)"
    bport="$(free_port)"
    db="$TMPDIR_E2E/b.sqlite"
    mlog="$TMPDIR_E2E/b-mock.log"
    blog="$TMPDIR_E2E/b-backend.log"
    body="$TMPDIR_E2E/b-body.json"
    start_mock "$mport" "good" "10" "$mlog"
    start_backend "$bport" "$db" "$blog" \
        OPENAI_BASE_URL="http://127.0.0.1:$mport/v1" OPENAI_API_KEY="test" \
        OPENAI_MODEL="mock" OPENAI_TIMEOUT_SECONDS="30"
    local base="http://127.0.0.1:$bport/api"

    req GET "$base/llm/status" "$body" >/dev/null
    assert_true "llm/status configured=true, model=mock" "$body" \
        'd["configured"] is True and d["model"] == "mock"'

    # -- real capture --------------------------------------------------------
    local cap="$TMPDIR_E2E/b-capture.json"
    assert_eq "capture 201" \
        "$(req POST "$base/capture" "$cap" '{"raw_text":"我們今天討論了付款流程重構的方向、決策與待辦事項。"}')" "201"
    assert_true "captured item uses LLM-provided title" "$cap" \
        'd["item"]["title"] == "重構付款流程以支援新金流商"'
    assert_true "captured item source is llm-capture" "$cap" 'd["item"]["source"] == "llm-capture"'
    assert_true "captured status from suggestion (needs-enrichment)" "$cap" \
        'd["item"]["status"] == "needs-enrichment"'
    assert_true "questions returned in response (2)" "$cap" 'len(d["questions"]) == 2'
    assert_true "questions persisted into open_questions as '- ' bullets" "$cap" \
        'd["item"]["open_questions"] == "\n".join("- " + q for q in d["questions"])'
    local id
    id="$(jget "$cap" 'd["item"]["id"]')"

    # Durable: re-fetch shows the same open_questions bullets.
    req GET "$base/items/$id" "$body" >/dev/null
    assert_true "open_questions durable after re-fetch" "$body" \
        'd["open_questions"].startswith("- ") and "\n- " in d["open_questions"]'

    # -- real enrich #1: seed decision, incomplete (merge + gaps, no promote) -
    local e1="$TMPDIR_E2E/b-enrich1.json"
    assert_eq "enrich #1 (seed) 200" \
        "$(req POST "$base/items/$id/enrich" "$e1" '{"additional_context":"金流商已回覆報價,需要記錄初步決策。 #variant:seed"}')" "200"
    assert_true "enrich #1 sets decisions section" "$e1" \
        'd["item"]["decisions"] == "採用方案A:直接串接金流商 API"'
    assert_true "enrich #1 returns 2 remaining gaps" "$e1" 'len(d["gaps"]) == 2'
    assert_true "enrich #1 incomplete: stage stays quick" "$e1" 'd["item"]["stage"] == "quick"'
    assert_true "enrich #1 incomplete: status stays needs-enrichment" "$e1" \
        'd["item"]["status"] == "needs-enrichment"'
    # Untouched capture field survives the merge.
    local capsnap e1snap
    capsnap="$(jget "$cap" 'd["item"]["snapshot"]')"
    e1snap="$(jget "$e1" 'd["item"]["snapshot"]')"
    assert_eq "enrich #1 merges (leaves untouched snapshot intact)" "$e1snap" "$capsnap"

    # -- real enrich #2: rewrite decision + complete (supersede + promote) ----
    local e2="$TMPDIR_E2E/b-enrich2.json"
    assert_eq "enrich #2 (complete) 200" \
        "$(req POST "$base/items/$id/enrich" "$e2" '{"additional_context":"團隊決定改變方向,改用第三方支付聚合。 #variant:complete"}')" "200"
    assert_true "enrich #2 no remaining gaps" "$e2" 'd["gaps"] == []'
    assert_true "enrich #2 complete: stage flips to full" "$e2" 'd["item"]["stage"] == "full"'
    assert_true "enrich #2 complete: promotes to active" "$e2" 'd["item"]["status"] == "active"'
    assert_true "supersede: new decision present" "$e2" '"改採方案B" in d["item"]["decisions"]'
    assert_true "supersede: prior decision retained" "$e2" '"採用方案A" in d["item"]["decisions"]'
    assert_true "supersede: marked with 'superseded'" "$e2" '"superseded" in d["item"]["decisions"]'

    # -- real assist-update: progress appended + updated bumped --------------
    local before_u
    before_u="$(jget "$e2" 'd["item"]["updated"]')"
    local up="$TMPDIR_E2E/b-assist.json"
    assert_eq "assist-update 200" \
        "$(req POST "$base/items/$id/assist-update" "$up" '{"note":"與第三方支付商確認串接文件進度。"}')" "200"
    local after_u ubumped
    after_u="$(jget "$up" 'd["item"]["updated"]')"
    ubumped="$(python3 -c "from datetime import datetime as t; print('yes' if t.fromisoformat('$after_u') > t.fromisoformat('$before_u') else 'no')")"
    assert_eq "assist-update bumps updated timestamp" "$ubumped" "yes"
    req GET "$base/items/$id" "$body" >/dev/null
    assert_true "assist-update appends its progress note" "$body" \
        'd["progress"][-1]["note"] == "更新後續行動與待答問題"'
    assert_true "full progress trail recorded in order" "$body" \
        '[e["note"] for e in d["progress"]] == ["AI 快速捕捉", "補充初步決策與背景資訊", "checklist 完成,決策改為方案B", "更新後續行動與待答問題"]'

    stop_servers
}

# ===========================================================================
# PHASE C -- degradation: garbage output (502, no row) and slow (502 Timeout)
# ===========================================================================
phase_c() {
    phase_banner "C (degradation: garbage 502 + slow Timeout 502)"

    # -- garbage: prose junk -> retry -> 502, nothing written ----------------
    local mport bport db mlog blog body
    mport="$(free_port)"
    bport="$(free_port)"
    db="$TMPDIR_E2E/c-garbage.sqlite"
    mlog="$TMPDIR_E2E/c-garbage-mock.log"
    blog="$TMPDIR_E2E/c-garbage-backend.log"
    body="$TMPDIR_E2E/c-garbage-body.json"
    start_mock "$mport" "garbage" "10" "$mlog"
    start_backend "$bport" "$db" "$blog" \
        OPENAI_BASE_URL="http://127.0.0.1:$mport/v1" OPENAI_API_KEY="test" \
        OPENAI_MODEL="mock" OPENAI_TIMEOUT_SECONDS="30"
    local base="http://127.0.0.1:$bport/api"

    assert_eq "garbage capture -> 502" \
        "$(req POST "$base/capture" "$body" '{"raw_text":"降級測試:期望回傳 502 的原始討論。"}')" "502"
    assert_true "garbage capture code llm_upstream_error" "$body" \
        'd["detail"]["code"] == "llm_upstream_error"'
    req GET "$base/items" "$body" >/dev/null
    assert_true "garbage capture wrote no row (total == 0)" "$body" 'd["total"] == 0'
    stop_servers

    # -- slow: sleeps past a tiny timeout -> 502 Timeout within the deadline --
    mport="$(free_port)"
    bport="$(free_port)"
    db="$TMPDIR_E2E/c-slow.sqlite"
    mlog="$TMPDIR_E2E/c-slow-mock.log"
    blog="$TMPDIR_E2E/c-slow-backend.log"
    body="$TMPDIR_E2E/c-slow-body.json"
    start_mock "$mport" "slow" "10" "$mlog"
    # Tiny 2s deadline; the mock sleeps 10s, so the wall-clock bound must fire.
    start_backend "$bport" "$db" "$blog" \
        OPENAI_BASE_URL="http://127.0.0.1:$mport/v1" OPENAI_API_KEY="test" \
        OPENAI_MODEL="mock" OPENAI_TIMEOUT_SECONDS="2"
    base="http://127.0.0.1:$bport/api"

    local start elapsed code
    start="$(date +%s.%N)"
    code="$(req POST "$base/capture" "$body" '{"raw_text":"降級測試:緩慢端點應於逾時內回傳 502。"}')"
    elapsed="$(python3 -c "print(f'{$(date +%s.%N) - $start:.2f}')")"
    assert_eq "slow capture -> 502" "$code" "502"
    assert_true "slow capture code llm_upstream_error" "$body" 'd["detail"]["code"] == "llm_upstream_error"'
    assert_true "slow capture message is a Timeout" "$body" 'd["detail"]["message"].startswith("Timeout")'
    local within
    within="$(python3 -c "print('yes' if $elapsed < 6.0 else 'no')")"
    assert_eq "slow capture bounded (<6s, not the 10s sleep); took ${elapsed}s" "$within" "yes"
    req GET "$base/items" "$body" >/dev/null
    assert_true "slow capture wrote no row (total == 0)" "$body" 'd["total"] == 0'
    stop_servers
}

# ===========================================================================
# PHASE D -- frontend production build serves the SPA shell
# ===========================================================================
phase_d() {
    phase_banner "D (frontend build + preview serves SPA shell)"
    local buildlog="$TMPDIR_E2E/d-build.log"
    if (cd "$FRONTEND_DIR" && pnpm build >"$buildlog" 2>&1); then
        pass "pnpm build succeeds"
    else
        fail "pnpm build failed"
        tail -n 15 "$buildlog" | sed 's/^/    /' || true
        return 0
    fi

    local pport plog root items
    pport="$(free_port)"
    plog="$TMPDIR_E2E/d-preview.log"
    root="$TMPDIR_E2E/d-root.html"
    items="$TMPDIR_E2E/d-items.html"
    start_preview "$pport" "$plog"
    local base="http://127.0.0.1:$pport"

    assert_eq "GET / -> 200" "$(req GET "$base/" "$root")" "200"
    assert_file_contains "GET / serves HTML doctype" "$root" "<!doctype html"
    assert_file_contains "GET / serves SPA mount node" "$root" 'id="root"'

    # Client-side route: served via the SPA history fallback (index.html shell).
    assert_eq "GET /items -> 200 (SPA fallback)" "$(req GET "$base/items" "$items")" "200"
    assert_file_contains "GET /items serves SPA shell" "$items" 'id="root"'

    stop_servers
}

# ===========================================================================
# main
# ===========================================================================
main() {
    echo "Context Memory e2e smoke -- repo: $REPO_ROOT"
    echo "Temp dir: $TMPDIR_E2E"

    phase_a
    phase_b
    phase_c
    phase_d

    echo ""
    echo "========== SUMMARY =========="
    local ph
    for ph in "A (unconfigured backend: probes + CRUD)" \
        "B (good mock: capture / enrich / supersede / assist-update)" \
        "C (degradation: garbage 502 + slow Timeout 502)" \
        "D (frontend build + preview serves SPA shell)"; do
        printf '  PHASE %s: %d passed, %d failed\n' \
            "$ph" "${PHASE_PASS[$ph]:-0}" "${PHASE_FAIL[$ph]:-0}"
    done
    printf '  TOTAL: %d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"

    if [ "$FAIL_COUNT" -gt 0 ]; then
        echo "RESULT: FAIL"
        exit 1
    fi
    echo "RESULT: PASS"
    exit 0
}

main "$@"
