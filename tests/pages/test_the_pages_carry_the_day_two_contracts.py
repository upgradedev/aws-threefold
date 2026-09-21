"""The pages and the published contract say what the day-2 contracts fix.

Rules are kept per project, a repository is governed through the installer and
rolled out in observe before enforce, a hook's loop refuses the repeating call
without halting the session, and a halted session can be resumed. Each of those
is something a reader acts on from a page or from the spec, so each is held
here to the contract in STATE.md or to the evidence file it quotes.

The rules and connect pages are also run, not only read: their inline script is
executed under Node with a stub document and a recording fetch, because what a
page sends is decided in script and a string search cannot show it. Those tests
skip where Node is absent; the rest need nothing but the standard library.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from threefold.interfaces.api_handlers import lambda_handler

ROOT = Path(__file__).resolve().parents[2]


def _get(path: str, stage: str = "prod") -> dict:
    return lambda_handler(
        {
            "rawPath": f"/{stage}{path}",
            "headers": {},
            "requestContext": {"http": {"method": "GET"}, "stage": stage},
        }
    )


def _page(path: str) -> str:
    response = _get(path)
    assert response["statusCode"] == 200, f"{path} answered {response['statusCode']}"
    return response["body"]


def _spec() -> dict:
    return json.loads(_page("/openapi.json"))


def _deployed_project_pattern() -> str:
    template = (ROOT / "deploy" / "template.yml").read_text(encoding="utf-8")
    match = re.search(r"AllowedProjectPattern:\s*\n(?:\s+\w+:.*\n)*?\s+Default:\s*'([^']+)'", template)
    assert match, "Could not read the AllowedProjectPattern default out of deploy/template.yml"
    return match.group(1)


# A stub of the few browser objects the two pages touch. Every element is a
# plain object made on first use, and fetch records each request and answers
# with whatever the scenario has set in `answer`. An answer may be a promise, so
# a scenario can hold one read back and let a later one finish first.
_BROWSER_STUB = r"""
const elements = {};
function el(id) {
  if (!elements[id]) {
    elements[id] = {
      id, value: '', innerHTML: '', innerText: '', textContent: '', className: '', href: '', disabled: false,
      listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; }, getAttribute() { return ''; }
    };
  }
  return elements[id];
}
globalThis.window = globalThis;
globalThis.document = { getElementById: el, querySelectorAll: () => [], addEventListener: () => {} };
const store = {};
globalThis.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
globalThis.location = { origin: 'https://example.test', href: 'https://example.test/prod/page.html', search: '' };
globalThis.history = { replaceState: (state, title, url) => { globalThis.location.href = url; } };
globalThis.tailwind = {};
const calls = [];
let answer = () => ({ status: 200, body: { rules: [], count: 0, is_default: true } });
globalThis.fetch = async (url, init) => {
  init = init || {};
  calls.push({ url, method: init.method || 'GET', headers: init.headers || {}, body: init.body ? JSON.parse(init.body) : null });
  const reply = await answer(url, init);
  return { ok: reply.status < 400, status: reply.status, json: async () => reply.body };
};
function held() { let release; const promise = new Promise(r => { release = r; }); return { promise, release }; }
const tick = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setTimeout(r, 0)); };
"""


def _run_page(path: str, scenario: str, tmp_path: Path, before: str = "") -> dict:
    """Runs the page's inline scripts, as served, then the scenario, under Node.

    `before` runs ahead of the page's own script, for what the page reads as it
    loads, such as the address it was opened at.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not on PATH, so the page's script cannot be run here")
    scripts = re.findall(r"<script>(.*?)</script>", _page(path), re.S)
    assert scripts, f"{path} carries no inline script"
    program = tmp_path / "page.js"
    program.write_text(
        _BROWSER_STUB
        + before
        + "\n;\n"
        + "\n;\n".join(scripts)
        + "\n;\n(async () => {\n  const out = {};\n  await tick();\n"
        + scenario
        + "\n  process.stdout.write(JSON.stringify(out));\n})()"
        + ".catch(err => { process.stderr.write(String((err && err.stack) || err)); process.exit(1); });\n",
        encoding="utf-8",
    )
    finished = subprocess.run([node, str(program)], capture_output=True, text=True, timeout=60)
    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout)


# ---------------------------------------------------------------- rules.html


def test_the_rules_page_holds_a_project_name_to_the_pattern_the_stack_deploys_with() -> None:
    """A second copy of the pattern in the page could drift from the template.

    A name outside the pattern is stored as `unlabelled`, so rules saved under it
    would govern no call the reader meant. The page's pattern has to be the
    stack's default, character for character.
    """
    body = _page("/rules.html")
    match = re.search(r"const PROJECT_PATTERN = /(.+?)/;", body)
    assert match, "rules.html has no PROJECT_PATTERN"
    assert match.group(1) == _deployed_project_pattern()

    pattern = re.compile(match.group(1))
    assert pattern.match("Acme-Billing")
    for refused in ("Globex-Billing", "Acme-", "acme-billing", "Acme-Billing<script>", "Acme-" + "x" * 41):
        assert not pattern.match(refused), f"{refused!r} would be sent as a project"


def test_the_rules_page_reads_saves_and_tries_the_chosen_projects_rules(tmp_path: Path) -> None:
    out = _run_page(
        "/rules.html",
        r"""
  out.firstRead = calls[calls.length - 1].url;

  answer = () => ({ status: 200, body: { rules: [{ id: 'shared-rule', when_path_matches: ['**/domain/**'], forbid_imports: ['boto3'] }], count: 1, is_default: true } });
  el('project-input').value = 'Acme-Billing';
  chooseProject();
  await tick();
  out.projectRead = calls[calls.length - 1].url;

  store['threefold-operator-key'] = 'op-key-123';
  el('editor').value = JSON.stringify({ rules: [{ id: 'billing-only', when_path_matches: ['**/billing/**'], forbid_imports: ['java.sql'] }] });
  answer = (url, init) => (init && init.method === 'POST')
    ? { status: 200, body: { status: 'RULES_UPDATED', count: 1, refresh_seconds: 30 } }
    : { status: 200, body: { rules: [], count: 0, is_default: false } };
  await save();
  await tick();
  out.projectSave = calls.filter(c => c.method === 'POST' && /\/rules$/.test(c.url)).pop();

  answer = () => ({ status: 200, body: { verdict: 'ALLOW', language: 'java', imports: [], applicable_rules: [], violations: [], note: 'none', rules_considered: 'in force', scope: 'layering rules only' } });
  el('try-path').value = 'src/main/java/com/acme/billing/domain/Order.java';
  el('try-content').value = 'import java.util.List;';
  await tryRules(false);
  out.explain = calls[calls.length - 1];
  out.explainSaid = el('try-result').innerHTML;

  answer = (url, init) => (init && init.method === 'POST')
    ? { status: 200, body: { status: 'RULES_UPDATED', count: 1 } }
    : { status: 200, body: { rules: [], count: 0, is_default: true } };
  showSharedSet();
  await tick();
  out.sharedRead = calls[calls.length - 1].url;
  el('editor').value = JSON.stringify({ rules: [{ id: 'everyone', when_path_matches: ['**/domain/**'], forbid_imports: ['boto3'] }] });
  await save();
  await tick();
  out.sharedSave = calls.filter(c => c.method === 'POST' && /\/rules$/.test(c.url)).pop();
""",
        tmp_path,
    )
    assert out["firstRead"] == "https://example.test/prod/rules"
    assert out["projectRead"] == "https://example.test/prod/rules?project=Acme-Billing"

    save = out["projectSave"]
    assert save["body"]["project"] == "Acme-Billing"
    assert [rule["id"] for rule in save["body"]["rules"]] == ["billing-only"]
    assert save["headers"].get("X-API-Key") == "op-key-123", "The save must carry the operator key stored on the settings page"

    assert out["explain"]["url"].endswith("/rules/explain")
    assert out["explain"]["body"]["project"] == "Acme-Billing"
    assert "Acme-Billing" in out["explainSaid"]

    assert out["sharedRead"] == "https://example.test/prod/rules"
    assert "project" not in out["sharedSave"]["body"], "With no project chosen, the save replaces the shared set as before"


def test_the_rules_page_says_whether_the_rules_shown_are_the_projects_own(tmp_path: Path) -> None:
    """is_default true for a project means it has none of its own; false means these are its own."""
    out = _run_page(
        "/rules.html",
        r"""
  el('project-input').value = 'Acme-Billing';
  answer = () => ({ status: 200, body: { rules: [{ id: 'shared-rule', when_path_matches: ['**'], forbid_imports: ['boto3'] }], count: 1, is_default: true } });
  chooseProject();
  await tick();
  out.defaultBadge = el('scope-badge').innerText;
  out.defaultOrigin = el('rules-origin').innerText;

  answer = () => ({ status: 200, body: { rules: [{ id: 'own-rule', when_path_matches: ['**'], forbid_imports: ['boto3'] }], count: 1, is_default: false } });
  load();
  await tick();
  out.ownBadge = el('scope-badge').innerText;
  out.ownOrigin = el('rules-origin').innerText;
  out.saveTarget = el('save-target').innerText;
""",
        tmp_path,
    )
    assert "SHARED DEFAULT" in out["defaultBadge"]
    assert "no rules of its own" in out["defaultOrigin"]
    assert "ITS OWN RULES" in out["ownBadge"]
    assert "for Acme-Billing alone" in out["ownOrigin"]
    assert "Acme-Billing" in out["saveTarget"]


def test_the_rules_page_never_sends_a_name_outside_the_pattern(tmp_path: Path) -> None:
    out = _run_page(
        "/rules.html",
        r"""
  el('project-input').value = 'Acme-Billing';
  chooseProject();
  await tick();
  const before = calls.length;
  el('project-input').value = 'Globex-Billing';
  chooseProject();
  await tick();
  out.sentAfterBadName = calls.length - before;
  out.problem = el('project-problem').innerText;
  out.stillOn = currentProject;
""",
        tmp_path,
    )
    assert out["sentAfterBadName"] == 0
    assert out["problem"], "A refused name must be explained, not ignored"
    assert out["stillOn"] == "Acme-Billing", "A refused name must not become what a save lands on"


# Acme-Alpha has saved a rule of its own, so a copy of it turning up in a save
# for another project can only have come from the editor.
_ALPHA_OWN = "{ status: 200, body: { rules: [{ id: 'alpha-own', when_path_matches: ['**/alpha/**'], forbid_imports: ['boto3'] }], count: 1, is_default: false } }"


def test_a_save_after_a_failed_read_never_sends_the_previous_projects_rules(tmp_path: Path) -> None:
    """The name on screen moves on at once; the editor moves on only when the new read succeeds.

    Found by running the page: Acme-Beta's read answered 503, the button read
    "Save for Acme-Beta", and the save sent Acme-Alpha's own rule under Beta's
    name, quietly giving Beta a copy of Alpha's gate.
    """
    out = _run_page(
        "/rules.html",
        r"""
  store['threefold-operator-key'] = 'op-key-123';
  answer = () => (ALPHA_OWN);
  el('project-input').value = 'Acme-Alpha';
  chooseProject();
  await tick();
  out.alphaEditor = el('editor').value;

  answer = (url, init) => (init && init.method === 'POST')
    ? { status: 200, body: { status: 'RULES_UPDATED', count: 1, refresh_seconds: 30 } }
    : { status: 503, body: { detail: 'unavailable' } };
  el('project-input').value = 'Acme-Beta';
  chooseProject();
  await tick();
  out.badge = el('scope-badge').innerText;
  out.button = el('save-button').innerText;
  await save();
  out.saidAfterFailedRead = el('save-result').innerHTML;
  resetEditor();
  await save();
  out.saidAfterReset = el('save-result').innerHTML;
  out.postsBeforeExample = calls.filter(c => c.method === 'POST').length;

  loadExample('java');
  await save();
  out.exampleSave = calls.filter(c => c.method === 'POST').pop();
""".replace("ALPHA_OWN", _ALPHA_OWN),
        tmp_path,
    )
    assert "alpha-own" in out["alphaEditor"]
    assert out["badge"] == "NOT READ" and "Acme-Beta" in out["button"], "The scenario found: Beta on the button, Alpha in the editor"
    assert out["postsBeforeExample"] == 0, "Acme-Alpha's rules were sent under Acme-Beta's name"
    assert "Acme-Alpha" in out["saidAfterFailedRead"] and "Nothing was sent" in out["saidAfterFailedRead"]
    assert "Nothing was sent" in out["saidAfterReset"], "Back to what is in force puts Alpha's rules back; it must not make them Beta's"

    # An example belongs to no project, so a failed read does not lock Beta out.
    example = out["exampleSave"]
    assert example["body"]["project"] == "Acme-Beta"
    assert [rule["id"] for rule in example["body"]["rules"]] == ["billing-domain-stays-pure"]


def test_a_save_while_the_next_projects_rules_are_still_being_read_sends_nothing(tmp_path: Path) -> None:
    out = _run_page(
        "/rules.html",
        r"""
  store['threefold-operator-key'] = 'op-key-123';
  answer = () => (ALPHA_OWN);
  el('project-input').value = 'Acme-Alpha';
  chooseProject();
  await tick();

  const betaRead = held();
  answer = (url, init) => (init && init.method === 'POST')
    ? { status: 200, body: { status: 'RULES_UPDATED', count: 1, refresh_seconds: 30 } }
    : betaRead.promise;
  el('project-input').value = 'Acme-Beta';
  chooseProject();
  await tick();
  await save();
  out.postsWhileReading = calls.filter(c => c.method === 'POST').length;
  out.said = el('save-result').innerHTML;

  betaRead.release({ status: 200, body: { rules: [{ id: 'beta-own', when_path_matches: ['**/beta/**'], forbid_imports: ['boto3'] }], count: 1, is_default: false } });
  await tick();
  await save();
  out.saveOnceRead = calls.filter(c => c.method === 'POST').pop();
""".replace("ALPHA_OWN", _ALPHA_OWN),
        tmp_path,
    )
    assert out["postsWhileReading"] == 0, "Acme-Alpha's rules were sent under Acme-Beta's name while Beta's were being read"
    assert "Acme-Alpha" in out["said"] and "Nothing was sent" in out["said"]

    # Once Beta's own rules are in the editor, the save goes through with them.
    assert out["saveOnceRead"]["body"]["project"] == "Acme-Beta"
    assert [rule["id"] for rule in out["saveOnceRead"]["body"]["rules"]] == ["beta-own"]


def test_a_link_naming_a_project_opens_the_page_on_that_projects_rules(tmp_path: Path) -> None:
    linked = _run_page(
        "/rules.html",
        r"""
  out.firstRead = calls[0].url;
  out.field = el('project-input').value;
  out.button = el('save-button').innerText;
""",
        tmp_path,
        before="location.search = '?project=Acme-Linked';",
    )
    assert linked["firstRead"] == "https://example.test/prod/rules?project=Acme-Linked"
    assert linked["field"] == "Acme-Linked"
    assert linked["button"] == "Save for Acme-Linked"

    # A link is held to the same pattern as a typed name, so markup in it is never read or saved under.
    refused = _run_page(
        "/rules.html",
        r"""
  out.firstRead = calls[0].url;
  out.field = el('project-input').value;
""",
        tmp_path,
        before="location.search = '?project=Acme-Linked%3Cscript%3E';",
    )
    assert refused["firstRead"] == "https://example.test/prod/rules"
    assert refused["field"] == ""


def test_only_the_latest_read_is_drawn_when_answers_arrive_out_of_order(tmp_path: Path) -> None:
    """Switching twice quickly must not let the slower first answer draw last.

    It would show one project's rules under the other's name, and put them in
    the editor that the save sends.
    """
    out = _run_page(
        "/rules.html",
        r"""
  const reads = {};
  answer = url => { const read = held(); reads[url] = read; return read.promise; };
  el('project-input').value = 'Acme-Slow';
  chooseProject();
  el('project-input').value = 'Acme-Fast';
  chooseProject();
  await tick();
  reads['https://example.test/prod/rules?project=Acme-Fast'].release({ status: 200, body: { rules: [{ id: 'fast-own', when_path_matches: ['**'], forbid_imports: ['boto3'] }], count: 1, is_default: false } });
  await tick();
  reads['https://example.test/prod/rules?project=Acme-Slow'].release({ status: 200, body: { rules: [{ id: 'slow-own', when_path_matches: ['**'], forbid_imports: ['boto3'] }], count: 1, is_default: false } });
  await tick();
  out.list = el('rules-list').innerHTML;
  out.badge = el('scope-badge').innerText;
  out.editor = el('editor').value;
""",
        tmp_path,
    )
    assert "fast-own" in out["list"] and "slow-own" not in out["list"]
    assert "Acme-Fast" in out["badge"]
    assert "fast-own" in out["editor"] and "slow-own" not in out["editor"]


def test_the_rules_page_escapes_what_the_service_returns(tmp_path: Path) -> None:
    """refresh_seconds reaches the save message from two answers, the save's and the last read's."""
    out = _run_page(
        "/rules.html",
        r"""
  answer = () => ({ status: 200, body: { rules: [{ id: '<img src=x onerror=alert(1)>', description: '<b>bold</b>', when_path_matches: ['<i>**</i>'], forbid_imports: ['boto3'] }], count: 1, is_default: false } });
  el('project-input').value = 'Acme-Billing';
  chooseProject();
  await tick();
  out.list = el('rules-list').innerHTML;

  store['threefold-operator-key'] = 'op-key-123';
  const rules = { status: 200, body: { rules: [{ id: 'billing-only', when_path_matches: ['**'], forbid_imports: ['boto3'] }], count: 1, is_default: false } };
  answer = (url, init) => (init && init.method === 'POST')
    ? { status: 200, body: { status: 'RULES_UPDATED', count: 1, refresh_seconds: '<img src=x onerror=alert(1)>' } }
    : rules;
  load();
  await tick();
  await save();
  out.savedFromTheSaveAnswer = el('save-result').innerHTML;

  answer = (url, init) => (init && init.method === 'POST')
    ? { status: 200, body: { status: 'RULES_UPDATED', count: 1 } }
    : { status: 200, body: Object.assign({}, rules.body, { refresh_seconds: '<svg onload=alert(2)>' }) };
  load();
  await tick();
  await save();
  out.savedFromTheReadAnswer = el('save-result').innerHTML;
""",
        tmp_path,
    )
    assert "<img" not in out["list"] and "&lt;img" in out["list"]
    assert "<b>bold" not in out["list"] and "<i>**" not in out["list"]

    assert "Saved 1 rule" in out["savedFromTheSaveAnswer"] and "<img" not in out["savedFromTheSaveAnswer"]
    assert "Saved 1 rule" in out["savedFromTheReadAnswer"] and "<svg" not in out["savedFromTheReadAnswer"]


# -------------------------------------------------------------- connect.html


def _section(body: str, section_id: str) -> str:
    match = re.search(rf'<section id="{section_id}".*?</section>', body, re.S)
    assert match, f"No section with id {section_id}"
    return match.group(0)


def test_the_connect_page_tells_a_reader_how_to_govern_their_own_repositories() -> None:
    section = _section(_page("/connect.html"), "govern-your-repositories")
    assert "Govern your own repositories" in section
    for fact in (
        "scripts/threefold_install.py",
        ".threefold.json",
        ".claude/settings.local.json",
        ".codex/hooks.json",
        ".agents/hooks.json",
        "pre-commit",
        ".git/info/exclude",
        "--mode enforce",
        "--uninstall",
    ):
        assert fact in section, f"The installer section does not mention {fact}"
    assert re.search(r"Codex loads a project's hooks only when that project is trusted in Codex", section)


def test_the_installer_command_carries_every_flag_and_the_endpoint_in_the_bar(tmp_path: Path) -> None:
    out = _run_page(
        "/connect.html",
        r"""
  out.install = el('install-snippet').textContent;
  out.config = el('threefold-json-snippet').textContent;
  out.enforce = el('enforce-snippet').textContent;
  out.uninstall = el('uninstall-snippet').textContent;
  el('apiBaseInput').value = 'https://acme-governance.example.test/prod/';
  renderSnippets();
  out.installElsewhere = el('install-snippet').textContent;
""",
        tmp_path,
    )
    install = out["install"]
    for flag in ("--repo ", "--project Acme-", "--mode observe", "--endpoint https://example.test/prod", "--api-key-file "):
        assert flag in install, f"The installer command lacks {flag.strip()}"
    assert "--endpoint https://acme-governance.example.test/prod " in out["installElsewhere"]

    config = json.loads(out["config"])
    assert set(config) == {"project", "endpoint", "mode", "api_key_file"}, "Only the keys the contract fixes"
    assert config["mode"] == "observe"
    assert re.match(_deployed_project_pattern(), config["project"])
    assert "--mode enforce" in out["enforce"]
    assert "--uninstall" in out["uninstall"]
    assert "a call carrying a credential is still refused on this machine" in install


def _plain(html: str) -> str:
    """The words a reader sees, with tags dropped and line breaks folded."""
    return " ".join(re.sub(r"<[^>]+>", "", html).split())


def _hook_decision_in_observe_on_a_credential(tmp_path: Path) -> str | None:
    """Runs the hook as an agent does, in observe, on a Write that carries a credential.

    Observe is set both ways it can be: the day-1 variable and the day-2
    per-repository file. The machine is a temporary directory and the endpoint
    answers nothing, so no owner file is read and nothing leaves.
    """
    home = tmp_path / "home"
    project = tmp_path / "acme-probe"
    home.mkdir()
    project.mkdir()
    endpoint = "http://127.0.0.1:9"
    (project / ".threefold.json").write_text(
        json.dumps({"project": "Acme-Probe", "endpoint": endpoint, "mode": "observe"}), encoding="utf-8"
    )
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("THREEFOLD_") and name.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    }
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        THREEFOLD_HOME=str(home / ".threefold"),
        THREEFOLD_PROJECT="Acme-Probe",
        THREEFOLD_ENDPOINT=endpoint,
        THREEFOLD_DRY_RUN="1",
        PYTHONIOENCODING="utf-8",
    )
    payload = {
        "session_id": "acme-probe-1",
        "cwd": str(project),
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(project / "src" / "settings.py"), "content": "KEY = '" + "AKIA" + "ACMEEXAMPLE00000'\n"},
    }
    ran = subprocess.run(
        [sys.executable, str(ROOT / "src" / "threefold" / "hooks" / "threefold_hook.py"), "--agent", "claude-code"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=env,
        cwd=str(project),
    )
    if not ran.stdout.strip():
        return None
    return json.loads(ran.stdout)["hookSpecificOutput"]["permissionDecision"]


def test_observe_mode_is_described_as_the_hook_runs_it(tmp_path: Path) -> None:
    """In observe the service refuses nothing, but the hook still refuses a credential.

    The pages said no call is refused in observe. The hook refuses a call
    carrying a credential before it builds any request, dry run or not, since
    the contract says such a call never leaves the machine; and a call it holds
    back is neither sent nor recorded. The hook is run here, so if observe ever
    does let a credential through, this test says the pages must change with it.
    """
    assert _hook_decision_in_observe_on_a_credential(tmp_path) == "deny"

    page = _page("/connect.html")
    section = _plain(_section(page, "govern-your-repositories"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    install = _plain(readme.split("## Install it in front of your own agent", 1)[1].split("\n## ", 1)[0])
    for where, words in (("connect.html", section), ("README.md", install)):
        assert "a call carrying a credential is still refused on your machine and never sent" in words.lower(), where
        assert "neither sent nor recorded" in words, f"{where} does not say a held-back call is neither sent nor recorded"
    for claim in (
        "none is refused",
        "judged and recorded, never refused, and never halts",
        "every call is judged and recorded but never refused",
    ):
        assert claim not in _plain(page) and claim not in _plain(readme), f"Still claimed: {claim!r}"


def _measured_per_agent() -> dict[str, str]:
    """Reads the result table of the enforcement evidence, one verdict per agent."""
    evidence = (ROOT / "docs" / "evidence" / "ENFORCEMENT_2026-09-21.md").read_text(encoding="utf-8")
    names = {"Claude Code": "claude-code", "Codex CLI": "codex", "Codex": "codex", "Antigravity": "antigravity"}
    verdicts: dict[str, set[str]] = {}
    for line in evidence.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5 or cells[0] not in names:
            continue
        happened = cells[4].strip("*").lower()
        verdicts.setdefault(names[cells[0]], set()).add(
            "stopped" if happened == "no" else "not-measured" if happened == "not measured" else "wrote-anyway"
        )
    assert verdicts, "No result rows were read from the evidence file, so this test proves nothing"
    return {agent: (found.pop() if len(found) == 1 else "mixed") for agent, found in verdicts.items()}


def test_each_agents_enforcement_claim_is_what_the_evidence_measured() -> None:
    """The table said "being verified" for all three after two had been measured.

    Worse would be the reverse: a page saying a deny stops the write for an
    agent nobody ran. Each row is held to the evidence file's result table.
    """
    body = _page("/connect.html")
    measured = _measured_per_agent()
    assert measured.get("codex") == "not-measured"
    for agent, verdict in measured.items():
        row = re.search(rf'<tr[^>]*data-agent="{agent}"[^>]*data-enforcement="([a-z-]+)"[^>]*>(.*?)</tr>', body, re.S)
        assert row, f"connect.html has no enforcement row for {agent}"
        claimed, cells = row.groups()
        assert claimed == verdict, f"connect.html claims {claimed} for {agent}; the evidence says {verdict}"
        if verdict == "stopped":
            assert "stops the write" in cells
        else:
            assert "stops the write" not in cells and "not measured" in cells
    assert "being verified" not in body


# ------------------------------------------------------------- openapi.json


def test_the_spec_documents_rules_per_project() -> None:
    paths = _spec()["paths"]
    read = paths["/rules"]["get"]
    assert any(p["name"] == "project" and p["in"] == "query" for p in read.get("parameters", []))
    assert "is_default" in read["description"]

    save = paths["/rules"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert "project" in save["properties"]
    assert "project" not in save.get("required", []), "Without project the shared set is replaced, as before"

    explain = paths["/rules/explain"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert "project" in explain["properties"]


def test_the_spec_documents_resume_behind_the_operator_key() -> None:
    """Documented with no body, Try it out on this route could only ever get a 400.

    The service requires operator_name and reason, refusing either when missing
    or over its limit, and answers 409 for a session that is not halted.
    """
    resume = _spec()["paths"]["/sessions/{session_id}/resume"]["post"]
    assert {"OperatorApiKey": []} in resume["security"]
    assert {"200", "400", "401", "403", "404", "409"} <= set(resume["responses"])
    assert "session-not-found" in resume["responses"]["404"]["description"]
    assert "session-not-halted" in resume["responses"]["409"]["description"]

    body = resume["requestBody"]
    assert body["required"] is True
    schema = body["content"]["application/json"]["schema"]
    assert set(schema["required"]) == {"operator_name", "reason"}
    for field, limit in (("operator_name", 120), ("reason", 240)):
        documented = schema["properties"][field]
        assert documented["type"] == "string" and documented["maxLength"] == limit, f"{field} as the service reads it"
    example = body["content"]["application/json"]["example"]
    assert set(example) == {"operator_name", "reason"}
    assert len(example["operator_name"]) <= 120 and len(example["reason"]) <= 240

    answered = resume["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    assert answered["status"]["enum"] == ["SESSION_RESUMED"]
    assert {"session_id", "operator", "reason", "resumed_at", "previous_trip_reason", "is_tripped"} <= set(answered)


def test_the_spec_says_a_hooks_loop_refuses_the_call_without_halting_the_session() -> None:
    paths = _spec()["paths"]
    evaluate = paths["/evaluate-tool-call"]["post"]["description"]
    assert "For origin hook, a detected loop refuses the repeating call" in evaluate
    assert "never halts the session" in evaluate
    assert "sim-" in evaluate and "keep the terminal halt" in evaluate
    assert "keeps the terminal halt" in paths["/simulate-loop"]["post"]["description"]


def test_the_spec_marks_nothing_as_not_deployed() -> None:
    """The service track implements every documented route in the same change."""
    assert "not deployed" not in json.dumps(_spec()).lower()


# ------------------------------------------------------------------ README.md


def test_the_readme_installs_with_the_installer_and_rolls_out_observe_then_enforce() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    install = readme.split("## Install it in front of your own agent", 1)[1].split("\n## ", 1)[0]
    for fact in ("scripts/threefold_install.py", "--mode observe", "--mode enforce", ".git/info/exclude"):
        assert fact in install, f"The install section does not mention {fact}"
    assert install.index("--mode observe") < install.index("--mode enforce")


def test_the_readme_prints_no_test_count() -> None:
    """A count in prose is wrong again the moment a test is added."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert not re.search(r"\b\d+\s+(tests|passed)\b", readme)


def test_the_readme_claims_no_enforcement_for_codex() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Codex was not measured" in readme
    assert "being verified" not in readme
