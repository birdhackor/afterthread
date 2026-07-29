#!/usr/bin/env bash
#
# Generate the frontend's compile-time API contract directly from FastAPI.
#
# The canonical OpenAPI JSON and its TypeScript projection are both committed.
# The JSON is the lossless contract drift sentinel: openapi-typescript
# deliberately omits constraints such as maxLength, so comparing only its
# projection would let real backend schema changes pass unnoticed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"
SCHEMA_OUTPUT="$FRONTEND_DIR/src/api/openapi.gen.json"
TYPES_OUTPUT="$FRONTEND_DIR/src/api/schema.gen.ts"

case "${1:-}" in
	"")
		MODE="write"
		;;
	--check)
		MODE="check"
		;;
	*)
		printf 'usage: %s [--check]\n' "$0" >&2
		exit 2
		;;
esac

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/afterthread-api-types.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT
SCHEMA_JSON="$TMP_DIR/openapi.json"
GENERATED_TYPES="$TMP_DIR/schema.gen.ts"

# Importing the application and asking FastAPI for app.openapi() uses the same
# Pydantic models and route metadata as production without binding a port,
# starting uvicorn, or making the generator depend on a live process.
(
	cd "$BACKEND_DIR"
	uv run --frozen python - "$SCHEMA_JSON" <<'PY'
import json
import sys
from pathlib import Path

from afterthread.main import app

Path(sys.argv[1]).write_text(
    json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
)

pnpm --dir "$FRONTEND_DIR" exec openapi-typescript \
	"$SCHEMA_JSON" \
	--output "$GENERATED_TYPES"

if [[ "$MODE" == "write" ]]; then
	cp "$SCHEMA_JSON" "$SCHEMA_OUTPUT"
	cp "$GENERATED_TYPES" "$TYPES_OUTPUT"
	printf 'updated %s\n' "$SCHEMA_OUTPUT"
	printf 'updated %s\n' "$TYPES_OUTPUT"
	exit 0
fi

STALE=0
if ! cmp -s "$SCHEMA_OUTPUT" "$SCHEMA_JSON"; then
	printf 'OpenAPI schema is stale; run `pnpm generate:api-types` from frontend/.\n' >&2
	diff -u "$SCHEMA_OUTPUT" "$SCHEMA_JSON" >&2 || true
	STALE=1
fi

if ! cmp -s "$TYPES_OUTPUT" "$GENERATED_TYPES"; then
	printf 'API types are stale; run `pnpm generate:api-types` from frontend/.\n' >&2
	diff -u "$TYPES_OUTPUT" "$GENERATED_TYPES" >&2 || true
	STALE=1
fi

if [[ "$STALE" -ne 0 ]]; then
	exit 1
fi

printf 'OpenAPI schema is current: %s\n' "$SCHEMA_OUTPUT"
printf 'API types are current: %s\n' "$TYPES_OUTPUT"
