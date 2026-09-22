# Refuse the edit, not the pull request: governing coding agents on AWS with deterministic gates and Amazon Bedrock

**For:** AWS Builder Center
**Hackathon:** AWS Zero to Shipped 2026 · **Category:** `#workplace-efficiency` · **Lane:** `#community`
**Try it, no account:** <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try>

---

## The moment that matters

A coding agent asked to archive orders to S3 can take the shortest path:
`import boto3` in the domain entity, because that makes the test pass. Every
architecture check most teams run (import linters, architecture tests) would
flag it, in CI, after the agent has moved on to the next file and built on top
of the mistake.

There is exactly one moment when that edit can still be refused cheaply: when
the agent asks to make it. Claude Code, Codex and Antigravity all let a hook
see a tool call before it runs and deny it. Threefold is a hook and a small
service on AWS that uses that moment.

## Deterministic code decides, Bedrock explains

The first design rule was that no model sits between an agent and its verdict.
A verdict has to be the same for the same call, fast enough to sit in front of
every edit, free to repeat, and immune to what the call itself says. A model
is none of those, and it would be reading text an agent wrote, which is where
a prompt injection would live.

So the gates are standard-library Python:

- **Layering rules**, per project, that an architect writes as three things:
  which paths a rule covers, what they may not depend on, and what is allowed
  anyway. Imports are read from each file's own statements in Python, Java, C#
  and TypeScript, so `from boto3 import client` is caught as surely as
  `import boto3`, and a commented-out import is not.
- **Credentials**, ten shapes at any depth of the arguments.
- **Protected paths**: the agents' own hook settings, `.git/hooks`,
  `git commit --no-verify`, and every other way to switch the hooks off.
- **Every shell route to a write.** Refuse an agent's `Write` and it may reach
  for `cat > file <<'EOF'`. The service reads a command for the writes it makes
  (redirections, heredocs, `sed -i`, `cp`, `git apply` and more) and judges
  readable content exactly like a `Write`.
- **Loops**: any repeating cycle of byte-identical calls, up to period six. A
  repeated `git status` or `gh run view` is noted and never refused, and a
  hook's loop refuses the repeating call without halting the developer's
  session.
- **Spend**: a ceiling on the tokens a caller declares, which bounds honest
  overruns rather than an adversary.

Amazon Bedrock has two jobs, both after the fact. When a person is reading a
page, Claude Haiku 4.5, through the `eu.` cross-region inference profile and
the Converse API, phrases a refusal in a sentence; the prompt tells it the gate
has already decided and never to contradict it, and every response carries
`explanation_source` so a reader knows whether a model or the code wrote the
sentence. A hook always sends `explain: false`, so a real agent's verdict never
waits on a model. And on the rules page, Bedrock drafts a layering rule from an
architect's sentence, which is then treated as untrusted: parsed, validated like
a save, set to observe, and tried on example files. Nothing is saved without an
operator.

## A refusal that says what to do instead

A deny that only says no sends the agent back to guess, and its next guess is
can be the same call spelled differently. So a refusal carries a fix when one
fits: the file rewritten through a port and an adapter, or an environment
lookup in place of a literal credential. A fix that would itself be refused is
worse than none, so every write it proposes is run back through the same gates
with the same rules before it is offered, and the response says whether all of
them passed. The hook appends the fix's one-line summary to the deny reason.
No model writes the fix.

## Rolling a rule out without breaking every team at once

A rule switched on everywhere at once is a rule that gets uninstalled on its
first false alarm. So every connected project starts in **Observe**: calls are
judged and recorded, and nothing is refused except a credential, which the hook
refuses on the developer's own machine. The dashboard shows what each rule
*would* have refused. An operator marks each of those correct or a false alarm,
and each rule reads its state from the labels: **Ready** when everything it
flagged was correct, **Quiet** when it flagged nothing, **Noisy** after a false
alarm. **Promote** moves the project to Enforce with the rules that earned it,
and the others keep observing. **Demote** is one click.

Connecting a repository is one command, copied from the dashboard:

```bash
curl -fsSL https://d1og72wpk4aqig.cloudfront.net/install.py -o threefold.py && python3 threefold.py connect --project Acme-Billing
```

The stack serves `install.py` with its own address written in. It downloads the
hook and the pre-commit check, keeps a file only when its SHA-256 matches the
stack's manifest, registers the hook for the agents it finds, lists everything
it wrote in `.git/info/exclude`, sends one harmless call, and opens the project
page. An operator of a private stack signs in with `threefold.py open`, which
trades the key in a local file for a single-use link, so no key is ever pasted
into a browser.

## What runs on AWS

```
agent ─► hook (local checks) ─┐
browser ─────────────────────►├─► CloudFront + AWS WAF (us-east-1)
                              │     ├─► S3, private, origin access control: the pages
                              │     └─► API Gateway HTTP API (eu-west-1)
                              │           └─► one Lambda, Python 3.11 on arm64
                              │                 ├─► DynamoDB, one table
                              │                 ├─► Bedrock, Claude Haiku 4.5
                              │                 └─► CloudWatch: EMF, 10 alarms, X-Ray
```

- **One Lambda function** answers every route, so the page's "try it" and a
  hook's verdict run the same code. The price: page reads and verdicts share one
  reserved concurrency of 25, kept apart by the API's stage throttle and the
  edge's per-address limit.
- **One DynamoDB table** holds sessions, the decision ledger by day, daily
  rollups written with `ADD` so charts stay exact however busy the ledger is,
  rules, project stages and sign-in records (stored only as hashes). Everything
  is read by key, with TTLs and point-in-time recovery.
- **CloudFront** serves the pages from a private bucket and sends the API paths
  to the function with a secret origin header, so the function believes the
  viewer's address and host only from the edge. That fixed two real problems:
  every viewer of one edge server shared one rate-limit bucket, and an
  installer fetched from the edge pointed its hook past the firewall. The
  secret is not authentication; the API's own URL stays public.
- **The hook fails open.** If the service cannot answer, the agent's own
  permissions decide, because a governance outage that stopped every developer
  would end the rollout. `THREEFOLD_FAIL_CLOSED=1` flips that.

## What was measured, and what was not

- **Does a deny stop the write?** Measured per agent on the file system on
  2026-09-21: in Claude Code 2.1.220 and the Antigravity desktop app the
  refused file was not created. Codex was not measured, so nothing is claimed
  for it.
- **Does the live stack do what the documents say?** A probe script checked the
  public stack claim by claim on 2026-09-22: 113 PASS, 0 FAIL, 3 SKIP.
- **Does Threefold change what an agent does?** Not measured yet. A benchmark
  harness runs an agent on six synthetic tasks, each tempting a governed
  violation, with no guidance, with the rules in `CLAUDE.md`, and with
  Threefold enforcing, and grades the result with an independent checker. Only
  a pilot has run, and its agent never reached the model, so there is no number
  to report. The headline will be computed by the harness's report script from
  the full matrix.
- **The certificate** Threefold issues is an unkeyed SHA-256 fingerprint over
  verdicts the caller supplies. It detects corruption, not an adversary, and
  nothing requires one before a merge.

## Takeaways

1. **Put hard limits in deterministic code** and give the model the jobs it is
   good at: explaining to a person, and proposing drafts a deterministic check
   then validates.
2. **Make every rule earn its enforcement.** Observe, label, promote, and demote
   in one click.
3. **Give the agent a fix, and check the fix with the same gate.**
4. **Measure the enforcement per agent.** A hook's deny is a request, and
   whether an agent honours it is an empirical question.

## Try it

- The two-stage rollout on a sandbox project of your own, in about a minute:
  <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try>
- The demo, where the third identical call halts the session:
  <https://d1og72wpk4aqig.cloudfront.net/>
- The operations dashboard: <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/overview>
