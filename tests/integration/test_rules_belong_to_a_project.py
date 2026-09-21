"""A project can carry its own layering rules without changing anyone else's.

One shared rule set made every team's architecture the same architecture, so
the only way to hold one team to a rule was to hold all of them to it. A
project's own set is stored beside the shared one, judges that project's calls
alone, and is read again on the same half-minute interval, one project at a
time. Everything else keeps the shared set, exactly as before.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import json

import pytest

from threefold.application import evaluator as evaluator_module
from threefold.application.dtos import InvalidRequestError, ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator, UnusableRulesError
from threefold.domain.layering_rules import DEFAULT_RULES
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository

BILLING_CORE = {
    "id": "acme-billing-core-no-http",
    "description": "Billing core may not make HTTP calls",
    "when_path_matches": ["**/billing/core/**/*.py"],
    "forbid_imports": ["requests"],
}
LEDGER = "src/billing/core/ledger.py"
HTTP_IMPORT = "import requests\n"
OPERATOR = {"Content-Type": "application/json", "X-API-Key": "operator-key-1"}


def _write(evaluator: GovernanceEvaluator, session_id: str, project: str, path: str = LEDGER,
           content: str = HTTP_IMPORT):
    return evaluator.evaluate_tool_call(
        ToolCallRequestDTO(
            session_id=session_id,
            developer_id="anonymous",
            project_name=project,
            tool_name="Write",
            action_type="FILE_WRITE",
            arguments={"file_path": path, "content": content},
        )
    )


class _Table:
    """Enough of a DynamoDB table to see which key a write landed on."""

    def __init__(self) -> None:
        self.items: dict = {}

    def put_item(self, Item, **_):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key, **_):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}


class _Resource:
    def __init__(self) -> None:
        self.table = _Table()

    def Table(self, _name):
        return self.table


# ---------------------------------------------------------------- storage


def test_a_project_set_is_stored_beside_the_shared_set_not_over_it() -> None:
    repo = DynamoDBSessionRepository()
    repo.save_project_rules("Acme-Billing", [BILLING_CORE])

    assert repo.load_project_rules("Acme-Billing") == [BILLING_CORE]
    assert repo.load_rules() is None, "Saving one project's rules must not create a shared set"
    assert repo.load_project_rules("Acme-Shipping") is None


def test_the_live_table_keeps_a_project_set_under_its_own_key() -> None:
    resource = _Resource()
    repo = DynamoDBSessionRepository(boto3_resource=resource)
    repo.save_rules(list(DEFAULT_RULES))
    repo.save_project_rules("Acme-Billing", [BILLING_CORE])

    keys = set(resource.table.items)
    assert ("CONFIG#rules#Acme-Billing", "METADATA") in keys
    assert ("CONFIG#rules", "METADATA") in keys, "The shared set keeps the key it always had"
    assert repo.load_project_rules("Acme-Billing") == [BILLING_CORE]
    assert repo.load_rules() == list(DEFAULT_RULES)


def test_a_failed_live_read_of_a_project_set_raises_rather_than_reporting_none() -> None:
    """None means "this project has no rules", which sends its calls to the shared set."""

    class _Down:
        def get_item(self, **_):
            raise ConnectionError("simulated DynamoDB outage")

    repo = DynamoDBSessionRepository(boto3_resource=type("R", (), {"Table": lambda self, name: _Down()})())
    with pytest.raises(ConnectionError):
        repo.load_project_rules("Acme-Billing")


# ---------------------------------------------------------------- the gate


def test_a_call_is_judged_by_its_own_projects_rules() -> None:
    evaluator = GovernanceEvaluator(session_repo=DynamoDBSessionRepository())
    evaluator.update_rules([BILLING_CORE], project="Acme-Billing")

    verdict = _write(evaluator, "project-rules-own", "Acme-Billing")
    assert verdict.status == "BLOCKED_BOUNDARY_VIOLATION"
    assert "acme-billing-core-no-http" in verdict.reason


def test_another_project_still_gets_the_shared_set() -> None:
    evaluator = GovernanceEvaluator(session_repo=DynamoDBSessionRepository())
    evaluator.update_rules([BILLING_CORE], project="Acme-Billing")

    assert _write(evaluator, "project-rules-other", "Acme-Shipping").status == "APPROVED"
    assert evaluator.layering_rules == DEFAULT_RULES, "The shared set must be untouched by a project save"
    domain = _write(evaluator, "project-rules-other-domain", "Acme-Shipping",
                    path="src/domain/models.py", content="import boto3\n")
    assert domain.status == "BLOCKED_BOUNDARY_VIOLATION", "The shipped rule still holds for everyone else"


def test_a_projects_set_replaces_the_shared_set_rather_than_adding_to_it() -> None:
    """The contract: the project's rules when they exist, the shared set otherwise.

    Pinned because the other reading, merging the two, would mean a project
    could never loosen a shared rule its architecture does not share, and the
    two readings are indistinguishable until someone relies on one of them.
    """
    evaluator = GovernanceEvaluator(session_repo=DynamoDBSessionRepository())
    evaluator.update_rules([BILLING_CORE], project="Acme-Billing")

    verdict = _write(evaluator, "project-rules-replace", "Acme-Billing",
                     path="src/domain/models.py", content="import boto3\n")
    assert verdict.status == "APPROVED"


def test_a_name_outside_the_pattern_cannot_have_rules_saved() -> None:
    """Such a call is stored as "unlabelled", so rules under its own name would never apply."""
    repo = DynamoDBSessionRepository()
    evaluator = GovernanceEvaluator(session_repo=repo)
    with pytest.raises(InvalidRequestError) as refused:
        evaluator.update_rules([BILLING_CORE], project="not-an-acme-name")
    assert refused.value.name == "project"
    assert repo.load_project_rules("not-an-acme-name") is None
    assert evaluator.layering_rules == DEFAULT_RULES


def test_a_project_save_is_all_or_nothing() -> None:
    repo = DynamoDBSessionRepository()
    evaluator = GovernanceEvaluator(session_repo=repo)
    with pytest.raises(UnusableRulesError):
        evaluator.update_rules([BILLING_CORE, {"id": "says-nothing"}], project="Acme-Billing")
    assert repo.load_project_rules("Acme-Billing") is None
    assert _write(evaluator, "project-rules-half", "Acme-Billing").status == "APPROVED"


def test_a_project_save_reaches_a_container_that_did_not_take_it() -> None:
    shared = DynamoDBSessionRepository()
    took_the_save = GovernanceEvaluator(session_repo=shared)
    elsewhere = GovernanceEvaluator(session_repo=shared)

    assert _write(elsewhere, "project-rules-elsewhere-1", "Acme-Billing").status == "APPROVED"
    took_the_save.update_rules([BILLING_CORE], project="Acme-Billing")
    assert _write(elsewhere, "project-rules-elsewhere-2", "Acme-Billing").status == "APPROVED", (
        "Inside the interval the other container still holds what it read"
    )

    elsewhere._project_rules["Acme-Billing"].read_at -= evaluator_module.RULES_REFRESH_SECONDS + 1
    assert _write(elsewhere, "project-rules-elsewhere-3", "Acme-Billing").status == "BLOCKED_BOUNDARY_VIOLATION"


def test_a_project_is_read_at_most_once_per_interval() -> None:
    """Every hook call names its project, so reading on each one would double the table's reads."""

    class _Counting(DynamoDBSessionRepository):
        reads = 0

        def load_project_rules(self, project):
            self.reads += 1
            return super().load_project_rules(project)

    repo = _Counting()
    evaluator = GovernanceEvaluator(session_repo=repo)
    for index in range(4):
        _write(evaluator, f"project-rules-count-{index}", "Acme-Billing")
    assert repo.reads == 1

    evaluator._project_rules["Acme-Billing"].read_at -= evaluator_module.RULES_REFRESH_SECONDS + 1
    _write(evaluator, "project-rules-count-late", "Acme-Billing")
    assert repo.reads == 2


def test_a_failed_re_read_keeps_the_project_rules_a_container_holds() -> None:
    """Dropping to the shared set on a transient error would silently loosen that project."""

    class _Flaky(DynamoDBSessionRepository):
        failing = False

        def load_project_rules(self, project):
            if self.failing:
                raise ConnectionError("simulated DynamoDB outage")
            return super().load_project_rules(project)

    repo = _Flaky()
    evaluator = GovernanceEvaluator(session_repo=repo)
    evaluator.update_rules([BILLING_CORE], project="Acme-Billing")
    repo.failing = True
    evaluator._project_rules["Acme-Billing"].read_at -= evaluator_module.RULES_REFRESH_SECONDS + 1

    assert _write(evaluator, "project-rules-flaky", "Acme-Billing").status == "BLOCKED_BOUNDARY_VIOLATION"


def test_a_name_outside_the_pattern_is_never_looked_up() -> None:
    """No rules can exist for it, so a read would be a table call bought for nothing."""

    class _Counting(DynamoDBSessionRepository):
        reads = 0

        def load_project_rules(self, project):
            self.reads += 1
            return super().load_project_rules(project)

    repo = _Counting()
    evaluator = GovernanceEvaluator(session_repo=repo)
    _write(evaluator, "project-rules-unlabelled", "unlabelled")
    _write(evaluator, "project-rules-unlabelled-2", "DurabilityCheck")
    assert repo.reads == 0
    assert evaluator.rules_in_force("unlabelled") == (evaluator.layering_rules, "shipped")


def test_the_projects_a_container_holds_are_bounded(monkeypatch) -> None:
    """A caller can send any name that fits the pattern, and each would be held forever."""
    monkeypatch.setattr(evaluator_module, "MAX_PROJECTS_HELD", 3)
    evaluator = GovernanceEvaluator(session_repo=DynamoDBSessionRepository())
    for name in ("Acme-One", "Acme-Two", "Acme-Three", "Acme-Four"):
        evaluator.rules_in_force(name)
    assert list(evaluator._project_rules) == ["Acme-Two", "Acme-Three", "Acme-Four"]

    evaluator.rules_in_force("Acme-Two")
    evaluator.rules_in_force("Acme-Five")
    assert list(evaluator._project_rules) == ["Acme-Four", "Acme-Two", "Acme-Five"], (
        "The least recently used project goes first, not the one read first"
    )


def test_the_source_says_which_set_a_project_gets() -> None:
    evaluator = GovernanceEvaluator(session_repo=DynamoDBSessionRepository())
    assert evaluator.rules_in_force("Acme-Billing")[1] == "shipped"

    evaluator.update_rules([dict(BILLING_CORE, id="acme-shared-no-http")])
    assert evaluator.rules_in_force("Acme-Billing")[1] == "shared"

    evaluator.update_rules([BILLING_CORE], project="Acme-Billing")
    assert evaluator.rules_in_force("Acme-Billing") == (
        evaluator._project_rules["Acme-Billing"].rules,
        "project",
    )
    assert evaluator.rules_in_force()[1] == "shared"


# ---------------------------------------------------------------- the routes


@pytest.fixture
def handler(monkeypatch):
    """The deployed handler, with an operator key and its shared rules restored after."""
    from threefold.interfaces import api_handlers

    monkeypatch.setenv("THREEFOLD_API_KEYS", "operator-key-1")
    evaluator = api_handlers._evaluator
    before = list(evaluator.layering_rules)
    stored = evaluator.session_repo.load_rules()
    yield api_handlers
    evaluator.session_repo.save_rules(stored if stored is not None else before)
    evaluator.layering_rules = before


def _call(api_handlers, method: str, path: str, body=None, headers=None, query=None):
    event = {
        "rawPath": f"/prod{path}",
        "headers": headers or {"Content-Type": "application/json"},
        "requestContext": {"http": {"method": method}, "stage": "prod"},
    }
    if query is not None:
        event["queryStringParameters"] = query
    if body is not None:
        event["body"] = json.dumps(body)
    response = api_handlers.lambda_handler(event)
    return response["statusCode"], json.loads(response["body"])


def test_reading_a_project_with_no_rules_of_its_own_returns_the_shared_set_and_says_so(handler) -> None:
    status, body = _call(handler, "GET", "/rules", query={"project": "Acme-Routes-None"})
    assert status == 200
    assert body["project"] == "Acme-Routes-None"
    assert body["is_default"] is True
    assert body["source"] == "shipped"
    assert body["rules"] == handler._evaluator.layering_rules
    assert body["warnings"] == []


def test_a_project_with_no_rules_of_its_own_reads_as_default_even_when_a_shared_set_is_saved(handler) -> None:
    """With a project, is_default means "nothing saved for this project", not "the shipped set".

    The one case where the two readings part: an operator has saved a shared
    set and the project asked about has none of its own. `source` says the
    shared set came back, so is_default does not have to.
    """
    status, _ = _call(handler, "POST", "/rules", {"rules": [BILLING_CORE]}, OPERATOR)
    assert status == 200

    status, body = _call(handler, "GET", "/rules", query={"project": "Acme-Routes-Shared"})
    assert status == 200
    assert body["project"] == "Acme-Routes-Shared"
    assert body["is_default"] is True
    assert body["source"] == "shared"
    assert [rule["id"] for rule in body["rules"]] == ["acme-billing-core-no-http"]
    assert body["rules"] == handler._evaluator.layering_rules


def test_a_project_save_is_read_back_for_that_project_only(handler) -> None:
    status, saved = _call(
        handler, "POST", "/rules", {"project": "Acme-Routes-Own", "rules": [BILLING_CORE]}, OPERATOR
    )
    assert status == 200
    assert saved["scope"] == "project"
    assert saved["project"] == "Acme-Routes-Own"

    status, own = _call(handler, "GET", "/rules", query={"project": "Acme-Routes-Own"})
    assert status == 200
    assert own["is_default"] is False
    assert own["source"] == "project"
    assert [rule["id"] for rule in own["rules"]] == ["acme-billing-core-no-http"]

    status, shared = _call(handler, "GET", "/rules")
    assert [rule["id"] for rule in shared["rules"]] == [rule["id"] for rule in DEFAULT_RULES]
    assert shared["is_default"] is True
    assert shared["source"] == "shipped"
    assert shared["project"] is None


def test_the_saved_project_set_governs_that_projects_calls_through_the_gate(handler) -> None:
    _call(handler, "POST", "/rules", {"project": "Acme-Routes-Gate", "rules": [BILLING_CORE]}, OPERATOR)
    write = {
        "tool_name": "Write",
        "action_type": "FILE_WRITE",
        "arguments": {"file_path": LEDGER, "content": HTTP_IMPORT},
    }
    status, own = _call(
        handler, "POST", "/evaluate-tool-call",
        dict(write, session_id="routes-gate-own", project_name="Acme-Routes-Gate"),
    )
    assert status == 200
    assert own["status"] == "BLOCKED_BOUNDARY_VIOLATION"

    status, other = _call(
        handler, "POST", "/evaluate-tool-call",
        dict(write, session_id="routes-gate-other", project_name="Acme-Routes-Elsewhere"),
    )
    assert other["status"] == "APPROVED"


def test_a_save_without_a_project_still_replaces_the_shared_set(handler) -> None:
    status, saved = _call(handler, "POST", "/rules", {"rules": [BILLING_CORE]}, OPERATOR)
    assert status == 200
    assert saved["scope"] == "shared"
    assert saved["project"] is None
    status, shared = _call(handler, "GET", "/rules")
    assert shared["source"] == "shared"
    assert shared["is_default"] is False


@pytest.mark.parametrize("project", ["not-an-acme-name", "", 7, ["Acme-Listed"], None])
def test_a_save_for_a_project_outside_the_pattern_is_refused(handler, project) -> None:
    """Null among them: a project picker left unset sends it, and it replaced every team's rules."""
    stored_before = handler._evaluator.session_repo.load_rules()
    status, problem = _call(handler, "POST", "/rules", {"project": project, "rules": [BILLING_CORE]}, OPERATOR)
    assert status == 400
    assert problem["invalid_params"][0]["name"] == "project"
    assert handler._evaluator.layering_rules == DEFAULT_RULES, "A refused project save must not land on the shared set"
    assert handler._evaluator.session_repo.load_rules() == stored_before, "nor in the table"


def test_a_project_save_still_needs_the_operator_key(handler) -> None:
    status, _ = _call(handler, "POST", "/rules", {"project": "Acme-Routes-Keyless", "rules": [BILLING_CORE]})
    assert status == 401
    assert handler._evaluator.session_repo.load_project_rules("Acme-Routes-Keyless") is None


def test_reading_a_project_outside_the_pattern_warns_that_it_gets_the_shared_set(handler) -> None:
    status, body = _call(handler, "GET", "/rules", query={"project": "not-an-acme-name"})
    assert status == 200
    assert body["is_default"] is True
    assert body["source"] == "shipped"
    assert "AllowedProjectPattern" in body["warnings"][0]


def test_explain_judges_against_the_projects_rules_when_no_draft_is_sent(handler) -> None:
    _call(handler, "POST", "/rules", {"project": "Acme-Routes-Explain", "rules": [BILLING_CORE]}, OPERATOR)
    status, own = _call(
        handler, "POST", "/rules/explain",
        {"path": LEDGER, "content": HTTP_IMPORT, "project": "Acme-Routes-Explain"},
    )
    assert status == 200
    assert own["verdict"] == "REFUSE"
    assert own["rules_source"] == "project"
    assert own["project"] == "Acme-Routes-Explain"

    status, shared = _call(handler, "POST", "/rules/explain", {"path": LEDGER, "content": HTTP_IMPORT})
    assert shared["verdict"] == "ALLOW"
    assert shared["rules_source"] == "shipped"
    assert shared["project"] is None


def test_a_draft_is_still_what_explain_tries_when_one_is_sent_with_a_project(handler) -> None:
    _call(handler, "POST", "/rules", {"project": "Acme-Routes-Draft", "rules": [BILLING_CORE]}, OPERATOR)
    draft = [dict(BILLING_CORE, id="draft-allows-http", forbid_imports=["httpx"])]
    status, body = _call(
        handler, "POST", "/rules/explain",
        {"path": LEDGER, "content": HTTP_IMPORT, "project": "Acme-Routes-Draft", "rules": draft},
    )
    assert status == 200
    assert body["verdict"] == "ALLOW"
    assert body["rules_source"] == "draft"


@pytest.mark.parametrize("project", [7, None, False, 0, ["Acme-Listed"]])
def test_explain_refuses_a_project_that_is_not_text(handler, project) -> None:
    """Null, false and 0 were read as "no project" and judged by the shared set without a word."""
    status, problem = _call(handler, "POST", "/rules/explain", {"path": LEDGER, "content": "", "project": project})
    assert status == 400
    assert problem["type"] == "urn:threefold:error:nothing-to-explain"


@pytest.mark.parametrize("project", ["not-an-acme-name", ""])
def test_explain_warns_about_a_project_outside_the_pattern_as_the_read_does(handler, project) -> None:
    """The same name got a warning from GET /rules and silence from explain."""
    status, body = _call(
        handler, "POST", "/rules/explain", {"path": LEDGER, "content": HTTP_IMPORT, "project": project}
    )
    assert status == 200
    assert body["project"] == project
    assert body["rules_source"] == "shipped"
    assert len(body["warnings"]) == 1
    assert "AllowedProjectPattern" in body["warnings"][0]

    status, read = _call(handler, "GET", "/rules", query={"project": "not-an-acme-name"})
    assert body["warnings"] == read["warnings"]


def test_explain_has_nothing_to_warn_about_for_a_project_in_the_pattern_or_none(handler) -> None:
    status, named = _call(
        handler, "POST", "/rules/explain",
        {"path": LEDGER, "content": HTTP_IMPORT, "project": "Acme-Routes-Explain-Quiet"},
    )
    assert status == 200
    assert named["warnings"] == []
    status, unnamed = _call(handler, "POST", "/rules/explain", {"path": LEDGER, "content": HTTP_IMPORT})
    assert status == 200
    assert unnamed["warnings"] == []


def test_the_console_coverage_still_describes_the_shared_rules(handler) -> None:
    """A project's set governs that project; the coverage line speaks for the deployment."""
    _call(handler, "POST", "/rules", {"project": "Acme-Routes-Coverage", "rules": [BILLING_CORE]}, OPERATOR)
    status, insights = _call(handler, "GET", "/api/insights")
    assert status == 200
    layering = next(row for row in insights["coverage"] if row["rule"] == "ARCHITECTURAL_BOUNDARY_SAFE")
    assert "acme-billing-core-no-http" not in layering["watches"]
    assert "python-domain-stays-pure" in layering["watches"]
