"""Managed mode: the project's stage on the service decides, and the hook remembers it.

A project starts in Observe and is promoted to Enforce from the dashboard, so
the machine must not decide on its own what a call's verdict can be. Managed
mode sends every call as not a dry run, and the service's stage for the project
decides. Two things still happen on the machine: every request says which mode
sent it, and the stage each response names is kept, because the one refusal
the hook makes without the network, a write to the files that decide whether
the hooks run, must follow the stage too. In Observe nothing but a credential
is refused, so that refusal applies only while the stage last seen is enforce.

Every test reads what reached the stub service and what is on disk under the
temporary THREEFOLD_HOME. Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict

import pytest

AGENTS = ("claude-code", "codex", "antigravity")
GOVERNANCE_FILES = [".claude/settings.local.json", ".codex/hooks.json", ".agents/hooks.json", ".threefold.json"]


def _write_json(path: Path, document: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _only(stub) -> Dict[str, Any]:
    assert len(stub.requests) == 1, stub.requests
    return stub.requests[0]["body"]


def _stage_file(machine, project: str = "Acme-Payments") -> Path:
    digest = hashlib.sha256(project.encode("utf-8")).hexdigest()[:16]
    return machine.threefold_home / "stage" / f"{digest}.json"


def _cache(machine, stage: Any, project: str = "Acme-Payments") -> None:
    _write_json(_stage_file(machine, project), {"stage": stage, "at": "2026-09-22T08:00:00Z"})


@pytest.fixture
def managed(monkeypatch):
    monkeypatch.setenv("THREEFOLD_MODE", "managed")


# --- what managed mode sends ------------------------------------------------------------

@pytest.mark.parametrize("agent", AGENTS)
def test_managed_mode_sends_every_call_as_not_a_dry_run_and_says_it_is_managed(agent, payloads, stub, run_hook, managed) -> None:
    code, out, err = run_hook(payloads.write(agent), ["--agent", agent])
    assert (code, out, err) == (0, "", "")
    body = _only(stub)
    assert body["dry_run"] is False, "the project's stage on the service decides, not the machine"
    assert body["hook_mode"] == "managed"


@pytest.mark.parametrize("layer", ["repository", "home"])
def test_managed_is_read_from_either_file_like_the_other_modes(layer, machine, payloads, stub, run_hook, monkeypatch) -> None:
    monkeypatch.delenv("THREEFOLD_PROJECT")
    if layer == "repository":
        _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "mode": "managed"})
    else:
        _write_json(machine.threefold_home / "config.json", {"project": "Acme-Ledger", "mode": "managed"})
    _, _, err = run_hook(payloads.command("claude-code", "npm test"))
    assert err == ""
    body = _only(stub)
    assert (body["project_name"], body["dry_run"], body["hook_mode"]) == ("Acme-Ledger", False, "managed")


@pytest.mark.parametrize(("mode", "dry_run"), [("observe", True), ("enforce", False)])
def test_the_other_modes_say_which_they_are(mode, dry_run, payloads, stub, run_hook, monkeypatch) -> None:
    monkeypatch.setenv("THREEFOLD_MODE", mode)
    run_hook(payloads.write("claude-code"))
    body = _only(stub)
    assert (body["dry_run"], body["hook_mode"]) == (dry_run, mode)


def test_the_environment_can_hold_a_managed_repository_to_observe(machine, payloads, stub, run_hook, monkeypatch) -> None:
    """Observe stays a hard cap on the machine: set there, no stage can make a call refusable."""
    _write_json(machine.project / ".threefold.json", {"project": "Acme-Ledger", "mode": "managed"})
    monkeypatch.setenv("THREEFOLD_MODE", "observe")
    run_hook(payloads.write("claude-code"))
    assert (_only(stub)["dry_run"], _only(stub)["hook_mode"]) == (True, "observe")


def test_managed_mode_never_prints_an_approval(payloads, stub, run_hook, managed) -> None:
    stub.answer(200, {"status": "APPROVED", "project_stage": "enforce"})
    assert run_hook(payloads.write("claude-code")) == (0, "", "")


def test_managed_mode_prints_the_refusal_the_stage_produced(payloads, stub, run_hook, managed, verdict) -> None:
    stub.answer(200, {"status": "BLOCKED_BOUNDARY_VIOLATION", "reason": "acme domain stays pure", "project_stage": "enforce"})
    _, out, _ = run_hook(payloads.write("claude-code", "src/domain/acme_user.py", "import boto3\n"))
    assert verdict.decision(out) == "deny"
    assert "acme domain stays pure" in verdict.reason(out)


def test_managed_mode_fails_open_when_the_service_cannot_be_reached(payloads, closed_endpoint, run_hook, managed) -> None:
    code, out, err = run_hook(payloads.write("claude-code"))
    assert (code, out) == (0, "")
    assert "could not check this call" in err


def test_managed_mode_still_refuses_a_credential_before_anything_is_sent(machine, payloads, stub, run_hook, managed, verdict) -> None:
    # Observe refuses nothing but a credential, and a credential it still refuses.
    _cache(machine, "observe")
    # Assembled at runtime so this file does not itself carry the pattern.
    key = "AKIA" + "Q7R2T9MZXK4WPLDC"
    _, out, _ = run_hook(payloads.write("claude-code", "src/config.py", f"KEY = '{key}'\n"))
    assert verdict.decision(out) == "deny"
    assert "credential" in verdict.reason(out)
    assert stub.requests == []


# --- the stage each response names is kept ------------------------------------------------

@pytest.mark.parametrize("stage", ["observe", "enforce"])
def test_the_stage_a_response_names_is_kept_under_a_hashed_name(stage, machine, payloads, stub, run_hook, managed) -> None:
    stub.answer(200, {"status": "APPROVED", "project_stage": stage})
    run_hook(payloads.write("claude-code"))
    path = _stage_file(machine)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["stage"] == stage
    assert document["at"].endswith("Z")
    assert "Acme" not in path.name, "the file name says nothing about the project"
    assert [p.name for p in path.parent.iterdir()] == [path.name], "no temporary file is left behind"


def test_a_refusal_carries_the_stage_too(machine, payloads, stub, run_hook, managed) -> None:
    stub.answer(200, {"status": "BLOCKED_LOOP_DETECTED", "reason": "repeat", "project_stage": "enforce"})
    run_hook(payloads.command("claude-code", "npm run build"))
    assert json.loads(_stage_file(machine).read_text(encoding="utf-8"))["stage"] == "enforce"


def test_the_stage_is_kept_in_every_mode(machine, payloads, stub, run_hook, monkeypatch) -> None:
    """A machine held to observe still learns the stage, so switching it to managed later starts right."""
    monkeypatch.setenv("THREEFOLD_MODE", "observe")
    stub.answer(200, {"status": "OBSERVED", "project_stage": "enforce"})
    run_hook(payloads.write("claude-code"))
    assert json.loads(_stage_file(machine).read_text(encoding="utf-8"))["stage"] == "enforce"


def test_each_project_keeps_its_own_stage(hook, machine, payloads, stub, run_hook, managed, monkeypatch) -> None:
    stub.answer(200, {"status": "APPROVED", "project_stage": "enforce"})
    run_hook(payloads.write("claude-code"))
    monkeypatch.setenv("THREEFOLD_PROJECT", "Acme-Billing")
    stub.answer(200, {"status": "APPROVED", "project_stage": "observe"})
    run_hook(payloads.write("claude-code"))
    home = str(machine.threefold_home)
    assert hook.cached_stage(home, "Acme-Payments") == "enforce"
    assert hook.cached_stage(home, "Acme-Billing") == "observe"


@pytest.mark.parametrize("answer", [
    {"status": "APPROVED"},
    {"status": "APPROVED", "project_stage": "promoted"},
    {"status": "APPROVED", "project_stage": ["enforce"]},
    {"status": "APPROVED", "project_stage": None},
])
def test_a_response_without_a_known_stage_leaves_the_last_one_alone(answer, machine, payloads, stub, run_hook, managed) -> None:
    _cache(machine, "enforce")
    before = _stage_file(machine).read_bytes()
    stub.answer(200, answer)
    run_hook(payloads.write("claude-code"))
    assert _stage_file(machine).read_bytes() == before


def test_a_stage_that_cannot_be_written_does_not_stop_the_agent(machine, payloads, stub, run_hook, managed) -> None:
    machine.threefold_home.mkdir(parents=True, exist_ok=True)
    (machine.threefold_home / "stage").write_text("a file where the folder should be", encoding="utf-8")
    stub.answer(200, {"status": "APPROVED", "project_stage": "enforce"})
    assert run_hook(payloads.write("claude-code")) == (0, "", "")


@pytest.mark.parametrize("content", [b"", b"{not json", b'"enforce"', b'{"stage": "ENFORCE"}', b'{"stage": 1}', b"\xff\xfe", b"{" * 5000])
def test_a_cached_stage_that_cannot_be_read_is_no_stage(hook, machine, content) -> None:
    path = _stage_file(machine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    assert hook.cached_stage(str(machine.threefold_home), "Acme-Payments") is None


# --- the hooks' own files follow the stage -------------------------------------------------

@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("relative", GOVERNANCE_FILES)
def test_in_managed_mode_with_no_stage_seen_a_write_to_the_hooks_own_files_is_sent_as_its_path(agent, relative, payloads, stub, run_hook, managed) -> None:
    """A newly connected project is in Observe, so nothing but a credential may be
    refused yet; the service still sees the attempt, without what it says."""
    content = '{"env": {"ACME_NOTE": "acme-private-remark"}}'
    code, out, _ = run_hook(payloads.write(agent, relative, content), ["--agent", agent])
    assert (code, out) == (0, "")
    body = _only(stub)
    assert body["arguments"]["file_path"] == relative
    assert body["arguments"]["content"] == ""
    assert (body["dry_run"], body["hook_mode"]) == (False, "managed")
    assert "acme-private-remark" not in json.dumps(stub.requests)


@pytest.mark.parametrize("relative", GOVERNANCE_FILES)
def test_in_managed_mode_while_the_project_observes_the_same_write_is_sent_as_its_path(relative, machine, payloads, stub, run_hook, managed) -> None:
    _cache(machine, "observe")
    run_hook(payloads.write("claude-code", relative, '{"acme": "acme-private-remark"}'))
    body = _only(stub)
    assert body["arguments"]["content"] == ""
    assert "acme-private-remark" not in json.dumps(stub.requests)


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("relative", GOVERNANCE_FILES + [".git/hooks/pre-commit"])
def test_in_managed_mode_while_the_project_enforces_the_write_is_refused_without_a_request(agent, relative, machine, payloads, stub, run_hook, managed, verdict) -> None:
    _cache(machine, "enforce")
    code, out, _ = run_hook(payloads.write(agent, relative, "{}"), ["--agent", agent])
    assert code == 0
    assert verdict.decision(out) == "deny"
    assert relative in verdict.reason(out)
    assert stub.requests == [], "turning governance off is refused without the network"


@pytest.mark.parametrize("relative", [".git/hooks/pre-commit", ".git/config"])
def test_in_managed_mode_while_the_project_observes_nothing_under_git_is_sent(relative, machine, payloads, stub, run_hook, managed, held_back_lines) -> None:
    """.git is a data directory, and .git/config can carry a token in a remote URL."""
    _cache(machine, "observe")
    content = '[remote "origin"]\n\turl = https://acme-bot:acme-token-value@example.invalid/acme.git\n'
    code, out, err = run_hook(payloads.write("claude-code", relative, content))
    assert (code, out) == (0, "")
    assert stub.requests == []
    assert held_back_lines()[-1].endswith(" data-file")
    assert "acme-token-value" not in err


def test_a_promotion_the_service_reports_is_followed_on_the_next_call_and_so_is_a_demotion(machine, payloads, stub, run_hook, managed, verdict) -> None:
    settings_write = payloads.write("claude-code", ".claude/settings.local.json", "{}")

    stub.answer(200, {"status": "APPROVED", "project_stage": "enforce"})
    run_hook(payloads.write("claude-code"))
    stub.reset()
    _, out, _ = run_hook(settings_write)
    assert verdict.decision(out) == "deny" and stub.requests == []

    stub.answer(200, {"status": "APPROVED", "project_stage": "observe"})
    run_hook(payloads.write("claude-code"))
    stub.reset()
    _, out, _ = run_hook(settings_write)
    assert out == ""
    assert len(stub.requests) == 1


def test_enforce_mode_refuses_the_write_whatever_stage_was_seen(machine, payloads, stub, run_hook, verdict, monkeypatch) -> None:
    monkeypatch.setenv("THREEFOLD_MODE", "enforce")
    _cache(machine, "observe")
    _, out, _ = run_hook(payloads.write("claude-code", ".threefold.json", "{}"))
    assert verdict.decision(out) == "deny"
    assert stub.requests == []


def test_observe_mode_sends_the_write_whatever_stage_was_seen(machine, payloads, stub, run_hook, monkeypatch) -> None:
    monkeypatch.setenv("THREEFOLD_MODE", "observe")
    _cache(machine, "enforce")
    _, out, _ = run_hook(payloads.write("claude-code", ".threefold.json", '{"acme": "acme-private-remark"}'))
    assert out == ""
    assert (_only(stub)["dry_run"], _only(stub)["arguments"]["content"]) == (True, "")


def test_a_stage_seen_for_another_project_does_not_decide_for_this_one(machine, payloads, stub, run_hook, managed) -> None:
    _cache(machine, "enforce", project="Acme-Billing")
    _, out, _ = run_hook(payloads.write("claude-code", ".threefold.json", "{}"))
    assert out == ""
    assert len(stub.requests) == 1
