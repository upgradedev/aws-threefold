"""`POST /rules/draft`: a layering rule drafted from a sentence, tried, and never saved.

The body is `{description, project?, examples?}`. The answer is one rule in the
format `POST /rules` accepts, set to observe, with the verdict it gives on each
example; see `threefold.application.rule_drafter` for how it is made and checked.

Who may call it. The security middleware decides, by name, as it decides
`POST /rules/explain`: both are listed in its `PAGE_READ_POSTS`, so a draft is
open to anyone on a stack whose reads are public and needs the operator, by key
or sign-in session, on a stack deployed with PublicReads=false. It may be open
on the public stack because it changes nothing. No rule is saved, no verdict is
issued and no ledger row is written, and the draft goes back only to the caller
who asked for it; putting it in force is a separate `POST /rules`, which needs
the operator on every stack. The public stack has no operator key at all, so a
closed route could never be tried there by the people the demonstration is for.
A private stack has no such audience, and every draft is paid for by the
account, so there it is closed, and a request the middleware refuses never
reaches this module, let alone the model.

What sets it apart from explain is the bill: explain costs CPU, and every draft
is one or two model calls. The brakes, and how far each one reaches:
- The caps on what a request may carry: a 600 character description, five
  examples of at most 4,000 characters, and only their paths and imports reach
  the model. These hold for every request.
- A 400 token answer and at most one repair, so a draft is at most two calls.
- A cap of 60 drafting calls per container, kept apart from the explanations'
  cap, so a flood of drafts can never leave a refusal without its sentence.
  The worst one container can be made to spend is those 60 calls.
- The per-IP token bucket every request passes, sixty in a burst and then two
  a second.
The last two live in the memory of one container. Lambda runs as many
containers as there are concurrent requests, the template reserves no
concurrency, and a caller's next request can land on a container whose bucket
has never seen them. So they bound one container, not the account: N busy
containers can make N times 60 calls. An account-wide bound needs reserved
concurrency or throttling in the template, which this track does not own.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from threefold.application.dtos import InvalidRequestError
from threefold.application.rule_drafter import (
    DRAFT_CLIENT_TIMEOUTS,
    SOURCE_BEDROCK,
    SOURCE_UNAVAILABLE,
    ModelUnavailableError,
    NotALayeringRuleError,
    UndraftableRuleError,
    draft_rule,
)
from threefold.domain.layering_rules import UNSUPPORTED
from threefold.infrastructure.bedrock_client import BedrockGovernanceClient
from threefold.infrastructure.metrics_emf import emit_threefold_emf_metrics
from threefold.infrastructure.security_middleware import rfc7807_error

logger = logging.getLogger("threefold.api.drafts")

DRAFT_PATH = "/rules/draft"

# Drafting calls one container may make before it answers 503 until recycled.
# Two per draft at most, so at least thirty drafts, and a separate budget from
# the 200 the verdict explanations share.
DRAFT_CALLS_PER_CONTAINER = 60

# Drafting model calls the whole account may make in one UTC day, counted in
# one table row every container adds to. The per-container cap above bounds a
# container; this bounds the account, which is what a public route needs: N
# busy containers could otherwise spend N times sixty. A draft claims its two
# calls before the model is asked, so a draft past the budget reaches no model.
DRAFT_MODEL_CALLS_PER_DAY = 400
MODEL_CALLS_PER_DRAFT = 2

_client: Optional[BedrockGovernanceClient] = None


class _DraftingSession:
    """Hands the governance client a runtime built with the drafting timeouts.

    `BedrockGovernanceClient` takes a session so a caller can choose how its
    runtime is built, and builds it with the verdict timeouts otherwise. A draft
    generates up to 400 tokens without streaming, so it needs a longer read
    timeout than a two-sentence explanation; `DRAFT_CLIENT_TIMEOUTS` in the
    drafter says how long and why. Everything else, the model id, the region,
    the call cap and the record of the last error, stays the client's own.
    """

    def client(
        self, service_name: str, region_name: Optional[str] = None, config: Any = None
    ) -> Any:
        import boto3
        from botocore.config import Config

        return boto3.client(
            service_name, region_name=region_name, config=Config(**DRAFT_CLIENT_TIMEOUTS)
        )


def _offline() -> bool:
    """The switch the governance client reads, read here because a session bypasses it.

    The client consults THREEFOLD_OFFLINE only when it builds its own runtime, so
    a session handed to it would bring a runtime back in an offline test or
    container. Without a session it stays offline, as it does for the verdicts.
    """
    return os.getenv("THREEFOLD_OFFLINE", "").lower() in ("1", "true", "yes")


def _drafting_client() -> BedrockGovernanceClient:
    """The container's drafting client, built on the first draft rather than at import.

    Its own instance, not the one the verdicts use, so the call cap above is its
    own. Built lazily because most containers never draft, and constructing a
    Bedrock client costs a cold start time it would never repay.
    """
    global _client
    if _client is None:
        session = None if _offline() else _DraftingSession()
        try:
            _client = BedrockGovernanceClient(
                max_calls_per_container=DRAFT_CALLS_PER_CONTAINER, boto3_session=session
            )
        except Exception as exc:
            # Without boto3 the session cannot build a runtime. The client built
            # without one then tries for itself, records why it could not, and
            # every draft answers 503 with no draft, as it does offline.
            logger.info("The drafting runtime could not be built with its own timeouts: %s", exc)
            _client = BedrockGovernanceClient(max_calls_per_container=DRAFT_CALLS_PER_CONTAINER)
    return _client


def handle(path: str, method: str, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Answers `POST /rules/draft`, or returns None for any other request.

    Input the caller can correct raises InvalidRequestError, which the router
    answers with its usual 400 problem, as it does for every other route.
    """
    if path != DRAFT_PATH or method != "POST":
        return None
    # Imported here, not at the top: the router imports this module while it is
    # itself being loaded, and the helpers used below are defined further down
    # that module than its imports. By the time a request arrives it is whole.
    from threefold.interfaces import api_handlers as router

    body = router._parse_body(event)
    # Present means a project was named, as explain reads it: a null, a number
    # or a list is refused rather than read as "no project".
    if "project" in body and not isinstance(body["project"], str):
        raise InvalidRequestError(
            "project must be a string. Leave it out to draft for the shared rules.", "project"
        )
    description = body.get("description")
    # Claimed only for a request that could draft at all: one the validation
    # below will refuse costs nothing and must not spend the day's budget.
    if isinstance(description, str) and description.strip() and not _claim_budget(router):
        _count("budget", 0, time.monotonic())
        problem = rfc7807_error(
            429,
            "Drafting Budget Spent",
            "The drafting calls this account may make today are spent, so no model was asked and "
            "nothing was drafted or saved. The budget resets at midnight UTC. A rule can still be "
            "written by hand and tried with POST /rules/explain.",
            path,
            error_type="urn:threefold:error:draft-budget-spent",
        )
        problem.update({"model": None, "saved": False})
        return router.build_response(429, problem)
    client = _drafting_client()
    model = getattr(client, "model_id", None)

    def rules_in_force(project: Optional[str]):
        # Only a caller who may read the rules reaches this: anyone on a stack
        # whose reads are public, and the operator on one whose reads are not.
        # So the draft is checked against the ids in force on every stack.
        rules, _source = router._evaluator.rules_in_force(project)
        return rules

    started = time.monotonic()
    try:
        drafted = draft_rule(
            body.get("description"),
            body.get("project"),
            body.get("examples"),
            client=client,
            existing_rules=rules_in_force,
        )
    except ModelUnavailableError as unavailable:
        logger.warning("A rule draft was refused because the model is unavailable: %s", unavailable)
        _count("unavailable", unavailable.calls, started)
        problem = rfc7807_error(
            503,
            "Model Unavailable",
            "Amazon Bedrock could not be asked or did not answer, so no rule was drafted and "
            "nothing was saved. Threefold does not stand in for the model with a rule of its own. "
            "Write the rule by hand and try it with POST /rules/explain, or ask again later.",
            path,
            error_type="urn:threefold:error:model-unavailable",
        )
        problem.update({"source": SOURCE_UNAVAILABLE, "model": model, "saved": False})
        return router.build_response(503, problem)
    except NotALayeringRuleError as declined:
        _count("declined", declined.attempts, started)
        problem = rfc7807_error(
            422,
            "Not A Layering Rule",
            "The model read the description as a boundary a layering rule cannot express, so "
            f"nothing was drafted: {declined.reason}",
            path,
            error_type="urn:threefold:error:not-a-layering-rule",
        )
        problem.update(
            {
                "source": SOURCE_BEDROCK,
                "model": model,
                "attempts": declined.attempts,
                "saved": False,
                "unsupported": UNSUPPORTED,
            }
        )
        return router.build_response(422, problem)
    except UndraftableRuleError as undraftable:
        _count("undraftable", undraftable.attempts, started)
        problem = rfc7807_error(
            502,
            "No Usable Draft",
            f"{undraftable} Each answer was checked as POST /rules would check it, and the "
            "reasons are in problems.",
            path,
            error_type="urn:threefold:error:undraftable-rule",
        )
        problem.update(
            {
                "problems": undraftable.problems,
                "rejected_draft": undraftable.rejected,
                "validation": {"ok": False, "errors": undraftable.problems},
                "source": SOURCE_BEDROCK,
                "model": model,
                "attempts": undraftable.attempts,
                "saved": False,
            }
        )
        return router.build_response(502, problem)

    drafted["warnings"] = router._project_warnings(drafted.get("project"))
    _count("drafted", drafted.get("attempts", 0), started)
    return router.build_response(200, drafted)


def _claim_budget(router: Any) -> bool:
    """Claims one draft's model calls from the account's daily budget.

    A store that cannot count (an older repository in a test double) lets the
    draft through, bounded by the per-container cap as before; the store's own
    fallback to memory keeps the bound per container through an outage.
    """
    repo = getattr(router._evaluator, "session_repo", None)
    claim = getattr(repo, "claim_draft_calls", None)
    if claim is None:
        return True
    return bool(claim(MODEL_CALLS_PER_DRAFT, DRAFT_MODEL_CALLS_PER_DAY))


def _count(outcome: str, model_calls: int, started: float) -> None:
    """One metric per draft, by an outcome from a fixed set, so the dimension stays bounded.

    The latency is the whole draft, model calls and all. It is how the drafting
    read timeout gets measured in the field: a draft that answers 503 after
    about six seconds ran into it.
    """
    emit_threefold_emf_metrics(
        {
            "RuleDrafts": 1.0,
            "RuleDraftModelCalls": float(model_calls),
            "RuleDraftLatencyMs": round((time.monotonic() - started) * 1000.0, 1),
        },
        dimensions={"Outcome": outcome},
        namespace="Threefold/Drafting",
    )
