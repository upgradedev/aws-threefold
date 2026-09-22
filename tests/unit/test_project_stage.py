"""A project observes before it enforces, and the stage decides only a hook's calls.

Every project starts in observe: each call from its hooks is judged and
recorded and nothing is refused. Enforce runs the gates, except for the rules
the operator left observing. The pages and the dashboard's scenarios always
enforce, so the public demo is unchanged, and a dry run is never refused
whatever the stage says.

The suite runs with DEFAULT_HOOK_STAGE=enforce (tests/conftest.py) so every
older test keeps its meaning; the tests of the observe default unset it here.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import pytest

from threefold.application import evaluator as evaluator_module
from threefold.application import projects as stages
from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository

PROJECT = "Acme-Stage"
DOMAIN_WRITE = {"file_path": "src/acme/domain/order.py", "content": "import boto3\n"}
JAVA_WRITE = {"file_path": "src/main/java/com/acme/domain/Order.java", "content": "import javax.persistence.Entity;\n"}
EDIT = {"file_path": "src/acme/service.py", "old_string": "a = 1", "new_string": "a = 2"}
NOW = "2026-09-22T08:00:00+00:00"


@pytest.fixture
def repo() -> DynamoDBSessionRepository:
    return DynamoDBSessionRepository()


@pytest.fixture
def evaluator(repo) -> GovernanceEvaluator:
    return GovernanceEvaluator(session_repo=repo)


@pytest.fixture
def observe_default(monkeypatch):
    """The stack as deployed: no DEFAULT_HOOK_STAGE, so a new project observes."""
    monkeypatch.delenv("DEFAULT_HOOK_STAGE", raising=False)


def _call(session_id: str, arguments=None, *, tool="Write", action="FILE_WRITE", origin="hook", **extra):
    return ToolCallRequestDTO(
        session_id=session_id,
        developer_id="anonymous",
        project_name=extra.pop("project", PROJECT),
        tool_name=tool,
        action_type=action,
        arguments=dict(arguments if arguments is not None else DOMAIN_WRITE),
        projected_input_tokens=extra.pop("input_tokens", 0),
        projected_output_tokens=0,
        origin=origin,
        agent=extra.pop("agent", "claude-code" if origin == "hook" else "page"),
        **extra,
    )


def _row(evaluator: GovernanceEvaluator, verdict_id: str) -> dict:
    rows = [row for row in evaluator.list_decisions(days=1) if row["verdict_id"] == verdict_id]
    assert rows, f"No ledger row for {verdict_id}"
    return rows[0]


def _configure(evaluator: GovernanceEvaluator, stage: str, observe_rules=(), project: str = PROJECT) -> None:
    config = stages.new_config(NOW, stage=stage)
    config["observe_rules"] = list(observe_rules)
    evaluator.save_project_config(project, config)


# ---------------------------------------------------------------- the default


def test_a_new_project_observes_by_default(evaluator, observe_default) -> None:
    verdict = evaluator.evaluate_tool_call(_call("stage-default"))
    assert verdict.status == "APPROVED"
    assert verdict.project_stage == "observe"
    assert verdict.dry_run is False, "The caller did not ask for a dry run, and the verdict says so"
    assert "Observe stage, not enforced" in verdict.reason
    assert verdict.observations and "python-domain-stays-pure" in verdict.observations[0]

    row = _row(evaluator, verdict.verdict_id)
    assert row["stage"] == "observe"
    assert row["dry_run"] is False
    assert row["rule_key"] == "python-domain-stays-pure"


def test_observe_never_halts_a_session(evaluator, observe_default) -> None:
    """CI's loop halts its session when enforced. Observed, it halts nothing."""
    for _ in range(4):
        verdict = evaluator.evaluate_tool_call(_call("stage-ci-loop", EDIT, tool="Edit", origin="ci", agent="ci"))
        assert verdict.status == "APPROVED"
    assert evaluator.session_repo.get_session("stage-ci-loop").is_tripped is False
    assert _row(evaluator, verdict.verdict_id)["rule_key"] == "LOOP"


def test_a_default_that_is_neither_stage_reads_as_observe(evaluator, monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_HOOK_STAGE", "enforse")
    assert stages.default_hook_stage() == "observe"
    assert evaluator.evaluate_tool_call(_call("stage-typo")).status == "APPROVED"


def test_the_suite_default_enforces(evaluator) -> None:
    verdict = evaluator.evaluate_tool_call(_call("stage-suite-default"))
    assert verdict.status == "BLOCKED_BOUNDARY_VIOLATION"
    assert verdict.project_stage == "enforce"
    assert _row(evaluator, verdict.verdict_id)["stage"] == "enforce"


# ---------------------------------------------------------------- a configured stage


def test_a_configured_stage_overrides_the_default_both_ways(evaluator, monkeypatch) -> None:
    _configure(evaluator, "enforce", project="Acme-Stage-Enforced")
    _configure(evaluator, "observe", project="Acme-Stage-Observed")

    monkeypatch.delenv("DEFAULT_HOOK_STAGE", raising=False)
    assert evaluator.evaluate_tool_call(_call("stage-cfg-1", project="Acme-Stage-Enforced")).status.startswith("BLOCKED")

    monkeypatch.setenv("DEFAULT_HOOK_STAGE", "enforce")
    assert evaluator.evaluate_tool_call(_call("stage-cfg-2", project="Acme-Stage-Observed")).status == "APPROVED"


def test_a_refusal_under_an_observed_rule_becomes_an_observation(evaluator) -> None:
    _configure(evaluator, "enforce", observe_rules=["python-domain-stays-pure"])

    watched = evaluator.evaluate_tool_call(_call("stage-observed-rule"))
    assert watched.status == "APPROVED"
    assert "python-domain-stays-pure" in watched.reason
    assert watched.dry_run is False
    row = _row(evaluator, watched.verdict_id)
    assert (row["rule_key"], row["stage"]) == ("python-domain-stays-pure", "enforce")

    enforced = evaluator.evaluate_tool_call(_call("stage-enforced-rule", JAVA_WRITE))
    assert enforced.status == "BLOCKED_BOUNDARY_VIOLATION"
    assert _row(evaluator, enforced.verdict_id)["rule_key"] == "java-domain-stays-pure"


def test_an_observed_gate_neither_refuses_nor_halts(evaluator) -> None:
    """The loop gate halts a CI session when it refuses, so the decision to
    observe has to be made before anything is written, not after."""
    _configure(evaluator, "enforce", observe_rules=["LOOP", "BUDGET"])
    for _ in range(4):
        verdict = evaluator.evaluate_tool_call(_call("stage-observed-loop", EDIT, tool="Edit", origin="ci", agent="ci"))
        assert verdict.status == "APPROVED"
    assert evaluator.session_repo.get_session("stage-observed-loop").is_tripped is False

    spend = evaluator.evaluate_tool_call(
        _call("stage-observed-budget", {"file_path": "README.md"}, tool="Read", action="FILE_READ",
              origin="ci", agent="ci", input_tokens=2_000_000)
    )
    assert spend.status == "APPROVED"
    assert _row(evaluator, spend.verdict_id)["rule_key"] == "BUDGET"
    assert evaluator.session_repo.get_session("stage-observed-budget").is_tripped is False


def test_a_gate_the_project_enforces_still_halts_durably(evaluator) -> None:
    """Observing one rule must not soften another: the refusal is judged again on
    the real session, and CI's loop halt is written."""
    _configure(evaluator, "enforce", observe_rules=["python-domain-stays-pure"])
    for _ in range(3):
        verdict = evaluator.evaluate_tool_call(_call("stage-enforced-loop", EDIT, tool="Edit", origin="ci", agent="ci"))
    assert verdict.status == "BLOCKED_LOOP_DETECTED"
    assert evaluator.session_repo.get_session("stage-enforced-loop").is_tripped is True


def test_an_approved_call_is_recorded_once_when_rules_are_observed(evaluator) -> None:
    _configure(evaluator, "enforce", observe_rules=["LOOP"])
    evaluator.evaluate_tool_call(_call("stage-once", {"file_path": "README.md"}, tool="Read", action="FILE_READ"))
    assert len(evaluator.session_repo.get_session("stage-once").history) == 1


# ---------------------------------------------------------------- what the stage never touches


def test_a_page_call_is_enforced_whatever_the_projects_stage(evaluator) -> None:
    _configure(evaluator, "observe")
    verdict = evaluator.evaluate_tool_call(_call("stage-page", origin="page"))
    assert verdict.status == "BLOCKED_BOUNDARY_VIOLATION"
    assert verdict.project_stage == "observe", "The project's stage is reported even where it did not apply"
    assert _row(evaluator, verdict.verdict_id)["stage"] == "enforce"


def test_a_call_with_no_origin_is_enforced_as_before(evaluator, observe_default) -> None:
    verdict = evaluator.evaluate_tool_call(_call("stage-v1", origin="unknown"))
    assert verdict.status == "BLOCKED_BOUNDARY_VIOLATION"


def test_a_scenario_session_keeps_its_halt_whatever_origin_it_claims(evaluator, observe_default) -> None:
    for _ in range(3):
        verdict = evaluator.evaluate_tool_call(_call("sim-loop-stage", EDIT, tool="Edit"))
    assert verdict.status == "BLOCKED_LOOP_DETECTED"
    assert verdict.session_tripped is True


def test_a_dry_run_is_never_refused_even_in_enforce(evaluator) -> None:
    _configure(evaluator, "enforce")
    verdict = evaluator.evaluate_tool_call(_call("stage-dry", dry_run=True))
    assert verdict.status == "APPROVED"
    assert verdict.dry_run is True
    assert "Dry run, not enforced" in verdict.reason
    row = _row(evaluator, verdict.verdict_id)
    assert (row["stage"], row["dry_run"]) == ("observe", True)
    assert verdict.project_stage == "enforce", "A dry run still learns the stage a managed hook would meet"


def test_an_unlabelled_project_is_on_the_default_and_never_looked_up(monkeypatch) -> None:
    class _Counting(DynamoDBSessionRepository):
        reads = 0

        def load_project_config(self, project):
            self.reads += 1
            return super().load_project_config(project)

    repo = _Counting()
    evaluator = GovernanceEvaluator(session_repo=repo)
    verdict = evaluator.evaluate_tool_call(_call("stage-unlabelled", project="unlabelled"))
    assert verdict.project_stage == "enforce"
    assert repo.reads == 0


# ---------------------------------------------------------------- how fast a change lands


def test_a_stage_is_read_at_most_once_per_interval() -> None:
    class _Counting(DynamoDBSessionRepository):
        reads = 0

        def load_project_config(self, project):
            self.reads += 1
            return super().load_project_config(project)

    repo = _Counting()
    evaluator = GovernanceEvaluator(session_repo=repo)
    for index in range(4):
        evaluator.evaluate_tool_call(_call(f"stage-count-{index}", {"file_path": "README.md"}, tool="Read", action="FILE_READ"))
    assert repo.reads == 1

    evaluator._project_configs[PROJECT].read_at -= evaluator_module.RULES_REFRESH_SECONDS + 1
    evaluator.evaluate_tool_call(_call("stage-count-late", {"file_path": "README.md"}, tool="Read", action="FILE_READ"))
    assert repo.reads == 2


def test_a_promotion_applies_at_once_here_and_within_the_interval_elsewhere(repo, observe_default) -> None:
    took_it = GovernanceEvaluator(session_repo=repo)
    elsewhere = GovernanceEvaluator(session_repo=repo)
    assert elsewhere.evaluate_tool_call(_call("stage-elsewhere-1")).status == "APPROVED"

    _configure(took_it, "enforce")
    assert took_it.evaluate_tool_call(_call("stage-here")).status.startswith("BLOCKED"), "Same container: at once"
    assert elsewhere.evaluate_tool_call(_call("stage-elsewhere-2")).status == "APPROVED", (
        "Inside the interval the other container still holds what it read"
    )
    elsewhere._project_configs[PROJECT].read_at -= evaluator_module.RULES_REFRESH_SECONDS + 1
    assert elsewhere.evaluate_tool_call(_call("stage-elsewhere-3")).status.startswith("BLOCKED")


def test_a_failed_re_read_keeps_the_stage_a_container_holds() -> None:
    """Falling back to the default on a transient error would silently change a project's stage."""

    class _Flaky(DynamoDBSessionRepository):
        failing = False

        def load_project_config(self, project):
            if self.failing:
                raise ConnectionError("simulated DynamoDB outage")
            return super().load_project_config(project)

    repo = _Flaky()
    evaluator = GovernanceEvaluator(session_repo=repo)
    _configure(evaluator, "observe")
    repo.failing = True
    evaluator._project_configs[PROJECT].read_at -= evaluator_module.RULES_REFRESH_SECONDS + 1
    assert evaluator.evaluate_tool_call(_call("stage-flaky")).status == "APPROVED"


def test_an_expired_sandbox_reads_as_unconfigured(repo, monkeypatch) -> None:
    repo.save_project_config("Acme-Sandbox-0a1b2c3d", stages.new_config(NOW, sandbox=True), ttl_seconds=60)
    assert repo.load_project_config("Acme-Sandbox-0a1b2c3d")["sandbox"] is True
    import time as time_module

    real = time_module.time
    monkeypatch.setattr(time_module, "time", lambda: real() + 120)
    assert repo.load_project_config("Acme-Sandbox-0a1b2c3d") is None
    assert "Acme-Sandbox-0a1b2c3d" not in repo.list_project_configs()


# ---------------------------------------------------------------- the hook's mode


def test_the_hook_mode_is_echoed_onto_the_row(evaluator) -> None:
    request = ToolCallRequestDTO.from_payload(
        {"session_id": "stage-mode", "project_name": PROJECT, "tool_name": "Read", "action_type": "FILE_READ",
         "arguments": {"file_path": "README.md"}, "origin": "hook", "agent": "codex", "hook_mode": "managed"}
    )
    assert request.hook_mode == "managed" and request.warnings == []
    verdict = evaluator.evaluate_tool_call(request)
    assert _row(evaluator, verdict.verdict_id)["hook_mode"] == "managed"


@pytest.mark.parametrize("sent, warned", [(None, False), ("", False), ("shadow", True), (["observe"], True)])
def test_a_hook_mode_outside_the_set_is_unknown(sent, warned) -> None:
    body = {"session_id": "stage-mode-odd", "project_name": PROJECT}
    if sent is not None:
        body["hook_mode"] = sent
    request = ToolCallRequestDTO.from_payload(body)
    assert request.hook_mode == "unknown"
    assert bool(request.warnings) is warned


# ---------------------------------------------------------------- the pure rules


def test_a_promotion_observes_every_rule_it_did_not_pick() -> None:
    keys = stages.project_rule_keys([{"id": "python-domain-stays-pure"}, {"id": "java-domain-stays-pure"}])
    assert keys == ["python-domain-stays-pure", "java-domain-stays-pure", "LOOP", "PROTECTED_PATH", "UNREADABLE_WRITE", "BUDGET"]
    config = stages.promoted(None, NOW, "a1b2c3d4", ["python-domain-stays-pure", "LOOP"], keys)
    assert config["stage"] == "enforce"
    assert config["observe_rules"] == ["java-domain-stays-pure", "PROTECTED_PATH", "UNREADABLE_WRITE", "BUDGET"]
    assert config["promoted_at"] == NOW
    assert config["history"][-1] == {
        "at": NOW, "action": "promote", "by": "a1b2c3d4",
        "enforce": ["python-domain-stays-pure", "LOOP"], "observe": config["observe_rules"],
    }


def test_the_history_keeps_the_last_twenty() -> None:
    keys = stages.project_rule_keys([])
    config = None
    for index in range(25):
        config = stages.demoted(config, f"2026-09-22T08:{index:02d}:00+00:00", "anonymous", keys)
    assert len(config["history"]) == stages.HISTORY_KEPT
    assert config["history"][0]["at"] == "2026-09-22T08:05:00+00:00"
