"""The installer puts the hook in front of three agents and takes it out again, exactly.

Every test runs against a repository made by `git init` in a temporary
directory, with HOME, THREEFOLD_HOME and git's global configuration pointed
into that directory too, so nothing here can read or change the machine's own
agent settings or hooks. The properties pinned are the ones a developer would
only find out about the hard way: a settings file that lost its other hooks, a
pre-commit hook that was replaced rather than chained, a second install that
registered the hook twice, and an uninstall that left something behind.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import codecs
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
MATCHERS = {
    ".claude/settings.local.json": "Write|Edit|MultiEdit|NotebookEdit|Bash",
    ".codex/hooks.json": "apply_patch|Edit|Write|Bash",
    ".agents/hooks.json": "write_to_file|replace_file_content|multi_replace_file_content|run_command",
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = _load("threefold_install")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


@pytest.fixture
def machine(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("THREEFOLD_") and name != "THREEFOLD_OFFLINE":
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    global_config = tmp_path / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("THREEFOLD_HOME", str(home / ".threefold"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for variable, value in (("GIT_AUTHOR_NAME", "Acme Dev"), ("GIT_AUTHOR_EMAIL", "dev@acme.example"),
                            ("GIT_COMMITTER_NAME", "Acme Dev"), ("GIT_COMMITTER_EMAIL", "dev@acme.example")):
        monkeypatch.setenv(variable, value)
    # The installed pre-commit check asks the service for the project's rules.
    # Home names a port nothing listens on, so a commit made by a test falls
    # back to the shipped rules instead of reaching the public stack.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = f"http://127.0.0.1:{probe.getsockname()[1]}/prod/"
    (home / ".threefold").mkdir()
    (home / ".threefold" / "config.json").write_text(json.dumps({"endpoint": closed}), encoding="utf-8")
    monkeypatch.setenv("THREEFOLD_TIMEOUT", "0.5")
    repo = tmp_path / "acme-ledger"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    (repo / "README.md").write_text("# Acme Ledger\n", encoding="utf-8")
    return SimpleNamespace(home=home, threefold_home=home / ".threefold", repo=repo, tmp=tmp_path)


def run(machine, *extra: str, project: Optional[str] = "Acme-Ledger") -> SimpleNamespace:
    argv = ["--repo", str(machine.repo)] + (["--project", project] if project else []) + list(extra)
    out = io.StringIO()
    code = installer.main(argv, out)
    return SimpleNamespace(code=code, out=out.getvalue())


def snapshot(*roots: Path) -> Dict[str, bytes]:
    """Every file under the roots, by path, with its bytes. Git's own objects are left out."""
    files = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root.parent).as_posix()
            if "/.git/objects/" in f"/{relative}/" or "/.git/logs/" in f"/{relative}/":
                continue
            files[relative] = path.read_bytes() if path.is_file() else b"<dir>"
    return files


def settings(machine, relative: str) -> dict:
    return json.loads((machine.repo / relative).read_text(encoding="utf-8"))


def commands_in(document: dict) -> List[str]:
    return [item["command"] for entry in document.get("hooks", {}).get("PreToolUse", []) for item in entry["hooks"]]


# --- install --------------------------------------------------------------------------

def test_an_install_writes_the_config_and_registers_the_hook_for_all_three_agents(machine) -> None:
    result = run(machine)
    assert result.code == 0, result.out
    config = json.loads((machine.repo / ".threefold.json").read_text(encoding="utf-8"))
    assert config == {"project": "Acme-Ledger", "mode": "observe"}
    hook_copy = (machine.threefold_home / "bin" / "threefold_hook.py").as_posix()
    for relative, matcher in MATCHERS.items():
        agent = {".claude": "claude-code", ".codex": "codex", ".agents": "antigravity"}[relative.split("/")[0]]
        entries = settings(machine, relative)["hooks"]["PreToolUse"]
        assert entries == [{"matcher": matcher, "hooks": [{"type": "command", "command": installer.hook_command(machine.threefold_home, agent)}]}]
        command = entries[0]["hooks"][0]["command"]
        assert "\\" not in command, "forward slashes, so the same line works in every shell"
        assert hook_copy in command and command.endswith(f"--agent {agent}")
    assert (machine.threefold_home / "bin" / "threefold_hook.py").read_bytes() == installer.HOOK_SOURCE.read_bytes()
    assert (machine.threefold_home / "lib" / "threefold" / "domain" / "layering_rules.py").is_file()


def test_the_endpoint_and_key_file_are_written_but_never_a_key(machine) -> None:
    key_file = machine.tmp / "acme.key"
    key_file.write_text("acme-secret-value", encoding="utf-8")
    result = run(machine, "--mode", "enforce", "--endpoint", "https://acme.example/prod", "--api-key-file", str(key_file))
    assert result.code == 0, result.out
    text = (machine.repo / ".threefold.json").read_text(encoding="utf-8")
    config = json.loads(text)
    assert config["endpoint"] == "https://acme.example/prod/"
    assert config["api_key_file"] == key_file.resolve().as_posix()
    assert config["mode"] == "enforce"
    assert "acme-secret-value" not in text + result.out


def test_codex_is_told_about_trust_and_its_home_config_is_not_touched(machine) -> None:
    result = run(machine)
    assert "trusted in Codex" in result.out
    assert not (machine.home / ".codex").exists()


def test_only_the_agents_asked_for_are_registered(machine) -> None:
    run(machine, "--agents", "codex")
    assert (machine.repo / ".codex" / "hooks.json").is_file()
    assert not (machine.repo / ".claude").exists() and not (machine.repo / ".agents").exists()


def test_a_second_install_changes_nothing(machine) -> None:
    run(machine)
    before = snapshot(machine.repo, machine.threefold_home)
    result = run(machine)
    assert result.code == 0
    assert snapshot(machine.repo, machine.threefold_home) == before
    assert "already runs the hook" in result.out


def test_existing_settings_are_merged_into_and_every_other_hook_is_kept(machine) -> None:
    original = {
        "permissions": {"allow": ["Bash(npm test)"]},
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "acme-lint --pre"}]}],
            "PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "acme-format"}]}],
        },
    }
    path = machine.repo / ".claude" / "settings.local.json"
    path.parent.mkdir()
    path.write_text(json.dumps(original, indent=4), encoding="utf-8")
    run(machine)
    merged = settings(machine, ".claude/settings.local.json")
    assert merged["permissions"] == original["permissions"]
    assert merged["hooks"]["PostToolUse"] == original["hooks"]["PostToolUse"]
    assert merged["hooks"]["PreToolUse"][0] == original["hooks"]["PreToolUse"][0]
    assert commands_in(merged)[1].endswith("--agent claude-code")

    assert run(machine, "--uninstall").code == 0
    assert settings(machine, ".claude/settings.local.json") == original, "uninstall takes out only its own entry"


def test_a_settings_file_that_is_not_valid_json_stops_the_install_before_anything_is_written(machine) -> None:
    path = machine.repo / ".codex" / "hooks.json"
    path.parent.mkdir()
    path.write_text("{ not json", encoding="utf-8")
    before = snapshot(machine.repo, machine.threefold_home)
    result = run(machine)
    assert result.code == 2
    assert "Nothing was changed" in result.out
    assert snapshot(machine.repo, machine.threefold_home) == before


# --- the pre-commit hook -----------------------------------------------------------------

def _hooks(machine) -> Path:
    return machine.repo / ".git" / "hooks"


def test_a_pre_commit_hook_is_installed_that_runs_the_check(machine) -> None:
    run(machine)
    script = (_hooks(machine) / "pre-commit").read_text(encoding="utf-8")
    assert installer.PRE_COMMIT_MARKER in script
    assert "threefold_cli.py" in script and " check " in script


def test_an_existing_pre_commit_hook_is_chained_and_still_runs(machine) -> None:
    original = "#!/bin/sh\necho acme-original-ran > \"$(git rev-parse --show-toplevel)/.acme-marker\"\n"
    hook = _hooks(machine) / "pre-commit"
    hook.write_text(original, encoding="utf-8", newline="\n")
    os.chmod(hook, 0o755)
    run(machine, "--mode", "enforce")
    assert (_hooks(machine) / installer.CHAINED_NAME).read_text(encoding="utf-8") == original
    assert installer.PRE_COMMIT_MARKER in hook.read_text(encoding="utf-8")

    _git(machine.repo, "add", "README.md")
    committed = _git(machine.repo, "commit", "-q", "-m", "docs: acme readme")
    assert committed.returncode == 0, committed.stdout + committed.stderr
    assert (machine.repo / ".acme-marker").is_file(), "the hook that was there first still ran"

    run(machine, "--uninstall")
    assert hook.read_text(encoding="utf-8") == original
    assert not (_hooks(machine) / installer.CHAINED_NAME).exists()


def test_the_installed_hook_refuses_a_commit_that_breaks_an_enforce_rule(machine) -> None:
    run(machine, "--mode", "enforce")
    domain = machine.repo / "src" / "domain" / "acme_user.py"
    domain.parent.mkdir(parents=True)
    domain.write_text("import boto3\n", encoding="utf-8")
    _git(machine.repo, "add", "src/domain/acme_user.py")
    committed = _git(machine.repo, "commit", "-q", "-m", "feat: acme user")
    assert committed.returncode != 0
    assert "python-domain-stays-pure" in committed.stdout + committed.stderr


def test_in_observe_mode_the_installed_hook_reports_and_lets_the_commit_through(machine) -> None:
    run(machine)
    domain = machine.repo / "src" / "domain" / "acme_user.py"
    domain.parent.mkdir(parents=True)
    domain.write_text("import boto3\n", encoding="utf-8")
    _git(machine.repo, "add", "src/domain/acme_user.py")
    committed = _git(machine.repo, "commit", "-q", "-m", "feat: acme user")
    assert committed.returncode == 0, committed.stdout + committed.stderr
    assert "WOULD REFUSE" in committed.stdout + committed.stderr


def test_with_core_hooks_path_set_nothing_is_installed_in_git_hooks_and_the_output_says_so(machine) -> None:
    _git(machine.repo, "config", "core.hooksPath", ".acme-hooks")
    result = run(machine)
    assert result.code == 0
    assert "core.hooksPath is set to .acme-hooks" in result.out
    assert not (_hooks(machine) / "pre-commit").exists()
    assert not (machine.repo / ".acme-hooks").exists()
    assert (machine.repo / ".threefold.json").is_file(), "the rest of the install still happens"


# --- what is kept out of git -------------------------------------------------------------

def test_every_file_written_is_listed_in_info_exclude_and_nothing_shows_as_untracked(machine) -> None:
    run(machine)
    exclude = (machine.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    for line in ["/.threefold.json", "/.claude/settings.local.json", "/.codex/hooks.json", "/.agents/hooks.json"]:
        assert line in exclude
    assert not (machine.repo / ".gitignore").exists()
    status = _git(machine.repo, "status", "--porcelain").stdout.splitlines()
    assert status == ["?? README.md"], status


# --- uninstall and dry run -----------------------------------------------------------------

def test_uninstall_returns_the_repository_to_exactly_what_it_was(machine) -> None:
    before = snapshot(machine.repo)
    run(machine)
    result = run(machine, "--uninstall", project=None)
    assert result.code == 0, result.out
    assert snapshot(machine.repo) == before
    assert "shared copies" in result.out and (machine.threefold_home / "bin" / "threefold_hook.py").is_file()


def test_uninstall_puts_back_a_repository_config_that_was_there_before(machine) -> None:
    previous = '{"project": "Acme-Old", "mode": "enforce"}\n'
    (machine.repo / ".threefold.json").write_text(previous, encoding="utf-8")
    run(machine)
    assert json.loads((machine.repo / ".threefold.json").read_text(encoding="utf-8"))["project"] == "Acme-Ledger"
    run(machine, "--uninstall")
    assert (machine.repo / ".threefold.json").read_text(encoding="utf-8") == previous


def test_a_dry_run_writes_nothing_and_says_what_it_would_do(machine) -> None:
    before = snapshot(machine.repo, machine.home)
    result = run(machine, "--dry-run")
    assert result.code == 0
    assert snapshot(machine.repo, machine.home) == before
    assert "would write .threefold.json" in result.out
    assert "would register the hook for codex" in result.out
    assert "would install a pre-commit hook" in result.out


def test_a_dry_run_of_an_uninstall_writes_nothing_either(machine) -> None:
    run(machine)
    before = snapshot(machine.repo, machine.home)
    result = run(machine, "--uninstall", "--dry-run")
    assert snapshot(machine.repo, machine.home) == before
    assert "would remove the pre-commit hook" in result.out


# --- files git tracks, and files that were there before ------------------------------------------

def _commit(machine, relative: str, text: str) -> None:
    path = machine.repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    assert _git(machine.repo, "add", relative).returncode == 0
    assert _git(machine.repo, "commit", "-q", "-m", f"chore: acme {relative}").returncode == 0


def test_a_settings_file_git_tracks_is_left_alone_and_the_output_says_what_to_do(machine) -> None:
    """A committed .codex/hooks.json is the normal case. Writing the hook into
    it put this machine's Python and home folder into the next commit, and
    .git/info/exclude has no effect on a tracked file."""
    committed = '{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "acme-lint"}]}]}}'
    _commit(machine, ".codex/hooks.json", committed)
    result = run(machine)
    assert result.code == 0, result.out
    assert ".codex/hooks.json is tracked by git" in result.out
    assert (machine.repo / ".codex" / "hooks.json").read_text(encoding="utf-8") == committed
    assert _git(machine.repo, "status", "--porcelain", "--", ".codex").stdout == ""
    exclude = (machine.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert "/.codex/hooks.json" not in exclude
    assert "/.claude/settings.local.json" in exclude, "the untracked files are still installed"

    run(machine, "--uninstall")
    assert (machine.repo / ".codex" / "hooks.json").read_text(encoding="utf-8") == committed


def test_a_repository_config_git_tracks_is_left_alone(machine) -> None:
    committed = '{"project": "Acme-Team", "mode": "enforce"}\n'
    _commit(machine, ".threefold.json", committed)
    result = run(machine)
    assert result.code == 0, result.out
    assert ".threefold.json is tracked by git" in result.out
    assert (machine.repo / ".threefold.json").read_bytes() == committed.encode("utf-8")
    assert _git(machine.repo, "status", "--porcelain", "--", ".threefold.json").stdout == ""


def test_uninstall_puts_a_file_that_was_there_before_back_byte_for_byte(machine) -> None:
    """Parsed JSON coming back equal is not the file coming back: indentation,
    line endings and the missing final newline were all rewritten before."""
    original = b'{\r\n    "permissions": {"allow": ["Bash(npm test)"]},\r\n    "hooks": {}\r\n}'
    settings_path = machine.repo / ".claude" / "settings.local.json"
    settings_path.parent.mkdir()
    settings_path.write_bytes(original)
    exclude = machine.repo / ".git" / "info" / "exclude"
    exclude_before = exclude.read_bytes().rstrip(b"\n") + b"\n# acme: a last line with no newline"
    exclude.write_bytes(exclude_before)
    run(machine)
    assert settings_path.read_bytes() != original
    run(machine, "--uninstall")
    assert settings_path.read_bytes() == original
    assert exclude.read_bytes() == exclude_before


def test_a_file_changed_by_hand_since_the_install_keeps_the_change_and_loses_only_the_hook(machine) -> None:
    path = machine.repo / ".claude" / "settings.local.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"permissions": {"allow": []}}), encoding="utf-8")
    run(machine)
    document = settings(machine, ".claude/settings.local.json")
    document["permissions"]["allow"].append("Bash(acme-build)")
    path.write_text(json.dumps(document), encoding="utf-8")
    result = run(machine, "--uninstall")
    assert "changed since the install" in result.out
    after = settings(machine, ".claude/settings.local.json")
    assert after["permissions"]["allow"] == ["Bash(acme-build)"]
    assert "hooks" not in after


def test_an_endpoint_and_key_file_are_paired_at_home_so_the_hook_sends_the_key_there(machine) -> None:
    key_file = machine.tmp / "acme.key"
    key_file.write_text("acme-operator-key-value", encoding="utf-8")
    run(machine, "--endpoint", "https://acme.example/dogfood", "--api-key-file", str(key_file))
    home_config = json.loads((machine.threefold_home / "config.json").read_text(encoding="utf-8"))
    assert home_config["trusted_endpoints"] == [{"endpoint": "https://acme.example/dogfood/", "api_key_file": key_file.resolve().as_posix()}]

    spec = importlib.util.spec_from_file_location("threefold_hook_for_install", installer.HOOK_SOURCE)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    resolved = hook.resolve_settings({"cwd": str(machine.repo)})
    assert (resolved.endpoint, resolved.api_key) == ("https://acme.example/dogfood/", "acme-operator-key-value")

    run(machine, "--endpoint", "https://acme.example/dogfood", "--api-key-file", str(key_file))
    again = json.loads((machine.threefold_home / "config.json").read_text(encoding="utf-8"))
    assert len(again["trusted_endpoints"]) == 1, "a second install pairs nothing twice"


# --- the project name -------------------------------------------------------------------------

@pytest.mark.parametrize("project", ["Acme Ledger", "Ledger", "acme-ledger", "Acme-", "Acme-" + "x" * 41, "Acme-Ledger/../x", "Acme_Ledger"])
def test_a_project_that_is_not_an_acme_alias_is_refused_and_nothing_is_written(machine, project) -> None:
    before = snapshot(machine.repo, machine.home)
    result = run(machine, project=project)
    assert result.code == 2
    assert "must match" in result.out
    assert snapshot(machine.repo, machine.home) == before


def test_a_path_that_is_not_a_directory_is_refused(machine) -> None:
    missing = machine.tmp / "acme-missing"
    out = io.StringIO()
    code = installer.main(["--repo", str(missing), "--project", "Acme-Ledger"], out)
    assert code == 2
    assert "is not a directory" in out.getvalue() and "Nothing was changed" in out.getvalue()
    assert not missing.exists()


# --- a workspace git does not recognise ------------------------------------------------------
#
# A workspace root holds several repositories and is not one itself. It used
# to be refused, which left no way to govern the repositories inside it from
# one place. It is now installed with the four configuration files only.

WORKSPACE_FILES = [".threefold.json", ".claude/settings.local.json", ".codex/hooks.json", ".agents/hooks.json"]


@pytest.fixture
def workspace(machine, monkeypatch) -> Path:
    """A directory holding repositories, with git stopped from looking above the test's own directory."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(machine.tmp))
    root = machine.tmp / "acme-workspace"
    for name in ("acme-alpha", "acme-beta", "acme-gamma"):
        (root / "repos" / name).mkdir(parents=True)
        (root / "repos" / name / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    (root / "NOTES.md").write_text("# Acme workspace\n", encoding="utf-8")
    return root


def run_in(machine, directory: Path, *extra: str, project: Optional[str] = "Acme-Workspace") -> SimpleNamespace:
    argv = ["--repo", str(directory)] + (["--project", project] if project else []) + list(extra)
    out = io.StringIO()
    code = installer.main(argv, out)
    return SimpleNamespace(code=code, out=out.getvalue())


def record_of(machine, directory: Path) -> Path:
    return installer.workspace_record(machine.threefold_home, directory.resolve())


def test_a_directory_git_does_not_recognise_is_installed_in_workspace_mode(machine, workspace) -> None:
    before = snapshot(workspace)
    result = run_in(machine, workspace)
    assert result.code == 0, result.out
    assert "workspace mode" in result.out and "no commit-time check" in result.out

    added = sorted(set(snapshot(workspace)) - set(before))
    assert added == sorted(["acme-workspace/" + relative for relative in WORKSPACE_FILES]
                           + ["acme-workspace/.claude", "acme-workspace/.codex", "acme-workspace/.agents"])
    assert not (workspace / ".git").exists()
    assert json.loads((workspace / ".threefold.json").read_text(encoding="utf-8")) == {"project": "Acme-Workspace", "mode": "observe"}
    for relative, matcher in MATCHERS.items():
        entries = json.loads((workspace / relative).read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        assert [entry["matcher"] for entry in entries] == [matcher]

    record = record_of(machine, workspace)
    assert record.parent == machine.threefold_home / "installs"
    assert re.fullmatch(r"[0-9a-f]{16}\.json", record.name)
    assert json.loads(record.read_text(encoding="utf-8"))["workspace"] == workspace.resolve().as_posix()


def test_a_second_workspace_install_changes_nothing(machine, workspace) -> None:
    run_in(machine, workspace)
    before = snapshot(workspace, machine.threefold_home)
    result = run_in(machine, workspace)
    assert result.code == 0, result.out
    assert snapshot(workspace, machine.threefold_home) == before
    assert "already runs the hook" in result.out


def test_a_workspace_install_merges_into_existing_claude_settings_and_uninstall_puts_them_back_byte_for_byte(machine, workspace) -> None:
    original = b'{\r\n    "permissions": {"allow": ["Bash(npm test)"]},\r\n    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "acme-lint"}]}]}\r\n}'
    path = workspace / ".claude" / "settings.local.json"
    path.parent.mkdir()
    path.write_bytes(original)
    run_in(machine, workspace)
    merged = json.loads(path.read_text(encoding="utf-8"))
    assert merged["permissions"] == {"allow": ["Bash(npm test)"]}
    assert commands_in(merged)[0] == "acme-lint"
    assert commands_in(merged)[1].endswith("--agent claude-code")

    assert run_in(machine, workspace, "--uninstall", project=None).code == 0
    assert path.read_bytes() == original


def test_a_workspace_uninstall_returns_the_directory_to_exactly_what_it_was(machine, workspace) -> None:
    before = snapshot(workspace)
    run_in(machine, workspace, "--include", "repos/acme-alpha/**")
    result = run_in(machine, workspace, "--uninstall", project=None)
    assert result.code == 0, result.out
    assert "workspace mode" in result.out
    assert snapshot(workspace) == before
    assert not record_of(machine, workspace).exists()
    assert not (machine.threefold_home / "installs").exists(), "the record's folder goes once it is empty"
    assert (machine.threefold_home / "bin" / "threefold_hook.py").is_file(), "the shared copies stay"


def test_a_directory_with_an_empty_folder_named_git_is_a_workspace_and_nothing_is_written_inside_it(machine, workspace) -> None:
    (workspace / ".git").mkdir()
    result = run_in(machine, workspace, "--mode", "enforce")
    assert result.code == 0, result.out
    assert "workspace mode" in result.out
    assert list((workspace / ".git").iterdir()) == []
    assert (workspace / ".threefold.json").is_file() and record_of(machine, workspace).is_file()

    run_in(machine, workspace, "--uninstall", project=None)
    assert list((workspace / ".git").iterdir()) == []
    assert not (workspace / ".threefold.json").exists()


def test_a_folder_git_does_not_accept_inside_a_repository_does_not_send_the_install_to_the_repository_above(machine) -> None:
    """Git passes over a .git it cannot read and answers with the checkout above.
    Installing there would have put a pre-commit hook and an install record in a
    repository nobody named."""
    inner = machine.repo / "acme-vendored"
    (inner / ".git").mkdir(parents=True)
    outer_before = snapshot(machine.repo / ".git")
    result = run_in(machine, inner)
    assert result.code == 0, result.out
    assert "workspace mode" in result.out
    assert (inner / ".threefold.json").is_file()
    assert list((inner / ".git").iterdir()) == []
    assert snapshot(machine.repo / ".git") == outer_before
    assert not (machine.repo / ".threefold.json").exists()


def test_a_workspace_dry_run_writes_nothing_and_prints_the_include_list(machine, workspace) -> None:
    before = snapshot(workspace, machine.home)
    result = run_in(machine, workspace, "--dry-run", "--include", "repos/acme-alpha/**", "--include", "repos/acme-beta/**")
    assert result.code == 0, result.out
    assert snapshot(workspace, machine.home) == before
    assert "workspace mode" in result.out
    assert "would write .threefold.json" in result.out
    assert "repos/acme-alpha/**, repos/acme-beta/**" in result.out
    assert "pre-commit hook" not in result.out.replace("No pre-commit hook is installed", "")


def test_the_hook_installed_in_a_workspace_sends_only_what_the_include_list_names(machine, workspace, monkeypatch) -> None:
    """End to end on the files the installer wrote, with each repository a real
    checkout and a project in the environment: the hook standing inside either
    repository still reads the list from the workspace root."""
    for name in ("acme-alpha", "acme-gamma"):
        assert _git(workspace / "repos" / name, "init", "-q").returncode == 0
    result = run_in(machine, workspace, "--include", "repos/acme-alpha/**")
    assert "workspace mode" in result.out, "a checkout inside the directory does not make the directory one"
    monkeypatch.setenv("THREEFOLD_PROJECT", "Acme-Env")
    spec = importlib.util.spec_from_file_location("threefold_hook_for_workspace", installer.HOOK_SOURCE)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    # The walk from a checkout passes every .git on its way to a .threefold.json; keep it in this test's directory.
    monkeypatch.setattr(hook, "_WALK_CEILING", str(machine.tmp))
    log = machine.threefold_home / "held_back.log"

    def write(name: str) -> None:
        repository = workspace / "repos" / name
        payload = {"session_id": "acme-ws", "cwd": str(repository), "tool_name": "Write",
                   "tool_input": {"file_path": str(repository / "app.py"), "content": "x = 1\n"}}
        hook.handle(json.dumps(payload), "claude-code")

    write("acme-gamma")
    assert log.read_text(encoding="utf-8").splitlines()[-1].endswith(" not-included")
    write("acme-alpha")
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1, "the included write went to the service instead"


# --- the include list ------------------------------------------------------------------------

def test_include_globs_are_written_to_the_config_in_the_order_given(machine) -> None:
    result = run(machine, "--include", "services/billing/**", "--include", ".\\services\\ledger\\**", "--include", "services/billing/**")
    assert result.code == 0, result.out
    config = json.loads((machine.repo / ".threefold.json").read_text(encoding="utf-8"))
    assert config["include"] == ["services/billing/**", "services/ledger/**"]
    assert "sending only calls inside services/billing/**, services/ledger/**" in result.out


@pytest.mark.parametrize(
    "glob",
    ["", "   ", "./", ".", "./.", ".\\", "/srv/acme/**", "\\acme\\**", "C:/acme/**", "c:\\acme\\**", "~/acme/**",
     "../acme-other/**", "repos/../../acme/**", "repos/acme-alpha/.."],
)
def test_an_include_glob_that_is_empty_absolute_or_climbs_out_is_refused_and_nothing_is_written(machine, workspace, glob) -> None:
    """`.` among them: it was written, matched nothing, and held back every call."""
    for directory in (machine.repo, workspace):
        before = snapshot(directory, machine.home)
        result = run_in(machine, directory, "--include", "repos/acme-alpha/**", "--include", glob)
        assert result.code == 2, result.out
        assert "--include" in result.out and "Nothing was changed" in result.out
        assert snapshot(directory, machine.home) == before


def test_a_dot_segment_inside_an_include_glob_is_written_without_it(machine) -> None:
    result = run(machine, "--include", "services/./billing/**", "--include", "services//ledger/")
    assert result.code == 0, result.out
    config = json.loads((machine.repo / ".threefold.json").read_text(encoding="utf-8"))
    assert config["include"] == ["services/billing/**", "services/ledger"]


@pytest.mark.parametrize("dry_run", [False, True], ids=["install", "dry-run"])
def test_include_is_refused_for_a_directory_below_the_top_of_a_repository(machine, dry_run) -> None:
    """The file goes to the top of the checkout, where globs written for the
    directory named would be read relative to another one, silently."""
    inner = machine.repo / "acme-ws"
    (inner / "repos" / "acme-alpha").mkdir(parents=True)
    before = snapshot(machine.repo, machine.home)
    result = run_in(machine, inner, "--include", "repos/acme-alpha/**", *(["--dry-run"] if dry_run else []))
    assert result.code == 2, result.out
    assert "relative to the directory given" in result.out and "Nothing was changed" in result.out
    assert f"--repo {machine.repo.resolve().as_posix()}" in result.out
    assert snapshot(machine.repo, machine.home) == before


def test_a_directory_below_the_top_of_a_repository_still_installs_at_the_top_without_include(machine) -> None:
    inner = machine.repo / "acme-sub"
    inner.mkdir()
    assert run_in(machine, inner).code == 0
    assert (machine.repo / ".threefold.json").is_file() and not (inner / ".threefold.json").exists()


def test_with_a_tracked_config_the_output_says_the_include_list_was_not_written(machine) -> None:
    _commit(machine, ".threefold.json", json.dumps({"project": "Acme-Ledger", "mode": "observe"}))
    committed = (machine.repo / ".threefold.json").read_bytes()
    result = run(machine, "--include", "services/billing/**")
    assert result.code == 0, result.out
    assert "include list was not written" in result.out and '"services/billing/**"' in result.out
    assert (machine.repo / ".threefold.json").read_bytes() == committed


def test_an_existing_config_windows_powershell_wrote_in_utf_16_is_replaced_and_put_back_byte_for_byte(machine) -> None:
    """It used to stop the install with a UnicodeDecodeError traceback."""
    original = json.dumps({"project": "Acme-Old"}).encode("utf-16")
    (machine.repo / ".threefold.json").write_bytes(original)
    result = run(machine, "--include", "services/billing/**")
    assert result.code == 0, result.out
    assert json.loads((machine.repo / ".threefold.json").read_text(encoding="utf-8"))["include"] == ["services/billing/**"]
    assert run(machine, "--uninstall", project=None).code == 0
    assert (machine.repo / ".threefold.json").read_bytes() == original


# --- what the installer copies from the hook ---------------------------------------------------
#
# The stack serves this file alone at /install.py, so it cannot import the
# hook and keeps its own copy of two of its readings. These are what keep the
# copies honest: if they ever disagree, connect would keep a configuration the
# hook reads differently, or refuse a name the hook would send.

HOOK_SOURCE = Path(__file__).resolve().parents[2] / "src" / "threefold" / "hooks" / "threefold_hook.py"


def _hook():
    spec = importlib.util.spec_from_file_location("threefold_hook_for_install", HOOK_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SAMPLE = '{"project": "Acme-Ledger"}'
ENCODED = [
    SAMPLE.encode("utf-8"),
    codecs.BOM_UTF8 + SAMPLE.encode("utf-8"),
    SAMPLE.encode("utf-16"),
    codecs.BOM_UTF16_BE + SAMPLE.encode("utf-16-be"),
    SAMPLE.encode("utf-32"),
    codecs.BOM_UTF32_BE + SAMPLE.encode("utf-32-be"),
    SAMPLE.encode("utf-16-le"),
    b"",
    b"\xe9\xff",
]


@pytest.mark.parametrize("raw", ENCODED)
def test_the_installer_decodes_a_file_exactly_as_the_hook_does(raw) -> None:
    hook = _hook()
    try:
        theirs: object = hook._decode_config(raw)
    except UnicodeDecodeError as error:
        theirs = type(error)
    try:
        ours: object = installer.decode_text(raw)
    except UnicodeDecodeError as error:
        ours = type(error)
    assert ours == theirs, raw


TERMS_AND_TEXTS = [
    ("globex", "Acme-Globex-Portal"), ("globex", "Acme-Payments"), ("xyz", "isspace"), ("xyz", "XYZ"),
    ("xyz", "acme_xyz"), ("xyz", "xyzbilling"), ("xyz", "classpath-xyz"), ("orion", "Acme-ORION-1"),
    ("acme", "Acme-Ledger"), ("zeta", "projekt-zeta"), ("ab", "Acme-Ab-One"),
]


@pytest.mark.parametrize(("term", "text"), TERMS_AND_TEXTS)
def test_the_installer_reads_a_never_send_term_exactly_as_the_hook_does(term, text) -> None:
    hook = _hook()
    assert installer.term_occurs(text, term) == hook.term_occurs(text, term), (term, text)
