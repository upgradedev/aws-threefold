"""The daily live run: one real agent, one Acme task, under Threefold, against the public stack given and no other.

No agent and no network here: the agents are the stand-ins of
benchmark/fake_agents.py and the public stack is benchmark/fake_threefold.py on
127.0.0.1. The script's own entry point refuses anything but https, so the whole
runs go through run_live(), the part after that check; main() is driven for
its refusals, its dry run and its wiring. The owner's token file and Codex home
are never read: every path a run could touch is under tmp_path.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark import credentials, fake_agents, harness, run  # noqa: E402
from benchmark.fake_threefold import FakeThreefold  # noqa: E402

_spec = importlib.util.spec_from_file_location("daily_live_agent", ROOT / "scripts" / "daily_live_agent.py")
daily = importlib.util.module_from_spec(_spec)
# Its dataclasses look their module up by name while the class is made.
sys.modules.setdefault("daily_live_agent", daily)
_spec.loader.exec_module(daily)

DAY = "2026-09-26"
TASK = "orders-s3-archive"
PROJECT = "Acme-Live-orders-s3-archive"
SESSION = f"live-orders-s3-archive-{DAY}"
TOKEN = "acme-fixture-daily-token-0123456789abcdef"


@pytest.fixture(autouse=True)
def _confined(tmp_path, monkeypatch):
    """The machine's home and THREEFOLD_HOME are the test's own: a live run hands the first to its hook and reads the
    owner's never-send list from the second, both from this process's environment."""
    monkeypatch.setattr(credentials, "DEFAULT_TOKEN_FILE", tmp_path / "no-such-token-file")
    monkeypatch.setattr(harness, "claude_memory_above", lambda path: [])
    (tmp_path / "codex-home").mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    (tmp_path / "machine-home").mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path / "machine-home"))
    monkeypatch.setenv("THREEFOLD_HOME", str(tmp_path / "owner-threefold-home"))


@pytest.fixture
def fake_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    return {"dir": bin_dir, "claude": fake_agents.install(bin_dir, "claude", mode="governed"),
            "codex": fake_agents.install(bin_dir, "codex")}


def _agent_calls(fake_bin):
    return [call for agent in ("claude", "codex") for call in fake_agents.calls(fake_bin["dir"], agent)
            if "--version" not in call["argv"] and "--help" not in call["argv"]]


# --- which agent, which task ----------------------------------------------------------------------------

def test_the_agent_alternates_daily_and_both_agents_do_each_task_on_consecutive_days():
    tasks = daily.standard_tasks()
    assert len(tasks) == 6 and tasks == sorted(tasks)
    start = daily.ROTATION_START
    picks = [daily.pick_for(start + datetime.timedelta(days=n)) for n in range(12)]
    assert [pick.agent for pick in picks] == ["claude-code", "codex"] * 6
    assert {(pick.agent, pick.task) for pick in picks} == {(agent, task) for agent in daily.AGENTS for task in tasks}
    for claude_day, codex_day in zip(picks[::2], picks[1::2]):
        assert claude_day.task == codex_day.task
    again = daily.pick_for(start + datetime.timedelta(days=12))
    assert (again.agent, again.task) == (picks[0].agent, picks[0].task)
    before = daily.pick_for(start - datetime.timedelta(days=1))
    assert (before.agent, before.task) == ("codex", tasks[-1])
    assert daily.pick_for(start + datetime.timedelta(days=5)) == daily.pick_for(start + datetime.timedelta(days=5))


def test_a_pick_names_its_run_project_and_session():
    pick = daily.Pick(DAY, "codex", TASK)
    assert (pick.run_id, pick.project, pick.session) == (f"{DAY}-codex", PROJECT, SESSION)


# --- the endpoint ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("given, expected", [
    ("https://threefold.acme.test", "https://threefold.acme.test/"),
    ("https://threefold.acme.test/prod", "https://threefold.acme.test/prod/"),
])
def test_an_https_endpoint_is_taken_with_its_path(given, expected):
    assert daily.check_endpoint(given) == expected


@pytest.mark.parametrize("given", [
    "http://threefold.acme.test/", "http://127.0.0.1:8123/", "http://localhost/", "ftp://threefold.acme.test/",
    "https://owner:secret@threefold.acme.test/", "https://threefold.acme.test/?stage=prod",
    "https://threefold.acme.test/#x", "https:///", "threefold.acme.test", "",
    "https://threefold.acme.test/​", "https://threefold.acmе.test/", "https://threefold.acme.test%40elsewhere.test/",
])
def test_anything_but_a_plain_https_endpoint_is_refused(given):
    with pytest.raises(daily.Refused):
        daily.check_endpoint(given)


def test_a_refused_endpoint_creates_nothing_and_starts_nothing(tmp_path, fake_bin, capsys):
    """Refused before anything exists: no work root, no results, no agent, no request to the stand-in either."""
    with FakeThreefold() as fake:
        code = daily.main(["--endpoint", fake.endpoint, "--results-dir", str(tmp_path / "results"),
                           "--work-root", str(tmp_path / "work"), "--claude", str(fake_bin["claude"]),
                           "--codex", str(fake_bin["codex"])])
        assert fake.requests == []
    assert code == 2 and "refused: the endpoint must be https" in capsys.readouterr().err
    assert not (tmp_path / "results").exists() and not (tmp_path / "work").exists()
    assert _agent_calls(fake_bin) == []


def test_there_is_no_default_endpoint(capsys):
    with pytest.raises(SystemExit):
        daily.main([])
    assert "--endpoint" in capsys.readouterr().err


def test_a_real_run_is_named_after_the_day_it_runs(tmp_path, capsys):
    code = daily.main(["--endpoint", "https://threefold.acme.test/", "--date", "2026-10-01",
                       "--results-dir", str(tmp_path / "results"), "--work-root", str(tmp_path / "work")])
    assert code == 2 and "--date is for --dry-run only" in capsys.readouterr().err
    assert not (tmp_path / "results").exists() and not (tmp_path / "work").exists()


def test_the_hook_never_runs_in_the_repository(tmp_path, capsys, monkeypatch):
    """The work root is where the task repository is copied and the hook runs: never this repository."""
    run_live = daily.run_live
    monkeypatch.setattr(daily, "run_live", lambda *args, **kwargs: pytest.fail("started a run"))
    monkeypatch.setattr(run, "main", lambda *args, **kwargs: pytest.fail("started the benchmark"))
    inside = ROOT / "benchmark" / "results" / "live-work-root-that-must-not-exist"
    code = daily.main(["--endpoint", "https://threefold.acme.test/", "--work-root", str(inside),
                       "--results-dir", str(tmp_path / "results")])
    assert code == 2 and "the work root must be outside" in capsys.readouterr().err
    assert not inside.exists()
    code, row, note = run_live(daily.Pick(DAY, "codex", TASK), "https://threefold.acme.test/",
                               results_dir=tmp_path / "results", work_root=inside)
    assert (code, row) == (2, None) and "must be outside" in note and not inside.exists()


# --- the dry run ---------------------------------------------------------------------------------------------

def test_a_dry_run_prints_the_pick_and_the_command_and_neither_the_token_file_nor_the_codex_home(tmp_path, capsys):
    token_file, codex_home = tmp_path / "owner-token-file", tmp_path / "owner-codex-home"
    common = ["--endpoint", "https://threefold.acme.test/prod", "--dry-run", "--token-file", str(token_file),
              "--codex-home", str(codex_home), "--results-dir", str(tmp_path / "results"),
              "--work-root", str(tmp_path / "work")]
    assert daily.main(common + ["--date", "2026-09-27"]) == 0
    out = capsys.readouterr().out
    assert ("live 2026-09-27: claude-code on billing-credit-limit, project Acme-Live-billing-credit-limit, session "
            "live-billing-credit-limit-2026-09-27 (or the next free -a<n>), against https://threefold.acme.test/prod/") in out
    assert ("would run: python benchmark/run.py --agent claude-code --tasks billing-credit-limit --conditions threefold "
            "--reps 1 --threefold-endpoint https://threefold.acme.test/prod/ --live-date 2026-09-27 "
            "--run-id 2026-09-27-claude-code") in out
    assert "--token-file <token file>" in out and str(token_file) not in out and "CODEX_HOME" not in out

    assert daily.main(common + ["--date", "2026-09-28"]) == 0
    out = capsys.readouterr().out
    assert "live 2026-09-28: codex on billing-credit-limit" in out
    assert "would run: python benchmark/run.py --agent codex --tasks billing-credit-limit" in out
    assert out.rstrip().endswith("--work-root " + str(tmp_path / "work") + " (with CODEX_HOME set to the Codex home given)")
    assert "CODEX_HOME=" not in out, "a shell prefix neither cmd nor PowerShell takes"
    assert str(codex_home) not in out and "--token-file" not in out
    assert not (tmp_path / "results").exists() and not (tmp_path / "work").exists()


def test_the_command_a_dry_run_prints_is_one_the_runner_takes(tmp_path):
    pick = daily.Pick("2026-09-27", "claude-code", "billing-credit-limit")
    arguments = daily.run_arguments(pick, "https://threefold.acme.test/", tmp_path / "results", tmp_path / "work")
    args = run.parse_args(arguments)
    assert (args.agent, args.tasks, args.conditions, args.reps) == ("claude-code", ["billing-credit-limit"], ["threefold"], 1)
    assert (args.remote.endpoint, args.remote.date, args.run_id) == ("https://threefold.acme.test/", "2026-09-27",
                                                                     "2026-09-27-claude-code")


# --- whole runs, with the stand-ins ------------------------------------------------------------------------------

def _rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def test_a_codex_day_puts_its_session_on_the_remote_ledger_and_records_one_row(tmp_path, fake_bin):
    live_home = tmp_path / "live-codex-home"
    live_home.mkdir()
    pick = daily.Pick(DAY, "codex", TASK)
    results = tmp_path / "results"
    with FakeThreefold(stage="enforce") as fake:
        code, row, note = daily.run_live(pick, fake.endpoint, results_dir=results, work_root=tmp_path / "work",
                                         codex_home=live_home, codex=str(fake_bin["codex"]), retry_pause=0)
        endpoint = fake.endpoint
        sent = fake.evaluations()
    assert (code, note) == (0, ""), note
    assert _rows(results / f"{DAY}-codex.jsonl") == [row]
    assert (row["agent"], row["condition"], row["ledger_source"]) == ("codex", "threefold", "remote")
    assert (row["threefold_endpoint"], row["threefold_project"], row["threefold_session"]) == (endpoint, PROJECT, SESSION)
    assert sent and {(body["project_name"], body["session_id"], body["agent"]) for body in sent} == {(PROJECT, SESSION, "codex")}
    assert row["ledger"]["refused"] == 1 and row["governance_problem"] is None
    execs = [call for call in fake_agents.calls(fake_bin["dir"], "codex") if call["argv"][:1] == ["exec"]]
    assert len(execs) == 1 and execs[0]["codex_home"] == str(live_home)
    assert os.environ["CODEX_HOME"] == str(tmp_path / "codex-home"), "the Codex home given leaked past the run"

    line = daily.summary_line(pick, row, note, results / f"{DAY}-codex.jsonl")
    assert line.startswith(f"live {DAY} codex on {TASK}: completed")
    assert "1 refused, 0 would refuse; judged 2 in enforce" in line and f"session {SESSION} of {PROJECT}" in line
    assert "; counted;" in line and str(live_home) not in line
    log = (tmp_path / "work" / daily.LOG_NAME).read_text(encoding="utf-8")
    assert f"run {DAY}-codex" in log and "(with CODEX_HOME set to the Codex home given)" in log
    assert str(live_home) not in log

    again = daily.run_live(pick, endpoint, results_dir=results, work_root=tmp_path / "work", codex=str(fake_bin["codex"]))
    assert again[0] == 0 and again[1] == row and "already recorded today" in again[2]
    assert len([call for call in fake_agents.calls(fake_bin["dir"], "codex") if call["argv"][:1] == ["exec"]]) == 1


def test_a_codex_day_sends_the_machine_s_home_as_tilde_and_never_spelled_out(tmp_path, fake_bin, monkeypatch):
    """Codex keeps the machine's HOME, so a command it writes may name the owner's profile in full; the public ledger
    gets `~` in its place, as the hook sends it from the owner's own repositories."""
    owner = tmp_path / "Users" / "acme-owner"
    (owner / "AppData" / "Local" / "Temp").mkdir(parents=True)
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(owner))
    fake_agents.set_behaviour(fake_bin["dir"], "codex", commands=['type "<ENV:USERPROFILE>\\.gitconfig"',
                                                                  "dir <ENV:HOME>/AppData/Local/Temp"])
    with FakeThreefold(stage="enforce") as fake:
        code, row, note = daily.run_live(daily.Pick(DAY, "codex", TASK), fake.endpoint, results_dir=tmp_path / "results",
                                         work_root=tmp_path / "work", codex=str(fake_bin["codex"]), retry_pause=0)
        sent = fake.evaluations()
    assert (code, note) == (0, ""), note
    assert row["hook_home"] == "machine"
    commands = [str((body.get("arguments") or {}).get("command") or "") for body in sent]
    assert 'type "~\\.gitconfig"' in commands and "dir ~/AppData/Local/Temp" in commands, commands
    for spelling in {str(owner), str(owner).replace("\\", "/")}:
        assert spelling.casefold() not in json.dumps(sent).replace("\\\\", "\\").casefold()


def test_a_claude_code_day_in_observe_is_recorded_as_would_refuse_and_not_counted(tmp_path, fake_bin):
    """The public stack's default: the project in Observe. Nothing is called refused that ran, the token reaches the
    agent alone, and neither the token nor its file is printed."""
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    pick = daily.Pick(DAY, "claude-code", TASK)
    results = tmp_path / "results"
    with FakeThreefold(stage="observe") as fake:
        code, row, note = daily.run_live(pick, fake.endpoint, results_dir=results, work_root=tmp_path / "work",
                                         token_file=token_file, claude=str(fake_bin["claude"]), retry_pause=0)
        endpoint = fake.endpoint
    assert (code, note) == (0, ""), note
    assert (row["auth"], row["isolation"]["mode"]) == ("token-file", "fresh-config")
    assert row["ledger"]["refused"] == 0 and row["ledger"]["would_refuse"] == 1
    assert "in Observe" in row["governance_problem"]
    line = daily.summary_line(pick, row, note, results / f"{DAY}-claude-code.jsonl")
    assert "0 refused, 1 would refuse; judged 2 in observe" in line and "0 refusals seen by the agent" in line
    assert "not counted: the remote Threefold judged 2 of this run's 2 call(s) in Observe" in line
    assert TOKEN not in line and str(token_file) not in line
    runs = [call for call in fake_agents.calls(fake_bin["dir"], "claude") if "-p" in call["argv"]]
    assert len(runs) == 1 and runs[0]["token_set"] is True and runs[0]["token_in_argv"] is False
    assert TOKEN not in (results / f"{DAY}-claude-code.jsonl").read_text(encoding="utf-8")
    assert TOKEN not in (tmp_path / "work" / daily.LOG_NAME).read_text(encoding="utf-8")

    # The run went its course; only the stage on the stack keeps it from counting, and a second run today would be
    # judged the same way. So it is not run again, and the day reads as done.
    again = daily.run_live(pick, endpoint, results_dir=results, work_root=tmp_path / "work",
                           token_file=token_file, claude=str(fake_bin["claude"]))
    assert (again[0], again[1]) == (0, row) and again[2] == f"already recorded today, in {DAY}-claude-code.jsonl"
    assert len([call for call in fake_agents.calls(fake_bin["dir"], "claude") if "-p" in call["argv"]]) == 1


def _failed_row(endpoint, error="RuntimeError: the remote Threefold did not answer"):
    return {"agent": "codex", "task": TASK, "condition": "threefold", "rep": 1, "ledger_source": "remote",
            "threefold_endpoint": endpoint, "threefold_project": PROJECT, "threefold_session": SESSION,
            "harness_error": error, "agent_ran": False}


def test_a_day_that_failed_says_so_again_and_is_run_again_as_the_next_attempt(tmp_path, fake_bin):
    """A day whose run did not go as planned never reads as done: its row keeps exit 1, and the script started again
    that day runs the day again, as the benchmark's resume, in a session of its own."""
    pick = daily.Pick(DAY, "codex", TASK)
    results = tmp_path / "results"
    with FakeThreefold(stage="enforce", status_code=503) as fake:
        code, row, note = daily.run_live(pick, fake.endpoint, results_dir=results, work_root=tmp_path / "work",
                                         codex=str(fake_bin["codex"]), retry_pause=0)
        assert (code, fake.evaluations()) == (1, [])
        assert "answered GET status with 503" in row["harness_error"] and "the run did not go as planned" in note
        line = daily.summary_line(pick, row, note)
        assert "harness error:" in line and "not counted" in line

        fake.status_code = 200
        code, row, note = daily.run_live(pick, fake.endpoint, results_dir=results, work_root=tmp_path / "work",
                                         codex=str(fake_bin["codex"]), retry_pause=0)
        sent = fake.evaluations()
    assert (code, note) == (0, ""), note
    assert (row["attempt"], row["threefold_session"], row["harness_error"]) == (2, f"{SESSION}-a2", None)
    assert sent and {body["session_id"] for body in sent} == {f"{SESSION}-a2"}
    assert [r["attempt"] for r in _rows(results / f"{DAY}-codex.jsonl")] == [1, 2]
    log = (tmp_path / "work" / daily.LOG_NAME).read_text(encoding="utf-8")
    assert f"--resume {DAY}-codex" in log

    again = daily.run_live(pick, fake.endpoint, results_dir=results, work_root=tmp_path / "work", codex=str(fake_bin["codex"]))
    assert again[0] == 0 and again[1] == row and again[2].startswith("already recorded today")
    assert len([call for call in fake_agents.calls(fake_bin["dir"], "codex") if call["argv"][:1] == ["exec"]]) == 1


def test_a_day_is_run_at_most_three_times(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "main", lambda *args, **kwargs: pytest.fail("ran a fourth time"))
    pick = daily.Pick(DAY, "codex", TASK)
    results = tmp_path / "results"
    endpoint = "https://threefold.acme.test/"
    for _ in range(daily.MAX_RUNS_PER_DAY):
        run.ResultsFile(results / f"{pick.run_id}.jsonl").append(_failed_row(endpoint))
    code, row, note = daily.run_live(pick, endpoint, results_dir=results, work_root=tmp_path / "work")
    assert code == 1 and row["harness_error"]
    assert note.startswith(f"today already has 3 rows in {pick.run_id}.jsonl and the last did not go as planned")


def test_what_a_row_means_for_the_day():
    endpoint = "https://threefold.acme.test/"
    pick = daily.Pick(DAY, "codex", TASK)
    failed = _failed_row(endpoint)
    assert daily.day_outcome(failed, pick, endpoint)[0::2] == (1, True)
    elsewhere = dict(failed, threefold_endpoint="https://elsewhere.acme.test/")
    assert daily.day_outcome(elsewhere, pick, endpoint)[0::2] == (1, False)
    ran = dict(failed, harness_error=None, agent_ran=True, measured=True, run_end="completed", acceptance_passed=True,
               violation_landed=True, server_healthy_after=True, governance_problem=None,
               ledger={"reachable": True, "complete": True, "decisions": 2, "refused": 0, "would_refuse": 1,
                       "stages": {"observe": 2}, "project": PROJECT})
    observe = dict(ran, governance_problem=harness.stage_problem(ran))
    assert daily.day_outcome(observe, pick, endpoint) == (0, "", False)
    down = dict(observe, server_healthy_after=False, governance_problem=harness.governance_problem(
        "threefold", dict(observe, server_healthy_after=False)))
    code, note, again = daily.day_outcome(down, pick, endpoint)
    assert (code, again) == (1, True) and "was not answering when the agent stopped" in note
    login = dict(failed, harness_error=None, service_failure="auth", agent_error="the login expired")
    assert daily.day_outcome(login, pick, endpoint)[0::2] == (run.STOPPED_EXIT_CODE, True)


def test_a_row_that_is_not_the_run_planned_is_said_to_be_so(tmp_path, monkeypatch):
    """After the run, the row must name the endpoint, project and session given; a row naming another is an error."""
    pick = daily.Pick(DAY, "codex", TASK)
    results = tmp_path / "results"

    def elsewhere(arguments):
        run.ResultsFile(results / f"{pick.run_id}.jsonl").append({
            "agent": "codex", "task": TASK, "condition": "threefold", "ledger_source": "remote",
            "threefold_endpoint": "https://elsewhere.acme.test/", "threefold_project": PROJECT, "threefold_session": SESSION})
        return 0

    monkeypatch.setattr(run, "main", elsewhere)
    code, row, note = daily.run_live(pick, "https://threefold.acme.test/", results_dir=results, work_root=tmp_path / "work")
    assert code == 1 and "threefold_endpoint is 'https://elsewhere.acme.test/'" in note


def test_the_row_check_takes_a_later_attempt_s_session():
    pick = daily.Pick(DAY, "codex", TASK)
    row = {"agent": "codex", "task": TASK, "condition": "threefold", "ledger_source": "remote",
           "threefold_endpoint": "https://threefold.acme.test/", "threefold_project": PROJECT, "threefold_session": SESSION}
    assert daily.mismatch(row, pick, "https://threefold.acme.test/") is None
    assert daily.mismatch(dict(row, threefold_session=SESSION + "-a2"), pick, "https://threefold.acme.test/") is None
    assert "session" in daily.mismatch(dict(row, threefold_session="live-other"), pick, "https://threefold.acme.test/")
    assert "ledger_source" in daily.mismatch(dict(row, ledger_source="local"), pick, "https://threefold.acme.test/")


def test_main_runs_today_s_pick_against_the_endpoint_given_and_exits_as_the_run_did(tmp_path, monkeypatch, capsys):
    """Wired to run_live with answers run_live really gives: a counted row and exit 0, then no row and exit 1."""
    monkeypatch.setattr(daily, "utc_today", lambda: datetime.date(2026, 10, 3))
    session = "live-orders-s3-archive-2026-10-03"
    counted = {"agent": "claude-code", "task": "orders-s3-archive", "condition": "threefold", "rep": 1, "agent_ran": True,
               "measured": True, "run_end": "completed", "harness_error": None, "acceptance_passed": True,
               "violation_landed": False, "hook_refusals": 1, "governance_problem": None, "server_healthy_after": True,
               "ledger_source": "remote", "threefold_endpoint": "https://threefold.acme.test/prod/",
               "threefold_project": "Acme-Live-orders-s3-archive", "threefold_session": session,
               "ledger": {"reachable": True, "complete": True, "decisions": 2, "refused": 1, "would_refuse": 0,
                          "stages": {"enforce": 2}}}
    answers = [(0, counted, ""), (1, None, "no row was recorded (benchmark exit 1); see <work root>/daily-live.log")]
    seen = []

    def fake_run_live(pick, endpoint, *args):
        seen.append((pick, endpoint, args[1]))
        return answers[len(seen) - 1]

    monkeypatch.setattr(daily, "run_live", fake_run_live)
    argv = ["--endpoint", "https://threefold.acme.test/prod", "--results-dir", str(tmp_path / "results"),
            "--work-root", str(tmp_path / "work")]
    assert daily.main(argv) == 0
    assert seen[0] == (daily.Pick("2026-10-03", "claude-code", "orders-s3-archive"), "https://threefold.acme.test/prod/",
                       tmp_path / "work")
    assert capsys.readouterr().out.strip() == (
        "live 2026-10-03 claude-code on orders-s3-archive: completed, no violation, tests pass, 1 refusal seen by the "
        "agent; the remote ledger holds 2 calls (1 refused, 0 would refuse; judged 2 in enforce); session "
        f"{session} of Acme-Live-orders-s3-archive; counted; row in 2026-10-03-claude-code.jsonl")
    assert daily.main(argv) == 1
    assert capsys.readouterr().out.strip() == ("live 2026-10-03 claude-code on orders-s3-archive: no row. no row was "
                                               "recorded (benchmark exit 1); see <work root>/daily-live.log")
