"""Security & Zero-Trust Production Tests for Threefold."""

import os
from threefold.infrastructure.security_middleware import (
    DEFAULT_DEMO_API_KEY,
    MAX_PAYLOAD_SIZE_BYTES,
    TokenBucketRateLimiter,
    rfc7807_error,
    validate_request_security,
)
from threefold.interfaces.api_handlers import lambda_handler


def test_public_status_endpoint_allowed_without_auth():
    """Verify health and status endpoints are accessible without credentials."""
    event = {
        "httpMethod": "GET",
        "path": "/status",
        "headers": {},
    }
    response = lambda_handler(event)
    assert response["statusCode"] == 200
    assert "HEALTHY" in response["body"]


def test_auth_enforcement_in_prod():
    """Verify that protected endpoints reject missing or invalid keys in production mode."""
    os.environ["STAGE"] = "prod"
    os.environ["ENFORCE_API_KEY"] = "true"

    try:
        # 1. Missing API Key -> 401 Unauthorized
        event = {
            "httpMethod": "POST",
            "path": "/evaluate-tool-call",
            "headers": {},
            "body": "{}",
        }
        res = lambda_handler(event)
        assert res["statusCode"] == 401
        assert "Unauthorized" in res["body"]

        # 2. Invalid API Key -> 403 Forbidden
        event_bad_key = {
            "httpMethod": "POST",
            "path": "/evaluate-tool-call",
            "headers": {"X-API-Key": "malicious-invalid-key"},
            "body": "{}",
        }
        res_bad = lambda_handler(event_bad_key)
        assert res_bad["statusCode"] == 403
        assert "Forbidden" in res_bad["body"]

        # 3. Valid Demo API Key -> 200 OK
        event_valid_key = {
            "httpMethod": "POST",
            "path": "/evaluate-tool-call",
            "headers": {"X-API-Key": DEFAULT_DEMO_API_KEY},
            "body": '{"tool_name": "read_file", "session_id": "test-sec"}',
        }
        res_ok = lambda_handler(event_valid_key)
        assert res_ok["statusCode"] == 200
    finally:
        os.environ["STAGE"] = "dev"
        os.environ["ENFORCE_API_KEY"] = "false"


def test_rate_limiter_trips_on_burst():
    """Verify that token bucket rate limiter restricts aggressive burst attacks."""
    limiter = TokenBucketRateLimiter(refill_rate_per_sec=1.0, max_tokens=4.0)
    client_ip = "198.51.100.99"

    for _ in range(4):
        assert limiter.is_allowed(client_ip) is True

    assert limiter.is_allowed(client_ip) is False


def test_payload_size_limit_rejection():
    """Verify that payloads exceeding 1MB are rejected with HTTP 413."""
    is_valid, problem = validate_request_security(
        headers={},
        client_ip="127.0.0.1",
        path="/evaluate-tool-call",
        payload_size_bytes=MAX_PAYLOAD_SIZE_BYTES + 500,
    )
    assert is_valid is False
    assert problem is not None
    assert problem["status"] == 413
    assert problem["title"] == "Payload Too Large"


def test_negative_tokens_rejected():
    """Verify that negative token parameters are rejected with HTTP 400 Problem Details."""
    event = {
        "httpMethod": "POST",
        "path": "/evaluate-tool-call",
        "headers": {},
        "body": '{"tool_name": "read_file", "projected_input_tokens": -500}',
    }
    res = lambda_handler(event)
    assert res["statusCode"] == 400
    assert "Invalid Parameter" in res["body"]


def test_rate_limiter_thread_safety():
    """Verify thread-safety of token bucket rate limiter under concurrent thread contention."""
    import concurrent.futures

    limiter = TokenBucketRateLimiter(refill_rate_per_sec=0.0, max_tokens=50.0)
    client_ip = "192.0.2.1"
    results = []

    def make_request():
        return limiter.is_allowed(client_ip)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(make_request) for _ in range(70)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    allowed = sum(1 for r in results if r is True)
    blocked = sum(1 for r in results if r is False)
    assert allowed == 50
    assert blocked == 20
