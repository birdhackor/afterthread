"""Unit tests for the budgeted item serializer (app.services.memory_ai).

An enrich / assist-update prompt embeds a snapshot of the whole item. Serialized
naively, an item with 19 sections of up to 20k characters each becomes a ~380k
character prompt that a small-context model rejects on every call -- a permanent
502. ``_serialize_item_for_prompt`` bounds that snapshot to a configurable
character budget: the metadata header is always in full, and the free-text
sections share the remainder, truncated behind an explicit marker.

These pin the two guarantees the router relies on -- an under-budget item is
rendered verbatim, and an oversized item is bounded with every non-empty section
still represented and visibly marked as truncated.
"""

from app.models import MemoryStage, MemoryStatus
from app.services.memory_ai import (
    _HEADER_TAGS_MAX,
    _TITLE_MAX,
    _TRUNCATION_MARKER,
    SECTION_FIELD_ORDER,
    _serialize_item_for_prompt,
)

_BUDGET = 32000


def _section_line(serialized: str, field: str) -> str:
    """Return the value rendered on ``field``'s line in the serialized item."""
    prefix = f"{field}: "
    for line in serialized.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    raise AssertionError(f"no line for section {field!r}")


def test_header_is_rendered_in_full() -> None:
    item = {
        "title": "決策紀錄",
        "status": MemoryStatus.active,
        "stage": MemoryStage.full,
        "tags": ["架構", "成本"],
        "snapshot": "簡短快照",
    }
    out = _serialize_item_for_prompt(item, _BUDGET)
    assert "title: 決策紀錄" in out
    # StrEnum members render as their wire value, not "MemoryStatus.active".
    assert "status: active" in out
    assert "stage: full" in out
    # Tags are comma-joined onto a single header line.
    assert "tags: 架構, 成本" in out


def test_empty_header_fields_render_placeholder() -> None:
    out = _serialize_item_for_prompt({"title": "T"}, _BUDGET)
    assert "status: (empty)" in out
    assert "stage: (empty)" in out
    assert "tags: (empty)" in out


def test_under_budget_item_is_rendered_verbatim() -> None:
    item = {
        "title": "T",
        "snapshot": "一段快照內容",
        "decisions": "採用方案 A",
        "risks": "風險 X",
    }
    out = _serialize_item_for_prompt(item, _BUDGET)
    # Every provided section appears in full, and nothing is marked truncated.
    assert _section_line(out, "snapshot") == "一段快照內容"
    assert _section_line(out, "decisions") == "採用方案 A"
    assert _section_line(out, "risks") == "風險 X"
    assert _TRUNCATION_MARKER not in out
    assert len(out) <= _BUDGET


def test_empty_sections_render_placeholder_not_dropped() -> None:
    out = _serialize_item_for_prompt({"title": "T", "snapshot": "s"}, _BUDGET)
    # The full checklist structure is preserved: an unfilled section still shows.
    assert _section_line(out, "why_matters") == "(empty)"
    assert _section_line(out, "decisions") == "(empty)"


def _oversized_item() -> dict[str, object]:
    item: dict[str, object] = {
        "title": "T",
        "status": MemoryStatus.active,
        "stage": MemoryStage.full,
        "tags": ["a"],
    }
    for field in SECTION_FIELD_ORDER:
        item[field] = "字" * 20000
    return item


def test_oversized_item_is_bounded_by_budget() -> None:
    out = _serialize_item_for_prompt(_oversized_item(), _BUDGET)
    assert len(out) <= _BUDGET


def test_oversized_item_keeps_every_nonempty_section_represented() -> None:
    out = _serialize_item_for_prompt(_oversized_item(), _BUDGET)
    for field in SECTION_FIELD_ORDER:
        # Each competing section still gets real content (the min-keep floor), not
        # dropped to nothing so a few large sections monopolize the budget.
        assert len(_section_line(out, field)) > 0, field


def test_oversized_item_marks_truncated_sections() -> None:
    out = _serialize_item_for_prompt(_oversized_item(), _BUDGET)
    assert _TRUNCATION_MARKER in out
    # Every truncated section carries the marker so the model knows it was cut.
    for field in SECTION_FIELD_ORDER:
        assert _section_line(out, field).endswith(_TRUNCATION_MARKER), field


def test_budget_is_honored_at_the_minimum_setting() -> None:
    # Even at the smallest allowed budget (config lower bound), the total stays
    # within budget and every section is still represented.
    out = _serialize_item_for_prompt(_oversized_item(), 4000)
    assert len(out) <= 4000
    for field in SECTION_FIELD_ORDER:
        assert len(_section_line(out, field)) > 0, field


def test_serialization_is_deterministic() -> None:
    item = _oversized_item()
    assert _serialize_item_for_prompt(item, _BUDGET) == _serialize_item_for_prompt(item, _BUDGET)


# --- defensive header caps (title/tags), independent of any CRUD-schema bound ---


def _oversized_header_item() -> dict[str, object]:
    """An item whose title/tags are pathologically large -- as if written
    directly against the database, or predating schemas.MemoryItemCreate/
    Update's own title/tags bounds -- bypassing CRUD-schema validation
    entirely. This dict is handed straight to the serializer, exactly as
    routers.ai._snapshot_item_for_ai does with a real ORM row's attributes.
    """
    item: dict[str, object] = {
        "title": "T" * 5000,
        "status": MemoryStatus.active,
        "stage": MemoryStage.full,
        "tags": [f"tag{i}" * 20 for i in range(200)],
    }
    for field in SECTION_FIELD_ORDER:
        item[field] = "字" * 20000
    return item


def test_oversized_header_is_bounded_by_budget() -> None:
    # Even a pathologically oversized title/tags -- on top of every section
    # already maxed out -- can never push the total past budget: the header is
    # defensively capped independent of the CRUD-schema bound or the section
    # budgeting below it.
    out = _serialize_item_for_prompt(_oversized_header_item(), _BUDGET)
    assert len(out) <= _BUDGET


def test_oversized_header_is_bounded_even_at_minimum_budget() -> None:
    out = _serialize_item_for_prompt(_oversized_header_item(), 4000)
    assert len(out) <= 4000


def test_oversized_title_is_capped_and_marked() -> None:
    out = _serialize_item_for_prompt(_oversized_header_item(), _BUDGET)
    title_line = _section_line(out, "title")
    assert len(title_line) <= _TITLE_MAX
    assert title_line.endswith(_TRUNCATION_MARKER)


def test_oversized_tags_line_is_capped_and_marked() -> None:
    out = _serialize_item_for_prompt(_oversized_header_item(), _BUDGET)
    tags_line = _section_line(out, "tags")
    assert len(tags_line) <= _HEADER_TAGS_MAX
    assert tags_line.endswith(_TRUNCATION_MARKER)


def test_oversized_header_still_leaves_sections_represented() -> None:
    # The header eating its own fixed cap must not starve the sections down to
    # nothing -- every section still gets the min-keep floor's worth of content.
    out = _serialize_item_for_prompt(_oversized_header_item(), _BUDGET)
    for field in SECTION_FIELD_ORDER:
        assert len(_section_line(out, field)) > 0, field
