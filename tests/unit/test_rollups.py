"""Charts and tiles read daily rollups, so they are exact however busy the ledger is.

Every recorded decision adds to one item per project and day. A failed rollup
never costs the verdict or the ledger row. The readiness of each rule, which
decides whether a project is promoted, is read off the same items.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import datetime
import time

import pytest

from threefold.application import projects as stages
from threefold.application import rollups
from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator
from threefold.infrastructure.dynamo_repo import ROLLUP_TTL_SECONDS, DynamoDBSessionRepository, rollup_counters

PROJECT = "Acme-Rollup"
TODAY = datetime.datetime.now(datetime.timezone.utc).date()
DAY = str(TODAY)


def _call(evaluator, session_id, arguments, *, tool="Write", action="FILE_WRITE", project=PROJECT, **extra):
    return evaluator.evaluate_tool_call(
        ToolCallRequestDTO(
            session_id=session_id,
            developer_id="anonymous",
            project_name=project,
            tool_name=tool,
            action_type=action,
            arguments=arguments,
            projected_input_tokens=0,
            projected_output_tokens=0,
            origin=extra.pop("origin", "hook"),
            agent=extra.pop("agent", "claude-code"),
            hook_mode=extra.pop("hook_mode", "managed"),
            **extra,
        )
    )


def _day(evaluator, project=PROJECT) -> dict:
    found = [item for item in evaluator.list_rollups(days=1, project=project)]
    assert len(found) == 1, found
    return found[0]


# ---------------------------------------------------------------- what a decision adds


def test_every_decision_adds_to_its_projects_day(monkeypatch) -> None:
    monkeypatch.delenv("DEFAULT_HOOK_STAGE", raising=False)  # observe, so the violation is observed
    evaluator = GovernanceEvaluator(session_repo=DynamoDBSessionRepository())
    _call(evaluator, "rollup-1", {"file_path": "README.md"}, tool="Read", action="FILE_READ")
    _call(evaluator, "rollup-2", {"file_path": "src/acme/domain/order.py", "content": "import boto3\n"}, agent="codex")
    _call(evaluator, "rollup-3", {"file_path": "src/acme/app.py", "content": "x = 1\n"}, origin="page", agent="page")
    _call(evaluator, "rollup-4", {"file_path": ".env"}, tool="Read", action="FILE_READ", origin="page", agent="page")

    day = _day(evaluator)
    assert day["day"] == DAY and day["project"] == PROJECT
    assert day["calls"] == 4
    assert (day["approved"], day["observed"], day["refused"]) == (2, 1, 1), "Disjoint, so they add up to calls"
    assert day["agent:claude-code"] == 1 and day["agent:codex"] == 1 and day["agent:page"] == 2
    assert day["origin:hook"] == 2 and day["origin:page"] == 2
    assert day["observed:python-domain-stays-pure"] == 1
    assert day["refused:PROTECTED_PATH"] == 1
    assert day["stage:observe"] == 2 and day["stage:enforce"] == 2
    assert day["hook_mode:managed"] == 4
    assert day["last_seen"] and day["last:PROTECTED_PATH"]


def test_a_rollup_expires_after_thirty_five_days() -> None:
    repo = DynamoDBSessionRepository()
    repo.record_decision({"timestamp": f"{DAY}T08:00:00+00:00", "verdict_id": "v1", "project_name": PROJECT,
                          "status": "APPROVED", "rule_key": "NONE"})
    stored = repo._memory_store[f"STATS#{DAY}#{PROJECT}"]
    assert abs(stored["ttl"] - (time.time() + ROLLUP_TTL_SECONDS)) < 60


def test_counters_follow_the_rule_key_not_the_invariant() -> None:
    counters, stamps = rollup_counters(
        {"status": "BLOCKED_BOUNDARY_VIOLATION", "rule_key": "java-domain-stays-pure", "agent": "codex",
         "origin": "hook", "stage": "enforce", "hook_mode": "enforce", "timestamp": "2026-09-22T08:00:00+00:00"}
    )
    assert counters["refused:java-domain-stays-pure"] == 1
    assert "refused:ARCHITECTURAL_BOUNDARY_SAFE" not in counters
    assert stamps == {"last_seen": "2026-09-22T08:00:00+00:00", "last:java-domain-stays-pure": "2026-09-22T08:00:00+00:00"}


# ---------------------------------------------------------------- against a live table


class _Table:
    def __init__(self, fail_updates: bool = False) -> None:
        self.puts = []
        self.updates = []
        self.fail_updates = fail_updates

    def put_item(self, Item, **_):  # noqa: N803 - boto3 spells it this way
        self.puts.append(Item)

    def get_item(self, Key, **_):  # noqa: N803
        return {}

    def update_item(self, **kwargs):
        if self.fail_updates:
            raise ConnectionError("simulated throttling")
        self.updates.append(kwargs)


class _Resource:
    def __init__(self, table) -> None:
        self.table = table

    def Table(self, _name):  # noqa: N802
        return self.table


def test_a_live_rollup_is_one_update_that_adds_and_names_every_counter() -> None:
    table = _Table()
    repo = DynamoDBSessionRepository(boto3_resource=_Resource(table))
    repo.record_decision({"timestamp": f"{DAY}T08:00:00+00:00", "verdict_id": "v1", "project_name": PROJECT,
                          "status": "APPROVED", "rule_key": "LOOP", "agent": "antigravity", "origin": "hook"})
    assert len(table.puts) == 1 and len(table.updates) == 1
    update = table.updates[0]
    assert update["Key"] == {"PK": f"STATS#{DAY}", "SK": PROJECT}
    expression = update["UpdateExpression"]
    assert "#ttl = if_not_exists(#ttl, :ttl)" in expression and " ADD " in expression
    names = update["ExpressionAttributeNames"]
    assert "agent:antigravity" in names.values() and "observed:LOOP" in names.values()
    assert ":" not in expression.replace(":c", "").replace(":s", "").replace(":ttl", ""), (
        "A name holding ':' must reach the expression only as a placeholder"
    )


def test_a_failed_rollup_costs_neither_the_verdict_nor_the_row() -> None:
    table = _Table(fail_updates=True)
    repo = DynamoDBSessionRepository(boto3_resource=_Resource(table))
    evaluator = GovernanceEvaluator(session_repo=repo)
    verdict = _call(evaluator, "rollup-throttled", {"file_path": "README.md"}, tool="Read", action="FILE_READ")
    assert verdict.status == "APPROVED"
    assert any(str(item["PK"]).startswith("DECISION#") for item in table.puts), "The ledger row was still written"


# ---------------------------------------------------------------- the overview


def _rollup(day, project, **counters):
    return dict(counters, day=str(day), project=project)


def test_the_overview_totals_come_from_the_rollups() -> None:
    yesterday = TODAY - datetime.timedelta(days=1)
    items = [
        _rollup(TODAY, "Acme-One", calls=5, approved=2, observed=2, refused=1, **{
            "agent:codex": 3, "agent:claude-code": 2, "origin:hook": 5, "observed:LOOP": 2,
            "refused:java-domain-stays-pure": 1, "reviewed:observed": 1, "review:false_alarm": 1,
            "last_seen": f"{TODAY}T09:00:00+00:00",
        }),
        _rollup(yesterday, "Acme-Two", calls=3, approved=3, **{"agent:antigravity": 3, "origin:ci": 3}),
        _rollup(TODAY, "Acme-Sandbox-0a1b2c3d", calls=9, approved=9, **{"agent:codex": 9}),
    ]
    configs = {"Acme-One": stages.new_config("2026-09-20T00:00:00+00:00", stage="enforce"),
               "Acme-Idle": stages.new_config("2026-09-20T00:00:00+00:00")}
    payload = rollups.overview(items, configs, days=7, today=TODAY)

    assert payload["source"] == "rollups" and payload["window_days"] == 7
    assert payload["totals"] == {
        "calls": 8, "approved": 5, "refused": 1, "would_refuse": 2, "needs_review": 1,
        "false_alarms": 1, "projects": 3, "agents": 3,
    }, "The expired sandbox is left out, and a configured project with no calls is listed"
    assert len(payload["series"]) == 7 and payload["series"][-1] == {"day": DAY, "approved": 2, "observed": 2, "refused": 1}
    assert payload["by_agent"] == [
        {"agent": "antigravity", "calls": 3}, {"agent": "codex", "calls": 3}, {"agent": "claude-code", "calls": 2},
    ]
    assert payload["by_origin"] == [{"origin": "hook", "calls": 5}, {"origin": "ci", "calls": 3}]
    assert {row["rule_key"]: (row["refused"], row["would_refuse"]) for row in payload["by_rule"]} == {
        "LOOP": (0, 2), "java-domain-stays-pure": (1, 0),
    }
    projects = {row["project"]: row for row in payload["by_project"]}
    assert projects["Acme-One"]["stage"] == "enforce" and projects["Acme-One"]["configured"] is True
    assert projects["Acme-Two"]["configured"] is False
    assert projects["Acme-Idle"]["calls"] == 0
    assert payload["stages"] == {"observe": 1, "enforce": 2}, "Unconfigured projects are on the suite's enforce default"


def test_the_overview_of_one_project_counts_only_it() -> None:
    items = [_rollup(TODAY, "Acme-One", calls=5, approved=5), _rollup(TODAY, "Acme-Two", calls=3, approved=3)]
    payload = rollups.overview(items, {}, days=1, project="Acme-Two", today=TODAY)
    assert payload["totals"]["calls"] == 3
    assert [row["project"] for row in payload["by_project"]] == ["Acme-Two"]


def test_a_name_the_pattern_no_longer_admits_is_shown_as_unlabelled(monkeypatch) -> None:
    """The pattern is a stack parameter. Counts written under a name it admitted
    yesterday must not put that name on a public page once it no longer does."""
    monkeypatch.setenv("ALLOWED_PROJECT_PATTERN", r"^Team-[a-z]+$")
    items = [
        _rollup(TODAY, "Acme-Payments-Internal", calls=2, approved=2),
        _rollup(TODAY, "unlabelled", calls=1, approved=1),
        _rollup(TODAY, "Team-web", calls=4, approved=4),
    ]
    configs = {"Acme-Payments-Internal": stages.new_config("2026-09-20T00:00:00+00:00", stage="enforce")}
    listed = rollups.projects_listing(items, configs)
    assert {row["project"]: row["calls"] for row in listed} == {"unlabelled": 3, "Team-web": 4}
    shown = rollups.overview(items, configs, days=1, today=TODAY)
    assert {row["project"] for row in shown["by_project"]} == {"unlabelled", "Team-web"}
    assert "Acme-Payments-Internal" not in str(shown) and "Acme-Payments-Internal" not in str(listed)


def test_the_projects_listing_names_agents_and_hook_modes() -> None:
    items = [_rollup(TODAY, "Acme-One", calls=4, approved=4, **{
        "agent:codex": 3, "agent:claude-code": 1, "hook_mode:managed": 3, "hook_mode:observe": 1})]
    configs = {"Acme-Fresh": stages.new_config("2026-09-22T00:00:00+00:00")}
    listed = {row["project"]: row for row in rollups.projects_listing(items, configs)}
    assert listed["Acme-One"]["agents"] == ["codex", "claude-code"]
    assert listed["Acme-One"]["hook_modes"] == ["managed", "observe"]
    assert listed["Acme-One"]["configured"] is False
    assert listed["Acme-Fresh"]["configured"] is True and listed["Acme-Fresh"]["calls"] == 0


# ---------------------------------------------------------------- reviews in the rollups


@pytest.mark.parametrize(
    "kind, old, new, expected",
    [
        ("observed", None, "correct", {"review:correct": 1, "correct:LOOP": 1, "reviewed:observed": 1, "reviewed:LOOP": 1}),
        ("observed", "correct", "false_alarm", {"review:correct": -1, "correct:LOOP": -1, "review:false_alarm": 1, "false_alarm:LOOP": 1}),
        ("observed", "false_alarm", None, {"review:false_alarm": -1, "false_alarm:LOOP": -1, "reviewed:observed": -1, "reviewed:LOOP": -1}),
        ("refused", None, "false_alarm", {"review:false_alarm": 1, "false_alarm:LOOP": 1}),
        ("observed", "correct", "correct", {}),
    ],
)
def test_a_label_moves_its_days_counters(kind, old, new, expected) -> None:
    assert rollups.review_deltas(kind, "LOOP", old, new) == expected


# ---------------------------------------------------------------- readiness


RULES = [{"id": "python-domain-stays-pure", "mode": "enforce"}, {"id": "java-domain-stays-pure", "mode": "enforce"}]


def _states(items, config=None) -> dict:
    found = rollups.readiness(items, config, RULES)
    return {row["rule_key"]: row for row in found["rules"]}, found["summary"]


def test_each_readiness_state() -> None:
    items = [_rollup(TODAY, PROJECT, calls=10, observed=6, **{
        "stage:observe": 10,
        "observed:python-domain-stays-pure": 2, "reviewed:python-domain-stays-pure": 2,
        "correct:python-domain-stays-pure": 2,
        "observed:java-domain-stays-pure": 1, "reviewed:java-domain-stays-pure": 1,
        "false_alarm:java-domain-stays-pure": 1,
        "observed:LOOP": 3,
        "review:correct": 2, "review:false_alarm": 1, "reviewed:observed": 3,
    })]
    rows, summary = _states(items)
    assert rows["python-domain-stays-pure"]["state"] == "ready"
    assert rows["java-domain-stays-pure"]["state"] == "noisy"
    assert rows["LOOP"]["state"] == "needs_review" and rows["LOOP"]["unreviewed"] == 3
    assert rows["BUDGET"]["state"] == "quiet"
    assert rows["LOOP"]["kind"] == "gate" and rows["python-domain-stays-pure"]["kind"] == "layering"
    assert [row["rule_key"] for row in rows.values()] == [
        "python-domain-stays-pure", "java-domain-stays-pure", "LOOP", "PROTECTED_PATH", "UNREADABLE_WRITE", "BUDGET"]
    assert summary == {
        "stage": "enforce", "days_observed": 1, "calls_observed": 10, "would_have_refused": 6,
        "reviewed": 3, "false_alarms": 1, "false_alarm_rate": 0.3333,
        "rules_ready": 1, "rules_quiet": 3, "rules_noisy": 1, "rules_needing_review": 1,
    }


def test_mode_now_follows_the_stage_and_the_observed_rules() -> None:
    config = stages.promoted(None, "2026-09-22T08:00:00+00:00", "anonymous", ["python-domain-stays-pure"],
                             stages.project_rule_keys(RULES))
    rows, summary = _states([], config)
    assert summary["stage"] == "enforce"
    assert rows["python-domain-stays-pure"]["mode_now"] == "enforce"
    assert rows["java-domain-stays-pure"]["mode_now"] == "observe"
    assert rows["python-domain-stays-pure"]["recommendation"].startswith("Enforcing")

    demoted = stages.demoted(config, "2026-09-22T09:00:00+00:00", "anonymous", stages.project_rule_keys(RULES))
    rows, _ = _states([], demoted)
    assert {row["mode_now"] for row in rows.values()} == {"observe"}
