"""Freezing a session is the operator's, except a scenario's on the public demo.

The kill switch used to be anonymous wherever the reads were public. That let
a stranger freeze any session they could name, including the fleet's
predictable ids, and on a stack with no operator key the freeze could never
be cleared, since the resume already needs one. So on the public demo only a
scenario session (sim-*) still freezes anonymously, for the first screen's
freeze button; any other id needs the operator there too. STATE.md records it
that way.

On the stack deployed with PublicReads=false the sessions are the owner's own
agent sessions, and the ids that name them are exactly what the private reads
hold back. So there the freeze climbs the same ladder the reads climb, and
clearing it already climbed: POST /sessions/<id>/resume has needed the
operator on every stack since it existed.

A freeze never creates the session it names: freezing an id nobody recorded
is 404, like resuming one.
"""
from __future__ import annotations

import json

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


def _record(session_id: str) -> None:
    """One ordinary call, so the freeze below names a session that exists."""
    response = lambda_handler(
        {
            "rawPath": "/prod/evaluate-tool-call",
            "headers": {"Content-Type": "application/json"},
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
            "body": json.dumps({
                "session_id": session_id,
                "project_name": "Acme-Ledger",
                "tool_name": "read_file",
                "action_type": "FILE_READ",
                "arguments": {"path": "README.md"},
            }),
        }
    )
    assert response["statusCode"] == 200


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
    _record("kill-private-2")
    status, body = _freeze("kill-private-2", {"X-API-Key": OPERATOR_KEY})
    assert status == 200 and body["status"] == "SESSION_FROZEN", "The operator freezes it"


def test_a_private_stack_with_no_key_configured_freezes_nothing(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.delenv("THREEFOLD_API_KEYS", raising=False)

    status, problem = _freeze("kill-private-3")
    assert status == 403
    assert problem["type"] == "urn:threefold:error:reads-private"
    assert _is_tripped("kill-private-3") is False


def test_a_scenario_freeze_stays_anonymous_on_the_public_demo(monkeypatch) -> None:
    """The first screen's freeze button acts on the last scenario's session."""
    monkeypatch.setenv("PUBLIC_READS", "true")
    monkeypatch.delenv("THREEFOLD_API_KEYS", raising=False)
    _record("sim-public-1")
    status, body = _freeze("sim-public-1")
    assert status == 200
    assert body["status"] == "SESSION_FROZEN"
    assert body["session_id"] == "sim-public-1"


def test_a_stranger_cannot_freeze_a_real_session_on_the_public_demo(monkeypatch) -> None:
    """Fleet and live ids are predictable, and the freeze could never be cleared."""
    monkeypatch.setenv("PUBLIC_READS", "true")
    monkeypatch.delenv("THREEFOLD_API_KEYS", raising=False)
    _record("kill-public-1")
    status, problem = _freeze("kill-public-1")
    assert status == 403
    assert problem["type"] == "urn:threefold:error:freeze-needs-operator"
    assert _is_tripped("kill-public-1") is False


def test_the_operator_freezes_any_recorded_session_on_the_public_demo(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_READS", "true")
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)
    _record("kill-public-2")
    status, body = _freeze("kill-public-2", {"X-API-Key": OPERATOR_KEY})
    assert status == 200 and body["status"] == "SESSION_FROZEN"


def test_freezing_an_id_nobody_recorded_is_404(monkeypatch) -> None:
    """A freeze mints no row, like a resume: there is nothing to lock."""
    monkeypatch.setenv("PUBLIC_READS", "true")
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)
    status, problem = _freeze("sim-unknown-9", {"X-API-Key": OPERATOR_KEY})
    assert status == 404
    assert problem["type"] == "urn:threefold:error:session-not-found"
    from threefold.interfaces.api_handlers import _evaluator

    assert _evaluator.session_repo.get_session("sim-unknown-9") is None


def test_the_predicate_names_the_freeze_and_nothing_beside_it() -> None:
    from threefold.infrastructure.security_middleware import is_session_terminate

    assert is_session_terminate("POST", "/sessions/acme-1/terminate")
    assert is_session_terminate("post", "/sessions/acme-1/terminate")
    assert not is_session_terminate("GET", "/sessions/acme-1/terminate")
    assert not is_session_terminate("POST", "/sessions/acme-1/resume")
    assert not is_session_terminate("POST", "/sessions/acme-1")


def test_the_scenario_predicate_names_only_a_scenario_freeze() -> None:
    from threefold.infrastructure.security_middleware import is_scenario_session_terminate

    assert is_scenario_session_terminate("POST", "/sessions/sim-loop-abc123/terminate")
    assert is_scenario_session_terminate("POST", "/sessions/sim-sec-abc123/terminate")
    assert not is_scenario_session_terminate("POST", "/sessions/acme-1/terminate")
    assert not is_scenario_session_terminate("POST", "/sessions/xsim-1/terminate")
    assert not is_scenario_session_terminate("GET", "/sessions/sim-loop-abc123/terminate")
    assert not is_scenario_session_terminate("POST", "/sessions/sim-loop-abc123/resume")
    assert is_scenario_session_terminate("POST", "/sessions/sim-%41bc/terminate")
