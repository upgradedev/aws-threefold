"""The OpenAPI link has to serve the contract, not a placeholder.

The spec was read from `docs/`, which sits outside the `CodeUri: ../src` the
deployment package is built from, so every request against the deployed stack
fell through to a two-line stub with no paths in it. A stub is worse than an
error here: it renders in Swagger UI as an API with no operations, which reads
as a product that documents nothing rather than as a deployment that is broken.
"""
from __future__ import annotations

import json

from threefold.interfaces.api_handlers import lambda_handler


def _get(path: str, stage: str = "prod") -> dict:
    return lambda_handler(
        {
            "rawPath": f"/{stage}{path}",
            "headers": {},
            "requestContext": {"http": {"method": "GET"}, "stage": stage},
        }
    )


def test_the_spec_route_answers_the_document_itself() -> None:
    response = _get("/openapi.json")
    assert response["statusCode"] == 200
    spec = json.loads(response["body"])
    assert spec["openapi"].startswith("3.")
    assert spec["paths"], "A spec with no paths is the placeholder, not the contract"


def test_the_spec_documents_the_routes_the_console_is_built_on() -> None:
    """Each page reads one of these. A contract that omits them is not the contract."""
    spec = json.loads(_get("/openapi.json")["body"])
    for route in ("/api/sessions", "/sessions/{session_id}", "/policy/config", "/evaluate-tool-call"):
        assert route in spec["paths"], f"{route} is served but undocumented"


def test_the_docs_alias_answers_the_same_document() -> None:
    assert json.loads(_get("/docs/openapi.json")["body"]) == json.loads(_get("/openapi.json")["body"])


def test_the_spec_ships_inside_the_deployment_package() -> None:
    """The regression that caused this: the file has to be under the packaged root."""
    import os

    from threefold.interfaces.api_handlers import WEB_ROOT

    assert os.path.isfile(os.path.join(WEB_ROOT, "openapi.json"))
    src_root = os.path.dirname(os.path.dirname(WEB_ROOT))
    assert os.path.basename(src_root) == "src", "WEB_ROOT must sit under the CodeUri the template packages"


def test_the_swagger_page_is_told_where_the_spec_is() -> None:
    """The page asked the origin with no stage prefix, which answers 404 on /prod."""
    response = _get("/swagger.html")
    assert response["statusCode"] == 200
    assert "__THREEFOLD_BASE_PATH__" not in response["body"], "The token must be substituted"
    assert 'const SERVED_BASE_PATH = "/prod";' in response["body"]
