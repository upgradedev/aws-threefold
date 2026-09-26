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
from threefold.application.labels import is_labelled, project_label
from threefold.application.rule_keys import GATE_KEYS, NONE

LABELS = ("correct", "false_alarm")

# What each value of a call's `agent` is, so a page can list the coding agents
# being governed apart from everything else that calls the service: `page` is a
# visitor pressing a button on a page here, `ci` a pipeline or a pre-commit
# check, and `unknown` a row written before the field existed. A value not
# listed here is `unknown` too.
CODING_AGENT = "coding_agent"
AGENT_KINDS = {
    "claude-code": CODING_AGENT,
    "codex": CODING_AGENT,
    "antigravity": CODING_AGENT,
    "page": "page",
    "ci": "ci",
    "pre-commit": "ci",
}
UNKNOWN_KIND = "unknown"


def agent_kind(agent: Any) -> str:
    """coding_agent, page, ci or unknown: what a by_agent entry is."""
    return AGENT_KINDS.get(str(agent or ""), UNKNOWN_KIND)


def is_sandbox(project: Any) -> bool:
    """Whether a project is a visitor's sandbox, `Acme-Sandbox-<8 hex>`, however it was made."""
    return bool(stages.SANDBOX_PATTERN.match(str(project or "")))


# Where a project's calls come from, so a page can say which numbers are
# synthetic. `fleet` is the synthetic Acme fleet that application/demo_fleet.py
# runs on the public stack, `sandbox` a visitor's sandbox, and `other`
# everything else: the service's own probes, the demo's page calls, and on a
# private stack the governed repositories themselves. The fleet's projects are
# these six names exactly; `Acme-Ledger-2` or `Acme-Payments-Internal` is other.
FLEET = "fleet"
SANDBOX = "sandbox"
OTHER = "other"
SOURCES = (FLEET, SANDBOX, OTHER)
FLEET_PROJECTS = (
    "Acme-Payments",
    "Acme-Checkout",
    "Acme-Ledger",
    "Acme-Search",
    "Acme-Mobile",
    "Acme-Platform",
)


def source_of(project: Any) -> str:
    """fleet, sandbox or other: where a project's calls come from, read off its name."""
    if is_sandbox(project):
        return SANDBOX
    if str(project or "") in FLEET_PROJECTS:
        return FLEET
    return OTHER


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


def review_deltas(kind: str, key: Any, old: Optional[str], new: Optional[str]) -> Dict[str, int]:
    """What a label moving from `old` to `new` changes in its day's counters.

    `review:<label>` counts labels of any flagged call; `<label>:<key>` counts
    them per rule; `reviewed:observed` and `reviewed:<key>` count only calls
    that were observed rather than refused, because those are the queue.

    `key` is one key, or every key the call was counted under: a rollup counts
    an observation once for each rule that would have refused it, so a label
    on it has to move once for each too. The per-call counters move once
    whatever the row's key is, because the reviewer labelled one call.
    """
    names = [key] if isinstance(key, str) else [str(name) for name in key]
    deltas: Counter = Counter()
    for label, sign in ((old, -1), (new, 1)):
        if label not in LABELS:
            continue
        deltas[f"review:{label}"] += sign
        if kind == "observed":
            deltas["reviewed:observed"] += sign
        for name in dict.fromkeys(names):
            deltas[f"{label}:{name}"] += sign
            if kind == "observed":
                deltas[f"reviewed:{name}"] += sign
    return {name: value for name, value in deltas.items() if value}


def _window(days: int, today: datetime.date) -> List[str]:
    """The UTC days a window covers, oldest first.

    Whole days, because a rollup is a day's counters and nothing finer can be
    read out of one. `days=1` is therefore today's partition: at 00:05 UTC that
    is five minutes of calls, not twenty-four hours of them. The overview says
    which day it starts at (`window_from`) so that nobody has to infer it from
    the count and get it wrong.
    """
    return [str(today - datetime.timedelta(days=offset)) for offset in range(days - 1, -1, -1)]


def _as_shown(
    rollups: Iterable[Mapping[str, Any]], configs: Mapping[str, Mapping[str, Any]]
) -> tuple:
    """The rollups and configurations under the names a public page may carry.

    A rollup's project is the name its calls were recorded under, which was
    labelled when it was written. The pattern is a stack parameter, though, and
    a name it admitted yesterday may not be admitted today: such a name is
    shown as "unlabelled" and its counts merge there, as a ledger row's would.
    A configuration under a name the pattern no longer admits applies to no
    call, so it is not listed.
    """
    shown = [dict(item, project=project_label(item.get("project"))) for item in rollups]
    live = {name: config for name, config in configs.items() if is_labelled(name)}
    return shown, live


def _is_expired_sandbox(project: str, configs: Mapping[str, Any]) -> bool:
    """A sandbox is gone a day after it was made; its counts stay out of the lists."""
    return project not in configs and is_sandbox(project)


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
        "sandbox": is_sandbox(name),
        "source": source_of(name),
        "calls": _sum(own, "calls"),
        "refused": _sum(own, "refused"),
        "would_refuse": _sum(own, "observed"),
        "needs_review": needs_review(own),
        "last_seen": _latest(own, "last_seen"),
    }


def _by_agent(agents: Counter, in_sandboxes: Counter) -> List[Dict[str, Any]]:
    """by_agent: each agent's calls, what kind of caller it is, and how many of its calls were in sandboxes.

    A sandbox is seeded with synthetic calls from every coding agent, so a
    coding agent can appear here from the seeding alone; `calls_in_sandboxes`
    lets a page say so.
    """
    return [
        dict(entry, kind=agent_kind(entry["agent"]), calls_in_sandboxes=in_sandboxes.get(entry["agent"], 0))
        for entry in _ranked(agents, "agent")
    ]


def _sources(rows: List[Mapping[str, Any]], rollups: List[Mapping[str, Any]]) -> Dict[str, Dict[str, int]]:
    """sources: the calls and the projects of the window by where they come from.

    Computed over the same projects as the totals, so each figure adds up to
    its total: the three `calls` to `totals.calls`, the three `projects` to
    `totals.projects`.
    """
    calls: Counter = Counter()
    for item in rollups:
        calls[source_of(item.get("project"))] += int(item.get("calls", 0) or 0)
    projects = Counter(row["source"] for row in rows)
    return {name: {"calls": calls.get(name, 0), "projects": projects.get(name, 0)} for name in SOURCES}


def _part_totals(rows: List[Mapping[str, Any]], rollups: List[Mapping[str, Any]], sandbox: bool) -> Dict[str, Any]:
    """The overview's totals over one side of the sandbox split, computed as the totals are."""
    own = [item for item in rollups if is_sandbox(item.get("project")) == sandbox]
    return {
        "projects": len(rows),
        "calls": _sum(own, "calls"),
        "approved": _sum(own, "approved"),
        "refused": _sum(own, "refused"),
        "would_refuse": _sum(own, "observed"),
        "needs_review": needs_review(own),
        "false_alarms": _sum(own, "review:false_alarm"),
    }


def overview(
    rollups: List[Mapping[str, Any]],
    configs: Mapping[str, Mapping[str, Any]],
    days: int,
    project: Optional[str] = None,
    today: Optional[datetime.date] = None,
) -> Dict[str, Any]:
    """GET /api/overview: tiles, a daily series, and totals by agent, origin, rule and project.

    Beside the totals, which keep their meaning, `sandbox_split` gives the same
    totals for visitors' sandboxes and for every other project, each by_agent
    entry carries its `kind` and its `calls_in_sandboxes`, and `coding_agents`
    lists the coding agents alone, so a page can say what each number is made
    of. `sources` gives the calls and projects of the synthetic fleet, of
    sandboxes and of everything else, and each by_project row names its
    `source`. A sandbox whose configuration has expired stays out of all of them.
    """
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    days_covered = _window(days, today)
    rollups, configs = _as_shown(rollups, configs)
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
    by_agent = _by_agent(agents, _prefixed([item for item in shown if is_sandbox(item.get("project"))], "agent:"))
    coding_agents = [entry for entry in by_agent if entry["kind"] == CODING_AGENT]
    return {
        "window_days": days,
        # The first UTC day these numbers cover. `window_days` alone reads as a
        # rolling window, and it is not one: a day is the smallest thing a
        # rollup holds, so `days=1` is today since 00:00 UTC and nothing more.
        "window_from": days_covered[0],
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
            "coding_agents": len(coding_agents),
        },
        # The same totals, for the visitors' sandboxes and for every other
        # project. A public stack's review backlog is mostly sandboxes', whose
        # seeded calls include would-refuse calls on purpose; split, a page can
        # say what its number is made of rather than leave a reader to guess.
        "sandbox_split": {
            "sandbox": _part_totals([row for row in by_project if row["sandbox"]], shown, True),
            "elsewhere": _part_totals([row for row in by_project if not row["sandbox"]], shown, False),
        },
        # Where the calls come from: the synthetic fleet, visitors' sandboxes,
        # or anything else. A public page says in words that the fleet is
        # synthetic; this is the figure it says it with.
        "sources": _sources(by_project, shown),
        "series": [
            {
                "day": day,
                "approved": _sum(by_day.get(day, []), "approved"),
                "observed": _sum(by_day.get(day, []), "observed"),
                "refused": _sum(by_day.get(day, []), "refused"),
            }
            for day in days_covered
        ],
        "by_agent": by_agent,
        # Beside by_agent, the coding agents alone, in the same order and shape.
        "coding_agents": coding_agents,
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
    rollups, configs = _as_shown(rollups, configs)
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
                # Refusals it made while already enforcing. They count towards
                # whether it flagged anything, so a promoted rule that refused
                # calls is Ready; shown beside would_refuse so that state can
                # be read off the row rather than taken on trust.
                "refused": refused,
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
