"""One command connects a folder: sensible defaults, no key copied, and proof the first call arrived.

`connect` is what a team runs, so every default is pinned here: the project a
folder name becomes, the agents found on the machine and why, managed mode,
and a second connect that keeps what the first chose. It ends by sending one
harmless dry-run call and opening the dashboard, signed in through a one-time
link when an operator key is configured, so these tests read what reached the
stack and what the browser was asked to open.

The stack is a local HTTP server in a thread that also serves the bundle and
the manifest a served installer installs from. A served installer is made the
way the stack makes one, by replacing the endpoint placeholder in the source.
HOME, USERPROFILE and THREEFOLD_HOME point into a temporary directory, the
browser is replaced, and nothing reaches any network but 127.0.0.1.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
INSTALLER_SOURCE = SRC / "threefold" / "tools" / "threefold_install.py"
PLACEHOLDER = "__THREEFOLD" "_ENDPOINT__"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = _load(ROOT / "scripts" / "threefold_install.py", "threefold_install_for_connect")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _closed_endpoint() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}/prod/"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(*roots: Path) -> Dict[str, bytes]:
    """Every file under the roots, by path, with its bytes. Git's own objects are left out."""
    files = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root.parent).as_posix()
            if "/.git/objects/" in f"/{relative}/" or "/.git/logs/" in f"/{relative}/":
                continue
            files[relative] = path.read_bytes() if path.is_file() else b"<dir>"
    return files


# --- the stack ----------------------------------------------------------------------------------

class Stack:
    """What the fake stack serves and answers, and every request that reached it."""

    def __init__(self) -> None:
        self.port = 0
        self.requests: List[Dict[str, Any]] = []
        self.files: Dict[str, bytes] = {}
        self.evaluation: Tuple[int, Any] = (200, {
            "verdict_id": "acme-verdict-1", "status": "APPROVED", "project_stage": "observe", "warnings": [],
        })
        self.link: Tuple[int, Optional[str]] = (200, None)
        self.projects: Dict[str, Tuple[int, Any]] = {}

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/prod/"

    def sent(self, method: str, relative: str) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["method"] == method and r["path"].split("?", 1)[0] == "/prod/" + relative]

    def answer(self, method: str, relative: str, body: Any) -> Tuple[int, Any]:
        if method == "GET":
            if relative in self.files:
                return 200, self.files[relative]
            if relative.startswith("api/projects/"):
                return self.projects.get(relative[len("api/projects/"):], (404, {"title": "Not Found"}))
            return 404, {"title": "Not Found"}
        if relative == "evaluate-tool-call":
            return self.evaluation
        if relative == "api/auth/links":
            status, url = self.link
            if status != 200:
                return status, {"title": "Forbidden"}
            next_route = body.get("next") if isinstance(body, dict) else None
            return 200, {
                "code": "acme-code-1", "expires_in": 120,
                "url": url or f"{self.endpoint}dashboard.html#/signin?code=acme-code-1&next={next_route}",
            }
        return 404, {"title": "Not Found"}


@pytest.fixture
def stack():
    state = Stack()

    class Handler(BaseHTTPRequestHandler):
        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else None
            state.requests.append({
                "method": method, "path": self.path, "body": body,
                "headers": {key.lower(): value for key, value in self.headers.items()},
            })
            path = self.path.split("?", 1)[0]
            relative = path[len("/prod/"):] if path.startswith("/prod/") else path
            status, answer = state.answer(method, relative, body)
            payload = answer if isinstance(answer, bytes) else json.dumps(answer).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream" if isinstance(answer, bytes) else "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()


def bundle_files() -> Dict[str, bytes]:
    """What the stack packs into its bundle, from this checkout."""
    files = {
        "bin/threefold_hook.py": (SRC / "threefold" / "hooks" / "threefold_hook.py").read_bytes(),
        "bin/threefold_cli.py": (SRC / "threefold" / "tools" / "threefold_cli.py").read_bytes(),
        "lib/threefold/__init__.py": (SRC / "threefold" / "__init__.py").read_bytes(),
    }
    for module in sorted((SRC / "threefold" / "domain").glob("*.py")):
        files[f"lib/threefold/domain/{module.name}"] = module.read_bytes()
    return files


def serve_bundle(stack: Stack, files: Optional[Dict[str, bytes]] = None, manifest_files: Optional[List[Dict[str, Any]]] = None,
                 installer_bytes: Optional[bytes] = None, **manifest_fields: Any) -> Dict[str, bytes]:
    files = bundle_files() if files is None else files
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    bundle = buffer.getvalue()
    manifest: Dict[str, Any] = {
        "files": manifest_files if manifest_files is not None else [
            {"path": path, "sha256": _sha(data), "bytes": len(data)} for path, data in files.items()
        ],
        "bundle_sha256": _sha(bundle),
    }
    if installer_bytes is not None:
        manifest["installer_sha256"] = _sha(installer_bytes)
        stack.files["install.py"] = installer_bytes
    manifest.update(manifest_fields)
    stack.files["dist/threefold-bundle.zip"] = bundle
    stack.files["dist/manifest.json"] = json.dumps(manifest).encode("utf-8")
    return files


def served_text(stack: Stack) -> str:
    """The installer as the stack serves it: the source with its own address in place of the placeholder."""
    return INSTALLER_SOURCE.read_text(encoding="utf-8").replace(PLACEHOLDER, stack.endpoint)


def served_installer(stack: Stack, tmp_path: Path, name: str = "install.py"):
    """A served copy saved to a folder of its own, far from any checkout, and loaded from there."""
    folder = tmp_path / "downloads"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_bytes(served_text(stack).encode("utf-8"))
    return _load(path, "threefold_install_served_" + re.sub(r"\W", "_", name)), path


# --- the machine --------------------------------------------------------------------------------

@pytest.fixture
def machine(tmp_path, monkeypatch):
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
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for variable, value in (("GIT_AUTHOR_NAME", "Acme Dev"), ("GIT_AUTHOR_EMAIL", "dev@acme.example"),
                            ("GIT_COMMITTER_NAME", "Acme Dev"), ("GIT_COMMITTER_EMAIL", "dev@acme.example")):
        monkeypatch.setenv(variable, value)
    # Nothing on the way to 127.0.0.1 but the fake stack.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    # The installer reads configuration through the hook, whose walk from a
    # checkout goes up to the first .threefold.json. One with no include list
    # here ends that walk inside this test's directory, and changes nothing else.
    (tmp_path / ".threefold.json").write_text("{}", encoding="utf-8")
    # Anything that falls through to home's endpoint meets a closed port, never the public stack.
    (home / ".threefold").mkdir()
    (home / ".threefold" / "config.json").write_text(json.dumps({"endpoint": _closed_endpoint()}), encoding="utf-8")
    monkeypatch.setenv("THREEFOLD_TIMEOUT", "2")
    repo = tmp_path / "acme-ledger"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    (repo / "README.md").write_text("# Acme Ledger\n", encoding="utf-8")
    return SimpleNamespace(home=home, threefold_home=home / ".threefold", repo=repo, tmp=tmp_path)


@pytest.fixture(autouse=True)
def browser(monkeypatch):
    """Every page a command asks to open, and none actually opened."""
    opened: List[str] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        return browser_state.opens

    browser_state = SimpleNamespace(opened=opened, opens=True)
    monkeypatch.setattr(installer, "open_url", fake_open)
    browser_state.fake = fake_open
    return browser_state


def run(module, *argv: str) -> SimpleNamespace:
    out = io.StringIO()
    code = module.main(list(argv), out)
    return SimpleNamespace(code=code, out=out.getvalue())


def connect(machine, stack, *extra: str, module=None, path: Optional[Path] = None) -> SimpleNamespace:
    target = str(path or machine.repo)
    return run(module or installer, "connect", target, "--agents", "claude-code,codex", "--endpoint", stack.endpoint, *extra)


def config_of(directory: Path) -> Dict[str, Any]:
    return json.loads((directory / ".threefold.json").read_text(encoding="utf-8"))


def key_file(machine, value: str = "acme-operator-key-1") -> Path:
    path = machine.tmp / "acme-operator.key"
    path.write_text(value, encoding="utf-8")
    return path


# --- the defaults ---------------------------------------------------------------------------------

@pytest.mark.parametrize(("folder", "project"), [
    ("acme-ledger", "Acme-ledger"),
    ("Billing Service", "Acme-Billing-Service"),
    ("payments_v2", "Acme-payments-v2"),
    ("Crème brûlée", "Acme-Creme-brulee"),
    ("--a--b--", "Acme-a-b"),
    ("Acme.Orders", "Acme-Orders"),
    ("acmeledger", "Acme-acmeledger"),
    ("x" * 60, "Acme-" + "x" * 40),
    ("a" * 39 + "-b", "Acme-" + "a" * 39),
])
def test_a_folder_name_becomes_an_acme_alias_that_fits_the_pattern(folder, project, tmp_path) -> None:
    alias = installer.default_project(tmp_path / folder)
    assert alias == project
    assert installer.PROJECT_PATTERN.match(alias)


@pytest.mark.parametrize("folder", ["___", "acme", "αβγ", "..."])
def test_a_folder_name_with_nothing_usable_is_named_by_a_hash_that_says_nothing(folder, tmp_path) -> None:
    alias = installer.default_project(tmp_path / folder)
    assert re.fullmatch(r"Acme-Repo-[0-9a-f]{8}", alias)


def _executable(folder: Path, name: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        (folder / f"{name}.exe").write_bytes(b"")
    else:
        path = folder / name
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)


@pytest.mark.parametrize(("folders", "commands", "expected"), [
    ([".claude"], [], [("claude-code", "~/.claude exists")]),
    ([], ["codex"], [("codex", "codex is on PATH")]),
    ([".gemini/antigravity"], [], [("antigravity", "~/.gemini/antigravity exists")]),
    ([".antigravity"], [], [("antigravity", "~/.antigravity exists")]),
    ([], ["antigravity"], [("antigravity", "antigravity is on PATH")]),
    ([".gemini"], [], []),
    ([".codex"], ["claude", "antigravity"], [
        ("claude-code", "claude is on PATH"), ("codex", "~/.codex exists"), ("antigravity", "antigravity is on PATH"),
    ]),
    ([], [], []),
])
def test_the_agents_on_the_machine_are_found_and_the_reason_is_given(folders, commands, expected, tmp_path) -> None:
    """~/.gemini alone is not Antigravity: other tools keep their settings there too."""
    home = tmp_path / "fake-home"
    home.mkdir()
    for folder in folders:
        (home / folder).mkdir(parents=True)
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    for command in commands:
        _executable(bin_dir, command)
    assert installer.detect_agents(home, str(bin_dir)) == expected


def test_connect_with_every_default_writes_managed_mode_and_the_folder_alias(machine, stack, monkeypatch) -> None:
    (machine.home / ".claude").mkdir()
    bin_dir = machine.tmp / "fake-bin"
    _executable(bin_dir, "codex")
    # git stays reachable; only the agents' commands are faked.
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.path.dirname(shutil.which("git"))]))
    monkeypatch.chdir(machine.repo)
    result = run(installer, "connect", "--endpoint", stack.endpoint, "--no-open")
    assert result.code == 0, result.out
    assert config_of(machine.repo) == {"project": "Acme-ledger", "mode": "managed", "endpoint": stack.endpoint}
    assert (machine.repo / ".claude" / "settings.local.json").is_file()
    assert (machine.repo / ".codex" / "hooks.json").is_file()
    assert not (machine.repo / ".agents").exists(), "Antigravity was not found, so it is not registered"
    assert "claude-code (~/.claude exists), codex (codex is on PATH)" in result.out
    assert "from the folder name" in result.out
    assert "Codex: trust this project in Codex" in result.out
    assert "Antigravity" not in result.out.split("Next", 1)[1]


def test_with_no_agent_found_all_three_are_registered_and_the_output_says_why(machine, stack, monkeypatch) -> None:
    empty = machine.tmp / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(empty), os.path.dirname(shutil.which("git"))]))
    result = run(installer, "connect", str(machine.repo), "--endpoint", stack.endpoint, "--no-open")
    assert result.code == 0, result.out
    assert "all three, because none was found" in result.out
    for relative in (".claude/settings.local.json", ".codex/hooks.json", ".agents/hooks.json"):
        assert (machine.repo / relative).is_file()
    assert "Antigravity: if it asks whether to trust this workspace's hooks, say yes." in result.out


def test_the_home_folder_is_refused_and_nothing_is_written(machine, stack) -> None:
    before = snapshot(machine.home)
    result = run(installer, "connect", str(machine.home), "--agents", "claude-code", "--endpoint", stack.endpoint)
    assert result.code == 2
    assert "home folder" in result.out and "Nothing was changed" in result.out
    assert snapshot(machine.home) == before
    assert stack.requests == []


def test_disconnecting_the_home_folder_is_refused_and_leaves_the_agents_own_hooks_alone(machine, stack) -> None:
    """connect refuses the home folder; disconnect went in anyway.

    With no install record there, uninstall falls back to removing any entry
    whose command names the hook, so a Threefold entry a developer registered
    by hand at user level went, and with it governance for every project of
    that agent on the machine.
    """
    hooks = machine.home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True, exist_ok=True)
    registered = {
        "hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [
                {"type": "command", "command": "python3 /opt/acme/threefold_hook.py --agent codex"},
                {"type": "command", "command": "python3 /opt/acme/audit.py"},
            ]},
        ]},
    }
    hooks.write_text(json.dumps(registered), encoding="utf-8")
    before = snapshot(machine.home)

    result = run(installer, "disconnect", str(machine.home))
    assert result.code == 2, result.out
    assert "home folder" in result.out
    assert snapshot(machine.home) == before
    assert json.loads(hooks.read_text(encoding="utf-8")) == registered


def test_the_legacy_uninstall_refuses_the_home_folder_too(machine, stack) -> None:
    hooks = machine.home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 /opt/acme/threefold_hook.py --agent codex"}]},
    ]}}), encoding="utf-8")
    before = snapshot(machine.home)
    result = run(installer, "--repo", str(machine.home), "--uninstall")
    assert result.code == 2, result.out
    assert "home folder" in result.out
    assert snapshot(machine.home) == before


# --- two checkouts of one repository ---------------------------------------------------------

@pytest.fixture
def linked_worktree(machine) -> Path:
    """A second checkout of the same repository, as `git worktree add` makes one.

    Its install record is its own, under .git/worktrees/<id>/, but
    `git rev-parse --git-path hooks` and `info/exclude` both answer with the
    common directory, which the two checkouts share.
    """
    assert _git(machine.repo, "add", "README.md").returncode == 0
    assert _git(machine.repo, "commit", "-qm", "first").returncode == 0
    feature = machine.tmp / "acme-feature"
    result = _git(machine.repo, "worktree", "add", "-q", str(feature), "-b", "feature")
    assert result.returncode == 0, result.stderr
    return feature


def test_disconnecting_one_checkout_keeps_what_the_other_still_needs(machine, stack, linked_worktree) -> None:
    """The pre-commit hook and the exclude lines live in the shared git directory.

    Removed for the checkout being disconnected, the other one lost its
    commit-time check and its .threefold.json and .claude/ came back as
    untracked files, one `git add .` away from being committed.
    """
    assert connect(machine, stack, "--no-open").code == 0
    assert connect(machine, stack, "--no-open", path=linked_worktree).code == 0
    pre_commit = machine.repo / ".git" / "hooks" / "pre-commit"
    assert pre_commit.is_file()
    assert _git(linked_worktree, "status", "--porcelain").stdout.strip() == ""

    result = run(installer, "disconnect", str(machine.repo))
    assert result.code == 0, result.out
    assert pre_commit.is_file(), "the other checkout still runs this on every commit"
    assert (linked_worktree / ".threefold.json").is_file()
    assert _git(linked_worktree, "status", "--porcelain").stdout.strip() == ""
    assert "still connected" in result.out
    assert not (machine.repo / ".threefold.json").exists()
    assert not (machine.repo / ".claude").exists()


def test_disconnecting_the_last_checkout_takes_the_shared_hook_out(machine, stack, linked_worktree) -> None:
    assert connect(machine, stack, "--no-open").code == 0
    assert connect(machine, stack, "--no-open", path=linked_worktree).code == 0
    assert run(installer, "disconnect", str(linked_worktree)).code == 0
    result = run(installer, "disconnect", str(machine.repo))
    assert result.code == 0, result.out
    assert "still connected" not in result.out
    assert not (machine.repo / ".git" / "hooks" / "pre-commit").exists()
    assert _git(machine.repo, "status", "--porcelain").stdout.strip() == ""


# --- a .threefold.json the repository committed ------------------------------------------------

@pytest.fixture
def committed_config(machine):
    """A cloned repository whose .threefold.json is tracked, so it, not this command, decides."""

    def commit(**fields):
        document = {"project": "Acme-Cloned", "mode": "observe"}
        document.update(fields)
        (machine.repo / ".threefold.json").write_text(json.dumps(document), encoding="utf-8")
        assert _git(machine.repo, "add", ".threefold.json").returncode == 0
        assert _git(machine.repo, "commit", "-qm", "configure threefold").returncode == 0
        return document

    return commit


def header(out: str, label: str) -> str:
    """The value on connect's header line for `label`."""
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith(label + " "):
            return stripped[len(label):].strip()
    return ""


def test_connect_reports_the_endpoint_and_mode_the_committed_file_decides(machine, stack, committed_config) -> None:
    """The header used to print the endpoint and the mode that were asked for.

    The hook reads the committed file, so the developer's calls went to the
    repository's server, in the repository's mode, while the output said
    otherwise and the key file was paired with a stack nothing would use.
    """
    other = _closed_endpoint()
    committed_config(endpoint=other)
    result = run(installer, "connect", str(machine.repo), "--agents", "claude-code",
                 "--endpoint", stack.endpoint, "--api-key-file", str(key_file(machine)),
                 "--project", "Acme-Mine", "--mode", "enforce", "--no-open")
    assert result.code == 0, result.out
    assert header(result.out, "endpoint") == other, result.out
    assert header(result.out, "mode").startswith("observe"), result.out
    assert "the tracked .threefold.json decides it" in result.out, "the header says what decided these"
    paired = json.loads((machine.threefold_home / "config.json").read_text(encoding="utf-8"))
    assert stack.endpoint not in json.dumps(paired.get("trusted_endpoints", [])), \
        "an endpoint the hook will not use is not paired with the key file"
    assert stack.sent("POST", "evaluate-tool-call") == [], "the first call went where the committed file says"


def test_a_committed_file_that_agrees_says_nothing_extra(machine, stack, committed_config) -> None:
    committed_config(endpoint=stack.endpoint, mode="managed")
    result = run(installer, "connect", str(machine.repo), "--agents", "claude-code",
                 "--endpoint", stack.endpoint, "--mode", "managed", "--no-open")
    assert result.code == 0, result.out
    assert header(result.out, "endpoint") == stack.endpoint
    assert header(result.out, "in force") == ""


def test_a_committed_file_that_sets_no_mode_names_the_default_and_not_the_commit(machine, committed_config) -> None:
    """The difference here is the hook's own fallback, not anything in the commit.

    connect defaults to managed and the hook falls back to enforce, so a
    tracked .threefold.json that sets no mode at all made the line fire. It
    said the commit decided, and the reader went to change a file that says
    nothing about the mode.
    """
    (machine.repo / ".threefold.json").write_text(json.dumps({"project": "Acme-Cloned"}), encoding="utf-8")
    assert _git(machine.repo, "add", ".threefold.json").returncode == 0
    assert _git(machine.repo, "commit", "-qm", "configure threefold").returncode == 0
    result = run(installer, "connect", str(machine.repo), "--agents", "claude-code",
                 "--endpoint", _closed_endpoint(), "--no-open", "--dry-run")
    assert result.code == 0, result.out
    assert header(result.out, "mode").startswith("enforce"), result.out
    assert "nothing sets it, so the hook's own default applies" in result.out, result.out
    assert "the tracked .threefold.json decides it" not in result.out, result.out
    assert "the committed file decides the project here" in result.out, result.out
    assert "decides the project and the mode" not in result.out, "the file this one commits says no mode"


def test_a_variable_in_the_environment_is_named_when_it_is_what_decides(
    machine, stack, committed_config, monkeypatch
) -> None:
    """The environment is read before any file, so it, not the commit, is named."""
    committed_config()
    monkeypatch.setenv("THREEFOLD_ENDPOINT", stack.endpoint)
    monkeypatch.setenv("THREEFOLD_MODE", "observe")
    result = run(installer, "connect", str(machine.repo), "--agents", "claude-code",
                 "--endpoint", _closed_endpoint(), "--mode", "enforce", "--no-open")
    assert result.code == 0, result.out
    assert header(result.out, "endpoint") == stack.endpoint, result.out
    assert header(result.out, "mode").startswith("observe"), result.out
    assert "THREEFOLD_ENDPOINT in the environment decides it" in result.out, result.out
    assert "THREEFOLD_MODE or THREEFOLD_DRY_RUN in the environment decides it" in result.out, result.out
    assert "the tracked .threefold.json decides it" not in result.out, result.out


def test_a_committed_include_list_is_reported(machine, stack, committed_config) -> None:
    """The list decides which of a workspace's folders are sent at all, so it is shown."""
    committed_config(endpoint=stack.endpoint, mode="managed", include=["repos/acme-a/**"])
    result = run(installer, "connect", str(machine.repo), "--agents", "claude-code",
                 "--endpoint", stack.endpoint, "--mode", "managed", "--no-open")
    assert result.code == 0, result.out
    assert "include: the tracked .threefold.json sends only what matches repos/acme-a/**" in result.out, result.out


def test_an_untracked_configuration_still_pairs_the_endpoint_with_its_key_file(machine, stack) -> None:
    result = connect(machine, stack, "--api-key-file", str(key_file(machine)), "--no-open")
    assert result.code == 0, result.out
    paired = json.loads((machine.threefold_home / "config.json").read_text(encoding="utf-8"))
    assert [entry["endpoint"] for entry in paired["trusted_endpoints"]] == [stack.endpoint]


def test_a_project_that_is_not_an_alias_is_refused(machine, stack) -> None:
    result = connect(machine, stack, "--project", "Acme Ledger")
    assert result.code == 2 and "must match" in result.out
    assert not (machine.repo / ".threefold.json").exists()


# --- the first call and the dashboard ----------------------------------------------------------------

def test_connect_sends_one_harmless_dry_run_call_and_says_it_was_recorded(machine, stack, browser) -> None:
    result = connect(machine, stack)
    assert result.code == 0, result.out
    calls = stack.sent("POST", "evaluate-tool-call")
    assert len(calls) == 1
    body = calls[0]["body"]
    assert re.fullmatch(r"threefold-connect-[0-9a-f]{8}", body.pop("session_id"))
    assert body == {
        "project_name": "Acme-ledger", "developer": "anonymous", "tool_name": "Bash", "action_type": "COMMAND_EXEC",
        "arguments": {"command": "git status"}, "agent": "claude-code", "origin": "hook", "explain": False,
        "dry_run": True, "hook_mode": "managed",
    }
    assert "x-api-key" not in calls[0]["headers"], "no key is configured for this endpoint"
    assert "first call  recorded: APPROVED, project stage observe" in result.out
    assert browser.opened == [f"{stack.endpoint}dashboard.html#/projects/Acme-ledger"]
    assert stack.sent("POST", "api/auth/links") == [], "no key, so no sign-in link is asked for"


def test_the_stage_the_first_call_names_is_kept_for_the_hook(machine, stack) -> None:
    stack.evaluation = (200, {"verdict_id": "acme-verdict-2", "status": "APPROVED", "project_stage": "enforce"})
    connect(machine, stack, "--no-open")
    digest = hashlib.sha256(b"Acme-ledger").hexdigest()[:16]
    cached = json.loads((machine.threefold_home / "stage" / f"{digest}.json").read_text(encoding="utf-8"))
    assert cached["stage"] == "enforce"


def test_with_an_operator_key_the_call_carries_it_and_the_dashboard_opens_through_a_sign_in_link(machine, stack, browser) -> None:
    key = key_file(machine)
    result = connect(machine, stack, "--api-key-file", str(key))
    assert result.code == 0, result.out
    assert stack.sent("POST", "evaluate-tool-call")[0]["headers"]["x-api-key"] == "acme-operator-key-1"
    links = stack.sent("POST", "api/auth/links")
    assert len(links) == 1
    assert links[0]["body"] == {"next": "/projects/Acme-ledger"}
    assert links[0]["headers"]["x-api-key"] == "acme-operator-key-1"
    assert browser.opened == [f"{stack.endpoint}dashboard.html#/signin?code=acme-code-1&next=/projects/Acme-ledger"]
    assert "signed in" in result.out
    assert "acme-operator-key-1" not in result.out
    assert "acme-code-1" not in result.out, "a link a browser opened is not printed as well"
    assert "acme-operator-key-1" not in (machine.repo / ".threefold.json").read_text(encoding="utf-8")


def test_with_no_browser_the_sign_in_link_is_printed_once_with_its_limits(machine, stack, browser) -> None:
    browser.opens = False
    result = connect(machine, stack, "--api-key-file", str(key_file(machine)))
    assert "sign in within 2 minutes, once, at:" in result.out
    assert "acme-code-1" in result.out


def test_a_sign_in_link_to_somewhere_else_is_not_opened(machine, stack, browser) -> None:
    stack.link = (200, "https://elsewhere.example/dashboard.html#/signin?code=acme-code-1")
    result = connect(machine, stack, "--api-key-file", str(key_file(machine)))
    assert browser.opened == [f"{stack.endpoint}dashboard.html#/projects/Acme-ledger"]
    assert "somewhere else" in result.out


def test_a_key_the_stack_refuses_for_a_link_still_opens_the_dashboard(machine, stack, browser) -> None:
    stack.link = (403, None)
    result = connect(machine, stack, "--api-key-file", str(key_file(machine)))
    assert result.code == 0
    assert browser.opened == [f"{stack.endpoint}dashboard.html#/projects/Acme-ledger"]
    assert "HTTP 403" in result.out


def test_no_open_prints_the_dashboard_and_opens_nothing(machine, stack, browser) -> None:
    result = connect(machine, stack, "--no-open", "--api-key-file", str(key_file(machine)))
    assert browser.opened == []
    assert f"{stack.endpoint}dashboard.html#/projects/Acme-ledger" in result.out
    assert stack.sent("POST", "api/auth/links") == [], "a link nobody opens would only expire"
    assert "to sign in from this machine:" in result.out and " open" in result.out


def test_a_stack_that_cannot_be_reached_is_said_plainly_and_connect_still_finishes(machine, stack, browser) -> None:
    closed = _closed_endpoint()
    result = run(installer, "connect", str(machine.repo), "--agents", "claude-code", "--endpoint", closed)
    assert result.code == 0, result.out
    assert "first call  not recorded:" in result.out and "could not be reached" in result.out
    assert browser.opened == [f"{closed}dashboard.html#/projects/Acme-ledger"]
    assert config_of(machine.repo)["mode"] == "managed"


def test_a_stack_that_wants_a_key_says_how_to_give_it_one(machine, stack) -> None:
    stack.evaluation = (401, {"title": "Unauthorized"})
    result = connect(machine, stack, "--no-open")
    assert "not recorded: the stack answered HTTP 401" in result.out
    assert "--api-key-file" in result.out


def test_a_warning_from_the_stack_is_shown(machine, stack) -> None:
    stack.evaluation = (200, {"verdict_id": "v", "status": "APPROVED", "project_stage": "observe",
                              "warnings": ["project_name was recorded as 'unlabelled'."]})
    result = connect(machine, stack, "--no-open")
    assert "recorded as 'unlabelled'" in result.out


def test_a_dry_run_writes_sends_and_opens_nothing(machine, stack, browser) -> None:
    before = snapshot(machine.repo, machine.home)
    result = connect(machine, stack, "--dry-run", "--api-key-file", str(key_file(machine)))
    assert result.code == 0, result.out
    assert snapshot(machine.repo, machine.home) == before
    assert stack.requests == [] and browser.opened == []
    assert "would write .threefold.json for Acme-ledger in managed mode" in result.out
    assert "would send one dry-run call" in result.out and "would open" in result.out


# --- a second connect keeps what the first chose -------------------------------------------------------

@pytest.fixture
def workspace(machine) -> Path:
    root = machine.tmp / "acme-workspace"
    for name in ("acme-alpha", "acme-beta"):
        (root / "repos" / name).mkdir(parents=True)
    return root


def test_a_second_connect_keeps_the_project_mode_and_include_list(machine, stack, workspace) -> None:
    first = connect(machine, stack, "--project", "Acme-Workspace", "--mode", "observe",
                    "--include", "repos/acme-alpha/**", "--no-open", path=workspace)
    assert first.code == 0, first.out
    again = run(installer, "connect", str(workspace), "--agents", "claude-code,codex", "--no-open")
    assert again.code == 0, again.out
    assert config_of(workspace) == {
        "project": "Acme-Workspace", "mode": "observe", "endpoint": stack.endpoint, "include": ["repos/acme-alpha/**"],
    }
    assert "kept" in again.out and "include list" in again.out


def test_a_workspace_connects_without_a_pre_commit_hook(machine, stack, workspace) -> None:
    result = connect(machine, stack, "--no-open", path=workspace)
    assert result.code == 0, result.out
    assert "a workspace, not a git repository" in result.out
    assert config_of(workspace)["project"] == "Acme-workspace"
    assert not (workspace / ".git").exists()


# --- a configuration Windows PowerShell wrote ------------------------------------------------------

def test_a_second_connect_keeps_an_include_list_a_powershell_edit_left_in_utf_16(machine, stack, workspace) -> None:
    """`>` and Out-File in Windows PowerShell 5.1 write UTF-16 with a byte order mark.

    Read as UTF-8 the file was no file at all, so the second connect kept
    nothing: it wrote the folder-derived name with no `include`, and every
    repository the owner had left out started being sent. The hook reads the
    same file in either encoding on purpose.
    """
    first = connect(machine, stack, "--project", "Acme-Workspace", "--mode", "observe",
                    "--include", "repos/acme-alpha/**", "--no-open", path=workspace)
    assert first.code == 0, first.out
    config = workspace / ".threefold.json"
    config.write_bytes(config.read_text(encoding="utf-8").encode("utf-16"))

    again = run(installer, "connect", str(workspace), "--agents", "claude-code,codex", "--no-open")
    assert again.code == 0, again.out
    assert "include list" in again.out
    assert config_of(workspace) == {
        "project": "Acme-Workspace", "mode": "observe", "endpoint": stack.endpoint, "include": ["repos/acme-alpha/**"],
    }


def test_a_configuration_that_cannot_be_read_stops_connect_instead_of_being_written_over(machine, stack, workspace) -> None:
    first = connect(machine, stack, "--project", "Acme-Workspace", "--include", "repos/acme-alpha/**",
                    "--no-open", path=workspace)
    assert first.code == 0, first.out
    config = workspace / ".threefold.json"
    before = config.read_bytes()
    config.write_bytes(b'{"project": "Acme-Workspace", "include": ["repos/acme-\xe9lpha/**"]}')

    again = run(installer, "connect", str(workspace), "--agents", "claude-code", "--no-open")
    assert again.code == 2, again.out
    assert "left alone" in again.out
    assert config.read_bytes() != before or True
    assert config.read_bytes() == b'{"project": "Acme-Workspace", "include": ["repos/acme-\xe9lpha/**"]}'


def test_an_agent_settings_file_in_utf_16_is_read_rather_than_crashing_the_command(machine, stack) -> None:
    settings = machine.repo / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(json.dumps({"env": {"ACME_REGION": "eu-west-1"}}).encode("utf-16"))
    result = connect(machine, stack, "--no-open")
    assert result.code == 0, result.out
    merged = json.loads(settings.read_text(encoding="utf-8"))
    assert merged["env"] == {"ACME_REGION": "eu-west-1"}
    assert merged["hooks"]["PreToolUse"]


def test_an_agent_settings_file_that_is_not_text_is_left_alone_with_a_message(machine, stack) -> None:
    settings = machine.repo / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"env": {"ACME_NOTE": "caf\xe9"}}')
    result = connect(machine, stack, "--no-open")
    assert result.code == 2, result.out
    assert "is not UTF-8 or UTF-16 text" in result.out and "left alone" in result.out
    assert settings.read_bytes() == b'{"env": {"ACME_NOTE": "caf\xe9"}}'


def test_an_exclude_file_that_is_not_utf_8_is_added_to_rather_than_crashing_the_command(machine, stack) -> None:
    exclude = machine.repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_bytes(b"# caf\xe9 build output\n/build\n")
    result = connect(machine, stack, "--no-open")
    assert result.code == 0, result.out
    after = exclude.read_bytes()
    assert after.startswith(b"# caf\xe9 build output\n/build\n")
    assert b"/.threefold.json" in after


def test_disconnect_takes_its_lines_out_of_an_exclude_file_that_is_not_utf_8(machine, stack) -> None:
    exclude = machine.repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_bytes(b"# caf\xe9 build output\n/build\n")
    assert connect(machine, stack, "--no-open").code == 0
    result = run(installer, "disconnect", str(machine.repo))
    assert result.code == 0, result.out
    assert exclude.read_bytes() == b"# caf\xe9 build output\n/build\n"


# --- a name the owner said must never leave --------------------------------------------------------

def _never_send(machine, text: str) -> None:
    machine.threefold_home.mkdir(parents=True, exist_ok=True)
    (machine.threefold_home / "never_send.txt").write_text(text, encoding="utf-8")


def test_connect_refuses_a_default_project_name_that_holds_a_never_send_term(machine, stack, tmp_path) -> None:
    """The folder is named after the client, so the default alias is too, and
    connect's own first call publishes it on the stack before the hook runs."""
    _never_send(machine, "globex\n")
    folder = tmp_path / "globex-billing"
    folder.mkdir()
    result = connect(machine, stack, "--no-open", path=folder)
    assert result.code == 2, result.out
    assert "never-send list" in result.out
    assert "globex" not in result.out.lower(), "the term is not printed on a screen that may be shared"
    assert not (folder / ".threefold.json").exists()
    assert stack.sent("POST", "evaluate-tool-call") == []


def test_connect_refuses_a_given_project_name_that_holds_a_never_send_term(machine, stack) -> None:
    _never_send(machine, "globex\n")
    result = connect(machine, stack, "--project", "Acme-Globex-Portal", "--no-open")
    assert result.code == 2, result.out
    assert "never-send list" in result.out
    assert not (machine.repo / ".threefold.json").exists()


def test_connect_refuses_when_the_never_send_list_itself_cannot_be_read(machine, stack) -> None:
    machine.threefold_home.mkdir(parents=True, exist_ok=True)
    (machine.threefold_home / "never_send.txt").write_bytes(b"globex-\xe9\xff\n")
    result = connect(machine, stack, "--no-open")
    assert result.code == 2, result.out
    assert "never-send" in result.out
    assert not (machine.repo / ".threefold.json").exists()


def test_a_project_name_on_no_list_connects_as_before(machine, stack) -> None:
    _never_send(machine, "globex\n")
    result = connect(machine, stack, "--no-open")
    assert result.code == 0, result.out
    assert config_of(machine.repo)["project"] == "Acme-ledger"


# --- status, disconnect and open ------------------------------------------------------------------------

def test_status_lists_every_install_with_its_project_mode_agents_and_stage(machine, stack, workspace) -> None:
    connect(machine, stack, "--no-open")
    connect(machine, stack, "--no-open", "--mode", "observe", path=workspace)
    stack.projects["Acme-ledger"] = (200, {"project": "Acme-ledger", "config": {"stage": "enforce"}})
    stack.projects["Acme-workspace"] = (200, {"project": "Acme-workspace", "config": None,
                                              "readiness": {"summary": {"stage": "observe"}}})
    index = json.loads((machine.threefold_home / "installs" / "index.json").read_text(encoding="utf-8"))
    assert [entry["project"] for entry in index["installs"]] == ["Acme-ledger", "Acme-workspace"]

    result = run(installer, "status")
    assert result.code == 0, result.out
    assert "2 connected" in result.out
    assert machine.repo.resolve().as_posix() in result.out and workspace.resolve().as_posix() in result.out
    assert "project Acme-ledger   mode managed   stage enforce" in result.out
    assert "project Acme-workspace   mode observe   stage observe" in result.out
    assert "agents  claude-code, codex" in result.out
    assert len(stack.sent("GET", "api/projects/Acme-ledger")) == 1


def test_a_second_connect_for_fewer_agents_still_lists_every_agent_whose_hook_runs(machine, stack) -> None:
    connect(machine, stack, "--no-open")
    run(installer, "connect", str(machine.repo), "--agents", "claude-code", "--no-open")
    assert "agents  claude-code, codex" in run(installer, "status").out


def test_status_asks_for_the_stage_with_the_key_and_says_unknown_when_it_cannot(machine, stack) -> None:
    connect(machine, stack, "--no-open", "--api-key-file", str(key_file(machine)))
    result = run(installer, "status")
    assert "stage unknown" in result.out, "the fake stack does not know this project"
    assert stack.sent("GET", "api/projects/Acme-ledger")[0]["headers"]["x-api-key"] == "acme-operator-key-1"
    assert "acme-operator-key-1" not in result.out


def test_status_says_unknown_when_the_stack_cannot_be_reached(machine, stack) -> None:
    closed = _closed_endpoint()
    run(installer, "connect", str(machine.repo), "--agents", "claude-code", "--endpoint", closed, "--no-open")
    result = run(installer, "status")
    assert result.code == 0
    assert "stage unknown" in result.out


def test_disconnect_takes_the_install_out_of_the_index_and_status_says_so(machine, stack, workspace) -> None:
    connect(machine, stack, "--no-open")
    connect(machine, stack, "--no-open", path=workspace)
    assert run(installer, "disconnect", str(machine.repo)).code == 0
    assert not (machine.repo / ".threefold.json").exists()
    assert "1 connected" in run(installer, "status").out

    result = run(installer, "disconnect", str(workspace))
    assert result.code == 0, result.out
    assert "Disconnected" in result.out
    assert not (machine.threefold_home / "installs").exists(), "the index goes with the last install"
    assert "not connected to anything" in run(installer, "status").out


def test_the_older_form_is_listed_too(machine, stack) -> None:
    result = run(installer, "--repo", str(machine.repo), "--project", "Acme-Ledger")
    assert result.code == 0, result.out
    assert "project Acme-Ledger   mode observe" in run(installer, "status").out
    run(installer, "--repo", str(machine.repo), "--uninstall")
    assert "not connected to anything" in run(installer, "status").out


def test_open_signs_in_to_the_project_of_the_folder_it_is_run_in(machine, stack, browser, monkeypatch) -> None:
    connect(machine, stack, "--no-open", "--api-key-file", str(key_file(machine)))
    monkeypatch.chdir(machine.repo)
    result = run(installer, "open")
    assert result.code == 0, result.out
    assert stack.sent("POST", "api/auth/links")[-1]["body"] == {"next": "/projects/Acme-ledger"}
    assert browser.opened == [f"{stack.endpoint}dashboard.html#/signin?code=acme-code-1&next=/projects/Acme-ledger"]


def test_open_goes_where_next_says_with_the_key_the_owner_paired(machine, stack, browser, monkeypatch) -> None:
    connect(machine, stack, "--no-open", "--api-key-file", str(key_file(machine)))
    monkeypatch.chdir(machine.tmp)
    result = run(installer, "open", "--endpoint", stack.endpoint, "--next", "/overview")
    assert result.code == 0, result.out
    link = stack.sent("POST", "api/auth/links")[-1]
    assert link["body"] == {"next": "/overview"} and link["headers"]["x-api-key"] == "acme-operator-key-1"


def test_open_without_a_key_opens_the_dashboard_and_says_it_is_not_signed_in(machine, stack, browser, monkeypatch) -> None:
    monkeypatch.chdir(machine.tmp)
    result = run(installer, "open", "--endpoint", stack.endpoint)
    assert result.code == 0, result.out
    assert browser.opened == [f"{stack.endpoint}dashboard.html#/overview"]
    assert "no operator key" in result.out
    assert stack.sent("POST", "api/auth/links") == []


@pytest.mark.parametrize("route", ["https://elsewhere.example/", "/projects/Acme x", "javascript:alert(1)", "projects"])
def test_open_refuses_a_route_that_is_not_one_of_the_dashboards(machine, stack, browser, route) -> None:
    result = run(installer, "open", "--endpoint", stack.endpoint, "--next", route)
    assert result.code == 2
    assert browser.opened == []


def test_the_usage_names_the_four_commands(machine) -> None:
    result = run(installer, "--help")
    assert result.code == 0
    for command in ("connect", "disconnect", "status", "open", "--repo"):
        assert command in result.out


# --- a served copy ------------------------------------------------------------------------------------

def test_the_source_carries_the_placeholder_exactly_once_and_a_checkout_copy_is_not_served() -> None:
    """The stack replaces every occurrence, so a second one would be replaced too and
    the copy could no longer tell it was served."""
    assert INSTALLER_SOURCE.read_text(encoding="utf-8").count(PLACEHOLDER) == 1
    assert installer.served_endpoint() is None


def test_the_installer_is_ascii_so_a_shell_can_pipe_it() -> None:
    INSTALLER_SOURCE.read_bytes().decode("ascii")


def test_a_served_copy_connects_from_the_stacks_bundle_with_no_endpoint_given(machine, stack, tmp_path) -> None:
    served, path = served_installer(stack, tmp_path)
    monkeypatch_open(served)
    files = serve_bundle(stack)
    assert served.served_endpoint() == stack.endpoint
    result = run(served, "connect", str(machine.repo), "--agents", "claude-code", "--no-open")
    assert result.code == 0, result.out
    for relative, data in files.items():
        assert (machine.threefold_home / relative).read_bytes() == data, relative
    assert (machine.threefold_home / "bin" / "threefold_install.py").read_bytes() == path.read_bytes()
    assert config_of(machine.repo)["endpoint"] == stack.endpoint
    assert len(stack.sent("GET", "dist/manifest.json")) == 1 and len(stack.sent("GET", "dist/threefold-bundle.zip")) == 1
    assert "first call  recorded" in result.out
    assert "threefold_install.py status" in result.out and ".threefold/bin/threefold_install.py" in result.out


def test_the_kept_copy_of_a_served_installer_runs_status_later(machine, stack, tmp_path) -> None:
    served, _ = served_installer(stack, tmp_path)
    monkeypatch_open(served)
    serve_bundle(stack)
    run(served, "connect", str(machine.repo), "--agents", "claude-code", "--no-open")
    kept = _load(machine.threefold_home / "bin" / "threefold_install.py", "threefold_install_kept")
    assert kept.served_endpoint() == stack.endpoint
    assert "1 connected" in run(kept, "status").out


@pytest.mark.parametrize("damage", ["file-hash", "file-size", "bundle-hash", "missing-file", "outside-path", "not-listed-required"])
def test_a_served_copy_refuses_a_bundle_that_does_not_match_its_manifest_and_writes_nothing(damage, machine, stack, tmp_path) -> None:
    served, _ = served_installer(stack, tmp_path)
    monkeypatch_open(served)
    files = bundle_files()
    entries = [{"path": p, "sha256": _sha(d), "bytes": len(d)} for p, d in files.items()]
    extra: Dict[str, Any] = {}
    if damage == "file-hash":
        entries[1]["sha256"] = _sha(b"something else")
    elif damage == "file-size":
        entries[0]["bytes"] += 1
    elif damage == "bundle-hash":
        extra["bundle_sha256"] = _sha(b"another bundle")
    elif damage == "missing-file":
        entries.append({"path": "lib/threefold/domain/acme_extra.py", "sha256": _sha(b"x = 1\n"), "bytes": 6})
    elif damage == "outside-path":
        entries.append({"path": "../acme_outside.py", "sha256": _sha(b"x"), "bytes": 1})
        files["../acme_outside.py"] = b"x"
    else:
        entries = [entry for entry in entries if entry["path"] != "bin/threefold_cli.py"]
    serve_bundle(stack, files=files, manifest_files=entries, **extra)
    before = snapshot(machine.repo, machine.home)
    result = run(served, "connect", str(machine.repo), "--agents", "claude-code", "--no-open")
    assert result.code == 2, result.out
    assert "nothing was written" in result.out.lower() and "Nothing was changed" in result.out
    assert snapshot(machine.repo, machine.home) == before
    assert stack.sent("POST", "evaluate-tool-call") == []
    assert not (machine.tmp / "acme_outside.py").exists()


def test_a_served_copy_that_cannot_reach_its_stack_installs_nothing(machine, tmp_path) -> None:
    dead = Stack()
    dead.port = int(_closed_endpoint().split(":")[2].split("/")[0])
    served, _ = served_installer(dead, tmp_path)
    monkeypatch_open(served)
    before = snapshot(machine.repo, machine.home)
    result = run(served, "connect", str(machine.repo), "--agents", "claude-code", "--no-open")
    assert result.code == 2
    assert "could not be downloaded" in result.out
    assert snapshot(machine.repo, machine.home) == before


def test_a_served_dry_run_downloads_nothing(machine, stack, tmp_path) -> None:
    served, _ = served_installer(stack, tmp_path)
    monkeypatch_open(served)
    serve_bundle(stack)
    before = snapshot(machine.repo, machine.home)
    result = run(served, "connect", str(machine.repo), "--agents", "claude-code", "--dry-run")
    assert result.code == 0, result.out
    assert stack.requests == []
    assert snapshot(machine.repo, machine.home) == before
    assert "would download the hook" in result.out


def test_piped_into_python_the_installer_is_read_as_running_from_no_file(machine, stack) -> None:
    """`python3 -` sets __file__ to `<stdin>`; read as a path it put the
    installer in the current directory. Some runners leave __file__ unset."""
    text = served_text(stack)
    for namespace in ({"__name__": "piped_a", "__file__": "<stdin>"}, {"__name__": "piped_b"}):
        exec(compile(text, "<stdin>", "exec"), namespace)
        assert namespace["HERE"] is None and namespace["HOOK_SOURCE"] is None
        assert namespace["own_source"]() is None
        assert namespace["installer_command"](machine.threefold_home).endswith(" threefold_install.py")


def test_a_served_copy_piped_into_python_connects_end_to_end(machine, stack) -> None:
    """The macOS and Linux command, run as a shell runs it: the served text on
    Python's stdin, `-` as the script, in the folder being connected."""
    text = served_text(stack)
    serve_bundle(stack, installer_bytes=text.encode("utf-8"))
    result = subprocess.run(
        [sys.executable, "-", "connect", "--agents", "claude-code", "--no-open"],
        input=text.encode("utf-8"), capture_output=True, cwd=str(machine.repo), timeout=120,
    )
    out = result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace")
    assert result.returncode == 0, out
    assert config_of(machine.repo) == {"project": "Acme-ledger", "mode": "managed", "endpoint": stack.endpoint}
    assert (machine.threefold_home / "bin" / "threefold_install.py").read_bytes() == text.encode("utf-8")
    assert (machine.threefold_home / "bin" / "threefold_hook.py").is_file()
    assert len(stack.sent("POST", "evaluate-tool-call")) == 1
    assert "first call  recorded" in out
    assert "<stdin>" not in out


def test_a_served_copy_piped_into_python_keeps_the_stacks_installer_when_its_hash_matches(machine, stack) -> None:
    """Piped, there is no file to copy, so the stack's own /install.py is fetched
    and kept only if it hashes to what the manifest says."""
    text = served_text(stack)
    serve_bundle(stack, installer_bytes=text.encode("utf-8"))
    namespace: Dict[str, Any] = {"__name__": "threefold_install_piped", "__file__": "<stdin>"}
    exec(compile(text, "<stdin>", "exec"), namespace)
    assert namespace["HERE"] is None and namespace["HOOK_SOURCE"] is None
    namespace["open_url"] = lambda url: True
    out = io.StringIO()
    code = namespace["main"](["connect", str(machine.repo), "--agents", "claude-code", "--no-open"], out)
    assert code == 0, out.getvalue()
    assert (machine.threefold_home / "bin" / "threefold_install.py").read_bytes() == text.encode("utf-8")
    assert (machine.threefold_home / "bin" / "threefold_hook.py").is_file()


def test_a_piped_copy_keeps_no_installer_whose_hash_does_not_match(machine, stack) -> None:
    text = served_text(stack)
    serve_bundle(stack, installer_bytes=text.encode("utf-8"))
    stack.files["install.py"] = b"# something else\n"
    namespace: Dict[str, Any] = {"__name__": "threefold_install_piped_bad", "__file__": "<stdin>"}
    exec(compile(text, "<stdin>", "exec"), namespace)
    namespace["open_url"] = lambda url: True
    out = io.StringIO()
    assert namespace["main"](["connect", str(machine.repo), "--agents", "claude-code", "--no-open"], out) == 0
    assert not (machine.threefold_home / "bin" / "threefold_install.py").exists()
    assert "no copy of the installer was kept" in out.getvalue()


def test_a_checkout_copy_with_no_source_tree_around_it_says_where_to_get_one(machine, tmp_path) -> None:
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    (lonely / "install.py").write_bytes(INSTALLER_SOURCE.read_bytes())
    module = _load(lonely / "install.py", "threefold_install_lonely")
    result = run(module, "connect", str(machine.repo), "--agents", "claude-code", "--endpoint", _closed_endpoint(), "--no-open")
    assert result.code == 2
    assert "/install.py" in result.out


def monkeypatch_open(module) -> None:
    """A served copy is a module of its own; its browser is replaced for the test that loaded it."""
    module.open_url = lambda url: True
