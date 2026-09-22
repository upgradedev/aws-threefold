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

`deploy/template.yml` is 49,398 bytes, within two kilobytes of the 51,200
bytes CloudFormation accepts inline, so both commands send it through the
packaging bucket, which has no such limit. (`deploy/edge.yml`, at 44,377
bytes, is still deployed inline in section 2.)

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
| `ReservedConcurrency` | `25` | Lambda refuses a reservation that leaves fewer than 100 unreserved: check `aws lambda get-account-settings` shows at least 150 for two stacks at 25, or deploy with `0` |
| `ApiThrottleRateLimit` / `ApiThrottleBurstLimit` | `100` / `200` | per route, answered 429 by API Gateway |
| `SlowCallAlarmMs` | `5000` | threshold of the two latency alarms |
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
series; the rules in `CLAUDE.md` held only with the strong model on the plain
tasks, and the cost of enforcing shows in the pressure rows, where the governed
agent finished 10 of 18 runs and otherwise stopped and reported the conflict.
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
workflow assumes the role `threefold-github-deploy` through GitHub's OIDC
provider, and that role does not exist yet [STATE-FILE], so every deploy so far
was run by hand with section 1. Neither workflow deploys the edge or publishes
the pages.

First check the trust policy's `sub` condition in
`deploy/iam/github-trust.json`: it must read
`repo:<owner>/<repository>:ref:refs/heads/main` for the one repository whose
`main` branch is allowed to deploy, with no wildcard. Then create the role,
from the repository root:

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

The last command must show exactly the `sub` written in
`deploy/iam/github-trust.json`, one repository's `refs/heads/main`, and no
wildcard. The account's GitHub OIDC provider already exists and is shared with
another project: do not recreate or modify it.

What the role may do: CloudFormation on `threefold-prod` only, the packaging
bucket, and Lambda functions, DynamoDB tables, S3 buckets, log groups, alarms,
the dashboard, the alarm topic, a budget and IAM roles whose names begin
`threefold-prod-`. What it may do more widely, because those actions take no
useful resource constraint: `apigateway:*` and a few CloudWatch and Lambda
listing actions within the account. It cannot read Secrets Manager, touch any
other stack, or create users or policies.

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

Not before judging ends. `STATE.md` records two dates for the end of judging,
and the later one, the week of 19 October, governs teardown [STATE-FILE].

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
| CloudWatch alarms (10 per stack) and the dashboard | a few USD a month |
| Lambda, API Gateway, DynamoDB on demand, CloudFront, S3 | per request and per GB; not estimated |
| Bedrock | per token, only for page explanations and rule drafts, capped per container at 200 successful explanation calls (failed calls are not counted) and 60 drafting calls (every call counted) |
| The benchmark | see section 6 |

`MonthlyBudgetUsd` puts an account-wide budget on the alarm topic when a
number is wanted rather than an estimate.
