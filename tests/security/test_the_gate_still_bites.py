"""Negative controls for the pre-commit gate.

The gate was changed from substring matching to parsing imports, which stopped it
reporting its own rule definitions. A scanner that was quietened rather than
fixed is worse than none, so each check below plants a real violation and
requires the gate to block it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "pre-commit-gate.py"


def _run_gate_on(directory: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), "--scan-dir", str(directory)],
        capture_output=True,
        text=True,
    )


def _domain_file(tmp_path: Path, body: str) -> Path:
    domain_dir = tmp_path / "planted" / "domain"
    domain_dir.mkdir(parents=True)
    target = domain_dir / "model.py"
    target.write_text(body, encoding="utf-8")
    return tmp_path / "planted"


def test_the_repository_passes_its_own_gate() -> None:
    result = _run_gate_on(REPO_ROOT / "src")
    assert result.returncode == 0, result.stderr


def test_a_real_sdk_import_in_the_domain_is_blocked(tmp_path: Path) -> None:
    planted = _domain_file(tmp_path, "import boto3\n\nclient = boto3.client('s3')\n")
    result = _run_gate_on(planted)
    assert result.returncode == 1
    assert "boto3" in result.stderr


def test_a_real_infrastructure_import_in_the_domain_is_blocked(tmp_path: Path) -> None:
    planted = _domain_file(tmp_path, "from threefold.infrastructure.s3_store import Store\n")
    result = _run_gate_on(planted)
    assert result.returncode == 1
    assert "infrastructure" in result.stderr


def test_a_live_looking_aws_key_is_blocked(tmp_path: Path) -> None:
    """A key that is not the AWS documentation example must still be caught."""
    planted = tmp_path / "planted"
    planted.mkdir()
    # Assembled at runtime so this test file does not itself carry the pattern.
    key = "AKIA" + "Q7R2T9MZXK4WPLDC"
    (planted / "leak.py").write_text(f'TOKEN = "{key}"\n', encoding="utf-8")
    result = _run_gate_on(planted)
    assert result.returncode == 1
    assert "SECRET LEAK" in result.stderr


def test_the_published_aws_example_key_is_not_reported(tmp_path: Path) -> None:
    """AWS prints this key in its own docs; flagging it trains people to ignore the gate."""
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "docs_example.py").write_text(
        'EXAMPLE = "AKIAIOSFODNN7EXAMPLE"\n', encoding="utf-8"
    )
    result = _run_gate_on(planted)
    assert result.returncode == 0, result.stderr


def test_the_word_infrastructure_in_a_comment_is_not_a_violation(tmp_path: Path) -> None:
    """The false positive that made the gate reject its own source."""
    planted = _domain_file(
        tmp_path,
        '# Domain must never import infrastructure, boto3 or a database driver.\n'
        'FORBIDDEN = ["import boto3", "from ..infrastructure"]\n',
    )
    result = _run_gate_on(planted)
    assert result.returncode == 0, result.stderr
