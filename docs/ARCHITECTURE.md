# Threefold architecture

What is deployed, how a tool call and a page request travel through it, where
state lives, and the trade-offs behind each choice, including the ones that
cost something.

How to read the tags. **[PRIMARY, date]** marks a claim checked on that date
against a live stack with a read-only request or a `describe`/`get` call, and
the check is named beside it; "rechecked" with a later date means the same
check was run again that day and answered the same, unless the line says what
changed. **[STATE-FILE]** marks a claim taken from `STATE.md` and not
re-measured here. Everything else describes the code on `main` and names the
file it comes from.

---

## 1. Topology

```mermaid
flowchart LR
  subgraph dev["Developer machine"]
    agent["Coding agent<br/>Claude Code · Codex · Antigravity · Muse"]
    hook["threefold_hook.py<br/>local checks first"]
    agent -- "PreToolUse" --> hook
  end

  viewer["Browser"]

  subgraph edge["Stack threefold-prod-edge · us-east-1"]
    waf["AWS WAF web ACL<br/>IP reputation · rate per IP · 2 managed groups"]
    cf["CloudFront distribution<br/>security headers on every response"]
    s3["S3 bucket, private<br/>origin access control<br/>pages and /assets/*"]
    fn["CloudFront Function<br/>copies Host into X-Threefold-Viewer-Host"]
    miss["Two CloudFront Functions, in edge.yml<br/>a page address with no page:<br/>404.html, status 404"]
    waf --- cf
    cf -- "default: the pages" --> miss
    miss --> s3
    cf -- "/assets/*" --> s3
    cf -- "API paths" --> fn
  end

  subgraph region["Stack threefold-prod · eu-west-1"]
    api["API Gateway HTTP API<br/>stage prod · throttle · access logs"]
    lambda["One Lambda function<br/>Python 3.11 · arm64 · X-Ray<br/>reserved concurrency"]
    ddb[("DynamoDB, one table<br/>TTL · point-in-time recovery")]
    cw["CloudWatch<br/>logs · metric filters · 11 alarms<br/>dashboard · SNS topic"]
    ev[("S3 evidence bucket<br/>nothing writes to it")]
    kms["KMS key<br/>signs certificates"]
    sched["EventBridge Scheduler<br/>every 15 minutes, only where DemoFleet=true<br/>the synthetic Acme fleet"]
    api --> lambda
    lambda --> ddb
    lambda -. "logs and EMF" .-> cw
    lambda -. "kms:Sign" .-> kms
    sched -- "one tick, invoked directly" --> lambda
  end

  bedrock["Amazon Bedrock<br/>Claude Haiku 4.5<br/>eu. inference profile"]

  viewer --> waf
  hook -- "HTTPS POST /evaluate-tool-call" --> waf
  fn -- "origin path /prod<br/>+ X-Threefold-Edge secret" --> api
  hook -. "or straight to the API URL" .-> api
  viewer -. "or straight to the API URL" .-> api
  lambda -- "page explanations and rule drafts only" --> bedrock
```

The same picture in words:

```
viewer or hook ──► CloudFront (WAF, security headers)            us-east-1
                     ├─ /, *.html, /app, /assets/* ──► S3 (private, OAC)
                     │    (in edge.yml: a missing page gets 404.html, status 404)
                     └─ API paths (20 behaviors) ──► viewer-host function
                                                      + X-Threefold-Edge
                                                          │
                                                          ▼
                   API Gateway HTTP API, stage prod               eu-west-1
                     (throttle 100 rps, burst 200, per route; access logs)
                                                          │
                                                          ▼
                   One Lambda (python3.11, arm64, 1,024 MB, 15 s,
                     X-Ray active, reserved concurrency 25)
                     ├─► DynamoDB single table (TTL, PITR)
                     ├─► Bedrock Converse, Haiku 4.5 (pages and drafts only)
                     ├─► KMS Sign (certificates)
                     └─► CloudWatch Logs: EMF records, metric filters, alarms

                   EventBridge Scheduler, every 15 minutes, only where
                     DemoFleet=true ──► the same Lambda, invoked directly
                     with one tick of the synthetic fleet (section 7)
```

Both pictures draw the stacks as `deploy/template.yml` and `deploy/edge.yml`
build them. The table below is what was checked on the live stacks, and when.
No check in it covers the two missing-page functions; section 2.1 describes
them as `deploy/edge.yml` builds them.

What was checked on the live public stacks **[PRIMARY, 2026-09-22]**:

| Piece | Check | Answer |
|---|---|---|
| Edge site | `GET https://d1og72wpk4aqig.cloudfront.net/` | 200 `text/html`, `server: AmazonS3`, `via: ... (CloudFront)` (rechecked 2026-09-27) |
| Edge security headers | response headers of that GET | `strict-transport-security: max-age=63072000; includeSubDomains`, `content-security-policy: default-src 'none'; ...`, `x-frame-options: DENY`, `x-content-type-options: nosniff`, `referrer-policy: no-referrer` (rechecked 2026-09-27) |
| Edge routes API paths to the function | `GET .../status`, `.../api/overview?days=7`, `.../install.py`, `.../dist/manifest.json`, `.../openapi.json`, `.../prod/status` | all 200, JSON or text from the function (rechecked 2026-09-27) |
| Distribution | `aws cloudfront get-distribution` | `Deployed`, 3 origins, 21 cache behaviors, the web ACL attached |
| Web ACL | `aws wafv2 get-web-acl` | rules `AmazonIpReputationList`, `RateLimitPerIp`, `CommonRuleSet`, `KnownBadInputsRuleSet` |
| Edge stack | `aws cloudformation describe-stacks --stack-name threefold-prod-edge --region us-east-1` | `RateLimitPerFiveMinutes=1000`, `PriceClass_100`, `AccessLogs=true` |
| API stage | `aws apigatewayv2 get-stage --api-id raa131f9dj --stage-name prod`, `get-routes` | `DefaultRouteSettings` `ThrottlingRateLimit 100`, `ThrottlingBurstLimit 200`, which API Gateway applies to each of the 7 routes separately; access logs to `/aws/vendedlogs/apigateway/threefold-prod/access` |
| Function | `aws lambda get-function-configuration`, `get-function-concurrency` | `python3.11`, `arm64`, 1024 MB, 15 s, tracing `Active`, reserved concurrency 25 (rechecked 2026-09-27, after `FunctionMemoryMb` replaced the fixed 256 MB) |
| Table | `aws dynamodb describe-continuous-backups`, `describe-time-to-live` | point-in-time recovery `ENABLED`, TTL on `ttl` `ENABLED` |
| Alarms | `aws cloudwatch describe-alarms --alarm-name-prefix threefold-prod-` | 11 alarms, all `OK` (rechecked 2026-09-27, after the slow-call alarm moved to the API's latency and the near-timeout alarm was added) |
| Dashboard | `aws cloudwatch list-dashboards` | `threefold-prod-operations` |
| API stack parameters | `aws cloudformation describe-stacks --stack-name threefold-prod` | `PublicReads=true`, `DefaultHookStage=observe`, `BedrockModelId=eu.anthropic.claude-haiku-4-5-20251001-v1:0`, `ReservedConcurrency=25`, `MonthlyBudgetUsd=0` |

Two parameters added since that check are set on the public stack as well:
`DemoFleet=true`, which creates the fleet's schedule (live since 2026-09-26),
and `EnforceProjectPattern=^Acme-Live-.+$`, under which the projects of the
real agent `scripts/daily_live_agent.py` runs start in Enforce **[STATE-FILE]**.

The API's own URL, `https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/`,
stays public and serves the same pages from the function. It answers without
any of the edge's security headers: a `GET /prod/dashboard.html` returned only
`date`, `content-type`, `content-length`, `cache-control: no-cache` and API
Gateway's request id **[PRIMARY, 2026-09-22, rechecked 2026-09-27]**. The pages at
`/prod/` and `/prod/dashboard.html` carry none of the edge's headers; the
function's own files carry one of them, `x-content-type-options: nosniff`, on
`/install.py`, `/hooks/threefold_hook.py` and `/assets/threefold.js`, and
nothing else of the edge's set **[PRIMARY, 2026-09-22, rechecked 2026-09-27]**.
The bare `/prod` without the trailing slash is API Gateway's own 404, before
the function is reached **[PRIMARY, 2026-09-22, rechecked 2026-09-27]**.

---

## 2. How a request travels

### 2.1 A visitor opens the dashboard

1. `GET https://d1og72wpk4aqig.cloudfront.net/dashboard.html` passes the web ACL
   and matches the distribution's default behavior, so it is read from the
   private bucket through origin access control. `scripts/publish_web.py`
   uploaded that page with `__THREEFOLD_BASE_PATH__` replaced by the empty
   string, because behind the edge the pages and the API share one origin.
2. The page loads `/assets/threefold.js` (bucket, long edge cache, invalidated
   on every publish) and fetches `/api/overview`, `/api/projects` and so on.
   Those paths match API behaviors: caching disabled, every viewer header except
   `Host` forwarded, the viewer-host function run on the viewer request, and the
   origin sends `X-Threefold-Edge` with the edge secret.
3. API Gateway applies the throttle of the route the path matches (`ANY /{proxy+}`
   for these reads) and invokes the function, which
   strips the stage, checks the request in `security_middleware.py` and routes it
   in `api_handlers.py` / `app_routes.py`.
4. Charts are inline SVG drawn by `assets/threefold.js`. There is no chart
   library and no build step; Tailwind comes from its CDN at a pinned version,
   which the edge's content security policy names exactly.

A mistyped page address is answered by the edge itself, as `deploy/edge.yml`
builds it. On the default behavior alone, a viewer-request function
(`MissingPageFunction`) sends a page address, one with no file type or with
`.html`, that names no published page to `/404.html` and marks the request,
and a viewer-response function (`MissingPageStatusFunction`) turns that
answer's 200 into 404. A missing file of another type keeps the bucket's own
404, and a path an API behavior takes keeps the API's own answer. A
distribution-wide custom error response would have been simpler and is not
used, because it would also replace the API's RFC 7807 problem documents,
which are 403s and 404s too. The list of pages is written into both functions,
and `tests/unit/test_edge_page_not_found.py` holds it to what
`scripts/publish_web.py` publishes.

### 2.2 A coding agent's tool call

1. The agent is about to run `Write`, `Edit`, `MultiEdit`, `NotebookEdit` or
   `Bash` (Claude Code), `apply_patch`, `Edit`, `Write` or `Bash` (Codex),
   `write_to_file`, `replace_file_content`, `multi_replace_file_content` or
   `run_command` (Antigravity), or `write_file`, `edit_file` or `powershell`
   (Muse), and hands the call to `threefold_hook.py` on stdin.
2. The hook decides locally what may leave the machine (section 3). A
   credential is refused there and never sent.
3. What remains is one `POST /evaluate-tool-call` to the endpoint the
   repository's `.threefold.json` names. An install made from the edge names the
   edge, because the function writes the viewer's host into `/install.py` when
   the request carries the edge secret (section 8.6).
4. The function runs the gates (section 4.2), applies the project's stage,
   records one ledger row and one rollup increment, and answers.
5. On a refusal the hook prints a deny in the agent's own format, with the
   validated fix's one-line summary appended; on an approval, and in Observe,
   it prints nothing, so the agent's own permission flow runs unchanged.

The hook always sends `explain: false`, so no model is called for its
verdict, refused or not: `bedrock_reviewer.py` returns the deterministic
sentence before any model call for an approval or for a caller that sent
`explain: false`, which `tests/integration/test_bedrock_stays_off_the_enforcement_path.py`
pins.

### 2.3 A page asks for an explanation

The demo page's scenarios (`index.html`), the connect page and the last step
of the dashboard's `#/try` walkthrough send `explain: true` (the walkthrough's
call imitates a hook, `origin: "hook"`, on a sandbox project, but asks for the
sentence because a visitor reads it). The refusal on the demo page's first
screen sends `explain: false`, so it waits on no model. A body that leaves
`explain` out is read as true. When the verdict is a refusal and the caller asked,
`bedrock_client.py` asks Claude Haiku 4.5 through the
`eu.` cross-region inference profile for one sentence, with a 1 s connect and
2.5 s read timeout, at most 200 successful calls per container, and arguments
redacted and cut to about 2 KB before they reach the prompt. The response
names its source in `explanation_source`: `bedrock`, or a deterministic
fallback when the model was not asked or not reached. The verdict itself was
decided before the model was asked and does not change with its answer.

### 2.4 An architect drafts a rule

`POST /rules/draft` (from `rules.html`) sends a sentence and optional example
files. `rule_drafter.py` asks Bedrock for one layering rule, then treats the
answer as untrusted: it is parsed as JSON, checked by the same
`validate_rules` a save uses, set to `observe`, and tried on the examples by the
functions `POST /rules/explain` uses. Nothing is stored. A draft becomes a rule
only through `POST /rules`, which needs the operator on every stack. The route
is capped at 400 answer tokens and one repair (two model calls at most per
draft, each one request with a 1 s connect and 5 s read timeout) and 60
drafting calls per container, each counted when it is made, answered or not
(`interfaces/draft_routes.py` sets the cap, `rule_drafter.py` counts), where
the explanations' 200 count only successful calls. Above those, each stack
may make 400 drafting model calls a UTC day, counted in its own table, so the
two stacks in one account may make 400 each: a draft claims its two in the
stack's table before the model is called (`DRAFTBUDGET#<day>`, section 5), and
once the stack's day is spent it answers 429
`urn:threefold:error:draft-budget-spent` without calling it. When the model
cannot be reached the caller gets no draft rather than a canned one.

### 2.5 A visitor walks the rollout on a sandbox

`#/try` in `dashboard.html` walks five steps, and each step's requests go out
only when the reader presses its button. `POST /api/sandbox` makes an Observe
project `Acme-Sandbox-<8 hex>` that expires in 24 hours and sends twelve
synthetic hook calls from the three agents through the real evaluator, none
asking for an explanation (`application/sandbox.py`). Its answer names, in
`seeded_false_alarm`, the one seeded call a reasonable reviewer would mark a
false alarm, read off the seed and never off the ledger: a test module under
`tests/domain/` that `python-domain-stays-pure` flags because its path pattern
covers any folder named `domain`. The page lists the flagged calls, asks the
reader to spot that one while labelling (`POST /api/projects/<name>/reviews`),
reads readiness (`GET /api/projects/<name>`), promotes with the Ready rules
checked and nothing else, and then shows the call it will send before sending
it: `POST /evaluate-tool-call`, as the agent and tool that made a call a
promoted rule flagged, one the reader marked correct where there is one, in a
`try-` session and with `explain: true` (section 4.2 says why the session
matters). On the public stack the labels and the promotion need no key
because the project is a sandbox, and `POST /api/sandbox` is open there only
(section 4.4).

---

## 3. The hook on the developer machine

`src/threefold/hooks/threefold_hook.py` is one standard-library file for four
agents, served at `/hooks/threefold_hook.py` and inside
`/dist/threefold-bundle.zip`. The installer puts one shared copy in
`THREEFOLD_HOME/bin/` (`~/.threefold` unless set) and registers it in
`.claude/settings.local.json`, `.codex/hooks.json` and `.agents/hooks.json`,
and for Muse as a native plugin from the `.threefold-muse/` bundle it writes
(`src/threefold/tools/threefold_muse_plugin/`).

**Configuration.** Each setting is read from the environment variable, then
`<project root>/.threefold.json`, then `THREEFOLD_HOME/config.json`.
`.threefold.json` holds `project` (an `Acme-*` alias), `endpoint`, `mode`,
optionally `api_key_file` (a path, never the key) and optionally `include`
(globs; a call is sent only when everything it targets falls inside them). A
key is never sent to an endpoint that only a repository's file names, unless
the owner paired that endpoint with the key file in `THREEFOLD_HOME/config.json`.

**Decided on the machine, before any network call.**

- A credential in the call (the same ten shapes the service matches) is
  refused locally, in every mode, and never sent.
- Six kinds of call are held back, neither sent nor recorded by the service:
  `outside-root` (a target outside the project root), `agent-config` (under
  `~/.claude`, `~/.codex` or `~/.gemini`), `data-file` (by extension and by
  directory, `.git` included), `never-send` (a term from the owner's local
  `never_send.txt`), `not-included` (outside the `include` globs) and
  `no-project` (no project configured). `held_back.log` records the time and
  the category only. A call held back is not checked by anything.
- A write made with a file tool (`Write`, `Edit`, a patch, `write_to_file` and
  the like) to the files that decide whether the hooks run
  (`.claude/settings*.json`, `.codex/hooks.json`, `.codex/config.toml`,
  `.agents/hooks.json`, `.threefold.json`, `.git/hooks/`, `.git/config`) is
  refused locally in `enforce` mode, and in `managed` mode only while the stage
  last seen for the project is `enforce`. Otherwise the two differ by where the
  file lives. The agent settings files and `.threefold.json` are sent as their
  path, without their content, and the service records the attempt under
  `PROTECTED_PATH`, as a would-refuse in Observe. `.git/hooks/` and
  `.git/config` sit under `.git`, a data directory, so a file-tool write to
  them is held back as `data-file` (the bullet above): not sent, not recorded,
  and so absent from Observe and the review queue, although it is the write
  that would switch the pre-commit check off. A shell command that writes to
  any of these files is not refused on the machine in any mode: it is sent
  whole, and the service refuses it as `PROTECTED_PATH` where the project
  enforces and records it as a would-refuse where it observes.

**What leaves the machine,** for a call that is sent: the session id, the
project alias, `anonymous` or a 12-hex hash of `THREEFOLD_DEVELOPER` computed
locally, the tool name, the action type and the arguments (for a write, the
text being written), with paths made relative to the project root and a
command's working directory as `cwd`; plus `agent`, `origin: "hook"`,
`explain: false`, `hook_mode`, and `dry_run` when the mode says so.

**Modes.**

| Mode | Sent as | Who decides | Default for |
|---|---|---|---|
| `managed` | a real call | the project's stage on the service: Observe records, Enforce refuses | `connect` |
| `observe` | `dry_run` | nobody refuses: a hard cap on this machine, whatever the stage | the older `--repo` installer form |
| `enforce` | a real call | the project's stage on the service, as in `managed`; on the machine, a file-tool write to the hooks' own files is always refused | the hook alone, with no configuration |

The hook caches the `project_stage` each response names in
`THREEFOLD_HOME/stage/`, which is how `managed` knows the stage without asking.

**When the service cannot answer.** A timeout (4 s by default), a network
failure, a 429 or a 5xx means the call could not be judged: the hook prints
nothing and exits 0, so the agent's own permissions decide.
`THREEFOLD_FAIL_CLOSED=1` refuses such calls instead. Any other 4xx is read as
the service refusing the call, which is why the edge's rate limit answers 429
rather than 403, and why a hook pointed at a path the edge does not route (a
POST that lands on the bucket answers 403) refuses every call. The local checks
above do not depend on the network and still apply.

**At commit.** The installer also adds a pre-commit hook running
`threefold_cli.py check`, which judges the staged content on the machine with
the same engine. Its rules are the project's own, fetched from the service
(`GET /rules?project=`); when they cannot be fetched it falls back to
`.threefold/rules.json` as committed at `HEAD`, then to the rules Threefold
ships. In `managed` mode it reads the project's stage from
`GET /api/projects/<name>` and refuses only what the stage enforces; a stage
it cannot read refuses nothing, as the hook fails open
(`src/threefold/tools/threefold_cli.py`).

---

## 4. The service: one Lambda function

### 4.1 Layers

The code follows a clean-architecture split, and the domain imports nothing
outside the standard library.

| Layer | Directory | What lives there |
|---|---|---|
| Domain | `src/threefold/domain/` | the session aggregate and tool invocation (`models.py`), the cost breaker and token pricing (`circuit_breaker.py`), the loop detector, the boundary guard and credential scan (`boundary_guard.py`), layering rules, import readers for Python, Java, C# and TypeScript, segment-by-segment path matching, and the shell-write reader (`shell_writes.py`) |
| Application | `src/threefold/application/` | the evaluator and its stages, rule keys, the validated fix proposer, the ledger, rollups, insights and self-correction, projects and readiness, the sandbox, the demo fleet, the rule drafter, the Bedrock reviewer and the certificate issuer |
| Infrastructure | `src/threefold/infrastructure/` | the DynamoDB repository, the sign-in store, the Bedrock client, the KMS signer, the security middleware and rate limiter, EMF metrics, retries and the idempotency cache |
| Interfaces | `src/threefold/interfaces/`, `src/threefold/web/`, `src/threefold/hooks/`, `src/threefold/tools/` | the Lambda handler and its routes, the local development server, the pages and assets, the hook, the installer and the pre-commit CLI |

### 4.2 The gates, in the order they run

`GovernanceEvaluator._run_gates` in `application/evaluator.py`:

1. **A halted session** refuses every call (`BLOCKED_CIRCUIT_BREAKER`).
2. **The boundary guard** (`domain/boundary_guard.py`,
   `evaluate_tool_boundary`) reads the call once, in this order: a credential
   in any argument, at any depth (`BLOCKED_SECRET_DETECTED`); then protected
   paths (`.env`, anything under `.git`, `.ssh`, `.aws`, `secrets`, key files);
   the files that decide whether the hooks run (the agents' hook settings,
   `.threefold.json`); what a shell command writes (`domain/shell_writes.py`),
   where readable content is judged by the layering rules like a `Write`, an
   unreadable write to a covered path is refused, and `git commit --no-verify`
   and the other ways to point git at other hooks are refused; then the
   layering rules in force for the calling project over the content the call
   carries; and last, destructive commands. Every refusal after the credential
   is `BLOCKED_BOUNDARY_VIOLATION`.
3. **The loop detector** looks for any repeating cycle of byte-identical call
   signatures, up to the policy's history window, and then for the same cycle
   over same-shape calls — same tool, targets and argument keys, values
   ignored — at a longer fuse. A repeated read or poll (`git status`, `ls`,
   `gh run view`, a file read) is noted and never refused. For a hook the
   repeating call is refused and the session is not halted; for the demo's
   `sim-*` and page sessions the session is halted, which is the flagship demo.
4. **The cost breaker** refuses a call whose projected cost exceeds the
   single-call cap, which comes from the policy (`max_single_call_usd`, $1.00 on
   the public stack **[PRIMARY, 2026-09-22, rechecked 2026-09-27]**, `GET /policy/config`), or that would
   take the session past 105% of the `budget_usd` the call declares ($10.00 when
   the caller sends none, `application/dtos.py`; the 5% is
   `CostCircuitBreaker`'s `hard_limit_buffer` in `domain/circuit_breaker.py`,
   which nothing in `src/` overrides), or past the policy's own session
   ceiling, which binds every session regardless of the budget the caller
   declared. Tokens are the caller's own declaration, priced by the
   `model_id` the call names — a Haiku id takes the Haiku row, everything
   else the default Sonnet-class rate. A hook declares no tokens, so a hook
   call costs nothing here.

**Stage.** For calls with `origin` `hook` or `ci` that are not dry runs, the
project's configured stage applies (`CONFIG#project#<name>`). A project with
none starts in Enforce when its name matches the stack's
`EnforceProjectPattern` (empty by default; `^Acme-Live-.+$` on the public
stack **[STATE-FILE]**), and otherwise in the stack's `DefaultHookStage`,
`observe` on the public stack **[PRIMARY, 2026-09-22]**
(`application/projects.py`, `stage_of`). Observe evaluates the call as a dry
run: recorded, never refused, never halting. Enforce runs the gates and turns a refusal whose
rule key the project still observes into an observation. Page calls and every
call in a `sim-` session always enforce, whatever their origin, so the demo
behaves the same under any setting (`application/projects.py`, `stage_applies`).
Each container holds a project's stage for `RULES_REFRESH_SECONDS` (30 s) before
reading it again, so a promotion or a demotion applies at once on the container
that handled it and within 30 seconds on every other warm container; a call
landing elsewhere in that window is judged under the old stage. A visitor's
sandbox is the exception: no container holds its stage, which is read on every
call (`_reads_every_time` in `application/evaluator.py`). Every read of a stage
is strongly consistent (`load_project_config` in
`infrastructure/dynamo_repo.py`), so a sandbox's call that arrives after a
promotion or a demotion has been answered is judged under the new stage on any
container.
The request's `hook_mode` is recorded on the ledger row and plays no part in
choosing the stage, so a machine in `enforce` mode is refused only where the
project enforces. The last step of the `#/try` walkthrough sends its call in a
`try-` session, not a `sim-` one, so the stage decides it: the same call is
recorded and approved before the promotion and refused after it, which is the
one thing the walkthrough sets out to show
(`tests/pages/test_the_walkthrough_proves_the_promotion.py`).

**Rule key.** Every ledger row carries `rule_key`: the id of the layering rule
that decided, or `LOOP`, `PROTECTED_PATH`, `UNREADABLE_WRITE`, `CREDENTIAL`,
`BUDGET`, `HALTED_SESSION`, or `NONE`. Readiness, the review queue and the
charts group by it. The key is the gate that decided, as the guard states it,
carried beside the verdict; only a row or verdict that carries none, such as a
row written before the key existed, is given one from the gate's own words at
the head of its reason (the rule it names first, or the unreadable-write
gate's phrase), never from anything the reason quotes, so a command cannot
name the rule it is counted under (`application/rule_keys.py`,
`tests/security/test_a_command_cannot_name_the_rule_it_is_counted_under.py`).

**Readiness.** `GET /api/projects/<name>` gives each of the project's rules (its
layering rules in force, plus the gates `LOOP`, `PROTECTED_PATH`,
`UNREADABLE_WRITE` and `BUDGET`) a state from the labels on what it flagged in
the window (`application/rollups.py`, `readiness`): Noisy after any false
alarm; else Needs review while a call it would have refused is unlabelled;
else Ready when at least one call it flagged was marked correct; else Quiet.
Ready rests on labels alone. A refusal nobody labelled is evidence of nothing
either way: it may be a visitor's button on the demo page, or one of the
probes' page calls, which always enforce, and a rollup does not say who was
refused. So a rule whose only record is unlabelled refusals reads Quiet, with
its `refused` count on the row, and every row's recommendation is a sentence
true of the counts beside it.

**The fix.** `application/fix_proposer.py` builds a concrete fix for a
refusal (a rewritten file, an adapter and a port, an environment lookup in
place of a literal credential) and runs every proposed write back through the
same gates with the same rules before offering it; `validated` is true only
when all of them passed. No model is involved. The response carries it as
`suggested_fix`, and the hook appends its one-line summary to the deny reason.
Past a size ceiling per kind of fix (1,500 characters for a rewrite) a refusal
goes out without one, so the fix never costs more time than the gate.

### 4.3 What a decision writes

`record_decision` writes one ledger row per decision, credential text
redacted before it is kept, and adds the decision to its day's rollup with
DynamoDB `ADD`. The rollup is best effort: a failed increment never fails a
verdict. Tiles and charts read the rollups, so they stay exact however busy
the ledger is; lists read the ledger. The one figure that reads the ledger is
self-correction on the overview and each project page: up to 2,000 rows, and
only nine fields of each (`application/ledger.py`,
`interfaces/app_routes.py`). Review labels (`correct`, `false_alarm`)
are stored on the ledger row itself.

`scripts/backfill_rollups.py` adds ledger rows written before rollups existed,
each exactly once: a row is claimed by a conditional update before it is
counted.

### 4.4 Access

`infrastructure/security_middleware.py` decides every request before routing.

- **Reads.** With `PublicReads=true` (the public stack) the data behind the
  pages is readable anonymously; with `false` it needs the operator.
- **Writes that outlive their caller** (the policy, the layering rules, a
  project's stage and reviews) need the operator on every stack. On the public
  stack a project named `Acme-Sandbox-<8 hex>` is writable by anyone, which is
  what `#/try` uses, and `POST /api/sandbox` is open there only.
- **The operator** is a key from `PolicyWriteApiKeys` (`X-API-Key` or
  `Authorization: Bearer`) or a live sign-in session. The public stack is
  deployed with no key, so its policy and rules writes are refused outright and
  sign-in is closed there (`POST /api/auth/links` answered 403 "Sign-In Is
  Closed Here" in `docs/evidence/PROBES_2026-09-22.md`, and again on the edge
  and the API in `docs/evidence/PROBES_2026-09-27-2-edge.md` and
  `docs/evidence/PROBES_2026-09-27-2.md`).
- **Sign-in without pasting a key.** `threefold.py open` sends the key from a
  local file in a header to `POST /api/auth/links`, gets a single-use code that
  expires in 120 seconds, and opens `dashboard.html#/signin?code=...`. The page
  exchanges the code for a session token valid for 12 hours. Codes and tokens
  are stored only as SHA-256 hashes; the browser never holds the key.
- **Per-address limit.** A token bucket per client address, 60 requests in a
  burst and 2 a second, kept in each container's memory. Bodies over 1 MB are
  refused.

### 4.5 Files the function serves

The pages and `/app`, `/assets/*` with the base path substituted, the hook at
`/hooks/threefold_hook.py` (and its older names), `/install.py` with the
stack's own address written in, `/dist/threefold-bundle.zip` and
`/dist/manifest.json` (the SHA-256 of every file, of the zip and of
`/install.py` as that host serves it), `/openapi.json` and `/proof.json`. All
are open on every stack, because a hook has to be fetchable before anyone
signs in.

---

## 5. State: one DynamoDB table

`PAY_PER_REQUEST`, partition key `PK` and sort key `SK`, server-side
encryption with the AWS managed key, TTL on the attribute `ttl`, point-in-time
recovery on (both checked live, section 1).

| Item family | `PK` / `SK` | Holds | Expires |
|---|---|---|---|
| Session | `SESSION#<session id>` / `METADATA` | the session aggregate: last 50 calls of history, cumulative cost and tokens, halt state and reason | 30 days |
| Ledger | `DECISION#<YYYY-MM-DD>` / `<timestamp>#<verdict id>` | one row per decision: project, developer hash, agent, origin, tool, target, status, `rule_key`, stage, hook mode, review label | 30 days |
| Rollups | `STATS#<YYYY-MM-DD>` / `<project>` | counters added per decision: calls, approved, refused, observed, per agent, per origin, per rule key | 35 days |
| Shared rules | `CONFIG#rules` / `METADATA` | the layering rules every project without its own set uses | never |
| Project rules | `CONFIG#rules#<project>` / `METADATA` | one project's layering rules | never |
| Project stage | `CONFIG#project#<name>` / `METADATA` | stage, observed rule keys, timestamps, last 20 history entries, sandbox flag | never; a sandbox after 24 hours |
| Project index | `CONFIG#projects` / `<name>` | a copy of every project's configuration, so the list is one Query | as above |
| Policy | `CONFIG#policy` / `METADATA` | the thresholds `/policy/config` returns | never |
| Sign-in | `AUTH#<sha256>` / `CODE` or `SESSION` | a sign-in code or a session, by hash only | 120 s for a code, 12 hours for a session |
| Drafting budget | `DRAFTBUDGET#<YYYY-MM-DD>` / `ACCOUNT` | the drafting model calls this stack claimed that UTC day, added by a conditional update; despite the sort key's name, each stack's table holds its own | 2 days |
| Run claim | `RUNCLAIM#<name>` / `CLAIM` | one scheduled run, put only if absent, so a fleet tick delivered twice sends nothing the second time | 24 hours |

A sandbox's expiry covers its stage configuration only. Once that is gone the
overview and the project list leave the project out (`application/rollups.py`,
`_is_expired_sandbox`), but its ledger rows and rollups keep their own 30- and
35-day expiry, and `GET /api/decisions` still lists its calls until then.

Every access is by key, except the sessions listing, which scans for session
metadata, in pages of at least 100 items and at most 50 pages per request.

---

## 6. Operations

- **Logs.** The function's log group keeps 30 days; the API access log keeps 14
  days, because every line holds a client address.
- **Metrics.** The function writes one CloudWatch Embedded Metric Format record
  to `Threefold/Governance` per call to `POST /evaluate-tool-call` (dimensions
  `Project` and `Environment`) and one per call to the universal adapter
  (dimension `Format`). `/simulate-loop`, `/simulate-secret`, the sandbox's
  seeded calls and the fleet's tick call the evaluator without writing either
  record, so the call-volume alarm counts calls to `/evaluate-tool-call` and
  the universal adapter only. Both stacks write that namespace, so five metric
  filters, each starting from either record, read the same records from each
  stack's own log group into `Threefold/<stack name>`:
  `ToolCallsEvaluated`, `VerdictApproved`, `CircuitBreakerTripped`, `LatencyMs`,
  `CurrentSessionCostUSD`. The last three are carried by the first route's
  record alone.
- **Alarms (11).** Function errors, throttles and an invocation over 12 seconds,
  near the 15-second timeout; API p95 latency, 5xx rate and 4xx rate; table
  throttled requests and system errors; calls into halted sessions; average
  evaluation latency; call volume. The slow-call alarm reads API Gateway's
  latency, what an HTTP caller waits for, so the demo fleet's scheduled tick,
  which invokes the function directly, is not in it; the near-timeout alarm
  sees a tick that runs long. Each notifies the SNS topic
  `<stack>-alarms` when it fires and when it clears, and treats missing data as
  not breaching. `AlarmEmail` subscribes an address (NoEcho); empty, the alarms
  still show their state in the console.
- **Dashboard.** `<stack>-operations`: the API, the function, the table, the
  governance metrics from this stack's log, and an account-wide Bedrock row.
- **Tracing.** X-Ray is active on the function. The X-Ray SDK is not bundled,
  so a trace shows the invocation and its cold start, not each DynamoDB or
  Bedrock call inside it, and an HTTP API does not start traces itself.
- **Budget.** `MonthlyBudgetUsd` creates an account-wide cost budget on the
  topic; 0, the value on the public stack, creates none.
- **Live probe.** `scripts/probe_live.py` checks a deployed stack against the
  project's claims and writes a dated evidence file. The latest committed
  runs, on 2026-09-27, ended 117 PASS, 0 FAIL, 3 SKIP
  through the edge (`docs/evidence/PROBES_2026-09-27-2-edge.md`) and the same
  against the API URL (`docs/evidence/PROBES_2026-09-27-2.md`). The first
  committed run, against the API URL on 2026-09-22, ended 113 PASS, 0 FAIL,
  3 SKIP (`docs/evidence/PROBES_2026-09-22.md`).

---

## 7. Two stacks, one template

| | Public demo | Private stack |
|---|---|---|
| Stack | `threefold-prod`, eu-west-1, with `threefold-prod-edge` in us-east-1 in front | a second stack from the same `deploy/template.yml` |
| `PublicReads` | `true` | `false`: the ledger, sessions, rules and policy need the operator |
| Operator key | none, so policy and rules writes and every stage write outside the `Acme-Sandbox-<8 hex>` projects are refused, and sign-in is closed | set, so the owner signs in with `threefold.py open` |
| `DemoFleet` | `true`, so the fleet's schedule exists **[STATE-FILE]** | `false`, the default, so there is none **[STATE-FILE]** |
| Traffic | the demo, the sandbox walkthrough, probes, visitors, the synthetic fleet, and a real coding agent's runs | the owner's own work at nine locations under `Acme-Proj-*` aliases **[STATE-FILE]**, every project in Observe in the snapshot of 2026-09-27 (`src/threefold/web/proof.json`) |
| In this repository | its URLs, its evidence | its stack name only; its address and key live on the owner's machine and are never committed |

**The synthetic fleet.** Every 15 minutes the schedule invokes the function
with one tick (`application/demo_fleet.py`): 20 to 40 synthetic hook calls
from Claude Code, Codex and Antigravity across six `Acme-*` projects, through
the real evaluator with `explain: false`, so no tick calls Bedrock. Now and
then it also acts as an operator would: it labels would-refuse calls, promotes
a project whose rules are ready and, rarely, demotes one. Those actions are
made in process, not through the API, which is how fleet projects reach
Enforce on a stack whose API refuses every stage write outside the
`Acme-Sandbox-<8 hex>` projects. The function accepts the tick only
from an event with no HTTP request context, so no caller of the API can
trigger one; Lambda does not retry it, and a tick delivered twice finds its
quarter hour claimed (`RUNCLAIM#`, section 5) and sends nothing. Nothing is
backdated: the fleet's history is as long as the schedule has run, since
2026-09-26 **[STATE-FILE]**.

**A real agent.** `scripts/daily_live_agent.py`, run from the owner's machine,
gives Claude Code or Codex one of the benchmark's Acme tasks through the edge,
in an `Acme-Live-<task>` project that starts in Enforce. On 2026-09-27
`benchmark/results/live/` holds two runs, Codex on 2026-09-26 and Claude Code
on 2026-09-27; its schedule on that machine is the owner's to create
**[STATE-FILE]**. The overview keeps five sources apart, `fleet`,
`live`, `probe`, `sandbox` and `other` (`application/rollups.py`,
`source_of`): `probe` is `Acme-Probe`, the project the service's own live
probes (`scripts/probe_live.py`) send their calls as, and like `fleet` and
`live` it is told apart only on the stack that runs the fleet. A page on the
public stack says in words that the fleet and the probes are synthetic.

Only aggregate numbers from the private stack may ever reach a public page,
through `scripts/build_proof.py`, which refuses to write if any string in its
output matches a project name, the key or an absolute path. The committed
`proof.json` carries such a private section, totals only, last rebuilt on
2026-09-27.

---

## 8. Trade-offs, stated honestly

### 8.1 Why one Lambda

Every route needs the same evaluator, the same rules cache and the same session
store, and a hook's verdict, a page's chart and a sign-in all read the same
table. One function means one deployment unit, one set of warm containers and
one place where the gates' code lives, so the page's "try it" and the hook's
verdict cannot drift apart.

What it costs. Page reads and hook verdicts share one reserved concurrency of
25, so a flood of dashboard loads competes with governed tool calls, and
nothing separates the two. The API throttle is 100 requests a second with a
burst of 200 for each of the API's seven routes on its own
(`DefaultRouteSettings`), so page reads (`ANY /{proxy+}`) and hook verdicts
(`POST /evaluate-tool-call`) each get that allowance; together with the edge's
per-address limit it bounds a flood, and does not keep one kind of request
from crowding out the other. The fleet's tick is one more invocation from the
same pool every 15 minutes on the public stack. One role carries the union of
what every route needs: Bedrock, the table (including `Scan` and
`DeleteItem`), `kms:Sign` on the stack's signing key, and the log and X-Ray
writes the SAM transform attaches; it holds no right on the evidence bucket.
And every in-memory limit (the per-address bucket, the explanation cap, the
per-container drafting cap) is per container, not per stack: N busy
containers allow N times the cap. The drafting budget of 400 model calls a day
is the exception, kept in the stack's table and so shared by every container of
that stack; it is per stack, not per account, since each stack has its own table.

### 8.2 Why a single table

Every access pattern is by key: a session by id, a day of the ledger by
partition, a day's rollups by partition, a project's configuration by name, a
sign-in by hash. One table gives one TTL attribute, one point-in-time recovery
setting, one IAM grant and one set of throttling alarms.

What it costs. All of one day's decisions share one partition key, so the
ledger's write rate for a single day is bounded by what one partition accepts;
no load test has been run, so no ceiling is claimed. The sessions listing is a
scan, bounded in pages rather than avoided. A restore from point-in-time
recovery creates a new table, and the stack has no switch to point the function
at it; no script for that exists.

### 8.3 Why the hook fails open by default

The hook sits in front of every write an agent makes. If a governance outage
stopped every developer's work, the first outage would end the rollout. So
when the service cannot answer, the hook prints nothing and the agent's own
permission flow decides.

What it costs. While the service is unreachable, nothing is judged by the
service: no layering rule, no loop detection, no ledger row. What still holds
is decided on the machine: a credential is refused, and in enforce (or managed
at an enforcing stage) a file-tool write to the hooks' own files is refused. A
team that prefers the other failure sets `THREEFOLD_FAIL_CLOSED=1`. The
pre-commit check does not replace the service in an outage: it judges on the
machine, but with the rules committed at `HEAD` or the shipped ones, and in
`managed` mode, which `connect` writes, a stage it cannot read refuses nothing
(section 3). Only a repository set to `enforce` on the machine still has its
commits refused while the service is down.

### 8.4 Why Bedrock is off the enforcement path

A verdict has to be the same for the same call, fast enough to sit in front of
every edit, free to repeat, and immune to what the call itself says. A model
is none of those: it is non-deterministic, it adds a network round trip and a
bill per call, and it would be reading text an agent wrote, which is where a
prompt injection would live. So the gates are standard-library Python and the
model is asked only after a refusal, only when the caller asked for a sentence
a person will read (the pages do; the hook always sends `explain: false`), and
only to phrase what was already decided. Approvals never call it, a real
hook's verdict never waits on it, and review labels and promotions never
involve it. Rule drafting uses it as a proposer whose output is
validated, set to observe and never saved without the operator.

What it costs. A hook's refusal carries the deterministic reason and the fix's
summary, not a model's sentence.

### 8.5 Why pages are served both by S3 behind the edge and by the function

The API URL was the project's first public address. It is in the evidence
files, it is the hook's built-in default endpoint, and installs made before the
edge point at it, so it keeps working exactly as before, pages included. The
edge adds what the function cannot give on its own: a web ACL, security headers
on every response, caching, and pages that are not an invocation each.

What it costs. There are two copies of every page. The function serves the
copy deployed with its code; the edge serves whatever `publish_web.py` last
uploaded, so a deploy that changes a page is not visible at the edge until the
pages are published again. On 2026-09-22 the two `dashboard.html` copies were
the same size, 159,267 bytes **[PRIMARY, 2026-09-22]**; on 2026-09-27 they
were byte for byte the same, 432,457 bytes with one SHA-256
**[PRIMARY, 2026-09-27]**, `GET /dashboard.html` on both URLs. The API URL has
no web ACL and serves its pages with none of the edge's headers (section 1),
and anyone can reach every route there, past the edge.

### 8.6 What the edge secret protects, and what it does not

Behind CloudFront the function would see an edge server as the caller and the
API's own name as the host. Two things broke without a fix: the per-address
limit put every viewer of one edge server in one bucket, and `/install.py`,
the manifest and sign-in links named the API URL, so a hook installed from the
edge talked to the API directly, past the web ACL.

Both API origins therefore send `X-Threefold-Edge` with a secret equal to the
API stack's `EdgeOriginSecret`, and CloudFront overwrites a viewer's own header
of that name. Only on a request carrying it does the function believe
`CloudFront-Viewer-Address` and the `X-Threefold-Viewer-Host` the viewer-host
function sets. Checked live **[PRIMARY, 2026-09-22, rechecked 2026-09-27]**:
`/install.py` fetched from the edge carries
`BAKED_ENDPOINT = "https://d1og72wpk4aqig.cloudfront.net/"`;
fetched from the API URL it carries the API URL; and fetched from the API URL
with a forged `X-Threefold-Viewer-Host`, with and without a wrong
`X-Threefold-Edge`, it still carries the API URL.

What it does not do:

- It is not authentication. The API URL stays public, every route answers
  there, and a caller who uses it skips the web ACL and the edge's headers.
- It does not keep reads private. That is `PublicReads`.
- It is readable inside the account: the distribution's configuration holds it
  (anyone allowed `cloudfront:GetDistributionConfig`) and so does the
  function's environment (`lambda:GetFunctionConfiguration`, which the CI
  deploy role's policy in `deploy/iam/` would grant; that role is written and,
  by the owner's decision, not created **[STATE-FILE]**). `NoEcho` keeps it
  out of `describe-stacks` only.
- It does not rotate itself. Changing it means deploying both stacks with the
  new value; until both agree, the function treats edge requests as untrusted.

---

## 9. The certificate, and what it is not

`POST /issue-certificate` returns a record over a session's own stored
verdicts: every judged call records its verdict on the session, and the
issuer reads those back, so no caller-supplied verdict can appear on it. It
carries an unkeyed SHA-256 fingerprint, which detects accidental corruption
and casual edits, and a KMS signature over the same bytes where the stack
holds a signing key. It is returned in the response and not archived; the S3
bucket the stack provisions stays empty. A session with no recorded verdicts
is refused with 400, and nothing in CI verifies a certificate before a merge
**[STATE-FILE]**.
