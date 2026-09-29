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

import pytest

from _browser import WEB, page_source, run
from _headless import measure
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


def _card(page: str, title: str) -> str:
    """The one card drawn under this title. A series' label is also a row of the comparison table, so the heading decides."""
    (found,) = [part for part in page.split("<section")[1:] if f'<h2 class="tf-section-title">{title}</h2>' in part]
    return found


def _series_evidence(card: str) -> str:
    return card.split('data-proof="series-evidence"', 1)[1].split("</ul>", 1)[0]


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
    assert 'id="view-title"' in out["view"] and out["title"] == "Proof - Threefold"
    assert 'href="#/proof"' in out["nav"] and ">Proof</a>" in out["nav"]
    assert re.search(r'href="#/proof"[^>]*aria-current="page"|aria-current="page"[^>]*href="#/proof"', out["nav"]), "The navigation marks the page"
    assert "https://example.test/prod/proof.json" in out["fetched"]


def test_the_committed_snapshot_is_shown_as_it_was_written(tmp_path: Path) -> None:
    """Every series the snapshot carries is drawn apart, with its own headline and rows."""
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
    series = COMMITTED["benchmarks"]
    assert len(series) >= 2 and 'data-proof="series"' in out["view"]
    for section in series:
        assert section["label"] in page, "each series is named"
        assert section["headline"] in page, "each headline is shown exactly as it was computed"
        assert section["source"] in page
    assert out["view"].count('data-series="pressure"') == sum(1 for section in series if section["family"] == "pressure")
    assert 'data-proof="pilot"' not in out["view"], "no committed series is a pilot"
    if "private" in COMMITTED:
        assert out["metrics"].get("calls_governed") == f"{COMMITTED['private']['calls_governed']:,}"
    # Each series' card cites the evidence the snapshot gives that series, and
    # the method's card only the method.
    for section in series:
        own = _series_evidence(_card(page, section["label"]))
        assert own.count("<li") == len(section["evidence"]), section["label"]
        for item in section["evidence"]:
            assert item["path"] in own, f"{section['label']} does not cite {item['path']}"
            if "href" in item:
                assert f'href="{item["href"]}"' in own
    method = _card(page, "How this was measured")
    for item in COMMITTED["method"]:
        assert item["path"] in method
    assert "docs/evidence/" not in method and "benchmark/results/" not in method


def test_a_pilot_snapshot_says_it_is_not_a_result(tmp_path: Path) -> None:
    pilot = json.loads(json.dumps(COMMITTED["benchmarks"][0]))
    pilot["pilot"] = True
    document = {"schema": COMMITTED["schema"], "generated_at": COMMITTED["generated_at"],
                "generated_by": COMMITTED["generated_by"], "benchmark": pilot, "method": COMMITTED["method"]}
    out = proof(
        r"""
  answer = proofAnswer(PILOT_DOC);
  await visit('#/proof');
  out.view = view();
  out.metrics = metrics(view());
""".replace("PILOT_DOC", json.dumps(document)),
        tmp_path,
    )
    page = _unescaped(out["view"])
    assert 'data-proof="pilot"' in out["view"] and "PILOT: not a result" in page
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
    # Nothing was refused, so the shared empty branch speaks, in the same
    # words as every other screen.
    assert "Self-corrected: no agent (hook or CI) refusal in this window." in words
    assert page.count('data-proof="provenance"') == 2, "Both sections say where they came from and when"
    assert "2026-09-29T08:00:00+00:00" in page and "2026-09-30" in page
    assert "GET /api/overview?days=30 and GET /api/projects on the owner's private stack" in page
    # An evidence link is followed only when it is an https address.
    assert 'href="https://example.test/acme/threefold/blob/main/docs/evidence/BENCHMARK_2026-09-30.md"' in out["view"]
    assert "javascript:" not in out["view"]


def test_each_series_card_cites_its_own_report_and_never_another_series(tmp_path: Path) -> None:
    """Two series of one day: each card lists its own report and rows, and the method's card neither."""
    out = proof(
        r"""
  const sonnet = JSON.parse(JSON.stringify(MEASURED.benchmark));
  sonnet.label = 'Standard tasks · Claude Code · claude-sonnet-5';
  const haiku = JSON.parse(JSON.stringify(MEASURED.benchmark));
  Object.assign(haiku, {
    label: 'Standard tasks · Claude Code · claude-haiku-4-5', models: ['claude-haiku-4-5'], run_ids: ['20260930T100000Z'],
    source: 'benchmark/report.py over benchmark/results/20260930T100000Z.jsonl',
    evidence: [
      { label: 'The report', path: 'docs/evidence/BENCHMARK_2026-09-30-HAIKU.md', href: 'https://example.test/acme/threefold/blob/main/docs/evidence/BENCHMARK_2026-09-30-HAIKU.md' },
      { label: 'The rows', path: 'benchmark/results/20260930T100000Z.jsonl' }
    ]
  });
  answer = proofAnswer(Object.assign({}, MEASURED, { benchmarks: [sonnet, haiku], benchmark: sonnet }));
  await visit('#/proof');
  out.view = view();
""",
        tmp_path,
    )
    page = _unescaped(out["view"])
    sonnet = _series_evidence(_card(page, "Standard tasks · Claude Code · claude-sonnet-5"))
    haiku = _series_evidence(_card(page, "Standard tasks · Claude Code · claude-haiku-4-5"))
    assert 'href="https://example.test/acme/threefold/blob/main/docs/evidence/BENCHMARK_2026-09-30.md"' in sonnet
    assert "benchmark/results/20260930T090000Z.jsonl" in sonnet
    assert "BENCHMARK_2026-09-30-HAIKU.md" not in sonnet and "20260930T100000Z" not in sonnet
    assert 'href="https://example.test/acme/threefold/blob/main/docs/evidence/BENCHMARK_2026-09-30-HAIKU.md"' in haiku
    assert "benchmark/results/20260930T100000Z.jsonl" in haiku
    assert "BENCHMARK_2026-09-30.md" not in haiku and "20260930T090000Z" not in haiku
    # A path is listed even when its link is not an https address, and that link is dropped.
    assert "javascript:" not in out["view"]
    method = _card(page, "How this was measured")
    assert "scripts/build_proof.py" in method
    assert "docs/evidence/" not in method and "benchmark/results/" not in method, "The first series' report would read as every series'"


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
    assert out["metrics"]["false_alarm_rate"] == "-", "No rate is a dash, never a figure over unlike counts"
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


# ------------------------------------------------- what the proof compares with

# The file the rules condition wrote the rules to, per agent, as the benchmark's
# harness names it (benchmark/harness.py, RULES_FILE_NAME).
RULES_FILES = {"Claude Code": "CLAUDE.md", "Codex": "AGENTS.md"}


def _said(markup: str, key: str) -> str:
    """The words of the paragraph marked data-proof=key, as a reader reads them."""
    found = re.search(rf'data-proof="{key}">(.*?)</p>', markup, re.S)
    assert found, f"No paragraph marked {key}"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", _unescaped(found.group(1)))).strip()


def _prompt_label(card: str) -> str:
    found = re.search(r'<tr data-condition="prompt">.*?<span class="text-gray-200">([^<]*)</span>', card, re.S)
    assert found, "The card has no row for the rules condition"
    return _unescaped(found.group(1))


def _with_codex_labels_corrected(snapshot: dict) -> dict:
    """The snapshot as report.py writes it once it names the file per agent."""
    fixed = json.loads(json.dumps(snapshot))
    for b in fixed["benchmarks"]:
        for c in b["conditions"]:
            if b["agent"] in RULES_FILES:
                c["label"] = re.sub(r"\b(?:CLAUDE|AGENTS)\.md\b", RULES_FILES[b["agent"]], c["label"])
    return fixed


@pytest.mark.parametrize("corrected", [False, True])
def test_each_series_names_the_rules_file_its_agent_read(tmp_path: Path, corrected: bool) -> None:
    """Both Codex cards said "rules in CLAUDE.md"; Codex was given the rules in AGENTS.md, as their headlines say.

    A snapshot written before report.py named the file per agent carries that
    label, so the page names the file from the series' agent, and says the
    same of a snapshot that already does. A lane's name says the same, and the
    legend and the table heading name every file the series used.
    """
    snapshot = _with_codex_labels_corrected(COMMITTED) if corrected else COMMITTED
    out = proof(
        f"""
  answer = proofAnswer({json.dumps(snapshot)});
  await visit('#/proof');
  out.view = view();
""",
        tmp_path,
    )
    page = _unescaped(out["view"])
    series = [b for b in snapshot["benchmarks"] if b["agent"] in RULES_FILES]
    assert {b["agent"] for b in series} == set(RULES_FILES), "The committed snapshot holds both agents"
    for b in series:
        label = _prompt_label(_card(page, b["label"]))
        other = next(f for f in RULES_FILES.values() if f != RULES_FILES[b["agent"]])
        assert RULES_FILES[b["agent"]] in label and other not in label, f"{b['label']}: {label}"
        assert RULES_FILES[b["agent"]] in b["headline"], "The card's own headline names the same file"
    lanes = re.findall(r'<div class="tf-ops-vlane" role="img" aria-label="([^"]*)"', out["view"])
    for b in series:
        name = ", ".join(b["label"].split(" · "))
        (lane,) = [_unescaped(l) for l in lanes if _unescaped(l).startswith(name + ". Rules in ")]
        assert lane.startswith(f"{name}. Rules in {RULES_FILES[b['agent']]}:"), lane
    assert '<th scope="col">Rules in CLAUDE.md or AGENTS.md</th>' in out["view"]
    assert "Rules in CLAUDE.md or AGENTS.md</li>" in out["view"], "The legend names both files"


def test_an_agent_the_page_does_not_know_keeps_the_snapshot_s_words(tmp_path: Path) -> None:
    out = proof(
        r"""
  const other = JSON.parse(JSON.stringify(MEASURED.benchmark));
  Object.assign(other, { agent: 'Acme Agent', label: 'Standard tasks · Acme Agent · acme-model' });
  answer = proofAnswer(Object.assign({}, MEASURED, { benchmarks: [other], benchmark: other }));
  await visit('#/proof');
  out.view = view();
""",
        tmp_path,
    )
    page = _unescaped(out["view"])
    assert _prompt_label(_card(page, "Standard tasks · Acme Agent · acme-model")) == "rules in CLAUDE.md"
    assert "<th scope=\"col\">Rules in the agent's own instructions file</th>" in page, "No file is named that the page cannot vouch for"


def _series_words(b: dict) -> str:
    parts = b["label"].split(" · ")
    family = b.get("family") or ""
    where = f"the {family} tasks" if re.fullmatch(r"[a-z][a-z-]*", family) else "the " + parts[0][:1].lower() + parts[0][1:]
    return f"{' · '.join(parts[1:]) or parts[0]} on {where}"


def _condition(b: dict, name: str) -> dict | None:
    return next((c for c in b.get("conditions", []) if c.get("condition") == name), None)


def _measured(stat: dict | None) -> bool:
    return bool(stat) and isinstance(stat.get("n"), int) and stat["n"] > 0 and isinstance(stat.get("rate"), (int, float))


def test_the_lead_compares_threefold_with_the_rules_written_down(tmp_path: Path) -> None:
    """The callout compared Threefold only with no guidance; against the rules in the file it ties in half the series.

    One sentence says it, read from the committed snapshot: the series where
    neither let a violation land, each series where the rules did, with its
    runs, and what Threefold did there, and the tests passed with the rules
    written down beside those with Threefold enforcing. Every figure is
    computed here from the same snapshot, so a rebuilt one is checked the same
    way.
    """
    out = proof(
        r"""
  answer = proofAnswer(COMMITTED);
  await visit('#/proof');
  out.view = view();
""",
        tmp_path,
    )
    said = _said(out["view"], "against-rules")
    shown = [b for b in COMMITTED["benchmarks"] if not b.get("pilot")]
    paired = [b for b in shown if _measured((_condition(b, "prompt") or {}).get("violation")) and _measured((_condition(b, "threefold") or {}).get("violation"))]
    assert paired, "The committed snapshot measures both conditions"
    files = " or ".join(f for f in ("CLAUDE.md", "AGENTS.md") if f in {RULES_FILES[b["agent"]] for b in paired})
    assert said.startswith(f"Against the rules written in {files}: "), said
    neither = [b for b in paired if _condition(b, "prompt")["violation"]["k"] == 0 and _condition(b, "threefold")["violation"]["k"] == 0]
    rules_let = [b for b in paired if _condition(b, "prompt")["violation"]["k"] > 0]
    if 0 < len(neither) < len(paired):
        assert f"neither they nor Threefold let a violation land in {len(neither)} of the {len(paired)} series" in said
    for b in rules_let:
        v = _condition(b, "prompt")["violation"]
        assert f"{v['k']} of {v['n']} runs ({_series_words(b)})" in said, f"{b['label']} is not named with its runs"
    for b in neither:
        assert f"({_series_words(b)})" not in said, "A series where neither let one land is counted, not listed"
    if rules_let and all(_condition(b, "threefold")["violation"]["k"] == 0 for b in rules_let):
        assert "and Threefold in none" in said
    # The tests passed, family by family, over the series measured under both.
    families: dict[str, list[dict]] = {}
    for b in paired:
        families.setdefault(b["family"], []).append(b)
    rules_passed, threefold_passed = [], []
    for family, members in families.items():
        for name, into in (("prompt", rules_passed), ("threefold", threefold_passed)):
            done = [_condition(b, name)["completion"] for b in members]
            into.append(f"{sum(d['k'] for d in done)} of {sum(d['n'] for d in done)}")
    words = [f"{k} runs of the {family} tasks" for k, family in zip(rules_passed, families)]
    joined = lambda items: items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]  # noqa: E731
    assert f"with the rules written down, the tests passed in {joined(words)}, against {joined(threefold_passed)} with Threefold enforcing." in said
    assert "%" not in said, "Every figure is k of n runs, as the snapshot holds it"


def test_the_comparison_counts_only_series_measured_under_both_and_says_what_threefold_let_land(tmp_path: Path) -> None:
    """A series with no rules condition is left out and said to be; where Threefold let one land, the sentence says so."""
    out = proof(
        r"""
  const run = (k, n) => ({ k, n, rate: k / n, ci_low: 0, ci_high: 1 });
  const series = (label, agent, family, conditions) => ({
    label, agent, family, pilot: false, models: [label.split(' · ')[2]], source: 'benchmark/report.py over ' + label,
    conditions: conditions.map(([condition, violation, completion]) => ({ condition, label: condition, n: violation.n, violation, completion }))
  });
  const list = [
    series('Standard tasks · Claude Code · model-a', 'Claude Code', 'standard', [['none', run(5, 18), run(18, 18)], ['prompt', run(2, 18), run(17, 18)], ['threefold', run(1, 18), run(18, 18)]]),
    series('Standard tasks · Codex · model-b', 'Codex', 'standard', [['none', run(4, 18), run(18, 18)], ['prompt', run(0, 18), run(18, 18)], ['threefold', run(0, 18), run(18, 18)]]),
    series('Pressure tasks · Codex · model-c', 'Codex', 'pressure', [['none', run(9, 9), run(9, 9)], ['prompt', run(0, 9), run(9, 9)], ['threefold', run(2, 9), run(5, 9)]]),
    series('Pressure tasks · Claude Code · model-d', 'Claude Code', 'pressure', [['none', run(9, 9), run(9, 9)], ['threefold', run(0, 9), run(4, 9)]])
  ];
  answer = proofAnswer({ schema: 1, benchmarks: list, benchmark: list[0] });
  await visit('#/proof');
  out.view = view();
""",
        tmp_path,
    )
    assert _said(out["view"], "against-rules") == (
        "Against the rules written in CLAUDE.md or AGENTS.md: "
        "neither they nor Threefold let a violation land in 1 of the 3 series measured under both; "
        "in 1 the rules did, in 2 of 18 runs (Claude Code · model-a on the standard tasks), "
        "and Threefold in 1 of 18 runs (Claude Code · model-a on the standard tasks); "
        "Threefold let a violation land where the rules did not in 2 of 9 runs (Codex · model-c on the pressure tasks); "
        "with the rules written down, the tests passed in 35 of 36 runs of the standard tasks and 9 of 9 runs of the pressure tasks, "
        "against 36 of 36 and 5 of 9 with Threefold enforcing."
    )


def test_the_owner_s_own_use_says_its_projects_only_observed_and_nothing_was_refused(tmp_path: Path) -> None:
    """The section said "Threefold governing the owner's real work" over ten projects in Observe and no refusal.

    Beside the tiles it now says, from the snapshot, what stage the projects
    were in and whether anything was refused, and self-correction says it has
    nothing to count rather than reading as a measurement. Where projects
    enforce and calls were refused, it says that instead.
    """
    out = proof(
        r"""
  answer = proofAnswer(COMMITTED);
  await visit('#/proof');
  out.committed = view();
  const enforcing = JSON.parse(JSON.stringify(MEASURED));
  Object.assign(enforcing.private, { refused: 6, stages: { observe: 7, enforce: 2 } });
  enforcing.private.self_correction = { refusals_considered: 6, self_corrected: 3, rate: 0.5, median_calls_to_correct: 2, rows_read: 900, complete: true };
  answer = proofAnswer(enforcing);
  await visit('#/overview');
  await visit('#/proof');
  out.enforcing = view();
  const older = JSON.parse(JSON.stringify(MEASURED));
  delete older.private.stages; delete older.private.refused;
  answer = proofAnswer(older);
  await visit('#/overview');
  await visit('#/proof');
  out.older = view();
""",
        tmp_path,
    )
    own = COMMITTED["private"]
    stages = own["stages"]
    stage_words = (f"all {stages['observe']} projects were in Observe and none in Enforce" if stages["enforce"] == 0
                   else f"{stages['observe']} projects were in Observe and {stages['enforce']} in Enforce")
    said = _said(out["committed"], "stages")
    assert said.startswith(f"At the snapshot, {stage_words}"), said
    if own["refused"] == 0:
        assert f"the service refused no call in the {own['window_days']} days these totals cover" in said
        assert _said(out["committed"], "self-corrected") == (
            "Self-corrected: no agent (hook or CI) refusal among the calls read · read stopped after the newest 2,000 calls.")
    assert "Threefold governing the owner" not in _unescaped(out["committed"])
    assert _said(out["enforcing"], "stages") == "At the snapshot, 7 projects were in Observe and 2 in Enforce, and the service refused 6 calls in the 30 days these totals cover."
    assert _said(out["enforcing"], "self-corrected") == "Self-corrected: 50% of 6 agent refusals · median 2 calls."
    assert 'data-proof="stages"' not in out["older"] and 'data-proof="self-corrected"' in out["older"], \
        "A snapshot that carries neither says nothing of them, and keeps its self-correction line"


def test_the_first_screen_leaves_the_focus_at_the_start_and_a_route_change_moves_it(tmp_path: Path) -> None:
    """On first load the title took the focus, so the first Tab skipped "Skip to content" and the header."""
    out = run(
        "dashboard.html",
        r"""
  out.first = document.activeElement && document.activeElement.id;
  out.drawn = /id="view-title"/.test(view());
  answer = contract();
  await visit('#/overview');
  out.after = document.activeElement && document.activeElement.id;
""",
        tmp_path,
        before=FIXTURES + PROOF_FIXTURES + f"\nconst COMMITTED = {json.dumps(COMMITTED)};\nanswer = proofAnswer(COMMITTED);\nopenAt('#/proof');\n",
    )
    assert out["drawn"] and not out["first"], "The page's first screen is drawn and nothing takes the focus"
    assert out["after"] == "view-title", "A route change moves the focus to the new screen's title"


def test_in_a_browser_the_first_tab_on_the_proof_reaches_skip_to_content(tmp_path: Path) -> None:
    """Opened at #/proof, the snapshot drawn, the focus is still the document's, and the first stop is the skip link.

    Reached by a route change instead, the proof's title has the focus once
    the snapshot has replaced the loading screen, title and all.
    """
    result = measure(
        "dashboard.html",
        tmp_path,
        width=1440,
        height=900,
        replies={
            "/proof.json": {"status": 200, "body": COMMITTED},
            "/api/auth/whoami": {"status": 200, "body": {"authenticated": False, "via": None, "reads_public": True, "sandbox_writes": True}},
        },
        moments={"drawn": 1500, "away": 2500, "back": 3500},
        # Each moment reads where the focus is, then takes the next step.
        probe=r"""() => {
  const stops = Array.from(document.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select, textarea, summary, [tabindex]'))
    .filter(e => e.getAttribute('tabindex') !== '-1' && e.getClientRects().length > 0);
  const a = document.activeElement;
  const seen = { active: !a || a === document.body ? 'body' : (a.id || a.tagName), first: stops.length ? stops[0].textContent.trim() : null,
    drawn: !!document.querySelector('[data-proof="against-rules"]') };
  window.__step = (window.__step || 0) + 1;
  if (window.__step === 1) location.hash = '#/overview';
  if (window.__step === 2) location.hash = '#/proof';
  return seen;
}""",
        before="window.tailwind = {}; history.replaceState(null, '', '#/proof');",
    )
    taken = result["taken"]
    assert taken["drawn"] == {"active": "body", "first": "Skip to content", "drawn": True}, taken["drawn"]
    assert taken["back"]["drawn"] and taken["back"]["active"] == "view-title", taken["back"]


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
    assert out["noneMetrics"]["self_corrected"] == "-", "No refusals is a dash, not a zero that reads as a failure"
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
    assert out["metrics"]["self_corrected"] == "-"


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



def test_the_lead_lane_is_told_apart_at_zero_and_no_lane_is_a_tab_stop(tmp_path: Path) -> None:
    """At 0% a lane is only its tick: the tick wears the lane's colour, and the Threefold lane names itself.

    A review found "Rules in CLAUDE.md" and "Threefold enforcing" drawn the
    same, as one grey tick each, in every series where both were 0%; and 18
    lanes were 18 tab stops before the evidence, with the same figures in the
    table right after the chart.
    """
    out = proof(
        r"""
  answer = proofAnswer(COMMITTED);
  await visit('#/proof');
  out.view = view();
""",
        tmp_path,
    )
    chart = out["view"].split("Did a violation land?")[1].split('data-proof="series"')[0]
    lanes = re.findall(r'<div class="tf-ops-vlane"[^>]*>', chart)
    assert lanes and not any("tabindex" in lane for lane in lanes), "A lane is not a tab stop; the table holds its figures"
    threefold = [lane for lane in lanes if "Threefold enforcing:" in lane]
    assert threefold and all("--c:#8b5cf6" in lane for lane in threefold)
    assert chart.count("<b>Threefold</b>") == len(threefold), "Each Threefold lane at or under half names itself"
    css = page_source("dashboard.html")
    assert "top: 2px; bottom: 2px; width: 3px; border-radius: 1px; background: var(--c); }" in css, "The tick wears the lane's colour"


def test_each_series_names_its_own_evidence_and_every_path_opens(tmp_path: Path) -> None:
    """Each series cites the report made from its own rows, on its own card, and each path is a link.

    The committed snapshot is built with --evidence-base, so every path it
    gives already carries its address in the public repository.
    """
    out = proof(
        r"""
  answer = proofAnswer(COMMITTED);
  await visit('#/proof');
  out.view = view();
""",
        tmp_path,
    )
    page = out["view"]
    cards = page.split("How this was measured")[0]
    series = [b for b in COMMITTED["benchmarks"] if b.get("evidence")]
    lists = re.findall(r'data-proof="series-evidence">(.*?)</ul>', cards, re.S)
    assert len(lists) == len(series), "Every series with evidence names it on its own card"
    reports = [[i["path"] for i in b["evidence"] if i["path"].endswith(".md")] for b in series]
    assert all(len(r) == 1 for r in reports) and len({r[0] for r in reports}) == len(series), "Each series has a report of its own"
    for markup, b in zip(lists, series):
        for item in b["evidence"]:
            assert f'href="https://github.com/upgradedev/aws-threefold/blob/main/{item["path"]}"' in markup
    # Whether the box is a keyboard stop is measured in a real browser, in
    # test_a_table_box_is_a_stop_only_while_it_scrolls: here, that it is marked.
    assert '<div class="tf-scroll-x" data-tf-scroll="The benchmark, every series">' in page, "The table's box is one the design system measures"
    assert '<p class="tf-ops-scroll-hint" data-tf-scroll-hint hidden>' in page, "Its hint waits for the measurement"


def _shared_and_unlinked(snapshot: dict) -> dict:
    """An older snapshot: paths without links, and the Haiku series given the Sonnet series' report."""
    older = json.loads(json.dumps(snapshot))
    for item in [i for b in older["benchmarks"] for i in b.get("evidence", [])] + older.get("method", []) + older["benchmark"].get("evidence", []):
        item.pop("href", None)
    haiku = next(b for b in older["benchmarks"] if "haiku" in b["label"] and b["label"].startswith("Standard"))
    haiku["evidence"] = [
        dict(i, path="docs/evidence/BENCHMARK_2026-09-22.md") if i["path"].endswith(".md") else i for i in haiku["evidence"]
    ]
    return older


def test_a_report_two_series_are_given_is_named_on_neither_card_and_each_path_opens(tmp_path: Path) -> None:
    """No card cites a report about another model, and every evidence path is a link.

    A snapshot built before each series was matched to its own report gave the
    Sonnet and the Haiku series one report, BENCHMARK_2026-09-22.md, whose
    Source rows line names the Sonnet rows, and the Haiku card cited it. A
    report two series with different rows are given is left off both cards and
    named once in the method card's Evidence list, and a card with no report of
    its own says where its report is found. Each path a snapshot gives without
    a link opens in the public repository.
    """
    older = _shared_and_unlinked(COMMITTED)
    out = proof(
        f"""
  answer = proofAnswer({json.dumps(older)});
  await visit('#/proof');
  out.view = view();
""",
        tmp_path,
    )
    page = out["view"]
    cards = page.split("How this was measured")[0]
    shared = "docs/evidence/BENCHMARK_2026-09-22.md"
    claims = [b for b in older["benchmarks"] if any(i.get("path") == shared for i in b.get("evidence", []))]
    assert len({b["source"] for b in claims}) > 1
    lists = re.findall(r'data-proof="series-evidence">(.*?)</ul>', cards, re.S)
    assert lists and not any(shared in markup for markup in lists), "A report two series are given is named on neither card"
    method = page.split("How this was measured")[1]
    assert method.count(shared + "</a>") == 1, "It is named once, in the method card's Evidence list"
    said = re.sub(r"\s+([,.])", r"\1", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cards)))
    for b in claims:
        rows = [i["path"] for i in b.get("evidence", []) if i["path"].endswith(".jsonl")]
        assert f"is the file in docs/evidence/ whose Source rows line names {', '.join(rows)}." in said, f"{b['label']} does not say where its report is"
    assert said.count("This snapshot names one report for this series and for a series with other result rows, so it is not listed here.") == len(claims)
    assert 'href="https://github.com/upgradedev/aws-threefold/tree/main/docs/evidence"' in cards
    for b in older["benchmarks"]:
        for item in b.get("evidence", []):
            assert f'href="https://github.com/upgradedev/aws-threefold/blob/main/{item["path"]}"' in page
    assert 'href="https://github.com/upgradedev/aws-threefold"' in method and "github.com/upgradedev/aws-threefold</a>, at the path shown" in method
    assert "each path opens it on the main branch" in method


def test_a_false_alarm_rate_over_a_handful_of_labels_is_said_as_a_count(tmp_path: Path) -> None:
    """0% over 5 labels read as a strong claim; below 30 labels the tile gives the count and says why."""
    out = proof(
        r"""
  const few = JSON.parse(JSON.stringify(MEASURED));
  Object.assign(few.private, { reviewed: 5, false_alarms: 0, false_alarm_rate: 0, would_refuse: 139 });
  answer = proofAnswer(few);
  await visit('#/proof');
  out.text = text(view());
  out.metrics = metrics(view());
""",
        tmp_path,
    )
    assert out["metrics"]["false_alarm_rate"] == "0 of 5"
    assert "too few labels for a rate: 5 of 139 would-refuse calls reviewed" in out["text"]
    assert "False-alarm rate" not in out["text"].split("How this was measured")[0]


def test_the_proof_says_its_figures_are_a_snapshot_and_its_times_are_read_in_utc(tmp_path: Path) -> None:
    out = proof(
        r"""
  answer = proofAnswer(MEASURED);
  await visit('#/proof');
  out.view = view();
  out.foot = el('foot-words').textContent;
  answer = contract();
  await visit('#/overview');
  out.overviewFoot = el('foot-words').textContent;
""",
        tmp_path,
    )
    assert out["foot"] == "The figures on this page come from a committed snapshot, served by the API"
    assert out["overviewFoot"] == "Every number on this page is read from the API as you look at it"
    assert '<time datetime="2026-09-29T08:00:00+00:00">29 Sep 2026, 08:00 UTC</time>' in out["view"]
    assert '<time datetime="2026-09-30">30 Sep 2026</time>' in out["view"]


# Each table's box, as a real browser lays it out: whether the table is wider
# than its box, and what the box then says about itself.
BOXES = r"""() => Array.from(document.querySelectorAll('[data-tf-scroll]')).map(b => {
  const hint = b.previousElementSibling;
  return {
    name: b.getAttribute('data-tf-scroll'), wide: b.scrollWidth > b.clientWidth + 1,
    tabindex: b.getAttribute('tabindex'), role: b.getAttribute('role'), label: b.getAttribute('aria-label'),
    hint: !!(hint && hint.hasAttribute('data-tf-scroll-hint') && !hint.hidden && hint.getClientRects().length > 0)
  };
})"""


@pytest.mark.parametrize("width", [1440, 375])
def test_a_table_box_is_a_stop_only_while_it_scrolls(tmp_path: Path, width: int) -> None:
    """At 1440 px every table fits and no box is a Tab stop; on a phone a box whose table is wider says it scrolls.

    Seven boxes on #/proof were once focusable regions announced "(scrolls
    sideways)" at every width, so a desk's Tab went through seven stops where
    nothing scrolls. The committed snapshot is served as it is on disk.
    """
    result = measure(
        "dashboard.html",
        tmp_path,
        width=width,
        height=900,
        replies={
            "/proof.json": {"status": 200, "body": COMMITTED},
            "/api/auth/whoami": {"status": 200, "body": {"authenticated": False, "via": None, "reads_public": True, "sandbox_writes": True}},
        },
        moments={"drawn": 1500},
        probe=BOXES,
        # No network reaches the Tailwind CDN here, so the page's one line of
        # Tailwind configuration is given something to configure.
        before="window.tailwind = {}; history.replaceState(null, '', '#/proof');",
    )
    boxes = result["taken"]["drawn"]
    series = [b for b in COMMITTED["benchmarks"] if b.get("conditions")]
    assert len(boxes) == 1 + len(series), "The series table and one table of conditions a series"
    for box in boxes:
        said = (box["tabindex"], box["role"], box["label"], box["hint"])
        if box["wide"]:
            assert said == ("0", "region", box["name"] + " (scrolls sideways)", True), f"{box['name']} scrolls, and says so"
        else:
            assert said == (None, None, None, False), f"{box['name']} fits, so it is not a stop and says nothing of scrolling"
    if width == 1440:
        assert not any(box["wide"] for box in boxes), "Nothing scrolls sideways on a desk"
    else:
        assert all(box["wide"] for box in boxes), "On a phone every one of these tables is wider than its box"
