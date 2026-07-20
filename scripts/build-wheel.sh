#!/usr/bin/env bash
#
# Build the context-memory wheel (+ sdist) with the frontend's production
# build baked in.
#
# Run from anywhere:    bash scripts/build-wheel.sh
# Prerequisites:        pnpm (frontend build), uv (backend build)
# Produces:             backend/dist/context_memory-*.whl and *.tar.gz
#
# Why a pre-build-and-copy step rather than pointing hatch straight at
# ../frontend/dist (see D02 in docs/web-v2-decisions.md): `uv build` builds
# the sdist first and then builds the wheel FROM that sdist in an isolated,
# unpacked copy -- `../frontend/dist` does not exist relative to that copy,
# so a hatch `force-include` pointing there raises FileNotFoundError on every
# build. Copying the built frontend inside `backend/context_memory/static/`
# (gitignored; see backend/pyproject.toml's `artifacts` entries) keeps it
# inside the tree hatch actually packages, in both the sdist and the wheel.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_DIR="$REPO_ROOT/frontend"
BACKEND_DIR="$REPO_ROOT/backend"
STATIC_DIR="$BACKEND_DIR/context_memory/static"

echo "==> [1/5] Installing frontend dependencies (frozen lockfile)"
pnpm --dir "$FRONTEND_DIR" install --frozen-lockfile

echo "==> [2/5] Building frontend production bundle"
pnpm --dir "$FRONTEND_DIR" build

echo "==> [3/5] Refreshing backend/context_memory/static/ from frontend/dist"
rm -rf "$STATIC_DIR"
cp -r "$FRONTEND_DIR/dist" "$STATIC_DIR"

# Freshness/sanity check: a silently-failed or misconfigured frontend build
# (or an accidental removal of this step from the pipeline) must fail loudly
# here, not ship a wheel with an empty/missing static/ that 404s on every
# route once installed.
if [ ! -f "$STATIC_DIR/index.html" ]; then
    echo "ERROR: $STATIC_DIR/index.html is missing." >&2
    echo "The frontend build did not produce a usable dist/ -- aborting before uv build." >&2
    exit 1
fi
echo "    OK: $STATIC_DIR/index.html present"

echo "==> [4/5] Building wheel + sdist (uv build)"
(cd "$BACKEND_DIR" && uv build)

echo "==> [5/5] Artifacts:"
ls -la "$BACKEND_DIR/dist"
