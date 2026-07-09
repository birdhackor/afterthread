---
description: Update progress or decisions in a memory item
---

Use the `context-memory` skill to update an existing memory item with:

```text
$ARGUMENTS
```

Find the target item from the path/title/keywords. Preserve prior rationale; do not erase old decisions unless they were simple mistakes. If something changed, mark the old view as superseded or revised and append a dated `Progress Log` entry.

Run:

```bash
python3 scripts/context_memory.py validate
python3 scripts/context_memory.py index
```

Reply in Traditional Chinese with the updated path and the next action now recorded.

