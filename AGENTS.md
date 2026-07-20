# context-memory

## Communication

- Reason internally in English when helpful.
- Communicate with the user in Traditional Chinese unless the user explicitly asks for another language.

## Project Purpose

This repo is a personal context-memory system for preventing architectural knowledge vaporization: the loss of project reasoning, decision context, tradeoffs, and next-action memory after conversations have gone cold. It has two interfaces sharing the same capture / full-enrichment / update / review methodology (`docs/methodology.md`) but NOT sharing data: a primary web app (SQLite-backed) and a supplementary file-based workflow (`memory/**/*.md`).

## Operating Rules

- The web app is the primary surface: `backend/` (FastAPI + SQLite) and `frontend/` (React/Mantine SPA), packaged and launched as one process via the `context-memory` console script (see root `README.md` for install/launch, `backend/README.md` for API/config/dev details, `frontend/README.md` for page/component conventions). Prefer it for any new user-facing capability; this is no longer a file-based-only MVP.
- The OpenCode skill (`.opencode/skills/context-memory/SKILL.md`) and `scripts/context_memory.py` remain supported supplements for terminal/agent-driven capture. They operate on the separate `memory/**/*.md` file store — do not assume they read or write the web app's SQLite database, or vice versa.
- Keep records small, durable, and easy to resume from, regardless of which surface created them.
- When working with the file-based store: run `python3 scripts/context_memory.py validate` after editing memory records, and `python3 scripts/context_memory.py index` after adding or materially updating one.
- When working on the web app's backend/frontend code: run that surface's own gates before considering a change done (see `backend/README.md` / `frontend/README.md` for the exact lint/type/test commands; `e2e/smoke.sh` and `e2e/wheel_smoke.sh` for full-stack checks).
- Ground every change in the actual code and existing docs (`docs/methodology.md`, `docs/web-v2-decisions.md`) rather than inventing workflows or UI that do not exist — in particular, this app has no chat interface; AI features are structured, per-item operations (capture, enrich, assist-update, tool install).

