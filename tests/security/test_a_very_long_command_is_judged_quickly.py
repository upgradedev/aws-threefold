"""A very long command is judged in milliseconds, not burned into a timeout.

`rm` followed by thousands of flags used to take 14 seconds against the
destructive-command pattern, whose nested quantifiers backtracked over every
possible split of the flags. One anonymous request could therefore hold a
Lambda slot to its timeout, and a handful could hold all of them. The match
is a single token walk now; the verdicts are unchanged.
"""
from __future__ import annotations

import json
import time

from threefold.interfaces.api_handlers import lambda_handler


def _verdict(command: str, index: int) -> dict:
    event = {
        "rawPath": "/prod/evaluate-tool-call",
        "requestContext": {"http": {"method": "POST", "sourceIp": f"10.9.7.{index}"}, "stage": "prod"},
        "headers": {"content-type": "application/json"},
        "body": json.dumps({
            "session_id": f"long-cmd-{index}",
            "project_name": "Acme-Ledger",
            "tool_name": "Bash",
            "action_type": "COMMAND_EXEC",
            "arguments": {"command": command},
            "agent": "claude-code",
            "origin": "hook",
            "explain": False,
        }),
    }
    return json.loads(lambda_handler(event, None)["body"])


def test_nine_thousand_flags_with_no_git_are_judged_quickly() -> None:
    command = "rm " + "-r " * 9000
    assert len(command) > 27000
    started = time.monotonic()
    verdict = _verdict(command, 1)
    elapsed = time.monotonic() - started
    assert verdict["status"] == "APPROVED", verdict.get("reason")
    assert elapsed < 5, f"took {elapsed:.1f}s"


def test_a_git_far_behind_thousands_of_flags_is_still_caught_quickly() -> None:
    command = "rm " + "-r " * 2000 + ".git"
    started = time.monotonic()
    verdict = _verdict(command, 2)
    elapsed = time.monotonic() - started
    assert verdict["status"].startswith("BLOCKED"), command[-20:]
    assert elapsed < 5, f"took {elapsed:.1f}s"


def test_flags_that_spell_rm_do_not_restart_the_walk() -> None:
    """Each `rm` occurrence used to walk the flags anew, so `--rm` six
    thousand times cost six thousand walks: quadratic, back at a timeout.
    Attempts share what the walk learned, so the run costs one walk. Six
    thousand stay under the segment cap, with no `.git` for the governance
    rule to fire on, so the verdict is the matcher's."""
    command = "rm " + "--rm " * 6000
    assert len(command) < 32768
    started = time.monotonic()
    verdict = _verdict(command, 3)
    elapsed = time.monotonic() - started
    assert verdict["status"] == "APPROVED", verdict.get("reason")
    assert elapsed < 5, f"took {elapsed:.1f}s"
