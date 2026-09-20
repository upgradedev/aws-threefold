# Threefold

**Threefold refuses a coding agent's edit the moment it is made, not after the commit, so your architecture does not rot while you sleep.**

Built for the AWS Zero to Shipped hackathon. **Category:** `#workplace-efficiency` · **Lane:** `#community`

[![CI](https://github.com/upgradedev/threefold-aws/actions/workflows/ci.yml/badge.svg)](https://github.com/upgradedev/threefold-aws/actions/workflows/ci.yml)
[![Deploy](https://github.com/upgradedev/threefold-aws/actions/workflows/deploy.yml/badge.svg)](https://github.com/upgradedev/threefold-aws/actions/workflows/deploy.yml)
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
that merely mentions the rule is not. When the answer is no, the tool call does
not happen.

```
$ cat call.json | python3 claude_code_hook.py
{"permissionDecision": "deny",
 "permissionDecisionReason": "Threefold refused this call. Clean Architecture
  violation: domain file 'src/domain/user.py' cannot depend on an outer layer
  (from boto3 import ...)"}
```

That is a real Claude Code hook against the live service, not a mock. Point
your own agent at it with [`claude_code_hook.py`](src/threefold/hooks/claude_code_hook.py),
which the deployment serves at
<https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/hooks/claude_code_hook.py>.

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

## 4 Guided User Journeys (Zero-Setup Live Demo)

Open <https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/> and press a button. The same page is in the repository at [`src/threefold/web/index.html`](src/threefold/web/index.html), which the Lambda serves.

Three more pages are served beside it, each one reading the live API rather than
a fixture: [`/sessions.html`](src/threefold/web/sessions.html) lists the sessions
the service has actually governed and reads any one of them back,
[`/settings.html`](src/threefold/web/settings.html) reads and writes the policy
thresholds the gates enforce, and [`/connect.html`](src/threefold/web/connect.html)
is the installation path for [`claude_code_hook.py`](src/threefold/hooks/claude_code_hook.py),
which puts Threefold in front of a real Claude Code session.

1. **Journey 1 · Runaway Tool Loop Interception:**  
   Click **Runaway Tool Loop**. The simulator sends 3 identical tool calls. Threefold detects the monomorphic loop on iteration 3, instantly trips the circuit breaker, locks the session, and halts token expenditure.
2. **Journey 2 · Secret Leakage Pre-Invocation Rejection:**  
   Click **Secret Leak Intercept**. An agent attempts to execute a shell command exporting an AWS Access Key (`AKIAIOSFODNN7EXAMPLE`). Threefold rejects the call before it leaves the local environment.
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

```
$ THREEFOLD_OFFLINE=1 python -m pytest tests -q
134 passed
```

- **Unit Tests:** cost arithmetic, single-invocation caps, monomorphic loops, ping-pong loops, secret regex matching, and Clean Architecture imports.
- **Integration Tests:** AWS Lambda API Gateway proxy routing, query normalization, and error handling.
- **Security & Tamper Tests:** Negative-control proving mutating 1 byte in the sealed evidence bundle triggers instant cryptographic validation failure.

---

## Clean-Room & Privacy Compliance

Threefold uses **100% synthetic development workloads** ("Acme DevCo - Core Services"). Zero proprietary corporate data, zero private repository identifiers, and zero personally identifiable information (PII).
