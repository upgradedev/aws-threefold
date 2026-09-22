"""Zero-Trust Security & API Guard Middleware for Threefold.

Implements:
1. API Key & Bearer Token Authentication with configurable environment bypass.
2. In-Memory Token-Bucket Rate Limiter (DoS & LLM cost-exhaustion defense).
3. RFC 7807 Problem Details for standardized HTTP error reporting.
4. Payload size & content guard (1MB ceiling).
5. Sign-in sessions: a live session token counts as the operator everywhere a
   configured key does, except for minting sign-in links.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

from threefold.infrastructure import auth_store

MAX_PAYLOAD_SIZE_BYTES = 1024 * 1024
# Read from the environment so no key literal lives in the source tree. The
# fallback is a placeholder, not a credential: the middleware only enforces keys
# when STAGE is prod or ENFORCE_API_KEY is set, and a deployment that turns those
# on without setting THREEFOLD_API_KEYS is meant to reject every request.
DEFAULT_DEMO_API_KEY = os.environ.get("THREEFOLD_DEMO_API_KEY", "unset-demo-key")


class TokenBucketRateLimiter:
    """Thread-safe token-bucket rate limiter for API endpoints."""

    def __init__(self, refill_rate_per_sec: float = 2.0, max_tokens: float = 60.0):
        self.refill_rate = refill_rate_per_sec
        self.max_tokens = max_tokens
        self._buckets: Dict[str, Tuple[float, float]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, client_id: str, tokens_requested: float = 1.0) -> bool:
        with self._lock:
            now = time.monotonic()
            if client_id not in self._buckets:
                self._buckets[client_id] = (self.max_tokens - tokens_requested, now)
                return True

            current_tokens, last_update = self._buckets[client_id]
            elapsed = now - last_update
            refreshed_tokens = min(self.max_tokens, current_tokens + elapsed * self.refill_rate)

            if refreshed_tokens >= tokens_requested:
                self._buckets[client_id] = (refreshed_tokens - tokens_requested, now)
                return True

            self._buckets[client_id] = (refreshed_tokens, now)
            return False

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


_global_rate_limiter = TokenBucketRateLimiter(refill_rate_per_sec=2.0, max_tokens=60.0)


def rfc7807_error(
    status_code: int,
    title: str,
    detail: str,
    instance: str = "/",
    error_type: str = "about:blank",
    invalid_params: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Generates an RFC 7807 Problem Details compliant error dictionary."""
    problem: Dict[str, Any] = {
        "error": detail,
        "type": error_type,
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": instance,
        "timestamp": time.time(),
    }
    if invalid_params:
        problem["invalid_params"] = invalid_params
    return problem


def presented_bearer(headers: Dict[str, str]) -> Optional[str]:
    """The value of `Authorization: Bearer <value>`, whatever it turns out to be.

    Read on its own, apart from X-API-Key, because a sign-in session travels
    only here: a request carrying a stale X-API-Key and a good session token
    would otherwise never have its session found. The scheme is matched without
    regard to case, as RFC 9110 says it is.
    """
    normalized = {str(k).lower(): v for k, v in headers.items()}
    auth_header = normalized.get("authorization")
    if isinstance(auth_header, str) and auth_header[:7].lower() == "bearer ":
        return auth_header[7:].strip() or None
    return None


def _presented_key(headers: Dict[str, str]) -> Optional[str]:
    """Reads the key off either header the API documents."""
    normalized = {str(k).lower(): v for k, v in headers.items()}
    api_key = normalized.get("x-api-key")
    if not isinstance(api_key, str) or not api_key:
        api_key = presented_bearer(headers)
    return api_key or None


def key_ref(key: str) -> str:
    """A short fingerprint of a key, stored with what the key minted.

    A sign-in code and the session it becomes carry the fingerprint of the key
    that asked for them, so removing that key from THREEFOLD_API_KEYS ends
    every session it opened rather than leaving them live for twelve hours.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _key_matches(presented: str, accepted: list[str]) -> bool:
    """Compares in constant time, and compares bytes.

    `in` on strings returns at the first differing character, which tells a
    patient caller how much of a key they have right. compare_digest does not,
    but it raises TypeError on a str that is not ASCII, which would have turned
    a malformed key into a server error, so both sides are encoded first.
    """
    candidate = presented.encode("utf-8")
    matched = False
    for key in accepted:
        if hmac.compare_digest(candidate, key.encode("utf-8")):
            matched = True
    return matched


def _configured_keys(allow_placeholder: bool) -> list[str]:
    """The keys this deployment accepts.

    `allow_placeholder` is the difference between the two callers. Reading the
    API with keys switched on falls back to the demo placeholder, which is how
    the local and container setups have always worked. A policy write does not:
    a deployment that has configured no key refuses the write rather than
    accepting one that is printed in the source.
    """
    raw = os.environ.get("THREEFOLD_API_KEYS")
    if raw is None and allow_placeholder:
        raw = DEFAULT_DEMO_API_KEY
    return [k.strip() for k in (raw or "").split(",") if k.strip()]


def presented_key_ref(headers: Dict[str, str]) -> Optional[str]:
    """The fingerprint of the configured operator key this request presents, if any.

    Both headers are tried, because a caller may send the key in either. The
    demo placeholder is never a key here: it is printed in the source.
    """
    accepted = _configured_keys(allow_placeholder=False)
    if not accepted:
        return None
    normalized = {str(k).lower(): v for k, v in headers.items()}
    for value in (normalized.get("x-api-key"), presented_bearer(headers)):
        if isinstance(value, str) and value and _key_matches(value.strip(), accepted):
            return key_ref(value.strip())
    return None


def live_session(headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """The sign-in session this request presents, if it is live and its key still is.

    Only a bearer value shaped like a session token is looked up, so a wrong
    key costs no read of the table. A session whose key has been removed from
    the stack is not a session any more.
    """
    token = presented_bearer(headers)
    if not token or not token.startswith(auth_store.SESSION_TOKEN_PREFIX):
        return None
    record = auth_store.default_store().session(token)
    if record is None or record.get("key_ref") not in configured_key_refs():
        return None
    return record


def configured_key_refs() -> set[str]:
    """The fingerprints of the operator keys configured now, placeholder excluded."""
    return {key_ref(k) for k in _configured_keys(allow_placeholder=False)}


def operator_identity(headers: Dict[str, str], allow_session: bool = True) -> Optional[Dict[str, Any]]:
    """Who the request is, as far as the operator ladder cares.

    Returns {"via": "key"|"session", "key_ref", "expires_at"} with expires_at an
    epoch second for a session and None for a key, or None for anyone else.
    """
    ref = presented_key_ref(headers)
    if ref is not None:
        return {"via": "key", "key_ref": ref, "expires_at": None}
    if allow_session:
        record = live_session(headers)
        if record is not None:
            return {"via": "session", "key_ref": record["key_ref"], "expires_at": int(record["ttl"])}
    return None


# Requests that change what the gates do to everyone after them. `POST
# /policy/config` writes through to DynamoDB under `CONFIG#policy`, and every
# later container adopts it on a cold start, so an anonymous caller could raise
# the loop threshold this product leads with and the change would outlive them.
# Reading the policy stays open: a judge has to be able to see what is enforced.
PROTECTED_WRITES = {
    ("POST", "/policy/config"),
    ("POST", "/policy"),
    # The layering rules are the architecture itself. An anonymous caller who
    # could rewrite them could delete the gate rather than trip it.
    ("POST", "/rules"),
    ("POST", "/rules/layering"),
}


def is_session_resume(method: str, path: str) -> bool:
    """True for POST /sessions/{id}/resume, which a set of fixed paths cannot list.

    Resuming clears a halt that a gate or an operator decided, and everything
    the session does afterwards runs on it, so it climbs the same ladder a
    policy write does. The kill switch beside it stays where STATE.md puts it:
    freezing a session can only stop work, and clearing one can restart it.
    """
    return method.upper() == "POST" and path.startswith("/sessions/") and path.endswith("/resume")


def is_protected_write(method: str, path: str) -> bool:
    """A write that needs the operator key on every stack, whatever else is open."""
    return (method.upper(), path) in PROTECTED_WRITES or is_session_resume(method, path)


# Sign-in. Minting a link is the one thing a session may not do: a session that
# could mint links could renew itself forever, and a stolen one would never
# lapse. The exchange, the sign-out and whoami are open, because each of them
# authenticates by what it carries rather than by who is asking.
AUTH_LINKS_PATH = "/api/auth/links"
AUTH_SESSIONS_PATH = "/api/auth/sessions"
AUTH_WHOAMI_PATH = "/api/auth/whoami"

PROJECTS_PATH = "/api/projects"
SANDBOX_PATH = "/api/sandbox"

# The one project name a stranger may write to, and only on a stack whose reads
# are public. POST /api/sandbox mints these names, so a visitor can walk the
# observe, review and promote loop on a project of their own without being
# handed the operator's. Matched against the path as it arrived, not decoded:
# a name that only matches after unquoting needs the operator, which is the
# safe direction to be wrong in.
SANDBOX_PROJECT_PATTERN = re.compile(r"Acme-Sandbox-[0-9a-f]{8}")
# What may follow a sandbox name: action segments in lower-case letters, such as
# /promote, /demote and /reviews. Nothing with a dot or a percent sign, so
# "/api/projects/<sandbox>/../<real project>" is never read as a sandbox write
# by one router and as a write to the real project by another.
_SANDBOX_ACTIONS = re.compile(r"(?:/[a-z]+)*")


def is_project_write(method: str, path: str) -> bool:
    """Anything but a read under /api/projects, which changes how a project is governed."""
    return method.upper() not in ("GET", "HEAD") and (
        path == PROJECTS_PATH or path.startswith(PROJECTS_PATH + "/")
    )


def is_sandbox_project_path(path: str) -> bool:
    """True for /api/projects/Acme-Sandbox-<8 hex> and the actions under it."""
    if not path.startswith(PROJECTS_PATH + "/"):
        return False
    name, slash, tail = path[len(PROJECTS_PATH) + 1:].partition("/")
    return bool(SANDBOX_PROJECT_PATTERN.fullmatch(name)) and bool(
        _SANDBOX_ACTIONS.fullmatch(slash + tail)
    )


# The pages and the hook are handed out anonymously on purpose: a reader who
# cannot open the install page or take the script cannot adopt the product, and
# a key requirement would put the artifacts that matter behind the one thing a
# visitor does not have. Only "/" used to be listed, so with keys enforced the
# dashboard opened and every link out of it answered 401. The hook is served
# under its own name and under the name it had before, so an install command a
# reader copied last week still works.
PUBLIC_PATHS = frozenset(
    {
        "/",
        "/index.html",
        "/connect.html",
        "/console.html",
        "/rules.html",
        "/sessions.html",
        "/settings.html",
        "/swagger.html",
        "/status",
        "/health",
        "/docs",
        "/openapi.json",
        "/openapi.yaml",
        "/hooks/threefold_hook.py",
        "/hooks/claude_code_hook.py",
        "/claude_code_hook.py",
        # The application and what connecting a repository downloads. The
        # installer and the bundle are how a team adopts the product, and the
        # dashboard is where a private stack's operator signs in, so none of
        # them can sit behind the key they are the way to.
        "/dashboard.html",
        "/app",
        "/install.py",
        "/dist/threefold-bundle.zip",
        "/dist/manifest.json",
        # Open on every method: each authenticates by what it carries. The
        # links route is deliberately absent, as is /api/sandbox, because this
        # set ignores the method and would open them on a private stack.
        AUTH_SESSIONS_PATH,
        AUTH_WHOAMI_PATH,
    }
)

# The dashboard's scripts, styles and icons. A prefix rather than a list, since
# the files are the page track's to add; which names are actually served is
# decided by the route's own allowlist, so this opens nothing that is not a file
# in web/assets/.
ASSETS_PREFIX = "/assets/"


def is_served_asset(method: str, path: str) -> bool:
    return method.upper() in ("GET", "HEAD") and path.startswith(ASSETS_PREFIX)

# The reads the pages make. Opening a page and refusing the data it is built on
# is the same regression as refusing the page, one step later: the console and
# the rules screen rendered and then every fetch answered 401 the moment STAGE
# or ENFORCE_API_KEY was set. Only reads are listed, by method, and each of them
# changes nothing: GET /sessions/{id} answers 404 for an id it has not seen
# rather than creating it. /readyz is not here: no page reads it, and it reports
# the table name, region, model id and raw client errors.
PAGE_READS = frozenset(
    {
        "/api/insights",
        "/api/sessions",
        "/rules",
        "/rules/layering",
        "/policy/config",
        "/policy",
        # The application's reads. Every row they return is reduced by
        # public_row(), as the console's are, and on a private stack they are
        # the operator's alone.
        "/api/overview",
        "/api/decisions",
        "/api/decision",
        PROJECTS_PATH,
    }
)

# The reads that need a body, so they arrive as POST. Trying a rule changes
# nothing and records nothing. Drafting one is the same kind of read with a model
# call inside it: no rule is saved, no verdict is issued, no ledger row is
# written, and the draft goes back only to the caller who asked. So both are
# decided as a page's read: open where PublicReads is true, which is how the
# public demo's visitors reach them with no key to present, and the operator's,
# by key or sign-in session, on a stack that keeps its rules private and pays
# for every draft. Putting a draft in force is POST /rules, a protected write.
PAGE_READ_POSTS = frozenset({"/rules/explain", "/rules/draft"})

PUBLIC_READS_ENV = "PUBLIC_READS"


def reads_are_public() -> bool:
    """Whether the data behind the pages is open to anyone, the default.

    The demo stack answers these reads anonymously because that is the ship
    gate. A stack that carries real use is deployed with PublicReads=false and
    they need the operator key; the pages themselves still open, and say so
    when a read is refused. Only an explicit false closes them, and the template
    admits nothing but "true" and "false".
    """
    return os.environ.get(PUBLIC_READS_ENV, "true").strip().lower() not in ("false", "0", "no")


def is_page_read(method: str, path: str) -> bool:
    """True for a read a page makes, which is open unless PublicReads is false."""
    verb = method.upper()
    if verb in ("GET", "HEAD") and (
        path in PAGE_READS
        or (path.startswith("/sessions/") and path.count("/") == 2)
        # One project's readiness, GET /api/projects/<name>, and nothing deeper.
        or (path.startswith(PROJECTS_PATH + "/") and path.count("/") == 3)
    ):
        return True
    # Trying a rule, or drafting one, changes nothing and records nothing, so
    # each is a read that happens to need a body.
    return verb == "POST" and path in PAGE_READ_POSTS


def _require_operator_key(
    headers: Dict[str, str],
    path: str,
    closed_title: str,
    closed_detail: str,
    closed_type: str,
    missing_detail: str,
    allow_session: bool = True,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """The ladder a durable write, or a private read, climbs.

    A configured key, or a live sign-in session where `allow_session` is set,
    passes. Otherwise: no key configured is 403 and says the door is shut here,
    a missing credential is 401, a wrong or expired one is 403. The demo
    placeholder never opens it: that key is printed in the source, so anyone
    could present it. A session cannot outlast the keys either, since it is
    honoured only while the key that opened it is still configured.
    """
    if operator_identity(headers, allow_session=allow_session) is not None:
        return True, None
    if not _configured_keys(allow_placeholder=False):
        return False, rfc7807_error(
            status_code=403,
            title=closed_title,
            detail=closed_detail,
            instance=path,
            error_type=closed_type,
        )
    if not _presented_key(headers):
        return False, rfc7807_error(
            status_code=401,
            title="Unauthorized",
            detail=missing_detail,
            instance=path,
            error_type="urn:threefold:error:missing-credentials",
        )
    if not allow_session and live_session(headers) is not None:
        return False, rfc7807_error(
            status_code=403,
            title="A Session Cannot Do This",
            detail=(
                "A sign-in session counts as the operator everywhere except here: minting a "
                "sign-in link needs the operator key itself, so a session can never renew "
                "itself. Run the CLI with the key file to get a link."
            ),
            instance=path,
            error_type="urn:threefold:error:session-not-enough",
        )
    return False, rfc7807_error(
        status_code=403,
        title="Forbidden",
        detail="The key or sign-in session presented is invalid or has expired.",
        instance=path,
        error_type="urn:threefold:error:invalid-credentials",
    )


def validate_request_security(
    headers: Dict[str, str],
    client_ip: str,
    path: str,
    payload_size_bytes: int = 0,
    rate_limiter: Optional[TokenBucketRateLimiter] = None,
    method: str = "GET",
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    limiter = rate_limiter or _global_rate_limiter
    stage = os.environ.get("STAGE", "dev").lower()
    enforce_auth = os.environ.get("ENFORCE_API_KEY", "false").lower() in ("true", "1", "yes")
    # HEAD is GET without the body, so it is decided exactly as GET is. Decided
    # as itself, a HEAD on an open read fell through to the key check.
    verb = "GET" if method.upper() == "HEAD" else method.upper()

    # 1. Payload size check
    if payload_size_bytes > MAX_PAYLOAD_SIZE_BYTES:
        return False, rfc7807_error(
            status_code=413,
            title="Payload Too Large",
            detail=f"Payload size ({payload_size_bytes} bytes) exceeds maximum ceiling ({MAX_PAYLOAD_SIZE_BYTES} bytes)",
            instance=path,
            error_type="urn:threefold:error:payload-too-large",
        )

    # 2. Rate limiting check
    if not limiter.is_allowed(client_ip):
        return False, rfc7807_error(
            status_code=429,
            title="Too Many Requests",
            detail="Rate limit exceeded. Maximum 60 requests per minute allowable.",
            instance=path,
            error_type="urn:threefold:error:rate-limit-exceeded",
        )

    # 3. Minting a sign-in link needs the key itself. A session is refused here,
    # because a session that could mint links could keep itself alive forever.
    if verb == "POST" and path == AUTH_LINKS_PATH:
        return _require_operator_key(
            headers,
            path,
            closed_title="Sign-In Is Closed Here",
            closed_detail=(
                "This deployment has no operator key configured, so there is nobody to sign "
                "in as. Set THREEFOLD_API_KEYS on the function to enable sign-in."
            ),
            closed_type="urn:threefold:error:sign-in-disabled",
            missing_detail=(
                "Minting a sign-in link requires the operator key. Provide it via 'X-API-Key' "
                "or 'Authorization: Bearer <key>'."
            ),
            allow_session=False,
        )

    # 3a. Durable policy writes are closed whether or not the rest is
    if is_session_resume(verb, path):
        return _require_operator_key(
            headers,
            path,
            closed_title="Sessions Cannot Be Resumed Here",
            closed_detail=(
                "This deployment has no operator key configured, so a halted session stays "
                "halted. Set THREEFOLD_API_KEYS on the function to enable resuming."
            ),
            closed_type="urn:threefold:error:policy-write-disabled",
            missing_detail=(
                "Resuming a halted session requires the operator key, because it clears a halt "
                "that a gate or an operator decided. Provide it via 'X-API-Key' or "
                "'Authorization: Bearer <key>'."
            ),
        )
    if is_protected_write(verb, path):
        return _require_operator_key(
            headers,
            path,
            closed_title="Policy Is Read Only Here",
            closed_detail=(
                "This deployment has no policy-write key configured, so the policy can be "
                "read but not changed. Set THREEFOLD_API_KEYS on the function to enable "
                "writes."
            ),
            closed_type="urn:threefold:error:policy-write-disabled",
            missing_detail=(
                "Changing the policy requires a key, even where reading it does not, because "
                "the write is durable and applies to every session after it. Provide it via "
                "'X-API-Key' or 'Authorization: Bearer <key>'."
            ),
        )

    # 3b. A project's stage, the rules it keeps observing and the review of what
    # it flagged decide what is refused on real developers' machines, so they
    # are the operator's. The exception is a sandbox, on a stack whose reads are
    # public: a visitor walking the demo gets a project of their own to promote.
    if is_project_write(verb, path):
        if reads_are_public() and is_sandbox_project_path(path):
            return True, None
        return _require_operator_key(
            headers,
            path,
            closed_title="Projects Are Read Only Here",
            closed_detail=(
                "This deployment has no operator key configured, so a project's stage, the "
                "rules it observes and its reviews can be read but not changed. Set "
                "THREEFOLD_API_KEYS on the function to enable changes."
            ),
            closed_type="urn:threefold:error:policy-write-disabled",
            missing_detail=(
                "Changing a project's stage, the rules it observes or its reviews requires the "
                "operator: the key via 'X-API-Key' or 'Authorization: Bearer <key>', or a "
                "sign-in session via 'Authorization: Bearer <token>'."
            ),
        )

    # 3c. A sandbox seeds synthetic calls into the ledger. On the public demo
    # that is the walkthrough; on a stack that carries real use it would mix
    # invented rows into real totals, so there only the operator may ask.
    if verb == "POST" and path == SANDBOX_PATH:
        if reads_are_public():
            return True, None
        return _require_operator_key(
            headers,
            path,
            closed_title="Sandboxes Are Closed Here",
            closed_detail=(
                "This deployment keeps its data private and has no operator key configured, so "
                "it creates no sandbox projects."
            ),
            closed_type="urn:threefold:error:reads-private",
            missing_detail=(
                "This deployment keeps its data private, so only the operator may create a "
                "sandbox project here. Provide the key via 'X-API-Key' or 'Authorization: "
                "Bearer <key>', or a sign-in session via 'Authorization: Bearer <token>'."
            ),
        )

    # 4. The pages, the hook and what connecting downloads, open on every stack.
    if path in PUBLIC_PATHS or is_served_asset(verb, path):
        return True, None

    # 4b. The reads those pages make: open by default, and on a stack deployed
    # with PublicReads=false closed exactly as a policy write is, because what
    # they return is that stack's real use. Writes to the same paths were
    # decided in step 3. When keys are enforced, the kill switch under
    # /sessions/{id}/terminate needs one like every other unlisted call; a
    # deployment that enforces no key, as the demo stack does, answers it
    # anonymously, and STATE.md says so.
    if is_page_read(verb, path):
        if reads_are_public():
            return True, None
        return _require_operator_key(
            headers,
            path,
            closed_title="Reads Are Private Here",
            closed_detail=(
                "This deployment keeps its sessions, ledger and rules private and has no "
                "operator key configured, so nothing here can be read. Set THREEFOLD_API_KEYS "
                "on the function to read it."
            ),
            closed_type="urn:threefold:error:reads-private",
            missing_detail=(
                "This deployment keeps its sessions, ledger and rules private, so reading them "
                "requires the operator key. Provide it via 'X-API-Key' or "
                "'Authorization: Bearer <key>'."
            ),
        )

    # 5. Authentication enforcement. A sign-in session is the operator here as
    # everywhere else; what follows is the key check as it always was, demo
    # placeholder included.
    if (enforce_auth or stage == "prod") and operator_identity(headers) is None:
        api_key = _presented_key(headers)
        configured_keys = _configured_keys(allow_placeholder=True)

        if not api_key:
            return False, rfc7807_error(
                status_code=401,
                title="Unauthorized",
                detail="Missing required API Key. Provide via 'X-API-Key' or 'Authorization: Bearer <key>' header.",
                instance=path,
                error_type="urn:threefold:error:missing-credentials",
            )

        if not _key_matches(api_key, configured_keys):
            return False, rfc7807_error(
                status_code=403,
                title="Forbidden",
                detail="Provided API Key is invalid or expired.",
                instance=path,
                error_type="urn:threefold:error:invalid-credentials",
            )

    return True, None
