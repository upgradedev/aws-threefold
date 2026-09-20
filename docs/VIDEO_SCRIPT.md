# Threefold: 3-Minute Demo Video Script & Production Guide

**Hackathon Track:** `#workplace-efficiency` (Developer Productivity & AI Governance)  
**Lane:** `#community`  
**Target Duration:** 2 minutes 45 seconds (< 180 seconds hard cap)  
**Host / Presenter:** Engineering Lead / Builder  
**URL:** `https://github.com/upgradedev/threefold-aws`  

---

## Video Timeline & Scene Breakdown

### Scene 1: The Problem — Autonomous Coding Agent Thrashing & Runaway Costs (0:00 - 0:25)
- **Visual:**
  - Full screen title slide: *"Threefold: Serverless Real-Time Governance, Cost Circuit Breakers & Invariant Enforcement for AI Coding Agents"*.
  - Cut to terminal showing an autonomous coding agent trapped in an infinite loop: repeated tool calls, token usage climbing rapidly, and cloud bills spiking.
- **Narration (Spoken):**
  > "Autonomous coding agents are revolutionizing software development. But when left ungoverned, agents can enter destructive thrashing loops—endlessly editing the same file, burning through thousands of dollars in LLM tokens in minutes, or accidentally leaking sensitive AWS credentials into public repositories.
  > Today's teams are forced to choose between completely manual code review or risking runaway agent execution.
  > We built **Threefold** to give engineering teams real-time, deterministic governance over any AI coding agent."

---

### Scene 2: The Architectural Axiom — "Deterministic Code Trips the Breaker, Bedrock Explains Why" (0:25 - 0:55)
- **Visual:**
  - Architecture diagram showing:
    `Agent Tool Invocation -> Universal Adapter (OpenAI / Anthropic) -> Deterministic Safety Invariants (Tokens, Loops, Boundaries, Secrets) -> Amazon Bedrock Reviewer -> DynamoDB & S3 Governance Certificate`.
  - Highlight that the deterministic gate decides, and Bedrock only explains the decision afterwards.
- **Narration (Spoken):**
  > "Threefold is built on a clear architectural principle: *Deterministic code trips the circuit breaker; Amazon Bedrock explains why.*
  > All agent tool calls pass through an ultra-fast, air-gapped governance layer in Python that enforces four critical invariants:
  > One: Token Budget Ceilings.
  > Two: N-Gram Thrashing & Loop Detection.
  > Three: Clean Architecture Layer Boundaries.
  > And Four: Pre-Invocation Secret Leakage Prevention.
  > If an invariant is breached, execution halts instantly. Then, Amazon Bedrock analyzes the incident and delivers actionable, architectural explanations to the developer."

---

### Scene 3: Live Application Walkthrough & Threat Interceptions (0:55 - 1:40)
- **Visual:**
  - Screen capture of the live web interface (<https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/> / `src/threefold/web/index.html`).
  - Show the green **"BACKEND ONLINE (v1.0.0)"** status badge.
  - Click **"Scenario 1: Thrashing Loop Intercept"**:
    - Terminal shows repeated calls to `edit_file("src/service.py")`.
    - At call #3, the circuit breaker trips. Status changes to red: `BLOCKED_LOOP_DETECTED`.
    - Bedrock explanation card displays: *"Agent thrashing detected... execution halted to prevent unbounded token consumption."*
  - Click **"Scenario 2: Secret Leak Intercept"**:
    - Agent attempts to execute `export AWS_ACCESS_KEY_ID=AKIA...`.
    - Gate instantly blocks it: `BLOCKED_SECRET_DETECTED`.
- **Narration (Spoken):**
  > "Here is Threefold running live on our server, intercepting real tool calls.
  > In Scenario 1, an agent gets stuck in a recursive loop modifying the same file. On the third repeated call, our N-gram loop detector trips the circuit breaker, stopping the session before budget is wasted. Amazon Bedrock immediately explains the root cause.
  > In Scenario 2, an agent attempts to execute a shell command containing an AWS Access Key. Threefold catches the credential in pre-invocation and neutralizes the leak before it ever leaves the developer's workstation."

---

### Scene 4: Universal Multi-Agent Adapter & Governance Certificate (1:40 - 2:15)
- **Visual:**
  - Click **"Scenario 5: Universal Adapter (OpenAI / Anthropic)"**:
    - Show real OpenAI `function_call` payload being ingested and normalized into domain entities.
    - Show Anthropic `tool_use` payload being approved.
  - Click **"Scenario 4: Compliant Run & Cert"**:
    - 4 safe, well-architected tool calls execute.
    - Budget remaining updates smoothly ($14.92 / $15.00).
    - Status turns green: `APPROVED`.
    - Click **"📥 Export Governance Certificate"**.
    - Open the downloaded JSON certificate showing the SHA-256 seal and list of verified invariants.
- **Narration (Spoken):**
  > "Threefold is model-agnostic. With our Universal Multi-Agent Adapter, you can protect Claude Code, Cursor, Copilot, or OpenAI Swarm agents using their native payload schemas.
  > When an agent operates safely within boundaries, Threefold tracks the cost it was told about and issues a SHA-256 fingerprinted **Governance Certificate**.
  > Teams can verify this certificate directly in their CI/CD deployment pipelines using our zero-dependency pre-commit hook script."

---

### Scene 5: Production Engineering, CloudWatch EMF, & Conclusion (2:15 - 2:45)
- **Visual:**
  - Switch to terminal. Run `THREEFOLD_OFFLINE=1 python -m pytest tests -q` showing all **65 tests passing**.
  - Show Swagger UI at `src/threefold/web/swagger.html` with OpenAPI 3.1 endpoints.
  - Show CloudWatch Embedded Metric Format (EMF) logs streaming in the terminal (`Threefold/Governance`).
  - Return to slide with GitHub repository link: `https://github.com/upgradedev/threefold-aws`.
- **Narration (Spoken):**
  > "Threefold is engineered for enterprise production: zero-trust token-bucket rate limiting, full jitter resilience, OpenAPI 3.1 specs, and real-time CloudWatch Embedded Metric Format telemetry.
  > Our automated test pyramid includes 65 hermetic unit, integration, and security tests—running clean-room with zero external dependencies.
  > Give your developers the superpower of autonomous AI agents—with the safety, cost control, and architectural integrity of Threefold.
  > Thank you, and explore our repository on GitHub!"

---

## Screen Recording Checklist

- [ ] Browser window 1: <https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/> (Threefold Interactive Console)
- [ ] Browser window 2: `https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/swagger.html` (Swagger UI)
- [ ] Terminal window: split pane showing pytest suite (`32 passed`), pre-commit hook execution, and live CloudWatch EMF logs
- [ ] Audio: crisp microphone recording matching the scene timings
- [ ] Final video duration: 2 minutes 35 seconds to 2 minutes 45 seconds (strictly < 3:00)
