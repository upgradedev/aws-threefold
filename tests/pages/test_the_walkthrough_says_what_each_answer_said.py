"""The walkthrough's last step, its finish and its frame say what the service answered, and only that.

The final panel sent the walkthrough's call within seconds of the promotion
and was answered APPROVED in stage Observe. The step then put the rule's
refusal sentence beside VERDICT APPROVED, offered no way to send the call
again, and the finish labelled that answer "the same call, in Enforce". The
answers these tests replay are the real service's, made here through the
handler: a sandbox's call before its promotion (the answer a stale stage
gives), after it, and in Enforce under a rule still observed. The same
review found the skip link routed a keyboard reader to "Not found", a
sandbox could be made in silence for twelve seconds, the finish's lead read
as if four things took one click, a verdict's full stop wrapped alone, the
sticky bar sat over what the keyboard reached, and a checked fix whose
adapter only throws was not called a skeleton.

The page's own script runs under Node with the stub browser in _browser.py;
these tests skip where Node is absent.
"""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from _browser import page_source, run

from threefold.interfaces.api_handlers import lambda_handler

RULE = "web-domain-stays-pure"
# The call step 5 sends, as the page sends it: a hook's call that asks for the explanation.
CALL = {
    "developer": "anonymous", "developer_id": "anonymous", "tool_name": "Write", "action_type": "FILE_WRITE",
    "agent": "claude-code", "origin": "hook", "explain": True, "hook_mode": "managed",
    "arguments": {"file_path": "src/web/domain/cart.ts", "content": "import axios from 'axios';\n"},
}


def _post(path: str, body: dict, index: int) -> dict:
    event = {
        "rawPath": f"/prod{path}",
        "requestContext": {"http": {"method": "POST", "sourceIp": f"10.8.8.{index}"}, "stage": "prod"},
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
    response = lambda_handler(event, None)
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"] or "{}")


def _answers(monkeypatch) -> dict:
    """What the service answers the walkthrough's call: before a promotion, after it, and with the rule left observing."""
    monkeypatch.delenv("DEFAULT_HOOK_STAGE", raising=False)
    # A session of its own for each call: the stack keeps sessions for the whole
    # module, and the same call again in one session is a loop.
    run_id = secrets.token_hex(4)
    first = _post("/api/sandbox", {}, 1)["project"]
    observe = _post("/evaluate-tool-call", dict(CALL, project_name=first, session_id=f"try-{run_id}-a1"), 2)
    _post(f"/api/projects/{first}/promote", {"enforce": [RULE]}, 3)
    refused = _post("/evaluate-tool-call", dict(CALL, project_name=first, session_id=f"try-{run_id}-a2"), 4)
    second = _post("/api/sandbox", {}, 5)["project"]
    _post(f"/api/projects/{second}/promote", {"enforce": ["python-domain-stays-pure"]}, 6)
    still_observed = _post("/evaluate-tool-call", dict(CALL, project_name=second, session_id=f"try-{run_id}-a3"), 7)
    plain = _post("/evaluate-tool-call", dict(CALL, project_name=second, session_id=f"try-{run_id}-a4", tool_name="Read",
                                              action_type="FILE_READ", arguments={"file_path": "README.md"}), 8)
    # The shapes the page is written against; if the service changes them, this says so first.
    assert observe["status"] == "APPROVED" and observe["project_stage"] == "observe" and observe["observed_rules"] == [RULE]
    assert refused["status"] == "BLOCKED_BOUNDARY_VIOLATION" and refused["project_stage"] == "enforce"
    assert still_observed["status"] == "APPROVED" and still_observed["project_stage"] == "enforce" and still_observed["observed_rules"] == [RULE]
    assert plain["status"] == "APPROVED" and plain["project_stage"] == "enforce" and not plain["observed_rules"]
    return {"observe": observe, "refused": refused, "still_observed": still_observed, "plain": plain}


# Step 5 set up as a promotion answered with Enforce leaves it, with the write
# the rule forbids planned; every answer the scenario queues is sent back in turn.
STEP_FIVE = r"""
const P = 'Acme-Sandbox-0a1b2c3d';
const words = markup => String(markup).replace(/<wbr>/g, '').replace(/<[^>]+>/g, ' ').replace(/&#039;/g, "'").replace(/\s+/g, ' ').trim();
const queued = [];
async function atStepFive(answers) {
  queued.push.apply(queued, answers);
  answer = api({ 'POST /evaluate-tool-call': () => ({ status: 200, body: queued.shift() }) });
  await visit('#/try');
  Object.assign(Dash.tryState, {
    project: P, step: 5, planned: true, seeded: 12, calls: [],
    promoted: { stage: 'enforce', answered: true, enforce: ['web-domain-stays-pure'], observe: [] },
    replay: { rule: 'web-domain-stays-pure', from: 'rule', verdict_id: null, agent: 'claude-code', tool_name: 'Write', action_type: 'FILE_WRITE',
      imports: 'axios', arguments: { file_path: 'src/web/domain/cart.ts', content: "import axios from 'axios';\n" } }
  });
}
"""


def text_of(markup: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup.replace("<wbr>", ""))).replace("&#039;", "'").strip()


def test_an_answer_judged_in_observe_is_worded_in_observe_and_can_be_sent_again(tmp_path: Path, monkeypatch) -> None:
    answers = _answers(monkeypatch)
    out = run(
        "dashboard.html",
        STEP_FIVE
        + "const OBSERVE = " + json.dumps(answers["observe"]) + ";\n"
        + "const REFUSED = " + json.dumps(answers["refused"]) + ";\n"
        + r"""
  await atStepFive([OBSERVE, REFUSED]);
  await click('try-send'); await tick();
  out.approved = el('view').innerHTML;
  await click('try-finish'); await tick();
  out.finishApproved = words(el('view').innerHTML);
  Dash.tryState.step = 5;
  await click('try-send'); await tick();
  out.refused = words(el('view').innerHTML);
  out.sent = calls.filter(c => c.url.indexOf('/evaluate-tool-call') !== -1).length;
  await click('try-finish'); await tick();
  out.finish = words(el('view').innerHTML);
""",
        tmp_path,
    )
    approved = text_of(out["approved"])
    # Every label is built from the verdict and the stage the answer reports.
    assert "Step 5 of 5 · approved" in approved and "Approved in Observe" in approved
    assert "Now, in Observe · approved" in approved
    assert "in Enforce ·" not in approved and "Refused" not in approved and "may not be in force" not in approved
    # What the answer means, in its own terms: the stage it reports against the one the promotion answered.
    assert "The answer reports your sandbox in Observe, though the promotion was answered with Enforce." in approved
    assert "In Observe a rule records a call it would refuse and lets it run: web-domain-stays-pure recorded this write." in approved
    # The rule's sentence is said as what it would have refused, never beside APPROVED as if it had.
    assert "Recorded, not refused" in approved
    assert "It would have been refused: A TypeScript module under domain/ may not import a client or a framework." in approved
    assert 'data-tone="observed"' in out["approved"]
    # Sending it again is offered beside the way on to the summary.
    assert re.search(r'id="try-primary"[^>]*data-action="try-send"[^>]*>Send it again', out["approved"]), "No way to send the call again"
    assert 'data-action="try-finish"' in out["approved"] and "See what just happened" in approved

    # The finish after that answer names the stage it reports.
    assert "Approved the same kind of call, in Observe" in out["finishApproved"]
    assert "Approved in Observe, and recorded as a call web-domain-stays-pure would have refused" in out["finishApproved"]
    assert "the same kind of call, in Enforce" not in out["finishApproved"]

    # The second answer, refused in Enforce, and the first kept beside it.
    assert out["sent"] == 2
    assert "Refused, before it ran" in out["refused"] and "Now, in Enforce · refused" in out["refused"]
    assert "Sent 2 times: APPROVED in Observe, then BLOCKED_BOUNDARY_VIOLATION in Enforce." in out["refused"]
    assert "Refused the same kind of call, in Enforce" in out["finish"]
    assert "Its verdict: BLOCKED_BOUNDARY_VIOLATION. Sent 2 times; the answer before was APPROVED in Observe." in out["finish"]


def test_an_approval_in_enforce_says_which_rule_still_observes_or_that_none_flagged_it(tmp_path: Path, monkeypatch) -> None:
    answers = _answers(monkeypatch)
    no_stage = {key: value for key, value in answers["observe"].items() if key != "project_stage"}
    out = run(
        "dashboard.html",
        STEP_FIVE
        + "const QUEUE = " + json.dumps([answers["still_observed"], answers["plain"], no_stage]) + ";\n"
        + r"""
  await atStepFive(QUEUE);
  await click('try-send'); await tick();
  out.stillObserved = words(el('view').innerHTML);
  await click('try-send'); await tick();
  out.plain = el('view').innerHTML;
  await click('try-send'); await tick();
  out.noStage = words(el('view').innerHTML);
""",
        tmp_path,
    )
    assert "Approved in Enforce" in out["stillObserved"]
    assert "The answer reports Enforce, and names web-domain-stays-pure as a rule still observed in this project: it recorded this write and let it run." in out["stillObserved"]
    assert "It would have been refused: A TypeScript module" in out["stillObserved"]

    plain = text_of(out["plain"])
    assert "Approved in Enforce" in plain and "The answer reports Enforce, and no rule flagged this write." in plain
    assert "Approved: no rule flagged it" in plain and "It would have been refused" not in plain
    assert 'data-tone="approved"' in out["plain"]

    assert "Step 5 of 5 · approved Approved " in out["noStage"], "An answer with no stage is given none"
    assert "The answer reports no stage; it names web-domain-stays-pure as a rule that would have refused this write, and let it run." in out["noStage"]
    assert "Now · approved" in out["noStage"]
    assert "Sent 3 times: APPROVED in Enforce, then APPROVED in Enforce, then APPROVED." in out["noStage"]


def test_a_checked_fix_whose_adapter_only_throws_is_called_a_skeleton(tmp_path: Path, monkeypatch) -> None:
    """The fix the service proposes for this refusal passes every gate, and its adapter's body throws.

    The label is read from the files the fix proposes, so it is checked here
    against what the proposer really writes, not against a hand-made stub.
    """
    answers = _answers(monkeypatch)
    fix = answers["refused"]["suggested_fix"]
    assert fix["validated"] is True
    stubbed = [w["path"] for w in fix["writes"] if 'throw new Error("Call axios here")' in w["content"]]
    assert stubbed == ["src/web/infrastructure/cart.adapter.ts"], "The proposer's adapter changed; read the label's pattern again"
    whole = dict(fix, writes=[dict(w, content="export class CartAdapter {}\n") if w["path"] in stubbed else w for w in fix["writes"]])
    unchecked = dict(fix, validated=False)
    queue = [answers["refused"], dict(answers["refused"], suggested_fix=whole), dict(answers["refused"], suggested_fix=unchecked)]
    out = run(
        "dashboard.html",
        STEP_FIVE
        + "const QUEUE = " + json.dumps(queue) + ";\n"
        + r"""
  await atStepFive(QUEUE);
  await click('try-send'); await tick();
  out.skeleton = words(el('view').innerHTML);
  await click('try-finish'); await tick();
  out.finish = words(el('view').innerHTML);
  Object.assign(Dash.tryState, { step: 5, result: null });
  await click('try-send'); await tick();
  out.whole = words(el('view').innerHTML);
  Dash.tryState.result = null;
  await click('try-send'); await tick();
  out.unchecked = words(el('view').innerHTML);
""",
        tmp_path,
    )
    skeleton = out["skeleton"]
    assert "Checked by Threefold: passes the same gates A skeleton: the gates pass; the body is yours to write" in skeleton
    assert "src/web/infrastructure/cart.adapter.ts · a skeleton: its body throws until you write it" in skeleton
    assert "with a skeleton fix checked by Threefold" in out["finish"]
    assert "Checked by Threefold: passes the same gates" in out["whole"] and "skeleton" not in out["whole"]
    assert "A skeleton: the body is yours to write" in out["unchecked"] and "the gates pass" not in out["unchecked"]


def test_the_finish_says_what_the_reader_did_and_keeps_a_verdict_s_full_stop_on_its_line(tmp_path: Path, monkeypatch) -> None:
    answers = _answers(monkeypatch)
    out = run(
        "dashboard.html",
        STEP_FIVE
        + "const REFUSED = " + json.dumps(answers["refused"]) + ";\n"
        + r"""
  await atStepFive([REFUSED]);
  Object.assign(Dash.tryState, { observed: [{ verdict_id: 'V-1', rule_key: 'web-domain-stays-pure' }], labels: { 'V-1': 'correct' } });
  await click('try-send'); await tick();
  await click('try-finish'); await tick();
  out.labelled = el('view').innerHTML;
  Object.assign(Dash.tryState, { step: 5, observed: [], labels: {} });
  await click('try-finish'); await tick();
  out.unlabelled = words(el('view').innerHTML);
""",
        tmp_path,
    )
    labelled = text_of(out["labelled"])
    assert "You watched, labelled and promoted; if a rule bites wrong, one click demotes the project to Observe. Here is what the service answered." in labelled
    assert "Watch, label, promote, and demote in one click" not in labelled
    assert "You watched and promoted; if a rule bites wrong" in out["unlabelled"], "Nothing flagged is nothing labelled"
    # The token may break after an underscore; its last part and the stop are one run of text.
    assert '<span class="tf-mono">BLOCKED_<wbr>BOUNDARY_<wbr>VIOLATION.</span>' in out["labelled"]


# ------------------------------------------------------------------ skip link

WALK_TO_TWO = r"""
const P = 'Acme-Sandbox-0a1b2c3d';
const flagged = { verdict_id: 'V-1', timestamp: new Date().toISOString(), project_name: P, agent: 'codex', tool_name: 'apply_patch', action_type: 'FILE_WRITE',
  status: 'APPROVED', rule_key: 'python-domain-stays-pure', observed_rules: ['python-domain-stays-pure'], observed_target: 'src/acme/domain/order.py',
  observed_reason: 'A Python file under domain/ may not import infrastructure or a driver.', stage: 'observe', review: null };
answer = api({
  'POST /api/sandbox': { status: 200, body: { project: P, calls_seeded: 12 } },
  '/api/decisions': { status: 200, body: { items: [flagged], next_cursor: null } },
  ['/api/projects/' + P]: { status: 200, body: { project: P, config: { stage: 'observe' }, readiness: { rules: [] } } },
  '/rules': { status: 200, body: { rules: [] } }
});
"""


def test_the_skip_link_moves_the_focus_to_the_screen_and_never_routes(tmp_path: Path) -> None:
    out = run(
        "dashboard.html",
        WALK_TO_TWO
        + r"""
  await visit('#/try');
  await click('try-create'); await tick();
  await click('try-show'); await tick();
  const state = Dash.tryState;
  let prevented = false;
  el('tf-skip').listeners.click({ preventDefault() { prevented = true; } });
  await tick();
  out.clicked = { prevented, hash: location.hash, focused: document.activeElement === el('view'), same: Dash.tryState === state, step: Dash.tryState.step, title: document.title };
  out.clickedView = el('view').innerHTML;
  document.activeElement = null;
  await visit('#view');
  out.visited = { hash: location.hash, focused: document.activeElement === el('view'), same: Dash.tryState === state, step: Dash.tryState.step, title: document.title };
  out.visitedView = el('view').innerHTML;
  await visit('#/projects');
  prevented = false;
  document.activeElement = null;
  el('tf-skip').listeners.click({ preventDefault() { prevented = true; } });
  await tick();
  out.projects = { prevented, hash: location.hash, focused: document.activeElement === el('view'), title: document.title };
""",
        tmp_path,
    )
    for moment in ("clicked", "visited"):
        seen = out[moment]
        assert seen["hash"] == "#/try" and seen["focused"] is True, seen
        assert seen["same"] is True and seen["step"] == 2, "The walkthrough was drawn again from the start"
        assert seen["title"].startswith("Try it"), seen
    assert out["clicked"]["prevented"] is True, "The link's own jump would have reached the router"
    for markup in (out["clickedView"], out["visitedView"]):
        assert "would have been refused" in markup and "Nothing lives at this address" not in markup
    projects = out["projects"]
    assert projects["prevented"] is True and projects["hash"] == "#/projects" and projects["focused"] is True
    assert projects["title"].startswith("Projects")


def test_a_page_opened_at_the_skip_link_s_address_draws_its_first_screen(tmp_path: Path) -> None:
    out = run(
        "dashboard.html",
        r"""
  out.title = document.title;
  out.hash = location.hash;
  out.view = el('view').innerHTML;
""",
        tmp_path,
        before="openAt('#view');\n",
    )
    assert out["title"].startswith("Overview") and out["hash"] == ""
    assert "Nothing lives at this address" not in out["view"]


def test_the_skip_link_stays_in_the_markup_so_it_works_without_script() -> None:
    body = page_source("dashboard.html")
    assert '<a id="tf-skip" class="tf-skip" href="#view">Skip to content</a>' in body
    assert '<div id="view" tabindex="-1"' in body


# ------------------------------------------------------------ making a sandbox


def test_making_a_sandbox_counts_the_seconds_and_keeps_the_button_busy_and_focused(tmp_path: Path) -> None:
    out = run(
        "dashboard.html",
        r"""
  const P = 'Acme-Sandbox-0a1b2c3d';
  const hold = held();
  answer = api({
    'POST /api/sandbox': () => hold.promise,
    '/api/decisions': { status: 200, body: { items: [], next_cursor: null } }
  });
  await visit('#/try');
  let now = 1790000000000;
  Date.now = () => now;
  const idle = intervals.size;
  el('try-primary').focus();
  const making = click('try-create');
  await tick();
  out.busy = el('view').innerHTML;
  out.live = el('live-status').textContent;
  out.focused = document.activeElement === el('try-primary');
  out.counting = intervals.size - idle;
  now += 7400;
  await runIntervals();
  out.waited = el('try-waited').textContent;
  click('try-create');
  await tick();
  out.posts = calls.filter(c => c.method === 'POST' && c.url.indexOf('/api/sandbox') !== -1).length;
  hold.release({ status: 200, body: { project: P, calls_seeded: 12 } });
  await making; await tick();
  out.after = intervals.size - idle;
  out.done = el('view').innerHTML;

  // A failure stops the clock too.
  await click('try-restart'); await tick();
  answer = api({ 'POST /api/sandbox': { status: 500, body: { detail: 'The stack could not make a sandbox.' } } });
  await click('try-create'); await tick();
  out.afterFailure = intervals.size - idle;
""",
        tmp_path,
    )
    busy = out["busy"]
    button = re.search(r'<button[^>]*id="try-primary"[^>]*>', busy).group(0)
    assert 'aria-busy="true"' in button and 'aria-disabled="true"' in button
    assert " disabled" not in button.replace('aria-disabled="true"', ""), "A disabled button drops the keyboard to the page"
    assert "Making your sandbox…" in busy
    assert '<b id="try-waited">0 s</b> so far. The stack answers once it has made your project and judged every seeded call.' in busy
    assert out["live"].startswith("Making your sandbox.")
    assert out["focused"] is True
    assert out["counting"] == 1
    assert out["waited"] == "7 s", "The seconds since the request left, whole"
    assert out["posts"] == 1, "A second press while it works sends nothing"
    assert out["after"] == 0 and out["afterFailure"] == 0, "The clock outlived the request"
    assert "12 calls arrived" in out["done"] and "try-waited" not in out["done"]


def test_the_clock_writes_the_seconds_and_draws_nothing() -> None:
    """The one timer the walkthrough holds lives outside its route and only writes a count."""
    body = page_source("dashboard.html")
    clock = body.split("function tryClock(id, since) {", 1)[1].split("\n    }\n", 1)[0]
    assert "setInterval(" in clock and "clearInterval(" in clock and ".textContent = tryWaited(since)" in clock
    assert "draw(" not in clock and "render(" not in clock and "innerHTML" not in clock
    waited = body.split("function tryWaited(since) {", 1)[1].split("\n    }\n", 1)[0]
    assert "Math.floor((Date.now() - since) / 1000)" in waited, "A count of whole seconds waited, never an estimate of what is left"


# ------------------------------------------------------------ room for the bar


def test_the_stage_keeps_room_for_the_sticky_bar_and_the_rail_while_the_walkthrough_is_open(tmp_path: Path) -> None:
    body = page_source("dashboard.html")
    css = body.split("const TRY_CSS = `", 1)[1].split("`;", 1)[0]
    assert ".tf-try-stage * { scroll-margin: calc(var(--tf-header-h) + var(--tf-try-rail-h, 0px) + 8px) 0 calc(var(--tf-try-bar-h, 0px) + 8px); }" in css
    assert ".tf-try-stage .tf-try-actions * { scroll-margin-bottom: 0; }" in css, "Reaching the bar's own buttons would scroll the page"
    out = run(
        "dashboard.html",
        r"""
  const beforeTry = new Set(winListeners.resize || []);
  await visit('#/try');
  const added = (winListeners.resize || []).filter(f => !beforeTry.has(f));
  out.step1 = el('view').innerHTML;
  out.room = Object.assign({}, document.documentElement.style.props);
  height.bar = 131;
  added.forEach(f => f({}));
  out.resized = document.documentElement.style.props['--tf-try-bar-h'];
  await visit('#/projects');
  out.left = Object.assign({}, document.documentElement.style.props);
  out.added = added.length;
  out.stillListening = added.filter(f => (winListeners.resize || []).indexOf(f) !== -1).length;
""",
        tmp_path,
        before=r"""
  document.documentElement = { style: { props: {}, setProperty(k, v) { this.props[k] = String(v); }, removeProperty(k) { delete this.props[k]; } } };
  const height = { bar: 92.4, rail: 47.2 };
  el('try-actions').getBoundingClientRect = () => ({ height: height.bar });
  el('try-rail').getBoundingClientRect = () => ({ height: height.rail });
  globalThis.getComputedStyle = node => ({ position: node && node.id === 'try-rail' ? 'sticky' : 'static', getPropertyValue: () => '' });
""",
    )
    assert out["room"] == {"--tf-try-bar-h": "93px", "--tf-try-rail-h": "48px"}
    assert out["resized"] == "131px", "A change of width measures the bar again"
    assert out["added"] == 1
    assert out["left"] == {} and out["stillListening"] == 0, "The room and its listener outlive the walkthrough"
    # The legend is read before the lanes it explains, so no bar sits over it on a desk's first screen.
    step1 = out["step1"]
    assert step1.index('class="tf-try-legend"') < step1.index('class="tf-try-lanes')
