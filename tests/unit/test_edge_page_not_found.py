"""A page address with no page answers 404.html with 404 at the edge, and nothing else changes.

deploy/edge.yml runs two CloudFront Functions on the default behavior, the one
that serves pages, and on no other: /assets/* and every API behavior keep their
answers, and no custom error response is used, since one would replace the
API's RFC 7807 problems too. MissingPageFunction, on the viewer request, sends
a page address (its last segment names no file type, or .html) that is not a
published page to /404.html and marks the request with
X-Threefold-Page-Not-Found, after dropping any such header the viewer sent.
MissingPageStatusFunction, on the viewer response, makes that 200 a 404 and
leaves a 304 or a 206 alone. A 400 or above from the bucket runs no viewer
response function ("CloudFront Functions event structure" in the developer
guide), so a missing script or image keeps S3's own 404. The rewrite comes
before the cache is read, so every address with no page shares the one cached
/404.html, and the mark never reaches the bucket, whose cache policy forwards
no header. Each function runs on every request to the default behavior, and
each run is billed.

The guide says the request in a function's event is the one CloudFront
received from the viewer, which leaves open whether a viewer response sees the
viewer request function's changes. So the status function repeats the page
test on the request it is handed, and the code above each handler is one text
in both functions. Were it handed the request as the viewer sent it, a mark
the viewer sent would reach it too, and could turn only that viewer's own
answer into a 404, since the status is set after the cache.

Held here: the page list is exactly what publish_web.py publishes (every key
but the assets) plus the root, so a page added or removed fails this file
until the list follows, and a new page answers 404 at the edge until the stack
is deployed with it. The code is run by node against events of every shape the
default behavior can receive: the pages and their aliases, a query string, a
trailing slash, unknown addresses, files of other types, and the bare API
prefixes that fall through to the pages. And the two functions together answer
404 whether the viewer response is handed the rewritten request or the one the
viewer sent.

Nothing here calls AWS. The code runs under node, the closest runtime this
machine has to cloudfront-js-2.0; without node those tests skip, and the page
list is still checked against the published pages.
"""
from __future__ import annotations

import functools
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def _load(name: str, path: Path):
    """A module by path, once. Registered before it runs: a dataclass looks its own module up by name."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


READER = _load("edge_cfn_yaml_reader", HERE / "test_edge_cfn_yaml.py")
# The route table and CloudFront's matching, as the behavior tests read them.
ROUTES = _load("edge_behaviors_for_missing_pages", HERE / "test_edge_behaviors.py")
# The header rules edge functions live under, as the template tests list them.
EDGE_RULES = _load("edge_template_for_missing_pages", HERE / "test_edge_template.py")
publish_web = _load("publish_web_for_missing_pages", REPO / "scripts" / "publish_web.py")

TEMPLATE = READER.load_edge_template()
RESOURCES = TEMPLATE["Resources"]
DISTRIBUTION = RESOURCES["Distribution"]["Properties"]["DistributionConfig"]
DEFAULT = DISTRIBUTION["DefaultCacheBehavior"]
REQUEST_FUNCTION = RESOURCES["MissingPageFunction"]
RESPONSE_FUNCTION = RESOURCES["MissingPageStatusFunction"]
REQUEST_CODE: str = REQUEST_FUNCTION["Properties"]["FunctionCode"]
RESPONSE_CODE: str = RESPONSE_FUNCTION["Properties"]["FunctionCode"]

NOT_FOUND_PAGE = "/404.html"
MARK = "x-threefold-page-not-found"
# CloudFront's limit on a function's code, from its quotas page.
FUNCTION_CODE_LIMIT = 10 * 1024
HANDLER = "function handler(event) {"

# The one declaration the list may live in, and the one shape each of its lines may
# take, a run of entries: a line that is not fails the parse rather than dropping out.
_PAGES_LITERAL = re.compile(r"^var PAGES = \{\n(?P<body>.*?)\n\};$", re.MULTILINE | re.DOTALL)
_ENTRY = r"'/[^'\\]*': true"
_LINE = re.compile(rf"    (?:{_ENTRY}, )*{_ENTRY},?")
_PATH = re.compile(r"'(/[^'\\]*)': true")


def pages_in(code: str) -> Set[str]:
    """The addresses a function's PAGES literal names, read strictly."""
    found = _PAGES_LITERAL.findall(code)
    assert len(found) == 1, "one PAGES literal per function"
    assert code.count("PAGES") == 2, "PAGES is declared once and read once, never added to"
    paths: List[str] = []
    for line in found[0].split("\n"):
        assert _LINE.fullmatch(line), f"not a line of page entries: {line!r}"
        paths.extend(_PATH.findall(line))
    assert len(set(paths)) == len(paths), "an address listed twice"
    return set(paths)


@functools.lru_cache(maxsize=None)
def published_keys() -> Tuple[str, ...]:
    return tuple(item.key for item in publish_web.collect(publish_web.DEFAULT_WEB_ROOT))


def published_pages() -> Set[str]:
    """Every address the bucket holds a page at: the keys but the assets, and the root."""
    return {"/" + key for key in published_keys() if not key.startswith("assets/")} | {"/"}


PAGES = sorted(published_pages())


# --- the list is the published pages ---------------------------------------------------


def test_the_parse_reads_every_entry_or_refuses() -> None:
    code = "var PAGES = {\n    '/': true, '/app': true,\n    '/x.html': true\n};\nPAGES[uri] !== true"
    assert pages_in(code) == {"/", "/app", "/x.html"}
    for broken in (
        "var PAGES = {\n    '/': true, // '/app': true\n};\nPAGES[uri]",
        "var PAGES = {\n    '/': true,\n    '/app': false\n};\nPAGES[uri]",
        "var PAGES = {\n    '/': true,, '/app': true\n};\nPAGES[uri]",
        "var PAGES = {\n    '/': true\n};\nPAGES['/extra'] = true;\nPAGES[uri]",
        "var PAGES = {\n    '/': true, '/': true\n};\nPAGES[uri]",
    ):
        with pytest.raises(AssertionError):
            pages_in(broken)


def test_the_parse_found_the_pages_known_to_be_listed() -> None:
    """If this fails the parse has gone blind, and the drift test below would compare nothing."""
    assert {"/", "/index.html", "/app", NOT_FOUND_PAGE, "/dashboard.html"} <= pages_in(REQUEST_CODE)


@pytest.mark.parametrize("code", [REQUEST_CODE, RESPONSE_CODE], ids=["viewer-request", "viewer-response"])
def test_the_page_list_is_exactly_what_publish_web_publishes(code: str) -> None:
    listed = pages_in(code)
    published = published_pages()
    assert listed == published, (
        f"published but not listed (the edge would answer them 404): {sorted(published - listed)}; "
        f"listed but not published: {sorted(listed - published)}"
    )


def test_both_functions_decide_with_the_same_code() -> None:
    """Everything above the handler is one text, so the two can never disagree about a page."""
    assert REQUEST_CODE.count(HANDLER) == 1 and RESPONSE_CODE.count(HANDLER) == 1
    assert REQUEST_CODE.split(HANDLER)[0] == RESPONSE_CODE.split(HANDLER)[0]
    assert f"var MARK = '{MARK}';" in REQUEST_CODE


def test_the_root_and_the_not_found_page_are_published_objects() -> None:
    keys = published_keys()
    assert DISTRIBUTION["DefaultRootObject"] in keys, "/ is answered with an object the publish writes"
    assert NOT_FOUND_PAGE.lstrip("/") in keys
    assert (publish_web.DEFAULT_WEB_ROOT / "404.html").is_file()


@pytest.mark.parametrize("path", PAGES)
def test_every_listed_page_reaches_the_default_behavior(path: str) -> None:
    """The functions run on the default behavior only; a page another pattern takes never meets them."""
    matched = [pattern for pattern, _ in ROUTES.BEHAVIORS if ROUTES.pattern_matches(pattern, ROUTES.normalize(path))]
    assert not matched, f"{path} is taken by {matched}"


# --- where the functions run, and as what ----------------------------------------------


@pytest.mark.parametrize(
    "resource, name",
    [("MissingPageFunction", "missing-page"), ("MissingPageStatusFunction", "missing-page-status")],
)
def test_each_function_is_published_on_the_current_runtime_as_plain_code(resource: str, name: str) -> None:
    properties = RESOURCES[resource]["Properties"]
    assert RESOURCES[resource]["Type"] == "AWS::CloudFront::Function"
    assert properties["AutoPublish"] is True, "only a LIVE function can be associated"
    assert properties["FunctionConfig"]["Runtime"] == "cloudfront-js-2.0"
    assert 0 < len(properties["FunctionConfig"]["Comment"]) <= 128
    assert properties["Name"] == {"Fn::Sub": "${AWS::StackName}-" + name}, "a second edge must not collide"
    code = properties["FunctionCode"]
    assert isinstance(code, str), "plain code, never a !Sub that could eat ${...}"
    assert "${" not in code
    assert len(code.encode("utf-8")) < FUNCTION_CODE_LIMIT


def test_the_pages_behavior_runs_one_function_per_event_and_no_other_behavior_runs_them() -> None:
    assert DEFAULT["FunctionAssociations"] == [
        {"EventType": "viewer-request", "FunctionARN": {"Fn::GetAtt": ["MissingPageFunction", "FunctionMetadata.FunctionARN"]}},
        {
            "EventType": "viewer-response",
            "FunctionARN": {"Fn::GetAtt": ["MissingPageStatusFunction", "FunctionMetadata.FunctionARN"]},
        },
    ]
    for behavior in DISTRIBUTION["CacheBehaviors"]:
        named = json.dumps(behavior.get("FunctionAssociations", []))
        assert "MissingPage" not in named, behavior["PathPattern"]


def test_no_custom_error_response_answers_for_the_functions() -> None:
    """The reason these are functions: an error response would replace the API's problems too."""
    assert "CustomErrorResponses" not in DISTRIBUTION


def test_the_mark_is_a_header_a_function_may_add_and_the_bucket_never_sees() -> None:
    assert MARK not in EDGE_RULES.FUNCTION_DISALLOWED_HEADERS
    assert not MARK.startswith(EDGE_RULES.FUNCTION_DISALLOWED_PREFIXES)
    assert MARK not in EDGE_RULES.FUNCTION_READ_ONLY_IN_VIEWER_REQUEST
    assert not MARK.startswith("cloudfront-")
    policy = RESOURCES[DEFAULT["CachePolicyId"]["Ref"]]["Properties"]["CachePolicyConfig"]
    assert policy["ParametersInCacheKeyAndForwardedToOrigin"]["HeadersConfig"] == {"HeaderBehavior": "none"}
    assert "OriginRequestPolicyId" not in DEFAULT


# --- the code, run -------------------------------------------------------------------------

# Addresses the default behavior can receive, by what the edge must do with them.
# A page address that names no page: the root-level and deep, .html and bare,
# a trailing slash on a real page, a case that is not the key's, an alias that
# is not one (/connect), and a dot inside a directory rather than the name.
MISSING = [
    "/nope",
    "/nope.html",
    "/NOPE.HTML",
    "/Index.html",
    "/app/",
    "/app/nothing",
    "/index.html/",
    "/connect.html/",
    "/connect",
    "/docs/guide",
    "/a.b/c",
    "/name.",
    "/acme-probe-0000-no-such-page",
]
# Paths with a file type other than .html, which keep the bucket's own answer.
OTHER_FILES = [
    "/favicon.ico",
    "/robots.txt",
    "/sitemap.xml",
    "/docs/x.md",
    "/nope.js",
    "/assets/nope.css",
    "/index.html.bak",
    "/.env",
]
# The bare forms of API prefixes, and other stages, which no API pattern takes:
# they fall through to the pages, and are addresses with no page there.
FALLS_THROUGH = [
    "/api",
    "/sessions",
    "/hooks",
    "/dist",
    "/adapter",
    "/prod",
    "/dev/evaluate-tool-call",
    "/v1/evaluate-tool-call",
]
QUERY = {"utm_source": {"value": "acme"}, "q": {"value": "a", "multiValue": [{"value": "a"}, {"value": "b"}]}}
VIEWER_HOST = {"host": {"value": "d1acme0edge.cloudfront.net"}}


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on this machine; the page list is still checked above")
    return node


def run(code: str, events: List[dict]) -> List[dict]:
    """The function's handler over every event, in one node process, the events on stdin.

    On stdin rather than the command line, whose length Windows caps.
    """
    script = code + (
        "\nlet input = '';"
        "\nprocess.stdin.on('data', (chunk) => { input += chunk; });"
        "\nprocess.stdin.on('end', () => {"
        "\n    process.stdout.write(JSON.stringify(JSON.parse(input).map((event) => handler(event))));"
        "\n});\n"
    )
    result = subprocess.run(
        [_node(), "-e", script], input=json.dumps(events), capture_output=True, text=True, timeout=60, check=True
    )
    return json.loads(result.stdout)


def viewer_request(uri: str, querystring: Optional[dict] = None, headers: Optional[dict] = None) -> dict:
    request = {
        "method": "GET",
        "uri": uri,
        "querystring": querystring if querystring is not None else {},
        "headers": headers if headers is not None else dict(VIEWER_HOST),
        "cookies": {},
    }
    return {"version": "1.0", "context": {"eventType": "viewer-request"}, "request": request}


def viewer_response(request: dict, status: int) -> dict:
    response = {
        "statusCode": status,
        "statusDescription": {200: "OK", 206: "Partial Content", 304: "Not Modified"}[status],
        "headers": {"content-type": {"value": "text/html; charset=utf-8"}, "etag": {"value": '"acme0etag"'}},
        "cookies": {},
    }
    return {"version": "1.0", "context": {"eventType": "viewer-response"}, "request": request, "response": response}


REQUEST_CASES: List[Tuple[str, dict]] = (
    [(f"page {uri}", viewer_request(uri)) for uri in PAGES]
    + [(f"missing {uri}", viewer_request(uri)) for uri in MISSING + FALLS_THROUGH]
    + [(f"other file {uri}", viewer_request(uri)) for uri in OTHER_FILES]
    + [
        ("page with a query", viewer_request("/index.html", QUERY)),
        ("missing with a query", viewer_request("/nope", QUERY)),
        ("page with a mark the viewer sent", viewer_request("/index.html", headers={**VIEWER_HOST, MARK: {"value": "1"}})),
        ("missing with a mark the viewer sent", viewer_request("/nope", headers={**VIEWER_HOST, MARK: {"value": "x"}})),
    ]
)


@functools.lru_cache(maxsize=None)
def rewritten() -> Dict[str, dict]:
    """Each request case, as MissingPageFunction hands it on."""
    outputs = run(REQUEST_CODE, [event for _, event in REQUEST_CASES])
    return {label: output for (label, _), output in zip(REQUEST_CASES, outputs)}


def _is_marked(request: dict) -> bool:
    return request["headers"].get(MARK) == {"value": "1"}


@pytest.mark.parametrize("path", PAGES)
def test_a_published_page_and_its_aliases_pass_untouched(path: str) -> None:
    out = rewritten()[f"page {path}"]
    assert out == viewer_request(path)["request"], "a page is asked for as it was sent"


@pytest.mark.parametrize("path", MISSING + FALLS_THROUGH)
def test_a_page_address_with_no_page_is_sent_to_the_not_found_page_and_marked(path: str) -> None:
    out = rewritten()[f"missing {path}"]
    assert out["uri"] == NOT_FOUND_PAGE
    assert _is_marked(out)
    assert out["method"] == "GET" and out["headers"]["host"] == VIEWER_HOST["host"], "nothing else is changed"


@pytest.mark.parametrize("path", OTHER_FILES)
def test_a_file_of_another_type_is_left_to_the_bucket(path: str) -> None:
    """A missing script or image keeps S3's own 404; the page would only mislead a browser loading it."""
    assert rewritten()[f"other file {path}"] == viewer_request(path)["request"]


def test_the_query_string_is_kept_and_decides_nothing() -> None:
    page = rewritten()["page with a query"]
    assert page["uri"] == "/index.html" and page["querystring"] == QUERY and not _is_marked(page)
    missing = rewritten()["missing with a query"]
    assert missing["uri"] == NOT_FOUND_PAGE and missing["querystring"] == QUERY and _is_marked(missing)


def test_a_mark_the_viewer_sent_is_removed() -> None:
    page = rewritten()["page with a mark the viewer sent"]
    assert page["uri"] == "/index.html" and MARK not in page["headers"]
    missing = rewritten()["missing with a mark the viewer sent"]
    assert missing["uri"] == NOT_FOUND_PAGE and missing["headers"][MARK] == {"value": "1"}, "only the function's own"


@pytest.mark.parametrize("path", FALLS_THROUGH)
def test_the_bare_api_prefixes_do_fall_through_to_the_pages(path: str) -> None:
    """These are the only API-looking addresses the functions ever see."""
    assert ROUTES.origin_for(path) == "web"


def test_no_api_route_ever_reaches_the_functions() -> None:
    """Every route the function answers, staged or not, is taken by an API behavior first."""
    for path in ROUTES.API_PATHS + ROUTES.STAGED_PATHS:
        assert ROUTES.origin_for(path) != "web", path
    assert len(ROUTES.API_PATHS) > 20, "the route collection has gone blind"


# --- the answer the viewer gets --------------------------------------------------------------


def _bucket_status(uri: str) -> int:
    """What the bucket answers for a request that reaches it: 200 for a published key, else 404."""
    key = DISTRIBUTION["DefaultRootObject"] if uri == "/" else uri.lstrip("/")
    return 200 if key in published_keys() else 404


# The viewer response is handed either the request as MissingPageFunction left it,
# or the request as the viewer sent it; the guide leaves it open, so both are run.
SEMANTICS = ("rewritten", "as sent")


@functools.lru_cache(maxsize=None)
def answers() -> Dict[Tuple[str, str], Tuple[int, str]]:
    """(label, semantics) -> (status the viewer gets, the object served) for every request case."""
    pending: List[Tuple[Tuple[str, str], dict]] = []
    final: Dict[Tuple[str, str], Tuple[int, str]] = {}
    for label, event in REQUEST_CASES:
        forwarded = rewritten()[label]
        status = _bucket_status(forwarded["uri"])
        for semantics in SEMANTICS:
            if status != 200:
                # A 400 or above from the origin does not run a viewer response function.
                final[(label, semantics)] = (status, forwarded["uri"])
                continue
            seen = forwarded if semantics == "rewritten" else event["request"]
            pending.append(((label, semantics), viewer_response(seen, 200)))
    outputs = run(RESPONSE_CODE, [event for _, event in pending])
    for (key, _), response in zip(pending, outputs):
        final[key] = (response["statusCode"], rewritten()[key[0]]["uri"])
    return final


@pytest.mark.parametrize("semantics", SEMANTICS)
@pytest.mark.parametrize("path", PAGES)
def test_a_page_answers_200_with_itself(path: str, semantics: str) -> None:
    assert answers()[(f"page {path}", semantics)] == (200, path)


@pytest.mark.parametrize("semantics", SEMANTICS)
@pytest.mark.parametrize("path", MISSING + FALLS_THROUGH)
def test_an_address_with_no_page_answers_404_with_the_not_found_page(path: str, semantics: str) -> None:
    assert answers()[(f"missing {path}", semantics)] == (404, NOT_FOUND_PAGE)


@pytest.mark.parametrize("semantics", SEMANTICS)
@pytest.mark.parametrize("path", OTHER_FILES)
def test_a_missing_file_of_another_type_answers_the_bucket_s_own_404(path: str, semantics: str) -> None:
    assert answers()[(f"other file {path}", semantics)] == (404, path)


@pytest.mark.parametrize("semantics", SEMANTICS)
def test_a_query_string_changes_no_answer(semantics: str) -> None:
    assert answers()[("page with a query", semantics)] == (200, "/index.html")
    assert answers()[("missing with a query", semantics)] == (404, NOT_FOUND_PAGE)


def test_a_mark_the_viewer_sent_does_not_turn_a_page_into_a_404() -> None:
    """Removed before the request goes on. Were the response function handed the
    request as sent, the mark could change only that viewer's own answer, since
    the status is set after the cache; edge.yml says so beside the functions."""
    assert answers()[("page with a mark the viewer sent", "rewritten")] == (200, "/index.html")
    assert answers()[("missing with a mark the viewer sent", "rewritten")] == (404, NOT_FOUND_PAGE)


def test_the_status_function_changes_only_a_200_and_only_the_status() -> None:
    marked = rewritten()["missing /nope"]
    cases = [
        viewer_response(marked, 200),
        viewer_response(marked, 304),
        viewer_response(marked, 206),
        viewer_response(viewer_request("/index.html")["request"], 200),
        viewer_response(viewer_request(NOT_FOUND_PAGE)["request"], 200),
    ]
    out = run(RESPONSE_CODE, cases)
    assert (out[0]["statusCode"], out[0]["statusDescription"]) == (404, "Not Found")
    assert out[0]["headers"] == cases[0]["response"]["headers"] and "body" not in out[0], "the page's own body and type"
    assert [o["statusCode"] for o in out[1:]] == [304, 206, 200, 200], (
        "a revalidation, a range, a page, and the not-found page asked for by name keep their status"
    )
