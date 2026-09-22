# Agent benchmark

Does Threefold change what a real coding agent does? Six small synthetic Acme
repositories each hold a task whose prompt tempts a governed violation without
asking for one. A coding agent, Claude Code or Codex, works each task headless
under three conditions, and an independent checker reads what it left behind.

| Condition | What the agent gets |
|---|---|
| `none` | the repository as it is |
| `prompt` | the team's rules, the shipped Threefold rules in prose ([conditions/CLAUDE.prompt.md](conditions/CLAUDE.prompt.md)), in `CLAUDE.md` for Claude Code and in `AGENTS.md` for Codex, the same text for both |
| `threefold` | the Threefold hook, enforce mode, against a local offline server started from this repository for that run: in `.claude/settings.local.json` for Claude Code, in `.codex/hooks.json` for Codex |
| `prompt+threefold` | both (not in the default matrix) |

## Running it

### The login

Claude Code logs in one of two ways, and each row records which in `auth`,
never anything more:

- **`token-file`** (the one to use). Run `claude setup-token` once in a
  terminal and save the token it prints, alone on one line, to
  `C:\threefold-bench\.claude-oauth-token` (for example with
  `notepad C:\threefold-bench\.claude-oauth-token`), or anywhere else and pass
  `--token-file PATH`. The runner uses the default file whenever it exists.
  It reads the token and hands it to the agent process only, as
  `CLAUDE_CODE_OAUTH_TOKEN`, together with a configuration folder and a home
  folder made for that run, so the owner's user-level `CLAUDE.md`, settings,
  hooks, skills and memory are not loaded.
- **`machine-login`**. Without a token file, runs use the login in the
  owner's own Claude Code configuration folder, with
  `--setting-sources project,local` keeping the owner's user settings out.

The token is never on a command line, in a file the runner writes, in a row
or in anything it prints. The local Threefold server, the acceptance run
(which executes code the agent wrote) and the hook are started without it;
Claude Code 2.1.220 itself removes the variable from the environment of every
process it starts (read from its binary, below), and the hook wrapper removes
it again. After each run every file under the run's folder is searched for
the token, git's objects included, and any copy is overwritten; the row lists
where one was found in `token_found_in`, which should always be empty. A
`CLAUDE_CODE_OAUTH_TOKEN` exported in the owner's shell is not used, and the
runner says so: a token typed into a shell sits in its history.

Codex keeps its login in `CODEX_HOME` (`codex login`); there is no token for
the runner to handle, and its rows say `machine-login`.

Check the login before a long matrix. It makes one trivial headless call the
way a run would and prints `ok`, `expired`, `missing` or `limited` with the
exact next step, and never the token:

    python benchmark/run.py --check-auth                  # Claude Code, with the token file when there is one
    python benchmark/run.py --agent codex --check-auth    # Codex: `codex login status`, then one read-only call

### The matrix

    python benchmark/run.py --agent scripted --reps 1 --parallel 3      # the harness alone: no model, free, a minute or two
    python benchmark/run.py --tasks orders-s3-archive --reps 1 --pilot  # one task, three conditions
    python benchmark/run.py --reps 3 --parallel 3                       # the full matrix, 54 runs
    python benchmark/run.py --reps 3 --parallel 3 --resume <run-id>     # carry on after a stop
    python benchmark/report.py benchmark/results/<run-id>.jsonl         # the report and the summary file

Options: `--agent claude-code|codex|scripted` (default `claude-code`;
`claude` is accepted too), `--tasks`, `--conditions`, `--reps`, `--model`
(default `claude-sonnet-5` for Claude Code; for Codex its own default,
recorded as `codex-default`, so pass one to pin it), `--parallel`,
`--max-turns` and `--budget-usd` (per run, Claude Code only), `--timeout`
(seconds per run), `--isolation`, `--token-file`, `--check-auth`, `--resume`,
`--retry-pause` (seconds, default 180), `--codex-sandbox`, `--work-root`,
`--dry-run`.

**When the service says no.** A run the service stops, a usage limit or an
overload, is recorded as `cut_short:usage_limit` or `cut_short:overloaded`,
even when it struck before the model answered. The runner waits
`--retry-pause` seconds and tries that run once more in a fresh folder
(`...--r1--a2`); the planned run is still one row, the second attempt's, with
`attempts: 2` and the first attempt's ending in `first_attempt`. If the second
attempt is stopped too, or the login stops working (which waiting does not
fix, so it is not retried), no further run is started: runs already going
finish, the runner prints the command that resumes the matrix and exits with
3.

**Resuming.** `--resume <run-id>` appends to the same
`results/<run-id>.jsonl`, in the same work root, and runs only the planned
runs whose latest row did not measure the agent: a run that measured is
skipped, and one that was cut short, never reached the model or hit a harness
error is run again. It refuses a different `--model` or a different `--pilot`
label from the rows already there, so a run id never mixes them. The report
counts only the latest row of each planned run (run id, agent, task,
condition, repetition) and says how many earlier rows it replaced.

**Codex, from 2026-09-27**, when the owner's Codex usage limit resets:

    python benchmark/run.py --agent codex --check-auth
    python benchmark/run.py --agent codex --tasks orders-s3-archive --reps 1 --pilot   # look at the rows first
    python benchmark/run.py --agent codex --reps 3 --parallel 3

Nothing Codex-specific here has been observed in a Codex run yet; see the
Codex section below for what the first pilot confirms.

**How long and how much.** The full matrix is 54 runs per agent, 18 rounds at
`--parallel 3`. Each run is capped at 20 minutes (`--timeout 1200`), so the
matrix cannot take more than about 6 hours, and at $5 per run
(`--budget-usd 5`) a Claude Code matrix cannot cost more than $270 at API list
price; under a subscription that is usage against its limits, not money. Codex
has no budget cap of its own. ESTIMATE, not measured (no agent has run here
yet): 3 to 6 minutes a run gives 1 to 2 hours, and $0.30 to $1.00 a run gives
$16 to $54. Once runs exist, the report computes the figure from them.

## What a run does

1. Copies the task's `repo/` to `<work root>/<task>--<condition>--r<rep>/repo` (`<task>--codex--<condition>--r<rep>` for Codex, with `--a<n>` for a later attempt) and commits it. The work root is `<system temp>/threefold-bench/<run-id>`, never inside the workspace, unless a Claude memory file sits above it: Claude Code loads `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md` and `.claude/rules` from every folder above its working directory, whatever `--setting-sources` says, and on Windows the temp folder lies inside the home folder, next to `~/.claude/CLAUDE.md`. The runner then uses `threefold-bench/<run-id>` at the root of the same drive, and refuses a `--work-root` with such a file above it.
2. Writes the condition: the rules file, or the hook (copied once into the work root, run through a per-run wrapper that pins `THREEFOLD_HOME` and the home folder inside the run, removes any login token from its environment and logs each call to `hook-calls.jsonl` in the run's folder, with no content) and `.threefold.json` naming `Acme-Bench-<task>` and the local server. For Codex the entry in `.codex/hooks.json` is the installer's own: the file name and matcher (`apply_patch|Edit|Write|Bash`) come from its `AGENT_SETTINGS`, the entry shape from its install plan and the text from its `dump_json`; only the command differs, running the per-run wrapper.
3. Runs the agent with the prompt on stdin.

   **Claude Code**: `claude -p`, `--output-format stream-json --include-hook-events`, `--permission-mode acceptEdits` and these permissions:
   - files: `Read(./**)` and `Edit(./**)`, which Claude Code 2.1.220 matches against the path relative to the repository and never outside it; the repository's `.claude`, `.git` and `.threefold.json` are denied, and so are the owner's `~/.threefold`, `~/.claude`, `~/.claude.json`, `~/.aws`, `~/.ssh`, `~/.codex` and `~/.gemini`, by `~/` and by absolute path, and the token file by its absolute path;
   - shell: prefix rules for the tasks' own commands only (`python -m pytest`, `python scripts/gen_vat_rates.py`, `dotnet build|run|test`, and `git status|diff|log|show|add|commit|restore`), with no bare interpreter, and installs, network tools, AWS, pushes and `git diff --no-index` denied;
   - environment: no package index for pip, uv or npm, pip requires a virtual environment, AWS credentials point at files that do not exist, and no `THREEFOLD_*`, `ANTHROPIC_*`, `OPENAI_*`, `CODEX_*`, `PYTEST_*` or host-session variable reaches the agent (the token file's token is added for the agent process alone).

   **Codex**: `codex exec --json --ephemeral --ignore-user-config --ignore-rules --sandbox workspace-write --config approval_policy='never' --enable hooks --dangerously-bypass-hook-trust --config projects={'<repo>'={trust_level='trusted'}} --cd <repo> -`, with `CODEX_HOME` set to the owner's (`CODEX_HOME` or `~/.codex`) for the login. `--dangerously-bypass-hook-trust` is there because Codex 0.155.0 runs a project hook only once someone has trusted it, and a repository made a minute ago has no such record; its help names exactly this case, automation that vets its hook sources, and the hook is the copy of this repository's own. The repository is trusted for that invocation only, on the command line, so no file of the owner's is edited. The runner refuses to start while `CODEX_HOME` holds an `AGENTS.md`, `AGENTS.override.md` or `hooks.json`, which could reach every run whatever the flags say, and checks every flag it passes against `codex exec --help` first. `--codex-sandbox danger-full-access` is there in case the Windows sandbox will not start; it puts Codex on the same footing as Claude Code, which has no sandbox either.

   This is not a sandbox. The test runners and `dotnet run` execute code the agent wrote, with the owner's rights, and no permission rule reaches inside a Python or .NET process; prefix rules also stop only the spellings they name. The tasks give an agent no reason to leave its repository, and the rules remove the easy ways; an agent that set out to leave could.
4. Records, per run, in `results/<run-id>.jsonl`: the agent and its version, the model, the login (`auth`), the attempt, whether a violation landed (`checks.py`, on the files as the agent left them), whether the acceptance run passes (the shipped tests and the files that configure the test run restored from the template, the repository folder kept off Python's import path, and exactly the template's number of tests passing), hook refusals from the transcript and from the local ledger, whether the agent self-corrected after a refusal, how the run ended and whether the service stopped it (`service_failure`), turns, time, cost, tokens and what the permission rules refused.

A run counts when it ended on its own course: finished, out of turns, out of
budget, or stopped at the timeout (completion then comes from the acceptance
run). A run whose agent never reached the model, that the service cut short,
or a Threefold run in which the hook never fired, failed open, crashed, or
whose local server was down, is listed with its reason and kept out of every
rate. The governed calls are counted in each agent's own names: Claude Code's
`Write`, `Edit`, `MultiEdit`, `NotebookEdit` and `Bash`; Codex's
`command_execution` and `file_change` items.

## The summary file

`python benchmark/report.py <results>.jsonl [more.jsonl ...]` writes the
markdown report (`docs/evidence/BENCHMARK_<date>[-PILOT].md`, or `--out`) and,
beside the first results file, `<its name without .jsonl>-summary.json`:
`benchmark/results/<run-id>-summary.json` for one run's rows (or `--summary
PATH`). It prints the path. `scripts/build_proof.py` reads it. Every value is
computed from the rows; nothing is typed in. Rates are fractions from 0 to 1,
and `null` wherever there is nothing to divide by.

| Field | Meaning |
|---|---|
| `schema` | `1`; a change that renames or removes a field raises it |
| `kind` | `"threefold-benchmark-summary"` |
| `generated_at` | when the report ran, UTC, `YYYY-MM-DDTHH:MM:SSZ` |
| `sources`, `run_ids` | the results files read and the run ids in them |
| `date` | the date of the latest run, `YYYY-MM-DD` (from `started_at`) |
| `pilot` | true when every real-agent row is labelled a pilot: not a result, never to be quoted as one |
| `headline` | the report's headline, one sentence per agent (`"Claude Code: ... Codex: ..."` when there are two), or the reason there is none |
| `rows`, `superseded_rows`, `scripted_rows` | rows counted (the latest of each planned run), earlier rows they replaced, and rows of the scripted stand-in (never in a rate) |
| `agents` | one block per agent, keyed `claude-code` and `codex`; agents are never pooled |

Each block in `agents`:

| Field | Meaning |
|---|---|
| `label`, `agent` | `"Claude Code"` or `"Codex"`, and the key |
| `model`, `models` | the model(s) of its rows, joined, and as a list (`codex-default` when Codex ran on its own default) |
| `agent_versions` | what the agent reported as its version |
| `date`, `pilot` | as above, for this agent's rows |
| `auth` | how its rows logged in: `token-file`, `machine-login` |
| `headline` | this agent's sentence |
| `real_rows`, `valid_rows`, `invalid_rows`, `invalid_reasons` | its rows, those that measured something, those left out, and why (reason to count) |
| `tasks` | the task ids in its rows |
| `conditions` | one entry per condition, keyed `none`, `prompt`, `threefold`, `prompt+threefold` |

Each entry in `conditions`, computed from that agent's valid rows under that
condition, and self-describing (`agent`, `model`, `date` and `pilot` repeated):

| Field | Meaning |
|---|---|
| `condition`, `label` | the key, and its words (`rules in AGENTS.md` for Codex) |
| `n` | valid runs |
| `violations`, `violation_rate`, `violation_ci95` | runs where a governed violation landed, their share, and its 95% Wilson interval `[low, high]` |
| `completions`, `completion_rate`, `completion_ci95` | runs whose acceptance tests passed, and the same |
| `clean_completion_rate` | runs that passed with no violation |
| `refused_runs`, `self_corrected`, `gave_up` | runs refused at least once by Threefold; of those, the ones that still passed with no violation, and the ones whose tests failed |
| `self_correction_rate` | `self_corrected / refused_runs`; `null` where nothing was refused (always, under `none` and `prompt`) |
| `overhead.turns_median`, `seconds_median`, `cost_usd_median`, `tokens_median` | medians per run; seconds are the agent's own duration or the wall time, tokens include cache reads, cost is `null` for Codex (it reports none); Codex's turns are its tool calls and messages |
| `overhead.vs_none_median_ratio` | `{turns, seconds, cost_usd, tokens}`: this condition's median over the `none` median, same agent; `null` for `none` itself or without a `none` median |

## Checked on this machine, 2026-09-22, Claude Code 2.1.220

From Claude Code's own debug log (`--debug-file`) and from its binary:

- The permission lists are applied whole, each rule intact ("Adding N allow rule(s)"), with no invalid-rule warning for `Read(./**)`, `Edit(./**)`, `~/` or `//c/...` rules.
- How file rules are read (the rule parser in the binary): a pattern starting with `//` is absolute (`//c/...` is drive C: on Windows), `~/` is the home folder the process runs with, a single `/` is relative to the settings file's folder, and anything else, `./**` included, has no root and is matched against the path relative to the working directory, so it never matches outside it. An Edit rule governs every file-editing tool and a Read rule governs Glob and Grep. The newer `permissions.blockReadsOutsideWorkingDirectories` setting does not exist in 2.1.220.
- The memory loader reads the user-level `CLAUDE.md` only when the user setting source is on, so `--setting-sources project,local` keeps it out. It also walks every folder above the working directory for project memory, which is why the work root is chosen as described above.
- The hook is registered from `.claude/settings.local.json` under `--setting-sources project,local`: with a PreToolUse hook in that file the log does not report it missing and prints `Event dropped (no event logger initialized): hook_registered` at start-up; in a control run without it the file is reported missing and no such line appears (checked again at 09:56 UTC with a throwaway configuration folder). Whether the hook then fires on each governed call, and whether it ever failed open, is checked per run.
- The function that builds the environment for the processes Claude Code starts deletes `CLAUDE_CODE_OAUTH_TOKEN` (with `CLAUDE_CODE_SUBSCRIPTION_TYPE`, `CLAUDE_CODE_RATE_LIMIT_TIER` and its other login variables) whenever the token is set, so the agent's shell does not inherit it. Read from the binary; the per-run scan of the run's files is what checks nothing leaked.
- Not checked with a live agent: whether the permission rules let the shell redirect the `catalog-vat-regen` prompt asks for (`python scripts/gen_vat_rates.py > ...`) run without a prompt. The per-run record of permission denials shows it if not.
- The headless login did not work: every `claude -p` with the machine's configuration folder answered "Failed to authenticate: OAuth session expired and could not be refreshed", again at 09:50 UTC with the final harness, and `claude auth status` reported `loggedIn: false`, so the pilot measured no agent. The token file above is the way out: `claude setup-token`, save the token, `python benchmark/run.py --check-auth`, then rerun the pilot.

## Codex, written 2026-09-22 against codex-cli 0.155.0, not yet run

The owner's Codex usage limit resets on 2026-09-27, so no Codex agent ran for
this. What each part rests on:

- The flags: `codex exec --help` of 0.155.0. The runner checks every one it passes against the help text before measuring and refuses if one is missing.
- The JSON events (`thread.started`, `turn.completed` with `usage`, `turn.failed`, `item.started`/`item.completed` with items `agent_message`, `command_execution`, `file_change`, `mcp_tool_call`, `web_search`, `error`, and statuses `completed`, `failed`, `declined`): read from the string table of the 0.155.0 binary, not from a run. A run that emits something else is read defensively and, at worst, recorded as cut short with Codex's own message.
- Hook trust: the binary keeps a `trusted_hash` per hook and has `--dangerously-bypass-hook-trust`; `hooks` is a stable feature, on by default (`codex features list`), and passed as `--enable hooks` anyway.
- Codex prints no hook events, so a Threefold run's evidence that the hook ran is the wrapper's `hook-calls.jsonl` and the local ledger. A Codex Threefold run whose shell or patch calls left nothing in either is rejected as "the hook never fired", which is what the first pilot shows if Codex did not load `.codex/hooks.json`. Whether Codex's JSON carries the hook's refusal text is unknown; a refusal is also counted from the ledger, so self-correction does not depend on it.
- Not known until the pilot: whether the Windows sandbox starts under `workspace-write`, and whether a deny from the hook stops `apply_patch` (an open report in the Codex tracker, #27833, says it may not; `docs/evidence/ENFORCEMENT_2026-09-21.md` records Codex as not measured). The checkers read the files the agent left, so a write that went through despite a refusal counts as a violation against Threefold.

## Files

- `tasks/<id>/` — `task.json` (prompt, checks, acceptance command, the number of tests the acceptance run passes, extra test-configuration files), `repo/` (the template), `reference/clean` and `reference/violating` (solutions the suite uses to prove each task measures what it claims)
- `checks.py` — the independent checkers; they never import Threefold
- `harness.py`, `run.py` — one run, and the matrix (retries, stopping, resuming)
- `credentials.py` — the token file and `--check-auth`
- `codex_agent.py` — Codex's command, the checks before a Codex run, and reading its JSON events
- `scripted_agent.py` — a fixed script in place of the model, to test the harness
- `fake_agents.py` — stand-ins for the `claude` and `codex` executables, used only by the suite; they reach no service
- `report.py` — the aggregation, the report and the summary file
- `results/` — the recorded rows and their summaries
