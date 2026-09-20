# Runbook

Two steps in this project cannot be automated from an agent session, because
they publish under the owner's identity or grant standing access to AWS. Both
are one command. Everything else in the pipeline runs itself once they are done.

## 1. Create the deploy role, once

The deploy workflow assumes this role through GitHub's OIDC provider, so no AWS
key is ever stored in the repository. The trust policy pins the subject to this
repository's `main` branch, which means a fork, a pull request from a fork, or
any other repository cannot assume it.

The OIDC provider already exists on the account. Do not recreate or modify it:
`arn:aws:iam::308857099262:oidc-provider/token.actions.githubusercontent.com`
is shared with another project.

```bash
cd repos/threefold

aws iam create-role \
  --role-name threefold-github-deploy \
  --assume-role-policy-document file://deploy/iam/github-trust.json \
  --description "GitHub Actions OIDC deploy role, upgradedev/threefold-aws main only" \
  --max-session-duration 3600

aws iam put-role-policy \
  --role-name threefold-github-deploy \
  --policy-name threefold-deploy \
  --policy-document file://deploy/iam/github-deploy-policy.json
```

Read the trust condition back before relying on it:

```bash
aws iam get-role --role-name threefold-github-deploy \
  --query 'Role.AssumeRolePolicyDocument.Statement[0].Condition'
```

It must show `repo:upgradedev/threefold-aws:ref:refs/heads/main` and not a
wildcard.

### What the role can and cannot do

Scoped to this stack: CloudFormation on `threefold-prod` only, the packaging
bucket prefix only, Lambda functions, DynamoDB tables, S3 buckets, log groups
and IAM roles whose names begin `threefold-prod-`.

Not scoped, and honestly so: `apigateway:*` and two Lambda listing actions take
no useful resource constraint, so they are granted account-wide within
eu-west-1. Narrowing them further would mean managing the API outside
CloudFormation, which costs more than it protects here. The role cannot read
Secrets Manager, cannot touch any other stack, and cannot create users or
policies.

## 2. Publish the repository, once

Do this only after `git log` shows the truth pass commit, because publishing is
the step that puts the text in front of other people.

```bash
cd repos/threefold

gh repo create upgradedev/threefold-aws \
  --public --source=. --remote=origin \
  --description "Threefold stops a coding agent at its third identical tool call. Deterministic gates on AWS Lambda, explained by Amazon Bedrock." \
  --push
```

Then read back what a stranger sees, rather than what you expect:

```bash
gh repo view upgradedev/threefold-aws --json visibility,url
curl -s -o /dev/null -w '%{http_code}\n' https://github.com/upgradedev/threefold-aws
gh run list --repo upgradedev/threefold-aws --limit 5
```

The push triggers both CI and Deploy. CI should pass. Deploy will fail until
step 1 is done, which is the correct order to see it in.

## Afterwards

The pipeline is then self-running: every push to `main` deploys and verifies,
and a scheduled probe checks the live URL every six hours.

Two things stay manual by choice. The Builder Center project and its two tags
are owner-gated. And the stack must not be torn down before judging finishes in
the week of 19 October, so the teardown command is deliberately not in any
workflow:

```bash
# Not before judging ends.
aws cloudformation delete-stack --stack-name threefold-prod --region eu-west-1
```
