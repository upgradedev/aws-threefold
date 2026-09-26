"""The application's routes: the ledger a page at a time, projects, and their stages.

`handle` is called by the Lambda router before its final 404 and answers only
the paths below, returning None for anything else. Who may call them is not
decided here: the security middleware has already run by the time a request
arrives, and its rules for these paths are its own.

Charts and tiles are read from the daily rollups; lists of calls from the
ledger. The one tile that cannot come from a rollup, self-correction, is read
from the ledger with a bounded read that says when it stopped short. Every
ledger row that leaves here has been through `public_row`, as the rows of
/api/insights are.
"""
from __future__ import annotations

import datetime
import functools
import inspect
import json
import logging
import os
import re
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import unquote

from threefold.application import ledger, rollups
from threefold.application import projects as stages
from threefold.application.dtos import InvalidRequestError
from threefold.application.labels import UNLABELLED, is_labelled, public_row
from threefold.application.rule_keys import kind_of, stored_rule_keys
from threefold.application.sandbox import create_sandbox
from threefold.infrastructure.idempotency import global_idempotency_cache
from threefold.infrastructure.security_middleware import rfc7807_error

logger = logging.getLogger("threefold.api.app")

PROJECT_PATH = re.compile(r"^/api/projects/([^/]+)(?:/(promote|demote|reviews))?$")

# The windows the contract fixes for each read.
OVERVIEW_DAYS = (7, 1, 30)
DECISION_DAYS = (7, 1, ledger.MAX_DAYS)
PROJECT_DAYS = (14, 1, 30)
PROJECTS_LISTING_DAYS = 7
MAX_CURSOR_LENGTH = 1000

# The proof page's snapshot, written by scripts/build_proof.py. It sits beside
# openapi.json rather than under assets/: the edge sends every *.json path to
# the function, so a JSON file in the bucket would be reachable only while the
# behaviors stay in their present order, and the asset route serves scripts,
# styles and icons only. Served as the function reads it, like openapi.json.
PROOF_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "proof.json")

Handler = Callable[[Dict[str, Any], str, Optional[str]], Dict[str, Any]]


def _handlers():
    """The router module, for its evaluator and its response helpers.

    Imported when a request arrives rather than when this module loads: the
    router imports this module lazily too, and neither may need the other to
    have finished loading first.
    """
    from threefold.interfaces import api_handlers

    return api_handlers


def _evaluator():
    return _handlers()._evaluator


def _repo(name: str):
    return getattr(_evaluator().session_repo, name, None)


def _respond(status: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return _handlers().build_response(status, body)


def _problem(status: int, title: str, detail: str, path: str, kind: str, name: Optional[str] = None) -> Dict[str, Any]:
    return _respond(
        status,
        rfc7807_error(
            status,
            title,
            detail,
            path,
            error_type=f"urn:threefold:error:{kind}",
            invalid_params=[{"name": name, "reason": detail}] if name else None,
        ),
    )


def _query(event: Dict[str, Any]) -> Dict[str, Any]:
    return event.get("queryStringParameters") or {}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def handle(path: str, method: str, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Answers one of the application's routes, or None when the path is not one."""
    route = _match(path, method)
    if route is None:
        return None
    handler, project = route
    try:
        response = handler(event, path, project)
    except (InvalidRequestError, stages.ConfigError) as invalid:
        return _problem(400, "Bad Request", invalid.detail, path, "bad-request", invalid.name)
    if method == "POST" and response.get("statusCode") == 200:
        _remember(event, method, path, response)
    return response


def _remember(event: Dict[str, Any], method: str, path: str, response: Dict[str, Any]) -> None:
    """Stores a write's answer under its Idempotency-Key, as the router's own writes do.

    The router answers a repeated key from the cache before any route runs, so
    a retried promotion is not a second history entry and a retried sandbox is
    the same sandbox. The key is scoped by method and path exactly as the
    router scopes it when it looks one up.
    """
    headers = event.get("headers") or {}
    key = next(
        (headers[name] for name in ("Idempotency-Key", "idempotency-key", "X-Idempotency-Key", "x-idempotency-key")
         if headers.get(name)),
        None,
    )
    if not key:
        return
    global_idempotency_cache.set(f"{method} {path} {key}", response["statusCode"], json.loads(response["body"]))


def _match(path: str, method: str) -> Optional[Tuple[Handler, Optional[str]]]:
    fixed = FIXED_ROUTES.get((method, path))
    if fixed is not None:
        return fixed, None
    found = PROJECT_PATH.match(path)
    if found is None:
        return None
    name, action = unquote(found.group(1)), found.group(2)
    if method == "GET" and action is None:
        return _get_project, name
    if method == "POST":
        return PROJECT_WRITES[action], name
    return None


# ---------------------------------------------------------------- reads


def _get_overview(event: Dict[str, Any], path: str, _: Optional[str]) -> Dict[str, Any]:
    query = _query(event)
    days = ledger.bounded_int(query, "days", *OVERVIEW_DAYS)
    project = ledger.DecisionFilters.from_query({"project": query.get("project")}).project
    evaluator = _evaluator()
    items = evaluator.list_rollups(days=days, project=project)
    payload = rollups.overview(items, evaluator.list_project_configs(), days, project=project)
    payload["self_correction"] = _self_correction(days, project)
    return _respond(200, payload)


def _self_correction(days: int, project: Optional[str]) -> Dict[str, Any]:
    """The self-correction figure for a window, or the unread one when the ledger cannot be read.

    Best effort, like a rollup: the tiles beside it are exact without it, so a
    ledger that fails to answer costs this one figure, marked incomplete, and
    never the overview or the project page.
    """
    reader = _repo("read_decision_day")
    if reader is None:
        return ledger.self_correction_unread()
    try:
        return ledger.self_correction(_reader_that_fails_loudly(reader), days, project)
    except Exception as exc:  # a figure never fails the page it is on
        logger.warning("Could not read the ledger for self-correction: %s", exc)
        return ledger.self_correction_unread()


def _reader_that_fails_loudly(reader: Callable[..., Any]) -> Callable[..., Any]:
    """The ledger reader, asked to raise on a failed query where the store can.

    The store answers a failed query with the rows its container holds, so a
    listing still shows something. For this figure that would read a ledger it
    could not reach as a complete window with no refusal in it; raised, the
    failure gives the unread figure, which says it is not complete.
    """
    try:
        accepts = "raise_errors" in inspect.signature(reader).parameters
    except (TypeError, ValueError):
        accepts = False
    return functools.partial(reader, raise_errors=True) if accepts else reader


def _get_decisions(event: Dict[str, Any], path: str, _: Optional[str]) -> Dict[str, Any]:
    query = _query(event)
    filters = ledger.DecisionFilters.from_query(query)
    cursor = query.get("cursor") or None
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > MAX_CURSOR_LENGTH):
        raise InvalidRequestError("cursor is not one this service issued.", "cursor")
    reader = _repo("read_decision_day")
    if reader is None:
        return _respond(200, {"items": [], "next_cursor": None})
    payload = ledger.page_decisions(
        reader,
        ledger.bounded_int(query, "days", *DECISION_DAYS),
        filters,
        ledger.bounded_int(query, "limit", ledger.DEFAULT_LIMIT, 1, ledger.MAX_LIMIT),
        cursor,
    )
    return _respond(200, payload)


def _required_query(query: Dict[str, Any], name: str) -> str:
    value = query.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise InvalidRequestError(f"{name} is required.", name)
    return value.strip()


def _get_decision(event: Dict[str, Any], path: str, _: Optional[str]) -> Dict[str, Any]:
    query = _query(event)
    timestamp = _required_query(query, "timestamp")
    verdict_id = _required_query(query, "verdict_id")
    reader = _repo("get_decision")
    row = reader(timestamp, verdict_id) if reader is not None else None
    if row is None:
        return _problem(404, "No Such Decision", "No decision with that timestamp and verdict_id is recorded.",
                        path, "decision-not-found")
    shown = ledger.shown_row(row)
    return _respond(
        200,
        {"decision": shown, "session": _session_summary(shown.get("session_id")), "rule": _rule_definition(shown)},
    )


def _session_summary(session_id: Any) -> Optional[Dict[str, Any]]:
    if not session_id:
        return None
    session = _evaluator().session_repo.get_session(str(session_id))
    if session is None:
        return None
    return {
        "session_id": session.session_id,
        # The store keeps the last fifty calls, so this saturates there, as the
        # sessions listing says of the same number.
        "calls": len(session.history),
        "cost_usd": session.total_cost_usd,
        "is_tripped": session.is_tripped,
    }


def _rule_definition(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The layering rule the row is keyed by, as that project's rules define it today."""
    project = row.get("project_name")
    rules, _ = _evaluator().rules_in_force(project if is_labelled(project) else None)
    for rule in rules:
        if rule.get("id") == row.get("rule_key"):
            return dict(rule)
    return None


def _get_projects(event: Dict[str, Any], path: str, _: Optional[str]) -> Dict[str, Any]:
    evaluator = _evaluator()
    items = evaluator.list_rollups(days=PROJECTS_LISTING_DAYS)
    return _respond(200, {"projects": rollups.projects_listing(items, evaluator.list_project_configs())})


def _get_project(event: Dict[str, Any], path: str, name: Optional[str]) -> Dict[str, Any]:
    if not (is_labelled(name) or name == UNLABELLED):
        return _problem(404, "No Such Project", "That name is not one this deployment records calls under.",
                        path, "project-not-found")
    evaluator = _evaluator()
    labelled = is_labelled(name)
    config = evaluator.project_config(name, fresh=True) if labelled else None
    rules, _ = evaluator.rules_in_force(name if labelled else None)
    days = ledger.bounded_int(_query(event), "days", *PROJECT_DAYS)
    items = evaluator.list_rollups(days=days, project=name)
    readiness = rollups.readiness(items, config, rules, name)
    readiness["summary"]["self_correction"] = _self_correction(days, name)
    return _respond(200, {"project": name, "config": config, "readiness": readiness})


def _get_proof(event: Dict[str, Any], path: str, _: Optional[str]) -> Dict[str, Any]:
    """GET /proof.json: the proof page's snapshot as scripts/build_proof.py wrote it.

    Absent is a 404 the page reads as "not measured yet", never an empty
    document it could mistake for a measurement of nothing.
    """
    try:
        with open(PROOF_PATH, "r", encoding="utf-8") as handle:
            proof = json.load(handle)
    except FileNotFoundError:
        return _problem(404, "Not Measured Yet",
                        "No proof snapshot is deployed here: scripts/build_proof.py writes one.",
                        path, "proof-not-found")
    except (OSError, ValueError) as unreadable:
        logger.error("The proof snapshot could not be read: %s", unreadable)
        proof = None
    if not isinstance(proof, dict):
        return _problem(500, "Proof Unreadable", "The proof snapshot deployed here is not a JSON object.",
                        path, "proof-unreadable")
    return _respond(200, proof)


# ---------------------------------------------------------------- writes


def _configurable(name: Optional[str]) -> str:
    """A name a configuration can be saved under: one AllowedProjectPattern admits."""
    if not is_labelled(name):
        raise InvalidRequestError(
            "The project must match this deployment's AllowedProjectPattern, or its calls are recorded as "
            "'unlabelled' and no stage could ever apply to them.",
            "project",
        )
    return str(name)


def _body(event: Dict[str, Any]) -> Dict[str, Any]:
    return _handlers()._parse_body(event)


def _project_keys(name: str) -> list:
    rules, _ = _evaluator().rules_in_force(name)
    return stages.project_rule_keys(rules)


def _sandbox_ttl(config: Optional[Dict[str, Any]]) -> Optional[int]:
    """What is left of a sandbox's day, so changing its stage never extends it."""
    if not config or not config.get("sandbox"):
        return None
    try:
        created = datetime.datetime.fromisoformat(str(config.get("created_at")))
    except ValueError:
        return stages.SANDBOX_TTL_SECONDS
    ends = created + datetime.timedelta(seconds=stages.SANDBOX_TTL_SECONDS)
    left = (ends - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    return max(60, int(left))


def _save(name: str, current: Optional[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    # A name in the sandbox pattern is a sandbox however it was first written.
    # On the public stack anyone may write one, and without this a stranger
    # could keep a configuration under a sandbox's name for ever.
    if stages.SANDBOX_PATTERN.match(name):
        config = dict(config, sandbox=True)
    saved = _evaluator().save_project_config(name, config, ttl_seconds=_sandbox_ttl(config))
    return _respond(200, {"project": name, "config": saved})


def _post_project(event: Dict[str, Any], path: str, name: Optional[str]) -> Dict[str, Any]:
    name = _configurable(name)
    body = _body(event)
    stage = stages.read_stage(body["stage"]) if body.get("stage") is not None else None
    observe_rules = None
    if body.get("observe_rules") is not None:
        observe_rules = stages.read_rule_keys(body["observe_rules"], _project_keys(name), "observe_rules")
    current = _evaluator().project_config(name, fresh=True)
    return _save(name, current, stages.updated(current, _now(), stage=stage, observe_rules=observe_rules))


def _post_promote(event: Dict[str, Any], path: str, name: Optional[str]) -> Dict[str, Any]:
    name = _configurable(name)
    body = _body(event)
    if "enforce" not in body:
        raise InvalidRequestError("enforce is required: the rule keys to enforce, possibly none.", "enforce")
    keys = _project_keys(name)
    enforce = stages.read_rule_keys(body["enforce"], keys, "enforce")
    current = _evaluator().project_config(name, fresh=True)
    by = ledger.credential_hash(event.get("headers") or {})
    return _save(name, current, stages.promoted(current, _now(), by, enforce, keys))


def _post_demote(event: Dict[str, Any], path: str, name: Optional[str]) -> Dict[str, Any]:
    name = _configurable(name)
    _body(event)  # parsed so a malformed body is refused like any other POST
    current = _evaluator().project_config(name, fresh=True)
    by = ledger.credential_hash(event.get("headers") or {})
    return _save(name, current, stages.demoted(current, _now(), by, _project_keys(name)))


def _post_reviews(event: Dict[str, Any], path: str, name: Optional[str]) -> Dict[str, Any]:
    name = _configurable(name)
    items, skipped = ledger.read_review_items(_body(event))
    reviewed_by = ledger.credential_hash(event.get("headers") or {})
    now = _now()
    updated = 0
    for item in items:
        reason = _apply_review(name, item, reviewed_by, now)
        if reason:
            skipped.append({"verdict_id": item.verdict_id, "reason": reason})
        else:
            updated += 1
    return _respond(200, {"updated": updated, "skipped": skipped})


def _apply_review(project: str, item: ledger.ReviewItem, reviewed_by: str, now: str) -> Optional[str]:
    """Labels one call, or says why it was skipped."""
    reader, labeller = _repo("get_decision"), _repo("label_decision")
    if reader is None or labeller is None:
        return "not-stored"
    row = reader(item.timestamp, item.verdict_id)
    if row is None:
        return "not-found"
    if public_row(row).get("project_name") != project:
        return "other-project"
    if not ledger.is_flagged(row):
        return "nothing-flagged"
    label = None if item.label == "clear" else item.label
    before = labeller(
        item.timestamp,
        item.verdict_id,
        project,
        label,
        note=item.note if label else "",
        reviewed_by=reviewed_by,
        reviewed_at=now,
    )
    if before is None:
        return "other-project"
    _count_review(project, item.timestamp[:10], before, label)
    return None


def _count_review(project: str, day: str, before: Dict[str, Any], label: Optional[str]) -> None:
    """Moves the label's counts in its day's rollup, best effort, like any rollup."""
    adjust = _repo("adjust_rollup")
    deltas = rollups.review_deltas(kind_of(before), stored_rule_keys(before), before.get("review"), label)
    if adjust is None or not deltas:
        return
    try:
        adjust(day, project, deltas)
    except Exception as exc:  # pragma: no cover - a rollup never fails the request
        logger.warning("Could not count a review into the rollup: %s", exc)


def _post_sandbox(event: Dict[str, Any], path: str, _: Optional[str]) -> Dict[str, Any]:
    try:
        created = create_sandbox(_evaluator())
    except ValueError as unusable:
        return _problem(409, "Sandbox Unavailable", str(unusable), path, "sandbox-unavailable")
    return _respond(200, created)


FIXED_ROUTES: Dict[Tuple[str, str], Handler] = {
    ("GET", "/api/overview"): _get_overview,
    ("GET", "/api/decisions"): _get_decisions,
    ("GET", "/api/decision"): _get_decision,
    ("GET", "/api/projects"): _get_projects,
    ("POST", "/api/sandbox"): _post_sandbox,
    ("GET", "/proof.json"): _get_proof,
}

PROJECT_WRITES: Dict[Optional[str], Handler] = {
    None: _post_project,
    "promote": _post_promote,
    "demote": _post_demote,
    "reviews": _post_reviews,
}
