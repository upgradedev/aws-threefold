"""GET /api/overview says what its totals are made of, driven as deployed.

A visitor's sandbox is seeded through the real evaluator with would-refuse
calls from all three coding agents, beside a project of the service's own and
the demo's page calls. The overview's totals keep their meaning; beside them it
splits them between sandboxes and everything else, marks each agent with its
kind, and lists the coding agents alone. Every existing field is checked to
still be there.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import pytest

from test_app_support import DOMAIN_WRITE, README, fresh_project, get, hook_call, post
from threefold.application import rollups
from threefold.application.evaluator import GovernanceEvaluator
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository
from threefold.interfaces import api_handlers

CONTRACT = {"window_days", "generated_at", "source", "totals", "series", "by_agent", "by_origin", "by_rule",
            "by_project", "stages", "self_correction"}
TOTALS = {"calls", "approved", "refused", "would_refuse", "needs_review", "false_alarms", "projects", "agents"}
PARTS = TOTALS - {"agents"}


@pytest.fixture(autouse=True)
def _a_store_of_its_own(monkeypatch):
    """Each test reads rollups holding only its own calls."""
    monkeypatch.setattr(api_handlers, "_evaluator", GovernanceEvaluator(session_repo=DynamoDBSessionRepository()))


@pytest.fixture
def stack(monkeypatch):
    """A sandbox, a project of the service's own in observe, and the demo's page calls."""
    monkeypatch.delenv("DEFAULT_HOOK_STAGE", raising=False)
    sandbox = post("/api/sandbox", {})["project"]
    probe = fresh_project("Acme-Probe")
    hook_call(probe, "probe-0a1b2c3d-layering", DOMAIN_WRITE)
    hook_call(probe, "probe-0a1b2c3d-read", README, tool="Read", action="FILE_READ")
    sim = fresh_project("Acme-Sim")
    hook_call(sim, "sim-acme-1", README, tool="Read", action="FILE_READ", origin="page", agent="page")
    return sandbox, probe, sim


def test_the_totals_are_split_between_sandboxes_and_every_other_project(stack) -> None:
    sandbox, probe, sim = stack
    payload = get("/api/overview", days=1)
    assert CONTRACT <= set(payload) and TOTALS <= set(payload["totals"]), "Every existing field is still there"
    split = payload["sandbox_split"]
    assert set(split) == {"sandbox", "elsewhere"} and set(split["sandbox"]) == set(split["elsewhere"]) == PARTS
    for name in PARTS:
        assert split["sandbox"][name] + split["elsewhere"][name] == payload["totals"][name], name
    # The sandbox's seeds include would-refuse calls on purpose; the probe's one violation is the rest.
    assert split["sandbox"]["projects"] == 1 and split["sandbox"]["would_refuse"] >= 2
    assert split["sandbox"]["needs_review"] == split["sandbox"]["would_refuse"]
    assert (split["elsewhere"]["projects"], split["elsewhere"]["would_refuse"], split["elsewhere"]["needs_review"]) == (2, 1, 1)
    sandbox_flags = {row["project"]: row["sandbox"] for row in payload["by_project"]}
    assert sandbox_flags == {sandbox: True, probe: False, sim: False}


def test_agents_carry_their_kind_and_the_coding_agents_are_listed_apart(stack) -> None:
    payload = get("/api/overview", days=1)
    by_agent = {entry["agent"]: entry for entry in payload["by_agent"]}
    assert {agent: entry["kind"] for agent, entry in by_agent.items()} == {
        "claude-code": "coding_agent", "codex": "coding_agent", "antigravity": "coding_agent", "page": "page"}
    # Codex and Antigravity called only from the sandbox's synthetic seeds.
    assert by_agent["codex"]["calls"] == by_agent["codex"]["calls_in_sandboxes"] > 0
    assert by_agent["claude-code"]["calls"] - by_agent["claude-code"]["calls_in_sandboxes"] == 2
    assert by_agent["page"]["calls_in_sandboxes"] == 0
    assert payload["totals"]["agents"] == 4 and payload["totals"]["coding_agents"] == 3
    assert {entry["agent"] for entry in payload["coding_agents"]} == {"claude-code", "codex", "antigravity"}


def test_the_projects_listing_says_which_projects_are_sandboxes(stack) -> None:
    sandbox, probe, _ = stack
    listed = {row["project"]: row for row in get("/api/projects")["projects"]}
    assert listed[sandbox]["sandbox"] is True and listed[probe]["sandbox"] is False


def test_every_new_field_is_in_the_document_the_deployment_serves() -> None:
    spec = get("/openapi.json")
    schemas = spec["components"]["schemas"]
    overview = spec["paths"]["/api/overview"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    assert "coding_agents" in overview["totals"]["properties"]
    assert set(overview["sandbox_split"]["properties"]) == {"sandbox", "elsewhere"}
    assert set(schemas["OverviewPart"]["properties"]) == PARTS
    agent_ref = {"$ref": "#/components/schemas/AgentCalls"}
    assert overview["by_agent"]["items"] == agent_ref and overview["coding_agents"]["items"] == agent_ref
    assert set(schemas["AgentCalls"]["properties"]) == {"agent", "calls", "kind", "calls_in_sandboxes"}
    assert set(schemas["AgentCalls"]["properties"]["kind"]["enum"]) == set(rollups.AGENT_KINDS.values()) | {rollups.UNKNOWN_KIND}
    assert "sandbox" in overview["by_project"]["items"]["properties"]
    listing = spec["paths"]["/api/projects"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert "sandbox" in listing["properties"]["projects"]["items"]["properties"]
