"""The halt has to outlive the worker that decided it.

Threefold runs on Lambda, so two requests in the same session routinely land on
two different containers. If a trip lived only in the memory of the container
that detected it, a second container would keep approving calls on a session
that is already halted, and the product's central claim would be false. These
tests pin that behaviour.
"""
from __future__ import annotations

import pytest

from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository, SessionConflictError


def _repeated_call(session_id: str) -> ToolCallRequestDTO:
    return ToolCallRequestDTO(
        session_id=session_id,
        developer_id="dev-durable",
        project_name="DurabilityCheck",
        tool_name="run_tests",
        action_type="COMMAND_EXEC",
        arguments={"cmd": "pytest -q"},
        projected_input_tokens=1000,
        projected_output_tokens=200,
        budget_usd=10.00,
    )


def _shared_store_repos() -> tuple[DynamoDBSessionRepository, DynamoDBSessionRepository]:
    """Two repositories over one store, standing in for two Lambda containers."""
    first = DynamoDBSessionRepository()
    second = DynamoDBSessionRepository()
    second._memory_store = first._memory_store
    return first, second


def test_trip_is_written_through_so_a_second_worker_sees_it() -> None:
    repo_a, repo_b = _shared_store_repos()
    worker_a = GovernanceEvaluator(session_repo=repo_a)
    request = _repeated_call("session-durable-1")

    assert worker_a.evaluate_tool_call(request).status == "APPROVED"
    assert worker_a.evaluate_tool_call(request).status == "APPROVED"
    third = worker_a.evaluate_tool_call(request)
    assert third.status == "BLOCKED_LOOP_DETECTED"
    assert third.session_tripped is True

    # A worker that never saw the first three calls must still find the session halted.
    reloaded = repo_b.get_session("session-durable-1")
    assert reloaded is not None
    assert reloaded.is_tripped is True
    assert "identical" in (reloaded.trip_reason or "")


def test_a_second_worker_cannot_approve_a_halted_session() -> None:
    repo_a, repo_b = _shared_store_repos()
    worker_a = GovernanceEvaluator(session_repo=repo_a)
    worker_b = GovernanceEvaluator(session_repo=repo_b)
    request = _repeated_call("session-durable-2")

    worker_a.evaluate_tool_call(request)
    worker_a.evaluate_tool_call(request)
    assert worker_a.evaluate_tool_call(request).status == "BLOCKED_LOOP_DETECTED"

    # Worker B arrives with a different tool call, which on its own would pass
    # every gate. The session is halted, so it must be refused anyway.
    fresh_tool = ToolCallRequestDTO(
        session_id="session-durable-2",
        developer_id="dev-durable",
        project_name="DurabilityCheck",
        tool_name="read_file",
        action_type="FILE_READ",
        arguments={"path": "/src/main.py"},
        projected_input_tokens=100,
        projected_output_tokens=50,
        budget_usd=10.00,
    )
    verdict = worker_b.evaluate_tool_call(fresh_tool)
    assert verdict.status == "BLOCKED_CIRCUIT_BREAKER"
    assert verdict.session_tripped is True


def test_the_store_refuses_to_clear_a_halted_session() -> None:
    """The terminal-state guard, exercised directly rather than through a verdict."""
    repo = DynamoDBSessionRepository()
    evaluator = GovernanceEvaluator(session_repo=repo)
    request = _repeated_call("session-durable-3")
    evaluator.evaluate_tool_call(request)
    evaluator.evaluate_tool_call(request)
    evaluator.evaluate_tool_call(request)

    halted = repo.get_session("session-durable-3")
    assert halted is not None and halted.is_tripped is True

    halted.is_tripped = False
    halted.trip_reason = None
    with pytest.raises(SessionConflictError):
        repo.save_session(halted)

    # An operator may still act on it, which is what force is for.
    assert repo.save_session(halted, force=True) is True


def test_operator_termination_survives_an_existing_trip() -> None:
    repo = DynamoDBSessionRepository()
    evaluator = GovernanceEvaluator(session_repo=repo)
    request = _repeated_call("session-durable-4")
    evaluator.evaluate_tool_call(request)
    evaluator.evaluate_tool_call(request)
    evaluator.evaluate_tool_call(request)

    session = evaluator.terminate_session(
        "session-durable-4", operator_name="ops-lead", reason="incident review"
    )
    assert session.is_terminated is True

    reloaded = repo.get_session("session-durable-4")
    assert reloaded is not None
    assert reloaded.is_terminated is True
    assert reloaded.terminated_by == "ops-lead"
    assert reloaded.termination_reason == "incident review"
