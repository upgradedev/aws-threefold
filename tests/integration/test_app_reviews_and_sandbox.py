"""An operator labels what each rule flagged, and a visitor can try it all in a sandbox.

A label lives on the ledger row it is about, only a row of the project named in
the path can be labelled through it, and who labelled it is kept as a short
hash of the credential presented, never the credential. The labels move the
review counts the tiles read. The sandbox is a real project, seeded through the
real evaluator, with one flagged call a reviewer should reject.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import hashlib
import time

import pytest

from test_app_support import DOMAIN_WRITE, JAVA_WRITE, README, fresh_project, get, hook_call, post, request
from threefold.application.projects import SANDBOX_PATTERN
from threefold.infrastructure.dynamo_repo import PROJECT_CONFIG_PREFIX
from threefold.interfaces.api_handlers import _evaluator

OPERATOR = {"X-API-Key": "operator-key-1"}


@pytest.fixture
def flagged(monkeypatch):
    """A project in observe with two would-refuse calls, under two rules, and one clean call."""
    monkeypatch.delenv("DEFAULT_HOOK_STAGE", raising=False)
    project = fresh_project("Acme-Review")
    hook_call(project, f"{project}-s", DOMAIN_WRITE)
    hook_call(project, f"{project}-s", JAVA_WRITE, agent="codex")
    hook_call(project, f"{project}-s", README, tool="Read", action="FILE_READ")
    rows = {row["rule_key"]: row for row in get("/api/decisions", project=project)["items"]}
    return project, rows


def _item(row: dict, label: str, **extra) -> dict:
    return dict({"timestamp": row["timestamp"], "verdict_id": row["verdict_id"], "label": label}, **extra)


def test_a_label_is_stored_on_the_row_and_counted_in_the_tiles(flagged) -> None:
    project, rows = flagged
    answer = post(f"/api/projects/{project}/reviews", {"items": [
        _item(rows["python-domain-stays-pure"], "correct", note="The domain reached for boto3."),
        _item(rows["java-domain-stays-pure"], "false_alarm"),
    ]}, headers=OPERATOR)
    assert answer == {"updated": 2, "skipped": []}

    labelled = {row["rule_key"]: row for row in get("/api/decisions", project=project)["items"]}
    assert labelled["python-domain-stays-pure"]["review"] == "correct"
    assert labelled["python-domain-stays-pure"]["review_note"] == "The domain reached for boto3."
    assert labelled["python-domain-stays-pure"]["reviewed_at"]
    assert labelled["java-domain-stays-pure"]["review"] == "false_alarm"

    totals = get("/api/overview", project=project)["totals"]
    assert (totals["needs_review"], totals["false_alarms"]) == (0, 1)
    rules = {row["rule_key"]: row for row in get(f"/api/projects/{project}")["readiness"]["rules"]}
    assert rules["python-domain-stays-pure"]["state"] == "ready"
    assert rules["java-domain-stays-pure"]["state"] == "noisy"
    summary = get(f"/api/projects/{project}")["readiness"]["summary"]
    assert (summary["reviewed"], summary["false_alarms"], summary["false_alarm_rate"]) == (2, 1, 0.5)


def test_who_labelled_is_a_hash_of_the_credential_never_the_credential(flagged) -> None:
    project, rows = flagged
    row = rows["python-domain-stays-pure"]
    post(f"/api/projects/{project}/reviews", {"items": [_item(row, "correct")]}, headers=OPERATOR)
    stored = _evaluator.session_repo._memory_store[f"DECISION#{row['timestamp'][:10]}#{row['timestamp']}#{row['verdict_id']}"]
    assert stored["reviewed_by"] == hashlib.sha256(b"operator-key-1").hexdigest()[:8]
    assert "operator-key-1" not in str(stored)

    # Nobody labels anonymously outside a sandbox: the access rules refuse the
    # write before it is routed, so the stored hash stays the operator's.
    status, _ = request("POST", f"/api/projects/{project}/reviews", {"items": [_item(row, "false_alarm")]},
                        headers={"X-API-Key": ""})
    assert status in (401, 403)
    assert stored["reviewed_by"] == hashlib.sha256(b"operator-key-1").hexdigest()[:8]


def test_clearing_a_label_puts_the_call_back_in_the_queue(flagged) -> None:
    project, rows = flagged
    row = rows["python-domain-stays-pure"]
    post(f"/api/projects/{project}/reviews", {"items": [_item(row, "false_alarm")]})
    post(f"/api/projects/{project}/reviews", {"items": [_item(row, "clear")]})
    cleared = get("/api/decisions", project=project, rule="python-domain-stays-pure")["items"][0]
    assert cleared["review"] is None and cleared["reviewed_at"] is None
    totals = get("/api/overview", project=project)["totals"]
    assert (totals["needs_review"], totals["false_alarms"]) == (2, 0)
    assert len(get("/api/decisions", project=project, kind="observed", review="unreviewed")["items"]) == 2


def test_a_label_changed_twice_is_counted_once(flagged) -> None:
    project, rows = flagged
    row = rows["java-domain-stays-pure"]
    for label in ("correct", "false_alarm", "false_alarm"):
        post(f"/api/projects/{project}/reviews", {"items": [_item(row, label)]})
    rules = {r["rule_key"]: r for r in get(f"/api/projects/{project}")["readiness"]["rules"]}
    assert (rules["java-domain-stays-pure"]["correct"], rules["java-domain-stays-pure"]["false_alarms"]) == (0, 1)


def test_an_item_that_cannot_be_labelled_is_skipped_with_its_reason(flagged) -> None:
    project, rows = flagged
    other, other_rows = fresh_project("Acme-Other"), None
    hook_call(other, f"{other}-s", DOMAIN_WRITE)
    other_rows = get("/api/decisions", project=other)["items"]
    clean = rows["NONE"]
    answer = post(f"/api/projects/{project}/reviews", {"items": [
        _item(other_rows[0], "correct"),
        _item(clean, "correct"),
        {"timestamp": "2026-09-22T00:00:00+00:00", "verdict_id": "V-nobody", "label": "correct"},
        _item(rows["python-domain-stays-pure"], "maybe"),
        _item(rows["python-domain-stays-pure"], "correct", note="x" * 201),
        "not an object",
    ]})
    reasons = [entry["reason"] for entry in answer["skipped"]]
    assert answer["updated"] == 0
    assert set(reasons) == {
        "other-project", "nothing-flagged", "not-found", "label-not-correct-false-alarm-or-clear",
        "note-longer-than-200", "not-an-object",
    }
    assert get("/api/decisions", project=other)["items"][0]["review"] is None, "The other project's row is untouched"


@pytest.mark.parametrize("body", [{}, {"items": []}, {"items": "all"}, {"items": [{}] * 101}])
def test_a_review_request_that_is_not_a_list_of_at_most_a_hundred_is_a_400(body) -> None:
    status, problem = request("POST", f"/api/projects/{fresh_project()}/reviews", body)
    assert status == 400 and problem["invalid_params"][0]["name"] == "items"


# ---------------------------------------------------------------- the sandbox


def test_a_sandbox_is_an_observed_project_seeded_through_the_evaluator() -> None:
    created = post("/api/sandbox", {})
    project = created["project"]
    assert SANDBOX_PATTERN.match(project)
    assert created["calls_seeded"] == 12
    assert created["url"] == f"dashboard.html#/projects/{project}"

    page = get(f"/api/projects/{project}")
    assert page["config"]["sandbox"] is True and page["config"]["stage"] == "observe", (
        "Observe whatever the stack's default is: the suite runs with enforce"
    )
    rows = get("/api/decisions", project=project)["items"]
    assert len(rows) == 12
    assert {row["agent"] for row in rows} == {"claude-code", "codex", "antigravity"}
    assert {row["origin"] for row in rows} == {"hook"}
    assert all(row["status"] == "APPROVED" for row in rows), "Observe refuses nothing"
    flagged = [row for row in rows if row["rule_key"] != "NONE"]
    assert len({row["rule_key"] for row in flagged}) >= 2
    false_alarm = [row for row in flagged if row["target"] == "tests/domain/test_order_totals.py"]
    assert [row["rule_key"] for row in false_alarm] == ["python-domain-stays-pure"]

    summary = page["readiness"]["summary"]
    assert summary["would_have_refused"] == len(flagged) and summary["rules_needing_review"] >= 2


def test_a_sandbox_expires_after_a_day() -> None:
    project = post("/api/sandbox", {})["project"]
    stored = _evaluator.session_repo._memory_store[f"{PROJECT_CONFIG_PREFIX}{project}#METADATA"]
    assert abs(stored["ttl"] - (time.time() + 24 * 3600)) < 120


def test_a_visitor_can_review_promote_and_demote_a_sandbox() -> None:
    project = post("/api/sandbox", {})["project"]
    rows = get("/api/decisions", project=project, kind="observed")["items"]
    items = [
        _item(row, "false_alarm" if row["target"].startswith("tests/domain/") else "correct") for row in rows
    ]
    assert post(f"/api/projects/{project}/reviews", {"items": items})["updated"] == len(rows)
    promoted = post(f"/api/projects/{project}/promote", {"enforce": ["java-domain-stays-pure", "PROTECTED_PATH"]})
    assert promoted["config"]["stage"] == "enforce" and promoted["config"]["sandbox"] is True
    stored = _evaluator.session_repo._memory_store[f"{PROJECT_CONFIG_PREFIX}{project}#METADATA"]
    assert stored["ttl"] <= time.time() + 24 * 3600 + 5, "Changing a sandbox's stage never extends its day"
    rules = {row["rule_key"]: row for row in get(f"/api/projects/{project}")["readiness"]["rules"]}
    assert rules["python-domain-stays-pure"]["state"] == "noisy"
    assert rules["java-domain-stays-pure"]["state"] == "ready"
    assert post(f"/api/projects/{project}/demote", {})["config"]["stage"] == "observe"


def test_a_sandbox_name_written_directly_is_still_a_sandbox() -> None:
    config = post("/api/projects/Acme-Sandbox-0badc0de", {"stage": "observe"})["config"]
    assert config["sandbox"] is True
    stored = _evaluator.session_repo._memory_store[f"{PROJECT_CONFIG_PREFIX}Acme-Sandbox-0badc0de#METADATA"]
    assert "ttl" in stored


def test_a_sandbox_is_unavailable_where_the_pattern_would_not_record_it(monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_PROJECT_PATTERN", r"^Team-[a-z]+$")
    status, problem = request("POST", "/api/sandbox", {})
    assert status == 409 and problem["type"] == "urn:threefold:error:sandbox-unavailable"
