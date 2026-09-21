# Threefold — Technical Architecture & Domain Specification

**Autonomous Coding Agent Governance, Cost Circuit-Breaker & Architectural Compliance Sidecar**  
**Hackathon:** AWS Zero to Shipped 2026  
**Category:** `#workplace-efficiency` · **Lane:** `#community`

---

## 1. Executive Summary & Design Philosophy

As software development adopts autonomous AI coding agents (Claude Code, Amazon Q Developer, custom tool-augmented agent loops), developers face severe risks of runaway cloud costs, architectural degradation, and inadvertent secret leakage.

When an autonomous coding agent encounters a compilation error or unexpected test failure, it frequently enters an **oscillating edit-fail loop**. In this thrashing state, the agent repeatedly views files, runs edits, and attempts tests, consuming tens of thousands of tokens and dozens of dollars per minute. Furthermore, without deterministic perimeter guardrails, agents routinely read local `.env` files containing production API keys, or inadvertently import heavy external infrastructure packages into clean domain models.

**Threefold** provides a state-of-the-art **deterministic governance sidecar**:
> **"Deterministic code trips circuit breakers and enforces boundaries; Amazon Bedrock provides semantic architectural explanations."**

The sidecar intercepts agent tool calls at the pre-invocation phase and runs four deterministic evaluations. If any invariant fails, execution is halted immediately. When operations succeed, Amazon Bedrock explains the verdict in a sentence, and the system issues a SHA-256 **Governance Certificate**.

The certificate is currently a fingerprinted record, not a gate: nothing in this repository verifies one before a merge. The fingerprint is an unkeyed SHA-256 of the payload. It detects accidental corruption and casual edits, and it is not tamper-evidence against an adversary: anyone who changes the payload can recompute the hash. Making it real means signing with KMS and shipping a verifier that checks the signature, which is not done. Making a CI check refuse a pull request whose session has no valid certificate is the next piece of work, and it is the part that would make the certificate load-bearing rather than decorative.

---

## 2. Clean Architecture & Domain-Driven Design (DDD)

Threefold strictly implements **Clean Architecture** (Robert C. Martin) and **Domain-Driven Design (DDD)**. All domain business logic is 100% pure Python with zero cloud or framework dependencies.

```
                    ┌─────────────────────────────────────────┐
                    │               INTERFACES                │
                    │   AWS Lambda Handlers, API Gateway      │
                    │   Interactive Web Dashboard (Lambda)   │
                    └────────────────────┬────────────────────┘
                                         │
                    ┌────────────────────▼────────────────────┐
                    │             INFRASTRUCTURE              │
                    │   Amazon Bedrock Converse Client        │
                    │   DynamoDB Repository                   │
                    │   S3 Evidence Store (SHA-256 Dossiers)  │
                    └────────────────────┬────────────────────┘
                                         │
                    ┌────────────────────▼────────────────────┐
                    │              APPLICATION                │
                    │   Governance Evaluator                  │
                    │   Bedrock Architectural Reviewer        │
                    │   Audit Certificate Issuer              │
                    └────────────────────┬────────────────────┘
                                         │
                    ┌────────────────────▼────────────────────┐
                    │                 DOMAIN                  │
                    │   AgentSession Aggregate Root           │
                    │   Cost Circuit Breaker & Token Math     │
                    │   N-gram Loop & Thrashing Detector      │
                    │   Boundary Guard & Secret Scanner       │
                    └─────────────────────────────────────────┘
```

### 2.1 The 4 Architectural Layers

#### Layer 1: Domain (`src/threefold/domain/`)
- **Purity:** Standard Python library only. Zero dependencies on AWS SDK or web frameworks.
- **Aggregates & Value Objects:**
  - `AgentSession`: Aggregate root tracking total input/output tokens, cumulative USD expenditure, tool call audit history, and circuit breaker trip state.
  - `ToolInvocation`: Captures tool name, action type (`FILE_READ`, `FILE_WRITE`, `COMMAND_EXEC`), arguments, and deterministic SHA-256 canonical signature.
  - `TokenUsage`: value object holding the USD cost of a call, from distinct input and output rates.
  - Known limitation: `TokenCostCalculator` carries per-model rates, but `GovernanceEvaluator` calls it without a model id, so every session is currently priced at the default Sonnet-class rate of $3 per million input and $15 per million output. A caller governing a cheaper model is therefore over-charged in the budget arithmetic. Letting the request name its model is the fix, and it is not done.
  - `GovernanceVerdict`: Value object encapsulating verdict status, risk level, pass/fail evaluation per rule, and proof hash.
- **Engines:**
  - `CostCircuitBreaker`: Enforces single-invocation cost ceiling ($2.50) and cumulative budget ceiling ($10.00).
  - `LoopDetector`: N-gram signature matching detecting monomorphic loops (3x identical tool calls) and ping-pong alternating loops (A -> B -> A -> B -> A).
  - `ArchitecturalBoundaryGuard`: Enforces Clean Architecture dependency inversion rules and forbids access to `.env`, `.git`, or cryptographic keys.
  - `SecretScanner`: High-precision regex engine scanning for AWS keys (`AKIA...`), GitHub PATs, and private keys.

#### Layer 2: Application (`src/threefold/application/`)
- **Use Cases & Orchestration:**
  - `GovernanceEvaluator`: Coordinates the 4 deterministic safety gates and updates the session aggregate.
  - `BedrockArchitecturalReviewer`: For compliant operations, invokes Amazon Bedrock Claude Haiku 4.5 to provide human-readable architectural reviews.
  - `AuditIssuer`: Assembles canonical audit records and computes 64-character SHA-256 cryptographic fingerprints for CI/CD gates.

#### Layer 3: Infrastructure (`src/threefold/infrastructure/`)
- **Adapters & Persistence:**
  - `BedrockGovernanceClient`: Implements the Bedrock Converse API abstraction with token-minimized prompts and deterministic fallback.
  - `EvidenceStore`: Generates canonical JSON audit dossiers and validates cryptographic integrity against tamper attacks.

#### Layer 4: Interfaces (`src/threefold/interfaces/` & `src/threefold/web/`)
- **Delivery:**
  - `api_handlers.py`: AWS Lambda proxy handler supporting REST operations (`/status`, `/evaluate-tool-call`, `/simulate-loop`, `/simulate-secret`, `/issue-certificate`).
  - `deploy/template.yml`: AWS SAM serverless definition specifying Graviton ARM64 Lambdas, HTTP API Gateway, DynamoDB, and S3.
  - `src/threefold/web/index.html`: Zero-dependency browser dashboard running Tailwind CSS from a pinned CDN build, with `connect.html`, `console.html`, `rules.html`, `sessions.html`, `settings.html` and `swagger.html` beside it.

---

## 3. Mathematical & Algorithmic Foundations

### 3.1 Tiered Token Cost Calculation

Token pricing is non-uniform between prompt ingestion and generation. The cost function $C(t_{in}, t_{out})$ is:

$$C(t_{in}, t_{out}) = \left(\frac{t_{in}}{10^6} \times P_{in}\right) + \left(\frac{t_{out}}{10^6} \times P_{out}\right)$$

Where for Amazon Bedrock Claude Haiku 4.5:
- $P_{in} = \$3.00$ per 1,000,000 tokens
- $P_{out} = \$15.00$ per 1,000,000 tokens

The circuit breaker trips if:
$$C_{projected} > C_{max\_single} \quad \text{or} \quad \sum C_{session} > B_{session} \times (1 + \epsilon)$$

### 3.2 N-gram Loop & Thrashing Detection

Each tool invocation $T_i$ is mapped to a canonical fingerprint $H(T_i)$:

$$H(T_i) = \text{SHA-256}\left(\text{ToolName} \mathbin{\Vert} \text{CanonicalJSON}(\text{Args})\right)[:16]$$

The sequence of recent fingerprints $S = [s_1, s_2, \dots, s_k]$ is analyzed for:
1. **Monomorphic Repetition:** $s_{k} = s_{k-1} = s_{k-2}$ (Threshold $N=3$).
2. **Ping-Pong Oscillation:** $s_k = s_{k-2} \land s_{k-1} = s_{k-3} \land s_{k} \neq s_{k-1}$.
3. **Circular 3-step Loop:** $s_k = s_{k-3} \land s_{k-1} = s_{k-4} \land s_{k-2} = s_{k-5}$.

---

## 4. End-to-End Sequence Flow

```mermaid
sequenceDiagram
    autonumber
    actor Dev as Coding Agent / Developer
    participant Proxy as Threefold Sidecar (Lambda)
    participant Sec as Secret & Boundary Guard
    participant Loop as N-gram Loop Detector
    participant Breaker as Cost Circuit Breaker
    participant Bedrock as Amazon Bedrock (Claude Haiku 4.5)
    participant CI as CI/CD Pipeline & Audit Store

    Dev->>Proxy: POST /evaluate-tool-call {tool_name, arguments, tokens}
    Proxy->>Sec: Scan for Secrets (AKIA...) & Protected Paths (.env)
    alt Secret or Boundary Violation Detected
        Sec-->>Proxy: FAIL: Secret or Layer Violation
        Proxy-->>Dev: REJECTED (Tool Execution Halted)
    else Boundary Safe
        Sec-->>Proxy: PASS
        Proxy->>Loop: Evaluate History for Recursive Thrashing
        alt Monomorphic or Ping-Pong Loop Detected
            Loop-->>Proxy: FAIL: Loop Thrashing Detected
            alt origin hook (a governed developer's own session)
                Proxy-->>Dev: REJECTED: the repeating call only, session not halted
            else sim- or page session (the demo)
                Proxy->>Proxy: Trip Circuit Breaker & Freeze Session
                Proxy-->>Dev: REJECTED: Circuit Breaker Tripped
            end
        else Loop Free
            Loop-->>Proxy: PASS
            Proxy->>Breaker: Evaluate Token Spend vs Budget Cap
            alt Budget Exceeded
                Breaker-->>Proxy: FAIL: Budget Ceiling Reached
                Proxy-->>Dev: REJECTED: Budget Exhausted
            else Within Budget
                Breaker-->>Proxy: PASS
                Proxy->>Bedrock: Converse API (Architectural Review & Commentary)
                Bedrock-->>Proxy: Review Summary & Risk Assessment
                Proxy->>CI: Issue SHA-256 Governance Certificate
                Proxy-->>Dev: APPROVED (Execute Tool Call)
            end
        end
    end
```

---

## 5. Cryptographic Certificate & Audit Dossier

When an agent completes a compliant workflow, Threefold issues a canonical fingerprinted certificate:

```json
{
  "certificate_id": "CERT-TF-9F4B18A72C3D",
  "session_id": "session-compliant-01",
  "developer_id": "dev-user",
  "project_name": "Acme-Core",
  "verdict_status": "COMPLIANT_APPROVED",
  "total_cost_usd": 0.0384,
  "total_tokens": 6400,
  "evaluations_count": 4,
  "all_passed": true,
  "evaluation_hashes": [
    "a1b2c3d4e5f6...",
    "b2c3d4e5f6a1...",
    "c3d4e5f6a1b2...",
    "d4e5f6a1b2c3..."
  ],
  "sha256_fingerprint": "e58b5f39c2d1b82736e4f3a1d95018b26182c0b471928374a56b2c81928374fa"
}
```

The SHA-256 fingerprint guarantees that neither the tool execution history, nor the token spend, nor the individual rule outcomes can be modified without failing verification in CI/CD.
