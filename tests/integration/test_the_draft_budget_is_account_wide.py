"""Drafting model calls are budgeted account-wide per UTC day, not per container.

A flood of drafts across N containers used to spend N times the 60 calls
each container allows, because the only bound lived in container memory.
Each draft now claims its two calls, the most one draft makes, out of one
daily account budget held in DynamoDB, claimed with a conditional write so
concurrent containers add rather than overwrite. Past the budget the route
answers 429 and no model call is made.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository
from threefold.interfaces import api_handlers as router
from threefold.interfaces import draft_routes


class _FakeTable:
    """A DynamoDB table that enforces the budget condition itself."""

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.updates: List[dict] = []
        self.fail_with: Any = None

    def update_item(self, **kwargs: Any) -> dict:
        self.updates.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        key = f"{kwargs['Key']['PK']}#{kwargs['Key']['SK']}"
        stored = self.items.setdefault(dict(kwargs["Key"]), {})
        have = int(stored.get("claimed", 0) or 0)
        calls = int(kwargs["ExpressionAttributeValues"][":calls"])
        remaining = int(kwargs["ExpressionAttributeValues"][":remaining"])
        assert "attribute_not_exists" in kwargs["ConditionExpression"], kwargs["ConditionExpression"]
        if have > remaining:
            error = type("ConditionalCheckFailedException", (Exception,), {})(
                "The conditional request failed"
            )
            error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}  # type: ignore[attr-defined]
            raise error
        stored["claimed"] = have + calls
        return {}


class _Resource:
    def __init__(self, table: _FakeTable) -> None:
        self._table = table

    def Table(self, _name: str) -> _FakeTable:
        return self._table


def _repo(table: _FakeTable | None = None) -> DynamoDBSessionRepository:
    if table is None:
        return DynamoDBSessionRepository()
    return DynamoDBSessionRepository(boto3_resource=_Resource(table))


# ------------------------------------------------------------------ the claim


def test_the_first_claim_of_the_day_succeeds() -> None:
    assert _repo().claim_draft_calls(2, 600, day="budget-day-001") is True


def test_claims_accumulate_until_the_cap() -> None:
    repo = _repo()
    assert repo.claim_draft_calls(2, 4, day="budget-day-002") is True
    assert repo.claim_draft_calls(2, 4, day="budget-day-002") is True
    assert repo.claim_draft_calls(2, 4, day="budget-day-002") is False


def test_a_denied_claim_counts_nothing() -> None:
    repo = _repo()
    assert repo.claim_draft_calls(2, 2, day="budget-day-003") is True
    assert repo.claim_draft_calls(2, 2, day="budget-day-003") is False
    assert repo.claim_draft_calls(1, 3, day="budget-day-003") is True


def test_a_claim_larger_than_the_whole_cap_is_denied_without_a_write() -> None:
    table = _FakeTable()
    assert _repo(table).claim_draft_calls(3, 2, day="budget-day-004") is False
    assert table.updates == []


def test_tomorrow_starts_unspent() -> None:
    repo = _repo()
    assert repo.claim_draft_calls(2, 2, day="budget-day-005") is True
    assert repo.claim_draft_calls(2, 2, day="budget-day-005") is False
    assert repo.claim_draft_calls(2, 2, day="budget-day-006") is True


def test_a_lost_condition_is_a_denial_not_an_error() -> None:
    table = _FakeTable()
    repo = _repo(table)
    assert repo.claim_draft_calls(2, 2, day="budget-day-007") is True
    assert repo.claim_draft_calls(2, 2, day="budget-day-007") is False
    assert len(table.updates) == 2


def test_a_storage_outage_falls_back_to_memory_and_still_bounds() -> None:
    table = _FakeTable()
    table.fail_with = ConnectionError("DynamoDB is down")
    repo = _repo(table)
    assert repo.claim_draft_calls(2, 2, day="budget-day-008") is True
    assert repo.claim_draft_calls(2, 2, day="budget-day-008") is False


def test_budget_rows_expire_two_days_out() -> None:
    repo = _repo()
    repo.claim_draft_calls(2, 600, day="budget-day-009")
    stored = repo._memory_store["DRAFTBUDGET#budget-day-009#ACCOUNT"]
    import time

    assert 0 < stored["ttl"] - time.time() <= 2 * 86400


# ------------------------------------------------------------------ the route


class _NoModel:
    """Stands where the model client would be, so a regression fails here instead of drafting."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError("A draft past the budget reached the model client")


class _ScriptedRuntime:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: List[dict] = []

    def converse(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": self.text}]}}, "stopReason": "end_turn"}


class _ScriptedClient:
    """Shaped like BedrockGovernanceClient: a runtime, a model id, a cap and its books."""

    def __init__(self, text: str) -> None:
        self._client = _ScriptedRuntime(text)
        self.model_id = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
        self.calls_made = 0
        self.max_calls_per_container = 60
        self.last_error = None


GOOD_RULE = json.dumps(
    {
        "id": "billing-domain-stays-pure",
        "description": "Billing domain classes may not reach persistence",
        "mode": "observe",
        "when_path_matches": ["**/billing/domain/**/*.java"],
        "forbid_imports": ["javax.persistence", "jakarta.persistence"],
        "allow_imports": ["java.util"],
    }
)
DESCRIPTION = "Billing domain classes may not reach persistence; java.util is fine."


def _draft(body: dict) -> tuple[int, dict]:
    response = router.lambda_handler(
        {
            "rawPath": "/prod/rules/draft",
            "headers": {"Content-Type": "application/json"},
            "requestContext": {"http": {"method": "POST", "sourceIp": "203.0.113.99"}, "stage": "prod"},
            "body": json.dumps(body),
        }
    )
    return response["statusCode"], json.loads(response["body"])


def test_a_draft_past_the_budget_is_answered_429_with_no_model_call(monkeypatch) -> None:
    monkeypatch.setattr(draft_routes, "_client", _NoModel())
    monkeypatch.setattr(draft_routes, "DRAFT_MODEL_CALLS_PER_DAY", 0)
    status, problem = _draft({"description": DESCRIPTION})
    assert status == 429
    assert problem["type"] == "urn:threefold:error:draft-budget-spent"
    assert problem["saved"] is False


def test_each_draft_claims_two_calls_and_the_third_is_refused(monkeypatch) -> None:
    """Two model calls at most per draft, so each draft claims two up front."""
    monkeypatch.setattr(draft_routes, "DRAFT_MODEL_CALLS_PER_DAY", 2)
    monkeypatch.setattr(draft_routes, "_client", _ScriptedClient(GOOD_RULE))
    status, _ = _draft({"description": DESCRIPTION})
    assert status == 200
    monkeypatch.setattr(draft_routes, "_client", _ScriptedClient(GOOD_RULE))
    status, problem = _draft({"description": DESCRIPTION})
    assert status == 429
    assert problem["type"] == "urn:threefold:error:draft-budget-spent"
