"""The walkthrough's last call is decided by the project's stage, not by the demo's rules.

A review found the call sent in a `sim-` session. Those always enforce whatever
the stage is, so the visitor would have seen the same refusal without promoting
anything, and the walkthrough would have proved nothing about the rollout. The
page must send it in a session the stage decides, and the service must then
observe that call before a promotion and refuse it after one.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from threefold.application.projects import SIMULATED_SESSION_PREFIX
from threefold.interfaces.api_handlers import lambda_handler

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "src" / "threefold" / "web" / "dashboard.html"

# Every document that describes the walkthrough's last call.
DOCUMENTS = ("README.md", "docs/ARCHITECTURE.md", "docs/VIDEO_SCRIPT.md")


def _sent_prefixes() -> list[str]:
    page = DASHBOARD.read_text(encoding="utf-8")
    block = page[page.index("'try-send': () =>"):]
    block = block[: block.index("}).then(")]
    return re.findall(r"newSessionId\('([^']*)'\)", block)


def test_the_page_sends_the_last_call_in_a_session_the_stage_decides() -> None:
    prefixes = _sent_prefixes()
    assert prefixes, "the walkthrough's last call names no session"
    assert all(not prefix.startswith(SIMULATED_SESSION_PREFIX) for prefix in prefixes), prefixes


def test_the_documents_describe_the_session_the_page_actually_sends() -> None:
    """The fix changed the page; the documents kept describing the old one.

    A reader of README, ARCHITECTURE or the video script was told the refusal
    at step 5 happens whatever the stage, which is now false and undersells the
    only thing the walkthrough is there to show.
    """
    prefixes = _sent_prefixes()
    assert len(prefixes) == 1, prefixes
    prefix = prefixes[0]

    stale = (
        "last call is sent in a `sim-` session",
        "sends its call in a\n`sim-` session",
        "This call runs in a `sim-` demo session",
        f"newSessionId('{SIMULATED_SESSION_PREFIX}')",
        "The walkthrough's last call is always enforced",
    )
    for name in DOCUMENTS:
        body = (ROOT / name).read_text(encoding="utf-8")
        for phrase in stale:
            assert phrase not in body, f"{name} still describes the walkthrough's old session: {phrase!r}"
        assert f"`{prefix}` session" in body, f"{name} does not name the `{prefix}` session the page sends"


def _call(path: str, body: dict, index: int) -> tuple:
    event = {
        "rawPath": f"/prod{path}",
        "requestContext": {"http": {"method": "POST", "sourceIp": f"10.7.7.{index}"}, "stage": "prod"},
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
    response = lambda_handler(event, None)
    return response["statusCode"], json.loads(response["body"] or "{}")


def test_a_try_session_is_observed_before_the_promotion_and_refused_after(monkeypatch) -> None:
    monkeypatch.delenv("DEFAULT_HOOK_STAGE", raising=False)
    status, sandbox = _call("/api/sandbox", {}, 1)
    assert status == 200
    project = sandbox["project"]
    call = {
        "session_id": "try-walkthrough-1", "project_name": project, "developer": "anonymous",
        "tool_name": "Write", "action_type": "FILE_WRITE", "agent": "claude-code", "origin": "hook",
        "explain": False, "hook_mode": "managed",
        "arguments": {"file_path": "src/web/domain/cart.ts", "content": "import client from 'axios';\n"},
    }
    _, before = _call("/evaluate-tool-call", call, 2)
    assert before["status"] == "APPROVED" and before["project_stage"] == "observe"
    status, _ = _call(f"/api/projects/{project}/promote", {"enforce": ["web-domain-stays-pure"]}, 3)
    assert status == 200
    _, after = _call("/evaluate-tool-call", dict(call, session_id="try-walkthrough-2"), 4)
    assert after["status"] == "BLOCKED_BOUNDARY_VIOLATION" and after["project_stage"] == "enforce"
