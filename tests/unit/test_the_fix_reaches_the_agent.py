"""The evaluator attaches a checked fix to the verdicts someone will read.

A refusal is read by the hook, which puts the fix's summary in its deny reason,
and by the pages, which show all of it. An observation is read only on a page:
in Observe a hook prints nothing, so a fix computed there would cost time on
every call and reach nobody. The fix rides on the verdict and never on the
ledger, whose rows anyone reads on the public stack; the ledger keeps what kind
of fix was offered and whether the gates passed it. And a fix is paid for on
the verdict's clock, so what it may add is held to the gate's own budget.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List

import pytest

from threefold.application import evaluator as evaluator_module
from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import FIX_MAX_CONTENT_CHARS, GovernanceEvaluator, carries_more_than
from threefold.domain.boundary_guard import ArchitecturalBoundaryGuard, iter_string_leaves
from threefold.domain.layering_rules import violations
from threefold.domain.models import ToolActionType, ToolInvocation

# The budget TRAPS.md sets for the gates (Trap 3): past 10 ms a verdict degrades
# the developer's experience, and a fix is paid for inside the verdict.
GATE_BUDGET_SECONDS = 0.010

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


def _python_domain_file(size: int) -> str:
    """A refused Python domain file of about `size` characters, built rather than spelled out."""
    parts = ["import boto3\n"]
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


# ---------------------------------------------------------------- what it may cost


def _time_the_fix(evaluator: GovernanceEvaluator, monkeypatch, request_for, runs: int = 25):
    """The time evaluate_tool_call spends on the fix, best of `runs`, and the last verdict.

    Best of many, as timeit takes it, with a pause between runs: a wall clock
    also counts whatever else the machine is doing, and with seven runs a busy
    moment across the whole suite once pushed every one of them past the
    budget. The fastest of runs spread over half a second is the one that
    measures the code rather than the neighbours.
    """
    spent: List[float] = []
    real = GovernanceEvaluator._suggest_fix

    def timed(request: Any, result: Any, rules: Any) -> Any:
        started = time.perf_counter()
        try:
            return real(request, result, rules)
        finally:
            spent.append(time.perf_counter() - started)

    monkeypatch.setattr(GovernanceEvaluator, "_suggest_fix", staticmethod(timed))
    result = None
    for index in range(runs):
        result = evaluator.evaluate_tool_call(request_for(index))
        time.sleep(0.01)
    return min(spent), result


def test_a_large_refused_write_adds_nothing_the_gate_budget_would_notice(evaluator, monkeypatch) -> None:
    """A 20,000 character domain file: the proposer would take about 60 ms to rewrite and check it.

    Measured on the development machine [PRIMARY], 2026-09-22, best of nine:
    the gate alone takes about 10 ms on this file, all of Trap 3's budget, and
    a full fix six times that. So a call this large is refused without a fix,
    and what the refusal pays for that decision is counting its characters,
    about 0.01 ms.
    """
    content = _python_domain_file(20_000)
    assert len(content) > 19_000
    spent, result = _time_the_fix(evaluator, monkeypatch, lambda index: _request(f"fix-large-{index}", _domain_write(content)))
    assert result.status == "BLOCKED_BOUNDARY_VIOLATION", "The refusal stands"
    assert result.suggested_fix is None, "Too large to rewrite and check within the gate's budget"
    assert spent < GATE_BUDGET_SECONDS, f"The fix step added {spent * 1000:.1f} ms to a large refusal"


def test_the_largest_write_that_is_sent_a_fix_stays_inside_the_gate_budget(evaluator, monkeypatch) -> None:
    """The dearest case that still pays: Python, the slowest language to rewrite, at the size limit.

    Measured on the development machine [PRIMARY], 2026-09-22, best of nine:
    about 5 ms just under 1,500 characters, against Trap 3's 10 ms.
    """
    arguments = _domain_write(_python_domain_file(FIX_MAX_CONTENT_CHARS - 60))
    assert FIX_MAX_CONTENT_CHARS - 120 < _carried(arguments) <= FIX_MAX_CONTENT_CHARS
    spent, result = _time_the_fix(evaluator, monkeypatch, lambda index: _request(f"fix-limit-{index}", dict(arguments)))
    assert result.status == "BLOCKED_BOUNDARY_VIOLATION"
    assert result.suggested_fix is not None and result.suggested_fix["validated"] is True
    assert spent < GATE_BUDGET_SECONDS, f"The fix added {spent * 1000:.1f} ms at the size limit"


def test_one_character_past_the_limit_is_sent_no_fix(evaluator, proposals) -> None:
    content = _python_domain_file(FIX_MAX_CONTENT_CHARS - 120)
    padding = FIX_MAX_CONTENT_CHARS + 1 - _carried(_domain_write(content))
    assert padding > 0
    arguments = _domain_write(content + "#" * padding)
    assert _carried(arguments) == FIX_MAX_CONTENT_CHARS + 1
    result = evaluator.evaluate_tool_call(_request("fix-past-limit", arguments))
    assert result.status == "BLOCKED_BOUNDARY_VIOLATION" and result.suggested_fix is None
    assert proposals == []


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
    assert carries_more_than({"content": nested}, FIX_MAX_CONTENT_CHARS) is True
