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
        project_name="PaymentService",
        tool_name="view_file",
        action_type="FILE_READ",
        arguments={"AbsolutePath": "/src/pay.py"},
    )
    # Call 1 & 2 succeed
    evaluator.evaluate_tool_call(req)
    evaluator.evaluate_tool_call(req)
    # Call 3 triggers loop detection
    res3 = evaluator.evaluate_tool_call(req)
    assert res3.status == "BLOCKED_LOOP_DETECTED"
    assert res3.session_tripped is True


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
    cert = AuditIssuer.issue_certificate(session, [res])

    assert cert.verdict_status == "COMPLIANT_APPROVED"
    assert cert.all_passed is True
    assert len(cert.sha256_fingerprint) == 64
    assert cert.certificate_id.startswith("CERT-TF-")
