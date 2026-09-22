"""Shared by the tests of the application's routes: API Gateway v2 events, as deployed.

Every call goes through `lambda_handler`, so the routes are exercised behind the
same router, middleware and evaluator a deployed stack runs. This module holds
no tests of its own; it is named like one so it sits with the files it serves.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional, Tuple

from threefold.interfaces.api_handlers import lambda_handler

DOMAIN_WRITE = {"file_path": "src/acme/domain/order.py", "content": "import boto3\n"}
JAVA_WRITE = {
    "file_path": "src/main/java/com/acme/domain/Order.java",
    "content": "package com.acme.domain;\nimport javax.persistence.Entity;\n",
}
README = {"file_path": "README.md"}


def fresh_project(prefix: str = "Acme-App") -> str:
    """A project no other test has written to, so shared rollups cannot leak between tests."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def request(
    method: str,
    path: str,
    body: Any = None,
    query: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict[str, Any]]:
    event = {
        "rawPath": f"/prod{path}",
        "headers": dict({"Content-Type": "application/json"}, **(headers or {})),
        "queryStringParameters": query,
        "requestContext": {"http": {"method": method}, "stage": "prod"},
    }
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    response = lambda_handler(event)
    return response["statusCode"], json.loads(response["body"] or "{}")


def get(path: str, **query) -> Dict[str, Any]:
    status, body = request("GET", path, query={k: str(v) for k, v in query.items()} or None)
    assert status == 200, body
    return body


def post(path: str, body: Any = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    status, answer = request("POST", path, body if body is not None else {}, headers=headers)
    assert status == 200, answer
    return answer


def hook_call(
    project: str,
    session_id: str,
    arguments: Dict[str, Any],
    tool: str = "Write",
    action: str = "FILE_WRITE",
    **extra: Any,
) -> Dict[str, Any]:
    body = {
        "session_id": session_id,
        "project_name": project,
        "developer": extra.pop("developer", "a1b2c3d4e5f6"),
        "tool_name": tool,
        "action_type": action,
        "arguments": arguments,
        "agent": extra.pop("agent", "claude-code"),
        "origin": extra.pop("origin", "hook"),
        "explain": False,
        "hook_mode": extra.pop("hook_mode", "managed"),
    }
    body.update(extra)
    return post("/evaluate-tool-call", body)
