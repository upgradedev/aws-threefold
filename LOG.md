# Threefold — Activity Log

## 2026-09-19T14:50:00+03:00 — Project Scaffolding
- Initialized Threefold repository under `repos/threefold`.
- Created four-file protocol: `CLAUDE.md`, `STATE.md`, `LOG.md`, `TRAPS.md`.
- Target: AWS Zero to Shipped Hackathon, Track: `#workplace-efficiency`, Lane: `#community`.
- Claimed Phase 1: Clean Architecture scaffolding, pure domain models, deterministic cost circuit breaker, loop detector, architectural boundary guard, and hermetic unit tests.
- Claimed agent: Antigravity.

## 2026-09-19T14:56:00+03:00 — Core Implementation & Test Pyramid
- Implemented pure Domain layer: `AgentSession`, `ToolInvocation`, `TokenUsage`, `TokenCostCalculator`, `CostCircuitBreaker`, `LoopDetector`, `ArchitecturalBoundaryGuard`, and `SecretScanner`.
- Implemented Application layer: `GovernanceEvaluator`, `BedrockArchitecturalReviewer`, and `AuditIssuer`.
- Implemented Infrastructure layer: `BedrockGovernanceClient` (Converse API), `EvidenceStore` (SHA-256 dossiers).
- Implemented Interfaces layer: `api_handlers.py` (AWS Lambda proxy handler with 6 REST operations).
- Constructed test pyramid: 23 tests across unit, integration, and security (tamper-resistance, clean-room invariant). 23/23 passed in 0.45s.

## 2026-09-19T14:58:00+03:00 — Web UI & Hackathon Documentation
- Created responsive dark-mode web dashboard (`web/index.html`) using CDN Tailwind CSS and Lucide icons.
- Created standalone self-verifying browser testbook (`web/testbook.html`) for hackathon judges with 5/5 green gates.
- Authored comprehensive documentation suite:
  - `docs/ARCHITECTURE.md`: Deep technical design, C4 container diagram, sequence flows, and algorithmic specifications.
  - `docs/WELL_ARCHITECTED.md`: Mapping across 6 AWS Well-Architected pillars + 2026 Agentic AI Lens.
  - `docs/PROOF_OF_AWS_AGENT.md`: Verifiable coding agent traces, tool calls, and execution environment.
  - `docs/BUILDER_CENTER_ARTICLE.md`: Publication article draft for AWS Builder Center.
- Created AWS SAM IaC template (`deploy/template.yml`) specifying ARM64 Graviton Lambdas, HTTP API Gateway, DynamoDB, and S3.

## 2026-09-20T19:58:00+03:00 — Operator console, three pages, and the deploy that carried them
- Added `src/threefold/web/{settings,sessions,connect}.html`, registered in `WEB_ASSETS`, and linked from the dashboard header. Each page reads the live API and shows nothing it did not obtain from it.
- Found and fixed four defects the pages exposed: `/sessions/{id}` never URL-decoded the path; the function role had no `dynamodb:Scan`, so the sessions listing fell back to one container's memory; the evaluator built its breaker with a $2.50 cap while `/policy/config` reported $1.00; `Idempotency-Key` was missing from the API's allowed CORS headers.
- Labelled `max_session_budget_usd` and `loop_history_window` on the settings page as stored but unenforced, because no gate reads them. Recorded as gap 1 in STATE.md.
- Dev server switched to `ThreadingHTTPServer`: an open browser tab no longer blocks every other request on port 8001.
- Tests 111 → 117. Deployed commit `b22db34` to `threefold-prod` in eu-west-1 with `cloudformation package` and `deploy`, the same commands the unrun pipeline uses. Verified on the live URL: three pages serve with the API base substituted, the listing returned 43 rows from the table to a cold container, the third identical call still halts, and a halted session still refuses unrelated work.

## 2026-09-20T20:35:00+03:00 — The OpenAPI link serves the contract
- Moved `openapi.json` from `docs/` to `src/threefold/web/`, inside the `CodeUri` the package is built from. The deployed handler had been falling through to a two-line placeholder on every request, which Swagger UI rendered as an API with no operations. The placeholder is deleted; a missing file now answers an RFC 7807 problem instead of impersonating a spec.
- `swagger.html` took the injected base path. It had asked for `location.origin + '/openapi.json'`, which misses `/prod` and answers 404.
- Corrected two false statements inside the document itself: it named Claude 3.5 Sonnet while the stack runs Haiku 4.5, and its only server was `127.0.0.1:8001`, so a judge pressing Try it out called their own laptop. A test now reads the model family out of `deploy/template.yml` and fails if the document drifts.
- The Dockerfile copied the old path and would no longer have built.
- Tests 117 → 124. Deployed and verified on the live URL: eleven paths served, twelve operations rendered, no console error.
