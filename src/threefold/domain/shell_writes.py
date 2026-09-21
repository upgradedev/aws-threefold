"""What a shell command writes, read the way the shell would run it.

A `Write` names its file and carries its content, so the layering rules can read
it. A `Bash` call can make the same write in a dozen other spellings: a
redirection, a heredoc, `tee`, `sed -i`, `cp`, `git apply`, or
`python -c "open(...).write(...)"`. An audit on 2026-09-21 wrote `import boto3`
into a domain file with `cat > src/domain/acme_user.py <<'EOF'` and the gate let
it through, because commands were only ever read for the paths they named, never
for what they wrote.

This module reads a command for its writes and nothing else. Each write is a
target and either the content being written or UNKNOWN.

The invariant everything else rests on: UNKNOWN means "a write route this module
recognised, whose content it cannot read", such as a `cp` from a file or a
redirect of a program's output. It never means "a command this module did not
understand". A command whose shape is not recognised yields no writes at all.
Refusing what could not be parsed would refuse `make`, `npm test` and every tool
nobody has taught it, and a guard that refuses ordinary work is a guard that
gets uninstalled. The price is paid knowingly: `python script.py` can write
anything, and nothing here pretends to see inside it.

Everything is bounded, because commands arrive from an agent and the gate sits
in front of every call. The scan jumps between shell metacharacters with one
compiled pattern, heredoc bodies are consumed by a line search without being
tokenised, shlex only ever sees capped segments, and nested scripts stop at a
fixed depth. When a cap is reached the analysis stops and says so in
`truncated`, so the caller can refuse what it could not finish reading rather
than approve the part it did.
"""
from __future__ import annotations

import ast
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

UNKNOWN: Optional[str] = None

MAX_COMMAND_CHARS = 1_100_000
MAX_OPERATORS = 20_000
MAX_SEGMENTS = 2_000
MAX_SEGMENT_CHARS = 32_768
MAX_TOKENISED_CHARS = 131_072
MAX_TOKENS = 20_000
MAX_FRAMES = 32
MAX_HEREDOCS = 64
# More files than any command an agent runs by hand writes. Past it the
# analysis stops, because judging twenty thousand patch entries one by one is
# how a single request would hold the gate for seconds.
MAX_WRITES = 1_000
MAX_NESTED_SCRIPTS = 3
MAX_CODE_CHARS = 200_000
PYTHON_PARSE_LIMIT = 100_000

# What a `$(...)`, a backtick or a `( ... )` group leaves behind in the command
# around it. The inner command is read on its own; the outer one sees a word it
# cannot know, which is what a substitution is until it runs.
PLACEHOLDER = "__threefold_substitution__"
_UNKNOWN_PART = "__threefold_unknown__"

# Characters inside quotes that shlex must not read as operators. `echo '>' x`
# is an echo of a greater-than sign, and without this a quoted `>` became a
# redirect to whatever followed it. They are swapped for private-use code points
# before tokenising and swapped back in every token after.
_PROTECT = str.maketrans({"<": "\ue000", ">": "\ue001", "&": "\ue002", "|": "\ue003"})
_UNPROTECT = str.maketrans({"\ue000": "<", "\ue001": ">", "\ue002": "&", "\ue003": "|"})


@dataclass(frozen=True)
class ShellWrite:
    """One write a command makes.

    `target` is None only for a write whose targets cannot be read at all, such
    as `git apply fix.patch`, where the patch lives on a machine the service
    cannot see. `content` is UNKNOWN (None) when the route is recognised but
    what it writes is not in the command. `deletes` marks a path removed or
    moved away rather than written.
    """

    target: Optional[str]
    content: Optional[str]
    route: str
    deletes: bool = False


@dataclass
class ShellAnalysis:
    """Everything read out of one command."""

    writes: List[ShellWrite] = field(default_factory=list)
    # Reasons, one per way the command turns off the hooks that govern the
    # repository: `git commit --no-verify`, `git -c core.hooksPath=...`.
    tampering: List[str] = field(default_factory=list)
    # The words of each simple command after wrappers such as `sudo` and `env`
    # are removed, in the order they run. The loop gate reads these to tell a
    # poll from work.
    commands: List[Tuple[str, ...]] = field(default_factory=list)
    truncated: bool = False


# --- the paths that govern the repository --------------------------------------

# Files that decide whether the hooks run at all. A write to any of them, by any
# tool, is how an agent would turn its own governance off, so each is refused
# as a protected path wherever the layering rules would have said nothing.
_GOVERNANCE_PAIRS = frozenset(
    (
        (".claude", "settings.json"),
        (".claude", "settings.local.json"),
        (".codex", "hooks.json"),
        (".codex", "config.toml"),
        (".agents", "hooks.json"),
        # Where `git config core.hooksPath` is kept, written directly.
        (".git", "config"),
    )
)
_GOVERNANCE_FILES = frozenset((".threefold.json",))
# Removing one of these directories removes the files above with it.
_GOVERNANCE_DIRECTORIES = frozenset((".claude", ".codex", ".agents", ".git", ".threefold"))


def _segments_of(path: str) -> List[str]:
    return [part for part in path.replace("\\", "/").split("/") if part and part != "."]


def is_governance_path(path: str, deletes: bool = False) -> bool:
    """Whether writing (or, with `deletes`, removing) this path touches the repository's own hooks.

    Matched by the trailing segments, not from the root, because the service is
    sent paths relative to wherever the agent happened to start and a nested
    checkout has its own settings. `.threefold/` is included beside the listed
    files: it holds the rules the pre-commit check falls back to, and rewriting
    them is the same act as rewriting the hook settings. Case is ignored, since
    two of the three agents run on file systems that ignore it too.
    """
    if not path:
        return False
    parts = [part.lower() for part in _segments_of(path)]
    if not parts:
        return False
    if parts[-1] in _GOVERNANCE_FILES:
        return True
    if len(parts) >= 2 and (parts[-2], parts[-1]) in _GOVERNANCE_PAIRS:
        return True
    for index in range(len(parts) - 1):
        if parts[index] == ".git" and parts[index + 1] == "hooks":
            return True
    if ".threefold" in parts[:-1] or parts[-1] == ".threefold":
        return True
    return deletes and parts[-1] in _GOVERNANCE_DIRECTORIES


# --- scanning: separators, groups and heredocs ----------------------------------

class _Segment:
    """One simple command's text, with the heredoc bodies it reads."""

    __slots__ = ("parts", "text", "heredocs", "piped")

    def __init__(self, piped: bool) -> None:
        self.parts: List[str] = []
        self.text = ""
        self.heredocs: List[Optional[str]] = []
        self.piped = piped


class _Frame:
    __slots__ = ("kind", "segment", "quoted")

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.segment = _Segment(False)
        self.quoted = False


_OPEN = "open"
_CLOSE = "close"

_COMMAND_SPECIAL = re.compile(r"[\\'\"`$()|&;<>\n#]")
_QUOTED_SPECIAL = re.compile(r"[\\\"`$]")
_WINDOWS_SEPARATOR_BEFORE = re.compile(r"[A-Za-z0-9_.:~\-]")
_WINDOWS_SEPARATOR_AFTER = re.compile(r"[A-Za-z0-9_.\-]")
_ANSI_C_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "a": "\a", "b": "\b", "e": "\x1b", "f": "\f", "v": "\v"}


class _Budget:
    """Shared by every level of one analysis, so nesting cannot multiply the work."""

    __slots__ = ("operators", "segments", "chars", "tokens", "truncated")

    def __init__(self) -> None:
        self.operators = 0
        self.segments = 0
        self.chars = 0
        self.tokens = 0
        self.truncated = False


def _last_char(parts: List[str]) -> str:
    for part in reversed(parts):
        if part:
            return part[-1]
    return ""


def _matching(text: str, start: int, opener: str, closer: str, depth: int) -> int:
    """The index just past the closer that brings `depth` to zero, or the end of the text."""
    index = start
    length = len(text)
    while index < length and depth > 0:
        char = text[index]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
        index += 1
    return index


def _ansi_c(text: str, start: int) -> Tuple[str, int]:
    """Reads `$'...'` from the quote at `start`. Returns (decoded text, index after it)."""
    out: List[str] = []
    index = start + 1
    length = len(text)
    while index < length:
        char = text[index]
        if char == "'":
            return "".join(out), index + 1
        if char == "\\" and index + 1 < length:
            code = text[index + 1]
            if code in _ANSI_C_ESCAPES:
                out.append(_ANSI_C_ESCAPES[code])
                index += 2
                continue
            if code == "x":
                digits = re.match(r"[0-9A-Fa-f]{1,2}", text[index + 2:index + 4])
                if digits:
                    out.append(chr(int(digits.group(0), 16)))
                    index += 2 + len(digits.group(0))
                    continue
            if code in "01234567":
                digits = re.match(r"[0-7]{1,3}", text[index + 1:index + 4])
                out.append(chr(int(digits.group(0), 8)))
                index += 1 + len(digits.group(0))
                continue
            out.append(code)
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out), length


def _heredoc_opener(text: str, start: int) -> Tuple[str, bool, int]:
    """Reads `<<DELIM`, `<<-DELIM`, `<< 'DELIM'` from `start`. Returns (delimiter, strip tabs, end)."""
    index = start + 2
    length = len(text)
    strip = index < length and text[index] == "-"
    if strip:
        index += 1
    while index < length and text[index] in " \t":
        index += 1
    word: List[str] = []
    while index < length and text[index] not in " \t\n;|&<>()" and len(word) < 256:
        char = text[index]
        if char in "'\"":
            end = text.find(char, index + 1)
            end = length if end == -1 else end
            word.append(text[index + 1:end])
            index = end + 1
        elif char == "\\" and index + 1 < length:
            word.append(text[index + 1])
            index += 2
        else:
            word.append(char)
            index += 1
    return "".join(word), strip, min(index, length)


def _read_heredocs(text: str, index: int, pending: List[Tuple[_Segment, int, str, bool]]) -> int:
    """Consumes the bodies owed after a newline, in the order their openers appeared.

    Found by one regular-expression search per body rather than a Python loop
    over lines, so a large file written through a heredoc costs a scan at C
    speed. A body with no closing line runs to the end, as bash reads it.
    """
    length = len(text)
    for segment, slot, delimiter, strip in pending:
        pattern = re.compile(
            r"^" + (r"\t*" if strip else "") + re.escape(delimiter) + r"\r?$", re.MULTILINE
        )
        found = pattern.search(text, index)
        if found is None:
            body, index = text[index:], length
        else:
            body = text[index:found.start()]
            index = min(found.end() + 1, length)
        if strip:
            body = re.sub(r"(?m)^\t+", "", body)
        segment.heredocs[slot] = body[:-1] if body.endswith("\n") else body
    return index


def _scan(text: str, budget: _Budget) -> List[Any]:
    """Splits a command into simple commands and group markers, in the order they run.

    Returns a flat list of `_Segment`, `_OPEN` and `_CLOSE`. A substitution's
    inner commands are emitted before the command that contains it, because
    that is when the shell runs them.
    """
    events: List[Any] = []
    frames = [_Frame("top")]
    pending: List[Tuple[_Segment, int, str, bool]] = []
    index, length = 0, len(text)

    def finish(frame: _Frame, piped_next: bool) -> None:
        segment = frame.segment
        segment.text = "".join(segment.parts)
        segment.parts = []
        if segment.text.strip():
            events.append(segment)
            budget.segments += 1
        frame.segment = _Segment(piped_next)

    def push(kind: str) -> bool:
        if len(frames) >= MAX_FRAMES:
            budget.truncated = True
            return False
        events.append(_OPEN)
        frames.append(_Frame(kind))
        return True

    def pop() -> None:
        frame = frames.pop()
        finish(frame, False)
        events.append(_CLOSE)
        frames[-1].segment.parts.append(PLACEHOLDER)

    while index < length:
        budget.operators += 1
        if budget.operators > MAX_OPERATORS or budget.segments > MAX_SEGMENTS:
            budget.truncated = True
            break
        frame = frames[-1]
        parts = frame.segment.parts

        if frame.quoted:
            found = _QUOTED_SPECIAL.search(text, index)
            if found is None:
                parts.append(text[index:].translate(_PROTECT))
                index = length
                break
            at = found.start()
            parts.append(text[index:at].translate(_PROTECT))
            char = text[at]
            if char == "\\":
                parts.append(text[at:at + 2].translate(_PROTECT))
                index = at + 2
            elif char == '"':
                parts.append('"')
                frame.quoted = False
                index = at + 1
            elif char == "`":
                index = at + 1
                if not push("backtick"):
                    break
            elif text.startswith("$((", at):
                end = _matching(text, at + 3, "(", ")", 2)
                parts.append(text[at:end].translate(_PROTECT))
                index = end
            elif text.startswith("$(", at):
                index = at + 2
                if not push("subst"):
                    break
            else:
                parts.append("$")
                index = at + 1
            continue

        found = _COMMAND_SPECIAL.search(text, index)
        if found is None:
            parts.append(text[index:])
            index = length
            break
        at = found.start()
        parts.append(text[index:at])
        char = text[at]
        index = at + 1

        if char == "\\":
            following = text[at + 1:at + 2]
            if following == "\n":
                index = at + 2  # a line continuation joins the lines
            elif following and _WINDOWS_SEPARATOR_AFTER.match(following) and _WINDOWS_SEPARATOR_BEFORE.match(_last_char(parts) or " "):
                # `src\domain\x.py` unquoted: bash would drop the backslashes and
                # name `srcdomainx.py`, but an agent on Windows means a path, and
                # reading it as the shell does would hide a domain write from the
                # rules. The separator is kept as the path it plainly is.
                parts.append("/")
            else:
                # `\>` is a literal greater-than sign, not a redirect, so it is
                # protected like a quoted one.
                parts.append("\\" + following.translate(_PROTECT))
                index = at + 2
        elif char == "'":
            end = text.find("'", at + 1)
            if end == -1:
                parts.append(text[at:])  # unterminated: shlex refuses the segment
                index = length
            else:
                parts.append(text[at:end + 1].translate(_PROTECT))
                index = end + 1
        elif char == '"':
            parts.append('"')
            frame.quoted = True
        elif char == "$":
            if text.startswith("$((", at):
                end = _matching(text, at + 3, "(", ")", 2)
                parts.append(text[at:end])
                index = end
            elif text.startswith("$(", at):
                index = at + 2
                if not push("subst"):
                    break
            elif text.startswith("${", at):
                end = _matching(text, at + 2, "{", "}", 1)
                parts.append(text[at:end])
                index = end
            elif text.startswith("$'", at):
                decoded, index = _ansi_c(text, at + 1)
                parts.append(shlex.quote(decoded).translate(_PROTECT))
            else:
                parts.append("$")
        elif char == "`":
            if frame.kind == "backtick":
                pop()
            elif not push("backtick"):
                break
        elif char == "(":
            if text.startswith("()", at):
                parts.append("()")
                index = at + 2
            elif not push("paren"):
                break
        elif char == ")":
            if frame.kind in ("subst", "paren"):
                pop()
            else:
                parts.append(")")
        elif char == "\n":
            finish(frame, False)
            if pending:
                index = _read_heredocs(text, index, pending)
                pending = []
        elif char == ";":
            if text.startswith(";;", at):
                index = at + 2
            finish(frame, False)
        elif char == "|":
            if _last_char(parts) == ">":
                parts.append("|")  # `>|` writes past noclobber
            elif text.startswith("||", at):
                index = at + 2
                finish(frame, False)
            elif text.startswith("|&", at):
                index = at + 2
                finish(frame, True)
            else:
                finish(frame, True)
        elif char == "&":
            if text.startswith("&&", at):
                index = at + 2
                finish(frame, False)
            elif _last_char(parts) in (">", "<") or text.startswith("&>", at):
                parts.append("&")
            else:
                finish(frame, False)  # backgrounded: still runs
        elif char == "<":
            if text.startswith("<<<", at):
                parts.append("<<<")
                index = at + 3
            elif text.startswith("<<", at):
                delimiter, strip, end = _heredoc_opener(text, at)
                if delimiter and len(pending) < MAX_HEREDOCS:
                    segment = frame.segment
                    segment.heredocs.append(None)
                    pending.append((segment, len(segment.heredocs) - 1, delimiter, strip))
                parts.append(text[at:end])
                index = end
            else:
                parts.append("<")
        elif char == ">":
            parts.append(">")
        elif char == "#":
            last = _last_char(parts)
            if not last or last.isspace():
                newline = text.find("\n", at)
                index = length if newline == -1 else newline
            else:
                parts.append("#")

    while len(frames) > 1:
        pop()
    finish(frames[0], False)
    return events


# --- tokens and simple commands ---------------------------------------------------

# Programs that can write a file. Used only to decide whether a segment too long
# to tokenise could have been writing, so the list errs towards inclusion.
_WRITER_WORDS = (
    "tee", "sed", "perl", "awk", "gawk", "cp", "mv", "install", "ln", "rsync", "dd", "patch", "apply_patch",
    "python", "py", "node", "bun", "deno", "bash", "sh", "zsh", "dash", "ksh", "eval", "rm", "unlink", "rmdir",
    "shred", "touch", "truncate", "curl", "wget", "sponge", "git", "watch", "set-content", "add-content",
    "out-file", "new-item", "copy-item", "move-item", "remove-item", "tee-object",
)
_WRITER_WORD = re.compile(
    r"(?<![\w.-])(?:" + "|".join(re.escape(word) for word in _WRITER_WORDS).replace("python", r"python[\d.]*")
    + r")(?![\w.-])",
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _could_write(text: str) -> bool:
    """Whether a segment names a redirect or a program that writes, cheaply.

    Each word is first looked for as plain text, which runs at C speed; only a
    segment containing one pays for the expression, whose lookbehind at every
    position cost 0.4 s on 400 KB of `${`.
    """
    if ">" in text:
        return True
    lowered = text.lower()
    return any(word in lowered for word in _WRITER_WORDS) and _WRITER_WORD.search(text) is not None


_REDIRECTS = frozenset((">", ">>", ">|", "&>", "&>>", ">&", "<", "<&", "<>", "<<", "<<<"))


def _tokens(segment: _Segment, budget: _Budget) -> Optional[List[str]]:
    """The segment's words, with quotes removed. None if it was too long to read."""
    text = segment.text
    if len(text) > MAX_SEGMENT_CHARS or budget.chars + len(text) > MAX_TOKENISED_CHARS:
        # Too long to tokenise. Only a segment that could be writing makes the
        # analysis incomplete; a 200 KB echo with no redirect writes nothing.
        if _could_write(text):
            budget.truncated = True
        return None
    budget.chars += len(text)
    lexer = shlex.shlex(text, posix=True, punctuation_chars="<>&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens: List[str] = []
    try:
        for token in lexer:
            budget.tokens += 1
            if budget.tokens > MAX_TOKENS:
                budget.truncated = True
                break
            tokens.append(token)
    except ValueError:
        # An unterminated quote. The shell refuses to run it, so it writes nothing.
        return []
    # Returned still protected. A quoted `'>'` and a real redirect both read `>`
    # once unprotected, so the operators are told apart here and every word is
    # unprotected by _parse_simple as it is kept.
    return tokens


class _Simple:
    """One simple command: its words, its assignments and where its streams go."""

    __slots__ = ("argv", "assignments", "outputs", "stdin_file", "stdin_text", "has_stdin")

    def __init__(self) -> None:
        self.argv: List[str] = []
        self.assignments: List[str] = []
        self.outputs: List[Tuple[str, str, str]] = []  # (target, fd, operator)
        self.stdin_file: Optional[str] = None
        self.stdin_text: Optional[str] = None
        self.has_stdin = False


def _parse_simple(tokens: List[str], segment: _Segment) -> _Simple:
    simple = _Simple()
    heredoc = 0
    index = 0
    count = len(tokens)
    while index < count:
        token = tokens[index]
        if token in _REDIRECTS:
            following = tokens[index + 1].translate(_UNPROTECT) if index + 1 < count else None
            index += 2
            fd = "0" if token.startswith("<") else ("&" if token.startswith("&") else "1")
            # `2>file` tokenises as `2`, `>`, `file`: the digit is the stream,
            # not an argument, and `2>&1` must not become a write to a file named 1.
            if len(simple.argv) > 1 and len(simple.argv[-1]) == 1 and simple.argv[-1].isdigit():
                fd = simple.argv.pop()
            if token == ">&":
                if following is None or following.isdigit() or following == "-":
                    continue
                simple.outputs.append((following, "&", token))
            elif token in ("<&", "<>"):
                continue
            elif token == "<":
                simple.stdin_file = following
                simple.has_stdin = True
            elif token == "<<":
                simple.stdin_text = segment.heredocs[heredoc] if heredoc < len(segment.heredocs) else None
                heredoc += 1
                simple.has_stdin = True
            elif token == "<<<":
                simple.stdin_text = (following or "") + "\n"
                simple.has_stdin = True
            elif following is not None:
                simple.outputs.append((following, fd, token))
            continue
        token = token.translate(_UNPROTECT)
        if not simple.argv and _ASSIGNMENT.match(token):
            simple.assignments.append(token)
        else:
            simple.argv.append(token)
        index += 1
    return simple


def program_name(word: str) -> str:
    """`/usr/bin/python3.11` and `C:/Python/python.exe` are both a program called python3.11 / python."""
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _skip_options(argv: List[str], start: int, with_value: Iterable[str] = ()) -> int:
    values = set(with_value)
    index = start
    while index < len(argv) and argv[index].startswith("-") and argv[index] != "-":
        if argv[index] == "--":
            return index + 1
        index += 2 if argv[index] in values else 1
    return index


def _unwrap(argv: List[str], assignments: List[str]) -> List[str]:
    """Removes the wrappers that run another command unchanged: sudo, env, nohup, timeout."""
    for _ in range(8):
        if not argv:
            return argv
        name = program_name(argv[0])
        if name in ("sudo", "doas"):
            argv = argv[_skip_options(argv, 1, ("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U")):]
        elif name == "env":
            index = 1
            while index < len(argv) and (argv[index].startswith("-") or _ASSIGNMENT.match(argv[index])):
                if _ASSIGNMENT.match(argv[index]):
                    assignments.append(argv[index])
                index += 2 if argv[index] in ("-u", "-C", "-S", "--unset", "--chdir", "--split-string") else 1
            argv = argv[index:]
        elif name in ("command", "builtin", "exec", "nohup", "time", "noglob"):
            argv = argv[_skip_options(argv, 1):]
        elif name == "nice":
            argv = argv[_skip_options(argv, 1, ("-n", "--adjustment")):]
        elif name == "timeout":
            index = _skip_options(argv, 1, ("-s", "-k", "--signal", "--kill-after"))
            argv = argv[index + 1:]
        elif name == "stdbuf":
            argv = argv[_skip_options(argv, 1, ("-i", "-o", "-e")):]
        else:
            return argv
    return argv


# --- the state one analysis carries -------------------------------------------------

class _State:
    """Where the command is, what it has written so far, and what it has found."""

    __slots__ = ("cwd", "stack", "known", "result", "budget", "depth")

    def __init__(self, cwd: str, known: Dict[str, Optional[str]], result: ShellAnalysis, budget: _Budget, depth: int) -> None:
        self.cwd = cwd
        self.stack: List[str] = []
        self.known = known
        self.result = result
        self.budget = budget
        self.depth = depth

    def child(self) -> "_State":
        """A new shell: it shares what has been written, not where it stands."""
        return _State(self.cwd, self.known, self.result, self.budget, self.depth + 1)


_SPECIAL_TARGETS = frozenset(("-", "nul", "$null", "con", "/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"))


def resolve(path: Optional[str], cwd: str = "") -> Optional[str]:
    """A written path as a relative or absolute POSIX path, or None for a stream such as /dev/null.

    Relative paths are joined to where the command stands after its `cd`s, so
    `cd src/domain && echo ... > user.py` is a write to `src/domain/user.py`.
    """
    if path is None:
        return None
    candidate = path.strip().replace("\\", "/")
    if not candidate:
        return None
    lowered = candidate.lower()
    if lowered in _SPECIAL_TARGETS or lowered.startswith(("/dev/", "/proc/self/fd/")):
        return None
    if cwd and not (candidate.startswith(("/", "~")) or re.match(r"^[A-Za-z]:/", candidate)):
        candidate = cwd.rstrip("/") + "/" + candidate
    return posixpath.normpath(candidate)


def _write(state: _State, target: Optional[str], content: Optional[str], route: str, deletes: bool = False) -> None:
    if len(state.result.writes) >= MAX_WRITES:
        state.budget.truncated = True
        return
    if target is None:
        state.result.writes.append(ShellWrite(None, content, route, deletes))
        return
    resolved = resolve(target, state.cwd)
    if resolved is None:
        return
    state.result.writes.append(ShellWrite(resolved, content, route, deletes))
    if deletes:
        state.known.pop(resolved, None)
    else:
        state.known[resolved] = content


def _known(state: _State, path: str) -> Optional[str]:
    """What this command itself wrote to `path` earlier, or UNKNOWN."""
    resolved = resolve(path, state.cwd)
    return state.known.get(resolved) if resolved is not None else UNKNOWN


def _looks_like_file(path: str) -> bool:
    last = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return bool(re.search(r"[^.]\.[A-Za-z0-9]{1,8}$", last))


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


# --- content of the simple writers -------------------------------------------------

def _unescape(text: str) -> str:
    """The backslash escapes echo -e, printf and sed replacements understand."""
    return re.sub(r"\\([ntr\\])", lambda match: {"n": "\n", "t": "\t", "r": "\r", "\\": "\\"}[match.group(1)], text)


def _echo(args: List[str]) -> str:
    newline = True
    index = 0
    while index < len(args) and re.fullmatch(r"-[neE]+", args[index]):
        if "n" in args[index]:
            newline = False
        index += 1
    return _unescape(" ".join(args[index:])) + ("\n" if newline else "")


def _printf(args: List[str]) -> Optional[str]:
    index = 0
    if args[:1] == ["--"]:
        index = 1
    if args[index:index + 1] == ["-v"]:
        return ""  # assigns to a variable, prints nothing
    if index >= len(args):
        return ""
    template, values = args[index], list(args[index + 1:])
    conversion = re.compile(r"%(?:%|[-+ #0]*\d*(?:\.\d+)?[sbdiouxXcqfeEgG])")
    if not conversion.search(template.replace("%%", "")):
        return _unescape(template.replace("%%", "%"))
    out: List[str] = []
    for _ in range(len(values) + 1):
        def substitute(match: "re.Match[str]") -> str:
            if match.group(0) == "%%":
                return "%"
            return values.pop(0) if values else ""

        out.append(conversion.sub(substitute, template))
        if not values or sum(len(piece) for piece in out) > MAX_CODE_CHARS:
            break
    return _unescape("".join(out))


def _sed_address(script: str, index: int) -> int:
    """Skips an address or an address range, such as `1,5`, `$`, `/re/I` or `0,/re/`."""
    for _ in range(2):
        while index < len(script) and script[index] in " \t":
            index += 1
        if index >= len(script):
            return index
        char = script[index]
        if char.isdigit():
            while index < len(script) and (script[index].isdigit() or script[index] == "~"):
                index += 1
        elif char == "$":
            index += 1
        elif char in "/\\":
            delimiter = char
            if char == "\\":
                index += 1
                delimiter = script[index] if index < len(script) else "/"
            index += 1
            while index < len(script) and script[index] != delimiter:
                index += 2 if script[index] == "\\" else 1
            index += 1
            while index < len(script) and script[index] in "IM":
                index += 1
        elif char == "+":
            index += 1
            while index < len(script) and script[index].isdigit():
                index += 1
        else:
            return index
        if index < len(script) and script[index] == ",":
            index += 1
            continue
        break
    while index < len(script) and script[index] in " \t!":
        index += 1
    return index


def _sed_delimited(script: str, index: int, delimiter: str) -> Tuple[str, int]:
    """Reads up to an unescaped delimiter. Returns (text with `\\delim` unescaped, index after it)."""
    out: List[str] = []
    while index < len(script) and script[index] != delimiter:
        if script[index] == "\\" and index + 1 < len(script):
            following = script[index + 1]
            out.append(following if following == delimiter else "\\" + following)
            index += 2
            continue
        out.append(script[index])
        index += 1
    return "".join(out), index + 1


def sed_added_text(script: str) -> Optional[str]:
    """What a sed script adds to a file: its replacements and its a/i/c text. UNKNOWN if it can add anything else.

    Deleting lines and printing add nothing. `y` transliterates, `r` reads
    another file in and `e` runs a command, so the text they add is not in the
    script and the answer is UNKNOWN rather than a guess.
    """
    added: List[str] = []
    index = 0
    steps = 0
    while index < len(script):
        steps += 1
        if steps > 1_000:
            return UNKNOWN
        while index < len(script) and script[index] in " \t\n;{}":
            index += 1
        if index >= len(script):
            break
        index = _sed_address(script, index)
        if index >= len(script):
            break
        command = script[index]
        index += 1
        if command == "s":
            if index >= len(script):
                return UNKNOWN
            delimiter = script[index]
            _, index = _sed_delimited(script, index + 1, delimiter)
            replacement, index = _sed_delimited(script, index, delimiter)
            flags_end = index
            while flags_end < len(script) and script[flags_end] not in ";\n}":
                flags_end += 1
            flags = script[index:flags_end]
            if "e" in flags or "w" in flags:
                return UNKNOWN
            added.append(_unescape(replacement.replace("\\&", "&")))
            index = flags_end
        elif command in "aic":
            rest = script[index:]
            if rest.startswith("\\"):
                rest = rest[1:].lstrip("\n")
            end = rest.find("\n")
            line = rest if end == -1 else rest[:end]
            added.append(_unescape(line.strip()))
            index = len(script) if end == -1 else index + (len(script[index:]) - len(rest)) + end
        elif command in "dDpPnNqQgGhHxlz=":
            continue
        elif command in "btT:":
            while index < len(script) and script[index] not in ";\n":
                index += 1
        else:
            return UNKNOWN
    return "\n".join(added)


# --- the writers, one by one ----------------------------------------------------------

def _files_after_options(args: List[str], with_value: Iterable[str] = ()) -> List[str]:
    values = set(with_value)
    files: List[str] = []
    index = 0
    literal = False
    while index < len(args):
        arg = args[index]
        if literal:
            files.append(arg)
        elif arg == "--":
            literal = True
        elif arg.startswith("-") and arg != "-":
            if arg in values:
                index += 1
        else:
            files.append(arg)
        index += 1
    return files


def _tee(args: List[str], stdin: Optional[str], state: _State, route: str = "tee") -> Optional[str]:
    for target in _files_after_options(args, ("--output-error",)):
        _write(state, target, stdin, route)
    return stdin


def _sed(args: List[str], state: _State) -> None:
    in_place = False
    scripts: List[Optional[str]] = []
    files: List[str] = []
    script_given = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            files.extend(args[index + 1:])
            break
        if arg.startswith("--"):
            name, _, value = arg.partition("=")
            if name == "--in-place":
                in_place = True
            elif name == "--expression":
                if not value and index + 1 < len(args):
                    index += 1
                    value = args[index]
                scripts.append(value)
                script_given = True
            elif name == "--file":
                if not value:
                    index += 1
                scripts.append(UNKNOWN)
                script_given = True
            index += 1
            continue
        if arg.startswith("-") and len(arg) > 1:
            letters = arg[1:]
            for position, letter in enumerate(letters):
                if letter == "i":
                    in_place = True
                    # BSD sed spells "no backup" as a separate empty argument.
                    if position == len(letters) - 1 and index + 1 < len(args) and args[index + 1] == "":
                        index += 1
                    break
                if letter in "ef":
                    value = letters[position + 1:]
                    if not value and index + 1 < len(args):
                        index += 1
                        value = args[index]
                    scripts.append(value if letter == "e" else UNKNOWN)
                    script_given = True
                    break
                if letter == "l":
                    if position == len(letters) - 1:
                        index += 1
                    break
            index += 1
            continue
        if not script_given:
            scripts.append(arg)
            script_given = True
        else:
            files.append(arg)
        index += 1
    if not in_place:
        return
    content: Optional[str] = ""
    pieces = []
    for script in scripts:
        added = sed_added_text(script) if script is not None else UNKNOWN
        if added is UNKNOWN:
            content = UNKNOWN
            break
        pieces.append(added)
    if content is not UNKNOWN:
        content = "\n".join(pieces)
    for target in files:
        _write(state, target, content, "sed -i")


def _perl(args: List[str], state: _State) -> None:
    in_place = False
    scripts: List[Optional[str]] = []
    files: List[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            files.extend(args[index + 1:])
            break
        if arg.startswith("-") and len(arg) > 1 and not arg.startswith("--"):
            letters = arg[1:]
            position = 0
            while position < len(letters):
                letter = letters[position]
                if letter == "i":
                    in_place = True
                    break
                if letter in "eE":
                    value = letters[position + 1:]
                    if not value and index + 1 < len(args):
                        index += 1
                        value = args[index]
                    scripts.append(value)
                    break
                if letter in "IMmCdDxV":
                    break  # the rest of the word is this option's value
                if letter == "0":
                    position += 1
                    while position < len(letters) and letters[position] in "0123456789abcdefABCDEFx":
                        position += 1
                    continue
                position += 1
            index += 1
            continue
        if not scripts:
            scripts.append(UNKNOWN)  # a program file
        else:
            files.append(arg)
        index += 1
    if not in_place:
        return
    content: Optional[str] = UNKNOWN
    pieces = []
    for script in scripts:
        if script is None:
            pieces = None
            break
        substitutions = [part.strip() for part in script.split(";") if part.strip()]
        for part in substitutions:
            match = re.fullmatch(r"s(.)(.*)", part, re.DOTALL)
            if not match or match.group(1).isalnum() or match.group(1) in "{([<":
                pieces = None
                break
            delimiter = match.group(1)
            _, after = _sed_delimited(part, 2, delimiter)
            replacement, end = _sed_delimited(part, after, delimiter)
            if not re.fullmatch(r"[gimsx]*", part[end:]):
                pieces = None
                break
            pieces.append(_unescape(replacement))
        if pieces is None:
            break
    if pieces is not None:
        content = "\n".join(pieces)
    for target in files:
        _write(state, target, content, "perl -i")


def _awk(args: List[str], state: _State) -> None:
    in_place = False
    program_given = False
    files: List[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-i", "--include"):
            in_place = in_place or (index + 1 < len(args) and args[index + 1] in ("inplace", "inplace.awk"))
            index += 2
            continue
        if arg.startswith("-i") and arg[2:] in ("inplace", "inplace.awk") or arg in ("--include=inplace",):
            in_place = True
        elif arg in ("-f", "-v", "-F"):
            program_given = program_given or arg == "-f"
            index += 2
            continue
        elif arg.startswith("-"):
            pass
        elif not program_given:
            program_given = True
        else:
            files.append(arg)
        index += 1
    if in_place:
        for target in files:
            _write(state, target, UNKNOWN, "awk -i inplace")


_COPY_VALUE_OPTIONS = {
    "cp": ("-t", "-S", "--target-directory", "--suffix"),
    "mv": ("-t", "-S", "--target-directory", "--suffix"),
    "ln": ("-t", "-S", "--target-directory", "--suffix"),
    "install": ("-t", "-S", "-m", "-o", "-g", "--target-directory", "--suffix", "--mode", "--owner", "--group"),
    "rsync": ("-e", "-f", "--rsh", "--filter", "--exclude", "--include", "-T", "--temp-dir"),
}


def _copy_like(name: str, args: List[str], state: _State, route: Optional[str] = None) -> None:
    """cp, mv, install, ln and rsync: the destination is written, and for mv the source goes."""
    route = route or name
    values = set(_COPY_VALUE_OPTIONS.get(name, ()))
    operands: List[str] = []
    target_directory: Optional[str] = None
    no_target_directory = False
    directories_only = False
    literal = False
    index = 0
    while index < len(args):
        arg = args[index]
        if literal or arg == "-" or not arg.startswith("-"):
            operands.append(arg)
        elif arg == "--":
            literal = True
        elif arg in ("-t", "--target-directory") and index + 1 < len(args):
            target_directory = args[index + 1]
            index += 1
        elif arg.startswith("--target-directory="):
            target_directory = arg.split("=", 1)[1]
        elif arg in ("-T", "--no-target-directory") and name != "rsync":
            no_target_directory = True
        elif name == "install" and arg in ("-d", "--directory"):
            directories_only = True
        elif arg in values:
            index += 1
        index += 1
    if directories_only:
        return
    if name == "rsync":
        # host:path is another machine; C:/path is a drive letter, not a host.
        operands = [op for op in operands if not re.match(r"^[^/\\]+:", op) or re.match(r"^[A-Za-z]:[/\\]", op)]

    if name == "ln" and len(operands) == 1 and not target_directory:
        sources, destinations = operands, [[_basename(operands[0])]]
    elif target_directory:
        sources = operands
        destinations = [[target_directory.rstrip("/") + "/" + _basename(source)] for source in sources]
    elif len(operands) >= 2:
        sources, destination = operands[:-1], operands[-1]
        if len(sources) > 1 or destination.endswith("/") or destination in (".", ".."):
            destinations = [[destination.rstrip("/") + "/" + _basename(source)] for source in sources]
        elif no_target_directory or _looks_like_file(destination):
            destinations = [[destination]]
        else:
            # `cp evil.py src/domain` writes src/domain/evil.py when src/domain
            # is a directory, which only the file system knows. Both readings
            # are judged, so the rule sees the file whichever one is true.
            destinations = [[destination, destination.rstrip("/") + "/" + _basename(sources[0])]]
    else:
        return

    for source, targets in zip(sources, destinations):
        content = UNKNOWN if name in ("ln", "rsync") else _known(state, source)
        for target in targets:
            _write(state, target, content, route)
    if name == "mv" or (name == "rsync" and "--remove-source-files" in args):
        for source in sources:
            _write(state, source, UNKNOWN, route, deletes=True)


def _dd(args: List[str], state: _State) -> None:
    source = next((arg[3:] for arg in args if arg.startswith("if=")), None)
    for arg in args:
        if arg.startswith("of="):
            _write(state, arg[3:], _known(state, source) if source else UNKNOWN, "dd of=")


def _curl(args: List[str], state: _State) -> None:
    takes_value = set("odHXuAebcTFmxwrCEKYyzQtUP")
    outputs: List[str] = []
    remote_name = False
    directory = ""
    urls: List[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("--"):
            name, _, value = arg.partition("=")
            if name in ("--output", "--output-dir"):
                if not value and index + 1 < len(args):
                    index += 1
                    value = args[index]
                if name == "--output":
                    outputs.append(value)
                else:
                    directory = value
            elif name in ("--remote-name", "--remote-name-all"):
                remote_name = True
        elif arg.startswith("-") and len(arg) > 1:
            for position, letter in enumerate(arg[1:]):
                if letter == "O":
                    remote_name = True
                elif letter in takes_value:
                    value = arg[position + 2:]
                    if not value and index + 1 < len(args):
                        index += 1
                        value = args[index]
                    if letter == "o":
                        outputs.append(value)
                    break
        else:
            urls.append(arg)
        index += 1
    prefix = directory.rstrip("/") + "/" if directory else ""
    for output in outputs:
        _write(state, prefix + output if output != "-" else output, UNKNOWN, "curl -o")
    if remote_name:
        for url in urls:
            name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            if name and "://" not in name:
                _write(state, prefix + name, UNKNOWN, "curl -O")


def _wget(args: List[str], state: _State) -> None:
    output: Optional[str] = None
    prefix = ""
    urls: List[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-O", "--output-document") and index + 1 < len(args):
            output = args[index + 1]
            index += 1
        elif arg.startswith("--output-document="):
            output = arg.split("=", 1)[1]
        elif arg.startswith("-O") and not arg.startswith("--"):
            output = arg[2:]
        elif arg in ("-P", "--directory-prefix") and index + 1 < len(args):
            prefix = args[index + 1].rstrip("/") + "/"
            index += 1
        elif arg.startswith("--directory-prefix="):
            prefix = arg.split("=", 1)[1].rstrip("/") + "/"
        elif not arg.startswith("-"):
            urls.append(arg)
        index += 1
    if output is not None:
        if output != "-":
            _write(state, prefix + output, UNKNOWN, "wget -O")
        return
    for url in urls:
        name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if name and "://" not in name:
            _write(state, prefix + name, UNKNOWN, "wget")


def _delete(args: List[str], state: _State, route: str) -> None:
    for target in _files_after_options(args):
        _write(state, target, UNKNOWN, route, deletes=True)


# --- patches ---------------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _strip_components(path: str, count: int) -> str:
    path = path.strip().split("\t", 1)[0]
    if path == "/dev/null":
        return path
    parts = path.split("/")
    return "/".join(parts[count:]) if count < len(parts) else parts[-1]


def diff_writes(text: str, strip: int = 1, reverse: bool = False) -> List[Tuple[str, str, bool]]:
    """Each file a unified diff writes, with the lines it adds. Returns (path, added text, deletes).

    Hunks are consumed by their declared lengths, so a removed line that
    happens to begin `-- ` is never read as the next file's header.
    """
    entries: List[Tuple[str, List[str], bool]] = []
    old_path = new_path = None
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- "):
            old_path = _strip_components(line[4:], strip)
        elif line.startswith("+++ ") and old_path is not None:
            new_path = _strip_components(line[4:], strip)
            if reverse:
                old_path, new_path = new_path, old_path
            if new_path == "/dev/null":
                entries.append((old_path, [], True))
            else:
                entries.append((new_path, [], False))
        elif line.startswith("rename to ") and entries is not None:
            entries.append((line[len("rename to "):].strip(), [], False))
        else:
            hunk = _HUNK.match(line)
            if hunk and entries:
                old_left = int(hunk.group(1) or 1)
                new_left = int(hunk.group(2) or 1)
                added = entries[-1][1]
                index += 1
                while index < len(lines) and (old_left > 0 or new_left > 0):
                    body = lines[index]
                    if body.startswith("+"):
                        (added if not reverse else []).append(body[1:])
                        new_left -= 1
                    elif body.startswith("-"):
                        if reverse:
                            added.append(body[1:])
                        old_left -= 1
                    elif body.startswith("\\"):
                        pass
                    else:
                        old_left -= 1
                        new_left -= 1
                    index += 1
                continue
        index += 1
    return [(path, "\n".join(added), deletes) for path, added, deletes in entries if path]


def apply_patch_writes(text: str) -> List[Tuple[str, str, bool]]:
    """Codex's apply_patch envelope, as (path, added text, deletes)."""
    entries: List[Tuple[str, List[str], bool]] = []
    for line in text.splitlines():
        line = line.rstrip("\r")
        if line.startswith("*** Add File: ") or line.startswith("*** Update File: "):
            entries.append((line.split(": ", 1)[1].strip(), [], False))
        elif line.startswith("*** Delete File: "):
            entries.append((line.split(": ", 1)[1].strip(), [], True))
        elif line.startswith("*** Move to: ") and entries:
            path, added, _ = entries.pop()
            entries.append((path, [], True))
            entries.append((line.split(": ", 1)[1].strip(), added, False))
        elif line.startswith("+") and entries:
            entries[-1][1].append(line[1:])
    return [(path, "\n".join(added), deletes) for path, added, deletes in entries if path]


def _apply_diff(state: _State, text: Optional[str], route: str, strip: int, reverse: bool = False, prefix: str = "") -> None:
    if text is None:
        # The patch is a file on the developer's machine. Which files it writes
        # is as unknown as what it writes into them.
        _write(state, None, UNKNOWN, route)
        return
    for path, added, deletes in diff_writes(text, strip, reverse):
        _write(state, prefix + path, UNKNOWN if deletes else added, route, deletes=deletes)


def _git_apply(args: List[str], stdin: Optional[str], has_stdin: bool, state: _State) -> None:
    if any(arg in ("--check", "--stat", "--numstat", "--summary") for arg in args) and "--apply" not in args:
        return
    strip = 1
    reverse = False
    prefix = ""
    patches: List[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-R", "--reverse"):
            reverse = True
        elif re.fullmatch(r"-p\d+", arg):
            strip = int(arg[2:])
        elif arg == "-p" and index + 1 < len(args) and args[index + 1].isdigit():
            strip = int(args[index + 1])
            index += 1
        elif arg.startswith("--directory="):
            prefix = arg.split("=", 1)[1].rstrip("/") + "/"
        elif arg in ("--exclude", "--include", "--whitespace", "-C") and index + 1 < len(args):
            index += 1
        elif not arg.startswith("-") or arg == "-":
            patches.append(arg)
        index += 1
    if patches and patches != ["-"]:
        for patch in patches:
            _apply_diff(state, _known(state, patch), "git apply", strip, reverse, prefix)
    else:
        _apply_diff(state, stdin if has_stdin else UNKNOWN, "git apply", strip, reverse, prefix)


def _patch(args: List[str], stdin: Optional[str], has_stdin: bool, state: _State) -> None:
    strip = 0
    reverse = False
    patch_file: Optional[str] = None
    output: Optional[str] = None
    operands: List[str] = []
    takes_value = ("-p", "-i", "-o", "-d", "-D", "-F", "-r", "-B", "-V", "-Y", "-z", "-g", "-x", "--input", "--output", "--directory", "--strip")
    index = 0
    while index < len(args):
        arg = args[index]
        name, _, value = arg.partition("=")
        if arg == "--dry-run":
            return
        if arg in ("-R", "--reverse"):
            reverse = True
        elif re.fullmatch(r"-p\d+", arg):
            strip = int(arg[2:])
        elif name == "--strip" and value.isdigit():
            strip = int(value)
        elif name in ("--input", "--output") and value:
            patch_file, output = (value, output) if name == "--input" else (patch_file, value)
        elif re.fullmatch(r"-[io].+", arg):
            patch_file, output = (arg[2:], output) if arg[1] == "i" else (patch_file, arg[2:])
        elif arg in takes_value and index + 1 < len(args):
            value = args[index + 1]
            if arg == "-p" and value.isdigit():
                strip = int(value)
            elif arg in ("-i", "--input"):
                patch_file = value
            elif arg in ("-o", "--output"):
                output = value
            index += 1
        elif not arg.startswith("-") or arg == "-":
            operands.append(arg)
        index += 1
    if patch_file is None and len(operands) >= 2:
        patch_file = operands[1]
    text = _known(state, patch_file) if patch_file else (stdin if has_stdin else UNKNOWN)
    explicit = output or (operands[0] if operands else None)
    if explicit:
        if text is None:
            _write(state, explicit, UNKNOWN, "patch")
        else:
            added = "\n".join(entry[1] for entry in diff_writes(text, strip, reverse))
            _write(state, explicit, added, "patch")
        return
    _apply_diff(state, text, "patch", strip, reverse)


# --- git: writes and the hooks it can be told to skip ----------------------------------

_GIT_GLOBAL_VALUES = ("-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--exec-path", "--config-env", "--attr-source")
_COMMIT_VALUES = (
    "-m", "-F", "-C", "-c", "-t", "--message", "--file", "--reuse-message", "--reedit-message",
    "--fixup", "--squash", "--template", "--cleanup", "--trailer", "--author", "--date", "--pathspec-from-file",
)
_CONFIG_READS = ("--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "get", "list")


def _is_hooks_path(key: str) -> bool:
    return key.strip().lower().startswith("core.hookspath")


def _git(args: List[str], assignments: List[str], stdin: Optional[str], has_stdin: bool, state: _State) -> None:
    tampering = state.result.tampering
    for assignment in assignments:
        name, _, value = assignment.partition("=")
        if name == "GIT_CONFIG_PARAMETERS" and "core.hookspath" in value.lower():
            tampering.append("GIT_CONFIG_PARAMETERS sets core.hooksPath")
        elif re.fullmatch(r"GIT_CONFIG_KEY_\d+", name) and _is_hooks_path(value):
            tampering.append(f"{name} sets core.hooksPath")

    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-c" or arg == "--config-env":
            value = args[index + 1] if index + 1 < len(args) else ""
            if _is_hooks_path(value):
                tampering.append(f"git {arg} {value.split('=', 1)[0]}=... points the hooks somewhere else")
            index += 2
            continue
        if arg.startswith("-c") and len(arg) > 2 and _is_hooks_path(arg[2:]):
            tampering.append("git -c core.hooksPath=... points the hooks somewhere else")
        if arg.startswith("--config-env=") and _is_hooks_path(arg.split("=", 1)[1]):
            tampering.append("git --config-env core.hooksPath points the hooks somewhere else")
        if arg in _GIT_GLOBAL_VALUES:
            index += 2
            continue
        if not arg.startswith("-"):
            break
        index += 1
    if index >= len(args):
        return
    subcommand, rest = args[index], args[index + 1:]

    if subcommand == "commit":
        position = 0
        while position < len(rest):
            arg = rest[position]
            if arg == "--":
                break
            if arg.startswith("--"):
                if len(arg) >= len("--no-veri") and "--no-verify".startswith(arg):
                    tampering.append("git commit --no-verify skips the pre-commit hook")
                elif arg in _COMMIT_VALUES:
                    position += 1
            elif arg.startswith("-") and len(arg) > 1:
                for letter in arg[1:]:
                    if letter == "n":
                        tampering.append("git commit -n skips the pre-commit hook")
                        break
                    if letter in "mFCctuS":
                        if letter in "mFCct" and arg.endswith(letter):
                            position += 1  # the value is the next word
                        break
            position += 1
    elif subcommand == "config":
        if not any(arg in _CONFIG_READS for arg in rest) and any(_is_hooks_path(arg) for arg in rest if not arg.startswith("-")):
            tampering.append("git config core.hooksPath points the hooks somewhere else")
    elif subcommand == "apply":
        _git_apply(rest, stdin, has_stdin, state)
    elif subcommand == "rm":
        if not any(arg in ("-n", "--dry-run") for arg in rest):
            _delete([arg for arg in rest if arg not in ("-r", "-f", "-q", "--cached", "--force", "--quiet")], state, "git rm")
    elif subcommand == "mv":
        if not any(arg in ("-n", "--dry-run") for arg in rest):
            _copy_like("mv", [arg for arg in rest if arg not in ("-f", "-k", "-v", "--force", "--verbose")], state, "git mv")


# --- code handed to an interpreter --------------------------------------------------------

def _call_name(node: ast.AST) -> str:
    """`open`, `os.remove`, `shutil.copy`: the dotted name a call is made through."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif parts:
        parts.append("?")
    return ".".join(reversed(parts))


class _PythonReader:
    """Finds the files a piece of Python opens for writing, and what it writes to them.

    Paths are evaluated as far as the code allows: literals, f-strings,
    `Path(a) / b`, `os.path.join` and names bound once to one of those. The part
    that cannot be known becomes a placeholder, so `os.path.join(root,
    "domain", "user.py")` is still a path a rule on `**/domain/**` covers.
    Content is only ever taken from literals: content with any unknown part is
    UNKNOWN, because the unknown part is exactly where an import would be.
    """

    PATH_WRAPPERS = frozenset(
        ("Path", "PurePath", "PosixPath", "WindowsPath", "PurePosixPath", "PureWindowsPath", "str", "os.fspath",
         "os.path.abspath", "os.path.normpath", "os.path.realpath", "os.path.expanduser")
    )
    OPENERS = frozenset(("open", "io.open", "builtins.open", "codecs.open"))
    COPIERS = frozenset(("shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree", "copy", "copy2", "copyfile", "copytree"))
    MOVERS = frozenset(("shutil.move", "os.rename", "os.replace", "move", "rename", "replace"))
    LINKERS = frozenset(("os.link", "os.symlink"))
    REMOVERS = frozenset(("os.remove", "os.unlink", "os.rmdir", "os.removedirs", "shutil.rmtree"))

    def __init__(self, tree: ast.AST, argv: Sequence[str]) -> None:
        self.tree = tree
        self.argv = list(argv)
        self.parents: Dict[int, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[id(child)] = parent
        self.bindings = self._bindings()

    def _bindings(self) -> Dict[str, Optional[ast.AST]]:
        """Names assigned exactly once, to what. A name assigned twice is not trusted."""
        seen: Dict[str, Optional[ast.AST]] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                seen[name] = None if name in seen else node.value
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)) and isinstance(node.target, ast.Name):
                seen[node.target.id] = None
        return seen

    def text(self, node: Optional[ast.AST], depth: int = 0) -> Tuple[Optional[str], bool]:
        """The string a node evaluates to, and whether every part of it is known."""
        if node is None or depth > 24:
            return None, False
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            value = node.value.decode("utf-8", "replace") if isinstance(node.value, bytes) else node.value
            return value, True
        if isinstance(node, ast.JoinedStr):
            pieces, complete = [], True
            for value in node.values:
                if isinstance(value, ast.Constant):
                    pieces.append(str(value.value))
                else:
                    inner, known = self.text(getattr(value, "value", None), depth + 1)
                    pieces.append(inner if inner is not None else _UNKNOWN_PART)
                    complete = complete and known
            return "".join(pieces), complete
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
            left, left_known = self.text(node.left, depth + 1)
            right, right_known = self.text(node.right, depth + 1)
            if left is None and right is None:
                return None, False
            joiner = "/" if isinstance(node.op, ast.Div) else ""
            return (left or _UNKNOWN_PART) + joiner + (right or _UNKNOWN_PART), left_known and right_known
        if isinstance(node, ast.Name):
            bound = self.bindings.get(node.id)
            return self.text(bound, depth + 1) if bound is not None else (None, False)
        if isinstance(node, ast.Subscript) and _call_name(node.value) == "sys.argv":
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, int) and 1 <= index.value <= len(self.argv):
                return self.argv[index.value - 1], True
            return None, False
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in self.PATH_WRAPPERS or name.endswith(".Path"):
                return self._joined(node.args, depth)
            if name == "os.path.join":
                return self._joined(node.args, depth)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath":
                return self._joined([node.func.value] + list(node.args), depth)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "join" and len(node.args) == 1:
                separator, separator_known = self.text(node.func.value, depth + 1)
                items = node.args[0]
                if separator is not None and isinstance(items, (ast.List, ast.Tuple)):
                    pieces = [self.text(item, depth + 1) for item in items.elts]
                    return separator.join(p or _UNKNOWN_PART for p, _ in pieces), separator_known and all(k for _, k in pieces)
        return None, False

    def _joined(self, parts: Sequence[ast.AST], depth: int) -> Tuple[Optional[str], bool]:
        if not parts:
            return None, False
        evaluated = [self.text(part, depth + 1) for part in parts]
        if all(value is None for value, _ in evaluated):
            return None, False
        return "/".join(value or _UNKNOWN_PART for value, _ in evaluated), all(known for _, known in evaluated)

    def content(self, node: Optional[ast.AST]) -> Optional[str]:
        value, complete = self.text(node)
        return value if value is not None and complete else UNKNOWN

    @staticmethod
    def _mode(call: ast.Call, position: int) -> str:
        if len(call.args) > position and isinstance(call.args[position], ast.Constant):
            return str(call.args[position].value)
        for keyword in call.keywords:
            if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
        return "r"

    def _handle_content(self, opener: ast.Call) -> Optional[str]:
        """What is written through the file object an `open(...)` returns, or UNKNOWN."""
        parent = self.parents.get(id(opener))
        if isinstance(parent, ast.Attribute) and parent.attr in ("write", "writelines"):
            call = self.parents.get(id(parent))
            if isinstance(call, ast.Call) and call.args:
                return self._written(parent.attr, call.args[0])
            return UNKNOWN
        handle: Optional[str] = None
        if isinstance(parent, ast.withitem) and isinstance(parent.optional_vars, ast.Name):
            handle = parent.optional_vars.id
        elif isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
            handle = parent.targets[0].id
        if handle is None:
            return UNKNOWN
        pieces: List[str] = []
        uses = 0
        understood = 0
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name) and node.id == handle and isinstance(node.ctx, ast.Load):
                uses += 1
                attribute = self.parents.get(id(node))
                call = self.parents.get(id(attribute)) if attribute is not None else None
                if isinstance(attribute, ast.Attribute) and isinstance(call, ast.Call) and call.func is attribute:
                    if attribute.attr in ("write", "writelines") and call.args:
                        written = self._written(attribute.attr, call.args[0])
                        if written is UNKNOWN:
                            return UNKNOWN
                        pieces.append(written)
                        understood += 1
                    elif attribute.attr in ("close", "flush"):
                        understood += 1
                elif isinstance(attribute, ast.keyword) and attribute.arg == "file":
                    printed = self.parents.get(id(attribute))
                    if isinstance(printed, ast.Call) and _call_name(printed.func) == "print":
                        values = [self.content(argument) for argument in printed.args]
                        if any(value is UNKNOWN for value in values):
                            return UNKNOWN
                        pieces.append(" ".join(values) + "\n")
                        understood += 1
        return "".join(pieces) if uses == understood else UNKNOWN

    def _written(self, method: str, argument: ast.AST) -> Optional[str]:
        if method == "writelines" and isinstance(argument, (ast.List, ast.Tuple)):
            values = [self.content(item) for item in argument.elts]
            return UNKNOWN if any(value is UNKNOWN for value in values) else "".join(values)
        return self.content(argument)

    def writes(self) -> List[Tuple[Optional[str], Optional[str], str, bool]]:
        found: List[Tuple[Optional[str], Optional[str], str, bool]] = []

        def path_of(node: Optional[ast.AST]) -> Optional[str]:
            value, _ = self.text(node)
            return value

        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
            if name in self.OPENERS and node.args:
                if any(letter in self._mode(node, 1) for letter in "wax+"):
                    found.append((path_of(node.args[0]), self._handle_content(node), "python open()", False))
            elif method == "open" and receiver is not None and name not in self.OPENERS:
                if any(letter in self._mode(node, 0) for letter in "wax+") and path_of(receiver) is not None:
                    found.append((path_of(receiver), self._handle_content(node), "python Path.open()", False))
            elif method in ("write_text", "write_bytes") and receiver is not None:
                found.append((path_of(receiver), self.content(node.args[0]) if node.args else UNKNOWN, f"python {method}()", False))
            elif method == "touch" and receiver is not None:
                found.append((path_of(receiver), "", "python touch()", False))
            elif name in self.COPIERS and len(node.args) >= 2:
                found.append((path_of(node.args[1]), UNKNOWN, f"python {name}()", False))
            elif name in self.MOVERS and len(node.args) >= 2 and "." in name:
                found.append((path_of(node.args[1]), UNKNOWN, f"python {name}()", False))
                found.append((path_of(node.args[0]), UNKNOWN, f"python {name}()", True))
            elif method in ("rename", "replace") and receiver is not None and len(node.args) == 1 and not node.keywords:
                # Path.rename(target) takes one argument; str.replace takes two,
                # and every `line.replace('\n', '')` in a one-liner is not a move.
                found.append((path_of(node.args[0]), UNKNOWN, f"python Path.{method}()", False))
                found.append((path_of(receiver), UNKNOWN, f"python Path.{method}()", True))
            elif name in self.LINKERS and len(node.args) >= 2:
                found.append((path_of(node.args[1]), UNKNOWN, f"python {name}()", False))
            elif method in ("symlink_to", "hardlink_to") and receiver is not None:
                found.append((path_of(receiver), UNKNOWN, f"python Path.{method}()", False))
            elif name in self.REMOVERS and node.args:
                found.append((path_of(node.args[0]), UNKNOWN, f"python {name}()", True))
            elif method in ("unlink", "rmdir") and receiver is not None and not name.startswith("os."):
                found.append((path_of(receiver), UNKNOWN, f"python Path.{method}()", True))
            elif name == "os.open" and len(node.args) >= 2:
                flags = ast.dump(node.args[1])
                if any(flag in flags for flag in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC")):
                    found.append((path_of(node.args[0]), UNKNOWN, "python os.open()", False))
        return found


_PY_OPEN_FALLBACK = re.compile(
    r"""open\(\s*(?P<q>['"])(?P<path>[^'"\n]{1,400})(?P=q)\s*,\s*(?:mode\s*=\s*)?['"](?P<mode>[rwaxbt+]{1,4})['"]"""
)
_PY_WRITE_TEXT_FALLBACK = re.compile(
    r"""Path\(\s*(?P<q>['"])(?P<path>[^'"\n]{1,400})(?P=q)\s*\)\s*\.\s*write_(?:text|bytes)\("""
)


def python_writes(code: str, argv: Sequence[str] = ()) -> List[Tuple[Optional[str], Optional[str], str, bool]]:
    """The writes a piece of Python makes: (path or None, content or UNKNOWN, route, deletes)."""
    if not code or len(code) > MAX_CODE_CHARS:
        return []
    tree = None
    if len(code) <= PYTHON_PARSE_LIMIT:
        try:
            tree = ast.parse(code)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            tree = None
    if tree is not None:
        try:
            return _PythonReader(tree, argv).writes()
        except RecursionError:
            pass
    # Code that does not parse, often because a shell mangled a quote on the way
    # in, still names its literal targets, and a write found by pattern is a
    # write whose content is UNKNOWN rather than no write at all.
    found: List[Tuple[Optional[str], Optional[str], str, bool]] = []
    for match in _PY_OPEN_FALLBACK.finditer(code):
        if any(letter in match.group("mode") for letter in "wax+"):
            found.append((match.group("path"), UNKNOWN, "python open()", False))
    for match in _PY_WRITE_TEXT_FALLBACK.finditer(code):
        found.append((match.group("path"), UNKNOWN, "python write_text()", False))
    return found


_JS_CALL = re.compile(
    r"\b(?P<fn>writeFileSync|appendFileSync|writeFile|appendFile|outputFileSync|outputFile|createWriteStream|"
    r"copyFileSync|copyFile|cpSync|renameSync|rename|symlinkSync|linkSync|unlinkSync|unlink|rmSync|rmdirSync|truncateSync)\s*\("
)
_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "`": "`", "0": "\0"}


def _js_arguments(code: str, start: int, wanted: int = 2) -> List[Tuple[str, Optional[str]]]:
    """The first few arguments of a call: ("literal", text) for a string literal, ("other", None) otherwise."""
    arguments: List[Tuple[str, Optional[str]]] = []
    index = start
    length = len(code)
    limit = min(length, start + 4_000)
    while index < limit and len(arguments) < wanted:
        while index < limit and code[index] in " \t\r\n":
            index += 1
        if index >= limit:
            break
        quote = code[index]
        if quote in "'\"`":
            out: List[str] = []
            index += 1
            interpolated = False
            while index < limit and code[index] != quote:
                char = code[index]
                if char == "\\" and index + 1 < limit:
                    following = code[index + 1]
                    if following in "xu":
                        width = 2 if following == "x" else 4
                        digits = code[index + 2:index + 2 + width]
                        if re.fullmatch(r"[0-9A-Fa-f]+", digits or "-") and len(digits) == width:
                            out.append(chr(int(digits, 16)))
                            index += 2 + width
                            continue
                    out.append(_JS_ESCAPES.get(following, following))
                    index += 2
                    continue
                if quote == "`" and code.startswith("${", index):
                    interpolated = True
                out.append(char)
                index += 1
            index += 1
            while index < limit and code[index] in " \t\r\n":
                index += 1
            if index < limit and code[index] in ",)":
                arguments.append(("other", None) if interpolated else ("literal", "".join(out)))
            else:
                arguments.append(("other", None))  # a literal that is only part of an expression
        else:
            arguments.append(("other", None))
        depth = 0
        while index < limit:
            char = code[index]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                if depth == 0:
                    return arguments
                depth -= 1
            elif char == "," and depth == 0:
                index += 1
                break
            index += 1
    return arguments


def node_writes(code: str) -> List[Tuple[Optional[str], Optional[str], str, bool]]:
    """The writes a piece of JavaScript makes through `fs`, read from its literal arguments."""
    if not code or len(code) > MAX_CODE_CHARS:
        return []
    found: List[Tuple[Optional[str], Optional[str], str, bool]] = []
    for match in _JS_CALL.finditer(code):
        function = match.group("fn")
        arguments = _js_arguments(code, match.end())
        first = arguments[0][1] if arguments and arguments[0][0] == "literal" else None
        second = arguments[1][1] if len(arguments) > 1 and arguments[1][0] == "literal" else None
        route = f"node {function}()"
        if function in ("writeFileSync", "appendFileSync", "writeFile", "appendFile", "outputFileSync", "outputFile"):
            if first is not None:
                found.append((first, second, route, False))
        elif function == "createWriteStream":
            if first is not None:
                found.append((first, UNKNOWN, route, False))
        elif function in ("copyFileSync", "copyFile", "cpSync", "symlinkSync", "linkSync"):
            if second is not None:
                found.append((second, UNKNOWN, route, False))
        elif function in ("renameSync", "rename"):
            if second is not None:
                found.append((second, UNKNOWN, route, False))
            if first is not None:
                found.append((first, UNKNOWN, route, True))
        elif function == "truncateSync":
            if first is not None:
                found.append((first, "", route, False))
        elif first is not None:
            found.append((first, UNKNOWN, route, True))
    return found


def _record(state: _State, writes: Iterable[Tuple[Optional[str], Optional[str], str, bool]]) -> None:
    for target, content, route, deletes in writes:
        if target:
            _write(state, target, content, route, deletes)


def _interpreter_code(args: List[str], stdin: Optional[str], has_stdin: bool, state: _State, eval_flags: Sequence[str],
                      value_options: Sequence[str]) -> Tuple[Optional[str], List[str]]:
    """The code an interpreter runs and the arguments after it, when the command carries it."""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in eval_flags:
            return (args[index + 1] if index + 1 < len(args) else ""), args[index + 2:]
        for flag in eval_flags:
            if flag.startswith("--") and arg.startswith(flag + "="):
                return arg.split("=", 1)[1], args[index + 1:]
        if "-c" in eval_flags and re.fullmatch(r"-[bBdEhiIOPqRsSuvx]*c", arg):
            return (args[index + 1] if index + 1 < len(args) else ""), args[index + 2:]
        if "-c" in eval_flags and re.fullmatch(r"-c.+", arg):
            return arg[2:], args[index + 1:]
        if arg == "-m":
            return None, []
        if arg == "-":
            return (stdin if has_stdin else None), args[index + 1:]
        if arg in value_options:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        # A script. Only one this same command wrote can be read.
        return _known(state, arg), args[index + 1:]
    return (stdin if has_stdin else None), []


PYTHONS = re.compile(r"^(?:python[\d.]*|py|pypy[\d.]*)$")
NODES = frozenset(("node", "nodejs", "bun"))
SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh", "ash", "busybox"))


def _shell_script(args: List[str], stdin: Optional[str], has_stdin: bool, state: _State) -> Optional[str]:
    index = 0
    while index < len(args):
        arg = args[index]
        if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", arg) and not arg.startswith("--"):
            return args[index + 1] if index + 1 < len(args) else ""
        if arg in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            index += 2
            continue
        if arg.startswith(("-", "+")):
            index += 1
            continue
        return _known(state, arg)
    return stdin if has_stdin else None


# --- PowerShell, which Antigravity runs on Windows -------------------------------------------

_POWERSHELL_PARAMETERS = {
    "path": "path", "literalpath": "path", "filepath": "path", "lp": "path", "pspath": "path",
    "value": "value", "destination": "destination", "itemtype": "type", "type": "type",
}


def _powershell(name: str, args: List[str], stdin: Optional[str], state: _State) -> Optional[str]:
    named: Dict[str, List[str]] = {}
    positional: List[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("-") and len(arg) > 1 and not arg[1].isdigit():
            key = arg[1:].rstrip(":").lower()
            canonical = _POWERSHELL_PARAMETERS.get(key)
            if canonical is None:
                canonical = next((value for full, value in _POWERSHELL_PARAMETERS.items() if len(key) >= 2 and full.startswith(key)), None)
            if canonical and index + 1 < len(args):
                named.setdefault(canonical, []).append(args[index + 1])
                index += 2
                continue
            index += 1
            continue
        positional.append(arg)
        index += 1
    paths = named.get("path") or positional[:1]
    route = name
    if name in ("set-content", "add-content", "new-item", "ni"):
        value = (named.get("value") or positional[1:2] or [None])[0]
        if value is None and stdin is not None:
            value = stdin
        if name in ("new-item", "ni") and (named.get("type") or ["file"])[0].lower() not in ("file", "f"):
            return ""
        for path in paths:
            _write(state, path, value if value is not None else ("" if name in ("new-item", "ni") else UNKNOWN), route)
        return ""
    if name in ("out-file", "tee-object"):
        for path in paths:
            _write(state, path, stdin, route)
        return stdin if name == "tee-object" else ""
    if name in ("copy-item", "copy", "cpi", "move-item", "move", "mi"):
        destination = (named.get("destination") or positional[1:2] or [None])[0]
        if destination:
            _copy_like("mv" if name.startswith("mo") or name == "mi" else "cp", paths + [destination], state, route)
        return ""
    if name in ("remove-item", "del", "erase", "ri", "rd"):
        for path in paths + positional[1:]:
            _write(state, path, UNKNOWN, route, deletes=True)
        return ""
    return UNKNOWN


# --- the analysis -------------------------------------------------------------------------------

def _run(name: str, argv: List[str], simple: _Simple, stdin: Optional[str], state: _State) -> Optional[str]:
    """Records what one simple command writes and returns what it prints, or UNKNOWN."""
    args = argv[1:]
    has_stdin = simple.has_stdin or stdin is not None
    if name == "echo":
        return _echo(args)
    if name == "printf":
        return _printf(args)
    if name in ("cat", "type", "get-content", "gc"):
        files = [arg for arg in args if not arg.startswith("-") or arg == "-"]
        if not files or files == ["-"]:
            return stdin
        contents = [_known(state, path) for path in files]
        return UNKNOWN if any(content is UNKNOWN for content in contents) else "".join(contents)
    if name == "tee":
        return _tee(args, stdin, state)
    if name == "sponge":
        return _tee(args[-1:], stdin, state, "sponge")
    if name in ("cd", "chdir", "set-location", "sl"):
        target = next((arg for arg in args if not arg.startswith("-") or arg == "-"), None)
        if target is None:
            state.cwd = "~"
        elif target != "-":
            state.cwd = resolve(target, state.cwd) or state.cwd
        return ""
    if name in ("pushd", "push-location"):
        state.stack.append(state.cwd)
        target = next((arg for arg in args if not arg.startswith(("-", "+"))), None)
        if target:
            state.cwd = resolve(target, state.cwd) or state.cwd
        return ""
    if name in ("popd", "pop-location"):
        if state.stack:
            state.cwd = state.stack.pop()
        return ""
    if name == "sed":
        _sed(args, state)
        return UNKNOWN
    if name == "perl":
        _perl(args, state)
        return UNKNOWN
    if name in ("awk", "gawk"):
        _awk(args, state)
        return UNKNOWN
    if name in ("cp", "mv", "install", "ln", "rsync"):
        _copy_like(name, args, state)
        return ""
    if name == "dd":
        _dd(args, state)
        return UNKNOWN
    if name in ("touch", "truncate"):
        for target in _files_after_options(args, ("-d", "-r", "-t", "-s", "--date", "--reference", "--size")):
            _write(state, target, "", name)
        return ""
    if name in ("rm", "unlink", "rmdir", "shred", "trash"):
        _delete(args, state, name)
        return ""
    if name == "curl":
        _curl(args, state)
        return UNKNOWN
    if name == "wget":
        _wget(args, state)
        return UNKNOWN
    if name == "git":
        _git(args, simple.assignments, stdin, has_stdin, state)
        return UNKNOWN
    if name == "patch":
        _patch(args, stdin, has_stdin, state)
        return UNKNOWN
    if name in ("apply_patch", "applypatch"):
        envelope = stdin if has_stdin else (args[0] if args else None)
        if envelope is None:
            _write(state, None, UNKNOWN, "apply_patch")
        else:
            for path, added, deletes in apply_patch_writes(envelope):
                _write(state, path, UNKNOWN if deletes else added, "apply_patch", deletes)
        return UNKNOWN
    if PYTHONS.match(name):
        code, rest = _interpreter_code(args, stdin, has_stdin, state, ("-c",), ("-W", "-X", "--check-hash-based-pycs"))
        if code:
            _record(state, python_writes(code, rest))
        return UNKNOWN
    if name in NODES or name == "deno":
        if name == "deno":
            if not args or args[0] != "eval":
                return UNKNOWN
            code = args[1] if len(args) > 1 else None
        else:
            code, _ = _interpreter_code(args, stdin, has_stdin, state, ("-e", "--eval", "-p", "--print"),
                                        ("-r", "--require", "--import", "--loader", "--experimental-loader", "-C", "--conditions"))
        if code:
            _record(state, node_writes(code))
        return UNKNOWN
    if name in SHELLS:
        script = _shell_script(args, stdin, has_stdin, state)
        if script:
            _analyse_into(script, state.child())
        return UNKNOWN
    if name == "eval":
        _analyse_into(" ".join(args), state.child())
        return UNKNOWN
    if name == "watch":
        index = _skip_options(argv, 1, ("-n", "--interval", "-q", "--equexit"))
        if index < len(argv):
            _analyse_into(" ".join(argv[index:]), state.child())
        return UNKNOWN
    if name in ("set-content", "add-content", "out-file", "tee-object", "new-item", "ni", "copy-item", "cpi",
                "copy", "move-item", "mi", "move", "remove-item", "del", "erase", "ri", "rd"):
        return _powershell(name, args, stdin, state)
    return UNKNOWN


def _analyse_into(text: str, state: _State) -> None:
    if state.depth > MAX_NESTED_SCRIPTS:
        # A script inside a script inside a script: past this, what it writes
        # is not read, and the caller is told rather than assured.
        state.budget.truncated = True
        return
    if len(text) > MAX_COMMAND_CHARS:
        state.budget.truncated = True
        text = text[:MAX_COMMAND_CHARS]
    events = _scan(text, state.budget)
    saved: List[Tuple[str, List[str]]] = []
    printed: List[Optional[str]] = [UNKNOWN]
    for event in events:
        if event is _OPEN:
            saved.append((state.cwd, list(state.stack)))
            printed.append(UNKNOWN)
            continue
        if event is _CLOSE:
            if saved:
                state.cwd, state.stack = saved.pop()
            if len(printed) > 1:
                printed.pop()
            continue
        tokens = _tokens(event, state.budget)
        if not tokens:
            printed[-1] = UNKNOWN
            continue
        simple = _parse_simple(tokens, event)
        argv = _unwrap(simple.argv, simple.assignments)
        if argv:
            state.result.commands.append(tuple(argv))
        if simple.stdin_text is not None:
            stdin: Optional[str] = simple.stdin_text
        elif simple.stdin_file is not None:
            stdin = _known(state, simple.stdin_file)
        elif event.piped:
            stdin = printed[-1]
        else:
            stdin = UNKNOWN
        name = program_name(argv[0]) if argv else ""
        if argv:
            output = _run(name, argv, simple, stdin, state)
        else:
            output = ""  # `> file` alone empties the file
        for target, fd, operator in simple.outputs:
            if fd in ("1", "&"):
                content = output
            else:
                # Another stream. echo and printf never write to it, so what
                # arrives there is nothing; any other program's is unknown.
                content = "" if name in ("echo", "printf") or not argv else UNKNOWN
            route = "heredoc" if simple.stdin_text is not None and name in ("cat", "") else f"redirect {operator}"
            _write(state, target, content, route)
        if any(fd in ("1", "&") for _, fd, _ in simple.outputs):
            output = ""
        printed[-1] = output


def analyse(command: Any, cwd: str = "") -> ShellAnalysis:
    """Every write, tampering and simple command in one shell command.

    `cwd` is where the command runs, relative to the project root; the hook
    sends commands with the root shortened to `.`, so the default is the root.
    A command sent as a list of words is quoted back into one line first, which
    is how Codex's exec form arrives.
    """
    result = ShellAnalysis()
    if isinstance(command, (list, tuple)):
        if not all(isinstance(word, str) for word in command):
            return result
        command = shlex.join(command)
    if not isinstance(command, str) or not command.strip():
        return result
    budget = _Budget()
    state = _State(cwd or "", {}, result, budget, 0)
    _analyse_into(command, state)
    result.truncated = budget.truncated
    return result
