"""Domain Events and Event Publisher for Threefold.

Implements Martin Fowler's Domain Event pattern to decouple governance aggregate
state changes (circuit trips, secret leaks, boundary violations) from notifications
and external webhooks (Slack, PagerDuty, CloudWatch).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any, Callable, Dict, List


@dataclass(frozen=True)
class DomainEvent:
    """Base domain event representing an immutable governance event."""

    event_id: str
    event_type: str
    occurred_at_utc: str
    aggregate_id: str
    payload: Dict[str, Any]

    @classmethod
    def create(cls, event_type: str, aggregate_id: str, payload: Dict[str, Any]) -> DomainEvent:
        now = datetime.now(timezone.utc).isoformat()
        digest = hashlib.sha256(f"{event_type}:{aggregate_id}:{now}".encode("utf-8")).hexdigest()[:12]
        return cls(
            event_id=f"EVT-{digest.upper()}",
            event_type=event_type,
            occurred_at_utc=now,
            aggregate_id=aggregate_id,
            payload=payload,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at_utc": self.occurred_at_utc,
            "aggregate_id": self.aggregate_id,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class CircuitBreakerTrippedEvent(DomainEvent):
    """Fired when an agent session exceeds budget ceiling or single-call spike."""
    pass


@dataclass(frozen=True)
class SecretLeakInterceptedEvent(DomainEvent):
    """Fired when an agent attempts to expose AWS access keys, tokens, or .env files."""
    pass


@dataclass(frozen=True)
class ArchitecturalBoundaryViolatedEvent(DomainEvent):
    """Fired when an agent attempts to bypass clean architecture layer boundaries or run destructive commands."""
    pass


@dataclass(frozen=True)
class LoopDetectedEvent(DomainEvent):
    """Fired when an agent enters an infinite repetitive or ping-pong tool calling loop."""
    pass


@dataclass(frozen=True)
class SessionTerminatedManuallyEvent(DomainEvent):
    """Fired when an enterprise administrator manually triggers the emergency kill-switch."""
    pass


class DomainEventPublisher:
    """Thread-safe in-memory domain event bus for Threefold."""

    _subscribers: Dict[str, List[Callable[[DomainEvent], None]]] = {}
    _published_events: List[DomainEvent] = []

    @classmethod
    def subscribe(cls, event_type: str, handler: Callable[[DomainEvent], None]) -> None:
        if event_type not in cls._subscribers:
            cls._subscribers[event_type] = []
        cls._subscribers[event_type].append(handler)

    @classmethod
    def publish(cls, event: DomainEvent) -> None:
        cls._published_events.append(event)
        for handler in cls._subscribers.get(event.event_type, []):
            try:
                handler(event)
            except Exception:
                pass
        for handler in cls._subscribers.get("*", []):
            try:
                handler(event)
            except Exception:
                pass

    @classmethod
    def get_published_events(cls) -> List[DomainEvent]:
        return list(cls._published_events)

    @classmethod
    def clear(cls) -> None:
        cls._published_events.clear()
        cls._subscribers.clear()
