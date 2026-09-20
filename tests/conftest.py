"""Keeps the suite hermetic.

Without this the repository and Bedrock clients pick up whatever AWS credentials
the developer happens to have, so the tests make real network calls, take
seventeen seconds, and pass or fail depending on the machine they run on.
"""
from __future__ import annotations

import os
import pytest


@pytest.fixture(autouse=True, scope="session")
def _force_offline_clients():
    os.environ["THREEFOLD_OFFLINE"] = "1"
    yield
