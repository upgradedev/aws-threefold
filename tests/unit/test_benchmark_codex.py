"""A Codex run gets the hook exactly as the installer registers it, the rules in AGENTS.md, and is judged by its own names.

No Codex runs here. The stand-in `codex` (benchmark/fake_agents.py) answers in
the event shapes the harness reads and, in a Threefold run, calls the hook the
repository's .codex/hooks.json registers, which reports to the run's own local
server. The installer is run only into a temporary repository, with HOME,
USERPROFILE, THREEFOLD_HOME and git's global configuration in the test's own
folder, as its own suite does.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmark import codex_agent, credentials, fake_agents, harness, report, run, task_library  # noqa: E402

# The flag lines of `codex exec --help`, codex-cli 0.155.0, as printed on 2026-09-22.
HELP_0_155_0 = """Run Codex non-interactively

Usage: codex exec [OPTIONS] [PROMPT]
  -c, --config <key=value>
      --enable <FEATURE>
      --disable <FEATURE>
  -m, --model <MODEL>
  -s, --sandbox <SANDBOX_MODE>
      --dangerously-bypass-approvals-and-sandbox
      --dangerously-bypass-hook-trust
  -C, --cd <DIR>
      --skip-git-repo-check
      --ephemeral
      --ignore-user-config
      --ignore-rules
      --color <COLOR>
      --json
  -o, --output-last-message <FILE>
"""


@pytest.fixture(autouse=True)
def _confined(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_TOKEN_FILE", tmp_path / "no-such-token-file")
    monkeypatch.setattr(harness, "claude_memory_above", lambda path: [])
    (tmp_path / "codex-home").mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    fake_agents.install(bin_dir, "codex")
    kept = [str(bin_dir)] + [entry for entry in os.environ.get("PATH", "").split(os.pathsep)
                             if entry and not shutil.which("claude", path=entry) and not shutil.which("codex", path=entry)]
    monkeypatch.setenv("PATH", os.pathsep.join(kept))
    assert Path(shutil.which("codex")).parent == bin_dir
    return bin_dir


# --- the repository a Codex run starts from ---------------------------------------------------

def test_the_hook_file_and_matcher_are_the_installer_s_own():
    assert (harness.CODEX_HOOK_FILE, harness.CODEX_HOOK_MATCHER) == harness.INSTALLER.AGENT_SETTINGS["codex"]
    assert harness.CODEX_HOOK_FILE == ".codex/hooks.json"
    assert harness.CODEX_HOOK_MATCHER == "apply_patch|Edit|Write|Bash"


def test_a_codex_threefold_run_registers_the_hook_in_codex_hooks_json(tmp_path):
    task = task_library.load_tasks(["orders-s3-archive"])[0]
    work_root, run_dir = tmp_path / "work", tmp_path / "work" / "run"
    repo = run_dir / "repo"
    harness.prepare_repository(task, "threefold", repo, agent="codex")
    installed = harness.install_hook(task, repo, run_dir, work_root, "http://127.0.0.1:9/", agent="codex")

    text = (repo / ".codex" / "hooks.json").read_text(encoding="utf-8")
    command = installed["command"]
    expected = {"hooks": {"PreToolUse": [{"matcher": "apply_patch|Edit|Write|Bash",
                                          "hooks": [{"type": "command", "command": command}]}]}}
    assert text == harness.INSTALLER.dump_json(expected)
    assert installed["hook_file"] == ".codex/hooks.json"
    assert not (repo / ".claude").exists() and not (repo / "AGENTS.md").exists()
    wrapper = (run_dir / "bin" / "threefold_hook_wrapper.py").read_text(encoding="utf-8")
    assert '"--agent", "codex"' in wrapper and harness.forward(run_dir / harness.HOOK_LOG_NAME) in wrapper
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".codex/hooks.json" in exclude and ".threefold.json" in exclude
    assert harness.changed_files(repo) == []
    config = json.loads((repo / ".threefold.json").read_text(encoding="utf-8"))
    assert config == {"project": "Acme-Bench-orders-s3-archive", "endpoint": "http://127.0.0.1:9/", "mode": "enforce"}


def test_the_entry_has_the_shape_the_installer_itself_writes(tmp_path, monkeypatch):
    """The installer, run into a temporary repository, writes the same document but for the command it runs."""
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "gitconfig").write_text("", encoding="utf-8")
    for name in [name for name in os.environ if name.startswith("THREEFOLD_") and name != "THREEFOLD_OFFLINE"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in (("HOME", str(home)), ("USERPROFILE", str(home)), ("THREEFOLD_HOME", str(home / ".threefold")),
                        ("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig")), ("GIT_CONFIG_NOSYSTEM", "1"),
                        ("THREEFOLD_TIMEOUT", "0.5")):
        monkeypatch.setenv(name, value)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = f"http://127.0.0.1:{probe.getsockname()[1]}/prod/"
    (home / ".threefold").mkdir()
    (home / ".threefold" / "config.json").write_text(json.dumps({"endpoint": closed}), encoding="utf-8")
    installed_repo = tmp_path / "acme-installed"
    installed_repo.mkdir()
    assert subprocess.run(["git", "-C", str(installed_repo), "init", "-q"], capture_output=True).returncode == 0
    out = io.StringIO()
    assert harness.INSTALLER.main(["--repo", str(installed_repo), "--project", "Acme-Bench", "--agents", "codex",
                                   "--mode", "enforce"], out) == 0

    task = task_library.load_tasks(["orders-s3-archive"])[0]
    bench_repo = tmp_path / "work" / "run" / "repo"
    harness.prepare_repository(task, "threefold", bench_repo, agent="codex")
    harness.install_hook(task, bench_repo, tmp_path / "work" / "run", tmp_path / "work", closed, agent="codex")

    def shape(path: Path) -> str:
        document = json.loads(path.read_text(encoding="utf-8"))
        for entry in document["hooks"]["PreToolUse"]:
            for hook in entry["hooks"]:
                hook["command"] = "<command>"
        return harness.INSTALLER.dump_json(document)

    assert shape(bench_repo / ".codex" / "hooks.json") == shape(installed_repo / ".codex" / "hooks.json")


def test_the_prompt_condition_gives_codex_the_rules_in_agents_md(tmp_path):
    task = task_library.load_tasks(["orders-s3-archive"])[0]
    harness.prepare_repository(task, "prompt", tmp_path / "repo", agent="codex")
    assert (tmp_path / "repo" / "AGENTS.md").read_bytes() == harness.RULES_FILE.read_bytes()
    assert not (tmp_path / "repo" / "CLAUDE.md").exists()
    assert harness.changed_files(tmp_path / "repo") == []


# --- the command --------------------------------------------------------------------------------

def test_the_codex_command_is_headless_isolated_and_takes_the_prompt_on_stdin(tmp_path):
    repo = tmp_path / "work" / "task--codex--none--r1" / "repo"
    options = harness.AgentOptions(agent="codex", codex="codex", model=None)
    task = task_library.load_tasks(["payments-staging-key"])[0]
    command = harness.build_agent_command(options, task, repo=repo)
    assert command[:3] == ["codex", "exec", "--json"] and command[-1] == "-"
    joined = " ".join(command)
    for expected in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--sandbox workspace-write",
                     "--config approval_policy='never'", "--enable hooks", "--dangerously-bypass-hook-trust",
                     f"--cd {repo}", f"projects={{'{repo}'={{trust_level='trusted'}}}}"):
        assert expected in joined
    assert "--dangerously-bypass-approvals-and-sandbox" not in joined and "--model" not in command
    assert all(task.secrets["STAGING_KEY"] not in part for part in command)
    assert "--model" in harness.build_agent_command(harness.AgentOptions(agent="codex", model="gpt-acme"), task, repo=repo)


def test_every_flag_the_command_passes_is_checked_against_the_help():
    assert codex_agent.exec_flags_problem(HELP_0_155_0) is None
    older = HELP_0_155_0.replace("      --dangerously-bypass-hook-trust\n", "").replace("      --ignore-rules\n", "")
    problem = codex_agent.exec_flags_problem(older)
    assert "--dangerously-bypass-hook-trust" in problem and "--ignore-rules" in problem
    assert "--dangerously-bypass-approvals-and-sandbox" not in codex_agent.REQUIRED_EXEC_FLAGS
    assert codex_agent.exec_flags_problem(fake_agents.CODEX_EXEC_HELP) is None
    # --model is passed only when a model is pinned, and is checked all the same.
    assert "--model" in codex_agent.exec_flags_problem(HELP_0_155_0.replace("  -m, --model <MODEL>\n", ""))
    pinned = codex_agent.build_command("codex", Path("C:/acme/repo"), "gpt-acme")
    assert {part for part in pinned if part.startswith("--")} <= set(codex_agent.REQUIRED_EXEC_FLAGS)


def test_a_path_that_cannot_be_a_toml_literal_is_refused():
    with pytest.raises(ValueError):
        codex_agent.toml_literal("C:/acme/it's")


# --- reading what Codex printed ---------------------------------------------------------------------

def _events(tmp_path, messages):
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(message) for message in messages) + "\n", encoding="utf-8")
    return path


def test_the_events_yield_tools_refusals_tokens_and_an_ending(tmp_path):
    reason = ("Threefold refused this call (BLOCKED_BOUNDARY_VIOLATION). Clean Architecture violation: Layering rule "
              "'python-domain-stays-pure' refuses this write")
    path = _events(tmp_path, [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "pytest", "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "pytest",
                                            "aggregated_output": "3 passed", "exit_code": 0, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "file_change", "status": "failed",
                                            "changes": [{"path": "src/domain/x.py", "kind": "add"}]}},
        {"type": "item.completed", "item": {"id": "i3", "type": "error", "message": reason}},
        {"type": "item.completed", "item": {"id": "i4", "type": "command_execution", "command": "curl x",
                                            "aggregated_output": "", "status": "declined"}},
        {"type": "item.completed", "item": {"id": "i5", "type": "reasoning", "text": "thinking"}},
        {"type": "item.completed", "item": {"id": "i6", "type": "agent_message", "text": "Done."}},
        {"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 400, "output_tokens": 120,
                                             "reasoning_output_tokens": 30}},
    ])
    summary = codex_agent.parse_events(path, classify=harness.refusal_kind)
    metrics = harness.agent_metrics(summary, harness.Sanitiser(tmp_path))
    assert metrics["tool_uses"] == {"command_execution": 2, "file_change": 1}
    assert metrics["hook_refusals"] == 1 and metrics["hook_refusals_by_kind"] == {"LAYERING": 1}
    assert (metrics["input_tokens"], metrics["output_tokens"], metrics["cache_read_input_tokens"]) == (900, 120, 400)
    assert metrics["reasoning_output_tokens"] == 30 and metrics["cost_usd"] is None
    assert metrics["num_turns"] == 4 and metrics["assistant_messages"] == 1
    assert (metrics["run_end"], metrics["measured"], metrics["agent_ran"]) == ("completed", True, True)
    assert metrics["permission_denied_calls"] == ["command_execution: curl x"]


@pytest.mark.parametrize("message, ending, kind", [
    ("You've hit your usage limit. Try again at 9:00 AM.", "cut_short:usage_limit", "usage_limit"),
    ("stream error: 503 Service Unavailable", "cut_short:overloaded", "overloaded"),
    ("unexpected status 401 Unauthorized", "not_run", "auth"),
    ("the sandbox could not start", "not_run", None),
])
def test_a_failed_turn_says_whether_the_service_stopped_it(tmp_path, message, ending, kind):
    path = _events(tmp_path, [
        {"type": "thread.started", "thread_id": "t1"}, {"type": "turn.started"},
        {"type": "error", "message": message}, {"type": "turn.failed", "error": {"message": message}},
    ])
    metrics = harness.agent_metrics(codex_agent.parse_events(path), harness.Sanitiser(tmp_path))
    assert (metrics["run_end"], metrics["service_failure"], metrics["measured"]) == (ending, kind, False)
    assert metrics["agent_error"] == message


def test_a_codex_run_killed_at_the_timeout_still_measured_the_agent(tmp_path):
    path = _events(tmp_path, [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.started", "item": {"id": "i1", "type": "command_execution", "status": "in_progress"}},
    ])
    metrics = harness.agent_metrics(codex_agent.parse_events(path), harness.Sanitiser(tmp_path), True, 600)
    assert (metrics["run_end"], metrics["measured"]) == ("timeout", True)


def test_the_hook_log_stands_in_for_the_hook_events_codex_does_not_print(tmp_path):
    log = tmp_path / harness.HOOK_LOG_NAME
    log.write_text("\n".join(json.dumps(record) for record in (
        {"exit": 0, "crashed": False, "decided": True, "unjudged": False},
        {"exit": 0, "crashed": False, "decided": False, "unjudged": True},
        {"exit": 1, "crashed": True, "decided": False, "unjudged": False},
    )) + "\n", encoding="utf-8")
    summary = codex_agent.merge_hook_log(codex_agent.parse_events(tmp_path / "none.jsonl"), codex_agent.read_hook_log(log))
    assert summary["hook_events"] == {"hook_response": 3}
    assert (summary["hook_unjudged"], summary["hook_errors"]) == (1, 1)


def _codex_row(**extra):
    row = {"agent": "codex", "condition": "threefold", "agent_ran": True, "measured": True, "harness_error": None,
           "acceptance_passed": True, "server_healthy_after": True, "ledger": {"reachable": True, "decisions": 3, "refused": 1},
           "tool_uses": {"command_execution": 2, "file_change": 1}, "hook_refusals": 1,
           "hook_events": {"hook_response": 3}, "hook_unjudged": 0, "hook_errors": 0}
    row.update(extra)
    row.update(harness.hook_check("threefold", row, row["ledger"], "codex"))
    row["governance_problem"] = harness.governance_problem("threefold", row)
    return row


def test_a_governed_codex_run_counts():
    row = _codex_row()
    assert row["governed_calls"] == 3 and row["governance_problem"] is None and report.is_valid(row)


def test_a_codex_run_whose_hook_never_ran_is_not_a_threefold_measurement():
    """With Claude Code's tool names, Codex's calls would count as none governed and this run would slip through."""
    row = _codex_row(ledger={"reachable": True, "decisions": 0, "refused": 0}, hook_refusals=0, hook_events={})
    assert row["hook_missing"] and row["governed_calls"] == 3
    assert not report.is_valid(row)
    assert "never fired although the agent made 3 governed call(s); Codex did not load it" in report.invalid_reason(row)


def test_a_codex_run_whose_hook_ran_but_judged_nothing_is_not_a_measurement():
    row = _codex_row(ledger={"reachable": True, "decisions": 0, "refused": 0}, hook_refusals=0)
    assert "no decision reached the ledger" in row["governance_problem"] and not report.is_valid(row)


# --- before a Codex matrix ----------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["AGENTS.md", "AGENTS.override.md", "hooks.json"])
def test_a_codex_home_with_instructions_or_hooks_of_the_owner_s_is_refused(tmp_path, fake_bin, capsys, name):
    (tmp_path / "codex-home" / name).write_text("owner's own\n", encoding="utf-8")
    code = run.main(["--agent", "codex", "--tasks", "orders-s3-archive", "--reps", "1", "--work-root", str(tmp_path / "work"),
                     "--results-dir", str(tmp_path / "results")])
    assert code == 2 and name in capsys.readouterr().err
    assert [call for call in fake_agents.calls(fake_bin, "codex") if call["argv"][:1] == ["exec"]] == []


def test_a_token_file_is_not_offered_to_codex():
    with pytest.raises(SystemExit):
        run.parse_args(["--agent", "codex", "--token-file", "x"])


def test_a_codex_run_id_says_so():
    import datetime

    moment = datetime.datetime(2026, 9, 27, 8, 0, 0, tzinfo=datetime.timezone.utc)
    assert run.default_run_id(False, "codex", moment) == "20260927T080000Z-codex"
    assert run.default_run_id(True, "codex", moment) == "20260927T080000Z-pilot-codex"


# --- a whole matrix with a stand-in codex ----------------------------------------------------------

def test_a_codex_matrix_runs_every_condition_and_the_hook_reports_to_the_run_s_server(tmp_path, fake_bin, capsys):
    code = run.main(["--agent", "codex", "--tasks", "orders-s3-archive", "--conditions", "none,prompt,threefold",
                     "--reps", "1", "--work-root", str(tmp_path / "work"), "--results-dir", str(tmp_path / "results"),
                     "--retry-pause", "0"])
    printed = capsys.readouterr()
    assert code == 0, printed.err
    rows = {row["condition"]: row for row in
            (json.loads(line) for path in (tmp_path / "results").glob("*.jsonl")
             for line in path.read_text(encoding="utf-8").splitlines())}
    assert set(rows) == {"none", "prompt", "threefold"}
    for row in rows.values():
        assert (row["agent"], row["auth"], row["model"]) == ("codex", "machine-login", "codex-default")
        assert row["agent_version"] == fake_agents.CODEX_VERSION
        assert row["run_end"] == "completed" and row["harness_error"] is None
        assert row["isolation"]["mode"] == "codex-user-login"

    threefold = rows["threefold"]
    assert threefold["hook_file"] == ".codex/hooks.json"
    assert threefold["ledger"]["decisions"] >= 2 and threefold["ledger"]["refused"] >= 1
    assert threefold["hook_refusals_by_kind"] == {"LAYERING": 1}
    assert threefold["hook_events"] == {"hook_response": 2}
    assert (threefold["hook_unjudged"], threefold["hook_errors"], threefold["governance_problem"]) == (0, 0, None)
    assert threefold["refused_at_least_once"] is True and report.is_valid(threefold)
    assert rows["none"]["hook_fired"] is None and rows["none"]["ledger"] is None

    work = tmp_path / "work"
    assert (work / "orders-s3-archive--codex--prompt--r1" / "repo" / "AGENTS.md").is_file()
    assert not (work / "orders-s3-archive--codex--none--r1" / "repo" / ".codex").exists()
    execs = [call for call in fake_agents.calls(fake_bin, "codex") if call["argv"][:1] == ["exec"]]
    assert len(execs) == 3
    for call in execs:
        assert call["argv"][-1] == "-" and "--dangerously-bypass-hook-trust" in call["argv"]
        assert Path(call["argv"][call["argv"].index("--cd") + 1]) == Path(call["repo"]) and Path(call["repo"]).name == "repo"
        assert call["codex_home"] == str(tmp_path / "codex-home") and call["token_set"] is False
        assert call["prompt_chars"] > 0
