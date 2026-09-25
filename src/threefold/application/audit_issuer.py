"""Issues governance certificates over a session's own stored verdicts.

The issuer reads what this service rendered and recorded, never what a caller
supplies: every judged call appends its verdict to the session, and the
certificate covers exactly those. A session with no recorded verdicts gets no
certificate, because `all()` over an empty list is True and a document that
certifies nothing while reading as a pass is the one failure that cannot ship.

The fingerprint is an unkeyed SHA-256 over the canonical payload: it detects
corruption and casual edits. Where a signing key is configured, the same
canonical bytes are signed with KMS, and the signature travels on the
certificate beside the key that made it.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Callable, Dict, List, Optional, Tuple

from threefold.application.dtos import EvaluationResultDTO, GovernanceCertificateDTO
from threefold.application.labels import developer_hash, project_label
from threefold.domain.exceptions import EmptyAttestationException
from threefold.domain.models import AgentSession, StoredVerdict

Signer = Callable[[bytes], Optional[Tuple[str, str]]]


def _default_signer(canonical: bytes) -> Optional[Tuple[str, str]]:
    from threefold.infrastructure.kms_signer import sign_certificate

    return sign_certificate(canonical)


class AuditIssuer:
    """Builds deterministic audit certificates from recorded history."""

    @staticmethod
    def issue_certificate(session: AgentSession, signer: Optional[Signer] = None) -> GovernanceCertificateDTO:
        """Issues the certificate for a governed session.

        `signer` signs the canonical bytes and returns the (base64 signature,
        key id); None signs with the stack's KMS key where one is configured,
        and issues unsigned anywhere else. Tests pass a stub and assert on
        the recorded fields rather than on the cryptography.
        """
        stored: List[StoredVerdict] = list(session.verdicts or [])
        if not stored:
            raise EmptyAttestationException(
                session.session_id,
                "no recorded verdicts: all_passed over an empty list would read "
                "as True, and this document must never certify what this "
                "service never judged.",
            )

        evaluations: List[EvaluationResultDTO] = [
            EvaluationResultDTO(
                verdict_id=v.verdict_id,
                session_id=session.session_id,
                status=v.status,
                risk_level="LOW",
                reason="",
                rule_evaluations=dict(v.rule_evaluations),
                current_session_cost_usd=0.0,
                session_tripped=False,
                proof_hash=v.proof_hash,
                dry_run=v.dry_run,
            )
            for v in stored
        ]

        all_passed = all(AuditIssuer._is_enforced_pass(e) for e in evaluations)
        shown_project = project_label(session.project_name)
        canonical_data = {
            "session_id": session.session_id,
            "developer_id": developer_hash(session.developer_id),
            "project_name": shown_project,
            "status": "COMPLIANT_APPROVED" if all_passed else "NON_COMPLIANT_REJECTED",
            "total_cost_usd": session.total_cost_usd,
            "total_tokens": session.total_input_tokens + session.total_output_tokens,
            "evaluations_count": len(evaluations),
            "all_passed": all_passed,
            "evaluation_hashes": [e.proof_hash for e in evaluations],
        }
        canonical = json.dumps(canonical_data, sort_keys=True).encode("utf-8")

        signature: Optional[str] = None
        signing_key_id: Optional[str] = None
        signed = (signer or _default_signer)(canonical)
        if signed is not None:
            signature, signing_key_id = signed

        return GovernanceCertificateDTO(
            certificate_id=f"CERT-TF-{secrets.token_hex(4).upper()}",
            session_id=session.session_id,
            developer_id=developer_hash(session.developer_id),
            project_name=shown_project,
            verdict_status="COMPLIANT_APPROVED" if all_passed else "NON_COMPLIANT_REJECTED",
            total_cost_usd=session.total_cost_usd,
            total_tokens=session.total_input_tokens + session.total_output_tokens,
            evaluations_count=len(evaluations),
            all_passed=all_passed,
            sha256_fingerprint=hashlib.sha256(canonical).hexdigest(),
            signature=signature,
            signing_key_id=signing_key_id,
        )

    @staticmethod
    def _is_enforced_pass(entry: EvaluationResultDTO) -> bool:
        return (
            entry.status == "APPROVED"
            and not entry.dry_run
            and all(value is True for value in (entry.rule_evaluations or {}).values())
        )
