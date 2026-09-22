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
    assert sentence.startswith("Across 12 Claude Code runs of claude-sonnet-5 on 1 Acme task(s)")
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


def test_a_threefold_run_whose_hook_never_fired_is_not_a_measurement():
    rows = _matrix() + [_row(condition="threefold", rep=9, hook_fired=False, hook_missing=True, governed_calls=4)]
    summary = report.aggregate(rows)
    assert summary["by_condition"]["threefold"]["n"] == 4
    assert summary["invalid"][0]["reason"].startswith("the Threefold hook never fired although the agent made 4 governed call(s)")


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


def test_the_limits_say_what_isolation_covered_and_what_it_did_not():
    rows = [dict(row, isolation={"mode": "user-config"}) for row in _matrix()]
    joined = " ".join(report.caveats(report.aggregate(rows)))
    assert "user-level CLAUDE.md out" in joined and "This is not a sandbox" in joined
    assert "No run had one above its work root." in joined
    # The sentences the reviewer found false are gone.
    assert "refused by the permission list, and the Threefold" not in joined
    assert "so an agent cannot pass by editing them" not in joined


def test_a_claude_md_above_the_work_root_is_reported():
    rows = [dict(row, isolation={"mode": "fresh-config", "claude_md_above_work_root": ["~\\.claude\\CLAUDE.md"]})
            for row in _matrix()]
    joined = " ".join(report.caveats(report.aggregate(rows)))
    assert f"{len(rows)} run(s) had one above their work root and may have read it: ~\\.claude\\CLAUDE.md." in joined


def test_runs_stopped_at_the_timeout_are_counted_and_explained():
    rows = _matrix() + [_row(condition="none", rep=5, agent_timed_out=True, run_end="timeout", measured=True,
                             num_turns=None, cost_usd=None, duration_ms=None, wall_seconds=1200.0, passed=False)]
    summary = report.aggregate(rows)
    assert summary["by_condition"]["none"]["n"] == 5 and summary["by_condition"]["none"]["timed_out"] == 1
    assert "1 valid run(s) were stopped at the per-run timeout" in " ".join(report.caveats(summary))


def test_a_run_the_service_cut_short_is_left_out():
    rows = _matrix() + [_row(condition="prompt", rep=7, run_end="cut_short:api_error", measured=False,
                             agent_error="API Error: 529 overloaded")]
    summary = report.aggregate(rows)
    assert summary["by_condition"]["prompt"]["n"] == 4
    assert summary["invalid"][0]["reason"] == "the run was cut short (api_error), not by the agent: API Error: 529 overloaded"


def test_the_matrix_estimate_gives_its_caps_and_says_when_it_is_only_an_estimate():
    blocked = report.aggregate([_row(condition=name, agent_ran=False, passed=False, pilot=True) for name in ("none", "prompt")])
    text = report.matrix_estimate(blocked)
    assert "54 runs" in text and "18 rounds" in text and "about 6 hours" in text and "$270" in text
    assert "ESTIMATE, not measured" in text
    measured = report.aggregate([dict(row, total_seconds=300.0) for row in _matrix()])
    text = report.matrix_estimate(measured)
    assert "about 1.5 hours" in text and "about $32" in text and "ESTIMATE" not in text


def test_a_login_failure_tells_the_owner_how_to_log_in():
    rows = [_row(condition=name, agent_ran=False, passed=False, pilot=True, measured=False, run_end="not_run",
                 agent_error="Not logged in · Please run /login") for name in ("none", "prompt", "threefold")]
    text = report.render(report.aggregate(rows), task_library.load_tasks(), ["fixture.jsonl"])
    assert "claude auth login" in text and "claude setup-token" in text


def test_a_broken_line_names_its_file(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bad.jsonl:1"):
        report.load_rows([bad])


# --- one row per planned run, and one agent at a time ---------------------------------------------

def test_only_the_latest_row_of_a_run_counts():
    """A run the service cut short and a resume ran again appears twice; only the second is the run."""
    rows = _matrix()
    rows.insert(0, _row(condition="none", rep=1, agent_ran=False, measured=False, run_end="cut_short:usage_limit",
                        passed=False, agent_error="Claude AI usage limit reached"))
    latest, superseded = report.latest_rows(rows)
    assert len(latest) == 12 and superseded == 1
    summary = report.aggregate(rows)
    assert summary["by_condition"]["none"]["n"] == 4 and summary["invalid"] == [] and summary["superseded"] == 1
    assert "1 earlier row(s) of runs that were run again after a resume" in " ".join(report.caveats(summary))


def _codex(rows):
    return [dict(row, agent="codex", model="gpt-acme", cost_usd=None, agent_version="codex-cli 0.155.0") for row in rows]


def test_two_agents_are_never_pooled():
    codex = _codex([_row(condition=name, rep=rep, violated=name == "none") for name in ("none", "prompt", "threefold")
                    for rep in (1, 2)])
    summary = report.aggregate(_matrix() + codex)
    assert summary["agents"] == ["claude-code", "codex"]
    assert summary["by_agent"]["claude-code"]["by_condition"]["none"]["n"] == 4
    assert summary["by_agent"]["codex"]["by_condition"]["none"]["n"] == 2
    assert summary["by_agent"]["codex"]["by_condition"]["none"]["violation"]["k"] == 2
    sentence = report.headline(summary)
    assert sentence.startswith("Claude Code: Across 12 Claude Code runs of claude-sonnet-5")
    assert "Codex: Across 6 Codex runs of gpt-acme" in sentence and "with the rules in AGENTS.md" in sentence
    text = report.render(summary, task_library.load_tasks(), ["fixture.jsonl"])
    for heading in ("## Claude Code: results by condition", "## Codex: results by condition", "## Codex: results by task"):
        assert heading in text
    assert "| | no guidance | rules in AGENTS.md | Threefold enforcing |" in text
    assert "`.codex/hooks.json` (Codex)" in text
    limits = " ".join(report.caveats(summary))
    assert "never pooled" in limits and "Codex runs (`codex exec --json`)" in limits and "Codex reports tokens but no cost" in limits
    # Both reach statements stand: the Claude Code denials are not read as covering Codex.
    assert "What Claude Code could reach" in limits and "What Codex could reach" in limits
    assert "were not denied by name as they are for Claude Code" in limits


def test_a_codex_only_report_speaks_of_agents_md_and_codex_s_reach():
    summary = report.aggregate(_codex(_matrix()))
    assert summary["agent"] == "codex" and "by_agent" not in summary
    text = report.render(summary, task_library.load_tasks(), ["fixture.jsonl"])
    assert "## Results by condition" in text and "rules in AGENTS.md" in text and "rules in CLAUDE.md" not in text
    limits = " ".join(report.caveats(summary))
    assert "What Codex could reach" in limits and "What Claude Code could reach" not in limits
    assert "Antigravity is not measured here" in limits


def test_a_report_of_the_scripted_stand_in_alone_claims_no_agent():
    scripted = [_row(condition=name, agent="scripted", model="scripted") for name in ("none", "prompt", "threefold")]
    summary = report.aggregate(scripted)
    assert summary["agents"] == [] and summary["agent"] is None
    text = report.render(summary, task_library.load_tasks(), ["fixture.jsonl"])
    assert "## Harness self-test (scripted agent, not a measurement)" in text
    assert "Agents: none measured" in text and "No headline: there are no real-agent runs" in text
    assert report.build_summary(scripted, ["fixture.jsonl"])["agents"] == {}


def test_a_single_agent_report_keeps_its_headings():
    text = report.render(report.aggregate(_matrix()), task_library.load_tasks(), ["fixture.jsonl"])
    assert "## Results by condition" in text and "## Results by task" in text and "Claude Code:" not in text


# --- the summary file ----------------------------------------------------------------------------------

def test_the_summary_carries_every_figure_per_agent_and_condition():
    rows = _matrix()
    document = report.build_summary(rows, ["benchmark/results/fixture.jsonl"])
    assert (document["schema"], document["kind"]) == (1, "threefold-benchmark-summary")
    assert document["run_ids"] == ["fixture"] and document["date"] == "2026-09-23" and document["pilot"] is False
    assert document["headline"] == report.headline(report.aggregate(rows))
    block = document["agents"]["claude-code"]
    assert (block["model"], block["valid_rows"], block["invalid_rows"]) == ("claude-sonnet-5", 12, 0)
    assert block["headline"] == document["headline"]
    none, prompt, threefold = (block["conditions"][name] for name in ("none", "prompt", "threefold"))
    for entry in (none, prompt, threefold):
        assert (entry["agent"], entry["model"], entry["date"], entry["pilot"], entry["n"]) == (
            "claude-code", "claude-sonnet-5", "2026-09-23", False, 4)
    assert (none["violation_rate"], prompt["violation_rate"], threefold["violation_rate"]) == (0.75, 0.5, 0.0)
    assert (none["completion_rate"], threefold["completion_rate"]) == (1.0, 0.75)
    assert none["violation_ci95"] == [pytest.approx(0.3006, abs=1e-4), pytest.approx(0.9544, abs=1e-4)]
    assert none["self_correction_rate"] is None and threefold["self_correction_rate"] == 0.75
    assert (threefold["refused_runs"], threefold["self_corrected"], threefold["gave_up"]) == (4, 3, 1)
    assert threefold["overhead"]["turns_median"] == 14 and none["overhead"]["turns_median"] == 10
    assert threefold["overhead"]["seconds_median"] == 90 and threefold["overhead"]["cost_usd_median"] == 0.75
    assert threefold["overhead"]["tokens_median"] == 1150
    assert threefold["overhead"]["vs_none_median_ratio"] == {"turns": 1.4, "seconds": 1.5, "cost_usd": 1.5, "tokens": 1.0}
    assert none["overhead"]["vs_none_median_ratio"] is None
    assert json.loads(json.dumps(document)) == document


def test_the_summary_keeps_each_agent_apart_and_says_when_nothing_was_measured():
    codex = _codex([_row(condition=name, pilot=True) for name in ("none", "prompt", "threefold")])
    blocked = [_row(condition=name, pilot=True, agent_ran=False, measured=False, run_end="not_run", passed=False,
                    agent_error="Not logged in · Please run /login") for name in ("none", "prompt", "threefold")]
    document = report.build_summary(blocked + codex, ["a.jsonl", "b.jsonl"])
    assert list(document["agents"]) == ["claude-code", "codex"] and document["pilot"] is True
    claude = document["agents"]["claude-code"]
    assert claude["valid_rows"] == 0 and claude["invalid_rows"] == 3
    assert list(claude["conditions"]) == ["none", "prompt", "threefold"]
    assert all(entry["n"] == 0 and entry["violation_rate"] is None and entry["self_correction_rate"] is None
               for entry in claude["conditions"].values())
    assert claude["headline"].startswith("PILOT, not a result: No headline: none of the 3 real-agent run(s)")
    assert document["agents"]["codex"]["conditions"]["none"]["model"] == "gpt-acme"
    assert document["agents"]["codex"]["conditions"]["none"]["overhead"]["cost_usd_median"] is None
    assert document["headline"].startswith("Claude Code: PILOT, not a result: No headline")


def test_the_report_writes_the_summary_beside_the_rows_and_says_where(tmp_path, capsys):
    results = tmp_path / "20260923T100000Z.jsonl"
    results.write_text("\n".join(json.dumps(row) for row in _matrix()) + "\n", encoding="utf-8")
    assert report.main([str(results), "--out", str(tmp_path / "report.md")]) == 0
    written = tmp_path / "20260923T100000Z-summary.json"
    assert f"summary: {written}" in capsys.readouterr().out
    document = json.loads(written.read_text(encoding="utf-8"))
    assert document["agents"]["claude-code"]["conditions"]["prompt"]["violation_rate"] == 0.5
    elsewhere = tmp_path / "elsewhere" / "proof.json"
    assert report.main([str(results), "--out", str(tmp_path / "report.md"), "--summary", str(elsewhere)]) == 0
    assert json.loads(elsewhere.read_text(encoding="utf-8"))["sources"] == [results.name]


def test_each_agent_is_dated_by_its_own_runs():
    """Claude Code measured now, Codex from 2026-09-27: each block, and each of its conditions, carries its own date."""
    codex = _codex([_row(condition=name, started_at="2026-09-28T09:00:00Z") for name in ("none", "prompt", "threefold")])
    document = report.build_summary(_matrix() + codex, ["a.jsonl", "b.jsonl"])
    assert document["date"] == "2026-09-28"
    claude, later = document["agents"]["claude-code"], document["agents"]["codex"]
    assert claude["date"] == "2026-09-23" and later["date"] == "2026-09-28"
    assert {entry["date"] for entry in claude["conditions"].values()} == {"2026-09-23"}
    assert {entry["date"] for entry in later["conditions"].values()} == {"2026-09-28"}
    scripted = [_row(condition="none", agent="scripted", model="scripted", started_at="2026-09-30T08:00:00Z")]
    assert report.build_summary(_matrix() + scripted, ["a.jsonl"])["agents"]["claude-code"]["date"] == "2026-09-23"


def test_one_agent_s_pilot_and_its_other_runs_are_never_pooled(tmp_path, capsys):
    pilot = [dict(row, run_id="pilot-run", pilot=True) for row in _matrix()[:3]]
    full = [dict(row, run_id="full-run") for row in _matrix()]
    problem = report.mixed_pilot_problem(pilot + full)
    assert "the Claude Code rows hold 3 pilot row(s) and 12 that are not" in problem
    with pytest.raises(ValueError):
        report.build_summary(pilot + full, ["a.jsonl", "b.jsonl"])
    first, second = tmp_path / "pilot-run.jsonl", tmp_path / "full-run.jsonl"
    first.write_text("".join(json.dumps(row) + "\n" for row in pilot), encoding="utf-8")
    second.write_text("".join(json.dumps(row) + "\n" for row in full), encoding="utf-8")
    code = report.main([str(first), str(second), "--out", str(tmp_path / "report.md")])
    assert code == 2 and "refused: the Claude Code rows hold 3 pilot row(s)" in capsys.readouterr().err
    assert not (tmp_path / "report.md").exists() and not (tmp_path / "pilot-run-summary.json").exists()

    # Two agents are never pooled, so a Codex pilot may stand beside a Claude Code result, each labelled.
    codex_pilot = _codex([dict(row, pilot=True) for row in _matrix()])
    assert report.mixed_pilot_problem(full + codex_pilot) is None
    document = report.build_summary(full + codex_pilot, ["full-run.jsonl", "codex.jsonl"])
    assert (document["pilot"], document["agents"]["claude-code"]["pilot"], document["agents"]["codex"]["pilot"]) == (
        False, False, True)
    assert document["agents"]["codex"]["headline"].startswith("PILOT, not a result: ")
