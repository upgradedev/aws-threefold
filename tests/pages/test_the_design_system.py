"""The design system and the shell every page shares: tokens, icons, motion, components, the palette.

The tokens are read from threefold.css and held to what the stylesheet claims
beside them: every text colour clears WCAG AA on every surface, and the
status colours a chart stacks are the ones the palette validator passed. The
behaviour is run under Node with the stub browser in _browser.py, driving the
layer's own listeners the way a browser would, so a keyboard path that only
works in the author's head fails here.

Everything named here is synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from _browser import WEB, page_source, run

CSS = (WEB / "assets" / "threefold.css").read_text(encoding="utf-8")
SHELL_PAGES = ["rules.html", "sessions.html", "settings.html", "connect.html", "swagger.html"]

# A value the service could send, which would run if it were written as markup.
EVIL = 'x"><img src=x onerror=alert(1)><svg onload=alert(2)>'

# Keyboard, input and click events as the layer's document listeners receive
# them. `stopped` and `prevented` say whether the layer claimed the key.
EVENTS = r"""
function press(key, extra) {
  const event = Object.assign({ key, target: el('view'), prevented: false, stopped: false,
    preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; } }, extra || {});
  (docListeners.keydown || []).forEach(fn => fn(event));
  return event;
}
function typeInPalette(text) {
  const input = el('tf-palette-input');
  input.setAttribute('data-tf-palette-input', '');
  input.value = text;
  (docListeners.input || []).forEach(fn => fn({ target: input }));
}
function clickOn(node) {
  const event = { target: node, prevented: false, preventDefault() { this.prevented = true; } };
  (docListeners.click || []).forEach(fn => fn(event));
  return event;
}
function projectsAnswer(list) {
  return { status: 200, body: { projects: list.map(name => ({ project: name, stage: 'observe', configured: true, calls: 12, needs_review: 3, would_refuse: 4, refused: 0 })) } };
}
"""


# ------------------------------------------------------------------- tokens


def _tokens() -> dict[str, str]:
    root = CSS.split(":root {", 1)[1].split("\n}", 1)[0]
    return dict(re.findall(r"(--tf-[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", root))


def _luminance(hex_colour: str) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_every_text_token_clears_aa_on_every_surface() -> None:
    tokens = _tokens()
    texts = ["--tf-text", "--tf-text-2", "--tf-text-3", "--tf-text-4", "--tf-accent-text",
             "--tf-refused-text", "--tf-observed-text", "--tf-approved-text", "--tf-info-text"]
    surfaces = ["--tf-bg", "--tf-bg-2", "--tf-surface-1", "--tf-surface-2", "--tf-surface-3", "--tf-inset", "--tf-ink-0"]
    for name in texts + surfaces:
        assert name in tokens, f"threefold.css defines no {name}"
    for text in texts:
        for surface in surfaces:
            ratio = _contrast(tokens[text], tokens[surface])
            assert ratio >= 4.5, f"{text} on {surface} is {ratio:.2f}:1, under WCAG AA"
    # White on the primary button's violet.
    assert _contrast("#ffffff", tokens["--tf-accent-strong"]) >= 4.5


def test_the_tailwind_grays_are_re_stepped_to_ink_that_clears_aa() -> None:
    """A page's text-gray-500 was Tailwind's #6b7280: 3.8:1 on a card."""
    layer = page_source("assets/threefold.js")
    block = layer.split("var INK_GRAY = {", 1)[1].split("};", 1)[0]
    gray = {int(k): v for k, v in re.findall(r"(\d+): '(#[0-9a-f]{6})'", block)}
    for text in (300, 400, 500, 600):
        for surface in (800, 900, 950):
            ratio = _contrast(gray[text], gray[surface])
            assert ratio >= 4.5, f"gray-{text} on gray-{surface} is {ratio:.2f}:1"
    tokens = _tokens()
    assert gray[900] == tokens["--tf-surface-1"] and gray[950] == tokens["--tf-inset"] and gray[800] == tokens["--tf-hairline"]


def test_the_status_colours_are_the_ones_the_validator_passed_and_the_charts_use() -> None:
    tokens = _tokens()
    layer = page_source("assets/threefold.js")
    colours = dict(re.findall(r"^\s+(approved|observed|refused|info|accent|surface): '(#[0-9a-f]{6})'", layer, re.M))
    assert colours["refused"] == tokens["--tf-refused"] == "#e11d48"
    assert colours["observed"] == tokens["--tf-observed"] == "#d97706"
    assert colours["approved"] == tokens["--tf-approved"] == "#0ea572"
    assert colours["surface"] == tokens["--tf-surface-1"], "The chart surface is the one the palette was validated against"
    # The validator's run is recorded beside the tokens, with the palette it passed.
    note = CSS.split("Palette validation", 1)[1].split("*/", 1)[0]
    assert 'validate_palette.js "#e11d48,#d97706,#0ea572" --mode dark --surface "#0e1322"' in note
    assert "PASS CVD separation" in note and "PASS normal-vision floor" in note
    assert "--pairs all" in note, "The categorical set is validated all-pairs"
    for status in ("--tf-refused", "--tf-observed", "--tf-approved"):
        assert _contrast(tokens[status], tokens["--tf-surface-1"]) >= 3, f"{status} is under 3:1 as a mark"


def test_only_a_wait_loops_and_every_animation_stops_under_reduced_motion() -> None:
    infinite = set(re.findall(r"animation:\s*(tf-[a-z-]+)[^;]*infinite", CSS))
    assert infinite == {"tf-shimmer", "tf-ping", "tf-spin"}, f"Something else loops forever: {infinite}"
    reduced = CSS.split("@media (prefers-reduced-motion: reduce) {", 1)[1].split("\n}\n", 1)[0]
    for selector in (".tf-reveal", ".tf-pulse", ".tf-skeleton", ".tf-waiting-dot::after", ".tf-palette", ".tf-dialog",
                     ".tf-toast", ".tf-popover", ".tf-sheet", ".tf-spin", '.tf-btn[aria-busy="true"]::before'):
        assert selector in reduced, f"{selector} still animates under prefers-reduced-motion"


# ------------------------------------------------------------------ icons


def test_one_icon_set_on_a_20px_grid_at_stroke_1_5(tmp_path: Path) -> None:
    names = ["shield", "check", "x", "alert", "eye", "lock", "git", "terminal", "sparkles", "chart", "list", "folder",
             "user", "settings", "key", "bolt", "arrow-right", "external", "copy", "search", "command", "spinner", "sun-moon"]
    out = run(
        "swagger.html",
        r"""
  out.icons = %s.map(name => [name, String(Threefold.icon(name, 20))]);
  out.unknown = String(Threefold.icon('not-an-icon'));
  out.safe = Threefold.icon('shield') instanceof Threefold.SafeHtml;
  out.injected = String(Threefold.icon('check', 16, '" onload="alert(1)'));
""" % json.dumps(names),
        tmp_path,
    )
    for name, svg in out["icons"]:
        assert svg.startswith('<svg class="tf-icon"'), f"{name} is not drawn"
        assert 'viewBox="0 0 20 20"' in svg and 'stroke-width="1.5"' in svg, f"{name} is off the grid"
        assert 'aria-hidden="true"' in svg and 'focusable="false"' in svg, f"{name} is announced"
    assert out["unknown"] == "", "An unknown name draws nothing rather than a guess"
    assert out["safe"], "An icon is markup the layer built, so it passes html`` unescaped"
    assert 'onload="' not in out["injected"], "A class name cannot open an attribute"


# ----------------------------------------------------------------- motion


MOTION = r"""
let reduce = false;
globalThis.matchMedia = query => ({ matches: reduce && /prefers-reduced-motion: reduce/.test(query) });
const frames = [];
let clock = 0;
globalThis.requestAnimationFrame = fn => { frames.push(fn); return frames.length; };
function flush(n) { for (let i = 0; i < (n || 40) && frames.length; i++) { clock += 100; frames.shift()(clock); } }
function node(id) {
  const n = makeEl(id);
  n.style = { props: {}, setProperty(k, v) { this.props[k] = v; } };
  n.offsetWidth = 0;
  return n;
}
"""


def test_the_motion_helpers_do_nothing_under_reduced_motion(tmp_path: Path) -> None:
    out = run(
        "swagger.html",
        r"""
  reduce = true;
  const a = node('a'); a.textContent = '12,345';
  out.reducedCount = Threefold.countUp(a, 12345);
  out.reducedFrames = frames.length;
  out.reducedText = a.textContent;
  const list = [node('r1'), node('r2')];
  out.reducedReveal = Threefold.reveal(list);
  out.reducedClasses = list.map(n => n.className);
  const p = node('p');
  out.reducedPulse = Threefold.pulse(p, 'ok');
  out.reducedPulseClass = p.className;

  reduce = false;
  const b = node('b'); b.textContent = '12,345';
  out.count = Threefold.countUp(b, 12345);
  out.first = b.textContent;
  flush(3);
  out.midway = b.textContent;
  flush();
  out.final = b.textContent;
  out.revealed = Threefold.reveal(list, { stagger: 40 });
  out.classes = list.map(n => n.className);
  out.delays = list.map(n => n.style.props['--tf-delay']);
  out.pulsed = Threefold.pulse(p, 'ok');
  out.pulseClass = p.className;
""",
        tmp_path,
        before=MOTION,
    )
    assert out["reducedCount"] is False and out["reducedFrames"] == 0 and out["reducedText"] == "12,345"
    assert out["reducedReveal"] == 0 and out["reducedClasses"] == ["", ""]
    assert out["reducedPulse"] is False and out["reducedPulseClass"] == ""
    assert out["count"] is True and out["first"] == "0"
    assert out["midway"] not in ("0", "12,345"), "The number climbs"
    assert out["final"] == "12,345", "It ends on exactly the words the page wrote, never 12.3K"
    assert out["revealed"] == 2 and all("tf-reveal" in c for c in out["classes"])
    assert out["delays"] == ["0ms", "40ms"], "A stagger, one step per node"
    assert out["pulsed"] is True and "tf-pulse" in out["pulseClass"] and "tf-pulse-ok" in out["pulseClass"]


def test_a_count_yields_to_the_page_and_waits_for_a_tab_that_is_seen(tmp_path: Path) -> None:
    out = run(
        "swagger.html",
        r"""
  const a = node('a'); a.textContent = '96';
  Threefold.countUp(a, 96);
  flush(2);
  a.textContent = '7';
  flush();
  out.yielded = a.textContent;

  const once = node('once'); once.textContent = '5';
  out.first = Threefold.countUp(once, 5, { key: 'tiles' });
  flush();
  out.again = Threefold.countUp(once, 5, { key: 'tiles' });

  document.visibilityState = 'hidden';
  const hidden = node('hidden'); hidden.textContent = '40';
  out.hidden = Threefold.countUp(hidden, 40);
  out.hiddenText = hidden.textContent;
""",
        tmp_path,
        before=MOTION,
    )
    assert out["yielded"] == "7", "A later write by the page is never overwritten by a stale climb"
    assert out["first"] is True and out["again"] is False, "A figure counts up once, not on every refresh"
    assert out["hidden"] is False and out["hiddenText"] == "40", "A background tab keeps the real number"


# ------------------------------------------------------------------ shell


def test_the_shell_has_five_destinations_more_and_a_search_that_says_its_keys(tmp_path: Path) -> None:
    out = run(
        "settings.html",
        r"""
  out.nav = el('page-nav').innerHTML;
""",
        tmp_path,
    )
    nav = out["nav"]
    bar = nav.split('<nav aria-label="Threefold" class="tf-nav">', 1)[1].split('<div class="tf-more"', 1)[0]
    assert re.findall(r">([A-Za-z]+)</a>", bar) == ["Overview", "Review", "Projects", "Rules", "Sessions"]
    more = nav.split("data-tf-more-menu", 1)[1].split("</div>", 1)[0]
    assert re.findall(r"</svg>([A-Za-z]+)</a>", more) == ["Connect", "Proof", "Demo", "API", "Settings"]
    assert 'aria-current="page"' in more, "The page the reader is on is marked where it is listed"
    toggle = re.search(r'<button type="button" class="tf-nav-link tf-nav-current" data-tf-more-toggle aria-expanded="false" aria-controls="([^"]+)"', nav)
    assert toggle, "More is marked current when the page is one of its items, and says it is collapsed"
    assert f'id="{toggle.group(1)}"' in nav
    search = re.search(r"<button[^>]*data-tf-palette-open[^>]*>", nav).group(0)
    assert 'aria-keyshortcuts="Control+K Meta+K /"' in search and "aria-haspopup=\"dialog\"" in search
    assert ">Ctrl</kbd><kbd class=\"tf-kbd\">K</kbd>" in nav, "The shortcut is shown, not only announced"
    sheet = nav.split('id="tf-menu"', 1)[1]
    for item in ("Overview", "Review", "Projects", "Rules", "Sessions", "Connect", "Proof", "Demo", "API", "Settings"):
        assert f"</svg>{item}</a>" in sheet, f"The phone's menu sheet lacks {item}"
    assert 'data-tf-menu aria-expanded="false" aria-controls="tf-menu"' in nav


@pytest.mark.parametrize("page", SHELL_PAGES)
def test_every_page_wears_the_same_header_and_a_skip_link(page: str) -> None:
    body = page_source(page)
    header = body.split('<header class="tf-header">', 1)[1].split("</header>", 1)[0]
    assert '<a class="tf-brand" href="dashboard.html#/overview" data-tf-page="dashboard.html#/overview"' in header
    assert '<div id="page-nav" class="tf-nav-mount">' in header
    assert '<a class="tf-skip" href="#main">Skip to content</a>' in body
    assert '<main id="main" class="tf-main" tabindex="-1">' in body
    assert '<link rel="stylesheet" href="assets/threefold.css" />' in body
    assert body.count("<h1 ") == 1, "One h1 a page"
    # The layer extends the page's Tailwind config, so it loads after it.
    if "tailwind.config" in body:
        assert body.index("tailwind.config") < body.index('src="assets/threefold.js"')


# --------------------------------------------------------------- components


def test_a_toast_is_read_out_and_its_action_runs_once(tmp_path: Path) -> None:
    out = run(
        "swagger.html",
        r"""
  let ran = 0;
  const id = Threefold.toast(EVIL_TEXT, { tone: 'ok', action: { label: 'Undo', run() { ran += 1; } } });
  out.markup = el('tf-toast-root').innerHTML;
  const button = makeEl('undo');
  button.setAttribute('data-tf-toast-action', id);
  clickOn(button);
  clickOn(button);
  out.ran = ran;
  out.after = el('tf-toast-root').innerHTML;
  Threefold.announce('Copied to the clipboard.');
  await new Promise(r => setTimeout(r, 60));
  out.live = el('tf-live').textContent;
""",
        tmp_path,
        before=EVENTS + "const EVIL_TEXT = %s;" % json.dumps(EVIL),
    )
    assert "<img" not in out["markup"] and "&lt;img" in out["markup"], "A toast's words are escaped"
    assert "tf-toast-ok" in out["markup"] and ">Undo</button>" in out["markup"]
    assert out["ran"] == 1, "The action runs once and the toast is gone"
    assert out["after"] == ""
    assert out["live"] == "Copied to the clipboard."


def test_a_dialog_takes_focus_closes_on_escape_and_gives_focus_back(tmp_path: Path) -> None:
    out = run(
        "swagger.html",
        r"""
  const opener = makeEl('opener');
  opener.focus();
  let closed = 0;
  Threefold.openDialog({ title: EVIL_TEXT, body: Threefold.html`<p>Every later call is refused.</p>`, onClose() { closed += 1; } });
  out.markup = el('tf-dialog-root').innerHTML;
  out.focused = document.activeElement && document.activeElement.id;
  out.escape = press('Escape');
  out.after = el('tf-dialog-root').innerHTML;
  out.returned = document.activeElement && document.activeElement.id;
  out.closed = closed;
""",
        tmp_path,
        before=EVENTS + "const EVIL_TEXT = %s;" % json.dumps(EVIL),
    )
    markup = out["markup"]
    assert 'role="dialog" aria-modal="true" aria-labelledby="tf-dialog-title"' in markup
    assert "<img" not in markup and "&lt;img" in markup
    assert out["focused"] == "tf-dialog", "Focus moves into the dialog"
    assert out["escape"]["prevented"] is True and out["after"] == "", "Escape closes it"
    assert out["returned"] == "opener" and out["closed"] == 1, "and focus goes back where it came from"


def test_tabs_select_with_a_click_and_move_with_the_arrow_keys(tmp_path: Path) -> None:
    out = run(
        "swagger.html",
        r"""
  const list = makeEl('shells');
  list.setAttribute('data-tf-tabs', '');
  const tabs = ['posix', 'powershell', 'cmd'].map(name => {
    const t = makeEl('tab-' + name);
    t.setAttribute('role', 'tab');
    t.setAttribute('aria-controls', 'panel-' + name);
    t.parentNode = list;
    t.closest = () => t;
    return t;
  });
  list.querySelectorAll = () => tabs;
  const state = () => tabs.map(t => t.getAttribute('aria-selected') + '/' + t.getAttribute('tabindex') + '/' + el(t.getAttribute('aria-controls')).hidden);
  Threefold.selectTab(tabs[0]);
  out.start = state();
  press('ArrowRight', { target: tabs[0] });
  out.right = state();
  out.focus = document.activeElement.id;
  press('End', { target: tabs[1] });
  out.end = state();
  press('ArrowRight', { target: tabs[2] });
  out.wrapped = state();
""",
        tmp_path,
        before=EVENTS,
    )
    assert out["start"] == ["true/0/false", "false/-1/true", "false/-1/true"]
    assert out["right"] == ["false/-1/true", "true/0/false", "false/-1/true"]
    assert out["focus"] == "tab-powershell", "The arrow keys move focus with the selection"
    assert out["end"][2] == "true/0/false"
    assert out["wrapped"][0] == "true/0/false", "Past the last tab is the first"


def test_a_stat_tile_and_an_empty_state_say_what_they_mean(tmp_path: Path) -> None:
    out = run(
        "swagger.html",
        r"""
  const T = Threefold;
  out.tile = String(T.statTile({ label: 'Stopped before it ran', value: 12345, sub: 'refused by a rule in Enforce', term: 'BLOCKED_*', href: '#/calls?kind=refused', metric: 'refused', delta: { value: 3, period: 'vs last week', upIsGood: false } }));
  out.plain = String(T.statTile({ label: EVIL_TEXT, value: null, sub: EVIL_TEXT }));
  out.empty = String(T.emptyState({ icon: 'plug', text: 'No call has reached this stack yet.', action: { label: 'Connect a repository', href: '#/connect', primary: true } }));
  out.chip = String(T.statusChip('refused'));
  out.observed = String(T.statusChip('observed', 'Would have been stopped'));
""",
        tmp_path,
        before="const EVIL_TEXT = %s;" % json.dumps(EVIL),
    )
    tile = out["tile"]
    assert 'href="#/calls?kind=refused"' in tile and 'data-metric="refused" data-tf-count="12345"' in tile
    assert re.search(r'data-metric="refused"[^>]*title="12,345">12.3K<', tile), \
        "The value shown is the API's, compacted as num() does, with the exact count on hover"
    assert "Stopped before it ran" in tile and "BLOCKED_*" in tile and "tf-delta-bad" in tile, "Up is bad for refusals"
    assert "<img" not in out["plain"] and "&lt;img" in out["plain"] and ">—<" in out["plain"], "No value, a dash"
    assert 'data-state="empty"' in out["empty"] and 'href="#/connect"' in out["empty"] and "tf-btn-primary" in out["empty"]
    assert "</svg>Refused</span>" in out["chip"], "A status is an icon and a word"
    assert "</svg>Would have been stopped</span>" in out["observed"]


# ------------------------------------------------------------------ charts


def test_every_mark_has_a_tooltip_on_hover_and_focus_and_a_table_a_reader_can_open(tmp_path: Path) -> None:
    out = run(
        "swagger.html",
        r"""
  const T = Threefold;
  const series = [{ day: '2026-09-24', approved: 5, observed: 2, refused: 1 }, { day: '2026-09-25', approved: 9, observed: 0, refused: 0 }];
  out.stack = String(T.stackedBars(series, { keys: [
    { key: 'refused', label: 'Refused', color: '#e11d48' }, { key: 'observed', label: EVIL_TEXT, color: '#d97706' }, { key: 'approved', label: 'Approved', color: '#0ea572' }
  ], href: (r, k) => '#/calls?kind=' + k }));
  out.plain = String(T.stackedBars(series, { keys: [{ key: 'approved', label: 'Approved', color: '#0ea572' }] }));
  out.bars = String(T.hbars([{ label: 'LOOP', segments: [{ value: 3, label: 'refused', color: '#e11d48' }, { value: 5, label: 'would refuse', color: '#d97706', href: '#/calls?rule=LOOP' }] }]));
  out.ring = String(T.donut([{ label: 'Refused', value: 2, color: '#e11d48' }, { label: 'Approved', value: 8, color: '#0ea572', href: '#/calls' }]));
  out.spark = String(T.sparkline([1, 4, 2], { label: 'Refused per day', labels: ['23 Sep', '24 Sep', '25 Sep'] }));

  const figure = makeEl('figure');
  figure.setAttribute('data-tf-figure', '');
  figure.setAttribute('data-table', 'closed');
  const toggle = makeEl('toggle');
  toggle.setAttribute('data-tf-table-toggle', '');
  toggle.parentNode = figure;
  clickOn(toggle);
  out.opened = [figure.getAttribute('data-table'), toggle.getAttribute('aria-expanded')];
  clickOn(toggle);
  out.closed = [figure.getAttribute('data-table'), toggle.getAttribute('aria-expanded')];

  const mark = makeEl('mark');
  mark.setAttribute('data-tf-tip', '25 Sep · Refused');
  mark.setAttribute('data-tf-tip-value', '4');
  (docListeners.focusin || []).forEach(fn => fn({ target: mark }));
  out.tip = [el('tf-tip').getAttribute('data-show'), el('tf-tip').textContent];
  (docListeners.focusout || []).forEach(fn => fn({ target: mark }));
  out.tipGone = el('tf-tip').getAttribute('data-show');
""",
        tmp_path,
        before=EVENTS + "const EVIL_TEXT = %s;" % json.dumps(EVIL),
    )
    stack = out["stack"]
    links = re.findall(r"<a href=[^>]*>", stack)
    assert len(links) == 4, "One link per drawn segment"
    assert all("data-tf-tip=" in a and "data-tf-tip-value=" in a and 'aria-label="' in a for a in links)
    assert stack.count('class="tf-hit"') == 4, "Each mark's hit area is wider than the mark"
    assert "<img" not in stack and "&lt;img" in stack, "A series label is escaped in the tooltip and the legend"
    table = re.search(r'aria-controls="(tf-stack-\d+-table)" aria-expanded="false"', stack)
    assert table and f'id="{table.group(1)}"' in stack and 'class="sr-only"' in stack
    assert "Show as a table" in stack
    assert re.search(r'class="tf-chart-label"[^>]*>14<', stack) or re.search(r'class="tf-chart-label"[^>]*>9<', stack), \
        "The newest or the busiest day carries its total on the cap"
    assert len(re.findall(r'class="tf-chart-label"', stack)) <= 4, "Direct labels on a few values, never every one"
    assert '<g tabindex="0"' in out["plain"], "A mark with no rows behind it is still reachable from the keyboard"
    assert 'class="tf-legend"' not in out["plain"], "One series needs no legend"
    assert "tf-hbar-seg" in out["bars"] and 'tabindex="0"' in out["bars"] and out["bars"].count("data-tf-tip=") == 2
    assert out["ring"].count("data-tf-tip=") == 2
    assert out["spark"].count('class="tf-hit"') == 3 and 'data-tf-tip="24 Sep"' in out["spark"]
    assert out["opened"] == ["open", "true"] and out["closed"] == ["closed", "false"]
    assert out["tip"][0] == "true" and "4" in out["tip"][1] and "25 Sep · Refused" in out["tip"][1]
    assert out["tipGone"] == "false"


# ----------------------------------------------------------------- palette


def dash(scenario: str, tmp_path: Path, before: str = "") -> dict:
    return run("dashboard.html", scenario, tmp_path, before=EVENTS + before)


def test_ctrl_k_cmd_k_and_slash_open_the_palette_but_slash_in_a_field_does_not(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = api({ '/api/projects': projectsAnswer(['Acme-Billing']) });
  out.ctrl = [press('k', { ctrlKey: true }).prevented, Threefold.palette.isOpen()];
  out.ctrlAgain = [press('k', { ctrlKey: true }).prevented, Threefold.palette.isOpen()];
  out.meta = [press('K', { metaKey: true }).prevented, Threefold.palette.isOpen()];
  press('Escape');
  out.inField = ['INPUT', 'TEXTAREA', 'SELECT'].map(tagName => { press('/', { target: { tagName, hasAttribute: () => false } }); return Threefold.palette.isOpen(); });
  out.inEditable = (press('/', { target: { isContentEditable: true, hasAttribute: () => false } }), Threefold.palette.isOpen());
  out.slash = [press('/', { target: el('view') }).prevented, Threefold.palette.isOpen()];
  out.markup = el('tf-palette-root').innerHTML;
""",
        tmp_path,
    )
    assert out["ctrl"] == [True, True] and out["ctrlAgain"] == [True, False], "Ctrl+K toggles"
    assert out["meta"] == [True, True], "Cmd+K opens it on a Mac"
    assert out["inField"] == [False, False, False] and out["inEditable"] is False, "/ typed into a field stays in the field"
    assert out["slash"] == [True, True]
    markup = out["markup"]
    assert 'role="dialog" aria-modal="true"' in markup
    assert re.search(r'<input id="tf-palette-input"[^>]*role="combobox"[^>]*aria-expanded="true" aria-controls="tf-palette-list"', markup)


def test_the_palette_moves_with_the_arrows_opens_with_enter_and_escape_gives_focus_back(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = api({ '/api/projects': projectsAnswer(['Acme-Billing']) });
  const opener = makeEl('opener');
  opener.focus();
  press('k', { ctrlKey: true });
  await tick();
  out.focused = document.activeElement.id;
  out.list = el('tf-palette-list').innerHTML;
  out.first = el('tf-palette-input').getAttribute('aria-activedescendant');
  press('ArrowDown');
  out.second = el('tf-palette-input').getAttribute('aria-activedescendant');
  out.secondRow = (el('tf-palette-list').innerHTML.match(/<a class="tf-palette-item" id="tf-pal-1" role="option" aria-selected="([a-z]+)"/) || [])[1];
  document.activeElement = makeEl('elsewhere');
  out.tab = press('Tab').prevented;
  out.afterTab = document.activeElement.id;
  press('Escape');
  out.closed = !Threefold.palette.isOpen() && el('tf-palette-root').innerHTML === '';
  out.returned = document.activeElement.id;

  press('k', { ctrlKey: true });
  press('ArrowDown');
  press('Enter');
  await tick();
  out.hash = location.hash;
  out.open = Threefold.palette.isOpen();
""",
        tmp_path,
    )
    assert out["focused"] == "tf-palette-input"
    assert out["first"] == "tf-pal-0" and 'id="tf-pal-0" role="option" aria-selected="true"' in out["list"]
    assert out["second"] == "tf-pal-1" and out["secondRow"] == "true", "The active row is the one announced"
    assert out["tab"] is True and out["afterTab"] == "tf-palette-input", "Tab never leaves the palette"
    assert out["closed"] and out["returned"] == "opener"
    assert out["hash"] == "#/review", "In the dashboard, a route is a hash change"
    assert out["open"] is False


def test_the_palette_matches_loosely_and_finds_a_project_by_its_alias(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = api({ '/api/projects': projectsAnswer(['Acme-Billing', 'Acme-Catalog']) });
  press('k', { ctrlKey: true });
  await tick();
  const top = q => { typeInPalette(q); return Threefold.palette.results().slice(0, 1).map(r => r.text)[0]; };
  out.rls = top('rls');
  out.sess = top('sess');
  out.catalog = top('catal');
  out.connect = top('connect');
  out.catalogHref = (typeInPalette('catal'), Threefold.palette.results()[0].href);
  out.none = (typeInPalette('zqxj'), el('tf-palette-list').innerHTML);
  out.status = el('tf-palette-status').textContent;
""",
        tmp_path,
    )
    assert out["rls"] == "Rules" and out["sess"] == "Sessions"
    assert out["catalog"] == "Acme-Catalog" and out["catalogHref"] == "#/projects/Acme-Catalog"
    assert out["connect"] == "Connect a repository"
    assert "Nothing matches" in out["none"] and out["status"] == "0 results."


def test_the_palette_escapes_a_project_name_and_lists_none_it_cannot_read(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = api({ '/api/projects': projectsAnswer([EVIL_TEXT]) });
  press('k', { ctrlKey: true });
  await tick();
  typeInPalette('x');
  out.evil = el('tf-palette-list').innerHTML;
  press('Escape');
""",
        tmp_path,
        before="const EVIL_TEXT = %s;" % json.dumps(EVIL),
    )
    assert "<img" not in out["evil"] and "<svg onload" not in out["evil"] and "&lt;img" in out["evil"]

    refused = dash(
        r"""
  answer = api({ '/api/projects': { status: 403, body: { title: 'Forbidden' } } });
  press('k', { ctrlKey: true });
  await tick();
  out.list = el('tf-palette-list').innerHTML;
  press('Escape');
  press('k', { ctrlKey: true });
  await tick();
  out.reads = calls.filter(c => /\/api\/projects$/.test(c.url)).length;
""",
        tmp_path,
    )
    assert 'role="presentation">Projects</div>' not in refused["list"], "No project is listed"
    assert "Forbidden" not in refused["list"] and "403" not in refused["list"], "and no error stands in its place"
    assert refused["reads"] == 1, "The list is asked for once a page"


def test_the_palette_signs_out_only_a_signed_in_reader_and_works_outside_the_dashboard(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = api({ '/api/projects': projectsAnswer([]), 'DELETE /api/auth/sessions': { status: 204, body: null } });
  press('k', { ctrlKey: true });
  await tick();
  typeInPalette('sign out');
  out.anonymous = Threefold.palette.results().map(r => r.text);
  press('Escape');
  store['threefold-session'] = JSON.stringify({ token: 'tok-1', expires_at: new Date(Date.now() + 3600e3).toISOString() });
  press('k', { ctrlKey: true });
  typeInPalette('sign out');
  out.signedIn = Threefold.palette.results()[0];
  press('Enter');
  await tick();
  out.deleted = calls.filter(c => c.method === 'DELETE').map(c => c.url);
  out.session = store['threefold-session'] || null;
""",
        tmp_path,
    )
    assert "Sign out" not in out["anonymous"]
    assert out["signedIn"]["text"] == "Sign out" and out["signedIn"]["action"] is True
    assert out["deleted"] == ["https://example.test/prod/api/auth/sessions"] and out["session"] is None

    away = run(
        "rules.html",
        r"""
  const assigned = [];
  Object.defineProperty(location, 'href', { configurable: true, get() { return 'https://example.test/prod/rules.html'; }, set(v) { assigned.push(String(v)); } });
  press('k', { ctrlKey: true });
  typeInPalette('overview');
  press('Enter');
  out.assigned = assigned;
""",
        tmp_path,
        pathname="/prod/rules.html",
        before=EVENTS,
    )
    assert away["assigned"] == ["https://example.test/prod/dashboard.html#/overview"], "Off the dashboard, a route is the page"
