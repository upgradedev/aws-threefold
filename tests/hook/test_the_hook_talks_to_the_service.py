"""The conversation between the hook and the deployed service.

Carries what tests/integration/test_the_hook_governs_a_real_agent.py proved
about the Claude Code hook, rewritten for the one hook that replaced it: a
refusal reaches the agent with its reason and its attributed explanation, an
unreachable service does not stop the agent, and fail-closed turns that around.
What changed is that an approval, or a failure to reach the service, now prints
nothing where the old hook printed `allow`.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

AGENTS = ("claude-code", "codex", "antigravity")
HOOK_PATH = Path(__file__).resolve().parents[2] / "src" / "threefold" / "hooks" / "threefold_hook.py"


# --- the request ------------------------------------------------------------------

def test_the_default_endpoint_is_the_live_stage_with_its_trailing_slash(hook) -> None:
    """API Gateway answers the bare /prod with its own Not Found, so the slash is part of the URL."""
    assert hook.DEFAULT_ENDPOINT == "https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/"


def test_the_endpoint_without_a_configured_value_is_the_default(hook) -> None:
    assert hook.endpoint() == hook.DEFAULT_ENDPOINT


@pytest.mark.parametrize("suffix", ["/", ""])
def test_the_call_is_posted_to_evaluate_tool_call_under_the_endpoint(suffix, stub, payloads, run_hook, monkeypatch) -> None:
    monkeypatch.setenv("THREEFOLD_ENDPOINT", stub.endpoint.rstrip("/") + suffix)
    run_hook(payloads.write("claude-code"))
    assert stub.requests[0]["path"] == "/prod/evaluate-tool-call"


def test_the_request_is_json(stub, payloads, run_hook) -> None:
    run_hook(payloads.write("claude-code"))
    assert stub.requests[0]["headers"]["content-type"] == "application/json"


def test_the_api_key_is_sent_only_when_one_is_configured(stub, payloads, run_hook, monkeypatch) -> None:
    run_hook(payloads.write("claude-code"))
    assert "x-api-key" not in stub.requests[0]["headers"]
    monkeypatch.setenv("THREEFOLD_API_KEY", "acme-operator-key")
    run_hook(payloads.write("claude-code"))
    assert stub.requests[1]["headers"]["x-api-key"] == "acme-operator-key"


def test_the_timeout_is_four_seconds_unless_configured(hook, monkeypatch) -> None:
    assert hook.timeout_seconds() == 4.0
    monkeypatch.setenv("THREEFOLD_TIMEOUT", "1.5")
    assert hook.timeout_seconds() == 1.5
    monkeypatch.setenv("THREEFOLD_TIMEOUT", "soon")
    assert hook.timeout_seconds() == 4.0


def test_a_slow_service_is_abandoned_at_the_configured_timeout(stub, payloads, run_hook, monkeypatch) -> None:
    stub.delay = 2.0
    monkeypatch.setenv("THREEFOLD_TIMEOUT", "0.2")
    started = time.monotonic()
    code, out, err = run_hook(payloads.write("claude-code"))
    assert time.monotonic() - started < 1.5
    assert (code, out) == (0, "")
    assert "timed out" in err


# --- what the answer becomes --------------------------------------------------------

def test_an_approval_prints_nothing_at_all(stub, payloads, run_hook) -> None:
    assert run_hook(payloads.write("claude-code")) == (0, "", "")


def test_a_refusal_carries_the_reason_and_attributes_bedrock(stub, payloads, run_hook, verdict) -> None:
    stub.answer(
        200,
        {
            "status": "BLOCKED_BOUNDARY_VIOLATION",
            "reason": "Clean Architecture violation: domain file cannot depend on an outer layer",
            "bedrock_explanation": "Importing the AWS SDK into the domain couples your core to infrastructure.",
            "explanation_source": "bedrock",
        },
    )
    _, out, _ = run_hook(payloads.write("claude-code", "src/domain/u.py", "import boto3\n"))
    reason = verdict.reason(out)
    assert verdict.decision(out) == "deny"
    assert "Clean Architecture violation" in reason
    assert "BLOCKED_BOUNDARY_VIOLATION" in reason
    assert "Bedrock: Importing the AWS SDK" in reason, "The explanation must be attributed, as it is in the API"


def test_a_deterministic_explanation_is_not_labelled_as_bedrock(stub, payloads, run_hook, verdict) -> None:
    stub.answer(
        200,
        {
            "status": "BLOCKED_LOOP_DETECTED",
            "reason": "Monomorphic loop detected",
            "bedrock_explanation": "The same call was made three times.",
            "explanation_source": "deterministic_fallback",
        },
    )
    _, out, _ = run_hook(payloads.command("claude-code", "pytest"))
    reason = verdict.reason(out)
    assert "Bedrock:" not in reason
    assert "Deterministic explanation: The same call was made three times." in reason


def test_a_status_the_hook_does_not_know_is_not_a_refusal(stub, payloads, run_hook) -> None:
    stub.answer(200, {"status": "OBSERVED", "reason": "a rule in observe mode would have refused this"})
    assert run_hook(payloads.write("claude-code")) == (0, "", "")


def test_a_client_error_is_refused_naming_the_status_and_the_problem_title(stub, payloads, run_hook, verdict) -> None:
    stub.answer(
        400,
        {"type": "about:blank", "title": "Invalid Parameter", "status": 400, "detail": "Budget must be greater than $0.00"},
        "application/problem+json",
    )
    _, out, _ = run_hook(payloads.write("claude-code"))
    reason = verdict.reason(out)
    assert verdict.decision(out) == "deny"
    assert "HTTP 400" in reason and "Invalid Parameter" in reason
    assert "Budget must be greater" in reason


def test_a_client_error_without_a_problem_document_still_names_its_status(stub, payloads, run_hook, verdict) -> None:
    stub.answer(413, b"<html>too big</html>", "text/html")
    _, out, _ = run_hook(payloads.write("claude-code"))
    assert verdict.decision(out) == "deny"
    assert "HTTP 413" in verdict.reason(out)


def test_a_refused_key_points_at_the_key(stub, payloads, run_hook, verdict) -> None:
    stub.answer(401, {"title": "Unauthorized", "status": 401, "detail": "API key required"})
    _, out, _ = run_hook(payloads.write("claude-code"))
    assert "THREEFOLD_API_KEY" in verdict.reason(out)


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_a_busy_or_broken_service_prints_nothing_and_one_line_on_stderr(status, stub, payloads, run_hook) -> None:
    stub.answer(status, {"title": "whatever", "status": status})
    code, out, err = run_hook(payloads.write("claude-code"))
    assert (code, out) == (0, "")
    assert len(err.strip().splitlines()) == 1
    assert f"HTTP {status}" in err


def test_an_unreadable_approval_is_treated_as_no_answer(stub, payloads, run_hook) -> None:
    """A captive portal answers 200 with a login page. That is not an approval."""
    stub.answer(200, b"<html>sign in to the wifi</html>", "text/html")
    code, out, err = run_hook(payloads.write("claude-code"))
    assert (code, out) == (0, "")
    assert err.strip()


def test_an_unreachable_service_does_not_stop_the_agent_and_says_so(closed_endpoint, payloads, run_hook) -> None:
    code, out, err = run_hook(payloads.write("claude-code"))
    assert (code, out) == (0, "")
    assert len(err.strip().splitlines()) == 1
    assert "could not check" in err


# --- fail-closed -------------------------------------------------------------------

@pytest.mark.parametrize("failure", ["http-429", "http-500", "timeout", "refused", "unreadable"])
def test_fail_closed_refuses_what_the_service_could_not_judge(failure, request, payloads, run_hook, verdict, monkeypatch) -> None:
    monkeypatch.setenv("THREEFOLD_FAIL_CLOSED", "1")
    if failure == "refused":
        request.getfixturevalue("closed_endpoint")
    else:
        stub = request.getfixturevalue("stub")
        if failure == "timeout":
            stub.delay = 1.0
            monkeypatch.setenv("THREEFOLD_TIMEOUT", "0.2")
        elif failure == "unreadable":
            stub.answer(200, b"not json", "text/plain")
        else:
            stub.answer(int(failure.split("-")[1]), {"title": "x"})
    code, out, _ = run_hook(payloads.write("claude-code"))
    assert code == 0
    assert verdict.decision(out) == "deny"
    assert "THREEFOLD_FAIL_CLOSED" in verdict.reason(out)


@pytest.mark.parametrize("agent", AGENTS)
def test_a_fail_closed_refusal_is_in_the_agents_own_format(agent, stub, payloads, run_hook, verdict, monkeypatch) -> None:
    monkeypatch.setenv("THREEFOLD_FAIL_CLOSED", "1")
    stub.answer(503, {"title": "Service Unavailable"})
    _, out, _ = run_hook(payloads.write(agent), ["--agent", agent])
    assert verdict.decision(out) == "deny"
    assert ("decision" in json.loads(out)) is (agent == "antigravity")


def test_fail_closed_does_not_turn_an_approval_into_anything(stub, payloads, run_hook, monkeypatch) -> None:
    monkeypatch.setenv("THREEFOLD_FAIL_CLOSED", "1")
    assert run_hook(payloads.write("claude-code")) == (0, "", "")


def test_fail_closed_does_not_refuse_a_call_the_hook_holds_back(stub, payloads, run_hook, monkeypatch) -> None:
    """Holding back is a decision, not a failure: it is not the service being unreachable."""
    monkeypatch.setenv("THREEFOLD_FAIL_CLOSED", "1")
    assert run_hook(payloads.write("claude-code", "data/x.py")) == (0, "", "")


def _unreadable_shell(project, tool, keys):
    """A governed shell call written the way the hook does not know how to read."""
    return {"session_id": "acme-codex-1", "cwd": str(project), "hook_event_name": "PreToolUse",
            "tool_name": tool, "tool_input": dict(keys)}


@pytest.mark.parametrize(
    "agent, tool, payload_keys",
    [
        ("codex", "shell", {"argv": ["bash", "-lc", "rm -rf src"]}),
        ("claude-code", "Bash", {"cmd": "rm -rf src"}),
    ],
)
def test_fail_closed_refuses_a_call_whose_shape_the_hook_cannot_read(
    agent, tool, payload_keys, machine, stub, run_hook, verdict, monkeypatch
) -> None:
    """A governed call nobody could judge is exactly what fail-closed is for.

    An argument layout the hook does not know is not an approval: it is a call
    the service never saw. Letting it through silently is the one thing an
    owner who set THREEFOLD_FAIL_CLOSED=1 asked not to happen.
    """
    monkeypatch.setenv("THREEFOLD_FAIL_CLOSED", "1")
    payload = _unreadable_shell(machine.project, tool, payload_keys)
    code, out, err = run_hook(payload, ["--agent", agent])
    assert (code, stub.requests) == (0, [])
    assert verdict.decision(out) == "deny"
    assert "THREEFOLD_FAIL_CLOSED" in verdict.reason(out)
    assert "shape the hook cannot read" in err
    assert (machine.threefold_home / "unknown_shapes.jsonl").exists()


def test_a_shape_the_hook_cannot_read_still_fails_open_by_default(machine, stub, run_hook) -> None:
    code, out, err = run_hook(_unreadable_shell(machine.project, "shell", {"argv": ["bash", "-lc", "rm -rf src"]}), ["--agent", "codex"])
    assert (code, out, stub.requests) == (0, "", [])
    assert "shape the hook cannot read" in err


# --- a single file that runs alone ----------------------------------------------------

def test_the_hook_imports_nothing_outside_the_standard_library() -> None:
    tree = ast.parse(HOOK_PATH.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "A relative import cannot work in a downloaded file"
            imported.add(node.module.split(".")[0])
    assert imported - set(sys.stdlib_module_names) - {"__future__"} == set()


def test_importing_the_hook_does_nothing(capsys, machine) -> None:
    """Tests import it; nothing may read stdin, print, or touch THREEFOLD_HOME on import."""
    from conftest import load_hook

    load_hook()
    assert capsys.readouterr() == ("", "")
    assert not machine.threefold_home.exists()


def _run_copied_hook(tmp_path: Path, payload, extra_env: dict, argv=()) -> subprocess.CompletedProcess:
    """Runs a copy of the file from a directory holding nothing else, with no src on the path."""
    alone = tmp_path / "downloads"
    alone.mkdir(exist_ok=True)
    shutil.copy(HOOK_PATH, alone / "threefold_hook.py")
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH" and not key.startswith("THREEFOLD_")}
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "threefold_hook.py", *argv],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        cwd=alone,
        env=env,
        timeout=30,
    )


def test_the_downloaded_file_refuses_a_credential_on_its_own(machine, payloads) -> None:
    """No network and no package: the local pre-scan is the whole path."""
    payload = payloads.write("antigravity", "src/settings.py", "KEY = 'AKIAABCDEFGHIJKLMNOP'")
    result = _run_copied_hook(
        machine.tmp,
        payload,
        {
            "HOME": str(machine.home),
            "USERPROFILE": str(machine.home),
            "THREEFOLD_HOME": str(machine.threefold_home),
            "THREEFOLD_PROJECT": "Acme-Payments",
            "THREEFOLD_ENDPOINT": "http://127.0.0.1:9/prod/",
        },
        ["--agent", "antigravity"],
    )
    assert result.returncode == 0
    document = json.loads(result.stdout)
    assert document["decision"] == "deny"
    assert "AWS_ACCESS_KEY" in document["reason"]


def test_the_downloaded_file_asks_the_service_and_stays_silent_on_approval(machine, payloads, stub) -> None:
    result = _run_copied_hook(
        machine.tmp,
        payloads.write("claude-code"),
        {
            "HOME": str(machine.home),
            "USERPROFILE": str(machine.home),
            "THREEFOLD_HOME": str(machine.threefold_home),
            "THREEFOLD_PROJECT": "Acme-Payments",
            "THREEFOLD_ENDPOINT": stub.endpoint,
            "NO_PROXY": "127.0.0.1,localhost",
        },
    )
    assert (result.returncode, result.stdout) == (0, b"")
    assert stub.requests[0]["body"]["project_name"] == "Acme-Payments"


def test_the_downloaded_file_survives_malformed_input(machine) -> None:
    alone = machine.tmp / "downloads"
    alone.mkdir()
    shutil.copy(HOOK_PATH, alone / "threefold_hook.py")
    result = subprocess.run(
        [sys.executable, "threefold_hook.py"],
        input=b"\xff\xfe not json",
        capture_output=True,
        cwd=alone,
        env={**{k: v for k, v in os.environ.items() if not k.startswith("THREEFOLD_")}, "HOME": str(machine.home), "USERPROFILE": str(machine.home)},
        timeout=30,
    )
    assert (result.returncode, result.stdout) == (0, b"")
    assert result.stderr.strip()
