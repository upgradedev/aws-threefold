"""Unit tests for Threefold Domain Value Objects and Domain Events.

Verifies DDD principles:
- Self-validating immutable value objects (USD, TokenCount, SessionId)
- Domain event publication and subscriber notification
- Custom domain exceptions
"""

from __future__ import annotations

import pytest

from threefold.domain.events import (
    CircuitBreakerTrippedEvent,
    DomainEventPublisher,
    LoopDetectedEvent,
    SecretLeakInterceptedEvent,
    SessionTerminatedManuallyEvent,
)
from threefold.domain.exceptions import (
    BudgetExceededException,
    InvalidValueObjectException,
    LoopDetectedException,
    SecurityBoundaryException,
)
from threefold.domain.value_objects import SessionId, TokenCount, USD


def test_usd_value_object_valid_and_rounding() -> None:
    cost = USD(0.045678)
    assert float(cost) == 0.045678
    assert cost.formatted == "$0.0457"
    assert str(cost) == "$0.0457"

    # Addition
    c2 = cost + 0.01
    assert float(c2) == 0.0557

    # Subtraction
    c3 = c2 - 0.1
    assert float(c3) == 0.0  # floors at 0.0


def test_usd_negative_value_rejected() -> None:
    with pytest.raises(InvalidValueObjectException) as exc:
        USD(-0.5)
    assert "cannot be negative" in str(exc.value)


def test_token_count_value_object() -> None:
    tokens = TokenCount(2500)
    assert int(tokens) == 2500
    assert str(tokens) == "2,500 tokens"

    with pytest.raises(InvalidValueObjectException):
        TokenCount(-10)


def test_session_id_value_object() -> None:
    sid = SessionId("sess-production-alpha-001")
    assert str(sid) == "sess-production-alpha-001"

    with pytest.raises(InvalidValueObjectException):
        SessionId("")
    with pytest.raises(InvalidValueObjectException):
        SessionId("   ")


def test_threefold_domain_event_publication() -> None:
    DomainEventPublisher.clear()
    events = []

    def handler(evt) -> None:
        events.append(evt)

    DomainEventPublisher.subscribe("CIRCUIT_BREAKER_TRIPPED", handler)

    trip_evt = CircuitBreakerTrippedEvent.create(
        event_type="CIRCUIT_BREAKER_TRIPPED",
        aggregate_id="sess-001",
        payload={"reason": "Budget cap reached", "total_cost_usd": 10.50},
    )
    DomainEventPublisher.publish(trip_evt)

    assert len(events) == 1
    assert events[0].aggregate_id == "sess-001"
    assert events[0].payload["total_cost_usd"] == 10.50
    DomainEventPublisher.clear()
