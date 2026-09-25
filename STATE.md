# Threefold — State Ledger

**Last updated:** 2026-09-25
**Hackathon:** AWS Zero to Shipped, submissions close 2026-10-02 23:59 PDT
**Category:** `#workplace-efficiency` · **Lane:** `#community` (hedge to `#commercial-potential` / `#startup` decided 2026-09-28)
**Entries permitted:** one. The Rules tab, ELIGIBILITY section, reads "Limit one entry per person." Threefold is that entry.
**Active agent claim:** Claude Code (this session), 2026-09-22 to 2026-09-29 — the application: dashboard, two-stage rollout, one-command connect. Tracks and the files each owns (a file outside a track's list is changed only by the owner at merge):
- A, hook and connect: `src/threefold/hooks/**`, `src/threefold/tools/**` (new: the installer and the CLI move here so the stack can serve them), `scripts/threefold_*.py`, `scripts/pre-commit-gate.py`, `tests/hook/**`, `tests/unit/test_threefold_*.py`
- B1, ledger and stages: `src/threefold/application/**`, `src/threefold/domain/**`, `src/threefold/infrastructure/dynamo_repo.py`, `src/threefold/interfaces/app_routes.py` (new), `tests/unit/**` except A's, `tests/integration/test_app_*.py` (new), `tests/conftest.py`
- B2, access and distribution: `src/threefold/infrastructure/security_middleware.py`, `src/threefold/infrastructure/auth_store.py` (new), `src/threefold/interfaces/access_routes.py` (new), `deploy/**`, `tests/security/**`, `tests/integration/test_access_*.py` (new); the only track besides the owner that touches `deploy/`
- D, the application pages: `src/threefold/web/**` (including new `dashboard.html` and `assets/`), `tests/pages/**`, `README.md`, `docs/*.md`
- E1, edge: `deploy/edge.yml` (new: CloudFront, S3 with origin access control, WAF, in us-east-1), `scripts/publish_web.py` (new), `tests/unit/test_edge_*.py` (new)
- E2, operations hardening: `deploy/template.yml` sections other than B2's parameter, environment and table grants (alarms, dashboard, tracing, throttling, point-in-time recovery, access logs), `tests/integration/test_the_template_*.py`
- E3, benchmark: `benchmark/**` (new), `docs/evidence/BENCHMARK_*.md` (new)
- E4, live probes: `scripts/probe_live.py` (new), `tests/unit/test_probe_live.py` (new)
- E5, rule drafting with Bedrock: `src/threefold/application/rule_drafter.py` (new), `src/threefold/interfaces/draft_routes.py` (new), `tests/unit/test_rule_drafter.py` (new)
- E6, validated fix per refusal: `src/threefold/application/fix_proposer.py` (new), `tests/unit/test_fix_proposer.py` (new); wired into verdicts by the owner after B1 merges
- Wave three (2026-09-22, after A, B1, B2, D and E1–E6 merged), each in its own worktree:
  - W1, the fix reaches the agent: `application/dtos.py`, `application/evaluator.py`, `application/labels.py`, `hooks/threefold_hook.py`, the call detail and the refusal panels in `web/dashboard.html`, `web/index.html`, `web/connect.html`, tests for these
  - W2, drafting on the rules page: `infrastructure/security_middleware.py` (the draft route only), `interfaces/draft_routes.py`, `web/rules.html`, tests for these
  - W3, the edge knows who is calling: `deploy/edge.yml`, `deploy/template.yml` (the edge secret parameter only), `deploy/iam/**`, `.github/workflows/**`, `interfaces/access_routes.py`, `interfaces/server.py`, `infrastructure/security_middleware.py` (the client address only), tests for these
  - W4, self-correction and the proof page: `application/insights.py`, `application/ledger.py`, `interfaces/app_routes.py`, `scripts/build_proof.py` (new), `web/assets/proof.json` (generated), the overview and a new `#/proof` route in `web/dashboard.html`, tests for these
  - W5, the benchmark runs for real: `benchmark/**`, `tests/unit/test_benchmark_*.py`
- Shared, one dispatch line each: `src/threefold/interfaces/api_handlers.py`, `src/threefold/web/openapi.json`, `docs/openapi.yaml`
- Owner: `STATE.md`, `LOG.md`, `TRAPS.md`, `CLAUDE.md`; merges and deploys

## Contracts, 2026-09-21

Fixed before the tracks split, so no track waits on another for a field name.

- **Hook request v2** (`POST /evaluate-tool-call`): `session_id`; `project_name`, required; a name that does not match the stack's `AllowedProjectPattern` (default `^Acme-[A-Za-z0-9-]{1,40}$`) is stored, counted and used as a metric dimension as `unlabelled`, and the response says so in `warnings`; `developer`, either `anonymous` or a 12-hex hash computed locally, never a user name, and any developer value is shown in public only as a short hash; `tool_name`, `action_type`, `arguments`; `agent`, one of `claude-code`, `codex`, `antigravity`, `pre-commit`, `ci`, `page`; `origin`, one of `hook`, `page`, `ci`; `explain`, false from hooks and true from the pages; `dry_run`, recorded as observed, never refused and never trips a session.
- **What leaves the machine.** A hook sends only tool calls whose target resolves inside the project root. It never sends paths under an agent's own configuration or memory (`~/.claude`, `~/.codex`, `~/.gemini`), data files (by extension and by directory), or any call containing a term from the owner's local never-send list. The list, the project aliases and the list of governed repositories live in `~/.threefold/` on the owner's machine and are never committed here.
- **Verdict output per agent.** Claude Code and Codex: `hookSpecificOutput.permissionDecision = "deny"` with a reason, and nothing at all on approval, so the agent's own permission flow still runs. Antigravity: `{"decision": "deny", "reason": ...}`, and nothing on approval. Every agent: a failure to reach the service prints nothing and exits 0, unless `THREEFOLD_FAIL_CLOSED=1`.
- **Per-repository configuration (day 2).** The hook resolves each setting in this order: environment variable, then `<project root>/.threefold.json`, then `~/.threefold/config.json`. `.threefold.json` holds `project` (an Acme-style alias), `endpoint`, `mode` (`observe` sends every call as `dry_run`, `enforce` does not) and optionally `api_key_file` (a path to a file holding the key, never the key itself). It is written by the install script and listed in `.git/info/exclude`, so it is never committed to the governed repository. It may also hold `include`, a list of globs relative to its own directory: when present, a call is sent only if every path it targets, and the directory a command runs in, falls inside one of them, and anything else is held back as `not-included`. This is how a workspace root that holds several repositories governs only the ones the owner chose. A directory that is not a git repository (a workspace root) is installed in workspace mode: the four configuration files only, no pre-commit hook and no `.git` edits, with the install record kept in `~/.threefold/installs/`.
- **Rules per project (day 2).** Stored under `CONFIG#rules#<project>`. `GET /rules?project=X` returns that project's rules, or the shipped set with `is_default: true`. `POST /rules` accepts `{"project": X, "rules": [...]}` with the operator key; without `project` it replaces the shared set as before. The evaluator uses the calling project's rules when they exist and the shared set otherwise. `POST /rules/explain` accepts `project` as well.
- **Loop gate v2 (day 2).** Repeats of read-only or polling calls (`git status|log|diff|show`, `ls`, `cat`, `pwd`, `gh run view|list|watch`, `sleep`, and reads) raise an observation, never a trip. For `origin: hook`, a detected loop refuses the repeating call and records it, but never halts the session: a governed developer is not locked out of their own session. `sim-*` and page sessions keep the terminal halt, so the demo still shows call 3 refused and the session frozen.
- **Every write route (day 2).** A `Bash` call is read for the writes it makes: redirections (`>`, `>>`, `tee`), heredoc bodies, `sed -i`, `perl -pi`, `cp`, `mv`, `install`, `ln`, `rsync`, `dd of=`, `git apply`, `patch`, and `python -c` / `node -e` opening a file for writing. A write whose content can be read is judged like a `Write`. A write to a path an enforce rule covers whose content cannot be read is refused, with the reason "use Write or Edit so the rule can read it"; under an observe rule it is recorded. Writes to agent hook settings (`.claude/settings*.json`, `.codex/hooks.json`, `.agents/hooks.json`) and to `.git/hooks`, `git commit --no-verify` and `git -c core.hooksPath=…` are refused as protected-path calls.
- **Two stacks.** `threefold-prod` stays the public demo. `threefold-dogfood` is created from the same template with `PublicReads=false`, so its ledger and sessions need the operator key to read, and it carries the owner's real use under aliases. Only anonymised totals from it are ever shown in public.

## Contracts, 2026-09-22 — the application

Fixed before the tracks split. A track that needs a field not listed here asks the owner; it does not invent one.

**The product in one paragraph.** A team connects a repository with one command. Every project starts in **Observe**: every call is judged and recorded, nothing is refused (except a credential, which the hook refuses on the machine as before). The dashboard shows what each rule *would* have refused; the operator marks each of those **correct** or **false alarm**. A rule becomes **Ready** when everything it flagged was marked correct, or **Quiet** when it flagged nothing. **Promote** moves the project to **Enforce** with the rules the operator picks; the others keep observing. **Demote** is one click back to Observe. The operator signs in to a private stack from the command line, never by pasting a key.

**Rule key.** Every ledger row gains `rule_key`: the id of the layering rule that decided (`java-domain-stays-pure`, ...) when a layering rule decided, otherwise one of `LOOP`, `PROTECTED_PATH`, `UNREADABLE_WRITE`, `CREDENTIAL`, `BUDGET`, `HALTED_SESSION`, or `NONE` for a call nothing flagged. It is set on refusals and on observations alike. Readiness, `observe_rules` and the review queue all group by it. Existing fields (`rule`, `observed_rules`, ...) are kept unchanged.

**Stage (B1).** Project configuration lives at `PK=CONFIG#project#<name>`: `{stage: "observe"|"enforce", observe_rules: [rule_key], created_at, updated_at, promoted_at, demoted_at, sandbox: bool, history: [{at, action, by, enforce, observe}] (last 20)}`. It applies only to calls with `origin` `hook` or `ci` that are not `dry_run`: the stage is the project's configured stage, or the stack's `DEFAULT_HOOK_STAGE` (template parameter `DefaultHookStage`, default `observe`) when the project has none. Observe evaluates the call exactly as a dry run does (recorded, never refused, never halts). Enforce runs the gates; a refusal whose `rule_key` is in `observe_rules` becomes an observation instead. Page and simulation calls are unaffected and always enforce, so the public demo is unchanged. Every evaluation response gains `project_stage`, and every ledger row gains `stage` and `hook_mode` (echoed from the request, `unknown` when absent). `tests/conftest.py` sets `DEFAULT_HOOK_STAGE=enforce` so every existing test keeps its meaning; tests of the new default unset it.

**Rollups (B1).** `record_decision` also adds to `PK=STATS#<YYYY-MM-DD>, SK=<project>` with `ADD`: `calls`, `approved`, `refused`, `observed`, `agent:<agent>`, `origin:<origin>`, `refused:<rule_key>`, `observed:<rule_key>`, and sets a 35-day `ttl` if absent. Best effort: a failed rollup never fails a verdict. Charts and tiles read rollups, so they are exact however busy the ledger is; lists read the ledger.

**Endpoints (B1, all JSON, all reads open where `PublicReads=true` and operator-only where false, every row reduced by `public_row()` as today):**
- `GET /api/overview?days=7&project=` → `{window_days, generated_at, source: "rollups", totals: {calls, approved, refused, would_refuse, needs_review, false_alarms, projects, agents}, series: [{day, approved, observed, refused}], by_agent: [{agent, calls}], by_origin: [{origin, calls}], by_rule: [{rule_key, refused, would_refuse}], by_project: [{project, stage, configured, calls, refused, would_refuse, needs_review, last_seen}], stages: {observe, enforce}}`. `days` 1–30.
- `GET /api/decisions?days=7&project=&rule=&kind=all|refused|observed|approved&review=any|unreviewed|correct|false_alarm&agent=&session=&limit=50&cursor=` → `{items: [row], next_cursor}`. `limit` ≤ 200. A row is today's ledger row plus `rule_key`, `stage`, `hook_mode`, `review` (`correct`|`false_alarm`|`null`), `reviewed_at`, `review_note`, `category`, `category_label`. The cursor is opaque.
- `GET /api/decision?timestamp=<iso>&verdict_id=<id>` → `{decision: row, session: {session_id, calls, cost_usd, is_tripped} | null, rule: <layering rule definition> | null}`; 404 when absent.
- `GET /api/projects` → `{projects: [{project, stage, configured, observe_rules, created_at, promoted_at, last_seen, calls, refused, would_refuse, needs_review, agents: [..], hook_modes: [..]}]}` over the last 7 days, including projects seen in rollups that have no configuration (`configured: false`, `stage` = the stack default).
- `GET /api/projects/<name>?days=14` → `{project, config, readiness: {summary: {stage, days_observed, calls_observed, would_have_refused, reviewed, false_alarms, false_alarm_rate, rules_ready, rules_quiet, rules_noisy, rules_needing_review}, rules: [{rule_key, kind: "layering"|"gate", mode_now: "observe"|"enforce", would_refuse, correct, false_alarms, unreviewed, last_seen, state: "quiet"|"needs_review"|"ready"|"noisy", recommendation}]}}`. A project's rules are its layering rules in force plus the gate keys `LOOP`, `PROTECTED_PATH`, `UNREADABLE_WRITE`, `BUDGET`. State: `noisy` if any false alarm, else `needs_review` if any unreviewed, else `ready` if it flagged anything, else `quiet`.
- `POST /api/projects/<name>` `{stage?, observe_rules?}` creates or updates the configuration.
- `POST /api/projects/<name>/promote` `{enforce: [rule_key]}` → stage `enforce`, `observe_rules` = the project's rules minus `enforce`; a history entry. `POST /api/projects/<name>/demote` `{}` → stage `observe`; a history entry.
- `POST /api/projects/<name>/reviews` `{items: [{timestamp, verdict_id, label: "correct"|"false_alarm"|"clear", note?}]}` (≤ 100) → `{updated, skipped: [{verdict_id, reason}]}`. The label is stored on the ledger item itself (`review`, `reviewed_at`, `review_note` ≤ 200 chars, `reviewed_by` = 8 hex of a hash of the credential used, never the credential); an item of another project is skipped.
- `POST /api/sandbox` `{}` → `{project: "Acme-Sandbox-<8 hex>", calls_seeded, url: "dashboard.html#/projects/<name>"}`. Creates an Observe project with `sandbox: true` and a 24 hour `ttl`, and seeds about a dozen synthetic hook calls from all three agents through the real evaluator, among them would-refuse calls under at least two rules, one of which a reasonable reviewer would call a false alarm.

**Access (B2).**
- Writes (`POST`) under `/api/projects/<name>` need the operator, except that on a stack with `PublicReads=true` a name matching `^Acme-Sandbox-[0-9a-f]{8}$` is writable by anyone. `POST /api/sandbox` is open only where `PublicReads=true`. The new `GET` routes are page reads.
- The operator is a configured key (`X-API-Key` or `Authorization: Bearer`) or a live **sign-in session** (`Authorization: Bearer <token>`). A session counts as the operator everywhere the key does, except for minting sign-in links.
- `POST /api/auth/links` (operator key only) `{next?: "/projects/<name>"}` → `{code, expires_in: 120, url: "<base>dashboard.html#/signin?code=<code>&next=<next>"}`. The code is single-use and stored only as a hash.
- `POST /api/auth/sessions` `{code}` → `{token, expires_at, ttl_seconds: 43200}`; a used, expired or unknown code is 401. `DELETE /api/auth/sessions` revokes the presented token. `GET /api/auth/whoami` → `{authenticated, via: "key"|"session"|null, expires_at, reads_public, sandbox_writes}`.
- Stored in `src/threefold/infrastructure/auth_store.py` on the same table, `PK=AUTH#<sha256>`, `SK=CODE|SESSION`, with `ttl`; in memory when offline. The browser never holds the operator key after sign-in.
- Served files: `GET /dashboard.html` and `GET /app` (the dashboard); `GET /assets/<file>` for `.js`, `.css` and `.svg` files in `src/threefold/web/assets/`, with `__THREEFOLD_BASE_PATH__` substituted as in pages; `GET /install.py` = `src/threefold/tools/threefold_install.py` with `__THREEFOLD_ENDPOINT__` replaced by the stack's own `https://<host>/<stage>/`; `GET /dist/threefold-bundle.zip` = `bin/threefold_hook.py`, `bin/threefold_cli.py`, `lib/threefold/__init__.py`, `lib/threefold/domain/*.py`; `GET /dist/manifest.json` = `{files: [{path, sha256, bytes}], bundle_sha256, installer_sha256}`. All open on every stack.

**Hook and connect (A).**
- `mode` gains `managed`: sent as not `dry_run`, so the project's stage on the server decides. `observe` stays a hard cap on the machine. Every request carries `hook_mode`. The hook caches `project_stage` from responses in `THREEFOLD_HOME/stage/`; in `managed` mode the local refusal of writes to the hooks' own files applies only while the cached stage is `enforce`, and otherwise those writes are sent as a path, as in observe.
- `threefold_install.py connect [PATH] [--project NAME] [--agents auto|<list>] [--mode managed|observe|enforce] [--endpoint URL] [--api-key-file F] [--include G ...] [--dry-run] [--no-open]`; also `disconnect [PATH]`, `status`, `open`. The old `--repo` form keeps working. Defaults: mode `managed`, agents detected on the machine, project `Acme-<directory name>` made to fit the pattern. Run as the file served at `/install.py`, it installs from `/dist/threefold-bundle.zip` of the endpoint baked into it. It ends by sending one harmless `dry_run` call, printing whether it was recorded, and opening `dashboard.html#/projects/<name>`, through a sign-in link when an operator key file is configured.

**Private names for projects (2026-09-23).** Every alias a page renders is followed by the reader's own name for it when this browser holds one (`Acme-Proj-FB · the portal one`). The names live in `localStorage` under `threefold-local-names`, are set on `settings.html`, and are never sent: no route, request body, URL, copied command or field a handler reads back carries one, which a page test proves by driving the handlers with a sentinel label. A switch in the shared navigation (`threefold-local-names-hidden`) turns them all off at once for a screenshot, and with no name set every page's markup is what it was before. `threefold_install.py status --names-json` prints only a JSON object of alias to this machine's folder name, on standard output, with the warning that these are real names on standard error.

**The application (D).** `dashboard.html` is one page with hash routes: `#/overview` (tiles, a stacked daily chart, by agent, by rule, by project; every tile and bar opens the rows behind it), `#/projects`, `#/projects/<name>` (stage, readiness per rule, Promote and Demote, recent calls, agents and their hook mode, setup), `#/review` (the queue of unreviewed would-refuse calls, grouped by project and rule, labelled one at a time or in bulk), `#/calls?<filters>` and `#/call?timestamp=&verdict_id=` (drill-down), `#/connect` (choose a name, copy one command, then watch for the first call), `#/signin?code=&next=`, and `#/try` (the sandbox walkthrough for an anonymous visitor on the public stack). Charts are inline SVG from `assets/threefold.js`, no chart library. Every page shares the navigation and the sign-in state from `assets/threefold.js`. `console.html` sends the reader to `dashboard.html#/overview`.

## Ship gate

The gate is pass or fail: live on AWS, reachable by a public URL, with documented
proof of a coding agent connected to the AWS console. The stack stays up past the
submission deadline, but **how far past is unresolved**: this file says judging runs
the weeks of 6 and 13 October while `docs/RUNBOOK.md` says it finishes in the week of
19 October, and neither cites the rules page it came from. Until the Rules tab settles
it, treat the later date as the one that governs teardown.

| Requirement | State | Evidence `[PRIMARY]` |
|---|---|---|
| Live on AWS, public URL | **PASS** | Since 2026-09-22 the primary URL is the edge, `https://d1og72wpk4aqig.cloudfront.net/` (stack `threefold-prod-edge`, us-east-1: CloudFront, pages in a private S3 bucket behind Origin Access Control, WAF, security headers), which routes every JSON path to the API below. The API URL `https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/` still serves the dashboard itself, 200 and `text/html`, to an anonymous request with no API key. Stack `threefold-prod`, eu-west-1. The trailing slash is part of the URL: API Gateway answers the bare `/prod` with its own `{"message":"Not Found"}` before the function is reached, so every link to this project must carry it |
| A visitor can run the demo | **PASS** | Walked in a browser: Scenario 1 sent one `POST /simulate-loop`, the function evaluated the same call three times inside that request, the response was `BLOCKED_LOOP_DETECTED`, and the panel showed a genuine Haiku 4.5 sentence under the heading "Amazon Bedrock (Claude Haiku 4.5)" |
| Reachable by the AI scorer | **PASS** | `STAGE` is unset on the function, so the middleware defaults to `dev` and enforces no key. Verified by unauthenticated request. Do not read this off `/status`, which prints `"stage": "prod"`: that field has its own default and says nothing about whether a key is required |
| Proof of coding agent connected to AWS | **PASS** | `docs/PROOF_OF_AWS_AGENT.md` rewritten around the real session: the commands run, the two defects AWS surfaced, and the CloudTrail principal. Raw output in `docs/evidence/DEPLOYMENT_2026-09-20.md` |
| Public repository | **PASS** | Since 2026-09-25: `https://github.com/upgradedev/aws-threefold`, public, `main` pushed. CI (10 jobs), CodeQL, secret scanning with push protection, dependabot and keepalive all live. The count of commits is deliberately not recorded here: `git log` holds it, and any line stating it is wrong again the moment it is committed |
| Continuous delivery | **WRITTEN, role missing** | `.github/workflows/{ci,deploy,keepalive}.yml`. Deploy assumes `threefold-github-deploy`, which does not exist yet. Policy documents are committed at `deploy/iam/`, creation is `docs/RUNBOOK.md` section 8. Confirmed 2026-09-25: the CI run fails at assume-role, so regional deploys run from the owner's machine with the runbook's own commands |
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
| A refusal actually stops the write | Measured per agent, on disk. 2026-09-21: Claude Code 2.1.220 refused a `Write` and a `Bash` redirection and neither file was created; the Antigravity desktop app refused `write_to_file` and no file was created (`docs/evidence/ENFORCEMENT_2026-09-21.md`). 2026-09-23, Codex CLI 0.155.0: in one run it composed a patch adding a cloud SDK import into a governed domain file, the hook refused it, and the file's sha256 was unchanged, while the same agent created an ungoverned file in the same run, so the unchanged file is the refusal's doing. Asked to try three ways around it, it tried none. Its shell route is not measured, and neither is a refusal over `apply_patch` from a hook that makes no network call (`docs/evidence/ENFORCEMENT_2026-09-23.md`). Two open reports say a deny can be ignored (claude-code#91574, openai/codex#27833), which is why this is measured per agent rather than assumed |
| One hook governs three agents, and keeps at home what it must | `src/threefold/hooks/threefold_hook.py`, served at `/hooks/threefold_hook.py` and at the old URLs. Offline end to end against the service on 2026-09-21: a forbidden import refused for Claude Code, Codex (`apply_patch`) and Antigravity (`write_to_file`); a clean write prints nothing; a write outside the project is not sent; a credential is refused locally and never reaches the service. Live on 2026-09-21, a hook's refusal and an approval both answered without a model call and at no cost, while a page's refusal still carried a Bedrock sentence |
| Public data carries no real names | Live on 2026-09-21: `/api/insights` and `/api/sessions` show only `Acme-*` projects or `unlabelled`, and developers only as 8-hex hashes. Project names outside `AllowedProjectPattern` are stored as `unlabelled` with a warning in the response |
| A refusal cannot be walked around through the shell | Live on 2026-09-22: `cat > src/domain/acme_user.py <<'EOF'` with `import boto3`, and `echo 'import boto3' >> src/domain/x.py`, both answer `BLOCKED_BOUNDARY_VIOLATION`, the same as a `Write`; a rewrite of `.claude/settings.json` and `git commit --no-verify` are refused; an ordinary `pytest -q 2>&1 | tail -5` is approved. In the suite, 87 ordinary commands are approved and 113 bypass probes are refused |
| A loop stops the loop, not the developer | Live on 2026-09-22: a hook polling `git status` four times is approved every time; a hook repeating `npm run build` is refused on the third call without halting the session, and the next different call is approved; the demo's own loop still freezes its session |
| The owner's real work is governed | Since 2026-09-22 at nine locations of the owner's own work under aliases `Acme-Proj-*`, three agents each (Codex from 2026-09-27, for projects trusted in Codex), reporting to the private stack. Switched from observe on the machine to managed the same day, with every project still in Observe on the server, so the switch changed nothing an agent sees; promotion is now a dashboard action. Re-checked after the switch with synthetic calls through the registered command, 9/9: a write inside a listed repository reaches the private stack and is recorded as observed; writes into excluded repositories and a workspace's own files are held back and never reach it; a credential is refused on the machine; `git status` shows no Threefold file |
| The application is live | 2026-09-22, live checks 34/34 against both stacks: the dashboard, `/app`, its assets and `/install.py` (with the stack's own URL and a matching sha256 header) are served; the bundle's files match its manifest and the bundled hook is the repository's; on the public stack a sandbox is made and seeded, labelled anonymously, readiness follows the labels (the rule with a false alarm reads noisy), promotion makes the same kind of call refused with a Bedrock sentence while the noisy rule keeps observing, demotion makes it observed again, an unconfigured project's hook calls start in Observe, and a page call still enforces; on the private stack the overview is 401 without a key, the operator key mints a one-time link, the code works once, the session reads the private overview, cannot mint links, and ends at sign-out |
| The dashboard's numbers cover the history | Daily rollups started with the 2026-09-22 deploy; `scripts/backfill_rollups.py` added every older ledger row exactly once (133 rows on the public stack, 184 on the private one), claiming each with a conditional update, and a second run added nothing |
| Every claim, probed live | `scripts/probe_live.py`, 2026-09-23, after the review's findings were fixed and deployed: the edge 113 PASS, 0 FAIL; the API 113 PASS, 0 FAIL (`docs/evidence/PROBES_2026-09-23.md`); the private stack, read-only with the key, 97 PASS, 0 FAIL. Through the edge, `/install.py` names the edge's own URL, the bucket refuses a direct request, and a page that does not exist answers 404. Rerun 2026-09-25 after the behavior batch and the KMS-key fix deployed: the API 113 PASS, 0 FAIL, 3 SKIP and the edge the same (`docs/evidence/PROBES_2026-09-25-437df7c8.md`, `docs/evidence/PROBES_2026-09-25-945ec047.md`) |
| A refusal carries a checked fix | Live 2026-09-22: a page Write of `import boto3` into a domain file is refused with `suggested_fix` of kind layering, validated true: the domain file behind an `OrderPort` and an adapter in `infrastructure/`, each passing the layering, credential, boundary and syntax checks. The ledger keeps only the fix's kind and whether it was checked; the hook's deny reason carries the summary line |
| A private stack closes every read it has not declared public | Live 2026-09-23: on the private stack `GET /sessions.json`, `/insights.json` and `/readyz` answer 401 without the operator key and 200 with it, as `/api/sessions`, `/api/insights` and `/api/overview` already did. The two aliases had been open: they were served from the same handlers as the `/api/*` names but were missing from the list the gate reads. A test now walks 108 GET paths derived from the router itself and fails if one is neither declared public nor closed. The public demo is unchanged, and still answers them anonymously |
| The benchmark, measured | Four matrices of real Claude Code runs on 2026-09-22, 162 runs in all, with checkers that do not import Threefold. Standard tasks, `claude-sonnet-5`: a governed violation landed in 3 of 18 runs with no guidance, 0 of 18 with the rules in CLAUDE.md, 0 of 18 with Threefold, and every run's tests passed. `claude-haiku-4-5`: 7 of 18, 3 of 18, 0 of 18. Pressure tasks, whose prompt asks for the forbidden shortcut: `claude-sonnet-5` 6 of 9, 0 of 9, 0 of 9; `claude-haiku-4-5` 9 of 9, 5 of 9, 0 of 9. The price is stated with it: in 8 of those 18 governed pressure runs the agent stopped and reported instead of finishing. Codex, measured 2026-09-23 on the same tasks: standard 3 of 18, 0 of 18 (rules in `AGENTS.md`), 0 of 18; pressure 9 of 9, 1 of 9, 0 of 9, with 3 of those 9 governed runs stopping rather than finishing. The hook judged 166 of Codex's calls across its two matrices. Reports in `docs/evidence/BENCHMARK_2026-09-2*.md`, rows in `benchmark/results/`, and the six series are shown apart on `#/proof`, each naming its agent |
| A false refusal found by the benchmark, and fixed | The first full matrix refused a read-only `find . -path ./.git -prune` as reaching a credential store, in a task where nothing was wrong. The command check no longer reads the repository's own `.git` as a credential store, while writes under it, `--no-verify`, `core.hooksPath` and `.git-credentials` stay refused, and `rm -rf .git` is now named destructive. The matrix was rerun on the fixed code, and the numbers above are that run's |
| Test suite | 4894 passed, 6 skipped on 2026-09-25 (4848 on 2026-09-23). Hermetic: `THREEFOLD_OFFLINE` is set at import, the application-route tests run with a synthetic operator key, and no test depends on the order the directories run in |

## Known gaps, not yet fixed

These are recorded because they are still false or missing in the tree. None is
hidden in a document that a judge would read as finished work.

1. Fixed on 2026-09-25: all five keys `/policy/config` returns are enforced.
   The session ceiling trips through the breaker on top of each call's own
   `budget_usd`, the history window sets the detector's cycle search (clamped
   2-25), and the pattern list refuses through the secret gate under the
   credential key, always enforced and never staged. The POST handler accepts
   and strictly validates the list, and a saved policy is re-read every thirty
   seconds rather than adopted at cold start only.
2. The `calls` count in the sessions listing saturates at 50, because the store
   keeps `history[-50:]`. Cost and tokens are cumulative and are not capped. The
   page says so rather than presenting 50 as a total.
3. Fixed on 2026-09-25: the uploader nothing called (`s3_store.py`) was deleted
   together with the function role's S3 statement, so the role holds no object
   rights at all. `EvidenceStore.create_sealed_bundle` stays: it seals locally
   and is covered by a test. The bucket stays provisioned and empty until
   archival lands.
4. Fixed on 2026-09-25: the certificate covers the session's own stored
   verdicts. Every judged call records its verdict on the session, approvals
   and refusals alike, and a supplied `evaluations` list is ignored. The same
   canonical bytes carry a KMS signature where the stack holds a signing key
   (unsigned with the absence stated, anywhere else). A session with no
   recorded verdicts is refused with 400. What remains is requiring a
   certificate before a merge, which nothing in CI does.
5. Four of the five offline fallback panels still show canned prose. They now say
   "Simulated, offline demo, no model was reached" on their face, but the numbers
   inside them are invented and should be replaced with a real offline run.
6. Partly fixed on 2026-09-25: the byte-exact tier now searches up to the
   policy's history window, and a second tier searches the same cycles over
   normalized shapes (same tool, targets and argument keys, values ignored),
   tripping two repeats later than the exact one. What is still missing is
   semantic matching: two calls that do the same thing under different tools
   or spellings are not matched. The README no longer claims entropy scanning,
   because there is no entropy code.
7. The comparative number exists now (the row above), but it is one machine,
   one agent, six standard and three pressure tasks, three repetitions, two
   models, and no human baseline. It is enough to quote with its confidence
   intervals, not enough to generalise from.
8. No video and no Builder Center article. Neither is required by the rules, but
   the Builder Center project itself is, and it is owner-gated.

9. The benchmark's rate for the shell-write pressure task is a floor: an agent
   that fixes the generator and still uses a redirect breaks the rule without
   leaving anything in the files, so the checker cannot count it. Threefold
   refuses that redirect either way.
10. Codex is measured now, but narrowly: one run of the real hook, over its
    patch tool, on one Windows machine, where Codex has no sandbox of its own
    and the runs passed `--dangerously-bypass-approvals-and-sandbox`. Its
    shell route, and a refusal from a hook that makes no network call, are not
    measured.
11. Fixed and live on 2026-09-23: a page that does not exist answers 404. It is
    the bucket's own XML, not an RFC 7807 problem, because custom error pages
    would replace the API's problems too.
12. A refused write gets a rewritten, checked fix up to 1,500 characters of
    content, and advice in words (no rewrite, not claimed as checked) up to
    6,000 characters for a layering refusal of a call that runs no command.
    Beyond that it gets no suggested fix, because producing one would cost more
    than a whole verdict.
13. `POST /rules/draft` is open on the public stack like `/rules/explain`; its
    Bedrock spend is bounded per container (60 calls) and by the function's
    reserved concurrency, not by an account-wide counter.

14. The gate itself goes past the 10 ms budget TRAPS.md sets on import-dense
    files: a verdict on a Python domain file of 500 distinct imports, the
    forbidden one last (7,935 characters), took 13.3 ms on the development
    machine. Ordinary files stay well inside it.

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

1. There is no Builder Center project. The public repository exists since
   2026-09-25 (`https://github.com/upgradedev/aws-threefold`); until the
   project exists alongside it there is no submission, whatever the code does.
   The Builder Center project is owner-gated: it is a profile, a Join, and a
   web form, and no command for it exists anywhere in this repository.
2. No measured number yet. The hook is installed-ready but has not been run
   across a working week, which is where the number comes from.
3. Fixed on 2026-09-25 except the CI check: the certificate is issued from
   the session's stored verdicts and signed with a KMS key where the stack
   holds one. What remains open is making a CI check refuse a pull request
   whose session has no valid certificate, which is the work that would make
   the certificate load-bearing rather than decorative.
4. Fixed on 2026-09-25: the evaluate request carries `model_id`, the
   calculator prices Haiku ids on the Haiku row and everything else on the
   default rate, and the ledger row keeps the model so a cost can be audited.
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
7. Fixed on 2026-09-25: warm containers re-read the stored policy every
   thirty seconds, the same staleness the layering rules accept, so a save
   applies everywhere within half a minute.

## Cost and teardown

Regional stacks (`threefold-prod`, `threefold-dogfood`, eu-west-1): PAY_PER_REQUEST
DynamoDB with point-in-time recovery, one 256 MB arm64 Lambda with X-Ray and 25
reserved copies, an HTTP API with throttling and access logs, alarms to an SNS
topic, a CloudWatch dashboard, a KMS signing key at 1 USD a month, and an S3
bucket nothing writes to. ESTIMATE, from list prices: a few USD a month each
at demo traffic, most of it alarms and custom metrics. The edge (`threefold-prod-edge`, us-east-1): WAF about 9 USD a month
(web ACL plus four rules) plus requests, CloudFront inside the free tier, the
pages bucket and a log bucket for cents. Bedrock calls are capped per container.

Teardown, never before judging completes: `aws cloudformation delete-stack` for
`threefold-prod-edge` (us-east-1), then `threefold-prod` and `threefold-dogfood`
(eu-west-1). The edge's two buckets are retained on purpose and must be emptied
and deleted by hand.
