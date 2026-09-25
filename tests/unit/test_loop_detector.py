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


def test_cycle_window_clamps_into_range():
    assert LoopDetector().max_cycle_length == 6
    assert LoopDetector(max_cycle_length=1).max_cycle_length == 2
    assert LoopDetector(max_cycle_length=10**9).max_cycle_length == 25
    detector = LoopDetector()
    assert detector.set_max_cycle_length(3) == 3
    assert detector.max_cycle_length == 3
    assert detector.set_max_cycle_length(10**9) == 25


def test_a_wider_window_finds_a_longer_cycle():
    tools = [make_invocation(f"tool_{i}", {"step": i}) for i in range(8)]
    history = tools + tools
    narrow = LoopDetector(max_cycle_length=6)
    is_safe, _ = narrow.evaluate_loop_risk(history, tools[0])
    assert is_safe is True
    wide = LoopDetector(max_cycle_length=8)
    is_safe, reason = wide.evaluate_loop_risk(history, tools[0])
    assert is_safe is False
    assert "Circular 8-step loop detected" in reason


def _shape(invocation):
    return "SAME"


def test_fuzzy_tier_trips_two_repeats_later_than_exact():
    detector = LoopDetector()
    history = [make_invocation("edit", {"n": i}) for i in range(3)]
    is_safe, _ = detector.evaluate_fuzzy_loop_risk(history, make_invocation("edit", {"n": 99}), _shape)
    assert is_safe is True, "Four same-shape calls can still be an agent iterating"
    history.append(make_invocation("edit", {"n": 100}))
    is_safe, reason = detector.evaluate_fuzzy_loop_risk(history, make_invocation("edit", {"n": 101}), _shape)
    assert is_safe is False
    assert "Similar loop detected" in reason
    assert "differing arguments" in reason


def test_fuzzy_tier_skips_calls_with_no_shape():
    detector = LoopDetector()
    history = [make_invocation("search", {"q": i}) for i in range(9)]
    is_safe, reason = detector.evaluate_fuzzy_loop_risk(history, make_invocation("search", {"q": 10}), lambda inv: None)
    assert is_safe is True
    assert "no targets" in reason


def test_fuzzy_tier_finds_a_similar_cycle():
    detector = LoopDetector()
    read = make_invocation("read", {"path": "a.txt"})
    write = make_invocation("write", {"path": "b.txt"})
    history = [read, write] * 4
    shapes = {"read": "R", "write": "W"}
    is_safe, reason = detector.evaluate_fuzzy_loop_risk(
        history, make_invocation("read", {"path": "c.txt"}), lambda inv: shapes[inv.tool_name]
    )
    assert is_safe is False
    assert "Similar 2-step cycle detected" in reason
