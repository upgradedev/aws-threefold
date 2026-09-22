"""The published document describes `POST /rules/draft` as the drafter enforces it.

The caps are read from the drafter rather than repeated here, so a cap changed
in code and not in the document fails this file instead of leaving Swagger UI
promising a request the route would refuse. Every status the route answers is
documented, 422 included, because a reader who meets an undocumented status has
been told less than the route does.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from threefold.application import rule_drafter
from threefold.domain.boundary_guard import MAX_PATHLIKE_LENGTH
from threefold.interfaces.api_handlers import lambda_handler

ROOT = Path(__file__).resolve().parents[2]


def _spec() -> dict:
    response = lambda_handler(
        {
            "rawPath": "/prod/openapi.json",
            "headers": {},
            "requestContext": {"http": {"method": "GET"}, "stage": "prod"},
        }
    )
    assert response["statusCode"] == 200
    return json.loads(response["body"])


def _draft() -> dict:
    paths = _spec()["paths"]
    assert "/rules/draft" in paths, "POST /rules/draft is served but undocumented"
    assert set(paths["/rules/draft"]) == {"post"}, "The route answers POST alone"
    return paths["/rules/draft"]["post"]


def test_the_request_carries_the_caps_the_drafter_enforces() -> None:
    schema = _draft()["requestBody"]["content"]["application/json"]["schema"]
    assert schema["required"] == ["description"]
    fields = schema["properties"]
    assert fields["description"]["maxLength"] == rule_drafter.MAX_DESCRIPTION_CHARS == 600
    assert fields["project"]["maxLength"] == rule_drafter.MAX_PROJECT_CHARS
    examples = fields["examples"]
    assert examples["maxItems"] == rule_drafter.MAX_EXAMPLES == 5
    example = examples["items"]
    assert set(example["required"]) == {"path", "expect"}
    assert example["properties"]["content"]["maxLength"] == rule_drafter.MAX_EXAMPLE_CONTENT_CHARS == 4000
    assert example["properties"]["path"]["maxLength"] == MAX_PATHLIKE_LENGTH
    assert example["properties"]["expect"]["enum"] == list(rule_drafter.EXPECTATIONS)


def test_every_status_the_route_answers_is_documented() -> None:
    responses = _draft()["responses"]
    assert {"200", "400", "401", "403", "422", "502", "503"} <= set(responses)
    assert "model-unavailable" in responses["503"]["description"]
    assert "undraftable-rule" in responses["502"]["description"]
    assert "not-a-layering-rule" in responses["422"]["description"]


def test_the_answer_says_the_rule_watches_and_nothing_was_saved() -> None:
    answer = _draft()["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    assert answer["rule"] == {"$ref": "#/components/schemas/LayeringRule"}
    assert answer["saved"]["const"] is False
    tried = answer["tried"]["items"]["properties"]
    assert {"path", "expected", "verdict", "matched"} <= set(tried)
    assert tried["verdict"]["enum"] == ["REFUSE", "OBSERVE", "ALLOW"]


def test_the_document_says_who_may_draft_on_each_kind_of_stack() -> None:
    draft = _draft()
    assert {} in draft["security"], "Open where reads are public, so the credential is optional"
    assert {"OperatorApiKey": []} in draft["security"] and {"SignInSession": []} in draft["security"]
    assert "PublicReads" in draft["description"]


def test_the_yaml_twin_carries_the_draft_route_too() -> None:
    yaml = pytest.importorskip("yaml")
    twin = yaml.safe_load((ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8"))
    assert twin["paths"]["/rules/draft"] == _spec()["paths"]["/rules/draft"]
