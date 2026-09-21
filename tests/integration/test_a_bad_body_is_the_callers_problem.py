"""A malformed request is a 400 that says what to send; a bug is a 500 that says nothing.

Both used to be the same thing. A body that was a JSON array rather than an
object reached `body.get` and came back as 500 with the exception text in it,
which tells a stranger about the inside of the function and tells the caller
nothing they can act on.
"""
from __future__ import annotations

import base64
import json
import logging

import pytest

from threefold.interfaces import api_handlers
from threefold.interfaces.api_handlers import lambda_handler

OPERATOR_KEY = "operator-key-bodies"

# Every POST that reads fields out of the body. /rules is deliberately absent:
# a rule set may be sent as the array itself, which the test below pins.
OBJECT_ROUTES = [
    "/evaluate-tool-call",
    "/adapter/universal-tool-call",
    "/universal-eval",
    "/issue-certificate",
    "/sessions/acme-session/terminate",
    "/policy/config",
]
NOT_OBJECTS = ["[]", '["a", "b"]', '"just a string"', "null", "42"]


@pytest.fixture(autouse=True)
def _an_operator_key_is_configured(monkeypatch):
    """So the policy write reaches its body instead of stopping at the door."""
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)


@pytest.fixture(autouse=True)
def _restore_what_a_write_here_changes():
    """A saved rule set or policy outlives this file otherwise.

    The handler's evaluator is module level, so anything these tests write is
    inherited by every test after them, and the failure lands somewhere
    unrelated and depends on the order the suite happened to run in.
    """
    evaluator = api_handlers._evaluator
    rules = list(evaluator.layering_rules)
    stored = evaluator.session_repo.load_rules()
    policy = evaluator.policy_config
    yield
    evaluator.session_repo.save_rules(stored if stored is not None else rules)
    evaluator.layering_rules = rules
    evaluator.update_policy(policy)


def _post(path: str, raw_body: str, request_id: str | None = "acme-request-42") -> tuple[int, dict, dict]:
    context = {"http": {"method": "POST"}, "stage": "prod"}
    if request_id is not None:
        context["requestId"] = request_id
    response = lambda_handler(
        {
            "rawPath": f"/prod{path}",
            "headers": {"Content-Type": "application/json", "X-API-Key": OPERATOR_KEY},
            "body": raw_body,
            "requestContext": context,
        }
    )
    return response["statusCode"], json.loads(response["body"]), response["headers"]


@pytest.mark.parametrize("path", OBJECT_ROUTES)
@pytest.mark.parametrize("raw_body", NOT_OBJECTS)
def test_a_body_that_is_not_an_object_is_refused_as_a_problem_document(path: str, raw_body: str) -> None:
    status, problem, headers = _post(path, raw_body)
    assert status == 400, f"{path} answered {status} for {raw_body}"
    assert headers["Content-Type"] == "application/problem+json"
    assert problem["type"] == "urn:threefold:error:bad-request"
    assert "JSON object" in problem["detail"]


def test_a_rule_set_may_still_be_sent_as_the_array_itself() -> None:
    """The one POST that takes a bare array, and the reason the check is per route."""
    rule = {
        "id": "acme-domain-stays-pure",
        "description": "Acme domain classes may not reach infrastructure",
        "when_path_matches": ["**/acme/domain/**/*.py"],
        "forbid_imports": ["boto3"],
        "allow_imports": [],
    }
    status, body, _ = _post("/rules", json.dumps([rule]))
    assert status == 200, body
    assert body["count"] == 1


def test_trying_a_rule_with_the_wrong_shape_keeps_its_own_answer() -> None:
    """This route tells the caller what a draft looks like, which a generic 400 would not."""
    status, problem, _ = _post("/rules/explain", "[]")
    assert status == 400
    assert problem["type"] == "urn:threefold:error:nothing-to-explain"


def test_invalid_json_is_a_400_that_names_the_body() -> None:
    status, problem, _ = _post("/evaluate-tool-call", "{not json")
    assert status == 400
    assert problem["invalid_params"][0]["name"] == "body"


def test_evaluations_that_are_not_verdict_objects_are_refused() -> None:
    status, problem, _ = _post("/issue-certificate", json.dumps({"session_id": "s", "evaluations": ["x"]}))
    assert status == 400
    assert problem["invalid_params"][0]["name"] == "evaluations"


@pytest.mark.parametrize(
    "invariants",
    ['"all good"', "[true]", '{"SECRET_LEAKAGE_FREE": "false"}', '{"SECRET_LEAKAGE_FREE": 1}'],
)
def test_invariants_that_are_not_true_or_false_are_refused(invariants: str) -> None:
    """The issuer reads a verdict's invariants now, so their shape is checked at the door.

    The session is a governed one, so nothing else stands between these and the
    issuer: without the check a string reached `.values()` as a 500, and the
    string "false" certified the session as compliant.
    """
    session_id = "bodies-invariants"
    status, _, _ = _post(
        "/evaluate-tool-call",
        json.dumps({"session_id": session_id, "project_name": "Acme-Bodies", "tool_name": "view_file"}),
    )
    assert status == 200
    raw = '{"session_id": "%s", "evaluations": [{"rule_evaluations": %s}]}' % (session_id, invariants)
    status, problem, _ = _post("/issue-certificate", raw)
    assert status == 400, problem
    assert problem["type"] == "urn:threefold:error:bad-request"
    assert "rule_evaluations" in problem["detail"]


def test_a_verdict_that_sends_no_invariants_is_still_accepted() -> None:
    """The dashboard and older callers send a status and a hash and nothing else."""
    session_id = "bodies-no-invariants"
    _post(
        "/evaluate-tool-call",
        json.dumps({"session_id": session_id, "project_name": "Acme-Bodies", "tool_name": "view_file"}),
    )
    for evaluation in ("{}", '{"rule_evaluations": null}'):
        raw = '{"session_id": "%s", "evaluations": [%s]}' % (session_id, evaluation)
        status, cert, _ = _post("/issue-certificate", raw)
        assert status == 200, cert
        assert cert["all_passed"] is True


def test_a_base64_body_is_parsed_after_it_is_decoded() -> None:
    """It was measured decoded and parsed encoded, so the fields were all defaults."""
    body = json.dumps({"session_id": "bodies-base64", "project_name": "Acme-Base64", "tool_name": "view_file"})
    response = lambda_handler(
        {
            "rawPath": "/prod/evaluate-tool-call",
            "headers": {"Content-Type": "application/json"},
            "body": base64.b64encode(body.encode("utf-8")).decode("ascii"),
            "isBase64Encoded": True,
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
        }
    )
    assert response["statusCode"] == 200
    verdict = json.loads(response["body"])
    assert verdict["session_id"] == "bodies-base64"


def test_a_doubled_slash_still_reaches_the_route() -> None:
    """The stack's ApiEndpoint output ends in "/", so callers append to it and get //status."""
    response = lambda_handler(
        {
            "rawPath": "/prod//status",
            "headers": {},
            "requestContext": {"http": {"method": "GET"}, "stage": "prod"},
        }
    )
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["service"] == "Threefold"


def _boom(*_args, **_kwargs):
    raise RuntimeError("acme-governance-table-47 is on fire")


def test_an_unexpected_failure_says_nothing_but_its_request_id(monkeypatch, caplog) -> None:
    monkeypatch.setattr(api_handlers._evaluator, "evaluate_tool_call", _boom)
    with caplog.at_level(logging.ERROR, logger="threefold.api"):
        status, problem, headers = _post("/evaluate-tool-call", json.dumps({"session_id": "bodies-500"}))

    assert status == 500
    assert headers["Content-Type"] == "application/problem+json"
    assert problem["request_id"] == "acme-request-42"
    assert "acme-governance-table-47" not in json.dumps(problem), "The exception text reached the caller"
    assert "acme-governance-table-47" in caplog.text, "And it has to be in the log, under the id"
    assert "acme-request-42" in caplog.text


def test_a_failure_outside_any_route_is_answered_the_same_way(monkeypatch, caplog) -> None:
    """Headers that are not a mapping never reach a route, so the backstop answers."""
    with caplog.at_level(logging.ERROR, logger="threefold.api"):
        response = lambda_handler(
            {
                "rawPath": "/prod/status",
                "headers": ["not", "a", "mapping"],
                "requestContext": {"http": {"method": "GET"}, "stage": "prod", "requestId": "acme-request-43"},
            }
        )
    assert response["statusCode"] == 500
    assert json.loads(response["body"])["request_id"] == "acme-request-43"


def test_a_request_with_no_id_of_its_own_is_given_one(monkeypatch) -> None:
    """A 500 a caller cannot quote is a 500 nobody can look up."""
    monkeypatch.setattr(api_handlers._evaluator, "evaluate_tool_call", _boom)
    status, problem, _ = _post("/evaluate-tool-call", json.dumps({"session_id": "bodies-500"}), request_id=None)
    assert status == 500
    assert len(problem["request_id"]) == 32
