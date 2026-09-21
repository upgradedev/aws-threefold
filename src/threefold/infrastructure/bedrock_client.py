"""Bedrock Converse API client for semantic architectural explanation.

Bedrock never decides anything here. The deterministic gates render the verdict;
this client only puts that verdict into words for a human reader. When the model
cannot be reached the caller is told so explicitly, through the returned source
label, rather than being handed a canned string dressed up as a model response.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional, Tuple
from threefold.application.dtos import EvaluationResultDTO, ToolCallRequestDTO
from threefold.domain.boundary_guard import redact_secrets

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_REGION = "eu-west-1"

SOURCE_BEDROCK = "bedrock"
SOURCE_FALLBACK = "deterministic_fallback"

# How long a verdict may wait for its sentence. The default botocore client
# waits 60 seconds to read and retries three times, which put a refusal behind
# minutes of a model that was not answering. The explanation is decoration on a
# decision already made, so one short attempt and then the labelled fallback.
CLIENT_TIMEOUTS = {
    "connect_timeout": 1,
    "read_timeout": 2.5,
    "retries": {"max_attempts": 1, "mode": "standard"},
}

# About 2 KB of the call's arguments is enough for a model to phrase a verdict,
# and it bounds what one request can send to a third party and be billed for.
MAX_PROMPT_ARGUMENTS_CHARS = 2048


def _client_config() -> Optional[Any]:
    """The botocore Config carrying CLIENT_TIMEOUTS, or None where botocore is absent."""
    try:
        from botocore.config import Config
    except Exception:  # pragma: no cover - the Lambda runtime always ships botocore
        return None
    return Config(**CLIENT_TIMEOUTS)


def prompt_safe(text: str, limit: int = MAX_PROMPT_ARGUMENTS_CHARS) -> str:
    """Redacts credentials, then truncates, in that order.

    Truncating first could cut a credential in half, and half a key no longer
    matches the pattern that would have redacted it, so its prefix would leave.
    """
    redacted = redact_secrets(text or "")
    if len(redacted) <= limit:
        return redacted
    return redacted[:limit] + f" [truncated, {len(redacted) - limit} more characters not sent]"


class BedrockGovernanceClient:
    """Explains a governance verdict using Amazon Bedrock, or says it could not."""

    def __init__(
        self,
        model_id: Optional[str] = None,
        region_name: Optional[str] = None,
        boto3_session: Optional[Any] = None,
        max_calls_per_container: int = 200,
    ) -> None:
        self.model_id = model_id or os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
        # AWS_REGION is populated by the Lambda runtime and is reserved, so it is
        # read here rather than declared in the function's environment variables.
        self.region_name = region_name or os.getenv("AWS_REGION", DEFAULT_REGION)
        self._offline = os.getenv("THREEFOLD_OFFLINE", "").lower() in ("1", "true", "yes")
        self.max_calls_per_container = max_calls_per_container
        self.calls_made = 0
        self.last_error: Optional[str] = None
        self._client = None

        if boto3_session is not None:
            self._client = boto3_session.client(
                "bedrock-runtime", region_name=self.region_name, config=_client_config()
            )
        elif not self._offline:
            try:
                import boto3
                self._client = boto3.client(
                    "bedrock-runtime", region_name=self.region_name, config=_client_config()
                )
            except Exception as exc:
                self.last_error = str(exc)
                logger.info("Bedrock client unavailable, explanations will be deterministic: %s", exc)
                self._client = None

    def describe_availability(self) -> Tuple[bool, str]:
        """Reports what this client has actually observed, for the readiness probe.

        No model is invoked here. A probe that called Converse on every readiness
        check would bill the account for being looked at.
        """
        if self._client is None:
            reason = self.last_error or "no client constructed"
            return False, f"No Bedrock runtime client in {self.region_name} ({reason})"
        if self.calls_made >= self.max_calls_per_container:
            return False, (
                f"This container reached its {self.max_calls_per_container} call cap; "
                "explanations are deterministic until it is recycled"
            )
        if self.last_error:
            return False, f"Last Converse call to {self.model_id} failed: {self.last_error}"
        if self.calls_made == 0:
            return True, (
                f"Client bound to {self.model_id} in {self.region_name}; "
                "no call made yet in this container"
            )
        return True, (
            f"{self.calls_made} successful Converse call(s) to {self.model_id} "
            f"in {self.region_name} from this container"
        )

    def review_agent_action(
        self,
        request: ToolCallRequestDTO,
        evaluation: EvaluationResultDTO,
    ) -> Tuple[str, str]:
        """Returns the explanation and the source that produced it.

        The source is either ``bedrock`` or ``deterministic_fallback``. Callers
        surface it verbatim so a reader can tell which one they are looking at.
        """
        if self._client is None:
            return self._deterministic_explanation(request, evaluation), SOURCE_FALLBACK

        if self.calls_made >= self.max_calls_per_container:
            logger.info("Per-container Bedrock call cap reached; using deterministic explanation")
            return self._deterministic_explanation(request, evaluation), SOURCE_FALLBACK

        system_prompt = (
            "You are Threefold, a governance sidecar for autonomous coding agents. "
            "A deterministic gate has already decided this call. Never contradict it. "
            "Explain the decision to a human engineer in at most two sentences."
        )

        # Nothing leaves for the model that the ledger would not keep: no
        # developer, credentials redacted, and the arguments cut to about 2 KB.
        # The reason is redacted too, because a protected-path refusal quotes
        # the command it refused.
        user_content = (
            f"Project: {prompt_safe(str(request.project_name), 120)}\n"
            f"Tool requested: {prompt_safe(str(request.tool_name), 120)} "
            f"({prompt_safe(str(request.action_type), 40)})\n"
            f"Arguments: {prompt_safe(json.dumps(request.arguments, default=str))}\n"
            f"Deterministic verdict: {evaluation.status}\n"
            f"Deterministic reason: {prompt_safe(evaluation.reason or '', 600)}\n"
            f"Session cost so far: ${evaluation.current_session_cost_usd:.4f}\n\n"
            "Explain why this decision protects the developer, in at most two sentences."
        )

        try:
            response = self._client.converse(
                modelId=self.model_id,
                messages=[{"role": "user", "content": [{"text": user_content}]}],
                system=[{"text": system_prompt}],
                inferenceConfig={"maxTokens": 256, "temperature": 0.2},
            )
            text = response["output"]["message"]["content"][0]["text"].strip()
            self.calls_made += 1
            self.last_error = None
            return text, SOURCE_BEDROCK
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("Bedrock Converse call failed: %s. Falling back.", exc)
            return self._deterministic_explanation(request, evaluation), SOURCE_FALLBACK

    def _deterministic_explanation(
        self,
        request: ToolCallRequestDTO,
        evaluation: EvaluationResultDTO,
    ) -> str:
        """Plain restatement of the gate's own decision. No model was involved."""
        status = evaluation.status
        tool = request.tool_name
        cost = evaluation.current_session_cost_usd

        if status == "APPROVED":
            return (
                f"Tool '{tool}' cleared every gate: no credential in its arguments, no protected "
                f"path, no repeated call, and the session is still inside budget at ${cost:.4f}."
            )
        if status == "BLOCKED_SECRET_DETECTED":
            return (
                f"Tool '{tool}' was stopped because its arguments carried something shaped like a "
                "credential. The call never left this process."
            )
        if status == "BLOCKED_LOOP_DETECTED":
            return (
                f"Tool '{tool}' was called with identical arguments once too often, so the session "
                f"was halted at ${cost:.4f} rather than paying for the same answer again."
            )
        if status == "BLOCKED_BOUNDARY_VIOLATION":
            return (
                f"Tool '{tool}' targeted a protected path or crossed an architectural layer it is "
                "not allowed to touch, so the call was refused."
            )
        if status == "BLOCKED_CIRCUIT_BREAKER":
            return (
                f"The session reached its spending limit at ${cost:.4f}, so '{tool}' was refused "
                "and the session is frozen."
            )
        return f"Tool '{tool}' was halted: {evaluation.reason}"
