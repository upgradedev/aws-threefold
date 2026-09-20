"""Data Transfer Objects for Threefold application layer."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional


@dataclass
class ToolCallRequestDTO:
    """Incoming request to evaluate an agent's intended tool invocation."""
    session_id: str
    developer_id: str
    project_name: str
    tool_name: str
    action_type: str
    arguments: Dict[str, Any]
    projected_input_tokens: int = 2000
    projected_output_tokens: int = 500
    budget_usd: float = 10.00


@dataclass
class EvaluationResultDTO:
    """Verdict returned to the agent proxy or CI/CD gate."""
    verdict_id: str
    session_id: str
    status: str
    risk_level: str
    reason: str
    rule_evaluations: Dict[str, bool]
    current_session_cost_usd: float
    session_tripped: bool
    proof_hash: str
    bedrock_explanation: Optional[str] = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GovernanceCertificateDTO:
    """Signed audit certificate summarizing compliant agent execution."""
    certificate_id: str
    session_id: str
    developer_id: str
    project_name: str
    verdict_status: str
    total_cost_usd: float
    total_tokens: int
    evaluations_count: int
    all_passed: bool
    sha256_fingerprint: str
    issued_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyConfigDTO:
    """Enterprise-level dynamic governance policy configuration."""
    max_single_call_usd: float = 1.00
    max_session_budget_usd: float = 10.00
    loop_history_window: int = 6
    monomorphic_repetition_threshold: int = 3
    blocked_patterns: List[str] = field(
        default_factory=lambda: [
            r"AKIA[0-9A-Z]{16}",
            r"aws_secret_access_key",
            r"\.env",
            r"\.pem$",
            r"id_rsa",
        ]
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EmergencyFreezeDTO:
    """Manual session termination request from security auditor."""
    session_id: str
    operator_name: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SubsystemHealthDTO:
    name: str
    status: str
    latency_ms: float
    details: str


@dataclass
class ReadinessResponseDTO:
    """Readiness probe result (/readyz)."""
    status: str
    service: str
    version: str
    timestamp_utc: str
    subsystems: List[SubsystemHealthDTO]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
