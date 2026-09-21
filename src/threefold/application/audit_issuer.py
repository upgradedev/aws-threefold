"""Certificate issuer for Threefold governance records, fingerprinted and unsigned."""
from __future__ import annotations

import hashlib
import json
from typing import List
from threefold.domain.models import AgentSession
from threefold.application.dtos import EvaluationResultDTO, GovernanceCertificateDTO
from threefold.application.labels import developer_hash, project_label
from threefold.domain.exceptions import EmptyAttestationException


class AuditIssuer:
    """Issues fingerprinted governance certificates over a session's verdicts.

    Fingerprinted, not tamper-evident: the hash is unkeyed, so anyone who edits
    the payload can recompute it. It catches corruption, not an adversary.
    """

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

        # A status alone does not say a call was governed. A dry run is recorded
        # as APPROVED with whatever failed left in rule_evaluations, exactly so
        # that a reader of the verdict can see the gate would have refused it, so
        # a certificate that read the status only attested compliance for a
        # session in which nothing was enforced. An invariant holds only where
        # the verdict says true, so a truthy stand-in such as "false" is not one.
        enforced = all(
            e.status == "APPROVED"
            and not e.dry_run
            and all(held is True for held in (e.rule_evaluations or {}).values())
            for e in evaluations
        )
        all_passed = enforced and not session.is_tripped
        status = "COMPLIANT_APPROVED" if all_passed else "NON_COMPLIANT_REJECTED"

        total_tokens = session.total_input_tokens + session.total_output_tokens

        # The same two rules the listing and the insights apply, applied here
        # because a certificate is handed to anyone who names a session id, and
        # those ids are published by the open listing. Labelling here rather
        # than in the handler keeps the fingerprint over exactly what the reader
        # is shown: a hash covering a raw name nobody sees would attest to a
        # document that does not exist.
        shown_developer = developer_hash(session.developer_id)
        shown_project = project_label(session.project_name)

        canonical_data = {
            "session_id": session.session_id,
            "developer_id": shown_developer,
            "project_name": shown_project,
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
            developer_id=shown_developer,
            project_name=shown_project,
            verdict_status=status,
            total_cost_usd=session.total_cost_usd,
            total_tokens=total_tokens,
            evaluations_count=len(evaluations),
            all_passed=all_passed,
            sha256_fingerprint=sha256_fingerprint,
        )
