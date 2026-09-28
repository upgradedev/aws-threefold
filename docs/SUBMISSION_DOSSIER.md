# Threefold: submission dossier

Field-by-field answers for the AWS Zero to Shipped submission form, written to
match the code on `main`. Where a sentence rests on a check of a live stack it
is tagged [PRIMARY] with the date of the check; where it rests on `STATE.md` it
is tagged [STATE-FILE]. Everything else describes the code on `main`.

**Application name:** Threefold
**Category:** `#workplace-efficiency` · **Lane:** `#community`
**Tagline:** Threefold refuses a coding agent's edit the moment it is made, not after the commit, so your architecture does not rot while you sleep.
**Live application:** <https://d1og72wpk4aqig.cloudfront.net/> (CloudFront edge), origin <https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/> (API Gateway). Both answered anonymously with 200 on 2026-09-27 (`docs/evidence/PROBES_2026-09-27-2-edge.md`, `docs/evidence/PROBES_2026-09-27-2.md`) [PRIMARY, 2026-09-27].
**Try it in about two minutes, no account:** <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try>, the two-stage rollout on a sandbox project of your own. The first screen's loop halt, <https://d1og72wpk4aqig.cloudfront.net/>, is one click and well under a minute.

---

## 1. Project description

Threefold governs the tool calls coding agents make. One standard-library hook
file sits in front of Claude Code, Codex, Antigravity and Muse and asks a
service on AWS about each write or command before it runs. Deterministic gates
decide: a
domain file importing infrastructure under the architect's layering rules, a
credential in the arguments, a write that switches the hooks off, the same call
repeating, a spend ceiling. A team connects a repository with one command.
By default a project starts in Observe, where calls are judged and recorded
and no rule refuses anything; a credential is still refused on the developer's
machine, and so is a request the service cannot take at all, such as a body
over 1 MB, because the hook reads any 4xx other than 429 as a refusal. The
operations dashboard shows what each rule would have refused; the operator labels each of those
correct or a false alarm, and promotes the project to Enforce with the rules
that earned it, or demotes it with one click. A refusal carries a fix that has
itself been run through the same gates. Amazon Bedrock never decides: it
phrases a refusal for a person reading a page and drafts rules for an
architect, and every response says which of the two produced the sentence.

## 2. Inspiration

Coding agents write code faster than anyone reviews it, and the architecture
checks teams already have (import linters, architecture tests) run in CI,
after the agent has moved on to the next file. The one moment a bad edit can
still be refused is when the agent asks to make it. And a rule switched on for
many teams at once is a rule that gets uninstalled on its first false alarm,
so the rollout had to show what a rule would stop before it stops anything.

## 3. What it does

1. **Intercepts tool calls on the developer's machine.** The hook refuses a
   credential locally and never sends it, holds back what must not leave the
   machine (targets outside the project, agent configuration, data files,
   terms on the owner's never-send list), and sends the rest to be judged. On
   an approval it prints nothing, so the agent's own permission flow still runs.
2. **Judges with deterministic gates.** Layering rules per project in Python,
   Java, C# and TypeScript, read from each file's own import statements; ten
   credential shapes at any depth of the arguments plus the policy's own
   blocked-pattern list; protected paths and every shell route to a write
   (redirections, heredocs, `sed -i`, `cp`, `git apply` and more); repeating
   cycles up to the policy's history window (six by default), byte-exact and,
   two repeats later, over normalized call shapes; a spend ceiling on the
   tokens the caller declares, priced per model.
3. **Rolls out in two stages.** Observe, review, readiness per rule (Ready,
   Quiet, Needs review, Noisy), Promote with the chosen rules, Demote in one
   click. Ready rests on labels alone: a rule is Ready when calls it flagged
   were marked correct, none a false alarm and none waiting for a label,
   and a rule whose only record is refusals nobody labelled reads Quiet.
   Page and demo calls always enforce, so the public demo is unaffected.
   A stack can name projects that start in Enforce instead
   (`EnforceProjectPattern`); the public stack names the `Acme-Live-*`
   projects a real coding agent works in (section 6), since no operator is
   there to promote them. Both such projects read Enforce on the overview
   with no stage stored for them [PRIMARY, 2026-09-27: `GET /api/overview`].
4. **Suggests a validated fix with every refusal it can.** A rewritten file,
   a port and an adapter, or an environment lookup in place of a literal
   credential, run back through the same gates before it is offered; the hook
   appends its one-line summary to the deny reason.
5. **Shows the operation.** `dashboard.html`: overview tiles and inline SVG
   charts that open the calls behind them, projects, a project page with
   readiness per rule, the review queue, call drill-down, a connect wizard,
   sign-in, the `#/try` sandbox walkthrough and a `#/proof` page. The
   walkthrough takes a visitor through both stages in about two minutes, by
   its own count: the sandbox it makes names the one seeded call a reviewer
   should mark a false alarm, the page asks the visitor to spot it and says
   whether they did, promotion starts with the Ready rules checked, and the
   last call is shown before it is sent. On `#/proof` each benchmark series
   cites the report made from its own rows, linked in the public repository.
   Daily rollups keep the charts exact however busy the ledger is. On the
   public stack the overview says where its calls came from: the synthetic
   Acme fleet, the live agent, visitors' sandboxes, or anything else
   [PRIMARY, 2026-09-27: `GET /api/overview`, its `sources`]. The code on
   `main` counts the service's own probes apart as well, a fifth source.
6. **Connects in one command and signs in without a key.** `install.py`, served
   by the stack with its own address written in, installs the hook for the
   agents it finds; `threefold.py open` signs the operator in through a
   single-use link, so no key is pasted into a browser.
7. **Drafts rules with Bedrock, and does not trust the draft.** On the rules
   page an architect describes a boundary in a sentence; Claude Haiku 4.5
   proposes a rule, which is validated like a save, set to observe and tried on
   example files. Nothing is saved without the operator.

## 4. How we built it

- **Edge:** Amazon CloudFront with the pages in a private S3 bucket behind
  origin access control, AWS WAF (IP reputation, a per-address rate limit, two
  managed rule groups), security headers on every response, and a secret origin
  header so the function trusts the viewer's address and host only from the
  edge. Its own stack in us-east-1. The template answers a page address that
  has no page with the product's own 404 page and status 404, through two
  CloudFront Functions on the pages' behavior alone, so a path an API behavior
  takes keeps the API's own answer.
- **API and compute:** Amazon API Gateway HTTP API (a throttle on each route,
  access logs) and one AWS Lambda function, Python 3.11 on arm64, X-Ray tracing,
  reserved concurrency.
- **State:** one Amazon DynamoDB table (sessions, the decision ledger, daily
  rollups, rules, project stages, sign-in records), with TTLs and point-in-time
  recovery.
- **AI:** Amazon Bedrock, Claude Haiku 4.5 through the `eu.` cross-region
  inference profile, Converse API, only for page explanations and rule drafts.
- **Operations:** eleven CloudWatch alarms, a dashboard, Embedded Metric Format
  metrics read into a namespace per stack, an SNS topic.
- **Demo data:** on the public stack only (`DemoFleet`), an Amazon EventBridge
  Scheduler schedule sends a synthetic Acme fleet's calls through the real
  gates every 15 minutes [STATE-FILE], and the overview and the first screen
  count those calls apart and call them synthetic [PRIMARY, 2026-09-27:
  `GET /api/overview`, its `sources`, and the first screen].
- **Code:** clean architecture with a standard-library domain, no build step,
  no chart library, no npm. The hook and the installer are single
  standard-library files.
- **Infrastructure as code:** two CloudFormation templates, `deploy/template.yml`
  (SAM transform) and `deploy/edge.yml`.
- **Checking it live:** `scripts/probe_live.py`: 117 PASS, 0 FAIL, 3 SKIP
  through the edge and the same at the API origin on 2026-09-27, after that
  day's deploy (`docs/evidence/PROBES_2026-09-27-2-edge.md`,
  `docs/evidence/PROBES_2026-09-27-2.md`) [PRIMARY, 2026-09-27].

## 5. Challenges we ran into

1. **Does a deny actually stop the write?** Two open bug reports said a hook's
   deny can be ignored, so it was measured per agent on the file system rather
   than assumed: Claude Code 2.1.220 and the Antigravity desktop app did not
   create the refused file (`docs/evidence/ENFORCEMENT_2026-09-21.md`). Codex
   CLI 0.155.0 was measured on 2026-09-23, in one run and on one route: the
   hook refused an `apply_patch` adding `boto3` to a governed file, and the
   file's sha256 was unchanged afterwards. Its other routes are not measured
   (`docs/evidence/ENFORCEMENT_2026-09-23.md`). Muse 1.4.0 was measured on
   2026-09-28: the hook refused a `write_file` and the refused file was not
   created; its edit and shell routes are not measured
   (`docs/evidence/ENFORCEMENT_2026-09-28-MUSE.md`).
2. **Every write route, not only the Write tool.** An agent refused a `Write`
   can reach for `cat > file <<'EOF'`. The service reads a shell command for
   the writes it makes and judges readable content like a `Write`, and refuses
   an unreadable write to a covered path with "use Write or Edit so the rule can
   read it".
3. **A loop gate that does not lock people out.** Halting a developer's session
   for polling `git status` stopped real work. Repeated reads and polls are now
   noted, never refused, and a hook's loop refuses the repeating call without
   halting the session; the demo still halts on the third call.
4. **The edge behind the function's back.** Behind CloudFront the function saw
   an edge server as every caller and handed out installers pointing past the
   firewall. A secret origin header, and a CloudFront Function copying the
   viewer's host, fixed both; the installer fetched from the edge names the edge
   [PRIMARY, 2026-09-22].
5. **A false refusal a real agent found.** The live agent's first run (section
   6), Codex on 2026-09-26, was refused a read ending in PowerShell's `2>$null`,
   which the command check took for a write to a shell expansion. Fixed and
   deployed on 2026-09-27
   (`tests/security/test_discarding_errors_in_powershell_is_not_a_write.py`).
   Looking for a way around the fix found an older hole, closed in the same
   change: inside double quotes the shell drops the backslash before `$`, so
   `bash -c "echo ... > src/domain/\$f"` had been approved [STATE-FILE].

## 6. Accomplishments we are proud of

- The owner's own work has been governed since 2026-09-22 at nine locations,
  under `Acme-Proj-*` aliases, in Observe, reporting to a private stack from the
  same template [STATE-FILE].
- The public stack passed its own live probe on 2026-09-27, 117 checks PASS,
  0 FAIL, 3 SKIP through the edge and the same at the API [PRIMARY,
  2026-09-27], and the private stack 101 PASS, 0 FAIL, read-only
  [STATE-FILE].
- A real coding agent works on the public stack.
  `scripts/daily_live_agent.py` gives Claude Code or Codex, alternating by
  date, one of the benchmark's Acme tasks in an `Acme-Live-*` project that
  starts in Enforce, and the overview counts its calls as a source of their
  own. Until the owner creates its scheduled task, a day runs only when the
  script is started by hand [STATE-FILE]. Two runs as of 2026-09-27: Codex on
  2026-09-26, 8 calls, one refused (falsely, challenge 5), and Claude Code on
  2026-09-27, 4 calls, none refused; the overview's live source held those
  12 calls in 2 projects [PRIMARY, 2026-09-27: `GET /api/overview`]. In both
  runs no violation landed and the acceptance tests passed, by their rows in
  `benchmark/results/live/`.
- Threefold was measured against the alternative rather than asserted over it.
  162 Claude Code runs on 2026-09-22, two models, two task families, graded by
  a checker that does not import Threefold: a governed violation landed in
  0 of 54 runs with Threefold enforcing, against 3, 7, 6 and 9 of the unguided
  runs of each series, and 0, 3, 0 and 5 with the same rules in `CLAUDE.md`
  and nothing enforcing them (`docs/evidence/BENCHMARK_2026-09-22*.md`).
- Nothing on the public stack names a real project or person: names outside the
  `Acme-*` pattern are stored as `unlabelled`, developers appear only as short
  hashes.
- The model is kept off every verdict: approvals and hook calls never wait on
  it, and a test pins that.

## 7. What we learned

- A governance rule has to earn its enforcement. Showing what it would have
  refused, and letting a person label it, is what makes switching it on safe.
- A refusal is worth more with a fix, and a fix is only worth giving if it
  passes the same gate that refused the original.
- Measure the enforcement per agent. A hook's deny is a request to the agent,
  and whether it is honoured is an empirical question.

## 8. What's next

- The benchmark, beyond what is measured. Six series ran: four of Claude Code
  2.1.220 on 2026-09-22 and two of Codex CLI 0.155.0 on 2026-09-23, each under
  no guidance, the rules in the prompt, and Threefold enforcing. A governed
  violation landed in 17% / 0% / 0%, 39% / 17% / 0% and 17% / 0% / 0% on the
  standard tasks, and 67% / 0% / 0%, 100% / 56% / 0% and 100% / 11% / 0% on the
  pressure ones: no violation under Threefold in any series, while the rules in
  the prompt alone held in both families with `claude-sonnet-5`, on the
  standard tasks only with Codex, and in neither with `claude-haiku-4-5`.
  The price is in the pressure series, where the governed agent finished 10 of
  18 runs in the two Claude series (16 of 27 counting Codex) and otherwise
  stopped and reported the conflict
  (`docs/evidence/BENCHMARK_2026-09-2*.md`). What is still missing is a third
  agent (Antigravity is unmeasured), a task set someone else wrote, and more
  than 18 or 9 runs a cell.
- Measure the three Codex enforcement cells 2026-09-23 did not reach: a
  refusal on the governed write over the shell, under the real hook and under a
  deny-only one, and one over `apply_patch` from a hook that makes no network
  call.
- Verify a certificate before a merge: the document is now issued from stored
  history and signed, but nothing in CI requires one. A check that refuses a
  pull request whose session has no valid certificate would make it
  load-bearing [STATE-FILE].
