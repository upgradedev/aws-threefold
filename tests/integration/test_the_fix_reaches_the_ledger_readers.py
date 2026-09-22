"""A suggested fix leaves in the response and nowhere else; the ledger says only that one was sent.

Driven through `lambda_handler` with API Gateway v2 events, as deployed. The
response to the call carries the whole fix, because the caller sent the source
it rewrites. Every reader of the ledger, including the anonymous one on the
public stack, gets the row's `suggested_fix_kind` and `suggested_fix_validated`
and never the files, the steps or the summary.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import json
import uuid

import pytest

from test_app_support import fresh_project, get, hook_call
from threefold.application.evaluator import GovernanceEvaluator
from threefold.application.labels import public_row
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository
from threefold.interfaces import api_handlers
from threefold.interfaces.api_handlers import lambda_handler

DOMAIN_WRITE = {"file_path": "src/acme_stock/domain/acme_item.py", "content": "import boto3\n\n\nclass AcmeItem:\n    count = 0\n"}


@pytest.fixture(autouse=True)
def _a_ledger_of_its_own(monkeypatch):
    """Each test reads a ledger holding only its own calls, as the other ledger tests do."""
    monkeypatch.setattr(api_handlers, "_evaluator", GovernanceEvaluator(session_repo=DynamoDBSessionRepository()))


def _never_in(fix: dict, text: str) -> None:
    for write in fix.get("writes") or []:
        assert write["content"] not in text, f"A proposed file reached a reader: {write['path']}"
    for step in fix["steps"]:
        assert step not in text, "A step reached a reader"
    assert fix["summary"] not in text, "The summary reached a reader"


def test_the_response_carries_the_fix_and_every_ledger_reader_only_its_kind() -> None:
    project = fresh_project("Acme-Stock")
    answer = hook_call(project, f"{project}-cc", DOMAIN_WRITE)
    assert answer["status"] == "BLOCKED_BOUNDARY_VIOLATION"
    fix = answer["suggested_fix"]
    assert fix["kind"] == "layering" and fix["validated"] is True and fix["writes"]

    listed = get("/api/decisions", project=project)
    (row,) = listed["items"]
    assert (row["suggested_fix_kind"], row["suggested_fix_validated"]) == ("layering", True)
    assert "suggested_fix" not in row

    one = get("/api/decision", timestamp=row["timestamp"], verdict_id=row["verdict_id"])
    assert (one["decision"]["suggested_fix_kind"], one["decision"]["suggested_fix_validated"]) == ("layering", True)
    assert "suggested_fix" not in one["decision"]

    insights = get("/api/insights", days=1)
    for text in (json.dumps(listed), json.dumps(one), json.dumps(insights)):
        _never_in(fix, text)


def test_an_approved_call_leaves_a_row_with_no_fix_fields() -> None:
    project = fresh_project("Acme-Stock")
    answer = hook_call(project, f"{project}-ok", {"file_path": "src/acme_stock/app.py", "content": "x = 1\n"})
    assert answer["status"] == "APPROVED" and answer["suggested_fix"] is None
    (row,) = get("/api/decisions", project=project)["items"]
    assert not any("fix" in key for key in row)


def test_a_hook_in_observe_is_sent_no_fix_and_its_row_records_none(monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_HOOK_STAGE", "observe")
    project = fresh_project("Acme-Stock")
    answer = hook_call(project, f"{project}-obs", DOMAIN_WRITE)
    assert answer["status"] == "APPROVED" and answer["observed_rules"]
    assert answer["suggested_fix"] is None
    (row,) = get("/api/decisions", project=project)["items"]
    assert row["rule_key"] == "python-domain-stays-pure", "The observation is recorded as before"
    assert not any("fix" in key for key in row)


@pytest.mark.parametrize(
    "path, status, kind, validated",
    [
        ("/simulate-loop", "BLOCKED_LOOP_DETECTED", "loop", False),
        ("/simulate-secret", "BLOCKED_SECRET_DETECTED", "credential", True),
    ],
)
def test_the_demo_scenarios_carry_the_fix_the_refusal_needs(path, status, kind, validated) -> None:
    """The demo page's refusal panels read it from these two answers."""
    response = lambda_handler(
        {
            "rawPath": f"/prod{path}",
            "headers": {"Content-Type": "application/json"},
            "requestContext": {"http": {"method": "POST"}, "stage": "prod", "requestId": uuid.uuid4().hex},
        }
    )
    body = json.loads(response["body"])
    assert response["statusCode"] == 200 and body["status"] == status
    assert body["suggested_fix"]["kind"] == kind and body["suggested_fix"]["validated"] is validated
    assert "AKIA" + "IOSFODNN7EXAMPLE" not in response["body"]


def test_public_row_passes_the_two_fields_and_never_a_fix() -> None:
    row = {
        "verdict_id": "V-1",
        "project_name": "Acme-Stock",
        "developer_id": "c0ffee000001",
        "suggested_fix_kind": "layering",
        "suggested_fix_validated": True,
        "suggested_fix": {"summary": "move it", "writes": [{"path": "src/a.py", "content": "import acme\n"}]},
    }
    shown = public_row(row)
    assert (shown["suggested_fix_kind"], shown["suggested_fix_validated"]) == ("layering", True)
    assert "suggested_fix" not in shown and "import acme" not in json.dumps(shown)
    assert "suggested_fix" in row, "The caller's own row is not rewritten underneath it"
