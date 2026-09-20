"""The sessions console has to show sessions the service actually governed.

The page at /sessions.html renders nothing of its own: every row, and every field
in the detail panel, comes from these two endpoints. So the contract they answer
on is the page, and it is tested here rather than in a browser.
"""
from __future__ import annotations

import json
import urllib.parse

from threefold.interfaces.api_handlers import lambda_handler


def _evaluate(session_id: str, tool_name: str = "read_file") -> dict:
    response = lambda_handler(
        {
            "httpMethod": "POST",
            "path": "/evaluate-tool-call",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {
                    "session_id": session_id,
                    "developer_id": "console-test",
                    "project_name": "Acme-Console",
                    "tool_name": tool_name,
                    "action_type": "FILE_READ",
                    "arguments": {"path": "README.md"},
                    "projected_input_tokens": 100,
                    "projected_output_tokens": 50,
                    "budget_usd": 5.0,
                }
            ),
        }
    )
    assert response["statusCode"] == 200
    return json.loads(response["body"])


def _list_sessions(limit: int = 50) -> dict:
    response = lambda_handler(
        {
            "httpMethod": "GET",
            "path": "/api/sessions",
            "headers": {},
            "queryStringParameters": {"limit": str(limit)},
        }
    )
    assert response["statusCode"] == 200
    return json.loads(response["body"])


def test_a_governed_session_appears_in_the_listing() -> None:
    session_id = "console-listing-001"
    _evaluate(session_id)

    body = _list_sessions()
    assert body["count"] == len(body["sessions"])
    # The page prints this verbatim, so a reader can tell a durable row from one
    # that lives in a single container.
    assert body["persistence"] in ("dynamodb", "memory")

    listed = {s["session_id"]: s for s in body["sessions"]}
    assert session_id in listed, "A session that was evaluated must be listable"

    row = listed[session_id]
    for field in (
        "developer_id",
        "project_name",
        "total_cost_usd",
        "total_input_tokens",
        "total_output_tokens",
        "is_tripped",
        "trip_reason",
        "is_terminated",
        "created_at",
        "calls",
    ):
        assert field in row, f"The console renders {field} and the API must supply it"


def test_the_listing_respects_the_limit_the_page_sends() -> None:
    for index in range(3):
        _evaluate(f"console-limit-{index:03d}")

    body = _list_sessions(limit=2)
    assert len(body["sessions"]) <= 2


def test_a_session_id_that_must_be_escaped_still_reads_back() -> None:
    """A percent-encoded id must not be read as an id nobody has ever used.

    The browser escapes the id into the path. API Gateway hands the raw path to
    the function, so without decoding, `GET /sessions/{id}` would miss the real
    session, silently create an empty one, and the detail panel would report that
    invention as the operator's session.
    """
    session_id = "console fixture <angle> 'quote"
    _evaluate(session_id)

    encoded = urllib.parse.quote(session_id, safe="")
    response = lambda_handler(
        {
            "httpMethod": "GET",
            "path": f"/sessions/{encoded}",
            "headers": {},
        }
    )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["session_id"] == session_id
    assert body["project_name"] == "Acme-Console", "The real session was found, not a fresh one"
    assert body["tool_call_history_count"] >= 1


def test_the_kill_switch_reaches_an_escaped_session_id() -> None:
    session_id = "console freeze <target>"
    _evaluate(session_id)

    encoded = urllib.parse.quote(session_id, safe="")
    response = lambda_handler(
        {
            "httpMethod": "POST",
            "path": f"/sessions/{encoded}/terminate",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {"operator_name": "Sessions console operator", "reason": "Escaped id freeze"}
            ),
        }
    )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["session_id"] == session_id
    assert body["is_tripped"] is True
