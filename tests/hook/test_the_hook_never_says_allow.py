"""The hook can take permission away and can never grant it.

The previous hook printed `permissionDecision: allow` on every approval and on
every failure to reach the service. Claude Code obeys an allow by skipping its
own permission prompt, so an unreachable service, or a service that approved
the call on the one axis it checks, waved through calls the developer would
otherwise have been asked about. Silence leaves the agent's own flow in charge;
that is the only thing an approval may print.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

AGENTS = ("claude-code", "codex", "antigravity")
HOOK_PATH = Path(__file__).resolve().parents[2] / "src" / "threefold" / "hooks" / "threefold_hook.py"

REFUSAL = {
    "status": "BLOCKED_BOUNDARY_VIOLATION",
    "reason": "Clean Architecture violation: domain file cannot depend on an outer layer",
    "bedrock_explanation": "Importing the AWS SDK into the domain couples the core to infrastructure.",
    "explanation_source": "bedrock",
}

SCENARIOS = {
    # name: (what the stub does, what the hook must print)
    "approved": ((200, {"status": "APPROVED"}), None),
    "denied": ((200, REFUSAL), "deny"),
    "http-400": ((400, {"type": "about:blank", "title": "Invalid Parameter", "status": 400, "detail": "bad"}), "deny"),
    "http-413": ((413, {"message": "Request Entity Too Large"}), "deny"),
    "http-429": ((429, {"title": "Too Many Requests", "status": 429}), None),
    "http-500": ((500, {"title": "Internal Server Error", "status": 500}), None),
    "timeout": ("timeout", None),
    "refused-connection": ("refused", None),
    "malformed-stdin": ("malformed", None),
}


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_a_decision_is_never_allow(scenario, agent, request, payloads, run_hook, verdict, monkeypatch) -> None:
    behaviour, expected = SCENARIOS[scenario]
    raw = None
    if behaviour == "refused":
        request.getfixturevalue("closed_endpoint")
    elif behaviour == "malformed":
        raw = '{"tool_name": "Write", "tool_input": '
    else:
        stub = request.getfixturevalue("stub")
        if behaviour == "timeout":
            stub.delay = 1.0
            monkeypatch.setenv("THREEFOLD_TIMEOUT", "0.2")
        else:
            stub.answer(*behaviour)

    code, out, _ = run_hook(payloads.write(agent), ["--agent", agent], raw=raw)

    assert code == 0
    assert verdict.decision(out) == expected
    assert '"allow"' not in out


@pytest.mark.parametrize("agent", AGENTS)
def test_an_internal_error_is_silent_rather_than_an_allow(agent, hook, payloads, run_hook, monkeypatch) -> None:
    """A bug in the hook must not stop the agent, and must not approve anything either."""

    def broken(*_args, **_kwargs):
        raise RuntimeError("Acme-Secret content that must not be echoed")

    monkeypatch.setattr(hook, "held_back_category", broken)
    code, out, err = run_hook(payloads.write(agent), ["--agent", agent])
    assert code == 0
    assert out == ""
    assert "RuntimeError" in err
    assert "Acme-Secret" not in err, "An exception's message can quote the content it choked on"


def test_the_hook_contains_no_allow_decision_at_all() -> None:
    """Checked in the source, so no code path added later can print one."""
    tree = ast.parse(HOOK_PATH.read_text(encoding="utf-8"))
    literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert "allow" not in literals
    assert "approve" not in literals


def test_the_deny_format_for_claude_code_and_codex_is_hook_specific_output(hook) -> None:
    for agent in ("claude-code", "codex"):
        assert hook.deny(agent, "because") == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "because",
            }
        }


def test_the_deny_format_for_antigravity_is_a_decision_and_a_reason(hook) -> None:
    assert hook.deny("antigravity", "because") == {"decision": "deny", "reason": "because"}


@pytest.mark.parametrize("agent", AGENTS)
def test_a_refusal_is_printed_as_one_json_document_in_the_agents_format(agent, stub, payloads, run_hook) -> None:
    stub.answer(200, REFUSAL)
    _, out, _ = run_hook(payloads.write(agent), ["--agent", agent])
    document = json.loads(out)
    if agent == "antigravity":
        assert set(document) == {"decision", "reason"}
        assert document["decision"] == "deny"
    else:
        assert set(document) == {"hookSpecificOutput"}
        assert document["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert document["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("agent", AGENTS)
def test_malformed_stdin_prints_nothing_and_says_so_on_stderr(agent, stub, run_hook) -> None:
    code, out, err = run_hook(None, ["--agent", agent], raw="not json at all")
    assert (code, out) == (0, "")
    assert len(err.strip().splitlines()) == 1
    assert stub.requests == []


def test_stdin_that_is_json_but_not_an_object_is_malformed_too(stub, run_hook) -> None:
    code, out, err = run_hook(None, raw='["Write", {"file_path": "a.py"}]')
    assert (code, out) == (0, "")
    assert err.strip()
    assert stub.requests == []
