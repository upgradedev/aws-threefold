"""Each console page links to the operation it is built on, not just to the docs.

A link to the front page of a spec makes a reader hunt for the endpoint. These
pages deep link, which means they carry an anchor that Swagger UI has to
recognise. That anchor is derived from the method and the path, so renaming a
route silently breaks every link pointing at it. This derives the same anchors
from the served document and fails when a page points at one that is not there.
"""
from __future__ import annotations

import functools
import json
import re

import pytest

from threefold.interfaces.api_handlers import lambda_handler


def _get(path: str, stage: str = "prod") -> dict:
    return lambda_handler(
        {
            "rawPath": f"/{stage}{path}",
            "headers": {},
            "requestContext": {"http": {"method": "GET"}, "stage": stage},
        }
    )


@functools.lru_cache(maxsize=1)
def _spec() -> dict:
    """One read per session. Every call is metered, including the suite's own."""
    return json.loads(_get("/openapi.json")["body"])


def _spec_anchors() -> set[str]:
    """Rebuilds the anchors Swagger UI generates for a spec with no operationIds."""
    anchors = set()
    spec = _spec()
    for path, operations in spec["paths"].items():
        for method in operations:
            anchors.add(f"{method.lower()}{re.sub(r'[^A-Za-z0-9]', '_', path)}")
    return anchors


CONSOLE_PAGES = ["/", "/settings.html", "/sessions.html", "/connect.html"]


@pytest.mark.parametrize("page", CONSOLE_PAGES)
def test_each_console_page_reaches_the_published_document(page: str) -> None:
    assert "swagger.html" in _get(page)["body"], f"{page} offers no way to the contract"


@pytest.mark.parametrize("page", CONSOLE_PAGES)
def test_every_deep_link_points_at_an_operation_that_exists(page: str) -> None:
    body = _get(page)["body"]
    # The anchor is joined to the page base in script, so it is matched on its own
    # rather than as a literal href.
    linked = set(re.findall(r"#/default/([A-Za-z0-9_]+)", body))
    assert linked, f"{page} links to the spec's front page only"

    missing = linked - _spec_anchors()
    assert not missing, f"{page} deep links to operations the spec does not document: {sorted(missing)}"


def test_the_pages_link_to_the_operations_they_actually_call() -> None:
    """A link to some operation is not the same as a link to the right one."""
    expected = {
        "/settings.html": {"get_policy_config", "post_policy_config"},
        "/sessions.html": {
            "get_api_sessions",
            "get_sessions__session_id_",
            "post_sessions__session_id__terminate",
        },
        "/connect.html": {"post_evaluate_tool_call"},
    }
    for page, operations in expected.items():
        linked = set(re.findall(r"#/default/([A-Za-z0-9_]+)", _get(page)["body"]))
        assert operations <= linked, f"{page} is missing links to {sorted(operations - linked)}"


def _spec_path_for(called_path: str):
    """Matches a path a page actually calls against the templated path in the spec.

    `/sessions/session-default/terminate` is the same operation as the documented
    `/sessions/{session_id}/terminate`, so the comparison is segment by segment
    with a template segment matching anything.
    """
    called_segments = called_path.split("/")
    for documented in _spec()["paths"]:
        documented_segments = documented.split("/")
        if len(documented_segments) != len(called_segments):
            continue
        if all(
            (spec_segment.startswith("{") and spec_segment.endswith("}")) or spec_segment == called_segment
            for spec_segment, called_segment in zip(documented_segments, called_segments)
        ):
            return documented
    return None


def test_the_dashboard_links_every_operation_it_calls() -> None:
    """The strip claims to list what the buttons call. That claim is checked here.

    A scenario that starts calling a different route, or a route that is renamed,
    would otherwise leave the dashboard pointing a reader at the wrong operation
    while still looking complete.
    """
    body = _get("/")["body"]
    called = set(re.findall(r"fetch\(`\$\{(?:base|apiBase)\}(/[^`?]+)`", body))
    assert called, "No endpoint calls were found on the dashboard, so this test proves nothing"

    linked = set(re.findall(r"#/default/([A-Za-z0-9_]+)", body))
    for path in sorted(called):
        documented = _spec_path_for(path)
        assert documented, f"The dashboard calls {path}, which the spec does not document"
        suffix = re.sub(r"[^A-Za-z0-9]", "_", documented)
        assert any(anchor.endswith(suffix) for anchor in linked), (
            f"The dashboard calls {path} and links to no entry for {documented}"
        )
