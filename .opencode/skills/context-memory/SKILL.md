---
name: context-memory
description: Capture, enrich, update, and review personal project memory records that preserve discussion context, decision rationale, tradeoffs, constraints, open questions, next actions, and recovery cues. Use when the user mentions context-memory, architectural knowledge vaporization, saving project/topic context, creating or updating memory items, quick capture, full enrichment, resuming old work, or reviewing parked/active topics.
license: MIT
---

# Context Memory

## Purpose

Help the user prevent architectural knowledge vaporization: losing the reasoning, constraints, alternatives, and next steps behind a topic after the original conversation is no longer fresh.

Communicate with the user in Traditional Chinese unless they request another language.

## Repo Contract

- Memory items live in `memory/YYYY/MM/YYYY-MM-DD-slug.md`.
- Use `templates/memory-item.md` for the canonical item shape.
- Use `docs/methodology.md` for detailed workflow rules when uncertain or changing the method.
- Use `docs/research-notes.md` only when the user asks about the background, survey, or rationale for the method.
- Use `scripts/context_memory.py` for deterministic create/list/index/validate operations.

## Workflow Decision

- If the user just finished a discussion, provides keywords, or asks to "記一下", do **Quick Capture**.
- If the user asks to complete, enrich, 補齊, or make an item durable, do **Full Enrichment**.
- If the user reports progress, changed decisions, new constraints, or follow-up facts, do **Update Existing Item**.
- If the user asks what to work on, what is stale, or what is pending, do **Review Memory**.

## Quick Capture

Goal: create a usable record with minimum friction.

Steps:

1. Infer a concise title from the user input.
2. Ask at most three questions before writing, and only when the answer is likely to evaporate soon.
3. Create the file:

   ```bash
   python3 scripts/context_memory.py new --title "TITLE" --summary "ONE SENTENCE SNAPSHOT"
   ```

4. Edit the generated item with the available context.
5. Mark missing details as `Unknown`; do not invent facts.
6. Set `status: needs-enrichment` if the item is important and incomplete; otherwise keep `capture-quick`.
7. Run:

   ```bash
   python3 scripts/context_memory.py validate
   python3 scripts/context_memory.py index
   ```

8. Tell the user the created path and the smallest useful enrichment prompt.

Prefer writing the record over extended interviewing. The first record can be imperfect.

## Full Enrichment

Goal: make the item recoverable by future-you.

Read the target item, then fill or improve:

- Background and trigger
- Stakeholders and knowledge sources
- Current state
- Desired outcome
- Decisions, proposals, rejected options, and deferred items
- Rationale and consequences
- Constraints and assumptions
- Risks
- Evidence and links
- Open questions
- Next actions
- Recovery cues: keywords, people, files/systems, resume trigger

Use the `Known`, `Inferred`, and `Unknown` subsections honestly. LLM inference is allowed only when labeled as inferred.

When materially complete, set `stage: full` and a suitable status such as `active`, `waiting`, `parked`, or `done`.

## Update Existing Item

1. Locate the item from a path, title, keyword, or `memory/INDEX.md`.
2. Preserve history. Append to `Progress Log` instead of silently replacing prior reasoning.
3. If a decision changes, keep the old rationale and mark it superseded or revised.
4. Update `updated`.
5. Refresh `Next Actions`, `Open Questions`, and `Recovery Cues`.
6. Validate and regenerate the index.

## Review Memory

1. Run `python3 scripts/context_memory.py list`.
2. Inspect `memory/INDEX.md` and relevant active/incomplete records.
3. Group results into:
   - needs enrichment
   - active next actions
   - waiting or blocked
   - parked/stale topics worth revisiting
4. Keep the answer concise and action-oriented.

## Capture Quality Bar

A good memory item lets future-you answer:

- What was the topic?
- Why did it matter?
- What did we believe or decide?
- Why did that reasoning make sense at the time?
- What alternatives were not chosen?
- What is still unknown?
- What should happen next?
- Which keywords, people, files, or systems help resume the thread?

## Safety Against Over-Documentation

Do not turn every item into a long report. Capture enough to resume. Prefer short factual bullets and dated progress log entries over polished prose.
