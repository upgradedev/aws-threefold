"""Where the hook takes its project, endpoint, mode and key from.

Day one read everything from the environment, which meant one project per
shell and an API key in every process the developer started. Day two adds two
files: `.threefold.json` in the repository, written by the installer, and
`config.json` in THREEFOLD_HOME. The environment still wins, the repository
comes next, home last. Every test here reads what reached the stub service,
because a setting that is resolved but not sent, or sent from the wrong layer,
looks correct from stdout alone.

The key tests look at what could leave the machine in a header. A repository's
own `.threefold.json` can name an endpoint and a key file, so a cloned
repository must not be able to aim the owner's key, or an arbitrary file on
disk, at a server of its choosing.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from threefold.domain.shell_writes import is_governance_path as service_governance_path

AGENTS = ("claude-code", "codex", "antigravity")


def _write_json(path: Path, document: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _sent(stub) -> Dict[str, Any]:
    assert len(stub.requests) == 1, stub.requests
    return stub.requests[0]


@pytest.fixture
def unconfigured(monkeypatch):
    """No project in the environment, so the files are what decide."""
    monkeypatch.delenv("THREEFOLD_PROJECT", raising=False)


# --- the order: environment, repository, home ------------------------------------

def test_a_project_named_only_in_the_repository_file_is_governed(machine, payloads, stub, run_hook, unconfigured) -> None:
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger"})
    code, out, err = run_hook(payloads.write("claude-code"))
    assert (code, out, err) == (0, "", "")
    assert _sent(stub)["body"]["project_name"] == "Acme-Ledger"


def test_a_project_named_only_at_home_is_governed(machine, payloads, stub, run_hook, unconfigured) -> None:
    _write_json(machine.threefold_home / "config.json", {"project": "Acme-Home"})
    run_hook(payloads.write("claude-code"))
    assert _sent(stub)["body"]["project_name"] == "Acme-Home"


def test_the_repository_file_wins_over_home(machine, payloads, stub, run_hook, unconfigured) -> None:
    _write_json(machine.threefold_home / "config.json", {"project": "Acme-Home", "mode": "enforce"})
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "mode": "observe"})
    run_hook(payloads.write("claude-code"))
    body = _sent(stub)["body"]
    assert (body["project_name"], body["dry_run"]) == ("Acme-Ledger", True)


def test_the_environment_wins_over_both_files(machine, payloads, stub, run_hook, monkeypatch) -> None:
    monkeypatch.setenv("THREEFOLD_PROJECT", "Acme-Env")
    monkeypatch.setenv("THREEFOLD_MODE", "enforce")
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "mode": "observe"})
    _write_json(machine.threefold_home / "config.json", {"project": "Acme-Home", "mode": "observe"})
    run_hook(payloads.write("claude-code"))
    body = _sent(stub)["body"]
    assert (body["project_name"], body["dry_run"]) == ("Acme-Env", False)


def test_the_endpoint_can_come_from_the_repository_file(machine, payloads, stub, run_hook, monkeypatch, unconfigured) -> None:
    monkeypatch.delenv("THREEFOLD_ENDPOINT")
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "endpoint": stub.endpoint.rstrip("/")})
    run_hook(payloads.write("claude-code"))
    assert _sent(stub)["path"] == "/prod/evaluate-tool-call", "a trailing slash is added so the path joins on"


def test_an_endpoint_that_is_not_a_url_falls_through_and_says_so(machine, payloads, stub, run_hook, monkeypatch, unconfigured) -> None:
    monkeypatch.delenv("THREEFOLD_ENDPOINT")
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "endpoint": "file:///etc/passwd"})
    _write_json(machine.threefold_home / "config.json", {"endpoint": stub.endpoint})
    code, out, err = run_hook(payloads.write("claude-code"))
    assert (code, out) == (0, "")
    assert len(stub.requests) == 1
    assert "not an http(s) URL" in err


# --- finding the repository file ---------------------------------------------------

def test_the_file_is_found_by_walking_up_from_a_subdirectory(machine, payloads, stub, run_hook, unconfigured) -> None:
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger"})
    (machine.project / ".git").mkdir()
    nested = machine.project / "services" / "billing"
    nested.mkdir(parents=True)
    payload = payloads.write("claude-code", "services/billing/app.py")
    payload["cwd"] = str(nested)
    run_hook(payload)
    assert _sent(stub)["body"]["project_name"] == "Acme-Ledger"


def test_a_nested_repository_is_not_governed_by_its_parents_file(machine, payloads, stub, run_hook, held_back_lines, unconfigured) -> None:
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger"})
    vendored = machine.project / "vendor" / "acme-lib"
    (vendored / ".git").mkdir(parents=True)
    payload = payloads.write("claude-code", "vendor/acme-lib/lib.py")
    payload["cwd"] = str(vendored)
    code, _, err = run_hook(payload)
    assert code == 0
    assert stub.requests == []
    assert "not governed" in err
    assert held_back_lines()[-1].endswith(" no-project")


def test_antigravity_is_configured_from_its_first_workspace(machine, payloads, stub, run_hook, unconfigured) -> None:
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger"})
    run_hook(payloads.write("antigravity"), ["--agent", "antigravity"])
    assert _sent(stub)["body"]["project_name"] == "Acme-Ledger"


def test_a_file_that_is_not_a_json_object_configures_nothing(machine, payloads, stub, run_hook, unconfigured) -> None:
    (machine.project / ".threefold.json").write_text('["Acme-Ledger"]', encoding="utf-8")
    _write_json(machine.threefold_home / "config.json", {"project": "Acme-Home"})
    run_hook(payloads.write("claude-code"))
    assert _sent(stub)["body"]["project_name"] == "Acme-Home"


def test_config_root_stops_at_the_nearest_git_directory(hook, machine) -> None:
    (machine.project / ".git").mkdir()
    deeper = machine.project / "a" / "b"
    deeper.mkdir(parents=True)
    assert hook._canonical(hook.config_root({"cwd": str(deeper)})) == hook._canonical(str(machine.project))


# --- the mode ----------------------------------------------------------------------

@pytest.mark.parametrize("agent", AGENTS)
def test_observe_mode_sends_every_call_as_a_dry_run(agent, machine, payloads, stub, run_hook, unconfigured) -> None:
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "mode": "observe"})
    run_hook(payloads.command(agent, "npm test"), ["--agent", agent])
    assert _sent(stub)["body"]["dry_run"] is True


def test_enforce_mode_does_not(machine, payloads, stub, run_hook, unconfigured) -> None:
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "mode": "enforce"})
    run_hook(payloads.write("claude-code"))
    assert _sent(stub)["body"]["dry_run"] is False


def test_a_mode_nobody_meant_is_ignored_and_the_next_layer_decides(machine, payloads, stub, run_hook, unconfigured) -> None:
    """A typo such as "observ" must not decide whether a call can be refused."""
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "mode": "observ"})
    _write_json(machine.threefold_home / "config.json", {"mode": "observe"})
    _, _, err = run_hook(payloads.write("claude-code"))
    assert _sent(stub)["body"]["dry_run"] is True
    assert "must be enforce or observe" in err


def test_the_environment_mode_overrides_a_file(machine, payloads, stub, run_hook, monkeypatch, unconfigured) -> None:
    monkeypatch.setenv("THREEFOLD_MODE", "observe")
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "mode": "enforce"})
    run_hook(payloads.write("claude-code"))
    assert _sent(stub)["body"]["dry_run"] is True


def test_with_no_mode_anywhere_the_hook_enforces(machine, payloads, stub, run_hook) -> None:
    run_hook(payloads.write("claude-code"))
    assert _sent(stub)["body"]["dry_run"] is False


# --- the key -----------------------------------------------------------------------

def test_the_key_file_is_read_at_call_time_and_stripped(machine, payloads, stub, run_hook, unconfigured) -> None:
    key_file = machine.threefold_home / "acme.key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text("acme-operator-key-1\n", encoding="utf-8")
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "api_key_file": str(key_file)})
    run_hook(payloads.write("claude-code"))
    key_file.write_text("  acme-operator-key-2  \n", encoding="utf-8")
    code, out, err = run_hook(payloads.write("claude-code"))
    keys = [request["headers"].get("x-api-key") for request in stub.requests]
    assert keys == ["acme-operator-key-1", "acme-operator-key-2"]
    assert "acme-operator-key" not in out + err, "the key is never printed"


def test_a_relative_key_file_is_read_from_beside_the_file_that_names_it(machine, payloads, stub, run_hook, unconfigured) -> None:
    (machine.project / "keys").mkdir()
    (machine.project / "keys" / "threefold.key").write_text("acme-relative-key", encoding="utf-8")
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "api_key_file": "keys/threefold.key"})
    run_hook(payloads.write("claude-code"))
    assert _sent(stub)["headers"].get("x-api-key") == "acme-relative-key"


def test_the_environment_key_file_is_read_too(machine, payloads, stub, run_hook, monkeypatch) -> None:
    key_file = machine.tmp / "env.key"
    key_file.write_text("acme-env-file-key", encoding="utf-8")
    monkeypatch.setenv("THREEFOLD_API_KEY_FILE", str(key_file))
    run_hook(payloads.write("claude-code"))
    assert _sent(stub)["headers"].get("x-api-key") == "acme-env-file-key"


@pytest.mark.parametrize(
    "content",
    [
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n",
        "[default]\naws_access_key_id = AKIAABCDEFGHIJKLMNOP\n",
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_" + "a" * 36,
        "a" * 2000,
        "",
    ],
    ids=["private-key", "credentials-file", "aws-key", "github-token", "too-long", "empty"],
)
def test_a_file_that_is_not_a_threefold_key_is_never_sent(content, machine, payloads, stub, run_hook, unconfigured) -> None:
    secret = machine.tmp / "not-a-key"
    secret.write_text(content, encoding="utf-8")
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "api_key_file": str(secret)})
    code, out, err = run_hook(payloads.write("claude-code"))
    assert (code, out) == (0, "")
    assert "x-api-key" not in _sent(stub)["headers"]
    assert "does not hold a Threefold key" in err
    for line in content.splitlines():
        if line.strip():
            assert line.strip() not in err, "nothing of the file's content is printed"


def test_a_key_written_into_a_config_file_is_refused_not_used(machine, payloads, stub, run_hook, unconfigured) -> None:
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "api_key": "acme-inline-key"})
    _, _, err = run_hook(payloads.write("claude-code"))
    assert "x-api-key" not in _sent(stub)["headers"]
    assert "only api_key_file is read" in err
    assert "acme-inline-key" not in err


def test_a_missing_key_file_sends_no_key_and_still_asks(machine, payloads, stub, run_hook, unconfigured) -> None:
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "api_key_file": "absent.key"})
    _, _, err = run_hook(payloads.write("claude-code"))
    assert "x-api-key" not in _sent(stub)["headers"]
    assert "could not be read" in err


def test_the_owners_key_is_not_sent_to_an_endpoint_a_repository_names(machine, payloads, stub, run_hook, monkeypatch, unconfigured) -> None:
    """A cloned repository with its own .threefold.json could otherwise send
    every call, and the owner's key in its header, to a server it chose."""
    monkeypatch.delenv("THREEFOLD_ENDPOINT")
    monkeypatch.setenv("THREEFOLD_API_KEY", "acme-owner-key")
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "endpoint": stub.endpoint})
    _, _, err = run_hook(payloads.write("claude-code"))
    assert "x-api-key" not in _sent(stub)["headers"]
    assert "was not sent" in err and "acme-owner-key" not in err


def test_the_owners_key_is_sent_when_the_repository_names_the_owners_own_endpoint(machine, payloads, stub, run_hook, monkeypatch, unconfigured) -> None:
    monkeypatch.delenv("THREEFOLD_ENDPOINT")
    key_file = machine.threefold_home / "owner.key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text("acme-owner-key", encoding="utf-8")
    _write_json(machine.threefold_home / "config.json", {"endpoint": stub.endpoint, "api_key_file": "owner.key"})
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "endpoint": stub.endpoint})
    run_hook(payloads.write("claude-code"))
    assert _sent(stub)["headers"].get("x-api-key") == "acme-owner-key"


def test_settings_never_show_the_key_in_their_repr(hook, machine, monkeypatch) -> None:
    monkeypatch.setenv("THREEFOLD_API_KEY", "acme-secret-in-repr")
    settings = hook.resolve_settings({"cwd": str(machine.project)})
    assert settings.api_key == "acme-secret-in-repr"
    assert "acme-secret-in-repr" not in repr(settings)


# --- the hooks' own files ------------------------------------------------------------

@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("relative", [".claude/settings.json", ".claude/settings.local.json", ".codex/hooks.json", ".agents/hooks.json", ".threefold.json", ".git/hooks/pre-commit"])
def test_in_enforce_mode_a_write_to_the_hooks_own_files_is_refused_without_a_request(agent, relative, payloads, stub, run_hook, verdict) -> None:
    code, out, _ = run_hook(payloads.write(agent, relative, "{}"), ["--agent", agent])
    assert code == 0
    assert verdict.decision(out) == "deny"
    assert relative in verdict.reason(out)
    assert stub.requests == [], "turning governance off is refused without the network"


def test_a_patch_that_deletes_the_repository_config_is_refused(payloads, stub, run_hook, verdict) -> None:
    patch = "*** Begin Patch\n*** Delete File: .threefold.json\n*** End Patch\n"
    _, out, _ = run_hook(payloads.codex_patch(patch), ["--agent", "codex"])
    assert verdict.decision(out) == "deny"
    assert stub.requests == []


@pytest.mark.parametrize("relative", [".claude/settings.local.json", ".git/hooks/pre-commit"])
def test_in_observe_mode_the_same_write_is_sent_as_a_dry_run(relative, machine, payloads, stub, run_hook, unconfigured) -> None:
    """Observe mode stops nothing, and the hook script under .git is sent rather
    than held back as data, so the rollout sees the attempt."""
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "mode": "observe"})
    code, out, _ = run_hook(payloads.write("claude-code", relative, "{}"))
    assert (code, out) == (0, "")
    body = _sent(stub)["body"]
    assert body["dry_run"] is True
    assert body["arguments"]["file_path"] == relative


def test_an_ordinary_settings_file_elsewhere_in_the_project_is_not_protected(payloads, stub, run_hook) -> None:
    code, out, _ = run_hook(payloads.write("claude-code", "src/settings.json", "{}"))
    assert (code, out) == (0, "")
    assert len(stub.requests) == 1


GOVERNANCE_SAMPLES = [
    ".claude/settings.json", ".claude/settings.local.json", ".codex/hooks.json", ".codex/config.toml",
    ".agents/hooks.json", ".threefold.json", ".git/hooks/pre-commit", ".git/config", ".threefold/rules.json",
    "sub/.claude/settings.json", ".CLAUDE/SETTINGS.JSON", ".claude", ".git", ".codex", "src/app.py",
    ".claude/agents/x.md", ".gitignore", "docs/hooks.json", ".github/workflows/ci.yml", "", ".",
]


@pytest.mark.parametrize("deletes", [False, True])
def test_the_hook_and_the_service_agree_on_which_files_govern_the_hooks(hook, deletes) -> None:
    """The hook is downloaded alone, so it carries its own copy of the predicate.
    A path one of them protects and the other does not is a way around one."""
    for path in GOVERNANCE_SAMPLES:
        assert hook.is_governance_path(path, deletes) == service_governance_path(path, deletes), path
