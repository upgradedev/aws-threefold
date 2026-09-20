"""N-gram loop detection and thrashing prevention engine."""
from __future__ import annotations

from typing import List, Tuple
from threefold.domain.models import ToolInvocation


class LoopDetector:
    """Detects runaway agent loops and ping-pong tool thrashing."""

    def __init__(self, repetition_threshold: int = 3) -> None:
        self.repetition_threshold = repetition_threshold

    def evaluate_loop_risk(
        self,
        history: List[ToolInvocation],
        next_call: ToolInvocation,
    ) -> Tuple[bool, str]:
        """Evaluates whether next_call forms an infinite loop or thrashing cycle.

        Returns (is_loop_free, failure_reason).
        """
        if not history:
            return True, "No prior history"

        next_sig = next_call.canonical_signature

        # Check 1: Monomorphic repetition (exact same tool + arguments N times)
        recent_signatures = [call.canonical_signature for call in history[-(self.repetition_threshold - 1):]]
        if len(recent_signatures) >= (self.repetition_threshold - 1):
            if all(sig == next_sig for sig in recent_signatures):
                return False, (
                    f"Monomorphic loop detected: Tool '{next_call.tool_name}' "
                    f"invoked with identical arguments {self.repetition_threshold} consecutive times"
                )

        # Check 2: Ping-pong oscillation (Pattern: A -> B -> A -> B -> [A])
        # Requires at least 4 history items: history[-4..-1] = [A, B, A, B], next = A
        if len(history) >= 4:
            sig_history = [c.canonical_signature for c in history]
            a = sig_history[-4]
            b = sig_history[-3]
            if (
                sig_history[-2] == a
                and sig_history[-1] == b
                and next_sig == a
                and a != b
            ):
                return False, (
                    f"Ping-pong oscillation loop detected: Agent oscillating between "
                    f"two alternating tool calls ({history[-2].tool_name} <-> {history[-1].tool_name})"
                )

        # Check 3: Sliding 3-gram circular loop (A -> B -> C -> A -> B -> C -> [A])
        if len(history) >= 6:
            sig_history = [c.canonical_signature for c in history]
            a = sig_history[-6]
            b = sig_history[-5]
            c = sig_history[-4]
            if (
                sig_history[-3] == a
                and sig_history[-2] == b
                and sig_history[-1] == c
                and next_sig == a
                and len({a, b, c}) == 3
            ):
                return False, (
                    f"Circular 3-step loop detected: Agent repeating 3-phase cycle "
                    f"({history[-3].tool_name} -> {history[-2].tool_name} -> {history[-1].tool_name})"
                )

        return True, "Execution flow is linear"
