"""The live probe reports what a stack does, and never what the operator holds.

`scripts/probe_live.py` checks a deployed stack against every claim the project
makes. These tests drive it against an in-process fake of that stack, reached
through the one function the probe sends requests with, so every PASS, FAIL and
SKIP it can reach is reached here without a network: a healthy stack passes, a
stack with one planted defect fails the check that covers it (and sometimes
others that depend on the same behaviour), and a contract endpoint that is
not deployed yet is a SKIP rather than either.

The property that matters most is negative. The operator key, every sign-in
code and every session token must never reach the terminal or the evidence
file, including when the stack itself echoes them back. That is tested against
a fake that does exactly that.

Two tests at the end drive the read-only groups through the real
`lambda_handler`, so the probe cannot drift from the code it probes. Names are
synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import itertools
import json
import re
import socket
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: the script's dataclasses resolve their
    # annotations through sys.modules, and an unregistered module has none.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


probe_live = _load("probe_live")
Response = probe_live.Response

KEY = "acme-operator-key-5f1e0c2a9b7d4e3f"
BASE = "https://acme-probe.invalid/prod/"
PATTERN = re.compile(r"^Acme-[A-Za-z0-9-]{1,40}$")
LAYERING_RULE = "python-domain-stays-pure"
GATES = ("LOOP", "PROTECTED_PATH", "UNREADABLE_WRITE", "BUDGET")

LEGACY_OPENAPI = {
    "/adapter/universal-tool-call": ["post"],
    "/api/sessions": ["get"],
    "/api/insights": ["get"],
    "/evaluate-tool-call": ["post"],
    "/issue-certificate": ["post"],
    "/policy/config": ["get", "post"],
    "/readyz": ["get"],
    "/sessions/{session_id}": ["get"],
    "/sessions/{session_id}/terminate": ["post"],
    "/sessions/{session_id}/resume": ["post"],
    "/simulate-loop": ["post"],
    "/simulate-secret": ["post"],
    "/hooks/threefold_hook.py": ["get"],
    "/status": ["get"],
    "/rules": ["get", "post"],
    "/rules/explain": ["post"],
}
APP_OPENAPI = {
    "/api/overview": ["get"],
    "/api/decisions": ["get"],
    "/api/decision": ["get"],
    "/api/projects": ["get"],
    "/api/projects/{name}": ["get", "post"],
    "/api/projects/{name}/promote": ["post"],
    "/api/projects/{name}/demote": ["post"],
    "/api/projects/{name}/reviews": ["post"],
    "/api/sandbox": ["post"],
    "/api/auth/links": ["post"],
    "/api/auth/sessions": ["post", "delete"],
    "/api/auth/whoami": ["get"],
}
PAGES = ("/", "/index.html", "/console.html", "/rules.html", "/connect.html", "/sessions.html", "/settings.html", "/swagger.html")


class FakeStack:
    """A Threefold stack in miniature, answering the probe as the contract says a stack does.

    `defects` plants one departure from the contract at a time, so a test can
    show the check that covers it fails. A defect may fail other checks that
    read the same behaviour too: a HEAD that carries a body fails every page.
    """

    def __init__(
        self,
        *,
        private: bool = False,
        key: Optional[str] = None,
        app: bool = True,
        auth: bool = True,
        dist: bool = True,
        new_pages: bool = True,
        hook_stage: str = "enforce",
        readiness: str = "ready",
        defects: Tuple[str, ...] = (),
    ) -> None:
        self.readiness = readiness
        self.private = private
        self.key = key
        self.app = app
        self.auth = auth
        self.dist = dist
        self.new_pages = new_pages
        self.hook_stage = hook_stage
        self.defects = set(defects)
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.ledger: List[Dict[str, Any]] = []
        self.projects: Dict[str, Dict[str, Any]] = {}
        self.codes: Dict[str, bool] = {}
        self.tokens: Dict[str, bool] = {}
        self.log: List[Tuple[str, str, Dict[str, str], Optional[bytes]]] = []
        self.minted: List[str] = []
        self._ids = itertools.count(1)
        self.throttle = 0
        # When set, a keyed GET /api/sessions answers 500 with the key inside a
        # detail this long before it, so the key can be placed across the point
        # where the probe shortens a quoted body.
        self.echo_pad: Optional[int] = None
        self.hook = b'#!/usr/bin/env python3\n"""An Acme hook."""\nprint("acme hook")\n'
        self.installer = f'"""Acme installer."""\nENDPOINT = {BASE!r}\nprint(ENDPOINT)\n'.encode()
        if "installer_placeholder" in self.defects:
            self.installer = b'ENDPOINT = "__THREEFOLD_ENDPOINT__"\n'
        self.bundle, self.manifest = self._bundle()

    # -- plumbing ----------------------------------------------------------

    def __call__(self, method, url, headers=None, body=None, timeout=20.0):
        headers = dict(headers or {})
        self.log.append((method, url, headers, body))
        if self.throttle > 0:
            self.throttle -= 1
            return self._json(429, {"type": "urn:threefold:error:rate-limit-exceeded", "title": "Too Many Requests", "status": 429, "detail": "slow down"})
        parsed = urlparse(url)
        assert parsed.path.startswith("/prod"), parsed.path
        path = parsed.path[len("/prod"):].rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        verb = "GET" if method == "HEAD" else method
        resp = self._route(verb, path, query, headers, body)
        if "echo_secrets" in self.defects:
            resp = self._echo(resp, headers)
        if method == "HEAD" and "head_body" not in self.defects:
            resp.body = b""
        return resp

    def _echo(self, resp: Response, headers: Dict[str, str]) -> Response:
        """Hands back every credential it was shown, the way a careless stack would."""
        seen = [headers.get("X-API-Key", ""), headers.get("Authorization", "")] + self.minted
        joined = " ".join(s for s in seen if s)
        data = resp.json()
        if isinstance(data, dict):
            data["seen"] = joined
            resp.body = json.dumps(data).encode()
        resp.headers["x-echo"] = joined
        resp.raw_headers.append(("X-Echo", joined))
        return resp

    def _json(self, status: int, payload: Any, problem: bool = False) -> Response:
        kind = "application/problem+json" if problem else "application/json"
        return Response(status, {"Content-Type": kind, "Access-Control-Allow-Origin": "*"}, json.dumps(payload).encode())

    def _problem(self, status: int, title: str, detail: str, kind: str = "urn:threefold:error:bad-request") -> Response:
        return self._json(status, {"type": kind, "title": title, "status": status, "detail": detail, "error": detail}, True)

    def _missing(self, path: str) -> Response:
        return self._problem(404, "Not Found", f"Endpoint '{path}' not found", "urn:threefold:error:not-found")

    def _operator(self, headers: Dict[str, str]) -> Optional[str]:
        presented = headers.get("X-API-Key")
        bearer = headers.get("Authorization", "")
        token = bearer[7:] if bearer.startswith("Bearer ") else None
        if self.key and (presented == self.key or token == self.key):
            return "key"
        if token and self.tokens.get(token):
            return "session"
        return None

    def _refuse(self, headers: Dict[str, str]) -> Response:
        if not self.key:
            return self._problem(403, "Policy Is Read Only Here", "no key configured", "urn:threefold:error:policy-write-disabled")
        if not headers.get("X-API-Key") and not headers.get("Authorization"):
            return self._problem(401, "Unauthorized", "a key is required", "urn:threefold:error:missing-credentials")
        return self._problem(403, "Forbidden", "that key is not accepted", "urn:threefold:error:invalid-credentials")

    @staticmethod
    def _parse(body: Optional[bytes]) -> Any:
        if not body:
            return {}
        return json.loads(body.decode())

    # -- routing -----------------------------------------------------------

    def _guarded(self, verb: str, path: str) -> bool:
        if verb == "POST" and path in ("/policy/config", "/rules"):
            return "open_policy_write" not in self.defects or path != "/policy/config"
        if verb == "POST" and path.startswith("/sessions/") and path.endswith("/resume"):
            return True
        if verb == "POST" and path.startswith("/api/projects/") and self.app:
            name = path.split("/")[3]
            open_sandbox = not self.private and re.fullmatch(r"Acme-Sandbox-[0-9a-f]{8}", name)
            if "loose_sandbox" in self.defects and not self.private and name.startswith("Acme-Sandbox"):
                open_sandbox = True
            return not open_sandbox
        return False

    def _private_read(self, verb: str, path: str) -> bool:
        if verb != "GET" or not self.private:
            return False
        reads = ("/api/insights", "/api/sessions", "/rules", "/policy/config")
        app_reads = ("/api/overview", "/api/decisions", "/api/decision", "/api/projects")
        if path in reads or (path.startswith("/sessions/") and path.count("/") == 2):
            return True
        return self.app and (path in app_reads or path.startswith("/api/projects/"))

    def _route(self, verb: str, path: str, query: Dict[str, str], headers: Dict[str, str], body: Optional[bytes]) -> Response:
        if verb == "DELETE" and "no_delete" in self.defects:
            # What a server with no DELETE handler at all answers, before any route.
            return Response(501, {"Content-Type": "text/html"}, b"<html><p>Unsupported method ('DELETE')</p></html>")
        if verb == "OPTIONS":
            return Response(
                204,
                {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-API-Key"
                    if "narrow_cors" not in self.defects else "Content-Type",
                },
            )
        if self._guarded(verb, path) and not self._operator(headers):
            return self._refuse(headers)
        if self._private_read(verb, path) and not self._operator(headers):
            return self._refuse(headers)
        try:
            return self._dispatch(verb, path, query, headers, body)
        except ValueError:
            return self._problem(400, "Bad Request", "Invalid JSON payload")

    def _dispatch(self, verb, path, query, headers, body) -> Response:
        pages = PAGES + (("/dashboard.html", "/app") if self.new_pages else ())
        if verb == "GET" and path in pages:
            if path == "/rules.html" and "missing_page" in self.defects:
                return self._missing(path)
            return Response(200, {"Content-Type": "text/html; charset=utf-8"}, b"<!doctype html><title>Acme</title><main>page</main>")
        if verb == "GET" and path == "/assets/threefold.js" and self.new_pages:
            return Response(200, {"Content-Type": "application/javascript"}, b"const BASE = '/prod';\n")
        if verb == "GET" and path == "/status":
            if "echo_secrets" in self.defects:
                return self._json(500, {"status": "BROKEN"})
            return self._json(200, {"status": "HEALTHY", "active_rules": ["A", "B", "C", "D"]})
        if verb == "GET" and path == "/readyz":
            if self.readiness == "ready":
                return self._json(200, {"status": "READY", "subsystems": [{"name": "SessionStore", "status": "HEALTHY"}]})
            details = {
                "offline": "No DynamoDB table bound; running on the in-process store",
                "broken": "GetItem failed: AccessDeniedException",
            }[self.readiness]
            return self._json(503, {"status": "DEGRADED", "subsystems": [{"name": "SessionStore", "status": "DEGRADED", "details": details}]})
        if verb == "GET" and path in ("/hooks/threefold_hook.py", "/hooks/claude_code_hook.py", "/claude_code_hook.py"):
            return Response(200, {"Content-Type": "text/plain; charset=utf-8", "X-Content-Type-Options": "nosniff"}, self.hook)
        if verb == "GET" and path == "/openapi.json":
            paths = dict(LEGACY_OPENAPI)
            if self.app and "undocumented_app" not in self.defects:
                paths.update(APP_OPENAPI)
            if "ghost_path" in self.defects:
                paths["/api/ghost"] = ["get"]
            return self._json(200, {"openapi": "3.0.3", "paths": {p: {m: {} for m in ms} for p, ms in paths.items()}})
        if verb == "POST" and path == "/evaluate-tool-call":
            return self._json(200, self.evaluate(self._parse(body)))
        if verb == "POST" and path in ("/adapter/universal-tool-call", "/issue-certificate", "/rules/explain", "/policy/config", "/rules"):
            self._parse(body)
            return self._json(200, {"status": "OK"})
        if verb == "POST" and path.startswith("/sessions/") and path.endswith(("/terminate", "/resume")):
            self._parse(body)
            return self._json(200, {"status": "OK"})
        if verb == "POST" and path in ("/simulate-loop", "/simulate-secret"):
            return self._json(200, {"status": "BLOCKED_LOOP_DETECTED"})
        if verb == "GET" and path.startswith("/sessions/"):
            session = self.sessions.get(path[len("/sessions/"):].replace("%20", " "))
            if session is None:
                return self._problem(404, "No Such Session", "none", "urn:threefold:error:session-not-found")
            return self._json(200, {"is_tripped": session["tripped"]})
        if verb == "GET" and path == "/api/sessions" and self.echo_pad is not None and headers.get("X-API-Key"):
            detail = "x" * self.echo_pad + " the request carried the header value " + headers["X-API-Key"]
            return self._json(500, {"status": 500, "detail": detail})
        if verb == "GET" and path in ("/api/insights", "/api/sessions"):
            projects =[{"project": "Acme-Core", "decisions": 3}, {"project": "unlabelled", "decisions": 1}]
            if "leak_name" in self.defects:
                projects.append({"project": "Unlisted-Repo-4471", "decisions": 2})
            return self._json(200, {"by_project": projects, "by_developer": [{"developer": "0a1b2c3d"}]})
        if verb == "GET" and path == "/rules":
            return self._json(200, {"rules": [], "project": None})
        if verb == "GET" and path == "/policy/config":
            return self._json(200, {})
        if self.app:
            answered = self._application(verb, path, query, headers, body)
            if answered is not None:
                return answered
        if self.auth:
            answered = self._auth(verb, path, headers, body)
            if answered is not None:
                return answered
        if self.dist:
            answered = self._distribution(verb, path)
            if answered is not None:
                return answered
        return self._missing(path)

    # -- the evaluator -------------------------------------------------------

    def evaluate(self, body: Dict[str, Any], seeding: bool = False) -> Dict[str, Any]:
        if not isinstance(body, dict):
            raise ValueError("body")
        sid = body["session_id"]
        project = body.get("project_name") or ""
        warnings = []
        if not PATTERN.fullmatch(project):
            warnings.append("project_name does not match, so it was recorded as 'unlabelled'.")
            project = "unlabelled"
        origin = body.get("origin")
        dry = bool(body.get("dry_run"))
        stage = None
        config = self.projects.get(project)
        if origin in ("hook", "ci") and not dry and self.app:
            stage = config["stage"] if config else self.hook_stage
        session = self.sessions.setdefault(sid, {"tripped": False, "history": []})
        text = json.dumps(body.get("arguments"))
        signature = json.dumps([body.get("tool_name"), body.get("arguments")], sort_keys=True)
        finding = None
        if session["tripped"]:
            finding = ("BLOCKED_CIRCUIT_BREAKER", "HALTED_SESSION")
        elif "AKIA" in text:
            finding = ("BLOCKED_SECRET_DETECTED", "CREDENTIAL")
        elif "/domain/" in text and "import boto3" in text:
            finding = ("BLOCKED_BOUNDARY_VIOLATION", LAYERING_RULE)
        elif ".claude/settings.json" in text or "--no-verify" in text:
            finding = ("BLOCKED_BOUNDARY_VIOLATION", "PROTECTED_PATH")
        elif session["history"][-2:] == [signature, signature] and "loop_never_trips" not in self.defects:
            finding = ("BLOCKED_LOOP_DETECTED", "LOOP")
        session["history"].append(signature)
        status, rule_key, observed, tripped = "APPROVED", "NONE", [], False
        if finding:
            rule_key = finding[1]
            observing = dry or stage == "observe" or (
                stage == "enforce" and config is not None and rule_key in config.get("observe_rules", [])
            )
            if observing and finding[0] != "BLOCKED_CIRCUIT_BREAKER":
                observed = [rule_key]
            else:
                status = finding[0]
                if rule_key == "LOOP" and origin == "page":
                    session["tripped"] = tripped = True
        explain = bool(body.get("explain"))
        if status == "APPROVED" and not ("approval_reaches_model" in self.defects and explain):
            source = "deterministic"
        else:
            source = "bedrock" if explain else "deterministic"
        number = next(self._ids)
        verdict = {
            "verdict_id": f"v{number:05d}",
            "timestamp": f"2026-09-22T10:{number // 60:02d}:{number % 60:02d}+00:00",
            "session_id": sid,
            "status": status,
            "session_tripped": tripped or session["tripped"],
            "explanation_source": source,
            "observations": [f"{rule_key} would refuse"] if observed else None,
            "observed_rules": observed or None,
            "dry_run": dry,
            "warnings": warnings,
            "reason": "synthetic",
        }
        if self.app:
            verdict["project_stage"] = stage
        self.ledger.append(
            {
                "verdict_id": verdict["verdict_id"],
                "timestamp": verdict["timestamp"],
                "session_id": sid,
                "project_name": project,
                "developer_id": "0a1b2c3d",
                "status": status,
                "rule_key": rule_key,
                "observed_rules": observed,
                "stage": stage or "enforce",
                "hook_mode": body.get("hook_mode", "unknown"),
                "review": None,
                "reviewed_at": None,
                "review_note": None,
            }
        )
        return verdict

    # -- the application contract -------------------------------------------

    def _rows(self, query: Dict[str, str]) -> List[Dict[str, Any]]:
        rows = list(reversed(self.ledger))
        if query.get("project"):
            rows = [r for r in rows if r["project_name"] == query["project"]]
        if query.get("session"):
            rows = [r for r in rows if r["session_id"] == query["session"]]
        if query.get("rule"):
            rows = [r for r in rows if r["rule_key"] == query["rule"]]
        kind = query.get("kind", "all")
        if kind == "refused":
            rows = [r for r in rows if r["status"].startswith("BLOCKED") or "loose_filter" in self.defects]
        elif kind == "observed":
            rows = [r for r in rows if r["observed_rules"]]
        review = query.get("review", "any")
        if review == "unreviewed":
            rows = [r for r in rows if r["review"] is None]
        elif review in ("correct", "false_alarm"):
            rows = [r for r in rows if r["review"] == review]
        return rows

    def _rules_of(self, name: str) -> List[Dict[str, Any]]:
        rules = []
        for key in (LAYERING_RULE,) + GATES:
            flagged = [
                r for r in self.ledger
                if r["project_name"] == name and r["rule_key"] == key and (r["status"].startswith("BLOCKED") or r["observed_rules"])
            ]
            rule = {
                "rule_key": key,
                "kind": "layering" if key == LAYERING_RULE else "gate",
                "mode_now": "observe",
                "would_refuse": len(flagged),
                "correct": sum(1 for r in flagged if r["review"] == "correct"),
                "false_alarms": sum(1 for r in flagged if r["review"] == "false_alarm"),
                "unreviewed": sum(1 for r in flagged if r["review"] is None),
                "last_seen": "",
                "recommendation": "",
            }
            rule["state"] = probe_live.expected_rule_state(rule) if "bad_state" not in self.defects else "ready"
            rules.append(rule)
        return rules

    def _application(self, verb, path, query, headers, body) -> Optional[Response]:
        if verb == "GET" and path == "/api/overview":
            approved = sum(1 for r in self.ledger if r["status"] == "APPROVED" and not r["observed_rules"])
            observed = sum(1 for r in self.ledger if r["observed_rules"])
            refused = sum(1 for r in self.ledger if r["status"].startswith("BLOCKED"))
            totals = {
                "calls": approved + observed + refused, "approved": approved + ("overview_mismatch" in self.defects),
                "refused": refused, "would_refuse": observed, "needs_review": observed, "false_alarms": 0,
                "projects": len({r["project_name"] for r in self.ledger}), "agents": 2,
            }
            return self._json(200, {
                "window_days": int(query.get("days", 7)), "generated_at": "2026-09-22T10:00:00+00:00", "source": "rollups",
                "totals": totals, "series": [{"day": "2026-09-22", "approved": approved, "observed": observed, "refused": refused}],
                "by_agent": [], "by_origin": [], "by_rule": [], "by_project": [], "stages": {"observe": 0, "enforce": 0},
            })
        if verb == "GET" and path == "/api/decisions":
            rows = self._rows(query)
            limit = min(int(query.get("limit", 50)), 200)
            offset = int(base64.b64decode(query["cursor"]).decode()) if query.get("cursor") else 0
            if "duplicate_page" in self.defects and offset:
                offset -= 1
            items = rows[offset: offset + limit]
            more = offset + limit < len(rows)
            cursor = base64.b64encode(str(offset + limit).encode()).decode() if more else None
            return self._json(200, {"items": items, "next_cursor": cursor})
        if verb == "GET" and path == "/api/decision":
            if not query.get("verdict_id"):
                return self._problem(400, "Bad Request", "verdict_id is required")
            row = next((r for r in self.ledger if r["verdict_id"] == query["verdict_id"]), None)
            if row is None:
                return self._problem(404, "No Such Decision", "none", "urn:threefold:error:decision-not-found")
            return self._json(200, {"decision": row, "session": None, "rule": None})
        if verb == "GET" and path == "/api/projects":
            names = sorted({r["project_name"] for r in self.ledger} | set(self.projects))
            projects = []
            for name in names:
                config = self.projects.get(name, {})
                projects.append({
                    "project": name, "stage": config.get("stage", self.hook_stage), "configured": bool(config),
                    "observe_rules": config.get("observe_rules", []), "created_at": None, "promoted_at": None,
                    "last_seen": "", "calls": 1, "refused": 0, "would_refuse": 0, "needs_review": 0,
                    "agents": ["claude-code"], "hook_modes": ["managed"],
                })
            if "thin_project_row" in self.defects:
                projects.append({"project": "Unlisted-Repo-4471", "stage": "observe"})
            return self._json(200, {"projects": projects})
        match = re.fullmatch(r"/api/projects/([^/]+)(/promote|/demote|/reviews)?", path)
        if match:
            name, action = match.group(1), match.group(2)
            if verb == "GET" and not action:
                config = self.projects.get(name, {"stage": self.hook_stage, "observe_rules": [], "history": []})
                rules = self._rules_of(name)
                summary = {key: 0 for key in probe_live.SUMMARY_KEYS}
                summary["stage"] = config["stage"]
                for state, key in (("ready", "rules_ready"), ("quiet", "rules_quiet"), ("noisy", "rules_noisy"), ("needs_review", "rules_needing_review")):
                    summary[key] = sum(1 for r in rules if r["state"] == state)
                return self._json(200, {"project": name, "config": config, "readiness": {"summary": summary, "rules": rules}})
            if verb != "POST":
                return None
            payload = self._parse(body)
            if not isinstance(payload, dict):
                raise ValueError("body")
            config = self.projects.setdefault(name, {"stage": "observe", "observe_rules": [], "history": [], "sandbox": False})
            all_keys = [r["rule_key"] for r in self._rules_of(name)]
            if action == "/promote":
                enforce = payload.get("enforce", [])
                config["stage"] = "enforce"
                config["observe_rules"] = [k for k in all_keys if k not in enforce]
                config["history"].append({"action": "promote", "enforce": enforce})
                return self._json(200, {"project": name, "config": config})
            if action == "/demote":
                config["stage"] = "observe"
                config["history"].append({"action": "demote"})
                return self._json(200, {"project": name, "config": config})
            if action == "/reviews":
                updated, skipped = 0, []
                for item in payload.get("items", []):
                    row = next((r for r in self.ledger if r["verdict_id"] == item.get("verdict_id")), None)
                    if row is None or row["project_name"] != name:
                        skipped.append({"verdict_id": item.get("verdict_id"), "reason": "another project"})
                        continue
                    row["review"] = None if item["label"] == "clear" else item["label"]
                    row["reviewed_by"] = "9f8e7d6c"
                    updated += 1
                return self._json(200, {"updated": updated, "skipped": skipped})
            config.update({k: v for k, v in payload.items() if k in ("stage", "observe_rules")})
            return self._json(200, {"project": name, "config": config})
        if verb == "POST" and path == "/api/sandbox":
            if self.private:
                return self._refuse({})
            name = f"Acme-Sandbox-{next(self._ids):08x}"
            self.projects[name] = {"stage": "observe", "observe_rules": [], "history": [], "sandbox": True}
            seeds = [
                ("Write", {"file_path": "src/domain/acme_seed.py", "content": "import boto3\n"}),
                ("Write", {"file_path": "src/domain/acme_seed_two.py", "content": "import boto3\n"}),
                ("Write", {"file_path": ".claude/settings.json", "content": "{}"}),
                ("Read", {"file_path": "README.md"}),
            ]
            for index, (tool, arguments) in enumerate(seeds):
                self.evaluate({"session_id": f"seed-{name}-{index}", "project_name": name, "tool_name": tool,
                               "arguments": arguments, "origin": "hook", "agent": "codex"}, seeding=True)
            return self._json(200, {"project": name, "calls_seeded": len(seeds), "url": f"dashboard.html#/projects/{name}"})
        return None

    # -- sign-in -------------------------------------------------------------

    def _auth(self, verb, path, headers, body) -> Optional[Response]:
        who = self._operator(headers)
        if verb == "GET" and path == "/api/auth/whoami":
            return self._json(200, {"authenticated": who is not None, "via": who, "expires_at": None,
                                    "reads_public": not self.private, "sandbox_writes": not self.private})
        if verb == "POST" and path == "/api/auth/links":
            if who != "key":
                return self._refuse(headers if who is None else {"X-API-Key": "wrong"})
            payload = self._parse(body)
            code = f"acmecode{next(self._ids):012d}"
            self.codes[code] = True
            self.minted.append(code)
            nxt = payload.get("next", "/overview")
            if "encoded_next" in self.defects:
                nxt = quote(nxt, safe="")
            tail = "" if "link_without_next" in self.defects else f"&next={nxt}"
            return self._json(200, {"code": code, "expires_in": 120, "url": f"{BASE}dashboard.html#/signin?code={code}{tail}"})
        if verb == "POST" and path == "/api/auth/sessions":
            payload = self._parse(body)
            code = payload.get("code")
            if not self.codes.get(code):
                return self._problem(401, "Unauthorized", "that code is used, expired or unknown", "urn:threefold:error:missing-credentials")
            self.codes[code] = "reusable_code" in self.defects
            token = f"acmetoken{next(self._ids):016d}"
            self.tokens[token] = True
            self.minted.append(token)
            return self._json(200, {"token": token, "expires_at": "2026-09-22T22:00:00+00:00", "ttl_seconds": 43200})
        if verb == "DELETE" and path == "/api/auth/sessions":
            bearer = headers.get("Authorization", "")[7:]
            if bearer not in self.tokens:
                return self._refuse(headers)
            if "sticky_token" not in self.defects:
                self.tokens[bearer] = False
            return Response(204, {"Access-Control-Allow-Origin": "*"}, b"")
        return None

    # -- distribution --------------------------------------------------------

    def _bundle(self) -> Tuple[bytes, Dict[str, Any]]:
        files = {
            "bin/threefold_hook.py": self.hook,
            "bin/threefold_cli.py": b"print('acme cli')\n",
            "lib/threefold/__init__.py": b"",
            "lib/threefold/domain/models.py": b"class AcmeModel:\n    pass\n",
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for path, content in files.items():
                archive.writestr(path, content)
        bundle = buffer.getvalue()
        listed = [{"path": p, "sha256": hashlib.sha256(c).hexdigest(), "bytes": len(c)} for p, c in files.items()]
        if "bundle_mismatch" in self.defects:
            listed[1]["sha256"] = "0" * 64
        manifest = {
            "files": listed,
            "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
            "installer_sha256": hashlib.sha256(self.installer).hexdigest(),
        }
        return bundle, manifest

    def _distribution(self, verb, path) -> Optional[Response]:
        if verb == "GET" and path == "/install.py":
            digest = hashlib.sha256(self.installer).hexdigest()
            return Response(200, {"Content-Type": "text/plain; charset=utf-8", "X-Content-SHA256": digest}, self.installer)
        if verb == "GET" and path == "/dist/manifest.json":
            return self._json(200, self.manifest)
        if verb == "GET" and path == "/dist/threefold-bundle.zip":
            return Response(200, {"Content-Type": "application/zip"}, self.bundle)
        return None


def _run(monkeypatch, stack: FakeStack, groups=None, base: str = BASE, **options) -> Tuple[Any, List[str]]:
    monkeypatch.setattr(probe_live, "http_request", stack)
    # A fixed run id, so no test depends on which one the draw produced. The
    # ids a draw can produce that once broke a check are tested by name below.
    options.setdefault("runid", "5eed0a1b")
    lines: List[str] = []
    probe = probe_live.Probe(base, pace=0, sleep=lambda seconds: None, emit=lines.append, **options)
    probe.run(groups or list(probe_live.GROUPS))
    return probe, lines


def _by_name(probe) -> Dict[str, Any]:
    return {f"{c.group}/{c.name}": c for c in probe.results}


def _failures(probe) -> List[str]:
    return [f"{c.group}/{c.name}: {c.evidence}" for c in probe.results if c.status == probe_live.FAIL]


# ---------------------------------------------------------------------------
# A healthy stack passes; an absent endpoint is a SKIP.
# ---------------------------------------------------------------------------


def test_a_healthy_public_stack_passes_every_group(monkeypatch):
    probe, _ = _run(monkeypatch, FakeStack(), expect="public")
    assert _failures(probe) == []
    groups_that_passed = {c.group for c in probe.results if c.status == probe_live.PASS}
    # Sign-in needs a key, so it is the one group a public run without one skips.
    assert groups_that_passed == set(probe_live.GROUPS) - {"signin"}
    checks = _by_name(probe)
    writes = next(line for line in probe.markdown().splitlines() if line.startswith("- **Writes:**"))
    # Every kind of write the run made is named, so the owner can find each row it left.
    assert "`probe-5eed0a1b-*` and project `Acme-Probe`" in writes
    assert "`POST /simulate-loop`" in writes and "`POST /simulate-secret`" in writes and "`Acme-Sim`" in writes
    assert "recorded as `unlabelled`" in writes
    assert re.search(r"sandbox `Acme-Sandbox-[0-9a-f]{8}` .*which expires within 24 hours", writes)
    assert checks["application/hook call refused after promotion"].status == probe_live.PASS
    assert checks["application/hook loop refused without halting under enforce"].status == probe_live.PASS
    assert checks["signin/sign-in flow"].status == probe_live.SKIP
    assert not probe.failed


@pytest.mark.parametrize("runid", ["30497919", "00000000", "abcdef01", "0a0a0a0a"])
def test_any_run_id_the_draw_can_produce_leaves_a_correct_sandbox_guard_passing(monkeypatch, runid):
    # An id of digits alone once made the upper-case near miss the real name,
    # so a correct stack failed about one public run in forty.
    probe, _ = _run(monkeypatch, FakeStack(), groups=["access"], expect="public", runid=runid)
    assert _failures(probe) == []
    assert _by_name(probe)["access/sandbox writes"].status == probe_live.PASS


def test_every_sandbox_near_miss_is_outside_the_pattern_whatever_the_run_id():
    for runid in ["30497919", "00000000", "ffffffff", "0badc0de"] + [f"{n:08d}" for n in range(0, 10**8, 9999991)]:
        probe = probe_live.Probe(BASE, runid=runid, emit=lambda line: None)
        good, misses = probe.sandbox_names()
        assert probe_live.SANDBOX_PATTERN.fullmatch(good), runid
        assert len(misses) == 4, runid
        for label, name in misses:
            assert name != good and not probe_live.SANDBOX_PATTERN.fullmatch(name), (runid, label, name)


def test_a_run_id_that_cannot_name_a_sandbox_is_refused():
    for bad in ("30497919a", "ABCDEF01", "probe", "3049791"):
        with pytest.raises(ValueError):
            probe_live.Probe(BASE, runid=bad, emit=lambda line: None)


def test_a_private_stack_is_closed_without_the_key_and_open_with_it(monkeypatch):
    probe, _ = _run(monkeypatch, FakeStack(private=True, key=KEY), key=KEY, expect="private")
    assert _failures(probe) == []
    checks = _by_name(probe)
    for read in ("api/insights", "api/sessions", "rules", "api/overview", "api/decisions", "api/projects"):
        check = checks[f"access/GET /{read} private"]
        assert check.status == probe_live.PASS
        assert "401 without a key, 200 with the key" in check.evidence
    assert checks["access/POST /api/sandbox closed"].status == probe_live.PASS
    # The sandbox is a public-stack walkthrough; a private one says so rather than failing.
    assert checks["application/sandbox created"].status == probe_live.SKIP
    signin = [c for c in probe.results if c.group == "signin"]
    assert len(signin) == 11 and all(c.status == probe_live.PASS for c in signin)


def test_contract_endpoints_not_deployed_yet_are_skipped_not_failed(monkeypatch):
    stack = FakeStack(private=True, key=KEY, app=False, auth=False, dist=False, new_pages=False)
    probe, _ = _run(monkeypatch, stack, key=KEY, expect="private")
    assert _failures(probe) == []
    checks = _by_name(probe)
    for name in (
        "availability/page /dashboard.html",
        "availability/page /app",
        "application/overview shape",
        "application/decisions pagination",
        "application/sandbox created",
        "distribution/install.py names this stack",
        "distribution/bundle matches the manifest file by file",
        "signin/whoami anonymous",
        "access/POST /api/projects/Acme-X/promote needs the key",
    ):
        assert checks[name].status == probe_live.SKIP, name
        assert "not deployed yet" in checks[name].evidence, name
    # What depends on an endpoint that is not there says what it waited for.
    assert "depends on" in checks["signin/sign out"].evidence
    assert "depends on" in checks["application/promote to enforce"].evidence
    # Everything deployed before the contract is still checked for real.
    assert checks["availability/page /rules.html"].status == probe_live.PASS
    assert checks["governance/page loop halts on the third call"].status == probe_live.PASS


def test_strict_reports_what_is_not_deployed_yet_as_a_failure(monkeypatch):
    stack = FakeStack(app=False, auth=False, dist=False, new_pages=False)
    probe, _ = _run(monkeypatch, stack, groups=["availability", "distribution"], strict=True)
    checks = _by_name(probe)
    assert checks["availability/page /dashboard.html"].status == probe_live.FAIL
    assert checks["distribution/install.py names this stack"].status == probe_live.FAIL
    assert checks["availability/page /index.html"].status == probe_live.PASS


# ---------------------------------------------------------------------------
# One planted defect fails the check that covers it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "defect, check, words",
    [
        ("missing_page", "availability/page /rules.html", "404"),
        ("head_body", "availability/page /index.html", "HEAD 200 but with"),
        ("ghost_path", "contract/GET /api/ghost", "documented, but"),
        ("undocumented_app", "contract/application reads are documented", "does not document"),
        ("open_policy_write", "access/POST /policy/config needs the key", "reached its route"),
        ("loose_sandbox", "access/sandbox writes", "writable too"),
        ("leak_name", "access/GET /api/insights public, names reduced", "must not show"),
        ("loop_never_trips", "governance/page loop halts on the third call", "third identical call"),
        ("approval_reaches_model", "governance/approval never reaches the model", "explanation_source='bedrock'"),
        ("overview_mismatch", "application/overview totals equal its series", "series sums to"),
        ("duplicate_page", "application/decisions pagination", "more than one page"),
        ("loose_filter", "application/decisions kind=refused filter", "not refusals"),
        ("bad_state", "application/project readiness", "counts make it"),
        ("installer_placeholder", "distribution/install.py names this stack", "__THREEFOLD_ENDPOINT__"),
        ("bundle_mismatch", "distribution/bundle matches the manifest file by file", "sha256 differs"),
        ("narrow_cors", "headers/CORS preflight for a keyed POST", "x-api-key"),
    ],
)
def test_a_planted_defect_fails_its_own_check(monkeypatch, defect, check, words):
    probe, _ = _run(monkeypatch, FakeStack(defects=(defect,)), expect="public")
    checks = _by_name(probe)
    assert checks[check].status == probe_live.FAIL, checks[check]
    assert words in checks[check].evidence
    assert probe.failed


@pytest.mark.parametrize(
    "defect, check",
    [
        ("reusable_code", "signin/code is single use"),
        ("sticky_token", "signin/token refused after sign out"),
        ("link_without_next", "signin/link minted with the key"),
    ],
)
def test_a_sign_in_defect_fails_its_own_check(monkeypatch, defect, check):
    probe, _ = _run(monkeypatch, FakeStack(private=True, key=KEY, defects=(defect,)), groups=["signin"], key=KEY)
    assert _by_name(probe)[check].status == probe_live.FAIL


def test_a_sign_in_link_may_percent_encode_where_it_leads(monkeypatch):
    probe, _ = _run(monkeypatch, FakeStack(private=True, key=KEY, defects=("encoded_next",)), groups=["signin"], key=KEY)
    assert _by_name(probe)["signin/link minted with the key"].status == probe_live.PASS


def test_a_read_only_run_on_a_private_stack_still_signs_in_and_writes_nothing_else(monkeypatch):
    stack = FakeStack(private=True, key=KEY)
    probe, _ = _run(monkeypatch, stack, read_only=True, key=KEY, expect="private")
    assert _failures(probe) == []
    signin = [c for c in probe.results if c.group == "signin"]
    assert len(signin) == 11 and all(c.status == probe_live.PASS for c in signin)
    assert stack.ledger == [] and stack.projects == {}
    assert "minted one session and revoked it" in probe.markdown()
    assert not any(token for token, live in stack.tokens.items() if live)


def test_a_projects_row_short_of_keys_is_reported_without_its_name(monkeypatch):
    stack = FakeStack(defects=("thin_project_row",))
    probe, lines = _run(monkeypatch, stack, groups=["application"], read_only=True, expect="public")
    check = _by_name(probe)["application/projects list shape"]
    assert check.status == probe_live.FAIL and "lacks" in check.evidence
    assert "'Un'... (18 chars)" in check.evidence
    assert "Unlisted-Repo-4471" not in probe.markdown()
    assert all("Unlisted-Repo-4471" not in line for line in lines)


def test_a_leaked_name_is_reported_without_being_repeated(monkeypatch, tmp_path):
    probe, lines = _run(monkeypatch, FakeStack(defects=("leak_name",)), groups=["access"], expect="public")
    written = probe.markdown()
    assert "Unlisted-Repo-4471" not in written
    assert all("Unlisted-Repo-4471" not in line for line in lines)
    assert "'Un'... (18 chars)" in written


LOCAL = "http://127.0.0.1:8001/prod/"


@pytest.mark.parametrize(
    "readiness, base, expected",
    [
        # The development server runs offline on purpose; its missing table is not a finding.
        ("offline", LOCAL, "SKIP"),
        # The same words from a deployed host mean the function lost its table.
        ("offline", BASE, "FAIL"),
        # A real failure fails wherever it happens.
        ("broken", LOCAL, "FAIL"),
        ("ready", BASE, "PASS"),
    ],
)
def test_readiness_excuses_only_a_local_server_running_offline(monkeypatch, readiness, base, expected):
    probe, _ = _run(monkeypatch, FakeStack(readiness=readiness), groups=["availability"], base=base)
    assert _by_name(probe)["availability/readyz"].status == expected


@pytest.mark.parametrize("base, expected", [(LOCAL, "SKIP"), (BASE, "FAIL")])
def test_a_501_is_excused_only_on_this_machine(monkeypatch, base, expected):
    # The development server defines no DELETE, so the standard library
    # answers 501 there. A deployed stack routes every method to the function.
    stack = FakeStack(private=True, key=KEY, defects=("no_delete",))
    probe, _ = _run(monkeypatch, stack, groups=["contract", "signin"], base=base, key=KEY)
    checks = _by_name(probe)
    assert checks["contract/DELETE /api/auth/sessions"].status == expected
    assert checks["signin/sign out"].status == expected
    assert checks["signin/token refused after sign out"].status == probe_live.SKIP
    if expected == "FAIL":
        assert "501" in checks["signin/sign out"].evidence and "deployed" in checks["signin/sign out"].evidence


def test_a_hook_loop_under_observe_is_a_skip_not_a_pass(monkeypatch):
    probe, _ = _run(monkeypatch, FakeStack(hook_stage="observe"), groups=["governance"])
    check = _by_name(probe)["governance/hook loop refuses without halting"]
    assert check.status == probe_live.SKIP
    assert "in observe here" in check.evidence and "never halted" in check.evidence


def test_a_stack_that_only_throttles_is_retried_rather_than_failed(monkeypatch):
    stack = FakeStack()
    stack.throttle = 3
    probe, _ = _run(monkeypatch, stack, groups=["availability"])
    assert _failures(probe) == []
    assert probe.retried_429 == 3


# ---------------------------------------------------------------------------
# Secrets never leave; a read-only run writes nothing.
# ---------------------------------------------------------------------------


def test_the_key_and_every_minted_secret_never_leave_the_probe(monkeypatch, tmp_path, capsys, caplog):
    stack = FakeStack(private=True, key=KEY, defects=("echo_secrets",))
    monkeypatch.setattr(probe_live, "http_request", stack)
    key_file = tmp_path / "operator.key"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    out = tmp_path / "PROBES.md"
    code = probe_live.main(
        ["--base", BASE, "--key-file", str(key_file), "--expect", "private", "--out", str(out), "--pace", "0"]
    )
    captured = capsys.readouterr()
    written = out.read_text(encoding="utf-8")
    # Not vacuous: the key was sent, codes and tokens were minted, and the
    # stack echoed them into bodies the evidence quotes.
    assert any(headers.get("X-API-Key") == KEY for _, _, headers, _ in stack.log)
    assert any(s.startswith("acmecode") for s in stack.minted)
    assert any(s.startswith("acmetoken") for s in stack.minted)
    assert "[redacted]" in written
    assert code == 1
    for secret in [KEY] + stack.minted:
        assert secret not in captured.out
        assert secret not in captured.err
        assert secret not in written
        assert secret not in caplog.text
    # The key goes in a header, never in a URL a log would keep.
    assert all(KEY not in url for _, url, _, _ in stack.log)


def _fragments(secret: str, shortest: int = 6) -> List[str]:
    """Every prefix of a secret long enough to matter, which is what a cut leaves behind."""
    return [secret[:n] for n in range(shortest, len(secret) + 1)]


@pytest.mark.parametrize("pad", [0, 3, 8, 13, 21, 30, 44, 60, 190])
def test_a_secret_cut_by_the_evidence_line_leaks_no_part_of_itself(monkeypatch, tmp_path, pad):
    # The fake answers a keyed read with the key inside a long error body. With
    # these paddings the key lands before, across and after the point where a
    # quoted body is shortened, and across the end of the evidence line.
    stack = FakeStack(key=KEY)
    stack.echo_pad = pad
    probe, lines = _run(monkeypatch, stack, groups=["contract"], key=KEY)
    check = _by_name(probe)["contract/GET /api/sessions"]
    assert check.status == probe_live.FAIL and "answers 500" in check.evidence
    written = probe.markdown()
    for text in lines + [written]:
        leaked = [f for f in _fragments(KEY) if f in text]
        assert leaked == [], (pad, leaked[-1:], text)


def test_a_secret_is_scrubbed_before_its_whitespace_is_collapsed():
    spaced = "acme  spaced\tkey  0123456789"
    lines: List[str] = []
    probe = probe_live.Probe(BASE, key=spaced, runid="5eed0a1b", emit=lines.append)
    probe.record("contract", "echo", probe_live.FAIL, f"the stack said: {spaced} and {' '.join(spaced.split())}")
    for text in lines + [probe.results[-1].evidence, probe.markdown()]:
        assert "spaced" not in text, text


def test_a_secret_already_cut_by_the_stack_or_by_a_check_is_still_held_back():
    lines: List[str] = []
    probe = probe_live.Probe(BASE, key=KEY, runid="5eed0a1b", emit=lines.append)
    # A check that shortened a value before handing it over, as the digest
    # checks do with a value the stack chose.
    probe.record("distribution", "digest", probe_live.FAIL, f"installer_sha256 {KEY[:12]}... but the body hashes to 0123")
    probe.record("contract", "cut", probe_live.FAIL, probe_live.one_line("q" * 70 + KEY, 80))
    for text in lines + [c.evidence for c in probe.results] + [probe.markdown()]:
        assert [f for f in _fragments(KEY) if f in text] == [], text


def test_a_sign_in_link_is_held_back_whole_without_hiding_the_base_it_starts_with(monkeypatch):
    stack = FakeStack(private=True, key=KEY)
    probe, lines = _run(monkeypatch, stack, groups=["signin"], key=KEY)
    link = next(value for value in probe._secrets if value.startswith(BASE))
    probe.record("signin", "link", probe_live.PASS, f"minted {link}")
    # The page the link opens, named up to a cut marker: the leading part of
    # the link, which is not secret, since only the code inside it is.
    probe.record("signin", "page", probe_live.PASS, f"the page {BASE}dashboard.html... answered 200")
    assert link not in probe.markdown() and all(link not in line for line in lines)
    assert f"{BASE}dashboard.html..." in probe.results[-1].evidence
    assert f"- **Base:** {BASE}" in probe.markdown()


def test_a_read_only_run_sends_nothing_that_can_change_the_stack(monkeypatch):
    stack = FakeStack()
    probe, _ = _run(monkeypatch, stack, read_only=True, expect="public")
    assert _failures(probe) == []
    writes = [(method, url, body) for method, url, _, body in stack.log if method in ("POST", "PUT", "PATCH")]
    assert writes, "the contract and access groups still prove the write routes exist"
    for method, url, body in writes:
        assert body == probe_live.MALFORMED, (method, url, body)
    # A DELETE is sent only to learn that the route is there, with nothing to revoke.
    for method, url, headers, _ in stack.log:
        if method == "DELETE":
            assert "Authorization" not in headers and "X-API-Key" not in headers, url
    assert not any("/evaluate-tool-call" in url and body != probe_live.MALFORMED for _, url, body in writes)
    assert not any(url.endswith(("/simulate-loop", "/simulate-secret", "/api/sandbox")) for _, url, _ in writes)
    assert stack.ledger == [] and stack.projects == {}


def test_the_evidence_file_says_what_was_probed_and_how(monkeypatch):
    probe, _ = _run(monkeypatch, FakeStack(), groups=["availability", "headers"], expect="public", runid="0badc0de")
    written = probe.markdown()
    head = written.splitlines()[:6]
    assert head[0].startswith("# Threefold live probes, ")
    assert f"- **Base:** {BASE}" in head
    assert any(line.startswith("- **Expected reads:** public") for line in head)
    assert "`probe-0badc0de`" in written
    assert "| POST /evaluate-tool-call | 20 |" in written
    assert "## availability" in written and "## headers" in written and "## governance" not in written


# ---------------------------------------------------------------------------
# The command line.
# ---------------------------------------------------------------------------


def test_the_exit_code_is_zero_on_a_clean_run_and_one_on_a_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(probe_live, "http_request", FakeStack())
    assert probe_live.main(["--base", BASE, "--only", "availability", "--out", "-", "--pace", "0"]) == 0
    monkeypatch.setattr(probe_live, "http_request", FakeStack(defects=("missing_page",)))
    assert probe_live.main(["--base", BASE, "--only", "availability", "--out", "-", "--pace", "0"]) == 1


def test_the_key_is_never_sent_over_plain_http_to_another_host(tmp_path, capsys):
    with pytest.raises(ValueError) as refused:
        probe_live.Probe("http://acme.invalid/prod/", key=KEY, emit=lambda line: None)
    assert KEY not in str(refused.value)
    # This machine, or no key at all, is fine.
    probe_live.Probe(LOCAL, key=KEY, emit=lambda line: None)
    probe_live.Probe("http://localhost:8001/prod/", key=KEY, emit=lambda line: None)
    probe_live.Probe("http://acme.invalid/prod/", emit=lambda line: None)
    key_file = tmp_path / "operator.key"
    key_file.write_text(KEY, encoding="utf-8")
    assert probe_live.main(["--base", "http://acme.invalid/prod/", "--key-file", str(key_file), "--out", "-"]) == 2
    captured = capsys.readouterr()
    assert "https" in captured.err and KEY not in captured.err + captured.out


def test_a_writing_run_against_another_host_says_so_before_it_starts(monkeypatch, capsys):
    monkeypatch.setattr(probe_live, "http_request", FakeStack())
    probe_live.main(["--base", BASE, "--only", "governance", "--out", "-", "--pace", "0"])
    out = capsys.readouterr().out.splitlines()
    warning = [i for i, line in enumerate(out) if line.startswith("Warning: this run writes to acme-probe.invalid")]
    first_check = next(i for i, line in enumerate(out) if line.startswith(("PASS", "FAIL", "SKIP")))
    assert warning and warning[0] < first_check
    assert "--read-only" in out[warning[0]]
    for argv in (["--read-only"], ["--only", "availability"]):
        monkeypatch.setattr(probe_live, "http_request", FakeStack())
        probe_live.main(["--base", BASE, "--out", "-", "--pace", "0", "--only", "governance"] + argv)
        assert "Warning: this run writes" not in capsys.readouterr().out, argv
    monkeypatch.setattr(probe_live, "http_request", FakeStack())
    probe_live.main(["--base", LOCAL, "--only", "governance", "--out", "-", "--pace", "0"])
    assert "Warning: this run writes" not in capsys.readouterr().out


def test_a_read_only_run_says_it_wrote_nothing(monkeypatch):
    probe, _ = _run(monkeypatch, FakeStack(), read_only=True, expect="public")
    assert "- **Writes:** none to the ledger or any project, read-only run" in probe.markdown()


def test_the_default_evidence_file_never_overwrites_and_keeps_a_private_stack_out_of_the_repository(tmp_path):
    root, temp = tmp_path / "repo", tmp_path / "temp"
    first = probe_live.default_out("0badc0de", False, root=root, temp=temp, today="2026-09-22")
    assert first == root / "docs" / "evidence" / "PROBES_2026-09-22.md"
    first.parent.mkdir(parents=True)
    first.write_text("an earlier run", encoding="utf-8")
    second = probe_live.default_out("5eed0a1b", False, root=root, temp=temp, today="2026-09-22")
    assert second == root / "docs" / "evidence" / "PROBES_2026-09-22-5eed0a1b.md"
    private = probe_live.default_out("5eed0a1b", True, root=root, temp=temp, today="2026-09-22")
    assert private == temp / "threefold-probes" / "PROBES_2026-09-22-5eed0a1b.md"
    assert root not in private.parents


def test_a_keyed_run_with_no_out_writes_its_evidence_outside_the_repository(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(probe_live, "http_request", FakeStack(private=True, key=KEY))
    monkeypatch.setattr(probe_live.tempfile, "tempdir", str(tmp_path))
    key_file = tmp_path / "operator.key"
    key_file.write_text(KEY, encoding="utf-8")
    probe_live.main(["--base", BASE, "--key-file", str(key_file), "--only", "availability", "--pace", "0"])
    written = list((tmp_path / "threefold-probes").glob("PROBES_*.md"))
    assert len(written) == 1 and KEY not in written[0].read_text(encoding="utf-8")
    assert "outside the repository" in capsys.readouterr().out


def test_a_crash_outside_any_check_fails_its_group_and_the_evidence_is_still_written(monkeypatch, tmp_path):
    import http.client

    stack = FakeStack()

    def breaks_on_the_spec(method, url, headers=None, body=None, timeout=20.0):
        if url.endswith("/openapi.json"):
            raise http.client.IncompleteRead(b"{", 499)
        return stack(method, url, headers, body, timeout)

    monkeypatch.setattr(probe_live, "http_request", breaks_on_the_spec)
    out = tmp_path / "PROBES.md"
    code = probe_live.main(["--base", BASE, "--only", "contract,access", "--out", str(out), "--pace", "0"])
    assert code == 1
    written = out.read_text(encoding="utf-8")
    assert "| group stopped | FAIL |" in written and "IncompleteRead" in written
    # The group after it still ran.
    assert "## access" in written


def test_an_unknown_group_is_refused_rather_than_running_nothing(capsys):
    assert probe_live.main(["--base", BASE, "--only", "availability,nonsense"]) == 2
    assert "unknown group 'nonsense'" in capsys.readouterr().err


def test_a_bad_key_file_is_refused_without_echoing_it(tmp_path, capsys):
    key_file = tmp_path / "two.key"
    key_file.write_text(f"{KEY}\n{KEY}-second\n", encoding="utf-8")
    assert probe_live.main(["--base", BASE, "--key-file", str(key_file)]) == 2
    err = capsys.readouterr().err
    assert "exactly one key" in err and KEY not in err


def test_group_aliases_are_understood():
    assert probe_live.parse_groups("sign-in,performance") == ["signin", "headers"]
    assert probe_live.parse_groups(None) == list(probe_live.GROUPS)


# ---------------------------------------------------------------------------
# The pure helpers.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, body, missing",
    [
        (404, {"type": "urn:threefold:error:not-found", "title": "Not Found", "detail": "Endpoint '/api/x' not found"}, True),
        (404, {"message": "Not Found"}, True),
        (404, None, True),
        (405, {"title": "Method Not Allowed"}, True),
        (501, None, True),
        (404, {"type": "urn:threefold:error:session-not-found", "title": "No Such Session", "detail": "none"}, False),
        (400, {"title": "Bad Request"}, False),
        (403, {"title": "Forbidden"}, False),
    ],
)
def test_a_missing_route_is_told_apart_from_a_missing_resource(status, body, missing):
    raw = b"<html>gone</html>" if body is None else json.dumps(body).encode()
    assert probe_live.route_missing(Response(status, {}, raw)) is missing


def test_percentiles_are_nearest_rank():
    samples = list(range(1, 21))
    assert probe_live.percentile(samples, 50) == 10
    assert probe_live.percentile(samples, 95) == 19
    assert probe_live.percentile([], 95) == 0.0


def test_the_base_keeps_its_trailing_slash_and_must_be_a_url():
    assert probe_live.normalise_base("https://acme.invalid/prod") == "https://acme.invalid/prod/"
    assert probe_live.normalise_base("https://acme.invalid/prod/") == "https://acme.invalid/prod/"
    for bad in ("acme.invalid/prod/", "ftp://acme.invalid/", "https://acme.invalid/prod/?k=1"):
        with pytest.raises(ValueError):
            probe_live.normalise_base(bad)


def test_public_names_are_scanned_everywhere_and_reported_masked():
    payload = {
        "by_project": [{"project": "Acme-Core"}, {"project": "unlabelled"}, {"project": "Unlisted-Repo"}],
        "rows": [{"project_name": "Acme-Ledger", "developer_id": "0a1b2c3d"}, {"developer_id": "a.person"}],
        "by_developer": [{"developer": ""}],
        "reviews": [{"reviewed_by": "not-a-hash"}],
        # What the service writes for a row with no project or no developer
        # names nobody, so a public page may show it.
        "unattributed": [{"project": "unattributed", "developer": "unattributed"}],
    }
    found = probe_live.scan_public_names(payload, PATTERN)
    assert len(found) == 3
    joined = " ".join(found)
    assert "Unlisted-Repo" not in joined and "a.person" not in joined
    assert "$.by_project[2].project" in joined and "$.rows[1].developer_id" in joined


def test_a_rules_state_follows_from_its_counts():
    state = probe_live.expected_rule_state
    assert state({"false_alarms": 1, "unreviewed": 3, "would_refuse": 4}) == "noisy"
    assert state({"false_alarms": 0, "unreviewed": 1, "would_refuse": 4}) == "needs_review"
    assert state({"false_alarms": 0, "unreviewed": 0, "would_refuse": 4}) == "ready"
    assert state({"false_alarms": 0, "unreviewed": 0, "would_refuse": 0}) == "quiet"


def test_a_digest_header_is_read_in_any_of_its_usual_forms():
    body = b"print('acme')\n"
    digest = hashlib.sha256(body).hexdigest()
    b64 = base64.b64encode(hashlib.sha256(body).digest()).decode()
    for headers in ({"X-Content-SHA256": digest}, {"Digest": f"sha-256={b64}"}, {"Repr-Digest": f"sha-256=:{b64}:"}):
        name, value = probe_live.digest_from_headers(Response(200, headers, body))
        assert value == digest
    assert probe_live.digest_from_headers(Response(200, {"Content-Type": "text/plain"}, body)) is None


def test_the_synthetic_credential_has_the_shape_the_gate_matches():
    from threefold.domain.boundary_guard import SecretScanner

    clean, label = SecretScanner.scan_payload(f"echo {probe_live.synthetic_credential()}")
    assert not clean and "AWS_ACCESS_KEY" in label


# ---------------------------------------------------------------------------
# The one function that touches the network, against a server on loopback.
# ---------------------------------------------------------------------------


class _Loopback(BaseHTTPRequestHandler):
    """Answers each path the way one kind of stack, or one kind of broken stack, would."""

    def log_message(self, *args: Any) -> None:  # the suite's output is not the place
        pass

    def _send(self, status: int, headers: List[Tuple[str, str]], body: bytes) -> None:
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        if self.path == "/ok":
            # Sent twice on purpose: a browser rejects the pair, and the probe
            # can only say so if it keeps both.
            self._send(200, [("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*"),
                             ("Access-Control-Allow-Origin", "*")], b'{"status": "HEALTHY"}')
        elif self.path == "/missing":
            self._send(404, [("Content-Type", "application/problem+json")],
                       b'{"type": "urn:threefold:error:not-found", "title": "Not Found", "status": 404}')
        elif self.path == "/moved":
            self._send(302, [("Location", "/elsewhere")], b"")
        elif self.path == "/elsewhere":
            self._send(200, [("Content-Type", "text/plain")], b"followed")
        elif self.path.startswith("/short"):
            # Promises 500 bytes and sends 10, then closes: IncompleteRead.
            self.send_response(200)
            self.send_header("Content-Length", "500")
            self.end_headers()
            self.wfile.write(b"0123456789")
        elif self.path == "/garbage":
            # Not HTTP at all: BadStatusLine.
            self.wfile.write(b"NOT-HTTP\r\n\r\n")
        else:
            self._send(404, [], b"")


@pytest.fixture
def loopback():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Loopback)
    server.seen = []
    # A short poll, so shutting it down does not cost half a second a test.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_real_request_keeps_the_status_the_body_and_every_header(loopback):
    server, base = loopback
    got = probe_live.http_request("GET", f"{base}/ok", headers={"Accept": "application/json"}, timeout=5)
    assert got.status == 200 and got.obj() == {"status": "HEALTHY"}
    assert got.content_type == "application/json"
    assert got.header_values("access-control-allow-origin") == ["*", "*"]
    assert got.header("access-control-allow-origin") == "*, *"
    head = probe_live.http_request("HEAD", f"{base}/ok", timeout=5)
    assert head.status == 200 and head.body == b""


def test_an_error_status_is_a_response_not_an_exception(loopback):
    server, base = loopback
    got = probe_live.http_request("GET", f"{base}/missing", timeout=5)
    assert got.status == 404 and got.content_type == "application/problem+json"
    assert got.obj()["title"] == "Not Found" and got.error == ""


def test_a_redirect_is_reported_and_never_followed_with_the_key(loopback):
    server, base = loopback
    got = probe_live.http_request("GET", f"{base}/moved", headers={"X-API-Key": KEY}, timeout=5)
    assert got.status == 302 and got.header("location") == "/elsewhere"
    assert [path for _, path, _ in server.seen] == ["/moved"]


@pytest.mark.parametrize("path, kind", [("/short", "IncompleteRead"), ("/garbage", "BadStatusLine")])
def test_a_broken_reply_is_no_answer_rather_than_a_crash(loopback, path, kind):
    server, base = loopback
    got = probe_live.http_request("GET", f"{base}{path}", timeout=5)
    assert got.status == 0 and got.error.startswith(kind), got.error


def test_nobody_listening_is_no_answer_rather_than_a_crash():
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
    got = probe_live.http_request("GET", f"http://127.0.0.1:{port}/status", timeout=5)
    assert got.status == 0 and got.error


def test_a_broken_reply_fails_one_check_and_the_run_goes_on(loopback, tmp_path):
    server, base = loopback
    # Every page of this "stack" is a body cut short: each check fails with
    # what happened, and the run still ends with its evidence written.
    out = tmp_path / "PROBES.md"
    code = probe_live.main(["--base", f"{base}/short/", "--only", "availability,contract", "--out", str(out),
                            "--pace", "0", "--timeout", "5"])
    assert code == 1
    written = out.read_text(encoding="utf-8")
    assert "## availability" in written and "## contract" in written


# ---------------------------------------------------------------------------
# The probe against the real handler, so it cannot drift from the code.
# ---------------------------------------------------------------------------


def _through_the_handler(method, url, headers=None, body=None, timeout=20.0):
    """Delivers one probe request to `lambda_handler` as API Gateway would."""
    from threefold.infrastructure.security_middleware import _global_rate_limiter
    from threefold.interfaces.api_handlers import lambda_handler

    # The probe paces itself for the deployed stack; in process there is no
    # reason to wait, so each request starts with a full bucket.
    _global_rate_limiter.reset()
    parsed = urlparse(url)
    event = {
        "httpMethod": method,
        "rawPath": parsed.path,
        "path": parsed.path,
        "queryStringParameters": {k: v[0] for k, v in parse_qs(parsed.query).items()} or None,
        "headers": {**dict(headers or {}), "host": parsed.netloc},
        "body": body.decode("utf-8") if body else None,
        "requestContext": {
            "stage": "prod",
            "domainName": parsed.netloc,
            "requestId": "acme-probe",
            "http": {"method": method, "path": parsed.path, "sourceIp": "203.0.113.7"},
        },
    }
    result = lambda_handler(event, None)
    payload = result.get("body") or ""
    return Response(
        result.get("statusCode", 200),
        result.get("headers") or {},
        payload.encode("utf-8") if isinstance(payload, str) else bytes(payload),
        elapsed_ms=1.0,
    )


def test_the_read_only_groups_agree_with_the_real_handler(monkeypatch):
    probe, _ = _run(
        monkeypatch, _through_the_handler, base=LOCAL,
        groups=["availability", "contract", "access", "headers"], read_only=True, expect="public",
    )
    assert _failures(probe) == []
    checks = _by_name(probe)
    # The suite runs offline, so readiness is the excused local case, not a pass.
    assert checks["availability/readyz"].status == probe_live.SKIP
    assert checks["availability/page /index.html"].status == probe_live.PASS
    assert checks["contract/GET /sessions/{session_id}"].status == probe_live.PASS
    assert checks["access/POST /rules needs the key"].status == probe_live.PASS
    assert checks["headers/CORS preflight for a keyed POST"].status == probe_live.PASS


def test_a_private_handler_is_closed_to_the_anonymous_and_the_key_never_shows(monkeypatch):
    monkeypatch.setenv("PUBLIC_READS", "false")
    monkeypatch.setenv("THREEFOLD_API_KEYS", KEY)
    probe, lines = _run(monkeypatch, _through_the_handler, base=LOCAL, groups=["access"], key=KEY, expect="private")
    assert _failures(probe) == []
    checks = _by_name(probe)
    assert "401 without a key, 200 with the key" in checks["access/GET /api/insights private"].evidence
    assert KEY not in probe.markdown()
    assert all(KEY not in line for line in lines)
