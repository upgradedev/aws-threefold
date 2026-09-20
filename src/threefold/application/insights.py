"""Aggregates the decision ledger into the few numbers a platform owner asks for.

This module answers questions, not fields: which rule refuses most, which team
is drifting, who is blocked, what did it cost. It computes nothing the ledger
did not record, and it reports its own blind spots, because a console that shows
zero boundary violations without saying that the boundary gate reads Python only
is a console that lies by omission.
"""
from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from typing import Any, Dict, List

# What the gates actually watch today. Shown on the console beside the counts,
# so a zero is read as "nothing was refused here" rather than "nothing happens
# here", which are different claims.
GATE_COVERAGE = [
    {
        "rule": "SECRET_LEAKAGE_FREE",
        "watches": "Ten credential shapes in any argument, at any depth, for every language",
        "blind_to": "Credentials that do not match a known shape, and anything already in the file on disk",
    },
    {
        "rule": "ARCHITECTURAL_BOUNDARY_SAFE",
        "watches": "Python files under a directory named domain/, importing one of twelve library roots or a package part named infrastructure or adapters",
        "blind_to": "Java, C#, TypeScript and every other language, any layering convention that is not a domain/ directory, and any dependency outside that list",
    },
    {
        "rule": "LOOP_THRASHING_FREE",
        "watches": "Any repeating cycle of byte-identical tool calls, up to period six, within one session",
        "blind_to": "Two calls that differ by one character, and repetition across separate sessions",
    },
    {
        "rule": "BUDGET_CIRCUIT_BREAKER_SAFE",
        "watches": "Projected spend per call and per session, against the policy",
        "blind_to": "Real usage. The counts are the ones the caller declares, so a caller declaring zero is not stopped",
    },
]


def _is_refusal(status: str) -> bool:
    return bool(status) and status.upper().startswith("BLOCKED")


# A rule is an invariant; a category is what a reader recognises. The boundary
# invariant covers two different worries — a layer being crossed and a
# credential store being reached — and a console that reported them as one
# number would tell a platform owner nothing he could act on differently.
CATEGORY_BY_REASON = (
    ("Clean Architecture violation", "LAYERING"),
    ("protected path", "PROTECTED_PATH"),
    ("credential store", "PROTECTED_PATH"),
    ("Sensitive credential detected", "CREDENTIAL_IN_ARGUMENTS"),
)

CATEGORY_LABELS = {
    "LAYERING": "Layer crossed",
    "PROTECTED_PATH": "Credential store or protected path reached",
    "CREDENTIAL_IN_ARGUMENTS": "Credential in the arguments",
    "LOOP": "Repeating cycle",
    "BUDGET": "Spend ceiling",
    "HALTED_SESSION": "Session already halted",
    "OTHER": "Other",
    "NONE": "Allowed",
}


def categorise(row: Dict[str, Any]) -> str:
    """Names what a reader would call this refusal."""
    status = (row.get("status") or "").upper()
    if not _is_refusal(status):
        return "NONE"
    if "LOOP" in status:
        return "LOOP"
    if "CIRCUIT_BREAKER" in status:
        return "HALTED_SESSION"
    if "BUDGET" in status or "COST" in status:
        return "BUDGET"
    reason = row.get("reason") or ""
    for needle, category in CATEGORY_BY_REASON:
        if needle.lower() in reason.lower():
            return category
    if "SECRET" in status:
        return "CREDENTIAL_IN_ARGUMENTS"
    if "BOUNDARY" in status:
        return "LAYERING"
    return "OTHER"


def summarise(decisions: List[Dict[str, Any]], window_days: int) -> Dict[str, Any]:
    """Turns a list of ledger rows into the console's payload."""
    by_project: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"decisions": 0, "refused": 0, "cost_usd": 0.0, "developers": set(), "rules": Counter(), "last_seen": ""}
    )
    by_developer: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"decisions": 0, "refused": 0, "projects": set(), "last_seen": ""}
    )
    by_day: Dict[str, Dict[str, int]] = defaultdict(lambda: {"decisions": 0, "refused": 0})
    rule_counts: Counter = Counter()
    category_counts: Counter = Counter()
    refusals: List[Dict[str, Any]] = []

    for row in decisions:
        project = row.get("project_name") or "unattributed"
        developer = row.get("developer_id") or "unattributed"
        status = row.get("status", "")
        timestamp = row.get("timestamp", "")
        refused = _is_refusal(status)

        entry = by_project[project]
        entry["decisions"] += 1
        entry["developers"].add(developer)
        entry["cost_usd"] = round(max(entry["cost_usd"], float(row.get("cost_usd", 0) or 0)), 4)
        entry["last_seen"] = max(entry["last_seen"], timestamp)

        person = by_developer[developer]
        person["decisions"] += 1
        person["projects"].add(project)
        person["last_seen"] = max(person["last_seen"], timestamp)

        day = timestamp[:10]
        if day:
            by_day[day]["decisions"] += 1

        if refused:
            entry["refused"] += 1
            entry["rules"][row.get("rule", "UNKNOWN")] += 1
            person["refused"] += 1
            rule_counts[row.get("rule", "UNKNOWN")] += 1
            category_counts[categorise(row)] += 1
            if day:
                by_day[day]["refused"] += 1
            refusals.append(row)

    total = len(decisions)
    refused_total = sum(1 for row in decisions if _is_refusal(row.get("status", "")))

    return {
        "window_days": window_days,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "totals": {
            "decisions": total,
            "refused": refused_total,
            "approved": total - refused_total,
            "refusal_rate": round(refused_total / total, 4) if total else 0.0,
            "projects": len(by_project),
            "developers": len(by_developer),
        },
        "by_category": [
            {"category": category, "label": CATEGORY_LABELS.get(category, category), "refusals": count}
            for category, count in sorted(category_counts.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "by_rule": [
            {"rule": rule, "refusals": count}
            for rule, count in sorted(rule_counts.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "by_project": sorted(
            (
                {
                    "project": project,
                    "decisions": data["decisions"],
                    "refused": data["refused"],
                    "refusal_rate": round(data["refused"] / data["decisions"], 4) if data["decisions"] else 0.0,
                    "developers": len(data["developers"]),
                    "top_rule": data["rules"].most_common(1)[0][0] if data["rules"] else "",
                    "last_seen": data["last_seen"],
                }
                for project, data in by_project.items()
            ),
            key=lambda row: (row["refused"], row["decisions"]),
            reverse=True,
        ),
        "by_developer": sorted(
            (
                {
                    "developer": developer,
                    "decisions": data["decisions"],
                    "refused": data["refused"],
                    "projects": len(data["projects"]),
                    "last_seen": data["last_seen"],
                }
                for developer, data in by_developer.items()
            ),
            key=lambda row: (row["refused"], row["decisions"]),
            reverse=True,
        ),
        "by_day": [
            {"day": day, "decisions": counts["decisions"], "refused": counts["refused"]}
            for day, counts in sorted(by_day.items())
        ],
        "recent_refusals": refusals[:25],
        "coverage": GATE_COVERAGE,
    }
