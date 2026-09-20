"""The hook is what makes the product intercept rather than rehearse.

These tests do not reach the network. They drive the hook's decision function
against a stubbed endpoint, because what has to be pinned is the contract with
Claude Code: what it sends, what it returns, and what it does when the service
cannot be reached.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# The hook moved under src/ so the deployment can hand it out; anything outside
# CodeUri never reaches the function.
HOOKS = Path(__file__).resolve().parents[2] / "src" / "threefold" / "hooks"
sys.path.insert(0, str(HOOKS))

import claude_code_hook as hook  # noqa: E402


class _Response:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def captured(monkeypatch):
    """Replaces the HTTP call and records what the hook would have sent."""
    sent = {}

    def fake_urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["body"] = json.loads(request.data.decode("utf-8"))
        return _Response(sent.get("verdict", {"status": "APPROVED"}))

    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_urlopen)
    return sent


def _decision(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecision"]


def test_an_approved_call_is_allowed(captured) -> None:
    result = hook.evaluate(
        {"tool_name": "Edit", "tool_input": {"file_path": "src/app.py", "new_string": "x = 1"}},
        "https://example.invalid/prod",
        "s1",
    )
    assert _decision(result) == "allow"


def test_a_refused_call_is_denied_and_carries_the_reason(captured) -> None:
    captured["verdict"] = {
        "status": "BLOCKED_BOUNDARY_VIOLATION",
        "reason": "Clean Architecture violation: domain file cannot depend on an outer layer",
        "bedrock_explanation": "Importing the AWS SDK into the domain couples your core to infrastructure.",
        "explanation_source": "bedrock",
    }
    result = hook.evaluate(
        {"tool_name": "Write", "tool_input": {"file_path": "src/domain/u.py", "content": "import boto3"}},
        "https://example.invalid/prod",
        "s2",
    )
    assert _decision(result) == "deny"
    detail = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Clean Architecture violation" in detail
    assert "Bedrock:" in detail, "The explanation must be attributed, as it is in the API"


def test_a_deterministic_explanation_is_not_labelled_as_bedrock(captured) -> None:
    captured["verdict"] = {
        "status": "BLOCKED_LOOP_DETECTED",
        "reason": "Monomorphic loop detected",
        "bedrock_explanation": "The same call was made three times.",
        "explanation_source": "deterministic_fallback",
    }
    result = hook.evaluate({"tool_name": "Bash", "tool_input": {"command": "pytest"}}, "https://x.invalid", "s3")
    detail = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Bedrock:" not in detail
    assert "Deterministic explanation:" in detail


def test_claude_code_tool_names_are_translated(captured) -> None:
    hook.evaluate({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "https://x.invalid", "s4")
    assert captured["body"]["action_type"] == "COMMAND_EXEC"
    hook.evaluate({"tool_name": "Read", "tool_input": {"file_path": "a.py"}}, "https://x.invalid", "s5")
    assert captured["body"]["action_type"] == "FILE_READ"


def test_the_whole_tool_input_is_forwarded(captured) -> None:
    """The guard finds paths and secrets by shape, so it needs every argument."""
    hook.evaluate(
        {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "src/domain/u.py", "new_source": "import boto3"}},
        "https://x.invalid",
        "s6",
    )
    assert captured["body"]["arguments"] == {
        "notebook_path": "src/domain/u.py",
        "new_source": "import boto3",
    }


def test_an_unreachable_service_allows_the_call_and_says_so(monkeypatch) -> None:
    """Fail open by default, and never silently."""
    def boom(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(hook.urllib.request, "urlopen", boom)
    monkeypatch.delenv("THREEFOLD_FAIL_CLOSED", raising=False)
    result = hook.evaluate({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "https://x.invalid", "s7")
    assert _decision(result) == "allow"
    assert "not checked" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_fail_closed_can_be_turned_on(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(hook.urllib.request, "urlopen", boom)
    monkeypatch.setenv("THREEFOLD_FAIL_CLOSED", "1")
    result = hook.evaluate({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "https://x.invalid", "s8")
    assert _decision(result) == "deny"


def test_a_non_dict_tool_input_does_not_crash_the_agent(captured) -> None:
    result = hook.evaluate({"tool_name": "Bash", "tool_input": "ls -la"}, "https://x.invalid", "s9")
    assert _decision(result) in ("allow", "deny")
    assert captured["body"]["arguments"] == {"value": "ls -la"}
