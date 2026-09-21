"""Which calls only read or wait, so that repeating them is polling rather than a loop.

An agent told to wait for CI runs the same `gh run view` every thirty seconds.
By shape that is a monomorphic loop, and on day one it halted the session of
the developer who asked for it. The loop gate now asks this function first; the
evaluator's half of that is tested with the evaluator. What is pinned here is
the line between looking and doing, which has to hold in both directions: a
poll that is not recognised halts a developer, and a write that is taken for a
poll is never stopped by the loop gate at all.
"""
from __future__ import annotations

import pytest

import threefold.domain as domain
from threefold.domain.loop_detector import is_read_or_poll
from threefold.domain.models import ToolActionType, ToolInvocation


def _command(command) -> ToolInvocation:
    return ToolInvocation(tool_name="Bash", action_type=ToolActionType.COMMAND_EXEC, arguments={"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline -5",
        "git diff HEAD~1",
        "git show HEAD:src/app.py",
        "git -C services/billing status",
        "git --no-pager log -3",
        "ls -la",
        "cat build/ci.log",
        "head -20 build/ci.log",
        "tail -f build/app.log",
        "pwd",
        "sleep 30",
        "sleep 30 && gh run view 1234",
        "gh run list --limit 5",
        "gh run watch 1234",
        "gh pr checks 42",
        "gh pr view 42 --json state",
        "watch -n 10 gh run list",
        "timeout 600 gh run watch 1234",
        "cd services/billing && git status",
        "ls src | grep acme",
        "find . -name '*.py'",
    ],
)
def test_reading_and_waiting_are_recognised(command: str) -> None:
    assert is_read_or_poll(_command(command)) is True


@pytest.mark.parametrize(
    "command",
    [
        "npm test",
        "pytest -q",
        "git push",
        "git commit -m wip",
        "git diff > build/changes.diff",
        "git diff --output=build/changes.diff",
        "cat build/ci.log | python scripts/acme_parse.py",
        "echo 'import boto3' > src/domain/x.py",
        "ls && rm -rf build",
        "find . -name '*.pyc' -delete",
        "find . -name '*.py' -exec sed -i 's/a/b/' {} +",
        "gh pr merge 42",
        "gh run rerun 1234",
        "watch -n 10 make deploy",
        "sleep 30 && git push --force",
        "",
    ],
)
def test_anything_that_acts_is_not_a_poll(command: str) -> None:
    assert is_read_or_poll(_command(command)) is False


def test_a_command_too_long_to_read_to_the_end_is_never_a_poll() -> None:
    padded = "sleep 1; X=" + "a" * 40_000 + " git status && python -c \"open('x','w')\""
    assert is_read_or_poll(_command(padded)) is False


def test_a_command_sent_as_a_list_of_words_is_read_the_same_way() -> None:
    assert is_read_or_poll(_command(["git", "status"])) is True
    assert is_read_or_poll(_command(["git", "push"])) is False


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "view_file", "list_dir", "grep_search"])
def test_the_agents_read_tools_are_reads(tool: str) -> None:
    assert is_read_or_poll(ToolInvocation(tool, ToolActionType.UNKNOWN, {"file_path": "src/app.py"})) is True


def test_a_call_declared_as_a_read_is_a_read_unless_it_carries_content() -> None:
    assert is_read_or_poll(ToolInvocation("fetch_file", ToolActionType.FILE_READ, {"path": "src/app.py"})) is True
    carrying = ToolInvocation("fetch_file", ToolActionType.FILE_READ, {"path": "src/app.py", "content": "import boto3"})
    assert is_read_or_poll(carrying) is False


def test_a_write_is_not_a_read() -> None:
    write = ToolInvocation("Write", ToolActionType.FILE_WRITE, {"file_path": "src/app.py", "content": "x = 1"})
    assert is_read_or_poll(write) is False


def test_the_evaluator_can_find_it_by_name_on_the_package_and_the_module() -> None:
    """Track B looks it up by name, so both places it could look must answer."""
    assert getattr(domain, "is_read_or_poll") is is_read_or_poll
