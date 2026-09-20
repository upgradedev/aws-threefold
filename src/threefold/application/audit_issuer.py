"""Cryptographic certificate issuer for Threefold governance verification."""
from __future__ import annotations

import hashlib
import json
from typing import List
from threefold.domain.models import AgentSession
from threefold.application.dtos import EvaluationResultDTO, GovernanceCertificateDTO
from threefold.domain.exceptions import EmptyAttestationException


class AuditIssuer:
    """Issues tamper-evident governance certificates for CI/CD pipelines and auditing."""

    @staticmethod
    def issue_certificate(
        session: AgentSession,
        evaluations: List[EvaluationResultDTO],
    ) -> GovernanceCertificateDTO:
        """Issues a certificate over verdicts that exist.

        The invariant lives here rather than only at the edge, because every
        caller of this function is publishing a governance artifact and none of
        them should be able to publish one that attests to nothing.
        """
        if not evaluations:
            raise EmptyAttestationException(
                session.session_id,
                "no evaluations were supplied, and a certificate over nothing would "
                "report all_passed on the strength of an empty list",
            )
        if not session.history:
            raise EmptyAttestationException(
                session.session_id,
                "this service has no record of governing that session, so there is "
                "nothing of its own to certify",
            )

        all_passed = all(e.status == "APPROVED" for e in evaluations) and not session.is_tripped
        status = "COMPLIANT_APPROVED" if all_passed else "NON_COMPLIANT_REJECTED"

        total_tokens = session.total_input_tokens + session.total_output_tokens

        canonical_data = {
            "session_id": session.session_id,
            "developer_id": session.developer_id,
            "project_name": session.project_name,
            "status": status,
            "total_cost_usd": session.total_cost_usd,
            "total_tokens": total_tokens,
            "evaluations_count": len(evaluations),
            "all_passed": all_passed,
            "evaluation_hashes": [e.proof_hash for e in evaluations],
        }

        canonical_json = json.dumps(canonical_data, sort_keys=True)
        sha256_fingerprint = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        certificate_id = f"CERT-TF-{sha256_fingerprint[:12].upper()}"

        return GovernanceCertificateDTO(
            certificate_id=certificate_id,
            session_id=session.session_id,
            developer_id=session.developer_id,
            project_name=session.project_name,
            verdict_status=status,
            total_cost_usd=session.total_cost_usd,
            total_tokens=total_tokens,
            evaluations_count=len(evaluations),
            all_passed=all_passed,
            sha256_fingerprint=sha256_fingerprint,
        )
