# Agent benchmark

Does Threefold change what a real coding agent does? Six small synthetic Acme
repositories each hold a task whose prompt tempts a governed violation without
asking for one. Claude Code works each task headless under three conditions,
and an independent checker reads what it left behind.

| Condition | What the agent gets |
|---|---|
| `none` | the repository as it is |
| `prompt` | the team's rules, the shipped Threefold rules in prose, in `CLAUDE.md` ([conditions/CLAUDE.prompt.md](conditions/CLAUDE.prompt.md)) |
| `threefold` | the Threefold hook in `.claude/settings.local.json`, enforce mode, against a local offline server started from this repository for that run |
| `prompt+threefold` | both (not in the default matrix) |

## Running it

Claude Code must be logged in: `claude -p "hi"` has to answer rather than
report an expired session. For full isolation, create a long-lived token with
`claude setup-token` and export it as `CLAUDE_CODE_OAUTH_TOKEN`; every run then
gets a configuration folder of its own, so nothing from the machine's user-level
`CLAUDE.md`, settings, skills or memory reaches the agent. Without it, runs use
the machine's configuration folder with `--setting-sources project,local`.

    python benchmark/run.py --agent scripted --reps 1 --parallel 3      # the harness alone: no model, free, a minute or two
    python benchmark/run.py --tasks orders-s3-archive --reps 1 --pilot  # one task, three conditions
    python benchmark/run.py --reps 3 --parallel 3                       # the full matrix, 54 runs
    python benchmark/report.py benchmark/results/<run-id>.jsonl         # docs/evidence/BENCHMARK_<date>.md

Options: `--tasks`, `--conditions`, `--reps`, `--model` (default
`claude-sonnet-5`), `--parallel`, `--max-turns`, `--timeout` (seconds per run),
`--budget-usd` (per run), `--isolation`, `--dry-run`.

## What a run does

1. Copies the task's `repo/` to `<system temp>/threefold-bench/<run-id>/<task>--<condition>--r<rep>/repo`, never inside the workspace, and commits it.
2. Writes the condition: `CLAUDE.md`, or the hook (copied into the work root, run through a per-run wrapper that pins `THREEFOLD_HOME` and the home folder inside the run) and `.threefold.json` naming `Acme-Bench-<task>` and the local server.
3. Runs `claude -p` with the prompt on stdin, `--output-format stream-json`, `--permission-mode acceptEdits` and an allow list limited to editing, the tests, the generator, reads and git. Installs, network tools, AWS and pushes are refused; AWS credentials point at files that do not exist; no `THREEFOLD_*` or host-session variable reaches the agent.
4. Records, per run, in `results/<run-id>.jsonl`: whether a violation landed (`checks.py`), whether the acceptance tests pass after being restored from the template, hook refusals from the transcript and from the local ledger, whether the agent self-corrected after a refusal, turns, time, cost and tokens.

A run whose agent never reached the model, or a Threefold run in which the hook
never fired, is listed with its reason and kept out of every rate.

## Checked on this machine, 2026-09-22, Claude Code 2.1.220

From Claude Code's own debug log (`--debug-file`), with the runner's exact command:

- The permission lists are applied whole: "Adding 31 allow rule(s)" and "Adding 25 deny rule(s)", with `Bash(python -m pip:*)` intact.
- The hook is registered from `.claude/settings.local.json` under `--setting-sources project,local`: with the hook installed the log finds that file and records a `hook_registered` event at start-up; in a control run without it the file is reported missing and no such event appears. Whether it then fires on each governed call is checked per run (`hook_fired`).
- The headless login did not work: every `claude -p` answered "Failed to authenticate: OAuth session expired and could not be refreshed", with and without `--setting-sources` and with the whole inherited environment, so the pilot measured no agent. `claude auth status` still reported a login, because it reads the stored credential without refreshing it.

## Files

- `tasks/<id>/` — `task.json` (prompt, checks, acceptance command), `repo/` (the template), `reference/clean` and `reference/violating` (solutions the suite uses to prove each task measures what it claims)
- `checks.py` — the independent checkers; they never import Threefold
- `harness.py`, `run.py` — one run, and the matrix
- `scripted_agent.py` — a fixed script in place of the model, to test the harness
- `report.py` — the aggregation and the report
- `results/` — the recorded rows
