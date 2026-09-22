"""Turns benchmark rows into docs/evidence/BENCHMARK_<date>.md: the method, the numbers and their limits.

    python benchmark/report.py benchmark/results/<run-id>.jsonl [more.jsonl ...]

Every number in the report, the headline sentence included, is computed from
the rows given; nothing is typed in. A run whose agent never reached the model
(an expired login, a bad flag) or whose harness failed is listed with its
reason and left out of every rate, because counting it as "no violation" would
flatter whichever condition it happened to fall in. Rows from the scripted
stand-in agent are shown only in their own section, as a test of the harness,
and never enter a rate or the headline. When every real-agent row is labelled a
pilot, so are the report's title, file name and headline.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import statistics
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark import task_library  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence"
CONDITION_ORDER = ("none", "prompt", "threefold", "prompt+threefold")
CONDITION_LABELS = {
    "none": "no guidance",
    "prompt": "rules in CLAUDE.md",
    "threefold": "Threefold enforcing",
    "prompt+threefold": "rules in CLAUDE.md and Threefold enforcing",
}


# --- reading ---------------------------------------------------------------------

def load_rows(paths: Iterable[Path]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in paths:
        for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError as error:
                    raise ValueError(f"{path}:{number} is not JSON: {error}") from None
    return rows


def is_scripted(row: Mapping[str, Any]) -> bool:
    return row.get("agent") == "scripted"


def is_valid(row: Mapping[str, Any]) -> bool:
    """A run that measured something: the agent reached the model, the harness did its part, and a
    Threefold run really had Threefold in front of it."""
    return (bool(row.get("agent_ran")) and not row.get("harness_error") and row.get("acceptance_passed") is not None
            and not row.get("hook_missing"))


def invalid_reason(row: Mapping[str, Any]) -> str:
    if row.get("harness_error"):
        return f"harness: {row['harness_error']}"
    if not row.get("agent_ran"):
        return f"agent did not run: {row.get('agent_error') or 'no output'}"
    if row.get("hook_missing"):
        return (f"the Threefold hook never fired although the agent made {row.get('governed_calls')} governed call(s); "
                "Claude Code did not load it, so this run did not measure Threefold")
    return "not judged"


# --- statistics ------------------------------------------------------------------

def wilson(successes: int, trials: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    """The Wilson score interval: honest at small n and at 0% or 100%, where the normal one is not."""
    if trials == 0:
        return None, None
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def _mean(values: Sequence[float]) -> Optional[float]:
    values = [value for value in values if value is not None]
    return statistics.fmean(values) if values else None


def _median(values: Sequence[float]) -> Optional[float]:
    values = [value for value in values if value is not None]
    return statistics.median(values) if values else None


def _seconds(row: Mapping[str, Any]) -> Optional[float]:
    if row.get("duration_ms") is not None:
        return float(row["duration_ms"]) / 1000.0
    return row.get("wall_seconds")


def _tokens(row: Mapping[str, Any]) -> Optional[int]:
    parts = [row.get(name) for name in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    if all(part is None for part in parts):
        return None
    return sum(int(part or 0) for part in parts)


def rate(successes: int, trials: int) -> Dict[str, Any]:
    low, high = wilson(successes, trials)
    return {"k": successes, "n": trials, "rate": (successes / trials) if trials else None, "ci_low": low, "ci_high": high}


def condition_stats(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    violations = sum(1 for row in rows if row.get("violation_landed"))
    passed = sum(1 for row in rows if row.get("acceptance_passed"))
    clean_done = sum(1 for row in rows if row.get("acceptance_passed") and not row.get("violation_landed"))
    refused_runs = [row for row in rows if row.get("refused_at_least_once")]
    return {
        "n": n,
        "violation": rate(violations, n),
        "completion": rate(passed, n),
        "clean_completion": rate(clean_done, n),
        "refused_runs": len(refused_runs),
        "self_corrected": sum(1 for row in refused_runs if row.get("self_corrected")),
        "gave_up": sum(1 for row in refused_runs if row.get("gave_up_after_refusal")),
        "refusals_per_run": _mean([float(row.get("hook_refusals") or 0) for row in rows]) if n else None,
        "refusal_kinds": dict(sum((Counter(row.get("hook_refusals_by_kind") or {}) for row in rows), Counter())),
        "tests_modified": sum(1 for row in rows if row.get("acceptance_tests_modified")),
        "timed_out": sum(1 for row in rows if row.get("agent_timed_out")),
        "turns_mean": _mean([row.get("num_turns") for row in rows]),
        "turns_median": _median([row.get("num_turns") for row in rows]),
        "seconds_mean": _mean([_seconds(row) for row in rows]),
        "seconds_median": _median([_seconds(row) for row in rows]),
        "cost_mean": _mean([row.get("cost_usd") for row in rows]),
        "cost_total": sum(float(row.get("cost_usd") or 0) for row in rows),
        "tokens_mean": _mean([_tokens(row) for row in rows]),
        "outside_rules_runs": sum(1 for row in rows if row.get("outside_rules")),
    }


def _ordered_conditions(names: Iterable[str]) -> List[str]:
    names = set(names)
    return [name for name in CONDITION_ORDER if name in names] + sorted(names - set(CONDITION_ORDER))


def aggregate(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    real = [row for row in rows if not is_scripted(row)]
    scripted = [row for row in rows if is_scripted(row)]
    valid = [row for row in real if is_valid(row)]
    invalid = [row for row in real if not is_valid(row)]
    conditions = _ordered_conditions(row["condition"] for row in real)
    by_condition = OrderedDict((name, condition_stats([row for row in valid if row["condition"] == name])) for name in conditions)
    tasks = sorted({row["task"] for row in real})
    per_task = {
        task: {name: condition_stats([row for row in valid if row["task"] == task and row["condition"] == name]) for name in conditions}
        for task in tasks
    }
    scripted_conditions = _ordered_conditions(row["condition"] for row in scripted)
    return {
        "rows": len(rows),
        "real_rows": len(real),
        "valid_rows": len(valid),
        "pilot": bool(real) and all(row.get("pilot") for row in real) or (not real and bool(scripted) and all(row.get("pilot") for row in scripted)),
        "models": sorted({str(row.get("model")) for row in real}),
        "claude_versions": sorted({str(row.get("claude_code_version")) for row in valid if row.get("claude_code_version")}),
        "isolation_modes": sorted({str((row.get("isolation") or {}).get("mode")) for row in real}),
        "platforms": sorted({str((row.get("harness") or {}).get("platform")) for row in real if row.get("harness")}),
        "run_ids": sorted({str(row.get("run_id")) for row in rows}),
        "dates": sorted({str(row.get("started_at", ""))[:10] for row in rows if row.get("started_at")}),
        "conditions": conditions,
        "by_condition": by_condition,
        "tasks": tasks,
        "per_task": per_task,
        "invalid": [{"task": row.get("task"), "condition": row.get("condition"), "rep": row.get("rep"), "reason": invalid_reason(row)} for row in invalid],
        "invalid_reasons": dict(Counter(invalid_reason(row) for row in invalid)),
        "scripted": OrderedDict((name, condition_stats([row for row in scripted if row["condition"] == name])) for name in scripted_conditions),
        "scripted_rows": len(scripted),
    }


# --- words -----------------------------------------------------------------------

def pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{100 * value:.0f}%"


def fraction(stat: Mapping[str, Any]) -> str:
    if not stat["n"]:
        return "n/a"
    return f"{pct(stat['rate'])} ({stat['k']}/{stat['n']})"


def with_ci(stat: Mapping[str, Any]) -> str:
    if not stat["n"]:
        return "n/a"
    return f"{fraction(stat)}, 95% CI {pct(stat['ci_low'])}–{pct(stat['ci_high'])}"


def headline(summary: Mapping[str, Any]) -> str:
    """One sentence, computed. It says why when the data cannot carry one."""
    prefix = "PILOT, not a result: " if summary["pilot"] else ""
    needed = ("none", "prompt", "threefold")
    stats = summary["by_condition"]
    missing = [name for name in needed if name not in stats or not stats[name]["n"]]
    if missing:
        if summary["real_rows"] and not summary["valid_rows"]:
            reasons = "; ".join(f"{reason} ({count} run(s))" for reason, count in summary["invalid_reasons"].items())
            return f"{prefix}No headline: none of the {summary['real_rows']} real-agent run(s) produced a measurement. {reasons}."
        if not summary["real_rows"]:
            return f"{prefix}No headline: there are no real-agent runs in these results."
        return f"{prefix}No headline: no valid runs yet under {', '.join(CONDITION_LABELS[name] for name in missing)}."
    models = ", ".join(summary["models"])
    n = sum(stats[name]["n"] for name in needed)
    return (
        f"{prefix}Across {n} runs of {models} on {len(summary['tasks'])} Acme task(s), a governed violation landed in "
        f"{fraction(stats['none']['violation'])} of runs with no guidance and {fraction(stats['prompt']['violation'])} "
        f"with the rules in CLAUDE.md, against {fraction(stats['threefold']['violation'])} with Threefold enforcing; "
        f"the acceptance tests passed in {fraction(stats['none']['completion'])}, "
        f"{fraction(stats['prompt']['completion'])} and {fraction(stats['threefold']['completion'])} of those runs respectively."
    )


def _number(value: Optional[float], digits: int = 1, unit: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}{unit}"


def _ratio(value: Optional[float], base: Optional[float]) -> str:
    if value is None or not base:
        return "n/a"
    return f"{value / base:.2f}x"


def caveats(summary: Mapping[str, Any]) -> List[str]:
    stats = summary["by_condition"]
    smallest = min((stat["n"] for stat in stats.values()), default=0)
    cell_sizes = [stat["n"] for task in summary["per_task"].values() for stat in task.values()]
    items = [
        f"Small samples. The smallest condition has {smallest} valid run(s) and a task-by-condition cell holds at most "
        f"{max(cell_sizes, default=0)}; the 95% intervals above are wide and differences inside them are not established.",
        f"One model family per run set ({', '.join(summary['models']) or 'none'}), one agent (Claude Code "
        f"{', '.join(summary['claude_versions']) or 'version unknown'}), on {', '.join(summary['platforms']) or 'an unrecorded platform'}. "
        "Other agents and models may behave differently; Codex and Antigravity are not measured here.",
        "The tasks were written by the people who built Threefold, to tempt exactly the violations its shipped rules cover. "
        "The violation rates are rates under temptation, not base rates of everyday work, and a task set chosen by someone else could favour a condition differently.",
        "The Threefold condition does not give the agent the rules in advance: it learns them from refusals. The prompt condition "
        "gives them in CLAUDE.md and nothing enforces them. Teams would normally use both; `prompt+threefold` measures that and is not in the default matrix.",
        "A violation is what the benchmark's own checkers find in the files the agent left behind (and, for the staging key, anywhere in git history). "
        "They restate the shipped rules independently and read more than the engine does (dynamic imports, fully qualified or implicitly imported C# types), "
        "so a violation Threefold did not catch still counts against it. Input and output the rules do not name (a standard-library socket in the domain) is reported separately and never counted.",
        "Completion is the task's own acceptance tests, restored from the template before they run, so an agent cannot pass by editing them. It does not grade code quality.",
        "Cost and tokens are Claude Code's own figures from its JSON output. Under a subscription the cost is an estimate of API list price, not money spent.",
    ]
    modes = summary["isolation_modes"]
    if "user-config" in modes:
        items.append(
            "Isolation was partial in some runs (user-config): the agent used the owner's Claude Code configuration folder with "
            "`--setting-sources project,local`, which keeps user settings and hooks out, and `--strict-mcp-config` and "
            "`--disable-slash-commands`, which keep MCP servers and skills out. Whether the owner's user-level CLAUDE.md was also kept "
            "out was not verified. Runs with a token in CLAUDE_CODE_OAUTH_TOKEN use a fresh configuration folder (fresh-config) and exclude it."
        )
    if "fresh-config" in modes:
        items.append(
            "fresh-config runs used a configuration folder created for the run, so no user-level CLAUDE.md, settings, hooks, skills, "
            "agents or memory from the owner's machine were loaded."
        )
    items.append(
        "Every run's work stays on the machine: the repository is a temporary copy outside the workspace, AWS credentials are pointed at "
        "files that do not exist, installs, network tools and pushes are refused by the permission list, and the Threefold server is a local, offline process for that run alone."
    )
    return items


def _task_table(summary: Mapping[str, Any], tasks_by_id: Mapping[str, task_library.Task]) -> List[str]:
    conditions = summary["conditions"]
    lines = ["| Task | Language | Governed by | " + " | ".join(CONDITION_LABELS.get(name, name) for name in conditions) + " |",
             "|---|---|---|" + "---|" * len(conditions)]
    for task_id in summary["tasks"]:
        task = tasks_by_id.get(task_id)
        cells = []
        for name in conditions:
            stat = summary["per_task"][task_id][name]
            if not stat["n"]:
                cells.append("n/a")
                continue
            cells.append(f"{stat['violation']['k']}/{stat['n']} violated · {stat['completion']['k']}/{stat['n']} passed")
        language = task.language if task else "?"
        governed = ", ".join(task.governed_by) if task else "?"
        lines.append(f"| `{task_id}` | {language} | {governed} | " + " | ".join(cells) + " |")
    return lines


def render(summary: Mapping[str, Any], tasks: Sequence[task_library.Task], sources: Sequence[str]) -> str:
    tasks_by_id = {task.id: task for task in tasks}
    pilot = summary["pilot"]
    date = summary["dates"][-1] if summary["dates"] else datetime.date.today().isoformat()
    title = f"# Agent benchmark{' — PILOT' if pilot else ''}, {date}"
    out: List[str] = [title, ""]
    if pilot:
        out += ["> **PILOT.** These rows prove the harness end to end. They are not a result, and no number below should be quoted as one.", ""]
    out += ["## Headline", "", headline(summary), ""]

    out += ["## Method", "",
            "Each run gives Claude Code, headless (`claude -p`), one task in a fresh temporary copy of a small synthetic Acme repository "
            "and lets it work until it stops, runs out of turns or times out. The same task and prompt run under each condition:", ""]
    for name in summary["conditions"] or ["none", "prompt", "threefold"]:
        description = {
            "none": "the repository as it is: a README describing the layout, no rules.",
            "prompt": "the team's rules (the shipped Threefold rules, in prose) in the repository's `CLAUDE.md`. Nothing enforces them.",
            "threefold": "the Threefold hook installed in the repository's `.claude/settings.local.json` in enforce mode, talking to a local "
                         "Threefold server started from this repository's source for that run (`THREEFOLD_OFFLINE=1`, `DEFAULT_HOOK_STAGE=enforce`). No `CLAUDE.md`.",
            "prompt+threefold": "both of the above.",
        }.get(name, "")
        out.append(f"- **{CONDITION_LABELS.get(name, name)}** (`{name}`): {description}")
    out += ["",
            "After the agent stops, an independent checker (`benchmark/checks.py`, which does not import Threefold) reads the files it left behind "
            "for a governed violation, then the task's acceptance tests are restored from the template and run. A refusal is counted from the "
            "agent's own transcript (the hook refuses a credential on the machine, so the server never sees it) and cross-checked with the local "
            "server's ledger (`/api/insights`). A run **self-corrected** when it was refused at least once and still finished with passing tests and no violation.",
            "", "The tasks:", "", "| Task | Language | Governed by | The temptation |", "|---|---|---|---|"]
    for task in tasks:
        if not summary["tasks"] or task.id in summary["tasks"]:
            out.append(f"| `{task.id}` | {task.language} | {', '.join(task.governed_by)} | {task.temptation} |")
    out.append("")

    out += ["## Results by condition", ""]
    if not summary["valid_rows"]:
        out += ["No valid real-agent runs. See *Runs that did not measure anything* below.", ""]
    else:
        conditions = [name for name in summary["conditions"] if summary["by_condition"][name]["n"]]
        header = "| | " + " | ".join(CONDITION_LABELS.get(name, name) for name in conditions) + " |"
        out += [header, "|---|" + "---|" * len(conditions)]

        def row(label: str, render_cell) -> None:
            out.append(f"| {label} | " + " | ".join(render_cell(summary["by_condition"][name]) for name in conditions) + " |")

        row("Valid runs", lambda s: str(s["n"]))
        row("Violation landed", lambda s: with_ci(s["violation"]))
        row("Acceptance tests passed", lambda s: with_ci(s["completion"]))
        row("Passed with no violation", lambda s: with_ci(s["clean_completion"]))
        row("Runs refused at least once", lambda s: str(s["refused_runs"]))
        row("… of which self-corrected", lambda s: f"{s['self_corrected']}/{s['refused_runs']}" if s["refused_runs"] else "n/a")
        row("… of which gave up (tests failing)", lambda s: f"{s['gave_up']}/{s['refused_runs']}" if s["refused_runs"] else "n/a")
        row("Refusals per run", lambda s: _number(s["refusals_per_run"], 2))
        row("Refusals by gate", lambda s: ", ".join(f"{k} {v}" for k, v in sorted(s["refusal_kinds"].items())) or "none")
        row("Turns (mean / median)", lambda s: f"{_number(s['turns_mean'])} / {_number(s['turns_median'])}")
        row("Seconds (mean / median)", lambda s: f"{_number(s['seconds_mean'], 0)} / {_number(s['seconds_median'], 0)}")
        row("Cost per run, USD (mean)", lambda s: _number(s["cost_mean"], 3))
        row("Tokens per run (mean, incl. cache)", lambda s: _number(s["tokens_mean"], 0))
        row("Acceptance files edited by the agent", lambda s: str(s["tests_modified"]))
        row("Timed out", lambda s: str(s["timed_out"]))
        row("Runs with I/O no rule names (not counted)", lambda s: str(s["outside_rules_runs"]))
        out.append("")
        base = summary["by_condition"].get("none")
        if base and base["n"] and "threefold" in summary["by_condition"] and summary["by_condition"]["threefold"]["n"]:
            tf = summary["by_condition"]["threefold"]
            out += ["**Overhead of Threefold against no guidance** (ratio of means): "
                    f"turns {_ratio(tf['turns_mean'], base['turns_mean'])}, time {_ratio(tf['seconds_mean'], base['seconds_mean'])}, "
                    f"cost {_ratio(tf['cost_mean'], base['cost_mean'])}.", ""]
        out += ["## Results by task", ""] + _task_table(summary, tasks_by_id) + [""]

    if summary["invalid"]:
        out += ["## Runs that did not measure anything", "",
                "Left out of every rate above. Counting them as clean would flatter whichever condition they fell in.", "",
                "| Task | Condition | Rep | Reason |", "|---|---|---|---|"]
        for item in summary["invalid"]:
            out.append(f"| `{item['task']}` | {item['condition']} | {item['rep']} | {item['reason']} |")
        out.append("")

    if summary["scripted_rows"]:
        out += ["## Harness self-test (scripted agent, not a measurement)", "",
                "`benchmark/scripted_agent.py` stands in for Claude Code with a fixed script: it writes the task's violating reference, asking the "
                "installed hook before every call as Claude Code does, and switches to the clean reference only if a call is refused. Its rows show "
                "that the set-up, the hook, the local server, the ledger, the checkers and the acceptance run work together on this machine. "
                "They say nothing about how an agent behaves.", "",
                "| Condition | Runs | Violation landed | Tests passed | Refused at least once | Self-corrected |", "|---|---|---|---|---|---|"]
        for name, stat in summary["scripted"].items():
            out.append(f"| {CONDITION_LABELS.get(name, name)} | {stat['n']} | {fraction(stat['violation'])} | {fraction(stat['completion'])} | "
                       f"{stat['refused_runs']} | {stat['self_corrected']} |")
        out.append("")

    out += ["## Limits", ""] + [f"- {item}" for item in caveats(summary)] + [""]
    out += ["## Reproduce", "",
            "```",
            "python benchmark/run.py --reps 3 --parallel 3        # the full matrix: 6 tasks x 3 conditions x 3 reps",
            "python benchmark/report.py benchmark/results/<run-id>.jsonl",
            "```", "",
            "Source rows: " + ", ".join(f"`{source}`" for source in sources) + ". Run ids: " + ", ".join(summary["run_ids"]) + ".", ""]
    return "\n".join(out)


def default_output(summary: Mapping[str, Any]) -> Path:
    date = summary["dates"][-1] if summary["dates"] else datetime.date.today().isoformat()
    return EVIDENCE_DIR / f"BENCHMARK_{date}{'-PILOT' if summary['pilot'] else ''}.md"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate benchmark rows into a report.")
    parser.add_argument("results", nargs="+", type=Path, help="one or more benchmark/results/*.jsonl files")
    parser.add_argument("--out", type=Path, default=None, help="default: docs/evidence/BENCHMARK_<date>[-PILOT].md")
    args = parser.parse_args(argv)
    rows = load_rows(args.results)
    summary = aggregate(rows)
    tasks = task_library.load_tasks()
    sources = []
    for path in args.results:
        try:
            sources.append(Path(path).resolve().relative_to(REPO_ROOT).as_posix())
        except ValueError:
            sources.append(Path(path).name)
    output = args.out or default_output(summary)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(summary, tasks, sources), encoding="utf-8", newline="\n")
    print(headline(summary))
    print(f"written: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
