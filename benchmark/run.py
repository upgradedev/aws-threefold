"""Runs the benchmark: real Claude Code sessions on the Acme tasks, under each condition, recorded as JSON lines.

    python benchmark/run.py --tasks orders-s3-archive --reps 1 --pilot
    python benchmark/run.py --reps 3 --parallel 3

Each run gets a fresh copy of the task repository under the system temp folder
(%TEMP%\\threefold-bench\\<run-id> on Windows), never inside the workspace. One
row per run is appended to benchmark/results/<run-id>.jsonl as soon as the run
ends, so an interrupted matrix keeps what it finished. Aggregate with
benchmark/report.py.

Before a real run, the machine needs a logged-in Claude Code: `claude auth
status` must say loggedIn, and a first `claude -p "hi"` must answer rather than
report an expired session. For the strongest isolation, create a long-lived
token with `claude setup-token` and export it as CLAUDE_CODE_OAUTH_TOKEN: each
run then gets a configuration folder of its own, so nothing from the owner's
user-level CLAUDE.md, settings, skills or memory reaches the agent.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark import harness, task_library  # noqa: E402

RESULTS_DIR = harness.BENCHMARK_DIR / "results"


def _csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Threefold agent benchmark.")
    parser.add_argument("--tasks", type=_csv, default=None, help="comma-separated task ids (default: all six)")
    parser.add_argument("--conditions", type=_csv, default=list(harness.DEFAULT_CONDITIONS),
                        help="comma-separated: none, prompt, threefold, prompt+threefold")
    parser.add_argument("--reps", type=int, default=3, help="repetitions of each task and condition (default 3)")
    parser.add_argument("--model", default=harness.DEFAULT_MODEL, help=f"model for claude --model (default {harness.DEFAULT_MODEL})")
    parser.add_argument("--parallel", type=int, default=1, help="runs at once (default 1)")
    parser.add_argument("--max-turns", type=int, default=harness.DEFAULT_MAX_TURNS)
    parser.add_argument("--timeout", type=int, default=harness.DEFAULT_TIMEOUT_S, help="seconds per run before it is stopped")
    parser.add_argument("--budget-usd", type=float, default=harness.DEFAULT_BUDGET_USD, help="claude --max-budget-usd per run")
    parser.add_argument("--isolation", choices=("auto", "fresh-config", "user-config"), default="auto")
    parser.add_argument("--agent", choices=("claude", "scripted"), default="claude",
                        help="scripted tests the harness with a fixed script instead of a model; its rows measure nothing")
    parser.add_argument("--claude", default=None, help="path to the claude executable (default: found on PATH)")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--pilot", action="store_true", help="label every row as a pilot, not a result")
    parser.add_argument("--work-root", type=Path, default=None, help="default: <system temp>/threefold-bench/<run-id>")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--dry-run", action="store_true", help="print the plan and the agent command, run nothing")
    args = parser.parse_args(argv)
    unknown = [condition for condition in args.conditions if condition not in harness.CONDITIONS]
    if unknown:
        parser.error(f"unknown condition(s) {', '.join(unknown)}; choose from {', '.join(harness.CONDITIONS)}")
    if args.reps < 1 or args.parallel < 1:
        parser.error("--reps and --parallel must be at least 1")
    return args


def default_run_id(pilot: bool, agent: str, now: Optional[datetime.datetime] = None) -> str:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    suffix = ("-pilot" if pilot else "") + ("-scripted" if agent == "scripted" else "")
    return now.strftime("%Y%m%dT%H%M%SZ") + suffix


def plan_runs(tasks: Sequence[task_library.Task], conditions: Sequence[str], reps: int) -> List[tuple]:
    """Every run, interleaved so that the conditions of one task and repetition sit next to each other.

    With --parallel equal to the number of conditions they then run side by
    side, at the same time of day against the same service, and a matrix cut
    short is still balanced across conditions.
    """
    return [(task, condition, rep) for rep in range(1, reps + 1) for task in tasks for condition in conditions]


def claude_version(claude: str) -> str:
    try:
        completed = subprocess.run([claude, "--version"], capture_output=True, timeout=60)
        return completed.stdout.decode("utf-8", "replace").strip()[:80]
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


class ResultsFile:
    """Appends one JSON line per run, from any thread."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, row: Dict[str, Any]) -> None:
        line = json.dumps(row, sort_keys=True, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")


def one_line(row: Dict[str, Any]) -> str:
    verdict = "VIOLATION" if row.get("violation_landed") else "clean"
    tests = "tests pass" if row.get("acceptance_passed") else "tests fail"
    extra = f", {row.get('hook_refusals') or 0} refusal(s)" if harness.uses_threefold(row["condition"]) else ""
    problem = row.get("harness_error") or ("" if row.get("agent_ran") else f"agent did not run: {row.get('agent_error')}")
    return (f"{row['task']:<26} {row['condition']:<17} r{row['rep']}  {verdict:<9} {tests}{extra}"
            f"  turns={row.get('num_turns')} cost={row.get('cost_usd')}" + (f"  [{problem}]" if problem else ""))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    tasks = task_library.load_tasks(args.tasks)
    run_id = args.run_id or default_run_id(args.pilot, args.agent)
    work_root = args.work_root or Path(tempfile.gettempdir()) / "threefold-bench" / run_id
    harness.ensure_outside_workspace(work_root)
    claude = args.claude or shutil.which("claude") or "claude"
    isolation = harness.choose_isolation(args.isolation, os.environ) if args.agent == "claude" else "user-config"
    options = harness.AgentOptions(
        agent=args.agent, claude=claude, model=args.model, max_turns=args.max_turns, timeout_s=args.timeout,
        budget_usd=args.budget_usd, isolation=isolation,
    )
    plan = harness.RunPlan(run_id=run_id, work_root=work_root, options=options, pilot=args.pilot)
    runs = plan_runs(tasks, args.conditions, args.reps)
    results = ResultsFile(Path(args.results_dir) / f"{run_id}.jsonl")

    print(f"run {run_id}: {len(runs)} run(s), {len(tasks)} task(s) x {len(args.conditions)} condition(s) x {args.reps} rep(s)")
    print(f"agent: {args.agent}" + (f" ({claude_version(claude)}, model {args.model}, isolation {isolation})" if args.agent == "claude" else ""))
    print(f"work root: {work_root}")
    print(f"results: {results.path}")
    if args.dry_run:
        command = harness.build_agent_command(options, tasks[0], Path("<run-dir>") / "agent-settings.json")
        print("command: " + " ".join(command) + "   (prompt on stdin)")
        for task, condition, rep in runs:
            print(f"  would run {task.id} / {condition} / r{rep}")
        return 0

    work_root.mkdir(parents=True, exist_ok=True)
    finished = 0
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(harness.run_one, task, condition, rep, plan): (task, condition, rep)
                   for task, condition, rep in runs}
        for future in as_completed(futures):
            row = future.result()
            results.append(row)
            finished += 1
            print(f"[{finished}/{len(runs)}] {one_line(row)}", flush=True)
    print(f"done. Aggregate with: python benchmark/report.py {results.path.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
