#!/usr/bin/env python3
"""Checks a deployed Threefold stack against every claim the project makes.

    python scripts/probe_live.py --base https://<api>/prod/ [--key-file PATH]
        [--expect public|private] [--out docs/evidence/PROBES_<date>.md]
        [--only group,...] [--read-only] [--strict]

Each check ends PASS, FAIL or SKIP with one line of evidence, the whole run is
written to a dated markdown file, and the exit code is 1 when anything failed.
The groups are availability, contract, access, governance, application,
signin, distribution and headers (headers and performance).

Three properties matter more than any single check:

- The operator key, and every sign-in code and session token minted during the
  run, is never printed, logged or written. The key travels only as a request
  header, never in a URL, and only over https unless the base is this machine.
  Every line that leaves the probe goes through one scrubber, and text the
  stack chose is scrubbed before it is shortened, so a stack that echoes a
  credential back cannot make the terminal or the evidence file carry it, or
  any leading part of it, either.
- A probe that writes can be told not to. Governance calls use session ids
  prefixed `probe-<runid>-` and the synthetic project `Acme-Probe`, so the owner
  can find and discount them; the evidence header lists every kind of write
  the run actually made, including the demo simulations the contract group
  calls, and a run that will write to a host other than this machine says so
  before it starts. `--read-only` skips every check that would record a
  decision or change a project. The sign-in group still runs under
  it, because it touches neither and revokes the one session it mints, and
  without it a private stack could not be checked at all without writes.
  Bodies sent to routes only to prove they exist are
  deliberately malformed JSON, so even a route whose guard has regressed
  answers 400 instead of acting.
- An endpoint of the 2026-09-22 application contract that is not deployed yet
  is reported as SKIP "not deployed yet", never as a pass and never as a
  failure, unless `--strict` asks for it to fail. Everything that was deployed
  before that contract fails on a 404 as it always should. A 501, which only
  the local development server answers (it defines no DELETE), is excused on
  a loopback base and fails anywhere else.

Standard library only, as the repository's rules require.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import http.client
import io
import json
import math
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

GROUPS: Tuple[str, ...] = (
    "availability",
    "contract",
    "access",
    "governance",
    "application",
    "signin",
    "distribution",
    "headers",
)
# The names a reader is likely to type for the same group, so `--only sign-in`
# does not fail on a hyphen.
GROUP_ALIASES = {
    "sign-in": "signin",
    "performance": "headers",
    "headers-and-performance": "headers",
}

DEFAULT_PROJECT_PATTERN = r"^Acme-[A-Za-z0-9-]{1,40}$"
SANDBOX_PATTERN = re.compile(r"^Acme-Sandbox-[0-9a-f]{8}$")
DEVELOPER_HASH = re.compile(r"^[0-9a-f]{8}$")
# A run id is the eight hex digits a sandbox name ends in, so the name this run
# writes under is one the sandbox guard must accept.
RUN_ID = re.compile(r"^[0-9a-f]{8}$")
PROBE_PROJECT = "Acme-Probe"
# What the service shows for a row that carries no value at all. A placeholder
# names nobody, so a public page may show it.
PROJECT_PLACEHOLDERS = ("unlabelled", "unattributed")
DEVELOPER_PLACEHOLDERS = ("unattributed",)
# The project the service's demo simulations record under.
SIMULATION_PROJECT = "Acme-Sim"

REDACTED = "[redacted]"
# A line shortened through a secret keeps the secret's first characters in
# front of the marker. Six or more of them are held back; fewer say nothing.
SHORTEST_CUT_FRAGMENT = 6
CUT_MARKER = re.compile(r"\.\.\.|…")

# Invalid JSON on purpose. Sent to a route only to learn whether it is there: a
# route that parses its body answers 400, a guarded one answers 401 or 403
# before it parses anything, and neither can change a thing.
MALFORMED = b'{"probe": '

# Pages that were deployed before the 2026-09-22 contract. A 404 here is a
# regression and fails.
LEGACY_PAGES = (
    "",
    "index.html",
    "console.html",
    "rules.html",
    "connect.html",
    "sessions.html",
    "settings.html",
    "swagger.html",
)
# Pages and files the 2026-09-22 contract adds. Until the tracks building them
# are merged and deployed, a 404 here means "not deployed yet".
CONTRACT_PAGES = ("dashboard.html", "app")
CONTRACT_ASSETS = ("assets/threefold.js",)

# The page reads the access contract names. The first three existed before the
# contract, so their absence fails; the others are contract endpoints.
LEGACY_READS = ("api/insights", "api/sessions", "rules")
CONTRACT_READS = ("api/overview", "api/decisions", "api/projects")

# The JSON reads of the application contract whose presence in /openapi.json is
# checked once they answer. Writes are not probed for this: a guard refuses them
# before routing, so an answer proves the guard and not the route.
DOCUMENTED_APP_READS = (
    ("/api/overview", "api/overview"),
    ("/api/decisions", "api/decisions"),
    ("/api/decision", "api/decision"),
    ("/api/projects", "api/projects"),
    ("/api/projects/{name}", f"api/projects/{PROBE_PROJECT}"),
    ("/api/auth/whoami", "api/auth/whoami"),
)

# Documented operations that act whatever their body says, so the contract
# group cannot prove they exist without doing what they do.
ACTS_WHATEVER_THE_BODY = {
    ("POST", "/simulate-loop"),
    ("POST", "/simulate-secret"),
    ("POST", "/api/sandbox"),
}

# What readiness reports for the two dependencies a server started with
# THREEFOLD_OFFLINE runs without on purpose. Excused only on a loopback base: on
# a deployed host the same words mean the function lost its table or its model
# client, and that fails.
UNBOUND_DEPENDENCY = re.compile(r"No DynamoDB table bound|No Bedrock runtime client", re.IGNORECASE)
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

GATE_RULE_KEYS = ("LOOP", "PROTECTED_PATH", "UNREADABLE_WRITE", "BUDGET")
FALLBACK_BOUNDARY_RULE = "python-domain-stays-pure"

OVERVIEW_KEYS = (
    "window_days", "generated_at", "source", "totals", "series",
    "by_agent", "by_origin", "by_rule", "by_project", "stages",
)
OVERVIEW_TOTALS = (
    "calls", "approved", "refused", "would_refuse", "needs_review",
    "false_alarms", "projects", "agents",
)
SERIES_KEYS = ("day", "approved", "observed", "refused")
BY_PROJECT_KEYS = (
    "project", "stage", "configured", "calls", "refused", "would_refuse",
    "needs_review", "last_seen",
)
ROW_CONTRACT_FIELDS = ("rule_key", "stage", "hook_mode", "review")
PROJECT_KEYS = (
    "project", "stage", "configured", "observe_rules", "created_at", "promoted_at",
    "last_seen", "calls", "refused", "would_refuse", "needs_review", "agents", "hook_modes",
)
SUMMARY_KEYS = (
    "stage", "days_observed", "calls_observed", "would_have_refused", "reviewed",
    "false_alarms", "false_alarm_rate", "rules_ready", "rules_quiet", "rules_noisy",
    "rules_needing_review",
)
RULE_KEYS = (
    "rule_key", "kind", "mode_now", "would_refuse", "correct", "false_alarms",
    "unreviewed", "last_seen", "state", "recommendation",
)

# The synthetic calls the governance group sends. Every name is Acme, and the
# paths are relative, so nothing here names a real project or a real machine.
DOMAIN_WRITE = {
    "file_path": "src/domain/acme_probe_order.py",
    "content": "import boto3\n\n\nclass AcmeProbeOrder:\n    pass\n",
}
HEREDOC_WRITE = "cat > src/domain/acme_probe_user.py <<'EOF'\nimport boto3\nEOF"
REDIRECT_WRITE = "echo 'import boto3' >> src/domain/acme_probe_ledger.py"
SETTINGS_WRITE = {"file_path": ".claude/settings.json", "content": '{"hooks": {}}\n'}
NO_VERIFY_COMMIT = 'git commit --no-verify -m "wip: acme probe"'
LOOPING_COMMAND = "npm run build"
DIFFERENT_COMMAND = "npm test"


def synthetic_credential() -> str:
    """An access key id in the shape the secret gate matches, made of Acme text.

    Assembled at run time so this file never holds the shape itself, which a
    secret scanner in front of the repository, or the owner's own hook, would
    rightly refuse.
    """
    return "".join(("AK", "IA", "ACMEPROBE", "0" * 7))


# ---------------------------------------------------------------------------
# The one HTTP seam. Tests replace `http_request` and nothing else.
# ---------------------------------------------------------------------------


@dataclass
class Response:
    """What one request came back with, or why it came back with nothing."""

    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    elapsed_ms: float = 0.0
    error: str = ""
    # Every header as sent, duplicates included. A header sent twice is joined
    # in `headers`, which hides that a browser rejects a doubled CORS header.
    raw_headers: List[Tuple[str, str]] = field(default_factory=list)
    method: str = ""
    path: str = ""

    def __post_init__(self) -> None:
        self.headers = {str(k).lower(): str(v) for k, v in (self.headers or {}).items()}
        if not self.raw_headers:
            self.raw_headers = list(self.headers.items())

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")

    def header_values(self, name: str) -> List[str]:
        wanted = name.lower()
        return [value for key, value in self.raw_headers if key.lower() == wanted]

    @property
    def content_type(self) -> str:
        return self.header("content-type").split(";")[0].strip().lower()

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def obj(self) -> Dict[str, Any]:
        """The body as a JSON object, or an empty one, so a check never tests for None."""
        value = self.json()
        return value if isinstance(value, dict) else {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Reports a redirect as the response it is.

    Following one silently would let a stack that bounces a page elsewhere pass
    as serving it, and would carry the key header to wherever it pointed.
    """

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = 20.0,
) -> Response:
    """Sends one request and returns what came back, never raising for a status."""
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    started = time.perf_counter()
    try:
        with _OPENER.open(request, timeout=timeout) as reply:
            payload = reply.read()
            status = reply.status
            raw = list(reply.headers.items())
    except urllib.error.HTTPError as problem:
        try:
            payload = problem.read()
        except (OSError, http.client.HTTPException):
            payload = b""
        status = problem.code
        raw = list(problem.headers.items()) if problem.headers else []
    # HTTPException is not an OSError: a body cut short (IncompleteRead) or a
    # reply that is not HTTP at all (BadStatusLine) raise it, and either must be
    # reported as no answer rather than end the run.
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as failure:
        reason = getattr(failure, "reason", failure)
        return Response(
            status=0,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            error=f"{type(failure).__name__}: {reason}",
        )
    elapsed = (time.perf_counter() - started) * 1000.0
    joined: Dict[str, str] = {}
    for key, value in raw:
        lower = key.lower()
        joined[lower] = f"{joined[lower]}, {value}" if lower in joined else value
    return Response(status=status, headers=joined, body=payload, elapsed_ms=elapsed, raw_headers=raw)


# ---------------------------------------------------------------------------
# Small pure helpers, tested on their own.
# ---------------------------------------------------------------------------


def normalise_base(base: str) -> str:
    """The base URL with its trailing slash.

    API Gateway answers the bare stage path with its own 404 before the
    function is reached, so the slash is part of the address, not decoration.
    """
    base = base.strip()
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("--base must be an http or https URL, such as https://<api>/prod/")
    if parsed.query or parsed.fragment:
        raise ValueError("--base must not carry a query or a fragment")
    return base if base.endswith("/") else base + "/"


def route_missing(resp: Response) -> bool:
    """True when the answer says there is no route, rather than no such thing.

    A 404 carrying the service's own "Endpoint '...' not found" problem, API
    Gateway's `{"message": "Not Found"}`, or no JSON at all means nothing is
    routed there. A 404 with any other problem, such as No Such Session, is a
    route answering about a resource, and the route is there.
    """
    if resp.status in (405, 501):
        return True
    if resp.status != 404:
        return False
    data = resp.json()
    if not isinstance(data, dict):
        return True
    if data.get("message") == "Not Found" and len(data) == 1:
        return True
    detail = str(data.get("detail") or data.get("error") or "")
    return bool(re.fullmatch(r"Endpoint '.*' not found", detail))


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile, so p95 of twenty samples is the nineteenth."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def one_line(text: Any, limit: int = 240) -> str:
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 3] + "..."


def encoded_forms(secret: str) -> List[str]:
    """The ways a secret can come back in a body or a header: as sent, URL-encoded,
    escaped inside a JSON string, and with its whitespace collapsed."""
    forms = {
        secret,
        urllib.parse.quote(secret, safe=""),
        urllib.parse.quote_plus(secret),
        json.dumps(secret)[1:-1],
        " ".join(secret.split()),
    }
    return sorted((form for form in forms if form), key=len, reverse=True)


def masked(value: str) -> str:
    """Enough of a value to find it again, never enough to read it.

    A name that should not be public must not become public by being reported
    in an evidence file that may be committed.
    """
    return f"{value[:2]!r}... ({len(value)} chars)"


def scan_public_names(value: Any, pattern: "re.Pattern[str]", where: str = "$") -> List[str]:
    """Every project name outside the pattern and every developer value that is not a short hash.

    The placeholders the service writes for a row with no project or no
    developer ("unattributed", and "unlabelled" for a project outside the
    pattern) name nobody, so they are not leaks. Returns locations with masked
    values, never the values themselves.
    """
    found: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            place = f"{where}.{key}"
            if isinstance(item, str):
                if key in ("project", "project_name"):
                    if item not in PROJECT_PLACEHOLDERS and not pattern.fullmatch(item):
                        found.append(f"{place}={masked(item)}")
                elif key in ("developer", "developer_id", "reviewed_by"):
                    if item and item not in DEVELOPER_PLACEHOLDERS and not DEVELOPER_HASH.fullmatch(item):
                        found.append(f"{place}={masked(item)}")
            else:
                found.extend(scan_public_names(item, pattern, place))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(scan_public_names(item, pattern, f"{where}[{index}]"))
    return found


def expected_rule_state(rule: Dict[str, Any]) -> str:
    """The state the contract derives from a rule's counts."""
    if _num(rule.get("false_alarms")) > 0:
        return "noisy"
    if _num(rule.get("unreviewed")) > 0:
        return "needs_review"
    if _num(rule.get("would_refuse")) > 0:
        return "ready"
    return "quiet"


def readiness_problems(payload: Dict[str, Any]) -> List[str]:
    """What is wrong with a `GET /api/projects/<name>` body, against the contract."""
    problems: List[str] = []
    for key in ("project", "config", "readiness"):
        if key not in payload:
            problems.append(f"no '{key}'")
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), dict) else {}
    summary = readiness.get("summary") if isinstance(readiness.get("summary"), dict) else None
    rules = readiness.get("rules") if isinstance(readiness.get("rules"), list) else None
    if summary is None:
        problems.append("no readiness.summary")
        summary = {}
    else:
        missing = [key for key in SUMMARY_KEYS if key not in summary]
        if missing:
            problems.append("summary lacks " + ",".join(missing))
    if rules is None:
        problems.append("no readiness.rules")
        return problems
    counts = {"ready": 0, "quiet": 0, "noisy": 0, "needs_review": 0}
    keys_seen = set()
    for rule in rules:
        if not isinstance(rule, dict):
            problems.append("a rule that is not an object")
            continue
        name = str(rule.get("rule_key", "?"))
        keys_seen.add(name)
        missing = [key for key in RULE_KEYS if key not in rule]
        if missing:
            problems.append(f"{name} lacks " + ",".join(missing))
        if rule.get("kind") not in ("layering", "gate"):
            problems.append(f"{name} kind={rule.get('kind')!r}")
        if rule.get("mode_now") not in ("observe", "enforce"):
            problems.append(f"{name} mode_now={rule.get('mode_now')!r}")
        state = rule.get("state")
        if state in counts:
            counts[state] += 1
        want = expected_rule_state(rule)
        if state != want:
            problems.append(f"{name} state={state!r} but its counts make it {want!r}")
    absent = [key for key in GATE_RULE_KEYS if key not in keys_seen]
    if absent:
        problems.append("gate keys missing: " + ",".join(absent))
    for state, summary_key in (
        ("ready", "rules_ready"),
        ("quiet", "rules_quiet"),
        ("noisy", "rules_noisy"),
        ("needs_review", "rules_needing_review"),
    ):
        if summary_key in summary and _num(summary.get(summary_key)) != counts[state]:
            problems.append(f"summary {summary_key}={summary.get(summary_key)} but {counts[state]} rules are {state}")
    return problems


def overview_problems(payload: Dict[str, Any]) -> List[str]:
    problems = [f"no '{key}'" for key in OVERVIEW_KEYS if key not in payload]
    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    missing = [key for key in OVERVIEW_TOTALS if key not in totals]
    if missing:
        problems.append("totals lack " + ",".join(missing))
    if payload.get("source") != "rollups":
        problems.append(f"source={payload.get('source')!r}, not 'rollups'")
    series = payload.get("series") if isinstance(payload.get("series"), list) else []
    for index, day in enumerate(series):
        lacking = [key for key in SERIES_KEYS if not isinstance(day, dict) or key not in day]
        if lacking:
            problems.append(f"series[{index}] lacks " + ",".join(lacking))
            break
    for index, row in enumerate(payload.get("by_project") or []):
        lacking = [key for key in BY_PROJECT_KEYS if not isinstance(row, dict) or key not in row]
        if lacking:
            problems.append(f"by_project[{index}] lacks " + ",".join(lacking))
            break
    stages = payload.get("stages") if isinstance(payload.get("stages"), dict) else {}
    if not {"observe", "enforce"} <= set(stages):
        problems.append("stages lacks observe/enforce")
    return problems


def digest_from_headers(resp: Response) -> Optional[Tuple[str, str]]:
    """A SHA-256 the server states for the body, as (header, hex), if it states one.

    The contract names no header, so the usual forms are all read: a bare hex
    value under any header naming sha256, and the base64 value of Digest,
    Content-Digest and Repr-Digest.
    """
    for name, value in resp.headers.items():
        text = value.strip()
        if "sha256" in name or "sha-256" in name:
            if re.fullmatch(r"[0-9a-fA-F]{64}", text):
                return name, text.lower()
        if name in ("digest", "content-digest", "repr-digest"):
            match = re.search(r"sha-256=:?([A-Za-z0-9+/=]+):?", text, re.IGNORECASE)
            if match:
                try:
                    return name, base64.b64decode(match.group(1)).hex()
                except ValueError:
                    continue
    return None


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _status_list(responses: Iterable[Dict[str, Any]]) -> str:
    return ", ".join(str(item.get("status", "?")) for item in responses)


# ---------------------------------------------------------------------------
# The probe.
# ---------------------------------------------------------------------------


@dataclass
class Check:
    group: str
    name: str
    status: str
    evidence: str


class Probe:
    """Runs the groups against one stack and keeps what each check found."""

    def __init__(
        self,
        base: str,
        *,
        key: Optional[str] = None,
        expect: Optional[str] = None,
        read_only: bool = False,
        strict: bool = False,
        pace: Optional[float] = None,
        timeout: float = 20.0,
        project_pattern: str = DEFAULT_PROJECT_PATTERN,
        p95_budget_ms: float = 2000.0,
        runid: Optional[str] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        emit: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.base = normalise_base(base)
        self._key = key or None
        if self._key and urllib.parse.urlparse(self.base).scheme != "https" and not self.is_local:
            # Refused before a single request: over plain http the key header
            # is readable by anything on the path, and a typo in the scheme
            # should not be what hands it over.
            raise ValueError("a key file needs an https base unless the base is this machine (127.0.0.1 or localhost)")
        self.expect = expect
        self.read_only = read_only
        self.strict = strict
        self.pace = pace
        self.timeout = timeout
        self.project_pattern = re.compile(project_pattern)
        self.p95_budget_ms = p95_budget_ms
        self.runid = runid or secrets.token_hex(4)
        if not RUN_ID.fullmatch(self.runid):
            raise ValueError("a run id is eight lower-case hex digits, the form a sandbox name ends in")
        self._sleep = sleep
        self._clock = clock
        self._emit = emit or _print_line
        # Everything that must never leave the probe. Filled as secrets are
        # minted, before any evidence about them is built. The key, codes and
        # tokens are also held back when a line was cut through them; a link is
        # held back whole only, because its leading part is this stack's base
        # URL, which the evidence names on purpose.
        self._secrets: List[str] = []
        self._spread: Dict[str, "re.Pattern[str]"] = {}
        self._cut: Dict[str, "re.Pattern[str]"] = {}
        if self._key:
            self.keep_secret(self._key)
        self.results: List[Check] = []
        # What this run wrote to the stack, in plain words, for the evidence
        # header, so the owner can find every row it left and discount it.
        self.writes: List[str] = []
        self.requests = 0
        self.retried_429 = 0
        self._last_request: Optional[float] = None
        self._mode: Optional[str] = None
        self._openapi: Optional[Dict[str, Any]] = None
        self.started_at = datetime.datetime.now(datetime.timezone.utc)
        # What the governance group recorded, for the groups that read it back.
        self.verdicts: List[Dict[str, Any]] = []
        self.latency: Dict[str, Tuple[float, float, int]] = {}
        # "minted" once a sign-in session exists, "revoked" once it is gone, so
        # the evidence says what the run left behind.
        self._session_minted = ""
        self._sandbox = ""

    # -- secrets ---------------------------------------------------------

    @property
    def has_key(self) -> bool:
        return self._key is not None

    @property
    def is_local(self) -> bool:
        """True for a server on this machine, where the development harness runs offline."""
        host = (urllib.parse.urlparse(self.base).hostname or "").lower()
        return host in LOOPBACK_HOSTS or host.endswith(".localhost")

    def keep_secret(self, value: Any, whole_only: bool = False) -> None:
        """Holds a value back from every line from now on.

        `whole_only` is for a value whose leading part is not secret, such as a
        sign-in link that starts with the base URL: it is removed where it
        appears whole, and its code is held back on its own.
        """
        if not isinstance(value, str) or len(value) < 4 or value in self._secrets:
            return
        self._secrets.append(value)
        if len(value) >= 8:
            # The secret with any whitespace between its characters, which is
            # what collapsing or wrapping a line makes of one that holds some.
            self._spread[value] = re.compile(r"\s*".join(re.escape(ch) for ch in value if not ch.isspace()))
        if not whole_only and len(value) > SHORTEST_CUT_FRAGMENT:
            prefixes = (re.escape(value[:n]) for n in range(len(value) - 1, SHORTEST_CUT_FRAGMENT - 1, -1))
            self._cut[value] = re.compile(f"(?:{'|'.join(prefixes)})(?={CUT_MARKER.pattern})")

    def scrub(self, text: Any) -> str:
        """Removes every secret this run holds from a line, however it is encoded or cut.

        Callers scrub before they shorten or collapse anything, so a secret is
        still whole when it is looked for. The last pass is for a line that was
        cut before it arrived, by the stack or by a check: the leading part of a
        secret in front of a cut marker is removed too.
        """
        text = str(text)
        for secret in sorted(self._secrets, key=len, reverse=True):
            for form in encoded_forms(secret):
                text = text.replace(form, REDACTED)
            spread = self._spread.get(secret)
            if spread is not None:
                text = spread.sub(REDACTED, text)
        for pattern in self._cut.values():
            text = pattern.sub(REDACTED, text)
        return text

    def clip(self, text: Any, limit: int = 240) -> str:
        """Text the stack chose, scrubbed first and only then shortened to one line."""
        return one_line(self.scrub(text), limit)

    def wrote(self, what: str) -> None:
        """Notes one kind of write this run made, once, for the evidence header."""
        if what not in self.writes:
            self.writes.append(what)

    # -- requests --------------------------------------------------------

    def url(self, path: str) -> str:
        return self.base + path.lstrip("/")

    def session(self, suffix: str) -> str:
        return f"probe-{self.runid}-{suffix}"

    def _wait_for_pace(self) -> None:
        # The stack meters each caller with a bucket of sixty requests that
        # refills at two a second, so a full run paced by nothing is answered
        # 429 part way through and reads as a wall of failures.
        gap = self.pace if self.pace is not None else (0.5 if self.requests >= 40 else 0.0)
        if gap > 0 and self._last_request is not None:
            wait = gap - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        raw: Optional[bytes] = None,
        auth: bool = False,
        token: Optional[str] = None,
        key_override: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        query: Optional[Dict[str, Any]] = None,
    ) -> Response:
        """One request, retried while the stack says 429, with the key only in a header."""
        if query:
            path = f"{path}?{urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})}"
        sent = {"Accept": "application/json, text/html;q=0.9, */*;q=0.5", "User-Agent": "threefold-probe/1"}
        data: Optional[bytes] = None
        if raw is not None:
            data = raw
            sent["Content-Type"] = "application/json"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            sent["Content-Type"] = "application/json"
        if key_override is not None:
            sent["X-API-Key"] = key_override
        elif auth and self._key:
            sent["X-API-Key"] = self._key
        if token:
            sent["Authorization"] = f"Bearer {token}"
        sent.update(headers or {})
        attempts = 5
        resp = Response(status=0, error="not sent")
        for attempt in range(attempts):
            self._wait_for_pace()
            self.requests += 1
            resp = http_request(method, self.url(path), headers=sent, body=data, timeout=self.timeout)
            resp.method, resp.path = method, "/" + path.lstrip("/")
            if resp.status != 429 or attempt == attempts - 1:
                break
            self.retried_429 += 1
            retry_after = _num(resp.header("retry-after"))
            self._sleep(retry_after if 0 < retry_after <= 30 else min(8.0, 1.0 * 2 ** attempt))
        return resp

    def evaluate(
        self,
        suffix: str,
        tool: str,
        action: str,
        arguments: Dict[str, Any],
        *,
        origin: str = "page",
        explain: bool = False,
        dry_run: bool = False,
        project: str = PROBE_PROJECT,
        session: Optional[str] = None,
    ) -> Tuple[Response, Dict[str, Any]]:
        """Sends one synthetic tool call the way a page or a hook would."""
        payload: Dict[str, Any] = {
            "session_id": session or self.session(suffix),
            "project_name": project,
            "developer": "anonymous",
            "tool_name": tool,
            "action_type": action,
            "arguments": arguments,
            "agent": "page" if origin == "page" else "claude-code",
            "origin": origin,
            "explain": explain,
            "dry_run": dry_run,
        }
        if origin == "hook":
            payload["hook_mode"] = "managed"
        resp = self.request("POST", "evaluate-tool-call", body=payload, auth=True)
        data = resp.obj()
        if resp.status == 200:
            if project == PROBE_PROJECT:
                self.wrote(f"synthetic calls under session ids `probe-{self.runid}-*` and project `{PROBE_PROJECT}`")
            elif not SANDBOX_PATTERN.fullmatch(project):
                label = project if self.project_pattern.fullmatch(project) else "unlabelled"
                self.wrote(f"calls under session ids `probe-{self.runid}-*` sent as project `{project}`, recorded as `{label}`")
        if data.get("verdict_id"):
            self.verdicts.append(
                {
                    "project": project,
                    "session_id": payload["session_id"],
                    "verdict_id": data.get("verdict_id"),
                    "timestamp": data.get("timestamp"),
                    "status": data.get("status"),
                }
            )
        return resp, data

    # -- recording -------------------------------------------------------

    def record(self, group: str, name: str, status: str, evidence: str) -> str:
        # Scrubbed whole before it is shortened, and again after: a secret cut
        # in half no longer matches, so the order is what keeps it out. A name
        # can carry text the stack chose, such as a documented path.
        name = self.clip(name)
        line = self.scrub(self.clip(evidence))
        self.results.append(Check(group, name, status, line))
        self._emit(self.scrub(f"{status:<4}  {group}/{name}: {line}"))
        return status

    def run_check(self, group: str, name: str, fn: Callable[..., Tuple[str, str]], *args: Any) -> str:
        """Runs one check, and turns a crash in the probe into a FAIL rather than an abort."""
        try:
            status, evidence = fn(*args)
        except Exception as exc:  # noqa: BLE001 - one broken check must not end the run
            status, evidence = FAIL, f"the probe itself failed: {type(exc).__name__}: {exc}"
        return self.record(group, name, status, evidence)

    def describe(self, resp: Response) -> str:
        if resp.status == 0:
            return f"no response ({resp.error})"
        kind = resp.content_type or "no content type"
        return f"{resp.status} {kind}"

    def snippet(self, resp: Response, limit: int = 120) -> str:
        return self.clip(resp.text(), limit) if resp.body else "empty body"

    def not_implemented(self, resp: Response) -> Tuple[str, str]:
        """The verdict for a 501, which only the local development server has an excuse for.

        The standard library answers 501 for a method its handler does not
        define, and the development server defines no DELETE. A deployed stack
        routes every method to the function, so a 501 there is a finding.
        """
        if self.is_local:
            return (
                FAIL if self.strict else SKIP,
                f"{resp.method} {resp.path} answers 501: the local development server does not implement "
                f"{resp.method} (it answers GET, POST, HEAD and OPTIONS only)",
            )
        return FAIL, f"{resp.method} {resp.path} answers 501 Not Implemented on a deployed stack"

    def not_there(self, resp: Response) -> Optional[Tuple[str, str]]:
        """The verdict for a contract endpoint that is not answering, or None when it is."""
        if resp.status == 501:
            return self.not_implemented(resp)
        if route_missing(resp):
            return (
                FAIL if self.strict else SKIP,
                f"not deployed yet: {resp.method} {resp.path} answers {resp.status} (contract 2026-09-22)",
            )
        return None

    def shown_name(self, name: Any) -> str:
        """A project name as the evidence may repeat it: masked unless a public page may show it.

        A name that should not be public must not become public by being
        reported in an evidence file.
        """
        if not isinstance(name, str) or not name:
            return "a row with no project"
        if name in PROJECT_PLACEHOLDERS or self.project_pattern.fullmatch(name):
            return name
        return masked(name)

    def writes_skipped(self) -> Tuple[str, str]:
        return SKIP, "read-only run: this check records calls on the stack"

    # -- shared reads ----------------------------------------------------

    def reads_mode(self) -> str:
        """Whether this stack answers its page reads anonymously, as it shows itself."""
        if self._mode is None:
            resp = self.request("GET", "api/insights")
            if resp.status == 200:
                self._mode = "public"
            elif resp.status in (401, 403):
                self._mode = "private"
            else:
                self._mode = "unknown"
        return self._mode

    def expected_mode(self) -> str:
        return self.expect or self.reads_mode()

    @property
    def detected_mode(self) -> Optional[str]:
        """What an anonymous read showed, or None when no group asked, without asking now."""
        return self._mode

    def openapi(self) -> Dict[str, Any]:
        if self._openapi is None:
            resp = self.request("GET", "openapi.json")
            self._openapi = resp.obj() if resp.status == 200 else {}
        return self._openapi

    # -- running ---------------------------------------------------------

    def run(self, groups: Sequence[str]) -> List[Check]:
        for group in GROUPS:
            if group in groups:
                try:
                    getattr(self, f"group_{group}")()
                except Exception as exc:  # noqa: BLE001 - one broken group must not end the run
                    # Some requests are made outside any single check, such as
                    # the spec the contract group walks. A crash there fails
                    # the group and the run goes on to write its evidence.
                    self.record(
                        group, "group stopped", FAIL,
                        f"the probe itself failed outside a check: {type(exc).__name__}: {exc}; "
                        "the rest of this group did not run",
                    )
        return self.results

    @property
    def failed(self) -> bool:
        return any(check.status == FAIL for check in self.results)

    # =====================================================================
    # availability
    # =====================================================================

    def group_availability(self) -> None:
        group = "availability"
        for page in LEGACY_PAGES:
            self.run_check(group, f"page /{page}", self._page, page, False)
        for page in CONTRACT_PAGES:
            self.run_check(group, f"page /{page}", self._page, page, True)
        for asset in CONTRACT_ASSETS:
            self.run_check(group, f"asset /{asset}", self._asset, asset)
        self.run_check(group, "status", self._status)
        self.run_check(group, "readyz", self._readyz)
        self.run_check(group, "hook served as text", self._hook_script)
        self.run_check(group, "hook under its old names", self._hook_old_names)

    def _page(self, page: str, contract: bool) -> Tuple[str, str]:
        got = self.request("GET", page)
        if contract:
            absent = self.not_there(got)
            if absent:
                return absent
        if got.status != 200:
            return FAIL, f"GET {self.describe(got)}: {self.snippet(got)}"
        if got.content_type != "text/html":
            return FAIL, f"GET 200 but served as {got.content_type or 'nothing'}, not text/html"
        if not got.body:
            return FAIL, "GET 200 text/html with an empty body"
        if b"__THREEFOLD_BASE_PATH__" in got.body:
            return FAIL, "GET 200 but the page still carries the literal __THREEFOLD_BASE_PATH__"
        head = self.request("HEAD", page)
        if head.status != 200:
            return FAIL, f"GET 200 text/html {len(got.body)} B, but HEAD {self.describe(head)}"
        if head.body:
            return FAIL, f"GET 200, HEAD 200 but with a {len(head.body)} B body"
        return PASS, f"GET 200 text/html {len(got.body)} B, base path substituted; HEAD 200 empty"

    def _asset(self, asset: str) -> Tuple[str, str]:
        got = self.request("GET", asset)
        absent = self.not_there(got)
        if absent:
            return absent
        if got.status != 200:
            return FAIL, f"GET {self.describe(got)}"
        if "javascript" not in got.content_type:
            return FAIL, f"GET 200 served as {got.content_type or 'nothing'}, not JavaScript"
        if b"__THREEFOLD_BASE_PATH__" in got.body:
            return FAIL, "GET 200 but the literal __THREEFOLD_BASE_PATH__ was not substituted"
        return PASS, f"GET 200 {got.content_type} {len(got.body)} B, base path substituted"

    def _status(self) -> Tuple[str, str]:
        got = self.request("GET", "status")
        data = got.obj()
        if got.status != 200 or got.content_type != "application/json":
            return FAIL, f"GET {self.describe(got)}: {self.snippet(got)}"
        if data.get("status") != "HEALTHY":
            return FAIL, f"200 but status={data.get('status')!r}"
        return PASS, f"200 application/json, status HEALTHY, {len(data.get('active_rules') or [])} gates listed"

    def _readyz(self) -> Tuple[str, str]:
        got = self.request("GET", "readyz")
        if got.status in (401, 403) and self.has_key:
            got = self.request("GET", "readyz", auth=True)
        if got.status in (401, 403):
            return SKIP, f"answers {got.status}: this stack keeps readiness behind the operator key and none was given"
        data = got.obj()
        parts = [s for s in data.get("subsystems") or [] if isinstance(s, dict)]
        listed = ", ".join(f"{s.get('name')} {s.get('status')}" for s in parts) or "no subsystems listed"
        if got.status == 200 and data.get("status") == "READY":
            return PASS, f"200 READY: {listed}"
        degraded = [s for s in parts if s.get("status") != "HEALTHY"]
        if (
            got.status == 503
            and self.is_local
            and degraded
            and all(UNBOUND_DEPENDENCY.search(str(s.get("details", ""))) for s in degraded)
        ):
            return SKIP, (
                f"503 from a local server running without its dependencies bound ({listed}); "
                "a deployed stack must answer 200 READY"
            )
        return FAIL, f"{self.describe(got)} status={data.get('status')!r}: {listed}"

    def _hook_script(self) -> Tuple[str, str]:
        got = self.request("GET", "hooks/threefold_hook.py")
        if got.status != 200:
            return FAIL, f"GET {self.describe(got)}"
        if got.content_type != "text/plain":
            return FAIL, f"200 but served as {got.content_type or 'nothing'}, not text/plain"
        try:
            compile(got.text(), "threefold_hook.py", "exec")
        except SyntaxError as bad:
            return FAIL, f"200 text/plain but not valid Python: line {bad.lineno}"
        return PASS, f"200 text/plain, {len(got.body)} B of valid Python, sha256 {_sha256(got.body)[:12]}"

    def _hook_old_names(self) -> Tuple[str, str]:
        current = self.request("GET", "hooks/threefold_hook.py")
        digests = []
        for old in ("hooks/claude_code_hook.py", "claude_code_hook.py"):
            got = self.request("GET", old)
            if got.status != 200:
                return FAIL, f"/{old} answers {self.describe(got)}; an install command copied before the rename breaks"
            digests.append(_sha256(got.body))
        if current.status == 200 and any(d != _sha256(current.body) for d in digests):
            return FAIL, "the old names serve a different file from /hooks/threefold_hook.py"
        return PASS, "/hooks/claude_code_hook.py and /claude_code_hook.py serve the same script"

    # =====================================================================
    # contract
    # =====================================================================

    def group_contract(self) -> None:
        group = "contract"
        spec = self.openapi()
        paths = spec.get("paths") if isinstance(spec.get("paths"), dict) else {}
        self.run_check(group, "openapi.json served", self._openapi_served, paths)
        for path in sorted(paths):
            operations = paths[path] if isinstance(paths[path], dict) else {}
            for method in ("get", "post", "put", "patch", "delete"):
                if method in operations:
                    self.run_check(group, f"{method.upper()} {path}", self._documented, method.upper(), path)
        self.run_check(group, "application reads are documented", self._app_reads_documented, paths)

    def _openapi_served(self, paths: Dict[str, Any]) -> Tuple[str, str]:
        if not paths:
            got = self.request("GET", "openapi.json")
            return FAIL, f"GET /openapi.json {self.describe(got)} with no paths"
        operations = sum(
            1 for ops in paths.values() if isinstance(ops, dict) for m in ops if m in ("get", "post", "put", "patch", "delete")
        )
        return PASS, f"200, {len(paths)} paths and {operations} operations documented"

    def _concrete(self, path: str) -> str:
        path = path.replace("{session_id}", self.session("contract"))
        path = path.replace("{name}", PROBE_PROJECT)
        return re.sub(r"\{[^}]+\}", f"probe-{self.runid}", path)

    def _documented(self, method: str, path: str) -> Tuple[str, str]:
        concrete = self._concrete(path)
        if (method, path) in ACTS_WHATEVER_THE_BODY:
            if self.read_only:
                return SKIP, "acts whatever its body says, so a read-only run does not call it"
            if path == "/api/sandbox":
                return SKIP, "creates a sandbox whatever its body says; the application group exercises it"
        if method == "GET":
            got = self.request("GET", concrete, auth=True)
        elif method == "DELETE":
            got = self.request("DELETE", concrete)
        else:
            # No key and a body that cannot parse: a guarded write is refused
            # before routing and an open one answers 400, so nothing changes.
            got = self.request(method, concrete, raw=MALFORMED)
        if got.status == 0:
            return FAIL, self.describe(got)
        if (method, path) in ACTS_WHATEVER_THE_BODY and 200 <= got.status < 300:
            self.wrote(f"`{method} {path}`, which records a demo session under project `{SIMULATION_PROJECT}`")
        if got.status == 501 and self.is_local:
            return self.not_implemented(got)
        if route_missing(got):
            return FAIL, f"documented, but {method} {concrete} answers {got.status}: {self.snippet(got, 80)}"
        if got.status >= 500 and not (path == "/readyz" and got.status == 503):
            return FAIL, f"answers {got.status} where a 4xx problem was due: {self.snippet(got, 80)}"
        title = got.obj().get("title")
        note = f" ({title})" if title else ""
        if method != "GET" and got.status in (401, 403):
            return PASS, (
                f"answered {got.status}{note} before routing: the guard proves the path is closed, "
                "not that the route behind it exists"
            )
        return PASS, f"answered {got.status}{note}"

    def _app_reads_documented(self, paths: Dict[str, Any]) -> Tuple[str, str]:
        answering, undocumented = [], []
        for documented, concrete in DOCUMENTED_APP_READS:
            got = self.request("GET", concrete, auth=True)
            if got.status == 0 or route_missing(got) or got.status == 501:
                continue
            answering.append(documented)
            operations = paths.get(documented)
            if not isinstance(operations, dict) or "get" not in operations:
                undocumented.append(documented)
        if not answering:
            return (FAIL if self.strict else SKIP), "not deployed yet: none of the application reads answers (contract 2026-09-22)"
        if undocumented:
            return FAIL, f"{len(answering)} answer, but /openapi.json does not document GET " + ", ".join(undocumented)
        return PASS, f"all {len(answering)} application reads that answer are documented"

    # =====================================================================
    # access
    # =====================================================================

    def group_access(self) -> None:
        group = "access"
        self.run_check(group, "reads mode", self._reads_mode)
        protected = (
            ("POST /policy/config", "policy/config", False),
            ("POST /rules", "rules", False),
            ("POST /sessions/<id>/resume", f"sessions/{self.session('access')}/resume", False),
            ("POST /api/projects/Acme-X/promote", "api/projects/Acme-X/promote", True),
        )
        for label, path, contract in protected:
            self.run_check(group, f"{label} needs the key", self._protected_write, path, contract)
            self.run_check(group, f"{label} refuses a wrong key", self._protected_wrong_key, path, contract)
        mode = self.expected_mode()
        for path in LEGACY_READS + CONTRACT_READS:
            contract = path in CONTRACT_READS
            if mode == "private":
                self.run_check(group, f"GET /{path} private", self._private_read, path, contract)
            else:
                self.run_check(group, f"GET /{path} public, names reduced", self._public_read, path, contract)
        self.run_check(group, "sandbox writes", self._sandbox_exception, mode)
        if mode == "private":
            self.run_check(group, "POST /api/sandbox closed", self._sandbox_closed)

    def _reads_mode(self) -> Tuple[str, str]:
        detected = self.reads_mode()
        if detected == "unknown":
            got = self.request("GET", "api/insights")
            return FAIL, f"GET /api/insights anonymously answers {self.describe(got)}, neither open nor closed"
        if self.expect and self.expect != detected:
            return FAIL, f"expected {self.expect} reads, but an anonymous GET /api/insights shows a {detected} stack"
        note = f"as expected ({self.expect})" if self.expect else "no --expect given, so the rest of the group checks what was detected"
        return PASS, f"anonymous GET /api/insights shows a {detected} stack, {note}"

    def _protected_write(self, path: str, contract: bool) -> Tuple[str, str]:
        got = self.request("POST", path, raw=MALFORMED)
        if contract:
            absent = self.not_there(got)
            if absent:
                return absent
        if got.status in (401, 403):
            return PASS, f"{got.status} {got.obj().get('title') or ''} without a key".strip()
        if got.status == 400:
            return FAIL, "the write reached its route without a key (400 on a malformed body)"
        return FAIL, f"without a key it answers {self.describe(got)}: {self.snippet(got, 80)}"

    def _protected_wrong_key(self, path: str, contract: bool) -> Tuple[str, str]:
        wrong = f"probe-wrong-key-{self.runid}"
        got = self.request("POST", path, raw=MALFORMED, key_override=wrong)
        if contract:
            absent = self.not_there(got)
            if absent:
                return absent
        if got.status in (401, 403):
            return PASS, f"{got.status} {got.obj().get('title') or ''} with a wrong key".strip()
        return FAIL, f"with a wrong key it answers {self.describe(got)}"

    def _private_read(self, path: str, contract: bool) -> Tuple[str, str]:
        anonymous = self.request("GET", path)
        if contract:
            absent = self.not_there(anonymous)
            if absent:
                return absent
        if anonymous.status not in (401, 403):
            return FAIL, f"a private read answers {self.describe(anonymous)} without a key"
        refused = f"{anonymous.status} without a key"
        if anonymous.status == 403:
            refused += " (403: the stack has no operator key configured, so nothing can open it)"
        if not self.has_key:
            return PASS, f"{refused}; no --key-file, so the keyed read was not tried"
        keyed = self.request("GET", path, auth=True)
        if keyed.status != 200:
            return FAIL, f"{refused}, but {self.describe(keyed)} with the key"
        return PASS, f"{refused}, 200 with the key"

    def _public_read(self, path: str, contract: bool) -> Tuple[str, str]:
        got = self.request("GET", path)
        if contract:
            absent = self.not_there(got)
            if absent:
                return absent
        if got.status != 200:
            return FAIL, f"a public read answers {self.describe(got)}"
        data = got.json()
        if data is None:
            return FAIL, f"200 but not JSON: {got.content_type}"
        leaks = scan_public_names(data, self.project_pattern)
        if leaks:
            return FAIL, f"{len(leaks)} value(s) a public page must not show, first at " + "; ".join(leaks[:3])
        return PASS, "200; every project is Acme-* or unlabelled and every developer an 8-hex hash"

    def sandbox_names(self) -> Tuple[str, List[Tuple[str, str]]]:
        """The sandbox name this run may write, and near misses a correct stack refuses.

        Each near miss is built to fall outside the pattern whatever the run id
        holds. Upper-casing the id is not enough on its own: an id of digits
        alone has no letter to change, and the "miss" would be the real name.
        """
        good = f"Acme-Sandbox-{self.runid}"
        candidates = [
            ("upper-case hex", f"Acme-Sandbox-{self.runid[:6].upper()}AF"),
            ("seven hex digits", f"Acme-Sandbox-{self.runid[:7]}"),
            ("nine hex digits", f"Acme-Sandbox-{self.runid}0"),
            ("misspelt prefix", f"Acme-Sandboxx-{self.runid}"),
        ]
        misses = [(label, name) for label, name in candidates if name != good and not SANDBOX_PATTERN.fullmatch(name)]
        return good, misses

    def _sandbox_exception(self, mode: str) -> Tuple[str, str]:
        good, near_misses = self.sandbox_names()
        got = self.request("POST", f"api/projects/{good}/reviews", raw=MALFORMED)
        absent = self.not_there(got)
        if absent:
            return absent
        if mode == "private":
            if got.status not in (401, 403):
                return FAIL, f"a private stack lets an anonymous caller write {good} ({got.status})"
            return PASS, f"a private stack refuses an anonymous write to {good} with {got.status}"
        if got.status in (401, 403):
            return FAIL, f"a public stack refuses an anonymous write to {good} ({got.status}), which the contract opens"
        opened = []
        for label, name in near_misses:
            miss = self.request("POST", f"api/projects/{name}/reviews", raw=MALFORMED)
            if miss.status not in (401, 403):
                opened.append(f"{label} {name}={miss.status}")
        if opened:
            return FAIL, "names outside ^Acme-Sandbox-[0-9a-f]{8}$ are writable too: " + ", ".join(opened)
        return PASS, (
            f"{good} is open to anyone ({got.status} on a malformed body, not 401/403); "
            f"{len(near_misses)} near misses are refused"
        )

    def _sandbox_closed(self) -> Tuple[str, str]:
        got = self.request("POST", "api/sandbox", raw=MALFORMED)
        absent = self.not_there(got)
        if absent:
            return absent
        if got.status in (401, 403):
            return PASS, f"{got.status} without a key on a private stack"
        return FAIL, f"a private stack answers {self.describe(got)} to an anonymous sandbox request"

    # =====================================================================
    # governance
    # =====================================================================

    def group_governance(self) -> None:
        group = "governance"
        checks: List[Tuple[str, Callable[[], Tuple[str, str]]]] = [
            ("domain import via Write refused", self._boundary_write),
            ("domain import via heredoc refused", lambda: self._boundary_command("heredoc", HEREDOC_WRITE)),
            ("domain import via redirect refused", lambda: self._boundary_command("redirect", REDIRECT_WRITE)),
            ("hook settings rewrite refused", self._protected_settings),
            ("git commit --no-verify refused", self._protected_no_verify),
            ("credential refused and not echoed", self._credential),
            ("page loop halts on the third call", self._page_loop),
            ("hook loop refuses without halting", self._hook_loop),
            ("dry run observed, not refused", self._dry_run),
            ("approval never reaches the model", self._approval_deterministic),
            ("refusal explained by the model", self._refusal_explained),
            ("unknown project recorded as unlabelled", self._unlabelled),
        ]
        for name, fn in checks:
            self.run_check(group, name, self.writes_skipped if self.read_only else fn)

    def _expect_status(self, resp: Response, data: Dict[str, Any], want: str, what: str) -> Tuple[str, str]:
        if resp.status != 200:
            return FAIL, f"{what}: {self.describe(resp)}: {self.snippet(resp, 80)}"
        if data.get("status") != want:
            return FAIL, f"{what} answered {data.get('status')!r}, not {want}: {self.clip(data.get('reason', ''), 90)}"
        return PASS, f"{what} answered {want}"

    def _boundary_write(self) -> Tuple[str, str]:
        resp, data = self.evaluate("boundary-write", "Write", "FILE_WRITE", DOMAIN_WRITE)
        return self._expect_status(resp, data, "BLOCKED_BOUNDARY_VIOLATION", "a Write of `import boto3` into src/domain/")

    def _boundary_command(self, suffix: str, command: str) -> Tuple[str, str]:
        resp, data = self.evaluate(f"boundary-{suffix}", "Bash", "COMMAND_EXEC", {"command": command})
        return self._expect_status(resp, data, "BLOCKED_BOUNDARY_VIOLATION", f"a Bash {suffix} of `import boto3` into src/domain/")

    def _protected_settings(self) -> Tuple[str, str]:
        resp, data = self.evaluate("protected-settings", "Write", "FILE_WRITE", SETTINGS_WRITE)
        return self._expect_status(resp, data, "BLOCKED_BOUNDARY_VIOLATION", "a rewrite of .claude/settings.json")

    def _protected_no_verify(self) -> Tuple[str, str]:
        resp, data = self.evaluate("protected-no-verify", "Bash", "COMMAND_EXEC", {"command": NO_VERIFY_COMMIT})
        return self._expect_status(resp, data, "BLOCKED_BOUNDARY_VIOLATION", "git commit --no-verify")

    def _credential(self) -> Tuple[str, str]:
        credential = synthetic_credential()
        resp, data = self.evaluate("credential", "Bash", "COMMAND_EXEC", {"command": f"echo {credential}"})
        status, evidence = self._expect_status(resp, data, "BLOCKED_SECRET_DETECTED", "a command carrying an access key id")
        if status != PASS:
            return status, evidence
        if credential.encode() in resp.body:
            return FAIL, "refused, but the response echoes the credential back"
        return PASS, f"{evidence}; the response does not echo it"

    def _page_loop(self) -> Tuple[str, str]:
        session = self.session("loop-page")
        seen = []
        for _ in range(3):
            resp, data = self.evaluate("", "Bash", "COMMAND_EXEC", {"command": LOOPING_COMMAND}, session=session)
            if resp.status != 200:
                return FAIL, f"call {len(seen) + 1}: {self.describe(resp)}"
            seen.append(data)
        statuses = _status_list(seen)
        third = seen[2]
        if [d.get("status") for d in seen[:2]] != ["APPROVED", "APPROVED"]:
            return FAIL, f"the first two identical calls were not both approved: {statuses}"
        if third.get("status") != "BLOCKED_LOOP_DETECTED" or third.get("session_tripped") is not True:
            return FAIL, f"the third identical call was {third.get('status')} with session_tripped={third.get('session_tripped')}"
        resp, after = self.evaluate("", "Bash", "COMMAND_EXEC", {"command": DIFFERENT_COMMAND}, session=session)
        if after.get("status") != "BLOCKED_CIRCUIT_BREAKER":
            return FAIL, f"{statuses} (halted), but the next different call was {after.get('status')}, so the halt did not hold"
        durable = ""
        stored = self.request("GET", f"sessions/{urllib.parse.quote(session, safe='')}", auth=True)
        if stored.status == 200:
            if stored.obj().get("is_tripped") is not True:
                return FAIL, f"{statuses} (halted), but GET /sessions/<id> says is_tripped={stored.obj().get('is_tripped')}"
            durable = "; GET /sessions/<id> is_tripped=true"
        elif stored.status in (401, 403):
            durable = "; the stored session needs the key to read"
        else:
            return FAIL, f"{statuses} (halted), but GET /sessions/<id> answers {self.describe(stored)}"
        return PASS, f"{statuses} (halted); the next different call BLOCKED_CIRCUIT_BREAKER{durable}"

    def _hook_loop(self) -> Tuple[str, str]:
        session = self.session("loop-hook")
        seen = []
        for _ in range(3):
            resp, data = self.evaluate(
                "", "Bash", "COMMAND_EXEC", {"command": LOOPING_COMMAND}, origin="hook", session=session
            )
            if resp.status != 200:
                return FAIL, f"call {len(seen) + 1}: {self.describe(resp)}"
            seen.append(data)
        statuses = _status_list(seen)
        if any(d.get("session_tripped") for d in seen):
            return FAIL, f"a hook session was halted by a loop: {statuses}"
        stage = seen[0].get("project_stage")
        if stage == "observe":
            if any(str(d.get("status", "")).startswith("BLOCKED") for d in seen):
                return FAIL, f"{PROBE_PROJECT} is in observe, yet a hook call was refused: {statuses}"
            return SKIP, (
                f"{PROBE_PROJECT} is in observe here: {statuses}, recorded and never halted; "
                "the refusal needs an enforcing project, which the application group checks after promotion"
            )
        if seen[2].get("status") != "BLOCKED_LOOP_DETECTED":
            return FAIL, f"the third identical hook call was {seen[2].get('status')}: {statuses}"
        resp, after = self.evaluate(
            "", "Bash", "COMMAND_EXEC", {"command": DIFFERENT_COMMAND}, origin="hook", session=session
        )
        if after.get("status") != "APPROVED":
            return FAIL, f"{statuses}, but the next different call was {after.get('status')}"
        return PASS, f"{statuses}, session not halted; the next different call APPROVED"

    def _dry_run(self) -> Tuple[str, str]:
        resp, data = self.evaluate("dry-run", "Write", "FILE_WRITE", DOMAIN_WRITE, origin="hook", dry_run=True)
        if resp.status != 200:
            return FAIL, self.describe(resp)
        if data.get("status") != "APPROVED":
            return FAIL, f"a dry run was answered {data.get('status')}"
        if not data.get("observations") and not data.get("observed_rules"):
            return FAIL, "approved, but nothing records what would have been refused"
        if data.get("session_tripped"):
            return FAIL, "a dry run halted its session"
        rules = ",".join(data.get("observed_rules") or []) or "observations"
        return PASS, f"APPROVED with dry_run={data.get('dry_run')}, observed {rules}, not halted"

    def _approval_deterministic(self) -> Tuple[str, str]:
        resp, data = self.evaluate("approval", "Read", "FILE_READ", {"file_path": "README.md"}, explain=True)
        if resp.status != 200 or data.get("status") != "APPROVED":
            return FAIL, f"a plain read was not approved: {self.describe(resp)} {data.get('status')}"
        source = data.get("explanation_source")
        if source != "deterministic":
            return FAIL, f"an approval asked to explain came back explanation_source={source!r}"
        return PASS, "APPROVED with explain=true, explanation_source=deterministic: no model call"

    def _refusal_explained(self) -> Tuple[str, str]:
        # One-directional on purpose: offline, throttled or capped, the service
        # says deterministic_fallback, which is honest, so it is not a failure
        # of the claim this probes. Only the model's own answer passes.
        resp, data = self.evaluate("explained", "Write", "FILE_WRITE", DOMAIN_WRITE, explain=True)
        if resp.status != 200 or not str(data.get("status", "")).startswith("BLOCKED"):
            return FAIL, f"the refusal to explain was not refused: {self.describe(resp)} {data.get('status')}"
        source = data.get("explanation_source")
        if source == "bedrock":
            return PASS, "a page refusal carried explanation_source=bedrock"
        return SKIP, f"the model was not reached here (explanation_source={source!r}); nothing to attribute"

    def _unlabelled(self) -> Tuple[str, str]:
        resp, data = self.evaluate(
            "unlabelled", "Read", "FILE_READ", {"file_path": "README.md"}, project=f"probe-{self.runid}-not-acme"
        )
        if resp.status != 200:
            return FAIL, self.describe(resp)
        warnings = " ".join(str(w) for w in data.get("warnings") or [])
        if "unlabelled" not in warnings:
            return FAIL, "a project outside the pattern was accepted with no 'unlabelled' warning"
        return PASS, "a project outside the pattern is answered with the 'unlabelled' warning"

    # =====================================================================
    # application
    # =====================================================================

    def group_application(self) -> None:
        group = "application"
        overview = self.request("GET", "api/overview", auth=True, query={"days": 7})
        self.run_check(group, "overview shape", self._overview_shape, overview)
        self.run_check(group, "overview totals equal its series", self._overview_totals, overview)
        pages = self._decision_pages()
        self.run_check(group, "decisions pagination", self._pagination, pages)
        self.run_check(group, "decision rows carry the contract fields", self._row_fields, pages)
        self.run_check(group, "decisions kind=refused filter", self._kind_filter)
        self.run_check(group, "decisions session filter", self._session_filter)
        self.run_check(group, "decision drill-down", self._decision_detail, pages)
        self.run_check(group, "projects list shape", self._projects_list)
        self.run_check(group, "project readiness", self._project_readiness)
        if self.read_only:
            self.record(group, "sandbox walkthrough", *self.writes_skipped())
            return
        self._sandbox_walkthrough(group)

    def _overview_shape(self, got: Response) -> Tuple[str, str]:
        absent = self.not_there(got)
        if absent:
            return absent
        if got.status != 200:
            return FAIL, f"GET /api/overview {self.describe(got)}"
        problems = overview_problems(got.obj())
        if problems:
            return FAIL, "; ".join(problems[:4])
        data = got.obj()
        return PASS, (
            f"window {data.get('window_days')} days from rollups, {len(data.get('series') or [])} days in the series, "
            f"{len(data.get('by_project') or [])} projects"
        )

    def _overview_totals(self, got: Response) -> Tuple[str, str]:
        absent = self.not_there(got)
        if absent:
            return absent
        data = got.obj()
        totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
        series = [d for d in data.get("series") or [] if isinstance(d, dict)]
        if got.status != 200 or not totals:
            return FAIL, f"GET /api/overview {self.describe(got)} with no totals"
        approved = sum(_num(d.get("approved")) for d in series)
        observed = sum(_num(d.get("observed")) for d in series)
        refused = sum(_num(d.get("refused")) for d in series)
        problems = []
        if _num(totals.get("approved")) != approved:
            problems.append(f"approved {totals.get('approved')} but the series sums to {approved:g}")
        if _num(totals.get("refused")) != refused:
            problems.append(f"refused {totals.get('refused')} but the series sums to {refused:g}")
        calls = _num(totals.get("calls"))
        if calls == approved + observed + refused:
            identity = "calls = approved + observed + refused"
        elif calls == approved + refused:
            identity = "calls = approved + refused (observed counted within approved)"
        else:
            identity = ""
            problems.append(
                f"calls {totals.get('calls')} is neither {approved + observed + refused:g} nor {approved + refused:g}"
            )
        if problems:
            return FAIL, "; ".join(problems)
        return PASS, (
            f"{identity}: {calls:g} = {approved:g} + {observed:g} + {refused:g}; "
            f"would_refuse {totals.get('would_refuse')} against {observed:g} observed in the series"
        )

    def _decision_query(self, **extra: Any) -> Dict[str, Any]:
        query: Dict[str, Any] = {"days": 7}
        query.update(extra)
        return query

    def _decision_pages(self) -> List[Response]:
        """Up to four pages of three rows, following the cursor, plus the second page asked again."""
        own = any(v["project"] == PROBE_PROJECT for v in self.verdicts)
        query = self._decision_query(limit=3, project=PROBE_PROJECT if own else None)
        pages: List[Response] = []
        cursor = None
        for _ in range(4):
            got = self.request("GET", "api/decisions", auth=True, query=dict(query, cursor=cursor))
            pages.append(got)
            cursor = got.obj().get("next_cursor") if got.status == 200 else None
            if not cursor:
                break
        return pages

    def _pagination(self, pages: List[Response]) -> Tuple[str, str]:
        first = pages[0]
        absent = self.not_there(first)
        if absent:
            return absent
        if any(p.status != 200 for p in pages):
            bad = next(p for p in pages if p.status != 200)
            return FAIL, f"a page answered {self.describe(bad)}"
        seen: Dict[Tuple[Any, Any], int] = {}
        duplicates = 0
        for page in pages:
            items = page.obj().get("items")
            if not isinstance(items, list):
                return FAIL, "a page has no items list"
            if len(items) > 3:
                return FAIL, f"limit=3 returned {len(items)} items"
            for item in items:
                marker = (item.get("timestamp"), item.get("verdict_id"))
                duplicates += marker in seen
                seen[marker] = seen.get(marker, 0) + 1
        if duplicates:
            return FAIL, f"{duplicates} row(s) came back on more than one page"
        if len(pages) == 1:
            count = len(pages[0].obj().get("items") or [])
            return SKIP, f"only {count} row(s) in the window, fewer than two pages: the cursor was not exercised"
        # The cursor round trip: the same cursor asked again must give the same page.
        cursor = pages[0].obj().get("next_cursor")
        own = any(v["project"] == PROBE_PROJECT for v in self.verdicts)
        again = self.request(
            "GET", "api/decisions", auth=True,
            query=self._decision_query(limit=3, project=PROBE_PROJECT if own else None, cursor=cursor),
        )
        ids = [i.get("verdict_id") for i in pages[1].obj().get("items") or []]
        ids_again = [i.get("verdict_id") for i in again.obj().get("items") or []]
        if ids != ids_again:
            return FAIL, "the same cursor asked twice returned different rows"
        return PASS, f"{len(pages)} pages, {len(seen)} rows, no duplicates; the second page is stable under its cursor"

    def _row_fields(self, pages: List[Response]) -> Tuple[str, str]:
        absent = self.not_there(pages[0])
        if absent:
            return absent
        items = [i for p in pages for i in (p.obj().get("items") or []) if isinstance(i, dict)]
        if not items:
            return SKIP, "no rows in the window to inspect"
        lacking = sorted({f for i in items for f in ROW_CONTRACT_FIELDS + ("timestamp", "verdict_id") if f not in i})
        if lacking:
            return FAIL, "rows lack " + ", ".join(lacking)
        bad_review = [i.get("review") for i in items if i.get("review") not in (None, "correct", "false_alarm")]
        if bad_review:
            return FAIL, f"review holds {bad_review[0]!r}, outside correct|false_alarm|null"
        return PASS, f"{len(items)} rows carry rule_key, stage, hook_mode and review"

    def _kind_filter(self) -> Tuple[str, str]:
        own = any(v["project"] == PROBE_PROJECT for v in self.verdicts)
        got = self.request(
            "GET", "api/decisions", auth=True,
            query=self._decision_query(kind="refused", limit=50, project=PROBE_PROJECT if own else None),
        )
        absent = self.not_there(got)
        if absent:
            return absent
        items = got.obj().get("items")
        if got.status != 200 or not isinstance(items, list):
            return FAIL, f"GET /api/decisions?kind=refused {self.describe(got)}"
        if not items:
            return SKIP, "no refusals in the window to filter"
        wrong = [i.get("status") for i in items if not str(i.get("status", "")).startswith("BLOCKED")]
        if wrong:
            return FAIL, f"{len(wrong)} of {len(items)} rows are not refusals (first: {wrong[0]!r})"
        return PASS, f"{len(items)} rows, every one a refusal"

    def _session_filter(self) -> Tuple[str, str]:
        mine = next((v for v in self.verdicts if v["project"] == PROBE_PROJECT), None)
        if mine is None:
            got = self.request("GET", "api/decisions", auth=True, query=self._decision_query(limit=1))
            absent = self.not_there(got)
            return absent or (SKIP, "the governance group did not run, so no session of this run exists to filter by")
        got = self.request(
            "GET", "api/decisions", auth=True, query=self._decision_query(session=mine["session_id"], limit=50)
        )
        absent = self.not_there(got)
        if absent:
            return absent
        items = got.obj().get("items")
        if got.status != 200 or not isinstance(items, list):
            return FAIL, f"GET /api/decisions?session= {self.describe(got)}"
        if not any(i.get("verdict_id") == mine["verdict_id"] for i in items):
            return FAIL, "the call this run just recorded is not listed under its own session"
        strays = [i for i in items if i.get("session_id") != mine["session_id"]]
        if strays:
            return FAIL, f"{len(strays)} row(s) of other sessions came back"
        return PASS, f"{len(items)} row(s), every one of the session asked for, including the call this run recorded"

    def _decision_detail(self, pages: List[Response]) -> Tuple[str, str]:
        absent = self.not_there(pages[0])
        if absent:
            return absent
        items = pages[0].obj().get("items") or []
        if not items:
            return SKIP, "no row to drill into"
        row = items[0]
        got = self.request(
            "GET", "api/decision", auth=True,
            query={"timestamp": row.get("timestamp"), "verdict_id": row.get("verdict_id")},
        )
        absent = self.not_there(got)
        if absent:
            return absent
        data = got.obj()
        if got.status != 200:
            return FAIL, f"GET /api/decision for a listed row answers {self.describe(got)}"
        missing = [k for k in ("decision", "session", "rule") if k not in data]
        if missing:
            return FAIL, "the drill-down lacks " + ", ".join(missing)
        if (data.get("decision") or {}).get("verdict_id") != row.get("verdict_id"):
            return FAIL, "the drill-down returned a different decision"
        bogus = self.request(
            "GET", "api/decision", auth=True,
            query={"timestamp": row.get("timestamp"), "verdict_id": f"probe-{self.runid}-none"},
        )
        if bogus.status != 404 or route_missing(bogus):
            return FAIL, f"an unknown verdict answers {self.describe(bogus)}, not 404"
        return PASS, "a listed row opens with decision, session and rule; an unknown verdict answers 404"

    def _projects_list(self) -> Tuple[str, str]:
        got = self.request("GET", "api/projects", auth=True)
        absent = self.not_there(got)
        if absent:
            return absent
        projects = got.obj().get("projects")
        if got.status != 200 or not isinstance(projects, list):
            return FAIL, f"GET /api/projects {self.describe(got)} with no projects list"
        problems = []
        for row in projects:
            lacking = [k for k in PROJECT_KEYS if not isinstance(row, dict) or k not in row]
            if lacking:
                problems.append(f"{self.shown_name(row.get('project') if isinstance(row, dict) else None)} lacks " + ",".join(lacking))
            elif row.get("stage") not in ("observe", "enforce"):
                problems.append(f"stage={row.get('stage')!r}")
        if problems:
            return FAIL, "; ".join(problems[:3])
        return PASS, f"{len(projects)} projects, each with stage, readiness counts, agents and hook modes"

    def _project_readiness(self) -> Tuple[str, str]:
        name = PROBE_PROJECT
        if not any(v["project"] == PROBE_PROJECT for v in self.verdicts):
            listing = self.request("GET", "api/projects", auth=True)
            absent = self.not_there(listing)
            if absent:
                return absent
            projects = [p for p in listing.obj().get("projects") or [] if isinstance(p, dict)]
            if not projects:
                return SKIP, "no project in the window to read readiness for"
            name = str(projects[0].get("project"))
        got = self.request("GET", f"api/projects/{urllib.parse.quote(name, safe='')}", auth=True, query={"days": 14})
        absent = self.not_there(got)
        if absent:
            return absent
        if got.status != 200:
            return FAIL, f"GET /api/projects/<name> {self.describe(got)}"
        problems = readiness_problems(got.obj())
        if problems:
            return FAIL, "; ".join(problems[:4])
        summary = got.obj()["readiness"]["summary"]
        shown = name if name == PROBE_PROJECT else "the first listed project"
        return PASS, (
            f"{shown}: stage {summary.get('stage')}, {len(got.obj()['readiness']['rules'])} rules, "
            "every state follows from its counts"
        )

    # -- the sandbox walkthrough -----------------------------------------

    def _project(self, name: str) -> Response:
        return self.request("GET", f"api/projects/{name}", auth=True, query={"days": 14})

    def _sandbox_walkthrough(self, group: str) -> None:
        """Creates a sandbox and takes it from observe to enforce and back, as a visitor would."""
        state: Dict[str, Any] = {}
        steps: List[Tuple[str, Callable[[Dict[str, Any]], Tuple[str, str]]]] = [
            ("sandbox created", self._sbx_create),
            ("sandbox starts in observe with would-refuse rules", self._sbx_observe),
            ("hook call observed before promotion", self._sbx_before),
            ("reviews label its own rows and skip another project's", self._sbx_reviews),
            ("review labels read back", self._sbx_labels),
            ("reviewed rule becomes ready", self._sbx_ready),
            ("promote to enforce", self._sbx_promote),
            ("hook call refused after promotion", self._sbx_after),
            ("rule left out keeps observing", self._sbx_partial),
            ("hook loop refused without halting under enforce", self._sbx_loop),
            ("demote to observe", self._sbx_demote),
            ("hook call observed after demotion", self._sbx_after_demote),
        ]
        for name, fn in steps:
            if state.get("stop"):
                self.record(group, name, SKIP, state["stop"])
                continue
            status = self.run_check(group, name, fn, state)
            if name in ("sandbox created", "promote to enforce") and status != PASS:
                state.setdefault("stop", f"depends on '{name}', which did not pass")

    def _sbx_create(self, state: Dict[str, Any]) -> Tuple[str, str]:
        got = self.request("POST", "api/sandbox", body={}, auth=True)
        absent = self.not_there(got)
        if absent:
            state["stop"] = "depends on POST /api/sandbox, which is not deployed yet"
            return absent
        if got.status in (401, 403) and self.expected_mode() == "private":
            state["stop"] = "the sandbox is open only on a public stack"
            return SKIP, f"{got.status}: a private stack keeps POST /api/sandbox closed, so the walkthrough needs the public one"
        data = got.obj()
        name = str(data.get("project") or "")
        if got.status in (200, 201):
            # Noted as soon as the stack says it made one, even one this check
            # then fails, so the evidence never omits a write that happened.
            shown = name if SANDBOX_PATTERN.fullmatch(name) else (masked(name) if name else "with no name given")
            self.wrote(f"sandbox `{shown}` with its seeded calls, reviews, promotion and demotion, which expires within 24 hours")
        if got.status not in (200, 201) or not SANDBOX_PATTERN.fullmatch(name):
            return FAIL, f"POST /api/sandbox {self.describe(got)}, project {masked(name) if name else 'missing'}"
        state["name"] = name
        self._sandbox = name
        problems = []
        if _num(data.get("calls_seeded")) < 1:
            problems.append(f"calls_seeded={data.get('calls_seeded')}")
        if data.get("url") != f"dashboard.html#/projects/{name}":
            problems.append(f"url={self.clip(data.get('url'), 60)!r}")
        if problems:
            return FAIL, f"{name} created, but " + ", ".join(problems)
        return PASS, f"{name}, {data.get('calls_seeded')} calls seeded, url dashboard.html#/projects/<name>"

    def _sbx_observe(self, state: Dict[str, Any]) -> Tuple[str, str]:
        got = self._project(state["name"])
        if got.status != 200:
            return FAIL, f"GET /api/projects/<sandbox> {self.describe(got)}"
        data = got.obj()
        config = data.get("config") if isinstance(data.get("config"), dict) else {}
        rules = [r for r in (data.get("readiness") or {}).get("rules") or [] if isinstance(r, dict)]
        state["rule_keys"] = [str(r.get("rule_key")) for r in rules]
        flagged = [str(r.get("rule_key")) for r in rules if _num(r.get("would_refuse")) > 0]
        problems = readiness_problems(data)
        if config.get("stage") != "observe":
            problems.insert(0, f"stage={config.get('stage')!r}")
        if config.get("sandbox") is not True:
            problems.append("config.sandbox is not true")
        if len(flagged) < 2:
            problems.append(f"only {len(flagged)} rule(s) would refuse, the contract seeds at least two")
        if problems:
            return FAIL, "; ".join(problems[:4])
        return PASS, f"stage observe, sandbox true, would-refuse under {len(flagged)} rules: {', '.join(flagged[:4])}"

    def _sbx_hook_write(self, state: Dict[str, Any], suffix: str, arguments: Dict[str, Any]) -> Tuple[Response, Dict[str, Any]]:
        return self.evaluate(
            "", "Write", "FILE_WRITE", arguments, origin="hook", project=state["name"],
            session=self.session(f"sbx-{suffix}"),
        )

    def _observed(self, resp: Response, data: Dict[str, Any], stage: str, what: str) -> Tuple[str, str]:
        if resp.status != 200:
            return FAIL, f"{what}: {self.describe(resp)}"
        if data.get("status") != "APPROVED":
            return FAIL, f"{what} was {data.get('status')} in {stage}; it should have been recorded, not refused"
        if data.get("project_stage") != stage:
            return FAIL, f"{what} came back with project_stage={data.get('project_stage')!r}, not {stage}"
        if not data.get("observations") and not data.get("observed_rules"):
            return FAIL, f"{what} was approved with nothing observed"
        return PASS, f"{what}: APPROVED with the would-refuse recorded, project_stage {stage}"

    def _sbx_before(self, state: Dict[str, Any]) -> Tuple[str, str]:
        resp, data = self._sbx_hook_write(state, "before", DOMAIN_WRITE)
        state["before"] = data
        return self._observed(resp, data, "observe", "a hook Write of `import boto3` into src/domain/")

    def _boundary_rule_key(self, state: Dict[str, Any]) -> str:
        """The rule key the ledger gave this run's own boundary call, read back rather than assumed."""
        before = state.get("before") or {}
        got = self.request(
            "GET", "api/decisions", auth=True,
            query=self._decision_query(project=state["name"], session=self.session("sbx-before"), limit=10),
        )
        for item in got.obj().get("items") or []:
            if item.get("verdict_id") == before.get("verdict_id") and item.get("rule_key") not in (None, "", "NONE"):
                return str(item["rule_key"])
        return FALLBACK_BOUNDARY_RULE

    def _sbx_reviews(self, state: Dict[str, Any]) -> Tuple[str, str]:
        key = self._boundary_rule_key(state)
        state["boundary_key"] = key
        listing = self.request(
            "GET", "api/decisions", auth=True,
            query=self._decision_query(project=state["name"], rule=key, kind="observed", review="unreviewed", limit=100),
        )
        own = [i for i in listing.obj().get("items") or [] if isinstance(i, dict)]
        if listing.status != 200 or not own:
            return FAIL, f"no unreviewed would-refuse rows under {key} to label ({self.describe(listing)})"
        items = [{"timestamp": i.get("timestamp"), "verdict_id": i.get("verdict_id"), "label": "correct"} for i in own]
        foreign = next((v for v in self.verdicts if v["project"] == PROBE_PROJECT and v.get("timestamp")), None)
        if foreign:
            items.append({"timestamp": foreign["timestamp"], "verdict_id": foreign["verdict_id"], "label": "correct"})
        got = self.request("POST", f"api/projects/{state['name']}/reviews", body={"items": items}, auth=True)
        data = got.obj()
        if got.status != 200:
            return FAIL, f"POST reviews {self.describe(got)}: {self.snippet(got, 80)}"
        state["labelled"] = [i["verdict_id"] for i in items[: len(own)]]
        skipped = [s.get("verdict_id") for s in data.get("skipped") or [] if isinstance(s, dict)]
        if _num(data.get("updated")) != len(own):
            return FAIL, f"sent {len(own)} of its own rows, updated={data.get('updated')}"
        if foreign and foreign["verdict_id"] not in skipped:
            return FAIL, f"a row of {PROBE_PROJECT} was not skipped when labelled under the sandbox"
        other = f"; a row of {PROBE_PROJECT} was skipped" if foreign else "; no other project's row was at hand to test the skip"
        return PASS, f"{len(own)} rows under {key} labelled correct{other}"

    def _sbx_labels(self, state: Dict[str, Any]) -> Tuple[str, str]:
        got = self.request(
            "GET", "api/decisions", auth=True,
            query=self._decision_query(project=state["name"], rule=state["boundary_key"], review="correct", limit=100),
        )
        items = {i.get("verdict_id"): i for i in got.obj().get("items") or [] if isinstance(i, dict)}
        missing = [v for v in state.get("labelled") or [] if v not in items]
        if got.status != 200 or missing:
            return FAIL, f"{len(missing)} labelled row(s) not listed under review=correct ({self.describe(got)})"
        wrong = [i for i in items.values() if i.get("review") != "correct"]
        if wrong:
            return FAIL, f"review=correct listed {len(wrong)} row(s) with another label"
        bad_by = [i.get("reviewed_by") for i in items.values() if i.get("reviewed_by") and not DEVELOPER_HASH.fullmatch(str(i.get("reviewed_by")))]
        if bad_by:
            return FAIL, "reviewed_by is not an 8-hex hash of the credential"
        return PASS, f"{len(items)} rows read back with review=correct and an 8-hex reviewed_by"

    def _sbx_ready(self, state: Dict[str, Any]) -> Tuple[str, str]:
        got = self._project(state["name"])
        rules = {str(r.get("rule_key")): r for r in (got.obj().get("readiness") or {}).get("rules") or [] if isinstance(r, dict)}
        rule = rules.get(state["boundary_key"])
        if got.status != 200 or rule is None:
            return FAIL, f"{state['boundary_key']} is not among the sandbox's rules ({self.describe(got)})"
        problems = readiness_problems(got.obj())
        if rule.get("state") != "ready":
            problems.insert(0, f"{state['boundary_key']} is {rule.get('state')!r} after every row was marked correct")
        if problems:
            return FAIL, "; ".join(problems[:3])
        return PASS, f"{state['boundary_key']} is ready: {rule.get('correct')} correct, 0 false alarms, 0 unreviewed"

    def _sbx_promote(self, state: Dict[str, Any]) -> Tuple[str, str]:
        enforce = [k for k in (state["boundary_key"], "LOOP") if k in state.get("rule_keys", [])]
        state["enforce"] = enforce
        got = self.request("POST", f"api/projects/{state['name']}/promote", body={"enforce": enforce}, auth=True)
        if got.status != 200:
            return FAIL, f"POST promote {self.describe(got)}: {self.snippet(got, 80)}"
        after = self._project(state["name"])
        config = after.obj().get("config") if isinstance(after.obj().get("config"), dict) else {}
        want_observe = sorted(set(state.get("rule_keys", [])) - set(enforce))
        problems = []
        if config.get("stage") != "enforce":
            problems.append(f"stage={config.get('stage')!r}")
        if sorted(config.get("observe_rules") or []) != want_observe:
            problems.append(f"observe_rules={sorted(config.get('observe_rules') or [])} not {want_observe}")
        if not any(isinstance(h, dict) and h.get("action") == "promote" for h in config.get("history") or []):
            problems.append("no promote entry in history")
        if problems:
            return FAIL, "promoted, but " + "; ".join(problems)
        return PASS, f"stage enforce for {', '.join(enforce)}; {len(want_observe)} rules keep observing; history records it"

    def _sbx_after(self, state: Dict[str, Any]) -> Tuple[str, str]:
        resp, data = self._sbx_hook_write(state, "after", DOMAIN_WRITE)
        if resp.status != 200:
            return FAIL, self.describe(resp)
        if data.get("status") != "BLOCKED_BOUNDARY_VIOLATION" or data.get("project_stage") != "enforce":
            return FAIL, f"after promotion the same hook call was {data.get('status')} with project_stage={data.get('project_stage')!r}"
        return PASS, "the same hook call is BLOCKED_BOUNDARY_VIOLATION with project_stage enforce"

    def _sbx_partial(self, state: Dict[str, Any]) -> Tuple[str, str]:
        if "PROTECTED_PATH" not in state.get("rule_keys", []) or "PROTECTED_PATH" in state.get("enforce", []):
            return SKIP, "PROTECTED_PATH is not a rule left in observe here"
        resp, data = self._sbx_hook_write(state, "partial", SETTINGS_WRITE)
        if resp.status != 200:
            return FAIL, self.describe(resp)
        if data.get("status") != "APPROVED" or not (data.get("observations") or data.get("observed_rules")):
            return FAIL, f"a rewrite of .claude/settings.json under an observing PROTECTED_PATH was {data.get('status')}"
        return PASS, "a hook-settings rewrite is recorded, not refused, while PROTECTED_PATH keeps observing"

    def _sbx_loop(self, state: Dict[str, Any]) -> Tuple[str, str]:
        if "LOOP" not in state.get("enforce", []):
            return SKIP, "LOOP is not among the sandbox's rules, so it was not promoted"
        session = self.session("sbx-loop")
        seen = []
        for command in (LOOPING_COMMAND, LOOPING_COMMAND, LOOPING_COMMAND, DIFFERENT_COMMAND):
            resp, data = self.evaluate(
                "", "Bash", "COMMAND_EXEC", {"command": command}, origin="hook", project=state["name"], session=session
            )
            if resp.status != 200:
                return FAIL, self.describe(resp)
            seen.append(data)
        statuses = _status_list(seen)
        if [d.get("status") for d in seen] != ["APPROVED", "APPROVED", "BLOCKED_LOOP_DETECTED", "APPROVED"]:
            return FAIL, f"hook calls under enforce answered {statuses}"
        if any(d.get("session_tripped") for d in seen):
            return FAIL, f"{statuses}, but the hook session was halted"
        return PASS, f"{statuses}: the repeat refused, the session never halted"

    def _sbx_demote(self, state: Dict[str, Any]) -> Tuple[str, str]:
        got = self.request("POST", f"api/projects/{state['name']}/demote", body={}, auth=True)
        if got.status != 200:
            return FAIL, f"POST demote {self.describe(got)}"
        config = self._project(state["name"]).obj().get("config") or {}
        if config.get("stage") != "observe":
            return FAIL, f"demoted, but stage={config.get('stage')!r}"
        if not any(isinstance(h, dict) and h.get("action") == "demote" for h in config.get("history") or []):
            return FAIL, "demoted, but history has no demote entry"
        return PASS, "stage observe again; history records the demotion"

    def _sbx_after_demote(self, state: Dict[str, Any]) -> Tuple[str, str]:
        resp, data = self._sbx_hook_write(state, "demoted", DOMAIN_WRITE)
        return self._observed(resp, data, "observe", "after demotion the same hook call")

    # =====================================================================
    # sign-in
    # =====================================================================

    def group_signin(self) -> None:
        group = "signin"
        if not self.has_key:
            self.record(group, "sign-in flow", SKIP, "no --key-file: minting a sign-in link needs the operator key")
            return
        # Runs under --read-only as well: it records nothing in the ledger and
        # changes no project, and the one session it mints it revokes itself.
        # Without it a private stack could not be checked without writes.
        state: Dict[str, Any] = {}
        steps: List[Tuple[str, Callable[[Dict[str, Any]], Tuple[str, str]]]] = [
            ("whoami anonymous", self._whoami_anonymous),
            ("whoami with the key", self._whoami_key),
            ("link minted with the key", self._link_mint),
            ("link refused without the key", self._link_needs_key),
            ("code exchanged for a session", self._session_exchange),
            ("code is single use", self._code_single_use),
            ("whoami with the session", self._whoami_session),
            ("session counts as the operator", self._session_operator),
            ("session cannot mint links", self._session_cannot_mint),
            ("sign out", self._sign_out),
            ("token refused after sign out", self._token_refused),
        ]
        for name, fn in steps:
            if state.get("stop"):
                self.record(group, name, SKIP, state["stop"])
                continue
            status = self.run_check(group, name, fn, state)
            if name in ("whoami anonymous", "link minted with the key", "code exchanged for a session") and status != PASS:
                state.setdefault("stop", f"depends on '{name}', which did not pass")

    def _whoami_anonymous(self, state: Dict[str, Any]) -> Tuple[str, str]:
        got = self.request("GET", "api/auth/whoami")
        absent = self.not_there(got)
        if absent:
            state["stop"] = "depends on the sign-in endpoints, which are not deployed yet"
            return absent
        data = got.obj()
        missing = [k for k in ("authenticated", "via", "expires_at", "reads_public", "sandbox_writes") if k not in data]
        if got.status != 200 or missing:
            return FAIL, f"{self.describe(got)}; lacks {', '.join(missing) or 'nothing'}"
        if data.get("authenticated") is not False or data.get("via") is not None:
            return FAIL, f"an anonymous caller is authenticated={data.get('authenticated')} via={data.get('via')!r}"
        mode = self.reads_mode()
        if mode in ("public", "private") and bool(data.get("reads_public")) != (mode == "public"):
            return FAIL, f"reads_public={data.get('reads_public')} on a stack whose reads are {mode}"
        return PASS, f"authenticated false, via null, reads_public {data.get('reads_public')}, sandbox_writes {data.get('sandbox_writes')}"

    def _whoami_key(self, state: Dict[str, Any]) -> Tuple[str, str]:
        data = self.request("GET", "api/auth/whoami", auth=True).obj()
        if data.get("authenticated") is not True or data.get("via") != "key":
            return FAIL, f"with the key: authenticated={data.get('authenticated')} via={data.get('via')!r}"
        return PASS, "authenticated true via key"

    def _link_mint(self, state: Dict[str, Any]) -> Tuple[str, str]:
        got = self.request("POST", "api/auth/links", body={"next": f"/projects/{PROBE_PROJECT}"}, auth=True)
        data = got.obj()
        # Held back from every line before anything about the link is written.
        self.keep_secret(data.get("code"))
        # Whole only: the link starts with this stack's base URL, which the
        # evidence names on purpose; the code inside it is held back above.
        self.keep_secret(data.get("url"), whole_only=True)
        if got.status not in (200, 201) or not isinstance(data.get("code"), str):
            return FAIL, f"POST /api/auth/links {self.describe(got)}"
        state["code"] = data["code"]
        problems = []
        if data.get("expires_in") != 120:
            problems.append(f"expires_in={data.get('expires_in')}")
        link = str(data.get("url", ""))
        if not link.startswith(f"{self.base}dashboard.html#/signin?"):
            problems.append("url does not open this stack's dashboard.html#/signin")
        # The hash route carries its own query, which may be percent-encoded,
        # so it is parsed rather than matched as text.
        route_query = urllib.parse.parse_qs(link.split("#", 1)[-1].partition("?")[2])
        if route_query.get("code") != [data["code"]]:
            problems.append("url does not carry the code")
        if route_query.get("next") != [f"/projects/{PROBE_PROJECT}"]:
            problems.append("url does not carry next")
        if problems:
            return FAIL, "minted, but " + ", ".join(problems)
        return PASS, "a code that expires in 120 s and a dashboard.html#/signin link on this stack, carrying next"

    def _link_needs_key(self, state: Dict[str, Any]) -> Tuple[str, str]:
        got = self.request("POST", "api/auth/links", body={"next": "/overview"})
        if got.status in (401, 403):
            return PASS, f"{got.status} without the key"
        self.keep_secret(got.obj().get("code"))
        return FAIL, f"an anonymous caller minted a link: {self.describe(got)}"

    def _session_exchange(self, state: Dict[str, Any]) -> Tuple[str, str]:
        got = self.request("POST", "api/auth/sessions", body={"code": state["code"]})
        data = got.obj()
        self.keep_secret(data.get("token"))
        if got.status not in (200, 201) or not isinstance(data.get("token"), str):
            return FAIL, f"POST /api/auth/sessions {self.describe(got)}"
        state["token"] = data["token"]
        self._session_minted = "minted"
        if data.get("ttl_seconds") != 43200 or not data.get("expires_at"):
            return FAIL, f"a token, but ttl_seconds={data.get('ttl_seconds')} expires_at={data.get('expires_at')!r}"
        return PASS, "a session token with ttl_seconds 43200 and an expiry"

    def _code_single_use(self, state: Dict[str, Any]) -> Tuple[str, str]:
        got = self.request("POST", "api/auth/sessions", body={"code": state["code"]})
        self.keep_secret(got.obj().get("token"))
        if got.status == 401:
            return PASS, "the same code a second time answers 401"
        return FAIL, f"the same code a second time answers {self.describe(got)}"

    def _whoami_session(self, state: Dict[str, Any]) -> Tuple[str, str]:
        data = self.request("GET", "api/auth/whoami", token=state["token"]).obj()
        if data.get("authenticated") is not True or data.get("via") != "session":
            return FAIL, f"with the session: authenticated={data.get('authenticated')} via={data.get('via')!r}"
        return PASS, "authenticated true via session"

    def _session_operator(self, state: Dict[str, Any]) -> Tuple[str, str]:
        # A malformed body to a protected write: the guard lets the session
        # through, the route refuses the body, and the rules stay as they are.
        write = self.request("POST", "rules", raw=MALFORMED, token=state["token"])
        if write.status != 400:
            return FAIL, f"a protected write with the session answers {self.describe(write)}, not 400 past the guard"
        evidence = "a protected write passes the guard (400 on a malformed body)"
        if self.expected_mode() == "private":
            read = self.request("GET", "api/insights", token=state["token"])
            if read.status != 200:
                return FAIL, f"{evidence}, but a private read with the session answers {self.describe(read)}"
            evidence += "; a private read answers 200"
        return PASS, evidence

    def _session_cannot_mint(self, state: Dict[str, Any]) -> Tuple[str, str]:
        got = self.request("POST", "api/auth/links", body={"next": "/overview"}, token=state["token"])
        self.keep_secret(got.obj().get("code"))
        if got.status in (401, 403):
            return PASS, f"{got.status}: minting a link needs the key itself"
        return FAIL, f"a session minted a sign-in link: {self.describe(got)}"

    def _sign_out(self, state: Dict[str, Any]) -> Tuple[str, str]:
        got = self.request("DELETE", "api/auth/sessions", token=state["token"])
        if got.status == 501:
            state["stop"] = "depends on sign out, which answered 501"
            return self.not_implemented(got)
        if got.status in (200, 204):
            self._session_minted = "revoked"
            return PASS, f"DELETE /api/auth/sessions answers {got.status}"
        return FAIL, f"DELETE /api/auth/sessions answers {self.describe(got)}"

    def _token_refused(self, state: Dict[str, Any]) -> Tuple[str, str]:
        who = self.request("GET", "api/auth/whoami", token=state["token"])
        if who.status == 200 and who.obj().get("authenticated") is not False:
            return FAIL, "whoami still authenticates the revoked token"
        write = self.request("POST", "rules", raw=MALFORMED, token=state["token"])
        if write.status not in (401, 403):
            return FAIL, f"a protected write with the revoked token answers {self.describe(write)}"
        return PASS, f"whoami answers {who.status} unauthenticated and a protected write {write.status}"

    # =====================================================================
    # distribution
    # =====================================================================

    def group_distribution(self) -> None:
        group = "distribution"
        installer = self.request("GET", "install.py")
        manifest = self.request("GET", "dist/manifest.json")
        self.run_check(group, "install.py names this stack", self._installer_endpoint, installer)
        self.run_check(group, "install.py matches its digest header", self._installer_header, installer)
        self.run_check(group, "install.py matches the manifest", self._installer_manifest, installer, manifest)
        self.run_check(group, "bundle matches the manifest file by file", self._bundle, manifest)
        self.run_check(group, "bundled hook is the served hook", self._bundle_hook, manifest)

    def _installer_endpoint(self, got: Response) -> Tuple[str, str]:
        absent = self.not_there(got)
        if absent:
            return absent
        if got.status != 200:
            return FAIL, f"GET /install.py {self.describe(got)}"
        text = got.text()
        if "__THREEFOLD_ENDPOINT__" in text:
            return FAIL, "the served installer still carries the literal __THREEFOLD_ENDPOINT__"
        if self.base not in text and self.base.rstrip("/") not in text:
            return FAIL, "the served installer does not name this stack's base URL"
        try:
            compile(text, "install.py", "exec")
        except SyntaxError as bad:
            return FAIL, f"the served installer is not valid Python: line {bad.lineno}"
        return PASS, f"{len(got.body)} B of valid Python naming this base, no placeholder left"

    def _installer_header(self, got: Response) -> Tuple[str, str]:
        absent = self.not_there(got)
        if absent:
            return absent
        stated = digest_from_headers(got)
        if stated is None:
            return SKIP, "no digest header on /install.py (looked for *sha256*, Digest, Content-Digest, Repr-Digest)"
        name, digest = stated
        actual = _sha256(got.body)
        if digest != actual:
            return FAIL, f"{name} states {digest[:12]}..., the body hashes to {actual[:12]}..."
        return PASS, f"{name} = sha256 of the body ({actual[:12]}...)"

    def _installer_manifest(self, installer: Response, manifest: Response) -> Tuple[str, str]:
        for resp in (installer, manifest):
            absent = self.not_there(resp)
            if absent:
                return absent
        data = manifest.obj()
        if manifest.status != 200 or not isinstance(data.get("files"), list):
            return FAIL, f"GET /dist/manifest.json {self.describe(manifest)} with no files list"
        missing = [k for k in ("bundle_sha256", "installer_sha256") if k not in data]
        if missing:
            return FAIL, "the manifest lacks " + ", ".join(missing)
        actual = _sha256(installer.body)
        if data.get("installer_sha256") != actual:
            return FAIL, f"installer_sha256 {str(data.get('installer_sha256'))[:12]}... but install.py hashes to {actual[:12]}..."
        return PASS, f"installer_sha256 equals the served install.py ({actual[:12]}...)"

    def _bundle(self, manifest: Response) -> Tuple[str, str]:
        absent = self.not_there(manifest)
        if absent:
            return absent
        bundle = self.request("GET", "dist/threefold-bundle.zip")
        absent = self.not_there(bundle)
        if absent:
            return absent
        data = manifest.obj()
        if bundle.status != 200:
            return FAIL, f"GET /dist/threefold-bundle.zip {self.describe(bundle)}"
        if data.get("bundle_sha256") != _sha256(bundle.body):
            return FAIL, "bundle_sha256 does not match the zip served"
        try:
            archive = zipfile.ZipFile(io.BytesIO(bundle.body))
        except zipfile.BadZipFile:
            return FAIL, "the bundle is not a zip archive"
        names = set(archive.namelist()) - {n for n in archive.namelist() if n.endswith("/")}
        listed = {str(f.get("path")): f for f in data.get("files") or [] if isinstance(f, dict)}
        problems = []
        for path, entry in listed.items():
            if path not in names:
                problems.append(f"{path} listed but not in the zip")
                continue
            content = archive.read(path)
            if entry.get("sha256") != _sha256(content):
                problems.append(f"{path} sha256 differs")
            if entry.get("bytes") != len(content):
                problems.append(f"{path} is {len(content)} B, the manifest says {entry.get('bytes')}")
        unlisted = sorted(names - set(listed))
        if unlisted:
            problems.append("in the zip but not the manifest: " + ", ".join(unlisted[:3]))
        required = ("bin/threefold_hook.py", "bin/threefold_cli.py", "lib/threefold/__init__.py")
        absent_files = [p for p in required if p not in names]
        if absent_files:
            problems.append("missing " + ", ".join(absent_files))
        if not any(n.startswith("lib/threefold/domain/") and n.endswith(".py") for n in names):
            problems.append("no lib/threefold/domain/*.py")
        if problems:
            return FAIL, "; ".join(problems[:4])
        return PASS, f"bundle_sha256 matches; {len(listed)} files, each with the listed sha256 and size"

    def _bundle_hook(self, manifest: Response) -> Tuple[str, str]:
        absent = self.not_there(manifest)
        if absent:
            return absent
        entry = next(
            (f for f in manifest.obj().get("files") or [] if isinstance(f, dict) and f.get("path") == "bin/threefold_hook.py"),
            None,
        )
        if entry is None:
            return FAIL, "the manifest lists no bin/threefold_hook.py"
        served = self.request("GET", "hooks/threefold_hook.py")
        if served.status != 200:
            return FAIL, f"GET /hooks/threefold_hook.py {self.describe(served)}"
        if entry.get("sha256") != _sha256(served.body):
            return FAIL, "the bundled hook differs from the one served at /hooks/threefold_hook.py"
        return PASS, "bin/threefold_hook.py in the bundle is byte for byte the served hook"

    # =====================================================================
    # headers and performance
    # =====================================================================

    def group_headers(self) -> None:
        group = "headers"
        self.run_check(group, "JSON routes say application/json", self._json_types)
        self.run_check(group, "errors are RFC 7807 problems", self._problem_types)
        self.run_check(group, "hook script headers", self._script_headers)
        self.run_check(group, "CORS on a JSON read", self._cors_simple)
        self.run_check(group, "CORS preflight for a keyed POST", self._cors_preflight)
        self.run_check(group, "latency of GET /status", self._latency_status)
        self.run_check(
            group, "latency of 20 approved evaluate calls",
            self.writes_skipped if self.read_only else self._latency_evaluate,
        )

    def _json_types(self) -> Tuple[str, str]:
        wrong, checked, closed = [], [], []
        for path in ("status", "openapi.json", "rules", "api/insights"):
            got = self.request("GET", path, auth=True)
            if got.status in (401, 403) and not self.has_key and path in LEGACY_READS:
                # A private read with no key to open it says nothing about the
                # type it would be served as.
                closed.append(f"/{path}")
                continue
            if got.status == 200 and got.content_type != "application/json":
                wrong.append(f"/{path} {got.content_type or 'none'}")
            elif got.status != 200:
                wrong.append(f"/{path} {got.status}")
            else:
                checked.append(f"/{path}")
        if wrong:
            return FAIL, "not 200 application/json: " + ", ".join(wrong)
        note = f"; {', '.join(closed)} private and no key given" if closed else ""
        return PASS, f"{', '.join(checked)} answer application/json{note}"

    def _problem_types(self) -> Tuple[str, str]:
        problems = []
        missing = self.request("GET", f"probe-{self.runid}-no-such-route")
        bad_body = self.request("POST", "evaluate-tool-call", raw=MALFORMED, auth=True)
        for resp, want in ((missing, 404), (bad_body, 400)):
            data = resp.obj()
            lacking = [k for k in ("type", "title", "status", "detail") if k not in data]
            if resp.status != want:
                problems.append(f"{resp.method} {resp.path.split('?')[0]} answers {resp.status}, not {want}")
            elif resp.content_type != "application/problem+json":
                problems.append(f"a {want} is {resp.content_type or 'untyped'}")
            elif lacking or data.get("status") != want:
                problems.append(f"a {want} problem lacks {', '.join(lacking) or 'a matching status'}")
        if problems:
            return FAIL, "; ".join(problems)
        return PASS, "an unknown route (404) and a malformed body (400) are application/problem+json with type, title, status, detail"

    def _script_headers(self) -> Tuple[str, str]:
        got = self.request("GET", "hooks/threefold_hook.py")
        if got.status != 200:
            return FAIL, f"GET /hooks/threefold_hook.py {self.describe(got)}"
        problems = []
        if got.content_type != "text/plain":
            problems.append(f"content type {got.content_type or 'none'}")
        if got.header("x-content-type-options").lower() != "nosniff":
            problems.append("no X-Content-Type-Options: nosniff")
        if problems:
            return FAIL, "; ".join(problems)
        return PASS, "text/plain with X-Content-Type-Options: nosniff, so a browser shows it rather than runs it"

    def _cors_simple(self) -> Tuple[str, str]:
        origin = "https://acme-probe.example"
        got = self.request("GET", "status", headers={"Origin": origin})
        values = got.header_values("access-control-allow-origin")
        if not values:
            return FAIL, f"GET /status with an Origin carries no Access-Control-Allow-Origin ({got.status})"
        if len(values) > 1:
            return FAIL, f"Access-Control-Allow-Origin is sent {len(values)} times; a browser rejects more than one"
        if values[0].strip() not in ("*", origin):
            return FAIL, f"Access-Control-Allow-Origin is {values[0]!r}"
        return PASS, f"Access-Control-Allow-Origin: {values[0].strip()} on a JSON read"

    def _cors_preflight(self) -> Tuple[str, str]:
        origin = "https://acme-probe.example"
        got = self.request(
            "OPTIONS", "evaluate-tool-call",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-api-key,authorization",
            },
        )
        if not 200 <= got.status < 300:
            return FAIL, f"the preflight answers {self.describe(got)}"
        allow_origin = got.header_values("access-control-allow-origin")
        methods = {m.strip().upper() for m in got.header("access-control-allow-methods").split(",")}
        allowed = {h.strip().lower() for h in got.header("access-control-allow-headers").split(",")}
        problems = []
        if len(allow_origin) != 1 or allow_origin[0].strip() not in ("*", origin):
            problems.append(f"Access-Control-Allow-Origin {allow_origin!r}")
        if "POST" not in methods and "*" not in methods:
            problems.append("POST not in Access-Control-Allow-Methods")
        missing = [h for h in ("content-type", "x-api-key", "authorization") if h not in allowed and "*" not in allowed]
        if missing:
            problems.append("Access-Control-Allow-Headers lacks " + ", ".join(missing))
        if problems:
            return FAIL, "; ".join(problems)
        return PASS, "the preflight allows POST with Content-Type, X-API-Key and Authorization from any origin"

    def _latency_status(self) -> Tuple[str, str]:
        samples = []
        for _ in range(10):
            got = self.request("GET", "status")
            if got.status != 200:
                return FAIL, f"GET /status answered {self.describe(got)} during the latency run"
            samples.append(got.elapsed_ms)
        p50, p95 = percentile(samples, 50), percentile(samples, 95)
        self.latency["GET /status"] = (p50, p95, len(samples))
        return PASS, f"10 calls: p50 {p50:.0f} ms, p95 {p95:.0f} ms"

    def _latency_evaluate(self) -> Tuple[str, str]:
        session = self.session("perf")
        # One call first, not counted, so a cold container does not stand in
        # for the gate's own latency.
        self.evaluate("", "Read", "FILE_READ", {"file_path": "docs/acme-probe-warmup.md"}, origin="hook", session=session)
        samples, refused = [], []
        for index in range(20):
            resp, data = self.evaluate(
                "", "Read", "FILE_READ", {"file_path": f"docs/acme-probe-{index:02d}.md"}, origin="hook", session=session
            )
            if resp.status != 200 or data.get("status") != "APPROVED":
                refused.append(f"{resp.status} {data.get('status')}")
            samples.append(resp.elapsed_ms)
        p50, p95 = percentile(samples, 50), percentile(samples, 95)
        self.latency["POST /evaluate-tool-call"] = (p50, p95, len(samples))
        if refused:
            return FAIL, f"{len(refused)} of 20 plain reads were not approved (first: {refused[0]})"
        if p95 > self.p95_budget_ms:
            return FAIL, f"p50 {p50:.0f} ms, p95 {p95:.0f} ms, over the {self.p95_budget_ms:.0f} ms budget"
        return PASS, f"20 approved: p50 {p50:.0f} ms, p95 {p95:.0f} ms (budget {self.p95_budget_ms:.0f} ms)"

    # =====================================================================
    # the evidence file
    # =====================================================================

    def markdown(self) -> str:
        counts = {s: sum(1 for c in self.results if c.status == s) for s in (PASS, FAIL, SKIP)}
        lines = [
            f"# Threefold live probes, {self.started_at.date().isoformat()}",
            "",
            f"- **Base:** {self.base}",
            f"- **Expected reads:** {self.expect or 'not given'} (detected: {self._mode or 'not probed'})",
            f"- **Run:** `probe-{self.runid}` at {self.started_at.strftime('%Y-%m-%dT%H:%M:%SZ')}, "
            f"{self.requests} requests, {self.retried_429} retried after 429",
            f"- **Key:** {'provided, never printed' if self.has_key else 'none'}",
            "- **Writes:** " + self._writes_note() + self._signin_note(),
            f"- **Not deployed yet counts as:** {'FAIL (--strict)' if self.strict else 'SKIP'}",
            f"- **Result:** {counts[PASS]} PASS, {counts[FAIL]} FAIL, {counts[SKIP]} SKIP",
            "",
            "Generated by `scripts/probe_live.py`. Each line is what the stack answered at the time of the run;",
            "rerun the command to see it now.",
        ]
        if self.latency:
            lines += ["", "## Latency", "", "| Call | Samples | p50 ms | p95 ms |", "|---|---|---|---|"]
            for call, (p50, p95, n) in self.latency.items():
                lines.append(f"| {call} | {n} | {p50:.0f} | {p95:.0f} |")
        for group in GROUPS:
            checks = [c for c in self.results if c.group == group]
            if not checks:
                continue
            lines += ["", f"## {group}", "", "| Check | Result | Evidence |", "|---|---|---|"]
            for check in checks:
                lines.append(f"| {_cell(check.name)} | {check.status} | {_cell(check.evidence)} |")
        return self.scrub("\n".join(lines) + "\n")

    def _writes_note(self) -> str:
        """Every kind of write the run made, noted as it made them, rather than what it meant to make."""
        if self.writes:
            return "; ".join(self.writes)
        return "none to the ledger or any project, read-only run" if self.read_only else "none"

    def _signin_note(self) -> str:
        if self._session_minted == "revoked":
            return "; the sign-in group minted one session and revoked it"
        if self._session_minted == "minted":
            return "; the sign-in group minted one session it could not revoke, which expires within 12 hours"
        return ""


def _cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _print_line(line: str) -> None:
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)


# ---------------------------------------------------------------------------
# Command line.
# ---------------------------------------------------------------------------


def parse_groups(value: Optional[str]) -> List[str]:
    if not value:
        return list(GROUPS)
    chosen = []
    for raw in value.split(","):
        name = GROUP_ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if not name:
            continue
        if name not in GROUPS:
            raise ValueError(f"unknown group {raw.strip()!r}; the groups are {', '.join(GROUPS)}")
        chosen.append(name)
    if not chosen:
        raise ValueError("--only names no group")
    return chosen


def read_key(path: str) -> str:
    """The key from its file, stripped. Its content is never echoed, even in an error."""
    try:
        key = Path(path).read_text(encoding="utf-8-sig").strip()
    except OSError as problem:
        raise ValueError(f"cannot read --key-file ({problem.strerror or 'unreadable'})") from None
    if not key or "\n" in key:
        raise ValueError("--key-file must hold exactly one key on one line")
    return key


REPOSITORY = Path(__file__).resolve().parents[1]
# The groups that can write to the stack when --read-only is not given.
WRITING_GROUPS = ("contract", "governance", "application", "headers")


def default_out(
    runid: str,
    private: bool,
    *,
    root: Optional[Path] = None,
    temp: Optional[Path] = None,
    today: Optional[str] = None,
) -> Path:
    """Where the evidence goes when --out is not given, never over an earlier file.

    A public stack's evidence belongs in the repository, as
    docs/evidence/PROBES_<date>.md, or PROBES_<date>-<runid>.md when a run on
    the same day already wrote that. A private stack's evidence names the
    private stack's address, which the repository must not carry, so it goes
    to the system's temporary folder instead and the owner decides where it
    belongs.
    """
    today = today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    if private:
        return Path(temp or tempfile.gettempdir()) / "threefold-probes" / f"PROBES_{today}-{runid}.md"
    folder = (root or REPOSITORY) / "docs" / "evidence"
    first = folder / f"PROBES_{today}.md"
    return first if not first.exists() else folder / f"PROBES_{today}-{runid}.md"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check a deployed Threefold stack against every claim the project makes.",
    )
    parser.add_argument("--base", required=True, help="the stack's base URL, such as https://<api>/prod/")
    parser.add_argument("--key-file", help="a file holding the operator key; the key is never printed")
    parser.add_argument("--expect", choices=("public", "private"), help="whether page reads should be open")
    parser.add_argument(
        "--out",
        help="the markdown evidence file ('-' writes none). Default: docs/evidence/PROBES_<date>.md, or "
        "PROBES_<date>-<runid>.md if that exists; for a private or keyed run, the system's temporary folder",
    )
    parser.add_argument("--only", help="a comma-separated list of groups: " + ",".join(GROUPS))
    parser.add_argument(
        "--read-only", action="store_true",
        help="skip every check that records a decision or changes a project (sign-in still mints and revokes one session)",
    )
    parser.add_argument("--strict", action="store_true", help="report contract endpoints that are not deployed yet as FAIL")
    parser.add_argument("--project-pattern", default=DEFAULT_PROJECT_PATTERN, help="the stack's AllowedProjectPattern")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds per request (default 20)")
    parser.add_argument(
        "--pace", default="auto",
        help="seconds between requests, or 'auto': none for the first 40, then 0.5 s, the rate the stack refills",
    )
    parser.add_argument("--p95-budget-ms", type=float, default=2000.0, help="p95 ceiling for evaluate calls (default 2000)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        groups = parse_groups(args.only)
        key = read_key(args.key_file) if args.key_file else None
        pace = None if str(args.pace).lower() == "auto" else float(args.pace)
        re.compile(args.project_pattern)
        probe = Probe(
            args.base,
            key=key,
            expect=args.expect,
            read_only=args.read_only,
            strict=args.strict,
            pace=pace,
            timeout=args.timeout,
            project_pattern=args.project_pattern,
            p95_budget_ms=args.p95_budget_ms,
        )
    except (ValueError, re.error) as problem:
        print(f"probe_live: {problem}", file=sys.stderr)
        return 2
    _print_line(
        f"Probing {probe.base} as run probe-{probe.runid} "
        f"({'read-only' if probe.read_only else 'writes allowed'}, key {'provided' if probe.has_key else 'none'}) "
        f"groups: {', '.join(groups)}"
    )
    if not probe.read_only and not probe.is_local and set(groups) & set(WRITING_GROUPS):
        # The only thing standing between a run and a deployed ledger is this
        # flag, so the run says what it is about to leave there before it does.
        _print_line(
            f"Warning: this run writes to {urllib.parse.urlparse(probe.base).hostname}: synthetic calls under "
            f"probe-{probe.runid}-* and {PROBE_PROJECT}, one call recorded as unlabelled, the demo simulations under "
            f"{SIMULATION_PROJECT}, and a sandbox on a public stack. Pass --read-only to write nothing."
        )
    try:
        probe.run(groups)
    finally:
        # Written even if the run is interrupted, so what was found is kept.
        counts = {s: sum(1 for c in probe.results if c.status == s) for s in (PASS, FAIL, SKIP)}
        _print_line(
            f"\n{counts[PASS]} PASS, {counts[FAIL]} FAIL, {counts[SKIP]} SKIP; "
            f"{probe.requests} requests, {probe.retried_429} retried after 429"
        )
        if args.out != "-":
            # A keyed run is the owner's run of their own stack unless it was
            # seen or declared public, so its evidence stays out of the
            # repository by default as well.
            private = probe.expect == "private" or probe.detected_mode == "private" or (
                probe.has_key and "public" not in (probe.expect, probe.detected_mode)
            )
            out = Path(args.out) if args.out else default_out(probe.runid, private)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(probe.markdown(), encoding="utf-8")
            where = " (outside the repository: it names a private stack)" if private and not args.out else ""
            _print_line(f"Evidence written to {out}{where}")
    return 1 if probe.failed else 0


if __name__ == "__main__":
    sys.exit(main())
