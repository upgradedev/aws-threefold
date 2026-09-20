"""Integration tests for Threefold /readyz, /policy/config, and Emergency Kill Switch.

Verifies:
- Deep readiness probe /readyz
- Dynamic policy configuration get and post
- Emergency session manual termination (kill switch)
- Idempotency key caching
"""

from __future__ import annotations

import json
import pytest

from threefold.infrastructure.idempotency import global_idempotency_cache
from threefold.interfaces.api_handlers import lambda_handler


def test_readyz_endpoint_reports_subsystem_health() -> None:
    event = {
        "httpMethod": "GET",
        "path": "/readyz",
        "headers": {},
    }
    response = lambda_handler(event)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["status"] == "READY"
    assert body["service"] == "Threefold"
    sub_names = [s["name"] for s in body["subsystems"]]
    assert "DynamoDBSessionsRepository" in sub_names
    assert "AmazonBedrockClaude35Sonnet" in sub_names
    assert "WebhookAlertDispatcher" in sub_names


def test_dynamic_policy_configuration() -> None:
    # 1. GET current policy config
    get_event = {
        "httpMethod": "GET",
        "path": "/policy/config",
        "headers": {},
    }
    get_res = lambda_handler(get_event)
    assert get_res["statusCode"] == 200
    get_body = json.loads(get_res["body"])
    assert "max_single_call_usd" in get_body

    # 2. POST updated policy config
    post_event = {
        "httpMethod": "POST",
        "path": "/policy/config",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "max_single_call_usd": 0.75,
            "max_session_budget_usd": 25.00,
            "monomorphic_repetition_threshold": 4,
        }),
    }
    post_res = lambda_handler(post_event)
    assert post_res["statusCode"] == 200
    post_body = json.loads(post_res["body"])
    assert post_body["status"] == "POLICY_UPDATED"
    assert post_body["config"]["max_single_call_usd"] == 0.75
    assert post_body["config"]["max_session_budget_usd"] == 25.00


def test_emergency_kill_switch_freezes_session() -> None:
    session_id = "sess-kill-test-001"
    event = {
        "httpMethod": "POST",
        "path": f"/sessions/{session_id}/terminate",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "operator_name": "VP of AI Safety",
            "reason": "Suspicious tool actuation anomaly",
        }),
    }
    res = lambda_handler(event)
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["status"] == "SESSION_FROZEN"
    assert body["session_id"] == session_id
    assert body["is_tripped"] is True

    # Subsequent tool invocation on frozen session must be blocked
    tool_event = {
        "httpMethod": "POST",
        "path": "/evaluate-tool-call",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "session_id": session_id,
            "tool_name": "read_file",
            "action_type": "FILE_READ",
            "arguments": {"path": "src/app.py"},
        }),
    }
    tool_res = lambda_handler(tool_event)
    assert tool_res["statusCode"] == 200
    tool_body = json.loads(tool_res["body"])
    assert tool_body["status"] == "BLOCKED_CIRCUIT_BREAKER"
    assert "Session execution frozen" in tool_body["reason"]


def test_idempotency_key_caching_on_kill_switch() -> None:
    global_idempotency_cache.clear()
    idemp_key = "idemp-agentic-test-key-999"

    event = {
        "httpMethod": "POST",
        "path": "/sessions/sess-idemp-01/terminate",
        "headers": {
            "Content-Type": "application/json",
            "Idempotency-Key": idemp_key,
        },
        "body": json.dumps({
            "operator_name": "Lead Auditor",
            "reason": "Idempotency test freeze",
        }),
    }

    # 1st call
    r1 = lambda_handler(event)
    assert r1["statusCode"] == 200
    b1 = json.loads(r1["body"])

    # 2nd call
    r2 = lambda_handler(event)
    assert r2["statusCode"] == 200
    b2 = json.loads(r2["body"])
    assert b2["session_id"] == b1["session_id"]
    assert b2["status"] == b1["status"]
