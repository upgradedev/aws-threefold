"""Who may change how a project is governed, who may make a sandbox, and who may read.

A project's stage, the rules it keeps observing and the review of what it
flagged decide what is refused on real developers' machines, so they are the
operator's. The single exception is a sandbox project on a stack whose reads
are public, so a visitor can walk observe, review and promote on a project of
their own. The name has to match exactly: every near miss is tried here,
because a pattern that is one character loose hands a stranger a real project.

These call the middleware directly with a limiter of their own, so they decide
access and nothing else; the routes behind them belong to another track.
"""
from __future__ import annotations

import pytest

from threefold.infrastructure import auth_store
from threefold.infrastructure.auth_store import AuthStore
from threefold.infrastructure.security_middleware import (
    AUTH_LINKS_PATH,
    PUBLIC_PATHS,
    SANDBOX_PATH,
    TokenBucketRateLimiter,
    key_ref,
    validate_request_security,
)

OPERATOR_KEY = "acme-synthetic-operator-key-0003"
SANDBOX = "Acme-Sandbox-0123abcd"


@pytest.fixture(autouse=True)
def _a_configured_key_and_a_clean_store(monkeypatch):
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)
    monkeypatch.delenv("STAGE", raising=False)
    monkeypatch.delenv("ENFORCE_API_KEY", raising=False)
    auth_store.reset_default_store(AuthStore())
    yield
    auth_store.reset_default_store()


def _decide(method: str, path: str, headers: dict | None = None):
    allowed, problem = validate_request_security(
        headers=headers or {},
        client_ip="203.0.113.9",
        path=path,
        method=method,
        rate_limiter=TokenBucketRateLimiter(max_tokens=1000.0),
    )
    return allowed, (problem or {}).get("status"), (problem or {}).get("type")


def _session_headers() -> dict:
    token, _ = auth_store.default_store().create_session(key_ref(OPERATOR_KEY))
    return {"Authorization": f"Bearer {token}"}


PROJECT_WRITES = [
    ("POST", "/api/projects/Acme-Billing"),
    ("POST", "/api/projects/Acme-Billing/promote"),
    ("POST", "/api/projects/Acme-Billing/demote"),
    ("POST", "/api/projects/Acme-Billing/reviews"),
    ("POST", "/api/projects"),
    ("DELETE", "/api/projects/Acme-Billing"),
    ("PUT", "/api/projects/Acme-Billing"),
    ("PATCH", "/api/projects/Acme-Billing"),
]


# ---------------------------------------------------------------- project writes


@pytest.mark.parametrize("method, path", PROJECT_WRITES)
@pytest.mark.parametrize("public", ["true", "false"])
def test_a_project_write_needs_the_operator(monkeypatch, method, path, public) -> None:
    monkeypatch.setenv("PUBLIC_READS", public)
    assert _decide(method, path)[:2] == (False, 401)
    assert _decide(method, path, {"X-API-Key": "acme-wrong"})[:2] == (False, 403)
    assert _decide(method, path, {"X-API-Key": OPERATOR_KEY})[0] is True
    assert _decide(method, path, _session_headers())[0] is True


def test_a_stack_with_no_key_changes_no_project(monkeypatch) -> None:
    monkeypatch.delenv("THREEFOLD_API_KEYS")
    allowed, status, kind = _decide("POST", "/api/projects/Acme-Billing/promote")
    assert (allowed, status, kind) == (False, 403, "urn:threefold:error:policy-write-disabled")


# -------------------------------------------------------------------- sandboxes


@pytest.mark.parametrize(
    "path",
    [
        f"/api/projects/{SANDBOX}",
        f"/api/projects/{SANDBOX}/promote",
        f"/api/projects/{SANDBOX}/demote",
        f"/api/projects/{SANDBOX}/reviews",
        "/api/projects/Acme-Sandbox-ffffffff",
        "/api/projects/Acme-Sandbox-00000000/promote",
    ],
)
def test_a_sandbox_is_anyones_to_change_on_the_public_demo(path) -> None:
    assert _decide("POST", path)[0] is True


@pytest.mark.parametrize("path", [f"/api/projects/{SANDBOX}", f"/api/projects/{SANDBOX}/promote"])
def test_a_sandbox_is_the_operators_on_a_private_stack(monkeypatch, path) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    assert _decide("POST", path)[:2] == (False, 401)
    assert _decide("POST", path, _session_headers())[0] is True


@pytest.mark.parametrize(
    "name",
    [
        "Acme-Sandbox-0123ABCD",  # upper-case hex
        "Acme-Sandbox-0123abc",  # seven
        "Acme-Sandbox-0123abcde",  # nine
        "Acme-Sandbox-0123abcg",  # not hex
        "acme-sandbox-0123abcd",  # lower-case prefix
        "Acme-Sandbox0123abcd",  # missing dash
        "Acme-Billing",  # a real project
        "Acme-Sandbox-0123abcd-x",
        "xAcme-Sandbox-0123abcd",
        "%41cme-Sandbox-0123abcd",  # matches only once decoded
        "Acme-Sandbox-0123abcd%2F..%2FAcme-Billing",
        "Acme-Sandbox-0123abcd\n",
        "Acme-Sandbox-0123abcd/../Acme-Billing",
        "Acme-Sandbox-0123abcd/../Acme-Billing/promote",
        "Acme-Sandbox-0123abcd/Promote",
        "Acme-Sandbox-0123abcd/promote.json",
        "Acme-Sandbox-0123abcd/%2e%2e",
        "",
    ],
)
def test_only_the_exact_sandbox_name_is_open(name) -> None:
    assert _decide("POST", f"/api/projects/{name}")[:2] == (False, 401), f"{name!r} was treated as a sandbox"


def test_reading_a_sandbox_does_not_open_writing_elsewhere() -> None:
    assert _decide("POST", "/api/projects/Acme-Billing/promote")[:2] == (False, 401)


# ------------------------------------------------------------- making a sandbox


def test_anyone_makes_a_sandbox_on_the_public_demo() -> None:
    assert _decide("POST", SANDBOX_PATH)[0] is True


def test_a_private_stack_makes_sandboxes_for_the_operator_only(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    assert _decide("POST", SANDBOX_PATH)[:2] == (False, 401)
    assert _decide("POST", SANDBOX_PATH, {"X-API-Key": OPERATOR_KEY})[0] is True
    assert _decide("POST", SANDBOX_PATH, _session_headers())[0] is True


def test_a_private_stack_with_no_key_makes_no_sandbox(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.delenv("THREEFOLD_API_KEYS")
    assert _decide("POST", SANDBOX_PATH)[:2] == (False, 403)


def test_neither_the_links_route_nor_the_sandbox_is_a_public_path() -> None:
    """PUBLIC_PATHS ignores the method, so listing either would open it on a private stack."""
    assert AUTH_LINKS_PATH not in PUBLIC_PATHS
    assert SANDBOX_PATH not in PUBLIC_PATHS


# ------------------------------------------------------------------ page reads


PAGE_READS = [
    "/api/overview",
    "/api/decisions",
    "/api/decision",
    "/api/projects",
    "/api/projects/Acme-Billing",
    f"/api/projects/{SANDBOX}",
]


@pytest.mark.parametrize("path", PAGE_READS)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_the_application_reads_are_open_on_the_public_demo(monkeypatch, path, method) -> None:
    monkeypatch.setenv("ENFORCE_API_KEY", "true")
    assert _decide(method, path)[0] is True


@pytest.mark.parametrize("path", PAGE_READS)
def test_the_application_reads_are_the_operators_on_a_private_stack(monkeypatch, path) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    allowed, status, kind = _decide("GET", path)
    assert (allowed, status, kind) == (False, 401, "urn:threefold:error:missing-credentials")
    assert _decide("GET", path, {"X-API-Key": OPERATOR_KEY})[0] is True
    assert _decide("GET", path, _session_headers())[0] is True


def test_a_private_stack_with_no_key_reads_nothing(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.delenv("THREEFOLD_API_KEYS")
    assert _decide("GET", "/api/overview")[1:] == (403, "urn:threefold:error:reads-private")


# ------------------------------------------------------------- always open


@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/api/auth/sessions"),
        ("DELETE", "/api/auth/sessions"),
        ("GET", "/api/auth/whoami"),
        ("GET", "/dashboard.html"),
        ("GET", "/app"),
        ("GET", "/assets/threefold.js"),
        ("HEAD", "/assets/threefold.css"),
        ("GET", "/install.py"),
        ("GET", "/dist/threefold-bundle.zip"),
        ("GET", "/dist/manifest.json"),
    ],
)
def test_sign_in_and_the_downloads_are_open_on_the_most_closed_stack(monkeypatch, method, path) -> None:
    """They authenticate by what they carry, or are how a team gets onto the stack."""
    monkeypatch.setenv("STAGE", "prod")
    monkeypatch.setenv("ENFORCE_API_KEY", "true")
    monkeypatch.setenv("PUBLIC_READS", "false")
    allowed, status, _ = _decide(method, path)
    assert allowed, f"{method} {path} was refused with {status}"


def test_an_asset_prefix_opens_only_reads(monkeypatch) -> None:
    monkeypatch.setenv("ENFORCE_API_KEY", "true")
    assert _decide("POST", "/assets/threefold.js")[:2] == (False, 401)


def test_minting_a_link_is_never_open(monkeypatch) -> None:
    """Not even on the public demo, where every read is."""
    assert _decide("POST", AUTH_LINKS_PATH)[:2] == (False, 401)
