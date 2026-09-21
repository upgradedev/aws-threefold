"""Keeps the suite hermetic.

Without this the repository and Bedrock clients pick up whatever AWS credentials
the developer happens to have, so the tests make real network calls, take
seventeen seconds, and pass or fail depending on the machine they run on.
"""
from __future__ import annotations

import os
import pytest

# Set on import as well as in the fixture below. The handler module builds its
# repository and Bedrock client when a test file imports it, which happens at
# collection, before any fixture runs, so a flag set only in a fixture arrived
# after the clients had already bound to whatever credentials the machine had.
os.environ["THREEFOLD_OFFLINE"] = "1"


@pytest.fixture(autouse=True, scope="session")
def _force_offline_clients():
    os.environ["THREEFOLD_OFFLINE"] = "1"
    yield


@pytest.fixture(autouse=True)
def _reset_the_shared_rate_limiter():
    """Stops the suite from rate limiting itself.

    `lambda_handler` meters every call against one process-wide token bucket,
    keyed by client address, and the whole suite arrives from 127.0.0.1. The
    bucket holds sixty tokens and refills at two a second, so once the suite grew
    past that, tests began failing with 429 in whatever order they happened to
    run in. That is a property of how many tests exist, not of the code under
    test. Each test now starts with a full bucket.

    The rate limiter's own tests build their own limiter, so nothing here hides
    the behaviour this clears.
    """
    from threefold.infrastructure.security_middleware import _global_rate_limiter

    _global_rate_limiter.reset()
    yield
