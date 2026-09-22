"""Integration tests for AWS Lambda api_handlers."""
from __future__ import annotations

import json
import uuid

import pytest
from threefold.interfaces.api_handlers import lambda_handler


def test_status_endpoint():
    event = {
        "httpMethod": "GET",
        "path": "/status",
    }
    response = lambda_handler(event)
    assert response["statusCode"] == 200
    assert response["headers"]["Access-Control-Allow-Origin"] == "*"
    body = json.loads(response["body"])
    assert body["service"] == "Threefold"
    assert body["status"] == "HEALTHY"


def test_evaluate_tool_call_endpoint_approved():
    event = {
        "httpMethod": "POST",
        "path": "/evaluate-tool-call",
        "body": json.dumps({
            "session_id": "session-api-1",
            "developer_id": "dev-101",
            "project_name": "AuthService",
            "tool_name": "list_dir",
            "action_type": "FILE_READ",
            "arguments": {"DirectoryPath": "/src"},
        }),
    }
    response = lambda_handler(event)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["status"] == "APPROVED"
    assert body["bedrock_explanation"] is not None


def test_simulate_loop_endpoint():
    # The route names its session after the request id, and falls back to
    # "001" when an event carries none, so every caller without one shares the
    # session sim-loop-001 in the handler's module-level evaluator. Any earlier
    # test that pressed the scenario (tests/security does) left it halted, and
    # this call was then refused as a halted session rather than as a loop. A
    # request id of its own gives this test a session of its own, as API
    # Gateway gives every deployed request.
    event = {
        "httpMethod": "POST",
        "path": "/simulate-loop",
        "requestContext": {"requestId": uuid.uuid4().hex},
    }
    response = lambda_handler(event)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["status"] == "BLOCKED_LOOP_DETECTED"
    assert body["session_tripped"] is True


def test_simulate_secret_endpoint():
    event = {
        "httpMethod": "POST",
        "path": "/simulate-secret",
    }
    response = lambda_handler(event)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["status"] == "BLOCKED_SECRET_DETECTED"


def test_bad_json_handling():
    event = {
        "httpMethod": "POST",
        "path": "/evaluate-tool-call",
        "body": "{invalid-json",
    }
    response = lambda_handler(event)
    assert response["statusCode"] == 400


def test_not_found_endpoint():
    event = {
        "httpMethod": "GET",
        "path": "/nonexistent-route",
    }
    response = lambda_handler(event)
    assert response["statusCode"] == 404
