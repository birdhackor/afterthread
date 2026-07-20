#!/usr/bin/env python3
"""Small helper for afterthread markdown records."""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MEMORY_ROOT = ROOT / "memory"
TEMPLATE = ROOT / "templates" / "memory-item.md"

REQUIRED_FRONTMATTER = [
    "id",
    "title",
    "status",
    "stage",
    "created",
    "updated",
    "tags",
]

REQUIRED_SECTIONS = [
    "## Capture Snapshot",
    "## Why This Matters",
    "## Current Understanding",
    "## Decisions And Rationale",
    "## Open Questions",
    "## Next Actions",
    "## Recovery Cues",
    "## Progress Log",
    "## Enrichment Checklist",
]


def today() -> str:
    return dt.date.today().isoformat()


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:60].strip("-") or "item"


def memory_files() -> list[Path]:
    if not MEMORY_ROOT.exists():
        return []
    return sorted(
        path
        for path in MEMORY_ROOT.rglob("*.md")
        if path.name != "INDEX.md" and path.is_file()
    )


def parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if len(lines) < 3 or lines[0] != "---":
        return {}
    end = None
    for idx, line in enumerate(lines[1:], start=1):
        if line == "---":
            end = idx
            break
    if end is None:
        return {}
    data: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        data[key.strip()] = raw.strip().strip('"')
    return data


def make_record(args: argparse.Namespace) -> int:
    date = args.date or today()
    slug = slugify(args.slug or args.title)
    item_id = f"{date}-{slug}"
    target_dir = MEMORY_ROOT / date[:4] / date[5:7]
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{item_id}.md"
    counter = 2
    while target.exists():
        item_id = f"{date}-{slug}-{counter}"
        target = target_dir / f"{item_id}.md"
        counter += 1

    if not TEMPLATE.exists():
        print(f"missing template: {TEMPLATE}", file=sys.stderr)
        return 2

    summary = args.summary or "Quick capture created. Fill details through OpenCode."
    content = TEMPLATE.read_text(encoding="utf-8").format(
        id=item_id,
        title=args.title.replace('"', '\\"'),
        date=date,
        summary=summary,
    )
    content = content.replace("status: capture-quick", f"status: {args.status}")
    content = content.replace("stage: quick", f"stage: {args.stage}")
    target.write_text(content, encoding="utf-8")
    print(target.relative_to(ROOT))
    return 0


def validate_records(_: argparse.Namespace) -> int:
    failures: list[str] = []
    for path in memory_files():
        text = path.read_text(encoding="utf-8")
        meta = parse_frontmatter(text)
        rel = path.relative_to(ROOT)
        if not meta:
            failures.append(f"{rel}: missing frontmatter")
            continue
        for key in REQUIRED_FRONTMATTER:
            if key not in meta or not meta[key]:
                failures.append(f"{rel}: missing frontmatter field {key}")
        for section in REQUIRED_SECTIONS:
            if section not in text:
                failures.append(f"{rel}: missing section {section}")

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print(f"OK: {len(memory_files())} memory item(s) valid")
    return 0


def write_index(_: argparse.Namespace) -> int:
    MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in memory_files():
        text = path.read_text(encoding="utf-8")
        meta = parse_frontmatter(text)
        rel = path.relative_to(ROOT)
        rows.append(
            (
                meta.get("updated", ""),
                meta.get("status", ""),
                meta.get("stage", ""),
                meta.get("title", path.stem),
                str(rel),
            )
        )
    rows.sort(reverse=True)

    lines = [
        "# Memory Index",
        "",
        f"Generated: {today()}",
        "",
        "| Updated | Status | Stage | Title | Path |",
        "| --- | --- | --- | --- | --- |",
    ]
    for updated, status, stage, title, rel in rows:
        lines.append(f"| {updated} | {status} | {stage} | {title} | `{rel}` |")
    lines.append("")
    (MEMORY_ROOT / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    print("memory/INDEX.md")
    return 0


def list_records(_: argparse.Namespace) -> int:
    for path in memory_files():
        meta = parse_frontmatter(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ROOT)
        print(
            f"{meta.get('updated', 'unknown')} "
            f"[{meta.get('status', 'unknown')}/{meta.get('stage', 'unknown')}] "
            f"{meta.get('title', path.stem)} - {rel}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage afterthread records")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="create a memory item from the template")
    new.add_argument("--title", required=True)
    new.add_argument("--summary", default="")
    new.add_argument("--date", default="")
    new.add_argument("--slug", default="")
    new.add_argument("--status", default="capture-quick")
    new.add_argument("--stage", default="quick")
    new.set_defaults(func=make_record)

    validate = sub.add_parser("validate", help="validate memory items")
    validate.set_defaults(func=validate_records)

    index = sub.add_parser("index", help="regenerate memory/INDEX.md")
    index.set_defaults(func=write_index)

    list_cmd = sub.add_parser("list", help="list memory items")
    list_cmd.set_defaults(func=list_records)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

