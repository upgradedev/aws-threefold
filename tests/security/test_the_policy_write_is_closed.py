"""Reading the policy is open. Changing it is not.

`POST /policy/config` writes through to DynamoDB under `CONFIG#policy`, and a
cold container adopts whatever it finds there, so an anonymous write would raise
the loop threshold this product leads with for every session that came after it
and outlive the caller. The read stays open because a judge, a scorer or an
operator has to be able to see what is actually enforced.
"""
from __future__ import annotations

import json
import os

import pytest

from threefold.interfaces.api_handlers import lambda_handler

POLICY = {
    "max_single_call_usd": 2.0,
    "max_session_budget_usd": 20.0,
    "loop_history_window": 6,
    "monomorphic_repetition_threshold": 4,
}


@pytest.fixture(autouse=True)
def _no_keys_unless_a_test_sets_them():
    previous = os.environ.pop("THREEFOLD_API_KEYS", None)
    yield
    os.environ.pop("THREEFOLD_API_KEYS", None)
    if previous is not None:
        os.environ["THREEFOLD_API_KEYS"] = previous


@pytest.fixture(autouse=True)
def _restore_the_rules_the_handler_holds():
    """A test that saves rules changes the handler's singleton for everything after it.

    The evaluator is module level so a later test would inherit whatever this
    file wrote, and the failure would land somewhere unrelated and order
    dependent.
    """
    from threefold.interfaces.api_handlers import _evaluator

    before = list(_evaluator.layering_rules)
    stored = _evaluator.session_repo.load_rules()
    yield
    # The stored copy is restored too: a container now reads the rules again
    # after a refresh interval, and would otherwise adopt what a test saved.
    _evaluator.session_repo.save_rules(stored if stored is not None else before)
    _evaluator.layering_rules = before


@pytest.fixture(autouse=True)
def _restore_the_policy_the_handler_holds():
    """The operator-key test saves a repetition threshold of four.

    Nothing put it back, which went unnoticed until a test here needed the
    third identical call to halt a session: under the leaked threshold it took
    a fourth, and that test failed only when it ran after this file's writes.
    """
    from threefold.interfaces.api_handlers import _evaluator

    before = _evaluator.policy_config
    yield
    _evaluator.update_policy(before)


def _call(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    event = {
        "rawPath": f"/prod{path}",
        "headers": headers or {"Content-Type": "application/json"},
        "requestContext": {"http": {"method": method}, "stage": "prod"},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    response = lambda_handler(event)
    return response["statusCode"], json.loads(response["body"])


def test_reading_the_policy_needs_no_key() -> None:
    status, body = _call("GET", "/policy/config")
    assert status == 200
    assert "monomorphic_repetition_threshold" in body


def test_an_anonymous_write_is_refused_when_no_key_is_configured() -> None:
    status, body = _call("POST", "/policy/config", POLICY)
    assert status == 403
    assert body["type"] == "urn:threefold:error:policy-write-disabled"
    assert "read but not changed" in body["detail"]


def test_the_short_alias_is_closed_too() -> None:
    """/policy and /policy/config are the same write."""
    assert _call("POST", "/policy", POLICY)[0] == 403


def test_a_write_without_a_key_is_refused_when_one_is_configured() -> None:
    os.environ["THREEFOLD_API_KEYS"] = "operator-key-1"
    status, body = _call("POST", "/policy/config", POLICY)
    assert status == 401
    assert body["type"] == "urn:threefold:error:missing-credentials"


def test_a_wrong_key_is_refused() -> None:
    os.environ["THREEFOLD_API_KEYS"] = "operator-key-1"
    status, _ = _call(
        "POST",
        "/policy/config",
        POLICY,
        {"Content-Type": "application/json", "X-API-Key": "not-the-key"},
    )
    assert status == 403


@pytest.mark.parametrize(
    "auth_headers",
    [
        {"X-API-Key": "operator-key-1"},
        {"Authorization": "Bearer operator-key-1"},
    ],
)
def test_the_operator_key_still_writes(auth_headers: dict) -> None:
    os.environ["THREEFOLD_API_KEYS"] = "operator-key-1"
    headers = {"Content-Type": "application/json", **auth_headers}
    status, body = _call("POST", "/policy/config", POLICY, headers)
    assert status == 200
    assert body["status"] == "POLICY_UPDATED"
    assert body["config"]["monomorphic_repetition_threshold"] == 4


def test_the_placeholder_key_never_opens_the_write() -> None:
    """A key printed in the source is not a key.

    The read path falls back to a demo placeholder when keys are switched on
    without being configured. The write path must not: that fallback is in the
    repository, so anyone could present it.
    """
    from threefold.infrastructure.security_middleware import DEFAULT_DEMO_API_KEY

    status, _ = _call(
        "POST",
        "/policy/config",
        POLICY,
        {"Content-Type": "application/json", "X-API-Key": DEFAULT_DEMO_API_KEY},
    )
    assert status == 403


def test_closing_the_write_left_the_rest_of_the_demo_open() -> None:
    """The zero-setup visitor path is the ship gate; this must not touch it."""
    status, _ = _call(
        "POST",
        "/evaluate-tool-call",
        {
            "session_id": "policy-guard-open-001",
            "tool_name": "view_file",
            "action_type": "FILE_READ",
            "arguments": {"path": "README.md"},
        },
    )
    assert status == 200
    assert _call("GET", "/status")[0] == 200
    assert _call("POST", "/simulate-loop")[0] == 200


def test_the_layering_rules_read_openly_and_write_closed() -> None:
    """The rules are the gate itself, so rewriting them is at least as closed.

    An anonymous caller who could replace them could delete the architecture
    rather than trip it, which is a quieter failure than raising a threshold.
    """
    status, body = _call("GET", "/rules")
    assert status == 200
    assert body["count"] >= 1
    assert body["languages_read"], "A reader must be able to see which languages are read"

    assert _call("POST", "/rules", {"rules": []})[0] == 403

    os.environ["THREEFOLD_API_KEYS"] = "operator-key-1"
    own = {
        "rules": [
            {
                "id": "acme-billing-domain",
                "description": "Billing domain classes may not reach persistence",
                "when_path_matches": ["**/billing/domain/**/*.java"],
                "forbid_imports": ["javax.persistence", "java.sql"],
                "allow_imports": ["java.util"],
            }
        ]
    }
    assert _call("POST", "/rules", own)[0] == 401, "A key is required even though reading needs none"

    status, saved = _call(
        "POST", "/rules", own, {"Content-Type": "application/json", "X-API-Key": "operator-key-1"}
    )
    assert status == 200
    assert saved["count"] == 1

    # And the gate now enforces what was saved, on a language it could not read before.
    status, verdict = _call(
        "POST",
        "/evaluate-tool-call",
        {
            "session_id": "rules-enforced-001",
            "project_name": "Acme-Billing",
            "tool_name": "Write",
            "action_type": "FILE_WRITE",
            "arguments": {
                "file_path": "src/main/java/com/acme/billing/domain/Order.java",
                "content": "package com.acme.billing.domain;\nimport javax.persistence.Entity;",
            },
        },
    )
    assert status == 200
    assert verdict["status"] == "BLOCKED_BOUNDARY_VIOLATION"
    assert "acme-billing-domain" in verdict["reason"], "The refusal must name the architect's own rule"


def test_a_rule_that_says_nothing_is_refused_rather_than_saved() -> None:
    """An empty rule set would silently remove the gate."""
    os.environ["THREEFOLD_API_KEYS"] = "operator-key-1"
    status, body = _call(
        "POST",
        "/rules",
        {"rules": [{"id": "empty"}]},
        {"Content-Type": "application/json", "X-API-Key": "operator-key-1"},
    )
    assert status == 400
    assert body["type"] == "urn:threefold:error:unusable-rule"


# With STAGE=prod every path not listed as public needs a key. The pages were
# listed and the reads they make were not, so each screen opened and then every
# fetch answered 401. Nothing in the suite noticed, because the event's stage is
# not what the middleware reads: the environment's is.
PUBLIC_READS = [
    ("GET", "/rules.html", None),
    ("GET", "/console.html", None),
    ("GET", "/rules", None),
    ("GET", "/api/insights", None),
    ("GET", "/api/sessions", None),
    ("GET", "/policy/config", None),
    ("GET", "/sessions/perimeter-probe", None),
    ("POST", "/rules/explain", {"path": "src/domain/models.py", "content": "import boto3"}),
    ("POST", "/rules/draft", {"description": "Domain modules may not import boto3."}),
]
STILL_CLOSED = [
    ("POST", "/rules", {"rules": []}),
    ("POST", "/rules", {"project": "Acme-Perimeter", "rules": []}),
    ("POST", "/policy/config", {}),
    ("POST", "/sessions/perimeter-probe/terminate", {"operator_name": "probe", "reason": "probe"}),
    ("POST", "/sessions/perimeter-probe/resume", {"operator_name": "probe", "reason": "probe"}),
    ("POST", "/evaluate-tool-call", {"session_id": "perimeter-probe", "tool_name": "Read"}),
]


@pytest.mark.parametrize("method, path, body", PUBLIC_READS)
def test_what_the_pages_read_stays_open_when_keys_are_enforced(monkeypatch, method, path, body) -> None:
    monkeypatch.setenv("STAGE", "prod")
    event = {
        "rawPath": f"/prod{path}",
        "headers": {"Content-Type": "application/json"},
        "requestContext": {"http": {"method": method}, "stage": "prod"},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    # The raw status: a page answers HTML, which the JSON helper cannot read.
    status = lambda_handler(event)["statusCode"]
    assert status not in (401, 403), f"{method} {path} answered {status} to an anonymous reader"


@pytest.mark.parametrize("method, path, body", STILL_CLOSED)
def test_opening_the_reads_left_every_write_closed(monkeypatch, method, path, body) -> None:
    monkeypatch.setenv("STAGE", "prod")
    status, _ = _call(method, path, body)
    assert status in (401, 403), f"{method} {path} answered {status} with no key"


def test_reading_a_session_that_does_not_exist_creates_nothing(monkeypatch) -> None:
    """An open read that stored a session for any id let anyone write rows by guessing."""
    from threefold.interfaces.api_handlers import _evaluator

    monkeypatch.setenv("STAGE", "prod")
    status, body = _call("GET", "/sessions/never-recorded-probe")
    assert status == 404
    assert body["type"] == "urn:threefold:error:session-not-found"
    assert _evaluator.session_repo.get_session("never-recorded-probe") is None


# A stack that carries real use is deployed with PublicReads=false. The pages
# stay open, because they are the product; what they read does not, because it
# is that deployment's own work. The two lists are kept apart here on purpose:
# the regression this guards against is closing one and forgetting the other.
PRIVATE_READS = [
    ("GET", "/rules", None),
    ("GET", "/api/insights", None),
    ("GET", "/api/sessions", None),
    ("GET", "/policy/config", None),
    ("GET", "/sessions/private-probe", None),
    ("POST", "/rules/explain", {"path": "src/domain/models.py", "content": "import boto3"}),
    ("POST", "/rules/draft", {"description": "Domain modules may not import boto3."}),
]
PAGES_THAT_STAY_OPEN = ["/", "/index.html", "/console.html", "/rules.html", "/sessions.html", "/settings.html"]


@pytest.mark.parametrize("method, path, body", PRIVATE_READS)
def test_a_private_stack_with_no_key_configured_refuses_the_reads(monkeypatch, method, path, body) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    status, problem = _call(method, path, body)
    assert status == 403
    assert problem["type"] == "urn:threefold:error:reads-private"


@pytest.mark.parametrize("path", PAGES_THAT_STAY_OPEN)
def test_a_private_stack_still_serves_its_pages(monkeypatch, path) -> None:
    """A console nobody can open is not a console, and the pages hold no data of their own."""
    monkeypatch.setenv("PUBLIC_READS", "false")
    response = lambda_handler(
        {
            "rawPath": f"/prod{path}" if path != "/" else "/prod",
            "headers": {},
            "requestContext": {"http": {"method": "GET"}, "stage": "prod"},
        }
    )
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith("text/html")


@pytest.mark.parametrize("method, path, body", PRIVATE_READS)
def test_a_private_read_climbs_the_same_ladder_a_write_does(monkeypatch, method, path, body) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.setenv("THREEFOLD_API_KEYS", "operator-key-1")

    assert _call(method, path, body)[0] == 401, "A missing key is 401"
    wrong = {"Content-Type": "application/json", "X-API-Key": "not-the-key"}
    assert _call(method, path, body, wrong)[0] == 403, "A wrong key is 403"
    right = {"Content-Type": "application/json", "X-API-Key": "operator-key-1"}
    assert _call(method, path, body, right)[0] not in (401, 403), "The operator's own key reads it"


def test_the_placeholder_key_does_not_open_a_private_read(monkeypatch) -> None:
    """The same rule the policy write has: a key printed in the source is not a key."""
    from threefold.infrastructure.security_middleware import DEFAULT_DEMO_API_KEY

    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.setenv("THREEFOLD_API_KEYS", "operator-key-1")
    presented = {"Content-Type": "application/json", "X-API-Key": DEFAULT_DEMO_API_KEY}
    assert _call("GET", "/api/sessions", None, presented)[0] == 403


def test_head_is_decided_exactly_as_get_is_on_a_private_read(monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_READS", "false")
    response = lambda_handler(
        {
            "rawPath": "/prod/api/sessions",
            "headers": {},
            "requestContext": {"http": {"method": "HEAD"}, "stage": "prod"},
        }
    )
    assert response["statusCode"] == 403


@pytest.mark.parametrize("method, path, body", PRIVATE_READS)
def test_the_public_demo_is_unchanged(monkeypatch, method, path, body) -> None:
    """PublicReads defaults to true, and the ship gate depends on it."""
    monkeypatch.setenv("PUBLIC_READS", "true")
    assert _call(method, path, body)[0] not in (401, 403)


def test_a_private_stack_still_takes_the_calls_its_own_hooks_send(monkeypatch) -> None:
    """Closing the reads must not close the gate: the hooks post to it all day."""
    monkeypatch.setenv("PUBLIC_READS", "false")
    status, verdict = _call(
        "POST",
        "/evaluate-tool-call",
        {
            "session_id": "private-reads-hook-001",
            "project_name": "Acme-Private",
            "agent": "claude-code",
            "origin": "hook",
            "explain": False,
            "tool_name": "view_file",
            "action_type": "FILE_READ",
            "arguments": {"path": "README.md"},
        },
    )
    assert status == 200
    assert verdict["status"] == "APPROVED"


RESUME = ("POST", "/sessions/perimeter-resume/resume", {"operator_name": "Acme On-call", "reason": "probe"})


def _halted(session_id: str) -> None:
    """A session a page halted, so a resume that got through would change something."""
    from threefold.application.dtos import ToolCallRequestDTO
    from threefold.interfaces.api_handlers import _evaluator

    repeat = ToolCallRequestDTO(
        session_id=session_id,
        developer_id="anonymous",
        project_name="Acme-Perimeter",
        tool_name="run_tests",
        action_type="COMMAND_EXEC",
        arguments={"cmd": "pytest -q"},
        origin="page",
    )
    for _ in range(3):
        _evaluator.evaluate_tool_call(repeat)
    assert _evaluator.session_repo.get_session(session_id).is_tripped


def test_a_resume_is_refused_outright_where_no_key_is_configured() -> None:
    """The demo stack deploys with no key, so there a halt stays a halt."""
    from threefold.interfaces.api_handlers import _evaluator

    _halted("perimeter-resume")
    status, body = _call(*RESUME)
    assert status == 403
    assert body["type"] == "urn:threefold:error:policy-write-disabled"
    assert "stays halted" in body["detail"]
    assert _evaluator.session_repo.get_session("perimeter-resume").is_tripped is True


def test_a_resume_climbs_the_same_ladder_a_policy_write_does(monkeypatch) -> None:
    from threefold.infrastructure.security_middleware import DEFAULT_DEMO_API_KEY
    from threefold.interfaces.api_handlers import _evaluator

    _halted("perimeter-resume")
    monkeypatch.setenv("THREEFOLD_API_KEYS", "operator-key-1")
    method, path, body = RESUME
    assert _call(method, path, body)[0] == 401, "A missing key is 401"
    assert _call(method, path, body, {"X-API-Key": "not-the-key"})[0] == 403, "A wrong key is 403"
    assert _call(method, path, body, {"X-API-Key": DEFAULT_DEMO_API_KEY})[0] == 403, (
        "A key printed in the source is not a key"
    )
    assert _evaluator.session_repo.get_session("perimeter-resume").is_tripped is True
    assert _call(method, path, body, {"X-API-Key": "operator-key-1"})[0] == 200


def test_the_kill_switch_was_not_closed_with_the_resume() -> None:
    """Freezing a session can only stop work, and the demo stack answers it anonymously on purpose."""
    status, body = _call(
        "POST", "/sessions/perimeter-freeze/terminate", {"operator_name": "probe", "reason": "probe"}
    )
    assert status == 200
    assert body["status"] == "SESSION_FROZEN"


def test_the_resume_predicate_names_only_the_resume() -> None:
    from threefold.infrastructure.security_middleware import is_protected_write

    assert is_protected_write("POST", "/sessions/acme-1/resume")
    assert is_protected_write("post", "/sessions/acme-1/resume")
    assert not is_protected_write("GET", "/sessions/acme-1/resume")
    assert not is_protected_write("POST", "/sessions/acme-1/terminate")
    assert not is_protected_write("POST", "/sessions/acme-1")
    assert is_protected_write("POST", "/rules")


def test_the_readiness_probe_is_not_opened_with_the_reads(monkeypatch) -> None:
    """No page reads it, and it reports the table name, region and raw client errors."""
    monkeypatch.setenv("STAGE", "prod")
    response = lambda_handler(
        {"rawPath": "/prod/readyz", "headers": {}, "requestContext": {"http": {"method": "GET"}, "stage": "prod"}}
    )
    assert response["statusCode"] == 401
