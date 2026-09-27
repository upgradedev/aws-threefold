"""A call is counted under the gate that decided it, never under a rule its own words name.

A refusal's rule key groups calls on the review queue and in the rollups, and
an operator promotes and demotes by it. The sentences the gates write quote
what the caller sent: a destructive command and a credential-store read quote
the command line, a protected path quotes the path, and a layering refusal
quotes the target path and the import. So a comment, a string, or a file name
could spell another rule's sentence, and a reader that looked for a rule
anywhere in the words filed the call under whichever rule was spelled.

The key comes from the gate: the guard states which gate found what and which
rule it broke, the evaluator carries that beside the verdict, and the ledger
stores it. A row or verdict that carries no key is read by the gate's own words
at the head of its sentence, before anything the caller sent. Every case here
is judged in enforce, in Observe, as a dry run, and with the project watching
the very rule the call names, and the key is checked where the ledger stores
it, where a keyless copy of the row is read back, and where the rollups count
it.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import pytest

from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator
from threefold.application.rule_keys import (
    PROTECTED_PATH,
    UNREADABLE_WRITE,
    finding_key,
    refusal_key,
    rule_key,
    stored_rule_keys,
)
from threefold.domain.boundary_guard import DESTRUCTIVE_FOUND, LAYERING_FOUND, BoundaryFinding
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository, rollup_counters

PROJECT = "Acme-Keys-Words"
PYTHON_RULE = "python-domain-stays-pure"
JAVA_RULE = "java-domain-stays-pure"
# The python rule's id is the longer of the two, so a reader that tried the
# rules in force longest first anywhere in the sentence found it before the
# java rule that actually refused.
NAMES_PYTHON = f"Layering rule '{PYTHON_RULE}' refuses this write"
JAVA_PATH_NAMING_PYTHON = f"src/main/java/com/acme/domain/{NAMES_PYTHON}.java"
JPA = "import javax.persistence.Entity;\n"


def _bash(command: str) -> tuple:
    return ("Bash", "COMMAND_EXEC", {"command": command})


CASES = [
    # A destructive command, and a rule it names in a comment or a string.
    ("push-comment", _bash(f"git push --force origin main # {NAMES_PYTHON}"), PROTECTED_PATH),
    ("rm-comment-with-the-lead", _bash(f"rm -rf / # Clean Architecture violation: {NAMES_PYTHON}"), PROTECTED_PATH),
    ("rm-after-an-echo", _bash(f"echo \"Layering rule '{PYTHON_RULE}' would refuse this write\"; rm -rf /"), PROTECTED_PATH),
    ("drop-in-a-string", _bash(f"psql -c \"drop database acme -- layering rule '{PYTHON_RULE}' covers it\""), PROTECTED_PATH),
    ("rm-plain", _bash("rm -rf /"), PROTECTED_PATH),
    # A destructive command in the unreadable-write gate's own words.
    ("push-files-not-named", _bash("git push --force origin main # to files it does not name"), PROTECTED_PATH),
    ("push-too-long", _bash("git push --force origin main # this command is too long to be read to the end"), PROTECTED_PATH),
    ("push-advice", _bash("git push --force origin main # use Write or Edit so the rule can read it"), PROTECTED_PATH),
    (
        "push-unreadable-sentence",
        _bash("git push --force origin main # Clean Architecture violation: this command writes by cp"),
        PROTECTED_PATH,
    ),
    # A credential store, a protected path and hook tampering, each naming a rule.
    ("credentials-comment", _bash(f"cat ~/.aws/credentials # layering rule '{PYTHON_RULE}' covers it"), PROTECTED_PATH),
    ("env-path-names-a-rule", ("Read", "FILE_READ", {"file_path": f".env/{NAMES_PYTHON}"}), PROTECTED_PATH),
    ("no-verify-message", _bash(f"git commit --no-verify -m \"{NAMES_PYTHON}\""), PROTECTED_PATH),
    # A real layering refusal of a shell write, whatever else it names.
    ("heredoc", _bash("cat > src/acme/domain/o.py <<'EOF'\nimport boto3\nEOF"), PYTHON_RULE),
    (
        "heredoc-body-names-java",
        _bash(f"cat > src/acme/domain/o.py <<'EOF'\n# Layering rule '{JAVA_RULE}' refuses this write\nimport boto3\nEOF"),
        PYTHON_RULE,
    ),
    ("heredoc-path-names-python", _bash(f"cat > \"{JAVA_PATH_NAMING_PYTHON}\" <<'EOF'\n{JPA}EOF"), JAVA_RULE),
    ("write-path-names-python", ("Write", "FILE_WRITE", {"file_path": JAVA_PATH_NAMING_PYTHON, "content": JPA}), JAVA_RULE),
    # A write the rules cannot read, whatever its path says.
    ("cp", _bash("cp /tmp/acme.py src/acme/domain/acme_user.py"), UNREADABLE_WRITE),
    (
        "cp-path-names-java",
        _bash(f"cp /tmp/acme.py \"src/acme/domain/Layering rule '{JAVA_RULE}' refuses this write.py\""),
        UNREADABLE_WRITE,
    ),
]

# How each stage is reached: the project's configuration and what the call says of itself.
STAGES = {
    "enforce": ({"stage": "enforce", "observe_rules": []}, {"origin": "hook"}),
    "observe": ({"stage": "observe", "observe_rules": []}, {"origin": "hook"}),
    "dry_run": (None, {"origin": "page", "dry_run": True}),
    "watching_the_named_rule": ({"stage": "enforce", "observe_rules": [PYTHON_RULE]}, {"origin": "hook"}),
}


def _refused_in(stage: str, key: str) -> bool:
    if stage in ("observe", "dry_run"):
        return False
    return not (stage == "watching_the_named_rule" and key == PYTHON_RULE)


def _evaluator(config) -> GovernanceEvaluator:
    made = GovernanceEvaluator(session_repo=DynamoDBSessionRepository(table_name="rule-key-words-test"))
    if config is not None:
        made.save_project_config(PROJECT, config)
    return made


def _judge(evaluator: GovernanceEvaluator, session: str, call: tuple, **extra):
    tool, action, arguments = call
    return evaluator.evaluate_tool_call(
        ToolCallRequestDTO(
            session_id=session,
            developer_id="anonymous",
            project_name=PROJECT,
            tool_name=tool,
            action_type=action,
            arguments=dict(arguments),
            agent="claude-code",
            **extra,
        )
    )


def _row(evaluator: GovernanceEvaluator, session: str) -> dict:
    rows = [row for row in evaluator.session_repo.list_decisions(days=1) if row.get("session_id") == session]
    assert len(rows) == 1, rows
    return rows[0]


def _counted_under(row: dict) -> list:
    counters, _ = rollup_counters(row)
    return sorted(name for name in counters if name.startswith(("refused:", "observed:")))


@pytest.mark.parametrize("stage", list(STAGES))
@pytest.mark.parametrize("name, call, expected", CASES, ids=[case[0] for case in CASES])
def test_the_key_is_the_gate_that_decided(stage: str, name: str, call: tuple, expected: str) -> None:
    config, extra = STAGES[stage]
    evaluator = _evaluator(config)
    session = f"words-{stage}-{name}"
    verdict = _judge(evaluator, session, call, **extra)
    row = _row(evaluator, session)
    refused = _refused_in(stage, expected)

    if refused:
        assert verdict.status == "BLOCKED_BOUNDARY_VIOLATION", verdict.reason
    else:
        assert verdict.status == "APPROVED", verdict.reason
        assert row["observed_rules"][0] == expected
    assert row["rule_key"] == expected, row["reason"]

    # Counted where the rollups count it, and reviewed under the same key.
    assert _counted_under(row) == [f"{'refused' if refused else 'observed'}:{expected}"]
    assert stored_rule_keys(row)[0] == expected

    # A keyless copy of the row, which is what a row written before the key
    # existed looks like, and what the backfill reads.
    keyless = {field: value for field, value in row.items() if field != "rule_key"}
    rules = evaluator.rules_in_force(PROJECT)[0]
    assert rule_key(keyless) == expected, keyless.get("observed_reason") or keyless.get("reason")
    assert rule_key(keyless, [rule["id"] for rule in rules]) == expected

    # A verdict that carries no gate is read the same way.
    verdict.decided_key = None
    assert GovernanceEvaluator._rule_key(verdict, rules) == expected


# ---------------------------------------------------------------- the guard's finding


def test_a_layering_finding_is_keyed_by_its_rule_not_by_its_sentence() -> None:
    sentence = f"Clean Architecture violation: Layering rule '{JAVA_RULE}' refuses this write: 'x' imports 'y'"
    assert finding_key(BoundaryFinding(LAYERING_FOUND, sentence, rule_id=PYTHON_RULE)) == PYTHON_RULE


def test_a_layering_finding_that_names_no_rule_is_not_filed_under_the_one_its_words_name() -> None:
    sentence = f"Clean Architecture violation: Layering rule '{JAVA_RULE}' refuses this write: 'x' imports 'y'"
    assert finding_key(BoundaryFinding(LAYERING_FOUND, sentence)) == PROTECTED_PATH


def test_a_destructive_finding_is_its_gate_whatever_its_sentence_quotes() -> None:
    sentence = f"Command 'rm -rf / # {NAMES_PYTHON}' contains a destructive operation"
    assert finding_key(BoundaryFinding(DESTRUCTIVE_FOUND, sentence)) == PROTECTED_PATH


# ---------------------------------------------------------------- rows that carry no key


def test_an_observed_gate_key_is_not_moved_by_its_reason() -> None:
    """The observation names the gate; its reason quotes the command that named another."""
    row = {
        "status": "APPROVED",
        "observed_rules": ["PROTECTED_PATH"],
        "observed_reason": "Command 'git push --force origin main # to files it does not name' contains a destructive operation",
    }
    assert rule_key(row) == PROTECTED_PATH


def test_a_legacy_observation_under_an_invariant_is_read_by_its_head() -> None:
    row = {
        "status": "APPROVED",
        "observed_rules": ["ARCHITECTURAL_BOUNDARY_SAFE"],
        "observed_reason": f"Command 'rm -rf / # Clean Architecture violation: {NAMES_PYTHON}' contains a destructive operation",
    }
    assert rule_key(row, [PYTHON_RULE, JAVA_RULE]) == PROTECTED_PATH


@pytest.mark.parametrize(
    "reason",
    [
        f"Target path '.env/{NAMES_PYTHON}' is protected by architectural governance",
        f"Target path '.claude/{NAMES_PYTHON}/settings.json' is protected by architectural governance: it decides",
        f"Command turns the repository's hooks off: git -c {NAMES_PYTHON}=... points the hooks somewhere else",
    ],
)
def test_a_sentence_that_quotes_the_caller_is_filed_with_its_gate(reason: str) -> None:
    assert refusal_key("BLOCKED_BOUNDARY_VIOLATION", reason) == PROTECTED_PATH
    assert refusal_key("BLOCKED_BOUNDARY_VIOLATION", reason, [PYTHON_RULE, JAVA_RULE]) == PROTECTED_PATH


def test_a_rule_named_after_the_one_that_refused_is_not_read() -> None:
    reason = (
        f"Clean Architecture violation: Layering rule '{JAVA_RULE}' refuses this write: A Java class under "
        f"domain/ may not reach persistence. '{JAVA_PATH_NAMING_PYTHON}' imports 'javax.persistence.Entity'"
    )
    assert refusal_key("BLOCKED_BOUNDARY_VIOLATION", reason, [PYTHON_RULE, JAVA_RULE]) == JAVA_RULE
    assert refusal_key("BLOCKED_BOUNDARY_VIOLATION", reason) == JAVA_RULE


# ---------------------------------------------------------------- ordinary calls


ORDINARY = [
    ("Write", "FILE_WRITE", {"file_path": "src/acme/domain/order.py", "content": "import boto3\n"}),
    ("Write", "FILE_WRITE", {"file_path": "src/main/java/com/acme/domain/Order.java", "content": JPA}),
    ("Write", "FILE_WRITE", {"file_path": "src/Acme/Domain/Order.cs", "content": "using System.Data.SqlClient;\n"}),
    ("Write", "FILE_WRITE", {"file_path": "src/web/domain/cart.ts", "content": "import axios from 'axios';\n"}),
    ("Read", "FILE_READ", {"file_path": ".env"}),
    ("Read", "FILE_READ", {"file_path": "README.md"}),
    ("Write", "FILE_WRITE", {"file_path": ".claude/settings.json", "content": "{}"}),
    _bash("export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"),
    _bash("cat .env"),
    _bash("git commit --no-verify -m wip"),
    _bash("rm -rf /"),
    _bash("cat > src/acme/domain/o.py <<'EOF'\nimport boto3\nEOF"),
    _bash("echo 'import boto3' > src/acme/domain/$NAME.py"),
    _bash("cp /tmp/acme.py src/domain/acme_user.py"),
    _bash("git apply /tmp/fix.patch"),
    # The sentence for a directory tree runs past the 240 characters the
    # ledger keeps, and loses its advice to the cut; its head still says it.
    _bash("cp -r /tmp/pkg src/acme/domain/"),
    _bash("git status"),
]


@pytest.mark.parametrize("stage", ["enforce", "observe"])
def test_an_ordinary_row_reads_back_as_the_gate_that_wrote_it(stage: str) -> None:
    """The reading of a keyless row agrees with the key the gates stated.

    Every rule here enforces. A rule whose own mode is observe lists its id for
    a write it could not read, while the reading of the same row names the
    unreadable-write policy; the two disagree today and that is left to the
    contract to settle, so it is not asserted here either way.
    """
    evaluator = _evaluator(STAGES[stage][0])
    rules = evaluator.rules_in_force(PROJECT)[0]
    for index, call in enumerate(ORDINARY):
        session = f"ordinary-{stage}-{index}"
        _judge(evaluator, session, call, origin="hook")
        row = _row(evaluator, session)
        keyless = {field: value for field, value in row.items() if field != "rule_key"}
        assert rule_key(keyless) == row["rule_key"], (call, row.get("reason"), row.get("observed_reason"))
        assert rule_key(keyless, [rule["id"] for rule in rules]) == row["rule_key"], call
