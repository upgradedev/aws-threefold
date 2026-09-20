# Taming Autonomous Coding Agents: How We Built Threefold with Amazon Bedrock and AWS Serverless

**Published for:** AWS Builder Center  
**Hackathon:** AWS Zero to Shipped 2026  
**Category:** `#workplace-efficiency` · **Lane:** `#community`  
**GitHub Repository:** `https://github.com/upgradedev/threefold-aws`

---

## Introduction: The Hidden Risk of Autonomous Coding Agents

In 2026, AI coding agents are no longer just completion tools—they are autonomous workers executing terminal commands, editing multi-file codebases, and orchestrating complex deployments. Using foundation models like **Anthropic Claude 3.5 Sonnet on Amazon Bedrock**, agents can solve difficult engineering problems in minutes.

However, as software teams scale autonomous agent usage, engineering managers and developers face two dangerous failure modes:

1. **The Infinite Thrashing Loop:** When an agent encounters a broken test or syntax error, it can enter a recursive edit-test loop, repeating the same tool actions and burning hundreds of thousands of tokens (and hundreds of cloud dollars) before a developer notices.
2. **Architectural Drift & Secret Ingestion:** Agents lack intrinsic awareness of enterprise boundaries. They will casually inspect `.env` files, read AWS credentials into prompt contexts, or introduce forbidden infrastructure packages into clean domain models.

To solve this, we built **Threefold**: an autonomous governance proxy, real-time cost circuit-breaker, and architectural compliance sidecar powered by **Amazon Bedrock** and **AWS Serverless**.

---

## The Core Philosophy: "Deterministic Code Trips the Breaker; Bedrock Explains Why"

When building guardrails for autonomous agents, a common mistake is using another prompt to check the first prompt. LLM-based guardrails suffer from non-zero latency, non-deterministic outputs, and token costs of their own.

Threefold adopts an **air-gapped hybrid architecture**:
1. **Deterministic perimeter:** every agent tool call is intercepted by standard-library Python running four checks, with no model on the critical path:
   - *Secret Scanner:* Blocks AWS Access Keys (`AKIA...`), GitHub tokens, and private keys at the argument boundary.
   - *Boundary Guard:* Prohibits reading `.env` files or importing outer-layer dependencies into pure domain code.
   - *Loop & Thrashing Detector:* Hashes tool calls into N-gram signatures to detect monomorphic loops ($\ge 3$ identical calls) or ping-pong thrashing.
   - *Cost Circuit Breaker:* Calculates exact USD spend using tiered model pricing ($3/M in, $15/M out) and trips the breaker if budget ceilings are breached.
2. **Cognitive Explanation (Amazon Bedrock):** When actions are approved or blocked, **Claude 3.5 Sonnet via the Bedrock Converse API** analyzes the architectural trade-offs, providing clear, human-readable explanations to the engineering lead.

```
 ┌──────────────────────┐          ┌──────────────────────┐          ┌──────────────────────┐
 │ Autonomous Coding    │          │ Deterministic Safety │          │ Amazon Bedrock       │
 │ Agent (Tool Call)    │─────────►│ Gatekeeper (no model)│─────────►│ (Claude Haiku 4.5)   │
 └──────────────────────┘          └──────────┬───────────┘          └──────────┬───────────┘
                                              │                                 │
                                      Evaluates Secrets,               Synthesizes Plain-
                                      Loops, Budgets & Arch            Language Architecture
                                              │                               Review
                                              ▼                                 │
                                   ┌──────────────────────┐                     ▼
                                   │  Circuit Breaker     │          ┌──────────────────────┐
                                   │  (Tripped or Safe)   │          │ Signed SHA-256 Cert  │
                                   └──────────────────────┘          │ for CI/CD Gates      │
                                                                     └──────────────────────┘
```

---

## Technical Highlights

### 1. Amazon Bedrock Converse API Integration
We integrated the Bedrock Converse API to deliver high-level architectural assessments of agent actions. By isolating the LLM from direct hardware actuation, we ensure the agent can never bypass its own governance rules.

```python
# Extract from src/threefold/infrastructure/bedrock_client.py
response = bedrock_runtime.converse(
    modelId="anthropic.claude-3-5-sonnet-20241022-v2:0",
    messages=[{"role": "user", "content": [{"text": user_content}]}],
    system=[{"text": "You are Threefold, an autonomous software governance agent..."}],
    inferenceConfig={"maxTokens": 256, "temperature": 0.2}
)
```

### 2. ARM64 Graviton2 Serverless Architecture
Threefold runs on **AWS Lambda** (arm64) behind an **Amazon API Gateway HTTP API**. The deterministic gates hold no model call, so a blocked call never waits on Bedrock; only the explanation for an allowed call does. Neither cold start nor gate latency has been measured, so neither is quoted here.

### 3. Cryptographic Governance Certificates
When a coding agent completes a compliant task, Threefold issues a signed **Governance Certificate** with a 64-character SHA-256 fingerprint:
```json
{
  "certificate_id": "CERT-TF-9F4B18A72C3D",
  "session_id": "session-compliant-01",
  "verdict_status": "COMPLIANT_APPROVED",
  "total_cost_usd": 0.0384,
  "all_passed": true,
  "sha256_fingerprint": "e58b5f39c2d1b82736e4f3a1d95018b26182c0b471928374a56b2c81928374fa"
}
```
This certificate is verified by CI/CD pipelines as a required status check before any pull request is eligible for merging.

---

## The 4 Guided User Journeys (Try It Live)

Open [`src/threefold/web/index.html`](../src/threefold/web/index.html) or open <https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/>:

1. **Journey 1 · Runaway Tool Loop:** Watch the simulator fire 3 identical tool calls. On iteration 3, the circuit breaker instantly trips with a red visual alert, freezing the session and preserving your budget.
2. **Journey 2 · Secret Leakage Intercept:** Simulate an agent passing an AWS Access Key (`AKIAIOSFODNN7EXAMPLE`) in a command argument. Blocked instantly at the perimeter before leaving the machine.
3. **Journey 3 · Clean Architecture Guard:** Simulate an agent attempting to inject `import boto3` into a domain aggregate. Blocked with a dependency inversion error.
4. **Journey 4 · Compliant Execution & Signed Certificate:** Run 4 safe development operations. Review Bedrock's architectural commentary and export the signed SHA-256 certificate to JSON.

---

## Key Takeaways for AWS Builders

1. **Hard limits belong in deterministic code:** Do not rely on prompt engineering to enforce financial or security boundaries. Hard circuit breakers must be deterministic.
2. **Agent governance enables autonomy:** Rather than restricting developers from using coding agents, automated governance gives teams the confidence to grant agents more autonomy while ensuring safety.
3. **Serverless is the ideal governance layer:** Threefold costs **$0.00/month** when developers are offline, scaling instantly to govern thousands of concurrent agent tool calls during peak development hours.

---

## Experience Threefold

- **Repository:** [`github.com/upgradedev/threefold-aws`](https://github.com/upgradedev/threefold-aws)
- **Hermetic Test Suite:** 65 tests passing in under a second (`python -m pytest repos/threefold/tests -v`).
- **Interactive Live Dashboard:** Open `src/threefold/web/index.html` in any browser. Zero installation required.
