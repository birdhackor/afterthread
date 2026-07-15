#!/usr/bin/env bash
#
# Standalone packaged-mode e2e smoke test.
#
# Builds the real wheel (scripts/build-wheel.sh), installs and runs it via
# `uvx` -- exactly how an end user would, per README's `uvx` install story --
# and drives the packaged app's HTTP surface with curl. This is the only
# harness that exercises the actual installed artifact: e2e/smoke.sh drives
# the backend via `uv run uvicorn` from a checkout and never touches
# packaging (wheel contents, the `context-memory` console script, SPA static
# serving, or the `cli.py` data-dir/`.env` wiring) at all.
#
# Run from the repo root:  bash e2e/wheel_smoke.sh
# Prerequisites:            uv (provides `uvx`), pnpm (frontend build), and
#                           network access on the FIRST run of a given
#                           environment -- `uvx` resolves the wheel's
#                           dependencies into its own managed tool venv, and
#                           an empty/cold uv package cache means real
#                           downloads. A warm cache (e.g. right after
#                           `uv sync` in backend/) makes this fast and
#                           offline, since the pinned versions match uv.lock.
#
# Every assertion prints PASS/FAIL; the script exits non-zero if any FAIL.
# The uvx-launched server (its whole process tree) and the temp dirs are
# always cleaned up via an EXIT trap -- see e2e/smoke.sh for the same
# process-group-kill hygiene this script borrows (setsid + `kill -- -$sid`,
# so it doesn't matter how many layers of subprocess uvx/uvicorn spawn
# underneath; they all inherit the one process group that gets signaled).

set -euo pipefail

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TMPDIR_E2E="$(mktemp -d "${TMPDIR:-/tmp}/cm-wheel-e2e.XXXXXX")"
DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cm-wheel-data.XXXXXX")"
SERVER_LOG="$TMPDIR_E2E/server.log"
HEADERS_FILE="$TMPDIR_E2E/last-headers.txt"

declare -a SIDS=()
LAST_PORT=""
WHEEL=""

PASS_COUNT=0
FAIL_COUNT=0

pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf '  PASS: %s\n' "$1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf '  FAIL: %s\n' "$1"; }

# assert_eq LABEL ACTUAL EXPECTED
assert_eq() {
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (got '$2', want '$3')"; fi
}

# assert_str_contains LABEL HAYSTACK NEEDLE (plain substring, not a glob)
assert_str_contains() {
    case "$2" in
        *"$3"*) pass "$1" ;;
        *) fail "$1 (got '$2', want substring '$3')" ;;
    esac
}

# assert_file_contains LABEL FILE FIXED_STRING
assert_file_contains() {
    if grep -qF -- "$3" "$2" 2>/dev/null; then pass "$1"; else fail "$1 (missing '$3')"; fi
}

# assert_true LABEL JSON_FILE PY_EXPR (mirrors e2e/smoke.sh's helper)
assert_true() {
    local out
    out="$(python3 - "$2" "$3" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    print(f"ERR:{exc}")
    raise SystemExit(0)
ns = {"d": d, "json": json, "len": len, "str": str, "bool": bool}
try:
    print("True" if bool(eval(sys.argv[2], {"__builtins__": {}}, ns)) else "False")
except Exception as exc:
    print(f"ERR:{exc}")
PY
)"
    if [ "$out" = "True" ]; then pass "$1"; else fail "$1 [$out]"; fi
}

# --- HTTP helper -------------------------------------------------------

# req METHOD URL OUTFILE [HEADER] [JSON_DATA] -> prints the HTTP status code.
# Always dumps response headers to $HEADERS_FILE (overwritten every call --
# read it via header_value immediately after, before the next req call).
req() {
    local method=$1 url=$2 out=$3 header=${4:-} data=${5:-}
    local -a curl_args
    curl_args=(-s --max-time 30 -D "$HEADERS_FILE" -o "$out" -w '%{http_code}' -X "$method")
    if [ -n "$header" ]; then
        curl_args+=(-H "$header")
    fi
    if [ -n "$data" ]; then
        curl_args+=(-H 'Content-Type: application/json' -d "$data")
    fi
    curl_args+=("$url")
    curl "${curl_args[@]}"
}

# header_value NAME -> case-insensitive header value from the last req()
# call's response, or "" if absent/unparseable.
header_value() {
    python3 - "$HEADERS_FILE" "$1" <<'PY'
import sys
name = sys.argv[2].strip().lower()
try:
    text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
except Exception:
    print("")
    raise SystemExit(0)
for line in text.splitlines():
    if ":" not in line:
        continue
    key, _, value = line.partition(":")
    if key.strip().lower() == name:
        print(value.strip())
        raise SystemExit(0)
print("")
PY
}

# extract_asset_js HTML_FILE -> the first /assets/*.js path referenced in
# the file, or "" if none found.
extract_asset_js() {
    python3 - "$1" <<'PY'
import re, sys
try:
    html = open(sys.argv[1], encoding="utf-8").read()
except Exception:
    print("")
    raise SystemExit(0)
m = re.search(r'(/assets/[^"\'<>]+\.js)', html)
print(m.group(1) if m else "")
PY
}

# --- process management (mirrors e2e/smoke.sh's start_backend hygiene) ----

BACKEND_PORT_PATTERN="Uvicorn running on http://127.0.0.1:"

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

port_from_log() {
    awk -v pat="$2" '
        index($0, pat) {
            rest = substr($0, index($0, pat) + length(pat))
            if (match(rest, /[0-9]+/)) { print substr(rest, RSTART, RLENGTH); exit }
        }
    ' "$1" 2>/dev/null
}

wait_200() {
    local url=$1 tries=${2:-200} i
    for ((i = 1; i <= tries; i++)); do
        if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null)" = "200" ]; then
            return 0
        fi
        sleep 0.25
    done
    return 1
}

# start_server -> launches `uvx --from $WHEEL context-memory` in its own
# session/process group (so the whole tree -- uvx, its managed venv's
# python, uvicorn -- is reaped together on teardown regardless of how many
# layers of subprocess sit in between), always with `--port 0` (no
# free()-then-bind TOCTOU window: uvicorn binds an OS-assigned port and logs
# it, exactly as e2e/smoke.sh's start_backend relies on for the same
# in-process `uvicorn.run()` call cli.py itself makes). The three OPENAI_*
# vars are forced empty so `configured` reads false regardless of the
# invoking shell's own environment (the .env main() pre-writes into
# $DATA_DIR deliberately contains no OPENAI_* keys, and cli.py's dotenv
# loading is override=False, so these explicit empties always win -- the
# same hole e2e/smoke.sh's start_backend documents for its own env/dotenv
# precedence).
#
# Launched with CWD=$TMPDIR_E2E, deliberately NOT $DATA_DIR: cli.py owns
# chdir-ing into the data dir, and the relative-DATABASE_URL assertion in
# main() only proves anything because the launch directory and the data dir
# are different -- if the chdir were dropped, the relative sqlite path from
# $DATA_DIR/.env would land the database HERE in $TMPDIR_E2E instead.
#
# ~60s cap on the startup log line: the FIRST `uvx` invocation in a given
# environment resolves and installs the wheel's dependencies into a fresh
# tool venv before context-memory's own "Context Memory: data dir=..." line
# (let alone uvicorn's) ever prints -- see the prerequisites note up top.
start_server() {
    (
        cd "$TMPDIR_E2E" &&
            exec setsid env \
                OPENAI_BASE_URL= OPENAI_API_KEY= OPENAI_MODEL= \
                uvx --from "$WHEEL" context-memory \
                    --host 127.0.0.1 --port 0 --data-dir "$DATA_DIR" \
                    >"$SERVER_LOG" 2>&1 &
        echo $! >"$TMPDIR_E2E/last.sid"
    )
    local sid
    sid="$(cat "$TMPDIR_E2E/last.sid")"
    SIDS+=("$sid")
    if ! wait_log_line "$SERVER_LOG" "$BACKEND_PORT_PATTERN" 600; then
        echo "  (server failed to start within ~60s; log tail:)"
        tail -n 40 "$SERVER_LOG" | sed 's/^/    /' || true
        return 1
    fi
    LAST_PORT="$(port_from_log "$SERVER_LOG" "$BACKEND_PORT_PATTERN")"
    if [ -z "$LAST_PORT" ]; then
        echo "  (could not parse bound port from log; log tail:)"
        tail -n 40 "$SERVER_LOG" | sed 's/^/    /' || true
        return 1
    fi
    if ! wait_200 "http://127.0.0.1:$LAST_PORT/api/health" 200; then
        echo "  (server failed readiness check; log tail:)"
        tail -n 40 "$SERVER_LOG" | sed 's/^/    /' || true
        return 1
    fi
}

stop_server() {
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
    stop_server
    [ -n "${TMPDIR_E2E:-}" ] && rm -rf "$TMPDIR_E2E" 2>/dev/null || true
    [ -n "${DATA_DIR:-}" ] && rm -rf "$DATA_DIR" 2>/dev/null || true
    exit "$ec"
}
trap teardown EXIT

# ===========================================================================
# main
# ===========================================================================
main() {
    echo "Context Memory wheel e2e smoke -- repo: $REPO_ROOT"
    echo "Scratch dir: $TMPDIR_E2E"
    echo "Data dir:    $DATA_DIR"

    echo ""
    echo "========== BUILD =========="
    bash "$REPO_ROOT/scripts/build-wheel.sh"

    local wheel
    wheel="$(ls -t "$REPO_ROOT/backend/dist"/context_memory-*.whl 2>/dev/null | head -n1)"
    if [ -z "$wheel" ]; then
        echo "FATAL: no backend/dist/context_memory-*.whl found after build-wheel.sh" >&2
        exit 1
    fi
    WHEEL="$wheel"
    echo "Using wheel: $WHEEL"

    # Pre-write a data-dir .env BEFORE the server starts: this run then also
    # proves the packaged-mode config contract end-to-end -- cli.py must load
    # <data-dir>/.env, and a RELATIVE sqlite path in it must be anchored to
    # the data dir (cli.py chdirs there), not to the launch CWD (which
    # start_server deliberately sets elsewhere -- see its comment). The file
    # contains no OPENAI_* keys, so the llm/status configured=false assertion
    # below still holds via start_server's explicit empty-string env vars.
    cat >"$DATA_DIR/.env" <<'ENVEOF'
# Written by e2e/wheel_smoke.sh (packaged-mode .env loading + anchoring test).
DATABASE_URL=sqlite:///./from_envfile.db
ENVEOF

    echo ""
    echo "========== SERVE (uvx --from <wheel> context-memory) =========="
    if ! start_server; then
        echo "FATAL: packaged server did not start -- aborting before any assertions." >&2
        exit 1
    fi
    local base="http://127.0.0.1:$LAST_PORT"
    echo "Server up: $base  (data dir: $DATA_DIR)"

    echo ""
    echo "========== ASSERTIONS =========="
    local code

    # -- GET / -> the SPA shell, same-origin, never cached -------------------
    local root_body="$TMPDIR_E2E/root.html"
    code="$(req GET "$base/" "$root_body")"
    local root_ct root_cc
    root_ct="$(header_value Content-Type)"
    root_cc="$(header_value Cache-Control)"
    assert_eq "GET / -> 200" "$code" "200"
    assert_str_contains "GET / Content-Type contains text/html" "$root_ct" "text/html"
    assert_file_contains "GET / body contains the SPA mount node" "$root_body" '<div id="root">'
    assert_str_contains "GET / Cache-Control contains no-cache" "$root_cc" "no-cache"

    # -- GET /items/123 (SPA deep link, browser-navigation Accept) -----------
    local deep_body="$TMPDIR_E2E/deep.html"
    code="$(req GET "$base/items/123" "$deep_body" "Accept: text/html")"
    assert_eq "GET /items/123 (deep link) -> 200" "$code" "200"
    assert_file_contains "GET /items/123 serves the index.html shell" "$deep_body" '<div id="root">'

    # -- a real /assets/*.js -> long-cache, immutable -------------------------
    local asset_path
    asset_path="$(extract_asset_js "$root_body")"
    if [ -z "$asset_path" ]; then
        fail "extract a real /assets/*.js path from index.html"
    else
        pass "extract a real /assets/*.js path from index.html ($asset_path)"
        local asset_body="$TMPDIR_E2E/asset.js" asset_ct asset_cc
        code="$(req GET "$base$asset_path" "$asset_body")"
        asset_ct="$(header_value Content-Type)"
        asset_cc="$(header_value Cache-Control)"
        assert_eq "GET $asset_path -> 200" "$code" "200"
        assert_str_contains "GET $asset_path Content-Type contains javascript" "$asset_ct" "javascript"
        assert_str_contains "GET $asset_path Cache-Control contains immutable" "$asset_cc" "immutable"
    fi

    # -- GET /api/nonexistent -> 404 JSON, not the SPA shell ------------------
    # Requires an explicit `Accept: application/json` here: FastAPI's
    # app.frontend() fallback (main.py) treats ANY request as
    # "looks like a browser navigating" -- and so serves index.html instead
    # of 404ing -- whenever Accept includes `text/html` OR is a bare `*/*`,
    # which is curl's (and a browser fetch()'s) own default Accept header
    # when none is set. A plain `curl $base/api/nonexistent` with no -H would
    # therefore get a 200 index.html here, NOT a 404 -- confirmed against the
    # installed fastapi.routing._is_frontend_navigation_request implementation
    # during implementation. The app's own client (frontend/src/api/client.js)
    # sends `Accept: application/json` on every request for exactly this
    # reason; curl passes it explicitly here to SIMULATE that client-layer
    # behavior (curl's default would not), so this assertion exercises the
    # same request shape the real SPA produces.
    local nf_body="$TMPDIR_E2E/nonexistent.json"
    code="$(req GET "$base/api/nonexistent" "$nf_body" "Accept: application/json")"
    local nf_ct
    nf_ct="$(header_value Content-Type)"
    assert_eq "GET /api/nonexistent -> 404" "$code" "404"
    assert_str_contains "GET /api/nonexistent Content-Type contains application/json" "$nf_ct" "application/json"

    # -- GET /api/llm/status -> unconfigured ---------------------------------
    # The pre-written $DATA_DIR/.env has no OPENAI_* keys, and start_server's
    # explicit empty-string env vars win over any dotenv value anyway
    # (override=False), so this must read unconfigured.
    local status_body="$TMPDIR_E2E/llm-status.json"
    code="$(req GET "$base/api/llm/status" "$status_body")"
    assert_eq "GET /api/llm/status -> 200" "$code" "200"
    assert_true "GET /api/llm/status configured=false (no OPENAI_* anywhere)" "$status_body" \
        'd["configured"] is False'

    # -- POST /api/items -> the data-dir .env's relative DATABASE_URL is both
    # loaded AND anchored to the data dir -------------------------------------
    local create_body="$TMPDIR_E2E/create.json"
    code="$(req POST "$base/api/items" "$create_body" "" '{"title":"測試項目","snapshot":"初始快照"}')"
    assert_eq "POST /api/items -> 201" "$code" "201"
    # The .env pre-written above sets DATABASE_URL=sqlite:///./from_envfile.db
    # (relative), so the database must appear at $DATA_DIR/from_envfile.db:
    # its existence proves the .env was loaded (otherwise the default
    # context_memory.db name would be used) and that the relative path was
    # anchored to the data dir by cli.py's chdir (otherwise it would land in
    # the launch CWD, $TMPDIR_E2E). The two negative assertions pin each
    # failure mode separately.
    if [ -f "$DATA_DIR/from_envfile.db" ]; then
        pass "data-dir .env's relative DATABASE_URL lands in --data-dir (from_envfile.db exists)"
    else
        fail "data-dir .env's relative DATABASE_URL lands in --data-dir (from_envfile.db exists)"
    fi
    if [ ! -f "$DATA_DIR/context_memory.db" ]; then
        pass "default db name unused (data-dir .env's DATABASE_URL was really loaded)"
    else
        fail "default db name unused (data-dir .env's DATABASE_URL was really loaded)"
    fi
    if [ ! -f "$TMPDIR_E2E/from_envfile.db" ]; then
        pass "no database in the launch CWD (relative path anchored by cli.py's chdir)"
    else
        fail "no database in the launch CWD (relative path anchored by cli.py's chdir)"
    fi

    echo ""
    echo "========== SUMMARY =========="
    printf '  TOTAL: %d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
    if [ "$FAIL_COUNT" -gt 0 ]; then
        echo "RESULT: FAIL"
        exit 1
    fi
    echo "RESULT: PASS"
    exit 0
}

main "$@"
