"""`POST /rules/draft`: a layering rule drafted from a sentence, tried, and never saved.

The body is `{description, project?, examples?}`. The answer is one rule in the
format `POST /rules` accepts, set to observe, with the verdict it gives on each
example; see `threefold.application.rule_drafter` for how it is made and checked.

Who may call it. The route is open on the public stack, as `POST /rules/explain`
is, and it is decided rather than inherited: it is not listed among the reads in
the security middleware, so it takes the default for an unlisted POST, open
where no key is enforced. It may stay open because it changes nothing. No rule
is saved, no verdict is issued and no ledger row is written, and the draft goes
back only to the caller who asked for it; putting it in force is a separate
`POST /rules`, which needs the operator on every stack. The public stack has no
operator key at all, so a closed route could never be tried there by the people
the demonstration is for.

What sets it apart from explain is the bill: explain costs CPU, and every draft
is one or two model calls on the account. The brakes are, in order, the per-IP
token bucket every request passes (sixty in a burst, then two a second), the
caps on what a request may carry (a 600 character description, five examples of
at most 4,000 characters, and only their paths and imports reach the model), a
400 token answer, at most one repair, and a cap on drafting calls per container
kept apart from the explanations' cap, so a flood of drafts can never leave a
refusal without its sentence. The worst a container can be made to spend is that
cap times two short prompts and two short answers.

On a stack that keeps its reads private the route is still reachable, as every
unlisted POST there is, so it reads nothing private there: the draft is not
checked against the rules in force, whose ids a clash would disclose, and
`POST /rules` checks it when the operator saves it. Closing the route is one
entry in the security middleware: listed among the protected writes it needs the
operator on every stack, and listed among the page reads it follows PublicReads.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from threefold.application.rule_drafter import (
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
from threefold.infrastructure.security_middleware import reads_are_public, rfc7807_error

logger = logging.getLogger("threefold.api.drafts")

DRAFT_PATH = "/rules/draft"

# Drafting calls one container may make before it answers 503 until recycled.
# Two per draft at most, so about thirty drafts, and a separate budget from the
# 200 the verdict explanations share.
DRAFT_CALLS_PER_CONTAINER = 60

_client: Optional[BedrockGovernanceClient] = None


def _drafting_client() -> BedrockGovernanceClient:
    """The container's drafting client, built on the first draft rather than at import.

    Its own instance, not the one the verdicts use, so the call cap above is its
    own. Built lazily because most containers never draft, and constructing a
    Bedrock client costs a cold start time it would never repay.
    """
    global _client
    if _client is None:
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
    client = _drafting_client()
    model = getattr(client, "model_id", None)

    def rules_in_force(project: Optional[str]):
        if not reads_are_public():
            return None
        rules, _source = router._evaluator.rules_in_force(project)
        return rules

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
        _count("unavailable", unavailable.calls)
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
        _count("declined", declined.attempts)
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
        _count("undraftable", undraftable.attempts)
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
    _count("drafted", drafted.get("attempts", 0))
    return router.build_response(200, drafted)


def _count(outcome: str, model_calls: int) -> None:
    """One metric per draft, by an outcome from a fixed set, so the dimension stays bounded."""
    emit_threefold_emf_metrics(
        {"RuleDrafts": 1.0, "RuleDraftModelCalls": float(model_calls)},
        dimensions={"Outcome": outcome},
        namespace="Threefold/Drafting",
    )
