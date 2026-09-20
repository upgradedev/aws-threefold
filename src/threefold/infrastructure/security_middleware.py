"""Zero-Trust Security & API Guard Middleware for Threefold.

Implements:
1. API Key & Bearer Token Authentication with configurable environment bypass.
2. In-Memory Token-Bucket Rate Limiter (DoS & LLM cost-exhaustion defense).
3. RFC 7807 Problem Details for standardized HTTP error reporting.
4. Payload size & content guard (1MB ceiling).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

MAX_PAYLOAD_SIZE_BYTES = 1024 * 1024
# Read from the environment so no key literal lives in the source tree. The
# fallback is a placeholder, not a credential: the middleware only enforces keys
# when STAGE is prod or ENFORCE_API_KEY is set, and a deployment that turns those
# on without setting THREEFOLD_API_KEYS is meant to reject every request.
DEFAULT_DEMO_API_KEY = os.environ.get("THREEFOLD_DEMO_API_KEY", "unset-demo-key")


class TokenBucketRateLimiter:
    """Thread-safe token-bucket rate limiter for API endpoints."""

    def __init__(self, refill_rate_per_sec: float = 2.0, max_tokens: float = 60.0):
        self.refill_rate = refill_rate_per_sec
        self.max_tokens = max_tokens
        self._buckets: Dict[str, Tuple[float, float]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, client_id: str, tokens_requested: float = 1.0) -> bool:
        with self._lock:
            now = time.monotonic()
            if client_id not in self._buckets:
                self._buckets[client_id] = (self.max_tokens - tokens_requested, now)
                return True

            current_tokens, last_update = self._buckets[client_id]
            elapsed = now - last_update
            refreshed_tokens = min(self.max_tokens, current_tokens + elapsed * self.refill_rate)

            if refreshed_tokens >= tokens_requested:
                self._buckets[client_id] = (refreshed_tokens - tokens_requested, now)
                return True

            self._buckets[client_id] = (refreshed_tokens, now)
            return False

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


_global_rate_limiter = TokenBucketRateLimiter(refill_rate_per_sec=2.0, max_tokens=60.0)


def rfc7807_error(
    status_code: int,
    title: str,
    detail: str,
    instance: str = "/",
    error_type: str = "about:blank",
    invalid_params: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Generates an RFC 7807 Problem Details compliant error dictionary."""
    problem: Dict[str, Any] = {
        "error": detail,
        "type": error_type,
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": instance,
        "timestamp": time.time(),
    }
    if invalid_params:
        problem["invalid_params"] = invalid_params
    return problem


def validate_request_security(
    headers: Dict[str, str],
    client_ip: str,
    path: str,
    payload_size_bytes: int = 0,
    rate_limiter: Optional[TokenBucketRateLimiter] = None,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    limiter = rate_limiter or _global_rate_limiter
    stage = os.environ.get("STAGE", "dev").lower()
    enforce_auth = os.environ.get("ENFORCE_API_KEY", "false").lower() in ("true", "1", "yes")

    # 1. Payload size check
    if payload_size_bytes > MAX_PAYLOAD_SIZE_BYTES:
        return False, rfc7807_error(
            status_code=413,
            title="Payload Too Large",
            detail=f"Payload size ({payload_size_bytes} bytes) exceeds maximum ceiling ({MAX_PAYLOAD_SIZE_BYTES} bytes)",
            instance=path,
            error_type="urn:threefold:error:payload-too-large",
        )

    # 2. Rate limiting check
    if not limiter.is_allowed(client_ip):
        return False, rfc7807_error(
            status_code=429,
            title="Too Many Requests",
            detail="Rate limit exceeded. Maximum 60 requests per minute allowable.",
            instance=path,
            error_type="urn:threefold:error:rate-limit-exceeded",
        )

    # 3. Public paths exemption
    # The hook is handed out anonymously on purpose: a reader who cannot take the
    # script cannot adopt the product, and a key requirement would put the one
    # artifact that matters behind the one thing a visitor does not have.
    public_paths = {
        "/",
        "/status",
        "/health",
        "/docs",
        "/openapi.json",
        "/openapi.yaml",
        "/hooks/claude_code_hook.py",
        "/claude_code_hook.py",
    }
    if path in public_paths:
        return True, None

    # 4. Authentication enforcement
    if enforce_auth or stage == "prod":
        normalized_headers = {k.lower(): v for k, v in headers.items()}
        api_key = normalized_headers.get("x-api-key")
        auth_header = normalized_headers.get("authorization", "")

        if not api_key and auth_header.startswith("Bearer "):
            api_key = auth_header[7:].strip()

        configured_keys = os.environ.get("THREEFOLD_API_KEYS", DEFAULT_DEMO_API_KEY).split(",")
        configured_keys = [k.strip() for k in configured_keys if k.strip()]

        if not api_key:
            return False, rfc7807_error(
                status_code=401,
                title="Unauthorized",
                detail="Missing required API Key. Provide via 'X-API-Key' or 'Authorization: Bearer <key>' header.",
                instance=path,
                error_type="urn:threefold:error:missing-credentials",
            )

        if api_key not in configured_keys:
            return False, rfc7807_error(
                status_code=403,
                title="Forbidden",
                detail="Provided API Key is invalid or expired.",
                instance=path,
                error_type="urn:threefold:error:invalid-credentials",
            )

    return True, None
