"""The overview says what its numbers are made of, so a page can state them plainly.

A public stack's review backlog is mostly the would-refuse calls seeded into
visitors' sandboxes, and its list of agents mixes coding agents with the demo's
own buttons and rows older than the agent field. The totals are kept as they
were; beside them the overview splits them between sandboxes and every other
project, marks each agent with the kind of caller it is, and lists the coding
agents alone. Read from rollup items built here, with no store.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import datetime

import pytest

from threefold.application import projects as stages
from threefold.application import rollups

TODAY = datetime.date(2026, 9, 25)
SANDBOX = "Acme-Sandbox-0a1b2c3d"
OTHER_SANDBOX = "Acme-Sandbox-9f8e7d6c"
CREATED = "2026-09-25T08:00:00+00:00"


def _rollup(project, **counters):
    return dict(counters, day=str(TODAY), project=project)


def _live(*names):
    """Configurations for sandboxes still inside their day, as POST /api/sandbox writes them."""
    return {name: stages.new_config(CREATED, sandbox=True) for name in names}


ITEMS = [
    _rollup(SANDBOX, calls=12, approved=6, observed=6, **{
        "agent:claude-code": 4, "agent:codex": 4, "agent:antigravity": 4,
        "reviewed:observed": 1, "review:false_alarm": 1}),
    _rollup(OTHER_SANDBOX, calls=12, approved=6, observed=6, **{
        "agent:claude-code": 4, "agent:codex": 4, "agent:antigravity": 4}),
    _rollup("Acme-Probe", calls=9, approved=5, observed=2, refused=2, **{
        "agent:claude-code": 7, "agent:unknown": 2, "reviewed:observed": 1, "review:correct": 1}),
    _rollup("Acme-Sim", calls=6, approved=4, refused=2, **{"agent:page": 6}),
]


def _overview(items=ITEMS, configs=None, **kwargs):
    return rollups.overview(items, _live(SANDBOX, OTHER_SANDBOX) if configs is None else configs, days=7,
                            today=TODAY, **kwargs)


# ---------------------------------------------------------------- the sandbox split


def test_needs_review_and_would_refuse_are_split_between_sandboxes_and_everything_else() -> None:
    payload = _overview()
    totals, split = payload["totals"], payload["sandbox_split"]
    assert (totals["would_refuse"], totals["needs_review"]) == (14, 12), "The totals keep their meaning"
    assert split["sandbox"] == {"projects": 2, "calls": 24, "approved": 12, "refused": 0, "would_refuse": 12,
                                "needs_review": 11, "false_alarms": 1}
    assert split["elsewhere"] == {"projects": 2, "calls": 15, "approved": 9, "refused": 4, "would_refuse": 2,
                                  "needs_review": 1, "false_alarms": 0}


def test_the_two_sides_add_up_to_the_totals() -> None:
    payload = _overview()
    for name in ("calls", "approved", "refused", "would_refuse", "needs_review", "false_alarms", "projects"):
        assert payload["sandbox_split"]["sandbox"][name] + payload["sandbox_split"]["elsewhere"][name] == payload["totals"][name], name


def test_each_project_row_says_whether_it_is_a_sandbox() -> None:
    by_project = {row["project"]: row for row in _overview()["by_project"]}
    assert by_project[SANDBOX]["sandbox"] is True and by_project[OTHER_SANDBOX]["sandbox"] is True
    assert by_project["Acme-Probe"]["sandbox"] is False and by_project["Acme-Sim"]["sandbox"] is False
    listed = {row["project"]: row["sandbox"] for row in rollups.projects_listing(ITEMS, _live(SANDBOX, OTHER_SANDBOX))}
    assert listed == {SANDBOX: True, OTHER_SANDBOX: True, "Acme-Probe": False, "Acme-Sim": False}


@pytest.mark.parametrize("name", ["Acme-Sandbox-0A1B2C3D", "Acme-Sandbox-0a1b2c3", "Acme-Sandboxes-0a1b2c3d", "Acme-Sandbox"])
def test_only_the_sandbox_pattern_is_a_sandbox(name) -> None:
    assert rollups.is_sandbox(name) is False
    assert rollups.is_sandbox(SANDBOX) is True


def test_an_expired_sandbox_stays_out_of_both_sides_as_it_stays_out_of_the_totals() -> None:
    payload = _overview(configs=_live(SANDBOX))
    assert payload["sandbox_split"]["sandbox"]["projects"] == 1 and payload["sandbox_split"]["sandbox"]["calls"] == 12
    assert payload["totals"]["calls"] == 27


def test_a_stack_with_no_sandbox_puts_everything_elsewhere() -> None:
    payload = _overview(items=ITEMS[2:], configs={})
    assert payload["sandbox_split"]["sandbox"] == {"projects": 0, "calls": 0, "approved": 0, "refused": 0,
                                                   "would_refuse": 0, "needs_review": 0, "false_alarms": 0}
    assert payload["sandbox_split"]["elsewhere"]["needs_review"] == payload["totals"]["needs_review"] == 1


def test_the_overview_of_one_sandbox_is_all_sandbox() -> None:
    payload = _overview(project=SANDBOX)
    assert payload["sandbox_split"]["sandbox"]["needs_review"] == 5 and payload["sandbox_split"]["elsewhere"]["calls"] == 0


# ---------------------------------------------------------------- agents


def test_each_agent_is_marked_with_the_kind_of_caller_it_is() -> None:
    kinds = {entry["agent"]: entry["kind"] for entry in _overview()["by_agent"]}
    assert kinds == {"claude-code": "coding_agent", "codex": "coding_agent", "antigravity": "coding_agent",
                     "page": "page", "unknown": "unknown"}


@pytest.mark.parametrize("agent, kind", [
    ("claude-code", "coding_agent"), ("codex", "coding_agent"), ("antigravity", "coding_agent"),
    ("page", "page"), ("ci", "ci"), ("pre-commit", "ci"), ("unknown", "unknown"), ("acme-bot", "unknown"), ("", "unknown"),
])
def test_the_kind_of_every_agent_value(agent, kind) -> None:
    assert rollups.agent_kind(agent) == kind


def test_the_coding_agents_are_listed_and_counted_beside_every_agent() -> None:
    payload = _overview()
    assert payload["totals"]["agents"] == 5, "totals.agents keeps its meaning: every agent value with a call"
    assert payload["totals"]["coding_agents"] == 3
    assert [entry["agent"] for entry in payload["coding_agents"]] == ["claude-code", "antigravity", "codex"]
    assert payload["coding_agents"] == [entry for entry in payload["by_agent"] if entry["kind"] == "coding_agent"]
    assert [entry["agent"] for entry in payload["by_agent"]] == ["claude-code", "antigravity", "codex", "page", "unknown"], (
        "by_agent keeps its order: most calls first, a tie by name")


def test_each_agent_says_how_many_of_its_calls_were_in_sandboxes() -> None:
    by_agent = {entry["agent"]: entry for entry in _overview()["by_agent"]}
    # Codex and Antigravity called only from the synthetic sandbox seeds; a
    # page can say so rather than list them as agents being governed.
    assert (by_agent["codex"]["calls"], by_agent["codex"]["calls_in_sandboxes"]) == (8, 8)
    assert (by_agent["claude-code"]["calls"], by_agent["claude-code"]["calls_in_sandboxes"]) == (15, 8)
    assert by_agent["page"]["calls_in_sandboxes"] == 0


def test_a_public_overview_of_page_and_unknown_calls_has_no_coding_agent() -> None:
    payload = _overview(items=[_rollup("Acme-Sim", calls=6, approved=6, **{"agent:page": 4, "agent:unknown": 2})], configs={})
    assert payload["totals"]["agents"] == 2 and payload["totals"]["coding_agents"] == 0
    assert payload["coding_agents"] == []
