"""A stack can name the projects that start in Enforce, and nothing else changes.

The public demo has no operator who could promote the projects a real coding
agent reports as once a day, and a real agent that could never be refused
would measure nothing. ENFORCE_PROJECT_PATTERN names them; every other
project still starts in the stack's default, and a project someone configured
keeps its own stage.
"""
from __future__ import annotations

import json

import pytest

from threefold.application import projects as stages
from threefold.interfaces.api_handlers import lambda_handler

DOMAIN_WRITE = {"file_path": "src/acme/domain/order.py", "content": "import boto3\n"}


def _call(project: str, session: str, index: int) -> dict:
    event = {
        "rawPath": "/prod/evaluate-tool-call",
        "requestContext": {"http": {"method": "POST", "sourceIp": f"10.44.0.{index}"}, "stage": "prod"},
        "headers": {"content-type": "application/json"},
        "body": json.dumps({
            "session_id": session, "project_name": project, "tool_name": "Write", "action_type": "FILE_WRITE",
            "arguments": DOMAIN_WRITE, "agent": "claude-code", "origin": "hook", "explain": False,
            "hook_mode": "managed",
        }),
    }
    return json.loads(lambda_handler(event, None)["body"])


@pytest.fixture
def observe_default(monkeypatch):
    monkeypatch.delenv("DEFAULT_HOOK_STAGE", raising=False)


def test_a_named_project_is_refused_from_its_first_call(monkeypatch, observe_default) -> None:
    monkeypatch.setenv("ENFORCE_PROJECT_PATTERN", r"^Acme-Live-.+$")
    verdict = _call("Acme-Live-orders-s3-archive", "live-orders-1", 1)
    assert verdict["status"] == "BLOCKED_BOUNDARY_VIOLATION" and verdict["project_stage"] == "enforce"


def test_every_other_project_still_starts_in_the_default(monkeypatch, observe_default) -> None:
    monkeypatch.setenv("ENFORCE_PROJECT_PATTERN", r"^Acme-Live-.+$")
    verdict = _call("Acme-Ordinary-one", "ordinary-1", 2)
    assert verdict["status"] == "APPROVED" and verdict["project_stage"] == "observe"


def test_without_the_parameter_nothing_starts_in_enforce(observe_default) -> None:
    verdict = _call("Acme-Live-orders-s3-archive", "live-orders-2", 3)
    assert verdict["status"] == "APPROVED" and verdict["project_stage"] == "observe"


def test_a_pattern_that_does_not_compile_is_ignored(monkeypatch, observe_default) -> None:
    monkeypatch.setenv("ENFORCE_PROJECT_PATTERN", "^Acme-Live-(")
    assert stages.enforced_by_default("Acme-Live-x") is False
    verdict = _call("Acme-Live-orders-s3-archive", "live-orders-3", 4)
    assert verdict["project_stage"] == "observe"


def test_a_configured_project_keeps_its_own_stage(monkeypatch) -> None:
    monkeypatch.setenv("ENFORCE_PROJECT_PATTERN", r"^Acme-Live-.+$")
    assert stages.stage_of({"stage": "observe"}, "Acme-Live-x") == "observe"
    assert stages.stage_of(None, "Acme-Live-x") == "enforce"
    assert stages.stage_of(None, "Acme-Other") == stages.default_hook_stage()
