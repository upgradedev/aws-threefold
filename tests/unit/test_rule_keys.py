"""Every ledger row names the rule that decided it, refusal and observation alike.

Readiness, the review queue and a project's observe_rules all group by one key:
the id of the layering rule that decided when one did, otherwise the gate. The
old `rule` column names an invariant, and a boundary refusal fails the same
invariant whether a layer was crossed or a credential store was reached, so it
cannot be what an operator promotes. It is kept exactly as it was beside the
new key.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import pytest

from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator
from threefold.application.rule_keys import (
    BUDGET,
    CREDENTIAL,
    HALTED_SESSION,
    LOOP,
    NONE,
    PROTECTED_PATH,
    UNREADABLE_WRITE,
    category_for,
    kind_of,
    layering_rule_named,
    refusal_key,
    rule_key,
)
from threefold.domain.layering_rules import DEFAULT_RULES
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository

WATCHING_JAVA = dict(DEFAULT_RULES[1], mode="observe")


def _evaluator(rules=None) -> GovernanceEvaluator:
    evaluator = GovernanceEvaluator(session_repo=DynamoDBSessionRepository())
    if rules is not None:
        evaluator.update_rules(rules)
    return evaluator


def _call(session_id: str, tool: str, action: str, arguments: dict, **extra) -> ToolCallRequestDTO:
    return ToolCallRequestDTO(
        session_id=session_id,
        developer_id="anonymous",
        project_name="Acme-Keys",
        tool_name=tool,
        action_type=action,
        arguments=arguments,
        projected_input_tokens=extra.pop("input_tokens", 0),
        projected_output_tokens=0,
        **extra,
    )


def _row(evaluator: GovernanceEvaluator, session_id: str, verdict_id: str = "") -> dict:
    """The newest row of a session, or the row of one verdict.

    By verdict when it matters: the clock this suite runs on can give two calls
    in a row the same timestamp, so "newest" is not always the last one made.
    """
    rows = [
        row for row in evaluator.list_decisions(days=1)
        if row["session_id"] == session_id and (not verdict_id or row["verdict_id"] == verdict_id)
    ]
    assert rows, f"No ledger row for {session_id}"
    return rows[0]


def _write(path: str, content: str) -> dict:
    return {"file_path": path, "content": content}


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "path, content, expected",
    [
        ("src/acme/domain/order.py", "import boto3\n", "python-domain-stays-pure"),
        ("src/main/java/com/acme/domain/Order.java", "import javax.persistence.Entity;\n", "java-domain-stays-pure"),
        ("src/Acme/Domain/Order.cs", "using System.Data.SqlClient;\n", "dotnet-domain-stays-pure"),
        ("src/web/domain/cart.ts", "import axios from 'axios';\n", "web-domain-stays-pure"),
    ],
)
def test_a_layering_refusal_is_keyed_by_the_rule_that_decided(path, content, expected) -> None:
    evaluator = _evaluator()
    session = f"keys-layer-{expected}"
    verdict = evaluator.evaluate_tool_call(_call(session, "Write", "FILE_WRITE", _write(path, content)))
    assert verdict.status == "BLOCKED_BOUNDARY_VIOLATION"
    row = _row(evaluator, session)
    assert row["rule_key"] == expected
    assert row["rule"] == "ARCHITECTURAL_BOUNDARY_SAFE", "The old column keeps its meaning"


@pytest.mark.parametrize(
    "session, tool, action, arguments, expected",
    [
        ("keys-secret", "Bash", "COMMAND_EXEC", {"command": "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"}, CREDENTIAL),
        ("keys-env", "Read", "FILE_READ", {"file_path": ".env"}, PROTECTED_PATH),
        ("keys-cat-env", "Bash", "COMMAND_EXEC", {"command": "cat .env"}, PROTECTED_PATH),
        ("keys-settings", "Write", "FILE_WRITE", _write(".claude/settings.json", "{}"), PROTECTED_PATH),
        ("keys-no-verify", "Bash", "COMMAND_EXEC", {"command": "git commit --no-verify -m wip"}, PROTECTED_PATH),
        ("keys-destructive", "Bash", "COMMAND_EXEC", {"command": "rm -rf /"}, PROTECTED_PATH),
        ("keys-cp", "Bash", "COMMAND_EXEC", {"command": "cp /tmp/acme.py src/domain/acme_user.py"}, UNREADABLE_WRITE),
        ("keys-patch", "Bash", "COMMAND_EXEC", {"command": "git apply /tmp/fix.patch"}, UNREADABLE_WRITE),
    ],
)
def test_a_gate_refusal_is_keyed_by_its_gate(session, tool, action, arguments, expected) -> None:
    evaluator = _evaluator()
    verdict = evaluator.evaluate_tool_call(_call(session, tool, action, arguments))
    assert verdict.status.startswith("BLOCKED"), verdict.reason
    assert _row(evaluator, session)["rule_key"] == expected


def test_an_unreadable_write_is_its_own_key_though_the_sentence_names_the_rule() -> None:
    """The unreadable-write policy decided, not the rule's import list, and the
    two are staged apart: a `cp` of a clean file is a different false alarm from
    a wrong import."""
    evaluator = _evaluator()
    verdict = evaluator.evaluate_tool_call(
        _call("keys-cp-named", "Bash", "COMMAND_EXEC", {"command": "cp /tmp/acme.py src/domain/acme_user.py"})
    )
    assert "python-domain-stays-pure" in verdict.reason
    assert _row(evaluator, "keys-cp-named")["rule_key"] == UNREADABLE_WRITE


def test_a_loop_and_the_halt_after_it_are_keyed_apart() -> None:
    evaluator = _evaluator()
    edit = {"file_path": "src/acme/service.py", "old_string": "a = 1", "new_string": "a = 2"}
    for _ in range(3):
        last = evaluator.evaluate_tool_call(_call("keys-loop", "Edit", "FILE_WRITE", dict(edit), origin="page"))
    assert last.status == "BLOCKED_LOOP_DETECTED"
    assert _row(evaluator, "keys-loop", last.verdict_id)["rule_key"] == LOOP

    after = evaluator.evaluate_tool_call(
        _call("keys-loop", "Read", "FILE_READ", {"file_path": "README.md"}, origin="page")
    )
    row = _row(evaluator, "keys-loop", after.verdict_id)
    assert row["rule_key"] == HALTED_SESSION
    assert row["rule"] == "SESSION_ALREADY_HALTED", "The old column keeps its own name for it"


def test_a_spend_breach_is_keyed_as_budget() -> None:
    evaluator = _evaluator()
    verdict = evaluator.evaluate_tool_call(
        _call("keys-budget", "Read", "FILE_READ", {"file_path": "README.md"}, input_tokens=2_000_000)
    )
    assert verdict.status == "BLOCKED_CIRCUIT_BREAKER"
    assert _row(evaluator, "keys-budget")["rule_key"] == BUDGET


def test_an_approval_nothing_flagged_is_keyed_none() -> None:
    evaluator = _evaluator()
    evaluator.evaluate_tool_call(_call("keys-clean", "Read", "FILE_READ", {"file_path": "README.md"}))
    assert _row(evaluator, "keys-clean")["rule_key"] == NONE


# ---------------------------------------------------------------- observations


def test_an_observing_rule_keys_its_observation_by_its_own_id() -> None:
    evaluator = _evaluator([DEFAULT_RULES[0], WATCHING_JAVA])
    verdict = evaluator.evaluate_tool_call(
        _call(
            "keys-watching",
            "Write",
            "FILE_WRITE",
            _write("src/main/java/com/acme/domain/Order.java", "import javax.persistence.Entity;\n"),
        )
    )
    assert verdict.status == "APPROVED"
    row = _row(evaluator, "keys-watching")
    assert row["rule_key"] == "java-domain-stays-pure"
    assert row["observed_rules"] == ["java-domain-stays-pure"]


def test_a_dry_run_names_the_layering_rule_that_would_have_refused() -> None:
    evaluator = _evaluator()
    verdict = evaluator.evaluate_tool_call(
        _call("keys-dry-layer", "Write", "FILE_WRITE", _write("src/acme/domain/order.py", "import boto3\n"), dry_run=True)
    )
    assert verdict.status == "APPROVED" and verdict.dry_run is True
    row = _row(evaluator, "keys-dry-layer")
    assert row["rule_key"] == "python-domain-stays-pure"
    # The field named the invariant here and the rule's own id in the test
    # above, for the same act of watching, because a dry run reached it by a
    # different route. Both now name the rule, and a call that would have
    # broken two rules lists both rather than one invariant twice.
    assert row["observed_rules"] == ["python-domain-stays-pure"]


def test_a_dry_run_of_a_gate_names_the_gate() -> None:
    evaluator = _evaluator()
    evaluator.evaluate_tool_call(_call("keys-dry-env", "Read", "FILE_READ", {"file_path": ".env"}, dry_run=True))
    assert _row(evaluator, "keys-dry-env")["rule_key"] == PROTECTED_PATH


def test_a_repeated_poll_names_no_rule() -> None:
    evaluator = _evaluator()
    for _ in range(4):
        last = evaluator.evaluate_tool_call(
            _call("keys-poll", "Bash", "COMMAND_EXEC", {"command": "git status"}, origin="hook")
        )
    assert last.observations, "The fourth poll carries the repeat note"
    row = _row(evaluator, "keys-poll", last.verdict_id)
    assert row["status"] == "APPROVED"
    assert row["rule_key"] == NONE


# ---------------------------------------------------------------- rows written before the key


def test_a_row_written_before_the_key_is_given_one_on_the_way_out() -> None:
    repo = DynamoDBSessionRepository()
    evaluator = GovernanceEvaluator(session_repo=repo)
    legacy = [
        {
            "verdict_id": "legacy-1", "session_id": "keys-legacy-refusal", "status": "BLOCKED_BOUNDARY_VIOLATION",
            "rule": "ARCHITECTURAL_BOUNDARY_SAFE",
            "reason": "Clean Architecture violation: Layering rule 'acme-billing-core' refuses this write: ...",
        },
        {
            "verdict_id": "legacy-2", "session_id": "keys-legacy-dry", "status": "APPROVED", "rule": "NONE",
            "observed_rules": ["LOOP_THRASHING_FREE"], "observed_reason": "Monomorphic loop detected",
        },
        {
            "verdict_id": "legacy-3", "session_id": "keys-legacy-watch", "status": "APPROVED", "rule": "NONE",
            "observed_rule": "acme-catalog-no-http",
            "observed_reason": "Layering rule 'acme-catalog-no-http' would refuse this write: ...",
        },
    ]
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    for row in legacy:
        repo.record_decision(dict(row, timestamp=now))
    keys = {row["session_id"]: row["rule_key"] for row in evaluator.list_decisions(days=1)}
    assert keys["keys-legacy-refusal"] == "acme-billing-core"
    assert keys["keys-legacy-dry"] == LOOP
    assert keys["keys-legacy-watch"] == "acme-catalog-no-http"


# ---------------------------------------------------------------- the reading itself


def test_an_id_with_a_quote_in_it_is_read_whole_when_the_rules_are_known() -> None:
    reason = "Layering rule 'acme's-core' refuses this write: Acme's core stays pure"
    assert layering_rule_named(reason, ["acme's-core"]) == "acme's-core"


def test_a_refusal_the_contract_names_no_gate_for_is_filed_with_protected_paths() -> None:
    assert refusal_key("BLOCKED_BOUNDARY_VIOLATION", "Command 'drop database acme' contains a destructive operation") == PROTECTED_PATH


def test_a_status_that_refuses_nothing_has_no_key() -> None:
    assert rule_key({"status": "APPROVED", "reason": "All deterministic governance invariants satisfied"}) == NONE
    assert rule_key({"status": "REQUIRES_HUMAN_APPROVAL", "reason": ""}) == NONE


def test_the_three_kinds_are_disjoint() -> None:
    assert kind_of({"status": "BLOCKED_LOOP_DETECTED"}) == "refused"
    assert kind_of({"status": "APPROVED", "rule_key": "LOOP"}) == "observed"
    assert kind_of({"status": "APPROVED", "rule_key": "NONE"}) == "approved"


def test_a_key_reads_as_the_category_insights_uses() -> None:
    assert category_for("java-domain-stays-pure") == "LAYERING"
    assert category_for(CREDENTIAL) == "CREDENTIAL_IN_ARGUMENTS"
    assert category_for(NONE) == "NONE"
