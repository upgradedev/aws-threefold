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
# The deterministic sentence by design rather than by failure: the call was
# approved, or the caller asked for no explanation. Kept apart from the
# fallback so a reader, and the readiness record, can tell "not asked" from
# "asked and the model did not answer".
SOURCE_DETERMINISTIC = "deterministic"


class BedrockArchitecturalReviewer:
    """Explains why a tool call was allowed or refused."""

    def __init__(self, bedrock_client: Optional[Any] = None) -> None:
        self.bedrock_client = bedrock_client

    def explain(
        self,
        request: ToolCallRequestDTO,
        evaluation: EvaluationResultDTO,
    ) -> Tuple[str, str]:
        """What every route calls: the model only for a refusal someone will read.

        Bedrock is kept off the enforcement path. An approval needs no
        persuading, and a hook that sends explain=false is sitting in front of
        a tool call waiting for a yes or a no; making it wait on a model as well
        put seconds of someone else's latency, and a bill, on every edit. Both
        get the deterministic sentence, labelled as such.
        """
        if evaluation.status == "APPROVED" or not getattr(request, "explain", True):
            return self._fallback_explanation(request, evaluation), SOURCE_DETERMINISTIC
        return self.review_action(request, evaluation)

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

        # Only a dry run approves a call whose invariants failed. Telling that
        # caller it "cleared every gate" would be the one sentence here that is
        # false.
        if status == "APPROVED" and not all((evaluation.rule_evaluations or {}).values()):
            watched = ", ".join(evaluation.observed_rules or []) or "a gate"
            return (
                f"Tool '{tool}' was let through only because this was a dry run: "
                f"{watched} would have refused it. Nothing was halted."
            )
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
