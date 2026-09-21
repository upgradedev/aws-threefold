"""The commit-time and CI backstop judges what reaches git by the rules the hook uses.

The hook fails open when the service is unreachable, cannot see an agent it is
not installed in, and cannot see a person. What reaches a commit is judged
again here, and these tests pin the three things that make that judgment the
same one: the staged content rather than the working copy, the project's own
rules from the same places in the same order, and the same rule id in the
refusal that the hook would have printed.

Every repository is made by `git init` in a temporary directory, and the rules
service is a local HTTP server, so nothing here reaches a network.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from threefold.domain.boundary_guard import ArchitecturalBoundaryGuard
from threefold.domain.models import ToolActionType, ToolInvocation

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load("threefold_cli")

CUSTOM_RULE = {
    "id": "acme-core-no-legacy",
    "description": "Core may not import the legacy billing client",
    "when_path_matches": ["**/core/**/*.py"],
    "forbid_imports": ["acme_legacy"],
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _closed_endpoint() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/prod/"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("THREEFOLD_") and name != "THREEFOLD_OFFLINE":
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    global_config = tmp_path / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("THREEFOLD_HOME", str(home / ".threefold"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    for variable, value in (("GIT_AUTHOR_NAME", "Acme Dev"), ("GIT_AUTHOR_EMAIL", "dev@acme.example"),
                            ("GIT_COMMITTER_NAME", "Acme Dev"), ("GIT_COMMITTER_EMAIL", "dev@acme.example")):
        monkeypatch.setenv(variable, value)
    # A project with no endpoint of its own is asked for its rules at the
    # endpoint the hook would use. Home names a port nothing listens on, so no
    # test here can reach the public stack by falling through to the default.
    (home / ".threefold").mkdir()
    (home / ".threefold" / "config.json").write_text(json.dumps({"endpoint": _closed_endpoint()}), encoding="utf-8")
    monkeypatch.setenv("THREEFOLD_TIMEOUT", "0.5")
    root = tmp_path / "acme-ledger"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("# Acme Ledger\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "docs: start")
    return root


def write(root: Path, relative: str, content: str, stage: bool = True) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if stage:
        _git(root, "add", relative)


def commit_rules(root: Path, document: Any, message: str = "chore: acme rules") -> None:
    """The rule file as the check reads it: committed, never from the working tree."""
    write(root, ".threefold/rules.json", json.dumps(document))
    _git(root, "commit", "-q", "-m", message)


def configure(root: Path, **fields: Any) -> None:
    (root / ".threefold.json").write_text(json.dumps(fields), encoding="utf-8")


def check(root: Path, *extra: str) -> SimpleNamespace:
    out = io.StringIO()
    code = cli.main(["check", "--repo", str(root), *extra], out)
    return SimpleNamespace(code=code, out=out.getvalue())


# --- the verdict and the mode --------------------------------------------------------

def test_enforce_mode_refuses_a_staged_violation_and_names_the_rule_the_hook_names(repo) -> None:
    configure(repo, project="Acme-Ledger", mode="enforce")
    write(repo, "src/domain/acme_user.py", "import boto3\n")
    result = check(repo)
    assert result.code == 1
    assert "REFUSED src/domain/acme_user.py [python-domain-stays-pure]" in result.out
    _, hook_reason = ArchitecturalBoundaryGuard.evaluate_tool_boundary(
        ToolInvocation("Write", ToolActionType.FILE_WRITE, {"file_path": "src/domain/acme_user.py", "content": "import boto3\n"})
    )
    assert "Layering rule 'python-domain-stays-pure'" in hook_reason


def test_observe_mode_prints_what_would_be_refused_and_exits_zero(repo) -> None:
    configure(repo, project="Acme-Ledger", mode="observe")
    write(repo, "src/domain/acme_user.py", "import boto3\n")
    result = check(repo)
    assert result.code == 0
    assert "WOULD REFUSE src/domain/acme_user.py [python-domain-stays-pure]" in result.out
    assert "observe mode" in result.out


def test_the_mode_flag_overrides_the_configured_mode(repo) -> None:
    configure(repo, project="Acme-Ledger", mode="observe")
    write(repo, "src/domain/acme_user.py", "import boto3\n")
    assert check(repo, "--mode", "enforce").code == 1


def test_with_no_configuration_the_shipped_rules_enforce(repo) -> None:
    write(repo, "src/domain/acme_user.py", "from boto3 import client\n")
    result = check(repo)
    assert result.code == 1
    assert "the rules Threefold ships" in result.out


def test_a_clean_commit_passes_quietly(repo) -> None:
    write(repo, "src/domain/acme_user.py", "from dataclasses import dataclass\n")
    write(repo, "src/infrastructure/store.py", "import boto3\n")
    result = check(repo)
    assert result.code == 0
    assert "REFUSE" not in result.out


def test_a_rule_that_only_observes_never_fails_a_commit_even_in_enforce_mode(repo) -> None:
    configure(repo, project="Acme-Ledger", mode="enforce")
    commit_rules(repo, [dict(CUSTOM_RULE, mode="observe")])
    write(repo, "src/core/invoice.py", "import acme_legacy\n")
    result = check(repo)
    assert result.code == 0
    assert "WOULD REFUSE src/core/invoice.py [acme-core-no-legacy]" in result.out


# --- what is judged ------------------------------------------------------------------------

def test_the_staged_content_is_judged_not_the_working_copy(repo) -> None:
    write(repo, "src/domain/acme_user.py", "import boto3\n")
    write(repo, "src/domain/acme_user.py", "x = 1\n", stage=False)
    assert check(repo).code == 1, "the commit would contain the import, whatever the working copy says"

    write(repo, "src/domain/acme_order.py", "x = 1\n")
    _git(repo, "reset", "-q", "src/domain/acme_user.py")
    write(repo, "src/domain/acme_order.py", "import boto3\n", stage=False)
    assert check(repo).code == 0, "an unstaged import is not in the commit"


def test_ci_judges_every_file_changed_since_the_base(repo) -> None:
    _git(repo, "checkout", "-q", "-b", "feature/acme")
    write(repo, "src/domain/acme_user.py", "import boto3\n")
    _git(repo, "commit", "-q", "-m", "feat: acme user")
    write(repo, "docs/notes.md", "notes\n")
    _git(repo, "commit", "-q", "-m", "docs: notes")
    out = io.StringIO()
    code = cli.main(["ci", "--base", "main", "--repo", str(repo)], out)
    assert code == 1
    assert "[python-domain-stays-pure]" in out.getvalue()
    assert "2 file(s) changed since main" in out.getvalue()


def test_ci_with_a_base_that_does_not_exist_is_an_error_not_a_pass(repo) -> None:
    out = io.StringIO()
    assert cli.main(["ci", "--base", "acme/no-such-branch", "--repo", str(repo)], out) == 2


# --- where the rules come from --------------------------------------------------------------

def test_the_repositorys_committed_rule_file_is_used_when_no_service_answers(repo) -> None:
    commit_rules(repo, {"rules": [CUSTOM_RULE]})
    write(repo, "src/core/invoice.py", "import acme_legacy\n")
    write(repo, "src/domain/acme_user.py", "import boto3\n")
    result = check(repo)
    assert result.code == 1
    assert "[acme-core-no-legacy]" in result.out
    assert "python-domain-stays-pure" not in result.out, "the project's rules replace the shipped ones"


class RulesService:
    def __init__(self) -> None:
        self.status = 200
        self.body: Any = {"rules": [CUSTOM_RULE], "is_default": False}
        self.requests: List[Dict[str, Any]] = []
        self.port = 0

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/prod/"


@pytest.fixture
def service():
    state = RulesService()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
            state.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
            payload = json.dumps(state.body).encode("utf-8")
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()


def test_the_projects_rules_are_fetched_from_the_configured_endpoint(repo, service, tmp_path) -> None:
    """The key goes with the request because home pairs that endpoint with that
    key file, as the installer writes it; a repository's endpoint alone gets none."""
    key = tmp_path / "acme.key"
    key.write_text("acme-operator-key", encoding="utf-8")
    home_config = tmp_path / "home" / ".threefold" / "config.json"
    document = json.loads(home_config.read_text(encoding="utf-8"))
    document["trusted_endpoints"] = [{"endpoint": service.endpoint, "api_key_file": str(key)}]
    home_config.write_text(json.dumps(document), encoding="utf-8")
    configure(repo, project="Acme-Ledger", mode="enforce", endpoint=service.endpoint, api_key_file=str(key))
    write(repo, "src/core/invoice.py", "import acme_legacy\n")
    result = check(repo)
    assert result.code == 1
    assert "[acme-core-no-legacy]" in result.out
    assert "the service's rules for Acme-Ledger" in result.out
    assert service.requests[0]["path"] == "/prod/rules?project=Acme-Ledger"
    assert service.requests[0]["headers"].get("x-api-key") == "acme-operator-key"


def test_a_service_that_cannot_answer_falls_back_to_the_rule_file_and_says_why(repo, service) -> None:
    service.status = 503
    configure(repo, project="Acme-Ledger", mode="enforce", endpoint=service.endpoint)
    commit_rules(repo, [CUSTOM_RULE])
    write(repo, "src/core/invoice.py", "import acme_legacy\n")
    result = check(repo)
    assert result.code == 1
    assert "could not fetch the rules for Acme-Ledger: the service answered HTTP 503" in result.out
    assert ".threefold/rules.json" in result.out


def test_a_project_with_no_endpoint_of_its_own_is_asked_for_its_rules_at_the_default_one(repo, monkeypatch, tmp_path) -> None:
    """The hook sends such a project's calls to the default endpoint, so its
    rules live there. Reading them anywhere else let the hook and this check
    name different rules for the same write."""
    (tmp_path / "home" / ".threefold" / "config.json").unlink()
    asked: List[Any] = []

    def fetch(endpoint, project, api_key, timeout):
        asked.append((endpoint, project))
        return [CUSTOM_RULE], ""

    monkeypatch.setattr(cli, "fetch_rules", fetch)
    configure(repo, project="Acme-Ledger", mode="enforce")
    write(repo, "src/core/invoice.py", "import acme_legacy\n")
    result = check(repo)
    hook = cli.load_hook()
    assert asked == [(hook.DEFAULT_ENDPOINT, "Acme-Ledger")]
    assert result.code == 1 and "[acme-core-no-legacy]" in result.out


def test_without_a_project_nothing_is_fetched(repo, service) -> None:
    write(repo, "src/domain/acme_user.py", "import boto3\n")
    check(repo)
    assert service.requests == []


# --- the rules are the committed ones, not the change's ----------------------------------------

LENIENT_RULE = dict(CUSTOM_RULE, forbid_imports=["acme_nothing"])


def test_check_reads_the_committed_rule_file_not_the_working_copy(repo) -> None:
    commit_rules(repo, [CUSTOM_RULE])
    (repo / ".threefold" / "rules.json").write_text(json.dumps([LENIENT_RULE]), encoding="utf-8")
    write(repo, "src/core/invoice.py", "import acme_legacy\n")
    result = check(repo)
    assert result.code == 1
    assert "[acme-core-no-legacy]" in result.out
    assert "not the committed one" in result.out


def test_a_commit_that_loosens_the_rules_is_judged_by_the_rules_before_it(repo) -> None:
    commit_rules(repo, [CUSTOM_RULE])
    write(repo, ".threefold/rules.json", json.dumps([LENIENT_RULE]))
    write(repo, "src/core/invoice.py", "import acme_legacy\n")
    result = check(repo)
    assert result.code == 1
    assert "this commit changes .threefold/rules.json" in result.out


def test_ci_takes_its_rules_from_the_base_and_reports_a_changed_rule_file(repo) -> None:
    """A branch that turns the rule to observe and adds the import passed with
    exit 0 when the rules came from the branch itself."""
    commit_rules(repo, [CUSTOM_RULE])
    _git(repo, "checkout", "-q", "-b", "feature/acme-loosen")
    commit_rules(repo, [dict(CUSTOM_RULE, mode="observe")], "chore: acme rules watch only")
    write(repo, "src/core/invoice.py", "import acme_legacy\n")
    _git(repo, "commit", "-q", "-m", "feat: acme invoice")
    out = io.StringIO()
    code = cli.main(["ci", "--base", "main", "--repo", str(repo), "--mode", "enforce"], out)
    assert code == 1
    assert "REFUSED src/core/invoice.py [acme-core-no-legacy]" in out.getvalue()
    assert f"REFUSED .threefold/rules.json [{cli.RULE_FILE_ID}]" in out.getvalue()


def test_ci_with_an_unchanged_rule_file_reports_nothing_about_it(repo) -> None:
    commit_rules(repo, [CUSTOM_RULE])
    _git(repo, "checkout", "-q", "-b", "feature/acme-docs")
    write(repo, "docs/notes.md", "notes\n")
    _git(repo, "commit", "-q", "-m", "docs: notes")
    out = io.StringIO()
    assert cli.main(["ci", "--base", "main", "--repo", str(repo)], out) == 0
    assert cli.RULE_FILE_ID not in out.getvalue()
