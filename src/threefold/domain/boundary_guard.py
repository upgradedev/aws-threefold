"""Architectural boundary enforcement, secret leakage scanner, and command safety.

Every check here walks the whole argument structure rather than reading a fixed
list of key names. An agent does not promise to name its arguments the way we
expect, and a guard that only looks where it is convenient is a guard that can
be stepped around by renaming a field.
"""
from __future__ import annotations

import re
from typing import Any, Iterator, List, Tuple
from threefold.domain.import_rules import find_forbidden_imports
from threefold.domain.models import ToolActionType, ToolInvocation

MAX_PATHLIKE_LENGTH = 400


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
    string would block a comment that merely mentions a dotfile.
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
        re.compile(r"\.env(?![A-Za-z0-9_])", re.IGNORECASE),
        re.compile(r"\.git(?![A-Za-z0-9_])", re.IGNORECASE),
        re.compile(r"(?<![A-Za-z0-9_])secrets(?![A-Za-z0-9_])", re.IGNORECASE),
        re.compile(r"\.(pem|key|pfx|pkcs12|p12)(?![A-Za-z0-9_])", re.IGNORECASE),
        re.compile(r"\.ssh(?![A-Za-z0-9_])", re.IGNORECASE),
        re.compile(r"\.aws(?![A-Za-z0-9_])", re.IGNORECASE),
        re.compile(r"(?<![A-Za-z0-9_])id_(rsa|ed25519|ecdsa)(?![A-Za-z0-9_])", re.IGNORECASE),
        re.compile(r"(^|[/\\])domain([/\\])core([/\\])frozen_", re.IGNORECASE),
    ]

    DESTRUCTIVE_COMMANDS: List[re.Pattern] = [
        re.compile(r"\brm\s+-rf\s+(/|\*|~|\$HOME)", re.IGNORECASE),
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
    def evaluate_tool_boundary(cls, invocation: ToolInvocation) -> Tuple[bool, str]:
        """Checks a tool invocation against safety and architectural boundaries.

        Returns (is_permitted, failure_reason).
        """
        arguments = invocation.arguments

        # 1. Credentials anywhere in the arguments, at any depth.
        is_clean, secret_msg = SecretScanner.scan_arguments(arguments)
        if not is_clean:
            return False, secret_msg

        # 2. Protected paths, found by shape rather than by argument name. The
        #    previous version read four fixed keys, so `notebook_path` was
        #    invisible to it.
        path_like = cls._path_like_leaves(arguments)
        for candidate in path_like:
            for pattern in cls.PROTECTED_PATH_PATTERNS:
                if pattern.search(candidate):
                    return False, f"Target path '{candidate}' is protected by architectural governance"

        # 3. The Clean Architecture rule. This runs whenever the call carries
        #    content and points at a domain file. It deliberately does not
        #    require the caller to have declared FILE_WRITE: the declared action
        #    type is a hint from the agent, and a guard that only inspects calls
        #    which admit to being writes is one omitted field from silence.
        domain_targets = [p for p in path_like if cls.DOMAIN_DIRECTORY.search(p)]
        if domain_targets:
            for leaf in iter_string_leaves(arguments):
                if leaf in path_like:
                    continue
                violations = find_forbidden_imports(leaf)
                if violations:
                    return False, (
                        f"Clean Architecture violation: domain file '{domain_targets[0]}' "
                        f"cannot depend on an outer layer ({violations[0]})"
                    )

        # 4. Destructive or exfiltrating shell commands.
        if invocation.action_type == ToolActionType.COMMAND_EXEC or not path_like:
            for leaf in iter_string_leaves(arguments):
                for pattern in cls.DESTRUCTIVE_COMMANDS:
                    if pattern.search(leaf):
                        return False, f"Command '{leaf[:120]}' contains a destructive operation"
            for leaf in iter_string_leaves(arguments):
                if looks_like_path(leaf):
                    continue
                for pattern in cls.PROTECTED_PATH_PATTERNS:
                    if pattern.search(leaf):
                        return False, (
                            f"Command '{leaf[:120]}' reaches a protected path or credential store"
                        )

        return True, "Architectural boundaries respected"
