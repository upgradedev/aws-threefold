"""Keeps the suite hermetic.

Without this the repository and Bedrock clients pick up whatever AWS credentials
the developer happens to have, so the tests make real network calls, take
seventeen seconds, and pass or fail depending on the machine they run on.
"""
from __future__ import annotations

import os
import sys

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
def _stack_parameters_start_at_their_defaults(monkeypatch):
    """Each test sees the stack as deployed with no parameter overridden.

    PUBLIC_READS and ALLOWED_PROJECT_PATTERN are read per request, so a value in
    the developer's shell, or one a test forgot to restore, would close the
    reads or relabel projects for every test after it. Tests that need them set
    them with monkeypatch, which undoes itself.
    """
    monkeypatch.delenv("PUBLIC_READS", raising=False)
    monkeypatch.delenv("ALLOWED_PROJECT_PATTERN", raising=False)
    yield


@pytest.fixture(autouse=True)
def _hooks_are_enforced_unless_a_test_says_otherwise(monkeypatch):
    """Every test written before stages existed keeps the meaning it had.

    A project nobody has configured is in the stack's DEFAULT_HOOK_STAGE, and
    the deployed default is observe, where a hook's call is never refused. The
    suite's hook tests were written when every hook call was enforced, so the
    suite runs with enforce; a test of the observe default unsets this with
    monkeypatch.
    """
    monkeypatch.setenv("DEFAULT_HOOK_STAGE", "enforce")
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


@pytest.fixture(autouse=True)
def _project_rules_do_not_outlive_their_test():
    """Rules saved for one project in one test are gone before the next.

    The handler's evaluator is module level, and it now holds rules per
    project as well as the shared set. The files that save rules restore the
    shared set themselves and know nothing of projects, so a project set saved
    in one file would judge a call in another, and the failure would land
    somewhere unrelated and order dependent. Nothing is imported here: a test
    that never loaded the handler has nothing to clear.
    """
    yield
    handlers = sys.modules.get("threefold.interfaces.api_handlers")
    if handlers is None:
        return
    from threefold.infrastructure.dynamo_repo import (
        PROJECT_CONFIG_PREFIX,
        PROJECT_INDEX_PARTITION,
        PROJECT_RULES_PREFIX,
    )

    evaluator = handlers._evaluator
    # A project's stage is held and stored the same way its rules are, and a
    # stage saved in one test would decide a call in another just as quietly.
    for name in ("_project_rules", "_project_configs"):
        held = getattr(evaluator, name, None)
        if held is not None:
            held.clear()
    store = getattr(evaluator.session_repo, "_memory_store", None) or {}
    shared = f"{PROJECT_RULES_PREFIX}METADATA"
    for key in [k for k in store if k.startswith(PROJECT_RULES_PREFIX) and k != shared]:
        del store[key]
    for key in [k for k in store if k.startswith((PROJECT_CONFIG_PREFIX, f"{PROJECT_INDEX_PARTITION}#"))]:
        del store[key]


@pytest.fixture(autouse=True)
def _an_operator_key_for_the_application_routes(request, monkeypatch):
    """Writes to a project need the operator since the access rules landed.

    A deployment with no key configured refuses them outright, so the tests of
    the application's routes run against a stack that has one. Every other
    module keeps the stack as deployed, with no key.
    """
    if request.module.__name__.rsplit(".", 1)[-1].startswith("test_app_"):
        monkeypatch.setenv("THREEFOLD_API_KEYS", "operator-key-1")
    yield
