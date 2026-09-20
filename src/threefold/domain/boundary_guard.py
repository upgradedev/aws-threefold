"""Architectural boundary enforcement, secret leakage scanner, and command safety."""
from __future__ import annotations

import re
from typing import List, Tuple
from threefold.domain.models import ToolActionType, ToolInvocation


class SecretScanner:
    """Pre-commit / Pre-invocation scanner that catches credentials before leakage."""

    PATTERNS: List[Tuple[str, re.Pattern]] = [
        ("AWS_ACCESS_KEY", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
        ("AWS_SECRET_KEY", re.compile(r"(?i)aws_secret_access_key\s*=\s*['\"][A-Za-z0-9/+=]{40}['\"]")),
        ("GITHUB_TOKEN", re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{36,255})\b")),
        ("GENERIC_API_KEY", re.compile(r"(?i)(api[_-]?key|secret[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{20,}['\"]")),
        ("PRIVATE_KEY_HEADER", re.compile(r"-----BEGIN (RSA|EC|OPENSSH|PGP) PRIVATE KEY-----")),
    ]

    @classmethod
    def scan_payload(cls, text: str) -> Tuple[bool, str]:
        """Scans string for sensitive credentials.

        Returns (is_clean, matched_secret_description).
        """
        for label, pattern in cls.PATTERNS:
            if pattern.search(text):
                return False, f"Sensitive credential detected: {label}"
        return True, "No credentials detected"


class ArchitecturalBoundaryGuard:
    """Enforces Clean Architecture layer boundaries and frozen directory protection."""

    # File paths that agents are strictly forbidden from altering or accessing
    PROTECTED_PATH_PATTERNS: List[re.Pattern] = [
        re.compile(r"(^|[\s/\\'\"])\.env(\b|[\s/\\'\"]|$)", re.IGNORECASE),
        re.compile(r"(^|[\s/\\'\"])\.git(\b|[\s/\\'\"]|$)", re.IGNORECASE),
        re.compile(r"(^|[\s/\\'\"])secrets(\b|[\s/\\'\"]|$)", re.IGNORECASE),
        re.compile(r"\.(pem|key|pfx|pkcs12)(\b|[\s/\\'\"]|$)", re.IGNORECASE),
        re.compile(r"(^|[/\\])domain([/\\])core([/\\])frozen_", re.IGNORECASE),
    ]

    # Prohibited shell command sequences
    DESTRUCTIVE_COMMANDS: List[re.Pattern] = [
        re.compile(r"\brm\s+-rf\s+(/|\*|~|\$HOME)", re.IGNORECASE),
        re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),
        re.compile(r"\bgit\s+push\s+.*--force", re.IGNORECASE),
        re.compile(r"\bdrop\s+database\b", re.IGNORECASE),
    ]

    @classmethod
    def is_forbidden_file_access(cls, path: str) -> bool:
        """Determines if a given file path breaches protected path governance."""
        for pattern in cls.PROTECTED_PATH_PATTERNS:
            if pattern.search(path):
                return True
        return False

    @classmethod
    def evaluate_tool_boundary(cls, invocation: ToolInvocation) -> Tuple[bool, str]:
        """Checks if a tool invocation breaches system safety or architectural boundaries.

        Returns (is_permitted, failure_reason).
        """
        args_str = str(invocation.arguments)

        # 1. Secret Leakage Check across arguments
        is_clean, secret_msg = SecretScanner.scan_payload(args_str)
        if not is_clean:
            return False, secret_msg

        # 2. File Path Protections (Read / Write)
        target_path = (
            invocation.arguments.get("path")
            or invocation.arguments.get("TargetFile")
            or invocation.arguments.get("AbsolutePath")
            or invocation.arguments.get("file_path")
            or ""
        )

        if target_path:
            for pattern in cls.PROTECTED_PATH_PATTERNS:
                if pattern.search(str(target_path)):
                    return False, f"Target path '{target_path}' is protected by architectural governance"

        # 3. Clean Architecture Dependency Inversion Check (Writing to Domain)
        if invocation.action_type == ToolActionType.FILE_WRITE and "domain" in str(target_path).lower():
            file_content = (
                invocation.arguments.get("content")
                or invocation.arguments.get("CodeContent")
                or invocation.arguments.get("ReplacementContent")
                or ""
            )
            # Domain must NOT import infrastructure, flask, fastapi, boto3, or database drivers
            forbidden_imports = ["import boto3", "import requests", "import fastapi", "import sqlalchemy", "from ..infrastructure"]
            for forbidden in forbidden_imports:
                if forbidden in file_content:
                    return False, (
                        f"Clean Architecture Violation: Domain file '{target_path}' "
                        f"cannot depend on outer layer ({forbidden})"
                    )

        # 4. Destructive or Exfiltration Shell Command Execution Check
        if invocation.action_type == ToolActionType.COMMAND_EXEC:
            cmd = invocation.arguments.get("CommandLine") or invocation.arguments.get("command") or ""
            for pattern in cls.DESTRUCTIVE_COMMANDS:
                if pattern.search(str(cmd)):
                    return False, f"Command '{cmd}' contains dangerous destructive operations"
            for pattern in cls.PROTECTED_PATH_PATTERNS:
                if pattern.search(str(cmd)):
                    return False, f"Command '{cmd}' attempts to access protected paths or credentials"

        return True, "Architectural boundaries respected"
