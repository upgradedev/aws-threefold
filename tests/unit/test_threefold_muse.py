"""The installer writes Muse's plugin bundle and registers it for the project.

Muse reads no settings file: `.threefold-muse/` holds the hook beside its
manifest, and `muse plugins install --scope project`, `approve` and `list`
do the registration the settings entries do for the other agents. What each
command answered when measured is in
docs/evidence/ENFORCEMENT_2026-09-28-MUSE.md; the fake below answers the same.
No test here runs a real muse: the fixture leaves no agent command on PATH.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import pytest

from threefold.tools import threefold_install as installer


ROOT = Path(installer.__file__).resolve().parents[3]
BUNDLE = ROOT / "src" / "threefold" / "tools" / "threefold_muse_plugin"


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
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for variable, value in (("GIT_AUTHOR_NAME", "Acme Dev"), ("GIT_AUTHOR_EMAIL", "dev@acme.example"),
                            ("GIT_COMMITTER_NAME", "Acme Dev"), ("GIT_COMMITTER_EMAIL", "dev@acme.example")):
        monkeypatch.setenv(variable, value)
    repo = tmp_path / "acme-ledger"
    repo.mkdir()
    assert subprocess.run(["git", "-C", str(repo), "init", "-q"], capture_output=True).returncode == 0
    (repo / "README.md").write_text("# Acme Ledger\n", encoding="utf-8")
    # No agent command is reachable except through the test: with muse on this
    # machine's PATH the installer would run the real one.
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(empty), os.path.dirname(shutil.which("git"))]))
    return SimpleNamespace(home=home, threefold_home=home / ".threefold", repo=repo, tmp=tmp_path)


def run(*argv: str):
    out = io.StringIO()
    return SimpleNamespace(code=installer.main(list(argv), out), out=out.getvalue())


def fake_muse(bin_dir: Path, log: Path, listing: Optional[str] = None, fail: str = "") -> None:
    """A muse command that records its argv and answers like the measured one.

    `listing` is what `plugins list` prints; `fail` names a subcommand that
    exits 1 instead, to show what the installer does with a refusal.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shown = "threefold\t1.0.0\tenabled=true" if listing is None else listing.strip()
    if os.name == "nt":
        # Only cmd's own verbs: PATH here holds nothing but this folder and
        # git's, so even findstr is out of reach.
        (bin_dir / "muse.cmd").write_text(
            "@echo off\n"
            f'echo %*>> "{log}"\n'
            'if "%1 %2"=="plugins list" echo ' + shown + "\n"
            + (f'if "%1 %2"=="{fail}" exit /b 1\n' if fail else "")
            + "exit /b 0\n",
            encoding="utf-8",
        )
    else:
        (bin_dir / "muse").write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{log}"\n'
            'case " $* " in\n'
            f'  *" plugins list "*) printf %s "{shown}";;\n'
            "esac\n"
            + (f'case " $* " in *" {fail} "*) exit 1;; esac\n' if fail else ""),
            encoding="utf-8",
        )
        (bin_dir / "muse").chmod(0o755)


def on_path(machine, monkeypatch, bin_dir: Path) -> None:
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(bin_dir), os.path.dirname(shutil.which("git"))])
    )
    assert installer.muse_binary() is not None


def calls_of(log: Path) -> List[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []


def plugin_files(machine) -> List[Path]:
    return [(machine.repo / ".threefold-muse").joinpath(*name.split("/")) for name in installer.MUSE_PLUGIN_FILES]


# --- the bundle --------------------------------------------------------------------------------

def test_the_bundle_manifest_pins_what_the_bundle_holds() -> None:
    manifest = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["written_by"] == "threefold_install.py"
    pinned = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    assert set(pinned) == {".muse-plugin/plugin.json", "hooks/threefold_hook.py"}
    for path, expected in pinned.items():
        data = BUNDLE.joinpath(*path.split("/")).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected, path


def test_the_bundle_hook_is_the_hook() -> None:
    assert (BUNDLE / "hooks" / "threefold_hook.py").read_bytes() == installer.HOOK_SOURCE.read_bytes()


def test_the_installer_s_manifest_is_the_bundle_s_byte_for_byte() -> None:
    assert installer.MUSE_PLUGIN_JSON.encode("utf-8") == (BUNDLE / ".muse-plugin" / "plugin.json").read_bytes()


def test_the_plugin_names_the_govern_hook() -> None:
    plugin = json.loads((BUNDLE / ".muse-plugin" / "plugin.json").read_text(encoding="utf-8"))
    (hook,) = plugin["capabilities"]["hooks"]
    assert (hook["id"], hook["event"]) == ("govern", "PreToolUse")
    assert hook["command"] == ["python", "hooks/threefold_hook.py", "--agent", "muse"]
    assert hook["timeoutMs"] == 10000
    assert plugin["name"] == installer.MUSE_PLUGIN_ID == "threefold"


def test_a_tampered_bundle_stops_the_install(tmp_path, monkeypatch) -> None:
    bundle = tmp_path / "threefold_muse_plugin"
    shutil.copytree(BUNDLE, bundle)
    with open(bundle / "hooks" / "threefold_hook.py", "ab") as handle:
        handle.write(b"# tampered\n")
    monkeypatch.setattr(installer, "MUSE_BUNDLE_SOURCE", bundle)
    with pytest.raises(installer.InstallError, match="does not match its manifest"):
        installer.muse_plugin_files(tmp_path / "home", None)


def test_a_bundle_without_its_hook_stops_the_install(tmp_path, monkeypatch) -> None:
    bundle = tmp_path / "threefold_muse_plugin"
    shutil.copytree(BUNDLE, bundle)
    (bundle / "hooks" / "threefold_hook.py").unlink()
    monkeypatch.setattr(installer, "MUSE_BUNDLE_SOURCE", bundle)
    with pytest.raises(installer.InstallError, match="has no hooks/threefold_hook.py"):
        installer.muse_plugin_files(tmp_path / "home", None)


def test_without_a_bundle_the_plugin_is_rendered_from_the_staged_hook(tmp_path, monkeypatch) -> None:
    """A served copy of the installer has no bundle beside it; it renders one."""
    monkeypatch.setattr(installer, "MUSE_BUNDLE_SOURCE", tmp_path / "no-bundle-here")
    files = dict(installer.muse_plugin_files(tmp_path / "home", b"# the staged hook\n"))
    assert files[".muse-plugin/plugin.json"] == installer.MUSE_PLUGIN_JSON.encode("utf-8")
    assert files["hooks/threefold_hook.py"] == b"# the staged hook\n"
    manifest = json.loads(files["manifest.json"].decode("utf-8"))
    assert manifest["written_by"] == "threefold_install.py"
    pinned = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    assert pinned["hooks/threefold_hook.py"] == hashlib.sha256(b"# the staged hook\n").hexdigest()


# --- install -----------------------------------------------------------------------------------

def test_connect_with_muse_registers_the_plugin(machine, monkeypatch) -> None:
    log = machine.tmp / "muse-calls.log"
    fake_muse(machine.tmp / "muse-bin", log)
    on_path(machine, monkeypatch, machine.tmp / "muse-bin")
    result = run("--repo", str(machine.repo), "--project", "Acme-Ledger", "--agents", "muse")
    assert result.code == 0, result.out
    for path in plugin_files(machine):
        assert path.is_file()
    calls = " ".join(calls_of(log))
    assert "plugins install" in calls and "--scope project" in calls
    assert "plugins approve threefold" in calls
    assert "plugins list" in calls
    assert "note:" not in result.out


def test_connect_without_muse_writes_the_files_and_leaves_the_commands(machine) -> None:
    result = run("--repo", str(machine.repo), "--project", "Acme-Ledger", "--agents", "muse")
    assert result.code == 0, result.out
    for path in plugin_files(machine):
        assert path.is_file()
    assert "muse is not on PATH" in result.out
    assert "muse plugins install .threefold-muse --scope project" in result.out
    assert "muse plugins approve threefold" in result.out


def test_a_failed_registration_leaves_a_note_not_a_failure(machine, monkeypatch) -> None:
    log = machine.tmp / "muse-calls.log"
    fake_muse(machine.tmp / "muse-bin", log, fail="plugins install")
    on_path(machine, monkeypatch, machine.tmp / "muse-bin")
    result = run("--repo", str(machine.repo), "--project", "Acme-Ledger", "--agents", "muse")
    assert result.code == 0, result.out
    for path in plugin_files(machine):
        assert path.is_file()
    assert "`muse plugins install` did not run cleanly" in result.out


def test_an_unlisted_plugin_is_reported_not_assumed(machine, monkeypatch) -> None:
    log = machine.tmp / "muse-calls.log"
    fake_muse(machine.tmp / "muse-bin", log, listing="no plugins\n")
    on_path(machine, monkeypatch, machine.tmp / "muse-bin")
    result = run("--repo", str(machine.repo), "--project", "Acme-Ledger", "--agents", "muse")
    assert result.code == 0, result.out
    assert "`muse plugins list` does not show threefold" in result.out


def test_a_tracked_plugin_file_is_never_written(machine) -> None:
    held = machine.repo / ".threefold-muse" / "manifest.json"
    held.parent.mkdir(parents=True)
    held.write_text('{"team": true}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(machine.repo), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(machine.repo), "commit", "-qm", "the team's plugin"], capture_output=True, check=True)
    result = run("--repo", str(machine.repo), "--project", "Acme-Ledger", "--agents", "muse")
    assert result.code == 0, result.out
    assert held.read_text(encoding="utf-8") == '{"team": true}\n'
    assert "is tracked by git" in result.out
    assert "register the Muse plugin" not in result.out


def test_a_second_install_for_fewer_agents_keeps_muse(machine, monkeypatch) -> None:
    log = machine.tmp / "muse-calls.log"
    fake_muse(machine.tmp / "muse-bin", log)
    on_path(machine, monkeypatch, machine.tmp / "muse-bin")
    assert run("--repo", str(machine.repo), "--project", "Acme-Ledger").code == 0
    result = run("--repo", str(machine.repo), "--project", "Acme-Ledger", "--agents", "claude-code")
    assert result.code == 0, result.out
    for path in plugin_files(machine):
        assert path.is_file()
    index = json.loads((machine.threefold_home / "installs" / "index.json").read_text(encoding="utf-8"))
    assert "muse" in index["installs"][0]["agents"]


def test_a_dry_run_prints_the_muse_commands_and_writes_nothing(machine) -> None:
    before = {path for path in machine.repo.rglob("*")}
    result = run("--repo", str(machine.repo), "--project", "Acme-Ledger", "--agents", "muse", "--dry-run")
    assert result.code == 0, result.out
    assert {path for path in machine.repo.rglob("*")} == before
    assert "muse plugins install" in result.out and "--scope project" in result.out
    assert "muse plugins approve threefold" in result.out


# --- status and disconnect ---------------------------------------------------------------------

def test_status_shows_a_registered_plugin(machine, monkeypatch) -> None:
    log = machine.tmp / "muse-calls.log"
    fake_muse(machine.tmp / "muse-bin", log)
    on_path(machine, monkeypatch, machine.tmp / "muse-bin")
    assert run("--repo", str(machine.repo), "--project", "Acme-Ledger", "--agents", "muse").code == 0
    result = run("status")
    assert result.code == 0, result.out
    assert "muse    plugin files present, registered" in result.out


def test_status_says_when_the_files_or_the_command_are_gone(machine, monkeypatch) -> None:
    result = run("--repo", str(machine.repo), "--project", "Acme-Ledger", "--agents", "muse")
    assert result.code == 0, result.out
    assert "muse is not on PATH so registration is unknown" in run("status").out
    shutil.rmtree(machine.repo / ".threefold-muse")
    assert "its .threefold-muse/ is gone" in run("status").out


def test_disconnect_unregisters_and_removes_the_plugin(machine, monkeypatch) -> None:
    log = machine.tmp / "muse-calls.log"
    fake_muse(machine.tmp / "muse-bin", log)
    on_path(machine, monkeypatch, machine.tmp / "muse-bin")
    assert run("--repo", str(machine.repo), "--project", "Acme-Ledger", "--agents", "muse").code == 0
    result = run("--repo", str(machine.repo), "--uninstall")
    assert result.code == 0, result.out
    assert not (machine.repo / ".threefold-muse").exists()
    assert "plugins remove threefold" in " ".join(calls_of(log))
    assert "Threefold is not connected to anything on this machine yet." in run("status").out


def test_disconnect_without_muse_leaves_the_registration_with_a_note(machine) -> None:
    assert run("--repo", str(machine.repo), "--project", "Acme-Ledger", "--agents", "muse").code == 0
    result = run("--repo", str(machine.repo), "--uninstall")
    assert result.code == 0, result.out
    assert not (machine.repo / ".threefold-muse").exists()
    assert "muse is not on PATH" in result.out
    assert "muse plugins remove threefold" in result.out


def test_disconnect_without_a_record_removes_only_a_marked_plugin(machine, monkeypatch) -> None:
    log = machine.tmp / "muse-calls.log"
    fake_muse(machine.tmp / "muse-bin", log)
    on_path(machine, monkeypatch, machine.tmp / "muse-bin")
    assert run("--repo", str(machine.repo), "--project", "Acme-Ledger", "--agents", "muse").code == 0
    records = list((machine.threefold_home / "installs").glob("*.json"))
    assert len(records) == 1
    records[0].unlink()
    keep = machine.repo / ".threefold-muse" / "notes.txt"
    keep.write_text("the owner's\n", encoding="utf-8")
    result = run("--repo", str(machine.repo), "--uninstall")
    assert result.code == 0, result.out
    assert not (machine.repo / ".threefold-muse" / "manifest.json").exists()
    assert keep.read_text(encoding="utf-8") == "the owner's\n"
    assert "plugins remove threefold" in " ".join(calls_of(log))


def test_disconnect_without_a_record_leaves_an_unmarked_plugin_alone(machine, monkeypatch) -> None:
    log = machine.tmp / "muse-calls.log"
    fake_muse(machine.tmp / "muse-bin", log)
    on_path(machine, monkeypatch, machine.tmp / "muse-bin")
    ours = machine.repo / ".threefold-muse"
    (ours / "hooks").mkdir(parents=True)
    (ours / "manifest.json").write_text("not json\n", encoding="utf-8")
    (ours / "hooks" / "threefold_hook.py").write_text("# someone else's\n", encoding="utf-8")
    result = run("--repo", str(machine.repo), "--uninstall")
    assert result.code == 0, result.out
    assert (ours / "manifest.json").read_text(encoding="utf-8") == "not json\n"
    assert (ours / "hooks" / "threefold_hook.py").read_text(encoding="utf-8") == "# someone else's\n"
    assert calls_of(log) == []


def test_run_muse_reports_a_missing_binary(monkeypatch) -> None:
    monkeypatch.setattr(installer, "muse_binary", lambda: None)
    assert installer.run_muse(Path("."), "plugins", "list") == (False, "")


def test_parse_agents_accepts_muse() -> None:
    assert installer.parse_agents("muse") == ["muse"]
    assert installer.parse_agents("codex,muse") == ["codex", "muse"]
    with pytest.raises(installer.InstallError, match="comma-separated list of claude-code, codex, antigravity, muse"):
        installer.parse_agents("muse,acme-bot")
