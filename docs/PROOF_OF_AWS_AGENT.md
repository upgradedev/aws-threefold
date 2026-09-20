# Proof of a coding agent connected to AWS

The hackathon asks for a coding agent connected to the AWS console, with
documented proof of the connection. This page is that proof. It records what the
agent did against account `308857099262`, and how a reader can check each claim
without taking our word for it.

An earlier version of this file showed a pytest transcript. That proved the agent
could run tests on a laptop, which is not what was asked, so it was replaced.

## The agent

Claude Code, driving the AWS CLI v2 and boto3 under the operator's credentials,
in a Windows terminal. The same session wrote the code, created the
stack, read the failures back out of AWS, and fixed them.

## What the connection was used for, in order

The first deploy went out with the code exactly as it stood, before any polish,
specifically so that packaging and IAM would fail early if they were going to.
They did, and that is the useful part of this record.

| # | The agent ran | AWS answered | What changed |
|---|---|---|---|
| 1 | `aws sts get-caller-identity` | `arn:aws:iam::308857099262:user/tf-surface-studio` | Connection confirmed before anything was created |
| 2 | `aws cloudformation package --template-file deploy/template.yml --s3-bucket cf-templates-qd4r568jc7n6-eu-west-1` | uploaded 116,087 bytes | The Lambda zip, built without SAM and without installing anything locally |
| 3 | `aws cloudformation deploy --stack-name threefold-prod --capabilities CAPABILITY_IAM` | `CREATE_COMPLETE` | Stack `threefold-prod` in eu-west-1 |
| 4 | `curl https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/status` | **HTTP 404** | The first real defect. API Gateway prefixes the path with the stage name, and the handler routed on `/prod/status`. Found in minutes because the deploy came first |
| 5 | Fixed the stage prefix, redeployed | HTTP 200 | The ship gate, passed |
| 6 | Three POSTs to `/evaluate-tool-call` with identical arguments | `APPROVED`, `APPROVED`, `BLOCKED_LOOP_DETECTED` | The product's central claim, working in the cloud |
| 7 | `aws dynamodb get-item` on that session | `is_tripped: false` | The second real defect. The API reported the session halted while the table said otherwise, so another container would have kept approving |
| 8 | Wrote the halt through, added the terminal-state guard, redeployed, repeated 6 and 7 | `is_tripped: true`, with a `ttl` | Fixed, and verified in the table rather than in the response |
| 9 | `aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventName,AttributeValue=Converse` | three events, principal `assumed-role/threefold-prod-ThreefoldFunctionRole-JzGN3b6RjC7w` | Bedrock is called by the function's own role, not by the operator's user |

Step 9 is the one worth checking closely. A Bedrock call made from a workstation
proves the account has model access. It does not prove the deployed application
can reach the model. Only the execution role appearing as the CloudTrail
principal shows that, and it is why that command is in the list.

## Checking it yourself

Everything below is readable by anyone with credentials on the account. The raw
output is committed at [`evidence/DEPLOYMENT_2026-09-20.md`](evidence/DEPLOYMENT_2026-09-20.md).

```bash
# The stack exists and is healthy
aws cloudformation describe-stacks --stack-name threefold-prod --region eu-west-1

# Bedrock was invoked by the function role, not by a person
aws cloudtrail lookup-events --region eu-west-1 \
  --lookup-attributes AttributeKey=EventName,AttributeValue=Converse
```

Without any AWS credentials at all, the deployed application answers:

```bash
curl -s https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/status
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' \
  https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/
```

## The connection is also the product

Threefold governs coding agents, and it was built by one. Every response from
`/evaluate-tool-call` carries `explanation_source`, which reads `bedrock` when
the model answered and `deterministic_fallback` when it did not, so a reader can
always tell which they are looking at. The same discipline produced this page:
the table above lists two defects the agent shipped and then caught, because a
proof that records only successes is not evidence of a working connection.

## What this does not claim

The agent ran under a human operator's credentials and every deploy was reviewed
before it was run. No autonomous production access was granted, and none is
claimed. The continuous deployment that now runs in GitHub Actions uses a
short-lived OIDC role rather than stored keys, described in
[`../.github/workflows/deploy.yml`](../.github/workflows/deploy.yml).
