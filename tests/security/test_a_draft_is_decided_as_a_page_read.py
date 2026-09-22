"""`POST /rules/draft` is decided by the middleware exactly as `POST /rules/explain` is.

Drafting a rule saves nothing, issues no verdict and writes no ledger row, so it
is a page's read that needs a body: open on a stack whose reads are public, and
the operator's, by key or sign-in session, on one deployed with
PublicReads=false. The handler used to carry its own copy of that decision
because the middleware did not list the route; these tests hold the middleware
to it on both kinds of stack, so the copy could be deleted.

They call the middleware directly with a limiter of their own, so they decide
access and nothing else. Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import pytest

from threefold.infrastructure import auth_store
from threefold.infrastructure.auth_store import AuthStore
from threefold.infrastructure.security_middleware import (
    PAGE_READ_POSTS,
    PROTECTED_WRITES,
    PUBLIC_PATHS,
    TokenBucketRateLimiter,
    is_page_read,
    key_ref,
    validate_request_security,
)

OPERATOR_KEY = "acme-synthetic-operator-key-0007"
DRAFT = "/rules/draft"
EXPLAIN = "/rules/explain"


@pytest.fixture(autouse=True)
def _a_configured_key_and_a_clean_store(monkeypatch):
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)
    monkeypatch.delenv("STAGE", raising=False)
    monkeypatch.delenv("ENFORCE_API_KEY", raising=False)
    monkeypatch.delenv("PUBLIC_READS", raising=False)
    auth_store.reset_default_store(AuthStore())
    yield
    auth_store.reset_default_store()


def _decide(path: str, headers: dict | None = None, method: str = "POST"):
    allowed, problem = validate_request_security(
        headers=headers or {},
        client_ip="203.0.113.21",
        path=path,
        method=method,
        rate_limiter=TokenBucketRateLimiter(max_tokens=1000.0),
    )
    return allowed, (problem or {}).get("status"), (problem or {}).get("type")


def _session_headers() -> dict:
    token, _ = auth_store.default_store().create_session(key_ref(OPERATOR_KEY))
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------ the classification


def test_the_draft_is_listed_beside_explain_as_a_read_that_needs_a_body() -> None:
    assert {DRAFT, EXPLAIN} <= PAGE_READ_POSTS
    assert is_page_read("POST", DRAFT) and is_page_read("POST", EXPLAIN)


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE", "PATCH"])
def test_only_a_post_to_the_draft_route_is_a_page_read(method) -> None:
    """The route answers POST alone; no other method is opened by the listing."""
    assert not is_page_read(method, DRAFT)


def test_the_draft_is_neither_a_public_path_nor_a_protected_write() -> None:
    """A public path ignores the method and would open drafts on a private stack; a protected
    write would close them on the public demo, where there is no key to present."""
    assert DRAFT not in PUBLIC_PATHS
    assert ("POST", DRAFT) not in PROTECTED_WRITES


def test_a_near_miss_is_not_read_as_the_draft_route() -> None:
    # A trailing slash or a doubled one is not a near miss: the router collapses
    # repeated slashes and drops a trailing one before the middleware sees the
    # path, so "/rules/draft/" and "/rules//draft" are the draft route itself,
    # decided and answered as it is. The test below holds that end to end.
    for path in ("/rules/drafts", "/rules/draft/x", "/Rules/draft", "/rules/draftx"):
        assert not is_page_read("POST", path), path


@pytest.mark.parametrize("spelling", ["/prod/rules/draft", "/prod/rules/draft/", "/prod//rules//draft"])
def test_every_spelling_the_router_reads_as_the_draft_is_decided_as_the_draft(monkeypatch, spelling) -> None:
    """The router and the middleware see one normalised path, so no spelling slips past the ladder."""
    import json

    from threefold.interfaces import draft_routes
    from threefold.interfaces.api_handlers import lambda_handler

    class NoModel:
        """Stands where the model client would be, so a regression fails here instead of drafting."""

        def __getattr__(self, name):
            raise AssertionError("An anonymous draft on a private stack reached the model client")

    monkeypatch.setattr(draft_routes, "_client", NoModel())
    monkeypatch.setenv("PUBLIC_READS", "false")
    response = lambda_handler(
        {
            "rawPath": spelling,
            "headers": {"Content-Type": "application/json"},
            "requestContext": {"http": {"method": "POST", "sourceIp": "203.0.113.22"}, "stage": "prod"},
            "body": json.dumps({"description": "Billing domain classes may not reach persistence."}),
        }
    )
    assert response["statusCode"] == 401, response["body"]
    assert json.loads(response["body"])["type"] == "urn:threefold:error:missing-credentials"


# ------------------------------------------------------------- the public stack


@pytest.mark.parametrize("enforced", [{}, {"ENFORCE_API_KEY": "true"}, {"STAGE": "prod"}])
def test_on_the_public_demo_anyone_may_draft(monkeypatch, enforced) -> None:
    """Whatever else the stack enforces, the demo's visitors have no key to present."""
    for name, value in enforced.items():
        monkeypatch.setenv(name, value)
    assert _decide(DRAFT)[0] is True
    assert _decide(DRAFT) == _decide(EXPLAIN)


def test_on_the_public_demo_with_no_key_configured_anyone_may_still_draft(monkeypatch) -> None:
    monkeypatch.delenv("THREEFOLD_API_KEYS")
    assert _decide(DRAFT)[0] is True


# ------------------------------------------------------------ the private stack


def test_on_a_private_stack_with_no_key_configured_nobody_drafts(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.delenv("THREEFOLD_API_KEYS")
    assert _decide(DRAFT) == (False, 403, "urn:threefold:error:reads-private")
    assert _decide(DRAFT) == _decide(EXPLAIN)


def test_on_a_private_stack_a_draft_climbs_the_operator_ladder(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    assert _decide(DRAFT) == (False, 401, "urn:threefold:error:missing-credentials")
    assert _decide(DRAFT, {"X-API-Key": "acme-wrong-key"}) == (
        False, 403, "urn:threefold:error:invalid-credentials"
    )
    assert _decide(DRAFT, {"X-API-Key": OPERATOR_KEY})[0] is True
    assert _decide(DRAFT, {"Authorization": f"Bearer {OPERATOR_KEY}"})[0] is True


def test_on_a_private_stack_a_sign_in_session_drafts_as_the_key_does(monkeypatch) -> None:
    """The dashboard's operator signs in rather than pasting a key, and drafts from the page."""
    monkeypatch.setenv("PUBLIC_READS", "false")
    assert _decide(DRAFT, _session_headers())[0] is True


def test_on_a_private_stack_the_placeholder_key_does_not_draft(monkeypatch) -> None:
    """A key printed in the source is not a key, here as on every private read."""
    from threefold.infrastructure.security_middleware import DEFAULT_DEMO_API_KEY

    monkeypatch.setenv("PUBLIC_READS", "false")
    assert _decide(DRAFT, {"X-API-Key": DEFAULT_DEMO_API_KEY})[:2] == (False, 403)


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-API-Key": "acme-wrong-key"}, {"X-API-Key": OPERATOR_KEY}],
    ids=["anonymous", "wrong-key", "operator"],
)
def test_on_a_private_stack_every_draft_is_answered_as_explain_would_be(monkeypatch, headers) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    assert _decide(DRAFT, headers) == _decide(EXPLAIN, headers)
