"""AWS Serverless Idempotency Middleware for Threefold.

Caches tool evaluation responses based on client Idempotency-Key headers
to prevent duplicate budget deductions or duplicate audit records under network retries.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Dict, Optional, Tuple


@dataclass
class CachedIdempotentResponse:
    status_code: int
    body: Dict[str, Any]
    created_at: float
    ttl_seconds: float = 300.0

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_seconds


class IdempotencyCache:
    """Thread-safe in-memory idempotency cache with TTL expiration."""

    def __init__(self, default_ttl_seconds: float = 300.0) -> None:
        self._cache: Dict[str, CachedIdempotentResponse] = {}
        self._lock = threading.Lock()
        self._default_ttl = default_ttl_seconds

    def get(self, idempotency_key: str) -> Optional[Tuple[int, Dict[str, Any]]]:
        with self._lock:
            cached = self._cache.get(idempotency_key)
            if not cached:
                return None
            if cached.is_expired:
                del self._cache[idempotency_key]
                return None
            return cached.status_code, cached.body

    def set(
        self,
        idempotency_key: str,
        status_code: int,
        body: Dict[str, Any],
        ttl_seconds: Optional[float] = None,
    ) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        with self._lock:
            self._cache[idempotency_key] = CachedIdempotentResponse(
                status_code=status_code,
                body=body,
                created_at=time.time(),
                ttl_seconds=ttl,
            )

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


# Global singleton instance
global_idempotency_cache = IdempotencyCache()
