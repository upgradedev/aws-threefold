# Threefold — State Ledger

**Last updated:** 2026-09-20
**Hackathon:** AWS Zero to Shipped, submissions close 2026-10-02 23:59 PDT
**Category:** `#workplace-efficiency` · **Lane:** `#community` (hedge to `#commercial-potential` / `#startup` decided 2026-09-28)
**Entries permitted:** one. The Rules tab, ELIGIBILITY section, reads "Limit one entry per person." Threefold is that entry.
**Active agent claim:** none

## Ship gate

The gate is pass or fail: live on AWS, reachable by a public URL, with documented
proof of a coding agent connected to the AWS console. Judging runs the weeks of
6 and 13 October, so the stack stays up past the submission deadline.

| Requirement | State | Evidence `[PRIMARY]` |
|---|---|---|
| Live on AWS, public URL | **PASS** | `https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/` serves the dashboard itself, 200 and `text/html`, to an anonymous request with no API key. Stack `threefold-prod`, eu-west-1 |
| A visitor can run the demo | **PASS** | Walked in a browser: Scenario 1 dispatched three calls to the live backend, the third returned `BLOCKED_LOOP_DETECTED`, and the panel showed a genuine Haiku 4.5 sentence under the heading "Amazon Bedrock (Claude Haiku 4.5)" |
| Reachable by the AI scorer | **PASS** | `STAGE` is unset so the middleware defaults to `dev` and enforces no key. Verified by unauthenticated request |
| Proof of coding agent connected to AWS | **PASS** | `docs/PROOF_OF_AWS_AGENT.md` rewritten around the real session: the commands run, the two defects AWS surfaced, and the CloudTrail principal. Raw output in `docs/evidence/DEPLOYMENT_2026-09-20.md` |
| Public repository | **BLOCKED, owner action** | Four commits on local `main`, no remote. One command, in `docs/RUNBOOK.md` step 2 |
| Continuous delivery | **WRITTEN, role missing** | `.github/workflows/{ci,deploy,keepalive}.yml`. Deploy assumes `threefold-github-deploy`, which does not exist yet. Policy documents are committed at `deploy/iam/`, creation is `docs/RUNBOOK.md` step 1 |
| Builder Center project, two tags | **NOT DONE** | Owner-gated. Requires Builder Center profile, Join, then the Create Project form |

## What is real, measured today

| Claim | Command |
|---|---|
| Bedrock answers from the function's own role | CloudTrail `Converse` events 12:00:35, 12:00:37 and 12:00:39 UTC, principal `assumed-role/threefold-prod-ThreefoldFunctionRole-JzGN3b6RjC7w`, model `eu.anthropic.claude-haiku-4-5-20251001-v1:0`, no error |
| The third identical call halts the session | Three POSTs to `/evaluate-tool-call`: APPROVED, APPROVED, `BLOCKED_LOOP_DETECTED` with `session_tripped: true` |
| The halt is durable | `aws dynamodb get-item` on the session returns `is_tripped: true`, the loop reason, and a `ttl` 30 days out |
| A halted session refuses unrelated work | A fourth call with a different tool returned `BLOCKED_CIRCUIT_BREAKER` |
| Every explanation names its source | Responses carry `explanation_source: "bedrock"` and `persistence: "dynamodb"`, and the UI prints the source as the heading rather than assuming Bedrock |
| Numeric arguments survive the round trip | Three identical calls with `{"retries": 3, "timeout": 1.5, "flag": true}` still halted on the third, so reloading history does not change a call's signature |
| Readiness can be alarmed on | `/readyz` answers 503 when a dependency is unreachable, 200 when both probes pass |
| Test suite | 59 passed in 0.57s, hermetic under `THREEFOLD_OFFLINE=1` |

## Known gaps, not yet fixed

These are recorded because they are still false or missing in the tree. None is
hidden in a document that a judge would read as finished work.

1. The source tree has changes that are committed but not yet deployed: the demo
   key moved to an environment variable and the gate was rewritten. The live
   stack still runs the previous commit. A deploy is needed, and once the role
   exists the pipeline does it on push.
2. Four of the five offline fallback panels still show canned prose. They now say
   "Simulated, offline demo, no model was reached" on their face, but the numbers
   inside them are invented and should be replaced with a real offline run.
3. The loop detector catches byte-identical repeats only. The README no longer
   claims entropy scanning, because there is no entropy code.
4. No headline number exists yet. This is the largest remaining gap for judging:
   the framing gate wants one comparative number against two named baselines.
5. No video and no Builder Center article. Neither is required by the rules, but
   the Builder Center project itself is, and it is owner-gated.

## Audit and what was done about it, 2026-09-20

An independent scoring pass and a competitive analysis both probed the live API.
Between them they found eight inputs that walked through the gate. All eight are
now refused, verified against the deployed stack rather than in tests alone.

| Input that used to be approved | Now | Cause that was fixed |
|---|---|---|
| Domain write with `action_type` omitted | refused | the content check no longer requires the caller to admit it is a write |
| `from boto3 import client` | refused | the rule parses imports with `ast` instead of matching substrings |
| `ASIA` and `sk-proj-` keys in a command | refused | ten credential shapes, up from five |
| `.env` under an argument named `filename` | refused | paths are found by shape, not by a four-name allowlist |
| `notebook_path` plus `new_source` | refused | same |
| A secret on its own line | refused | each string is scanned on its own, not `str(arguments)` |
| `curl -d @.env` | refused | protected-path patterns use lookaheads |
| An A, A, B cycle, fifteen calls deep | refused | any repeating period up to six, not three hardcoded shapes |

Ordinary work still passes: an edit outside the domain, and seven varied calls
in a row, are approved on the live API.

**The product now intercepts rather than rehearses.**
[`hooks/claude_code_hook.py`](hooks/claude_code_hook.py) puts Threefold in front
of a real Claude Code session. Confirmed end to end against the live stack: a
write of `from boto3 import client` into a domain file comes back denied with a
Bedrock sentence attached, an ordinary edit is allowed, and `cat ~/.aws/credentials`
is refused. It fails open by default and says so in the reason it returns.

**Positioning changed.** The README leads with the one rule that has no
incumbent, enforced at the moment of the edit rather than in CI. The other three
gates are described honestly as less novel than the tools that specialise in
them. This matters because Policy in Amazon Bedrock AgentCore has intercepted
tool calls before execution since March, and the judging panel is five AWS
employees. The distinction that holds is local tool calls against gateway
traffic.

### Still open

1. There is no Builder Center project and no public repository. Until both
   exist there is no submission, whatever the code does. One command each, in
   `docs/RUNBOOK.md`.
2. No measured number yet. The hook is installed-ready but has not been run
   across a working week, which is where the number comes from.
3. The certificate is a fingerprint, not a signature. There is no KMS call and
   no key, so it detects corruption rather than an adversary, and nothing in CI
   verifies one before a merge. Both limitations are now stated wherever the
   certificate appears.
4. `TokenCostCalculator` is never given a model id, so every session is priced
   at the default Sonnet-class rate rather than the model actually in use.
5. The cost gate trusts caller-declared token counts. A caller declaring zero is
   not stopped. A proxy that meters real usage is the stronger control and this
   is not one.

## Cost and teardown

PAY_PER_REQUEST DynamoDB, one 256 MB arm64 Lambda, an HTTP API and an empty S3
bucket. Bedrock calls are capped per container. Teardown is
`aws cloudformation delete-stack --stack-name threefold-prod --region eu-west-1`,
which must not run before judging completes.
