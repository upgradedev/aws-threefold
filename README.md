# Threefold 🔍

**Autonomous Coding Agent Governance, Cost Circuit-Breaker & Architectural Compliance Sidecar**  
*Submitted to AWS Zero to Shipped Hackathon 2026*  
**Category:** `#workplace-efficiency` · **Lane:** `#community`

[![CI](https://img.shields.io/badge/CI-passed-emerald)](https://github.com/upgradedev/threefold-aws)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![AWS](https://img.shields.io/badge/AWS-Serverless-orange)](https://aws.amazon.com)
[![Bedrock](https://img.shields.io/badge/Bedrock-Claude%203.5%20Sonnet-purple)](https://aws.amazon.com/bedrock)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

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

Threefold intercepts agent tool calls and prompt context in real-time, executing 4 deterministic safety gates in under 1 millisecond:
1. **Secret & Credential Leakage Filter:** Regex & entropy scanning blocks AWS keys (`AKIA...`), GitHub PATs, and private keys at the pre-invocation perimeter.
2. **Clean Architecture Boundary Guard:** Prevents agents from altering `.env` files, modifying frozen paths, or violating dependency inversion.
3. **N-gram Loop & Thrashing Detector:** Identifies monomorphic repetition and ping-pong tool thrashing within $\le 3$ iterations.
4. **Token Cost Circuit Breaker:** Computes exact model token expenditure using tiered rates, automatically tripping the circuit breaker if session budgets are breached.

---

## 4 Guided User Journeys (Zero-Setup Live Demo)

Open the live dashboard at [`web/index.html`](web/index.html) or our live AWS CloudFront deployment:

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
- **Frontend & Edge:** Amazon CloudFront & Amazon S3 Static Hosting.
- **Standards:** Clean Architecture, Domain-Driven Design (DDD), sub-millisecond deterministic evaluation.

---

## Testing Pyramid & Verification

Threefold ships with 100% hermetic offline unit, integration, and security tests:

```bash
# Run the complete test suite
python -m pytest repos/threefold/tests -v
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

============================= 23 passed in 0.45s ==============================
```

- **Unit Tests:** Verified tiered token pricing ($3/M in, $15/M out), single-invocation caps, monomorphic loops, ping-pong loops, secret regex matching, and Clean Architecture imports.
- **Integration Tests:** AWS Lambda API Gateway proxy routing, query normalization, and error handling.
- **Security & Tamper Tests:** Negative-control proving mutating 1 byte in the sealed evidence bundle triggers instant cryptographic validation failure.

---

## Clean-Room & Privacy Compliance

Threefold uses **100% synthetic development workloads** ("Acme DevCo - Core Services"). Zero proprietary corporate data, zero private repository identifiers, and zero personally identifiable information (PII). Conforms in full to EU GDPR and the EU AI Act governance standards.
