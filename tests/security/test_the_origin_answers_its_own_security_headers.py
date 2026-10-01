"""The origin answers its own security headers, not just the edge's.

Through the edge every response carries the distribution's headers policy,
but that policy overrides rather than merges: fetched straight from the API
URL, a page used to arrive with no framing, sniffing, transport or referrer
protection at all, so the dashboard could be framed by another site and page
URLs could leak to the CDNs in the Referer. The function now sends its own
set. The framing-only CSP intersects safely with the edge's full policy on
the way through, and no page frames anything.
"""
from __future__ import annotations

from threefold.interfaces.api_handlers import lambda_handler


def _get(path: str) -> dict:
    return lambda_handler({
        "rawPath": f"/prod{path}",
        "headers": {},
        "requestContext": {"http": {"method": "GET"}, "stage": "prod"},
    })


def test_a_page_cannot_be_framed_sniffed_or_leak_its_url() -> None:
    response = _get("/")
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith("text/html")
    assert response["headers"]["X-Frame-Options"] == "DENY"
    assert response["headers"]["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert response["headers"]["X-Content-Type-Options"] == "nosniff"
    assert response["headers"]["Referrer-Policy"] == "no-referrer"
    assert response["headers"]["Strict-Transport-Security"] == "max-age=63072000; includeSubDomains"


def test_json_answers_carry_sniffing_and_transport_protection() -> None:
    response = _get("/status")
    assert response["statusCode"] == 200
    assert response["headers"]["X-Content-Type-Options"] == "nosniff"
    assert response["headers"]["Strict-Transport-Security"] == "max-age=63072000; includeSubDomains"


def test_a_served_script_carries_transport_protection() -> None:
    response = lambda_handler({
        "rawPath": "/prod/install.py",
        "headers": {"Host": "acme0demo02.execute-api.eu-west-1.amazonaws.com"},
        "requestContext": {"http": {"method": "GET"}, "stage": "prod"},
    })
    assert response["statusCode"] == 200
    assert response["headers"]["X-Content-Type-Options"] == "nosniff"
    assert response["headers"]["Strict-Transport-Security"] == "max-age=63072000; includeSubDomains"
