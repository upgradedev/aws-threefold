"""The overview and the projects listing count the daily live agent's calls apart from everything else.

scripts/daily_live_agent.py runs one real coding agent a day, Claude Code or
Codex, on one of the benchmark's six standard tasks against the public stack,
as the project `Acme-Live-<task>`. Those calls are real, so they are neither
the synthetic fleet's nor other callers': `GET /api/overview` counts them in
`sources.live`, and their rows name the source `live`.

The six names are the live agent's only on a stack whose DemoFleet is true,
the public one it reports to, which every test here is unless it says
otherwise. The function does not ship benchmark/, so the application names the
six projects itself; one test reads the list the daily script really uses and
fails when the two drift.

Read from rollup items built here, with no store and no network. Names are
synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

from threefold.application import projects as stages
from threefold.application import rollups

ROOT = Path(__file__).resolve().parents[2]
TODAY = datetime.date(2026, 9, 27)
CREATED = "2026-09-26T08:00:00+00:00"
SANDBOX = "Acme-Sandbox-0a1b2c3d"
LIVE = "Acme-Live-billing-credit-limit"
LIVE_TOO = "Acme-Live-warehouse-carrier-notify"


def _rollup(project, day=TODAY, **counters):
    return dict(counters, day=str(day), project=project)


ITEMS = [
    _rollup("Acme-Payments", calls=30, approved=27, observed=2, refused=1, **{"agent:codex": 30}),
    _rollup(LIVE, calls=4, approved=4, **{"agent:claude-code": 4}),
    _rollup(LIVE_TOO, day=TODAY - datetime.timedelta(days=1), calls=8, approved=7, refused=1, **{"agent:codex": 8}),
    _rollup(SANDBOX, calls=12, approved=6, observed=6, **{"agent:antigravity": 12}),
    _rollup("Acme-Probe", calls=9, approved=9, **{"agent:claude-code": 9}),
    # A benchmark pressure task and a suffixed name: close to the live agent's, and not its.
    _rollup("Acme-Live-pressure-catalog-shell-regen", calls=5, approved=5, **{"agent:codex": 5}),
    _rollup("Acme-Live-orders-s3-archive-2", calls=2, approved=2, **{"agent:codex": 2}),
]
CONFIGS = {
    "Acme-Payments": stages.new_config(CREATED, stage="enforce"),
    SANDBOX: stages.new_config(CREATED, sandbox=True),
}


def _overview(items=ITEMS, **kwargs):
    return rollups.overview(items, CONFIGS, days=7, today=TODAY, **kwargs)


def _daily_script():
    """scripts/daily_live_agent.py as the scheduled task runs it, under a name of this file's own."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    name = "daily_live_agent_for_sources"
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "daily_live_agent.py")
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses look their module up by name while the class is made.
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def a_stack_that_runs_the_fleet(monkeypatch):
    monkeypatch.setenv(rollups.DEMO_FLEET_ENV, "true")


# --- which projects are the live agent's -----------------------------------------------------------------

@pytest.mark.parametrize("project", rollups.LIVE_PROJECTS)
def test_each_of_the_six_live_projects_is_live(project) -> None:
    assert rollups.source_of(project) == "live"


@pytest.mark.parametrize(
    "project",
    [
        "Acme-Live-pressure-catalog-shell-regen",  # a benchmark task, not a standard one
        "Acme-Live-orders-s3-archive-2",
        "Acme-Live-orders-s3-archive-a1",  # a later attempt's session suffix, never a project's
        "acme-live-orders-s3-archive",
        "Acme-Live-Orders-S3-Archive",
        " Acme-Live-orders-s3-archive",
        "Acme-Live-orders-s3-archive ",
        "Acme-Live-",
        "Acme-Live",
        "Acme-Live-orders",
        "Live-orders-s3-archive",
        "orders-s3-archive",
    ],
)
def test_a_name_close_to_a_live_project_is_other(project) -> None:
    assert rollups.source_of(project) == "other"


def test_the_live_projects_are_neither_the_fleet_s_nor_sandboxes() -> None:
    assert len(rollups.LIVE_PROJECTS) == len(set(rollups.LIVE_PROJECTS)) == 6
    assert rollups.LIVE_PROJECTS == tuple(f"Acme-Live-{task}" for task in rollups.LIVE_TASKS)
    assert not set(rollups.LIVE_PROJECTS) & set(rollups.FLEET_PROJECTS)
    assert not any(rollups.is_sandbox(name) for name in rollups.LIVE_PROJECTS)
    assert rollups.SOURCES.index("live") == rollups.SOURCES.index("fleet") + 1


@pytest.mark.parametrize("value", [None, "", "false", "0", "yes"])
def test_where_the_fleet_does_not_run_the_live_names_are_other(value, monkeypatch) -> None:
    """The daily agent reports only to the public stack; elsewhere the benchmark may name a team's own runs so."""
    if value is None:
        monkeypatch.delenv(rollups.DEMO_FLEET_ENV, raising=False)
    else:
        monkeypatch.setenv(rollups.DEMO_FLEET_ENV, value)
    assert {rollups.source_of(name) for name in rollups.LIVE_PROJECTS} == {"other"}
    payload = _overview()
    assert payload["sources"]["live"] == {"calls": 0, "projects": 0}
    assert "live" not in {row["source"] for row in payload["by_project"]}
    assert "live" not in {row["source"] for row in rollups.projects_listing(ITEMS, CONFIGS)}


def test_the_six_live_projects_are_the_ones_the_daily_script_reports_as() -> None:
    """The function cannot import benchmark/, so its list is a copy: this is where a drift shows."""
    daily = _daily_script()
    assert list(rollups.LIVE_TASKS) == daily.standard_tasks(), \
        "benchmark/tasks' standard family changed: update LIVE_TASKS in application/rollups.py"
    rotation = [daily.pick_for(daily.ROTATION_START + datetime.timedelta(days=n)) for n in range(12)]
    assert {pick.project for pick in rotation} == set(rollups.LIVE_PROJECTS), \
        "The daily script names its projects differently from application/rollups.py"
    assert all(rollups.source_of(pick.project) == "live" for pick in rotation)


# --- what the overview and the listing count ---------------------------------------------------------------

def test_sources_count_the_live_agent_s_calls_and_projects_and_still_add_up_to_the_totals() -> None:
    payload = _overview()
    assert payload["sources"] == {
        "fleet": {"calls": 30, "projects": 1},
        "live": {"calls": 12, "projects": 2},
        "sandbox": {"calls": 12, "projects": 1},
        "other": {"calls": 16, "projects": 3},
    }
    assert list(payload["sources"]) == list(rollups.SOURCES)
    assert sum(part["calls"] for part in payload["sources"].values()) == payload["totals"]["calls"] == 70
    assert sum(part["projects"] for part in payload["sources"].values()) == payload["totals"]["projects"] == 7


def test_every_by_project_row_names_the_live_source() -> None:
    rows = {row["project"]: row["source"] for row in _overview()["by_project"]}
    assert rows == {
        "Acme-Payments": "fleet",
        LIVE: "live",
        LIVE_TOO: "live",
        SANDBOX: "sandbox",
        "Acme-Probe": "other",
        "Acme-Live-pressure-catalog-shell-regen": "other",
        "Acme-Live-orders-s3-archive-2": "other",
    }


def test_one_live_project_s_overview_counts_only_its_own_calls() -> None:
    payload = _overview(project=LIVE_TOO)
    assert payload["sources"]["live"] == {"calls": 8, "projects": 1}
    assert payload["sources"]["fleet"] == payload["sources"]["sandbox"] == payload["sources"]["other"] == {"calls": 0, "projects": 0}


def test_the_projects_listing_names_the_live_source() -> None:
    rows = {row["project"]: row for row in rollups.projects_listing(ITEMS, CONFIGS)}
    assert rows[LIVE]["source"] == rows[LIVE_TOO]["source"] == "live"
    assert rows[LIVE]["sandbox"] is False and rows[LIVE]["agents"] == ["claude-code"]
    assert rows["Acme-Live-pressure-catalog-shell-regen"]["source"] == "other"


def test_the_live_agent_s_calls_stay_on_the_non_sandbox_side_of_the_split() -> None:
    """sandbox_split keeps its meaning: live is a source, not a third side of the split."""
    payload = _overview()
    assert payload["sandbox_split"]["elsewhere"]["calls"] == 70 - 12
    assert payload["sandbox_split"]["elsewhere"]["projects"] == 6
