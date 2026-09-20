"""The detector used to know three shapes and miss everything between them.

Monomorphic repetition, two calls alternating, and a three-step cycle were each
hardcoded. An agent looping A, A, B matched none of them and was approved
fifteen calls deep in a live probe, which is exactly the runaway the product is
named after. These tests pin the general behaviour.
"""
from __future__ import annotations

import pytest

from threefold.domain.loop_detector import LoopDetector
from threefold.domain.models import ToolActionType, ToolInvocation


def _call(name: str) -> ToolInvocation:
    return ToolInvocation(
        tool_name=f"tool_{name}",
        action_type=ToolActionType.FILE_READ,
        arguments={"step": name},
    )


def _run(pattern: str):
    """Feeds a pattern one call at a time and returns the index that was refused."""
    detector = LoopDetector()
    history: list[ToolInvocation] = []
    for index, letter in enumerate(pattern):
        candidate = _call(letter)
        allowed, reason = detector.evaluate_loop_risk(history, candidate)
        if not allowed:
            return index, reason
        history.append(candidate)
    return None, "Execution flow is linear"


def test_the_cycle_that_escaped_every_hardcoded_shape() -> None:
    """A, A, B repeated. Approved fifteen calls deep before this change."""
    index, reason = _run("AABAABA")
    assert index is not None, "An A,A,B cycle must be recognised"
    assert "3-step" in reason


def test_the_same_call_three_times_still_trips() -> None:
    index, reason = _run("AAA")
    assert index == 2
    assert "Monomorphic" in reason


def test_two_calls_alternating_still_trips() -> None:
    index, reason = _run("ABABA")
    assert index == 4
    assert "Ping-pong" in reason


def test_a_three_step_cycle_still_trips() -> None:
    index, reason = _run("ABCABCA")
    assert index == 6
    assert "3-step" in reason


@pytest.mark.parametrize("pattern", ["ABCDABCDA", "AABBAABBA"])
def test_longer_cycles_are_recognised(pattern: str) -> None:
    index, _ = _run(pattern)
    assert index is not None, f"{pattern} is a cycle and should be refused"


@pytest.mark.parametrize("pattern", ["ABCDEFG", "AABBCC", "ABACAD"])
def test_work_that_is_not_a_cycle_is_left_alone(pattern: str) -> None:
    """False trips are what make a guard get switched off."""
    index, reason = _run(pattern)
    assert index is None, f"{pattern} was refused at {index}: {reason}"


def test_the_threshold_is_configurable() -> None:
    detector = LoopDetector(repetition_threshold=2)
    history = [_call("A")]
    allowed, _ = detector.evaluate_loop_risk(history, _call("A"))
    assert allowed is False, "At a threshold of two, the second identical call closes the loop"
