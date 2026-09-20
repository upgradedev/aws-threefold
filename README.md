# Threefold 🔍

**Autonomous Coding Agent Governance, Cost Circuit-Breaker & Architectural Compliance Sidecar**  
Built for the AWS Zero to Shipped hackathon. **Category:** `#workplace-efficiency` · **Lane:** `#community`

[![CI](https://github.com/upgradedev/threefold-aws/actions/workflows/ci.yml/badge.svg)](https://github.com/upgradedev/threefold-aws/actions/workflows/ci.yml)
[![Deploy](https://github.com/upgradedev/threefold-aws/actions/workflows/deploy.yml/badge.svg)](https://github.com/upgradedev/threefold-aws/actions/workflows/deploy.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

**Live: <https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/>** — open it and press a scenario. No account, no key, nothing to install.

---

## The Problem

As enterprise engineering teams and open-source contributors increasingly integrate autonomous coding agents (Claude 3.5 Sonnet, AWS Q Developer, custom LLM tool loops) directly into their terminal workflows and CI/CD pipelines, three severe engineering bottlenecks emerge:

1. **Token Cost Runaways & Looping Traps:** Autonomous agents caught in repetitive edit-test-fail cycles consume tens of thousands of tokens and hundreds of cloud dollars within minutes.
2. **Architectural Drift & Boundary Violations:** Coding agents inadvertently modify frozen core architecture, bypass Clean Architecture rules (e.g. importing infrastructure into pure domain entities), or edit unauthorized sensitive directories.
3. **Secret & Credential Leakage:** Agents reading local project directories can accidentally ingest `.env` files or API keys and stream them into model prompts or pull request commits.
4. **Lack of Cryptographic Auditability in CI/CD:** Teams have no verifiable proof of what tools were executed, what safety invariants passed, and whether costs were bounded before code is merged.

---

## The Threefold Solution

**Threefold** is an autonomous governance proxy, cost circuit-breaker, and compliance engine:

> **The Central Tenet:** *Deterministic code trips circuit breakers and enforces boundaries; Amazon Bedrock provides semantic architectural explanations.*

Threefold intercepts an agent's intended tool call and runs four deterministic gates before the call is allowed to proceed:
1. **Secret and credential filter:** five regular expressions block AWS keys (`AKIA...`), GitHub PATs, and private keys at the pre-invocation perimeter.
2. **Clean Architecture Boundary Guard:** Prevents agents from altering `.env` files, modifying frozen paths, or violating dependency inversion.
3. **N-gram Loop & Thrashing Detector:** Identifies monomorphic repetition and ping-pong tool thrashing within $\le 3$ iterations.
4. **Token Cost Circuit Breaker:** Computes exact model token expenditure using tiered rates, automatically tripping the circuit breaker if session budgets are breached.

---

## 4 Guided User Journeys (Zero-Setup Live Demo)

Open <https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/> and press a button. The same page is in the repository at [`src/threefold/web/index.html`](src/threefold/web/index.html), which the Lambda serves.

1. **Journey 1 · Runaway Tool Loop Interception:**  
   Click **Runaway Tool Loop**. The simulator sends 3 identical tool calls. Threefold detects the monomorphic loop on iteration 3, instantly trips the circuit breaker, locks the session, and halts token expenditure.
2. **Journey 2 · Secret Leakage Pre-Invocation Rejection:**  
   Click **Secret Leak Intercept**. An agent attempts to execute a shell command exporting an AWS Access Key (`AKIAIOSFODNN7EXAMPLE`). Threefold rejects the call before it leaves the local environment.
3. **Journey 3 · Clean Architecture Drift Prevention:**  
   Click **Clean Architecture Drift**. An agent attempts to write `import boto3` inside `src/domain/user.py`. Threefold blocks the write with a Clean Architecture violation alert.
4. **Journey 4 · Compliant Execution & Signed Certificate:**  
   Click **Compliant Run & Cert**. 4 legitimate tool calls execute within budget ($0.0384). Amazon Bedrock provides architectural commentary, and the system issues an immutable SHA-256 **Governance Certificate** exportable to JSON.

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
        │  Execution Aborted      │                       │ (Claude 3.5 Sonnet)     │
        └─────────────────────────┘                       └────────────┬────────────┘
                                                                       │
                                                                       ▼
                                                          ┌─────────────────────────┐
                                                          │ SHA-256 Audit Cert      │
                                                          │ (DynamoDB & S3 Sealed)  │
                                                          └─────────────────────────┘
```

---

## Technology Stack & AWS Services

- **Reasoning Engine:** Amazon Bedrock (Anthropic Claude 3.5 Sonnet via Converse API).
- **Serverless Compute:** AWS Lambda (Python 3.11 on ARM64 Graviton2).
- **Ingress & API:** Amazon API Gateway HTTP API with full CORS support.
- **Audit Storage:** Amazon DynamoDB (Single-table session state) & Amazon S3 (Cryptographic evidence bundles).
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
============================= test session starts =============================
collected 23 items

tests/integration/test_api_handlers.py ......                            [ 26%]
tests/security/test_tamper_and_invariants.py ..                          [ 34%]
tests/unit/test_boundary_guard.py ....                                   [ 52%]
tests/unit/test_circuit_breaker.py ....                                  [ 69%]
tests/unit/test_evaluator.py ...                                         [ 82%]
tests/unit/test_loop_detector.py ....                                    [100%]

============================= 23 passed in under a second ==============================
```

- **Unit Tests:** Verified tiered token pricing ($3/M in, $15/M out), single-invocation caps, monomorphic loops, ping-pong loops, secret regex matching, and Clean Architecture imports.
- **Integration Tests:** AWS Lambda API Gateway proxy routing, query normalization, and error handling.
- **Security & Tamper Tests:** Negative-control proving mutating 1 byte in the sealed evidence bundle triggers instant cryptographic validation failure.

---

## Clean-Room & Privacy Compliance

Threefold uses **100% synthetic development workloads** ("Acme DevCo - Core Services"). Zero proprietary corporate data, zero private repository identifiers, and zero personally identifiable information (PII).
