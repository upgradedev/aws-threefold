"""The operator's screens: what needs attention, what the numbers are made of, and the portfolio.

The overview leads with what an operator acts on today (the review queue, the
rules a false alarm made noisy, the sessions the circuit breaker halted), says
on the public demo what its numbers are made of, and counts the coding agents
apart from every other caller. The projects screen reads as a portfolio: each
project's stage, where its calls come from, and how far its labels have come.
Run under Node with the stub browser in _browser.py, with answers shaped as the
contracts in STATE.md fix them.

Every name here is synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import re
from pathlib import Path

from _browser import run
from test_the_application_pages import FIXTURES

OPS = r"""
const PUBLIC = { status: 200, body: { authenticated: false, via: null, expires_at: null, reads_public: true, sandbox_writes: true } };
const PRIVATE = { status: 200, body: { authenticated: true, via: 'session', expires_at: null, reads_public: false, sandbox_writes: false } };
function falseAlarm(i, project, rule) {
  return row(i, { project_name: project, rule_key: rule, observed_rules: [rule], review: 'false_alarm', reviewed_at: NOW });
}
function session(id, project, tripped) {
  return { session_id: id, project_name: project, developer_id: 'ab12cd34', calls: 4, total_cost_usd: 0.01,
    is_tripped: tripped, trip_reason: tripped ? 'Loop detected: the same call three times' : '', is_terminated: false, created_at: NOW };
}
"""


def ops(scenario: str, tmp_path: Path) -> dict:
    return run("dashboard.html", scenario, tmp_path, before=FIXTURES + OPS)


# ------------------------------------------------------- needs your attention


def test_the_overview_leads_with_what_needs_the_operator(tmp_path: Path) -> None:
    out = ops(
        r"""
  answer = contract({
    '/api/decisions': { status: 200, body: { items: [
      falseAlarm(1, 'Acme-Ledger', 'java-domain-stays-pure'), falseAlarm(2, 'Acme-Ledger', 'java-domain-stays-pure'),
      falseAlarm(3, 'Acme-Checkout', 'PROTECTED_PATH'), row(4)
    ], next_cursor: null } },
    '/api/sessions': { status: 200, body: { sessions: [
      session('fleet-ledger-codex-1', 'Acme-Ledger', true), session('sim-0a1b2c3d', 'Acme-Demo', true), session('fleet-ledger-codex-2', 'Acme-Ledger', false)
    ] } }
  });
  await visit('#/overview?days=7');
  await tick();
  out.view = view();
  out.text = text(view());
  out.reads = calls.map(c => c.url);
  await runIntervals();
  out.afterTimer = calls.map(c => c.url);
""",
        tmp_path,
    )
    page, words = out["view"], out["text"]
    assert page.index("Needs your attention") < page.index('data-metric="calls"'), "The strip comes before the numbers"
    # The queue: its size, and the projects with the most waiting first.
    assert "21 calls wait for your label" in words
    assert page.index('href="#/review?project=Acme-Catalog&amp;days=7"') < page.index('href="#/review?project=Acme-Billing&amp;days=7"')
    assert 'href="#/review?days=7"' in page, "The strip starts the review in the same window"
    # Rules a false alarm made noisy, from the ledger's labels, and only those.
    assert "2 rules turned noisy" in words and "2 false alarms" in words and "1 false alarm" in words
    assert page.index("java-domain-stays-pure") < page.index("PROTECTED_PATH"), "The noisiest rule first"
    assert "https://example.test/prod/api/decisions?review=false_alarm&days=7&limit=200" in out["reads"]
    # Halted sessions, leaving out the demo page's own scenarios.
    assert "1 session halted" in words and "fleet-ledger-codex-1" in words
    assert "sim-0a1b2c3d" not in words and "fleet-ledger-codex-2" not in words
    assert "https://example.test/prod/sessions.html?session=fleet-ledger-codex-1" in page
    assert "https://example.test/prod/api/sessions?limit=200" in out["reads"]
    # The thirty-second refresh re-reads the overview, never the table scan.
    scans = lambda urls: sum(1 for u in urls if "/api/sessions" in u)
    assert scans(out["afterTimer"]) == scans(out["reads"]) == 1
    overviews = lambda urls: sum(1 for u in urls if "/api/overview" in u)
    assert overviews(out["afterTimer"]) == overviews(out["reads"]) + 1


def test_a_quiet_window_says_each_slot_is_clear(tmp_path: Path) -> None:
    out = ops(
        r"""
  answer = contract({
    '/api/overview': { status: 200, body: overviewBody({ needs_review: 0 }) },
    '/api/decisions': { status: 200, body: { items: [], next_cursor: null } },
    '/api/sessions': { status: 200, body: { sessions: [session('fleet-search-claude-code-1', 'Acme-Search', false)] } }
  });
  await visit('#/overview?days=7');
  await tick();
  out.text = text(view());
  out.tones = (view().match(/data-tone="ok"/g) || []).length;
""",
        tmp_path,
    )
    words = out["text"]
    assert "Nothing waits for a label" in words and "No rule turned noisy" in words and "No session is halted" in words
    assert out["tones"] == 3


def test_a_slot_that_cannot_be_read_says_so_and_guesses_nothing(tmp_path: Path) -> None:
    out = ops(
        r"""
  answer = contract({
    '/api/decisions': { status: 200, body: { items: [falseAlarm(1, 'Acme-Ledger', 'LOOP')], next_cursor: 'more' } },
    '/api/sessions': { status: 403, body: { detail: 'Forbidden' } }
  });
  await visit('#/overview?days=7');
  await tick();
  out.text = text(view());
""",
        tmp_path,
    )
    words = out["text"]
    assert "Sessions could not be read" in words and "needs the operator" in words
    assert "No session is halted" not in words, "An unread list is not an empty one"
    assert "Counted over the newest 1 false alarm; the ledger holds more." in words


# --------------------------------------------------- what the numbers are made of


def test_the_public_demo_says_what_its_numbers_are_made_of(tmp_path: Path) -> None:
    out = ops(
        r"""
  const sources = { fleet: { calls: 3210, projects: 6 }, sandbox: { calls: 96, projects: 8 }, other: { calls: 41, projects: 2 } };
  const split = { sandbox: { projects: 8, calls: 96, approved: 60, refused: 0, would_refuse: 36, needs_review: 30, false_alarms: 2 },
                  elsewhere: { projects: 8, calls: 3251, approved: 3000, refused: 37, would_refuse: 21, needs_review: 0, false_alarms: 2 } };
  answer = contract({ '/api/auth/whoami': PUBLIC, '/api/overview': { status: 200, body: Object.assign(overviewBody(), { sources, sandbox_split: split }) } });
  Threefold.whoami(true);
  await visit('#/overview?days=7');
  out.sources = view();
  answer = contract({ '/api/auth/whoami': PUBLIC, '/api/overview': { status: 200, body: Object.assign(overviewBody(), { sandbox_split: split }) } });
  await visit('#/overview?days=14');
  out.split = view();
  answer = contract({ '/api/auth/whoami': PRIVATE, '/api/overview': { status: 200, body: Object.assign(overviewBody(), { sources, sandbox_split: split }) } });
  Threefold.whoami(true);
  await visit('#/overview?days=30');
  out.private = view();
""",
        tmp_path,
    )
    sources = re.sub(r"<[^>]+>", "", out["sources"])
    assert 'data-sources="sources"' in out["sources"]
    assert "Everything on this public demo is synthetic. 3,210 of these calls come from the Acme fleet, 6 synthetic projects" in sources
    assert "96 from visitors' sandboxes" in sources and "41 from the service's own probes and the demo page" in sources
    split = re.sub(r"<[^>]+>", "", out["split"])
    assert 'data-sources="sandbox_split"' in out["split"]
    assert "96 of these calls come from visitors' sandboxes, and the other 3,251 from the synthetic Acme fleet" in split
    assert "30 of them are in visitors' sandboxes." in split, "The queue says how much of it is sandboxes'"
    assert "tf-ops-source" not in out["private"], "A private stack's numbers are the operator's own"


def test_the_agents_tile_counts_coding_agents_and_names_the_rest_apart(tmp_path: Path) -> None:
    out = ops(
        r"""
  const body = overviewBody({ agents: 5, coding_agents: 3 });
  body.by_agent = [
    { agent: 'claude-code', calls: 700, kind: 'coding_agent', calls_in_sandboxes: 12 },
    { agent: 'codex', calls: 300, kind: 'coding_agent', calls_in_sandboxes: 300 },
    { agent: 'antigravity', calls: 200, kind: 'coding_agent', calls_in_sandboxes: 0 },
    { agent: 'page', calls: 80, kind: 'page', calls_in_sandboxes: 0 },
    { agent: 'unknown', calls: 4, kind: 'unknown', calls_in_sandboxes: 0 }
  ];
  body.coding_agents = body.by_agent.slice(0, 3);
  answer = contract({ '/api/overview': { status: 200, body } });
  await visit('#/overview?days=7');
  out.view = view();
  out.metrics = metrics(view());
""",
        tmp_path,
    )
    page = out["view"]
    assert out["metrics"]["coding_agents"] == "3" and "agents" not in out["metrics"], "The tile counts coding agents only"
    tile = re.search(r'aria-label="(Coding agents: 3[^"]*)"', page)
    assert tile and "not agents: Page (80 calls), Unknown (4 calls)" in tile.group(1)
    apart = page.split("Not coding agents", 1)[1]
    assert "Page" in apart and "a visitor pressing a button on a page here" in apart and "Unknown" in apart
    assert "12 of them in visitors' sandboxes" in page and "every one of them in visitors' sandboxes" in page


# ------------------------------------------------------------------ portfolio


def test_the_projects_screen_reads_as_a_portfolio(tmp_path: Path) -> None:
    out = ops(
        r"""
  const list = [
    Object.assign({}, PROJECTS.projects[0], { project: 'Acme-Payments', source: 'fleet', would_refuse: 10, needs_review: 3 }),
    Object.assign({}, PROJECTS.projects[1], { project: 'Acme-Sandbox-0a1b2c3d', sandbox: true, would_refuse: 4, needs_review: 0 }),
    Object.assign({}, PROJECTS.projects[1], { project: 'Acme-Probe', source: 'other', would_refuse: 0, needs_review: 0 })
  ];
  answer = contract({ '/api/projects': { status: 200, body: { projects: list } } });
  await visit('#/projects');
  out.view = view();
  out.metrics = metrics(view());
""",
        tmp_path,
    )
    page = out["view"]
    assert out["metrics"]["projects"] == "3"
    assert 'data-src="fleet"' in page and 'data-src="sandbox"' in page and 'data-src="other"' in page
    assert ">Fleet<" in page and ">Sandbox<" in page and ">Other<" in page
    assert "7/10 labelled" in page and "4/4 labelled" in page and "nothing flagged" in page
    assert 'aria-label="7 of 10 flagged calls labelled"' in page
    assert 'data-row-href="#/projects/Acme-Payments"' in page, "A row opens its project"


# ---------------------------------------------------------------- review keys


KEYS = r"""
function press(key, extra) {
  const event = Object.assign({ key, target: el('view'), prevented: false,
    preventDefault() { this.prevented = true; }, stopImmediatePropagation() {} }, extra || {});
  (docListeners.keydown || []).forEach(fn => fn(event));
  return event;
}
function currentRow() {
  const chunk = view().split('<li id="qrow-').find(part => /^\d+"[^>]*data-current="true"/.test(part)) || '';
  return (chunk.match(/data-verdict="(VERDICT-\d+)"/) || [])[1] || null;
}
"""


def test_the_queue_is_worked_from_the_keyboard(tmp_path: Path) -> None:
    out = ops(
        KEYS
        + r"""
  const sent = [];
  const record = (u, i, body) => { sent.push(u.pathname.replace(/^\/prod/, '') + ' ' + body.items.map(x => x.verdict_id + ':' + x.label).join(',')); return { status: 200, body: { updated: body.items.length, skipped: [] } }; };
  answer = contract({
    '/api/decisions': { status: 200, body: { items: [
      row(1), row(2), row(3, { project_name: 'Acme-Catalog', rule_key: 'PROTECTED_PATH', observed_rules: ['PROTECTED_PATH'], target: '.claude/settings.json', observed_target: '.claude/settings.json' })
    ], next_cursor: null } },
    'POST /api/projects/Acme-Billing/reviews': record,
    'POST /api/projects/Acme-Catalog/reviews': record
  });
  await visit('#/review');
  out.legend = text(view().split('aria-label="Keyboard"')[1].split('</div>')[0]);
  out.start = currentRow();
  out.j = [press('j').prevented, currentRow()];
  press('c'); await tick();
  out.afterC = currentRow();
  out.progress = text(el('view').innerHTML.split('id="review-count"')[1].split('</p>')[0]);
  press('f'); await tick();
  press('k');
  out.k = currentRow();
  press('u'); await tick();
  out.afterU = (view().match(/data-action="label-one"/g) || []).length / 2;
  const before = sent.length;
  out.typing = press('c', { target: { tagName: 'INPUT', hasAttribute: () => false } }).prevented;
  out.modified = press('c', { ctrlKey: true }).prevented;
  await tick();
  out.ignored = sent.length === before;
  await visit('#/projects');
  press('c'); await tick();
  out.gone = sent.length === before;
  out.sent = sent;
""",
        tmp_path,
    )
    assert "J next" in out["legend"] and "C correct" in out["legend"] and "F false alarm" in out["legend"] and "U undo the last label" in out["legend"]
    assert out["start"] == "VERDICT-1", "The keyboard starts on the first row of the rule with the most waiting"
    assert out["j"] == [True, "VERDICT-2"], "J moves down and claims the key"
    assert out["afterC"] == "VERDICT-3", "Labelled, the row leaves and the next one takes its place"
    assert "1 call labelled on this visit" in out["progress"]
    assert out["k"] == "VERDICT-1"
    assert out["sent"] == [
        "/api/projects/Acme-Billing/reviews VERDICT-2:correct",
        "/api/projects/Acme-Catalog/reviews VERDICT-3:false_alarm",
        "/api/projects/Acme-Catalog/reviews VERDICT-3:clear",
    ], "C and F label the row the keyboard is on, each to its own project; U takes back the last"
    assert out["afterU"] == 2, "The undone call is back in the queue"
    assert out["typing"] is False and out["modified"] is False and out["ignored"], "A key typed in a field or with a modifier is left alone"
    assert out["gone"], "The keys leave with the screen"
