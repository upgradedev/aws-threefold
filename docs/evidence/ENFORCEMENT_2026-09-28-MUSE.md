# Does a refusal stop the write in Muse? Measured, 2026-09-28

Muse (Muse Code 1.4.0, Meta) is the fourth agent Threefold governs. It is
governed through its native plugin hooks, not through the MSP approval route:
the MSP route was measured dead on the same day (below), while a plugin hook
that answers deny stops the write in the agent. The standard is the same as
for the other agents: not what the agent says it did, but what is on the disk
afterwards — and, for the hook's own paths, what the plugin runner reports.

## Result

| Agent | Version | Route | Hook called | Write happened anyway |
|---|---|---|---|---|
| Muse | 1.4.0 | `write_file`, real hook refusing (owner runs, disk checked) | yes, and refused it | **no** |
| Muse | 1.4.0 | `write_file`, credential deny through the plugin runner (runner reported blocked; disk not checked) | yes, and refused it | **no** |
| Muse | 1.4.0 | `write_file`, service BLOCKED through the plugin runner (runner reported blocked; disk not checked) | yes, and refused it | **no** |
| Muse | 1.4.0 | `edit_file` (stdin shape captured; never refused) | yes, allowed | not measured |
| Muse | 1.4.0 | shell (`powershell`; stdin shape captured; never refused) | yes, allowed | not measured |

Read the rows exactly as they are written:

- **Row 1 is the decisive one.** In the owner's runs a deny from the hook
  stops the write in Muse Code 1.4.0 over `write_file`: the refused file was
  not created, the agent reported the call blocked, and no workaround was
  found in the measured runs.
- **Rows 2 and 3 are the runner's word, not the disk's.** The plugin runner
  was asked to run the real hook over a `write_file` carrying a credential
  and over one the stub service answered BLOCKED; both times the hook printed
  the Claude-style deny and the runner reported `should_block: true` with
  `permission_decision: "deny"`. No file was checked afterwards.
- **Rows 4 and 5 are not enforcement.** A PreToolUse hook captured the
  `edit_file` and `powershell` stdin shapes on the wire, but no deny was ever
  answered on those routes, so whether a deny stops an edit or a shell
  command in Muse is not measured.

## The MSP route is dead

Before the plugin route was tried, the owner probed `muse msp` (server 1.4.0)
as a bare JSON-RPC client, five probes in `C:/tmp/msp_v*.txt`, all on
2026-09-28 against workspace `C:/tmp/msp-probe-ws`:

- v1: `initialize` with the wrong shape answers parse error (`-32700`).
- v2: `initialize` answers `serverInfo: muse 1.4.0`; `session/start` with an
  approval mode is rejected (`-32030`, `approval_mode_ceiling`, "approval
  mode exceeds or is incomparable with the sealed startup mode");
  `session/list` is empty.
- v2b: every approval mode tried. `allowAll` and `onRequest` are rejected
  with `approval_mode_ceiling`; `promptUnmatched`, `denyUnmatched` and no
  mode start a session.
- v3: turns started under approve and deny both time out, and the probe files
  are written anyway (`probe_a.txt`, `probe_b.txt` exist): nothing asks.
- v4: `view/subscribe` is `method not found` (`-32601`); after a turn starts,
  `approval/listPending` answers `{"approvals": [], "userInputs": []}`.
- v5: the same under a further mode: `probe_c.txt` is written with content
  `gamma`, and the pending list is empty.

No approval or request reaches a bare MSP client under any mode, so there is
nothing there for Threefold to answer. The plugin route below is what ships.

## What the plugin hook is (owner probes)

A plugin root holds `.muse-plugin/plugin.json` naming one `PreToolUse` hook:

    "capabilities": {"hooks": [{"id": "govern", "event": "PreToolUse",
      "command": ["python", "hooks/threefold_hook.py", "--agent", "muse"],
      "timeoutMs": 10000, "statusMessage": "Threefold is judging this call"}]}

Registered with `muse plugins install <plugin-root> --scope project` and then
`muse plugins approve threefold`; removed with `muse plugins remove
threefold`. Placing files in the project does not register anything on its
own: there is no project auto-load.

Each call arrives on the hook's stdin in this shape (eight calls captured in
`C:/tmp/muse-tool-shapes.jsonl`):

    {"hook_event_name": "PreToolUse", "tool_name": ..., "tool_input": {...},
     "tool_use_id": ..., "session_id": ..., "turn_id": ...,
     "cwd": <absolute workdir>, "transcript_path": null, "model": ...,
     "permission_mode": ..., "model_provider": "meta"}

Tools: `write_file` (`content`, `path`) writes like Claude Code's Write,
`edit_file` (`find`, `path`, `replace`) edits like Edit, `powershell`
(`command`, `description`, `workdir`) runs the shell like Bash. Paths are
relative and resolve against the `cwd` the payload carries; the `workdir`
carries a Win32 `\\?\` prefix. Reads (`read_file`, `search`) and internal
calls (`submit_reminder_decision`, `cron_*`) are ignored: the hook prints
nothing and exits 0, and the agent proceeds (approval passthrough).

A deny is the Claude-style shape on stdout, exit 0:

    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
      "permissionDecision": "deny", "permissionDecisionReason": <reason>}}

## What this change measured (runner and CLI)

With this change's bundle (`src/threefold/tools/threefold_muse_plugin/`), in
scratch directories under `C:/tmp/muse-probe*` (never in a repository or the
owner's projects), on 2026-09-28, `muse --version` answering
`Muse Code 1.4.0`:

- `muse plugins validate <bundle>` answers
  `valid threefold native skills=0 commands=0 hooks=1 mcp=0 reminders=0
  diagnostics=0`. The first attempt, without `version`, `description` and
  `compat.manifestDir`, was invalid with three diagnostics, so the bundle
  carries all three.
- `muse plugins install <bundle> --scope project` exits 0 and answers
  `installed threefold 1.0.0 enabled=true trust=user-local
  provenance=native-local cache=...`, plus the warning `third-party plugin:
  hooks require review before activation`. It writes no file into the project
  directory: the directory holds exactly what it held before.
- `muse plugins list` afterwards answers
  `threefold 1.0.0 enabled=true active=true trust=user-local
  provenance=native-local valid=true diagnostics=0`. It answers the same from
  a different working directory, so the listing alone does not prove which
  project the registration belongs to.
- `muse plugins inspect threefold` shows the hook capability
  `plugin:threefold:hook:govern` at `status=review_needed` before approval.
- `muse plugins approve threefold` exits 0 with no prompt, answering
  `approve plugin:threefold:hook:govern`.
- `muse plugins hook test threefold:govern --fixture <path> --json` runs the
  real hook. The fixture is an envelope, `{"event": "PreToolUse", "stdin":
  {...}}`; the hook does not inherit the caller's environment, so the fixture
  `cwd` held a `.threefold.json` naming the project and endpoint instead.
  A `write_file` carrying a credential was denied locally: the hook's stdout
  held the deny above, exit 0, and the runner reported `should_block: true`
  with `permission_decision: "deny"` and terminal status `blocked`.
- With the endpoint pointing at a local stub answering `{"status":
  "BLOCKED", ...}`, the stub saw one request (`agent: muse`, `tool_name:
  Write`, `action_type: FILE_WRITE`, the relative path intact), the hook
  printed the deny, and the runner again reported `should_block: true` with
  `permission_decision: "deny"`.
- `muse plugins remove threefold` exits 0 answering `removed threefold`;
  `muse plugins list` then answers `no plugins`, the project's directory is
  unchanged, and the plugin's cache directory under the Muse home is gone.

## What is not measured

- Whether a deny stops an `edit_file` or a `powershell` call in a live Muse
  session. The hook answers the same deny shape on those routes, but no agent
  run has checked the disk afterwards.
- Whether the hook fires only in the project it was installed for. The
  listing shows the plugin from any directory; activation scope was not
  probed.
- Which project two checkouts of one repository belong to for project scope.
  Each install and removal runs in its own directory; sharing between linked
  worktrees was not probed.
- The approval prompt on other Muse versions (`approve` needed no input on
  1.4.0), and the hook command's bare `python` on a machine without Python
  on PATH.
