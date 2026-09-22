# Threefold: demo video script

**Category:** `#workplace-efficiency` · **Lane:** `#community`
**Target length:** 2:45, under a 3:00 hard cap
**What is on screen:** the public site <https://d1og72wpk4aqig.cloudfront.net/> and its pages; a real Claude Code session in a synthetic `Acme-*` repository connected to that same public stack; and, in scene 5, the public stack's CloudWatch dashboard in the AWS console, which needs the operator's own AWS sign-in. Nothing is mocked and no other stack appears. Every sentence of narration is something the screen shows or the code does; where each spoken claim comes from is listed after the scenes.

---

## Scene 1: the problem (0:00 to 0:20)

- **Visual:** title card, "Threefold: refuse the edit, not the pull request".
  Then a coding agent in a terminal writing `import boto3` into a file under
  `src/domain/`.
- **Narration:**
  > "Coding agents write code faster than anyone reviews it. The architecture
  > checks most teams have run in CI, after the agent has moved on. The only
  > moment a bad edit can still be refused is when the agent asks to make it.
  > Threefold answers at that moment."

## Scene 2: who decides (0:20 to 0:45)

- **Visual:** the architecture diagram from `docs/ARCHITECTURE.md`: the hook on
  the developer's machine, CloudFront with WAF, API Gateway, one Lambda,
  DynamoDB, and Bedrock off to the side.
- **Narration:**
  > "One hook file sits in front of Claude Code, Codex and Antigravity. It
  > refuses a credential on the machine and sends the rest to a service on AWS.
  > Deterministic gates decide: the architect's layering rules, credentials,
  > writes that would switch the hooks off, repeating calls, a spend ceiling.
  > Amazon Bedrock never decides. It explains a refusal to a person, and every
  > response says which one you are reading."

## Scene 3: the two-stage rollout, live (0:45 to 1:45)

- **Visual:** open `https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try`
  and walk the five steps. Note the sandbox's name: scene 4 uses it.
  1. **Make a sandbox.** A project `Acme-Sandbox-<8 hex>` appears, seeded with
     twelve synthetic hook calls from all three agents.
  2. **See what would be refused.** The would-refuse calls, grouped by rule.
     Nothing was refused: the project is in Observe.
  3. **Label each call.** Mark the real violations correct and the one false
     alarm (a test module under `tests/domain/`) as a false alarm. The Java,
     web and protected-path rules turn Ready; the Python rule turns Noisy.
  4. **Promote.** Enforce the rules that earned it, keeping
     `java-domain-stays-pure` checked; the noisy Python rule keeps observing.
  5. **Send it again.** Frame the result panel: the refusal, the fix Threefold
     suggests with whether that fix passed the same gates, and the sentence
     from Amazon Bedrock. This call runs in a `sim-` demo session, which the
     service enforces whatever the project's stage, so it shows what a refusal
     carries and is not evidence that the promotion caused it. The narration
     says so, and scene 4 shows the promotion refusing a real agent. Keep the
     step-5 card's own sentence ("now in Enforce. This time the rule in force
     refuses it") out of frame: it claims more than a `sim-` call shows.
- **Narration:**
  > "Every project starts in Observe. Calls are judged and recorded, and the
  > dashboard shows what each rule would have refused. You mark each one correct
  > or a false alarm. A rule whose every flag was correct is Ready; the one with
  > a false alarm is Noisy and keeps observing. Promote the project with the
  > rules that earned it, and from then on a hook's call that breaks one of them
  > is refused. The walkthrough's last call shows what a refusal carries: the
  > rule's reason, a fix that has itself been run through the same gates, and a
  > sentence from Amazon Bedrock for the person reading. That one is a demo
  > call, always enforced. Next, the same promotion refuses a real agent.
  > Demote is one click."

## Scene 4: one command, and a real agent refused (1:45 to 2:15)

- **Visual:** the dashboard's `#/connect` page, then the command run in a
  synthetic repository, with the sandbox from scene 3 as the project:

  ```powershell
  irm https://d1og72wpk4aqig.cloudfront.net/install.py -OutFile threefold.py; py threefold.py connect --project Acme-Sandbox-<8 hex>
  ```

  The installer lists what it wrote, sends one dry-run call, reports it
  recorded, and opens the project page, which shows the stage Enforce. Cut to
  Claude Code, started in that repository after connecting, asked to write a
  new domain class, `src/main/java/com/acme/domain/Invoice.java`, annotated
  with `javax.persistence.Entity`. The deny appears with the rule's name,
  `java-domain-stays-pure`, and, when a fix fits, the fix's one-line summary;
  the file is not created. This call comes from a real Claude Code session,
  not a `sim-` one, so it is refused because the project enforces that rule.
- **Narration:**
  > "Connecting a repository is one command. It installs the hook for the
  > agents it finds, keeps every file it writes out of git, and opens the
  > project. Here it joins the sandbox we just promoted. Claude Code asks to
  > write a domain class that imports a persistence framework, and is refused
  > before the write, told which rule and what to do instead. We checked on
  > the file system that a refused file is not created, for Claude Code and for
  > Antigravity. Codex has not been measured yet, so we make no claim for it."

## Scene 5: running it, and what is not claimed yet (2:15 to 2:45)

- **Visual:** the dashboard's overview; the CloudWatch dashboard
  `threefold-prod-operations` with its alarms; then the dashboard's `#/proof`
  page, which shows the benchmark as a pilot with nothing measured yet.
- **Narration:**
  > "It runs on AWS behind CloudFront and AWS WAF, on one Lambda function and one
  > DynamoDB table, with ten CloudWatch alarms and point-in-time recovery. A
  > live probe checks the project's claims against the deployed stack. What is not
  > measured, we do not claim: the benchmark comparing agents with and without
  > Threefold has only run as a pilot, and its headline will come from the full
  > run. Try the rollout yourself at the link below. It takes a minute."
- **End card:** `https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try`

---

## Where each spoken claim comes from

| Claim | Source |
|---|---|
| A promoted sandbox refuses a real hook's call, and a demoted one records it | [PRIMARY, 2026-09-22] `docs/evidence/PROBES_2026-09-22.md`, group "application": a hook `Write` of `import boto3` into `src/domain/` answered `APPROVED` with the would-refuse recorded before promotion, `BLOCKED_BOUNDARY_VIOLATION` with `project_stage` `enforce` after it, and `APPROVED` again after demotion. The probe's sessions are named `probe-<run id>-*`, not `sim-` |
| The walkthrough's last call is always enforced | `dashboard.html` sends it with `session_id` from `T.newSessionId('sim-')`; `application/projects.py`, `stage_applies`, returns false for any `sim-` session |
| Visitors can promote only sandbox projects on the public stack | `infrastructure/security_middleware.py`: a project write is open without a key only where reads are public and the name is `Acme-Sandbox-<8 hex>`; the public stack has no operator key [STATE-FILE] |
| A refused file is not created, for Claude Code and Antigravity; Codex not measured | [STATE-FILE], `docs/evidence/ENFORCEMENT_2026-09-21.md` |
| Behind CloudFront and AWS WAF | [PRIMARY, 2026-09-22] `aws cloudfront list-distributions`: the distribution behind `d1og72wpk4aqig.cloudfront.net` is `Deployed` with the web ACL `threefold-prod-edge-web-acl` attached |
| Ten CloudWatch alarms | [PRIMARY, 2026-09-22] `aws cloudwatch describe-alarms --alarm-name-prefix threefold-prod-`: 10 alarms, all `OK` |
| Point-in-time recovery | [PRIMARY, 2026-09-22] `aws dynamodb describe-continuous-backups`: `ENABLED` |
| A live probe checks the claims | [PRIMARY, 2026-09-22] `docs/evidence/PROBES_2026-09-22.md`, run against the public origin URL: 113 PASS, 0 FAIL, 3 SKIP |
| The benchmark has only run as a pilot | `docs/evidence/BENCHMARK_2026-09-22-PILOT.md`: its real-agent runs never reached the model, so no result exists |

---

## Recording checklist

- [ ] Only the public stack, <https://d1og72wpk4aqig.cloudfront.net/>. Do not
      connect to, or show the address of, any other stack.
- [ ] Browser: `https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try`, in a
      fresh private window so the walkthrough starts at step 1. Label the calls
      honestly, and keep `java-domain-stays-pure` checked when promoting.
- [ ] Browser: `https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/connect`
      and `#/proof`.
- [ ] Terminal: a synthetic git repository with no
      `src/main/java/com/acme/domain/Invoice.java` yet, connected with the
      command in scene 4, `--project` set to the sandbox's name, within 24 hours of making
      the sandbox (after that its stage configuration expires and the project
      reads as Observe again). On the public stack a visitor can promote only a
      sandbox, which is why scene 4 uses its name. Start Claude Code in that
      repository after connecting. No real project or company name on screen.
- [ ] CloudWatch console: `threefold-prod-operations` in eu-west-1, signed in
      as the operator. No account id, email address or other stack in frame.
- [ ] Do not show a test count or any benchmark number: none is measured yet.
- [ ] Final length between 2:35 and 2:45.
