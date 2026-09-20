# Threefold: AWS Zero to Shipped Hackathon Submission Dossier

**Target Category:** `#workplace-efficiency` (Developer Productivity & AI Governance)  
**Target Lane:** `#community`  
**Application Name:** Threefold  
**Tagline / Elevator Pitch:** Serverless real-time governance proxy, token cost circuit breakers, and architectural invariant enforcement for autonomous AI coding agents on AWS.  
**Public Repository URL:** `https://github.com/upgradedev/threefold-aws`  
**Live Application URL:** <https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/>  
**Article Published URL:** not published yet  
**Demo Video URL:** not recorded yet  

---

## Submission Portal Field-by-Field Answers

### 1. Project Description (Short Summary)
Threefold is a high-performance, serverless governance proxy and architectural guardrail engine for autonomous AI coding agents (Claude Code, Cursor, Copilot, OpenAI Swarm). Operating on the philosophy that *"Deterministic code trips the circuit breaker; Amazon Bedrock explains why,"* Threefold intercepts tool invocations before execution, evaluating four critical invariants: token-cost budgets, recursive thrashing loops, Clean Architecture layer boundaries, and credential leakage. When compliant, it tracks live dollar spend and issues an immutable, SHA-256 fingerprinted Governance Certificate archived in Amazon S3 and DynamoDB, verifiable via a zero-dependency git pre-commit hook in CI/CD deployment pipelines.

---

### 2. Inspiration
Autonomous coding agents are rapidly transforming enterprise software engineering. However, without guardrails, autonomous agents introduce substantial financial and security risks: recursive thrashing loops where an agent endlessly modifies the same lines of code, runaway token usage consuming thousands of dollars in cloud API bills, and catastrophic credential leakage when an agent outputs or commits secret keys into repositories. Engineering leaders were caught between two bad choices: either severely restrict agent autonomy with slow manual approvals, or accept uncontrolled financial and architectural risk. We built Threefold to provide real-time, deterministic, automated safety without slowing down developer velocity.

---

### 3. What It Does
1. **Tool interception and invariant verification:** intercepts an agent's intended tool request (file edits, shell execution, queries) and checks it against four deterministic rules before it runs.
2. **Cost Circuit Breaker & Real-Time Spend Tracking:** Models exact token-to-dollar pricing for foundation models, enforcing hard session budget caps and blocking single-invocation spend spikes.
3. **N-Gram Thrashing & Infinite Loop Detection:** Tracks recent invocation sequences using monomorphic and ping-pong N-gram pattern matching, tripping the circuit breaker before tokens are burned.
4. **Architectural Boundary & Secret Scanner:** Enforces Clean Architecture dependency rules (e.g., domain entities cannot import outer infrastructure frameworks) and blocks sensitive API keys or credentials from being committed or piped to shell commands.
5. **Amazon Bedrock Architectural Explanations:** When a safety rule trips, Amazon Bedrock (Claude Haiku 4.5) reviews the incident and generates plain-language, contextual advice for the developer.
6. **Universal Multi-Agent Adapter:** Seamlessly parses both native OpenAI `function_call` and Anthropic `tool_use` schemas, enabling universal compatibility with any coding agent.
7. **Fingerprinted Governance Certificates & Git Pre-Commit Hook:** Issues SHA-256 fingerprinted audit records and includes a standalone pre-commit hook script (`scripts/pre-commit-gate.py`) for CI/CD gates.

---

### 4. How We Built It
- **Architecture:** Clean Architecture & Domain-Driven Design (DDD) with strict layer boundaries: Domain Core, Application Evaluators, Infrastructure Adapters, and Interfaces.
- **AI Reasoning:** Amazon Bedrock Claude Haiku 4.5 (`eu.anthropic.claude-haiku-4-5-20251001-v1:0`) accessed via the Bedrock Converse API for contextual incident explanation.
- **State & Evidence Persistence:** Amazon DynamoDB single-table design with session history and budget attributes, and Amazon S3 for governance certificate bundles.
- **Observability:** Real-time AWS CloudWatch Embedded Metric Format (EMF) emitting zero-overhead structured telemetry directly to stdout.
- **Security & Resilience:** Zero-trust token-bucket rate limiting (60 req/min), API Key authentication, RFC 7807 Problem Details, and exponential backoff with full jitter retry decorators.
- **API & UI:** Complete OpenAPI 3.1 specifications, interactive Swagger UI (`src/threefold/web/swagger.html`), and a responsive single-page console (`src/threefold/web/index.html`) using vanilla JavaScript with zero external npm build dependencies.
- **Infrastructure as Code & CI/CD:** AWS SAM (`template.yml`), multi-stage Dockerfiles, Docker Compose, and GitHub Actions CI running 65 tests.

---

### 5. Challenges We Overcame
1. **No model on the critical path:** the whole invariant engine is standard-library Python, so a refusal never waits on an LLM. The overhead has not been benchmarked, so no figure is claimed.
2. **Universal Multi-Agent Compatibility:** Different AI models use conflicting tool-calling representations (OpenAI's JSON-string `arguments` vs. Anthropic's structured `input` object). We developed a universal normalization adapter that maps any agent payload into unified domain entities.
3. **A dependency-free domain:** the gates and the domain model import nothing outside the standard library, and boto3 is imported lazily in the infrastructure adapters only. The Lambda therefore ships without a build step.

---

### 6. Accomplishments That We're Proud Of
- **Deterministic Prevention of Cost Runaways:** Created an automated circuit breaker that refuses a call whose declared cost would breach the cap. It trusts the token counts the caller declares, so a caller that declares zero is not stopped.
- **100% Green Automated Test Pyramid:** Built a comprehensive 65-test hermetic test suite covering unit, integration, universal adapter, and security tests.
- **Turnkey Production Readiness:** Delivered production Docker containers, OpenAPI 3.1 specs, Swagger UI, CloudWatch EMF metrics, and pre-commit hook gating.

---

### 7. What We Learned
- How to decouple deterministic invariant checking from asynchronous generative explanation to achieve both high speed and deep semantic context.
- The critical role of token-bucket rate limiting and RFC 7807 problem details in building zero-trust serverless APIs.
- How to design zero-dependency developer tooling that integrates seamlessly into git hooks and enterprise CI/CD pipelines.

---

### 8. What's Next for Threefold
- Native IDE plugins for VS Code, Cursor, and JetBrains for inline visual circuit-breaker status.
- Team-wide shared DynamoDB budget pools with organization-level hierarchy and RBAC.
- Semantic drift detection comparing agent diffs against high-level architectural design documents.
