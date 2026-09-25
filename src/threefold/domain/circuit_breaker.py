"""Cost circuit breaker and token price calculation engine."""
from __future__ import annotations

from typing import Dict, Optional, Tuple
from threefold.domain.models import AgentSession, TokenUsage


class TokenCostCalculator:
    """Calculates exact dollar cost using tiered model rates."""

    # Published rates per 1 million tokens for the models a governed agent typically runs.
    DEFAULT_RATES: Dict[str, Dict[str, float]] = {
        "eu.anthropic.claude-haiku-4-5-20251001-v1:0": {
            "input_per_m": 1.00,
            "output_per_m": 5.00,
        },
        "eu.anthropic.claude-sonnet-4-5-20250929-v1:0": {
            "input_per_m": 3.00,
            "output_per_m": 15.00,
        },
        "default": {
            "input_per_m": 3.00,
            "output_per_m": 15.00,
        },
    }

    @classmethod
    def _rates_for(cls, model_id: str) -> Dict[str, float]:
        """The per-million-token rates for a model id, defaulting honestly.

        An exact table hit wins; otherwise a Haiku id in any naming — a dated
        build, a regional prefix, an inference profile — takes the Haiku row.
        Everything else is priced at the default Sonnet-class rate, which is
        what every call cost before model ids were plumbed. No rate is
        invented for a model whose price is not in the table.
        """
        lowered = (model_id or "").lower()
        for key, rates in cls.DEFAULT_RATES.items():
            if lowered == key.lower():
                return rates
        if "haiku" in lowered:
            return cls.DEFAULT_RATES["eu.anthropic.claude-haiku-4-5-20251001-v1:0"]
        return cls.DEFAULT_RATES["default"]

    @classmethod
    def calculate(
        cls,
        input_tokens: int,
        output_tokens: int,
        model_id: str = "default",
    ) -> TokenUsage:
        rates = cls._rates_for(model_id)
        input_cost = (input_tokens / 1_000_000.0) * rates["input_per_m"]
        output_cost = (output_tokens / 1_000_000.0) * rates["output_per_m"]
        total_cost = round(input_cost + output_cost, 6)
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=total_cost,
            model_id=model_id or "default",
        )


class CostCircuitBreaker:
    """Deterministic circuit breaker that halts runaway agent token consumption."""

    def __init__(
        self,
        max_single_invocation_cost: float = 2.50,
        hard_limit_buffer: float = 0.05,
        max_session_budget_usd: Optional[float] = None,
    ) -> None:
        self.max_single_invocation_cost = max_single_invocation_cost
        self.hard_limit_buffer = hard_limit_buffer
        self.max_session_budget_usd = max_session_budget_usd

    def evaluate_cost_risk(
        self,
        session: AgentSession,
        projected_usage: TokenUsage,
    ) -> Tuple[bool, str]:
        """Evaluates whether an upcoming token expenditure exceeds safety limits.

        Returns (is_permitted, failure_reason).
        """
        # 1. Check if session was already tripped
        if session.is_tripped:
            return False, f"Session already tripped: {session.trip_reason}"

        # 2. Check single invocation cost spike
        if projected_usage.cost_usd > self.max_single_invocation_cost:
            reason = (
                f"Single invocation cost ${projected_usage.cost_usd:.4f} exceeds "
                f"maximum safety cap of ${self.max_single_invocation_cost:.2f}"
            )
            session.trip_circuit_breaker(reason)
            return False, reason

        # 3. Check cumulative budget breach
        new_total_cost = round(session.total_cost_usd + projected_usage.cost_usd, 4)
        budget_ceiling = round(session.budget_usd * (1.0 + self.hard_limit_buffer), 4)

        if new_total_cost > budget_ceiling:
            reason = (
                f"Projected session cost ${new_total_cost:.4f} exceeds "
                f"allocated budget limit of ${session.budget_usd:.2f}"
            )
            session.trip_circuit_breaker(reason)
            return False, reason

        # 4. Check the operator's policy ceiling, where one is set. It binds
        # every session regardless of the budget the caller declared, so a
        # caller that sends itself a generous budget is still held to it.
        if self.max_session_budget_usd is not None and new_total_cost > self.max_session_budget_usd:
            reason = (
                f"Projected session cost ${new_total_cost:.4f} exceeds "
                f"the policy session ceiling of ${self.max_session_budget_usd:.2f}"
            )
            session.trip_circuit_breaker(reason)
            return False, reason

        return True, "Cost within permissible limits"
