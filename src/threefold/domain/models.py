"""Domain models and value objects for Threefold."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import secrets
from typing import Any, Dict, List, Optional


class VerdictStatus(str, Enum):
    """Status emitted by the governance evaluation."""
    APPROVED = "APPROVED"
    BLOCKED_CIRCUIT_BREAKER = "BLOCKED_CIRCUIT_BREAKER"
    BLOCKED_LOOP_DETECTED = "BLOCKED_LOOP_DETECTED"
    BLOCKED_BOUNDARY_VIOLATION = "BLOCKED_BOUNDARY_VIOLATION"
    BLOCKED_SECRET_DETECTED = "BLOCKED_SECRET_DETECTED"
    REQUIRES_HUMAN_APPROVAL = "REQUIRES_HUMAN_APPROVAL"


class RiskLevel(str, Enum):
    """Risk severity classification."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ToolActionType(str, Enum):
    """Categorization of agent tool invocation."""
    FILE_READ = "FILE_READ"
    FILE_WRITE = "FILE_WRITE"
    COMMAND_EXEC = "COMMAND_EXEC"
    WEB_SEARCH = "WEB_SEARCH"
    LLM_INVOCATION = "LLM_INVOCATION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TokenUsage:
    """Immutable value object representing token consumption and cost."""
    input_tokens: int
    output_tokens: int
    cost_usd: float

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ToolInvocation:
    """An attempted tool call by an autonomous coding agent."""
    tool_name: str
    action_type: ToolActionType
    arguments: Dict[str, Any]
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def canonical_signature(self) -> str:
        """Deterministic fingerprint of the tool name and sorted arguments."""
        canonical_args = json.dumps(self.arguments, sort_keys=True, default=str)
        payload = f"{self.tool_name}:{canonical_args}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class AgentSession:
    """Aggregate root tracking an autonomous agent work session."""
    session_id: str
    developer_id: str
    project_name: str
    budget_usd: float
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    history: List[ToolInvocation] = field(default_factory=list)
    is_tripped: bool = False
    trip_reason: Optional[str] = None
    is_terminated: bool = False
    terminated_by: Optional[str] = None
    termination_reason: Optional[str] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def record_usage(self, usage: TokenUsage) -> None:
        """Accumulates token counts and USD expenditure."""
        self.total_input_tokens += usage.input_tokens
        self.total_output_tokens += usage.output_tokens
        self.total_cost_usd = round(self.total_cost_usd + usage.cost_usd, 4)

    def record_tool_call(self, invocation: ToolInvocation) -> None:
        """Appends tool invocation to session audit history."""
        self.history.append(invocation)

    def trip_circuit_breaker(self, reason: str) -> None:
        """Locks session and forbids further agent actuation."""
        self.is_tripped = True
        self.trip_reason = reason

        from threefold.domain.events import CircuitBreakerTrippedEvent, DomainEventPublisher
        DomainEventPublisher.publish(
            CircuitBreakerTrippedEvent.create(
                event_type="CIRCUIT_BREAKER_TRIPPED",
                aggregate_id=self.session_id,
                payload={
                    "developer_id": self.developer_id,
                    "project_name": self.project_name,
                    "total_cost_usd": self.total_cost_usd,
                    "budget_usd": self.budget_usd,
                    "reason": reason,
                },
            )
        )

    def terminate_manually(self, operator: str, reason: str) -> None:
        """Enterprise emergency kill-switch to manually freeze a rogue session."""
        self.is_tripped = True
        self.trip_reason = f"MANUALLY_TERMINATED by {operator}: {reason}"
        self.is_terminated = True
        self.terminated_by = operator
        self.termination_reason = reason

        from threefold.domain.events import DomainEventPublisher, SessionTerminatedManuallyEvent
        DomainEventPublisher.publish(
            SessionTerminatedManuallyEvent.create(
                event_type="SESSION_TERMINATED_MANUALLY",
                aggregate_id=self.session_id,
                payload={
                    "operator": operator,
                    "reason": reason,
                    "total_cost_usd": self.total_cost_usd,
                },
            )
        )


@dataclass(frozen=True)
class PolicyRule:
    """Rule enforced by the governance engine."""
    rule_id: str
    name: str
    description: str
    is_blocking: bool = True


@dataclass(frozen=True)
class GovernanceVerdict:
    """Final decision rendered by the deterministic evaluation gate."""
    verdict_id: str
    session_id: str
    status: VerdictStatus
    risk_level: RiskLevel
    reason: str
    rule_evaluations: Dict[str, bool]
    proof_hash: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @classmethod
    def create(
        cls,
        session_id: str,
        status: VerdictStatus,
        risk_level: RiskLevel,
        reason: str,
        rule_evaluations: Dict[str, bool],
    ) -> GovernanceVerdict:
        timestamp = datetime.now(timezone.utc).isoformat()
        canonical = json.dumps(
            {
                "session_id": session_id,
                "status": status.value,
                "risk_level": risk_level.value,
                "reason": reason,
                "rule_evaluations": rule_evaluations,
                "timestamp": timestamp,
            },
            sort_keys=True,
        )
        proof_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        # The id is the proof salted with a random value, not the proof alone.
        # Two approvals in one session carry the same reason and invariants,
        # so on a clock that ticks every millisecond they shared a timestamp,
        # a proof and so an id, and the ledger, keyed by the id, kept one of
        # the two. The proof stays over the fields the caller is given, so it
        # still verifies from them.
        salted = hashlib.sha256(f"{proof_hash}:{secrets.token_hex(8)}".encode("utf-8")).hexdigest()
        verdict_id = f"VERDICT-{salted[:12].upper()}"
        return cls(
            verdict_id=verdict_id,
            session_id=session_id,
            status=status,
            risk_level=risk_level,
            reason=reason,
            rule_evaluations=rule_evaluations,
            proof_hash=proof_hash,
            timestamp=timestamp,
        )
