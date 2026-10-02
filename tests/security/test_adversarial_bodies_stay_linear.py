"""Bodies built to make a pattern backtrack are judged in milliseconds.

Three more inputs held a Lambda slot for seconds per request on the open
route, each well under the 1 MB body cap: `-eyJ` repeated (the JWT shape
restarted its scan at every `eyJ` a hyphen preceded), `git push ` repeated
(`.*` rescanned the rest of the line from every push), and an edit context of
many distinct imports (the introduced-imports check rebuilt a set per module).
The verdicts of ordinary inputs are unchanged, and an `edit_before` or
`edit_after` that is not a context the hook could have sent no longer hides
its text from the gates.
"""
from __future__ import annotations

import json
import time

from threefold.interfaces.api_handlers import lambda_handler


def _verdict(tool: str, arguments: dict, index: int) -> dict:
    event = {
        "rawPath": "/prod/evaluate-tool-call",
        "requestContext": {"http": {"method": "POST", "sourceIp": f"10.9.6.{index}"}, "stage": "prod"},
        "headers": {"content-type": "application/json"},
        "body": json.dumps({
            "session_id": f"linear-body-{index}",
            "project_name": "Acme-Ledger",
            "tool_name": tool,
            "arguments": arguments,
            "agent": "claude-code",
            "origin": "hook",
            "explain": False,
        }),
    }
    return json.loads(lambda_handler(event, None)["body"])


def _timed(tool: str, arguments: dict, index: int) -> dict:
    started = time.monotonic()
    verdict = _verdict(tool, arguments, index)
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"took {elapsed:.1f}s"
    return verdict


def test_a_hyphenated_chain_of_jwt_heads_is_judged_quickly() -> None:
    verdict = _timed("search", {"q": "-eyJ" * 200_000}, 1)
    assert verdict["status"] == "APPROVED", verdict.get("reason")


def test_a_line_of_repeated_pushes_is_judged_quickly() -> None:
    verdict = _timed("search", {"q": "git push " * 90_000}, 2)
    assert verdict["status"] == "APPROVED", verdict.get("reason")


def test_an_edit_context_of_many_imports_is_judged_quickly() -> None:
    imports = "".join(f"import m{index}\n" for index in range(9000))
    arguments = {
        "file_path": "src/acme/domain/order.py",
        "content": "x",
        "edit_before": imports[:65536],
        "edit_after": imports[:131072],
    }
    verdict = _timed("Edit", arguments, 3)
    assert verdict["status"] == "APPROVED", verdict.get("reason")


def test_a_real_jwt_and_a_forced_push_are_still_refused() -> None:
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpM"
    assert _verdict("search", {"q": f"Authorization: Bearer {token}"}, 4)["status"] == "BLOCKED_SECRET_DETECTED"
    pushed = _verdict("Bash", {"command": "git push origin main && git push -f origin main"}, 5)
    assert pushed["status"].startswith("BLOCKED"), pushed.get("reason")
    assert _verdict("Bash", {"command": "git push origin main"}, 6)["status"] == "APPROVED"


def test_the_context_keys_hide_nothing_outside_an_edit() -> None:
    hidden = _verdict("mcp__acme__do", {"edit_after": "AKIAQWERTYUIOPASDFGH"}, 7)
    assert hidden["status"] == "BLOCKED_SECRET_DETECTED", hidden.get("reason")


def test_an_edit_beside_an_existing_key_is_still_judged_on_what_it_writes() -> None:
    arguments = {
        "file_path": "src/acme/app/settings.py",
        "content": "X = 2",
        "edit_before": "KEY = 'AKIAQWERTYUIOPASDFGH'\nX = 1\n",
        "edit_after": "KEY = 'AKIAQWERTYUIOPASDFGH'\nX = 2\n",
    }
    verdict = _verdict("Edit", arguments, 8)
    assert verdict["status"] == "APPROVED", verdict.get("reason")
