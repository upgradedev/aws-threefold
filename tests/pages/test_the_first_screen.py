"""The first screen says what Threefold is in one sentence and offers three things to do, each one click.

A judge who opens the public URL meets the hero before anything else: the
sentence, then "Try the two-stage rollout (60 s)", "Open the dashboard" and
"Connect a repository", then the zero-setup scenarios that trip the circuit
breaker and issue the certificate. The hero holds no number of its own: its
one line of figures is read from /api/overview when the page loads, without a
key, says that it counts a public demo stack's own traffic rather than usage,
and stays hidden when the stack does not answer, needs a key, or has counted
nothing. The sentence names three agents; the paragraph under it says which
of them a refusal has been measured to stop.

The markup is read as the stack serves it; the script runs under Node with the
stub browser in _browser.py, and those tests skip where Node is absent.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from _browser import page_source, run

SENTENCE = (
    "Threefold governs Claude Code, Codex and Antigravity: it refuses architecture violations and leaked credentials "
    "before they are written, halts loops and runaway cost, and rolls out in two stages so nothing breaks."
)
ACTIONS = [
    ("hero-try", "dashboard.html#/try", "Try the two-stage rollout (60 s)"),
    ("hero-dashboard", "dashboard.html#/overview", "Open the dashboard"),
    ("hero-connect", "dashboard.html#/connect", "Connect a repository"),
]

# simulateLoop writes its terminal log with createElement and appendChild,
# which the shared stub browser does not provide; the log is not under test.
DEMO_DOM = r"""
document.createElement = tag => ({ tagName: tag, className: '', textContent: '', innerHTML: '', setAttribute() {}, click() {}, remove() {} });
el('terminal-log').appendChild = () => {};
"""


def _hero() -> str:
    """The hero's content, from the end of its opening tag to its closing one."""
    body = page_source("index.html")
    after = body.split('id="what-threefold-is"', 1)[1]
    return after[after.index(">") + 1 :].split("</section>", 1)[0]


def _text(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", markup)).split())


def _overview(**totals) -> str:
    """A /api/overview answer shaped as the 2026-09-22 contract fixes it, with these totals."""
    values = dict(calls=1284, approved=1190, refused=37, would_refuse=57, needs_review=21, false_alarms=4, projects=12, agents=3)
    values.update(totals)
    fields = ", ".join(f"{key}: {value}" for key, value in values.items())
    return (
        "{ window_days: 7, generated_at: new Date().toISOString(), source: 'rollups', totals: { " + fields + " }, "
        "series: [], by_agent: [], by_origin: [], by_rule: [], by_project: [], stages: { observe: 12, enforce: 0 } }"
    )


def _load(tmp_path: Path, overview: str, before: str = "") -> dict:
    """The page loaded against a stack whose /api/overview answers `overview` (a reply, or 'network')."""
    return run(
        "index.html",
        r"""
  await tick();
  out.live = el('hero-live').innerHTML;
  out.hidden = el('hero-live').classList.contains('hidden');
  out.asked = calls.filter(c => c.url.indexOf('/api/overview') !== -1).map(c => ({ url: c.url, method: c.method, headers: c.headers }));
""",
        tmp_path,
        before=DEMO_DOM
        + before
        + "answer = api({ '/status': { status: 200, body: { service: 'Threefold', status: 'HEALTHY' } }, '/api/overview': "
        + overview
        + " });\n",
    )


# ---------------------------------------------------------------- what the markup says


def test_the_hero_is_one_sentence_of_what_threefold_does() -> None:
    hero = _hero()
    heading = re.search(r'<h2 id="hero-sentence"[^>]*>(.*?)</h2>', hero, re.S)
    assert heading, "The hero leads with its sentence"
    assert _text(heading.group(1)) == SENTENCE


def test_the_three_actions_come_next_each_one_click_in_this_order() -> None:
    hero = _hero()
    actions = hero.split('id="hero-actions"', 1)[1].split("</div>", 1)[0]
    found = re.findall(r'<a id="([^"]+)" href="([^"]+)"[^>]*>(.*?)</a>', actions, re.S)
    assert [(ident, href, _text(label)) for ident, href, label in found] == ACTIONS
    assert hero.index('id="hero-sentence"') < hero.index('id="hero-actions"')


def test_the_demo_scenarios_follow_the_hero_unchanged() -> None:
    """CLAUDE.md rule 6: the circuit breaker trip and the certificate stay one click from the first screen."""
    body = page_source("index.html")
    hero_ends = body.index("</section>", body.index('id="what-threefold-is"'))
    for scenario in ("simulateLoop", "simulateSecret", "simulateBoundary", "simulateCompliant", "simulateUniversalAdapter"):
        assert body.index(f'onclick="{scenario}()"') > hero_ends, f"{scenario} moved above the hero"
    assert body.index('id="what-threefold-is"') < body.index("Guided Agent Scenarios")
    main = body.split("<main", 1)[1].split(">", 1)[1]
    first_element = re.search(r"<(?!!--)[a-z]+[^>]*>", main).group(0)
    assert first_element.startswith('<section id="what-threefold-is"'), f"The hero is the first thing in main, not {first_element}"


def test_the_hero_says_which_agents_a_refusal_is_measured_to_stop() -> None:
    """The sentence says "before they are written" for three agents; STATE.md has that measured for two.

    Claude Code and Antigravity refused a Write and no file was created; Codex could not be measured, so its edits
    count as governed at commit time only. The sentence is the one the first screen was specified with, so the
    scope sits in the paragraph right under the actions, where the same glance reaches it.
    """
    hero = _hero()
    scope = re.search(r'<span id="hero-scope">(.*?)</span>', hero, re.S)
    assert scope, "The hero says what has been measured"
    assert _text(scope.group(1)) == (
        "That a refusal stops the write has been measured for Claude Code and Antigravity; for Codex it has not been "
        "measured yet, so its edits count as governed at commit time only."
    )
    assert hero.index('id="hero-actions"') < hero.index('id="hero-scope"')


def test_the_hero_writes_no_number_of_its_own() -> None:
    hero = _hero()
    assert re.findall(r"\d+", _text(hero)) == ["60"], "Only the 60 in the walkthrough's label; figures come from the API"
    live = re.search(r'<p id="hero-live" class="([^"]*)"[^>]*>(.*?)</p>', hero, re.S)
    assert live and "hidden" in live.group(1).split() and live.group(2).strip() == "", "The live line starts hidden and empty"


def test_the_hero_fits_a_phone_without_scrolling_sideways() -> None:
    """375 px: the actions stack, nothing refuses to wrap, and nothing is given a fixed width."""
    body = page_source("index.html")
    assert '<meta name="viewport" content="width=device-width, initial-scale=1.0" />' in body
    hero = _hero()
    actions_classes = re.search(r'id="hero-actions" class="([^"]*)"', hero).group(1).split()
    assert "flex-col" in actions_classes and "sm:flex-row" in actions_classes and "sm:flex-wrap" in actions_classes
    assert "whitespace-nowrap" not in hero and "truncate" not in hero
    assert not re.search(r"\b(?:min-)?w-(?:\[\d|\d{2,})", hero), "A fixed width can push a 375 px screen sideways"
    assert "break-words" in re.search(r'id="hero-sentence" class="([^"]*)"', hero).group(1).split()


# ---------------------------------------------------------------- what the script does


def test_the_links_carry_the_stage_prefix_when_served(tmp_path: Path) -> None:
    out = run(
        "index.html",
        r"""
  out.links = ['hero-try', 'hero-dashboard', 'hero-connect'].map(id => el(id).href);
""",
        tmp_path,
        before=DEMO_DOM,
    )
    assert out["links"] == [f"https://example.test/prod/{href}" for _, href, _ in ACTIONS]


def test_the_live_line_is_read_from_the_overview_at_load(tmp_path: Path) -> None:
    out = _load(tmp_path, "{ status: 200, body: " + _overview() + " }")
    assert not out["hidden"]
    assert _text(out["live"]) == (
        "This public demo stack, last 7 days: 1,284 tool calls judged, 37 refused, 57 that would have been refused "
        "while observing, across 12 projects. They are this project's own probes and tests, the scenarios on this page, "
        "the sandboxes visitors start (each walkthrough adds one) and anyone else calling its open API, so they show "
        "that the stack is live, not how widely Threefold is used. See them on the dashboard."
    )
    assert 'href="https://example.test/prod/dashboard.html#/overview"' in out["live"]
    (asked,) = out["asked"]
    assert asked["url"] == "https://example.test/prod/api/overview?days=7" and asked["method"] == "GET"
    assert "X-API-Key" not in asked["headers"], "No key is sent unless someone typed one"


def test_the_live_line_is_read_without_a_key_even_when_one_is_typed(tmp_path: Path) -> None:
    """So it only ever shows a stack whose reads are open, which is what its words say it is."""
    out = _load(tmp_path, "{ status: 200, body: " + _overview() + " }", before="el('apiKeyInput').value = 'acme-operator-key';\n")
    (asked,) = out["asked"]
    assert "X-API-Key" not in asked["headers"], "A typed key would read a private stack's totals under a demo label"
    assert not out["hidden"]


def test_one_of_each_reads_in_the_singular(tmp_path: Path) -> None:
    out = _load(tmp_path, "{ status: 200, body: " + _overview(calls=1, refused=0, would_refuse=1, projects=1) + " }")
    assert "1 tool call judged, 0 refused, 1 that would have been refused while observing, across 1 project." in _text(out["live"])


def test_the_live_line_stays_hidden_when_the_stack_does_not_answer_with_figures(tmp_path: Path) -> None:
    cases = {
        "not found": "{ status: 404, body: { detail: 'no route' } }",
        "a private stack": "{ status: 401, body: { title: 'Unauthorized' } }",
        "unreachable": "'network'",
        "nothing counted": "{ status: 200, body: " + _overview(calls=0, refused=0, would_refuse=0, projects=0) + " }",
        "not the contract's shape": "{ status: 200, body: { totals: 'many' } }",
        "no window": "{ status: 200, body: { totals: { calls: 5, refused: 1, would_refuse: 0, projects: 1 } } }",
    }
    for name, overview in cases.items():
        out = _load(tmp_path, overview)
        assert out["hidden"] and out["live"] == "", name


def test_hostile_totals_never_reach_the_page(tmp_path: Path) -> None:
    evil = "'<img src=x onerror=alert(1)>'"
    out = _load(tmp_path, "{ status: 200, body: " + _overview(calls=evil, refused=evil) + " }")
    assert out["hidden"] and out["live"] == "", "A figure that is not a number is not shown at all"
    out = _load(tmp_path, "{ status: 200, body: " + _overview(calls=-3) + " }")
    assert out["hidden"], "A negative count is not a count"
    out = _load(tmp_path, "{ status: 200, body: " + _overview(calls=2.5) + " }")
    assert out["hidden"] and out["live"] == "", "A fractional count is not shown rounded down; it is not shown"


def test_the_flagship_loop_still_trips_after_the_hero_has_loaded(tmp_path: Path) -> None:
    out = run(
        "index.html",
        r"""
  await tick();
  out.liveShown = !el('hero-live').classList.contains('hidden');
  await simulateLoop(); await tick();
  out.tag = el('verdict-tag').innerText;
  out.breaker = el('cb-indicator').innerHTML;
""",
        tmp_path,
        before=DEMO_DOM
        + "answer = api({ '/status': { status: 200, body: { service: 'Threefold', status: 'HEALTHY' } }, "
        + "'/api/overview': { status: 200, body: "
        + _overview()
        + " }, 'POST /simulate-loop': { status: 200, body: { status: 'BLOCKED_LOOP_DETECTED', reason: 'Loop detected', "
        + "session_id: 'sim-1', session_tripped: true, bedrock_explanation: 'A sentence.', explanation_source: 'deterministic' } } });\n",
    )
    assert out["liveShown"]
    assert out["tag"] == "BLOCKED_LOOP_DETECTED"
    assert "CIRCUIT BREAKER: TRIPPED" in out["breaker"]
