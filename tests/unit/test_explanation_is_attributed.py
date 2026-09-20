"""Every explanation must say what produced it.

A deterministic sentence presented as a model response is a fabrication, and it
is the kind a reader cannot detect. So the reviewer returns a source label
alongside the text, and these tests pin that the label tracks reality.
"""
from __future__ import annotations

from threefold.application.bedrock_reviewer import BedrockArchitecturalReviewer
from threefold.application.dtos import EvaluationResultDTO, ToolCallRequestDTO
from threefold.infrastructure.bedrock_client import (
    SOURCE_BEDROCK,
    SOURCE_FALLBACK,
    BedrockGovernanceClient,
)


def _request() -> ToolCallRequestDTO:
    return ToolCallRequestDTO(
        session_id="session-explain-1",
        developer_id="dev-explain",
        project_name="ExplainService",
        tool_name="write_file",
        action_type="FILE_WRITE",
        arguments={"path": "/src/app.py"},
        projected_input_tokens=100,
        projected_output_tokens=50,
        budget_usd=5.00,
    )


def _evaluation(status: str = "APPROVED") -> EvaluationResultDTO:
    return EvaluationResultDTO(
        verdict_id="VERDICT-TEST",
        session_id="session-explain-1",
        status=status,
        risk_level="LOW",
        reason="All deterministic governance invariants satisfied",
        rule_evaluations={},
        current_session_cost_usd=0.1234,
        session_tripped=False,
        proof_hash="0" * 64,
    )


class _StubConverse:
    """Stands in for bedrock-runtime, so no network call is made."""

    def __init__(self, text: str = "The call was allowed because nothing looked dangerous."):
        self.text = text
        self.calls = 0

    def converse(self, **kwargs):
        self.calls += 1
        return {"output": {"message": {"content": [{"text": self.text}]}}}


class _FailingConverse:
    def converse(self, **kwargs):
        raise RuntimeError("model unavailable in this region")


def test_no_client_is_labelled_as_fallback_not_as_bedrock() -> None:
    reviewer = BedrockArchitecturalReviewer(BedrockGovernanceClient(boto3_session=None))
    text, source = reviewer.review_action(_request(), _evaluation())
    assert source == SOURCE_FALLBACK
    assert "Bedrock" not in text, "A deterministic sentence must not claim to be a model response"


def test_a_real_model_answer_is_labelled_bedrock_and_returned_verbatim() -> None:
    client = BedrockGovernanceClient(model_id="test-model", region_name="eu-west-1")
    stub = _StubConverse()
    client._client = stub
    text, source = BedrockArchitecturalReviewer(client).review_action(_request(), _evaluation())
    assert source == SOURCE_BEDROCK
    assert text == stub.text
    assert stub.calls == 1


def test_a_failed_call_falls_back_and_says_so() -> None:
    client = BedrockGovernanceClient(model_id="test-model", region_name="eu-west-1")
    client._client = _FailingConverse()
    text, source = BedrockArchitecturalReviewer(client).review_action(_request(), _evaluation())
    assert source == SOURCE_FALLBACK
    assert "Bedrock" not in text
    ok, detail = client.describe_availability()
    assert ok is False
    assert "model unavailable" in detail


def test_the_per_container_cap_stops_calling_and_reports_why() -> None:
    client = BedrockGovernanceClient(
        model_id="test-model", region_name="eu-west-1", max_calls_per_container=1
    )
    stub = _StubConverse()
    client._client = stub

    _, first_source = client.review_agent_action(_request(), _evaluation())
    _, second_source = client.review_agent_action(_request(), _evaluation())

    assert first_source == SOURCE_BEDROCK
    assert second_source == SOURCE_FALLBACK, "The cap must hold once it is reached"
    assert stub.calls == 1

    ok, detail = client.describe_availability()
    assert ok is False
    assert "cap" in detail


def test_readiness_never_invokes_the_model() -> None:
    """Probing readiness must not bill the account for being looked at."""
    client = BedrockGovernanceClient(model_id="test-model", region_name="eu-west-1")
    stub = _StubConverse()
    client._client = stub
    client.describe_availability()
    assert stub.calls == 0
