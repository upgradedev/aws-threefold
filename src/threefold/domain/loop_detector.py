"""Cycle detection over tool-call signatures, and which calls are only looking."""
from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Tuple
from threefold.domain.boundary_guard import CONTENT_KEYS, READ_TOOLS, analysed, command_cwd, shell_command
from threefold.domain.models import ToolActionType, ToolInvocation
from threefold.domain.shell_writes import program_name

# Programs that only look. Waiting on CI is a loop by shape, the same `gh run
# view` every thirty seconds, and halting a developer's session for it punishes
# the agent for doing what it was told. So a repeat of one of these is recorded
# and never trips anything; what counts as one is kept narrow on purpose,
# because every name here is a name the loop gate stops refusing.
_READ_PROGRAMS = frozenset(
    (
        "ls", "cat", "head", "tail", "pwd", "sleep", "cd", "pushd", "popd",
        "grep", "egrep", "fgrep", "rg", "wc", "stat", "file", "tree", "which", "true",
        # PowerShell, which Antigravity runs on Windows.
        "dir", "type", "get-content", "gc", "get-childitem", "gci", "get-location", "start-sleep", "select-string",
    )
)
_GIT_READS = frozenset(("status", "log", "diff", "show"))
_GH_READS = {"run": frozenset(("view", "list", "watch")), "pr": frozenset(("checks", "view"))}
_GIT_GLOBAL_VALUES = frozenset(("-C", "-c", "--git-dir", "--work-tree", "--namespace"))
# find is a read until it is told to act on what it finds.
_FIND_ACTIONS = frozenset(("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"))
_WATCH_VALUES = frozenset(("-n", "--interval", "-d", "--differences", "-q", "--equexit"))


def _git_reads(argv: Sequence[str]) -> bool:
    index = 1
    while index < len(argv) and argv[index].startswith("-"):
        index += 2 if argv[index] in _GIT_GLOBAL_VALUES else 1
    if index >= len(argv) or argv[index] not in _GIT_READS:
        return False
    # `git diff --output=patch.txt` writes a file, and so does `git log --output`.
    return not any(arg == "--output" or arg.startswith("--output=") for arg in argv[index + 1:])


def _gh_reads(argv: Sequence[str]) -> bool:
    words = [arg for arg in argv[1:] if not arg.startswith("-")]
    return len(words) >= 2 and words[1] in _GH_READS.get(words[0], ())


def _polls(argv: Sequence[str]) -> bool:
    """Whether one simple command, wrappers already removed, only reads or waits."""
    if not argv:
        return False
    name = program_name(argv[0])
    if name == "git":
        return _git_reads(argv)
    if name == "gh":
        return _gh_reads(argv)
    if name == "find":
        return not any(arg in _FIND_ACTIONS for arg in argv[1:])
    if name == "watch":
        index = 1
        while index < len(argv) and argv[index].startswith("-"):
            index += 2 if argv[index] in _WATCH_VALUES else 1
        return index < len(argv) and _polls(argv[index:])
    return name in _READ_PROGRAMS


def is_read_or_poll(invocation: ToolInvocation) -> bool:
    """Whether a call only reads or waits, so repeating it is polling rather than a runaway.

    A read tool, or a call declared as a read, counts when it carries no content
    to write. A command counts when every simple command in it is one of the
    reads above and it writes nothing at all: `git diff > changes.diff` is a
    write, and a pipeline into `python` is not a read whatever came before it.
    A command too long to be read to the end is never a poll, because the part
    not read is where the write would be.
    """
    command = shell_command(invocation)
    if command is None:
        declared_read = invocation.action_type == ToolActionType.FILE_READ
        read_tool = str(invocation.tool_name or "").lower() in READ_TOOLS
        if not (declared_read or read_tool):
            return False
        arguments = invocation.arguments if isinstance(invocation.arguments, dict) else {}
        return not any(isinstance(key, str) and key.lower() in CONTENT_KEYS for key in arguments)
    analysis = analysed(command, command_cwd(invocation))
    if analysis.truncated or analysis.writes or analysis.tampering or not analysis.commands:
        return False
    return all(_polls(argv) for argv in analysis.commands)


class LoopDetector:
    """Detects runaway agent loops and ping-pong tool thrashing."""

    MIN_CYCLE_LENGTH = 2
    MAX_CYCLE_LENGTH = 25
    # The fuzzy tier trips two repeats later than the byte-exact one. Three
    # same-shape calls can still be an agent iterating; five in a row is a
    # retry storm, and the tier exists for storms, not for iteration.
    FUZZY_THRESHOLD_BUMP = 2

    def __init__(self, repetition_threshold: int = 3, max_cycle_length: int = 6) -> None:
        self.repetition_threshold = repetition_threshold
        # Cycles longer than this are not searched for. A six-step loop already
        # needs thirteen calls to be recognised at the default threshold, and
        # beyond twenty-five the window costs more history than a session
        # carries. The policy's loop_history_window lands here through the
        # evaluator, clamped into range so a wild value cannot hang the gate.
        self.max_cycle_length = max(
            self.MIN_CYCLE_LENGTH, min(int(max_cycle_length), self.MAX_CYCLE_LENGTH)
        )

    def set_max_cycle_length(self, window: int) -> int:
        """Adopts the policy's loop_history_window, clamped into range.

        Returns the bound in force, so the caller can report what the detector
        actually searches rather than what the policy asked for.
        """
        self.max_cycle_length = max(
            self.MIN_CYCLE_LENGTH, min(int(window), self.MAX_CYCLE_LENGTH)
        )
        return self.max_cycle_length

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

    def evaluate_fuzzy_loop_risk(
        self,
        history: List[ToolInvocation],
        next_call: ToolInvocation,
        fuzzy: Callable[[ToolInvocation], Optional[str]],
    ) -> Tuple[bool, str]:
        """Whether next_call repeats the shape of what came before it.

        The second tier, asked only when the byte-exact tier finds nothing.
        `fuzzy` normalizes a call to what it does — the same tool on the same
        targets — and returns None where a call has no shape to compare, which
        keeps targetless calls out of both sequences rather than lumping them
        into one false shape. A longer run is required than the exact tier —
        threshold plus FUZZY_THRESHOLD_BUMP consecutive shapes — because
        sameness here is cheaper than identity.
        """
        current = fuzzy(next_call)
        if current is None:
            return True, "Nothing to compare: the call names no targets"
        fuzzy_history = [sig for sig in (fuzzy(prior) for prior in history) if sig is not None]
        if not fuzzy_history:
            return True, "No prior history"
        sequence = fuzzy_history + [current]
        period = self._repeating_period(sequence, repeats=max(self.repetition_threshold + 1, 1))
        if period is None:
            return True, "Execution flow is dissimilar"
        if period == 1:
            runs = self.repetition_threshold + self.FUZZY_THRESHOLD_BUMP
            return False, (
                f"Similar loop detected: tool '{next_call.tool_name}' ran {runs} times "
                "on the same targets with differing arguments"
            )
        return False, (
            f"Similar {period}-step cycle detected: the same tools on the same "
            "targets with differing arguments"
        )

    def _repeating_period(self, sequence: List[str], repeats: Optional[int] = None) -> int | None:
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

        The fuzzy tier reuses this search, passing its own longer repeat
        count: sameness is the caller's normalization, cycles are still cycles.
        """
        if repeats is None:
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
