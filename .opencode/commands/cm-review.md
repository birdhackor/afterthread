---
description: Review context-memory items and suggest next actions
---

Use the `context-memory` skill to review current memory items.

Current records:

!`python3 scripts/context_memory.py list`

Inspect `memory/INDEX.md` and any item that appears active, waiting, capture-quick, or needs-enrichment.

Return a concise Traditional Chinese agenda:

- items that need enrichment
- active next actions
- waiting/blocked items
- stale or parked items worth revisiting
