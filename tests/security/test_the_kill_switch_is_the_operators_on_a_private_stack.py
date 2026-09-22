"""Freezing a session is open where the reads are, and the operator's where they are not.

The kill switch is deliberately anonymous on the public demo: a visitor has no
key to present, and freezing a session can only stop work. STATE.md records it
that way.

On the stack deployed with PublicReads=false the sessions are the owner's own
agent sessions, and the ids that name them are exactly what the private reads
hold back. A stranger who had one could freeze a real session, and once that
project is in Enforce every later call in it comes back
BLOCKED_CIRCUIT_BREAKER. So there the freeze climbs the same ladder the reads
climb, and clearing it already climbed: POST /sessions/<id>/resume has needed
the operator on every stack since it existed.
"""
from __future__ import annotations

import json

import pytest

from threefold.interfaces.api_handlers import lambda_handler

OPERATOR_KEY = "acme-operator-key-kill-switch"
BODY = {"operator_name": "Acme on-call", "reason": "review probe"}


def _freeze(session_id: str, headers: dict | None = None):
    response = lambda_handler(
        {
            "rawPath": f"/prod/sessions/{session_id}/terminate",
            "headers": dict({"Content-Type": "application/json"}, **(headers or {})),
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
            "body": json.dumps(BODY),
        }
    )
    return response["statusCode"], json.loads(response["body"])


def _is_tripped(session_id: str) -> bool:
    from threefold.interfaces.api_handlers import _evaluator

    session = _evaluator.session_repo.get_session(session_id)
    return bool(session and session.is_tripped)


def test_a_stranger_cannot_freeze_a_session_on_a_private_stack(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)

    status, problem = _freeze("kill-private-1")
    assert status == 401
    assert problem["type"] == "urn:threefold:error:missing-credentials"
    assert _is_tripped("kill-private-1") is False


def test_the_freeze_climbs_the_same_ladder_the_private_reads_do(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)

    assert _freeze("kill-private-2")[0] == 401, "A missing key is 401"
    assert _freeze("kill-private-2", {"X-API-Key": "not-the-key"})[0] == 403, "A wrong key is 403"
    status, body = _freeze("kill-private-2", {"X-API-Key": OPERATOR_KEY})
    assert status == 200 and body["status"] == "SESSION_FROZEN", "The operator freezes it"


def test_a_private_stack_with_no_key_configured_freezes_nothing(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.delenv("THREEFOLD_API_KEYS", raising=False)

    status, problem = _freeze("kill-private-3")
    assert status == 403
    assert problem["type"] == "urn:threefold:error:reads-private"
    assert _is_tripped("kill-private-3") is False


@pytest.mark.parametrize("session_id", ["kill-public-1", "kill public <2>"])
def test_the_public_demo_still_answers_the_kill_switch_anonymously(monkeypatch, session_id) -> None:
    """The ship gate's demo has no key to present, and STATE.md says this is open there."""
    import urllib.parse

    monkeypatch.setenv("PUBLIC_READS", "true")
    status, body = _freeze(urllib.parse.quote(session_id, safe=""))
    assert status == 200
    assert body["status"] == "SESSION_FROZEN"
    assert body["session_id"] == session_id


def test_the_predicate_names_the_freeze_and_nothing_beside_it() -> None:
    from threefold.infrastructure.security_middleware import is_session_terminate

    assert is_session_terminate("POST", "/sessions/acme-1/terminate")
    assert is_session_terminate("post", "/sessions/acme-1/terminate")
    assert not is_session_terminate("GET", "/sessions/acme-1/terminate")
    assert not is_session_terminate("POST", "/sessions/acme-1/resume")
    assert not is_session_terminate("POST", "/sessions/acme-1")
