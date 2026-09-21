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


def test_the_retired_testbook_is_neither_served_nor_public() -> None:
    """The page was removed; a route left behind would answer for a file that is gone."""
    from threefold.interfaces.api_handlers import WEB_ASSETS
    from threefold.infrastructure.security_middleware import PUBLIC_PATHS

    assert "/testbook.html" not in WEB_ASSETS
    assert "/testbook.html" not in PUBLIC_PATHS
    assert _get("/testbook.html")["statusCode"] == 404


def _head(path: str, stage: str = "prod") -> dict:
    return lambda_handler(
        {
            "rawPath": f"/{stage}{path}" if path != "/" else f"/{stage}",
            "headers": {},
            "requestContext": {"http": {"method": "HEAD"}, "stage": stage},
        }
    )


@pytest.mark.parametrize(
    "path", ["/", "/rules.html", "/status", "/openapi.json", "/api/sessions", "/api/insights", "/rules"]
)
def test_head_answers_as_get_does_with_no_body(path: str) -> None:
    """A link checker or uptime probe sends HEAD, and a 404 there reads as the page being gone."""
    got = _get(path)
    head = _head(path)
    assert head["statusCode"] == got["statusCode"] == 200
    assert head["headers"] == got["headers"]
    assert head["body"] == ""


def test_head_on_an_unknown_route_is_still_a_404() -> None:
    assert _head("/no-such-page.html")["statusCode"] == 404


def test_head_on_an_open_read_is_not_refused_when_keys_are_enforced(monkeypatch) -> None:
    """Decided as its own verb, HEAD fell through to the key check that GET is exempt from."""
    monkeypatch.setenv("STAGE", "prod")
    assert _head("/api/sessions")["statusCode"] == 200
    assert _head("/rules.html")["statusCode"] == 200


@pytest.mark.parametrize(
    "path, title",
    [
        ("/settings.html", "Threefold — Policy Settings"),
        ("/sessions.html", "Threefold — Governed Sessions"),
        ("/connect.html", "Threefold — Connect a Coding Agent"),
        ("/console.html", "Threefold — Enforcement Console"),
        ("/rules.html", "Threefold — Architecture Rules"),
    ],
)
def test_the_operator_pages_are_reachable(path: str, title: str) -> None:
    """A page that is written but not routed is a page nobody can open."""
    response = _get(path)
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith("text/html")
    assert f"<title>{title}</title>" in response["body"]


@pytest.mark.parametrize("path", ["/settings.html", "/sessions.html", "/connect.html", "/console.html", "/rules.html"])
def test_the_operator_pages_are_told_the_api_base(path: str) -> None:
    """Each page calls the API itself, so each one needs the stage prefix injected."""
    response = _get(path)
    assert "__THREEFOLD_BASE_PATH__" not in response["body"], "The token must be substituted"
    assert 'const SERVED_BASE_PATH = "/prod";' in response["body"]


@pytest.mark.parametrize(
    "path", ["/", "/settings.html", "/sessions.html", "/connect.html", "/console.html", "/rules.html"]
)
def test_every_page_escapes_values_that_are_not_strings(path: str) -> None:
    """escapeHtml returned String(value) unescaped for a non-string, so a tool_name sent
    as a list reached the console as markup."""
    body = _get(path)["body"]
    assert "if (typeof str !== 'string') return str == null ? '' : String(str);" not in body
    assert "if (typeof str !== 'string') str = str == null ? '' : String(str);" in body
