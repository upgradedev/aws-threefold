"""The overview and the projects listing say which calls are the synthetic fleet's.

The public demo is fed by a synthetic Acme fleet, by visitors' sandboxes and by
the service's own probes and page calls. `GET /api/overview` gains `sources`,
the calls and projects of each, and every `by_project` row and every
`GET /api/projects` row gains `source`, so a page can say in words which of its
numbers are synthetic. Read from rollup items built here, with no store.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from threefold.application import projects as stages
from threefold.application import rollups

TODAY = datetime.date(2026, 9, 26)
CREATED = "2026-09-26T08:00:00+00:00"
SANDBOX = "Acme-Sandbox-0a1b2c3d"
SPEC = json.loads(
    (Path(__file__).resolve().parents[2] / "src" / "threefold" / "web" / "openapi.json").read_text(encoding="utf-8")
)


def _rollup(project, day=TODAY, **counters):
    return dict(counters, day=str(day), project=project)


ITEMS = [
    _rollup("Acme-Payments", calls=30, approved=27, observed=2, refused=1, **{"agent:codex": 30}),
    _rollup("Acme-Payments", day=TODAY - datetime.timedelta(days=1), calls=20, approved=20, **{"agent:codex": 20}),
    _rollup("Acme-Ledger", calls=25, approved=25, **{"agent:claude-code": 25}),
    _rollup(SANDBOX, calls=12, approved=6, observed=6, **{"agent:antigravity": 12}),
    _rollup("Acme-Probe", calls=9, approved=9, **{"agent:claude-code": 9}),
    # Close to a fleet name and not one: a test's fresh project, and another team's.
    _rollup("Acme-Ledger-0a1b2c3d4e", calls=4, approved=4, **{"agent:claude-code": 4}),
    _rollup("Acme-Payments-Internal", calls=3, approved=3, **{"agent:claude-code": 3}),
]
CONFIGS = {
    "Acme-Payments": stages.new_config(CREATED, stage="enforce"),
    "Acme-Ledger": stages.new_config(CREATED),
    SANDBOX: stages.new_config(CREATED, sandbox=True),
}


def _overview(items=ITEMS, **kwargs):
    return rollups.overview(items, CONFIGS, days=7, today=TODAY, **kwargs)


@pytest.mark.parametrize(
    "project, source",
    [
        ("Acme-Payments", "fleet"),
        ("Acme-Checkout", "fleet"),
        ("Acme-Ledger", "fleet"),
        ("Acme-Search", "fleet"),
        ("Acme-Mobile", "fleet"),
        ("Acme-Platform", "fleet"),
        (SANDBOX, "sandbox"),
        ("Acme-Probe", "other"),
        ("Acme-Sim", "other"),
        ("unlabelled", "other"),
        ("Acme-Ledger-0a1b2c3d4e", "other"),
        ("Acme-Payments-Internal", "other"),
        ("acme-payments", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_a_project_is_the_fleets_only_under_one_of_its_six_names_exactly(project, source) -> None:
    assert rollups.source_of(project) == source


def test_the_fleet_is_six_projects_and_the_three_sources_are_closed() -> None:
    assert len(rollups.FLEET_PROJECTS) == len(set(rollups.FLEET_PROJECTS)) == 6
    assert rollups.SOURCES == ("fleet", "sandbox", "other")
    assert not any(rollups.is_sandbox(name) for name in rollups.FLEET_PROJECTS)


def test_sources_count_the_calls_and_projects_of_each_and_add_up_to_the_totals() -> None:
    payload = _overview()
    assert payload["sources"] == {
        "fleet": {"calls": 75, "projects": 2},
        "sandbox": {"calls": 12, "projects": 1},
        "other": {"calls": 16, "projects": 3},
    }
    assert sum(part["calls"] for part in payload["sources"].values()) == payload["totals"]["calls"] == 103
    assert sum(part["projects"] for part in payload["sources"].values()) == payload["totals"]["projects"] == 6


def test_every_by_project_row_names_its_source() -> None:
    rows = {row["project"]: row["source"] for row in _overview()["by_project"]}
    assert rows == {
        "Acme-Payments": "fleet",
        "Acme-Ledger": "fleet",
        SANDBOX: "sandbox",
        "Acme-Probe": "other",
        "Acme-Ledger-0a1b2c3d4e": "other",
        "Acme-Payments-Internal": "other",
    }


def test_one_project_s_overview_counts_only_that_project_s_source() -> None:
    payload = _overview(project="Acme-Payments")
    assert payload["sources"]["fleet"] == {"calls": 50, "projects": 1}
    assert payload["sources"]["sandbox"] == payload["sources"]["other"] == {"calls": 0, "projects": 0}


def test_an_empty_window_still_names_every_source() -> None:
    payload = rollups.overview([], {}, days=7, today=TODAY)
    assert payload["sources"] == {name: {"calls": 0, "projects": 0} for name in rollups.SOURCES}


def test_an_expired_sandbox_counts_nowhere() -> None:
    configs = {name: config for name, config in CONFIGS.items() if name != SANDBOX}
    payload = rollups.overview(ITEMS, configs, days=7, today=TODAY)
    assert payload["sources"]["sandbox"] == {"calls": 0, "projects": 0}
    assert sum(part["calls"] for part in payload["sources"].values()) == payload["totals"]["calls"]


def test_the_projects_listing_names_each_row_s_source() -> None:
    rows = {row["project"]: row["source"] for row in rollups.projects_listing(ITEMS, CONFIGS)}
    assert rows["Acme-Payments"] == rows["Acme-Ledger"] == "fleet"
    assert rows[SANDBOX] == "sandbox" and rows["Acme-Probe"] == "other"
    assert rows["Acme-Payments-Internal"] == "other"


def test_the_existing_fields_keep_their_meaning() -> None:
    payload = _overview()
    assert payload["source"] == "rollups", "The top-level source still says where the numbers are read from"
    assert set(payload["sandbox_split"]) == {"sandbox", "elsewhere"}
    for row in payload["by_project"]:
        assert row["sandbox"] is (row["source"] == "sandbox")


def _schema(path: str) -> dict:
    return SPEC["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]


def test_the_published_contract_documents_the_new_fields() -> None:
    overview = _schema("/api/overview")["properties"]
    assert set(overview["sources"]["properties"]) == set(rollups.SOURCES)
    for name in rollups.SOURCES:
        assert overview["sources"]["properties"][name] == {"$ref": "#/components/schemas/OverviewSource"}
    assert set(SPEC["components"]["schemas"]["OverviewSource"]["properties"]) == {"calls", "projects"}
    listing = _schema("/api/projects")["properties"]["projects"]["items"]["properties"]
    for row in (overview["by_project"]["items"]["properties"], listing):
        assert row["source"]["enum"] == list(rollups.SOURCES)
        for name in rollups.FLEET_PROJECTS:
            assert name in row["source"]["description"], f"{name} is not documented as the fleet's"


def test_the_yaml_twin_carries_the_same_fields() -> None:
    yaml = pytest.importorskip("yaml")
    twin = yaml.safe_load((Path(__file__).resolve().parents[2] / "docs" / "openapi.yaml").read_text(encoding="utf-8"))
    assert twin["components"]["schemas"]["OverviewSource"] == SPEC["components"]["schemas"]["OverviewSource"]
