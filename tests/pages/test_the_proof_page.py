"""The proof page, and self-correction where the dashboard shows it.

The proof page renders the snapshot scripts/build_proof.py writes, served at
GET /proof.json: every figure with its source and when it was taken, and "not
measured yet" wherever the snapshot has nothing, never a zero. The committed
snapshot is served to the page as it is on disk, so the page is checked against
the file a visitor would actually read. Run under Node with the stub browser in
_browser.py, as the other page tests are.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from _browser import WEB, page_source, run
from test_the_application_pages import FIXTURES

COMMITTED = json.loads((WEB / "proof.json").read_text(encoding="utf-8"))

PROOF_FIXTURES = r"""
function condition(name, label, n, extra) {
  return Object.assign({
    condition: name, label, n, not_measured: 0,
    violation: { k: 0, n, rate: n ? 0 : null, ci_low: n ? 0 : null, ci_high: n ? 0.49 : null },
    completion: { k: n, n, rate: n ? 1 : null, ci_low: n ? 0.51 : null, ci_high: n ? 1 : null },
    self_correction: { refused_runs: 0, self_corrected: 0, rate: null },
    overhead: name === 'none' ? null : { against: 'none', turns: null, seconds: null, cost: null }
  }, extra || {});
}
const MEASURED = {
  schema: 1, generated_at: '2026-09-30T12:00:00Z', generated_by: 'scripts/build_proof.py',
  benchmark: {
    source: 'benchmark/report.py over benchmark/results/20260930T090000Z.jsonl', snapshot_at: '2026-09-30', pilot: false,
    headline: 'Across 54 runs of claude-sonnet-5 on 6 Acme task(s), a governed violation landed in 61% (11/18) of runs with no guidance.',
    agent: 'Claude Code', agent_versions: ['2.1.220'], models: ['claude-sonnet-5'], dates: ['2026-09-30'],
    run_ids: ['20260930T090000Z'], tasks: ['orders-s3-archive'], rows: 55, real_rows: 55, valid_rows: 54, scripted_rows: 0,
    conditions: [
      condition('none', 'no guidance', 18, { violation: { k: 11, n: 18, rate: 0.6111, ci_low: 0.3862, ci_high: 0.797 } }),
      condition('prompt', 'rules in CLAUDE.md', 18, { violation: { k: 7, n: 18, rate: 0.3889, ci_low: 0.203, ci_high: 0.6138 },
        overhead: { against: 'none', turns: 1.02, seconds: 0.98, cost: 1.01 } }),
      condition('threefold', 'Threefold enforcing', 18, { not_measured: 1,
        self_correction: { refused_runs: 16, self_corrected: 13, rate: 0.8125 },
        overhead: { against: 'none', turns: 1.37, seconds: 1.41, cost: 1.33 } })
    ],
    evidence: [
      { label: 'The report', path: 'docs/evidence/BENCHMARK_2026-09-30.md', href: 'https://example.test/acme/threefold/blob/main/docs/evidence/BENCHMARK_2026-09-30.md' },
      { label: 'The rows', path: 'benchmark/results/20260930T090000Z.jsonl', href: 'javascript:alert(1)' }
    ]
  },
  private: {
    source: "GET /api/overview?days=30 and GET /api/projects on the owner's private stack", snapshot_at: '2026-09-29T08:00:00+00:00',
    window_days: 30, days_observed: 8, calls_governed: 4210, would_refuse: 210, refused: 0, reviewed: 150, false_alarms: 12,
    false_alarm_rate: 0.08, projects: 9, agents: 2, stages: { observe: 9, enforce: 0 },
    self_correction: { refusals_considered: 0, self_corrected: 0, rate: null, median_calls_to_correct: null, complete: true }
  },
  method: [{ label: 'How this snapshot was built', path: 'scripts/build_proof.py' }]
};
function proofAnswer(body) { return api({ '/proof.json': { status: 200, body } }); }
"""


def proof(scenario: str, tmp_path: Path) -> dict:
    return run("dashboard.html", scenario, tmp_path, before=FIXTURES + PROOF_FIXTURES + f"\nconst COMMITTED = {json.dumps(COMMITTED)};\n")


def _unescaped(markup: str) -> str:
    return (markup.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&#039;", "'").replace("&amp;", "&"))


# ------------------------------------------------------------------- the route


def test_the_proof_route_draws_a_screen_and_the_navigation_links_to_it(tmp_path: Path) -> None:
    out = proof(
        r"""
  answer = proofAnswer(COMMITTED);
  await visit('#/proof');
  out.view = view();
  out.title = document.title;
  out.nav = el('tf-nav').innerHTML;
  out.fetched = calls.map(c => c.url);
""",
        tmp_path,
    )
    assert 'id="view-title"' in out["view"] and out["title"] == "Proof — Threefold"
    assert 'href="#/proof"' in out["nav"] and ">Proof</a>" in out["nav"]
    assert re.search(r'href="#/proof"[^>]*aria-current="page"|aria-current="page"[^>]*href="#/proof"', out["nav"]), "The navigation marks the page"
    assert "https://example.test/prod/proof.json" in out["fetched"]


def test_the_committed_pilot_snapshot_is_shown_as_it_was_written(tmp_path: Path) -> None:
    out = proof(
        r"""
  answer = proofAnswer(COMMITTED);
  await visit('#/proof');
  out.view = view();
  out.text = text(view());
  out.metrics = metrics(view());
""",
        tmp_path,
    )
    page = _unescaped(out["view"])
    bench = COMMITTED["benchmark"]
    assert 'data-proof="pilot"' in out["view"] and "PILOT: not a result" in page
    assert bench["headline"] in page, "The headline is shown exactly as it was computed"
    assert bench["source"] in page and bench["snapshot_at"] in page
    for condition in bench["conditions"]:
        assert f'data-condition="{condition["condition"]}"' in out["view"]
    assert page.count("not measured") >= 6, "A condition nothing measured reads not measured, never 0%"
    assert "0%" not in out["text"]
    assert "1 run measured nothing" in out["text"]
    for item in bench["evidence"] + COMMITTED["method"]:
        assert item["path"] in page
    assert "Each is a file in the Threefold repository, at the path shown." in out["text"]
    # The snapshot carries no private section, so that section says so and shows no figure.
    assert "This snapshot carries no totals from the owner's own work." in page
    assert out["metrics"] == {}, "No tile is drawn for a section the snapshot does not carry"


def test_a_measured_snapshot_shows_each_figure_with_its_source_and_its_date(tmp_path: Path) -> None:
    out = proof(
        r"""
  answer = proofAnswer(MEASURED);
  await visit('#/proof');
  out.view = view();
  out.text = text(view());
  out.metrics = metrics(view());
""",
        tmp_path,
    )
    page, words = _unescaped(out["view"]), out["text"]
    assert 'data-proof="pilot"' not in out["view"]
    assert "61% (11/18)" in words and "39% (7/18)" in words and "95% CI 39%–80%" in words
    assert "81% (13/16 refused runs)" in words
    assert "turns 1.37× · time 1.41× · cost 1.33×" in words and "the baseline" in words
    assert "no run was refused" in words
    assert out["metrics"] == {"days_observed": "8", "calls_governed": "4,210", "would_refuse": "210", "reviewed": "150",
                              "false_alarm_rate": "8%", "projects": "9", "agents": "2"}
    assert "12 of 150 reviewed" in words and "of the last 30" in words
    assert "Self-corrected: no agent (hook or CI) refusal in this window." in words
    assert page.count('data-proof="provenance"') == 2, "Both sections say where they came from and when"
    assert "2026-09-29T08:00:00+00:00" in page and "2026-09-30" in page
    assert "GET /api/overview?days=30 and GET /api/projects on the owner's private stack" in page
    # An evidence link is followed only when it is an https address.
    assert 'href="https://example.test/acme/threefold/blob/main/docs/evidence/BENCHMARK_2026-09-30.md"' in out["view"]
    assert "javascript:" not in out["view"]


def test_a_false_alarm_rate_the_totals_cannot_give_says_why(tmp_path: Path) -> None:
    out = proof(
        r"""
  const enforcing = JSON.parse(JSON.stringify(MEASURED));
  Object.assign(enforcing.private, { refused: 6, reviewed: 2, false_alarms: null, false_alarm_rate: null });
  answer = proofAnswer(enforcing);
  await visit('#/proof');
  out.text = text(view());
  out.metrics = metrics(view());
""",
        tmp_path,
    )
    assert out["metrics"]["false_alarm_rate"] == "—", "No rate is a dash, never a figure over unlike counts"
    assert "not given: calls were refused in this window, and these totals count their labels too" in out["text"]
    assert "The rate is given only for a window in which nothing was refused" in out["text"]


def test_no_snapshot_reads_not_measured_yet_and_shows_no_figure(tmp_path: Path) -> None:
    out = proof(
        r"""
  answer = api({ '/proof.json': { status: 404, body: { title: 'Not Measured Yet', type: 'urn:threefold:error:proof-not-found' } } });
  await visit('#/proof');
  out.view = view();
  out.text = text(view());
""",
        tmp_path,
    )
    assert 'data-state="empty"' in out["view"] and "Not measured yet" in out["text"]
    assert "data-metric" not in out["view"]
    assert not re.search(r"\d", _unescaped(out["text"])), f"The empty state carries a number: {out['text']}"


def test_a_snapshot_with_neither_section_invents_nothing(tmp_path: Path) -> None:
    out = proof(
        r"""
  answer = proofAnswer({ schema: 1 });
  await visit('#/proof');
  out.view = view();
  out.text = text(view());
""",
        tmp_path,
    )
    assert out["view"].count('data-state="empty"') >= 3, "Benchmark, own use and evidence each say not measured"
    assert "data-metric" not in out["view"]
    assert not re.search(r"\d", _unescaped(out["text"])), f"A snapshot of nothing showed a number: {out['text']}"


def test_a_snapshot_that_cannot_be_read_is_an_error_not_an_empty_state(tmp_path: Path) -> None:
    out = proof(
        r"""
  answer = () => 'network';
  await visit('#/proof');
  out.view = view();
  answer = api({ '/proof.json': { status: 500, body: { title: 'Proof Unreadable' } } });
  await visit('#/overview');
  await visit('#/proof');
  out.broken = view();
""",
        tmp_path,
    )
    for markup in (out["view"], out["broken"]):
        assert 'data-state="error"' in markup and 'data-state="empty"' not in markup


def test_every_string_in_the_snapshot_is_escaped(tmp_path: Path) -> None:
    out = proof(
        r"""
  const evil = JSON.parse(JSON.stringify(MEASURED));
  evil.generated_by = EVIL;
  evil.benchmark.headline = EVIL; evil.benchmark.source = EVIL; evil.benchmark.snapshot_at = EVIL;
  evil.benchmark.models = [EVIL]; evil.benchmark.agent = EVIL; evil.benchmark.dates = [EVIL];
  evil.benchmark.conditions[0].label = EVIL; evil.benchmark.conditions[0].condition = EVIL;
  evil.benchmark.evidence = [{ label: EVIL, path: EVIL, href: 'https://example.test/' + EVIL }];
  evil.private.source = EVIL; evil.private.snapshot_at = EVIL;
  evil.method = [{ label: EVIL, path: EVIL }];
  answer = proofAnswer(evil);
  await visit('#/proof');
  out.view = view();
""",
        tmp_path,
    )
    assert "<img" not in out["view"] and "<svg onload" not in out["view"]
    assert "&lt;img" in out["view"]


def test_the_snapshot_route_the_page_reads_is_in_the_published_document() -> None:
    spec = json.loads((WEB / "openapi.json").read_text(encoding="utf-8"))
    assert "get" in spec["paths"]["/proof.json"]
    assert "T.api('/proof.json')" in page_source("dashboard.html")


# ------------------------------------------------------------ self-correction


def test_the_overview_tile_shows_self_correction_and_opens_the_refusals(tmp_path: Path) -> None:
    out = proof(
        r"""
  const figure = { refusals_considered: 12, self_corrected: 5, rate: 0.4167, median_calls_to_correct: 2, rows_read: 900, complete: true };
  answer = contract({ '/api/overview': { status: 200, body: Object.assign(overviewBody(), { self_correction: figure }) } });
  await visit('#/overview?days=7');
  out.full = view();
  answer = contract({ '/api/overview': { status: 200, body: Object.assign(overviewBody(), { self_correction: Object.assign({}, figure, { complete: false, rows_read: 2000, median_calls_to_correct: 1.5 }) }) } });
  await visit('#/overview?days=14');
  out.partial = view();
""",
        tmp_path,
    )
    tile = re.search(r'<a href="([^"]+)"[^>]*aria-label="(Self-corrected[^"]*)"[^>]*>(.*?)</a>', out["full"], re.S)
    assert tile, "The overview draws a Self-corrected tile"
    assert tile.group(1).replace("&amp;", "&") == "#/calls?kind=refused&days=7"
    body = re.sub(r"<[^>]+>", " ", tile.group(3))
    assert re.search(r'data-metric="self_corrected"[^>]*>5<', tile.group(3))
    assert "42% of 12 agent refusals" in body and "median 2 calls" in body
    # The link opens every refusal, a larger set than was counted, and says so.
    label = _unescaped(tile.group(2))
    assert "Opens every refused call in this window" in label and "counted over" not in label
    assert "a hook or CI call to a file" in label and "a refused command is left out" in label
    assert "read stopped after the newest 2,000 calls" in out["partial"] and "of every project" not in out["partial"]
    assert "median 1.5 calls" in out["partial"]


def test_the_overview_tile_is_honest_when_there_was_nothing_to_correct(tmp_path: Path) -> None:
    out = proof(
        r"""
  answer = contract({ '/api/overview': { status: 200, body: Object.assign(overviewBody(), { self_correction: { refusals_considered: 0, self_corrected: 0, rate: null, median_calls_to_correct: null, rows_read: 40, complete: true } }) } });
  await visit('#/overview?days=7');
  out.none = view();
  out.noneMetrics = metrics(view());
  answer = contract();
  await visit('#/overview?days=30');
  out.older = view();
""",
        tmp_path,
    )
    assert "no agent (hook or CI) refusal in this window" in out["none"]
    assert out["noneMetrics"]["self_corrected"] == "—", "No refusals is a dash, not a zero that reads as a failure"
    assert "not reported by this stack" in out["older"], "A stack that predates the field says so"


def test_a_ledger_that_could_not_be_read_says_so_and_shows_no_figure(tmp_path: Path) -> None:
    out = proof(
        r"""
  answer = contract({ '/api/overview': { status: 200, body: Object.assign(overviewBody(), { self_correction: { refusals_considered: 0, self_corrected: 0, rate: null, median_calls_to_correct: null, rows_read: 0, complete: false } }) } });
  await visit('#/overview?days=7');
  out.view = view();
  out.metrics = metrics(view());
""",
        tmp_path,
    )
    assert "not measured: the ledger could not be read" in out["view"]
    assert "no agent (hook or CI) refusal" not in out["view"], "An unread ledger is not a window with no refusal"
    assert out["metrics"]["self_corrected"] == "—"


def test_the_project_page_shows_the_same_figure_for_its_project(tmp_path: Path) -> None:
    out = proof(
        r"""
  const detail = detailBody('enforce');
  detail.readiness.summary.self_correction = { refusals_considered: 4, self_corrected: 3, rate: 0.75, median_calls_to_correct: 1, rows_read: 70, complete: true };
  answer = contract({ '/api/projects/Acme-Billing': { status: 200, body: detail } });
  await visit('#/projects/Acme-Billing?days=14');
  out.view = view();
  out.metrics = metrics(view());
  const quiet = detailBody('observe');
  quiet.readiness.summary.self_correction = { refusals_considered: 0, self_corrected: 0, rate: null, median_calls_to_correct: null, rows_read: 70, complete: true };
  answer = contract({ '/api/projects/Acme-Billing': { status: 200, body: quiet } });
  await visit('#/projects/Acme-Billing');
  out.quiet = view();
  const partial = detailBody('enforce');
  partial.readiness.summary.self_correction = { refusals_considered: 1, self_corrected: 1, rate: 1, median_calls_to_correct: 1, rows_read: 2000, complete: false };
  answer = contract({ '/api/projects/Acme-Billing': { status: 200, body: partial } });
  await visit('#/projects/Acme-Billing?days=30');
  out.partial = view();
""",
        tmp_path,
    )
    assert out["metrics"]["self_corrected"] == "3"
    assert "75% of 4 agent refusals" in out["view"] and "median 1 call" in out["view"]
    assert 'href="#/calls?project=Acme-Billing&amp;kind=refused&amp;days=14"' in out["view"]
    assert "Opens every refused call of this project in this window" in out["view"]
    assert "no agent (hook or CI) refusal in this window" in out["quiet"]
    # A read cut short spent its rows on every project, not this one alone.
    assert "read stopped after the newest 2,000 calls of every project" in out["partial"]
