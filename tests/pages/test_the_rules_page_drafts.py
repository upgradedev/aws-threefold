"""The rules page drafts a rule from a sentence, tries it, and saves it only when asked.

What the panel sends and shows is decided in script, so these tests run the
page's own script and the shared layer it loads under Node, with the stub
browser and recording fetch of _browser.py. The service's answers are shaped as
`POST /rules/draft`, `POST /rules/explain` and `GET /rules` answer them. Where the
model's words reach the page they carry markup that would run if it were not
escaped. Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import re
from pathlib import Path

from _browser import page_source, run

PROJECT = "Acme-Billing"

# A drafted answer as the route gives it, with every string the model could
# have put there carrying markup, and a handful of helpers the scenarios share.
FIXTURES = r"""
const EVIL = 'x"><svg onload=alert(2)><img src=x onerror=alert(1)>';
const ORDER = 'src/main/java/com/acme/billing/domain/Order.java';
const RULE = {
  id: 'billing-domain-stays-pure', description: 'Billing domain classes may not reach persistence', mode: 'observe',
  when_path_matches: ['**/billing/domain/**/*.java'], forbid_imports: ['javax.persistence'], allow_imports: ['java.util']
};
function drafted(extra) {
  return Object.assign({
    rule: RULE,
    validation: { ok: true, errors: [] },
    tried: [
      { path: ORDER, content_preview: 'import javax.persistence.Entity;', expected: 'refuse', verdict: 'OBSERVE', matched: true, origin: 'caller', note: 'imports javax.persistence' },
      { path: 'src/billing/domain/acme/Example.java', content_preview: 'import java.util.List;\n', expected: 'refuse', verdict: 'ALLOW', matched: false, origin: 'generated', note: 'No layering rule applies' }
    ],
    source: 'bedrock', model: 'eu.anthropic.claude-haiku-4-5-20251001-v1:0', attempts: 1, project: 'Acme-Billing',
    saved: false, notes: ['Saved nothing. The draft watches first.'], unsupported: '', warnings: []
  }, extra || {});
}
const IN_FORCE = { rules: [{ id: 'shared-rule', mode: 'enforce', when_path_matches: ['**/domain/**'], forbid_imports: ['boto3'] }], count: 1, is_default: true, refresh_seconds: 30 };
function posts(path) { return calls.filter(c => c.method === 'POST' && new URL(c.url).pathname.replace(/^\/prod/, '') === path); }
function describe(text) { el('draft-description').value = text; updateDraftCounters(); }
"""

SIGNED_IN = "store['threefold-session'] = JSON.stringify({ token: 'tok-1', expires_at: new Date(Date.now() + 3600e3).toISOString() });\n"


def rules_page(scenario: str, tmp_path: Path, before: str = "") -> dict:
    """Runs rules.html with the fixtures and a stack that answers GET /rules."""
    return run(
        "rules.html",
        scenario,
        tmp_path,
        before=FIXTURES + "answer = api({ 'GET /rules': { status: 200, body: IN_FORCE } });\n" + before,
    )


# ------------------------------------------------------------ what the page declares


def test_the_page_declares_the_draft_panel_and_the_three_calls_it_makes() -> None:
    body = page_source("rules.html")
    assert 'id="draft"' in body, "The project page links to rules.html#draft, so the panel needs that id"
    assert "Draft a rule from a sentence" in body
    for field in ("draft-description", "draft-description-count", "draft-project", "draft-examples",
                  "draft-add-example", "draft-button", "draft-rule", "draft-retry-button", "draft-save-button"):
        assert f'id="{field}"' in body, f"The panel has no {field}"
    assert 'data-anchor="#/default/post_rules_draft"' in body, "The panel deep links to its operation"
    assert "'/rules/draft'" in body and "'/rules/explain'" in body and "'/rules?project='" in body
    assert "A drafted rule starts in OBSERVE" in body


def test_the_panel_holds_the_caps_the_drafter_enforces() -> None:
    from threefold.application import rule_drafter

    body = page_source("rules.html")
    assert f"const DRAFT_MAX_DESCRIPTION = {rule_drafter.MAX_DESCRIPTION_CHARS};" in body
    assert f"const DRAFT_MAX_EXAMPLES = {rule_drafter.MAX_EXAMPLES};" in body
    assert f"const DRAFT_MAX_CONTENT = {rule_drafter.MAX_EXAMPLE_CONTENT_CHARS};" in body


def test_a_save_is_sent_from_one_function_and_that_function_only_from_its_button() -> None:
    """Drafting and saving are never combined: no draft path reaches POST /rules."""
    body = page_source("rules.html")
    script = body.split('id="draft"', 1)[1]
    assert re.findall(r"saveDraftedRule\(", body) == ["saveDraftedRule(", "saveDraftedRule("], (
        "saveDraftedRule is defined once and called only from the Save to project button"
    )
    assert 'onclick="saveDraftedRule()"' in script
    draft_fn = body.split("async function draftRule()", 1)[1].split("\n    }\n", 1)[0]
    assert "'/rules/draft'" in draft_fn and "DEFAULT_API_BASE + '/rules'," not in draft_fn


# ------------------------------------------------------------------ the form


def test_the_description_counter_and_the_draft_button_follow_the_600_character_cap(tmp_path: Path) -> None:
    out = rules_page(
        r"""
  out.initial = { disabled: el('draft-button').disabled, count: el('draft-description-count').innerText };
  describe('Billing domain classes may not reach persistence.');
  out.written = { disabled: el('draft-button').disabled, count: el('draft-description-count').innerText };
  describe('d'.repeat(600));
  out.atCap = el('draft-button').disabled;
  describe('d'.repeat(601));
  out.over = { disabled: el('draft-button').disabled, count: el('draft-description-count').innerText, tone: el('draft-description-count').className, hint: el('draft-hint').innerText };
  describe('\u{1F600}'.repeat(600));
  out.emoji = { disabled: el('draft-button').disabled, count: el('draft-description-count').innerText };
  describe('   ');
  out.blank = el('draft-button').disabled;
  await draftRule();
  out.sentWhenBlank = posts('/rules/draft').length;
""",
        tmp_path,
    )
    assert out["initial"] == {"disabled": True, "count": "0 / 600"}
    assert out["written"]["disabled"] is False and out["written"]["count"] == "49 / 600"
    assert out["atCap"] is False, "600 characters is allowed, as the service allows it"
    assert out["over"]["disabled"] is True and out["over"]["count"] == "601 / 600"
    assert "text-red-400" in out["over"]["tone"] and "not cut" in out["over"]["hint"]
    assert out["emoji"] == {"disabled": False, "count": "600 / 600"}, "Characters are counted as the service counts them"
    assert out["blank"] is True and out["sentWhenBlank"] == 0


def test_up_to_five_examples_each_with_a_counter_against_4000(tmp_path: Path) -> None:
    out = rules_page(
        r"""
  describe('Billing domain classes may not reach persistence.');
  for (let i = 0; i < 7; i++) addDraftExample();
  out.count = draftExamples.length;
  out.addDisabled = el('draft-add-example').disabled;
  out.label = el('draft-examples-count').innerText;
  el('draft-ex-path-0').value = ORDER;
  el('draft-ex-content-0').value = 'import javax.persistence.Entity;';
  el('draft-ex-path-1').value = 'src/main/java/com/acme/billing/domain/Money.java';
  el('draft-ex-expect-1').value = 'allow';
  el('draft-ex-path-2').value = 'src/main/java/com/acme/billing/domain/Tax.java';
  el('draft-ex-content-2').value = 'x'.repeat(4001);
  updateDraftCounters();
  out.overContent = { disabled: el('draft-button').disabled, count: el('draft-ex-count-2').innerText, hint: el('draft-hint').innerText };
  removeDraftExample(4); removeDraftExample(3); removeDraftExample(2);
  out.afterRemove = { count: draftExamples.length, addDisabled: el('draft-add-example').disabled, kept: draftExamples, disabled: el('draft-button').disabled };
  el('draft-ex-path-1').value = '';
  updateDraftCounters();
  out.noPath = { disabled: el('draft-button').disabled, hint: el('draft-hint').innerText };
""",
        tmp_path,
    )
    assert out["count"] == 5 and out["addDisabled"] is True and out["label"] == "5 / 5"
    assert out["overContent"]["disabled"] is True and out["overContent"]["count"] == "4001 / 4000"
    assert "Example 3 is 4001 characters" in out["overContent"]["hint"], "The first problem is the one named"
    kept = out["afterRemove"]
    assert kept["count"] == 2 and kept["addDisabled"] is False and kept["disabled"] is False
    assert kept["kept"][0] == {"path": "src/main/java/com/acme/billing/domain/Order.java", "content": "import javax.persistence.Entity;", "expect": "refuse"}
    assert kept["kept"][1]["expect"] == "allow", "Removing a row keeps what was chosen in the others"
    assert out["noPath"]["disabled"] is True and "Example 2 has no path" in out["noPath"]["hint"]


def test_the_project_defaults_to_the_one_in_the_address_and_follows_the_page_until_typed(tmp_path: Path) -> None:
    out = rules_page(
        r"""
  out.linked = el('draft-project').value;
  el('project-input').value = 'Acme-Ledger';
  chooseProject();
  await tick();
  out.followed = el('draft-project').value;
  el('draft-project').value = 'Acme-Payments';
  draftFieldChanged({ target: el('draft-project') });
  showSharedSet();
  await tick();
  out.kept = el('draft-project').value;
  el('draft-project').value = 'Globex-Billing';
  describe('Billing domain classes may not reach persistence.');
  out.foreign = { disabled: el('draft-button').disabled, hint: el('draft-hint').innerText };
""",
        tmp_path,
        before="location.search = '?project=Acme-Billing';",
    )
    assert out["linked"] == PROJECT
    assert out["followed"] == "Acme-Ledger"
    assert out["kept"] == "Acme-Payments", "Once typed in, the field is the reader's"
    assert out["foreign"]["disabled"] is True and "Acme-" in out["foreign"]["hint"]


# ------------------------------------------------------------------ drafting


def test_drafting_sends_the_sentence_project_and_examples_and_shows_the_observed_draft(tmp_path: Path) -> None:
    out = rules_page(
        r"""
  answer = api({ 'GET /rules': { status: 200, body: IN_FORCE }, 'POST /rules/draft': { status: 200, body: drafted() } });
  describe('  Billing domain classes may not reach persistence.  ');
  addDraftExample();
  el('draft-ex-path-0').value = '  ' + ORDER + ' ';
  el('draft-ex-content-0').value = 'import javax.persistence.Entity;';
  const pending = draftRule();
  out.loading = { result: el('draft-result').innerHTML, disabled: el('draft-button').disabled };
  await pending;
  await tick();
  out.request = posts('/rules/draft')[0];
  out.saves = posts('/rules').length;
  out.rule = JSON.parse(el('draft-rule').value);
  out.boxHidden = el('draft-box').hidden;
  out.mode = el('draft-mode').innerHTML;
  out.result = el('draft-result').innerHTML;
  out.validation = el('draft-validation').innerHTML;
  out.tried = el('draft-tried').innerHTML;
  out.saveDisabled = el('draft-save-button').disabled;
  out.note = el('draft-save-note').innerHTML;
""",
        tmp_path,
        before=SIGNED_IN + "location.search = '?project=Acme-Billing';",
    )
    assert 'data-state="loading"' in out["loading"]["result"] and out["loading"]["disabled"] is True
    request = out["request"]
    assert request["body"] == {
        "description": "Billing domain classes may not reach persistence.",
        "project": PROJECT,
        "examples": [{"path": "src/main/java/com/acme/billing/domain/Order.java", "content": "import javax.persistence.Entity;", "expect": "refuse"}],
    }
    assert request["headers"].get("Authorization") == "Bearer tok-1", "The draft carries the shared sign-in"
    assert out["saves"] == 0, "Asking for a draft saves nothing"
    assert out["rule"]["id"] == "billing-domain-stays-pure" and out["rule"]["mode"] == "observe"
    assert out["boxHidden"] is False
    assert "OBSERVE" in out["mode"]
    assert "Not saved" in out["result"] and "Saved nothing. The draft watches first." in out["result"]
    assert "it can be saved as written" in out["validation"]
    assert "1 of 2 behaved as expected" in out["tried"]
    assert 'data-result="matched"' in out["tried"] and 'data-result="mismatched"' in out["tried"]
    assert "your example" in out["tried"] and "built from the rule" in out["tried"]
    assert out["saveDisabled"] is False, "Signed in, with a draft for a project, the save is offered"
    assert "observes first" in out["note"] and "dashboard.html#/projects/Acme-Billing" in out["note"]


def test_the_models_words_are_shown_as_text_never_as_markup(tmp_path: Path) -> None:
    out = rules_page(
        r"""
  const hostile = drafted({
    rule: Object.assign({}, RULE, { id: EVIL, description: EVIL, mode: 'observe' }),
    model: EVIL, notes: [EVIL], warnings: [EVIL], project: EVIL,
    tried: [{ path: EVIL, content_preview: EVIL, expected: 'refuse', verdict: EVIL, matched: false, origin: 'caller', note: EVIL }]
  });
  const screens = {};
  const cases = {
    ok: { status: 200, body: hostile },
    unavailable: { status: 503, body: { type: 'urn:threefold:error:model-unavailable', detail: EVIL, source: 'unavailable', saved: false } },
    undraftable: { status: 502, body: { detail: EVIL, problems: [EVIL], rejected_draft: { id: EVIL, forbid_imports: [EVIL] } } },
    declined: { status: 422, body: { detail: EVIL, unsupported: EVIL } },
    refused: { status: 400, body: { detail: EVIL, invalid_params: [{ name: EVIL }] } },
    private: { status: 403, body: { detail: EVIL } }
  };
  describe('Billing domain classes may not reach persistence.');
  for (const name of Object.keys(cases)) {
    answer = api({ 'GET /rules': { status: 200, body: IN_FORCE }, 'POST /rules/draft': cases[name] });
    await draftRule();
    await tick();
    screens[name] = el('draft-result').innerHTML + el('draft-validation').innerHTML + el('draft-tried').innerHTML + el('draft-mode').innerHTML;
  }
  out.screens = screens;
  answer = api({ 'GET /rules': { status: 200, body: IN_FORCE }, 'POST /rules/draft': cases.ok });
  await draftRule();
  await tick();
  out.ruleText = el('draft-rule').value;
""",
        tmp_path,
        before="location.search = '?project=Acme-Billing';",
    )
    for name, markup in out["screens"].items():
        assert "<img" not in markup and "<svg" not in markup, f"The {name} answer was written as markup"
        assert "&lt;img" in markup, f"The {name} answer was not shown at all, so this test proves nothing there"
    assert "<img src=x" in out["ruleText"], "In the editor the rule is text, set as a value"


def test_a_model_that_is_unavailable_leaves_no_draft_on_the_page(tmp_path: Path) -> None:
    out = rules_page(
        r"""
  answer = api({ 'GET /rules': { status: 200, body: IN_FORCE }, 'POST /rules/draft': { status: 200, body: drafted() } });
  describe('Billing domain classes may not reach persistence.');
  await draftRule();
  await tick();
  out.before = { hidden: el('draft-box').hidden, rule: el('draft-rule').value !== '' };
  answer = api({ 'GET /rules': { status: 200, body: IN_FORCE }, 'POST /rules/draft': { status: 503, body: {
    type: 'urn:threefold:error:model-unavailable', title: 'Model Unavailable',
    detail: 'Amazon Bedrock could not be asked or did not answer, so no rule was drafted and nothing was saved.',
    source: 'unavailable', model: null, saved: false } } });
  await draftRule();
  await tick();
  out.after = { hidden: el('draft-box').hidden, rule: el('draft-rule').value, result: el('draft-result').innerHTML,
                saveDisabled: el('draft-save-button').disabled, retryState: lastDraft, draftEnabled: !el('draft-button').disabled };
  out.saves = posts('/rules').length;
""",
        tmp_path,
        before=SIGNED_IN + "location.search = '?project=Acme-Billing';",
    )
    assert out["before"] == {"hidden": False, "rule": True}
    after = out["after"]
    assert after["hidden"] is True and after["rule"] == "", "No draft is shown in place of the one the model did not write"
    assert "Amazon Bedrock is not available, so no rule was drafted" in after["result"]
    assert "No rule is shown in its place" in after["result"]
    assert after["saveDisabled"] is True and after["retryState"] is None
    assert after["draftEnabled"] is True, "The reader can ask again"
    assert out["saves"] == 0


def test_the_other_refusals_each_say_what_happened(tmp_path: Path) -> None:
    out = rules_page(
        r"""
  const cases = {
    undraftable: { status: 502, body: { type: 'urn:threefold:error:undraftable-rule', detail: 'The model answered 2 time(s) and no answer was a usable rule, so nothing was drafted.', problems: ['forbid_imports is empty'], rejected_draft: { id: 'x', forbid_imports: [] } } },
    declined: { status: 422, body: { type: 'urn:threefold:error:not-a-layering-rule', detail: 'A size limit is not an import.' } },
    refused: { status: 400, body: { detail: 'description must be at most 600 characters; it is 601.', invalid_params: [{ name: 'description' }] } },
    private: { status: 401, body: { detail: 'reading them requires the operator key' } },
    offline: 'network'
  };
  describe('Billing domain classes may not reach persistence.');
  out.screens = {};
  for (const name of Object.keys(cases)) {
    answer = api({ 'GET /rules': { status: 200, body: IN_FORCE }, 'POST /rules/draft': cases[name] === 'network' ? () => 'network' : cases[name] });
    await draftRule();
    await tick();
    out.screens[name] = { result: el('draft-result').innerHTML, hidden: el('draft-box').hidden };
  }
""",
        tmp_path,
    )
    screens = out["screens"]
    assert all(screen["hidden"] is True for screen in screens.values()), "No refusal leaves a draft to save"
    assert "not with a rule that can be used" in screens["undraftable"]["result"]
    assert "forbid_imports is empty" in screens["undraftable"]["result"]
    assert "not one a layering rule can say" in screens["declined"]["result"]
    assert "refused before the model was asked" in screens["refused"]["result"]
    assert "description" in screens["refused"]["result"]
    assert "needs the operator" in screens["private"]["result"] and "threefold.py open" in screens["private"]["result"]
    assert "could not be reached" in screens["offline"]["result"]


# ------------------------------------------------------------------ try again


def test_try_again_tries_the_edited_rule_with_explain_and_saves_nothing(tmp_path: Path) -> None:
    out = rules_page(
        r"""
  answer = api({
    'GET /rules': { status: 200, body: IN_FORCE },
    'POST /rules/draft': { status: 200, body: drafted() },
    'POST /rules/explain': (u, init, body) => {
      const fires = /persistence|sql/.test(body.content) && /billing\/domain/.test(body.path);
      return { status: 200, body: { verdict: fires ? 'OBSERVE' : 'ALLOW', violations: fires ? [{ mode: 'observe', reason: 'imports a forbidden module' }] : [], note: 'none', rules_considered: 'draft', imports: [], applicable_rules: [] } };
    }
  });
  describe('Billing domain classes may not reach persistence.');
  addDraftExample();
  el('draft-ex-path-0').value = ORDER;
  el('draft-ex-content-0').value = 'import javax.persistence.Entity;';
  await draftRule();
  await tick();
  const edited = Object.assign({}, RULE, { forbid_imports: ['javax.persistence', 'java.sql'] });
  el('draft-rule').value = JSON.stringify(edited);
  addDraftExample();
  el('draft-ex-path-1').value = 'src/main/java/com/acme/billing/domain/Ledger.java';
  el('draft-ex-content-1').value = 'import java.sql.Connection;';
  await retryDraft();
  await tick();
  out.explains = posts('/rules/explain').map(c => c.body);
  out.saves = posts('/rules').length;
  out.tried = el('draft-tried').innerHTML;
  out.validation = el('draft-validation').innerHTML;

  answer = api({ 'GET /rules': { status: 200, body: IN_FORCE }, 'POST /rules/explain': { status: 400, body: { detail: 'unusable', problems: [{ index: 0, id: 'billing-domain-stays-pure', reason: 'forbid_imports is empty' }] } } });
  el('draft-rule').value = JSON.stringify(Object.assign({}, RULE, { forbid_imports: [] }));
  await retryDraft();
  out.unusable = el('draft-validation').innerHTML;
  el('draft-rule').value = '{ not json';
  await retryDraft();
  out.notJson = el('draft-validation').innerHTML;
""",
        tmp_path,
    )
    explains = out["explains"]
    assert [body["path"] for body in explains] == [
        "src/main/java/com/acme/billing/domain/Order.java",
        "src/main/java/com/acme/billing/domain/Ledger.java",
        "src/billing/domain/acme/Example.java",
    ], "The example rows as they stand, then the file the service built from the draft"
    for body in explains:
        assert body["rules"] == [{**explains[0]["rules"][0]}] and body["rules"][0]["forbid_imports"] == ["javax.persistence", "java.sql"]
    assert out["saves"] == 0, "Trying a rule again saves nothing"
    assert "Tried again against your edited rule: 2 of 3 behaved as expected" in out["tried"]
    assert out["tried"].count('data-result="matched"') == 2, "The two files of the architect's own now behave"
    assert "Nothing was saved" in out["validation"]
    assert "cannot be used as written" in out["unusable"] and "forbid_imports is empty" in out["unusable"]
    assert "not valid JSON" in out["notJson"]


# ------------------------------------------------------------------ saving


def test_save_to_project_is_offered_only_with_a_sign_in_or_a_key(tmp_path: Path) -> None:
    out = rules_page(
        r"""
  answer = api({ 'GET /rules': { status: 200, body: IN_FORCE }, 'POST /rules/draft': { status: 200, body: drafted() } });
  out.beforeDraft = el('draft-save-button').disabled;
  describe('Billing domain classes may not reach persistence.');
  await draftRule();
  await tick();
  out.anonymous = { disabled: el('draft-save-button').disabled, hint: el('draft-save-hint').innerText };
  await saveDraftedRule();
  out.anonymousSaves = posts('/rules').length;
  el('api-key').value = 'typed-operator-key';
  draftFieldChanged({ target: el('api-key') });
  out.typed = el('draft-save-button').disabled;
  el('api-key').value = '';
  draftFieldChanged({ target: el('api-key') });
  store['threefold-operator-key'] = 'stored-operator-key';
  updateDraftSaveState();
  out.stored = el('draft-save-button').disabled;
""",
        tmp_path,
        before="location.search = '?project=Acme-Billing';",
    )
    assert out["beforeDraft"] is True, "There is nothing to save before a draft"
    assert out["anonymous"]["disabled"] is True and "threefold.py open" in out["anonymous"]["hint"]
    assert out["anonymousSaves"] == 0
    assert out["typed"] is False and out["stored"] is False


def test_a_draft_for_the_shared_set_cannot_be_saved_to_a_project(tmp_path: Path) -> None:
    out = rules_page(
        r"""
  answer = api({ 'GET /rules': { status: 200, body: IN_FORCE }, 'POST /rules/draft': { status: 200, body: drafted({ project: null }) } });
  describe('Billing domain classes may not reach persistence.');
  await draftRule();
  await tick();
  out.request = posts('/rules/draft')[0].body;
  out.disabled = el('draft-save-button').disabled;
  out.hint = el('draft-save-hint').innerText;
""",
        tmp_path,
        before=SIGNED_IN,
    )
    assert "project" not in out["request"]
    assert out["disabled"] is True and "Name a project" in out["hint"]


def test_save_to_project_sends_the_projects_rules_and_the_edited_rule_with_the_sign_in(tmp_path: Path) -> None:
    out = rules_page(
        r"""
  answer = api({
    'GET /rules': (u) => ({ status: 200, body: u.searchParams.get('project') === 'Acme-Billing' ? IN_FORCE : { rules: [], count: 0, is_default: true } }),
    'POST /rules/draft': { status: 200, body: drafted() },
    'POST /rules': (u, init, body) => ({ status: 200, body: { status: 'RULES_UPDATED', count: body.rules.length, refresh_seconds: 30 } })
  });
  describe('Billing domain classes may not reach persistence.');
  await draftRule();
  await tick();
  out.savesAfterDraft = posts('/rules').length;
  el('draft-rule').value = JSON.stringify(Object.assign({}, RULE, { description: 'Edited by the architect' }));
  const readsBefore = calls.length;
  await saveDraftedRule();
  await tick();
  const sequence = calls.slice(readsBefore);
  out.firstRead = { url: sequence[0].url, method: sequence[0].method, auth: sequence[0].headers.Authorization };
  out.save = posts('/rules')[0];
  out.result = el('draft-save-result').innerHTML;
""",
        tmp_path,
        before=SIGNED_IN + "location.search = '?project=Acme-Billing';",
    )
    assert out["savesAfterDraft"] == 0
    assert out["firstRead"] == {"url": "https://example.test/prod/rules?project=Acme-Billing", "method": "GET", "auth": "Bearer tok-1"}
    save = out["save"]
    assert save["headers"].get("Authorization") == "Bearer tok-1"
    assert save["body"]["project"] == PROJECT
    assert [rule["id"] for rule in save["body"]["rules"]] == ["shared-rule", "billing-domain-stays-pure"]
    assert save["body"]["rules"][-1]["description"] == "Edited by the architect", "The edited rule is saved, not the draft as it came"
    assert save["body"]["rules"][-1]["mode"] == "observe"
    result = out["result"]
    assert "Saved to Acme-Billing, in observe" in result and "refuses nothing yet" in result
    assert "no rules of its own" in result, "Saving beside the shared default says the project now keeps a copy of it"
    assert "dashboard.html#/projects/Acme-Billing" in result and "promote" in result


def test_a_rule_edited_out_of_observe_is_not_saved(tmp_path: Path) -> None:
    out = rules_page(
        r"""
  answer = api({ 'GET /rules': { status: 200, body: IN_FORCE }, 'POST /rules/draft': { status: 200, body: drafted() } });
  describe('Billing domain classes may not reach persistence.');
  await draftRule();
  await tick();
  el('draft-rule').value = JSON.stringify(Object.assign({}, RULE, { mode: 'enforce' }));
  await saveDraftedRule();
  out.enforce = el('draft-save-result').innerHTML;
  const withoutMode = Object.assign({}, RULE); delete withoutMode.mode;
  el('draft-rule').value = JSON.stringify(withoutMode);
  await saveDraftedRule();
  out.missing = el('draft-save-result').innerHTML;
  out.saves = posts('/rules').length;
""",
        tmp_path,
        before=SIGNED_IN + "location.search = '?project=Acme-Billing';",
    )
    assert "Nothing was sent" in out["enforce"] and "enforce" in out["enforce"]
    assert "Nothing was sent" in out["missing"] and "which means enforce" in out["missing"]
    assert out["saves"] == 0


def test_a_save_that_cannot_read_the_projects_rules_first_sends_nothing(tmp_path: Path) -> None:
    """POST /rules replaces a whole set, so the draft alone would take every other rule away."""
    out = rules_page(
        r"""
  answer = api({ 'GET /rules': { status: 200, body: IN_FORCE }, 'POST /rules/draft': { status: 200, body: drafted() } });
  describe('Billing domain classes may not reach persistence.');
  await draftRule();
  await tick();
  answer = api({ 'GET /rules': { status: 403, body: { detail: 'private' } } });
  await saveDraftedRule();
  out.result = el('draft-save-result').innerHTML;
  out.saves = posts('/rules').length;
  answer = api({ 'GET /rules': { status: 200, body: IN_FORCE }, 'POST /rules': { status: 400, body: { detail: 'unusable', problems: [{ index: 1, id: 'billing-domain-stays-pure', reason: 'id "billing-domain-stays-pure" is used by an earlier rule' }] } } });
  await saveDraftedRule();
  out.clash = el('draft-save-result').innerHTML;
""",
        tmp_path,
        before=SIGNED_IN + "location.search = '?project=Acme-Billing';",
    )
    assert "could not be read" in out["result"] and "nothing was sent" in out["result"]
    assert out["saves"] == 0
    assert "Nothing was saved" in out["clash"] and "is used by an earlier rule" in out["clash"]


# ------------------------------------------------------------ the way in


def test_the_project_page_links_to_drafting_a_rule_for_that_project(tmp_path: Path) -> None:
    out = run(
        "dashboard.html",
        r"""
  const detail = name => ({ status: 200, body: { project: name, config: null, readiness: { summary: { stage: 'observe' }, rules: [] } } });
  answer = api({
    '/api/projects/Acme-Billing': detail('Acme-Billing'),
    '/api/projects/unlabelled': detail('unlabelled'),
    '/api/decisions': { status: 200, body: { items: [], next_cursor: null } }
  });
  await visit('#/projects/Acme-Billing');
  out.named = el('view').innerHTML;
  await visit('#/projects/unlabelled');
  out.unlabelled = el('view').innerHTML;
""",
        tmp_path,
    )
    link = re.search(r'<a class="tf-link" data-link="draft-rule" href="([^"]+)">([^<]+)</a>', out["named"])
    assert link, "The project page offers no way to draft a rule for the project"
    assert link.group(1) == "https://example.test/prod/rules.html?project=Acme-Billing#draft"
    assert link.group(2) == "Draft a rule for this project"
    assert 'data-link="draft-rule"' not in out["unlabelled"], (
        "A name outside the pattern would open the rules page on the shared set, not on this project"
    )
