---
description: Fully enrich an existing afterthread item
---

Use the `afterthread` skill to perform FULL ENRICHMENT for:

```text
$ARGUMENTS
```

If a path is provided, read that file. If only a title or keywords are provided, inspect `memory/INDEX.md` and relevant files.

Fill the anti-vaporization checklist: background, stakeholders, current state, desired outcome, decisions, rationale, alternatives, constraints, assumptions, risks, evidence, next actions, and recovery cues.

Preserve uncertainty with Known/Inferred/Unknown. Append to `Progress Log`, update frontmatter `updated`, set `stage: full` when the checklist is materially complete, and run:

```bash
python3 scripts/afterthread.py validate
python3 scripts/afterthread.py index
```

Reply in Traditional Chinese with what changed and any still-open questions.

