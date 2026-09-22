"""The overview reads rollups; the call lists read the ledger, a page at a time.

A filtered list may read many rows to return a few, so a request reads a
bounded number of ledger pages and an opaque cursor resumes exactly after the
last row it returned, across the day partitions, never skipping one. Every row
leaves through `public_row`, as the rows of /api/insights do. Driven through
`lambda_handler` with API Gateway v2 events, as deployed.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import datetime

import pytest

from test_app_support import DOMAIN_WRITE, JAVA_WRITE, README, fresh_project, get, hook_call, request
from threefold.application import ledger
from threefold.application.evaluator import GovernanceEvaluator
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository
from threefold.interfaces import api_handlers

YESTERDAY = datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=1)


@pytest.fixture(autouse=True)
def _a_ledger_of_its_own(monkeypatch):
    """Each test reads a ledger holding only its own calls.

    The handler's evaluator is shared by the whole suite, and a day's partition
    of it holds every call any test made today, so paging through it would
    measure the suite rather than the paging.
    """
    monkeypatch.setattr(api_handlers, "_evaluator", GovernanceEvaluator(session_repo=DynamoDBSessionRepository()))


@pytest.fixture
def project(monkeypatch) -> str:
    """A fresh project in the deployed default, observe, with a known mix of calls."""
    monkeypatch.delenv("DEFAULT_HOOK_STAGE", raising=False)
    name = fresh_project("Acme-Ledger")
    hook_call(name, f"{name}-cc", DOMAIN_WRITE, developer="c0ffee000001")
    hook_call(name, f"{name}-cc", README, tool="Read", action="FILE_READ")
    hook_call(name, f"{name}-cx", JAVA_WRITE, agent="codex")
    hook_call(name, f"{name}-cx", {"file_path": ".env"}, tool="Read", action="FILE_READ", agent="codex", origin="page")
    hook_call(name, f"{name}-ag", {"file_path": "src/web/app.ts", "content": "export const a = 1;\n"},
              agent="antigravity")
    return name


def _items(**query) -> list:
    return get("/api/decisions", **query)["items"]


# ---------------------------------------------------------------- the overview


def test_the_overview_totals_come_from_the_rollups(project) -> None:
    payload = get("/api/overview", project=project, days=7)
    assert payload["source"] == "rollups" and payload["window_days"] == 7
    totals = payload["totals"]
    assert (totals["calls"], totals["approved"], totals["refused"], totals["would_refuse"]) == (5, 2, 1, 2)
    assert (totals["needs_review"], totals["false_alarms"], totals["projects"], totals["agents"]) == (2, 0, 1, 3)
    assert len(payload["series"]) == 7
    assert payload["series"][-1]["observed"] == 2 and payload["series"][-1]["refused"] == 1
    assert {row["rule_key"] for row in payload["by_rule"]} == {
        "python-domain-stays-pure", "java-domain-stays-pure", "PROTECTED_PATH",
    }
    assert {row["origin"]: row["calls"] for row in payload["by_origin"]} == {"hook": 4, "page": 1}
    assert payload["by_project"][0]["project"] == project
    assert payload["stages"] == {"observe": 1, "enforce": 0}


def test_the_overview_window_is_clamped() -> None:
    assert get("/api/overview", days=90)["window_days"] == 30
    assert get("/api/overview", days="nope")["window_days"] == 7


def test_the_insights_the_console_reads_are_unchanged(project) -> None:
    insights = get("/api/insights", days=1)
    assert {"totals", "by_rule", "by_project", "recent_refusals", "coverage"} <= set(insights)


# ---------------------------------------------------------------- the list and its filters


def test_every_row_is_reduced_for_a_public_page_and_carries_the_new_fields(project) -> None:
    rows = _items(project=project)
    assert len(rows) == 5
    for row in rows:
        assert row["project_name"] == project
        assert len(row["developer_id"]) == 8, "A developer is shown only as a short hash"
        assert {"rule_key", "stage", "hook_mode", "review", "reviewed_at", "review_note", "category",
                "category_label"} <= set(row)
        assert "reviewed_by" not in row
    assert "c0ffee000001" not in str(rows)


@pytest.mark.parametrize(
    "query, expected",
    [
        ({"kind": "refused"}, 1),
        ({"kind": "observed"}, 2),
        ({"kind": "approved"}, 2),
        ({"rule": "java-domain-stays-pure"}, 1),
        ({"agent": "codex"}, 2),
        ({"review": "unreviewed"}, 5),
        ({"review": "correct"}, 0),
    ],
)
def test_each_filter_narrows_the_list(project, query, expected) -> None:
    assert len(_items(project=project, **query)) == expected


def test_a_session_filter_needs_no_project(project) -> None:
    rows = _items(session=f"{project}-cx")
    assert len(rows) == 2 and {row["agent"] for row in rows} == {"codex"}


def test_a_row_reads_as_the_category_its_rule_key_names(project) -> None:
    refused = _items(project=project, kind="refused")[0]
    assert (refused["rule_key"], refused["category"]) == ("PROTECTED_PATH", "PROTECTED_PATH")
    observed = _items(project=project, rule="python-domain-stays-pure")[0]
    assert observed["category"] == "LAYERING" and observed["stage"] == "observe"


@pytest.mark.parametrize("query", [{"kind": "blocked"}, {"review": "maybe"}, {"cursor": "not-a-cursor"}])
def test_a_filter_or_cursor_it_does_not_know_is_a_400(query) -> None:
    status, problem = request("GET", "/api/decisions", query=query)
    assert status == 400, problem


# ---------------------------------------------------------------- pages


def _record_yesterday(project: str, count: int) -> None:
    repo = api_handlers._evaluator.session_repo
    for index in range(count):
        repo.record_decision({
            "verdict_id": f"{project}-y{index}", "timestamp": f"{YESTERDAY}T10:00:0{index}+00:00",
            "session_id": f"{project}-old", "project_name": project, "status": "APPROVED",
            "agent": "claude-code", "origin": "hook",
        })


def test_pages_follow_one_another_across_days_without_repeating_or_skipping(project) -> None:
    _record_yesterday(project, 3)
    seen, cursor, pages = [], None, 0
    while True:
        query = {"project": project, "limit": "3", "days": "2"}
        if cursor:
            query["cursor"] = cursor
        body = get("/api/decisions", **query)
        pages += 1
        seen.extend(row["verdict_id"] for row in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
        assert pages < 10
    assert len(seen) == 8 and len(set(seen)) == 8, "Five from today and three from yesterday, each once"
    assert seen[-3:] == [f"{project}-y2", f"{project}-y1", f"{project}-y0"], "Newest first, today before yesterday"


def test_a_window_of_one_day_does_not_reach_yesterday(project) -> None:
    _record_yesterday(project, 2)
    assert len(_items(project=project, days=1)) == 5


def test_a_request_that_runs_out_of_pages_says_where_to_go_on(project, monkeypatch) -> None:
    monkeypatch.setattr(ledger, "PAGE_SIZE", 1)
    monkeypatch.setattr(ledger, "MAX_PAGES_PER_REQUEST", 2)
    first = get("/api/decisions", project=project, kind="refused")
    total = list(first["items"])
    cursor = first["next_cursor"]
    while cursor:
        body = get("/api/decisions", project=project, kind="refused", cursor=cursor)
        total.extend(body["items"])
        cursor = body["next_cursor"]
    assert [row["rule_key"] for row in total] == ["PROTECTED_PATH"]


def test_a_legacy_row_is_given_its_rule_key_on_the_way_out() -> None:
    name = fresh_project("Acme-Legacy")
    api_handlers._evaluator.session_repo.record_decision({
        "verdict_id": f"{name}-legacy", "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "session_id": f"{name}-s", "project_name": name, "status": "BLOCKED_BOUNDARY_VIOLATION",
        "rule": "ARCHITECTURAL_BOUNDARY_SAFE",
        "reason": "Clean Architecture violation: Layering rule 'acme-billing-core' refuses this write: ...",
    })
    row = _items(project=name)[0]
    assert row["rule_key"] == "acme-billing-core" and row["hook_mode"] == "unknown" and row["stage"] == "enforce"


# ---------------------------------------------------------------- one decision


def test_one_decision_comes_with_its_session_and_its_rule(project) -> None:
    row = _items(project=project, rule="python-domain-stays-pure")[0]
    body = get("/api/decision", timestamp=row["timestamp"], verdict_id=row["verdict_id"])
    assert body["decision"]["verdict_id"] == row["verdict_id"]
    assert len(body["decision"]["developer_id"]) == 8
    assert body["session"]["session_id"] == f"{project}-cc" and body["session"]["is_tripped"] is False
    assert body["rule"]["id"] == "python-domain-stays-pure"
    assert "when_path_matches" in body["rule"]


def test_a_gate_decision_has_no_rule_definition(project) -> None:
    row = _items(project=project, kind="refused")[0]
    assert get("/api/decision", timestamp=row["timestamp"], verdict_id=row["verdict_id"])["rule"] is None


def test_an_unknown_decision_is_a_404_and_a_missing_key_a_400() -> None:
    status, problem = request("GET", "/api/decision", query={"timestamp": "2026-09-22T00:00:00+00:00",
                                                              "verdict_id": "V-nobody"})
    assert status == 404 and problem["type"] == "urn:threefold:error:decision-not-found"
    status, problem = request("GET", "/api/decision", query={"verdict_id": "V-nobody"})
    assert status == 400 and problem["invalid_params"][0]["name"] == "timestamp"
