"""A product nobody can obtain is a demo.

The install page used to tell a reader to clone a repository that is not
published, and the Lambda package contained `src/` only, so the one artifact
that puts Threefold in front of a real agent could not be had by anyone. The
script now lives inside the packaged tree and the service hands it out.

The hook was renamed when it began serving every agent rather than Claude Code
alone. The file name is read from SERVED_SCRIPTS rather than repeated here, so
the tests follow the route table instead of a spelling. The tests that read the
real file skip while it is absent, which it is on a branch that carries the
service change without the hook itself; the mechanism is tested either way,
against a stand-in script.
"""
from __future__ import annotations

import os

import pytest

from threefold.interfaces import api_handlers
from threefold.interfaces.api_handlers import HOOKS_ROOT, SERVED_SCRIPTS, lambda_handler

CANONICAL_PATH = "/hooks/threefold_hook.py"
LEGACY_PATHS = ("/hooks/claude_code_hook.py", "/claude_code_hook.py")
HOOK_FILE = SERVED_SCRIPTS[CANONICAL_PATH]

needs_the_hook = pytest.mark.skipif(
    not os.path.isfile(os.path.join(HOOKS_ROOT, HOOK_FILE)),
    reason=(
        f"src/threefold/hooks/{HOOK_FILE} is not in this tree yet; it arrives with the hook "
        "change, and these tests read the real file"
    ),
)


def _get(path: str, stage: str = "prod", method: str = "GET") -> dict:
    return lambda_handler(
        {
            "rawPath": f"/{stage}{path}",
            "headers": {},
            "requestContext": {"http": {"method": method}, "stage": stage},
        }
    )


def test_every_hook_path_serves_the_one_script() -> None:
    """The old name is kept so an install command copied before the rename still works."""
    assert set(SERVED_SCRIPTS) == {CANONICAL_PATH, *LEGACY_PATHS}
    assert set(SERVED_SCRIPTS.values()) == {"threefold_hook.py"}


def test_each_path_hands_out_the_file_it_names(tmp_path, monkeypatch) -> None:
    """Exercised against a stand-in, so the route is tested even where the hook is absent."""
    stand_in = "#!/usr/bin/env python3\n# Acme-Hooks stand-in\nprint('acme')\n"
    (tmp_path / HOOK_FILE).write_text(stand_in, encoding="utf-8")
    monkeypatch.setattr(api_handlers, "HOOKS_ROOT", str(tmp_path))

    for path in SERVED_SCRIPTS:
        response = _get(path)
        assert response["statusCode"] == 200, path
        assert response["headers"]["Content-Type"].startswith("text/plain")
        assert f'filename="{HOOK_FILE}"' in response["headers"]["Content-Disposition"]
        assert response["body"] == stand_in, f"{path} served something other than {HOOK_FILE}"


def test_a_missing_hook_is_reported_rather_than_served_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api_handlers, "HOOKS_ROOT", str(tmp_path))
    response = _get(CANONICAL_PATH)
    assert response["statusCode"] == 500
    assert "script-missing" in response["body"]


@needs_the_hook
def test_the_hook_is_served() -> None:
    response = _get(CANONICAL_PATH)
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith("text/plain")
    assert response["body"].startswith("#!/usr/bin/env python3")


@needs_the_hook
def test_the_old_paths_serve_the_same_script() -> None:
    """curl -O keeps the last path segment, so every spelling must agree."""
    canonical = _get(CANONICAL_PATH)["body"]
    for path in LEGACY_PATHS:
        assert _get(path)["body"] == canonical, path


@needs_the_hook
def test_what_is_served_is_what_is_in_the_tree() -> None:
    """A served copy that drifts from the file under test is worse than none."""
    with open(os.path.join(HOOKS_ROOT, HOOK_FILE), "r", encoding="utf-8") as handle:
        assert _get(CANONICAL_PATH)["body"] == handle.read()


@needs_the_hook
def test_what_is_served_is_runnable_python() -> None:
    """A reader pipes this into an interpreter. It has to compile."""
    compile(_get(CANONICAL_PATH)["body"], HOOK_FILE, "exec")


@needs_the_hook
def test_the_hook_ships_inside_the_deployment_package() -> None:
    """The regression that caused this: docs/ and hooks/ are outside CodeUri."""
    assert os.path.isfile(os.path.join(HOOKS_ROOT, HOOK_FILE))
    src_root = os.path.dirname(os.path.dirname(HOOKS_ROOT))
    assert os.path.basename(src_root) == "src", "The hook must sit under the CodeUri the template packages"


def test_the_hook_is_reachable_without_a_key(monkeypatch) -> None:
    """Putting the one artifact that matters behind a key nobody has is no use."""
    from threefold.infrastructure.security_middleware import validate_request_security

    # The install page belongs here too: a dashboard whose every link answers 401
    # is not a zero-setup visitor path. So does a stack with private reads,
    # which keeps its data closed and its pages and hook open.
    monkeypatch.setenv("ENFORCE_API_KEY", "true")
    monkeypatch.setenv("PUBLIC_READS", "false")
    for path in (
        *SERVED_SCRIPTS,
        "/connect.html",
        "/console.html",
        "/index.html",
        "/sessions.html",
        "/settings.html",
        "/swagger.html",
    ):
        allowed, problem = validate_request_security(headers={}, client_ip="203.0.113.7", path=path)
        assert allowed, f"{path} was refused with {problem}"


def test_the_install_page_offers_the_script_rather_than_a_placeholder() -> None:
    """connect.html told the reader to clone `<repository>`, which does not exist.

    Either served spelling counts: the page may link the new name or the old
    one, and both answer with the same file.
    """
    page = _get("/connect.html")["body"]
    linked = [path for path in SERVED_SCRIPTS if path.startswith("/hooks/") and path.lstrip("/") in page]
    assert linked, "The page must link the script it documents"
    assert "git clone &lt;repository&gt;" not in page and "git clone <repository>" not in page
