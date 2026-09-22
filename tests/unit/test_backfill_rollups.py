"""The rollup backfill counts every row written before the rollups, once, and nothing else.

Driven against a fake table that honours the one conditional update the script
relies on, because the property that matters is exactly-once: a row the new
code recorded was counted when it was written, and a second run of the script
must add nothing.
"""
from __future__ import annotations

import datetime
import importlib.util
import io
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_rollups.py"
spec = importlib.util.spec_from_file_location("backfill_rollups", SCRIPT)
backfill_rollups = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill_rollups)

TODAY = datetime.date(2026, 9, 22)


class ConditionalCheckFailedException(Exception):
    pass


class FakeTable:
    def __init__(self, items):
        self.items = {(item["PK"], item["SK"]): dict(item) for item in items}
        self.writes = 0

    def query(self, KeyConditionExpression, ExpressionAttributeValues, ExclusiveStartKey=None):
        wanted = ExpressionAttributeValues[":pk"]
        rows = sorted((item for (pk, _), item in self.items.items() if pk == wanted), key=lambda item: item["SK"])
        # One row per page, so the paging loop is exercised.
        start = 0
        if ExclusiveStartKey:
            start = [row["SK"] for row in rows].index(ExclusiveStartKey["SK"]) + 1
        page = rows[start:start + 1]
        answer = {"Items": [dict(row) for row in page]}
        if start + 1 < len(rows):
            answer["LastEvaluatedKey"] = {"PK": wanted, "SK": page[-1]["SK"]}
        return answer

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues):
        item = self.items[(Key["PK"], Key["SK"])]
        if "rolled_up" in item or "rule_key" in item:
            raise ConditionalCheckFailedException("ConditionalCheckFailed")
        item["rolled_up"] = True
        self.writes += 1


class FakeRepo:
    def __init__(self, table):
        self._table = table
        self.rollups = {}

    def adjust_rollup(self, day, project, counters, stamps=None):
        stored = self.rollups.setdefault((day, project), {})
        for name, value in counters.items():
            stored[name] = stored.get(name, 0) + value
        stored.update(stamps or {})


def _row(sk, **fields):
    return dict({"PK": "DECISION#2026-09-22", "SK": sk, "timestamp": "2026-09-22T08:00:00+00:00",
                 "project_name": "Acme-Ledger", "agent": "claude-code", "origin": "hook"}, **fields)


def _table():
    return FakeTable([
        _row("a", status="APPROVED", dry_run=True),
        _row("b", status="APPROVED", dry_run=True, observed_rules=["python-domain-stays-pure"],
             observed_reason="Clean Architecture violation: Layering rule 'python-domain-stays-pure' refuses this write"),
        _row("c", status="BLOCKED_BOUNDARY_VIOLATION", reason="Command 'cat .env' reaches a protected path or credential store"),
        # Recorded by the new code, so already in its rollup.
        _row("d", status="APPROVED", rule_key="NONE", stage="observe", hook_mode="managed"),
    ])


def test_rows_from_before_the_rollups_are_added_once_with_the_recording_codes_counters() -> None:
    repo = FakeRepo(_table())
    totals = backfill_rollups.backfill(repo, 1, TODAY, dry_run=False, out=io.StringIO())
    assert totals == {"rows": 4, "already_counted": 1, "claimed": 3, "added": 3}
    counted = repo.rollups[("2026-09-22", "Acme-Ledger")]
    assert (counted["calls"], counted["approved"], counted["observed"], counted["refused"]) == (3, 1, 1, 1)
    assert counted["observed:python-domain-stays-pure"] == 1
    assert counted["refused:PROTECTED_PATH"] == 1
    # An old dry run was judged in observe; an old enforced call in enforce.
    assert counted["stage:observe"] == 2 and counted["stage:enforce"] == 1


def test_a_second_run_adds_nothing() -> None:
    table = _table()
    repo = FakeRepo(table)
    backfill_rollups.backfill(repo, 1, TODAY, dry_run=False, out=io.StringIO())
    before = {key: dict(value) for key, value in repo.rollups.items()}
    totals = backfill_rollups.backfill(repo, 1, TODAY, dry_run=False, out=io.StringIO())
    assert totals["added"] == 0 and totals["already_counted"] == 4
    assert repo.rollups == before


def test_a_dry_run_writes_nothing_and_says_what_it_would_add() -> None:
    table = _table()
    repo = FakeRepo(table)
    out = io.StringIO()
    totals = backfill_rollups.backfill(repo, 1, TODAY, dry_run=True, out=out)
    assert totals["added"] == 0 and table.writes == 0 and not repo.rollups
    assert "would add 3 row(s)" in out.getvalue()


def test_a_row_from_before_the_labels_is_filed_under_the_label_a_read_asks_for() -> None:
    """The rollup's sort key is the label, because that is what every read names.

    A row written before labelling existed keeps whatever project name its
    caller sent. Filed under that name, its counts sat in an item nothing
    reads: the unfiltered overview relabels every rollup, so the call showed up
    under 'unlabelled' there, while a read filtered to 'unlabelled' fetches
    that one item by name and never found it. The project then read fewer calls
    filtered than unfiltered, and fewer than the ledger listed.
    """
    table = FakeTable([
        _row("legacy", status="APPROVED", project_name="a-legacy-repository-name"),
        _row("labelled", status="APPROVED", project_name="Acme-Ledger"),
    ])
    repo = FakeRepo(table)
    backfill_rollups.backfill(repo, 1, TODAY, dry_run=False, out=io.StringIO())

    assert sorted(project for _, project in repo.rollups) == ["Acme-Ledger", "unlabelled"]
    assert repo.rollups[("2026-09-22", "unlabelled")]["calls"] == 1
    assert ("2026-09-22", "a-legacy-repository-name") not in repo.rollups


def test_a_row_with_no_project_at_all_is_filed_under_unlabelled_too() -> None:
    table = FakeTable([_row("empty", status="APPROVED", project_name="")])
    repo = FakeRepo(table)
    backfill_rollups.backfill(repo, 1, TODAY, dry_run=False, out=io.StringIO())
    assert list(repo.rollups) == [("2026-09-22", "unlabelled")]


def test_the_backfill_labels_by_the_pattern_the_stack_deploys_with(monkeypatch) -> None:
    """A stack with its own AllowedProjectPattern is backfilled with that pattern."""
    monkeypatch.setenv("ALLOWED_PROJECT_PATTERN", r"^Acme-[A-Za-z0-9-]{1,40}$")
    repo = FakeRepo(FakeTable([_row("other", status="APPROVED", project_name="Widget-Co")]))
    backfill_rollups.backfill(repo, 1, TODAY, dry_run=False, out=io.StringIO())
    assert list(repo.rollups) == [("2026-09-22", "unlabelled")]

    monkeypatch.setenv("ALLOWED_PROJECT_PATTERN", r"^(Acme|Widget)-[A-Za-z0-9-]{1,40}$")
    other = FakeRepo(FakeTable([_row("other", status="APPROVED", project_name="Widget-Co")]))
    backfill_rollups.backfill(other, 1, TODAY, dry_run=False, out=io.StringIO())
    assert list(other.rollups) == [("2026-09-22", "Widget-Co")]


def test_a_backfilled_day_files_a_call_where_the_recording_path_files_it() -> None:
    """The script's own promise: a backfilled day and a recorded day cannot disagree."""
    from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository

    recorded = DynamoDBSessionRepository(table_name="acme-backfill-parity")
    assert recorded._table is None, "offline, so the rollup is counted in memory"
    recorded.record_decision({
        "timestamp": "2026-09-22T08:00:00+00:00",
        "verdict_id": "V-parity",
        "status": "APPROVED",
        # Labelled on the way in by the DTO, so the store is given the label.
        "project_name": "unlabelled",
        "agent": "claude-code",
        "origin": "hook",
        "stage": "enforce",
        "hook_mode": "managed",
        "rule_key": "NONE",
    })
    filed_by_recording = {
        item["SK"] for key, item in recorded._memory_store.items() if key.startswith("STATS#2026-09-22")
    }

    repo = FakeRepo(FakeTable([_row("legacy", status="APPROVED", project_name="a-legacy-repository-name")]))
    backfill_rollups.backfill(repo, 1, TODAY, dry_run=False, out=io.StringIO())
    filed_by_backfill = {project for _, project in repo.rollups}

    assert filed_by_backfill == filed_by_recording == {"unlabelled"}
