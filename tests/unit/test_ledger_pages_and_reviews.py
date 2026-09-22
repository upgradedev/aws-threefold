"""The ledger read a page at a time, labels stored on its rows, and the sandbox's seed.

The cursor must resume exactly after the last row a request returned, across
day partitions, whether the request stopped because it had enough rows or
because it ran out of pages to read. A label is written only onto a row of the
project named, conditionally, so a label cannot land on another team's call.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import datetime
import hashlib

import pytest

from threefold.application import ledger
from threefold.application import projects as stages
from threefold.application.dtos import InvalidRequestError
from threefold.application.evaluator import GovernanceEvaluator
from threefold.application.sandbox import SEEDED_CALLS, create_sandbox, seeded_requests
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository

TODAY = datetime.date(2026, 9, 22)


def _rows(day: str, count: int, **fields):
    return [
        dict({"verdict_id": f"{day}-{index}", "timestamp": f"{day}T10:00:{59 - index:02d}+00:00",
              "project_name": "Acme-Pages", "status": "APPROVED", "rule_key": "NONE",
              "_sk": f"{day}T10:00:{59 - index:02d}+00:00#{day}-{index}"}, **fields)
        for index in range(count)
    ]


class _Reader:
    """A ledger of fixed rows, paged the way read_decision_day pages."""

    def __init__(self, by_day):
        self.by_day = by_day
        self.reads = 0

    def __call__(self, day, after, limit):
        self.reads += 1
        rows = [row for row in self.by_day.get(day, []) if after is None or row["_sk"] < after]
        page = rows[:limit]
        return page, (page[-1]["_sk"] if len(rows) > len(page) else None)


def _walk(reader, days, filters, limit):
    seen, cursor = [], None
    for _ in range(50):
        body = ledger.page_decisions(reader, days, filters, limit, cursor, today=TODAY)
        seen.extend(row["verdict_id"] for row in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError("The cursor never ran out")


def test_pages_cover_the_window_once_each_newest_first(monkeypatch) -> None:
    monkeypatch.setattr(ledger, "PAGE_SIZE", 4)
    reader = _Reader({"2026-09-22": _rows("2026-09-22", 5), "2026-09-20": _rows("2026-09-20", 3)})
    seen = _walk(reader, 3, ledger.DecisionFilters(), limit=2)
    assert seen == [f"2026-09-22-{i}" for i in range(5)] + [f"2026-09-20-{i}" for i in range(3)]


def test_a_filter_that_skips_most_rows_still_returns_each_match_once(monkeypatch) -> None:
    monkeypatch.setattr(ledger, "PAGE_SIZE", 3)
    monkeypatch.setattr(ledger, "MAX_PAGES_PER_REQUEST", 2)
    rows = _rows("2026-09-22", 9)
    for index in (1, 4, 8):
        rows[index]["status"] = "BLOCKED_LOOP_DETECTED"
        rows[index]["rule_key"] = "LOOP"
    seen = _walk(_Reader({"2026-09-22": rows}), 1, ledger.DecisionFilters(kind="refused"), limit=1)
    assert seen == ["2026-09-22-1", "2026-09-22-4", "2026-09-22-8"]


def test_a_request_reads_a_bounded_number_of_pages(monkeypatch) -> None:
    monkeypatch.setattr(ledger, "PAGE_SIZE", 1)
    monkeypatch.setattr(ledger, "MAX_PAGES_PER_REQUEST", 3)
    reader = _Reader({"2026-09-22": _rows("2026-09-22", 10)})
    body = ledger.page_decisions(reader, 1, ledger.DecisionFilters(project="Acme-Elsewhere"), 5, today=TODAY)
    assert body["items"] == [] and body["next_cursor"] and reader.reads == 3


def test_the_last_page_of_the_window_has_no_cursor() -> None:
    body = ledger.page_decisions(_Reader({}), 1, ledger.DecisionFilters(), 5, today=TODAY)
    assert body == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    "cursor",
    ["", "!!!", ledger.encode_cursor("yesterday", None, "2026-09-20"),
     ledger.encode_cursor("2026-09-20", None, "2026-09-22"), "eyJ2IjoyfQ"],
)
def test_a_cursor_this_service_did_not_issue_is_refused(cursor) -> None:
    with pytest.raises(InvalidRequestError):
        ledger.decode_cursor(cursor)


def test_a_cursor_round_trips() -> None:
    cursor = ledger.encode_cursor("2026-09-22", "2026-09-22T10:00:00+00:00#V-1", "2026-09-16")
    assert ledger.decode_cursor(cursor) == ("2026-09-22", "2026-09-22T10:00:00+00:00#V-1", "2026-09-16")


# ---------------------------------------------------------------- reviews


def test_the_reviewer_is_a_hash_of_whichever_header_carried_the_credential() -> None:
    expected = hashlib.sha256(b"k-1").hexdigest()[:8]
    assert ledger.credential_hash({"X-API-Key": "k-1"}) == expected
    assert ledger.credential_hash({"authorization": "Bearer k-1"}) == expected
    assert ledger.credential_hash({}) == "anonymous"
    assert ledger.credential_hash({"Authorization": "Basic k-1"}) == "anonymous"


def test_review_items_are_read_one_by_one() -> None:
    usable, skipped = ledger.read_review_items({"items": [
        {"timestamp": "2026-09-22T10:00:00+00:00", "verdict_id": "V-1", "label": "correct", "note": "  fine  "},
        {"timestamp": "yesterday", "verdict_id": "V-2", "label": "correct"},
        {"timestamp": "2026-09-22T10:00:00+00:00", "verdict_id": "V-3", "label": "clear"},
    ]})
    assert [(item.verdict_id, item.label, item.note) for item in usable] == [("V-1", "correct", "fine"), ("V-3", "clear", "")]
    assert skipped == [{"verdict_id": "V-2", "reason": "timestamp-missing"}]


class _Table:
    def __init__(self, fail_condition=False):
        self.calls = []
        self.fail_condition = fail_condition

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_condition:
            error = type("ConditionalCheckFailedException", (Exception,), {})
            raise error("condition failed")
        return {"Attributes": {"verdict_id": "V-1", "timestamp": "2026-09-22T10:00:00+00:00",
                               "project_name": "Acme-Pages", "status": "APPROVED", "review": "correct"}}

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return {"Items": [{"PK": "DECISION#2026-09-22", "SK": "2026-09-22T10:00:00+00:00#V-1", "verdict_id": "V-1",
                           "timestamp": "2026-09-22T10:00:00+00:00"}],
                "LastEvaluatedKey": {"PK": "DECISION#2026-09-22", "SK": "2026-09-22T10:00:00+00:00#V-1"}}


class _Resource:
    def __init__(self, table):
        self.table = table

    def Table(self, _name):  # noqa: N802 - boto3 spells it this way
        return self.table


def test_a_live_label_is_conditional_on_the_project_and_returns_what_it_replaced() -> None:
    table = _Table()
    repo = DynamoDBSessionRepository(boto3_resource=_Resource(table))
    before = repo.label_decision("2026-09-22T10:00:00+00:00", "V-1", "Acme-Pages", "false_alarm",
                                 note="n", reviewed_by="a1b2c3d4", reviewed_at="2026-09-22T11:00:00+00:00")
    assert before["review"] == "correct"
    call = table.calls[0]
    assert call["Key"] == {"PK": "DECISION#2026-09-22", "SK": "2026-09-22T10:00:00+00:00#V-1"}
    assert call["ConditionExpression"] == "attribute_exists(PK) AND #project = :project"
    assert call["ExpressionAttributeValues"][":project"] == "Acme-Pages"
    assert call["ReturnValues"] == "ALL_OLD"


def test_a_live_label_on_another_projects_row_changes_nothing() -> None:
    repo = DynamoDBSessionRepository(boto3_resource=_Resource(_Table(fail_condition=True)))
    assert repo.label_decision("2026-09-22T10:00:00+00:00", "V-1", "Acme-Other", None) is None


def test_a_cleared_label_is_removed_from_the_row() -> None:
    table = _Table()
    DynamoDBSessionRepository(boto3_resource=_Resource(table)).label_decision(
        "2026-09-22T10:00:00+00:00", "V-1", "Acme-Pages", None)
    assert table.calls[0]["UpdateExpression"] == "REMOVE review, reviewed_at, review_note, reviewed_by"


def test_a_live_day_page_resumes_after_the_key_it_was_given() -> None:
    table = _Table()
    repo = DynamoDBSessionRepository(boto3_resource=_Resource(table))
    rows, after = repo.read_decision_day("2026-09-22", after="2026-09-22T11:00:00+00:00#V-9", limit=1)
    query = table.calls[0]
    assert query["ExclusiveStartKey"] == {"PK": "DECISION#2026-09-22", "SK": "2026-09-22T11:00:00+00:00#V-9"}
    assert query["ScanIndexForward"] is False and query["Limit"] == 1
    assert rows[0]["_sk"] == "2026-09-22T10:00:00+00:00#V-1" and after == "2026-09-22T10:00:00+00:00#V-1"


# ---------------------------------------------------------------- the sandbox's seed


def test_the_seed_is_a_dozen_hook_calls_from_all_three_agents() -> None:
    requests = seeded_requests("Acme-Sandbox-0a1b2c3d")
    assert len(requests) == len(SEEDED_CALLS) == 12
    assert {request.agent for request in requests} == {"claude-code", "codex", "antigravity"}
    assert {(request.origin, request.dry_run, request.explain, request.hook_mode) for request in requests} == {
        ("hook", False, False, "managed")
    }
    assert all(len(request.developer_id) == 12 for request in requests), "A 12-hex stand-in, never a name"


def test_a_sandbox_observes_whatever_the_stacks_default(monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_HOOK_STAGE", "enforce")
    evaluator = GovernanceEvaluator(session_repo=DynamoDBSessionRepository())
    created = create_sandbox(evaluator)
    config = evaluator.project_config(created["project"], fresh=True)
    assert config["stage"] == stages.OBSERVE and config["sandbox"] is True
    rows = evaluator.list_decisions(days=1)
    assert len(rows) == 12 and all(row["status"] == "APPROVED" for row in rows)
    keys = {row["rule_key"] for row in rows} - {"NONE"}
    assert keys == {"python-domain-stays-pure", "java-domain-stays-pure", "web-domain-stays-pure", "PROTECTED_PATH"}
