"""AWS Lambda proxy handler exposing REST endpoints for Threefold.

Includes Zero-Trust security middleware, AWS CloudWatch EMF metrics,
Universal Multi-Agent Adapter (OpenAI / Anthropic tool-use formats),
and RFC 7807 Problem Details error handling.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict
from urllib.parse import unquote

from threefold.application.audit_issuer import AuditIssuer
from threefold.application.bedrock_reviewer import BedrockArchitecturalReviewer
from threefold.application.dtos import PolicyConfigDTO, ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator
from threefold.domain.exceptions import EmptyAttestationException
from threefold.infrastructure.bedrock_client import BedrockGovernanceClient
from threefold.infrastructure.idempotency import global_idempotency_cache
from threefold.infrastructure.metrics_emf import emit_threefold_emf_metrics
from threefold.infrastructure.security_middleware import (
    rfc7807_error,
    validate_request_security,
)

logger = logging.getLogger("threefold.api")
logger.setLevel(logging.INFO)

# Global singleton evaluator for Lambda container reuse
_evaluator = GovernanceEvaluator()
_bedrock_client = BedrockGovernanceClient()
_reviewer = BedrockArchitecturalReviewer(_bedrock_client)

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With, X-API-Key, Idempotency-Key",
}


WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

# The Claude Code hook is the one artifact that turns this service from a demo
# into something in front of a real agent, so the deployment hands it out rather
# than telling a reader to find a repository. It lives under the packaged tree
# for that reason: anything outside CodeUri never reaches the function.
HOOKS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
SERVED_SCRIPTS = {
    "/hooks/claude_code_hook.py": "claude_code_hook.py",
    "/claude_code_hook.py": "claude_code_hook.py",
}

# Paths the deployed stack serves as pages rather than as JSON. The dashboard sits
# at the root so the public URL opens the application itself.
WEB_ASSETS = {
    "/": "index.html",
    "/index.html": "index.html",
    "/testbook.html": "testbook.html",
    "/swagger.html": "swagger.html",
    "/settings.html": "settings.html",
    "/sessions.html": "sessions.html",
    "/connect.html": "connect.html",
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


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Primary AWS Lambda event router."""
    start_time = time.time()
    http_method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method", "GET")
    raw_path = event.get("path") or event.get("rawPath", "/")
    headers = event.get("headers") or {}
    client_ip = (
        event.get("requestContext", {}).get("http", {}).get("sourceIp")
        or event.get("requestContext", {}).get("identity", {}).get("sourceIp")
        or "127.0.0.1"
    )

    # Normalization: API Gateway prefixes rawPath with the stage name when the API
    # is deployed to a named stage (for example /prod/status), so strip it before routing.
    path = raw_path.split("?")[0].rstrip("/")
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

    payload_size = len(raw_body.encode("utf-8")) if isinstance(raw_body, str) else 0

    # 1. Zero-Trust Security & Rate Limiting Guard
    is_authorized, security_problem = validate_request_security(
        headers=headers,
        client_ip=client_ip,
        path=path,
        payload_size_bytes=payload_size,
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
        if path in ("/api/sessions", "/sessions.json") and http_method == "GET":
            limit = 50
            try:
                limit = int((event.get("queryStringParameters") or {}).get("limit", 50))
            except (TypeError, ValueError):
                limit = 50
            sessions = _evaluator.list_sessions(limit=max(1, min(limit, 200)))
            return build_response(
                200,
                {
                    "sessions": sessions,
                    "count": len(sessions),
                    "persistence": getattr(_evaluator.session_repo, "persistence_mode", "memory"),
                },
            )

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

            # Strict input validation
            input_tokens = int(body.get("projected_input_tokens", 2000))
            output_tokens = int(body.get("projected_output_tokens", 500))
            budget_usd = float(body.get("budget_usd", 10.00))

            if input_tokens < 0 or output_tokens < 0:
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

            if budget_usd <= 0.0:
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

            request = ToolCallRequestDTO(
                session_id=body.get("session_id", "session-default"),
                developer_id=body.get("developer_id", "dev-user"),
                project_name=body.get("project_name", "Acme-Core"),
                tool_name=body.get("tool_name", "unknown_tool"),
                action_type=body.get("action_type", "FILE_READ"),
                arguments=body.get("arguments", {}),
                projected_input_tokens=input_tokens,
                projected_output_tokens=output_tokens,
                budget_usd=budget_usd,
            )
            result = _evaluator.evaluate_tool_call(request)
            explanation, explanation_source = _reviewer.review_action(request, result)
            result.bedrock_explanation = explanation
            result.explanation_source = explanation_source
            result.persistence = getattr(_evaluator.session_repo, "persistence_mode", "memory")

            latency_ms = (time.time() - start_time) * 1000.0
            emit_threefold_emf_metrics(
                {
                    "ToolCallsEvaluated": 1.0,
                    "VerdictApproved": 1.0 if result.status == "APPROVED" else 0.0,
                    "CircuitBreakerTripped": 1.0 if result.session_tripped else 0.0,
                    "CurrentSessionCostUSD": result.current_session_cost_usd,
                    "LatencyMs": latency_ms,
                },
                dimensions={"Project": request.project_name, "Environment": "Production"},
            )
            return build_response(200, result.to_dict())

        # Route 2b: Universal Multi-Agent Adapter (OpenAI / Anthropic format)
        if path in ("/adapter/universal-tool-call", "/universal-eval") and http_method == "POST":
            body = _parse_body(event)
            session_id = body.get("session_id", "session-universal")
            project_name = body.get("project_name", "Universal-Agent")

            # Extract payload: handle nested tool_call or root payload
            tc = body.get("tool_call", body)
            if not isinstance(tc, dict):
                tc = body

            tool_name = "unknown_tool"
            tool_args = {}
            if "type" in tc and tc["type"] == "tool_use":
                tool_name = tc.get("name", "tool")
                tool_args = tc.get("input", {})
            # Detect format: OpenAI function_call
            elif "function" in tc:
                fn = tc["function"]
                tool_name = fn.get("name", "function")
                raw_args = fn.get("arguments", {})
                tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            elif "name" in tc and "arguments" in tc:
                tool_name = tc["name"]
                raw_args = tc["arguments"]
                tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            else:
                tool_name = tc.get("tool_name", tc.get("name", "unknown_tool"))
                tool_args = tc.get("arguments", tc.get("input", {}))

            # Infer action type
            action_type = "FILE_READ"
            lower_tool = tool_name.lower()
            if any(k in lower_tool for k in ("write", "edit", "create", "modify", "save")):
                action_type = "FILE_WRITE"
            elif any(k in lower_tool for k in ("exec", "command", "bash", "shell", "run")):
                action_type = "COMMAND_EXEC"

            req = ToolCallRequestDTO(
                session_id=session_id,
                developer_id=body.get("developer_id", "universal-user"),
                project_name=project_name,
                tool_name=tool_name,
                action_type=action_type,
                arguments=tool_args,
                projected_input_tokens=int(body.get("projected_input_tokens", 2500)),
                projected_output_tokens=int(body.get("projected_output_tokens", 800)),
                budget_usd=float(body.get("budget_usd", 15.00)),
            )
            result = _evaluator.evaluate_tool_call(req)
            result.bedrock_explanation, result.explanation_source = _reviewer.review_action(req, result)
            result.persistence = getattr(_evaluator.session_repo, "persistence_mode", "memory")

            emit_threefold_emf_metrics(
                {
                    "UniversalToolEvaluated": 1.0,
                    "VerdictApproved": 1.0 if result.status == "APPROVED" else 0.0,
                },
                dimensions={"Format": "Universal", "Tool": tool_name},
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
            third_result.bedrock_explanation, third_result.explanation_source = _reviewer.review_action(req, third_result)
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
            result.bedrock_explanation, result.explanation_source = _reviewer.review_action(req, result)
            result.persistence = getattr(_evaluator.session_repo, "persistence_mode", "memory")

            emit_threefold_emf_metrics(
                {"SecretLeakBlocked": 1.0},
                namespace="Threefold/Security",
            )
            return build_response(200, result.to_dict())

        # Route 5: Issue Cryptographic Governance Certificate
        if path == "/issue-certificate" and http_method == "POST":
            body = _parse_body(event)
            session_id = body.get("session_id", "session-default")
            session = _evaluator.get_or_create_session(session_id)
            evaluations = body.get("evaluations", [])

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
                        rule_evaluations=e.get("rule_evaluations", {}),
                        current_session_cost_usd=float(e.get("current_session_cost_usd", 0.0)),
                        session_tripped=bool(e.get("session_tripped", False)),
                        proof_hash=e.get("proof_hash", "hash"),
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
            session = _evaluator.get_or_create_session(target_session_id)
            return build_response(200, {
                "session_id": session.session_id,
                "project_name": session.project_name,
                "cumulative_cost_usd": session.total_cost_usd,
                "budget_usd": session.budget_usd,
                "budget_remaining_usd": round(max(0.0, session.budget_usd - session.total_cost_usd), 4),
                "is_tripped": session.is_tripped,
                "tool_call_history_count": len(session.history),
                "created_at": session.created_at,
            })

        # Route 6b: Enterprise Emergency Kill Switch (Manual Session Freeze)
        if path.startswith("/sessions/") and path.endswith("/terminate") and http_method == "POST":
            target_session_id = unquote(path.replace("/sessions/", "").replace("/terminate", "").strip())
            body = _parse_body(event)
            operator_name = body.get("operator_name", "Enterprise Security Admin")
            reason = body.get("reason", "Manual emergency kill-switch invoked")
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

        # Route 7: Dynamic Enterprise Policy Configuration
        if path in ("/policy/config", "/policy") and http_method == "GET":
            return build_response(200, _evaluator.policy_config.to_dict())

        if path in ("/policy/config", "/policy") and http_method == "POST":
            body = _parse_body(event)
            config = PolicyConfigDTO(
                max_single_call_usd=float(body.get("max_single_call_usd", 1.00)),
                max_session_budget_usd=float(body.get("max_session_budget_usd", 10.00)),
                loop_history_window=int(body.get("loop_history_window", 6)),
                monomorphic_repetition_threshold=int(body.get("monomorphic_repetition_threshold", 3)),
            )
            _evaluator.update_policy(config)
            res_dict = {"status": "POLICY_UPDATED", "config": config.to_dict()}
            if idempotency_key:
                global_idempotency_cache.set(idempotency_key, 200, res_dict)
            return build_response(200, res_dict)

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
    except Exception as exc:
        logger.exception("Internal error processing request: %s", exc)
        return build_response(
            500,
            rfc7807_error(
                500,
                "Internal Server Error",
                str(exc),
                path,
                error_type="urn:threefold:error:internal-error",
            ),
        )


def _parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
    raw = event.get("body")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception as exc:
        raise ValueError(f"Invalid JSON payload: {exc}")
