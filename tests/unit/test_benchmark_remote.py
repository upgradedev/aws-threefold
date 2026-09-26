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
import re
import subprocess
import sys
import urllib.request
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
    home is the test's own. So are the machine's home and THREEFOLD_HOME: a live run gives its hook the machine's home
    and carries the owner's never-send list from THREEFOLD_HOME, and a run started through the runner reads both from
    this process's environment."""
    monkeypatch.setattr(credentials, "DEFAULT_TOKEN_FILE", tmp_path / "no-such-token-file")
    monkeypatch.setattr(harness, "claude_memory_above", lambda path: [])
    (tmp_path / "codex-home").mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    (tmp_path / "machine-home").mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path / "machine-home"))
    monkeypatch.setenv("THREEFOLD_HOME", str(tmp_path / "owner-threefold-home"))


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
    # Invisible, look-alike and encoded: each would reach a host other than the one the owner reads.
    ("https://threefold.acme.test/​", "invisible"),
    ("https://threefold.acmе.test/", "invisible"),
    ("https://threefold.acme.test /", "invisible"),
    ("https://threefold.acme.test%40elsewhere.test/", "plain host name"),
    ("https://threefold.acme.test%2F/", "plain host name"),
    ("https://threefold_acme.test/", "plain host name"),
    ("https://-threefold.acme.test/", "plain host name"),
])
def test_an_endpoint_that_is_not_one_is_refused(given, words):
    with pytest.raises(ValueError, match=words):
        harness.remote_endpoint(given)


@pytest.mark.parametrize("given, expected", [
    ("https://xn--threefld-e1a.acme.test/", "https://xn--threefld-e1a.acme.test/"),
    ("https://192.0.2.10:8443/prod", "https://192.0.2.10:8443/prod/"),
    ("http://[::1]:8123/", "http://[::1]:8123/"),
])
def test_an_ascii_host_an_address_and_a_punycode_name_are_taken(given, expected):
    assert harness.remote_endpoint(given) == expected


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
    assert set(fake.paths()) == {"/status", f"/sessions/{SESSION}", "/evaluate-tool-call", "/api/decisions"}
    # Before the agent starts: the endpoint answers, the stack keeps no session by the name, and its ledger holds no
    # row in that session under any project.
    assert fake.paths()[:3] == ["/status", f"/sessions/{SESSION}", "/api/decisions"]
    reads = [item["query"] for item in fake.requests if item["path"] == "/api/decisions"]
    assert len(reads) >= 3, "the fake answers two rows a page, so the cursor must have been followed"
    for query in reads:
        # The session is read whole: a row under another project shares its loop history, halt and spend.
        assert "project" not in query and (query["session"], query["days"]) == (SESSION, "2")

    ledger = row["ledger"]
    assert (ledger["source"], ledger["reachable"], ledger["complete"]) == ("remote", True, True)
    assert ledger["decisions"] == len(evaluations) and ledger["refused"] == 1 and ledger["would_refuse"] == 0
    assert ledger["stages"] == {"enforce": len(evaluations)} and ledger["by_rule_key"] == {"python-domain-stays-pure": 1}
    assert (ledger["other_projects"], ledger["halted_from_outside"]) == (0, False)
    assert row["hook_calls"] == len(evaluations) and row["project_stage_remote"] is None
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


def test_a_project_promoted_with_the_flagging_rule_still_observing_is_not_counted_as_enforcing(tmp_path):
    """After Promote, the rules the operator did not pick keep observing: their flags are recorded under the stage
    `enforce` and let the call through. That is the normal state after the README's own step, and a run in it did not
    measure Threefold enforcing the rule that flagged it."""
    with FakeThreefold(stage="enforce", observe_rules=["python-domain-stays-pure"]) as fake:
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, fake.endpoint), _confined_env(tmp_path))
    assert row["harness_error"] is None, row["harness_error"]
    ledger = row["ledger"]
    assert (ledger["refused"], ledger["would_refuse"], set(ledger["stages"])) == (0, 1, {"enforce"})
    assert ledger["would_refuse_by_rule_key"] == {"python-domain-stays-pure": 1}
    assert row["project_stage_cached"] == "enforce" and row["violation_landed"] is True
    assert row["governance_problem"] == (
        "the remote Threefold let 1 of this run's call(s) through that python-domain-stays-pure would have refused, "
        f"because the project {PROJECT} is promoted there with that rule still observing: this run did not measure "
        "Threefold enforcing")
    assert not report.is_valid(dict(row, agent="claude-code"))


def test_a_repeated_read_is_not_a_rule_that_watched():
    """A repeated read or poll is noted with the key NONE: nothing would have refused it, so it is no would-refuse."""
    rows = [{"session_id": SESSION, "project_name": PROJECT, "status": "APPROVED", "rule_key": "NONE", "stage": "enforce"},
            {"session_id": SESSION, "project_name": PROJECT, "status": "BLOCKED_BOUNDARY_VIOLATION",
             "rule_key": "python-domain-stays-pure", "stage": "enforce"}]
    ledger = dict(harness.summarise_decisions(rows), reachable=True, complete=True, project=PROJECT)
    assert (ledger["would_refuse"], ledger["would_refuse_by_rule_key"]) == (0, {})
    row = {"condition": "threefold", "ledger_source": "remote", "server_healthy_after": True, "ledger": ledger}
    assert harness.governance_problem("threefold", row) is None
    two = dict(ledger, would_refuse=3, would_refuse_by_rule_key={"LOOP": 1, "python-domain-stays-pure": 2})
    assert "through that LOOP, python-domain-stays-pure would have refused" in harness.governance_problem(
        "threefold", dict(row, ledger=two))
    assert "with those rules still observing" in harness.governance_problem("threefold", dict(row, ledger=two))


def test_a_remote_threefold_that_does_not_answer_starts_no_agent(tmp_path):
    with FakeThreefold(status_code=503) as fake:
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, fake.endpoint), _confined_env(tmp_path))
    assert "answered GET status with 503" in row["harness_error"] and "no agent was started" in row["harness_error"]
    assert fake.paths() == ["/status"]
    run_dir = tmp_path / "work" / f"{TASK}--threefold--r1"
    assert not (run_dir / "transcript.jsonl").exists()
    assert not report.is_valid(dict(row, agent="claude-code"))


def _plan_in(tmp_path, endpoint, folder):
    plan = _plan(tmp_path, endpoint)
    plan.work_root = tmp_path / folder
    return plan


def test_a_second_run_on_the_same_day_is_a_session_of_its_own_and_counts_only_its_own_calls(tmp_path):
    """The reviewer's probe as a test: two runs on one day, each with a fresh work root, so the folders say nothing of
    the first. The session already holding rows on the remote ledger is never reused."""
    with FakeThreefold(stage="enforce") as fake:
        first = harness.run_one(_task(), "threefold", 1, _plan_in(tmp_path, fake.endpoint, "work-1"), _confined_env(tmp_path))
        second = harness.run_one(_task(), "threefold", 1, _plan_in(tmp_path, fake.endpoint, "work-2"), _confined_env(tmp_path))
        sent = fake.evaluations()
    assert first["harness_error"] is None and second["harness_error"] is None, (first["harness_error"], second["harness_error"])
    assert (first["threefold_session"], second["threefold_session"]) == (SESSION, f"{SESSION}-a2")
    assert first["attempt"] == second["attempt"] == 1
    each = len(sent) // 2
    assert each and [body["session_id"] for body in sent] == [SESSION] * each + [f"{SESSION}-a2"] * each
    for row in (first, second):
        assert (row["ledger"]["decisions"], row["ledger"]["refused"], row["ledger"]["sessions"]) == (each, 1, 1)
        assert row["governance_problem"] is None


def test_a_ledger_that_cannot_be_read_before_the_run_starts_no_agent(tmp_path):
    """Checked before the agent starts, after GET status: a run whose ledger cannot be read back measures nothing."""
    class Unreadable(FakeThreefold):
        def _decisions(self, query):
            return None  # the handler answers 200 with {} and no items: not a ledger

    with Unreadable() as fake:
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, fake.endpoint), _confined_env(tmp_path))
    assert "answered GET api/decisions with an answer that is not a ledger, so no agent was started" in row["harness_error"]
    assert fake.paths() == ["/status", f"/sessions/{SESSION}", "/api/decisions"] and fake.evaluations() == []
    assert not (tmp_path / "work" / f"{TASK}--threefold--r1" / "transcript.jsonl").exists()
    with FakeThreefold(redirect_decisions_to="https://elsewhere.acme.test/api/decisions") as fake:
        row = harness.run_one(_task(), "threefold", 1, _plan_in(tmp_path, fake.endpoint, "work-2"), _confined_env(tmp_path))
    assert "answered GET api/decisions with 302, so no agent was started" in row["harness_error"]
    assert fake.evaluations() == []


def test_when_every_session_name_is_taken_no_agent_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "MAX_SESSION_TRIES", 2)
    with FakeThreefold() as fake:
        for session in (SESSION, f"{SESSION}-a2"):
            fake.rows.append({"session_id": session, "project_name": PROJECT, "status": "APPROVED", "rule_key": "NONE",
                              "stage": "enforce"})
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, fake.endpoint), _confined_env(tmp_path))
    assert row["harness_error"] == (f"RuntimeError: the sessions {SESSION} to {SESSION}-a2 are all in use on the remote "
                                    "Threefold already, so no agent was started")
    assert fake.evaluations() == []


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
    # The other session's row is nobody's business; the row in this session under another project is counted apart,
    # never named, and means the run was not judged on its own record.
    assert ledger["other_projects"] == 1 and "Acme-Other" not in json.dumps(ledger)
    row = {"condition": "threefold", "ledger_source": "remote", "server_healthy_after": True, "ledger": ledger}
    assert harness.governance_problem("threefold", row) == (
        f"the remote ledger holds 1 call(s) in this run's session {SESSION} under another project; the stack keeps a "
        "session's loop history, halt and spend by its name alone, so this run was not judged on its own calls")


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


# --- the session is the run's own --------------------------------------------------------------------------

STRANGER = "Acme-Stranger"


def _row_in(session, project, key="NONE", status="APPROVED", stage="enforce", at="2026-09-26T00:00:00.000000Z"):
    return {"timestamp": at, "verdict_id": "acme0000", "session_id": session, "project_name": project,
            "status": status, "rule_key": key, "stage": stage}


def test_a_session_name_used_under_another_project_is_never_taken(tmp_path):
    """The reviewer's probe on the stand-in: a visitor's calls in the day's session name, under a project of their
    own. The stack keeps a session by its name alone, so the run takes the next name and is judged on its own calls."""
    with FakeThreefold(stage="enforce") as fake:
        fake.rows.append(_row_in(SESSION, STRANGER, key="LOOP", status="BLOCKED_LOOP_DETECTED"))
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, fake.endpoint), _confined_env(tmp_path))
        sent = fake.evaluations()
    assert row["harness_error"] is None, row["harness_error"]
    assert row["threefold_session"] == f"{SESSION}-a2"
    assert sent and {body["session_id"] for body in sent} == {f"{SESSION}-a2"}
    assert (row["ledger"]["other_projects"], row["ledger"]["refused"], row["governance_problem"]) == (0, 1, None)


def test_a_session_frozen_with_the_kill_switch_before_the_run_is_never_taken(tmp_path):
    """The kill switch freezes a session and writes no ledger row: GET sessions/<id> is what sees it."""
    with FakeThreefold(stage="enforce") as fake:
        fake.freeze(SESSION)
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, fake.endpoint), _confined_env(tmp_path))
        sent = fake.evaluations()
    assert row["harness_error"] is None, row["harness_error"]
    assert row["threefold_session"] == f"{SESSION}-a2" and {body["session_id"] for body in sent} == {f"{SESSION}-a2"}
    assert row["ledger"]["by_rule_key"] == {"python-domain-stays-pure": 1} and row["governance_problem"] is None


def test_a_session_record_that_cannot_be_read_starts_no_agent(tmp_path):
    class Unreadable(FakeThreefold):
        def _session(self, session):
            return {"not": "a session"}

    with Unreadable() as fake:
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, fake.endpoint), _confined_env(tmp_path))
    assert row["harness_error"] == ("RuntimeError: the remote Threefold answered GET sessions/<the run's session> with "
                                    "an answer that is not a session, so no agent was started")
    assert fake.evaluations() == []


def _meddled(monkeypatch, meddle):
    """Calls `meddle(session)` just after the run has claimed its session and before the agent starts: someone using
    the session while the agent works, which no check made before the run can see."""
    claim = harness.claim_unused_session

    def claim_then_meddle(server, remote, task, rep, attempt):
        session = claim(server, remote, task, rep, attempt)
        meddle(session)
        return session

    monkeypatch.setattr(harness, "claim_unused_session", claim_then_meddle)


@pytest.mark.parametrize("stage", ["enforce", "observe"])
def test_calls_under_another_project_in_the_session_while_the_agent_works_make_the_run_not_count(
        tmp_path, monkeypatch, stage):
    """Found by the read after the run. Under Observe, the stack's default, it is named before the stage, so the
    daily run reads it as a run that went wrong and runs the day again, never as a day done."""
    with FakeThreefold(stage=stage) as fake:
        _meddled(monkeypatch, lambda session: fake.rows.append(_row_in(session, STRANGER)))
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, fake.endpoint), _confined_env(tmp_path))
    assert row["harness_error"] is None, row["harness_error"]
    assert row["ledger"]["other_projects"] == 1 and STRANGER not in json.dumps(row)
    assert row["governance_problem"] == (
        f"the remote ledger holds 1 call(s) in this run's session {SESSION} under another project; the stack keeps a "
        "session's loop history, halt and spend by its name alone, so this run was not judged on its own calls")
    assert row["governance_problem"] != harness.stage_problem(row)
    assert not report.is_valid(dict(row, agent="claude-code"))


def test_more_calls_under_the_run_s_project_than_its_hook_sent_make_the_run_not_count(tmp_path, monkeypatch):
    """The project and the session are both public names, so a stranger can post under the run's own project too; each
    run of the hook sends one call at most, and the wrapper's log counts them."""
    with FakeThreefold(stage="enforce") as fake:
        _meddled(monkeypatch, lambda session: fake.rows.append(_row_in(session, PROJECT)))
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, fake.endpoint), _confined_env(tmp_path))
        sent = fake.evaluations()
    assert row["harness_error"] is None, row["harness_error"]
    assert row["hook_calls"] == len(sent) and row["ledger"]["decisions"] == len(sent) + 1
    assert row["governance_problem"] == (
        f"the remote ledger holds {len(sent) + 1} call(s) under this run's project and session {SESSION}, and the hook "
        f"ran {len(sent)} time(s), one call at most each: calls this run did not send were judged in its session")
    assert not report.is_valid(dict(row, agent="claude-code"))


@pytest.mark.parametrize("stage", ["enforce", "observe"])
def test_a_session_frozen_while_the_agent_works_makes_the_run_not_count(tmp_path, monkeypatch, stage):
    """The kill switch is open to anyone on the public demo, and a live session is on its pages. Every call after the
    freeze is refused (or, under Observe, would be) as HALTED_SESSION, which no call of the run's own caused."""
    with FakeThreefold(stage=stage) as fake:
        _meddled(monkeypatch, fake.freeze)
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, fake.endpoint), _confined_env(tmp_path))
    assert row["harness_error"] is None, row["harness_error"]
    ledger = row["ledger"]
    assert ledger["halted_from_outside"] is True and set(ledger["by_rule_key"]) == {"HALTED_SESSION"}
    assert (ledger["refused"] > 0) == (stage == "enforce") and ledger["other_projects"] == 0
    assert row["governance_problem"] == (
        f"this run's session {SESSION} was halted before any call of its own tripped it (the kill switch, or calls it "
        "did not send), so its refusals after that were the halt's, not its rules'")
    assert not report.is_valid(dict(row, agent="claude-code"))


def test_a_halt_the_run_tripped_itself_is_its_own():
    loop = _row_in(SESSION, PROJECT, key="LOOP", status="BLOCKED_LOOP_DETECTED", at="2026-09-26T10:00:01Z")
    halted = _row_in(SESSION, PROJECT, key="HALTED_SESSION", status="BLOCKED_CIRCUIT_BREAKER", at="2026-09-26T10:00:02Z")
    budget = dict(loop, rule_key="BUDGET", status="BLOCKED_CIRCUIT_BREAKER")
    assert harness.halted_from_outside([halted, loop]) is False, "the ledger's order, not the list's, decides"
    assert harness.halted_from_outside([budget, halted]) is False
    assert harness.halted_from_outside([dict(halted, timestamp="2026-09-26T10:00:00Z"), loop]) is True
    # Under Observe the run's own loop halts nothing, so a halt after it came from outside.
    assert harness.halted_from_outside([dict(loop, stage="observe", status="APPROVED"),
                                        dict(halted, stage="observe", status="APPROVED")]) is True
    assert harness.halted_from_outside([loop]) is False and harness.halted_from_outside([]) is False


def test_a_run_the_stack_judged_no_call_of_is_read_by_the_project_s_stage(tmp_path):
    """The reviewer's probe as a test: an agent that makes no governed call leaves no ledger row and no stage in the
    hook's cache. The stack's own word for the project's stage then says whether the run measured enforcement."""
    claude = fake_agents.install(tmp_path / "bin", "claude", mode="ok")
    for stage in ("observe", "enforce"):
        with FakeThreefold(stage=stage) as fake:
            code = run.main(["--tasks", TASK, "--conditions", "threefold", "--reps", "1", "--claude", str(claude),
                             "--isolation", "user-config", "--threefold-endpoint", fake.endpoint, "--live-date", DAY,
                             "--run-id", f"{DAY}-{stage}", "--work-root", str(tmp_path / f"work-{stage}"),
                             "--results-dir", str(tmp_path / "results"), "--retry-pause", "0"])
            paths = fake.paths()
        row = run.ResultsFile(tmp_path / "results" / f"{DAY}-{stage}.jsonl").rows()[0]
        assert code == 0 and row["harness_error"] is None, row["harness_error"]
        assert (row["ledger"]["decisions"], row["hook_calls"], row["project_stage_cached"]) == (0, 0, None)
        assert row["project_stage_remote"] == stage and paths[-1] == f"/api/projects/{PROJECT}"
        if stage == "observe":
            assert row["governance_problem"] == (
                f"the remote Threefold holds the project {PROJECT} in Observe and its ledger holds no call of this "
                "run's, so it would have refused nothing: this run did not measure Threefold enforcing")
            assert not report.is_valid(row)
        else:
            assert row["governance_problem"] is None and report.is_valid(row)


# --- against a real Threefold from this repository's source, as the remote ---------------------------------

OPERATOR_KEY = "acme-fixture-operator-key-0001"


class _ServerAsRemote(harness.LocalServer):
    """A server from this repository's source on 127.0.0.1, standing in for a public stack, in the stage given, with a
    fixture operator key so a test can promote a project as the operator would."""

    def __init__(self, run_dir, stage):
        super().__init__(run_dir)
        self.stage = stage

    def environment(self, base):
        env = super().environment(base)
        env.update({"DEFAULT_HOOK_STAGE": self.stage, "THREEFOLD_API_KEYS": OPERATOR_KEY})
        return env

    def configure(self, project, body):
        request = urllib.request.Request(self.endpoint + f"api/projects/{project}", data=json.dumps(body).encode("utf-8"),
                                         method="POST", headers={"Content-Type": "application/json",
                                                                 "X-API-Key": OPERATOR_KEY})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))


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


def test_the_real_service_promoted_with_the_rule_still_observing_is_not_counted_as_enforcing(tmp_path):
    """The reviewer's probe as a test: the stack's default stage is Observe, the operator puts the live project in
    Enforce with the layering rule still observing, and the run's row says it did not measure Threefold enforcing."""
    server = _ServerAsRemote(tmp_path / "remote", "observe")
    (tmp_path / "remote").mkdir()
    server.start()
    try:
        status, _ = server.configure(PROJECT, {"stage": "enforce", "observe_rules": ["python-domain-stays-pure"]})
        assert status == 200
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, server.endpoint), _confined_env(tmp_path))
    finally:
        server.stop()
    assert row["harness_error"] is None, row["harness_error"]
    ledger = row["ledger"]
    assert (ledger["refused"], ledger["would_refuse"], set(ledger["stages"])) == (0, 1, {"enforce"})
    assert ledger["would_refuse_by_rule_key"] == {"python-domain-stays-pure": 1}
    assert row["project_stage_cached"] == "enforce" and row["violation_landed"] is True
    assert "python-domain-stays-pure would have refused" in row["governance_problem"]
    assert "still observing: this run did not measure Threefold enforcing" in row["governance_problem"]
    assert not report.is_valid(dict(row, agent="claude-code"))


def _page_call(endpoint, session, project=STRANGER):
    """What any visitor can send: a page's call, under a project of their own, in a session they name."""
    body = {"session_id": session, "project_name": project, "developer": "anonymous", "tool_name": "Bash",
            "action_type": "execute", "arguments": {"command": "npm run build"}, "agent": "page", "origin": "page",
            "explain": False, "dry_run": False}
    return _post(endpoint + "evaluate-tool-call", body)


def _post(url, body):
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _real_remote(tmp_path, stage):
    (tmp_path / "remote").mkdir()
    server = _ServerAsRemote(tmp_path / "remote", stage)
    server.start()
    return server


def test_the_real_service_s_default_stage_is_read_as_observe_from_its_own_rows(tmp_path):
    """The public stack's default, pinned against the real service: the live project is not configured and the stack's
    DEFAULT_HOOK_STAGE is observe. Its rows say APPROVED, stage observe, and the rule that would have refused."""
    server = _real_remote(tmp_path, "observe")
    try:
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, server.endpoint), _confined_env(tmp_path))
        _, document = harness.remote_json(harness.RemoteServer(server.endpoint, PROJECT, SESSION).decisions_url())
    finally:
        server.stop()
    assert row["harness_error"] is None, row["harness_error"]
    rows = document["items"]
    assert rows and {(item["status"], item["stage"], item["project_name"]) for item in rows} == {
        ("APPROVED", "observe", PROJECT)}
    assert sorted(item["rule_key"] for item in rows if item["rule_key"] != "NONE") == ["python-domain-stays-pure"]
    ledger = row["ledger"]
    assert (ledger["decisions"], ledger["refused"], ledger["would_refuse"]) == (len(rows), 0, 1)
    assert ledger["stages"] == {"observe": len(rows)} and ledger["would_refuse_by_rule_key"] == {"python-domain-stays-pure": 1}
    assert (ledger["other_projects"], ledger["halted_from_outside"], row["hook_calls"]) == (0, False, len(rows))
    assert row["project_stage_cached"] == "observe" and row["violation_landed"] is True
    assert row["governance_problem"] == harness.stage_problem(row) == (
        f"the remote Threefold judged {len(rows)} of this run's {len(rows)} call(s) in Observe (the project {PROJECT} is "
        "not promoted there), so it recorded what it would have refused and refused nothing: this run did not measure "
        "Threefold enforcing")
    assert not report.is_valid(dict(row, agent="claude-code"))


def test_the_real_service_a_stranger_s_loop_in_the_session_name_before_the_run(tmp_path):
    """The reviewer's probe as a test: three page calls from another project in the day's session name trip it on the
    real service. The run never takes that name, so none of its calls is refused by a halt it did not cause."""
    server = _real_remote(tmp_path, "enforce")
    try:
        answers = [_page_call(server.endpoint, SESSION) for _ in range(3)]
        assert (answers[-1]["status"], answers[-1]["session_tripped"]) == ("BLOCKED_LOOP_DETECTED", True)
        assert harness.RemoteServer(server.endpoint, PROJECT, SESSION).unused() is False
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, server.endpoint), _confined_env(tmp_path))
    finally:
        server.stop()
    assert row["harness_error"] is None, row["harness_error"]
    assert row["threefold_session"] == f"{SESSION}-a2"
    ledger = row["ledger"]
    assert ledger["by_rule_key"] == {"python-domain-stays-pure": 1} and ledger["other_projects"] == 0
    assert row["governance_problem"] is None and report.is_valid(dict(row, agent="claude-code"))


def test_the_real_service_a_stranger_in_the_session_while_the_agent_works(tmp_path, monkeypatch):
    """On the stack's default stage, the reviewer's case: a stranger's loop trips the session after the run claimed
    it. The run's calls are recorded as would-refuse HALTED_SESSION; the row names the stranger's calls, not the stage."""
    server = _real_remote(tmp_path, "observe")
    try:
        _meddled(monkeypatch, lambda session: [_page_call(server.endpoint, session) for _ in range(3)])
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, server.endpoint), _confined_env(tmp_path))
    finally:
        server.stop()
    assert row["harness_error"] is None, row["harness_error"]
    ledger = row["ledger"]
    assert ledger["other_projects"] == 3 and ledger["refused"] == 0
    assert "HALTED_SESSION" in ledger["would_refuse_by_rule_key"] and ledger["halted_from_outside"] is True
    assert row["governance_problem"].startswith(
        f"the remote ledger holds 3 call(s) in this run's session {SESSION} under another project")
    assert not report.is_valid(dict(row, agent="claude-code"))


def test_the_real_service_s_kill_switch_while_the_agent_works(tmp_path, monkeypatch):
    """POST sessions/<id>/terminate, open to anyone where reads are public, freezes the session and writes no row."""
    server = _real_remote(tmp_path, "enforce")
    try:
        _meddled(monkeypatch, lambda session: _post(server.endpoint + f"sessions/{session}/terminate",
                                                    {"operator_name": "acme-visitor", "reason": "curious"}))
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, server.endpoint), _confined_env(tmp_path))
    finally:
        server.stop()
    assert row["harness_error"] is None, row["harness_error"]
    ledger = row["ledger"]
    assert ledger["other_projects"] == 0 and set(ledger["by_rule_key"]) == {"HALTED_SESSION"}
    assert ledger["halted_from_outside"] is True
    assert row["governance_problem"].startswith(f"this run's session {SESSION} was halted before any call of its own")
    assert not report.is_valid(dict(row, agent="claude-code"))


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


# --- what leaves the machine for a public ledger ---------------------------------------------------------------

def _strings(value):
    """Every string a request body holds, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _spellings(path):
    """A folder as a command may name it: as written, with forward slashes, and as Git Bash writes a drive."""
    text = str(path)
    forms = {text, text.replace("\\", "/")}
    if len(text) > 2 and text[1] == ":":
        forms.add("/" + text[0].lower() + text[2:].replace("\\", "/"))
    return forms


def _owner_profile(tmp_path, monkeypatch):
    """A machine home laid out as a Windows profile, with the TEMP folder inside it, as this process's environment."""
    owner = tmp_path / "Users" / "acme-owner"
    temp = owner / "AppData" / "Local" / "Temp"
    temp.mkdir(parents=True)
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(owner))
    for name in ("TEMP", "TMP"):
        monkeypatch.setenv(name, str(temp))
    return owner


def _live_claude_run(tmp_path, commands, stage="enforce"):
    """One whole Claude Code run through the runner against a stand-in, the fake `claude` adding `commands`."""
    claude = fake_agents.install(tmp_path / "bin", "claude", mode="governed", commands=commands)
    with FakeThreefold(stage=stage) as fake:
        code = run.main(["--tasks", TASK, "--conditions", "threefold", "--reps", "1", "--claude", str(claude),
                         "--isolation", "user-config", "--threefold-endpoint", fake.endpoint, "--live-date", DAY,
                         "--run-id", f"{DAY}-claude-code", "--work-root", str(tmp_path / "work"),
                         "--results-dir", str(tmp_path / "results"), "--retry-pause", "0"])
    rows = run.ResultsFile(tmp_path / "results" / f"{DAY}-claude-code.jsonl").rows()
    assert code == 0 and len(rows) == 1
    return rows[0], fake


def test_a_live_run_sends_the_machine_s_home_as_tilde_and_never_spelled_out(tmp_path, monkeypatch):
    """The reviewer's probe as a test: the agent spells out its TEMP folder and a file in its home. The hook shortens
    the machine's home to ~ in what it sends, as on the owner's own repositories, so the owner's login name, which a
    Windows profile path carries, never reaches the remote ledger."""
    owner = _owner_profile(tmp_path, monkeypatch)
    row, fake = _live_claude_run(tmp_path, ["python -m pytest -q --basetemp <ENV:TEMP>\\bt", 'dir "<ENV:TEMP>"',
                                            "cat <ENV:USERPROFILE>/.gitconfig"])
    assert row["harness_error"] is None, row["harness_error"]
    assert (row["hook_home"], row["never_send_list"]) == ("machine", "none")
    sent = fake.evaluations()
    assert [body["tool_name"] for body in sent] == ["Write", "Bash", "Bash", "Bash", "Bash"], "a call was held back"
    texts = [text.casefold() for body in sent for text in _strings(body)]
    for spelling in _spellings(owner):
        assert not any(spelling.casefold() in text for text in texts), f"{spelling} reached the remote ledger"
    commands = [text for text in texts if text.startswith(("python -m pytest", "dir ", "cat "))]
    assert len(commands) == 3, commands
    assert re.search(r"--basetemp ~[\\/]appdata[\\/]local[\\/]temp[\\/]bt$", commands[0]), commands[0]
    assert re.fullmatch(r'dir "~[\\/]appdata[\\/]local[\\/]temp"', commands[1]), commands[1]
    assert commands[2] == "cat ~/.gitconfig"
    assert not (owner / ".threefold").exists() and sorted(path.name for path in owner.iterdir()) == ["AppData"], \
        "the run wrote into the machine's home"


def test_the_hook_of_a_local_run_keeps_a_home_of_the_run_s_own(tmp_path):
    """Only a run reporting to a remote Threefold is given the machine's home; the matrix's local runs are unchanged."""
    hook = tmp_path / "hook.py"
    hook.write_text("", encoding="utf-8")
    local = harness.write_hook_wrapper(tmp_path / "local-run", hook).read_text(encoding="utf-8")
    assert f'os.environ["USERPROFILE"] = "{harness.forward(tmp_path / "local-run" / "home")}"' in local
    live = harness.write_hook_wrapper(tmp_path / "live-run", hook, session=SESSION,
                                      home=tmp_path / "machine-home").read_text(encoding="utf-8")
    assert f'os.environ["USERPROFILE"] = "{harness.forward(tmp_path / "machine-home")}"' in live
    assert f'os.environ["THREEFOLD_HOME"] = "{harness.forward(tmp_path / "live-run" / "threefold-home")}"' in live
    assert not (tmp_path / "live-run" / "home").exists()
    assert "must never carry the owner's login name" in live and "must never carry" not in local


def test_the_owner_s_never_send_list_goes_with_a_live_run_and_nothing_else_of_threefold_home(tmp_path, monkeypatch):
    """A call holding one of the owner's never-send terms never reaches the public ledger. Only the list is carried:
    the owner's config.json, which may name a key file, stays where it is and is never read."""
    owner_home = tmp_path / "owner-threefold-home"
    owner_home.mkdir()
    listed = "# the owner's own terms\r\nAcme-Codename-Orion\r\n".encode("utf-8")
    (owner_home / harness.NEVER_SEND_NAME).write_bytes(listed)
    (tmp_path / "private-key").write_text("acme-fixture-private-stack-key-0123456789\n", encoding="utf-8")
    (owner_home / "config.json").write_text(json.dumps({"api_key_file": str(tmp_path / "private-key")}), encoding="utf-8")
    row, fake = _live_claude_run(tmp_path, ["echo acme-codename-orion > notes.txt", "python -m pytest -q"])
    assert row["harness_error"] is None, row["harness_error"]
    assert row["never_send_list"] == "copied"
    sent = fake.evaluations()
    assert [body["tool_name"] for body in sent] == ["Write", "Bash", "Bash"], "the never-send call was sent, or more held"
    assert not any("orion" in text.casefold() for body in sent for text in _strings(body))
    assert not any({"x-api-key", "authorization"} & set(item["headers"]) for item in fake.requests), "a key was sent"
    # The copy is the hook's for as long as the agent works, and gone after: the run's folders stay behind.
    run_home = tmp_path / "work" / f"{TASK}--threefold--r1" / "threefold-home"
    assert run_home.is_dir() and sorted(run_home.rglob(harness.NEVER_SEND_NAME)) == []
    assert not (run_home / "config.json").exists()
    assert not any(b"orion" in path.read_bytes().lower() for path in run_home.rglob("*") if path.is_file())
    assert (owner_home / harness.NEVER_SEND_NAME).read_bytes() == listed
    recorded = [text.casefold() for text in _strings(row)]
    assert not any("orion" in text or str(owner_home).casefold() in text for text in recorded)


def test_a_never_send_list_that_cannot_be_read_starts_no_agent(tmp_path):
    (tmp_path / "threefold-home" / harness.NEVER_SEND_NAME).mkdir(parents=True)
    with FakeThreefold() as fake:
        row = harness.run_one(_task(), "threefold", 1, _plan(tmp_path, fake.endpoint), _confined_env(tmp_path))
    assert "never-send list is there but could not be read" in row["harness_error"]
    assert fake.evaluations() == []
    assert not (tmp_path / "work" / f"{TASK}--threefold--r1" / "transcript.jsonl").exists()


def test_the_harness_carries_the_file_the_hook_reads():
    hook = (REPO_ROOT / "src" / "threefold" / "hooks" / "threefold_hook.py").read_text(encoding="utf-8")
    assert f'NEVER_SEND_NAME = "{harness.NEVER_SEND_NAME}"' in hook
    assert re.search(r"MAX_NEVER_SEND_BYTES = ([0-9_]+)", hook)
    assert int(re.search(r"MAX_NEVER_SEND_BYTES = ([0-9_]+)", hook).group(1).replace("_", "")) < harness.NEVER_SEND_COPY_BYTES


@pytest.mark.parametrize("env, expected", [
    ({"THREEFOLD_HOME": "D:/acme/threefold-home"}, "D:/acme/threefold-home"),
    ({}, "<home>/.threefold"),
    ({"THREEFOLD_HOME": "~/.threefold-acme"}, "<home>/.threefold-acme"),
])
def test_the_owner_s_threefold_home_is_found_as_the_hook_finds_it(tmp_path, env, expected):
    home = tmp_path / "machine"
    found = harness.owner_threefold_home(dict(env, HOME=str(home), USERPROFILE=str(home)))
    assert found == Path(os.path.abspath(expected.replace("<home>", str(home))))


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
