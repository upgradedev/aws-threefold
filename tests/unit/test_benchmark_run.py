"""The runner builds the command it claims to, keeps the owner's machine out of every run, and reads what came back.

No test here starts an agent: the agent process is replaced wherever a run
would start one. The hook is run only through the per-run wrapper, inside a
temporary repository, with its home folders in the temporary run directory.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmark import harness, run, task_library  # noqa: E402


def _options(**overrides):
    values = dict(agent="claude", claude="claude", model="claude-sonnet-5", max_turns=40, timeout_s=600,
                  budget_usd=3.0, isolation="user-config")
    values.update(overrides)
    return harness.AgentOptions(**values)


# --- the command ----------------------------------------------------------------------

def test_the_agent_command_is_headless_stream_json_and_isolated():
    command = harness.build_agent_command(_options())
    assert command[:2] == ["claude", "-p"]
    joined = " ".join(command)
    for expected in ("--output-format stream-json", "--verbose", "--include-hook-events", "--model claude-sonnet-5",
                     "--max-turns 40", "--max-budget-usd 3", "--permission-mode acceptEdits",
                     "--setting-sources project,local", "--strict-mcp-config", "--disable-slash-commands",
                     "--no-session-persistence"):
        assert expected in joined
    assert "bypassPermissions" not in joined and "--dangerously-skip-permissions" not in joined


def test_the_prompt_is_never_on_the_command_line():
    """--allowedTools takes many values, so a prompt after it would be read as one more tool."""
    task = task_library.load_tasks(["payments-staging-key"])[0]
    command = harness.build_agent_command(_options(), task)
    assert command[-len(harness.ALLOWED_TOOLS):] == list(harness.ALLOWED_TOOLS)
    assert all(task.secrets["STAGING_KEY"] not in part for part in command)
    assert all("refund" not in part.lower() for part in command)


def test_the_agent_may_edit_and_test_but_not_install_or_reach_out():
    allowed, refused = set(harness.ALLOWED_TOOLS), set(harness.DISALLOWED_TOOLS)
    assert {"Edit", "Write", "Bash(python:*)", "Bash(dotnet run:*)"} <= allowed
    assert {"Bash(pip:*)", "Bash(python -m pip:*)", "Bash(dotnet add:*)", "Bash(npm:*)", "Bash(curl:*)",
            "Bash(aws:*)", "Bash(git push:*)", "WebFetch", "WebSearch"} <= refused
    assert not {tool for tool in allowed if tool.startswith(("Bash(rm", "Bash(curl", "Bash(aws"))}


def test_the_matcher_is_the_installer_s():
    for candidate in (REPO_ROOT / "src" / "threefold" / "tools" / "threefold_install.py",
                      REPO_ROOT / "scripts" / "threefold_install.py"):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            match = re.search(r'"claude-code":\s*\("\.claude/settings\.local\.json",\s*"([^"]+)"\)', text)
            if match:
                assert match.group(1) == harness.HOOK_MATCHER
                return
    pytest.skip("the installer's matcher table was not found where it used to live")


def test_the_scripted_agent_runs_a_script_not_a_model():
    task = task_library.load_tasks(["orders-s3-archive"])[0]
    command = harness.build_agent_command(_options(agent="scripted"), task)
    assert command[1].endswith("scripted_agent.py") and command[-2:] == ["--task", "orders-s3-archive"]


# --- the environment ----------------------------------------------------------------------

def test_nothing_from_the_host_session_or_the_owner_s_settings_reaches_a_run(tmp_path):
    host = {
        "PATH": "/usr/bin", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "desktop", "CLAUDE_CODE_SESSION_ID": "x",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ANTHROPIC_API_KEY": "not-for-the-agent",
        "THREEFOLD_ENDPOINT": "https://owner.example/", "THREEFOLD_PROJECT": "Acme-Owner", "THREEFOLD_HOME": "/owner",
        "AWS_ACCESS_KEY_ID": "owner", "AWS_PROFILE": "owner", "CLAUDE_CODE_OAUTH_TOKEN": "kept-for-login",
    }
    env = harness.agent_environment(host, tmp_path, "user-config")
    assert not [key for key in env if key.startswith(("THREEFOLD", "ANTHROPIC")) or key == "CLAUDECODE"]
    assert "CLAUDE_CODE_ENTRYPOINT" not in env and "CLAUDE_CODE_SESSION_ID" not in env
    assert "AWS_ACCESS_KEY_ID" not in env and "AWS_PROFILE" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "kept-for-login"
    assert Path(env["AWS_SHARED_CREDENTIALS_FILE"]).parent == tmp_path / "aws"
    assert not Path(env["AWS_SHARED_CREDENTIALS_FILE"]).exists()
    assert Path(env["NUGET_PACKAGES"]).is_relative_to(tmp_path)
    assert "CLAUDE_CONFIG_DIR" not in env


def test_fresh_config_gives_each_run_its_own_configuration_folder(tmp_path):
    env = harness.agent_environment({"CLAUDE_CODE_OAUTH_TOKEN": "t"}, tmp_path, "fresh-config")
    assert Path(env["CLAUDE_CONFIG_DIR"]) == tmp_path / "claude-config"
    assert (tmp_path / "claude-config").is_dir()


def test_isolation_follows_the_login_available():
    assert harness.choose_isolation("auto", {"CLAUDE_CODE_OAUTH_TOKEN": "t"}) == "fresh-config"
    assert harness.choose_isolation("auto", {}) == "user-config"
    with pytest.raises(ValueError):
        harness.choose_isolation("fresh-config", {})


def test_the_work_root_must_be_outside_the_repository_and_the_workspace(tmp_path):
    with pytest.raises(ValueError):
        harness.ensure_outside_workspace(REPO_ROOT / "benchmark" / "work")
    harness.ensure_outside_workspace(tmp_path / "threefold-bench" / "run")


def test_the_local_server_is_offline_enforcing_and_confined(tmp_path):
    server = harness.LocalServer(tmp_path, port=45678)
    assert server.command()[1:] == ["-m", "threefold.interfaces.server", "--port", "45678", "--host", "127.0.0.1"]
    env = server.environment({"THREEFOLD_ENDPOINT": "https://owner.example/", "AWS_SECRET_ACCESS_KEY": "owner"})
    assert env["THREEFOLD_OFFLINE"] == "1" and env["DEFAULT_HOOK_STAGE"] == "enforce"
    assert env["PYTHONPATH"] == str(REPO_ROOT / "src")
    assert Path(env["HOME"]).is_relative_to(tmp_path) and Path(env["USERPROFILE"]).is_relative_to(tmp_path)
    assert "THREEFOLD_ENDPOINT" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert server.endpoint == "http://127.0.0.1:45678/"


# --- driving the agent, with the process replaced -----------------------------------------------

class _FakeProcess:
    instances = []

    def __init__(self, command, cwd, env, stdin, stdout, stderr, **kwargs):
        self.command, self.cwd, self.env = command, cwd, env
        self.stdin = _Sink()
        self.pid = 4242
        self.returncode = None
        stdout.write(b'{"type":"result","subtype":"success","num_turns":3}\n')
        _FakeProcess.instances.append(self)

    def wait(self, timeout=None):
        if self.env.get("FAKE_HANG"):
            raise subprocess.TimeoutExpired(self.command, timeout)
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode


class _Sink:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    def close(self):
        pass


def test_run_agent_feeds_the_prompt_on_stdin_in_the_task_repository(tmp_path, monkeypatch):
    _FakeProcess.instances.clear()
    monkeypatch.setattr(harness.subprocess, "Popen", _FakeProcess)
    code, timed_out, _ = harness.run_agent(["claude", "-p"], "archive the order", tmp_path, {"A": "1"}, 60,
                                           tmp_path / "t.jsonl", tmp_path / "e.txt")
    process = _FakeProcess.instances[-1]
    assert (code, timed_out) == (0, False)
    assert process.stdin.data == b"archive the order"
    assert process.cwd == str(tmp_path) and process.env == {"A": "1"}
    assert harness.parse_transcript(tmp_path / "t.jsonl")["result"]["num_turns"] == 3


def test_a_run_that_overruns_is_stopped_with_everything_it_started(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(harness.subprocess, "Popen", _FakeProcess)
    monkeypatch.setattr(harness, "kill_tree", lambda process: killed.append(process.pid))
    code, timed_out, _ = harness.run_agent(["claude"], "x", tmp_path, {"FAKE_HANG": "1"}, 1,
                                           tmp_path / "t.jsonl", tmp_path / "e.txt")
    assert (code, timed_out, killed) == (None, True, [4242])


# --- reading what came back -------------------------------------------------------------------

def _stream(tmp_path, messages):
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(message) for message in messages) + "\n", encoding="utf-8")
    return path


def test_the_transcript_yields_turns_cost_tokens_tools_and_refusals(tmp_path):
    path = _stream(tmp_path, [
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5", "claude_code_version": "2.1.220", "permissionMode": "acceptEdits"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a", "name": "Write", "input": {}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "is_error": True, "content": [
            {"type": "text", "text": "Threefold refused this call (BLOCKED_BOUNDARY_VIOLATION). Clean Architecture violation: ..."}]}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "b", "name": "Write", "input": {}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "b", "is_error": True,
            "content": "Threefold refused this call before it left the machine: it contains a credential (OPENAI_KEY)."}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "c", "name": "Bash", "input": {}}]}},
        {"type": "system", "subtype": "hook_response"},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 7, "duration_ms": 81234,
         "total_cost_usd": 0.4123, "usage": {"input_tokens": 12, "output_tokens": 900, "cache_read_input_tokens": 5000,
                                              "cache_creation_input_tokens": 700}, "permission_denials": [{"tool_name": "Bash"}]},
    ])
    metrics = harness.agent_metrics(harness.parse_transcript(path), harness.Sanitiser(tmp_path))
    assert metrics["agent_ran"] and metrics["agent_error"] == ""
    assert (metrics["num_turns"], metrics["cost_usd"], metrics["output_tokens"]) == (7, 0.4123, 900)
    assert metrics["tool_uses"] == {"Write": 2, "Bash": 1}
    assert metrics["hook_refusals"] == 2
    assert metrics["hook_refusals_by_kind"] == {"LAYERING": 1, "CREDENTIAL": 1}
    assert metrics["permission_denials"] == 1 and metrics["hook_events"] == {"hook_response": 1}
    assert metrics["claude_code_version"] == "2.1.220"


def test_an_expired_login_is_recorded_as_an_agent_that_did_not_run(tmp_path):
    path = _stream(tmp_path, [
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5"},
        {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "total_cost_usd": 0,
         "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
         "usage": {"input_tokens": 0, "output_tokens": 0}},
    ])
    metrics = harness.agent_metrics(harness.parse_transcript(path), harness.Sanitiser(tmp_path))
    assert metrics["agent_ran"] is False
    assert metrics["agent_error"].startswith("Failed to authenticate")


@pytest.mark.parametrize("reason, kind", [
    ("it contains a credential (AWS_ACCESS_KEY)", "CREDENTIAL"),
    ("... use Write or Edit so the rule can read it", "UNREADABLE_WRITE"),
    ("Clean Architecture violation: Layering rule 'python-domain-stays-pure' refuses this write", "LAYERING"),
    ("it changes .claude/settings.local.json, which decides whether the agent's hooks run", "PROTECTED_PATH"),
    ("Threefold refused this call (BLOCKED_LOOP_DETECTED)", "LOOP"),
    ("something else", "OTHER"),
])
def test_a_refusal_is_attributed_to_its_gate(reason, kind):
    assert harness.refusal_kind(reason) == kind


def test_recorded_text_carries_no_machine_paths(tmp_path):
    sanitise = harness.Sanitiser(tmp_path)
    text = sanitise(f"failed in {tmp_path / 'x'} under {Path.home() / 'y'}")
    assert str(tmp_path) not in text and str(Path.home()) not in text
    assert "<work>" in text and "~" in text


# --- the hook, installed as a real install would be ----------------------------------------------

def test_the_hook_is_installed_from_a_copy_and_confined_to_the_run(tmp_path):
    task = task_library.load_tasks(["orders-s3-archive"])[0]
    work_root, run_dir = tmp_path / "work", tmp_path / "work" / "run"
    repo = run_dir / "repo"
    harness.prepare_repository(task, "threefold", repo)
    installed = harness.install_hook(task, repo, run_dir, work_root, "http://127.0.0.1:9/")

    settings = json.loads((repo / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    entry = settings["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == harness.HOOK_MATCHER
    command = entry["hooks"][0]["command"]
    assert harness.forward(run_dir / "bin" / "threefold_hook_wrapper.py") in command
    assert str(REPO_ROOT / "src") not in command and harness.forward(REPO_ROOT) not in command
    assert (work_root / "bin" / "threefold_hook.py").read_bytes() == harness.HOOK_SOURCE.read_bytes()
    assert installed["hook_sha256"] == harness.sha256_file(harness.HOOK_SOURCE)
    config = json.loads((repo / ".threefold.json").read_text(encoding="utf-8"))
    assert config == {"project": "Acme-Bench-orders-s3-archive", "endpoint": "http://127.0.0.1:9/", "mode": "enforce"}
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".threefold.json" in exclude and ".claude/settings.local.json" in exclude
    assert harness.changed_files(repo) == []
    wrapper = (run_dir / "bin" / "threefold_hook_wrapper.py").read_text(encoding="utf-8")
    assert harness.forward(run_dir / "threefold-home") in wrapper and harness.forward(run_dir / "home") in wrapper


def test_the_installed_hook_refuses_a_credential_without_any_server(tmp_path):
    """The wrapper runs the copied hook with its homes inside the run; no network is needed for this refusal."""
    task = task_library.load_tasks(["payments-staging-key"])[0]
    work_root, run_dir = tmp_path / "work", tmp_path / "work" / "run"
    repo = run_dir / "repo"
    harness.prepare_repository(task, "threefold", repo)
    harness.install_hook(task, repo, run_dir, work_root, "http://127.0.0.1:9/")
    command = json.loads((repo / ".claude" / "settings.local.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    payload = {"session_id": "s", "cwd": str(repo), "hook_event_name": "PreToolUse", "tool_name": "Write",
               "tool_input": {"file_path": str(repo / "tests" / "integration" / "t.py"),
                              "content": f"KEY = '{task.secrets['STAGING_KEY']}'\n"}}
    env = harness.base_environment(os.environ, run_dir)
    completed = subprocess.run(command, shell=True, input=json.dumps(payload).encode(), capture_output=True,
                               cwd=str(repo), env=env, timeout=60)
    decision = json.loads(completed.stdout.decode().strip().splitlines()[-1])
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "contains a credential" in decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert (run_dir / "threefold-home").is_dir()


def test_the_prompt_condition_commits_the_rules_and_the_others_do_not(tmp_path):
    task = task_library.load_tasks(["orders-s3-archive"])[0]
    harness.prepare_repository(task, "prompt", tmp_path / "prompt")
    harness.prepare_repository(task, "none", tmp_path / "none")
    assert (tmp_path / "prompt" / "CLAUDE.md").read_text(encoding="utf-8") == harness.RULES_FILE.read_text(encoding="utf-8")
    assert not (tmp_path / "none" / "CLAUDE.md").exists()
    assert harness.changed_files(tmp_path / "prompt") == [] and harness.committed_by_agent(tmp_path / "prompt") == 0


def test_the_rules_in_claude_md_are_the_shipped_rules():
    """The prompt condition must tell the agent the same rules Threefold enforces, every forbidden name of them."""
    from threefold.domain.layering_rules import DEFAULT_RULES

    text = harness.RULES_FILE.read_text(encoding="utf-8")
    for rule in DEFAULT_RULES:
        for pattern in rule["forbid_imports"]:
            if "infrastructure" in pattern.lower() or "adapters" in pattern.lower():
                continue
            assert f"`{pattern}`" in text, f"CLAUDE.prompt.md does not name {pattern} from {rule['id']}"


# --- planning ---------------------------------------------------------------------------------

def test_runs_are_interleaved_so_conditions_run_side_by_side():
    tasks = task_library.load_tasks(["orders-s3-archive", "billing-credit-limit"])
    planned = run.plan_runs(tasks, ["none", "prompt", "threefold"], 2)
    assert len(planned) == 12
    assert [(task.id, condition, rep) for task, condition, rep in planned[:3]] == [
        ("orders-s3-archive", "none", 1), ("orders-s3-archive", "prompt", 1), ("orders-s3-archive", "threefold", 1)]
    assert planned[-1][2] == 2


def test_a_pilot_run_id_says_so():
    import datetime

    moment = datetime.datetime(2026, 9, 22, 14, 5, 9, tzinfo=datetime.timezone.utc)
    assert run.default_run_id(True, "claude", moment) == "20260922T140509Z-pilot"
    assert run.default_run_id(False, "scripted", moment) == "20260922T140509Z-scripted"


def test_a_dry_run_prints_the_plan_and_starts_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(harness, "run_one", lambda *args, **kwargs: pytest.fail("a dry run started a run"))
    code = run.main(["--tasks", "orders-s3-archive", "--reps", "1", "--dry-run", "--claude", "claude-not-called",
                     "--work-root", str(tmp_path / "work"), "--results-dir", str(tmp_path / "results"), "--isolation", "user-config"])
    output = capsys.readouterr().out
    assert code == 0
    assert "3 run(s)" in output and "--output-format stream-json" in output and "(prompt on stdin)" in output
    assert not (tmp_path / "work").exists() and not (tmp_path / "results").exists()


def test_unknown_conditions_are_refused():
    with pytest.raises(SystemExit):
        run.parse_args(["--conditions", "none,threefold-lite"])


def test_every_row_is_appended_as_it_finishes(tmp_path):
    results = run.ResultsFile(tmp_path / "r.jsonl")
    results.append({"task": "a", "rep": 1})
    results.append({"task": "b", "rep": 1})
    assert [json.loads(line)["task"] for line in (tmp_path / "r.jsonl").read_text(encoding="utf-8").splitlines()] == ["a", "b"]
