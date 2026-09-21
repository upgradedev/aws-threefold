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

The same invariant covers what the shell decides only when the command runs: a
variable, a substitution, a glob, an escape sequence. `M=boto3; echo "import
$M" > src/domain/x.py` wrote `import boto3` while this module read the content
as `import $M` and approved it. Such a part is never read as the text it is
spelled with. While scanning, each is swapped for a private-use code point, so
a word that carries one is known to be unreadable wherever it ends up: as
content it is UNKNOWN, and as a target it is a pattern, which the gate asks
"could this be a file a rule covers?" rather than reading as a literal path.

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
import fnmatch
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from threefold.domain.path_match import _segments as _glob_segments

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

# What the shell decides only when the command runs. Each stands in a word for
# the part it replaces, so the word's other characters are still read.
#
# EXPANSION replaces a whole `$NAME`, `${...}`, `$((...))`, `$(...)`, backtick
# or `{a,b}` brace group: what it becomes can be any text, slashes included.
EXPANSION = ""
# The three characters an unquoted glob is made of. A quoted `*` stays a `*`.
STAR = ""
QUESTION = ""
BRACKET = ""
# An unquoted backslash between two word characters. Bash drops it and keeps
# the letter; an agent on Windows means a separator. Which one ran is not known,
# so a target that carries one is judged both ways.
BACKSLASH = ""
# Any single name, dot files included. Used only inside this module, for "a
# file directly beneath this directory".
_ANY_NAME = ""

# What a `$(...)`, a backtick or a `( ... )` group leaves behind in the command
# around it. The inner command is read on its own; the outer one sees a word it
# cannot know, which is what a substitution is until it runs.
PLACEHOLDER = EXPANSION
_UNKNOWN_PART = EXPANSION

_OPAQUE = (EXPANSION, STAR, QUESTION, BRACKET, _ANY_NAME)
_GLOB_CHARACTERS = str.maketrans({"*": STAR, "?": QUESTION, "[": BRACKET})
_DISPLAY = str.maketrans({EXPANSION: "${...}", STAR: "*", QUESTION: "?", BRACKET: "[", BACKSLASH: "\\", _ANY_NAME: "*"})
# `{a,b}` and `{1..3}` unquoted: one word becomes several. The class stops at
# the next brace or space, so an unclosed group cannot make the search quadratic.
_BRACE_GROUP = re.compile(r"\{[^{}\s]*(?:,|\.\.)[^{}\s]*\}")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def is_opaque(text: Optional[str]) -> bool:
    """Whether a word or a path carries a part the shell decides only when it runs."""
    return bool(text) and any(marker in text for marker in _OPAQUE)


def display(text: Optional[str]) -> str:
    """A word as a person would recognise it, with each unreadable part shown as written."""
    return (text or "").translate(_DISPLAY)


def _unquoted(chunk: str) -> str:
    """Unquoted text with its brace groups and glob characters marked, at C speed."""
    if "{" in chunk:
        chunk = _BRACE_GROUP.sub(EXPANSION, chunk)
    return chunk.translate(_GLOB_CHARACTERS)

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
    cannot see, or `open(path, 'w')` with a path computed when the code runs.
    `content` is UNKNOWN (None) when the route is recognised but what it writes
    is not in the command. `deletes` marks a path removed or moved away rather
    than written.

    `pattern` marks a target that carries an unreadable part (see is_opaque):
    it is a shape the file will have, not a path. `tree` marks a pattern that
    stands for every file of a directory tree copied, moved or linked in.
    `fragment` marks content that replaces part of a line whose other parts are
    not in the command, as `sed -i 's/json/boto3/'` does.
    """

    target: Optional[str]
    content: Optional[str]
    route: str
    deletes: bool = False
    pattern: bool = False
    tree: bool = False
    fragment: bool = False


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


# --- what a pattern could name ---------------------------------------------------
#
# A target with an unreadable part is a shape, not a path. The gate asks two
# questions of it: could it be one of the files above, and could it be a file a
# layering rule covers. Both are answered by asking whether some path matches
# the target's shape and a glob at once, segment by segment as path_match
# matches, so a rule is read the same way whether it meets a path or a shape.
#
# A segment is None for "any number of whole segments" (a `**` in a rule, an
# expansion in a command) or a glob: a tuple of tokens, each a lower-cased
# character, _ONE for any one character or _RUN for any run of them, with a
# flag saying the shell's own rule applies that a leading dot is matched only
# by a dot.

_ONE = "\x00?"
_RUN = "\x00*"
_Glob = Tuple[Tuple[str, ...], bool]
_Shape = Tuple[Optional[_Glob], ...]
MAX_SHAPE_STATES = 20_000


def _glob_of(segment: str, shell: bool) -> _Glob:
    """One segment as tokens. `shell` reads the markers of a command; otherwise `*` and `?` of a rule."""
    tokens: List[str] = []
    index = 0
    length = len(segment)
    while index < length:
        char = segment[index]
        if shell and char in (STAR, _ANY_NAME):
            tokens.append(_RUN)
        elif shell and char == QUESTION:
            tokens.append(_ONE)
        elif shell and char == BRACKET:
            close = segment.find("]", index + 2)
            if close == -1:
                tokens.append("[")
            else:
                tokens.append(_ONE)
                index = close
        elif not shell and char == "*":
            tokens.append(_RUN)
        elif not shell and char == "?":
            tokens.append(_ONE)
        else:
            tokens.append(char.lower())
        index += 1
    dot_rule = shell and bool(segment) and segment[0] in (STAR, QUESTION, BRACKET)
    return tuple(tokens), dot_rule


@lru_cache(maxsize=4096)
def _globs_meet(globs: Tuple[_Glob, ...]) -> bool:
    """Whether one non-empty segment exists that every glob matches.

    A table over the globs' positions, each state visited once, capped: past
    the cap the answer is yes, because the question is only ever asked to find
    out whether a write could be refused, and "could" is the safe side.
    """
    start = (tuple(0 for _ in globs), False)
    seen = {start}
    stack = [start]
    dot_rule = any(rule for _, rule in globs)
    while stack:
        if len(seen) > MAX_SHAPE_STATES:
            return True
        positions, started = stack.pop()
        if started and all(position == len(tokens) for position, (tokens, _) in zip(positions, globs)):
            return True
        following: List[Tuple[Tuple[int, ...], bool]] = []
        for index, (position, (tokens, _)) in enumerate(zip(positions, globs)):
            if position < len(tokens) and tokens[position] == _RUN:
                following.append((positions[:index] + (position + 1,) + positions[index + 1:], started))
        required: Optional[str] = None
        moved: List[int] = []
        possible = True
        for position, (tokens, _) in zip(positions, globs):
            if position == len(tokens):
                possible = False
                break
            token = tokens[position]
            if token == _RUN:
                moved.append(position)
            elif token == _ONE:
                moved.append(position + 1)
            else:
                if required is not None and required != token:
                    possible = False
                    break
                required = token
                moved.append(position + 1)
        if possible and not (dot_rule and not started and required == "."):
            following.append((tuple(moved), True))
        for state in following:
            if state not in seen:
                seen.add(state)
                stack.append(state)
    return False


@lru_cache(maxsize=4096)
def _shapes_meet(shapes: Tuple[_Shape, ...]) -> bool:
    """Whether one path exists that every shape matches, segment by segment."""
    start = tuple(0 for _ in shapes)
    seen = {start}
    stack = [start]
    while stack:
        if len(seen) > MAX_SHAPE_STATES:
            return True
        state = stack.pop()
        if all(position == len(shape) for position, shape in zip(state, shapes)):
            return True
        following: List[Tuple[int, ...]] = []
        for index, (position, shape) in enumerate(zip(state, shapes)):
            if position < len(shape) and shape[position] is None:
                following.append(state[:index] + (position + 1,) + state[index + 1:])
        globs: List[_Glob] = []
        moved: List[int] = []
        possible = True
        for position, shape in zip(state, shapes):
            if position == len(shape):
                possible = False
                break
            unit = shape[position]
            if unit is None:
                moved.append(position)
            else:
                globs.append(unit)
                moved.append(position + 1)
        if possible and globs and _globs_meet(tuple(globs)):
            following.append(tuple(moved))
        for candidate in following:
            if candidate not in seen:
                seen.add(candidate)
                stack.append(candidate)
    return False


def _target_shape(pattern: str) -> _Shape:
    """A command's target as a shape. A segment with an expansion in it can be any number of segments."""
    units: List[Optional[_Glob]] = []
    for segment in pattern.replace("\\", "/").split("/"):
        if segment in ("", "."):
            continue
        if EXPANSION in segment:
            units.append(None)
            tail = segment.rsplit(EXPANSION, 1)[1]
            if tail:
                # `$NAME.py` ends in `.py` whatever NAME holds.
                units.append(_glob_of(_ANY_NAME + tail, shell=True))
        else:
            units.append(_glob_of(segment, shell=True))
    return tuple(units)


@lru_cache(maxsize=1024)
def _rule_shape(glob: str) -> _Shape:
    return tuple(None if segment == "**" else _glob_of(segment, shell=False) for segment in _glob_segments(glob))


def pattern_matches_glob(pattern: str, glob: str, suffix: str = "") -> bool:
    """Whether some path the shell could make of `pattern` is covered by `glob` and ends with `suffix`.

    Matched without case, as path_match matches, and never for an empty
    target, which is the directory the command stands in rather than a file.
    """
    target = _target_shape(pattern)
    if not target or len(pattern) > 1_024:
        return bool(target)
    shapes = [target, _rule_shape(glob)]
    if suffix:
        shapes.append((None, _glob_of(_ANY_NAME + suffix.lower(), shell=True)))
    return _shapes_meet(tuple(shapes))


# The governance paths as globs: the named files, the two directories whose
# every file counts, and, for a deletion, the directories that hold them.
_GOVERNANCE_FILES_GLOBS = (
    "**/.claude/settings.json", "**/.claude/settings.local.json", "**/.codex/hooks.json", "**/.codex/config.toml",
    "**/.agents/hooks.json", "**/.git/config", "**/.threefold.json",
)
_GOVERNANCE_HOMES_GLOBS = ("**/.git/hooks/**", "**/.threefold/**")
_GOVERNANCE_DIRECTORY_GLOBS = ("**/.claude", "**/.codex", "**/.agents", "**/.git", "**/.threefold")
_GOVERNANCE_FINAL_NAMES = ("settings.json", "settings.local.json", "hooks.json", "config.toml", "config", ".threefold.json")
_GOVERNANCE_DIRECTORY_NAMES = (".claude", ".codex", ".agents", ".git", ".threefold")


def _segment_could_be(segment: str, name: str) -> bool:
    return _globs_meet((_glob_of(segment, shell=True), _glob_of(name, shell=False)))


def pattern_is_governance(pattern: str, deletes: bool = False, tree: bool = False) -> bool:
    """Whether a target with unreadable parts could be one of the files that decide whether the hooks run.

    Without expansions, only globs: `.claude/setting?.json` is the settings
    file, and `rm -rf build/*` is not `.threefold.json`, because the shell's `*`
    never matches a leading dot.

    With expansions, an expansion could supply any part of any path, and
    reading it that freely would make `> "$OUT/report.txt"` a write into
    .git/hooks and refuse it in every mode. So the literal part has to carry
    what makes the path a governance path: the file's own name
    (`$D/settings.json`, whatever D is), a `.threefold` or `.git/hooks`
    directory it passes through, or the settings directory an expansion is
    written into (`.claude/$F`).

    A `tree` is the files beneath a copied directory. The agents read their
    settings at the project root, so a tree is asked about the root's files:
    `cp -r /tmp/x/. ./` could bring a .claude/settings.json, `cp -r /tmp/x src/`
    brings one nobody reads. A tree from inside the project brings files that
    were already governed where they stood, and is not asked at all.
    """
    directories = _GOVERNANCE_DIRECTORY_GLOBS if deletes else ()
    if tree:
        if pattern.endswith(_ANY_NAME):
            return False
        anchored = _GOVERNANCE_FILES_GLOBS + _GOVERNANCE_HOMES_GLOBS + directories
        return any(pattern_matches_glob(pattern, glob[3:]) for glob in anchored)
    segments = [segment for segment in pattern.replace("\\", "/").split("/") if segment not in ("", ".")]
    if not segments:
        return False
    if not any(EXPANSION in segment for segment in segments):
        return any(pattern_matches_glob(pattern, glob) for glob in _GOVERNANCE_FILES_GLOBS + _GOVERNANCE_HOMES_GLOBS + directories)
    last = segments[-1]
    if EXPANSION not in last:
        names = _GOVERNANCE_FINAL_NAMES + (_GOVERNANCE_DIRECTORY_NAMES if deletes else ())
        if any(_segment_could_be(last, name) for name in names):
            if any(pattern_matches_glob(pattern, glob) for glob in _GOVERNANCE_FILES_GLOBS + directories):
                return True
    for index, segment in enumerate(segments):
        if EXPANSION in segment:
            continue
        following = segments[index + 1] if index + 1 < len(segments) else ""
        if _segment_could_be(segment, ".threefold") or (
            _segment_could_be(segment, ".git") and following and EXPANSION not in following and _segment_could_be(following, "hooks")
        ):
            if any(pattern_matches_glob(pattern, glob) for glob in _GOVERNANCE_HOMES_GLOBS):
                return True
    if EXPANSION in last and len(segments) > 1 and EXPANSION not in segments[-2]:
        directory = "/".join(segments[:-1])
        if any(pattern_matches_glob(directory, glob) for glob in ("**/.claude", "**/.codex", "**/.agents", "**/.git")):
            return True
    return False


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


def _expansion_end(text: str, at: int) -> Optional[int]:
    """Where a `$` expansion that starts at `at` ends, or None when this `$` is a plain dollar sign.

    `$(` is not handled here: a substitution holds a command, and the scanner
    reads that command rather than skipping it.
    """
    following = text[at + 1:at + 2]
    if not following:
        return None
    if text.startswith("$((", at):
        return _matching(text, at + 3, "(", ")", 2)
    if following == "{":
        return _matching(text, at + 2, "{", "}", 1)
    if following.isdigit() or following in "@*#?$!-":
        return at + 2
    name = _NAME.match(text, at + 1)
    return name.end() if name else None


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


def _heredoc_opener(text: str, start: int) -> Tuple[str, bool, bool, int]:
    """Reads `<<DELIM`, `<<-DELIM`, `<< 'DELIM'` from `start`. Returns (delimiter, strip tabs, quoted, end).

    A delimiter with any quoting in it makes the body literal; an unquoted one
    has its body expanded by the shell, which is what `quoted` records.
    """
    index = start + 2
    length = len(text)
    strip = index < length and text[index] == "-"
    if strip:
        index += 1
    while index < length and text[index] in " \t":
        index += 1
    word: List[str] = []
    quoted = False
    while index < length and text[index] not in " \t\n;|&<>()" and len(word) < 256:
        char = text[index]
        if char in "'\"":
            quoted = True
            end = text.find(char, index + 1)
            end = length if end == -1 else end
            word.append(text[index + 1:end])
            index = end + 1
        elif char == "\\" and index + 1 < length:
            quoted = True
            word.append(text[index + 1])
            index += 2
        else:
            word.append(char)
            index += 1
    return "".join(word), strip, quoted, min(index, length)


# What the shell expands in the body of a heredoc whose delimiter is unquoted.
# Each closer is optional and each class stops at the next `$` or backtick, so
# every match consumes what it scans and the pass stays linear.
_BODY_EXPANSION = re.compile(
    r"\\([$`\\\n])|\$(?:\{[^}$`]*\}?|\([^)$`]*\)?\)?|[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])|`[^`]*`?"
)


def _expanded_body(body: str) -> str:
    """An unquoted heredoc's body with each expansion marked, as the shell would leave it unread.

    `cat > x.py <<EOF` with `import $M` in it writes whatever M holds. The
    escapes the shell honours in such a body are applied, so `\\$` stays a
    dollar sign rather than becoming an expansion.
    """
    if "$" not in body and "`" not in body and "\\" not in body:
        return body

    def replace(match: "re.Match[str]") -> str:
        escaped = match.group(1)
        if escaped is not None:
            return "" if escaped == "\n" else escaped
        return EXPANSION

    return _BODY_EXPANSION.sub(replace, body)


def _read_heredocs(text: str, index: int, pending: List[Tuple[_Segment, int, str, bool, bool]]) -> int:
    """Consumes the bodies owed after a newline, in the order their openers appeared.

    Found by one regular-expression search per body rather than a Python loop
    over lines, so a large file written through a heredoc costs a scan at C
    speed. A body with no closing line runs to the end, as bash reads it.
    """
    length = len(text)
    for segment, slot, delimiter, strip, quoted in pending:
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
        body = body[:-1] if body.endswith("\n") else body
        segment.heredocs[slot] = body if quoted else _expanded_body(body)
    return index


def _scan(text: str, budget: _Budget) -> List[Any]:
    """Splits a command into simple commands and group markers, in the order they run.

    Returns a flat list of `_Segment`, `_OPEN` and `_CLOSE`. A substitution's
    inner commands are emitted before the command that contains it, because
    that is when the shell runs them.
    """
    events: List[Any] = []
    frames = [_Frame("top")]
    pending: List[Tuple[_Segment, int, str, bool, bool]] = []
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
            elif text.startswith("$(", at) and not text.startswith("$((", at):
                index = at + 2
                if not push("subst"):
                    break
            else:
                end = _expansion_end(text, at)
                if end is None:
                    parts.append("$")
                    index = at + 1
                else:
                    parts.append(EXPANSION)
                    index = end
            continue

        found = _COMMAND_SPECIAL.search(text, index)
        if found is None:
            parts.append(_unquoted(text[index:]))
            index = length
            break
        at = found.start()
        parts.append(_unquoted(text[index:at]))
        char = text[at]
        index = at + 1

        if char == "\\":
            following = text[at + 1:at + 2]
            if following == "\n":
                index = at + 2  # a line continuation joins the lines
            elif following and _WINDOWS_SEPARATOR_AFTER.match(following) and _WINDOWS_SEPARATOR_BEFORE.match(_last_char(parts) or " "):
                # `src\domain\x.py` unquoted: an agent on Windows means a path,
                # and bash drops the backslash and names `srcdomainx.py`. Either
                # may be what runs, and `.claude/setting\s.json` is the settings
                # file under the second reading, so both are kept and judged.
                parts.append(BACKSLASH)
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
            if text.startswith("$(", at) and not text.startswith("$((", at):
                index = at + 2
                if not push("subst"):
                    break
            elif text.startswith("$'", at):
                decoded, index = _ansi_c(text, at + 1)
                parts.append(shlex.quote(decoded).translate(_PROTECT))
            elif text.startswith('$"', at):
                pass  # a translated string: the quotes that follow are read as quotes
            else:
                end = _expansion_end(text, at)
                if end is None:
                    parts.append("$")
                else:
                    parts.append(EXPANSION)
                    index = end
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
                delimiter, strip, quoted, end = _heredoc_opener(text, at)
                if delimiter and len(pending) < MAX_HEREDOCS:
                    segment = frame.segment
                    segment.heredocs.append(None)
                    pending.append((segment, len(segment.heredocs) - 1, delimiter, strip, quoted))
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

    __slots__ = ("argv", "assignments", "outputs", "stdin_file", "stdin_text", "has_stdin", "heredoc")

    def __init__(self) -> None:
        self.argv: List[str] = []
        self.assignments: List[str] = []
        self.outputs: List[Tuple[str, str, str]] = []  # (target, fd, operator)
        self.stdin_file: Optional[str] = None
        self.stdin_text: Optional[str] = None
        self.has_stdin = False
        self.heredoc = False


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
            elif token == "<&":
                continue
            elif token == "<>":
                # Opened for reading and writing. On standard input nothing is
                # written to it; on any other stream, `1<> file`, it is a write
                # that does not truncate.
                if fd != "0" and following is not None:
                    simple.outputs.append((following, fd, token))
            elif token == "<":
                simple.stdin_file = following
                simple.has_stdin = True
            elif token == "<<":
                simple.stdin_text = segment.heredocs[heredoc] if heredoc < len(segment.heredocs) else None
                heredoc += 1
                simple.has_stdin = True
                simple.heredoc = True
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
    name = word.replace(BACKSLASH, "/").replace("\\", "/").rsplit("/", 1)[-1].lower()
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
        elif name == "busybox" and len(argv) > 1 and not argv[1].startswith("-"):
            # `busybox cp a b` is cp. Read as a shell, its applet name was taken
            # for a script and the copy was never seen.
            argv = argv[1:]
        else:
            return argv
    return argv


# Words that open, continue or close a compound command. `if true; then cp a
# b; fi` splits into `if true`, `then cp a b` and `fi`, and the middle one's
# first word is `then`, which no writer is called; the cp inside was never read.
_RESERVED_OPENERS = frozenset(("then", "do", "else", "elif", "if", "while", "until", "!", "{", "time"))
# A closer that carries a redirect or a pipe sends the whole compound's output
# there: `for ...; do echo x; done > file`.
_RESERVED_CLOSERS = frozenset(("}", "fi", "done", "esac"))


def _strip_reserved(argv: List[str], assignments: List[str], state: "_State") -> Tuple[List[str], bool]:
    """The simple command inside a compound one's opening words, and whether it was a closer.

    Also takes off a function definition's head, since its body is read as
    though it runs, and a case arm's pattern.
    """
    closer = False
    for _ in range(16):
        if not argv:
            break
        word = argv[0]
        if word in _RESERVED_OPENERS:
            argv = argv[1:]
        elif word in _RESERVED_CLOSERS:
            closer = True
            if word == "esac" and state.case_depth:
                state.case_depth -= 1
            argv = argv[1:]
        elif word == "case":
            # `case $x in a) cmd` arrives as one segment: skip to past `in`,
            # and the first arm's pattern goes with the check below.
            state.case_depth += 1
            argv = argv[argv.index("in") + 1:] if "in" in argv else []
            if argv and argv[0].endswith(")"):
                argv = argv[1:]
        elif state.case_depth and (word.endswith(")") or word == EXPANSION):
            argv = argv[1:]  # the pattern of a later arm, `b)` or `(b)`
        elif word == "function" and len(argv) > 1:
            argv = argv[3:] if len(argv) > 2 and argv[2] == "()" else argv[2:]
        elif word.endswith("()") and len(word) > 2:
            argv = argv[1:]
        elif len(argv) > 1 and argv[1] == "()":
            argv = argv[2:]
        else:
            break
    while argv and _ASSIGNMENT.match(argv[0]):
        assignments.append(argv.pop(0))
    return argv, closer


# --- the state one analysis carries -------------------------------------------------

class _State:
    """Where the command is, what it has written so far, and what it has found."""

    __slots__ = ("cwd", "stack", "known", "result", "budget", "depth", "case_depth")

    def __init__(self, cwd: str, known: Dict[str, Optional[str]], result: ShellAnalysis, budget: _Budget, depth: int) -> None:
        self.cwd = cwd
        self.stack: List[str] = []
        self.known = known
        self.result = result
        self.budget = budget
        self.depth = depth
        self.case_depth = 0

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
    if cwd and not _is_absolute(candidate):
        candidate = cwd.rstrip("/") + "/" + candidate
    if is_opaque(candidate) or BACKSLASH in candidate:
        return _normalise_pattern(candidate)
    return posixpath.normpath(candidate)


def _is_absolute(path: str) -> bool:
    return path.startswith(("/", "~")) or bool(re.match(r"^[A-Za-z]:/", path))


def _normalise_pattern(path: str) -> str:
    """normpath for a path with unreadable parts, which normpath would get wrong.

    `$D/..` is not the directory `$D` came from when D holds `a/b`, so a `..`
    after a segment that can stand for several is not collapsed: the whole path
    becomes one expansion, which the gate reads as "could be anywhere".
    """
    absolute = path.startswith("/")
    parts: List[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts and (EXPANSION in parts[-1] or BACKSLASH in parts[-1]):
                return EXPANSION
            if parts and parts[-1] != "..":
                parts.pop()
            elif not absolute:
                parts.append("..")
            continue
        parts.append(segment)
    joined = "/".join(parts)
    return ("/" + joined) if absolute else (joined or ".")


def _readings(path: str) -> List[str]:
    """Each path a target could be: a backslash between letters is a separator or nothing."""
    if BACKSLASH not in path:
        return [path]
    readings = []
    for separator in ("/", ""):
        reading = path.replace(BACKSLASH, separator)
        reading = _normalise_pattern(reading) if is_opaque(reading) else posixpath.normpath(reading)
        if reading not in readings:
            readings.append(reading)
    return readings


def _write(state: _State, target: Optional[str], content: Optional[str], route: str, deletes: bool = False,
           tree: bool = False, fragment: bool = False) -> None:
    # Content that carries a part the shell decides at run time is content
    # nobody can read before it runs, whatever the rest of it says.
    if content is not None and (is_opaque(content) or BACKSLASH in content):
        content = UNKNOWN
    if target is None:
        if len(state.result.writes) >= MAX_WRITES:
            state.budget.truncated = True
            return
        state.result.writes.append(ShellWrite(None, content, route, deletes))
        return
    resolved = resolve(target, state.cwd)
    if resolved is None:
        return
    for reading in _readings(resolved):
        if len(state.result.writes) >= MAX_WRITES:
            state.budget.truncated = True
            return
        pattern = is_opaque(reading)
        state.result.writes.append(ShellWrite(reading, content, route, deletes, pattern, tree and pattern, fragment))
        if pattern:
            continue
        if deletes:
            state.known.pop(reading, None)
        else:
            state.known[reading] = content


def _known(state: _State, path: str) -> Optional[str]:
    """What this command itself wrote to `path` earlier, or UNKNOWN."""
    resolved = resolve(path, state.cwd)
    if resolved is None or is_opaque(resolved) or BACKSLASH in resolved:
        return UNKNOWN
    return state.known.get(resolved)


def _looks_like_file(path: str) -> bool:
    last = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return bool(re.search(r"[^.]\.[A-Za-z0-9]{1,8}$", last))


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


# --- content of the simple writers -------------------------------------------------

_SIMPLE_ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


def _decode_escapes(text: str, flavour: str) -> Tuple[Optional[str], bool]:
    """Backslash escapes as echo -e and %b ("echo") or a printf format ("printf") expand them.

    Returns (text, stopped): `stopped` is a `\\c`, after which echo and printf
    print nothing more. A backslash this does not know makes the result UNKNOWN
    rather than a guess: the shells disagree about the rare ones, and a guess
    is how `printf '\\151mport boto3'` was read as clean content that wrote
    `import boto3`.
    """
    if "\\" not in text:
        return text, False
    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        slash = text.find("\\", index)
        if slash == -1:
            out.append(text[index:])
            break
        out.append(text[index:slash])
        code = text[slash + 1:slash + 2]
        index = slash + 2
        if not code:
            return UNKNOWN, False  # a backslash at the very end
        if code in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[code])
        elif code == "c" and flavour == "echo":
            return "".join(out), True
        elif flavour == "printf" and code in "\"'?":
            out.append(code)
        elif code in ("x", "u", "U"):
            width = {"x": 2, "u": 4, "U": 8}[code]
            digits = re.match(r"[0-9A-Fa-f]{1,%d}" % width, text[index:index + width])
            if not digits:
                return UNKNOWN, False
            value = int(digits.group(0), 16)
            if value > 0x10FFFF:
                return UNKNOWN, False
            out.append(chr(value))
            index += len(digits.group(0))
        elif code == "0" and flavour == "echo":
            digits = re.match(r"[0-7]{0,3}", text[index:index + 3]).group(0)
            out.append(chr(int(digits or "0", 8)))
            index += len(digits)
        elif code in "01234567" and flavour == "printf":
            digits = re.match(r"[0-7]{1,3}", text[slash + 1:slash + 4]).group(0)
            out.append(chr(int(digits, 8) & 0xFF))
            index = slash + 1 + len(digits)
        else:
            return UNKNOWN, False
    return "".join(out), False


def _echo(args: List[str]) -> Optional[str]:
    """What echo prints. With a backslash in it and no -e, UNKNOWN when the shells disagree.

    Bash's echo prints `import\\x20boto3` as written and zsh's expands it to
    `import boto3`. Which shell runs the command is not in the command, so text
    whose two readings differ is read as neither.
    """
    newline = True
    expand = False
    index = 0
    while index < len(args) and re.fullmatch(r"-[neE]+", args[index]):
        for letter in args[index][1:]:
            if letter == "n":
                newline = False
            else:
                expand = letter == "e"
        index += 1
    text = " ".join(args[index:])
    if "\\" not in text:
        return text + ("\n" if newline else "")
    decoded, stopped = _decode_escapes(text, "echo")
    if decoded is UNKNOWN:
        return UNKNOWN
    if expand:
        return decoded + ("\n" if newline and not stopped else "")
    if decoded != text or stopped:
        return UNKNOWN
    return text + ("\n" if newline else "")


_PRINTF_CONVERSION = re.compile(r"%([-+ #0']*)(\d*|\*)(?:\.(\d*|\*))?([a-zA-Z%])")


def _shell_number(value: str) -> Optional[int]:
    """A printf numeric argument as the shell reads it: decimal, 0x hex, 0 octal or 'c for a character code."""
    value = value.strip()
    if value[:1] in ("'", '"'):
        return ord(value[1]) if len(value) > 1 else 0
    try:
        if re.fullmatch(r"[-+]?0[xX][0-9A-Fa-f]+", value):
            return int(value, 16)
        if re.fullmatch(r"[-+]?0[0-7]+", value):
            return int(value, 8)
        if re.fullmatch(r"[-+]?\d+", value):
            return int(value)
    except ValueError:
        return None
    return None


def _printf_conversion(match: "re.Match[str]", values: List[str]) -> Tuple[Optional[str], bool]:
    """One `%...` of a printf format with its argument. Returns (text or UNKNOWN, stopped)."""
    flags, width, precision, kind = match.groups()
    if kind == "%":
        return "%", False
    if "*" in (width, precision or "") or "'" in flags:
        return UNKNOWN, False
    value = values.pop(0) if values else ""
    spec = "%" + flags + width + ("." + precision if precision is not None else "")
    if kind == "s":
        return (spec + "s") % value, False
    if kind == "b":
        decoded, stopped = _decode_escapes(value, "echo")
        return ((spec + "s") % decoded if decoded is not None else UNKNOWN), stopped
    if kind == "c":
        return ("%" + flags.replace("0", "") + width + "s") % value[:1], False
    if kind in "dioxXu":
        number = _shell_number(value) if value else 0
        if number is None or (kind in "oxXu" and number < 0) or ("#" in flags and kind == "o"):
            return UNKNOWN, False
        return (spec + ("d" if kind in "iu" else kind)) % number, False
    if kind in "eEfFgG":
        try:
            number = float(value) if value else 0.0
        except ValueError:
            return UNKNOWN, False
        return (spec + kind) % number, False
    # %q quotes for the shell, %a is hex floating point, %(...)T is a date:
    # each prints text that is not the argument as written.
    return UNKNOWN, False


def _printf(args: List[str]) -> Optional[str]:
    """What printf prints, conversions and escapes applied the way the shell applies them."""
    index = 0
    if args[:1] == ["--"]:
        index = 1
    if args[index:index + 1] == ["-v"]:
        return ""  # assigns to a variable, prints nothing
    if index >= len(args):
        return ""
    template, values = args[index], list(args[index + 1:])
    out: List[str] = []
    # The format is used again while arguments remain, and once whatever happens.
    for _ in range(len(values) + 1):
        consumed = len(values)
        position = 0
        for match in _PRINTF_CONVERSION.finditer(template):
            literal, stopped = _decode_escapes(template[position:match.start()], "printf")
            if literal is UNKNOWN:
                return UNKNOWN
            out.append(literal)
            converted, stopped = _printf_conversion(match, values)
            if converted is UNKNOWN:
                return UNKNOWN
            out.append(converted)
            if stopped:
                return "".join(out)
            position = match.end()
        literal, _ = _decode_escapes(template[position:], "printf")
        if literal is UNKNOWN:
            return UNKNOWN
        out.append(literal)
        if not values or len(values) == consumed or sum(len(piece) for piece in out) > MAX_CODE_CHARS:
            break
    return "".join(out)


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


_SED_CONTROL = {"n": "\n", "t": "\t", "a": "\a", "f": "\f", "v": "\v", "r": "\r"}


def _sed_text(text: str, replacement: bool) -> Optional[str]:
    """A sed replacement (or a/i/c text) with GNU sed's escapes applied, or UNKNOWN.

    In a replacement `&` and `\\1` are the text that was matched, which is in
    the file rather than the command, so either makes the result UNKNOWN; so
    does an escape this does not know. `\\L`, `\\U`, `\\l`, `\\u` and `\\E`
    change case, which is how `\\LIMPORT BOTO3` becomes an import.
    """
    out: List[str] = []
    case = ""       # "L" or "U" until \E
    once = ""       # "l" or "u" for the next character only

    def emit(piece: str) -> None:
        nonlocal once
        for char in piece:
            if once:
                char = char.lower() if once == "l" else char.upper()
                once = ""
            elif case:
                char = char.lower() if case == "L" else char.upper()
            out.append(char)

    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "&" and replacement:
            return UNKNOWN
        if char != "\\":
            emit(char)
            index += 1
            continue
        code = text[index + 1:index + 2]
        index += 2
        if not code:
            return UNKNOWN
        if code in _SED_CONTROL:
            emit(_SED_CONTROL[code])
        elif code in "\\&\n/":
            emit(code)
        elif code in "LU":
            case, once = code, ""
        elif code in "lu":
            once = code
        elif code == "E":
            case = once = ""
        elif code == "c" and index < length:
            emit(chr(ord(text[index].upper()) ^ 0x40))
            index += 1
        elif code in "dox":
            digits = re.match({"d": r"[0-9]{1,3}", "o": r"[0-7]{1,3}", "x": r"[0-9A-Fa-f]{1,2}"}[code], text[index:index + 3])
            if not digits:
                return UNKNOWN
            emit(chr(int(digits.group(0), {"d": 10, "o": 8, "x": 16}[code]) & 0xFF))
            index += len(digits.group(0))
        elif not replacement and code == " ":
            emit(" ")
        else:
            return UNKNOWN  # a back-reference, or an escape nobody can be sure of
    return "".join(out)


def _sed_whole_line(pattern: str) -> bool:
    """Whether an `s` pattern always matches a whole line, so its replacement is the whole line."""
    return pattern.startswith("^") and pattern.endswith("$") and not pattern.endswith("\\$")


def _sed_line_text(script: str, index: int) -> Tuple[str, int]:
    """The text of an a, i or c command from just after the letter. Returns (raw text, index after it).

    `1i\\` followed by a newline puts the text on the next line, and a line
    ending in a backslash continues onto the next one, as GNU sed reads them.
    """
    while index < len(script) and script[index] in " \t":
        index += 1
    if script.startswith("\\\n", index):
        index += 2
    elif script.startswith("\\", index):
        index += 1
    lines: List[str] = []
    while True:
        end = script.find("\n", index)
        line = script[index:] if end == -1 else script[index:end]
        index = len(script) if end == -1 else end + 1
        trailing = len(line) - len(line.rstrip("\\"))
        if trailing % 2 == 1 and end != -1:
            lines.append(line[:-1])
            continue
        lines.append(line)
        return "\n".join(lines), index


def _sed_script(script: str) -> Tuple[Optional[str], bool, List[str]]:
    """What a sed script adds, whether any of it replaces part of a line, and the files its `w` writes.

    Returns (added text or UNKNOWN, fragment, files). Deleting lines and
    printing add nothing. `y` transliterates, `r` reads another file in and `e`
    runs a command, so the text they add is not in the script and the answer is
    UNKNOWN rather than a guess. `w file`, as a command or an `s` flag, writes
    that file whether or not -i is given.
    """
    added: List[str] = []
    files: List[str] = []
    fragment = False
    unknown = False
    index = 0
    steps = 0
    length = len(script)
    while index < length:
        steps += 1
        if steps > 1_000:
            return UNKNOWN, fragment, files
        while index < length and script[index] in " \t\n;{}":
            index += 1
        if index >= length:
            break
        index = _sed_address(script, index)
        if index >= length:
            break
        command = script[index]
        index += 1
        if command == "s":
            if index >= length:
                return UNKNOWN, fragment, files
            delimiter = script[index]
            pattern, index = _sed_delimited(script, index + 1, delimiter)
            replacement, index = _sed_delimited(script, index, delimiter)
            while index < length and script[index] in "gpiImM0123456789":
                index += 1
            if index < length and script[index] == "e":
                return UNKNOWN, fragment, files
            if index < length and script[index] == "w":
                end = script.find("\n", index)
                files.append(script[index + 1:end if end != -1 else length].strip())
                index = length if end == -1 else end
            text = _sed_text(replacement, replacement=True)
            if text is UNKNOWN:
                unknown = True
            else:
                added.append(text)
                fragment = fragment or not _sed_whole_line(pattern)
        elif command in "aic":
            raw, index = _sed_line_text(script, index)
            text = _sed_text(raw, replacement=False)
            if text is UNKNOWN:
                unknown = True
            else:
                added.append(text)
        elif command in "wW":
            end = script.find("\n", index)
            files.append(script[index:end if end != -1 else length].strip())
            index = length if end == -1 else end
        elif command in "dDpPnNqQgGhHxlz=":
            continue
        elif command in "btT:":
            while index < length and script[index] not in ";\n":
                index += 1
        else:
            return UNKNOWN, fragment, files
    return (UNKNOWN if unknown else "\n".join(added)), fragment, [name for name in files if name]


def sed_added_text(script: str) -> Optional[str]:
    """What a sed script adds to a file: its replacements and its a/i/c text. UNKNOWN if it can add anything else."""
    return _sed_script(script)[0]


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
    # GNU sed joins every -e script with a newline and reads the result as one
    # program, so `-e '1i\' -e 'import boto3'` is an insert of `import boto3`.
    # Read one by one, the second was taken for an `i` command.
    if any(script is None for script in scripts):
        content, fragment, written = UNKNOWN, False, []
    else:
        content, fragment, written = _sed_script("\n".join(script for script in scripts if script is not None))
    for target in written:
        _write(state, target, UNKNOWN, "sed w")
    if not in_place:
        return
    for target in files:
        _write(state, target, content, "sed -i", fragment=fragment)


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
    fragment = False
    pieces: Optional[List[str]] = []
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
            pattern, after = _sed_delimited(part, 2, delimiter)
            replacement, end = _sed_delimited(part, after, delimiter)
            text = _perl_text(replacement) if re.fullmatch(r"[gimsxo]*", part[end:]) else UNKNOWN
            if text is UNKNOWN:
                pieces = None
                break
            pieces.append(text)
            fragment = fragment or not _sed_whole_line(pattern)
        if pieces is None:
            break
    if pieces is not None:
        content = "\n".join(pieces)
    for target in files:
        _write(state, target, content, "perl -i", fragment=fragment)


_PERL_SIMPLE = {"t": "\t", "n": "\n", "r": "\r", "f": "\f", "b": "\b", "a": "\a", "e": "\x1b", "\\": "\\", "/": "/"}


def _perl_text(text: str) -> Optional[str]:
    """A Perl replacement read as the double-quoted string it is, or UNKNOWN.

    `$` and `@` interpolate a variable or a match, which is not in the command.
    Escapes are applied where they are certain, and case changes with them;
    anything else is UNKNOWN rather than a guess.
    """
    if "$" in text or "@" in text:
        return UNKNOWN
    out: List[str] = []
    case = ""
    once = ""

    def emit(piece: str) -> None:
        nonlocal once
        for char in piece:
            if once:
                char = char.lower() if once == "l" else char.upper()
                once = ""
            elif case:
                char = char.lower() if case == "L" else char.upper()
            out.append(char)

    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "\\":
            emit(char)
            index += 1
            continue
        code = text[index + 1:index + 2]
        index += 2
        if not code:
            return UNKNOWN
        if code in _PERL_SIMPLE:
            emit(_PERL_SIMPLE[code])
        elif code in "LU":
            case, once = code, ""
        elif code in "lu":
            once = code
        elif code == "E":
            case = once = ""
        elif code == "x":
            braced = re.match(r"\{([0-9A-Fa-f]{1,6})\}", text[index:index + 8])
            digits = braced or re.match(r"[0-9A-Fa-f]{1,2}", text[index:index + 2])
            if not digits:
                return UNKNOWN
            emit(chr(int(digits.group(1) if braced else digits.group(0), 16)))
            index += len(digits.group(0))
        elif code == "0":
            digits = re.match(r"0[0-7]{0,2}", text[index - 1:index + 2]).group(0)
            emit(chr(int(digits, 8)))
            index += len(digits) - 1
        elif code == "c" and index < length:
            emit(chr(ord(text[index].upper()) ^ 0x40))
            index += 1
        else:
            return UNKNOWN  # \1 is a back-reference; anything else is not certain
    return "".join(out)


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


_RECURSIVE_LETTERS = {"cp": "rRa", "rsync": "ra"}


def _inside_project(path: str, state: _State) -> bool:
    """Whether a source is a literal path inside the project: relative, readable, not climbing out."""
    resolved = resolve(path, state.cwd)
    return bool(resolved) and not is_opaque(resolved) and BACKSLASH not in resolved and not _is_absolute(resolved) \
        and resolved != ".." and not resolved.startswith("../")


def _copy_like(name: str, args: List[str], state: _State, route: Optional[str] = None) -> None:
    """cp, mv, install, ln and rsync: the destination is written, and for mv the source goes.

    A copy that can bring a whole directory with it (`cp -r`, `rsync -a`, `mv`
    of what may be a directory, `ln -s` to one) also writes every file beneath
    its destination, which is recorded as a tree. From outside the project, or
    from a source nobody can name, anything can be in that tree. From inside
    the project, the files were already there and already governed where they
    stood, so what matters is only whether the destination itself sits where a
    rule reaches: `cp -r src/infra src/domain/`, not `cp -r build/ dist/`.
    """
    route = route or name
    values = set(_COPY_VALUE_OPTIONS.get(name, ()))
    operands: List[str] = []
    target_directory: Optional[str] = None
    no_target_directory = False
    directories_only = False
    recursive = False
    symbolic = False
    deletes_extra = False
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
        elif arg in ("--recursive", "--archive"):
            recursive = True
        elif arg == "--symbolic":
            symbolic = True
        elif name == "rsync" and (arg == "--del" or arg.startswith("--delete")):
            deletes_extra = True
        elif not arg.startswith("--"):
            letters = arg[1:]
            recursive = recursive or any(letter in _RECURSIVE_LETTERS.get(name, "") for letter in letters)
            symbolic = symbolic or (name == "ln" and "s" in letters)
        index += 1
    if directories_only:
        return
    if name == "rsync":
        # host:path is another machine; C:/path is a drive letter, not a host.
        operands = [op for op in operands if not re.match(r"^[^/\\]+:", op) or re.match(r"^[A-Za-z]:[/\\]", op)]

    destination: Optional[str] = None
    if name == "ln" and len(operands) == 1 and not target_directory:
        sources, destinations = operands, [[_basename(operands[0])]]
        destination = "."
    elif target_directory:
        sources = operands
        destinations = [[target_directory.rstrip("/") + "/" + _basename(source)] for source in sources]
        destination = target_directory
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

    # The tree. A destination that plainly names a file brings no directory
    # with it, and one outside the project (absolute or under ~) is not the
    # project's: a tree copied out to /tmp is a backup, not a write.
    carries_tree = recursive or name == "mv" or (name == "ln" and symbolic)
    single_file = len(sources) == 1 and not target_directory and _looks_like_file(destination or "")
    if carries_tree and destination is not None and not single_file:
        outside = not all(_inside_project(source, state) for source in sources)
        tail = EXPANSION if outside else _ANY_NAME
        base = destination.replace("\\", "/").rstrip("/") or "/"
        into_directory = bool(target_directory) or destination.endswith("/") or destination in (".", "..") or len(sources) > 1
        roots: List[str] = []
        for source in sources:
            name_of = _basename(source)
            # `rsync -a src/ dest` and `cp -r src/. dest` bring the contents,
            # not the directory, so the tree starts at the destination itself.
            contents = name_of in ("", ".") or (name == "rsync" and source.endswith("/"))
            if name == "ln" and len(operands) == 1 and not target_directory:
                roots.append(name_of)
            elif contents:
                roots.append(base)
            elif into_directory:
                roots.append(base + "/" + name_of)
            else:
                roots.extend((base, base + "/" + name_of))
        for place in dict.fromkeys(roots):
            if not place or _is_absolute(place):
                continue
            # From inside the project the question is only where the tree
            # lands. When that is an expansion, `cp -r src "$TMP/"`, it cannot
            # be answered, and the files it carries were governed where they
            # stood, so the tree is not recorded rather than refused on a guess.
            if not outside and is_opaque(resolve(place, state.cwd)):
                continue
            _write(state, place + "/" + tail, UNKNOWN, route, tree=True)
    if deletes_extra and destination is not None and not _is_absolute(destination):
        # `rsync --delete` removes whatever the destination has that the source
        # lacks, which at the project root includes the hooks' own files.
        _write(state, destination.rstrip("/") + "/" + EXPANSION, UNKNOWN, route, deletes=True, tree=True)

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
        target = path if _is_absolute(path) else prefix + path
        _write(state, target, UNKNOWN if deletes else added, route, deletes=deletes)


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
    directory = ""
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
        elif name in ("--input", "--output", "--directory") and value:
            if name == "--input":
                patch_file = value
            elif name == "--output":
                output = value
            else:
                directory = value
        elif re.fullmatch(r"-[iod].+", arg):
            if arg[1] == "i":
                patch_file = arg[2:]
            elif arg[1] == "o":
                output = arg[2:]
            else:
                directory = arg[2:]
        elif arg in takes_value and index + 1 < len(args):
            value = args[index + 1]
            if arg == "-p" and value.isdigit():
                strip = int(value)
            elif arg in ("-i", "--input"):
                patch_file = value
            elif arg in ("-o", "--output"):
                output = value
            elif arg in ("-d", "--directory"):
                directory = value
            index += 1
        elif not arg.startswith("-") or arg == "-":
            operands.append(arg)
        index += 1
    if patch_file is None and len(operands) >= 2:
        patch_file = operands[1]
    # `patch -d DIR` changes into DIR before doing anything else, so every
    # path it reads or writes, the patch file included, starts there.
    prefix = directory.rstrip("/") + "/" if directory else ""
    if patch_file and not _is_absolute(patch_file.replace("\\", "/")):
        patch_file = prefix + patch_file
    text = _known(state, patch_file) if patch_file else (stdin if has_stdin else UNKNOWN)
    explicit = output or (operands[0] if operands else None)
    if explicit:
        explicit = explicit if _is_absolute(explicit.replace("\\", "/")) else prefix + explicit
        if text is None:
            _write(state, explicit, UNKNOWN, "patch")
        else:
            added = "\n".join(entry[1] for entry in diff_writes(text, strip, reverse))
            _write(state, explicit, added, "patch")
        return
    _apply_diff(state, text, "patch", strip, reverse, prefix)


# --- git: writes and the hooks it can be told to skip ----------------------------------

_GIT_GLOBAL_VALUES = ("-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--exec-path", "--config-env", "--attr-source")
_COMMIT_VALUES = (
    "-m", "-F", "-C", "-c", "-t", "--message", "--file", "--reuse-message", "--reedit-message",
    "--fixup", "--squash", "--template", "--cleanup", "--trailer", "--author", "--date", "--pathspec-from-file",
)
_CONFIG_READS = ("--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "get", "list")
_CONFIG_WRITES = (
    "--unset", "--unset-all", "--add", "--replace-all", "--rename-section", "--remove-section", "--edit", "-e",
    "set", "unset", "edit", "rename-section", "remove-section",
)
# Options of `git config` that take a value of their own and name no key.
_CONFIG_VALUES = ("-f", "--file", "--blob", "-t", "--type", "--default", "--comment", "--value", "--fixed-value")
# The files the installer lists in .git/info/exclude, which `git clean -x`
# removes with the rest of what is ignored.
_IGNORED_GOVERNANCE = (".threefold.json", ".claude/settings.local.json", ".codex/hooks.json", ".agents/hooks.json")


def _is_hooks_path(key: str) -> bool:
    """A setting that decides which hooks git runs, or one whose name cannot be read.

    `include.path` and `includeIf.*.path` pull in another configuration file,
    which can set core.hooksPath where no command shows it.
    """
    lowered = key.strip().lower()
    if is_opaque(lowered):
        return True
    return lowered.startswith("core.hookspath") or lowered.startswith("include.path") or (
        lowered.startswith("includeif.") and ".path" in lowered
    )


def git_environment_tampering(name: str, value: str) -> Optional[str]:
    """Why setting this environment variable turns git's hooks off, or None.

    Checked for every assignment in the command, not only on the git command
    itself: `export GIT_CONFIG_KEY_0=core.hooksPath ...; git commit` sets it one
    command earlier.
    """
    if name == "GIT_CONFIG_PARAMETERS" and ("core.hookspath" in value.lower() or "include" in value.lower() or is_opaque(value)):
        return "GIT_CONFIG_PARAMETERS sets core.hooksPath"
    if re.fullmatch(r"GIT_CONFIG_KEY_\d+", name) and _is_hooks_path(value):
        return f"{name} sets core.hooksPath"
    if name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG"):
        return f"{name} points git at a configuration file the service cannot read"
    return None


def _config_operands(rest: List[str]) -> List[str]:
    """The words of a `git config` that are neither options nor option values, subcommand word dropped."""
    operands: List[str] = []
    index = 0
    while index < len(rest):
        arg = rest[index]
        if arg in _CONFIG_VALUES:
            index += 2
            continue
        if not arg.startswith("-"):
            operands.append(arg)
        index += 1
    if operands and operands[0] in ("get", "list", "set", "unset", "edit", "rename-section", "remove-section"):
        operands = operands[1:]
    return operands


def _config_is_read(rest: List[str]) -> bool:
    """Whether a `git config` only reads: `git config core.hooksPath`, `--get`, `--list`.

    With no action named, one operand is a read and two are a write, so
    `git config core.hooksPath` looks and `git config core.hooksPath x` sets.
    """
    if any(arg in _CONFIG_WRITES or arg.startswith(("--unset", "--replace-all", "--add")) for arg in rest):
        return False
    if any(arg in _CONFIG_READS for arg in rest):
        return True
    return len(_config_operands(rest)) == 1


def _git_clean(rest: List[str], state: _State) -> None:
    """`git clean -x` or `-X` removes ignored files, and the installer's files are ignored ones."""
    letters = "".join(arg[1:] for arg in rest if arg.startswith("-") and not arg.startswith("--"))
    if "n" in letters or "--dry-run" in rest or not ("x" in letters or "X" in letters):
        return
    excluded: List[str] = []
    pathspecs: List[str] = []
    index = 0
    while index < len(rest):
        arg = rest[index]
        if arg == "-e" and index + 1 < len(rest):
            excluded.append(rest[index + 1])
            index += 2
            continue
        if arg.startswith("--exclude="):
            excluded.append(arg.split("=", 1)[1])
        elif arg.startswith("-e") and len(arg) > 2 and not arg.startswith("--"):
            excluded.append(arg[2:])
        elif not arg.startswith("-"):
            pathspecs.append(arg)
        index += 1
    if pathspecs and not any(is_opaque(spec) or (resolve(spec, state.cwd) or "") in (".", "") or spec.startswith(":/") for spec in pathspecs):
        return  # a subdirectory only; the hooks' files live at the root
    for relative in _IGNORED_GOVERNANCE:
        # With -x, an -e pattern still counts as ignored and keeps its file;
        # with -X it only adds to what is removed.
        kept = "X" not in letters and any(
            fnmatch.fnmatch(relative, pattern.lstrip("/")) or fnmatch.fnmatch(_basename(relative), pattern.lstrip("/"))
            or relative.startswith(pattern.strip("/") + "/")
            for pattern in excluded
        )
        if not kept:
            _write(state, relative, UNKNOWN, "git clean -x", deletes=True)


def _git(args: List[str], assignments: List[str], stdin: Optional[str], has_stdin: bool, state: _State) -> None:
    tampering = state.result.tampering
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-c" or arg == "--config-env":
            value = args[index + 1] if index + 1 < len(args) else ""
            if _is_hooks_path(value):
                tampering.append(f"git {arg} {display(value.split('=', 1)[0])}=... points the hooks somewhere else")
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
        if "--edit" in rest or "-e" in rest or "edit" in rest[:1]:
            tampering.append("git config --edit can set core.hooksPath where no command shows it")
        elif not _config_is_read(rest) and _is_hooks_path((_config_operands(rest) or [""])[0]):
            tampering.append("git config core.hooksPath points the hooks somewhere else")
    elif subcommand == "clean":
        _git_clean(rest, state)
    elif subcommand == "apply":
        _git_apply(rest, stdin, has_stdin, state)
    elif subcommand == "rm":
        if not any(arg in ("-n", "--dry-run") for arg in rest):
            _delete([arg for arg in rest if arg not in ("-r", "-f", "-q", "--cached", "--force", "--quiet")], state, "git rm")
    elif subcommand == "mv":
        if not any(arg in ("-n", "--dry-run") for arg in rest):
            _copy_like("mv", [arg for arg in rest if arg not in ("-f", "-k", "-v", "--force", "--verbose")], state, "git mv")


# --- code handed to an interpreter --------------------------------------------------------

def _call_name(node: ast.AST, aliases: Optional[Dict[str, str]] = None) -> str:
    """`open`, `os.remove`, `shutil.copy`: the dotted name a call is made through.

    With `aliases`, a name imported or bound under another one is read as the
    one it stands for: after `from pathlib import Path as P`, `P(...)` is Path.
    """
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif parts:
        parts.append("?")
    name = ".".join(reversed(parts))
    if aliases:
        head, dot, tail = name.partition(".")
        if head in aliases:
            name = aliases[head] + dot + tail
    return name


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

    # Modules whose functions are named "module.function" in the sets above.
    ALIASED_MODULES = frozenset(("os", "os.path", "shutil", "io", "codecs", "builtins"))

    def __init__(self, tree: ast.AST, argv: Sequence[str]) -> None:
        self.tree = tree
        self.argv = list(argv)
        self.parents: Dict[int, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[id(child)] = parent
        self.bindings = self._bindings()
        self.aliases = self._aliases()

    def _aliases(self) -> Dict[str, str]:
        """Names that stand for a known function or module under another spelling.

        `from pathlib import Path as P; P('src/domain/x.py').write_text(...)`
        was found as a write with no path, and a write with no path was dropped.
        """
        aliases: Dict[str, str] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    if node.module == "pathlib":
                        target = alias.name
                    elif node.module in self.ALIASED_MODULES:
                        target = f"{node.module}.{alias.name}"
                    else:
                        continue
                    if target != local:
                        aliases[local] = target
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname and alias.asname != alias.name:
                        aliases[alias.asname] = alias.name
        # A name bound once to another name: `W = Path`, `o = open`.
        for name, value in self.bindings.items():
            if name not in aliases and isinstance(value, (ast.Name, ast.Attribute)):
                target = _call_name(value, aliases)
                if target and target != name and "?" not in target:
                    aliases[name] = target
        return aliases

    def name_of(self, node: ast.AST) -> str:
        return _call_name(node, self.aliases)

    @staticmethod
    def argument(call: ast.Call, position: int, *names: str) -> Optional[ast.AST]:
        """A call's argument by position or by keyword: `open(file=..., mode=...)` is still an open."""
        if len(call.args) > position and not any(isinstance(arg, ast.Starred) for arg in call.args[:position + 1]):
            return call.args[position]
        for keyword in call.keywords:
            if keyword.arg in names:
                return keyword.value
        return None

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
        if isinstance(node, ast.Subscript) and self.name_of(node.value) == "sys.argv":
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, int) and 1 <= index.value <= len(self.argv):
                return self.argv[index.value - 1], True
            return None, False
        if isinstance(node, ast.Call):
            name = self.name_of(node.func)
            if name in self.PATH_WRAPPERS or name.endswith(".Path") or name.split(".")[-1] in self.PATH_WRAPPERS and name.startswith("pathlib."):
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
                    if isinstance(printed, ast.Call) and self.name_of(printed.func) == "print":
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
        """Every write the code makes. A path that cannot be worked out is None.

        None is kept only for calls that can be nothing but a file write: open()
        for writing, write_text, shutil and os by their full names. A method that
        merely shares a name with one, such as `df.rename(columns)`, is reported
        only when its path can be read, so a pandas call is never a refusal.
        """
        found: List[Tuple[Optional[str], Optional[str], str, bool]] = []
        argument = self.argument

        def path_of(node: Optional[ast.AST]) -> Optional[str]:
            value, _ = self.text(node)
            return value

        def unambiguous(path: Optional[str], content: Optional[str], route: str, deletes: bool = False) -> None:
            found.append((path, content, route, deletes))

        def if_named(path: Optional[str], content: Optional[str], route: str, deletes: bool = False) -> None:
            if path is not None:
                found.append((path, content, route, deletes))

        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            name = self.name_of(node.func)
            method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
            if name in self.OPENERS:
                target = argument(node, 0, "file", "filename")
                if target is not None and any(letter in self._mode(node, 1) for letter in "wax+"):
                    unambiguous(path_of(target), self._handle_content(node), "python open()")
            elif method == "open" and receiver is not None:
                if any(letter in self._mode(node, 0) for letter in "wax+"):
                    if_named(path_of(receiver), self._handle_content(node), "python Path.open()")
            elif method in ("write_text", "write_bytes") and receiver is not None:
                data = argument(node, 0, "data")
                unambiguous(path_of(receiver), self.content(data) if data is not None else UNKNOWN, f"python {method}()")
            elif method == "touch" and receiver is not None:
                if_named(path_of(receiver), "", "python touch()")
            elif name in self.COPIERS and argument(node, 1, "dst") is not None:
                report = unambiguous if "." in name else if_named
                report(path_of(argument(node, 1, "dst")), UNKNOWN, f"python {name}()")
            elif name in self.MOVERS and "." in name and argument(node, 1, "dst") is not None:
                unambiguous(path_of(argument(node, 1, "dst")), UNKNOWN, f"python {name}()")
                if_named(path_of(argument(node, 0, "src")), UNKNOWN, f"python {name}()", True)
            elif method in ("rename", "replace") and receiver is not None and len(node.args) + len(node.keywords) == 1:
                # Path.rename(target) takes one argument; str.replace takes two,
                # and every `line.replace('\n', '')` in a one-liner is not a move.
                if_named(path_of(argument(node, 0, "target")), UNKNOWN, f"python Path.{method}()")
                if_named(path_of(receiver), UNKNOWN, f"python Path.{method}()", True)
            elif name in self.LINKERS and argument(node, 1, "dst") is not None:
                unambiguous(path_of(argument(node, 1, "dst")), UNKNOWN, f"python {name}()")
            elif method in ("symlink_to", "hardlink_to") and receiver is not None:
                if_named(path_of(receiver), UNKNOWN, f"python Path.{method}()")
            elif name in self.REMOVERS and argument(node, 0, "path") is not None:
                if_named(path_of(argument(node, 0, "path")), UNKNOWN, f"python {name}()", True)
            elif method in ("unlink", "rmdir") and receiver is not None and not name.startswith("os."):
                if_named(path_of(receiver), UNKNOWN, f"python Path.{method}()", True)
            elif name == "os.open" and argument(node, 1, "flags") is not None:
                flags = ast.dump(argument(node, 1, "flags"))
                if any(flag in flags for flag in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC")):
                    unambiguous(path_of(argument(node, 0, "path")), UNKNOWN, "python os.open()")
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
        # A path that is not a literal is reported as None, so the write is
        # judged as one whose file cannot be named rather than dropped. Only
        # for the names that belong to fs alone: `rename` and `unlink` are
        # methods of half the objects in JavaScript.
        if function in ("writeFileSync", "appendFileSync", "writeFile", "appendFile", "outputFileSync", "outputFile"):
            found.append((first, second, route, False))
        elif function == "createWriteStream":
            found.append((first, UNKNOWN, route, False))
        elif function in ("copyFileSync", "copyFile", "cpSync", "symlinkSync", "linkSync"):
            found.append((second, UNKNOWN, route, False))
        elif function in ("renameSync", "rename"):
            if second is not None or function == "renameSync":
                found.append((second, UNKNOWN, route, False))
            if first is not None:
                found.append((first, UNKNOWN, route, True))
        elif function == "truncateSync":
            found.append((first, "", route, False))
        elif first is not None:
            found.append((first, UNKNOWN, route, True))
    return found


def _record(state: _State, writes: Iterable[Tuple[Optional[str], Optional[str], str, bool]]) -> None:
    """Records an interpreter's writes. One whose path cannot be read is kept, with no path.

    Dropping it was how `P('src/domain/x.py').write_text(...)` under an alias
    passed, while `git apply fix.patch`, just as unnamed, was refused. A
    deletion with no path is not kept: removing an unnamed file is judged by
    nothing but the governance paths, and those need a name.
    """
    for target, content, route, deletes in writes:
        if target:
            _write(state, target, content, route, deletes)
        elif not deletes:
            _write(state, None, content, route)


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
        # node -pe, -ep: short flags run together, one of them taking the code.
        if "-e" in eval_flags and re.fullmatch(r"-[a-zA-Z]*[ep][a-zA-Z]*", arg) and not re.search(r"[rC]", arg):
            return (args[index + 1] if index + 1 < len(args) else ""), args[index + 2:]
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
SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh", "ash", "mksh"))


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


# --- commands that run other commands on words they find -------------------------------------

_XARGS_VALUES = ("-a", "-d", "-E", "-e", "-I", "-L", "-l", "-n", "-P", "-s", "--arg-file", "--delimiter", "--eof",
                 "--replace", "--max-lines", "--max-args", "--max-procs", "--max-chars", "--process-slot-var")


def _xargs(args: List[str], state: _State) -> None:
    """`xargs CMD ARGS`: CMD runs with ARGS and words read from input, which nobody can name.

    `find src/domain -name '*.py' | xargs sed -i '1i import boto3'` edits every
    file find prints. Each word xargs supplies is an expansion, so the edit is
    judged as a write to any file the rule could cover.
    """
    replace: Optional[str] = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-I", "--replace") and index + 1 < len(args):
            replace = args[index + 1]
            index += 2
            continue
        if arg.startswith("--replace="):
            replace = arg.split("=", 1)[1] or "{}"
        elif arg == "-i" or (arg.startswith("-i") and not arg.startswith("--")):
            replace = arg[2:] or "{}"
        elif arg.startswith("-I") and len(arg) > 2:
            replace = arg[2:]
        elif arg in _XARGS_VALUES:
            index += 2
            continue
        elif not arg.startswith("-"):
            break
        index += 1
    command = args[index:] or ["echo"]
    if replace:
        command = [word.replace(replace, EXPANSION) for word in command]
    else:
        command = command + [EXPANSION]
    _run_nested(command, state.child())


def _find(args: List[str], state: _State) -> None:
    """`find ROOT ... -exec CMD {} ;`: CMD runs on every file found under ROOT."""
    roots: List[str] = []
    index = 0
    while index < len(args) and (args[index] in ("-H", "-L", "-P", "-D") or args[index].startswith("-O")):
        index += 2 if args[index] == "-D" else 1
    while index < len(args) and not args[index].startswith("-") and args[index] not in ("(", "!", ","):
        roots.append(args[index])
        index += 1
    root = (roots[0] if roots else ".").rstrip("/") or "/"
    while index < len(args):
        arg = args[index]
        if arg in ("-exec", "-execdir", "-ok", "-okdir"):
            end = index + 1
            while end < len(args) and args[end] not in (";", "+"):
                end += 1
            found = root + "/" + EXPANSION if arg in ("-exec", "-ok") else "./" + EXPANSION
            command = [word.replace("{}", found) for word in args[index + 1:end]]
            if command:
                child = state.child()
                if arg in ("-execdir", "-okdir"):
                    child.cwd = resolve(root + "/" + EXPANSION, state.cwd) or EXPANSION
                _run_nested(command, child)
            index = end + 1
            continue
        if arg in ("-fprint", "-fprint0", "-fls", "-fprintf") and index + 1 < len(args):
            _write(state, args[index + 1], UNKNOWN, f"find {arg}")
            index += 2
            continue
        index += 1


def _run_nested(argv: List[str], state: _State) -> None:
    """Runs a command another command hands its words to, with nothing on its input."""
    if state.depth > MAX_NESTED_SCRIPTS:
        state.budget.truncated = True
        return
    unwrapped = _unwrap(list(argv), [])
    if unwrapped:
        state.result.commands.append(tuple(unwrapped))
        _run(program_name(unwrapped[0]), unwrapped, _Simple(), UNKNOWN, state)


def _code_of_an_unknown_program(args: List[str], state: _State) -> None:
    """`$PYTHON -c "..."`, `"$(which node)" -e "..."`: the program is not in the command, its code is.

    What the code writes is read as shell, as Python and as JavaScript, and
    whichever it is finds its writes; the others find nothing in text that is
    not theirs.
    """
    for index, arg in enumerate(args[:-1]):
        if arg in ("-c", "-e", "--eval", "-p", "--print") or re.fullmatch(r"-[a-zA-Z]*[ce]", arg):
            code = args[index + 1]
            _record(state, python_writes(code, args[index + 2:]))
            _record(state, node_writes(code))
            _analyse_into(code, state.child())
            return


# --- the analysis -------------------------------------------------------------------------------

def _run(name: str, argv: List[str], simple: _Simple, stdin: Optional[str], state: _State) -> Optional[str]:
    """Records what one simple command writes and returns what it prints, or UNKNOWN."""
    args = argv[1:]
    has_stdin = simple.has_stdin or stdin is not None
    if is_opaque(argv[0]):
        _code_of_an_unknown_program(args, state)
        return UNKNOWN
    if name == "xargs":
        _xargs(args, state)
        return UNKNOWN
    if name == "find":
        _find(args, state)
        return UNKNOWN
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


_DECLARERS = frozenset(("export", "declare", "typeset", "local", "readonly"))


def _check_git_environment(assignments: List[str], argv: List[str], state: _State) -> None:
    """Environment that turns git's hooks off, wherever in the command it is set.

    A prefix (`VAR=x git commit`), a bare assignment and an `export` all count:
    the git command may come several segments later.
    """
    words = list(assignments)
    if argv and program_name(argv[0]) in _DECLARERS:
        words.extend(word for word in argv[1:] if _ASSIGNMENT.match(word))
    for word in words:
        name, _, value = word.partition("=")
        reason = git_environment_tampering(name, value)
        if reason and reason not in state.result.tampering:
            state.result.tampering.append(reason)


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
        argv, closer = _strip_reserved(simple.argv, simple.assignments, state)
        # `exec 3> file` and `exec > file` run no command: the redirect stays
        # open on the shell itself, and whatever the rest of the command prints
        # to it lands in the file.
        on_the_shell = bool(argv) and program_name(argv[0]) == "exec"
        argv = _unwrap(argv, simple.assignments)
        on_the_shell = on_the_shell and not argv
        _check_git_environment(simple.assignments, argv, state)
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
        elif closer or on_the_shell:
            # `done > file`, `} | tee file`: what arrives is the output of the
            # whole compound command, which is not this segment's to know.
            output = UNKNOWN
        else:
            output = ""  # `> file` alone empties the file
        for target, fd, operator in simple.outputs:
            if on_the_shell:
                content = UNKNOWN
            elif fd in ("1", "&"):
                content = output
            else:
                # Another stream. echo and printf never write to it, so what
                # arrives there is nothing; any other program's is unknown.
                content = "" if name in ("echo", "printf") or not (argv or closer) else UNKNOWN
            if on_the_shell:
                route = f"exec {operator}"
            elif simple.heredoc and name in ("cat", ""):
                route = "heredoc"
            else:
                route = f"redirect {operator}"
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
