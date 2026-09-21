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
import re
import shutil
import subprocess
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
# with whatever the scenario has set in `answer`.
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
  const reply = answer(url, init);
  return { ok: reply.status < 400, status: reply.status, json: async () => reply.body };
};
const tick = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setTimeout(r, 0)); };
"""


def _run_page(path: str, scenario: str, tmp_path: Path) -> dict:
    """Runs the page's inline scripts, as served, then the scenario, under Node."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not on PATH, so the page's script cannot be run here")
    scripts = re.findall(r"<script>(.*?)</script>", _page(path), re.S)
    assert scripts, f"{path} carries no inline script"
    program = tmp_path / "page.js"
    program.write_text(
        _BROWSER_STUB
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


def test_the_rules_page_escapes_what_the_service_returns(tmp_path: Path) -> None:
    out = _run_page(
        "/rules.html",
        r"""
  answer = () => ({ status: 200, body: { rules: [{ id: '<img src=x onerror=alert(1)>', description: '<b>bold</b>', when_path_matches: ['<i>**</i>'], forbid_imports: ['boto3'] }], count: 1, is_default: false } });
  el('project-input').value = 'Acme-Billing';
  chooseProject();
  await tick();
  out.list = el('rules-list').innerHTML;
""",
        tmp_path,
    )
    assert "<img" not in out["list"] and "&lt;img" in out["list"]
    assert "<b>bold" not in out["list"] and "<i>**" not in out["list"]


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
    resume = _spec()["paths"]["/sessions/{session_id}/resume"]["post"]
    assert {"OperatorApiKey": []} in resume["security"]
    assert {"200", "401", "403", "404"} <= set(resume["responses"])
    assert "session-not-found" in resume["responses"]["404"]["description"]


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
