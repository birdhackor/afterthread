#!/usr/bin/env bash
#
# Build the afterthread wheel (+ sdist) with the frontend's production
# build baked in.
#
# Run from anywhere:    bash scripts/build-wheel.sh
# Prerequisites:        pnpm (frontend build), uv (backend build)
# Produces:             backend/dist/afterthread-*.whl and *.tar.gz
# Optional release gate: set RELEASE_TAG=vX.Y.Z to require that the tag
#                        matches backend/pyproject.toml's project version.
#
# Why a pre-build-and-copy step rather than pointing hatch straight at
# ../frontend/dist (see D02 in docs/web-v2-decisions.md): `uv build` builds
# the sdist first and then builds the wheel FROM that sdist in an isolated,
# unpacked copy -- `../frontend/dist` does not exist relative to that copy,
# so a hatch `force-include` pointing there raises FileNotFoundError on every
# build. Copying the built frontend inside `backend/afterthread/static/`
# (gitignored; see backend/pyproject.toml's `artifacts` entries) keeps it
# inside the tree hatch actually packages, in both the sdist and the wheel.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_DIR="$REPO_ROOT/frontend"
BACKEND_DIR="$REPO_ROOT/backend"
STATIC_DIR="$BACKEND_DIR/afterthread/static"
DIST_DIR="$BACKEND_DIR/dist"

echo "==> [1/6] Checking release metadata and cleaning old archives"
if ! cmp -s "$REPO_ROOT/LICENSE" "$BACKEND_DIR/LICENSE"; then
    echo "ERROR: LICENSE and backend/LICENSE differ." >&2
    echo "The repository and distribution must ship identical MIT terms." >&2
    exit 1
fi

PACKAGE_VERSION="$(cd "$BACKEND_DIR" && uv version --short)"
if [ -n "${RELEASE_TAG:-}" ] && [ "$RELEASE_TAG" != "v$PACKAGE_VERSION" ]; then
    echo "ERROR: release tag '$RELEASE_TAG' does not match package version '$PACKAGE_VERSION'." >&2
    echo "Expected tag: v$PACKAGE_VERSION" >&2
    exit 1
fi

# dist/ is a disposable build-output directory. Remove every prior Python
# archive, including artifacts left behind under the pre-rename package name,
# so a later upload can never pick up a stale wheel/sdist via a broad glob.
mkdir -p "$DIST_DIR"
for archive in "$DIST_DIR"/*.whl "$DIST_DIR"/*.tar.gz; do
    [ -e "$archive" ] || continue
    rm -f -- "$archive"
done

echo "    OK: version $PACKAGE_VERSION; previous wheel/sdist archives removed"

echo "==> [2/6] Installing frontend dependencies (frozen lockfile)"
pnpm --dir "$FRONTEND_DIR" install --frozen-lockfile

echo "==> [3/6] Building frontend production bundle"
pnpm --dir "$FRONTEND_DIR" build

echo "==> [4/6] Refreshing backend/afterthread/static/ from frontend/dist"
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

echo "==> [5/6] Building wheel + sdist (uv build)"
(cd "$BACKEND_DIR" && uv build)

echo "==> [6/6] Validating artifacts"
EXPECTED_WHEEL="$DIST_DIR/afterthread-$PACKAGE_VERSION-py3-none-any.whl"
EXPECTED_SDIST="$DIST_DIR/afterthread-$PACKAGE_VERSION.tar.gz"
if [ ! -f "$EXPECTED_WHEEL" ] || [ ! -f "$EXPECTED_SDIST" ]; then
    echo "ERROR: expected release artifacts were not produced:" >&2
    echo "  $EXPECTED_WHEEL" >&2
    echo "  $EXPECTED_SDIST" >&2
    exit 1
fi

archive_count=0
for archive in "$DIST_DIR"/*.whl "$DIST_DIR"/*.tar.gz; do
    [ -e "$archive" ] || continue
    archive_count=$((archive_count + 1))
done
if [ "$archive_count" -ne 2 ]; then
    echo "ERROR: dist contains $archive_count Python archives; expected exactly 2." >&2
    ls -la "$DIST_DIR" >&2
    exit 1
fi

ls -la "$DIST_DIR"
