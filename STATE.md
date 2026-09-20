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
| Live on AWS, public URL | **PASS** | `https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod` returns 200 to an anonymous `curl` with no API key. Stack `threefold-prod`, eu-west-1, `UPDATE_COMPLETE` |
| Reachable by the AI scorer | **PASS** | `STAGE` is unset so the middleware defaults to `dev` and enforces no key. Verified by unauthenticated request |
| Proof of coding agent connected to AWS | **PARTIAL** | Commands and CloudTrail captured in `docs/evidence/DEPLOYMENT_2026-09-20.md`. `docs/PROOF_OF_AWS_AGENT.md` still holds the old pytest transcript and must be rewritten |
| Public repository | **NOT DONE** | Local `main` only, no remote. `gh repo create upgradedev/threefold-aws --public` is owner-gated |
| Builder Center project, two tags | **NOT DONE** | Owner-gated. Requires Builder Center profile, Join, then the Create Project form |

## What is real, measured today

| Claim | Command |
|---|---|
| Bedrock answers from the function's own role | CloudTrail `Converse` events 12:00:35, 12:00:37 and 12:00:39 UTC, principal `assumed-role/threefold-prod-ThreefoldFunctionRole-JzGN3b6RjC7w`, model `eu.anthropic.claude-haiku-4-5-20251001-v1:0`, no error |
| The third identical call halts the session | Three POSTs to `/evaluate-tool-call`: APPROVED, APPROVED, `BLOCKED_LOOP_DETECTED` with `session_tripped: true` |
| The halt is durable | `aws dynamodb get-item` on the session returns `is_tripped: true`, the loop reason, and a `ttl` 30 days out |
| A halted session refuses unrelated work | A fourth call with a different tool returned `BLOCKED_CIRCUIT_BREAKER` |
| Every explanation names its source | Responses carry `explanation_source: "bedrock"` and `persistence: "dynamodb"` |
| Test suite | 53 passed in 0.64s, hermetic under `THREEFOLD_OFFLINE=1` |

## Known gaps, not yet fixed

These are recorded because they are still false or missing in the tree. None is
hidden in a document that a judge would read as finished work.

1. `README.md` says "Submitted to AWS Zero to Shipped Hackathon" and advertises a
   CloudFront deployment that does not exist. The CI badge is a static image.
2. `docs/SUBMISSION_DOSSIER.md` carries placeholder article and video URLs that
   resolve to nothing, and a `127.0.0.1` live URL.
3. `docs/PROOF_OF_AWS_AGENT.md` is a pytest transcript attributed to a coding
   agent that never touched AWS.
4. `scripts/pre-commit-gate.py --scan-dir src/` exits 1 against this repository,
   so the first CI run would be red.
5. The web UI defaults its API base to `localhost:8001` and falls back to canned
   output without labelling it as simulated. It is not served from the stack.
6. The loop detector catches byte-identical repeats only. The README's "entropy
   scanning" claim has no code behind it.
7. No headline number exists. The `$14.8k` figure in earlier drafts was never
   computed and has no place in anything public.

## Cost and teardown

PAY_PER_REQUEST DynamoDB, one 256 MB arm64 Lambda, an HTTP API and an empty S3
bucket. Bedrock calls are capped per container. Teardown is
`aws cloudformation delete-stack --stack-name threefold-prod --region eu-west-1`,
which must not run before judging completes.
