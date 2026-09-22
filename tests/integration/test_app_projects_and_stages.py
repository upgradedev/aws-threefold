"""A project is listed, read for readiness, promoted and demoted through the routes.

The walk the dashboard makes: a connected repository's first calls land in
observe, the project page shows what each rule would have refused, the operator
promotes it with the rules they trust and the others keep observing, and one
step demotes it again. Driven through `lambda_handler` with API Gateway v2
events, as deployed.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import hashlib

import pytest

from test_app_support import DOMAIN_WRITE, JAVA_WRITE, README, fresh_project, get, hook_call, post, request


@pytest.fixture
def observe_default(monkeypatch):
    monkeypatch.delenv("DEFAULT_HOOK_STAGE", raising=False)


def test_a_new_projects_first_calls_are_observed_and_listed(observe_default) -> None:
    project = fresh_project()
    verdict = hook_call(project, f"{project}-s1", DOMAIN_WRITE)
    assert verdict["status"] == "APPROVED"
    assert verdict["project_stage"] == "observe"
    hook_call(project, f"{project}-s2", README, tool="Read", action="FILE_READ", agent="codex", hook_mode="observe",
              dry_run=True)

    listed = {row["project"]: row for row in get("/api/projects")["projects"]}
    row = listed[project]
    assert row["configured"] is False and row["stage"] == "observe"
    assert (row["calls"], row["refused"], row["would_refuse"], row["needs_review"]) == (2, 0, 1, 1)
    assert sorted(row["agents"]) == ["claude-code", "codex"]
    assert sorted(row["hook_modes"]) == ["managed", "observe"]
    assert row["last_seen"]


def test_the_project_page_reads_readiness_per_rule(observe_default) -> None:
    project = fresh_project()
    hook_call(project, f"{project}-s", DOMAIN_WRITE)
    page = get(f"/api/projects/{project}")
    assert page["project"] == project and page["config"] is None
    rules = {row["rule_key"]: row for row in page["readiness"]["rules"]}
    assert rules["python-domain-stays-pure"]["state"] == "needs_review"
    assert rules["python-domain-stays-pure"]["would_refuse"] == 1
    assert rules["java-domain-stays-pure"]["state"] == "quiet"
    assert set(rules) >= {"LOOP", "PROTECTED_PATH", "UNREADABLE_WRITE", "BUDGET"}
    summary = page["readiness"]["summary"]
    assert (summary["stage"], summary["days_observed"], summary["calls_observed"], summary["would_have_refused"]) == (
        "observe", 1, 1, 1,
    )


def test_a_stage_set_by_post_decides_the_next_call(observe_default) -> None:
    project = fresh_project()
    saved = post(f"/api/projects/{project}", {"stage": "enforce"})
    assert saved["config"]["stage"] == "enforce" and saved["config"]["created_at"]
    verdict = hook_call(project, f"{project}-s", DOMAIN_WRITE)
    assert verdict["status"] == "BLOCKED_BOUNDARY_VIOLATION"
    assert verdict["project_stage"] == "enforce"

    listed = {row["project"]: row for row in get("/api/projects")["projects"]}
    assert listed[project]["configured"] is True and listed[project]["refused"] == 1


def test_a_promotion_enforces_what_was_picked_and_a_demotion_undoes_it(observe_default) -> None:
    project = fresh_project()
    promoted = post(f"/api/projects/{project}/promote", {"enforce": ["python-domain-stays-pure", "LOOP"]},
                    headers={"X-API-Key": "operator-key-1"})
    config = promoted["config"]
    assert config["stage"] == "enforce"
    assert config["observe_rules"] == [
        "java-domain-stays-pure", "dotnet-domain-stays-pure", "web-domain-stays-pure",
        "PROTECTED_PATH", "UNREADABLE_WRITE", "BUDGET",
    ]
    by = hashlib.sha256(b"operator-key-1").hexdigest()[:8]
    assert config["history"][-1]["action"] == "promote" and config["history"][-1]["by"] == by
    assert "operator-key-1" not in str(config), "The credential is never stored, only a short hash of it"

    assert hook_call(project, f"{project}-p", DOMAIN_WRITE)["status"] == "BLOCKED_BOUNDARY_VIOLATION"
    watched = hook_call(project, f"{project}-j", JAVA_WRITE)
    assert watched["status"] == "APPROVED", "A rule left observing records rather than refuses"
    assert watched["observations"]

    demoted = post(f"/api/projects/{project}/demote", {})
    assert demoted["config"]["stage"] == "observe"
    assert [entry["action"] for entry in demoted["config"]["history"]] == ["promote", "demote"]
    assert demoted["config"]["history"][-1]["by"] == hashlib.sha256(b"operator-key-1").hexdigest()[:8]
    assert hook_call(project, f"{project}-p2", DOMAIN_WRITE)["status"] == "APPROVED"

    page = get(f"/api/projects/{project}")
    assert page["config"]["demoted_at"] and page["config"]["promoted_at"]
    assert {row["mode_now"] for row in page["readiness"]["rules"]} == {"observe"}


def test_a_retried_promotion_with_its_idempotency_key_is_one_history_entry() -> None:
    project = fresh_project()
    retry = {"Idempotency-Key": f"{project}-promote-1"}
    first = post(f"/api/projects/{project}/promote", {"enforce": ["LOOP"]}, headers=retry)
    again = post(f"/api/projects/{project}/promote", {"enforce": ["LOOP"]}, headers=retry)
    assert again == first
    assert len(get(f"/api/projects/{project}")["config"]["history"]) == 1


def test_readiness_says_which_rules_enforce_after_a_promotion() -> None:
    project = fresh_project()
    post(f"/api/projects/{project}/promote", {"enforce": ["python-domain-stays-pure"]})
    rules = {row["rule_key"]: row for row in get(f"/api/projects/{project}")["readiness"]["rules"]}
    assert rules["python-domain-stays-pure"]["mode_now"] == "enforce"
    assert rules["LOOP"]["mode_now"] == "observe"


def test_a_rule_that_refused_while_enforcing_is_ready_and_shows_its_refusals() -> None:
    """The live probe found a promoted rule reading Ready over a row of zeros.

    Its refusals count towards whether it flagged anything, so the row carries
    them too, and the state can be checked against the numbers beside it.
    """
    project = fresh_project()
    post(f"/api/projects/{project}/promote", {"enforce": ["python-domain-stays-pure"]})
    assert hook_call(project, f"{project}-s", DOMAIN_WRITE)["status"] == "BLOCKED_BOUNDARY_VIOLATION"
    rules = {row["rule_key"]: row for row in get(f"/api/projects/{project}")["readiness"]["rules"]}
    row = rules["python-domain-stays-pure"]
    assert (row["would_refuse"], row["refused"], row["unreviewed"], row["state"]) == (0, 1, 0, "ready")
    assert rules["LOOP"]["refused"] == 0 and rules["LOOP"]["state"] == "quiet"


def test_observe_rules_can_be_set_directly() -> None:
    project = fresh_project()
    post(f"/api/projects/{project}", {"stage": "enforce", "observe_rules": ["python-domain-stays-pure"]})
    assert hook_call(project, f"{project}-s", DOMAIN_WRITE)["status"] == "APPROVED"
    assert hook_call(project, f"{project}-j", JAVA_WRITE)["status"] == "BLOCKED_BOUNDARY_VIOLATION"


@pytest.mark.parametrize(
    "path, body, field",
    [
        ("", {"stage": "shadow"}, "stage"),
        ("", {"observe_rules": ["no-such-rule"]}, "observe_rules"),
        ("", {"observe_rules": "LOOP"}, "observe_rules"),
        ("/promote", {}, "enforce"),
        ("/promote", {"enforce": ["CREDENTIAL"]}, "enforce"),
        ("/promote", {"enforce": [1]}, "enforce"),
    ],
)
def test_a_configuration_the_caller_can_correct_is_a_400(path, body, field) -> None:
    project = fresh_project()
    status, problem = request("POST", f"/api/projects/{project}{path}", body)
    assert status == 400, problem
    assert problem["type"] == "urn:threefold:error:bad-request"
    assert problem["invalid_params"][0]["name"] == field


def test_a_name_outside_the_pattern_cannot_be_configured_or_read() -> None:
    status, problem = request("POST", "/api/projects/Not An Acme Name", {"stage": "enforce"})
    assert status == 400, problem
    status, problem = request("GET", "/api/projects/not-acme")
    assert status == 404 and problem["type"] == "urn:threefold:error:project-not-found"


def test_the_unlabelled_bucket_has_a_page_but_no_configuration() -> None:
    page = get("/api/projects/unlabelled")
    assert page["config"] is None
    status, _ = request("POST", "/api/projects/unlabelled/promote", {"enforce": []})
    assert status == 400


def test_a_malformed_body_is_the_callers_problem() -> None:
    status, problem = request("POST", f"/api/projects/{fresh_project()}/demote", "[1, 2")
    assert status == 400 and problem["invalid_params"][0]["name"] == "body"


def test_an_unknown_method_or_path_falls_through_to_not_found() -> None:
    # Refused before routing now: every method but GET under /api/projects needs the operator.
    assert request("DELETE", "/api/projects")[0] in (401, 404, 405)
    assert request("GET", "/api/sandbox")[0] == 404
    assert request("GET", f"/api/projects/{fresh_project()}/promote")[0] == 404
