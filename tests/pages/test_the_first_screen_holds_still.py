"""The first screen, laid out by a real browser: nothing moves while the verdict lands, and a phone never scrolls sideways.

The demonstration beside the promise is drawn before its answer comes, and the
answer can take seconds. Whatever the width, the proof strip under it must be
where it was when the verdict lands, when a recorded run stands in, and while
the moment plays again; on a phone the page must not scroll sideways, the
demonstration's bar must keep to one line whatever its label says, and the
connection controls' summary must leave its chevron room. Under the scenarios,
the terminal is as tall as its lines, on a desk no taller than the gates beside
it, and there the result's two columns end near each other whatever came back,
a live answer or a recorded replay.

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
    # One line where the page's own font is loaded; a wider fallback may take two, never one word to a line.
    assert words["height"] <= landed["lineHeight"] * 2.6, "The summary's words keep to a line or two; the chip goes under them"
    assert landed["chevron"] >= 8


LAYOUT = r"""() => {
  const box = s => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect(); return { top: b.top + scrollY, bottom: b.bottom + scrollY, left: b.left, right: b.right, height: b.height, width: b.width }; };
  const shown = e => !!e && !e.hidden && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const landed = Array.from(document.querySelectorAll('.tf-demo-landed > *')).filter(shown);
  const outcome = box('.tf-demo-outcome');
  return {
    how: box('#how-it-works'),
    flowShown: Array.from(document.querySelectorAll('#how-it-works svg.tf-diagram-wide, #how-it-works svg.tf-diagram-narrow')).filter(shown).map(e => e.getAttribute('class')),
    connectors: getComputedStyle(document.querySelector('.tf-step-card'), '::before').content,
    slack: outcome.bottom - Math.max.apply(null, landed.map(e => e.getBoundingClientRect().bottom + scrollY)),
    actions: Array.from(document.querySelectorAll('#hero-actions > *')).map(e => { const b = e.getBoundingClientRect(); return { id: e.id, top: b.top, left: b.left, width: b.width }; }),
    names: Array.from(document.querySelectorAll('.tf-scenario-name')).map(e => e.getBoundingClientRect().top),
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth
  };
}"""


# A face much wider than the page's own, as a machine without it may fall back
# to: what holds in the fallback here must hold there too.
WIDE_FONT = r"""
document.addEventListener('DOMContentLoaded', () => {
  const style = document.createElement('style');
  style.textContent = ':root { --tf-font-sans: Verdana, sans-serif !important; }';
  document.head.appendChild(style);
});
"""


def _layout(tmp_path: Path, width: int, height: int, before: str = "") -> dict:
    return measure("index.html", tmp_path, width=width, height=height, replies=_replies(HEROES["live"]), moments={"landed": 3200},
                   probe=LAYOUT, before=before)["taken"]["landed"]


def test_on_a_phone_the_steps_are_the_flow_and_how_it_works_stays_short(tmp_path: Path) -> None:
    """The stacked steps carry the flow in their gaps, so the drawing that would repeat them waits for a wider screen."""
    phone = _layout(tmp_path, 375, 812)
    assert phone["flowShown"] == [], "No second drawing of the three steps under them on a phone"
    assert "the call" in phone["connectors"], "The steps are joined by the call going down"
    # It took 3.3 screens when the drawing repeated the steps; the bound holds in a wide fallback face too.
    for face in ("", WIDE_FONT):
        how = _layout(tmp_path, 375, 812, face)["how"]["height"] if face else phone["how"]["height"]
        assert how < 2.9 * 812, f"How it works takes {how / 812:.1f} phone screens"
    assert phone["scrollWidth"] <= phone["clientWidth"]
    desk = _layout(tmp_path, 1440, 900)
    assert desk["flowShown"] == ["tf-diagram-wide"], "On a desk the steps sit side by side and the one drawing joins them"
    tablet = _layout(tmp_path, 768, 1024)
    assert tablet["flowShown"] == ["tf-diagram-narrow"]


@pytest.mark.parametrize("width,height", [(375, 812), (768, 1024), (1440, 900)])
def test_the_verdict_fills_the_room_its_ghost_held(tmp_path: Path, width: int, height: int) -> None:
    """A verdict shorter than the recorded one leaves no empty band under the card's last box."""
    got = _layout(tmp_path, width, height)
    assert abs(got["slack"]) < 1, f"{got['slack']:.0f} px of empty room under the verdict at {width} px"


@pytest.mark.parametrize("face", ["page", "wide"])
def test_the_hero_actions_never_leave_one_button_alone_on_a_row(tmp_path: Path, face: str) -> None:
    for width, height in ((768, 1024), (1024, 768), (1280, 800), (1440, 900)):
        acts = {a["id"]: a for a in _layout(tmp_path, width, height, WIDE_FONT if face == "wide" else "")["actions"]}
        primary, secondary, link = acts["hero-try"], acts["hero-dashboard"], acts["hero-connect"]
        side_by_side = abs(primary["top"] - secondary["top"]) < 1
        stacked = abs(primary["left"] - secondary["left"]) < 1 and abs(primary["width"] - secondary["width"]) < 1
        assert side_by_side or stacked, f"At {width} px, in the {face} face, the two buttons are neither on one row nor one column"
        assert link["top"] > max(primary["top"], secondary["top"]) and abs(link["left"] - primary["left"]) < 1, \
            f"At {width} px the quiet link has the line under the buttons to itself"


def test_the_scenario_names_line_up_whether_or_not_their_card_is_numbered(tmp_path: Path) -> None:
    names = _layout(tmp_path, 1440, 900)["names"]
    assert max(names) - min(names) < 0.5, f"The adapter's name sits apart from the numbered ones: {names}"


# A live loop refusal as the stack sends it, with the fix that comes with one.
LOOP = {
    "status": "BLOCKED_LOOP_DETECTED",
    "reason": "Monomorphic loop detected: Tool 'edit_file' invoked with identical arguments 3 consecutive times",
    "session_id": "sim-loop-1", "session_tripped": True, "current_session_cost_usd": 0.027, "explanation_source": "deterministic",
    "bedrock_explanation": "Tool 'edit_file' was called with identical arguments once too often, so the session was halted at $0.0270.",
    "suggested_fix": {
        "kind": "loop", "validated": False,
        "summary": "Loop: edit_file on src/service.py was called 3 times with identical arguments. Change the arguments or ask the human.",
        "steps": ["Read the result of the last call.", "Change the arguments or the approach.", "Poll with a read-only command.",
                  "An operator resumes the session."],
    },
}

# What the terminal's lines need, measured by setting its height aside for a
# moment: the lines and the padding, then its border, held between its own
# minimum and cap, both taken as outer heights like the lines, since its box
# is content-box. A terminal stretched to its cell is taller than that. The
# room is the well it sits in, which on a desk is what the gates' cell leaves
# beside it. A column ends where its last box does: the sent one at the
# terminal's border, the other at the session's spend.
TERMINAL = r"""() => {
  const box = e => e.getBoundingClientRect();
  const t = document.getElementById('terminal-log');
  const style = getComputedStyle(t);
  const px = name => parseFloat(style[name]);
  const kept = [t.style.minHeight, t.style.height];
  t.style.minHeight = '0px'; t.style.height = '0px';
  const lines = t.scrollHeight + px('borderTopWidth') + px('borderBottomWidth');
  t.style.minHeight = kept[0]; t.style.height = kept[1];
  const frame = px('paddingTop') + px('paddingBottom') + px('borderTopWidth') + px('borderBottomWidth');
  const least = px('minHeight') + frame;
  const need = Math.min(Math.max(lines, least), px('maxHeight') + frame);
  const grid = document.querySelector('.tf-result-grid');
  const cells = Array.from(grid.children);
  const end = cell => { const shown = Array.from(cell.children).filter(e => e.getClientRects().length); return shown.length ? box(shown[shown.length - 1]).bottom - box(grid).top : 0; };
  const fix = document.getElementById('fix-container'), cert = document.getElementById('cert-container');
  return { client: t.clientHeight, scroll: t.scrollHeight, height: box(t).height, need: need, least: least, room: box(t.parentElement).height,
    newestInView: t.scrollTop + t.clientHeight >= t.scrollHeight - 1, lines: t.children.length,
    sentEnd: box(t).bottom - box(grid).top, judgedEnd: end(cells[1]), sideBySide: Math.abs(box(cells[0]).top - box(cells[1]).top) < 1,
    fixShown: !fix.classList.contains('hidden'), fixWidth: box(fix).width, certShown: !cert.classList.contains('hidden'),
    certWidth: box(cert).width, gridWidth: box(grid).width, badge: document.getElementById('terminal-badge').textContent,
    spend: document.getElementById('kpi-spend').textContent, verdict: document.getElementById('verdict-tag').textContent,
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth };
}"""

# The loop, then the boundary write with its checked fix of two files, then the
# compliant run and its certificate, each pressed once the one before has landed.
SCENARIOS = r"""
document.addEventListener('DOMContentLoaded', () => {
  setTimeout(() => simulateLoop(), 2000);
  setTimeout(() => simulateBoundary(), 3600);
  setTimeout(() => simulateCompliant(), 5200);
});
"""
SCENARIO_MOMENTS = {"idle": 1800, "loop": 3400, "boundary": 5000, "certificate": 6600}


def _scenario_replies(answer: str) -> dict:
    """The stack's answers to the hero and the scenarios, measured only at the moments above.

    Recorded, the stack answers /status and then no scenario's call, so each
    replays its recorded run: the loop's eight lines, a boundary with no fix,
    and the recorded certificate.
    """
    hero = {key: value for key, value in HEROES[answer].items() if key != "measure"}
    loop = {"status": 200, "body": LOOP, "delay": 120} if answer == "live" else {"network": True, "delay": 120}
    return dict(_replies(hero), **{"POST /simulate-loop": loop})


# What each answer's loop leaves in the terminal and the gates, so a test knows
# it measured the answer it meant to: the live one's four lines, or the
# recorded run's eight.
LOOP_LANDED = {
    "live": {"lines": 4, "badge": "Live answer", "spend": "$0.0270"},
    "recorded": {"lines": 8, "badge": "Recorded replay", "spend": "$0.0270"},
}


@pytest.mark.parametrize("answer", sorted(LOOP_LANDED))
@pytest.mark.parametrize("width,height", [(375, 812), (768, 1024), (1024, 768), (1100, 900), (1440, 900)])
def test_the_terminal_is_as_tall_as_its_lines(tmp_path: Path, width: int, height: int, answer: str) -> None:
    """A review found the idle guide cut off on a phone, and four lines in a 600 px black pane on a desk.

    The terminal now shows the whole idle guide at every width, keeps that
    height when a scenario's first lines replace it, and is never stretched
    past what its lines need, whatever sits beside it. On a desk it is also
    never taller than the room the gates' cell leaves it (a later review found
    the recorded loop's lines leaving that cell an empty field), and never
    shorter than its own minimum; past that room its lines scroll, the newest
    in view.
    """
    got = measure("index.html", tmp_path, width=width, height=height, replies=_scenario_replies(answer),
                  moments=SCENARIO_MOMENTS, probe=TERMINAL, before=SCENARIOS)
    taken = got["taken"]
    idle, loop = taken["idle"], taken["loop"]
    assert idle["scroll"] <= idle["client"], f"At {width} px the idle guide is cut off: {idle['scroll']} px of lines in {idle['client']} px"
    landed = LOOP_LANDED[answer]
    assert (loop["lines"], loop["badge"], loop["spend"]) == (landed["lines"], landed["badge"], landed["spend"]), \
        f"Measured after the {answer} loop landed"
    if answer == "live":
        assert abs(loop["height"] - idle["height"]) < 0.5, "A scenario's first lines neither shrink nor stretch the pane"
    for moment, at in taken.items():
        if width < 1024:
            assert abs(at["height"] - at["need"]) < 1, \
                f"At {width} px ({moment}) the terminal is {at['height']:.0f} px where its lines need {at['need']:.0f}: stretched to its cell"
        else:
            assert at["room"] >= at["least"] - 1, f"At {width} px ({moment}) the terminal has less room than its minimum"
            assert abs(at["height"] - min(at["need"], at["room"])) < 1, \
                f"At {width} px ({moment}) the terminal is {at['height']:.0f} px where its lines need {at['need']:.0f} " \
                f"and the gates leave it {at['room']:.0f}"
        assert at["newestInView"], f"At {width} px ({moment}) the newest line is scrolled out of view"
    assert loop["scrollWidth"] <= loop["clientWidth"]


@pytest.mark.parametrize("answer", sorted(LOOP_LANDED))
@pytest.mark.parametrize("width,height", [(1024, 768), (1100, 900), (1440, 900)])
def test_on_a_desk_neither_column_of_the_result_is_left_empty(tmp_path: Path, width: int, height: int, answer: str) -> None:
    """A judge: after a scenario with a long fix, the terminal's column was mostly empty page beside it.

    The terminal and the gates with their sentence sit side by side and end
    near each other, whatever the scenario and whether the answer is live or
    a recorded replay (whose loop left the gates' column 138 px short at
    1024 px); a fix or a certificate, of any length, runs the panel's full
    width under both.
    """
    got = measure("index.html", tmp_path, width=width, height=height, replies=_scenario_replies(answer),
                  moments=SCENARIO_MOMENTS, probe=TERMINAL, before=SCENARIOS)
    taken = got["taken"]
    assert taken["loop"]["badge"] == LOOP_LANDED[answer]["badge"], f"Measured the {answer} answers"
    assert taken["boundary"]["verdict"] == "BLOCKED_BOUNDARY_VIOLATION"
    assert taken["boundary"]["fixShown"] == (answer == "live"), "A live refusal brings its fix of two files; a replay shows none"
    assert taken["certificate"]["certShown"], "The certificate is shown"
    for moment, at in taken.items():
        assert at["sideBySide"], f"At {width} px ({moment}) the terminal and the gates are not side by side"
        gap = abs(at["sentEnd"] - at["judgedEnd"])
        assert gap < 96, f"At {width} px ({moment}) one column ends {gap:.0f} px before the other ({at['sentEnd']:.0f} and {at['judgedEnd']:.0f})"
    for moment, shown, wide in (("boundary", "fixShown", "fixWidth"), ("certificate", "certShown", "certWidth")):
        if taken[moment][shown]:
            assert abs(taken[moment][wide] - taken[moment]["gridWidth"]) < 1, f"The {moment}'s row does not run the panel's width"
    assert taken["boundary"]["scrollWidth"] <= taken["boundary"]["clientWidth"]
