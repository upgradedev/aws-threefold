"""The sessions listing has to read the whole table, not the first page of it.

One table holds a row per session and a row per decision, and there are far
more decisions. The listing used to scan once, for a few hundred items, and keep
the session rows among them: past a couple of hundred decisions it returned a
handful of sessions, or none, and said nothing about the rest. Nothing in the
suite noticed, because the in-process store it falls back to is small.

The fake below pages the way DynamoDB does, which is the part that matters:
Limit and the page boundary apply before the filter, so a page can come back
with no items in it and still not be the last one.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from threefold.infrastructure.dynamo_repo import MAX_SCAN_PAGES, DynamoDBSessionRepository

PAGE_SIZE = 100


class _FakeTable:
    def __init__(self, items: List[Dict[str, Any]], page_size: int = PAGE_SIZE) -> None:
        self.items = items
        self.page_size = page_size
        self.scans: List[Dict[str, Any]] = []

    def scan(self, **kwargs):
        self.scans.append(kwargs)
        start = int((kwargs.get("ExclusiveStartKey") or {}).get("offset", 0))
        stop = min(start + min(kwargs.get("Limit", self.page_size), self.page_size), len(self.items))
        page = self.items[start:stop]
        wanted = (kwargs.get("ExpressionAttributeValues") or {}).get(":metadata")
        if kwargs.get("FilterExpression"):
            assert kwargs["FilterExpression"] == "SK = :metadata", kwargs["FilterExpression"]
            page = [item for item in page if item.get("SK") == wanted]
        response: Dict[str, Any] = {"Items": page, "Count": len(page), "ScannedCount": stop - start}
        if stop < len(self.items):
            response["LastEvaluatedKey"] = {"offset": stop}
        return response

    def get_item(self, Key):  # noqa: N803 - boto3 spells it this way
        return {}


class _FakeResource:
    def __init__(self, table: _FakeTable) -> None:
        self._table = table

    def Table(self, name):  # noqa: N802 - boto3 spells it this way
        return self._table


def _mixed_rows(total: int = 600) -> List[Dict[str, Any]]:
    """A table of `total` rows where every third one is a session.

    The decision rows sit in front of and between the sessions, as they do in
    the real table, so a reader that stops at the first page sees almost none
    of them.
    """
    rows: List[Dict[str, Any]] = []
    sessions = 0
    for index in range(total):
        if index % 3 == 2:
            sessions += 1
            rows.append(
                {
                    "PK": f"SESSION#acme-session-{sessions:03d}",
                    "SK": "METADATA",
                    "developer_id": "acme-dev-paging",
                    "project_name": "Acme-Paging",
                    "total_cost_usd": "0.01",
                    "is_tripped": False,
                    "created_at": f"2026-09-21T{sessions // 60:02d}:{sessions % 60:02d}:00+00:00",
                    "history_json": json.dumps([{"tool_name": "view_file"}]),
                }
            )
        else:
            rows.append(
                {
                    "PK": "DECISION#2026-09-21",
                    "SK": f"2026-09-21T00:00:{index % 60:02d}+00:00#V-{index:04d}",
                    "project_name": "Acme-Paging",
                    "status": "APPROVED",
                }
            )
    return rows


@pytest.fixture
def table() -> _FakeTable:
    return _FakeTable(_mixed_rows())


def _repository(table: _FakeTable) -> DynamoDBSessionRepository:
    return DynamoDBSessionRepository(table_name="Acme-Paging-Table", boto3_resource=_FakeResource(table))


def test_every_session_is_listed_however_many_decisions_are_in_the_way(table: _FakeTable) -> None:
    listed = _repository(table).list_sessions(limit=250)
    assert len(listed) == 200, "Sessions past the first scan page were lost"
    assert {row["session_id"] for row in listed} == {f"acme-session-{n:03d}" for n in range(1, 201)}


def test_the_scan_asks_for_metadata_rows_and_follows_the_pages(table: _FakeTable) -> None:
    _repository(table).list_sessions(limit=250)
    assert len(table.scans) == 6, "600 rows at 100 a page is six scans"
    assert all(scan["FilterExpression"] == "SK = :metadata" for scan in table.scans)
    assert "ExclusiveStartKey" not in table.scans[0]
    assert [scan["ExclusiveStartKey"]["offset"] for scan in table.scans[1:]] == [100, 200, 300, 400, 500]


def test_a_page_with_no_sessions_in_it_does_not_end_the_scan() -> None:
    """The filter runs after the page is read, so an empty page is normal."""
    rows = [
        {"PK": "DECISION#2026-09-21", "SK": f"t#{index}", "status": "APPROVED"}
        for index in range(PAGE_SIZE * 2)
    ]
    rows.append(
        {
            "PK": "SESSION#acme-session-last",
            "SK": "METADATA",
            "developer_id": "acme-dev-paging",
            "project_name": "Acme-Paging",
            "created_at": "2026-09-21T10:00:00+00:00",
            "history_json": "[]",
        }
    )
    listed = _repository(_FakeTable(rows)).list_sessions(limit=50)
    assert [row["session_id"] for row in listed] == ["acme-session-last"]


def test_the_newest_sessions_come_first_and_the_limit_is_honoured(table: _FakeTable) -> None:
    listed = _repository(table).list_sessions(limit=5)
    assert len(listed) == 5
    assert [row["session_id"] for row in listed] == [f"acme-session-{n:03d}" for n in range(200, 195, -1)]
    assert listed[0]["calls"] == 1, "The page shows a call count, so the listing has to carry one"


def test_a_table_that_never_stops_paging_is_still_bounded() -> None:
    """A scan that followed LastEvaluatedKey forever would hang the function."""

    class _Endless(_FakeTable):
        def scan(self, **kwargs):
            response = super().scan(**kwargs)
            response["LastEvaluatedKey"] = {"offset": 0}
            return response

    endless = _Endless(_mixed_rows(total=PAGE_SIZE))
    listed = _repository(endless).list_sessions(limit=10)
    assert len(endless.scans) == MAX_SCAN_PAGES
    assert listed, "What was read is still answered, rather than nothing"


def test_a_scan_that_fails_falls_back_to_what_this_container_holds() -> None:
    """The store the repository keeps in memory is the last resort, as before."""

    class _Broken(_FakeTable):
        def scan(self, **kwargs):
            raise RuntimeError("Scan is not permitted for this role")

    repository = _repository(_Broken([]))
    repository._memory_store["SESSION#acme-session-local#METADATA"] = {
        "PK": "SESSION#acme-session-local",
        "SK": "METADATA",
        "developer_id": "acme-dev-paging",
        "project_name": "Acme-Paging",
        "created_at": "2026-09-21T09:00:00+00:00",
        "history_json": "[]",
    }
    assert [row["session_id"] for row in repository.list_sessions()] == ["acme-session-local"]
