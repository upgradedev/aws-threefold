"""Request v2 adds who sent a call and how, and changes nothing for a v1 caller.

The hooks now say which agent they sit in front of, whether a hook, a page or
CI sent the call, whether they want a sentence explaining the verdict, and
whether this is a dry run. The pages and every caller written before those
fields existed send none of them, and must get exactly the answer they got.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from threefold.interfaces.api_handlers import _evaluator, lambda_handler


def _post(body, path: str = "/evaluate-tool-call") -> tuple[int, dict]:
    response = lambda_handler(
        {
            "rawPath": f"/prod{path}",
            "headers": {"Content-Type": "application/json"},
            "body": body if isinstance(body, str) else json.dumps(body),
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
        }
    )
    return response["statusCode"], json.loads(response["body"])


def _ledger_row(session_id: str) -> dict:
    rows = [r for r in _evaluator.list_decisions(days=1) if r["session_id"] == session_id]
    assert rows, f"No ledger row for {session_id}"
    return rows[0]


def _v1(session_id: str, **overrides) -> dict:
    body = {
        "session_id": session_id,
        "developer_id": "acme-dev-v1",
        "project_name": "Acme-Requests",
        "tool_name": "view_file",
        "action_type": "FILE_READ",
        "arguments": {"path": "README.md"},
        "projected_input_tokens": 100,
        "projected_output_tokens": 50,
        "budget_usd": 5.0,
    }
    body.update(overrides)
    return body


DOMAIN_WRITE = {
    "tool_name": "Write",
    "action_type": "FILE_WRITE",
    "arguments": {"file_path": "src/domain/order.py", "content": "import boto3"},
}


def test_a_v1_body_is_answered_as_it_always_was() -> None:
    status, verdict = _post(_v1("v2-plain-v1"))
    assert status == 200
    assert verdict["status"] == "APPROVED"
    assert verdict["warnings"] == [], "A well-formed v1 body has nothing to warn about"
    assert verdict["dry_run"] is False

    row = _ledger_row("v2-plain-v1")
    assert row["agent"] == "unknown" and row["origin"] == "unknown"
    assert row["dry_run"] is False
    assert row["developer_id"] == "acme-dev-v1"


def test_agent_and_origin_are_stored_on_the_ledger_row() -> None:
    status, _ = _post(_v1("v2-agent-origin", agent="codex", origin="hook", explain=False))
    assert status == 200
    row = _ledger_row("v2-agent-origin")
    assert row["agent"] == "codex"
    assert row["origin"] == "hook"


def test_an_agent_outside_the_known_set_is_kept_as_unknown_and_the_caller_is_told() -> None:
    """Both fields are counted by; a free-text value would be one more thing a caller fills."""
    status, verdict = _post(_v1("v2-odd-agent", agent="<script>acme</script>", origin="carrier-pigeon"))
    assert status == 200
    row = _ledger_row("v2-odd-agent")
    assert row["agent"] == "unknown" and row["origin"] == "unknown"
    assert any(w.startswith("agent is not one of") for w in verdict["warnings"])
    assert any(w.startswith("origin is not one of") for w in verdict["warnings"])


def test_developer_is_the_v2_name_and_wins_over_developer_id() -> None:
    local_hash = hashlib.sha256(b"acme-laptop").hexdigest()[:12]
    _post(_v1("v2-developer", developer=local_hash))
    assert _ledger_row("v2-developer")["developer_id"] == local_hash


def test_a_caller_that_names_no_developer_is_anonymous() -> None:
    body = _v1("v2-anonymous")
    body.pop("developer_id")
    _post(body)
    assert _ledger_row("v2-anonymous")["developer_id"] == "anonymous"


@pytest.mark.parametrize("field, value", [("explain", "sometimes"), ("dry_run", [True]), ("explain", 2)])
def test_a_flag_that_is_not_a_yes_or_no_is_refused(field: str, value) -> None:
    status, problem = _post(_v1("v2-bad-flag", **{field: value}))
    assert status == 400
    assert problem["invalid_params"][0]["name"] == field


@pytest.mark.parametrize("field", ["budget_usd", "projected_input_tokens"])
@pytest.mark.parametrize("value", ["plenty", None, [1], {"n": 1}])
def test_a_number_that_is_not_one_is_a_400_not_a_500(field: str, value) -> None:
    status, problem = _post(_v1("v2-bad-number", **{field: value}))
    assert status == 400, problem
    assert problem["invalid_params"][0]["name"] == field


def test_a_budget_of_nan_is_refused() -> None:
    """Every comparison with NaN is false, so a NaN budget could never be exceeded."""
    raw = json.dumps(_v1("v2-nan"), allow_nan=True).replace('"budget_usd": 5.0', '"budget_usd": NaN')
    assert "NaN" in raw
    status, problem = _post(raw)
    assert status == 400
    assert "finite" in problem["detail"]


# ------------------------------------------------------------------ dry run


def test_a_dry_run_is_never_refused_and_records_what_would_have_refused_it() -> None:
    status, verdict = _post(_v1("v2-dry-boundary", dry_run=True, **DOMAIN_WRITE))
    assert status == 200
    assert verdict["status"] == "APPROVED", "A dry run is recorded as observed, never refused"
    assert verdict["dry_run"] is True
    assert verdict["observed_rules"] == ["ARCHITECTURAL_BOUNDARY_SAFE"]
    assert "would have been refused" in verdict["reason"]
    assert verdict["rule_evaluations"]["ARCHITECTURAL_BOUNDARY_SAFE"] is False, (
        "The invariant did fail; it was only not enforced"
    )

    row = _ledger_row("v2-dry-boundary")
    assert row["status"] == "APPROVED"
    assert row["rule"] == "NONE", "Nothing refused the call, so no rule fired"
    assert row["observed_rules"] == ["ARCHITECTURAL_BOUNDARY_SAFE"]
    assert row["dry_run"] is True


def test_the_same_write_without_dry_run_is_still_refused() -> None:
    """The control for the test above: the gate itself did not go soft."""
    status, verdict = _post(_v1("v2-real-boundary", **DOMAIN_WRITE))
    assert status == 200
    assert verdict["status"] == "BLOCKED_BOUNDARY_VIOLATION"
    assert verdict["dry_run"] is False


def test_a_dry_run_loop_is_observed_and_never_trips_the_session() -> None:
    session_id = "v2-dry-loop"
    call = _v1(session_id, dry_run=True, tool_name="run_tests", action_type="COMMAND_EXEC",
               arguments={"cmd": "pytest -q"})
    # One more than the repetition the policy in force allows, read off the
    # detector rather than assumed: the policy is a setting, and another test
    # in this suite may have saved a different one.
    repeats = _evaluator.loop_detector.repetition_threshold + 1
    verdicts = [_post(call)[1] for _ in range(repeats)]

    assert [v["status"] for v in verdicts] == ["APPROVED"] * repeats
    assert "LOOP_THRASHING_FREE" in (verdicts[-1]["observed_rules"] or []), (
        "The repeated call is the loop the gate would have halted"
    )
    assert all(v["session_tripped"] is False for v in verdicts)

    stored = _evaluator.session_repo.get_session(session_id)
    assert stored is not None and stored.is_tripped is False, "A dry run must never halt the real session"

    # And the session goes on answering real calls on their own merits.
    status, verdict = _post(_v1(session_id, tool_name="view_file", arguments={"path": "docs/a.md"}))
    assert status == 200
    assert verdict["status"] == "APPROVED"


def test_a_dry_run_over_budget_does_not_trip_the_session() -> None:
    session_id = "v2-dry-budget"
    status, verdict = _post(
        _v1(session_id, dry_run=True, projected_input_tokens=5_000_000, projected_output_tokens=0, budget_usd=0.5)
    )
    assert status == 200
    assert verdict["status"] == "APPROVED"
    assert verdict["observed_rules"] == ["BUDGET_CIRCUIT_BREAKER_SAFE"]
    assert _evaluator.session_repo.get_session(session_id).is_tripped is False


def test_a_dry_run_verdict_is_hashed_over_what_the_caller_was_given() -> None:
    """The verdict was rebuilt, not edited, so its proof still verifies."""
    _, verdict = _post(_v1("v2-dry-proof", dry_run=True, **DOMAIN_WRITE))
    canonical = json.dumps(
        {
            "session_id": verdict["session_id"],
            "status": verdict["status"],
            "risk_level": verdict["risk_level"],
            "reason": verdict["reason"],
            "rule_evaluations": verdict["rule_evaluations"],
            "timestamp": verdict["timestamp"],
        },
        sort_keys=True,
    )
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == verdict["proof_hash"]


def test_a_spend_ceiling_is_filed_under_spend_and_a_halted_session_is_not() -> None:
    """One status covers both, and the console asks a different question of each.

    The dry run above reads whichever rule the ledger names, so a breach filed
    as SESSION_ALREADY_HALTED would tell an architect a session had been
    stopped when what happened was a cost ceiling.
    """
    from threefold.application.evaluator import GovernanceEvaluator
    from threefold.application.dtos import ToolCallRequestDTO

    class _AlreadyHalted:
        """A breaker that reports the session was halted before it was asked."""

        max_single_invocation_cost = 1.0

        def evaluate_cost_risk(self, session, projected_usage):
            return False, "Session already tripped: halted by another worker"

    evaluator = GovernanceEvaluator(cost_breaker=_AlreadyHalted())
    request = ToolCallRequestDTO(
        session_id="v2-halted-elsewhere",
        developer_id="acme-dev-v1",
        project_name="Acme-Requests",
        tool_name="view_file",
        action_type="FILE_READ",
        arguments={"path": "README.md"},
    )
    evaluator.evaluate_tool_call(request)
    halted = next(r for r in evaluator.list_decisions(days=1) if r["session_id"] == "v2-halted-elsewhere")
    assert halted["rule"] == "SESSION_ALREADY_HALTED"

    # And the breach that does the halting wears the cost gate's own name.
    status, verdict = _post(
        _v1("v2-over-budget", projected_input_tokens=5_000_000, projected_output_tokens=0, budget_usd=0.5)
    )
    assert status == 200 and verdict["status"] == "BLOCKED_CIRCUIT_BREAKER"
    assert _ledger_row("v2-over-budget")["rule"] == "BUDGET_CIRCUIT_BREAKER_SAFE"


def test_the_console_counts_a_dry_run_as_observed_not_refused() -> None:
    _post(_v1("v2-dry-console", project_name="Acme-DryRun", dry_run=True, **DOMAIN_WRITE))
    response = lambda_handler(
        {
            "rawPath": "/prod/api/insights",
            "headers": {},
            "requestContext": {"http": {"method": "GET"}, "stage": "prod"},
        }
    )
    project = next(p for p in json.loads(response["body"])["by_project"] if p["project"] == "Acme-DryRun")
    assert project["refused"] == 0
    assert project["observed"] == 1


def test_a_hook_call_that_declares_no_tokens_costs_nothing() -> None:
    """Priced at the page defaults, a busy session was halted for spend it never made.

    About 1.3 cents a call against a $10 ceiling meant a working session was
    refused after some 740 tool calls. A hook sees tool calls, not model usage,
    so an undeclared hook call is free and the spend gate judges declared usage.
    """
    from threefold.application.dtos import ToolCallRequestDTO

    hook = ToolCallRequestDTO.from_payload({"project_name": "Acme-Hook", "origin": "hook", "tool_name": "Bash"})
    assert hook.projected_input_tokens == 0 and hook.projected_output_tokens == 0

    declared = ToolCallRequestDTO.from_payload(
        {"project_name": "Acme-Hook", "origin": "hook", "projected_input_tokens": 900, "projected_output_tokens": 100}
    )
    assert (declared.projected_input_tokens, declared.projected_output_tokens) == (900, 100)

    page = ToolCallRequestDTO.from_payload({"project_name": "Acme-Page", "origin": "page"})
    assert page.projected_input_tokens == 2000, "Callers that are not hooks keep the old defaults"
