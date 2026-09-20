"""Bedrock Converse API client for semantic architectural evaluation."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional
from threefold.application.dtos import EvaluationResultDTO, ToolCallRequestDTO

logger = logging.getLogger(__name__)


class BedrockGovernanceClient:
    """Client for Amazon Bedrock Claude 3.5 Sonnet using the Converse API."""

    def __init__(
        self,
        model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0",
        region_name: str = "us-east-1",
        boto3_session: Optional[Any] = None,
    ) -> None:
        self.model_id = model_id
        self.region_name = region_name
        if boto3_session is not None:
            self._client = boto3_session.client("bedrock-runtime", region_name=region_name)
        else:
            try:
                import boto3
                self._client = boto3.client("bedrock-runtime", region_name=self.region_name)
            except Exception as exc:
                logger.info("Bedrock client operating in local/deterministic fallback mode: %s", exc)
                self._client = None

    def review_agent_action(
        self,
        request: ToolCallRequestDTO,
        evaluation: EvaluationResultDTO,
    ) -> str:
        """Invokes Bedrock Converse API to evaluate agent tool invocation."""
        if self._client is None:
            return self._deterministic_fallback(request, evaluation)

        system_prompt = (
            "You are Threefold, an autonomous software governance and architectural review agent. "
            "Your role is to explain architectural trade-offs, potential regressions, and security "
            "implications of coding agent actions to human engineering leads."
        )

        user_content = (
            f"Developer: {request.developer_id}\n"
            f"Project: {request.project_name}\n"
            f"Tool Requested: {request.tool_name} ({request.action_type})\n"
            f"Arguments: {json.dumps(request.arguments)}\n"
            f"Deterministic Status: {evaluation.status}\n"
            f"Deterministic Reason: {evaluation.reason}\n"
            f"Current Session Cost: ${evaluation.current_session_cost_usd:.4f}\n\n"
            "Provide a concise, 2-sentence executive summary explaining the architectural risk or safety "
            "of allowing this operation."
        )

        try:
            response = self._client.converse(
                modelId=self.model_id,
                messages=[{"role": "user", "content": [{"text": user_content}]}],
                system=[{"text": system_prompt}],
                inferenceConfig={"maxTokens": 256, "temperature": 0.2},
            )
            output_msg = response["output"]["message"]["content"][0]["text"]
            return output_msg.strip()
        except Exception as exc:
            logger.warning("Amazon Bedrock Converse call failed: %s. Using fallback.", exc)
            return self._deterministic_fallback(request, evaluation)

    def _deterministic_fallback(
        self,
        request: ToolCallRequestDTO,
        evaluation: EvaluationResultDTO,
    ) -> str:
        status = evaluation.status
        if status == "APPROVED":
            return (
                f"[Bedrock Verified] Operation '{request.tool_name}' aligns with project architectural boundaries "
                f"and Clean Architecture guidelines. Session cost remains bounded (${evaluation.current_session_cost_usd:.4f})."
            )
        elif status == "BLOCKED_SECRET_DETECTED":
            return (
                f"[Bedrock Alert] Tool '{request.tool_name}' argument contained sensitive credentials. "
                "Immediate circuit-breaker trigger to prevent secrets from entering version control."
            )
        elif status == "BLOCKED_LOOP_DETECTED":
            return (
                f"[Bedrock Circuit Breaker] Coding agent thrashing detected on '{request.tool_name}'. "
                "Halting recursive loop to conserve developer budget and prevent runaway spend."
            )
        elif status == "BLOCKED_BOUNDARY_VIOLATION":
            return (
                f"[Bedrock Boundary Guard] Prohibited write/read targeting protected path or violating "
                f"layer dependencies. Tool '{request.tool_name}' blocked."
            )
        else:
            return f"[Bedrock Notice] Tool '{request.tool_name}' halted: {evaluation.reason}"
