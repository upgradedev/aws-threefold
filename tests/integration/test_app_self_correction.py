"""Self-correction on the overview and on a project's page, and the proof snapshot's route.

Driven through `lambda_handler` with API Gateway v2 events, as deployed: real
hook calls go through the real evaluator, are refused or approved by the real
gates, and the figure is read back from the ledger they were recorded in. The
existing fields of both responses are checked to be unchanged beside it.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import datetime
import json
import time

import pytest

from test_app_support import DOMAIN_WRITE, README, fresh_project, get, hook_call, request
from threefold.application import ledger
from threefold.application.evaluator import GovernanceEvaluator
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository
from threefold.interfaces import api_handlers, app_routes

CLEAN_WRITE = {"file_path": DOMAIN_WRITE["file_path"], "content": "from dataclasses import dataclass\n"}
FIELDS = {"refusals_considered", "self_corrected", "rate", "median_calls_to_correct", "rows_read", "complete"}


@pytest.fixture(autouse=True)
def _a_ledger_of_its_own(monkeypatch):
    """Each test reads a ledger holding only its own calls, as the ledger tests do."""
    monkeypatch.setattr(api_handlers, "_evaluator", GovernanceEvaluator(session_repo=DynamoDBSessionRepository()))


def _next_instant() -> None:
    """Returns once the clock the ledger stamps calls with has moved on.

    A real agent's calls are seconds apart. This machine's clock may tick only
    once a millisecond, so calls made back to back can share a timestamp, and
    the ledger records no other order: the figure then reads them as possibly
    simultaneous, which is right for the ledger and not what these tests are
    about. Waiting for the next tick gives each call its own instant, as a real
    session's calls have. The rule for tied calls has its own unit tests.
    """
    start = datetime.datetime.now(datetime.timezone.utc)
    while datetime.datetime.now(datetime.timezone.utc) <= start:
        time.sleep(0.0005)


@pytest.fixture
def corrected() -> str:
    """A project in enforce whose agent was refused once and then wrote the same file cleanly."""
    name = fresh_project("Acme-Correct")
    session = f"{name}-cc"
    assert hook_call(name, session, DOMAIN_WRITE)["status"].startswith("BLOCKED")
    _next_instant()
    hook_call(name, session, README, tool="Read", action="FILE_READ")
    _next_instant()
    assert hook_call(name, session, CLEAN_WRITE)["status"] == "APPROVED"
    # A second session that was refused and walked away.
    assert hook_call(name, f"{name}-cx", DOMAIN_WRITE, agent="codex")["status"].startswith("BLOCKED")
    return name


def test_the_overview_reports_self_correction_beside_its_unchanged_totals(corrected) -> None:
    payload = get("/api/overview", project=corrected, days=7)
    figure = payload["self_correction"]
    assert set(figure) == FIELDS
    assert (figure["refusals_considered"], figure["self_corrected"], figure["rate"]) == (2, 1, 0.5)
    assert figure["median_calls_to_correct"] == 2 and figure["rows_read"] == 4 and figure["complete"] is True
    # The fields the contract fixed are all still there, with the same meaning.
    assert payload["source"] == "rollups"
    assert (payload["totals"]["calls"], payload["totals"]["refused"], payload["totals"]["approved"]) == (4, 2, 2)
    assert {"window_days", "generated_at", "series", "by_agent", "by_origin", "by_rule", "by_project", "stages"} <= set(payload)


def test_the_project_page_reports_it_in_the_readiness_summary(corrected) -> None:
    body = get(f"/api/projects/{corrected}", days=14)
    summary = body["readiness"]["summary"]
    assert (summary["self_correction"]["refusals_considered"], summary["self_correction"]["self_corrected"]) == (2, 1)
    assert {"stage", "days_observed", "calls_observed", "would_have_refused", "reviewed", "false_alarms",
            "false_alarm_rate", "rules_ready", "rules_quiet", "rules_noisy", "rules_needing_review"} <= set(summary)
    assert isinstance(body["readiness"]["rules"], list) and body["project"] == corrected


def test_the_overview_without_a_project_counts_every_projects_sessions(corrected) -> None:
    # Another project's session: a tie with it cannot reorder any of these.
    other = fresh_project("Acme-Other")
    hook_call(other, f"{other}-cc", DOMAIN_WRITE)
    assert get("/api/overview", days=1)["self_correction"]["refusals_considered"] == 3


def test_a_project_in_observe_refuses_nothing_so_nothing_is_considered(monkeypatch) -> None:
    monkeypatch.delenv("DEFAULT_HOOK_STAGE", raising=False)
    name = fresh_project("Acme-Watch")
    hook_call(name, f"{name}-cc", DOMAIN_WRITE)
    _next_instant()
    hook_call(name, f"{name}-cc", CLEAN_WRITE)
    figure = get(f"/api/projects/{name}")["readiness"]["summary"]["self_correction"]
    assert figure["refusals_considered"] == 0 and figure["rate"] is None and figure["complete"] is True
    assert get("/api/overview", project=name)["totals"]["would_refuse"] == 1


def test_a_page_refusal_is_not_an_agent_to_correct() -> None:
    name = fresh_project("Acme-Page")
    hook_call(name, f"sim-{name}", DOMAIN_WRITE, origin="page", agent="page")
    _next_instant()
    hook_call(name, f"sim-{name}", CLEAN_WRITE, origin="page", agent="page")
    assert get("/api/overview", project=name)["self_correction"]["refusals_considered"] == 0


def test_a_read_cut_short_says_so(corrected, monkeypatch) -> None:
    monkeypatch.setattr(ledger, "SELF_CORRECTION_ROWS", 2)
    figure = get("/api/overview", project=corrected)["self_correction"]
    assert figure["complete"] is False and figure["rows_read"] == 2


def test_a_store_that_cannot_page_the_ledger_gives_the_unread_figure(monkeypatch) -> None:
    class _NoLedger:
        def __getattr__(self, name):
            if name == "read_decision_day":
                raise AttributeError(name)
            return getattr(DynamoDBSessionRepository(), name)

    monkeypatch.setattr(api_handlers, "_evaluator", GovernanceEvaluator(session_repo=_NoLedger()))
    assert get("/api/overview")["self_correction"] == ledger.self_correction_unread()


def test_a_ledger_whose_queries_fail_is_not_read_as_a_complete_empty_window(monkeypatch) -> None:
    # The store answers a failed query with an empty day so its listings keep
    # working; read that way, a throttled table would show "no refusal in this
    # window", complete. The figure asks it to raise, and so says it is unread.
    class _Throttled:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise RuntimeError("ProvisionedThroughputExceededException")
            return fail

    repo = DynamoDBSessionRepository()
    repo._table = _Throttled()
    monkeypatch.setattr(api_handlers, "_evaluator", GovernanceEvaluator(session_repo=repo))
    assert get("/api/overview")["self_correction"] == ledger.self_correction_unread()
    summary = get("/api/projects/Acme-Throttled")["readiness"]["summary"]
    assert summary["self_correction"]["complete"] is False
    # The listing beside it still reads the failure as it always has.
    assert get("/api/decisions")["items"] == []


def test_commands_are_left_out_because_the_ledger_keeps_only_their_program() -> None:
    name = fresh_project("Acme-Shell")
    session = f"{name}-cc"
    refused = hook_call(name, session, {"command": "git commit --no-verify -m wip"}, tool="Bash", action="COMMAND_EXEC")
    assert refused["status"].startswith("BLOCKED")
    _next_instant()
    assert hook_call(name, session, {"command": "git status"}, tool="Bash", action="COMMAND_EXEC")["status"] == "APPROVED"
    _next_instant()
    heredoc = {"command": "cat > src/acme/domain/order.py <<'PY'\nimport boto3\nPY"}
    assert hook_call(name, session, heredoc, tool="Bash", action="COMMAND_EXEC")["status"].startswith("BLOCKED")
    _next_instant()
    assert hook_call(name, session, CLEAN_WRITE)["status"] == "APPROVED"
    figure = get("/api/overview", project=name)["self_correction"]
    # Neither `git status` correcting a refused commit, nor a refused shell
    # write scored as never corrected: both refusals are left out.
    assert (figure["refusals_considered"], figure["self_corrected"], figure["rate"]) == (0, 0, None)
    assert get("/api/overview", project=name)["totals"]["refused"] == 2


def test_every_row_the_figure_reads_is_reduced_first(monkeypatch) -> None:
    seen = []
    real = ledger.shown_row

    def spy(row):
        shown = real(row)
        seen.append(shown)
        return shown

    monkeypatch.setattr(ledger, "shown_row", spy)
    name = fresh_project("Acme-Reduced")
    hook_call(name, f"{name}-cc", DOMAIN_WRITE, developer="c0ffee000002")
    get("/api/overview", project=name)
    assert seen and all(len(row["developer_id"]) == 8 for row in seen)
    assert "c0ffee000002" not in json.dumps(seen)


# ---------------------------------------------------------------- the proof snapshot


def test_the_proof_snapshot_is_served_as_it_was_written(tmp_path, monkeypatch) -> None:
    snapshot = {"schema": 1, "generated_at": "2026-09-22T12:00:00Z", "benchmark": {"pilot": True}}
    target = tmp_path / "proof.json"
    target.write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(app_routes, "PROOF_PATH", str(target))
    assert get("/proof.json") == snapshot


def test_no_snapshot_is_a_404_the_page_reads_as_not_measured(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_routes, "PROOF_PATH", str(tmp_path / "absent.json"))
    status, body = request("GET", "/proof.json")
    assert status == 404 and body["type"] == "urn:threefold:error:proof-not-found"
    assert body["title"] == "Not Measured Yet"


@pytest.mark.parametrize("content", ["[1, 2]", "{not json"])
def test_a_snapshot_that_is_not_an_object_is_the_deployments_fault(tmp_path, monkeypatch, content) -> None:
    target = tmp_path / "proof.json"
    target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(app_routes, "PROOF_PATH", str(target))
    status, body = request("GET", "/proof.json")
    assert status == 500 and body["type"] == "urn:threefold:error:proof-unreadable"


@pytest.mark.parametrize("env, closed", [
    ({"ENFORCE_API_KEY": "true", "THREEFOLD_API_KEYS": "acme-key-1"}, "/readyz"),
    ({"STAGE": "prod", "THREEFOLD_API_KEYS": "acme-key-1"}, "/readyz"),
    ({"PUBLIC_READS": "false"}, "/api/overview"),
])
def test_the_snapshot_is_open_to_anyone_on_every_stack(monkeypatch, env, closed) -> None:
    # A decision, not an accident of which variables a stack sets: the page
    # that shows it is public, and it carries only anonymised totals. The
    # route beside it shows each stack really is closing what it closes.
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert request("GET", closed)[0] in (401, 403)
    assert request("GET", "/proof.json")[0] == 200
    assert request("HEAD", "/proof.json")[0] == 200


def test_the_committed_snapshot_is_the_one_the_route_serves() -> None:
    assert app_routes.PROOF_PATH.replace("\\", "/").endswith("src/threefold/web/proof.json")


# ---------------------------------------------------------------- the published document


def test_both_fields_and_the_snapshot_are_in_the_document_the_deployment_serves() -> None:
    spec = get("/openapi.json")
    schema = spec["components"]["schemas"]["SelfCorrection"]
    assert set(schema["required"]) == FIELDS and set(schema["properties"]) == FIELDS
    ref = {"$ref": "#/components/schemas/SelfCorrection"}
    overview = spec["paths"]["/api/overview"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert overview["properties"]["self_correction"] == ref
    project = spec["paths"]["/api/projects/{name}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert project["properties"]["readiness"]["properties"]["summary"]["properties"]["self_correction"] == ref
    proof = spec["paths"]["/proof.json"]["get"]
    assert {"200", "404", "500"} <= set(proof["responses"])
