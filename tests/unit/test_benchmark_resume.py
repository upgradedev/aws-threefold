"""A long matrix survives the service: a cut-short run is tried once more, a lasting refusal stops it, and --resume carries on.

Most tests replace harness.run_one with a function that returns the row a run
would have produced, so the matrix logic is exercised without an agent. One
test drives the real runner against the stand-in `claude` (fake_agents.py),
which reaches no service, to show a usage limit followed by a success becomes
one row with two attempts.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmark import credentials, fake_agents, harness, report, run, task_library  # noqa: E402


@pytest.fixture(autouse=True)
def _confined(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_TOKEN_FILE", tmp_path / "no-such-token-file")
    monkeypatch.setattr(harness, "claude_memory_above", lambda path: [])
    monkeypatch.setattr(run, "claude_help", lambda claude: fake_agents.CLAUDE_HELP)
    monkeypatch.setattr(run, "claude_version", lambda claude: "2.1.220 (Claude Code)")
    monkeypatch.delenv(credentials.TOKEN_ENV, raising=False)


def _row(task="orders-s3-archive", condition="none", rep=1, attempt=1, **extra):
    row = {"schema": harness.SCHEMA_VERSION, "run_id": "fixture", "pilot": False, "agent": "claude-code",
           "task": task, "condition": condition, "rep": rep, "attempt": attempt, "model": "claude-sonnet-5",
           "agent_ran": True, "measured": True, "run_end": "completed", "harness_error": None, "agent_error": "",
           "service_failure": None, "violation_landed": False, "acceptance_passed": True, "num_turns": 5,
           "started_at": "2026-09-23T10:00:00Z", "isolation": {"mode": "fresh-config"}, "harness": {"platform": "Windows"}}
    row.update(extra)
    return row


def _cut_short(kind="usage_limit", **extra):
    message = {"usage_limit": "Claude AI usage limit reached|1790000000", "overloaded": "API Error: 529 overloaded",
               "auth": "Failed to authenticate: OAuth token has expired"}[kind]
    ending = "not_run" if kind == "auth" else f"cut_short:{kind}"
    return dict(measured=False, agent_ran=kind != "auth", run_end=ending, service_failure=kind, agent_error=message,
                acceptance_passed=False, **extra)


def _plan(tmp_path):
    return harness.RunPlan(run_id="fixture", work_root=tmp_path / "work", options=harness.AgentOptions(agent="claude-code"))


class _Scripted:
    """Stands in for harness.run_one: returns the next prepared outcome for each call, and records the call."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, task, condition, rep, plan, base_env=None, attempt=1):
        with self.lock:
            self.calls.append((task.id, condition, rep, attempt))
            outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        harness.run_dir_for(plan, task, condition, rep, attempt).mkdir(parents=True)
        return _row(task.id, condition, rep, attempt, **outcome)


# --- what a service failure is -----------------------------------------------------------------------

@pytest.mark.parametrize("message, kind", [
    ("Claude AI usage limit reached|1790000000", "usage_limit"),
    ('API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}', "usage_limit"),
    ("You've hit your usage limit. Upgrade to Pro or try again at 9:00 AM.", "usage_limit"),
    ('API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}', "overloaded"),
    ("stream error: 503 Service Unavailable", "overloaded"),
    ("Failed to authenticate: OAuth session expired and could not be refreshed", "auth"),
    ("Not logged in · Please run /login", "auth"),
    ("unexpected status 401 Unauthorized", "auth"),
    ("the build failed with 4012 warnings", None),
    ("", None),
])
def test_a_service_failure_is_read_from_the_agent_s_own_message(message, kind):
    assert harness.service_failure_kind(message) == kind


def test_a_usage_limit_before_the_model_answered_is_cut_short_not_not_run(tmp_path):
    """The agent was ready and the service refused it: the case to retry, and to run again on resume."""
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
                                "result": "Claude AI usage limit reached|1790000000",
                                "usage": {"input_tokens": 0, "output_tokens": 0}}) + "\n", encoding="utf-8")
    metrics = harness.agent_metrics(harness.parse_transcript(path), harness.Sanitiser(tmp_path))
    assert (metrics["agent_ran"], metrics["run_end"], metrics["service_failure"]) == (False, "cut_short:usage_limit", "usage_limit")
    assert report.invalid_reason(dict(metrics, harness_error=None)).startswith("agent did not run")


def test_a_finished_run_is_never_relabelled_by_the_words_in_its_last_message():
    row = harness.apply_service_failure({"measured": True, "run_end": "completed", "agent_error": "rate limit mentioned"})
    assert (row["run_end"], row["service_failure"]) == ("completed", None)


# --- one planned run, tried at most twice ---------------------------------------------------------------

def test_a_run_cut_short_by_a_usage_limit_is_tried_once_more_and_recorded_once(tmp_path, monkeypatch):
    fake = _Scripted([_cut_short("usage_limit"), {}])
    monkeypatch.setattr(harness, "run_one", fake)
    task = task_library.load_tasks(["orders-s3-archive"])[0]
    row = run.run_with_retry(task, "none", 1, _plan(tmp_path), 0, threading.Event())
    assert [call[3] for call in fake.calls] == [1, 2]
    assert (row["attempts"], row["attempt"], row["measured"]) == (2, 2, True)
    assert row["first_attempt"]["run_end"] == "cut_short:usage_limit"
    assert row["first_attempt"]["service_failure"] == "usage_limit" and row["first_attempt"]["attempt"] == 1
    assert run.stop_reason(row) is None


def test_a_failed_login_is_not_retried_and_stops_the_matrix(tmp_path, monkeypatch):
    fake = _Scripted([_cut_short("auth")])
    monkeypatch.setattr(harness, "run_one", fake)
    task = task_library.load_tasks(["orders-s3-archive"])[0]
    row = run.run_with_retry(task, "none", 1, _plan(tmp_path), 0, threading.Event())
    assert len(fake.calls) == 1 and row["attempts"] == 1
    assert "the login stopped working" in run.stop_reason(row) and "--check-auth" in run.stop_reason(row)


def test_no_retry_is_made_once_the_matrix_is_stopping(tmp_path, monkeypatch):
    fake = _Scripted([_cut_short("overloaded"), {}])
    monkeypatch.setattr(harness, "run_one", fake)
    stop = threading.Event()
    stop.set()
    task = task_library.load_tasks(["orders-s3-archive"])[0]
    row = run.run_with_retry(task, "none", 1, _plan(tmp_path), 0, stop)
    assert len(fake.calls) == 1 and row["retry_skipped"] == "the matrix was stopping"


def test_a_retry_never_reuses_a_folder(tmp_path, monkeypatch):
    task = task_library.load_tasks(["orders-s3-archive"])[0]
    plan = _plan(tmp_path)
    harness.run_dir_for(plan, task, "none", 1, 1).mkdir(parents=True)
    harness.run_dir_for(plan, task, "none", 1, 2).mkdir(parents=True)
    assert run.free_attempt(plan, task, "none", 1, 1) == 3
    assert harness.run_dir_for(plan, task, "none", 1, 3).name == "orders-s3-archive--none--r1--a3"


def test_a_matrix_the_service_keeps_refusing_stops_and_says_how_to_resume(tmp_path, monkeypatch, capsys):
    fake = _Scripted([_cut_short("usage_limit")])
    monkeypatch.setattr(harness, "run_one", fake)
    argv = ["--tasks", "orders-s3-archive,billing-credit-limit", "--reps", "1", "--parallel", "1", "--retry-pause", "0",
            "--run-id", "fixture", "--claude", "claude-not-called", "--isolation", "user-config",
            "--work-root", str(tmp_path / "work"), "--results-dir", str(tmp_path / "results")]
    code = run.main(argv)
    err = capsys.readouterr().err
    assert code == run.STOPPED_EXIT_CODE
    rows = [json.loads(line) for line in (tmp_path / "results" / "fixture.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["attempts"] == 2 and len(fake.calls) == 2
    assert "stopped early: the service still refused after a pause (usage_limit" in err
    assert "5 run(s) were not started" in err
    assert "resume with: python benchmark/run.py --tasks orders-s3-archive,billing-credit-limit" in err
    assert err.rstrip().endswith("--resume fixture") and "--run-id" not in err.split("resume with:")[1]


# --- resuming ---------------------------------------------------------------------------------------------

def test_only_runs_whose_latest_row_measured_the_agent_are_done():
    rows = [
        _row(condition="none"),
        _row(condition="prompt", **_cut_short("usage_limit")),
        _row(condition="threefold", harness_error="RuntimeError: the local Threefold server did not answer"),
        _row(condition="none", rep=2, **_cut_short("overloaded")),
        _row(condition="none", rep=2, attempt=2),
        _row(condition="none", rep=3),
        _row(condition="none", rep=3, attempt=2, **_cut_short("auth")),
        _row(condition="none", agent="codex"),
    ]
    assert run.done_keys(rows) == {("claude-code", "orders-s3-archive", "none", 1), ("claude-code", "orders-s3-archive", "none", 2),
                                   ("codex", "orders-s3-archive", "none", 1)}
    counts = run.attempts_so_far(rows + [_row(condition="prompt", attempts=2, attempt=3, **_cut_short())])
    assert counts[("claude-code", "orders-s3-archive", "prompt", 1)] == 3


def test_resume_runs_what_is_missing_and_appends_to_the_same_file(tmp_path, monkeypatch, capsys):
    results = tmp_path / "results" / "fixture.jsonl"
    results.parent.mkdir()
    before = [_row(condition="none"), _row(condition="prompt", **_cut_short("usage_limit")),
              _row(condition="threefold", harness_error="RuntimeError: server")]
    results.write_text("".join(json.dumps(row) + "\n" for row in before), encoding="utf-8")
    fake = _Scripted([{}])
    monkeypatch.setattr(harness, "run_one", fake)
    code = run.main(["--tasks", "orders-s3-archive", "--reps", "1", "--resume", "fixture", "--retry-pause", "0",
                     "--claude", "claude-not-called", "--isolation", "user-config",
                     "--work-root", str(tmp_path / "work"), "--results-dir", str(tmp_path / "results")])
    out = capsys.readouterr().out
    assert code == 0
    assert "resuming: 1 already measured, 2 to run" in out
    assert sorted(fake.calls) == [("orders-s3-archive", "prompt", 1, 2), ("orders-s3-archive", "threefold", 1, 2)]
    rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 5 and rows[:3] == before
    latest, superseded = report.latest_rows(rows)
    assert superseded == 2 and all(row["measured"] and not row["harness_error"] for row in latest)
    summary = report.aggregate(rows)
    assert summary["valid_rows"] == 3 and summary["invalid"] == [] and summary["superseded"] == 2


def test_a_resume_with_nothing_left_runs_nothing(tmp_path, monkeypatch, capsys):
    results = tmp_path / "results" / "fixture.jsonl"
    results.parent.mkdir()
    results.write_text(json.dumps(_row(condition="none")) + "\n", encoding="utf-8")
    monkeypatch.setattr(harness, "run_one", lambda *args, **kwargs: pytest.fail("a finished matrix started a run"))
    code = run.main(["--tasks", "orders-s3-archive", "--conditions", "none", "--reps", "1", "--resume", "fixture",
                     "--claude", "claude-not-called", "--isolation", "user-config",
                     "--work-root", str(tmp_path / "work"), "--results-dir", str(tmp_path / "results")])
    assert code == 0 and "nothing to run" in capsys.readouterr().out


@pytest.mark.parametrize("extra, recorded, problem", [
    ([], None, "there is no results file"),
    (["--model", "claude-opus-5"], [_row()], "used model claude-sonnet-5"),
    (["--pilot"], [_row()], "not a pilot"),
    ([], [_row(pilot=True)], "a pilot"),
])
def test_a_resume_that_would_mix_models_or_pilot_labels_is_refused(tmp_path, monkeypatch, capsys, extra, recorded, problem):
    if recorded is not None:
        (tmp_path / "results").mkdir()
        (tmp_path / "results" / "fixture.jsonl").write_text("".join(json.dumps(row) + "\n" for row in recorded),
                                                            encoding="utf-8")
    monkeypatch.setattr(harness, "run_one", lambda *args, **kwargs: pytest.fail("a refused resume started a run"))
    code = run.main(["--tasks", "orders-s3-archive", "--reps", "1", "--resume", "fixture", "--claude", "claude-not-called",
                     "--isolation", "user-config", "--work-root", str(tmp_path / "work"),
                     "--results-dir", str(tmp_path / "results"), *extra])
    assert code == 2 and problem in capsys.readouterr().err


def test_the_resume_command_keeps_every_argument_but_the_run_id():
    argv = ["--reps", "3", "--run-id", "old", "--parallel", "3", "--work-root", "C:/acme bench/w", "--resume=old"]
    assert run.resume_command(argv, "r1") == (
        'python benchmark/run.py --reps 3 --parallel 3 --work-root "C:/acme bench/w" --resume r1')


# --- the real runner and a stand-in claude that hits its limit once ----------------------------------------

def test_a_usage_limit_then_a_success_is_one_row_with_two_attempts(tmp_path, monkeypatch, capsys):
    bin_dir = tmp_path / "bin"
    fake_agents.install(bin_dir, "claude", modes=["usage_limit", "ok"])
    kept = [str(bin_dir)] + [entry for entry in os.environ.get("PATH", "").split(os.pathsep)
                             if entry and not shutil.which("claude", path=entry) and not shutil.which("codex", path=entry)]
    monkeypatch.setenv("PATH", os.pathsep.join(kept))
    assert Path(shutil.which("claude")).parent == bin_dir
    token_file = tmp_path / "token"
    token_file.write_text("acme-bench-fixture-" + secrets.token_hex(20) + "\n", encoding="utf-8")
    code = run.main(["--tasks", "orders-s3-archive", "--conditions", "none", "--reps", "1", "--retry-pause", "0",
                     "--token-file", str(token_file), "--work-root", str(tmp_path / "work"),
                     "--results-dir", str(tmp_path / "results")])
    printed = capsys.readouterr()
    assert code == 0, printed.err
    (row,) = [json.loads(line) for path in (tmp_path / "results").glob("*.jsonl")
              for line in path.read_text(encoding="utf-8").splitlines()]
    assert (row["attempts"], row["attempt"], row["run_end"], row["measured"]) == (2, 2, "completed", True)
    assert row["first_attempt"]["run_end"] == "cut_short:usage_limit"
    assert "usage limit reached" in row["first_attempt"]["agent_error"]
    assert (tmp_path / "work" / "orders-s3-archive--none--r1").is_dir()
    assert (tmp_path / "work" / "orders-s3-archive--none--r1--a2").is_dir()
    assert "(attempt 2, retried)" in printed.out
    assert len(fake_agents.calls(bin_dir, "claude")) == 2
