"""A throwaway project an anonymous visitor can promote, review and demote.

POST /api/sandbox makes `Acme-Sandbox-<8 hex>` in observe, gone a day later,
and sends a dozen synthetic hook calls from all three agents through the real
evaluator, so the dashboard has something true to show: what the shipped rules
would have refused, and one refusal a reviewer should reject.

Every call is synthetic, as the clean-room rule requires. No call asks for an
explanation, so seeding a sandbox never reaches the model.
"""
from __future__ import annotations

import datetime
import hashlib
import secrets
from typing import Any, Dict, List, Tuple

from threefold.application import projects as stages
from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.labels import is_labelled

SANDBOX_PREFIX = "Acme-Sandbox-"

# (agent, tool, action type, arguments). Arguments differ call to call, so the
# loop gate never fires and the mix below is the mix the dashboard shows.
SEEDED_CALLS: Tuple[Tuple[str, str, str, Dict[str, Any]], ...] = (
    ("claude-code", "Read", "FILE_READ", {"file_path": "README.md"}),
    ("claude-code", "Edit", "FILE_WRITE", {
        "file_path": "src/acme/billing/invoice.py",
        "old_string": "total = subtotal",
        "new_string": "total = subtotal + tax",
    }),
    # Correct: the domain layer reaching for an AWS client.
    ("claude-code", "Write", "FILE_WRITE", {
        "file_path": "src/acme/domain/order.py",
        "content": "import boto3\n\n\nclass Order:\n    pass\n",
    }),
    ("claude-code", "Bash", "COMMAND_EXEC", {"command": "pytest -q tests/billing"}),
    ("codex", "shell", "COMMAND_EXEC", {"command": "git status"}),
    # Correct: a JPA annotation inside a domain entity.
    ("codex", "apply_patch", "FILE_WRITE", {
        "file_path": "src/main/java/com/acme/domain/Order.java",
        "content": "package com.acme.domain;\n\nimport javax.persistence.Entity;\n\n@Entity\npublic class Order {}\n",
    }),
    # Correct: reading the environment file is reaching for credentials.
    ("codex", "shell", "COMMAND_EXEC", {"command": "cat .env"}),
    # The intended false alarm. `python-domain-stays-pure` covers
    # `**/domain/**/*.py`, and that glob also matches this test module under
    # tests/domain/. A test that drives the order endpoint through FastAPI's
    # TestClient is not domain code at all, so a reasonable reviewer marks this
    # call a false alarm: the rule's path pattern is wider than the layer it
    # means to protect.
    ("codex", "apply_patch", "FILE_WRITE", {
        "file_path": "tests/domain/test_order_totals.py",
        "content": (
            "from fastapi.testclient import TestClient\n\n"
            "from acme.app import app\n\n\n"
            "def test_totals_are_rounded():\n"
            "    assert TestClient(app).get('/orders/1').json()['total'] == 10.5\n"
        ),
    }),
    ("antigravity", "view_file", "FILE_READ", {"file_path": "docs/ARCHITECTURE.md"}),
    # Correct: an HTTP client imported into the web domain.
    ("antigravity", "write_to_file", "FILE_WRITE", {
        "file_path": "src/web/domain/cart.ts",
        "content": "import axios from 'axios';\n\nexport const total = (items: number[]) => items.length;\n",
    }),
    ("antigravity", "run_command", "COMMAND_EXEC", {"command": "npm run build"}),
    ("antigravity", "write_to_file", "FILE_WRITE", {
        "file_path": "src/web/components/CartView.tsx",
        "content": "import React from 'react';\n\nexport const CartView = () => null;\n",
    }),
)


def new_sandbox_name() -> str:
    return f"{SANDBOX_PREFIX}{secrets.token_hex(4)}"


def _developer(agent: str, project: str) -> str:
    """A 12-hex stand-in for a developer, as a hook computes one: never a name."""
    return hashlib.sha256(f"{project}:{agent}".encode("utf-8")).hexdigest()[:12]


def seeded_requests(project: str) -> List[ToolCallRequestDTO]:
    return [
        ToolCallRequestDTO(
            session_id=f"{project.lower()}-{agent}",
            developer_id=_developer(agent, project),
            project_name=project,
            tool_name=tool,
            action_type=action,
            arguments=dict(arguments),
            projected_input_tokens=0,
            projected_output_tokens=0,
            agent=agent,
            origin="hook",
            explain=False,
            dry_run=False,
            hook_mode="managed",
        )
        for agent, tool, action, arguments in SEEDED_CALLS
    ]


def create_sandbox(evaluator: Any) -> Dict[str, Any]:
    """Makes the project, then seeds it through the evaluator a hook would reach.

    Configured before seeding, and explicitly in observe, so the calls are
    judged as a new project's would be whatever the stack's default stage is.
    """
    name = new_sandbox_name()
    if not is_labelled(name):
        raise ValueError(
            "This deployment's AllowedProjectPattern does not admit sandbox names, so a sandbox's "
            "calls could not be recorded under its own name."
        )
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    evaluator.save_project_config(
        name,
        stages.new_config(now, stage=stages.OBSERVE, sandbox=True),
        ttl_seconds=stages.SANDBOX_TTL_SECONDS,
    )
    seeded = 0
    for request in seeded_requests(name):
        evaluator.evaluate_tool_call(request)
        seeded += 1
    return {"project": name, "calls_seeded": seeded, "url": f"dashboard.html#/projects/{name}"}
