"""The dashboard's numbers, read from the daily rollups rather than the ledger.

Each project's day is one item of counters, added to as every decision is
recorded, so a chart or a tile costs one read per day however busy the ledger
is, and is exact. Lists of calls read the ledger; these never do.

Reviews are counted into the same items when a call is labelled (see
`review_deltas`), which is what lets `needs_review` and `false_alarms` be tiles
too. Those counters are the application's addition to the contract's list, and
so are `stage:<stage>`, `hook_mode:<mode>`, `last_seen` and `last:<rule_key>`.
"""
from __future__ import annotations

import datetime
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional

from threefold.application import projects as stages
from threefold.application.rule_keys import GATE_KEYS, NONE

LABELS = ("correct", "false_alarm")


def _sum(rollups: Iterable[Mapping[str, Any]], name: str) -> int:
    return sum(int(item.get(name, 0) or 0) for item in rollups)


def _prefixed(rollups: Iterable[Mapping[str, Any]], prefix: str) -> Counter:
    """Every counter under a prefix, summed, keyed by what follows the prefix."""
    found: Counter = Counter()
    for item in rollups:
        for name, value in item.items():
            if isinstance(name, str) and name.startswith(prefix):
                found[name[len(prefix):]] += int(value or 0)
    return found


def _latest(rollups: Iterable[Mapping[str, Any]], name: str) -> Optional[str]:
    stamps = [str(item[name]) for item in rollups if item.get(name)]
    return max(stamps) if stamps else None


def _ranked(counts: Counter, label: str) -> List[Dict[str, Any]]:
    return [
        {label: name, "calls": count}
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if count > 0
    ]


def needs_review(rollups: Iterable[Mapping[str, Any]]) -> int:
    """Calls a rule would have refused that nobody has labelled yet."""
    items = list(rollups)
    return max(0, _sum(items, "observed") - _sum(items, "reviewed:observed"))


def review_deltas(kind: str, key: str, old: Optional[str], new: Optional[str]) -> Dict[str, int]:
    """What a label moving from `old` to `new` changes in its day's counters.

    `review:<label>` counts labels of any flagged call; `<label>:<key>` counts
    them per rule; `reviewed:observed` and `reviewed:<key>` count only calls
    that were observed rather than refused, because those are the queue.
    """
    deltas: Counter = Counter()
    for label, sign in ((old, -1), (new, 1)):
        if label not in LABELS:
            continue
        deltas[f"review:{label}"] += sign
        deltas[f"{label}:{key}"] += sign
        if kind == "observed":
            deltas["reviewed:observed"] += sign
            deltas[f"reviewed:{key}"] += sign
    return {name: value for name, value in deltas.items() if value}


def _window(days: int, today: datetime.date) -> List[str]:
    return [str(today - datetime.timedelta(days=offset)) for offset in range(days - 1, -1, -1)]


def _is_expired_sandbox(project: str, configs: Mapping[str, Any]) -> bool:
    """A sandbox is gone a day after it was made; its counts stay out of the lists."""
    return project not in configs and bool(stages.SANDBOX_PATTERN.match(project))


def _projects_in(rollups: List[Mapping[str, Any]], configs: Mapping[str, Any], only: Optional[str]) -> List[str]:
    names = {str(item.get("project")) for item in rollups if item.get("project")} | set(configs)
    if only is not None:
        names = {only}
    return sorted(name for name in names if not _is_expired_sandbox(name, configs))


def _project_totals(name: str, rollups: List[Mapping[str, Any]], configs: Mapping[str, Any]) -> Dict[str, Any]:
    own = [item for item in rollups if item.get("project") == name]
    config = configs.get(name)
    return {
        "project": name,
        "stage": stages.stage_of(config),
        "configured": config is not None,
        "calls": _sum(own, "calls"),
        "refused": _sum(own, "refused"),
        "would_refuse": _sum(own, "observed"),
        "needs_review": needs_review(own),
        "last_seen": _latest(own, "last_seen"),
    }


def overview(
    rollups: List[Mapping[str, Any]],
    configs: Mapping[str, Mapping[str, Any]],
    days: int,
    project: Optional[str] = None,
    today: Optional[datetime.date] = None,
) -> Dict[str, Any]:
    """GET /api/overview: tiles, a daily series, and totals by agent, origin, rule and project."""
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    names = _projects_in(rollups, configs, project)
    shown = [item for item in rollups if item.get("project") in set(names)]
    by_day: Dict[str, List[Mapping[str, Any]]] = {}
    for item in shown:
        by_day.setdefault(str(item.get("day")), []).append(item)
    agents = _prefixed(shown, "agent:")
    refused_by_rule = _prefixed(shown, "refused:")
    observed_by_rule = _prefixed(shown, "observed:")
    by_project = [_project_totals(name, shown, configs) for name in names]
    stage_counts = Counter(row["stage"] for row in by_project)
    return {
        "window_days": days,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "rollups",
        "totals": {
            "calls": _sum(shown, "calls"),
            "approved": _sum(shown, "approved"),
            "refused": _sum(shown, "refused"),
            "would_refuse": _sum(shown, "observed"),
            "needs_review": needs_review(shown),
            "false_alarms": _sum(shown, "review:false_alarm"),
            "projects": len(by_project),
            "agents": sum(1 for count in agents.values() if count > 0),
        },
        "series": [
            {
                "day": day,
                "approved": _sum(by_day.get(day, []), "approved"),
                "observed": _sum(by_day.get(day, []), "observed"),
                "refused": _sum(by_day.get(day, []), "refused"),
            }
            for day in _window(days, today)
        ],
        "by_agent": _ranked(agents, "agent"),
        "by_origin": _ranked(_prefixed(shown, "origin:"), "origin"),
        "by_rule": [
            {"rule_key": key, "refused": refused_by_rule[key], "would_refuse": observed_by_rule[key]}
            for key in sorted(
                set(refused_by_rule) | set(observed_by_rule),
                key=lambda k: (-(refused_by_rule[k] + observed_by_rule[k]), k),
            )
            if key != NONE
        ],
        "by_project": sorted(by_project, key=lambda row: (-row["calls"], row["project"])),
        "stages": {stages.OBSERVE: stage_counts.get(stages.OBSERVE, 0), stages.ENFORCE: stage_counts.get(stages.ENFORCE, 0)},
    }


def projects_listing(rollups: List[Mapping[str, Any]], configs: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """GET /api/projects: every configured project, and every project the rollups saw."""
    listed = []
    for name in _projects_in(rollups, configs, None):
        own = [item for item in rollups if item.get("project") == name]
        config = configs.get(name) or {}
        row = _project_totals(name, rollups, configs)
        row.update(
            observe_rules=list(config.get("observe_rules") or []),
            created_at=config.get("created_at"),
            promoted_at=config.get("promoted_at"),
            agents=[entry["agent"] for entry in _ranked(_prefixed(own, "agent:"), "agent")],
            hook_modes=[entry["hook_mode"] for entry in _ranked(_prefixed(own, "hook_mode:"), "hook_mode")],
        )
        listed.append(row)
    return sorted(listed, key=lambda row: (row["last_seen"] or "", row["project"]), reverse=True)


def _state(false_alarms: int, unreviewed: int, flagged: int) -> str:
    if false_alarms:
        return "noisy"
    if unreviewed:
        return "needs_review"
    return "ready" if flagged else "quiet"


def _recommendation(state: str, mode: str, unreviewed: int, false_alarms: int) -> str:
    enforcing = mode == stages.ENFORCE
    if state == "noisy":
        return (
            f"Enforcing with {false_alarms} false alarm(s): demote, or keep this rule observing while it is refined."
            if enforcing
            else f"{false_alarms} false alarm(s): keep it observing, or refine the rule, before enforcing it."
        )
    if state == "needs_review":
        return f"{unreviewed} flagged call(s) not reviewed: mark each correct or false alarm before deciding."
    if state == "ready":
        return (
            "Enforcing, and every call it flagged was marked correct."
            if enforcing
            else "Every call it flagged was marked correct: ready to enforce."
        )
    return (
        "Enforcing, and it flagged nothing in this window."
        if enforcing
        else "Flagged nothing in this window, so enforcing it would have refused nothing seen here."
    )


def readiness(
    rollups: List[Mapping[str, Any]],
    config: Optional[Mapping[str, Any]],
    layering_rules: List[Mapping[str, Any]],
) -> Dict[str, Any]:
    """GET /api/projects/<name>: whether each rule has earned enforcement.

    A rule's calls are the ones its key flagged in the window. `would_refuse`
    counts the observed ones; a refusal it made while already enforcing counts
    towards whether it flagged anything, and a label on one counts like any
    label, so a promoted rule that turns out noisy says so.
    """
    stage = stages.stage_of(config)
    rule_by_id = {str(rule.get("id")): rule for rule in layering_rules if rule.get("id")}
    rows = []
    for key in stages.project_rule_keys(layering_rules):
        observed = _sum(rollups, f"observed:{key}")
        refused = _sum(rollups, f"refused:{key}")
        correct = _sum(rollups, f"correct:{key}")
        false_alarms = _sum(rollups, f"false_alarm:{key}")
        unreviewed = max(0, observed - _sum(rollups, f"reviewed:{key}"))
        mode = stages.mode_now(key, stage, config, rule_by_id.get(key))
        state = _state(false_alarms, unreviewed, observed + refused)
        rows.append(
            {
                "rule_key": key,
                "kind": "gate" if key in GATE_KEYS and key not in rule_by_id else "layering",
                "mode_now": mode,
                "would_refuse": observed,
                "correct": correct,
                "false_alarms": false_alarms,
                "unreviewed": unreviewed,
                "last_seen": _latest(rollups, f"last:{key}"),
                "state": state,
                "recommendation": _recommendation(state, mode, unreviewed, false_alarms),
            }
        )
    reviewed = _sum(rollups, "review:correct") + _sum(rollups, "review:false_alarm")
    false_alarms = _sum(rollups, "review:false_alarm")
    states = Counter(row["state"] for row in rows)
    observed_days = {str(item.get("day")) for item in rollups if int(item.get("stage:observe", 0) or 0) > 0}
    return {
        "summary": {
            "stage": stage,
            "days_observed": len(observed_days),
            "calls_observed": _sum(rollups, "stage:observe"),
            "would_have_refused": _sum(rollups, "observed"),
            "reviewed": reviewed,
            "false_alarms": false_alarms,
            "false_alarm_rate": round(false_alarms / reviewed, 4) if reviewed else 0.0,
            "rules_ready": states.get("ready", 0),
            "rules_quiet": states.get("quiet", 0),
            "rules_noisy": states.get("noisy", 0),
            "rules_needing_review": states.get("needs_review", 0),
        },
        "rules": rows,
    }
