"""Two verdicts never share an id, even when the clock does not move between them.

The id used to be the first twelve hex of the proof hash, and the proof covers
the session, status, reason, invariants and timestamp. Two approvals in one
session within one clock tick (a millisecond on some machines) had the same id,
and the ledger, keyed by it, kept one of the two. The proof itself must still
verify from the fields the caller is given.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from threefold.domain import models
from threefold.domain.models import GovernanceVerdict, RiskLevel, VerdictStatus

FROZEN = datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc)


class _FrozenClock(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D401 - mirrors datetime.now
        return FROZEN


def _approve() -> GovernanceVerdict:
    return GovernanceVerdict.create(
        session_id="acme-same-tick",
        status=VerdictStatus.APPROVED,
        risk_level=RiskLevel.LOW,
        reason="All deterministic governance invariants satisfied",
        rule_evaluations={"SECRET_LEAKAGE_FREE": True},
    )


def test_two_identical_verdicts_in_one_tick_have_different_ids(monkeypatch) -> None:
    monkeypatch.setattr(models, "datetime", _FrozenClock)
    first, second = _approve(), _approve()
    assert first.timestamp == second.timestamp and first.proof_hash == second.proof_hash
    assert first.verdict_id != second.verdict_id
    assert first.verdict_id.startswith("VERDICT-") and len(first.verdict_id) == len("VERDICT-") + 12


def test_the_proof_still_verifies_from_the_fields_given() -> None:
    verdict = _approve()
    canonical = json.dumps(
        {
            "session_id": verdict.session_id,
            "status": verdict.status.value,
            "risk_level": verdict.risk_level.value,
            "reason": verdict.reason,
            "rule_evaluations": verdict.rule_evaluations,
            "timestamp": verdict.timestamp,
        },
        sort_keys=True,
    )
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == verdict.proof_hash
