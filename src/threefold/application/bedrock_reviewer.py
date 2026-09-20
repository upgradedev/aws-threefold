"""Turns a deterministic governance verdict into a sentence a human can read.

The reviewer never changes a verdict. It asks Amazon Bedrock to phrase the
decision, and when the model is not reachable it says so through the source
label it returns alongside the text.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Tuple
from threefold.application.dtos import EvaluationResultDTO, ToolCallRequestDTO

logger = logging.getLogger(__name__)

SOURCE_BEDROCK = "bedrock"
SOURCE_FALLBACK = "deterministic_fallback"


class BedrockArchitecturalReviewer:
    """Explains why a tool call was allowed or refused."""

    def __init__(self, bedrock_client: Optional[Any] = None) -> None:
        self.bedrock_client = bedrock_client

    def review_action(
        self,
        request: ToolCallRequestDTO,
        evaluation: EvaluationResultDTO,
    ) -> Tuple[str, str]:
        """Returns ``(explanation, source)`` where source names what produced it."""
        if self.bedrock_client is not None:
            try:
                return self.bedrock_client.review_agent_action(request, evaluation)
            except Exception as exc:
                logger.warning("Bedrock invocation failed, explaining deterministically: %s", exc)

        return self._fallback_explanation(request, evaluation), SOURCE_FALLBACK

    def _fallback_explanation(
        self,
        request: ToolCallRequestDTO,
        evaluation: EvaluationResultDTO,
    ) -> str:
        """Plain restatement of the gate's decision, used when no model answered."""
        status = evaluation.status
        tool = request.tool_name
        cost = evaluation.current_session_cost_usd

        if status == "BLOCKED_SECRET_DETECTED":
            return (
                f"Tool '{tool}' was stopped because its arguments carried something shaped "
                "like a credential. The call never left this process."
            )
        if status == "BLOCKED_BOUNDARY_VIOLATION":
            return (
                f"Tool '{tool}' targeted a protected path or crossed an architectural layer "
                "it is not allowed to touch, so the call was refused."
            )
        if status == "BLOCKED_LOOP_DETECTED":
            return (
                f"Tool '{tool}' was called with identical arguments once too often, so the "
                f"session was halted at ${cost:.4f} rather than paying for the same answer again."
            )
        if status == "BLOCKED_CIRCUIT_BREAKER":
            return (
                f"The session reached its spending limit of ${request.budget_usd:.2f} at "
                f"${cost:.4f}, so '{tool}' was refused and the session is frozen."
            )
        return (
            f"Tool '{tool}' cleared every gate: no credential in its arguments, no protected "
            f"path, no repeated call, and the session is still inside budget at ${cost:.4f}."
        )
