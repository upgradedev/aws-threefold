"""What a stranger may read about a project and a person.

Both fields arrive from the caller and both end up on pages anyone can open.
A project name that does not match the stack's AllowedProjectPattern is stored,
counted and shown as "unlabelled", which keeps a real repository name off the
public console and stops a caller inventing a new CloudWatch dimension with
every request. A developer is shown only as a short hash, whatever was sent.
Both rules are applied on the way out as well as on the way in, because rows
written before the rules existed are still in the table.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from threefold.application.labels import DEFAULT_PROJECT_PATTERN, developer_hash, project_label
from threefold.domain.models import AgentSession
from threefold.interfaces.api_handlers import _evaluator, lambda_handler

RAW_PROJECT = "Acme Internal_Payments"  # spaces and an underscore: outside the pattern
RAW_DEVELOPER = "acme-dev-jordan"


def _evaluate(session_id: str, **overrides) -> dict:
    body = {
        "session_id": session_id,
        "developer_id": RAW_DEVELOPER,
        "project_name": "Acme-Labels",
        "tool_name": "view_file",
        "action_type": "FILE_READ",
        "arguments": {"path": "README.md"},
        "projected_input_tokens": 100,
        "projected_output_tokens": 50,
        "budget_usd": 5.0,
    }
    body.update(overrides)
    response = lambda_handler(
        {
            "rawPath": "/prod/evaluate-tool-call",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body),
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
        }
    )
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def _get(path: str, query: dict | None = None) -> dict:
    response = lambda_handler(
        {
            "rawPath": f"/prod{path}",
            "headers": {},
            "queryStringParameters": query,
            "requestContext": {"http": {"method": "GET"}, "stage": "prod"},
        }
    )
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def _ledger_row(session_id: str) -> dict:
    rows = [r for r in _evaluator.list_decisions(days=1) if r["session_id"] == session_id]
    assert rows, f"No ledger row for {session_id}"
    return rows[0]


def _emf(captured: str, metric: str = "ToolCallsEvaluated") -> dict:
    """The metric record the handler wrote to stdout for a tool call."""
    for line in captured.splitlines():
        if line.startswith("{") and metric in line:
            return json.loads(line)
    raise AssertionError(f"No EMF record carrying {metric} was emitted")


def test_a_name_outside_the_pattern_is_stored_as_unlabelled_and_the_caller_is_told() -> None:
    verdict = _evaluate("labels-unlabelled", project_name=RAW_PROJECT)
    assert any("AllowedProjectPattern" in w for w in verdict["warnings"])
    assert RAW_PROJECT not in json.dumps(verdict), "The name was echoed back somewhere"
    assert _ledger_row("labels-unlabelled")["project_name"] == "unlabelled"


def test_a_name_inside_the_pattern_is_kept_as_it_is() -> None:
    verdict = _evaluate("labels-kept", project_name="Acme-Payments")
    assert verdict["warnings"] == []
    assert _ledger_row("labels-kept")["project_name"] == "Acme-Payments"


def test_a_call_that_names_no_project_is_unlabelled_rather_than_attributed_to_one() -> None:
    """It used to default to Acme-Core, which attributed anonymous calls to a real project."""
    body_without_project = {"session_id": "labels-missing", "tool_name": "view_file"}
    response = lambda_handler(
        {
            "rawPath": "/prod/evaluate-tool-call",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body_without_project),
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
        }
    )
    verdict = json.loads(response["body"])
    assert any("was not sent" in w for w in verdict["warnings"])
    assert _ledger_row("labels-missing")["project_name"] == "unlabelled"


def test_the_metric_dimension_carries_the_label_rather_than_the_request(capsys) -> None:
    """A dimension a caller controls is a new metric, and a new charge, per request."""
    _evaluate("labels-metric", project_name=RAW_PROJECT)
    record = _emf(capsys.readouterr().out)
    assert record["Project"] == "unlabelled"


def test_the_universal_adapter_logs_the_tool_name_rather_than_dimensioning_on_it(capsys) -> None:
    """The same cardinality rule as Project: a caller's string is not a dimension.

    A tool name arrives from the caller, so used as a dimension every invented
    name was a new CloudWatch metric, and a new charge. Kept as a property it is
    still searchable in Logs Insights. The dimension set is asserted too, because
    moving a key out of it silently retires any alarm that was keyed on it.
    """
    response = lambda_handler(
        {
            "rawPath": "/prod/adapter/universal-tool-call",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {
                    "session_id": "labels-universal",
                    "name": "acme-invented-tool-name",
                    "arguments": json.dumps({"path": "README.md"}),
                }
            ),
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
        }
    )
    assert response["statusCode"] == 200, response["body"]

    record = _emf(capsys.readouterr().out, "UniversalToolEvaluated")
    assert record["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Format"]]
    assert record["Format"] == "Universal"
    assert record["Tool"] == "acme-invented-tool-name", "The tool name is still recorded"


def test_the_pattern_is_the_one_the_stack_was_deployed_with(monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_PROJECT_PATTERN", r"^Team-[a-z]+$")
    assert project_label("Team-alpha") == "Team-alpha"
    assert project_label("Acme-Payments") == "unlabelled", "The stack's own pattern governs"


def test_a_pattern_that_does_not_compile_falls_back_rather_than_admitting_everything(monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_PROJECT_PATTERN", "^Acme-[unclosed")
    assert project_label("Acme-Payments") == "Acme-Payments"
    assert project_label("anything at all") == "unlabelled"


@pytest.mark.parametrize(
    "name",
    [
        "Acme-Payments\nAcme-Other",  # a second line must not ride in on a $ anchor
        "Acme-Payments ",
        "acme-payments",
        "Acme-" + "x" * 400,
        "",
    ],
)
def test_names_that_only_look_like_the_pattern_are_unlabelled(name: str) -> None:
    assert project_label(name) == "unlabelled"


def test_the_default_pattern_admits_the_synthetic_namespace_the_demo_uses() -> None:
    assert DEFAULT_PROJECT_PATTERN == r"^Acme-[A-Za-z0-9-]{1,40}$"
    assert project_label("Acme-Core") == "Acme-Core"


# ------------------------------------------------------- what a reader is shown


def test_a_developer_is_shown_only_as_a_short_stable_hash() -> None:
    assert developer_hash(RAW_DEVELOPER) == hashlib.sha256(RAW_DEVELOPER.encode()).hexdigest()[:8]
    assert len(developer_hash(RAW_DEVELOPER)) == 8
    assert developer_hash("anonymous") != "anonymous", "Every value is hashed, this one included"
    assert developer_hash("") == "", "A row with no developer stays unattributed"


def test_the_console_shows_no_raw_project_or_developer_even_for_old_rows() -> None:
    """Rows written before the labels existed are still in the table."""
    import datetime

    _evaluator.session_repo.record_decision(
        {
            "verdict_id": "V-OLD-001",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "session_id": "labels-old-row",
            "developer_id": RAW_DEVELOPER,
            "project_name": RAW_PROJECT,
            "tool_name": "Write",
            "action_type": "FILE_WRITE",
            "status": "BLOCKED_BOUNDARY_VIOLATION",
            "rule": "ARCHITECTURAL_BOUNDARY_SAFE",
            "reason": "Clean Architecture violation",
            "target": "src/domain/order.py",
            "cost_usd": 0.01,
        }
    )
    payload = _get("/api/insights")
    body = json.dumps(payload)
    assert RAW_PROJECT not in body
    assert RAW_DEVELOPER not in body
    assert any(p["project"] == "unlabelled" for p in payload["by_project"])
    assert any(p["developer"] == developer_hash(RAW_DEVELOPER) for p in payload["by_developer"])
    refusal = next(r for r in payload["recent_refusals"] if r["session_id"] == "labels-old-row")
    assert refusal["project_name"] == "unlabelled"
    assert refusal["developer_id"] == developer_hash(RAW_DEVELOPER)


def test_the_sessions_listing_and_its_detail_show_the_same_labels() -> None:
    session = AgentSession(
        session_id="labels-old-session",
        developer_id=RAW_DEVELOPER,
        project_name=RAW_PROJECT,
        budget_usd=5.0,
    )
    _evaluator.session_repo.save_session(session, force=True)

    listing = _get("/api/sessions", {"limit": "200"})
    body = json.dumps(listing)
    assert RAW_PROJECT not in body and RAW_DEVELOPER not in body
    row = next(s for s in listing["sessions"] if s["session_id"] == "labels-old-session")
    assert row["project_name"] == "unlabelled"
    assert row["developer_id"] == developer_hash(RAW_DEVELOPER)

    # The detail the page links to from that row has to agree with it.
    detail = _get("/sessions/labels-old-session")
    assert detail["project_name"] == "unlabelled"
