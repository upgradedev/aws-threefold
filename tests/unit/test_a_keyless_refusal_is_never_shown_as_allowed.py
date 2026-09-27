"""A refusal no gate key names is shown as Other on the ledger pages, as /api/insights counts it.

The NONE category is labelled Allowed. A refused row whose key resolves to
NONE (a status the rule keys do not know) used to be shown under it on
/api/decisions while /api/insights counted it as Other.
"""
from __future__ import annotations

from threefold.application import insights, ledger


def _row(status: str) -> dict:
    return {"verdict_id": "acme-1", "timestamp": "2026-09-28T07:00:00+00:00", "session_id": "acme-s", "project_name": "Acme-Payments",
            "status": status, "reason": "Refused for a reason no gate key names", "target": "src/acme/app.py", "origin": "hook"}


def test_a_keyless_refusal_is_other_on_both_routes() -> None:
    row = _row("BLOCKED_BY_SOMETHING_NEW")
    shown = ledger.shown_row(row)
    assert shown["rule_key"] == "NONE"
    assert (shown["category"], shown["category_label"]) == ("OTHER", "Other")
    assert insights.categorise(row) == "OTHER"


def test_an_approved_call_keeps_its_own_category() -> None:
    shown = ledger.shown_row(_row("APPROVED"))
    assert shown["category"] == "NONE"
