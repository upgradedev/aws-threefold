"""The fix a verdict carries answers the gate that decided it.

The proposer picks a family of fix (loop, budget, halted session, or the
boundary's own questions) before anything else. It read that off `rule_key`,
which no verdict carries: the response has no such field, and the evaluator
marks each verdict with the gate that decided it as `decided_key`. So every
family was chosen by the fallback, which ranks the failed invariants rather
than follow the gate. For a refusal the two agree, since only the deciding
gate's invariant is false. For a page's dry run that two gates would have
refused they did not: the reason and the ledger name the first gate, and the
fix answered whichever the ranking put first, a loop fix for a layering
violation that repeats and generic spend advice for a loop over the budget.

Every verdict here is made by the real evaluator. Names are synthetic, as the
clean-room rule requires.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Tuple

import pytest

from threefold.application import fix_proposer
from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator
from threefold.application.fix_proposer import propose_fix
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository

PROJECT = "Acme-Fix-Family"
# Built by concatenation so no file in the repository holds a credential-shaped string whole.
ACCESS_KEY = "AKIA" + "ACMEEXAMPLE00000"
DOMAIN_WRITE = {"file_path": "src/acme/domain/order.py", "content": "import boto3\n"}
README = {"file_path": "README.md"}
# Declared usage past the per-call cap, which trips the session.
EXPENSIVE = {"projected_input_tokens": 5_000_000, "projected_output_tokens": 2_000_000}


def _evaluator() -> GovernanceEvaluator:
    return GovernanceEvaluator(session_repo=DynamoDBSessionRepository(table_name="fix-family-test"))


def _request(session: str, tool: str, action: str, arguments: Dict[str, Any], **extra: Any) -> ToolCallRequestDTO:
    fields: Dict[str, Any] = dict(
        session_id=session,
        developer_id="anonymous",
        project_name=PROJECT,
        tool_name=tool,
        action_type=action,
        arguments=dict(arguments),
        agent="claude-code",
        origin="hook",
    )
    fields.update(extra)
    return ToolCallRequestDTO(**fields)


def _last(evaluator: GovernanceEvaluator, times: int, *args: Any, **extra: Any) -> Tuple[ToolCallRequestDTO, Any]:
    request = _request(*args, **extra)
    verdict = None
    for _ in range(times):
        verdict = evaluator.evaluate_tool_call(request)
    return request, verdict


def _rules(evaluator: GovernanceEvaluator) -> List[Dict[str, Any]]:
    return evaluator.rules_in_force(PROJECT)[0]


# Each family the proposer knows, with the kind of fix it gives: how the verdict
# is made (calls, then the arguments of the last), its gate and its family.
FAMILIES = [
    ("layering", 1, ("Write", "FILE_WRITE", DOMAIN_WRITE), {}, "python-domain-stays-pure", "boundary"),
    (
        "credential",
        1,
        ("Write", "FILE_WRITE", {"file_path": "src/acme/settings.py", "content": f"KEY = '{ACCESS_KEY}'\n"}),
        {},
        "CREDENTIAL",
        "boundary",
    ),
    ("protected_path", 1, ("Read", "FILE_READ", {"file_path": ".env"}), {}, "PROTECTED_PATH", "boundary"),
    ("destructive_command", 1, ("Bash", "COMMAND_EXEC", {"command": "rm -rf /"}), {}, "PROTECTED_PATH", "boundary"),
    (
        "unreadable_write",
        1,
        ("Bash", "COMMAND_EXEC", {"command": "cp /tmp/acme.py src/acme/domain/acme_user.py"}),
        {},
        "UNREADABLE_WRITE",
        "boundary",
    ),
    ("loop", 4, ("Bash", "COMMAND_EXEC", {"command": "make acme"}), {}, "LOOP", "loop"),
    ("budget", 1, ("Read", "FILE_READ", README), dict(origin="page", explain=True, **EXPENSIVE), "BUDGET", "budget"),
]


@pytest.mark.parametrize(
    "kind, times, call, extra, key, family", FAMILIES, ids=[case[0] for case in FAMILIES]
)
def test_each_family_is_the_gate_the_verdict_names(kind, times, call, extra, key, family) -> None:
    evaluator = _evaluator()
    request, verdict = _last(evaluator, times, f"family-{kind}", *call, **extra)

    assert verdict.status.startswith("BLOCKED"), verdict.reason
    assert verdict.decided_key == key
    assert fix_proposer._family(verdict) == family
    assert verdict.suggested_fix is not None and verdict.suggested_fix["kind"] == kind

    # A refusal fails only its own gate's invariant, so a copy that names no
    # gate is given the same fix by the fallback: nothing a refusal was
    # offered before changes.
    keyless = copy.copy(verdict)
    keyless.decided_key = None
    assert fix_proposer._family(keyless) == family
    assert propose_fix(request, keyless, _rules(evaluator))["kind"] == kind


def test_a_halted_session_is_answered_as_one() -> None:
    evaluator = _evaluator()
    evaluator.evaluate_tool_call(_request("family-halted", "Read", "FILE_READ", README, origin="page", **EXPENSIVE))
    request, verdict = _last(evaluator, 1, "family-halted", "Read", "FILE_READ", README, origin="page", explain=True)

    assert (verdict.status, verdict.decided_key) == ("BLOCKED_CIRCUIT_BREAKER", "HALTED_SESSION")
    assert fix_proposer._family(verdict) == "halted"
    assert verdict.suggested_fix["kind"] == "halted_session"


def test_a_page_s_dry_run_is_answered_for_the_rule_that_would_have_refused_it() -> None:
    evaluator = _evaluator()
    request, verdict = _last(
        evaluator, 1, "family-observed", "Write", "FILE_WRITE", DOMAIN_WRITE, origin="page", explain=True, dry_run=True
    )

    assert verdict.status == "APPROVED" and verdict.observed_rules == ["python-domain-stays-pure"]
    assert fix_proposer._family(verdict) == "boundary"
    assert verdict.suggested_fix["kind"] == "layering" and verdict.suggested_fix["validated"] is True


# ---------------------------------------------------------------- the gate, not the ranking


def test_a_layering_violation_that_repeats_is_answered_with_the_layering_fix() -> None:
    """A dry run the layering rule and the loop gate would both refuse.

    Its reason and its ledger row name the layering rule, which the guard found
    first. The ranking put the loop first, and the page was told only that the
    same Write was repeated, when a checked rewrite of it was there to offer.
    """
    evaluator = _evaluator()
    both = []
    for _ in range(6):
        verdict = evaluator.evaluate_tool_call(
            _request("family-two-gates", "Write", "FILE_WRITE", DOMAIN_WRITE, origin="page", explain=True, dry_run=True)
        )
        if verdict.observed_rules == ["python-domain-stays-pure", "LOOP"]:
            both.append(verdict)
    assert both, "The loop gate never fired on the repeated dry run, so this test proves nothing"
    for verdict in both:
        assert verdict.rule_evaluations["LOOP_THRASHING_FREE"] is False
        assert verdict.decided_key == "python-domain-stays-pure"
        assert fix_proposer._family(verdict) == "boundary"
        assert verdict.suggested_fix["kind"] == "layering", verdict.suggested_fix["summary"]
        assert verdict.suggested_fix["validated"] is True


def test_a_loop_over_the_budget_is_answered_with_the_loop_fix_and_its_count() -> None:
    """A dry run the loop gate and the spend ceiling would both refuse; its reason names the loop."""
    evaluator = _evaluator()
    both = []
    for _ in range(6):
        verdict = evaluator.evaluate_tool_call(
            _request(
                "family-loop-spend", "Bash", "COMMAND_EXEC", {"command": "make acme"},
                origin="page", explain=True, dry_run=True, **EXPENSIVE,
            )
        )
        if verdict.observed_rules == ["LOOP", "BUDGET"]:
            both.append(verdict)
    assert both, "The loop gate never fired on the repeated dry run, so this test proves nothing"
    for verdict in both:
        assert verdict.rule_evaluations["BUDGET_CIRCUIT_BREAKER_SAFE"] is False
        assert fix_proposer._family(verdict) == "loop"
        fix = verdict.suggested_fix
        count = re.search(r"(\d+) consecutive times", verdict.reason)
        assert fix["kind"] == "loop" and count, (fix, verdict.reason)
        assert f"called {count.group(1)} times" in fix["summary"], fix["summary"]


def test_the_gate_decides_where_the_invariants_point_elsewhere() -> None:
    """A loop refusal whose verdict also marks the spend invariant failed is still the loop's."""
    evaluator = _evaluator()
    request, verdict = _last(evaluator, 4, "family-loop-marked", "Bash", "COMMAND_EXEC", {"command": "make acme"})
    assert verdict.decided_key == "LOOP"
    marked = copy.copy(verdict)
    marked.rule_evaluations = dict(verdict.rule_evaluations, BUDGET_CIRCUIT_BREAKER_SAFE=False)

    assert propose_fix(request, marked, _rules(evaluator))["kind"] == "loop"
    unmarked = copy.copy(marked)
    unmarked.decided_key = None
    assert propose_fix(request, unmarked, _rules(evaluator))["kind"] == "budget", "The ranking the key now overrides"


def test_a_layering_rule_named_like_a_gate_is_a_layering_rule() -> None:
    """An operator's rule id is compared as written: `loop` in lower case is not the loop gate."""
    evaluator = _evaluator()
    evaluator.update_rules(
        [
            {
                "id": "loop",
                "description": "Acme domain modules may not reach AWS",
                "mode": "enforce",
                "when_path_matches": ["**/acme/domain/**/*.py"],
                "forbid_imports": ["boto3"],
            }
        ]
    )
    request, verdict = _last(evaluator, 1, "family-rule-named-loop", "Write", "FILE_WRITE", DOMAIN_WRITE)

    assert (verdict.status, verdict.decided_key) == ("BLOCKED_BOUNDARY_VIOLATION", "loop")
    assert fix_proposer._family(verdict) == "boundary"
    assert verdict.suggested_fix["kind"] == "layering"


def test_the_key_the_verdict_carries_is_read_before_one_a_row_carries() -> None:
    assert fix_proposer._decided_key({"decided_key": "LOOP", "rule_key": "BUDGET"}) == "LOOP"
    assert fix_proposer._decided_key({"rule_key": "BUDGET"}) == "BUDGET"
    assert fix_proposer._decided_key({"decided_key": None, "rule_key": ""}) == ""
