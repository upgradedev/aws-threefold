"""A project's stage: whether a hook's calls are observed or enforced.

Every project starts in observe, where each call is judged and recorded and
nothing is refused, so a team can see what the rules would have stopped before
anyone is stopped. Promotion moves it to enforce with the rules the operator
picked; the others keep observing. Demotion is one step back.

The stage decides only what a governed developer's own tools send: calls with
origin `hook` or `ci` that are not dry runs. The pages and the dashboard's
scenarios always enforce, so the public demo is what it was, and a dry run is
never refused whatever the stage.

Everything here is pure: the evaluator reads and holds the configuration, and
the routes store it.
"""
from __future__ import annotations

import functools
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from threefold.application.rule_keys import GATE_KEYS

logger = logging.getLogger(__name__)

OBSERVE = "observe"
ENFORCE = "enforce"
STAGES = (OBSERVE, ENFORCE)

DEFAULT_HOOK_STAGE_ENV = "DEFAULT_HOOK_STAGE"
# Projects whose name matches this pattern start in Enforce instead of the
# stack's default, until someone configures them. Empty by default, so nothing
# changes unless a stack asks for it. The public demo uses it for the projects a
# real coding agent reports as once a day: that stack has no operator to promote
# them, and a real agent that could never be refused would measure nothing.
ENFORCE_PROJECT_PATTERN_ENV = "ENFORCE_PROJECT_PATTERN"
STAGED_ORIGINS = ("hook", "ci")
# The dashboard's scenarios mint these. Their halt is the demo, so they keep
# enforcing whatever origin a caller claims for them.
SIMULATED_SESSION_PREFIX = "sim-"

HISTORY_KEPT = 20
MAX_OBSERVE_RULES = 60
MAX_RULE_KEY_LENGTH = 80

SANDBOX_TTL_SECONDS = 24 * 3600
SANDBOX_PATTERN = re.compile(r"^Acme-Sandbox-[0-9a-f]{8}$")


@functools.lru_cache(maxsize=8)
def _warn_unusable_default(value: str) -> None:
    # Logged once per value, because the stage is read on every call.
    logger.warning("%s=%r is not observe or enforce; using observe", DEFAULT_HOOK_STAGE_ENV, value)


def default_hook_stage() -> str:
    """The stage of a project nobody has configured, read on every call.

    Read per call rather than at import, as the project pattern is: the
    handler's evaluator is built when the module is imported, before a test or
    an operator's environment has had its say. A value that is neither stage is
    read as observe, the documented default, and logged.
    """
    raw = os.environ.get(DEFAULT_HOOK_STAGE_ENV)
    if raw is None or not raw.strip():
        return OBSERVE
    value = raw.strip().lower()
    if value in STAGES:
        return value
    _warn_unusable_default(raw)
    return OBSERVE


def stage_applies(request: Any) -> bool:
    """Whether the project's stage decides this call at all."""
    if bool(getattr(request, "dry_run", False)):
        return False
    if str(getattr(request, "session_id", "") or "").startswith(SIMULATED_SESSION_PREFIX):
        return False
    return getattr(request, "origin", "") in STAGED_ORIGINS


def enforced_by_default(project: Optional[str]) -> bool:
    """Whether an unconfigured project of this name starts in Enforce.

    Read per call, as the default stage is. A pattern that does not compile is
    read as no pattern at all and logged: a typo in a parameter must not turn
    every project to Enforce, nor stop a verdict.
    """
    raw = (os.environ.get(ENFORCE_PROJECT_PATTERN_ENV) or "").strip()
    if not raw or not project:
        return False
    try:
        return re.fullmatch(raw, str(project)) is not None
    except re.error:
        logger.warning("%s is not a valid pattern and was ignored", ENFORCE_PROJECT_PATTERN_ENV)
        return False


def stage_of(config: Optional[Mapping[str, Any]], project: Optional[str] = None) -> str:
    """The project's stage: its own when configured; when not, Enforce if its name asks for it, else the default."""
    if config and config.get("stage") in STAGES:
        return str(config["stage"])
    if enforced_by_default(project):
        return ENFORCE
    return default_hook_stage()


def judged_stage(request: Any, project_stage: str) -> str:
    """The stage a call was actually judged under, as the ledger records it.

    A dry run is observed by definition, and a page or scenario call is
    enforced by definition, whatever the project's own stage is.
    """
    if stage_applies(request):
        return project_stage
    return OBSERVE if bool(getattr(request, "dry_run", False)) else ENFORCE


def observe_keys(request: Any, config: Optional[Mapping[str, Any]], stage: str) -> frozenset:
    """The rule keys whose refusals become observations for this call."""
    if stage != ENFORCE or not stage_applies(request) or not config:
        return frozenset()
    return frozenset(str(key) for key in config.get("observe_rules") or [])


def project_rule_keys(layering_rules: Iterable[Mapping[str, Any]]) -> List[str]:
    """A project's rules: its layering rules in force, then the gates it can stage."""
    keys = [str(rule.get("id")) for rule in layering_rules if rule.get("id")]
    return list(dict.fromkeys(keys + list(GATE_KEYS)))


def new_config(now: str, stage: str = OBSERVE, sandbox: bool = False) -> Dict[str, Any]:
    return {
        "stage": stage,
        "observe_rules": [],
        "created_at": now,
        "updated_at": now,
        "promoted_at": None,
        "demoted_at": None,
        "sandbox": bool(sandbox),
        "history": [],
    }


def normalise_config(raw: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """A stored configuration in the contract's shape, or None when there is none.

    Lenient, as reading stored rules is: a stage that is not one of the two is
    kept out rather than guessed, so the project falls back to the stack's
    default instead of to whichever stage a typo happened to resemble.
    """
    if not raw:
        return None
    stage = raw.get("stage")
    history = [dict(entry) for entry in (raw.get("history") or []) if isinstance(entry, Mapping)]
    return {
        "stage": stage if stage in STAGES else None,
        "observe_rules": [str(key) for key in (raw.get("observe_rules") or []) if isinstance(key, str)],
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "promoted_at": raw.get("promoted_at"),
        "demoted_at": raw.get("demoted_at"),
        "sandbox": bool(raw.get("sandbox", False)),
        "history": history[-HISTORY_KEPT:],
    }


class ConfigError(ValueError):
    """A configuration request the caller can correct, naming the field."""

    def __init__(self, detail: str, name: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.name = name


def read_stage(value: Any) -> str:
    if not isinstance(value, str) or value.strip().lower() not in STAGES:
        raise ConfigError('stage must be "observe" or "enforce".', "stage")
    return value.strip().lower()


def read_rule_keys(value: Any, known: Iterable[str], name: str) -> List[str]:
    """A list of rule keys this project has, in the order given, without repeats.

    A key the project does not have is refused rather than stored: a stage that
    names a rule nobody can see would read as a promise nothing keeps.
    """
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ConfigError(f"{name} must be a list of rule keys.", name)
    if len(value) > MAX_OBSERVE_RULES:
        raise ConfigError(f"{name} has {len(value)} rule keys; the limit is {MAX_OBSERVE_RULES}.", name)
    cleaned = list(dict.fromkeys(item.strip()[:MAX_RULE_KEY_LENGTH] for item in value))
    allowed = set(known)
    unknown = [key for key in cleaned if key not in allowed]
    if unknown:
        raise ConfigError(
            f"{name} names rule keys this project does not have: {', '.join(unknown[:10])}. "
            f"Its rules are: {', '.join(sorted(allowed))}.",
            name,
        )
    return cleaned


def _history_entry(now: str, action: str, by: str, enforce: List[str], observe: List[str]) -> Dict[str, Any]:
    return {"at": now, "action": action, "by": by, "enforce": list(enforce), "observe": list(observe)}


def _with_history(config: Dict[str, Any], entry: Dict[str, Any]) -> Dict[str, Any]:
    config["history"] = (list(config.get("history") or []) + [entry])[-HISTORY_KEPT:]
    return config


def updated(
    config: Optional[Mapping[str, Any]],
    now: str,
    stage: Optional[str] = None,
    observe_rules: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """A configuration created or changed by POST /api/projects/<name>."""
    base = dict(normalise_config(config) or new_config(now, stage=default_hook_stage()))
    if base.get("stage") is None:
        base["stage"] = default_hook_stage()
    if stage is not None:
        base["stage"] = stage
    if observe_rules is not None:
        base["observe_rules"] = list(observe_rules)
    base["updated_at"] = now
    return base


def promoted(
    config: Optional[Mapping[str, Any]], now: str, by: str, enforce: List[str], all_keys: List[str]
) -> Dict[str, Any]:
    """Enforce the rules picked; every other rule of the project keeps observing."""
    base = updated(config, now)
    watching = [key for key in all_keys if key not in set(enforce)]
    base.update(stage=ENFORCE, observe_rules=watching, promoted_at=now)
    return _with_history(base, _history_entry(now, "promote", by, enforce, watching))


def demoted(config: Optional[Mapping[str, Any]], now: str, by: str, all_keys: List[str]) -> Dict[str, Any]:
    """Back to observe. observe_rules is kept, so a promotion undone is one step to redo."""
    base = updated(config, now)
    base.update(stage=OBSERVE, demoted_at=now)
    return _with_history(base, _history_entry(now, "demote", by, [], list(all_keys)))


def mode_now(key: str, stage: str, config: Optional[Mapping[str, Any]], rule: Optional[Mapping[str, Any]]) -> str:
    """Whether a rule refuses today for this project's hooks, or only records."""
    if stage != ENFORCE:
        return OBSERVE
    if config and key in (config.get("observe_rules") or []):
        return OBSERVE
    if rule is not None and rule.get("mode") == OBSERVE:
        return OBSERVE
    return ENFORCE
