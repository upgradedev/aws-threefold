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
did, and that is the useful part of this record. Raw output:
[`evidence/DEPLOYMENT_2026-09-20.md`](evidence/DEPLOYMENT_2026-09-20.md).

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

## 2026-09-22: the stacks as they stand, read back by the agent

Every command below is read-only, and each answer is what AWS returned on
2026-09-22 [PRIMARY, 2026-09-22].

| The agent ran | AWS answered |
|---|---|
| `aws cloudformation describe-stacks --stack-name threefold-prod --region eu-west-1` | `UPDATE_COMPLETE`, last updated 2026-09-22T13:53:15Z; `PublicReads=true`, `DefaultHookStage=observe`, `BedrockModelId=eu.anthropic.claude-haiku-4-5-20251001-v1:0`, `ReservedConcurrency=25`; secrets shown as `****` |
| `aws cloudformation describe-stacks --stack-name threefold-prod-edge --region us-east-1` | `CREATE_COMPLETE`; `ApiDomainName=raa131f9dj.execute-api.eu-west-1.amazonaws.com`, `RateLimitPerFiveMinutes=1000`, `PriceClass_100`; `SiteUrl` `https://d1og72wpk4aqig.cloudfront.net/` |
| `aws lambda get-function-configuration` and `get-function-concurrency` on the function | `python3.11`, `arm64`, 256 MB, 15 s, tracing `Active`, reserved concurrency 25 |
| `aws apigatewayv2 get-stage --api-id raa131f9dj --stage-name prod` | throttling 100 requests a second, burst 200; access logs to `/aws/vendedlogs/apigateway/threefold-prod/access` |
| `aws dynamodb describe-continuous-backups` and `describe-time-to-live` on the table | point-in-time recovery `ENABLED`; TTL on `ttl` `ENABLED` |
| `aws cloudwatch describe-alarms --alarm-name-prefix threefold-prod-` | 10 alarms, all `OK` |
| `aws wafv2 get-web-acl` on the edge's web ACL | `AmazonIpReputationList`, `RateLimitPerIp`, `CommonRuleSet`, `KnownBadInputsRuleSet` |
| `aws cloudfront get-distribution` | `Deployed`, 3 origins, 21 cache behaviors, the web ACL attached |

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

The whole public stack, checked claim by claim by `scripts/probe_live.py`:
[`evidence/PROBES_2026-09-22.md`](evidence/PROBES_2026-09-22.md), 113 PASS,
0 FAIL, 3 SKIP against the origin URL.

## The connection is also the product

Threefold governs coding agents, and it was built by one. Every response from
`/evaluate-tool-call` carries `explanation_source`: `bedrock` when the model
phrased the refusal, `deterministic` when it was not asked (an approval, or a
hook, which sends `explain: false`), and `deterministic_fallback` when it was
asked and did not answer. A reader can always tell which one they are looking
at. The same discipline produced this page: the first table lists two defects
the agent shipped and then caught, because a proof that records only
successes is not evidence of a working connection.

## What this does not claim

The agent ran under a human operator's credentials, and every deploy was
reviewed before it was run. No autonomous production access was granted, and
none is claimed. The GitHub Actions workflows in `.github/workflows/` are
written to deploy with a short-lived OIDC role instead of stored keys, and that
role has not been created yet, so no deployment has run from them
[STATE-FILE]; every deploy so far was run by hand, with the commands in
[`RUNBOOK.md`](RUNBOOK.md).
