"""Deterministic governance evaluator coordinating all safety gates."""
from __future__ import annotations

import copy
import datetime
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from threefold.domain import boundary_guard as boundary_guard_module
from threefold.domain import loop_detector as loop_detector_module
from threefold.domain.models import (
    AgentSession,
    GovernanceVerdict,
    RiskLevel,
    ToolActionType,
    ToolInvocation,
    VerdictStatus,
)
from threefold.domain.circuit_breaker import CostCircuitBreaker, TokenCostCalculator
from threefold.domain.loop_detector import LoopDetector
from threefold.domain.layering_rules import DEFAULT_RULES, normalise_rules, validate_rules
from threefold.domain.boundary_guard import (
    ArchitecturalBoundaryGuard,
    describe_target,
    observe_layering,
    redact_secrets,
)
from threefold.application.dtos import (
    EvaluationResultDTO,
    InvalidRequestError,
    PolicyConfigDTO,
    ReadinessResponseDTO,
    SubsystemHealthDTO,
    ToolCallRequestDTO,
)
from threefold.application.labels import is_labelled
from threefold.application import projects as stages
from threefold.application.projects import SIMULATED_SESSION_PREFIX
from threefold.application.rule_keys import (
    FROZEN_SESSION_REASON,
    HALTED_SESSION_REASONS,
    rule_key as rule_key_of,
    with_rule_key,
)
from threefold.domain.events import (
    ArchitecturalBoundaryViolatedEvent,
    DomainEventPublisher,
    LoopDetectedEvent,
    SecretLeakInterceptedEvent,
)
from threefold.infrastructure.dynamo_repo import SessionConflictError

logger = logging.getLogger(__name__)

# How long a container trusts the rules it holds before reading them again.
# Lambda runs several warm containers, each of which loaded the rules on its own
# cold start, so a save reached only the container that took it and the rules
# page said "in force from the next tool call" while the others went on
# enforcing the old set until they happened to be recycled. One read per
# container per half minute is the price of that sentence being true.
RULES_REFRESH_SECONDS = 30.0

# How a refusal that comes from the session's own state, rather than from a
# gate, begins. Both wear the same status, so this prefix is what tells a call
# into an already halted session apart from the breach that halted it.
#
# The cost gate has a sentence of its own for a session that is already halted.
# The pre-check below means it cannot be reached through this evaluator today,
# but a breaker that a caller supplies can return it, and reading that as a
# spend problem is the mislabel this whole distinction exists to avoid.
#
# FROZEN_SESSION_REASON and HALTED_SESSION_REASONS are defined beside the code
# that reads a rule key off a verdict and imported above, so the sentence
# written here and the sentence read there cannot drift apart.

# How many projects' rules one container holds at once. The project name is
# the caller's to send, and every labelled name that arrives gets an entry
# whether or not it has rules of its own, so without a bound a caller cycling
# through names that fit the pattern would grow this without end.
MAX_PROJECTS_HELD = 128

# Session ids the dashboard's scenarios mint (SIMULATED_SESSION_PREFIX,
# imported above). Their loop halt is the demo: call three refused and the
# session frozen, so they keep it whatever origin says, and for the same reason
# a project's stage never applies to them.

# The note a repeated read or poll leaves on its approval instead of a trip.
READ_OR_POLL_REPEAT = "Repeat of a read or poll, recorded rather than refused"


class UnusableRulesError(ValueError):
    """A save that would have dropped or changed a rule, with the reasons."""

    def __init__(self, message: str, problems: list) -> None:
        super().__init__(message)
        self.problems = problems


class SessionNotHaltedError(RuntimeError):
    """A resume asked of a session that is running, so there is nothing to clear.

    Not a ValueError: the handler answers those with 400, and this is a
    conflict with the session's state rather than a malformed request.
    """


@dataclass
class _HeldRules:
    """One project's rules as a container last read them.

    `rules` is None when the project has none of its own, which is remembered
    as carefully as a set that exists: otherwise every call from a project that
    uses the shared set would cost a read.
    """

    rules: Optional[List[Dict[str, Any]]]
    read_at: float


@dataclass
class _HeldConfig:
    """One project's stage configuration as a container last read it.

    `config` is None when the project has never been configured, remembered
    for the same reason _HeldRules remembers "no rules": otherwise every call
    from a project on the stack's default stage would cost a read.
    """

    config: Optional[Dict[str, Any]]
    read_at: float


def is_read_or_poll(invocation: ToolInvocation) -> bool:
    """Whether the domain classes this call as a read or a poll.

    The classifier belongs to the domain track and may not be there yet, so it
    is looked up by name each time rather than imported: the loop gate's own
    module first, then the boundary guard's, which is where the other readings
    of a command live. Missing, or raising, counts as "not a read", so the loop
    gate keeps biting rather than waving a call through on a classifier fault.
    """
    for module in (loop_detector_module, boundary_guard_module):
        classifier = getattr(module, "is_read_or_poll", None)
        if not callable(classifier):
            continue
        try:
            return bool(classifier(invocation))
        except Exception as exc:
            logger.warning("is_read_or_poll failed; judging the call as a write: %s", exc)
            return False
    return False


def loop_halts_session(request: Any) -> bool:
    """Whether a loop detected on this call freezes the whole session.

    A hook sits in front of a developer's own agent, and halting the session
    there locked the developer out of their own work until an operator cleared
    it, for a repeat the refusal alone had already stopped. So a hook's loop is
    refused call by call and the session stays open. The dashboard's scenarios
    (sim-*) and the pages keep the terminal halt, because the frozen session is
    what they exist to show; any other origin keeps it too, because that is
    what every caller had before origins existed.
    """
    if str(getattr(request, "session_id", "") or "").startswith(SIMULATED_SESSION_PREFIX):
        return True
    return getattr(request, "origin", "") != "hook"


class GovernanceEvaluator:
    """Evaluates agent tool requests against deterministic safety and cost invariants."""

    def __init__(
        self,
        cost_breaker: Optional[CostCircuitBreaker] = None,
        loop_detector: Optional[LoopDetector] = None,
        session_repo: Optional[Any] = None,
        policy_config: Optional[PolicyConfigDTO] = None,
    ) -> None:
        # The gates are built from the policy rather than from their own defaults.
        # They used to disagree: the policy reported a $1.00 single-call cap while a
        # freshly built breaker enforced $2.50, so /policy/config described a limit
        # that no call was ever measured against until someone happened to POST a
        # policy. A gate a caller supplies is left exactly as it was supplied.
        self.policy_config = policy_config or PolicyConfigDTO()
        self.cost_breaker = cost_breaker or CostCircuitBreaker(
            max_single_invocation_cost=self.policy_config.max_single_call_usd
        )
        self.loop_detector = loop_detector or LoopDetector(
            repetition_threshold=self.policy_config.monomorphic_repetition_threshold
        )
        if session_repo is not None:
            self.session_repo = session_repo
        else:
            from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository
            self.session_repo = DynamoDBSessionRepository()
        if policy_config is None:
            self._adopt_saved_policy()
        # The layering rules are the architecture of whoever runs this, so they
        # are read from storage rather than compiled in. A deployment that has
        # never been given any enforces the shipped set.
        self.layering_rules = self._adopt_saved_rules()
        self._rules_read_at = time.monotonic()
        # A project's own rules, when it has any, replace the shared set for
        # that project's calls. Read on first use and again after the same
        # interval the shared set is, one project at a time, least recently
        # used first out.
        self._project_rules: "OrderedDict[str, _HeldRules]" = OrderedDict()
        # A project's stage, held the same way and for the same interval, so
        # an evaluation costs no extra read most of the time, a promotion
        # applies at once on the container that took it, and on every other
        # container within RULES_REFRESH_SECONDS.
        self._project_configs: "OrderedDict[str, _HeldConfig]" = OrderedDict()

    def _adopt_saved_policy(self) -> None:
        """Applies the stored policy, so a cold container does not start on defaults."""
        loader = getattr(self.session_repo, "load_policy", None)
        if loader is None:
            return
        try:
            saved = loader()
        except Exception as exc:  # pragma: no cover - storage is best effort
            logger.warning("Could not read the saved policy: %s", exc)
            return
        if not saved:
            return
        try:
            self._apply(
                PolicyConfigDTO(
                    max_single_call_usd=float(saved.get("max_single_call_usd", 1.00)),
                    max_session_budget_usd=float(saved.get("max_session_budget_usd", 10.00)),
                    loop_history_window=int(saved.get("loop_history_window", 6)),
                    monomorphic_repetition_threshold=int(
                        saved.get("monomorphic_repetition_threshold", 3)
                    ),
                )
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Saved policy was unreadable, keeping defaults: %s", exc)

    def _adopt_saved_rules(self) -> list:
        loader = getattr(self.session_repo, "load_rules", None)
        if loader is None:
            return list(DEFAULT_RULES)
        try:
            saved = loader()
        except Exception as exc:  # pragma: no cover - storage is best effort
            logger.warning("Could not read the layering rules: %s", exc)
            return list(DEFAULT_RULES)
        cleaned = normalise_rules(saved) if saved else []
        return cleaned or list(DEFAULT_RULES)

    def refresh_rules_if_stale(self) -> None:
        """Reads the stored rules again when this container's copy is old.

        A read that fails keeps the rules this container already holds. The
        cold-start path falls back to the shipped set because it has nothing
        else, but a warm container that did the same on a transient error would
        swap an architect's rules for the defaults every thirty seconds, and
        nothing would say so.
        """
        if time.monotonic() - self._rules_read_at < RULES_REFRESH_SECONDS:
            return
        self._rules_read_at = time.monotonic()
        loader = getattr(self.session_repo, "load_rules", None)
        if loader is None:
            return
        try:
            saved = loader()
        except Exception as exc:  # pragma: no cover - storage is best effort
            logger.warning("Could not re-read the layering rules; keeping the ones held: %s", exc)
            return
        if not saved:
            self.layering_rules = list(DEFAULT_RULES)
            return
        cleaned = normalise_rules(saved)
        if cleaned:
            self.layering_rules = cleaned

    def rules_in_force(self, project: Optional[str] = None) -> Tuple[List[Dict[str, Any]], str]:
        """The layering rules a call from `project` is judged by, and where they came from.

        The source is "project" when the project has rules of its own,
        otherwise "shared" when an operator has saved a shared set and
        "shipped" when nobody has. A name outside AllowedProjectPattern is
        never looked up: no rules can be saved for it, and such a call is
        stored as "unlabelled" anyway.
        """
        self.refresh_rules_if_stale()
        own = self._project_rules_for(project) if project else None
        if own is not None:
            return own, "project"
        shared = self.layering_rules
        return shared, ("shipped" if shared == DEFAULT_RULES else "shared")

    def _project_rules_for(self, project: str) -> Optional[List[Dict[str, Any]]]:
        """One project's own rules, or None, read at most once per interval.

        A read that fails keeps what this container already holds for the
        project, for the reason refresh_rules_if_stale gives. One that fails
        before anything is held falls back to the shared set until the next
        interval, which is the same staleness a warm container already accepts.
        """
        if not is_labelled(project):
            return None
        loader = getattr(self.session_repo, "load_project_rules", None)
        if loader is None:
            return None
        now = time.monotonic()
        held = self._project_rules.get(project)
        if held is not None and now - held.read_at < RULES_REFRESH_SECONDS:
            self._project_rules.move_to_end(project)
            return held.rules
        kept = held.rules if held is not None else None
        try:
            saved = loader(project)
        except Exception as exc:
            logger.warning("Could not read the layering rules for a project; keeping the ones held: %s", exc)
            rules = kept
        else:
            # Nothing saved is an instruction to use the shared set, as it is
            # for the shared set to use the shipped one. A stored set that no
            # longer normalises to anything is not: that keeps what was held.
            rules = (normalise_rules(saved) or kept) if saved else None
        self._hold(project, rules, now)
        return rules

    def _hold(self, project: str, rules: Optional[List[Dict[str, Any]]], read_at: float) -> None:
        self._project_rules[project] = _HeldRules(rules=rules, read_at=read_at)
        self._project_rules.move_to_end(project)
        while len(self._project_rules) > MAX_PROJECTS_HELD:
            self._project_rules.popitem(last=False)

    # ------------------------------------------------------------ project stage

    def project_config(self, project: Optional[str], fresh: bool = False) -> Optional[Dict[str, Any]]:
        """A project's stage configuration, or None when it has none.

        Read at most once per interval, like a project's rules, unless `fresh`
        asks for the stored copy, as the routes that change it do. A read that
        fails keeps what this container holds, and one that fails before
        anything is held reads as "not configured" until the next interval: the
        stack's default stage, which is the same staleness a warm container
        already accepts for rules. A name outside AllowedProjectPattern is never
        looked up, because no configuration can be saved for it.
        """
        if not is_labelled(project):
            return None
        loader = getattr(self.session_repo, "load_project_config", None)
        if loader is None:
            return None
        now = time.monotonic()
        held = self._project_configs.get(project)
        if not fresh and held is not None and now - held.read_at < RULES_REFRESH_SECONDS:
            self._project_configs.move_to_end(project)
            return held.config
        kept = held.config if held is not None else None
        try:
            config = stages.normalise_config(loader(project))
        except Exception as exc:
            logger.warning("Could not read a project's stage; keeping the one held: %s", exc)
            config = kept
        self._hold_config(project, config, now)
        return config

    def save_project_config(self, project: str, config: Dict[str, Any], ttl_seconds: Optional[int] = None) -> Dict[str, Any]:
        """Stores a project's configuration and holds it at once on this container."""
        if not is_labelled(project):
            raise InvalidRequestError(
                "project must match this deployment's AllowedProjectPattern.", "project"
            )
        cleaned = stages.normalise_config(config) or stages.new_config(
            datetime.datetime.now(datetime.timezone.utc).isoformat()
        )
        saver = getattr(self.session_repo, "save_project_config", None)
        if saver is not None:
            saver(project, cleaned, ttl_seconds=ttl_seconds)
        self._hold_config(project, cleaned, time.monotonic())
        return cleaned

    def list_project_configs(self) -> Dict[str, Dict[str, Any]]:
        """Every configured project, by name, as stored."""
        lister = getattr(self.session_repo, "list_project_configs", None)
        if lister is None:
            return {}
        found = {}
        for name, raw in (lister() or {}).items():
            cleaned = stages.normalise_config(raw)
            if cleaned is not None:
                found[name] = cleaned
        return found

    def stage_for(self, project: Optional[str]) -> Tuple[str, Optional[Dict[str, Any]]]:
        """The project's stage and the configuration it came from."""
        config = self.project_config(project)
        return stages.stage_of(config), config

    def _hold_config(self, project: str, config: Optional[Dict[str, Any]], read_at: float) -> None:
        self._project_configs[project] = _HeldConfig(config=config, read_at=read_at)
        self._project_configs.move_to_end(project)
        while len(self._project_configs) > MAX_PROJECTS_HELD:
            self._project_configs.popitem(last=False)

    def update_rules(self, raw_rules, project: Optional[str] = None) -> list:
        """Replaces the layering rules and stores them, or changes nothing.

        The save is refused whole when any rule in it cannot be used. Keeping the
        usable ones answered 200 with a rule missing, and the only sign was a
        count one lower than the architect sent.

        With a project, the set is that project's alone and the shared set is
        untouched. The name has to fit AllowedProjectPattern, because a call
        whose name does not is stored as "unlabelled" and could never be judged
        by rules saved under the name it was sent with.
        """
        if project is not None and not is_labelled(project):
            raise InvalidRequestError(
                "project must match this deployment's AllowedProjectPattern. Leave it out "
                "to replace the shared rules.",
                "project",
            )
        cleaned, problems = validate_rules(raw_rules)
        if problems:
            raise UnusableRulesError(
                f"{len(problems)} rule(s) cannot be used, so nothing was saved.", problems
            )
        if not cleaned:
            raise UnusableRulesError(
                "No rule was supplied. A rule needs at least one path pattern under "
                "when_path_matches and at least one pattern under forbid_imports.",
                [],
            )
        if project is not None:
            self._hold(project, cleaned, time.monotonic())
            saver = getattr(self.session_repo, "save_project_rules", None)
            if saver is not None:
                try:
                    saver(project, cleaned)
                except Exception as exc:  # pragma: no cover - storage is best effort
                    logger.warning("Could not persist a project's layering rules: %s", exc)
            return cleaned
        self.layering_rules = cleaned
        self._rules_read_at = time.monotonic()
        saver = getattr(self.session_repo, "save_rules", None)
        if saver is not None:
            try:
                saver(cleaned)
            except Exception as exc:  # pragma: no cover - storage is best effort
                logger.warning("Could not persist the layering rules: %s", exc)
        return cleaned

    def _apply(self, config: PolicyConfigDTO) -> None:
        self.policy_config = config
        self.cost_breaker.max_single_invocation_cost = config.max_single_call_usd
        self.loop_detector.repetition_threshold = config.monomorphic_repetition_threshold

    def update_policy(self, config: PolicyConfigDTO) -> None:
        """Applies a policy and stores it.

        A setting that lives only in the memory of whichever container answered
        is not a setting. It would apply to some requests and not others, which
        is worse than having no setting at all.
        """
        self._apply(config)
        saver = getattr(self.session_repo, "save_policy", None)
        if saver is not None:
            try:
                saver(config.to_dict())
            except Exception as exc:  # pragma: no cover - storage is best effort
                logger.warning("Could not persist the policy: %s", exc)

    def list_sessions(self, limit: int = 50):
        """Recent sessions for the dashboard."""
        lister = getattr(self.session_repo, "list_sessions", None)
        return lister(limit=limit) if lister else []

    def list_decisions(self, days: int = 7, limit: int = 1000):
        """Every decision in a window, for the console that reports on them.

        A row written before rule keys existed is given the one it would have
        been given, best effort, from the reason it kept.
        """
        lister = getattr(self.session_repo, "list_decisions", None)
        return [with_rule_key(row) for row in lister(days=days, limit=limit)] if lister else []

    def list_rollups(self, days: int = 7, project: Optional[str] = None) -> List[Dict[str, Any]]:
        """Each project's daily totals over a window, for the charts and tiles."""
        lister = getattr(self.session_repo, "list_rollups", None)
        return lister(days=days, project=project) if lister else []

    def terminate_session(self, session_id: str, operator_name: str, reason: str) -> AgentSession:
        """Manual enterprise kill-switch to immediately freeze an agent session."""
        session = self.get_or_create_session(session_id)
        session.terminate_manually(operator=operator_name, reason=reason)
        # An operator may halt a session that has already tripped itself, so this
        # write deliberately overrides the terminal-state guard.
        self.session_repo.save_session(session, force=True)
        return session

    def resume_session(self, session_id: str, operator_name: str, reason: str) -> Optional[AgentSession]:
        """Clears a halted session and records who cleared it, why, and what halt.

        None for a session this service has no record of. Unlike the kill
        switch, a resume never creates the session it names: there is nothing
        to resume, and a write here would let a key holder mint rows by typo.

        The history and the spend are kept. A resume clears the halt, not the
        record of what led to it, so the gates still apply afterwards: a
        session past its budget halts again on its next call that costs
        anything, and a page or scenario session that repeats the call that
        halted it halts again.
        """
        session = self.session_repo.get_session(session_id)
        if session is None:
            return None
        if not session.is_tripped:
            raise SessionNotHaltedError(
                f"Session {session_id!r} is not halted, so there is nothing to resume."
            )
        cleared = session.trip_reason or ""
        # Assigned here because AgentSession, which belongs to the domain track,
        # has a method to halt a session and none to resume one.
        session.is_tripped = False
        session.trip_reason = None
        session.is_terminated = False
        session.terminated_by = None
        session.termination_reason = None
        session.resumed_by = operator_name
        session.resume_reason = reason
        session.resumed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        session.resumed_from = cleared
        # The stored row is tripped, and the terminal-state guard exists to
        # refuse exactly the write that clears it. This is the one caller
        # entitled to, which is why the route needs the operator key.
        self.session_repo.save_session(session, force=True)
        return session

    def check_readiness(self, bedrock_client: Optional[Any] = None) -> ReadinessResponseDTO:
        """Readiness probe reporting what each dependency has actually shown.

        The session store is exercised at call time with a real read. The model
        is not: invoking one on every readiness check would bill the account for
        being looked at, so that subsystem reports the client's own record and
        is healthy on a container that has not called it yet. The detail string
        says which of the two a reader is looking at, and this docstring used to
        claim both were measured.
        """
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        subsystems = []

        started = time.perf_counter()
        probe = getattr(self.session_repo, "probe", None)
        if probe is None:
            store_ok, store_detail = False, "Repository does not expose a read probe"
        else:
            store_ok, store_detail = probe()
        subsystems.append(
            SubsystemHealthDTO(
                name="SessionStore",
                status="HEALTHY" if store_ok else "DEGRADED",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                details=store_detail,
            )
        )

        started = time.perf_counter()
        if bedrock_client is None:
            model_ok, model_detail = False, "No Bedrock client bound to this evaluator"
        else:
            model_ok, model_detail = bedrock_client.describe_availability()
        subsystems.append(
            SubsystemHealthDTO(
                name="BedrockExplanationModel",
                status="HEALTHY" if model_ok else "DEGRADED",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                details=model_detail,
            )
        )

        overall = "READY" if all(s.status == "HEALTHY" for s in subsystems) else "DEGRADED"
        return ReadinessResponseDTO(
            status=overall,
            service="Threefold",
            version="1.0.0",
            timestamp_utc=now_iso,
            subsystems=subsystems,
        )

    def get_or_create_session(
        self,
        session_id: str,
        developer_id: str = "dev-default",
        project_name: str = "Acme-Core",
        budget_usd: float = 10.00,
    ) -> AgentSession:
        """Loads the session from durable storage, or creates it.

        There is deliberately no in-process session cache. A warm Lambda
        container holding its own copy would keep approving calls on a session
        that another container has already tripped.
        """
        persistent_session = self.session_repo.get_session(session_id)
        if persistent_session is not None:
            return persistent_session

        new_session = AgentSession(
            session_id=session_id,
            developer_id=developer_id,
            project_name=project_name,
            budget_usd=budget_usd,
        )
        self.session_repo.save_session(new_session)
        return new_session

    def evaluate_tool_call(self, request: ToolCallRequestDTO) -> EvaluationResultDTO:
        """Evaluates a tool call and records the decision.

        The recording is here rather than inside the gates because a refusal used
        to leave no trace at all: the gates return early, so only approved calls
        reached the session write. Nothing could answer which rule refused what,
        for whom, last week, which is the only question a platform owner has.
        """
        # Resolved once, so every gate in this call reads the same set even if
        # a refresh lands halfway through. The stage is resolved the same way.
        project = getattr(request, "project_name", None)
        rules, _ = self.rules_in_force(project)
        project_stage, config = self.stage_for(project)
        stage = stages.judged_stage(request, project_stage)
        result = self._decide(
            request,
            rules,
            dry_run=bool(getattr(request, "dry_run", False)),
            stage=stage,
            observe_keys=stages.observe_keys(request, config, stage),
        )
        result.project_stage = project_stage
        self._record_decision(request, result, rules, stage=stage)
        return result

    @staticmethod
    def _rule_key(result: EvaluationResultDTO, rules: List[Dict[str, Any]]) -> str:
        """The rule key of a verdict, read with the ids of the rules that judged it."""
        return rule_key_of(
            {
                "status": result.status,
                "reason": result.reason,
                "observed_rules": getattr(result, "observed_rules", None),
                "observations": getattr(result, "observations", None),
            },
            [rule.get("id", "") for rule in rules or []],
        )

    def _record_decision(
        self,
        request: ToolCallRequestDTO,
        result: EvaluationResultDTO,
        rules: Optional[List[Dict[str, Any]]] = None,
        stage: str = stages.ENFORCE,
    ) -> None:
        """Appends one row to the decision ledger, best effort.

        Deliberately narrow: the tool, the rule, who and where, and a short
        descriptor of the target. Never the arguments and never file content.
        The service already sees those; it does not need to keep them, and a
        ledger that stored a refused secret would be the joke that writes itself.
        """
        recorder = getattr(self.session_repo, "record_decision", None)
        if recorder is None:
            return
        try:
            recorder(
                {
                    "verdict_id": result.verdict_id,
                    "timestamp": result.timestamp,
                    # Stored as short strings whatever the caller sent. These are
                    # rendered on pages anyone can open, and a tool_name sent as a
                    # list reached one of them as markup.
                    "session_id": str(request.session_id or "")[:160],
                    "developer_id": str(request.developer_id or "")[:120],
                    "project_name": str(request.project_name or "")[:120],
                    "tool_name": str(request.tool_name or "")[:120],
                    "action_type": str(request.action_type),
                    # Which agent, and whether a hook, a page or CI sent it. Both
                    # arrive from closed sets, so they are safe to count by.
                    "agent": str(getattr(request, "agent", "") or "unknown")[:40],
                    "origin": str(getattr(request, "origin", "") or "unknown")[:40],
                    "dry_run": bool(getattr(request, "dry_run", False)),
                    # The stage the call was judged under, and the mode the
                    # hook was told to send in. Kept apart from dry_run, which
                    # stays what the caller asked for: an observe-stage call
                    # was not a dry run, it was a project not yet enforcing.
                    "stage": stage,
                    "hook_mode": str(getattr(request, "hook_mode", "") or "unknown")[:40],
                    "status": result.status,
                    "rule": self._rule_that_fired(result),
                    # What readiness, reviews and a project's stage group by:
                    # the layering rule that decided, or the gate, or NONE. Set
                    # on observations as well as refusals, because an
                    # observation is the evidence a rule is promoted on.
                    "rule_key": self._rule_key(result, rules or []),
                    # The reason distinguishes a layer being crossed from a
                    # credential store being reached. Both fail the same
                    # invariant and a reader acts on them differently.
                    "reason": redact_secrets(result.reason or "")[:240],
                    # A call that ran but that a rule in observe mode would have
                    # refused. The status stays APPROVED, because it was.
                    # Every observing rule is kept, not the first: an architect
                    # staging three rules at once decides each rollout from
                    # these counts, and keeping one would undercount the rest.
                    "observed_rules": list(getattr(result, "observed_rules", None) or []),
                    "observed_rule": (getattr(result, "observed_rules", None) or [""])[0],
                    "observed_reason": redact_secrets((result.observations or [""])[0])[:240],
                    # The file the observation is about. The row's target is the
                    # first path in the call, which in a multi-file edit can be a
                    # different file from the one the rule would have refused.
                    "observed_target": (getattr(result, "observed_target", "") or "")[:160],
                    "target": describe_target(request),
                    "cost_usd": result.current_session_cost_usd,
                }
            )
        except Exception as exc:  # pragma: no cover - the ledger must not break a verdict
            logger.warning("Could not record the decision: %s", exc)

    @staticmethod
    def _rule_that_fired(result: EvaluationResultDTO) -> str:
        """Names the gate that refused, or NONE when every gate passed.

        A call into a session that is already halted is refused by the session's
        state, not by a gate. The verdict marks the budget invariant false for
        every one of them, whatever did the halting, so a loop-halted session
        would file all of its later refusals under budget and the console would
        report a cost problem where there was a thrashing problem. The breach
        that halts a session in the first place is the cost gate's, and it wears
        that name: it is told apart by the reason, which is the only thing that
        distinguishes the two on one status.
        """
        status = (result.status or "").upper()
        # A dry run keeps the invariant that failed marked false, because it did
        # fail, but nothing refused the call, so no rule fired.
        if status == "APPROVED":
            return "NONE"
        if status == "BLOCKED_CIRCUIT_BREAKER" and (result.reason or "").startswith(HALTED_SESSION_REASONS):
            return "SESSION_ALREADY_HALTED"
        for rule, passed in (result.rule_evaluations or {}).items():
            if not passed:
                return rule
        return "NONE"

    def _decide(
        self,
        request: ToolCallRequestDTO,
        rules: List[Dict[str, Any]],
        dry_run: bool = False,
        stage: str = stages.ENFORCE,
        observe_keys: frozenset = frozenset(),
    ) -> EvaluationResultDTO:
        """Evaluates tool invocation against all deterministic safety gates.

        `stage` is the one the call is judged under. Observe is judged exactly
        as a dry run is. Enforce runs the gates, except that a refusal whose
        rule key is in `observe_keys` becomes an observation.
        """
        session = self.get_or_create_session(
            session_id=request.session_id,
            developer_id=request.developer_id,
            project_name=request.project_name,
            budget_usd=request.budget_usd,
        )
        if dry_run or stage == stages.OBSERVE:
            # The loop and cost gates trip the session object they are handed, so a
            # dry run hands them a copy and never writes a halt: a call that is only
            # being watched must not stop the real session it belongs to. A call the
            # gates approve is recorded as any approved call is, so the loop gate
            # still sees the history it needs on the next dry run.
            halted_before = session.is_tripped
            found = self._run_gates(request, copy.deepcopy(session), rules, may_halt=False)
            if dry_run:
                return self._as_observed(found, halted_before)
            return self._as_observed(found, halted_before, lead="Observe stage, not enforced.", dry_run=False)
        if not observe_keys:
            return self._run_gates(request, session, rules, may_halt=True)
        return self._enforce_except(request, session, rules, observe_keys)

    def _enforce_except(
        self,
        request: ToolCallRequestDTO,
        session: AgentSession,
        rules: List[Dict[str, Any]],
        observe_keys: frozenset,
    ) -> EvaluationResultDTO:
        """Enforces every rule but the ones the project still observes.

        Which rule refused is only known once the gates have run, and a gate
        that refuses may already have halted the session by then: the loop gate
        does for CI, the cost gate does for everyone. So the gates run first on
        a copy, as a dry run does, and nothing is written. A call they approve
        was recorded then, as any approved call is. A refusal under an observed
        key becomes an observation. Any other refusal is judged again on the
        real session, which is safe because a refusal writes nothing but its
        halt, and gives the same answer because the gates are deterministic
        over the same history.
        """
        halted_before = session.is_tripped
        found = self._run_gates(request, copy.deepcopy(session), rules, may_halt=False)
        if found.status == VerdictStatus.APPROVED.value:
            return found
        key = self._rule_key(found, rules)
        if key in observe_keys:
            return self._as_observed(
                found,
                halted_before,
                lead=f"Rule {key} is observed in this project, not enforced.",
                dry_run=False,
            )
        return self._run_gates(request, session, rules, may_halt=True)

    def _as_observed(
        self,
        found: EvaluationResultDTO,
        halted_before: bool,
        lead: str = "Dry run, not enforced.",
        dry_run: bool = True,
    ) -> EvaluationResultDTO:
        """Turns what the gates found into an approval that records it.

        For a dry run, an observe-stage call, or a refusal under a rule the
        project still observes; `lead` says which, and `dry_run` stays what the
        caller asked for. A fresh verdict rather than an edited one, so the
        proof hash covers the status and reason the caller is actually given.
        """
        if found.status == VerdictStatus.APPROVED.value:
            found.dry_run = dry_run
            return found
        rule = self._rule_that_fired(found)
        verdict = GovernanceVerdict.create(
            session_id=found.session_id,
            status=VerdictStatus.APPROVED,
            risk_level=RiskLevel(found.risk_level),
            reason=f"{lead} This call would have been refused: {found.reason}",
            # Left as the gates found them. The invariant did fail; it was only
            # not enforced, and a certificate over this verdict should say so.
            rule_evaluations=found.rule_evaluations,
        )
        observed = EvaluationResultDTO(
            verdict_id=verdict.verdict_id,
            session_id=found.session_id,
            status=verdict.status.value,
            risk_level=verdict.risk_level.value,
            reason=verdict.reason,
            rule_evaluations=verdict.rule_evaluations,
            current_session_cost_usd=found.current_session_cost_usd,
            session_tripped=halted_before,
            proof_hash=verdict.proof_hash,
            timestamp=verdict.timestamp,
            dry_run=dry_run,
        )
        observed.observations = [found.reason]
        observed.observed_rules = [rule]
        return observed

    def _run_gates(
        self,
        request: ToolCallRequestDTO,
        session: AgentSession,
        rules: List[Dict[str, Any]],
        may_halt: bool,
    ) -> EvaluationResultDTO:
        """The gates themselves. `may_halt` is false only for a dry run.

        `rules` are the layering rules for the calling project, resolved once
        by the caller.
        """
        rule_evaluations: Dict[str, bool] = {
            "SECRET_LEAKAGE_FREE": True,
            "ARCHITECTURAL_BOUNDARY_SAFE": True,
            "LOOP_THRASHING_FREE": True,
            "BUDGET_CIRCUIT_BREAKER_SAFE": True,
        }

        # Pre-check: has the session already been frozen or tripped?
        if session.is_tripped:
            rule_evaluations["BUDGET_CIRCUIT_BREAKER_SAFE"] = False
            verdict = GovernanceVerdict.create(
                session_id=session.session_id,
                status=VerdictStatus.BLOCKED_CIRCUIT_BREAKER,
                risk_level=RiskLevel.CRITICAL,
                reason=f"{FROZEN_SESSION_REASON}: {session.trip_reason}",
                rule_evaluations=rule_evaluations,
            )
            return self._to_dto(session, verdict)

        try:
            action_type = ToolActionType(request.action_type)
        except ValueError:
            action_type = ToolActionType.UNKNOWN

        invocation = ToolInvocation(
            tool_name=request.tool_name,
            action_type=action_type,
            arguments=request.arguments,
        )

        # Gate 1: Check for Secrets & Credentials
        is_boundary_safe, boundary_reason = ArchitecturalBoundaryGuard.evaluate_tool_boundary(
            invocation, rules=rules
        )
        if not is_boundary_safe:
            if "Sensitive credential detected" in boundary_reason:
                rule_evaluations["SECRET_LEAKAGE_FREE"] = False
                DomainEventPublisher.publish(
                    SecretLeakInterceptedEvent.create(
                        event_type="SECRET_LEAK_INTERCEPTED",
                        aggregate_id=session.session_id,
                        payload={"tool": request.tool_name, "reason": boundary_reason},
                    )
                )
                verdict = GovernanceVerdict.create(
                    session_id=session.session_id,
                    status=VerdictStatus.BLOCKED_SECRET_DETECTED,
                    risk_level=RiskLevel.CRITICAL,
                    reason=boundary_reason,
                    rule_evaluations=rule_evaluations,
                )
                return self._to_dto(session, verdict)
            else:
                rule_evaluations["ARCHITECTURAL_BOUNDARY_SAFE"] = False
                DomainEventPublisher.publish(
                    ArchitecturalBoundaryViolatedEvent.create(
                        event_type="ARCHITECTURAL_BOUNDARY_VIOLATED",
                        aggregate_id=session.session_id,
                        payload={"tool": request.tool_name, "reason": boundary_reason},
                    )
                )
                verdict = GovernanceVerdict.create(
                    session_id=session.session_id,
                    status=VerdictStatus.BLOCKED_BOUNDARY_VIOLATION,
                    risk_level=RiskLevel.HIGH,
                    reason=boundary_reason,
                    rule_evaluations=rule_evaluations,
                )
                return self._to_dto(session, verdict)

        # Gate 2: Check for Loop & Thrashing
        is_loop_free, loop_reason = self.loop_detector.evaluate_loop_risk(session.history, invocation)
        repeat_note = ""
        if not is_loop_free and is_read_or_poll(invocation):
            # An agent waiting on CI or checking `git status` between edits
            # repeats itself by design, and halting it for that stopped real
            # work for a pattern that changes nothing. The call runs, joins the
            # history like any approved call, and the repeat is noted on it.
            repeat_note = f"{READ_OR_POLL_REPEAT}: {loop_reason}"
        elif not is_loop_free:
            rule_evaluations["LOOP_THRASHING_FREE"] = False
            DomainEventPublisher.publish(
                LoopDetectedEvent.create(
                    event_type="LOOP_DETECTED",
                    aggregate_id=session.session_id,
                    payload={"tool": request.tool_name, "reason": loop_reason},
                )
            )
            reason = loop_reason
            if loop_halts_session(request):
                session.trip_circuit_breaker(loop_reason)
                if may_halt:
                    self._persist_halt(session)
            elif may_halt:
                # Nothing is written: the refused call stays out of the history
                # as every refusal does, so the same call is refused again while
                # a different one is judged on its own.
                reason = (
                    f"{loop_reason}. This repeat was refused and the session was not halted, "
                    "so the next different call is judged normally."
                )
            else:
                # A dry run refuses nothing, and _as_observed already says the
                # call would have been refused. Saying "this repeat was refused"
                # as well put a refusal that never happened on an approved call
                # and into the ledger, so a dry run states the policy instead.
                reason = (
                    f"{loop_reason}. A hook's repeat is refused on its own and never halts the "
                    "session, so the next different call is judged normally."
                )
            verdict = GovernanceVerdict.create(
                session_id=session.session_id,
                status=VerdictStatus.BLOCKED_LOOP_DETECTED,
                risk_level=RiskLevel.HIGH,
                reason=reason,
                rule_evaluations=rule_evaluations,
            )
            return self._to_dto(session, verdict)

        # Gate 3: Check Cost & Budget Limits
        projected_usage = TokenCostCalculator.calculate(
            input_tokens=request.projected_input_tokens,
            output_tokens=request.projected_output_tokens,
        )
        is_cost_safe, cost_reason = self.cost_breaker.evaluate_cost_risk(session, projected_usage)
        if not is_cost_safe:
            rule_evaluations["BUDGET_CIRCUIT_BREAKER_SAFE"] = False
            # evaluate_cost_risk trips the session on a breach, so the halt is durable too.
            if may_halt:
                self._persist_halt(session)
            verdict = GovernanceVerdict.create(
                session_id=session.session_id,
                status=VerdictStatus.BLOCKED_CIRCUIT_BREAKER,
                risk_level=RiskLevel.CRITICAL,
                reason=cost_reason,
                rule_evaluations=rule_evaluations,
            )
            return self._to_dto(session, verdict)

        # All gates passed. The write happens before the verdict is issued: if a
        # concurrent container has already halted this session, the write is
        # refused and the caller is told the session is blocked, not approved.
        session.record_usage(projected_usage)
        session.record_tool_call(invocation)
        try:
            self.session_repo.save_session(session)
        except SessionConflictError as exc:
            logger.warning("Refusing to approve a session halted elsewhere: %s", exc)
            stored = self.session_repo.get_session(session.session_id)
            rule_evaluations["BUDGET_CIRCUIT_BREAKER_SAFE"] = False
            blocked_reason = (
                stored.trip_reason
                if stored is not None and stored.trip_reason
                else "Session was halted by another worker"
            )
            verdict = GovernanceVerdict.create(
                session_id=session.session_id,
                status=VerdictStatus.BLOCKED_CIRCUIT_BREAKER,
                risk_level=RiskLevel.CRITICAL,
                reason=f"{FROZEN_SESSION_REASON}: {blocked_reason}",
                rule_evaluations=rule_evaluations,
            )
            return self._to_dto(stored or session, verdict)

        verdict = GovernanceVerdict.create(
            session_id=session.session_id,
            status=VerdictStatus.APPROVED,
            risk_level=RiskLevel.LOW,
            reason="All deterministic governance invariants satisfied",
            rule_evaluations=rule_evaluations,
        )
        approved = self._to_dto(session, verdict)
        watched = observe_layering(invocation, rules=rules)
        if watched:
            approved.observations = [item["reason"] for item in watched]
            # One rule watching two files of a multi-file edit is one rule that
            # would have refused this call, not two, and counting it twice
            # inflated the number an architect decides a rollout from.
            approved.observed_rules = list(dict.fromkeys(item["rule_id"] for item in watched))
            approved.observed_target = watched[0].get("path", "")
        if repeat_note:
            # Appended after any rule's observation, so the ledger's first
            # observed reason still belongs to its first observed rule. It names
            # no rule: the console counts observed rules as "would refuse", and
            # no gate would refuse this call.
            approved.observations = list(approved.observations or []) + [repeat_note]
        return approved

    def _persist_halt(self, session: AgentSession) -> None:
        """Writes a halted session through, so every worker observes the trip.

        ``force`` is used because the session is already tripped in memory and the
        terminal-state guard would otherwise refuse the very write that records it.
        """
        try:
            self.session_repo.save_session(session, force=True)
        except Exception as exc:  # pragma: no cover - defensive, storage is best effort
            logger.error("Failed to persist circuit breaker halt: %s", exc)

    def _to_dto(self, session: AgentSession, verdict: GovernanceVerdict) -> EvaluationResultDTO:
        return EvaluationResultDTO(
            verdict_id=verdict.verdict_id,
            session_id=session.session_id,
            status=verdict.status.value,
            risk_level=verdict.risk_level.value,
            reason=verdict.reason,
            rule_evaluations=verdict.rule_evaluations,
            current_session_cost_usd=session.total_cost_usd,
            session_tripped=session.is_tripped,
            proof_hash=verdict.proof_hash,
            timestamp=verdict.timestamp,
        )
