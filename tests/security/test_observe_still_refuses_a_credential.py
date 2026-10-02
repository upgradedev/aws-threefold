"""A project in Observe still refuses a credential on the service.

Observe watches every rule and refuses nothing it can stage, but the
credential gate is never staged: the hook refuses a key on the machine in
every mode, and the service approving the same key from a caller without
the hook (a CI job, a script, a hook that failed to scan) contradicted that.
A dry run is still only recorded.
"""
from __future__ import annotations

import json

import pytest

from threefold.interfaces.api_handlers import lambda_handler

KEY = "AKIAQWERTYUIOPASDFGH"


def _verdict(index: int, dry_run: bool) -> dict:
    event = {
        "rawPath": "/prod/evaluate-tool-call",
        "requestContext": {"http": {"method": "POST", "sourceIp": f"10.9.5.{index}"}, "stage": "prod"},
        "headers": {"content-type": "application/json"},
        "body": json.dumps({
            "session_id": f"observe-credential-{index}",
            "project_name": "Acme-Observe-Credential",
            "tool_name": "Bash",
            "arguments": {"command": f"export AWS_ACCESS_KEY_ID={KEY}"},
            "agent": "claude-code",
            "origin": "hook",
            "explain": False,
            "dry_run": dry_run,
        }),
    }
    return json.loads(lambda_handler(event, None)["body"])


@pytest.fixture(autouse=True)
def _observe(monkeypatch):
    monkeypatch.setenv("DEFAULT_HOOK_STAGE", "observe")


def test_a_credential_is_refused_in_observe() -> None:
    verdict = _verdict(1, dry_run=False)
    assert verdict["project_stage"] == "observe"
    assert verdict["status"] == "BLOCKED_SECRET_DETECTED", verdict.get("reason")
    assert verdict["session_tripped"] is False


def test_a_dry_run_only_records_it() -> None:
    verdict = _verdict(2, dry_run=True)
    assert verdict["status"] == "APPROVED"
    assert verdict.get("observed_rules"), "the dry run records what it would have refused"


def test_any_other_rule_still_only_watches_in_observe() -> None:
    event = {
        "rawPath": "/prod/evaluate-tool-call",
        "requestContext": {"http": {"method": "POST", "sourceIp": "10.9.5.3"}, "stage": "prod"},
        "headers": {"content-type": "application/json"},
        "body": json.dumps({
            "session_id": "observe-credential-3",
            "project_name": "Acme-Observe-Credential",
            "tool_name": "Write",
            "arguments": {"file_path": "src/acme/domain/order.py", "content": "import boto3\n"},
            "agent": "claude-code",
            "origin": "hook",
            "explain": False,
        }),
    }
    verdict = json.loads(lambda_handler(event, None)["body"])
    assert verdict["status"] == "APPROVED"
    assert verdict.get("observed_rules")
