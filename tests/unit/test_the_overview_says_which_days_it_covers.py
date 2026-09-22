"""The overview names the first day it covers, so nothing has to guess at it.

Rollups are daily. `days=1` therefore reads one UTC partition, today's, and at
00:05 UTC "today" is five minutes long. The payload said only `window_days: 1`,
and a reader that turned that into "the last 24 hours" was wrong by however
much of the day had not happened yet. The window now states its own first day,
which is the thing that is true.
"""
from __future__ import annotations

import datetime

from threefold.application.rollups import overview

TODAY = datetime.date(2026, 9, 22)


def _rollups():
    return [
        {"project": "Acme-Core", "day": "2026-09-21", "calls": 4, "approved": 4},
        {"project": "Acme-Core", "day": "2026-09-22", "calls": 2, "approved": 2},
    ]


def test_one_day_covers_today_only_and_says_so() -> None:
    """The reader hands over today's partition alone; the payload names the day."""
    today_only = [item for item in _rollups() if item["day"] == "2026-09-22"]
    payload = overview(today_only, {}, days=1, today=TODAY)
    assert payload["window_days"] == 1
    assert payload["window_from"] == "2026-09-22"
    assert [row["day"] for row in payload["series"]] == ["2026-09-22"]
    assert payload["totals"]["calls"] == 2


def test_a_longer_window_names_its_first_day_too() -> None:
    payload = overview(_rollups(), {}, days=7, today=TODAY)
    assert payload["window_from"] == "2026-09-16"
    assert payload["series"][0]["day"] == "2026-09-16"
    assert payload["series"][-1]["day"] == "2026-09-22"
    assert payload["totals"]["calls"] == 6


def test_the_first_day_is_the_first_day_of_the_series() -> None:
    for days in (1, 2, 14, 30):
        payload = overview([], {}, days=days, today=TODAY)
        assert payload["window_from"] == payload["series"][0]["day"], days
