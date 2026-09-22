"""Aggregates the decision ledger into the few numbers a platform owner asks for.

This module answers questions, not fields: which rule refuses most, which team
is drifting, who is blocked, what did it cost. It computes nothing the ledger
did not record, and it reports its own blind spots, because a console that shows
zero boundary violations without saying that the boundary gate reads Python only
is a console that lies by omission.
"""
from __future__ import annotations

import bisect
import datetime
import statistics
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional

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
        # Replaced at call time by describe_layering(), which reads the rules in
        # force. A fixed sentence here would have gone stale the first time an
        # architect saved their own, and a coverage statement that lies is worse
        # than none: it is the line a reader trusts to interpret a zero.
        "watches": "Set from the layering rules in force",
        "blind_to": "Set from the layering rules in force",
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
        lambda: {"decisions": 0, "refused": 0, "observed": 0, "developers": set(), "rules": Counter(), "categories": Counter(), "last_seen": ""}
    )
    by_developer: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"decisions": 0, "refused": 0, "projects": set(), "last_seen": ""}
    )
    by_day: Dict[str, Dict[str, int]] = defaultdict(lambda: {"decisions": 0, "refused": 0})
    rule_counts: Counter = Counter()
    category_counts: Counter = Counter()
    observed_counts: Counter = Counter()
    observations: List[Dict[str, Any]] = []
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
        entry["last_seen"] = max(entry["last_seen"], timestamp)

        person = by_developer[developer]
        person["decisions"] += 1
        person["projects"].add(project)
        person["last_seen"] = max(person["last_seen"], timestamp)

        day = timestamp[:10]
        if day:
            by_day[day]["decisions"] += 1

        watched_by = row.get("observed_rules") or ([row["observed_rule"]] if row.get("observed_rule") else [])
        if watched_by and not refused:
            # A rule in observe mode would have refused this, and it ran anyway.
            # Counted on its own line: adding it to refusals would report a
            # stopped call that was not stopped. The call counts once; each rule
            # that would have refused it counts once towards its own total.
            entry["observed"] += 1
            for rule_id in watched_by:
                observed_counts[rule_id] += 1
            observations.append(row)

        if refused:
            entry["refused"] += 1
            entry["rules"][row.get("rule", "UNKNOWN")] += 1
            entry["categories"][categorise(row)] += 1
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
            "observed": len(observations),
            "projects": len(by_project),
            "developers": len(by_developer),
        },
        "by_category": [
            {"category": category, "label": CATEGORY_LABELS.get(category, category), "refusals": count}
            for category, count in sorted(category_counts.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "by_observed_rule": [
            {"rule": rule, "would_refuse": count}
            for rule, count in sorted(observed_counts.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "recent_observations": observations[:25],
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
                    "observed": data["observed"],
                    "developers": len(data["developers"]),
                    "top_rule": data["rules"].most_common(1)[0][0] if data["rules"] else "",
                    "top_category": data["categories"].most_common(1)[0][0] if data["categories"] else "",
                    "top_category_label": CATEGORY_LABELS.get(
                        data["categories"].most_common(1)[0][0], ""
                    ) if data["categories"] else "",
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
        "recent_refusals": [
            dict(
                row,
                category=categorise(row),
                category_label=CATEGORY_LABELS.get(categorise(row), "Other"),
            )
            for row in refusals[:25]
        ],
        "coverage": GATE_COVERAGE,
    }


# ---------------------------------------------------------------- self-correction

# How many of its own next calls an agent is given to find an acceptable way to
# do what was refused. Past that, a later approval on the same target is more
# likely to be unrelated work than an answer to the refusal.
SELF_CORRECTION_WINDOW = 10

# Only a governed agent can correct itself. A page call is a visitor pressing a
# button, and the demo's own sessions are refused on purpose and never retried,
# so counting them would measure the demo rather than the agents.
SELF_CORRECTION_ORIGINS = ("hook", "ci")


def _same_target(value: Any) -> str:
    """A target as two calls to the same file are compared.

    A path an agent sends with backslashes, or with a leading "./", names the
    same file as one sent without; anything else is compared exactly.
    """
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _is_command(row: Mapping[str, Any]) -> bool:
    """Whether a row records a command, whose target is only the program it ran.

    The same test boundary_guard.describe_target applies when it decides what
    to record: for a command it keeps the program name and never the rest of
    the line, which is where a refused credential would be.
    """
    action = str(row.get("action_type") or "").upper()
    return "COMMAND" in action or "EXEC" in action


def _instant(row: Mapping[str, Any]) -> str:
    return str(row.get("timestamp") or "")


def _answers(row: Mapping[str, Any], target: str) -> bool:
    """Whether a later call did acceptably what a refusal of `target` stopped."""
    return (
        not _is_refusal(str(row.get("status") or ""))
        and not _is_command(row)
        and _same_target(row.get("target")) == target
    )


def _calls_to_correct(calls: List[Mapping[str, Any]], instants: List[str], index: int, target: str, window: int) -> Optional[int]:
    """How many calls it took to correct the refusal at `index`, or None if none did within `window`.

    Calls recorded at the same instant have no order the ledger can recover:
    on a clock that ticks once a millisecond one agent's calls can share one,
    and the sort key that follows the timestamp is a random id. So nothing
    here depends on how tied calls were sorted. A call recorded at the
    refusal's own instant may have come before it, and is never taken as its
    correction; and a correction's distance counts every call that may have
    come between, which is those sharing the refusal's instant, those after
    it, and all of those sharing the correction's own. A tie can only lengthen
    the distance, never shorten it or stretch the window.
    """
    first = bisect.bisect_left(instants, instants[index])
    later = bisect.bisect_right(instants, instants[index])
    while later < len(calls):
        end = bisect.bisect_right(instants, instants[later])
        distance = end - first - 1
        if distance > window:
            return None
        if any(_answers(calls[position], target) for position in range(later, end)):
            return distance
        later = end
    return None


def self_correction(rows: Iterable[Mapping[str, Any]], window: int = SELF_CORRECTION_WINDOW) -> Dict[str, Any]:
    """How often an agent that was refused went on to do the same thing acceptably.

    A refusal of a governed call (origin hook or ci) self-corrected when one of
    the next `window` calls of the same session, in time order, aimed at the
    same target and was not refused: approved, or only observed by a rule that
    is still watching. The distance is how many calls that took, 1 being the
    very next one. Calls that share a timestamp are not ordered against each
    other; `_calls_to_correct` says how that is resolved.

    Every refusal counts on its own, so a target refused twice before the call
    that went through is two refusals, each corrected. A refusal whose target
    the ledger did not record (arguments with no path) is not considered at
    all: it cannot be matched with anything, and scoring it as uncorrected
    would lower the rate for a reason that has nothing to do with the agent.
    Commands are left out on both sides for the same reason. The ledger keeps
    only the program a command ran, so "the same target" would mean "the same
    program": a refused `git commit --no-verify` would be corrected by any
    later `git status`, and a refused shell write would never be corrected by
    the Write to the same file its refusal tells the agent to use. A refused
    command is therefore not considered, and a command is never taken as a
    correction, though it still counts as one of the calls in between. In the
    observe stage nothing is refused, so such a window considers no refusal.

    `rate` and `median_calls_to_correct` are None when there is nothing to
    divide or rank: none of none is not a rate, and a median of no distances
    would read as corrected at once.
    """
    sessions: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    seen = set()
    for row in rows:
        if str(row.get("origin") or "") not in SELF_CORRECTION_ORIGINS or not row.get("session_id"):
            continue
        identity = (row.get("timestamp"), row.get("verdict_id"))
        if identity in seen:
            continue
        seen.add(identity)
        sessions[str(row["session_id"])].append(row)

    considered = 0
    distances: List[int] = []
    for calls in sessions.values():
        calls.sort(key=_instant)
        instants = [_instant(call) for call in calls]
        for index, call in enumerate(calls):
            target = _same_target(call.get("target"))
            if not _is_refusal(str(call.get("status") or "")) or not target or _is_command(call):
                continue
            considered += 1
            distance = _calls_to_correct(calls, instants, index, target, window)
            if distance is not None:
                distances.append(distance)

    corrected = len(distances)
    return {
        "refusals_considered": considered,
        "self_corrected": corrected,
        "rate": round(corrected / considered, 4) if considered else None,
        "median_calls_to_correct": statistics.median(distances) if distances else None,
    }


def describe_layering(rules: List[Dict[str, Any]], languages_read: List[str]) -> Dict[str, str]:
    """What the layering gate watches, read off the rules actually in force."""
    if not rules:
        return {
            "rule": "ARCHITECTURAL_BOUNDARY_SAFE",
            "watches": "Nothing: no layering rule is configured",
            "blind_to": "Every layering question, until a rule is saved",
        }
    # An observing rule refuses nothing, so counting it as "in force" beside the
    # enforcing ones would overstate what a zero on this console means.
    enforcing = [rule for rule in rules if rule.get("mode", "enforce") != "observe"]
    observing = [rule for rule in rules if rule.get("mode", "enforce") == "observe"]

    def _listed(items: List[str], limit: int = 6) -> str:
        # A shortened list says it is shortened. Six of nine patterns printed
        # as if they were all of them told a reader the other three were not
        # watched.
        shown = ", ".join(items[:limit])
        return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")

    def _named(group: List[Dict[str, Any]]) -> str:
        return _listed([rule.get("id", "?") for rule in group])

    paths = sorted({pattern for rule in enforcing for pattern in rule.get("when_path_matches", [])})
    watches = (
        f"{len(enforcing)} rule(s) refusing ({_named(enforcing)}), over paths matching {_listed(paths)}"
        if enforcing
        else "No rule refuses anything: every layering rule here only observes"
    )
    if observing:
        watches += f"; {len(observing)} more only recording what it would refuse ({_named(observing)})"
    # Extensions rather than language names: "typescript" hid that .js, .jsx
    # and .mjs files are read by the same reader.
    watches += f". Imports are read from files ending {', '.join(languages_read)}"
    return {
        "rule": "ARCHITECTURAL_BOUNDARY_SAFE",
        "watches": watches,
        "blind_to": (
            "Any path no refusing rule covers, any file type not in that list, and the "
            "dependencies a file does not declare: an import is read from the "
            "file's own statements, not resolved, followed or injected"
        ),
    }


def with_layering_coverage(
    payload: Dict[str, Any],
    rules: List[Dict[str, Any]],
    languages_read: List[str],
) -> Dict[str, Any]:
    """Swaps the placeholder coverage row for the one the deployment can prove."""
    described = describe_layering(rules, languages_read)
    payload["coverage"] = [
        described if entry["rule"] == "ARCHITECTURAL_BOUNDARY_SAFE" else entry
        for entry in payload.get("coverage", [])
    ]
    return payload
