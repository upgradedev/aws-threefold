"""Security, tamper-resistance, and clean-room invariant tests."""
from __future__ import annotations

import os
import re
from pathlib import Path
import pytest
from threefold.application.dtos import GovernanceCertificateDTO
from threefold.infrastructure.evidence_store import EvidenceStore


def test_tamper_resistance_negative_control():
    """Mutating 1 byte in the sealed evidence bundle must trigger instant verification failure."""
    cert = GovernanceCertificateDTO(
        certificate_id="CERT-TF-ABCDEF123456",
        session_id="session-sec-01",
        developer_id="dev-sec",
        project_name="SecurityCore",
        verdict_status="COMPLIANT_APPROVED",
        total_cost_usd=0.0450,
        total_tokens=1500,
        evaluations_count=1,
        all_passed=True,
        sha256_fingerprint="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    bundle = EvidenceStore.create_sealed_bundle(cert, {"environment": "production"})

    # Positive control: untampered bundle passes verification
    is_valid, msg = EvidenceStore.verify_sealed_bundle(bundle)
    assert is_valid is True
    assert "100% authentic" in msg

    # Negative control: mutate 1 character in the payload
    tampered_bundle = {
        "bundle_id": bundle["bundle_id"],
        "payload": dict(bundle["payload"]),
        "bundle_sha256": bundle["bundle_sha256"],
    }
    tampered_bundle["payload"]["metadata"] = {"environment": "compromised"}

    is_tampered_valid, tamper_msg = EvidenceStore.verify_sealed_bundle(tampered_bundle)
    assert is_tampered_valid is False
    assert "Tamper detected" in tamper_msg


def test_clean_room_invariant_no_enterprise_leaks():
    """Ensures the source tree carries no credentials, personal emails or workstation paths."""
    patterns = {
        "aws access key": re.compile(r"(?<![A-Z0-9])(AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
        "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "bearer token": re.compile(r"(?i)bearer\s+[a-z0-9._-]{32,}"),
        "personal email": re.compile(r"(?i)[a-z0-9._%+-]+@(?!example\.com|example\.org)[a-z0-9.-]+\.[a-z]{2,}"),
        "workstation path": re.compile(r"(?i)[a-z]:[\\/]users[\\/]|/home/[a-z0-9_-]+/"),
    }
    allowlist = ("EXAMPLE",)  # AWS documentation sample keys such as AKIAIOSFODNN7EXAMPLE

    src_dir = Path(__file__).resolve().parents[2] / "src"
    assert src_dir.exists(), f"Source directory {src_dir} must exist"

    for root, _, files in os.walk(src_dir):
        for file in files:
            if not file.endswith(".py"):
                continue
            file_path = Path(root) / file
            content = file_path.read_text(encoding="utf-8")
            for label, pattern in patterns.items():
                for match in pattern.finditer(content):
                    if any(token in match.group(0) for token in allowlist):
                        continue
                    raise AssertionError(
                        f"Clean-room violation: {label} '{match.group(0)}' "
                        f"found in {file_path.relative_to(src_dir)}"
                    )
