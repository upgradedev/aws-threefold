# Owner handoff: the three things only you can do

Everything else is done: the code is deployed, probed 117/0/3 on the edge
and the origin, and CI is green. These three need your logins, so they are
yours. Each package below is copy-paste ready.

**Deadline: 2026-10-02 23:59 PDT.** Keep the stacks up through winners week;
teardown is in `docs/RUNBOOK.md` and must not run before judging completes.

**Links used below (all checked 200 on 2026-09-30):**

- Hackathon page: <https://builder.aws.com/build/hackathons/e83e84e5-4f4c-383b-bbe9-4a15ac195d55/zero-to-shipped>
  (fallback: <https://builder.aws.com/build/hackathons>, search "Zero to Shipped")
- Live app: <https://d1og72wpk4aqig.cloudfront.net/>
- Walkthrough: <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try>
- Proof page: <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/proof>
- Repository: <https://github.com/upgradedev/aws-threefold>

---

## Package A: the Builder Center project (the submission itself)

Without this there is no submission, whatever the code does. Steps:

1. Sign in to Builder Center (a Builder ID profile; 18+).
2. Open the hackathon page above and click **+ Join**. Joining unlocks
   project creation and Discussion posting.
3. Create the project. Paste the fields below.

**Project name:**

```text
Threefold
```

**Tagline:**

```text
Threefold refuses a coding agent's edit the moment it is made, not after the commit, so your architecture does not rot while you sleep.
```

**Category / Lane:**

```text
#workplace-efficiency / #community
```

**Description (short):**

```text
Threefold governs the tool calls coding agents make. One standard-library hook file sits in front of Claude Code, Codex, Antigravity and Muse and asks a service on AWS about each write or command before it runs. Deterministic gates decide: layering rules, credentials, writes that switch the hooks off, repeating calls, a spend ceiling. Projects start in Observe, where nothing is refused and the dashboard shows what would be; the operator labels each, then promotes to Enforce with the rules that earned it. Amazon Bedrock never decides: it explains refusals to people and drafts rules for architects. Pull requests are judged the same way with nothing installed, through a required check.

Live, no account: https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try
Repository: https://github.com/upgradedev/aws-threefold
```

**Links to attach to the project:**

```text
Live application: https://d1og72wpk4aqig.cloudfront.net/
Two-minute walkthrough: https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try
Measured proof (6 series, 243 runs): https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/proof
Repository: https://github.com/upgradedev/aws-threefold
Demo video: (paste after upload, Package C)
Article: (paste after posting, Package B)
```

**Proof of the agent-to-AWS connection** (if the form asks how the agent
connects to AWS): `docs/PROOF_OF_AWS_AGENT.md` in the repository, with raw
output in `docs/evidence/DEPLOYMENT_2026-09-20.md`. Quote its first
paragraph and link the file on GitHub.

---

## Package B: the article

The article is written and fresh: `docs/BUILDER_CENTER_ARTICLE.md`
(probes rechecked 2026-09-30, five live-agent rows, six benchmark series).

**Title (the file's first line):**

```text
Refuse the edit, not the pull request: governing coding agents on AWS with deterministic gates and Amazon Bedrock
```

**Tags:**

```text
zero-to-shipped, workplace-efficiency, community, bedrock, lambda, governance
```

**Body:** paste the whole file except its first five lines (the title and
the `For: / Hackathon: / Try it:` meta block). Start at the `A claim about
the live stacks...` paragraph. Keep the `[PRIMARY, date]` tags; they are
the evidence trail.

After posting, copy the article URL back into the project's links.

---

## Package C: the video

The script is written and timed: `docs/VIDEO_SCRIPT.md` (2:45, hard cap
3:00). The recording checklist at its end is the shoot list.

**Already verified for you on 2026-09-30 (no need to recheck):**

- The live sandbox seeds the false alarm the script describes
  (`python-domain-stays-pure`).
- The live `#/proof` page serves the six measured series (4 Claude Code,
  2 Codex), snapshot 2026-09-28. Glance at it before filming scene 5; if a
  PILOT banner ever appears instead, stop and say so before narrating
  measured results over it.
- The walkthrough, overview, proof, connect and installer URLs all answer
  200 anonymously.

**Recording notes beyond the checklist:**

- Film scene 3 in a fresh private window so the walkthrough starts at
  step 1. The sandbox name from scene 3 is the `--project` of scene 4;
  record scene 4 within 24 hours, before the sandbox expires.
- Scene 5 needs your AWS sign-in for the `threefold-prod-operations`
  CloudWatch dashboard in eu-west-1. Keep account id, email and any other
  stack out of frame.
- Never show the dogfood stack, its address, or the operator key. Never
  show real project or company names; the video uses `Acme-*` only.

**Upload:** unlisted or public YouTube (or the host Builder Center
accepts), then paste the URL into the project links and under the
article's Try-it section if you edit it.

**Video description (paste under the upload):**

```text
Threefold refuses a coding agent's edit the moment it is made, not after the commit. Deterministic gates on AWS (Lambda, DynamoDB, CloudFront) judge every write or command before it runs; Amazon Bedrock explains, never decides.

Try it live, no account: https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try
Measured proof: https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/proof
Repository: https://github.com/upgradedev/aws-threefold
Category #workplace-efficiency, lane #community, AWS Zero to Shipped 2026.
```

---

## After all three

- [ ] Builder Center project exists and links the live app, repo, video,
      and article.
- [ ] Article posted with the six series and the 2026-09-30 probe figures.
- [ ] Video uploaded, 2:35 to 2:45, linked from the project.
- [ ] Stacks still up (`threefold-prod`, `threefold-dogfood`,
      `threefold-prod-edge`).
- [ ] Tell the agent: it will record the submission in `STATE.md` and
      `LOG.md`.
