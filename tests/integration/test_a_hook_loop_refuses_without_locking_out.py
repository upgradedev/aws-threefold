"""The loop gate refuses the repeat, and who it locks out depends on who asked.

Day one halted any session whose third call repeated the first two. In front
of a developer's own agent that was the wrong trade: the refusal had already
stopped the repeat, and the halt went on to refuse everything else the
developer did in that session until an operator cleared it. So a hook's loop is
refused call by call and the session stays open. The dashboard's scenarios and
the pages keep the terminal halt, because a frozen session is what they show.

Repeats of a read or a poll (an agent waiting on CI, `git status` between
edits) are never a trip at all. The domain decides what counts as one; it is
stubbed here so the policy is tested whatever that classifier later says.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import json

import pytest

from threefold.application import evaluator as evaluator_module
from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import READ_OR_POLL_REPEAT, GovernanceEvaluator
from threefold.application.insights import summarise
from threefold.domain import boundary_guard as boundary_guard_module
from threefold.domain import loop_detector as loop_detector_module
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository

EDIT = {"file_path": "src/acme/service.py", "old_string": "a = 1", "new_string": "a = 2"}
POLL = {"command": "git status"}


def _call(session_id: str, origin: str, tool: str = "Edit", arguments=None, action: str = "FILE_WRITE"):
    return ToolCallRequestDTO(
        session_id=session_id,
        developer_id="anonymous",
        project_name="Acme-Loops",
        tool_name=tool,
        action_type=action,
        arguments=dict(arguments if arguments is not None else EDIT),
        projected_input_tokens=0,
        projected_output_tokens=0,
        origin=origin,
        agent="claude-code" if origin == "hook" else "page",
    )


def _poll(session_id: str, origin: str):
    return _call(session_id, origin, tool="Bash", arguments=POLL, action="COMMAND_EXEC")


def _stub_poll_classifier(invocation) -> bool:
    return invocation.tool_name == "Bash" and invocation.arguments.get("command") == "git status"


@pytest.fixture
def evaluator() -> GovernanceEvaluator:
    return GovernanceEvaluator(session_repo=DynamoDBSessionRepository())


@pytest.fixture
def no_classifier(monkeypatch):
    """Today's tree, whatever the domain track has merged: nothing is a read or a poll."""
    monkeypatch.delattr(loop_detector_module, "is_read_or_poll", raising=False)
    monkeypatch.delattr(boundary_guard_module, "is_read_or_poll", raising=False)


@pytest.fixture
def stub_classifier(monkeypatch, no_classifier):
    monkeypatch.setattr(loop_detector_module, "is_read_or_poll", _stub_poll_classifier, raising=False)


# ---------------------------------------------------------------- a hook's loop


def test_a_hook_loop_refuses_the_repeat_without_halting_the_session(evaluator, no_classifier) -> None:
    request = _call("hook-loop-refuse", "hook")
    assert evaluator.evaluate_tool_call(request).status == "APPROVED"
    assert evaluator.evaluate_tool_call(request).status == "APPROVED"

    third = evaluator.evaluate_tool_call(request)
    assert third.status == "BLOCKED_LOOP_DETECTED"
    assert third.rule_evaluations["LOOP_THRASHING_FREE"] is False
    assert third.session_tripped is False
    assert "not halted" in third.reason, "The reason must say the session is still open"
    assert "identical arguments" in third.reason, "and still say what the loop was"

    stored = evaluator.session_repo.get_session("hook-loop-refuse")
    assert stored.is_tripped is False, "No halt may be persisted for a hook's loop"
    assert stored.trip_reason is None


def test_the_next_different_call_in_a_hook_session_is_judged_normally(evaluator, no_classifier) -> None:
    request = _call("hook-loop-next", "hook")
    for _ in range(3):
        evaluator.evaluate_tool_call(request)

    different = _call("hook-loop-next", "hook", arguments=dict(EDIT, new_string="a = 3"))
    verdict = evaluator.evaluate_tool_call(different)
    assert verdict.status == "APPROVED"
    assert verdict.session_tripped is False


def test_the_same_repeat_keeps_being_refused(evaluator, no_classifier) -> None:
    """Not halting the session must not turn the fourth repeat into an approval."""
    request = _call("hook-loop-again", "hook")
    for _ in range(3):
        evaluator.evaluate_tool_call(request)
    fourth = evaluator.evaluate_tool_call(request)
    assert fourth.status == "BLOCKED_LOOP_DETECTED"
    assert fourth.session_tripped is False


def test_a_hook_loop_refusal_is_recorded_on_the_ledger(evaluator, no_classifier) -> None:
    request = _call("hook-loop-ledger", "hook")
    for _ in range(3):
        evaluator.evaluate_tool_call(request)
    rows = [row for row in evaluator.list_decisions() if row["session_id"] == "hook-loop-ledger"]
    refused = [row for row in rows if row["status"] == "BLOCKED_LOOP_DETECTED"]
    assert len(refused) == 1
    assert refused[0]["rule"] == "LOOP_THRASHING_FREE"
    assert refused[0]["origin"] == "hook"


def test_a_dry_run_hook_loop_is_approved_and_still_halts_nothing(evaluator, no_classifier) -> None:
    request = _call("hook-loop-dry", "hook")
    request.dry_run = True
    for _ in range(2):
        evaluator.evaluate_tool_call(request)
    third = evaluator.evaluate_tool_call(request)
    assert third.status == "APPROVED"
    assert third.dry_run is True
    assert "not halted" in third.observations[0]
    assert evaluator.session_repo.get_session("hook-loop-dry").is_tripped is False


# ---------------------------------------------------------------- who keeps the halt


def test_a_simulated_session_keeps_the_terminal_halt_even_from_a_hook(evaluator, no_classifier) -> None:
    request = _call("sim-hook-loop", "hook")
    for _ in range(2):
        evaluator.evaluate_tool_call(request)
    third = evaluator.evaluate_tool_call(request)
    assert third.status == "BLOCKED_LOOP_DETECTED"
    assert third.session_tripped is True

    different = _call("sim-hook-loop", "hook", tool="Read", arguments={"file_path": "README.md"}, action="FILE_READ")
    assert evaluator.evaluate_tool_call(different).status == "BLOCKED_CIRCUIT_BREAKER"


@pytest.mark.parametrize("origin", ["page", "ci", "unknown"])
def test_every_origin_but_a_hook_keeps_the_terminal_halt(evaluator, no_classifier, origin) -> None:
    """Pages by contract; the rest because that is what every caller had before origins existed."""
    request = _call(f"loop-terminal-{origin}", origin)
    for _ in range(2):
        evaluator.evaluate_tool_call(request)
    third = evaluator.evaluate_tool_call(request)
    assert third.status == "BLOCKED_LOOP_DETECTED"
    assert third.session_tripped is True
    assert evaluator.session_repo.get_session(f"loop-terminal-{origin}").is_tripped is True


def test_the_dashboard_scenario_still_freezes_its_session() -> None:
    """The ship gate's Scenario 1: call three refused and the session frozen.

    Run against whatever read-or-poll classifier the tree carries, so it fails
    the day an edit is ever classed as a poll.
    """
    from threefold.interfaces.api_handlers import lambda_handler

    response = lambda_handler(
        {
            "rawPath": "/prod/simulate-loop",
            "headers": {"Content-Type": "application/json"},
            "requestContext": {"http": {"method": "POST"}, "stage": "prod", "requestId": "loopv2x"},
        }
    )
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["status"] == "BLOCKED_LOOP_DETECTED"
    assert body["session_tripped"] is True


# ---------------------------------------------------------------- reads and polls


@pytest.mark.parametrize(
    "session_id, origin",
    [("hook-poll", "hook"), ("page-poll", "page"), ("sim-poll", "page"), ("unknown-poll", "unknown")],
)
def test_a_repeated_read_or_poll_is_approved_with_an_observation(evaluator, stub_classifier, session_id, origin) -> None:
    """Never a trip, whoever asks: repeating a read changes nothing."""
    request = _poll(session_id, origin)
    verdicts = [evaluator.evaluate_tool_call(request) for _ in range(5)]
    assert [v.status for v in verdicts] == ["APPROVED"] * 5
    assert all(v.rule_evaluations["LOOP_THRASHING_FREE"] for v in verdicts)
    assert verdicts[2].observations and verdicts[2].observations[-1].startswith(READ_OR_POLL_REPEAT)
    assert not verdicts[2].observed_rules, "No rule would refuse this, so none may be named"
    assert evaluator.session_repo.get_session(session_id).is_tripped is False


def test_a_poll_that_is_not_yet_a_loop_carries_no_observation(evaluator, stub_classifier) -> None:
    request = _poll("hook-poll-early", "hook")
    first = evaluator.evaluate_tool_call(request)
    second = evaluator.evaluate_tool_call(request)
    assert first.observations is None and second.observations is None


def test_a_repeated_poll_joins_the_history_like_any_approved_call(evaluator, stub_classifier) -> None:
    request = _poll("hook-poll-history", "hook")
    for _ in range(4):
        evaluator.evaluate_tool_call(request)
    assert len(evaluator.session_repo.get_session("hook-poll-history").history) == 4


def test_a_write_in_the_same_session_is_still_caught_looping(evaluator, stub_classifier) -> None:
    """Only the read is waved through. The classifier must not become a way round the gate."""
    request = _call("page-poll-then-edit", "page")
    evaluator.evaluate_tool_call(_poll("page-poll-then-edit", "page"))
    for _ in range(2):
        evaluator.evaluate_tool_call(request)
    third = evaluator.evaluate_tool_call(request)
    assert third.status == "BLOCKED_LOOP_DETECTED"
    assert third.session_tripped is True


def test_a_repeated_poll_is_on_the_ledger_but_not_counted_as_would_refuse(evaluator, stub_classifier) -> None:
    request = _poll("hook-poll-ledger", "hook")
    for _ in range(3):
        evaluator.evaluate_tool_call(request)
    rows = [row for row in evaluator.list_decisions() if row["session_id"] == "hook-poll-ledger"]
    noted = [row for row in rows if row["observed_reason"].startswith(READ_OR_POLL_REPEAT)]
    assert len(noted) == 1
    assert noted[0]["status"] == "APPROVED"
    assert noted[0]["observed_rules"] == []
    assert summarise(rows, 7)["totals"]["observed"] == 0, (
        "The console's observed column means a rule would have refused the call"
    )


def test_a_missing_classifier_counts_as_never_a_read(evaluator, no_classifier) -> None:
    request = _poll("hook-poll-none", "page")
    verdicts = [evaluator.evaluate_tool_call(request) for _ in range(3)]
    assert verdicts[2].status == "BLOCKED_LOOP_DETECTED"


def test_a_classifier_that_raises_counts_as_a_write(evaluator, monkeypatch, no_classifier) -> None:
    """A fault in the classifier must not wave a loop through."""

    def _broken(invocation):
        raise RuntimeError("simulated classifier fault")

    monkeypatch.setattr(loop_detector_module, "is_read_or_poll", _broken, raising=False)
    request = _poll("hook-poll-broken", "page")
    verdicts = [evaluator.evaluate_tool_call(request) for _ in range(3)]
    assert verdicts[2].status == "BLOCKED_LOOP_DETECTED"


def test_the_loop_detectors_classifier_is_asked_before_the_boundary_guards(monkeypatch, no_classifier) -> None:
    probe = _poll("classifier-order", "hook")
    invocation = type("Call", (), {"tool_name": probe.tool_name, "arguments": probe.arguments})()

    monkeypatch.setattr(boundary_guard_module, "is_read_or_poll", lambda call: True, raising=False)
    assert evaluator_module.is_read_or_poll(invocation) is True, "The boundary guard's is found when it is the only one"

    monkeypatch.setattr(loop_detector_module, "is_read_or_poll", lambda call: False, raising=False)
    assert evaluator_module.is_read_or_poll(invocation) is False


def test_the_domains_own_classifier_lets_a_hook_poll_git_status(evaluator) -> None:
    """Runs once the domain track's classifier is in the tree, and is skipped until then."""
    if not any(
        callable(getattr(module, "is_read_or_poll", None))
        for module in (loop_detector_module, boundary_guard_module)
    ):
        pytest.skip("is_read_or_poll is not in the domain yet")
    request = _poll("hook-poll-real", "hook")
    verdicts = [evaluator.evaluate_tool_call(request) for _ in range(4)]
    assert [v.status for v in verdicts] == ["APPROVED"] * 4
