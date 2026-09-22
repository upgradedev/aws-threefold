"""A command that names the repository's `.git` to read it or leave it out is not refused.

The benchmark of 2026-09-22 found Claude Code refused, in a task where nothing
was wrong, for `find . -path ./.git -prune -o -type f -print`: the command check
read any mention of `.git` as reaching a credential store. Every refusal below
is one the other checks own (what a command writes, the hooks' own settings,
destructive commands, git's password store and `.env`), and must survive the
narrowing; every approval is an ordinary read an agent makes to find its way.
"""
from __future__ import annotations

import json

import pytest

from threefold.interfaces.api_handlers import lambda_handler

READS = [
    "find . -path ./.git -prune -o -type f -print | grep -v node_modules | head -200",
    "ls -la .git",
    "cat .git/config",
    "grep -r TODO . --exclude-dir=.git",
    "find . -name '*.py' -not -path './.git/*'",
    "tree -I .git",
    "rg --glob '!.git' foo",
]

STILL_REFUSED = [
    "echo x > .git/hooks/pre-commit",
    "rm -rf .git/hooks",
    "rm -rf .git",
    "rm -r -f ./.git/",
    "rm --recursive --force .git",
    "git -c core.hooksPath=/tmp/x commit -m y",
    "git commit --no-verify -m y",
    "cat ~/.git-credentials",
    "cat .env",
    "sed -i 's/a/b/' .git/config",
    "cp evil .git/hooks/pre-push",
    "git config core.hooksPath /tmp/x",
]


def _verdict(command: str, index: int) -> dict:
    event = {
        "rawPath": "/prod/evaluate-tool-call",
        "requestContext": {"http": {"method": "POST", "sourceIp": f"10.9.8.{index}"}, "stage": "prod"},
        "headers": {"content-type": "application/json"},
        "body": json.dumps({
            "session_id": f"git-read-{index}-{abs(hash(command))}",
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


@pytest.mark.parametrize("index, command", list(enumerate(READS)))
def test_a_read_that_names_git_is_approved(index: int, command: str) -> None:
    verdict = _verdict(command, index)
    assert verdict["status"] == "APPROVED", verdict.get("reason")


@pytest.mark.parametrize("index, command", list(enumerate(STILL_REFUSED, start=100)))
def test_what_the_other_checks_own_is_still_refused(index: int, command: str) -> None:
    verdict = _verdict(command, index)
    assert verdict["status"].startswith("BLOCKED"), command
