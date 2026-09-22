"""The published contract documents sign-in and the downloads as they behave.

Swagger UI's Try it out sends what the document says, so a route documented
without its credential, or with the wrong one, is a button that can only fail.
"""
from __future__ import annotations

import json

from threefold.interfaces import access_routes
from threefold.interfaces.api_handlers import lambda_handler


def _spec() -> dict:
    response = lambda_handler(
        {"rawPath": "/prod/openapi.json", "headers": {}, "requestContext": {"http": {"method": "GET"}, "stage": "prod"}}
    )
    return json.loads(response["body"])


def test_every_access_route_is_documented() -> None:
    paths = _spec()["paths"]
    for path, methods in {
        "/api/auth/links": {"post"},
        "/api/auth/sessions": {"post", "delete"},
        "/api/auth/whoami": {"get"},
        access_routes.INSTALLER_PATH: {"get"},
        access_routes.BUNDLE_PATH: {"get"},
        access_routes.MANIFEST_PATH: {"get"},
    }.items():
        assert path in paths, f"{path} is served but undocumented"
        assert methods <= set(paths[path]), f"{path} documents {sorted(paths[path])}, not {sorted(methods)}"


def test_minting_a_link_is_documented_behind_the_key_alone() -> None:
    spec = _spec()
    links = spec["paths"]["/api/auth/links"]["post"]
    assert links["security"] == [{"OperatorApiKey": []}], "A session cannot mint a link"
    assert "session-not-enough" in links["responses"]["403"]["description"]
    session = spec["components"]["securitySchemes"]["SignInSession"]
    assert (session["type"], session["scheme"]) == ("http", "bearer")


def test_the_documented_fields_are_the_contracts() -> None:
    paths = _spec()["paths"]
    link = paths["/api/auth/links"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert set(link["required"]) == {"code", "expires_in", "url"}
    session = paths["/api/auth/sessions"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert set(session["required"]) == {"token", "expires_at", "ttl_seconds"}
    whoami = paths["/api/auth/whoami"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert set(whoami["required"]) == {"authenticated", "via", "expires_at", "reads_public", "sandbox_writes"}
    manifest = paths["/dist/manifest.json"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert set(manifest["required"]) == {"files", "bundle_sha256", "installer_sha256"}
    assert set(manifest["properties"]["files"]["items"]["required"]) == {"path", "sha256", "bytes"}


def test_the_documented_hash_header_is_the_one_sent() -> None:
    paths = _spec()["paths"]
    for path in (access_routes.INSTALLER_PATH, access_routes.BUNDLE_PATH):
        assert access_routes.SHA256_HEADER in paths[path]["get"]["responses"]["200"]["headers"]
