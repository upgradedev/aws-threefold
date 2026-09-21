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

import importlib.util
import io
import json
import os
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


# --- the project name -------------------------------------------------------------------------

@pytest.mark.parametrize("project", ["Acme Ledger", "Ledger", "acme-ledger", "Acme-", "Acme-" + "x" * 41, "Acme-Ledger/../x", "Acme_Ledger"])
def test_a_project_that_is_not_an_acme_alias_is_refused_and_nothing_is_written(machine, project) -> None:
    before = snapshot(machine.repo, machine.home)
    result = run(machine, project=project)
    assert result.code == 2
    assert "must match" in result.out
    assert snapshot(machine.repo, machine.home) == before


def test_a_directory_that_is_not_a_repository_is_refused(machine) -> None:
    elsewhere = machine.tmp / "not-a-repo"
    elsewhere.mkdir()
    out = io.StringIO()
    code = installer.main(["--repo", str(elsewhere), "--project", "Acme-Ledger"], out)
    assert code == 2
    assert "not inside a git repository" in out.getvalue()
