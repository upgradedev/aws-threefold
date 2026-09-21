"""A rule set is saved whole or not at all, and a refusal names each rule it could not use.

The first version kept what it could use and dropped the rest with 200
RULES_UPDATED. A mode typo, a list sent as a string or a forty-first pattern
each changed what was enforced with no sign but a count one lower than sent. A
string was worse than dropped: `"**/domain/**"` without brackets was iterated
into one-character globs, one of them `*`, and saved. A number was a 500 on an
open route.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import json
import os

import pytest

from threefold.domain.layering_rules import MAX_PATTERNS_PER_RULE, validate_rules
from threefold.interfaces.api_handlers import _evaluator, lambda_handler

GOOD = {
    "id": "billing-domain",
    "description": "Billing domain classes may not reach persistence",
    "when_path_matches": ["**/billing/domain/**/*.java"],
    "forbid_imports": ["javax.persistence"],
    "allow_imports": ["java.util"],
}


@pytest.fixture(autouse=True)
def _operator_key_and_clean_rules():
    previous = os.environ.get("THREEFOLD_API_KEYS")
    os.environ["THREEFOLD_API_KEYS"] = "operator-key-1"
    before = list(_evaluator.layering_rules)
    stored = _evaluator.session_repo.load_rules()
    yield
    _evaluator.session_repo.save_rules(stored if stored is not None else before)
    _evaluator.layering_rules = before
    if previous is None:
        os.environ.pop("THREEFOLD_API_KEYS", None)
    else:
        os.environ["THREEFOLD_API_KEYS"] = previous


def _save(rules):
    response = lambda_handler(
        {
            "rawPath": "/prod/rules",
            "headers": {"Content-Type": "application/json", "X-API-Key": "operator-key-1"},
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
            "body": json.dumps({"rules": rules}),
        }
    )
    return response["statusCode"], json.loads(response["body"])


@pytest.mark.parametrize(
    "broken, reason_fragment",
    [
        (dict(GOOD, when_path_matches="**/domain/**"), "must be a list"),
        (dict(GOOD, forbid_imports=7), "must be a list"),
        (dict(GOOD, forbid_imports=["java.sql", 3]), "only non-empty strings"),
        (dict(GOOD, mode="warn"), "mode must be"),
        (dict(GOOD, mode=""), "mode must be"),
        (dict(GOOD, mode=False), "mode must be"),
        (dict(GOOD, forbid_imports=[f"pkg{i}" for i in range(MAX_PATTERNS_PER_RULE + 1)]), "the limit is"),
        (dict(GOOD, when_path_matches=["a" * 301]), "longer than"),
        (dict(GOOD, forbid_imports=[]), "is empty"),
    ],
    ids=["paths-as-string", "forbid-as-number", "non-string-item", "unknown-mode", "empty-mode",
         "false-mode", "too-many-patterns", "pattern-too-long", "nothing-forbidden"],
)
def test_a_rule_that_cannot_be_used_refuses_the_whole_save(broken, reason_fragment) -> None:
    before = list(_evaluator.layering_rules)
    status, body = _save([GOOD | {"id": "kept-if-half-applied"}, broken])
    assert status == 400
    assert body["type"] == "urn:threefold:error:unusable-rule"
    assert any(reason_fragment in problem["reason"] for problem in body["problems"]), body["problems"]
    assert body["problems"][0]["index"] == 1, "The problem names the rule by position"
    assert _evaluator.layering_rules == before, "Half of a refused save must not be applied"


def test_a_missing_or_null_mode_is_the_default_and_nothing_else_is() -> None:
    usable, problems = validate_rules([dict(GOOD, id="a"), dict(GOOD, id="b", mode=None)])
    assert not problems
    assert [rule["mode"] for rule in usable] == ["enforce", "enforce"]


def test_mode_is_trimmed_and_read_without_regard_to_case() -> None:
    usable, problems = validate_rules([dict(GOOD, mode="  Observe ")])
    assert not problems and usable[0]["mode"] == "observe"


def test_two_rules_may_not_share_an_id() -> None:
    """A refusal quotes one rule by id, so two with the same id make it ambiguous."""
    _, problems = validate_rules([GOOD, dict(GOOD)])
    assert problems and "earlier rule" in problems[0]["reason"]


def test_a_body_that_is_a_bare_list_is_accepted() -> None:
    response = lambda_handler(
        {
            "rawPath": "/prod/rules",
            "headers": {"Content-Type": "application/json", "X-API-Key": "operator-key-1"},
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
            "body": json.dumps([GOOD]),
        }
    )
    assert response["statusCode"] == 200, response["body"]


def test_a_good_save_says_how_soon_every_server_applies_it() -> None:
    status, body = _save([GOOD])
    assert status == 200
    assert body["count"] == 1
    assert body["refresh_seconds"] > 0


def test_a_draft_with_an_unusable_rule_is_not_tried() -> None:
    """Trying what survived explained as "no rule covers this path"."""
    response = lambda_handler(
        {
            "rawPath": "/prod/rules/explain",
            "headers": {"Content-Type": "application/json"},
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
            "body": json.dumps(
                {
                    "path": "src/main/java/com/acme/billing/domain/Order.java",
                    "content": "import javax.persistence.Entity;",
                    "rules": [dict(GOOD, mode="observ")],
                }
            ),
        }
    )
    body = json.loads(response["body"])
    assert response["statusCode"] == 400
    assert body["type"] == "urn:threefold:error:unusable-rule"
    assert "mode must be" in body["problems"][0]["reason"]
