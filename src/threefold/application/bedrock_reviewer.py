"""Semantic architectural reviewer powered by Amazon Bedrock Claude 3.5 Sonnet."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from threefold.application.dtos import EvaluationResultDTO, ToolCallRequestDTO

logger = logging.getLogger(__name__)


class BedrockArchitecturalReviewer:
    """Invokes Amazon Bedrock to explain risk and architectural impact for human operators."""

    def __init__(self, bedrock_client: Optional[Any] = None) -> None:
        self.bedrock_client = bedrock_client

    def review_action(
        self,
        request: ToolCallRequestDTO,
        evaluation: EvaluationResultDTO,
    ) -> str:
        """Generates an architectural risk explanation using Bedrock or fallback."""
        if self.bedrock_client is not None:
            try:
                return self.bedrock_client.review_agent_action(request, evaluation)
            except Exception as exc:
                logger.warning("Bedrock invocation failed, falling back to deterministic explanation: %s", exc)

        return self._fallback_explanation(request, evaluation)

    def _fallback_explanation(
        self,
        request: ToolCallRequestDTO,
        evaluation: EvaluationResultDTO,
    ) -> str:
        """Deterministic heuristic explanation of the evaluation result."""
        status = evaluation.status
        tool = request.tool_name
        cost = evaluation.current_session_cost_usd

        if status == "BLOCKED_SECRET_DETECTED":
            return (
                f"[Threefold Security Alert] Tool '{tool}' was intercepted because sensitive "
                "credentials or API keys were detected in the argument payload. "
                "The payload was halted to prevent accidental commit or upstream leakage."
            )
        elif status == "BLOCKED_BOUNDARY_VIOLATION":
            return (
                f"[Threefold Architecture Alert] Tool '{tool}' attempted to access or modify "
                "a protected architectural path or violated Clean Architecture layer rules. "
                "Action blocked to prevent architectural drift."
            )
        elif status == "BLOCKED_LOOP_DETECTED":
            return (
                f"[Threefold Circuit Breaker] Agent thrashing detected for tool '{tool}'. "
                "The agent entered a recursive repetition cycle. Session execution halted "
                "to prevent unbounded token consumption."
            )
        elif status == "BLOCKED_CIRCUIT_BREAKER":
            return (
                f"[Threefold Cost Cap Alert] Session cost (${cost:.4f}) reached the allocated "
                f"budget limit of ${request.budget_usd:.2f}. Execution frozen to protect cloud budget."
            )
        else:
            return (
                f"[Threefold Verified] Tool '{tool}' complies with all architectural boundaries, "
                f"contains zero secrets, and is within budget. Session spend: ${cost:.4f}."
            )
