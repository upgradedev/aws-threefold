#!/usr/bin/env python3
"""Zero-Dependency Pre-Commit Governance Gate for Threefold.

Can be run directly or copied into .git/hooks/pre-commit.
Scans modified files or target directories for:
1. Secret Leakage (AWS Access Keys, GitHub Tokens, Private Keys).
2. Clean Architecture Domain Invariants (domain importing infrastructure).
3. Forbidden file patterns (.env, credentials).

Exit Code:
  0 - Clean: all governance invariants satisfied.
  1 - Blocked: violation detected.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Add src to path if running from repo root
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root / "src"))

try:
    from threefold.domain.boundary_guard import ArchitecturalBoundaryGuard, SecretScanner
except ImportError:
    # Standalone fallback if package structure differs
    import re

    class SecretScanner:
        @staticmethod
        def scan_text(text: str) -> tuple[bool, str]:
            if "AKIA" in text and re.search(r"AKIA[0-9A-Z]{16}", text):
                return True, "AWS Access Key ID leaked"
            if "-----BEGIN OPENSSH PRIVATE KEY-----" in text or "-----BEGIN RSA PRIVATE KEY-----" in text:
                return True, "Private key leaked"
            return False, ""

    class ArchitecturalBoundaryGuard:
        @staticmethod
        def is_forbidden_file_access(path: str) -> bool:
            clean = path.replace("\\", "/")
            return ".env" in clean or ".git/" in clean


def scan_file(file_path: Path) -> list[str]:
    """Inspects a single file against security & architectural rules."""
    violations = []

    # Check forbidden file names
    if ArchitecturalBoundaryGuard.is_forbidden_file_access(str(file_path)):
        violations.append(f"Forbidden file access attempt: {file_path}")
        return violations

    if not file_path.is_file():
        return violations

    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        violations.append(f"Could not read {file_path}: {exc}")
        return violations

    # Check secrets
    is_clean, secret_msg = SecretScanner.scan_payload(content)
    if not is_clean:
        violations.append(f"SECRET LEAK DETECTED in {file_path}: {secret_msg}")

    # Check Clean Architecture: domain layer must never import infrastructure
    clean_path = str(file_path).replace("\\", "/")
    if "/domain/" in clean_path:
        for line_no, line in enumerate(content.splitlines(), start=1):
            if "infrastructure" in line and ("import" in line or "from" in line):
                violations.append(
                    f"CLEAN ARCHITECTURE VIOLATION in {file_path}:{line_no} -> Domain cannot import infrastructure: '{line.strip()}'"
                )

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description="Threefold Pre-Commit Security & Architecture Gate")
    parser.add_argument("--scan-dir", default=str(repo_root / "src"), help="Target directory to scan")
    parser.add_argument("--file", help="Specific file to scan")
    args = parser.parse_args()

    all_violations = []

    if args.file:
        target_file = Path(args.file)
        all_violations.extend(scan_file(target_file))
    else:
        target_dir = Path(args.scan_dir)
        if target_dir.exists():
            for p in target_dir.rglob("*.py"):
                all_violations.extend(scan_file(p))

    if all_violations:
        print("\n" + "=" * 70, file=sys.stderr)
        print("[BLOCKED] THREEFOLD PRE-COMMIT GATE: COMMIT REJECTED", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        for v in all_violations:
            print(f"  * {v}", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        print("Remediation: Remove secrets and respect Clean Architecture boundaries before committing.\n", file=sys.stderr)
        return 1

    print("[PASSED] Threefold Pre-Commit Gate: All security & architectural invariants satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
