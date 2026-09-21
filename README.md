# Threefold

**Threefold refuses a coding agent's edit the moment it is made, not after the commit, so your architecture does not rot while you sleep.**

Built for the AWS Zero to Shipped hackathon. **Category:** `#workplace-efficiency` · **Lane:** `#community`

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

**Live: <https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/>** — open it and press a scenario. No account, no key, nothing to install.

---

## Why I built it

I run three coding agents on one laptop and I pay for their tokens. They are
good, and they are tireless, and that is the problem. One of them spent forty
minutes rewriting the same failing test. Another quietly imported the AWS SDK
into a domain entity because that was the shortest path to a green test, and I
found it a week later in review.

The second one is the reason this exists. The first costs money. The second
costs the shape of the codebase, and nothing was watching at the moment it
happened.

## The rule nothing else enforces

A file under a `domain/` directory may not import the outside world. That is an
old idea and every architecture linter checks it: import-linter, ArchUnit, Ruff.
All of them check **after** the code is written, in CI, once the agent has
already moved on to the next file.

Threefold checks at the moment the agent asks to write it, which is the only
moment the edit can still be refused. It parses the content, so
`from boto3 import client` is caught as surely as `import boto3`, and a comment
that merely mentions the rule is not. When the answer is no, the agent is handed
a refusal before the tool call runs.

```
$ cat call.json | python threefold_hook.py --agent claude-code
{"hookSpecificOutput": {
   "hookEventName": "PreToolUse",
   "permissionDecision": "deny",
   "permissionDecisionReason": "Threefold refused this call. Clean Architecture
    violation: domain file 'src/domain/user.py' cannot depend on an outer layer
    (from boto3 import ...)"}}
```

That is the hook talking to the live service, not a mock. It is one file,
[`threefold_hook.py`](src/threefold/hooks/threefold_hook.py), for three agents,
and the deployment serves it at
<https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/hooks/threefold_hook.py>.
On an approval it prints nothing, so the agent's own permission flow runs
unchanged. Whether a deny actually stops the write is being verified for each
agent, and the install page says so per agent rather than claiming enforcement
that has not been shown.

## Three more gates, honestly described

These matter, and none of them is novel. Mature tools do each one better, and
Threefold's contribution is that they run at the same perimeter as the rule
above rather than in four different places.

1. **Cycles.** Any repeating pattern of tool calls, not a fixed list of shapes.
   The agent looping A, A, B forever is caught, which is where the name comes
   from: the third time round the cycle, it stops.
2. **Credentials.** Ten patterns across AWS, GitHub, OpenAI, Anthropic, Slack,
   Google and JWTs, scanned at every depth of the arguments. Dedicated scanners
   carry hundreds of rules; use one of those in CI as well.
3. **Budget.** A session cost ceiling and a single-call spike cap. It trusts the
   token counts the caller declares, so it bounds honest overruns rather than an
   adversary. A proxy that meters real usage is the stronger control.

> **The tenet:** deterministic code decides, Amazon Bedrock only explains. Every
> response says which one you are reading, in a field called
> `explanation_source`.

---

## Install it in front of your own agent

One file for three agents. The deployment serves it, so there is nothing to
clone, no package to install and no account to make.

```bash
curl -O https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/hooks/threefold_hook.py

# Required: the alias this project is sent under, never its real name
export THREEFOLD_PROJECT="Acme-Billing"
```

Then register it, in the project you want governed, with the absolute path to
the script on your machine. `--agent` is what tells the one file which agent is
calling it.

**Claude Code** — `.claude/settings.local.json`

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

**Codex** — `.codex/hooks.json`

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "apply_patch|Edit|Write|Bash",
        "hooks": [
          { "type": "command", "command": "python /absolute/path/to/threefold_hook.py --agent codex" }
        ]
      }
    ]
  }
}
```

**Antigravity** — `.agents/hooks.json`

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "write_to_file|replace_file_content|multi_replace_file_content|run_command",
        "hooks": [
          { "type": "command", "command": "python /absolute/path/to/threefold_hook.py --agent antigravity" }
        ]
      }
    ]
  }
}
```

| Variable | Needed | Effect |
|---|---|---|
| `THREEFOLD_PROJECT` | required | The name this project is sent under. Use an alias, never the real name: it is what the console and the metrics show. |
| `THREEFOLD_DEVELOPER` | optional | Who you are, for the per-developer counts. Hashed on your machine; only the 12-character hash is sent. Unset, the hook sends `anonymous`. |
| `THREEFOLD_FAIL_CLOSED` | optional | `1` refuses the call when the service cannot be reached. Unset, an unreachable service means the hook prints nothing and exits 0. |
| `THREEFOLD_HOME` | optional | Where the hook's local files live, `~/.threefold` unless set. `never_send.txt` goes there, holding the terms you never want sent. |
| `THREEFOLD_ENDPOINT` | optional | The service the hook asks. |

**What leaves your machine.** For a call it sends, the hook sends one
`POST /evaluate-tool-call`: the session id, your project alias, `anonymous` or a
hash of `THREEFOLD_DEVELOPER`, the tool name, the action type and the call's
arguments, which for a write carry the text being written, plus which agent this
is and that it came from a hook. It never sends a tool call whose target is
outside the project root, anything under `~/.claude`, `~/.codex` or `~/.gemini`,
data files by extension and by directory, or any call containing a term from
your own `never_send.txt`. A call held back is not sent, so it is not checked.
The never-send list, your project aliases and the list of governed repositories
stay in `~/.threefold/`.

---

## 4 Guided User Journeys (Zero-Setup Live Demo)

Open <https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/> and press a button. The same page is in the repository at [`src/threefold/web/index.html`](src/threefold/web/index.html), which the Lambda serves.

Four more pages are served beside it, each one reading the live API rather than
a fixture: [`/console.html`](src/threefold/web/console.html) is the enforcement
console, what was refused across the projects and what the gates do not watch;
[`/rules.html`](src/threefold/web/rules.html) reads, tries and saves the layering
rules; [`/sessions.html`](src/threefold/web/sessions.html) lists the sessions the
service has actually governed and reads any one of them back;
[`/settings.html`](src/threefold/web/settings.html) reads and writes the policy
thresholds. [`/connect.html`](src/threefold/web/connect.html) is the install
path above, with the same three blocks, what leaves your machine, and what has
and has not been shown for each agent.

1. **Journey 1 · Runaway Tool Loop Interception:**  
   Click **Runaway Tool Loop**. The simulator sends 3 identical tool calls. Threefold detects the monomorphic loop on iteration 3, instantly trips the circuit breaker, locks the session, and halts token expenditure.
2. **Journey 2 · Secret Leakage Pre-Invocation Rejection:**  
   Click **Secret Leak Intercept**. An agent attempts to execute a shell command exporting an AWS Access Key (`AKIAIOSFODNN7EXAMPLE`). Threefold refuses the call before the command runs.
3. **Journey 3 · Clean Architecture Drift Prevention:**  
   Click **Clean Architecture Drift**. An agent attempts to write `import boto3` inside `src/domain/user.py`. Threefold blocks the write with a Clean Architecture violation alert.
4. **Journey 4 · Compliant Execution & Certificate:**  
   Click **Compliant Run & Cert**. 4 legitimate tool calls execute within budget ($0.0384). Amazon Bedrock provides architectural commentary, and the system issues a SHA-256 **Governance Certificate** exportable to JSON. The fingerprint is unkeyed: it detects corruption, not an adversary, and nothing in CI requires one before a merge.

---

## Architecture

```
                       ┌──────────────────────────────────────────────┐
                       │          Autonomous Coding Agent             │
                       │     (Bedrock Claude, AWS Q, Tool Loop)       │
                       └──────────────────────┬───────────────────────┘
                                              │ 1. Intercept Tool Call
                                              ▼
                               ┌───────────────────────────────┐
                               │     Threefold Gatekeeper    │
                               └──────────────┬────────────────┘
                                              │
                    ┌─────────────────────────┼─────────────────────────┐
                    │                         │                         │
                    ▼                         ▼                         ▼
         ┌─────────────────────┐   ┌─────────────────────┐   ┌─────────────────────┐
         │ 1. Secret Scanner   │   │ 2. Boundary Guard   │   │ 3. Loop Detector    │
         │ (AKIA, PATs, Keys)  │   │ (Clean Arch & .env) │   │ (N-gram Thrashing)  │
         └─────────────────────┘   └─────────────────────┘   └─────────────────────┘
                    │                         │                         │
                    └─────────────────────────┼─────────────────────────┘
                                              │
                                              ▼
                                   ┌─────────────────────┐
                                   │ 4. Cost Breaker     │
                                   │ (Token Rates & Cap) │
                                   └──────────┬──────────┘
                                              │
                     ┌────────────────────────┴────────────────────────┐
             [Trip / Reject]                                    [Pass]
                     │                                                 │
                     ▼                                                 ▼
        ┌─────────────────────────┐                       ┌─────────────────────────┐
        │  Session Frozen         │                       │ Amazon Bedrock Reviewer │
        │  Execution Aborted      │                       │ (Claude Haiku 4.5)     │
        └─────────────────────────┘                       └────────────┬────────────┘
                                                                       │
                                                                       ▼
                                                          ┌─────────────────────────┐
                                                          │ SHA-256 Audit Cert      │
                                                          │ (returned, not stored)  │
                                                          └─────────────────────────┘
```

---

## Technology Stack & AWS Services

- **Reasoning Engine:** Amazon Bedrock (Anthropic Claude Haiku 4.5 via the Converse API).
- **Serverless Compute:** AWS Lambda (Python 3.11 on ARM64 Graviton2).
- **Ingress & API:** Amazon API Gateway HTTP API with full CORS support.
- **Audit Storage:** Amazon DynamoDB, single-table session state. The stack also provisions an S3 bucket for evidence bundles, and nothing writes to it: the certificate is returned in the response, and the session behind it is what persists.
- **Frontend:** served by the same Lambda that answers the API, so one URL is the whole application. There is no CloudFront distribution in the stack.
- **Standards:** Clean Architecture, Domain-Driven Design. The gates are pure standard-library Python with no model on the critical path, so the verdict does not wait on an LLM. No latency figure is quoted because none has been measured.

---

## Testing Pyramid & Verification

The suite is hermetic: `THREEFOLD_OFFLINE=1` keeps every AWS client out of the tests, so they make no network call and do not depend on the credentials on the machine.

```bash
# Run the complete test suite
THREEFOLD_OFFLINE=1 python -m pytest tests -v
```

The count is deliberately not printed here: `pytest` prints it, and a number
written into prose is wrong again the moment a test is added.

- **Unit Tests:** cost arithmetic, single-invocation caps, monomorphic loops, ping-pong loops, secret regex matching, and Clean Architecture imports.
- **Integration Tests:** AWS Lambda API Gateway proxy routing, query normalization, and error handling.
- **Security & Tamper Tests:** Negative-control proving mutating 1 byte in the sealed evidence bundle triggers instant cryptographic validation failure.

---

## Clean-Room & Privacy Compliance

Threefold uses **100% synthetic development workloads** ("Acme DevCo - Core Services"). Zero proprietary corporate data, zero private repository identifiers, and zero personally identifiable information (PII).
