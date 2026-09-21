"""A path means nothing without the directory it is read from.

The service judges `echo 'import boto3' > acme_user.py` by where that file
lands. Codex runs a command in its own `workdir` and Antigravity in its own
`Cwd`, and Claude Code stands wherever the last `cd` left it. Before this, the
hook dropped all three: the service read every relative path from the project
root, so a write into src/domain made from inside src/domain reached it as a
write to `acme_user.py`, which no rule on `**/domain/**` covers. Every test here
reads what arrived at the stub service, and the last ones run the same
arguments through the service's own gate.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from threefold.domain.boundary_guard import UNREADABLE_WRITE, ArchitecturalBoundaryGuard, write_pairs
from threefold.domain.models import ToolActionType, ToolInvocation


def _sent(stub) -> Dict[str, Any]:
    assert len(stub.requests) == 1, stub.requests
    return stub.requests[0]["body"]


def _judged(arguments: Dict[str, Any]):
    return ArchitecturalBoundaryGuard.evaluate_tool_boundary(ToolInvocation("Bash", ToolActionType.COMMAND_EXEC, arguments))


def _codex(machine, command: str, workdir: str) -> Dict[str, Any]:
    return {
        "session_id": "acme-codex-1",
        "cwd": str(machine.project),
        "hook_event_name": "PreToolUse",
        "tool_name": "shell",
        "tool_input": {"command": ["bash", "-lc", command], "workdir": workdir},
    }


def test_a_codex_workdir_below_the_root_is_sent_as_where_the_command_runs(machine, stub, run_hook) -> None:
    (machine.project / "src" / "domain").mkdir(parents=True)
    run_hook(_codex(machine, "echo 'import boto3' > acme_user.py", str(machine.project / "src" / "domain")), ["--agent", "codex"])
    arguments = _sent(stub)["arguments"]
    assert arguments["cwd"] == "src/domain"
    allowed, reason = _judged(arguments)
    # Refused for the import it writes, which is only readable when bash's
    # script arrives whole: with the words joined by spaces the redirect was
    # read as bash's own, and refused only as output nobody could read.
    assert not allowed and "imports 'boto3'" in reason


@pytest.mark.parametrize(
    "script, reason",
    [
        ("cp /tmp/acme.py src/domain/x.py", UNREADABLE_WRITE),
        ("git commit --no-verify -m x", "--no-verify"),
        ("python -c \"open('src/domain/x.py','w').write('import boto3')\"", "python-domain-stays-pure"),
    ],
)
def test_a_codex_command_given_as_words_reaches_the_service_as_the_script_bash_runs(script, reason, machine, stub, run_hook) -> None:
    """Joined with spaces, `bash -lc cp /tmp/acme.py src/domain/x.py` gives bash
    the script `cp` and nothing the service could read a write in. Only the
    service's verdict on what actually arrived shows the difference."""
    run_hook(_codex(machine, script, str(machine.project)), ["--agent", "codex"])
    arguments = _sent(stub)["arguments"]
    allowed, why = _judged(arguments)
    assert not allowed
    assert reason in why


def test_an_antigravity_cwd_below_the_root_is_sent_the_same_way(machine, payloads, stub, run_hook) -> None:
    (machine.project / "src" / "domain").mkdir(parents=True)
    payload = payloads.antigravity("run_command", {"CommandLine": "cp /tmp/acme.py acme_user.py", "Cwd": str(machine.project / "src" / "domain")})
    run_hook(payload, ["--agent", "antigravity"])
    arguments = _sent(stub)["arguments"]
    assert arguments["cwd"] == "src/domain"
    assert _judged(arguments)[0] is False


def test_a_command_run_at_the_root_carries_no_cwd_at_all(payloads, stub, run_hook) -> None:
    run_hook(payloads.command("antigravity", "npm test"), ["--agent", "antigravity"])
    assert _sent(stub)["arguments"] == {"command": "npm test"}


def test_an_absolute_path_in_a_command_run_below_the_root_still_names_the_same_file(machine, stub, run_hook) -> None:
    """The root is shortened to the path from where the command runs, not to `.`,
    or the service would read the file as src/domain/src/domain/acme_user.py."""
    domain = machine.project / "src" / "domain"
    domain.mkdir(parents=True)
    target = (domain / "acme_user.py").as_posix()
    run_hook(_codex(machine, f"echo 'import boto3' > {target}", str(domain)), ["--agent", "codex"])
    arguments = _sent(stub)["arguments"]
    assert machine.project.as_posix() not in json.dumps(arguments)
    allowed, reason = _judged(arguments)
    assert not allowed and "'src/domain/acme_user.py'" in reason


def test_a_command_run_outside_the_project_is_held_back(machine, stub, run_hook, held_back_lines) -> None:
    elsewhere = machine.tmp / "acme-other-checkout"
    elsewhere.mkdir()
    code, out, _ = run_hook(_codex(machine, "echo x > notes.txt", str(elsewhere)), ["--agent", "codex"])
    assert (code, out) == (0, "")
    assert stub.requests == []
    assert held_back_lines()[-1].endswith(" outside-root")


# --- an installed repository measures from its own root ------------------------------------

def test_with_a_repository_config_a_write_from_deep_inside_keeps_its_full_path(machine, stub, run_hook, monkeypatch) -> None:
    monkeypatch.delenv("THREEFOLD_PROJECT")
    (machine.project / ".threefold.json").write_text(json.dumps({"project": "Acme-Ledger"}), encoding="utf-8")
    domain = machine.project / "src" / "domain"
    domain.mkdir(parents=True)
    payload = {
        "session_id": "acme-session-1",
        "cwd": str(domain),
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(domain / "acme_user.py"), "content": "import boto3\n"},
    }
    run_hook(payload)
    assert _sent(stub)["arguments"]["file_path"] == "src/domain/acme_user.py"


def test_with_a_repository_config_a_claude_code_command_from_deep_inside_says_where_it_runs(machine, stub, run_hook, monkeypatch) -> None:
    monkeypatch.delenv("THREEFOLD_PROJECT")
    (machine.project / ".threefold.json").write_text(json.dumps({"project": "Acme-Ledger"}), encoding="utf-8")
    domain = machine.project / "src" / "domain"
    domain.mkdir(parents=True)
    payload = {
        "session_id": "acme-session-1",
        "cwd": str(domain),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo 'import boto3' > acme_user.py"},
    }
    run_hook(payload)
    arguments = _sent(stub)["arguments"]
    assert arguments["cwd"] == "src/domain"
    assert _judged(arguments)[0] is False


def test_a_relative_patch_path_is_read_from_where_the_agent_stands(machine, stub, run_hook, monkeypatch) -> None:
    monkeypatch.delenv("THREEFOLD_PROJECT")
    (machine.project / ".threefold.json").write_text(json.dumps({"project": "Acme-Ledger"}), encoding="utf-8")
    services = machine.project / "services"
    services.mkdir()
    patch = "*** Begin Patch\n*** Add File: billing/app.py\n+x = 1\n*** End Patch\n"
    payload = {"session_id": "s", "cwd": str(services), "tool_name": "apply_patch", "tool_input": {"command": patch}}
    run_hook(payload, ["--agent", "codex"])
    assert _sent(stub)["arguments"]["file_path"] == "services/billing/app.py"


# --- the service reads the working directory and nothing more ---------------------------------

def test_the_service_reads_a_relative_path_from_the_directory_it_is_told() -> None:
    allowed, reason = _judged({"command": "echo 'import boto3' > acme_user.py", "cwd": "src/domain"})
    assert not allowed and "'src/domain/acme_user.py'" in reason
    assert _judged({"command": "echo 'import boto3' > acme_user.py"})[0] is True


def test_a_working_directory_outside_the_root_is_ignored_rather_than_trusted() -> None:
    for cwd in ("../acme-other", "/srv/acme", "C:/acme", "~/acme"):
        assert _judged({"command": "echo 'import boto3' > src/domain/acme_user.py", "cwd": cwd})[0] is False, cwd


def test_a_working_directory_is_never_judged_as_a_file_being_written() -> None:
    """A lone path-like leaf is paired with the call's text as its content by the
    Write fallback. The cwd is where a command runs, not a file it writes."""
    arguments = {"command": "ls -la", "cwd": "src/domain"}
    assert write_pairs(arguments) == []
    watching_everything = [{
        "id": "acme-domain-everything", "mode": "enforce", "description": "",
        "when_path_matches": ["**/domain/**"], "forbid_imports": ["ls"], "allow_imports": [],
    }]
    allowed, reason = ArchitecturalBoundaryGuard.evaluate_tool_boundary(
        ToolInvocation("Bash", ToolActionType.COMMAND_EXEC, arguments), rules=watching_everything
    )
    assert allowed, reason


def test_a_command_with_a_working_directory_is_still_scanned_for_destructive_work() -> None:
    """The cwd is a path-like leaf, and step 4 used to run only for calls with
    none, unless the call declared COMMAND_EXEC."""
    invocation = ToolInvocation("Bash", ToolActionType.UNKNOWN, {"command": "rm -rf /", "cwd": "src/billing"})
    assert ArchitecturalBoundaryGuard.evaluate_tool_boundary(invocation)[0] is False
