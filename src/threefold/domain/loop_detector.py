"""Cycle detection over tool-call signatures."""
from __future__ import annotations

from typing import List, Tuple
from threefold.domain.models import ToolInvocation


class LoopDetector:
    """Detects runaway agent loops and ping-pong tool thrashing."""

    def __init__(self, repetition_threshold: int = 3, max_cycle_length: int = 6) -> None:
        self.repetition_threshold = repetition_threshold
        # Cycles longer than this are not searched for. A six-step loop already
        # needs thirteen calls to be recognised at the default threshold, and
        # beyond that the window costs more history than a session carries.
        self.max_cycle_length = max_cycle_length

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
        sequence = [call.canonical_signature for call in history] + [next_sig]

        period = self._repeating_period(sequence)
        if period is None:
            return True, "Execution flow is linear"

        if period == 1:
            return False, (
                f"Monomorphic loop detected: Tool '{next_call.tool_name}' "
                f"invoked with identical arguments {self.repetition_threshold} consecutive times"
            )

        cycle = [call.tool_name for call in history[-period:]]
        shape = "Ping-pong oscillation loop" if period == 2 else f"Circular {period}-step loop"
        return False, (
            f"{shape} detected: Agent repeating the cycle "
            f"({' -> '.join(cycle)}) for the {self.repetition_threshold}rd time"
        )

    def _repeating_period(self, sequence: List[str]) -> int | None:
        """Finds the shortest cycle the sequence has just closed, if any.

        Three hardcoded shapes used to be checked here: the same call repeated,
        two calls alternating, and a three-step cycle. That left gaps between
        them. An agent looping A, A, B forever matched none of the three and was
        approved indefinitely, which is precisely the runaway this product is
        named after.

        This looks for any period instead. A cycle of length p counts once the
        sequence shows it `repetition_threshold - 1` times over and then begins
        it again, which for the default threshold of three means the third
        occurrence of the first call in the cycle.
        """
        repeats = max(self.repetition_threshold - 1, 1)
        for period in range(1, self.max_cycle_length + 1):
            window_length = period * repeats + 1
            if len(sequence) < window_length:
                break
            window = sequence[-window_length:]
            if all(window[i] == window[i % period] for i in range(window_length)):
                # A period that is a multiple of a shorter one is reported by
                # the shorter one first, because the search runs shortest first.
                return period
        return None
