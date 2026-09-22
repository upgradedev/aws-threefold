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

DASHBOARD = Path(__file__).resolve().parents[2] / "src" / "threefold" / "web" / "dashboard.html"


def test_the_page_sends_the_last_call_in_a_session_the_stage_decides() -> None:
    page = DASHBOARD.read_text(encoding="utf-8")
    block = page[page.index("'try-send': () =>"):]
    block = block[: block.index("}).then(")]
    prefixes = re.findall(r"newSessionId\('([^']*)'\)", block)
    assert prefixes, "the walkthrough's last call names no session"
    assert all(not prefix.startswith(SIMULATED_SESSION_PREFIX) for prefix in prefixes), prefixes


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
