"""AWS Lambda proxy handler exposing REST endpoints for Threefold.

Includes Zero-Trust security middleware, AWS CloudWatch EMF metrics,
Universal Multi-Agent Adapter (OpenAI / Anthropic tool-use formats),
and RFC 7807 Problem Details error handling.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict
from urllib.parse import unquote

from threefold.application.audit_issuer import AuditIssuer
from threefold.application.bedrock_reviewer import BedrockArchitecturalReviewer
from threefold.application.dtos import InvalidRequestError, PolicyConfigDTO, ToolCallRequestDTO
from threefold.application.evaluator import (
    RULES_REFRESH_SECONDS,
    GovernanceEvaluator,
    SessionNotHaltedError,
    UnusableRulesError,
)
from threefold.application.insights import summarise, with_layering_coverage
from threefold.application.labels import is_labelled, project_label, public_row
from threefold.domain.imports import LANGUAGE_BY_SUFFIX, declared_imports
from threefold.domain.boundary_guard import MAX_PATHLIKE_LENGTH, looks_like_path, redact_secrets
from threefold.domain.layering_rules import (
    DEFAULT_RULES,
    UNSUPPORTED,
    rules_for_path,
    validate_rules,
    violations as layering_violations,
)
from threefold.domain.exceptions import EmptyAttestationException
from threefold.infrastructure.bedrock_client import BedrockGovernanceClient
from threefold.infrastructure.idempotency import global_idempotency_cache
from threefold.infrastructure.metrics_emf import emit_threefold_emf_metrics
from threefold.infrastructure.security_middleware import (
    INSIGHTS_READ_PATHS,
    SESSIONS_READ_PATHS,
    rfc7807_error,
    validate_request_security,
)
from threefold.interfaces import access_routes
from threefold.interfaces import draft_routes

logger = logging.getLogger("threefold.api")
logger.setLevel(logging.INFO)

# Global singleton evaluator for Lambda container reuse
_evaluator = GovernanceEvaluator()
_bedrock_client = BedrockGovernanceClient()
_reviewer = BedrockArchitecturalReviewer(_bedrock_client)

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With, X-API-Key, Idempotency-Key",
}


WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

# The hook is the one artifact that turns this service from a demo into
# something in front of a real agent, so the deployment hands it out rather
# than telling a reader to find a repository. It lives under the packaged tree
# for that reason: anything outside CodeUri never reaches the function.
HOOKS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
# One script now serves every agent. The two claude_code_hook.py paths are the
# names it was published under, kept so an install command copied before the
# rename still fetches a working hook rather than a 404.
SERVED_SCRIPTS = {
    "/hooks/threefold_hook.py": "threefold_hook.py",
    "/hooks/claude_code_hook.py": "threefold_hook.py",
    "/claude_code_hook.py": "threefold_hook.py",
}

# What the kill switch records when the caller names neither, which is what it
# has always recorded for a caller that sent an empty body.
DEFAULT_TERMINATION_OPERATOR = "Enterprise Security Admin"
DEFAULT_TERMINATION_REASON = "Manual emergency kill-switch invoked"

# Paths the deployed stack serves as pages rather than as JSON. The dashboard sits
# at the root so the public URL opens the application itself.
WEB_ASSETS = {
    "/": "index.html",
    "/index.html": "index.html",
    "/swagger.html": "swagger.html",
    "/settings.html": "settings.html",
    "/sessions.html": "sessions.html",
    "/connect.html": "connect.html",
    "/console.html": "console.html",
    "/rules.html": "rules.html",
}


def _read_web_asset(filename: str, stage: str) -> Any:
    """Loads a page and tells it which base path the API is reachable on.

    The stage prefix is injected here rather than derived in the browser because
    the page's own URL does not distinguish /prod from /prod/ reliably.
    """
    asset_path = os.path.join(WEB_ROOT, filename)
    if not os.path.isfile(asset_path):
        return None
    with open(asset_path, "r", encoding="utf-8") as handle:
        markup = handle.read()
    base_path = f"/{stage}" if stage and stage != "$default" else ""
    return markup.replace("__THREEFOLD_BASE_PATH__", base_path)


def build_script_response(source: str, filename: str) -> Dict[str, Any]:
    """Hands back a script as text a browser will show and a shell can pipe.

    Served as plain text rather than as an attachment so a reader can inspect
    what they are about to run before they run it, which for a file that sits in
    front of an agent's tool calls is the only defensible default.
    """
    headers = dict(CORS_HEADERS)
    headers["Content-Type"] = "text/plain; charset=utf-8"
    headers["Cache-Control"] = "no-cache"
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return {"statusCode": 200, "headers": headers, "body": source}


def build_html_response(status_code: int, markup: str) -> Dict[str, Any]:
    """Returns a page, with the CORS headers the JSON routes also send."""
    headers = dict(CORS_HEADERS)
    headers["Content-Type"] = "text/html; charset=utf-8"
    headers["Cache-Control"] = "no-cache"
    return {"statusCode": status_code, "headers": headers, "body": markup}


def build_response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """Formats standardized API Gateway proxy response with CORS and problem details."""
    content_type = "application/json"
    if status_code >= 400 and "type" in body and "title" in body:
        content_type = "application/problem+json"

    headers = dict(CORS_HEADERS)
    headers["Content-Type"] = content_type

    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(body, sort_keys=True),
    }


def _request_id(event: Dict[str, Any], context: Any) -> str:
    """The id an operator can find this request by in the function's log."""
    context_id = getattr(context, "aws_request_id", None)
    gateway_id = (event.get("requestContext") or {}).get("requestId")
    return str(gateway_id or context_id or uuid.uuid4().hex)


def _internal_error(path: str, request_id: str) -> Dict[str, Any]:
    """The only thing a caller learns about a failure nobody anticipated.

    The exception text used to be the detail, which handed a stranger table
    names, library internals and whatever a stack trace happened to mention.
    It goes to the log under the request id instead, and the caller gets the id.
    """
    problem = rfc7807_error(
        500,
        "Internal Server Error",
        "The service could not complete this request. The cause is in the function's "
        "log under this request id.",
        path,
        error_type="urn:threefold:error:internal-error",
    )
    problem["request_id"] = request_id
    return problem


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Primary AWS Lambda event router.

    HEAD is answered as GET with the body removed, so a link checker or an
    uptime probe gets the status and headers a browser would. It used to fall
    through to 404, which a probe reads as the page being gone.
    """
    request_id = _request_id(event, context)
    method = str(
        event.get("httpMethod")
        or ((event.get("requestContext") or {}).get("http") or {}).get("method")
        or "GET"
    ).upper()
    is_head = method == "HEAD"
    try:
        response = _route(event, "GET" if is_head else method, request_id)
    except Exception:
        # A backstop for anything raised before a route's own handler runs,
        # such as headers that are not a mapping.
        logger.exception("Unhandled error, request %s", request_id)
        path = str(event.get("path") or event.get("rawPath") or "/")
        response = build_response(500, _internal_error(path, request_id))
    if is_head:
        response = dict(response)
        response["body"] = ""
    return response


def _route(event: Dict[str, Any], http_method: str, request_id: str) -> Dict[str, Any]:
    """Routes one request. HEAD arrives here already turned into GET."""
    start_time = time.time()
    raw_path = event.get("path") or event.get("rawPath", "/")
    headers = event.get("headers") or {}
    client_ip = (
        event.get("requestContext", {}).get("http", {}).get("sourceIp")
        or event.get("requestContext", {}).get("identity", {}).get("sourceIp")
        or "127.0.0.1"
    )

    # Normalization: API Gateway prefixes rawPath with the stage name when the API
    # is deployed to a named stage (for example /prod/status), so strip it before routing.
    # Repeated slashes are collapsed first: the stack's ApiEndpoint output ends
    # in "/", so anything that appends "/status" to it asks for /prod//status.
    path = re.sub(r"/{2,}", "/", raw_path.split("?")[0]).rstrip("/")
    stage = event.get("requestContext", {}).get("stage", "")
    if stage and stage != "$default" and (path == f"/{stage}" or path.startswith(f"/{stage}/")):
        path = path[len(stage) + 1:]
    if not path:
        path = "/"

    # Handle CORS pre-flight
    if http_method == "OPTIONS":
        return build_response(200, {"status": "OK"})

    raw_body = event.get("body") or ""
    if isinstance(raw_body, str) and event.get("isBase64Encoded"):
        import base64
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except Exception:
            return build_response(400, rfc7807_error(400, "Bad Request", "Invalid Base64 payload", path))
        # The routes read the body off the event, so the decoded text has to be
        # put back there; it used to be measured decoded and parsed encoded.
        event = dict(event, body=raw_body, isBase64Encoded=False)

    payload_size = len(raw_body.encode("utf-8")) if isinstance(raw_body, str) else 0

    # 1. Zero-Trust Security & Rate Limiting Guard
    is_authorized, security_problem = validate_request_security(
        headers=headers,
        client_ip=client_ip,
        path=path,
        payload_size_bytes=payload_size,
        # The method is part of the decision: reading the policy is open to
        # anyone, and writing it is not, on the same path.
        method=http_method,
    )
    if not is_authorized and security_problem:
        return build_response(security_problem["status"], security_problem)

    # Idempotency check for mutating methods
    idempotency_key = (
        headers.get("Idempotency-Key")
        or headers.get("idempotency-key")
        or headers.get("X-Idempotency-Key")
        or headers.get("x-idempotency-key")
    )
    # Keyed by method and route as well as the caller's key. Keyed by the key
    # alone, an anonymous POST /rules/explain carrying the key another caller had
    # used on a write was answered with that write's stored response.
    if idempotency_key:
        idempotency_key = f"{http_method} {path} {idempotency_key}"
    if idempotency_key and http_method == "POST":
        cached = global_idempotency_cache.get(idempotency_key)
        if cached:
            return build_response(cached[0], cached[1])

    try:
        # Route 0: the application itself. A visitor who opens the public URL gets
        # the dashboard, not a JSON document, and the page is told the exact API
        # base so nothing has to be configured by hand.
        if http_method == "GET" and path in WEB_ASSETS:
            page = _read_web_asset(WEB_ASSETS[path], stage)
            if page is not None:
                return build_html_response(200, page)

        # Route 0b: the hook itself. A reader who wants Threefold in front of their
        # own agent can take the script from the deployment they just watched work,
        # without a repository, an account or a package manager.
        if http_method == "GET" and path in SERVED_SCRIPTS:
            script_path = os.path.join(HOOKS_ROOT, SERVED_SCRIPTS[path])
            if not os.path.isfile(script_path):
                logger.error("The hook is missing from the package at %s", script_path)
                return build_response(
                    500,
                    rfc7807_error(
                        500,
                        "Script Unavailable",
                        "The hook was not found in this deployment.",
                        path,
                        error_type="urn:threefold:error:script-missing",
                    ),
                )
            with open(script_path, "r", encoding="utf-8") as handle:
                return build_script_response(handle.read(), SERVED_SCRIPTS[path])

        # Route 1: Health & Operational Status
        if path in ("/status", "/health") and http_method == "GET":
            emit_threefold_emf_metrics({"HealthCheck": 1.0}, namespace="Threefold/Operations")
            return build_response(
                200,
                {
                    "service": "Threefold",
                    "version": "1.0.0",
                    "status": "HEALTHY",
                    "stage": os.environ.get("STAGE", "prod"),
                    "active_rules": [
                        "SECRET_LEAKAGE_FREE",
                        "ARCHITECTURAL_BOUNDARY_SAFE",
                        "LOOP_THRASHING_FREE",
                        "BUDGET_CIRCUIT_BREAKER_SAFE",
                    ],
                    "target_track": "#workplace-efficiency",
                },
            )

        # Route 0a: the sessions the service has actually governed. The dashboard
        # reads this; a browser asking for /sessions gets the page above instead.
        # Both names come from the middleware's own table, so the route and the
        # rule about who may call it cannot name different paths.
        if path in SESSIONS_READ_PATHS and http_method == "GET":
            limit = 50
            try:
                limit = int((event.get("queryStringParameters") or {}).get("limit", 50))
            except (TypeError, ValueError):
                limit = 50
            # Labelled and hashed on the way out as well as in, because rows
            # written before the labels existed are still in the table.
            sessions = [
                public_row(row) for row in _evaluator.list_sessions(limit=max(1, min(limit, 200)))
            ]
            return build_response(
                200,
                {
                    "sessions": sessions,
                    "count": len(sessions),
                    "persistence": getattr(_evaluator.session_repo, "persistence_mode", "memory"),
                },
            )

        # Route 0c: what the gates did, aggregated the way a platform owner asks.
        # The console renders this and computes nothing of its own, so a number on
        # the page cannot disagree with the ledger it came from.
        if path in INSIGHTS_READ_PATHS and http_method == "GET":
            days = 7
            try:
                days = int((event.get("queryStringParameters") or {}).get("days", 7))
            except (TypeError, ValueError):
                days = 7
            days = max(1, min(days, 30))
            # Every row is reduced before it is counted, not only the per-developer
            # table: the recent refusals and observations are whole ledger rows,
            # and a raw name reached the page through them.
            decisions = [public_row(row) for row in _evaluator.list_decisions(days=days, limit=2000)]
            payload = summarise(decisions, days)
            _evaluator.refresh_rules_if_stale()
            payload = with_layering_coverage(
                payload,
                _evaluator.layering_rules,
                sorted(LANGUAGE_BY_SUFFIX),
            )
            payload["persistence"] = getattr(_evaluator.session_repo, "persistence_mode", "memory")
            return build_response(200, payload)

        # Route 1a: Deep Readiness Probe (/readyz)
        if path in ("/readyz", "/ready") and http_method == "GET":
            readiness = _evaluator.check_readiness(bedrock_client=_bedrock_client)
            # A readiness probe that always answers 200 cannot be alarmed on, so a
            # degraded result is reported with the status code that says so.
            code = 200 if readiness.status == "READY" else 503
            return build_response(code, readiness.to_dict())

        # Route 1b: OpenAPI JSON Specification. The spec lives beside the pages that
        # read it, inside the deployment package. It used to be loaded from docs/,
        # which sits outside CodeUri and is never uploaded, so every deployed
        # request fell through to a two-line placeholder that read like a finished
        # spec. There is no placeholder now: a missing file is reported as one.
        if path in ("/openapi.json", "/docs/openapi.json") and http_method == "GET":
            openapi_spec_path = os.path.join(WEB_ROOT, "openapi.json")
            if not os.path.isfile(openapi_spec_path):
                logger.error("The OpenAPI document is missing from the package at %s", openapi_spec_path)
                return build_response(
                    500,
                    rfc7807_error(
                        500,
                        "Specification Unavailable",
                        "The OpenAPI document was not found in this deployment.",
                        path,
                        error_type="urn:threefold:error:spec-missing",
                    ),
                )
            with open(openapi_spec_path, "r", encoding="utf-8") as f:
                return build_response(200, json.load(f))

        # Route 2: Single Tool Call Evaluation (Core Governance Gate)
        if path == "/evaluate-tool-call" and http_method == "POST":
            body = _parse_body(event)
            # The id names a row on the sessions page, and naming one creates it.
            _check_named_session(body)
            request = ToolCallRequestDTO.from_payload(body)

            if request.projected_input_tokens < 0 or request.projected_output_tokens < 0:
                return build_response(
                    400,
                    rfc7807_error(
                        400,
                        "Invalid Parameter",
                        "Token counts cannot be negative numbers",
                        path,
                        invalid_params=[{"name": "projected_tokens", "reason": "Must be >= 0"}],
                    ),
                )

            if request.budget_usd <= 0.0:
                return build_response(
                    400,
                    rfc7807_error(
                        400,
                        "Invalid Parameter",
                        "Budget must be greater than $0.00",
                        path,
                        invalid_params=[{"name": "budget_usd", "reason": "Must be > 0"}],
                    ),
                )

            result = _evaluator.evaluate_tool_call(request)
            explanation, explanation_source = _reviewer.explain(request, result)
            result.bedrock_explanation = explanation
            result.explanation_source = explanation_source
            result.persistence = getattr(_evaluator.session_repo, "persistence_mode", "memory")
            result.warnings = list(request.warnings)

            latency_ms = (time.time() - start_time) * 1000.0
            emit_threefold_emf_metrics(
                {
                    "ToolCallsEvaluated": 1.0,
                    "VerdictApproved": 1.0 if result.status == "APPROVED" else 0.0,
                    "CircuitBreakerTripped": 1.0 if result.session_tripped else 0.0,
                    "CurrentSessionCostUSD": result.current_session_cost_usd,
                    "LatencyMs": latency_ms,
                },
                # Already labelled, so a caller cannot mint a metric per request
                # by sending a new project name each time.
                dimensions={"Project": request.project_name, "Environment": "Production"},
            )
            return build_response(200, result.to_dict())

        # Route 2b: Universal Multi-Agent Adapter (OpenAI / Anthropic format)
        if path in ("/adapter/universal-tool-call", "/universal-eval") and http_method == "POST":
            body = _parse_body(event)
            _check_named_session(body)

            # Extract payload: handle nested tool_call or root payload. An
            # envelope that also names a tool call beside the one it wraps is
            # refused rather than read, because only the wrapped one would be
            # judged: `{"tool_call": {...}, "tool_calls": [<the real call>]}`
            # would answer for the empty one and leave the other's arguments
            # unread, which is the shadowing the shapes below are refused for.
            tc = body.get("tool_call", body)
            if not isinstance(tc, dict):
                tc = body
            elif tc is not body:
                stray = _tool_call_shapes({k: v for k, v in body.items() if k != "tool_call"})
                if stray:
                    raise InvalidRequestError(_ambiguous_tool_call(["tool_call"] + stray), "tool_call")

            # Read, or refused. A shape this route cannot read used to be
            # evaluated as unknown_tool with no arguments at all, and a call
            # with no arguments passes every gate: the two shapes OpenAI
            # actually returns, a message-level function_call and a tool_calls
            # array, both landed there and came back APPROVED with a credential
            # in the payload nobody had read.
            tool_name, tool_args = _universal_tool_call(tc)

            # Infer action type
            action_type = "FILE_READ"
            lower_tool = tool_name.lower()
            if any(k in lower_tool for k in ("write", "edit", "create", "modify", "save")):
                action_type = "FILE_WRITE"
            elif any(k in lower_tool for k in ("exec", "command", "bash", "shell", "run")):
                action_type = "COMMAND_EXEC"

            req = ToolCallRequestDTO.from_payload(
                body,
                default_session_id="session-universal",
                default_input_tokens=2500,
                default_output_tokens=800,
                default_budget_usd=15.00,
                tool_name=tool_name,
                action_type=action_type,
                arguments=tool_args,
            )
            result = _evaluator.evaluate_tool_call(req)
            result.bedrock_explanation, result.explanation_source = _reviewer.explain(req, result)
            result.persistence = getattr(_evaluator.session_repo, "persistence_mode", "memory")
            result.warnings = list(req.warnings)

            emit_threefold_emf_metrics(
                {
                    "UniversalToolEvaluated": 1.0,
                    "VerdictApproved": 1.0 if result.status == "APPROVED" else 0.0,
                },
                # The tool name is the caller's to choose, so it is logged as a
                # property rather than used as a dimension: as a dimension, every
                # new name a caller invented was a new metric on the bill.
                dimensions={"Format": "Universal"},
                extra_properties={"Tool": str(tool_name)[:120]},
            )
            return build_response(200, {
                "adapter_status": "SUCCESS",
                "detected_tool_name": tool_name,
                "detected_action_type": action_type,
                "evaluation": result.to_dict(),
            })

        # Route 3: Thrashing Loop Simulation
        if path == "/simulate-loop" and http_method == "POST":
            session_id = f"sim-loop-{event.get('requestContext', {}).get('requestId', '001')[:6]}"
            req = ToolCallRequestDTO(
                session_id=session_id,
                developer_id="dev-sim",
                project_name="Acme-Sim",
                tool_name="edit_file",
                action_type="FILE_WRITE",
                arguments={"TargetFile": "src/service.py", "Instruction": "fix typo"},
            )
            _evaluator.evaluate_tool_call(req)
            _evaluator.evaluate_tool_call(req)
            third_result = _evaluator.evaluate_tool_call(req)
            third_result.bedrock_explanation, third_result.explanation_source = _reviewer.explain(req, third_result)
            third_result.persistence = getattr(_evaluator.session_repo, "persistence_mode", "memory")

            emit_threefold_emf_metrics(
                {"LoopDetected": 1.0, "CircuitBreakerTripped": 1.0},
                namespace="Threefold/SafetyTrips",
            )
            return build_response(200, third_result.to_dict())

        # Route 4: Secret Leakage Simulation
        if path == "/simulate-secret" and http_method == "POST":
            session_id = f"sim-sec-{event.get('requestContext', {}).get('requestId', '002')[:6]}"
            req = ToolCallRequestDTO(
                session_id=session_id,
                developer_id="dev-sim",
                project_name="Acme-Sim",
                tool_name="run_command",
                action_type="COMMAND_EXEC",
                arguments={"command": "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"},
            )
            result = _evaluator.evaluate_tool_call(req)
            result.bedrock_explanation, result.explanation_source = _reviewer.explain(req, result)
            result.persistence = getattr(_evaluator.session_repo, "persistence_mode", "memory")

            emit_threefold_emf_metrics(
                {"SecretLeakBlocked": 1.0},
                namespace="Threefold/Security",
            )
            return build_response(200, result.to_dict())

        # Route 5: Issue Cryptographic Governance Certificate
        if path == "/issue-certificate" and http_method == "POST":
            body = _parse_body(event)
            _check_named_session(body)
            session_id = body.get("session_id", "session-default")
            evaluations = body.get("evaluations", [])
            # Checked before the session is touched. A list of anything else
            # used to reach `e.get` and come back as a server error.
            if not isinstance(evaluations, list) or not all(isinstance(e, dict) for e in evaluations):
                raise InvalidRequestError(
                    "evaluations must be a list of verdict objects.", "evaluations"
                )
            # The issuer reads each verdict's invariants, not only its status,
            # so their shape is the caller's to get right: anything but an
            # object reached `.values()` as a server error, and the string
            # "false" would have counted as an invariant that held.
            for e in evaluations:
                invariants = e.get("rule_evaluations") or {}
                if not isinstance(invariants, dict) or not all(
                    isinstance(held, bool) for held in invariants.values()
                ):
                    raise InvalidRequestError(
                        "rule_evaluations must map each invariant to true or false.", "evaluations"
                    )
            session = _evaluator.get_or_create_session(session_id)

            parsed_evals = []
            for e in evaluations:
                from threefold.application.dtos import EvaluationResultDTO
                parsed_evals.append(
                    EvaluationResultDTO(
                        verdict_id=e.get("verdict_id", "V-001"),
                        session_id=session_id,
                        status=e.get("status", "APPROVED"),
                        risk_level=e.get("risk_level", "LOW"),
                        reason=e.get("reason", "OK"),
                        rule_evaluations=e.get("rule_evaluations") or {},
                        current_session_cost_usd=float(e.get("current_session_cost_usd", 0.0)),
                        session_tripped=bool(e.get("session_tripped", False)),
                        proof_hash=e.get("proof_hash", "hash"),
                        # Carried through, because it is what tells the issuer
                        # the gate was watching rather than enforcing. Dropping
                        # it here turned a session governed in dry run into a
                        # compliant one on the way in.
                        dry_run=bool(e.get("dry_run", False)),
                    )
                )

            # A certificate that attests to nothing is the one artifact this
            # product cannot afford to hand out, so the refusal is reported as a
            # problem the caller can read rather than as a server error.
            try:
                cert = AuditIssuer.issue_certificate(session, parsed_evals)
            except EmptyAttestationException as empty:
                emit_threefold_emf_metrics(
                    {"CertificatesRefused": 1.0},
                    namespace="Threefold/Audits",
                )
                return build_response(
                    400,
                    rfc7807_error(
                        400,
                        "Nothing To Certify",
                        empty.message,
                        path,
                        error_type="urn:threefold:error:empty-attestation",
                        invalid_params=[
                            {
                                "name": "evaluations",
                                "reason": (
                                    "Send the verdicts the certificate covers. This service "
                                    "issues one only for a session it has evaluated."
                                ),
                            }
                        ],
                    ),
                )

            emit_threefold_emf_metrics(
                {"CertificatesIssued": 1.0},
                namespace="Threefold/Audits",
            )
            return build_response(200, cert.to_dict())

        # Route 6: Inspect Session History & Metrics
        if path.startswith("/sessions/") and http_method == "GET":
            # The raw path is not URL-decoded by API Gateway, so a session id carrying
            # a space or a bracket would arrive percent-encoded and look like an id
            # nobody has ever used. Reading it back would then create an empty session
            # and report that as the caller's, which is worse than an error.
            target_session_id = unquote(path.replace("/sessions/", "").strip())
            # Read, never created. This route is open to anonymous readers, and a
            # read that stored a fresh session for any id it was asked about let
            # anyone write rows to the table by guessing.
            session = _evaluator.session_repo.get_session(target_session_id)
            if session is None:
                return build_response(
                    404,
                    rfc7807_error(
                        404,
                        "No Such Session",
                        f"No session named {target_session_id!r} has been recorded.",
                        path,
                        error_type="urn:threefold:error:session-not-found",
                    ),
                )
            return build_response(200, {
                "session_id": session.session_id,
                # The listing's rule applies to the detail it links to, or an old
                # row's raw project name would be one click away.
                "project_name": project_label(session.project_name),
                "cumulative_cost_usd": session.total_cost_usd,
                "budget_usd": session.budget_usd,
                "budget_remaining_usd": round(max(0.0, session.budget_usd - session.total_cost_usd), 4),
                "is_tripped": session.is_tripped,
                "tool_call_history_count": len(session.history),
                "created_at": session.created_at,
            })

        # Route 6b: Enterprise Emergency Kill Switch (Manual Session Freeze)
        if path.startswith("/sessions/") and path.endswith("/terminate") and http_method == "POST":
            target_session_id = _checked_session_id(
                unquote(path.replace("/sessions/", "").replace("/terminate", "").strip())
            )
            body = _parse_body(event)
            # Bounded and redacted before anything is frozen. models.py builds
            # `trip_reason` out of these two, the sessions listing returns it as
            # it is, and public_row() rewrites only the project and the
            # developer, so on a stack whose reads are public this is text a
            # caller publishes on a page every visitor can open. Taken from the
            # body as they came, they were unbounded, untyped and unredacted,
            # while the reason on a ledger row has been redacted and cut to 240
            # characters since the ledger existed.
            operator_name = _bounded_text(body, "operator_name", 120, DEFAULT_TERMINATION_OPERATOR)
            reason = _bounded_text(body, "reason", 240, DEFAULT_TERMINATION_REASON)
            frozen_session = _evaluator.terminate_session(target_session_id, operator_name, reason)

            res_dict = {
                "status": "SESSION_FROZEN",
                "session_id": target_session_id,
                "operator": operator_name,
                "reason": reason,
                "is_tripped": frozen_session.is_tripped,
                "total_cost_usd": frozen_session.total_cost_usd,
            }
            if idempotency_key:
                global_idempotency_cache.set(idempotency_key, 200, res_dict)

            emit_threefold_emf_metrics(
                {"SessionManuallyTerminated": 1.0},
                namespace="Threefold/Emergency",
            )
            return build_response(200, res_dict)

        # Route 6b2: resume a halted session. The operator key is checked by the
        # middleware before this runs. Before this route existed a halt was
        # permanent, so a developer whose own hook session tripped had no way
        # back to it but a new session id.
        if path.startswith("/sessions/") and path.endswith("/resume") and http_method == "POST":
            target_session_id = unquote(path[len("/sessions/"):-len("/resume")].strip())
            body = _parse_body(event)
            # Both are required, because the record of who cleared a halt and
            # why is the reason this route is closed rather than open.
            operator_name = _required_text(body, "operator_name", 120)
            reason = _required_text(body, "reason", 240)
            try:
                resumed = _evaluator.resume_session(target_session_id, operator_name, reason)
            except SessionNotHaltedError as running:
                return build_response(
                    409,
                    rfc7807_error(
                        409,
                        "Session Is Not Halted",
                        str(running),
                        path,
                        error_type="urn:threefold:error:session-not-halted",
                    ),
                )
            if resumed is None:
                return build_response(
                    404,
                    rfc7807_error(
                        404,
                        "No Such Session",
                        f"No session named {target_session_id!r} has been recorded.",
                        path,
                        error_type="urn:threefold:error:session-not-found",
                    ),
                )
            res_dict = {
                "status": "SESSION_RESUMED",
                "session_id": target_session_id,
                "operator": operator_name,
                "reason": reason,
                "resumed_at": resumed.resumed_at,
                "previous_trip_reason": resumed.resumed_from,
                "is_tripped": resumed.is_tripped,
                "total_cost_usd": resumed.total_cost_usd,
                "budget_usd": resumed.budget_usd,
                # Split by who sends the calls, because the loop gate treats them
                # apart: a hook session's repeat is refused and never halts it, so
                # telling an operator it "halts again" described a lockout that
                # cannot happen.
                "note": (
                    "The halt is cleared and the history and spend are kept, so the gates still "
                    "apply: a session past its budget halts again on its next call that costs "
                    "anything, and a page or scenario session that repeats the call that halted "
                    "it halts again. A hook session's repeat is refused but does not halt it."
                ),
            }
            if idempotency_key:
                global_idempotency_cache.set(idempotency_key, 200, res_dict)
            emit_threefold_emf_metrics(
                {"SessionResumed": 1.0},
                namespace="Threefold/Emergency",
            )
            return build_response(200, res_dict)

        # Route 6c: the layering rules. Reading them is open, because a reader
        # has to be able to see the architecture they are being held to; writing
        # them is not, because they are the gate rather than a setting on it.
        if path in ("/rules", "/rules/layering") and http_method == "GET":
            project = (event.get("queryStringParameters") or {}).get("project") or None
            if project is not None and not isinstance(project, str):
                raise InvalidRequestError("project must be given once.", "project")
            rules, source = _evaluator.rules_in_force(project)
            warnings = _project_warnings(project)
            return build_response(
                200,
                {
                    "rules": rules,
                    "count": len(rules),
                    # True when nothing was saved for what was asked about:
                    # without a project, that the shipped set is in force, as
                    # the rules page has always read it; with one, that the
                    # project has no rules of its own. `source` says which set
                    # came back instead, so a reader never has to infer it.
                    "is_default": (source != "project") if project else rules == DEFAULT_RULES,
                    "source": source,
                    "project": project,
                    "warnings": warnings,
                    "languages_read": sorted(set(LANGUAGE_BY_SUFFIX.values())),
                    "extensions_read": sorted(LANGUAGE_BY_SUFFIX),
                    "refresh_seconds": RULES_REFRESH_SECONDS,
                    "unsupported": UNSUPPORTED,
                },
            )

        # Route 6d: try the rules on a file without saving or recording anything.
        # An architect writing a rule needs to know whether it fires on the file in
        # front of them before ten teams find out, and a draft rule set can be sent
        # in the body so nothing has to be saved to be tried. Open, because it
        # changes nothing: no verdict is issued and no ledger row is written.
        if path == "/rules/explain" and http_method == "POST":
            # Parsed leniently: this route answers a non-object body with its own
            # problem, which tells the caller what to send.
            body = _parse_body(event, expect_object=False)

            def _cannot_explain(detail):
                return build_response(
                    400,
                    rfc7807_error(
                        400, "Nothing To Explain", detail, path,
                        error_type="urn:threefold:error:nothing-to-explain",
                    ),
                )

            if not isinstance(body, dict):
                return _cannot_explain("Send a JSON object with the path of the file and its content.")
            target = body.get("path")
            content = body.get("content") or ""
            if not isinstance(target, str) or not target.strip():
                return _cannot_explain("Send the path of the file and its content.")
            if not isinstance(content, str):
                return _cannot_explain("content must be a string.")
            # Present means a project was named, as it does on the save: null,
            # a number or a list used to be read as "no project" and judged by
            # the shared set without a word. "" is text, so it is a name
            # outside the pattern and gets the same warning GET /rules gives.
            project = body.get("project")
            if "project" in body and not isinstance(project, str):
                return _cannot_explain(
                    "project must be a string. Leave it out to judge by the shared rules."
                )
            # The same test the gate applies before it judges a path at all. A
            # path the gate would never evaluate used to be judged here, so the
            # page said REFUSE for a write the gate then approved.
            if not looks_like_path(target):
                return _cannot_explain(
                    "The gate does not treat this as a file path, so it would never judge it: a path "
                    f"is at most {MAX_PATHLIKE_LENGTH} characters, on one line, with no spaces."
                )
            draft = body.get("rules")
            if draft is not None:
                rules, problems = validate_rules(draft)
                if problems or not rules:
                    # A draft whose rules were silently dropped explained as
                    # "no rule covers this path", which was true only of the
                    # rules that survived.
                    problem = rfc7807_error(
                        400,
                        "No Usable Rule",
                        f"{len(problems) or 'No'} rule(s) in the draft cannot be used, so it was not tried.",
                        path,
                        error_type="urn:threefold:error:unusable-rule",
                    )
                    problem["problems"] = problems
                    return build_response(400, problem)
                source = "draft"
            else:
                # The set the gate would judge this project's write by: its own
                # when it has one, the shared set when it does not.
                rules, source = _evaluator.rules_in_force(project)
            language, modules = declared_imports(target, content)
            found, note = layering_violations(target, content, rules)
            enforced = [item for item in found if item["mode"] == "enforce"]
            watched = [item for item in found if item["mode"] == "observe"]
            return build_response(
                200,
                {
                    "path": target,
                    "language": language or None,
                    "imports": modules,
                    "rules_considered": "draft" if draft is not None else "in force",
                    "rules_source": source,
                    "project": project,
                    "warnings": _project_warnings(project),
                    "applicable_rules": [rule["id"] for rule in rules_for_path(target, rules)],
                    "verdict": "REFUSE" if enforced else ("OBSERVE" if watched else "ALLOW"),
                    "violations": found,
                    "note": note,
                    # Only the layering rules are tried here. A write this answers
                    # ALLOW for can still be refused for a credential, a protected
                    # path, a loop or its cost.
                    "scope": "layering rules only",
                },
            )

        if (drafted := draft_routes.handle(path, http_method, event)) is not None:
            return drafted  # Route 6d2: POST /rules/draft, drafted by Bedrock, never saved.

        if path in ("/rules", "/rules/layering") and http_method == "POST":
            # The one POST that takes a bare array as well as an object: a rule
            # set can be sent as the list itself.
            body = _parse_body(event, expect_object=False)
            submitted = body.get("rules", body) if isinstance(body, dict) else body
            # Without a project the shared set is replaced, as it always was.
            # With one, only that project's set is, and a name the stack would
            # store as "unlabelled" is refused by the evaluator as a 400.
            # "Without" means the key is absent. A project picker left unset
            # sends null, and reading that as "no project" replaced every
            # team's rules for a caller who meant to change one project's.
            project = None
            if isinstance(body, dict) and "project" in body:
                project = body["project"]
                if not isinstance(project, str):
                    raise InvalidRequestError(
                        "project must be a project name matching this deployment's "
                        "AllowedProjectPattern. Leave it out to replace the shared rules.",
                        "project",
                    )
            try:
                saved = _evaluator.update_rules(submitted, project=project)
            except UnusableRulesError as invalid:
                problem = rfc7807_error(
                    400,
                    "No Usable Rule",
                    str(invalid),
                    path,
                    error_type="urn:threefold:error:unusable-rule",
                )
                problem["problems"] = invalid.problems
                return build_response(400, problem)
            res_dict = {
                "status": "RULES_UPDATED",
                "count": len(saved),
                "rules": saved,
                "project": project,
                "scope": "project" if project is not None else "shared",
                "refresh_seconds": RULES_REFRESH_SECONDS,
            }
            if idempotency_key:
                global_idempotency_cache.set(idempotency_key, 200, res_dict)
            emit_threefold_emf_metrics({"LayeringRulesUpdated": 1.0}, namespace="Threefold/Audits")
            return build_response(200, res_dict)

        # Route 7: Dynamic Enterprise Policy Configuration
        if path in ("/policy/config", "/policy") and http_method == "GET":
            return build_response(200, _evaluator.policy_config.to_dict())

        if path in ("/policy/config", "/policy") and http_method == "POST":
            body = _parse_body(event)
            try:
                config = PolicyConfigDTO(
                    max_single_call_usd=float(body.get("max_single_call_usd", 1.00)),
                    max_session_budget_usd=float(body.get("max_session_budget_usd", 10.00)),
                    loop_history_window=int(body.get("loop_history_window", 6)),
                    monomorphic_repetition_threshold=int(body.get("monomorphic_repetition_threshold", 3)),
                )
            except (TypeError, ValueError):
                raise InvalidRequestError("Every policy value must be a number.") from None
            _evaluator.update_policy(config)
            res_dict = {"status": "POLICY_UPDATED", "config": config.to_dict()}
            if idempotency_key:
                global_idempotency_cache.set(idempotency_key, 200, res_dict)
            return build_response(200, res_dict)

        # Route 8: sign-in, the dashboard and what connecting a repository downloads.
        served = access_routes.handle(path, http_method, event)
        if served is not None:
            return served
        # The application's routes (overview, decisions, projects and their stages,
        # reviews, sandbox), imported here so that module can reach this one's
        # evaluator without either needing the other loaded first.
        from threefold.interfaces import app_routes
        app_response = app_routes.handle(path, http_method, event)
        if app_response is not None:
            return app_response

        return build_response(
            404,
            rfc7807_error(
                404,
                "Not Found",
                f"Endpoint '{path}' not found",
                path,
                error_type="urn:threefold:error:not-found",
            ),
        )

    except InvalidRequestError as invalid:
        return build_response(
            400,
            rfc7807_error(
                400,
                "Bad Request",
                invalid.detail,
                path,
                error_type="urn:threefold:error:bad-request",
                invalid_params=[{"name": invalid.name, "reason": invalid.detail}] if invalid.name else None,
            ),
        )
    except ValueError as val_err:
        return build_response(
            400,
            rfc7807_error(
                400,
                "Bad Request",
                str(val_err),
                path,
                error_type="urn:threefold:error:bad-request",
            ),
        )
    except Exception:
        logger.exception("Internal error processing request %s on %s", request_id, path)
        return build_response(500, _internal_error(path, request_id))


def _project_warnings(project: Any) -> list:
    """What a reader of the rules should know about the project they named.

    Shared by GET /rules and POST /rules/explain so the two cannot disagree
    about any name that reaches it: explain used to judge a name outside the
    pattern by the shared set in silence, while GET warned about the same
    name. An empty name reaches it only from explain, because GET reads an
    empty query value as no project at all.
    """
    if project is None or is_labelled(project):
        return []
    return [
        "project does not match this deployment's AllowedProjectPattern, so no rules "
        "can be saved for it and its calls are judged by the shared rules."
    ]


UNREADABLE_TOOL_CALL = (
    "This adapter reads an Anthropic tool_use object, an OpenAI function_call, an OpenAI "
    "tool_calls array of one, or {tool_name, arguments}. Send the tool call in one of "
    "those shapes: a body naming no tool would be judged as a call with no arguments, "
    "which every gate approves."
)
ARGUMENTS_MUST_BE_AN_OBJECT = (
    "arguments must be a JSON object, or the JSON text of one, as OpenAI sends them. The "
    "gates read the strings inside it, so anything else would be approved without having "
    "been read."
)


def _tool_arguments(value: Any) -> Dict[str, Any]:
    """One tool call's arguments as an object the gates can walk.

    Anthropic sends them as an object and OpenAI as the JSON text of one, so
    both are read. Absent is an empty object, which is what a tool that takes
    no arguments sends. Anything else is refused rather than evaluated: the
    gates scan the strings inside an object, so a number or a bare string
    would be judged as a call carrying nothing.
    """
    if value is None:
        return {}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except ValueError:
            raise InvalidRequestError(ARGUMENTS_MUST_BE_AN_OBJECT, "arguments") from None
    if not isinstance(value, dict):
        raise InvalidRequestError(ARGUMENTS_MUST_BE_AN_OBJECT, "arguments")
    return value


def _tool_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError("The tool call must name the tool it calls.", "name")
    return value.strip()


def _openai_function(call: Any) -> tuple:
    """One OpenAI call: a tool call object, or the function object inside it."""
    if isinstance(call, dict) and isinstance(call.get("function"), dict):
        call = call["function"]
    if not isinstance(call, dict):
        raise InvalidRequestError(UNREADABLE_TOOL_CALL, "tool_call")
    return _tool_name(call.get("name")), _tool_arguments(call.get("arguments"))


def _tool_call_shapes(tc: Dict[str, Any]) -> list:
    """Every shape this adapter reads that the payload names, in dispatch order.

    A payload naming exactly one is read as that one. Naming none is refused,
    and so is naming two: see `_ambiguous_tool_call`.
    """
    shapes = []
    if tc.get("type") == "tool_use" or ("name" in tc and "input" in tc):
        shapes.append("tool_use")
    if "tool_calls" in tc:
        shapes.append("tool_calls")
    if "function_call" in tc:
        shapes.append("function_call")
    if "function" in tc:
        shapes.append("function")
    if "tool_name" in tc or ("name" in tc and "arguments" in tc):
        shapes.append("tool_name/arguments")
    return shapes


def _ambiguous_tool_call(shapes: list) -> str:
    return (
        "This body names the tool call in more than one shape (" + ", ".join(shapes) + "), and "
        "this route answers with one verdict, so it cannot tell which arguments to judge. Send "
        "the call in one shape: judging either one would leave the arguments in the other "
        "approved without having been read."
    )


def _universal_tool_call(tc: Any) -> tuple:
    """The tool and the arguments inside one native payload, or a 400.

    The shapes an agent actually emits: Anthropic's `tool_use` object, OpenAI's
    `tool_calls` array, OpenAI's message-level `function_call`, a single OpenAI
    tool call object, and this service's own `{tool_name, arguments}`. Anything
    else is refused. It used to be approved instead, as a call named
    "unknown_tool" with no arguments, which is a call every gate lets through.

    A payload naming two of them is refused rather than dispatched to whichever
    is tested first. Told apart in order, `name` and `input` beside a native
    envelope shadowed it: the gate judged the empty `input` and answered
    APPROVED while the `tool_calls`, `function_call` or `function` beside it —
    a command exporting an access key id — was never read at all. Picking the
    other order only moves which shape can hide behind which, so the payload
    that names two is the caller's to send as one.
    """
    if not isinstance(tc, dict):
        raise InvalidRequestError(UNREADABLE_TOOL_CALL, "tool_call")
    shapes = _tool_call_shapes(tc)
    if len(shapes) > 1:
        raise InvalidRequestError(_ambiguous_tool_call(shapes), "tool_call")
    if not shapes:
        raise InvalidRequestError(UNREADABLE_TOOL_CALL, "tool_call")
    shape = shapes[0]
    if shape == "tool_use":
        return _tool_name(tc.get("name")), _tool_arguments(tc.get("input"))
    if shape == "tool_calls":
        calls = tc["tool_calls"]
        # One request, one verdict. A batch would need a verdict each, and
        # answering with one would leave the rest judged by nothing.
        if not isinstance(calls, list) or len(calls) != 1:
            raise InvalidRequestError(
                "tool_calls must hold exactly one tool call: this route answers with one "
                "verdict, so send each call in its own request.",
                "tool_calls",
            )
        return _openai_function(calls[0])
    if shape == "function_call":
        return _openai_function(tc["function_call"])
    if shape == "function":
        return _openai_function(tc)
    return (
        _tool_name(tc.get("tool_name") if tc.get("tool_name") is not None else tc.get("name")),
        _tool_arguments(tc.get("arguments")),
    )


SESSION_ID_LIMIT = 200


def _checked_session_id(value: Any) -> str:
    """The session id a caller names, which naming creates and a page then shows.

    Every route that names a session creates it: the two recording routes, the
    certificate and the freeze. `/api/sessions` lists the id exactly as it
    arrived, and `public_row()` rewrites only the project and the developer, so
    on a stack whose reads are public the id is the piece of that row a caller
    writes and every visitor reads. Unbounded and unredacted it was the third
    piece of caller text on that page, beside the operator and the reason a
    freeze writes, which are bounded and redacted.

    Refused rather than cut or redacted, unlike those two: an id shortened or
    rewritten on the way in would address a different session than the caller
    named, so the freeze would lock one row and answer for another. 200
    characters is far above what names a session in practice — an agent's is a
    UUID — and the limit is on the id, never on how many sessions there may be.
    """
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError("session_id must be text.", "session_id")
    named = value.strip()
    if len(named) > SESSION_ID_LIMIT:
        raise InvalidRequestError(
            f"session_id must be at most {SESSION_ID_LIMIT} characters: naming a session "
            "creates it, and the sessions page shows the id it was named by.",
            "session_id",
        )
    if redact_secrets(named) != named:
        raise InvalidRequestError(
            "session_id must not carry a credential: naming a session creates it, and the "
            "sessions page shows the id it was named by, as it arrived.",
            "session_id",
        )
    return named


def _check_named_session(body: Dict[str, Any]) -> None:
    """The same check for a route that reads the id out of a body it may leave out.

    A body naming no session gets the default this route has always used, so a
    caller that names none is unaffected.
    """
    named = body.get("session_id")
    if named is None or named == "":
        return
    _checked_session_id(named)


def _bounded_text(body: Dict[str, Any], name: str, limit: int, default: str) -> str:
    """A field a caller may leave out, kept only as bounded text with no credential in it.

    Absent or empty is the default this route has always used, so a caller that
    sends neither field still freezes a session. Anything that is not a string
    is refused rather than stored as whatever the JSON held, because what is
    stored is shown on a page. Over the limit is refused rather than cut, for
    the reason `_required_text` gives: a record shortened on the way in says
    something its author did not. What is kept is passed through
    `redact_secrets`, as every reason the ledger keeps already is.
    """
    value = body.get(name)
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        raise InvalidRequestError(f"{name} must be text.", name)
    value = value.strip()
    if not value:
        return default
    if len(value) > limit:
        raise InvalidRequestError(f"{name} must be at most {limit} characters.", name)
    return redact_secrets(value)


def _required_text(body: Dict[str, Any], name: str, limit: int) -> str:
    """A field that must be a non-empty string of at most `limit` characters.

    Refused rather than cut to length: the value is kept as a record, and a
    record shortened on the way in says something its author did not.
    """
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError(f"{name} is required and must be text.", name)
    value = value.strip()
    if len(value) > limit:
        raise InvalidRequestError(f"{name} must be at most {limit} characters.", name)
    return value


def _parse_body(event: Dict[str, Any], expect_object: bool = True) -> Any:
    """Reads the JSON body, and by default insists that it is an object.

    Every POST that reads fields expects an object. An array, a string or null
    used to reach `body.get` and come back as a 500 carrying the exception
    text; it is the caller's mistake, so it is now a 400 that says what to send.
    """
    raw = event.get("body")
    if raw is None or raw == "":
        return {}
    if isinstance(raw, (dict, list)):
        parsed = raw
    else:
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            raise InvalidRequestError(f"Invalid JSON payload: {exc}", "body") from None
    if expect_object and not isinstance(parsed, dict):
        raise InvalidRequestError(
            f"The body must be a JSON object, not {type(parsed).__name__ if parsed is not None else 'null'}.",
            "body",
        )
    return parsed
