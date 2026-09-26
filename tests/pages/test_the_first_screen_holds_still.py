"""The first screen, laid out by a real browser: nothing moves while the verdict lands, and a phone never scrolls sideways.

The demonstration beside the promise is drawn before its answer comes, and the
answer can take seconds. Whatever the width, the proof strip under it must be
where it was when the verdict lands, when a recorded run stands in, and while
the moment plays again; on a phone the page must not scroll sideways, the
demonstration's bar must keep to one line whatever its label says, and the
connection controls' summary must leave its chevron room.

These open the page as the stack serves it in headless Chrome, Chromium or
Edge through _headless.py, with no network, and skip where none is installed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from _browser import ROOT
from _headless import measure

LIVE = {
    "status": "BLOCKED_BOUNDARY_VIOLATION",
    "reason": (
        "Clean Architecture violation: Layering rule 'python-domain-stays-pure' refuses this write: A Python file under "
        "domain/ may not import infrastructure or a driver. 'src/domain/user.py' imports 'boto3', which matches 'boto3'"
    ),
    "session_id": "sim-hero", "session_tripped": False, "explanation_source": "deterministic",
    "suggested_fix": {
        "kind": "layering", "validated": True,
        "summary": "Checked fix: move boto3 out of the domain behind UserPort; adapter: src/infrastructure/user_adapter.py.",
        "steps": ["Write the domain file below."],
        "writes": [
            {"path": "src/domain/user.py", "content": "class UserPort: ...\n"},
            {"path": "src/infrastructure/user_adapter.py", "content": "import boto3\n", "new_file": True},
        ],
        "checks": [{"gate": g, "path": p, "passed": True} for p in ("src/domain/user.py", "src/infrastructure/user_adapter.py")
                   for g in ("layering", "credential", "boundary", "syntax")],
    },
}
OVERVIEW = {
    "window_days": 7,
    "totals": {"calls": 1284, "approved": 1190, "refused": 37, "would_refuse": 57, "needs_review": 3, "false_alarms": 0, "projects": 8, "agents": 3},
    "series": [{"day": f"2026-09-2{i}", "approved": 100 + i, "observed": 5 + i, "refused": 3 + i} for i in range(7)],
    "sources": {"fleet": {"calls": 1102, "projects": 6}, "sandbox": {"calls": 120, "projects": 9}, "other": {"calls": 62, "projects": 3}},
}
PROOF = json.loads((ROOT / "src" / "threefold" / "web" / "proof.json").read_text(encoding="utf-8"))

HEROES = {
    # A round trip of three digits, as the real stack's usually is.
    "live": {"status": 200, "body": LIVE, "delay": 450, "measure": "waiting"},
    "recorded": {"network": True, "delay": 450, "measure": "waiting"},
}

PROBE = r"""() => {
  const box = s => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect(); return { top: b.top + scrollY, left: b.left, right: b.right, width: b.width, height: b.height }; };
  const shown = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const summary = document.querySelector('#connection-panel > summary');
  return {
    phase: document.getElementById('hero-demo').getAttribute('data-phase'),
    source: document.getElementById('hero-source').textContent.replace(/\s+/g, ' ').trim(),
    proof: box('#proof'), demo: box('#hero-demo'), bar: box('.tf-demo-bar'),
    barItems: Array.from(document.querySelectorAll('.tf-demo-bar > *')).filter(shown).map(e => { const b = e.getBoundingClientRect(); return { middle: b.top + b.height / 2, right: b.right }; }),
    title: box('#hero-demo-title'),
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
    summary: box('#connection-panel > summary'), summaryPadding: parseFloat(getComputedStyle(summary).paddingRight),
    chevron: parseFloat(getComputedStyle(summary, '::after').width), gap: parseFloat(getComputedStyle(summary).columnGap) || 0,
    words: box('.tf-connection-name'), chip: box('#statusRouteBadge'),
    lineHeight: parseFloat(getComputedStyle(summary).lineHeight)
  };
}"""


def _replies(hero: dict) -> dict:
    return {
        "/status": {"status": 200, "body": {"service": "Threefold", "status": "HEALTHY"}, "delay": 176},
        "/api/overview": {"status": 200, "body": OVERVIEW, "delay": 300},
        "/proof.json": {"status": 200, "body": PROOF, "delay": 200},
        "POST /evaluate-tool-call": hero,
    }


@pytest.mark.parametrize("answer", sorted(HEROES))
@pytest.mark.parametrize("width,height", [(375, 812), (768, 1024), (1440, 900)])
def test_nothing_under_the_demonstration_moves_while_its_verdict_lands(tmp_path: Path, width: int, height: int, answer: str) -> None:
    got = measure("index.html", tmp_path, width=width, height=height, replies=_replies(HEROES[answer]),
                  moments={"landed": 3200}, probe=PROBE)
    waiting, landed = got["taken"]["waiting"], got["taken"]["landed"]
    assert waiting["phase"] == "waiting" and landed["phase"] == "landed", "Measured once while it asked and once after it landed"
    assert abs(landed["proof"]["top"] - waiting["proof"]["top"]) < 0.5, \
        f"The proof strip moved {landed['proof']['top'] - waiting['proof']['top']:.1f} px at {width} px when the {answer} verdict landed"
    assert abs(landed["demo"]["height"] - waiting["demo"]["height"]) < 0.5, "The demonstration is as tall before its verdict as after"


def test_replay_moves_nothing_and_asks_nothing(tmp_path: Path) -> None:
    # Motion on, so there is a Replay; it is pressed once the verdict is in.
    press = r"""
document.addEventListener('DOMContentLoaded', () => setTimeout(() => document.getElementById('hero-replay').click(), 2600));
"""
    got = measure("index.html", tmp_path, width=375, height=812, replies=_replies(HEROES["live"]),
                  moments={"landed": 2500, "replaying": 2900, "again": 4600}, probe=PROBE, before=press, reduced_motion=False)
    taken = got["taken"]
    assert [taken[m]["phase"] for m in ("landed", "replaying", "again")] == ["landed", "waiting", "landed"]
    for moment in ("replaying", "again"):
        assert abs(taken[moment]["proof"]["top"] - taken["landed"]["proof"]["top"]) < 0.5, f"The proof strip moved while replaying ({moment})"
    asks = [a for a in got["asked"] if a.startswith("POST ") and a.endswith("/evaluate-tool-call")]
    assert len(asks) == 1, f"Replay sent a call: {got['asked']}"


KEPT = r"""
localStorage.setItem('threefold-hero-answer', JSON.stringify({
  base: location.origin + '/prod', at: Date.now() - 12 * 60000, ms: 212, status: 'BLOCKED_BOUNDARY_VIOLATION',
  reason: "Clean Architecture violation: Layering rule 'python-domain-stays-pure' refuses this write: A Python file under domain/ may not import infrastructure or a driver. 'src/domain/user.py' imports 'boto3', which matches 'boto3'",
  fix: { summary: 'Checked fix: move boto3 out of the domain behind UserPort; adapter: src/infrastructure/user_adapter.py.', validated: true,
    checks: [{ passed: true }, { passed: true }], writes: [{ path: 'src/domain/user.py', new_file: false }] }
}));
"""


@pytest.mark.parametrize("label", ["live", "kept", "recorded"])
def test_a_phone_keeps_the_demonstrations_bar_to_one_line_and_never_scrolls_sideways(tmp_path: Path, label: str) -> None:
    hero = HEROES["recorded" if label == "recorded" else "live"]
    got = measure("index.html", tmp_path, width=375, height=812, replies=_replies(hero), moments={"landed": 3200}, probe=PROBE,
                  before=KEPT if label == "kept" else "")
    landed = got["taken"]["landed"]
    assert landed["source"] == {"live": "Live · 450 ms", "kept": "Live · 12 min ago", "recorded": "Recorded 2026-09-25, replayed offline"}[label]
    middles = [item["middle"] for item in landed["barItems"]]
    assert max(middles) - min(middles) <= 2, f"The bar broke onto a second line with the {label} label: {landed['barItems']}"
    assert all(item["right"] <= landed["bar"]["right"] + 0.5 for item in landed["barItems"]), "Nothing in the bar reaches past its edge"
    assert landed["scrollWidth"] <= landed["clientWidth"], f"A 375 px page scrolls sideways by {landed['scrollWidth'] - landed['clientWidth']} px"


def test_the_connection_summary_leaves_its_chevron_room_on_a_phone(tmp_path: Path) -> None:
    got = measure("index.html", tmp_path, width=375, height=812, replies=_replies(HEROES["live"]), moments={"landed": 3200}, probe=PROBE)
    landed = got["taken"]["landed"]
    summary, words, chip = landed["summary"], landed["words"], landed["chip"]
    room = summary["left"] + summary["width"] - landed["summaryPadding"] - landed["chevron"] - landed["gap"]
    assert words["right"] <= room + 0.5 and chip["right"] <= room + 0.5, "The chevron keeps its own room at the edge"
    assert words["height"] <= landed["lineHeight"] * 1.6, "The summary's words keep to one line; the chip goes under them"
    assert landed["chevron"] >= 8
