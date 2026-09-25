"""Every policy field is enforced, not only stored.

Three of the policy's fields used to be decorative: the session ceiling was
never read, the history window never reached the detector, and the pattern
list was discarded by the write path. They now bind: the ceiling trips
through the breaker, the window sets the detector's cycle search, and the
patterns refuse through the secret gate. A policy saved on one container is
adopted by the others within the refresh interval rather than at cold start.
"""
from __future__ import annotations

import json

import pytest

from threefold.application.dtos import PolicyConfigDTO, ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository
from threefold.interfaces.api_handlers import _evaluator, lambda_handler

OPERATOR_KEY = "policy-enforced-test-key"


@pytest.fixture(autouse=True)
def _restore_the_policy_this_file_writes():
    before = _evaluator.policy_config
    yield
    _evaluator.update_policy(before)


@pytest.fixture
def _operator_key(monkeypatch):
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)


def _post_policy(body: dict, key: str | None = OPERATOR_KEY) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["X-API-Key"] = key
    response = lambda_handler(
        {
            "httpMethod": "POST",
            "path": "/policy/config",
            "headers": headers,
            "body": json.dumps(body),
        }
    )
    return response["statusCode"], json.loads(response["body"])


def _govern(session_id: str, **overrides) -> dict:
    call = {
        "session_id": session_id,
        "developer_id": "policy-test",
        "project_name": "Acme-Policy",
        "tool_name": "view_file",
        "action_type": "FILE_READ",
        "arguments": {"path": "README.md"},
        "projected_input_tokens": 100,
        "projected_output_tokens": 50,
        "budget_usd": 100.0,
    }
    call.update(overrides)
    response = lambda_handler(
        {
            "rawPath": "/prod/evaluate-tool-call",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(call),
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
        }
    )
    assert response["statusCode"] == 200
    return json.loads(response["body"])


def test_the_session_ceiling_trips_through_the_breaker(_operator_key) -> None:
    status, _ = _post_policy({"max_session_budget_usd": 0.0005})
    assert status == 200
    verdict = _govern("policy-ceiling-001")
    assert verdict["status"] == "BLOCKED_CIRCUIT_BREAKER"
    assert "policy session ceiling" in verdict["reason"]


def test_the_window_reaches_the_detector(_operator_key) -> None:
    status, body = _post_policy({"loop_history_window": 2})
    assert status == 200
    assert body["config"]["loop_history_window"] == 2
    assert _evaluator.loop_detector.max_cycle_length == 2

    status, body = _post_policy({"loop_history_window": 10**9})
    assert status == 200
    assert _evaluator.loop_detector.max_cycle_length == 25, "A wild window must not hang the gate"


def test_blocked_patterns_are_accepted_and_enforced(_operator_key) -> None:
    status, body = _post_policy({"blocked_patterns": [r"AcmeSecret\w+"]})
    assert status == 200
    assert body["config"]["blocked_patterns"] == [r"AcmeSecret\w+"]

    verdict = _govern(
        "policy-patterns-001",
        tool_name="write_to_file",
        action_type="FILE_WRITE",
        arguments={"TargetFile": "/src/notes.txt", "CodeContent": "deploy AcmeSecretProject at dawn"},
    )
    assert verdict["status"] == "BLOCKED_SECRET_DETECTED"
    assert "Blocked pattern" in verdict["reason"]
    assert "AcmeSecretProject" not in verdict["reason"]


def test_blocked_patterns_cannot_be_staged(_operator_key) -> None:
    """Additional secret shapes refuse with the compiled shapes' force: always."""
    from threefold.application.rule_keys import GATE_KEYS

    assert "CREDENTIAL" not in GATE_KEYS
    status, _ = _post_policy({"blocked_patterns": [r"AcmeSecret\w+"]})
    assert status == 200
    verdict = _govern(
        "policy-patterns-observe-001",
        tool_name="write_to_file",
        action_type="FILE_WRITE",
        arguments={"content": "AcmeSecretProject"},
        dry_run=True,
    )
    assert verdict["status"] == "APPROVED", "A dry run is recorded, not refused"
    assert verdict["rule_evaluations"]["SECRET_LEAKAGE_FREE"] is False
    assert "Blocked pattern" in verdict["reason"]


@pytest.mark.parametrize(
    "patterns",
    [
        "AcmeSecret",
        [42],
        [""],
        ["x" * 201],
        ["([unclosed"],
        [f"pattern-{i}" for i in range(51)],
    ],
)
def test_malformed_pattern_lists_are_refused_with_their_position(_operator_key, patterns) -> None:
    status, problem = _post_policy({"blocked_patterns": patterns})
    assert status == 400
    assert problem["invalid_params"][0]["name"] == "blocked_patterns"


def test_a_write_without_patterns_keeps_the_shipped_shapes(_operator_key) -> None:
    status, body = _post_policy({"max_single_call_usd": 0.5})
    assert status == 200
    assert body["config"]["blocked_patterns"] == PolicyConfigDTO().blocked_patterns


def test_an_explicit_empty_list_clears_the_patterns(_operator_key) -> None:
    status, body = _post_policy({"blocked_patterns": []})
    assert status == 200
    assert body["config"]["blocked_patterns"] == []
    verdict = _govern(
        "policy-patterns-empty-001",
        tool_name="write_to_file",
        action_type="FILE_WRITE",
        arguments={"TargetFile": "/src/notes.txt", "CodeContent": "nothing blocked here"},
    )
    assert verdict["status"] == "APPROVED"


def test_a_policy_saved_on_one_container_reaches_the_others() -> None:
    """Within the refresh interval, not at cold start only."""
    repo = DynamoDBSessionRepository()
    first = GovernanceEvaluator(session_repo=repo)
    second = GovernanceEvaluator(session_repo=repo)
    assert second.policy_config.max_single_call_usd == 1.00

    first.update_policy(
        PolicyConfigDTO(
            max_single_call_usd=0.25,
            max_session_budget_usd=10.00,
            loop_history_window=6,
            monomorphic_repetition_threshold=3,
        )
    )
    second.refresh_policy_if_stale()
    assert second.policy_config.max_single_call_usd == 1.00, "A fresh copy is not re-read"

    second._policy_read_at -= 31.0
    second.refresh_policy_if_stale()
    assert second.policy_config.max_single_call_usd == 0.25
    assert second.cost_breaker.max_single_invocation_cost == 0.25


def test_an_evaluation_refreshes_a_stale_policy() -> None:
    repo = DynamoDBSessionRepository()
    first = GovernanceEvaluator(session_repo=repo)
    second = GovernanceEvaluator(session_repo=repo)
    first.update_policy(
        PolicyConfigDTO(
            max_single_call_usd=100.0,
            max_session_budget_usd=0.0001,
            loop_history_window=6,
            monomorphic_repetition_threshold=3,
        )
    )
    second._policy_read_at -= 31.0
    result = second.evaluate_tool_call(
        ToolCallRequestDTO(
            session_id="policy-refresh-001",
            developer_id="policy-test",
            project_name="Acme-Policy",
            tool_name="view_file",
            action_type="FILE_READ",
            arguments={"path": "README.md"},
            projected_input_tokens=100,
            projected_output_tokens=50,
            budget_usd=100.0,
        )
    )
    assert result.status == "BLOCKED_CIRCUIT_BREAKER"
    assert "policy session ceiling" in result.reason
