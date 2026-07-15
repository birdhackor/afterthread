"""Tests for the review grouping endpoint."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session

from context_memory.config import get_settings
from context_memory.models import MemoryItem


def _create(client: TestClient, title: str, status: str) -> dict:
    response = client.post("/api/items", json={"title": title, "status": status})
    assert response.status_code == 201, response.text
    return response.json()


def test_review_groups_are_disjoint_and_exclude_terminal(client: TestClient) -> None:
    for status in (
        "capture-quick",
        "needs-enrichment",
        "active",
        "waiting",
        "parked",
        "done",
        "superseded",
    ):
        _create(client, f"item-{status}", status)

    review = client.get("/api/review").json()

    assert {item["title"] for item in review["needs_enrichment"]} == {
        "item-capture-quick",
        "item-needs-enrichment",
    }
    assert [item["title"] for item in review["active"]] == ["item-active"]
    assert [item["title"] for item in review["waiting"]] == ["item-waiting"]
    assert [item["title"] for item in review["parked"]] == ["item-parked"]

    all_titles = {item["title"] for group in review.values() for item in group}
    assert "item-done" not in all_titles
    assert "item-superseded" not in all_titles


def test_review_group_sorted_oldest_first(client: TestClient, session: Session) -> None:
    older = _create(client, "older", "needs-enrichment")
    newer = _create(client, "newer", "capture-quick")

    stored_older = session.get(MemoryItem, older["id"])
    assert stored_older is not None
    stored_older.updated = datetime.now(UTC) - timedelta(days=30)
    session.add(stored_older)
    session.commit()

    group = client.get("/api/review").json()["needs_enrichment"]
    # Oldest updated first = needs attention first.
    assert [item["title"] for item in group] == ["older", "newer"]
    assert newer["id"] > older["id"]


def test_is_stale_flips_past_threshold(client: TestClient, session: Session) -> None:
    item = _create(client, "aging", "active")

    fresh = client.get("/api/review").json()["active"][0]
    assert fresh["is_stale"] is False

    stored = session.get(MemoryItem, item["id"])
    assert stored is not None
    threshold_days = get_settings().stale_after_days
    stored.updated = datetime.now(UTC) - timedelta(days=threshold_days + 1)
    session.add(stored)
    session.commit()

    aged = client.get("/api/review").json()["active"][0]
    assert aged["is_stale"] is True


def test_review_empty_when_no_items(client: TestClient) -> None:
    review = client.get("/api/review").json()
    assert review == {
        "needs_enrichment": [],
        "active": [],
        "waiting": [],
        "parked": [],
    }
