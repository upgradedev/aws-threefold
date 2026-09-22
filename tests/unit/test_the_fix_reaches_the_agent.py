"""The evaluator attaches a checked fix to the verdicts someone will read.

A refusal is read by the hook, which puts the fix's summary in its deny reason,
and by the pages, which show all of it. An observation is read only on a page:
in Observe a hook prints nothing, so a fix computed there would cost time on
every call and reach nobody. The fix rides on the verdict and never on the
ledger, whose rows anyone reads on the public stack; the ledger keeps what kind
of fix was offered and whether the gates passed it. And a fix is paid for on
the verdict's clock, so what it may add is held to the gate's own budget, by a
ceiling on what the call carries that depends on what the fix has to do.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List

import pytest

from threefold.application import evaluator as evaluator_module
from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import (
    FIX_ADVICE_MAX_CHARS,
    FIX_CREDENTIAL_MAX_CHARS,
    FIX_LAYERING_ADVICE_MAX_CHARS,
    FIX_REWRITE_MAX_CHARS,
    GovernanceEvaluator,
    carries_more_than,
    fix_max_chars,
    fix_options,
    runs_a_command,
)
from threefold.domain.boundary_guard import ArchitecturalBoundaryGuard, iter_string_leaves
from threefold.domain.layering_rules import violations
from threefold.domain.models import ToolActionType, ToolInvocation

PROJECT = "Acme-Fixes"
DOMAIN_PATH = "src/acme_orders/domain/acme_order.py"
DOMAIN_WRITE = "import boto3\n\n\nclass AcmeOrder:\n    total = 0\n"


def _request(session: str, arguments: Dict[str, Any], **overrides: Any) -> ToolCallRequestDTO:
    fields: Dict[str, Any] = dict(
        session_id=session,
        developer_id="c0ffee000001",
        project_name=PROJECT,
        tool_name="Write",
        action_type="FILE_WRITE",
        arguments=arguments,
        projected_input_tokens=0,
        projected_output_tokens=0,
        agent="claude-code",
        origin="hook",
        explain=False,
        hook_mode="managed",
    )
    fields.update(overrides)
    return ToolCallRequestDTO(**fields)


def _domain_write(content: str = DOMAIN_WRITE, path: str = DOMAIN_PATH) -> Dict[str, Any]:
    return {"file_path": path, "content": content}


def _python_domain_file(size: int, head: str = "import boto3\n") -> str:
    """A Python file of about `size` characters beginning with `head`, built rather than spelled out.

    With the default head it is a domain file the layering rule refuses.
    """
    parts = [head]
    length = len(parts[0])
    index = 0
    while True:
        part = f"\n\ndef acme_rule_{index}(order):\n    return order.total * {index}\n"
        if length + len(part) > size:
            break
        parts.append(part)
        length += len(part)
        index += 1
    return "".join(parts)


def _carried(arguments: Dict[str, Any]) -> int:
    return sum(len(leaf) for leaf in iter_string_leaves(arguments))


def _passes_the_gate(write: Dict[str, Any], rules: List[Dict[str, Any]]) -> bool:
    if write.get("old_string") is not None:
        tool, arguments = "Edit", {"file_path": write["path"], "old_string": write["old_string"], "new_string": write["content"]}
    else:
        tool, arguments = "Write", {"file_path": write["path"], "content": write["content"]}
    allowed, _ = ArchitecturalBoundaryGuard.evaluate_tool_boundary(
        ToolInvocation(tool_name=tool, action_type=ToolActionType.FILE_WRITE, arguments=arguments), rules=rules
    )
    found, _ = violations(write["path"], write["content"], rules)
    return allowed and not found


@pytest.fixture
def evaluator() -> GovernanceEvaluator:
    return GovernanceEvaluator()


@pytest.fixture
def recorded(evaluator, monkeypatch) -> List[Dict[str, Any]]:
    """Every row the evaluator hands the ledger, exactly as it hands it over."""
    rows: List[Dict[str, Any]] = []
    real = evaluator.session_repo.record_decision

    def record(decision: Dict[str, Any]) -> bool:
        rows.append(json.loads(json.dumps(decision, default=str)))
        return real(decision)

    monkeypatch.setattr(evaluator.session_repo, "record_decision", record)
    return rows


@pytest.fixture
def proposals(monkeypatch) -> List[Dict[str, Any]]:
    """Every call the evaluator makes to the proposer, with what it passed."""
    calls: List[Dict[str, Any]] = []
    real = evaluator_module.propose_fix

    def spy(request: Any, result: Any, rules: Any = None, **options: Any) -> Any:
        calls.append({"request": request, "result": result, "rules": rules, "options": options})
        return real(request, result, rules, **options)

    monkeypatch.setattr(evaluator_module, "propose_fix", spy)
    return calls


# ---------------------------------------------------------------- refusals


def test_a_refused_write_carries_a_validated_fix_whose_writes_pass_the_gate(evaluator) -> None:
    result = evaluator.evaluate_tool_call(_request("fix-refused", _domain_write()))
    assert result.status == "BLOCKED_BOUNDARY_VIOLATION"
    fix = result.suggested_fix
    assert fix is not None and fix["kind"] == "layering"
    assert fix["validated"] is True
    assert fix["writes"], "A small refused Write is sent the files it should write instead"
    rules, _ = evaluator.rules_in_force(PROJECT)
    for write in fix["writes"]:
        assert _passes_the_gate(write, rules), f"The validated fix is itself refused at {write['path']}"
    assert fix["checks"] and all(check["passed"] for check in fix["checks"])
    assert result.to_dict()["suggested_fix"] == fix, "The fix reaches the response through to_dict"


def test_the_fix_is_checked_against_the_rules_that_judged_the_call(evaluator, proposals) -> None:
    rules, _ = evaluator.rules_in_force(PROJECT)
    evaluator.evaluate_tool_call(_request("fix-rules", _domain_write()))
    (call,) = proposals
    assert call["rules"] == rules
    assert "phrase" not in call["options"], "No model phrases a fix on the verdict's path"


def test_every_kind_of_refusal_is_sent_its_own_fix(evaluator) -> None:
    secret = evaluator.evaluate_tool_call(
        _request("fix-secret", {"command": "export AWS_ACCESS_KEY_ID=" + "AKIA" + "IOSFODNN7EXAMPLE"}, tool_name="Bash", action_type="COMMAND_EXEC")
    )
    assert secret.status == "BLOCKED_SECRET_DETECTED"
    assert secret.suggested_fix["kind"] == "credential"
    assert "AKIA" + "IOSFODNN7EXAMPLE" not in json.dumps(secret.suggested_fix), "A fix never repeats the credential"

    loop = _request("fix-loop", {"command": "npm run build"}, tool_name="Bash", action_type="COMMAND_EXEC")
    results = [evaluator.evaluate_tool_call(loop) for _ in range(3)]
    assert results[-1].status == "BLOCKED_LOOP_DETECTED"
    assert results[-1].suggested_fix["kind"] == "loop"
    assert results[-1].suggested_fix["validated"] is False, "No write fixes a loop, so none is claimed"
    assert all(result.suggested_fix is None for result in results[:2]), "An approval is sent nothing"


def test_a_call_into_a_halted_session_is_told_how_it_resumes(evaluator) -> None:
    page = dict(origin="page", agent="page", explain=True)
    loop = _request("sim-fix-halted", {"file_path": "src/acme/service.py", "instruction": "fix typo"}, tool_name="edit_file", **page)
    for _ in range(3):
        evaluator.evaluate_tool_call(loop)
    after = evaluator.evaluate_tool_call(_request("sim-fix-halted", {"path": "README.md"}, tool_name="view_file", action_type="FILE_READ", **page))
    assert after.status == "BLOCKED_CIRCUIT_BREAKER"
    assert after.suggested_fix["kind"] == "halted_session"


# ---------------------------------------------------------------- observations


def test_a_hooks_observation_carries_no_fix_and_costs_none(evaluator, proposals, monkeypatch) -> None:
    """A hook sends explain false and prints nothing on approval, so a fix would reach nobody."""
    monkeypatch.setenv("DEFAULT_HOOK_STAGE", "observe")
    observed = evaluator.evaluate_tool_call(_request("fix-hook-observe", _domain_write()))
    assert observed.status == "APPROVED" and observed.observed_rules
    assert observed.suggested_fix is None
    dry = evaluator.evaluate_tool_call(_request("fix-hook-dry", _domain_write(), dry_run=True, hook_mode="observe"))
    assert dry.status == "APPROVED" and dry.observed_rules and dry.suggested_fix is None
    assert proposals == [], "The proposer was not even asked"


def test_a_rule_the_project_still_observes_sends_a_hook_no_fix(evaluator) -> None:
    evaluator.save_project_config(PROJECT, {"stage": "enforce", "observe_rules": ["python-domain-stays-pure"]})
    observed = evaluator.evaluate_tool_call(_request("fix-observe-rule", _domain_write()))
    assert observed.status == "APPROVED" and observed.observed_rules
    assert observed.suggested_fix is None


def test_a_pages_observation_carries_the_fix(evaluator) -> None:
    """A page sends explain true and shows the observation, so the fix has a reader there."""
    observed = evaluator.evaluate_tool_call(
        _request("fix-page-dry", _domain_write(), origin="page", agent="page", explain=True, dry_run=True)
    )
    assert observed.status == "APPROVED" and observed.observed_rules
    fix = observed.suggested_fix
    assert fix is not None and fix["kind"] == "layering" and fix["validated"] is True


def test_a_repeated_read_is_noted_but_sent_no_fix(evaluator, proposals) -> None:
    read = _request("fix-poll", {"command": "git status"}, tool_name="Bash", action_type="COMMAND_EXEC", origin="page", agent="page", explain=True)
    results = [evaluator.evaluate_tool_call(read) for _ in range(4)]
    assert results[-1].status == "APPROVED" and results[-1].observations, "The repeat is noted"
    assert all(result.suggested_fix is None for result in results)
    assert proposals == [], "Nothing would have refused it, so nothing is proposed"


def test_a_fault_in_the_fix_never_costs_the_verdict(evaluator, recorded, monkeypatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the proposer broke")

    monkeypatch.setattr(evaluator_module, "propose_fix", broken)
    result = evaluator.evaluate_tool_call(_request("fix-broken", _domain_write()))
    assert result.status == "BLOCKED_BOUNDARY_VIOLATION" and result.suggested_fix is None
    assert not any("fix" in key and recorded[-1][key] is not None for key in recorded[-1])


def test_an_approval_is_sent_no_fix(evaluator, proposals) -> None:
    approved = evaluator.evaluate_tool_call(_request("fix-approved", {"file_path": "src/acme/app.py", "content": "x = 1\n"}))
    assert approved.status == "APPROVED" and approved.suggested_fix is None
    assert proposals == []


# ---------------------------------------------------------------- the ledger


def test_the_ledger_row_keeps_only_the_kind_and_whether_it_was_validated(evaluator, recorded) -> None:
    result = evaluator.evaluate_tool_call(_request("fix-ledger", _domain_write()))
    fix = result.suggested_fix
    (row,) = recorded
    assert {key for key in row if "fix" in key} == {"suggested_fix_kind", "suggested_fix_validated"}
    assert (row["suggested_fix_kind"], row["suggested_fix_validated"]) == ("layering", True)
    stored = json.dumps(row)
    for write in fix["writes"]:
        assert write["content"] not in stored, "A proposed file reached the ledger"
    for step in fix["steps"]:
        assert step not in stored, "A step reached the ledger"
    assert fix["summary"] not in stored, "The summary reached the ledger"
    assert "Protocol" not in stored and "Adapter" not in stored

    raw = [item for item in evaluator.session_repo._memory_store.values() if item.get("session_id") == "fix-ledger"]
    assert len(raw) == 1 and "suggested_fix" not in raw[0], "The stored item holds no fix"
    (read,) = [r for r in evaluator.list_decisions(days=1) if r["session_id"] == "fix-ledger"]
    assert (read["suggested_fix_kind"], read["suggested_fix_validated"]) == ("layering", True)


def test_an_unvalidated_fix_is_recorded_as_such(evaluator, recorded) -> None:
    loop = _request("fix-ledger-loop", {"command": "npm run build"}, tool_name="Bash", action_type="COMMAND_EXEC")
    for _ in range(3):
        evaluator.evaluate_tool_call(loop)
    assert (recorded[-1]["suggested_fix_kind"], recorded[-1]["suggested_fix_validated"]) == ("loop", False)


def test_a_row_without_a_fix_is_exactly_what_it_was(evaluator, recorded) -> None:
    evaluator.evaluate_tool_call(_request("fix-ledger-approved", {"file_path": "src/acme/app.py", "content": "x = 1\n"}))
    (read,) = [r for r in evaluator.list_decisions(days=1) if r["session_id"] == "fix-ledger-approved"]
    assert not any("fix" in key for key in read), "An approval's row gains no field"
    raw = [item for item in evaluator.session_repo._memory_store.values() if item.get("session_id") == "fix-ledger-approved"]
    assert not any("fix" in key for key in raw[0])


# ---------------------------------------------------------------- when the fix has a reader


def test_a_hook_body_that_leaves_explain_out_is_sent_no_fix_for_an_observation(evaluator, proposals, recorded, monkeypatch) -> None:
    """A body without `explain` reads as explaining, so a v1 or third-party hook must not pay for a fix it never prints."""
    monkeypatch.setenv("DEFAULT_HOOK_STAGE", "observe")
    request = ToolCallRequestDTO.from_payload(
        {
            "session_id": "fix-hook-no-explain",
            "project_name": PROJECT,
            "tool_name": "Write",
            "action_type": "FILE_WRITE",
            "arguments": _domain_write(),
            "agent": "claude-code",
            "origin": "hook",
            "hook_mode": "managed",
        }
    )
    assert request.explain is True, "The premise: a body that leaves explain out reads as true"
    observed = evaluator.evaluate_tool_call(request)
    assert observed.status == "APPROVED" and observed.observed_rules
    assert observed.suggested_fix is None
    assert proposals == [], "The proposer was not even asked"
    assert not any("fix" in key and recorded[-1][key] is not None for key in recorded[-1])


def test_a_ci_observation_is_sent_no_fix_even_when_it_asks_for_an_explanation(evaluator, proposals) -> None:
    observed = evaluator.evaluate_tool_call(
        _request("fix-ci-dry", _domain_write(), origin="ci", agent="ci", explain=True, dry_run=True)
    )
    assert observed.status == "APPROVED" and observed.observed_rules
    assert observed.suggested_fix is None and proposals == []


# ---------------------------------------------------------------- how much a call may carry


ACME_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
CREDENTIAL_MODULE_HEAD = f"import os\n\nACME_KEY = '{ACME_KEY}'\n"
PAD = "# " + "acme " * 2_000


def _padded(head: str, size: int) -> str:
    """`head`, then a comment, to exactly `size` characters."""
    return (head + PAD * (size // len(PAD) + 1))[:size]


def _exactly(arguments: Dict[str, Any], key: str, size: int) -> Dict[str, Any]:
    """The arguments with `key` padded so that the call carries exactly `size` characters."""
    rest = _carried(arguments) - len(arguments[key])
    return dict(arguments, **{key: _padded(arguments[key], size - rest)})


def test_each_kind_of_verdict_reads_its_own_ceiling() -> None:
    for key in ("LOOP", "BUDGET", "HALTED_SESSION", "PROTECTED_PATH"):
        assert fix_max_chars(key) == FIX_ADVICE_MAX_CHARS, key
        assert fix_max_chars(key, True) == FIX_ADVICE_MAX_CHARS, key
        assert fix_options(key) == {}, key
    assert fix_max_chars("CREDENTIAL") == FIX_CREDENTIAL_MAX_CHARS
    assert fix_options("CREDENTIAL") == {}
    for key in ("python-domain-stays-pure", "java-domain-stays-pure", "acme-core-no-http"):
        assert fix_max_chars(key) == FIX_LAYERING_ADVICE_MAX_CHARS, key
        assert fix_max_chars(key, True) == FIX_REWRITE_MAX_CHARS, f"{key}: a command keeps the rewrite ceiling"
        assert fix_options(key) == {"max_content_chars": FIX_REWRITE_MAX_CHARS}, key
    for key in ("UNREADABLE_WRITE", "NONE", ""):
        assert fix_max_chars(key) == FIX_REWRITE_MAX_CHARS, key
        assert fix_options(key) == {"max_content_chars": FIX_REWRITE_MAX_CHARS}, key
    assert FIX_REWRITE_MAX_CHARS < FIX_CREDENTIAL_MAX_CHARS < FIX_LAYERING_ADVICE_MAX_CHARS < FIX_ADVICE_MAX_CHARS
    assert FIX_LAYERING_ADVICE_MAX_CHARS < REFERENCE_CHARS, "The reference is the refused write too large for any fix"


def test_a_call_runs_a_command_as_the_guard_reads_one() -> None:
    assert runs_a_command(_request("cmd-1", {"command": "ls"}, tool_name="Bash", action_type="COMMAND_EXEC"))
    assert runs_a_command(_request("cmd-2", {"command": "ls"})), "A call that carries `command` runs it, whatever it says it is"
    assert runs_a_command(_request("cmd-3", {"cmd": ["ls", "-la"]}, tool_name="exec_command", action_type="UNKNOWN"))
    assert not runs_a_command(_request("cmd-4", _domain_write()))
    assert not runs_a_command(_request("cmd-5", {"file_path": DOMAIN_PATH, "script": "import boto3\n"}))


def test_a_loop_past_the_rewrite_ceiling_is_still_told_what_repeated(evaluator) -> None:
    arguments = {"file_path": "src/acme/app.py", "content": _padded("x = 1\n", 3_000)}
    assert _carried(arguments) > FIX_REWRITE_MAX_CHARS
    results = [evaluator.evaluate_tool_call(_request("fix-loop-3k", dict(arguments))) for _ in range(3)]
    assert results[-1].status == "BLOCKED_LOOP_DETECTED"
    assert results[-1].suggested_fix["kind"] == "loop"


def test_a_protected_path_past_the_rewrite_ceiling_is_told_the_governed_way(evaluator) -> None:
    arguments = {"file_path": ".claude/settings.json", "content": _padded('{"hooks": {}}\n', 3_000)}
    result = evaluator.evaluate_tool_call(_request("fix-protected-3k", arguments))
    assert result.status == "BLOCKED_BOUNDARY_VIOLATION"
    assert result.suggested_fix["kind"] == "protected_path"


def test_a_destructive_command_past_the_rewrite_ceiling_is_told_what_to_do_instead(evaluator) -> None:
    arguments = {"command": _padded("rm -rf / ", 2_000)}
    result = evaluator.evaluate_tool_call(_request("fix-destructive-2k", arguments, tool_name="Bash", action_type="COMMAND_EXEC"))
    assert result.status == "BLOCKED_BOUNDARY_VIOLATION"
    assert result.suggested_fix["kind"] == "destructive_command"


def test_a_halted_session_past_the_rewrite_ceiling_is_told_how_it_resumes(evaluator) -> None:
    page = dict(origin="page", agent="page", explain=True)
    loop = _request("sim-fix-halted-2k", {"file_path": "src/acme/service.py", "instruction": "fix typo"}, tool_name="edit_file", **page)
    for _ in range(3):
        evaluator.evaluate_tool_call(loop)
    after = evaluator.evaluate_tool_call(
        _request("sim-fix-halted-2k", {"file_path": "src/acme/app.py", "content": _padded("x = 1\n", 2_000)}, **page)
    )
    assert after.status == "BLOCKED_CIRCUIT_BREAKER"
    assert after.suggested_fix["kind"] == "halted_session"


def test_a_call_over_its_cost_cap_past_the_rewrite_ceiling_is_told_how_to_split_it(evaluator) -> None:
    arguments = {"file_path": "src/acme/app.py", "content": _padded("x = 1\n", 2_000)}
    result = evaluator.evaluate_tool_call(
        _request("fix-budget-2k", arguments, projected_input_tokens=10**9, projected_output_tokens=10**9)
    )
    assert result.status == "BLOCKED_CIRCUIT_BREAKER"
    assert result.suggested_fix["kind"] == "budget"


def test_a_credential_past_the_rewrite_ceiling_is_still_rewritten_and_checked(evaluator) -> None:
    arguments = {"file_path": "src/acme/acme_client.py", "content": _python_domain_file(2_900, head=CREDENTIAL_MODULE_HEAD)}
    assert FIX_REWRITE_MAX_CHARS < _carried(arguments) <= FIX_CREDENTIAL_MAX_CHARS
    result = evaluator.evaluate_tool_call(_request("fix-credential-3k", arguments))
    assert result.status == "BLOCKED_SECRET_DETECTED"
    fix = result.suggested_fix
    assert fix["kind"] == "credential" and fix["validated"] is True
    assert ACME_KEY not in json.dumps(fix), "A fix never repeats the credential"


def test_at_the_rewrite_ceiling_a_layering_refusal_is_rewritten_and_checked(evaluator, proposals) -> None:
    arguments = _exactly(_domain_write(), "content", FIX_REWRITE_MAX_CHARS)
    result = evaluator.evaluate_tool_call(_request("fix-at-rewrite-limit", arguments))
    assert result.status == "BLOCKED_BOUNDARY_VIOLATION"
    fix = result.suggested_fix
    assert fix["kind"] == "layering" and fix["validated"] is True and fix["writes"]
    rules, _ = evaluator.rules_in_force(PROJECT)
    for write in fix["writes"]:
        assert _passes_the_gate(write, rules), f"The validated fix is itself refused at {write['path']}"
    assert proposals[-1]["options"] == {"max_content_chars": FIX_REWRITE_MAX_CHARS}


def test_the_rewrite_ceiling_is_on_what_is_rewritten_not_on_the_path_beside_it(evaluator, proposals) -> None:
    arguments = _exactly(_domain_write(), "content", FIX_REWRITE_MAX_CHARS + 1)
    assert len(arguments["content"]) < FIX_REWRITE_MAX_CHARS < _carried(arguments)
    result = evaluator.evaluate_tool_call(_request("fix-past-by-its-path", arguments))
    assert result.suggested_fix["validated"] is True and result.suggested_fix["writes"]


def test_a_write_one_character_past_the_rewrite_ceiling_is_told_in_words_what_to_move_and_where(evaluator, proposals) -> None:
    """It used to get nothing at all: too large to rewrite within the budget, and so no fix."""
    content = _padded(_python_domain_file(FIX_REWRITE_MAX_CHARS - 120, head="import boto3\nimport requests\n"), FIX_REWRITE_MAX_CHARS + 1)
    arguments = _domain_write(content)
    assert len(content) == FIX_REWRITE_MAX_CHARS + 1
    result = evaluator.evaluate_tool_call(_request("fix-past-rewrite-limit", arguments))
    assert result.status == "BLOCKED_BOUNDARY_VIOLATION", "The refusal stands, whatever the fix says"
    fix = result.suggested_fix
    assert fix is not None and fix["kind"] == "layering"
    assert fix["validated"] is False and fix["writes"] == [] and fix["checks"] == []
    assert fix["steps"][0] == (
        f"In {DOMAIN_PATH}, remove the imports of boto3, requests: rule 'python-domain-stays-pure' forbids them there."
    )
    assert "adapter under src/acme_orders/infrastructure/ that implements it" in fix["steps"][1]
    assert "boto3 and 1 more" in fix["summary"] and "src/acme_orders/infrastructure" in fix["summary"]
    (call,) = proposals
    assert call["options"] == {"max_content_chars": FIX_REWRITE_MAX_CHARS}


def test_the_advice_reaches_the_agent_in_its_deny_reason(evaluator) -> None:
    from threefold.hooks.threefold_hook import refusal_reason

    arguments = _exactly(_domain_write(), "content", FIX_LAYERING_ADVICE_MAX_CHARS)
    judged = json.loads(json.dumps(evaluator.evaluate_tool_call(_request("fix-advice-to-hook", arguments)).to_dict()))
    fix = judged["suggested_fix"]
    assert fix["validated"] is False
    reason = refusal_reason(judged)
    assert reason.split("\n")[-1] == f"Suggested fix: {fix['summary']}", "Advice is labelled as advice, never as checked"
    assert "move boto3 behind a port, adapter under src/acme_orders/infrastructure" in reason


def test_one_character_past_the_layering_advice_ceiling_is_sent_no_fix(evaluator, proposals) -> None:
    arguments = _exactly(_domain_write(), "content", FIX_LAYERING_ADVICE_MAX_CHARS + 1)
    result = evaluator.evaluate_tool_call(_request("fix-past-advice-limit", arguments))
    assert result.status == "BLOCKED_BOUNDARY_VIOLATION" and result.suggested_fix is None
    assert proposals == [], "Past its ceiling the proposer is not even asked"


def test_a_multi_edit_past_the_rewrite_ceiling_is_answered_in_words_not_rewritten_edit_by_edit(evaluator) -> None:
    edits = [
        {"old_string": f"A_{index} = 0", "new_string": _python_domain_file(FIX_REWRITE_MAX_CHARS - 600, head=f"import boto3\nA_{index} = 1\n")}
        for index in range(3)
    ]
    arguments = {"file_path": DOMAIN_PATH, "edits": edits}
    assert all(len(edit["new_string"]) < FIX_REWRITE_MAX_CHARS for edit in edits)
    assert FIX_REWRITE_MAX_CHARS < _carried(arguments) <= FIX_LAYERING_ADVICE_MAX_CHARS
    result = evaluator.evaluate_tool_call(_request("fix-multi-edit", arguments, tool_name="MultiEdit"))
    assert result.status == "BLOCKED_BOUNDARY_VIOLATION"
    assert result.suggested_fix["validated"] is False and result.suggested_fix["writes"] == []


def test_a_command_past_the_rewrite_ceiling_is_sent_no_fix(evaluator, proposals) -> None:
    """Its writes are read again for each question the proposer asks, so it keeps the rewrite ceiling."""
    bash = dict(tool_name="Bash", action_type="COMMAND_EXEC")
    body = _python_domain_file(FIX_REWRITE_MAX_CHARS * 2)
    arguments = {"command": f"cat > {DOMAIN_PATH} <<'EOF'\n{body}EOF\n"}
    assert FIX_REWRITE_MAX_CHARS < _carried(arguments) <= FIX_LAYERING_ADVICE_MAX_CHARS
    result = evaluator.evaluate_tool_call(_request("fix-heredoc-past", arguments, **bash))
    assert result.status == "BLOCKED_BOUNDARY_VIOLATION" and result.suggested_fix is None
    assert proposals == []

    small = {"command": f"cat > {DOMAIN_PATH} <<'EOF'\n{DOMAIN_WRITE}EOF\n"}
    result = evaluator.evaluate_tool_call(_request("fix-heredoc-small", small, **bash))
    assert result.suggested_fix["kind"] == "layering" and result.suggested_fix["validated"] is True


@pytest.mark.parametrize(
    "kind, ceiling, arguments, padded, overrides, status",
    [
        (
            "credential",
            FIX_CREDENTIAL_MAX_CHARS,
            {"file_path": "src/acme/acme_client.py", "content": f"ACME_KEY = '{ACME_KEY}'\n"},
            "content",
            {},
            "BLOCKED_SECRET_DETECTED",
        ),
        ("layering", FIX_LAYERING_ADVICE_MAX_CHARS, _domain_write(), "content", {}, "BLOCKED_BOUNDARY_VIOLATION"),
        ("protected_path", FIX_ADVICE_MAX_CHARS, {"file_path": ".claude/settings.json", "content": "{}\n"}, "content", {}, "BLOCKED_BOUNDARY_VIOLATION"),
        (
            "destructive_command",
            FIX_ADVICE_MAX_CHARS,
            {"command": "rm -rf / "},
            "command",
            {"tool_name": "Bash", "action_type": "COMMAND_EXEC"},
            "BLOCKED_BOUNDARY_VIOLATION",
        ),
    ],
)
def test_each_ceiling_is_where_its_fix_stops(kind, ceiling, arguments, padded, overrides, status, evaluator, proposals) -> None:
    at = evaluator.evaluate_tool_call(_request(f"fix-at-{kind}", _exactly(arguments, padded, ceiling), **overrides))
    assert at.status == status
    assert at.suggested_fix is not None and at.suggested_fix["kind"] == kind
    asked = len(proposals)
    past = evaluator.evaluate_tool_call(_request(f"fix-past-{kind}", _exactly(arguments, padded, ceiling + 1), **overrides))
    assert past.status == status and past.suggested_fix is None
    assert len(proposals) == asked, "Past its ceiling the proposer is not even asked"


def test_measuring_a_call_stops_as_soon_as_the_answer_is_known() -> None:
    seen: List[str] = []

    class Counting(dict):
        def items(self):
            for key, value in super().items():
                seen.append(key)
                yield key, value

    arguments = Counting((f"k{index}", "x" * 100) for index in range(1000))
    assert carries_more_than(arguments, 1_000) is True
    assert len(seen) < 20, "It read the whole call to answer a question the first keys settled"
    assert carries_more_than({"file_path": "a.py", "content": "x"}, 1_000) is False


def test_arguments_too_deep_to_walk_count_as_too_large() -> None:
    nested: Any = "leaf"
    for _ in range(5_000):
        nested = [nested]
    assert carries_more_than({"content": nested}, FIX_REWRITE_MAX_CHARS) is True


# ---------------------------------------------------------------- what it may cost
#
# Trap 3's budget is 10 ms of gate on the development machine, and a test run
# is not that machine: coverage alone took the verdict on the reference call
# below from 10 ms to 23 ms there, and a CI runner is slower again, so a ceiling
# written in milliseconds failed under the repository's own CI command. The
# budget is held as a workload instead: what a whole verdict costs, in the same
# process and under the same instrumentation, on a refused Python domain Write
# of REFERENCE_CHARS characters, which took the development machine's verdict
# about 10 ms [PRIMARY], 2026-09-22, best of nine. There the dearest fix of each
# kind at its ceiling took 0.48 to 0.60 of it, and 0.32 to 0.53 under coverage:
# instrumentation slows both sides by about as much. So the comparison leaves
# room for a noisy runner without being loose: a ceiling roughly doubled would
# fail it.

REFERENCE_CHARS = 20_000


def _against_the_budget(evaluator: GovernanceEvaluator, monkeypatch, request_for, runs: int = 15):
    """The fix step on request_for(i), and a whole verdict on the reference call, best of `runs` each.

    Interleaved, so a busy moment slows both rather than one of them; the best
    of each, as timeit takes it, because the fastest run is the one that
    measures the code rather than whatever else the machine was doing.
    Returns (fix seconds, budget seconds, the last verdict).
    """
    spent: List[float] = []
    real = GovernanceEvaluator._suggest_fix

    def timed(*args: Any) -> Any:
        started = time.perf_counter()
        try:
            return real(*args)
        finally:
            spent.append(time.perf_counter() - started)

    monkeypatch.setattr(GovernanceEvaluator, "_suggest_fix", staticmethod(timed))
    reference = _domain_write(_python_domain_file(REFERENCE_CHARS))
    fixes: List[float] = []
    budgets: List[float] = []
    result = None
    for index in range(runs):
        started = time.perf_counter()
        evaluator.evaluate_tool_call(_request(f"fix-reference-{index}", dict(reference)))
        budgets.append(time.perf_counter() - started)
        spent.clear()
        result = evaluator.evaluate_tool_call(request_for(index))
        fixes.append(spent[-1])
    return min(fixes), min(budgets), result


# The dearest case under each ceiling, as measured: Python, the slowest language
# to rewrite, for the two rewrites and for a layering refusal answered in words,
# and a protected path, the dearest advice, for the last. Each carries between
# its ceiling less 120 characters and its ceiling.
DEAREST = [
    ("layering", FIX_REWRITE_MAX_CHARS, _domain_write(_python_domain_file(FIX_REWRITE_MAX_CHARS - 60)), True),
    ("layering", FIX_LAYERING_ADVICE_MAX_CHARS, _domain_write(_python_domain_file(FIX_LAYERING_ADVICE_MAX_CHARS - 60)), False),
    (
        "credential",
        FIX_CREDENTIAL_MAX_CHARS,
        {"file_path": "src/acme/acme_client.py", "content": _python_domain_file(FIX_CREDENTIAL_MAX_CHARS - 60, head=CREDENTIAL_MODULE_HEAD)},
        True,
    ),
    ("protected_path", FIX_ADVICE_MAX_CHARS, {"file_path": ".claude/settings.json", "content": _python_domain_file(FIX_ADVICE_MAX_CHARS - 60)}, False),
]


@pytest.mark.parametrize(
    "kind, ceiling, arguments, validated", DEAREST, ids=[f"{case[0]}-{'rewritten' if case[3] else 'in-words'}" for case in DEAREST]
)
def test_the_dearest_fix_of_each_kind_stays_inside_the_gate_budget(kind, ceiling, arguments, validated, evaluator, monkeypatch) -> None:
    assert ceiling - 120 < _carried(arguments) <= ceiling
    spent, budget, result = _against_the_budget(
        evaluator, monkeypatch, lambda index: _request(f"fix-dearest-{kind}-{index}", dict(arguments))
    )
    fix = result.suggested_fix
    assert fix is not None and fix["kind"] == kind and fix["validated"] is validated
    assert spent < budget, (
        f"The {kind} fix at its ceiling took {spent * 1000:.1f} ms, more than the {budget * 1000:.1f} ms a whole "
        f"verdict took here on the {REFERENCE_CHARS:,} character reference, which is Trap 3's 10 ms on the development machine"
    )


def test_a_large_refused_write_pays_only_for_counting_its_characters(evaluator, monkeypatch) -> None:
    """A refused domain Write the size of the reference itself: past every layering ceiling, so no fix.

    What the refusal pays for that decision is reading its rule key and
    counting its characters as far as the ceiling, a few microseconds on the
    development machine against the verdict's 10 ms.
    """
    content = _python_domain_file(REFERENCE_CHARS)
    spent, budget, result = _against_the_budget(
        evaluator, monkeypatch, lambda index: _request(f"fix-large-{index}", _domain_write(content))
    )
    assert result.status == "BLOCKED_BOUNDARY_VIOLATION", "The refusal stands"
    assert result.suggested_fix is None, "Too large to rewrite and check within the gate's budget"
    assert spent < budget / 20, f"Deciding against a fix took {spent * 1000:.2f} ms against a {budget * 1000:.1f} ms verdict"
