"""Which rule decided a call, as the one key readiness, reviews and stages group by.

The ledger already names the invariant that failed (`rule`), and a boundary
refusal fails the same invariant whether a layer was crossed, a credential store
was reached or a write could not be read. An operator promotes and demotes
rules, not invariants, so every row also carries `rule_key`: the id of the
layering rule that decided when one did, otherwise one of the fixed gate keys
below, or NONE when nothing flagged the call.

The guard returns a sentence rather than a structure, and it is shared with the
hook's bundle, so the key is read off the verdict here rather than by changing
what the domain returns. The sentences it reads are the guard's own and are
pinned by its tests; the matching follows `insights.CATEGORY_BY_REASON`.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping, Optional

from threefold.domain.boundary_guard import (
    COMMAND_PATH_FOUND,
    CREDENTIAL_FOUND,
    DESTRUCTIVE_FOUND,
    GOVERNANCE_FOUND,
    LAYERING_FOUND,
    PROTECTED_PATH_FOUND,
    TAMPERING_FOUND,
    UNREADABLE_FOUND,
    UNREADABLE_WRITE as UNREADABLE_WRITE_ADVICE,
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

# A layering rule names itself in every sentence it writes: "Layering rule 'x'
# refuses this write", "would refuse", or "layering rule 'x' covers ...". The id
# is at most 80 characters, which bounds the search on a hostile reason.
_LAYERING_RULE = re.compile(r"[Ll]ayering rule '(.{1,80}?)' (?:refuses|would refuse|covers)")

# A write the rules could not read. Checked before the rule's name, because a
# refusal for an unreadable write to a covered path names the rule as well:
# it is the unreadable-write policy that decided, not the rule's import list,
# and an operator stages the two apart. The last two markers sit early in their
# sentences, so a stored reason cut at 240 characters is still recognised.
_UNREADABLE_MARKERS = (
    UNREADABLE_WRITE_ADVICE,
    "too long to be read to the end",
    "to files it does not name",
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

    The sentences below quote the caller's own command — a destructive command
    and a command-protected path both embed its first 120 characters, and a
    layering refusal embeds the target path — so reading the key back out of
    them let a trailing comment file a refusal under whichever rule it named.
    A project that observes that rule then approved the call. The gate is a
    fact the guard states; `refusal_key` below stays for a stored row, which is
    all that is left of a verdict nobody carried the finding from.
    """
    if finding.kind == LAYERING_FOUND:
        return finding.rule_id or layering_rule_named(finding.reason) or PROTECTED_PATH
    return _KEY_BY_FINDING.get(finding.kind, PROTECTED_PATH)


def is_unreadable_write(reason: str) -> bool:
    return any(marker in (reason or "") for marker in _UNREADABLE_MARKERS)


def layering_rule_named(reason: str, known_ids: Iterable[str] = ()) -> Optional[str]:
    """The layering rule a sentence names, or None.

    The rules in force are tried first, longest id first, so an id that
    contains a quote or a space is still read whole. A stored row whose rules
    are gone falls back to the pattern.
    """
    text = reason or ""
    for rule_id in sorted((i for i in known_ids if i), key=len, reverse=True):
        if f"ayering rule '{rule_id}'" in text:
            return rule_id
    found = _LAYERING_RULE.search(text)
    return found.group(1) if found else None


# The two sentences that quote the caller's own command line, and so are the
# two a caller could write the rest of this module's vocabulary into. Both come
# from the same gate and are filed under the same key, so they are recognised by
# their own frame before anything inside the quotes is read. A layering refusal
# opens with "Clean Architecture violation" or "Layering rule", a governance one
# with "Target path", and command tampering with "Command turns", so none of
# them can be taken for one of these.
_QUOTES_THE_COMMAND = (
    "' reaches a protected path or credential store",
    "' contains a destructive operation",
)


def quotes_the_command(reason: str) -> bool:
    text = reason or ""
    return text.startswith("Command '") and any(marker in text for marker in _QUOTES_THE_COMMAND)


def refusal_key(status: str, reason: str, known_ids: Iterable[str] = ()) -> str:
    """The key of a refusal, from its status and the sentence the gate wrote.

    A destructive command (`rm -rf /`, a force push, `drop database`) is filed
    under PROTECTED_PATH. The contract's keys are closed and it names no gate
    for these; they are refused by the same guard, on the same invariant, as
    a protected path, and are staged with it.
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
        if quotes_the_command(reason):
            return PROTECTED_PATH
        if is_unreadable_write(reason):
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
    if is_unreadable_write(reason):
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
    A review of the row is therefore a review for each of them — the reviewer
    labels the call, and each of those rules flagged that same call — and
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
