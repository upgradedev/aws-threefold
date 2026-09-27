"""The overview and the projects listing count the service's own probes apart from real callers.

scripts/probe_live.py checks a deployed stack against every claim the project
makes, and sends its governed calls as the project `Acme-Probe`. A judge
reviewing the public demo found those synthetic calls labelled "Other", mixed
with real callers. They are a source of their own now, `probe`: counted in
`sources.probe` by `GET /api/overview`, and named on their rows.

Only the one name, and only on a stack whose DemoFleet is true, the public one
the probes are pointed at, which every test here is unless it says otherwise:
on another stack a name is only what a caller chose to send. The probes' other
writes keep their own source: the sandbox a run creates is a sandbox, the call
it sends outside the name pattern is recorded as `unlabelled`, and the
scenarios it calls record under the simulation project every visitor's
scenarios use. The function does not ship scripts/, so the application names
the project itself; one test reads the script's own name and fails when the
two drift.

Read from rollup items built here, with no store and no network. Names are
synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from threefold.application import projects as stages
from threefold.application import rollups

ROOT = Path(__file__).resolve().parents[2]
TODAY = datetime.date(2026, 9, 27)
CREATED = "2026-09-27T08:00:00+00:00"
SANDBOX = "Acme-Sandbox-0a1b2c3d"
PROBE = "Acme-Probe"
LIVE = "Acme-Live-billing-credit-limit"
SPEC = json.loads((ROOT / "src" / "threefold" / "web" / "openapi.json").read_text(encoding="utf-8"))


def _rollup(project, day=TODAY, **counters):
    return dict(counters, day=str(day), project=project)


ITEMS = [
    _rollup("Acme-Payments", calls=30, approved=27, observed=2, refused=1, **{"agent:codex": 30}),
    _rollup(LIVE, calls=4, approved=4, **{"agent:claude-code": 4}),
    _rollup(PROBE, calls=9, approved=3, refused=6, **{"agent:page": 6, "agent:claude-code": 3}),
    _rollup(PROBE, day=TODAY - datetime.timedelta(days=1), calls=5, approved=5, **{"agent:claude-code": 5}),
    _rollup(SANDBOX, calls=12, approved=6, observed=6, **{"agent:antigravity": 12}),
    # What else a probe run writes, each under a name that is not the probes' alone.
    _rollup("Acme-Sim", calls=3, refused=1, approved=2, **{"agent:page": 3}),
    _rollup("unlabelled", calls=1, approved=1, **{"agent:page": 1}),
    # Close to the probes' name, and not it.
    _rollup("Acme-Probe-2", calls=2, approved=2, **{"agent:codex": 2}),
]
CONFIGS = {
    "Acme-Payments": stages.new_config(CREATED, stage="enforce"),
    SANDBOX: stages.new_config(CREATED, sandbox=True),
}


def _overview(items=ITEMS, **kwargs):
    return rollups.overview(items, CONFIGS, days=7, today=TODAY, **kwargs)


def _probe_script():
    """scripts/probe_live.py as the owner runs it, under a name of this file's own."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    name = "probe_live_for_sources"
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "probe_live.py")
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses look their module up by name while the class is made.
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def a_stack_that_runs_the_fleet(monkeypatch):
    monkeypatch.setenv(rollups.DEMO_FLEET_ENV, "true")


# --- which projects are the probes' ----------------------------------------------------------------------

def test_the_probes_project_is_a_source_of_its_own() -> None:
    assert rollups.source_of(PROBE) == "probe"


@pytest.mark.parametrize(
    "project, source",
    [
        ("Acme-Sim", "other"),  # every visitor's scenarios record here too
        ("unlabelled", "other"),  # the name the probe sends outside the pattern on purpose is recorded so
        (SANDBOX, "sandbox"),  # a probe run's sandbox is a sandbox like any other
        ("Acme-Probe-2", "other"),
        ("Acme-Probes", "other"),
        ("acme-probe", "other"),
        ("ACME-PROBE", "other"),
        (" Acme-Probe", "other"),
        ("Acme-Probe ", "other"),
        ("probe-0a1b2c3d-not-acme", "other"),
        ("Probe", "other"),
    ],
)
def test_only_the_probes_own_name_is_probe(project, source) -> None:
    assert rollups.source_of(project) == source


def test_the_sources_are_in_the_order_the_pages_list_them() -> None:
    assert rollups.SOURCES == ("fleet", "live", "probe", "sandbox", "other")
    assert not set(rollups.PROBE_PROJECTS) & (set(rollups.FLEET_PROJECTS) | set(rollups.LIVE_PROJECTS))
    assert not any(rollups.is_sandbox(name) for name in rollups.PROBE_PROJECTS)


@pytest.mark.parametrize("value", [None, "", "false", "0", "yes"])
def test_where_the_fleet_does_not_run_the_probes_name_is_other(value, monkeypatch) -> None:
    """On another stack the name proves nothing: a team may send its own work as Acme-Probe, and it is not synthetic."""
    if value is None:
        monkeypatch.delenv(rollups.DEMO_FLEET_ENV, raising=False)
    else:
        monkeypatch.setenv(rollups.DEMO_FLEET_ENV, value)
    assert rollups.source_of(PROBE) == "other"
    payload = _overview()
    assert payload["sources"]["probe"] == {"calls": 0, "projects": 0}
    assert "probe" not in {row["source"] for row in payload["by_project"]}
    assert "probe" not in {row["source"] for row in rollups.projects_listing(ITEMS, CONFIGS)}


def test_the_probes_project_is_the_one_the_script_sends_its_calls_as() -> None:
    """The function cannot import scripts/, so its name is a copy: this is where a drift shows."""
    probe = _probe_script()
    assert probe.PROBE_PROJECT in rollups.PROBE_PROJECTS, \
        "scripts/probe_live.py sends its calls under another name: update PROBE_PROJECTS in application/rollups.py"
    assert rollups.source_of(probe.PROBE_PROJECT) == "probe"
    assert rollups.source_of(probe.SIMULATION_PROJECT) == "other", "The scenarios' project is not the probes' alone"
    assert set(probe.SOURCES) <= set(rollups.SOURCES), "The probe asks for a source the service does not report"


# --- what the overview and the listing count ---------------------------------------------------------------

def test_sources_count_the_probes_calls_and_project_and_still_add_up_to_the_totals() -> None:
    payload = _overview()
    assert payload["sources"] == {
        "fleet": {"calls": 30, "projects": 1},
        "live": {"calls": 4, "projects": 1},
        "probe": {"calls": 14, "projects": 1},
        "sandbox": {"calls": 12, "projects": 1},
        "other": {"calls": 6, "projects": 3},
    }
    assert list(payload["sources"]) == list(rollups.SOURCES)
    assert sum(part["calls"] for part in payload["sources"].values()) == payload["totals"]["calls"] == 66
    assert sum(part["projects"] for part in payload["sources"].values()) == payload["totals"]["projects"] == 7


def test_a_probe_call_is_the_probes_whatever_agent_it_names() -> None:
    """The probes send page calls and hook calls alike; the project is what makes them the probes'."""
    rows = {row["project"]: row for row in _overview()["by_project"]}
    assert rows[PROBE]["source"] == "probe" and rows[PROBE]["calls"] == 14 and rows[PROBE]["refused"] == 6


def test_every_by_project_row_names_the_probe_source() -> None:
    rows = {row["project"]: row["source"] for row in _overview()["by_project"]}
    assert rows == {
        "Acme-Payments": "fleet",
        LIVE: "live",
        PROBE: "probe",
        SANDBOX: "sandbox",
        "Acme-Sim": "other",
        "unlabelled": "other",
        "Acme-Probe-2": "other",
    }


def test_the_probes_overview_counts_only_their_own_calls() -> None:
    payload = _overview(project=PROBE)
    assert payload["sources"]["probe"] == {"calls": 14, "projects": 1}
    assert all(part == {"calls": 0, "projects": 0} for name, part in payload["sources"].items() if name != "probe")


def test_the_projects_listing_names_the_probe_source() -> None:
    rows = {row["project"]: row for row in rollups.projects_listing(ITEMS, CONFIGS)}
    assert rows[PROBE]["source"] == "probe" and rows[PROBE]["sandbox"] is False
    assert rows[PROBE]["agents"] == ["claude-code", "page"]
    assert rows["Acme-Sim"]["source"] == rows["Acme-Probe-2"]["source"] == "other"


def test_the_probes_calls_stay_on_the_non_sandbox_side_of_the_split() -> None:
    """sandbox_split keeps its meaning: probe is a source, not a third side of the split."""
    payload = _overview()
    assert payload["sandbox_split"]["elsewhere"]["calls"] == 66 - 12
    assert payload["sandbox_split"]["elsewhere"]["projects"] == 6


# --- the published contract ---------------------------------------------------------------------------------

def _schema(path: str) -> dict:
    return SPEC["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]


def test_the_published_contract_names_the_probe_source() -> None:
    overview = _schema("/api/overview")["properties"]
    assert overview["sources"]["properties"]["probe"] == {"$ref": "#/components/schemas/OverviewSource"}
    assert "probe" in overview["sources"]["description"]
    listing = _schema("/api/projects")["properties"]["projects"]["items"]["properties"]
    for row in (overview["by_project"]["items"]["properties"], listing):
        assert row["source"]["enum"] == list(rollups.SOURCES)
        described = row["source"]["description"]
        assert "probe for Acme-Probe, exactly" in described
        assert "the service's own probes and page calls included" not in described, "Other no longer claims the probes"
