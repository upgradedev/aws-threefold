"""A read that arrives as POST is as private as the same data read by GET.

The stack that carries real use is deployed with `PublicReads=false`, and its
sessions and ledger are the operator's alone. Closing every GET left the same
data reachable one verb over: `POST /issue-certificate` answers for a session
the service has governed, with its project, its developer and its spend, so an
anonymous caller who knew a session id read the row `/api/sessions` had just
refused them. The two scenarios seeded synthetic calls into a ledger that
carries real use, which is the reason `POST /api/sandbox` is already closed
there.

Three things are pinned:

* the three POSTs are the operator's on a private stack, with the same ladder
  every other private route climbs;
* they are untouched on the public demo, where the ship gate needs them
  anonymous; and
* the recording routes stay open there deliberately, because a machine this
  stack governs reports to it without a key. Closing them would stop the work
  the stack exists to record rather than a reader of it. What that leaves open
  is named in the test itself, because it is more than a read: a caller who
  knows a session id can send a call into it and trip its circuit breaker. A
  later track that decides otherwise changes that test on purpose rather than
  by accident.
"""
from __future__ import annotations

import json

import pytest

from threefold.interfaces.api_handlers import lambda_handler

OPERATOR_KEY = "acme-operator-key-private-posts"
OWNER_SESSION = "owner-private-post-1"
OWNER_PROJECT = "Acme-Secret"

# Written out rather than imported from the middleware: these are the paths a
# caller sends, so a change to one is a change a caller would see.
CLOSED_POSTS = {
    "/issue-certificate": {
        "session_id": OWNER_SESSION,
        "evaluations": [{"status": "APPROVED", "rule_evaluations": {"SECRET_LEAKAGE_FREE": True}}],
    },
    "/simulate-loop": {},
    "/simulate-secret": {},
}


def _post(path: str, body: dict, headers: dict | None = None):
    response = lambda_handler(
        {
            "rawPath": f"/prod{path}",
            "headers": dict({"Content-Type": "application/json"}, **(headers or {})),
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
            "body": json.dumps(body),
        }
    )
    return response["statusCode"], json.loads(response["body"])


def _record_the_owners_call(headers: dict | None = None):
    """One call of the owner's, so there is a private session to answer for."""
    return _post(
        "/evaluate-tool-call",
        {
            "session_id": OWNER_SESSION,
            "project_name": OWNER_PROJECT,
            "developer": "acme-owner",
            "tool_name": "read_file",
            "action_type": "FILE_READ",
            "arguments": {"path": "README.md"},
        },
        headers,
    )


@pytest.mark.parametrize("path", sorted(CLOSED_POSTS))
def test_a_caller_with_no_credential_is_refused(monkeypatch, path) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)
    _record_the_owners_call({"X-API-Key": OPERATOR_KEY})

    status, problem = _post(path, CLOSED_POSTS[path])
    assert status == 401, problem
    assert problem["type"] == "urn:threefold:error:missing-credentials"


def test_the_certificate_no_longer_answers_a_private_session_to_a_stranger(monkeypatch) -> None:
    """The row the sessions read had just refused, one verb over."""
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)
    _record_the_owners_call({"X-API-Key": OPERATOR_KEY})

    status, refused = _post("/issue-certificate", CLOSED_POSTS["/issue-certificate"])
    assert status == 401
    assert OWNER_PROJECT not in json.dumps(refused)

    listing = lambda_handler(
        {
            "rawPath": "/prod/api/sessions",
            "headers": {},
            "requestContext": {"http": {"method": "GET"}, "stage": "prod"},
        }
    )
    assert listing["statusCode"] == 401, "the GET was already closed; the POST now agrees"


@pytest.mark.parametrize("path", sorted(CLOSED_POSTS))
def test_a_wrong_key_is_forbidden_and_the_operators_key_works(monkeypatch, path) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)
    _record_the_owners_call({"X-API-Key": OPERATOR_KEY})

    assert _post(path, CLOSED_POSTS[path], {"X-API-Key": "not-the-key"})[0] == 403
    status, body = _post(path, CLOSED_POSTS[path], {"X-API-Key": OPERATOR_KEY})
    assert status == 200, body


@pytest.mark.parametrize("path", sorted(CLOSED_POSTS))
def test_a_private_stack_with_no_key_configured_says_so(monkeypatch, path) -> None:
    """The same answer the private reads give: there is nobody to be here."""
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.delenv("THREEFOLD_API_KEYS", raising=False)
    status, problem = _post(path, CLOSED_POSTS[path])
    assert status == 403, problem
    assert problem["type"] == "urn:threefold:error:reads-private"


@pytest.mark.parametrize("path", sorted(CLOSED_POSTS))
def test_the_public_demo_answers_every_one_of_them_anonymously(monkeypatch, path) -> None:
    """PublicReads defaults to true and the sixty-second visitor path depends on it."""
    monkeypatch.setenv("PUBLIC_READS", "true")
    _record_the_owners_call()
    status, body = _post(path, CLOSED_POSTS[path])
    assert status == 200, body


def test_the_recording_routes_stay_open_on_a_private_stack(monkeypatch) -> None:
    """Deliberate: this is how a machine this stack governs reports to it.

    The hook sends its calls here, and on the stack carrying the owner's real
    use that report arrives with no key. Closing this would stop the work the
    stack exists to record rather than a reader of it, so it stays open until
    the machines that report to it carry a key.

    The price of that is not only a read: the spend gate believes the token
    counts a caller declares, so a caller who knows a session id can send one
    call into it and trip its circuit breaker, and the owner's next call in
    that session is refused. It is the end state the kill switch is closed here
    to prevent, reached the other way, and it is written down rather than left
    to be found.
    """
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)
    status, body = _record_the_owners_call()
    assert status == 200, body

    status, adapted = _post(
        "/adapter/universal-tool-call",
        {"session_id": OWNER_SESSION, "type": "tool_use", "name": "read_file", "input": {"path": "README.md"}},
    )
    assert status == 200, adapted
