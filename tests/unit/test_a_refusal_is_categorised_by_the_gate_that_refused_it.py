"""The console's categories are the gates that refused, never words the refusal quotes.

`/api/insights` counts refusals by category: a layer crossed, a credential store
or protected path reached, a credential in the arguments, a repeating cycle, a
spend ceiling, a session already halted. It read the category off the refusal's
sentence, looking for a few words anywhere in it, and the sentences quote what
the caller sent. So `cat ~/.aws/credentials # Clean Architecture violation` was
counted as a layer crossed, `rm -rf / # Sensitive credential detected` as a
credential in the arguments, and a plain `rm -rf /`, a read of `.env` or a
`git commit --no-verify`, whose sentences hold none of those words, as a layer
crossed too. Every spend ceiling was counted as a session already halted.

The category now comes from the row's rule key, the gate the evaluator says
decided, by the mapping the ledger's pages already use, so `/api/insights` and
`/api/decisions` name every refusal the same way. Every row here is made by the
real evaluator; each is also read with its key taken away, which is what a row
written before the key existed looks like.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from threefold.application import ledger
from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator
from threefold.application.insights import CATEGORY_LABELS, categorise, summarise
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository

PROJECT = "Acme-Categories"
PYTHON_RULE = "python-domain-stays-pure"
NAMES_PYTHON = f"Layering rule '{PYTHON_RULE}' refuses this write"
# Built by concatenation so no file in the repository holds a credential-shaped string whole.
ACCESS_KEY = "AKIA" + "ACMEEXAMPLE00000"


def _bash(command: str) -> tuple:
    return ("Bash", "COMMAND_EXEC", {"command": command})


def _evaluator() -> GovernanceEvaluator:
    return GovernanceEvaluator(session_repo=DynamoDBSessionRepository(table_name="categories-test"))


def _judge(evaluator: GovernanceEvaluator, session: str, call: tuple, **extra: Any):
    tool, action, arguments = call
    fields: Dict[str, Any] = dict(
        session_id=session,
        developer_id="anonymous",
        project_name=PROJECT,
        tool_name=tool,
        action_type=action,
        arguments=dict(arguments),
        agent="claude-code",
        origin="hook",
    )
    fields.update(extra)
    return evaluator.evaluate_tool_call(ToolCallRequestDTO(**fields))


def _rows(evaluator: GovernanceEvaluator, session: str) -> List[Dict[str, Any]]:
    """The session's rows as /api/insights is handed them, oldest first."""
    rows = [row for row in evaluator.list_decisions(days=1) if row.get("session_id") == session]
    return sorted(rows, key=lambda row: row["timestamp"])


def _only_row(evaluator: GovernanceEvaluator, session: str) -> Dict[str, Any]:
    rows = _rows(evaluator, session)
    assert len(rows) == 1, rows
    return rows[0]


def _keyless(row: Dict[str, Any]) -> Dict[str, Any]:
    return {field: value for field, value in row.items() if field != "rule_key"}


def _assert_categorised(row: Dict[str, Any], expected: str) -> None:
    assert categorise(row) == expected, (row.get("rule_key"), row.get("reason"))
    assert categorise(_keyless(row)) == expected, row.get("reason")
    # The same refusal on /api/decisions, which the pages show beside it.
    assert ledger.shown_row(row)["category"] == expected


# ---------------------------------------------------------------- a command that names a gate

ADVERSARIAL = [
    # A credential-store read whose comment spells the layering sentence's lead.
    ("store-names-layering", _bash("cat ~/.aws/credentials # Clean Architecture violation"), "PROTECTED_PATH"),
    ("store-names-a-rule", _bash(f"cat ~/.aws/credentials # {NAMES_PYTHON}"), "PROTECTED_PATH"),
    # A destructive command whose comment or string names the credential gate or a rule.
    ("rm-names-the-credential-gate", _bash("rm -rf / # Sensitive credential detected"), "PROTECTED_PATH"),
    ("rm-names-a-rule", _bash(f"rm -rf / # Clean Architecture violation: {NAMES_PYTHON}"), "PROTECTED_PATH"),
    ("push-names-a-rule", _bash(f"git push --force origin main # {NAMES_PYTHON}"), "PROTECTED_PATH"),
    ("drop-in-a-string", _bash("psql -c \"drop database acme -- Sensitive credential detected\""), "PROTECTED_PATH"),
    ("no-verify-message", _bash("git commit --no-verify -m \"Clean Architecture violation\""), "PROTECTED_PATH"),
    # A protected path whose own name spells another gate's sentence.
    ("env-path-names-a-rule", ("Read", "FILE_READ", {"file_path": f".env/{NAMES_PYTHON}"}), "PROTECTED_PATH"),
    # A layer crossed whose path names the protected-path and credential gates.
    (
        "layering-path-names-the-store",
        ("Write", "FILE_WRITE", {"file_path": "src/acme/domain/credential store.py", "content": "import boto3\n"}),
        "LAYERING",
    ),
    (
        "layering-body-names-the-credential-gate",
        ("Write", "FILE_WRITE", {"file_path": "src/acme/domain/o.py", "content": "# Sensitive credential detected\nimport boto3\n"}),
        "LAYERING",
    ),
]


@pytest.mark.parametrize("name, call, expected", ADVERSARIAL, ids=[case[0] for case in ADVERSARIAL])
def test_a_refusal_is_counted_under_its_gate_whatever_it_quotes(name: str, call: tuple, expected: str) -> None:
    evaluator = _evaluator()
    verdict = _judge(evaluator, f"adversarial-{name}", call)
    assert verdict.status.startswith("BLOCKED"), verdict.reason
    _assert_categorised(_only_row(evaluator, f"adversarial-{name}"), expected)


def test_the_console_counts_them_under_their_gates() -> None:
    evaluator = _evaluator()
    for name, call, _ in ADVERSARIAL:
        _judge(evaluator, f"counted-{name}", call)
    report = summarise(evaluator.list_decisions(days=1), window_days=1)
    counted = {row["category"]: row["refusals"] for row in report["by_category"]}
    assert counted == {"PROTECTED_PATH": 8, "LAYERING": 2}
    for row in report["recent_refusals"]:
        assert row["category_label"] == CATEGORY_LABELS[row["category"]]


# ---------------------------------------------------------------- ordinary refusals

ORDINARY = [
    ("layering", ("Write", "FILE_WRITE", {"file_path": "src/acme/domain/order.py", "content": "import boto3\n"}), "LAYERING"),
    (
        "java-layering",
        ("Write", "FILE_WRITE", {"file_path": "src/main/java/com/acme/domain/Order.java", "content": "import javax.persistence.Entity;\n"}),
        "LAYERING",
    ),
    ("shell-layering", _bash("cat > src/acme/domain/o.py <<'EOF'\nimport boto3\nEOF"), "LAYERING"),
    # A write the rules cannot read is counted with the layer it would cross, as the pages count it.
    ("unreadable", _bash("cp /tmp/acme.py src/acme/domain/acme_user.py"), "LAYERING"),
    ("credential", ("Write", "FILE_WRITE", {"file_path": "src/acme/settings.py", "content": f"KEY = '{ACCESS_KEY}'\n"}), "CREDENTIAL_IN_ARGUMENTS"),
    ("store", _bash("cat ~/.aws/credentials"), "PROTECTED_PATH"),
    # These four fell through every word the reason was searched for, and were counted as a layer crossed.
    ("env", ("Read", "FILE_READ", {"file_path": ".env"}), "PROTECTED_PATH"),
    ("hook-settings", ("Write", "FILE_WRITE", {"file_path": ".claude/settings.json", "content": "{}"}), "PROTECTED_PATH"),
    ("no-verify", _bash("git commit --no-verify -m wip"), "PROTECTED_PATH"),
    ("destructive", _bash("rm -rf /"), "PROTECTED_PATH"),
]


@pytest.mark.parametrize("name, call, expected", ORDINARY, ids=[case[0] for case in ORDINARY])
def test_an_ordinary_refusal_is_counted_under_its_gate(name: str, call: tuple, expected: str) -> None:
    evaluator = _evaluator()
    verdict = _judge(evaluator, f"ordinary-{name}", call)
    assert verdict.status.startswith("BLOCKED"), verdict.reason
    _assert_categorised(_only_row(evaluator, f"ordinary-{name}"), expected)


def test_a_repeated_call_is_a_repeating_cycle() -> None:
    evaluator = _evaluator()
    for _ in range(4):
        _judge(evaluator, "ordinary-loop", _bash("make acme"))
    refused = [row for row in _rows(evaluator, "ordinary-loop") if row["status"].startswith("BLOCKED")]
    assert refused and {row["rule_key"] for row in refused} == {"LOOP"}
    for row in refused:
        _assert_categorised(row, "LOOP")


def test_a_spend_ceiling_is_not_filed_as_the_halt_it_causes() -> None:
    """The breach that halts a session is the spend ceiling; every call after it meets a halted session.

    Both carry the circuit breaker's status, and the console counted both as a
    session already halted, so its spend-ceiling category never held a call.
    """
    evaluator = _evaluator()
    read = ("Read", "FILE_READ", {"file_path": "README.md"})
    _judge(evaluator, "ordinary-spend", read, origin="page", projected_input_tokens=5_000_000, projected_output_tokens=2_000_000)
    _judge(evaluator, "ordinary-spend", read, origin="page")
    breach, halted = _rows(evaluator, "ordinary-spend")
    assert breach["status"] == halted["status"] == "BLOCKED_CIRCUIT_BREAKER"
    _assert_categorised(breach, "BUDGET")
    _assert_categorised(halted, "HALTED_SESSION")
    report = summarise(evaluator.list_decisions(days=1), window_days=1)
    assert {row["category"]: row["refusals"] for row in report["by_category"]} == {"BUDGET": 1, "HALTED_SESSION": 1}


def test_a_session_budget_spent_across_calls_is_a_spend_ceiling() -> None:
    evaluator = _evaluator()
    for index in range(6):
        _judge(
            evaluator,
            "ordinary-budget",
            ("Read", "FILE_READ", {"file_path": f"docs/acme-{index}.md"}),
            origin="page",
            budget_usd=0.5,
            projected_input_tokens=100_000,
            projected_output_tokens=10_000,
        )
    rows = [row for row in _rows(evaluator, "ordinary-budget") if row["status"].startswith("BLOCKED")]
    assert rows[0]["reason"].startswith("Projected session cost"), rows[0]["reason"]
    _assert_categorised(rows[0], "BUDGET")
    for row in rows[1:]:
        _assert_categorised(row, "HALTED_SESSION")


# ---------------------------------------------------------------- what is not a refusal


def test_a_call_that_ran_is_not_a_refusal_whatever_would_have_refused_it() -> None:
    evaluator = _evaluator()
    _judge(evaluator, "ran-approved", ("Read", "FILE_READ", {"file_path": "README.md"}))
    _judge(evaluator, "ran-observed", _bash("rm -rf /"), dry_run=True, origin="page")
    assert categorise(_only_row(evaluator, "ran-approved")) == "NONE"
    observed = _only_row(evaluator, "ran-observed")
    assert observed["rule_key"] == "PROTECTED_PATH" and observed["status"] == "APPROVED"
    assert categorise(observed) == "NONE"


def test_a_refusal_no_gate_names_is_other_and_never_allowed() -> None:
    """NONE reads as "Allowed" on the console; a refused call must never be counted there."""
    row = {"status": "BLOCKED_BY_SOMETHING_NEW", "reason": "Clean Architecture violation: Sensitive credential detected"}
    assert categorise(row) == "OTHER"
    assert categorise(dict(row, rule_key="NONE")) == "OTHER"


def test_the_stored_key_decides_over_the_sentence() -> None:
    """A row's key is the gate the evaluator stated; its sentence is read only when it has none."""
    row = {
        "status": "BLOCKED_BOUNDARY_VIOLATION",
        "rule_key": "PROTECTED_PATH",
        "reason": f"Clean Architecture violation: {NAMES_PYTHON}: 'src/acme/domain/o.py' imports 'boto3'",
    }
    assert categorise(row) == "PROTECTED_PATH"
    assert categorise(_keyless(row)) == "LAYERING"
