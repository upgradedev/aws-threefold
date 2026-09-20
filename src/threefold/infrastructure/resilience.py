"""Resilience, Retry & Fault Tolerance for Threefold.

Implements exponential backoff with full jitter for Amazon Bedrock and DynamoDB calls.
"""

from __future__ import annotations

import functools
import logging
import random
import time
from typing import Any, Callable, TypeVar

logger = logging.getLogger("threefold.resilience")

T = TypeVar("T")


def retry_with_exponential_backoff(
    max_retries: int = 3,
    base_delay_seconds: float = 0.5,
    max_delay_seconds: float = 5.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator applying exponential backoff with full jitter to any callable."""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            retries = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    retries += 1
                    if retries > max_retries:
                        logger.error(
                            "Max retries (%d) exceeded for %s: %s",
                            max_retries,
                            func.__name__,
                            str(e),
                        )
                        raise

                    ceiling = min(max_delay_seconds, base_delay_seconds * (2 ** retries))
                    sleep_duration = random.uniform(0, ceiling)

                    logger.warning(
                        "Retry %d/%d for %s after %.3fs due to: %s",
                        retries,
                        max_retries,
                        func.__name__,
                        sleep_duration,
                        str(e),
                    )
                    time.sleep(sleep_duration)

        return wrapper

    return decorator
