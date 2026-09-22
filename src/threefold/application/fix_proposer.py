r"""A concrete fix for a refusal, checked against the same gates before it is offered.

A refusal that says only "no" sends an agent back to guess, and its next guess is
often the same call spelled differently. A fix that would itself be refused is
worse than none: the agent follows it, is refused again, and learns that the
advice is noise. So every fix this module offers with code in it has been run
through the gates that refused the original call, with the same rules, and it
says so in `validated` and `checks`. Nothing here asks a model for the fix. An
optional phrasing hook may reword the summary; it never touches the fix.

What `writes` means. Threefold never sees a file on disk, only what a call
carried. So a write is "send this instead of what you sent", not "the file as it
should be": for a Write it is the whole content, for an Edit it is the new text
beside the same `old_string`, and for text a shell command added to a file whose
other lines Threefold never saw it is marked `partial`, to be applied with an
Edit where the command put it.

The shape:

    {kind, summary, steps, writes, validated, checks}

- kind: layering, credential, protected_path, unreadable_write, loop, budget,
  halted_session or destructive_command.
- summary: one line of at most 200 characters, safe to append to a hook's deny
  reason: no secret, no newline, no control character.
- steps: what to do, in order, in words.
- writes: [{path, content}], with `old_string` for an Edit retry, `partial`
  for shell-added text, and `new_file` for a port or an adapter Threefold
  proposes as a new file (it cannot see whether one is already there, and the
  steps say to add to it rather than overwrite it if so). Left out when the
  files come to more than 8 KB, and the steps say so; they were still checked
  in full.
- validated: true only when every proposed write was run through the gates and
  passed: every layering rule in any mode, the credential scan, and the whole
  boundary guard, with the rules the call was judged by. Every Python file this
  module writes whole (a port, an adapter, a domain file that parsed before) is
  also parsed, because a file that does not parse is not a fix whatever the
  gates say. Never true for advice in words, for a loop, or for content
  Threefold could not see. It does not claim the rewritten code behaves as the
  original did: the gates judge imports and credentials, not meaning.
- checks: [{gate, path, passed}] for every check that was run.

Which fix a refusal gets is decided by the verdict's family (a rule_key when the
response carries one, else its rule_evaluations and status) and then by asking
the questions the boundary guard asks, in the guard's order, of the call itself.
Reason text is read in four places only, all of them for detail rather than for
the decision, and each says less when the text does not match rather than
guessing: a loop's repeat count and cycle, a halted session's cause, which of
the circuit breaker's two cost sentences refused the call, and (until the
rule_key reaches every verdict) the evaluator's own halted-session prefix, the
same one the evaluator itself relies on.

A detected secret is never repeated. Fixes replace it with an environment lookup;
every string that leaves this module is passed through the redaction the ledger
uses; and the text of every secret found in the call, including the body of a
private key the scanner knows only by its header, is withheld from the answer
wherever it would otherwise appear.

What it costs. A layering fix rewrites at most `max_content_chars` of a call's
writes, taken together, counting every write in the call and not only those a
rule flags: MAX_CONTENT_CHARS (24 KB) unless the caller says otherwise, and the
evaluator passes 1,500. Past it the fix is advice in words from the one read of
the imports the diagnosis already made: the imports to move (the first four by
name, then how many more), the rules that forbid them, and the first candidate
layer where no rule forbids them, found by matching those imports against the
rules at each candidate adapter path, with no second parse and nothing
written. Each distinct import is matched against the rules once per proposal,
however many questions are asked of it, and only against the patterns that
could catch it (_flagged_modules, _catchable); the first version matched every
import four times against every pattern, and a Python file of 500 short
imports at 7,947 characters cost four times a whole verdict [PRIMARY],
2026-09-22. Below the cap, a layering fix reads the file a fixed number of
times (about six parses, however many imports it moves; a test holds this),
then judges the small port and adapter. Measured on the development machine
[PRIMARY], 2026-09-22, medians: the shipped fixtures of 170 to 330 bytes take 2
to 5 ms; files just under 24 KB take about 100 ms for Python or TypeScript and
45 ms for Java, five to seven times what the gate alone takes on the same file,
however many of their imports move. What the advice costs against a whole
verdict, and so where the evaluator stops asking for it, is measured and
recorded beside FIX_LAYERING_ADVICE_MAX_CHARS in application/evaluator.py.
Not measured on the Lambda, whose 256 MB share of a core will make both slower.
A refused call pays this once, on top of the gate; an approved call never does.

Wiring (for the owner, after B1 merges; this track changes none of these files):

1. Response field `suggested_fix`: add `suggested_fix: Optional[Dict[str, Any]]
   = None` to EvaluationResultDTO (src/threefold/application/dtos.py). to_dict
   is asdict, so it reaches the /evaluate-tool-call response unchanged; add it
   as a nullable object to the EvaluationResult schema in the shared
   src/threefold/web/openapi.json and docs/openapi.yaml, one line each.
2. Where: GovernanceEvaluator.evaluate_tool_call in
   src/threefold/application/evaluator.py, after `result = self._decide(request,
   rules, ...)` and before `self._record_decision(request, result)`, passing the
   same `rules` list resolved at the top of that method, so the fix is checked
   against exactly the rules that judged the call:
       if result.status != "APPROVED" or (request.explain and result.observations):
           result.suggested_fix = propose_fix(request, result, rules)
   Compute it only when someone will read it: a refusal, which the hook prints,
   or a page call (`explain: true`) that shows an observation. In Observe, the
   default stage, the hook prints nothing on approval, so a fix computed for a
   hook's would-refuse costs time on every such call and reaches no agent. Pass
   no `phrase` on this path: a model call has no place inside a verdict's
   latency.
3. Never store it. `writes` carry the agent's own source code. `_record_decision`
   must not copy `suggested_fix` into the ledger row, and `public_row`
   (src/threefold/application/labels.py) must never expose it, above all on a
   stack with PublicReads=true, where every ledger row is readable by anyone. If
   the dashboard wants a column, store only `kind` and `validated`, after asking
   the owner for the field, as the contract requires.
4. Hook: in `refusal_reason()` in src/threefold/hooks/threefold_hook.py, after
   the explanation line, append the summary as its own line:
       fix = verdict.get("suggested_fix")
       if isinstance(fix, dict) and isinstance(fix.get("summary"), str):
           label = "Suggested fix, checked by Threefold" if fix.get("validated") is True else "Suggested fix"
           summary = re.sub(r"[\x00-\x1f\x7f]+", " ", fix["summary"])[:200]
           detail += f"\n{label}: {summary}"
   The hook strips control characters itself (it has no helper for that
   today; `re` is already imported there) because it trusts nothing the
   network sends, although this module sends one clean line. Only the summary
   goes into the deny reason, never the
   writes; `deny()` then carries it as permissionDecisionReason (Claude Code,
   Codex) or reason (Antigravity). A stack without the field changes nothing.
5. Budget: the hook gives up after DEFAULT_TIMEOUT_SECONDS = 4.0 and fails open,
   and the Lambda has Timeout 15 at 256 MB. The cost above is what keeps a large
   refused write inside that; raising MAX_CONTENT_CHARS spends it.
"""
from __future__ import annotations

import ast
import functools
import json
import keyword
import logging
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Collection, Dict, List, Optional, Sequence, Set, Tuple

from threefold.domain import imports as import_readers
from threefold.domain import layering_rules as layering_internals
from threefold.domain.boundary_guard import (
    COMMAND_KEYS,
    COMMAND_PATH_FOUND,
    CONTENT_KEYS,
    CREDENTIAL_FOUND,
    DESTRUCTIVE_FOUND,
    GOVERNANCE_FOUND,
    LAYERING_FOUND,
    PATH_KEYS,
    PROTECTED_PATH_FOUND,
    REMOVED_KEYS,
    TAMPERING_FOUND,
    UNREADABLE_FOUND,
    UNREADABLE_WRITE,
    ArchitecturalBoundaryGuard,
    SecretScanner,
    analysed,
    command_cwd,
    describe_target,
    governed_write_targets,
    iter_string_leaves,
    iter_write_targets,
    looks_like_path,
    named_path,
    not_file_words,
    observe_layering,
    redact_secrets,
    shell_command,
    shell_command_at,
    shell_findings,
    shell_observations,
    shell_refusal,
    target_paths,
    write_pairs,
)
from threefold.application.rule_keys import (
    CREDENTIAL as CREDENTIAL_KEY,
    PROTECTED_PATH as PROTECTED_PATH_KEY,
    finding_key,
)
from threefold.domain.imports import declared_imports, language_for
from threefold.domain.layering_rules import (
    DEFAULT_RULES,
    ENFORCE,
    normalise_rules,
    rules_for_path,
    violations,
)
from threefold.domain.models import ToolActionType, ToolInvocation
from threefold.domain.path_match import matches, normalise as normalise_path
from threefold.domain.shell_writes import (
    ShellAnalysis,
    ShellWrite,
    analyse as analyse_shell,
    display as shell_display,
    is_governance_path,
    pattern_is_governance,
)

logger = logging.getLogger(__name__)

# What a verdict carries. Past this the files are described rather than
# included: a hook's response and a ledger-adjacent payload are not the place
# for a whole module, and the dashboard can ask for the call again.
MAX_WRITE_BYTES = 8 * 1024
MAX_SUMMARY_CHARS = 200
MAX_STEP_CHARS = 600
MAX_STEPS = 16
# The closing steps (rotate the credential, the gates were run) come last and
# matter most, so a list too long for MAX_STEPS loses its middle, not its end.
KEPT_TAIL_STEPS = 3
# The default for propose_fix's max_content_chars: writes that come to more
# than this, taken together, are not rewritten, only described. A rewrite costs
# five to seven times the gate's own read of the file, and at 64 KB that was
# 350 to 470 ms on the development machine [PRIMARY], before a 256 MB Lambda's
# fraction of a core multiplies it, against a hook that gives up after 4 s and
# then lets the call through. Three times the 8 KB a verdict carries is past
# anything the answer can include; a larger file gets the same advice in words
# from the one read of its imports the fix makes anyway.
MAX_CONTENT_CHARS = 24_000
MAX_METHODS = 8
MAX_COMMAND_IN_STEP = 400

KIND_LAYERING = "layering"
KIND_CREDENTIAL = "credential"
KIND_PROTECTED_PATH = "protected_path"
KIND_UNREADABLE = "unreadable_write"
KIND_LOOP = "loop"
KIND_BUDGET = "budget"
KIND_HALTED = "halted_session"
KIND_DESTRUCTIVE = "destructive_command"

GATE_LAYERING = "layering"
GATE_CREDENTIAL = "credential"
GATE_BOUNDARY = "boundary"
# Not a Threefold gate: a check that a Python file this module wrote whole
# parses. The gates read imports and credentials, so a docstring broken by a
# path with `"""` or `\N` in it passed all three and was still no fix.
GATE_SYNTAX = "syntax"
# Not a pass of the content, which Threefold could not see: a record that a
# Write to this path is judged by reading it, so the route proposed will not be
# refused as unreadable. It never makes a fix validated on its own.
GATE_ROUTE = "route"

# How the evaluator begins a refusal that comes from a halted session rather
# than from a gate. Copied rather than imported: the evaluator will import this
# module to attach fixes, and importing it back would be a cycle.
HALTED_PREFIXES = ("Session execution frozen", "Session already tripped")

# The rule keys the application contract fixes, mapped to a family. A layering
# rule's own id is any other key.
_FAMILY_BY_RULE_KEY = {
    "LOOP": "loop",
    "BUDGET": "budget",
    "HALTED_SESSION": "halted",
    "SESSION_ALREADY_HALTED": "halted",
}


# --- the shapes passed around inside this module ------------------------------


@dataclass
class _Fix:
    kind: str
    summary: str
    steps: List[str] = field(default_factory=list)
    writes: List[Dict[str, Any]] = field(default_factory=list)
    validated: bool = False
    checks: List[Dict[str, Any]] = field(default_factory=list)
    # The text of every secret the call carried, found independently of the
    # scanner's view of the answer. _finish keeps each out of everything it
    # returns: the scanner knows a private key only by its header, so a body
    # left behind by a rewrite passed its check and reached the answer.
    withheld: List[str] = field(default_factory=list)


@dataclass
class _Diagnosis:
    """What refused the call, found by asking the guard's questions again."""

    kind: str
    why: str = ""
    path: str = ""
    label: str = ""
    detail: str = ""
    analysis: Optional[ShellAnalysis] = None


@dataclass
class _Site:
    """One place a call writes: a path and what the call meant to put there."""

    path: str
    content: Optional[str]
    shape: str = "write"  # write | edit | partial
    old_string: Optional[str] = None
    literal_heredoc: bool = False
    # A whole Python file that parsed as the call sent it, so its proposal must too.
    must_parse: bool = False


@dataclass
class _Removed:
    """One import statement taken out of a domain file, kept to be put into the adapter."""

    module: str
    text: str
    bindings: List[str]
    level: int = 0
    names: List[str] = field(default_factory=list)
    literal: str = ""
    rule_id: str = ""


@dataclass
class _Outcome:
    writes: List[Dict[str, Any]] = field(default_factory=list)
    checks: List[Dict[str, Any]] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    ok: bool = True
    why: str = ""
    plans: List[Dict[str, str]] = field(default_factory=list)
    # Set by _too_large: this part is advice in words, because the call was too
    # large to rewrite.
    advice: bool = False


class _CannotRemove(Exception):
    """An import that sits where taking it out would break the statement around it."""


# --- the entry point -------------------------------------------------------------


def propose_fix(
    request: Any,
    result: Any,
    rules: Optional[List[Dict[str, Any]]] = None,
    *,
    phrase: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
    max_write_bytes: int = MAX_WRITE_BYTES,
    max_content_chars: int = MAX_CONTENT_CHARS,
    skip_keys: Collection[str] = (),
) -> Optional[Dict[str, Any]]:
    """The fix for a refused call, or None when there is nothing to fix.

    `request` and `result` are the evaluator's request and verdict, as objects
    or as dicts. `rules` are the layering rules the call was judged by; the
    shipped set when None. An approval gets None, unless a dry run or a rule in
    observe mode says it would have been refused, in which case it gets the fix
    it would have needed.

    `phrase` may reword the summary (a Bedrock client, say). It is handed the
    finished fix and its answer replaces only the summary, cleaned the same way;
    if it fails, the deterministic summary stands.

    `max_content_chars` is the most text a layering fix rewrites: the content
    of every write in the call, taken together, including writes no rule flags,
    because the proposal reads those too. Past it the fix is advice in words
    from the one read of the imports the diagnosis already made: validated
    false, no writes, and steps that name the imports to move (the first four,
    then how many more), the rules that forbid them, and the first candidate
    layer where no rule forbids them, with one closing step giving the call's
    size against the cap. The evaluator passes a small value here so that a
    large refused write still gets that much within the gate's budget, where a
    rewrite would not fit. The default, MAX_CONTENT_CHARS, is what every caller
    got before the cap could be set; because the writes are now counted
    together, a call whose files each stay under it but together pass it gets
    the advice where it used to get a rewrite.

    `skip_keys` are the rule keys the calling project is still only watching.
    The gates stepped over those findings to reach the one that refused, and
    the fix has to step over the same ones, or it answers for a rule nobody is
    enforcing and names a file the call was not refused for.

    Never raises: a fault here must not turn a refusal into a 500.
    """
    try:
        active = normalise_rules(rules) if rules else list(DEFAULT_RULES if rules is None else [])
        fix = _propose(request, result, active, max(0, int(max_content_chars)), skip_keys)
        if fix is None:
            return None
        fix.withheld = list(dict.fromkeys(fix.withheld + _withheld(_field(request, "arguments"))))
        return _finish(fix, max_write_bytes, phrase)
    except Exception as exc:  # pragma: no cover - the refusal stands without a fix
        logger.warning("Could not propose a fix; the refusal stands on its own: %s", exc)
        return None
    finally:
        # The memo holds the agent's source for the length of one proposal and
        # no longer: a warm container must not keep one caller's files around.
        _imports.cache_clear()
        _parse_python.cache_clear()
        _FLAGGED.clear()


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _invocation(request: Any) -> ToolInvocation:
    """The call as the gates see it, built the way the evaluator builds it."""
    arguments = _field(request, "arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {}
    raw_action = _field(request, "action_type")
    try:
        action = ToolActionType(getattr(raw_action, "value", raw_action))
    except ValueError:
        action = ToolActionType.UNKNOWN
    return ToolInvocation(tool_name=str(_field(request, "tool_name") or ""), action_type=action, arguments=arguments)


def _effective_reason(result: Any) -> str:
    """The reason a refusal gave, or for an approval the reason it would have been refused."""
    status = str(_field(result, "status") or "").upper()
    if status and status != "APPROVED":
        return str(_field(result, "reason") or "")
    observations = _field(result, "observations") or []
    if isinstance(observations, list) and observations and isinstance(observations[0], str):
        return observations[0]
    return str(_field(result, "reason") or "")


def _family(result: Any) -> Optional[str]:
    """Which gate spoke: loop, budget, halted, boundary, observed, or None for a plain approval."""
    status = str(_field(result, "status") or "").upper()
    evaluations = _field(result, "rule_evaluations")
    evaluations = evaluations if isinstance(evaluations, dict) else {}
    rule_key = str(_field(result, "rule_key") or "").upper()
    reason = _effective_reason(result)
    if rule_key in _FAMILY_BY_RULE_KEY:
        return _FAMILY_BY_RULE_KEY[rule_key]
    if rule_key and rule_key != "NONE":
        return "boundary"
    if evaluations.get("BUDGET_CIRCUIT_BREAKER_SAFE") is False or status == "BLOCKED_CIRCUIT_BREAKER":
        return "halted" if reason.startswith(HALTED_PREFIXES) else "budget"
    if evaluations.get("LOOP_THRASHING_FREE") is False or status == "BLOCKED_LOOP_DETECTED":
        return "loop"
    if (
        evaluations.get("SECRET_LEAKAGE_FREE") is False
        or evaluations.get("ARCHITECTURAL_BOUNDARY_SAFE") is False
        or (status and status != "APPROVED")
    ):
        return "boundary"
    if _field(result, "observed_rules") or _field(result, "observations"):
        return "observed"
    return None


def _propose(
    request: Any,
    result: Any,
    rules: List[Dict[str, Any]],
    max_content_chars: int = MAX_CONTENT_CHARS,
    skip_keys: Collection[str] = (),
) -> Optional[_Fix]:
    family = _family(result)
    if family is None:
        return None
    if family == "halted":
        return _halted_fix(request, result)
    if family == "budget":
        return _budget_fix(request, result)
    if family == "loop":
        return _loop_fix(request, result)
    invocation = _invocation(request)
    diagnosis = _diagnose(invocation, rules, skip_keys) or _diagnose_observed(invocation, rules)
    if diagnosis is None:
        # The gates, asked again with the rules in force, find nothing: the
        # rules changed since, or the refusal came from somewhere this module
        # does not know. Guessing would be the noise this module exists to avoid.
        return None
    if skip_keys:
        # A rule the project is still only watching decided nothing here, so it
        # decides nothing about the answer either. Rewriting a file only that
        # rule flags would hand the agent work it was never stopped for, under
        # a summary line — the one the hook prints — naming a rule that did not
        # refuse the call.
        rules = [rule for rule in rules if rule.get("id") not in skip_keys]
    if diagnosis.kind == KIND_CREDENTIAL:
        return _credential_fix(invocation, rules, diagnosis, max_content_chars)
    if diagnosis.kind == KIND_PROTECTED_PATH:
        return _protected_fix(diagnosis)
    if diagnosis.kind == KIND_DESTRUCTIVE:
        return _destructive_fix(diagnosis)
    return _write_fix(invocation, rules, diagnosis, max_content_chars)


# --- asking the guard's questions again ---------------------------------------------


def _enforcing(rules: List[Dict[str, Any]]) -> bool:
    return any(rule.get("mode", ENFORCE) == ENFORCE for rule in rules)


# What each of the guard's gates means to a reader who has to be told what to
# do instead. The guard says which gate found a thing wrong; this says how the
# fix for it is written.
_DIAGNOSIS_FOR_FINDING = {
    CREDENTIAL_FOUND: (KIND_CREDENTIAL, ""),
    PROTECTED_PATH_FOUND: (KIND_PROTECTED_PATH, "protected"),
    GOVERNANCE_FOUND: (KIND_PROTECTED_PATH, "hooks"),
    TAMPERING_FOUND: (KIND_PROTECTED_PATH, "tampering"),
    COMMAND_PATH_FOUND: (KIND_PROTECTED_PATH, "command"),
    DESTRUCTIVE_FOUND: (KIND_DESTRUCTIVE, ""),
    LAYERING_FOUND: (KIND_LAYERING, ""),
    UNREADABLE_FOUND: (KIND_UNREADABLE, ""),
}


def _diagnose(
    invocation: ToolInvocation, rules: List[Dict[str, Any]], skip_keys: Collection[str] = ()
) -> Optional[_Diagnosis]:
    """The first check in evaluate_tool_boundary that refuses this call, and what it saw.

    The order is the guard's own, so the kind always names what actually
    refused. The questions are asked again here rather than taken from the
    guard's own walk because they are asked through this module's memo of the
    call's imports, which the fix then reuses: walking the guard again would
    parse the agent's file a second time inside the verdict's own budget.

    `skip_keys` are the rule keys this project is still only watching. The
    gates stepped over those findings to reach the one that refused, so this
    steps over the same ones: without that, a MultiEdit refused for its second
    file was diagnosed from its first, and the summary the hook prints named a
    rule that had refused nothing.
    """
    arguments = invocation.arguments if isinstance(invocation.arguments, dict) else {}
    watched = frozenset(skip_keys)

    clean, message = SecretScanner.scan_arguments(arguments)
    if not clean:
        # The guard asks nothing else once it finds one, and a credential is
        # never a key a project stages, so this is the end either way.
        if CREDENTIAL_KEY in watched:
            return None
        return _Diagnosis(KIND_CREDENTIAL, label=message.rsplit(": ", 1)[-1])

    unstaged = PROTECTED_PATH_KEY not in watched
    # The guard's own order, and the guard's own reading of the call: which
    # argument is the command, and which of its words it never opens, are
    # resolved once here as they are there, so the two cannot disagree about
    # what the call named.
    command_key, command = shell_command_at(invocation)
    analysis = analysed(command, command_cwd(invocation)) if command is not None else None
    spelled, skipped_words = not_file_words(command, analysis)

    for candidate in target_paths(arguments, command_key, skipped_words):
        if ArchitecturalBoundaryGuard.is_forbidden_file_access(candidate) and unstaged:
            return _Diagnosis(KIND_PROTECTED_PATH, why="protected", path=candidate)

    for target in governed_write_targets(invocation):
        if is_governance_path(target) and unstaged:
            return _Diagnosis(KIND_PROTECTED_PATH, why="hooks", path=target)

    if analysis is not None:
        found = _shell_diagnosis(analysis, rules, watched)
        if found is not None:
            return found

    # The layering gate's question (layering_rules.evaluate: does an enforcing
    # rule flag one of the file's imports), asked through the memoised import
    # read so the fix that follows does not parse the same file again.
    enforcing = [
        rule
        for rule in rules
        if rule.get("mode", ENFORCE) == ENFORCE and rule.get("id") not in watched
    ]
    for target, content in write_pairs(arguments):
        if _flagged_modules(target, content, enforcing):
            return _Diagnosis(KIND_LAYERING, path=target)

    path_like = [leaf for leaf in iter_string_leaves(arguments) if looks_like_path(leaf)]
    if invocation.action_type == ToolActionType.COMMAND_EXEC or command is not None or not path_like:
        for leaf in iter_string_leaves(arguments):
            for pattern in ArchitecturalBoundaryGuard.DESTRUCTIVE_COMMANDS:
                found_text = pattern.search(leaf)
                if found_text and unstaged:
                    return _Diagnosis(KIND_DESTRUCTIVE, detail=found_text.group(0))
        for leaf in iter_string_leaves(arguments):
            if looks_like_path(leaf) or leaf in skipped_words:
                continue
            for pattern in ArchitecturalBoundaryGuard.PROTECTED_PATH_PATTERNS:
                found_text = pattern.search(spelled.get(leaf, leaf))
                if found_text and unstaged:
                    return _Diagnosis(KIND_PROTECTED_PATH, why="command", detail=found_text.group(0))
    return None


def _shell_diagnosis(
    analysis: ShellAnalysis, rules: List[Dict[str, Any]], watched: Collection[str] = ()
) -> Optional[_Diagnosis]:
    """Which of a command's writes refused the call, in the guard's own order."""
    for finding in shell_findings(analysis, rules):
        if finding_key(finding) in watched:
            continue
        kind, why = _DIAGNOSIS_FOR_FINDING.get(finding.kind, (KIND_PROTECTED_PATH, ""))
        if finding.kind == UNREADABLE_FOUND:
            return _Diagnosis(kind, why=finding.detail, path=finding.path, analysis=analysis)
        return _Diagnosis(
            kind, why=why, path=finding.path, detail=finding.detail, analysis=analysis
        )
    return None


def _diagnose_observed(invocation: ToolInvocation, rules: List[Dict[str, Any]]) -> Optional[_Diagnosis]:
    """What a rule in observe mode would have refused in a call the gates let through."""
    watched = observe_layering(invocation, rules)
    if not watched:
        return None
    kind = KIND_LAYERING if any(item.get("module") for item in watched) else KIND_UNREADABLE
    return _Diagnosis(kind, path=str(watched[0].get("path") or ""))


# --- where a call writes -------------------------------------------------------------


def _tool_sites(arguments: Any) -> List[_Site]:
    """Each path in a Write, Edit or MultiEdit with the content meant for it.

    The same walk as iter_write_targets, keeping the `old_string` beside each
    edit so a retry can be an Edit. If the two walks ever disagree, the gate's
    pairing wins and the sites are proposed as plain writes.
    """
    found: List[_Site] = []

    def walk(node: Any, inherited: str) -> None:
        if isinstance(node, dict):
            path_value = ""
            for key, value in node.items():
                if isinstance(value, str) and isinstance(key, str) and key.lower() in PATH_KEYS and named_path(value):
                    path_value = value
            owner = path_value or inherited
            if owner:
                removed = next(
                    (
                        value
                        for key, value in node.items()
                        if isinstance(key, str) and key.lower() in REMOVED_KEYS and isinstance(value, str)
                    ),
                    None,
                )
                for key, value in node.items():
                    if isinstance(key, str) and isinstance(value, str) and key.lower() in CONTENT_KEYS:
                        found.append(_Site(owner, value, "edit" if removed is not None else "write", removed))
            for value in node.values():
                walk(value, owner if isinstance(value, (list, tuple)) else "")
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item, inherited)

    walk(arguments, "")
    if found and [(site.path, site.content) for site in found] == iter_write_targets(arguments):
        return found
    return [_Site(path, content) for path, content in write_pairs(arguments)]


def _adds_to_file(write: ShellWrite, command: Any) -> bool:
    """Whether a shell write adds to a file rather than replacing it.

    The reader names the route but not always the operator: a heredoc into
    `>>` and one into `>` share the route "heredoc". Proposing a whole-file Write
    of what was only appended would erase the rest of the file, so anything that
    might be an addition is treated as one.
    """
    route = write.route or ""
    if write.fragment or ">>" in route:
        return True
    if route.startswith(("sed", "perl", "awk", "patch", "git apply", "apply_patch", "add-content")):
        return True
    text = command if isinstance(command, str) else " ".join(command or [])
    name = posixpath.basename((write.target or "").replace("\\", "/"))
    if route == "heredoc" and name and re.search(r">>\s*['\"]?[^\s'\"]*" + re.escape(name), text):
        return True
    if route.startswith("tee") and re.search(r"\btee\b[^|;&\n]*\s(?:-a|--append)\b", text):
        return True
    if route.startswith(("python", "node")) and re.search(r"""['"]a\+?['"]|['"]ab['"]|appendFile""", text):
        return True
    return False


_HEREDOC_OPENER = re.compile(r"(?<![<\w])<<(-?)[ \t]*([A-Za-z_][A-Za-z0-9_]*)")


def _literal_heredocs(command: Any, cwd: str) -> Dict[str, str]:
    """What each unquoted heredoc says as written, by target.

    `cat > src/domain/price.ts <<EOF` with a template literal in it is read by
    the shell with `${name}` expanded, so the reader cannot know the text and the
    gate refuses the write as unreadable. Quoting the delimiter makes the text
    literal, which is almost always what an agent meant, and the reader then
    knows it exactly. The fix proposes that text as a Write and says why.
    """
    if not isinstance(command, str):
        return {}
    quoted = _HEREDOC_OPENER.sub(lambda match: f"<<{match.group(1)}'{match.group(2)}'", command)
    if quoted == command:
        return {}
    analysis = analyse_shell(quoted, cwd)
    return {
        write.target: write.content
        for write in analysis.writes
        if write.route == "heredoc" and write.target and write.content is not None and not write.pattern
    }


# --- checking a proposal with the gates themselves ---------------------------------


def _entry(site: _Site, content: str, path: Optional[str] = None) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"path": path or site.path, "content": content}
    if site.shape == "edit" and site.old_string is not None:
        entry["old_string"] = site.old_string
    elif site.shape == "partial":
        entry["partial"] = True
    return entry


def _parses(path: str, content: str) -> bool:
    """Whether Python content parses, for a file this module wrote whole."""
    try:
        compile(content, path or "<fix>", "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    return True


def _check_entry(
    entry: Dict[str, Any], rules: List[Dict[str, Any]], must_parse: bool = False
) -> Tuple[List[Dict[str, Any]], str]:
    """Runs one proposed write through the gates. Returns the checks and why one failed.

    Three questions: does any layering rule, enforcing or watching, flag it; does
    the credential scanner find anything in it; and does the whole boundary
    guard, the check the evaluator runs first, let the call through. The first
    is stricter than the gate on purpose: a fix a watching rule would flag is a
    fix the dashboard would list as a would-refuse the day after. With
    `must_parse`, a Python file is also parsed: the gates would pass a file that
    no interpreter could load.
    """
    checks, why = _gate_entry(entry, rules)
    path = entry["path"]
    if must_parse and language_for(path) == "python":
        parsed = _parses(path, entry["content"])
        checks.append({"gate": GATE_SYNTAX, "path": path, "passed": parsed})
        if not parsed and not why:
            why = f"the proposed {path} would not parse as Python"
    return checks, why


def _gate_entry(entry: Dict[str, Any], rules: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    """The three gate questions of _check_entry."""
    path = entry["path"]
    content = entry["content"]
    found, _ = violations(path, content, rules)
    leaves = [content] + ([entry["old_string"]] if entry.get("old_string") is not None else [])
    credential_clean = all(SecretScanner.scan_payload(leaf)[0] for leaf in leaves)
    if entry.get("old_string") is not None:
        tool, arguments = "Edit", {"file_path": path, "old_string": entry["old_string"], "new_string": content}
    else:
        tool, arguments = "Write", {"file_path": path, "content": content}
    allowed, reason = ArchitecturalBoundaryGuard.evaluate_tool_boundary(
        ToolInvocation(tool_name=tool, action_type=ToolActionType.FILE_WRITE, arguments=arguments), rules=rules
    )
    checks = [
        {"gate": GATE_LAYERING, "path": path, "passed": not found},
        {"gate": GATE_CREDENTIAL, "path": path, "passed": credential_clean},
        {"gate": GATE_BOUNDARY, "path": path, "passed": allowed},
    ]
    why = ""
    if found:
        why = found[0]["reason"]
    elif not credential_clean:
        why = f"{path} would still carry a credential"
    elif not allowed:
        why = reason
    return checks, why


# --- layering: take the import out, declare a port, put the import in an adapter ----


def _mask(content: str, language: str) -> str:
    """The content with its comments blanked, every other character where it was.

    The import readers delete comments before matching; blanking them instead
    finds the same statements at offsets that still point into the original.
    """
    blank = lambda match: re.sub(r"[^\n]", " ", match.group(0))  # noqa: E731
    if language == "python":
        return import_readers._HASH_COMMENT.sub(blank, content)
    kept: List[str] = []
    position = 0
    while True:
        start = content.find("/*", position)
        if start == -1:
            kept.append(content[position:])
            break
        kept.append(content[position:start])
        end = content.find("*/", start + 2)
        stop = len(content) if end == -1 else end + 2
        kept.append(re.sub(r"[^\n]", " ", content[start:stop]))
        position = stop
        if end == -1:
            break
    return import_readers._LINE_COMMENT.sub(blank, "".join(kept))


@functools.lru_cache(maxsize=4)
def _parse_python(content: str) -> Optional[ast.Module]:
    """The tree, or None when the content does not parse or is past the reader's parse limit.

    Remembered for the length of one proposal, as _imports is: the removal and
    the question whether the file parsed before ask about the same content.
    Callers only read the tree.
    """
    if len(content) > import_readers.PYTHON_PARSE_LIMIT:
        return None
    try:
        return ast.parse(content)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def _apply_edits(content: str, edits: List[List[Any]]) -> str:
    """Applies [start, end, replacement] edits, last first, tidying the lines they empty."""
    for start, end, replacement in sorted(((e[0], e[1], e[2]) for e in edits), key=lambda e: e[0], reverse=True):
        if replacement is not None:
            content = content[:start] + replacement + content[end:]
            continue
        tail = re.match(r"[ \t]*;[ \t]*", content[end:])
        if tail:
            end += tail.end()
        content = content[:start] + content[end:]
        line_start = content.rfind("\n", 0, start) + 1
        line_end = content.find("\n", start)
        if line_end == -1:
            line_end = len(content)
        if not content[line_start:line_end].strip():
            content = content[:line_start] + content[line_end + 1:]
    return content


def _alias_text(alias: ast.alias) -> str:
    return alias.name + (f" as {alias.asname}" if alias.asname else "")


def _char_offset(line: str, byte_col: int) -> int:
    """ast reports columns in UTF-8 bytes; slicing needs characters."""
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))


def _python_removal(content: str, modules: Collection[str]) -> Tuple[str, List[_Removed]]:
    tree = _parse_python(content)
    if tree is None:
        return _python_line_removal(content, modules)
    lines = content.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def offset(lineno: int, col: int) -> int:
        line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        return starts[min(lineno - 1, len(lines))] + _char_offset(line, col)

    # One walk: the tree is the cost here, and a second walk for the parents
    # doubled it. Only an import's parent block is ever needed.
    parents: Dict[int, list] = {}
    edits: List[List[Any]] = []
    removed: List[_Removed] = []
    for node in ast.walk(tree):
        for name in ("body", "orelse", "finalbody"):
            block = getattr(node, name, None)
            if isinstance(block, list):
                for child in block:
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        parents[id(child)] = block
        if isinstance(node, ast.Import):
            hits = [alias for alias in node.names if alias.name in modules]
            if not hits:
                continue
            keep = [alias for alias in node.names if alias.name not in modules]
            replacement = ("import " + ", ".join(_alias_text(alias) for alias in keep)) if keep else None
            for alias in hits:
                removed.append(_Removed(alias.name, "import " + _alias_text(alias), [alias.asname or alias.name]))
        elif isinstance(node, ast.ImportFrom) and node.module in modules:
            replacement = None
            module = node.module
            names = [_alias_text(alias) for alias in node.names]
            removed.append(
                _Removed(
                    module,
                    f"from {'.' * node.level}{module} import {', '.join(names)}",
                    [alias.asname or alias.name for alias in node.names if alias.name != "*"],
                    level=node.level,
                    names=names,
                )
            )
        else:
            continue
        edits.append([offset(node.lineno, node.col_offset), offset(node.end_lineno, node.end_col_offset), replacement, node])

    # A block whose every statement is being removed keeps a `pass`, or
    # `if TYPE_CHECKING:` with its only import gone would no longer parse.
    blocks: Dict[int, list] = {}
    for edit in edits:
        block = parents.get(id(edit[3]))
        if block is not None and block is not tree.body:
            blocks[id(block)] = block
    for block in blocks.values():
        emptied = [edit for edit in edits if parents.get(id(edit[3])) is block and edit[2] is None]
        if len(emptied) == len(block):
            emptied[-1][2] = "pass"
    return _apply_edits(content, edits), removed


def _python_line_removal(content: str, modules: Collection[str]) -> Tuple[str, List[_Removed]]:
    """The line reader's statements, for content that does not parse (it arrives mid-edit)."""
    masked = _mask(content, "python")
    edits: List[List[Any]] = []
    removed: List[_Removed] = []
    for match in import_readers._PYTHON_FALLBACK.finditer(masked):
        keyword_start = match.start() + len(match.group(0)) - len(match.group(0).lstrip(" \t"))
        line_end = masked.find("\n", match.start())
        line_end = len(masked) if line_end == -1 else line_end
        if match.group("from") is not None:
            module = match.group("from").lstrip(".")
            if module not in modules:
                continue
            semicolon = masked.find(";", match.end(), line_end)
            end = semicolon if semicolon != -1 else line_end
            text = content[keyword_start:end].strip()
            level = len(match.group("from")) - len(match.group("from").lstrip("."))
            clause = text.split(" import ", 1)[1] if " import " in text else ""
            names = [name.strip() for name in clause.strip("() \t").split(",") if name.strip()]
            bindings = [name.split(" as ")[-1].strip() for name in names if name.split(" as ")[0].strip() != "*"]
            removed.append(_Removed(module, text, bindings, level=level, names=names))
            edits.append([keyword_start, end, None])
            continue
        parts = [part.strip() for part in match.group("import").split(",") if part.strip()]
        hits = [part for part in parts if part.split(" ")[0].strip("()").lstrip(".") in modules]
        if not hits:
            continue
        keep = [part for part in parts if part not in hits]
        replacement = ("import " + ", ".join(keep)) if keep else None
        for part in hits:
            binding = part.split(" as ")[-1].strip() if " as " in part else part.split(" ")[0]
            removed.append(_Removed(part.split(" ")[0].strip("()").lstrip("."), "import " + part, [binding]))
        edits.append([keyword_start, match.end(), replacement])
    return _apply_edits(content, edits), removed


def _pattern_removal(content: str, modules: Collection[str], language: str) -> Tuple[str, List[_Removed]]:
    """Java and C# statements, found by the reader's own patterns."""
    pattern = import_readers._JAVA if language == "java" else import_readers._CSHARP
    masked = _mask(content, language)
    edits: List[List[Any]] = []
    removed: List[_Removed] = []
    for match in pattern.finditer(masked):
        module = match.group("module")
        if module not in modules:
            continue
        start = match.start() + len(match.group(0)) - len(match.group(0).lstrip(" \t"))
        text = content[start:match.end()].strip()
        if language == "java":
            bindings = [] if "*" in text else [module.rsplit(".", 1)[-1]]
        else:
            alias = re.match(r"(?:global\s+)?using\s+(?:static\s+)?(\w+)\s*=", text)
            bindings = [alias.group(1)] if alias else []
        removed.append(_Removed(module, text, bindings))
        edits.append([start, match.end(), None])
    return _apply_edits(content, edits), removed


_TS_KEYWORD = re.compile(r"\b(?:import|export)\b")
_TS_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
_TS_DECLARATION = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?(?:const|let|var)\b")


def _binding_names(text: str) -> List[str]:
    """The names `const x = require(...)` or `const {a, b: c} = ...` binds."""
    text = (text or "").strip()
    if text[:1] in "{[":
        names = []
        for part in text.strip("{}[] \n\t").split(","):
            part = part.strip()
            if ":" in part:
                part = part.split(":")[-1].strip()
            part = part.split("=")[0].strip().lstrip(".")
            if _TS_IDENTIFIER.fullmatch(part):
                names.append(part)
        return names
    return [text] if _TS_IDENTIFIER.fullmatch(text) else []


def _ts_import_bindings(text: str) -> List[str]:
    match = re.match(r"import\s+(?:type\s+)?(?P<clause>[\s\S]*?)\s*from\s*['\"]", text)
    if not match:
        return []
    clause = match.group("clause").strip()
    names: List[str] = []
    default = re.match(r"([A-Za-z_$][\w$]*)\s*(?:,|$)", clause)
    if default:
        names.append(default.group(1))
    namespace = re.search(r"\*\s*as\s+([A-Za-z_$][\w$]*)", clause)
    if namespace:
        names.append(namespace.group(1))
    named = re.search(r"\{([^}]*)\}", clause)
    if named:
        for part in named.group(1).split(","):
            part = re.sub(r"^\s*type\s+", "", part).strip()
            name = part.split(" as ")[-1].strip()
            if _TS_IDENTIFIER.fullmatch(name):
                names.append(name)
    return names


def _ts_call_statement(masked: str, match: "re.Match[str]") -> Tuple[int, int, List[str]]:
    """The whole statement around a `require('x')`, or _CannotRemove if it is inside an expression."""
    line_start = masked.rfind("\n", 0, match.start()) + 1
    rest = re.match(r"[ \t]*;?[ \t]*(?=\n|$)", masked[match.end():])
    if rest is None:
        raise _CannotRemove("the call is part of a longer expression")
    end = match.end() + rest.end()
    single = re.fullmatch(
        r"[ \t]*(?:export[ \t]+)?(?:(?:const|let|var)[ \t]+(?P<binding>[^=;\n]+?)[ \t]*=[ \t]*)?(?:await[ \t]+)?",
        masked[line_start:match.start()],
    )
    if single:
        return line_start, end, _binding_names(single.group("binding") or "")
    declaration = None
    for candidate in _TS_DECLARATION.finditer(masked, 0, match.start()):
        declaration = candidate
    if declaration is not None and ";" not in masked[declaration.start():match.start()]:
        head = re.fullmatch(
            r"[ \t]*(?:export[ \t]+)?(?:const|let|var)\s+(?P<binding>[\s\S]+?)\s*=\s*(?:await\s+)?",
            masked[declaration.start():match.start()],
        )
        if head:
            return declaration.start(), end, _binding_names(head.group("binding"))
    raise _CannotRemove("the call is part of a longer expression")


def _ts_removal(content: str, modules: Collection[str]) -> Tuple[str, List[_Removed], Set[str]]:
    """Every statement importing one of `modules`, and the modules one of them could not be taken from."""
    masked = _mask(content, "typescript")
    edits: List[List[Any]] = []
    removed: List[_Removed] = []
    stuck: Set[str] = set()
    taken = set()
    for pattern in (import_readers._TS_FROM, import_readers._TS_BARE):
        for match in pattern.finditer(masked):
            module = match.group("module")
            if module not in modules:
                continue
            keyword_match = _TS_KEYWORD.search(masked, match.start(), match.end())
            if keyword_match is None or keyword_match.start() in taken:
                continue
            start = keyword_match.start()
            end = match.end()
            tail = re.match(r"[ \t]*;", masked[end:])
            if tail:
                end += tail.end()
            taken.add(start)
            text = content[start:end]
            bindings = _ts_import_bindings(text) if keyword_match.group(0) == "import" else []
            removed.append(_Removed(module, text.strip(), bindings, literal=module))
            edits.append([start, end, None])
    for match in import_readers._TS_CALL.finditer(masked):
        module = match.group("module")
        if module not in modules:
            continue
        try:
            start, end, bindings = _ts_call_statement(masked, match)
        except _CannotRemove:
            stuck.add(module)
            continue
        if start in taken:
            continue
        taken.add(start)
        removed.append(_Removed(module, content[start:end].strip(), bindings, literal=module))
        edits.append([start, end, None])
    return _apply_edits(content, edits), removed, stuck


@functools.lru_cache(maxsize=16)
def _imports(path: str, content: str) -> Tuple[str, ...]:
    """declared_imports, remembered for the length of one proposal.

    The same content is asked about by the site filter, the removal and its
    check; each ask used to be a full parse. propose_fix clears it on the way out.
    """
    return tuple(declared_imports(path, content)[1])


# What the rules said about one write's imports, for the length of one
# proposal. The diagnosis, the site filter, the path's own filter and the
# advice each ask it of the same content, and each ask used to match every
# import against every pattern again: four passes over a file of 500 short
# imports took 30 ms, three times a whole verdict [PRIMARY], 2026-09-22.
# Keyed by the rules that apply, as values rather than by identity, because
# the diagnosis asks with a list of the enforcing rules that is gone by the
# time the next question is asked. propose_fix clears it on the way out, as it
# does _imports, because it holds the agent's source; the bound is for a
# caller outside a proposal.
_FLAGGED: Dict[Tuple[str, str, Tuple[Any, ...]], Dict[str, str]] = {}
_FLAGGED_MAX_ENTRIES = 32


def _applicable_key(applicable: List[Dict[str, Any]]) -> Tuple[Any, ...]:
    """What _flagging_rule reads of these rules, as a value a memo can be keyed by."""
    return tuple(
        (rule.get("id"), tuple(rule.get("forbid_imports") or ()), tuple(rule.get("allow_imports") or ()))
        for rule in applicable
    )


def _flagged_modules(path: str, content: str, rules: List[Dict[str, Any]]) -> Dict[str, str]:
    """Every import any rule flags, enforcing or watching, with the first rule that flags it.

    violations() stops at one import per rule, which is enough to refuse a call
    and not enough to fix one: a file with sixty forbidden imports took sixty
    rounds of parse-remove-parse, tens of seconds on a large file. This is the
    same loop without the stop, using the gate's own matchers and its own rule
    for an allowance that is more specific than the prohibition. The proposal is
    still judged afterwards by violations() itself, so a drift between the two
    shows up as a fix that is not validated, never as one that is wrongly.

    Each distinct import is matched once per proposal, however often the same
    content is asked about and however often a module is imported: the answer
    is remembered in _FLAGGED.
    """
    applicable = rules_for_path(path, rules)
    if not applicable or not language_for(path):
        return {}
    key = (path, content, _applicable_key(applicable))
    known = _FLAGGED.get(key)
    if known is None:
        known = {}
        for module in dict.fromkeys(_imports(path, content)):
            rule_id = _flagging_rule(module, applicable)
            if rule_id:
                known[module] = rule_id
        if len(_FLAGGED) >= _FLAGGED_MAX_ENTRIES:
            _FLAGGED.clear()
        _FLAGGED[key] = known
    return dict(known)


@functools.lru_cache(maxsize=256)
def _screen(
    patterns: Tuple[str, ...]
) -> Tuple[Dict[str, Tuple[int, ...]], Tuple[Tuple[int, Tuple[str, ...]], ...]]:
    """What a module must contain for each of these patterns to catch it. (plain targets, globs)

    Read off the patterns the way layering_rules._forbidden_by reads them, by
    position. A pattern without a wildcard catches a module only when,
    lower-cased, it is the module or a prefix of it ending at a segment
    boundary, so the first map is from that lower-cased target to the
    positions that spell it. A glob catches a module only when every segment it
    spells out in full, with no `*` or `?`, is one of the module's segments,
    because path_match matches such a segment by equality alone, so the second
    is each glob's position with those segments. Holds the rules' patterns,
    never the agent's source, so it outlives a proposal.
    """
    plain: Dict[str, List[int]] = {}
    globs: List[Tuple[int, Tuple[str, ...]]] = []
    for index, pattern in enumerate(patterns):
        if not pattern:
            continue
        spelled = layering_internals._module_segments(pattern)
        if "*" in pattern or "?" in pattern:
            literals = tuple(
                seg for seg in normalise_path(spelled).lower().split("/") if seg and "*" not in seg and "?" not in seg
            )
            globs.append((index, literals))
        elif spelled:
            plain.setdefault(spelled.lower(), []).append(index)
    return {target: tuple(indices) for target, indices in plain.items()}, tuple(globs)


def _catchable(module: str, patterns: Optional[Sequence[str]]) -> List[str]:
    """The patterns here that might catch `module`, in their own order; no other one can.

    A screen, not a matcher: it answers from a lookup per segment of the module,
    where _forbidden_by normalises every pattern again for every module and
    walks each glob segment by segment, about 16 microseconds a module against
    the shipped Python rule [PRIMARY], 2026-09-22. It may keep a pattern that
    does not catch the module, which costs one real match, and never drops one
    that does. _forbidden_by answers with the first pattern in order that
    catches the module, so asking it about these alone gives the answer it
    gives for the whole list, and the decision stays the gate's own matcher's.
    A test holds the two answers equal.
    """
    if not patterns:
        return []
    ordered = tuple(patterns)
    plain, globs = _screen(ordered)
    candidate = layering_internals._module_segments(module)
    if not candidate:
        return []
    found: List[int] = []
    if plain:
        lowered = candidate.lower()
        found.extend(plain.get(lowered, ()))
        boundary = lowered.find("/")
        while boundary != -1:
            found.extend(plain.get(lowered[:boundary], ()))
            boundary = lowered.find("/", boundary + 1)
    if globs:
        segments = {seg for seg in normalise_path(candidate).lower().split("/") if seg}
        found.extend(index for index, literals in globs if all(literal in segments for literal in literals))
    return [ordered[index] for index in sorted(set(found))]


def _flagging_rule(module: str, applicable: List[Dict[str, Any]]) -> str:
    """The id of the first of these rules that forbids importing `module`, or an empty string.

    Each answer is layering_rules._forbidden_by's, the gate's own matcher, asked
    only about the patterns _catchable keeps, which gives the same answer as
    asking it about all of them.
    """
    for rule in applicable:
        forbid = _catchable(module, rule["forbid_imports"])
        if not forbid:
            continue
        offending = layering_internals._forbidden_by(module, forbid)
        if not offending:
            continue
        permitted = layering_internals._forbidden_by(module, _catchable(module, rule.get("allow_imports")))
        if permitted and layering_internals._specificity(permitted) > layering_internals._specificity(offending):
            continue
        return rule["id"]
    return ""


def _refused_at(path: str, modules: Sequence[str], rules: List[Dict[str, Any]]) -> List[str]:
    """Which of `modules` the rules would flag in a file at `path`, read from the rules alone.

    The question _flagged_modules asks, for imports already known rather than
    read from content, so it costs a match of each module against the rules
    covering the path and no parse at all.
    """
    applicable = rules_for_path(path, rules)
    if not applicable or not language_for(path):
        return []
    return [module for module in modules if _flagging_rule(module, applicable)]


def _remove_modules(path: str, content: str, modules: Collection[str]) -> Tuple[str, List[_Removed], Set[str]]:
    """Takes every statement importing one of `modules` out, in one pass. (content, removed, stuck)"""
    language = language_for(path)
    if language == "python":
        new, removed = _python_removal(content, modules)
        return new, removed, set()
    if language in ("java", "csharp"):
        new, removed = _pattern_removal(content, modules, language)
        return new, removed, set()
    if language == "typescript":
        return _ts_removal(content, modules)
    return content, [], set(modules)


def _remove_offending(
    path: str, content: str, rules: List[Dict[str, Any]]
) -> Tuple[str, List[_Removed], List[str], str]:
    """Removes every import any rule flags, enforcing or watching. (content, removed, rule ids, why not)

    One pass takes every flagged import out: removal never adds an import, so
    nothing new can appear for a second pass to find. A flagged module with no
    statement taken out stops the fix with the reason. One the reader still
    sees afterwards (the removal and the reader disagreeing) is caught when the
    proposal is judged by the gate itself, and the fix is then not validated.
    """
    flagged = _flagged_modules(path, content, rules)
    if not flagged:
        return content, [], [], ""
    new, taken, stuck = _remove_modules(path, content, flagged)
    missed = [module for module in flagged if module in stuck or not any(item.module == module for item in taken)]
    if missed:
        return content, [], [], (
            f"the statement in '{path}' that imports '{missed[0]}' could not be taken out "
            "without breaking the code around it"
        )
    for statement in taken:
        statement.rule_id = flagged.get(statement.module, "")
    return new, taken, list(dict.fromkeys(flagged.values())), ""


# --- names and paths for the port and the adapter -------------------------------------

_JAVA_KEYWORDS = frozenset(
    (
        "abstract assert boolean break byte case catch char class const continue default do double else enum "
        "extends final finally float for goto if implements import instanceof int interface long native new "
        "package private protected public return short static strictfp super switch synchronized this throw "
        "throws transient try void volatile while var record yield true false null"
    ).split()
)
_JAVA_PACKAGE = re.compile(r"^[ \t]*package[ \t]+([\w.]+)[ \t]*;", re.MULTILINE)
_CSHARP_NAMESPACE = re.compile(r"^[ \t]*namespace[ \t]+([\w.]+)[ \t]*(;|\{|$)", re.MULTILINE)


def _split(path: str) -> Tuple[str, str, str, str]:
    """(directory, file name, stem, extension) of a path, with forward slashes."""
    normal = (path or "").replace("\\", "/")
    directory, name = posixpath.split(normal)
    stem, extension = posixpath.splitext(name)
    return directory, name, stem, extension


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


def _pascal(stem: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", stem or "")
    name = "".join(word[:1].upper() + word[1:] for word in words) or "Module"
    return "Module" + name if name[0].isdigit() else name


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def _script_flavour(path: str) -> str:
    return "typescript" if _split(path)[3].lower() in (".ts", ".tsx") else "javascript"


def _method_name(name: str, language: str) -> Optional[str]:
    if language == "python":
        candidate = _snake(name)
        return candidate if candidate.isidentifier() and not keyword.iskeyword(candidate) else None
    if language == "csharp":
        candidate = name[:1].upper() + name[1:]
        return candidate if re.fullmatch(r"[A-Za-z_]\w*", candidate) else None
    candidate = name[:1].lower() + name[1:]
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", candidate) or candidate in ("constructor", "prototype"):
        return None
    if language == "java" and candidate in _JAVA_KEYWORDS:
        return None
    return candidate


def _members(content: str, bindings: Sequence[str], language: str) -> List[Tuple[str, Optional[str]]]:
    """The operations the domain code performs on what it imported: the port's methods.

    `boto3.client(...)` becomes `client`, a called name such as `Key(...)`
    becomes `key`. When nothing is called in a way this can read, the port gets
    one method, `perform`, and the steps say to name it properly.
    """
    masked = _mask(content, language)
    found: List[Tuple[str, Optional[str]]] = []
    seen = set()
    members: Dict[str, List[str]] = {binding: [] for binding in bindings}
    called: Set[str] = set()
    if bindings:
        # One scan for every binding: a file that moved sixty imports scanned
        # itself a hundred and twenty times here. Longest first, so `ab` is
        # tried before `a`. Not after `@`: `@Table(name = "x")` is an
        # annotation, not something the domain asks the outside world to do.
        alternation = "|".join(re.escape(binding) for binding in sorted(members, key=len, reverse=True))
        pattern = re.compile(
            r"(?<![\w$.@])(?P<binding>" + alternation + r")\s*(?:\.\s*(?P<member>[A-Za-z_$][\w$]*)|(?P<call>\())"
        )
        for match in pattern.finditer(masked):
            if match.group("member"):
                members[match.group("binding")].append(match.group("member"))
            elif masked[max(0, match.start() - 4):match.start()] != "new ":
                called.add(match.group("binding"))
    for binding in bindings:
        for member in members[binding]:
            name = _method_name(member, language)
            if name and name not in seen:
                seen.add(name)
                found.append((name, f"{binding}.{member}"))
        if binding in called:
            name = _method_name(binding, language)
            if name and name not in seen:
                seen.add(name)
                found.append((name, binding))
    if not found:
        return [("Perform" if language == "csharp" else "perform", None)]
    return found[:MAX_METHODS]


def _uses(content: str, bindings: Sequence[str], language: str) -> int:
    if not bindings:
        return 0
    masked = _mask(content, language)
    alternation = "|".join(re.escape(binding) for binding in sorted(set(bindings), key=len, reverse=True))
    return len(re.findall(r"(?<![\w$.])(?:" + alternation + r")(?![\w$])", masked))


def _cased(layer: str, like: str) -> str:
    if any(character.isupper() for character in layer):
        return layer
    return layer[:1].upper() + layer[1:] if like[:1].isupper() else layer


def _adapter_directories(path: str, rules: List[Dict[str, Any]], rule_ids: Sequence[str]) -> List[Tuple[str, str, str]]:
    """Where an adapter could live, first choice first: (directory, layer name, replaced segment).

    Derived from the rules' own patterns. The segment a rule's path pattern
    names (`domain` in `**/domain/**/*.py`) is the layer the file is in, and the
    layers the rule forbids importing by package (`**.infrastructure.**`) are
    where the outside world is meant to live. Each candidate replaces the one
    with the other; the first that the rules accept wins, and when none does
    the fix says so rather than inventing a place.
    """
    normal = path.replace("\\", "/")
    directories = normal.split("/")[:-1]
    covering = rules_for_path(path, rules)
    literals = set()
    for rule in covering:
        for glob in rule.get("when_path_matches") or ():
            if not matches(normal, glob):
                continue
            for segment in glob.replace("\\", "/").split("/")[:-1]:
                if segment and not any(character in segment for character in "*?["):
                    literals.add(segment.lower())
    index: Optional[int] = None
    for position in range(len(directories) - 1, -1, -1):
        if directories[position].lower() in literals:
            index = position
            break
    if index is None and directories:
        index = len(directories) - 1

    layers: List[str] = []
    deciding = [rule for rule in rules if rule["id"] in set(rule_ids)] or covering
    for rule in deciding:
        for pattern in rule.get("forbid_imports") or ():
            if not pattern.startswith("**"):
                continue
            words = [word for word in re.split(r"[./\\]", pattern) if word and not any(ch in word for ch in "*?[")]
            if len(words) == 1:
                layers.append(words[0])
    layers.extend(("infrastructure", "adapters"))

    seen = set()
    candidates: List[Tuple[str, str, str]] = []
    for layer in layers:
        if layer.lower() in seen:
            continue
        seen.add(layer.lower())
        if index is None:
            name = layer
            candidates.append((name, name, ""))
            continue
        name = _cased(layer, directories[index])
        if name.lower() == directories[index].lower():
            continue
        candidates.append(("/".join(directories[:index] + [name] + directories[index + 1:]), name, directories[index]))
    return candidates


def _swap_segment(dotted: str, old: str, new: str) -> str:
    parts = dotted.split(".")
    for position in range(len(parts) - 1, -1, -1):
        if old and parts[position].lower() == old.lower():
            parts[position] = new
            return ".".join(parts)
    return ""


def _package_from_directory(directory: str, language: str) -> str:
    parts = [part for part in directory.split("/") if part and part != "."]
    markers = ("java", "kotlin", "src") if language == "java" else ("src",)
    for marker in markers:
        if marker in parts:
            parts = parts[len(parts) - parts[::-1].index(marker):]
            break
    if parts and all(re.fullmatch(r"[A-Za-z_][\w.]*", part) for part in parts):
        return ".".join(parts)
    return ""


def _relative_module(from_directory: str, to_path: str) -> str:
    relative = posixpath.relpath(to_path, from_directory or ".")
    return relative if relative.startswith(".") else "./" + relative


def _rebase_python(level: int, module: str, domain_directory: str, adapter_directory: str) -> str:
    """A relative `from ..x import y` said again from the adapter's package."""
    base = domain_directory
    for _ in range(max(level - 1, 0)):
        base = posixpath.dirname(base)
    target = posixpath.join(base, *module.split(".")) if module else base
    parts = posixpath.relpath(target or ".", adapter_directory or ".").split("/")
    ups = 0
    while parts and parts[0] == "..":
        ups += 1
        parts.pop(0)
    if parts == ["."]:
        parts = []
    return "." * (ups + 1) + ".".join(parts)


def _render_statement(statement: _Removed, language: str, domain_path: str, adapter_path: str) -> str:
    """The removed statement as the adapter needs it, relative paths said from the adapter."""
    domain_directory = _split(domain_path)[0]
    adapter_directory = _split(adapter_path)[0]
    if language == "python" and statement.level:
        rebased = _rebase_python(statement.level, statement.module, domain_directory, adapter_directory)
        return f"from {rebased} import {', '.join(statement.names)}"
    if language == "typescript" and statement.literal.startswith("."):
        target = posixpath.normpath(posixpath.join(domain_directory, statement.literal))
        rebased = _relative_module(adapter_directory, target)
        for quote in ("'", '"'):
            old = f"{quote}{statement.literal}{quote}"
            if old in statement.text:
                new = f"{quote}{rebased}{quote}" if _PLAIN_MODULE.fullmatch(rebased) else _module_literal(rebased)
                return statement.text.replace(old, new, 1)
    return statement.text


def _libraries(removed: Sequence[_Removed]) -> str:
    names = list(dict.fromkeys(statement.module.rstrip(".") for statement in removed))
    if len(names) > 3:
        return ", ".join(names[:3]) + f" and {len(names) - 3} more"
    return ", ".join(names)


# --- the text of a port and an adapter, per language ------------------------------------


def _python_port(port: str, methods: Sequence[Tuple[str, Optional[str]]], libraries: str, stem: str) -> str:
    libraries, stem = _comment_text(libraries), _comment_text(stem)
    lines = [
        f"class {port}(Protocol):",
        f'    """What {stem} needs from {libraries}, declared here so the domain never depends on it.',
        "",
        "    An adapter outside the domain implements it and is passed in by the caller.",
        '    """',
        "",
    ]
    lines.extend(f"    def {name}(self, *args: Any, **kwargs: Any) -> Any: ..." for name, _ in methods)
    return "\n".join(lines) + "\n"


def _insert_python_port(content: str, block: str) -> str:
    """Puts `from typing import Any, Protocol` and the port after the file's leading imports."""
    header = "from typing import Any, Protocol\n"
    tree = _parse_python(content)
    if tree is None:
        separator = "" if not content or content.endswith("\n") else "\n"
        return f"{content}{separator}\n{header}\n\n{block}"
    lines = content.splitlines(keepends=True)
    after = 0
    body = tree.body
    docstring = bool(body) and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str)
    for position, node in enumerate(body):
        if position == 0 and docstring:
            after = node.end_lineno or after
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            after = node.end_lineno or after
            continue
        break
    head = "".join(lines[:after])
    if head and not head.endswith("\n"):
        head += "\n"
    tail = "".join(lines[after:]).lstrip("\n")
    insertion = header + "\n\n" + block
    if tail.strip():
        insertion += "\n\n"
    return head + insertion + tail


def _script_port(port: str, methods: Sequence[Tuple[str, Optional[str]]], libraries: str, stem: str, flavour: str) -> str:
    libraries, stem = _comment_text(libraries), _comment_text(stem)
    comment = (
        f"/** What {stem} needs from {libraries}, declared here so the domain never depends on it. "
        "An adapter outside the domain implements it. */"
    )
    if flavour == "typescript":
        lines = [comment, f"export interface {port} {{"]
        lines.extend(f"  {name}(...args: unknown[]): unknown;" for name, _ in methods)
    else:
        lines = [comment, f"export class {port} {{"]
        for name, _ in methods:
            message = f"{port}.{name} is implemented by an adapter outside the domain"
            lines.extend([f"  {name}(...args) {{", f"    throw new Error({_quoted(message)});", "  }"])
    lines.append("}")
    return "\n".join(lines) + "\n"


def _insert_script_port(content: str, block: str) -> str:
    masked = _mask(content, "typescript")
    last = -1
    for pattern in (import_readers._TS_FROM, import_readers._TS_BARE):
        for match in pattern.finditer(masked):
            last = max(last, match.end())
    if last == -1:
        return block + ("\n" + content if content.strip() else "")
    line_end = content.find("\n", last)
    line_end = len(content) if line_end == -1 else line_end + 1
    head = content[:line_end]
    if not head.endswith("\n"):
        head += "\n"
    tail = content[line_end:].lstrip("\n")
    return head + "\n" + block + ("\n" + tail if tail.strip() else "")


def _quoted(text: str) -> str:
    """A double-quoted string literal that Python, TypeScript, Java and C# all read the same way."""
    return '"' + re.sub(r'[\\"]', lambda match: "\\" + match.group(0), re.sub(r"[^\x20-\x7e]", "?", text)) + '"'


# What may stand as written inside a docstring or a comment in every language
# this module writes: letters, digits and the punctuation a path or a package
# name uses. Everything else becomes `_`. A path is the caller's to choose, and
# `a"""+__import__("os").getcwd()+"""b.py` closed a docstring and ran as code;
# `src\acme\domain\user.py` made `\u` an escape and the file stopped parsing;
# `*/` would close a Java or TypeScript comment the same way, and `<` or `&`
# would break a C# XML doc comment.
_COMMENT_UNSAFE = re.compile(r"[^\w./@+~,()\[\]$=: -]")
_PLAIN_MODULE = re.compile(r"[\w./@+~-]+")


def _comment_text(text: str) -> str:
    """Text that cannot end, escape or corrupt the comment or docstring it is put in."""
    return _COMMENT_UNSAFE.sub("_", (text or "").replace("\\", "/"))


def _module_literal(module: str) -> str:
    """A TypeScript or JavaScript module specifier that names exactly this path.

    Single-quoted, as the rest of the file is, when the path is plain; a JSON
    string otherwise, which every JavaScript engine reads as the same string, so
    a quote in a directory name can neither break the import nor end it early.
    """
    if _PLAIN_MODULE.fullmatch(module or ""):
        return f"'{module}'"
    return json.dumps(module)


def _java_port(package: str, port: str, methods: Sequence[Tuple[str, Optional[str]]], libraries: str, stem: str) -> str:
    libraries, stem = _comment_text(libraries), _comment_text(stem)
    lines = [f"package {package};", ""] if package else []
    lines.append(
        f"/** What {stem} needs from {libraries}, declared in the domain so {stem} never depends on it. "
        "An adapter outside the domain implements it. */"
    )
    lines.append(f"public interface {port} {{")
    lines.extend(f"    Object {name}(Object... args);" for name, _ in methods)
    lines.append("}")
    return "\n".join(lines) + "\n"


def _csharp_block(namespace: str, block_style: bool, body: List[str]) -> List[str]:
    if not namespace:
        return body
    if block_style:
        return [f"namespace {namespace}", "{"] + ["    " + line if line else "" for line in body] + ["}"]
    return [f"namespace {namespace};", ""] + body


def _csharp_port(namespace: str, block_style: bool, port: str, methods: Sequence[Tuple[str, Optional[str]]], libraries: str, stem: str) -> str:
    libraries, stem = _comment_text(libraries), _comment_text(stem)
    body = [
        f"/// <summary>What {stem} needs from {libraries}, declared in the domain so {stem} never references it. "
        "An adapter outside the domain implements it.</summary>",
        f"public interface {port}",
        "{",
    ]
    body.extend(f"    object {name}(params object[] args);" for name, _ in methods)
    body.append("}")
    return "\n".join(_csharp_block(namespace, block_style, body)) + "\n"


def _adapter_text(
    language: str,
    flavour: str,
    adapter: str,
    port: str,
    port_path: str,
    adapter_path: str,
    statements: Sequence[str],
    methods: Sequence[Tuple[str, Optional[str]]],
    libraries: str,
    package: Tuple[str, str],
    block_style: bool,
) -> str:
    """The adapter: the imports the domain gave up, and the port implemented with them."""
    domain_package, adapter_package = package
    # Raw names go only where they are code the domain file already held (the
    # moved statements) or through _quoted; prose goes through _comment_text.
    call_hint = libraries
    libraries = _comment_text(libraries)
    if language == "python":
        lines = [
            f'"""Implements {port} from {_comment_text(port_path)} with {libraries}, outside the domain."""',
            "from typing import Any",
            "",
            *statements,
            "",
            "",
            f"class {adapter}:",
            f'    """Satisfies {port} structurally: hand an instance to the domain code that needs it."""',
        ]
        for name, target in methods:
            lines.append("")
            lines.append(f"    def {name}(self, *args: Any, **kwargs: Any) -> Any:")
            if target:
                lines.append(f"        return {target}(*args, **kwargs)")
            else:
                lines.append(f"        raise NotImplementedError({_quoted('Call ' + call_hint + ' here')})")
        return "\n".join(lines) + "\n"
    if language == "typescript":
        module = _module_literal(_relative_module(_split(adapter_path)[0], _script_module_path(port_path, flavour)))
        if flavour == "typescript":
            lines = [*statements, f"import type {{ {port} }} from {module};", ""]
            lines.append(f"/** Implements {port} with {libraries}, outside the domain. */")
            lines.append(f"export class {adapter} implements {port} {{")
            for name, target in methods:
                lines.append(f"  {name}(...args: unknown[]): unknown {{")
                lines.append(f"    {_script_call(target, call_hint, typed=True)}")
                lines.append("  }")
        else:
            lines = [*statements, f"import {{ {port} }} from {module};", ""]
            lines.append(f"/** Implements {port} with {libraries}, outside the domain. */")
            lines.append(f"export class {adapter} extends {port} {{")
            for name, target in methods:
                lines.append(f"  {name}(...args) {{")
                lines.append(f"    {_script_call(target, call_hint, typed=False)}")
                lines.append("  }")
        lines.append("}")
        return "\n".join(lines) + "\n"
    if language == "java":
        lines = [f"package {adapter_package};", ""] if adapter_package else []
        lines.extend(statements)
        if domain_package and domain_package != adapter_package:
            lines.append(f"import {domain_package}.{port};")
        lines.extend(["", f"/** Implements {port} with {libraries}, outside the domain. */", f"public class {adapter} implements {port} {{"])
        for name, target in methods:
            message = f"Delegate to {target or libraries} here"
            lines.extend(
                [
                    "",
                    "    @Override",
                    f"    public Object {name}(Object... args) {{",
                    f"        throw new UnsupportedOperationException({_quoted(message)});",
                    "    }",
                ]
            )
        lines.append("}")
        return "\n".join(lines) + "\n"
    # C#
    lines = list(statements)
    if domain_package and domain_package != adapter_package:
        lines.append(f"using {domain_package};")
    lines.append("")
    body = [f"/// <summary>Implements {port} with {libraries}, outside the domain.</summary>", f"public class {adapter} : {port}", "{"]
    for position, (name, target) in enumerate(methods):
        if position:
            body.append("")
        message = f"Delegate to {target or libraries} here"
        body.extend(
            [
                f"    public object {name}(params object[] args)",
                "    {",
                f"        throw new System.NotImplementedException({_quoted(message)});",
                "    }",
            ]
        )
    body.append("}")
    lines.extend(_csharp_block(adapter_package, block_style, body))
    return "\n".join(lines) + "\n"


def _script_module_path(port_path: str, flavour: str) -> str:
    directory, _, stem, extension = _split(port_path)
    if flavour == "typescript":
        return _join(directory, stem)
    return _join(directory, stem + (extension if extension in (".js", ".mjs", ".cjs") else ".js"))


def _script_call(target: Optional[str], libraries: str, typed: bool) -> str:
    if not target:
        return f"throw new Error({_quoted('Call ' + libraries + ' here')});"
    if not typed:
        return f"return {target}(...args);"
    if "." in target:
        owner, member = target.rsplit(".", 1)
        return f"return ({owner} as unknown as Record<string, (...a: unknown[]) => unknown>).{member}(...args);"
    return f"return ({target} as unknown as (...a: unknown[]) => unknown)(...args);"


def _port_file_name(language: str, flavour: str, stem: str, base: str, extension: str) -> str:
    if language == "python":
        return f"{stem}_port.py"
    if language == "java":
        return f"{base}Port.java"
    if language == "csharp":
        return f"I{base}Port.cs"
    return f"{stem}.port" + (".ts" if flavour == "typescript" else (extension if extension in (".js", ".mjs", ".cjs") else ".js"))


def _adapter_file_name(language: str, flavour: str, stem: str, base: str, extension: str) -> str:
    if language == "python":
        return f"{stem}_adapter.py"
    if language == "java":
        return f"{base}Adapter.java"
    if language == "csharp":
        return f"{base}Adapter.cs"
    return f"{stem}.adapter" + (".ts" if flavour == "typescript" else (extension if extension in (".js", ".mjs", ".cjs") else ".js"))


def _adds_nothing(site: _Site, content: str) -> bool:
    """Whether retrying this edit or addition without the import would change nothing.

    An Edit whose new_string only added the import came back as an Edit with
    new_string equal to old_string, which Claude Code's Edit tool rejects as a
    no-op, and a `>>` of only the import came back as an empty addition. Neither
    is a fix: the file should be left as it is.
    """
    if site.shape == "edit" and site.old_string is not None:
        return content == site.old_string or content.strip() == site.old_string.strip()
    if site.shape == "partial":
        return not content.strip()
    return False


def _plan_layers(
    path: str,
    group: List[_Site],
    cleaned: List[str],
    removed: List[_Removed],
    rule_ids: List[str],
    rules: List[Dict[str, Any]],
) -> _Outcome:
    """The domain write, the port and the adapter, tried at each candidate layer until the rules accept one."""
    language = language_for(path)
    flavour = _script_flavour(path) if language == "typescript" else language
    directory, _, stem, extension = _split(path)
    base = _pascal(stem)
    port = f"I{base}Port" if language == "csharp" else f"{base}Port"
    adapter = f"{base}Adapter"
    libraries = _libraries(removed)
    modules = list(dict.fromkeys(statement.module.rstrip(".") for statement in removed))
    bindings = list(dict.fromkeys(binding for statement in removed for binding in statement.bindings))
    methods = _members("\n".join(cleaned), bindings, language)
    whole = len(group) == 1 and group[0].shape == "write"
    separate_port = language in ("java", "csharp") or not whole
    masked_original = _mask(group[0].content or "", language)
    block_style = bool(re.search(r"(?m)^[ \t]*namespace[ \t]+[\w.]+\s*\{", masked_original))
    # A whole Python file that parsed as sent must still parse as proposed. One
    # that arrived mid-edit, or a fragment, is judged only by the gates, as the
    # gate judges it.
    parsed_before = language == "python" and whole and _parse_python(group[0].content or "") is not None

    domain_package = ""
    if language == "java":
        found = _JAVA_PACKAGE.search(masked_original)
        domain_package = found.group(1) if found else _package_from_directory(directory, "java")
    elif language == "csharp":
        found = _CSHARP_NAMESPACE.search(masked_original)
        domain_package = found.group(1) if found else _package_from_directory(directory, "csharp")

    outcome = _Outcome()
    domain_entries: List[Dict[str, Any]] = []
    new_files: List[Dict[str, Any]] = []
    unchanged: List[_Site] = []
    if whole and not separate_port:
        if language == "python":
            content = _insert_python_port(cleaned[0], _python_port(port, methods, libraries, stem))
        else:
            content = _insert_script_port(cleaned[0], _script_port(port, methods, libraries, stem, flavour))
        domain_entries.append(_entry(group[0], content))
        port_path = path
    else:
        for site, content in zip(group, cleaned):
            if _adds_nothing(site, content):
                unchanged.append(site)
                continue
            domain_entries.append(_entry(site, content))
        port_path = _join(directory, _port_file_name(language, flavour, stem, base, extension))
        if language == "python":
            port_text = (
                f'"""The port {_comment_text(stem)} depends on, declared in the domain so it never depends on '
                f'{_comment_text(libraries)}."""\n'
                "from typing import Any, Protocol\n\n\n" + _python_port(port, methods, libraries, stem)
            )
        elif language == "java":
            port_text = _java_port(domain_package, port, methods, libraries, stem)
        elif language == "csharp":
            port_text = _csharp_port(domain_package, block_style, port, methods, libraries, stem)
        else:
            port_text = _script_port(port, methods, libraries, stem, flavour)
        new_files.append({"path": port_path, "content": port_text, "new_file": True})

    for entry, must_parse in [(entry, parsed_before) for entry in domain_entries] + [(entry, True) for entry in new_files]:
        checks, why = _check_entry(entry, rules, must_parse=must_parse)
        outcome.checks.extend(checks)
        if why:
            outcome.ok = False
            outcome.why = outcome.why or f"the domain side still fails a check: {why}"
    if not outcome.ok:
        outcome.steps.append(
            f"In {path}, remove the import of {libraries} and move it behind a port; "
            f"a mechanical rewrite was checked and still fails: {outcome.why}."
        )
        return outcome

    attempts: List[Dict[str, Any]] = []
    tried: List[str] = []
    refused_by_rules = 0
    for adapter_directory, layer, replaced in _adapter_directories(path, rules, rule_ids):
        adapter_path = _join(adapter_directory, _adapter_file_name(language, flavour, stem, base, extension))
        tried.append(adapter_path)
        adapter_package = ""
        if language == "java":
            adapter_package = _swap_segment(domain_package, replaced, layer.lower()) or _package_from_directory(adapter_directory, "java")
        elif language == "csharp":
            adapter_package = _swap_segment(domain_package, replaced, layer) or _package_from_directory(adapter_directory, "csharp")
        statements = [_render_statement(statement, language, path, adapter_path) for statement in removed]
        statements = list(dict.fromkeys(statements))
        text = _adapter_text(
            language, flavour, adapter, port, port_path, adapter_path, statements, methods, libraries,
            (domain_package, adapter_package), block_style,
        )
        entry = {"path": adapter_path, "content": text, "new_file": True}
        checks, why = _check_entry(entry, rules, must_parse=True)
        attempts.extend(checks)
        if why:
            refused_by_rules += any(not check["passed"] and check["gate"] != GATE_SYNTAX for check in checks)
            continue
        outcome.writes = domain_entries + new_files + [entry]
        outcome.checks.extend(checks)
        outcome.plans.append(
            {"path": path, "libraries": libraries, "modules": modules, "port": port, "port_path": port_path, "adapter": adapter_path}
        )
        _layer_steps(
            outcome, path, group, removed, bindings, cleaned, language, port, port_path, adapter, adapter_path,
            methods, flavour, bool(domain_entries), unchanged, [write["path"] for write in new_files] + [adapter_path],
        )
        return outcome

    outcome.ok = False
    outcome.checks.extend(attempts)
    places = ", ".join(tried) or "no directory at all"
    if tried and refused_by_rules < len(tried):
        # At least one layer would take the import; what failed was the file
        # Threefold wrote there, which a person can write where a template cannot.
        outcome.why = f"the adapter Threefold would write for {libraries} ({places}) does not parse"
        outcome.steps.append(
            f"In {path}, move {libraries} behind a port and write the adapter by hand at one of {places}: "
            "the one generated there does not parse, usually because a directory name is not a valid module name."
        )
        return outcome
    outcome.why = f"no layer these rules permit can hold {libraries}: every candidate ({places}) is refused too"
    outcome.steps.append(
        f"In {path}, {libraries} cannot simply move to another layer: the rules refuse it at {places} as well. "
        "Ask the architect which layer may depend on it, or change the rule, then move the import there behind a port."
    )
    return outcome


def _layer_steps(
    outcome: _Outcome,
    path: str,
    group: List[_Site],
    removed: List[_Removed],
    bindings: List[str],
    cleaned: List[str],
    language: str,
    port: str,
    port_path: str,
    adapter: str,
    adapter_path: str,
    methods: Sequence[Tuple[str, Optional[str]]],
    flavour: str,
    domain_changes: bool,
    unchanged: List[_Site],
    new_paths: List[str],
) -> None:
    statements = list(dict.fromkeys(re.sub(r"\s+", " ", statement.text).strip() for statement in removed))
    rules_named = ", ".join(dict.fromkeys(f"'{statement.rule_id}'" for statement in removed if statement.rule_id))
    shown = ", ".join(f"`{text}`" for text in statements[:4]) + (f" and {len(statements) - 4} more" if len(statements) > 4 else "")
    kind = {"python": "a Protocol", "java": "an interface", "csharp": "an interface"}.get(
        language, "an interface" if flavour == "typescript" else "a class to extend"
    )
    forbids = f"which rule {rules_named or 'in force'} forbids there"
    if domain_changes:
        retry = {"edit": "Retry the Edit with the new text below", "partial": "Add the text below with Edit"}.get(
            group[0].shape, "Write the domain file below"
        )
        outcome.steps.append(f"{retry}: it drops {shown} from {path}, {forbids}.")
        if unchanged:
            outcome.steps.append(
                f"Leave out the call's other change(s) to {path}: without the import they change nothing."
            )
    else:
        what = "the edit" if group[0].shape == "edit" else "that addition"
        outcome.steps.append(f"Nothing needs retrying in {path}: without {shown}, {forbids}, {what} adds nothing.")
    where = "in the same file" if port_path == path else f"in {port_path}, beside it in the domain"
    outcome.steps.append(f"Declare {port}, {kind}, {where}, for what the domain needs: {', '.join(name for name, _ in methods)}.")
    outcome.steps.append(f"Create {adapter_path}: {adapter} holds the import and implements {port}; the rules do not refuse it there.")
    single = len(new_paths) == 1
    outcome.steps.append(
        f"{' and '.join(new_paths)} {'is' if single else 'are'} proposed as new, and Threefold cannot see whether "
        f"{'it already exists' if single else 'they already exist'}: if {'it does' if single else 'one does'}, "
        "add the class to it with Edit rather than overwrite it."
    )
    remaining = _uses("\n".join(cleaned), bindings, language)
    if remaining:
        outcome.steps.append(
            f"Route the {remaining} remaining use(s) of {', '.join(bindings[:4])} in {path} through {port}: "
            f"take it as a constructor or function parameter, and pass an instance of {adapter} in from outside the domain."
        )
    generic = [name for name, target in methods if target is None]
    if generic:
        outcome.steps.append(f"Rename {port}.{generic[0]} after what the domain actually asks for; nothing it calls could be read.")


# --- the writes fix: layering, and writes the rules could not read ---------------------


def _write_fix(
    invocation: ToolInvocation,
    rules: List[Dict[str, Any]],
    diagnosis: _Diagnosis,
    max_content_chars: int = MAX_CONTENT_CHARS,
) -> _Fix:
    command = shell_command(invocation)
    if command is not None:
        return _shell_write_fix(invocation, rules, diagnosis, command, max_content_chars)
    written = [site for site in _tool_sites(invocation.arguments) if site.content is not None]
    sites = [site for site in written if _flagged_modules(site.path, site.content, rules)]
    if not sites:
        return _Fix(
            diagnosis.kind,
            f"No checked fix: the rules flag {diagnosis.path or 'this call'}, but no write in it could be rewritten.",
            [f"Remove the import the rule names from {diagnosis.path or 'the file'} and move it behind a port outside the domain."],
        )
    outcome = _fix_sites(sites, rules, max_content_chars, sum(len(site.content or "") for site in written))
    return _assemble(diagnosis.kind, outcome, [], shell=False)


def _fix_sites(
    sites: List[_Site],
    rules: List[Dict[str, Any]],
    max_content_chars: int = MAX_CONTENT_CHARS,
    written: Optional[int] = None,
) -> _Outcome:
    """The fix for these sites, rewritten while the call's writes come to `max_content_chars` or less.

    `written` is how much the call writes, taken together: every write in it,
    including those no rule flags and so are not among `sites`, because the
    proposal reads each of them as well. It defaults to the sites' own content.
    """
    outcome = _Outcome()
    groups: Dict[str, List[_Site]] = {}
    for site in sites:
        groups.setdefault(site.path, []).append(site)
    # What a fix costs grows with all the text the call writes, so the cap is
    # on the call's writes taken together: a MultiEdit of twenty edits, each
    # under the cap, would otherwise be twenty rewrites, and a small flagged
    # edit beside a large clean one is read in full to find that it is clean.
    # And a fix is validated only when every write in it is, so rewriting the
    # small files of a call whose large one gets advice would be work nobody
    # receives.
    size = sum(len(site.content or "") for site in sites) if written is None else max(0, written)
    too_large = size > max_content_chars
    for path, group in groups.items():
        part = _fix_path(path, group, rules, max_content_chars if too_large else None)
        outcome.writes.extend(part.writes)
        outcome.checks.extend(part.checks)
        outcome.steps.extend(part.steps)
        outcome.plans.extend(part.plans)
        outcome.advice = outcome.advice or part.advice
        if not part.ok:
            outcome.ok = False
            outcome.why = outcome.why or part.why
    if outcome.advice:
        # Said once for the call, after each file's own steps: it is the
        # call's size, not any one file's, that put the rewrite out of reach.
        outcome.steps.append(
            f"What this call writes comes to {size:,} characters, over the {max_content_chars:,} Threefold rewrites and "
            "checks within a verdict, so this is advice in words and no code is proposed. The write that follows it is "
            "judged by the same gates as any other."
        )
    return outcome


def _permitted_layer(
    path: str, modules: Sequence[str], rules: List[Dict[str, Any]], rule_ids: Sequence[str]
) -> Tuple[str, List[str]]:
    """The first directory for an adapter holding `modules` where no rule forbids them. (directory, tried)

    The candidates are _plan_layers' own, derived from the rules' patterns, in
    the same order, and each is asked whether a rule covering the adapter file
    _plan_layers would write there flags one of the modules. Nothing is written
    or parsed: the modules are already known, so this is a match of each
    against the rules covering that path. The directory is empty when every
    candidate forbids them.

    It asks less than _plan_layers does. The rewrite judges the adapter's whole
    text, its import of the port back into the domain included, with every gate;
    this judges only the imports being moved. So the advice says no rule
    forbids them there, never that an adapter there would pass.
    """
    language = language_for(path)
    flavour = _script_flavour(path) if language == "typescript" else language
    _, _, stem, extension = _split(path)
    name = _adapter_file_name(language, flavour, stem, _pascal(stem), extension)
    tried: List[str] = []
    for directory, _, _ in _adapter_directories(path, rules, rule_ids):
        tried.append(directory)
        if not _refused_at(_join(directory, name), modules, rules):
            return directory, tried
    return "", tried


def _too_large(path: str, group: List[_Site], rules: List[Dict[str, Any]], cap: int = MAX_CONTENT_CHARS) -> _Outcome:
    """Advice in words for writes too large to rewrite, from the one read of their imports already made.

    It names the imports to move (the first four, and how many more there are),
    the rules that forbid them (the first three, and "others" past that), and
    the first candidate layer where no rule forbids them, or says that every
    candidate does. It carries no code and no checks, because nothing was
    rewritten to check. The sentence saying why, the call's size against the
    cap, is added once for the whole call by _fix_sites.
    """
    flagged: Dict[str, str] = {}
    for site in group:
        flagged.update(_flagged_modules(path, site.content or "", rules))
    modules = list(flagged)
    rule_ids = list(dict.fromkeys(flagged.values()))
    shown = ", ".join(modules[:4]) + (f" and {len(modules) - 4} more" if len(modules) > 4 else "")
    shown = shown or "the module the rule names"
    named = (modules[0] if len(modules) == 1 else f"{modules[0]} and {len(modules) - 1} more") if modules else "the import"
    rules_named = ", ".join(f"'{rule_id}'" for rule_id in rule_ids[:3]) + (" and others" if len(rule_ids) > 3 else "")
    which = f"rule {rules_named}" if len(rule_ids) == 1 else (f"rules {rules_named}" if rule_ids else "a rule in force")
    them, imports = ("them", "imports") if len(modules) > 1 else ("it", "import")
    directory, tried = _permitted_layer(path, modules, rules, rule_ids)
    steps = [f"In {path}, remove the {imports} of {shown}: {which} forbids {them} there."]
    if directory:
        steps.append(
            f"Declare a port in the domain for what {path} needs from {shown}, and move the {imports} into an adapter "
            f"under {directory}/ that implements it: no layering rule in force forbids {them} there. Pass the adapter in "
            "from outside the domain."
        )
        why = f"too large to rewrite within the verdict (over {cap:,} characters); move {named} behind a port, adapter under {_short(directory)}"
    else:
        places = ", ".join(f"{place}/" for place in tried) or "no directory at all"
        steps.append(
            f"{shown} cannot simply move to another layer: the rules refuse {them} at {places} as well. Ask the "
            f"architect which layer may depend on {them}, or change the rule, then move the {imports} there behind a port."
        )
        why = f"too large to rewrite within the verdict (over {cap:,} characters), and no layer these rules permit can hold {named}"
    return _Outcome(ok=False, why=why, steps=steps, advice=True)


def _fix_path(path: str, group: List[_Site], rules: List[Dict[str, Any]], too_large_over: Optional[int] = None) -> _Outcome:
    """The fix for one path's writes; advice in words when `too_large_over` names the cap the call is past."""
    flagged = [site for site in group if site.content is not None and _flagged_modules(path, site.content, rules)]
    if not flagged:
        # Nothing to move: the content as sent, or as recovered from the
        # command, is itself the proposal, checked like any other.
        part = _Outcome()
        for site in group:
            entry = _entry(site, site.content or "")
            checks, why = _check_entry(entry, rules, must_parse=site.must_parse)
            part.writes.append(entry)
            part.checks.extend(checks)
            if why:
                part.ok = False
                part.why = part.why or why
        return part
    if too_large_over is not None:
        return _too_large(path, group, rules, too_large_over)
    cleaned: List[str] = []
    removed: List[_Removed] = []
    rule_ids: List[str] = []
    for site in group:
        new, taken, ids, why = _remove_offending(path, site.content or "", rules)
        if why:
            return _Outcome(ok=False, why=why, steps=[f"In {path}, {why}; take the import out by hand and move it behind a port."])
        cleaned.append(new)
        removed.extend(taken)
        rule_ids.extend(rule_id for rule_id in ids if rule_id not in rule_ids)
    return _plan_layers(path, group, cleaned, removed, rule_ids, rules)


def _route_advice(write: ShellWrite) -> str:
    """What to do instead of a shell write whose content Threefold could not see."""
    target = shell_display(write.target) if write.target else ""
    route = write.route or "the shell"
    if write.target is None:
        return (
            f"The command writes by {route} to files it does not name in a way that can be read: make each change "
            "as an Edit or a Write naming its file, so the rule reads it before it lands."
        )
    if write.pattern:
        return f"'{target}' is named with a shell expansion or a glob: name the file literally and write it with Write."
    if write.fragment or route.startswith(("sed", "perl", "awk")):
        return (
            f"Change {target} with Edit, giving the whole line as old_string and the whole new line as new_string, "
            f"instead of {route}."
        )
    if route in ("cp", "mv", "install", "ln", "rsync", "copy-item", "move-item", "dd of=") or route.startswith(("cp", "mv")):
        return f"Read the file the command copies and Write {target} with its content, instead of {route}, so the rule reads it first."
    if route.startswith(("curl", "wget")):
        return f"Download outside the project, check what arrived, then Write {target} with it."
    if route == "heredoc":
        return f"Quote the heredoc delimiter (<<'EOF') so its text is literal, or better, Write {target} directly."
    if route.startswith(("redirect", "exec")):
        return f"The command sends a program's output to {target}: run the program on its own, then Write {target} with what it printed."
    return f"Write {target} with Write or Edit instead of {route}, so the rule can read it."


def _route_check(write: ShellWrite) -> Optional[Dict[str, Any]]:
    if write.target is None or write.pattern:
        return None
    target = write.target
    readable = bool(language_for(target)) and not is_governance_path(target) and not ArchitecturalBoundaryGuard.is_forbidden_file_access(target)
    return {"gate": GATE_ROUTE, "path": target, "passed": readable}


def _shell_write_fix(
    invocation: ToolInvocation,
    rules: List[Dict[str, Any]],
    diagnosis: _Diagnosis,
    command: Any,
    max_content_chars: int = MAX_CONTENT_CHARS,
) -> _Fix:
    cwd = command_cwd(invocation)
    analysis = diagnosis.analysis or analysed(command, cwd)
    if analysis.truncated:
        return _Fix(
            KIND_UNREADABLE,
            "No checked fix: the command is too long to be read to the end. Split it, and make its file writes with Write.",
            [
                "The command is too long to be read to the end for the files it writes, and an enforce rule is active.",
                "Split it into shorter calls, and make each file write with Write or Edit so the rule reads it.",
            ],
        )
    recovered = _literal_heredocs(command, cwd)
    sites: List[_Site] = []
    notes: List[str] = []
    route_checks: List[Dict[str, Any]] = []
    written = 0
    for write in analysis.writes:
        if write.deletes:
            continue
        written += len(write.content or recovered.get(write.target or "", "") or "")
        single = ShellAnalysis(writes=[write])
        if not (shell_refusal(single, rules) or shell_observations(single, rules)):
            continue
        content = write.content
        literal = False
        if content is None and write.route == "heredoc" and write.target in recovered:
            content = recovered[write.target]
            literal = True
        if write.target is None or write.pattern or content is None or write.fragment:
            notes.append(_route_advice(write))
            check = _route_check(write)
            if check:
                route_checks.append(check)
            continue
        shape = "partial" if _adds_to_file(write, command) else "write"
        sites.append(_Site(write.target, content, shape, literal_heredoc=literal))
    if not sites and not notes:
        return _Fix(
            diagnosis.kind,
            "No checked fix: make the command's file writes with Write or Edit so the rules can read them.",
            ["Make the command's file writes with Write or Edit, naming each file, so the rules read them before they land."],
        )
    outcome = _fix_sites(sites, rules, max_content_chars, written) if sites else _Outcome(ok=False)
    outcome.checks.extend(route_checks)
    extra: List[str] = []
    if any(site.literal_heredoc for site in sites):
        extra.append(
            "The heredoc's delimiter was not quoted, so the shell would have expanded `$...` in its text. The proposed "
            "Write carries the text exactly as written; if the expansion was meant, work the value out first and write it literally."
        )
    if sites:
        extra.append(
            "Make these writes with Write (or Edit, for text added to a file) instead of the shell command, so the rules "
            "read them before they land. If the command did more than write these files, run the rest as a call of its own."
        )
    return _assemble(diagnosis.kind, outcome, notes, shell=True, extra=extra)


def _assemble(kind: str, outcome: _Outcome, notes: List[str], shell: bool, extra: Optional[List[str]] = None) -> _Fix:
    """One fix from what each site came to: validated only if every write was fixed and passed."""
    validated = outcome.ok and not notes and bool(outcome.writes) and all(check["passed"] for check in outcome.checks if check["gate"] != GATE_ROUTE)
    steps = list(outcome.steps) + list(notes) + list(extra or [])
    if validated:
        steps.append("Threefold ran the same gates, with the same rules, on every proposed write, and each passed.")
        summary = _layering_summary(kind, outcome)
        return _Fix(kind, summary, steps, outcome.writes, True, outcome.checks)
    why = (notes[0] if notes and (kind == KIND_UNREADABLE or not outcome.why) else outcome.why) or "the proposal did not pass the gates"
    summary = f"No checked fix: {why[:1].lower()}{why[1:].rstrip('.')}."
    # A textual fix carries no code: a write that has not passed is not handed out.
    return _Fix(kind, summary, steps, [], False, outcome.checks)


def _layering_summary(kind: str, outcome: _Outcome) -> str:
    if not outcome.plans:
        paths = ", ".join(dict.fromkeys(_short(write["path"]) for write in outcome.writes))
        return f"Checked fix: send the writes below with Write instead of the shell ({paths}); they pass the same rules."
    plan = outcome.plans[0]
    # Counted from the modules themselves: the display string is already cut to
    # "a, b, c and N more", and counting its commas said "2 more" for sixty.
    modules = plan["modules"] or [plan["libraries"]]
    named = modules[0] if len(modules) == 1 else f"{modules[0]} and {len(modules) - 1} more"
    more = f" ({len(outcome.plans) - 1} more file(s) likewise)" if len(outcome.plans) > 1 else ""
    return (
        f"Checked fix: move {named} out of the domain behind {plan['port']}; "
        f"adapter: {_short(plan['adapter'], 90)}{more}."
    )


def _short(path: str, limit: int = 60) -> str:
    path = shell_display(path)
    return path if len(path) <= limit else "..." + path[-(limit - 3):]


# --- credentials: read it from the environment instead ---------------------------------

_PREFIXED_LABELS = ("AWS_SECRET_KEY", "GENERIC_API_KEY")
_VALUE_AFTER_KEY = re.compile(r"[:=]\s*['\"]?([A-Za-z0-9/+=_\-]{20,})")
_TOKEN_CHARACTER = re.compile(r"[A-Za-z0-9/+=_\-]")
PEM_LABEL = "PRIVATE_KEY_HEADER"
_PEM_HEADER = dict(SecretScanner.PATTERNS)[PEM_LABEL]
_PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
# What separates the lines of a key: a newline, or the two characters `\n` when
# the key sits in a one-line string literal.
_PEM_BREAK = r"(?:[ \t]|\r?\n|\\r|\\n)"
# The body of a key, line after line: base64 that runs to the end of its line,
# or the header fields an encrypted key carries. Each line must end where a
# line or a string ends, so the first word of the next line of code (`other:`
# in YAML) is never taken for base64.
_PEM_BODY = re.compile(
    rf"(?:{_PEM_BREAK}+(?:[A-Za-z0-9+/]{{4,}}={{0,2}}|Proc-Type:[^\n\\]*|DEK-Info:[^\n\\]*)"
    r"(?=[ \t]*(?:\r?\n|\\[nr]|$|[\"'`])))*"
)
_PEM_TAIL = re.compile(rf"{_PEM_BREAK}*-----END [A-Z ]*PRIVATE KEY-----")
# A line that holds nothing but base64, as a key's body does however it is
# quoted: bare, indented in YAML, or one string per line joined with `+`.
_KEY_LINE = re.compile(r"(?m)^[\s\"'`+(,]*([A-Za-z0-9+/]{16,}={0,2})(?:\\[nr])*[\s\"'`+),;]*$")
_KEY_REGION_WITH_END = 16_384
_KEY_REGION_WITHOUT_END = 4_096
_DEFAULT_ENVIRONMENT_NAMES = {
    "AWS_ACCESS_KEY": "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_KEY": "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN": "GITHUB_TOKEN",
    "OPENAI_KEY": "OPENAI_API_KEY",
    "ANTHROPIC_KEY": "ANTHROPIC_API_KEY",
    "SLACK_TOKEN": "SLACK_TOKEN",
    "GOOGLE_API_KEY": "GOOGLE_API_KEY",
    "JWT": "AUTH_TOKEN",
    "GENERIC_API_KEY": "API_KEY",
    "PRIVATE_KEY_HEADER": "PRIVATE_KEY_PEM",
}
# Names that say "a secret" without saying which. A label that knows better
# (an AWS key is AWS_ACCESS_KEY_ID wherever it sits) wins over these.
_GENERIC_NAMES = frozenset(
    (
        "KEY", "TOKEN", "SECRET", "PASSWORD", "VALUE", "API_KEY", "APIKEY", "AUTH", "AUTHORIZATION", "BEARER",
        "CREDENTIAL", "CREDENTIALS", "SECRET_KEY", "ACCESS_KEY", "DATA", "HEADER", "HEADERS", "PAT",
    )
)
# Names a process already uses for something else. Proposing one would read the
# system's PATH or HOME as the credential, and a step saying "set PATH in the
# environment" would break the shell of whoever followed it.
_RESERVED_NAMES = frozenset(
    (
        "PATH", "HOME", "USER", "USERNAME", "LOGNAME", "USERPROFILE", "USERDOMAIN", "PWD", "OLDPWD", "CWD",
        "SHELL", "TEMP", "TMP", "TMPDIR", "TERM", "LANG", "LANGUAGE", "OS", "COMSPEC", "WINDIR", "SYSTEMROOT",
        "SYSTEMDRIVE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMDATA", "PATHEXT", "HOSTNAME", "HOST",
        "PORT", "URL", "URI", "IFS", "PS1", "PS2", "PS4", "PROMPT_COMMAND", "EDITOR", "VISUAL", "PAGER",
        "DISPLAY", "MAIL", "TZ", "CI", "LD_LIBRARY_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH", "PYTHONPATH",
        "PYTHONHOME", "NODE_PATH", "NODE_OPTIONS", "NODE_ENV", "JAVA_HOME", "GOPATH", "GOROOT", "SSH_AUTH_SOCK",
        "SSH_AGENT_PID", "GPG_AGENT_INFO", "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE", "GOOGLE_APPLICATION_CREDENTIALS", "KUBECONFIG", "DOCKER_HOST",
        "DOCKER_CONFIG", "GIT_DIR", "GIT_WORK_TREE",
    )
)
_RESERVED_PREFIXES = ("XDG_", "LC_", "NPM_CONFIG_", "RUNNER_")
# A name that ends in one of these names a place, not a secret: `REPO_URL`
# holding a URL with a token in it wants the token read from the environment,
# not a variable called REPO_URL that would have to hold the token alone.
_LOCATION_SUFFIXES = frozenset(
    (
        "URL", "URI", "HOST", "HOSTNAME", "PORT", "PATH", "DIR", "DIRECTORY", "FILE", "FILENAME", "HOME",
        "ENDPOINT", "ADDRESS", "ADDR", "DOMAIN", "REGION", "EMAIL", "USER", "USERNAME",
    )
)
_FLAVOURS = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".cs": "csharp", ".go": "go", ".rb": "ruby", ".php": "php",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".ps1": "powershell", ".psm1": "powershell",
}
_LOOKUPS = {
    "python": 'os.environ["{name}"]',
    "typescript": "process.env.{name}",
    "javascript": "process.env.{name}",
    "java": 'System.getenv("{name}")',
    "kotlin": 'System.getenv("{name}")',
    "csharp": 'System.Environment.GetEnvironmentVariable("{name}")',
    "go": 'os.Getenv("{name}")',
    "ruby": 'ENV["{name}"]',
    "php": "getenv('{name}')",
    "powershell": "$env:{name}",
}
_POSIX_SHELLS = frozenset(("sh", "bash", "zsh", "dash", "ksh", "ash", "mksh"))
_POWERSHELLS = frozenset(("pwsh", "powershell"))


class _CannotSubstitute(Exception):
    """A secret that sits where no lookup could take its place and still be read."""


def _flavour(path: str) -> str:
    """The language a file is written in, for its environment lookup; 'config' for the rest."""
    _, name, _, extension = _split(path)
    return _FLAVOURS.get(extension.lower(), "config")


def _has_secret(text: Optional[str]) -> bool:
    return bool(text) and not SecretScanner.scan_payload(text)[0]


def _pem_extent(text: str, end: int) -> int:
    """Where a private key that starts with a header ending at `end` ends.

    Through its body and its END line when they follow. The scanner matches
    only the header, and a span that stopped there replaced the header and left
    the whole key body in the proposed file, where no scanner saw it again.
    """
    body = _PEM_BODY.match(text, end)
    stop = body.end() if body else end
    tail = _PEM_TAIL.match(text, stop)
    return tail.end() if tail else stop


def _key_material(text: str) -> List[str]:
    """The lines of base64 after each private key header: what a key's body looks like.

    Found independently of the rewrite, so a body the rewrite missed (split
    across edits, cut off, or joined from one string per line) is still known,
    and is kept out of the answer.
    """
    runs: List[str] = []
    for header in _PEM_HEADER.finditer(text or ""):
        closing = _PEM_END.search(text, header.end(), header.end() + _KEY_REGION_WITH_END)
        stop = closing.start() if closing else header.end() + _KEY_REGION_WITHOUT_END
        runs.extend(match.group(1) for match in _KEY_LINE.finditer(text, header.end(), stop))
    return list(dict.fromkeys(runs))


def _secret_spans(text: str) -> List[Tuple[int, int, str]]:
    """Where each credential sits: the value itself, not the key name in front of it."""
    spans: List[Tuple[int, int, str]] = []
    for label, pattern in SecretScanner.PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if label in _PREFIXED_LABELS:
                inner = _VALUE_AFTER_KEY.search(text, start, end)
                if inner:
                    start, end = inner.span(1)
            elif label == PEM_LABEL:
                end = _pem_extent(text, end)
            # The pattern can stop short of the token, which would leave its
            # tail in the file beside the lookup. The whole token goes.
            while end < len(text) and label != PEM_LABEL and _TOKEN_CHARACTER.match(text[end]):
                end += 1
            spans.append((start, end, label))
    spans.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    kept: List[Tuple[int, int, str]] = []
    for span in spans:
        if kept and span[0] < kept[-1][1]:
            if span[1] > kept[-1][1]:
                kept[-1] = (kept[-1][0], span[1], kept[-1][2])
            continue
        kept.append(span)
    return kept


def _withheld(arguments: Any) -> List[str]:
    """The text of every secret anywhere in a call's arguments, to be kept out of its fix."""
    found: List[str] = []
    for leaf in iter_string_leaves(arguments if isinstance(arguments, (dict, list, tuple)) else {}):
        if not isinstance(leaf, str):
            continue
        found.extend(leaf[start:end] for start, end, _ in _secret_spans(leaf) if end - start >= 8)
        found.extend(_key_material(leaf))
    return list(dict.fromkeys(found))


def _environment_name(text: str, start: int, label: str) -> str:
    """The variable to read: the name the code gave the value, or the label's usual one."""
    default = _DEFAULT_ENVIRONMENT_NAMES.get(label, "API_KEY")
    line_start = text.rfind("\n", 0, start) + 1
    # Back to before the literal the value sits in, so `"Bearer ` in front of a
    # token does not hide the name the literal is assigned to.
    before = re.sub(r"""[`'"]+[^`'"]*$""", "", text[line_start:start])
    found = re.search(r"([A-Za-z_][A-Za-z0-9_\-]*)['\"]?\s*(?::=|=>|[:=])\s*(?:[A-Za-z_][\w.]*\(\s*)?$", before)
    if not found:
        return default
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", found.group(1)).replace("-", "_").upper()
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]{2,63}", name):
        return default
    if name in _GENERIC_NAMES and label not in ("GENERIC_API_KEY", "JWT"):
        return default
    if name in _RESERVED_NAMES or name.startswith(_RESERVED_PREFIXES) or name.rsplit("_", 1)[-1] in _LOCATION_SUFFIXES:
        return default
    return name


def _enclosing_literal(text: str, start: int, end: int, flavour: str) -> Tuple[str, Optional[Tuple[int, int, str, str]]]:
    """What a secret sits in: ("literal", (open, close, quote, prefix)), or "unterminated", "comment" or "bare".

    "unterminated" is a string opened before the secret whose end this text
    does not hold (the rest is in another edit, or was cut off): a lookup put
    there would be read as literal text, so nothing can be substituted.
    """
    line_start = text.rfind("\n", 0, start) + 1
    segment = text[line_start:start]
    opened: Optional[str] = None
    open_at = -1
    position = 0
    triple_ok = flavour in ("python", "java", "kotlin")
    while position < len(segment):
        character = segment[position]
        if opened:
            if character == "\\" and flavour != "shell":
                position += 2
                continue
            if segment.startswith(opened, position):
                opened = None
                position += 1
                continue
            position += 1
            continue
        if character in "\"'`":
            quote = character * 3 if triple_ok and segment.startswith(character * 3, position) else character
            opened, open_at = quote, line_start + position
            position += len(quote)
            continue
        if flavour == "python" and character == "#":
            return "comment", None
        if flavour not in ("python", "shell", "config") and segment.startswith("//", position):
            return "comment", None
        position += 1
    if opened:
        close = _closing_quote(text, end, opened, flavour)
        if close is None:
            return "unterminated", None
        return "literal", (open_at, close, opened, _string_prefix(text, open_at, flavour))
    for quote in ('"""', "'''", "`"):
        if quote != "`" and not triple_ok:
            continue
        if quote == "`" and flavour not in ("typescript", "javascript", "go"):
            continue
        opening = text.rfind(quote, 0, start)
        if opening == -1 or text[opening + len(quote):start].strip():
            continue
        closing = text.find(quote, end)
        if closing != -1 and not text[end:closing].strip():
            return "literal", (opening, closing, quote, _string_prefix(text, opening, flavour))
        if closing == -1:
            return "unterminated", None
    return "bare", None


def _closing_quote(text: str, position: int, quote: str, flavour: str) -> Optional[int]:
    multiline = len(quote) == 3 or quote == "`"
    while position < len(text):
        if text[position] == "\n" and not multiline:
            return None
        if text[position] == "\\" and flavour != "shell":
            position += 2
            continue
        if text.startswith(quote, position):
            return position
        position += 1
    return None


def _string_prefix(text: str, open_at: int, flavour: str) -> str:
    if flavour != "python":
        return ""
    found = re.search(r"(?i)(?<![\w])(rb|br|fr|rf|r|b|u|f)$", text[max(0, open_at - 2):open_at])
    return found.group(1) if found else ""


_HEREDOC = re.compile(r"(?<![<\w])<<(-?)[ \t]*(\\?)(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\3")


def _heredoc_bodies(text: str) -> List[Tuple[int, int, bool]]:
    """(start, end, quoted) of each heredoc body in a shell command, in order.

    Several openers on one line have bodies one after another, as the shell
    reads them. A quoted delimiter (<<'EOF', <<"EOF", <<\\EOF) makes the body
    literal, so no variable in it is ever expanded.
    """
    bodies: List[Tuple[int, int, bool]] = []
    cursor = 0
    for match in _HEREDOC.finditer(text):
        if match.start() < cursor:
            continue
        line_end = text.find("\n", match.end())
        if line_end == -1:
            break
        body_start = line_end + 1
        for opener in _HEREDOC.finditer(text, match.start(), line_end):
            tabs = r"\t*" if opener.group(1) == "-" else ""
            closing = re.compile(r"(?m)^" + tabs + re.escape(opener.group(4)) + r"[ \t]*\r?$").search(text, body_start)
            body_end = closing.start() if closing else len(text)
            bodies.append((body_start, body_end, bool(opener.group(2) or opener.group(3))))
            body_start = min(len(text), closing.end() + 1) if closing else len(text)
        cursor = body_start
    return bodies


def _shell_context(text: str, start: int, bodies: Sequence[Tuple[int, int, bool]]) -> Tuple[str, int]:
    """Where `start` sits in a shell command: bare, single, ansi ($'...'), double or comment; and where that opened.

    Read from the start of the command, not of the line, because a quoted
    string may span lines; with a backslash escaping the next character outside
    single quotes, so `'it'\\''s` is read as the shell reads it; and past each
    heredoc body, whose quotes are text. Reading quotes on one line, as the
    other languages are read, put `"${NAME}"` inside single quotes after a `\\'`.
    """
    body_ends = {body_start: body_end for body_start, body_end, _ in bodies}
    state, opened, position = "bare", -1, 0
    while position < start:
        if state == "bare" and position in body_ends:
            position = max(position + 1, body_ends[position])
            continue
        character = text[position]
        if state == "bare":
            if character == "\\":
                position += 2
                continue
            if text.startswith("$'", position):
                state, opened = "ansi", position
                position += 2
                continue
            if character == "'":
                state, opened = "single", position
            elif character == '"':
                state, opened = "double", position
            elif character == "#" and (position == 0 or text[position - 1] in " \t\r\n;&|("):
                line_end = text.find("\n", position)
                if line_end == -1 or line_end >= start:
                    return "comment", position
                position = line_end
                continue
        elif state == "single":
            if character == "'":
                state = "bare"
        else:
            if character == "\\":
                position += 2
                continue
            if character == ("'" if state == "ansi" else '"'):
                state = "bare"
        position += 1
    return state, opened


def _shell_close(text: str, position: int, quote: str, escapes: bool) -> Optional[int]:
    """The index of the quote that ends a shell string, or None when this text does not hold it."""
    while position < len(text):
        if escapes and text[position] == "\\":
            position += 2
            continue
        if text[position] == quote:
            return position
        position += 1
    return None


def _substitute_shell(text: str, start: int, end: int, name: str) -> str:
    placeholder = "${" + name + "}"
    bodies = _heredoc_bodies(text)
    for body_start, body_end, quoted in bodies:
        if body_start <= start < body_end:
            if quoted:
                raise _CannotSubstitute(
                    "the credential sits in a quoted heredoc (<<'EOF'), whose text the shell never expands"
                )
            # An unquoted heredoc expands ${NAME} as it is; quotes around it
            # would be literal characters in the text.
            return text[:start] + placeholder + text[end:]
    state, opened = _shell_context(text, start, bodies)
    unterminated = "the credential sits in a quoted string that continues past the text this call carries"
    if state == "comment":
        return text[:start] + placeholder + text[end:]
    if state == "bare":
        return text[:start] + '"' + placeholder + '"' + text[end:]
    if state == "double":
        if _shell_close(text, end, '"', escapes=True) is None:
            raise _CannotSubstitute(unterminated)
        return text[:start] + placeholder + text[end:]
    if state == "single":
        close = _shell_close(text, end, "'", escapes=False)
        if close is None:
            raise _CannotSubstitute(unterminated)
        # Close the single quotes, expand in double quotes, open them again;
        # where the secret began or ended the string, without an empty '' there.
        head = text[:opened] if opened == start - 1 else text[:start] + "'"
        tail = text[close + 1:] if close == end else "'" + text[end:]
        return head + '"' + placeholder + '"' + tail
    # $'...': close it, expand, and open a new $'...' so the escapes after the
    # secret keep their meaning.
    if _shell_close(text, end, "'", escapes=True) is None:
        raise _CannotSubstitute(unterminated)
    return text[:start] + "'\"" + placeholder + "\"$'" + text[end:]


def _powershell_context(text: str, start: int) -> Tuple[str, int]:
    """Where `start` sits in PowerShell: bare, single, double, here-single, here-double or comment; and where that opened."""
    state, opened, position = "bare", -1, 0
    while position < start:
        character = text[position]
        pair = text[position:position + 2]
        if state == "bare":
            if pair in ("@'", '@"') and text[position + 2:position + 3] in ("\n", "\r"):
                state, opened = ("here-single" if pair == "@'" else "here-double"), position
                position += 2
                continue
            if character == "#" and (position == 0 or text[position - 1] in " \t\r\n;"):
                line_end = text.find("\n", position)
                if line_end == -1 or line_end >= start:
                    return "comment", position
                position = line_end
                continue
            if character == "'":
                state, opened = "single", position
            elif character == '"':
                state, opened = "double", position
            position += 1
        elif state == "single":
            if pair == "''":
                position += 2
                continue
            if character == "'":
                state = "bare"
            position += 1
        elif state == "double":
            if character == "`" or pair == '""':
                position += 2
                continue
            if character == '"':
                state = "bare"
            position += 1
        else:
            closer = "\n'@" if state == "here-single" else '\n"@'
            if text.startswith(closer, position):
                state = "bare"
                position += len(closer)
                continue
            position += 1
    return state, opened


def _powershell_close(text: str, position: int, quote: str) -> Optional[int]:
    """The index of the quote that closes a PowerShell string, skipping its escapes."""
    while position < len(text):
        if quote == '"' and text[position] == "`":
            position += 2
            continue
        if text.startswith(quote * 2, position):
            position += 2
            continue
        if text[position] == quote:
            return position
        position += 1
    return None


def _as_expandable(single_quoted: str) -> str:
    """The inside of a PowerShell '...' string, said again inside "..." so that it means the same."""
    text = single_quoted.replace("''", "'")
    return text.replace("`", "``").replace("$", "`$").replace('"', '`"')


def _substitute_powershell(text: str, start: int, end: int, name: str) -> str:
    """A secret replaced by $env:NAME where PowerShell expands it.

    PowerShell expands nothing inside '...', and `${NAME}` is one of its own
    variables, not the environment's: the bash spelling offered before was two
    ways wrong. A single-quoted string holding the secret becomes a
    double-quoted one with its other text escaped so that it still reads the
    same; a single-quoted here-string cannot expand at all.
    """
    lookup = "$env:" + name
    state, opened = _powershell_context(text, start)
    if state == "here-single":
        raise _CannotSubstitute("the credential sits in a single-quoted here-string (@'...'@), which PowerShell never expands")
    if state == "comment":
        return text[:start] + lookup + text[end:]
    if state in ("double", "here-double"):
        if state == "double":
            close = _powershell_close(text, end, '"')
            if close is not None and opened + 1 == start and close == end:
                return text[:opened] + lookup + text[close + 1:]
        return text[:start] + "$(" + lookup + ")" + text[end:]
    if state == "single":
        close = _powershell_close(text, end, "'")
        if close is None:
            raise _CannotSubstitute("the credential sits in a quoted string that continues past the text this call carries")
        before, after = text[opened + 1:start], text[end:close]
        if not before and not after:
            return text[:opened] + lookup + text[close + 1:]
        return text[:opened] + '"' + _as_expandable(before) + "$(" + lookup + ")" + _as_expandable(after) + '"' + text[close + 1:]
    whole_word = (start == 0 or text[start - 1] in " \t\r\n=,;(") and (end == len(text) or text[end] in " \t\r\n,;)")
    return text[:start] + (lookup if whole_word else "$(" + lookup + ")") + text[end:]


def _substitute(text: str, start: int, end: int, name: str, flavour: str, bare_lookup: bool = False) -> str:
    """The text with one secret replaced by the language's own way of reading `name`.

    Raises _CannotSubstitute where no lookup could stand in the secret's place
    and be read as one: a validated fix must still read the variable where the
    secret was, not carry `${NAME}` as literal text. With `bare_lookup`, a
    secret outside any string becomes the lookup itself; the caller must then
    prove, with a parser, that the lookup landed in code.
    """
    if flavour == "shell":
        return _substitute_shell(text, start, end, name)
    if flavour == "powershell":
        return _substitute_powershell(text, start, end, name)
    placeholder = "${" + name + "}"
    lookup_format = _LOOKUPS.get(flavour)
    if lookup_format is None:
        # A configuration file has no lookup of its own; ${NAME} is what the
        # tools that read such files substitute, and it carries no secret.
        return text[:start] + placeholder + text[end:]
    lookup = lookup_format.format(name=name)
    kind, literal = _enclosing_literal(text, start, end, flavour)
    if kind == "unterminated":
        raise _CannotSubstitute("the credential sits in a string that continues past the text this call carries")
    if kind == "comment":
        return text[:start] + placeholder + text[end:]
    if literal is None:
        # No string this line opens holds it. It may be unquoted code, or the
        # middle of a string or comment that began lines earlier; which one
        # cannot be told from here, and a lookup in the wrong one is either
        # literal text or code that does not parse.
        if bare_lookup:
            return text[:start] + lookup + text[end:]
        raise _CannotSubstitute("the credential does not sit in a string that can be found on its line")
    open_at, close_at, quote, prefix = literal
    inner_start = open_at + len(quote)
    before, after = text[inner_start:start], text[end:close_at]
    if quote == "`" and flavour in ("typescript", "javascript"):
        return text[:start] + "${" + lookup + "}" + text[end:]
    literal_start = open_at - len(prefix)
    literal_end = close_at + len(quote)
    if not before.strip() and not after.strip():
        return text[:literal_start] + lookup + text[literal_end:]
    joiner = " . " if flavour == "php" else " + "
    parts = []
    if before:
        parts.append(prefix + quote + before + quote)
    parts.append(lookup)
    if after:
        parts.append(prefix + quote + after + quote)
    return text[:literal_start] + joiner.join(parts) + text[literal_end:]


def _replace_secrets(text: str, flavour: str, bare_lookup: bool = False) -> Tuple[str, List[str], bool, str]:
    """Every credential in the text replaced by a lookup. (text, names, clean afterwards, why not)"""
    current = text
    names: List[str] = []
    for _ in range(16):
        spans = _secret_spans(current)
        if not spans:
            break
        for start, end, label in reversed(spans):
            name = _environment_name(current, start, label)
            try:
                current = _substitute(current, start, end, name, flavour, bare_lookup)
            except _CannotSubstitute as why:
                return text, names, False, str(why)
            names.append(name)
    if _secret_spans(current):
        return text, names, False, "not every credential could be replaced by a lookup"
    # The scanner knows a key only by its header. What the rewrite left of a
    # body is found by comparing against the original, not by scanning again.
    if any(run in current for run in _key_material(text)):
        return text, names, False, "part of a private key's body would be left behind"
    return current, names, True, ""


def _python_lookups(content: str) -> Optional[int]:
    """How many `os.environ[...]` reads the code holds, or None when it does not parse.

    The lookup is placed by reading quotes on one line, and a quote inside a
    string that began lines earlier fools that. Counting the reads the parser
    sees tells a lookup that became code from one that became text.
    """
    tree = _parse_python(content)
    if tree is None:
        return None
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "environ"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "os"
    )


def _ensure_import_os(content: str) -> Tuple[str, bool]:
    """Adds `import os` where a lookup needs it, after the docstring and any __future__ import."""
    tree = _parse_python(content)
    after = 0
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name == "os" and not alias.asname for alias in node.names):
                return content, False
        body = tree.body
        docstring = bool(body) and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str)
        for position, node in enumerate(body):
            if position == 0 and docstring:
                after = node.end_lineno or after
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                after = node.end_lineno or after
                continue
            break
    elif re.search(r"(?m)^[ \t]*import[ \t]+os\b", content):
        return content, False
    lines = content.splitlines(keepends=True)
    head = "".join(lines[:after])
    if head and not head.endswith("\n"):
        head += "\n"
    return head + "import os\n" + "".join(lines[after:]), True


def _command_key(arguments: Dict[str, Any], command: Any) -> Optional[str]:
    for key in COMMAND_KEYS:
        if arguments.get(key) is command or arguments.get(key) == command:
            return key
    return None


def _credential_fix(
    invocation: ToolInvocation,
    rules: List[Dict[str, Any]],
    diagnosis: _Diagnosis,
    max_content_chars: int = MAX_CONTENT_CHARS,
) -> _Fix:
    arguments = invocation.arguments if isinstance(invocation.arguments, dict) else {}
    label = diagnosis.label or "credential"
    command = shell_command(invocation)
    sites: List[_Site] = []
    covered: List[Any] = []
    if command is not None:
        analysis = analysed(command, command_cwd(invocation))
        sites = [
            _Site(write.target, write.content, "partial" if _adds_to_file(write, command) else "write")
            for write in analysis.writes
            if write.target and not write.pattern and not write.deletes and write.content is not None and _has_secret(write.content)
        ]
        if not sites:
            return _command_credential_fix(invocation, rules, command, label)
        covered = [command] if isinstance(command, str) else list(command)
        text = command if isinstance(command, str) else " ".join(str(word) for word in command)
        # The Writes replace the command; if the command also carried the
        # credential somewhere else, running "the rest" would leak it.
        stray_in_command = len(_secret_spans(text)) > sum(len(_secret_spans(site.content or "")) for site in sites)
    else:
        stray_in_command = False
        sites = [site for site in _tool_sites(arguments) if _has_secret(site.content) or _has_secret(site.old_string)]
        covered = [site.content for site in sites]

    notes: List[str] = []
    steps: List[str] = []
    fixed: List[_Site] = []
    names: List[str] = []
    for site in sites:
        if _has_secret(site.old_string):
            notes.append(
                f"The Edit's old_string for {site.path} quotes the credential itself, so any Edit that removes it is "
                "refused too: ask a person to take it out of the file, then rotate it."
            )
            continue
        flavour = _flavour(site.path)
        new_content, found_names, clean, why = _replace_secrets(site.content or "", flavour)
        if not clean and flavour == "python" and site.shape == "write":
            # A whole Python file can be parsed, so there a secret outside any
            # string may become the lookup itself, provided the result parses:
            # the count below then says whether the lookup landed in code.
            # Nowhere else can that be checked, so nowhere else is it tried.
            bare = _replace_secrets(site.content or "", flavour, bare_lookup=True)
            if bare[2] and _python_lookups(bare[0]) is not None:
                new_content, found_names, clean, why = bare
        if clean and flavour == "python":
            before, after = _python_lookups(site.content or ""), _python_lookups(new_content)
            if after is not None and after - (before or 0) < len(found_names):
                clean, why = False, "the credential sits inside a longer string, where a lookup would be text rather than code"
        if not clean:
            variable = (found_names or [_DEFAULT_ENVIRONMENT_NAMES.get(label, "API_KEY")])[0]
            notes.append(
                f"In {site.path}, {why}, so no lookup can replace it here: remove the whole value by hand and read it "
                f"from {variable} in the environment."
            )
            continue
        names.extend(found_names)
        if flavour == "python" and "os.environ[" in new_content:
            if site.shape == "write":
                new_content, added = _ensure_import_os(new_content)
                if added:
                    steps.append(f"`import os` is added to {site.path} for the lookup.")
            else:
                steps.append(f"Make sure `import os` is at the top of {site.path} for the lookup.")
        # A whole Python file that parsed as sent must parse as proposed.
        must_parse = flavour == "python" and site.shape == "write" and _parses(site.path, site.content or "")
        fixed.append(_Site(site.path, new_content, site.shape, site.old_string, must_parse=must_parse))
    stray = [
        leaf
        for leaf in iter_string_leaves(arguments)
        if _has_secret(leaf) and leaf not in covered and not any(leaf == site.old_string for site in sites)
    ]
    if stray:
        notes.append("A credential also sits elsewhere in the call's arguments, where no rewrite here can reach it: take it out of the call.")
    if stray_in_command:
        notes.append("The command also carries the credential outside the file it writes: take it out of the command as well.")

    names = list(dict.fromkeys(names))
    variable = names[0] if names else _DEFAULT_ENVIRONMENT_NAMES.get(label, "API_KEY")
    outcome = _fix_sites(fixed, rules, max_content_chars) if fixed else _Outcome(ok=False)
    lead = []
    for site in fixed:
        lead.append(
            f"Read the credential ({label}) from the environment variable {', '.join(names) or variable} in {site.path} "
            "instead of writing it into the file; the proposed content does this."
        )
    closing = [
        f"Set {', '.join(names) or variable} in the environment the code runs in (a secret store or your own shell), never in the repository.",
        "If the credential was ever committed or shared, rotate it.",
    ]
    if command is not None and fixed:
        closing.insert(0, "Make these writes with Write instead of the shell command, so the rules read them before they land.")
    validated = outcome.ok and not notes and bool(outcome.writes) and all(check["passed"] for check in outcome.checks)
    all_steps = lead + steps + outcome.steps + notes + closing
    if validated:
        all_steps.append("Threefold ran the credential scan and the same gates on every proposed write, and each passed.")
        where = _short(fixed[0].path)
        summary = f"Checked fix: read {variable} from the environment in {where} instead of the literal {label} credential."
        return _Fix(KIND_CREDENTIAL, summary, all_steps, outcome.writes, True, outcome.checks)
    why = notes[0] if notes else (outcome.why or "the rewrite did not pass the gates")
    return _Fix(KIND_CREDENTIAL, f"No checked fix for the {label}: {why}", all_steps, [], False, outcome.checks)


def _script_word(words: Sequence[str]) -> Tuple[Optional[int], str]:
    """Which word of an argv a shell runs as its script (`bash -lc '<script>'`), and in which flavour."""
    if not words:
        return None, ""
    program = posixpath.basename(str(words[0]).replace("\\", "/")).lower()
    if program.endswith(".exe"):
        program = program[:-4]
    if program in _POSIX_SHELLS:
        index = 1
        while index < len(words) - 1:
            word = str(words[index])
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", word):
                return index + 1, "shell"
            if word in ("-o", "+o"):
                index += 2
                continue
            if not word.startswith(("-", "+")) or word == "--":
                break
            index += 1
        return None, ""
    if program in _POWERSHELLS:
        for index in range(1, len(words) - 1):
            if str(words[index]).lower() in ("-command", "-c"):
                return index + 1, "powershell"
    return None, ""


def _command_credential_fix(invocation: ToolInvocation, rules: List[Dict[str, Any]], command: Any, label: str) -> _Fix:
    """A command carrying a credential: the same command reading it from the environment.

    Validated only where the rewritten command still expands the variable at
    the place the secret was. A list of words is run without a shell, so
    nothing in it expands, unless it is itself a shell running a script; a
    quoted heredoc and a single-quoted PowerShell here-string expand nothing;
    PowerShell reads the environment as $env:NAME, not ${NAME}.
    """
    arguments = dict(invocation.arguments)
    key = _command_key(arguments, command)
    powershell_tool = str(invocation.tool_name or "").lower() == "powershell"
    flavour = "powershell" if powershell_tool else "shell"
    names: List[str] = []
    why = ""
    script: Optional[int] = None
    if isinstance(command, str):
        rewritten: Any
        rewritten, names, clean, why = _replace_secrets(command, flavour)
    else:
        words = list(command)
        script, script_flavour = _script_word([str(word) for word in words])
        clean = True
        for position, word in enumerate(words):
            if not isinstance(word, str) or not _secret_spans(word):
                continue
            if position != script:
                clean = False
                why = (
                    "the command is a list of words run without a shell, so no variable written into it would ever "
                    "be expanded"
                )
                break
            flavour = script_flavour
            new_word, found, word_clean, word_why = _replace_secrets(word, script_flavour)
            names.extend(found)
            if not word_clean:
                clean, why = False, word_why
                break
            words[position] = new_word
        rewritten = words
    names = list(dict.fromkeys(names)) or [_DEFAULT_ENVIRONMENT_NAMES.get(label, "API_KEY")]
    variable = names[0]
    reference = f"$env:{variable}" if flavour == "powershell" else f"${variable}"
    closing = [
        f"Export {', '.join(names)} in your own shell, outside the agent, so the value never passes through the agent.",
        "If the credential was ever committed or shared, rotate it.",
    ]
    if key is None or not clean:
        advice = [f"Take the {label} out of the command: {why or 'no environment reference could take its place there'}."]
        if not isinstance(command, str):
            advice.append(
                f"Run it through a shell instead (sh -c with \"${variable}\" in the script), or have the program read "
                f"{variable} from the environment itself."
            )
        else:
            advice.append(f"Rewrite the command so that the value is read from the environment as {reference} where the shell expands it.")
        return _Fix(
            KIND_CREDENTIAL,
            f"No checked fix: take the {label} out of the command and read it from the environment as {reference}.",
            advice + closing,
        )
    arguments[key] = rewritten
    stray = [leaf for leaf in iter_string_leaves(arguments) if _has_secret(leaf)]
    allowed, _ = ArchitecturalBoundaryGuard.evaluate_tool_boundary(
        ToolInvocation(tool_name=invocation.tool_name, action_type=invocation.action_type, arguments=arguments), rules=rules
    )
    checks = [
        {"gate": GATE_CREDENTIAL, "path": "(command)", "passed": not stray},
        {"gate": GATE_BOUNDARY, "path": "(command)", "passed": allowed},
    ]
    if isinstance(rewritten, str):
        text, shown_as = rewritten, "Run instead"
    elif script is not None and script < len(rewritten):
        # The script alone reads far better than the shell quoting of a whole
        # argv around it, and it is the one word that changed.
        text, shown_as = str(rewritten[script]), f"Pass this as the script {posixpath.basename(str(rewritten[0]))} runs"
    else:
        text, shown_as = shlex.join(str(word) for word in rewritten), "Run instead"
    steps = [f"Refer to the {label} as {reference} in the command instead of writing it out."]
    if "\n" in text or "\r" in text:
        # A step is one line; a heredoc flattened onto one line is a different
        # command, so a command that spans lines is described, not repeated.
        steps.append(f"The command spans several lines, so it is not repeated here: replace the literal with {reference} where it sits.")
    elif len(text) <= MAX_COMMAND_IN_STEP:
        steps.append(f"{shown_as}: {text}")
    steps.extend(closing)
    if not stray and allowed:
        steps.append("Threefold ran the credential scan and the same gates on the rewritten command, and it passed.")
        return _Fix(
            KIND_CREDENTIAL,
            f"Checked fix: use {reference} from the environment in the command instead of the literal {label} credential.",
            steps,
            [],
            True,
            checks,
        )
    return _Fix(
        KIND_CREDENTIAL,
        f"No checked fix: the command still fails a gate once the {label} is replaced by {reference}.",
        steps,
        [],
        False,
        checks,
    )


# --- advice in words: protected paths, destructive commands, loops, budget -------------


def _protected_fix(diagnosis: _Diagnosis) -> _Fix:
    """No code fix exists for these. The governed way to get the change made does."""
    path = _short(diagnosis.path or "")
    installer = "hooks change only through threefold_install.py"
    if diagnosis.why == "tampering":
        what = diagnosis.detail or "the command turns the repository's hooks off"
        return _Fix(
            KIND_PROTECTED_PATH,
            f"No code fix: {what}. Run it without that and fix what the check reports; {installer}.",
            [
                f"The command is refused as a protected-path call: {what}.",
                "Run the same command without --no-verify or core.hooksPath, and fix whatever the pre-commit check reports.",
                f"If the hooks themselves need to change, ask the operator: {installer} (connect or disconnect).",
            ],
        )
    # `.git/hooks/pre-commit` trips the `.git` pattern before the hooks check is
    # reached; the advice that helps is still the one about hooks.
    if diagnosis.why == "hooks" or (diagnosis.path and is_governance_path(diagnosis.path)):
        return _Fix(
            KIND_PROTECTED_PATH,
            f"No code fix: {path} decides whether the agent's hooks run. Ask the operator; {installer}.",
            [
                f"{path} decides whether the agent's hooks run, so no agent call may write or remove it.",
                f"Ask the operator for the change: {installer} (connect, disconnect or status), which the operator runs.",
            ],
        )
    lowered = (diagnosis.path or diagnosis.detail or "").lower()
    if ".env" in lowered:
        advice = "Read the setting from the process environment instead of the .env file; a person changes .env."
    elif ".git" in lowered:
        advice = "Use git commands for repository state; nothing under .git is edited directly."
    elif "frozen_" in lowered:
        advice = "This domain core file is frozen: ask the owner to change it."
    else:
        advice = "Keys and credentials are provisioned by the operator, never written or read by the agent: ask the operator."
    target = path or _short(diagnosis.detail or "a protected path")
    return _Fix(
        KIND_PROTECTED_PATH,
        f"No code fix: {target} is protected. {advice}",
        [f"{target} is protected by architectural governance, so the call is refused whatever it contains.", advice],
    )


def _destructive_fix(diagnosis: _Diagnosis) -> _Fix:
    detail = (diagnosis.detail or "").lower()
    if "push" in detail:
        advice = "Push without force; if history really must be rewritten, a person does it."
    elif "drop" in detail:
        advice = "A person drops databases; the agent may write the migration for them to review."
    elif "format" in detail:
        advice = "A person formats drives."
    else:
        advice = "Delete the specific files or directories you mean, inside the project, by name."
    return _Fix(
        KIND_DESTRUCTIVE,
        f"No code fix: the command contains a destructive operation. {advice}",
        ["The command contains a destructive operation, which is refused whatever else it does.", advice],
    )


def _loop_fix(request: Any, result: Any) -> _Fix:
    """What was repeated and how often; never validated, because no write would fix a loop."""
    reason = _effective_reason(result)
    arguments = _field(request, "arguments") or {}
    target = describe_target(
        SimpleNamespace(action_type=str(getattr(_field(request, "action_type"), "value", _field(request, "action_type")) or ""), arguments=arguments if isinstance(arguments, dict) else {})
    )
    tool = str(_field(request, "tool_name") or "the tool")[:60]
    # The count and the cycle are in the loop detector's sentence and nowhere
    # else in a verdict. Read from it when it says them; when it does not, the
    # fix says "repeatedly" rather than a number it made up.
    count_match = re.search(r"(\d+) consecutive times", reason) or re.search(r"for the (\d+)\w* time", reason)
    times = f"{int(count_match.group(1))} times" if count_match else "repeatedly"
    cycle = re.search(r"cycle \(([^)]{1,200})\)", reason)
    if cycle:
        what = f"the cycle {cycle.group(1)}"
        how = f"{what} was repeated {times}"
    else:
        what = f"{tool} on {_short(target, 50)}" if target else tool
        how = f"{what} was called {times} with identical arguments"
    halted = bool(_field(result, "session_tripped"))
    session = str(_field(result, "session_id") or _field(request, "session_id") or "")[:64]
    steps = [
        f"{how[:1].upper()}{how[1:]}, and nothing changed between the calls.",
        "Read the result of the last call before calling again; if it failed, change the arguments or the approach instead of retrying.",
        "If you are waiting on something (CI, a build), poll with a read-only command such as `gh run view` or `git status`, which Threefold records rather than refuses.",
    ]
    if halted:
        steps.append(f"The session is halted. After the loop is understood, an operator resumes it with POST /sessions/{session or '<session>'}/resume.")
        tail = "The session is halted; an operator resumes it."
    else:
        steps.append("The session was not halted: the next different call is judged normally.")
        tail = "The next different call is judged normally."
    return _Fix(KIND_LOOP, f"Loop: {how}. Change the arguments or the approach, or ask the human. {tail}", steps)


def _budget_fix(request: Any, result: Any) -> _Fix:
    """The cost gate: one call over the per-call cap, or a session over its budget. Both halt it."""
    session = str(_field(result, "session_id") or _field(request, "session_id") or "")[:64]
    resume = (
        f"The cost gate halted the session; an operator resumes it with POST /sessions/{session or '<session>'}/resume "
        "once the spend is understood."
    )
    # The circuit breaker's two sentences are the only thing in a verdict that
    # tells the per-call cap from the session budget; neither is a field. A
    # sentence that is neither gets advice that covers both, not a guess.
    reason = _effective_reason(result)
    if not reason.startswith(("Single invocation cost", "Projected session cost")):
        return _Fix(
            KIND_BUDGET,
            "No code fix: the cost gate refused this call. Split the work into smaller calls, or ask the operator about the budget.",
            [
                "The cost gate refused the call: either its declared cost is over the per-call cap, or the session's budget is spent.",
                "Split the work into smaller calls, or ask the operator to raise max_single_call_usd or budget_usd.",
                resume,
            ],
        )
    if reason.startswith("Single invocation cost"):
        return _Fix(
            KIND_BUDGET,
            "No code fix: this one call declares more cost than the per-call cap. Split the work into smaller calls, or ask the operator.",
            [
                "The call's declared token usage costs more than the per-call safety cap allows.",
                "Split the work into smaller calls, or ask the operator to raise max_single_call_usd in the policy.",
                resume,
            ],
        )
    spent = _field(result, "current_session_cost_usd")
    budget = _field(request, "budget_usd")
    try:
        detail = f" (${float(spent):.2f} of ${float(budget):.2f})"
    except (TypeError, ValueError):
        detail = ""
    return _Fix(
        KIND_BUDGET,
        f"No code fix: this session has spent its budget{detail}. Ask the operator to raise it, or start a new session.",
        [
            f"The session's spend reached its budget{detail}.",
            "Ask the operator to raise budget_usd for this work, or start a new session for the next task.",
            resume,
        ],
    )


def _halted_fix(request: Any, result: Any) -> _Fix:
    reason = _effective_reason(result)
    cause = reason.split(": ", 1)[1] if ": " in reason else reason
    session = str(_field(result, "session_id") or _field(request, "session_id") or "")[:64]
    return _Fix(
        KIND_HALTED,
        "No code fix: this session is halted. An operator resumes it after reading why it halted.",
        [
            f"The session was halted earlier: {cause[:160]}",
            f"An operator resumes it with POST /sessions/{session or '<session>'}/resume once the cause is understood; until then every call is refused.",
        ],
    )


# --- the answer as it leaves --------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _one_line(text: Any, limit: int) -> str:
    """One line, redacted, printable, at most `limit` characters."""
    cleaned = redact_secrets(shell_display(_CONTROL.sub(" ", str(text or ""))))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3].rstrip() + "..."
    return cleaned


def _scrub(text: Any, withheld: Sequence[str]) -> str:
    """The text with every withheld secret taken out, before it is flattened to one line."""
    text = str(text or "")
    for secret in withheld:
        if secret in text:
            text = text.replace(secret, "[REDACTED]")
    return text


def _finish(fix: _Fix, max_write_bytes: int, phrase: Optional[Callable[[Dict[str, Any]], Optional[str]]]) -> Dict[str, Any]:
    withheld = sorted((text for text in fix.withheld if len(text) >= 8), key=len, reverse=True)
    steps = [_one_line(_scrub(step, withheld), MAX_STEP_CHARS) for step in fix.steps if step]
    if len(steps) > MAX_STEPS:
        head = MAX_STEPS - KEPT_TAIL_STEPS - 1
        omitted = len(steps) - head - KEPT_TAIL_STEPS
        steps = steps[:head] + [f"{omitted} further step(s) are left out here to keep this short."] + steps[-KEPT_TAIL_STEPS:]
    writes = [dict(write) for write in fix.writes]
    validated = bool(fix.validated) and bool(fix.checks) and all(check.get("passed") for check in fix.checks if check.get("gate") != GATE_ROUTE)
    # Belt and braces: a write that still carries a credential is never handed
    # out, whatever the code above believed about it. Two tests, because the
    # scanner alone missed a key's body: it matches only the header.
    if any(
        _has_secret(write.get("content"))
        or _has_secret(write.get("old_string"))
        or any(secret in str(write.get(part) or "") for part in ("content", "old_string") for secret in withheld)
        for write in writes
    ):
        writes = []
        validated = False
        steps.append("The proposed files were withheld because a credential was still found in them.")
    size = sum(
        len(str(write.get("path", "")).encode("utf-8"))
        + len(str(write.get("content", "")).encode("utf-8"))
        + len(str(write.get("old_string") or "").encode("utf-8"))
        for write in writes
    )
    summary = _scrub(fix.summary, withheld)
    include_writes = size <= max_write_bytes
    if not include_writes:
        steps.append(
            f"The proposed files come to {size} bytes, more than the {max_write_bytes} a verdict carries, so they are "
            "not included here; each was checked in full, and the steps above say what they hold."
        )
        suffix = f" Files over {max_write_bytes // 1024} KB are not included."
        summary = _one_line(summary, MAX_SUMMARY_CHARS - len(suffix)) + suffix
    out: Dict[str, Any] = {"kind": fix.kind, "summary": _one_line(summary, MAX_SUMMARY_CHARS), "steps": steps}
    if include_writes:
        out["writes"] = writes
    out["validated"] = validated
    out["checks"] = [
        {"gate": str(check.get("gate")), "path": _one_line(_scrub(check.get("path"), withheld), 300), "passed": bool(check.get("passed"))}
        for check in fix.checks
    ]
    if phrase is not None:
        try:
            worded = phrase(dict(out))
        except Exception as exc:  # pragma: no cover - the deterministic summary stands
            logger.warning("The phrasing hook failed; keeping the deterministic summary: %s", exc)
            worded = None
        if isinstance(worded, str) and worded.strip():
            out["summary"] = _one_line(_scrub(worded, withheld), MAX_SUMMARY_CHARS)
    return out
