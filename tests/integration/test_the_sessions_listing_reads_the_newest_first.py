"""The sessions listing reads the newest sessions first, however much ledger the table holds.

On the public stack the synthetic fleet (application/demo_fleet.py) writes
about three thousand ledger rows a weekday into the table the sessions live
in. A Scan reads that table in hash order, not by recency, and the listing
stops after MAX_SCAN_PAGES pages: past about ten thousand items it left out
sessions at random, the newest among them, and every anonymous request paid
for all fifty pages. Each session's summary is now also written to one
partition sorted by when the session began, and the listing reads that with
one Query.

The fake table below behaves as DynamoDB does where it matters: Scan returns
items in an order that has nothing to do with time, Limit applies before the
filter, Query returns one partition in sort-key order. The volumes are the
fleet's own plan for a week. Nothing calls AWS.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from threefold.application import demo_fleet
from threefold.domain.models import AgentSession, ToolActionType, ToolInvocation
from threefold.infrastructure.dynamo_repo import (
    MAX_SCAN_PAGES,
    SESSION_INDEX_PARTITION,
    DynamoDBSessionRepository,
)

UTC = datetime.timezone.utc
MONDAY = datetime.datetime(2026, 9, 28, tzinfo=UTC)


class _Table:
    """Scan in hash order, Query by partition in sort-key order, Put and Get by key."""

    def __init__(self, fail_index_writes: bool = False) -> None:
        self.items: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.scans = 0
        self.queries: List[Dict[str, Any]] = []
        self.fail_index_writes = fail_index_writes
        self._order: Optional[List[Tuple[str, str]]] = None

    def put_item(self, Item, **_):  # noqa: N803 - boto3 spells it this way
        if self.fail_index_writes and Item["PK"] == SESSION_INDEX_PARTITION:
            raise ConnectionError("simulated throttling")
        self.items[(Item["PK"], Item["SK"])] = dict(Item)
        self._order = None

    def get_item(self, Key, **_):  # noqa: N803
        found = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(found)} if found else {}

    def scan(self, Limit, ExclusiveStartKey=None, FilterExpression=None, ExpressionAttributeValues=None, **_):  # noqa: N803
        self.scans += 1
        if self._order is None:
            self._order = sorted(self.items, key=lambda key: hashlib.md5("|".join(key).encode()).hexdigest())
        start = self._order.index((ExclusiveStartKey["PK"], ExclusiveStartKey["SK"])) + 1 if ExclusiveStartKey else 0
        keys = self._order[start:start + Limit]
        page = [dict(self.items[key]) for key in keys]
        if FilterExpression:
            assert FilterExpression == "SK = :metadata"
            page = [item for item in page if item["SK"] == ExpressionAttributeValues[":metadata"]]
        response: Dict[str, Any] = {"Items": page}
        if start + Limit < len(self._order):
            response["LastEvaluatedKey"] = {"PK": keys[-1][0], "SK": keys[-1][1]}
        return response

    def query(self, KeyConditionExpression, ExpressionAttributeValues, ScanIndexForward=True, Limit=None,  # noqa: N803
              ExclusiveStartKey=None, **_):
        self.queries.append({"Limit": Limit, "ScanIndexForward": ScanIndexForward})
        assert KeyConditionExpression == "PK = :pk"
        partition = ExpressionAttributeValues[":pk"]
        keys = sorted((key for key in self.items if key[0] == partition), key=lambda key: key[1],
                      reverse=not ScanIndexForward)
        if ExclusiveStartKey:
            keys = keys[keys.index((ExclusiveStartKey["PK"], ExclusiveStartKey["SK"])) + 1:]
        page = keys[:Limit] if Limit else keys
        response: Dict[str, Any] = {"Items": [dict(self.items[key]) for key in page]}
        if Limit and len(keys) > Limit:
            response["LastEvaluatedKey"] = {"PK": page[-1][0], "SK": page[-1][1]}
        return response


class _Resource:
    def __init__(self, table: _Table) -> None:
        self.table = table

    def Table(self, _name):  # noqa: N802 - boto3 spells it this way
        return self.table


def _repository(table: _Table) -> DynamoDBSessionRepository:
    return DynamoDBSessionRepository(table_name="acme-fleet-table", boto3_resource=_Resource(table))


def _session(session_id: str, created_at: str, project: str = "Acme-Payments", calls: int = 1) -> AgentSession:
    history = [
        ToolInvocation(tool_name="Read", action_type=ToolActionType.FILE_READ,
                       arguments={"file_path": f"src/acme/module_{n}.py"}, timestamp=created_at)
        for n in range(calls)
    ]
    return AgentSession(session_id=session_id, developer_id="0a1b2c3d4e5f", project_name=project,
                        budget_usd=10.0, history=history, created_at=created_at)


def _a_fleet_week(table: _Table) -> List[Tuple[str, str]]:
    """Seven days of the fleet's planned volume: ledger rows put straight in, sessions through the repository.

    Returns every session as (created_at, session id).
    """
    repository = _repository(table)
    created: Dict[str, str] = {}
    first = demo_fleet.bucket_of(MONDAY)
    for bucket in range(first, first + 7 * 96):
        at = datetime.datetime.fromtimestamp(bucket * demo_fleet.TICK_SECONDS, tz=UTC)
        plan = demo_fleet.plan_tick(bucket)
        for index, call in enumerate(plan.calls[: plan.most]):
            stamp = (at + datetime.timedelta(seconds=index)).isoformat()
            table.items[(f"DECISION#{at.date()}", f"{stamp}#{bucket}-{index}")] = {
                "PK": f"DECISION#{at.date()}", "SK": f"{stamp}#{bucket}-{index}", "project_name": call.project,
            }
            if call.session_id not in created:
                created[call.session_id] = stamp
                repository.save_session(_session(call.session_id, stamp, call.project))
    table._order = None
    return sorted(((stamp, session_id) for session_id, stamp in created.items()), reverse=True)


@pytest.fixture(scope="module")
def fleet_week() -> Tuple[_Table, List[Tuple[str, str]]]:
    table = _Table()
    return table, _a_fleet_week(table)


@pytest.mark.parametrize("limit", [50, 200])
def test_the_newest_sessions_are_listed_after_a_week_of_the_fleet(fleet_week, limit: int) -> None:
    table, sessions = fleet_week
    assert len(table.items) > 20_000, "a week of the fleet is past what fifty scan pages hold"
    scans_before = table.scans
    listed = _repository(table).list_sessions(limit=limit)
    assert [row["session_id"] for row in listed] == [session_id for _, session_id in sessions[:limit]]
    assert table.scans == scans_before, "A full index answers the listing without scanning the table"


def test_the_scan_alone_would_have_lost_the_newest_of_them(fleet_week) -> None:
    """What the listing did before the index: the reason this file exists, measured on the same table."""
    table, sessions = fleet_week
    repository = _repository(table)
    scans_before = table.scans
    scanned = {str(item["PK"])[len("SESSION#"):] for item in repository._scan_session_metadata(50)}
    newest = {session_id for _, session_id in sessions[:50]}
    assert table.scans - scans_before == MAX_SCAN_PAGES
    assert len(newest - scanned) > 10, "the scan found almost all the newest sessions; the fixture is too small"


def test_one_query_of_one_page_answers_a_default_listing(fleet_week) -> None:
    table, _ = fleet_week
    before = len(table.queries)
    _repository(table).list_sessions(limit=50)
    assert table.queries[before:] == [{"Limit": 50, "ScanIndexForward": False}]


def test_sessions_written_before_the_index_are_still_listed() -> None:
    """While the index holds fewer sessions than asked for, the scan fills in, and nothing is listed twice."""
    table = _Table()
    for n in range(5):
        stamp = (MONDAY + datetime.timedelta(minutes=n)).isoformat()
        table.items[(f"SESSION#acme-legacy-{n}", "METADATA")] = {
            "PK": f"SESSION#acme-legacy-{n}", "SK": "METADATA", "project_name": "Acme-Payments",
            "created_at": stamp, "history_json": json.dumps([{"tool_name": "Read"}]),
        }
    repository = _repository(table)
    for n in range(3):
        repository.save_session(_session(f"acme-new-{n}", (MONDAY + datetime.timedelta(hours=1, minutes=n)).isoformat()))
    listed = repository.list_sessions(limit=10)
    assert [row["session_id"] for row in listed] == [
        "acme-new-2", "acme-new-1", "acme-new-0", "acme-legacy-4", "acme-legacy-3", "acme-legacy-2",
        "acme-legacy-1", "acme-legacy-0",
    ]
    assert table.scans > 0


def test_the_index_entry_follows_its_session() -> None:
    """One entry per session, rewritten on each save, carrying what the listing shows and the session's ttl."""
    table = _Table()
    repository = _repository(table)
    session = _session("fleet-payments-codex-1", MONDAY.isoformat(), calls=2)
    repository.save_session(session)
    session.history.extend(_session("x", MONDAY.isoformat(), calls=3).history)
    session.is_tripped, session.trip_reason = True, "Loop detected"
    repository.save_session(session, force=True)
    entries = [item for key, item in table.items.items() if key[0] == SESSION_INDEX_PARTITION]
    assert len(entries) == 1
    (entry,) = entries
    assert entry["SK"] == f"{MONDAY.isoformat()}#fleet-payments-codex-1"
    assert entry["ttl"] == table.items[("SESSION#fleet-payments-codex-1", "METADATA")]["ttl"]
    assert "history_json" not in entry and "verdicts_json" not in entry, "the entry holds the summary, not the calls"
    (row,) = repository.list_sessions(limit=1)
    assert (row["calls"], row["is_tripped"], row["trip_reason"]) == (5, True, "Loop detected")


def test_an_index_write_that_fails_never_fails_the_session_s_own() -> None:
    table = _Table(fail_index_writes=True)
    repository = _repository(table)
    assert repository.save_session(_session("acme-unindexed", MONDAY.isoformat())) is True
    assert ("SESSION#acme-unindexed", "METADATA") in table.items
    assert [row["session_id"] for row in repository.list_sessions(limit=5)] == ["acme-unindexed"], (
        "The scan still finds it"
    )
