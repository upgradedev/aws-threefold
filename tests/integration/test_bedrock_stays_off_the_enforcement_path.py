"""The model phrases refusals. It is never in the way of a decision.

A hook sits in front of every tool call a coding agent makes, so anything the
verdict waits on is latency somebody pays for on every edit, and a bill. So the
model is asked only when there is a refusal for a person to read and the caller
asked for the sentence: never for an approval, never for a hook that sent
explain=false, and never for longer than a few seconds. What it is asked
carries no developer, no credential and about 2 KB of arguments at most.

Names and the credential below are synthetic: AKIAIOSFODNN7EXAMPLE is AWS's own
published example key.
"""
from __future__ import annotations

import json

import pytest

from threefold.application.bedrock_reviewer import SOURCE_BEDROCK, SOURCE_DETERMINISTIC, SOURCE_FALLBACK
from threefold.infrastructure.bedrock_client import (
    CLIENT_TIMEOUTS,
    MAX_PROMPT_ARGUMENTS_CHARS,
    BedrockGovernanceClient,
    prompt_safe,
)
from threefold.interfaces import api_handlers
from threefold.interfaces.api_handlers import lambda_handler

EXAMPLE_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


class _Stub:
    """Stands in for bedrock-runtime and records every prompt it was sent."""

    def __init__(self, text: str = "The write was refused because it crosses a layer.") -> None:
        self.text = text
        self.prompts: list[dict] = []

    def converse(self, **kwargs):
        self.prompts.append(kwargs)
        return {"output": {"message": {"content": [{"text": self.text}]}}}


class _Failing:
    def converse(self, **kwargs):
        raise RuntimeError("model unavailable in this region")


@pytest.fixture
def model(monkeypatch) -> _Stub:
    """Binds a stub to the handler's own client and puts it back afterwards."""
    stub = _Stub()
    monkeypatch.setattr(api_handlers._bedrock_client, "_client", stub)
    monkeypatch.setattr(api_handlers._bedrock_client, "calls_made", 0)
    monkeypatch.setattr(api_handlers._bedrock_client, "last_error", None)
    return stub


def _evaluate(session_id: str, **overrides) -> dict:
    body = {
        "session_id": session_id,
        "developer_id": "acme-dev-bedrock",
        "project_name": "Acme-Bedrock",
        "tool_name": "Write",
        "action_type": "FILE_WRITE",
        "arguments": {"file_path": "src/domain/order.py", "content": "import boto3"},
        "projected_input_tokens": 100,
        "projected_output_tokens": 50,
        "budget_usd": 5.0,
    }
    body.update(overrides)
    response = lambda_handler(
        {
            "rawPath": "/prod/evaluate-tool-call",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body),
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
        }
    )
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def test_an_approval_never_reaches_the_model(model: _Stub) -> None:
    """An approval needs no persuading, and the agent is waiting on it."""
    verdict = _evaluate(
        "bedrock-approved",
        tool_name="view_file",
        action_type="FILE_READ",
        arguments={"path": "README.md"},
    )
    assert verdict["status"] == "APPROVED"
    assert verdict["explanation_source"] == SOURCE_DETERMINISTIC
    assert verdict["bedrock_explanation"], "The caller still gets a sentence, just not a model's"
    assert model.prompts == [], "Bedrock was called for an approval"


def test_a_hook_that_wants_no_sentence_is_not_made_to_wait_for_one(model: _Stub) -> None:
    verdict = _evaluate("bedrock-no-explain", explain=False, agent="claude-code", origin="hook")
    assert verdict["status"] == "BLOCKED_BOUNDARY_VIOLATION"
    assert verdict["explanation_source"] == SOURCE_DETERMINISTIC
    assert model.prompts == [], "Bedrock was called for a caller that asked for no explanation"


def test_a_refusal_a_page_will_show_is_explained_by_the_model(model: _Stub) -> None:
    """The other half: with explain left to its default, the sentence is a real one."""
    verdict = _evaluate("bedrock-explained", origin="page", agent="page")
    assert verdict["status"] == "BLOCKED_BOUNDARY_VIOLATION"
    assert verdict["explanation_source"] == SOURCE_BEDROCK
    assert verdict["bedrock_explanation"] == model.text
    assert len(model.prompts) == 1


def test_a_dry_run_is_not_explained_by_the_model_either(model: _Stub) -> None:
    """It is recorded as an approval, and its sentence says what would have refused it."""
    verdict = _evaluate("bedrock-dry-run", dry_run=True)
    assert verdict["status"] == "APPROVED"
    assert verdict["explanation_source"] == SOURCE_DETERMINISTIC
    assert "dry run" in verdict["bedrock_explanation"]
    assert "cleared every gate" not in verdict["bedrock_explanation"]
    assert model.prompts == []


def test_a_model_that_fails_is_labelled_as_a_fallback_not_as_a_refusal(monkeypatch) -> None:
    monkeypatch.setattr(api_handlers._bedrock_client, "_client", _Failing())
    monkeypatch.setattr(api_handlers._bedrock_client, "calls_made", 0)
    monkeypatch.setattr(api_handlers._bedrock_client, "last_error", None)
    verdict = _evaluate("bedrock-failed")
    assert verdict["status"] == "BLOCKED_BOUNDARY_VIOLATION", "The verdict does not depend on the model"
    assert verdict["explanation_source"] == SOURCE_FALLBACK
    assert "Bedrock" not in verdict["bedrock_explanation"]


def test_the_prompt_carries_no_developer_and_no_credential(model: _Stub) -> None:
    _evaluate(
        "bedrock-redacted",
        developer="acme-dev-private",
        tool_name="run_command",
        action_type="COMMAND_EXEC",
        arguments={"command": f"export AWS_ACCESS_KEY_ID={EXAMPLE_KEY}"},
    )
    sent = json.dumps(model.prompts[0])
    assert EXAMPLE_KEY not in sent, "The credential the gate stopped was sent to a third party"
    assert "AWS_ACCESS_KEY REDACTED" in sent
    assert "acme-dev-private" not in sent, "A developer must not be named to the model"


def test_a_credential_at_the_truncation_boundary_is_redacted_before_it_is_cut(model: _Stub) -> None:
    """Truncating first would cut the key in half, and half a key matches no pattern."""
    padding = "x" * (MAX_PROMPT_ARGUMENTS_CHARS - 20)
    _evaluate(
        "bedrock-boundary",
        tool_name="run_command",
        action_type="COMMAND_EXEC",
        arguments={"command": f"echo {padding}{EXAMPLE_KEY}"},
    )
    sent = json.dumps(model.prompts[0])
    assert "AKIAIOSF" not in sent, "A prefix of the key survived the cut"


def test_a_huge_argument_is_cut_to_about_two_kilobytes(model: _Stub) -> None:
    _evaluate(
        "bedrock-huge",
        tool_name="run_command",
        action_type="COMMAND_EXEC",
        arguments={"command": "cat ~/.aws/credentials " + "y" * 200_000},
    )
    prompt = model.prompts[0]["messages"][0]["content"][0]["text"]
    assert len(prompt) < MAX_PROMPT_ARGUMENTS_CHARS + 1024, "The whole argument was sent"
    assert "truncated" in prompt, "A cut prompt must say it was cut"


def test_prompt_safe_leaves_short_clean_text_alone() -> None:
    assert prompt_safe("cat README.md") == "cat README.md"


def test_the_client_waits_seconds_rather_than_minutes() -> None:
    """botocore's defaults are a 60 second read and three retries, on every call."""
    pytest.importorskip("botocore")
    captured = {}

    class _Session:
        def client(self, name, **kwargs):
            captured.update(kwargs)
            captured["name"] = name
            return _Stub()

    BedrockGovernanceClient(boto3_session=_Session())
    config = captured["config"]
    assert config is not None, "The client was built on botocore's defaults"
    assert config.connect_timeout == CLIENT_TIMEOUTS["connect_timeout"] == 1
    assert config.read_timeout == CLIENT_TIMEOUTS["read_timeout"] == 2.5
    assert config.retries == {"max_attempts": 1, "mode": "standard"}
