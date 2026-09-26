"""The application: one dashboard with the contract's routes, and the layer every page shares.

What a screen shows is decided in script, so these tests run the dashboard's own
script and the shared layer it loads under Node, with a stub browser and a
recording fetch (see _browser.py). The service's answers are shaped exactly as
the 2026-09-22 contract in STATE.md fixes them; where a test needs a field, it is
one the contract lists. Everything else is read from the files as served.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from _browser import ROOT, WEB, page_source, run

PAGES = ["dashboard.html", "index.html", "rules.html", "sessions.html", "settings.html", "connect.html", "console.html"]
SHARED_LAYER_PAGES = ["dashboard.html", "index.html", "rules.html", "sessions.html", "settings.html", "connect.html"]

# Contract-shaped answers, and a hostile variant in which every string the
# service could return carries markup that would run if it were not escaped.
FIXTURES = r"""
const NOW = new Date().toISOString();
const DAY = n => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);
const EVIL = 'x"><svg onload=alert(2)><img src=x onerror=alert(1)>';
function row(i, extra) {
  return Object.assign({
    verdict_id: 'VERDICT-' + i, timestamp: new Date(Date.now() - i * 60000).toISOString(), session_id: 's-' + i,
    developer_id: 'ab12cd34', project_name: 'Acme-Billing', tool_name: 'Write', action_type: 'FILE_WRITE',
    agent: 'claude-code', origin: 'hook', dry_run: false, status: 'APPROVED', rule: 'NONE',
    target: 'src/billing/domain/Invoice.java', reason: 'Dry run, not enforced.', observed_rule: 'java-domain-stays-pure',
    observed_rules: ['java-domain-stays-pure'], observed_reason: 'imports javax.persistence into the domain',
    observed_target: 'src/billing/domain/Invoice.java', cost_usd: 0.01, rule_key: 'java-domain-stays-pure',
    stage: 'observe', hook_mode: 'managed', review: null, reviewed_at: null, review_note: '',
    category: 'LAYERING', category_label: 'Layer crossed'
  }, extra || {});
}
function overviewBody(totals) {
  return {
    window_days: 7, generated_at: NOW, source: 'rollups',
    totals: Object.assign({ calls: 1284, approved: 1190, refused: 37, would_refuse: 57, needs_review: 21, false_alarms: 4, projects: 2, agents: 2 }, totals || {}),
    series: [6, 5, 4, 3, 2, 1, 0].map(n => ({ day: DAY(n), approved: 150 + n, observed: 6, refused: n === 0 ? 0 : 4 })),
    by_agent: [{ agent: 'claude-code', calls: 900 }, { agent: 'codex', calls: 384 }],
    by_origin: [{ origin: 'hook', calls: 1200 }, { origin: 'page', calls: 84 }],
    by_rule: [{ rule_key: 'java-domain-stays-pure', refused: 20, would_refuse: 30 }, { rule_key: 'LOOP', refused: 17, would_refuse: 0 }],
    by_project: [
      { project: 'Acme-Billing', stage: 'enforce', configured: true, calls: 700, refused: 30, would_refuse: 10, needs_review: 3, last_seen: NOW },
      { project: 'Acme-Catalog', stage: 'observe', configured: false, calls: 584, refused: 7, would_refuse: 47, needs_review: 18, last_seen: NOW }
    ],
    stages: { observe: 1, enforce: 1 }
  };
}
const RULES = [
  { rule_key: 'java-domain-stays-pure', kind: 'layering', mode_now: 'observe', would_refuse: 5, correct: 5, false_alarms: 0, unreviewed: 0, last_seen: NOW, state: 'ready', recommendation: 'Promote: every flag it raised was right.' },
  { rule_key: 'LOOP', kind: 'gate', mode_now: 'observe', would_refuse: 0, correct: 0, false_alarms: 0, unreviewed: 0, last_seen: null, state: 'quiet', recommendation: 'It flagged nothing; promoting it costs nothing.' },
  { rule_key: 'PROTECTED_PATH', kind: 'gate', mode_now: 'observe', would_refuse: 4, correct: 2, false_alarms: 1, unreviewed: 1, last_seen: NOW, state: 'noisy', recommendation: 'Keep observing: one flag was a false alarm.' }
];
function detailBody(stage, rules) {
  return {
    project: 'Acme-Billing',
    config: { stage, observe_rules: [], created_at: NOW, updated_at: NOW, promoted_at: null, demoted_at: null, sandbox: false,
      history: [{ at: NOW, action: 'create', by: 'ab12cd34', enforce: [], observe: ['LOOP'] }] },
    readiness: {
      summary: { stage, days_observed: 5, calls_observed: 70, would_have_refused: 9, reviewed: 7, false_alarms: 1, false_alarm_rate: 0.14, rules_ready: 1, rules_quiet: 1, rules_noisy: 1, rules_needing_review: 0 },
      rules: rules || RULES
    }
  };
}
const PROJECTS = { projects: [
  { project: 'Acme-Billing', stage: 'enforce', configured: true, observe_rules: ['PROTECTED_PATH'], created_at: NOW, promoted_at: NOW, last_seen: NOW, calls: 700, refused: 30, would_refuse: 10, needs_review: 3, agents: ['claude-code', 'codex'], hook_modes: ['managed', 'observe'] },
  { project: 'Acme-Catalog', stage: 'observe', configured: false, observe_rules: [], created_at: null, promoted_at: null, last_seen: NOW, calls: 584, refused: 7, would_refuse: 47, needs_review: 18, agents: ['antigravity'], hook_modes: ['unknown'] }
] };
const DECISION = {
  decision: row(1),
  session: { session_id: 's-1', calls: 4, cost_usd: 0.02, is_tripped: false },
  rule: { id: 'java-domain-stays-pure', description: 'Billing domain classes stay free of persistence', mode: 'observe',
    when_path_matches: ['**/billing/domain/**/*.java'], forbid_imports: ['javax.persistence'], allow_imports: ['java.util'] }
};
function contract(over) {
  return api(Object.assign({
    '/api/overview': () => ({ status: 200, body: overviewBody() }),
    '/api/projects': { status: 200, body: PROJECTS },
    '/api/projects/Acme-Billing': { status: 200, body: detailBody('observe') },
    '/api/decisions': { status: 200, body: { items: [row(1), row(2, { status: 'BLOCKED_BOUNDARY_VIOLATION', observed_rules: [], observed_rule: '', reason: 'The domain imports infrastructure.' }), row(3, { agent: 'codex', hook_mode: 'observe' })], next_cursor: 'abc' } },
    '/api/decision': { status: 200, body: DECISION },
    '/dist/manifest.json': { status: 200, body: { files: [], bundle_sha256: 'cd'.repeat(32), installer_sha256: 'ab'.repeat(32) } }
  }, over || {}));
}
function hostile() {
  const evilRow = i => row(i, {
    project_name: EVIL, tool_name: EVIL, target: EVIL, reason: EVIL, observed_reason: EVIL, observed_target: EVIL,
    observed_rules: [EVIL], rule_key: EVIL, agent: EVIL, hook_mode: EVIL, session_id: EVIL, developer_id: EVIL,
    review_note: EVIL, category_label: EVIL, stage: EVIL, rule: EVIL
  });
  const ov = overviewBody();
  ov.by_agent = [{ agent: EVIL, calls: 5 }];
  ov.by_origin = [{ origin: EVIL, calls: 5 }];
  ov.by_rule = [{ rule_key: EVIL, refused: 3, would_refuse: 2 }];
  ov.by_project = [{ project: EVIL, stage: EVIL, configured: true, calls: 9, refused: 1, would_refuse: 1, needs_review: 1, last_seen: EVIL }];
  const rules = [{ rule_key: EVIL, kind: EVIL, mode_now: 'observe', would_refuse: 1, correct: 1, false_alarms: 0, unreviewed: 0, last_seen: NOW, state: EVIL, recommendation: EVIL }];
  const detail = detailBody('enforce', rules);
  detail.config.history = [{ at: NOW, action: EVIL, by: EVIL, enforce: [EVIL], observe: [EVIL] }];
  return api({
    '/api/overview': { status: 200, body: ov },
    '/api/projects': { status: 200, body: { projects: [Object.assign({}, PROJECTS.projects[0], { project: EVIL, stage: EVIL, agents: [EVIL], hook_modes: [EVIL], last_seen: EVIL })] } },
    ['/api/projects/' + encodeURIComponent(EVIL)]: { status: 200, body: detail },
    '/api/decisions': { status: 200, body: { items: [evilRow(1), evilRow(2)], next_cursor: null } },
    '/api/decision': { status: 200, body: { decision: evilRow(1), session: { session_id: EVIL, calls: 1, cost_usd: 0, is_tripped: false },
      rule: { id: EVIL, description: EVIL, mode: EVIL, when_path_matches: [EVIL], forbid_imports: [EVIL], allow_imports: [EVIL] } } },
    '/dist/manifest.json': { status: 200, body: { installer_sha256: EVIL } }
  });
}
function hrefs(markup, prefix) {
  const found = [];
  const re = new RegExp('href="(' + prefix.replace(/[/?#]/g, m => '\\' + m) + '[^"]*)"', 'g');
  let m;
  while ((m = re.exec(markup))) found.push(m[1].replace(/&amp;/g, '&'));
  return found;
}
function params(href) {
  const q = href.indexOf('?');
  const out = {};
  if (q >= 0) new URLSearchParams(href.slice(q + 1)).forEach((v, k) => { out[k] = v; });
  return out;
}
"""


def dash(scenario: str, tmp_path: Path, before: str = "") -> dict:
    """Runs the dashboard with the fixtures defined first, where `before` can use them."""
    return run("dashboard.html", scenario, tmp_path, before=FIXTURES + before)


def _deployed_project_pattern() -> str:
    template = (ROOT / "deploy" / "template.yml").read_text(encoding="utf-8")
    match = re.search(r"AllowedProjectPattern:\s*\n(?:\s+\w+:.*\n)*?\s+Default:\s*'([^']+)'", template)
    assert match, "Could not read the AllowedProjectPattern default out of deploy/template.yml"
    return match.group(1)


def _contract_routes() -> set[str]:
    """The routes STATE.md fixes for dashboard.html, as the page's route table writes them."""
    state = (ROOT / "STATE.md").read_text(encoding="utf-8")
    paragraph = state.split("**The application (D).**", 1)[1].split("\n", 1)[0]
    routes = {route.replace("<name>", ":name") for route in re.findall(r"`#(/[a-z]+(?:/<name>)?)", paragraph)}
    assert len(routes) >= 9, f"Read only {sorted(routes)} from the contract, so this test proves little"
    return routes


# Routes added after the contract paragraph was written, each named here so
# the check stays exact for every other route. The owner adds `#/proof` to
# "The application (D)" paragraph in STATE.md at merge; this set can then go,
# and the union below is the same either way.
ROUTES_PENDING_IN_THE_CONTRACT = {"/proof"}


# ------------------------------------------------------------------- routes


def test_the_dashboard_declares_every_route_the_contract_lists() -> None:
    body = page_source("dashboard.html")
    declared = set(re.findall(r"\{ path: '([^']+)'", body))
    assert declared == _contract_routes() | ROUTES_PENDING_IN_THE_CONTRACT


def test_every_route_draws_a_screen_with_a_heading(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract();
  const routes = ['#/overview', '#/projects', '#/projects/Acme-Billing', '#/review', '#/calls', '#/call?timestamp=t&verdict_id=VERDICT-1', '#/connect', '#/signin', '#/try'];
  out.screens = {};
  for (const r of routes) {
    await visit(r);
    out.screens[r] = { heading: /id="view-title"/.test(view()), title: document.title, words: text(view()).length };
  }
""",
        tmp_path,
    )
    for route, screen in out["screens"].items():
        assert screen["heading"], f"{route} drew no heading"
        assert screen["title"].endswith("— Threefold"), f"{route} left the document title as {screen['title']!r}"
        assert screen["words"] > 80, f"{route} drew almost nothing"


# -------------------------------------------------------------- shared layer


@pytest.mark.parametrize("page", SHARED_LAYER_PAGES)
def test_every_page_loads_the_shared_layer(page: str) -> None:
    body = page_source(page)
    assert '<script src="assets/threefold.js"></script>' in body
    assert '<link rel="stylesheet" href="assets/threefold.css" />' in body
    # In the head, so it has run before the page's own script reaches for it.
    assert body.index('src="assets/threefold.js"') < body.index("</head>")


def test_the_shared_layer_is_told_the_api_base_as_the_pages_are() -> None:
    served = page_source("assets/threefold.js")
    assert "__THREEFOLD_BASE_PATH__" not in served
    assert 'var SERVED_BASE_PATH = "/prod";' in served
    assert "'http://localhost:8001'" in served, "Opened from disk, the layer falls back to the local server as the pages do"
    assert "if (typeof str !== 'string') str = str == null ? '' : String(str);" in served


def test_the_shared_layer_holds_a_project_name_to_the_pattern_the_stack_deploys_with() -> None:
    match = re.search(r"var PROJECT_PATTERN = /(.+?)/;", page_source("assets/threefold.js"))
    assert match, "threefold.js has no PROJECT_PATTERN"
    assert match.group(1) == _deployed_project_pattern()


def test_a_sign_in_goes_as_a_bearer_token_and_the_pasted_key_only_without_one(tmp_path: Path) -> None:
    out = dash(
        r"""
  const later = new Date(Date.now() + 3600e3).toISOString();
  store['threefold-session'] = JSON.stringify({ token: 'tok-1', expires_at: later });
  store['threefold-operator-key'] = 'op-key-9';
  out.both = Threefold.authHeaders();
  out.typed = Threefold.authHeaders({}, 'typed-key');
  delete store['threefold-session'];
  out.keyOnly = Threefold.authHeaders();
  delete store['threefold-operator-key'];
  out.none = Threefold.authHeaders();

  store['threefold-session'] = JSON.stringify({ token: 'tok-epoch', expires_at: Math.floor(Date.now() / 1000) + 3600 });
  out.epoch = Threefold.authHeaders();

  store['threefold-session'] = JSON.stringify({ token: 'tok-1', expires_at: later });
  answer = contract();
  calls.length = 0;
  await visit('#/projects');
  out.sent = calls.find(c => c.url.indexOf('/api/projects') !== -1).headers;
""",
        tmp_path,
    )
    assert out["both"].get("Authorization") == "Bearer tok-1" and "X-API-Key" not in out["both"]
    assert out["typed"].get("X-API-Key") == "typed-key" and "Authorization" not in out["typed"], "A key typed into a page wins"
    assert out["keyOnly"].get("X-API-Key") == "op-key-9" and "Authorization" not in out["keyOnly"]
    assert "X-API-Key" not in out["none"] and "Authorization" not in out["none"]
    assert out["epoch"].get("Authorization") == "Bearer tok-epoch", "expires_at in epoch seconds is read as a time, not as expired"
    assert out["sent"].get("Authorization") == "Bearer tok-1"


def test_an_expired_unreadable_or_unstorable_sign_in_is_never_sent_and_never_breaks_a_page(tmp_path: Path) -> None:
    out = dash(
        r"""
  store['threefold-session'] = JSON.stringify({ token: 'tok-old', expires_at: new Date(Date.now() - 1000).toISOString() });
  out.expired = Threefold.authHeaders();
  out.expiredForgotten = !('threefold-session' in store);
  store['threefold-session'] = '{not json';
  out.junk = Threefold.authHeaders();
  storageBlocked = true;
  out.blocked = Threefold.authHeaders();
  out.kept = Threefold.writeSession('tok-memory', null);
  out.memory = Threefold.authHeaders();
  answer = contract();
  await visit('#/projects');
  out.drew = /Acme-Billing/.test(view());
""",
        tmp_path,
    )
    for case in ("expired", "junk", "blocked"):
        assert "Authorization" not in out[case] and "X-API-Key" not in out[case], case
    assert out["expiredForgotten"]
    assert out["kept"] is False, "With storage blocked a sign-in lives in memory, and says so"
    assert out["memory"].get("Authorization") == "Bearer tok-memory"
    assert out["drew"], "Blocked storage must not stop the page drawing"


def test_a_refusal_becomes_a_signed_out_state_that_says_what_to_run(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = api({ '/api/overview': { status: 401, body: { title: 'Unauthorized', detail: 'An operator credential is required.' } } });
  await visit('#/overview?days=14');
  out.anonymous = view();

  store['threefold-session'] = JSON.stringify({ token: 'tok-1', expires_at: new Date(Date.now() + 3600e3).toISOString() });
  await visit('#/overview?days=30');
  out.session = view();
  out.sessionForgotten = !('threefold-session' in store);

  store['threefold-operator-key'] = 'op-key-9';
  answer = api({ '/api/overview': { status: 403, body: { detail: 'Forbidden' } } });
  await visit('#/overview?days=1');
  out.key = view();
""",
        tmp_path,
    )
    for case in ("anonymous", "session", "key"):
        page = out[case]
        assert 'data-state="signed-out"' in page, case
        assert "python threefold.py open" in page, f"{case}: the signed-out state must say what to run"
        assert "settings.html#operator-key" in page, f"{case}: and name the fallback"
    assert "Sign in to read this stack" in out["anonymous"]
    assert "Your sign-in has ended" in out["session"]
    assert out["sessionForgotten"], "A sign-in the stack refused is dropped"
    assert "was not accepted" in out["key"]


def test_the_navigation_shows_the_stack_kind_and_the_sign_in_and_signs_out(tmp_path: Path) -> None:
    out = dash(
        r"""
  const later = new Date(Date.now() + 3600e3).toISOString();
  store['threefold-session'] = JSON.stringify({ token: 'tok-1', expires_at: later });
  answer = contract({
    '/api/auth/whoami': { status: 200, body: { authenticated: true, via: 'session', expires_at: later, reads_public: false, sandbox_writes: false } },
    'DELETE /api/auth/sessions': { status: 204, body: null }
  });
  Threefold.whoami(true);
  await visit('#/projects');
  out.nav = el('tf-nav').innerHTML;
  answer = contract({
    '/api/auth/whoami': { status: 200, body: { authenticated: false, via: null, expires_at: null, reads_public: false, sandbox_writes: false } },
    'DELETE /api/auth/sessions': { status: 204, body: null }
  });
  await Threefold.signOut();
  await tick();
  out.signout = calls.filter(c => c.method === 'DELETE').pop();
  out.forgotten = !('threefold-session' in store);
  out.after = el('tf-nav').innerHTML;
""",
        tmp_path,
    )
    nav = out["nav"]
    for item in ("Overview", "Review", "Projects", "Rules", "Sessions", "Connect", "Settings", "Demo"):
        assert f">{item}</a>" in nav, f"The navigation lacks {item}"
    assert 'aria-current="page"' in nav and ">Private stack<" in nav and "Signed in" in nav and "data-tf-signout" in nav
    assert out["signout"]["url"].endswith("/api/auth/sessions")
    assert out["signout"]["headers"].get("Authorization") == "Bearer tok-1", "Sign out revokes the token it presents"
    assert out["forgotten"]
    assert "Signed out" in out["after"]


# ------------------------------------------------- keys, numbers and escaping


def _served_files() -> list[Path]:
    return sorted(p for p in WEB.rglob("*") if p.suffix in (".html", ".js", ".css"))


def test_no_page_carries_an_operator_key() -> None:
    """A key in a served file would be handed to every visitor."""
    literal_header = re.compile(r"""['"]?(X-API-Key|Authorization)['"]?\s*[:=]\s*['"](Bearer\s+)?[A-Za-z0-9_\-]{6,}['"]""")
    stored_literal = re.compile(r"""setItem\(\s*(OPERATOR_KEY_STORAGE|SESSION_STORAGE|'threefold-operator-key'|'threefold-session')\s*,\s*['"]""")
    filled_field = re.compile(r"""id="(apiKeyInput|operatorKeyInput|api-key)"[^>]*\bvalue="[^"]+""")
    long_token = re.compile(r"(?<![A-Za-z0-9_\-/.#])[A-Za-z0-9]{32,}(?![A-Za-z0-9])")
    # The demo page replays one recorded run per scenario when offline. Those
    # fixtures are public hashes and a public signature over the public demo
    # ledger, verifiable by anyone, so the token-length rule does not apply to
    # them; what applies instead is the shape rule below, which fails on an
    # actual secret shape inside the recorded block.
    secret_shape = re.compile(
        r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
        r"sk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}|xox[abposr]-[A-Za-z0-9\-]{10,}|"
        r"gh[pousr]_[A-Za-z0-9_]{36,}|AIza[A-Za-z0-9_\-]{35}|"
        r"(?i:api[_-]?key|secret[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{8,}['\"]"
    )
    for path in _served_files():
        body = path.read_text(encoding="utf-8")
        name = path.relative_to(WEB).as_posix()
        assert not literal_header.search(body), f"{name} sends a literal credential"
        assert not stored_literal.search(body), f"{name} stores a literal credential"
        assert not filled_field.search(body), f"{name} ships a key field already filled"
        body = _without_recorded_block(body, name, secret_shape)
        for token in long_token.findall(body):
            assert len(set(token)) == 1, f"{name} carries a long token-like string: {token[:12]}…"


def _without_recorded_block(body: str, name: str, secret_shape: "re.Pattern[str]") -> str:
    """The page without its recorded offline fixtures, which are checked apart."""
    marker = "const RECORDED = {"
    start = body.find(marker)
    if start < 0:
        return body
    depth = 0
    index = start + len(marker) - 1
    in_string: str | None = None
    while index < len(body):
        char = body[index]
        if in_string is not None:
            if char == in_string and body[index - 1] != "\\":
                in_string = None
        elif char in ("'", '"'):
            in_string = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                if end < len(body) and body[end] == ";":
                    end += 1
                recorded = body[start:end]
                found = secret_shape.search(recorded)
                assert not found, f"{name} carries a recorded secret shape: {found.group(0)[:12]}…"
                return body[:start] + body[end:]
        index += 1
    raise AssertionError(f"{name} opens a RECORDED block it never closes")


def test_the_dashboard_invents_no_numbers(tmp_path: Path) -> None:
    """A past audit flagged canned numbers. Every figure here must come from an answer."""
    body = page_source("dashboard.html")
    shell = body.split('<div id="view"', 1)[1].split('<p id="live-status"', 1)[0]
    words = re.sub(r"<[^>]+>", " ", shell.split(">", 1)[1])
    assert not re.search(r"\d", words), "The page's own markup carries a number before the API has answered"

    out = dash(
        r"""
  answer = () => 'network';
  out.offline = {};
  for (const r of ['#/overview', '#/projects', '#/projects/Acme-Billing', '#/review', '#/calls', '#/call?timestamp=t&verdict_id=v', '#/connect', '#/try']) {
    await visit(r);
    out.offline[r] = metrics(view());
  }
  const canary = { calls: 90001, approved: 80021, refused: 3007, would_refuse: 6973, needs_review: 4111, false_alarms: 13, projects: 17, agents: 3 };
  answer = contract({ '/api/overview': { status: 200, body: overviewBody(canary) } });
  await visit('#/overview?days=14');
  out.shown = metrics(view());
  out.expected = {};
  Object.keys(canary).forEach(k => { out.expected[k] = Threefold.num(canary[k]); });
""",
        tmp_path,
    )
    for route, found in out["offline"].items():
        assert found == {}, f"{route} showed figures with no answer from the service: {found}"
    shown = out["shown"]
    for key in ("calls", "refused", "would_refuse", "needs_review", "projects", "agents"):
        assert shown.get(key) == out["expected"][key], f"The {key} tile shows {shown.get(key)!r}, not the API's value"


def test_every_value_from_the_service_is_escaped_on_every_screen(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = hostile();
  out.screens = {};
  for (const r of ['#/overview', '#/projects', '#/projects/' + encodeURIComponent(EVIL), '#/review', '#/calls?project=' + encodeURIComponent(EVIL), '#/call?timestamp=t&verdict_id=v']) {
    await visit(r);
    out.screens[r] = view();
  }
  await visit('#/connect');
  answer = api({ '/api/decisions': { status: 200, body: { items: [row(1, { agent: EVIL, tool_name: EVIL, target: EVIL, observed_target: EVIL, hook_mode: EVIL, stage: EVIL, timestamp: new Date(Date.now() + 1000).toISOString() })] } }, '/dist/manifest.json': { status: 200, body: { installer_sha256: EVIL } } });
  await runIntervals();
  out.screens['#/connect'] = view() + el('connect-wait').innerHTML;
""",
        tmp_path,
    )
    for route, markup in out["screens"].items():
        assert "<img" not in markup and "<svg onload" not in markup, f"{route} wrote service data as markup"
        assert "&lt;img" in markup, f"{route} did not show the value at all, so this test proves nothing there"


def test_markup_reaches_the_page_only_through_the_escaping_helper() -> None:
    dashboard = page_source("dashboard.html")
    layer = page_source("assets/threefold.js")
    for name, source in (("dashboard.html", dashboard), ("assets/threefold.js", layer)):
        for sink in (r"outerHTML\s*=", r"insertAdjacentHTML", r"document\.write\("):
            assert not re.search(sink, source), f"{name} writes markup through {sink}"
    assert not re.search(r"\.innerHTML\s*=(?!=)", dashboard), "The dashboard writes innerHTML directly instead of through setHtml"
    assert len(re.findall(r"\.innerHTML\s*=(?!=)", layer)) == 1, "Only setHtml writes innerHTML in the shared layer"
    # raw() marks markup as trusted, so it may only ever take a literal or an icon.
    for name, source in (("dashboard.html", dashboard), ("assets/threefold.js", layer)):
        for argument in re.findall(r"(?<!function )\braw\(([^()]*)\)", source):
            if not argument.strip():
                continue  # a comment naming the function
            assert re.fullmatch(r"'[^']*'|(T\.)?ICONS\.[a-z]+", argument.strip()), f"{name} passes {argument!r} to raw()"


# ----------------------------------------------------------------- overview


def test_every_tile_bar_and_row_opens_the_rows_behind_it(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract();
  await visit('#/overview?days=7');
  out.calls = hrefs(view(), '#/calls').map(params);
  out.projects = hrefs(view(), '#/projects');
  out.review = hrefs(view(), '#/review');
  out.today = DAY(0);
  out.threeDaysAgo = DAY(3);
""",
        tmp_path,
    )
    links = out["calls"]

    def has(**wanted: str) -> bool:
        return any(link == {k: str(v) for k, v in wanted.items()} for link in links)

    # The tiles
    assert has(days=7) and has(kind="refused", days=7) and has(kind="observed", days=7)
    assert has(kind="observed", review="unreviewed", days=7)
    assert "#/projects" in out["projects"]
    # The chart: a segment opens that day's rows, reading back far enough to reach it
    assert has(kind="approved", day=out["threeDaysAgo"], days=4)
    assert has(kind="refused", day=out["threeDaysAgo"], days=4) and has(kind="observed", day=out["today"], days=1)
    # The outcome ring, by rule, by agent
    assert has(kind="approved", days=7)
    assert has(rule="java-domain-stays-pure", days=7) and has(rule="java-domain-stays-pure", kind="refused", days=7)
    assert has(rule="java-domain-stays-pure", kind="observed", days=7)
    assert has(agent="codex", days=7)
    # By project: the name opens the project, each number its rows, needs review the queue
    assert "#/projects/Acme-Catalog" in out["projects"]
    assert has(days=7, project="Acme-Catalog", kind="observed") and has(days=7, project="Acme-Billing")
    assert "#/review?project=Acme-Catalog" in out["review"]


def test_the_overview_says_what_to_do_when_nothing_was_governed(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract({ '/api/overview': { status: 200, body: Object.assign(overviewBody({ calls: 0, approved: 0, refused: 0, would_refuse: 0, needs_review: 0, false_alarms: 0, projects: 0, agents: 0 }), { series: [], by_agent: [], by_origin: [], by_rule: [], by_project: [] }) } });
  await visit('#/overview?days=7');
  out.view = view();
""",
        tmp_path,
    )
    page = out["view"]
    assert 'data-state="empty"' in page
    assert 'href="#/connect"' in page and 'href="#/try"' in page and 'href="#/overview?days=30"' in page
    assert "data-metric" not in page, "An empty window shows what to do next, not a wall of zeros"


def test_the_overview_refreshes_every_thirty_seconds_only_while_the_tab_is_visible(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract();
  await visit('#/overview?days=7');
  const reads = () => calls.filter(c => c.url.indexOf('/api/overview') !== -1).length;
  out.first = reads();
  out.every = Array.from(intervals.values()).map(i => i.ms);
  await runIntervals();
  out.visible = reads();
  document.visibilityState = 'hidden';
  await runIntervals();
  out.hidden = reads();
  document.visibilityState = 'visible';
  await visit('#/projects');
  await runIntervals();
  out.left = reads();
  await visit('#/overview?days=99');
  out.clamped = calls.filter(c => c.url.indexOf('/api/overview') !== -1).pop().url;
""",
        tmp_path,
    )
    assert 30000 in out["every"]
    assert out["visible"] == out["first"] + 1
    assert out["hidden"] == out["visible"], "A hidden tab must not spend the rate limit"
    assert out["left"] == out["hidden"], "Leaving the overview stops its refresh"
    assert out["clamped"].endswith("/api/overview?days=7"), "A window outside 1, 7, 14 or 30 falls back to 7"


# ----------------------------------------------------------------- projects


def test_the_project_screen_explains_the_stage_and_each_rules_readiness(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract();
  await visit('#/projects/Acme-Billing');
  out.observe = view();
  answer = contract({
    '/api/projects/Acme-Billing': { status: 200, body: detailBody('enforce', RULES.map(r => Object.assign({}, r, { mode_now: r.state === 'noisy' ? 'observe' : 'enforce' }))) },
    '/api/decisions': { status: 200, body: { items: [row(1, { agent: 'codex', hook_mode: 'observe' }), row(2)], next_cursor: null } }
  });
  await visit('#/projects/Acme-Billing?days=14');
  out.enforce = view();
  out.calls = calls.map(c => c.url);
""",
        tmp_path,
    )
    observe = out["observe"]
    assert "In Observe: the rules record, and refuse nothing" in observe
    assert "a call carrying a credential, which the hook refuses on the machine" in observe
    for chip in (">Ready<", ">Quiet<", ">Noisy<"):
        assert chip in observe
    assert "Keep observing: one flag was a false alarm." in observe
    assert "irm https://example.test/prod/install.py -OutFile threefold.py; py threefold.py connect --project Acme-Billing" in observe
    assert "curl -fsSL https://example.test/prod/install.py -o threefold.py &amp;&amp; python3 threefold.py connect --project Acme-Billing" in observe

    enforce = out["enforce"]
    assert "In Enforce: 2 rules refuse a call before it runs" in enforce
    assert "still observes" in enforce and "PROTECTED_PATH" in enforce
    assert 'data-action="demote-open"' in enforce and "Back to Observe" in enforce
    assert "capped to observe on its machine" in enforce, "A hook capped to observe under Enforce is flagged"
    assert "https://example.test/prod/api/projects/Acme-Billing?days=14" in out["calls"]


def test_promote_preselects_ready_and_quiet_rules_and_sends_the_choice(tmp_path: Path) -> None:
    out = dash(
        r"""
  let promoted = null;
  answer = contract({ 'POST /api/projects/Acme-Billing/promote': (u, i, body) => { promoted = body; return { status: 200, body: { stage: 'enforce' } }; } });
  await visit('#/projects/Acme-Billing');
  click('promote-open');
  const dialog = el('modal-root').innerHTML;
  out.checked = {};
  dialog.replace(/data-rule="([^"]+)"\s*(checked)?/g, (m, rule, checked) => { out.checked[rule] = !!checked; return m; });
  out.dialogText = text(dialog);
  await click('promote-confirm');
  await tick();
  out.first = promoted;
  click('promote-open');
  click('promote-toggle', { 'data-rule': 'PROTECTED_PATH', checked: true });
  click('promote-toggle', { 'data-rule': 'LOOP', checked: false });
  await click('promote-confirm');
  await tick();
  out.second = promoted;
  out.toast = text(el('toast-root').innerHTML);
""",
        tmp_path,
    )
    assert out["checked"] == {"java-domain-stays-pure": True, "LOOP": True, "PROTECTED_PATH": False}
    assert "Unchecked rules keep observing" in out["dialogText"]
    assert out["first"] == {"enforce": ["java-domain-stays-pure", "LOOP"]}
    assert out["second"] == {"enforce": ["java-domain-stays-pure", "PROTECTED_PATH"]}
    assert "Acme-Billing is in Enforce" in out["toast"]


def test_a_refused_promotion_says_it_needs_the_operator_and_changes_nothing(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract({ 'POST /api/projects/Acme-Billing/promote': { status: 401, body: { detail: 'Unauthorized' } } });
  await visit('#/projects/Acme-Billing');
  click('promote-open');
  await click('promote-confirm');
  await tick();
  out.error = el('promote-error').textContent;
  out.open = el('modal-root').innerHTML.length > 0;
""",
        tmp_path,
    )
    assert "needs the operator" in out["error"] and "python threefold.py open" in out["error"]
    assert out["open"], "The dialog stays open so the choice is not lost"


def test_demote_is_one_confirmed_click_back_to_observe(tmp_path: Path) -> None:
    out = dash(
        r"""
  let demoted = null;
  answer = contract({
    '/api/projects/Acme-Billing': { status: 200, body: detailBody('enforce') },
    'POST /api/projects/Acme-Billing/demote': (u, i, body) => { demoted = body; return { status: 200, body: { stage: 'observe' } }; }
  });
  await visit('#/projects/Acme-Billing');
  click('demote-open');
  out.dialog = text(el('modal-root').innerHTML);
  await click('demote-confirm');
  await tick();
  out.demoted = demoted;
  out.toast = text(el('toast-root').innerHTML);
""",
        tmp_path,
    )
    assert "stops refusing from the next call" in out["dialog"]
    assert out["demoted"] == {}
    assert "back in Observe" in out["toast"]


def test_the_projects_screen_lists_stage_counts_agents_and_hook_modes(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract();
  await visit('#/projects');
  out.view = view();
  out.calls = hrefs(view(), '#/calls').map(params);
""",
        tmp_path,
    )
    page = out["view"]
    for fact in ("Acme-Billing", "Acme-Catalog", "Enforce", "Observe · default", "Claude Code", "Antigravity", "managed", "unknown"):
        assert fact in page, f"The projects screen does not show {fact}"
    assert {"days": "7", "project": "Acme-Catalog", "kind": "observed"} in out["calls"]


# ------------------------------------------------------------------- review


def test_the_review_queue_groups_calls_by_project_and_rule(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract({ '/api/decisions': { status: 200, body: { items: [
    row(1), row(2), row(3, { project_name: 'Acme-Catalog', rule_key: 'PROTECTED_PATH', observed_rules: ['PROTECTED_PATH'], target: '.claude/settings.json', observed_target: '.claude/settings.json' })
  ], next_cursor: null } } });
  await visit('#/review');
  out.view = view();
  out.read = calls.filter(c => c.url.indexOf('/api/decisions') !== -1).pop().url;
""",
        tmp_path,
    )
    assert out["read"] == "https://example.test/prod/api/decisions?kind=observed&review=unreviewed&days=30&limit=200"
    page = out["view"]
    assert page.count("<section") == 2, "Two groups: Acme-Billing under one rule, Acme-Catalog under another"
    assert "All 2 correct" in page, "A group of several calls can be labelled at once"
    assert "All 1 correct" not in page, "A group of one call is labelled by its row's own buttons, not a bulk button too"
    assert page.count('data-action="label-one"') == 6, "Every call, the lone one included, has its Correct and False alarm"
    for fact in ("src/billing/domain/Invoice.java", ".claude/settings.json", "imports javax.persistence into the domain", "Claude Code"):
        assert fact in page


def test_a_label_shows_at_once_and_comes_back_when_the_service_refuses_it(tmp_path: Path) -> None:
    out = dash(
        r"""
  const items = [row(1), row(2)];
  let reply = held();
  const sent = [];
  answer = contract({
    '/api/decisions': { status: 200, body: { items, next_cursor: null } },
    'POST /api/projects/Acme-Billing/reviews': (u, i, body) => { sent.push(body); return reply.promise; }
  });
  await visit('#/review');
  const pending = click('label-one', { 'data-verdict': 'VERDICT-1', 'data-timestamp': items[0].timestamp, 'data-label': 'correct', 'data-group-index': '0' });
  out.whilePending = /data-verdict="VERDICT-1"/.test(view());
  reply.release({ status: 500, body: { detail: 'The table is unavailable.' } });
  await pending;
  await tick();
  out.afterFailure = /data-verdict="VERDICT-1"/.test(view());
  out.failure = el('review-error').textContent;

  reply = held();
  const second = click('label-one', { 'data-verdict': 'VERDICT-1', 'data-timestamp': items[0].timestamp, 'data-label': 'false_alarm', 'data-group-index': '0' });
  reply.release({ status: 200, body: { updated: 0, skipped: [{ verdict_id: 'VERDICT-1', reason: 'belongs to another project' }] } });
  await second;
  await tick();
  out.afterSkip = /data-verdict="VERDICT-1"/.test(view());
  out.skip = el('review-error').textContent;
  out.sent = sent;
""",
        tmp_path,
    )
    assert out["whilePending"] is False, "The label shows at once, before the service answers"
    assert out["afterFailure"] is True, "A refused label puts the call back in the queue"
    assert "were not saved" in out["failure"] and "back in the queue" in out["failure"]
    assert out["afterSkip"] is True and "belongs to another project" in out["skip"]
    first = out["sent"][0]["items"][0]
    assert first["verdict_id"] == "VERDICT-1" and first["label"] == "correct" and first["timestamp"]


def test_a_group_is_labelled_in_one_request_with_its_note_and_can_be_undone(tmp_path: Path) -> None:
    out = dash(
        r"""
  const sent = [];
  answer = contract({
    '/api/decisions': { status: 200, body: { items: [row(1), row(2)], next_cursor: null } },
    'POST /api/projects/Acme-Billing/reviews': (u, i, body) => { sent.push(body); return { status: 200, body: { updated: body.items.length, skipped: [] } }; }
  });
  await visit('#/review');
  el('note-0').value = 'checked with the billing team';
  await click('label-group', { 'data-group': JSON.stringify(['Acme-Billing', 'java-domain-stays-pure']), 'data-label': 'false_alarm' });
  await tick();
  out.emptied = /data-state="empty"/.test(view());
  const toastId = (el('toast-root').innerHTML.match(/data-toast="(toast-\d+)"/) || [])[1];
  await click('toast', { 'data-toast': toastId });
  await tick();
  out.restored = (view().match(/data-action="label-one"/g) || []).length;
  out.sent = sent;
""",
        tmp_path,
    )
    batch, undo = out["sent"]
    assert [item["verdict_id"] for item in batch["items"]] == ["VERDICT-1", "VERDICT-2"]
    assert all(item["label"] == "false_alarm" and item["note"] == "checked with the billing team" for item in batch["items"])
    assert out["emptied"], "With the group labelled, the queue says there is nothing left"
    assert [item["label"] for item in undo["items"]] == ["clear", "clear"]
    assert out["restored"] == 4, "Undo puts both calls back, each with its two buttons"


def test_a_group_larger_than_the_route_accepts_is_sent_in_the_chunks_it_takes(tmp_path: Path) -> None:
    """The queue reads 200 at a time, but POST .../reviews takes 100 an item.

    A noisy rule, which is exactly what bulk labelling exists for, therefore
    makes a group the route rejects whole. The page sends it in chunks instead.
    """
    out = dash(
        r"""
  const many = [];
  for (let i = 1; i <= 120; i++) many.push(row(i));
  const sent = [];
  answer = contract({
    '/api/decisions': { status: 200, body: { items: many, next_cursor: null } },
    'POST /api/projects/Acme-Billing/reviews': (u, i, body) => {
      sent.push(body.items.length);
      // The contract's own limit, as read_review_items enforces it.
      if (body.items.length > 100) return { status: 400, body: { detail: 'items holds ' + body.items.length + ' reviews; the limit is 100.' } };
      return { status: 200, body: { updated: body.items.length, skipped: [] } };
    }
  });
  await visit('#/review');
  out.button = /All 120 correct/.test(view());
  await click('label-group', { 'data-group': JSON.stringify(['Acme-Billing', 'java-domain-stays-pure']), 'data-label': 'correct' });
  await tick();
  out.emptied = /data-state="empty"/.test(view());
  out.error = el('review-error').textContent;
  out.labelled = sent.slice();
  const toastId = (el('toast-root').innerHTML.match(/data-toast="(toast-\d+)"/) || [])[1];
  await click('toast', { 'data-toast': toastId });
  await tick();
  out.undone = sent.slice(out.labelled.length);
  out.restored = (view().match(/data-action="label-one"/g) || []).length;
""",
        tmp_path,
    )
    assert out["button"], "The bulk button offers the whole group"
    assert out["labelled"] == [100, 20], "The group goes out in the chunks the route accepts"
    assert out["emptied"], "Every call in the group was labelled"
    assert out["error"] == "", f"Nothing was refused, but the page said: {out['error']}"
    assert out["undone"] == [100, 20], "Undo is chunked the same way"
    assert out["restored"] == 240, "Undo puts all 120 calls back, each with its two buttons"


def test_a_chunk_that_fails_puts_back_only_the_labels_that_did_not_land(tmp_path: Path) -> None:
    out = dash(
        r"""
  const many = [];
  for (let i = 1; i <= 120; i++) many.push(row(i));
  let seen = 0;
  answer = contract({
    '/api/decisions': { status: 200, body: { items: many, next_cursor: null } },
    'POST /api/projects/Acme-Billing/reviews': (u, i, body) => {
      seen += 1;
      return seen === 1
        ? { status: 200, body: { updated: body.items.length, skipped: [] } }
        : { status: 500, body: { detail: 'The table is unavailable.' } };
    }
  });
  await visit('#/review');
  await click('label-group', { 'data-group': JSON.stringify(['Acme-Billing', 'java-domain-stays-pure']), 'data-label': 'correct' });
  await tick();
  out.error = el('review-error').textContent;
  out.back = (view().match(/data-action="label-one"/g) || []).length / 2;
""",
        tmp_path,
    )
    assert out["back"] == 20, "Only the chunk that failed comes back into the queue"
    assert "were not saved" in out["error"]
    assert "20 calls back in the queue" in out["error"] and "100 calls had already been saved" in out["error"]


def test_the_chunk_that_landed_before_a_failure_is_offered_the_same_undo(tmp_path: Path) -> None:
    """Chunking made a group non-atomic, so a half-saved group needs its Undo.

    The rows of the chunk that landed are labelled on the service and gone from
    the queue. The error line says so; without an Undo beside it the only way
    back is the call screen, one row at a time.
    """
    out = dash(
        r"""
  const many = [];
  for (let i = 1; i <= 120; i++) many.push(row(i));
  let seen = 0;
  const sent = [];
  answer = contract({
    '/api/decisions': { status: 200, body: { items: many, next_cursor: null } },
    'POST /api/projects/Acme-Billing/reviews': (u, i, body) => {
      seen += 1;
      const labels = body.items.map(x => x.label).filter((v, k, a) => a.indexOf(v) === k).join('+');
      sent.push(labels + ':' + body.items.length);
      return seen === 2
        ? { status: 500, body: { detail: 'The table is unavailable.' } }
        : { status: 200, body: { updated: body.items.length, skipped: [] } };
    }
  });
  await visit('#/review');
  await click('label-group', { 'data-group': JSON.stringify(['Acme-Billing', 'java-domain-stays-pure']), 'data-label': 'correct' });
  await tick();
  out.offer = el('toast-root').innerHTML;
  const toastId = (out.offer.match(/data-toast="(toast-\d+)"/) || [])[1];
  await click('toast', { 'data-toast': toastId });
  await tick();
  out.sent = sent.slice();
  out.back = (view().match(/data-action="label-one"/g) || []).length / 2;
""",
        tmp_path,
    )
    assert "100 calls had already been marked correct." in out["offer"], f"No Undo was offered: {out['offer']!r}"
    assert ">Undo</button>" in out["offer"]
    assert out["sent"] == ["correct:100", "correct:20", "clear:100"], "The Undo clears exactly the chunk that landed"
    assert out["back"] == 120, "Every call in the group is back in the queue"


def test_an_undo_whose_second_chunk_fails_puts_back_the_calls_it_did_clear(tmp_path: Path) -> None:
    """Chunking made undo non-atomic, so a half-done undo must still be visible."""
    out = dash(
        r"""
  const many = [];
  for (let i = 1; i <= 120; i++) many.push(row(i));
  let seen = 0;
  answer = contract({
    '/api/decisions': { status: 200, body: { items: many, next_cursor: null } },
    'POST /api/projects/Acme-Billing/reviews': (u, i, body) => {
      seen += 1;
      // 1 and 2 label the group; 3 and 4 undo it, and the last one fails.
      return seen === 4
        ? { status: 500, body: { detail: 'The table is unavailable.' } }
        : { status: 200, body: { updated: body.items.length, skipped: [] } };
    }
  });
  await visit('#/review');
  await click('label-group', { 'data-group': JSON.stringify(['Acme-Billing', 'java-domain-stays-pure']), 'data-label': 'correct' });
  await tick();
  const toastId = (el('toast-root').innerHTML.match(/data-toast="(toast-\d+)"/) || [])[1];
  await click('toast', { 'data-toast': toastId });
  await tick();
  out.back = (view().match(/data-action="label-one"/g) || []).length / 2;
  out.toast = text(el('toast-root').innerHTML);
""",
        tmp_path,
    )
    assert out["back"] == 100, "The chunk the service did clear is unreviewed again, so it comes back"
    assert "100 calls back in the queue" in out["toast"] and "was not saved" in out["toast"]


# ------------------------------------------------------------ calls and call


def test_the_calls_screen_sends_every_filter_and_reads_on_with_the_cursor(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract();
  await visit('#/calls?project=Acme-Billing&rule=LOOP&kind=refused&review=correct&agent=codex&session=s-9&days=14');
  out.first = calls.filter(c => c.url.indexOf('/api/decisions') !== -1).pop().url;
  out.sentence = text(view());
  await click('more');
  await tick();
  out.more = calls.filter(c => c.url.indexOf('/api/decisions') !== -1).pop().url;
  el('f-kind').value = 'observed'; el('f-review').value = 'unreviewed'; el('f-project').value = 'Acme-Catalog';
  el('f-rule').value = ''; el('f-agent').value = ''; el('f-session').value = ''; el('f-days').value = '30';
  click('filter');
  await tick();
  out.hash = location.hash;
""",
        tmp_path,
    )
    first = out["first"]
    for part in ("days=14", "project=Acme-Billing", "rule=LOOP", "kind=refused", "review=correct", "agent=codex", "session=s-9", "limit=50"):
        assert part in first, f"The calls screen does not send {part}"
    assert "cursor=abc" in out["more"]
    assert "Refused calls marked correct in Acme-Billing under LOOP from Codex in session s-9 in the last 14 days" in out["sentence"]
    assert out["hash"] == "#/calls?kind=observed&review=unreviewed&project=Acme-Catalog&days=30"


def test_a_day_filter_reads_on_until_that_days_rows(tmp_path: Path) -> None:
    out = dash(
        r"""
  const at = (day, hh) => day + 'T' + hh + ':00:00Z';
  const target = DAY(2);
  answer = contract({ '/api/decisions': u => u.searchParams.get('cursor') === 'p2'
    ? { status: 200, body: { items: [row(7, { timestamp: at(target, '12'), status: 'BLOCKED_LOOP_DETECTED', observed_rules: [] }), row(8, { timestamp: at(DAY(3), '09') })], next_cursor: 'p3' } }
    : { status: 200, body: { items: [row(5, { timestamp: at(DAY(0), '08') }), row(6, { timestamp: at(DAY(1), '08') })], next_cursor: 'p2' } } });
  await visit('#/calls?kind=refused&day=' + target + '&days=3');
  out.reads = calls.filter(c => c.url.indexOf('/api/decisions') !== -1).map(c => c.url);
  out.view = view();
""",
        tmp_path,
    )
    assert len(out["reads"]) == 2 and "cursor=p2" in out["reads"][1]
    assert "VERDICT-7" in out["view"] and "VERDICT-5" not in out["view"] and "VERDICT-8" not in out["view"]
    assert 'data-action="more"' not in out["view"], "Past that day there is nothing more to read"


def test_a_day_the_read_has_not_got_back_to_says_so_rather_than_none_in_the_ledger(tmp_path: Path) -> None:
    """A bar on the overview links to that day, so the rows behind it exist.

    /api/decisions takes a window, not a date, so the day is filtered here and
    the read follows the cursor only so far. When one busy day fills every page
    of that budget, the screen must not claim the ledger holds nothing.
    """
    out = dash(
        r"""
  const target = DAY(4);
  let page = 0;
  // Every page is full of rows newer than the day asked for, as one busy day makes it.
  answer = contract({ '/api/decisions': () => {
    page += 1;
    return { status: 200, body: {
      items: [row(100 + page, { timestamp: DAY(0) + 'T09:00:00Z' }), row(200 + page, { timestamp: DAY(0) + 'T10:00:00Z' })],
      next_cursor: 'page-' + page } };
  } });
  await visit('#/calls?kind=approved&day=' + target + '&days=7');
  const decisions = calls.filter(c => c.url.indexOf('/api/decisions') !== -1);
  out.reads = decisions.length;
  out.limits = decisions.map(c => new URL(c.url).searchParams.get('limit'));
  out.view = view();
  out.text = text(view());
  await click('more');
  await tick();
  out.after = calls.filter(c => c.url.indexOf('/api/decisions') !== -1).length;
""",
        tmp_path,
    )
    assert out["reads"] == 5, "The first read follows the cursor to the end of its page budget"
    assert out["limits"] == ["200"] * 5, "A day filtered here asks for the largest page the contract allows"
    assert 'data-state="not-reached"' in out["view"] and 'data-state="empty"' not in out["view"]
    assert "none in the ledger" not in out["text"], "The rows behind that bar have not been read, not proved absent"
    assert "none among the 10 newest rows read so far" in out["text"]
    assert 'data-action="more"' in out["view"], "The reader can keep reading back towards that day"
    assert out["after"] == 10, "Load more reads on from the cursor the budget stopped at"
    assert "Drop the day filter" in out["view"], "The day is the filter to drop here"
    assert "Show 30 days" in out["view"], "Widening the window is a way out of this card too"


def test_a_filter_the_route_read_past_offers_only_the_ways_out_that_change_it(tmp_path: Path) -> None:
    """Empty with a cursor happens without a day filter too, and reads differently.

    GET /api/decisions filters its own rows and reads a bounded number of ledger
    pages, so an ordinary filter that matches nothing on a busy ledger comes back
    empty with a cursor. The screen must not count rows it never saw, must not
    offer to drop a day filter that is not set, and must keep the way to widen
    the window that the other empty card has.
    """
    out = dash(
        r"""
  // The route found no match inside its own page budget and says where to go on.
  answer = contract({ '/api/decisions': () => ({ status: 200, body: { items: [], next_cursor: 'more' } }) });
  await visit('#/calls?project=Acme-Nope&days=7');
  out.view = view();
  out.text = text(view());
  out.here = location.hash;
  out.links = hrefs(view(), '#/calls').filter((v, i, a) => a.indexOf(v) === i);
""",
        tmp_path,
    )
    assert 'data-state="not-reached"' in out["view"] and 'data-state="empty"' not in out["view"]
    assert "none in the ledger" not in out["text"], "Rows the route never reached are not proved absent"
    assert "0 newest rows" not in out["text"], "The screen counted no rows here, so it claims no count"
    assert "none in the rows read so far" in out["text"]
    assert "Drop the day filter" not in out["view"], "There is no day filter to drop"
    assert "Show 30 days" in out["view"], "The window can still be widened from this card"
    assert out["here"] == "#/calls?project=Acme-Nope&days=7"
    assert out["here"] not in out["links"], f"A way out leads back to this page: {out['links']}"


def test_the_call_screen_shows_the_row_the_rule_and_the_session_with_their_links(tmp_path: Path) -> None:
    out = dash(
        r"""
  let labelled = null;
  answer = contract({ 'POST /api/projects/Acme-Billing/reviews': (u, i, body) => { labelled = body; return { status: 200, body: { updated: 1, skipped: [] } }; } });
  await visit('#/call?timestamp=' + encodeURIComponent('2026-09-22T10:00:00Z') + '&verdict_id=VERDICT-1');
  out.read = calls.filter(c => c.url.indexOf('/api/decision?') !== -1).pop().url;
  out.view = view();
  el('call-note').value = 'right to flag';
  await click('call-label', { 'data-label': 'correct' });
  await tick();
  out.labelled = labelled;
  answer = api({ '/api/decision': { status: 404, body: { detail: 'not found' } } });
  await visit('#/call?timestamp=t&verdict_id=gone');
  out.missing = text(view());
""",
        tmp_path,
    )
    assert out["read"] == "https://example.test/prod/api/decision?timestamp=2026-09-22T10%3A00%3A00Z&verdict_id=VERDICT-1"
    page = out["view"]
    for fact in ("VERDICT-1", "java-domain-stays-pure", "src/billing/domain/Invoice.java", "javax.persistence", "**/billing/domain/**/*.java", "managed", "Layer crossed"):
        assert fact in page, f"The call screen does not show {fact}"
    assert "https://example.test/prod/sessions.html?session=s-1" in page
    assert "https://example.test/prod/swagger.html#/default/get_api_decision" in page
    (item,) = out["labelled"]["items"]
    assert (item["verdict_id"], item["label"], item["note"]) == ("VERDICT-1", "correct", "right to flag") and item["timestamp"]
    assert "not in the ledger" in out["missing"]


# ------------------------------------------------------------------ connect


def test_connect_writes_the_one_command_for_this_stack_and_watches_for_the_first_call(tmp_path: Path) -> None:
    out = dash(
        r"""
  let decisions = { items: [], next_cursor: null };
  answer = contract({ '/api/decisions': () => ({ status: 200, body: decisions }) });
  await visit('#/connect');
  out.posix = view();
  click('os', { 'data-os': 'powershell' });
  out.powershell = view();
  out.every = Array.from(intervals.values()).map(i => i.ms);
  out.poll = calls.filter(c => c.url.indexOf('/api/decisions') !== -1).pop().url;
  out.waiting = text(el('connect-wait').innerHTML);
  const polls = () => calls.filter(c => c.url.indexOf('/api/decisions') !== -1).length;
  const beforeHidden = polls();
  document.visibilityState = 'hidden';
  await runIntervals();
  await runIntervals();
  out.hiddenPolls = polls() - beforeHidden;
  document.visibilityState = 'visible';
  decisions = { items: [row(1, { agent: 'codex', timestamp: new Date(Date.now() + 1000).toISOString() })], next_cursor: null };
  await runIntervals();
  out.connected = text(view());
  const before = calls.length;
  await runIntervals();
  out.stopped = calls.length === before;
""",
        tmp_path,
    )
    assert "curl -fsSL https://example.test/prod/install.py -o threefold.py &amp;&amp; python3 threefold.py connect --project Acme-Billing" in out["posix"]
    assert "irm https://example.test/prod/install.py -OutFile threefold.py; py threefold.py connect --project Acme-Billing" in out["powershell"]
    assert "ab" * 32 in out["powershell"], "The installer's sha256 from dist/manifest.json is shown for the careful reader"
    assert 3000 in out["every"]
    assert out["poll"] == "https://example.test/prod/api/decisions?project=Acme-Billing&limit=1"
    assert "Watching for a call from Acme-Billing" in out["waiting"]
    assert out["hiddenPolls"] == 0, "A hidden tab must not spend the address's rate limit on the watch"
    assert "Connected" in out["connected"] and "Codex reached the stack from Acme-Billing" in out["connected"]
    assert "You are in Observe" in out["connected"]
    assert out["stopped"], "Once the first call arrived, the watch stops"


def test_connect_offers_no_command_for_a_name_the_stack_would_store_as_unlabelled(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract({ '/api/decisions': { status: 200, body: { items: [], next_cursor: null } } });
  await visit('#/connect');
  Dash.connect.setName('Globex-Billing');
  await tick();
  const before = calls.length;
  await runIntervals();
  out.polled = calls.length - before;
  out.view = text(view());
  out.problem = el('connect-name-problem').textContent;
  out.missingManifest = null;
""",
        tmp_path,
    )
    assert out["polled"] == 0, "A name outside the pattern is never watched for"
    assert "threefold.py connect --project Globex-Billing" not in out["view"]
    assert "unlabelled" in out["problem"]


def test_connect_says_so_when_the_stack_publishes_no_checksum(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = api({ '/api/decisions': { status: 200, body: { items: [] } } });
  await visit('#/connect');
  out.view = text(view());
""",
        tmp_path,
    )
    assert "The manifest could not be read (HTTP 404)" in out["view"]


# ------------------------------------------------------------------- signin


def test_sign_in_takes_the_code_out_of_the_address_and_keeps_only_a_session(tmp_path: Path) -> None:
    out = dash(
        r"""
  out.firstReplace = replaced[0];
  out.posted = calls.filter(c => c.method === 'POST').map(c => ({ url: c.url, body: c.body }));
  out.session = JSON.parse(store['threefold-session'] || 'null');
  out.keyGone = !('threefold-operator-key' in store);
  out.hash = location.hash;
  out.projectRead = calls.filter(c => c.url.indexOf('/api/projects/Acme-Billing') !== -1).map(c => c.headers.Authorization);
""",
        tmp_path,
        before=r"""
  openAt('#/signin?code=c-123&next=%2Fprojects%2FAcme-Billing');
  store['threefold-operator-key'] = 'op-key-9';
  answer = contract({ 'POST /api/auth/sessions': { status: 200, body: { token: 'tok-new', expires_at: new Date(Date.now() + 43200e3).toISOString(), ttl_seconds: 43200 } } });
""",
    )
    assert "code=" not in out["firstReplace"], "The code must leave the address bar before anything else happens"
    assert out["posted"] == [{"url": "https://example.test/prod/api/auth/sessions", "body": {"code": "c-123"}}]
    assert out["session"]["token"] == "tok-new" and set(out["session"]) == {"token", "expires_at"}
    assert out["keyGone"], "After a sign-in this browser holds a session, never the operator key"
    assert out["hash"] == "#/projects/Acme-Billing"
    assert "Bearer tok-new" in out["projectRead"]


def test_a_used_or_expired_sign_in_link_says_so_and_what_to_run(tmp_path: Path) -> None:
    out = dash(
        r"""
  out.view = text(view());
  out.session = store['threefold-session'] || null;
""",
        tmp_path,
        before=r"""
  openAt('#/signin?code=spent');
  answer = api({ 'POST /api/auth/sessions': { status: 401, body: { detail: 'The code is unknown, used or expired.' } } });
""",
    )
    assert "This sign-in link has already been used, or has expired" in out["view"]
    assert "python threefold.py open" in out["view"]
    assert out["session"] is None


def test_a_sign_in_that_answers_after_the_reader_moved_on_is_still_kept(tmp_path: Path) -> None:
    """The code is spent once the stack answers; dropping the session would waste it."""
    out = dash(
        r"""
  await visit('#/projects');
  reply.release({ status: 200, body: { token: 'tok-late', expires_at: null, ttl_seconds: 43200 } });
  await tick();
  out.session = JSON.parse(store['threefold-session'] || 'null');
  out.hash = location.hash;
""",
        tmp_path,
        before=r"""
  openAt('#/signin?code=c-9');
  const reply = held();
  answer = contract({ 'POST /api/auth/sessions': () => reply.promise });
""",
    )
    assert out["session"]["token"] == "tok-late"
    assert out["hash"] == "#/projects", "The reader stays where they went"


def test_a_sign_in_never_sends_the_reader_off_this_page(tmp_path: Path) -> None:
    out = dash(
        r"""
  out.next = ['/projects/Acme-Billing', '/review?project=Acme-Billing', '//evil.example/x', 'https://evil.example', 'javascript:alert(1)', '/signin?code=x', '/nowhere', ''].map(Dash.safeNext);
""",
        tmp_path,
    )
    assert out["next"] == ["/projects/Acme-Billing", "/review?project=Acme-Billing", "/overview", "/overview", "/overview", "/overview", "/overview", "/overview"]


# ---------------------------------------------------------------------- try


# The walkthrough lets a path break only after a slash, with <wbr>. A browser's
# text of the page has nothing there, and the text these scenarios read leaves
# it out too, so a path is found whole.
WALK = r"""
  const view = () => el('view').innerHTML.replace(/<wbr>/g, '');
"""


def walk(scenario: str, tmp_path: Path, before: str = "") -> dict:
    return dash(WALK + scenario, tmp_path, before=before)


def test_the_walkthrough_breaks_a_path_only_between_its_folders(tmp_path: Path) -> None:
    """A narrow line wraps a path at a slash, never inside a name or before its extension.

    The lanes, the flagged cards, the stack and its queue split
    'CartView.ts|x', 'test_order_totals|.py' and 'Order.jav|a' at 1440px,
    because they broke a path anywhere.
    """
    out = dash(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const target = 'src/main/java/com/acme/domain/Order.java';
  answer = api({
    'POST /api/sandbox': { status: 200, body: { project: P, calls_seeded: 1 } },
    ['/api/projects/' + P]: { status: 200, body: { project: P, config: { stage: 'observe' }, readiness: { rules: [] } } },
    '/api/decisions': { status: 200, body: { items: [row(1, { project_name: P, target, observed_target: target })], next_cursor: null } }
  });
  await visit('#/try');
  await click('try-create'); await tick();
  out.lanes = view();
  await click('try-show'); await tick();
  out.cards = view();
  await click('try-review'); await tick();
  out.stack = view();
""",
        tmp_path,
    )
    broken = "src/<wbr>main/<wbr>java/<wbr>com/<wbr>acme/<wbr>domain/<wbr>Order.java"
    assert out["lanes"].count(broken) == 1, "The lane does not break the path at its slashes"
    assert out["cards"].count(broken) == 1, "The flagged card does not break the path at its slashes"
    assert out["stack"].count(broken) == 2, "The card on top and its queue do not break the path at its slashes"
    css = page_source("dashboard.html").split("const TRY_CSS = `", 1)[1].split("`;", 1)[0]
    for rule in (".tf-try-call-target {", ".tf-try-dl dd {", ".tf-try-card-file {", ".tf-try-q-file {"):
        line = css.split(rule, 1)[1].split("}", 1)[0]
        assert "anywhere" not in line, f"{rule} still breaks a path anywhere"


def test_the_walkthrough_runs_the_rollout_from_sandbox_to_a_real_refusal(tmp_path: Path) -> None:
    out = walk(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const obs = (i, rule, target, extra) => row(i, Object.assign({ project_name: P, rule_key: rule, observed_rules: [rule], observed_rule: rule, target, observed_target: target }, extra || {}));
  const observed = [
    obs(1, 'python-domain-stays-pure', 'src/acme/domain/order.py', { agent: 'claude-code' }),
    obs(2, 'python-domain-stays-pure', 'src/acme/domain/invoice.py', { agent: 'codex' }),
    obs(3, 'PROTECTED_PATH', 'docs/.git-hooks-howto.md', { agent: 'antigravity' })
  ];
  let labelled = false;
  const sent = {};
  answer = api({
    'POST /api/sandbox': (u, i, body) => { sent.sandbox = body; return { status: 200, body: { project: P, calls_seeded: 12, url: 'dashboard.html#/projects/' + P } }; },
    ['/api/projects/' + P]: () => ({ status: 200, body: { project: P, config: { stage: 'observe', sandbox: true }, readiness: { summary: { stage: 'observe' }, rules: labelled
      ? [{ rule_key: 'python-domain-stays-pure', state: 'ready', mode_now: 'observe', recommendation: 'Promote' }, { rule_key: 'PROTECTED_PATH', state: 'noisy', mode_now: 'observe', recommendation: 'Keep observing' }, { rule_key: 'LOOP', state: 'quiet', mode_now: 'observe', recommendation: 'Nothing flagged' }]
      : [{ rule_key: 'python-domain-stays-pure', state: 'needs_review', mode_now: 'observe' }, { rule_key: 'PROTECTED_PATH', state: 'needs_review', mode_now: 'observe' }, { rule_key: 'LOOP', state: 'quiet', mode_now: 'observe' }] } } }),
    '/api/decisions': { status: 200, body: { items: observed, next_cursor: null } },
    ['POST /api/projects/' + P + '/reviews']: (u, i, body) => { (sent.reviews = sent.reviews || []).push(body.items[0]); labelled = (sent.reviews.length >= 3); return { status: 200, body: { updated: 1, skipped: [] } }; },
    ['POST /api/projects/' + P + '/promote']: (u, i, body) => { sent.promote = body; return { status: 200, body: { stage: 'enforce' } }; },
    '/api/decision': { status: 200, body: { decision: observed[0], session: null, rule: { id: 'python-domain-stays-pure', when_path_matches: ['**/domain/**'], forbid_imports: ['**.infrastructure.**', 'boto3'] } } },
    'POST /evaluate-tool-call': (u, i, body) => { sent.evaluate = body; return { status: 200, body: { status: 'BLOCKED_BOUNDARY_VIOLATION', reason: 'The domain may not import boto3.', bedrock_explanation: 'A domain module that imports an AWS client couples the model to infrastructure.', explanation_source: 'bedrock', project_stage: 'enforce' } }; }
  });
  await visit('#/try');
  out.step1 = text(view());
  await click('try-create'); await tick();
  out.step2 = text(view());
  await click('try-show'); await tick();
  out.cards = text(view());
  await click('try-review'); await tick();
  out.step3 = text(view());
  await click('try-label', { 'data-verdict': 'VERDICT-1', 'data-label': 'correct' });
  await click('try-label', { 'data-verdict': 'VERDICT-2', 'data-label': 'correct' });
  await click('try-label', { 'data-verdict': 'VERDICT-3', 'data-label': 'false_alarm' });
  await tick();
  out.labelled = text(view());
  await click('try-readiness'); await tick();
  out.step4 = view();
  await click('try-promote'); await tick();
  out.promoted = text(view());
  await click('try-next'); await tick();
  out.step5 = text(view());
  await click('try-send'); await tick();
  out.done = text(view());
  await click('try-finish'); await tick();
  out.finish = view();
  out.sent = sent;
  out.saved = JSON.parse(store['threefold-try'] || 'null');
""",
        tmp_path,
    )
    assert "Make my sandbox" in out["step1"]
    assert out["sent"]["sandbox"] == {}
    assert "12 calls arrived" in out["step2"] and "Every call was judged and recorded in Acme-Sandbox-0a1b2c3d" in out["step2"]
    assert "the project is in Observe" in out["step2"]
    # The calls as they arrived, in a lane per agent, and how many of them the list held.
    for agent in ("Claude Code", "Codex", "Antigravity"):
        assert agent in out["step2"], f"The timeline has no lane for {agent}"
    assert "The list read back 3 of the 12 so far" in out["step2"], "A short read of the ledger is said, never padded"
    # Step 2: each flagged call as a card of agent, file, rule and why.
    cards = out["cards"]
    assert "3 calls would have been refused" in cards
    for part in ("Claude Code", "src/acme/domain/order.py", "python-domain-stays-pure", "PROTECTED_PATH", "imports javax.persistence into the domain"):
        assert part in cards, f"The step-2 cards do not show {part!r}"
    assert "One of them is a false alarm" in out["step3"] and "docs/.git-hooks-howto.md" in out["step3"]
    assert [r["label"] for r in out["sent"]["reviews"]] == ["correct", "correct", "false_alarm"]
    assert "3 of 3 labelled" in out["labelled"] and "Every call has a label" in out["labelled"]
    checked = dict(re.findall(r'data-rule="([^"]+)"\s*(checked)?', out["step4"]))
    assert checked == {"python-domain-stays-pure": "checked", "PROTECTED_PATH": "", "LOOP": "checked"}, "Ready and Quiet are checked; the noisy rule keeps observing"
    # A rule's state animates from the one read before the labels to the one read after, and only where it changed.
    assert out["step4"].count("tf-try-flipping") == 2, "python-domain-stays-pure and PROTECTED_PATH changed; LOOP did not"
    assert out["sent"]["promote"] == {"enforce": ["python-domain-stays-pure", "LOOP"]}
    assert "Now in Enforce" in out["promoted"] and "Enforces" in out["promoted"] and "Keeps observing" in out["promoted"]
    assert "now in Enforce. This time the rule in force refuses it" in out["step5"]
    assert "src/acme/domain/order.py" in out["step5"] and "import boto3" in out["step5"]
    evaluate = out["sent"]["evaluate"]
    assert evaluate["origin"] == "hook" and evaluate["agent"] == "claude-code" and evaluate["explain"] is True
    assert evaluate["project_name"] == "Acme-Sandbox-0a1b2c3d" and evaluate["tool_name"] == "Write"
    assert evaluate["arguments"] == {"file_path": "src/acme/domain/order.py", "content": "import boto3\n"}
    assert "Refused, before it ran" in out["done"] and "BLOCKED_BOUNDARY_VIOLATION" in out["done"]
    assert "couples the model to infrastructure" in out["done"] and "Amazon Bedrock" in out["done"]
    assert out["saved"]["project"] == "Acme-Sandbox-0a1b2c3d", "A reload can resume the same sandbox"
    # The completion screen: three facts read from the responses, and three ways on.
    finish = out["finish"]
    words = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", finish))
    assert "That was the whole rollout" in words
    assert "12 calls judged in Observe" in words and "3 calls would have been refused, and none was" in words
    assert "You marked 2 calls correct and 1 call a false alarm, so PROTECTED_PATH stayed in Observe" in words
    assert "2 rules moved to Enforce" in words
    # Plain words first, the precise verdict second.
    assert "Stopped by python-domain-stays-pure before it ran, with a sentence from Amazon Bedrock. Its verdict: BLOCKED_BOUNDARY_VIOLATION." in words
    for href in ('href="#/connect"', 'href="#/projects/Acme-Sandbox-0a1b2c3d"', 'href="#/proof"'):
        assert href in finish, f"The completion screen does not offer {href}"


def test_the_completion_counts_the_rules_the_promotion_s_answer_put_in_force(tmp_path: Path) -> None:
    """The second completion fact reads the promotion as the service recorded it.

    Its figure was counted from the list the page sent, while its source line
    names the promotion's answer. And a rule's bar, one mark per flag, says
    how many more there are past the 24 it draws rather than stopping short.
    """
    out = walk(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const observed = [row(1, { project_name: P, rule_key: 'java-domain-stays-pure' })];
  answer = api({
    'POST /api/sandbox': { status: 200, body: { project: P, calls_seeded: 12 } },
    ['/api/projects/' + P]: { status: 200, body: { project: P, config: { stage: 'observe' }, readiness: { rules: [
      { rule_key: 'java-domain-stays-pure', kind: 'layering', state: 'ready', would_refuse: 30, correct: 30, false_alarms: 0, unreviewed: 0 },
      { rule_key: 'LOOP', kind: 'gate', state: 'quiet', would_refuse: 0, correct: 0, false_alarms: 0, unreviewed: 0 }] } } },
    '/api/decisions': { status: 200, body: { items: observed, next_cursor: null } },
    ['POST /api/projects/' + P + '/reviews']: { status: 200, body: { updated: 1, skipped: [] } },
    ['POST /api/projects/' + P + '/promote']: { status: 200, body: { project: P, config: { stage: 'enforce', observe_rules: ['LOOP'],
      history: [{ at: NOW, action: 'create', enforce: [], observe: [] }, { at: NOW, action: 'promote', by: 'anonymous', enforce: ['java-domain-stays-pure'], observe: ['LOOP'] }] } } },
    '/api/decision': { status: 200, body: { decision: observed[0], session: null, rule: { id: 'java-domain-stays-pure', forbid_imports: ['javax.persistence'] } } },
    '/rules': { status: 200, body: { rules: [] } },
    'POST /evaluate-tool-call': { status: 200, body: { status: 'BLOCKED_BOUNDARY_VIOLATION', reason: 'Refused.', project_stage: 'enforce' } }
  });
  await visit('#/try');
  await click('try-create'); await tick();
  await click('try-show'); await tick();
  await click('try-review'); await tick();
  await click('try-label', { 'data-verdict': 'VERDICT-1', 'data-label': 'correct' }); await tick();
  await click('try-readiness'); await tick();
  out.step4 = view();
  await click('try-promote'); await tick();
  out.sent = calls.filter(c => c.url.indexOf('/promote') !== -1).pop().body;
  out.promoted = text(view());
  await click('try-next'); await tick();
  await click('try-send'); await tick();
  await click('try-finish'); await tick();
  out.done = text(view());
""",
        tmp_path,
    )
    assert out["sent"] == {"enforce": ["java-domain-stays-pure", "LOOP"]}
    assert "1 rule now refuses the calls that break it; LOOP keeps observing." in out["promoted"]
    assert "1 rule moved to Enforce" in out["done"], "The figure is the request's, not the answer's"
    assert out["step4"].count('class="tf-try-bar-seg"') == 24 and 'class="tf-try-bar-more">+6<' in out["step4"]


def test_the_walkthrough_is_labelled_by_keyboard_alone(tmp_path: Path) -> None:
    """C and F label the call on top of the stack and the arrows move through it.

    Only on the labelling step, never while the reader types or holds a
    modifier, and not at all once the reader has left the walkthrough. The keys
    go through the same path a click does, so each one is saved as a review.
    """
    out = dash(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const obs = (i, rule, target, agent) => row(i, { project_name: P, rule_key: rule, observed_rules: [rule], observed_rule: rule, target, observed_target: target, agent });
  const observed = [
    obs(1, 'python-domain-stays-pure', 'src/acme/domain/order.py', 'claude-code'),
    obs(2, 'python-domain-stays-pure', 'tests/domain/test_order_totals.py', 'codex'),
    obs(3, 'web-domain-stays-pure', 'src/web/domain/cart.ts', 'antigravity')
  ];
  const reviews = [];
  answer = api({
    'POST /api/sandbox': { status: 200, body: { project: P, calls_seeded: 12 } },
    ['/api/projects/' + P]: { status: 200, body: { project: P, config: { stage: 'observe' }, readiness: { rules: [] } } },
    '/api/decisions': { status: 200, body: { items: observed, next_cursor: null } },
    ['POST /api/projects/' + P + '/reviews']: (u, i, body) => { reviews.push([body.items[0].verdict_id, body.items[0].label]); return { status: 200, body: { updated: 1, skipped: [] } }; },
    '/api/overview': { status: 200, body: overviewBody() }
  });
  function press(key, extra) {
    const event = Object.assign({ key, target: el('view'), prevented: false, preventDefault() { this.prevented = true; } }, extra || {});
    (docListeners.keydown || []).forEach(fn => fn(event));
    return event;
  }
  await visit('#/try');
  await click('try-create'); await tick();
  await click('try-show'); await tick();
  out.beforeTheStep = press('c').prevented;
  await click('try-review'); await tick();
  out.first = Dash.tryState.cursor;
  out.order = Dash.tryState.observed.map(r => r.verdict_id);
  out.hint = text(view());
  out.c = press('c').prevented; await tick();
  out.afterC = Dash.tryState.cursor;
  out.focusAfterC = document.activeElement && document.activeElement.id;
  out.describedBy = (/id="try-correct"[^>]*aria-describedby="([^"]*)"/.exec(view()) || [])[1] || '';
  out.named = ['try-card-count', 'try-card-file', 'try-card-rule'].filter(id => view().indexOf('id="' + id + '"') !== -1);
  out.spoken = el('live-status').textContent;
  out.typing = press('f', { target: { tagName: 'INPUT' } }).prevented;
  out.modified = press('f', { ctrlKey: true }).prevented;
  out.right = press('ArrowRight').prevented;
  out.afterRight = Dash.tryState.cursor;
  out.left = press('ArrowLeft').prevented;
  out.afterLeft = Dash.tryState.cursor;
  press('F'); await tick();
  press('c'); await tick();
  out.reviews = reviews.slice();
  out.done = text(view());
  out.focused = document.activeElement && document.activeElement.id;
  await visit('#/overview');
  press('c'); await tick();
  out.afterLeaving = reviews.length;
""",
        tmp_path,
    )
    assert out["beforeTheStep"] is False, "A key does nothing before the labelling step"
    assert out["first"] == 0 and out["c"] is True and out["afterC"] == 1, "C labels the top call and the next one comes up"
    assert out["focusAfterC"] == "try-correct", "The keyboard stays on the stack"
    # The button the keyboard lands on names the call it now labels, and the announcement says which is next.
    assert out["describedBy"] == "try-card-count try-card-file try-card-rule" and len(out["named"]) == 3
    assert out["spoken"] == "Marked Correct: src/web/domain/cart.ts. 1 of 3 labelled. Next: tests/domain/test_order_totals.py."
    assert "Keys: C correct F false alarm" in out["hint"]
    assert out["typing"] is False and out["modified"] is False, "Typing and a held modifier are left alone"
    assert out["right"] is True and out["afterRight"] == 2 and out["left"] is True and out["afterLeft"] == 1
    # The stack runs in the order the calls arrived: VERDICT-3 is the oldest of the three.
    assert out["order"] == ["VERDICT-3", "VERDICT-2", "VERDICT-1"]
    assert out["reviews"] == [["VERDICT-3", "correct"], ["VERDICT-2", "false_alarm"], ["VERDICT-1", "correct"]]
    assert "3 of 3 labelled" in out["done"] and "Every call has a label" in out["done"]
    assert out["focused"] == "try-primary", "With every call labelled, the keyboard lands on the way on"
    assert out["afterLeaving"] == 3, "The keys stop listening when the reader leaves the walkthrough"


def test_a_held_key_labels_one_call_and_a_failed_label_keeps_the_keyboard_on_the_stack(tmp_path: Path) -> None:
    """Holding C labels the call on top once, not every call left in a burst.

    And a label the service does not keep is taken back with the keyboard on
    the call, not on the way on, which is disabled until every call has one.
    """
    out = dash(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const obs = (i, target) => row(i, { project_name: P, target, observed_target: target });
  let refuse = false;
  const reviews = [];
  answer = api({
    'POST /api/sandbox': { status: 200, body: { project: P, calls_seeded: 12 } },
    ['/api/projects/' + P]: { status: 200, body: { project: P, config: { stage: 'observe' }, readiness: { rules: [] } } },
    '/api/decisions': { status: 200, body: { items: [obs(1, 'src/acme/domain/a.py'), obs(2, 'src/acme/domain/b.py'), obs(3, 'src/acme/domain/c.py')], next_cursor: null } },
    ['POST /api/projects/' + P + '/reviews']: (u, i, body) => { reviews.push(body.items[0].verdict_id); return refuse ? { status: 500, body: { detail: 'down' } } : { status: 200, body: { updated: 1, skipped: [] } }; }
  });
  function press(key, extra) {
    const event = Object.assign({ key, target: el('view'), preventDefault() {} }, extra || {});
    (docListeners.keydown || []).forEach(fn => fn(event));
  }
  await visit('#/try');
  await click('try-create'); await tick();
  await click('try-show'); await tick();
  await click('try-review'); await tick();
  press('c'); await tick();
  press('c', { repeat: true }); press('c', { repeat: true }); await tick();
  out.afterHold = reviews.length;
  out.receipt = text(view().replace(/<wbr>/g, ''));
  press('c'); await tick();
  refuse = true;
  press('f'); await tick();
  out.focused = document.activeElement && document.activeElement.id;
  out.error = text(view());
""",
        tmp_path,
    )
    assert out["afterHold"] == 1, "A held key labelled more than the call on top"
    assert "Marked Correct : src/acme/domain/c.py" in out["receipt"], "The labelled call leaves a receipt on screen"
    assert "That did not work" in out["error"]
    assert out["focused"] == "try-correct", "A failed label drops the keyboard on a disabled button"


def test_the_walkthrough_reads_readiness_only_after_every_label_is_saved(tmp_path: Path) -> None:
    """A reader who labels the last call and presses on at once sees that label counted.

    The last label moves the keyboard to the way on, so Enter can follow it
    within milliseconds, while its save is still in flight; readiness read
    then would show the rule just labelled as still needing review, and leave
    it unchecked. The read waits for every save.
    """
    out = dash(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const only = row(1, { project_name: P });
  const hold = held();
  answer = api({
    'POST /api/sandbox': { status: 200, body: { project: P, calls_seeded: 12 } },
    ['/api/projects/' + P]: { status: 200, body: { project: P, config: { stage: 'observe' }, readiness: { rules: [{ rule_key: 'java-domain-stays-pure', state: 'ready' }] } } },
    '/api/decisions': { status: 200, body: { items: [only], next_cursor: null } },
    ['POST /api/projects/' + P + '/reviews']: () => hold.promise.then(() => ({ status: 200, body: { updated: 1, skipped: [] } }))
  });
  const reads = () => calls.filter(c => c.method === 'GET' && new URL(c.url).pathname.endsWith('/api/projects/' + P)).length;
  await visit('#/try');
  await click('try-create'); await tick();
  await click('try-show'); await tick();
  await click('try-review'); await tick();
  out.readsBefore = reads();
  click('try-label', { 'data-verdict': 'VERDICT-1', 'data-label': 'correct' });
  await tick();
  const pressing = click('try-readiness');
  await tick();
  out.readsWhileSaving = reads();
  out.busyWhileSaving = Dash.tryState.busy;
  hold.release();
  await pressing; await tick();
  out.readsAfter = reads();
  out.step = Dash.tryState.step;
""",
        tmp_path,
    )
    assert out["readsWhileSaving"] == out["readsBefore"], "Readiness was read while a label was still being saved"
    assert out["busyWhileSaving"] is True, "The way on shows it is waiting"
    assert out["readsAfter"] == out["readsBefore"] + 1 and out["step"] == 4


def test_a_label_the_service_does_not_keep_holds_the_reader_on_the_call_it_was_for(tmp_path: Path) -> None:
    """A save that fails while "See what that did" waits on it never moves the reader to step 4.

    The wait once resolved whether the save kept the label or not, so the
    reader landed on step 4 with that label taken back, the rail ticking step
    3 at "4 of 5 labelled", a rule reading Needs review, one rule fewer to
    promote, and no way back to label the call again.
    """
    out = dash(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const rows = [row(1, { project_name: P, target: 'src/acme/domain/a.py', observed_target: 'src/acme/domain/a.py' }), row(2, { project_name: P, target: 'src/acme/domain/b.py', observed_target: 'src/acme/domain/b.py' })];
  const hold = held();
  let saves = 0;
  answer = api({
    'POST /api/sandbox': { status: 200, body: { project: P, calls_seeded: 12 } },
    ['/api/projects/' + P]: { status: 200, body: { project: P, config: { stage: 'observe' }, readiness: { rules: [{ rule_key: 'java-domain-stays-pure', state: 'ready' }] } } },
    '/api/decisions': { status: 200, body: { items: rows, next_cursor: null } },
    ['POST /api/projects/' + P + '/reviews']: () => {
      saves += 1;
      return saves === 2 ? hold.promise.then(() => ({ status: 503, body: { detail: 'store down' } })) : { status: 200, body: { updated: 1, skipped: [] } };
    }
  });
  const reads = () => calls.filter(c => c.method === 'GET' && new URL(c.url).pathname.endsWith('/api/projects/' + P)).length;
  await visit('#/try');
  await click('try-create'); await tick();
  await click('try-show'); await tick();
  await click('try-review'); await tick();
  out.readsBefore = reads();
  await click('try-label', { 'data-verdict': 'VERDICT-2', 'data-label': 'correct' }); await tick();
  click('try-label', { 'data-verdict': 'VERDICT-1', 'data-label': 'correct' });
  await tick();
  const pressing = click('try-readiness');
  await tick();
  hold.release();
  await pressing; await tick();
  out.step = Dash.tryState.step;
  out.busy = Dash.tryState.busy;
  out.readsAfterFailure = reads();
  out.cursorOn = Dash.tryState.observed[Dash.tryState.cursor].verdict_id;
  out.focused = document.activeElement && document.activeElement.id;
  out.failed = text(view());
  await click('try-label', { 'data-verdict': 'VERDICT-1', 'data-label': 'correct' }); await tick();
  await click('try-readiness'); await tick();
  out.stepAfterRelabel = Dash.tryState.step;
  out.readsAfterRelabel = reads();
""",
        tmp_path,
    )
    assert out["step"] == 3 and out["busy"] is False, "A label the service did not keep moved the reader on"
    assert out["readsAfterFailure"] == out["readsBefore"], "Readiness was read with a label missing"
    assert out["cursorOn"] == "VERDICT-1" and out["focused"] == "try-correct", "The keyboard is not back on the call to label again"
    failed = out["failed"]
    assert "1 of 2 labelled" in failed and "Was the rule right?" in failed
    assert "That did not work: the label for src/acme/domain/a.py was not saved (HTTP 503: store down). Mark it again." in failed
    assert out["stepAfterRelabel"] == 4 and out["readsAfterRelabel"] == out["readsBefore"] + 1


REPLAY = r"""
const P = 'Acme-Sandbox-0a1b2c3d';
const obs = (i, rule, target, extra) => row(i, Object.assign({ project_name: P, rule_key: rule, observed_rules: [rule], observed_rule: rule, target, observed_target: target }, extra || {}));
const observed = [
  obs(1, 'python-domain-stays-pure', 'src/acme/domain/order.py', { observed_reason: "Clean Architecture violation: Layering rule 'python-domain-stays-pure' refuses this write: A Python file under domain/ may not import infrastructure or a driver. 'src/acme/domain/order.py' imports 'boto3', which matches 'boto3'" }),
  obs(2, 'PROTECTED_PATH', 'cat', { agent: 'codex', tool_name: 'shell', action_type: 'COMMAND_EXEC', observed_target: '', observed_reason: "Command 'cat .env' reaches a protected path or credential store" })
];
const sent = { evaluate: [], promote: [] };
answer = api({
  'POST /api/sandbox': { status: 200, body: { project: P, calls_seeded: 12 } },
  ['/api/projects/' + P]: { status: 200, body: { project: P, config: { stage: 'observe', sandbox: true }, readiness: { rules: [
    { rule_key: 'python-domain-stays-pure', kind: 'layering', state: 'ready', would_refuse: 1, correct: 1, false_alarms: 0, unreviewed: 0 },
    { rule_key: 'PROTECTED_PATH', kind: 'gate', state: 'ready', would_refuse: 1, correct: 1, false_alarms: 0, unreviewed: 0 },
    { rule_key: 'dotnet-domain-stays-pure', kind: 'layering', state: 'quiet', would_refuse: 0, correct: 0, false_alarms: 0, unreviewed: 0 },
    { rule_key: 'LOOP', kind: 'gate', state: 'quiet', would_refuse: 0, correct: 0, false_alarms: 0, unreviewed: 0 }] } } },
  '/api/decisions': { status: 200, body: { items: observed, next_cursor: null } },
  ['POST /api/projects/' + P + '/reviews']: { status: 200, body: { updated: 1, skipped: [] } },
  ['POST /api/projects/' + P + '/promote']: (u, i, body) => { sent.promote.push(body.enforce); return { status: 200, body: { project: P, config: { stage: 'enforce', observe_rules: [] } } }; },
  '/api/decision': { status: 200, body: { decision: observed[0], session: null, rule: { id: 'python-domain-stays-pure', forbid_imports: ['boto3'] } } },
  '/rules': { status: 200, body: { rules: [{ id: 'dotnet-domain-stays-pure', when_path_matches: ['**/Domain/**/*.cs'], forbid_imports: ['**.Infrastructure.**', 'System.Data'] }] } },
  'POST /evaluate-tool-call': (u, i, body) => { sent.evaluate.push(body); return { status: 200, body: { status: 'BLOCKED_BOUNDARY_VIOLATION', reason: 'Refused.', project_stage: 'enforce' } }; }
});
async function toStepFour(keep) {
  await visit('#/try');
  await click('try-restart'); await tick();
  await click('try-create'); await tick();
  await click('try-show'); await tick();
  await click('try-review'); await tick();
  await click('try-label', { 'data-verdict': 'VERDICT-1', 'data-label': 'correct' });
  await click('try-label', { 'data-verdict': 'VERDICT-2', 'data-label': 'correct' });
  await tick();
  await click('try-readiness'); await tick();
  for (const key of ['python-domain-stays-pure', 'PROTECTED_PATH', 'dotnet-domain-stays-pure', 'LOOP']) {
    await click('try-toggle', { 'data-rule': key, checked: keep.indexOf(key) !== -1 });
  }
}
"""


def test_the_walkthrough_sends_a_gate_s_call_as_the_call_it_flagged(tmp_path: Path) -> None:
    """A protected path's command is sent again as that command, never rebuilt as a write.

    The replay once took whatever call was marked correct under a rule in
    force and wrote an import into a file named after its target. For the
    command `cat .env` that was a write to a file called `cat`, which the
    service approved, and the climax said the rule "may not be in force".
    """
    out = dash(
        REPLAY
        + r"""
  await toStepFour(['PROTECTED_PATH']);
  out.button = text(view());
  await click('try-promote'); await tick();
  await click('try-next'); await tick();
  out.send = text(view());
  await click('try-send'); await tick();
  out.sent = sent.evaluate.pop();
""",
        tmp_path,
    )
    assert "Promote with 1 rule" in out["button"]
    assert out["sent"]["action_type"] == "COMMAND_EXEC"
    assert out["sent"]["agent"] == "codex" and out["sent"]["tool_name"] == "shell", "The command goes again from the agent and tool that ran it"
    assert out["sent"]["arguments"] == {"command": "cat .env"}, "The command the gate flagged is the command sent again"
    assert "Terminal" in out["send"] and "cat .env" in out["send"] and "Earlier, in Observe" in out["send"]
    assert "the same command" in out["send"]


def test_the_walkthrough_builds_a_quiet_rule_s_write_from_the_rule_and_sends_nothing_a_gate_would_approve(tmp_path: Path) -> None:
    """With no flagged call under a rule in force, the call to send comes from the rule, or is not sent.

    A quiet layering rule says which files it watches and what they may not
    import, so the write is built from that. A gate that one call cannot trip
    (a loop, a budget) is never stood in for by a call it would approve, and
    Enforce with no rule in force is never offered.
    """
    out = dash(
        REPLAY
        + r"""
  await toStepFour(['dotnet-domain-stays-pure']);
  await click('try-promote'); await tick();
  await click('try-next'); await tick();
  out.quietSend = text(view());
  await click('try-send'); await tick();
  out.quietSent = sent.evaluate.pop();

  await toStepFour(['LOOP']);
  await click('try-promote'); await tick();
  await click('try-next'); await tick();
  out.gateOnly = text(view());
  const before = sent.evaluate.length;
  await click('try-finish'); await tick();
  out.gateDone = text(view());
  out.gateSent = sent.evaluate.length - before;

  await toStepFour([]);
  out.none = view();
  const promotes = sent.promote.length;
  await click('try-promote'); await tick();
  out.nonePromoted = sent.promote.length - promotes;
  out.noneError = text(view());
""",
        tmp_path,
    )
    sent = out["quietSent"]
    assert sent["tool_name"] == "Write" and sent["arguments"] == {"file_path": "src/Domain/Order.cs", "content": "using System.Data;\n"}
    assert "built from the rule itself" in out["quietSend"]
    assert "No single call can show these rules" in out["gateOnly"] and "LOOP" in out["gateOnly"]
    assert out["gateSent"] == 0, "Nothing is sent for a gate one call cannot show"
    assert "No call was sent" in out["gateDone"]
    assert re.search(r'id="try-primary"[^>]*data-action="try-promote"[^>]*disabled', out["none"]), "Promote with no rule is not offered"
    assert "Promote with 0 rules" in text_of(out["none"]) and "Check at least one rule to promote" in text_of(out["none"])
    assert out["nonePromoted"] == 0 and "Check at least one rule to promote" in out["noneError"]


def test_the_replay_sends_the_call_as_the_agent_and_tool_that_made_it(tmp_path: Path) -> None:
    """Step 5 sends a flagged call again from the agent and with the tool that made it.

    On the default run Claude Code's only flagged call is under the rule the
    false alarm makes noisy, so the call sent again is another agent's. It
    once went as Claude Code's Write whatever agent had made it, and the
    judged moment spent its words on the swap ("Agent Antigravity → Claude
    Code", "whichever agent sends the call") instead of showing the same call,
    now refused. Now only the stage differs, and every place that compares
    the two calls says so.
    """
    out = dash(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const layer = (i, rule, target, agent, tool, imp, desc) => row(i, { project_name: P, agent, tool_name: tool, rule_key: rule, observed_rules: [rule], observed_rule: rule,
    target, observed_target: target, observed_reason: "Clean Architecture violation: Layering rule '" + rule + "' refuses this write: " + desc + ". '" + target + "' imports '" + imp + "', which matches '" + imp + "'" });
  const observed = [
    layer(1, 'web-domain-stays-pure', 'src/web/domain/cart.ts', 'antigravity', 'write_to_file', 'axios', 'A TypeScript module under domain/ may not import a client or a framework'),
    layer(2, 'java-domain-stays-pure', 'src/main/java/com/acme/domain/Order.java', 'codex', 'apply_patch', 'javax.persistence.Entity', 'A Java class under domain/ may not reach persistence'),
    layer(3, 'python-domain-stays-pure', 'src/acme/domain/order.py', 'claude-code', 'Write', 'boto3', 'A Python file under domain/ may not import infrastructure or a driver')
  ];
  const labels = {};
  const readiness = () => ['web-domain-stays-pure', 'java-domain-stays-pure', 'python-domain-stays-pure'].map(key => {
    const mine = observed.filter(r => r.rule_key === key).map(r => labels[r.verdict_id]);
    const alarms = mine.filter(l => l === 'false_alarm').length;
    const correct = mine.filter(l => l === 'correct').length;
    return { rule_key: key, kind: 'layering', would_refuse: mine.length, correct, false_alarms: alarms, unreviewed: mine.length - alarms - correct,
      state: alarms ? 'noisy' : correct === mine.length ? 'ready' : 'needs_review' };
  });
  const sent = [];
  answer = api({
    'POST /api/sandbox': { status: 200, body: { project: P, calls_seeded: 12 } },
    ['/api/projects/' + P]: () => ({ status: 200, body: { project: P, config: { stage: 'observe', sandbox: true }, readiness: { rules: readiness() } } }),
    '/api/decisions': { status: 200, body: { items: observed, next_cursor: null } },
    ['POST /api/projects/' + P + '/reviews']: (u, i, body) => { labels[body.items[0].verdict_id] = body.items[0].label; return { status: 200, body: { updated: 1, skipped: [] } }; },
    ['POST /api/projects/' + P + '/promote']: { status: 200, body: { project: P, config: { stage: 'enforce', observe_rules: [] } } },
    '/api/decision': { status: 200, body: { decision: null, session: null, rule: null } },
    '/rules': { status: 200, body: { rules: [] } },
    'POST /evaluate-tool-call': (u, i, body) => { sent.push(body); return { status: 200, body: { status: 'BLOCKED_BOUNDARY_VIOLATION', reason: 'Refused.', project_stage: 'enforce' } }; }
  });
  async function run(alarmFrom, sameInstant) {
    Object.keys(labels).forEach(k => delete labels[k]);
    if (sameInstant) observed.forEach(r => { r.timestamp = NOW; });
    await visit('#/try');
    await click('try-restart'); await tick();
    await click('try-create'); await tick();
    await click('try-show'); await tick();
    await click('try-review'); await tick();
    for (const r of Dash.tryState.observed.slice()) {
      await click('try-label', { 'data-verdict': r.verdict_id, 'data-label': r.agent === alarmFrom ? 'false_alarm' : 'correct' });
    }
    await tick();
    await click('try-readiness'); await tick();
    await click('try-promote'); await tick();
    await click('try-next'); await tick();
    const send = text(view());
    await click('try-send'); await tick();
    return { send, climax: text(view()), sent: sent[sent.length - 1] };
  }
  out.web = await run('claude-code');
  out.java = await run('antigravity');
  out.tie = await run('claude-code', true);
""",
        tmp_path,
    )
    # Claude Code's rule is noisy: the newest call in force is Antigravity's, and it goes as Antigravity's.
    web = out["web"]
    assert web["sent"]["agent"] == "antigravity" and web["sent"]["tool_name"] == "write_to_file"
    assert web["sent"]["arguments"] == {"file_path": "src/web/domain/cart.ts", "content": "import axios from 'axios';\n"}
    send = web["send"]
    assert "Send the same call again" in send
    assert "Same agent, same file, same import, and your sandbox is now in Enforce. This time the rule in force refuses it." in send
    assert "Now, in Enforce · as Antigravity's hook sends it" in send and "write_to_file · Antigravity · hook" in send
    assert "Agent the same" in send and "File the same" in send and "Import the same" in send and "Stage Observe → Enforce" in send
    assert "The write that ran in Observe is refused in Enforce: same agent, same file, same import, a new stage." in web["climax"]
    for swap in ("Claude Code sends", "from Claude Code", "whichever agent", "→ Claude Code"):
        assert swap not in send and swap not in web["climax"], f"Step 5 still tells of a swap of agents: {swap!r}"
    # With Antigravity's call a false alarm, the newest call in force is Codex's, sent with Codex's own tool.
    java = out["java"]
    assert java["sent"]["agent"] == "codex" and java["sent"]["tool_name"] == "apply_patch"
    assert java["sent"]["arguments"] == {"file_path": "src/main/java/com/acme/domain/Order.java", "content": "import javax.persistence.Entity;\n"}
    assert "apply_patch · Codex · hook" in java["send"]
    # A coarse clock stamps the sandbox's calls in one instant: the later lane was sent later, so every run replays the same call.
    assert out["tie"]["sent"]["arguments"]["file_path"] == "src/web/domain/cart.ts", "Calls stamped together are not told apart the way they were sent"
    assert out["tie"]["sent"]["agent"] == "antigravity"


def test_the_rule_as_written_stands_beside_the_call_it_judged_on_steps_three_and_five(tmp_path: Path) -> None:
    """The card on top of the stack, and the call about to be sent, show the rule as GET /rules has it.

    Its path patterns, with the one that covers the call's path marked, are
    how a reader spots the false alarm (a test module under tests/domain/ is
    not the domain), and its forbidden packages, with the one the import
    falls under marked, say what the refusal will be about. When the rules
    cannot be read, nothing stands in for them.
    """
    scenario = r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const layer = (i, target, agent, imp) => row(i, { project_name: P, agent, rule_key: 'python-domain-stays-pure', observed_rules: ['python-domain-stays-pure'], observed_rule: 'python-domain-stays-pure',
    target, observed_target: target, observed_reason: "Clean Architecture violation: Layering rule 'python-domain-stays-pure' refuses this write: A Python file under domain/ may not import infrastructure or a driver. '" + target + "' imports '" + imp + "', which matches '" + imp + "'" });
  const observed = [layer(1, 'tests/domain/test_order_totals.py', 'codex', 'fastapi.testclient'), layer(2, 'src/acme/domain/order.py', 'claude-code', 'boto3')];
  const RULE = { id: 'python-domain-stays-pure', description: 'A Python file under domain/ may not import infrastructure or a driver',
    when_path_matches: ['**/domain/**/*.py', '**/domain/**/*.pyi'], forbid_imports: ['boto3', 'botocore', 'requests', 'httpx', 'sqlalchemy', 'fastapi', 'flask', 'django'] };
  answer = api({
    'POST /api/sandbox': { status: 200, body: { project: P, calls_seeded: 12 } },
    ['/api/projects/' + P]: { status: 200, body: { project: P, config: { stage: 'observe', sandbox: true }, readiness: { rules: [
      { rule_key: 'python-domain-stays-pure', kind: 'layering', state: 'ready', would_refuse: 2, correct: 2, false_alarms: 0, unreviewed: 0 }] } } },
    '/api/decisions': { status: 200, body: { items: observed, next_cursor: null } },
    ['POST /api/projects/' + P + '/reviews']: { status: 200, body: { updated: 1, skipped: [] } },
    ['POST /api/projects/' + P + '/promote']: { status: 200, body: { project: P, config: { stage: 'enforce', observe_rules: [] } } },
    '/api/decision': { status: 200, body: { decision: null, session: null, rule: RULE } },
    '/rules': READABLE ? { status: 200, body: { rules: [RULE] } } : { status: 503, body: { detail: 'down' } }
  });
  await visit('#/try');
  await click('try-create'); await tick();
  await click('try-show'); await tick();
  await click('try-review'); await tick();
  out.first = view();
  await click('try-label', { 'data-verdict': 'VERDICT-2', 'data-label': 'correct' }); await tick();
  out.second = view();
  await click('try-label', { 'data-verdict': 'VERDICT-1', 'data-label': 'correct' }); await tick();
  await click('try-readiness'); await tick();
  await click('try-promote'); await tick();
  await click('try-next'); await tick();
  out.send = view();
"""
    out = walk("const READABLE = true;\n" + scenario, tmp_path)
    hit = r'<code class="tf-try-glob" data-hit>\s*<svg[^>]*>.*?</svg>\s*'
    first, second, send = out["first"], out["second"], out["send"]
    # The oldest call is on top: Claude Code's order.py, whose import falls under boto3.
    assert "What the rule watches" in text_of(first)
    assert re.search(hit + re.escape("**/domain/**/*.py") + "<", first, re.S), "The pattern that covers the path is not marked"
    assert '<code class="tf-try-glob">**/domain/**/*.pyi<' in first, "A pattern that does not cover the path is marked"
    assert re.search(hit + "boto3<", first, re.S) and "and 2 more" in text_of(first)
    # The next card is the test module: the same pattern covers it, which is the false alarm to spot.
    assert "tests/domain/test_order_totals.py" in text_of(second)
    assert re.search(hit + re.escape("**/domain/**/*.py") + "<", second, re.S) and re.search(hit + "fastapi<", second, re.S)
    # Step 5: the rule now in force, in its own words, beside the call about to be sent: the
    # newest call marked correct, the test module, whose import falls under fastapi.
    words = text_of(send)
    assert "Now in force python-domain-stays-pure" in words and "A Python file under domain/ may not import infrastructure or a driver." in words
    assert "tests/domain/test_order_totals.py" in words
    assert re.search(hit + "fastapi<", send, re.S), "The package the import falls under is not marked"

    unreadable = walk("const READABLE = false;\n" + scenario, tmp_path)
    for name in ("first", "send"):
        assert "tf-try-rulebox" not in unreadable[name], "A rule that could not be read was shown"


# A phone of 375px: every `max-width` query up to that width matches, the page
# is scrolled down a long step, and each scroll the page asks for is recorded.
PHONE = r"""
globalThis.matchMedia = q => {
  const m = /max-width:\s*(\d+)px/.exec(String(q));
  return { matches: !!m && 375 <= Number(m[1]), media: String(q), addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} };
};
globalThis.scrollY = 1087;
const scrolled = [];
globalThis.scrollTo = (x, y) => { scrolled.push(y); };
"""

PHONE_WALK = r"""
  await visit('#/try');
  await click('try-restart'); await tick();
  await click('try-create'); await tick();
  await click('try-show'); await tick();
  await click('try-review'); await tick();
  const stack = view();
  const card = (/<article class="tf-try-card"[\s\S]*?<\/article>/.exec(stack) || [''])[0];
  const bar = stack.slice(stack.indexOf('class="tf-try-actions"'));
  out.onCard = card.indexOf('id="try-correct"') !== -1 && card.indexOf('id="try-false"') !== -1;
  out.inBar = bar.indexOf('id="try-correct"') !== -1 && bar.indexOf('id="try-false"') !== -1 && bar.indexOf('class="tf-try-thumb"') !== -1;
  await click('try-label', { 'data-verdict': 'VERDICT-1', 'data-label': 'correct' });
  await click('try-label', { 'data-verdict': 'VERDICT-2', 'data-label': 'correct' });
  await tick();
  await click('try-readiness'); await tick();
  // The reader scrolls down the list of rules, so the stage's top is far above.
  el('try-stage').getBoundingClientRect = () => ({ top: -900, height: 2400 });
  el('try-rail').getBoundingClientRect = () => ({ top: 56, height: 52 });
  const before = typeof scrolled === 'undefined' ? 0 : scrolled.length;
  await click('try-promote'); await tick();
  out.scrolls = typeof scrolled === 'undefined' ? null : scrolled.slice(before);
  out.promoted = text(view());
"""


def test_on_a_phone_the_labels_sit_under_the_thumb_and_a_promotion_plays_in_view(tmp_path: Path) -> None:
    """Below 640px the two labels move to the sticky bar; on a desk they stay on the card.

    And a promotion made from the bar at the foot of a long list of rules takes
    the reader back up to the stage's top, where the switch slides and the
    chip turns from Observe to Enforce: at 375px they once played 500 to 800px
    above the screen, and the reader saw none of it.
    """
    phone = dash(REPLAY + PHONE_WALK, tmp_path, before=PHONE)
    desk = dash(REPLAY + PHONE_WALK, tmp_path)
    assert phone["inBar"] is True and phone["onCard"] is False, "On a phone the labels are not in the thumb bar"
    assert desk["onCard"] is True and desk["inBar"] is False, "On a desk the labels left the card"
    assert "Now in Enforce" in phone["promoted"]
    # The stage's top, less the header and the sticky rail: -900 + 1087 - 56 - 52 - 12.
    assert phone["scrolls"] == [67], "The promotion did not bring the stage back into view"


def test_the_flagged_cards_name_the_whole_command_and_an_import_the_reason_cut(tmp_path: Path) -> None:
    """A card never shows a cut import as the thing a call imported, nor a program as the command.

    The service keeps 240 characters of a reason, which ends a Java reason
    inside the import ("javax.persistence.Enti"); the rule's own definition
    names the package it falls under. A command's target is only its program
    ("cat"); the whole command is in its reason.
    """
    cut = (
        "Observe stage, not enforced. This call would have been refused: Clean Architecture violation: Layering rule "
        "'java-domain-stays-pure' refuses this write: A Java class under domain/ may not reach persistence, HTTP or the "
        "container. 'src/main/java/com/acme/domain/Order.java' imports 'javax.persistence.Enti"
    )
    out = dash(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const CUT = """ + json.dumps(cut) + r""";
  const observed = [
    row(1, { project_name: P, agent: 'codex', tool_name: 'apply_patch', rule_key: 'java-domain-stays-pure', observed_rules: ['java-domain-stays-pure'],
      target: 'src/main/java/com/acme/domain/Order.java', observed_target: 'src/main/java/com/acme/domain/Order.java', observed_reason: CUT.replace(/^Observe stage, not enforced\. This call would have been refused: /, ''), reason: CUT.slice(0, 240) }),
    row(2, { project_name: P, agent: 'codex', tool_name: 'shell', action_type: 'COMMAND_EXEC', rule_key: 'PROTECTED_PATH', observed_rules: ['PROTECTED_PATH'],
      target: 'cat', observed_target: '', observed_reason: "Command 'cat .env' reaches a protected path or credential store" })
  ];
  answer = api({
    'POST /api/sandbox': { status: 200, body: { project: P, calls_seeded: 12 } },
    ['/api/projects/' + P]: { status: 200, body: { project: P, config: { stage: 'observe' }, readiness: { rules: [] } } },
    '/api/decisions': { status: 200, body: { items: observed, next_cursor: null } },
    '/rules': { status: 200, body: { rules: [{ id: 'java-domain-stays-pure', when_path_matches: ['**/domain/**/*.java'], forbid_imports: ['java.sql', 'javax.persistence', '**.infrastructure.**'] }] } }
  });
  await visit('#/try');
  await click('try-create'); await tick();
  await click('try-show'); await tick();
  out.cards = text(view());
  await click('try-review'); await tick();
  out.stack = text(view());
""",
        tmp_path,
    )
    cards = out["cards"]
    assert "It imports from javax.persistence , a package the rule forbids." in cards
    assert "javax.persistence.Enti" not in cards, "A cut import is shown as if it were the import"
    assert "Command cat .env" in cards, "The command row names the program alone"
    assert "It reaches a protected path or credential store." in cards
    assert "cat .env" in out["stack"]


def test_a_sandbox_with_nothing_flagged_says_so_and_offers_a_second_read(tmp_path: Path) -> None:
    """Step 2 with no flagged call is a designed state, not a blank stage.

    A ledger can lag a moment behind a new sandbox, so the read that found
    nothing is offered again, and the calls that did arrive stay on screen.
    """
    out = dash(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  let reads = 0;
  answer = api({
    'POST /api/sandbox': { status: 200, body: { project: P, calls_seeded: 12 } },
    ['/api/projects/' + P]: { status: 200, body: { project: P, config: { stage: 'observe' }, readiness: { rules: [] } } },
    '/api/decisions': u => {
      if (u.searchParams.get('kind') === 'observed') { reads += 1; return { status: 200, body: { items: [], next_cursor: null } }; }
      return { status: 200, body: { items: [row(1, { project_name: P, observed_rules: [], observed_rule: '', rule_key: 'NONE', target: 'README.md', observed_target: '', tool_name: 'Read', action_type: 'FILE_READ' })], next_cursor: null } };
    }
  });
  await visit('#/try');
  await click('try-create'); await tick();
  await click('try-show'); await tick();
  out.empty = view();
  await click('try-show'); await tick();
  out.reads = reads;
""",
        tmp_path,
    )
    words = text_of(out["empty"])
    assert "No call was flagged" in words and 'class="tf-empty"' in out["empty"]
    assert "Read the list again" in words and "README.md" in words, "The calls that arrived stay on the empty step"
    assert out["reads"] == 2


def text_of(markup: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup))


def test_a_resumed_sandbox_that_was_promoted_reads_as_enforce_on_every_step(tmp_path: Path) -> None:
    """A visitor who comes back to a sandbox they promoted is shown the stage the service keeps.

    The walkthrough once assumed Observe until it promoted the project itself,
    so a resumed sandbox already in Enforce read "the project is in Observe",
    offered to promote it again, and counted a call judged in Enforce among
    the calls judged in Observe.
    """
    out = walk(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const obs = (i, rule, target, extra) => row(i, Object.assign({ project_name: P, rule_key: rule, observed_rules: [rule], observed_rule: rule, target, observed_target: target, review: 'correct' }, extra || {}));
  const observed = [
    obs(2, 'python-domain-stays-pure', 'src/acme/domain/order.py'),
    obs(3, 'PROTECTED_PATH', 'cat', { agent: 'codex', tool_name: 'shell', action_type: 'COMMAND_EXEC', observed_target: '', observed_reason: "Command 'cat .env' reaches a protected path or credential store" })
  ];
  const refusedLater = row(1, { project_name: P, status: 'BLOCKED_BOUNDARY_VIOLATION', stage: 'enforce', observed_rules: [], observed_rule: '', rule_key: 'python-domain-stays-pure', reason: 'The domain may not import boto3.' });
  const sent = {};
  store['threefold-try'] = JSON.stringify({ project: P, at: Date.now() });
  answer = api({
    ['/api/projects/' + P]: { status: 200, body: { project: P,
      config: { stage: 'enforce', observe_rules: ['PROTECTED_PATH', 'LOOP'], sandbox: true,
        history: [{ at: NOW, action: 'promote', by: 'anonymous', enforce: ['python-domain-stays-pure'], observe: ['PROTECTED_PATH', 'LOOP'] }] },
      readiness: { summary: { stage: 'enforce' }, rules: [
        { rule_key: 'python-domain-stays-pure', kind: 'layering', state: 'ready', mode_now: 'enforce', would_refuse: 1, correct: 1, false_alarms: 0, unreviewed: 0 },
        { rule_key: 'PROTECTED_PATH', kind: 'gate', state: 'ready', mode_now: 'observe', would_refuse: 1, correct: 1, false_alarms: 0, unreviewed: 0 },
        { rule_key: 'LOOP', kind: 'gate', state: 'quiet', mode_now: 'observe', would_refuse: 0, correct: 0, false_alarms: 0, unreviewed: 0 }] } } },
    '/api/decisions': u => ({ status: 200, body: { items: u.searchParams.get('kind') === 'observed' ? observed : [refusedLater].concat(observed), next_cursor: null } }),
    '/api/decision': { status: 200, body: { decision: observed[0], session: null, rule: { id: 'python-domain-stays-pure', forbid_imports: ['boto3'] } } },
    'POST /evaluate-tool-call': (u, i, body) => { sent.evaluate = body; return { status: 200, body: { status: 'BLOCKED_BOUNDARY_VIOLATION', reason: 'The domain may not import boto3.', project_stage: 'enforce' } }; }
  });
  await visit('#/try');
  out.offer = text(view());
  await click('try-resume'); await tick();
  out.step1 = text(view());
  await click('try-show'); await tick();
  await click('try-review'); await tick();
  await click('try-readiness'); await tick();
  out.step4 = text(view());
  out.offersPromote = view().indexOf('data-action="try-promote"') !== -1;
  await click('try-next'); await tick();
  out.step5 = text(view());
  await click('try-send'); await tick();
  await click('try-finish'); await tick();
  out.done = text(view());
  out.sent = sent;
""",
        tmp_path,
    )
    assert "Continue with it" in out["offer"]
    assert "the project is in Enforce, promoted earlier" in out["step1"]
    assert "the project is in Observe" not in out["step1"], "Step 1 contradicts the stage the service keeps"
    assert "Already in Enforce" in out["step4"] and "1 rule refuses the calls that break it" in out["step4"]
    assert "PROTECTED_PATH and LOOP keep observing" in out["step4"]
    assert out["offersPromote"] is False, "A project already in Enforce is not offered the promotion again"
    assert "src/acme/domain/order.py" in out["step5"], "The call to send is planned for a promotion made on an earlier visit"
    assert out["sent"]["evaluate"]["arguments"] == {"file_path": "src/acme/domain/order.py", "content": "import boto3\n"}
    assert "3 calls judged 2 in Observe and 1 in Enforce" in out["done"]
    assert "calls judged in Observe" not in out["done"], "A call judged in Enforce is not counted as judged in Observe"
    # Each stage's count on its own: the refusal in Enforce is not one of the calls Observe let run.
    assert "In Observe, 2 calls would have been refused and ran; in Enforce, 1 call was refused." in out["done"]
    assert "would have been refused, and 1 was" not in out["done"]


def test_a_resumed_sandbox_says_it_is_reading_until_the_reads_answer(tmp_path: Path) -> None:
    """While a sandbox made earlier is read back, the screen says so and claims nothing about it.

    The resume once showed "Step 1 of 5 · done", "Your sandbox is ready",
    "Every call was judged and recorded" and lanes "Sending its calls…"
    before a single call had been read, and offered to continue "with the
    labels you gave it" to a visitor who had given none.
    """
    out = dash(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const hold = held();
  store['threefold-try'] = JSON.stringify({ project: P, at: Date.now() });
  answer = api({
    ['/api/projects/' + P]: { status: 200, body: { project: P, config: { stage: 'observe', sandbox: true }, readiness: { rules: [] } } },
    '/api/decisions': () => hold.promise.then(() => ({ status: 200, body: { items: [row(1, { project_name: P })], next_cursor: null } }))
  });
  await visit('#/try');
  out.offer = text(view());
  const resuming = click('try-resume');
  await tick();
  out.reading = text(view());
  hold.release();
  await resuming; await tick();
  out.read = text(view());
""",
        tmp_path,
    )
    assert "You started one earlier: Acme-Sandbox-0a1b2c3d" in out["offer"] and "Continue with it where you left off" in out["offer"]
    assert "with the labels you gave it" not in out["offer"]
    reading = out["reading"]
    assert "Opening your sandbox" in reading and "Reading its calls…" in reading and "Reading your sandbox…" in reading
    for claim in ("· done", "Your sandbox is ready", "judged and recorded", "Sending its calls"):
        assert claim not in reading, f"While reading, the screen claims {claim!r}"
    assert "Step 1 of 5 · done" in out["read"] and "Every call was judged and recorded" in out["read"]


def test_the_walkthrough_words_the_service_s_counts_and_its_sources_plainly(tmp_path: Path) -> None:
    """The service's "1 false alarm(s)" reads "1 false alarm", and a caption names its source in a few words.

    A sentence the model did not write is said so beneath the caption, in
    sentence case, rather than in a caption of shouting capitals; and the
    rail's note for a refusal is drawn in the refusal's colour.
    """
    out = dash(
        REPLAY.replace(
            "{ rule_key: 'PROTECTED_PATH', kind: 'gate', state: 'ready', would_refuse: 1, correct: 1, false_alarms: 0, unreviewed: 0 }",
            "{ rule_key: 'PROTECTED_PATH', kind: 'gate', state: 'noisy', would_refuse: 3, correct: 2, false_alarms: 1, unreviewed: 0, recommendation: '1 false alarm(s): keep it observing, or refine the rule, before enforcing it.' },"
            + " { rule_key: 'LOOP_TWO', kind: 'gate', state: 'needs_review', would_refuse: 2, correct: 0, false_alarms: 0, unreviewed: 2, recommendation: '2 flagged call(s) not reviewed: mark each correct or false alarm before deciding.' }",
        ).replace(
            "body: { status: 'BLOCKED_BOUNDARY_VIOLATION', reason: 'Refused.', project_stage: 'enforce' }",
            "body: { status: 'BLOCKED_BOUNDARY_VIOLATION', reason: 'Refused.', project_stage: 'enforce', bedrock_explanation: 'The domain reached for an AWS client.', explanation_source: 'deterministic_fallback' }",
        )
        + r"""
  await toStepFour(['python-domain-stays-pure']);
  out.step4 = text(view());
  await click('try-promote'); await tick();
  await click('try-next'); await tick();
  await click('try-send'); await tick();
  out.climax = view();
""",
        tmp_path,
    )
    assert "1 false alarm: keep it observing, or refine the rule" in out["step4"]
    assert "2 flagged calls not reviewed" in out["step4"] and "(s)" not in out["step4"]
    climax = out["climax"]
    assert re.search(r'class="tf-try-quote-src">\s*<svg[^>]*>.*?</svg>\s*Deterministic explanation</figcaption>', climax, re.S)
    assert "Amazon Bedrock was asked and did not answer, so this sentence is the service's own." in text_of(climax).replace("&#039;", "'")
    assert 'data-tone="refused">Refused<' in climax, "The rail's note for the refusal is not in the refusal's colour"


def test_a_resumed_sandbox_s_rail_counts_only_the_lists_it_has_read(tmp_path: Path) -> None:
    """The rail's note for step 2 waits for the list of flagged calls it counts.

    A resumed sandbox reads its readiness on step 1, before that list, and the
    rail once counted the empty list as "0 calls flagged" beside a project bar
    that said, on the same screen, that five calls would refuse.
    """
    out = dash(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const obs = (i, rule, target, extra) => row(i, Object.assign({ project_name: P, rule_key: rule, observed_rules: [rule], observed_rule: rule, target, observed_target: target }, extra || {}));
  const observed = [
    obs(2, 'python-domain-stays-pure', 'src/acme/domain/order.py'),
    obs(3, 'PROTECTED_PATH', 'cat', { agent: 'codex', tool_name: 'shell', action_type: 'COMMAND_EXEC', observed_target: '', observed_reason: "Command 'cat .env' reaches a protected path or credential store" })
  ];
  const approved = row(4, { project_name: P, agent: 'antigravity', observed_rules: [], observed_rule: '', rule_key: 'NONE', target: 'README.md', observed_target: '' });
  store['threefold-try'] = JSON.stringify({ project: P, at: Date.now() });
  answer = api({
    ['/api/projects/' + P]: { status: 200, body: { project: P, config: { stage: 'observe', sandbox: true }, readiness: { rules: [
      { rule_key: 'python-domain-stays-pure', kind: 'layering', state: 'needs_review', would_refuse: 1, correct: 0, false_alarms: 0, unreviewed: 1 },
      { rule_key: 'PROTECTED_PATH', kind: 'gate', state: 'needs_review', would_refuse: 1, correct: 0, false_alarms: 0, unreviewed: 1 }] } } },
    '/api/decisions': u => ({ status: 200, body: { items: u.searchParams.get('kind') === 'observed' ? observed : observed.concat([approved]), next_cursor: null } })
  });
  function notes() {
    return view().split('<li class="tf-try-step"').slice(1).map(li => { const m = /class="tf-try-step-note"[^>]*>([^<]*)</.exec(li); return m ? m[1] : ''; });
  }
  await visit('#/try');
  await click('try-resume'); await tick();
  out.resumed = notes();
  out.bar = text(view());
  await click('try-show'); await tick();
  out.read = notes();
""",
        tmp_path,
    )
    assert out["resumed"][0] == "3 calls · 3 agents"
    assert out["resumed"][1] == "", "The rail counted a list it had not read yet"
    assert "2 would refuse" in out["bar"], "The project bar on the same screen reads the calls it has"
    assert out["read"][1] == "2 calls flagged", "Once the list is read, the rail counts it"


def test_the_walkthrough_escapes_every_value_the_service_sends(tmp_path: Path) -> None:
    """Every string the walkthrough shows from a response is escaped, on every step.

    The rows, the readiness, the promotion's answer, the rule the replay is
    built from and the refusal all carry markup that would run if it were
    written as markup; the screens are read at each step, the climax and the
    completion included.
    """
    out = dash(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const evilRow = i => row(i, { project_name: P, tool_name: EVIL, target: EVIL, observed_target: EVIL, reason: EVIL, observed_reason: EVIL,
    rule_key: EVIL, observed_rules: [EVIL], observed_rule: EVIL, agent: EVIL, session_id: EVIL });
  const rows = [evilRow(1), evilRow(2)];
  answer = api({
    'POST /api/sandbox': { status: 200, body: { project: P, calls_seeded: 2 } },
    ['/api/projects/' + P]: { status: 200, body: { project: P, config: { stage: 'observe' }, readiness: { rules: [
      { rule_key: EVIL, kind: EVIL, state: EVIL, recommendation: EVIL, would_refuse: 2, correct: 1, false_alarms: 1, unreviewed: 0 }
    ] } } },
    '/api/decisions': { status: 200, body: { items: rows, next_cursor: null } },
    ['POST /api/projects/' + P + '/reviews']: { status: 200, body: { updated: 1, skipped: [] } },
    ['POST /api/projects/' + P + '/promote']: { status: 200, body: { project: P, config: { stage: 'enforce', observe_rules: [EVIL] } } },
    '/api/decision': { status: 200, body: { decision: rows[0], session: null, rule: { id: EVIL, forbid_imports: [EVIL] } } },
    '/rules': { status: 200, body: { rules: [{ id: EVIL, description: EVIL, when_path_matches: [EVIL, '**/' + EVIL], forbid_imports: [EVIL] }] } },
    'POST /evaluate-tool-call': { status: 200, body: { status: EVIL, reason: EVIL, bedrock_explanation: EVIL, explanation_source: 'bedrock', project_stage: EVIL,
      suggested_fix: { kind: EVIL, summary: EVIL, steps: [EVIL], writes: [{ path: EVIL, content: EVIL, new_file: true }], validated: true, checks: [{ gate: EVIL, path: EVIL, passed: true }] } } }
  });
  out.screens = {};
  await visit('#/try');
  await click('try-create'); await tick();
  out.screens.arrived = view();
  await click('try-show'); await tick();
  out.screens.cards = view();
  await click('try-review'); await tick();
  out.screens.stack = view();
  await click('try-label', { 'data-verdict': 'VERDICT-1', 'data-label': 'correct' }); await tick();
  await click('try-label', { 'data-verdict': 'VERDICT-2', 'data-label': 'false_alarm' }); await tick();
  await click('try-readiness'); await tick();
  Dash.tryState.selected = new Set([EVIL]);
  await click('try-promote'); await tick();
  out.screens.promoted = view();
  await click('try-next'); await tick();
  out.screens.send = view();
  await click('try-send'); await tick();
  out.screens.climax = view();
  out.sent = calls.filter(c => c.url.indexOf('/evaluate-tool-call') !== -1).pop().body;
  await click('try-finish'); await tick();
  out.screens.done = view();
""",
        tmp_path,
    )
    for name, markup in out["screens"].items():
        assert "<img" not in markup and "<svg onload" not in markup, f"Service data reached the {name} screen as markup"
        assert "&lt;img" in markup, f"The hostile value was not shown on the {name} screen, so this proves nothing there"
    for name in ("stack", "send"):
        assert "tf-try-rulebox" in out["screens"][name], f"The rule as written was not shown on the {name} screen, so this proves nothing about it"
    assert out["sent"]["session_id"].startswith("try-"), "The replay is still a hook's call in a session the stage decides"


def test_the_walkthrough_moves_only_for_a_reader_who_has_not_asked_for_less() -> None:
    """Every animation the walkthrough declares sits under prefers-reduced-motion: no-preference.

    Its default styles are the final state, so a reader who asked for less
    motion sees the same screens, still; the markup never waits on a timer.
    """
    body = page_source("dashboard.html")
    css = body.split("const TRY_CSS = `", 1)[1].split("`;", 1)[0]
    motion = css.split("@media (prefers-reduced-motion: no-preference) {", 1)
    assert len(motion) == 2, "The walkthrough's motion is not gated on the reader's preference"
    outside = motion[0] + motion[1].split(chr(10) + "}" + chr(10), 1)[1]
    assert not re.search(r"(^|[;{\s])animation(-name)?\s*:", outside), "An animation runs whatever the reader asked for"
    assert "infinite" not in css, "Nothing in the walkthrough loops"
    view = body.split("function viewTry(ctx)", 1)[1].split("// ------------------------------------------------------------- proof", 1)[0]
    assert "setTimeout" not in view and "setInterval" not in view, "A step's content never waits on a timer"


def test_the_walkthrough_says_where_it_runs_when_the_stack_is_private(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = api({
    '/api/auth/whoami': { status: 200, body: { authenticated: false, via: null, expires_at: null, reads_public: false, sandbox_writes: false } },
    'POST /api/sandbox': { status: 403, body: { detail: 'Forbidden' } }
  });
  Threefold.whoami(true);
  await visit('#/try');
  out.private = text(view());
  out.privateRail = view().indexOf('tf-try-rail') !== -1;
  answer = api({ 'POST /api/sandbox': { status: 403, body: { detail: 'Forbidden' } } });
  Threefold.whoami(true);
  await visit('#/overview');
  await visit('#/try');
  await click('try-create'); await tick();
  out.refused = text(view());
""",
        tmp_path,
    )
    assert "The walkthrough runs on the public demo" in out["private"] and "Connect a repository" in out["private"]
    assert out["privateRail"] is False, "A private stack draws the five steps with the first current, though none can be taken"
    assert "does not let visitors make a sandbox" in out["refused"]


def _ttl_days(module: str, name: str) -> int:
    """The expiry a module compiles in, read from the source rather than restated."""
    source = (ROOT / "src" / "threefold" / module).read_text(encoding="utf-8")
    match = re.search(rf"^{name} = (.+)$", source, re.M)
    assert match, f"{module} defines no {name}"
    return round(eval(match.group(1), {"__builtins__": {}}) / 86400)  # noqa: S307 — an arithmetic literal from our own source


def test_the_walkthrough_says_what_expires_in_a_day_and_what_stays_thirty(tmp_path: Path) -> None:
    """Only the sandbox's stage configuration carries the 24-hour expiry.

    Its calls and labels are ordinary ledger rows, which keep the 30-day
    session expiry, so a page telling an anonymous visitor that the sandbox
    "deletes itself within a day" promises a deletion that does not happen.
    """
    assert _ttl_days("application/projects.py", "SANDBOX_TTL_SECONDS") == 1
    assert _ttl_days("infrastructure/dynamo_repo.py", "SESSION_TTL_SECONDS") == 30

    out = dash(
        r"""
  answer = contract();
  await visit('#/try');
  out.step1 = text(view());
""",
        tmp_path,
    )
    assert "deletes itself within a day" not in out["step1"], "Nothing deletes the calls it recorded"
    assert "Its stage expires within a day" in out["step1"]
    assert "stay in the public call lists for 30 days" in out["step1"]


# ------------------------------------------------------------------- charts


def test_the_charts_are_accessible_and_every_mark_opens_its_rows(tmp_path: Path) -> None:
    out = dash(
        r"""
  const T = Threefold;
  const series = [{ day: DAY(1), approved: 5, observed: 2, refused: 0 }, { day: DAY(0), approved: 3, observed: 0, refused: 1 }];
  out.stack = T.stackedBars(series, { title: 'Calls per day', desc: 'Stacked', keys: [
    { key: 'refused', label: 'Refused', color: '#e11d48' }, { key: 'observed', label: 'Would refuse', color: '#d97706' }, { key: 'approved', label: 'Approved', color: '#059669' }
  ], href: (r, k) => '#/calls?kind=' + k + '&day=' + r.day }).toString();
  out.bars = T.hbars([{ label: 'LOOP', href: '#/calls?rule=LOOP', segments: [{ value: 3, label: 'refused', color: '#e11d48', href: '#/calls?rule=LOOP&kind=refused' }] }]).toString();
  out.ring = T.donut([{ label: 'Refused', value: 2, color: '#e11d48', href: '#/calls?kind=refused' }, { label: 'Approved', value: 8, color: '#059669', href: '#/calls?kind=approved' }]).toString();
  out.spark = T.sparkline([1, 4, 2], { label: 'Refused per day' }).toString();
""",
        tmp_path,
    )
    stack = out["stack"]
    assert 'role="img"' in stack and "<title" in stack and "<desc" in stack
    assert 'class="sr-only"' in stack and "<caption>Calls per day</caption>" in stack, "A table stands in for the chart"
    assert 'class="tf-legend"' in stack, "More than one series always carries a legend"
    assert stack.count("<a href=") == 4, "One link per drawn segment: 2 + 2 non-zero values"
    assert "open these calls" in stack
    assert out["bars"].count("<a href=") == 2 and "role=\"img\"" in out["bars"]
    assert out["ring"].count("<a href=") == 4, "Each part links from the ring and from its legend"
    assert 'aria-label="Refused per day: 1, 4, 2"' in out["spark"]


# ----------------------------------------------------------- existing pages


def test_the_console_forwards_to_the_overview_and_keeps_a_link_without_script() -> None:
    body = page_source("console.html")
    assert '<meta http-equiv="refresh" content="0; url=dashboard.html#/overview" />' in body
    assert 'href="dashboard.html#/overview"' in body
    assert "location.replace(DESTINATION)" in body and "PAGE_BASE + 'dashboard.html#/overview'" in body


def test_the_demo_page_opens_the_dashboard_and_the_walkthrough_and_keeps_its_scenarios() -> None:
    body = page_source("index.html")
    assert 'id="hero-try" href="dashboard.html#/try"' in body
    assert 'id="hero-dashboard" href="dashboard.html#/overview"' in body
    for scenario in ("simulateLoop", "simulateSecret", "simulateBoundary", "simulateCompliant", "simulateUniversalAdapter", "triggerManualFreeze"):
        assert f"async function {scenario}()" in body, f"The flagship demo lost {scenario}"
        assert f'onclick="{scenario}()"' in body


def test_the_connect_page_leads_with_the_one_line_connect_and_the_two_stages(tmp_path: Path) -> None:
    body = page_source("connect.html")
    assert body.index('id="connect-in-one-command"') < body.index('id="govern-your-repositories"')
    lead = body.split('id="connect-in-one-command"', 1)[1].split("</section>", 1)[0]
    assert "Stage 1 · Observe" in lead and "Stage 2 · Enforce" in lead and "dashboard.html#/connect" in lead
    out = run(
        "connect.html",
        r"""
  out.powershell = el('oneline-powershell').textContent;
  out.posix = el('oneline-posix').textContent;
  out.wizard = el('wizard-link').href;
  out.nav = el('page-nav').innerHTML;
""",
        tmp_path,
    )
    assert out["powershell"] == "irm https://example.test/prod/install.py -OutFile threefold.py; py threefold.py connect --project Acme-Billing"
    assert out["posix"] == "curl -fsSL https://example.test/prod/install.py -o threefold.py && python3 threefold.py connect --project Acme-Billing"
    assert out["wizard"] == "https://example.test/prod/dashboard.html#/connect"
    assert "aria-current=\"page\"" in out["nav"] and ">Connect</a>" in out["nav"], "The shared navigation replaced the page's own"


def test_settings_leads_with_signing_in_and_keeps_the_key_as_the_fallback(tmp_path: Path) -> None:
    body = page_source("settings.html")
    assert body.index('id="sign-in"') < body.index('id="operator-key"')
    sign_in = body.split('id="sign-in"', 1)[1].split('id="operator-key"', 1)[0]
    assert "python threefold.py open" in sign_in
    assert "Fallback: an operator key for this browser" in body and 'id="operatorKeyInput"' in body
    out = run(
        "settings.html",
        r"""
  out.state = el('sign-in-state').textContent;
  out.read = calls.find(c => c.url.indexOf('/policy/config') !== -1).headers;
  el('apiKeyInput').value = 'typed-key';
  await loadPolicy();
  out.typed = calls.filter(c => c.url.indexOf('/policy/config') !== -1).pop().headers;
  el('apiKeyInput').value = '';
  el('apiBaseInput').value = 'https://elsewhere.example.test/prod';
  await loadPolicy();
  out.elsewhere = calls.filter(c => c.url.indexOf('/policy/config') !== -1).pop().headers;
""",
        tmp_path,
        before="store['threefold-session'] = JSON.stringify({ token: 'tok-1', expires_at: new Date(Date.now() + 3600e3).toISOString() });",
    )
    assert "This browser is signed in" in out["state"]
    assert out["read"].get("Authorization") == "Bearer tok-1"
    assert out["typed"].get("X-API-Key") == "typed-key" and "Authorization" not in out["typed"]
    assert "Authorization" not in out["elsewhere"], "A sign-in goes only to the host that served the page"


def test_the_rules_page_sends_a_sign_in_when_the_shared_layer_is_loaded(tmp_path: Path) -> None:
    out = run(
        "rules.html",
        r"""
  out.read = calls.filter(c => /\/rules/.test(c.url)).pop().headers;
  out.nav = el('page-nav').innerHTML;
""",
        tmp_path,
        before="store['threefold-session'] = JSON.stringify({ token: 'tok-1', expires_at: new Date(Date.now() + 3600e3).toISOString() });",
    )
    assert out["read"].get("Authorization") == "Bearer tok-1"
    assert ">Rules</a>" in out["nav"]


def test_the_sessions_page_opens_the_session_a_link_names(tmp_path: Path) -> None:
    out = run(
        "sessions.html",
        r"""
  out.urls = calls.map(c => c.url);
""",
        tmp_path,
        before="location.search = '?session=' + encodeURIComponent('s-1 <b>');",
    )
    assert "https://example.test/prod/sessions/s-1%20%3Cb%3E" in out["urls"]


# ------------------------------------------------------------- the contract


def _documented(method: str, path: str, spec: dict) -> bool:
    called = path.split("/")
    for documented, operations in spec["paths"].items():
        parts = documented.split("/")
        if len(parts) != len(called) or method.lower() not in operations:
            continue
        if all((p.startswith("{") and p.endswith("}")) or p == c for p, c in zip(parts, called)):
            return True
    return False


def test_every_endpoint_the_application_calls_is_in_the_published_document(tmp_path: Path) -> None:
    """The routes the dashboard reads and writes, as the contract names them.

    Tracks B1 and B2 add these operations to openapi.json; until they are merged
    this lists exactly which are missing. Files the stack serves (the installer
    and its manifest) are not API operations and are left out.
    """
    out = dash(
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  answer = (url, init) => {
    const path = new URL(url).pathname.replace(/^\/prod/, '');
    const method = (init && init.method) || 'GET';
    if (method === 'POST' && path === '/api/sandbox') return { status: 200, body: { project: P, calls_seeded: 3 } };
    if (method === 'POST' && path === '/api/auth/sessions') return { status: 200, body: { token: 't', expires_at: null } };
    if (path.indexOf('/api/projects/') === 0 && method === 'GET') return { status: 200, body: detailBody('observe') };
    if (path === '/api/decisions') return { status: 200, body: { items: [row(1, { project_name: 'Acme-Billing' })], next_cursor: null } };
    if (path === '/api/decision') return { status: 200, body: DECISION };
    if (path === '/api/overview') return { status: 200, body: overviewBody() };
    if (path === '/api/projects') return { status: 200, body: PROJECTS };
    if (path === '/evaluate-tool-call') return { status: 200, body: { status: 'APPROVED' } };
    return { status: 200, body: {} };
  };
  store['threefold-session'] = JSON.stringify({ token: 't', expires_at: null });
  for (const r of ['#/overview', '#/projects', '#/review', '#/calls', '#/call?timestamp=t&verdict_id=VERDICT-1', '#/connect']) await visit(r);
  await visit('#/projects/Acme-Billing');
  click('promote-open'); await click('promote-confirm'); await tick();
  click('demote-open'); await click('demote-confirm'); await tick();
  await visit('#/review');
  const stamp = (view().match(/data-timestamp="([^"]+)"/) || [])[1];
  await click('label-one', { 'data-verdict': 'VERDICT-1', 'data-timestamp': stamp, 'data-label': 'correct', 'data-group-index': '0' });
  await visit('#/try');
  await click('try-create'); await tick();
  Dash.tryState.readiness = { rules: [{ rule_key: 'java-domain-stays-pure', state: 'ready' }] };
  Dash.tryState.selected = new Set(['java-domain-stays-pure']);
  Dash.tryState.observed = [row(1, { project_name: P })];
  Dash.tryState.labels = { 'VERDICT-1': 'correct' };
  await click('try-promote'); await tick();
  await click('try-send'); await tick();
  await Threefold.signOut();
  location.hash = '#/signin?code=c';
  await tick();
  out.called = Array.from(new Set(calls.map(c => c.method + ' ' + new URL(c.url).pathname.replace(/^\/prod/, '').replace(/\/(Acme-[A-Za-z0-9-]+)(?=\/|$)/, '/{name}'))));
""",
        tmp_path,
    )
    spec = json.loads((WEB / "openapi.json").read_text(encoding="utf-8"))
    called = sorted(c for c in out["called"] if not c.split(" ", 1)[1].startswith(("/dist/", "/install.py")))
    for expected in ("GET /api/overview", "GET /api/decisions", "POST /api/projects/{name}/promote", "POST /api/sandbox", "POST /evaluate-tool-call"):
        assert expected in called, f"The walk did not reach {expected}, so this test proves less than it says"
    missing = [c for c in called if not _documented(*c.split(" ", 1), spec)]
    assert not missing, f"openapi.json does not document these operations the application calls: {missing}"
