"""Architectural boundary enforcement, secret leakage scanner, and command safety.

Every check here walks the whole argument structure rather than reading a fixed
list of key names. An agent does not promise to name its arguments the way we
expect, and a guard that only looks where it is convenient is a guard that can
be stepped around by renaming a field.
"""
from __future__ import annotations

import posixpath
import re
from collections import Counter
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
    PLACEHOLDER,
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


def named_path(text: str) -> str:
    """The file a value given under an explicit path key names, or "".

    Nothing is guessed here. The agent said `file_path`, so the string is the
    path, whatever it contains. `looks_like_path` used to decide this too, and
    it says no to any string with a space in it, so `src/domain/order line.py`
    paired with no content and every layering and governance check below it was
    skipped, while the same write sent as a heredoc was refused.

    The value is taken without the whitespace around it. A newline is legal in
    a POSIX file name and so is a trailing space, and both were a way round
    everything below: the glob still covered `src/domain/order.py\\n`, but the
    suffix it was read for did not, so the rules found no language, read no
    imports and said nothing. Judging the trimmed name judges the file the
    agent almost certainly meant, and judging more is the safe direction.

    One limit is kept, and it is a real limit rather than a claim about what
    a path can be: a value longer than MAX_PATHLIKE_LENGTH names nothing here,
    so a whole file's text sent under a mistyped path key is never walked as a
    path — and so a path that long is not judged either.
    """
    if not isinstance(text, str) or len(text) > MAX_PATHLIKE_LENGTH:
        return ""
    return text.strip()


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
        # A store named exactly `secrets`, and a bare `.key` or `.pem`, are
        # credential stores like any other. `rg -n "secrets" docs/` and
        # `jq -r .key config.json` are freed where the freeing belongs, by
        # leaving out the words the command never opens (`not_file_words`);
        # narrowing the pattern instead would have freed `cat secrets` and
        # `Read {file_path: ".key"}` with them, on every route, since these
        # patterns judge the paths a call names as well as its command lines.
        re.compile(r"(?<![A-Za-z0-9_])secrets(?![A-Za-z0-9_])", re.IGNORECASE),
        re.compile(r"\.(pem|key|pfx|pkcs12|p12)(?![A-Za-z0-9_])", re.IGNORECASE),
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
    # refused by the governance check on what a command writes (2b and 2d
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
        blocked_patterns: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        """Checks a tool invocation against safety and architectural boundaries.

        Returns (is_permitted, failure_reason). The first finding decides, as it
        always has; a caller that stages rules one by one reads them all with
        `boundary_findings`.
        """
        for finding in cls.boundary_findings(invocation, rules, blocked_patterns):
            return False, finding.reason
        return True, "Architectural boundaries respected"

    @classmethod
    def boundary_findings(
        cls,
        invocation: ToolInvocation,
        rules: Optional[List[Dict[str, Any]]] = None,
        blocked_patterns: Optional[List[str]] = None,
    ) -> Iterator["BoundaryFinding"]:
        """Everything wrong with this call, in the order the gates ask.

        A generator on purpose. `evaluate_tool_boundary` takes the first and
        stops, so an ordinary verdict costs exactly what it cost before; only a
        caller that has to look past a finding — because the project is still
        only observing that rule — pays for the rest.

        `blocked_patterns` are the policy's additional secret shapes, asked
        after every gate above. A pattern that does not compile is skipped
        rather than trusted: the write path refuses such lists, so one that
        arrives here came from storage written by hand.

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

        # What the call runs, read once: the writes it makes are judged in 2d,
        # and the words it never opens are left out of the checks below, which
        # would otherwise read a command's own search pattern as a file.
        command_key, command = shell_command_at(invocation)
        analysis = analysed(command, command_cwd(invocation)) if command is not None else None
        spelled, not_a_file = not_file_words(command, analysis)

        # 2. Protected paths: the file the call names, found by shape as well as
        #    by argument name, since `notebook_path` was invisible to the four
        #    fixed keys this replaced. What a call *writes* is not a path it
        #    names, so an Edit whose new text is `process.env.PORT` is no longer
        #    reported as a target path the agent cannot act on, and neither is
        #    the `process.env` a command sent word by word is searching for.
        #    That last exemption belongs to the command and stays inside it: a
        #    `file_path` is judged as the path the agent said it was, whatever
        #    else the call carries.
        for candidate in target_paths(arguments, command_key, not_a_file):
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

        # 2d. What a shell command writes. A command was only ever read for the
        #     paths it named, so `cat > src/domain/user.py <<'EOF'` carried
        #     `import boto3` past a gate that would have refused the same text
        #     sent as a Write.
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
            # asked about — is blanked (`spelled`, read at the top of this
            # walk) before the credential-store patterns run over the command,
            # exactly as `.git` was taken out of them on 2026-09-22. A command
            # that cannot be read this way is scanned whole, as it always was.
            for leaf in iter_string_leaves(arguments):
                if looks_like_path(leaf) or leaf in not_a_file:
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

        # 5. The policy's blocked patterns, asked last. A hit files under the
        #    credential key: the shipped patterns are secret shapes, and an
        #    operator's own are refused with the same force, which means always
        #    enforced and never staged. Last because every finding above is
        #    more specific about the same call: reading `.env` is refused as
        #    the path it names (2), `cat .env` as the path the command reaches
        #    (4), and a blocked term inside a domain write as the layer it
        #    crosses (3). Last is still a refusal: a caller that stages a rule
        #    walks past the observed findings, and a credential is never
        #    observed, so an observed layer does not shelter a blocked term.
        #    The reason names the operator's pattern, never the caller's text
        #    that matched it: echoing the match would hand the secret back in
        #    the verdict. A pattern that does not compile is skipped rather
        #    than trusted: the write path refuses such lists, so one that
        #    arrives here came from storage written by hand.
        for pattern in blocked_patterns or []:
            if not isinstance(pattern, str) or not pattern:
                continue
            try:
                matcher = re.compile(pattern)
            except re.error:
                continue
            if any(matcher.search(leaf) for leaf in iter_string_leaves(arguments)):
                yield BoundaryFinding(
                    CREDENTIAL_FOUND,
                    f"Blocked pattern '{pattern[:80]}' matched the arguments. "
                    "Remove the blocked term or have the operator change the policy.",
                    label="BLOCKED_PATTERN",
                )
                return


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

    The paths are the ones `target_paths` finds, which is what every other
    reader of a path key was moved to: on the shape heuristic alone this said
    nothing at all about `Write {file_path: 'src/acme/domain/order line.py'}`,
    so the ledger row for exactly the write a space used to hide recorded an
    empty target, and /api/decisions said a rule was broken without saying
    what the call was aimed at.
    """
    action = str(getattr(request, "action_type", "")).upper()
    arguments = getattr(request, "arguments", None)
    if not isinstance(arguments, dict):
        return ""

    if "COMMAND" in action or "EXEC" in action:
        for key in COMMAND_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                program = value.strip().split()[0]
                return redact_secrets(program)[:60]
        return ""

    for candidate in target_paths(arguments):
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


def target_paths(
    arguments: Any, command_key: str = "", command_words: frozenset = frozenset()
) -> Iterator[str]:
    """Every file this call names, in the order the call names them.

    A value under a path key is a path whatever it looks like; every other
    string is one only if it has a path's shape, which is what found `.env`
    under `AbsolutePath` and `notebook_path` when the guard read four fixed key
    names. What the call *writes* is left out: an Edit that puts
    `process.env.PORT` into a file was reported as "Target path
    'process.env.PORT' is protected", a sentence about a path the agent never
    named and cannot act on.

    `command_key` is the argument the call's command was actually read from,
    and `command_words` the words of that command it never opens. Both are
    resolved by the caller from `shell_command_at`, so what counts as "the
    command" here is the same thing the gates ran, not a second guess from the
    key's name: a call that carries a real `file_path` beside a `command` used
    to have that path waved through because a word of the command happened to
    spell it. A word of the command the command does open is still a path the
    call names, which is how `local_shell ["cat", ".env"]` is refused.
    """
    def walk(node: Any, key: Optional[str], in_command: bool, top: bool = False) -> Iterator[str]:
        lowered = key.lower() if isinstance(key, str) else None
        if isinstance(node, str):
            if in_command:
                if node not in command_words and looks_like_path(node):
                    yield node
            elif lowered in PATH_KEYS:
                if named_path(node):
                    yield named_path(node)
            elif lowered not in _NOT_A_TARGET and looks_like_path(node):
                yield node
        elif isinstance(node, dict):
            for name, item in node.items():
                if isinstance(name, str) and looks_like_path(name):
                    yield name
                yield from walk(
                    item,
                    name if isinstance(name, str) else None,
                    in_command or bool(top and command_key and name == command_key),
                )
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                yield from walk(item, key, in_command)

    seen = set()
    for candidate in walk(arguments, None, False, top=True):
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
#
# "Demonstrably" is the whole of it, and the first attempt was not:
#
# - A word is only taken out of the command it was read from, at the place it
#   was written. Searching the line for it blanked the first occurrence that
#   matched literally, so `cat .env; echo ".e"nv` — where the benign word is
#   quote-split and has no literal occurrence of its own — erased the `.env`
#   that `cat` opens.
# - A word the command *prints* is a word anything reading its output can
#   open: `echo .env | xargs cat` reads the store as surely as `cat .env`.
#   Those are left out only when nothing in the call can read that output.
# - `find`'s `-name` operand is the name of the files `-exec`, `-delete` and a
#   pipe into `xargs` then open. The repository's own shell reader says so
#   (tests/unit/test_shell_writes.py, "find -exec runs its command on files
#   nobody names"), and with an action like those nothing is taken out.
# - A word any command in the call does open is never taken out, whichever
#   other command printed or matched it.

# Programs that print their operands rather than read them.
_PRINTERS = frozenset(("echo", "printf"))
# Programs whose first operand is a pattern, a filter or a script. A pattern is
# neither opened nor printed, so a pipe after it changes nothing.
_PATTERN_FIRST = frozenset(("grep", "egrep", "fgrep", "rg", "ag", "ack", "jq", "sed", "awk", "gawk", "mawk"))
# When the pattern is given by a flag instead, which operand is a file is no
# longer clear from the outside, so nothing is taken out.
_PATTERN_FLAGS = ("-e", "-f", "--regexp", "--file", "--from-file", "--expression")
_PATTERN_FLAG_PREFIXES = ("--regexp=", "--file=", "--from-file=", "--expression=")
# git subcommands that name a path without ever printing what is in it.
# `diff` and `log -p` are deliberately not among them: both print the file, so
# `git log -p -- .env` reaches the credential store as surely as `cat` does.
# These do print the path itself, so they are read as printers are.
_GIT_ASKS = frozenset(("check-ignore", "status", "ls-files"))
_FIND_PATTERN_FLAGS = frozenset(
    ("-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex", "-lname", "-ilname")
)
# What `find` does to the files its pattern matched, rather than listing them.
# With any of these the pattern names files the command itself opens or removes.
_FIND_ACTIONS = frozenset(
    ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls")
)


def _program(word: str) -> str:
    return word.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _pattern_operand(rest: List[str]) -> List[int]:
    """Where the one leading operand that is a pattern rather than a file sits, or nowhere."""
    for word in rest:
        if word in _PATTERN_FLAGS or word.startswith(_PATTERN_FLAG_PREFIXES):
            return []
    for index, word in enumerate(rest):
        if word.startswith("-") and word != "-":
            continue
        return [index]
    return []


def _not_file_indices(argv: List[str]) -> Tuple[List[int], List[int]]:
    """Which words of one simple command it never opens, as positions in `argv`.

    Two lists, because they are not equally safe to leave out: the first holds
    words the command neither opens nor puts anywhere (a search pattern, a
    filter), the second words it does not open but does print, which anything
    reading its output can open.
    """
    if not argv:
        return [], []
    name = _program(argv[0])
    rest = argv[1:]
    if name == "git":
        subcommand = next((word for word in rest if not word.startswith("-")), "")
        if subcommand == "grep":
            after = rest.index("grep") + 1
            return [1 + after + index for index in _pattern_operand(rest[after:])], []
        return ([], list(range(1, len(argv)))) if subcommand in _GIT_ASKS else ([], [])
    if name in _PRINTERS:
        return [], list(range(1, len(argv)))
    if name == "find":
        if any(word.lower() in _FIND_ACTIONS for word in rest):
            return [], []
        return [], [
            index + 2 for index, word in enumerate(rest[:-1]) if word.lower() in _FIND_PATTERN_FLAGS
        ]
    if name in _PATTERN_FIRST:
        return [1 + index for index in _pattern_operand(rest)], []
    return [], []


def read_words(text: str) -> Tuple[List[Tuple[int, int, str]], bool]:
    """Each word of a command line as (start, end, value), and whether a pipe joins two.

    A second, deliberately small reader beside the one in `shell_writes`: that
    one says what a command does, this one says *where in the line* each word
    was written, which is what a word has to be taken out by without moving the
    rest of the line. Quotes are resolved the way a POSIX shell resolves them,
    so `.e"nv"` is the word `.env` and blanking it blanks those six characters
    and no others.

    The two readers are checked against each other in `not_file_words`: a line
    this one reads differently takes every word out of the exemption, so a
    quoting form neither models is refused rather than approved.
    """
    spans: List[Tuple[int, int, str]] = []
    piped = False
    index, size = 0, len(text)
    while index < size:
        while index < size and text[index].isspace():
            index += 1
        if index >= size:
            break
        start, quote, value = index, "", []
        while index < size and (quote or not text[index].isspace()):
            char = text[index]
            if quote:
                if char == quote:
                    quote = ""
                elif char == "\\" and quote == '"' and index + 1 < size:
                    index += 1
                    value.append(text[index])
                else:
                    value.append(char)
            elif char in "'\"":
                quote = char
            elif char == "\\" and index + 1 < size:
                index += 1
                value.append(text[index])
            else:
                piped = piped or char == "|"
                value.append(char)
            index += 1
        spans.append((start, index, "".join(value)))
    return spans, piped


def not_file_words(command: Any, analysis: Optional[ShellAnalysis]) -> Tuple[Dict[str, str], frozenset]:
    """How to read a command leaf in place of itself, and which words to skip.

    A command sent as one string comes back with those words blanked where they
    were written, keeping every other offset; a command sent as a list of words
    comes back as the set of words to pass over. Only words that would
    otherwise be read as a credential store are taken out, so nothing else
    about the command changes.

    Nothing at all is taken out of a command the readers cannot agree on, one
    too long to be read to the end, or one that leaves an operand for the shell
    to work out when it runs (`xargs`, `find -exec`, a substitution): there the
    file that is opened is exactly the one no word names.
    """
    if analysis is None or analysis.truncated:
        return {}, frozenset()
    never: List[str] = []
    printed: List[str] = []
    opens: List[str] = []
    for argv in analysis.commands:
        words = list(argv)
        if any(PLACEHOLDER in word for word in words):
            return {}, frozenset()
        unopened, shown = _not_file_indices(words)
        for index, word in enumerate(words):
            if index in unopened:
                never.append(word)
            elif index in shown:
                printed.append(word)
            else:
                opens.append(word)
    opens.extend(write.target for write in analysis.writes if write.target)

    if isinstance(command, str):
        spans, piped = read_words(command)
        # A word the small reader never produced is a word it cannot place, so
        # the exemption is dropped rather than guessed at.
        if not {value for _, _, value in spans} >= {word for word in never + printed + opens if word}:
            return {}, frozenset()
    else:
        spans, piped = [], any(isinstance(word, str) and "|" in word for word in command or ())

    exempt = list(never)
    if not piped:
        # Its output reaches nothing that could open what it names, so a word
        # it only prints is a word nobody opens. The text it writes into a file
        # is the same thing said the other way round, and is why a heredoc
        # adding `.env` to .gitignore is not a read of `.env`.
        exempt.extend(printed)
        exempt.extend(word for write in analysis.writes for word in (write.content or "").split())
    opened = set(opens)
    counted = Counter(word for word in exempt if word)
    exempt_set = {
        word
        for word in counted
        if word not in opened
        and any(pattern.search(word) for pattern in ArchitecturalBoundaryGuard.COMMAND_PROTECTED_PATTERNS)
    }
    if not exempt_set:
        return {}, frozenset()
    if not isinstance(command, str):
        return {}, frozenset(exempt_set)
    # Every occurrence in the line has to be one of the occurrences that earned
    # the exemption. `cat .env > out` beside `echo .env` writes the word twice
    # and only one of them is printed, so neither is blanked.
    written = Counter(value for _, _, value in spans)
    blanked = list(command)
    for start, end, value in spans:
        if value in exempt_set and counted[value] >= written[value]:
            blanked[start:end] = " " * (end - start)
    return {command: "".join(blanked)}, frozenset()


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
                        path_value = named_path(value)
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


def shell_command_at(invocation: ToolInvocation) -> Tuple[str, Any]:
    """Which argument holds the command a call runs, and the command, or ("", None).

    The declared action type is a hint, as it is for the layering rules: a call
    that carries `command` is read as the command it is whatever it says it is.
    The other keys (`cmd`, `script`, `shell`) are read only when the call does
    say it runs a command, because a write's `script` can be a file's content.

    The key travels with the command because a reader that has to know which
    strings are "the command" must not work it out a second time: two answers
    to that question, in the gate and in the walk over the call's paths, is how
    an extra `command` field switched the protected-path check off for a
    `file_path` beside it.
    """
    arguments = invocation.arguments
    if not isinstance(arguments, dict):
        return "", None
    declared = (
        invocation.action_type == ToolActionType.COMMAND_EXEC
        or str(invocation.tool_name or "").lower() in SHELL_TOOLS
    )
    for key in COMMAND_KEYS:
        if not (declared or key == "command"):
            continue
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return key, value
        if isinstance(value, (list, tuple)) and value and all(isinstance(word, str) for word in value):
            return key, list(value)
    return "", None


def shell_command(invocation: ToolInvocation) -> Any:
    """The command a call runs, as text or as a list of words, or None if it runs none."""
    return shell_command_at(invocation)[1]


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
                yield named_path(item)
            else:
                yield from _named_paths(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _named_paths(item)


def governed_write_targets(invocation: ToolInvocation) -> List[str]:
    """The paths a call writes that are not its command's own, for the governance check.

    A shell command's targets come from reading the command, in shell_refusal.
    A call declared as a read, or made by a read tool, that carries no content
    writes nothing, so it is not refused for naming a settings file.

    A call that carries a command used to give up every target, and a Write
    with a real `file_path` beside a `command` field therefore wrote
    .claude/settings.json — the file that decides whether any of this runs at
    all — with this check switched off. The command's own words are still
    left alone, because `write_pairs` would read them as paths the call names
    and refuse a read for naming a settings file; what the call names under a
    path key is a path it names, whatever else it carries.
    """
    arguments = invocation.arguments or {}
    running = shell_command(invocation) is not None
    # write_pairs falls back to shape when nothing pairs by key, and a
    # command's own words have a path's shape, so a `Read` carrying a command
    # would look like a write of the file it names. Where there is a command,
    # only what pairs by key counts as content.
    pairs = iter_write_targets(arguments) if running else write_pairs(arguments)
    reading = (
        invocation.action_type == ToolActionType.FILE_READ
        or str(invocation.tool_name or "").lower() in READ_TOOLS
    )
    if reading and not pairs:
        return []
    named = list(_named_paths(arguments))
    if running:
        return list(dict.fromkeys(named))
    return list(dict.fromkeys([target for target, _ in pairs] + named))


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
