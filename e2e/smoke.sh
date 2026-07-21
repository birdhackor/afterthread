#!/usr/bin/env bash
#
# End-to-end smoke harness for the afterthread web stack.
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
#                               supersede) / assist-update / list filters (tag,
#                               q) and pagination (limit/offset)
#   C  backend + degraded    -> garbage mock (502, no row), slow mock (502
#                               Timeout within the configured deadline), and a
#                               PATCH racing an in-flight enrich (409 conflict,
#                               PATCH survives)
#   D  frontend build        -> `pnpm install --frozen-lockfile` + `pnpm build` +
#                               `pnpm preview` serve the SPA shell
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

TMPDIR_E2E="$(mktemp -d "${TMPDIR:-/tmp}/aft-e2e.XXXXXX")"

# --- server bookkeeping (for teardown) -------------------------------------

# Session-leader PIDs for every server this harness starts, so stop_servers
# can signal each one's process group. Teardown only ever touches PIDs this
# script itself launched and tracked here -- never anything discovered by
# port number, which could belong to an unrelated process that grabbed the
# port after ours exited (or one it never owned at all, e.g. a colliding CI
# job or a developer's local service). See stop_servers.
declare -a SIDS=()

# Set by start_mock/start_backend/start_preview to the port the just-started
# server actually bound (parsed from its own log line -- see port_from_log).
# A plain global "return value" handoff: each start_* call is immediately
# followed by the caller reading LAST_PORT, so there is no risk of a stale
# value leaking across servers.
LAST_PORT=""

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

# ts_bumped BEFORE AFTER -> "yes" if AFTER is a strictly later ISO-8601
# timestamp than BEFORE, "no" otherwise -- including when either argument is
# missing/malformed (e.g. a jget that already turned an unparseable body into
# ""). Values travel via argv, not string-interpolated into the python source,
# and the parse is wrapped in try/except so this can never raise: it always
# prints "yes" or "no" and exits 0, so callers can feed the result straight to
# assert_eq and get a labeled FAIL instead of aborting the phase under set -e.
# The API serializes UTC timestamps with a trailing "Z" (pydantic v2's
# default), which datetime.fromisoformat only accepts natively from Python
# 3.11 onward -- a bare `python3` on an older interpreter (<=3.10) raises
# ValueError on it, silently landing "no" for every call via the except
# clause below. Both values are normalized (Z -> +00:00) before parsing so
# this works on any python3, not just whatever happens to be first on PATH.
ts_bumped() {
    python3 - "$1" "$2" <<'PY'
import sys
from datetime import datetime
try:
    before = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
    after = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
    print("yes" if after > before else "no")
except Exception:
    print("no")
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

# urlenc STRING -> percent-encoded STRING (query-string safe), via Python's
# urllib.parse.quote, so non-ASCII query values (e.g. Chinese tag/q filters)
# are never sent as raw UTF-8 bytes in the request line.
urlenc() {
    python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

# --- process management ----------------------------------------------------

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

# wait_log_line FILE PATTERN [TRIES] -> 0 once FILE contains the fixed
# string PATTERN, else 1 after TRIES*0.1s (default 100 tries => ~10s). Lets
# callers wait for a server-side signal (e.g. a request the server logged)
# instead of guessing a fixed sleep.
wait_log_line() {
    local file=$1 pattern=$2 tries=${3:-100} i
    for ((i = 1; i <= tries; i++)); do
        if grep -qF -- "$pattern" "$file" 2>/dev/null; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

# port_from_log FILE PATTERN -> prints the first run of digits after PATTERN's
# first occurrence in FILE, else empty. ANSI CSI styling is removed first:
# color-aware tools may wrap the port itself (for example ESC[1m46613ESC[22m),
# whose style parameter would otherwise be mistaken for the bound port.
# Callers must wait_log_line for PATTERN first (bounded wait) -- this performs
# one synchronous read of an already-written line, never a poll of its own.
port_from_log() {
    awk -v pat="$2" '
        BEGIN { ansi = sprintf("%c\\[[0-9;?]*[ -/]*[@-~]", 27) }
        {
            line = $0
            gsub(ansi, "", line)
        }
        index(line, pat) {
            rest = substr(line, index(line, pat) + length(pat))
            if (match(rest, /[0-9]+/)) { print substr(rest, RSTART, RLENGTH); exit }
        }
    ' "$1" 2>/dev/null
}

# Distinctive substrings each server logs immediately before the port it
# actually bound -- mock_llm.py's own startup line, uvicorn's "Uvicorn
# running on ..." line, and vite preview's "Local: ..." line -- consumed by
# wait_log_line + port_from_log below instead of pre-picking a port with a
# free()-then-bind TOCTOU window (see start_mock/start_backend/start_preview).
MOCK_PORT_PATTERN="mock-llm listening on port "
BACKEND_PORT_PATTERN="Uvicorn running on http://127.0.0.1:"
PREVIEW_PORT_PATTERN="http://127.0.0.1:"

# start_mock MODE SLOW_SECONDS LOGFILE [EXTRA_ARG ...]
#
# Always binds to an OS-assigned ephemeral port (`--port 0`), so there is no
# free()-then-bind TOCTOU window: this function waits (bounded) for the mock
# to log the port it actually got (MOCK_PORT_PATTERN), parses it out, and
# leaves it in LAST_PORT for the caller -- see port_from_log above. Trailing
# args pass through verbatim (e.g. `--release-file FILE` for --mode hold).
#
# `setsid cmd &` backgrounds setsid directly -- no wrapping subshell or `&&`
# list -- so $! is exactly the PID bash forks to exec setsid. Job control is
# off in this non-interactive script, so that forked PID is never a
# process-group leader when setsid() runs, meaning the syscall succeeds in
# place (no internal setsid re-fork) and setsid's own exec into python3
# preserves the PID. $! is therefore the new session AND process-group
# leader: PID == PGID == SID, confirmed with `ps -o pid,pgid,sid`. See
# start_backend/start_preview for the same guarantee when a `cd` is also
# needed.
start_mock() {
    local mode=$1 slow=$2 logf=$3
    shift 3
    setsid python3 "$MOCK" --port 0 --mode "$mode" --slow-seconds "$slow" "$@" >"$logf" 2>&1 &
    local sid=$!
    SIDS+=("$sid")
    if ! wait_log_line "$logf" "$MOCK_PORT_PATTERN" 200; then
        echo "  (mock failed to start; log:)"; sed 's/^/    /' "$logf" || true
        return 1
    fi
    LAST_PORT="$(port_from_log "$logf" "$MOCK_PORT_PATTERN")"
    if [ -z "$LAST_PORT" ]; then
        echo "  (mock: could not parse bound port from log:)"; sed 's/^/    /' "$logf" || true
        return 1
    fi
    if ! wait_200 "http://127.0.0.1:$LAST_PORT/health" 80; then
        echo "  (mock failed readiness check; log:)"; sed 's/^/    /' "$logf" || true
        return 1
    fi
}

# start_backend DB_FILE LOGFILE [ENV=VAL ...]
# The three OPENAI_* fields llm_configured() gates on (base_url, api_key,
# model) default to EXPLICIT empty-string overrides here, not `env -u`
# unsets. pydantic-settings' precedence is env > dotenv, so an *unset* var
# still falls through to backend/.env if the checkout has one configured
# (e.g. a developer's real credentials) -- silently making an "unconfigured"
# phase read configured:true. An explicit `NAME=` assignment, by contrast, IS
# present in the child's environment (even though its value is empty), so it
# always wins over dotenv, closing that hole. Extra ENV=VAL args (e.g. the
# OPENAI_* triple the configured phases pass) are listed after these
# defaults in the `env` invocation, so `env`'s left-to-right assignment
# semantics let them override the empty defaults for phases B/C.
# OPENAI_TIMEOUT_SECONDS is still scrubbed via `-u` rather than defaulted to
# "": it is a gt=0-bounded float, so an empty override would fail
# pydantic-settings validation and crash the backend at startup instead of
# merely reading as unconfigured (and it plays no part in llm_configured()).
#
# Always launched with `--port 0`: uvicorn binds an OS-assigned ephemeral
# port (no free()-then-bind TOCTOU window) and, once ASGI startup completes,
# logs it in its own "Uvicorn running on http://127.0.0.1:<port>" line. This
# function waits for that line (bounded, BACKEND_PORT_PATTERN), parses the
# port out of it, and leaves it in LAST_PORT for the caller -- see
# port_from_log above.
#
# The outer subshell only exists to scope the `cd` without disturbing the
# caller's CWD; $! set inside it isn't visible outside, hence the last.sid
# handoff file. Backgrounding `cd dir && setsid ... &` verbatim (no `exec`)
# would make $! the PID of that wrapping "cd && setsid" job -- NOT the setsid
# session leader underneath it -- whenever bash forks rather than execs the
# trailing command in place, which this script cannot rely on. `exec` before
# setsid removes the ambiguity: it forces the backgrounded job to replace
# itself with setsid (same PID, no further fork), and setsid then execs into
# uv/uvicorn the same way, so the PID captured as $! is, by construction,
# the actual session/group leader (PID == PGID == SID, confirmed with
# `ps -o pid,pgid,sid`) that stop_servers' `kill -- -$sid` targets. uv/uvicorn
# may fork further children of their own (workers, etc.), but those inherit
# this same process group, so the group kill still reaps the whole tree.
start_backend() {
    local db=$1 logf=$2
    shift 2
    (
        cd "$BACKEND_DIR" &&
            exec setsid env -u OPENAI_TIMEOUT_SECONDS \
                DATABASE_URL="sqlite:///$db" \
                OPENAI_BASE_URL= OPENAI_API_KEY= OPENAI_MODEL= "$@" \
                uv run uvicorn afterthread.main:app --host 127.0.0.1 --port 0 >"$logf" 2>&1 &
        echo $! >"$TMPDIR_E2E/last.sid"
    )
    local sid
    sid="$(cat "$TMPDIR_E2E/last.sid")"
    SIDS+=("$sid")
    if ! wait_log_line "$logf" "$BACKEND_PORT_PATTERN" 500; then
        echo "  (backend failed to start; log tail:)"; tail -n 15 "$logf" | sed 's/^/    /' || true
        return 1
    fi
    LAST_PORT="$(port_from_log "$logf" "$BACKEND_PORT_PATTERN")"
    if [ -z "$LAST_PORT" ]; then
        echo "  (backend: could not parse bound port from log tail:)"; tail -n 15 "$logf" | sed 's/^/    /' || true
        return 1
    fi
    if ! wait_200 "http://127.0.0.1:$LAST_PORT/api/health" 200; then
        echo "  (backend failed readiness check; log tail:)"; tail -n 15 "$logf" | sed 's/^/    /' || true
        return 1
    fi
}

# start_preview LOGFILE
#
# Always launched with `--port 0` (no --strictPort: with an OS-assigned port
# "already in use" can't happen, so it would be a no-op) -- vite prints the
# port it actually bound in its own "Local: http://127.0.0.1:<port>/" line.
# This function waits for that line (bounded, PREVIEW_PORT_PATTERN), parses
# the port out of it, and leaves it in LAST_PORT for the caller -- see
# port_from_log above.
#
# Same PID == PGID == SID guarantee as start_backend, and for the same
# reason: `exec` before setsid forces the backgrounded "cd && setsid" job to
# become setsid in place (no extra fork), so $! is the actual session/group
# leader that stop_servers' `kill -- -$sid` targets. pnpm may itself spawn
# the real vite preview process as a child, but that child inherits this
# process group too, so the group kill still reaps it.
start_preview() {
    local logf=$1
    (
        cd "$FRONTEND_DIR" &&
            exec setsid pnpm preview --host 127.0.0.1 --port 0 >"$logf" 2>&1 &
        echo $! >"$TMPDIR_E2E/last.sid"
    )
    local sid
    sid="$(cat "$TMPDIR_E2E/last.sid")"
    SIDS+=("$sid")
    if ! wait_log_line "$logf" "$PREVIEW_PORT_PATTERN" 300; then
        echo "  (preview failed to start; log tail:)"; tail -n 15 "$logf" | sed 's/^/    /' || true
        return 1
    fi
    LAST_PORT="$(port_from_log "$logf" "$PREVIEW_PORT_PATTERN")"
    if [ -z "$LAST_PORT" ]; then
        echo "  (preview: could not parse bound port from log tail:)"; tail -n 15 "$logf" | sed 's/^/    /' || true
        return 1
    fi
    if ! wait_200 "http://127.0.0.1:$LAST_PORT/" 120; then
        echo "  (preview failed readiness check; log tail:)"; tail -n 15 "$logf" | sed 's/^/    /' || true
        return 1
    fi
}

# Kill every tracked server's process group (TERM, then KILL after a grace
# period) and reset the array. This signals ONLY PIDs the harness itself
# started and tracked into SIDS -- there is deliberately no port-based
# fallback kill here. A port-number sweep (e.g. `lsof -ti tcp:$p | xargs
# kill`) can't distinguish "our server, still bound" from "our server already
# exited and something unrelated grabbed the port in the race window since"
# and would SIGKILL whatever it finds either way -- on a shared CI box or a
# dev machine that "whatever" can be someone else's process. Each $sid is the
# tracked launch's own session/group leader (see start_backend/start_mock/
# start_preview), so `kill -- -$sid` reaps that server's entire tree by
# construction, making a port sweep both unnecessary and unsafe.
stop_servers() {
    local sid
    for sid in "${SIDS[@]:-}"; do
        [ -n "$sid" ] && kill -TERM -"$sid" 2>/dev/null || true
    done
    sleep 0.3 || true
    for sid in "${SIDS[@]:-}"; do
        [ -n "$sid" ] && kill -KILL -"$sid" 2>/dev/null || true
    done
    SIDS=()
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
    db="$TMPDIR_E2E/a.sqlite"
    log="$TMPDIR_E2E/a-backend.log"
    body="$TMPDIR_E2E/a-body.json"
    start_backend "$db" "$log"
    port="$LAST_PORT"
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
    bumped="$(ts_bumped "$before" "$after")"
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
    phase_banner "B (good mock: capture / enrich / supersede / assist-update / filters+pagination)"
    local mport bport db mlog blog body
    db="$TMPDIR_E2E/b.sqlite"
    mlog="$TMPDIR_E2E/b-mock.log"
    blog="$TMPDIR_E2E/b-backend.log"
    body="$TMPDIR_E2E/b-body.json"
    start_mock "good" "10" "$mlog"
    mport="$LAST_PORT"
    start_backend "$db" "$blog" \
        OPENAI_BASE_URL="http://127.0.0.1:$mport/v1" OPENAI_API_KEY="test" \
        OPENAI_MODEL="mock" OPENAI_TIMEOUT_SECONDS="30"
    bport="$LAST_PORT"
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
    ubumped="$(ts_bumped "$before_u" "$after_u")"
    assert_eq "assist-update bumps updated timestamp" "$ubumped" "yes"
    req GET "$base/items/$id" "$body" >/dev/null
    assert_true "assist-update appends its progress note" "$body" \
        'd["progress"][-1]["note"] == "更新後續行動與待答問題"'
    assert_true "full progress trail recorded in order" "$body" \
        '[e["note"] for e in d["progress"]] == ["AI 快速捕捉", "補充初步決策與背景資訊", "checklist 完成,決策改為方案B", "更新後續行動與待答問題"]'

    # -- list filters: tag / q substring (+ non-match) on the captured item --
    # The captured item is still the only row in this phase's DB, and it
    # carries known zh-TW tags (["付款", "重構", "金流"]) and title
    # ("重構付款流程以支援新金流商") from the mock's capture draft, untouched by
    # either enrich call above (neither variant's `sections` includes "tags").
    req GET "$base/items?tag=$(urlenc "付款")" "$body" >/dev/null
    assert_true "tag filter (one of the captured item's tags) -> total 1" "$body" 'd["total"] == 1'

    # The title is pure Traditional Chinese, which has no upper/lower
    # distinction, so a "case-variant" of a substring is byte-identical to the
    # substring itself here -- this still exercises the py_casefold-driven
    # q substring match end-to-end (cross-case folding on a cased script, e.g.
    # "école"/"RÉSUMÉ", is covered by backend/tests/test_items.py).
    local qsub
    qsub="$(python3 -c 'print("付款流程".upper())')"
    req GET "$base/items?q=$(urlenc "$qsub")" "$body" >/dev/null
    assert_true "q filter (case-variant substring of captured title) -> total 1" "$body" \
        'd["total"] == 1'

    req GET "$base/items?q=$(urlenc "不存在的字串xyz")" "$body" >/dev/null
    assert_true "q filter non-matching substring -> total 0" "$body" 'd["total"] == 0'

    # -- pagination: limit/offset once a second item exists ------------------
    # Checking the item COUNT at a single offset can't tell "offset applied"
    # apart from "offset ignored": an implementation that always serves page 0
    # would still return len==1/total==2 for limit=1&offset=1 here (there are
    # only 2 rows total). Fetch both offset=0 and offset=1 and additionally
    # compare the returned ids -- a real offset must page to a DIFFERENT row.
    # List order is `updated DESC, id DESC` (see routers/items.py::list_items).
    # Item #1 (`$id`) was last touched by assist-update above; the second item
    # is created after that (and gets a higher id too), so it is the
    # more-recently-updated row and sorts first (offset=0), while item #1 --
    # older by `updated`, and by `id` -- sorts second (offset=1) regardless of
    # timestamp precision.
    local second="$TMPDIR_E2E/b-second-create.json"
    assert_eq "create second item 201" \
        "$(req POST "$base/items" "$second" '{"title":"第二個項目","snapshot":"用於分頁測試"}')" "201"

    local page0="$TMPDIR_E2E/b-page0.json" page1="$TMPDIR_E2E/b-page1.json"
    req GET "$base/items?limit=1&offset=0" "$page0" >/dev/null
    req GET "$base/items?limit=1&offset=1" "$page1" >/dev/null
    assert_true "limit=1&offset=0 returns exactly 1 item" "$page0" 'len(d["items"]) == 1'
    assert_true "limit=1&offset=0 total == 2" "$page0" 'd["total"] == 2'
    assert_true "limit=1&offset=1 returns exactly 1 item" "$page1" 'len(d["items"]) == 1'
    assert_true "limit=1&offset=1 total == 2" "$page1" 'd["total"] == 2'

    local id1
    id1="$(jget "$page1" 'd["items"][0]["id"]')"
    assert_true "offset=0 and offset=1 return different item ids" "$page0" \
        "d[\"items\"][0][\"id\"] != $id1"
    assert_eq "offset=1 id is item #1, the older-updated row" "$id1" "$id"

    stop_servers
}

# ===========================================================================
# PHASE C -- degradation: garbage output (502, no row) and slow (502 Timeout)
# ===========================================================================
phase_c() {
    phase_banner "C (degradation: garbage 502 + slow Timeout 502 + conflict 409)"

    # -- garbage: prose junk -> retry -> 502, nothing written ----------------
    local mport bport db mlog blog body
    db="$TMPDIR_E2E/c-garbage.sqlite"
    mlog="$TMPDIR_E2E/c-garbage-mock.log"
    blog="$TMPDIR_E2E/c-garbage-backend.log"
    body="$TMPDIR_E2E/c-garbage-body.json"
    start_mock "garbage" "10" "$mlog"
    mport="$LAST_PORT"
    start_backend "$db" "$blog" \
        OPENAI_BASE_URL="http://127.0.0.1:$mport/v1" OPENAI_API_KEY="test" \
        OPENAI_MODEL="mock" OPENAI_TIMEOUT_SECONDS="30"
    bport="$LAST_PORT"
    local base="http://127.0.0.1:$bport/api"

    assert_eq "garbage capture -> 502" \
        "$(req POST "$base/capture" "$body" '{"raw_text":"降級測試:期望回傳 502 的原始討論。"}')" "502"
    assert_true "garbage capture code llm_upstream_error" "$body" \
        'd["detail"]["code"] == "llm_upstream_error"'
    req GET "$base/items" "$body" >/dev/null
    assert_true "garbage capture wrote no row (total == 0)" "$body" 'd["total"] == 0'
    stop_servers

    # -- slow: sleeps past a tiny timeout -> 502 Timeout within the deadline --
    db="$TMPDIR_E2E/c-slow.sqlite"
    mlog="$TMPDIR_E2E/c-slow-mock.log"
    blog="$TMPDIR_E2E/c-slow-backend.log"
    body="$TMPDIR_E2E/c-slow-body.json"
    start_mock "slow" "10" "$mlog"
    mport="$LAST_PORT"
    # Tiny 2s deadline; the mock sleeps 10s, so the wall-clock bound must fire.
    start_backend "$db" "$blog" \
        OPENAI_BASE_URL="http://127.0.0.1:$mport/v1" OPENAI_API_KEY="test" \
        OPENAI_MODEL="mock" OPENAI_TIMEOUT_SECONDS="2"
    bport="$LAST_PORT"
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

    # -- conflict: a PATCH landing mid-await races an in-flight enrich -------
    # backend OPENAI_TIMEOUT_SECONDS is deliberately large (30) here: unlike
    # the slow-Timeout leg above, this scenario wants the mock's held reply to
    # actually SUCCEED (just late), so the race is between the in-flight
    # enrich and a concurrent PATCH -- not between the mock and the deadline.
    #
    # This is a barrier by construction, not a timing window: --mode hold
    # blocks the mock's reply until this scenario touches --release-file, so
    # there is no fixed delay to race against (unlike a --mode slow sleep,
    # which could in principle elapse before the PATCH lands on a slow/busy
    # runner). The PATCH is guaranteed to land while the enrich is still
    # parked on the mock, every run, on any runner.
    db="$TMPDIR_E2E/c-conflict.sqlite"
    mlog="$TMPDIR_E2E/c-conflict-mock.log"
    blog="$TMPDIR_E2E/c-conflict-backend.log"
    body="$TMPDIR_E2E/c-conflict-body.json"
    local release_file="$TMPDIR_E2E/c-conflict.release"
    start_mock "hold" "0" "$mlog" --release-file "$release_file"
    mport="$LAST_PORT"
    start_backend "$db" "$blog" \
        OPENAI_BASE_URL="http://127.0.0.1:$mport/v1" OPENAI_API_KEY="test" \
        OPENAI_MODEL="mock" OPENAI_TIMEOUT_SECONDS="30"
    bport="$LAST_PORT"
    base="http://127.0.0.1:$bport/api"

    local ccreate="$TMPDIR_E2E/c-conflict-create.json"
    assert_eq "conflict: create item 201" \
        "$(req POST "$base/items" "$ccreate" '{"title":"併發衝突測試項目","status":"needs-enrichment"}')" "201"
    local cid
    cid="$(jget "$ccreate" 'd["id"]')"

    # Fire enrich in the background: the handler snapshots `updated` almost
    # immediately, then blocks inside the LLM call -- which runs strictly
    # outside any DB transaction (see backend/README.md's optimistic-409
    # design note) -- until the mock is released below. Its HTTP status lands
    # in $enrich_code; curl itself always exits 0 here (no -f), so this
    # background job cannot trip `set -e` in the parent shell regardless of
    # what status the server returns.
    local enrich_out="$TMPDIR_E2E/c-conflict-enrich.json"
    local enrich_code="$TMPDIR_E2E/c-conflict-enrich-code.txt"
    (req POST "$base/items/$cid/enrich" "$enrich_out" \
        '{"additional_context":"背景補充,預期與併發 PATCH 衝突。"}' >"$enrich_code") &
    local enrich_pid=$!

    # Wait for a SIGNAL instead of a fixed sleep: _snapshot_item_for_ai
    # (afterthread/routers/ai.py) is awaited to completion BEFORE enrich_item ever
    # posts to the mock, so the mock logging "holding response" -- written
    # the instant it has received and JSON-parsed the request, strictly
    # BEFORE it starts polling for the release file -- is proof the pre-await
    # `updated` snapshot has already been taken. The mock then blocks there
    # (bounded by its own 60s hard cap) until released below, so -- unlike a
    # fixed sleep -- there is no risk of the reply racing ahead of the PATCH
    # on a loaded runner.
    if ! wait_log_line "$mlog" "holding response"; then
        echo "  (timed out waiting for the mock to hold the enrich request; log:)"
        sed 's/^/    /' "$mlog" || true
    fi

    local cpatch="$TMPDIR_E2E/c-conflict-patch.json"
    assert_eq "conflict: concurrent PATCH -> 200" \
        "$(req PATCH "$base/items/$cid" "$cpatch" '{"status":"active"}')" "200"

    # Only now release the mock's held reply: the PATCH is guaranteed to have
    # already landed, so the enrich's own DB write must lose the race.
    touch "$release_file"

    wait "$enrich_pid"
    local ccode
    ccode="$(cat "$enrich_code")"
    assert_eq "conflict: enrich racing a concurrent PATCH -> 409" "$ccode" "409"
    assert_true "conflict: 409 detail.code == conflict" "$enrich_out" 'd["detail"]["code"] == "conflict"'

    req GET "$base/items/$cid" "$body" >/dev/null
    assert_true "conflict: concurrent PATCH's value survived (not overwritten)" "$body" \
        'd["status"] == "active"'

    stop_servers
}

# ===========================================================================
# PHASE D -- frontend production build serves the SPA shell
# ===========================================================================
phase_d() {
    phase_banner "D (frontend build + preview serves SPA shell)"

    # A fresh clone has no frontend/node_modules, so `pnpm build` below would
    # fail outright. Installing first (frozen to the committed lockfile, so
    # this never silently drifts deps) makes the phase self-sufficient; when
    # node_modules is already up to date -- the common case on a dev machine
    # or a warm CI cache -- pnpm's own up-to-date check makes this near-instant.
    local installlog="$TMPDIR_E2E/d-install.log"
    if (cd "$FRONTEND_DIR" && pnpm install --frozen-lockfile >"$installlog" 2>&1); then
        pass "pnpm install --frozen-lockfile succeeds"
    else
        fail "pnpm install --frozen-lockfile failed"
        tail -n 15 "$installlog" | sed 's/^/    /' || true
        return 0
    fi

    local buildlog="$TMPDIR_E2E/d-build.log"
    if (cd "$FRONTEND_DIR" && pnpm build >"$buildlog" 2>&1); then
        pass "pnpm build succeeds"
    else
        fail "pnpm build failed"
        tail -n 15 "$buildlog" | sed 's/^/    /' || true
        return 0
    fi

    local pport plog root items
    plog="$TMPDIR_E2E/d-preview.log"
    root="$TMPDIR_E2E/d-root.html"
    items="$TMPDIR_E2E/d-items.html"
    start_preview "$plog"
    pport="$LAST_PORT"
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
    echo "afterthread e2e smoke -- repo: $REPO_ROOT"
    echo "Temp dir: $TMPDIR_E2E"

    phase_a
    phase_b
    phase_c
    phase_d

    echo ""
    echo "========== SUMMARY =========="
    local ph
    for ph in "A (unconfigured backend: probes + CRUD)" \
        "B (good mock: capture / enrich / supersede / assist-update / filters+pagination)" \
        "C (degradation: garbage 502 + slow Timeout 502 + conflict 409)" \
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
