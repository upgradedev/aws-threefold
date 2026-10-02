# Threefold against the six Well-Architected pillars

Each pillar lists what is actually deployed, with the template resource or the
live check behind it, and then what is missing. Nothing here is a score.

Tags: **[PRIMARY, date: check]** is a claim checked on that date against the
live public stacks with a read-only request or a `describe`/`get`/`list` call,
named after the colon; "rechecked" with a later date means the same check was
run again that day and answered the same. **[STATE-FILE]** is taken from
`STATE.md`. Resource names are the logical IDs in `deploy/template.yml` (the
regional stack, `threefold-prod`, eu-west-1) and `deploy/edge.yml` (the edge,
`threefold-prod-edge`, us-east-1). The topology itself is in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## 1. Operational excellence

| Practice | What is deployed |
|---|---|
| Infrastructure as code | Both stacks come from the two templates and nothing is configured by hand. The regional template takes its operations settings as parameters (`ReservedConcurrency`, `ApiThrottleRateLimit`, `ApiThrottleBurstLimit`, `SlowCallAlarmMs`, `AlarmEmail`, `MonthlyBudgetUsd`). An update that names none of them changes none of them, because `aws cloudformation deploy` keeps each parameter's previous value on an existing stack, not because the defaults match: `AlarmEmail` defaults to empty, while the public stack runs with an address subscribed. |
| Alarms | Eleven `AWS::CloudWatch::Alarm` resources: function errors, throttles and a run nearing the timeout; the API's p95 latency, 5xx and 4xx rates; table throttled requests and system errors; calls into halted sessions; evaluation latency; call volume. All eleven exist and were `OK` [PRIMARY, 2026-09-27: `describe-alarms --alarm-name-prefix threefold-prod-`]. Each notifies `ThreefoldAlarmTopic` on firing and on clearing; the public stack's topic has one confirmed email subscription [PRIMARY, 2026-09-22: `list-subscriptions-by-topic`, address not printed]. |
| Dashboard | `ThreefoldOperationsDashboard`, `threefold-prod-operations` [PRIMARY, 2026-09-22: `list-dashboards`]: the API, the function, the table, the governance metrics from this stack's own log, and an account-wide Bedrock row. |
| Metrics per stack | The function writes Embedded Metric Format records to `Threefold/Governance`, which both stacks share, so five `AWS::Logs::MetricFilter` resources read each stack's own log into `Threefold/<stack name>`. That is what lets an alarm tell the public stack from the private one. |
| Tracing and logs | X-Ray `Tracing: Active` on the function [PRIMARY, 2026-09-22: `get-function-configuration`]; function logs kept 30 days; API access logs in `/aws/vendedlogs/apigateway/threefold-prod/access` [PRIMARY, 2026-09-22: `get-stage`], kept 14 days; CloudFront standard logs in their own bucket, kept 30 days. |
| Checking the live system | `scripts/probe_live.py` checks a deployed stack against the project's claims and writes a dated evidence file. The latest committed runs, on 2026-09-27, ended 117 PASS, 0 FAIL, 3 SKIP through the edge and the same against the origin (`docs/evidence/PROBES_2026-09-27-2-edge.md`, `docs/evidence/PROBES_2026-09-27-2.md`). |
| Tests | The suite is hermetic (`THREEFOLD_OFFLINE=1` set before any AWS client is built) and includes tests that read the templates: every alarm, the fleet's schedule, the deploy role's coverage of every resource, the edge's behaviors. 5,624 passed and 6 skipped on 2026-09-27 [STATE-FILE]. CI runs it on every push and pull request to `main`, in ten jobs with bandit, pip-audit, trivy and `sam validate` on both templates, beside CodeQL, secret scanning and dependabot (`.github/workflows/`, `.github/dependabot.yml`; secret scanning is a repository setting) [STATE-FILE]. |
| Procedures | [`RUNBOOK.md`](RUNBOOK.md): deploy both stacks, publish the pages, backfill rollups, probe, benchmark, roll back, tear down. |

**Gaps.**
- The deploy workflow in `.github/workflows/` is written, but the role it
  assumes is deliberately not created: without a permissions boundary, a role
  that may create roles and write their policies is in practice an account
  administrator. Every deploy so far was run by hand with the runbook's
  commands [STATE-FILE].
- X-Ray shows each invocation and its cold start only: the X-Ray SDK is not
  bundled, so DynamoDB and Bedrock calls inside an invocation are not segments.
- No procedure is automated for restoring the table from point-in-time recovery.

---

## 2. Security

| Practice | What is deployed |
|---|---|
| Edge firewall | `WebAcl` on the distribution with `AmazonIpReputationList`, `RateLimitPerIp` (1,000 requests per address per five minutes, answered 429), `CommonRuleSet` and `KnownBadInputsRuleSet` [PRIMARY, 2026-09-22: `wafv2 get-web-acl`, edge `describe-stacks`]. The body-inspecting rules count rather than block, because every POST body is an agent's source code and a block reads to the hook as a refusal; the template says why, rule by rule. |
| Response headers | `SecurityHeadersPolicy` on every behavior: HSTS for two years, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a content security policy with `default-src 'none'` [PRIMARY, 2026-09-22, rechecked 2026-09-27: response headers of `GET https://d1og72wpk4aqig.cloudfront.net/`]. |
| Private origin | `WebBucket` blocks all public access, uses bucket-owner-enforced ownership and AES-256 encryption, and its policy lets only this distribution read, through origin access control, and denies anything but TLS. |
| Who may do what | Reads are open where `PublicReads=true` (the public stack [PRIMARY, 2026-09-22: `describe-stacks`]) and need the operator where it is `false`. The policy, the layering rules, project stages and reviews need the operator on every stack; on the public stack only `Acme-Sandbox-<8 hex>` projects are writable anonymously. The public stack is deployed with no operator key, so those writes are refused outright there [STATE-FILE]; the demo fleet's labels and promotions are made inside the function, not through the API. |
| Credentials | The operator key is a `NoEcho` parameter. Sign-in codes (120 seconds, single use) and sessions (12 hours) are stored as SHA-256 hashes in the table; the browser never holds the key. The hook reads a key from a file, never sends it to an endpoint only a repository names, and refuses a credential in a tool call on the developer's machine. |
| Least privilege | The function's role may invoke only `eu.anthropic.claude-*` inference profiles and `anthropic.claude-*` foundation models, may touch only its own table, and may use its stack's KMS key only to sign, beside the log and X-Ray writes the SAM transform attaches; it holds no S3 rights. The fleet's schedule has a role of its own that may invoke this one function and nothing else, assumable only by EventBridge Scheduler for this account. |
| Abuse bounds | The HTTP API's throttle, 100 requests a second with a burst of 200 for each of its seven routes on its own (`DefaultRouteSettings`), so page reads (`ANY /{proxy+}`) and hook verdicts (`POST /evaluate-tool-call`) each get that allowance [PRIMARY, 2026-09-22: `get-stage`, `get-routes`]; it bounds a flood on any one route and does not keep page reads from crowding out verdicts; reserved concurrency 25 [PRIMARY, 2026-09-22: `get-function-concurrency`]; a per-address token bucket in each container (60 in a burst, 2 a second); bodies over 1 MB refused; Bedrock calls capped per container: 200 successful explanation calls (a failed call is not counted, so failures are not bounded by it) and 60 drafting calls, each counted whether it was answered or not; and drafting capped for each stack at 400 model calls a UTC day, claimed in the stack's own table before the model is called and answered 429 once spent, so the account's two stacks may make 400 each. |
| Data | Table encryption with the AWS managed key `aws/dynamodb` [PRIMARY, 2026-09-22: `describe-table`, `SSEType: KMS`], whose use appears in CloudTrail as KMS events; DynamoDB caches the table key, so they come periodically, not once per request. Project names outside `AllowedProjectPattern` are stored as `unlabelled`, developers are shown only as short hashes, and ledger rows keep a command's program name as their target; a refusal's reason can quote the command it refused, passed through the credential redactor and cut at 240 characters, and where reads are public that reason is public. `AlarmEmail` is `NoEcho`. |

**Gaps.**
- The origin URL is public and answers every route without the web ACL. Since
  2026-10-01 its pages carry framing denial, HSTS, `nosniff` and a referrer
  policy from the function itself, but not the edge's full content security
  policy [PRIMARY, 2026-10-02: `GET /prod/` on the origin].
- The edge secret is not authentication, and it is readable by anyone in the
  account allowed `cloudfront:GetDistributionConfig` or
  `lambda:GetFunctionConfiguration`. See `ARCHITECTURE.md` section 8.6.
- The content security policy allows `'unsafe-inline'` scripts and styles,
  because the pages use inline scripts and handlers.
- Evaluating a call, the scenarios and the certificate are open POSTs on the
  public stack by design, each bounded by the session it names; the kill
  switch answers anonymously only for a scenario session (sim-*), and freezing
  an id nobody recorded is 404 [STATE-FILE].
- The function role holds no S3 rights at all: the uploader nothing called and
  its grant were deleted together, and the provisioned bucket stays empty. The
  Bedrock grant covers every Claude model on those two patterns, not only the
  one configured.
- The certificate covers the session's own stored verdicts, fingerprinted
  unkeyed and signed with KMS where the stack holds a key, but nothing in CI
  requires one before a merge [STATE-FILE].

---

## 3. Reliability

| Practice | What is deployed |
|---|---|
| Backups | `PointInTimeRecoveryEnabled: true` on `ThreefoldTable`, 35 days, restorable to any second [PRIMARY, 2026-09-22: `describe-continuous-backups` `ENABLED`]. The pages bucket is versioned, and replaced pages stay restorable for 30 days. |
| State that must survive a container | A halted session is written through to the table, so another container refuses it too [STATE-FILE]. Layering rules, the saved policy and each project's stage are re-read by every warm container after 30 seconds (`application/evaluator.py`), so a change reaches all of them within half a minute. The stage of a visitor's sandbox is read on every call instead, with a strongly consistent read, so a promotion applies to every call that arrives after it, on any container. A fleet tick claims its quarter hour in the table first, so a tick delivered twice sends nothing the second time. |
| Failure isolation | The ledger write and the rollup increment are best effort: a failure logs and never fails a verdict. A Bedrock failure or timeout (1 s connect, 2.5 s read) falls back to a deterministic sentence, labelled as such. |
| Managed, regional services | Lambda, API Gateway, DynamoDB on demand, EventBridge Scheduler for the fleet, and CloudFront; no server or capacity to manage. |
| Readiness | `/readyz` exercises the table with a real read and answers 503 when a dependency is unreachable [STATE-FILE]. |
| The hook's failure mode | Fails open by default, so a service outage does not stop developers; `THREEFOLD_FAIL_CLOSED=1` for teams that prefer the opposite. Credential refusal happens on the machine and does not depend on the service. |

**Gaps.**
- One region. The regional stack has no standby, and the edge forwards only to
  it.
- A restore from point-in-time recovery creates a new table and nothing points
  the function at it without a template change.
- For up to 30 seconds after a policy, rule or stage change, a call landing on
  another warm container is judged under the old one.
- The per-address limiter, the explanation cap and the per-container drafting
  cap live in each container's memory, so they bound a container, not the
  stack; only the daily drafting budget is kept in the table, so it bounds the
  stack, and each stack in the account has its own.
- While the hook cannot reach the service, the service judges nothing.

---

## 4. Performance efficiency

| Practice | What is deployed |
|---|---|
| No model on a hook's verdict | The gates are standard-library Python; a hook sends `explain: false`, and the reviewer returns before any model call for it, which a test pins. |
| Measured latency | From the probing machine, 2026-09-27, in one pass: through the edge, `POST /evaluate-tool-call` p50 197 ms, p95 341 ms over 20 samples and `GET /status` p50 147 ms, p95 183 ms over 10 (`docs/evidence/PROBES_2026-09-27-2-edge.md`); against the origin, the same calls p50 308 ms, p95 343 ms and p50 280 ms, p95 1,329 ms (`docs/evidence/PROBES_2026-09-27-2.md`). Over 10 samples a p95 is close to the slowest one. Every figure includes the network from the probing machine; the gate's own time is not separated out. |
| Compute | arm64, 1024 MB (`FunctionMemoryMb`: Lambda gives CPU in proportion to memory), 15 s timeout [PRIMARY, 2026-09-27: `get-function-configuration`]. At 256 MB the dashboard's overview took 2.5 to 3.5 s and the demo fleet's tick 4.8 to 5.6 s; at 1024 MB, with the overview reading only the fields it uses, the overview took 0.6 s warm and the tick 1.3 s [PRIMARY, 2026-09-27: Lambda REPORT lines for the overview, the tick's own `Demo fleet tick:` log lines]. |
| Caching | Pages at the edge for 60 seconds (`PageCachePolicy`), assets up to a year (`AssetCachePolicy`), both invalidated by every `publish_web.py` run; every API behavior uses the managed CachingDisabled policy, because every answer is live. HTTP/2 and HTTP/3 at the edge, `PriceClass_100`. |
| Bounded fix cost | A refusal carries a validated fix only up to a size ceiling per kind of fix, set where the dearest fix of that kind reached about 60% of a 10 ms budget on the development machine (`application/evaluator.py`). |

**Gaps.**
- No load test has been run, so no throughput or concurrency ceiling is
  claimed.
- Page reads, hook verdicts and the fleet's tick share one function and one
  concurrency pool.
- Latency is probed from one machine, twenty samples at most per call; no
  figure from other places or under load is claimed.

---

## 5. Cost optimization

| Practice | What is deployed |
|---|---|
| Pay per request | Lambda, API Gateway, DynamoDB `PAY_PER_REQUEST`, CloudFront. No provisioned capacity anywhere. |
| The model is the exception | Bedrock is called only for a refusal a page asked to have explained, and for rule drafts, with per-container caps: 200 successful explanation calls, and 60 drafting calls counted whether answered or not; drafting is also capped for each stack at 400 model calls a UTC day, counted in the stack's own table. Approvals, hook verdicts and the fleet's calls never call it. |
| Data that expires | Sessions and the ledger after 30 days, rollups after 35, sign-in records within 12 hours, a sandbox's stage configuration after 24 hours (its ledger rows and counters keep the 30- and 35-day expiry above, and its calls stay in the public call lists until then), through the table's TTL [PRIMARY, 2026-09-22: `describe-time-to-live` `ENABLED`]. Logs 30 and 14 days; replaced page versions and access logs 30 days. |
| A budget, when wanted | `MonthlyBudgetUsd` creates an account-wide cost budget notifying the alarm topic. It is 0, so no budget, on the public stack [PRIMARY, 2026-09-22: `describe-stacks`]. |

**What it costs, ESTIMATE.** No bill has been read for this document. From
the tracks' notes and AWS list prices, not measured: the web ACL with its four
rules is about 9 USD a month plus a per-request charge; the alarms and the
dashboard come to a few USD a month; the KMS signing key is 1 USD a month
[STATE-FILE]; the rest is per request and was not estimated, including the
fleet's 96 ticks a day on the public stack.

**Gaps.**
- The edge adds a fixed monthly cost that the regional stack alone did not
  have.
- `EvidenceBucket` is provisioned and empty.

---

## 6. Sustainability

| Practice | What is deployed |
|---|---|
| Nothing idles | Every compute and database resource scales with requests; a quiet stack runs no compute, except that where `DemoFleet` is true (the public stack) the schedule invokes the function once every 15 minutes, by design, so the demo always has something true to show. |
| Efficient compute | arm64 for the function. |
| Less work per request | Pages are served from the edge cache rather than an invocation each; a hook's verdict involves no model call. |
| Keeping less | TTLs delete sessions, the ledger, rollups and sign-in records; logs and old page versions expire. |

**Gaps.** Nothing here has been measured. No figure for energy, carbon or
tokens saved by refusing a repeating call is claimed.
