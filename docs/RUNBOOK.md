# Runbook

How to deploy, publish, check, roll back and tear down Threefold, and what it
costs. Commands are for bash; PowerShell equivalents are given where they
differ. Every AWS command runs under the operator's own credentials.

| Stack | Template | Region | What it is |
|---|---|---|---|
| `threefold-prod` | `deploy/template.yml` | eu-west-1 | the public demo: API, function, table, alarms |
| `threefold-prod-edge` | `deploy/edge.yml` | us-east-1 | CloudFront, the pages bucket, the web ACL, in front of `threefold-prod` |
| the private stack | `deploy/template.yml` | eu-west-1 | the owner's own use, `PublicReads=false`. Its address and key live on the owner's machine and are never written into this repository, its evidence files or its commit messages |

Two secrets are involved and neither is ever typed on a command line, echoed,
or kept inside the repository: the **operator key** (a private stack's
`PolicyWriteApiKeys`) and the **edge secret** (`EdgeOriginSecret`, the same
value in the regional stack and in its edge). Keep each in a file of its own
outside the repository.

---

## 1. Deploy a regional stack

`deploy/template.yml` has grown past the 51,200 bytes CloudFormation accepts
for an inline template, so both commands send it through the packaging bucket,
whose limit for a template is some twenty times higher. Its exact size is not written
down here, because it changes with every edit to the template and a number in a
document does not; `wc -c deploy/template.yml` gives it. (`deploy/edge.yml` is
still comfortably under that limit and is deployed inline in section 2.)

```bash
aws cloudformation package \
  --template-file deploy/template.yml \
  --s3-bucket <packaging-bucket> --s3-prefix threefold \
  --output-template-file packaged.yml --region eu-west-1

aws cloudformation deploy \
  --template-file packaged.yml \
  --s3-bucket <packaging-bucket> --s3-prefix threefold \
  --stack-name threefold-prod --region eu-west-1 \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --no-fail-on-empty-changeset
```

On an existing stack, every parameter the command does not name keeps its
current value, secrets included, so an ordinary code deploy names none.

**Parameters worth knowing** (all have defaults):

| Parameter | Default | Notes |
|---|---|---|
| `PublicReads` | `true` | `false` for a private stack: the ledger, sessions, rules and policy then need the operator |
| `PolicyWriteApiKeys` | empty, `NoEcho` | the operator key(s). Empty refuses every policy, rules and stage write and closes sign-in |
| `AlarmEmail` | empty, `NoEcho` | subscribes an address to the alarm topic; AWS sends a confirmation first |
| `EdgeOriginSecret` | empty, `NoEcho` | the edge secret; empty trusts no edge header |
| `DefaultHookStage` | `observe` | the stage for a project with none of its own |
| `DemoFleet` | `false` | `true` on the public stack only: a schedule runs the synthetic Acme fleet every fifteen minutes |
| `FunctionMemoryMb` | `1024` | the function's memory in megabytes, from 512 to 1769, one full core. Lambda gives CPU in proportion to it; the stacks first ran with 256, so the first deploy that does not name it raises them to the default |
| `ReservedConcurrency` | `25` | Lambda refuses a reservation that leaves fewer than 100 unreserved: check `aws lambda get-account-settings` shows at least 150 for two stacks at 25, or deploy with `0` |
| `ApiThrottleRateLimit` / `ApiThrottleBurstLimit` | `100` / `200` | per route, answered 429 by API Gateway |
| `SlowCallAlarmMs` | `5000` | threshold of the two latency alarms: the p95 of every HTTP request's latency at API Gateway, and the average evaluation latency. The fleet's scheduled tick is in neither; the near-timeout alarm (an invocation over 12 seconds) watches it |
| `MonthlyBudgetUsd` | `0` | an account-wide budget; set it on one stack only |
| `AllowedProjectPattern` | `^Acme-[A-Za-z0-9-]{1,40}$` | other names are stored and shown as `unlabelled` |

**Passing a secret from a file, without echoing it.**

```bash
# once: make an edge secret straight into a file (64 URL-safe characters)
python3 -c "import secrets; print(secrets.token_urlsafe(48))" > /path/outside/the/repo/edge-secret.txt

aws cloudformation deploy \
  --template-file packaged.yml \
  --s3-bucket <packaging-bucket> --s3-prefix threefold \
  --stack-name threefold-prod --region eu-west-1 \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "EdgeOriginSecret=$(cat /path/outside/the/repo/edge-secret.txt)" \
    "AlarmEmail=$(cat /path/outside/the/repo/alarm-email.txt)"
```

The value never reaches the terminal or the shell's history, which keeps the
`$(cat ...)` rather than what it expands to. It is still an argument of the
`aws` process while that runs, visible to anyone who can list this machine's
processes, so run it on a machine only you use.

PowerShell:

```powershell
py -c "import secrets; print(secrets.token_urlsafe(48))" | Set-Content -NoNewline C:\path\outside\the\repo\edge-secret.txt
$edge = (Get-Content -Raw C:\path\outside\the\repo\edge-secret.txt).Trim()
aws cloudformation deploy --template-file packaged.yml --s3-bucket <packaging-bucket> --s3-prefix threefold `
  --stack-name threefold-prod --region eu-west-1 --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND `
  --no-fail-on-empty-changeset --parameter-overrides "EdgeOriginSecret=$edge"
Remove-Variable edge
```

**A private stack** is the same template under another name:

```bash
aws cloudformation deploy \
  --template-file packaged.yml \
  --s3-bucket <packaging-bucket> --s3-prefix threefold \
  --stack-name <private-stack> --region eu-west-1 \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --no-fail-on-empty-changeset \
  --parameter-overrides PublicReads=false \
    "PolicyWriteApiKeys=$(cat /path/outside/the/repo/operator.key)" \
    "AlarmEmail=$(cat /path/outside/the/repo/alarm-email.txt)"
```

Then read the address from the stack's `ApiEndpoint` output and keep it with
the key, outside the repository.

**Check it answered.** `curl -s https://<api>/prod/status` returns
`"status": "HEALTHY"`; `curl -s -o /dev/null -w '%{http_code}\n' https://<api>/prod/`
returns 200. Keep the trailing slash: the bare `/prod` is API Gateway's own 404.

## 2. Deploy the edge, in us-east-1

A web ACL for CloudFront can only live in us-east-1, so the edge is its own
stack, pointed at the regional API by host name. Deploy the regional stack
with the edge secret first, then the edge with the same value. Until both
agree, the function treats requests from the edge as untrusted: its
per-address limit counts the edge server as the caller, so every viewer behind
one edge server shares one bucket, and `/install.py`, `/dist/manifest.json` and
sign-in links name the API's own address instead of the edge's. The API URL
itself works throughout.

```bash
aws cloudformation deploy \
  --template-file deploy/edge.yml \
  --stack-name threefold-prod-edge --region us-east-1 \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    ApiDomainName=raa131f9dj.execute-api.eu-west-1.amazonaws.com \
    "EdgeOriginSecret=$(cat /path/outside/the/repo/edge-secret.txt)"
```

Optional parameters: `ApiStagePath` (default `/prod`), `RateLimitPerFiveMinutes`
(default 1000 per address), `PriceClass` (default `PriceClass_100`),
`AccessLogs` (default `true`, a separate bucket kept 30 days). The site is the
`SiteUrl` output, `https://<distribution>.cloudfront.net/`.

**Check the secret took.** The installer the edge serves must name the edge:

```bash
curl -s https://<distribution>.cloudfront.net/install.py | grep '^BAKED_ENDPOINT'
# BAKED_ENDPOINT = "https://<distribution>.cloudfront.net/"
```

If it names the API URL instead, the two stacks hold different secrets.

## 3. Publish the pages to the edge

The edge serves pages from its bucket, not from the function, so run this
after every deploy that changes anything under `src/threefold/web/`:

```bash
python scripts/publish_web.py --stack-name threefold-prod-edge --dry-run   # every step, nothing run
python scripts/publish_web.py --stack-name threefold-prod-edge             # upload, then one /* invalidation
python scripts/publish_web.py --stack-name threefold-prod-edge --prune     # also delete pages removed from src/
```

It uploads every `*.html` and everything under `assets/` with the base path
emptied, uploads `dashboard.html` again as `app`, and invalidates `/*` once.
`openapi.json` and `proof.json` are not uploaded: the edge sends every `*.json`
path to the function, which serves the copies deployed with its code.

## 4. Backfill the daily rollups

Only for a stack that recorded decisions before the rollups existed. Each row
is claimed by a conditional update before it is counted, so a rerun adds
nothing twice. It prints counts only.

```bash
python scripts/backfill_rollups.py --stack <stack> --region eu-west-1 --days 30 --dry-run
python scripts/backfill_rollups.py --stack <stack> --region eu-west-1 --days 30
```

## 5. Probe a live stack

`scripts/probe_live.py` checks a deployed stack against the project's claims,
prints PASS, FAIL or SKIP per check, writes a dated evidence file and exits 1
when anything failed. It never prints the key, or any code or token it mints.

Public stack, through the edge and at the origin:

```bash
python scripts/probe_live.py --base https://d1og72wpk4aqig.cloudfront.net/ --expect public
python scripts/probe_live.py --base https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/ --expect public
```

These write: synthetic calls under project `Acme-Probe` and session ids
`probe-<run id>-*`, the two demo simulations, and one sandbox that expires in
24 hours; the evidence header lists every kind of write. `--read-only` skips
every check that records a decision or changes a project. The evidence goes to
`docs/evidence/PROBES_<date>.md`.

A private stack:

```bash
python scripts/probe_live.py --base https://<private-stack>/prod/ --expect private \
  --key-file /path/outside/the/repo/operator.key --read-only
```

For a private or keyed run the evidence file goes to the system's temporary
folder, not to `docs/evidence/`, so the private address never lands in the
repository. Leave it there.

## 6. The agent benchmark

The harness in `benchmark/` runs a coding agent headless on six synthetic Acme
tasks, and on three pressure variants whose prompt asks for the forbidden
shortcut, under three conditions, and grades what it left behind.

Measured on 2026-09-22, four matrices, 162 Claude Code runs. A governed
violation landed with no guidance / with the rules in `CLAUDE.md` / with
Threefold enforcing:

| Report | Rows | Violation landed | Tests passed, Threefold |
|---|---|---|---|
| `BENCHMARK_2026-09-22.md` (standard, `claude-sonnet-5`) | `20260922T143932Z.jsonl` | 17% / 0% / 0% | 18/18 |
| `BENCHMARK_2026-09-22-HAIKU.md` (standard, `claude-haiku-4-5`) | `20260922T145644Z.jsonl` | 39% / 17% / 0% | 18/18 |
| `BENCHMARK_2026-09-22-PRESSURE-SONNET.md` | `20260922T161455Z-pressure.jsonl` | 67% / 0% / 0% | 6/9 |
| `BENCHMARK_2026-09-22-PRESSURE-HAIKU.md` | `20260922T162306Z-pressure.jsonl` | 100% / 56% / 0% | 4/9 |

The two families are never pooled. Nothing under Threefold violated in any
series. The `CLAUDE.md` column follows the model rather than the family: with
`claude-sonnet-5` the rules held in both families with nothing enforcing them
(0/18 and 0/9), with `claude-haiku-4-5` in neither (17% and 56%). The cost of
enforcing shows in the pressure rows, where the governed agent finished 10 of
18 runs and otherwise stopped and reported the conflict.

The pilot before them (`BENCHMARK_2026-09-22-PILOT.md`) measured nothing: its
real-agent runs never reached the model on an expired login, which the token
file below fixed.

**The login.** Run `claude setup-token` once in a terminal and save the token
it prints, alone on one line, to `C:\threefold-bench\.claude-oauth-token`, or
anywhere and pass `--token-file PATH`. The runner hands it to the agent process
alone, with a configuration folder and a home folder made for each run. A
`CLAUDE_CODE_OAUTH_TOKEN` exported in the shell is not used.

```bash
python benchmark/run.py --check-auth                                   # ok, expired, missing, limited or error
python benchmark/run.py --agent scripted --reps 1 --parallel 3         # the harness alone, no model, free
python benchmark/run.py --tasks orders-s3-archive --reps 1 --pilot     # one task, three conditions, labelled pilot
python benchmark/run.py --reps 3 --parallel 3                          # the standard matrix: 54 runs
python benchmark/run.py --family pressure --reps 3 --parallel 3        # the pressure matrix: 27 runs
python benchmark/run.py --reps 3 --parallel 3 --resume <run-id>        # after a stop
python benchmark/report.py benchmark/results/<run-id>.jsonl            # docs/evidence/BENCHMARK_<date>.md and a summary
python scripts/build_proof.py \
  --series benchmark/results/20260922T143932Z.jsonl \
  --series benchmark/results/20260922T145644Z.jsonl \
  --series benchmark/results/20260922T161455Z-pressure.jsonl \
  --series benchmark/results/20260922T162306Z-pressure.jsonl
```

`report.py` computes the headline from the rows; nothing else states one. Each
run of the matrix is its own `--series`, so two models, or the standard and the
pressure tasks, are never pooled into one set of rates.
`build_proof.py` writes `src/threefold/web/proof.json` from the same rows (and,
with `--private-endpoint` and `--key-file`, anonymised totals from a private
stack, refusing to write if any string matches a project name, the key or an
absolute path). The dashboard's `#/proof` page reads `/proof.json`, which the
function serves, so a new snapshot reaches the public page with the next
regional deploy (section 1), not with `publish_web.py`.

Bounds, from the harness's own caps: each run stops at 20 minutes and, for
Claude Code, $5 at API list price, so the 54-run matrix cannot take more than
about 6 hours or cost more than $270 at list price (the 27-run pressure matrix,
3 hours and $135); under a subscription that is usage against its limits. What
the four matrices of 2026-09-22 actually took, from their own rows: the
standard matrix about 0.3 hours and $10 with `claude-sonnet-5` and about
0.3 hours and $4 with `claude-haiku-4-5`; the pressure matrix about 0.1 hours
and $6, then about 0.1 hours and $1. Codex runs use `--agent codex` and its own
login (`codex login`) and are not measured.

## 7. Connect, open, status, disconnect

In the repository to govern (Windows PowerShell, then macOS and Linux):

```powershell
irm https://d1og72wpk4aqig.cloudfront.net/install.py -OutFile threefold.py; py threefold.py connect --project Acme-Billing
```

```bash
curl -fsSL https://d1og72wpk4aqig.cloudfront.net/install.py -o threefold.py && python3 threefold.py connect --project Acme-Billing
```

For a private stack, fetch `install.py` from that stack instead and add
`--api-key-file /path/outside/the/repo/operator.key`.

| Command | Does |
|---|---|
| `python3 threefold.py connect [PATH] [--project NAME] [--mode managed\|observe\|enforce] [--agents auto\|LIST] [--include GLOB ...] [--dry-run] [--no-open]` | installs the hook for the agents found, the pre-commit check and `.threefold.json`, lists them in `.git/info/exclude`, sends one dry-run call and opens the project's page |
| `python3 threefold.py open [--next /projects/NAME]` | signs in to the dashboard through a single-use link, with the key file configured for that stack; without one, opens it unsigned |
| `python3 threefold.py status` | every connected folder on this machine and its stage on the stack |
| `python3 threefold.py disconnect [PATH]` | removes exactly what connect added; a file changed by hand since keeps the change |

The installer keeps a copy of itself at `~/.threefold/bin/threefold_install.py`,
so these work after `threefold.py` is deleted. `--dry-run` writes nothing,
downloads nothing and calls nothing.

## 8. Continuous deployment (optional)

`.github/workflows/deploy.yml` packages and deploys `threefold-prod` and then
checks the live URL; `ci.yml` runs the gate, the tests and template
validation; `keepalive.yml` checks the public URL every six hours. The deploy
workflow runs only when someone dispatches it on `main` (Actions, Deploy, Run
workflow), never on a push: a deploy stays a person's decision. It assumes the
role `threefold-github-deploy` through GitHub's OIDC provider, and that role
does not exist yet [STATE-FILE], so every deploy so far was run by hand with
section 1. Neither workflow deploys the edge or publishes the pages.

The workflow names no parameters, so `threefold-prod` keeps the `DemoFleet`,
`EnforceProjectPattern` and `EdgeOriginSecret` it has, as section 1 describes.
A parameter the stack does not have yet takes the template's default on the
first deploy that carries it; the comment above the deploy step in
`deploy.yml` gives the AWS CLI's wording and the exceptions.

The trust policy, `deploy/iam/github-trust.json`, accepts one token subject:
`repo:upgradedev/aws-threefold:ref:refs/heads/main`, this repository's `main`
branch, matched with `StringEquals` and no wildcard, for the audience
`sts.amazonaws.com`. A run on another branch, from a fork or for a pull
request is refused, and so is a job given a GitHub environment, whose token
names the environment instead of the branch.
`tests/security/test_the_deploy_role_trusts_one_branch_of_one_repository.py`
reads the repository from the GitHub URL in `README.md` and fails if the
policy names another. Create the role from the repository root:

```bash
aws iam create-role \
  --role-name threefold-github-deploy \
  --assume-role-policy-document file://deploy/iam/github-trust.json \
  --description "GitHub Actions OIDC deploy role, main branch of one repository only" \
  --max-session-duration 3600

aws iam put-role-policy \
  --role-name threefold-github-deploy \
  --policy-name threefold-deploy \
  --policy-document file://deploy/iam/github-deploy-policy.json

aws iam get-role --role-name threefold-github-deploy \
  --query 'Role.AssumeRolePolicyDocument.Statement[0].Condition'
```

The last command must show exactly the `sub` above and no wildcard. If the role
was created earlier from a trust policy with another `sub`, it has never been
assumable; replace its trust policy rather than recreating the role:

```bash
aws iam update-assume-role-policy \
  --role-name threefold-github-deploy \
  --policy-document file://deploy/iam/github-trust.json
```

Run the `put-role-policy` command again whenever
`deploy/iam/github-deploy-policy.json` changes: it replaces the inline policy
of that name.

The account's GitHub OIDC provider already exists and is shared with another
project: do not recreate or modify it.

What the role may do, as `tests/integration/test_the_deploy_role_covers_the_template.py`
checks it action by action against every resource the template declares:
CloudFormation change sets on `threefold-prod` only, through the Serverless
transform; objects under `threefold/` in the packaging bucket; the Lambda
functions, DynamoDB tables, log groups, alarms, alarm topic and fleet schedule
in eu-west-1 whose names begin `threefold-prod-`; and, because a bucket's,
a role's, a dashboard's and a budget's ARN carry no Region, only the evidence
bucket, the dashboard and the budget under the names this stack gives them,
and roles whose names begin `threefold-prod-ThreefoldFunctionRole-` or
`threefold-prod-DemoFleetScheduleRole-`, the prefixes of the two roles this
stack generates. That keeps the edge stack out of reach, although its generated
bucket names also begin `threefold-prod-`. It may pass the function's role
only to Lambda and the schedule's role only to EventBridge Scheduler, and
attach to them only the two AWS managed policies the Serverless transform
attaches.

Where a resource's name cannot be known before it exists, the grant is wider.
API ids and KMS key ids are generated, so it may call every API Gateway action
on every API Gateway resource in the account, in any Region: REST, HTTP and
WebSocket APIs, API keys and their values, custom domain names and their
mappings, usage plans, VPC links and the account's API Gateway settings. It
may also call every key action the template needs on every key in the account
and Region; `kms:CreateKey` itself names no key and is limited to an RSA_2048
signing key, the kind the template declares. Those two grants reach the
private stack's API and certificate key. Limiting the API Gateway grant to
this Region's HTTP APIs and their tags (`arn:aws:apigateway:eu-west-1::/apis`,
`/apis/*` and `/tags/*`) is the likely next narrowing, but whether that covers
every call CloudFormation makes for an HTTP API has not been checked, and a
grant that falls short is found only by a live deploy that rolls back. It may
also make the log deliveries an HTTP API's access log needs, list functions,
log groups and dashboards, and read the account's Lambda settings. Its own
grants do not let it read Secrets Manager, create users, groups or managed
policies, or change another stack's CloudFormation, functions, tables,
buckets, log groups, alarms, topics, schedules or roles, provided that
stack's own name does not begin `threefold-prod-`: the names in eu-west-1
are matched as a prefix. Through the roles it may create, though, it can do
all of those things, as the next paragraph explains.

Its IAM grants name the roles by prefix, because CloudFormation appends a
random suffix to each generated role name. So the deploy role can create any
number of roles whose names begin `threefold-prod-ThreefoldFunctionRole-` or
`threefold-prod-DemoFleetScheduleRole-`. It can give each any trust policy,
rewrite the trust policy of an existing one, the live function's role
included, with `iam:UpdateAssumeRolePolicy`, and put any inline policy on it.
A role that trusts a principal outside the account and carries a policy
allowing every action is access that outlives the one-hour session and
survives the deletion of `threefold-github-deploy`. Assuming a role needs no
`iam:PassRole`, so the PassRole limits above do not stop this. Its reach is
therefore that of an account administrator, and it can make that reach
last. Anyone who can push to `main` can change what the workflow runs with
it: treat write access to the repository as administrative access to the
account.

Nothing caps this today. The remedy is a permissions boundary, in three
parts: a managed policy the owner creates, allowing at most what the function
and the schedule need; `PermissionsBoundary` set to that policy on the
function and on `DemoFleetScheduleRole` in `deploy/template.yml`; and an
`iam:PermissionsBoundary` condition requiring it on the deploy role's
`iam:CreateRole`, `iam:PutRolePolicy` and `iam:AttachRolePolicy`. The deploy
role holds no grant to put or delete a role's boundary and must not be given
one. The condition key does not apply to `iam:UpdateAssumeRolePolicy`, so the
role could still make a bounded role assumable from outside the account: the
boundary caps what that hands out at what the boundary allows, rather than
closing the path. The boundary changes the template and the live roles, so it
is a change of its own and not part of this policy.

## 9. Roll back

| What | How |
|---|---|
| A failed stack update | CloudFormation rolls it back by itself. |
| A bad code deploy | Check out the previous commit (in a separate worktree) and run section 1 again; parameters keep their values. |
| A bad page publish | Run `publish_web.py` from the previous commit, or restore the previous object version in the pages bucket (versioned; replaced versions kept 30 days) and invalidate `/*`. |
| A project promoted too early | **Demote** on its dashboard page, or `POST /api/projects/<name>/demote` with the operator. It applies at once on the container that handled it and within 30 seconds on every other warm container (`RULES_REFRESH_SECONDS`), so a call in that half minute can still be refused under Enforce. |
| A repository | `threefold.py disconnect`. |
| The edge | Delete `threefold-prod-edge` (section 10). The origin URL keeps working exactly as before, but hooks installed from the edge name the edge as their endpoint: without it they cannot reach the service and fail open until reconnected against the origin. |
| Table data | Point-in-time recovery restores to a **new** table (`aws dynamodb restore-table-to-point-in-time`). The function keeps using the old one; nothing in the template or the scripts switches it, so copying items back is manual. |

## 10. Tear down

Not before the winners are announced. The hackathon's milestone table
(`builder.aws.com`, Zero to Shipped) puts Gate 1 (AI and human scoring) in the
week of 5 October, Gate 2 (human judging) in the week of 12 October, and the
announcement in the week of 19 October, so nothing below runs before that week
[STATE-FILE].

1. Disconnect every governed repository, or its hooks will fail open against a
   missing endpoint.
2. Read the edge's bucket names before deleting it:

   ```bash
   aws cloudformation describe-stacks --stack-name threefold-prod-edge --region us-east-1 \
     --query "Stacks[0].Outputs[?OutputKey=='WebBucketName' || OutputKey=='LogBucketName'].OutputValue" --output text
   aws cloudformation delete-stack --stack-name threefold-prod-edge --region us-east-1
   ```

   Both buckets are **retained** by design (a versioned bucket with objects
   cannot be deleted by CloudFormation). Empty each one, every version and
   delete marker included, and delete it, in the console or with `aws s3api`.
3. If the record should outlive the stack, back the table up first
   (`aws dynamodb create-backup --table-name <table> --backup-name <name>`), then:

   ```bash
   aws cloudformation delete-stack --stack-name threefold-prod --region eu-west-1
   ```

   The evidence bucket is not retained and is empty, so it goes with the stack;
   if anything was ever written to it, the delete fails until it is emptied.

## 11. What it costs

ESTIMATE throughout: no bill was read for this page, and the figures come from
the tracks' own notes and AWS list prices, not from measurement.

| Item | ESTIMATE |
|---|---|
| AWS WAF web ACL with four rules | about 9 USD a month, plus a per-request charge |
| CloudWatch alarms (11 per stack) and the dashboard | a few USD a month |
| Lambda, API Gateway, DynamoDB on demand, CloudFront, S3 | per request and per GB; not estimated |
| Bedrock | per token, only for page explanations and rule drafts, capped per container at 200 successful explanation calls (failed calls are not counted) and 60 drafting calls (every call counted) |
| The benchmark | see section 6 |

`MonthlyBudgetUsd` puts an account-wide budget on the alarm topic when a
number is wanted rather than an estimate.
