"""Every application route the router answers is in the document the deployment serves.

A route that works but is missing from /openapi.json is one Swagger UI cannot
show and the dashboard's deep links cannot reach, so the two are checked
against each other here rather than trusted to be edited together.
"""
from __future__ import annotations

from test_app_support import get
from threefold.interfaces import app_routes

PROJECT_ROUTES = {
    ("get", "/api/projects/{name}"),
    ("post", "/api/projects/{name}"),
    ("post", "/api/projects/{name}/promote"),
    ("post", "/api/projects/{name}/demote"),
    ("post", "/api/projects/{name}/reviews"),
}


def _documented() -> set:
    spec = get("/openapi.json")
    return {(method, path) for path, operations in spec["paths"].items() for method in operations}


def test_every_application_route_is_documented() -> None:
    answered = {(method.lower(), path) for method, path in app_routes.FIXED_ROUTES} | PROJECT_ROUTES
    missing = answered - _documented()
    assert not missing, f"Answered but not documented: {sorted(missing)}"


def test_the_evaluation_documents_the_hook_mode_and_the_project_stage() -> None:
    evaluate = get("/openapi.json")["paths"]["/evaluate-tool-call"]["post"]
    request = evaluate["requestBody"]["content"]["application/json"]["schema"]["properties"]
    response = evaluate["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    assert request["hook_mode"]["enum"] == ["observe", "managed", "enforce"]
    assert response["project_stage"]["enum"] == ["observe", "enforce"]
