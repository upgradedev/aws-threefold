"""Unit tests for LoopDetector."""
from __future__ import annotations

import pytest
from threefold.domain.models import ToolActionType, ToolInvocation
from threefold.domain.loop_detector import LoopDetector


def make_invocation(tool: str, args: dict) -> ToolInvocation:
    return ToolInvocation(
        tool_name=tool,
        action_type=ToolActionType.FILE_WRITE,
        arguments=args,
    )


def test_loop_detector_permits_linear_workflow():
    detector = LoopDetector(repetition_threshold=3)
    history = [
        make_invocation("view_file", {"path": "src/main.py"}),
        make_invocation("grep_search", {"query": "parse_args"}),
        make_invocation("replace_file_content", {"path": "src/main.py", "line": 10}),
    ]
    next_call = make_invocation("run_command", {"command": "pytest"})
    is_safe, _ = detector.evaluate_loop_risk(history, next_call)
    assert is_safe is True


def test_monomorphic_loop_detected():
    detector = LoopDetector(repetition_threshold=3)
    history = [
        make_invocation("edit_file", {"path": "test.py", "chunk": "foo"}),
        make_invocation("edit_file", {"path": "test.py", "chunk": "foo"}),
    ]
    # Third identical call
    next_call = make_invocation("edit_file", {"path": "test.py", "chunk": "foo"})
    is_safe, reason = detector.evaluate_loop_risk(history, next_call)
    assert is_safe is False
    assert "Monomorphic loop detected" in reason


def test_ping_pong_loop_detected():
    detector = LoopDetector(repetition_threshold=3)
    inv_a = make_invocation("edit_file", {"path": "a.py"})
    inv_b = make_invocation("test_file", {"path": "a_test.py"})

    # History: A -> B -> A -> B
    history = [inv_a, inv_b, inv_a, inv_b]

    # Next: A
    next_call = inv_a
    is_safe, reason = detector.evaluate_loop_risk(history, next_call)
    assert is_safe is False
    assert "Ping-pong oscillation loop detected" in reason


def test_circular_3_step_loop_detected():
    detector = LoopDetector(repetition_threshold=3)
    a = make_invocation("tool_a", {"step": 1})
    b = make_invocation("tool_b", {"step": 2})
    c = make_invocation("tool_c", {"step": 3})

    # History: A -> B -> C -> A -> B -> C
    history = [a, b, c, a, b, c]

    # Next: A
    next_call = a
    is_safe, reason = detector.evaluate_loop_risk(history, next_call)
    assert is_safe is False
    assert "Circular 3-step loop detected" in reason
