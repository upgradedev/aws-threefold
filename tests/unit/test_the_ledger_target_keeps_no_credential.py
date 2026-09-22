"""The ledger's `target` is redacted, as its `reason` already was.

`describe_target` returns the path or URL a call was aimed at, and on a stack
with PublicReads=true every reader of `/api/decisions` sees it. A URL that
carries a token in its query string was stored verbatim, so the row that exists
to say a credential was stopped published the credential. The reason beside it
was redacted; the target was not.
"""
from __future__ import annotations

from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator
from threefold.domain.boundary_guard import describe_target
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository

# Synthetic, and shaped only so the scanner recognises it.
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


class _Request:
    def __init__(self, action_type: str, arguments: dict) -> None:
        self.action_type = action_type
        self.arguments = arguments


def test_describe_target_redacts_a_token_in_a_url() -> None:
    request = _Request("WEB_SEARCH", {"url": f"https://api.example.invalid/repos/acme/app?access_token={TOKEN}"})
    target = describe_target(request)
    assert TOKEN not in target
    assert "[GITHUB_TOKEN REDACTED]" in target
    # What the call was aimed at is still legible.
    assert "api.example.invalid/repos/acme/app" in target


def test_describe_target_redacts_a_secret_in_a_path() -> None:
    request = _Request("FILE_READ", {"file_path": f"/tmp/{TOKEN}/notes.txt"})
    assert TOKEN not in describe_target(request)


def test_the_recorded_row_carries_no_credential() -> None:
    """The refusal is recorded, and neither its reason nor its target keeps the token."""
    repo = DynamoDBSessionRepository(table_name="ledger-target-test")
    evaluator = GovernanceEvaluator(session_repo=repo)
    request = ToolCallRequestDTO(
        session_id="target-redaction-1",
        developer_id="anonymous",
        project_name="Acme-Ledger",
        tool_name="WebFetch",
        action_type="WEB_SEARCH",
        arguments={"url": f"https://api.example.invalid/repos/acme/app?access_token={TOKEN}"},
        agent="ci",
        origin="ci",
    )
    verdict = evaluator.evaluate_tool_call(request)
    assert verdict.status == "BLOCKED_SECRET_DETECTED"

    rows = [row for row in repo.list_decisions(days=1) if row.get("session_id") == "target-redaction-1"]
    assert rows, "the refusal should have been recorded"
    for row in rows:
        for field in ("target", "reason", "observed_reason", "observed_target"):
            assert TOKEN not in str(row.get(field) or ""), field
