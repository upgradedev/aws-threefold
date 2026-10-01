"""An edit is judged on the imports it adds, not only on the fragment it sends.

A partial-line edit carries no import syntax in its own text: changing `json`
to `boto3` sends the word `boto3`, which declares no import, so the gate that
reads imports approved it. The hook therefore sends the file before and after
the edit alongside, and only the difference is judged: imports the file
already had are the author's past, not this edit's.
"""
from __future__ import annotations

import json

from threefold.interfaces.api_handlers import lambda_handler

# Assembled rather than written out, as the rest of the suite assembles one, so
# this file does not itself carry a credential-shaped literal. It is synthetic.
SYNTHETIC_KEY = "AKIA" + "A1B2C3D4E5F6G7H8"


def _judge(session_id: str, path: str, content: str, before: str, after: str, dry_run=False) -> dict:
    body = {
        "session_id": session_id,
        "project_name": "Acme-Ledger",
        "tool_name": "Edit",
        "action_type": "FILE_WRITE",
        "arguments": {"file_path": path, "content": content, "edit_before": before, "edit_after": after},
        "agent": "claude-code",
        "origin": "hook",
        "explain": False,
    }
    if dry_run:
        body["dry_run"] = True
    event = {
        "rawPath": "/prod/evaluate-tool-call",
        "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
    return json.loads(lambda_handler(event, None)["body"])


def test_a_fragment_that_adds_a_forbidden_import_is_refused() -> None:
    verdict = _judge(
        "edit-intro-py-1",
        "src/domain/order.py",
        "boto3",
        "import json\n\n\ndef total():\n    return 0\n",
        "import boto3\n\n\ndef total():\n    return 0\n",
    )
    assert verdict["status"] == "BLOCKED_BOUNDARY_VIOLATION", verdict.get("reason")
    assert "introduces 'boto3'" in verdict["reason"]


def test_a_fragment_that_adds_a_forbidden_import_is_refused_typescript() -> None:
    verdict = _judge(
        "edit-intro-ts-1",
        "src/domain/cart.ts",
        "'axios'",
        "import { z } from 'zod';\n",
        "import { z } from 'axios';\n",
    )
    assert verdict["status"] == "BLOCKED_BOUNDARY_VIOLATION", verdict.get("reason")
    assert "introduces 'axios'" in verdict["reason"]


def test_a_fragment_that_adds_a_forbidden_import_is_refused_java() -> None:
    verdict = _judge(
        "edit-intro-java-1",
        "src/domain/Order.java",
        "java.sql.Connection",
        "import java.util.List;\n",
        "import java.sql.Connection;\n",
    )
    assert verdict["status"] == "BLOCKED_BOUNDARY_VIOLATION", verdict.get("reason")
    assert "introduces 'java.sql.Connection'" in verdict["reason"]


def test_removing_a_forbidden_import_is_allowed() -> None:
    verdict = _judge(
        "edit-intro-rm-1",
        "src/domain/order.py",
        "json",
        "import boto3\n",
        "import json\n",
    )
    assert verdict["status"] == "APPROVED", verdict.get("reason")


def test_editing_around_a_pre_existing_violation_is_allowed() -> None:
    """The file's past is not this edit's: the import was there before it."""
    before = "import boto3\n\n\ndef total():\n    return 0\n"
    verdict = _judge("edit-intro-pre-1", "src/domain/order.py", "x = 1", before, before + "\n")
    assert verdict["status"] == "APPROVED", verdict.get("reason")


def test_a_pre_existing_secret_in_the_file_is_not_the_edit_s() -> None:
    """The before/after ride along for imports only; the edit's own text is still scanned."""
    before = f'KEY = "{SYNTHETIC_KEY}"\n\n\ndef total():\n    return 0\n'
    verdict = _judge("edit-intro-sec-1", "src/domain/order.py", "x = 1", before, before + "\n")
    assert verdict["status"] == "APPROVED", verdict.get("reason")


def test_a_watched_rule_labels_what_the_edit_introduces() -> None:
    verdict = _judge(
        "edit-intro-dry-1",
        "src/domain/order.py",
        "boto3",
        "import json\n",
        "import boto3\n",
        dry_run=True,
    )
    assert verdict["status"] == "APPROVED", verdict.get("reason")
    assert "python-domain-stays-pure" in verdict.get("observed_rules", [])


def test_a_fragment_sent_without_context_is_judged_as_before() -> None:
    """Callers that send no before/after keep today's verdict: the residual is documented, not silent."""
    event = {
        "rawPath": "/prod/evaluate-tool-call",
        "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
        "headers": {"content-type": "application/json"},
        "body": json.dumps({
            "session_id": "edit-intro-none-1",
            "project_name": "Acme-Ledger",
            "tool_name": "Edit",
            "action_type": "FILE_WRITE",
            "arguments": {"file_path": "src/domain/order.py", "content": "boto3"},
            "agent": "claude-code",
            "origin": "hook",
            "explain": False,
        }),
    }
    verdict = json.loads(lambda_handler(event, None)["body"])
    assert verdict["status"] == "APPROVED", verdict.get("reason")
