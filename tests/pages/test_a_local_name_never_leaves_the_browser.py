"""The reader's own names for the aliases: shown everywhere, sent nowhere.

The ledger knows a project only by its alias, because no real repository name
may leave the owner's machine. The owner cannot tell their own projects apart
from the aliases, so a name of their own is kept in the browser and drawn
beside each alias. The whole value of that depends on one promise, which is
what most of this file is about: a name is display text and nothing else. It
never reaches a URL, a query string, a request body, a header, the address bar
or a copied command.

These run the pages' own scripts under Node with the stub browser in
_browser.py, and drive the handlers as well as the routes, because the ways a
label could leak are all in a handler reading a rendered value back: the calls
screen's project filter, the connect wizard's name field and copy button, and
the review queue's POST to a project's own route.

Every alias here is synthetic, as the clean-room rule requires, and the labels
are obviously invented.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from _browser import page_source, run

# What this browser is told a project is called. Made up on purpose: no real
# repository is named anything like these, and a leak is unmistakable in a
# recorded request.
PRIVATE = "the paper aeroplane one"
OTHER = "second breakfast service"
# A label that would run if a page wrote it into markup unescaped.
HOSTILE = 'x"><img src=x onerror=alert(1)><svg onload=alert(2)>'

NAMES_KEY = "threefold-local-names"
HIDDEN_KEY = "threefold-local-names-hidden"

# Contract-shaped answers, trimmed to what a screen that names a project needs.
# Times are fixed two hours back so "2 h ago" is the same on every render and
# two renders of one screen can be compared character for character.
FIXTURES = r"""
const THEN = new Date(Date.now() - 7200000).toISOString();
const DAY = n => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);
const PRIVATE = %(private)s;
const OTHER = %(other)s;
const NAMES = { 'Acme-Billing': PRIVATE, 'Acme-Catalog': OTHER };
function row(i, extra) {
  return Object.assign({
    verdict_id: 'VERDICT-' + i, timestamp: new Date(Date.parse(THEN) - i * 60000).toISOString(), session_id: 's-' + i,
    developer_id: 'ab12cd34', project_name: 'Acme-Billing', tool_name: 'Write', action_type: 'FILE_WRITE',
    agent: 'claude-code', origin: 'hook', dry_run: false, status: 'APPROVED', rule: 'NONE',
    target: 'src/billing/domain/Invoice.java', reason: 'Recorded, not enforced.', observed_rule: 'java-domain-stays-pure',
    observed_rules: ['java-domain-stays-pure'], observed_reason: 'imports javax.persistence into the domain',
    observed_target: 'src/billing/domain/Invoice.java', cost_usd: 0.01, rule_key: 'java-domain-stays-pure',
    stage: 'observe', hook_mode: 'managed', review: null, reviewed_at: null, review_note: '',
    category: 'LAYERING', category_label: 'Layer crossed'
  }, extra || {});
}
const OVERVIEW = {
  window_days: 7, generated_at: THEN, source: 'rollups',
  totals: { calls: 1284, approved: 1190, refused: 37, would_refuse: 57, needs_review: 21, false_alarms: 4, projects: 2, agents: 2 },
  series: [6, 5, 4, 3, 2, 1, 0].map(n => ({ day: DAY(n), approved: 150 + n, observed: 6, refused: 4 })),
  by_agent: [{ agent: 'claude-code', calls: 900 }, { agent: 'codex', calls: 384 }],
  by_origin: [{ origin: 'hook', calls: 1200 }, { origin: 'page', calls: 84 }],
  by_rule: [{ rule_key: 'java-domain-stays-pure', refused: 20, would_refuse: 30 }],
  by_project: [
    { project: 'Acme-Billing', stage: 'enforce', configured: true, calls: 700, refused: 30, would_refuse: 10, needs_review: 3, last_seen: THEN },
    { project: 'Acme-Catalog', stage: 'observe', configured: false, calls: 584, refused: 7, would_refuse: 47, needs_review: 18, last_seen: THEN }
  ],
  stages: { observe: 1, enforce: 1 }
};
const RULES = [
  { rule_key: 'java-domain-stays-pure', kind: 'layering', mode_now: 'observe', would_refuse: 5, correct: 5, false_alarms: 0, unreviewed: 0, last_seen: THEN, state: 'ready', recommendation: 'Promote: every flag it raised was right.' }
];
const DETAIL = {
  project: 'Acme-Billing',
  config: { stage: 'observe', observe_rules: [], created_at: THEN, updated_at: THEN, promoted_at: null, demoted_at: null, sandbox: false,
    history: [{ at: THEN, action: 'create', by: 'ab12cd34', enforce: [], observe: ['LOOP'] }] },
  readiness: {
    summary: { stage: 'observe', days_observed: 5, calls_observed: 70, would_have_refused: 9, reviewed: 7, false_alarms: 1,
      false_alarm_rate: 0.14, rules_ready: 1, rules_quiet: 0, rules_noisy: 0, rules_needing_review: 0 },
    rules: RULES
  }
};
const PROJECTS = { projects: [
  { project: 'Acme-Billing', stage: 'enforce', configured: true, observe_rules: [], created_at: THEN, promoted_at: THEN, last_seen: THEN,
    calls: 700, refused: 30, would_refuse: 10, needs_review: 3, agents: ['claude-code'], hook_modes: ['managed'] },
  { project: 'Acme-Catalog', stage: 'observe', configured: false, observe_rules: [], created_at: null, promoted_at: null, last_seen: THEN,
    calls: 584, refused: 7, would_refuse: 47, needs_review: 18, agents: ['codex'], hook_modes: ['unknown'] }
] };
const DECISION = {
  decision: row(1),
  session: { session_id: 's-1', calls: 4, cost_usd: 0.02, is_tripped: false },
  rule: { id: 'java-domain-stays-pure', description: 'Billing domain classes stay free of persistence', mode: 'observe',
    when_path_matches: ['**/billing/domain/**/*.java'], forbid_imports: ['javax.persistence'], allow_imports: ['java.util'] }
};
const REVIEWABLE = { items: [row(1), row(2)], next_cursor: null };
function contract(over) {
  return api(Object.assign({
    '/api/overview': { status: 200, body: OVERVIEW },
    '/api/projects': { status: 200, body: PROJECTS },
    '/api/projects/Acme-Billing': { status: 200, body: DETAIL },
    '/api/decisions': { status: 200, body: REVIEWABLE },
    '/api/decision': { status: 200, body: DECISION },
    'POST /api/projects/Acme-Billing/reviews': { status: 200, body: { updated: 2, skipped: [] } },
    '/dist/manifest.json': { status: 200, body: { files: [], bundle_sha256: 'cd'.repeat(32), installer_sha256: 'ab'.repeat(32) } }
  }, over || {}));
}
// Every request this program recorded, flattened to text, so one search finds a
// label wherever it might have travelled.
function sentText() {
  return calls.map(c => c.method + ' ' + c.url + ' ' + JSON.stringify(c.headers || {}) + ' ' + JSON.stringify(c.body === undefined ? null : c.body)).join('\n');
}
function attr(markup, name) {
  const found = [];
  const re = new RegExp(name + '="([^"]*)"', 'g');
  let m;
  while ((m = re.exec(String(markup)))) found.push(m[1]);
  return found;
}
""" % {"private": json.dumps(PRIVATE), "other": json.dumps(OTHER)}

# Every screen of the dashboard that names a project.
NAMING_ROUTES = [
    "#/overview",
    "#/projects",
    "#/projects/Acme-Billing",
    "#/review",
    "#/calls",
    "#/call?timestamp=t&verdict_id=VERDICT-1",
    "#/connect?project=Acme-Billing",
]
# The same, minus the overview: its charts number their own ids as they are
# drawn, so two renders of it differ in the ids alone and cannot be compared
# character for character. The overview is checked for labels on its own.
COMPARABLE_ROUTES = [r for r in NAMING_ROUTES if r != "#/overview"]


def dash(scenario: str, tmp_path: Path, before: str = "") -> dict:
    return run("dashboard.html", scenario, tmp_path, before=FIXTURES + before)


def stored(names: dict, hidden: bool = False) -> str:
    """What a browser holding these names looks like before a page loads."""
    setup = "store[%s] = %s;\n" % (json.dumps(NAMES_KEY), json.dumps(json.dumps(names)))
    if hidden:
        setup += "store[%s] = '1';\n" % json.dumps(HIDDEN_KEY)
    return setup


# ------------------------------------------------------- the shared layer


def test_a_name_is_kept_only_as_one_clean_line_of_text(tmp_path: Path) -> None:
    out = dash(
        r"""
  out.written = Threefold.writeLocalNames({
    'Acme-Billing': '  the paper   aeroplane\n one  ',
    'Acme-Catalog': 42,
    'Acme-Ledger': { nested: true },
    'Acme-Empty': '   ',
    '': 'no alias at all',
    '__proto__': 'never an object\'s prototype'
  });
  out.kept = Threefold.readLocalNames();
  out.stored = JSON.parse(store['threefold-local-names']);
  out.protoIsClean = Object.getPrototypeOf(Threefold.readLocalNames()) === Object.prototype;
  const long = 'z'.repeat(400);
  Threefold.writeLocalNames({ 'Acme-Billing': long });
  out.longest = Threefold.readLocalNames()['Acme-Billing'].length;
  out.max = Threefold.LOCAL_NAME_MAX;
  const many = {};
  for (let i = 0; i < Threefold.LOCAL_NAMES_MAX + 25; i++) many['Acme-P' + i] = 'name ' + i;
  Threefold.writeLocalNames(many);
  out.capped = Object.keys(Threefold.readLocalNames()).length;
  out.cap = Threefold.LOCAL_NAMES_MAX;
  Threefold.writeLocalNames({});
  out.emptied = !('threefold-local-names' in store);
  out.junk = (store['threefold-local-names'] = '{not json', Threefold.readLocalNames());
  out.notAnObject = (store['threefold-local-names'] = '["a list"]', Threefold.readLocalNames());
""",
        tmp_path,
    )
    assert out["written"] is True
    assert out["kept"] == {"Acme-Billing": "the paper aeroplane one"}, "one line, trimmed, and nothing that is not text"
    assert out["stored"] == out["kept"], "what is on screen is what is in storage"
    assert out["protoIsClean"], "a pasted __proto__ never reaches an object's prototype"
    assert out["longest"] == out["max"] and out["max"] == 60
    assert out["capped"] == out["cap"] and out["cap"] == 200
    assert out["emptied"], "clearing every name removes the key rather than storing {}"
    assert out["junk"] == {} and out["notAnObject"] == {}


def test_the_switch_is_absent_until_a_name_is_set_and_then_says_what_it_does(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract();
  await visit('#/projects');
  out.bare = el('tf-nav').innerHTML;
  Threefold.writeLocalNames(NAMES);
  await tick();
  out.withName = el('tf-nav').innerHTML;
  const button = makeEl('switch');
  button.setAttribute('data-tf-names', '');
  (docListeners.click || []).forEach(fn => fn({ target: button }));
  await tick();
  out.afterPress = el('tf-nav').innerHTML;
  out.hiddenStored = store['threefold-local-names-hidden'];
  (docListeners.click || []).forEach(fn => fn({ target: button }));
  await tick();
  out.afterSecondPress = el('tf-nav').innerHTML;
  out.hiddenGone = !('threefold-local-names-hidden' in store);
""",
        tmp_path,
    )
    assert "data-tf-names" not in out["bare"], "no name set, so the navigation reads exactly as it did"
    assert "data-tf-names" in out["withName"]
    assert '<button type="button"' in out["withName"], "a button, so it is reachable and worked from the keyboard"
    assert 'aria-pressed="true"' in out["withName"] and "Your names: on" in out["withName"]
    assert "Press to hide them" in out["withName"], "the control says what pressing it does"
    assert 'aria-pressed="false"' in out["afterPress"] and "Your names: off" in out["afterPress"]
    assert "Press to show them" in out["afterPress"]
    assert out["hiddenStored"] == "1", "remembered in this browser"
    assert 'aria-pressed="true"' in out["afterSecondPress"] and out["hiddenGone"]


def test_a_browser_that_blocks_storage_keeps_the_names_for_the_tab_and_draws_the_pages(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract();
  storageBlocked = true;
  out.written = Threefold.writeLocalNames(NAMES);
  out.kept = Threefold.readLocalNames();
  out.hiddenWritten = Threefold.setLocalNamesHidden(true);
  out.hidden = Threefold.localNamesHidden();
  Threefold.setLocalNamesHidden(false);
  await visit('#/projects');
  out.drew = view();
""",
        tmp_path,
    )
    assert out["written"] is False, "a blocked write says so rather than appearing to have saved"
    assert out["kept"] == {"Acme-Billing": PRIVATE, "Acme-Catalog": OTHER}, "kept for this tab"
    assert out["hiddenWritten"] is False and out["hidden"] is True
    assert "Acme-Billing" in out["drew"] and PRIVATE in out["drew"], "the screen still draws, and still labels"


# ---------------------------------------------------------- what is drawn


def test_every_screen_that_names_a_project_shows_the_reader_s_own_name(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract();
  out.screens = {};
  for (const route of %(routes)s) {
    await visit(route);
    out.screens[route] = view();
  }
""" % {"routes": json.dumps(NAMING_ROUTES)},
        tmp_path,
        before=stored({"Acme-Billing": PRIVATE, "Acme-Catalog": OTHER}),
    )
    for route, markup in out["screens"].items():
        assert "Acme-Billing" in markup, f"{route} dropped the alias the API knows"
        assert PRIVATE in markup, f"{route} shows no name of the reader's own"
        assert 'class="tf-local-name"' in markup, f"{route} does not draw the name in the quieter style"


def test_the_breadcrumb_and_the_heading_of_a_project_carry_the_name(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract();
  await visit('#/projects/Acme-Billing');
  const markup = view();
  out.crumb = markup.split('</nav>')[0];
  out.heading = markup.split('id="view-title"')[1].split('</h1>')[0];
  out.title = document.title;
""",
        tmp_path,
        before=stored({"Acme-Billing": PRIVATE}),
    )
    assert "Acme-Billing" in out["crumb"] and PRIVATE in out["crumb"]
    assert "Acme-Billing" in out["heading"] and PRIVATE in out["heading"]
    assert PRIVATE not in out["title"], "the document title stays the alias, so a shared window says nothing"


def test_the_switch_hides_every_name_and_the_screens_read_as_they_did_before(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract();
  out.bare = {};
  for (const route of %(routes)s) { await visit(route); out.bare[route] = view(); }
  await visit('#/nothing-here');
  store['threefold-local-names'] = JSON.stringify(NAMES);
  store['threefold-local-names-hidden'] = '1';
  out.hidden = {};
  for (const route of %(routes)s) { await visit(route); out.hidden[route] = view(); }
  await visit('#/nothing-here');
  delete store['threefold-local-names-hidden'];
  out.shown = {};
  for (const route of %(all)s) { await visit(route); out.shown[route] = view(); }
""" % {"routes": json.dumps(COMPARABLE_ROUTES), "all": json.dumps(NAMING_ROUTES)},
        tmp_path,
    )
    for route, markup in out["hidden"].items():
        assert markup == out["bare"][route], f"{route} does not read exactly as it does with no name set"
    for route, markup in out["shown"].items():
        assert PRIVATE in markup, f"{route} lost its name when the switch went back on"
    for route, markup in out["hidden"].items():
        assert PRIVATE not in markup and OTHER not in markup, f"{route} still shows a name while they are hidden"


def test_a_name_carrying_markup_is_escaped_like_every_other_untrusted_string(tmp_path: Path) -> None:
    out = dash(
        r"""
  answer = contract();
  out.screens = {};
  for (const route of %(routes)s) { await visit(route); out.screens[route] = view(); }
  await visit('#/calls?project=Acme-Billing');
  out.chips = attr(view(), 'aria-label');
""" % {"routes": json.dumps(NAMING_ROUTES)},
        tmp_path,
        before=stored({"Acme-Billing": HOSTILE}),
    )
    for route, markup in out["screens"].items():
        assert "<img src=x" not in markup and "<svg onload" not in markup, f"{route} wrote a name into the page as markup"
        assert "&lt;img src=x onerror=alert(1)&gt;" in markup, f"{route} did not escape the name"
    for value in out["chips"]:
        assert "<" not in value and '"' not in value, "a name reaches an aria-label as text, never as markup"


def test_the_sessions_page_names_a_project_the_reader_s_way_in_the_table_and_the_detail(tmp_path: Path) -> None:
    out = run(
        "sessions.html",
        r"""
  out.table = el('sessions-body').innerHTML;
  await selectSession('s-1');
  out.detail = el('detail-body').innerHTML;
  el('filterInput').value = 'paper aeroplane';
  renderTable();
  out.filtered = el('sessions-body').innerHTML;
  el('filterInput').value = '';
  Threefold.setLocalNamesHidden(true);
  await tick();
  out.hiddenTable = el('sessions-body').innerHTML;
  out.hiddenDetail = el('detail-body').innerHTML;
  out.sent = calls.map(c => c.url).join(' ');
""",
        tmp_path,
        before=stored({"Acme-Billing": PRIVATE}) + r"""
answer = api({
  '/api/sessions': { status: 200, body: { sessions: [
    { session_id: 's-1', project_name: 'Acme-Billing', developer_id: 'ab12cd34', calls: 4, total_cost_usd: 0.02,
      total_input_tokens: 10, total_output_tokens: 5, is_tripped: false, is_terminated: false, created_at: new Date(Date.now() - 7200000).toISOString() }
  ], persistence: 'dynamodb' } },
  '/sessions/s-1': { status: 200, body: { session_id: 's-1', project_name: 'Acme-Billing', is_tripped: false,
    tool_call_history_count: 4, cumulative_cost_usd: 0.02, budget_usd: 10, budget_remaining_usd: 9.98,
    created_at: new Date(Date.now() - 7200000).toISOString(), tool_call_history: [] } }
});
""",
    )
    assert "Acme-Billing" in out["table"] and PRIVATE in out["table"]
    assert "Acme-Billing" in out["detail"] and PRIVATE in out["detail"]
    assert "Acme-Billing" in out["filtered"], "the filter finds a project by the reader's own name for it"
    assert PRIVATE not in out["hiddenTable"] and "Acme-Billing" in out["hiddenTable"]
    assert PRIVATE not in out["hiddenDetail"] and "Acme-Billing" in out["hiddenDetail"]
    assert PRIVATE not in out["sent"] and "paper" not in out["sent"], "neither the filter nor the switch sends anything"


def test_the_rules_page_says_whose_rules_are_on_screen_the_reader_s_way(tmp_path: Path) -> None:
    out = run(
        "rules.html",
        r"""
  out.scope = el('scope-badge').innerText || el('scope-badge').textContent;
  out.origin = el('rules-origin').innerText || el('rules-origin').textContent;
  out.target = el('save-target').innerText || el('save-target').textContent;
  out.field = el('project-input').value;
  Threefold.setLocalNamesHidden(true);
  await tick();
  out.hiddenOrigin = el('rules-origin').innerText || el('rules-origin').textContent;
  out.sent = calls.map(c => c.method + ' ' + c.url + ' ' + JSON.stringify(c.body || null)).join('\n');
""",
        tmp_path,
        pathname="/prod/rules.html",
        before=stored({"Acme-Billing": PRIVATE}) + r"""
location.search = '?project=Acme-Billing';
answer = api({ '/rules': { status: 200, body: { rules: [
  { id: 'java-domain-stays-pure', description: 'Billing domain classes stay free of persistence', mode: 'observe',
    when_path_matches: ['**/billing/domain/**/*.java'], forbid_imports: ['javax.persistence'] }
], is_default: false, count: 1, extensions_read: ['.java'], refresh_seconds: 30 } } });
""",
    )
    assert "Acme-Billing" in out["scope"] and PRIVATE in out["scope"]
    assert "Acme-Billing" in out["origin"] and PRIVATE in out["origin"]
    assert "Acme-Billing" in out["target"] and PRIVATE in out["target"]
    assert out["field"] == "Acme-Billing", "the field a save is built from holds the alias alone"
    assert PRIVATE not in out["hiddenOrigin"] and "Acme-Billing" in out["hiddenOrigin"]
    assert PRIVATE not in out["sent"] and "paper" not in out["sent"]


# ------------------------------------------- never in a link, never in a request


def test_no_request_no_address_and_no_copied_command_carries_a_name(tmp_path: Path) -> None:
    """Drives the handlers, not only the routes: that is where a label could leak."""
    out = dash(
        r"""
  answer = contract();

  // Every screen that names a project, drawn.
  for (const route of %(routes)s) { await visit(route); }

  // The calls screen's filter: the page put the alias in the field, and Apply
  // sends back whatever the field holds.
  await visit('#/calls?project=Acme-Billing');
  out.projectField = attr(view().split('id="f-project"')[1].split('/>')[0], 'value');
  el('f-project').value = 'Acme-Billing';
  el('f-kind').value = 'observed';
  await click('filter');
  await tick();
  out.hashAfterFilter = location.hash;

  // The review queue: labelling a group posts to that project's own route.
  await visit('#/review');
  await click('label-group', { 'data-group': JSON.stringify(['Acme-Billing', 'java-domain-stays-pure']), 'data-label': 'correct' });
  await tick();

  // A single call's label, which builds the same route from the row.
  await visit('#/call?timestamp=t&verdict_id=VERDICT-1');
  await click('call-label', { 'data-label': 'correct' });
  await tick();

  // The connect wizard: the command a reader copies, and the field it is built
  // from. The copy goes through the shared layer's own click handler.
  await visit('#/connect?project=Acme-Billing');
  out.nameField = attr(view().split('id="connect-name"')[1].split('/>')[0], 'value');
  const commands = attr(view(), 'data-tf-copy');
  out.commands = commands;
  for (const command of commands) {
    const node = makeEl('copy');
    node.setAttribute('data-tf-copy', command);
    (docListeners.click || []).forEach(fn => fn({ target: node }));
  }
  await tick();
  Dash.connect.setName('Acme-Billing');
  await tick();

  out.copied = copied;
  out.replaced = replaced;
  out.hash = location.hash;
  out.sent = sentText();
  out.hrefs = attr(view(), 'href');
""" % {"routes": json.dumps(NAMING_ROUTES)},
        tmp_path,
        before=stored({"Acme-Billing": PRIVATE, "Acme-Catalog": OTHER}),
    )
    for label in (PRIVATE, OTHER):
        assert label not in out["sent"], f"a request carried {label!r}:\n{out['sent']}"
        assert label not in out["hash"] and label not in out["hashAfterFilter"], "the address bar carried a name"
        assert not any(label in url for url in out["replaced"]), "a rewritten address carried a name"
        assert not any(label in text for text in out["copied"]), "a copied command carried a name"
        assert not any(label in href for href in out["hrefs"]), "a link carried a name"
    # Some words of the label on their own would be a leak too: nothing of it travels.
    assert "aeroplane" not in out["sent"] and "breakfast" not in out["sent"]
    assert out["projectField"] == ["Acme-Billing"], "the filter field holds the alias, which is what Apply sends"
    assert out["nameField"] == ["Acme-Billing"], "the connect field holds the alias, which the command is built from"
    assert out["commands"] and all("--project Acme-Billing" in c for c in out["commands"])
    assert any("/api/projects/Acme-Billing/reviews" in line for line in out["sent"].split("\n")), \
        "the review did reach the service, so the check above is not passing on an empty list"


def test_the_pages_never_put_a_name_where_a_handler_would_read_it_back() -> None:
    """The fields and commands a handler reads are built from the alias, in the source."""
    dashboard = page_source("dashboard.html")
    assert 'id="f-project" class="tf-input w-full font-mono" value="${f.project}"' in dashboard
    assert 'id="connect-name" type="text" class="tf-input w-full font-mono" value="${state.name}"' in dashboard
    assert "'irm ' + base + 'install.py -OutFile threefold.py; py threefold.py connect --project ' + name" in dashboard
    rules = page_source("rules.html")
    assert "const typed = document.getElementById('project-input').value.trim();" in rules
    assert "if (currentProject) url.searchParams.set('project', currentProject); else url.searchParams.delete('project');" in rules
    # The helpers that add a label return markup or plain text for display. None
    # of them is used to build a path, a query or a body anywhere.
    for page in ("dashboard.html", "sessions.html", "rules.html", "settings.html", "assets/threefold.js"):
        body = page_source(page)
        for line in body.split("\n"):
            if not re.search(r"projectName\(|projectNameText\(|projectShown\(|projectShownText\(|projectLabel\(", line):
                continue
            if "function " in line or "//" in line.split("project")[0]:
                continue
            for sink in ("encodeURIComponent(", "searchParams.set", "fetch(", "T.api(", "data-tf-copy=\"${"):
                assert sink not in line, f"{page}: a name is built into {sink} here:\n{line.strip()}"


# ------------------------------------------------------------ the panel


SETTINGS = r"""
answer = api({
  '/policy/config': { status: 200, body: { max_single_call_usd: 1, max_session_budget_usd: 10,
    loop_history_window: 6, monomorphic_repetition_threshold: 3, blocked_patterns: [] } },
  '/api/projects': { status: 200, body: { projects: [
    { project: 'Acme-Billing' }, { project: 'Acme-Catalog' }
  ] } }
});
"""


def settings(scenario: str, tmp_path: Path, before: str = "") -> dict:
    return run("settings.html", scenario, tmp_path, before=SETTINGS + before)


def test_the_panel_offers_a_field_for_every_project_the_stack_knows(tmp_path: Path) -> None:
    out = settings(
        r"""
  out.rows = el('local-names-rows').innerHTML;
  el('ln-0').value = '  the paper aeroplane one ';
  saveLocalNames();
  await tick();
  out.stored = store['threefold-local-names'];
  out.state = el('local-names-state').innerHTML;
  out.sent = calls.map(c => c.method + ' ' + c.url + ' ' + JSON.stringify(c.body || null)).join('\n');
  out.rowsAfter = el('local-names-rows').innerHTML;
""",
        tmp_path,
    )
    assert 'id="ln-0"' in out["rows"] and 'id="ln-1"' in out["rows"]
    assert "Acme-Billing" in out["rows"] and "Acme-Catalog" in out["rows"]
    assert json.loads(out["stored"]) == {"Acme-Billing": PRIVATE}
    assert "Saved in this browser: 1 name" in out["state"] and "Nothing was sent" in out["state"]
    assert PRIVATE not in out["sent"] and "aeroplane" not in out["sent"], "saving a name sends nothing"
    assert PRIVATE in out["rowsAfter"], "the fields show what was kept"


def test_the_panel_takes_a_mapping_pasted_from_a_file_and_says_what_it_left_out(tmp_path: Path) -> None:
    out = settings(
        r"""
  el('local-names-json').value = JSON.stringify({ 'Acme-Catalog': 'second breakfast service', 'Acme-Ledger': 12, 'Acme-Vault': 'a name from a file' });
  el('ln-0').value = 'the paper aeroplane one';
  saveLocalNames();
  await tick();
  out.stored = JSON.parse(store['threefold-local-names']);
  out.state = el('local-names-state').innerHTML;
  out.box = el('local-names-json').value;
  out.rows = el('local-names-rows').innerHTML;

  el('local-names-json').value = 'not json at all';
  saveLocalNames();
  out.badState = el('local-names-state').innerHTML;
  out.unchanged = JSON.parse(store['threefold-local-names']);

  el('local-names-json').value = '["a list"]';
  saveLocalNames();
  out.listState = el('local-names-state').innerHTML;
""",
        tmp_path,
    )
    assert out["stored"] == {
        "Acme-Billing": PRIVATE,
        "Acme-Catalog": OTHER,
        "Acme-Vault": "a name from a file",
    }, "the paste is applied over the fields, and can name an alias with no field yet"
    assert "1 entry was left out" in out["state"] and "has to be text" in out["state"]
    assert out["box"] == "", "the box is emptied once what was in it is stored"
    assert "Acme-Vault" in out["rows"], "an alias only the paste knew is listed and editable"
    assert "not JSON" in out["badState"] and "nothing changed" in out["badState"]
    assert out["unchanged"] == out["stored"], "a paste that will not parse changes nothing"
    assert "JSON object of alias to name" in out["listState"]


def test_clear_all_and_copy_as_json_do_what_they_say(tmp_path: Path) -> None:
    out = settings(
        r"""
  el('ln-0').value = 'the paper aeroplane one';
  saveLocalNames();
  await tick();
  copyLocalNames(el('local-names-copy'));
  out.copied = copied.slice();
  out.copyState = el('local-names-state').innerHTML;
  clearLocalNames();
  await tick();
  out.afterClear = 'threefold-local-names' in store;
  out.clearState = el('local-names-state').innerHTML;
  out.rows = el('local-names-rows').innerHTML;
  out.sent = calls.map(c => c.url).join(' ');
""",
        tmp_path,
    )
    assert json.loads(out["copied"][0]) == {"Acme-Billing": PRIVATE}
    assert "Copied 1 name" in out["copyState"] and "not a backup" in out["copyState"]
    assert out["afterClear"] is False and "Cleared from this browser" in out["clearState"]
    assert PRIVATE not in out["rows"], "the fields empty with the storage"
    assert PRIVATE not in out["sent"] and "aeroplane" not in out["sent"]


def test_the_panel_says_plainly_that_nothing_is_sent_and_that_storage_is_one_browser() -> None:
    body = page_source("settings.html")
    panel = body.split('id="local-names"', 1)[1].split("<!-- WHAT EACH THRESHOLD GATES", 1)[0]
    assert "Your own names for these projects" in panel
    assert "Nothing here is sent anywhere." in panel
    assert "threefold-local-names" in panel, "the panel names the key it writes"
    assert "belongs to that browser on that machine" in panel
    assert "clearing this site's data deletes them" in panel
    assert ">Save<" in panel and ">Clear all<" in panel and ">Copy as JSON<" in panel
    assert "status --names-json" in panel, "and how to build the mapping from the machine's own checkouts"


def test_a_blocked_browser_is_told_the_names_live_only_as_long_as_the_tab(tmp_path: Path) -> None:
    out = settings(
        r"""
  storageBlocked = true;
  el('ln-0').value = 'the paper aeroplane one';
  saveLocalNames();
  await tick();
  out.state = el('local-names-state').innerHTML;
  out.rows = el('local-names-rows').innerHTML;
""",
        tmp_path,
    )
    assert "blocks site storage" in out["state"] and "go when it closes" in out["state"]
    assert "Nothing was sent to the service" in out["state"]
    assert PRIVATE in out["rows"], "the name still works for as long as the tab is open"


def test_the_panel_lists_the_names_already_stored_when_the_project_list_cannot_be_read(tmp_path: Path) -> None:
    out = settings(
        r"""
  out.rows = el('local-names-rows').innerHTML;
""",
        tmp_path,
        before=stored({"Acme-Vault": "a name from a file"}) + "answer = api({ '/api/projects': { status: 403, body: { title: 'Forbidden' } } });",
    )
    assert "could not be read" in out["rows"] and "HTTP 403" in out["rows"]
    assert "Acme-Vault" in out["rows"] and "a name from a file" in out["rows"]


@pytest.mark.parametrize("page", ["dashboard.html", "sessions.html", "rules.html", "settings.html"])
def test_no_page_ships_a_name_of_its_own(page: str) -> None:
    """Nothing in the files carries a label: they only ever come from the browser."""
    body = page_source(page)
    assert "threefold-local-names" not in body or page == "settings.html", \
        "only the settings panel names the key, and only to say where it writes"
    assert PRIVATE not in body and OTHER not in body
