# Proof of a coding agent connected to AWS

The hackathon asks for a coding agent connected to the AWS console, with
documented proof of the connection. This page is that proof. It records what
the agent did against account `308857099262`, and how a reader can check each
claim without taking our word for it.

## The agent

Claude Code, driving the AWS CLI v2 and boto3 under the operator's
credentials, in a Windows terminal. A session of it wrote the code, created
the first stack, read the failures back out of AWS and fixed them; another, on
2026-09-22, read the live stacks back, read-only, for the documentation you are
reading.

## 2026-09-20: the first deployment, defects included

The first deploy went out with the code exactly as it stood, before any
polish, so that packaging and IAM would fail early if they were going to. They
did, and that is the useful part of this record. The raw output kept from that
day, the stack's description, its `/status` answering and the three
CloudTrail events of step 9, is in
[`evidence/DEPLOYMENT_2026-09-20.md`](evidence/DEPLOYMENT_2026-09-20.md). The
other steps are recorded only in this table, written by the session that ran
them; their transcripts were not kept.

| # | The agent ran | AWS answered | What changed |
|---|---|---|---|
| 1 | `aws sts get-caller-identity` | `arn:aws:iam::308857099262:user/<the operator's IAM user>` | Connection confirmed before anything was created |
| 2 | `aws cloudformation package --template-file deploy/template.yml --s3-bucket cf-templates-qd4r568jc7n6-eu-west-1` | uploaded 116,087 bytes | The Lambda zip, built without SAM and without installing anything locally |
| 3 | `aws cloudformation deploy --stack-name threefold-prod --capabilities CAPABILITY_IAM` | `CREATE_COMPLETE` | Stack `threefold-prod` in eu-west-1 |
| 4 | `curl https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/status` | **HTTP 404** | The first real defect. API Gateway prefixes the path with the stage name, and the handler routed on `/prod/status`. Found in minutes because the deploy came first |
| 5 | Fixed the stage prefix, redeployed | HTTP 200 | The service answered |
| 6 | Three POSTs to `/evaluate-tool-call` with identical arguments | `APPROVED`, `APPROVED`, `BLOCKED_LOOP_DETECTED` | The product's central claim, working in the cloud |
| 7 | `aws dynamodb get-item` on that session | `is_tripped: false` | The second real defect. The API reported the session halted while the table said otherwise, so another container would have kept approving |
| 8 | Wrote the halt through, added the terminal-state guard, redeployed, repeated 6 and 7 | `is_tripped: true`, with a `ttl` | Fixed, and verified in the table rather than in the response |
| 9 | `aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventName,AttributeValue=Converse` | three events, principal `assumed-role/threefold-prod-ThreefoldFunctionRole-JzGN3b6RjC7w` | Bedrock is called by the function's own role, not by the operator's user |

Step 9 is the one worth checking closely. A Bedrock call made from a
workstation proves the account has model access. It does not prove the
deployed application can reach the model. Only the execution role appearing
as the CloudTrail principal shows that, and it is why that command is in the
list.

## 2026-09-22: the stacks as they stood, read back by the agent

Every command below is read-only, and each answer is what AWS returned on
2026-09-22 [PRIMARY, 2026-09-22].

| The agent ran | AWS answered |
|---|---|
| `aws cloudformation describe-stacks --stack-name threefold-prod --region eu-west-1` | `UPDATE_COMPLETE`, last updated 2026-09-22T13:53:15Z; `PublicReads=true`, `DefaultHookStage=observe`, `BedrockModelId=eu.anthropic.claude-haiku-4-5-20251001-v1:0`, `ReservedConcurrency=25`; secrets shown as `****` |
| `aws cloudformation describe-stacks --stack-name threefold-prod-edge --region us-east-1` | `CREATE_COMPLETE`; `ApiDomainName=raa131f9dj.execute-api.eu-west-1.amazonaws.com`, `RateLimitPerFiveMinutes=1000`, `PriceClass_100`; `SiteUrl` `https://d1og72wpk4aqig.cloudfront.net/` |
| `aws lambda get-function-configuration` and `get-function-concurrency` on the function | `python3.11`, `arm64`, 256 MB, 15 s, tracing `Active`, reserved concurrency 25 |
| `aws apigatewayv2 get-stage --api-id raa131f9dj --stage-name prod` | throttling 100 requests a second, burst 200, applied to each route separately; access logs to `/aws/vendedlogs/apigateway/threefold-prod/access` |
| `aws dynamodb describe-continuous-backups` and `describe-time-to-live` on the table | point-in-time recovery `ENABLED`; TTL on `ttl` `ENABLED` |
| `aws cloudwatch describe-alarms --alarm-name-prefix threefold-prod-` | 10 alarms, all `OK` |
| `aws wafv2 get-web-acl` on the edge's web ACL | `AmazonIpReputationList`, `RateLimitPerIp`, `CommonRuleSet`, `KnownBadInputsRuleSet` |
| `aws cloudfront get-distribution` | `Deployed`, 3 origins, 21 cache behaviors, the web ACL attached |

What has changed since. Rechecked with the same commands on 2026-09-27, the
function runs at 1024 MB, set by the `FunctionMemoryMb` parameter, and there
are 11 alarms, all `OK`, after the slow-call alarm moved to the HTTP API's p95
latency and a near-timeout alarm was added
([`ARCHITECTURE.md`](ARCHITECTURE.md)) [PRIMARY, 2026-09-27]. The public stack
also carries two parameters added on 2026-09-26: `DemoFleet=true`, which runs
a synthetic Acme fleet through the real gates every 15 minutes, labelled
synthetic wherever it appears, and `EnforceProjectPattern=^Acme-Live-.+$`, so
the daily live agent's projects start in Enforce [STATE-FILE].

## Checking it yourself

With credentials on the account:

```bash
aws cloudformation describe-stacks --stack-name threefold-prod --region eu-west-1
aws cloudformation describe-stacks --stack-name threefold-prod-edge --region us-east-1

# Bedrock was invoked by the function role, not by a person
aws cloudtrail lookup-events --region eu-west-1 \
  --lookup-attributes AttributeKey=EventName,AttributeValue=Converse
```

Without any AWS credentials at all, the deployed application answers, at the
edge and at its origin:

```bash
curl -s https://d1og72wpk4aqig.cloudfront.net/status
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' https://d1og72wpk4aqig.cloudfront.net/
curl -s https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/status
```

And the check that the edge and the function trust each other: the installer
fetched from the edge names the edge, and fetched from the origin names the
origin.

```bash
curl -s https://d1og72wpk4aqig.cloudfront.net/install.py | grep '^BAKED_ENDPOINT'
curl -s https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/install.py | grep '^BAKED_ENDPOINT'
```

The whole public stack, checked claim by claim by `scripts/probe_live.py`: first
on 2026-09-22, 113 PASS, 0 FAIL, 3 SKIP against the origin URL
([`evidence/PROBES_2026-09-22.md`](evidence/PROBES_2026-09-22.md)); most
recently on 2026-09-27, after that day's second deploy, 117 PASS, 0 FAIL,
3 SKIP both through the edge and at the origin
([`evidence/PROBES_2026-09-27-2-edge.md`](evidence/PROBES_2026-09-27-2-edge.md),
[`evidence/PROBES_2026-09-27-2.md`](evidence/PROBES_2026-09-27-2.md)). Among
those checks, the installer served through the edge names the edge.

## The connection is also the product

Threefold governs coding agents, and it was built by one. Every response from
`/evaluate-tool-call` carries `explanation_source`: `bedrock` when the model
phrased the refusal, `deterministic` when it was not asked (an approval, or a
hook, which sends `explain: false`), and `deterministic_fallback` when it was
asked and did not answer. A reader can always tell which one they are looking
at. The same discipline produced this page: the first table lists two defects
the agent shipped and then caught, because a proof that records only
successes is not evidence of a working connection.

Since 2026-09-26 a coding agent also works against the deployed service
through the hook: `scripts/daily_live_agent.py` gives Codex or Claude Code,
alternating by day, one of the benchmark's Acme tasks in a project that
starts in Enforce on the public stack. Its first run, Codex CLI 0.155.0 on
2026-09-26, was refused once, falsely: a PowerShell read ending in `2>$null`
was taken for a write. That was fixed and deployed on 2026-09-27, and looking
for a way around the fix closed an older hole, where
`bash -c "... > src/domain/\$f"` had been approved [STATE-FILE]. The second
run, Claude Code 2.1.220 on 2026-09-27, made 4 calls, all approved, left no
violation and passed its acceptance tests
([`benchmark/results/live/`](../benchmark/results/live/)) [PRIMARY,
2026-09-27]. Until the owner schedules the script, a day runs only when it
is started by hand [STATE-FILE].

## What this does not claim

The agent ran under a human operator's credentials, and every deploy was
reviewed before it was run. No autonomous production access was granted, and
none is claimed. The deploy workflow, `.github/workflows/deploy.yml`, is
written to deploy with a short-lived OIDC role instead of stored keys. That
role has deliberately not been created: on 2026-09-27 the owner decided
against it, because a role that may create roles and write their policies is
in practice an account administrator until a permissions boundary caps it
([`RUNBOOK.md`](RUNBOOK.md), section 8). No deployment has run from the
workflow [STATE-FILE]; every deploy so far was run by hand, with the commands
in [`RUNBOOK.md`](RUNBOOK.md).
