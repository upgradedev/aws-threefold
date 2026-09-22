"""The development server signs out and hands out the bundle as the deployment does.

The standard library decides which methods exist before the handler is
reached, so a DELETE was a 501 locally while the deployment signed the page
out. A download comes back from the handler as base64 with isBase64Encoded set,
which API Gateway decodes; the local server has to decode it too, or the
installer would receive base64 text where it expects a zip.

The server is bound to a loopback port the operating system chooses and shut
down in the same test. Nothing leaves the machine.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import threading
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from threefold.infrastructure import auth_store
from threefold.infrastructure.auth_store import AuthStore
from threefold.interfaces import access_routes
from threefold.interfaces.server import ThreefoldHTTPRequestHandler

OPERATOR_KEY = "acme-synthetic-operator-key-0004"


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ThreefoldHTTPRequestHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[0], httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def package(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "threefold"
    for name, text in {
        "__init__.py": "",
        "domain/__init__.py": "",
        "hooks/threefold_hook.py": "# synthetic hook\n",
        "tools/threefold_cli.py": "# synthetic cli\n",
        "tools/threefold_install.py": 'ENDPOINT = "__THREEFOLD_ENDPOINT__"\n',
    }.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(text.encode("utf-8"))
    monkeypatch.setattr(access_routes, "PACKAGE_ROOT", str(root))
    monkeypatch.setattr(access_routes, "HOOKS_ROOT", str(root / "hooks"))
    monkeypatch.setattr(access_routes, "TOOLS_ROOT", str(root / "tools"))
    access_routes._BUNDLE_CACHE.clear()
    yield root
    access_routes._BUNDLE_CACHE.clear()


@pytest.fixture
def signed_in_store(monkeypatch):
    monkeypatch.setenv("THREEFOLD_API_KEYS", OPERATOR_KEY)
    auth_store.reset_default_store(AuthStore())
    yield auth_store.default_store()
    auth_store.reset_default_store()


def _request(address, method: str, path: str, body: dict | None = None, headers: dict | None = None):
    connection = http.client.HTTPConnection(address[0], address[1], timeout=5)
    try:
        payload = json.dumps(body) if body is not None else None
        connection.request(method, path, body=payload, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


def test_signing_out_reaches_the_handler(server, signed_in_store) -> None:
    status, _, raw = _request(server, "POST", "/api/auth/links", {}, {"X-API-Key": OPERATOR_KEY})
    assert status == 200
    link = json.loads(raw)
    assert link["url"].startswith(f"http://{server[0]}:{server[1]}/dashboard.html#/signin?code=")

    status, _, raw = _request(server, "POST", "/api/auth/sessions", {"code": link["code"]})
    token = json.loads(raw)["token"]
    bearer = {"Authorization": f"Bearer {token}"}
    assert json.loads(_request(server, "GET", "/api/auth/whoami", headers=bearer)[2])["via"] == "session"

    status, _, raw = _request(server, "DELETE", "/api/auth/sessions", headers=bearer)
    assert status == 200, "The standard library answered 501 before the handler was reached"
    assert json.loads(raw)["authenticated"] is False
    assert json.loads(_request(server, "GET", "/api/auth/whoami", headers=bearer)[2])["authenticated"] is False


def _listed(value: str) -> set:
    return {item.strip() for item in value.split(",") if item.strip()}


def test_the_preflight_allows_signing_out_with_a_bearer(server) -> None:
    status, headers, _ = _request(server, "OPTIONS", "/api/auth/sessions")
    assert status == 200
    assert "DELETE" in _listed(headers["Access-Control-Allow-Methods"])
    assert {"Authorization", "X-API-Key"} <= _listed(headers["Access-Control-Allow-Headers"]), (
        "the key travels in either header, and the preflight has to allow both"
    )


def test_the_local_preflight_allows_what_the_deployed_function_does(server) -> None:
    """Two lists that drift apart make a page work locally and fail its preflight when deployed."""
    from threefold.interfaces.api_handlers import CORS_HEADERS

    _, headers, _ = _request(server, "OPTIONS", "/api/projects/Acme-Billing/reviews")
    for name in ("Access-Control-Allow-Methods", "Access-Control-Allow-Headers"):
        assert _listed(headers[name]) == _listed(CORS_HEADERS[name]), name
    assert headers["Access-Control-Allow-Origin"] == CORS_HEADERS["Access-Control-Allow-Origin"]


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/status"),
        ("OPTIONS", "/api/auth/sessions"),
        ("DELETE", "/api/auth/sessions"),
        ("GET", "/no-such-route"),
        ("POST", "/policy/config"),
        ("HEAD", "/status"),
    ],
)
def test_each_cors_header_is_sent_once(server, method: str, path: str) -> None:
    """Sent twice, a header reaches the browser as a list, and "*, *" is no origin at all."""
    _, headers, _ = _request(server, method, path, {} if method == "POST" else None)
    for name in ("Access-Control-Allow-Origin", "Access-Control-Allow-Methods", "Access-Control-Allow-Headers"):
        assert len(headers.get_all(name) or []) == 1, f"{method} {path}: {name} was not sent exactly once"


def test_the_bundle_arrives_as_a_zip_not_as_base64(server, package) -> None:
    status, headers, raw = _request(server, "GET", "/dist/threefold-bundle.zip")
    assert status == 200
    assert headers["Content-Type"] == "application/zip"
    assert raw[:2] == b"PK"
    assert headers[access_routes.SHA256_HEADER] == hashlib.sha256(raw).hexdigest()
    assert "bin/threefold_hook.py" in zipfile.ZipFile(io.BytesIO(raw)).namelist()


def test_the_installer_points_at_the_local_server(server, package) -> None:
    status, headers, raw = _request(server, "GET", "/install.py")
    assert status == 200
    assert raw.decode("utf-8") == f'ENDPOINT = "http://{server[0]}:{server[1]}/"\n'
    assert headers[access_routes.SHA256_HEADER] == hashlib.sha256(raw).hexdigest()
