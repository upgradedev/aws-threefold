"""Domain exceptions for Threefold.

Enforces Clean Architecture & DDD principles by providing specific exception types
for autonomous agent policy violations, circuit breaker trips, and security breaches.
"""

from __future__ import annotations


class DomainException(Exception):
    """Base domain exception for Threefold business rule violations."""

    def __init__(self, message: str, code: str = "DOMAIN_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class BudgetExceededException(DomainException):
    """Raised when an agent tool invocation breaches cumulative or per-call budget."""

    def __init__(self, current_spend_usd: float, budget_usd: float) -> None:
        message = (
            f"Agent session expenditure (${current_spend_usd:.4f}) exceeds "
            f"configured budget limit (${budget_usd:.2f})"
        )
        super().__init__(message, code="BUDGET_EXCEEDED")
        self.current_spend_usd = current_spend_usd
        self.budget_usd = budget_usd


class SecurityBoundaryException(DomainException):
    """Raised when an agent attempts to access protected credentials or violate layers."""

    def __init__(self, target_resource: str, reason: str) -> None:
        message = f"Security boundary violation on resource '{target_resource}': {reason}"
        super().__init__(message, code="SECURITY_BOUNDARY_VIOLATION")
        self.target_resource = target_resource
        self.reason = reason


class LoopDetectedException(DomainException):
    """Raised when agent actuation enters a runaway monomorphic or ping-pong thrashing loop."""

    def __init__(self, pattern_signature: str, repetitions: int) -> None:
        message = f"Autonomous thrashing loop detected ({repetitions} repetitions of '{pattern_signature}')"
        super().__init__(message, code="LOOP_DETECTED")
        self.pattern_signature = pattern_signature
        self.repetitions = repetitions


class SessionFrozenException(DomainException):
    """Raised when actuation is attempted on a manually terminated/frozen agent session."""

    def __init__(self, session_id: str, frozen_by: str, reason: str) -> None:
        message = f"Session '{session_id}' was frozen by '{frozen_by}': {reason}"
        super().__init__(message, code="SESSION_FROZEN")
        self.session_id = session_id
        self.frozen_by = frozen_by
        self.reason = reason


class InvalidValueObjectException(DomainException):
    """Raised when a value object fails instantiation validation."""

    def __init__(self, vo_name: str, value: object, reason: str) -> None:
        message = f"Invalid value for {vo_name}: {value!r} ({reason})"
        super().__init__(message, code="INVALID_VALUE_OBJECT")
        self.vo_name = vo_name
        self.value = value


class EmptyAttestationException(DomainException):
    """Raised when a certificate is asked to attest to nothing.

    `all()` over an empty list is True, so a certificate issued over no
    evaluations used to come back COMPLIANT_APPROVED with all_passed set: a
    document that certifies nothing while reading as a clean bill of health.
    That is the one failure a governance artifact must not have.
    """

    def __init__(self, session_id: str, reason: str) -> None:
        message = f"Refusing to certify session '{session_id}': {reason}"
        super().__init__(message, code="EMPTY_ATTESTATION")
        self.session_id = session_id
        self.reason = reason
