# Refuse the edit, not the pull request: governing coding agents on AWS with deterministic gates and Amazon Bedrock

**For:** AWS Builder Center
**Hackathon:** AWS Zero to Shipped 2026 · **Category:** `#workplace-efficiency` · **Lane:** `#community`
**Try it, no account:** <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try>

A claim about the live stacks is tagged **[PRIMARY, date]** when it was
checked on that date with a read-only request or an AWS `describe`/`get`/`list`
call, and **[STATE-FILE]** when it is taken from the project's state ledger
and was not re-measured for this article.

---

## The moment that matters

A coding agent asked to archive orders to S3 can take the shortest path:
`import boto3` in the domain entity, because that makes the test pass. Architecture checks would flag it in CI, after the agent has built on the
mistake.

There is exactly one moment when that edit can still be refused cheaply: when
the agent asks to make it. Claude Code, Codex, Antigravity and Muse all let a
hook see a tool call before it runs and deny it. Threefold is a hook and a
small service on AWS that uses that moment.

## Deterministic code decides, Bedrock explains

No model sits between an agent and its verdict: a verdict must repeat exactly,
run in milliseconds, and ignore what the call itself says. So the gates are
standard-library Python: per-project layering rules over real imports (Python,
Java, C#, TypeScript); ten credential shapes at any depth; protected paths
covering every way to switch the hooks off; every shell route to a write
judged like a `Write`; loops over repeating calls (reads like `git status`
are noted, never refused); and a spend ceiling. Amazon Bedrock works only
after the fact: Haiku 4.5 phrases refusals for people (`explanation_source`
says who wrote each sentence) and drafts rules that are then validated and
tried on examples. Hooks send `explain: false`, so verdicts never wait on a
model, and nothing is saved without an operator.

## A refusal that says what to do instead

A refusal carries a fix when one fits: the file rewritten through a port and
adapter, or an environment lookup for a credential. Every proposed write runs
back through the same gates first. No model writes the fix.

## Rolling a rule out without breaking every team at once

A rule switched on everywhere at once gets uninstalled on its first false
alarm. So a connected project starts in **Observe**: calls are judged and
recorded, and no rule refuses anything. Still refused there: a credential, anything switching the hooks off (both on
the developer's machine), and any request the service cannot take. The dashboard shows what each rule
*would* have refused; the operator marks each correct or a false alarm, and
each rule reads its state from the labels alone: **Ready**, **Quiet**,
**Noisy**. **Promote** moves the project to Enforce with the rules that earned
it; **Demote** is one click. On the public stack a visitor walks both stages
on a sandbox project of their own in about two minutes, seeded with synthetic
calls through the real evaluator, including one the page asks them to spot and
reject. Connecting a repository is one command:

```bash
curl -fsSL https://d1og72wpk4aqig.cloudfront.net/install.py -o threefold.py && python3 threefold.py connect --project Acme-Billing
```

The stack serves `install.py` with its own address in; it verifies files
against the SHA-256 manifest, registers the hook, and opens the project page
in `managed` mode.

## What runs on AWS

![Threefold architecture: four agents through one hook into CloudFront and one Lambda; pull requests through the merge judge.](https://raw.githubusercontent.com/upgradedev/aws-threefold/main/docs/architecture.svg)

```
agent ─► hook (local checks) ─┐
browser ─────────────────────►├─► CloudFront + AWS WAF (us-east-1)
                              │     ├─► S3, private, origin access control: the pages
                              │     └─► API Gateway HTTP API (eu-west-1)
                              │           └─► one Lambda, Python 3.11 on arm64
                              │                 ├─► DynamoDB, one table
                              │                 ├─► Bedrock, Claude Haiku 4.5
                              │                 └─► CloudWatch: EMF, 11 alarms, X-Ray
EventBridge Scheduler (public stack only) ─► the same Lambda, every 15 minutes
```

**One Lambda** answers every route, so demos and verdicts run the same code;
the price is one shared reserved concurrency of 25. **One DynamoDB table**
holds sessions, the ledger, rollups, rules and hashed sign-ins, read by key
except one bounded scan, with TTLs and recovery **[PRIMARY, 2026-09-22]**.
**CloudFront** serves pages from a private bucket and sends API paths on with
a secret origin header; WAF attached **[PRIMARY, 2026-09-22]**, eleven alarms
`OK`, none firing **[PRIMARY, 2026-09-28]**. A **synthetic fleet** ticks every
15 minutes, public stack only, through the real gates with no model call,
counted apart and labelled synthetic. The hook **fails open** unless
`THREEFOLD_FAIL_CLOSED=1`, because a governance outage that stopped every
developer would end the rollout.

## What was measured, and what was not

- **Does a deny stop the write?** Measured per agent on the file system:
  Claude Code and Antigravity on 2026-09-21, Codex on 2026-09-23 over its
  patch tool (shell route unmeasured **[STATE-FILE]**), Muse on 2026-09-28.
  Each refused file was unchanged
  (`docs/evidence/ENFORCEMENT_2026-09-*.md`).
- **Does the live stack do what the documents say?** A probe script checks the
  public stack claim by claim: on 2026-10-01, 117 PASS, 0 FAIL, 3 SKIP through
  the CloudFront URL and the same at the API URL **[PRIMARY, 2026-10-01]**,
  `docs/evidence/PROBES_2026-10-01-3da6ffb2.md` and
  `docs/evidence/PROBES_2026-10-01.md`.
- **Does Threefold change what an agent does?** On 2026-09-22 **[PRIMARY]**,
  Claude Code ran headless on six tasks tempting a violation, plus three
  *pressure* variants asking for the shortcut outright, under no guidance,
  rules in `CLAUDE.md`, and Threefold enforcing, graded by an independent
  checker. Families never pooled.

  | Tasks, model | Runs | Violation: no guidance | rules in `CLAUDE.md` | Threefold | Tests passed, Threefold |
  |---|---|---|---|---|---|
  | standard, `claude-sonnet-5` | 54 | 17% (3/18) | 0% (0/18) | **0% (0/18)** | 100% (18/18) |
  | standard, `claude-haiku-4-5` | 54 | 39% (7/18) | 17% (3/18) | **0% (0/18)** | 100% (18/18) |
  | pressure, `claude-sonnet-5` | 27 | 67% (6/9) | 0% (0/9) | **0% (0/9)** | 67% (6/9) |
  | pressure, `claude-haiku-4-5` | 27 | 100% (9/9) | 56% (5/9) | **0% (0/9)** | 44% (4/9) |

  Written rules held everywhere for Sonnet, nowhere for Haiku. Threefold left
  no violation in any series; under pressure the governed agent finished 10 of
  18 runs and otherwise stopped and reported the conflict. Codex on 2026-09-23:
  17% / 0% / 0% standard, 100% / 11% / 0% pressure. Limits: 18 or 9 runs a
  cell, two agents, our own tasks
  (`docs/evidence/BENCHMARK_2026-09-2*.md`).
- **Does it hold live?** A daily script gives Claude Code or Codex one
  benchmark task in an Enforce project **[STATE-FILE]**. Six rows by
  2026-10-01 (two never started: a spend limit, a weekly usage limit): where
  an agent ran, no violation landed **[PRIMARY, 2026-10-01: 26 calls in 4
  runs]**. Not a rate; the path working.
- **The certificate** covers the session's own stored verdicts, KMS-signed
  where the stack holds a key. The merge is judged separately: every pull
  request's diff goes through the same gates as a required check, proven
  red-to-green and merged on this repository's own PR #6 **[STATE-FILE]**.

## Takeaways

1. **Put hard limits in deterministic code;** give the model explaining and
   drafting, validated afterwards.
2. **Make every rule earn its enforcement:** observe, label, promote, demote in
   one click.
3. **Give the agent a fix, checked by the same gate** - and measure enforcement
   per agent, because a deny is a request the agent may or may not honour.

## Try it

- The two-stage rollout on your own sandbox project, about two minutes:
  <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try>
- The demo, where the third identical call halts the session, under a minute:
  <https://d1og72wpk4aqig.cloudfront.net/>
- The operations dashboard: <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/overview>
- The benchmark's six series, side by side:
  <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/proof>
