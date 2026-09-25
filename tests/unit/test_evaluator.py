"""Unit tests for GovernanceEvaluator and AuditIssuer."""
from __future__ import annotations

import pytest
from threefold.application.audit_issuer import AuditIssuer
from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator


def test_evaluator_approves_safe_request():
    evaluator = GovernanceEvaluator()
    req = ToolCallRequestDTO(
        session_id="session-eval-1",
        developer_id="dev-alice",
        project_name="OrderService",
        tool_name="view_file",
        action_type="FILE_READ",
        arguments={"AbsolutePath": "/src/service.py"},
        projected_input_tokens=1000,
        projected_output_tokens=200,
        budget_usd=5.00,
    )
    result = evaluator.evaluate_tool_call(req)
    assert result.status == "APPROVED"
    assert result.risk_level == "LOW"
    assert result.session_tripped is False
    assert result.rule_evaluations["SECRET_LEAKAGE_FREE"] is True
    assert result.rule_evaluations["ARCHITECTURAL_BOUNDARY_SAFE"] is True


def test_evaluator_trips_on_loop():
    evaluator = GovernanceEvaluator()
    req = ToolCallRequestDTO(
        session_id="session-eval-2",
        developer_id="dev-alex",
        project_name="Acme-Payments",
        tool_name="edit_file",
        action_type="FILE_WRITE",
        arguments={"TargetFile": "src/pay.py", "Instruction": "fix typo"},
    )
    # Call 1 & 2 succeed
    evaluator.evaluate_tool_call(req)
    evaluator.evaluate_tool_call(req)
    # Call 3 triggers loop detection
    res3 = evaluator.evaluate_tool_call(req)
    assert res3.status == "BLOCKED_LOOP_DETECTED"
    assert res3.session_tripped is True


def test_repeating_a_read_is_recorded_rather_than_halting_the_session():
    """Reading the same file three times is polling, not a runaway.

    The first version halted a session for it, so an agent re-reading a file
    while it waited on a build was locked out of its own session.
    """
    evaluator = GovernanceEvaluator()
    req = ToolCallRequestDTO(
        session_id="session-eval-read",
        developer_id="dev-alex",
        project_name="Acme-Payments",
        tool_name="view_file",
        action_type="FILE_READ",
        arguments={"AbsolutePath": "/src/pay.py"},
    )
    results = [evaluator.evaluate_tool_call(req) for _ in range(4)]
    assert all(result.status == "APPROVED" for result in results)
    assert results[-1].session_tripped is False


def test_audit_issuer_generates_valid_certificate():
    evaluator = GovernanceEvaluator()
    req = ToolCallRequestDTO(
        session_id="session-eval-cert",
        developer_id="dev-charlie",
        project_name="InventoryService",
        tool_name="view_file",
        action_type="FILE_READ",
        arguments={"AbsolutePath": "/src/catalog.py"},
    )
    res = evaluator.evaluate_tool_call(req)
    session = evaluator.get_or_create_session(req.session_id)
    cert = AuditIssuer.issue_certificate(session)

    assert cert.verdict_status == "COMPLIANT_APPROVED"
    assert cert.all_passed is True
    assert len(cert.sha256_fingerprint) == 64
    assert cert.certificate_id.startswith("CERT-TF-")


def test_the_reported_policy_is_the_policy_the_gates_enforce():
    """A settings page reads /policy/config. That has to be what a call is measured against.

    The evaluator used to build its breaker with a $2.50 cap while reporting a
    $1.00 policy, so a fresh stack described a limit it did not apply until a
    policy happened to be posted. An operator reading the settings page would have
    been told a number no call was ever checked against.
    """
    evaluator = GovernanceEvaluator()
    assert evaluator.cost_breaker.max_single_invocation_cost == evaluator.policy_config.max_single_call_usd
    assert evaluator.loop_detector.repetition_threshold == evaluator.policy_config.monomorphic_repetition_threshold


def test_a_supplied_gate_is_left_as_it_was_supplied():
    """Building the gates from the policy must not overwrite one a caller injected."""
    from threefold.domain.circuit_breaker import CostCircuitBreaker

    injected = CostCircuitBreaker(max_single_invocation_cost=7.5)
    evaluator = GovernanceEvaluator(cost_breaker=injected)
    assert evaluator.cost_breaker is injected
    assert evaluator.cost_breaker.max_single_invocation_cost == 7.5

def test_the_same_call_costs_by_the_model_that_made_it():
    evaluator = GovernanceEvaluator()

    def governed(model: str, session: str):
        return evaluator.evaluate_tool_call(
            ToolCallRequestDTO(
                session_id=session,
                developer_id="dev-test",
                project_name="Acme-Pricing",
                tool_name="view_file",
                action_type="FILE_READ",
                arguments={"path": "README.md"},
                projected_input_tokens=100_000,
                projected_output_tokens=0,
                budget_usd=100.00,
                model_id=model,
            )
        )

    haiku = governed("claude-haiku-4-5-20251001", "session-price-haiku")
    default = governed("default", "session-price-default")
    assert haiku.current_session_cost_usd == 0.10
    assert default.current_session_cost_usd == 0.30

    rows = {row["session_id"]: row for row in evaluator.list_decisions(days=30, limit=10)}
    assert rows["session-price-haiku"]["model"] == "claude-haiku-4-5-20251001"
    assert rows["session-price-default"]["model"] == "default"

def test_fuzzy_signature_normalizes_past_the_bytes():
    from threefold.application.evaluator import _fuzzy_signature
    from threefold.domain.models import ToolActionType, ToolInvocation

    def edit(content):
        return ToolInvocation(
            tool_name="write_to_file",
            action_type=ToolActionType.FILE_WRITE,
            arguments={"TargetFile": "/src/notes.txt", "CodeContent": content},
        )

    assert _fuzzy_signature(edit("one")) == _fuzzy_signature(edit("two"))
    other = ToolInvocation(
        tool_name="write_to_file",
        action_type=ToolActionType.FILE_WRITE,
        arguments={"TargetFile": "/src/other.txt", "CodeContent": "one"},
    )
    assert _fuzzy_signature(edit("one")) != _fuzzy_signature(other)
    search = ToolInvocation(
        tool_name="web_search",
        action_type=ToolActionType.WEB_SEARCH,
        arguments={"query": "something"},
    )
    assert _fuzzy_signature(search) is None


def test_five_same_shape_edits_halt_where_three_do_not():
    evaluator = GovernanceEvaluator()

    def edit_run(content, session="session-fuzzy-edits"):
        return evaluator.evaluate_tool_call(
            ToolCallRequestDTO(
                session_id=session,
                developer_id="dev-test",
                project_name="Acme-Fuzzy",
                tool_name="write_to_file",
                action_type="FILE_WRITE",
                arguments={"TargetFile": "/src/notes.txt", "CodeContent": content},
                budget_usd=100.00,
            )
        )

    for i in range(4):
        result = edit_run(f"attempt {i}")
        assert result.status == "APPROVED", result
    fifth = edit_run("attempt 4")
    assert fifth.status == "BLOCKED_LOOP_DETECTED"
    assert "Similar loop detected" in fifth.reason
    assert fifth.session_tripped is True


def test_same_shape_reads_are_noted_never_halted():
    evaluator = GovernanceEvaluator()
    session = "session-fuzzy-reads"
    for offset in range(6):
        result = evaluator.evaluate_tool_call(
            ToolCallRequestDTO(
                session_id=session,
                developer_id="dev-test",
                project_name="Acme-Fuzzy",
                tool_name="view_file",
                action_type="FILE_READ",
                arguments={"path": "README.md", "offset": offset},
                budget_usd=100.00,
            )
        )
        assert result.status == "APPROVED", result
    assert evaluator.get_or_create_session(session).is_tripped is False
