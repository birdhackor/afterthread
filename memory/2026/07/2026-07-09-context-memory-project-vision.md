---
id: 2026-07-09-context-memory-project-vision
title: "context-memory project vision"
status: active
stage: full
created: 2026-07-09
updated: 2026-07-20
tags: [project-vision, mvp, opencode-skill, architectural-knowledge-vaporization]
source: user-background
confidence: mixed
---

# context-memory project vision

## Capture Snapshot

`context-memory` exists to help capture project/topic context right after discussion, before the reasoning evaporates into a few insufficient keywords. The MVP should be an OpenCode skill that asks targeted questions and maintains a simple private git repo of memory items.

## Why This Matters

- After a conversation, the user often understands a project deeply but delays implementation because it is not urgent.
- A few keywords feel sufficient while the context is still in working memory, but become insufficient later.
- Manually deciding how much detail to record is hard and too time-consuming.
- An LLM can reduce capture friction by asking the missing questions that future-you will need answered.

## Current Understanding

### Known

- The initial target is a file-based MVP for OpenCode, not a web service.
- The repo should record many topics and support later progress updates.
- The user wants a workflow that feels like a personal secretary or PM assistant.
- The core problem has been named **architectural knowledge vaporization**.
- The desired workflow has two phases: quick capture first, full enrichment later.

### Inferred

- The first version should optimize for repeat use, not maximal structure.
- The skill should bias toward writing a rough item quickly rather than blocking on a long interview.
- The data format should stay simple enough for git, search, and AI agents to manipulate safely.

### Unknown

- Which exact topics the user will capture most often.
- Whether future records should remain free-form markdown or move toward a stricter schema.
- Whether this should later become a web app, an API-backed service, or remain a repo/skill workflow.

## Decisions And Rationale

### Decisions

- Build the MVP as an OpenCode project-local skill in `.opencode/skills/context-memory/SKILL.md`.
- Add slash commands for capture, enrichment, update, and review.
- Store memory items as markdown files under `memory/YYYY/MM/`.
- Include a small Python CLI for create/list/index/validate operations.
- Keep `docs/research-notes.md` and `docs/methodology.md` as the method memory for future iteration.
- (2026-07-20) Rename the project `context-memory` → `afterthread`, timed ahead of making the GitHub repo public and publishing to PyPI; `context-memory` was judged too generic a name to keep.

### Alternatives Considered

- Start with a web service: deferred because it would add product and infrastructure complexity before proving the capture loop.
- Use one large notebook: rejected because small modular records are easier to update, search, and version.
- Require full enrichment immediately: rejected because high initial friction would reduce usage.
- (2026-07-20) `memoryskein` as the new project name: rejected — hard to spell and pronounce, carries an unwanted association with the Skein hash function, and `memory-*` is already a crowded prefix on PyPI.

### Rationale

- OpenCode officially discovers project-local skills from `.opencode/skills/<name>/SKILL.md`, making a repo-local skill directly usable.
- ADR and design-rationale practices support small records that preserve motivation, alternatives, and consequences.
- A two-stage flow matches the user's time constraint: capture now, enrich later.
- (2026-07-20) `afterthread` = "after" + "thread", punning on "afterthought": once a conversation has gone cold, the goal is to hold on to the thread of reasoning. A PyPI check found `afterthread` and its close variants unclaimed, and a web search turned up no conflicting or confusable existing project.

### Consequences

- The MVP is immediately usable in this repo through OpenCode.
- Record quality depends on the skill following the quick/full distinction.
- Some future migration may be needed if the repo becomes a web-backed product.
- (2026-07-20) Rename scope: GitHub repo (`birdhackor/afterthread`; old repo URL auto-redirects), PyPI distribution and console script `afterthread`, Python package `afterthread/`, environment variables `AFTERTHREAD_{HOST,PORT,DATA_DIR}`, data directory `~/.local/share/afterthread`, database file `afterthread.db`, the OpenCode skill and commands (`cm-*` → `aft-*`), the documentation site (https://birdhackor.github.io/afterthread/), and `scripts/afterthread.py`.
- (2026-07-20) Data migration: in packaged mode, on its next launch the app auto-renames the old default data directory and old DB file in place; it does not merge into or overwrite any path that already exists under the new name. **SUPERSEDED the same day — see the next bullet; no migration ships.**
- (2026-07-20, supersedes the above) Legacy-data auto-migration was built, reviewed twice, and then REMOVED entirely; the rename ships with no migration at all. Why: this tool had never been installed in packaged mode (no `~/.local/share/context-memory`, no dev DB), so the machinery migrated data that did not exist. Moving a SQLite database safely is genuinely hard — it is a file SET (main + `-wal`/`-shm`/`-journal`), so a naive rename can drop committed transactions; the two review rounds kept surfacing real residual risks (live-writer races, TOCTOU on the no-clobber check, a fail-open path that would have booted an empty app beside real data). For a single-user self-use tool the honest trade was to delete the feature rather than keep hardening it: starting over costs nothing here, and the removed code was pure risk surface. Verified by diffing the final `cli.py` against the pre-rename file with the rename applied — byte-identical. Generalizable lesson: when a defensive feature's failure modes are harder than the problem it solves, check whether the problem is real before hardening further.

## Constraints And Assumptions

### Constraints

- The initial version should be a private git repo.
- The MVP should be usable without a database or server.
- The skill should avoid long mandatory interviews during quick capture.

### Assumptions

- OpenCode will be run from the repo root or a subdirectory within the git worktree.
- The user is comfortable letting an agent edit markdown files.
- Markdown plus git is enough for the first useful version.

## Risks

- The skill may ask too many questions and become annoying.
- Records may become too verbose and costly to maintain.
- The agent may infer too much; confidence markers are needed.
- If `memory/INDEX.md` is not regenerated, review workflows may miss new records.

## Evidence And Links

- Project background supplied by the user on 2026-07-09.
- OpenCode skill and command docs are summarized in `docs/research-notes.md`.
- Method translation is documented in `docs/methodology.md`.

## Open Questions

- What exact prompts should become muscle memory for the user?
- How should records be tagged once the repo contains many topics?
- Should there be a weekly review command later?
- Should the CLI eventually support search and stale-item detection?

## Next Actions

- Use `/cm-capture` after the next real project discussion.
- Use `/cm-enrich` on any quick captures before the end of the day.
- After a few real records, review whether the template is too heavy or too light.

## Recovery Cues

- Keywords: architectural knowledge vaporization, quick capture, full enrichment, ADR, design rationale, OpenCode skill, personal PM.
- People: user; future collaborators mentioned per item.
- Files or systems: `.opencode/skills/context-memory/SKILL.md`, `.opencode/commands/`, `memory/`, `docs/methodology.md`.
- Resume trigger: after the first real memory item feels either too slow, too shallow, or hard to resume from.
- (2026-07-20) New keywords: afterthread, rename, afterthought, memoryskein (rejected alternative). New files or systems: `.opencode/skills/afterthread/SKILL.md`, `scripts/afterthread.py`, `afterthread/` package, `afterthread.db`.

## Progress Log

- 2026-07-09: Project background captured and MVP structure created.
- 2026-07-20: Decision recorded — project renamed `context-memory` → `afterthread` (see Decisions And Rationale for name origin, the rejected `memoryskein` alternative, rename scope, and data-migration behavior).

## Enrichment Checklist

- [x] Background and trigger
- [x] Stakeholders and knowledge sources
- [x] Current state
- [x] Desired outcome
- [x] Decisions/proposals/rejections/deferred items
- [x] Rationale
- [x] Alternatives
- [x] Constraints
- [x] Assumptions
- [x] Risks
- [x] Evidence and links
- [x] Next actions
- [x] Recovery cues
