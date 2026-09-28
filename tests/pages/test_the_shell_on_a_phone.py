"""The shell and the operator's tiles on a phone, measured in a real browser.

A judges' review at 375 px found the full-screen menu letting Tab reach the
controls behind it, and the tiles' sparklines running out of their cards.
The menu is the design system's, so every page wears it: while it is open,
the page under it is inert and Tab wraps within the header that holds it,
and closing it gives the focus back to the button that opened it. A tile's
sparkline gives way to its value and stays inside its card.

These open the pages as the stack serves them in headless Chrome, Chromium or
Edge through _headless.py, with no network, and skip where none is installed.
What a font decides (how many lines a word takes) is not measured here.
Everything named is synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from _headless import measure

WHOAMI = {"authenticated": False, "via": None, "expires_at": None, "reads_public": True, "sandbox_writes": True}

MENU_PROBE = r"""() => {
  const button = document.querySelector('[data-tf-menu]');
  const sheet = document.getElementById('tf-menu');
  const header = sheet.closest('header');
  const name = el => !el ? 'nothing' : el === document.body ? 'body' : (el.getAttribute('aria-label') || el.textContent || el.tagName).trim().slice(0, 40);
  const key = (k, shift) => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: k, shiftKey: !!shift, bubbles: true, cancelable: true }));
  const inert = () => Array.from(document.body.children).filter(c => c.hasAttribute('inert')).map(c => c.tagName.toLowerCase() + (c.id ? '#' + c.id : c.className ? '.' + String(c.className).split(' ')[0] : ''));
  const reachable = () => Array.from(header.querySelectorAll('a[href], button:not([disabled]), input, select, [tabindex]:not([tabindex="-1"])'))
    .filter(n => n.getClientRects().length > 0);
  button.focus();
  button.click();
  const opened = { open: !sheet.classList.contains('hidden'), focus: name(document.activeElement), inSheet: sheet.contains(document.activeElement), inert: inert(),
    mainInert: document.querySelector('main').closest('[inert]') !== null };
  const nodes = reachable();
  nodes[nodes.length - 1].focus();
  const last = name(document.activeElement);
  key('Tab');
  const wrapped = { from: last, to: name(document.activeElement), first: name(nodes[0]) };
  key('Tab', true);
  const back = name(document.activeElement);
  // Focus that somehow left the header is brought back to it by the next Tab.
  document.body.focus();
  key('Tab');
  const returned = header.contains(document.activeElement);
  sheet.querySelector('a').focus();
  key('Escape');
  const closed = { open: !sheet.classList.contains('hidden'), focus: document.activeElement === button, inert: inert(), expanded: button.getAttribute('aria-expanded') };
  return { opened, wrapped, back, last, returned, closed };
}"""


@pytest.mark.parametrize("page, hash_", [("sessions.html", ""), ("dashboard.html", "#/overview")])
def test_the_menu_sheet_holds_the_focus_while_it_covers_the_page(tmp_path: Path, page: str, hash_: str) -> None:
    result = measure(
        page,
        tmp_path / page.replace(".", "-"),
        width=375,
        height=812,
        replies={"/api/auth/whoami": {"status": 200, "body": WHOAMI}},
        moments={"menu": 1500},
        probe=MENU_PROBE,
        before="window.tailwind = {};" + (f"history.replaceState(null, '', {hash_!r});" if hash_ else ""),
    )
    got = result["taken"]["menu"]
    opened = got["opened"]
    assert opened["open"] and opened["inSheet"], "The menu opens with the focus on its first link"
    assert opened["mainInert"], "The page under the sheet is inert while it is open"
    assert "header.tf-header" not in opened["inert"], "The header, which holds the sheet and its button, stays live"
    wrapped = got["wrapped"]
    assert wrapped["to"] == wrapped["first"], f"Tab from the last control in the header wraps to its first ({wrapped})"
    assert got["back"] == got["last"], "Shift+Tab from the first control wraps to the last"
    assert got["returned"], "Tab never leaves for the page behind"
    closed = got["closed"]
    assert not closed["open"] and closed["focus"] and closed["expanded"] == "false", "Escape closes it and gives the focus back to its button"
    assert closed["inert"] == [], "Closed, nothing the menu made inert is left so"


OVERVIEW = {
    "window_days": 7, "generated_at": "2026-09-28T04:00:00+00:00", "source": "rollups",
    "totals": {"calls": 1108, "approved": 991, "refused": 4, "would_refuse": 113, "needs_review": 47, "false_alarms": 2,
               "projects": 7, "agents": 3, "coding_agents": 3},
    "series": [{"day": f"2026-09-{d:02d}", "approved": 100 + d, "observed": 10 + d % 4, "refused": d % 3} for d in range(22, 29)],
    "by_agent": [{"agent": "claude-code", "calls": 500, "kind": "coding_agent"}, {"agent": "codex", "calls": 400, "kind": "coding_agent"},
                 {"agent": "antigravity", "calls": 208, "kind": "coding_agent"}],
    "by_origin": [{"origin": "hook", "calls": 1108}],
    "by_rule": [{"rule_key": "PROTECTED_PATH", "refused": 2, "would_refuse": 40}],
    "by_project": [{"project": "Acme-Payments", "stage": "observe", "configured": True, "source": "fleet", "calls": 195, "refused": 1,
                    "would_refuse": 20, "needs_review": 5, "last_seen": "2026-09-28T04:00:00+00:00"}],
    "stages": {"observe": 4, "enforce": 3},
}

SPARK_PROBE = r"""() => {
  const out = [];
  document.querySelectorAll('.tf-tile').forEach(tile => {
    const spark = tile.querySelector('.tf-tile-spark svg');
    if (!spark) return;
    const t = tile.getBoundingClientRect();
    const s = spark.getBoundingClientRect();
    const v = tile.querySelector('.tf-tile-value').getBoundingClientRect();
    out.push({ label: tile.querySelector('.tf-tile-label').textContent.trim(), tileRight: Math.round(t.right), sparkRight: Math.round(s.right),
      sparkLeft: Math.round(s.left), valueRight: Math.round(v.right), width: Math.round(s.width) });
  });
  return { tiles: out, scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth };
}"""


def test_a_tile_s_sparkline_stays_inside_its_card_at_375_px(tmp_path: Path) -> None:
    result = measure(
        "dashboard.html",
        tmp_path,
        width=375,
        height=812,
        replies={"/api/auth/whoami": {"status": 200, "body": WHOAMI}, "/api/overview": {"status": 200, "body": OVERVIEW}},
        moments={"drawn": 2000},
        probe=SPARK_PROBE,
        before="window.tailwind = {}; history.replaceState(null, '', '#/overview');",
    )
    got = result["taken"]["drawn"]
    assert len(got["tiles"]) >= 3, "The calls, refused and would-refuse tiles each draw a sparkline"
    for tile in got["tiles"]:
        assert tile["sparkRight"] <= tile["tileRight"], f"{tile['label']}: the sparkline runs out of its card ({tile})"
        assert tile["sparkLeft"] >= tile["valueRight"], f"{tile['label']}: the sparkline gives way to the value ({tile})"
    assert got["scrollWidth"] <= got["clientWidth"], "Nothing scrolls sideways at 375 px"
