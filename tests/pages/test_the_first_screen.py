"""The first screen says what Threefold is, shows it working, and proves it, in that order.

A judge who opens the public URL meets the hero before anything else: one
promise line under twelve words, one sentence of how, "Try the two-stage
rollout — 2 min", "Open the live dashboard", and two quiet links, one down to
the flagship's scenarios and one to connect a repository, and beside them the
product's core moment. That moment is one
proposed write, the boundary write RECORDED.boundary was recorded from, asked
of this stack through the route a hook asks: the answer is the stack's own when
it gives one, labelled live, with its reason and whatever fix came with it, and the
recorded run otherwise, labelled recorded, with no fix, because a recording
kept none.

Under the hero, a proof strip whose every figure is read as the page loads:
calls judged, stopped and would have been stopped from GET /api/overview
(without a key, with one clause saying what the counts are made of), and the
benchmark pooled from GET /proof.json. Then how it works (three steps, one
flow diagram, the two-stage rollout, the three agents in the evidence's own
words), the AWS services as one diagram, the gates the flagship demo trips
(where the flagship's "under 60 seconds" is said, as the walkthrough's "about
two minutes" is said on the button that opens it), and a footer holding the
links and the hackathon's tags.

The markup is read as the stack serves it; the script runs under Node with the
stub browser in _browser.py, and those tests skip where Node is absent.
"""
from __future__ import annotations

import html
import itertools
import json
import re
from pathlib import Path

from _browser import ROOT, page_source, run

PROMISE = "Stop bad agent writes before they reach your code."
# A judge read "your repos observe before they enforce" twice before it made
# sense; the second sentence says the same in the page's own plain words.
SENTENCE = (
    "Deterministic gates on AWS judge each write and command from Claude Code, Codex or Antigravity. "
    "Each repository only watches until you promote its rules."
)
SCOPE = "A refusal is measured to stop the write in Claude Code and Antigravity, and in Codex once, over its patch tool only."
ACTIONS = [
    ("hero-try", "dashboard.html#/try", "Try the two-stage rollout — 2 min"),
    ("hero-dashboard", "dashboard.html#/overview", "Open the live dashboard"),
    ("hero-watch", "#watch-the-gates", "Watch a loop get halted, no account"),
    ("hero-connect", "dashboard.html#/connect", "Connect your repository"),
]
SECTIONS = ["what-threefold-is", "proof", "how-it-works", "built-on-aws", "watch-the-gates"]
REPOSITORY = "https://github.com/upgradedev/aws-threefold"
RECORDED_REASON = (
    "Clean Architecture violation: Layering rule 'python-domain-stays-pure' refuses this write: A Python file under "
    "domain/ may not import infrastructure or a driver. 'src/domain/user.py' imports 'boto3', which matches 'boto3'"
)
# The same reason as the hero says it: one plain sentence, then the rule's id
# and the status on the line under it, the status a tag of its own, so no
# separator is left hanging where the line wraps on a phone.
PLAIN_REASON = "A Python file under domain/ may not import infrastructure or a driver, and src/domain/user.py imports boto3."
RULE_LINE = "rule python-domain-stays-pure BLOCKED_BOUNDARY_VIOLATION"
PROOF_FILE = ROOT / "src" / "threefold" / "web" / "proof.json"

# simulateLoop writes its terminal log with createElement and appendChild,
# which the shared stub browser does not provide; the log is not under test.
DEMO_DOM = r"""
document.createElement = tag => ({ tagName: tag, className: '', textContent: '', innerHTML: '', setAttribute() {}, click() {}, remove() {} });
el('terminal-log').appendChild = () => {};
"""

# A refusal of the hero's call as the stack answers it, with the fix it checked.
LIVE_REFUSAL = r"""
const LIVE_REFUSAL = {
  status: 'BLOCKED_BOUNDARY_VIOLATION',
  reason: "Clean Architecture violation: Layering rule 'python-domain-stays-pure' refuses this write: A Python file under domain/ may not import infrastructure or a driver. 'src/domain/user.py' imports 'boto3', which matches 'boto3'",
  session_id: 'sim-hero', session_tripped: false, explanation_source: 'deterministic', bedrock_explanation: 'A sentence.',
  suggested_fix: {
    kind: 'layering', validated: true,
    summary: 'Checked fix: move boto3 out of the domain behind UserPort; adapter: src/infrastructure/user_adapter.py.',
    steps: ['Write the domain file below.'],
    writes: [
      { path: 'src/domain/user.py', content: 'class UserPort: ...\n' },
      { path: 'src/infrastructure/user_adapter.py', content: 'import boto3\n', new_file: true }
    ],
    checks: [
      { gate: 'layering', path: 'src/domain/user.py', passed: true },
      { gate: 'credential', path: 'src/domain/user.py', passed: true },
      { gate: 'layering', path: 'src/infrastructure/user_adapter.py', passed: true }
    ]
  }
};
const EVIL = 'x"><svg onload=alert(2)><img src=x onerror=alert(1)>';
"""


def _section(ident: str) -> str:
    """A section's content, from the end of its opening tag to its closing one."""
    body = page_source("index.html")
    after = body.split(f'id="{ident}"', 1)[1]
    return after[after.index(">") + 1 :].split("</section>", 1)[0]


def _hero() -> str:
    return _section("what-threefold-is")


def _style() -> str:
    return re.search(r"<style>(.*?)</style>", page_source("index.html"), re.S).group(1)


def _text(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def _read(markup: str) -> str:
    """The words as a reader sees them: an inline tag ends no word, so no space is put before a stop."""
    return re.sub(r" ([.,])", lambda m: m.group(1), _text(markup))


def _overview(extra: str = "", **totals) -> str:
    """A /api/overview answer shaped as the contract fixes it, with these totals."""
    values = dict(calls=1284, approved=1190, refused=37, would_refuse=57, needs_review=21, false_alarms=4, projects=12, agents=3)
    values.update(totals)
    fields = ", ".join(f"{key}: {value}" for key, value in values.items())
    return (
        "{ window_days: 7, generated_at: new Date().toISOString(), source: 'rollups', totals: { " + fields + " }, "
        "series: [], by_agent: [], by_origin: [], by_rule: [], by_project: [], stages: { observe: 12, enforce: 0 }"
        + (", " + extra if extra else "")
        + " }"
    )


def _load(tmp_path: Path, overview: str = "'network'", hero: str = "'network'", proof: str = "'network'", before: str = "", scenario: str = "") -> dict:
    """The page loaded against a stack whose three reads answer as given (a reply, or 'network')."""
    out = _run_loaded(tmp_path, overview, hero, proof, before, scenario)
    # The caption is markup, so a phone never breaks the route or the date at
    # a hyphen; what is compared is what a reader reads.
    out["caption"] = _read(out["caption"])
    return out


def _run_loaded(tmp_path: Path, overview: str, hero: str, proof: str, before: str, scenario: str) -> dict:
    return run(
        "index.html",
        r"""
  await tick();
  out.live = el('proof-live').innerHTML;
  out.where = el('proof-where').innerHTML;
  out.whereHidden = el('proof-where').hidden;
  out.bench = el('proof-bench').innerHTML;
  out.reads = el('proof-links').innerHTML;
  out.overviewAsked = calls.filter(c => c.url.indexOf('/api/overview') !== -1).map(c => ({ url: c.url, method: c.method, headers: c.headers }));
  out.proofAsked = calls.filter(c => c.url.indexOf('/proof.json') !== -1).map(c => ({ url: c.url, method: c.method, headers: c.headers }));
  out.heroAsked = calls.filter(c => c.url.indexOf('/evaluate-tool-call') !== -1).map(c => ({ url: c.url, method: c.method, headers: c.headers, body: c.body }));
  out.source = el('hero-source').innerHTML;
  out.caption = el('hero-caption').innerHTML;
  out.captionMarkup = el('hero-caption').innerHTML;
  out.diff = el('hero-diff').innerHTML;
  out.call = el('hero-call').innerHTML;
  out.verdict = el('hero-verdict').innerHTML;
  out.verdictHidden = el('hero-verdict').hidden;
  out.waitingHidden = el('hero-waiting').hidden;
  out.reason = el('hero-reason').innerHTML;
  out.reasonHidden = el('hero-reason').hidden;
  out.fix = el('hero-fix').innerHTML;
  out.fixHidden = el('hero-fix').hidden;
  out.landed = el('hero-demo').getAttribute('data-verdict');
  out.motion = el('hero-demo').getAttribute('data-motion');
  out.dataSource = el('hero-demo').getAttribute('data-source');
  out.flagged = [0, 1, 2].map(i => el('hero-line-' + i).classList.contains('is-flagged'));
  out.scenarioFix = el('fix-box').innerHTML;
  out.freezeTitle = el('btnEmergencyFreeze').title || '';
""" + scenario,
        tmp_path,
        before=DEMO_DOM
        + LIVE_REFUSAL
        + before
        + "answer = api({ '/status': { status: 200, body: { service: 'Threefold', status: 'HEALTHY' } }, "
        + "'/api/overview': " + overview + ", "
        + "'POST /evaluate-tool-call': " + hero + ", "
        + "'/proof.json': " + proof + " });\n",
    )


def _metrics(markup: str) -> dict:
    return dict(re.findall(r'data-metric="([^"]+)"[^>]*>([^<]*)<', markup))


# ---------------------------------------------------------------- what the markup says


def test_the_hero_leads_with_one_promise_under_twelve_words() -> None:
    hero = _hero()
    heading = re.search(r'<h1 id="hero-promise"[^>]*>(.*?)</h1>', hero, re.S)
    assert heading, "The hero leads with its promise"
    assert _text(heading.group(1)) == PROMISE
    assert len(PROMISE.split()) < 12
    assert page_source("index.html").count("<h1") == 1, "One h1 a page"
    sentence = re.search(r'<p id="hero-sentence"[^>]*>(.*?)</p>', hero, re.S)
    assert sentence and _text(sentence.group(1)) == SENTENCE
    assert hero.index('id="hero-promise"') < hero.index('id="hero-sentence"') < hero.index('id="hero-actions"')


def test_the_actions_come_next_each_one_click_in_this_order() -> None:
    """A judge: the flagship sat some 3,500 px down and nothing on the first screen led to it.

    CLAUDE.md rule 6's loop halt and certificate are one quiet link away now,
    beside connecting a repository, both under the two buttons.
    """
    hero = _hero()
    actions = hero.split('id="hero-actions"', 1)[1].split("</div>", 1)[0]
    found = re.findall(r'<a id="([^"]+)" href="([^"]+)" class="([^"]*)"[^>]*>(.*?)</a>', actions, re.S)
    assert [(ident, href, _text(label)) for ident, href, _, label in found] == ACTIONS
    classes = [set(cls.split()) for _, _, cls, _ in found]
    assert {"tf-btn", "tf-btn-primary"} <= classes[0], "Trying the two-stage rollout is the primary action"
    assert "tf-btn" in classes[1] and "tf-btn-primary" not in classes[1], "The dashboard is the secondary one"
    for quiet in classes[2:]:
        assert "tf-btn" not in quiet and "tf-hero-link" in quiet, "Watching the gates and connecting are quiet text links"
    assert re.search(r'<p class="tf-hero-links">\s*<a id="hero-watch"[^>]*>.*?</a>\s*<a id="hero-connect"', actions, re.S), \
        "The two quiet links share one line under the buttons"
    assert f'<section id="{ACTIONS[2][1][1:]}"' in page_source("index.html"), "The link lands on the scenarios"
    assert "tf-hero-link-down" in classes[2], "Its arrow points down the page, where it goes"


def test_each_length_of_time_is_said_where_it_is_true() -> None:
    """A judge: the button read "— 60 s", and the walkthrough it opens says "About two minutes".

    The walkthrough's own figure goes on the button that opens it. The flagship
    claim of CLAUDE.md rule 6, the circuit breaker's halt and the certificate
    in under 60 seconds, is said beside the two scenarios that make it.
    """
    # The walkthrough wears the design system's page head: its lede is the
    # second argument of the pageTitle() call that titles it.
    walkthrough = re.search(r"pageTitle\('Try the two-stage rollout',\s*'([^']*)'", page_source("dashboard.html"))
    assert walkthrough, "The walkthrough's lede, which gives its length, is not found where it was"
    assert _text(walkthrough.group(1)).startswith("About two minutes on the public demo"), \
        "The walkthrough gives another figure now: the button that opens it must say the same"
    label = {ident: words for ident, _, words in ACTIONS}["hero-try"]
    assert label.endswith("— 2 min") and "60" not in _text(_hero()), "The hero's button says the walkthrough's length"
    # The page's own words are pinned below; of the owner's rule only its figure
    # is checked, so rewording rule 6 does not fail a page that has not changed.
    assert "60 seconds" in (ROOT / "CLAUDE.md").read_text(encoding="utf-8"), "CLAUDE.md rule 6 gives another figure now"
    lede = re.search(r'<p id="gates-lede"[^>]*>(.*?)</p>', _section("watch-the-gates"), re.S)
    assert lede and _text(lede.group(1)).endswith(
        "The circuit breaker halting a loop (Scenario 1) and a governance certificate (Scenario 4) take one click each, "
        "both in under 60 seconds."), "The flagship's time is said where its two scenarios are"


def test_the_sections_come_in_the_order_a_judge_needs_them() -> None:
    """CLAUDE.md rule 6: the circuit breaker trip and the certificate stay on the first screen's page."""
    body = page_source("index.html")
    main = body.split("<main", 1)[1].split("</main>", 1)[0]
    first_element = re.search(r"<(?!!--)[a-z]+[^>]*>", main.split(">", 1)[1]).group(0)
    assert first_element.startswith('<section id="what-threefold-is"'), f"The hero is the first thing in main, not {first_element}"
    positions = [main.index(f'<section id="{ident}"') for ident in SECTIONS]
    assert positions == sorted(positions), "hero, proof, how it works, built on AWS, then the gates"
    gates = _section("watch-the-gates")
    for scenario in ("simulateLoop", "simulateSecret", "simulateBoundary", "simulateCompliant", "simulateUniversalAdapter"):
        assert f'onclick="{scenario}()"' in gates, f"{scenario} left the gates section"
        assert body.index(f'onclick="{scenario}()"') > body.index("</section>", body.index('id="what-threefold-is"'))
    assert 'onclick="triggerManualFreeze()"' in gates and 'onclick="resetDemo()"' in gates
    assert body.index("</main>") < body.index("<footer")


def test_the_hero_says_which_agents_a_refusal_is_measured_to_stop() -> None:
    """The sentence names three agents; the evidence measured all three, unevenly.

    Claude Code and Antigravity refused a Write and no file was created
    (`docs/evidence/ENFORCEMENT_2026-09-21.md`). Codex was measured on
    2026-09-23, on one route in one run: the hook refused an `apply_patch` and
    the file was unchanged, while the shell route was never refused in any run
    (`docs/evidence/ENFORCEMENT_2026-09-23.md`). The hero may not round that up
    to Codex, so the line right under the actions says how far it goes.
    """
    hero = _hero()
    scope = re.search(r'<span id="hero-scope">(.*?)</span>', hero, re.S)
    assert scope, "The hero says what has been measured"
    assert _text(scope.group(1)) == SCOPE
    assert hero.index('id="hero-actions"') < hero.index('id="hero-scope"')


def test_the_hero_writes_no_number_of_its_own() -> None:
    """Only the 2 in the walkthrough's label, its own length: the demonstration is drawn from an answer."""
    hero = _hero()
    assert re.findall(r"\d+", _text(hero)) == ["2"]
    for ident in ("hero-verdict", "hero-reason", "hero-fix"):
        tag = re.search(rf'<(\w+) id="{ident}"[^>]*>(.*?)</\1>', hero, re.S)
        assert tag and " hidden" in tag.group(0).split(">", 1)[0] and tag.group(2).strip() == "", f"{ident} starts hidden and empty"
    assert re.search(r'<ol id="hero-diff"[^>]*></ol>', hero), "The proposed write is drawn from the call, not written into the page"


def test_the_hero_fits_a_phone_without_scrolling_sideways() -> None:
    """375 px: the columns stack, the actions stack and wrap, and nothing is given a width that pushes the page sideways."""
    body = page_source("index.html")
    assert '<meta name="viewport" content="width=device-width, initial-scale=1.0" />' in body
    style = _style()
    assert ".tf-hero { position: relative; display: grid; grid-template-columns: minmax(0, 1fr);" in style
    assert re.search(r"@media \(min-width: 1024px\) \{\s*\.tf-hero \{ grid-template-columns: minmax\(0, 1fr\) minmax\(0, 1fr\);", style), \
        "Side by side only from a desk's width"
    assert ".tf-hero-actions { display: flex; flex-direction: column;" in style
    assert "@media (min-width: 640px) { .tf-hero-actions { flex-direction: row; flex-wrap: wrap;" in style
    assert ".tf-hero-actions .tf-btn { white-space: normal;" in style, "A long label wraps inside its button"
    glow = re.search(r"\.tf-hero::before \{(.*?)\}", style, re.S).group(1)
    assert "left: 0; right: 0;" in glow, "The glow stays inside the hero: past it, a phone's page scrolls sideways"
    assert "overflow-wrap: break-word" in re.search(r"\.tf-hero-title \{(.*?)\}", style, re.S).group(1)
    hero = _hero()
    assert "nowrap" not in hero and "truncate" not in hero
    assert not re.search(r'style="[^"]*\b(?:min-)?width:\s*\d', hero), "A fixed width can push a 375 px screen sideways"


# A browser that draws frames, and says whether its reader asked for less
# motion; the design system's two motion helpers are watched as the page calls
# them. REDUCE is set by each test before this runs.
MOTION = r"""
globalThis.requestAnimationFrame = fn => setTimeout(() => fn(Date.now()), 16);
globalThis.matchMedia = q => ({ matches: REDUCE && q.indexOf('reduce') !== -1, addEventListener() {}, removeListener() {} });
const moved = [];
let layer;
Object.defineProperty(globalThis, 'Threefold', { configurable: true, get() { return layer; }, set(v) {
  const reveal = v.reveal, pulse = v.pulse;
  v.reveal = (nodes, o) => { moved.push(['reveal', Array.from(nodes || []).map(n => n && n.id), o && o.stagger]); return reveal(nodes, o); };
  v.pulse = (node, tone) => { moved.push(['pulse', node && node.id, tone]); return pulse(node, tone); };
  layer = v;
} });
"""


def test_the_demonstration_moves_only_for_a_reader_who_has_not_asked_for_less(tmp_path: Path) -> None:
    """Its motion is the design system's own, which does nothing for a reader who asked for less."""
    style = _style()
    reduced = style.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert ".tf-demo, .tf-demo *" in reduced and "animation: none !important" in reduced
    assert "@keyframes" not in style, "No motion of the page's own: T.reveal and T.pulse, from the design system"
    script = page_source("index.html")
    assert "return !!T && !T.reducedMotion() && typeof window.requestAnimationFrame === 'function';" in script
    seen = r"""
  await new Promise(r => setTimeout(r, 1300)); await tick();
  out.motion = el('hero-demo').getAttribute('data-motion');
  out.phase = el('hero-demo').getAttribute('data-phase');
  out.replayHidden = el('hero-replay').hidden;
  out.moved = moved;
"""
    moving = _load(tmp_path, hero="{ status: 200, body: LIVE_REFUSAL }", before="const REDUCE = false;\n" + MOTION, scenario=seen)
    assert moving["motion"] == "on" and moving["phase"] == "landed" and moving["replayHidden"] is False
    assert ["reveal", ["hero-verdict", "hero-reason", "hero-fix"], 140] in moving["moved"], "The verdict, its reason and its fix rise in, in turn"
    assert ["pulse", "hero-stamp", "danger"] in moving["moved"], "and the refusal's stamp pulses once"
    assert any(m[0] == "reveal" and m[2] == 130 for m in moving["moved"]), "The lines arrive one after another"
    still = _load(tmp_path, hero="{ status: 200, body: LIVE_REFUSAL }", before="const REDUCE = true;\n" + MOTION, scenario=seen)
    assert still["motion"] == "off" and still["phase"] == "landed" and still["moved"] == [], "Nothing moves for a reader who asked for less"
    assert still["replayHidden"] is True, "and there is nothing to replay"


def test_replay_plays_the_answer_again_and_sends_nothing(tmp_path: Path) -> None:
    out = _load(
        tmp_path,
        hero="{ status: 200, body: LIVE_REFUSAL }",
        before="const REDUCE = false;\n" + MOTION,
        scenario=r"""
  await new Promise(r => setTimeout(r, 1300)); await tick();
  const asked = () => calls.filter(c => c.url.indexOf('/evaluate-tool-call') !== -1).length;
  out.askedBefore = asked();
  el('hero-replay').listeners.click();
  el('hero-replay').listeners.click();
  await tick();
  out.during = { phase: el('hero-demo').getAttribute('data-phase'), verdictHidden: el('hero-verdict').hidden,
    waiting: !el('hero-waiting').hidden, words: el('hero-waiting-text').textContent, landed: el('hero-demo').getAttribute('data-verdict') };
  await new Promise(r => setTimeout(r, 1300)); await tick();
  out.after = { phase: el('hero-demo').getAttribute('data-phase'), verdict: el('hero-verdict').innerHTML, waiting: !el('hero-waiting').hidden,
    source: el('hero-source').innerHTML };
  out.askedAfter = asked();
  out.stamps = moved.filter(m => m[0] === 'pulse').length;
""",
    )
    assert out["askedBefore"] == 1 and out["askedAfter"] == 1, "Replay sends no second call"
    during = out["during"]
    assert during["phase"] == "waiting" and during["verdictHidden"] is True and during["landed"] == "pending"
    assert during["waiting"] and during["words"] == "Playing this stack’s answer again; nothing is sent", \
        "While it plays again it says so, and never that it is asking"
    after = out["after"]
    assert after["phase"] == "landed" and "Refused before it was written" in after["verdict"] and not after["waiting"]
    assert re.search(r"Live · \d+ ms", _text(after["source"])), "The same answer, with the round trip it took the first time"
    assert out["stamps"] == 2, "One landing on load and one for the replay, though Replay was pressed twice"


def test_the_header_holds_the_shell_and_the_footer_the_links_and_the_tags() -> None:
    body = page_source("index.html")
    header = body.split('<header class="tf-header">', 1)[1].split("</header>", 1)[0]
    assert '<div id="page-nav" class="tf-nav-mount">' in header
    assert "#workplace-efficiency" not in header and "#community" not in header, "The tags moved to the footer"
    assert '<a class="tf-skip" href="#main">Skip to content</a>' in body
    assert '<main id="main" class="tf-main tf-landing" tabindex="-1">' in body
    footer = body.split("<footer", 1)[1].split("</footer>", 1)[0]
    assert ">#workplace-efficiency</span>" in footer and ">#community</span>" in footer
    links = {_text(label): href for href, label in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', footer, re.S)}
    assert links == {
        "Proof": "dashboard.html#/proof",
        "API": "swagger.html",
        "Architecture": f"{REPOSITORY}/blob/main/docs/ARCHITECTURE.md",
        "Repository": REPOSITORY,
    }


def test_how_it_works_shows_three_steps_one_flow_the_rollout_and_the_three_agents() -> None:
    section = _section("how-it-works")
    steps = re.findall(r'<li class="tf-card tf-step-card">(.*?)</li>', section, re.S)
    assert [_text(re.search(r"<h3>(.*?)</h3>", s).group(1)) for s in steps] == [
        "The hook asks", "Threefold judges, on AWS", "The dashboard shows"]
    assert all('data-tf-icon="' in s for s in steps), "Each step carries its icon"
    flows = re.findall(r'<svg class="tf-diagram-(?:wide|narrow)" viewBox="[^"]+" role="img" aria-labelledby="([^"]+)">', section)
    assert len(flows) == 2, "One flow, drawn across for a desk and down for a phone"
    for labels in flows:
        for ident in labels.split():
            assert f'id="{ident}"' in section, "Each drawing is named and described"
    rollout = section.split('id="rollout"', 1)[1]
    names = [_text(n) for n in re.findall(r'<span class="tf-stage-name">(.*?)</span>\s*<p>', rollout, re.S)]
    assert names == ["Observe", "Review", "Enforce"]
    assert "<strong>Demote</strong> is one click back to Observe" in rollout


def test_each_agent_is_described_in_the_words_of_the_evidence_it_links() -> None:
    section = _section("how-it-works")
    agents = re.findall(r'<li class="tf-card tf-agent">(.*?)</li>', section, re.S)
    assert [re.search(r'<span class="tf-agent-name">(.*?)</span>', a).group(1) for a in agents] == ["Claude Code", "Antigravity", "Codex"]
    for agent in agents:
        (link,) = re.findall(rf'href="{re.escape(REPOSITORY)}/blob/main/(docs/evidence/[^"]+)"', agent)
        evidence = (ROOT / link).read_text(encoding="utf-8")
        for quoted in re.findall(r"“(.+?)”", _text(re.search(r"<blockquote>(.*?)</blockquote>", agent, re.S).group(1))):
            words = quoted.rstrip(".,").replace("…", "").strip()
            assert " ".join(words.split()) in " ".join(evidence.replace("`", "").split()), f"{words!r} is not in {link}"
    assert "tf-chip-amber" in agents[2] and "Measured once, over its patch tool" in agents[2], "Codex is not rounded up"
    assert "shell route is not measured" in _text(agents[2])


def test_the_page_promises_a_fix_only_where_one_can_be_made() -> None:
    """The fix proposer has refusals it proposes nothing for, and fixes it cannot check.

    Scenario 1's loop fix comes back unchecked, and a refusal may bring no fix
    at all, so no sentence on the page may say a checked fix comes with every
    refusal; each one that names a checked fix says it comes where one can be made.
    """
    body = page_source("index.html")
    words = " ".join(html.unescape(body).split()).lower()
    assert "every refusal" not in words and "with each refusal" not in words
    for found in re.finditer(r"\ba checked fix\b", words):
        assert words[found.end():].startswith(" where one can be made"), \
            f"An unqualified promise of a checked fix: …{words[max(0, found.start() - 80):found.end() + 40]}…"
    assert "fix in a replay" in words, "A replay says it carries no fix"


def test_bedrock_is_named_for_both_things_it_does_and_for_no_verdict() -> None:
    """docs/ARCHITECTURE.md: the function asks Bedrock for "page explanations and rule drafts only"."""
    body = page_source("index.html")
    assert "page explanations and rule drafts only" in (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    words = " ".join(html.unescape(body).split())
    assert "only to explain" not in words and "explains only" not in words and "asked only when" not in words
    for ident in ("aws-wide-desc", "aws-narrow-desc"):
        desc = re.search(rf'<desc id="{ident}">(.*?)</desc>', body, re.S).group(1)
        assert "page explanations and rule drafts, never for a verdict" in desc, ident


def test_the_architecture_strip_names_the_services_and_links_the_long_version() -> None:
    section = _section("built-on-aws")
    for drawing in re.findall(r"<svg class=\"tf-diagram-(?:wide|narrow)\".*?</svg>", section, re.S):
        titles = [_text(t) for t in re.findall(r'<text class="dg-title"[^>]*>(.*?)</text>', drawing)]
        for service in ("CloudFront + WAF", "API Gateway", "AWS Lambda", "DynamoDB", "Amazon Bedrock", "EventBridge Scheduler"):
            assert service in titles, f"{service} is missing from a drawing of the stack"
        assert 'role="img"' in drawing and "<desc" in drawing
    assert f'href="{REPOSITORY}/blob/main/docs/ARCHITECTURE.md"' in section
    assert (ROOT / "docs" / "ARCHITECTURE.md").is_file()


def test_the_gates_are_one_picker_and_one_result_panel() -> None:
    gates = _section("watch-the-gates")
    buttons = re.findall(r'<button type="button" id="scenario-(\w+)" class="tf-scenario" aria-pressed="false" onclick="(\w+)\(\)">', gates)
    assert buttons == [("loop", "simulateLoop"), ("secret", "simulateSecret"), ("boundary", "simulateBoundary"),
                       ("compliant", "simulateCompliant"), ("adapter", "simulateUniversalAdapter")]
    assert gates.count('id="result-panel"') == 1 and gates.count('id="terminal-log"') == 1 and gates.count('id="verdict-tag"') == 1
    assert gates.index('id="terminal-log"') < gates.index('id="bedrock-box"'), "The terminal and the verdict share one panel"


# ---------------------------------------------------------------- the hero, running


def test_the_links_carry_the_stage_prefix_when_served(tmp_path: Path) -> None:
    out = run(
        "index.html",
        r"""
  out.links = ['hero-try', 'hero-dashboard', 'hero-connect'].map(id => el(id).href);
""",
        tmp_path,
        before=DEMO_DOM,
    )
    assert out["links"] == [f"https://example.test/prod/{href}" for ident, href, _ in ACTIONS if ident != "hero-watch"]


def test_the_navigation_s_demo_item_opens_the_flagship_scenarios(tmp_path: Path) -> None:
    """A judge: Demo, whose hint says it opens the flagship scenarios, reloaded the top of this page.

    Its fragment rides in the item's page, not in its hash: the navigation
    gives a dashboard route's hash alone on the dashboard, where this one
    would be taken for a route of its own.
    """
    for page, options in (("index.html", "{ active: 'demo' }"), ("settings.html", "{ active: 'settings' }"),
                          ("settings.html", "{ active: 'overview', inDashboard: true }")):
        out = run(page, f"  Threefold.mountNav(el('page-nav'), {options});\n  out.nav = el('page-nav').innerHTML;\n", tmp_path,
                  before=DEMO_DOM if page == "index.html" else "")
        more = out["nav"].split("data-tf-more-menu", 1)[1].split("</div>", 1)[0]
        demo = re.search(r'<a href="([^"]+)" class="tf-menu-item"[^>]*>(?:(?!</a>).)*?</svg>Demo</a>', more, re.S)
        assert demo and demo.group(1) == "https://example.test/prod/index.html#watch-the-gates", f"{page} {options}: {demo and demo.group(1)}"
    assert '<section id="watch-the-gates"' in page_source("index.html")


def test_a_reader_sent_to_the_scenarios_is_left_on_them_once_the_figures_above_fill_in(tmp_path: Path) -> None:
    """The strip and the benchmark card above the scenarios grow after the browser has scrolled to them.

    Chromium keeps them in view on its own; a browser without scroll
    anchoring would leave the reader short of them. Once both reads are in,
    the page brings them back under the header, but never against a reader
    who has scrolled, pressed a key or touched the page in the meantime.
    """
    def landed(hash_: str, meddle: str = "") -> list:
        return run(
            "index.html",
            r"""
  """ + meddle + r"""
  gate.release({ status: 200, body: OVERVIEW });
  await tick();
  out.scrolled = scrolledTo;
""",
            tmp_path,
            before=DEMO_DOM + LIVE_REFUSAL + f"openAt({json.dumps(hash_)});\n"
            + "const scrolledTo = [];\nel('watch-the-gates').scrollIntoView = o => scrolledTo.push(o);\n"
            + "const gate = held();\nconst OVERVIEW = " + _overview() + ";\n"
            + "answer = api({ '/status': { status: 200, body: { service: 'Threefold', status: 'HEALTHY' } }, "
            + "'/api/overview': () => gate.promise, 'POST /evaluate-tool-call': { status: 200, body: LIVE_REFUSAL }, '/proof.json': 'network' });\n",
        )["scrolled"]

    assert landed("#watch-the-gates") == [{"block": "start"}], "Brought back under the header, at once, once"
    assert landed("") == [] and landed("#proof") == [], "Only a reader sent to the scenarios"
    for event in ("wheel", "touchstart", "keydown", "mousedown"):
        assert landed("#watch-the-gates", f"(winListeners[{json.dumps(event)}] || []).forEach(f => f({{}}));") == [], \
            f"A reader who used {event} since the page loaded is left where they are"


def test_the_hero_asks_the_stack_once_and_shows_its_live_answer(tmp_path: Path) -> None:
    out = _load(tmp_path, hero="{ status: 200, body: LIVE_REFUSAL }")
    (asked,) = out["heroAsked"]
    assert asked["url"] == "https://example.test/prod/evaluate-tool-call" and asked["method"] == "POST"
    assert "X-API-Key" not in asked["headers"]
    body = asked["body"]
    assert body["tool_name"] == "write_to_file" and body["action_type"] == "FILE_WRITE"
    assert body["arguments"] == {"TargetFile": "src/domain/user.py", "CodeContent": "import boto3\nclass User:\n  pass"}, \
        "The very call RECORDED.boundary was recorded from"
    assert body["session_id"].startswith("sim-") and body["project_name"] == "Acme-Core"
    assert (body["agent"], body["origin"], body["explain"]) == ("page", "page", False), "A page call that spends no model call"

    assert "Live" in out["source"] and re.search(r"Live · \d+ ms", _text(out["source"])), "The round trip, in milliseconds under a second"
    assert out["caption"] == "Judged just now by this stack through POST /evaluate-tool-call, the route every hook calls.", \
        "The route a hook calls, never 'as a hook': a page call is enforced where a hook's on an observing project is only recorded"
    assert '<span class="tf-nobr">POST /evaluate-tool-call</span>' in out["captionMarkup"], "The route is never broken at a hyphen"
    assert "import boto3" in out["diff"] and "class User:" in out["diff"] and "src/domain/user.py" in out["call"]
    assert not out["verdictHidden"] and out["waitingHidden"]
    assert "Refused before it was written" in out["verdict"]
    reason = _read(out["reason"])
    assert reason == PLAIN_REASON + " " + RULE_LINE and not out["reasonHidden"], "One plain sentence, the rule's id second"
    assert "which matches" not in reason and "Clean Architecture violation:" not in reason, "The service's machine text is not the sentence"
    assert out["landed"] == "refused" and out["flagged"] == [True, False, False], "The verdict lands on the import line"
    assert out["dataSource"] == "live"
    fix = out["fix"]
    assert not out["fixHidden"] and "Checked fix" in _text(fix) and "3 of 3 gate checks passed" in fix
    summary = _text(re.search(r'<p class="tf-demo-fix-summary"[^>]*>(.*?)</p>', fix, re.S).group(1))
    assert summary == "Move boto3 out of the domain behind UserPort; adapter: src/infrastructure/user_adapter.py.", \
        "The service's summary, without a second 'Checked fix:' under a title that already says it"
    assert 'title="Checked fix: move boto3 out of the domain behind UserPort;' in fix, "The service's own words on hover"
    note = re.search(r'<p class="tf-demo-fix-note" title="([^"]*)">(.*?)</p>', fix, re.S)
    assert note and note.group(1) == "src/domain/user.py; src/infrastructure/user_adapter.py (new)", "Each file, and which is new"
    assert _text(note.group(2)) == "2 files proposed · never applied automatically", "A starting point, never applied automatically"
    assert out["motion"] == "off", "With no animation frames the moment lands at once"
    assert out["scenarioFix"] == "" and out["freezeTitle"] == "", "The hero touches neither the scenarios' panel nor their session"


KEPT = "threefold-hero-answer"


def _kept(minutes_ago: float, base: str = "https://example.test/prod", **fields) -> str:
    """A live answer this browser kept `minutes_ago`, as the page stores it."""
    entry = (
        "{ base: " + json.dumps(base) + ", at: Date.now() - " + repr(minutes_ago) + " * 60000, ms: 101, "
        "status: LIVE_REFUSAL.status, reason: LIVE_REFUSAL.reason, "
        "fix: { summary: LIVE_REFUSAL.suggested_fix.summary, validated: true, checks: [{ passed: true }, { passed: true }, { passed: true }], "
        "writes: [{ path: 'src/domain/user.py', new_file: false }, { path: 'src/infrastructure/user_adapter.py', new_file: true }] } }"
    )
    overrides = "".join(f"kept[{json.dumps(key)}] = {value};\n" for key, value in fields.items())
    return "{ const kept = " + entry + ";\n" + overrides + f"store[{json.dumps(KEPT)}] = JSON.stringify(kept); }}\n"


def test_a_live_answer_is_kept_in_this_browser_with_only_what_the_hero_shows(tmp_path: Path) -> None:
    """Every ask is one more refused page call in the ledger the strip and the sessions console read."""
    out = _load(tmp_path, hero="{ status: 200, body: LIVE_REFUSAL }", scenario=f"  out.kept = JSON.parse(store[{json.dumps(KEPT)}]);\n")
    kept = out["kept"]
    assert kept["base"] == "https://example.test/prod", "Kept for the stack that gave it"
    assert (kept["status"], kept["reason"]) == ("BLOCKED_BOUNDARY_VIOLATION", html.unescape(RECORDED_REASON))
    assert isinstance(kept["at"], (int, float)) and isinstance(kept["ms"], (int, float))
    assert kept["fix"]["validated"] is True and len(kept["fix"]["checks"]) == 3
    assert kept["fix"]["writes"] == [{"path": "src/domain/user.py", "new_file": False},
                                     {"path": "src/infrastructure/user_adapter.py", "new_file": True}], \
        "Only what the hero shows: the files' names, never their content"
    for reply in ("'network'", "{ status: 500, body: {} }", "{ status: 200, body: { status: 7 } }"):
        out = _load(tmp_path, hero=reply, scenario=f"  out.kept = store[{json.dumps(KEPT)}] || null;\n")
        assert out["kept"] is None, f"{reply}: a recorded replay is never kept"


def test_a_visit_within_the_hour_shows_the_kept_answer_and_asks_nothing(tmp_path: Path) -> None:
    """A judge: a kept answer's chip read green "Live · 12 min ago" with the stack unreachable.

    "Live" is kept for an answer given on this load. A kept one is shown
    without asking the stack anything, so its chip is neutral and says only
    when the stack answered.
    """
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview() + " }", before=_kept(12),
                scenario="  out.sourceClass = el('hero-source').className;\n  out.sourceTitle = el('hero-source').getAttribute('title');\n")
    assert out["heroAsked"] == [], "A reload adds no refused page call to the ledger"
    assert _text(out["source"]) == "Answered 12 min ago", "The chip says when the answer was given, not a round trip that did not happen now"
    assert "tf-chip-gray" in out["sourceClass"] and "tf-chip-emerald" not in out["sourceClass"], "Not coloured as a live answer"
    assert "live" not in out["sourceTitle"].lower() and out["sourceTitle"].startswith("An answer this stack gave 12 minutes ago")
    assert out["caption"] == "Judged by this stack 12 minutes ago and shown again, so a reload adds no call to its ledger."
    assert out["dataSource"] == "live" and out["landed"] == "refused" and out["flagged"] == [True, False, False]
    assert _read(out["reason"]) == PLAIN_REASON + " " + RULE_LINE
    assert "3 of 3 gate checks passed" in out["fix"] and "src/infrastructure/user_adapter.py (new)" in out["fix"]
    assert _metrics(out["live"])["calls"] == "1,284"


def test_an_old_foreign_or_unreadable_kept_answer_is_not_used(tmp_path: Path) -> None:
    cases = {
        "older than an hour": _kept(61),
        "from another stack": _kept(5, base="https://elsewhere.example.test/prod"),
        "dated in the future": _kept(-5),
        "not a verdict": _kept(5, status="7"),
        "not JSON": f"store[{json.dumps(KEPT)}] = '{{not json';\n",
        "storage that throws": _kept(5) + "storageBlocked = true;\n",
    }
    for name, before in cases.items():
        out = _load(tmp_path, hero="{ status: 200, body: LIVE_REFUSAL }", before=before)
        assert len(out["heroAsked"]) == 1, f"{name}: the stack is asked again"
        assert re.search(r"Live · \d+ ms", _text(out["source"])), f"{name}: and its fresh answer shown"


def test_a_kept_answer_is_escaped_like_any_answer(tmp_path: Path) -> None:
    before = _kept(3, status="'BLOCKED_' + EVIL", reason="EVIL + \" rule '\" + EVIL + \"'\"", fix="{ summary: EVIL, validated: true, writes: [{ path: EVIL, new_file: true }], checks: [{ passed: true }] }")
    out = _load(tmp_path, before=before)
    assert out["heroAsked"] == []
    for markup in (out["verdict"], out["fix"], out["reason"]):
        assert "<img" not in markup and "<svg onload" not in markup, "Storage is data, never markup"
    assert out["fix"].count("&lt;img") >= 2 and out["reason"].count("&lt;img") >= 3


def test_the_hero_asks_once_the_counts_are_read_so_counts_read_in_time_never_include_the_visit(tmp_path: Path) -> None:
    """The hero's call is a refused page call in the ledger; sent alongside the read, the strip could count it.

    The hero waits for the read for HERO_COUNTS_WAIT_MS at most (the next test),
    so this holds for counts that come back within that.
    """
    out = run(
        "index.html",
        r"""
  const heroCalls = () => calls.filter(c => c.url.indexOf('/evaluate-tool-call') !== -1).length;
  out.overviewAsked = calls.filter(c => c.url.indexOf('/api/overview') !== -1).length;
  out.heroWhileCounting = heroCalls();
  out.waitingWhileCounting = !el('hero-waiting').hidden;
  gate.release({ status: 200, body: OVERVIEW });
  await tick();
  out.heroAfter = heroCalls();
  out.order = calls.map(c => c.url).filter(u => u.indexOf('/api/overview') !== -1 || u.indexOf('/evaluate-tool-call') !== -1);
  out.live = el('proof-live').innerHTML;
  out.verdict = el('hero-verdict').innerHTML;
""",
        tmp_path,
        before=DEMO_DOM
        + LIVE_REFUSAL
        + "const gate = held();\nconst OVERVIEW = " + _overview() + ";\n"
        + "answer = api({ '/status': { status: 200, body: { service: 'Threefold', status: 'HEALTHY' } }, "
        + "'/api/overview': () => gate.promise, 'POST /evaluate-tool-call': { status: 200, body: LIVE_REFUSAL }, "
        + "'/proof.json': 'network' });\n",
    )
    assert out["overviewAsked"] == 1 and out["heroWhileCounting"] == 0, "No call of the hero's own while the counts are read"
    assert out["waitingWhileCounting"], "The demonstration says it is asking meanwhile"
    assert out["heroAfter"] == 1, "Once the counts are in, the hero asks, once"
    assert [u.rsplit("/", 1)[-1] for u in out["order"]] == ["overview?days=7", "evaluate-tool-call"]
    assert _metrics(out["live"])["calls"] == "1,284" and "Refused before it was written" in out["verdict"]


def test_a_counts_read_that_never_answers_holds_the_hero_back_under_a_second(tmp_path: Path) -> None:
    """The moment is the first ten seconds: a slow strip may not spend them."""
    out = run(
        "index.html",
        r"""
  const heroCalls = () => calls.filter(c => c.url.indexOf('/evaluate-tool-call') !== -1).length;
  out.before = heroCalls();
  const t0 = Date.now();
  while (!heroCalls() && Date.now() - t0 < 3000) await new Promise(r => setTimeout(r, 25));
  out.waited = Date.now() - t0;
  await tick();
  out.after = heroCalls();
  out.verdict = el('hero-verdict').innerHTML;
  out.strip = el('proof-live').innerHTML;
""",
        tmp_path,
        before=DEMO_DOM
        + LIVE_REFUSAL
        + "const gate = held();\n"
        + "answer = api({ '/status': { status: 200, body: { service: 'Threefold', status: 'HEALTHY' } }, "
        + "'/api/overview': () => gate.promise, 'POST /evaluate-tool-call': { status: 200, body: LIVE_REFUSAL }, "
        + "'/proof.json': 'network' });\n",
    )
    assert out["before"] == 0 and out["after"] == 1
    assert out["waited"] < 1500, f"The hero asked {out['waited']} ms after load, with the counts still unread"
    assert "Refused before it was written" in out["verdict"] and out["strip"] == "", "The verdict landed with the counts still being read"


def test_a_stack_that_does_not_answer_in_time_gives_way_to_the_recorded_run(tmp_path: Path) -> None:
    out = run(
        "index.html",
        r"""
  await new Promise(r => setTimeout(r, 1800));
  out.slow = el('hero-waiting-text').textContent;
  out.slowShown = !el('hero-waiting').hidden;
  await new Promise(r => setTimeout(r, 3600));
  await tick();
  out.caption = el('hero-caption').innerHTML;
  out.dataSource = el('hero-demo').getAttribute('data-source');
  out.verdict = el('hero-verdict').innerHTML;
  out.aborted = !!(signal && signal.aborted);
  out.kept = store['threefold-hero-answer'] || null;
""",
        tmp_path,
        before=DEMO_DOM
        + LIVE_REFUSAL
        + "let signal = null;\n"
        + "answer = api({ '/status': { status: 200, body: { service: 'Threefold', status: 'HEALTHY' } }, "
        + "'/api/overview': { status: 200, body: " + _overview() + " }, "
        + "'POST /evaluate-tool-call': (u, init) => { signal = init.signal; return new Promise(() => {}); }, "
        + "'/proof.json': 'network' });\n",
    )
    assert out["slowShown"] and out["slow"] == "Still waiting for this stack; the recorded run stands in after 4 s", \
        "A wait past a second and a half says what it is waiting for, and for how long"
    assert _read(out["caption"]) == "This stack did not answer within 4 seconds, so this replays the live API’s answer from 2026-09-25."
    assert '<span class="tf-nobr">2026-09-25</span>' in out["caption"], "The date is never broken at a hyphen"
    assert out["dataSource"] == "recorded" and "Refused before it was written" in out["verdict"]
    assert out["aborted"], "The call that lost the race is cancelled"
    assert out["kept"] is None


def test_the_hero_replays_the_recorded_run_and_says_truly_why(tmp_path: Path) -> None:
    """The caption says what this stack did: never 'not contacted' when it answered."""
    run_ = "so this replays the live API’s answer from 2026-09-25."
    cases = {
        "unreachable": ("'network'", "This stack could not be reached, " + run_),
        "an error": ("{ status: 500, body: { title: 'Internal' } }", "This stack answered HTTP 500, not a verdict, " + run_),
        "a private stack": ("{ status: 401, body: { title: 'Unauthorized' } }",
                            "This stack judges only its operator’s calls (HTTP 401), " + run_),
        "a forbidden call": ("{ status: 403, body: { title: 'Forbidden' } }",
                             "This stack judges only its operator’s calls (HTTP 403), " + run_),
        "a rate limit": ("{ status: 429, body: { title: 'Too Many Requests' } }",
                         "This stack is limiting this browser’s calls (HTTP 429), " + run_),
        "not a verdict": ("{ status: 200, body: { status: 7 } }",
                          "This stack answered, but not with a verdict this page can read, " + run_),
    }
    for name, (reply, caption) in cases.items():
        out = _load(tmp_path, hero=reply)
        chip = _text(out["source"])
        if name == "unreachable":
            assert chip == "Recorded 2026-09-25, replayed offline", name
        else:
            assert chip == "Recorded 2026-09-25, replayed", f"{name}: a stack that answered was reached, so the chip does not say offline"
        assert out["dataSource"] == "recorded", f"{name}: a phone gives the recorded label the bar's room"
        assert out["caption"] == caption, name
        assert "without contacting it" not in out["caption"], f"{name}: the stack was asked"
        assert _read(out["reason"]) == PLAIN_REASON + " " + RULE_LINE, name
        assert "Refused before it was written" in out["verdict"] and out["flagged"] == [True, False, False], name
        assert "No fix in a replay" in out["fix"] and "Checked fix" not in out["fix"] and "gate checks" not in out["fix"], name
        assert '<a id="hero-ask-again" class="tf-link" href="#scenario-boundary">Ask the stack again in Scenario 3</a>' in out["fix"], \
            f"{name}: a replay invents no fix, and says where one is asked for"


def test_asking_again_from_a_replay_checks_the_stack_and_runs_scenario_three(tmp_path: Path) -> None:
    """The replay's one way on runs the scenario it names, and asks the stack rather than replaying blindly.

    The scenarios ask the stack only when the last /status check found it
    answering; a replay means the load's check may not have, so the link checks
    again before it runs Scenario 3, and the scenario then brings its result
    into view under the header, as every scenario does.
    """
    out = _load(
        tmp_path,
        hero="'network'",
        scenario=r"""
  answer = api({ '/status': { status: 200, body: { service: 'Threefold', status: 'HEALTHY' } },
    'POST /evaluate-tool-call': { status: 200, body: LIVE_REFUSAL } });
  liveBackendActive = false;
  const before = calls.length;
  const scrolled = [];
  el('result-panel').scrollIntoView = o => scrolled.push(o);
  el('result-panel').getBoundingClientRect = () => ({ top: 2400, bottom: 3000 });
  globalThis.innerHeight = 812;
  let focused = null;
  el('scenario-boundary').focus = o => { focused = o; };
  let prevented = false;
  el('hero-fix').listeners.click({ target: { closest: s => s === '#hero-ask-again' ? {} : null }, preventDefault() { prevented = true; } });
  await tick();
  out.asked = calls.slice(before).map(c => c.method + ' ' + c.url.replace('https://example.test/prod', ''));
  out.prevented = prevented;
  out.focused = focused;
  out.scrolled = scrolled;
  out.pressed = el('scenario-boundary').getAttribute('aria-pressed');
  out.tag = el('verdict-tag').innerText;
  out.scenarioFix = el('fix-box').innerHTML;
  let other = false;
  el('hero-fix').listeners.click({ target: { closest: () => null }, preventDefault() { other = true; } });
  out.otherClickPrevented = other;
""",
    )
    assert out["asked"][:2] == ["GET /status", "POST /evaluate-tool-call"], "The stack is checked, then asked"
    assert out["prevented"] and out["focused"] == {"preventScroll": True}, "Focus moves to Scenario 3 without a jump of its own"
    assert out["pressed"] == "true" and out["tag"] == "BLOCKED_BOUNDARY_VIOLATION" and "Move boto3 out of the domain" in out["scenarioFix"]
    assert out["scrolled"] and out["scrolled"][0] == {"block": "start"}, \
        "The result is brought under the header at once, as a link's target is: a glide down the whole page is too far to follow"
    assert out["otherClickPrevented"] is False, "Any other click in the fix box is left alone"
    style = _style()
    assert ".tf-landing > section, .tf-scenario { scroll-margin-top: calc(var(--tf-header-h) + 16px); }" in style, \
        "Without script the link still lands the card clear of the sticky header"


def test_a_kept_answer_is_labelled_as_one_before_its_verdict_lands(tmp_path: Path) -> None:
    """While a kept answer's lines arrive, nothing on the card says the stack is being asked."""
    out = _load(
        tmp_path,
        before="const REDUCE = false;\n" + MOTION + _kept(12),
        scenario=r"""
  out.early = { source: el('hero-source').innerHTML, caption: el('hero-caption').innerHTML,
    waiting: el('hero-waiting-text').textContent, landed: el('hero-demo').getAttribute('data-verdict') };
""",
    )
    early = out["early"]
    assert early["landed"] is None, "Measured before the verdict lands"
    assert _text(early["source"]) == "Answered 12 min ago" and "Asking" not in early["source"]
    assert _read(early["caption"]).startswith("Judged by this stack 12 minutes ago") and "Asking" not in early["caption"]
    assert early["waiting"] == "Showing the answer this stack gave 12 minutes ago"
    assert out["heroAsked"] == []


def test_the_hero_shows_an_answer_that_is_not_a_refusal_as_what_it_is(tmp_path: Path) -> None:
    out = _load(tmp_path, hero="{ status: 200, body: { status: 'APPROVED', reason: 'All deterministic governance invariants satisfied' } }")
    assert "Refused before it was written" not in out["verdict"]
    assert "Approved" in out["verdict"] and out["landed"] == "approved" and out["flagged"] == [False, False, False]
    assert _read(out["reason"]) == "All deterministic governance invariants satisfied. APPROVED"
    assert out["fixHidden"] and "fix" not in _text(out["fix"]).lower(), "An approved write needs no fix, so none is promised"
    out = _load(tmp_path, hero="{ status: 200, body: { status: 'BLOCKED_BOUNDARY_VIOLATION', reason: 'Refused.' } }")
    assert "No fix came with this answer" in out["fix"] and not out["fixHidden"], "A refusal that brought no fix says so"


def test_the_hero_escapes_everything_the_stack_says(tmp_path: Path) -> None:
    out = _load(
        tmp_path,
        hero="{ status: 200, body: { status: 'BLOCKED_' + EVIL, reason: EVIL + \" rule '\" + EVIL + \"'\", "
        "suggested_fix: { summary: EVIL, validated: true, writes: [{ path: EVIL, content: EVIL, new_file: true }], checks: [{ passed: true }] } } }",
    )
    for markup in (out["verdict"], out["fix"], out["reason"]):
        assert "<img" not in markup and "<svg onload" not in markup, "Service data reached the page as markup"
    assert out["fix"].count("&lt;img") >= 2
    assert out["reason"].count("&lt;img") >= 3, "The sentence, the rule and the status, each as text"
    assert '"><svg' not in out["reason"], "Not even inside the attribute that keeps the service's own words"


# ---------------------------------------------------------------- the proof strip


def test_the_counts_are_read_from_the_overview_at_load_and_say_what_they_are_made_of(tmp_path: Path) -> None:
    sources = "sources: { fleet: { calls: 1102, projects: 6 }, sandbox: { calls: 120, projects: 9 }, other: { calls: 62, projects: 3 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(sources) + " }")
    (asked,) = out["overviewAsked"]
    assert asked["url"] == "https://example.test/prod/api/overview?days=7" and asked["method"] == "GET"
    assert "X-API-Key" not in asked["headers"], "No key is sent unless someone typed one"
    assert _metrics(out["live"]) == {"calls": "1,284", "refused": "37", "would_refuse": "57"}
    assert "Calls judged" in out["live"] and "Stopped" in out["live"] and "Would have been stopped" in out["live"]
    assert 'href="https://example.test/prod/dashboard.html#/calls?days=7&amp;kind=refused"' in out["live"], "Each tile opens its rows"
    assert "totals." not in out["live"], "A reader is shown words, not the API's field names"
    subs = [_text(s) for s in re.findall(r'<span class="tf-tile-sub">(.*?)</span>', out["live"], re.S)]
    assert subs == ["in the last 7 days", "before they ran", "recorded while a project observes"], \
        "Each sub-line says what its number means, and the window is named once"
    terms = [_text(t) for t in re.findall(r'<span class="tf-tile-term">(.*?)</span>', out["live"], re.S)]
    assert terms == ["Refused", "Would refuse"], "Plain words first, then the dashboard's own term for the same calls"
    assert "Open the refused calls" in out["live"] and "Open the would-refuse calls" in out["live"], "Each link says which rows it opens, in the dashboard's words"
    assert not out["whereHidden"]
    assert _read(out["where"]) == (
        "Where they come from: 1,102 from a synthetic Acme fleet run through the real gates, 120 from visitors’ "
        "sandboxes, 62 from other API callers such as probes and page demos."
    ), "One clause, where a reader first meets the numbers, and the fleet called synthetic"


def test_the_stopped_tile_says_how_many_of_its_refusals_are_in_this_pages_demo_project(tmp_path: Path) -> None:
    """The hero's call and Scenario 3 are refused into Acme-Core, one per visit, and the tile opens those very rows.

    So the number stays the stack's own count, which is what its link lists,
    and the line under it says how many of them are in the page's demo
    project, from that project's own row in by_project. It names the project
    rather than the sender: anyone calling the open API may write to Acme-Core
    too, and Scenarios 1 and 2 record elsewhere.
    """
    def stopped_sub(by_project: str) -> str:
        out = _load(tmp_path, overview="{ status: 200, body: " + _overview("by_project: " + by_project, refused=14).replace("by_project: [], ", "", 1) + " }")
        subs = [_text(s) for s in re.findall(r'<span class="tf-tile-sub">(.*?)</span>', out["live"], re.S)]
        assert _metrics(out["live"])["refused"] == "14", "The tile's number is the count its rows list"
        return subs[1]

    rows = "[{ project: 'Acme-Payments', calls: 900, refused: 4 }, { project: 'Acme-Core', calls: 12, refused: 10 }]"
    assert stopped_sub(rows) == "before they ran; 10 in this page’s demo project"
    assert stopped_sub("[{ project: 'Acme-Core', calls: 3, refused: 1 }]") == "before they ran; 1 in this page’s demo project"
    for none in ("[{ project: 'Acme-Payments', calls: 900, refused: 14 }]", "[{ project: 'Acme-Core', calls: 4, refused: 0 }]",
                 "[{ project: 'Acme-Core', calls: 40, refused: 40 }]", "[{ project: 'Acme-Core', refused: '<img src=x>' }]", "'many'"):
        assert stopped_sub(none) == "before they ran", f"{none}: no share that is not a count within the tile's own"


# The four sources as the stack reports them, and the project rows they are
# counted from: 1,284 calls, 37 refused and 57 would-refuse, as the totals say.
SPLIT_SOURCES = ("sources: { fleet: { calls: 1102, projects: 2 }, live: { calls: 40, projects: 1 }, "
                 "sandbox: { calls: 80, projects: 1 }, other: { calls: 62, projects: 2 } }")
SPLIT_ROWS = [
    {"project": "Acme-Live-billing-credit-limit", "source": "live", "calls": 40, "refused": 2, "would_refuse": 0},
    {"project": "Acme-Checkout", "source": "fleet", "calls": 700, "refused": 10, "would_refuse": 30},
    {"project": "Acme-Mobile", "source": "fleet", "calls": 402, "refused": 5, "would_refuse": 20},
    {"project": "Acme-Sandbox-0a1b2c3d", "source": "sandbox", "calls": 80, "refused": 3, "would_refuse": 7},
    {"project": "Acme-Probe", "source": "other", "calls": 50, "refused": 14, "would_refuse": 0},
    {"project": "Acme-Core", "source": "other", "calls": 12, "refused": 3, "would_refuse": 0},
]


def _split_load(tmp_path: Path, rows: list, sources: str = SPLIT_SOURCES, **totals) -> dict:
    """The page loaded against an overview with these project rows."""
    body = _overview(sources + ", by_project: " + json.dumps(rows), **totals).replace("by_project: [], ", "", 1)
    return _load(tmp_path, overview="{ status: 200, body: " + body + " }")


def _split_subs(tmp_path: Path, rows: list, sources: str = SPLIT_SOURCES, **totals) -> tuple:
    """The Stopped and Would-have-been-stopped sub-lines for these project rows, and the strip's markup."""
    out = _split_load(tmp_path, rows, sources, **totals)
    subs = [_text(s) for s in re.findall(r'<span class="tf-tile-sub">(.*?)</span>', out["live"], re.S)]
    return subs[1], subs[2], out["live"]


def test_the_stopped_and_would_refuse_tiles_say_where_their_counts_come_from(tmp_path: Path) -> None:
    """A review read "Stopped 346 before they ran" as governed work; most of it was probes and the synthetic fleet.

    Each of the two tiles now splits its own number by source, from the
    by_project rows the stack counts it from, real agent runs first, and
    only when the parts add up to the tile's number. The fleet is called
    synthetic on the tile itself, not only in the sentence under it.
    """
    stopped, observed, markup = _split_subs(tmp_path, SPLIT_ROWS)
    assert stopped == ("before they ran: 2 in real agent runs (see below), 15 from the synthetic fleet, 3 from visitors’ sandboxes, "
                       "17 from other callers such as probes and page demos")
    assert observed == "recorded while a project observes: 50 from the synthetic fleet, 7 from visitors’ sandboxes", \
        "Parts with nothing in them are left out"
    assert _metrics(markup) == {"calls": "1,284", "refused": "37", "would_refuse": "57"}, "The tiles' numbers are the stack's own counts"
    assert "in this page’s demo project" not in markup, "Its refusals are within the probes and page demos part"
    assert 'aria-label="Stopped: 37. before they ran: 2 in real agent runs (see below),' in markup, "The link's label reads the same words"


def test_real_runs_are_named_as_such_only_when_the_stack_counts_them_so(tmp_path: Path) -> None:
    """A live project's row counts every caller in it; `sources.live` counts only Claude Code and Codex.

    When the two disagree on the calls, some of the project's calls were not
    the real agents', so its refusals are placed in the projects the real
    agents report to rather than credited to their runs. When real runs were
    judged and none was refused, the Stopped tile says so, and the
    Would-refuse tile, whose projects never observe, does not.
    """
    other_callers = [dict(row, calls=44) if row["source"] == "live" else row for row in SPLIT_ROWS]
    stopped, _, _ = _split_subs(tmp_path, other_callers)
    assert stopped.startswith("before they ran: 2 in the projects real agents report to (see below), 15 from the synthetic fleet")
    none_refused = [dict(row, refused=0) if row["source"] == "live" else dict(row, refused=16) if row["project"] == "Acme-Probe" else row
                    for row in SPLIT_ROWS]
    stopped, observed, _ = _split_subs(tmp_path, none_refused)
    assert stopped.startswith("before they ran: none from real agent runs, 15 from the synthetic fleet")
    assert "real agent" not in observed
    no_live = "sources: { fleet: { calls: 1102 }, live: { calls: 0 }, sandbox: { calls: 80 }, other: { calls: 102 } }"
    rows = [dict(row, source="other") if row["source"] == "live" else row for row in none_refused]
    stopped, _, _ = _split_subs(tmp_path, rows, sources=no_live)
    assert "real agent" not in stopped, "With no real run in the window, none is mentioned"


def test_a_refusal_in_real_agent_runs_is_counted_not_offered_as_a_right_one(tmp_path: Path) -> None:
    """A review: the first real refusal on the public stack was a false alarm, and the tile read it as a stop.

    STATE.md: the one refusal of the first live run was a PowerShell read taken
    for a write. The overview says nothing of a refused call's review, so the
    tile counts such refusals and points below, and the sentence under the
    tiles says a refusal can be wrong and opens each project's refused rows,
    where the reason and any review are.
    """
    out = _split_load(tmp_path, SPLIT_ROWS)
    assert _read(out["where"]).endswith(
        "A refusal can be wrong. Real agent runs’ refusals, each with its reason and any review: "
        "2 refused calls in Acme-Live-billing-credit-limit."
    )
    links = re.findall(r'<a class="tf-link" href="([^"]+)">(.*?)</a>', out["where"], re.S)
    assert links == [("https://example.test/prod/dashboard.html#/calls?days=7&amp;kind=refused&amp;project=Acme-Live-billing-credit-limit",
                      "2 refused calls in Acme-Live-billing-credit-limit")], "The link opens that project's refused rows, in the tile's window"
    two = SPLIT_ROWS + [{"project": "Acme-Live-warehouse-carrier-notify", "source": "live", "calls": 8, "refused": 1, "would_refuse": 0}]
    two = [dict(row, refused=13) if row["project"] == "Acme-Probe" else row for row in two]
    sources = SPLIT_SOURCES.replace("live: { calls: 40, projects: 1 }", "live: { calls: 48, projects: 2 }").replace("other: { calls: 62", "other: { calls: 54")
    out = _split_load(tmp_path, two, sources=sources)
    assert _read(out["where"]).endswith(
        "each with its reason and any review: 2 refused calls in Acme-Live-billing-credit-limit and "
        "1 refused call in Acme-Live-warehouse-carrier-notify."
    ), "One link a project"
    other_callers = [dict(row, calls=44) if row["source"] == "live" else row for row in SPLIT_ROWS]
    out = _split_load(tmp_path, other_callers)
    assert "Refusals in the projects real agents report to, each with its reason and any review: 2 refused calls in" in _read(out["where"])
    none_refused = [dict(row, refused=0) if row["source"] == "live" else dict(row, refused=16) if row["project"] == "Acme-Probe" else row
                    for row in SPLIT_ROWS]
    out = _split_load(tmp_path, none_refused)
    assert "can be wrong" not in out["where"] and "<a " not in out["where"], "With no refusal in real runs, nothing points to one"
    more = [dict(row, refused=100) if row["project"] == "Acme-Probe" else row for row in SPLIT_ROWS]
    out = _split_load(tmp_path, more)
    assert "can be wrong" not in out["where"], "Where the tile gives no split, the sentence points to nothing"


def test_a_real_run_project_name_reaches_the_link_as_text(tmp_path: Path) -> None:
    hostile = 'Acme-Live-"><img src=x onerror=alert(1)>'
    rows = [dict(row, project=hostile) if row["source"] == "live" else row for row in SPLIT_ROWS]
    out = _split_load(tmp_path, rows)
    assert "<img" not in out["where"] and "onerror=alert(1)>" not in out["where"], "Service data reached the page as markup"
    assert "&lt;img src=x onerror=alert(1)&gt;" in out["where"], "The name is shown, escaped"
    assert "project=Acme-Live-%22%3E%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E" in out["where"], "and carried in the link encoded"
    for missing in (None, "", 7):
        rows = [dict(row, project=missing) if row["source"] == "live" else row for row in SPLIT_ROWS]
        stopped, _, _ = _split_subs(tmp_path, rows)
        assert stopped == "before they ran; 3 in this page’s demo project", f"project {missing!r}: a refusal it cannot point to gives no split"


def test_a_split_that_would_contradict_its_tile_is_not_given(tmp_path: Path) -> None:
    more = [dict(row, refused=100) if row["project"] == "Acme-Probe" else row for row in SPLIT_ROWS]
    stopped, observed, _ = _split_subs(tmp_path, more)
    assert stopped == "before they ran; 3 in this page’s demo project", "Parts that add up to more than 37 give no figure"
    assert observed.startswith("recorded while a project observes: 50 from the synthetic fleet"), "Each tile is judged on its own parts"
    for hostile in ("'<img src=x onerror=alert(1)>'", "2.5", "-4", "null"):
        rows = json.dumps(SPLIT_ROWS).replace('"refused": 14', '"refused": ' + hostile.replace("'", '"'))
        body = _overview(SPLIT_SOURCES + ", by_project: " + rows).replace("by_project: [], ", "", 1)
        out = _load(tmp_path, overview="{ status: 200, body: " + body + " }")
        subs = [_text(s) for s in re.findall(r'<span class="tf-tile-sub">(.*?)</span>', out["live"], re.S)]
        assert "<img" not in out["live"] and "onerror" not in out["live"], "Service data reached the page as markup"
        assert subs[1] == "before they ran; 3 in this page’s demo project", f"refused {hostile}: a part that is not a count gives no split"
    unknown = [dict(row, source="<img src=x>") if row["project"] == "Acme-Probe" else row for row in SPLIT_ROWS]
    stopped, _, markup = _split_subs(tmp_path, unknown)
    assert stopped == "before they ran; 3 in this page’s demo project" and "<img" not in markup, "A source the page does not know gives no split"


def test_a_day_with_no_call_between_days_with_some_is_named(tmp_path: Path) -> None:
    """The line reads zero on such a day; the page cannot tell a quiet day from an uncounted one, so it names the day."""
    def first_sub(series: str) -> str:
        out = _load(tmp_path, overview="{ status: 200, body: " + _overview("series: [" + series + "]").replace("series: [], ", "", 1) + " }")
        return [_text(s) for s in re.findall(r'<span class="tf-tile-sub">(.*?)</span>', out["live"], re.S)][0]

    day = lambda d, n: f"{{ day: '2026-09-{d}', approved: {n}, observed: 0, refused: 0 }}"  # noqa: E731
    assert first_sub(", ".join([day(20, 5), day(21, 0), day(22, 3)])) == "in the last 7 days; none counted on 21 Sep"
    assert first_sub(", ".join([day(20, 5), day(21, 0), day(22, 0), day(23, 3)])) == "in the last 7 days; none counted on 21 Sep and 22 Sep"
    assert first_sub(", ".join([day(19, 5), day(20, 0), day(21, 0), day(22, 0), day(23, 3)])) == "in the last 7 days; none counted on 3 of the days"
    assert first_sub(", ".join([day(20, 0), day(21, 5), day(22, 3), day(23, 0)])) == "in the last 7 days", \
        "Days before the first count and after the last are the window's edges, not days missing inside it"


def test_without_sources_the_sandboxes_are_still_told_apart(tmp_path: Path) -> None:
    split = "sandbox_split: { sandbox: { calls: 24, projects: 2 }, elsewhere: { calls: 1260, projects: 10 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(split) + " }")
    assert "24 of the 1,284 calls from visitors’ sandboxes, the rest from probes" in _text(out["where"])
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview() + " }")
    assert _read(out["where"]) == "Where they come from: probes, visitors’ sandboxes, page demos and other API callers."
    hostile = "sources: { fleet: { calls: '<img src=x onerror=alert(1)>' }, sandbox: { calls: 1 }, other: { calls: 1 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(hostile) + " }")
    assert "<img" not in out["where"] and "synthetic Acme fleet" not in out["where"], "A source that is not a count is not used"
    mismatched = "sources: { fleet: { calls: 900 }, sandbox: { calls: 120 }, other: { calls: 62 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(mismatched) + " }")
    where = _text(out["where"])
    assert "900" not in where and "1,284" not in where and "120" not in where, "Parts that do not add up to the 1,284 calls give no figure"
    assert "Where they come from: a synthetic Acme fleet run through the real gates, visitors’ sandboxes" in where, \
        "A fleet the stack reports is named in words even then: synthetic calls are never left unlabelled"
    nothing = "sources: { fleet: { calls: 0 }, sandbox: { calls: 0 }, other: { calls: 0 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(nothing) + " }")
    assert "synthetic Acme fleet" not in _text(out["where"]) and "probes, visitors’ sandboxes, page demos" in _text(out["where"]), \
        "Parts that add up to nothing are not used, and no fleet is claimed"


def test_the_daily_live_agent_is_counted_as_real_runs(tmp_path: Path) -> None:
    """Its calls are real Claude Code or Codex runs on Acme tasks in projects that enforce: said so, with its count.

    No cadence is claimed. STATE.md: until the owner creates the daily schedule, a
    day runs only when the script is started by hand, so "one Acme task a day" was
    a promise the stack does not yet keep.
    """
    sources = ("sources: { fleet: { calls: 1102, projects: 6 }, live: { calls: 40, projects: 2 }, "
               "sandbox: { calls: 80, projects: 9 }, other: { calls: 62, projects: 3 } }")
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(sources) + " }")
    assert _read(out["where"]) == (
        "Where they come from: 1,102 from a synthetic Acme fleet run through the real gates, 40 from real Claude Code "
        "or Codex runs on Acme tasks in projects that enforce, 80 from visitors’ sandboxes, 62 from other API callers "
        "such as probes and page demos."
    ), "The four parts add up to the 1,284 calls, so each is given, the live agent's called real"
    mismatched = sources.replace("calls: 40", "calls: 400")
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(mismatched) + " }")
    assert _read(out["where"]) == (
        "Where they come from: a synthetic Acme fleet run through the real gates, real Claude Code or Codex runs on "
        "Acme tasks in projects that enforce, visitors’ sandboxes, probes, page demos and other API callers."
    ), "Parts that do not add up give no figure, and the live agent is still named"
    only_live = "sources: { fleet: { calls: 0 }, live: { calls: 1284 }, sandbox: { calls: 0 }, other: { calls: 0 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(only_live) + " }")
    assert _read(out["where"]) == (
        "Where they come from: 1,284 from real Claude Code or Codex runs on Acme tasks in projects that enforce."
    )


def test_a_stack_without_live_or_with_none_reads_as_before(tmp_path: Path) -> None:
    """A stack from before live counts those calls as other; a window with no live run names none."""
    before = ("Where they come from: 1,102 from a synthetic Acme fleet run through the real gates, 120 from visitors’ "
              "sandboxes, 62 from other API callers such as probes and page demos.")
    older = "sources: { fleet: { calls: 1102, projects: 6 }, sandbox: { calls: 120, projects: 9 }, other: { calls: 62, projects: 3 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(older) + " }")
    assert _read(out["where"]) == before, "Without a live part the three parts still add up, and are given"
    zero = older.replace("sandbox:", "live: { calls: 0, projects: 0 }, sandbox:")
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(zero) + " }")
    assert _read(out["where"]) == before, "A live agent with no calls in the window is not mentioned"
    assert "Claude Code" not in out["where"]


def test_a_live_part_that_is_not_a_count_gives_no_figures(tmp_path: Path) -> None:
    for value in ("'<img src=x onerror=alert(1)>'", "2.5", "-4", "null", "'40'"):
        hostile = ("sources: { fleet: { calls: 1102 }, live: { calls: " + value + " }, "
                   "sandbox: { calls: 120 }, other: { calls: 62 } }")
        out = _load(tmp_path, overview="{ status: 200, body: " + _overview(hostile) + " }")
        where = _read(out["where"])
        assert "<img" not in out["where"] and "onerror" not in out["where"], "Service data reached the page as markup"
        assert "1,102" not in where and "Claude Code" not in where, f"live {value}: parts with one that is not a count give no figure"
        assert where == ("Where they come from: a synthetic Acme fleet run through the real gates, visitors’ sandboxes, "
                         "probes, page demos and other API callers."), "The fleet is still named in words"
    escaped = "sources: { fleet: { calls: 0 }, live: '<img src=x onerror=alert(1)>', sandbox: { calls: 0 }, other: { calls: 1284 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(escaped) + " }")
    assert "<img" not in out["where"] and "Claude Code" not in out["where"]


# The same window, as a stack answers it once the service's own probes are a
# source of their own: Acme-Probe's 50 calls, 14 of them refused, move out of
# other, which keeps the page's demo project.
PROBE_SOURCES = SPLIT_SOURCES.replace("other: { calls: 62, projects: 2 }",
                                      "probe: { calls: 50, projects: 1 }, other: { calls: 12, projects: 1 }")
PROBE_ROWS = [dict(row, source="probe") if row["project"] == "Acme-Probe" else row for row in SPLIT_ROWS]


def test_the_service_s_own_probes_are_named_synthetic_never_real(tmp_path: Path) -> None:
    """A judge: the probes' calls were counted as other callers', beside real ones. They are named for what they are."""
    out = _split_load(tmp_path, PROBE_ROWS, sources=PROBE_SOURCES)
    assert _read(out["where"]).startswith(
        "Where they come from: 1,102 from a synthetic Acme fleet run through the real gates, 40 from real Claude Code "
        "or Codex runs on Acme tasks in projects that enforce, 80 from visitors’ sandboxes, 50 from the service’s own "
        "synthetic probes, 12 from page demos and other API callers."
    ), "The five parts add up to the 1,284 calls, and the other callers no longer claim the probes"
    stopped, observed, markup = _split_subs(tmp_path, PROBE_ROWS, sources=PROBE_SOURCES)
    assert stopped == ("before they ran: 2 in real agent runs (see below), 15 from the synthetic fleet, 3 from visitors’ "
                       "sandboxes, 14 from the service’s own synthetic probes, 3 from page demos and other callers")
    assert observed == "recorded while a project observes: 50 from the synthetic fleet, 7 from visitors’ sandboxes"
    assert _metrics(markup) == {"calls": "1,284", "refused": "37", "would_refuse": "57"}, "The tiles' numbers are the stack's own counts"
    for words in (_read(out["where"]), stopped):
        probes = words.split("synthetic probes")[0].rsplit(",", 1)[-1]
        assert "real" not in probes, f"The probes are never called real: {probes!r}"


def test_probes_the_stack_reports_are_named_even_when_their_parts_give_no_figure(tmp_path: Path) -> None:
    mismatched = PROBE_SOURCES.replace("probe: { calls: 50", "probe: { calls: 500")
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(mismatched) + " }")
    assert _read(out["where"]) == (
        "Where they come from: a synthetic Acme fleet run through the real gates, real Claude Code or Codex runs on "
        "Acme tasks in projects that enforce, the service’s own synthetic probes, visitors’ sandboxes, page demos and "
        "other API callers."
    ), "Parts that do not add up give no figure, and the probes are still named as synthetic"
    only_probes = "sources: { fleet: { calls: 0 }, live: { calls: 0 }, probe: { calls: 9 }, sandbox: { calls: 0 }, other: { calls: 1 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(only_probes) + " }")
    assert _read(out["where"]) == (
        "Where they come from: the service’s own synthetic probes, visitors’ sandboxes, page demos and other API callers."
    )


def test_a_probe_part_that_is_not_a_count_gives_no_figures(tmp_path: Path) -> None:
    for value in ("'<img src=x onerror=alert(1)>'", "2.5", "-4", "null", "'50'"):
        hostile = PROBE_SOURCES.replace("probe: { calls: 50", "probe: { calls: " + value)
        out = _load(tmp_path, overview="{ status: 200, body: " + _overview(hostile) + " }")
        where = _read(out["where"])
        assert "<img" not in out["where"] and "onerror" not in out["where"], "Service data reached the page as markup"
        assert "1,102" not in where, f"probe {value}: parts with one that is not a count give no figure"
        assert where == ("Where they come from: a synthetic Acme fleet run through the real gates, real Claude Code or "
                         "Codex runs on Acme tasks in projects that enforce, visitors’ sandboxes, probes, page demos and "
                         "other API callers."), "The fleet and the live agent are still named in words"


def test_a_stack_that_does_not_run_the_fleet_still_counts_probes_among_the_other_callers(tmp_path: Path) -> None:
    """Only the fleet's stack tells the probes' project apart; elsewhere sources.probe is 0 and Acme-Probe is other."""
    private = ("sources: { fleet: { calls: 0, projects: 0 }, live: { calls: 0, projects: 0 }, probe: { calls: 0, projects: 0 }, "
               "sandbox: { calls: 80, projects: 1 }, other: { calls: 62, projects: 2 } }")
    rows = [row for row in SPLIT_ROWS if row["source"] in ("sandbox", "other")]
    out = _split_load(tmp_path, rows, sources=private, calls=142, refused=20, would_refuse=7)
    assert _read(out["where"]) == (
        "Where they come from: 80 from visitors’ sandboxes, 62 from other API callers such as probes and page demos."
    ), "A probe count of 0 on a stack without the fleet does not take the probes out of the other callers"
    stopped, _, _ = _split_subs(tmp_path, rows, sources=private, calls=142, refused=20, would_refuse=7)
    assert stopped == "before they ran: 3 from visitors’ sandboxes, 17 from other callers such as probes and page demos"


# Every source the overview can report, with a count a reader can tell apart.
COMBINATION_PARTS = {"fleet": 1102, "live": 40, "probe": 50, "sandbox": 80, "other": 12}


def test_the_sentence_reads_as_one_clause_a_source_whichever_sources_are_there(tmp_path: Path) -> None:
    """A judge read "in projects that enforce, 325 from probes" as two sources.

    The sentence separates its sources with commas, so no source's own words
    may carry one. Every combination of the five sources is written, with the
    fleet's stack telling the probes apart and without, and with figures and
    without: each source present is said once, in one clause with its count
    when there is one, and in the order the sentence gives them.
    """
    cases = []
    for size in range(1, len(COMBINATION_PARTS) + 1):
        for present in itertools.combinations(COMBINATION_PARTS, size):
            # A fleet in the window tells the probes apart; without one, a
            # stack that runs the fleet still does, and one that does not, not.
            for fleet_projects in ((6,) if "fleet" in present else (0, 6)):
                calls = {name: COMBINATION_PARTS[name] if name in present else 0 for name in COMBINATION_PARTS}
                sources = {name: {"calls": n, "projects": fleet_projects if name == "fleet" else int(n > 0)} for name, n in calls.items()}
                total = sum(calls.values())
                cases.append({"present": list(present), "sources": sources, "calls": total, "figures": True})
                cases.append({"present": list(present), "sources": sources, "calls": total + 1, "figures": False})
    said = run(
        "index.html",
        "  out.said = " + json.dumps(cases) + ".map(c => String(sourcesSentence({ sources: c.sources }, c.calls)));\n",
        tmp_path,
        before=DEMO_DOM,
    )["said"]
    in_order = ["fleet", "live", "sandbox", "probe", "other"]
    named = {"fleet": "a synthetic Acme fleet run through the real gates",
             "live": "real Claude Code or Codex runs on Acme tasks in projects that enforce",
             "probe": "the service’s own synthetic probes"}
    lead = "Where they come from: "
    for case, markup in zip(cases, said):
        sentence = _read(markup)
        assert sentence.startswith(lead) and sentence.endswith(".") and sentence.count(".") == 1, sentence
        clauses = sentence[len(lead):-1].split(", ")
        if case["figures"]:
            sources = [name for name in in_order if name in case["present"]]
            assert len(clauses) == len(sources), f"{len(sources)} sources read as {len(clauses)}: {sentence!r}"
            for name, clause in zip(sources, clauses):
                count = f"{COMBINATION_PARTS[name]:,}"
                assert clause.startswith(count + " from ") and not re.search(r"\d", clause[len(count):]), \
                    f"{name} is not a clause of its own, with its count: {clause!r}"
        else:
            assert not re.search(r"\d", sentence), f"Parts that do not add up give no figure: {sentence!r}"
            for name, words in named.items():
                if name in case["present"]:
                    assert clauses.count(words) == 1, f"{name} is not one item of the list: {clauses}"


def test_a_tile_splits_its_count_one_clause_a_source_whichever_sources_are_there(tmp_path: Path) -> None:
    """The Stopped tile's parts are separated by commas too, so none of them carries one of its own."""
    refused = {"live": 2, "fleet": 15, "sandbox": 3, "probe": 14, "other": 4}
    cases = []
    for size in range(1, len(refused) + 1):
        for present in itertools.combinations(refused, size):
            for fleet_projects in ((6,) if "fleet" in present else (0, 6)):
                sources = {name: {"calls": COMBINATION_PARTS[name] if name in present else 0,
                                  "projects": fleet_projects if name == "fleet" else int(name in present)} for name in refused}
                rows = [{"project": "Acme-" + name.title(), "source": name, "calls": COMBINATION_PARTS[name], "refused": refused[name]}
                        for name in present]
                cases.append({"present": list(present), "data": {"sources": sources, "by_project": rows},
                              "total": sum(refused[name] for name in present)})
    words = run(
        "index.html",
        "  out.words = " + json.dumps(cases) + ".map(c => sourceSplit(c.data, 'refused', c.total, true).words);\n",
        tmp_path,
        before=DEMO_DOM,
    )["words"]
    for case, said in zip(cases, words):
        sources = [name for name in ("live", "fleet", "sandbox", "probe", "other") if name in case["present"]]
        clauses = said.split(", ")
        assert len(clauses) == len(sources), f"{len(sources)} sources read as {len(clauses)}: {said!r}"
        for name, clause in zip(sources, clauses):
            assert re.match(rf"{refused[name]} (?:in|from) ", clause) and not re.search(r"\d", clause[len(str(refused[name])):]), \
                f"{name} is not a clause of its own, with its count: {clause!r}"


def test_the_counts_are_read_without_a_key_even_when_one_is_typed(tmp_path: Path) -> None:
    """So they only ever show a stack whose reads are open, which is what their words say it is."""
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview() + " }", before="el('apiKeyInput').value = 'acme-operator-key';\n")
    (asked,) = out["overviewAsked"]
    assert "X-API-Key" not in asked["headers"], "A typed key would read a private stack's totals under a demo label"
    assert _metrics(out["live"])["calls"] == "1,284"


def test_one_of_each_reads_in_the_singular(tmp_path: Path) -> None:
    split = "sandbox_split: { sandbox: { calls: 1, projects: 1 }, elsewhere: { calls: 0, projects: 0 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(split, calls=1, refused=0, would_refuse=1, projects=1) + " }")
    assert "1 of the 1 call from visitors’ sandboxes" in _text(out["where"])
    assert "none in the last 7 days" in _text(out["live"]), "A count of nothing says so in words"


def test_a_sparse_window_says_what_it_holds_instead_of_drawing_a_flat_line(tmp_path: Path) -> None:
    one_day = "series: [{ day: '2026-09-20', approved: 0, observed: 0, refused: 0 }, { day: '2026-09-21', approved: 5, observed: 2, refused: 1 }]"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(one_day) + " }")
    assert "tf-spark" not in out["live"] and "all on 21 Sep" in out["live"]
    subs = [_text(sub) for sub in re.findall(r'<span class="tf-tile-sub">(.*?)</span>', out["live"], re.S)]
    assert subs == ["in the last 7 days, all on 21 Sep", "before they ran", "recorded while a project observes"], \
        "The day is named once, on the first tile, when every call fell on it"
    two_days = "series: [{ day: '2026-09-20', approved: 3, observed: 1, refused: 2 }, { day: '2026-09-21', approved: 5, observed: 2, refused: 1 }]"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(two_days) + " }")
    assert out["live"].count("tf-spark") == 3, "Two days with a count make a trend"


def test_no_count_is_shown_when_the_stack_does_not_answer_with_figures(tmp_path: Path) -> None:
    cases = {
        "not found": ("{ status: 404, body: { detail: 'no route' } }", "The stack answered HTTP 404 instead of its figures"),
        "a private stack": ("{ status: 401, body: { title: 'Unauthorized' } }", "This stack keeps its figures private"),
        "unreachable": ("'network'", "The stack did not answer"),
        "nothing counted": ("{ status: 200, body: " + _overview(calls=0, refused=0, would_refuse=0, projects=0) + " }", "Nothing judged here yet"),
        "not the contract's shape": ("{ status: 200, body: { totals: 'many' } }", "not with figures this page can read"),
        "no window": ("{ status: 200, body: { totals: { calls: 5, refused: 1, would_refuse: 0, projects: 1 } } }", "not with figures this page can read"),
    }
    for name, (reply, words) in cases.items():
        out = _load(tmp_path, overview=reply)
        assert words in _text(out["live"]), name
        assert "data-metric" not in out["live"] and out["whereHidden"], f"{name}: no figure and no source line"
        if name != "nothing counted":
            assert not re.search(r"\d", _text(out["live"]).replace("HTTP 404", "")), f"{name}: not one number invented"


def test_an_answer_that_is_not_json_is_called_an_answer_not_silence(tmp_path: Path) -> None:
    """A 200 whose body is not JSON: the stack answered, so neither card says it did not."""
    not_json = r"""
const stubFetch = globalThis.fetch;
globalThis.fetch = async (url, init) => {
  const res = await stubFetch(url, init);
  const path = String(url).split('?')[0];
  if (path.endsWith('/api/overview') || path.endsWith('/proof.json')) {
    return { ok: res.ok, status: res.status, json: async () => { throw new SyntaxError('Unexpected token < in JSON'); } };
  }
  return res;
};
"""
    out = _load(tmp_path, overview="{ status: 200, body: {} }", proof="{ status: 200, body: {} }", before=not_json)
    live, bench = _text(out["live"]), _text(out["bench"])
    assert "The stack answered, but not with figures this page can read" in live and "did not answer" not in live
    assert "The stack answered, but not with a snapshot this page can read" in bench
    assert "could not be read" not in bench and "holds no complete series" not in bench
    assert "data-metric" not in out["live"] + out["bench"] and out["whereHidden"]


def test_hostile_totals_never_reach_the_page(tmp_path: Path) -> None:
    evil = "'<img src=x onerror=alert(1)>'"
    for totals in (dict(calls=evil, refused=evil), dict(calls=-3), dict(calls=2.5)):
        out = _load(tmp_path, overview="{ status: 200, body: " + _overview(**totals) + " }")
        assert "data-metric" not in out["live"] and "<img" not in out["live"], "A figure that is not a whole count is not shown"


# ---------------------------------------------------------------- the benchmark


def _pooled(proof: dict) -> dict:
    """What the page must say, computed here from the snapshot itself."""
    sums = {key: {"k": 0, "n": 0, "done": 0, "done_n": 0} for key in ("none", "prompt", "threefold")}
    series = 0
    landed = 0
    for b in proof["benchmarks"]:
        if b.get("pilot"):
            continue
        conditions = {c["condition"]: c for c in b["conditions"]}
        series += 1
        for key, s in sums.items():
            s["k"] += conditions[key]["violation"]["k"]
            s["n"] += conditions[key]["violation"]["n"]
            s["done"] += conditions[key]["completion"]["k"]
            s["done_n"] += conditions[key]["completion"]["n"]
        landed += conditions["threefold"]["violation"]["k"] > 0
    return {"series": series, "landed": landed, "sums": sums, "runs": sum(s["n"] for s in sums.values())}


def test_the_benchmark_is_pooled_from_the_snapshot_this_stack_serves(tmp_path: Path) -> None:
    proof = json.loads(PROOF_FILE.read_text(encoding="utf-8"))
    want = _pooled(proof)
    out = _load(tmp_path, proof="{ status: 200, body: " + json.dumps(proof) + " }")
    (asked,) = out["proofAsked"]
    assert asked["url"] == "https://example.test/prod/proof.json" and "X-API-Key" not in asked["headers"]
    bench = _text(out["bench"])
    tf = want["sums"]["threefold"]
    assert _metrics(out["bench"])["bench-threefold"] == f"{tf['k']} of {tf['n']}"
    real = "real runs" if all(b.get("scripted_rows") == 0 for b in proof["benchmarks"] if not b.get("pilot")) else "runs"
    assert f"{want['series']} series, {want['runs']:,} {real} of Claude Code and Codex." in bench
    if want["landed"] == 0:
        assert f"No rule-breaking write landed under Threefold in any of the {want['series']} series." in bench
    assert "governed violation" not in bench.replace("The benchmark calls these governed violations.", ""), \
        "One plain term on the card; the benchmark's own term is named once, under the chart"
    for key in ("none", "prompt"):
        assert f"{want['sums'][key]['k']} of {want['sums'][key]['n']}" in bench
    none = want["sums"]["none"]
    assert (f"The price: the acceptance tests passed in {tf['done']} of those {tf['done_n']} runs under Threefold, "
            f"against {none['done']} of {none['done_n']} with no guidance") in bench, \
        "The price is stated with the result, and read against what the agents finished with no guidance"
    assert 'href="https://example.test/prod/dashboard.html#/proof"' in out["bench"]
    interval = re.search(r'<p class="tf-bench-note" data-bench="interval">(.*?)</p>', out["bench"], re.S)
    assert interval and _text(interval.group(1)) == _interval_line(proof), \
        "The pooled count is quoted with each series' own 95% interval, by family, and the tasks and repetitions behind it"
    families = {b["family"] for b in proof["benchmarks"] if not b.get("pilot")}
    if families == {"standard", "pressure"}:
        assert f"By condition, the {want['series']} series pooled, standard and pressure tasks together." in bench, \
            "The bars pool the two families, which the reports never do, so they say so"
    time = re.search(r'<p class="tf-bench-price" data-bench="time">(.*?)</p>', out["bench"], re.S)
    assert time and _text(time.group(1)) == _time_line(proof), "The time Threefold cost is read from the snapshot, beside its price"
    case = re.search(r'<p class="tf-bench-case" data-bench="case">(.*?)</p>', out["bench"], re.S)
    assert case and _text(case.group(1)) == CASE, "The case for a team's time is said where its evidence is shown"
    assert out["bench"].index('data-bench="price"') < out["bench"].index('data-bench="time"') < out["bench"].index('data-bench="case"')


def test_the_benchmark_bars_are_one_named_stop_per_row(tmp_path: Path) -> None:
    """A judge: the "x of 81" bars gave every nonzero segment its own unnamed keyboard stop.

    Each row is one stop now, named by the label that counts every segment,
    and a segment is a bar with a hover tooltip, never a stop of its own.
    """
    proof = json.loads(PROOF_FILE.read_text(encoding="utf-8"))
    out = _load(tmp_path, proof="{ status: 200, body: " + json.dumps(proof) + " }")
    bench = out["bench"]
    rows = re.findall(r'<div class="tf-hbar" role="img" aria-label="([^"]+)"( tabindex="0")?>', bench)
    assert len(rows) == 3, "One row per condition"
    assert all(stop for _, stop in rows), "Every row is a stop, named"
    assert all(re.search(r": \d", label) for label, _ in rows), "Each label counts its segments"
    spans = re.findall(r"<span[^>]*tf-hbar-seg[^>]*>", bench)
    assert spans, "The bars are drawn"
    assert all("tabindex" not in s and "aria-label" not in s for s in spans), "No segment is a stop"


# The case for a team's time that the evidence supports, and no more: where a
# rule-breaking write is caught, and what the agent gets back. Nothing
# measured time saved, and the sentence says so.
CASE = ("What a team gets for it: a rule-breaking write is refused as the agent makes it, not found later in review, "
        "and the agent is told why, with a checked fix where one can be made. No time saved in review has been measured.")


def _time_line(proof: dict) -> str:
    """What the card must say of the time, from each series' own overhead in the snapshot (a ratio of mean wall times)."""
    ratios = []
    for b in proof["benchmarks"]:
        if b.get("pilot"):
            continue
        overhead = {c["condition"]: c for c in b["conditions"]}["threefold"]["overhead"]
        assert overhead["against"] == "none"
        ratios.append(overhead["seconds"])
    low, high = min(ratios), max(ratios)
    said = f"{low:.2f} times" if low == high else f"{low:.2f} to {high:.2f} times"
    return (f"The time: on average, a run under Threefold took {said} as long as one with no guidance"
            + (", series by series." if len(ratios) > 1 else "."))


def test_the_time_is_said_only_from_a_ratio_every_series_carries(tmp_path: Path) -> None:
    """A judge: a #workplace-efficiency entry omitted the wall time its own benchmark measured.

    The line is a range over each counted series' overhead in seconds against
    no guidance; a series without one, or with one that is not a positive
    number, leaves the line out rather than let the range speak for it.
    """
    def series(seconds, against="none"):
        conditions = [
            {"condition": "none", "violation": {"k": 5, "n": 9}, "completion": {"k": 9, "n": 9}, "overhead": None},
            {"condition": "prompt", "violation": {"k": 2, "n": 9}, "completion": {"k": 9, "n": 9}},
            {"condition": "threefold", "violation": {"k": 0, "n": 9}, "completion": {"k": 7, "n": 9},
             "overhead": {"against": against, "turns": 1.2, "seconds": seconds, "cost": None}},
        ]
        return {"agent": "Codex", "pilot": False, "scripted_rows": 0, "conditions": conditions}

    def time_of(*benchmarks) -> str:
        out = _load(tmp_path, proof="{ status: 200, body: " + json.dumps({"benchmarks": list(benchmarks)}) + " }")
        found = re.search(r'data-bench="time">(.*?)</p>', out["bench"], re.S)
        assert "<img" not in out["bench"], "Service data reached the page as markup"
        return _text(found.group(1)) if found else ""

    assert time_of(series(1.56)) == "The time: on average, a run under Threefold took 1.56 times as long as one with no guidance."
    assert time_of(series(2.31), series(1.18), series(1.4)) == (
        "The time: on average, a run under Threefold took 1.18 to 2.31 times as long as one with no guidance, series by series.")
    for hostile in (None, 0, -1.2, "'1.5'", "'<img src=x>'"):
        value = hostile.strip("'") if isinstance(hostile, str) else hostile
        assert time_of(series(1.3), series(value)) == "", f"seconds {hostile!r}: no range over the series that carry one"
    assert time_of(series(1.3), series(1.5, against="prompt")) == "", "A ratio against another baseline is not this one"


def _pct(value: float) -> str:
    """A share as T.pct writes it: a whole percent, rounded half up, and <1% for a share under one percent."""
    share = value * 100
    return "<1%" if 0 < share < 1 else f"{int(share + 0.5)}%"


def _interval_line(proof: dict) -> str:
    """What the benchmark card must say under its headline, from each series' own interval in the snapshot."""
    words = {"standard": "standard-task", "pressure": "pressure-task"}
    intervals: dict = {}
    tasks: dict = {"standard": [], "pressure": []}
    reps = set()
    for b in proof["benchmarks"]:
        if b.get("pilot"):
            continue
        violation = {c["condition"]: c for c in b["conditions"]}["threefold"]["violation"]
        intervals.setdefault(b["family"], []).append((violation["n"], violation["ci_low"], violation["ci_high"]))
        tasks[b["family"]] += [t for t in b["tasks"] if t not in tasks[b["family"]]]
        reps.add(violation["n"] / len(b["tasks"]))
    (each,) = reps
    parts = []
    for family in ("standard", "pressure"):
        found = intervals.get(family)
        if not found:
            continue
        if len(set(found)) == 1:
            n, low, high = found[0]
            parts.append(f"{_pct(low)}–{_pct(high)} for {'the' if len(found) == 1 else 'each'} {words[family]} series of {n} runs")
        else:
            parts.append(f"within {_pct(min(x[1] for x in found))}–{_pct(max(x[2] for x in found))} for each of the {len(found)} {words[family]} series")
    named = [family for family in ("standard", "pressure") if intervals.get(family)]
    split = ", " + " and ".join(f"{len(tasks[f])} {f}" for f in named) if len(named) > 1 else ""
    total = sum(len(tasks[f]) for f in named)
    return (f"95% interval under Threefold, series by series: {'; '.join(parts)}. {total} Acme tasks{split}, "
            f"each run {int(each)} times per condition in every series.")


def test_a_violation_under_threefold_is_said_and_a_pilot_is_not_counted(tmp_path: Path) -> None:
    def series(agent, tf_k, pilot=False, complete=True, scripted=0):
        conditions = [
            {"condition": "none", "violation": {"k": 5, "n": 9}, "completion": {"k": 9, "n": 9}},
            {"condition": "prompt", "violation": {"k": 2, "n": 9}, "completion": {"k": 9, "n": 9}},
            {"condition": "threefold", "violation": {"k": tf_k, "n": 9}, "completion": {"k": 7, "n": 9}},
        ]
        return {"agent": agent, "pilot": pilot, "scripted_rows": scripted, "conditions": conditions if complete else conditions[:2]}

    proof = {"benchmarks": [series("Codex", 1), series("Claude Code", 0), series("Claude Code", 9, pilot=True), series("Codex", 9, complete=False)]}
    out = _load(tmp_path, proof="{ status: 200, body: " + json.dumps(proof) + " }")
    bench = _text(out["bench"])
    assert "2 series, 54 real runs of Codex and Claude Code." in bench
    assert "A rule-breaking write landed under Threefold in 1 of the 2 series." in bench
    assert _metrics(out["bench"])["bench-threefold"] == "1 of 18"
    scripted = {"benchmarks": [series("Codex", 0), series("Claude Code", 0, scripted=3)]}
    out = _load(tmp_path, proof="{ status: 200, body: " + json.dumps(scripted) + " }")
    assert "2 series, 54 runs of Codex and Claude Code." in _text(out["bench"]), "Runs are called real only when none was scripted"
    evil = {"benchmarks": [series("<img src=x onerror=alert(1)>", 0)]}
    out = _load(tmp_path, proof="{ status: 200, body: " + json.dumps(evil) + " }")
    assert "<img" not in out["bench"] and "&lt;img" in out["bench"]
    assert 'data-bench="interval"' not in out["bench"], "A series without its family, interval or tasks gives no interval line"
    hostile = series("Codex", 0)
    hostile.update(family="<img src=x onerror=alert(1)>", tasks=["a", "b", "c"])
    hostile["conditions"][2]["violation"].update(ci_low=0, ci_high=0.2992)
    out = _load(tmp_path, proof="{ status: 200, body: " + json.dumps({"benchmarks": [hostile]}) + " }")
    assert "<img" not in out["bench"] and 'data-bench="interval"' not in out["bench"], "A family this page does not know gives no line"
    known = dict(hostile, family="pressure")
    out = _load(tmp_path, proof="{ status: 200, body: " + json.dumps({"benchmarks": [known]}) + " }")
    assert _text(re.search(r'data-bench="interval">(.*?)</p>', out["bench"], re.S).group(1)) == (
        "95% interval under Threefold, series by series: 0%–30% for the pressure-task series of 9 runs. "
        "3 Acme tasks, each run 3 times per condition in every series.")


def test_no_benchmark_result_is_shown_without_a_snapshot(tmp_path: Path) -> None:
    cases = {
        "{ status: 404, body: { title: 'Not Measured Yet' } }": "No benchmark snapshot is deployed on this stack",
        "{ status: 401, body: { title: 'Unauthorized' } }": "keeps its benchmark snapshot private",
        "'network'": "could not be read",
        "{ status: 200, body: { benchmarks: 'many' } }": "holds no complete series",
    }
    for reply, words in cases.items():
        out = _load(tmp_path, proof=reply)
        assert words in _text(out["bench"]), reply
        assert "data-metric" not in out["bench"], reply
        assert 'data-bench="time"' not in out["bench"] and 'data-bench="case"' not in out["bench"], \
            f"{reply}: the case and its cost are said only beside the evidence for them"


def test_the_line_beside_the_proof_heading_names_only_the_reads_it_shows(tmp_path: Path) -> None:
    """A judge: with both reads failing, the heading still said "Read as this page loaded, from GET …".

    The line now names the reads whose figures the strip and the card show,
    after both have answered; a read that failed or gave nothing to show is
    named as such. Each link keeps the stage prefix, though it is drawn after
    the header's own links were given theirs.
    """
    assert re.search(r'<p id="proof-links" class="tf-proof-links">Reading, as this page loads, from\s*<a',
                     _section("proof")), "Before the reads answer, the line says they are being read"
    overview = "{ status: 200, body: " + _overview() + " }"
    snapshot = "{ status: 200, body: " + PROOF_FILE.read_text(encoding="utf-8") + " }"
    nothing_judged = "{ status: 200, body: " + _overview(calls=0, refused=0, would_refuse=0, projects=0) + " }"
    both = "Read as this page loaded, from GET /api/overview and GET /proof.json"
    neither = "Nothing to show from GET /api/overview or GET /proof.json as this page loaded"
    cases = {
        "both answered": (overview, snapshot, both),
        "nothing judged yet is still an answer": (nothing_judged, snapshot, both),
        "the benchmark failed": (overview, "'network'", "Read as this page loaded, from GET /api/overview; GET /proof.json gave nothing to show"),
        "the counts failed": ("{ status: 503, body: {} }", snapshot, "Read as this page loaded, from GET /proof.json; GET /api/overview gave nothing to show"),
        "both unreachable": ("'network'", "'network'", neither),
        "both private": ("{ status: 401, body: {} }", "{ status: 401, body: {} }", neither),
        "answers with nothing to show": ("{ status: 200, body: { totals: 'many' } }", "{ status: 200, body: { benchmarks: 'many' } }", neither),
    }
    for name, (counts, bench, said) in cases.items():
        out = _load(tmp_path, overview=counts, proof=bench)
        assert _read(out["reads"]).replace(" ;", ";") == said, name
        for label, anchor in (("GET /api/overview", "get_api_overview"), ("GET /proof.json", "get_proof_json")):
            assert f'href="https://example.test/prod/swagger.html#/default/{anchor}"' in out["reads"], f"{name}: {label} keeps the stage prefix"


# ---------------------------------------------------------------- the gates


def test_the_flagship_loop_still_trips_after_the_page_has_loaded(tmp_path: Path) -> None:
    out = _load(
        tmp_path,
        overview="{ status: 200, body: " + _overview() + " }",
        hero="{ status: 200, body: LIVE_REFUSAL }",
        before="",
        scenario=r"""
  answer = api({ '/status': { status: 200, body: { service: 'Threefold', status: 'HEALTHY' } },
    'POST /simulate-loop': { status: 200, body: { status: 'BLOCKED_LOOP_DETECTED', reason: 'Loop detected', session_id: 'sim-1', session_tripped: true, bedrock_explanation: 'A sentence.', explanation_source: 'deterministic' } } });
  await checkApiHealth();
  await simulateLoop(); await tick();
  out.tag = el('verdict-tag').innerText;
  out.breaker = el('cb-indicator').innerHTML;
  out.pressed = ['loop', 'secret', 'boundary', 'compliant', 'adapter'].map(k => el('scenario-' + k).getAttribute('aria-pressed'));
  out.title = el('result-title').textContent;
  out.badge = el('terminal-badge').innerText;
  resetDemo();
  out.pressedAfterReset = ['loop', 'secret', 'boundary', 'compliant', 'adapter'].map(k => el('scenario-' + k).getAttribute('aria-pressed'));
  out.breakerAfterReset = el('cb-indicator').innerHTML;
""",
    )
    assert _metrics(out["live"])["calls"] == "1,284"
    assert out["tag"] == "BLOCKED_LOOP_DETECTED"
    assert "Circuit breaker: tripped" in out["breaker"]
    assert out["pressed"] == ["true", "false", "false", "false", "false"] and out["title"] == "Runaway tool loop"
    assert out["badge"] == "Live answer"
    assert out["pressedAfterReset"] == ["false"] * 5 and "Circuit breaker: armed" in out["breakerAfterReset"]


def test_a_live_answer_shows_the_session_spend_it_carries(tmp_path: Path) -> None:
    """Every verdict carries current_session_cost_usd, so a live scenario never says "none yet" of a cost it was told.

    A review found "Session spend none yet" under a live loop refusal whose
    answer carried $0.027: only the certificate set the spend. A measured
    zero is a measurement too; a value that is not a number leaves the words.
    """
    healthy = "'/status': { status: 200, body: { service: 'Threefold', status: 'HEALTHY' } }"
    out = run(
        "index.html",
        r"""
  answer = api({ """ + healthy + r""",
    'POST /simulate-loop': { status: 200, body: { status: 'BLOCKED_LOOP_DETECTED', reason: 'Loop detected', session_id: 'sim-1', session_tripped: true, current_session_cost_usd: 0.027, explanation_source: 'deterministic' } },
    'POST /simulate-secret': { status: 200, body: { status: 'BLOCKED_SECRET_DETECTED', reason: 'Sensitive credential detected', session_id: 'sim-2', current_session_cost_usd: 0, explanation_source: 'deterministic' } },
    'POST /evaluate-tool-call': { status: 200, body: { status: 'BLOCKED_BOUNDARY_VIOLATION', reason: 'Refused.', current_session_cost_usd: 0.0012 } },
    'POST /adapter/universal-tool-call': { status: 200, body: { detected_tool_name: 'edit_file', detected_action_type: 'FILE_WRITE', evaluation: { status: 'APPROVED', current_session_cost_usd: 0.0096, proof_hash: 'ab' } } } });
  await checkApiHealth();
  const spend = () => [el('kpi-spend').innerText, el('kpi-spend').classList.contains('tf-kpi-none')];
  await simulateLoop(); await tick(); out.loop = spend();
  await simulateSecret(); await tick(); out.secret = spend();
  await simulateBoundary(); await tick(); out.boundary = spend();
  await simulateUniversalAdapter(); await tick(); out.adapter = spend();
  for (const cost of ["'0.027'", 'null', '-1', 'Infinity']) {
    answer = api({ """ + healthy + r""",
      'POST /simulate-loop': { status: 200, body: { status: 'BLOCKED_LOOP_DETECTED', reason: 'Loop detected', session_id: 'sim-3', session_tripped: true, current_session_cost_usd: eval(cost) } } });
    await simulateLoop(); await tick();
    (out.unread = out.unread || []).push(spend());
  }
""",
        tmp_path,
        before=DEMO_DOM,
    )
    assert out["loop"] == ["$0.0270", False], "The live loop shows the cost its answer carried"
    assert out["secret"] == ["$0.0000", False], "A zero the answer carried is shown as one, with what it counts (the next test)"
    assert out["boundary"] == ["$0.0012", False] and out["adapter"] == ["$0.0096", False]
    assert out["unread"] == [["none yet", True]] * 4, "A cost that is not a finite, non-negative number is not shown"


SPEND_NOTE = "Projected from the token counts this demo declares, or the service’s default where none is declared; approved calls only, nothing metered."
CERTIFIED_NOTE = "Projected from the token counts this demo declares, 1,200 input and 400 output per call; approved calls only, nothing metered."


def test_the_spend_says_it_is_projected_and_what_it_counts(tmp_path: Path) -> None:
    """A judge: "Session spend $0.0384" and "6400 tokens consumed" came from token counts the demo declares.

    No token is metered: the service projects a session's cost from the
    counts each call declares, and adds only the calls that passed every
    gate. So the figure is called projected, the note under it says what it
    counts, and where Amazon Bedrock was asked for the sentence beside it, a
    $0.0000 never reads as the model's cost: the note says that call is not
    in it.
    """
    healthy = "'/status': { status: 200, body: { service: 'Threefold', status: 'HEALTHY' } }"
    out = run(
        "index.html",
        r"""
  const note = () => [el('kpi-note').textContent, el('kpi-note').hidden];
  out.idle = note();
  const secret = source => ({ status: 200, body: { status: 'BLOCKED_SECRET_DETECTED', reason: 'Sensitive credential detected', session_id: 'sim-2', current_session_cost_usd: 0, explanation_source: source, bedrock_explanation: 'A sentence.' } });
  for (const source of ['bedrock', 'deterministic_fallback', 'deterministic']) {
    answer = api({ """ + healthy + r""", 'POST /simulate-secret': secret(source) });
    await checkApiHealth();
    await simulateSecret(); await tick();
    out[source] = [el('kpi-spend').innerText].concat(note());
  }
  let n = 0;
  answer = api({ """ + healthy + r""",
    'POST /evaluate-tool-call': () => { n += 1; return { status: 200, body: { status: 'APPROVED', reason: 'ok', current_session_cost_usd: 0.0096 * n, explanation_source: 'deterministic' } }; },
    'POST /issue-certificate': { status: 200, body: { certificate_id: 'CERT-TF-1', total_cost_usd: 0.0384, total_tokens: 6400, sha256_fingerprint: 'ab', signature: null } } });
  await checkApiHealth();
  await simulateCompliant(); await tick();
  out.certified = [el('kpi-spend').innerText, el('kpi-tokens').innerText].concat(note());
  out.declared = calls.filter(c => c.url.endsWith('/evaluate-tool-call')).slice(-4).map(c => [c.body.projected_input_tokens, c.body.projected_output_tokens]);
  resetDemo();
  out.reset = [el('kpi-spend').innerText, el('kpi-tokens').innerText].concat(note());
""",
        tmp_path,
        before=DEMO_DOM,
    )
    assert out["idle"][0] == "" and '<span id="kpi-note" class="tf-session-note" hidden></span>' in page_source("index.html"), \
        "No note before there is a figure"
    assert out["bedrock"] == ["$0.0000", SPEND_NOTE + " The Amazon Bedrock call behind the sentence above is not counted.", False]
    assert out["deterministic_fallback"] == ["$0.0000", SPEND_NOTE + " The model call asked for the sentence above is not counted.", False]
    assert out["deterministic"] == ["$0.0000", SPEND_NOTE, False], "No model was asked, so none is mentioned"
    assert out["declared"] == [[1200, 400]] * 4, "The note's counts are the ones the requests declare"
    assert out["certified"] == ["$0.0384", "6,400 projected tokens", CERTIFIED_NOTE, False]
    assert out["reset"] == ["none yet", "", "", True], "Reset takes the note away with the figure"
    body = page_source("index.html")
    stats = body.split('class="tf-session-stats"', 1)[1].split("</div>", 1)[0]
    assert "<span>Projected spend <b id=\"kpi-spend\"" in stats and "Session spend" not in body
    assert "tokens consumed" not in body and "(cost $" not in body, "Nothing calls a projection a cost consumed"


# A terminal whose lines are kept, each able to be taken away, with the
# counter a waiting line carries.
LOGGED = r"""
const logged = [];
document.createElement = tag => ({
  tagName: tag, className: '', textContent: '', innerHTML: '', removed: false, counter: { textContent: '' },
  setAttribute() {}, click() {}, remove() { this.removed = true; },
  querySelector(sel) { return sel === '[data-tf-waited]' ? this.counter : null; }
});
el('terminal-log').appendChild = node => logged.push(node);
const shown = () => logged.filter(n => !n.removed).map(n => text(n.innerHTML || n.textContent));
"""


def test_a_slow_loop_says_what_it_waits_for_and_counts_the_seconds(tmp_path: Path) -> None:
    """A judge: on a cold start the loop showed only "Sending…" for several seconds.

    POST /simulate-loop sends its three calls on the service, in one request,
    so there is no call to print as it goes; past a second, a line says what
    the terminal waits for and counts the seconds (hidden from a screen
    reader, since the terminal is a log), and it goes when the answer comes,
    whether a verdict, an error or no answer at all.
    """
    healthy = "'/status': { status: 200, body: { service: 'Threefold', status: 'HEALTHY' } }"
    loop = "{ status: 200, body: { status: 'BLOCKED_LOOP_DETECTED', reason: 'Loop detected', session_id: 'sim-1', session_tripped: true, current_session_cost_usd: 0.027, explanation_source: 'deterministic' } }"
    out = run(
        "index.html",
        r"""
  const waitFor = async reply => {
    const gate = held();
    answer = api({ """ + healthy + r""", 'POST /simulate-loop': () => gate.promise });
    await checkApiHealth();
    logged.length = 0;
    const running = simulateLoop();
    await new Promise(r => setTimeout(r, 400));
    const early = shown();
    await new Promise(r => setTimeout(r, 900));
    const waiting = shown();
    const line = logged.find(n => /Waiting/.test(n.innerHTML));
    const timers = intervals.size;
    await new Promise(r => setTimeout(r, 1000));
    await runIntervals();
    const counted = line ? line.counter.textContent : null;
    gate.release(reply);
    await running; await tick();
    return { early, waiting, markup: line ? line.innerHTML : '', timers, counted, after: shown(), timersAfter: intervals.size,
      tag: el('verdict-tag').innerText, badge: el('terminal-badge').innerText };
  };
  out.answered = await waitFor(""" + loop + r""");
  out.failed = await waitFor({ status: 503, body: {} });
  out.unreachable = await waitFor('network');
  answer = api({ """ + healthy + r""", 'POST /simulate-loop': """ + loop + r""" });
  logged.length = 0;
  await simulateLoop(); await new Promise(r => setTimeout(r, 1200)); await tick();
  out.fast = shown();
""",
        tmp_path,
        before=DEMO_DOM + LOGGED,
    )
    waiting_words = "… Waiting for the service, which sends the three calls itself and answers after the third:"
    for name in ("answered", "failed", "unreachable"):
        got = out[name]
        assert not any("Waiting" in line for line in got["early"]), f"{name}: no waiting line in the first second"
        assert [line for line in got["waiting"] if "Waiting" in line] == [waiting_words + " 1 s"], f"{name}: past a second, it says what it waits for"
        assert 'aria-hidden="true" data-tf-waited>1 s</span>' in got["markup"], f"{name}: the count is hidden from a screen reader"
        assert got["timers"] == 1 and got["counted"] == "2 s", f"{name}: the count goes up each second"
        assert not any("Waiting" in line for line in got["after"]) and got["timersAfter"] == 0, f"{name}: the line and its timer go with the wait"
        assert got["tag"] == "BLOCKED_LOOP_DETECTED"
    assert out["answered"]["badge"] == "Live answer" and out["failed"]["badge"] == "Recorded replay" == out["unreachable"]["badge"]
    assert not any("Waiting" in line for line in out["fast"]), "An answer inside a second leaves no line behind"


def _explained(tmp_path: Path, text: str, source: str = "bedrock") -> dict:
    """What "Why, in a sentence" shows for a live loop refusal explained in these words."""
    body = {"status": "BLOCKED_LOOP_DETECTED", "reason": "Loop detected", "session_id": "sim-1", "session_tripped": True,
            "bedrock_explanation": text, "explanation_source": source}
    return run(
        "index.html",
        r"""
  answer = api({ '/status': { status: 200, body: { service: 'Threefold', status: 'HEALTHY' } },
    'POST /simulate-loop': { status: 200, body: """ + json.dumps(body) + r""" } });
  await checkApiHealth();
  await simulateLoop(); await tick();
  out.box = el('bedrock-box').innerHTML;
""",
        tmp_path,
        before=DEMO_DOM,
    )["box"]


def test_an_explanation_in_markdown_is_shown_as_plain_sentences(tmp_path: Path) -> None:
    """A review saw "# Threefold Governance Decision: BLOCKED_LOOP_DETECTED **Decision Explanation:** …" under "Why, in a sentence".

    The words are taken out of the Markdown, never rendered as it: the heading
    and the bold label go, the first sentence or two are shown, and the whole
    text, in the same plain words, is one click under them.
    """
    explanation = (
        "# Threefold Governance Decision: BLOCKED_LOOP_DETECTED\n\n"
        "**Decision Explanation:** This block stops a runaway agent from paying for the same edit again. "
        "The tool `edit_file` was called with identical arguments three times, at $0.0270 so far.\n\n"
        "## Why it matters\n"
        "- Each repeat costs tokens and changes nothing in `src/service.py`.\n"
        "- The session is halted until an operator resumes it, see [the runbook](https://example.test/runbook).\n"
    )
    box = _explained(tmp_path, explanation)
    lead, more = box.split("<details", 1)
    assert _text(lead) == ("Amazon Bedrock (Claude Haiku 4.5): This block stops a runaway agent from paying for the same edit again. "
                           "The tool edit_file was called with identical arguments three times, at $0.0270 so far.")
    assert '<summary>The whole explanation</summary>' in more
    whole = _text(more)
    assert "Each repeat costs tokens and changes nothing in src/service.py." in whole and "see the runbook." in whole
    for mark in ("#", "**", "`", "](", "<h", "<ul", "<code", "<a "):
        assert mark not in box.replace("<details", "").replace("</details>", ""), f"{mark!r} reached the page"
    one_line = explanation.replace("\n\n", " ", 1)
    assert _text(_explained(tmp_path, one_line).split("<details", 1)[0]).startswith(
        "Amazon Bedrock (Claude Haiku 4.5): This block stops a runaway agent"), "A heading that runs into the text on one line is dropped too"


def test_a_plain_explanation_is_shown_whole_and_escaped(tmp_path: Path) -> None:
    reason = ("Clean Architecture violation: Layering rule 'python-domain-stays-pure' refuses this write: A Python file under "
              "domain/ may not import infrastructure or a driver. 'src/domain/user.py' imports 'boto3', which matches 'boto3'")
    box = _explained(tmp_path, reason, "deterministic")
    assert "<details" not in box and _text(box) == "Deterministic explanation, the model is not asked for this verdict: " + reason, \
        "A short answer is shown whole, as it came"
    hostile = "# <img src=x onerror=alert(1)>\n**Why:** `<script>alert(2)</script>` [click](javascript:alert(3)) was refused. " + "x" * 300
    box = _explained(tmp_path, hostile)
    assert "<img" not in box and "<script" not in box and "javascript:" not in box and "<a " not in box
    assert "&lt;script&gt;alert(2)&lt;/script&gt; click was refused." in box, "The words are escaped after the markup is taken out"


# What leadSentences shows of a long explanation: its first sentence, and the
# second when the first is short. Each case is the text and the lead expected.
LEADS = [
    # An abbreviation before a capital does not end the sentence.
    ("Domain code may not import a cloud SDK, e.g. AWS clients or queues, because the domain must stay pure and "
     "testable without a network. Move the import behind a port.",
     "Domain code may not import a cloud SDK, e.g. AWS clients or queues, because the domain must stay pure and "
     "testable without a network."),
    ("The call was judged in the U.S. East region at 12:00 and refused before it ran, as every enforced project's "
     "calls are. Nothing was written.",
     "The call was judged in the U.S. East region at 12:00 and refused before it ran, as every enforced project's "
     "calls are."),
    ("It was refused, i.e. The write never reached the file, and the agent was told why in the same answer, with the "
     "rule that decided. Try again with the fix.",
     "It was refused, i.e. The write never reached the file, and the agent was told why in the same answer, with the "
     "rule that decided."),
    ("The edge in N. Virginia forwarded it to the function, which judged it against the layering rules of the "
     "project and refused it. The file was not written.",
     "The edge in N. Virginia forwarded it to the function, which judged it against the layering rules of the "
     "project and refused it."),
    ("Refused (cf. Scenario 3). The same import in an adapter would pass.",
     "Refused (cf. Scenario 3). The same import in an adapter would pass."),
    # A stop followed by a space and a capital does end one, after a closing
    # quote or bracket too, and after a file name.
    ('The hook answered "deny." The agent read the reason and wrote the file elsewhere, under infrastructure/, where '
     'the rule allows it. Then it ran the tests.',
     'The hook answered "deny." The agent read the reason and wrote the file elsewhere, under infrastructure/, where '
     'the rule allows it.'),
    ("It imports boto3 in src/domain/user.py. The domain must not depend on a cloud SDK, so the write was refused "
     "before it reached the disk and the session carries on.",
     "It imports boto3 in src/domain/user.py. The domain must not depend on a cloud SDK, so the write was refused "
     "before it reached the disk and the session carries on."),
    ("The session was halted (after the third call.) Each repeat had cost tokens and changed nothing in the file, "
     "so the breaker stopped it there. An operator resumes it.",
     "The session was halted (after the third call.) Each repeat had cost tokens and changed nothing in the file, "
     "so the breaker stopped it there."),
    # A stop inside a number or a version ends nothing.
    ("The session had spent $0.0270 on three identical calls to edit_file, and v2.1 of the rules refuses the fourth "
     "before it runs. An operator resumes it.",
     "The session had spent $0.0270 on three identical calls to edit_file, and v2.1 of the rules refuses the fourth "
     "before it runs."),
    # With no end found, the text is shown whole.
    (("no capital follows any stop here. so the whole text is the lead. " * 3).strip(),
     ("no capital follows any stop here. so the whole text is the lead. " * 3).strip()),
]


def test_a_long_explanation_is_led_by_whole_sentences_never_a_fragment(tmp_path: Path) -> None:
    """A judge: a sentence was taken to end at any stop before a capital, so "e.g. The" cut the lead into a fragment.

    A full stop after an initial or an abbreviation ends no sentence, and when
    it is unclear the text is not cut: two sentences shown as one are whole,
    one cut short is not.
    """
    out = run("index.html", "  out.leads = " + json.dumps([text for text, _ in LEADS]) + ".map(leadSentences);\n",
              tmp_path, before=DEMO_DOM)
    for (text, expected), lead in zip(LEADS, out["leads"]):
        assert lead == expected, f"{text!r} led with {lead!r}"
    whole = LEADS[0][0] + " " + "Each repeat costs tokens and changes nothing in src/service.py. " * 3
    box = _explained(tmp_path, whole.strip())
    lead, more = box.split("<details", 1)
    assert _text(lead) == "Amazon Bedrock (Claude Haiku 4.5): " + LEADS[0][1], "Through the page, the lead is the same whole sentence"
    assert "Move the import behind a port." in _text(more)


def test_the_four_scenarios_carry_the_numbers_the_readme_gives_them() -> None:
    gates = _section("watch-the-gates")
    cards = re.findall(r'<button type="button" id="scenario-(\w+)".*?</button>', gates, re.S)
    kinds = {}
    for key in cards:
        card = gates.split(f'id="scenario-{key}"', 1)[1].split("</button>", 1)[0]
        number = re.search(r'<span class="tf-scenario-num" aria-hidden="true">(\d)</span><span class="tf-sr-only">Scenario (\d): </span>', card)
        kinds[key] = (number.group(1), number.group(2)) if number else None
    assert kinds == {"loop": ("1", "1"), "secret": ("2", "2"), "boundary": ("3", "3"), "compliant": ("4", "4"), "adapter": None}, \
        "Numbered one to four, seen and read aloud alike; the adapter is not one of the four"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for number in set(re.findall(r"Scenario (\d)", readme)):
        assert number in {"1", "2", "3", "4"}, f"The README names a Scenario {number} the page does not have"
    assert "tf-scenario-route" not in gates, "Every route is in the terminal's guide and the contract strip, none on one card alone"


def test_the_idle_terminal_says_what_each_scenario_sends_and_reset_says_it_again(tmp_path: Path) -> None:
    gates = _section("watch-the-gates")
    idle = _text(gates.split('id="terminal-log"', 1)[1].split("</div>", 1)[0].split(">", 1)[1])
    guide = [
        ("1 · Loop", "POST /simulate-loop"), ("2 · Credential", "POST /simulate-secret"), ("3 · Architecture", "POST /evaluate-tool-call"),
        ("4 · Certificate", "POST /evaluate-tool-call ×4, POST /issue-certificate"), ("Adapter", "POST /adapter/universal-tool-call"),
    ]
    for key, route in guide:
        assert f"{key} {route}" in idle, f"The idle terminal does not say what {key} sends"
    assert "with no answer, the first four replay a recorded run" in idle
    out = run("index.html", "  resetDemo();\n  out.log = el('terminal-log').innerHTML;\n", tmp_path, before=DEMO_DOM)
    after = _text(out["log"])
    assert after.startswith("// Reset. Each scenario sends real calls to this stack")
    assert all(f"{key} {route}" in after for key, route in guide), "Reset shows the same guide"


def test_a_scenario_pressed_on_a_phone_brings_its_result_into_view(tmp_path: Path) -> None:
    """On a phone the result panel sits under five cards; where it is already on screen nothing moves."""
    scenario = r"""
  const scrolled = [];
  const panel = el('result-panel');
  panel.scrollIntoView = o => scrolled.push(o);
  globalThis.innerHeight = 812;
  answer = api({ '/status': { status: 200, body: { service: 'Threefold', status: 'HEALTHY' } },
    'POST /simulate-secret': { status: 200, body: { status: 'BLOCKED_SECRET_DETECTED', reason: 'Sensitive credential detected', session_id: 'sim-2', explanation_source: 'deterministic' } } });
  await checkApiHealth();
  panel.getBoundingClientRect = () => ({ top: 1460, bottom: 2100 });
  await simulateSecret(); await tick();
  out.below = scrolled.splice(0);
  panel.getBoundingClientRect = () => ({ top: 140, bottom: 780 });
  await simulateSecret(); await tick();
  out.onScreen = scrolled.splice(0);
  panel.getBoundingClientRect = () => ({ top: -600, bottom: 40 });
  await simulateSecret(); await tick();
  out.above = scrolled.splice(0);
"""
    out = run("index.html", scenario, tmp_path, before=DEMO_DOM)
    assert out["below"] == [{"behavior": "smooth", "block": "start"}], "Off the bottom of the screen, it is brought up"
    assert out["onScreen"] == [], "Already in view, nothing moves"
    assert out["above"] == [{"behavior": "smooth", "block": "start"}], "Scrolled past, it is brought back"
    still = run("index.html", scenario, tmp_path,
                before=DEMO_DOM + "globalThis.matchMedia = q => ({ matches: q.indexOf('reduce') !== -1 });\n")
    assert still["below"] == [{"behavior": "auto", "block": "start"}], "A reader who asked for less motion is taken there without a glide"


def test_the_page_draws_only_in_the_design_systems_colours() -> None:
    """Every colour on the first screen is a token of assets/threefold.css, or a mix of one."""
    style = _style()
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", style), "A colour written outside the tokens"
    script = page_source("index.html").split("<script>", 2)[2]
    assert not re.search(r"color:\s*'#", script), "A chart colour written outside T.COLORS"


def test_the_status_round_trip_is_the_browsers_record_of_it_when_it_keeps_one(tmp_path: Path) -> None:
    """A tab opened in the background runs its timers late; the clock around the call then says minutes."""
    record = (
        "performance.getEntriesByName = url => url.endsWith('/status') ? [{ startTime: 1000, responseEnd: 1176 }] : [];\n"
    )
    out = _load(tmp_path, before=record, scenario="  out.badge = el('statusRouteBadge').innerHTML;\n")
    assert _text(out["badge"]) == "live · /status 200 HEALTHY · 176 ms"


def test_the_caption_keeps_room_for_every_caption_it_can_end_with(tmp_path: Path) -> None:
    """The caption's cell is as tall as its longest caption from the start, so it never grows when the verdict lands."""
    out = run(
        "index.html",
        r"""
  out.room = el('hero-caption-room').innerHTML;
  out.captions = [heroCaption({ source: 'live' }), heroCaption({ source: 'live', kept: true, at: Date.now() - 59 * 60000 })]
    .concat(['timeout', 'private', 'limited', 'http', 'unreadable', 'network'].map(k => heroCaption({ source: 'recorded', why: { kind: k, http: 503 } })));
  out.kinds = HERO_RECORDED_KINDS;
""",
        tmp_path,
        before=DEMO_DOM,
    )
    assert out["kinds"] == ["timeout", "private", "limited", "http", "unreadable", "network"]
    assert _read(out["room"]) == max(out["captions"], key=len), "The room is the longest caption there is"
    script = page_source("index.html")
    handled = set(re.findall(r"if \(kind === '(\w+)'\) return", script)) | {"network"}
    assert handled == set(out["kinds"]), "A kind of recorded caption the room does not know of"
