"""`POST /rules/explain` answers "would this rule fire on this file?" and changes nothing.

An architect writing a rule needs to know whether it fires on the file in front
of them before ten teams find out. The endpoint is open because it changes
nothing, and these tests hold it to that: no verdict is issued, no ledger row is
written, and a draft sent in the body is never put in force.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import json

import pytest

from threefold.interfaces.api_handlers import _evaluator, lambda_handler

ORDER = "src/main/java/com/acme/billing/domain/Order.java"
DRAFT = [
    {
        "id": "billing-domain",
        "description": "Billing domain classes may not reach persistence",
        "when_path_matches": ["**/billing/domain/**/*.java"],
        "forbid_imports": ["javax.persistence"],
        "allow_imports": ["java.util"],
    },
    {
        "id": "billing-no-http",
        "description": "Billing domain classes may not make HTTP calls",
        "mode": "observe",
        "when_path_matches": ["**/billing/domain/**/*.java"],
        "forbid_imports": ["java.net.http"],
        "allow_imports": [],
    },
]


@pytest.fixture(autouse=True)
def _restore_the_rules_the_handler_holds():
    before = list(_evaluator.layering_rules)
    stored = _evaluator.session_repo.load_rules()
    yield
    # The stored copy is restored too: a container now reads the rules again
    # after a refresh interval, and would otherwise adopt what a test saved.
    _evaluator.session_repo.save_rules(stored if stored is not None else before)
    _evaluator.layering_rules = before


def _explain(body: dict):
    event = {
        "rawPath": "/prod/rules/explain",
        "headers": {"Content-Type": "application/json"},
        "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
        "body": json.dumps(body),
    }
    response = lambda_handler(event)
    return response["statusCode"], json.loads(response["body"])


def test_a_draft_is_tried_without_being_put_in_force() -> None:
    before = list(_evaluator.layering_rules)
    status, body = _explain({"path": ORDER, "content": "import javax.persistence.Entity;", "rules": DRAFT})
    assert status == 200
    assert body["verdict"] == "REFUSE"
    assert body["rules_considered"] == "draft"
    assert body["imports"] == ["javax.persistence.Entity"]
    assert body["violations"][0]["rule_id"] == "billing-domain"
    assert _evaluator.layering_rules == before, "Trying a draft must not change what is enforced"


def test_an_observing_rule_explains_as_observe_not_refuse() -> None:
    status, body = _explain({"path": ORDER, "content": "import java.net.http.HttpClient;", "rules": DRAFT})
    assert status == 200
    assert body["verdict"] == "OBSERVE"
    assert body["violations"][0]["mode"] == "observe"


def test_a_clean_file_explains_as_allow_and_says_why() -> None:
    status, body = _explain({"path": ORDER, "content": "import java.util.List;", "rules": DRAFT})
    assert status == 200
    assert body["verdict"] == "ALLOW"
    assert body["violations"] == []
    assert body["note"]


def test_without_a_draft_it_judges_the_rules_in_force() -> None:
    status, body = _explain({"path": ORDER, "content": "import javax.persistence.Entity;"})
    assert status == 200
    assert body["rules_considered"] == "in force"
    assert body["verdict"] == "REFUSE", "The shipped Java rule forbids javax.persistence in a domain class"


def test_explaining_writes_nothing_to_the_ledger() -> None:
    before = len(_evaluator.list_decisions(days=1))
    _explain({"path": ORDER, "content": "import javax.persistence.Entity;", "rules": DRAFT})
    assert len(_evaluator.list_decisions(days=1)) == before


def test_a_file_type_it_cannot_read_is_not_reported_as_clean() -> None:
    broad = [dict(DRAFT[0], when_path_matches=["**/domain/**"])]
    status, body = _explain(
        {"path": "src/main/kotlin/com/acme/billing/domain/Order.kt", "content": "import javax.persistence.Entity", "rules": broad}
    )
    assert status == 200
    assert body["verdict"] == "ALLOW"
    assert body["language"] is None
    assert "does not read" in body["note"]


def test_nothing_to_explain_is_a_problem_document() -> None:
    status, body = _explain({"content": "import java.util.List;"})
    assert status == 400
    assert body["type"] == "urn:threefold:error:nothing-to-explain"
