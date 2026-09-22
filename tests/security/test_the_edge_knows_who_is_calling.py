"""Behind the edge, the function counts each viewer and points back at the edge.

Through CloudFront the function sees a CloudFront server as the caller and the
API's own host name as the host. The edge adds a secret origin header, the
viewer's address arrives in CloudFront-Viewer-Address, and the edge's function
copies the viewer's Host into X-Threefold-Viewer-Host. The function believes
the last two only on a request carrying the secret, because the API's URL stays
public and anyone can send any header to it.

Every secret, key, address and host name here is synthetic, and every request
goes through lambda_handler or the middleware in this process. Nothing leaves
the machine.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest

from threefold.infrastructure import auth_store, security_middleware
from threefold.infrastructure.auth_store import AuthStore
from threefold.infrastructure.security_middleware import (
    TokenBucketRateLimiter,
    header_value,
    rate_limit_bucket,
    rate_limit_key,
    validate_request_security,
    viewer_address,
)
from threefold.interfaces import access_routes
from threefold.interfaces.api_handlers import lambda_handler

EDGE_SECRET = "acme-synthetic-edge-secret-0001-abcdefghijklmnopqrstuvwxyz"
OTHER_SECRET = "acme-synthetic-edge-secret-0002-abcdefghijklmnopqrstuvwxyz"
OPERATOR_KEY = "acme-synthetic-operator-key-0005"
API_DOMAIN = "acme0edge01.execute-api.eu-west-1.amazonaws.com"
EDGE_HOST = "d1acme0edge.cloudfront.net"
# The CloudFront server the function sees as the caller, shared by every viewer
# it serves. Documentation ranges only (RFC 5737 and RFC 3849).
CLOUDFRONT_SERVER = "192.0.2.200"
VIEWER_A = "198.51.100.10"
VIEWER_B = "203.0.113.20"
INSTALLER_SOURCE = 'ENDPOINT = "__THREEFOLD_ENDPOINT__"\n'


@pytest.fixture(autouse=True)
def _no_edge_secret_unless_a_test_sets_one(monkeypatch):
    """A secret in the developer's shell would otherwise decide what these tests see."""
    monkeypatch.delenv(security_middleware.EDGE_SECRET_ENV, raising=False)
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)
    monkeypatch.delenv("STAGE", raising=False)
    monkeypatch.delenv("ENFORCE_API_KEY", raising=False)
    auth_store.reset_default_store(AuthStore())
    yield
    auth_store.reset_default_store()


@pytest.fixture
def edge_secret(monkeypatch) -> str:
    monkeypatch.setenv(security_middleware.EDGE_SECRET_ENV, EDGE_SECRET)
    return EDGE_SECRET


@pytest.fixture
def package(tmp_path: Path, monkeypatch) -> Path:
    """The files the served routes read, in a temporary package tree."""
    root = tmp_path / "threefold"
    for name, text in {
        "__init__.py": "",
        "domain/__init__.py": "",
        "hooks/threefold_hook.py": "# synthetic hook\n",
        "tools/threefold_cli.py": "# synthetic cli\n",
        "tools/threefold_install.py": INSTALLER_SOURCE,
        "web/dashboard.html": '<script>const BASE = "__THREEFOLD_BASE_PATH__";</script>',
    }.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(text.encode("utf-8"))
    monkeypatch.setattr(access_routes, "PACKAGE_ROOT", str(root))
    monkeypatch.setattr(access_routes, "WEB_ROOT", str(root / "web"))
    monkeypatch.setattr(access_routes, "HOOKS_ROOT", str(root / "hooks"))
    monkeypatch.setattr(access_routes, "TOOLS_ROOT", str(root / "tools"))
    access_routes._BUNDLE_CACHE.clear()
    yield root
    access_routes._BUNDLE_CACHE.clear()


def _through_edge(viewer: str = VIEWER_A, secret: str | None = EDGE_SECRET, host: str | None = EDGE_HOST) -> dict:
    """The headers a request carries when CloudFront forwards it, as API Gateway lower-cases them."""
    headers = {"cloudfront-viewer-address": f"{viewer}:46532"}
    if secret is not None:
        headers["x-threefold-edge"] = secret
    if host is not None:
        headers["x-threefold-viewer-host"] = host
    return headers


def _call(method: str, path: str, headers: dict | None = None, body=None, source_ip: str = CLOUDFRONT_SERVER):
    event = {
        "rawPath": f"/prod{path}",
        "headers": headers or {},
        "requestContext": {
            "http": {"method": method, "sourceIp": source_ip},
            "stage": "prod",
            "domainName": API_DOMAIN,
        },
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return lambda_handler(event)


# ------------------------------------------------------------ the viewer's address


@pytest.mark.parametrize(
    "value, expected",
    [
        # IPv4, as CloudFront writes it, and bare.
        ("198.51.100.10:46532", "198.51.100.10"),
        ("198.51.100.10", "198.51.100.10"),
        # IPv6 as CloudFront writes it: no brackets, the port after one more colon.
        ("2001:db8::1:46532", "2001:db8::1"),
        ("2001:db8:85a3:0:0:8a2e:370:7334:60776", "2001:db8:85a3::8a2e:370:7334"),
        # IPv6 in brackets, with and without a port.
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("[2001:db8::1]", "2001:db8::1"),
        # Bare IPv6 that cannot be read as address and port.
        ("2001:db8::1", "2001:db8::1"),
        ("2001:db8::", "2001:db8::"),
        ("::1", "::1"),
        # Two spellings of one address share one bucket.
        ("2001:DB8:0:0:0:0:0:1:443", "2001:db8::1"),
        # An IPv4 viewer reached over IPv6 is counted as the IPv4 address.
        ("::ffff:198.51.100.10:46532", "198.51.100.10"),
        ("  203.0.113.20:1  ", "203.0.113.20"),
    ],
)
def test_the_viewer_address_is_read_in_every_form_it_arrives_in(value: str, expected: str) -> None:
    assert viewer_address(value) == expected


def test_the_port_never_reaches_the_key_so_a_viewer_keeps_one_bucket() -> None:
    """The source port changes with every connection; a key holding it would limit nothing."""
    keys = {viewer_address(f"2001:db8::7:{port}") for port in (1025, 40000, 65535)}
    assert keys == {"2001:db8::7"}
    assert {viewer_address(f"198.51.100.10:{port}") for port in (1025, 40000)} == {"198.51.100.10"}


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "not-an-address",
        "198.51.100.10:99999",
        "198.51.100.10:",
        "198.51.100.10:port",
        "198.51.100.10:８０",
        "[2001:db8::1",
        "[2001:db8::1]x",
        "[2001:db8::1]:",
        "198.51.100.10:1, 203.0.113.20:2",
        "198.51.100.300:1",
        "a" * 80,
    ],
)
def test_anything_else_is_not_an_address(value) -> None:
    assert viewer_address(value) is None


# ---------------------------------------------------------------- the rate-limit key


def test_through_the_edge_the_key_is_the_viewer(edge_secret) -> None:
    assert rate_limit_key(_through_edge(VIEWER_A), CLOUDFRONT_SERVER) == VIEWER_A
    assert rate_limit_key(_through_edge(VIEWER_B), CLOUDFRONT_SERVER) == VIEWER_B


def test_a_spoofed_viewer_address_is_ignored_when_the_stack_has_no_secret() -> None:
    """The API's URL is public: without the proof, the header is whatever the caller chose."""
    assert rate_limit_key(_through_edge(VIEWER_A, secret=None), CLOUDFRONT_SERVER) == CLOUDFRONT_SERVER
    assert rate_limit_key(_through_edge(VIEWER_A, secret=EDGE_SECRET), CLOUDFRONT_SERVER) == CLOUDFRONT_SERVER


def test_a_spoofed_viewer_address_is_ignored_without_the_right_secret(edge_secret) -> None:
    for presented in (None, "", OTHER_SECRET, EDGE_SECRET[:-1], EDGE_SECRET + "x", EDGE_SECRET.upper()):
        headers = _through_edge(VIEWER_A, secret=presented)
        assert rate_limit_key(headers, CLOUDFRONT_SERVER) == CLOUDFRONT_SERVER, presented


def test_a_secret_shorter_than_the_edge_accepts_is_no_secret(monkeypatch) -> None:
    short = "acme-short-secret"
    monkeypatch.setenv(security_middleware.EDGE_SECRET_ENV, short)
    assert rate_limit_key(_through_edge(VIEWER_A, secret=short), CLOUDFRONT_SERVER) == CLOUDFRONT_SERVER


def test_the_secret_read_from_a_file_with_a_newline_still_matches(monkeypatch) -> None:
    """A value pasted from a file keeps its line ending; the edge sends none."""
    monkeypatch.setenv(security_middleware.EDGE_SECRET_ENV, EDGE_SECRET + "\r\n")
    assert rate_limit_key(_through_edge(VIEWER_A), CLOUDFRONT_SERVER) == VIEWER_A


def test_header_names_are_matched_whatever_their_case(edge_secret) -> None:
    headers = {"X-Threefold-Edge": EDGE_SECRET, "CloudFront-Viewer-Address": f"{VIEWER_B}:9"}
    assert rate_limit_key(headers, CLOUDFRONT_SERVER) == VIEWER_B


def test_the_edge_without_a_readable_viewer_address_falls_back_to_the_source(edge_secret) -> None:
    headers = {"x-threefold-edge": EDGE_SECRET, "cloudfront-viewer-address": "not-an-address"}
    assert rate_limit_key(headers, CLOUDFRONT_SERVER) == CLOUDFRONT_SERVER
    assert rate_limit_key({"x-threefold-edge": EDGE_SECRET}, CLOUDFRONT_SERVER) == CLOUDFRONT_SERVER


@pytest.mark.parametrize(
    "address, expected",
    [
        # IPv4, and anything that is not an address, is its own bucket, unchanged.
        ("198.51.100.10", "198.51.100.10"),
        ("127.0.0.1", "127.0.0.1"),
        ("unknown", "unknown"),
        ("", ""),
        # IPv6 is counted by its /64, written as a network so it cannot collide
        # with the key of a single address.
        ("2001:db8::1", "2001:db8::/64"),
        ("2001:db8::7:1025", "2001:db8::/64"),
        ("2001:db8:85a3::8a2e:370:7334", "2001:db8:85a3::/64"),
        ("2001:db8:85a3:1::1", "2001:db8:85a3:1::/64"),
        ("2001:DB8:0:0:ffff:ffff:ffff:ffff", "2001:db8::/64"),
        ("::1", "::/64"),
        # An IPv4 address written as IPv6 counts as the IPv4 address.
        ("::ffff:198.51.100.10", "198.51.100.10"),
    ],
)
def test_the_bucket_is_the_address_or_its_ipv6_prefix(address: str, expected: str) -> None:
    assert rate_limit_bucket(address) == expected


def test_an_ipv6_viewer_keeps_one_bucket_across_the_addresses_of_its_prefix(edge_secret) -> None:
    """Rotating through the addresses of one /64 would otherwise buy a fresh bucket per address."""
    keys = {
        rate_limit_key(_through_edge(viewer), CLOUDFRONT_SERVER)
        for viewer in ("[2001:db8:0:7::1]", "[2001:db8:0:7::2]", "[2001:db8:0:7:ffff:ffff:ffff:ffff]")
    }
    assert keys == {"2001:db8:0:7::/64"}
    assert rate_limit_key(_through_edge("[2001:db8:0:8::1]"), CLOUDFRONT_SERVER) == "2001:db8:0:8::/64"


def test_ipv6_as_cloudfront_writes_it_reaches_the_prefix_bucket(edge_secret) -> None:
    headers = {"x-threefold-edge": EDGE_SECRET, "cloudfront-viewer-address": "2001:db8:85a3:0:0:8a2e:370:7334:60776"}
    assert rate_limit_key(headers, CLOUDFRONT_SERVER) == "2001:db8:85a3::/64"


def test_an_ipv6_source_address_is_counted_by_its_prefix_too() -> None:
    """Without the edge's proof the source address is used, and the same rule applies to it."""
    assert rate_limit_key({}, "2001:db8:0:9::1") == rate_limit_key({}, "2001:db8:0:9::2") == "2001:db8:0:9::/64"
    assert rate_limit_key({}, VIEWER_A) == VIEWER_A


def test_rotating_addresses_inside_one_ipv6_prefix_is_still_limited(edge_secret) -> None:
    limiter = TokenBucketRateLimiter(refill_rate_per_sec=0.0, max_tokens=60.0)
    results = [
        validate_request_security(
            _through_edge(f"[2001:db8:0:7::{i:x}]"), CLOUDFRONT_SERVER, "/status", rate_limiter=limiter
        )
        for i in range(1, 62)
    ]
    assert results[-1][0] is False
    assert results[-1][1]["status"] == 429
    allowed, _ = validate_request_security(
        _through_edge("[2001:db8:0:8::1]"), CLOUDFRONT_SERVER, "/status", rate_limiter=limiter
    )
    assert allowed, "the next prefix over is another viewer"


def test_a_header_under_two_spellings_with_different_values_is_no_header() -> None:
    """Which spelling a dict yields first is not something a caller should decide."""
    assert header_value({"X-Threefold-Edge": "a", "x-threefold-edge": "b"}, "X-Threefold-Edge") is None
    assert header_value({"x-threefold-edge": "b", "X-Threefold-Edge": "a"}, "X-Threefold-Edge") is None
    assert header_value({"X-Threefold-Edge": "a", "x-threefold-edge": "a"}, "X-Threefold-Edge") == "a"
    assert header_value({"x-threefold-edge": "a"}, "X-THREEFOLD-EDGE") == "a"
    assert header_value({}, "X-Threefold-Edge") is None


@pytest.mark.parametrize("order", ["secret first", "wrong value first"])
def test_an_extra_copy_of_the_secret_header_withdraws_the_proof_whatever_its_order(edge_secret, order) -> None:
    pairs = [("x-threefold-edge", EDGE_SECRET), ("X-Threefold-Edge", "acme-wrong")]
    if order == "wrong value first":
        pairs.reverse()
    headers = {**dict(pairs), "cloudfront-viewer-address": f"{VIEWER_A}:1"}
    assert rate_limit_key(headers, CLOUDFRONT_SERVER) == CLOUDFRONT_SERVER


def test_the_same_secret_under_two_spellings_is_still_the_proof(edge_secret) -> None:
    headers = {"x-threefold-edge": EDGE_SECRET, "X-Threefold-Edge": EDGE_SECRET, "cloudfront-viewer-address": f"{VIEWER_A}:1"}
    assert rate_limit_key(headers, CLOUDFRONT_SERVER) == VIEWER_A


def test_two_different_viewer_addresses_leave_the_source_as_the_key(edge_secret) -> None:
    headers = {
        "x-threefold-edge": EDGE_SECRET,
        "cloudfront-viewer-address": f"{VIEWER_A}:1",
        "CloudFront-Viewer-Address": f"{VIEWER_B}:1",
    }
    assert rate_limit_key(headers, CLOUDFRONT_SERVER) == CLOUDFRONT_SERVER


def _exhaust(limiter: TokenBucketRateLimiter, headers: dict, calls: int = 61) -> list[bool]:
    return [
        validate_request_security(headers, CLOUDFRONT_SERVER, "/status", rate_limiter=limiter)[0]
        for _ in range(calls)
    ]


def test_one_busy_viewer_no_longer_empties_the_bucket_for_everyone_behind_the_edge(edge_secret) -> None:
    limiter = TokenBucketRateLimiter(refill_rate_per_sec=0.0, max_tokens=60.0)
    assert _exhaust(limiter, _through_edge(VIEWER_A))[-1] is False, "the busy viewer is limited"
    allowed, problem = validate_request_security(_through_edge(VIEWER_B), CLOUDFRONT_SERVER, "/status", rate_limiter=limiter)
    assert allowed and problem is None, "another viewer on the same edge server is not"


def test_without_the_secret_rotating_the_header_buys_no_fresh_buckets() -> None:
    limiter = TokenBucketRateLimiter(refill_rate_per_sec=0.0, max_tokens=60.0)
    results = [
        validate_request_security(
            {"cloudfront-viewer-address": f"198.51.100.{i % 250}:1"}, CLOUDFRONT_SERVER, "/status", rate_limiter=limiter
        )
        for i in range(61)
    ]
    assert results[-1][0] is False
    assert results[-1][1]["status"] == 429


def test_the_handler_counts_each_viewer_behind_one_edge_server(edge_secret) -> None:
    statuses = [_call("GET", "/status", _through_edge(VIEWER_A))["statusCode"] for _ in range(70)]
    assert 429 in statuses
    assert _call("GET", "/status", _through_edge(VIEWER_B))["statusCode"] == 200


def test_the_secret_is_never_logged_or_returned(edge_secret, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    bodies = []
    for headers in (_through_edge(VIEWER_A), _through_edge(VIEWER_A, secret=OTHER_SECRET)):
        for path in ("/status", "/api/auth/whoami", "/no-such-route"):
            bodies.append(_call("GET", path, headers)["body"])
    for _ in range(70):
        bodies.append(_call("GET", "/status", _through_edge(VIEWER_A))["body"])
    assert any("Too Many Requests" in body for body in bodies), "the limited path was exercised"
    for text in [caplog.text, *bodies]:
        assert EDGE_SECRET not in text
        assert OTHER_SECRET not in text


# ----------------------------------------------------------- the stack's public base


def _installer(headers: dict) -> str:
    response = _call("GET", "/install.py", headers)
    assert response["statusCode"] == 200, response["body"]
    return response["body"]


def test_through_the_edge_the_installer_points_at_the_edge_root(package, edge_secret) -> None:
    assert _installer(_through_edge()) == f'ENDPOINT = "https://{EDGE_HOST}/"\n'


def test_the_installer_fetched_directly_still_points_at_the_api(package, edge_secret) -> None:
    assert _installer({}) == f'ENDPOINT = "https://{API_DOMAIN}/prod/"\n'


def test_a_spoofed_viewer_host_is_ignored_when_the_stack_has_no_secret(package) -> None:
    assert _installer(_through_edge()) == f'ENDPOINT = "https://{API_DOMAIN}/prod/"\n'


def test_a_spoofed_viewer_host_is_ignored_without_the_right_secret(package, edge_secret) -> None:
    for presented in (None, OTHER_SECRET):
        assert _installer(_through_edge(secret=presented)) == f'ENDPOINT = "https://{API_DOMAIN}/prod/"\n'


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "localhost",
        "d1acme0edge.cloudfront.net:443",
        "198.51.100.10",
        "[2001:db8::1]",
        "d1acme0edge.cloudfront.net.",
        "acme..cloudfront.net",
        "-acme.cloudfront.net",
        "acme.cloudfront.net/evil",
        "acme.cloudfront.net\r\nX-Injected: 1",
        'acme.cloudfront.net"; import os; "',
        "acme_edge.cloudfront.net",
        ("a" * 63 + ".") * 4 + "net",
        # The Kelvin sign lower-cases to an ASCII "k": a name that only becomes
        # a host name on the way in is not the one the viewer used.
        "\u212a.example",
        "d1acme0edge.cloudfront.\u212aet",
    ],
)
def test_a_viewer_host_that_is_not_a_plain_host_name_is_ignored(package, edge_secret, host) -> None:
    """It is written into a script and into links, so anything else keeps today's address."""
    assert _installer(_through_edge(host=host)) == f'ENDPOINT = "https://{API_DOMAIN}/prod/"\n'


def test_two_different_viewer_hosts_are_no_viewer_host(package, edge_secret) -> None:
    headers = {**_through_edge(), "X-Threefold-Viewer-Host": "acme-other.example"}
    assert _installer(headers) == f'ENDPOINT = "https://{API_DOMAIN}/prod/"\n'


def test_a_viewer_host_is_written_in_lower_case(package, edge_secret) -> None:
    assert _installer(_through_edge(host="D1Acme0Edge.CloudFront.NET")) == f'ENDPOINT = "https://{EDGE_HOST}/"\n'


def test_the_manifest_fetched_through_the_edge_hashes_the_installer_the_edge_serves(package, edge_secret) -> None:
    through_edge = json.loads(_call("GET", "/dist/manifest.json", _through_edge())["body"])
    direct = json.loads(_call("GET", "/dist/manifest.json", {})["body"])
    assert through_edge["installer_sha256"] == hashlib.sha256(_installer(_through_edge()).encode("utf-8")).hexdigest()
    assert direct["installer_sha256"] == hashlib.sha256(_installer({}).encode("utf-8")).hexdigest()
    assert through_edge["installer_sha256"] != direct["installer_sha256"], "two installers, two hashes"
    assert through_edge["bundle_sha256"] == direct["bundle_sha256"], "one bundle, whichever way it is fetched"


def test_a_sign_in_link_minted_through_the_edge_opens_the_edge(edge_secret) -> None:
    headers = {**_through_edge(), "X-API-Key": OPERATOR_KEY}
    response = _call("POST", "/api/auth/links", headers, body={"next": "/projects/Acme-Billing"})
    assert response["statusCode"] == 200, response["body"]
    url = json.loads(response["body"])["url"]
    assert url.startswith(f"https://{EDGE_HOST}/dashboard.html#/signin?code=")
    assert url.endswith("&next=%2Fprojects%2FAcme-Billing")


def test_a_sign_in_link_minted_directly_still_opens_the_api(edge_secret) -> None:
    response = _call("POST", "/api/auth/links", {"X-API-Key": OPERATOR_KEY}, body={})
    assert json.loads(response["body"])["url"].startswith(f"https://{API_DOMAIN}/prod/dashboard.html#/signin?code=")


def test_a_page_the_function_serves_keeps_its_base_path_through_the_edge(package, edge_secret) -> None:
    """The edge sends /prod/dashboard.html here unprefixed, so the page's API base stays /prod."""
    response = _call("GET", "/dashboard.html", _through_edge())
    assert response["statusCode"] == 200
    assert 'const BASE = "/prod";' in response["body"]
