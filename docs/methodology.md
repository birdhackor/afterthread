# Methodology

## Goal

`afterthread` stores enough context for future-you or an AI agent to resume a topic without replaying the original discussion.

The unit is a **memory item**. A memory item can be a project, topic, decision, plan, investigation, risk, or deferred task. It does not need to be a final architecture decision.

## Two-Stage Workflow

### 1. Quick Capture

Use this immediately after a discussion or when you only have a few minutes.

The agent should create the item even if details are incomplete. Ask at most three questions, and only if the answer is likely to evaporate soon.

Minimum durable fields:

- title
- one-paragraph snapshot
- why this matters
- current understanding
- next action or resume trigger
- open questions
- recovery cues

### 2. Full Enrichment

Use later the same day or before the topic becomes cold.

The agent should fill the anti-vaporization checklist:

- Background: what triggered this topic?
- Stakeholders: who knows what?
- Current state: what is true now?
- Desired outcome: what would count as progress?
- Decisions: what was decided, proposed, rejected, or deferred?
- Rationale: why did the current thinking make sense?
- Alternatives: what options were considered and why were they not selected?
- Constraints: time, budget, compatibility, team, politics, operations.
- Assumptions: what might be false?
- Risks: what could hurt later?
- Evidence: links, files, logs, messages, tickets, conversations.
- Next actions: concrete owner/action/date when known.
- Recovery cues: keywords and facts future-you would search for.

## Confidence Markers

Every item should distinguish:

- `Known`: directly stated or observed.
- `Inferred`: plausible interpretation by the agent.
- `Unknown`: missing but important.

This matters because LLMs are useful for recovering design rationale, but they can also over-complete uncertain gaps.

## Status Values

Use one of these values in frontmatter:

- `capture-quick`: created quickly and still incomplete.
- `needs-enrichment`: important enough to complete soon.
- `active`: current work or live topic.
- `waiting`: blocked by someone else or an external event.
- `parked`: intentionally deferred.
- `done`: no current action, retained for memory.
- `superseded`: replaced by another item.

## File Naming

Memory items live at:

```text
memory/YYYY/MM/YYYY-MM-DD-short-slug.md
```

The date is the capture date, not necessarily the original discussion date.

## Update Style

When updating an item:

- Change frontmatter `updated`.
- Update the current state and next actions.
- Append to `Progress Log` instead of rewriting history away.
- If a decision changes, preserve the old rationale and mark what superseded it.
- Run `python3 scripts/afterthread.py validate`.
- Run `python3 scripts/afterthread.py index`.

