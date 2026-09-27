"""The operator's screens: what needs attention, what the numbers are made of, and the portfolio.

The overview leads with what an operator acts on today (the review queue, the
rules a false alarm made noisy, the sessions the circuit breaker halted), says
on the public demo what its numbers are made of, and counts the coding agents
apart from every other caller. The projects screen reads as a portfolio: each
project's stage, where its calls come from, and how ready it is to enforce.
Run under Node with the stub browser in _browser.py, with answers shaped as the
contracts in STATE.md fix them.

Every name here is synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import html
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


def text_of(markup: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup))


def ops(scenario: str, tmp_path: Path) -> dict:
    return run("dashboard.html", scenario, tmp_path, before=FIXTURES + OPS)


# ------------------------------------------------------- needs your attention


def test_the_overview_leads_with_what_needs_the_operator(tmp_path: Path) -> None:
    out = ops(
        r"""
  answer = contract({
    '/api/decisions': { status: 200, body: { items: [
      falseAlarm(1, 'Acme-Ledger', 'java-domain-stays-pure'), falseAlarm(2, 'Acme-Ledger', 'java-domain-stays-pure'),
      Object.assign(falseAlarm(3, 'Acme-Checkout', 'PROTECTED_PATH'), { status: 'BLOCKED_PROTECTED_PATH', observed_rules: [], observed_rule: '' }), row(4)
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
    # PROTECTED_PATH's false alarm is on a call it refused: a rule that bit wrong is the refused colour.
    assert re.search(r'data-tone="danger" data-slot="noisy"', page) and "refused a call marked false alarm" in words
    assert page.index("java-domain-stays-pure") < page.index("PROTECTED_PATH"), "The noisiest rule first"
    assert "https://example.test/prod/api/decisions?review=false_alarm&days=7&limit=200" in out["reads"]
    # Halted sessions, leaving out the demo page's own scenarios.
    assert "1 session halted" in words and "fleet-ledger-codex-1" in words and "started just now" in words
    assert "sim-0a1b2c3d" not in words and "fleet-ledger-codex-2" not in words
    assert "https://example.test/prod/sessions.html?session=fleet-ledger-codex-1" in page
    assert "https://example.test/prod/api/sessions?limit=200" in out["reads"]
    # The thirty-second refresh re-reads the overview, never the table scan.
    scans = lambda urls: sum(1 for u in urls if "/api/sessions" in u)
    assert scans(out["afterTimer"]) == scans(out["reads"]) == 1
    overviews = lambda urls: sum(1 for u in urls if "/api/overview" in u)
    assert overviews(out["afterTimer"]) == overviews(out["reads"]) + 1


def test_the_refresh_reads_the_false_alarms_with_the_numbers(tmp_path: Path) -> None:
    """A label set while the tab is open reaches the strip and the portfolio on the next refresh."""
    out = ops(
        r"""
  const body = overviewBody({ needs_review: 0 });
  body.by_project = [{ project: 'Acme-Search', stage: 'observe', configured: true, source: 'fleet', calls: 40, refused: 0, would_refuse: 5, needs_review: 0, last_seen: NOW }];
  let alarms = [];
  let failAlarms = false;
  const cell = () => view().split('data-row-href="#/projects/Acme-Search"')[1].split('</tr>')[0];
  answer = contract({
    '/api/overview': () => ({ status: 200, body }),
    '/api/decisions': () => (failAlarms ? 'network' : { status: 200, body: { items: alarms, next_cursor: null } }),
    '/api/sessions': { status: 200, body: { sessions: [] } }
  });
  await visit('#/overview?days=7');
  await tick();
  out.before = cell();
  out.sessionsNote = text(view().split('data-slot="halted"')[1].split('</article>')[0]);
  alarms = [falseAlarm(1, 'Acme-Search', 'python-domain-stays-pure')];
  await runIntervals();
  out.after = cell();
  out.noisy = text(view().split('data-slot="noisy"')[1].split('</article>')[0]);
  failAlarms = true;
  await runIntervals();
  out.failed = cell();
  out.stale = text(view().split('data-slot="noisy"')[1].split('</article>')[0]);
  out.reads = calls.map(c => c.url);
""",
        tmp_path,
    )
    assert 'data-state="ready"' in out["before"] and 'data-part="correct"' in out["before"]
    assert "Read at" in out["sessionsNote"] and "Refresh to read again" in out["sessionsNote"], "The sessions slot says when it was read"
    after = out["after"]
    assert 'data-state="noisy"' in after and "1 false alarm" in after, "A false alarm labelled since the first read turns the row Noisy"
    assert 'data-state="ready"' not in after
    assert "1 rule turned noisy" in out["noisy"] and "Acme-Search" in out["noisy"]
    failed = out["failed"]
    assert 'data-state="ready"' not in failed and 'data-part="correct"' not in failed, "A false-alarm read that failed claims no Ready"
    assert "the last re-read failed" in out["stale"], "The slot keeps what it read and says it is stale"
    assert sum(1 for u in out["reads"] if "review=false_alarm" in u) == 3, "Read once on opening and once with each refresh"
    assert sum(1 for u in out["reads"] if "/api/sessions" in u) == 1, "The timer never scans the session table"


def test_an_empty_window_starts_the_strip_once_calls_arrive(tmp_path: Path) -> None:
    out = ops(
        r"""
  let body = overviewBody({ calls: 0, approved: 0, refused: 0, would_refuse: 0, needs_review: 0 });
  answer = contract({
    '/api/overview': () => ({ status: 200, body }),
    '/api/decisions': { status: 200, body: { items: [falseAlarm(1, 'Acme-Ledger', 'LOOP')], next_cursor: null } },
    '/api/sessions': { status: 200, body: { sessions: [session('fleet-ledger-codex-1', 'Acme-Ledger', true)] } }
  });
  await visit('#/overview?days=7');
  await tick();
  out.emptyReads = calls.map(c => c.url).filter(u => u.indexOf('/api/sessions') !== -1 || u.indexOf('review=false_alarm') !== -1);
  body = overviewBody();
  await runIntervals();
  out.text = text(view());
""",
        tmp_path,
    )
    assert out["emptyReads"] == [], "An empty window reads nothing for the strip to show"
    assert "1 rule turned noisy" in out["text"] and "1 session halted" in out["text"], "Once calls arrive, the strip reads and says what it found"


def test_a_quiet_window_says_each_slot_is_clear(tmp_path: Path) -> None:
    out = ops(
        r"""
  answer = contract({
    '/api/overview': { status: 200, body: overviewBody({ needs_review: 0 }) },
    '/api/decisions': { status: 200, body: { items: [], next_cursor: null } },
    '/api/sessions': { status: 200, body: { sessions: Array.from({ length: 200 }, (_, i) => session('fleet-search-codex-' + i, 'Acme-Search', false)) } }
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
    assert "Among the newest 200 sessions read; the table holds more." in words, "A full read is not claimed as every session"
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
  answer = contract({ '/api/decisions': 'network', '/api/sessions': { status: 200, body: { sessions: [] } } });
  await visit('#/overview?days=14');
  await tick();
  out.network = text(view());
""",
        tmp_path,
    )
    words = out["text"]
    assert "Sessions could not be read" in words and "needs the operator" in words
    assert "No session is halted" not in words, "An unread list is not an empty one"
    assert "Counted over the newest 1 false alarm; the ledger holds more." in words
    assert "False alarms could not be read The service could not be reached. Nothing is shown rather than a guess." in out["network"]
    assert ".." not in out["network"], "The service's own full stop is not doubled"


def test_the_strip_leaves_out_what_halts_or_misfires_on_purpose(tmp_path: Path) -> None:
    out = ops(
        r"""
  const body = overviewBody({ needs_review: 25 });
  body.by_project = [
    { project: 'Acme-Sandbox-0a1b2c3d', stage: 'observe', configured: true, sandbox: true, calls: 12, refused: 0, would_refuse: 5, needs_review: 20, last_seen: NOW },
    { project: 'Acme-Ledger', stage: 'observe', configured: true, source: 'fleet', calls: 90, refused: 0, would_refuse: 9, needs_review: 5, last_seen: NOW }
  ];
  const resumed = [];
  let sessions = [session('probe-20260926a-loop-page', 'Acme-Core', true), session('sim-0a1b2c3d', 'Acme-Demo', true), session('fleet-ledger-codex-1', 'Acme-Ledger', true)];
  answer = contract({
    '/api/auth/whoami': PRIVATE,
    '/api/overview': { status: 200, body },
    '/api/decisions': { status: 200, body: { items: [
      falseAlarm(1, 'Acme-Sandbox-0a1b2c3d', 'python-domain-stays-pure'), falseAlarm(2, 'Acme-Sandbox-1a2b3c4d', 'python-domain-stays-pure'),
      falseAlarm(3, 'Acme-Ledger', 'LOOP')
    ], next_cursor: null } },
    '/api/sessions': () => ({ status: 200, body: { sessions } }),
    'POST /sessions/fleet-ledger-codex-1/resume': (u, i, b) => { resumed.push(b); sessions = sessions.map(s => Object.assign({}, s, { is_tripped: s.session_id.indexOf('fleet') === 0 ? false : s.is_tripped })); return { status: 200, body: { status: 'SESSION_RESUMED' } }; }
  });
  Threefold.whoami(true);
  await visit('#/overview?days=7');
  await tick();
  out.operator = view();
  out.reviews = text(view().split('data-slot="reviews"')[1].split('</article>')[0]);
  out.noisy = text(view().split('data-slot="noisy"')[1].split('</article>')[0]);
  out.halted = text(view().split('data-slot="halted"')[1].split('</article>')[0]);
  await click('resume-open', { 'data-session': 'fleet-ledger-codex-1' });
  el('dialog-first').value = '';
  el('resume-name').value = 'Ops on call';
  await click('resume-confirm', { 'data-session': 'fleet-ledger-codex-1' });
  out.unsaid = el('resume-error').textContent;
  el('dialog-first').value = 'The loop was a retry I stopped';
  await click('resume-confirm', { 'data-session': 'fleet-ledger-codex-1' });
  await tick();
  out.resumed = resumed;
  out.after = text(view().split('data-slot="halted"')[1].split('</article>')[0]);
  answer = contract({ '/api/auth/whoami': PUBLIC, '/api/overview': { status: 200, body },
    '/api/sessions': { status: 200, body: { sessions: [session('fleet-ledger-codex-1', 'Acme-Ledger', true)] } } });
  Threefold.whoami(true);
  await visit('#/overview?days=14');
  await tick();
  out.visitor = view();
""",
        tmp_path,
    )
    reviews, noisy, halted = out["reviews"], out["noisy"], out["halted"]
    assert reviews.index("Acme-Ledger") < reviews.index("Acme-Sandbox-0a1b2c3d"), "The operator's own projects lead a visitor's sandbox"
    assert "1 rule turned noisy" in noisy and "LOOP" in noisy and "python-domain-stays-pure" not in noisy
    assert 'data-slot="noisy"' in out["operator"] and re.search(r'data-tone="warn" data-slot="noisy"', out["operator"]), "A rule that only watched is flagged in the would-refuse colour"
    assert "None of its false alarms was refused: it only watched those calls." in noisy
    assert "refused nothing" not in noisy and "before it enforces" not in noisy, "A rule that refused other calls, or enforces now, is not told otherwise"
    assert "Left out: 2 false alarms in visitors' sandboxes, where the walkthrough asks for one." in noisy
    assert "1 session halted" in halted and "fleet-ledger-codex-1" in halted
    assert "probe-20260926a" not in halted and "sim-0a1b2c3d" not in halted, "The probes and the demo's scenarios halt on purpose"
    assert 'data-action="resume-open"' in out["operator"] and "until you resume it" in halted
    assert "Say why, and who you are" in out["unsaid"], "A resume with no reason sends nothing"
    assert out["resumed"] == [{"operator_name": "Ops on call", "reason": "The loop was a retry I stopped"}]
    assert "No session is halted" in out["after"], "After a resume, the strip reads the sessions again"
    visitor = out["visitor"]
    assert 'data-action="resume-open"' not in visitor and "until the operator resumes it" in visitor
    assert "Waiting for a label" in visitor and "Waiting for your label" not in visitor


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
    assert "Where these calls come from, on this public demo: 3,210 from the synthetic Acme fleet, 6 projects whose scheduled agents run through the real gates" in sources
    assert "; 96 from visitors' sandboxes" in sources
    assert "; 41 from other callers: the service's own probes, the demo page, or a repository connected to this stack." in sources
    split = re.sub(r"<[^>]+>", "", out["split"])
    assert 'data-sources="sandbox_split"' in out["split"]
    assert "96 from visitors' sandboxes; the other 3,251 from everything else: the synthetic Acme fleet where it runs, the service's own probes, the demo page, or a repository connected to this stack." in split
    for page in (sources, split):
        # Only the fleet is synthetic by contract: the page cannot know what
        # else reached a public stack, so it does not claim the rest is.
        assert "Everything on this public demo is synthetic" not in page
    assert "30 of them are in visitors' sandboxes, which anyone may label." in split, "The queue says how much of it is sandboxes'"
    assert "tf-ops-source" not in out["private"], "A private stack's numbers are the operator's own"


def _source_note(markup: str) -> str:
    """The overview's source banner alone, as markup."""
    found = re.search(r'<div class="tf-ops-source".*?(?=\s*<div class="space-y-8">)', markup, re.S)
    assert found, "The public overview says where its calls come from"
    return found.group(0)


def test_the_public_demo_names_the_daily_live_agent_as_real(tmp_path: Path) -> None:
    """The live agent's calls are real Claude Code or Codex runs, said in words beside a hue no other source uses."""
    out = ops(
        r"""
  const sources = { fleet: { calls: 3210, projects: 6 }, live: { calls: 12, projects: 2 }, sandbox: { calls: 96, projects: 8 }, other: { calls: 41, projects: 2 } };
  answer = contract({ '/api/auth/whoami': PUBLIC, '/api/overview': { status: 200, body: Object.assign(overviewBody(), { sources }) } });
  Threefold.whoami(true);
  await visit('#/overview?days=7');
  out.live = view();
  const zero = Object.assign({}, sources, { live: { calls: 0, projects: 0 } });
  answer = contract({ '/api/auth/whoami': PUBLIC, '/api/overview': { status: 200, body: Object.assign(overviewBody(), { sources: zero }) } });
  await visit('#/overview?days=14');
  out.zero = view();
  const older = { fleet: sources.fleet, sandbox: sources.sandbox, other: sources.other };
  answer = contract({ '/api/auth/whoami': PUBLIC, '/api/overview': { status: 200, body: Object.assign(overviewBody(), { sources: older }) } });
  await visit('#/overview?days=30');
  out.older = view();
  const quiet = { fleet: { calls: 0, projects: 0 }, live: { calls: 0, projects: 0 }, sandbox: { calls: 0, projects: 0 }, other: { calls: 0, projects: 0 } };
  answer = contract({ '/api/auth/whoami': PUBLIC, '/api/overview': { status: 200, body: Object.assign(overviewBody(), { sources: quiet }) } });
  await visit('#/overview?days=1');
  out.quiet = view();
""",
        tmp_path,
    )
    note = _source_note(out["live"])
    words = html.unescape(re.sub(r"<[^>]+>", "", note))
    assert ("Where these calls come from, on this public demo: 3,210 from the synthetic Acme fleet, 6 projects whose "
            "scheduled agents run through the real gates; 12 from the daily live agent: real Claude Code or Codex runs "
            "on Acme tasks, in projects that enforce; 96 from visitors' sandboxes; 41 from other callers") in words
    assert "Daily live agent, real" in words, "The legend names the hue in words"
    live_hue = "#d55181"
    assert f'data-tf-tip="Daily live agent, real" data-tf-tip-value="12" data-tf-tip-color="{live_hue}"' in note
    for other_hue in ("#8b5cf6", "#0891b2", "#4b5470"):
        assert f'data-tf-tip="Daily live agent, real" data-tf-tip-value="12" data-tf-tip-color="{other_hue}"' not in note
    assert note.count(live_hue) == 3, "The live hue is on its segment, its tooltip and its legend swatch, and on nothing else"
    assert 'aria-label="Acme fleet, synthetic 3,210, Daily live agent, real 12, Sandboxes 96, Other callers 41"' in note

    zero_note = _source_note(out["zero"])
    zero = html.unescape(re.sub(r"<[^>]+>", "", zero_note))
    assert "Claude Code" not in zero and "; 96 from visitors' sandboxes" in zero, "A live agent with no calls in the window is not claimed"
    assert 'data-tf-tip="Daily live agent, real"' not in out["zero"], "No segment is drawn for none"
    assert "Daily live agent" not in zero_note, "Nor is it in the legend or the bar's label"
    assert 'aria-label="Acme fleet, synthetic 3,210, Sandboxes 96, Other callers 41"' in zero_note

    older = _source_note(out["older"])
    assert 'data-sources="sources"' in older
    assert "live agent" not in older and "Claude Code" not in older and "null" not in older, \
        "A stack from before live counts those calls as other, and the page names no live agent it cannot count"
    assert "3,210 from the synthetic Acme fleet" in html.unescape(re.sub(r"<[^>]+>", "", older))

    quiet = html.unescape(re.sub(r"<[^>]+>", "", _source_note(out["quiet"])))
    assert ("On this public demo, calls come from the synthetic Acme fleet where it runs, the daily live agent's real "
            "Claude Code or Codex runs where it reports, visitors' sandboxes") in quiet


def test_the_live_agent_s_figures_are_counts_or_are_not_shown(tmp_path: Path) -> None:
    out = ops(
        r"""
  const EVIL = '<img src=x onerror=alert(1)>';
  const sources = { fleet: { calls: 3210, projects: 6 }, live: { calls: EVIL, projects: EVIL }, sandbox: { calls: 96, projects: 8 }, other: { calls: 41, projects: 2 } };
  answer = contract({ '/api/auth/whoami': PUBLIC, '/api/overview': { status: 200, body: Object.assign(overviewBody(), { sources }) } });
  Threefold.whoami(true);
  await visit('#/overview?days=7');
  out.hostile = view();
  const list = [Object.assign({}, PROJECTS.projects[1], { project: 'Acme-Live-orders-s3-archive', source: 'live' }),
                Object.assign({}, PROJECTS.projects[0], { project: 'Acme-Live-catalog-vat-regen', source: EVIL })];
  answer = contract({ '/api/projects': { status: 200, body: { projects: list } } });
  await visit('#/projects');
  out.projects = view();
""",
        tmp_path,
    )
    note = _source_note(out["hostile"])
    assert "<img" not in out["hostile"] and "onerror" not in note
    assert "live agent" not in note and "Claude Code" not in note, "A live part that is not a count is not named as one"
    assert "3,210 from the synthetic Acme fleet" in html.unescape(re.sub(r"<[^>]+>", "", note))
    assert "<img" not in out["projects"] and 'data-src="live"' in out["projects"]
    assert set(re.findall(r'data-src="([^"]*)"', out["projects"])) == {"live"}, "A source the page does not know gets no chip at all"


def test_a_live_part_that_is_not_a_whole_count_names_no_live_agent(tmp_path: Path) -> None:
    """2.5 would read as 3 real runs, and -4 as a legend entry: neither is a count, so the banner reads as a stack without live."""
    out = ops(
        r"""
  const busy = { fleet: { calls: 3210, projects: 6 }, sandbox: { calls: 96, projects: 8 }, other: { calls: 41, projects: 2 } };
  const quiet = { fleet: { calls: 0, projects: 0 }, sandbox: { calls: 0, projects: 0 }, other: { calls: 0, projects: 0 } };
  const overview = sources => ({ '/api/auth/whoami': PUBLIC, '/api/overview': { status: 200, body: Object.assign(overviewBody(), { sources }) } });
  answer = contract(overview(busy));
  Threefold.whoami(true);
  let n = 0;
  for (const [name, calls] of [['half', 2.5], ['negative', -4], ['nan', NaN], ['infinite', Infinity]]) {
    for (const [kind, rest] of [['busy', busy], ['quiet', quiet]]) {
      answer = contract(overview(Object.assign({}, rest, { live: { calls, projects: 1 } })));
      await visit('#/overview?days=' + (n++ % 2 ? 14 : 7));
      out[name + '-' + kind] = view();
    }
  }
""",
        tmp_path,
    )
    for value in ("half", "negative", "nan", "infinite"):
        busy = _source_note(out[f"{value}-busy"])
        words = html.unescape(re.sub(r"<[^>]+>", "", busy))
        assert "live agent" not in words and "Claude Code" not in words, f"live {value}: no clause names it"
        assert "Daily live agent" not in busy, f"live {value}: no segment, legend entry or label names it"
        assert 'aria-label="Acme fleet, synthetic 3,210, Sandboxes 96, Other callers 41"' in busy, value
        assert ("Where these calls come from, on this public demo: 3,210 from the synthetic Acme fleet, 6 projects whose "
                "scheduled agents run through the real gates; 96 from visitors' sandboxes; 41 from other callers") in words
        quiet = html.unescape(re.sub(r"<[^>]+>", "", _source_note(out[f"{value}-quiet"])))
        assert ("On this public demo, calls come from the synthetic Acme fleet where it runs, visitors' sandboxes") in quiet, \
            f"live {value}: the list of where calls can come from reads as a stack without live"


def test_a_live_project_carries_its_chip_in_the_portfolio(tmp_path: Path) -> None:
    out = ops(
        r"""
  const list = [
    Object.assign({}, PROJECTS.projects[0], { project: 'Acme-Payments', source: 'fleet' }),
    Object.assign({}, PROJECTS.projects[1], { project: 'Acme-Live-billing-credit-limit', source: 'live' }),
    Object.assign({}, PROJECTS.projects[1], { project: 'Acme-Probe', source: 'other' })
  ];
  answer = contract({ '/api/projects': { status: 200, body: { projects: list } } });
  await visit('#/projects');
  out.view = view();
""",
        tmp_path,
    )
    page = out["view"]
    chip = re.search(r'<span class="tf-chip tf-chip-gray tf-ops-src" data-src="live" title="([^"]*)">Live</span>', page)
    assert chip, "A live project's row says Live, in a word beside its dot"
    assert html.unescape(chip.group(1)) == "The daily live agent: a real Claude Code or Codex run on an Acme task, in a project that enforces"
    assert 'data-src="fleet"' in page and ">Fleet<" in page
    other = re.search(r'<span class="tf-chip tf-chip-gray tf-ops-src" data-src="other" title="([^"]*)">Other</span>', page)
    assert other and html.unescape(other.group(1)) == (
        "Not the fleet, the daily live agent or a sandbox: the service's own probes, the demo page, or a connected repository"
    ), "Other is what is left once all three named sources, the live agent's included, are set apart"
    style = (Path(__file__).resolve().parents[2] / "src" / "threefold" / "web" / "dashboard.html").read_text(encoding="utf-8")
    dots = dict(re.findall(r'\.tf-ops-src\[data-src="(\w+)"\]::before \{ background: var\((--tf-cat-[\w-]+)\); \}', style))
    assert dots == {"fleet": "--tf-cat-1", "live": "--tf-cat-3", "sandbox": "--tf-cat-2"},         "Each source has a categorical hue of its own, and other keeps the neutral one"


def test_a_visitor_is_offered_a_sandbox_rather_than_a_queue_they_cannot_work(tmp_path: Path) -> None:
    out = ops(
        r"""
  answer = contract({ '/api/auth/whoami': PUBLIC, '/api/decisions': { status: 200, body: { items: [], next_cursor: null } } });
  Threefold.whoami(true);
  await visit('#/overview?days=7');
  await tick();
  out.visitor = view().split('data-slot="reviews"')[1].split('</article>')[0];
  out.visitorText = text(out.visitor);
  answer = contract({ '/api/auth/whoami': PRIVATE, '/api/decisions': { status: 200, body: { items: [], next_cursor: null } } });
  Threefold.whoami(true);
  await visit('#/overview?days=14');
  await tick();
  out.operator = view().split('data-slot="reviews"')[1].split('</article>')[0];
  out.operatorText = text(out.operator);
""",
        tmp_path,
    )
    visitor, operator = out["visitorText"], out["operatorText"]
    assert "21 calls wait for a label" in visitor and "The other projects' labels are the operator's." in visitor
    assert 'href="#/try"' in out["visitor"] and "Try it with your own sandbox" in visitor
    assert "tf-kbd" not in out["visitor"], "No keys offered for labels a visitor could not set"
    assert "21 calls wait for your label" in operator and "Start reviewing" in operator
    assert "tf-ops-kbd-hint" in out["operator"], "The operator's keys are named, and hidden where there is no keyboard"


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
    # Acme-Probe's rules refused 7 calls and flagged none to watch: its rules
    # did act, so it is not "nothing flagged", and nothing waits for a label.
    assert "7/10 labelled" in page and "4/4 labelled" in page and "nothing waits for a label" in page
    assert 'aria-label="7 of 10 flagged calls labelled"' in page
    assert 'data-row-href="#/projects/Acme-Payments"' in page, "A row opens its project"


def test_a_projects_readiness_is_green_only_when_no_false_alarm_can_hide(tmp_path: Path) -> None:
    out = ops(
        r"""
  const list = [
    Object.assign({}, PROJECTS.projects[1], { project: 'Acme-Checkout', source: 'fleet', refused: 0, would_refuse: 2, needs_review: 0 }),
    Object.assign({}, PROJECTS.projects[1], { project: 'Acme-Payments', source: 'fleet', refused: 0, would_refuse: 5, needs_review: 0 }),
    Object.assign({}, PROJECTS.projects[1], { project: 'Acme-Search', source: 'fleet', refused: 0, would_refuse: 0, needs_review: 0 })
  ];
  const cell = name => view().split('data-row-href="#/projects/' + name + '"')[1].split('</tr>')[0];
  answer = contract({ '/api/projects': { status: 200, body: { projects: list } },
    '/api/decisions': { status: 200, body: { items: [falseAlarm(1, 'Acme-Checkout', 'python-domain-stays-pure')], next_cursor: null } } });
  await visit('#/projects');
  out.read = calls.map(c => c.url).filter(u => u.indexOf('review=false_alarm') !== -1);
  out.checkout = cell('Acme-Checkout'); out.payments = cell('Acme-Payments'); out.search = cell('Acme-Search');
  answer = contract({ '/api/projects': { status: 200, body: { projects: list } },
    '/api/decisions': { status: 200, body: { items: [], next_cursor: 'more' } } });
  await visit('#/overview');
  await visit('#/projects');
  out.partial = cell('Acme-Payments');
""",
        tmp_path,
    )
    assert out["read"] == ["https://example.test/prod/api/decisions?review=false_alarm&days=7&limit=200"]
    checkout = out["checkout"]
    assert 'data-state="noisy"' in checkout and ">Noisy<" in checkout, "One false alarm makes the project Noisy, however much is labelled"
    assert 'data-part="false_alarm"' in checkout and "2/2 labelled" in checkout and "1 false alarm" in checkout
    payments = out["payments"]
    assert 'data-state="ready"' in payments and 'data-part="correct"' in payments, "Read to the end with none, every label is correct: Ready"
    assert 'data-state="quiet"' in out["search"] and "nothing flagged" in out["search"]
    partial = out["partial"]
    assert 'data-part="correct"' not in partial and 'data-state="ready"' not in partial, "A read that stopped short claims no Ready"
    assert 'data-part="labelled"' in partial and "5/5 labelled" in partial


# -------------------------------------------------------------------- project


def test_the_stage_hero_is_a_headline_a_consequence_and_the_action(tmp_path: Path) -> None:
    out = ops(
        r"""
  answer = contract();
  await visit('#/projects/Acme-Billing');
  out.observe = view().split('class="tf-ops-hero"')[1].split('</section>')[0];
  const fresh = detailBody('observe', [{ rule_key: 'LOOP', kind: 'gate', mode_now: 'observe', would_refuse: 0, correct: 0, false_alarms: 0, unreviewed: 0, last_seen: null, state: 'quiet', recommendation: '' }]);
  Object.assign(fresh.readiness.summary, { reviewed: 0, false_alarms: 0, false_alarm_rate: 0, rules_ready: 0, rules_quiet: 1, rules_noisy: 0 });
  answer = contract({ '/api/projects/Acme-Billing': { status: 200, body: fresh } });
  await visit('#/projects/Acme-Billing?days=7');
  out.unlabelled = view().split('class="tf-ops-hero"')[1].split('</section>')[0];
""",
        tmp_path,
    )
    observe = out["observe"]
    lead, rest = observe.split('<p class="tf-ops-hero-text">', 1)[1].split("</p>", 1)
    # Ready and nothing to go on are said apart, as the promote dialog checks only the Ready ones.
    assert re.sub(r"<[^>]+>", "", lead) == "1 of 3 rules reads Ready, and 1 has nothing to go on yet; 1 call waits for a label."
    assert observe.count('class="tf-ops-hero-text"') == 1, "One line of consequence, not paragraphs"
    assert rest.index('data-action="promote-open"') < rest.index("<details"), "The action comes before how the stage works"
    assert "False-alarm rate 14% over 7 labelled calls." in observe
    unlabelled = out["unlabelled"]
    assert "No rule has anything to go on yet: none flagged an agent's call in the last 7 days." in unlabelled
    assert "Every rule reads Ready" not in unlabelled
    assert "No flagged call is labelled yet, so there is no false-alarm rate." in unlabelled
    assert "False-alarm rate" not in unlabelled and "over 0" not in unlabelled, "No rate is shown over nothing"


def test_a_stage_change_hands_the_focus_to_the_new_headline(tmp_path: Path) -> None:
    out = ops(
        r"""
  let stage = 'observe';
  let demotes = 0;
  const demote = held();
  answer = contract({
    '/api/projects/Acme-Billing': () => ({ status: 200, body: detailBody(stage) }),
    'POST /api/projects/Acme-Billing/promote': () => { stage = 'enforce'; return { status: 200, body: { stage } }; },
    'POST /api/projects/Acme-Billing/demote': () => { demotes += 1; return demote.promise.then(() => { stage = 'observe'; return { status: 200, body: { stage } }; }); }
  });
  await visit('#/projects/Acme-Billing');
  click('promote-open');
  await click('promote-confirm');
  await tick();
  out.afterPromote = document.activeElement && document.activeElement.id;
  out.hero = view().split('id="stage-title"')[1].split('</h2>')[0];
  click('demote-open');
  const pending = click('demote-confirm');
  await tick();
  out.busy = el('dialog-footer').innerHTML;
  click('demote-confirm');
  await tick();
  demote.release();
  await pending;
  await tick();
  out.demotes = demotes;
  out.afterDemote = document.activeElement && document.activeElement.id;
  out.observe = view().split('id="stage-title"')[1].split('</h2>')[0];
""",
        tmp_path,
    )
    assert out["afterPromote"] == "stage-title", "Focus lands on the new stage, not on the page"
    assert "In Enforce" in out["hero"]
    assert "Going back to Observe…" in out["busy"] and "disabled" in out["busy"], "Back to Observe says it is working"
    assert out["demotes"] == 1, "A second press while the first is out sends nothing"
    assert out["afterDemote"] == "stage-title" and "In Observe" in out["observe"]


def test_quiet_rules_that_did_nothing_share_one_row(tmp_path: Path) -> None:
    out = ops(
        r"""
  const quiet = (key, mode, refused) => ({ rule_key: key, kind: 'gate', mode_now: mode, would_refuse: 0, refused: refused || 0, correct: 0, false_alarms: 0, unreviewed: 0, last_seen: null, state: 'quiet', recommendation: 'Enforcing, and it flagged nothing in this window.' });
  const rules = [quiet('LOOP', 'enforce'), quiet('BUDGET', 'enforce'), quiet('UNREADABLE_WRITE', 'observe'), quiet('CREDENTIAL', 'enforce', 4),
    { rule_key: 'python-domain-stays-pure', kind: 'layering', mode_now: 'observe', would_refuse: 2, refused: 0, correct: 1, false_alarms: 1, unreviewed: 0, last_seen: NOW, state: 'noisy', recommendation: '1 false alarm(s): keep it observing, or refine the rule, before enforcing it.' }];
  answer = contract({ '/api/projects/Acme-Billing': { status: 200, body: detailBody('enforce', rules) } });
  await visit('#/projects/Acme-Billing');
  out.list = view().split('class="tf-ops-rules')[1].split('</section>')[0];
""",
        tmp_path,
    )
    rows = re.findall(r'<li class="tf-ops-rule[^"]*" data-state="([a-z_]+)"', out["list"])
    assert rows == ["noisy", "quiet", "quiet"], "The noisy rule, the quiet rule that refused, then one row for the rest"
    quiet = out["list"].split('class="tf-ops-rule tf-ops-rule-quiet"', 1)[1]
    assert 'data-rules="3"' in out["list"] and "3 rules flagged nothing" in quiet
    for key in ("LOOP", "BUDGET", "UNREADABLE_WRITE"):
        assert f"rule={key}" in quiet, f"{key} is named, and opens its calls"
    assert "CREDENTIAL" not in quiet, "A quiet rule that refused keeps its own row"
    assert "2 enforce and 1 observe" in re.sub(r"<[^>]+>", "", quiet)
    assert out["list"].count("Enforcing, and it flagged nothing in this window.") == 1, "The boilerplate is said once, not once a rule"


def test_a_project_page_names_where_its_calls_come_from(tmp_path: Path) -> None:
    """The chip beside the title is drawn from the project answer's own `source`, as its row in the listing is."""
    out = ops(
        r"""
  out.heads = {};
  const cases = [['Acme-Payments', 'fleet'], ['Acme-Live-billing-credit-limit', 'live'], ['Acme-Probe', 'probe'], ['Acme-Portal', 'other'], ['Acme-Billing', null]];
  for (const [name, source] of cases) {
    const body = Object.assign(detailBody('observe'), { project: name }, source ? { source } : {});
    answer = contract({ ['/api/projects/' + name]: { status: 200, body } });
    await visit('#/projects/' + name);
    out.heads[source || 'older'] = view().split('id="view-title"')[1].split('class="tf-ops-hero"')[0];
  }
  const box = Object.assign(detailBody('observe'), { project: 'Acme-Sandbox-0a1b2c3d', source: 'sandbox' });
  box.config = Object.assign({}, box.config, { sandbox: true });
  answer = contract({ '/api/projects/Acme-Sandbox-0a1b2c3d': { status: 200, body: box } });
  await visit('#/projects/Acme-Sandbox-0a1b2c3d');
  out.heads.sandbox = view().split('id="view-title"')[1].split('class="tf-ops-hero"')[0];
""",
        tmp_path,
    )
    heads = out["heads"]
    for source, word in (("fleet", ">Fleet<"), ("live", ">Live<"), ("probe", ">Probe, synthetic<"), ("other", ">Other<")):
        assert f'data-src="{source}"' in heads[source] and word in heads[source], f"A {source} project's page says so beside its title"
    assert "tf-ops-src" not in heads["older"], "A stack whose answer carries no source is not given one by the page"
    sandbox = heads["sandbox"]
    assert sandbox.count("tf-ops-src") == 1 and 'data-src="sandbox"' in sandbox and "Sandbox · expires within a day" in sandbox, \
        "A sandbox wears one chip, which says when it goes"


def test_the_agents_card_gives_a_hook_mode_only_to_calls_that_came_through_a_hook(tmp_path: Path) -> None:
    """The demo page's calls, and calls that did not say how they came, are not an older hook."""
    out = ops(
        r"""
  const items = [
    row(1, { origin: 'page', agent: 'page', hook_mode: 'unknown' }), row(2, { origin: 'page', agent: 'page', hook_mode: 'unknown' }),
    row(3, { origin: 'unknown', agent: 'unknown', hook_mode: 'unknown' }),
    row(4, { hook_mode: 'unknown' }), row(5)
  ];
  answer = contract({ '/api/decisions': { status: 200, body: { items, next_cursor: null } } });
  await visit('#/projects/Acme-Billing');
  out.agents = view().split('id="agents-body"')[1].split('</section>')[0];
  const probe = Object.assign(detailBody('observe'), { project: 'Acme-Probe', source: 'probe' });
  answer = contract({ '/api/projects/Acme-Probe': { status: 200, body: probe }, '/api/decisions': { status: 200, body: { items: items.slice(0, 2).map(r => Object.assign({}, r, { project_name: 'Acme-Probe' })), next_cursor: null } } });
  await visit('#/projects/Acme-Probe');
  out.probe = view().split('id="agents-body"')[1].split('</section>')[0];
""",
        tmp_path,
    )
    cards = out["agents"].split("<li ")[1:]
    words = [text_of(card) for card in cards]
    page = next(w for w in words if "The demo page" in w)
    assert "2 calls" in page and "no hook mode" in page and "always enforces" in page
    assert "older hook" not in page and "unknown" not in page, "A page call is not a hook too old to say its mode"
    other = next(w for w in words if "Unknown" in w)
    assert "Sent without saying how it arrived, so no hook mode is recorded." in other and "older hook" not in other
    claude = next(w for w in words if "Claude Code" in w)
    assert "managed" in claude and "an older hook" in claude, "A hook that did not report its mode is still said to be one"
    assert sum("older hook" in w for w in words) == 1
    assert "The live probes" in text_of(out["probe"]) and "The demo page" not in text_of(out["probe"]), \
        "On the probes' own project, their page calls are named as theirs"


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
  return (chunk.match(/data-row="(VERDICT-\d+)\|/) || [])[1] || null;
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


def test_a_held_key_labels_one_call_not_one_per_repeat(tmp_path: Path) -> None:
    out = ops(
        KEYS
        + r"""
  const sent = [];
  answer = contract({
    '/api/decisions': { status: 200, body: { items: [row(1), row(2), row(3), row(4)], next_cursor: null } },
    'POST /api/projects/Acme-Billing/reviews': (u, i, body) => { sent.push(body.items.map(x => x.verdict_id + ':' + x.label).join(',')); return { status: 200, body: { updated: body.items.length, skipped: [] } }; }
  });
  await visit('#/review');
  press('c'); await tick();
  out.repeats = [press('c', { repeat: true }), press('f', { repeat: true }), press('c', { repeat: true })].map(e => e.prevented);
  await tick();
  out.afterHeld = sent.slice();
  press('u'); await tick();
  press('u', { repeat: true }); await tick();
  out.afterUndo = sent.slice();
  press('j'); press('j', { repeat: true });
  out.walked = currentRow();
""",
        tmp_path,
    )
    assert out["afterHeld"] == ["VERDICT-1:correct"], "Holding C labels the call the key went down on, and no other"
    assert out["repeats"] == [True, True, True], "A repeat is still the queue's key, so the page does not scroll or type"
    assert out["afterUndo"] == ["VERDICT-1:correct", "VERDICT-1:clear"], "Holding U takes back one label"
    assert out["walked"] == "VERDICT-3", "A held J still walks the queue, as a held arrow would"


QUEUE = r"""
function flagged(i, project) {
  return row(i, { project_name: project, rule_key: 'python-domain-stays-pure', observed_rule: 'python-domain-stays-pure',
    observed_rules: ['python-domain-stays-pure'], target: 'src/acme/domain/order_' + i + '.py', observed_target: 'src/acme/domain/order_' + i + '.py' });
}
function groupOrder() { return (view().match(/<h3 id="group-\d+"[\s\S]*?font-mono tf-break">[^<]+/g) || []).map(h => h.replace(/[\s\S]*>/, '')); }
"""


def test_a_rule_says_its_sentence_once_and_each_call_what_differs(tmp_path: Path) -> None:
    """The queue reads by rule: the rule's sentence once, then each project's calls by file and import."""
    out = ops(
        KEYS
        + QUEUE
        + r"""
  const said = (file, module) => "Clean Architecture violation: Layering rule 'python-domain-stays-pure' refuses this write: A Python file under domain/ may not import infrastructure or a driver. '" + file + "' imports '" + module + "', which matches '" + module + "'";
  const layered = (i, project, module) => Object.assign(flagged(i, project), { observed_reason: said('src/acme/domain/order_' + i + '.py', module) });
  answer = contract({
    '/api/decisions': { status: 200, body: { items: [
      layered(1, 'Acme-Checkout', 'boto3'), layered(2, 'Acme-Checkout', 'sqlalchemy'), layered(3, 'Acme-Ledger', 'boto3'),
      row(4, { project_name: 'Acme-Ledger', rule_key: 'PROTECTED_PATH', observed_rules: ['PROTECTED_PATH'], observed_rule: 'PROTECTED_PATH', target: '.env', observed_target: '.env', observed_reason: "Command 'cat .env' reaches a protected path or credential store" })
    ], next_cursor: null } }
  });
  await visit('#/review');
  out.view = view();
  out.text = text(view());
  out.order = groupOrder();
  out.count = text(el('view').innerHTML.split('id="review-count"')[1].split('</p>')[0]);
""",
        tmp_path,
    )
    page, words = out["view"], out["text"]
    assert page.count('class="tf-card tf-ops-rsec"') == 2, "One section a rule, not one a project and rule"
    assert words.count("A Python file under domain/ may not import infrastructure or a driver.") == 1, "The rule's sentence is said once"
    assert "Layering rule 'python-domain-stays-pure' refuses this write" not in words, "The chip names the rule; its lead is not repeated"
    assert words.count("imports boto3") == 2 and "imports sqlalchemy" in words, "Each call says what differs: its import"
    assert "src/acme/domain/order_1.py' imports" not in words, "The file is on the row already"
    assert "Command 'cat .env' reaches a protected path or credential store" in words, "A lone call's reason is shown whole"
    assert out["order"] == ["Acme-Checkout", "Acme-Ledger", "Acme-Ledger"]
    assert "4 calls waiting in the last 7 days, under 2 rules in 2 projects" in out["count"]
    assert page.count('data-action="label-group"') == 2, "Bulk labels only where a project holds more than one call"


def test_the_keyboard_keeps_its_group_while_labels_change_the_counts(tmp_path: Path) -> None:
    """Labels shrink the group being worked below its neighbour; the group stays put, and so does the keyboard."""
    out = ops(
        KEYS
        + QUEUE
        + r"""
  const sent = [];
  const record = (u, i, body) => { sent.push(u.pathname.replace(/^\/prod\/api\/projects\//, '') + ' ' + body.items.map(x => x.verdict_id + ':' + x.label).join(',')); return { status: 200, body: { updated: body.items.length, skipped: [] } }; };
  answer = contract({
    '/api/decisions': { status: 200, body: { items: [1, 2, 3, 4, 5, 6].map(i => flagged(i, 'Acme-Checkout')).concat([7, 8, 9, 10].map(i => flagged(i, 'Acme-Mobile'))), next_cursor: null } },
    'POST /api/projects/Acme-Checkout/reviews': record,
    'POST /api/projects/Acme-Mobile/reviews': record
  });
  await visit('#/review');
  out.before = groupOrder();
  press('j');
  press('c'); await tick();
  press('c'); await tick();
  press('f'); await tick();
  out.after = groupOrder();
  out.current = currentRow();
  press('c'); await tick();
  out.sent = sent;
""",
        tmp_path,
    )
    assert out["before"] == ["Acme-Checkout", "Acme-Mobile"]
    assert out["after"] == ["Acme-Checkout", "Acme-Mobile"], "Three calls left in Checkout, four in Mobile: the groups keep their places"
    assert out["current"] == "VERDICT-5", "The keyboard stays on the next call of the group being worked"
    assert out["sent"] == [
        "Acme-Checkout/reviews VERDICT-2:correct",
        "Acme-Checkout/reviews VERDICT-3:correct",
        "Acme-Checkout/reviews VERDICT-4:false_alarm",
        "Acme-Checkout/reviews VERDICT-5:correct",
    ], "Every key labelled the call the reader was on, in the project they were reading"


def test_labelling_another_group_leaves_the_keyboard_where_it_is(tmp_path: Path) -> None:
    out = ops(
        KEYS
        + QUEUE
        + r"""
  answer = contract({
    '/api/decisions': { status: 200, body: { items: [1, 2, 3].map(i => flagged(i, 'Acme-Checkout')).concat([4, 5].map(i => flagged(i, 'Acme-Mobile'))), next_cursor: null } },
    'POST /api/projects/Acme-Checkout/reviews': (u, i, body) => ({ status: 200, body: { updated: body.items.length, skipped: [] } })
  });
  await visit('#/review');
  press('j'); press('j'); press('j');
  out.before = currentRow();
  await click('label-group', { 'data-group': JSON.stringify(['Acme-Checkout', 'python-domain-stays-pure']), 'data-label': 'correct' });
  await tick();
  out.after = currentRow();
""",
        tmp_path,
    )
    assert out["before"] == "VERDICT-4"
    assert out["after"] == "VERDICT-4", "A label set elsewhere does not move the keyboard"


def test_the_keyboard_gets_its_row_back_after_an_undo_and_a_rollback(tmp_path: Path) -> None:
    out = ops(
        KEYS
        + QUEUE
        + r"""
  let refuse = false;
  answer = contract({
    '/api/decisions': { status: 200, body: { items: [flagged(1, 'Acme-Checkout'), flagged(2, 'Acme-Checkout')], next_cursor: null } },
    'POST /api/projects/Acme-Checkout/reviews': (u, i, body) => refuse
      ? { status: 500, body: { detail: 'The table is unavailable.' } }
      : { status: 200, body: { updated: body.items.length, skipped: [] } }
  });
  const focused = () => {
    const id = (document.activeElement && document.activeElement.id) || '';
    const on = (view().match(/<li id="(qrow-\d+)"[^>]*data-current="true"/) || [])[1];
    return id && id === on ? currentRow() : 'focus on ' + (id || 'nothing');
  };
  await visit('#/review');
  press('c'); await tick();
  document.activeElement = null;
  press('u'); await tick();
  out.afterUndo = focused();
  refuse = true;
  press('j');
  press('c');
  document.activeElement = null;
  await tick();
  out.afterRollback = focused();
  out.error = el('review-error').textContent;
""",
        tmp_path,
    )
    assert out["afterUndo"] == "VERDICT-1", "U puts the call back and the keyboard, and the focus, on it"
    assert out["afterRollback"] == "VERDICT-2", "A refused label comes back with the focus on it"
    assert out["error"].endswith("The calls are back in the queue.") and ".." not in out["error"]


def test_a_note_is_asked_for_and_more_is_claimed_only_from_a_full_page(tmp_path: Path) -> None:
    out = ops(
        QUEUE
        + r"""
  answer = contract({ '/api/decisions': { status: 200, body: { items: [flagged(1, 'Acme-Checkout'), flagged(2, 'Acme-Checkout')], next_cursor: 'day-scan' } } });
  await visit('#/review');
  out.before = view();
  out.count = text(el('view').innerHTML.split('id="review-count"')[1].split('</p>')[0]);
  await click('note-open', { 'data-group': JSON.stringify(['Acme-Checkout', 'python-domain-stays-pure']) });
  out.after = view();
  out.focus = document.activeElement && document.activeElement.id;
""",
        tmp_path,
    )
    assert "Add a note to these labels" in out["before"] and 'id="note-0"' not in out["before"], "A note is a link until asked for"
    assert 'id="note-0"' in out["after"] and out["focus"] == "note-0", "Asked for, the field opens with the focus in it"
    assert "more beyond these" not in out["count"], "A cursor with a page that is not full does not say more calls wait"
    assert "Look further back" in out["before"] and "Load more" not in out["before"]


def test_a_visitor_is_told_which_groups_they_may_label(tmp_path: Path) -> None:
    out = ops(
        KEYS
        + QUEUE
        + r"""
  const sent = [];
  const record = (u, i, body) => { sent.push(u.pathname.replace(/^\/prod\/api\/projects\//, '')); return { status: 200, body: { updated: body.items.length, skipped: [] } }; };
  answer = contract({
    '/api/auth/whoami': PUBLIC,
    '/api/projects': { status: 200, body: { projects: PROJECTS.projects.concat([Object.assign({}, PROJECTS.projects[1], { project: 'Acme-Sandbox-0a1b2c3d', source: 'sandbox' })]) } },
    '/api/decisions': { status: 200, body: { items: [flagged(1, 'Acme-Checkout'), flagged(2, 'Acme-Checkout'), flagged(3, 'Acme-Sandbox-0a1b2c3d')], next_cursor: null } },
    'POST /api/projects/Acme-Checkout/reviews': record,
    'POST /api/projects/Acme-Sandbox-0a1b2c3d/reviews': record
  });
  Threefold.whoami(true);
  await visit('#/review');
  out.order = groupOrder();
  out.note = text(view().split('class="tf-ops-visitor')[1].split('class="tf-ops-keys')[0]);
  out.buttons = (view().match(/data-action="label-one"/g) || []).length;
  out.locked = view().split('</svg>Operator only</span>').length - 1;
  press('j');
  out.on = currentRow();
  press('c'); await tick();
  out.refused = el('review-error').textContent;
  out.stillThere = /qrow-1"[^>]*data-current="true"/.test(view());
  press('k');
  press('c'); await tick();
  out.sent = sent;
  answer = contract({ '/api/auth/whoami': PRIVATE, '/api/decisions': { status: 200, body: { items: [flagged(1, 'Acme-Checkout')], next_cursor: null } } });
  Threefold.whoami(true);
  await visit('#/review?days=7');
  out.operator = view();
""",
        tmp_path,
    )
    assert out["order"] == ["Acme-Sandbox-0a1b2c3d", "Acme-Checkout"], "What a visitor may label comes first"
    assert "You can label the 1 group from visitors' sandboxes, which come first." in out["note"]
    assert out["buttons"] == 2, "Only the sandbox's call carries Correct and False alarm"
    assert out["locked"] == 1 and out["on"] == "VERDICT-1"
    assert "is the operator's, so nothing was sent" in out["refused"] and out["stillThere"], "C on a project a visitor may not label sends nothing and removes nothing"
    assert out["sent"] == ["Acme-Sandbox-0a1b2c3d/reviews"]
    assert "tf-ops-visitor" not in out["operator"] and "Operator only" not in out["operator"]


def test_the_queue_counts_what_the_overview_counts(tmp_path: Path) -> None:
    """The queue reads the same week the overview does, and leaves out what the overview leaves out.

    A review of the live site found the overview saying 81 calls waited and
    the queue, over 30 days, 156: most of the difference was sandboxes whose
    stage had expired, which the overview and the project list no longer
    count. Other visitors' sandboxes, each with the same seeded calls, now
    fold into one line, and the reader's own sandbox leads.
    """
    out = ops(
        QUEUE
        + r"""
  const live = ['Acme-Sandbox-aaaaaaa1', 'Acme-Sandbox-aaaaaaa2', 'Acme-Sandbox-aaaaaaa3'];
  store['threefold-try'] = JSON.stringify({ project: 'Acme-Sandbox-aaaaaaa1', at: Date.now() });
  answer = contract({
    '/api/auth/whoami': PUBLIC,
    '/api/projects': { status: 200, body: { projects: PROJECTS.projects.concat(['Acme-Checkout'].concat(live).map(project => Object.assign({}, PROJECTS.projects[1], { project }))) } },
    '/api/decisions': { status: 200, body: { items: [
      flagged(1, 'Acme-Checkout'), flagged(2, 'Acme-Checkout'), flagged(3, 'Acme-Sandbox-aaaaaaa1'), flagged(4, 'Acme-Sandbox-aaaaaaa2'),
      flagged(5, 'Acme-Sandbox-aaaaaaa3'), flagged(6, 'Acme-Sandbox-dead0001'), flagged(7, 'Acme-Sandbox-dead0001')
    ], next_cursor: null } }
  });
  Threefold.whoami(true);
  await visit('#/review');
  out.read = calls.filter(c => c.url.indexOf('/api/decisions') !== -1).pop().url;
  out.order = groupOrder();
  out.text = text(view());
  out.view = view();
  await click('unfold'); await tick();
  out.unfolded = groupOrder();
  out.after = view();
""",
        tmp_path,
    )
    assert "days=7" in out["read"], "The queue reads the same week as every other screen"
    words = out["text"]
    assert "5 calls waiting in the last 7 days" in words
    assert "2 of them in 2 visitors' sandboxes, folded below" in words
    assert "Left out: 2 calls from visitors' sandboxes that have expired, which the overview no longer counts either." in words
    assert "Acme-Sandbox-dead0001" not in out["view"], "An expired sandbox's calls are not in the queue"
    assert out["order"] == ["Acme-Sandbox-aaaaaaa1", "Acme-Checkout"], "The reader's own sandbox leads; the others are folded"
    assert "Your own sandbox comes first: you can label its calls." in words
    assert 'data-fold="sandboxes"' in out["view"] and "Show them" in words
    assert "2 calls waiting in 2 sandboxes, under 1 rule." in words
    view = out["view"]
    assert view.index('data-fold="sandboxes"') < view.index(">Acme-Checkout<"), \
        "For a visitor, the line the other sandboxes fold into stands before the groups only the operator may label"
    assert out["unfolded"] == ["Acme-Sandbox-aaaaaaa1", "Acme-Sandbox-aaaaaaa2", "Acme-Sandbox-aaaaaaa3", "Acme-Checkout"], \
        "Shown, the folded sandboxes take the place of their line, and the groups above keep theirs"


def test_a_visitor_s_keyboard_reaches_the_sandboxes_they_may_label(tmp_path: Path) -> None:
    """Folded, the sandboxes are a place J and K stop at; shown, they come first, as the note says.

    A review found the note saying the sandboxes came first while Show them
    drew them last, and J and K walking only the rows a visitor may not label.
    """
    out = ops(
        KEYS
        + QUEUE
        + r"""
  const sent = [];
  const record = (u, i, body) => { sent.push(u.pathname.replace(/^\/prod\/api\/projects\//, '')); return { status: 200, body: { updated: body.items.length, skipped: [] } }; };
  const boxes = ['Acme-Sandbox-aaaaaaa2', 'Acme-Sandbox-aaaaaaa3'];
  const items = [flagged(1, 'Acme-Checkout'), flagged(2, 'Acme-Checkout'), flagged(3, boxes[0]), flagged(4, boxes[1])];
  answer = contract({
    '/api/auth/whoami': PUBLIC,
    '/api/projects': { status: 200, body: { projects: PROJECTS.projects.concat(['Acme-Checkout'].concat(boxes).map(project => Object.assign({}, PROJECTS.projects[1], { project }))) } },
    '/api/decisions': { status: 200, body: { items, next_cursor: null } },
    'POST /api/projects/Acme-Sandbox-aaaaaaa2/reviews': record
  });
  Threefold.whoami(true);
  await visit('#/review');
  const note = () => text(view().split('class="tf-ops-visitor')[1].split('font-medium">')[1].split('</p>')[0]);
  const onFold = () => /data-fold="sandboxes" data-current="true"/.test(view());
  out.first = { view: view(), note: note(), onFold: onFold() };
  press('c'); await tick();
  out.c = { focus: document.activeElement && document.activeElement.id, sent: sent.length, said: el('live-status').textContent };
  press('j');
  out.j = { row: currentRow(), onFold: onFold() };
  press('k');
  out.k = { focus: document.activeElement && document.activeElement.id, onFold: onFold() };
  await click('unfold'); await tick();
  out.shown = { order: groupOrder(), row: currentRow(), note: note(), folded: view().indexOf('data-fold=') !== -1 };
  press('c'); await tick();
  out.sent = sent;
  answer = contract({
    '/api/auth/whoami': PRIVATE,
    '/api/projects': { status: 200, body: { projects: PROJECTS.projects.concat(['Acme-Checkout'].concat(boxes).map(project => Object.assign({}, PROJECTS.projects[1], { project }))) } },
    '/api/decisions': { status: 200, body: { items: [flagged(1, 'Acme-Checkout'), flagged(3, boxes[0]), flagged(4, boxes[1])], next_cursor: null } }
  });
  Threefold.whoami(true);
  await visit('#/review?days=7');
  out.operator = { view: view(), start: currentRow() };
  press('j');
  out.operator.j = { focus: document.activeElement && document.activeElement.id, onFold: onFold() };
""",
        tmp_path,
    )
    first = out["first"]
    assert first["view"].index('data-fold="sandboxes"') < first["view"].index(">Acme-Checkout<"), "The folded line stands first"
    assert first["onFold"], "The keyboard starts on the one place a visitor can act from"
    assert first["note"] == "You can label the calls in visitors' sandboxes, folded into one line at the top of the queue: Show them, then label."
    assert out["c"]["sent"] == 0 and out["c"]["focus"] == "fold-show", "C on the folded line labels nothing and goes to Show them"
    assert "Press Enter to show them" in out["c"]["said"]
    assert out["j"] == {"row": "VERDICT-1", "onFold": False}, "J moves on to the next row"
    assert out["k"] == {"focus": "fold-show", "onFold": True}, "K comes back to the folded line"
    shown = out["shown"]
    assert shown["order"] == ["Acme-Sandbox-aaaaaaa2", "Acme-Sandbox-aaaaaaa3", "Acme-Checkout"] and not shown["folded"]
    assert shown["note"] == "You can label the 2 groups from visitors' sandboxes, which come first.", "The note is true of the order"
    assert shown["row"] == "VERDICT-3", "Shown, the keyboard is on the first call it can label"
    assert out["sent"] == ["Acme-Sandbox-aaaaaaa2/reviews"]
    operator = out["operator"]
    assert operator["view"].index(">Acme-Checkout<") < operator["view"].index('data-fold="sandboxes"'), \
        "For the operator, the visitors' sandboxes follow the projects that are theirs"
    assert operator["start"] == "VERDICT-1" and operator["j"] == {"focus": "fold-show", "onFold": True}, \
        "The operator's J reaches the folded line after the last row"


def test_a_visitor_s_first_j_lands_on_show_them_not_past_it(tmp_path: Path) -> None:
    """The queue first draws with the folded line current and the focus on the title; the first J goes to Show them.

    A review found the first J stepping past the folded line to a row only the
    operator may label, while the line showed an Enter hint the focus did not
    back. The hint is drawn only while Show them holds the focus (the style
    below), and the first J takes the reader there.
    """
    out = ops(
        KEYS
        + QUEUE
        + r"""
  const boxes = ['Acme-Sandbox-aaaaaaa2', 'Acme-Sandbox-aaaaaaa3'];
  answer = contract({
    '/api/auth/whoami': PUBLIC,
    '/api/projects': { status: 200, body: { projects: PROJECTS.projects.concat(['Acme-Checkout'].concat(boxes).map(project => Object.assign({}, PROJECTS.projects[1], { project }))) } },
    '/api/decisions': { status: 200, body: { items: [flagged(1, 'Acme-Checkout'), flagged(2, 'Acme-Checkout'), flagged(3, boxes[0]), flagged(4, boxes[1])], next_cursor: null } }
  });
  Threefold.whoami(true);
  await visit('#/review');
  const onFold = () => /data-fold="sandboxes" data-current="true"/.test(view());
  const focus = () => document.activeElement && document.activeElement.id;
  out.load = { focus: focus(), onFold: onFold() };
  press('j');
  out.j1 = { focus: focus(), onFold: onFold(), row: currentRow(), said: el('live-status').textContent };
  press('j');
  out.j2 = { focus: focus(), onFold: onFold(), row: currentRow() };
""",
        tmp_path,
    )
    assert out["load"] == {"focus": "view-title", "onFold": True}, "The queue draws with the title focused and the folded line current"
    assert out["j1"]["focus"] == "fold-show" and out["j1"]["onFold"] and out["j1"]["row"] is None, \
        "The first J lands on Show them, not past it"
    assert "Press Enter to show them" in out["j1"]["said"]
    assert out["j2"] == {"focus": "qrow-0", "onFold": False, "row": "VERDICT-1"}, "The next J moves on to the first row"
    from _browser import page_source
    page = page_source("dashboard.html")
    assert "#fold-show:not(:focus) .tf-ops-kbd-hint { display: none; }" in page, \
        "The Enter hint shows only while Show them holds the focus"


def test_a_name_with_no_call_and_no_configuration_is_not_a_project_to_promote(tmp_path: Path) -> None:
    """A mistyped or old name drew as a project in Observe, every rule Quiet, with Promote on offer."""
    out = ops(
        r"""
  const detail = detailBody('observe', RULES.map(r => Object.assign({}, r, { would_refuse: 0, correct: 0, false_alarms: 0, unreviewed: 0, state: 'quiet' })));
  detail.project = 'Acme-Nope-Missing';
  detail.config = null;
  detail.readiness.summary = Object.assign({}, detail.readiness.summary, { calls_observed: 0, days_observed: 0, would_have_refused: 0, reviewed: 0 });
  answer = contract({ '/api/projects/Acme-Nope-Missing': { status: 200, body: detail }, '/api/decisions': { status: 200, body: { items: [], next_cursor: null } } });
  await visit('#/projects/Acme-Nope-Missing');
  await tick();
  out.view = view();
  // The same readiness with a call from a page in the window: a project.
  answer = contract({ '/api/projects/Acme-Nope-Missing': { status: 200, body: detail }, '/api/decisions': { status: 200, body: { items: [row(1, { project_name: 'Acme-Nope-Missing', origin: 'page', agent: 'page' })], next_cursor: null } } });
  await visit('#/projects/Acme-Nope-Missing?days=7');
  await tick();
  out.called = view();
""",
        tmp_path,
    )
    page = out["view"]
    assert "No call from this project in the last 14 days" in page
    assert "Connect it, or check the name." in page
    assert 'data-action="promote-open"' not in page, "There is nothing to promote"
    assert 'href="#/connect"' in page and 'href="#/projects"' in page
    assert "No call from this project" not in out["called"] and 'data-action="promote-open"' in out["called"], "A project with calls is drawn as one"


def test_a_visitor_sees_the_stage_and_label_controls_off_where_only_the_operator_may_act(tmp_path: Path) -> None:
    """Promote and the labels are drawn off, with why, before anything is pressed.

    On a fleet project an anonymous visitor could open the promotion dialog,
    press Promote, and only then read that it needs the operator; the Review
    screen already said so up front.
    """
    out = ops(
        r"""
  answer = contract({ '/api/auth/whoami': PUBLIC });
  Threefold.whoami(true);
  await visit('#/projects/Acme-Billing');
  out.project = view();
  await click('promote-open'); await tick();
  out.dialog = el('modal-root').innerHTML;
  await visit('#/call?timestamp=t&verdict_id=VERDICT-1');
  out.call = view();
  answer = contract({ '/api/auth/whoami': PRIVATE });
  Threefold.whoami(true);
  await visit('#/projects/Acme-Billing');
  out.operator = view();
""",
        tmp_path,
    )
    project = out["project"]
    assert re.search(r'data-action="promote-open"[^>]*disabled', project), "Promote is off for a visitor"
    assert "Operator only" in project and "Sign in to promote" in project and 'href="#/try"' in project
    assert "tf-dialog" not in out["dialog"], "The dialog does not open for a visitor"
    call = out["call"]
    assert re.search(r'data-action="call-label" data-label="correct"[^>]*disabled', call)
    assert "Sign in to label" in call and "Operator only" in call
    assert not re.search(r'data-action="promote-open"[^>]*disabled', out["operator"]) and "Operator only" not in out["operator"]


def test_an_observe_project_says_why_it_still_shows_refused_calls(tmp_path: Path) -> None:
    """A project in Observe refuses no agent's call; its refused calls say how they arrived.

    The screen said "refuse nothing" beside a rule that had refused 74 calls,
    every one from the demo page, which always enforces.
    """
    out = ops(
        r"""
  const rules = [Object.assign({}, RULES[0], { mode_now: 'observe', refused: 3 })].concat(RULES.slice(1));
  answer = contract({
    '/api/projects/Acme-Billing': { status: 200, body: detailBody('observe', rules) },
    '/api/decisions': u => u.searchParams.get('kind') === 'refused'
      ? { status: 200, body: { items: [1, 2, 3].map(i => row(i, { status: 'BLOCKED_BOUNDARY_VIOLATION', origin: 'page', agent: 'page', observed_rules: [], observed_rule: '' })), next_cursor: null } }
      : { status: 200, body: { items: [row(4)], next_cursor: null } }
  });
  await visit('#/projects/Acme-Billing');
  await tick();
  out.text = text(view());
  answer = contract();
  await visit('#/projects');
  out.projects = text(view());
""",
        tmp_path,
    )
    words = re.sub(r"\s+([,.:)])", r"\1", out["text"])
    assert "In Observe: agents' calls are recorded, never refused" in words
    assert "3 calls were refused here in the last 14 days all the same: all 3 from the demo page, which always enforces." in words
    assert "3 refused (all 3 from the demo page)" in words
    projects = out["projects"]
    assert "agents' calls recorded, never refused" in projects
    assert "Calls from the demo page always enforce, and a credential is always refused, so a project in Observe can still show refused calls." in projects


def test_a_rule_whose_only_record_is_refusals_nobody_labelled_reads_quiet_never_ready(tmp_path: Path) -> None:
    """A rule whose only record is refusals nobody labelled is Quiet on every screen, never Ready.

    The service's probe project read "Ready" with "Every call it flagged was
    marked correct" beside 0 correct, 0 flagged and 6 refused, all 6 from the
    demo page, which always enforces; and the hero said "Every rule reads
    Ready or Quiet: promote" beside 0 calls observed. The service now reads
    such a rule Quiet, with a sentence that claims no label. The chip, its
    title, the sentence, the tally, the headline, the promote dialog and the
    portfolio say the same, whether the refusals came from the demo page or
    from an agent's hook while the project enforced, and nothing reads Ready
    while a read is on its way.
    """
    out = ops(
        r"""
  const probe = { rule_key: 'python-domain-stays-pure', kind: 'layering', mode_now: 'observe', would_refuse: 0, correct: 0, false_alarms: 0, unreviewed: 0,
    refused: 6, last_seen: NOW, state: 'quiet', recommendation: 'Nobody labelled the 6 call(s) it refused in this window, so nothing here shows it is ready to enforce.' };
  const quiet = { rule_key: 'LOOP', kind: 'gate', mode_now: 'observe', would_refuse: 0, correct: 0, false_alarms: 0, unreviewed: 0, refused: 0, last_seen: null, state: 'quiet', recommendation: 'Flagged nothing in this window, so enforcing it would have refused nothing seen here.' };
  const body = detailBody('observe', [probe, quiet]);
  body.config = null;
  Object.assign(body.readiness.summary, { calls_observed: 0, days_observed: 0, would_have_refused: 0, reviewed: 0, false_alarms: 0, false_alarm_rate: 0, rules_ready: 0, rules_quiet: 2, rules_noisy: 0 });
  const pageRefusals = { status: 200, body: { items: [1, 2, 3, 4, 5, 6].map(i => row(i, { status: 'BLOCKED_BOUNDARY_VIOLATION', origin: 'page', agent: 'page', rule_key: 'python-domain-stays-pure', observed_rules: [], observed_rule: '' })), next_cursor: null } };
  const refusedRead = held();
  answer = contract({
    '/api/auth/whoami': PRIVATE,
    '/api/projects/Acme-Probe': { status: 200, body },
    '/api/decisions': u => u.searchParams.get('kind') === 'refused' ? refusedRead.promise.then(() => pageRefusals) : { status: 200, body: { items: [row(7, { project_name: 'Acme-Probe', agent: 'page', origin: 'page' })], next_cursor: null } }
  });
  Threefold.whoami(true);
  await visit('#/projects/Acme-Probe');
  out.reading = view();
  refusedRead.release();
  await tick();
  out.read = view();
  click('promote-open');
  out.dialog = el('modal-root').innerHTML;
  out.checked = {};
  out.dialog.replace(/data-rule="([^"]+)"\s*(checked)?/g, (m, rule, checked) => { out.checked[rule] = !!checked; return m; });
  click('close-dialog');
  // The same rule refused an agent's hook calls while the project enforced, and nobody labelled one: still Quiet.
  const hooked = { status: 200, body: { items: [1, 2, 3, 4, 5, 6].map(i => row(i, { status: 'BLOCKED_BOUNDARY_VIOLATION', origin: 'hook', rule_key: 'python-domain-stays-pure', observed_rules: [], observed_rule: '' })), next_cursor: null } };
  const enforcing = detailBody('enforce', [Object.assign({}, probe, { mode_now: 'enforce', recommendation: 'Enforcing, and nobody labelled the 6 call(s) it refused in this window, so nothing here shows it was right.' }), quiet]);
  answer = contract({
    '/api/auth/whoami': PRIVATE,
    '/api/projects/Acme-Probe': { status: 200, body: enforcing },
    '/api/decisions': u => u.searchParams.get('kind') === 'refused' ? hooked : { status: 200, body: { items: [row(8, { project_name: 'Acme-Probe' })], next_cursor: null } }
  });
  await visit('#/projects/Acme-Probe?days=7');
  await tick();
  out.hooked = view();
  click('promote-open');
  out.hookedDialog = el('modal-root').innerHTML;
  click('close-dialog');
  // Agents' calls observed, every rule Quiet, one with refusals nobody labelled: the headline does not say none flagged.
  const quietBody = detailBody('observe', [probe, quiet]);
  Object.assign(quietBody.readiness.summary, { calls_observed: 5, reviewed: 0, false_alarms: 0, false_alarm_rate: 0, rules_ready: 0, rules_quiet: 2, rules_noisy: 0 });
  answer = contract({ '/api/auth/whoami': PRIVATE, '/api/projects/Acme-Probe': { status: 200, body: quietBody },
    '/api/decisions': { status: 200, body: { items: [], next_cursor: null } } });
  await visit('#/projects/Acme-Probe?days=30');
  await tick();
  out.quietHero = view().split('class="tf-ops-hero"')[1].split('</section>')[0];
  // The portfolio: a row that refused calls and would have refused none is Quiet once the false alarms are read,
  // unsaid while they are read, Noisy if one of its refusals was marked a false alarm, and never Ready.
  const list = { projects: [
    { project: 'Acme-Probe', stage: 'observe', configured: false, observe_rules: [], created_at: null, promoted_at: null, last_seen: NOW, calls: 6, refused: 6, would_refuse: 0, needs_review: 0, agents: ['page'], hook_modes: ['unknown'] },
    { project: 'Acme-Ledger', stage: 'enforce', configured: true, observe_rules: [], created_at: NOW, promoted_at: NOW, last_seen: NOW, calls: 40, refused: 5, would_refuse: 0, needs_review: 0, agents: ['claude-code'], hook_modes: ['managed'] }
  ] };
  const alarmsRead = held();
  answer = contract({ '/api/projects': { status: 200, body: list },
    '/api/decisions': u => alarmsRead.promise.then(() => ({ status: 200, body: { items: [], next_cursor: null } })) });
  await visit('#/projects');
  out.portfolioReading = view();
  alarmsRead.release();
  await tick();
  out.portfolio = view();
  answer = contract({ '/api/projects': { status: 200, body: list },
    '/api/decisions': { status: 200, body: { items: [Object.assign(falseAlarm(1, 'Acme-Ledger', 'PROTECTED_PATH'), { status: 'BLOCKED_PROTECTED_PATH', observed_rules: [], observed_rule: '' })], next_cursor: null } } });
  await visit('#/overview');
  await visit('#/projects');
  await tick();
  out.portfolioNoisy = view();
""",
        tmp_path,
    )
    reading = out["reading"]
    rule = reading.split('data-state="quiet"')[1].split("</li>")[0]
    assert ">Quiet<" in rule and ">Ready<" not in rule and "tf-ops-chip-wait" not in rule, "The service's state is drawn at once"
    assert "Every call it flagged was marked correct" not in reading
    read = out["read"]
    assert 'data-state="ready"' not in read and ">Ready<" not in read.split('class="tf-ops-rules')[1]
    row = read.split('data-state="quiet"')[1].split("</li>")[0]
    assert 'title="Quiet: no label says anything about the calls it refused"' in row, "A Quiet rule that refused calls is not said to have flagged nothing"
    said = text_of(row).replace("&#039;", "'")
    assert "Nobody labelled the 6 calls it refused in this window, so nothing here shows it is ready to enforce." in said
    assert "6 refused (all 6 from the demo page) in the last 14 days" in re.sub(r"\s+([,.:)])", r"\1", said)
    assert "Flagged nothing" not in said and "marked correct" not in said, "A rule that refused calls flagged them, and nobody labelled one"
    hero = read.split('class="tf-ops-hero"')[1].split("</section>")[0]
    lead = re.sub(r"<[^>]+>", "", hero.split('<p class="tf-ops-hero-text">', 1)[1].split("</p>", 1)[0])
    assert lead == "No agent's call was observed here in the last 14 days, so no rule has anything to go on yet."
    tally = dict(re.findall(r'data-tally="([a-z_]+)"><b[^>]*>([^<]*)</b>', hero))
    assert tally == {"ready": "0", "quiet": "2", "needs_review": "0", "noisy": "0"}, "The tally counts the rules as their rows state them"
    assert out["checked"] == {"python-domain-stays-pure": False, "LOOP": False}, "A Quiet rule is not checked for the reader"
    dialog = re.sub(r"\s+([,.:;])", r"\1", text_of(out["dialog"])).replace("&#039;", "'")
    assert "No rule reads Ready, so none is checked for you." in dialog
    untried = out["dialog"].split('data-group="untried"')[1]
    assert 'data-rule="LOOP"' in untried and 'data-rule="python-domain-stays-pure"' not in untried, "A rule that refused calls did flag them"
    assert "Flagged nothing here No call here tested this rule; check one to enforce it anyway." in dialog
    assert "0 flagged · 6 refused · 0 correct" in dialog and "Nobody labelled the 6 calls it refused" in dialog
    hooked = out["hooked"]
    assert 'data-state="ready"' not in hooked and ">Ready<" not in hooked.split('class="tf-ops-rules')[1], "An agent's refusals, unlabelled, make no rule Ready"
    hooked_row = text_of(hooked.split('data-state="quiet"')[1].split("</li>")[0])
    assert "Enforcing, and nobody labelled the 6 calls it refused in this window, so nothing here shows it was right." in hooked_row
    assert "Flagged nothing here" in text_of(out["hookedDialog"]) and 'data-rule="python-domain-stays-pure"' not in out["hookedDialog"].split('data-group="untried"')[1]
    quiet_lead = re.sub(r"<[^>]+>", "", out["quietHero"].split('<p class="tf-ops-hero-text">', 1)[1].split("</p>", 1)[0])
    assert quiet_lead == (
        "No rule has anything to go on yet: none flagged a call to review in the last 30 days, and nobody labelled a call "
        "one of them refused."
    ), "A rule that refused calls is not said to have flagged none"

    def cell(view: str, name: str) -> str:
        return view.split('data-row-href="#/projects/' + name + '"')[1].split("</tr>")[0]

    for name in ("Acme-Probe", "Acme-Ledger"):
        waiting = cell(out["portfolioReading"], name)
        assert 'data-state="unknown"' in waiting and ">Quiet<" not in waiting and ">Ready<" not in waiting, f"{name}: nothing is claimed while the false alarms are read"
    probe_cell = cell(out["portfolio"], "Acme-Probe")
    assert 'data-state="quiet"' in probe_cell and ">Quiet<" in probe_cell and "demo-page calls only" in probe_cell
    assert 'title="Quiet: no label says anything about the calls it refused"' in probe_cell
    ledger = cell(out["portfolio"], "Acme-Ledger")
    assert 'data-state="quiet"' in ledger and "nothing waits for a label" in ledger
    assert 'data-state="ready"' not in out["portfolio"] and ">Ready<" not in out["portfolio"], "Refusals alone make no project Ready"
    assert 'data-state="noisy"' in cell(out["portfolioNoisy"], "Acme-Ledger"), "A refusal marked a false alarm still makes a project Noisy"


def test_the_service_s_probes_are_labelled_synthetic_where_the_stack_names_them(tmp_path: Path) -> None:
    """A probe project is a synthetic source of its own: named so, drawn so, and not work for the operator."""
    out = ops(
        r"""
  const sources = { fleet: { calls: 3210, projects: 6 }, sandbox: { calls: 96, projects: 8 }, probe: { calls: 40, projects: 1 }, other: { calls: 41, projects: 2 } };
  const body = Object.assign(overviewBody(), { sources });
  body.by_project = body.by_project.concat([{ project: 'Acme-Probe', source: 'probe', stage: 'observe', configured: false, calls: 40, refused: 30, would_refuse: 36, needs_review: 36, last_seen: NOW }]);
  answer = contract({ '/api/auth/whoami': PUBLIC, '/api/overview': { status: 200, body } });
  Threefold.whoami(true);
  await visit('#/overview?days=7');
  await tick();
  out.view = view();
  // A public stack that does not run the fleet: it reports probe as 0, and Acme-Probe's calls are other.
  const noFleet = Object.assign(overviewBody(), { sources: { fleet: { calls: 0, projects: 0 }, sandbox: { calls: 96, projects: 8 }, probe: { calls: 0, projects: 0 }, other: { calls: 81, projects: 3 } } });
  answer = contract({ '/api/auth/whoami': PUBLIC, '/api/overview': { status: 200, body: noFleet } });
  await visit('#/overview?days=14');
  await tick();
  out.noFleet = view();
""",
        tmp_path,
    )
    no_fleet = html.unescape(re.sub(r"<[^>]+>", "", out["noFleet"]))
    assert "81 from other callers: the service's own probes, the demo page, or a repository connected to this stack" in no_fleet, (
        "Where the fleet does not run, the probes are among the other callers"
    )
    page = out["view"]
    words = html.unescape(re.sub(r"<[^>]+>", "", page))
    assert "40 from the service's own probes, synthetic" in words
    assert "41 from other callers: the demo page, or a repository connected to this stack" in words, "Other no longer claims the probes"
    assert "Probes, synthetic" in words, "The legend names the probes"
    reviews = page.split('data-slot="reviews"')[1].split("</article>")[0]
    assert "Acme-Probe" not in reviews and "Left out: 36 calls in the service's own probes, which are synthetic." in html.unescape(re.sub(r"<[^>]+>", "", reviews))
    table = page.split("By project")[1]
    assert 'data-src="probe"' in table and "Probe, synthetic" in table
    assert table.index("Acme-Probe") > table.index("Acme-Catalog"), "The probe comes after the projects someone works in"


def test_a_reason_the_ledger_cut_says_so_and_the_record_names_its_extra_fields(tmp_path: Path) -> None:
    """A reason cut at 240 characters ends in an ellipsis and a note, not mid-word; repeated fields are not listed twice."""
    out = ops(
        r"""
  const cut = ('The rule found an import ' + 'x'.repeat(300)).slice(0, 240);
  const decision = Object.assign({}, DECISION, { decision: row(1, { observed_reason: cut, category: 'LAYERING', model: 'default', surprise: 'kept' }) });
  answer = contract({ '/api/decision': { status: 200, body: decision } });
  await visit('#/call?timestamp=t&verdict_id=VERDICT-1');
  out.view = view();
""",
        tmp_path,
    )
    page = out["view"]
    assert page.count("cut at 240 characters by the ledger") == 2, "The hero and the record each say the reason was cut"
    assert "xxxx…" in page
    assert "Model default" in text_of(page), "A field the list does not name, but the page knows, is named"
    assert ">category</dt>" not in page.replace("\n", "") and ">observed_rule</dt>" not in page.replace("\n", ""), "A field that repeats a listed one is left out"
    assert "Other fields the ledger returned (1)" in page and "surprise" in page


# ---------------------------------------------------------------------- proof


def test_the_proof_page_leads_with_the_violation_rates_on_one_scale(tmp_path: Path) -> None:
    import json

    from test_the_proof_page import COMMITTED, PROOF_FIXTURES

    out = run(
        "dashboard.html",
        r"""
  answer = proofAnswer(COMMITTED);
  await visit('#/proof');
  out.view = view();
  const pilot = JSON.parse(JSON.stringify(COMMITTED));
  pilot.benchmarks.forEach(b => { b.pilot = true; });
  answer = proofAnswer(pilot);
  await visit('#/overview');
  await visit('#/proof');
  out.pilot = view();
""",
        tmp_path,
        before=FIXTURES + OPS + PROOF_FIXTURES + f"\nconst COMMITTED = {json.dumps(COMMITTED)};\n",
    )
    page = out["view"]
    series = COMMITTED["benchmarks"]
    chart = page.split("Did a violation land?", 1)[1].split('data-proof="series"', 1)[0]
    assert page.index("Did a violation land?") < page.index('data-proof="series"'), "The chart leads, the table follows"
    assert chart.count("<figure") == 1, "Every series in one figure, so the conditions are compared at a glance"
    assert chart.count("data-proof-row") == len(series), "One row a series, never pooled"
    assert "0%" in chart and "100%" in chart, "Every series is drawn on the same 0 to 100% scale"
    for section in series:
        for condition in section["conditions"]:
            v = condition["violation"]
            assert f"{v['k']}/{v['n']}" in chart
    governed = [next(c for c in s["conditions"] if c["condition"] == "threefold") for s in series]
    if all(c["violation"]["k"] == 0 for c in governed):
        assert f"No governed violation landed with Threefold enforcing, in any of the {len(series)} series" in chart
    assert "with no guidance, a violation landed in " in chart and "one landed" not in chart, "The verdict does not read as one violation"
    assert "Rules in CLAUDE.md or AGENTS.md" in chart and "rules in CLAUDE.md<" not in chart, "The chart does not name Claude Code's file for Codex"
    assert "data-metric" not in chart and "data-series=" not in chart and 'data-proof="provenance"' not in chart
    assert 'class="tf-legend"' in chart, "Three conditions carry a legend as well as their own labels"
    # What enforcing cost sits in the verdict, beside what it bought, family by family.
    cost = re.sub(r"<[^>]+>", "", chart.split('data-proof="cost"', 1)[1].split("</p>", 1)[0])
    families: dict = {}
    for section in series:
        done = next(c for c in section["conditions"] if c["condition"] == "threefold")["completion"]
        k, n = families.get(section["family"], (0, 0))
        families[section["family"]] = (k + done["k"], n + done["n"])
    for k, n in families.values():
        assert (f"every run of the" in cost and f"({k}/{n})" in cost) if k == n else f"{k} of {n} runs" in cost
    assert chart.index('data-proof="cost"') < chart.index("data-proof-row"), "The cost is read before the chart, not only in the table"
    assert "Did a violation land?" not in out["pilot"], "A pilot proves the harness, not a rate, so it is not charted"


# -------------------------------------------------------------- call, connect


def test_the_calls_screen_chooses_the_outcome_in_one_place(tmp_path: Path) -> None:
    out = ops(
        r"""
  answer = contract();
  await visit('#/calls?kind=observed&agent=codex&days=7');
  out.view = view();
  // The stub keeps no markup, so the fields hold what a browser would show.
  el('f-review').value = 'unreviewed'; el('f-agent').value = 'codex'; el('f-days').value = '7';
  click('filter');
  await tick();
  out.hash = location.hash;
""",
        tmp_path,
    )
    page = out["view"]
    assert re.search(r'<span aria-current="true"[^>]*>.*?Would refuse</span>', page), "The tab on screen names the outcome"
    assert '<select id="f-kind"' not in page and 'id="f-kind" value="observed"' in page, "The form carries the outcome, it does not ask again"
    chips = page.split('aria-label="Filter the calls"', 1)[1]
    assert "Remove the filter Codex" in chips and "Remove the filter Would refuse" not in chips, "No chip repeats the tab"
    assert out["hash"] == "#/calls?kind=observed&review=unreviewed&agent=codex&days=7", "Applying the form keeps the outcome the tabs chose"


def test_a_call_reads_as_an_incident_card(tmp_path: Path) -> None:
    out = ops(
        r"""
  const refused = row(1, { status: 'BLOCKED_BOUNDARY_VIOLATION', observed_rules: [], observed_rule: '', reason: 'The domain imports infrastructure.',
    suggested_fix_kind: 'layering', suggested_fix_validated: true });
  answer = contract({ '/api/decision': { status: 200, body: Object.assign({}, DECISION, { decision: refused }) } });
  await visit('#/call?timestamp=t&verdict_id=VERDICT-1');
  out.refused = view();
  answer = contract();
  await visit('#/call?timestamp=t&verdict_id=VERDICT-1&n=2');
  out.observed = view();
  answer = contract({ '/api/decision': { status: 200, body: { decision: row(3, { observed_rules: [], observed_rule: '', rule_key: 'NONE' }), session: null, rule: null } } });
  await visit('#/call?timestamp=t&verdict_id=VERDICT-3');
  out.approved = view();
""",
        tmp_path,
    )
    refused = out["refused"]
    card = refused.split('class="tf-ops-incident"', 1)[1].split("</article>", 1)[0]
    assert 'data-outcome="refused"' in refused and "Refused before it ran" in card
    for question in ("What the agent tried", "What Threefold said", "The rule", "Suggested fix"):
        assert question in card, f"The incident card does not answer: {question}"
    assert "src/billing/domain/Invoice.java" in card and "The domain imports infrastructure." in card
    assert "java-domain-stays-pure" in card and "Move the import out of the domain" in card
    assert refused.index("tf-ops-incident") < refused.index("Every field the ledger keeps"), "The card leads, the record follows"
    assert "It ran, and a rule in Observe would have refused it" in out["observed"] and "Was the rule right?" in out["observed"]
    approved = out["approved"]
    assert "Approved: nothing flagged it" in approved and "None: nothing flagged this call" in approved
    assert "Suggested fix" not in approved and "Was the rule right?" not in approved


def test_the_incident_card_speaks_of_whoever_sent_the_call(tmp_path: Path) -> None:
    out = ops(
        r"""
  const page = row(1, { agent: 'page', origin: 'page', session_id: 'sim-review-1', hook_mode: 'unknown', status: 'BLOCKED_BOUNDARY_VIOLATION',
    observed_rules: [], observed_rule: '', reason: 'The domain imports infrastructure.', project_name: 'Acme-Demo' });
  answer = contract({ '/api/decision': { status: 200, body: { decision: page, session: null, rule: null } } });
  await visit('#/call?timestamp=t&verdict_id=VERDICT-1');
  out.page = text(view().split('class="tf-ops-incident"')[1].split('</article>')[0]);
  const observed = Object.assign({}, DECISION, { rule: Object.assign({}, DECISION.rule, { mode: 'enforce' }) });
  answer = contract({ '/api/decision': { status: 200, body: observed } });
  await visit('#/call?timestamp=t&verdict_id=VERDICT-1&n=2');
  out.observed = view();
  const capped = Object.assign({}, observed, { decision: row(1, { dry_run: true, hook_mode: 'observe' }) });
  answer = contract({ '/api/decision': { status: 200, body: capped } });
  await visit('#/call?timestamp=t&verdict_id=VERDICT-1&n=3');
  out.capped = text(view());
""",
        tmp_path,
    )
    page = out["page"]
    assert "Page through its page" not in page and "through its page" not in page
    assert "What the page sent" in page and "A visitor, pressing a button on a page here" in page
    assert "the page showed why" in page and "the agent was told why" not in page, "A page call was not sent by an agent"
    assert "Hook mode" not in page, "A page call has no hook"
    assert "A refusal" in page and "An incident" not in page
    observed = out["observed"]
    assert "A flagged call" in observed and "An incident" not in observed
    rule = observed.split("What decided.", 1)[1]
    assert 'data-acted="observe"' in rule and "Observed this call" in rule, "The rule card says how the rule acted on this call"
    assert "Written to enforce; it only watched because the project was in Observe." in re.sub(r"<[^>]+>", "", rule)
    assert 'tf-chip-purple">enforce<' not in observed, "No enforce chip under a call the rule only watched"
    capped = out["capped"]
    assert "the hook on that machine is capped to Observe" in capped, "A capped hook's call is real work, named as such"
    assert "test call" not in capped and "dry run" not in capped


def test_a_count_the_service_did_not_inflect_reads_as_a_reader_would(tmp_path: Path) -> None:
    out = ops(
        KEYS
        + r"""
  const rules = [{ rule_key: 'LOOP', kind: 'gate', mode_now: 'observe', would_refuse: 3, correct: 1, false_alarms: 1, unreviewed: 1, last_seen: NOW, state: 'noisy', recommendation: '1 false alarm(s): keep it observing.' },
    { rule_key: 'BUDGET', kind: 'gate', mode_now: 'observe', would_refuse: 3, correct: 1, false_alarms: 2, unreviewed: 0, last_seen: NOW, state: 'noisy', recommendation: '2 false alarm(s): keep it observing.' }];
  answer = contract({ '/api/projects/Acme-Billing': { status: 200, body: detailBody('observe', rules) }, '/api/decisions': { status: 200, body: { items: [row(1)], next_cursor: null } } });
  await visit('#/projects/Acme-Billing');
  out.project = text(view());
  await visit('#/review');
  press('u'); await tick();
  out.toast = el('toast-root').innerHTML;
""",
        tmp_path,
    )
    assert "1 false alarm: keep it observing." in out["project"] and "2 false alarms: keep it observing." in out["project"]
    assert "(s)" not in out["project"]
    assert "Nothing to undo" in out["toast"], "U with nothing to take back says so on screen"


def test_connect_waits_with_a_radar_and_turns_into_a_success_card(tmp_path: Path) -> None:
    out = ops(
        r"""
  let decisions = { items: [], next_cursor: null };
  answer = contract({ '/api/decisions': () => ({ status: 200, body: decisions }) });
  await visit('#/connect');
  out.waiting = view();
  out.wait = el('connect-wait').innerHTML;
  decisions = { items: [row(1, { agent: 'antigravity', timestamp: new Date(Date.now() + 1000).toISOString() })], next_cursor: null };
  await runIntervals();
  out.connected = view();
""",
        tmp_path,
    )
    waiting = out["waiting"]
    assert 'role="tablist"' in waiting and waiting.count('role="tab"') == 2 and waiting.count('role="tabpanel"') == 2
    assert re.search(r'id="os-tab-posix"[^>]*aria-selected="true"', waiting), "The shell this browser runs on is chosen first"
    assert re.search(r'id="os-panel-powershell"[^>]*hidden', waiting), "The other shell's command waits behind its tab"
    assert "tf-ops-radar" in out["wait"] and "Waiting for the first call" in out["wait"]
    steps = waiting.split('class="tf-ops-steps"', 1)[1].split("</ol>", 1)[0]
    assert re.search(r'data-state="current" aria-current="step"><span class="tf-step-num" data-state="current">2<', steps), "Step 2 is current until a call lands"
    connected = out["connected"]
    assert "tf-ops-arrived" in connected and "tf-ops-radar" not in connected
    done = connected.split('class="tf-ops-steps"', 1)[1].split("</ol>", 1)[0]
    assert done.count('<li data-state="done"') == 3, "Every step is done once the first call lands"


def test_the_success_card_says_the_projects_own_stage_not_the_dry_runs(tmp_path: Path) -> None:
    out = ops(
        r"""
  const first = () => ({ items: [row(1, { agent: 'codex', dry_run: true, stage: 'observe', tool_name: 'Read', target: 'README.md', observed_rules: [], observed_rule: '', rule_key: 'NONE', timestamp: new Date(Date.now() + 1000).toISOString() })], next_cursor: null });
  let decisions = { items: [], next_cursor: null };
  const stage = held();
  answer = contract({
    '/api/decisions': () => ({ status: 200, body: decisions }),
    '/api/projects/Acme-Billing': () => stage.promise
  });
  await visit('#/connect');
  decisions = first();
  await runIntervals();
  out.reading = text(el('connect-wait').innerHTML);
  stage.release({ status: 200, body: detailBody('enforce', [RULES[0], Object.assign({}, RULES[1], { mode_now: 'enforce' }), RULES[2]]) });
  await tick();
  out.enforce = text(view());
  answer = contract({ '/api/decisions': () => ({ status: 200, body: decisions }), '/api/projects/Acme-Billing': 'network' });
  decisions = { items: [], next_cursor: null };
  await visit('#/overview');
  await visit('#/connect');
  decisions = first();
  await runIntervals();
  out.failed = text(view());
""",
        tmp_path,
    )
    assert "Reading the stage of Acme-Billing" in out["reading"] and "You are in Observe" not in out["reading"], "Nothing is claimed before the stage is read"
    enforce = out["enforce"]
    assert "This project is in Enforce" in enforce and "1 rule refuses a call from here" in enforce and "2 rules still observe" in enforce
    assert "You are in Observe" not in enforce and "Nothing your agents send from here is refused" not in enforce
    assert "a test call, recorded and never refused" in enforce and "stage observe" not in enforce, "The dry run's own Observe is not the project's stage"
    failed = out["failed"]
    assert "could not be read" in failed and "You are in Observe" not in failed and "in Enforce" not in failed


# --------------------------------------------------------------------- sign-in


def test_sign_in_promises_only_what_the_code_does_and_fails_inside_its_card(tmp_path: Path) -> None:
    out = ops(
        r"""
  answer = contract();
  await visit('#/signin');
  out.command = view();
  answer = contract({ 'POST /api/auth/sessions': { status: 200, body: {} } });
  await visit('#/signin?code=code-one');
  await tick();
  out.noSession = view();
  answer = contract({ 'POST /api/auth/sessions': { status: 500, body: { detail: 'The table is unavailable.' } } });
  await visit('#/signin?code=code-two');
  await tick();
  out.failed = view();
""",
        tmp_path,
    )
    words = re.sub(r"<[^>]+>", " ", out["command"])
    # The command sends the key to the stack to mint the link, and a hook on
    # a private stack sends it too, so the page never says the key stays home;
    # an unused link signs in whoever opens it, so it is not called unshareable.
    assert "never leaves the machine" not in words and "forwarded" not in words
    assert "The command sends the key to the stack, never to this page." in words
    assert "Whoever opens it first is signed in, so treat it like the key." in words
    for failure, title in ((out["noSession"], "The sign-in could not be completed"), (out["failed"], "The sign-in did not complete")):
        assert "tf-ops-signin-card" in failure and title in failure, "A failed sign-in stays in the branded card"
        assert 'data-state="signin-failed"' in failure and "python threefold.py open" in failure
    assert "The table is unavailable" in out["failed"] and 'data-action="signin-retry"' in out["failed"], "An error that may have left the link unspent offers to try it again"
    assert 'data-action="signin-retry"' not in out["noSession"], "A link the stack took is spent, so no retry is offered"


def test_what_the_operator_reads_is_12px_and_breaks_between_words(tmp_path: Path) -> None:
    from _browser import page_source

    ops_style = page_source("dashboard.html").split("/* OPS: the operator's screens", 1)[1].split("</style>", 1)[0]
    assert "0.625rem" not in ops_style, "No 10px text on the operator's screens"
    micro = [line.strip().split(" {", 1)[0] for line in ops_style.splitlines() if "--tf-fs-micro" in line]
    assert micro == [".tf-ops-att-kicker", ".tf-ops-flow-q"], "Only the uppercase kickers keep the 11px step"
    assert re.search(r"\.tf-ops-flow \.tf-chip \{[^}]*white-space: normal", ops_style), "A chip in the incident card wraps inside its card"
    out = ops(
        r"""
  answer = contract({ '/api/decisions': { status: 200, body: { items: [row(1, { rule_key: 'PROTECTED_PATH', observed_rules: ['PROTECTED_PATH'], target: 'src/acme/domain/very_long_module_name.py', observed_target: 'src/acme/domain/very_long_module_name.py' })], next_cursor: null } } });
  await visit('#/projects/Acme-Billing');
  out.recent = view().split('Recent calls')[1].split('All calls from')[0];
""",
        tmp_path,
    )
    recent = out["recent"]
    assert '<th scope="col">Rule</th>' not in recent, "A narrow card folds the rule under its call"
    assert "PROTECTED_<wbr>PATH" in recent and "very_<wbr>long_<wbr>module_<wbr>name.py" in recent, "Keys and paths break between words"


def test_a_screen_title_takes_the_focus_without_drawing_a_ring() -> None:
    from _browser import page_source

    styles = page_source("dashboard.html").split("</style>")
    assert any("#view-title:focus, #view-title:focus-visible { outline: none; }" in block for block in styles), (
        "The title the router focuses is a heading, not a control: no ring frames it"
    )
