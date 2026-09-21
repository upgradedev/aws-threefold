# Threefold — State Ledger

**Last updated:** 2026-09-21
**Hackathon:** AWS Zero to Shipped, submissions close 2026-10-02 23:59 PDT
**Category:** `#workplace-efficiency` · **Lane:** `#community` (hedge to `#commercial-potential` / `#startup` decided 2026-09-28)
**Entries permitted:** one. The Rules tab, ELIGIBILITY section, reads "Limit one entry per person." Threefold is that entry.
**Active agent claim:** Claude Code (this session), 2026-09-21 to 2026-09-29 — the implementation plan below. Tracks and the files each owns:
- A, engine and hooks: `src/threefold/domain/**`, `src/threefold/hooks/**`, `scripts/threefold_*.py`, `scripts/pre-commit-gate.py`, `tests/unit/**`, `tests/security/**`, `tests/hook/**`
- B, service and AWS: `src/threefold/interfaces/**`, `src/threefold/application/**`, `src/threefold/infrastructure/**`, `deploy/**`, `tests/integration/**`; the only track that deploys
- D, pages and story: `src/threefold/web/*.html`, `README.md`, `docs/*.md`
- Owner: `STATE.md`, `LOG.md`, `TRAPS.md`, `CLAUDE.md`

## Contracts, 2026-09-21

Fixed before the tracks split, so no track waits on another for a field name.

- **Hook request v2** (`POST /evaluate-tool-call`): `session_id`; `project_name`, required; a name that does not match the stack's `AllowedProjectPattern` (default `^Acme-[A-Za-z0-9-]{1,40}$`) is stored, counted and used as a metric dimension as `unlabelled`, and the response says so in `warnings`; `developer`, either `anonymous` or a 12-hex hash computed locally, never a user name, and any developer value is shown in public only as a short hash; `tool_name`, `action_type`, `arguments`; `agent`, one of `claude-code`, `codex`, `antigravity`, `pre-commit`, `ci`, `page`; `origin`, one of `hook`, `page`, `ci`; `explain`, false from hooks and true from the pages; `dry_run`, recorded as observed, never refused and never trips a session.
- **What leaves the machine.** A hook sends only tool calls whose target resolves inside the project root. It never sends paths under an agent's own configuration or memory (`~/.claude`, `~/.codex`, `~/.gemini`), data files (by extension and by directory), or any call containing a term from the owner's local never-send list. The list, the project aliases and the list of governed repositories live in `~/.threefold/` on the owner's machine and are never committed here.
- **Verdict output per agent.** Claude Code and Codex: `hookSpecificOutput.permissionDecision = "deny"` with a reason, and nothing at all on approval, so the agent's own permission flow still runs. Antigravity: `{"decision": "deny", "reason": ...}`, and nothing on approval. Every agent: a failure to reach the service prints nothing and exits 0, unless `THREEFOLD_FAIL_CLOSED=1`.
- **Per-repository configuration (day 2).** The hook resolves each setting in this order: environment variable, then `<project root>/.threefold.json`, then `~/.threefold/config.json`. `.threefold.json` holds `project` (an Acme-style alias), `endpoint`, `mode` (`observe` sends every call as `dry_run`, `enforce` does not) and optionally `api_key_file` (a path to a file holding the key, never the key itself). It is written by the install script and listed in `.git/info/exclude`, so it is never committed to the governed repository.
- **Rules per project (day 2).** Stored under `CONFIG#rules#<project>`. `GET /rules?project=X` returns that project's rules, or the shipped set with `is_default: true`. `POST /rules` accepts `{"project": X, "rules": [...]}` with the operator key; without `project` it replaces the shared set as before. The evaluator uses the calling project's rules when they exist and the shared set otherwise. `POST /rules/explain` accepts `project` as well.
- **Loop gate v2 (day 2).** Repeats of read-only or polling calls (`git status|log|diff|show`, `ls`, `cat`, `pwd`, `gh run view|list|watch`, `sleep`, and reads) raise an observation, never a trip. For `origin: hook`, a detected loop refuses the repeating call and records it, but never halts the session: a governed developer is not locked out of their own session. `sim-*` and page sessions keep the terminal halt, so the demo still shows call 3 refused and the session frozen.
- **Every write route (day 2).** A `Bash` call is read for the writes it makes: redirections (`>`, `>>`, `tee`), heredoc bodies, `sed -i`, `perl -pi`, `cp`, `mv`, `install`, `ln`, `rsync`, `dd of=`, `git apply`, `patch`, and `python -c` / `node -e` opening a file for writing. A write whose content can be read is judged like a `Write`. A write to a path an enforce rule covers whose content cannot be read is refused, with the reason "use Write or Edit so the rule can read it"; under an observe rule it is recorded. Writes to agent hook settings (`.claude/settings*.json`, `.codex/hooks.json`, `.agents/hooks.json`) and to `.git/hooks`, `git commit --no-verify` and `git -c core.hooksPath=…` are refused as protected-path calls.
- **Two stacks.** `threefold-prod` stays the public demo. `threefold-dogfood` is created from the same template with `PublicReads=false`, so its ledger and sessions need the operator key to read, and it carries the owner's real use under aliases. Only anonymised totals from it are ever shown in public.

## Ship gate

The gate is pass or fail: live on AWS, reachable by a public URL, with documented
proof of a coding agent connected to the AWS console. The stack stays up past the
submission deadline, but **how far past is unresolved**: this file says judging runs
the weeks of 6 and 13 October while `docs/RUNBOOK.md` says it finishes in the week of
19 October, and neither cites the rules page it came from. Until the Rules tab settles
it, treat the later date as the one that governs teardown.

| Requirement | State | Evidence `[PRIMARY]` |
|---|---|---|
| Live on AWS, public URL | **PASS** | `https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/` serves the dashboard itself, 200 and `text/html`, to an anonymous request with no API key. Stack `threefold-prod`, eu-west-1. The trailing slash is part of the URL: API Gateway answers the bare `/prod` with its own `{"message":"Not Found"}` before the function is reached, so every link to this project must carry it |
| A visitor can run the demo | **PASS** | Walked in a browser: Scenario 1 sent one `POST /simulate-loop`, the function evaluated the same call three times inside that request, the response was `BLOCKED_LOOP_DETECTED`, and the panel showed a genuine Haiku 4.5 sentence under the heading "Amazon Bedrock (Claude Haiku 4.5)" |
| Reachable by the AI scorer | **PASS** | `STAGE` is unset on the function, so the middleware defaults to `dev` and enforces no key. Verified by unauthenticated request. Do not read this off `/status`, which prints `"stage": "prod"`: that field has its own default and says nothing about whether a key is required |
| Proof of coding agent connected to AWS | **PASS** | `docs/PROOF_OF_AWS_AGENT.md` rewritten around the real session: the commands run, the two defects AWS surfaced, and the CloudTrail principal. Raw output in `docs/evidence/DEPLOYMENT_2026-09-20.md` |
| Public repository | **BLOCKED, owner action** | `main` is local only, with no remote configured, so none of the work is published. Publishing is one command, in `docs/RUNBOOK.md` step 2. The count of commits is deliberately not recorded here: `git log` holds it, and any line stating it is wrong again the moment it is committed |
| Continuous delivery | **WRITTEN, role missing** | `.github/workflows/{ci,deploy,keepalive}.yml`. Deploy assumes `threefold-github-deploy`, which does not exist yet. Policy documents are committed at `deploy/iam/`, creation is `docs/RUNBOOK.md` step 1 |
| Builder Center project, two tags | **NOT DONE** | Owner-gated. Requires Builder Center profile, Join, then the Create Project form |

## What is real, measured today

| Claim | Command |
|---|---|
| Bedrock answers from the function's own role | CloudTrail `Converse` events 12:00:35, 12:00:37 and 12:00:39 UTC, principal `assumed-role/threefold-prod-ThreefoldFunctionRole-JzGN3b6RjC7w`, model `eu.anthropic.claude-haiku-4-5-20251001-v1:0`, no error |
| The third identical call halts the session | Three POSTs to `/evaluate-tool-call`: APPROVED, APPROVED, `BLOCKED_LOOP_DETECTED` with `session_tripped: true` |
| The halt is durable | `aws dynamodb get-item` on the session returns `is_tripped: true`, the loop reason, and a `ttl` 30 days out |
| A halted session refuses unrelated work | A fourth call with a different tool returned `BLOCKED_CIRCUIT_BREAKER` |
| Every explanation names its source | Responses carry `explanation_source: "bedrock"` and `persistence: "dynamodb"`, and the UI prints the source as the heading rather than assuming Bedrock |
| Numeric arguments survive the round trip | Three identical calls with `{"retries": 3, "timeout": 1.5, "flag": true}` still halted on the third, so reloading history does not change a call's signature |
| Readiness can be alarmed on | `/readyz` answers 503 when a dependency is unreachable and 200 when both probes pass; live now it answers 200 with `GetItem succeeded against threefold-prod-ThreefoldTable-12AHEKBKP5RCP`. The two probes are not equally strong and the response says which is which: the store is exercised with a real read, while the Bedrock probe reports the client's own record and returns healthy on a container that has not called the model yet, because invoking one on every readiness check would bill the account for being looked at |
| The operator console is served | `/settings.html`, `/sessions.html` and `/connect.html` each answer 200 `text/html` to an anonymous request, with the API base substituted. Walked in a browser against the live URL: the sessions page listed 43 sessions, the policy page read the live policy, and the connect page's denial carried a Bedrock sentence. Those 43 are real rows from the table rather than fixtures in the page, but most were created by this project's own testing, not by governed outside work |
| The sessions listing reads the table, not one container | A Lambda cold-started by the deploy returned 43 rows to `/api/sessions` with an empty in-process store, so the rows came from the DynamoDB scan. `dynamodb:Scan` was missing from the function role before this deploy and the fallback would have hidden it |
| A session id that must be escaped reads back | `GET /sessions/<urlencoded 'live console fixture <angle>'>` returns that session with its real project and call count, rather than creating an empty one |
| The published contract is the real one | `/openapi.json` on the live stack returns all eleven paths including `/api/sessions`, and `/prod/swagger.html` renders twelve operations from it with no console error. Both used to fail: the document sat outside `CodeUri` so a two-line placeholder was served, and the page asked for a URL missing the stage prefix |
| The spec names what is deployed | Its description said Claude 3.5 Sonnet while the stack runs `eu.anthropic.claude-haiku-4-5-20251001-v1:0`, and its only server was `127.0.0.1:8001`, so Try it out went to the reader's own laptop. Both corrected, and a test now reads the model family out of `deploy/template.yml` and fails if the document drifts from it |
| Every page reaches the operation it calls | The four pages deep link into the document rather than at its cover: the dashboard lists the seven operations its buttons call, settings links `/policy/config`, the sessions console links `/api/sessions`, `/sessions/{id}` and the terminate route, and connect links `/evaluate-tool-call`. Walked on the live URL: following one opens Swagger UI with that operation expanded |
| The links cannot drift from the routes | Swagger UI derives its anchors from method and path, so a renamed route would break every link pointing at it silently. A test rebuilds those anchors from the served document, extracts the endpoints the dashboard actually fetches, and fails when one is unlinked. Confirmed by deleting a single entry and watching it fail |
| A visitor can take the hook | The deployment serves it at `/hooks/claude_code_hook.py`, anonymously, as readable text. Downloaded from the live URL and run: an ordinary read came back `allow`, a `from boto3 import client` write into a domain file came back `deny` with a Bedrock sentence. Before this, `connect.html` told the reader to `git clone <repository>` — a literal placeholder, since the repository is not published — and the install path ended there |
| A certificate cannot attest to nothing | `POST /issue-certificate` with `{"evaluations": []}` used to answer 200 with `verdict_status: COMPLIANT_APPROVED` and `all_passed: true`, because `all()` over an empty list is true. It now answers 400 `Nothing To Certify`, as does a session this service has no record of governing. Verified against the live stack, and the dashboard's Scenario 4 still issues its certificate |
| The policy cannot be rewritten by a stranger | `POST /policy/config` and its `/policy` alias now require an operator key even where reads need none, because the write lands in DynamoDB and every later container adopts it. With no key configured, which is how the stack deploys, the write is refused outright with 403 `Policy Is Read Only Here`; with one configured, a missing key is 401 and a wrong key is 403. Verified against the live stack, and `GET /policy/config` still answers 200 to an anonymous request |
| The layering rules can be read, tried and saved from a page | `/rules.html` answers 200 anonymously on the live stack, lists the four shipped rules with ENFORCE badges, and trying `import javax.persistence.Entity` in a Java domain class returns REFUSE naming `java-domain-stays-pure`. Its three deep links open `GET /rules`, `POST /rules/explain` and `POST /rules` in Swagger UI. An anonymous save answers 403 `Policy Is Read Only Here`, because the stack deploys with no operator key. Verified 2026-09-21 |
| A rule can watch before it bites | A rule with `mode: observe` lets the call run and records which rule would have refused it and on which file. The console counts these apart from refusals, in a Would refuse column and its own section. Measured end to end on a local server running the deployed code: two observed calls and one refusal gave `refused: 1, observed: 2`, and a three-file MultiEdit counted once and named the file the rule watched. The live ledger has no observations yet, because no observing rule has been saved there |
| The open explain route cannot be made slow | Live: a draft with seven stacked `**` answered in 0.25 s (57 s before), a 56 KB path is refused with 400 in 0.47 s (19 s before), an 800 KB Python literal and 5,000-deep nesting are both answered and refused rather than exhausting memory or returning 500. Paths are matched segment by segment with no regular expression |
| An open read writes nothing | Live: `GET /sessions/<unknown id>` answers 404 `session-not-found`, and the id does not appear in `/api/sessions` afterwards. Drilling into a real session from the listing still answers 200 |
| A refusal actually stops the write | Measured on 2026-09-21 with a local hook that answers `deny` and makes no network call, then checking the file system. Claude Code 2.1.220 refused both a `Write` and a `Bash` redirection, and neither file was created; the Antigravity desktop app refused `write_to_file` and no file was created. Codex CLI 0.155.0 could not be measured, because the account had reached its usage limit until 2026-09-27, so no claim is made for it and its edits count as governed at commit time only. Two open reports say a deny can be ignored (claude-code#91574, openai/codex#27833), which is why this is measured per agent rather than assumed. Method, results and the observed argument shapes: `docs/evidence/ENFORCEMENT_2026-09-21.md` |
| One hook governs three agents, and keeps at home what it must | `src/threefold/hooks/threefold_hook.py`, served at `/hooks/threefold_hook.py` and at the old URLs. Offline end to end against the service on 2026-09-21: a forbidden import refused for Claude Code, Codex (`apply_patch`) and Antigravity (`write_to_file`); a clean write prints nothing; a write outside the project is not sent; a credential is refused locally and never reaches the service. Live on 2026-09-21, a hook's refusal and an approval both answered without a model call and at no cost, while a page's refusal still carried a Bedrock sentence |
| Public data carries no real names | Live on 2026-09-21: `/api/insights` and `/api/sessions` show only `Acme-*` projects or `unlabelled`, and developers only as 8-hex hashes. Project names outside `AllowedProjectPattern` are stored as `unlabelled` with a warning in the response |
| Test suite | 659 passed in 13.2s. It is hermetic now: `THREEFOLD_OFFLINE` used to be set in a session fixture, which runs after the handler module has built its AWS clients at collection time, so the suite had been trying real endpoints (56 s per run, one readiness test failing on HEAD) until 2026-09-21. It never reached live data, because the default table name it would have used does not exist. Each test still starts with a full rate-limit bucket, and the rate limiter's own tests build their own instance |

## Known gaps, not yet fixed

These are recorded because they are still false or missing in the tree. None is
hidden in a document that a judge would read as finished work.

1. Three of the five keys `/policy/config` returns are enforced by nothing.
   `max_session_budget_usd` and `loop_history_window` survive a cold start and
   come back from that endpoint, but no gate reads them: the session ceiling is
   the `budget_usd` each call declares, and the detector's cycle length is
   compiled in at six. `blocked_patterns` is the third: it is defined on the DTO
   and read by no module in `src/`, because the credential shapes the secret gate
   matches live in the domain rules instead. The POST handler builds the policy
   from four fields, so a caller that sends a pattern list has it discarded and
   the defaults echoed back as though they had been accepted. `/settings.html` carries all three on
   its face: the two numeric ones are badged "stored, not enforced", and the
   pattern list is shown read only with the reason. The remaining two keys are
   wired, and the breaker is now built from the policy rather than from its own
   $2.50 default, so the cap `/policy/config` reports is the cap a call is
   measured against, including on a cold container that was never sent a policy.
2. The `calls` count in the sessions listing saturates at 50, because the store
   keeps `history[-50:]`. Cost and tokens are cumulative and are not capped. The
   page says so rather than presenting 50 as a total.
3. Nothing writes to the S3 bucket the stack provisions. `S3CertificateUploader`
   exists and is called by nothing, `EvidenceStore.create_sealed_bundle` only by
   a test, so the bucket stays empty and the function carries an `s3:PutObject`
   grant it never uses. Every document that claimed archival has been corrected;
   the dead class and the unused grant are still there.
4. The certificate still covers verdicts the caller supplies. An empty list and
   a session this service never governed are now both refused with 400, and the
   invariant sits in `AuditIssuer` so no caller can go around it, but the
   contents of the evaluations are still taken on the caller's word: a session
   with one real call can be certified with four invented ones. Issuing from the
   session's own stored history is the fix, and it is not done.
5. Four of the five offline fallback panels still show canned prose. They now say
   "Simulated, offline demo, no model was reached" on their face, but the numbers
   inside them are invented and should be replaced with a real offline run.
6. The loop detector compares signatures byte for byte. A signature is a SHA-256
   of the tool name and its sorted arguments, so a call that differs by one
   character is a different call and two semantically identical calls are not
   matched. Any repeating cycle of those signatures is caught, up to period six,
   which is what the audit table below means by an A, A, B cycle; what is missing
   is fuzzy or semantic matching. The README no longer claims entropy scanning,
   because there is no entropy code.
7. No headline number exists yet. This is the largest remaining gap for judging:
   the framing gate wants one comparative number against two named baselines.
8. No video and no Builder Center article. Neither is required by the rules, but
   the Builder Center project itself is, and it is owner-gated.

## Audit and what was done about it, 2026-09-20

An independent scoring pass and a competitive analysis both probed the live API.
Between them they found eight inputs that walked through the gate. All eight are
now refused, verified against the deployed stack rather than in tests alone.

| Input that used to be approved | Now | Cause that was fixed |
|---|---|---|
| Domain write with `action_type` omitted | refused | the content check no longer requires the caller to admit it is a write |
| `from boto3 import client` | refused | the rule parses imports with `ast` instead of matching substrings |
| `ASIA` and `sk-proj-` keys in a command | refused | ten credential shapes, up from five |
| `.env` under an argument named `filename` | refused | paths are found by shape, not by a four-name allowlist |
| `notebook_path` plus `new_source` | refused | same |
| A secret on its own line | refused | each string is scanned on its own, not `str(arguments)` |
| `curl -d @.env` | refused | protected-path patterns use lookaheads |
| An A, A, B cycle, fifteen calls deep | refused | any repeating period up to six, not three hardcoded shapes |

Ordinary work still passes: an edit outside the domain, and seven varied calls
in a row, are approved on the live API.

Unlike the tables above, the eight rows cite no command a reader could repeat:
they were one-off probes against the deployed stack on 2026-09-20 and the
transcripts were not kept. What is repeatable is the regression suite left
behind, `tests/security/test_the_perimeter_holds.py` and
`tests/integration/test_the_hook_governs_a_real_agent.py`, which carry all eight
inputs, so the table's claim survives as tests even though its evidence does
not survive as output.

**The product now intercepts rather than rehearses.**
[`claude_code_hook.py`](src/threefold/hooks/claude_code_hook.py) puts Threefold in front
of a real Claude Code session. Confirmed end to end against the live stack: a
write of `from boto3 import client` into a domain file comes back denied with a
Bedrock sentence attached, an ordinary edit is allowed, and `cat ~/.aws/credentials`
is refused. It fails open by default and says so in the reason it returns.

**Positioning changed.** The README leads with the one rule that has no
incumbent, enforced at the moment of the edit rather than in CI. The other three
gates are described honestly as less novel than the tools that specialise in
them. This matters because Policy in Amazon Bedrock AgentCore has intercepted
tool calls before execution since March, and the judging panel is five AWS
employees. The distinction that holds is local tool calls against gateway
traffic.

### Still open

1. There is no Builder Center project and no public repository. Until both
   exist there is no submission, whatever the code does. The repository is one
   command, in `docs/RUNBOOK.md` step 2. The Builder Center project is not: it is
   a profile, a Join, and a web form, and no command for it exists anywhere in
   this repository.
2. No measured number yet. The hook is installed-ready but has not been run
   across a working week, which is where the number comes from.
3. The certificate is a fingerprint, not a signature. There is no KMS call and
   no key, so it detects corruption rather than an adversary, and nothing in CI
   verifies one before a merge. Every surface now says so, including the panel
   where a visitor meets the document: the dashboard, the README, the submission
   dossier, the Builder Center article, the video script and the docstrings in
   `dtos.py`, `audit_issuer.py` and `s3_store.py` were each corrected after an
   audit found them claiming a signature, S3 archival or CI verification. What
   remains open is the thing itself: making a CI check refuse a pull request
   whose session has no valid certificate, which is the work that would make the
   certificate load-bearing rather than decorative.
4. `TokenCostCalculator` is never given a model id, so every session is priced
   at the default Sonnet-class rate rather than the model actually in use.
5. The API still enforces no key on reads, deliberately: `STAGE` is unset so an
   anonymous judge and the AI scorer reach everything, which is the ship gate's
   scorer row. The two writes that outlive their caller, the policy and the
   layering rules, are closed. Everything else a visitor can POST (evaluating a
   call, the scenarios, the certificate, the kill switch) is still open and is
   bounded by the session it names. If `STAGE=prod` or `ENFORCE_API_KEY` is ever
   set, the reads the pages make stay open by method and those POSTs need the
   key. A test pins both halves by setting the environment, which is what the
   middleware reads.
6. The cost gate trusts caller-declared token counts. A caller declaring zero is
   not stopped. A proxy that meters real usage is the stronger control and this
   is not one.
7. A saved policy reaches only the container that took it. The layering rules
   are read again by every container after 30 seconds, so the rules page can say
   when a save applies everywhere. The policy is still adopted only at cold
   start, so after a `POST /policy/config` other warm containers keep the old
   thresholds until they are recycled. The fix is the rules fix applied to
   `_adopt_saved_policy`.

## Cost and teardown

PAY_PER_REQUEST DynamoDB, one 256 MB arm64 Lambda, an HTTP API and an empty S3
bucket. Bedrock calls are capped per container. Teardown is
`aws cloudformation delete-stack --stack-name threefold-prod --region eu-west-1`,
which must not run before judging completes.
