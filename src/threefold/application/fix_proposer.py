"""A concrete fix for a refusal, checked against the same gates before it is offered.

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
- writes: [{path, content}], with `old_string` for an Edit retry and `partial`
  for shell-added text. Left out when the files come to more than 8 KB, and the
  steps say so; they were still checked in full.
- validated: true only when every proposed write was run through the gates and
  passed. Never true for advice in words, for a loop, or for content Threefold
  could not see.
- checks: [{gate, path, passed}] for every check that was run.

Which fix a refusal gets is decided by the verdict's family (its
rule_evaluations, or a rule_key when the response carries one) and then by
asking the questions the boundary guard asks, in the guard's order, of the call
itself. Reason strings are prose and change; the gates are the contract.

A detected secret is never repeated. Fixes replace it with an environment lookup,
and every string that leaves this module is passed through the redaction the
ledger uses.
"""
from __future__ import annotations

import ast
import keyword
import logging
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from threefold.domain import imports as import_readers
from threefold.domain.boundary_guard import (
    COMMAND_KEYS,
    CONTENT_KEYS,
    PATH_KEYS,
    REMOVED_KEYS,
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
    observe_layering,
    redact_secrets,
    shell_command,
    shell_observations,
    shell_refusal,
    write_pairs,
)
from threefold.domain.imports import declared_imports, language_for
from threefold.domain.layering_rules import (
    DEFAULT_RULES,
    ENFORCE,
    evaluate as evaluate_layering,
    normalise_rules,
    rules_for_path,
    violations,
)
from threefold.domain.models import ToolActionType, ToolInvocation
from threefold.domain.path_match import matches
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
MAX_STEPS = 14
# Content larger than this is not rewritten. The gate reads it, but a fix that
# re-parses and re-emits half a megabyte on every refusal is a cost nobody asked
# for, and the 8 KB cap would leave it out of the answer anyway.
MAX_CONTENT_CHARS = 512_000
MAX_METHODS = 8
MAX_REMOVAL_ROUNDS = 64
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

    Never raises: a fault here must not turn a refusal into a 500.
    """
    try:
        active = normalise_rules(rules) if rules else list(DEFAULT_RULES if rules is None else [])
        fix = _propose(request, result, active)
        if fix is None:
            return None
        return _finish(fix, max_write_bytes, phrase)
    except Exception as exc:  # pragma: no cover - the refusal stands without a fix
        logger.warning("Could not propose a fix; the refusal stands on its own: %s", exc)
        return None


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


def _propose(request: Any, result: Any, rules: List[Dict[str, Any]]) -> Optional[_Fix]:
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
    diagnosis = _diagnose(invocation, rules) or _diagnose_observed(invocation, rules)
    if diagnosis is None:
        # The gates, asked again with the rules in force, find nothing: the
        # rules changed since, or the refusal came from somewhere this module
        # does not know. Guessing would be the noise this module exists to avoid.
        return None
    if diagnosis.kind == KIND_CREDENTIAL:
        return _credential_fix(invocation, rules, diagnosis)
    if diagnosis.kind == KIND_PROTECTED_PATH:
        return _protected_fix(diagnosis)
    if diagnosis.kind == KIND_DESTRUCTIVE:
        return _destructive_fix(diagnosis)
    return _write_fix(invocation, rules, diagnosis)


# --- asking the guard's questions again ---------------------------------------------


def _enforcing(rules: List[Dict[str, Any]]) -> bool:
    return any(rule.get("mode", ENFORCE) == ENFORCE for rule in rules)


def _diagnose(invocation: ToolInvocation, rules: List[Dict[str, Any]]) -> Optional[_Diagnosis]:
    """The first check in evaluate_tool_boundary that refuses this call, and what it saw.

    The order is the guard's own, so the kind always names what actually
    refused. A test holds the two together over every refusal in the suite.
    """
    arguments = invocation.arguments if isinstance(invocation.arguments, dict) else {}

    clean, message = SecretScanner.scan_arguments(arguments)
    if not clean:
        return _Diagnosis(KIND_CREDENTIAL, label=message.rsplit(": ", 1)[-1])

    path_like = [leaf for leaf in iter_string_leaves(arguments) if looks_like_path(leaf)]
    for candidate in path_like:
        if ArchitecturalBoundaryGuard.is_forbidden_file_access(candidate):
            return _Diagnosis(KIND_PROTECTED_PATH, why="protected", path=candidate)

    for target in governed_write_targets(invocation):
        if is_governance_path(target):
            return _Diagnosis(KIND_PROTECTED_PATH, why="hooks", path=target)

    command = shell_command(invocation)
    if command is not None:
        analysis = analysed(command, command_cwd(invocation))
        if shell_refusal(analysis, rules):
            return _shell_diagnosis(analysis, rules)

    for target, content in write_pairs(arguments):
        if rules_for_path(target, rules) and not evaluate_layering(target, content, rules)[0]:
            return _Diagnosis(KIND_LAYERING, path=target)

    if invocation.action_type == ToolActionType.COMMAND_EXEC or command is not None or not path_like:
        for leaf in iter_string_leaves(arguments):
            for pattern in ArchitecturalBoundaryGuard.DESTRUCTIVE_COMMANDS:
                found = pattern.search(leaf)
                if found:
                    return _Diagnosis(KIND_DESTRUCTIVE, detail=found.group(0))
        for leaf in iter_string_leaves(arguments):
            if looks_like_path(leaf):
                continue
            for pattern in ArchitecturalBoundaryGuard.PROTECTED_PATH_PATTERNS:
                found = pattern.search(leaf)
                if found:
                    return _Diagnosis(KIND_PROTECTED_PATH, why="command", detail=found.group(0))
    return None


def _shell_diagnosis(analysis: ShellAnalysis, rules: List[Dict[str, Any]]) -> _Diagnosis:
    """Which of a command's writes shell_refusal refused, in shell_refusal's order."""
    if analysis.tampering:
        return _Diagnosis(KIND_PROTECTED_PATH, why="tampering", detail=analysis.tampering[0], analysis=analysis)
    for write in analysis.writes:
        if write.target is None:
            continue
        if write.pattern:
            if pattern_is_governance(write.target, write.deletes, write.tree):
                return _Diagnosis(KIND_PROTECTED_PATH, why="hooks", path=shell_display(write.target), analysis=analysis)
        elif is_governance_path(write.target, deletes=write.deletes):
            return _Diagnosis(KIND_PROTECTED_PATH, why="hooks", path=write.target, analysis=analysis)
    if analysis.truncated and _enforcing(rules):
        return _Diagnosis(KIND_UNREADABLE, why="truncated", analysis=analysis)
    for write in analysis.writes:
        reason = shell_refusal(ShellAnalysis(writes=[write]), rules)
        if reason:
            kind = KIND_UNREADABLE if UNREADABLE_WRITE in reason else KIND_LAYERING
            return _Diagnosis(kind, path=shell_display(write.target or ""), analysis=analysis)
    return _Diagnosis(KIND_UNREADABLE, why="unknown", analysis=analysis)


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
                if isinstance(value, str) and isinstance(key, str) and key.lower() in PATH_KEYS and looks_like_path(value):
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


def _check_entry(entry: Dict[str, Any], rules: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    """Runs one proposed write through the gates. Returns the checks and why one failed.

    Three questions: does any layering rule, enforcing or watching, flag it; does
    the credential scanner find anything in it; and does the whole boundary
    guard, the check the evaluator runs first, let the call through. The first
    is stricter than the gate on purpose: a fix a watching rule would flag is a
    fix the dashboard would list as a would-refuse the day after.
    """
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


def _parse_python(content: str) -> Optional[ast.Module]:
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


def _python_removal(content: str, module: str) -> Tuple[str, List[_Removed]]:
    tree = _parse_python(content)
    if tree is None:
        return _python_line_removal(content, module)
    lines = content.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def offset(lineno: int, col: int) -> int:
        line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        return starts[min(lineno - 1, len(lines))] + _char_offset(line, col)

    parents: Dict[int, list] = {}
    for node in ast.walk(tree):
        for name in ("body", "orelse", "finalbody"):
            block = getattr(node, name, None)
            if isinstance(block, list):
                for child in block:
                    parents[id(child)] = block

    edits: List[List[Any]] = []
    removed: List[_Removed] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits = [alias for alias in node.names if alias.name == module]
            if not hits:
                continue
            keep = [alias for alias in node.names if alias.name != module]
            replacement = ("import " + ", ".join(_alias_text(alias) for alias in keep)) if keep else None
            removed.append(
                _Removed(
                    module,
                    "import " + ", ".join(_alias_text(alias) for alias in hits),
                    [alias.asname or alias.name for alias in hits],
                )
            )
        elif isinstance(node, ast.ImportFrom) and node.module == module:
            replacement = None
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


def _python_line_removal(content: str, module: str) -> Tuple[str, List[_Removed]]:
    """The line reader's statements, for content that does not parse (it arrives mid-edit)."""
    masked = _mask(content, "python")
    edits: List[List[Any]] = []
    removed: List[_Removed] = []
    for match in import_readers._PYTHON_FALLBACK.finditer(masked):
        keyword_start = match.start() + len(match.group(0)) - len(match.group(0).lstrip(" \t"))
        line_end = masked.find("\n", match.start())
        line_end = len(masked) if line_end == -1 else line_end
        if match.group("from") is not None:
            if match.group("from").lstrip(".") != module:
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
        hits = [part for part in parts if part.split(" ")[0].strip("()").lstrip(".") == module]
        if not hits:
            continue
        keep = [part for part in parts if part not in hits]
        replacement = ("import " + ", ".join(keep)) if keep else None
        bindings = [part.split(" as ")[-1].strip() if " as " in part else part.split(" ")[0] for part in hits]
        removed.append(_Removed(module, "import " + ", ".join(hits), bindings))
        edits.append([keyword_start, match.end(), replacement])
    return _apply_edits(content, edits), removed


def _pattern_removal(content: str, module: str, language: str) -> Tuple[str, List[_Removed]]:
    """Java and C# statements, found by the reader's own patterns."""
    pattern = import_readers._JAVA if language == "java" else import_readers._CSHARP
    masked = _mask(content, language)
    edits: List[List[Any]] = []
    removed: List[_Removed] = []
    for match in pattern.finditer(masked):
        if match.group("module") != module:
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


def _ts_removal(content: str, module: str) -> Tuple[str, List[_Removed]]:
    masked = _mask(content, "typescript")
    edits: List[List[Any]] = []
    removed: List[_Removed] = []
    taken = set()
    for pattern in (import_readers._TS_FROM, import_readers._TS_BARE):
        for match in pattern.finditer(masked):
            if match.group("module") != module:
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
        if match.group("module") != module:
            continue
        start, end, bindings = _ts_call_statement(masked, match)
        if start in taken:
            continue
        taken.add(start)
        removed.append(_Removed(module, content[start:end].strip(), bindings, literal=module))
        edits.append([start, end, None])
    return _apply_edits(content, edits), removed


def _remove_module(path: str, content: str, module: str) -> Tuple[str, List[_Removed]]:
    """Takes every statement importing `module` out of the content, or changes nothing.

    The reader is asked again afterwards. If it still sees the module, what was
    taken out was not what the gate reads, and nothing is reported as removed.
    """
    language = language_for(path)
    try:
        if language == "python":
            new, removed = _python_removal(content, module)
        elif language in ("java", "csharp"):
            new, removed = _pattern_removal(content, module, language)
        elif language == "typescript":
            new, removed = _ts_removal(content, module)
        else:
            return content, []
    except _CannotRemove:
        return content, []
    if not removed:
        return content, []
    if module in declared_imports(path, new)[1]:
        return content, []
    return new, removed


def _remove_offending(
    path: str, content: str, rules: List[Dict[str, Any]]
) -> Tuple[str, List[_Removed], List[str], str]:
    """Removes every import any rule flags, enforcing or watching. (content, removed, rule ids, why not)

    violations() reports one import per rule, which is enough to refuse a call
    and not enough to fix one: a file with three forbidden imports would come
    back with two still in it. So the question is asked again after each
    removal until nothing is flagged or nothing more can be taken out.
    """
    current = content
    removed: List[_Removed] = []
    rule_ids: List[str] = []
    for _ in range(MAX_REMOVAL_ROUNDS):
        found, _ = violations(path, current, rules)
        if not found:
            return current, removed, rule_ids, ""
        progressed = False
        for item in found:
            new, taken = _remove_module(path, current, item["module"])
            if not taken:
                continue
            for statement in taken:
                statement.rule_id = item["rule_id"]
            removed.extend(taken)
            rule_ids.append(item["rule_id"])
            current = new
            progressed = True
        if not progressed:
            return current, removed, rule_ids, (
                f"the statement in '{path}' that imports '{found[0]['module']}' could not be taken out "
                "without breaking the code around it"
            )
    return current, removed, rule_ids, f"'{path}' imports more forbidden modules than one fix can move"


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
    for binding in bindings:
        escaped = re.escape(binding)
        # Not after `@`: `@Table(name = "x")` is an annotation, not something
        # the domain asks the outside world to do.
        for match in re.finditer(r"(?<![\w$.@])" + escaped + r"\s*\.\s*([A-Za-z_$][\w$]*)", masked):
            name = _method_name(match.group(1), language)
            if name and name not in seen:
                seen.add(name)
                found.append((name, f"{binding}.{match.group(1)}"))
        if re.search(r"(?<![\w$.@])(?<!new )" + escaped + r"\s*\(", masked):
            name = _method_name(binding, language)
            if name and name not in seen:
                seen.add(name)
                found.append((name, binding))
    if not found:
        return [("Perform" if language == "csharp" else "perform", None)]
    return found[:MAX_METHODS]


def _uses(content: str, bindings: Sequence[str], language: str) -> int:
    masked = _mask(content, language)
    return sum(len(re.findall(r"(?<![\w$.])" + re.escape(binding) + r"(?![\w$])", masked)) for binding in bindings)


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
                return statement.text.replace(old, f"{quote}{rebased}{quote}", 1)
    return statement.text


def _libraries(removed: Sequence[_Removed]) -> str:
    names = list(dict.fromkeys(statement.module.rstrip(".") for statement in removed))
    if len(names) > 3:
        return ", ".join(names[:3]) + f" and {len(names) - 3} more"
    return ", ".join(names)


# --- the text of a port and an adapter, per language ------------------------------------


def _python_port(port: str, methods: Sequence[Tuple[str, Optional[str]]], libraries: str, stem: str) -> str:
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


def _java_port(package: str, port: str, methods: Sequence[Tuple[str, Optional[str]]], libraries: str, stem: str) -> str:
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
    if language == "python":
        lines = [
            f'"""Implements {port} from {port_path} with {libraries}, outside the domain."""',
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
                lines.append(f"        raise NotImplementedError({_quoted('Call ' + libraries + ' here')})")
        return "\n".join(lines) + "\n"
    if language == "typescript":
        module = _relative_module(_split(adapter_path)[0], _script_module_path(port_path, flavour))
        if flavour == "typescript":
            lines = [*statements, f"import type {{ {port} }} from '{module}';", ""]
            lines.append(f"/** Implements {port} with {libraries}, outside the domain. */")
            lines.append(f"export class {adapter} implements {port} {{")
            for name, target in methods:
                lines.append(f"  {name}(...args: unknown[]): unknown {{")
                lines.append(f"    {_script_call(target, libraries, typed=True)}")
                lines.append("  }")
        else:
            lines = [*statements, f"import {{ {port} }} from '{module}';", ""]
            lines.append(f"/** Implements {port} with {libraries}, outside the domain. */")
            lines.append(f"export class {adapter} extends {port} {{")
            for name, target in methods:
                lines.append(f"  {name}(...args) {{")
                lines.append(f"    {_script_call(target, libraries, typed=False)}")
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
    bindings = list(dict.fromkeys(binding for statement in removed for binding in statement.bindings))
    methods = _members("\n".join(cleaned), bindings, language)
    whole = len(group) == 1 and group[0].shape == "write"
    separate_port = language in ("java", "csharp") or not whole
    masked_original = _mask(group[0].content or "", language)
    block_style = bool(re.search(r"(?m)^[ \t]*namespace[ \t]+[\w.]+\s*\{", masked_original))

    domain_package = ""
    if language == "java":
        found = _JAVA_PACKAGE.search(masked_original)
        domain_package = found.group(1) if found else _package_from_directory(directory, "java")
    elif language == "csharp":
        found = _CSHARP_NAMESPACE.search(masked_original)
        domain_package = found.group(1) if found else _package_from_directory(directory, "csharp")

    outcome = _Outcome()
    domain_entries: List[Dict[str, Any]] = []
    if whole and not separate_port:
        if language == "python":
            content = _insert_python_port(cleaned[0], _python_port(port, methods, libraries, stem))
        else:
            content = _insert_script_port(cleaned[0], _script_port(port, methods, libraries, stem, flavour))
        domain_entries.append(_entry(group[0], content))
        port_path = path
    else:
        for site, content in zip(group, cleaned):
            domain_entries.append(_entry(site, content))
        port_path = _join(directory, _port_file_name(language, flavour, stem, base, extension))
        if language == "python":
            port_text = (
                f'"""The port {stem} depends on, declared in the domain so it never depends on {libraries}."""\n'
                "from typing import Any, Protocol\n\n\n" + _python_port(port, methods, libraries, stem)
            )
        elif language == "java":
            port_text = _java_port(domain_package, port, methods, libraries, stem)
        elif language == "csharp":
            port_text = _csharp_port(domain_package, block_style, port, methods, libraries, stem)
        else:
            port_text = _script_port(port, methods, libraries, stem, flavour)
        domain_entries.append({"path": port_path, "content": port_text})

    for entry in domain_entries:
        checks, why = _check_entry(entry, rules)
        outcome.checks.extend(checks)
        if why:
            outcome.ok = False
            outcome.why = outcome.why or f"the domain side still fails a gate: {why}"
    if not outcome.ok:
        outcome.steps.append(
            f"In {path}, remove the import of {libraries} and move it behind a port; "
            f"a mechanical rewrite was checked and still fails: {outcome.why}."
        )
        return outcome

    attempts: List[Dict[str, Any]] = []
    tried: List[str] = []
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
        entry = {"path": adapter_path, "content": text}
        checks, why = _check_entry(entry, rules)
        attempts.extend(checks)
        if why:
            continue
        outcome.writes = domain_entries + [entry]
        outcome.checks.extend(checks)
        outcome.plans.append({"path": path, "libraries": libraries, "port": port, "port_path": port_path, "adapter": adapter_path})
        _layer_steps(outcome, path, group, removed, bindings, cleaned, language, port, port_path, adapter, adapter_path, methods, flavour)
        return outcome

    outcome.ok = False
    outcome.checks.extend(attempts)
    places = ", ".join(tried) or "no directory at all"
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
) -> None:
    statements = list(dict.fromkeys(re.sub(r"\s+", " ", statement.text).strip() for statement in removed))
    rules_named = ", ".join(dict.fromkeys(f"'{statement.rule_id}'" for statement in removed if statement.rule_id))
    shown = ", ".join(f"`{text}`" for text in statements[:4]) + (f" and {len(statements) - 4} more" if len(statements) > 4 else "")
    kind = {"python": "a Protocol", "java": "an interface", "csharp": "an interface"}.get(
        language, "an interface" if flavour == "typescript" else "a class to extend"
    )
    retry = {"edit": "Retry the Edit with the new text below", "partial": "Add the text below with Edit"}.get(
        group[0].shape, "Write the domain file below"
    )
    outcome.steps.append(f"{retry}: it drops {shown} from {path}, which rule {rules_named or 'in force'} forbids there.")
    where = "in the same file" if port_path == path else f"in {port_path}, beside it in the domain"
    outcome.steps.append(f"Declare {port}, {kind}, {where}, for what the domain needs: {', '.join(name for name, _ in methods)}.")
    outcome.steps.append(f"Create {adapter_path}: {adapter} holds the import and implements {port}; the rules do not refuse it there.")
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


def _write_fix(invocation: ToolInvocation, rules: List[Dict[str, Any]], diagnosis: _Diagnosis) -> _Fix:
    command = shell_command(invocation)
    if command is not None:
        return _shell_write_fix(invocation, rules, diagnosis, command)
    sites = [
        site
        for site in _tool_sites(invocation.arguments)
        if site.content is not None and violations(site.path, site.content, rules)[0]
    ]
    if not sites:
        return _Fix(
            diagnosis.kind,
            f"No checked fix: the rules flag {diagnosis.path or 'this call'}, but no write in it could be rewritten.",
            [f"Remove the import the rule names from {diagnosis.path or 'the file'} and move it behind a port outside the domain."],
        )
    outcome = _fix_sites(sites, rules)
    return _assemble(diagnosis.kind, outcome, [], shell=False)


def _fix_sites(sites: List[_Site], rules: List[Dict[str, Any]]) -> _Outcome:
    outcome = _Outcome()
    groups: Dict[str, List[_Site]] = {}
    for site in sites:
        groups.setdefault(site.path, []).append(site)
    for path, group in groups.items():
        part = _fix_path(path, group, rules)
        outcome.writes.extend(part.writes)
        outcome.checks.extend(part.checks)
        outcome.steps.extend(part.steps)
        outcome.plans.extend(part.plans)
        if not part.ok:
            outcome.ok = False
            outcome.why = outcome.why or part.why
    return outcome


def _fix_path(path: str, group: List[_Site], rules: List[Dict[str, Any]]) -> _Outcome:
    flagged = [site for site in group if site.content is not None and violations(path, site.content, rules)[0]]
    if not flagged:
        # Nothing to move: the content as sent, or as recovered from the
        # command, is itself the proposal, checked like any other.
        part = _Outcome()
        for site in group:
            entry = _entry(site, site.content or "")
            checks, why = _check_entry(entry, rules)
            part.writes.append(entry)
            part.checks.extend(checks)
            if why:
                part.ok = False
                part.why = part.why or why
        return part
    if any(len(site.content or "") > MAX_CONTENT_CHARS for site in group):
        return _Outcome(ok=False, why=f"'{path}' is too large to rewrite here", steps=[
            f"{path} is too large for a mechanical rewrite: remove the import the rule names and move it behind a port."
        ])
    cleaned: List[str] = []
    removed: List[_Removed] = []
    rule_ids: List[str] = []
    for site in group:
        new, taken, ids, why = _remove_offending(path, site.content or "", rules)
        if why:
            return _Outcome(ok=False, why=why, steps=[f"In {path}, {why}; take the import out by hand and move it behind a port."])
        cleaned.append(new)
        removed.extend(taken)
        rule_ids.extend(ids)
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


def _shell_write_fix(invocation: ToolInvocation, rules: List[Dict[str, Any]], diagnosis: _Diagnosis, command: Any) -> _Fix:
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
    for write in analysis.writes:
        if write.deletes:
            continue
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
    outcome = _fix_sites(sites, rules) if sites else _Outcome(ok=False)
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
    why = outcome.why or (notes[0] if notes else "the proposal did not pass the gates")
    if kind == KIND_UNREADABLE and notes:
        summary = f"No checked fix: {notes[0][:1].lower()}{notes[0][1:]}"
    else:
        summary = f"No checked fix: {why.rstrip('.')}."
    # A textual fix carries no code: a write that has not passed is not handed out.
    return _Fix(kind, summary, steps, [], False, outcome.checks)


def _layering_summary(kind: str, outcome: _Outcome) -> str:
    if not outcome.plans:
        paths = ", ".join(dict.fromkeys(_short(write["path"]) for write in outcome.writes))
        return f"Checked fix: send the writes below with Write instead of the shell ({paths}); they pass the same rules."
    plan = outcome.plans[0]
    libraries = plan["libraries"].split(", ")
    named = libraries[0] if len(libraries) == 1 else f"{libraries[0]} and {len(libraries) - 1} more"
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
_PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
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
_FLAVOURS = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".cs": "csharp", ".go": "go", ".rb": "ruby", ".php": "php",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".ps1": "powershell",
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


def _flavour(path: str) -> str:
    """The language a file is written in, for its environment lookup; 'config' for the rest."""
    _, name, _, extension = _split(path)
    return _FLAVOURS.get(extension.lower(), "config")


def _has_secret(text: Optional[str]) -> bool:
    return bool(text) and not SecretScanner.scan_payload(text)[0]


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
            elif label == "PRIVATE_KEY_HEADER":
                closing = _PEM_END.search(text, end)
                if closing:
                    end = closing.end()
            # The pattern can stop short of the token, which would leave its
            # tail in the file beside the lookup. The whole token goes.
            while end < len(text) and label != "PRIVATE_KEY_HEADER" and _TOKEN_CHARACTER.match(text[end]):
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


def _environment_name(text: str, start: int, label: str) -> str:
    """The variable to read: the name the code gave the value, or the label's usual one."""
    default = _DEFAULT_ENVIRONMENT_NAMES.get(label, "API_KEY")
    line_start = text.rfind("\n", 0, start) + 1
    # Back to before the literal the value sits in, so `"Bearer ` in front of a
    # token does not hide the name the literal is assigned to.
    before = re.sub(r"""[`'"][^`'"]*$""", "", text[line_start:start])
    found = re.search(r"([A-Za-z_][A-Za-z0-9_\-]*)['\"]?\s*(?::=|=>|[:=])\s*(?:[A-Za-z_][\w.]*\(\s*)?$", before)
    if not found:
        return default
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", found.group(1)).replace("-", "_").upper()
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]{2,63}", name):
        return default
    if name in _GENERIC_NAMES and label not in ("GENERIC_API_KEY", "JWT"):
        return default
    return name


def _enclosing_literal(text: str, start: int, end: int, flavour: str) -> Optional[Tuple[int, int, str, str]]:
    """The string literal a secret sits in: (opening index, closing index, quote, string prefix)."""
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
            return None
        if flavour not in ("python", "shell", "config") and segment.startswith("//", position):
            return None
        position += 1
    if opened:
        close = _closing_quote(text, end, opened, flavour)
        if close is None:
            return None
        return open_at, close, opened, _string_prefix(text, open_at, flavour)
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
            return opening, closing, quote, _string_prefix(text, opening, flavour)
    return None


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


def _substitute(text: str, start: int, end: int, name: str, flavour: str) -> str:
    """The text with one secret replaced by the language's own way of reading `name`."""
    placeholder = "${" + name + "}"
    if flavour == "shell":
        literal = _enclosing_literal(text, start, end, "shell")
        if literal is None:
            return text[:start] + '"' + placeholder + '"' + text[end:]
        if literal[2] == "'":
            return text[:start] + "'\"" + placeholder + "\"'" + text[end:]
        return text[:start] + placeholder + text[end:]
    lookup_format = _LOOKUPS.get(flavour)
    if lookup_format is None:
        # A configuration file has no lookup of its own; ${NAME} is what the
        # tools that read such files substitute, and it carries no secret.
        return text[:start] + placeholder + text[end:]
    lookup = lookup_format.format(name=name)
    literal = _enclosing_literal(text, start, end, flavour)
    if literal is None:
        return text[:start] + placeholder + text[end:]
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


def _replace_secrets(text: str, flavour: str) -> Tuple[str, List[str], bool]:
    """Every credential in the text replaced by a lookup. (text, names, clean afterwards)"""
    current = text
    names: List[str] = []
    for _ in range(16):
        spans = _secret_spans(current)
        if not spans:
            return current, names, True
        for start, end, label in reversed(spans):
            name = _environment_name(current, start, label)
            current = _substitute(current, start, end, name, flavour)
            names.append(name)
    return current, names, not _secret_spans(current)


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


def _credential_fix(invocation: ToolInvocation, rules: List[Dict[str, Any]], diagnosis: _Diagnosis) -> _Fix:
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
        text = command if isinstance(command, str) else " ".join(command)
        if len(_secret_spans(text)) > sum(len(_secret_spans(site.content or "")) for site in sites):
            # The Writes replace the command, and the command also carried the
            # credential somewhere else: running "the rest" would leak it.
            stray_in_command = True
        else:
            stray_in_command = False
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
        new_content, found_names, clean = _replace_secrets(site.content or "", flavour)
        if not clean:
            notes.append(f"Not every credential in {site.path} could be replaced by a lookup; remove it by hand.")
            continue
        names.extend(found_names)
        if flavour == "python" and "os.environ[" in new_content:
            if site.shape == "write":
                new_content, added = _ensure_import_os(new_content)
                if added:
                    steps.append(f"`import os` is added to {site.path} for the lookup.")
            else:
                steps.append(f"Make sure `import os` is at the top of {site.path} for the lookup.")
        fixed.append(_Site(site.path, new_content, site.shape, site.old_string))
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
    outcome = _fix_sites(fixed, rules) if fixed else _Outcome(ok=False)
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


def _command_credential_fix(invocation: ToolInvocation, rules: List[Dict[str, Any]], command: Any, label: str) -> _Fix:
    """A command carrying a credential: the same command reading it from the environment."""
    arguments = dict(invocation.arguments)
    key = _command_key(arguments, command)
    names: List[str] = []
    if isinstance(command, str):
        rewritten: Any
        rewritten, names, clean = _replace_secrets(command, "shell")
    else:
        words = []
        clean = True
        for word in command:
            new_word, found, word_clean = _replace_secrets(word, "config")
            words.append(new_word)
            names.extend(found)
            clean = clean and word_clean
        rewritten = words
    names = list(dict.fromkeys(names)) or [_DEFAULT_ENVIRONMENT_NAMES.get(label, "API_KEY")]
    variable = names[0]
    closing = [
        f"Export {', '.join(names)} in your own shell, outside the agent, so the value never passes through the agent.",
        "If the credential was ever committed or shared, rotate it.",
    ]
    if key is None or not clean:
        return _Fix(
            KIND_CREDENTIAL,
            f"No checked fix: take the {label} out of the command and read it from the environment as ${variable}.",
            [f"Replace the {label} in the command with ${variable}."] + closing,
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
    text = rewritten if isinstance(rewritten, str) else shlex.join(rewritten)
    steps = [f"Refer to the {label} as ${variable} in the command instead of writing it out."]
    if len(text) <= MAX_COMMAND_IN_STEP:
        steps.append(f"Run instead: {text}")
    steps.extend(closing)
    if not stray and allowed:
        steps.append("Threefold ran the credential scan and the same gates on the rewritten command, and it passed.")
        return _Fix(
            KIND_CREDENTIAL,
            f"Checked fix: use ${variable} from the environment in the command instead of the literal {label} credential.",
            steps,
            [],
            True,
            checks,
        )
    return _Fix(
        KIND_CREDENTIAL,
        f"No checked fix: the command still fails a gate once the {label} is replaced by ${variable}.",
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
    if diagnosis.why == "hooks":
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
    count_match = re.search(r"(\d+) consecutive times", reason) or re.search(r"for the (\d+)\w* time", reason)
    count = int(count_match.group(1)) if count_match else 3
    cycle = re.search(r"cycle \(([^)]{1,200})\)", reason)
    if cycle:
        what = f"the cycle {cycle.group(1)}"
        how = f"{what} was repeated {count} times"
    else:
        what = f"{tool} on {_short(target, 50)}" if target else tool
        how = f"{what} was called {count} times with identical arguments"
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
            f"The session's spend reached its budget{detail}, so the cost gate halted it.",
            "Ask the operator to raise budget_usd for this work, or start a new session for the next task.",
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


def _finish(fix: _Fix, max_write_bytes: int, phrase: Optional[Callable[[Dict[str, Any]], Optional[str]]]) -> Dict[str, Any]:
    steps = [_one_line(step, MAX_STEP_CHARS) for step in fix.steps if step][:MAX_STEPS]
    writes = [dict(write) for write in fix.writes]
    validated = bool(fix.validated) and bool(fix.checks) and all(check.get("passed") for check in fix.checks if check.get("gate") != GATE_ROUTE)
    # Belt and braces: a write that still carries a credential is never handed
    # out, whatever the code above believed about it.
    if any(_has_secret(write.get("content")) or _has_secret(write.get("old_string")) for write in writes):
        writes = []
        validated = False
        steps.append("The proposed files were withheld because a credential was still found in them.")
    size = sum(
        len(str(write.get("path", "")).encode("utf-8"))
        + len(str(write.get("content", "")).encode("utf-8"))
        + len(str(write.get("old_string") or "").encode("utf-8"))
        for write in writes
    )
    summary = fix.summary
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
        {"gate": str(check.get("gate")), "path": _one_line(check.get("path"), 300), "passed": bool(check.get("passed"))}
        for check in fix.checks
    ]
    if phrase is not None:
        try:
            worded = phrase(dict(out))
        except Exception as exc:  # pragma: no cover - the deterministic summary stands
            logger.warning("The phrasing hook failed; keeping the deterministic summary: %s", exc)
            worded = None
        if isinstance(worded, str) and worded.strip():
            out["summary"] = _one_line(worded, MAX_SUMMARY_CHARS)
    return out
