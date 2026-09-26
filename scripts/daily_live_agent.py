#!/usr/bin/env python3
"""Once a day, a real coding agent does one of the benchmark's Acme tasks under Threefold, on a public stack.

    python scripts/daily_live_agent.py --endpoint https://<public stack>/             # today's run
    python scripts/daily_live_agent.py --endpoint https://<public stack>/ --dry-run   # what it would run, and nothing else
    python scripts/daily_live_agent.py --endpoint https://<public stack>/ --dry-run --date 2026-10-01

The public ledger then carries real agent sessions beside the synthetic fleet.
Each run is one run of the benchmark's `threefold` condition (benchmark/run.py,
unchanged in everything else: the same task repository copied outside the
workspace, the same permission lists, the same login handling), except that
the hook in the task repository reports to the endpoint given, as the project
`Acme-Live-<task>` in the session `live-<task>-<date>`, and what the run left
there is read back from that stack's GET /api/decisions.

Which agent and which task, deterministically from the UTC date: the agent
alternates every day (Claude Code, then Codex), and the task moves on every
second day through the six standard tasks, so each task is done by both agents
on consecutive days and all twelve pairs come round every twelve days.

The row goes to benchmark/results/live/<date>-<agent>.jsonl and one line is
printed. The benchmark's own output (which names the token file's path) goes
to daily-live.log in the run's work root, outside the repository, not to the
terminal. A day that already has a row is not run again.

What it refuses, before anything is created:
- an endpoint that is not https, or that carries a user name, a password, a
  query or a fragment; there is no default endpoint;
- a work root inside this repository, its workspace or ~/.threefold: the hook
  runs only in the task repository the benchmark copies there;
- `--date` for a real run: a session is named after the day it ran.

After the run it checks that the row names exactly the endpoint, project and
session it planned, and says so if not. The public stack judges a project by
its stage there: a project it holds in Observe has its calls recorded and
none refused, and the row then says the run did not measure Threefold
enforcing (governance_problem), while its calls are still on the public ledger.

Exit codes: 0 a row was recorded as planned (or today's already was), 1 no
row, a row that does not match the plan, or a harness error, 2 refused before
running, 3 the benchmark stopped (a usage limit or a login that stopped working).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmark import harness, report, run, task_library  # noqa: E402

# The first day of the rotation: day 0 is Claude Code on the first standard task.
ROTATION_START = datetime.date(2026, 9, 27)
AGENTS = ("claude-code", "codex")
LIVE_RESULTS_DIR = harness.BENCHMARK_DIR / "results" / "live"
CONDITION = "threefold"
LOG_NAME = "daily-live.log"
# How a path that must not be printed is shown in the command a dry run prints.
TOKEN_FILE_SHOWN = "<token file>"


class Refused(Exception):
    """Why the script will not run, said before anything is created."""


@dataclass(frozen=True)
class Pick:
    """The day's run: which agent, which standard task, and the UTC day its session is named after."""

    date: str
    agent: str
    task: str

    @property
    def run_id(self) -> str:
        return f"{self.date}-{self.agent}"

    @property
    def project(self) -> str:
        return harness.live_project(self.task)

    @property
    def session(self) -> str:
        return f"live-{self.task}-{self.date}"


def standard_tasks() -> List[str]:
    """The six standard tasks, in the order of their folders, which is the order of the rotation."""
    return [task.id for task in task_library.load_tasks(family="standard")]


def pick_for(day: datetime.date, tasks: Optional[Sequence[str]] = None) -> Pick:
    """The agent alternates daily; the task moves on every second day, so both agents do each task in turn."""
    tasks = list(tasks or standard_tasks())
    index = (day - ROTATION_START).days
    return Pick(date=day.isoformat(), agent=AGENTS[index % len(AGENTS)], task=tasks[(index // len(AGENTS)) % len(tasks)])


def check_endpoint(url: Any) -> str:
    """The endpoint as the hook will be given it, or Refused: https only, and nothing in it but a place."""
    try:
        endpoint = harness.remote_endpoint(url, allow_loopback_http=False)
    except ValueError as error:
        raise Refused(str(error)) from None
    if not endpoint.startswith("https://"):
        raise Refused("the endpoint must be https")
    return endpoint


def utc_today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


def run_arguments(pick: Pick, endpoint: str, results_dir: Path, work_root: Path, token_file: Optional[Path] = None,
                  claude: Optional[str] = None, codex: Optional[str] = None,
                  retry_pause: Optional[float] = None) -> List[str]:
    """The arguments of benchmark/run.py for the day's one run of the threefold condition against the endpoint."""
    arguments = [
        "--agent", pick.agent, "--tasks", pick.task, "--conditions", CONDITION, "--reps", "1",
        "--threefold-endpoint", endpoint, "--live-date", pick.date, "--run-id", pick.run_id,
        "--results-dir", str(results_dir), "--work-root", str(work_root),
    ]
    if token_file is not None and pick.agent == "claude-code":
        arguments += ["--token-file", str(token_file)]
    if claude and pick.agent == "claude-code":
        arguments += ["--claude", claude]
    if codex and pick.agent == "codex":
        arguments += ["--codex", codex]
    if retry_pause is not None:
        arguments += ["--retry-pause", f"{retry_pause:g}"]
    return arguments


def shown_command(arguments: Sequence[str], codex_home: bool = False) -> str:
    """The command a dry run prints: the token file's path is not printed, and neither is the Codex home."""
    shown: List[str] = []
    hide_next = False
    for item in arguments:
        if hide_next:
            shown.append(TOKEN_FILE_SHOWN)
            hide_next = False
            continue
        hide_next = item == "--token-file"
        shown.append(f'"{item}"' if " " in item else item)
    prefix = "CODEX_HOME=<the Codex home given> " if codex_home else ""
    return prefix + " ".join(["python", "benchmark/run.py", *shown])


def read_rows(path: Path) -> List[Dict[str, Any]]:
    try:
        return run.ResultsFile(path).rows()
    except (OSError, ValueError):
        return []


def mismatch(row: Mapping[str, Any], pick: Pick, endpoint: str) -> Optional[str]:
    """Why a recorded row is not the run that was planned, or None."""
    wanted = {"agent": pick.agent, "task": pick.task, "condition": CONDITION, "ledger_source": "remote",
              "threefold_endpoint": endpoint, "threefold_project": pick.project}
    for field, value in wanted.items():
        if row.get(field) != value:
            return f"the row's {field} is {row.get(field)!r}, not {value!r}"
    session = str(row.get("threefold_session") or "")
    if session != pick.session and not session.startswith(pick.session + "-"):
        return f"the row's session is {session!r}, not {pick.session!r}"
    return None


@contextlib.contextmanager
def _environment(name: str, value: Optional[str]):
    """Sets one variable for the run only, when a value is given."""
    if value is None:
        yield
        return
    before = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = before


def run_live(pick: Pick, endpoint: str, results_dir: Path = LIVE_RESULTS_DIR, work_root: Optional[Path] = None,
             token_file: Optional[Path] = None, codex_home: Optional[Path] = None, claude: Optional[str] = None,
             codex: Optional[str] = None, retry_pause: Optional[float] = None) -> Tuple[int, Optional[Dict[str, Any]], str]:
    """Runs the day's one run through benchmark/run.py and returns (exit code, the row, a note).

    `endpoint` is taken as given: main() is where it is checked to be https.
    The benchmark's output goes to the log in the work root. The note is
    empty, or says why the exit code is not 0.
    """
    results = Path(results_dir) / f"{pick.run_id}.jsonl"
    earlier = read_rows(results)
    if earlier:
        return 0, earlier[-1], f"already recorded today, in {results.name}; delete that file to run again"
    work_root = Path(work_root) if work_root is not None else run.default_work_root(pick.run_id)
    try:
        harness.ensure_outside_workspace(work_root)
    except ValueError as error:
        return 2, None, f"refused: {error}"
    arguments = run_arguments(pick, endpoint, Path(results_dir), work_root, token_file, claude, codex, retry_pause)
    work_root.mkdir(parents=True, exist_ok=True)
    log = work_root / LOG_NAME
    with open(log, "a", encoding="utf-8") as handle, contextlib.redirect_stdout(handle), \
            contextlib.redirect_stderr(handle), \
            _environment("CODEX_HOME", str(codex_home) if codex_home and pick.agent == "codex" else None):
        print(f"--- {harness.utc_now()} {shown_command(arguments, bool(codex_home and pick.agent == 'codex'))}")
        try:
            code = run.main(arguments)
        except SystemExit as stop:  # argparse refusing an argument
            code = stop.code if isinstance(stop.code, int) else 2
        except Exception as error:  # noqa: BLE001 - said in the log and the line, never as a traceback on the terminal
            print(f"the benchmark failed: {type(error).__name__}: {error}")
            code = 1
    rows = read_rows(results)
    row = rows[-1] if rows else None
    if row is None:
        return (code or 1), None, f"no row was recorded (benchmark exit {code}); see {log}"
    problem = mismatch(row, pick, endpoint)
    if problem:
        return 1, row, f"the recorded row is not the run planned: {problem}"
    if row.get("harness_error"):
        return (code or 1), row, f"see {log}"
    return code, row, "" if code == 0 else f"the benchmark stopped (exit {code}); see {log}"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def summary_line(pick: Pick, row: Optional[Mapping[str, Any]], note: str = "", results: Optional[Path] = None) -> str:
    """One line: what the agent did, what the remote ledger holds for its session, and whether the row counts.

    A call a rule would have refused but that ran (the stage was Observe) is
    `would refuse`, never `refused`.
    """
    head = f"live {pick.date} {pick.agent} on {pick.task}"
    if row is None:
        return f"{head}: no row. {note}".strip()
    if row.get("harness_error"):
        outcome = f"harness error: {row['harness_error']}"
    elif not row.get("agent_ran"):
        outcome = f"the agent did not run ({row.get('agent_error') or row.get('run_end')})"
    else:
        outcome = ", ".join([
            str(row.get("run_end") or "?"),
            "violation landed" if row.get("violation_landed") else "no violation",
            "tests pass" if row.get("acceptance_passed") else "tests fail",
            f"{_plural(int(row.get('hook_refusals') or 0), 'refusal')} seen by the agent",
        ])
    ledger = row.get("ledger") or {}
    if ledger.get("reachable"):
        stages = ", ".join(f"{count} in {stage}" for stage, count in sorted((ledger.get("stages") or {}).items()))
        held = (f"the remote ledger holds {_plural(int(ledger.get('decisions') or 0), 'call')} "
                f"({int(ledger.get('refused') or 0)} refused, {int(ledger.get('would_refuse') or 0)} would refuse"
                + (f"; judged {stages}" if stages else "") + ")")
    elif ledger:
        held = "the remote ledger could not be read"
    else:
        held = "the remote ledger was not read"
    counted = "counted" if report.is_valid(row) else f"not counted: {report.invalid_reason(row)}"
    where = f"session {row.get('threefold_session') or pick.session} of {row.get('threefold_project') or pick.project}"
    line = f"{head}: {outcome}; {held}; {where}; {counted}"
    if results is not None:
        try:
            shown = Path(results).resolve().relative_to(REPO_ROOT).as_posix()
        except ValueError:
            shown = Path(results).name
        line += f"; row in {shown}"
    return line + (f". {note}" if note else "")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one real coding agent on one Acme task under Threefold against a public stack: the day's run.")
    parser.add_argument("--endpoint", required=True,
                        help="the public stack's base URL, https only (the API's URL with its stage, or the edge's)")
    parser.add_argument("--dry-run", action="store_true", help="print the day's pick and the command, run nothing")
    parser.add_argument("--date", type=datetime.date.fromisoformat, default=None,
                        help="with --dry-run only: show another UTC day's pick (YYYY-MM-DD)")
    parser.add_argument("--results-dir", type=Path, default=LIVE_RESULTS_DIR,
                        help="where <date>-<agent>.jsonl is written (default benchmark/results/live)")
    parser.add_argument("--work-root", type=Path, default=None,
                        help="default: the benchmark's own, <system temp or drive root>/threefold-bench/<date>-<agent>")
    parser.add_argument("--token-file", type=Path, default=None,
                        help="Claude Code's token file (default: the benchmark's, when it exists); never printed")
    parser.add_argument("--codex-home", type=Path, default=None,
                        help="CODEX_HOME for a Codex day: the folder holding only the Codex login; never printed")
    parser.add_argument("--claude", default=None, help="the claude executable (default: found on PATH)")
    parser.add_argument("--codex", default=None, help="the codex executable (default: found on PATH)")
    parser.add_argument("--retry-pause", type=float, default=None,
                        help="seconds before a run a usage limit cut short is tried once more (the benchmark's default)")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        endpoint = check_endpoint(args.endpoint)
        if args.date is not None and not args.dry_run:
            raise Refused("--date is for --dry-run only: a real run is named after the UTC day it runs")
        day = args.date or utc_today()
        pick = pick_for(day)
        pick.project  # a name the stack would not show is refused here, not after the agent ran
        work_root = args.work_root or run.default_work_root(pick.run_id)
        # The hook runs only in the task repository the benchmark copies under the work root.
        harness.ensure_outside_workspace(work_root)
    except (Refused, ValueError) as error:
        print(f"refused: {error}.", file=sys.stderr)
        return 2
    if args.dry_run:
        arguments = run_arguments(pick, endpoint, args.results_dir, work_root, args.token_file, args.claude,
                                  args.codex, args.retry_pause)
        results = Path(args.results_dir) / f"{pick.run_id}.jsonl"
        print(f"live {pick.date}: {pick.agent} on {pick.task}, project {pick.project}, session {pick.session}, "
              f"against {endpoint}")
        print("would run: " + shown_command(arguments, bool(args.codex_home and pick.agent == "codex")))
        if read_rows(results):
            print(f"(a row for this day is already in {results.name}, so a real run would do nothing)")
        return 0
    code, row, note = run_live(pick, endpoint, args.results_dir, work_root, args.token_file, args.codex_home,
                               args.claude, args.codex, args.retry_pause)
    results = Path(args.results_dir) / f"{pick.run_id}.jsonl"
    print(summary_line(pick, row, note, results if row is not None else None))
    return code


if __name__ == "__main__":
    sys.exit(main())
