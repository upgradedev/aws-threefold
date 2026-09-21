"""`POST /rules/explain` is open, so what it costs is decided by whoever calls it.

An adversarial review measured four ways a small anonymous request held a worker
past the fifteen-second Lambda timeout or past its memory: stacked `**` in a
draft glob (254 bytes, 57 seconds), a 56 KB path against the shipped rules
(19 seconds), a 1 MB Python literal (747 MB peak on a 256 MB function) and a
run of unclosed block comments (7 seconds for 60 KB). Each is pinned here with a
ceiling far above what the fixed code takes and far below what the old code
took, so a regression fails loudly rather than slowly.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import json
import time

import pytest

from threefold.domain.imports import declared_imports
from threefold.domain.path_match import matches
from threefold.interfaces.api_handlers import lambda_handler


def _explain(body, headers=None):
    event = {
        "rawPath": "/prod/rules/explain",
        "headers": headers or {"Content-Type": "application/json"},
        "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
        "body": body if isinstance(body, str) else json.dumps(body),
    }
    response = lambda_handler(event)
    return response["statusCode"], json.loads(response["body"])


def _timed(action):
    started = time.perf_counter()
    result = action()
    return result, time.perf_counter() - started


def test_stacked_double_stars_cannot_backtrack() -> None:
    _, took = _timed(lambda: matches("a/" * 60 + "b.py", "**/" * 7 + "zzz.py"))
    assert took < 0.5, f"Seven stacked ** took {took:.2f}s; the regex version took 57s"


def test_a_draft_of_stacked_double_stars_is_answered_quickly() -> None:
    draft = [{"id": "d", "when_path_matches": ["**/" * 7 + "zzz.py"], "forbid_imports": ["x"]}]
    (status, _), took = _timed(lambda: _explain({"path": "a/" * 60 + "b.py", "content": "", "rules": draft}))
    assert status == 200
    assert took < 1.0


def test_a_long_path_against_the_shipped_rules_is_linear() -> None:
    _, took = _timed(lambda: matches("domain/" * 8000 + "x", "**/domain/**/*.py"))
    assert took < 1.0, f"A 56 KB path took {took:.2f}s against one shipped pattern"


def test_a_path_the_gate_would_never_judge_is_refused_rather_than_judged() -> None:
    """Judging it cost time and, worse, answered REFUSE for a write the gate approves."""
    status, body = _explain({"path": "src/domain/" + "a/" * 300 + "x.py", "content": "import boto3"})
    assert status == 400
    assert "would never judge it" in body["detail"]


@pytest.mark.parametrize(
    "path, content",
    [
        ("notes/x.py", "x=[" + "a," * 520000 + "]"),
        ("src/domain/x.py", "x=[" + "a," * 520000 + "]\nimport boto3"),
        ("notes/x.py", "x=" + "a+" * 520000 + "a"),
        ("src/domain/x.ts", "/* " * 20000),
    ],
    ids=["large-literal", "large-literal-covered", "deep-expression", "unclosed-comments"],
)
def test_hostile_content_is_read_cheaply(path: str, content: str) -> None:
    (_, _), took = _timed(lambda: declared_imports(path, content))
    assert took < 1.0, f"{path}: {took:.2f}s"


def test_a_large_python_file_still_has_its_imports_read() -> None:
    """Falling back from the parser must not become a way to hide an import."""
    _, modules = declared_imports("src/domain/x.py", "x=[" + "a," * 60000 + "]\nimport boto3\n")
    assert "boto3" in modules


def test_nesting_deep_enough_to_break_the_parser_is_not_a_500() -> None:
    status, body = _explain({"path": "src/domain/x.py", "content": "x=" + "(" * 5000 + ")" * 5000 + "\nimport boto3"})
    assert status == 200
    assert body["verdict"] == "REFUSE"


def test_an_unclosed_comment_hides_what_follows_it_and_nothing_before() -> None:
    _, modules = declared_imports("src/domain/x.ts", "import axios from 'axios';\n/* never closed import y from 'y'")
    assert modules == ["axios"]


@pytest.mark.parametrize("body", ["[1, 2]", '"text"', "42"])
def test_a_body_that_is_not_an_object_is_a_400_not_a_500(body: str) -> None:
    status, problem = _explain(body)
    assert status == 400
    assert problem["type"] == "urn:threefold:error:nothing-to-explain"


def test_content_that_is_not_text_is_a_400() -> None:
    status, _ = _explain({"path": "src/domain/x.py", "content": {"not": "text"}})
    assert status == 400


def test_an_idempotency_key_does_not_replay_another_routes_answer() -> None:
    """Keyed by the key alone, explain returned whatever the key had been used for."""
    headers = {"Content-Type": "application/json", "Idempotency-Key": "shared-key-001"}
    loop = lambda_handler(
        {
            "rawPath": "/prod/simulate-loop",
            "headers": headers,
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
            "body": "{}",
        }
    )
    assert loop["statusCode"] == 200
    status, body = _explain({"path": "src/domain/x.py", "content": "import boto3"}, headers)
    assert status == 200
    assert body.get("verdict") == "REFUSE", "Explain answered with another route's cached response"
