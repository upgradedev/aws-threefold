"""The public URL has to open the application, not a JSON document.

The ship gate is judged by someone following one link. If the root returns an
API payload, a reader has to already know which endpoints exist before the
product can show them anything.
"""
from __future__ import annotations

import json

import pytest

from threefold.interfaces.api_handlers import lambda_handler


def _get(path: str, stage: str = "prod") -> dict:
    return lambda_handler(
        {
            "rawPath": f"/{stage}{path}" if path != "/" else f"/{stage}",
            "headers": {},
            "requestContext": {"http": {"method": "GET"}, "stage": stage},
        }
    )


def test_the_root_serves_the_dashboard() -> None:
    response = _get("/")
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith("text/html")
    assert "<title>Threefold" in response["body"]


def test_the_page_is_told_the_exact_api_base() -> None:
    """The browser must not be left to guess the stage prefix from its own URL."""
    response = _get("/")
    assert '__THREEFOLD_BASE_PATH__' not in response["body"], "The token must be substituted"
    assert 'const SERVED_BASE_PATH = "/prod";' in response["body"]


def test_an_unstaged_deployment_gets_an_empty_base_path() -> None:
    response = lambda_handler(
        {
            "rawPath": "/",
            "headers": {},
            "requestContext": {"http": {"method": "GET"}, "stage": "$default"},
        }
    )
    assert 'const SERVED_BASE_PATH = "";' in response["body"]


def test_the_status_route_still_answers_json() -> None:
    response = _get("/status")
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith("application/json")
    assert json.loads(response["body"])["service"] == "Threefold"


def test_the_testbook_is_reachable() -> None:
    response = _get("/testbook.html")
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith("text/html")


@pytest.mark.parametrize(
    "path, title",
    [
        ("/settings.html", "Threefold — Policy Settings"),
        ("/sessions.html", "Threefold — Governed Sessions"),
        ("/connect.html", "Threefold — Connect a Coding Agent"),
        ("/console.html", "Threefold — Enforcement Console"),
    ],
)
def test_the_operator_pages_are_reachable(path: str, title: str) -> None:
    """A page that is written but not routed is a page nobody can open."""
    response = _get(path)
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith("text/html")
    assert f"<title>{title}</title>" in response["body"]


@pytest.mark.parametrize("path", ["/settings.html", "/sessions.html", "/connect.html", "/console.html"])
def test_the_operator_pages_are_told_the_api_base(path: str) -> None:
    """Each page calls the API itself, so each one needs the stage prefix injected."""
    response = _get(path)
    assert "__THREEFOLD_BASE_PATH__" not in response["body"], "The token must be substituted"
    assert 'const SERVED_BASE_PATH = "/prod";' in response["body"]
