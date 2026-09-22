"""On a stack deployed with PublicReads=false, a read is closed unless it is listed open.

The stack that carries real use is deployed with `PublicReads=false`, and its
ledger, its session list and its rules are the operator's alone. That rule used
to be carried by one list of paths, `PAGE_READS`, and the router answered two of
those reads under a second name each: `/sessions.json` beside `/api/sessions`
and `/insights.json` beside `/api/insights`. The second names were not on the
list, so they were not decided as private reads at all. They fell through to the
key check at the end of the middleware, which asks for a key only where `STAGE`
is prod or `ENFORCE_API_KEY` is set, and the template sets neither. A caller
with no credential read the private stack's session ids, project aliases and
ledger summary from them.

Two things are pinned here, because fixing the two names alone would leave the
next alias to be found the same way:

* the reads the router answers under two names are decided by one table that
  the router itself spells its dispatch with, so an alias cannot be missing
  from it; and
* every other GET on such a stack is closed by default. The walk below derives
  the paths from the router modules rather than from a list written by hand, so
  a route added later is covered by this test on the day it is added.

The public demo is untouched: `reads_are_public()` is true there and every
check below short-circuits, which the last tests state as their own assertions.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
from typing import Any, Set

import pytest

from threefold.infrastructure.security_middleware import (
    INSIGHTS_READ_PATHS,
    PAGE_READS,
    PUBLIC_PATHS,
    SESSIONS_READ_PATHS,
)
from threefold.interfaces import access_routes, api_handlers, app_routes, draft_routes
from threefold.interfaces.api_handlers import lambda_handler

OPERATOR_KEY = "acme-operator-key-private-reads"

# Every module that answers a request, so a path any of them serves is walked.
ROUTER_MODULES = (api_handlers, app_routes, access_routes, draft_routes)

# A segment standing in for the parts of a path a caller fills in: a session id
# under /sessions/, a project name under /api/projects/, an asset's file name.
SAMPLE_SEGMENT = "Acme-Probe-1"


def _looks_like_a_path(value: Any) -> bool:
    """A string that could be a path this service routes on."""
    return (
        isinstance(value, str)
        and value.startswith("/")
        and len(value) < 80
        and " " not in value
        and "\n" not in value
    )


def _paths_inside(value: Any, depth: int = 0) -> Set[str]:
    """Every path-shaped string in a route table, however it is spelled.

    Route tables are dicts keyed by a path or by (method, path), tuples of the
    names one read answers to, and plain constants. All three are read here, so
    a table written in any of those shapes is walked.
    """
    if isinstance(value, str):
        return {value} if _looks_like_a_path(value) else set()
    if depth > 2:
        return set()
    found: Set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found |= _paths_inside(key, depth + 1) | _paths_inside(item, depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            found |= _paths_inside(item, depth + 1)
    return found


def _router_paths() -> Set[str]:
    """The paths the router modules name, read from the router rather than listed here.

    Two sources, because a route is spelled either way: a literal compared with
    the request's path inside `_route`, and a table of routes at module level.
    The tables are read from the loaded module, so a path imported from another
    module (the two reads that answer to two names) is found as well.
    """
    found: Set[str] = set()
    for module in ROUTER_MODULES:
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Constant) and _looks_like_a_path(node.value):
                found.add(node.value)
        for value in vars(module).values():
            found |= _paths_inside(value)
    # What a caller puts in a path as well as the fixed part of it.
    return found | {f"{path.rstrip('/')}/{SAMPLE_SEGMENT}" for path in found}


ROUTER_GET_PATHS = sorted(_router_paths())


def _as_routed(path: str) -> str:
    """The path the middleware is handed, which is the one the router normalized.

    Repeated slashes are collapsed and a trailing slash is dropped before any
    decision is made, so "/assets/" is not the assets prefix and "//" is the
    root page. Asking the door about the path as written would ask it about a
    path it never sees.
    """
    collapsed = re.sub(r"/{2,}", "/", path).rstrip("/")
    return collapsed or "/"


# What a private stack hands to anyone: the pages a reader needs to adopt the
# product or to sign in, the hook and the installer, the contract and the
# committed proof snapshot. Written out rather than imported from the module
# under test, so the walk below checks behaviour against a decision rather than
# against the list that decides it. Adding a path to the middleware's own list
# fails the pin under it until someone writes the path down here too, which is
# the point: opening a read on the stack that carries real use is a decision.
DECLARED_PUBLIC = {
    "/",
    "/index.html",
    "/connect.html",
    "/console.html",
    "/rules.html",
    "/sessions.html",
    "/settings.html",
    "/swagger.html",
    "/status",
    "/health",
    "/docs",
    "/openapi.json",
    "/openapi.yaml",
    "/proof.json",
    "/hooks/threefold_hook.py",
    "/hooks/claude_code_hook.py",
    "/claude_code_hook.py",
    "/dashboard.html",
    "/app",
    "/install.py",
    "/dist/threefold-bundle.zip",
    "/dist/manifest.json",
    "/api/auth/sessions",
    "/api/auth/whoami",
}
DECLARED_PUBLIC_PREFIX = "/assets/"


def test_the_open_list_is_the_one_this_file_was_written_against() -> None:
    """The pin: a path opened later is a path this file has to name as well."""
    assert set(PUBLIC_PATHS) == DECLARED_PUBLIC


def _declared_public(path: str) -> bool:
    """Whether this path is one of those, as the request's path is routed."""
    routed = _as_routed(path)
    return routed in DECLARED_PUBLIC or routed.startswith(DECLARED_PUBLIC_PREFIX)


def _get(path: str, headers: dict | None = None) -> int:
    response = lambda_handler(
        {
            "rawPath": f"/prod{path}",
            "headers": dict({"host": "acme-probe.invalid"}, **(headers or {})),
            "requestContext": {"http": {"method": "GET"}, "stage": "prod"},
        }
    )
    return response["statusCode"]


def _body(path: str, headers: dict | None = None) -> dict:
    response = lambda_handler(
        {
            "rawPath": f"/prod{path}",
            "headers": dict({"host": "acme-probe.invalid"}, **(headers or {})),
            "requestContext": {"http": {"method": "GET"}, "stage": "prod"},
        }
    )
    return json.loads(response["body"])


@pytest.mark.parametrize("path", ROUTER_GET_PATHS)
def test_every_get_the_router_answers_is_open_by_name_or_closed(monkeypatch, path) -> None:
    """The walk: on a private stack each GET is either declared public or 401."""
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)
    status = _get(path)
    if _declared_public(path):
        assert status not in (401, 403), (
            f"GET {path} is listed as open on every stack and answered {status}"
        )
    else:
        assert status == 401, (
            f"GET {path} is not declared public, so a caller with no credential must be "
            f"refused on a stack with PublicReads=false; it answered {status}"
        )


def test_the_walk_reaches_the_reads_it_was_written_for() -> None:
    """A derivation that stopped finding paths would pass the walk while checking nothing."""
    walked = set(ROUTER_GET_PATHS)
    for path in (
        "/api/sessions",
        "/sessions.json",
        "/api/insights",
        "/insights.json",
        "/api/overview",
        "/api/decisions",
        "/proof.json",
        f"/sessions/{SAMPLE_SEGMENT}",
        f"/api/projects/{SAMPLE_SEGMENT}",
    ):
        assert path in walked, f"the walk no longer derives {path} from the router"


def test_the_router_and_the_door_name_the_same_reads() -> None:
    """The two reads with two names each are decided by one table, so neither can drift."""
    assert set(SESSIONS_READ_PATHS) | set(INSIGHTS_READ_PATHS) <= PAGE_READS
    assert "/sessions.json" in SESSIONS_READ_PATHS
    assert "/insights.json" in INSIGHTS_READ_PATHS


@pytest.mark.parametrize("path", ["/sessions.json", "/insights.json"])
def test_an_alias_is_as_private_as_the_read_it_answers_for(monkeypatch, path) -> None:
    """What /api/sessions and /api/insights already did, under their other name."""
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)

    assert _get(path) == 401, "A missing key is 401"
    assert _get(path, {"X-API-Key": "not-the-key"}) == 403, "A wrong key is 403"
    assert _get(path, {"X-API-Key": OPERATOR_KEY}) == 200, "The operator reads it"


@pytest.mark.parametrize("path", ["/sessions.json", "/insights.json"])
def test_an_alias_on_a_private_stack_with_no_key_says_the_reads_are_private(monkeypatch, path) -> None:
    """The same answer its /api/ spelling gives, from the same ladder."""
    monkeypatch.setenv("PUBLIC_READS", "false")
    assert _get(path) == 403
    assert _body(path)["type"] == "urn:threefold:error:reads-private"


def test_a_read_nobody_listed_is_closed_rather_than_open(monkeypatch) -> None:
    """The class of bug: a path the router serves that nobody added to a list.

    A path this service does not route at all stands in for the route that has
    not been written yet. On a private stack it is refused; on the public demo
    it is the 404 it always was.
    """
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)
    monkeypatch.setenv("PUBLIC_READS", "false")
    assert _get("/api/a-read-added-next-week") == 401
    monkeypatch.setenv("PUBLIC_READS", "true")
    assert _get("/api/a-read-added-next-week") == 404


@pytest.mark.parametrize("path", ["/sessions.json", "/insights.json", "/api/sessions", "/api/insights"])
def test_the_public_demo_answers_every_one_of_them_anonymously(monkeypatch, path) -> None:
    """PublicReads defaults to true and the ship gate depends on it."""
    monkeypatch.setenv("PUBLIC_READS", "true")
    assert _get(path) == 200


@pytest.mark.parametrize("path", ["/", "/index.html", "/console.html", "/dashboard.html", "/install.py"])
def test_a_private_stack_still_hands_out_the_pages_and_the_installer(monkeypatch, path) -> None:
    """A stack whose dashboard will not open is a stack nobody can sign in to."""
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)
    assert _get(path) == 200
