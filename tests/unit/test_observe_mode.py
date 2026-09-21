"""A rule can watch before it bites.

An architect cannot switch on a rule for forty teams on the strength of a
belief about their code. A rule in observe mode records what it would have
refused and lets the call run, so the rollout decision is made from a week of
evidence rather than from the complaints on the first morning.

The properties pinned here are the ones whose failure would be quiet: an
observing rule that blocked, a rule with no mode that stopped enforcing, and an
observation counted as a refusal on the console.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator
from threefold.application.insights import describe_layering, summarise
from threefold.domain.layering_rules import DEFAULT_RULES, evaluate, normalise_rules, observed

CATALOG = "src/main/java/com/acme/catalog/domain/Product.java"
HTTP_IMPORT = "package com.acme.catalog.domain;\nimport java.net.http.HttpClient;"

WATCHING = {
    "id": "catalog-no-http",
    "description": "Catalog domain types may not make HTTP calls",
    "mode": "observe",
    "when_path_matches": ["**/catalog/domain/**/*.java"],
    "forbid_imports": ["java.net.http"],
    "allow_imports": [],
}
ENFORCING = {
    "id": "catalog-no-sql",
    "description": "Catalog domain types may not reach JDBC",
    "when_path_matches": ["**/catalog/domain/**/*.java"],
    "forbid_imports": ["java.sql"],
    "allow_imports": [],
}


def _write(evaluator: GovernanceEvaluator, session_id: str, content: str) -> object:
    return evaluator.evaluate_tool_call(
        ToolCallRequestDTO(
            session_id=session_id,
            developer_id="observe-dev",
            project_name="Acme-Catalog",
            tool_name="Write",
            action_type="FILE_WRITE",
            arguments={"file_path": CATALOG, "content": content},
            projected_input_tokens=100,
            projected_output_tokens=50,
            budget_usd=5.0,
        )
    )


def test_an_observing_rule_never_refuses() -> None:
    allowed, _ = evaluate(CATALOG, HTTP_IMPORT, [WATCHING])
    assert allowed is True
    seen = observed(CATALOG, HTTP_IMPORT, [WATCHING])
    assert [item["rule_id"] for item in seen] == ["catalog-no-http"]
    assert "would refuse" in seen[0]["reason"]


def test_a_rule_that_names_no_mode_still_enforces() -> None:
    """Every rule saved before modes existed must keep refusing exactly as it did."""
    cleaned = normalise_rules([ENFORCING])
    assert cleaned[0]["mode"] == "enforce"
    assert evaluate(CATALOG, "import java.sql.Connection;", cleaned)[0] is False
    assert all(rule["mode"] == "enforce" for rule in DEFAULT_RULES)


def test_a_mode_it_does_not_know_is_dropped_rather_than_guessed() -> None:
    """Guessing "warn" as enforce would stop people; guessing it as observe would not."""
    cleaned = normalise_rules([dict(ENFORCING, id="typo", mode="warn"), ENFORCING])
    assert [rule["id"] for rule in cleaned] == ["catalog-no-sql"]


def test_mode_is_read_without_regard_to_case() -> None:
    cleaned = normalise_rules([dict(WATCHING, mode="OBSERVE")])
    assert cleaned and cleaned[0]["mode"] == "observe"


def test_an_observing_rule_beside_an_enforcing_one_does_not_weaken_it() -> None:
    both = [WATCHING, ENFORCING]
    content = HTTP_IMPORT + "\nimport java.sql.Connection;"
    allowed, reason = evaluate(CATALOG, content, both)
    assert allowed is False
    assert "catalog-no-sql" in reason


def test_the_call_runs_and_the_ledger_says_what_would_have_stopped_it() -> None:
    evaluator = GovernanceEvaluator()
    evaluator.update_rules([WATCHING, ENFORCING])

    result = _write(evaluator, "observe-001", HTTP_IMPORT)
    assert result.status == "APPROVED"
    assert result.observed_rules == ["catalog-no-http"]
    assert result.observations and "would refuse" in result.observations[0]

    rows = [r for r in evaluator.list_decisions(days=1) if r["session_id"] == "observe-001"]
    assert rows and rows[0]["status"] == "APPROVED"
    assert rows[0]["observed_rule"] == "catalog-no-http"


def test_a_clean_write_carries_no_observation() -> None:
    evaluator = GovernanceEvaluator()
    evaluator.update_rules([WATCHING])
    result = _write(evaluator, "observe-clean", "package com.acme.catalog.domain;\nimport java.util.List;")
    assert result.status == "APPROVED"
    assert not result.observed_rules


def test_the_console_counts_an_observation_apart_from_a_refusal() -> None:
    """Adding observations to refusals would report stopped calls that were not stopped."""
    evaluator = GovernanceEvaluator()
    evaluator.update_rules([WATCHING, ENFORCING])
    _write(evaluator, "observe-console-1", HTTP_IMPORT)
    _write(evaluator, "observe-console-2", "import java.sql.Connection;")

    report = summarise(evaluator.list_decisions(days=1), window_days=1)
    totals = report["totals"]
    assert totals["decisions"] == 2
    assert totals["refused"] == 1
    assert totals["observed"] == 1
    assert report["by_observed_rule"] == [{"rule": "catalog-no-http", "would_refuse": 1}]
    assert [row["session_id"] for row in report["recent_observations"]] == ["observe-console-1"]
    project = next(p for p in report["by_project"] if p["project"] == "Acme-Catalog")
    assert project["refused"] == 1 and project["observed"] == 1


def test_the_coverage_card_does_not_count_an_observing_rule_as_protection() -> None:
    """A zero beside "2 rules in force" reads as safety one of them never provides."""
    card = describe_layering([WATCHING, ENFORCING], ["java"])
    assert "1 rule(s) refusing (catalog-no-sql)" in card["watches"]
    assert "only recording" in card["watches"] and "catalog-no-http" in card["watches"]

    only_watching = describe_layering([WATCHING], ["java"])
    assert only_watching["watches"].startswith("No rule refuses anything")


def test_two_observing_rules_on_one_write_are_both_counted() -> None:
    """Staging three rules at once is the normal rollout; each needs its own count.

    The ledger kept the first observing rule of a call and dropped the rest, so
    the second rule's total read low and an architect deciding whether to switch
    it on was reading the wrong number.
    """
    also_watching = dict(WATCHING, id="catalog-no-client-lib", forbid_imports=["java.net"])
    evaluator = GovernanceEvaluator()
    evaluator.update_rules([WATCHING, also_watching])
    result = _write(evaluator, "observe-two", HTTP_IMPORT)
    assert result.status == "APPROVED"
    assert sorted(result.observed_rules) == ["catalog-no-client-lib", "catalog-no-http"]

    report = summarise(evaluator.list_decisions(days=1), window_days=1)
    assert report["totals"]["observed"] == 1, "One call ran, whatever number of rules watched it"
    counted = {row["rule"]: row["would_refuse"] for row in report["by_observed_rule"]}
    assert counted == {"catalog-no-http": 1, "catalog-no-client-lib": 1}


# Found by the adversarial review of this change, each reproduced before it was fixed.


def _multi_edit(evaluator: GovernanceEvaluator, session_id: str, edits: list) -> object:
    return evaluator.evaluate_tool_call(
        ToolCallRequestDTO(
            session_id=session_id,
            developer_id="observe-dev",
            project_name="Acme-Catalog",
            tool_name="MultiEdit",
            action_type="FILE_WRITE",
            arguments={"edits": edits},
            projected_input_tokens=100,
            projected_output_tokens=50,
            budget_usd=5.0,
        )
    )


def test_one_rule_watching_two_files_of_one_call_counts_once() -> None:
    evaluator = GovernanceEvaluator()
    evaluator.update_rules([WATCHING])
    other = CATALOG.replace("Product.java", "Price.java")
    result = _multi_edit(
        evaluator,
        "observe-twice",
        [{"file_path": CATALOG, "content": HTTP_IMPORT}, {"file_path": other, "content": HTTP_IMPORT}],
    )
    assert result.observed_rules == ["catalog-no-http"]
    report = summarise(evaluator.list_decisions(days=1), window_days=1)
    assert report["by_observed_rule"] == [{"rule": "catalog-no-http", "would_refuse": 1}]


def test_the_observation_names_the_file_it_is_about() -> None:
    """The row's target is the call's first path, which can be a different, clean file."""
    evaluator = GovernanceEvaluator()
    evaluator.update_rules([WATCHING])
    clean = "src/main/java/com/acme/catalog/application/Service.java"
    _multi_edit(
        evaluator,
        "observe-target",
        [{"file_path": clean, "content": "import java.util.List;"}, {"file_path": CATALOG, "content": HTTP_IMPORT}],
    )
    row = next(r for r in evaluator.list_decisions(days=1) if r["session_id"] == "observe-target")
    assert row["target"] == clean
    assert row["observed_target"] == CATALOG


def test_removing_a_forbidden_import_is_not_judged_as_adding_it() -> None:
    """A MultiEdit's edits carry no path of their own, so the text being deleted was judged."""
    from threefold.domain.boundary_guard import ArchitecturalBoundaryGuard
    from threefold.domain.models import ToolActionType, ToolInvocation

    removing = ToolInvocation(
        tool_name="MultiEdit",
        action_type=ToolActionType.FILE_WRITE,
        arguments={
            "file_path": "src/domain/models.py",
            "edits": [{"old_string": "import boto3\n", "new_string": ""}],
        },
    )
    allowed, reason = ArchitecturalBoundaryGuard.evaluate_tool_boundary(removing, rules=DEFAULT_RULES)
    assert allowed, f"Deleting the import was refused: {reason}"

    adding = ToolInvocation(
        tool_name="MultiEdit",
        action_type=ToolActionType.FILE_WRITE,
        arguments={
            "file_path": "src/domain/models.py",
            "edits": [{"old_string": "x = 1\n", "new_string": "import boto3\nx = 1\n"}],
        },
    )
    allowed, _ = ArchitecturalBoundaryGuard.evaluate_tool_boundary(adding, rules=DEFAULT_RULES)
    assert not allowed, "The edit's new text reaches its file through the path above it"


def test_a_save_reaches_a_container_that_did_not_take_it() -> None:
    """Each container loaded the rules on its own cold start and never again."""
    from threefold.application import evaluator as evaluator_module
    from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository

    shared = DynamoDBSessionRepository()
    took_the_save = GovernanceEvaluator(session_repo=shared)
    elsewhere = GovernanceEvaluator(session_repo=shared)
    took_the_save.update_rules([WATCHING])
    assert elsewhere.layering_rules != took_the_save.layering_rules

    elsewhere._rules_read_at -= evaluator_module.RULES_REFRESH_SECONDS + 1
    result = _write(elsewhere, "observe-elsewhere", HTTP_IMPORT)
    assert result.observed_rules == ["catalog-no-http"], "The other container still enforced the old rules"
