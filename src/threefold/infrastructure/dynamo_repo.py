"""DynamoDB repository for Threefold persistent session state and governance records."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from threefold.domain.models import AgentSession, ToolActionType, ToolInvocation

logger = logging.getLogger(__name__)

# Every decision the service makes, partitioned by day so a window is a query
# rather than a scan.
DECISION_PARTITION = "DECISION"

SESSION_TTL_SECONDS = 30 * 24 * 3600

# Each scan page is at most 1 MB, so this bounds the sessions listing at a read
# the function's timeout can afford.
MAX_SCAN_PAGES = 50

# The smallest page the listing ever asks for. Following LastEvaluatedKey and
# capping each page are separate protections and the listing needs both: without
# a Limit every page reads up to a megabyte, so one anonymous GET /api/sessions
# could pull MAX_SCAN_PAGES megabytes out of the table.
MIN_SCAN_PAGE_SIZE = 100

# One project's layering rules live under this prefix plus the project name,
# beside the shared set at CONFIG#rules rather than inside it, so saving one
# team's architecture can never overwrite everyone else's.
PROJECT_RULES_PREFIX = "CONFIG#rules#"

# A project's stage configuration lives under this prefix plus its name, the
# way its rules do. A second copy is kept under one partition, with the name as
# the sort key, so every configured project is listed by one Query rather than
# by scanning a table that is mostly ledger. Both are written on every save;
# the first is the one a single read and the evaluator trust.
PROJECT_CONFIG_PREFIX = "CONFIG#project#"
PROJECT_INDEX_PARTITION = "CONFIG#projects"
PROJECT_CONFIG_FIELDS = (
    "stage", "observe_rules", "created_at", "updated_at", "promoted_at", "demoted_at", "sandbox", "history",
)

# Pages of the project index a listing may read, each at most 1 MB.
MAX_INDEX_PAGES = 20

# Each project's totals for one day, added to on every decision. Kept a little
# longer than the ledger so a thirty-day chart never loses its first day.
STATS_PARTITION = "STATS"
ROLLUP_TTL_SECONDS = 35 * 24 * 3600


def rollup_counters(decision: Dict[str, Any]) -> tuple:
    """The counters one decision adds to its day, and the stamps it sets.

    `approved`, `observed` and `refused` are disjoint, so a day's calls are
    their sum: observed is a call that ran and that a rule would have refused.
    Besides the counters the contract fixes, `stage:` and `hook_mode:` are
    counted so the projects listing can say which modes a project's hooks run
    in without reading the ledger, and `last_seen` and `last:<rule_key>` are
    stamped for the times the readiness table shows.
    """
    status = str(decision.get("status") or "").upper()
    key = str(decision.get("rule_key") or "NONE")
    if status.startswith("BLOCKED"):
        kind = "refused"
    elif key != "NONE":
        kind = "observed"
    else:
        kind = "approved"
    counters = {
        "calls": 1,
        kind: 1,
        f"agent:{decision.get('agent') or 'unknown'}": 1,
        f"origin:{decision.get('origin') or 'unknown'}": 1,
        f"stage:{decision.get('stage') or 'enforce'}": 1,
        f"hook_mode:{decision.get('hook_mode') or 'unknown'}": 1,
    }
    timestamp = str(decision.get("timestamp") or "")
    stamps = {"last_seen": timestamp} if timestamp else {}
    if kind != "approved":
        counters[f"{kind}:{key}"] = 1
        if timestamp:
            stamps[f"last:{key}"] = timestamp
    return counters, stamps

# Who last resumed a halted session, why, when, and which halt they cleared.
# Kept on the session row beside trip_reason and terminated_by, because that is
# where the halt itself is recorded. They travel as plain attributes on the
# session object: AgentSession belongs to the domain track and has no fields for
# them yet, and a row written without them would drop the record on the very
# next approved call, which rewrites the whole item.
RESUME_FIELDS = ("resumed_by", "resume_reason", "resumed_at", "resumed_from")


def _decision_key(timestamp: str, verdict_id: str) -> Dict[str, str]:
    """The key a decision was written under: its day, then its time and verdict."""
    return {"PK": f"{DECISION_PARTITION}#{str(timestamp)[:10]}", "SK": f"{timestamp}#{verdict_id}"}


def clean_decision(row: Dict[str, Any]) -> Dict[str, Any]:
    """A stored ledger row as every reader gets it, whatever year it was written in."""
    return {
        "verdict_id": row.get("verdict_id", ""),
        "timestamp": row.get("timestamp", ""),
        "session_id": row.get("session_id", ""),
        "developer_id": row.get("developer_id", ""),
        "project_name": row.get("project_name", ""),
        "tool_name": row.get("tool_name", ""),
        "action_type": row.get("action_type", ""),
        # Rows written before request v2 carry neither, and say so.
        "agent": row.get("agent", "unknown") or "unknown",
        "origin": row.get("origin", "unknown") or "unknown",
        "dry_run": bool(row.get("dry_run", False)),
        "status": row.get("status", ""),
        "rule": row.get("rule", "NONE"),
        "target": row.get("target", ""),
        "reason": row.get("reason", ""),
        "observed_rule": row.get("observed_rule", ""),
        # Rows written before every rule was kept carry one name only.
        "observed_rules": list(row.get("observed_rules") or ([row["observed_rule"]] if row.get("observed_rule") else [])),
        "observed_reason": row.get("observed_reason", ""),
        "observed_target": row.get("observed_target", ""),
        "cost_usd": float(row.get("cost_usd", 0) or 0),
        # Empty on rows written before the key existed. The application layer
        # gives those one on the way out, because reading it off a reason is
        # its business, not the store's.
        "rule_key": row.get("rule_key", "") or "",
        # A row from before stages was enforced unless it was a dry run, which
        # is what it says it was judged under.
        "stage": row.get("stage") or ("observe" if row.get("dry_run") else "enforce"),
        "hook_mode": row.get("hook_mode", "unknown") or "unknown",
        # A review is stored on the row it is about. Who made it is kept as a
        # short hash and is not handed to readers of the row.
        "review": row.get("review") or None,
        "reviewed_at": row.get("reviewed_at") or None,
        "review_note": row.get("review_note") or None,
    }


def _plain(value: Any) -> Any:
    """A value read from DynamoDB with its Decimals turned back into numbers.

    The resource API hands every number back as a Decimal, which json.dumps
    refuses, so anything that reaches a response passes through here first.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    return value


class SessionConflictError(RuntimeError):
    """Raised when a write is refused because the stored session is already tripped.

    A tripped session is terminal. Once the circuit breaker has fired, no later
    write may advance or clear it, so a second Lambda container cannot approve
    work on a session another container has already halted.
    """


class DynamoDBSessionRepository:
    """Production repository persisting agent sessions and circuit breaker states across Lambda invocations."""

    def __init__(
        self,
        table_name: Optional[str] = None,
        region_name: Optional[str] = None,
        boto3_resource: Optional[Any] = None,
    ) -> None:
        self.table_name = table_name or os.getenv("TABLE_NAME", "Threefold-Governance-prod")
        # AWS_REGION is set by the Lambda runtime; it is reserved and cannot be
        # declared in the function's own environment variables.
        self.region_name = region_name or os.getenv("AWS_REGION", "eu-west-1")
        self._table = None
        self._memory_store: Dict[str, Dict[str, Any]] = {}
        self._offline = os.getenv("THREEFOLD_OFFLINE", "").lower() in ("1", "true", "yes")

        if boto3_resource is not None:
            self._table = boto3_resource.Table(self.table_name)
        elif not self._offline:
            try:
                import boto3
                dynamo = boto3.resource("dynamodb", region_name=self.region_name)
                self._table = dynamo.Table(self.table_name)
            except Exception as exc:
                logger.info("DynamoDB client operating in local/in-memory fallback: %s", exc)
                self._table = None

    @property
    def is_live(self) -> bool:
        return self._table is not None

    @property
    def persistence_mode(self) -> str:
        """Reports where session state actually landed on the most recent write.

        'dynamodb' means the table accepted the write. 'memory' means the write
        survives only inside this process, so a second container will not see it.
        """
        return self._last_persistence_mode

    _last_persistence_mode: str = "memory"

    def probe(self) -> tuple[bool, str]:
        """Read probe exercising the same permission the application uses.

        Uses GetItem rather than DescribeTable because the function's IAM policy
        grants GetItem, so this reports the capability that actually matters.
        """
        if self._table is None:
            return False, "No DynamoDB table bound; running on the in-process store"
        try:
            self._table.get_item(Key={"PK": "SESSION#__probe__", "SK": "METADATA"})
            return True, f"GetItem succeeded against {self.table_name}"
        except Exception as exc:
            return False, f"GetItem failed against {self.table_name}: {exc}"

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        """Loads persistent session from DynamoDB or memory store."""
        item = None
        if self._table is not None:
            try:
                res = self._table.get_item(Key={"PK": f"SESSION#{session_id}", "SK": "METADATA"})
                item = res.get("Item")
            except Exception as exc:
                logger.warning("Failed to fetch session from DynamoDB: %s", exc)

        if item is None:
            item = self._memory_store.get(f"SESSION#{session_id}#METADATA")

        if not item:
            return None

        # Reconstruct ToolInvocation history
        history: List[ToolInvocation] = []
        raw_history = item.get("history_json")
        if raw_history:
            try:
                parsed_list = json.loads(raw_history)
                for h in parsed_list:
                    action_type = ToolActionType(h.get("action_type", "UNKNOWN"))
                    history.append(
                        ToolInvocation(
                            tool_name=h.get("tool_name", ""),
                            action_type=action_type,
                            arguments=h.get("arguments", {}),
                            timestamp=h.get("timestamp", ""),
                        )
                    )
            except Exception as exc:
                logger.warning("Could not parse history JSON: %s", exc)

        session = AgentSession(
            session_id=session_id,
            developer_id=item.get("developer_id", "dev-default"),
            project_name=item.get("project_name", "Acme-Core"),
            budget_usd=float(item.get("budget_usd", 10.0)),
            total_cost_usd=float(item.get("total_cost_usd", 0.0)),
            total_input_tokens=int(item.get("total_input_tokens", 0)),
            total_output_tokens=int(item.get("total_output_tokens", 0)),
            history=history,
            is_tripped=bool(item.get("is_tripped", False)),
            trip_reason=item.get("trip_reason") or None,
            is_terminated=bool(item.get("is_terminated", False)),
            terminated_by=item.get("terminated_by") or None,
            termination_reason=item.get("termination_reason") or None,
            created_at=item.get("created_at", ""),
        )
        for name in RESUME_FIELDS:
            if item.get(name):
                setattr(session, name, item[name])
        return session

    def list_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns recent sessions, newest first, for the dashboard.

        A scan is honest at this scale and dishonest at any other. The table
        holds one item per session with a thirty day ttl, so the working set is
        small; a deployment with real traffic wants a secondary index on a
        recency key instead, and this is where that change goes.

        The scan follows LastEvaluatedKey to the end. It used to read one page
        of a few hundred items and keep the sessions among them, but the same
        table holds a ledger row for every decision, so once decisions
        outnumbered that page the listing lost sessions and nothing said so.
        The filter runs after DynamoDB has read each page, so a page can come
        back with no sessions in it and still not be the last one.
        """
        items: List[Dict[str, Any]] = []
        if self._table is not None:
            try:
                items = self._scan_session_metadata(limit)
            except Exception as exc:
                logger.warning("Failed to list sessions from DynamoDB: %s", exc)
                items = []
        if not items:
            items = [
                item for key, item in self._memory_store.items()
                if key.endswith("#METADATA")
            ]

        summaries = []
        for item in items:
            pk = str(item.get("PK", ""))
            if not pk.startswith("SESSION#") or pk.endswith("__probe__"):
                continue
            summaries.append(
                {
                    "session_id": pk[len("SESSION#"):],
                    "developer_id": item.get("developer_id", ""),
                    "project_name": item.get("project_name", ""),
                    "total_cost_usd": float(item.get("total_cost_usd", 0) or 0),
                    "total_input_tokens": int(item.get("total_input_tokens", 0) or 0),
                    "total_output_tokens": int(item.get("total_output_tokens", 0) or 0),
                    "is_tripped": bool(item.get("is_tripped", False)),
                    "trip_reason": item.get("trip_reason") or "",
                    "is_terminated": bool(item.get("is_terminated", False)),
                    "created_at": item.get("created_at", ""),
                    "calls": len(json.loads(item.get("history_json") or "[]")),
                }
            )
        summaries.sort(key=lambda s: s["created_at"], reverse=True)
        return summaries[:limit]

    def _scan_session_metadata(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Every session metadata item, across as many scan pages as it takes.

        Expressed as strings rather than with boto3's Attr helper so this module
        keeps its single lazy import of boto3. Bounded twice: each page carries a
        Limit, so no single page reads a megabyte on behalf of an anonymous
        caller, and MAX_SCAN_PAGES caps how many pages a table far past the size
        this listing was designed for may cost. The log says when it was cut
        short. The Limit applies before the filter, so it is several times the
        number of sessions asked for, most rows in this table being decisions.
        """
        items: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {
            "FilterExpression": "SK = :metadata",
            "ExpressionAttributeValues": {":metadata": "METADATA"},
            "Limit": max(limit * 4, MIN_SCAN_PAGE_SIZE),
        }
        for _ in range(MAX_SCAN_PAGES):
            response = self._table.scan(**kwargs)
            items.extend(i for i in response.get("Items", []) if i.get("SK") == "METADATA")
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return items
            kwargs["ExclusiveStartKey"] = last_key
        logger.warning(
            "Session listing stopped after %d scan pages with more left; the oldest sessions may be missing",
            MAX_SCAN_PAGES,
        )
        return items

    # ------------------------------------------------------------------ ledger

    def record_decision(self, decision: Dict[str, Any]) -> bool:
        """Appends one decision to the ledger.

        Partitioned by day and sorted by timestamp, so a console asks for a
        window with one Query per day rather than scanning the table. The same
        thirty day ttl the sessions carry applies here: this is an operating
        record, not an archive, and it says so wherever it is shown.
        """
        timestamp = str(decision.get("timestamp") or "")
        day = timestamp[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        item = {
            "PK": f"{DECISION_PARTITION}#{day}",
            "SK": f"{timestamp}#{decision.get('verdict_id', '')}",
            "ttl": int(time.time()) + SESSION_TTL_SECONDS,
        }
        for key, value in decision.items():
            if isinstance(value, float):
                item[key] = str(value)
            elif value is not None:
                item[key] = value

        self._add_to_rollup(day, decision)
        if self._table is not None:
            try:
                self._table.put_item(Item=item)
                # A decision is a write like any other, and the badge that reports
                # where writes land was reading session writes only. On the console,
                # which is a page about this ledger, that was the wrong write.
                self._last_persistence_mode = "dynamodb"
                return True
            except Exception as exc:
                logger.warning("DynamoDB record_decision failed, keeping it in memory: %s", exc)
        self._last_persistence_mode = "memory"
        self._memory_store[f"{item['PK']}#{item['SK']}"] = item
        return True

    # ------------------------------------------------------------------ rollups

    def _add_to_rollup(self, day: str, decision: Dict[str, Any]) -> None:
        """Counts one decision into its project's daily totals, best effort.

        Charts and tiles read these rather than the ledger, so they are exact
        however busy the ledger is. A failed rollup never fails a verdict, and
        never stops the ledger row being written: it is logged and dropped.
        """
        try:
            counters, stamps = rollup_counters(decision)
            project = str(decision.get("project_name") or "unlabelled")[:120]
            self.adjust_rollup(day, project, counters, stamps)
        except Exception as exc:  # pragma: no cover - the rollup must not break a verdict
            logger.warning("Could not add a decision to the daily rollup: %s", exc)

    def adjust_rollup(
        self,
        day: str,
        project: str,
        counters: Dict[str, int],
        stamps: Optional[Dict[str, str]] = None,
    ) -> None:
        """ADDs to PK=STATS#<day>, SK=<project>, and sets its ttl if it has none.

        One UpdateItem, so concurrent containers add rather than overwrite.
        Counter names hold ':' ("agent:codex"), which an update expression
        cannot spell, so every name goes through ExpressionAttributeNames.
        `stamps` are SET, for the last time something was seen. Negative
        counts are how a review that is changed or cleared is taken back out.
        """
        counters = {name: int(value) for name, value in counters.items() if int(value)}
        stamps = dict(stamps or {})
        if not counters and not stamps:
            return
        partition = f"{STATS_PARTITION}#{day}"
        expires = int(time.time()) + ROLLUP_TTL_SECONDS
        if self._table is not None:
            names: Dict[str, str] = {"#ttl": "ttl"}
            values: Dict[str, Any] = {":ttl": expires}
            added = []
            for index, (name, value) in enumerate(sorted(counters.items())):
                names[f"#c{index}"] = name
                values[f":c{index}"] = value
                added.append(f"#c{index} :c{index}")
            assigned = ["#ttl = if_not_exists(#ttl, :ttl)"]
            for index, (name, value) in enumerate(sorted(stamps.items())):
                names[f"#s{index}"] = name
                values[f":s{index}"] = value
                assigned.append(f"#s{index} = :s{index}")
            expression = "SET " + ", ".join(assigned)
            if added:
                expression += " ADD " + ", ".join(added)
            try:
                self._table.update_item(
                    Key={"PK": partition, "SK": project},
                    UpdateExpression=expression,
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                )
                return
            except Exception as exc:
                logger.warning("DynamoDB rollup update failed, counting in memory: %s", exc)
        stored = self._memory_store.setdefault(
            f"{partition}#{project}", {"PK": partition, "SK": project, "ttl": expires}
        )
        for name, value in counters.items():
            stored[name] = int(stored.get(name, 0) or 0) + value
        stored.update(stamps)

    def list_rollups(self, days: int = 7, project: Optional[str] = None) -> List[Dict[str, Any]]:
        """The daily totals of the last `days` days, one dict per project and day.

        Each carries `day` and `project` beside its counters. One Query per day
        for every project, or one GetItem per day for one.
        """
        today = datetime.now(timezone.utc).date()
        wanted = [str(today - timedelta(days=offset)) for offset in range(max(1, days))]
        items: List[Dict[str, Any]] = []
        if self._table is not None:
            for day in wanted:
                partition = f"{STATS_PARTITION}#{day}"
                try:
                    if project is not None:
                        found = self._table.get_item(Key={"PK": partition, "SK": project}).get("Item")
                        items.extend([found] if found else [])
                        continue
                    kwargs: Dict[str, Any] = {
                        "KeyConditionExpression": "PK = :pk",
                        "ExpressionAttributeValues": {":pk": partition},
                    }
                    for _ in range(MAX_INDEX_PAGES):
                        response = self._table.query(**kwargs)
                        items.extend(response.get("Items", []))
                        if not response.get("LastEvaluatedKey"):
                            break
                        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                except Exception as exc:
                    logger.warning("Failed to read the rollups for %s: %s", day, exc)
        if not items:
            prefixes = tuple(f"{STATS_PARTITION}#{day}#" for day in wanted)
            items = [
                item for key, item in self._memory_store.items()
                if key.startswith(prefixes) and (project is None or item.get("SK") == project)
            ]
        rollups = []
        for item in items:
            plain = _plain(item)
            day = str(plain.pop("PK", ""))[len(STATS_PARTITION) + 1:]
            name = str(plain.pop("SK", ""))
            plain.pop("ttl", None)
            rollups.append(dict(plain, day=day, project=name))
        return rollups

    def list_decisions(self, days: int = 7, limit: int = 1000) -> List[Dict[str, Any]]:
        """Returns the decisions of the last `days` days, newest first."""
        today = datetime.now(timezone.utc).date()
        wanted = [str(today - timedelta(days=offset)) for offset in range(max(1, days))]
        rows: List[Dict[str, Any]] = []

        if self._table is not None:
            for day in wanted:
                if len(rows) >= limit:
                    break
                try:
                    # Expressed as a string rather than with boto3's Key helper so
                    # this module keeps its single lazy import of boto3.
                    response = self._table.query(
                        KeyConditionExpression="PK = :pk",
                        ExpressionAttributeValues={":pk": f"{DECISION_PARTITION}#{day}"},
                        ScanIndexForward=False,
                        Limit=min(limit, 500),
                    )
                    rows.extend(response.get("Items", []))
                except Exception as exc:
                    logger.warning("Failed to read the decision ledger for %s: %s", day, exc)

        if not rows:
            rows = [
                item
                for key, item in self._memory_store.items()
                if key.startswith(f"{DECISION_PARTITION}#")
                and str(item.get("timestamp", ""))[:10] in wanted
            ]

        cleaned = [clean_decision(row) for row in rows]
        cleaned.sort(key=lambda d: d["timestamp"], reverse=True)
        return cleaned[:limit]

    def read_decision_day(
        self, day: str, after: Optional[str] = None, limit: int = 200
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """One page of one day's decisions, newest first, and where the next page starts.

        `after` is the sort key of the last row a caller has seen; the second
        value is the sort key to pass as `after` next time, or None when the day
        is exhausted. Each row carries its sort key as `_sk` so a caller that
        stops part way through a page can resume exactly after the last row it
        kept rather than after the last row this page happened to hold.
        """
        partition = f"{DECISION_PARTITION}#{day}"
        if self._table is not None:
            kwargs: Dict[str, Any] = {
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": partition},
                "ScanIndexForward": False,
                "Limit": max(1, int(limit)),
            }
            if after:
                kwargs["ExclusiveStartKey"] = {"PK": partition, "SK": after}
            try:
                response = self._table.query(**kwargs)
            except Exception as exc:
                logger.warning("Failed to read the decision ledger for %s: %s", day, exc)
            else:
                items = response.get("Items", [])
                last = response.get("LastEvaluatedKey") or {}
                rows = [dict(clean_decision(item), _sk=str(item.get("SK", ""))) for item in items]
                return rows, (str(last["SK"]) if last.get("SK") else None)
        prefix = f"{partition}#"
        keyed = sorted(
            ((key[len(prefix):], item) for key, item in self._memory_store.items() if key.startswith(prefix)),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if after:
            keyed = [pair for pair in keyed if pair[0] < after]
        page = keyed[: max(1, int(limit))]
        rows = [dict(clean_decision(item), _sk=sort_key) for sort_key, item in page]
        more = len(keyed) > len(page)
        return rows, (page[-1][0] if more and page else None)

    def get_decision(self, timestamp: str, verdict_id: str) -> Optional[Dict[str, Any]]:
        """One decision by the two values every row carries, or None."""
        key = _decision_key(timestamp, verdict_id)
        if self._table is not None:
            try:
                item = self._table.get_item(Key=key).get("Item")
            except Exception as exc:
                logger.warning("Failed to read a decision: %s", exc)
            else:
                return clean_decision(item) if item else None
        item = self._memory_store.get(f"{key['PK']}#{key['SK']}")
        return clean_decision(item) if item else None

    def label_decision(
        self,
        timestamp: str,
        verdict_id: str,
        project: str,
        label: Optional[str],
        note: str = "",
        reviewed_by: str = "anonymous",
        reviewed_at: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Stores a review on the ledger row itself, or clears it when `label` is None.

        Conditional on the row existing and belonging to `project`, so a label
        sent under one project's name can never land on another's row. Returns
        the row as it was before, which says what the label replaced, or None
        when the condition failed.
        """
        key = _decision_key(timestamp, verdict_id)
        if self._table is not None:
            # Every name through a placeholder, so no attribute can collide with
            # one of DynamoDB's reserved words.
            names = {
                "#project": "project_name", "#review": "review", "#at": "reviewed_at",
                "#note": "review_note", "#by": "reviewed_by",
            }
            values: Dict[str, Any] = {":project": project}
            if label is None:
                expression = "REMOVE #review, #at, #note, #by"
            else:
                expression = "SET #review = :label, #at = :at, #note = :note, #by = :by"
                values.update({":label": label, ":at": reviewed_at, ":note": note, ":by": reviewed_by})
            try:
                response = self._table.update_item(
                    Key=key,
                    UpdateExpression=expression,
                    ConditionExpression="attribute_exists(PK) AND #project = :project",
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                    ReturnValues="ALL_OLD",
                )
                return clean_decision(response.get("Attributes") or {})
            except Exception as exc:
                code = ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code", "")
                if type(exc).__name__ == "ConditionalCheckFailedException" or code == "ConditionalCheckFailedException":
                    return None
                logger.warning("DynamoDB review update failed, labelling in memory: %s", exc)
        stored = self._memory_store.get(f"{key['PK']}#{key['SK']}")
        if not stored or stored.get("project_name") != project:
            return None
        before = clean_decision(stored)
        if label is None:
            for name in ("review", "reviewed_at", "review_note", "reviewed_by"):
                stored.pop(name, None)
        else:
            stored.update(review=label, reviewed_at=reviewed_at, review_note=note, reviewed_by=reviewed_by)
        return before

    def load_policy(self) -> Optional[Dict[str, Any]]:
        """Reads the saved policy, so settings outlive the container that set them."""
        if self._table is not None:
            try:
                res = self._table.get_item(Key={"PK": "CONFIG#policy", "SK": "METADATA"})
                item = res.get("Item")
                if item:
                    return {k: v for k, v in item.items() if k not in ("PK", "SK", "ttl")}
            except Exception as exc:
                logger.warning("Failed to read policy from DynamoDB: %s", exc)
        return self._memory_store.get("CONFIG#policy#METADATA")

    def save_policy(self, config: Dict[str, Any]) -> bool:
        """Persists the policy. Settings that vanish on a cold start are not settings."""
        item = {"PK": "CONFIG#policy", "SK": "METADATA"}
        item.update({k: (str(v) if isinstance(v, float) else v) for k, v in config.items()})
        if self._table is not None:
            try:
                self._table.put_item(Item=item)
                self._memory_store["CONFIG#policy#METADATA"] = item
                return True
            except Exception as exc:
                logger.warning("DynamoDB save_policy failed, writing to memory: %s", exc)
        self._memory_store["CONFIG#policy#METADATA"] = item
        return True

    def load_rules(self) -> Optional[List[Dict[str, Any]]]:
        """Reads the saved layering rules, so a cold container enforces the same ones.

        A failed read raises rather than answering None. None means "nothing was
        ever saved", and a warm container that re-reads the rules would take it
        as an instruction to go back to the shipped set.
        """
        return self._get_rules("CONFIG#rules")

    def save_rules(self, rules: List[Dict[str, Any]]) -> bool:
        """Persists the rules. An architecture that resets on a cold start is not one."""
        return self._put_rules("CONFIG#rules", rules)

    def load_project_rules(self, project: str) -> Optional[List[Dict[str, Any]]]:
        """Reads the rules saved for one project, or None when it has none of its own.

        A method of its own rather than a parameter on load_rules: subclasses
        elsewhere override load_rules with no arguments, and a project passed to
        one of those would fail as a read and be taken for an outage. A failed
        read raises for the same reason load_rules does.
        """
        return self._get_rules(f"{PROJECT_RULES_PREFIX}{project}")

    def save_project_rules(self, project: str, rules: List[Dict[str, Any]]) -> bool:
        """Persists one project's rules beside the shared set, never over it."""
        return self._put_rules(f"{PROJECT_RULES_PREFIX}{project}", rules)

    def _get_rules(self, partition: str) -> Optional[List[Dict[str, Any]]]:
        if self._table is not None:
            try:
                res = self._table.get_item(Key={"PK": partition, "SK": "METADATA"})
            except Exception as exc:
                logger.warning("Failed to read layering rules %s from DynamoDB: %s", partition, exc)
                raise
            item = res.get("Item")
            if item and item.get("rules_json"):
                return json.loads(item["rules_json"])
            return None
        stored = self._memory_store.get(f"{partition}#METADATA")
        if stored and stored.get("rules_json"):
            return json.loads(stored["rules_json"])
        return None

    def _put_rules(self, partition: str, rules: List[Dict[str, Any]]) -> bool:
        # The memory key is the partition and the sort key together, so a project
        # an operator's own pattern allows to be called "METADATA" still lands
        # on a key of its own rather than on the shared set's.
        memory_key = f"{partition}#METADATA"
        item = {
            "PK": partition,
            "SK": "METADATA",
            "rules_json": json.dumps(rules, sort_keys=True),
            "rule_count": len(rules),
        }
        if self._table is not None:
            try:
                self._table.put_item(Item=item)
                self._memory_store[memory_key] = item
                self._last_persistence_mode = "dynamodb"
                return True
            except Exception as exc:
                logger.warning("DynamoDB save of layering rules %s failed, writing to memory: %s", partition, exc)
        self._memory_store[memory_key] = item
        self._last_persistence_mode = "memory"
        return True

    # ------------------------------------------------------------ project stage

    @staticmethod
    def _config_from_item(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The configuration an item holds, or None when it has expired.

        DynamoDB deletes an expired item some time after its ttl, not at it, so
        a sandbox a day old is treated as gone here rather than whenever the
        sweeper arrives.
        """
        if not item:
            return None
        expires = item.get("ttl")
        if expires is not None:
            try:
                if int(expires) < int(time.time()):
                    return None
            except (TypeError, ValueError):
                pass
        return {name: _plain(item[name]) for name in PROJECT_CONFIG_FIELDS if name in item}

    def load_project_config(self, project: str) -> Optional[Dict[str, Any]]:
        """Reads one project's stage configuration, or None when it has none.

        A failed read raises, as a failed read of rules does: None means "never
        configured", and a warm container would take it as an instruction to
        fall back to the stack's default stage.
        """
        key = f"{PROJECT_CONFIG_PREFIX}{project}"
        if self._table is not None:
            try:
                res = self._table.get_item(Key={"PK": key, "SK": "METADATA"})
            except Exception as exc:
                logger.warning("Failed to read the stage of %s from DynamoDB: %s", key, exc)
                raise
            return self._config_from_item(res.get("Item"))
        return self._config_from_item(self._memory_store.get(f"{key}#METADATA"))

    def save_project_config(
        self, project: str, config: Dict[str, Any], ttl_seconds: Optional[int] = None
    ) -> bool:
        """Persists a project's configuration, and its copy in the project index.

        `ttl_seconds` is for a sandbox, which is gone a day after it was made;
        a real project's configuration never expires.
        """
        fields = {name: config.get(name) for name in PROJECT_CONFIG_FIELDS if config.get(name) is not None}
        extra: Dict[str, Any] = {}
        if ttl_seconds:
            extra["ttl"] = int(time.time()) + int(ttl_seconds)
        primary = dict({"PK": f"{PROJECT_CONFIG_PREFIX}{project}", "SK": "METADATA"}, **fields, **extra)
        index = dict({"PK": PROJECT_INDEX_PARTITION, "SK": project}, **fields, **extra)
        if self._table is not None:
            try:
                self._table.put_item(Item=primary)
                self._table.put_item(Item=index)
                self._memory_store[f"{primary['PK']}#METADATA"] = primary
                self._memory_store[f"{PROJECT_INDEX_PARTITION}#{project}"] = index
                self._last_persistence_mode = "dynamodb"
                return True
            except Exception as exc:
                logger.warning("DynamoDB save of the stage of %s failed, writing to memory: %s", project, exc)
        self._memory_store[f"{primary['PK']}#METADATA"] = primary
        self._memory_store[f"{PROJECT_INDEX_PARTITION}#{project}"] = index
        self._last_persistence_mode = "memory"
        return True

    def list_project_configs(self) -> Dict[str, Dict[str, Any]]:
        """Every configured project, by name, from the project index."""
        items: List[Dict[str, Any]] = []
        if self._table is not None:
            try:
                kwargs: Dict[str, Any] = {
                    "KeyConditionExpression": "PK = :pk",
                    "ExpressionAttributeValues": {":pk": PROJECT_INDEX_PARTITION},
                }
                for _ in range(MAX_INDEX_PAGES):
                    response = self._table.query(**kwargs)
                    items.extend(response.get("Items", []))
                    if not response.get("LastEvaluatedKey"):
                        break
                    kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            except Exception as exc:
                logger.warning("Failed to list configured projects: %s", exc)
                items = []
        if not items:
            prefix = f"{PROJECT_INDEX_PARTITION}#"
            items = [item for key, item in self._memory_store.items() if key.startswith(prefix)]
        found: Dict[str, Dict[str, Any]] = {}
        for item in items:
            config = self._config_from_item(item)
            if config is not None and item.get("SK"):
                found[str(item["SK"])] = config
        return found

    def save_session(self, session: AgentSession, force: bool = False) -> bool:
        """Persists complete session state to DynamoDB.

        Unless ``force`` is set, the write is refused when the stored session is
        already tripped, and ``SessionConflictError`` is raised. Operators pass
        ``force`` to terminate a session that has already halted itself.
        """
        history_serialized = json.dumps([
            {
                "tool_name": h.tool_name,
                "action_type": h.action_type.value,
                "arguments": h.arguments,
                "timestamp": h.timestamp,
            }
            for h in session.history[-50:]  # Keep last 50 actions for sliding window
        ])

        item = {
            "PK": f"SESSION#{session.session_id}",
            "SK": "METADATA",
            "developer_id": session.developer_id,
            "project_name": session.project_name,
            "budget_usd": str(session.budget_usd),
            "total_cost_usd": str(session.total_cost_usd),
            "total_input_tokens": session.total_input_tokens,
            "total_output_tokens": session.total_output_tokens,
            "is_tripped": session.is_tripped,
            "trip_reason": session.trip_reason or "",
            "is_terminated": session.is_terminated,
            "terminated_by": session.terminated_by or "",
            "termination_reason": session.termination_reason or "",
            "history_json": history_serialized,
            "created_at": session.created_at,
            "ttl": int(time.time()) + SESSION_TTL_SECONDS,
        }
        for name in RESUME_FIELDS:
            recorded = getattr(session, name, None)
            if recorded:
                item[name] = str(recorded)
        memory_key = f"SESSION#{session.session_id}#METADATA"

        if self._table is not None:
            put_kwargs: Dict[str, Any] = {"Item": item}
            if not force:
                put_kwargs["ConditionExpression"] = (
                    "attribute_not_exists(PK) OR attribute_not_exists(is_tripped) "
                    "OR is_tripped = :not_tripped"
                )
                put_kwargs["ExpressionAttributeValues"] = {":not_tripped": False}
            try:
                self._table.put_item(**put_kwargs)
                self._last_persistence_mode = "dynamodb"
                self._memory_store[memory_key] = item
                return True
            except Exception as exc:
                error_code = ""
                response = getattr(exc, "response", None)
                if isinstance(response, dict):
                    error_code = response.get("Error", {}).get("Code", "")
                if (
                    type(exc).__name__ == "ConditionalCheckFailedException"
                    or error_code == "ConditionalCheckFailedException"
                ):
                    raise SessionConflictError(
                        f"Session {session.session_id} is already tripped in durable storage"
                    ) from exc
                logger.warning("DynamoDB save_session failed, writing to memory: %s", exc)

        self._last_persistence_mode = "memory"
        stored = self._memory_store.get(memory_key)
        if not force and stored and stored.get("is_tripped"):
            raise SessionConflictError(
                f"Session {session.session_id} is already tripped in the in-process store"
            )
        self._memory_store[memory_key] = item
        return True
