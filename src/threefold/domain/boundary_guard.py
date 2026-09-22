"""Architectural boundary enforcement, secret leakage scanner, and command safety.

Every check here walks the whole argument structure rather than reading a fixed
list of key names. An agent does not promise to name its arguments the way we
expect, and a guard that only looks where it is convenient is a guard that can
be stepped around by renaming a field.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Iterator, List, Optional, Tuple
from threefold.domain.layering_rules import (
    DEFAULT_RULES,
    ENFORCE,
    OBSERVE,
    observed as observed_layering,
    rules_for_path,
    violations,
)
from threefold.domain.imports import LANGUAGE_BY_SUFFIX, language_for
from threefold.domain.models import ToolActionType, ToolInvocation
from threefold.domain.shell_writes import (
    ShellAnalysis,
    ShellWrite,
    analyse as analyse_shell,
    display as shell_display,
    is_governance_path,
    pattern_is_governance,
    pattern_matches_glob,
)

MAX_PATHLIKE_LENGTH = 400

# The words the contract fixes for a write the rules cannot read. An agent told
# only "refused" retries the same route; told this, it has somewhere to go.
UNREADABLE_WRITE = "use Write or Edit so the rule can read it"

# Where a command sits in a call's arguments. The same names describe_target
# reads, so the ledger and the gate agree on what "the command" is.
COMMAND_KEYS = ("command", "cmd", "script", "shell")
SHELL_TOOLS = frozenset(
    ("bash", "shell", "local_shell", "exec_command", "unified_exec", "container.exec", "run_command", "powershell")
)
# Where a command runs, relative to the project root, as the hook sends it.
# It is a place, not a file anything is written to, so it is never paired with
# content as a write target.
WORKING_DIRECTORY_KEYS = ("cwd", "workdir")


def iter_string_leaves(value: Any) -> Iterator[str]:
    """Yields every string anywhere inside an argument structure.

    Arguments nest. A secret inside `{"edits": [{"new_source": "..."}]}` is a
    secret, and scanning only the top level would miss it.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from iter_string_leaves(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from iter_string_leaves(item)


def looks_like_path(text: str) -> bool:
    """Whether a string is plausibly a filesystem path rather than prose.

    Deliberately conservative. Running the protected-path patterns over every
    string would block a comment that merely mentions a dotfile. It is a guess
    about a loose string, so it is never asked about a value the call itself
    filed under a path key: there `named_path` decides, and a file name with a
    space in it is a file name.
    """
    if not text or len(text) > MAX_PATHLIKE_LENGTH or "\n" in text:
        return False
    if " " in text.strip():
        return False
    return (
        "/" in text
        or "\\" in text
        or text.startswith(".")
        or bool(re.fullmatch(r"[\w.\-]+\.\w{1,6}", text))
    )


def named_path(text: str) -> bool:
    """Whether a value given under an explicit path key names a file.

    Nothing is guessed here. The agent said `file_path`, so the string is the
    path, whatever it contains. `looks_like_path` used to decide this too, and
    it says no to any string with a space in it, so `src/domain/order line.py`
    paired with no content and every layering and governance check below it was
    skipped, while the same write sent as a heredoc was refused. Only the two
    limits that make a string unusable as a path are kept: a length bound, so a
    whole file's text under a mistyped key cannot be walked as one, and a
    newline, which no single path carries.
    """
    return bool(text) and len(text) <= MAX_PATHLIKE_LENGTH and "\n" not in text and bool(text.strip())


# Which gate found a thing wrong with a call. The application groups rules by
# these: a project promotes some and keeps others watching, so which gate spoke
# has to be a fact the guard states, not a word read back out of its sentence.
CREDENTIAL_FOUND = "credential"
PROTECTED_PATH_FOUND = "protected-path"
GOVERNANCE_FOUND = "governance"
TAMPERING_FOUND = "tampering"
LAYERING_FOUND = "layering"
UNREADABLE_FOUND = "unreadable"
DESTRUCTIVE_FOUND = "destructive"
COMMAND_PATH_FOUND = "command-path"


@dataclass(frozen=True)
class BoundaryFinding:
    """One thing the guard found wrong with a call, and which gate found it.

    `reason` is the sentence the call's maker is given, unchanged from the one
    the guard has always written. Everything beside it is what the gate saw:
    `rule_id` for a layering rule, `path` for the file, `label` for the kind of
    credential, `detail` for the text a pattern matched, and `from_shell` for a
    finding read out of a command rather than out of the call's own fields.
    """

    kind: str
    reason: str
    rule_id: str = ""
    path: str = ""
    label: str = ""
    detail: str = ""
    from_shell: bool = False


class SecretScanner:
    """Catches credentials in an agent's arguments before the call is made."""

    PATTERNS: List[Tuple[str, re.Pattern]] = [
        ("AWS_ACCESS_KEY", re.compile(r"(?<![A-Z0-9])((?:AKIA|ASIA)[0-9A-Z]{16})(?![A-Z0-9])")),
        ("AWS_SECRET_KEY", re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}")),
        ("GITHUB_TOKEN", re.compile(r"(?<![A-Za-z0-9_])(gh[pousr]_[A-Za-z0-9_]{36,255})(?![A-Za-z0-9_])")),
        ("OPENAI_KEY", re.compile(r"(?<![A-Za-z0-9_])sk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
        ("ANTHROPIC_KEY", re.compile(r"(?<![A-Za-z0-9_])sk-ant-[A-Za-z0-9_\-]{20,}")),
        ("SLACK_TOKEN", re.compile(r"(?<![A-Za-z0-9_])xox[abposr]-[A-Za-z0-9\-]{10,}")),
        ("GOOGLE_API_KEY", re.compile(r"(?<![A-Za-z0-9_])AIza[A-Za-z0-9_\-]{35}(?![A-Za-z0-9_\-])")),
        ("JWT", re.compile(r"(?<![A-Za-z0-9_])eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
        ("GENERIC_API_KEY", re.compile(r"(?i)(api[_-]?key|secret[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{20,}['\"]")),
        ("PRIVATE_KEY_HEADER", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ]

    @classmethod
    def scan_payload(cls, text: str) -> Tuple[bool, str]:
        """Scans one string for credentials. Returns (is_clean, description)."""
        for label, pattern in cls.PATTERNS:
            if pattern.search(text):
                return False, f"Sensitive credential detected: {label}"
        return True, "No credentials detected"

    @classmethod
    def scan_arguments(cls, arguments: Any) -> Tuple[bool, str]:
        """Scans every string in an argument structure, however deeply nested.

        The previous version scanned `str(arguments)`, which renders a newline
        as the two characters backslash and n. A key on its own line then had a
        word character in front of it, so the word boundary in the pattern never
        matched and the secret passed.
        """
        for leaf in iter_string_leaves(arguments):
            is_clean, message = cls.scan_payload(leaf)
            if not is_clean:
                return False, message
        return True, "No credentials detected"


class ArchitecturalBoundaryGuard:
    """Enforces Clean Architecture layer boundaries and protected paths."""

    # Trailing lookaheads rather than a list of allowed preceding characters.
    # The earlier patterns required whitespace or a slash in front, so a shell
    # redirect like `curl -d @.env` put an at sign there and slipped past.
    PROTECTED_PATH_PATTERNS: List[re.Pattern] = [
        # `.env.example` and its spellings are the committed template: a file
        # whose whole purpose is to name the variables without their values.
        # Refusing it refused `git diff -- .env.example` and reading the
        # template before writing the real one.
        re.compile(r"\.env(?!\.(?:example|sample|template|dist|defaults)(?![A-Za-z0-9_.]))(?![A-Za-z0-9_])", re.IGNORECASE),
        re.compile(r"\.git(?![A-Za-z0-9_])", re.IGNORECASE),
        # A credential store is a place: `secrets/`, `.secrets`,
        # `secrets.json`, `config/secrets.yml`. The bare word on its own is a
        # search term, and matching it refused `rg -n "secrets" docs/`.
        re.compile(r"(?<![A-Za-z0-9_])secrets(?=[/\\.])|(?<=[/\\.])secrets(?![A-Za-z0-9_])", re.IGNORECASE),
        # A key file has a name in front of the extension (`server.key`,
        # `deploy.pem`). A bare `.key` is a field, and matching it refused
        # `jq -r .key config.json`.
        re.compile(r"(?<=[A-Za-z0-9_\-])\.(pem|key|pfx|pkcs12|p12)(?![A-Za-z0-9_])", re.IGNORECASE),
        re.compile(r"\.ssh(?![A-Za-z0-9_])", re.IGNORECASE),
        re.compile(r"\.aws(?![A-Za-z0-9_])", re.IGNORECASE),
        re.compile(r"(?<![A-Za-z0-9_])id_(rsa|ed25519|ecdsa)(?![A-Za-z0-9_])", re.IGNORECASE),
        re.compile(r"(^|[/\\])domain([/\\])core([/\\])frozen_", re.IGNORECASE),
    ]

    # What a command line must not name. The repository's own `.git` is not a
    # credential store, and read-only commands name it all the time to leave it
    # out (`find . -path ./.git -prune`, `grep --exclude-dir=.git`, `tree -I
    # .git`): the benchmark of 2026-09-22 found an agent refused for exactly
    # that, in a task where nothing was wrong. Writes under `.git` are still
    # refused by the governance check on what a command writes (2b and 2c
    # below), and deleting it is a destructive command. `.git-credentials`, the
    # file git's store helper keeps passwords in, stays protected.
    COMMAND_PROTECTED_PATTERNS: List[re.Pattern] = [
        pattern for pattern in PROTECTED_PATH_PATTERNS if pattern.pattern != r"\.git(?![A-Za-z0-9_])"
    ] + [re.compile(r"\.git-credentials(?![A-Za-z0-9_])", re.IGNORECASE)]

    DESTRUCTIVE_COMMANDS: List[re.Pattern] = [
        re.compile(r"\brm\s+-rf\s+(/|\*|~|\$HOME)", re.IGNORECASE),
        # The repository's history, whatever the order or spelling of the flags.
        re.compile(
            r"\brm\s+(-[A-Za-z]*\s+|--[a-z-]+\s+)*(-[A-Za-z]*r[A-Za-z]*|--recursive)\s+(-[A-Za-z]*\s+|--[a-z-]+\s+)*"
            r"(\./)?\.git/?(\s|$|;|&|\|)",
            re.IGNORECASE,
        ),
        re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),
        re.compile(r"\bgit\s+push\s+.*(--force|-f)\b", re.IGNORECASE),
        re.compile(r"\bdrop\s+database\b", re.IGNORECASE),
    ]

    DOMAIN_DIRECTORY = re.compile(r"(^|[/\\])domain([/\\]|$)", re.IGNORECASE)

    @classmethod
    def is_forbidden_file_access(cls, path: str) -> bool:
        """Whether a path breaches protected path governance."""
        return any(pattern.search(path) for pattern in cls.PROTECTED_PATH_PATTERNS)

    @classmethod
    def _path_like_leaves(cls, arguments: Any) -> List[str]:
        return [leaf for leaf in iter_string_leaves(arguments) if looks_like_path(leaf)]

    @classmethod
    def evaluate_tool_boundary(
        cls,
        invocation: ToolInvocation,
        rules: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, str]:
        """Checks a tool invocation against safety and architectural boundaries.

        Returns (is_permitted, failure_reason). The first finding decides, as it
        always has; a caller that stages rules one by one reads them all with
        `boundary_findings`.
        """
        for finding in cls.boundary_findings(invocation, rules):
            return False, finding.reason
        return True, "Architectural boundaries respected"

    @classmethod
    def boundary_findings(
        cls,
        invocation: ToolInvocation,
        rules: Optional[List[Dict[str, Any]]] = None,
    ) -> Iterator["BoundaryFinding"]:
        """Everything wrong with this call, in the order the gates ask.

        A generator on purpose. `evaluate_tool_boundary` takes the first and
        stops, so an ordinary verdict costs exactly what it cost before; only a
        caller that has to look past a finding — because the project is still
        only observing that rule — pays for the rest.

        Each finding says which gate found it. Reading that back out of the
        sentence was the defect: the sentences quote the caller's own command,
        so a trailing comment could name any rule it liked and have the refusal
        filed under it.
        """
        arguments = invocation.arguments

        # 1. Credentials anywhere in the arguments, at any depth. Nothing after
        #    this is asked: a credential is never staged, and the sentences
        #    below quote the arguments the credential is in.
        is_clean, secret_msg = SecretScanner.scan_arguments(arguments)
        if not is_clean:
            yield BoundaryFinding(CREDENTIAL_FOUND, secret_msg, label=secret_msg.rsplit(": ", 1)[-1])
            return

        # 2. Protected paths: the file the call names, found by shape as well as
        #    by argument name, since `notebook_path` was invisible to the four
        #    fixed keys this replaced. What a call *writes* is not a path it
        #    names, so an Edit whose new text is `process.env.PORT` is no longer
        #    reported as a target path the agent cannot act on.
        for candidate in target_paths(arguments):
            for pattern in cls.PROTECTED_PATH_PATTERNS:
                if pattern.search(candidate):
                    yield BoundaryFinding(
                        PROTECTED_PATH_FOUND,
                        f"Target path '{candidate}' is protected by architectural governance",
                        path=candidate,
                    )
                    break

        active_rules = rules if rules is not None else DEFAULT_RULES

        # 2b. The files that decide whether the hooks run at all. The layering
        #     rules have nothing to say about `{}` written into
        #     .claude/settings.json, and that write is the one that turns every
        #     other check off, so it is refused whatever the rules are.
        for target in governed_write_targets(invocation):
            if is_governance_path(target):
                yield BoundaryFinding(GOVERNANCE_FOUND, governance_reason(target), path=target)

        # 2c. What a shell command writes. A command was only ever read for the
        #     paths it named, so `cat > src/domain/user.py <<'EOF'` carried
        #     `import boto3` past a gate that would have refused the same text
        #     sent as a Write.
        command = shell_command(invocation)
        analysis = analysed(command, command_cwd(invocation)) if command is not None else None
        if analysis is not None:
            yield from shell_findings(analysis, active_rules)

        # 3. The layering rules. These are declared rather than compiled in, so
        #    the rule that has no incumbent can be the architecture of whoever is
        #    running this rather than the one example it shipped with. The check
        #    runs whenever the call carries content and points at a covered path.
        #    It deliberately does not require the caller to have declared
        #    FILE_WRITE: the declared action type is a hint from the agent, and a
        #    guard that only inspects calls which admit to being writes is one
        #    omitted field away from silence.
        for target, content in write_pairs(arguments):
            if not rules_for_path(target, active_rules):
                continue
            found, _ = violations(target, content, active_rules)
            for item in found:
                if item["mode"] == ENFORCE:
                    yield BoundaryFinding(
                        LAYERING_FOUND,
                        f"Clean Architecture violation: {item['reason']}",
                        rule_id=item["rule_id"],
                        path=target,
                    )

        # 4. Destructive or exfiltrating shell commands. A call that carries a
        #    command is read as one whatever it declares, and its working
        #    directory is a path-like leaf that must not switch this off.
        path_like = cls._path_like_leaves(arguments)
        if invocation.action_type == ToolActionType.COMMAND_EXEC or command is not None or not path_like:
            for leaf in iter_string_leaves(arguments):
                for pattern in cls.DESTRUCTIVE_COMMANDS:
                    found_text = pattern.search(leaf)
                    if found_text:
                        yield BoundaryFinding(
                            DESTRUCTIVE_FOUND,
                            f"Command '{leaf[:120]}' contains a destructive operation",
                            detail=found_text.group(0),
                            from_shell=command is not None,
                        )
                        break
            # A word the command never treats as a file — the pattern `grep` is
            # given, the line `echo` appends to .gitignore, the path `git` is
            # asked about — is blanked before the credential-store patterns run
            # over the command, exactly as `.git` was taken out of them on
            # 2026-09-22. A command that cannot be read this way is scanned
            # whole, as it always was.
            spelled, skipped = not_file_words(command, analysis)
            for leaf in iter_string_leaves(arguments):
                if looks_like_path(leaf) or leaf in skipped:
                    continue
                for pattern in cls.COMMAND_PROTECTED_PATTERNS:
                    found_text = pattern.search(spelled.get(leaf, leaf))
                    if found_text:
                        yield BoundaryFinding(
                            COMMAND_PATH_FOUND,
                            f"Command '{leaf[:120]}' reaches a protected path or credential store",
                            detail=found_text.group(0),
                            from_shell=command is not None,
                        )
                        break


def describe_target(request: Any) -> str:
    """A short, safe descriptor of what a tool call was aimed at.

    The ledger needs to say what was attempted without keeping what was
    attempted. For a file operation that is the path. For a command it is the
    program name alone: the rest of a command line is exactly where a refused
    credential would be, and a ledger that stored those would recreate the leak
    it exists to record. Nothing here ever returns file content.

    A path was not a safe place either. A URL carries its query string, and
    `…?access_token=…` put the token into the one field the ledger publishes
    word for word on a stack whose reads are public, beside a reason that was
    redacted. Every descriptor now leaves through the same redaction.
    """
    action = str(getattr(request, "action_type", "")).upper()
    arguments = getattr(request, "arguments", None)
    if not isinstance(arguments, dict):
        return ""

    if "COMMAND" in action or "EXEC" in action:
        for key in ("command", "cmd", "script", "shell"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                program = value.strip().split()[0]
                return redact_secrets(program)[:60]
        return ""

    for candidate in iter_string_leaves(arguments):
        if looks_like_path(candidate):
            return redact_secrets(candidate)[:160]
    return ""


def redact_secrets(text: str) -> str:
    """Replaces anything the scanner recognises as a credential with its label.

    A refusal reason quotes the command it refused, and a command that reaches a
    protected path can carry a token in the same line. Storing that reason
    verbatim would put the credential in the very record that exists to say it
    was stopped, so every reason is passed through here before it is kept.
    """
    if not text:
        return ""
    redacted = text
    for label, pattern in SecretScanner.PATTERNS:
        redacted = pattern.sub(f"[{label} REDACTED]", redacted)
    return redacted


CONTENT_KEYS = (
    "content", "new_string", "new_str", "new_source", "text", "body",
    "source", "code", "contents", "replacement", "patch", "diff",
)
PATH_KEYS = ("file_path", "path", "filepath", "target_file", "notebook_path", "absolutepath", "filename")


# What an edit takes out rather than puts in. A MultiEdit carries its path at the
# top and its edits beneath, with no path of their own, so nothing paired and the
# fallback judged every string in the call against the path, including the text
# being deleted: removing a forbidden import was refused for containing it.
REMOVED_KEYS = ("old_string", "old_str", "old", "original", "search", "find", "before")

# What a call carries that is text for a file rather than the name of one.
_NOT_A_TARGET = frozenset(CONTENT_KEYS + REMOVED_KEYS)


def target_paths(arguments: Any) -> Iterator[str]:
    """Every file this call names, in the order the call names them.

    A value under a path key is a path whatever it looks like; every other
    string is one only if it has a path's shape, which is what found `.env`
    under `AbsolutePath` and `notebook_path` when the guard read four fixed key
    names. What the call *writes* is left out: an Edit that puts
    `process.env.PORT` into a file was reported as "Target path
    'process.env.PORT' is protected", a sentence about a path the agent never
    named and cannot act on.
    """
    def walk(node: Any, key: Optional[str]) -> Iterator[str]:
        lowered = key.lower() if isinstance(key, str) else None
        if isinstance(node, str):
            if lowered in PATH_KEYS:
                if named_path(node):
                    yield node
            elif lowered not in _NOT_A_TARGET and looks_like_path(node):
                yield node
        elif isinstance(node, dict):
            for name, item in node.items():
                if isinstance(name, str) and looks_like_path(name):
                    yield name
                yield from walk(item, name if isinstance(name, str) else None)
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                yield from walk(item, key)

    seen = set()
    for candidate in walk(arguments, None):
        if candidate not in seen:
            seen.add(candidate)
            yield candidate


# --- a word a command never treats as a file ------------------------------------------
#
# The narrowing of `.git` on 2026-09-22 (c4a222c) took the repository's own
# directory out of the credential-store patterns, because a read-only command
# names it all the time to leave it out. `.env`, `secrets` and key names have
# the same problem and cannot be taken out: reading `.env` really is reaching a
# credential store. So the words that are demonstrably not files are taken out
# of the command instead, and everything else is scanned exactly as before. A
# command the shell reader cannot parse, a program not listed here, and a
# redirection's source are all scanned whole.

# Programs that print their operands rather than read them.
_PRINTERS = frozenset(("echo", "printf"))
# Programs whose first operand is a pattern, a filter or a script.
_PATTERN_FIRST = frozenset(("grep", "egrep", "fgrep", "rg", "ag", "ack", "jq", "sed", "awk", "gawk", "mawk"))
# When the pattern is given by a flag instead, which operand is a file is no
# longer clear from the outside, so nothing is taken out.
_PATTERN_FLAGS = ("-e", "-f", "--regexp", "--file", "--from-file", "--expression")
_PATTERN_FLAG_PREFIXES = ("--regexp=", "--file=", "--from-file=", "--expression=")
# git subcommands that name a path without reading what is in it.
_GIT_ASKS = frozenset(("check-ignore", "status", "ls-files", "log"))
_FIND_PATTERN_FLAGS = frozenset(
    ("-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex", "-lname", "-ilname")
)


def _program(word: str) -> str:
    return word.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _pattern_operand(rest: List[str]) -> List[str]:
    """The one leading operand that is a pattern rather than a file, or none."""
    for word in rest:
        if word in _PATTERN_FLAGS or word.startswith(_PATTERN_FLAG_PREFIXES):
            return []
    for word in rest:
        if word.startswith("-") and word != "-":
            continue
        return [word]
    return []


def _words_that_are_not_files(argv: List[str]) -> List[str]:
    """The words of one simple command that name no file it opens."""
    if not argv:
        return []
    name = _program(argv[0])
    rest = list(argv[1:])
    if name == "git":
        subcommand = next((word for word in rest if not word.startswith("-")), "")
        if subcommand == "grep":
            return _pattern_operand(rest[rest.index("grep") + 1:])
        return rest if subcommand in _GIT_ASKS else []
    if name in _PRINTERS:
        return rest
    if name == "find":
        return [rest[index + 1] for index, word in enumerate(rest[:-1]) if word.lower() in _FIND_PATTERN_FLAGS]
    if name in _PATTERN_FIRST:
        return _pattern_operand(rest)
    return []


def not_file_words(command: Any, analysis: Optional[ShellAnalysis]) -> Tuple[Dict[str, str], frozenset]:
    """How to read a command leaf in place of itself, and which words to skip.

    A command sent as one string comes back with those words blanked, keeping
    every other offset; a command sent as a list of words comes back as the set
    of words to pass over. Only words that would otherwise be read as a
    credential store are taken out, so nothing else about the command changes.
    """
    if analysis is None:
        return {}, frozenset()
    words: List[str] = []
    for argv in analysis.commands:
        words.extend(_words_that_are_not_files(list(argv)))
    words = [
        word
        for word in words
        if word and any(pattern.search(word) for pattern in ArchitecturalBoundaryGuard.COMMAND_PROTECTED_PATTERNS)
    ]
    if not words:
        return {}, frozenset()
    if not isinstance(command, str):
        return {}, frozenset(words)
    blanked = command
    for word in words:
        at = blanked.find(word)
        if at >= 0:
            blanked = blanked[:at] + " " * len(word) + blanked[at + len(word):]
    return {command: blanked}, frozenset()


def iter_write_targets(arguments: Any) -> List[Tuple[str, str]]:
    """Pairs each path in a call with the content meant for THAT path.

    A multi-file edit carries several pairs, and judging every path against
    every string was a cross product: one file's forbidden import refused a
    different, clean file, and the reason named the clean file and an import it
    did not contain. A refusal that is wrong about which file it is refusing is
    worse than a missed violation, because it cannot be argued with.

    Content with no path beside it belongs to the path of the object holding
    the list it sits in, which is how a MultiEdit's edits reach the file they
    edit. Only a list passes its owner's path down. An object nested under a
    path, such as `{"file_path": ..., "response": {"body": ...}}`, is not an edit
    of that file, and pairing it would refuse the file for text nobody wrote to
    it.
    """
    pairs: List[Tuple[str, str]] = []

    def walk(node: Any, inherited: str) -> None:
        if isinstance(node, dict):
            path_value = ""
            for key, value in node.items():
                if isinstance(value, str) and isinstance(key, str):
                    if key.lower() in PATH_KEYS and named_path(value):
                        path_value = value
            owner = path_value or inherited
            if owner:
                for key, value in node.items():
                    if isinstance(key, str) and isinstance(value, str) and key.lower() in CONTENT_KEYS:
                        pairs.append((owner, value))
            for value in node.values():
                walk(value, owner if isinstance(value, (list, tuple)) else "")
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item, inherited)

    walk(arguments, "")
    return pairs


def _written_leaves(value: Any) -> Iterator[str]:
    """Every string in a call except the ones an edit is removing."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in REMOVED_KEYS:
                continue
            yield from _written_leaves(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _written_leaves(item)


def write_pairs(arguments: Any) -> List[Tuple[str, str]]:
    """Each path in a call with the content meant for it, however the call is shaped."""
    pairs = iter_write_targets(arguments)
    if pairs:
        return pairs
    places = _working_directories(arguments)
    path_like = [leaf for leaf in iter_string_leaves(arguments) if looks_like_path(leaf) and leaf not in places]
    if len(path_like) == 1:
        # One path and loose content: everything written in the call is meant for it.
        return [(path_like[0], leaf) for leaf in _written_leaves(arguments) if leaf not in path_like]
    return []


def _working_directories(arguments: Any) -> List[str]:
    """The working directory a command call names, which is where it runs rather than what it writes."""
    if not isinstance(arguments, dict):
        return []
    return [value for key, value in arguments.items() if isinstance(key, str) and key.lower() in WORKING_DIRECTORY_KEYS and isinstance(value, str)]


def observe_layering(invocation: ToolInvocation, rules: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, str]]:
    """What the rules in observe mode would have refused in this call.

    Kept apart from the refusing pass on purpose: the path that decides whether a
    call runs is the path an earlier review found four defects in, and watching
    must not be able to change what is refused.
    """
    active_rules = rules if rules is not None else DEFAULT_RULES
    found: List[Dict[str, str]] = []
    for target, content in write_pairs(invocation.arguments or {}):
        for item in observed_layering(target, content, active_rules):
            found.append(dict(item, path=target))
    command = shell_command(invocation)
    if command is not None:
        found.extend(shell_observations(analysed(command, command_cwd(invocation)), active_rules))
    return found


# --- what a shell command writes -------------------------------------------------

# Tools that only read. A call to one of them names a settings file without
# writing it, and refusing `Read .claude/settings.json` would refuse the agent
# looking at the configuration it is being asked about.
READ_TOOLS = frozenset(
    (
        "read", "grep", "glob", "ls", "notebookread", "webfetch", "websearch",
        "read_file", "list_dir", "list_directory", "grep_search", "find_by_name",
        "view_file", "view_file_outline", "view_code_item", "codebase_search", "search_files",
    )
)

MAX_CACHED_COMMAND = 65_536


def shell_command(invocation: ToolInvocation) -> Any:
    """The command a call runs, as text or as a list of words, or None if it runs none.

    The declared action type is a hint, as it is for the layering rules: a call
    that carries `command` is read as the command it is whatever it says it is.
    The other keys (`cmd`, `script`, `shell`) are read only when the call does
    say it runs a command, because a write's `script` can be a file's content.
    """
    arguments = invocation.arguments
    if not isinstance(arguments, dict):
        return None
    declared = (
        invocation.action_type == ToolActionType.COMMAND_EXEC
        or str(invocation.tool_name or "").lower() in SHELL_TOOLS
    )
    for key in COMMAND_KEYS:
        if not (declared or key == "command"):
            continue
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (list, tuple)) and value and all(isinstance(word, str) for word in value):
            return list(value)
    return None


def command_cwd(invocation: ToolInvocation) -> str:
    """Where the command runs, relative to the project root, or "" for the root itself.

    The hook sends it for a Codex workdir or an Antigravity Cwd below the root.
    Without it `echo ... > user.py` run in src/domain was judged as a write to
    user.py at the root, which no rule on `**/domain/**` covers. A value that
    is absolute or climbs out of the root is not a place inside the project and
    is ignored rather than trusted.
    """
    arguments = invocation.arguments if isinstance(invocation.arguments, dict) else {}
    for value in _working_directories(arguments):
        candidate = value.strip().replace("\\", "/")
        if not candidate or candidate.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", candidate):
            continue
        normal = posixpath.normpath(candidate)
        if normal == "." or normal == ".." or normal.startswith("../"):
            continue
        return normal
    return ""


def analysed(command: Any, cwd: str = "") -> ShellAnalysis:
    """The writes one command makes, read once per command rather than once per gate.

    The boundary check, the observe pass and the loop gate each ask about the
    same command in the same request. The result is shared, so no caller may
    change it: `writes` and `commands` are handed out as tuples for that
    reason. Long commands are not kept, so the cache cannot hold megabytes.
    """
    key = tuple(command) if isinstance(command, list) else command
    size = sum(len(word) for word in key) if isinstance(key, tuple) else len(key or "")
    if size > MAX_CACHED_COMMAND:
        return _frozen(analyse_shell(command, cwd))
    return _analysed(key, cwd)


def _frozen(analysis: ShellAnalysis) -> ShellAnalysis:
    analysis.writes = tuple(analysis.writes)  # type: ignore[assignment]
    analysis.tampering = tuple(analysis.tampering)  # type: ignore[assignment]
    analysis.commands = tuple(analysis.commands)  # type: ignore[assignment]
    return analysis


@lru_cache(maxsize=128)
def _analysed(key: Any, cwd: str) -> ShellAnalysis:
    return _frozen(analyse_shell(list(key) if isinstance(key, tuple) else key, cwd))


def _named_paths(value: Any) -> Iterator[str]:
    """Every path given under a path-shaped key, however deep."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in PATH_KEYS and isinstance(item, str) and named_path(item):
                yield item
            else:
                yield from _named_paths(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _named_paths(item)


def governed_write_targets(invocation: ToolInvocation) -> List[str]:
    """The paths a call other than a shell command writes, for the governance check.

    A shell command's targets come from reading the command, in shell_refusal.
    A call declared as a read, or made by a read tool, that carries no content
    writes nothing, so it is not refused for naming a settings file.
    """
    if shell_command(invocation) is not None:
        return []
    arguments = invocation.arguments or {}
    pairs = write_pairs(arguments)
    reading = (
        invocation.action_type == ToolActionType.FILE_READ
        or str(invocation.tool_name or "").lower() in READ_TOOLS
    )
    if reading and not pairs:
        return []
    targets = [target for target, _ in pairs] + list(_named_paths(arguments))
    return list(dict.fromkeys(targets))


def governance_reason(target: str, route: str = "", deletes: bool = False) -> str:
    verb = "removes" if deletes else "writes"
    how = f" by {route}" if route else ""
    return (
        f"Target path '{target}' is protected by architectural governance: it decides whether "
        f"the agent's hooks run, and this call {verb} it{how}"
    )


def _pattern_governance_reason(write: ShellWrite) -> str:
    verb = "removes" if write.deletes else "writes"
    where = shell_display(write.target)
    if write.tree:
        return (
            f"Target path '{_tree_root(where)}' is protected by architectural governance: this call {verb} a "
            f"directory tree there by {write.route}, and the tree could hold the files that decide whether the "
            "agent's hooks run"
        )
    return (
        f"Target path '{where}' is protected by architectural governance: it is named with a shell expansion or a "
        f"glob, it could be a file that decides whether the agent's hooks run, and this call {verb} it by "
        f"{write.route}. Name the path literally"
    )


def _tree_root(where: str) -> str:
    return where.rsplit("/", 1)[0] if "/" in where else "."


def _enforcing(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [rule for rule in rules if rule.get("mode", ENFORCE) == ENFORCE]


# --- judging one write a command makes ---------------------------------------------

# The words that begin an import in each language the rules read.
_IMPORT_WORDS = {
    "python": ("import", "from"),
    "java": ("import",),
    "csharp": ("using",),
    "typescript": ("import", "from", "require", "export"),
}
_WORD = re.compile(r"[A-Za-z0-9_@\-]+")


def _pieces(patterns: Any) -> set:
    found = set()
    for pattern in patterns or ():
        for piece in re.split(r"[./*]+", str(pattern)):
            piece = piece.strip("@").lower()
            if len(piece) >= 3:
                found.add(piece)
    return found


def _fragment_could_import(content: str, rule: Dict[str, Any], language: str) -> bool:
    """Whether text that replaces part of a line could complete an import the rule forbids.

    `sed -i 's/json/boto3/'` turns `import json` into `import boto3`, and the
    replacement on its own is not an import at all. So a replacement that names
    a module the rule forbids, or brings an import keyword into a line whose
    other parts cannot be read, is judged as content that cannot be read. One
    that does neither, `s/old_name/new_name/`, is judged as it is. A module the
    rule also allows by name, such as C#'s `System`, is not counted.
    """
    words = {word.strip("@").lower() for word in _WORD.findall(content)}
    if any(keyword in words for keyword in _IMPORT_WORDS.get(language, ())):
        return True
    forbidden = _pieces(rule.get("forbid_imports")) - _pieces(rule.get("allow_imports"))
    return bool(words & forbidden)


def _finding(
    rule: Dict[str, Any], path: str, reason: str, module: str = "", pattern: str = "", kind: str = LAYERING_FOUND
) -> Dict[str, str]:
    """One rule's verdict on one write. `kind` says which gate it belongs to.

    A rule's import list and the policy on a write the rules cannot read are
    staged apart by an operator, and both name the rule in their sentence, so
    which one decided is recorded rather than read back out of the words.
    """
    return {
        "rule_id": rule["id"],
        "mode": rule.get("mode", ENFORCE),
        "module": module,
        "pattern": pattern,
        "reason": reason,
        "path": path,
        "kind": kind,
    }


def _unreadable_finding(rule: Dict[str, Any], write: ShellWrite, fragment: bool = False) -> Dict[str, str]:
    target = write.target or ""
    if rule.get("mode", ENFORCE) == ENFORCE:
        what = (
            f"changes part of a line in it by {write.route} to text that could complete an import the rule "
            "forbids, while the rest of the line cannot be read"
            if fragment else f"writes it by {write.route} with content the rule cannot read"
        )
        reason = f"Clean Architecture violation: layering rule '{rule['id']}' covers '{target}', and this command {what}: {UNREADABLE_WRITE}"
        return _finding(rule, target, reason, kind=UNREADABLE_FOUND)
    if not fragment:
        return _unreadable_observation(rule, target, write.route)
    reason = (
        f"Layering rule '{rule['id']}' would refuse this write: the command changes part of a line in '{target}' by "
        f"{write.route} to text that could complete an import the rule forbids, while the rest of the line cannot "
        f"be read, and would be told to {UNREADABLE_WRITE}"
    )
    return _finding(rule, target, reason, kind=UNREADABLE_FOUND)


def _unreadable_pattern_finding(rule: Dict[str, Any], write: ShellWrite, fragment: bool = False) -> Dict[str, str]:
    where = shell_display(write.target)
    enforce = rule.get("mode", ENFORCE) == ENFORCE
    lead = "Clean Architecture violation: layering rule" if enforce else "Layering rule"
    verb = "refuses" if enforce else "would refuse"
    if write.tree:
        reason = (
            f"{lead} '{rule['id']}' {verb} this write: the command brings a directory tree into "
            f"'{_tree_root(where)}' by {write.route}, and the rule could cover a file in it whose content it cannot "
            f"read: {UNREADABLE_WRITE}"
        )
    else:
        what = (
            "changes part of a line there to text that could complete an import the rule forbids"
            if fragment else "writes there with content the rule cannot read"
        )
        reason = (
            f"{lead} '{rule['id']}' {verb} this write: the command names '{where}' with a shell expansion or a "
            f"glob that is only resolved when it runs, the rule could cover it, and the command {what} by "
            f"{write.route}: name the file literally, or {UNREADABLE_WRITE}"
        )
    return _finding(rule, where, reason, kind=UNREADABLE_FOUND)


def _suffixes_within(target: str, rule: Dict[str, Any]) -> List[str]:
    """The readable file types a shape could be while a rule covers it; empty if it never could."""
    globs = rule.get("when_path_matches") or ()
    if not any(pattern_matches_glob(target, glob) for glob in globs):
        return []
    return [suffix for suffix in LANGUAGE_BY_SUFFIX if any(pattern_matches_glob(target, glob, suffix) for glob in globs)]


def _pattern_findings(write: ShellWrite, rules: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """The rules a write to a shape rather than a path breaks or would break.

    Content that cannot be read is judged as it would be for a path the rule
    covers. Content that can be read is judged once per file type the shape
    could be, by the rule's own lists, so `echo done > "$LOG"` is approved and
    `D=src/domain; echo 'import boto3' > $D/x.py` is refused naming the rule.
    """
    where = shell_display(write.target)
    findings: List[Dict[str, str]] = []
    for rule in rules:
        suffixes = _suffixes_within(write.target or "", rule)
        if not suffixes:
            continue
        if write.content is None:
            findings.append(_unreadable_pattern_finding(rule, write))
            continue
        probe = dict(rule, when_path_matches=["**"])
        for suffix in suffixes:
            found, _ = violations("threefold-shape" + suffix, write.content, [probe])
            if found:
                item = found[0]
                enforce = item["mode"] == ENFORCE
                description = rule.get("description") or rule["id"]
                reason = (
                    f"Layering rule '{rule['id']}' {'refuses' if enforce else 'would refuse'} this write: "
                    f"{description}. '{where}' is named with a shell expansion or a glob and could be a file the "
                    f"rule covers, and the content imports '{item['module']}', which matches '{item['pattern']}'"
                )
                if enforce:
                    reason = f"Clean Architecture violation: {reason} (written by {write.route})"
                findings.append(_finding(rule, where, reason, item["module"], item["pattern"]))
                break
            if write.fragment and _fragment_could_import(write.content, rule, LANGUAGE_BY_SUFFIX[suffix]):
                findings.append(_unreadable_pattern_finding(rule, write, fragment=True))
                break
    return findings


def _write_findings(write: ShellWrite, rules: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Every rule one of a command's writes breaks or would break, enforcing and watching alike.

    shell_refusal takes the first that enforces and shell_observations the
    ones that watch, so the two can never judge the same write differently.
    """
    if write.pattern:
        return _pattern_findings(write, rules)
    target = write.target or ""
    covering = rules_for_path(target, rules)
    if not covering:
        return []
    language = language_for(target)
    if write.content is None:
        # Telling an agent to "use Write or Edit so the rule can read it" is
        # only true for a file type the rules can read. A Markdown file under
        # domain/ is allowed as a Write, so it is allowed through `cp` as well.
        return [_unreadable_finding(rule, write) for rule in covering] if language else []
    found, _ = violations(target, write.content, rules)
    findings = []
    for item in found:
        reason = item["reason"]
        if item["mode"] == ENFORCE:
            reason = f"Clean Architecture violation: {reason} (written by {write.route})"
        findings.append(dict(item, reason=reason, path=target, kind=LAYERING_FOUND))
    if write.fragment and language:
        judged = {item["rule_id"] for item in found}
        findings.extend(
            _unreadable_finding(rule, write, fragment=True)
            for rule in covering
            if rule["id"] not in judged and _fragment_could_import(write.content, rule, language)
        )
    return findings


def shell_refusal(analysis: ShellAnalysis, rules: List[Dict[str, Any]]) -> Optional[str]:
    """Why a command's writes are refused, or None. Judged the way a Write would be.

    Four decisions the contract leaves to this function, each deliberate:

    - A deletion is judged only as a governance path. `rm src/domain/x.py`
      writes no import, and telling an agent to delete a file "with Write or
      Edit" is advice it cannot follow.
    - A write whose files cannot be known, `git apply fix.patch`, `patch <
      fix.patch` or `open(path, 'w')` with a computed path, is refused while any
      enforce rule is active. The patch lives on the developer's machine, and
      two calls, one writing the patch into /tmp and one applying it, would
      otherwise carry any import into any domain file. `git checkout` and `git
      merge` are not treated so: what they write was committed, and the
      pre-commit check has already read it.
    - A write to a shape, a target with an expansion or a glob in it, is judged
      against every rule that could cover a file of that shape.
    - A command too long to read to the end is refused while any enforce rule
      is active, for the same reason: padding must not be a way past the gate.

    Under observe rules alone each of these is recorded instead, by
    shell_observations.
    """
    for finding in shell_findings(analysis, rules):
        return finding.reason
    return None


def shell_findings(analysis: ShellAnalysis, rules: List[Dict[str, Any]]) -> Iterator[BoundaryFinding]:
    """Everything a command's writes break, in the order shell_refusal asks.

    shell_refusal is the first of these; a caller that stages rules one by one
    reads on. The sentences are the ones shell_refusal has always returned.
    """
    for reason in analysis.tampering:
        yield BoundaryFinding(
            TAMPERING_FOUND,
            f"Command turns the repository's hooks off: {reason}. Refused as a protected-path call",
            detail=reason,
            from_shell=True,
        )
    for write in analysis.writes:
        if write.target is None:
            continue
        if write.pattern:
            if pattern_is_governance(write.target, write.deletes, write.tree):
                yield BoundaryFinding(
                    GOVERNANCE_FOUND,
                    _pattern_governance_reason(write),
                    path=shell_display(write.target),
                    from_shell=True,
                )
        elif is_governance_path(write.target, deletes=write.deletes):
            yield BoundaryFinding(
                GOVERNANCE_FOUND,
                governance_reason(write.target, write.route, write.deletes),
                path=write.target,
                from_shell=True,
            )
    enforced = _enforcing(rules)
    if analysis.truncated and enforced:
        yield BoundaryFinding(
            UNREADABLE_FOUND,
            "Clean Architecture violation: this command is too long to be read to the end for the "
            f"files it writes, and an enforce rule is active: {UNREADABLE_WRITE}",
            detail="truncated",
            from_shell=True,
        )
    for write in analysis.writes:
        if write.deletes:
            continue
        if write.target is None:
            if enforced:
                yield BoundaryFinding(
                    UNREADABLE_FOUND,
                    f"Clean Architecture violation: this command writes by {write.route} to files it does not name "
                    "in a way that can be read (a patch kept in a file, or a path worked out when it runs), so neither "
                    f"the files nor what is written to them can be checked against rule '{enforced[0]['id']}': "
                    f"{UNREADABLE_WRITE}",
                    from_shell=True,
                )
            continue
        for finding in _write_findings(write, rules):
            if finding["mode"] == ENFORCE:
                yield BoundaryFinding(
                    UNREADABLE_FOUND if finding.get("kind") == UNREADABLE_FOUND else LAYERING_FOUND,
                    finding["reason"],
                    rule_id=finding["rule_id"],
                    path=finding.get("path", ""),
                    from_shell=True,
                )


def _unreadable_observation(rule: Dict[str, Any], target: str, how: str) -> Dict[str, str]:
    where = f"'{target}'" if target else "files it cannot name"
    return {
        "rule_id": rule["id"],
        "mode": OBSERVE,
        "module": "",
        "pattern": "",
        "reason": (
            f"Layering rule '{rule['id']}' would refuse this write: the command writes {where} by {how} "
            f"with content the rule cannot read, and would be told to {UNREADABLE_WRITE}"
        ),
        "path": target,
        "kind": UNREADABLE_FOUND,
    }


def shell_observations(analysis: ShellAnalysis, rules: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """What the observe rules would have refused among a command's writes.

    The mirror of shell_refusal for rules that only watch, built from the same
    findings: readable content is judged as a Write's would be, and content
    that cannot be read is recorded against each watching rule that covers the
    path, so a rule rolled out in observe mode shows the `cp` into domain/ it
    would stop as well as the Write.
    """
    watching = [rule for rule in rules if rule.get("mode") == OBSERVE]
    if not watching:
        return []
    found: List[Dict[str, str]] = []
    if analysis.truncated:
        found.extend(_unreadable_observation(rule, "", "a command too long to read") for rule in watching)
    for write in analysis.writes:
        if write.deletes:
            continue
        if write.target is None:
            found.extend(_unreadable_observation(rule, "", write.route) for rule in watching)
        else:
            found.extend(item for item in _write_findings(write, rules) if item["mode"] == OBSERVE)
    return found
