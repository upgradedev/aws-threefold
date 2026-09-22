# Threefold: submission dossier

Field-by-field answers for the AWS Zero to Shipped submission form, written to
match the code on `main` and the live stacks. Where a sentence rests on a live
check it says so; where it rests on `STATE.md` it is tagged [STATE-FILE].

**Application name:** Threefold
**Category:** `#workplace-efficiency` · **Lane:** `#community`
**Tagline:** Threefold refuses a coding agent's edit the moment it is made, not after the commit, so your architecture does not rot while you sleep.
**Live application:** <https://d1og72wpk4aqig.cloudfront.net/> (CloudFront edge), origin <https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/> (API Gateway). Both answered anonymously with 200 on 2026-09-22 [PRIMARY, 2026-09-22].
**Try it in a minute, no account:** <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try>

---

## 1. Project description

Threefold governs the tool calls coding agents make. One standard-library hook
file sits in front of Claude Code, Codex and Antigravity and asks a service on
AWS about each write or command before it runs. Deterministic gates decide: a
domain file importing infrastructure under the architect's layering rules, a
credential in the arguments, a write that switches the hooks off, the same call
repeating, a spend ceiling. A team connects a repository with one command.
Every project starts in Observe, where calls are judged and recorded and no
rule refuses anything; a credential is still refused on the developer's
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
   credential shapes at any depth of the arguments; protected paths and every
   shell route to a write (redirections, heredocs, `sed -i`, `cp`, `git apply`
   and more); repeating cycles of identical calls up to period six; a spend
   ceiling on the tokens the caller declares.
3. **Rolls out in two stages.** Observe, review, readiness per rule (Ready,
   Quiet, Needs review, Noisy), Promote with the chosen rules, Demote in one
   click. Page and demo calls always enforce, so the public demo is unaffected.
4. **Suggests a validated fix with every refusal it can.** A rewritten file,
   a port and an adapter, or an environment lookup in place of a literal
   credential, run back through the same gates before it is offered; the hook
   appends its one-line summary to the deny reason.
5. **Shows the operation.** `dashboard.html`: overview tiles and inline SVG
   charts that open the calls behind them, projects, a project page with
   readiness per rule, the review queue, call drill-down, a connect wizard,
   sign-in, the `#/try` sandbox walkthrough and a `#/proof` page. Daily rollups
   keep the charts exact however busy the ledger is.
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
  edge. Its own stack in us-east-1.
- **API and compute:** Amazon API Gateway HTTP API (a throttle on each route,
  access logs) and one AWS Lambda function, Python 3.11 on arm64, X-Ray tracing,
  reserved concurrency.
- **State:** one Amazon DynamoDB table (sessions, the decision ledger, daily
  rollups, rules, project stages, sign-in records), with TTLs and point-in-time
  recovery.
- **AI:** Amazon Bedrock, Claude Haiku 4.5 through the `eu.` cross-region
  inference profile, Converse API, only for page explanations and rule drafts.
- **Operations:** ten CloudWatch alarms, a dashboard, Embedded Metric Format
  metrics read into a namespace per stack, an SNS topic.
- **Code:** clean architecture with a standard-library domain, no build step,
  no chart library, no npm. The hook and the installer are single
  standard-library files.
- **Infrastructure as code:** two CloudFormation templates, `deploy/template.yml`
  (SAM transform) and `deploy/edge.yml`.
- **Checking it live:** `scripts/probe_live.py`, run against the public origin
  on 2026-09-22: 113 PASS, 0 FAIL, 3 SKIP (`docs/evidence/PROBES_2026-09-22.md`).

## 5. Challenges we ran into

1. **Does a deny actually stop the write?** Two open bug reports said a hook's
   deny can be ignored, so it was measured per agent on the file system rather
   than assumed: Claude Code 2.1.220 and the Antigravity desktop app did not
   create the refused file. Codex could not be measured (its account had hit a
   usage limit), so nothing is claimed for it
   (`docs/evidence/ENFORCEMENT_2026-09-21.md`).
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

## 6. Accomplishments we are proud of

- The owner's own work has been governed since 2026-09-22 at nine locations,
  under `Acme-Proj-*` aliases, in Observe, reporting to a private stack from the
  same template [STATE-FILE].
- The public stack passed its own live probe, 113 checks PASS, 0 FAIL, 3 SKIP.
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

- The benchmark. The harness in `benchmark/` runs a coding agent on six
  synthetic tasks under three conditions and grades the result independently.
  Only a pilot exists, and its real-agent runs never reached the model, so no
  comparative number exists yet; the headline will be computed by
  `benchmark/report.py` from the full matrix
  (`docs/evidence/BENCHMARK_2026-09-22-PILOT.md`).
- Measure Codex enforcement once its account resets.
- Price each call by the model actually in use, and let the session ceiling and
  loop window come from the policy, as the settings page already admits they do
  not [STATE-FILE].
- Sign the governance certificate with a key and issue it from the session's
  stored history instead of verdicts the caller supplies [STATE-FILE].
