"""Deterministic governance evaluator coordinating all safety gates."""
from __future__ import annotations

import datetime
import logging
import time
from typing import Any, Dict, Optional
from threefold.domain.models import (
    AgentSession,
    GovernanceVerdict,
    RiskLevel,
    ToolActionType,
    ToolInvocation,
    VerdictStatus,
)
from threefold.domain.circuit_breaker import CostCircuitBreaker, TokenCostCalculator
from threefold.domain.loop_detector import LoopDetector
from threefold.domain.boundary_guard import (
    ArchitecturalBoundaryGuard,
    describe_target,
    redact_secrets,
)
from threefold.application.dtos import (
    EvaluationResultDTO,
    PolicyConfigDTO,
    ReadinessResponseDTO,
    SubsystemHealthDTO,
    ToolCallRequestDTO,
)
from threefold.domain.events import (
    ArchitecturalBoundaryViolatedEvent,
    DomainEventPublisher,
    LoopDetectedEvent,
    SecretLeakInterceptedEvent,
)
from threefold.infrastructure.dynamo_repo import SessionConflictError

logger = logging.getLogger(__name__)


class GovernanceEvaluator:
    """Evaluates agent tool requests against deterministic safety and cost invariants."""

    def __init__(
        self,
        cost_breaker: Optional[CostCircuitBreaker] = None,
        loop_detector: Optional[LoopDetector] = None,
        session_repo: Optional[Any] = None,
        policy_config: Optional[PolicyConfigDTO] = None,
    ) -> None:
        # The gates are built from the policy rather than from their own defaults.
        # They used to disagree: the policy reported a $1.00 single-call cap while a
        # freshly built breaker enforced $2.50, so /policy/config described a limit
        # that no call was ever measured against until someone happened to POST a
        # policy. A gate a caller supplies is left exactly as it was supplied.
        self.policy_config = policy_config or PolicyConfigDTO()
        self.cost_breaker = cost_breaker or CostCircuitBreaker(
            max_single_invocation_cost=self.policy_config.max_single_call_usd
        )
        self.loop_detector = loop_detector or LoopDetector(
            repetition_threshold=self.policy_config.monomorphic_repetition_threshold
        )
        if session_repo is not None:
            self.session_repo = session_repo
        else:
            from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository
            self.session_repo = DynamoDBSessionRepository()
        if policy_config is None:
            self._adopt_saved_policy()

    def _adopt_saved_policy(self) -> None:
        """Applies the stored policy, so a cold container does not start on defaults."""
        loader = getattr(self.session_repo, "load_policy", None)
        if loader is None:
            return
        try:
            saved = loader()
        except Exception as exc:  # pragma: no cover - storage is best effort
            logger.warning("Could not read the saved policy: %s", exc)
            return
        if not saved:
            return
        try:
            self._apply(
                PolicyConfigDTO(
                    max_single_call_usd=float(saved.get("max_single_call_usd", 1.00)),
                    max_session_budget_usd=float(saved.get("max_session_budget_usd", 10.00)),
                    loop_history_window=int(saved.get("loop_history_window", 6)),
                    monomorphic_repetition_threshold=int(
                        saved.get("monomorphic_repetition_threshold", 3)
                    ),
                )
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Saved policy was unreadable, keeping defaults: %s", exc)

    def _apply(self, config: PolicyConfigDTO) -> None:
        self.policy_config = config
        self.cost_breaker.max_single_invocation_cost = config.max_single_call_usd
        self.loop_detector.repetition_threshold = config.monomorphic_repetition_threshold

    def update_policy(self, config: PolicyConfigDTO) -> None:
        """Applies a policy and stores it.

        A setting that lives only in the memory of whichever container answered
        is not a setting. It would apply to some requests and not others, which
        is worse than having no setting at all.
        """
        self._apply(config)
        saver = getattr(self.session_repo, "save_policy", None)
        if saver is not None:
            try:
                saver(config.to_dict())
            except Exception as exc:  # pragma: no cover - storage is best effort
                logger.warning("Could not persist the policy: %s", exc)

    def list_sessions(self, limit: int = 50):
        """Recent sessions for the dashboard."""
        lister = getattr(self.session_repo, "list_sessions", None)
        return lister(limit=limit) if lister else []

    def list_decisions(self, days: int = 7, limit: int = 1000):
        """Every decision in a window, for the console that reports on them."""
        lister = getattr(self.session_repo, "list_decisions", None)
        return lister(days=days, limit=limit) if lister else []

    def terminate_session(self, session_id: str, operator_name: str, reason: str) -> AgentSession:
        """Manual enterprise kill-switch to immediately freeze an agent session."""
        session = self.get_or_create_session(session_id)
        session.terminate_manually(operator=operator_name, reason=reason)
        # An operator may halt a session that has already tripped itself, so this
        # write deliberately overrides the terminal-state guard.
        self.session_repo.save_session(session, force=True)
        return session

    def check_readiness(self, bedrock_client: Optional[Any] = None) -> ReadinessResponseDTO:
        """Readiness probe reporting what each dependency has actually shown.

        The session store is exercised at call time with a real read. The model
        is not: invoking one on every readiness check would bill the account for
        being looked at, so that subsystem reports the client's own record and
        is healthy on a container that has not called it yet. The detail string
        says which of the two a reader is looking at, and this docstring used to
        claim both were measured.
        """
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        subsystems = []

        started = time.perf_counter()
        probe = getattr(self.session_repo, "probe", None)
        if probe is None:
            store_ok, store_detail = False, "Repository does not expose a read probe"
        else:
            store_ok, store_detail = probe()
        subsystems.append(
            SubsystemHealthDTO(
                name="SessionStore",
                status="HEALTHY" if store_ok else "DEGRADED",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                details=store_detail,
            )
        )

        started = time.perf_counter()
        if bedrock_client is None:
            model_ok, model_detail = False, "No Bedrock client bound to this evaluator"
        else:
            model_ok, model_detail = bedrock_client.describe_availability()
        subsystems.append(
            SubsystemHealthDTO(
                name="BedrockExplanationModel",
                status="HEALTHY" if model_ok else "DEGRADED",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                details=model_detail,
            )
        )

        overall = "READY" if all(s.status == "HEALTHY" for s in subsystems) else "DEGRADED"
        return ReadinessResponseDTO(
            status=overall,
            service="Threefold",
            version="1.0.0",
            timestamp_utc=now_iso,
            subsystems=subsystems,
        )

    def get_or_create_session(
        self,
        session_id: str,
        developer_id: str = "dev-default",
        project_name: str = "Acme-Core",
        budget_usd: float = 10.00,
    ) -> AgentSession:
        """Loads the session from durable storage, or creates it.

        There is deliberately no in-process session cache. A warm Lambda
        container holding its own copy would keep approving calls on a session
        that another container has already tripped.
        """
        persistent_session = self.session_repo.get_session(session_id)
        if persistent_session is not None:
            return persistent_session

        new_session = AgentSession(
            session_id=session_id,
            developer_id=developer_id,
            project_name=project_name,
            budget_usd=budget_usd,
        )
        self.session_repo.save_session(new_session)
        return new_session

    def evaluate_tool_call(self, request: ToolCallRequestDTO) -> EvaluationResultDTO:
        """Evaluates a tool call and records the decision.

        The recording is here rather than inside the gates because a refusal used
        to leave no trace at all: the gates return early, so only approved calls
        reached the session write. Nothing could answer which rule refused what,
        for whom, last week, which is the only question a platform owner has.
        """
        result = self._decide(request)
        self._record_decision(request, result)
        return result

    def _record_decision(self, request: ToolCallRequestDTO, result: EvaluationResultDTO) -> None:
        """Appends one row to the decision ledger, best effort.

        Deliberately narrow: the tool, the rule, who and where, and a short
        descriptor of the target. Never the arguments and never file content.
        The service already sees those; it does not need to keep them, and a
        ledger that stored a refused secret would be the joke that writes itself.
        """
        recorder = getattr(self.session_repo, "record_decision", None)
        if recorder is None:
            return
        try:
            recorder(
                {
                    "verdict_id": result.verdict_id,
                    "timestamp": result.timestamp,
                    "session_id": request.session_id,
                    "developer_id": request.developer_id,
                    "project_name": request.project_name,
                    "tool_name": request.tool_name,
                    "action_type": str(request.action_type),
                    "status": result.status,
                    "rule": self._rule_that_fired(result),
                    # The reason distinguishes a layer being crossed from a
                    # credential store being reached. Both fail the same
                    # invariant and a reader acts on them differently.
                    "reason": redact_secrets(result.reason or "")[:240],
                    "target": describe_target(request),
                    "cost_usd": result.current_session_cost_usd,
                }
            )
        except Exception as exc:  # pragma: no cover - the ledger must not break a verdict
            logger.warning("Could not record the decision: %s", exc)

    @staticmethod
    def _rule_that_fired(result: EvaluationResultDTO) -> str:
        """Names the gate that refused, or NONE when every gate passed."""
        for rule, passed in (result.rule_evaluations or {}).items():
            if not passed:
                return rule
        return "NONE"

    def _decide(self, request: ToolCallRequestDTO) -> EvaluationResultDTO:
        """Evaluates tool invocation against all deterministic safety gates."""
        session = self.get_or_create_session(
            session_id=request.session_id,
            developer_id=request.developer_id,
            project_name=request.project_name,
            budget_usd=request.budget_usd,
        )

        rule_evaluations: Dict[str, bool] = {
            "SECRET_LEAKAGE_FREE": True,
            "ARCHITECTURAL_BOUNDARY_SAFE": True,
            "LOOP_THRASHING_FREE": True,
            "BUDGET_CIRCUIT_BREAKER_SAFE": True,
        }

        # Pre-check: has the session already been frozen or tripped?
        if session.is_tripped:
            rule_evaluations["BUDGET_CIRCUIT_BREAKER_SAFE"] = False
            verdict = GovernanceVerdict.create(
                session_id=session.session_id,
                status=VerdictStatus.BLOCKED_CIRCUIT_BREAKER,
                risk_level=RiskLevel.CRITICAL,
                reason=f"Session execution frozen: {session.trip_reason}",
                rule_evaluations=rule_evaluations,
            )
            return self._to_dto(session, verdict)

        try:
            action_type = ToolActionType(request.action_type)
        except ValueError:
            action_type = ToolActionType.UNKNOWN

        invocation = ToolInvocation(
            tool_name=request.tool_name,
            action_type=action_type,
            arguments=request.arguments,
        )

        # Gate 1: Check for Secrets & Credentials
        is_boundary_safe, boundary_reason = ArchitecturalBoundaryGuard.evaluate_tool_boundary(invocation)
        if not is_boundary_safe:
            if "Sensitive credential detected" in boundary_reason:
                rule_evaluations["SECRET_LEAKAGE_FREE"] = False
                DomainEventPublisher.publish(
                    SecretLeakInterceptedEvent.create(
                        event_type="SECRET_LEAK_INTERCEPTED",
                        aggregate_id=session.session_id,
                        payload={"tool": request.tool_name, "reason": boundary_reason},
                    )
                )
                verdict = GovernanceVerdict.create(
                    session_id=session.session_id,
                    status=VerdictStatus.BLOCKED_SECRET_DETECTED,
                    risk_level=RiskLevel.CRITICAL,
                    reason=boundary_reason,
                    rule_evaluations=rule_evaluations,
                )
                return self._to_dto(session, verdict)
            else:
                rule_evaluations["ARCHITECTURAL_BOUNDARY_SAFE"] = False
                DomainEventPublisher.publish(
                    ArchitecturalBoundaryViolatedEvent.create(
                        event_type="ARCHITECTURAL_BOUNDARY_VIOLATED",
                        aggregate_id=session.session_id,
                        payload={"tool": request.tool_name, "reason": boundary_reason},
                    )
                )
                verdict = GovernanceVerdict.create(
                    session_id=session.session_id,
                    status=VerdictStatus.BLOCKED_BOUNDARY_VIOLATION,
                    risk_level=RiskLevel.HIGH,
                    reason=boundary_reason,
                    rule_evaluations=rule_evaluations,
                )
                return self._to_dto(session, verdict)

        # Gate 2: Check for Loop & Thrashing
        is_loop_free, loop_reason = self.loop_detector.evaluate_loop_risk(session.history, invocation)
        if not is_loop_free:
            rule_evaluations["LOOP_THRASHING_FREE"] = False
            DomainEventPublisher.publish(
                LoopDetectedEvent.create(
                    event_type="LOOP_DETECTED",
                    aggregate_id=session.session_id,
                    payload={"tool": request.tool_name, "reason": loop_reason},
                )
            )
            session.trip_circuit_breaker(loop_reason)
            self._persist_halt(session)
            verdict = GovernanceVerdict.create(
                session_id=session.session_id,
                status=VerdictStatus.BLOCKED_LOOP_DETECTED,
                risk_level=RiskLevel.HIGH,
                reason=loop_reason,
                rule_evaluations=rule_evaluations,
            )
            return self._to_dto(session, verdict)

        # Gate 3: Check Cost & Budget Limits
        projected_usage = TokenCostCalculator.calculate(
            input_tokens=request.projected_input_tokens,
            output_tokens=request.projected_output_tokens,
        )
        is_cost_safe, cost_reason = self.cost_breaker.evaluate_cost_risk(session, projected_usage)
        if not is_cost_safe:
            rule_evaluations["BUDGET_CIRCUIT_BREAKER_SAFE"] = False
            # evaluate_cost_risk trips the session on a breach, so the halt is durable too.
            self._persist_halt(session)
            verdict = GovernanceVerdict.create(
                session_id=session.session_id,
                status=VerdictStatus.BLOCKED_CIRCUIT_BREAKER,
                risk_level=RiskLevel.CRITICAL,
                reason=cost_reason,
                rule_evaluations=rule_evaluations,
            )
            return self._to_dto(session, verdict)

        # All gates passed. The write happens before the verdict is issued: if a
        # concurrent container has already halted this session, the write is
        # refused and the caller is told the session is blocked, not approved.
        session.record_usage(projected_usage)
        session.record_tool_call(invocation)
        try:
            self.session_repo.save_session(session)
        except SessionConflictError as exc:
            logger.warning("Refusing to approve a session halted elsewhere: %s", exc)
            stored = self.session_repo.get_session(session.session_id)
            rule_evaluations["BUDGET_CIRCUIT_BREAKER_SAFE"] = False
            blocked_reason = (
                stored.trip_reason
                if stored is not None and stored.trip_reason
                else "Session was halted by another worker"
            )
            verdict = GovernanceVerdict.create(
                session_id=session.session_id,
                status=VerdictStatus.BLOCKED_CIRCUIT_BREAKER,
                risk_level=RiskLevel.CRITICAL,
                reason=f"Session execution frozen: {blocked_reason}",
                rule_evaluations=rule_evaluations,
            )
            return self._to_dto(stored or session, verdict)

        verdict = GovernanceVerdict.create(
            session_id=session.session_id,
            status=VerdictStatus.APPROVED,
            risk_level=RiskLevel.LOW,
            reason="All deterministic governance invariants satisfied",
            rule_evaluations=rule_evaluations,
        )
        return self._to_dto(session, verdict)

    def _persist_halt(self, session: AgentSession) -> None:
        """Writes a halted session through, so every worker observes the trip.

        ``force`` is used because the session is already tripped in memory and the
        terminal-state guard would otherwise refuse the very write that records it.
        """
        try:
            self.session_repo.save_session(session, force=True)
        except Exception as exc:  # pragma: no cover - defensive, storage is best effort
            logger.error("Failed to persist circuit breaker halt: %s", exc)

    def _to_dto(self, session: AgentSession, verdict: GovernanceVerdict) -> EvaluationResultDTO:
        return EvaluationResultDTO(
            verdict_id=verdict.verdict_id,
            session_id=session.session_id,
            status=verdict.status.value,
            risk_level=verdict.risk_level.value,
            reason=verdict.reason,
            rule_evaluations=verdict.rule_evaluations,
            current_session_cost_usd=session.total_cost_usd,
            session_tripped=session.is_tripped,
            proof_hash=verdict.proof_hash,
            timestamp=verdict.timestamp,
        )
