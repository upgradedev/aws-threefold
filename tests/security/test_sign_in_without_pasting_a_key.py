"""The operator signs in to a private stack from the command line, never by pasting the key.

The key mints a single-use link; the page trades the code in it for a session;
the session is the operator everywhere the key is, except that it cannot mint a
link, so a session can never renew itself. Every request here goes through
lambda_handler, so the middleware and the route are tested together as the
deployment runs them. The keys are synthetic.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

import pytest

from threefold.infrastructure import auth_store, security_middleware
from threefold.infrastructure.auth_store import AuthStore
from threefold.interfaces.api_handlers import lambda_handler

OPERATOR_KEY = "acme-synthetic-operator-key-0001"
OTHER_KEY = "acme-synthetic-operator-key-0002"
DOMAIN = "acme0demo01.execute-api.eu-west-1.amazonaws.com"
POLICY = {
    "max_single_call_usd": 1.0,
    "max_session_budget_usd": 10.0,
    "loop_history_window": 6,
    "monomorphic_repetition_threshold": 3,
}


@pytest.fixture(autouse=True)
def _one_operator_key_and_a_clean_store(monkeypatch):
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)
    monkeypatch.delenv("STAGE", raising=False)
    monkeypatch.delenv("ENFORCE_API_KEY", raising=False)
    auth_store.reset_default_store(AuthStore())
    yield
    auth_store.reset_default_store()


@pytest.fixture(autouse=True)
def _restore_the_policy_the_handler_holds():
    """A policy write here must not change the thresholds later tests run under."""
    from threefold.interfaces.api_handlers import _evaluator

    before = _evaluator.policy_config
    yield
    _evaluator.update_policy(before)


def _call(method: str, path: str, body=None, headers=None, stage: str = "prod") -> tuple[int, dict, dict]:
    event = {
        # A $default stage is served at the root, with no prefix to strip.
        "rawPath": path if stage == "$default" else f"/{stage}{path}",
        "headers": headers or {},
        "requestContext": {"http": {"method": method}, "stage": stage, "domainName": DOMAIN},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    response = lambda_handler(event)
    return response["statusCode"], response["headers"], json.loads(response["body"] or "{}")


def _key(value: str = OPERATOR_KEY) -> dict:
    return {"X-API-Key": value}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mint(next_path: str | None = None, headers: dict | None = None) -> dict:
    status, _, body = _call("POST", "/api/auth/links", {} if next_path is None else {"next": next_path}, headers or _key())
    assert status == 200, body
    return body


def _session(headers: dict | None = None) -> dict:
    status, _, body = _call("POST", "/api/auth/sessions", {"code": _mint(headers=headers)["code"]})
    assert status == 200, body
    return body


# ------------------------------------------------------------------- the link


def test_the_key_mints_a_link_back_to_this_stacks_dashboard() -> None:
    status, headers, body = _call("POST", "/api/auth/links", {"next": "/projects/Acme-Billing"}, _key())
    assert status == 200
    assert set(body) == {"code", "expires_in", "url"}
    assert body["expires_in"] == 120
    assert body["url"] == (
        f"https://{DOMAIN}/prod/dashboard.html#/signin?code={quote(body['code'], safe='')}"
        "&next=%2Fprojects%2FAcme-Billing"
    )
    assert headers["Cache-Control"] == "no-store", "A response carrying a credential is never cached"


def test_a_link_with_no_destination_opens_the_overview() -> None:
    assert _mint()["url"].endswith("&next=%2Foverview")


def test_the_key_works_as_a_bearer_too() -> None:
    assert _call("POST", "/api/auth/links", {}, _bearer(OPERATOR_KEY))[0] == 200


def test_an_unstaged_deployment_links_to_its_root() -> None:
    status, _, body = _call("POST", "/api/auth/links", {}, _key(), stage="$default")
    assert status == 200
    assert body["url"].startswith(f"https://{DOMAIN}/dashboard.html#/signin?code=")


@pytest.mark.parametrize(
    "target",
    [
        "https://elsewhere.example/steal",
        "//elsewhere.example",
        "/\\elsewhere.example",
        "javascript:alert(1)",
        "projects/Acme-Billing",
        "/projects/Acme Billing",
        "/projects/Acme-Billing#frag",
        "/projects/\u00e9",
        "/" + "a" * 300,
        "",
        17,
        ["/overview"],
        {"path": "/overview"},
    ],
)
def test_a_destination_outside_the_dashboard_is_refused(target) -> None:
    status, _, body = _call("POST", "/api/auth/links", {"next": target}, _key())
    assert status == 400
    assert body["invalid_params"][0]["name"] == "next"


def test_nobody_mints_a_link_without_the_key() -> None:
    status, _, body = _call("POST", "/api/auth/links", {})
    assert status == 401
    assert body["type"] == "urn:threefold:error:missing-credentials"
    status, _, body = _call("POST", "/api/auth/links", {}, _key("acme-wrong-key"))
    assert status == 403
    assert body["type"] == "urn:threefold:error:invalid-credentials"


def test_a_stack_with_no_key_has_nobody_to_sign_in_as(monkeypatch) -> None:
    monkeypatch.delenv("THREEFOLD_API_KEYS")
    status, _, body = _call("POST", "/api/auth/links", {}, _key())
    assert status == 403
    assert body["type"] == "urn:threefold:error:sign-in-disabled"


def test_the_demo_placeholder_never_mints_a_link(monkeypatch) -> None:
    monkeypatch.delenv("THREEFOLD_API_KEYS")
    placeholder = security_middleware.DEFAULT_DEMO_API_KEY
    assert _call("POST", "/api/auth/links", {}, _key(placeholder))[0] == 403


def test_a_session_cannot_mint_a_link() -> None:
    """Otherwise a session could renew itself forever, and a stolen one would never lapse."""
    token = _session()["token"]
    status, _, body = _call("POST", "/api/auth/links", {}, _bearer(token))
    assert status == 403
    assert body["type"] == "urn:threefold:error:session-not-enough"


# --------------------------------------------------------------- the exchange


def test_a_code_becomes_a_twelve_hour_session() -> None:
    status, headers, body = _call("POST", "/api/auth/sessions", {"code": _mint()["code"]})
    assert status == 200
    assert set(body) == {"token", "expires_at", "ttl_seconds"}
    assert body["ttl_seconds"] == 43200
    assert body["token"].startswith(auth_store.SESSION_TOKEN_PREFIX)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", body["expires_at"])
    assert headers["Cache-Control"] == "no-store"


def test_a_code_signs_in_once() -> None:
    code = _mint()["code"]
    assert _call("POST", "/api/auth/sessions", {"code": code})[0] == 200
    status, _, body = _call("POST", "/api/auth/sessions", {"code": code})
    assert status == 401
    assert body["type"] == "urn:threefold:error:sign-in-code-invalid"


def test_a_code_lapses_after_two_minutes(monkeypatch) -> None:
    start = auth_store._now()
    code = _mint()["code"]
    monkeypatch.setattr(auth_store, "_now", lambda: start + 121)
    status, _, body = _call("POST", "/api/auth/sessions", {"code": code})
    assert status == 401
    assert body["type"] == "urn:threefold:error:sign-in-code-invalid"


def test_an_invented_code_is_refused() -> None:
    assert _call("POST", "/api/auth/sessions", {"code": "acme-invented-code"})[0] == 401


@pytest.mark.parametrize("body", [{}, {"code": ""}, {"code": 17}, {"code": None}, {"code": ["x"]}])
def test_an_exchange_without_a_code_is_the_callers_mistake(body) -> None:
    status, _, problem = _call("POST", "/api/auth/sessions", body)
    assert status == 400
    assert problem["invalid_params"][0]["name"] == "code"


def test_a_link_minted_by_a_key_since_removed_signs_nobody_in(monkeypatch) -> None:
    code = _mint()["code"]
    monkeypatch.setenv("THREEFOLD_API_KEYS", OTHER_KEY)
    assert _call("POST", "/api/auth/sessions", {"code": code})[0] == 401


def test_the_exchange_is_rate_limited_like_everything_else() -> None:
    statuses = [_call("POST", "/api/auth/sessions", {"code": f"acme-guess-{i}"})[0] for i in range(70)]
    assert 429 in statuses
    assert set(statuses) <= {401, 429}


# ------------------------------------------------------ the session is the operator


def test_a_session_writes_the_policy() -> None:
    token = _session()["token"]
    assert _call("POST", "/policy/config", POLICY)[0] == 401
    status, _, body = _call("POST", "/policy/config", POLICY, _bearer(token))
    assert status == 200, body
    assert body["status"] == "POLICY_UPDATED"


def test_a_session_reads_a_private_stack(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    token = _session()["token"]
    for path in ("/api/sessions", "/api/insights", "/rules", "/policy/config"):
        assert _call("GET", path)[0] == 401, f"{path} must be closed without the operator"
        status, _, body = _call("GET", path, headers=_bearer(token))
        assert status == 200, f"{path} refused a live session: {body}"


def test_a_session_resumes_as_the_key_would() -> None:
    """Past the middleware: the unknown session is the route's 404, not a 401."""
    token = _session()["token"]
    body = {"operator_name": "Acme On-Call", "reason": "Synthetic resume check"}
    assert _call("POST", "/sessions/acme-no-such-session/resume", body)[0] == 401
    assert _call("POST", "/sessions/acme-no-such-session/resume", body, _bearer(token))[0] == 404


def test_a_session_passes_where_every_call_needs_a_key(monkeypatch) -> None:
    monkeypatch.setenv("ENFORCE_API_KEY", "true")
    token = _session()["token"]
    call = {"tool_name": "read_file", "session_id": "acme-sign-in-check", "project_name": "Acme-Core"}
    assert _call("POST", "/evaluate-tool-call", call)[0] == 401
    assert _call("POST", "/evaluate-tool-call", call, _bearer(token))[0] == 200


def test_a_stale_api_key_header_does_not_hide_a_good_session() -> None:
    token = _session()["token"]
    headers = {"X-API-Key": "acme-stale-key", "Authorization": f"Bearer {token}"}
    assert _call("POST", "/policy/config", POLICY, headers)[0] == 200


def test_the_bearer_scheme_is_read_without_regard_to_case() -> None:
    token = _session()["token"]
    assert _call("POST", "/policy/config", POLICY, {"authorization": f"bearer {token}"})[0] == 200


def test_removing_the_key_ends_every_session_it_opened(monkeypatch) -> None:
    token = _session()["token"]
    monkeypatch.setenv("THREEFOLD_API_KEYS", OTHER_KEY)
    status, _, body = _call("POST", "/policy/config", POLICY, _bearer(token))
    assert status == 403
    assert body["type"] == "urn:threefold:error:invalid-credentials"


def test_a_lapsed_session_is_refused(monkeypatch) -> None:
    start = auth_store._now()
    token = _session()["token"]
    monkeypatch.setattr(auth_store, "_now", lambda: start + 43200 + 1)
    assert _call("POST", "/policy/config", POLICY, _bearer(token))[0] == 403


# ------------------------------------------------------------------ sign-out


def test_signing_out_ends_the_session_at_once() -> None:
    token = _session()["token"]
    assert _call("POST", "/policy/config", POLICY, _bearer(token))[0] == 200
    status, headers, body = _call("DELETE", "/api/auth/sessions", headers=_bearer(token))
    assert status == 200
    assert body["authenticated"] is False and body["via"] is None
    assert headers["Cache-Control"] == "no-store"
    assert _call("POST", "/policy/config", POLICY, _bearer(token))[0] == 403
    assert _call("GET", "/api/auth/whoami", headers=_bearer(token))[2]["authenticated"] is False


def test_signing_out_twice_or_with_nothing_is_not_an_error() -> None:
    token = _session()["token"]
    assert _call("DELETE", "/api/auth/sessions", headers=_bearer(token))[0] == 200
    assert _call("DELETE", "/api/auth/sessions", headers=_bearer(token))[0] == 200
    assert _call("DELETE", "/api/auth/sessions")[0] == 200


def test_signing_out_one_session_leaves_another() -> None:
    first, second = _session()["token"], _session()["token"]
    _call("DELETE", "/api/auth/sessions", headers=_bearer(first))
    assert _call("POST", "/policy/config", POLICY, _bearer(second))[0] == 200


def test_a_sign_out_the_store_did_not_confirm_is_reported(monkeypatch) -> None:
    token = _session()["token"]
    monkeypatch.setattr(auth_store.default_store(), "revoke", lambda _token: False)
    status, _, body = _call("DELETE", "/api/auth/sessions", headers=_bearer(token))
    assert status == 503
    assert body["type"] == "urn:threefold:error:sign-out-incomplete"


# -------------------------------------------------------------------- whoami


def test_whoami_for_a_stranger_on_the_public_demo() -> None:
    status, headers, body = _call("GET", "/api/auth/whoami")
    assert status == 200
    assert body == {
        "authenticated": False,
        "via": None,
        "expires_at": None,
        "reads_public": True,
        "sandbox_writes": True,
    }
    assert headers["Cache-Control"] == "no-store"


def test_whoami_for_the_key() -> None:
    body = _call("GET", "/api/auth/whoami", headers=_key())[2]
    assert body["authenticated"] is True and body["via"] == "key" and body["expires_at"] is None


def test_whoami_for_a_session_names_its_expiry() -> None:
    session = _session()
    body = _call("GET", "/api/auth/whoami", headers=_bearer(session["token"]))[2]
    assert body["authenticated"] is True
    assert body["via"] == "session"
    assert body["expires_at"] == session["expires_at"]


def test_whoami_on_a_private_stack(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    status, _, body = _call("GET", "/api/auth/whoami")
    assert status == 200, "whoami is open on every stack: it answers about what the caller carries"
    assert body["reads_public"] is False and body["sandbox_writes"] is False


@pytest.mark.parametrize("headers", [{"Authorization": "Bearer tfs_invented"}, {"X-API-Key": "acme-wrong"}])
def test_whoami_for_a_credential_that_does_not_hold(headers) -> None:
    status, _, body = _call("GET", "/api/auth/whoami", headers=headers)
    assert status == 200
    assert body["authenticated"] is False


# ------------------------------------------------------------- the comparison


def test_a_key_that_is_not_ascii_is_refused_rather_than_a_server_error(monkeypatch) -> None:
    """compare_digest raises TypeError on a str that is not ASCII."""
    assert _call("POST", "/policy/config", POLICY, _key("acme-cl\u00e9"))[0] == 403
    monkeypatch.setenv("STAGE", "prod")
    call = {"tool_name": "read_file", "session_id": "acme-ascii-check"}
    assert _call("POST", "/evaluate-tool-call", call, _key("acme-cl\u00e9"))[0] == 403


def test_keys_are_compared_in_constant_time(monkeypatch) -> None:
    seen = []
    real = security_middleware.hmac.compare_digest

    def spy(a, b):
        seen.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(security_middleware.hmac, "compare_digest", spy)
    assert _call("POST", "/policy/config", POLICY, _key())[0] == 200
    assert seen and all(pair == (bytes, bytes) for pair in seen)


def test_only_hashes_of_codes_and_tokens_are_stored() -> None:
    code = _mint()["code"]
    held = json.dumps(auth_store.default_store()._memory)
    assert code not in held
    token = _call("POST", "/api/auth/sessions", {"code": code})[2]["token"]
    held = json.dumps(auth_store.default_store()._memory)
    assert token not in held and OPERATOR_KEY not in held
