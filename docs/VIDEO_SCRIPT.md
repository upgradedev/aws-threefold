# Threefold: demo video script

**Category:** `#workplace-efficiency` · **Lane:** `#community`
**Target length:** 2:45, under a 3:00 hard cap
**What is on screen:** the public site <https://d1og72wpk4aqig.cloudfront.net/> and its pages; a real Claude Code session in a synthetic `Acme-*` repository connected to that same public stack; and, in scene 5, the public stack's CloudWatch dashboard in the AWS console, which needs the operator's own AWS sign-in. Nothing is mocked and no other stack appears. Most calls on the overview come from the public stack's synthetic Acme fleet, sent through the real gates every 15 minutes, and the page calls them synthetic; nothing in the video presents them as real use. Every sentence of narration is something the screen shows or the code does; where each spoken claim comes from is listed after the scenes.

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
  > "One hook file sits in front of Claude Code, Codex, Antigravity and Muse. It
  > refuses a credential on the machine and sends the rest to AWS.
  > Deterministic gates decide: layering rules, credentials, writes that
  > switch the hooks off, repeating calls, a spend ceiling. Amazon Bedrock
  > never decides. It explains a refusal to a person, and every response says
  > which one you are reading."

## Scene 3: the two-stage rollout, live (0:45 to 1:40)

- **Visual:** open `https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try`
  and walk the five steps. The page puts the walk at about two minutes for a
  reader, so the scene cuts between steps rather than speeding the screen up.
  Note the sandbox's name: scene 4 uses it.
  1. **Make a sandbox.** A project `Acme-Sandbox-<8 hex>` appears in Observe,
     seeded with twelve synthetic hook calls from all three agents; nothing
     was refused.
  2. **See what would be refused.** The flagged calls, grouped by rule. Each
     one ran: the project is in Observe.
  3. **Label each call.** The page says one of the calls is a false alarm and
     asks you to spot it. Mark the real violations correct and the test module
     under `tests/domain/` a false alarm; once every call has a label, the
     page says you found the call the sandbox seeded, and why it is one. The
     Java, web and protected-path rules turn Ready; the Python rule turns
     Noisy.
  4. **Promote.** The page has already checked the three Ready rules and left
     the Noisy Python rule unchecked, observing. Leave the checks as they are:
     `java-domain-stays-pure` is among them, and scene 4 needs it in force.
  5. **Send it again.** The page shows the call before it sends it: the newest
     call you marked correct under a layering rule now in force, with the
     same agent, file and import, which for this seed is Antigravity's write
     of `src/web/domain/cart.ts` importing `axios`. Press Send it and frame the
     result panel: the refusal, the fix Threefold suggests with whether that
     fix passed the same gates, and the sentence from Amazon Bedrock when the
     model answered. This call runs in a `try-` session, which the project's
     stage decides exactly as it decides a real hook's call, so the refusal is
     evidence that the promotion caused it: the same call was recorded and
     approved when step 1 seeded the sandbox, in Observe.
- **Narration:**
  > "By default, a project starts in Observe. Calls are judged and recorded,
  > and the dashboard shows what each rule would have refused. You mark each
  > one correct or a false alarm; this sandbox planted one false alarm, and
  > the page tells you if you found it. A rule whose every flag was correct is
  > Ready; the one with a false alarm is Noisy and keeps observing. Promote
  > with the Ready rules, and a hook's call that breaks one of them is
  > refused. The walkthrough sends that same call again, and now it is
  > refused: the rule's reason, a fix checked by the same gates, and a
  > sentence from Amazon Bedrock. Nothing about the call changed; the stage
  > did. Demote is one click."

## Scene 4: one command, and a real agent refused (1:40 to 2:15)

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
  > Antigravity, for Codex once, over its patch tool, and for Muse over its
  > file-write tool."

## Scene 5: running it, and what the benchmark says (2:15 to 2:45)

- **Visual:** the dashboard's overview, with the line that says where its
  calls come from (the synthetic Acme fleet among them) in frame; the
  CloudWatch dashboard `threefold-prod-operations` with its alarms; then the
  dashboard's `#/proof` page, on its chart "Did a violation land?", which
  shows the six measured series (four of Claude Code, two of Codex). Keep the
  card's verdict above the chart in frame: its cost sentence, 16 of 27 runs
  of the pressure tasks across both agents, is the figure the narration
  speaks. Hold on the Threefold lane, which reads 0% in every row, and let
  the pressure rows' "Tests passed, Threefold" figures (67%, 44% and 67%)
  stay readable in the same shot: the cost is part of the claim, not a
  footnote.
- **Narration:**
  > "On AWS, eleven CloudWatch alarms watch it. We measured it on Claude Code
  > and Codex, graded by a checker that does not import Threefold. A violation
  > landed in every series with no guidance, in three with the rules only
  > written down, and in none with Threefold enforcing. The cost: where the
  > prompt asked for the shortcut, the agents finished sixteen of twenty-seven
  > runs, and otherwise stopped and reported the conflict. Try it at the link
  > below."
- **End card:** `https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try`

---

## Where each spoken claim comes from

| Claim | Source |
|---|---|
| A promoted sandbox refuses a real hook's call, and a demoted one records it | [PRIMARY, 2026-09-27] `docs/evidence/PROBES_2026-09-27-2-edge.md`, through the edge the video shows (first on 2026-09-22, `docs/evidence/PROBES_2026-09-22.md`), group "application": a hook `Write` of `import boto3` into `src/domain/` answered `APPROVED` with the would-refuse recorded before promotion, `BLOCKED_BOUNDARY_VIOLATION` with `project_stage` `enforce` after it, and `APPROVED` again after demotion. The probe's sessions are named `probe-<run id>-*`, not `sim-` |
| The sandbox planted one false alarm, and the page tells you if you found it | `application/sandbox.py`: `POST /api/sandbox` answers with `seeded_false_alarm`, `{target, rule_key, why}`, read off the seed (the test module `tests/domain/test_order_totals.py` under `python-domain-stays-pure`), never off the ledger; `dashboard.html`'s `#/try` asks the reader to spot it only when the answer names it, and once every call has a label says whether the reader marked it a false alarm |
| Promote with the Ready rules | `dashboard.html`'s `#/try` checks the rules whose state is `ready` and nothing else (`readyKeys`); state comes from `GET /api/projects/<name>`, where Ready rests on labels alone (`application/rollups.py`, `_state`). Walked on a local server on 2026-09-28: Java, web and `PROTECTED_PATH` checked, the Noisy Python rule and the Quiet gates not |
| The walkthrough's last call is decided by the promotion | `dashboard.html` sends it with `session_id` from `T.newSessionId('try-')`, so `application/projects.py`, `stage_applies`, applies the project's stage to it as to any hook's call. `tests/pages/test_the_walkthrough_proves_the_promotion.py` sends that call to a fresh sandbox before and after promoting it: `APPROVED` with `project_stage` `observe`, then `BLOCKED_BOUNDARY_VIOLATION` with `enforce` |
| Visitors can promote only sandbox projects on the public stack | `infrastructure/security_middleware.py`: a project write is open without a key only where reads are public and the name is `Acme-Sandbox-<8 hex>`; the public stack has no operator key [STATE-FILE] |
| A refused file is not created: Claude Code and Antigravity, Codex over its patch tool, and Muse over its file-write tool | [STATE-FILE], `docs/evidence/ENFORCEMENT_2026-09-21.md`, `docs/evidence/ENFORCEMENT_2026-09-23.md` and `docs/evidence/ENFORCEMENT_2026-09-28-MUSE.md`. Codex is one run, over `apply_patch`; its shell route is not measured. Muse is over `write_file`; its edit and shell routes are not measured |
| Eleven CloudWatch alarms | [PRIMARY, 2026-09-27] `aws cloudwatch describe-alarms --alarm-name-prefix threefold-prod-`: 11 alarms, all `OK` |
| Claude Code and Codex, graded by a checker that does not import Threefold: a violation landed in every series with no guidance, in three with the rules only written down, and in none with Threefold enforcing; 16 of 27 pressure runs finished | [PRIMARY, 2026-09-22] the four Claude Code reports `docs/evidence/BENCHMARK_2026-09-22.md`, `-HAIKU.md`, `-PRESSURE-SONNET.md` and `-PRESSURE-HAIKU.md`, 162 runs from `benchmark/results/20260922T143932Z.jsonl`, `…145644Z.jsonl`, `…161455Z-pressure.jsonl` and `…162306Z-pressure.jsonl`; [PRIMARY, 2026-09-23] the two Codex reports `docs/evidence/BENCHMARK_2026-09-23-CODEX.md` and `-CODEX-PRESSURE.md`, 81 runs from `benchmark/results/20260923T025154Z-codex.jsonl` and `…031215Z-pressure-codex.jsonl`. Violation landed, no guidance / rules only written down (in `CLAUDE.md`, or `AGENTS.md` for Codex) / Threefold: 17% / 0% / 0%, then 39% / 17% / 0%, then 67% / 0% / 0%, then 100% / 56% / 0% for Claude Code, and 17% / 0% / 0%, then 100% / 11% / 0% for Codex; acceptance tests passed under Threefold 100% (18/18), 100% (18/18), 67% (6/9), 44% (4/9), then 100% (18/18), 67% (6/9). The pressure runs finished: Claude Code's two series 10 of 18, Codex's 6 of 9, 16 of 27 in all, the figure the live `#/proof` card's verdict states for the pressure tasks across both agents [PRIMARY, 2026-09-27: `GET /proof.json`, snapshot 2026-09-27T08:09:51Z]. Grading is `benchmark/checks.py`, which does not import Threefold |

---

## Recording checklist

- [ ] Only the public stack, <https://d1og72wpk4aqig.cloudfront.net/>. Do not
      connect to, or show the address of, any other stack.
- [ ] Browser: `https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try`, in a
      fresh private window so the walkthrough starts at step 1. Label the calls
      honestly, and leave the Ready rules the page checks as they are when
      promoting; `java-domain-stays-pure` is among them.
- [ ] Before filming scene 3, check that the live walkthrough is the one
      scene 3 describes: at step 3 it says whether you found the seeded false
      alarm, at step 4 only the Ready rules are checked, and at step 5 it waits
      for Send it. A page reaches the edge only with a page publish
      (`docs/RUNBOOK.md` section 3), and the sandbox's answer only with a
      regional deploy (section 1).
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
- [ ] Do not show a test count. Benchmark numbers are fine now that six
      series are measured (four of Claude Code, two of Codex), but only as
      the reports and `#/proof` state them, with the two families apart and
      the pressure series' completion rate in the same shot as its violation
      rate.
- [ ] Before filming scene 5, check that the live `#/proof` page shows the
      six series and no PILOT banner, each series' card citing the report made
      from its own rows, as the snapshot committed in
      `src/threefold/web/proof.json` (written 2026-09-27T20:30:24Z, with
      `--evidence-base`) does: a new snapshot reaches the page only with a
      regional deploy (`docs/RUNBOOK.md` section 1). Narrating measured results
      over a PILOT banner is the one thing this scene must not do.
- [ ] Final length between 2:35 and 2:45.
