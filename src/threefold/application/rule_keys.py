"""Which rule decided a call, as the one key readiness, reviews and stages group by.

The ledger already names the invariant that failed (`rule`), and a boundary
refusal fails the same invariant whether a layer was crossed, a credential store
was reached or a write could not be read. An operator promotes and demotes
rules, not invariants, so every row also carries `rule_key`: the id of the
layering rule that decided when one did, otherwise one of the fixed gate keys
below, or NONE when nothing flagged the call.

A verdict's key is the gate that decided it, as the guard states it
(`finding_key`): the evaluator carries it beside the verdict and the ledger
stores it. A sentence is read only for a row or a verdict that carries no key,
such as a row written before the key existed, and then only by the gate's own
words at its head.
Every sentence that names a rule opens with the rule, and everything the caller
sent (a path, an import, the command itself) comes after it, so nothing the
caller wrote can decide which key a row is counted under. The exception is the
unreadable-write gate's "this command ..." sentences, one of which names its
rule only at the end: they are recognised by that opening alone and never read
for a rule.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from threefold.domain.boundary_guard import (
    COMMAND_PATH_FOUND,
    CREDENTIAL_FOUND,
    DESTRUCTIVE_FOUND,
    GOVERNANCE_FOUND,
    LAYERING_FOUND,
    PROTECTED_PATH_FOUND,
    TAMPERING_FOUND,
    UNREADABLE_FOUND,
)

LOOP = "LOOP"
PROTECTED_PATH = "PROTECTED_PATH"
UNREADABLE_WRITE = "UNREADABLE_WRITE"
CREDENTIAL = "CREDENTIAL"
BUDGET = "BUDGET"
HALTED_SESSION = "HALTED_SESSION"
NONE = "NONE"

# The gates a project can stage one by one, beside its layering rules. A
# credential is not among them: it is refused on the machine before it is ever
# sent, and a project in enforce never waves one through. A halted session is
# not either, because it is the consequence of another gate, not a rule.
GATE_KEYS = (LOOP, PROTECTED_PATH, UNREADABLE_WRITE, BUDGET)
FIXED_KEYS = (LOOP, PROTECTED_PATH, UNREADABLE_WRITE, CREDENTIAL, BUDGET, HALTED_SESSION, NONE)

# How a refusal that comes from the session's own state begins. The evaluator
# writes these sentences; they live here so the reading and the writing cannot
# drift apart.
FROZEN_SESSION_REASON = "Session execution frozen"
HALTED_SESSION_REASONS = (FROZEN_SESSION_REASON, "Session already tripped")

# How the gates open every sentence that names a layering rule: a refusal leads
# with the violation, an observation with the rule, and the rule's id comes
# straight after, before anything the caller sent. The id and what decided are
# read there and nowhere else. The exception is the unreadable-write gate's
# "this command ..." sentences, one of which names its rule only at the end:
# they are recognised by that opening alone (_UNREADABLE_HEAD below) and never
# read for a rule. A sentence that quotes the caller (a destructive command, a
# credential store, a protected or governance path, hook tampering) opens with
# "Command" or "Target path" instead, so nothing inside its quotes is ever
# read: a comment naming a rule is part of the command, not the head.
_VIOLATION_LEAD = "Clean Architecture violation: "
_RULE_LEADS = ("Layering rule '", "layering rule '")
_RULE_VERBS = ("refuses", "would refuse", "covers")

# An id the rules in force do not know, read where the lead ends. At most 80
# characters, which bounds the search on a hostile reason.
_ID_AFTER_LEAD = re.compile(r"(.{1,80}?)' (?=refuses|would refuse|covers)", re.DOTALL)

# A write the rules could not read. Checked before the rule's name, because a
# refusal for an unreadable write to a covered path names the rule as well:
# it is the unreadable-write policy that decided, not the rule's import list,
# and an operator stages the two apart. Each is the gate's own phrase in full,
# at the head or right after the rule's id, so a stored reason cut at 240
# characters is still recognised. An import refusal puts the rule's
# description after "this write: ", and that is the operator's to word, which
# is why a bare "the command" would not be enough to tell the two apart.
_UNREADABLE_HEAD = _VIOLATION_LEAD + "this command "
_UNREADABLE_AFTER_ID = "covers '"
_THIS_WRITE = ("refuses this write: ", "would refuse this write: ")
_UNREADABLE_AFTER_VERB = (
    "the command changes part of a line in '",
    "the command brings a directory tree into '",
    "the command names '",
    "the command writes '",
    "the command writes files it cannot name",
)

# A dry run and an observe-stage call record the invariant that failed as their
# observed rule. Read back as the refusal it would have been, so the key names
# the gate or the layering rule rather than the invariant.
_STATUS_FOR_INVARIANT = {
    "SECRET_LEAKAGE_FREE": "BLOCKED_SECRET_DETECTED",
    "ARCHITECTURAL_BOUNDARY_SAFE": "BLOCKED_BOUNDARY_VIOLATION",
    "LOOP_THRASHING_FREE": "BLOCKED_LOOP_DETECTED",
    "BUDGET_CIRCUIT_BREAKER_SAFE": "BLOCKED_CIRCUIT_BREAKER",
    "SESSION_ALREADY_HALTED": "BLOCKED_CIRCUIT_BREAKER",
}

# What a reader recognises each key as, in the words insights already uses.
_CATEGORY_FOR_KEY = {
    CREDENTIAL: "CREDENTIAL_IN_ARGUMENTS",
    LOOP: "LOOP",
    PROTECTED_PATH: "PROTECTED_PATH",
    BUDGET: "BUDGET",
    HALTED_SESSION: "HALTED_SESSION",
    UNREADABLE_WRITE: "LAYERING",
    NONE: "NONE",
}


# Which key each of the guard's gates is staged under. A destructive command
# and a command that reaches a credential store are filed with the protected
# path: the contract's keys are closed and name no gate of their own for them,
# they are refused by the same guard on the same invariant, and an operator
# stages them together.
_KEY_BY_FINDING = {
    CREDENTIAL_FOUND: CREDENTIAL,
    PROTECTED_PATH_FOUND: PROTECTED_PATH,
    GOVERNANCE_FOUND: PROTECTED_PATH,
    TAMPERING_FOUND: PROTECTED_PATH,
    DESTRUCTIVE_FOUND: PROTECTED_PATH,
    COMMAND_PATH_FOUND: PROTECTED_PATH,
    UNREADABLE_FOUND: UNREADABLE_WRITE,
}


def finding_key(finding: Any) -> str:
    """The key of one thing the guard found, taken from the gate that found it.

    The sentences below quote the caller's own command - a destructive command
    and a command-protected path both embed its first 120 characters, and a
    layering refusal embeds the target path - so reading the key back out of
    them let a trailing comment file a refusal under whichever rule it named.
    A project that observes that rule then approved the call. The gate is a
    fact the guard states; `refusal_key` below stays for a stored row, which is
    all that is left of a verdict nobody carried the finding from.

    A layering finding is keyed by the rule the guard says it broke and by
    nothing else. Its sentence carries the target path and the import, both
    the caller's, and a path can spell another rule's sentence as readily as a
    comment can. A layering finding that named no rule would be filed with the
    guard's other gates, never under a rule its words suggest; the guard names
    one on every layering finding it makes.
    """
    if finding.kind == LAYERING_FOUND:
        return finding.rule_id or PROTECTED_PATH
    return _KEY_BY_FINDING.get(finding.kind, PROTECTED_PATH)


def _named_rule(reason: str, known_ids: Iterable[str] = ()) -> Optional[Tuple[str, str]]:
    """The rule a gate's sentence opens with, and the gate's words after it, or None.

    The rules in force are tried first, longest id first, so an id that
    contains a quote or a space is still read whole. A stored row whose rules
    are gone falls back to the pattern. Both read at the head only: the rule's
    id is the first thing after the lead, and a rule named anywhere later was
    named by whatever the caller sent.
    """
    text = reason or ""
    if text.startswith(_VIOLATION_LEAD):
        text = text[len(_VIOLATION_LEAD):]
    lead = next((lead for lead in _RULE_LEADS if text.startswith(lead)), None)
    if lead is None:
        return None
    head = text[len(lead):]
    for rule_id in sorted((i for i in known_ids if i), key=len, reverse=True):
        if head.startswith(f"{rule_id}' "):
            rest = head[len(rule_id) + 2:]
            if rest.startswith(_RULE_VERBS):
                return rule_id, rest
    found = _ID_AFTER_LEAD.match(head)
    return (found.group(1), head[found.end():]) if found else None


def is_unreadable_write(reason: str, known_ids: Iterable[str] = ()) -> bool:
    """Whether the gate's sentence says the rules could not read the write."""
    text = reason or ""
    if text.startswith(_UNREADABLE_HEAD):
        return True
    named = _named_rule(text, known_ids)
    if named is None:
        return False
    rest = named[1]
    if rest.startswith(_UNREADABLE_AFTER_ID):
        return True
    verb = next((verb for verb in _THIS_WRITE if rest.startswith(verb)), None)
    return verb is not None and rest[len(verb):].startswith(_UNREADABLE_AFTER_VERB)


def layering_rule_named(reason: str, known_ids: Iterable[str] = ()) -> Optional[str]:
    """The layering rule a gate's sentence opens with, or None."""
    named = _named_rule(reason, known_ids)
    return named[0] if named else None


def refusal_key(status: str, reason: str, known_ids: Iterable[str] = ()) -> str:
    """The key of a refusal, from its status and the sentence the gate wrote.

    A destructive command (`rm -rf /`, a force push, `drop database`) is filed
    under PROTECTED_PATH. The contract's keys are closed and it names no gate
    for these; they are refused by the same guard, on the same invariant, as
    a protected path, and are staged with it. Its sentence quotes the command
    and opens with "Command", so a rule the command names in a comment or a
    string is never read as the rule that refused it.
    """
    upper = (status or "").upper()
    if not upper.startswith("BLOCKED"):
        return NONE
    if "SECRET" in upper:
        return CREDENTIAL
    if "LOOP" in upper:
        return LOOP
    if "CIRCUIT_BREAKER" in upper:
        return HALTED_SESSION if (reason or "").startswith(HALTED_SESSION_REASONS) else BUDGET
    if "BOUNDARY" in upper:
        if is_unreadable_write(reason, known_ids):
            return UNREADABLE_WRITE
        return layering_rule_named(reason, known_ids) or PROTECTED_PATH
    return NONE


def _observed_rules(row: Mapping[str, Any]) -> list:
    observed = row.get("observed_rules")
    if observed:
        return [str(item) for item in observed if item]
    single = row.get("observed_rule")
    return [str(single)] if single else []


def _observed_reason(row: Mapping[str, Any]) -> str:
    reason = row.get("observed_reason")
    if reason:
        return str(reason)
    observations = row.get("observations") or []
    return str(observations[0]) if observations else ""


def rule_key(row: Mapping[str, Any], known_ids: Iterable[str] = ()) -> str:
    """The key for a verdict or a ledger row, refusal and observation alike.

    Takes the verdict's own fields (`status`, `reason`, `observed_rules`,
    `observations`) or a stored row's (`observed_reason` in place of
    `observations`), so a row written before the key existed is given one on
    the way out by exactly the rule that gives it to a new one.
    """
    status = str(row.get("status") or "")
    if status.upper().startswith("BLOCKED"):
        return refusal_key(status, str(row.get("reason") or ""), known_ids)
    observed = _observed_rules(row)
    if not observed:
        # A repeated read or poll leaves a note on its approval and names no
        # rule, because no gate would have refused it.
        return NONE
    reason = _observed_reason(row)
    first = observed[0]
    if first in _STATUS_FOR_INVARIANT:
        return refusal_key(_STATUS_FOR_INVARIANT[first], reason, known_ids)
    if first in FIXED_KEYS:
        # A gate's own key, as the gates stated it. The reason beside it can
        # quote the caller's command, and nothing in it moves the key.
        return first
    if is_unreadable_write(reason, known_ids):
        # A rule watching a write it could not read lists its own id, and it
        # is the unreadable-write policy that would have refused the call.
        return UNREADABLE_WRITE
    return first


def stored_rule_key(row: Mapping[str, Any]) -> str:
    """The key a stored row carries, or the one it would have been given."""
    key = row.get("rule_key")
    if isinstance(key, str) and key:
        return key
    return rule_key(row)


def stored_rule_keys(row: Mapping[str, Any]) -> list:
    """Every key a stored row is counted under, the way its rollup counted it.

    One for a refusal: the gate that decided. For an observation, every rule
    that would have refused it, because the rollup counted it once for each.
    A review of the row is therefore a review for each of them - the reviewer
    labels the call, and each of those rules flagged that same call - and
    without that the rules past the first would read unreviewed for ever and
    never become promotable.

    The same agreement the store asks for: the keys are used only when the
    row's `rule_key` is among them, so a row written by an older writer is
    counted and reviewed under its one key exactly as it was.
    """
    key = stored_rule_key(row)
    if is_refusal(row):
        return [key]
    observed = [str(name) for name in _observed_rules(row) if name]
    return list(dict.fromkeys([key] + observed)) if key in observed else [key]


def is_refusal(row: Mapping[str, Any]) -> bool:
    return str(row.get("status") or "").upper().startswith("BLOCKED")


def kind_of(row: Mapping[str, Any]) -> str:
    """refused, observed (ran, and something would have refused it) or approved.

    The three are disjoint, so a day's calls are their sum and a stacked chart
    of them adds up.
    """
    if is_refusal(row):
        return "refused"
    return "observed" if stored_rule_key(row) != NONE else "approved"


def category_for(key: str) -> str:
    """What a reader would call a call flagged under this key."""
    if key in _CATEGORY_FOR_KEY:
        return _CATEGORY_FOR_KEY[key]
    return "LAYERING"


def with_rule_key(row: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of a row that is sure to carry its key."""
    shown = dict(row)
    shown["rule_key"] = stored_rule_key(row)
    return shown
