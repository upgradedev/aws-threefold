"""Only the schedule starts a fleet tick: no HTTP route, method or body can.

The public stack's function answers anonymous HTTP requests and, where the
stack's DemoFleet parameter is true, a scheduled event every fifteen minutes.
A tick writes a batch of synthetic calls and acts as an operator, so a caller
of the API able to start one could fill the ledger at will. The handler
recognises the tick only as the exact event the schedule sends, which no HTTP
event can be: API Gateway, the edge and the local server all hand the function
an event with a request context, a path and headers, and put a body under
`body` as a string.

These tests drive the handler with every method on every path the router
names, in both event shapes the function receives, with the tick event as the
body, the query string, a header, base64 and inside another object, and then
through the local server over a real socket. The tick is counted by a spy,
which none of them reaches. The schedule's own event, sent as the schedule
sends it, is the control: it runs one tick and answers a summary.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import ast
import base64
import http.client
import inspect
import json
import threading
from http.server import ThreadingHTTPServer
from typing import Any, Dict, List, Set

import pytest

from test_app_support import get
from threefold.application import demo_fleet
from threefold.application.evaluator import GovernanceEvaluator
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository
from threefold.infrastructure.security_middleware import _global_rate_limiter
from threefold.interfaces import access_routes, api_handlers, app_routes, draft_routes
from threefold.interfaces.api_handlers import lambda_handler
from threefold.interfaces.server import ThreefoldHTTPRequestHandler

TICK = {"threefold_fleet": {"tick": 1}}
TICK_TEXT = json.dumps(TICK)
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
ROUTER_MODULES = (api_handlers, app_routes, access_routes, draft_routes)
# Paths a guess would try, beside every path the router names.
GUESSES = {"/", "/threefold_fleet", "/fleet", "/fleet/tick", "/api/fleet", "/api/fleet/tick", "/api/tick", "/tick"}


def _looks_like_a_path(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("/") and len(value) < 80 and " " not in value and "\n" not in value


def _router_paths() -> Set[str]:
    """Every path-shaped constant in the router modules, and every one in their route tables."""
    found: Set[str] = set()
    for module in ROUTER_MODULES:
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Constant) and _looks_like_a_path(node.value):
                found.add(node.value)
        for value in vars(module).values():
            items = value.items() if isinstance(value, dict) else value if isinstance(value, (tuple, set, frozenset, list)) else ()
            for item in items:
                for part in item if isinstance(item, tuple) else (item,):
                    if _looks_like_a_path(part):
                        found.add(part)
    found |= GUESSES
    # What a caller puts in a path as well as the fixed part of it: a session
    # id, a project name, an asset's file name.
    return found | {f"{path.rstrip('/')}/Acme-Probe-1" for path in found}


PATHS = sorted(_router_paths())


@pytest.fixture
def ticks(monkeypatch) -> List[Any]:
    """Every tick that starts, recorded instead of run, on a store of the test's own."""
    started: List[Any] = []
    monkeypatch.setattr(api_handlers, "_evaluator", GovernanceEvaluator(session_repo=DynamoDBSessionRepository()))
    monkeypatch.setattr(demo_fleet, "run_tick", lambda evaluator, **kwargs: started.append(evaluator) or {"calls": 0})
    return started


def _gateway_event(method: str, path: str, body: Any = None, query=None, headers=None, encoded=False) -> Dict[str, Any]:
    """An HTTP API (payload 2.0) event, as API Gateway and the edge hand one to the function."""
    event: Dict[str, Any] = {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": f"/prod{path}",
        "headers": dict({"content-type": "application/json"}, **(headers or {})),
        "queryStringParameters": query,
        "requestContext": {"http": {"method": method, "path": f"/prod{path}", "sourceIp": "203.0.113.7"},
                           "stage": "prod", "requestId": "acme-request-1"},
        "isBase64Encoded": encoded,
    }
    if body is not None:
        event["body"] = body
    return event


def _server_event(method: str, path: str, body: Any = None) -> Dict[str, Any]:
    """The event the local development server builds for a request."""
    return {
        "httpMethod": method,
        "path": path,
        "rawPath": path,
        "queryStringParameters": None,
        "headers": {"Content-Type": "application/json"},
        "body": body,
        "requestContext": {"http": {"method": method, "path": path}},
    }


def _variants(method: str, path: str) -> List[Dict[str, Any]]:
    return [
        _gateway_event(method, path),
        _gateway_event(method, path, TICK_TEXT),
        _gateway_event(method, path, base64.b64encode(TICK_TEXT.encode()).decode(), encoded=True),
        _gateway_event(method, path, json.dumps({"event": TICK, **TICK})),
        _gateway_event(method, path, query={"threefold_fleet": json.dumps(TICK["threefold_fleet"]), "tick": "1"}),
        _gateway_event(method, path, headers={"threefold_fleet": TICK_TEXT, "x-threefold-fleet": "1"}),
        _server_event(method, path, TICK_TEXT),
    ]


@pytest.mark.parametrize("method", METHODS)
def test_no_path_and_no_body_starts_a_tick(method: str, ticks) -> None:
    assert len(PATHS) > 100, "The walk found too few of the router's paths to mean anything"
    answered = 0
    for path in PATHS:
        for event in _variants(method, path):
            _global_rate_limiter.reset()
            response = lambda_handler(event)
            assert "threefold_fleet" not in response, (method, path)
            assert isinstance(response.get("statusCode"), int), (method, path)
            answered += 1
    assert not ticks, f"{len(ticks)} tick(s) started from an HTTP request"
    assert answered == len(PATHS) * 7


def test_an_http_event_that_also_carries_the_tick_key_is_still_a_request(ticks) -> None:
    """Even an event no gateway could build is read as the request it looks like."""
    for event in (
        dict(_gateway_event("POST", "/"), **TICK),
        dict(_server_event("POST", "/evaluate-tool-call", TICK_TEXT), **TICK),
        dict(TICK, requestContext={"http": {"method": "GET"}}),
        dict(TICK, headers={}),
        dict(TICK, body=TICK_TEXT),
    ):
        response = lambda_handler(event)
        assert "statusCode" in response and "threefold_fleet" not in response
    assert not ticks


def test_the_schedule_s_event_starts_exactly_one_tick(ticks) -> None:
    response = lambda_handler(json.loads(TICK_TEXT))
    assert len(ticks) == 1 and ticks[0] is api_handlers._evaluator
    assert set(response) == {"threefold_fleet"} and response["threefold_fleet"]["ok"] is True
    assert "statusCode" not in response, "The answer goes back to the scheduler, not to a browser"


def test_the_dispatch_is_one_line_before_anything_reads_the_event() -> None:
    """The tick is decided before the method, the path or the middleware is read."""
    body = inspect.getsource(api_handlers.lambda_handler)
    code = body[body.index('"""', body.index('"""') + 3) + 3:]
    first = [line.strip() for line in code.splitlines() if line.strip()][:2]
    assert first == [
        "if demo_fleet.is_tick_event(event):",
        "return demo_fleet.run_scheduled_tick(_evaluator, context)",
    ]
    uses = [
        node.attr for node in ast.walk(ast.parse(inspect.getsource(api_handlers)))
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "demo_fleet"
    ]
    assert sorted(uses) == ["is_tick_event", "run_scheduled_tick"], "One dispatch, nowhere else"
    for module in (app_routes, access_routes, draft_routes):
        assert "demo_fleet" not in inspect.getsource(module)


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ThreefoldHTTPRequestHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("path", ["/", "/threefold_fleet", "/api/fleet", "/evaluate-tool-call", "/api/overview"])
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_the_local_server_cannot_start_a_tick_either(server, ticks, method: str, path: str) -> None:
    connection = http.client.HTTPConnection(server[0], server[1], timeout=10)
    try:
        connection.request(method, path, body=TICK_TEXT, headers={"Content-Type": "application/json",
                                                                  "threefold_fleet": TICK_TEXT})
        response = connection.getresponse()
        answer = response.read()
    finally:
        connection.close()
    # 501 is the standard library answering a method the server does not route (PUT).
    assert response.status in (200, 400, 401, 403, 404, 405, 409, 415, 422, 501)
    assert b'"threefold_fleet"' not in answer or path == "/evaluate-tool-call" and response.status == 400
    assert not ticks


# ---------------------------------------------------------------- the real tick, end to end


@pytest.fixture
def a_store(monkeypatch):
    monkeypatch.delenv("DEFAULT_HOOK_STAGE", raising=False)
    evaluator = GovernanceEvaluator(session_repo=DynamoDBSessionRepository())
    monkeypatch.setattr(api_handlers, "_evaluator", evaluator)
    return evaluator


def test_a_scheduled_tick_feeds_the_overview_and_is_labelled_as_the_fleet(a_store) -> None:
    answer = lambda_handler({"threefold_fleet": {"tick": 1}})["threefold_fleet"]
    assert answer["ok"] is True and demo_fleet.MIN_CALLS <= answer["calls"] <= demo_fleet.MAX_CALLS
    sandbox = api_handlers.lambda_handler(
        _gateway_event("POST", "/api/sandbox", "{}")
    )
    assert sandbox["statusCode"] == 200
    payload = get("/api/overview", days=1)
    assert payload["sources"]["fleet"] == {"calls": answer["calls"], "projects": 6}
    assert payload["sources"]["sandbox"]["projects"] == 1 and payload["sources"]["sandbox"]["calls"] > 0
    assert payload["sources"]["other"] == {"calls": 0, "projects": 0}
    assert sum(part["calls"] for part in payload["sources"].values()) == payload["totals"]["calls"]
    sources = {row["project"]: row["source"] for row in payload["by_project"]}
    assert sorted(name for name, source in sources.items() if source == "fleet") == sorted(demo_fleet.rollups.FLEET_PROJECTS)
    listed = {row["project"]: row["source"] for row in get("/api/projects")["projects"]}
    assert listed == sources
    sessions = {row["session_id"] for row in get("/api/decisions", days=1, limit=200)["items"]
                if row["project_name"] in demo_fleet.rollups.FLEET_PROJECTS}
    assert sessions and all(session.startswith("fleet-") for session in sessions)
