"""Tests for Settings validation in app.config.

`stale_after_days` feeds `timedelta(days=...)` inside
`schemas.MemoryItemRead.is_stale`. An unbounded value (e.g. a fat-fingered
`STALE_AFTER_DAYS=99999999999`) boots fine but then raises `OverflowError`
from that timedelta on every read/commit touching a stale-eligible item.
Bounding the field at construction makes a bad value fail loudly at startup,
via pydantic-settings, instead.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_stale_after_days_lower_bound_accepted() -> None:
    assert Settings(stale_after_days=0).stale_after_days == 0


def test_stale_after_days_upper_bound_accepted() -> None:
    assert Settings(stale_after_days=36500).stale_after_days == 36500


def test_stale_after_days_above_upper_bound_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(stale_after_days=36501)


def test_stale_after_days_negative_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(stale_after_days=-1)
