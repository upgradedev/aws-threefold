"""The report computes every number from the rows, and says so when the rows cannot carry a headline."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmark import report, task_library  # noqa: E402


def _row(task="orders-s3-archive", condition="none", rep=1, violated=False, passed=True, **extra):
    row = {
        "schema": 1, "run_id": "fixture", "pilot": False, "agent": "claude-code", "task": task, "language": "python",
        "condition": condition, "rep": rep, "model": "claude-sonnet-5", "started_at": "2026-09-23T10:00:00Z",
        "agent_ran": True, "agent_error": "", "harness_error": None, "violation_landed": violated,
        "acceptance_passed": passed, "num_turns": 10, "duration_ms": 60000, "cost_usd": 0.5,
        "input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0,
        "hook_refusals": 0, "hook_refusals_by_kind": {}, "claude_code_version": "2.1.220",
        "isolation": {"mode": "fresh-config"}, "harness": {"platform": "Windows"},
        "refused_at_least_once": None, "self_corrected": None, "gave_up_after_refusal": None,
    }
    row.update(extra)
    return row


def _matrix():
    rows = []
    # none: 3 of 4 violate; prompt: 2 of 4; threefold: 0 of 4, one refused and gave up.
    for rep, violated in enumerate([True, True, True, False], start=1):
        rows.append(_row(condition="none", rep=rep, violated=violated))
    for rep, violated in enumerate([True, False, True, False], start=1):
        rows.append(_row(condition="prompt", rep=rep, violated=violated))
    for rep in range(1, 4):
        rows.append(_row(condition="threefold", rep=rep, hook_refusals=1, hook_refusals_by_kind={"LAYERING": 1},
                         refused_at_least_once=True, self_corrected=True, gave_up_after_refusal=False,
                         num_turns=14, cost_usd=0.75, duration_ms=90000))
    rows.append(_row(condition="threefold", rep=4, passed=False, hook_refusals=2, hook_refusals_by_kind={"LAYERING": 2},
                     refused_at_least_once=True, self_corrected=False, gave_up_after_refusal=True, num_turns=14,
                     cost_usd=0.75, duration_ms=90000))
    return rows


def test_wilson_matches_the_textbook_values():
    low, high = report.wilson(0, 10)
    assert low == 0.0 and high == pytest.approx(0.2775, abs=1e-3)
    low, high = report.wilson(5, 10)
    assert (low, high) == (pytest.approx(0.2366, abs=1e-3), pytest.approx(0.7634, abs=1e-3))
    assert report.wilson(0, 0) == (None, None)


def test_rates_come_from_the_rows():
    summary = report.aggregate(_matrix())
    stats = summary["by_condition"]
    assert list(stats) == ["none", "prompt", "threefold"]
    assert (stats["none"]["violation"]["k"], stats["none"]["violation"]["n"]) == (3, 4)
    assert stats["prompt"]["violation"]["rate"] == 0.5
    assert stats["threefold"]["violation"]["k"] == 0
    assert stats["threefold"]["completion"]["k"] == 3
    assert (stats["threefold"]["refused_runs"], stats["threefold"]["self_corrected"], stats["threefold"]["gave_up"]) == (4, 3, 1)
    assert stats["threefold"]["refusal_kinds"] == {"LAYERING": 5}
    assert stats["threefold"]["turns_mean"] == 14 and stats["none"]["seconds_mean"] == 60
    assert stats["none"]["clean_completion"]["k"] == 1


def test_the_headline_is_computed_not_typed():
    sentence = report.headline(report.aggregate(_matrix()))
    assert sentence.startswith("Across 12 runs of claude-sonnet-5 on 1 Acme task(s)")
    assert "75% (3/4) of runs with no guidance" in sentence
    assert "50% (2/4) with the rules in CLAUDE.md" in sentence
    assert "0% (0/4) with Threefold enforcing" in sentence
    assert "passed in 100% (4/4), 100% (4/4) and 75% (3/4) of those runs" in sentence


def test_a_run_that_never_reached_the_model_is_left_out_and_explained():
    rows = _matrix() + [_row(condition="none", rep=9, agent_ran=False, violated=False, passed=False,
                             agent_error="Failed to authenticate: OAuth session expired")]
    summary = report.aggregate(rows)
    assert summary["by_condition"]["none"]["n"] == 4
    assert summary["invalid"][0]["reason"].startswith("agent did not run: Failed to authenticate")


def test_no_headline_when_nothing_was_measured():
    rows = [_row(condition=name, agent_ran=False, passed=False, pilot=True, agent_error="Failed to authenticate: expired")
            for name in ("none", "prompt", "threefold")]
    summary = report.aggregate(rows)
    sentence = report.headline(summary)
    assert sentence.startswith("PILOT, not a result: No headline: none of the 3 real-agent run(s) produced a measurement.")
    assert "Failed to authenticate" in sentence


def test_no_headline_when_a_condition_is_missing():
    rows = [row for row in _matrix() if row["condition"] != "prompt"]
    assert report.headline(report.aggregate(rows)) == "No headline: no valid runs yet under rules in CLAUDE.md."


def test_scripted_rows_never_enter_a_rate_or_the_headline():
    scripted = [_row(condition=name, agent="scripted", violated=name == "none") for name in ("none", "prompt", "threefold")]
    summary = report.aggregate(_matrix() + scripted)
    assert summary["by_condition"]["none"]["n"] == 4
    assert summary["scripted_rows"] == 3
    assert summary["scripted"]["none"]["violation"]["k"] == 1


def test_a_pilot_is_labelled_everywhere(tmp_path):
    rows = [dict(row, pilot=True) for row in _matrix()]
    summary = report.aggregate(rows)
    assert summary["pilot"]
    assert report.headline(summary).startswith("PILOT, not a result: Across")
    text = report.render(summary, task_library.load_tasks(), ["benchmark/results/fixture.jsonl"])
    assert text.startswith("# Agent benchmark — PILOT, 2026-09-23")
    assert "**PILOT.**" in text
    assert report.default_output(summary).name == "BENCHMARK_2026-09-23-PILOT.md"


def test_the_report_file_is_written_from_jsonl(tmp_path):
    results = tmp_path / "fixture.jsonl"
    results.write_text("\n".join(json.dumps(row) for row in _matrix()) + "\n", encoding="utf-8")
    out = tmp_path / "BENCHMARK_test.md"
    assert report.main([str(results), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    for section in ("## Headline", "## Method", "## Results by condition", "## Results by task", "## Limits", "## Reproduce"):
        assert section in text
    assert "Overhead of Threefold against no guidance" in text and "turns 1.40x" in text
    assert "| `orders-s3-archive` | python | python-domain-stays-pure | 3/4 violated · 4/4 passed |" in text


def test_the_limits_say_what_isolation_did_not_cover():
    rows = [dict(row, isolation={"mode": "user-config"}) for row in _matrix()]
    joined = " ".join(report.caveats(report.aggregate(rows)))
    assert "user-level CLAUDE.md" in joined and "not verified" in joined


def test_a_broken_line_names_its_file(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bad.jsonl:1"):
        report.load_rows([bad])
