#!/usr/bin/env bash
#
# Generate the frontend's compile-time API contract directly from FastAPI.
#
# The raw JSON is deliberately temporary: schema.gen.ts is the browser-build-
# independent artifact the frontend imports and commits. Keeping the
# intermediate JSON would create two generated files that can drift from one
# another without adding any type information.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"
OUTPUT="$FRONTEND_DIR/src/api/schema.gen.ts"

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
	cp "$GENERATED_TYPES" "$OUTPUT"
	printf 'updated %s\n' "$OUTPUT"
	exit 0
fi

if cmp -s "$OUTPUT" "$GENERATED_TYPES"; then
	printf 'API types are current: %s\n' "$OUTPUT"
	exit 0
fi

printf 'API types are stale; run `pnpm generate:api-types` from frontend/.\n' >&2
diff -u "$OUTPUT" "$GENERATED_TYPES" >&2 || true
exit 1
