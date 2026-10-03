# Owner handoff: published project and optional materials

The owner published the [Builder Center project](https://builder.aws.com/project/3K8lHWCcYrIspFkKAIEWxuiOBcS/threefold-architecture-guardrails-for-coding-agents)
on 2026-10-03. It links the live app, repository, walkthrough, measured proof
and deployment evidence. The article and video below are prepared as optional
supporting material; no publication links for them appear in the project.

**Deadline: 2026-10-02 23:59 PDT.** Keep the stacks up through winners week;
teardown is in `docs/RUNBOOK.md` and must not run before judging completes.

**Links used below (all checked 200 on 2026-10-01):**

- Hackathon page: <https://builder.aws.com/build/hackathons/e83e84e5-4f4c-383b-bbe9-4a15ac195d55/zero-to-shipped>
  (fallback: <https://builder.aws.com/build/hackathons>, search "Zero to Shipped")
- Live app: <https://d1og72wpk4aqig.cloudfront.net/>
- Walkthrough: <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try>
- Proof page: <https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/proof>
- Repository: <https://github.com/upgradedev/aws-threefold>
- Published project: <https://builder.aws.com/project/3K8lHWCcYrIspFkKAIEWxuiOBcS/threefold-architecture-guardrails-for-coding-agents>

---

## Package A: the Builder Center project (published)

The owner published the [project](https://builder.aws.com/project/3K8lHWCcYrIspFkKAIEWxuiOBcS/threefold-architecture-guardrails-for-coding-agents)
with the live application, two-minute walkthrough, measured proof and repository.
The article and video below can be added if published.

**Proof of the agent-to-AWS connection** (if the form asks how the agent
connects to AWS): `docs/PROOF_OF_AWS_AGENT.md` in the repository, with raw
output in `docs/evidence/DEPLOYMENT_2026-09-20.md`. Quote its first
paragraph and link the file on GitHub.

---

## Package B: the article

The article is written and fresh: `docs/BUILDER_CENTER_ARTICLE.md`
(rewritten to ~1200 words, probes rechecked 2026-10-01, seven live-agent
rows with five runs, six benchmark series).

**Title (the file's first line):**

```text
Refuse the edit, not the pull request: governing coding agents on AWS with deterministic gates and Amazon Bedrock
```

**Image:** the article carries the architecture diagram by URL
(`docs/architecture.svg` on GitHub, verified 200 on 2026-10-01). If Builder
Center does not render it, upload that file from the repo in its place.

**Tags:**

```text
zero-to-shipped, workplace-efficiency, community, bedrock, lambda, governance
```

**Body:** paste the whole file except its first five lines (the title and
the `For: / Hackathon: / Try it:` meta block). Start at `## The moment that
matters`. The note on the `[PRIMARY, date]` tags is at the end; keep it and
the tags, they are the evidence trail. The section `How it was built and
shipped` is the deployment journey the project post asks for.

After posting, copy the article URL back into the project's links.

---

## Package C: the video

The script is written and timed: `docs/VIDEO_SCRIPT.md` (2:45, hard cap
3:00). The recording checklist at its end is the shoot list.

**Already verified for you on 2026-10-01 (no need to recheck):**

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
With its hook installed, Threefold checks supported coding-agent writes and commands before they run. Deterministic gates on AWS judge architecture rules; Amazon Bedrock explains, never decides. Projects start with architecture rules in Observe while credential and hook-protection checks remain active locally.

Try it live, no account: https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/try
Measured proof: https://d1og72wpk4aqig.cloudfront.net/dashboard.html#/proof
Repository: https://github.com/upgradedev/aws-threefold
Category #workplace-efficiency, lane #community, AWS Zero to Shipped 2026.
```

---

## Current status

- [x] Builder Center project published with the live app, repository and
      measured proof. Cover uploaded.
- [ ] Optional article publication has not been verified.
- [ ] Optional video publication has not been verified.
- [ ] Stacks still up (`threefold-prod`, `threefold-dogfood`,
      `threefold-prod-edge`).
- [x] Submission recorded in `STATE.md` and `LOG.md`.
