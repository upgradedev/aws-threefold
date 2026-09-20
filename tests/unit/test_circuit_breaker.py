"""Unit tests for TokenCostCalculator and CostCircuitBreaker."""
from __future__ import annotations

import pytest
from threefold.domain.models import AgentSession, TokenUsage
from threefold.domain.circuit_breaker import CostCircuitBreaker, TokenCostCalculator


def test_token_cost_calculation():
    # 1,000,000 input tokens at $3.00/M + 1,000,000 output tokens at $15.00/M = $18.00
    usage = TokenCostCalculator.calculate(1_000_000, 1_000_000)
    assert usage.cost_usd == 18.00
    assert usage.total_tokens == 2_000_000

    # 10,000 input ($0.03) + 2,000 output ($0.03) = $0.06
    usage_small = TokenCostCalculator.calculate(10_000, 2_000)
    assert usage_small.cost_usd == 0.06


def test_circuit_breaker_permits_within_budget():
    breaker = CostCircuitBreaker(max_single_invocation_cost=2.0)
    session = AgentSession(
        session_id="session-001",
        developer_id="dev-test",
        project_name="Test-Project",
        budget_usd=5.00,
    )
    usage = TokenCostCalculator.calculate(50_000, 10_000)  # ~$0.30
    is_safe, reason = breaker.evaluate_cost_risk(session, usage)
    assert is_safe is True
    assert "within permissible limits" in reason


def test_circuit_breaker_blocks_single_invocation_spike():
    breaker = CostCircuitBreaker(max_single_invocation_cost=1.0)
    session = AgentSession(
        session_id="session-002",
        developer_id="dev-test",
        project_name="Test-Project",
        budget_usd=10.00,
    )
    # Huge output payload costing > $1.00
    usage = TokenCostCalculator.calculate(100_000, 100_000)  # ~$1.80
    is_safe, reason = breaker.evaluate_cost_risk(session, usage)
    assert is_safe is False
    assert "exceeds maximum safety cap" in reason
    assert session.is_tripped is True


def test_circuit_breaker_trips_when_cumulative_budget_exceeded():
    breaker = CostCircuitBreaker(hard_limit_buffer=0.0)
    session = AgentSession(
        session_id="session-003",
        developer_id="dev-test",
        project_name="Test-Project",
        budget_usd=1.00,
        total_cost_usd=0.95,
    )
    usage = TokenCostCalculator.calculate(20_000, 5_000)  # ~$0.135
    is_safe, reason = breaker.evaluate_cost_risk(session, usage)
    assert is_safe is False
    assert "exceeds allocated budget limit" in reason
    assert session.is_tripped is True

    # Subsequent call must immediately fail
    subsequent_call, sub_reason = breaker.evaluate_cost_risk(session, TokenUsage(10, 10, 0.0001))
    assert subsequent_call is False
    assert "Session already tripped" in sub_reason
