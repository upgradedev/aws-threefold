"""A declared token count is a count: never negative, never astronomical.

A negative count lowered the spend the ceiling had already seen, so a later
dollar of real calls went through under it; a 400-digit integer overflowed
the price arithmetic into a 500. Both are the caller's mistake, answered 400.
"""
from __future__ import annotations

import json

import pytest

from threefold.interfaces.api_handlers import lambda_handler


def _status(path: str, tokens: int, index: int) -> int:
    event = {
        "rawPath": f"/prod{path}",
        "requestContext": {"http": {"method": "POST", "sourceIp": f"10.9.3.{index}"}, "stage": "prod"},
        "headers": {"content-type": "application/json"},
        "body": json.dumps({
            "session_id": f"bounded-tokens-{index}",
            "tool_name": "search",
            "arguments": {"q": "x"},
            "projected_input_tokens": tokens,
        }),
    }
    return lambda_handler(event, None)["statusCode"]


@pytest.mark.parametrize("path", ["/evaluate-tool-call", "/universal-eval"])
def test_a_negative_count_is_refused(path) -> None:
    assert _status(path, -3_000_000, 1 if path == "/evaluate-tool-call" else 2) == 400


@pytest.mark.parametrize("path", ["/evaluate-tool-call", "/universal-eval"])
def test_an_astronomical_count_is_refused_not_a_500(path) -> None:
    assert _status(path, int("9" * 400), 3 if path == "/evaluate-tool-call" else 4) == 400


def test_an_ordinary_count_is_judged() -> None:
    assert _status("/evaluate-tool-call", 1000, 5) == 200
