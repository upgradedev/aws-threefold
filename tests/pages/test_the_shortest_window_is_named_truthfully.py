"""The shortest window the service can answer is a UTC day, and the page says so.

A rollup holds a day's counters, so `days=1` reads one partition: today, from
00:00 UTC. The picker offered it as "24 h" and every sentence around it read
"the last 24 hours", which at 00:05 UTC described five minutes of calls as a
day's worth. The error shrank through the day and never went away.

The server already states the first day it covers (`window_from`, see
tests/unit/test_the_overview_says_which_days_it_covers.py). These hold the page
to the same thing, in the words a reader sees.
"""
from __future__ import annotations

from pathlib import Path

from _browser import run

FIXTURES = r"""
const NOW = new Date().toISOString();
const DAY = n => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);
function overviewBody() {
  return {
    window_days: 1, window_from: DAY(0), generated_at: NOW, source: 'rollups',
    totals: { calls: 12, approved: 10, refused: 1, would_refuse: 1, needs_review: 0, false_alarms: 0, projects: 1, agents: 1 },
    series: [{ day: DAY(0), approved: 10, observed: 1, refused: 1 }],
    by_agent: [{ agent: 'claude-code', calls: 12 }],
    by_origin: [{ origin: 'hook', calls: 12 }],
    by_rule: [{ rule_key: 'java-domain-stays-pure', refused: 1, would_refuse: 1 }],
    by_project: [{ project: 'Acme-Billing', stage: 'observe', configured: true, calls: 12, refused: 1, would_refuse: 1, needs_review: 0, last_seen: NOW }],
    stages: { observe: 1, enforce: 0 }
  };
}
"""


def _overview(tmp_path: Path) -> str:
    out = run(
        "dashboard.html",
        r"""
  answer = (url) => url.indexOf('/api/overview') !== -1
    ? { status: 200, body: overviewBody() }
    : { status: 200, body: {} };
  await visit('#/overview?days=1');
  out.view = view();
""",
        tmp_path,
        before=FIXTURES,
    )
    return out["view"]


def test_the_page_does_not_claim_twenty_four_hours(tmp_path: Path) -> None:
    page = _overview(tmp_path)
    assert "24 hours" not in page
    assert "24 h" not in page


def test_the_page_says_the_window_is_a_utc_day(tmp_path: Path) -> None:
    page = _overview(tmp_path)
    assert "the day so far, UTC" in page


def test_the_picker_offers_the_window_under_the_same_name(tmp_path: Path) -> None:
    """The button's label and the sentence beside it are one claim, made twice."""
    page = _overview(tmp_path)
    assert ">Today<" in page
    assert 'aria-label="the day so far, UTC"' in page
