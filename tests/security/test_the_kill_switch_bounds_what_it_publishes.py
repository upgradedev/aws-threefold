"""What a caller writes into a freeze is shown on a page, so it is bounded and redacted.

`POST /sessions/<id>/terminate` needs no key on the public demo, on purpose:
freezing a session can only stop work. What it writes, though, does not stop
there. `models.py` builds `trip_reason` as "MANUALLY_TERMINATED by <operator>:
<reason>", the sessions listing returns that string as it is, and `public_row()`
rewrites only the project name and the developer. So both fields reached
`/api/sessions` and the sessions page exactly as they arrived: any length up to
the 1 MB body ceiling, any JSON type, and a credential included. The same text
sent through `/evaluate-tool-call` is kept as `redact_secrets(reason)[:240]`.

These pin the same treatment for the kill switch, and pin that an ordinary
freeze from either console still works.
"""
from __future__ import annotations

import json

import pytest

from threefold.interfaces.api_handlers import lambda_handler

# What the route records when the caller names neither, written out rather
# than imported: they are what this route has always answered with, so a
# change to either is a change a caller would see.
DEFAULT_OPERATOR = "Enterprise Security Admin"
DEFAULT_REASON = "Manual emergency kill-switch invoked"

# Assembled rather than written out, as the rest of the suite assembles one, so
# this file does not itself carry a credential-shaped literal. It is synthetic.
SYNTHETIC_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _call(method: str, path: str, body: dict | None = None):
    event = {
        "rawPath": f"/prod{path}",
        "headers": {"Content-Type": "application/json"},
        "requestContext": {"http": {"method": method}, "stage": "prod"},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    response = lambda_handler(event)
    return response["statusCode"], json.loads(response["body"])


def _freeze(session_id: str, body: dict | None):
    return _call("POST", f"/sessions/{session_id}/terminate", body)


def _trip_reason(session_id: str) -> str | None:
    status, listing = _call("GET", "/api/sessions", None)
    assert status == 200
    row = next((s for s in listing["sessions"] if s["session_id"] == session_id), None)
    return None if row is None else row.get("trip_reason")


def test_a_credential_in_the_reason_is_replaced_by_its_label() -> None:
    """Otherwise the freeze publishes the very thing the gate exists to stop."""
    status, body = _freeze("kill-redact-1", {"operator_name": "Acme on-call", "reason": f"saw {SYNTHETIC_TOKEN}"})
    assert status == 200
    assert SYNTHETIC_TOKEN not in json.dumps(body)

    shown = _trip_reason("kill-redact-1")
    assert SYNTHETIC_TOKEN not in shown
    assert "[GITHUB_TOKEN REDACTED]" in shown


def test_a_credential_in_the_operator_name_is_replaced_too() -> None:
    status, body = _freeze("kill-redact-2", {"operator_name": SYNTHETIC_TOKEN, "reason": "review"})
    assert status == 200
    assert SYNTHETIC_TOKEN not in json.dumps(body)
    assert SYNTHETIC_TOKEN not in _trip_reason("kill-redact-2")


def test_a_reason_past_its_bound_is_refused_and_freezes_nothing() -> None:
    """A caller could publish a megabyte of their own text on the sessions page."""
    status, problem = _freeze("kill-bound-1", {"operator_name": "Acme on-call", "reason": "x" * 5000})
    assert status == 400
    assert problem["type"] == "urn:threefold:error:bad-request"
    assert "240" in problem["detail"]
    assert _trip_reason("kill-bound-1") is None, "the session was frozen by a refused request"


def test_an_operator_name_past_its_bound_is_refused() -> None:
    status, problem = _freeze("kill-bound-2", {"operator_name": "a" * 500, "reason": "review"})
    assert status == 400
    assert "120" in problem["detail"]


@pytest.mark.parametrize("field", ["operator_name", "reason"])
@pytest.mark.parametrize("value", [{"a": 1}, ["x"], 42, True])
def test_a_field_that_is_not_text_is_refused(field, value) -> None:
    """Stored as it arrived, a dict reached the page as whatever json.dumps made of it."""
    body = {"operator_name": "Acme on-call", "reason": "review"}
    body[field] = value
    status, problem = _freeze(f"kill-type-{field}", body)
    assert status == 400, problem
    assert problem["invalid_params"][0]["name"] == field


def test_an_ordinary_freeze_from_the_console_is_unchanged() -> None:
    """Both pages send a short operator and a short reason; both must still work."""
    status, body = _freeze(
        "kill-ordinary-1",
        {
            "operator_name": "Sessions console operator",
            "reason": "Manual kill switch invoked from the sessions console",
        },
    )
    assert status == 200
    assert body["status"] == "SESSION_FROZEN"
    assert body["is_tripped"] is True
    assert _trip_reason("kill-ordinary-1") == (
        "MANUALLY_TERMINATED by Sessions console operator: "
        "Manual kill switch invoked from the sessions console"
    )


def test_a_freeze_that_names_neither_field_still_records_the_defaults() -> None:
    """An empty body froze a session before this, and still does."""
    status, body = _freeze("kill-default-1", {})
    assert status == 200
    assert body["operator"] == DEFAULT_OPERATOR
    assert body["reason"] == DEFAULT_REASON
    assert _trip_reason("kill-default-1") == (
        f"MANUALLY_TERMINATED by {DEFAULT_OPERATOR}: {DEFAULT_REASON}"
    )


# Synthetic, and the key id AWS prints in its own documentation. Twenty
# characters in, twenty-five out: "[AWS_ACCESS_KEY REDACTED]" is longer than
# what it replaces, which is what the two tests below are about.
SYNTHETIC_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"


def _reason_packed_with_key_ids(length: int) -> str:
    """A reason at exactly `length` characters, most of it key ids."""
    packed = " ".join([SYNTHETIC_KEY_ID] * (length // (len(SYNTHETIC_KEY_ID) + 1)))
    return (packed + " " + "x" * length)[:length]


def test_a_reason_the_labels_push_past_the_bound_is_refused() -> None:
    """Measured before redacting only, it passed the bound and was stored past it.

    The route answers that a reason is at most 240 characters, and what a reader
    of the sessions page sees is the redacted text. A 240-character reason packed
    with key ids came back through `redact_secrets` at about 300, so the answer
    the caller was given about their own record was not true of it.
    """
    reason = _reason_packed_with_key_ids(240)
    assert len(reason) == 240
    status, problem = _freeze("kill-grown-1", {"reason": reason})
    assert status == 400, problem
    assert problem["invalid_params"][0]["name"] == "reason"
    assert "240" in problem["detail"]
    assert _trip_reason("kill-grown-1") is None


def test_what_a_freeze_publishes_is_never_longer_than_the_bound_it_states() -> None:
    """One key id still fits inside 240 characters, so it is kept and redacted."""
    reason = f"saw {SYNTHETIC_KEY_ID} in the log"
    status, body = _freeze("kill-grown-2", {"reason": reason})
    assert status == 200, body
    published = _trip_reason("kill-grown-2")
    assert SYNTHETIC_KEY_ID not in published
    assert "[AWS_ACCESS_KEY REDACTED]" in published
    assert len(published) <= len(f"MANUALLY_TERMINATED by {DEFAULT_OPERATOR}: ") + 240
