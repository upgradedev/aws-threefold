"""A halted session can be resumed by an operator, and the resume is on the record.

Until this route existed a halt was permanent: the terminal-state guard refuses
every write that would clear it, which is right for a worker and wrong for the
person responsible for the session. The resume is closed behind the operator
key, names who cleared the halt and why, and keeps what led to it: the history
and the spend stay, so the gates go on applying to whatever comes next.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator, SessionNotHaltedError
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository
from threefold.infrastructure.idempotency import global_idempotency_cache

OPERATOR = {"Content-Type": "application/json", "X-API-Key": "operator-key-1"}
WHO = {"operator_name": "Acme On-call", "reason": "Loop was a flaky test, fixed upstream"}


def _repeat(session_id: str, origin: str = "page") -> ToolCallRequestDTO:
    return ToolCallRequestDTO(
        session_id=session_id,
        developer_id="anonymous",
        project_name="Acme-Resume",
        tool_name="run_tests",
        action_type="COMMAND_EXEC",
        arguments={"cmd": "pytest -q"},
        projected_input_tokens=1000,
        projected_output_tokens=200,
        origin=origin,
    )


def _different(session_id: str) -> ToolCallRequestDTO:
    return ToolCallRequestDTO(
        session_id=session_id,
        developer_id="anonymous",
        project_name="Acme-Resume",
        tool_name="read_file",
        action_type="FILE_READ",
        arguments={"path": "src/acme/app.py"},
        projected_input_tokens=100,
        projected_output_tokens=50,
        origin="page",
    )


def _halt(evaluator: GovernanceEvaluator, session_id: str) -> None:
    for _ in range(3):
        evaluator.evaluate_tool_call(_repeat(session_id))
    assert evaluator.session_repo.get_session(session_id).is_tripped


# ---------------------------------------------------------------- the evaluator


def test_a_resume_clears_the_halt_and_the_next_call_is_judged_normally() -> None:
    evaluator = GovernanceEvaluator(session_repo=DynamoDBSessionRepository())
    _halt(evaluator, "resume-clears")
    assert evaluator.evaluate_tool_call(_different("resume-clears")).status == "BLOCKED_CIRCUIT_BREAKER"

    resumed = evaluator.resume_session("resume-clears", "Acme On-call", "flaky test")
    assert resumed.is_tripped is False
    assert evaluator.evaluate_tool_call(_different("resume-clears")).status == "APPROVED"


def test_a_resume_records_who_why_when_and_the_halt_it_cleared() -> None:
    repo = DynamoDBSessionRepository()
    evaluator = GovernanceEvaluator(session_repo=repo)
    _halt(evaluator, "resume-record")
    halted_for = repo.get_session("resume-record").trip_reason

    evaluator.resume_session("resume-record", "Acme On-call", "flaky test")
    stored = repo.get_session("resume-record")
    assert stored.resumed_by == "Acme On-call"
    assert stored.resume_reason == "flaky test"
    assert stored.resumed_from == halted_for
    assert stored.resumed_at


def test_a_resumed_session_no_longer_carries_the_reason_it_was_halted_for() -> None:
    """The sessions console shows trip_reason, and a running session must not show an old halt.

    The halt it cleared is kept, under resumed_from, so nothing is lost by
    clearing it here.
    """
    repo = DynamoDBSessionRepository()
    evaluator = GovernanceEvaluator(session_repo=repo)
    _halt(evaluator, "resume-reason-cleared")
    assert repo.get_session("resume-reason-cleared").trip_reason

    evaluator.resume_session("resume-reason-cleared", "Acme On-call", "flaky test")
    assert repo.get_session("resume-reason-cleared").trip_reason is None
    listed = next(row for row in repo.list_sessions(limit=1000) if row["session_id"] == "resume-reason-cleared")
    assert listed["is_tripped"] is False
    assert listed["trip_reason"] == ""


def test_the_record_survives_the_approved_calls_after_it() -> None:
    """An approval rewrites the whole session row, and a record it dropped would be no record."""
    repo = DynamoDBSessionRepository()
    evaluator = GovernanceEvaluator(session_repo=repo)
    _halt(evaluator, "resume-survives")
    evaluator.resume_session("resume-survives", "Acme On-call", "flaky test")

    assert evaluator.evaluate_tool_call(_different("resume-survives")).status == "APPROVED"
    assert repo.get_session("resume-survives").resumed_by == "Acme On-call"


def test_a_resume_keeps_the_history_and_the_spend() -> None:
    repo = DynamoDBSessionRepository()
    evaluator = GovernanceEvaluator(session_repo=repo)
    _halt(evaluator, "resume-keeps")
    before = repo.get_session("resume-keeps")

    evaluator.resume_session("resume-keeps", "Acme On-call", "flaky test")
    after = repo.get_session("resume-keeps")
    assert after.total_cost_usd == before.total_cost_usd
    assert len(after.history) == len(before.history)


def test_a_resumed_scenario_session_that_repeats_the_call_halts_again() -> None:
    """The gates still apply: the resume clears the halt, not the loop that caused it."""
    evaluator = GovernanceEvaluator(session_repo=DynamoDBSessionRepository())
    _halt(evaluator, "resume-repeats")
    evaluator.resume_session("resume-repeats", "Acme On-call", "flaky test")

    again = evaluator.evaluate_tool_call(_repeat("resume-repeats"))
    assert again.status == "BLOCKED_LOOP_DETECTED"
    assert again.session_tripped is True


def test_a_terminated_session_is_no_longer_marked_terminated_once_resumed() -> None:
    repo = DynamoDBSessionRepository()
    evaluator = GovernanceEvaluator(session_repo=repo)
    evaluator.evaluate_tool_call(_different("resume-terminated"))
    evaluator.terminate_session("resume-terminated", "Acme Security", "suspicious actuation")

    evaluator.resume_session("resume-terminated", "Acme Security", "cleared after review")
    stored = repo.get_session("resume-terminated")
    assert stored.is_terminated is False
    assert stored.terminated_by is None
    assert stored.termination_reason is None
    assert stored.resumed_from.startswith("MANUALLY_TERMINATED by Acme Security")


def test_resuming_an_unknown_session_answers_none_and_creates_nothing() -> None:
    repo = DynamoDBSessionRepository()
    evaluator = GovernanceEvaluator(session_repo=repo)
    assert evaluator.resume_session("resume-never-seen", "Acme On-call", "typo") is None
    assert repo.get_session("resume-never-seen") is None


def test_resuming_a_running_session_is_refused_and_records_nothing() -> None:
    repo = DynamoDBSessionRepository()
    evaluator = GovernanceEvaluator(session_repo=repo)
    evaluator.evaluate_tool_call(_different("resume-running"))
    with pytest.raises(SessionNotHaltedError):
        evaluator.resume_session("resume-running", "Acme On-call", "just in case")
    assert getattr(repo.get_session("resume-running"), "resumed_by", None) is None


# ---------------------------------------------------------------- the route


def _post(path: str, body=None, headers=None):
    from threefold.interfaces.api_handlers import lambda_handler

    event = {
        "rawPath": f"/prod{path}",
        "headers": headers or {"Content-Type": "application/json"},
        "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    response = lambda_handler(event)
    return response["statusCode"], json.loads(response["body"])


@pytest.fixture
def handler_evaluator(monkeypatch):
    from threefold.interfaces.api_handlers import _evaluator

    monkeypatch.setenv("THREEFOLD_API_KEYS", "operator-key-1")
    return _evaluator


def test_the_route_resumes_a_halted_session(handler_evaluator) -> None:
    _halt(handler_evaluator, "route-resume-ok")
    status, body = _post("/sessions/route-resume-ok/resume", WHO, OPERATOR)
    assert status == 200
    assert body["status"] == "SESSION_RESUMED"
    assert body["session_id"] == "route-resume-ok"
    assert body["operator"] == WHO["operator_name"]
    assert body["reason"] == WHO["reason"]
    assert body["is_tripped"] is False
    assert "identical" in body["previous_trip_reason"]
    assert body["resumed_at"]
    assert handler_evaluator.session_repo.get_session("route-resume-ok").is_tripped is False


def test_the_routes_note_does_not_promise_a_hook_session_a_halt_it_never_gets(handler_evaluator) -> None:
    """A hook's loop is refused call by call and never halts, and the note an operator reads must say so.

    The hook session here was halted by the kill switch, which is how a hook
    session is halted now that its loops are not. After the resume the repeat
    is refused and the session stays open, which is what the note has to say.
    """
    session_id = "route-resume-hook"
    handler_evaluator.evaluate_tool_call(_repeat(session_id, origin="hook"))
    handler_evaluator.terminate_session(session_id, "Acme Security", "suspicious actuation")

    status, body = _post(f"/sessions/{session_id}/resume", WHO, OPERATOR)
    assert status == 200
    assert "page or scenario session that repeats the call that halted it halts again" in body["note"]
    assert "A hook session's repeat is refused but does not halt it" in body["note"]

    verdicts = [handler_evaluator.evaluate_tool_call(_repeat(session_id, origin="hook")) for _ in range(3)]
    assert verdicts[-1].status == "BLOCKED_LOOP_DETECTED"
    assert verdicts[-1].session_tripped is False
    assert handler_evaluator.session_repo.get_session(session_id).is_tripped is False


def test_the_route_decodes_the_session_id_as_the_read_does(handler_evaluator) -> None:
    session_id = "route resume <angle>"
    _halt(handler_evaluator, session_id)
    status, body = _post(f"/sessions/{quote(session_id)}/resume", WHO, OPERATOR)
    assert status == 200
    assert body["session_id"] == session_id


def test_the_route_answers_404_for_an_unknown_session_and_creates_nothing(handler_evaluator) -> None:
    status, problem = _post("/sessions/route-resume-unknown/resume", WHO, OPERATOR)
    assert status == 404
    assert problem["type"] == "urn:threefold:error:session-not-found"
    assert handler_evaluator.session_repo.get_session("route-resume-unknown") is None


def test_the_route_answers_409_for_a_session_that_is_not_halted(handler_evaluator) -> None:
    handler_evaluator.evaluate_tool_call(_different("route-resume-running"))
    status, problem = _post("/sessions/route-resume-running/resume", WHO, OPERATOR)
    assert status == 409
    assert problem["type"] == "urn:threefold:error:session-not-halted"


@pytest.mark.parametrize(
    "case, body, field",
    [
        ("no-name", {"reason": "no name"}, "operator_name"),
        ("no-reason", {"operator_name": "Acme On-call"}, "reason"),
        ("blank-name", {"operator_name": "   ", "reason": "blank name"}, "operator_name"),
        ("name-not-text", {"operator_name": 7, "reason": "not text"}, "operator_name"),
        ("reason-too-long", {"operator_name": "Acme On-call", "reason": "x" * 241}, "reason"),
    ],
)
def test_the_route_needs_who_and_why(handler_evaluator, case, body, field) -> None:
    """The record is the reason the route is closed; a resume without one is refused, not defaulted."""
    session_id = f"route-resume-bad-{case}"
    _halt(handler_evaluator, session_id)
    status, problem = _post(f"/sessions/{session_id}/resume", body, OPERATOR)
    assert status == 400
    assert problem["invalid_params"][0]["name"] == field
    assert handler_evaluator.session_repo.get_session(session_id).is_tripped is True


def test_a_retried_resume_with_the_same_idempotency_key_gets_the_first_answer(handler_evaluator) -> None:
    """An operator whose first request timed out is told it worked, not that nothing was halted."""
    _halt(handler_evaluator, "route-resume-retry")
    headers = dict(OPERATOR, **{"Idempotency-Key": "acme-resume-retry-1"})
    try:
        first = _post("/sessions/route-resume-retry/resume", WHO, headers)
        second = _post("/sessions/route-resume-retry/resume", WHO, headers)
    finally:
        global_idempotency_cache.clear()
    assert first[0] == 200
    assert second == first


def test_the_route_is_closed_to_an_anonymous_caller(handler_evaluator) -> None:
    _halt(handler_evaluator, "route-resume-anon")
    status, problem = _post("/sessions/route-resume-anon/resume", WHO)
    assert status == 401
    assert handler_evaluator.session_repo.get_session("route-resume-anon").is_tripped is True
