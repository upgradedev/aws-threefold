"""Data Transfer Objects for Threefold application layer."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import re
from typing import Any, Dict, List, Optional

from threefold.application.labels import label_project

# Who sent a call and through what. Closed sets, because both are stored on
# every ledger row and a free-text field there is one more thing a caller can
# fill with anything. A value outside the set is kept as "unknown" and the
# response says so; a caller that sends nothing, as every caller did before
# these fields existed, is "unknown" without a warning.
KNOWN_AGENTS = ("claude-code", "codex", "antigravity", "pre-commit", "ci", "page")
KNOWN_ORIGINS = ("hook", "page", "ci")
# How the hook on the developer's machine was told to send: `observe` sends
# every call as a dry run, `managed` leaves the decision to the project's stage
# on the server, `enforce` never sends a dry run. Echoed onto the ledger so the
# dashboard can say which machines a promotion will actually reach.
KNOWN_HOOK_MODES = ("observe", "managed", "enforce")
UNKNOWN = "unknown"


class InvalidRequestError(ValueError):
    """A request the caller can correct, with a detail that is safe to show them."""

    def __init__(self, detail: str, name: Optional[str] = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.name = name


def _flag(body: Dict[str, Any], key: str, default: bool) -> bool:
    """Reads a yes/no field, accepting the spellings a shell script sends."""
    value = body.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "0", "no"):
        return False
    raise InvalidRequestError(f"{key} must be true or false.", key)


def _number(body: Dict[str, Any], key: str, default: float, cast: type) -> Any:
    """Reads a numeric field, refusing what would pass a comparison by accident.

    JSON as Python parses it admits NaN and Infinity, and a budget of NaN is
    never exceeded because every comparison with it is false.
    """
    value = body.get(key, default)
    if isinstance(value, bool):
        raise InvalidRequestError(f"{key} must be a number.", key)
    try:
        number = cast(value)
    except (TypeError, ValueError, OverflowError):
        raise InvalidRequestError(f"{key} must be a number.", key) from None
    if isinstance(number, float) and not math.isfinite(number):
        raise InvalidRequestError(f"{key} must be a finite number.", key)
    return number


def _closed_set(body: Dict[str, Any], key: str, known: tuple, warnings: List[str]) -> str:
    value = body.get(key)
    if value is None or value == "":
        return UNKNOWN
    if isinstance(value, str) and value in known:
        return value
    warnings.append(f"{key} is not one of {', '.join(known)}, so it was recorded as '{UNKNOWN}'.")
    return UNKNOWN


@dataclass
class ToolCallRequestDTO:
    """Incoming request to evaluate an agent's intended tool invocation."""
    session_id: str
    developer_id: str
    project_name: str
    tool_name: str
    action_type: str
    arguments: Dict[str, Any]
    projected_input_tokens: int = 2000
    projected_output_tokens: int = 500
    budget_usd: float = 10.00
    # Request v2. Every field has the value an old caller implied by not
    # sending it, so a v1 body still means what it meant.
    agent: str = UNKNOWN
    origin: str = UNKNOWN
    # A page wants the sentence; a hook sits in front of every tool call and
    # must not wait on a model to be told no, so hooks send false.
    explain: bool = True
    # Recorded as observed, never refused, and never trips the session.
    dry_run: bool = False
    # The hook's own mode, from KNOWN_HOOK_MODES, or "unknown".
    hook_mode: str = UNKNOWN
    # The model that made the call, priced by the cost calculator. Not a closed
    # set: models proliferate, and an id the table does not know is priced at
    # the default Sonnet-class rate, which is what every call cost before this
    # field existed.
    model_id: str = "default"
    # What was changed about the request on the way in, returned to the caller.
    warnings: List[str] = field(default_factory=list)

    @classmethod
    def from_payload(
        cls,
        body: Dict[str, Any],
        *,
        default_session_id: str = "session-default",
        default_input_tokens: int = 2000,
        default_output_tokens: int = 500,
        default_budget_usd: float = 10.00,
        tool_name: Optional[str] = None,
        action_type: Optional[str] = None,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> "ToolCallRequestDTO":
        """Builds a request from a JSON body, v1 or v2.

        The routes that read their tool from an envelope of their own, like the
        universal adapter, pass it in; everything else is read from the body.
        The project is labelled here, once, so the session row, the ledger row
        and the metric dimension cannot disagree about it.
        """
        warnings: List[str] = []
        project, project_warning = label_project(body.get("project_name"))
        if project_warning:
            warnings.append(project_warning)
        # `developer` is the v2 name. A v1 caller sent `developer_id`; one that
        # sent neither is anonymous, which is what the contract calls it.
        developer = body.get("developer")
        if developer is None or developer == "":
            developer = body.get("developer_id")
        if developer is None or developer == "":
            developer = "anonymous"
        origin = _closed_set(body, "origin", KNOWN_ORIGINS, warnings)
        # A hook declares no token counts: it sees a tool call, not the model's
        # usage. Pricing it at the page defaults charged every call about 1.3
        # cents, so a working session hit the $10 ceiling after some 740 calls
        # and was halted for spend it never made. The spend gate judges declared
        # usage only, and an undeclared hook call costs nothing here.
        if origin == "hook":
            default_input_tokens = 0
            default_output_tokens = 0
        return cls(
            session_id=body.get("session_id", default_session_id),
            developer_id=str(developer)[:120],
            project_name=project,
            tool_name=tool_name if tool_name is not None else body.get("tool_name", "unknown_tool"),
            action_type=action_type if action_type is not None else body.get("action_type", "FILE_READ"),
            arguments=arguments if arguments is not None else body.get("arguments", {}),
            projected_input_tokens=_number(body, "projected_input_tokens", default_input_tokens, int),
            projected_output_tokens=_number(body, "projected_output_tokens", default_output_tokens, int),
            budget_usd=_number(body, "budget_usd", default_budget_usd, float),
            agent=_closed_set(body, "agent", KNOWN_AGENTS, warnings),
            origin=origin,
            explain=_flag(body, "explain", True),
            dry_run=_flag(body, "dry_run", False),
            hook_mode=_closed_set(body, "hook_mode", KNOWN_HOOK_MODES, warnings),
            model_id=str(body.get("model_id", "default") or "default")[:120],
            warnings=warnings,
        )


@dataclass
class EvaluationResultDTO:
    """Verdict returned to the agent proxy or CI/CD gate."""
    verdict_id: str
    session_id: str
    status: str
    risk_level: str
    reason: str
    rule_evaluations: Dict[str, bool]
    current_session_cost_usd: float
    session_tripped: bool
    proof_hash: str
    bedrock_explanation: Optional[str] = None
    # Names what produced bedrock_explanation: "bedrock" when the model answered,
    # "deterministic_fallback" when it did not. Never inferred, always set by the caller.
    explanation_source: Optional[str] = None
    # Where the session state for this verdict actually landed: "dynamodb" or "memory".
    persistence: Optional[str] = None
    # What rules in observe mode would have refused. The call still ran; this is
    # the record an architect reads before turning a rule to enforce.
    observations: Optional[List[str]] = None
    observed_rules: Optional[List[str]] = None
    observed_target: Optional[str] = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # True when the caller asked for a dry run: whatever the gates found was
    # recorded under observations and nothing was refused.
    dry_run: bool = False
    # What the service changed about the request, such as a project name it
    # recorded as "unlabelled". Always a list, so a client never tests for it.
    warnings: List[str] = field(default_factory=list)
    # The calling project's stage on this stack, "observe" or "enforce": its
    # own when it has been configured, the stack's default otherwise. Reported
    # on every verdict, whether or not the stage applied to this call, so a
    # hook in observe mode still learns what a managed hook would be held to.
    project_stage: Optional[str] = None
    # What to send instead, from application/fix_proposer.py: {kind, summary,
    # steps, writes, validated, checks}. Set on a refusal and on a page's
    # observation when the call fits the ceiling for its kind of fix
    # (evaluator.fix_max_chars), None otherwise. It lives on the response
    # alone: its writes carry the caller's own source, so the ledger keeps
    # only its kind and whether it was validated.
    suggested_fix: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GovernanceCertificateDTO:
    """Audit certificate summarising a compliant agent execution.

    The fingerprint is an unkeyed SHA-256 over the canonical payload: it
    detects corruption and casual edits. Where the stack holds a signing key,
    the same canonical bytes carry a KMS signature over them, and `signature`
    is None anywhere else, which is the honest unsigned state rather than a
    missing field.
    """
    certificate_id: str
    session_id: str
    developer_id: str
    project_name: str
    verdict_status: str
    total_cost_usd: float
    total_tokens: int
    evaluations_count: int
    all_passed: bool
    sha256_fingerprint: str
    signature: Optional[str] = None
    signing_key_id: Optional[str] = None
    issued_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyConfigDTO:
    """Enterprise-level dynamic governance policy configuration.

    Every field is enforced: the single-call cap and the session ceiling by
    the breaker, the history window by the loop detector's cycle search, the
    threshold by its monomorphic run, and the blocked patterns by the secret
    gate, which refuses a call whose arguments match any of them. The patterns
    are additional secret shapes, so like the compiled ones they are always
    enforced and never staged.
    """
    max_single_call_usd: float = 1.00
    max_session_budget_usd: float = 10.00
    loop_history_window: int = 6
    monomorphic_repetition_threshold: int = 3
    # Shapes no gate covers, deliberately. Path shapes do not belong here:
    # `.env` refused the fixer's own `os.environ` rewrites, so no credential
    # fix ever validated, and any shape a stageable gate already matches would
    # refuse in observe mode what the gate is only watching, since a pattern
    # hit is never staged. The gates keep judging the paths; an operator who
    # wants a term refused everywhere adds it to this list.
    blocked_patterns: List[str] = field(
        default_factory=lambda: [
            r"AKIA[0-9A-Z]{16}",
            r"aws_secret_access_key",
        ]
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# At most fifty patterns of two hundred characters each. The gate compiles and
# tries every one against every string a call carries, so without a bound an
# operator's own list is the easiest denial of service this service offers.
MAX_BLOCKED_PATTERNS = 50
MAX_BLOCKED_PATTERN_CHARS = 200


def clean_blocked_patterns(raw: Any) -> List[str]:
    """The usable patterns out of a stored or defaulted list, leniently.

    Drops what is not a string, what is too long, what does not compile, and
    everything past the cap. Storage and defaults pass through here; the
    policy write validates strictly instead, so an operator is told what was
    wrong rather than silently kept on a subset.
    """
    if not isinstance(raw, list):
        return []
    cleaned: List[str] = []
    for pattern in raw:
        if len(cleaned) >= MAX_BLOCKED_PATTERNS:
            break
        if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_BLOCKED_PATTERN_CHARS:
            continue
        try:
            re.compile(pattern)
        except re.error:
            continue
        cleaned.append(pattern)
    return cleaned


@dataclass
class EmergencyFreezeDTO:
    """Manual session termination request from security auditor."""
    session_id: str
    operator_name: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SubsystemHealthDTO:
    name: str
    status: str
    latency_ms: float
    details: str


@dataclass
class ReadinessResponseDTO:
    """Readiness probe result (/readyz)."""
    status: str
    service: str
    version: str
    timestamp_utc: str
    subsystems: List[SubsystemHealthDTO]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
