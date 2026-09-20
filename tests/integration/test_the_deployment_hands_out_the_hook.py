"""A product nobody can obtain is a demo.

The install page used to tell a reader to clone a repository that is not
published, and the Lambda package contained `src/` only, so the one artifact
that puts Threefold in front of a real agent could not be had by anyone. The
script now lives inside the packaged tree and the service hands it out.
"""
from __future__ import annotations

import os

from threefold.interfaces.api_handlers import HOOKS_ROOT, lambda_handler


def _get(path: str, stage: str = "prod") -> dict:
    return lambda_handler(
        {
            "rawPath": f"/{stage}{path}",
            "headers": {},
            "requestContext": {"http": {"method": "GET"}, "stage": stage},
        }
    )


def test_the_hook_is_served() -> None:
    response = _get("/hooks/claude_code_hook.py")
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith("text/plain")
    assert response["body"].startswith("#!/usr/bin/env python3")


def test_the_short_path_serves_the_same_script() -> None:
    """curl -O keeps the last path segment, so both spellings must agree."""
    assert _get("/claude_code_hook.py")["body"] == _get("/hooks/claude_code_hook.py")["body"]


def test_what_is_served_is_what_is_in_the_tree() -> None:
    """A served copy that drifts from the file under test is worse than none."""
    with open(os.path.join(HOOKS_ROOT, "claude_code_hook.py"), "r", encoding="utf-8") as handle:
        assert _get("/hooks/claude_code_hook.py")["body"] == handle.read()


def test_what_is_served_is_runnable_python() -> None:
    """A reader pipes this into an interpreter. It has to compile."""
    compile(_get("/hooks/claude_code_hook.py")["body"], "claude_code_hook.py", "exec")


def test_the_hook_ships_inside_the_deployment_package() -> None:
    """The regression that caused this: docs/ and hooks/ are outside CodeUri."""
    assert os.path.isfile(os.path.join(HOOKS_ROOT, "claude_code_hook.py"))
    src_root = os.path.dirname(os.path.dirname(HOOKS_ROOT))
    assert os.path.basename(src_root) == "src", "The hook must sit under the CodeUri the template packages"


def test_the_hook_is_reachable_without_a_key() -> None:
    """Putting the one artifact that matters behind a key nobody has is no use."""
    from threefold.infrastructure.security_middleware import validate_request_security

    # The install page belongs here too: a dashboard whose every link answers 401
    # is not a zero-setup visitor path.
    for path in (
        "/hooks/claude_code_hook.py",
        "/claude_code_hook.py",
        "/connect.html",
        "/index.html",
        "/sessions.html",
        "/settings.html",
        "/swagger.html",
        "/testbook.html",
    ):
        os.environ["ENFORCE_API_KEY"] = "true"
        try:
            allowed, problem = validate_request_security(headers={}, client_ip="203.0.113.7", path=path)
        finally:
            os.environ["ENFORCE_API_KEY"] = "false"
        assert allowed, f"{path} was refused with {problem}"


def test_the_install_page_offers_the_script_rather_than_a_placeholder() -> None:
    """connect.html told the reader to clone `<repository>`, which does not exist."""
    page = _get("/connect.html")["body"]
    assert "hooks/claude_code_hook.py" in page, "The page must link the script it documents"
    assert "git clone &lt;repository&gt;" not in page and "git clone <repository>" not in page
