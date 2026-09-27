"""A layering rule may not be named after a gate's key.

A rule's id is its rule_key. A rule saved as `LOOP` or `BUDGET` would be keyed,
rolled up, categorised and given fixes as that gate everywhere, so a save
naming one is refused, and the domain's list of reserved ids stays the list the
application counts gates under.
"""
from __future__ import annotations

from threefold.application import rule_keys
from threefold.domain import layering_rules


def _rule(rule_id: str) -> dict:
    return {"id": rule_id, "when_path_matches": ["src/domain/**/*.py"], "forbid_imports": ["boto3"]}


def test_the_reserved_ids_are_the_keys_the_gates_are_counted_under() -> None:
    assert set(layering_rules.RESERVED_IDS) == set(rule_keys.FIXED_KEYS)


def test_a_rule_named_after_a_gate_is_refused_and_says_why() -> None:
    for key in rule_keys.FIXED_KEYS:
        usable, problems = layering_rules.validate_rules([_rule(key)])
        assert usable == [], key
        assert problems and "the key a gate is counted under" in problems[0]["reason"], key


def test_an_ordinary_rule_and_a_lower_case_name_still_save() -> None:
    usable, problems = layering_rules.validate_rules([_rule("python-domain-stays-pure"), _rule("loop")])
    assert problems == []
    assert [rule["id"] for rule in usable] == ["python-domain-stays-pure", "loop"]
