"""The threefold condition against a remote Threefold: the hook names the endpoint, project and session given, and
the run's decisions are read back from that Threefold's GET /api/decisions, never from anywhere else.

No agent and no network: the agents are the harness's stand-ins (the scripted
agent, and the fake `claude` of benchmark/fake_agents.py), and the remote
Threefold is benchmark/fake_threefold.py or a real server from this
repository's source, both listening on 127.0.0.1. Every folder a run could
touch is under the test's own tmp_path, and the owner's token file is never
read.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmark import credentials, fake_agents, harness, report, run, task_library  # noqa: E402
from benchmark.fake_threefold import FakeThreefold  # noqa: E402

DAY = "2026-09-26"
TASK = "orders-s3-archive"
PROJECT = "Acme-Live-orders-s3-archive"
SESSION = f"live-orders-s3-archive-{DAY}"


@pytest.fixture(autouse=True)
def _confined(tmp_path, monkeypatch):
    """The owner's token file is never read, a CLAUDE.md above tmp_path is not this machine's business, and Codex's
    home is the test's own."""
    monkeypatch.setattr(credentials, "DEFAULT_TOKEN_FILE", tmp_path / "no-such-token-file")
    monkeypatch.setattr(harness, "claude_memory_above", lambda path: [])
    (tmp_path / "codex-home").mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))


def _plan(tmp_path, endpoint, agent="scripted"):
    options = harness.AgentOptions(agent=agent, max_turns=10, timeout_s=300, budget_usd=0.0, isolation="user-config")
    return harness.RunPlan(run_id="suite", work_root=tmp_path / "work", options=options, home=tmp_path / "owner-home",
                           remote=harness.RemoteThreefold(endpoint, DAY, ledger_wait_s=0))


def _confined_env(tmp_path):
    env = dict(os.environ)
    env.update({"HOME": str(tmp_path / "home"), "USERPROFILE": str(tmp_path / "home"),
                "THREEFOLD_HOME": str(tmp_path / "threefold-home"), "THREEFOLD_ENDPOINT": "https://owner.acme.test/",
                "THREEFOLD_PROJECT": "Acme-Owner"})
    return env


def _task():
    return task_library.load_tasks([TASK])[0]


# --- the endpoint, the project and the session ------------------------------------------------------

@pytest.mark.parametrize("given, expected", [
    ("https://threefold.acme.test", "https://threefold.acme.test/"),
    ("https://Threefold.Acme.Test/prod", "https://threefold.acme.test/prod/"),
    ("  https://threefold.acme.test:8443/prod/  ", "https://threefold.acme.test:8443/prod/"),
    ("http://127.0.0.1:8123", "http://127.0.0.1:8123/"),
    ("http://localhost:8123/x/", "http://localhost:8123/x/"),
])
def test_an_endpoint_is_https_or_this_machine_and_keeps_its_path(given, expected):
    assert harness.remote_endpoint(given) == expected


@pytest.mark.parametrize("given, words", [
    ("http://threefold.acme.test/", "https"),
    ("ftp://threefold.acme.test/", "https"),
    ("threefold.acme.test", "https"),
    ("https://user:secret@threefold.acme.test/", "user name or password"),
    ("https://token@threefold.acme.test/", "user name or password"),
    ("https://threefold.acme.test/?project=x", "query"),
    ("https://threefold.acme.test/#top", "fragment"),
    ("https:///prod/", "no host"),
    ("https://threefold.acme.test/pro d/", "spaces"),
    ("https://threefold.acme.test:port/", "port"),
    ("", "spaces"),
])
def test_an_endpoint_that_is_not_one_is_refused(given, words):
    with pytest.raises(ValueError, match=words):
        harness.remote_endpoint(given)


def test_plain_http_to_this_machine_is_only_for_a_stand_in():
    with pytest.raises(ValueError, match="must be https"):
        harness.remote_endpoint("http://127.0.0.1:8123/", allow_loopback_http=False)


def test_every_standard_task_has_a_live_project_the_stack_shows_and_a_session_per_run():
    remote = harness.RemoteThreefold("https://threefold.acme.test/", DAY)
    for task in task_library.load_tasks(family="standard"):
        assert harness.PROJECT_PATTERN.match(remote.project(task))
        assert remote.project(task) == f"Acme-Live-{task.id}"
    task = _task()
    assert remote.session(task) == SESSION
    assert remote.session(task, rep=2) == f"{SESSION}-r2" and remote.session(task, attempt=2) == f"{SESSION}-a2"
    with pytest.raises(ValueError, match="project pattern"):
        harness.live_project("x" * 40)
    with pytest.raises(ValueError):
        harness.RemoteThreefold("https://threefold.acme.test/", "26/09/2026")
    with pytest.raises(ValueError):
        harness.RemoteThreefold("https://threefold.acme.test/", "2026-02-30")


# --- the wrapper sends every call under the run's session ----------------------------------------------

ECHO_HOOK = '''import json, sys
raw = sys.stdin.buffer.read()
sys.stdout.write(json.dumps({"seen": raw.decode("utf-8")}) + "\\n")
'''


def _through_wrapper(tmp_path, session, text):
    hook = tmp_path / "echo_hook.py"
    hook.write_text(ECHO_HOOK, encoding="utf-8")
    wrapper = harness.write_hook_wrapper(tmp_path / "run", hook, session=session)
    completed = subprocess.run([sys.executable, str(wrapper)], input=text.encode("utf-8"), capture_output=True,
                               timeout=60, env=harness.base_environment(os.environ, tmp_path / "run"))
    return json.loads(completed.stdout.decode("utf-8").strip().splitlines()[-1])["seen"]


def test_the_wrapper_sends_every_call_under_the_live_session(tmp_path):
    call = {"session_id": "agent-own-id", "conversationId": "agent-own-id", "tool_name": "Write", "tool_input": {"x": "é"}}
    seen = json.loads(_through_wrapper(tmp_path, SESSION, json.dumps(call)))
    assert seen["session_id"] == seen["conversationId"] == SESSION
    assert seen["tool_input"] == {"x": "é"} and seen["tool_name"] == "Write"
    without_id = json.loads(_through_wrapper(tmp_path, SESSION, json.dumps({"tool_name": "Bash"})))
    assert without_id == {"tool_name": "Bash", "session_id": SESSION}


def test_the_wrapper_passes_anything_else_on_untouched_and_changes_nothing_without_a_session(tmp_path):
    assert _through_wrapper(tmp_path, SESSION, "not json at all") == "not json at all"
    assert _through_wrapper(tmp_path, SESSION, "[1, 2]") == "[1, 2]"
    original = json.dumps({"session_id": "agent-own-id", "tool_name": "Write"})
    assert _through_wrapper(tmp_path, None, original) == original


# --- whole runs against a stand-in remote Threefold ----------------------------------------------------

def test_a_remote_run_reports_to_the_endpoint_given_and_reads_its_session_back(tmp_path):
    """The owner's THREEFOLD_ENDPOINT and THREEFOLD_PROJECT in the environment reach nothing: every call goes to the
    endpoint given, as the live project and session, and the ledger is read back from there alone."""
    with FakeThreefold(stage="enforce") as fake:
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, fake.endpoint), _confined_env(tmp_path))
    assert row["harness_error"] is None, row["harness_error"]
    assert (row["ledger_source"], row["threefold_endpoint"]) == ("remote", fake.endpoint)
    assert (row["threefold_project"], row["threefold_session"]) == (PROJECT, SESSION)

    evaluations = fake.evaluations()
    assert evaluations, "the hook sent nothing to the remote Threefold"
    for body in evaluations:
        assert (body["project_name"], body["session_id"]) == (PROJECT, SESSION)
        assert (body["agent"], body["origin"], body["hook_mode"], body["dry_run"]) == ("claude-code", "hook", "enforce", False)
    assert set(fake.paths()) == {"/status", "/evaluate-tool-call", "/api/decisions"}
    reads = [item["query"] for item in fake.requests if item["path"] == "/api/decisions"]
    assert len(reads) >= 2, "the fake answers two rows a page, so the cursor must have been followed"
    for query in reads:
        assert (query["project"], query["session"], query["days"]) == (PROJECT, SESSION, "2")

    ledger = row["ledger"]
    assert (ledger["source"], ledger["reachable"], ledger["complete"]) == ("remote", True, True)
    assert ledger["decisions"] == len(evaluations) and ledger["refused"] == 1 and ledger["would_refuse"] == 0
    assert ledger["stages"] == {"enforce": len(evaluations)} and ledger["by_rule_key"] == {"python-domain-stays-pure": 1}
    assert (row["server_healthy_after"], row["threefold_config_intact"], row["project_stage_cached"]) == (True, True, "enforce")
    assert row["hook_refusals_by_kind"] == {"LAYERING": 1}
    assert (row["violation_landed"], row["acceptance_passed"], row["governance_problem"]) == (False, True, None)
    assert (row["refused_at_least_once"], row["self_corrected"]) == (True, True)
    assert report.is_valid(dict(row, agent="claude-code"))

    config = json.loads((tmp_path / "work" / f"{TASK}--threefold--r1" / "repo" / ".threefold.json").read_text(encoding="utf-8"))
    assert config == {"project": PROJECT, "endpoint": fake.endpoint, "mode": "enforce"}
    assert not (tmp_path / "threefold-home").exists(), "the owner's THREEFOLD_HOME was touched"


def test_a_project_the_remote_stack_holds_in_observe_is_recorded_but_not_counted_as_enforcing(tmp_path):
    """The public stack's default stage is Observe: every call recorded, none refused. The row says so, the violation
    that landed is not charged to Threefold, and a call that ran is `would_refuse`, never a refusal."""
    with FakeThreefold(stage="observe") as fake:
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, fake.endpoint), _confined_env(tmp_path))
    assert row["harness_error"] is None, row["harness_error"]
    ledger = row["ledger"]
    assert ledger["refused"] == 0 and ledger["would_refuse"] == 1 and set(ledger["stages"]) == {"observe"}
    assert row["project_stage_cached"] == "observe" and row["violation_landed"] is True
    assert "in Observe" in row["governance_problem"] and PROJECT in row["governance_problem"]
    assert "did not measure Threefold enforcing" in row["governance_problem"]
    assert not report.is_valid(dict(row, agent="claude-code"))
    assert report.invalid_reason(dict(row, agent="claude-code")) == row["governance_problem"]


def test_a_remote_threefold_that_does_not_answer_starts_no_agent(tmp_path):
    with FakeThreefold(status_code=503) as fake:
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, fake.endpoint), _confined_env(tmp_path))
    assert "answered GET status with 503" in row["harness_error"] and "no agent was started" in row["harness_error"]
    assert fake.paths() == ["/status"]
    run_dir = tmp_path / "work" / f"{TASK}--threefold--r1"
    assert not (run_dir / "transcript.jsonl").exists()
    assert not report.is_valid(dict(row, agent="claude-code"))


def test_the_ledger_is_never_read_through_a_redirect(tmp_path):
    with FakeThreefold() as elsewhere, FakeThreefold(redirect_decisions_to=None) as fake:
        fake.redirect_decisions_to = elsewhere.endpoint + "api/decisions"
        ledger = harness.RemoteServer(fake.endpoint, PROJECT, SESSION, wait_s=0).ledger()
    assert (ledger["source"], ledger["reachable"], ledger["status"]) == ("remote", False, 302)
    assert elsewhere.requests == []
    row = {"condition": "threefold", "ledger_source": "remote", "ledger": ledger, "server_healthy_after": True}
    assert harness.governance_problem("threefold", row) == "the remote Threefold's ledger could not be read when the agent stopped"


def test_a_ledger_that_never_ends_is_read_as_incomplete_not_as_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "MAX_LEDGER_PAGES", 3)
    with FakeThreefold(endless=True) as fake:
        ledger = harness.RemoteServer(fake.endpoint, PROJECT, SESSION, wait_s=0).ledger()
    assert (ledger["reachable"], ledger["complete"], ledger["pages"]) == (True, False, 3)
    assert len(fake.paths("GET")) == 3
    row = {"condition": "threefold", "ledger_source": "remote", "ledger": ledger, "server_healthy_after": True}
    assert "was not read to the end" in harness.governance_problem("threefold", row)


def test_an_empty_answer_is_read_again_only_when_the_run_made_governed_calls(tmp_path):
    """A store may answer a read made just after a write without it; waiting a little is cheaper than a lost run."""
    slept = []
    with FakeThreefold(hidden_reads=2) as fake:
        fake.rows.append({"session_id": SESSION, "project_name": PROJECT, "status": "APPROVED", "rule_key": "NONE",
                          "stage": "enforce"})
        server = harness.RemoteServer(fake.endpoint, PROJECT, SESSION, wait_s=7, rereads=3, sleep=slept.append)
        assert server.ledger(wait_for_rows=False)["decisions"] == 0 and slept == []
        assert server.ledger(wait_for_rows=True)["decisions"] == 1 and slept == [7]


def test_rows_of_other_sessions_or_projects_are_never_counted(tmp_path):
    """Filtered by the query, and again on arrival: a server that sent more than was asked for adds nothing."""
    rows = [{"session_id": SESSION, "project_name": PROJECT, "status": "BLOCKED_BOUNDARY_VIOLATION",
             "rule_key": "python-domain-stays-pure", "rule": "ARCHITECTURAL_BOUNDARY_SAFE", "category": "LAYERING",
             "stage": "enforce"},
            {"session_id": "fleet-payments-codex-1", "project_name": "Acme-Payments", "status": "BLOCKED_LOOP_DETECTED",
             "rule_key": "LOOP", "stage": "enforce"},
            {"session_id": SESSION, "project_name": "Acme-Other", "status": "APPROVED", "rule_key": "NONE", "stage": "enforce"}]

    class Loose(FakeThreefold):
        def _decisions(self, query):
            return {"items": rows, "next_cursor": None}

    with Loose() as fake:
        ledger = harness.RemoteServer(fake.endpoint, PROJECT, SESSION, wait_s=0).ledger()
    assert (ledger["decisions"], ledger["refused"], ledger["sessions"]) == (1, 1, 1)
    assert ledger["by_category"] == {"LAYERING": 1}


def test_a_changed_threefold_json_and_an_unreachable_endpoint_are_named_as_remote():
    base = {"condition": "threefold", "ledger_source": "remote", "server_healthy_after": True,
            "ledger": {"reachable": True, "complete": True, "decisions": 3, "stages": {"enforce": 3}, "refused": 1}}
    assert harness.governance_problem("threefold", base) is None
    changed = dict(base, threefold_config_intact=False)
    assert ".threefold.json no longer named the endpoint" in harness.governance_problem("threefold", changed)
    down = dict(base, server_healthy_after=False)
    assert harness.governance_problem("threefold", down) == "the remote Threefold was not answering when the agent stopped"
    unjudged = dict(base, hook_unjudged=2)
    assert "could not reach the remote Threefold for 2 call(s)" in harness.governance_problem("threefold", unjudged)
    # The local wording is unchanged.
    local = dict(base, ledger_source="local", server_healthy_after=False)
    assert harness.governance_problem("threefold", local) == "the local Threefold server was not answering when the agent stopped"


# --- against a real Threefold from this repository's source, as the remote ---------------------------------

class _ServerAsRemote(harness.LocalServer):
    """A server from this repository's source on 127.0.0.1, standing in for a public stack, in the stage given."""

    def __init__(self, run_dir, stage):
        super().__init__(run_dir)
        self.stage = stage

    def environment(self, base):
        env = super().environment(base)
        env["DEFAULT_HOOK_STAGE"] = self.stage
        return env


def test_the_real_service_s_decisions_read_back_as_the_run_s_ledger(tmp_path):
    """The shape of GET /api/decisions is the real one: rows with session_id, project_name, status, rule_key and
    stage, a cursor, and the project and session filters."""
    server = _ServerAsRemote(tmp_path / "remote", "enforce")
    (tmp_path / "remote").mkdir()
    server.start()
    try:
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, server.endpoint), _confined_env(tmp_path))
    finally:
        server.stop()
    assert row["harness_error"] is None, row["harness_error"]
    ledger = row["ledger"]
    assert ledger["reachable"] and ledger["complete"] and ledger["decisions"] >= 2 and ledger["refused"] == 1
    assert set(ledger["stages"]) == {"enforce"} and ledger["sessions"] == 1
    assert ledger["by_rule_key"] == {"python-domain-stays-pure": 1}
    assert row["project_stage_cached"] == "enforce" and row["governance_problem"] is None
    assert row["violation_landed"] is False and report.is_valid(dict(row, agent="claude-code"))


# --- the runner -----------------------------------------------------------------------------------------

def test_the_runner_takes_a_remote_endpoint_and_refuses_one_that_is_not(tmp_path):
    args = run.parse_args(["--threefold-endpoint", "https://threefold.acme.test/prod", "--live-date", DAY])
    assert (args.remote.endpoint, args.remote.date) == ("https://threefold.acme.test/prod/", DAY)
    assert run.parse_args([]).remote is None
    assert run.parse_args(["--threefold-endpoint", "https://threefold.acme.test/"]).remote.date == run.utc_day()
    for bad in (["--threefold-endpoint", "http://threefold.acme.test/"], ["--live-date", DAY],
                ["--threefold-endpoint", "https://threefold.acme.test/", "--live-date", "yesterday"]):
        with pytest.raises(SystemExit):
            run.parse_args(bad)


def test_a_results_file_never_mixes_two_threefolds():
    remote = harness.RemoteThreefold("https://threefold.acme.test/", DAY)
    tasks = [_task()]
    local_rows = [{"condition": "threefold", "ledger_source": "local"}, {"condition": "none"}]
    remote_rows = [{"condition": "threefold", "threefold_endpoint": remote.endpoint}]
    assert run.remote_problem(local_rows, None, tasks) is None
    assert run.remote_problem(remote_rows, remote, tasks) is None
    assert "pass the same --threefold-endpoint" in run.remote_problem(local_rows, remote, tasks)
    assert "pass the same --threefold-endpoint" in run.remote_problem(remote_rows, None, tasks)
    other = harness.RemoteThreefold("https://other.acme.test/", DAY)
    assert "https://threefold.acme.test/" in run.remote_problem(remote_rows, other, tasks)


def test_a_dry_run_names_the_remote_threefold_and_starts_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(harness, "run_one", lambda *args, **kwargs: pytest.fail("a dry run started a run"))
    code = run.main(["--tasks", TASK, "--conditions", "threefold", "--reps", "1", "--dry-run", "--claude", "claude-not-called",
                     "--isolation", "user-config", "--threefold-endpoint", "https://threefold.acme.test/", "--live-date", DAY,
                     "--work-root", str(tmp_path / "work"), "--results-dir", str(tmp_path / "results")])
    output = capsys.readouterr().out
    assert code == 0
    assert (f"threefold: the remote Threefold at https://threefold.acme.test/, projects Acme-Live-<task>, "
            f"sessions live-<task>-{DAY}") in output
    assert not (tmp_path / "work").exists() and not (tmp_path / "results").exists()


def test_a_whole_claude_code_run_through_the_runner_reports_to_the_remote_threefold(tmp_path, monkeypatch, capsys):
    """The stand-in `claude` asks the hook .claude/settings.local.json registers, as Claude Code does."""
    bin_dir = tmp_path / "bin"
    claude = fake_agents.install(bin_dir, "claude", mode="governed")
    with FakeThreefold(stage="enforce") as fake:
        code = run.main(["--tasks", TASK, "--conditions", "threefold", "--reps", "1", "--claude", str(claude),
                         "--isolation", "user-config", "--threefold-endpoint", fake.endpoint, "--live-date", DAY,
                         "--run-id", f"{DAY}-claude-code", "--work-root", str(tmp_path / "work"),
                         "--results-dir", str(tmp_path / "results"), "--retry-pause", "0"])
    assert code == 0, capsys.readouterr().err
    rows = run.ResultsFile(tmp_path / "results" / f"{DAY}-claude-code.jsonl").rows()
    assert len(rows) == 1
    row = rows[0]
    assert (row["agent"], row["condition"], row["ledger_source"]) == ("claude-code", "threefold", "remote")
    assert (row["threefold_endpoint"], row["threefold_project"], row["threefold_session"]) == (fake.endpoint, PROJECT, SESSION)
    assert [(body["session_id"], body["tool_name"]) for body in fake.evaluations()] == [(SESSION, "Write"), (SESSION, "Bash")]
    assert row["ledger"]["decisions"] == 2 and row["ledger"]["refused"] == 1
    assert row["hook_refusals_by_kind"] == {"LAYERING": 1} and row["governance_problem"] is None
    assert report.is_valid(row)


# --- the report -------------------------------------------------------------------------------------------

def _report_row(**extra):
    row = {"run_id": "r", "agent": "claude-code", "task": TASK, "family": "standard", "condition": "threefold", "rep": 1,
           "model": "claude-sonnet-5", "pilot": False, "agent_ran": True, "measured": True, "run_end": "completed",
           "harness_error": None, "acceptance_passed": True, "violation_landed": False, "governance_problem": None}
    row.update(extra)
    return row


def test_the_report_never_pools_live_rows_with_the_matrix_s():
    local = _report_row(ledger_source="local")
    live = _report_row(run_id="live", ledger_source="remote", threefold_endpoint="https://threefold.acme.test/")
    assert report.mixed_ledger_problem([local]) is None and report.mixed_ledger_problem([live]) is None
    problem = report.mixed_ledger_problem([local, live])
    assert "1 run(s) against a remote Threefold and 1 against a local server" in problem
    with pytest.raises(ValueError, match="remote Threefold"):
        report.build_summary([local, live], ["x.jsonl"])
    # Another agent's live rows beside Claude Code's matrix are not pooled anyway.
    assert report.mixed_ledger_problem([local, dict(live, agent="codex")]) is None


def test_the_report_names_a_remote_threefold_as_remote():
    row = _report_row(ledger_source="remote", server_healthy_after=False)
    assert report.invalid_reason(row) == "the remote Threefold was not answering when the agent stopped"
    old = _report_row(server_healthy_after=False)
    assert report.invalid_reason(old) == "the local Threefold server was not answering when the agent stopped"
