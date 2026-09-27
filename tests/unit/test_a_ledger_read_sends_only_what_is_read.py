"""The two big ledger reads ask the table for the fields they read, and count the same either way.

The overview's self-correction figure reads up to two thousand ledger rows on
every request, and the fleet's sweep up to twelve hundred on every tick. Every
attribute of every row is parsed on the way in, and a row carries some thirty,
so each read names the fields it reads and the table sends those alone. That
is only safe while the names are complete: a field read and not named would
come back missing, and a number would change without anything failing. So the
code that reads the rows is held to its list here, field by field, and each
figure is computed both ways over a ledger the fleet wrote and must agree.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import copy
import datetime
import functools
from typing import Any, Dict, List

import pytest

import threefold.domain.models as models
from threefold.application import demo_fleet, dtos, insights, ledger, rollups
from threefold.application import projects as stages
from threefold.application.evaluator import GovernanceEvaluator
from threefold.application.sandbox import create_sandbox
from threefold.infrastructure import dynamo_repo
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository
from threefold.interfaces import app_routes

UTC = datetime.timezone.utc
TICKS = 20
# The last tick of the ledger below, a Monday afternoon: the fleet's plan is
# drawn from each tick's quarter hour, so a fixed day is the same ledger on
# every run, and a working afternoon has every kind of call in it.
END = datetime.datetime(2026, 9, 21, 16, 0, tzinfo=UTC)
TODAY = END.date()
AFTER_THE_LAST_TICK = END + datetime.timedelta(minutes=5)
NOISY = {"file_path": "tests/domain/test_search_api.py", "content": "from fastapi.testclient import TestClient\n"}
CROSSING = {"file_path": "src/acme_search/domain/ranking.py", "content": "import boto3\n"}


class _Clock:
    """The service's clock, moved by the test: every reading is 20 ms after the last."""

    def __init__(self) -> None:
        self.now = END
        clock = self

        class _Moving(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                clock.now += datetime.timedelta(milliseconds=20)
                return clock.now if tz is not None else clock.now.replace(tzinfo=None)

        self.datetime = _Moving


def _legacy_rows(day: str) -> Dict[str, Dict[str, Any]]:
    """Two rows as an older writer left them: no rule key, no stage, one observed rule under its old name."""
    rows = {}
    for index, dry_run in enumerate((True, False)):
        stamp = f"{day}T00:00:0{index}+00:00"
        rows[f"DECISION#{day}#{stamp}#legacy-{index}"] = {
            "PK": f"DECISION#{day}", "SK": f"{stamp}#legacy-{index}", "timestamp": stamp,
            "verdict_id": f"legacy-{index}", "session_id": "legacy-session", "project_name": "Acme-Legacy",
            "status": "APPROVED", "dry_run": dry_run, "origin": "hook",
            "observed_rule": "python-domain-stays-pure",
            "observed_reason": "Layering rule 'python-domain-stays-pure' would refuse this write",
            "target": "src/acme/domain/order.py",
        }
    return rows


def _legacy_fleet_rows(day: str) -> Dict[str, Dict[str, Any]]:
    """Two of the fleet's calls on a test module as an older writer left them: a refusal and an observation.

    With no rule key stored, the key is read back off the reason, so these
    are the rows that need the sweep to fetch the reasons it would otherwise
    never read.
    """
    rows = {}
    for index, (status, reason) in enumerate((
        ("BLOCKED_BOUNDARY_VIOLATION", "Layering rule 'python-domain-stays-pure' refuses this write"),
        ("APPROVED", ""),
    )):
        stamp = f"{day}T12:00:0{index}+00:00"
        rows[f"DECISION#{day}#{stamp}#older-{index}"] = {
            "PK": f"DECISION#{day}", "SK": f"{stamp}#older-{index}", "timestamp": stamp,
            "verdict_id": f"older-{index}", "session_id": "fleet-payments-codex-older", "project_name": "Acme-Payments",
            "status": status, "reason": reason, "origin": "hook", "target": NOISY["file_path"],
            "observed_rules": [] if index == 0 else ["python-domain-stays-pure"],
            "observed_reason": "" if index == 0 else "Layering rule 'python-domain-stays-pure' would refuse this write",
        }
    return rows


@pytest.fixture(scope="module")
def fleet_store() -> Dict[str, Dict[str, Any]]:
    """What twenty ticks of the fleet, a sandbox and four old rows leave in a store."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(rollups.DEMO_FLEET_ENV, "true")
        clock = _Clock()
        for module in (dtos, models, dynamo_repo):
            patch.setattr(module, "datetime", clock.datetime)
        evaluator = GovernanceEvaluator(session_repo=DynamoDBSessionRepository())
        start = END - datetime.timedelta(minutes=15 * (TICKS - 1))
        clock.now = start
        # Two projects enforce from the start, so there are refusals for
        # agents to correct; the rest observe, so there are calls to label.
        demo_fleet._ensure_configured(evaluator, start)
        for name in ("Acme-Payments", "Acme-Treasury"):
            keys = demo_fleet._keys(evaluator, name)
            evaluator.save_project_config(name, stages.promoted(None, start.isoformat(), "fleet", keys, keys))
        for index in range(TICKS):
            moment = start + datetime.timedelta(minutes=15 * index)
            clock.now = moment
            demo_fleet.run_tick(evaluator, now=moment)
            if index == TICKS // 2:
                create_sandbox(evaluator)
        # A call on a test module and a layer crossing, both observed and
        # neither labelled yet, so the sweep gives both of its labels.
        evaluator.save_project_config("Acme-Search", stages.new_config(END.isoformat(), stage=stages.OBSERVE))
        for index, arguments in enumerate((NOISY, CROSSING)):
            evaluator.evaluate_tool_call(dtos.ToolCallRequestDTO.from_payload({
                "session_id": f"fleet-search-codex-{index}", "project_name": "Acme-Search",
                "developer": "0a1b2c3d4e5f", "tool_name": "apply_patch", "action_type": "FILE_WRITE",
                "arguments": arguments, "agent": "codex", "origin": "hook", "explain": False, "dry_run": False,
                "hook_mode": "managed",
            }))
        store = evaluator.session_repo._memory_store
        store.update(_legacy_rows(str(END.date())))
        store.update(_legacy_fleet_rows(str(END.date())))
        return copy.deepcopy(store)


def _repo_on(store: Dict[str, Dict[str, Any]]) -> DynamoDBSessionRepository:
    repo = DynamoDBSessionRepository()
    repo._memory_store = copy.deepcopy(store)
    return repo


def _days(store: Dict[str, Dict[str, Any]]) -> List[str]:
    return sorted({key.split("#")[1] for key in store if key.startswith("DECISION#")})


class _Recording(dict):
    """A row that notes each field read from it, and notes "*" when it is copied or walked whole."""

    def __init__(self, row: Dict[str, Any], seen: set) -> None:
        super().__init__(row)
        self.seen = seen

    def get(self, key, default=None):
        self.seen.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.seen.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.seen.add(key)
        return super().__contains__(key)

    def _whole(self, name):
        self.seen.add("*")
        return getattr(super(), name)

    def items(self):
        return self._whole("items")()

    def keys(self):
        return self._whole("keys")()

    def values(self):
        return self._whole("values")()

    def __iter__(self):
        return self._whole("__iter__")()

    def copy(self):
        return self._whole("copy")()


# ---------------------------------------------------------------- the store


def test_a_page_read_for_some_fields_is_the_same_page_holding_only_those(fleet_store) -> None:
    repo = _repo_on(fleet_store)
    fields = ("timestamp", "status", "stage", "observed_rules", "review")
    for day in _days(fleet_store):
        whole, whole_next = repo.read_decision_day(day, None, 7)
        some, some_next = repo.read_decision_day(day, None, 7, fields=fields)
        assert some_next == whole_next
        assert some == [dict({name: row[name] for name in fields}, _sk=row["_sk"]) for row in whole]


class _Table:
    """A live table's Query, as DynamoDB answers it: newest first, projected, and strict about names."""

    def __init__(self, items: List[Dict[str, Any]]) -> None:
        self.items = items
        self.queries: List[Dict[str, Any]] = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        names = kwargs.get("ExpressionAttributeNames") or {}
        projection = kwargs.get("ProjectionExpression")
        used = [part.strip() for part in projection.split(",")] if projection else []
        if set(names) - set(used):
            raise ValueError("ValidationException: Value provided in ExpressionAttributeNames unused in expressions")
        rows = sorted(self.items, key=lambda item: item["SK"], reverse=True)[: kwargs["Limit"]]
        if projection:
            wanted = [names.get(part, part) for part in used]
            rows = [{name: row[name] for name in wanted if name in row} for row in rows]
        return {"Items": rows}


class _Resource:
    def __init__(self, table) -> None:
        self.table = table

    def Table(self, _name):  # noqa: N802 - boto3 spells it this way
        return self.table


def test_a_live_page_asks_the_table_for_those_fields_and_what_they_are_made_of() -> None:
    day = "2026-09-22"
    items = [dict(item) for item in _legacy_rows(day).values()]
    items.append(dict(items[1], SK=f"{day}T09:00:00+00:00#modern", verdict_id="modern", stage="enforce",
                      rule_key="LOOP", observed_rules=["LOOP"], observed_rule="LOOP", reason="a sentence " * 20))
    table = _Table(items)
    repo = DynamoDBSessionRepository(boto3_resource=_Resource(table))
    rows, _ = repo.read_decision_day(day, None, 10, fields=("status", "stage", "observed_rules"))
    query = table.queries[-1]
    names = query["ExpressionAttributeNames"]
    placeholders = [part.strip() for part in query["ProjectionExpression"].split(",")]
    assert all(placeholder.startswith("#") for placeholder in placeholders), (
        "status is one of DynamoDB's reserved words, so no name is written bare"
    )
    assert set(names.values()) == {"SK", "status", "stage", "dry_run", "observed_rules", "observed_rule"}, (
        "The sort key for `_sk`, and what stage and observed_rules are made of on a row an older writer left"
    )
    assert rows == [
        {"status": "APPROVED", "stage": "enforce", "observed_rules": ["LOOP"], "_sk": f"{day}T09:00:00+00:00#modern"},
        {"status": "APPROVED", "stage": "enforce", "observed_rules": ["python-domain-stays-pure"],
         "_sk": f"{day}T00:00:01+00:00#legacy-1"},
        {"status": "APPROVED", "stage": "observe", "observed_rules": ["python-domain-stays-pure"],
         "_sk": f"{day}T00:00:00+00:00#legacy-0"},
    ]
    repo.read_decision_day(day, None, 10)
    assert "ProjectionExpression" not in table.queries[-1], "A reader that names nothing gets whole rows, as before"


@pytest.mark.parametrize(
    "fields", [demo_fleet.SWEEP_FIELDS, app_routes.SELF_CORRECTION_READ], ids=["sweep", "self-correction"]
)
def test_every_row_cleaned_from_what_the_table_sends_has_the_same_fields(fleet_store, fields) -> None:
    """Each ledger row, projected as a live table projects it, cleans to the fields it cleans to whole.

    The sweep's tests, and most of the figure's, run on the memory store,
    which cleans the whole item and then keeps the fields. A live table sends
    only what the projection names, so a field cleaned from an attribute it
    does not name would fall to its default there and nowhere else; here it
    differs.
    """
    fetched = set(dynamo_repo.decision_projection(fields)["ExpressionAttributeNames"].values())
    items = [item for key, item in fleet_store.items() if key.startswith("DECISION#")]
    assert any(not item.get("rule_key") for item in items), "Rows an older writer left are among them"
    for item in items:
        whole = dynamo_repo.clean_decision(item)
        sent = dynamo_repo.clean_decision({name: value for name, value in item.items() if name in fetched})
        assert {name: sent[name] for name in fields if name in sent} == {
            name: whole[name] for name in fields if name in whole
        }, item["SK"]


# ---------------------------------------------------------------- self-correction


def test_self_correction_reads_no_field_it_does_not_name(fleet_store) -> None:
    repo = _repo_on(fleet_store)
    seen: set = set()
    rows = []
    for day in _days(fleet_store):
        page, _ = repo.read_decision_day(day, None, 5000)
        rows.extend(_Recording(ledger.shown_row(row), seen) for row in page)
    figure = insights.self_correction(rows)
    assert figure["refusals_with_later_call"] > 0 and figure["self_corrected"] > 0, "The figure had something to count"
    assert seen <= set(insights.SELF_CORRECTION_FIELDS), sorted(seen - set(insights.SELF_CORRECTION_FIELDS))


@pytest.mark.parametrize("budget", [None, 37, 400])
@pytest.mark.parametrize("project", [None, "Acme-Payments", "Acme-Legacy", "unlabelled"])
def test_the_figure_is_the_same_read_whole_or_read_for_its_fields(fleet_store, budget, project) -> None:
    repo = _repo_on(fleet_store)
    whole = ledger.self_correction(repo.read_decision_day, 7, project, today=TODAY, budget=budget)
    asked = functools.partial(repo.read_decision_day, fields=app_routes.SELF_CORRECTION_READ)
    assert ledger.self_correction(asked, 7, project, today=TODAY, budget=budget) == whole
    routed = app_routes._self_correction_reader(repo.read_decision_day)
    assert ledger.self_correction(routed, 7, project, today=TODAY, budget=budget) == whole


def test_the_route_asks_for_the_figure_s_fields_and_to_hear_of_a_failure() -> None:
    asked: List[Dict[str, Any]] = []

    def reader(day, after=None, limit=200, *, raise_errors=False, fields=None):
        asked.append({"raise_errors": raise_errors, "fields": fields})
        return [], None

    ledger.self_correction(app_routes._self_correction_reader(reader), 1)
    assert asked == [{"raise_errors": True, "fields": app_routes.SELF_CORRECTION_READ}]
    named = set(insights.SELF_CORRECTION_FIELDS) | {"project_name", "developer_id"}
    assert set(app_routes.SELF_CORRECTION_READ) == named, "The count's fields, the project filter and the developer"


# ---------------------------------------------------------------- the fleet's sweep


def _evaluator_on(store: Dict[str, Dict[str, Any]]) -> GovernanceEvaluator:
    return GovernanceEvaluator(session_repo=_repo_on(store))


def test_the_sweep_reads_no_field_it_does_not_name(fleet_store, monkeypatch) -> None:
    monkeypatch.setenv(rollups.DEMO_FLEET_ENV, "true")
    evaluator = _evaluator_on(fleet_store)
    repo = evaluator.session_repo
    whole = repo.read_decision_day
    seen: set = set()
    asked: List[Any] = []

    def reader(day, after=None, limit=200, *, raise_errors=False, fields=None):
        asked.append(fields)
        # Every field of every row, so a read of any field at all is seen.
        rows, next_after = whole(day, after, limit)
        return [_Recording(row, seen) for row in rows], next_after

    monkeypatch.setattr(repo, "read_decision_day", reader)
    summary = demo_fleet.TickSummary(tick=0)
    demo_fleet._sweep(evaluator, AFTER_THE_LAST_TICK, summary, min_age=datetime.timedelta(0))
    assert asked and all(fields == demo_fleet.SWEEP_FIELDS for fields in asked)
    assert summary.labelled["correct"] > 0 and summary.labelled["false_alarm"] > 0, "The sweep had calls to label"
    assert seen - {"_sk"} <= set(demo_fleet.SWEEP_FIELDS), sorted(seen - {"_sk"} - set(demo_fleet.SWEEP_FIELDS))


def _ledger_and_rollups(repo: DynamoDBSessionRepository) -> Dict[str, Dict[str, Any]]:
    """The rows and the counters, without the expiry each write stamps from the wall clock."""
    return {
        key: {name: value for name, value in item.items() if name != "ttl"}
        for key, item in repo._memory_store.items()
        if key.startswith(("DECISION#", "STATS#"))
    }


@pytest.mark.parametrize("budget", [demo_fleet.SWEEP_ROW_BUDGET, 150])
def test_the_sweep_labels_the_same_calls_read_whole_or_read_for_its_fields(fleet_store, monkeypatch, budget) -> None:
    monkeypatch.setenv(rollups.DEMO_FLEET_ENV, "true")
    monkeypatch.setattr(demo_fleet, "SWEEP_ROW_BUDGET", budget)
    outcomes = []
    for read_whole in (False, True):
        if read_whole:
            monkeypatch.setattr(demo_fleet, "_sweep_reader", lambda reader: reader)
        evaluator = _evaluator_on(fleet_store)
        summary = demo_fleet.TickSummary(tick=0)
        demo_fleet._sweep(evaluator, AFTER_THE_LAST_TICK, summary, min_age=datetime.timedelta(0))
        outcomes.append((dict(summary.labelled), sorted(summary.labelled_rows), sorted(summary.false_alarms_in),
                         _ledger_and_rollups(evaluator.session_repo)))
    assert sum(outcomes[0][0].values()) > 0
    assert outcomes[0] == outcomes[1]
