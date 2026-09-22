#!/usr/bin/env python3
"""Counts the ledger rows written before the daily rollups existed into those rollups.

    backfill_rollups.py --stack threefold-dogfood [--region eu-west-1] [--days 30] [--dry-run]
    backfill_rollups.py --table <table name> ...

The dashboard's tiles and charts read `STATS#<day>` items that every decision
adds to as it is recorded. Rows recorded before that code was deployed were
never added, so a stack that was already in use shows its history as empty.
This walks the ledger partitions of the window and adds each such row exactly
once, with the same counters the recording code computes, so a backfilled day
and a recorded day cannot disagree about what a call counts as.

Exactly once is enforced on the row itself. A row is claimed by a conditional
update that sets `rolled_up` only where neither `rolled_up` nor `rule_key` is
present, and it is added to its rollup only after that claim succeeds: a row
recorded by the new code carries `rule_key` and was counted when it was
written, and a row claimed by an earlier run of this script is skipped. A run
interrupted between the claim and the add under-counts that one row rather
than counting any row twice, and says how many it claimed.

Each row is filed under the label the recording path would file it under, not
under the name stored on it: a row written before labelling existed keeps
whatever its caller sent, and a rollup under that name is one no read ever asks
for. The label comes from `ALLOWED_PROJECT_PATTERN`, the same variable the
function reads, so a stack deployed with its own `AllowedProjectPattern` is
backfilled with that pattern set in the environment; the run prints the pattern
it used.

Reads the stack's table name from CloudFormation when given `--stack`. Uses the
caller's AWS credentials; prints counts only, never row contents.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from threefold.application.labels import allowed_project_pattern, project_label  # noqa: E402
from threefold.application.rule_keys import rule_key  # noqa: E402
from threefold.infrastructure.dynamo_repo import (  # noqa: E402
    DECISION_PARTITION,
    DynamoDBSessionRepository,
    rollup_counters,
)


def table_of(stack: str, region: str) -> str:
    """The physical name of the stack's table, from its resources."""
    out = subprocess.run(
        [
            "aws", "cloudformation", "describe-stack-resource", "--stack-name", stack,
            "--logical-resource-id", "ThreefoldTable", "--region", region,
            "--query", "StackResourceDetail.PhysicalResourceId", "--output", "text",
        ],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def needs_backfill(item: Dict[str, Any]) -> bool:
    """A ledger row the recording code never added to a rollup."""
    return not item.get("rule_key") and not item.get("rolled_up")


def as_decision(item: Dict[str, Any]) -> Dict[str, Any]:
    """The fields the recording code counts, for a row written before them.

    The stage a row was judged under is what the read path says of old rows:
    observe for a dry run, enforce otherwise. The key is derived by the same
    function that gives an old row its key on the way out to a reader.

    The project is labelled by the same function the recording path labels it
    with, because the label is what the rollup is filed under. A row written
    before labelling existed keeps whatever name its caller sent, and filed
    under that name its counts sat in a rollup no read ever asks for: the
    unfiltered overview relabels every rollup it reads, so the call showed up
    under 'unlabelled' there, while a read filtered to 'unlabelled' fetches
    that one item by name and never saw it. One project then read fewer calls
    filtered than unfiltered, and fewer than the ledger listed.
    """
    return {
        "timestamp": str(item.get("timestamp") or ""),
        "status": item.get("status", ""),
        "project_name": project_label(item.get("project_name")),
        "agent": item.get("agent") or "unknown",
        "origin": item.get("origin") or "unknown",
        "stage": item.get("stage") or ("observe" if item.get("dry_run") else "enforce"),
        "hook_mode": item.get("hook_mode") or "unknown",
        "rule_key": rule_key(item),
    }


def ledger_items(table: Any, day: str) -> Iterable[Dict[str, Any]]:
    """Every row of one day's partition, page by page."""
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": f"{DECISION_PARTITION}#{day}"},
    }
    while True:
        page = table.query(**kwargs)
        yield from page.get("Items", [])
        if not page.get("LastEvaluatedKey"):
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def claim(table: Any, item: Dict[str, Any]) -> bool:
    """Marks a row as counted, only if nothing has counted it yet."""
    try:
        table.update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression="SET rolled_up = :yes",
            ConditionExpression="attribute_not_exists(rolled_up) AND attribute_not_exists(rule_key)",
            ExpressionAttributeValues={":yes": True},
        )
        return True
    except Exception as exc:  # the conditional check failing is the expected skip
        if "ConditionalCheckFailed" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc):
            return False
        raise


def backfill(repo: DynamoDBSessionRepository, days: int, today: datetime.date, dry_run: bool, out: Any) -> Dict[str, int]:
    totals = {"rows": 0, "already_counted": 0, "claimed": 0, "added": 0}
    for offset in range(days):
        day = str(today - datetime.timedelta(days=offset))
        found = claimed = 0
        for item in ledger_items(repo._table, day):
            totals["rows"] += 1
            if not needs_backfill(item):
                totals["already_counted"] += 1
                continue
            found += 1
            if dry_run:
                continue
            if not claim(repo._table, item):
                totals["already_counted"] += 1
                continue
            claimed += 1
            totals["claimed"] += 1
            decision = as_decision(item)
            counters, stamps = rollup_counters(decision)
            repo.adjust_rollup(day, str(decision["project_name"])[:120], counters, stamps)
            totals["added"] += 1
        if found:
            verb = "would add" if dry_run else "added"
            print(f"{day}: {verb} {found if dry_run else claimed} row(s)", file=out)
    return totals


def main(argv: Optional[Sequence[str]] = None, out: Any = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--stack", help="the CloudFormation stack whose table to backfill")
    target.add_argument("--table", help="the table name, when it is already known")
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--days", type=int, default=30, help="how many days back, the ledger keeps thirty")
    parser.add_argument("--dry-run", action="store_true", help="count what would be added and write nothing")
    args = parser.parse_args(argv)

    os.environ.pop("THREEFOLD_OFFLINE", None)
    table_name = args.table or table_of(args.stack, args.region)
    repo = DynamoDBSessionRepository(table_name=table_name, region_name=args.region)
    if repo._table is None:
        print("could not reach DynamoDB with these credentials", file=out)
        return 2
    days = max(1, min(args.days, 31))
    # Printed, because a stack whose AllowedProjectPattern is not the default
    # is backfilled correctly only when the same pattern is in the environment,
    # and a run that used the wrong one should say so in its own output.
    print(f"labelling projects with {allowed_project_pattern().pattern}", file=out)
    totals = backfill(repo, days, datetime.datetime.now(datetime.timezone.utc).date(), args.dry_run, out)
    print(json.dumps(dict(totals, dry_run=args.dry_run, days=days)), file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
