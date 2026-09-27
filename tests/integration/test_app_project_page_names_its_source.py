"""A project's own answer names where its calls come from, as its row in the listing does.

The project page draws the Fleet, Live, Probe or Sandbox chip from
GET /api/projects/<name>, so that answer carries the same `source` as the
project's row in GET /api/projects, and the published document says so.
Driven through `lambda_handler` with API Gateway v2 events, as deployed.

The source is read off the name and the stack, so the demo's own names are
asked about without sending them a call: a call would change the counts
other tests read for the fleet's projects. Names are synthetic, as the
clean-room rule requires.
"""
from __future__ import annotations

import pytest

from test_app_support import README, fresh_project, get, hook_call, post
from threefold.application import rollups


@pytest.mark.parametrize(
    "project, source",
    [
        (rollups.FLEET_PROJECTS[0], "fleet"),
        (rollups.LIVE_PROJECTS[0], "live"),
        (rollups.PROBE_PROJECTS[0], "probe"),
        ("Acme-Sandbox-0a1b2c3d", "sandbox"),
        ("Acme-Team-Portal", "other"),
    ],
)
def test_the_project_answer_names_its_source_where_the_fleet_runs(monkeypatch, project, source) -> None:
    monkeypatch.setenv("DEMO_FLEET", "true")
    assert get(f"/api/projects/{project}")["source"] == source


def test_off_the_fleet_the_demo_names_are_other(monkeypatch) -> None:
    """A stack that does not run the fleet does not call a team's own project the fleet's, the live agent's or the probes'."""
    monkeypatch.setenv("DEMO_FLEET", "false")
    for project in (rollups.FLEET_PROJECTS[0], rollups.LIVE_PROJECTS[0], rollups.PROBE_PROJECTS[0]):
        assert get(f"/api/projects/{project}")["source"] == "other"
    assert get("/api/projects/Acme-Sandbox-0a1b2c3d")["source"] == "sandbox"


def test_the_page_and_the_row_it_was_opened_from_say_the_same(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_FLEET", "true")
    project = fresh_project()
    hook_call(project, f"{project}-s", README, tool="Read", action="FILE_READ")
    sandbox = post("/api/sandbox", {})["project"]
    listed = {item["project"]: item for item in get("/api/projects")["projects"]}
    for name, source in ((project, "other"), (sandbox, "sandbox")):
        assert listed[name]["source"] == source
        assert get(f"/api/projects/{name}")["source"] == source


def test_the_published_document_names_the_field() -> None:
    paths = get("/openapi.json")["paths"]
    answer = paths["/api/projects/{name}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    listing = paths["/api/projects"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    row = listing["properties"]["projects"]["items"]["properties"]
    assert answer["properties"]["source"]["enum"] == row["source"]["enum"] == list(rollups.SOURCES)
