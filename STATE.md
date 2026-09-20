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

## Audit, 2026-09-20

What a judge would find, written down before they find it.

**The two gaps that are not scores.** There is no Builder Center project and no
public repository. Until both exist there is no submission, however good the
code is, and neither is a thing an agent may do unsupervised. Both are one
command each in `docs/RUNBOOK.md`.

**The differentiator is prose, not code.** Every reviewer who has looked at this
named the same thing as the novel idea: a governance certificate that a CI check
refuses to merge without. Nothing in this repository verifies a certificate.
`ARCHITECTURE.md` used to state the requirement as fact and now states it as the
next piece of work. The same is true of the dogfooding story: a Claude Code hook
pointing at the deployed API would make the interception real rather than
simulated, and it does not exist.

**The demo intercepts nothing.** `/simulate-loop` and `/simulate-secret` build
their own payloads server side. The gates are real and the halt is durable, but
no agent is being stopped, so the panels show a rehearsal rather than a capture.

**What the loop detector does and does not catch.** Three checks: identical
signature repeated, ping-pong between two calls, and a repeating three-step
cycle. All three compare a SHA-256 of the tool name and sorted arguments, so ten
different edits to the same file are not a loop by this definition, although the
README's "edit-test-fail cycles" invites the reader to expect otherwise.

**Numbers that were never measured have been removed** rather than defended. The
sub-millisecond gate, the 0.5ms evaluation and the 180ms cold start are gone,
and the Well-Architected page no longer contradicts itself about whether any
latency was measured. Nothing replaced them, because there is still no
benchmark. There is also still no headline comparative number against a named
baseline, which is the largest remaining gap for scoring.

**The perimeter is demo grade. Six of seven adversarial inputs passed the live
gate** in an independent probe on 2026-09-20. This is the most serious finding
and none of it is fixed.

| Input sent to the live API | Verdict | Why it passed |
|---|---|---|
| Domain write of `import boto3` with `action_type` omitted | APPROVED | the handler defaults to `FILE_READ` and the guard only inspects `FILE_WRITE` |
| Domain write of `from boto3 import client` | APPROVED | the guard matches substrings, so the other import form is unseen |
| A temporary `ASIA` key and an `sk-proj-` key in a shell command | APPROVED | five regular expressions, neither pattern among them |
| Reading `.env` passed under an argument named `filename` | APPROVED | only four argument names are inspected |
| An A,A,B cycle repeated five times, one session | APPROVED | none of the three loop shapes describes it |
| Twelve calls declaring zero projected tokens | APPROVED, session cost stayed at zero | the cost is whatever the caller declares |
| One call declaring 200,000 output tokens | BLOCKED | the per-call cap does work |

Two of these defeat the exact scenarios the README advertises. The repository
also contains a stricter checker than the product ships: the pre-commit script
parses imports with `ast`, while the live gate matches substrings, so the weaker
engine is the one on the perimeter.

The tests pass alongside these holes because each rule is asserted against the
one literal the demo uses. There is no negative-variant coverage of the API gate
at all, which is the gap to close first: a variant test per rule would have
caught every row above.

**The cost engine prices every session wrongly.** `TokenCostCalculator` carries
per-model rates but is never given a model id, so the table is unreachable and
all sessions are billed at the default Sonnet-class rate.

## Cost and teardown

PAY_PER_REQUEST DynamoDB, one 256 MB arm64 Lambda, an HTTP API and an empty S3
bucket. Bedrock calls are capped per container. Teardown is
`aws cloudformation delete-stack --stack-name threefold-prod --region eu-west-1`,
which must not run before judging completes.
