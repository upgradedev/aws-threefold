"""Unit tests for ArchitecturalBoundaryGuard and SecretScanner."""
from __future__ import annotations

import pytest
from threefold.domain.models import ToolActionType, ToolInvocation
from threefold.domain.boundary_guard import ArchitecturalBoundaryGuard, SecretScanner


def test_secret_scanner_catches_aws_key():
    clean_text = "Reading file src/utils.py with line count 50"
    is_clean, _ = SecretScanner.scan_payload(clean_text)
    assert is_clean is True

    secret_text = "AKIAIOSFODNN7EXAMPLE is used for S3 authentication"
    is_clean, reason = SecretScanner.scan_payload(secret_text)
    assert is_clean is False
    assert "AWS_ACCESS_KEY" in reason


def test_boundary_guard_blocks_env_file_access():
    inv = ToolInvocation(
        tool_name="view_file",
        action_type=ToolActionType.FILE_READ,
        arguments={"AbsolutePath": "/app/.env"},
    )
    is_safe, reason = ArchitecturalBoundaryGuard.evaluate_tool_boundary(inv)
    assert is_safe is False
    assert "protected by architectural governance" in reason


def test_boundary_guard_blocks_clean_architecture_domain_violation():
    # Attempting to write into domain with forbidden infrastructure import
    inv = ToolInvocation(
        tool_name="write_to_file",
        action_type=ToolActionType.FILE_WRITE,
        arguments={
            "TargetFile": "/src/my_app/domain/models.py",
            "CodeContent": "import boto3\n\nclass User:\n    pass",
        },
    )
    is_safe, reason = ArchitecturalBoundaryGuard.evaluate_tool_boundary(inv)
    assert is_safe is False
    assert "clean architecture violation" in reason.lower()


def test_boundary_guard_blocks_destructive_shell_command():
    inv = ToolInvocation(
        tool_name="run_command",
        action_type=ToolActionType.COMMAND_EXEC,
        arguments={"CommandLine": "rm -rf /"},
    )
    is_safe, reason = ArchitecturalBoundaryGuard.evaluate_tool_boundary(inv)
    assert is_safe is False
    assert "destructive" in reason.lower()


def test_blocked_pattern_refuses_without_echoing_the_match():
    inv = ToolInvocation(
        tool_name="write_to_file",
        action_type=ToolActionType.FILE_WRITE,
        arguments={"TargetFile": "/src/notes.txt", "CodeContent": "deploy AcmeSecretProject at dawn"},
    )
    is_safe, reason = ArchitecturalBoundaryGuard.evaluate_tool_boundary(
        inv, blocked_patterns=[r"AcmeSecret\w+"]
    )
    assert is_safe is False
    assert "Blocked pattern" in reason
    assert "AcmeSecretProject" not in reason, "The verdict must not hand the match back"


def test_blocked_pattern_skips_what_does_not_compile():
    inv = ToolInvocation(
        tool_name="view_file",
        action_type=ToolActionType.FILE_READ,
        arguments={"path": "README.md"},
    )
    is_safe, _ = ArchitecturalBoundaryGuard.evaluate_tool_boundary(
        inv, blocked_patterns=["([unclosed", "", None, 42]
    )
    assert is_safe is True
