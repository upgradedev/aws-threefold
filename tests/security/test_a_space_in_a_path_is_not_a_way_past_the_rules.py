"""A file name with a space in it is still a file name.

`looks_like_path` is a shape heuristic: it answers whether a loose string in a
call is plausibly a path, and it says no to anything containing a space, so
prose is never judged as a file. That heuristic was also applied to the value
under an explicit path key (`file_path`, `path`, `notebook_path`, ...), where
there is nothing to guess: the agent said this is the file. A governed write to
`src/domain/order service.py` therefore paired with no content, and every
layering and governance check below it was skipped.

The same content sent as a heredoc to the same quoted path was refused, because
the shell reader resolves the path itself, so the gap was specific to the file
tool route.
"""
from __future__ import annotations

import pytest

from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator
from threefold.domain.boundary_guard import (
    ArchitecturalBoundaryGuard,
    describe_target,
    looks_like_path,
    write_pairs,
)
from threefold.domain.layering_rules import DEFAULT_RULES
from threefold.domain.models import ToolActionType, ToolInvocation
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository

FORBIDDEN = "import boto3\n"


def _evaluate(arguments: dict, tool: str = "Write", action: ToolActionType = ToolActionType.FILE_WRITE):
    invocation = ToolInvocation(tool_name=tool, action_type=action, arguments=arguments)
    return ArchitecturalBoundaryGuard.evaluate_tool_boundary(invocation, rules=DEFAULT_RULES)


SPACED_WRITES = [
    ({"file_path": "src/domain/order service.py", "content": FORBIDDEN}, "Write"),
    ({"file_path": "src/domain/order models/order.py", "content": FORBIDDEN}, "Write"),
    ({"path": "src/domain/order service.py", "content": FORBIDDEN}, "Write"),
    ({"target_file": "src/domain/order service.py", "new_source": FORBIDDEN}, "NotebookEdit"),
    (
        {"file_path": "src/domain/order service.py", "old_string": "x", "new_string": FORBIDDEN},
        "Edit",
    ),
    (
        {"edits": [{"file_path": "src/domain/a b.py", "new_string": FORBIDDEN}]},
        "MultiEdit",
    ),
]


@pytest.mark.parametrize("arguments, tool", SPACED_WRITES)
def test_a_layering_rule_reads_a_path_with_a_space(arguments: dict, tool: str) -> None:
    allowed, reason = _evaluate(arguments, tool)
    assert allowed is False, f"{tool} {arguments} was approved"
    assert "python-domain-stays-pure" in reason


def test_a_spaced_directory_does_not_hide_the_hooks_own_settings() -> None:
    allowed, reason = _evaluate({"file_path": "my project/.claude/settings.json", "content": "{}"})
    assert allowed is False
    assert "architectural governance" in reason


def test_a_spaced_directory_does_not_hide_a_credential_store() -> None:
    """And the refusal names the path, not the command it was mistaken for.

    Before the path key was trusted, this was refused — but by the command scan
    below it, which called the write "Command 'my project/.env'". A test that
    only looked for the word "protected" in the sentence passed either way and
    pinned nothing.
    """
    allowed, reason = _evaluate({"file_path": "my project/.env", "content": "TOKEN=1\n"})
    assert allowed is False
    assert "Target path 'my project/.env' is protected" in reason


def test_a_clean_write_to_a_spaced_path_is_still_approved() -> None:
    """The narrowing must not turn every spaced file name into a refusal."""
    allowed, reason = _evaluate({"file_path": "src/domain/order service.py", "content": "VALUE = 1\n"})
    assert allowed is True, reason


def test_the_shape_heuristic_itself_is_unchanged() -> None:
    """Loose prose is still not read as a path; only an explicit path key is trusted."""
    assert looks_like_path("src/domain/order service.py") is False
    assert looks_like_path("src/domain/order_service.py") is True


def test_the_pairing_finds_the_spaced_path() -> None:
    assert write_pairs({"file_path": "src/domain/order service.py", "content": FORBIDDEN}) == [
        ("src/domain/order service.py", FORBIDDEN)
    ]


SPACED_TARGETS = [
    ({"file_path": "src/domain/order line.py", "content": FORBIDDEN}, "src/domain/order line.py"),
    ({"file_path": "my project/.claude/settings.json", "content": "{}"}, "my project/.claude/settings.json"),
    ({"notebook_path": "notebooks/a b.ipynb"}, "notebooks/a b.ipynb"),
    ({"edits": [{"file_path": "src/domain/a b.py", "new_string": FORBIDDEN}]}, "src/domain/a b.py"),
]


@pytest.mark.parametrize("arguments, expected", SPACED_TARGETS)
def test_the_ledger_descriptor_names_a_spaced_path(arguments: dict, expected: str) -> None:
    """What the ledger says a call was aimed at reads the path key too.

    `describe_target` was left on the shape heuristic while every other reader
    of a path key moved off it, so the row for exactly the write a space used
    to hide recorded an empty target.
    """
    request = ToolInvocation(
        tool_name="Write", action_type=ToolActionType.FILE_WRITE, arguments=arguments
    )
    assert describe_target(request) == expected


def test_the_recorded_refusal_says_what_the_call_was_aimed_at() -> None:
    """End to end: /api/decisions should not say a rule was broken and nothing else."""
    repo = DynamoDBSessionRepository(table_name="spaced-target-test")
    evaluator = GovernanceEvaluator(session_repo=repo)
    request = ToolCallRequestDTO(
        session_id="spaced-target-1",
        developer_id="anonymous",
        project_name="Acme-Spaced",
        tool_name="Write",
        action_type="FILE_WRITE",
        arguments={"file_path": "src/domain/order line.py", "content": FORBIDDEN},
        agent="claude-code",
        origin="hook",
    )
    verdict = evaluator.evaluate_tool_call(request)
    assert verdict.status == "BLOCKED_BOUNDARY_VIOLATION"

    rows = [row for row in repo.list_decisions(days=1) if row.get("session_id") == "spaced-target-1"]
    assert rows, "the refusal should have been recorded"
    assert rows[0]["target"] == "src/domain/order line.py"


def test_the_heredoc_route_was_never_open_and_still_is_not() -> None:
    allowed, reason = _evaluate(
        {"command": "cat > 'src/domain/order service.py' <<'EOF'\nimport boto3\nEOF"},
        tool="Bash",
        action=ToolActionType.COMMAND_EXEC,
    )
    assert allowed is False, reason


PADDED = [
    "src/domain/order.py\n",
    "src/domain/order.py ",
    " src/domain/order.py",
    "\tsrc/domain/order.py\n",
]


@pytest.mark.parametrize("path", PADDED)
def test_whitespace_around_a_path_is_not_a_way_past_the_rules(path: str) -> None:
    """The same gap as a space in the name, with the space at the end instead.

    A newline and a trailing space are both legal in a POSIX file name, and
    both left every check below saying nothing: the glob covered
    `src/domain/order.py\n`, but the suffix it was read for did not, so no
    language was found, no import was read and the rule never spoke.
    """
    allowed, reason = _evaluate({"file_path": path, "content": FORBIDDEN})
    assert allowed is False, f"{path!r} was approved"
    assert "python-domain-stays-pure" in reason


def test_a_padded_credential_store_is_named_as_itself() -> None:
    allowed, reason = _evaluate({"file_path": ".env\n", "content": "TOKEN=1\n"})
    assert allowed is False
    assert "Target path '.env' is protected" in reason


def test_a_padded_path_is_recorded_as_the_file_it_names() -> None:
    request = ToolInvocation(
        tool_name="Write",
        action_type=ToolActionType.FILE_WRITE,
        arguments={"file_path": "src/domain/order line.py\n", "content": FORBIDDEN},
    )
    assert describe_target(request) == "src/domain/order line.py"
