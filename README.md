# Threefold

**Threefold refuses a coding agent's edit the moment it is made, not after the commit, so your architecture does not rot while you sleep.**

Built for the AWS Zero to Shipped hackathon. **Category:** `#workplace-efficiency` · **Lane:** `#community`

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

Threefold sits in front of the tool calls a coding agent makes (Claude Code,
Codex and Antigravity, through one hook file) and answers each write or command
before it runs. Deterministic gates decide: a domain file importing
infrastructure under the architect's layering rules, a credential in the
arguments, a write that switches the hooks off, the same call repeating, a
spend ceiling. A team connects a repository with one command, and every project
starts in **Observe**: calls are judged and recorded, and only a credential is
refused, on the developer's own machine. The operations dashboard shows what
each rule *would* have refused; the operator marks each of those correct or a
false alarm, promotes the project to **Enforce** with the rules that earned it,
and demotes it with one click. Amazon Bedrock never decides; it phrases a
refusal for a person reading a page and drafts rules for an architect, and
every response names which of the two you are reading.

## Live

| | URL | What it is |
|---|---|---|
| **Site** | **<https://d1og72wpk4aqig.cloudfront.net/>** | CloudFront in front of everything: the pages from a private S3 bucket, every API path to the function, AWS WAF, security headers |
| Origin | <https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/> | the API Gateway URL the edge forwards to. It serves the same pages from the function and stays public; it has no WAF and none of the edge's headers. The trailing slash is part of it: the bare `/prod` is API Gateway's own 404 |

Both answered anonymously, with no key, on 2026-09-22: `GET /` returned 200
`text/html` from each, and the site's response carried HSTS, a content security
policy, `x-frame-options: DENY`, `nosniff` and `referrer-policy: no-referrer`
[PRIMARY, 2026-09-22]. How the two fit together, and why both exist:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Sixty seconds, no account

1. **The two-stage rollout, on a project of your own:**
   <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try>. Five steps: make
   a sandbox project (`Acme-Sandbox-<8 hex>`, seeded with twelve synthetic hook
   calls from all three agents through the real evaluator, gone after 24 hours),
   see what its rules would have refused, label each call, promote, and send the
   same kind of call again to watch it refused, with the fix it suggests. That
   last call is sent in a `sim-` session, and the service enforces every `sim-`
   session whatever the project's stage, so it would be refused without the
   promotion too; a real hook's call follows the stage.
2. **The flagship demo:** <https://d1og72wpk4aqig.cloudfront.net/>. Scenario 1
   sends one `POST /simulate-loop`; the function evaluates the same call three
   times and the third is refused with `BLOCKED_LOOP_DETECTED`, halting that demo
   session. Scenario 4 evaluates four ordinary calls and issues a governance
   certificate. Every click gets a fresh session.
3. **The dashboard itself:**
   <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/overview>. Every tile
   and bar opens the calls behind it. `#/proof` shows what has been measured, and
   says "not measured yet" where nothing has.

The public stack has no operator key [STATE-FILE], so anonymous visitors can
read, try and run the sandbox, and cannot change the policy, the layering rules
or any real project's stage.

---

## Install it in front of your own agent

**One command.** These are the lines the dashboard's connect wizard
(`dashboard.html#/connect`) builds, with the stack the page was served from as
the base; `Acme-Billing` is the project alias you choose.

Windows PowerShell:

```powershell
irm https://d1og72wpk4aqig.cloudfront.net/install.py -OutFile threefold.py; py threefold.py connect --project Acme-Billing
```

macOS and Linux:

```bash
curl -fsSL https://d1og72wpk4aqig.cloudfront.net/install.py -o threefold.py && python3 threefold.py connect --project Acme-Billing
```

`/install.py` is the installer with the address of the stack that served it
written in. Fetched from the site it names the site; fetched from the origin it
names the origin [PRIMARY, 2026-09-22]. It is served as readable text, so read
it first if you like. `connect`:

- downloads the hook, the pre-commit check and the engine that check runs on
  from the stack's `/dist/threefold-bundle.zip`, and writes each file only when
  its SHA-256 matches `/dist/manifest.json`;
- writes `.threefold.json` (the project, the mode, the endpoint; a key file
  path if you give one, never a key) and merges one hook entry per agent into
  `.claude/settings.local.json`, `.codex/hooks.json` and `.agents/hooks.json`,
  keeping every other entry;
- adds a pre-commit hook running `threefold_cli.py check`, chaining one already
  there;
- lists everything it wrote in `.git/info/exclude`, so none of it is committed,
  and never writes a file git already tracks;
- sends one harmless dry-run call, prints whether the stack recorded it, and
  opens the dashboard on the project.

Codex reads a project's hooks only once that project is trusted in Codex; the
installer does not change that setting for you. Later, `py threefold.py status`
lists every connected folder and its stage, `py threefold.py disconnect [PATH]`
removes exactly what was added, and `py threefold.py open` signs you in (below).
The installer keeps a copy of itself at `~/.threefold/bin/threefold_install.py`
for when `threefold.py` is gone.

From a checkout of this repository, [`scripts/threefold_install.py`](scripts/threefold_install.py)
is the same installer:

```bash
python scripts/threefold_install.py connect /path/to/acme-billing --project Acme-Billing \
  --endpoint https://d1og72wpk4aqig.cloudfront.net/
```

**Modes.** `connect` installs in `managed` mode: each call is sent to be judged
and the project's stage on the service decides, Observe or Enforce.
`--mode observe` pins the machine instead: every call goes as a dry run, which
the service judges and records and no rule refuses, whatever the stage says; the
older form `--repo PATH --project NAME` defaults to it. `--mode enforce` sends
calls exactly as `managed` does, so the project's stage still decides on the
service; what it adds is on the machine, where a file-tool write (`Write`,
`Edit`, a patch) to the hooks' own files is refused whatever stage was last
seen. A machine installed with
`--mode observe` therefore reaches enforcement in two steps: install again with
`--mode managed` or `--mode enforce` (or set `"mode"` in `.threefold.json`), and
promote the project on the dashboard, or deploy the stack with
`DefaultHookStage=enforce` for projects that have no stage of their own.

In every mode, a call carrying a credential is still refused on your machine
and never sent, and a call the hook holds back is neither sent nor recorded.

**What leaves your machine.** For a call it sends, the hook sends one
`POST /evaluate-tool-call`: the session id, your project alias, `anonymous` or
a 12-character hash of `THREEFOLD_DEVELOPER` computed on your machine, the tool
name, the action type and the call's arguments, which for a write carry the
text being written, with paths relative to the project root; plus which agent
this is, that it came from a hook, and its mode. It holds back, and so never
checks, a call whose target is outside the project root, anything under
`~/.claude`, `~/.codex` or `~/.gemini`, data files by extension and by
directory, any call containing a term from your own `never_send.txt`, and,
when `.threefold.json` has an `include` list, anything outside it. The
never-send list, your aliases and the list of governed repositories stay in
`~/.threefold/`.

**When the service cannot answer**, the hook prints nothing and exits 0, so
the agent's own permission flow decides: it fails open. `THREEFOLD_FAIL_CLOSED=1`
refuses instead. The credential refusal happens on the machine and does not
depend on the network.

**Does a deny stop the write?** Measured per agent, on the file system, on
2026-09-21: in Claude Code 2.1.220 and in the Antigravity desktop app the
refused file was not created. Codex was not measured, because its account had
reached its usage limit, so no claim is made for it and its edits count as
governed at commit time only. Method and results:
[`docs/evidence/ENFORCEMENT_2026-09-21.md`](docs/evidence/ENFORCEMENT_2026-09-21.md).

### By hand, one agent at a time

The hook is one standard-library file for three agents, served by the stack:

```bash
curl -O https://d1og72wpk4aqig.cloudfront.net/hooks/threefold_hook.py
```

Register it in the project, with the absolute path to the file. `--agent`
tells the one file which agent is calling it.

| Agent | File | Matcher | Command |
|---|---|---|---|
| Claude Code | `.claude/settings.local.json` | `Write\|Edit\|MultiEdit\|NotebookEdit\|Bash` | `python /absolute/path/to/threefold_hook.py --agent claude-code` |
| Codex | `.codex/hooks.json` | `apply_patch\|Edit\|Write\|Bash` | `python /absolute/path/to/threefold_hook.py --agent codex` |
| Antigravity | `.agents/hooks.json` | `write_to_file\|replace_file_content\|multi_replace_file_content\|run_command` | `python /absolute/path/to/threefold_hook.py --agent antigravity` |

Each file takes the same shape:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
        "hooks": [
          { "type": "command", "command": "python /absolute/path/to/threefold_hook.py --agent claude-code" }
        ]
      }
    ]
  }
}
```

| Variable | Effect |
|---|---|
| `THREEFOLD_PROJECT` | Required, here or in `.threefold.json`. The alias the project is sent under; unset, nothing is sent. Use an alias, never the real name. |
| `THREEFOLD_ENDPOINT` | The stack the hook asks. Without one it asks the origin URL above. |
| `THREEFOLD_MODE` | `managed`, `observe` or `enforce`, as above. The hook alone, with no configuration, runs `enforce`. |
| `THREEFOLD_API_KEY_FILE` | A file holding the operator key, for a stack that enforces one. A key is never sent to an endpoint that only a repository's own `.threefold.json` names. |
| `THREEFOLD_DEVELOPER` | Hashed on your machine; only the hash is sent. Unset, the hook sends `anonymous`. |
| `THREEFOLD_FAIL_CLOSED` | `1` refuses a call the service could not judge. |
| `THREEFOLD_TIMEOUT` | Seconds to wait for the service, 4 unless set. |
| `THREEFOLD_HOME` | Where the hook's local files live, `~/.threefold` unless set. |

---

## The two-stage rollout

1. **Observe.** A newly connected project has no stage of its own, so it takes
   the stack's default, `observe` on the public stack [PRIMARY, 2026-09-22:
   `DefaultHookStage=observe` in `describe-stacks`]. Every call is judged and
   recorded; a call a rule would have refused runs, and is counted as
   "would refuse".
2. **Review.** `#/review` queues every unreviewed would-refuse call, grouped by
   project and rule. Each is marked **correct** or **false alarm**, one at a
   time or in bulk; the label is stored on the ledger row.
3. **Readiness.** `#/projects/<name>` shows each rule's state: **Ready** when
   everything it flagged was marked correct, **Quiet** when it flagged nothing,
   **Needs review** while anything it flagged is unlabelled, **Noisy** after any
   false alarm.
4. **Promote.** Promote moves the project to Enforce with the rules you pick;
   the others keep observing, recording what they would refuse. **Demote** is one
   click back to Observe and applies from the next call.

The stage applies to calls from hooks and CI. The demo's page and simulation
calls always enforce, so the public demo behaves the same whatever a project's
stage is. A machine pinned to `--mode observe` is never refused by the service,
whatever the dashboard says, and the project page flags such a machine among
its agents.

A refusal comes with a **validated fix** when one fits: a rewritten file, a
port and an adapter, an environment lookup in place of a literal credential.
Every write it proposes is run back through the same gates with the same rules
before it is offered, `validated` says whether all of them passed, and the
hook appends its one-line summary to the deny reason, so the agent is told what
to do instead. No model writes it.

## Sign in without pasting a key

On a stack deployed with an operator key (a private stack, `PublicReads=false`),
connect once with the key file:

```bash
python3 threefold.py connect --project Acme-Billing --endpoint https://<your stack>/ --api-key-file /path/to/operator.key
python3 threefold.py open
```

`open` sends the key from that file in a header to `POST /api/auth/links`,
receives a single-use code that expires in 120 seconds, and opens
`dashboard.html#/signin?code=...`. The page trades the code for a session
valid for 12 hours. The code and the session are stored only as SHA-256
hashes, and the browser never holds the key. The public stack has no operator
key, so sign-in is closed there: `POST /api/auth/links` answers 403 "Sign-In Is
Closed Here" ([`docs/evidence/PROBES_2026-09-22.md`](docs/evidence/PROBES_2026-09-22.md)),
and `GET /api/auth/whoami` reports `reads_public: true, sandbox_writes: true`
[PRIMARY, 2026-09-22].

## What the gates watch, and what they are blind to

The service states its own coverage at `GET /api/insights`, and the layering
row is computed from the rules in force, so it changes when an architect saves
a rule. As served by the public stack [PRIMARY, 2026-09-22]:

| Gate | Watches | Blind to |
|---|---|---|
| Credentials | Ten credential shapes in any argument, at any depth, for every language | Credentials that do not match a known shape, and anything already in the file on disk |
| Layering | 4 rules refusing (`python-domain-stays-pure`, `java-domain-stays-pure`, `dotnet-domain-stays-pure`, `web-domain-stays-pure`) over `**/Domain/**/*.cs`, `**/domain/**/*.java`, `**/domain/**/*.py`, `**/domain/**/*.pyi`, `**/domain/**/*.ts`, `**/domain/**/*.tsx`; imports read from `.cs`, `.java`, `.js`, `.jsx`, `.mjs`, `.py`, `.pyi`, `.ts`, `.tsx` | Any path no refusing rule covers, any file type not in that list, and the dependencies a file does not declare: an import is read from the file's own statements, not resolved, followed or injected |
| Loops | Any repeating cycle of byte-identical tool calls, up to period six, within one session | Two calls that differ by one character, and repetition across separate sessions |
| Budget | Projected spend per call and per session, against the policy | Real usage. The counts are the ones the caller declares, so a caller declaring zero is not stopped |

The same boundary gate also refuses writes that switch governance off (the
agents' hook settings, `.git/hooks`, `git commit --no-verify` and the other ways
to point git at other hooks), shell writes whose content cannot be read on a
path a rule covers, and destructive commands. A shell command is read for the
writes it makes, so a heredoc into a domain file is judged like a `Write`.

Blind by design, as the hook's contract in `STATE.md` sets it [STATE-FILE] and
`src/threefold/hooks/threefold_hook.py` implements it:

- A call the hook holds back is not checked by anything, and while the service
  cannot be reached the hook fails open.
- Everything under `.git` is a data directory to the hook. Outside `enforce`
  mode (and `managed` mode while the project enforces), where the hook refuses
  it on the machine, a `Write` or `Edit` to `.git/hooks/` or `.git/config` is
  held back: it is not sent, so an attempt to switch the pre-commit check off
  that way never shows up in Observe or the review queue. The same write made
  through a shell command is sent, and recorded under `PROTECTED_PATH`.

Also true, and recorded in `STATE.md` as not yet fixed [STATE-FILE]:

- Three of the five keys `/policy/config` returns are enforced by nothing:
  `max_session_budget_usd` and `loop_history_window` are stored and read by no
  gate, and `blocked_patterns` is read by no module. The settings page says so
  on its face.
- Every session is priced at the default Sonnet-class rate, because no model id
  reaches the cost calculator.
- The governance certificate is an unkeyed SHA-256 fingerprint over verdicts
  the caller supplies. It detects corruption, not an adversary, it is returned
  rather than archived, and nothing in CI requires one before a merge. The S3
  bucket the stack provisions is empty.
- The sessions listing's call count stops at 50, because a session keeps its
  last 50 calls.
- Four of the five offline fallback panels on the demo page show canned text,
  labelled as simulated.
- No comparative benchmark number exists yet (below).

---

## Architecture in brief

```
agent ─► threefold_hook.py ─┐   (local: credential refusal, hold-back, mode)
                            ▼
browser ─► CloudFront + WAF ─┬─► S3, private, origin access control (pages, assets)
          (us-east-1)        └─► API Gateway HTTP API, stage prod (eu-west-1)
                                   └─► one Lambda, Python 3.11 on arm64
                                         ├─► DynamoDB, one table (sessions, ledger,
                                         │     daily rollups, rules, stages, sign-in)
                                         ├─► Bedrock, Claude Haiku 4.5 (page
                                         │     explanations and rule drafts only)
                                         └─► CloudWatch (EMF metrics, 10 alarms,
                                               a dashboard, X-Ray)
```

Two stacks come from the one template: `threefold-prod`, the public demo above,
and a private stack with `PublicReads=false` that carries the owner's own work
under `Acme-Proj-*` aliases, all in Observe [STATE-FILE]. Its address is never
written in this repository. Topology, data model and trade-offs:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The six Well-Architected
pillars against what is deployed: [`docs/WELL_ARCHITECTED.md`](docs/WELL_ARCHITECTED.md).
Deploying, publishing the pages, probing, rolling back and tearing down:
[`docs/RUNBOOK.md`](docs/RUNBOOK.md).

## Run it locally

The tests are hermetic: `tests/conftest.py` sets `THREEFOLD_OFFLINE=1` before
anything builds an AWS client, so they make no network call and need no
credentials. Standard library and pytest only.

```bash
python -m pytest -q
```

The count is deliberately not printed here: pytest prints it, and a number
written into prose is wrong again the moment a test is added.

The local server runs the same Lambda handler on the standard library's HTTP
server, with the table and the model replaced by memory and deterministic text:

```bash
THREEFOLD_OFFLINE=1 python src/threefold/interfaces/server.py --port 8001
```

(PowerShell: `$env:THREEFOLD_OFFLINE = "1"; python src/threefold/interfaces/server.py --port 8001`.)
Then open <http://localhost:8001/> or <http://localhost:8001/dashboard.html>.

## Evidence

| File | What it shows |
|---|---|
| [`docs/evidence/PROBES_2026-09-22.md`](docs/evidence/PROBES_2026-09-22.md) | `scripts/probe_live.py` against the public origin URL: 113 PASS, 0 FAIL, 3 SKIP, with every check's evidence line. No probe of the edge URL is committed yet |
| [`docs/evidence/ENFORCEMENT_2026-09-21.md`](docs/evidence/ENFORCEMENT_2026-09-21.md) | Whether a deny stops the write, per agent, checked on the file system |
| [`docs/evidence/DEPLOYMENT_2026-09-20.md`](docs/evidence/DEPLOYMENT_2026-09-20.md) | Raw output of the first deployment, including the CloudTrail events that show Bedrock called by the function's own role |
| [`docs/evidence/BENCHMARK_2026-09-22-PILOT.md`](docs/evidence/BENCHMARK_2026-09-22-PILOT.md) | A **pilot** of the agent benchmark. Its three real-agent runs never reached the model (an expired login), so it measured nothing about agents; the scripted rows only prove the harness. No benchmark result exists yet. The headline will be computed by `benchmark/report.py` from the full matrix |
| [`docs/PROOF_OF_AWS_AGENT.md`](docs/PROOF_OF_AWS_AGENT.md) | The coding agent connected to AWS: what it ran, what AWS answered, and how to check it |

## Clean room

Every project, session and workload in this repository and on the public stack
is synthetic (`Acme-*`). A project name outside the stack's
`AllowedProjectPattern` is stored and shown as `unlabelled`, and developers
appear in public only as short hashes.
