"""The overview, a project page and a fleet tick, counted against a table that answers as DynamoDB does.

On the deployed function every Query and GetItem is a round trip, and every
attribute of every item it returns is parsed at a fraction of a CPU, so what a
request costs is how many of them it makes and how much each sends back. The
overview reads the ledger for its self-correction figure; the pins here are
that each of those reads asks for the figure's fields alone, that the number
of reads stays within what the constants allow, and that the payload is the
same, number for number, as when every field of every row was read. The fleet's
tick is counted through its repository the same way.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import copy
import datetime
import functools
import inspect
from decimal import Decimal
from typing import Any, Dict, List

import pytest

import threefold.domain.models as models
from test_app_support import get
from threefold.application import demo_fleet, dtos, ledger, rollups
from threefold.application import projects as stages
from threefold.application.evaluator import GovernanceEvaluator
from threefold.infrastructure import dynamo_repo
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository
from threefold.interfaces import api_handlers, app_routes

UTC = datetime.timezone.utc
TICKS = 12
DAYS = 7


class _Clock:
    """The service's clock, moved by the test: every reading is 20 ms after the last."""

    def __init__(self, start: datetime.datetime) -> None:
        self.now = start
        clock = self

        class _Moving(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                clock.now += datetime.timedelta(milliseconds=20)
                return clock.now if tz is not None else clock.now.replace(tzinfo=None)

        self.datetime = _Moving


@pytest.fixture(scope="module")
def fleet_items() -> List[Dict[str, Any]]:
    """Every item three hours of the fleet leave in a table, ending a minute ago, as the reads' windows need."""
    end = datetime.datetime.now(UTC).replace(microsecond=0) - datetime.timedelta(minutes=1)
    start = end - datetime.timedelta(minutes=15 * (TICKS - 1))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(rollups.DEMO_FLEET_ENV, "true")
        clock = _Clock(start)
        for module in (dtos, models, dynamo_repo):
            patch.setattr(module, "datetime", clock.datetime)
        evaluator = GovernanceEvaluator(session_repo=DynamoDBSessionRepository())
        # Two projects enforce from the start, so there are refusals for the
        # self-correction figure to count.
        demo_fleet._ensure_configured(evaluator, start)
        for name in ("Acme-Payments", "Acme-Treasury"):
            keys = demo_fleet._keys(evaluator, name)
            evaluator.save_project_config(name, stages.promoted(None, start.isoformat(), "fleet", keys, keys))
        for index in range(TICKS):
            moment = start + datetime.timedelta(minutes=15 * index)
            clock.now = moment
            demo_fleet.run_tick(evaluator, now=moment)
        return copy.deepcopy(list(evaluator.session_repo._memory_store.values()))


def _as_stored(value: Any) -> Any:
    """A value as the resource API hands it back: every number a Decimal."""
    if isinstance(value, bool) or not isinstance(value, (int, float, dict, list)):
        return value
    if isinstance(value, dict):
        return {name: _as_stored(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_as_stored(item) for item in value]
    return Decimal(str(value))


class _LiveTable:
    """GetItem and Query as DynamoDB answers them, each one counted, and strict about expression names."""

    def __init__(self, items: List[Dict[str, Any]]) -> None:
        self.items = {(item["PK"], item["SK"]): _as_stored(item) for item in items}
        self.calls: List[tuple] = []

    def get_item(self, Key, **kwargs):  # noqa: N803 - boto3 spells it this way
        self.calls.append(("GetItem", dict(kwargs, Key=Key), 0))
        found = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": copy.deepcopy(found)} if found else {}

    def query(self, **kwargs):
        partition = kwargs["ExpressionAttributeValues"][":pk"]
        newest_first = kwargs.get("ScanIndexForward") is False
        keys = sorted((sk for pk, sk in self.items if pk == partition), reverse=newest_first)
        start = (kwargs.get("ExclusiveStartKey") or {}).get("SK")
        if start is not None:
            keys = [sk for sk in keys if (sk < start if newest_first else sk > start)]
        limit = kwargs.get("Limit")
        page = keys[:limit] if limit else keys
        names = kwargs.get("ExpressionAttributeNames") or {}
        projection = kwargs.get("ProjectionExpression")
        used = [part.strip() for part in projection.split(",")] if projection else []
        if set(names) - set(used):
            raise ValueError("ValidationException: Value provided in ExpressionAttributeNames unused in expressions")
        items = [copy.deepcopy(self.items[(partition, sk)]) for sk in page]
        if projection:
            wanted = [names.get(part, part) for part in used]
            items = [{name: item[name] for name in wanted if name in item} for item in items]
        self.calls.append(("Query", kwargs, sum(len(item) for item in items)))
        response: Dict[str, Any] = {"Items": items}
        # DynamoDB stops at the limit and says where, whether or not anything is left.
        if limit and len(page) == limit:
            response["LastEvaluatedKey"] = {"PK": partition, "SK": page[-1]}
        return response

    def ledger_queries(self) -> List[tuple]:
        return [call for call in self.calls if call[0] == "Query" and
                str(call[1]["ExpressionAttributeValues"][":pk"]).startswith("DECISION#")]


class _Resource:
    def __init__(self, table) -> None:
        self.table = table

    def Table(self, _name):  # noqa: N802 - boto3 spells it this way
        return self.table


@pytest.fixture
def table(fleet_items, monkeypatch) -> _LiveTable:
    """The fleet's items in a live-shaped table, behind the router, counted from the first request on."""
    monkeypatch.setenv(rollups.DEMO_FLEET_ENV, "true")
    live = _LiveTable(fleet_items)
    evaluator = GovernanceEvaluator(session_repo=DynamoDBSessionRepository(boto3_resource=_Resource(live)))
    monkeypatch.setattr(api_handlers, "_evaluator", evaluator)
    live.calls.clear()
    return live


def _whole_rows(monkeypatch) -> None:
    """The figure's reader as it was: asked to raise on a failure, and sent every field of every row."""
    def reader(read):
        accepts = "raise_errors" in inspect.signature(read).parameters
        return functools.partial(read, raise_errors=True) if accepts else read

    monkeypatch.setattr(app_routes, "_self_correction_reader", reader)


def _without_the_clock(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {name: value for name, value in payload.items() if name != "generated_at"}


def test_the_overview_reads_the_ledger_for_the_figure_s_fields_alone(table) -> None:
    payload = get("/api/overview", days=DAYS)
    figure = payload["self_correction"]
    assert figure["rows_read"] > 0 and figure["complete"] is True
    ledger_reads = table.ledger_queries()
    wanted = {"SK", *app_routes.SELF_CORRECTION_READ}
    for _, query, attributes in ledger_reads:
        names = query["ExpressionAttributeNames"]
        assert set(names.values()) == wanted, "Each page of the ledger asks for the figure's fields and its sort key"
        assert query["Limit"] <= ledger.PAGE_SIZE
    assert sum(attributes for _, _, attributes in ledger_reads) <= figure["rows_read"] * len(wanted)


def test_the_overview_makes_no_more_round_trips_than_its_constants_allow(table) -> None:
    get("/api/overview", days=DAYS)
    rollup_reads = DAYS
    config_reads = 1
    ledger_reads = DAYS + ledger.SELF_CORRECTION_ROWS // ledger.PAGE_SIZE + 1
    assert len(table.calls) <= rollup_reads + config_reads + ledger_reads, [call[0] for call in table.calls]
    assert all(kind == "Query" for kind, _, _ in table.calls), "No read of the overview is one item at a time"


def test_the_overview_is_the_same_as_when_every_field_was_read(table, monkeypatch) -> None:
    for days in (1, DAYS, 30):
        for query in ({}, {"project": "Acme-Payments"}, {"project": "Acme-Nobody"}):
            projected = _without_the_clock(get("/api/overview", days=days, **query))
            with monkeypatch.context() as patch:
                _whole_rows(patch)
                whole = _without_the_clock(get("/api/overview", days=days, **query))
            assert projected == whole, (days, query)


def test_a_project_page_is_the_same_as_when_every_field_was_read(table, monkeypatch) -> None:
    for name in rollups.FLEET_PROJECTS:
        projected = get(f"/api/projects/{name}")
        with monkeypatch.context() as patch:
            _whole_rows(patch)
            whole = get(f"/api/projects/{name}")
        assert projected == whole, name
    assert all(set(query["ExpressionAttributeNames"].values()) == {"SK", *app_routes.SELF_CORRECTION_READ}
               for _, query, _ in table.ledger_queries() if "ProjectionExpression" in query)


# ---------------------------------------------------------------- the fleet's tick


class _Counted:
    """A repository that records every call made to it, and is otherwise the one it wraps."""

    def __init__(self, repo: DynamoDBSessionRepository) -> None:
        self._repo = repo
        self.calls: List[tuple] = []

    def __getattr__(self, name: str) -> Any:
        found = getattr(self._repo, name)
        if not callable(found):
            return found

        @functools.wraps(found)
        def counted(*args, **kwargs):
            result = found(*args, **kwargs)
            self.calls.append((name, args, kwargs, result))
            return result

        return counted


def test_a_tick_reads_the_ledger_for_the_sweep_s_fields_alone_and_within_its_budget(fleet_items, monkeypatch) -> None:
    monkeypatch.setenv(rollups.DEMO_FLEET_ENV, "true")
    repo = DynamoDBSessionRepository()
    repo._memory_store = {f"{item['PK']}#{item['SK']}": copy.deepcopy(item) for item in fleet_items}
    counted = _Counted(repo)
    evaluator = GovernanceEvaluator(session_repo=counted)
    summary = demo_fleet.run_tick(evaluator, now=datetime.datetime.now(UTC))
    assert summary["calls"] > 0
    reads = [call for call in counted.calls if call[0] == "read_decision_day"]
    assert reads, "The sweep read the ledger"
    assert all(kwargs.get("fields") == demo_fleet.SWEEP_FIELDS for _, _, kwargs, _ in reads)
    assert sum(len(rows) for _, _, _, (rows, _) in reads) <= demo_fleet.SWEEP_ROW_BUDGET
    assert all(set(row) <= {"_sk", *demo_fleet.SWEEP_FIELDS} for _, _, _, (rows, _) in reads for row in rows)
