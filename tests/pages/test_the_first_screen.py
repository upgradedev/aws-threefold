"""The first screen says what Threefold is, shows it working, and proves it, in that order.

A judge who opens the public URL meets the hero before anything else: one
promise line under twelve words, one sentence of how, "Watch it stop a bad
write — 60 s", "Open the live dashboard" and a quiet "Connect your
repository", and beside them the product's core moment. That moment is one
proposed write, the boundary write RECORDED.boundary was recorded from, asked
of this stack exactly as a hook asks it: the answer is the stack's own when it
gives one, labelled live, with its reason and the fix it checked, and the
recorded run otherwise, labelled recorded, with no fix, because a recording
kept none.

Under the hero, a proof strip whose every figure is read as the page loads:
calls judged, stopped and would have been stopped from GET /api/overview
(without a key, with one clause saying what the counts are made of), and the
benchmark pooled from GET /proof.json. Then how it works (three steps, one
flow diagram, the two-stage rollout, the three agents in the evidence's own
words), the AWS services as one diagram, the gates the flagship demo trips,
and a footer holding the links and the hackathon's tags.

The markup is read as the stack serves it; the script runs under Node with the
stub browser in _browser.py, and those tests skip where Node is absent.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from _browser import ROOT, page_source, run

PROMISE = "Stop bad agent writes before they reach your code."
SENTENCE = (
    "Deterministic gates on AWS judge each write and command from Claude Code, Codex or Antigravity, and your "
    "repos observe before they enforce."
)
SCOPE = (
    "That a refusal stops the write has been measured for Claude Code and Antigravity, and for Codex once, over "
    "its patch tool; the other routes Codex could write through are not measured."
)
ACTIONS = [
    ("hero-try", "dashboard.html#/try", "Watch it stop a bad write — 60 s"),
    ("hero-dashboard", "dashboard.html#/overview", "Open the live dashboard"),
    ("hero-connect", "dashboard.html#/connect", "Connect your repository"),
]
SECTIONS = ["what-threefold-is", "proof", "how-it-works", "built-on-aws", "watch-the-gates"]
REPOSITORY = "https://github.com/upgradedev/aws-threefold"
RECORDED_REASON = (
    "Clean Architecture violation: Layering rule 'python-domain-stays-pure' refuses this write: A Python file under "
    "domain/ may not import infrastructure or a driver. 'src/domain/user.py' imports 'boto3', which matches 'boto3'"
)
# The same reason as the hero says it: one plain sentence, then the rule's id
# and the status on the line under it.
PLAIN_REASON = "A Python file under domain/ may not import infrastructure or a driver, and src/domain/user.py imports boto3."
RULE_LINE = "rule python-domain-stays-pure · BLOCKED_BOUNDARY_VIOLATION"
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
    return run(
        "index.html",
        r"""
  await tick();
  out.live = el('proof-live').innerHTML;
  out.where = el('proof-where').innerHTML;
  out.whereHidden = el('proof-where').hidden;
  out.bench = el('proof-bench').innerHTML;
  out.overviewAsked = calls.filter(c => c.url.indexOf('/api/overview') !== -1).map(c => ({ url: c.url, method: c.method, headers: c.headers }));
  out.proofAsked = calls.filter(c => c.url.indexOf('/proof.json') !== -1).map(c => ({ url: c.url, method: c.method, headers: c.headers }));
  out.heroAsked = calls.filter(c => c.url.indexOf('/evaluate-tool-call') !== -1).map(c => ({ url: c.url, method: c.method, headers: c.headers, body: c.body }));
  out.source = el('hero-source').innerHTML;
  out.caption = el('hero-caption').textContent;
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


def test_the_three_actions_come_next_each_one_click_in_this_order() -> None:
    hero = _hero()
    actions = hero.split('id="hero-actions"', 1)[1].split("</div>", 1)[0]
    found = re.findall(r'<a id="([^"]+)" href="([^"]+)" class="([^"]*)"[^>]*>(.*?)</a>', actions, re.S)
    assert [(ident, href, _text(label)) for ident, href, _, label in found] == ACTIONS
    classes = [set(cls.split()) for _, _, cls, _ in found]
    assert {"tf-btn", "tf-btn-primary"} <= classes[0], "Watching it work is the primary action"
    assert "tf-btn" in classes[1] and "tf-btn-primary" not in classes[1], "The dashboard is the secondary one"
    assert "tf-btn" not in classes[2] and "tf-hero-link" in classes[2], "Connecting is a quiet text link"


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
    """Only the 60 in the walkthrough's label: the demonstration is drawn from an answer."""
    hero = _hero()
    assert re.findall(r"\d+", _text(hero)) == ["60"]
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
    assert during["waiting"] and during["words"] == "Playing this stack’s answer again; nothing is sent",         "While it plays again it says so, and never that it is asking"
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
    assert out["links"] == [f"https://example.test/prod/{href}" for _, href, _ in ACTIONS]


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
    assert out["caption"] == (
        "Judged just now by this stack through POST /evaluate-tool-call, and kept in its ledger as a page call, "
        "which is always enforced. For an hour, this browser shows this answer again rather than adding another call."
    ), "A page call is enforced where a hook's call on an observing project would only be recorded, so it is not called a hook's"
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
    assert summary == "Move boto3 out of the domain behind UserPort; adapter: src/infrastructure/user_adapter.py.",         "The service's summary, without a second 'Checked fix:' under a title that already says it"
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
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview() + " }", before=_kept(12))
    assert out["heroAsked"] == [], "A reload adds no refused page call to the ledger"
    assert _text(out["source"]) == "Live · 12 min ago", "The chip says when the answer was given, not a round trip that did not happen now"
    assert out["caption"] == (
        "Judged by this stack 12 minutes ago through POST /evaluate-tool-call, and shown again rather than asked again, "
        "so a reload adds no call to its ledger. This browser asks afresh once the answer is an hour old."
    )
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


def test_the_hero_asks_only_once_the_counts_are_read_so_a_visit_never_counts_itself(tmp_path: Path) -> None:
    """The hero's call is a refused page call in the ledger; sent alongside the read, the strip could count it."""
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
  out.caption = el('hero-caption').textContent;
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
    assert out["caption"] == (
        "This stack did not answer within 4 seconds, so this is a real run recorded against the live API on 2026-09-25, replayed here."
    )
    assert out["dataSource"] == "recorded" and "Refused before it was written" in out["verdict"]
    assert out["aborted"], "The call that lost the race is cancelled"
    assert out["kept"] is None


def test_the_hero_replays_the_recorded_run_and_says_truly_why(tmp_path: Path) -> None:
    """The caption says what this stack did: never 'not contacted' when it answered."""
    run_ = "so this is a real run recorded against the live API on 2026-09-25, replayed here."
    cases = {
        "unreachable": ("'network'", "This stack could not be reached, " + run_),
        "an error": ("{ status: 500, body: { title: 'Internal' } }", "This stack answered HTTP 500 instead of a verdict, " + run_),
        "a private stack": ("{ status: 401, body: { title: 'Unauthorized' } }",
                            "This stack judges only its operator’s calls (it answered HTTP 401), " + run_),
        "a forbidden call": ("{ status: 403, body: { title: 'Forbidden' } }",
                             "This stack judges only its operator’s calls (it answered HTTP 403), " + run_),
        "a rate limit": ("{ status: 429, body: { title: 'Too Many Requests' } }",
                         "This stack is limiting calls from this browser for now (HTTP 429), " + run_),
        "not a verdict": ("{ status: 200, body: { status: 7 } }",
                          "This stack answered, but not with a verdict this page can read, " + run_),
    }
    for name, (reply, caption) in cases.items():
        out = _load(tmp_path, hero=reply)
        assert "Recorded 2026-09-25, replayed offline" in out["source"], name
        assert out["dataSource"] == "recorded", f"{name}: a phone gives the recorded label the bar's room"
        assert out["caption"] == caption, name
        assert "without contacting it" not in out["caption"], f"{name}: the stack was asked"
        assert _read(out["reason"]) == PLAIN_REASON + " " + RULE_LINE, name
        assert "Refused before it was written" in out["verdict"] and out["flagged"] == [True, False, False], name
        assert "No fix in a replay" in out["fix"] and "Checked fix" not in out["fix"] and "gate checks" not in out["fix"],             f"{name}: a replay invents no fix"


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
    assert subs == ["in the last 7 days", "refused before they ran", "recorded while a project observes"],         "Each sub-line says what its number means, and the window is named once"
    assert not out["whereHidden"]
    assert _text(out["where"]) == (
        "Where they come from: of 1,284 calls on this stack, 1,102 from a synthetic Acme fleet that sends "
        "its calls through the real gates; 120 from sandboxes visitors started; 62 from the service’s own probes, "
        "the demos on this page and anyone else calling its open API. They show the stack at work, not how widely "
        "Threefold is used."
    )


def test_without_sources_the_sandboxes_are_still_told_apart(tmp_path: Path) -> None:
    split = "sandbox_split: { sandbox: { calls: 24, projects: 2 }, elsewhere: { calls: 1260, projects: 10 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(split) + " }")
    assert "24 of the 1,284 calls came from sandboxes visitors started" in _text(out["where"])
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview() + " }")
    assert "this stack's own traffic: the service's own probes, the sandboxes visitors start" in _text(out["where"])
    hostile = "sources: { fleet: { calls: '<img src=x onerror=alert(1)>' }, sandbox: { calls: 1 }, other: { calls: 1 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(hostile) + " }")
    assert "<img" not in out["where"] and "synthetic Acme fleet" not in out["where"], "A source that is not a count is not used"
    mismatched = "sources: { fleet: { calls: 900 }, sandbox: { calls: 120 }, other: { calls: 62 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(mismatched) + " }")
    where = _text(out["where"])
    assert "900" not in where and "1,284" not in where and "120" not in where, "Parts that do not add up to the 1,284 calls give no figure"
    assert "this stack's own traffic, including a synthetic Acme fleet that sends its calls through the real gates" in where,         "A fleet the stack reports is named in words even then: synthetic calls are never left unlabelled"
    nothing = "sources: { fleet: { calls: 0 }, sandbox: { calls: 0 }, other: { calls: 0 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(nothing) + " }")
    assert "synthetic Acme fleet" not in _text(out["where"]) and "this stack's own traffic" in _text(out["where"]),         "Parts that add up to nothing are not used, and no fleet is claimed"


def test_the_counts_are_read_without_a_key_even_when_one_is_typed(tmp_path: Path) -> None:
    """So they only ever show a stack whose reads are open, which is what their words say it is."""
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview() + " }", before="el('apiKeyInput').value = 'acme-operator-key';\n")
    (asked,) = out["overviewAsked"]
    assert "X-API-Key" not in asked["headers"], "A typed key would read a private stack's totals under a demo label"
    assert _metrics(out["live"])["calls"] == "1,284"


def test_one_of_each_reads_in_the_singular(tmp_path: Path) -> None:
    split = "sandbox_split: { sandbox: { calls: 1, projects: 1 }, elsewhere: { calls: 0, projects: 0 } }"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(split, calls=1, refused=0, would_refuse=1, projects=1) + " }")
    assert "1 of the 1 call came from sandboxes" in _text(out["where"])
    assert "none in the last 7 days" in _text(out["live"]), "A count of nothing says so in words"


def test_a_sparse_window_says_what_it_holds_instead_of_drawing_a_flat_line(tmp_path: Path) -> None:
    one_day = "series: [{ day: '2026-09-20', approved: 0, observed: 0, refused: 0 }, { day: '2026-09-21', approved: 5, observed: 2, refused: 1 }]"
    out = _load(tmp_path, overview="{ status: 200, body: " + _overview(one_day) + " }")
    assert "tf-spark" not in out["live"] and "all on 21 Sep" in out["live"]
    subs = [_text(sub) for sub in re.findall(r'<span class="tf-tile-sub">(.*?)</span>', out["live"], re.S)]
    assert subs == ["in the last 7 days, all on 21 Sep", "refused before they ran", "recorded while a project observes"],         "The day is named once, on the first tile, when every call fell on it"
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
    assert "governed violation" not in bench.replace("The benchmark calls these governed violations.", ""),         "One plain term on the card; the benchmark's own term is named once, under the chart"
    for key in ("none", "prompt"):
        assert f"{want['sums'][key]['k']} of {want['sums'][key]['n']}" in bench
    none = want["sums"]["none"]
    assert (f"The price: the acceptance tests passed in {tf['done']} of those {tf['done_n']} runs under Threefold, "
            f"against {none['done']} of {none['done_n']} with no guidance") in bench,         "The price is stated with the result, and read against what the agents finished with no guidance"
    assert 'href="https://example.test/prod/dashboard.html#/proof"' in out["bench"]


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
