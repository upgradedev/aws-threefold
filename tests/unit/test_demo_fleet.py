"""The synthetic Acme fleet: what a tick sends, what its operator does, and what it never does.

The fleet feeds the public demo's charts and tiles. Each tick sends a bounded
batch of synthetic hook calls from three coding agents across six projects
through the real evaluator, then acts as the projects' operator: it labels
would-refuse calls, promotes a project whose rules are ready (the noisy rule
keeps observing) and now and then demotes one. Everything a tick decides is
drawn from its fifteen-minute bucket, so these tests reproduce ticks exactly.

The tests that run many ticks drive a simulated clock through the three places
the service reads the time (a verdict's stamp, a session's creation, the store's
windows), so a day of ticks takes seconds and reads the same on any date. The
fleet itself never reads that clock for anything but the moment it runs.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import ast
import collections
import datetime
import inspect
import json
import logging
import re
from typing import Any, Dict, List

import pytest

import threefold.domain.models as models
from threefold.application import demo_fleet, dtos, rollups
from threefold.application import projects as stages
from threefold.application.bedrock_reviewer import BedrockArchitecturalReviewer
from threefold.application.evaluator import GovernanceEvaluator
from threefold.domain.boundary_guard import SecretScanner
from threefold.infrastructure import dynamo_repo
from threefold.infrastructure.bedrock_client import BedrockGovernanceClient
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository
from threefold.interfaces import app_routes

UTC = datetime.timezone.utc
# A Monday, midnight UTC: a week of buckets from here covers working hours,
# nights and a weekend.
MONDAY = datetime.datetime(2026, 9, 21, tzinfo=UTC)
A_WEEK = [demo_fleet.bucket_of(MONDAY) + offset for offset in range(7 * 96)]
SESSION = re.compile(r"^fleet-(payments|checkout|ledger|search|mobile|platform)-(claude-code|codex|antigravity)-\d{1,4}$")


class SimulatedClock:
    """The service's clock, moved by the test: every reading is 20 ms after the last."""

    def __init__(self, start: datetime.datetime) -> None:
        self.now = start
        clock = self

        class _Clock(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                clock.now += datetime.timedelta(milliseconds=20)
                return clock.now if tz is not None else clock.now.replace(tzinfo=None)

        self.datetime = _Clock

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for module in (dtos, models, dynamo_repo):
            monkeypatch.setattr(module, "datetime", self.datetime)


def _fresh_evaluator() -> GovernanceEvaluator:
    return GovernanceEvaluator(session_repo=DynamoDBSessionRepository())


def _run_ticks(evaluator, clock: SimulatedClock, start: datetime.datetime, ticks: int) -> List[Dict[str, Any]]:
    summaries = []
    for index in range(ticks):
        moment = start + datetime.timedelta(minutes=15 * index)
        clock.now = moment
        summaries.append(demo_fleet.run_tick(evaluator, now=moment))
    return summaries


def _rows(evaluator) -> List[Dict[str, Any]]:
    """The ledger's rows in the order they were recorded."""
    store = evaluator.session_repo._memory_store
    return [dict(item) for key, item in store.items() if key.startswith("DECISION#")]


class Day:
    """What a simulated day of ticks left behind, read while its clock was still in place."""

    def __init__(self, evaluator, summaries: List[Dict[str, Any]], clock: SimulatedClock) -> None:
        self.summaries = summaries
        self.rows = _rows(evaluator)
        # Everything the store holds: ledger rows, sessions with their call
        # history, rollups and configurations.
        self.items = {key: dict(item) for key, item in evaluator.session_repo._memory_store.items()}
        self.rollups = evaluator.list_rollups(days=7)
        self.configs = {name: evaluator.project_config(name, fresh=True) for name in rollups.FLEET_PROJECTS}
        self.keys = {name: demo_fleet._keys(evaluator, name) for name in rollups.FLEET_PROJECTS}
        self.readiness = {name: demo_fleet._readiness_rows(evaluator, name) for name in rollups.FLEET_PROJECTS}
        self.overview = rollups.overview(self.rollups, evaluator.list_project_configs(), 7, today=MONDAY.date())
        self.last_tick = MONDAY + datetime.timedelta(minutes=15 * (len(summaries) - 1))


@pytest.fixture(scope="module")
def a_day() -> Day:
    """Ninety-six ticks, one simulated Monday, on a store of its own.

    The clock is put back before any test reads the result, so no other test
    in this module runs on it.
    """
    with pytest.MonkeyPatch.context() as patch:
        clock = SimulatedClock(MONDAY)
        clock.install(patch)
        evaluator = _fresh_evaluator()
        day = Day(evaluator, _run_ticks(evaluator, clock, MONDAY, 96), clock)
    return day


# ---------------------------------------------------------------- the event


@pytest.mark.parametrize(
    "event, expected",
    [
        ({"threefold_fleet": {"tick": 1}}, True),
        ({"threefold_fleet": {"tick": 2}}, False),
        ({"threefold_fleet": {"tick": "1"}}, False),
        ({"threefold_fleet": {"tick": True}}, False),
        ({"threefold_fleet": {"tick": 1.0}}, False),
        ({"threefold_fleet": {"tick": 1, "at": "2026-09-01T00:00:00+00:00"}}, False),
        ({"threefold_fleet": {}}, False),
        ({"threefold_fleet": 1}, False),
        ({"threefold_fleet": {"tick": 1}, "requestContext": {"http": {"method": "POST"}}}, False),
        ({"threefold_fleet": {"tick": 1}, "body": "{}"}, False),
        ({"threefold_fleet": {"tick": 1}, "rawPath": "/"}, False),
        ({"body": '{"threefold_fleet": {"tick": 1}}', "requestContext": {"http": {"method": "POST"}}}, False),
        ({}, False),
        (None, False),
        ([{"threefold_fleet": {"tick": 1}}], False),
        ('{"threefold_fleet": {"tick": 1}}', False),
    ],
)
def test_only_the_schedule_s_exact_event_is_a_tick(event, expected) -> None:
    assert demo_fleet.is_tick_event(event) is expected


def test_the_event_carries_no_time_so_nothing_can_be_backdated() -> None:
    """The tick's moment is the clock's when it runs: the event has nothing else to say."""
    assert "now" not in inspect.signature(demo_fleet.run_scheduled_tick).parameters
    assert not demo_fleet.is_tick_event({"threefold_fleet": {"tick": 1, "now": "2026-01-01T00:00:00+00:00"}})


# ---------------------------------------------------------------- the plan


def test_a_tick_is_planned_from_its_bucket_alone() -> None:
    bucket = A_WEEK[40]
    assert demo_fleet.plan_tick(bucket) == demo_fleet.plan_tick(bucket)
    assert demo_fleet.plan_tick(bucket) != demo_fleet.plan_tick(bucket + 1)


def test_the_bucket_is_the_quarter_hour_the_moment_falls_in() -> None:
    at = datetime.datetime(2026, 9, 21, 10, 14, 59, tzinfo=UTC)
    assert demo_fleet.bucket_of(at) == demo_fleet.bucket_of(at.replace(minute=0, second=0))
    assert demo_fleet.bucket_of(at.replace(minute=15, second=0)) == demo_fleet.bucket_of(at) + 1


def test_every_tick_of_a_week_sends_between_twenty_and_forty_calls() -> None:
    """Fewest is every call sent whatever is refused and whatever the hook keeps; most is every call it may send."""
    assert (demo_fleet.MIN_CALLS, demo_fleet.MAX_CALLS) == (20, 40)
    outside = [
        (bucket, plan.fewest, plan.most)
        for bucket, plan in ((bucket, demo_fleet.plan_tick(bucket)) for bucket in A_WEEK)
        if not demo_fleet.MIN_CALLS <= plan.fewest <= plan.most <= demo_fleet.MAX_CALLS
    ]
    assert not outside, outside


def test_working_hours_are_busier_than_nights_and_weekends() -> None:
    def mean(buckets):
        return sum(demo_fleet.plan_tick(b).target for b in buckets) / len(buckets)

    monday = A_WEEK[:96]
    working = [b for index, b in enumerate(monday) if 7 * 4 <= index < 18 * 4]
    night = [b for index, b in enumerate(monday) if index < 6 * 4]
    saturday_working = [b for index, b in enumerate(A_WEEK[5 * 96:6 * 96]) if 7 * 4 <= index < 18 * 4]
    assert mean(working) > mean(saturday_working) > mean(night) - 5
    assert mean(working) > mean(night) + 10


def _week_calls() -> List[demo_fleet.PlannedCall]:
    return [call for bucket in A_WEEK[:96] for call in demo_fleet.plan_tick(bucket).calls]


def test_every_call_is_a_hook_call_of_the_fleet_that_asks_for_no_explanation() -> None:
    for call in _week_calls():
        body = call.body()
        assert (body["origin"], body["explain"], body["dry_run"], body["hook_mode"]) == ("hook", False, False, "managed")
        assert body["agent"] in demo_fleet.AGENTS and body["project_name"] in rollups.FLEET_PROJECTS
        assert SESSION.match(body["session_id"]), body["session_id"]
        assert body["session_id"].split("-")[1] == demo_fleet.PROJECT_BY_NAME[body["project_name"]].suffix
        assert re.fullmatch(r"[0-9a-f]{12}", body["developer"])
        request = call.request()
        assert request.explain is False and request.origin == "hook" and request.warnings == []
        assert request.projected_input_tokens == request.projected_output_tokens == 0, "A hook declares no tokens"


def test_every_agent_uses_its_own_tool_names() -> None:
    tools = collections.defaultdict(set)
    for call in _week_calls():
        tools[call.agent].add(call.step.tool)
    assert tools["claude-code"] <= {"Read", "Edit", "Write", "Bash"}
    assert tools["codex"] <= {"shell", "apply_patch"}
    assert tools["antigravity"] <= {"view_file", "write_to_file", "replace_file_content", "run_command"}
    assert all(len(names) >= 2 for names in tools.values())


def test_a_day_is_mostly_ordinary_work_with_every_kind_of_trouble_in_it() -> None:
    intents = collections.Counter(call.step.intent for call in _week_calls() if not call.step.if_refused)
    total = sum(intents.values())
    assert intents["ordinary"] / total > 0.75, intents
    for kind in ("layering", "noisy", "shell_write", "protected", "loop", "credential"):
        assert intents[kind] >= 3, (kind, intents)


def test_sessions_last_an_hour_and_every_pair_works_in_a_day() -> None:
    per_session = collections.defaultdict(set)
    for bucket in A_WEEK[:96]:
        for call in demo_fleet.plan_tick(bucket).calls:
            per_session[call.session_id].add(bucket)
    assert all(max(buckets) - min(buckets) < demo_fleet.TICKS_PER_SESSION for buckets in per_session.values())
    pairs = {(call.project, call.agent) for call in _week_calls()}
    assert len(pairs) == len(rollups.FLEET_PROJECTS) * len(demo_fleet.AGENTS)


def test_the_source_holds_no_credential_shape_and_the_made_up_ones_are_caught() -> None:
    source = inspect.getsource(demo_fleet)
    for _, pattern in SecretScanner.PATTERNS:
        assert not pattern.search(source), f"{pattern.pattern} matches the fleet's own source"
    made_up = [
        call for call in _week_calls() if call.step.intent == "credential"
    ]
    assert made_up
    for call in made_up:
        text = " ".join(str(value) for value in call.step.arguments.values())
        assert any(pattern.search(text) for _, pattern in SecretScanner.PATTERNS), text
        for stage in (None, stages.OBSERVE, stages.ENFORCE):
            assert demo_fleet.held_on_machine(call.step, stage) == demo_fleet.HELD_CREDENTIAL, "Never sent, in any stage"
    others = [call for call in _week_calls() if call.step.intent != "credential"]
    assert not [call for call in others if demo_fleet.carries_credential(call.step)]


def test_the_six_projects_are_the_ones_the_overview_counts_as_the_fleet() -> None:
    assert tuple(project.name for project in demo_fleet.PROJECTS) == rollups.FLEET_PROJECTS
    assert {project.stack for project in demo_fleet.PROJECTS} == set(demo_fleet.STACKS)


def test_readiness_is_read_over_the_window_the_project_page_reads() -> None:
    assert demo_fleet.READINESS_DAYS == app_routes.PROJECT_DAYS[0]


# ---------------------------------------------------------------- determinism


def _ledger_shape(evaluator) -> List[tuple]:
    """Every row, in the order the calls were made (the store keeps it), without ids or stamps."""
    rows = _rows(evaluator)
    return [(row["session_id"], row["tool_name"], row["status"], row["rule_key"], row["target"], row["stage"],
             row.get("review")) for row in rows]


def test_the_same_tick_from_the_same_state_gives_the_same_verdicts_and_labels(monkeypatch) -> None:
    moment = MONDAY + datetime.timedelta(hours=10)
    outcomes = []
    for _ in range(2):
        clock = SimulatedClock(moment)
        clock.install(monkeypatch)
        evaluator = _fresh_evaluator()
        summaries = _run_ticks(evaluator, clock, moment, 3)
        outcomes.append((summaries, _ledger_shape(evaluator)))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0][0]["calls"] >= demo_fleet.MIN_CALLS


def test_a_tick_on_real_time_is_reproducible_too() -> None:
    """Without a simulated clock: two stores, one moment, the same answers."""
    moment = datetime.datetime.now(UTC)
    first, second = _fresh_evaluator(), _fresh_evaluator()
    one, two = demo_fleet.run_tick(first, now=moment), demo_fleet.run_tick(second, now=moment)
    assert one == two
    shape = [(row[0], row[1], row[2], row[3], row[4]) for row in _ledger_shape(first)]
    assert shape == [(row[0], row[1], row[2], row[3], row[4]) for row in _ledger_shape(second)]


# ---------------------------------------------------------------- a day of it


def test_every_tick_of_the_day_stayed_inside_its_bounds(a_day: Day) -> None:
    for summary in a_day.summaries:
        assert demo_fleet.MIN_CALLS <= summary["calls"] <= demo_fleet.MAX_CALLS, summary
        assert summary["calls"] == sum(summary["verdicts"].values())
        assert summary["cut_short"] is False


def test_every_call_of_the_day_reached_the_ledger_as_a_managed_hook_call(a_day: Day) -> None:
    assert len(a_day.rows) == sum(summary["calls"] for summary in a_day.summaries)
    for row in a_day.rows:
        assert (row["origin"], row["hook_mode"], row["dry_run"]) == ("hook", "managed", False)
        assert row["agent"] in demo_fleet.AGENTS and row["project_name"] in rollups.FLEET_PROJECTS
        assert SESSION.match(row["session_id"])
    assert {row["timestamp"][:10] for row in a_day.rows} == {str(MONDAY.date())}, "Written when it ran, not before"


def _kind(row: Dict[str, Any]) -> str:
    if row["status"] != "APPROVED":
        return "refused"
    return "observed" if row["rule_key"] != "NONE" else "approved"


def test_the_mix_is_mostly_approved_with_would_refuse_and_refused_calls_beside_it(a_day: Day) -> None:
    kinds = collections.Counter(_kind(row) for row in a_day.rows)
    total = sum(kinds.values())
    assert kinds["approved"] / total > 0.8
    assert kinds["observed"] >= 50 and kinds["refused"] >= 20, kinds


def test_observe_records_what_enforce_refuses(a_day: Day) -> None:
    """The same kinds of call, recorded in Observe and refused in Enforce."""
    seen = collections.defaultdict(set)
    for row in a_day.rows:
        if row["rule_key"] != "NONE":
            seen[row["rule_key"]].add((row["stage"], _kind(row)))
    for key in ("LOOP", "PROTECTED_PATH"):
        assert ("observe", "observed") in seen[key] and ("enforce", "refused") in seen[key], (key, seen[key])
    assert "CREDENTIAL" not in seen, "A hook refuses a credential on the machine, so none reaches the service"
    layering = {key: value for key, value in seen.items() if key.endswith("-stays-pure")}
    assert any(("observe", "observed") in value for value in layering.values())
    assert any(("enforce", "refused") in value for value in layering.values())
    assert "UNREADABLE_WRITE" in seen


def test_a_credential_never_leaves_the_machine(a_day: Day) -> None:
    """Every credential an agent tries is held where a hook holds it: nothing of it is sent or stored.

    The hook refuses a credential before anything is sent, in every mode, so
    no ledger row, session history, rollup or configuration may carry one.
    """
    stored = [key for key, item in a_day.items.items() if not SecretScanner.scan_arguments(item)[0]]
    assert not stored, stored[:5]
    assert not [row for row in a_day.rows if row["rule_key"] == "CREDENTIAL"]
    tried = sum(
        1
        for summary in a_day.summaries
        for call in demo_fleet.plan_tick(summary["tick"]).calls
        if call.step.intent == "credential"
    )
    held = sum(summary["held_on_machine"][demo_fleet.HELD_CREDENTIAL] for summary in a_day.summaries)
    assert tried > 0 and held == tried, (tried, held)


def test_the_agent_answers_a_credential_held_on_the_machine(a_day: Day) -> None:
    """Refused on the machine, the agent reads the value from the environment instead, and that is sent."""
    answers = [
        item for key, item in a_day.items.items()
        if key.startswith("SESSION#") and "env('AWS_ACCESS_KEY_ID')" in str(item.get("history_json"))
    ]
    held = sum(summary["held_on_machine"][demo_fleet.HELD_CREDENTIAL] for summary in a_day.summaries)
    assert answers and len(answers) <= held


def test_a_hook_settings_write_goes_without_its_content_or_not_at_all(a_day: Day) -> None:
    """As the hook sends it: the path alone while the project observes, nothing while it enforces."""
    settings = set(demo_fleet.HOOK_SETTINGS.values())
    sent = [row for row in a_day.rows if row["target"] in settings]
    assert sent, "A settings write in an observing project reaches the service"
    for row in sent:
        assert (row["stage"], row["rule_key"], row["status"]) == ("observe", "PROTECTED_PATH", "APPROVED"), row
    histories = [
        entry
        for key, item in a_day.items.items() if key.startswith("SESSION#")
        for entry in json.loads(item.get("history_json") or "[]")
        if (entry.get("arguments") or {}).get("file_path") in settings
    ]
    assert histories
    for entry in histories:
        assert entry["arguments"]["content"] == "" and entry["arguments"]["note"] == demo_fleet.CONTENT_NOT_SENT
    held = sum(summary["held_on_machine"][demo_fleet.HELD_HOOK_SETTINGS] for summary in a_day.summaries)
    assert held > 0, "An enforcing project's hook refuses the write on the machine"


@pytest.mark.parametrize(
    "step, stage, held",
    [
        (demo_fleet._Tools("codex").write(".codex/hooks.json", '{"hooks": {}}\n'), stages.ENFORCE, "hook_settings"),
        (demo_fleet._Tools("codex").write(".codex/hooks.json", '{"hooks": {}}\n'), stages.OBSERVE, None),
        (demo_fleet._Tools("codex").write(".codex/hooks.json", '{"hooks": {}}\n'), None, None),
        (demo_fleet._Tools("claude-code").write("src/acme_payments/settings.py", "DEBUG = False\n"), stages.ENFORCE, None),
        (demo_fleet._Tools("claude-code").run("cat .env"), stages.ENFORCE, None),
    ],
)
def test_what_the_hook_keeps_on_the_machine(step, stage, held) -> None:
    assert demo_fleet.held_on_machine(step, stage) == held


def test_a_settings_write_the_hook_sends_carries_its_path_and_no_content() -> None:
    step = demo_fleet._Tools("antigravity").write(".agents/hooks.json", '{"hooks": {}}\n')
    sent = demo_fleet.as_sent(step)
    assert sent.arguments == {"file_path": ".agents/hooks.json", "content": "", "note": demo_fleet.CONTENT_NOT_SENT}
    assert step.arguments["content"], "The plan itself is left as it was"
    ordinary = demo_fleet._Tools("antigravity").write("src/mobile/config.ts", "export const retries = 3;\n")
    assert demo_fleet.as_sent(ordinary) is ordinary


def test_the_loop_rule_stops_a_repeated_build_without_halting_the_session(a_day: Day) -> None:
    rows = sorted(a_day.rows, key=lambda row: row["timestamp"])
    loops = [row for row in rows if row["rule_key"] == "LOOP"]
    assert loops
    for row in loops:
        assert row["tool_name"] in ("Bash", "shell", "run_command")
    assert not [row for row in rows if row["rule_key"] == "HALTED_SESSION"], "A hook's loop never halts its session"


def test_a_refused_agent_mostly_corrects_itself(a_day: Day) -> None:
    """After a layering refusal, a later write to the same file in the same session is let through, mostly."""
    rows = sorted(a_day.rows, key=lambda row: row["timestamp"])
    refused = [row for row in rows if row["status"] == "BLOCKED_BOUNDARY_VIOLATION" and row["rule_key"].endswith("-stays-pure")]
    assert refused
    corrected = 0
    for row in refused:
        after = [other for other in rows if other["session_id"] == row["session_id"] and other["timestamp"] > row["timestamp"]]
        if any(other["target"] == row["target"] and other["status"] == "APPROVED" for other in after[:3]):
            corrected += 1
    assert 0.5 <= corrected / len(refused) < 1.0, (corrected, len(refused))


def test_the_operator_labels_mostly_correct_and_a_few_false_alarms_on_the_noisy_rule(a_day: Day) -> None:
    reviewed = [row for row in a_day.rows if row.get("review")]
    labels = collections.Counter(row["review"] for row in reviewed)
    assert labels["correct"] > 5 * labels["false_alarm"] > 0, labels
    for row in reviewed:
        assert row["reviewed_by"] == demo_fleet.REVIEWER == "fleet"
        assert row["review_note"] == demo_fleet.NOTES[row["review"]]
        if row["review"] == "false_alarm":
            assert row["rule_key"] == demo_fleet.NOISY_RULE and row["target"].startswith("tests/domain/"), row
    labelled = sum(summary["labelled"]["correct"] + summary["labelled"]["false_alarm"] for summary in a_day.summaries)
    assert labelled == len(reviewed)


def test_every_label_is_counted_in_its_day_s_rollup(a_day: Day) -> None:
    for label in ("correct", "false_alarm"):
        counted = sum(int(item.get(f"review:{label}", 0)) for item in a_day.rollups)
        assert counted == sum(1 for row in a_day.rows if row.get("review") == label)
    observed_reviewed = sum(1 for row in a_day.rows if row.get("review") and row["status"] == "APPROVED")
    assert sum(int(item.get("reviewed:observed", 0)) for item in a_day.rollups) == observed_reviewed


def test_a_small_review_queue_is_left_for_a_visitor_to_see(a_day: Day) -> None:
    """Older calls are swept; some of the last few hours' would-refuse calls wait."""
    waiting = [row for row in a_day.rows if _kind(row) == "observed" and not row.get("review")]
    assert 0 < len(waiting) < 60, len(waiting)
    swept_before = (a_day.last_tick - demo_fleet.SWEEP_MIN_AGE).isoformat()
    assert not [row for row in waiting if row["timestamp"] < swept_before], "A call older than the sweep's age waits"
    assert a_day.overview["totals"]["needs_review"] == len(waiting)


def test_about_half_the_projects_enforce_and_never_more_than_three(a_day: Day) -> None:
    counts = [sum(1 for stage in summary["stages"].values() if stage == "enforce") for summary in a_day.summaries]
    assert max(counts) <= demo_fleet.TARGET_ENFORCE
    assert counts[-1] == demo_fleet.TARGET_ENFORCE
    assert sum(1 for count in counts if count == demo_fleet.TARGET_ENFORCE) > len(counts) / 3


def test_promotions_follow_readiness_and_leave_the_noisy_rule_observing(a_day: Day) -> None:
    actions = [action for summary in a_day.summaries for action in summary["actions"]]
    promotions = [action for action in actions if action["action"] == "promote"]
    assert promotions
    for action in promotions:
        assert action["enforce"], action
        assert set(action["enforce"]) | set(action["observe"]) == set(a_day.keys[action["project"]])
    for name, config in a_day.configs.items():
        assert config is not None and config["sandbox"] is False
        assert all(entry["by"] == "fleet" for entry in config["history"])
        if config["stage"] == stages.ENFORCE:
            for row in a_day.readiness[name]:
                if row["state"] == "noisy":
                    assert row["mode_now"] == stages.OBSERVE, (name, row)


def test_the_noisy_rule_reads_noisy_on_a_python_project_s_page(a_day: Day) -> None:
    states = {name: {row["rule_key"]: row["state"] for row in a_day.readiness[name]}
              for name in ("Acme-Payments", "Acme-Search")}
    assert any(rules[demo_fleet.NOISY_RULE] == "noisy" for rules in states.values()), states


def test_the_overview_counts_the_day_as_the_fleet_s(a_day: Day) -> None:
    payload = a_day.overview
    calls = sum(summary["calls"] for summary in a_day.summaries)
    assert payload["sources"]["fleet"] == {"calls": calls, "projects": 6}
    assert payload["totals"]["calls"] == calls and payload["totals"]["coding_agents"] == 3
    assert {row["source"] for row in payload["by_project"]} == {"fleet"}
    assert payload["totals"]["false_alarms"] > 0 and payload["totals"]["refused"] > 0
    assert payload["stages"] == {"observe": 3, "enforce": 3}
    by_rule = {entry["rule_key"] for entry in payload["by_rule"]}
    assert by_rule >= {"LOOP", "PROTECTED_PATH", "UNREADABLE_WRITE", demo_fleet.NOISY_RULE}
    assert "CREDENTIAL" not in by_rule, "The fleet's credentials are refused on the machine and never counted here"


# ---------------------------------------------------------------- the operator, alone


def _configs(enforcing=(), demoted_at=None, promoted_at=None) -> Dict[str, Any]:
    configs = {}
    for name in rollups.FLEET_PROJECTS:
        config = stages.new_config(MONDAY.isoformat(), stage=stages.ENFORCE if name in enforcing else stages.OBSERVE)
        config["demoted_at"], config["promoted_at"] = demoted_at, promoted_at
        configs[name] = config
    return configs


def test_with_fewer_than_three_enforcing_the_operator_promotes_about_every_other_tick() -> None:
    now = MONDAY + datetime.timedelta(days=2)
    actions = [demo_fleet.stage_actions(bucket, now, _configs(enforcing=rollups.FLEET_PROJECTS[:1])) for bucket in A_WEEK]
    promoted = [action[0] for action in actions if action]
    assert all(action["action"] == "promote" and action["project"] != rollups.FLEET_PROJECTS[0] for action in promoted)
    assert 0.4 < len(promoted) / len(actions) < 0.6


def test_with_three_enforcing_the_operator_demotes_rarely() -> None:
    now = MONDAY + datetime.timedelta(days=2)
    enforcing = rollups.FLEET_PROJECTS[:3]
    actions = [demo_fleet.stage_actions(bucket, now, _configs(enforcing=enforcing)) for bucket in A_WEEK]
    demoted = [action[0] for action in actions if action]
    assert all(action["action"] == "demote" and action["project"] in enforcing for action in demoted)
    assert 0 < len(demoted) < len(actions) * 0.03, len(demoted)


def test_more_than_three_enforcing_is_brought_back_at_once() -> None:
    now = MONDAY + datetime.timedelta(days=2)
    for bucket in A_WEEK[:50]:
        (action,) = demo_fleet.stage_actions(bucket, now, _configs(enforcing=rollups.FLEET_PROJECTS[:4]))
        assert action["action"] == "demote"


def test_a_project_just_demoted_is_not_promoted_and_one_just_promoted_is_not_demoted() -> None:
    now = MONDAY + datetime.timedelta(days=2)
    recent = (now - datetime.timedelta(hours=1)).isoformat()
    for bucket in A_WEEK:
        assert not demo_fleet.stage_actions(bucket, now, _configs(demoted_at=recent))
        assert not demo_fleet.stage_actions(bucket, now, _configs(enforcing=rollups.FLEET_PROJECTS[:4], promoted_at=recent))
    later = now + demo_fleet.COOLDOWN
    assert any(demo_fleet.stage_actions(bucket, later, _configs(demoted_at=recent)) for bucket in A_WEEK[:20])


@pytest.mark.parametrize(
    "row, label",
    [
        ({"status": "APPROVED", "rule_key": "python-domain-stays-pure", "target": "tests/domain/test_fee_api.py"}, "false_alarm"),
        ({"status": "APPROVED", "rule_key": "python-domain-stays-pure", "observed_target": "tests/domain/test_x.py",
          "target": "tests/domain/test_x.py"}, "false_alarm"),
        ({"status": "APPROVED", "rule_key": "python-domain-stays-pure", "target": "src/acme_payments/domain/fee.py"}, "correct"),
        ({"status": "APPROVED", "rule_key": "LOOP", "target": ""}, "correct"),
        ({"status": "BLOCKED_BOUNDARY_VIOLATION", "rule_key": "python-domain-stays-pure",
          "target": "tests/domain/test_fee_api.py"}, "false_alarm"),
        ({"status": "BLOCKED_BOUNDARY_VIOLATION", "rule_key": "java-domain-stays-pure",
          "target": "src/main/java/com/acme/ledger/domain/Account.java"}, None),
        ({"status": "APPROVED", "rule_key": "NONE", "target": "README.md"}, None),
        ({"status": "APPROVED", "rule_key": "web-domain-stays-pure", "target": "tests/domain/cart.test.ts"}, "correct"),
    ],
)
def test_what_the_operator_labels_and_how(row, label) -> None:
    assert demo_fleet.label_for(row) == label


def _seed(evaluator, project: str, calls: List[tuple]) -> None:
    for index, (tool, action, arguments) in enumerate(calls):
        body = {
            "session_id": f"fleet-payments-codex-{index}", "project_name": project, "developer": "0a1b2c3d4e5f",
            "tool_name": tool, "action_type": action, "arguments": arguments, "agent": "codex",
            "origin": "hook", "explain": False, "dry_run": False, "hook_mode": "managed",
        }
        evaluator.evaluate_tool_call(dtos.ToolCallRequestDTO.from_payload(body))


NOISY_CALL = ("apply_patch", "FILE_WRITE", {"file_path": "tests/domain/test_fee_api.py",
                                            "content": "from fastapi.testclient import TestClient\n"})
CROSSING = ("apply_patch", "FILE_WRITE", {"file_path": "src/acme_payments/domain/fee.py", "content": "import boto3\n"})
LOOPING = ("shell", "COMMAND_EXEC", {"command": "python -m build --wheel"})


def test_a_promotion_enforces_what_is_ready_and_leaves_the_noisy_rule_observing(monkeypatch) -> None:
    monkeypatch.setattr(demo_fleet, "MIN_CALLS_OBSERVED", 1)
    evaluator = _fresh_evaluator()
    now = datetime.datetime.now(UTC)
    demo_fleet._ensure_configured(evaluator, now)
    _seed(evaluator, "Acme-Payments", [CROSSING, NOISY_CALL, ("shell", "COMMAND_EXEC", {"command": "cat .env"})])
    summary = demo_fleet.TickSummary(tick=0)
    action = demo_fleet._promote(evaluator, "Acme-Payments", datetime.datetime.now(UTC), summary, None)
    assert action is not None and action["action"] == "promote"
    assert demo_fleet.NOISY_RULE in action["observe"] and "PROTECTED_PATH" in action["enforce"]
    assert summary.labelled == {"correct": 2, "false_alarm": 1}, "The queue is reviewed before promoting"
    config = evaluator.project_config("Acme-Payments", fresh=True)
    assert config["stage"] == stages.ENFORCE and demo_fleet.NOISY_RULE in config["observe_rules"]
    assert config["history"][-1]["action"] == "promote" and config["history"][-1]["by"] == "fleet"


def test_a_project_with_nothing_ready_is_not_promoted(monkeypatch) -> None:
    monkeypatch.setattr(demo_fleet, "MIN_CALLS_OBSERVED", 1)
    evaluator = _fresh_evaluator()
    demo_fleet._ensure_configured(evaluator, datetime.datetime.now(UTC))
    _seed(evaluator, "Acme-Payments", [NOISY_CALL, ("shell", "COMMAND_EXEC", {"command": "pytest -q"})])
    action = demo_fleet._promote(evaluator, "Acme-Payments", datetime.datetime.now(UTC), demo_fleet.TickSummary(tick=0), None)
    assert action is None, "Only a noisy rule and quiet ones: nothing has earned enforcement"
    assert evaluator.project_config("Acme-Payments", fresh=True)["stage"] == stages.OBSERVE


def test_a_project_observed_too_briefly_is_not_promoted() -> None:
    evaluator = _fresh_evaluator()
    demo_fleet._ensure_configured(evaluator, datetime.datetime.now(UTC))
    _seed(evaluator, "Acme-Payments", [CROSSING])
    summary = demo_fleet.TickSummary(tick=0)
    assert demo_fleet._promote(evaluator, "Acme-Payments", datetime.datetime.now(UTC), summary, None) is None
    assert summary.labelled == {"correct": 0, "false_alarm": 0}, "Its queue is left for the sweep"


def test_an_enforcing_rule_that_turns_noisy_goes_back_to_observing(monkeypatch) -> None:
    monkeypatch.setattr(demo_fleet, "MIN_CALLS_OBSERVED", 1)
    evaluator = _fresh_evaluator()
    demo_fleet._ensure_configured(evaluator, datetime.datetime.now(UTC))
    _seed(evaluator, "Acme-Payments", [CROSSING])
    first = demo_fleet._promote(evaluator, "Acme-Payments", datetime.datetime.now(UTC), demo_fleet.TickSummary(tick=0), None)
    assert demo_fleet.NOISY_RULE in first["enforce"], "No false alarm yet: the rule was Ready"
    _seed(evaluator, "Acme-Payments", [NOISY_CALL])
    rows = [row for row in _rows(evaluator) if row["target"] == NOISY_CALL[2]["file_path"]]
    assert rows[0]["status"] == "BLOCKED_BOUNDARY_VIOLATION", "Enforcing, the rule refused a test module"
    summary = demo_fleet.TickSummary(tick=0)
    moment = datetime.datetime.now(UTC)
    demo_fleet._sweep(evaluator, moment, summary, min_age=datetime.timedelta(0))
    assert summary.labelled["false_alarm"] == 1 and summary.false_alarms_in == {"Acme-Payments"}
    action = demo_fleet._observe_noisy(evaluator, "Acme-Payments", moment)
    assert action == {"action": "observe_noisy", "project": "Acme-Payments", "observe": [demo_fleet.NOISY_RULE]}
    config = evaluator.project_config("Acme-Payments", fresh=True)
    assert config["stage"] == stages.ENFORCE and demo_fleet.NOISY_RULE in config["observe_rules"]
    _seed(evaluator, "Acme-Payments", [NOISY_CALL])
    same = [row for row in _rows(evaluator) if row["target"] == NOISY_CALL[2]["file_path"]]
    assert [row["status"] for row in same] == ["BLOCKED_BOUNDARY_VIOLATION", "APPROVED"], (
        "Observing again, the same call runs and is recorded"
    )


def test_a_row_labelled_this_tick_is_not_labelled_again_by_a_stale_read(monkeypatch) -> None:
    """The ledger query is eventually consistent: it can return a row just labelled without its review."""
    evaluator = _fresh_evaluator()
    demo_fleet._ensure_configured(evaluator, datetime.datetime.now(UTC))
    _seed(evaluator, "Acme-Payments", [CROSSING, NOISY_CALL])
    repo = evaluator.session_repo
    fresh_read, label = repo.read_decision_day, repo.label_decision
    labels: List[str] = []

    def stale_read(*args, **kwargs):
        rows, after = fresh_read(*args, **kwargs)
        return [{key: value for key, value in row.items() if key != "review"} for row in rows], after

    def counted(*args, **kwargs):
        labels.append(args[1])
        return label(*args, **kwargs)

    monkeypatch.setattr(repo, "read_decision_day", stale_read)
    monkeypatch.setattr(repo, "label_decision", counted)
    summary = demo_fleet.TickSummary(tick=0)
    moment = datetime.datetime.now(UTC) + datetime.timedelta(seconds=1)
    demo_fleet._sweep(evaluator, moment, summary, min_age=datetime.timedelta(0))
    demo_fleet._sweep(evaluator, moment, summary, min_age=datetime.timedelta(0))
    assert summary.labelled == {"correct": 1, "false_alarm": 1}
    assert len(labels) == len(set(labels)) == 2, labels


def test_demotion_is_one_step_back_to_observe() -> None:
    evaluator = _fresh_evaluator()
    now = datetime.datetime.now(UTC)
    demo_fleet._ensure_configured(evaluator, now)
    keys = demo_fleet._keys(evaluator, "Acme-Ledger")
    evaluator.save_project_config("Acme-Ledger", stages.promoted(None, now.isoformat(), "fleet", keys, keys))
    assert demo_fleet._demote(evaluator, "Acme-Ledger", now) == {"action": "demote", "project": "Acme-Ledger"}
    config = evaluator.project_config("Acme-Ledger", fresh=True)
    assert config["stage"] == stages.OBSERVE and config["history"][-1]["action"] == "demote"


# ---------------------------------------------------------------- never the model, never past the deadline


def test_a_tick_never_asks_the_model(monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("The fleet asked the model for a sentence")

    for owner, name in (
        (BedrockGovernanceClient, "review_agent_action"),
        (BedrockGovernanceClient, "describe_availability"),
        (BedrockArchitecturalReviewer, "explain"),
        (BedrockArchitecturalReviewer, "review_action"),
    ):
        monkeypatch.setattr(owner, name, refuse)
    summary = demo_fleet.run_tick(_fresh_evaluator(), now=datetime.datetime.now(UTC))
    assert summary["calls"] >= demo_fleet.MIN_CALLS
    imported = set()
    for node in ast.walk(ast.parse(inspect.getsource(demo_fleet))):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not [name for name in imported if "bedrock" in name.lower() or "drafter" in name.lower()], imported


class _Context:
    def __init__(self, remaining_ms: int) -> None:
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms


def test_a_tick_stops_sending_well_before_the_function_times_out() -> None:
    """With less time left than the margin, nothing is sent and nothing is raised."""
    answer = demo_fleet.run_scheduled_tick(_fresh_evaluator(), _Context(int(demo_fleet.STOP_BEFORE_DEADLINE_SECONDS * 1000) - 1))
    summary = answer["threefold_fleet"]
    assert summary["ok"] is True and summary["cut_short"] is True and summary["calls"] == 0


def test_a_tick_with_time_to_spare_runs_whole() -> None:
    answer = demo_fleet.run_scheduled_tick(_fresh_evaluator(), _Context(15000))
    summary = answer["threefold_fleet"]
    assert summary["ok"] is True and summary["cut_short"] is False
    assert demo_fleet.MIN_CALLS <= summary["calls"] <= demo_fleet.MAX_CALLS
    assert set(summary) >= {"tick", "calls", "planned", "verdicts", "held_on_machine", "labelled", "actions", "stages", "seconds"}


def test_every_tick_leaves_one_line_in_the_function_s_log(caplog) -> None:
    """The line an operator reads to see ticks run, emitted even where the root logger is left at WARNING."""
    root = logging.getLogger()
    kept = root.level
    root.setLevel(logging.WARNING)
    try:
        assert logging.getLogger("threefold.fleet").isEnabledFor(logging.INFO)
        caplog.handler.setLevel(logging.NOTSET)
        demo_fleet.run_scheduled_tick(_fresh_evaluator(), _Context(15000))
    finally:
        root.setLevel(kept)
    lines = [record for record in caplog.records if record.name == "threefold.fleet" and record.levelno == logging.INFO]
    assert len(lines) == 1 and lines[0].getMessage().startswith("Demo fleet tick: {")
    assert '"seconds"' in lines[0].getMessage() and '"calls"' in lines[0].getMessage()


def test_a_failing_tick_is_answered_not_raised(monkeypatch) -> None:
    """Lambda retries an asynchronous invocation that raises, which would send the batch again."""
    evaluator = _fresh_evaluator()

    def broken(*_args, **_kwargs):
        raise RuntimeError("the store is unreachable")

    monkeypatch.setattr(evaluator, "evaluate_tool_call", broken)
    assert demo_fleet.run_scheduled_tick(evaluator, None) == {"threefold_fleet": {"ok": False}}


# ---------------------------------------------------------------- never twice


def test_a_tick_delivered_twice_sends_its_batch_once(monkeypatch) -> None:
    """The scheduler delivers at least once; a second delivery in the same quarter hour finds it claimed."""
    moment = datetime.datetime.now(UTC).replace(minute=7, second=0, microsecond=0)
    monkeypatch.setattr(demo_fleet, "_now", lambda: moment)
    evaluator = _fresh_evaluator()
    first = demo_fleet.run_scheduled_tick(evaluator, _Context(15000))["threefold_fleet"]
    rows = len(_rows(evaluator))
    assert first["ok"] is True and first["calls"] == rows >= demo_fleet.MIN_CALLS
    second = demo_fleet.run_scheduled_tick(evaluator, _Context(15000))["threefold_fleet"]
    assert second == {"ok": True, "tick": demo_fleet.bucket_of(moment), "skipped": "not claimed"}
    assert len(_rows(evaluator)) == rows, "Nothing was sent the second time"
    monkeypatch.setattr(demo_fleet, "_now", lambda: moment + datetime.timedelta(minutes=15))
    third = demo_fleet.run_scheduled_tick(evaluator, _Context(15000))["threefold_fleet"]
    assert third["ok"] is True and third["calls"] >= demo_fleet.MIN_CALLS, "The next quarter hour is a tick of its own"


def test_a_tick_whose_bucket_cannot_be_claimed_sends_nothing(monkeypatch) -> None:
    evaluator = _fresh_evaluator()
    monkeypatch.setattr(evaluator.session_repo, "claim_once", lambda name, ttl: False)
    answer = demo_fleet.run_scheduled_tick(evaluator, _Context(15000))["threefold_fleet"]
    assert answer["skipped"] == "not claimed" and not _rows(evaluator)


class _ClaimTable:
    """Enough of a table to see the claim's conditional put."""

    def __init__(self, down: bool = False) -> None:
        self.puts: List[Dict[str, Any]] = []
        self.keys = set()
        self.down = down

    def put_item(self, Item, ConditionExpression=None, **_):  # noqa: N803 - boto3 spells it this way
        if self.down:
            raise ConnectionError("simulated outage")
        self.puts.append({"Item": Item, "ConditionExpression": ConditionExpression})
        if (Item["PK"], Item["SK"]) in self.keys:
            error = type("ConditionalCheckFailedException", (Exception,), {})()
            error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            raise error
        self.keys.add((Item["PK"], Item["SK"]))


def _repo_on(table: _ClaimTable) -> DynamoDBSessionRepository:
    return DynamoDBSessionRepository(boto3_resource=type("R", (), {"Table": lambda self, name: table})())


def test_the_store_grants_a_claim_once_across_containers() -> None:
    table = _ClaimTable()
    first, second = _repo_on(table), _repo_on(table)
    assert first.claim_once("fleet-tick-1", demo_fleet.CLAIM_TTL_SECONDS) is True
    assert second.claim_once("fleet-tick-1", demo_fleet.CLAIM_TTL_SECONDS) is False
    assert first.claim_once("fleet-tick-2", demo_fleet.CLAIM_TTL_SECONDS) is True
    put = table.puts[0]
    assert put["ConditionExpression"] == "attribute_not_exists(PK)"
    assert (put["Item"]["PK"], put["Item"]["SK"]) == ("RUNCLAIM#fleet-tick-1", "CLAIM")
    assert 0 < put["Item"]["ttl"] - int(datetime.datetime.now(UTC).timestamp()) <= demo_fleet.CLAIM_TTL_SECONDS


def test_a_store_that_cannot_be_reached_grants_no_claim() -> None:
    """Skipping a quarter hour is cheaper than risking it twice."""
    assert _repo_on(_ClaimTable(down=True)).claim_once("fleet-tick-1", 60) is False
