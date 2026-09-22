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

from threefold.domain.boundary_guard import ArchitecturalBoundaryGuard, looks_like_path, write_pairs
from threefold.domain.layering_rules import DEFAULT_RULES
from threefold.domain.models import ToolActionType, ToolInvocation

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


def test_the_heredoc_route_was_never_open_and_still_is_not() -> None:
    allowed, reason = _evaluate(
        {"command": "cat > 'src/domain/order service.py' <<'EOF'\nimport boto3\nEOF"},
        tool="Bash",
        action=ToolActionType.COMMAND_EXEC,
    )
    assert allowed is False, reason
