"""Every path the function answers reaches it through the edge, and no page does.

The distribution's default behavior serves the bucket, so a route the edge does
not list is not an error anyone sees at deploy time: it answers the bucket's 403
from then on. This test is the list's other half. It collects the routes from
the code that answers them (every comparison of `path` in
src/threefold/interfaces/, the served-script table, the paths in the published
OpenAPI document) and from the 2026-09-22 contract for the routes other tracks
are building now, and resolves each one the way CloudFront does: first matching
pattern wins, `*` is any run of characters, `?` one character, case counts.
Every JSON route must land on the API origin; every page, /app and every asset
on the bucket, and no API pattern may match a page even out of order.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pytest

REPO = Path(__file__).resolve().parents[2]
INTERFACES = REPO / "src" / "threefold" / "interfaces"
WEB_ROOT = REPO / "src" / "threefold" / "web"
# CloudFront's default quota was 25 cache behaviors when this edge was designed;
# the default behavior is counted too, the conservative reading.
BEHAVIOR_QUOTA = 25


def _reader():
    name = "edge_cfn_yaml_reader"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("test_edge_cfn_yaml.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


DISTRIBUTION = _reader().load_edge_template()["Resources"]["Distribution"]["Properties"]["DistributionConfig"]
BEHAVIORS: List[Tuple[str, str]] = [(b["PathPattern"], b["TargetOriginId"]) for b in DISTRIBUTION["CacheBehaviors"]]
DEFAULT_ORIGIN = DISTRIBUTION["DefaultCacheBehavior"]["TargetOriginId"]

# The routes the 2026-09-22 contract adds, which tracks A, B1 and B2 are building
# in parallel and which are not in this tree yet. One concrete path per route.
CONTRACT_API_PATHS = [
    "/evaluate-tool-call",
    "/api/overview",
    "/api/decisions",
    "/api/decision",
    "/api/projects",
    "/api/projects/Acme-Ledger",
    "/api/projects/Acme-Ledger/promote",
    "/api/projects/Acme-Ledger/demote",
    "/api/projects/Acme-Ledger/reviews",
    "/api/sandbox",
    "/api/auth/links",
    "/api/auth/sessions",
    "/api/auth/whoami",
    "/api/sessions",
    "/api/insights",
    "/install.py",
    "/dist/threefold-bundle.zip",
    "/dist/manifest.json",
    "/hooks/threefold_hook.py",
    "/hooks/claude_code_hook.py",
    "/claude_code_hook.py",
    "/rules",
    "/rules/",
    "/rules/explain",
    "/rules/layering",
    "/policy",
    "/policy/config",
    "/sessions/acme-session-1",
    "/sessions/acme-session-1/terminate",
    "/sessions/acme-session-1/resume",
    "/sessions.json",
    "/insights.json",
    "/status",
    "/health",
    "/ready",
    "/readyz",
    "/openapi.json",
    "/docs/openapi.json",
    "/simulate-loop",
    "/simulate-secret",
    "/issue-certificate",
    "/universal-eval",
    "/adapter/universal-tool-call",
]
# Served from the bucket: the pages the contract names, /app, and the assets.
CONTRACT_PAGE_PATHS = [
    "/",
    "/index.html",
    "/dashboard.html",
    "/app",
    "/console.html",
    "/connect.html",
    "/sessions.html",
    "/rules.html",
    "/settings.html",
    "/swagger.html",
    "/assets/threefold.js",
    "/assets/threefold.css",
    "/assets/threefold.svg",
]
# Stands in for a path parameter or the tail of a prefix route.
SAMPLE_SEGMENT = "acme-sample"


def is_page(path: str) -> bool:
    """A path the bucket serves: a page, the root, /app, or an asset."""
    return path in ("/", "/app") or path.endswith(".html") or path.startswith("/assets/")


def pattern_matches(pattern: str, path: str) -> bool:
    """CloudFront's path pattern semantics: '*' any run, '?' one character, case-sensitive."""
    if not pattern.startswith("/"):
        pattern = "/" + pattern
    expression = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(expression, path, flags=re.DOTALL) is not None


def origin_for(path: str) -> str:
    for pattern, origin in BEHAVIORS:
        if pattern_matches(pattern, path):
            return origin
    return DEFAULT_ORIGIN


def _constant_strings(node: ast.AST) -> List[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    if isinstance(node, ast.Dict):
        return [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("frozenset", "set", "tuple"):
        return [s for arg in node.args for s in _constant_strings(arg)]
    return []


def routes_in_source(source: str) -> Tuple[Set[str], Set[str]]:
    """(exact routes, prefix routes) a module compares `path` against."""
    tree = ast.parse(source)
    named: Dict[str, List[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    named[target.id] = _constant_strings(node.value)
    exact: Set[str] = set()
    prefixes: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "path":
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.In)):
                    values = _constant_strings(comparator)
                    if isinstance(op, ast.In) and isinstance(comparator, ast.Name):
                        values = named.get(comparator.id, [])
                    exact.update(v for v in values if v.startswith("/"))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "path"
        ):
            for arg in node.args:
                prefixes.update(v for v in _constant_strings(arg) if v.startswith("/"))
    return exact, prefixes


def routes_in_code() -> Tuple[Set[str], Set[str]]:
    exact: Set[str] = set()
    prefixes: Set[str] = set()
    for module in sorted(INTERFACES.glob("*.py")):
        found_exact, found_prefixes = routes_in_source(module.read_text(encoding="utf-8"))
        exact |= found_exact
        prefixes |= found_prefixes
    return exact, prefixes


def routes_in_openapi() -> Set[str]:
    document = json.loads((WEB_ROOT / "openapi.json").read_text(encoding="utf-8"))
    return {re.sub(r"\{[^}]+\}", SAMPLE_SEGMENT, path) for path in document.get("paths", {})}


def concrete(exact: Iterable[str], prefixes: Iterable[str]) -> Set[str]:
    """Every route as a path a request would carry."""
    return set(exact) | {prefix + SAMPLE_SEGMENT for prefix in prefixes}


def page_paths() -> Set[str]:
    paths = set(CONTRACT_PAGE_PATHS) | {f"/{page.name}" for page in WEB_ROOT.glob("*.html")}
    assets = WEB_ROOT / "assets"
    if assets.is_dir():
        paths |= {"/assets/" + p.relative_to(assets).as_posix() for p in assets.rglob("*") if p.is_file()}
    return paths


CODE_EXACT, CODE_PREFIXES = routes_in_code()
CODE_ROUTES = concrete(CODE_EXACT, CODE_PREFIXES)
API_PATHS = sorted(
    {p for p in CODE_ROUTES | routes_in_openapi() if not is_page(p)} | set(CONTRACT_API_PATHS)
)
PAGE_PATHS = sorted(page_paths() | {p for p in CODE_ROUTES if is_page(p)})


# --- the matcher and the collectors are right before anything relies on them ----------


@pytest.mark.parametrize(
    "pattern, path, expected",
    [
        ("/api/*", "/api/overview", True),
        ("/api/*", "/api", False),
        ("/rules/*", "/rules/", True),
        ("/rules", "/rules.html", False),
        ("/sessions/*", "/sessions.html", False),
        ("/ready*", "/readyz", True),
        ("/simulate-*", "/simulate-loop", True),
        ("/status", "/Status", False),
        ("/a?c", "/abc", True),
        ("api/*", "/api/x", True),
    ],
)
def test_the_matcher_follows_cloudfront_s_pattern_rules(pattern: str, path: str, expected: bool) -> None:
    assert pattern_matches(pattern, path) is expected


def test_the_collector_reads_every_form_a_route_is_written_in() -> None:
    source = (
        "SERVED = {'/hooks/x.py': 'x.py'}\n"
        "def route(path):\n"
        "    if path == '/one': pass\n"
        "    if path in ('/two', '/three'): pass\n"
        "    if path in SERVED: pass\n"
        "    if path.startswith('/four/') and path.endswith('/end'): pass\n"
        "    if path.startswith(f'/{stage}/'): pass\n"
        "    other = 'not-a-route'\n"
    )
    exact, prefixes = routes_in_source(source)
    assert exact == {"/one", "/two", "/three", "/hooks/x.py"}
    assert prefixes == {"/four/"}


def test_the_collector_found_the_routes_the_handler_is_known_to_have() -> None:
    """If this fails the scan has gone blind, and every coverage test below would pass vacuously."""
    assert {"/evaluate-tool-call", "/status", "/readyz", "/openapi.json", "/hooks/threefold_hook.py"} <= CODE_EXACT
    assert "/sessions/" in CODE_PREFIXES
    assert "/" in CODE_EXACT and "/rules.html" in CODE_EXACT, "the pages table was read"


# --- the coverage itself ------------------------------------------------------------------


def test_the_distribution_stays_inside_the_behavior_quota() -> None:
    assert len(BEHAVIORS) + 1 <= BEHAVIOR_QUOTA, (
        f"{len(BEHAVIORS)} cache behaviors plus the default exceed {BEHAVIOR_QUOTA}; merge patterns before adding one"
    )


def test_each_pattern_is_rooted_and_listed_once() -> None:
    patterns = [pattern for pattern, _ in BEHAVIORS]
    assert len(set(patterns)) == len(patterns)
    assert all(pattern.startswith("/") for pattern in patterns)


def test_the_bucket_is_the_default_so_an_unlisted_path_never_reaches_the_function() -> None:
    assert DEFAULT_ORIGIN == "web"


@pytest.mark.parametrize("path", API_PATHS)
def test_every_json_route_reaches_the_api(path: str) -> None:
    assert origin_for(path) == "api", f"{path} would be answered by the bucket, not the function"


@pytest.mark.parametrize("path", PAGE_PATHS)
def test_every_page_and_asset_is_served_from_the_bucket(path: str) -> None:
    assert origin_for(path) == "web", f"{path} would be sent to the function, not the bucket"


@pytest.mark.parametrize("path", PAGE_PATHS)
def test_no_api_pattern_matches_a_page_even_out_of_order(path: str) -> None:
    """Behaviors get reordered; a page must not depend on sitting above an API pattern."""
    overlapping = [p for p, origin in BEHAVIORS if origin == "api" and pattern_matches(p, path)]
    assert not overlapping, f"{path} is matched by API patterns {overlapping}"


def test_every_api_pattern_is_needed_by_some_route() -> None:
    """A pattern no route uses is a path to the function nobody meant to open."""
    for pattern, origin in BEHAVIORS:
        if origin == "api":
            assert any(pattern_matches(pattern, path) for path in API_PATHS), f"{pattern} serves no known route"


def test_the_hook_s_own_request_reaches_the_api() -> None:
    """The hook posts to <endpoint>evaluate-tool-call; pointed at the edge, that is this path."""
    endpoint = "https://d1acme.cloudfront.net/"
    path = "/" + (endpoint + "evaluate-tool-call").split("/", 3)[3]
    assert origin_for(path) == "api"
