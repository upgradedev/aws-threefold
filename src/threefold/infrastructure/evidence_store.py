"""Tamper-evident JSON audit ledger with SHA-256 integrity verification."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Tuple
from threefold.application.dtos import GovernanceCertificateDTO


class EvidenceStore:
    """Stores and validates cryptographic evidence bundles for agent governance."""

    @staticmethod
    def create_sealed_bundle(
        certificate: GovernanceCertificateDTO,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Creates a canonical, tamper-evident audit record."""
        bundle_body = {
            "certificate": certificate.to_dict(),
            "metadata": metadata,
        }
        canonical_json = json.dumps(bundle_body, sort_keys=True)
        bundle_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

        return {
            "bundle_id": f"BUNDLE-{certificate.certificate_id}",
            "payload": bundle_body,
            "bundle_sha256": bundle_hash,
        }

    @staticmethod
    def verify_sealed_bundle(bundle: Dict[str, Any]) -> Tuple[bool, str]:
        """Validates that payload has not been tampered with since issuance."""
        if "payload" not in bundle or "bundle_sha256" not in bundle:
            return False, "Malformed bundle: missing payload or sha256"

        claimed_hash = bundle["bundle_sha256"]
        canonical_json = json.dumps(bundle["payload"], sort_keys=True)
        actual_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

        if claimed_hash != actual_hash:
            return False, f"Tamper detected: claimed {claimed_hash} != calculated {actual_hash}"

        return True, "Bundle integrity verified: 100% authentic"
