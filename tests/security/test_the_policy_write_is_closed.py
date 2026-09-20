"""Reading the policy is open. Changing it is not.

`POST /policy/config` writes through to DynamoDB under `CONFIG#policy`, and a
cold container adopts whatever it finds there, so an anonymous write would raise
the loop threshold this product leads with for every session that came after it
and outlive the caller. The read stays open because a judge, a scorer or an
operator has to be able to see what is actually enforced.
"""
from __future__ import annotations

import json
import os

import pytest

from threefold.interfaces.api_handlers import lambda_handler

POLICY = {
    "max_single_call_usd": 2.0,
    "max_session_budget_usd": 20.0,
    "loop_history_window": 6,
    "monomorphic_repetition_threshold": 4,
}


@pytest.fixture(autouse=True)
def _no_keys_unless_a_test_sets_them():
    previous = os.environ.pop("THREEFOLD_API_KEYS", None)
    yield
    os.environ.pop("THREEFOLD_API_KEYS", None)
    if previous is not None:
        os.environ["THREEFOLD_API_KEYS"] = previous


def _call(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    event = {
        "rawPath": f"/prod{path}",
        "headers": headers or {"Content-Type": "application/json"},
        "requestContext": {"http": {"method": method}, "stage": "prod"},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    response = lambda_handler(event)
    return response["statusCode"], json.loads(response["body"])


def test_reading_the_policy_needs_no_key() -> None:
    status, body = _call("GET", "/policy/config")
    assert status == 200
    assert "monomorphic_repetition_threshold" in body


def test_an_anonymous_write_is_refused_when_no_key_is_configured() -> None:
    status, body = _call("POST", "/policy/config", POLICY)
    assert status == 403
    assert body["type"] == "urn:threefold:error:policy-write-disabled"
    assert "read but not changed" in body["detail"]


def test_the_short_alias_is_closed_too() -> None:
    """/policy and /policy/config are the same write."""
    assert _call("POST", "/policy", POLICY)[0] == 403


def test_a_write_without_a_key_is_refused_when_one_is_configured() -> None:
    os.environ["THREEFOLD_API_KEYS"] = "operator-key-1"
    status, body = _call("POST", "/policy/config", POLICY)
    assert status == 401
    assert body["type"] == "urn:threefold:error:missing-credentials"


def test_a_wrong_key_is_refused() -> None:
    os.environ["THREEFOLD_API_KEYS"] = "operator-key-1"
    status, _ = _call(
        "POST",
        "/policy/config",
        POLICY,
        {"Content-Type": "application/json", "X-API-Key": "not-the-key"},
    )
    assert status == 403


@pytest.mark.parametrize(
    "auth_headers",
    [
        {"X-API-Key": "operator-key-1"},
        {"Authorization": "Bearer operator-key-1"},
    ],
)
def test_the_operator_key_still_writes(auth_headers: dict) -> None:
    os.environ["THREEFOLD_API_KEYS"] = "operator-key-1"
    headers = {"Content-Type": "application/json", **auth_headers}
    status, body = _call("POST", "/policy/config", POLICY, headers)
    assert status == 200
    assert body["status"] == "POLICY_UPDATED"
    assert body["config"]["monomorphic_repetition_threshold"] == 4


def test_the_placeholder_key_never_opens_the_write() -> None:
    """A key printed in the source is not a key.

    The read path falls back to a demo placeholder when keys are switched on
    without being configured. The write path must not: that fallback is in the
    repository, so anyone could present it.
    """
    from threefold.infrastructure.security_middleware import DEFAULT_DEMO_API_KEY

    status, _ = _call(
        "POST",
        "/policy/config",
        POLICY,
        {"Content-Type": "application/json", "X-API-Key": DEFAULT_DEMO_API_KEY},
    )
    assert status == 403


def test_closing_the_write_left_the_rest_of_the_demo_open() -> None:
    """The zero-setup visitor path is the ship gate; this must not touch it."""
    status, _ = _call(
        "POST",
        "/evaluate-tool-call",
        {
            "session_id": "policy-guard-open-001",
            "tool_name": "view_file",
            "action_type": "FILE_READ",
            "arguments": {"path": "README.md"},
        },
    )
    assert status == 200
    assert _call("GET", "/status")[0] == 200
    assert _call("POST", "/simulate-loop")[0] == 200
