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


# ------------------------------------------------------------------- routes


def test_the_dashboard_declares_every_route_the_contract_lists() -> None:
    body = page_source("dashboard.html")
    declared = set(re.findall(r"\{ path: '([^']+)'", body))
    assert declared == _contract_routes()


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
    for path in _served_files():
        body = path.read_text(encoding="utf-8")
        name = path.relative_to(WEB).as_posix()
        assert not literal_header.search(body), f"{name} sends a literal credential"
        assert not stored_literal.search(body), f"{name} stores a literal credential"
        assert not filled_field.search(body), f"{name} ships a key field already filled"
        for token in long_token.findall(body):
            assert len(set(token)) == 1, f"{name} carries a long token-like string: {token[:12]}…"


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
    assert "Observe: what the hooks send is judged and recorded, and not refused" in observe
    assert "a call carrying a credential, which the hook refuses on the machine" in observe
    for chip in (">Ready<", ">Quiet<", ">Noisy<"):
        assert chip in observe
    assert "Keep observing: one flag was a false alarm." in observe
    assert "irm https://example.test/prod/install.py -OutFile threefold.py; py threefold.py connect --project Acme-Billing" in observe
    assert "curl -fsSL https://example.test/prod/install.py -o threefold.py &amp;&amp; python3 threefold.py connect --project Acme-Billing" in observe

    enforce = out["enforce"]
    assert "Enforce: the rules in force refuse a call before it runs" in enforce
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
    assert "All 2 correct" in page and "All 1 correct" in page
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


def test_the_walkthrough_runs_the_rollout_from_sandbox_to_a_real_refusal(tmp_path: Path) -> None:
    out = dash(
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
  out.step3 = text(view());
  await click('try-label', { 'data-verdict': 'VERDICT-1', 'data-label': 'correct' });
  await click('try-label', { 'data-verdict': 'VERDICT-2', 'data-label': 'correct' });
  await click('try-label', { 'data-verdict': 'VERDICT-3', 'data-label': 'false_alarm' });
  await tick();
  await click('try-readiness'); await tick();
  out.step4 = view();
  await click('try-promote'); await tick();
  out.step5 = text(view());
  await click('try-send'); await tick();
  out.done = text(view());
  out.sent = sent;
  out.saved = JSON.parse(store['threefold-try'] || 'null');
""",
        tmp_path,
    )
    assert "Make my sandbox" in out["step1"]
    assert out["sent"]["sandbox"] == {}
    assert "Acme-Sandbox-0a1b2c3d received 12 calls" in out["step2"]
    assert "One of them is a false alarm" in out["step3"] and "docs/.git-hooks-howto.md" in out["step3"]
    assert [r["label"] for r in out["sent"]["reviews"]] == ["correct", "correct", "false_alarm"]
    checked = dict(re.findall(r'data-rule="([^"]+)"\s*(checked)?', out["step4"]))
    assert checked == {"python-domain-stays-pure": "checked", "PROTECTED_PATH": "", "LOOP": "checked"}, "Ready and Quiet are checked; the noisy rule keeps observing"
    assert out["sent"]["promote"] == {"enforce": ["python-domain-stays-pure", "LOOP"]}
    evaluate = out["sent"]["evaluate"]
    assert evaluate["origin"] == "hook" and evaluate["agent"] == "claude-code" and evaluate["explain"] is True
    assert evaluate["project_name"] == "Acme-Sandbox-0a1b2c3d" and evaluate["tool_name"] == "Write"
    assert evaluate["arguments"] == {"file_path": "src/acme/domain/order.py", "content": "import boto3\n"}
    assert "Refused, before it ran" in out["done"] and "BLOCKED_BOUNDARY_VIOLATION" in out["done"]
    assert "couples the model to infrastructure" in out["done"] and "Amazon Bedrock" in out["done"]
    assert out["saved"]["project"] == "Acme-Sandbox-0a1b2c3d", "A reload can resume the same sandbox"


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
    assert "does not let visitors make a sandbox" in out["refused"]


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
