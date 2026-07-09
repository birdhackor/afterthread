---
description: Quickly capture a context-memory item
---

Use the `context-memory` skill to perform QUICK CAPTURE for this input:

```text
$ARGUMENTS
```

Create a memory item in `memory/YYYY/MM/`.

Rules:

- Ask at most three questions before writing, and only when the missing answer is likely to evaporate.
- If the title is implicit, infer one.
- Use `python3 scripts/context_memory.py new --title "..." --summary "..."` to create the file, then edit it with the captured details.
- Mark incomplete but important items as `needs-enrichment`; otherwise keep `capture-quick`.
- Run `python3 scripts/context_memory.py validate` and `python3 scripts/context_memory.py index`.
- Reply to the user in Traditional Chinese with the created path and remaining enrichment questions.

