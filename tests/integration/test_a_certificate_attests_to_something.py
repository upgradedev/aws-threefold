"""A certificate over nothing used to come back as a clean bill of health.

`all()` over an empty list is True, so `POST /issue-certificate` with
`{"evaluations": []}` returned `COMPLIANT_APPROVED`, `all_passed: true` and a
valid fingerprint over an empty list. For a product whose flagship artifact is
that certificate, a document that certifies nothing while reading as a pass is
the one failure that cannot ship.
"""
from __future__ import annotations

import json

import pytest

from threefold.application.audit_issuer import AuditIssuer
from threefold.domain.exceptions import EmptyAttestationException
from threefold.interfaces.api_handlers import lambda_handler


def _post(path: str, body: dict) -> tuple[int, dict]:
    response = lambda_handler(
        {
            "rawPath": f"/prod{path}",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body),
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
        }
    )
    return response["statusCode"], json.loads(response["body"])


def _govern_one_call(session_id: str) -> dict:
    """Puts a real evaluation on the session, the way any caller would."""
    status, verdict = _post(
        "/evaluate-tool-call",
        {
            "session_id": session_id,
            "developer_id": "cert-test",
            "project_name": "Acme-Cert",
            "tool_name": "view_file",
            "action_type": "FILE_READ",
            "arguments": {"path": "README.md"},
            "projected_input_tokens": 100,
            "projected_output_tokens": 50,
            "budget_usd": 5.0,
        },
    )
    assert status == 200
    return verdict


def test_an_empty_evaluation_list_is_refused() -> None:
    status, body = _post("/issue-certificate", {"session_id": "cert-empty-001", "evaluations": []})
    assert status == 400, "A certificate over no evaluations must not be issued"
    assert body["title"] == "Nothing To Certify"
    assert body["type"] == "urn:threefold:error:empty-attestation"
    assert "all_passed" in body["detail"], "The refusal should say what it is refusing to imply"


def test_a_missing_evaluations_field_is_refused_too() -> None:
    """Omitting the field is the same claim as sending an empty one."""
    status, _ = _post("/issue-certificate", {"session_id": "cert-empty-002"})
    assert status == 400


def test_a_session_this_service_never_governed_is_refused() -> None:
    """Verdicts alone are the caller's word; the session has to be one we saw."""
    status, body = _post(
        "/issue-certificate",
        {
            "session_id": "cert-stranger-001",
            "evaluations": [{"status": "APPROVED", "proof_hash": "deadbeef"}],
        },
    )
    assert status == 400
    assert "no record of governing" in body["detail"]


def test_a_governed_session_still_gets_its_certificate() -> None:
    """The refusal must not break the flagship demo, which is the point of it."""
    session_id = "cert-real-001"
    verdict = _govern_one_call(session_id)

    status, cert = _post(
        "/issue-certificate",
        {
            "session_id": session_id,
            "evaluations": [
                {
                    "verdict_id": verdict["verdict_id"],
                    "status": verdict["status"],
                    "proof_hash": verdict["proof_hash"],
                }
            ],
        },
    )
    assert status == 200
    assert cert["evaluations_count"] == 1
    assert cert["verdict_status"] == "COMPLIANT_APPROVED"
    assert len(cert["sha256_fingerprint"]) == 64
    assert cert["certificate_id"].startswith("CERT-TF-")


def test_the_invariant_lives_in_the_issuer_not_only_at_the_edge() -> None:
    """Any caller publishing this artifact is held to the same rule."""
    from threefold.domain.models import AgentSession

    session = AgentSession(
        session_id="cert-direct-001",
        developer_id="dev",
        project_name="Acme-Cert",
        budget_usd=5.0,
    )
    with pytest.raises(EmptyAttestationException) as refused:
        AuditIssuer.issue_certificate(session, [])
    assert refused.value.code == "EMPTY_ATTESTATION"
