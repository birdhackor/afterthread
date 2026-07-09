# context-memory

## Communication

- Reason internally in English when helpful.
- Communicate with the user in Traditional Chinese unless the user explicitly asks for another language.

## Project Purpose

This repo is a personal context-memory system for preventing architectural knowledge vaporization: the loss of project reasoning, decision context, tradeoffs, and next-action memory after conversations have gone cold.

## Operating Rules

- Prefer the OpenCode skill in `.opencode/skills/context-memory/SKILL.md` for capture, enrichment, update, and review workflows.
- Keep records small, durable, and easy to resume from.
- Use `python3 scripts/context_memory.py validate` after editing memory records.
- Use `python3 scripts/context_memory.py index` after adding or materially updating records.
- Do not convert the repo into an application until the file-based MVP has proven useful.

