# Threefold: demo video script

**Category:** `#workplace-efficiency` · **Lane:** `#community`
**Target length:** 2:45, under a 3:00 hard cap
**Everything shown is live:** <https://d1og72wpk4aqig.cloudfront.net/> and its pages. Nothing in the script needs a mock, and every sentence of narration is something the screen shows or the code does.

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
  and walk the five steps.
  1. **Make a sandbox.** A project `Acme-Sandbox-<8 hex>` appears, seeded with
     twelve synthetic hook calls from all three agents.
  2. **See what would be refused.** The would-refuse calls, grouped by rule.
     Nothing was refused: the project is in Observe.
  3. **Label each call.** Mark the real violations correct and the one false
     alarm as a false alarm. Rules turn Ready or Noisy.
  4. **Promote.** Enforce the rules that earned it; the noisy one keeps
     observing.
  5. **Send it again.** The same kind of call is refused, with the fix
     Threefold suggests, whether that fix passed the same gates, and a sentence
     from Amazon Bedrock explaining the refusal. The walkthrough sends this call
     in a `sim-` session, which the service enforces whatever the stage, so the
     narration below describes what the stage does for real hooks rather than
     presenting this one refusal as caused by the promotion.
- **Narration:**
  > "Every project starts in Observe. Calls are judged and recorded, and the
  > dashboard shows what each rule would have refused. You mark each one correct
  > or a false alarm. A rule whose every flag was correct is Ready. Promote the
  > project with the rules that earned it, and the next violation is refused
  > before it lands, with a fix that has itself been run through the same gates.
  > Demote is one click."

## Scene 4: one command, and a real agent refused (1:45 to 2:15)

- **Visual:** the dashboard's `#/connect` page, copy the command, run it in a
  repository:

  ```powershell
  irm https://d1og72wpk4aqig.cloudfront.net/install.py -OutFile threefold.py; py threefold.py connect --project Acme-Billing
  ```

  The installer lists what it wrote, sends one dry-run call, reports it
  recorded, and opens the project page. Cut to Claude Code asked to write
  `import boto3` into a domain file of a project promoted to Enforce on a stack
  you operate (see the checklist): the deny
  appears with the rule's name and the fix's one-line summary, and the file is
  not created.
- **Narration:**
  > "Connecting a repository is one command. It installs the hook for the
  > agents it finds, keeps every file it writes out of git, and opens the
  > project. When a rule is in force, the agent is refused before the write,
  > and told what to do instead. We checked on the file system that the refused
  > file is not created, for Claude Code and for Antigravity. Codex has not been
  > measured yet, so we make no claim for it."

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

## Recording checklist

- [ ] Browser: `https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try`, in a
      fresh private window so the walkthrough starts at step 1.
- [ ] Browser: `https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/connect`
      and `#/proof`.
- [ ] Terminal: a synthetic `Acme-*` repository, connected with the command
      above and promoted to Enforce on a stack you operate (the public stack has
      no operator key, so only sandbox projects can be promoted there). No real
      project or company name on screen.
- [ ] CloudWatch console: `threefold-prod-operations` in eu-west-1.
- [ ] Do not show a test count or any benchmark number: none is measured yet.
- [ ] Final length between 2:35 and 2:45.
