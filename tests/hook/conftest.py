"""Fixtures for the universal hook.

The hook reads its configuration from the environment and keeps its lists and
logs under THREEFOLD_HOME, which defaults to the owner's own ~/.threefold, and
it treats ~/.claude, ~/.codex and ~/.gemini as off limits. So every test here
runs with HOME, USERPROFILE and THREEFOLD_HOME pointed into a temporary
directory and every other THREEFOLD_* variable removed: a forgotten monkeypatch
cannot read the owner's never-send list or append to their logs.

The service is a real HTTP server on 127.0.0.1 answering from a script, so the
hook's urllib code runs as it does against AWS, and a test can say what reached
the server as well as what the hook printed.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

HOOK_PATH = Path(__file__).resolve().parents[2] / "src" / "threefold" / "hooks" / "threefold_hook.py"
AGENTS = ("claude-code", "codex", "antigravity")


def load_hook():
    """Loads the hook from its file, the way it runs: alone, not as part of the package."""
    spec = importlib.util.spec_from_file_location("threefold_hook", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["threefold_hook"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def hook():
    return load_hook()


@pytest.fixture(autouse=True)
def machine(monkeypatch, tmp_path):
    """A developer machine that exists only for this test."""
    for name in list(os.environ):
        if name.startswith("THREEFOLD_") and name != "THREEFOLD_OFFLINE":
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "work" / "acme-folder-name"
    project.mkdir(parents=True)
    # The hook walks up from the agent's cwd to the nearest .threefold.json or
    # .git. This marker stops that walk inside the test's own directory, so no
    # file above it on the real machine can configure a test.
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("THREEFOLD_HOME", str(home / ".threefold"))
    monkeypatch.setenv("THREEFOLD_PROJECT", "Acme-Payments")
    # Nothing on the way to 127.0.0.1 but the stub.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    return SimpleNamespace(home=home, threefold_home=home / ".threefold", project=project, tmp=tmp_path)


class Stub:
    """What the service will answer, and everything that reached it."""

    def __init__(self) -> None:
        self.status = 200
        self.body: Any = {"status": "APPROVED"}
        self.content_type = "application/json"
        self.delay = 0.0
        self.requests: List[Dict[str, Any]] = []
        self.port = 0

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/prod/"

    def answer(self, status: int, body: Any, content_type: str = "application/json") -> None:
        self.status, self.body, self.content_type = status, body, content_type

    def reset(self) -> None:
        self.answer(200, {"status": "APPROVED"})
        self.delay = 0.0
        self.requests = []


@pytest.fixture(scope="session")
def _stub_server():
    """One server for the whole run.

    Each test resets it and reads only what it sent itself. Standing it up per
    test cost more than every test in this directory put together: the port,
    the thread and the shutdown are the slow parts, not the requests.
    """
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - the name http.server calls
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            state.requests.append(
                {
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                    "body": json.loads(raw.decode("utf-8")),
                }
            )
            if state.delay:
                time.sleep(state.delay)
            payload = state.body if isinstance(state.body, bytes) else json.dumps(state.body).encode("utf-8")
            try:
                self.send_response(state.status)
                self.send_header("Content-Type", state.content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except OSError:
                pass  # the hook gave up waiting, which is what a timeout test wants

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state.port = server.server_address[1]
    # A short poll interval: shutdown() waits for the next poll, 0.5 s by default,
    # which across every test here added seconds to a suite that otherwise takes three.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
def stub(_stub_server, monkeypatch):
    _stub_server.reset()
    monkeypatch.setenv("THREEFOLD_ENDPOINT", _stub_server.endpoint)
    return _stub_server


@pytest.fixture
def closed_endpoint(monkeypatch):
    """An endpoint on a port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}/prod/"
    monkeypatch.setenv("THREEFOLD_ENDPOINT", endpoint)
    # Windows retries a refused connection to localhost for about two seconds
    # before reporting it; a short timeout keeps the suite fast either way.
    monkeypatch.setenv("THREEFOLD_TIMEOUT", "0.3")
    return endpoint


class Payloads:
    """Each agent's stdin, built the way that agent sends it."""

    def __init__(self, project: Path) -> None:
        self.project = project

    def write(self, agent: str, relative: str = "src/app.py", content: str = "x = 1\n") -> Dict[str, Any]:
        path = str(self.project / relative)
        if agent == "claude-code":
            return {
                "session_id": "acme-session-1",
                "transcript_path": str(self.project / "transcript.jsonl"),
                "cwd": str(self.project),
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": path, "content": content},
            }
        if agent == "codex":
            added = "\n".join("+" + line for line in content.split("\n"))
            patch = f"*** Begin Patch\n*** Add File: {relative}\n{added}\n*** End Patch\n"
            return self.codex_patch(patch)
        return self.antigravity("write_to_file", {"TargetFile": path, "CodeContent": content})

    def command(self, agent: str, command: str) -> Dict[str, Any]:
        if agent == "claude-code":
            return {
                "session_id": "acme-session-1",
                "cwd": str(self.project),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": command, "description": "run it"},
            }
        if agent == "codex":
            return {
                "session_id": "acme-codex-1",
                "cwd": str(self.project),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": command},
            }
        return self.antigravity("run_command", {"CommandLine": command, "Cwd": str(self.project)})

    def codex_patch(self, patch: str) -> Dict[str, Any]:
        return {
            "session_id": "acme-codex-1",
            "cwd": str(self.project),
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
        }

    def antigravity(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "toolCall": {"name": name, "args": args},
            "conversationId": "acme-conversation-1",
            "workspacePaths": [str(self.project)],
        }


@pytest.fixture
def payloads(machine) -> Payloads:
    return Payloads(machine.project)


@pytest.fixture
def run_hook(hook):
    """Runs main() once and returns (exit code, stdout, stderr)."""

    def run(payload: Any, argv: Optional[List[str]] = None, raw: Optional[str] = None) -> Tuple[int, str, str]:
        text = raw if raw is not None else json.dumps(payload)
        stdout, stderr = io.StringIO(), io.StringIO()
        code = hook.main(argv or [], io.StringIO(text), stdout, stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    return run


def decision_of(stdout: str) -> Optional[str]:
    """The decision a hook's stdout carries, in either agent's format, or None for silence."""
    if not stdout.strip():
        return None
    document = json.loads(stdout)
    if "hookSpecificOutput" in document:
        return document["hookSpecificOutput"]["permissionDecision"]
    return document["decision"]


def reason_of(stdout: str) -> str:
    document = json.loads(stdout)
    if "hookSpecificOutput" in document:
        return document["hookSpecificOutput"]["permissionDecisionReason"]
    return document["reason"]


@pytest.fixture
def verdict():
    """Reads what the hook printed: `verdict.decision(out)` and `verdict.reason(out)`."""
    return SimpleNamespace(decision=decision_of, reason=reason_of)


@pytest.fixture
def held_back_lines(machine):
    """The lines of held_back.log, or an empty list if the hook never wrote one."""

    def read() -> List[str]:
        log = machine.threefold_home / "held_back.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    return read
