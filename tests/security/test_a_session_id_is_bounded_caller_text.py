"""The session id is caller text a page publishes, so it is bounded and carries no credential.

Naming a session creates it. The two recording routes, the certificate and the
freeze each create the session they name, `/api/sessions` lists the id exactly
as it arrived, and `public_row()` rewrites only the project name and the
developer. So on a stack whose reads are public the id was the third piece of
caller text on that page, beside the operator and the reason a freeze writes —
and unlike those two it was unbounded and unredacted: a 448-character id
carrying a credential was listed in full.

Refused rather than cut or redacted, unlike the other two: an id shortened or
rewritten on the way in would address a different session than the caller
named, so a freeze would lock one row and answer for another.

Every route here names a session. A route added later that names one belongs in
this list, or it is a fourth way to write onto the same page.
"""
from __future__ import annotations

import json

import pytest

from threefold.interfaces.api_handlers import lambda_handler

# Written out rather than imported from the module under test: it is the
# bound a caller is told about and refused by, so a change to it is a change
# a caller would see.
SESSION_ID_LIMIT = 200

# Assembled rather than written out, as the rest of the suite assembles one, so
# this file does not itself carry a credential-shaped literal. It is synthetic.
SYNTHETIC_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
LEAKING_ID = "leaked-" + SYNTHETIC_TOKEN + "-" + "p" * 380
OVERLONG_ID = "s" * (SESSION_ID_LIMIT + 1)


def _call(method: str, path: str, body: dict | None = None):
    event = {
        "rawPath": f"/prod{path}",
        "headers": {"Content-Type": "application/json"},
        "requestContext": {"http": {"method": method}, "stage": "prod"},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    response = lambda_handler(event)
    return response["statusCode"], json.loads(response["body"])


def _freeze(session_id: str):
    return _call("POST", f"/sessions/{session_id}/terminate", {})


def _record(session_id: str):
    return _call(
        "POST",
        "/evaluate-tool-call",
        {
            "session_id": session_id,
            "project_name": "Acme-Core",
            "tool_name": "read_file",
            "action_type": "FILE_READ",
            "arguments": {"path": "README.md"},
        },
    )


def _adapter(session_id: str):
    return _call(
        "POST",
        "/adapter/universal-tool-call",
        {"session_id": session_id, "type": "tool_use", "name": "read_file", "input": {"path": "README.md"}},
    )


def _certify(session_id: str):
    return _call(
        "POST",
        "/issue-certificate",
        {
            "session_id": session_id,
            "evaluations": [{"status": "APPROVED", "rule_evaluations": {"SECRET_LEAKAGE_FREE": True}}],
        },
    )


NAMING_ROUTES = {
    "the freeze": _freeze,
    "the recording route": _record,
    "the universal adapter": _adapter,
    "the certificate": _certify,
}


def _listed_ids() -> list:
    status, listing = _call("GET", "/api/sessions", None)
    assert status == 200
    return [session["session_id"] for session in listing["sessions"]]


@pytest.mark.parametrize("route", sorted(NAMING_ROUTES))
def test_a_session_id_carrying_a_credential_is_refused(route) -> None:
    """Otherwise the id publishes the very thing the gate exists to stop."""
    status, problem = NAMING_ROUTES[route](LEAKING_ID)
    assert status == 400, problem
    assert problem["invalid_params"][0]["name"] == "session_id"
    # The refusal says what is wrong without quoting it back. `instance` is the
    # path the caller themselves sent, as it is in every problem document here.
    assert SYNTHETIC_TOKEN not in problem["detail"]
    assert LEAKING_ID not in _listed_ids(), f"{route} published it anyway"


@pytest.mark.parametrize("route", sorted(NAMING_ROUTES))
def test_a_session_id_past_the_bound_is_refused(route) -> None:
    status, problem = NAMING_ROUTES[route](OVERLONG_ID)
    assert status == 400, problem
    assert problem["invalid_params"][0]["name"] == "session_id"
    assert str(SESSION_ID_LIMIT) in problem["detail"]
    assert OVERLONG_ID not in _listed_ids(), f"{route} published it anyway"


@pytest.mark.parametrize("route", sorted(NAMING_ROUTES))
def test_an_ordinary_session_id_still_works_on_every_route(route) -> None:
    """The bound is far above what names a session: an agent's is a UUID."""
    session_id = f"sid-ok-{abs(hash(route)) % 10000}"
    # The certificate answers only for a session this service has governed, so
    # the call it attests to is recorded first, as the console's demo does.
    _record(session_id)
    status, body = NAMING_ROUTES[route](session_id)
    assert status == 200, body
    assert session_id in _listed_ids()


def test_a_session_id_at_the_bound_is_kept() -> None:
    """Refused one character later, so the boundary itself is pinned."""
    at_limit = "b" * SESSION_ID_LIMIT
    status, body = _record(at_limit)
    assert status == 200, body
    assert at_limit in _listed_ids()


def test_a_body_that_names_no_session_still_gets_the_default() -> None:
    """A caller that names none is unaffected: the route's own default names it."""
    status, body = _call(
        "POST",
        "/evaluate-tool-call",
        {"project_name": "Acme-Core", "tool_name": "read_file", "action_type": "FILE_READ", "arguments": {}},
    )
    assert status == 200, body
    assert body["session_id"] == "session-default"


def test_a_session_id_that_is_not_text_is_refused() -> None:
    """Stored as whatever the JSON held, it is shown on the page as that."""
    status, problem = _call(
        "POST",
        "/evaluate-tool-call",
        {
            "session_id": {"nested": "object"},
            "project_name": "Acme-Core",
            "tool_name": "read_file",
            "action_type": "FILE_READ",
            "arguments": {},
        },
    )
    assert status == 400, problem
    assert problem["invalid_params"][0]["name"] == "session_id"
