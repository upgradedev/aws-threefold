"""A certificate over nothing used to come back as a clean bill of health.

`all()` over an empty list is True, so `POST /issue-certificate` with
`{"evaluations": []}` returned `COMPLIANT_APPROVED`, `all_passed: true` and a
valid fingerprint over an empty list. For a product whose flagship artifact is
that certificate, a document that certifies nothing while reading as a pass is
the one failure that cannot ship.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from threefold.application.audit_issuer import AuditIssuer
from threefold.application.labels import developer_hash
from threefold.domain.exceptions import EmptyAttestationException
from threefold.domain.models import AgentSession, ToolActionType, ToolInvocation
from threefold.interfaces.api_handlers import _evaluator, lambda_handler

# Synthetic, as the clean-room rule requires: a name shaped like the ones the
# labels exist to keep off a public page, and a value shaped like a person.
RAW_PROJECT = "AcmeCorp Internal_Payments"
RAW_DEVELOPER = "acme-dev-marios"


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


@pytest.fixture
def old_session():
    """A row as it was written before the labels existed, still in the table.

    Saved through the repository rather than posted, because the write path
    labels a project now and the point of this row is that it was stored raw.
    It carries a call, or the issuer would refuse to certify it at all. Removed
    afterwards, because the handler's repository is module level and a raw row
    left in it would sit in /api/sessions for every test that runs after this.
    """
    session = AgentSession(
        session_id="cert-old-row-001",
        developer_id=RAW_DEVELOPER,
        project_name=RAW_PROJECT,
        budget_usd=5.0,
    )
    session.record_tool_call(
        ToolInvocation(
            tool_name="view_file",
            action_type=ToolActionType.FILE_READ,
            arguments={"path": "README.md"},
        )
    )
    _evaluator.session_repo.save_session(session, force=True)
    yield session
    _evaluator.session_repo._memory_store.pop(f"SESSION#{session.session_id}#METADATA", None)


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


# --------------------------------------- what the certificate may call a pass

# A certificate says every call it covers passed a gate that could have stopped
# it. A dry run is recorded as APPROVED with the invariants it broke left in
# place, so a caller echoing this service's own output back to it used to
# certify a session in which nothing was enforced.


def test_a_session_governed_only_in_dry_run_does_not_certify_as_compliant() -> None:
    """The verdicts below are this service's own, echoed back verbatim."""
    session_id = "cert-dry-run-001"
    status, approved = _post(
        "/evaluate-tool-call",
        {
            "session_id": session_id,
            "project_name": "Acme-Cert",
            "tool_name": "view_file",
            "action_type": "FILE_READ",
            "arguments": {"path": "README.md"},
            "dry_run": True,
        },
    )
    assert status == 200
    status, observed = _post(
        "/evaluate-tool-call",
        {
            "session_id": session_id,
            "project_name": "Acme-Cert",
            "tool_name": "run_command",
            "action_type": "COMMAND_EXEC",
            "arguments": {"command": "cat ~/.aws/credentials"},
            "dry_run": True,
        },
    )
    assert status == 200
    assert observed["status"] == "APPROVED", "A dry run is recorded, not refused"
    assert observed["rule_evaluations"]["ARCHITECTURAL_BOUNDARY_SAFE"] is False

    status, cert = _post(
        "/issue-certificate",
        {"session_id": session_id, "evaluations": [approved, observed]},
    )
    assert status == 200
    assert cert["all_passed"] is False
    assert cert["verdict_status"] == "NON_COMPLIANT_REJECTED"


def test_a_dry_run_that_broke_no_rule_is_still_not_an_enforced_pass() -> None:
    """Nothing was in a position to stop this call, which is what is being certified.

    Its invariants all hold, so only the dry_run flag distinguishes it, and the
    flag has to survive the certificate route's own parsing of the verdict.
    """
    session_id = "cert-dry-run-002"
    status, verdict = _post(
        "/evaluate-tool-call",
        {
            "session_id": session_id,
            "project_name": "Acme-Cert",
            "tool_name": "view_file",
            "action_type": "FILE_READ",
            "arguments": {"path": "README.md"},
            "dry_run": True,
        },
    )
    assert status == 200
    assert verdict["dry_run"] is True
    assert all(verdict["rule_evaluations"].values())

    status, cert = _post("/issue-certificate", {"session_id": session_id, "evaluations": [verdict]})
    assert status == 200
    assert cert["verdict_status"] == "NON_COMPLIANT_REJECTED"


def test_a_broken_invariant_is_read_even_where_the_status_says_approved() -> None:
    """The status is one field of a verdict; the invariants are the verdict."""
    session_id = "cert-invariants-001"
    _govern_one_call(session_id)
    status, cert = _post(
        "/issue-certificate",
        {
            "session_id": session_id,
            "evaluations": [
                {
                    "status": "APPROVED",
                    "proof_hash": "deadbeef",
                    "rule_evaluations": {"SECRET_LEAKAGE_FREE": False},
                }
            ],
        },
    )
    assert status == 200
    assert cert["all_passed"] is False


def test_the_issuer_counts_an_invariant_as_held_only_when_it_says_true(old_session: AgentSession) -> None:
    """The route checks the shape; a caller in-process reaches the issuer without it."""
    from threefold.application.dtos import EvaluationResultDTO

    verdict = EvaluationResultDTO(
        verdict_id="V-ACME-001",
        session_id=old_session.session_id,
        status="APPROVED",
        risk_level="LOW",
        reason="OK",
        rule_evaluations={"SECRET_LEAKAGE_FREE": "false"},  # type: ignore[dict-item]
        current_session_cost_usd=0.0,
        session_tripped=False,
        proof_hash="deadbeef",
    )
    assert AuditIssuer.issue_certificate(old_session, [verdict]).all_passed is False


# ------------------------------------------ what the certificate may show

# The listing publishes every session id, and a certificate is issued to anyone
# who names one, so a certificate that carried the raw developer and project
# handed back exactly what the labels keep off the listing it was read from.


def test_a_certificate_names_no_developer_even_when_one_was_sent() -> None:
    """The developer is stored as it arrives and labelled on the way out."""
    session_id = "cert-labels-001"
    status, verdict = _post(
        "/evaluate-tool-call",
        {
            "session_id": session_id,
            "developer_id": RAW_DEVELOPER,
            "project_name": "Acme-Cert",
            "tool_name": "view_file",
            "action_type": "FILE_READ",
            "arguments": {"path": "README.md"},
        },
    )
    assert status == 200

    status, cert = _post(
        "/issue-certificate",
        {"session_id": session_id, "evaluations": [{"proof_hash": verdict["proof_hash"]}]},
    )
    assert status == 200
    assert RAW_DEVELOPER not in json.dumps(cert), "The certificate named a person"
    assert cert["developer_id"] == developer_hash(RAW_DEVELOPER)


def test_a_certificate_for_a_row_written_before_the_labels_shows_neither(
    old_session: AgentSession,
) -> None:
    """The listing's rule applies to every route that reads that row, this one included."""
    status, cert = _post(
        "/issue-certificate",
        {"session_id": old_session.session_id, "evaluations": [{"proof_hash": "deadbeef"}]},
    )
    assert status == 200
    body = json.dumps(cert)
    assert RAW_PROJECT not in body and RAW_DEVELOPER not in body
    assert cert["project_name"] == "unlabelled"
    assert cert["developer_id"] == developer_hash(RAW_DEVELOPER)


def test_the_fingerprint_covers_the_values_the_reader_is_shown(old_session: AgentSession) -> None:
    """Labelling the handler's response instead would hash what nobody sees.

    The fingerprint is the only property this document has: recomputing it from
    the certificate detects an edit. A hash taken over the raw names while the
    reader is handed the labelled ones cannot be recomputed from the reader's
    copy, so it would detect nothing.
    """
    status, cert = _post(
        "/issue-certificate",
        {"session_id": old_session.session_id, "evaluations": [{"proof_hash": "deadbeef"}]},
    )
    assert status == 200

    canonical = json.dumps(
        {
            "session_id": cert["session_id"],
            "developer_id": cert["developer_id"],
            "project_name": cert["project_name"],
            "status": cert["verdict_status"],
            "total_cost_usd": cert["total_cost_usd"],
            "total_tokens": cert["total_tokens"],
            "evaluations_count": cert["evaluations_count"],
            "all_passed": cert["all_passed"],
            "evaluation_hashes": ["deadbeef"],
        },
        sort_keys=True,
    )
    assert cert["sha256_fingerprint"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_the_verdicts_the_dashboard_echoes_back_whole_still_certify() -> None:
    """Scenario 4 posts each response body unmodified, every field included.

    That is the shape the new checks read, the invariants and dry_run, so the
    flagship demo is pinned here rather than trusted to still pass.
    """
    session_id = "cert-whole-verdicts-001"
    verdicts = []
    for path in ("README.md", "src/acme/service.py"):
        status, verdict = _post(
            "/evaluate-tool-call",
            {
                "session_id": session_id,
                "developer_id": "dev-lead",
                "project_name": "Acme-Core",
                "tool_name": "view_file",
                "action_type": "FILE_READ",
                "arguments": {"path": path},
                "projected_input_tokens": 1200,
                "projected_output_tokens": 400,
            },
        )
        assert status == 200 and verdict["status"] == "APPROVED", verdict
        verdicts.append(verdict)

    status, cert = _post("/issue-certificate", {"session_id": session_id, "evaluations": verdicts})
    assert status == 200, cert
    assert cert["verdict_status"] == "COMPLIANT_APPROVED"
    assert cert["all_passed"] is True
    assert cert["evaluations_count"] == 2


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
