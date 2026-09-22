"""Sign-in, the dashboard, and what connecting a repository downloads.

Two jobs share this module because both are about getting a person or a
machine onto a stack without handing either the operator key:

* Sign-in. The CLI, holding the key, asks POST /api/auth/links for a single-use
  code and opens the link it returns. The page trades the code at POST
  /api/auth/sessions for a session token and presents that token from then on,
  so the browser never holds the key. DELETE /api/auth/sessions signs out and
  GET /api/auth/whoami says who the page is.
* Distribution. GET /install.py is the installer with this stack's own address
  baked in, GET /dist/threefold-bundle.zip is the hook, the CLI and the domain
  package it needs, and GET /dist/manifest.json carries the hashes of both so
  the installer can check what it downloaded. GET /dashboard.html, GET /app and
  GET /assets/<file> serve the application itself.

The access rules for every route here are decided by security_middleware
before `handle` is called; nothing in this module grants access.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from threefold.application.dtos import InvalidRequestError
from threefold.infrastructure import auth_store
from threefold.infrastructure.security_middleware import (
    ASSETS_PREFIX,
    AUTH_LINKS_PATH,
    AUTH_SESSIONS_PATH,
    AUTH_WHOAMI_PATH,
    VIEWER_HOST_HEADER,
    configured_key_refs,
    edge_request_is_trusted,
    operator_identity,
    presented_bearer,
    presented_key_ref,
    reads_are_public,
    rfc7807_error,
)

logger = logging.getLogger("threefold.access")

# Every root is read when a request is served rather than bound at import, so a
# test can point one at a temporary directory with monkeypatch.
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_ROOT = os.path.join(PACKAGE_ROOT, "web")
ASSETS_ROOT = os.path.join(WEB_ROOT, "assets")
HOOKS_ROOT = os.path.join(PACKAGE_ROOT, "hooks")
TOOLS_ROOT = os.path.join(PACKAGE_ROOT, "tools")

DASHBOARD_PATHS = ("/dashboard.html", "/app")
DASHBOARD_FILE = "dashboard.html"
INSTALLER_PATH = "/install.py"
INSTALLER_FILE = "threefold_install.py"
BUNDLE_PATH = "/dist/threefold-bundle.zip"
BUNDLE_FILE = "threefold-bundle.zip"
MANIFEST_PATH = "/dist/manifest.json"

ENDPOINT_TOKEN = "__THREEFOLD_ENDPOINT__"
BASE_PATH_TOKEN = "__THREEFOLD_BASE_PATH__"
# The hash of exactly the bytes in the body, so `curl ... | sha256sum` can be
# compared with it and with the manifest without trusting either alone.
SHA256_HEADER = "X-Threefold-SHA256"

# Where a sign-in link lands when the CLI names nowhere in particular.
DEFAULT_NEXT = "/overview"
MAX_NEXT_LENGTH = 256

# Asset names the route will look up: one file name, letters, digits, dashes
# and underscores, at most two inner dots, one of three extensions. No slash, no
# percent sign and no "..", so nothing outside web/assets/ can be named, before
# the resolved path is checked against the directory as well.
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}(?:\.[A-Za-z0-9_-]{1,32}){0,2}\.(js|css|svg)")
ASSET_TYPES = {
    "js": "text/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "svg": "image/svg+xml; charset=utf-8",
}

# A host as API Gateway or a local server reports it, optionally with a port.
# It is written into the installer's source and into sign-in links, so anything
# else is refused rather than escaped.
HOST_PATTERN = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]{2,45}\])(?::[0-9]{1,5})?"
)
STAGE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
# The name a viewer reached the edge by, as the edge's function copies it from
# Host: a DNS name of at least two labels whose last label starts with a
# letter, so no port, no IP address and no trailing dot. The edge serves only
# the default HTTPS port, so a name with a port is not one it would hand out.
VIEWER_HOST_PATTERN = re.compile(
    r"(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)

# One zip per container, keyed by the roots it was built from. Rebuilt only when
# a root changes, which in a deployment is never, so every download from one
# container is the same bytes and the manifest's hash stays true.
_BUNDLE_CACHE: Dict[Tuple[str, str, str], Tuple[bytes, List[Dict[str, Any]]]] = {}
# A fixed timestamp and fixed attributes for every entry. zipfile otherwise
# writes the current time and the build platform into each header, and the
# same files would zip to different bytes on every build.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_ZIP_FILE_MODE = 0o100644


class _MissingFromDeployment(Exception):
    """A file the route serves is not in the package this function was built from."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _api():
    """The handler module, for its response builders.

    Imported when a request is served rather than at import time: the handler
    imports this module, so a top-level import here would be circular.
    """
    from threefold.interfaces import api_handlers

    return api_handlers


def handle(path: str, method: str, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Answers the routes this module owns, or None for any other request."""
    verb = method.upper()
    if path == AUTH_LINKS_PATH and verb == "POST":
        return _mint_link(event)
    if path == AUTH_SESSIONS_PATH and verb == "POST":
        return _open_session(event)
    if path == AUTH_SESSIONS_PATH and verb == "DELETE":
        return _close_session(event)
    if path == AUTH_WHOAMI_PATH and verb == "GET":
        return _json(200, whoami(event.get("headers") or {}), private=True)
    if verb != "GET":
        return None
    if path in DASHBOARD_PATHS:
        return _dashboard(event)
    if path.startswith(ASSETS_PREFIX):
        return _asset(path[len(ASSETS_PREFIX):], event)
    if path == INSTALLER_PATH:
        return _installer(event)
    if path == BUNDLE_PATH:
        return _bundle(path)
    if path == MANIFEST_PATH:
        return _manifest(event)
    return None


# ---------------------------------------------------------------------- sign-in


def whoami(headers: Dict[str, Any]) -> Dict[str, Any]:
    """Who the request is, and what this stack lets anyone do.

    `sandbox_writes` is whether this stack lets anyone create and write sandbox
    projects, which is exactly when its reads are public.
    """
    identity = operator_identity(headers)
    return {
        "authenticated": identity is not None,
        "via": identity["via"] if identity else None,
        "expires_at": _iso(identity["expires_at"]) if identity and identity["expires_at"] else None,
        "reads_public": reads_are_public(),
        "sandbox_writes": reads_are_public(),
    }


def _mint_link(event: Dict[str, Any]) -> Dict[str, Any]:
    headers = event.get("headers") or {}
    ref = presented_key_ref(headers)
    if ref is None:
        # The middleware refuses anything but the key before this runs, so this
        # is a second lock on the same door rather than a path anyone reaches.
        return _problem(403, "Forbidden", "Minting a sign-in link requires the operator key.",
                        AUTH_LINKS_PATH, "urn:threefold:error:invalid-credentials")
    body = _api()._parse_body(event)
    target = body.get("next")
    target = DEFAULT_NEXT if target is None else _checked_next(target)
    base = public_base(event)
    if base is None:
        return _unknown_host(AUTH_LINKS_PATH)
    code, _ = auth_store.default_store().create_code(ref)
    return _json(
        200,
        {
            "code": code,
            "expires_in": auth_store.CODE_TTL_SECONDS,
            "url": f"{base}{DASHBOARD_FILE}#/signin?code={quote(code, safe='')}&next={quote(target, safe='')}",
        },
        private=True,
    )


def _checked_next(target: Any) -> str:
    """A place inside the dashboard to go after signing in, and nowhere else.

    The page navigates to it, so anything that could leave the page is refused:
    a scheme, a host ("//" or a backslash, which browsers read as a slash),
    whitespace, control characters, and a fragment of its own.
    """
    reason = "next must be a path inside the dashboard, such as /projects/Acme-Billing."
    if not isinstance(target, str) or not target.startswith("/") or len(target) > MAX_NEXT_LENGTH:
        raise InvalidRequestError(reason, "next")
    if "//" in target or "\\" in target or "#" in target:
        raise InvalidRequestError(reason, "next")
    if any(not ("!" <= ch <= "~") for ch in target):
        raise InvalidRequestError(reason, "next")
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        raise InvalidRequestError(reason, "next")
    return target


def _open_session(event: Dict[str, Any]) -> Dict[str, Any]:
    body = _api()._parse_body(event)
    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        raise InvalidRequestError("code is required: the one in the sign-in link.", "code")
    store = auth_store.default_store()
    record = store.consume_code(code.strip())
    # Checked after the code is spent, so a link minted by a key that has since
    # been removed is used up rather than left to be tried again.
    if record is None or record.get("key_ref") not in configured_key_refs():
        return _problem(
            401,
            "Sign-In Link Not Valid",
            "This sign-in link has been used, has expired, or was never issued. Ask the CLI for "
            "a new one; each link works once, for two minutes.",
            AUTH_SESSIONS_PATH,
            "urn:threefold:error:sign-in-code-invalid",
        )
    token, expires = store.create_session(record["key_ref"])
    return _json(
        200,
        {"token": token, "expires_at": _iso(expires), "ttl_seconds": auth_store.SESSION_TTL_SECONDS},
        private=True,
    )


def _close_session(event: Dict[str, Any]) -> Dict[str, Any]:
    """Revokes the presented session and answers with whoami as it now stands.

    Signing out of a session that is already gone, or presenting none, is not
    an error: the caller wanted to be signed out and is.
    """
    headers = event.get("headers") or {}
    token = presented_bearer(headers)
    if token and token.startswith(auth_store.SESSION_TOKEN_PREFIX):
        if not auth_store.default_store().revoke(token):
            return _problem(
                503,
                "Sign-Out Not Recorded",
                "The session was refused here at once, but the store did not confirm the "
                "sign-out, so it may be honoured elsewhere until it lapses. Try again.",
                AUTH_SESSIONS_PATH,
                "urn:threefold:error:sign-out-incomplete",
            )
    return _json(200, whoami(headers), private=True)


# ------------------------------------------------------------------ served files


def edge_base(event: Dict[str, Any]) -> Optional[str]:
    """https://<viewer host>/ when the request came through the edge, else None.

    Behind CloudFront the function sees the API's own host, because the edge
    replaces Host on the way to the origin, so an installer or a sign-in link
    built from it would send a reader past the edge, its web ACL included. The
    edge copies the viewer's Host into its own header, and that header is
    believed only on a request carrying the edge's secret: the API's URL is
    public, and anyone can send the header to it. No stage prefix, because the
    edge maps its root onto the stage.
    """
    headers = event.get("headers") or {}
    if not edge_request_is_trusted(headers):
        return None
    wanted = VIEWER_HOST_HEADER.lower()
    host = next((v for k, v in headers.items() if str(k).lower() == wanted), None)
    if not isinstance(host, str):
        return None
    host = host.strip().lower()
    if not VIEWER_HOST_PATTERN.fullmatch(host):
        return None
    return f"https://{host}/"


def public_base(event: Dict[str, Any]) -> Optional[str]:
    """This stack's own address, https://<host>/<stage>/, as the request reached it.

    Through the edge it is the edge's address instead (see edge_base), so the
    installer, the manifest's installer_sha256 and a sign-in link fetched there
    all point back at the edge. The manifest's installer hash therefore differs
    between the edge and the API's own URL, as the installers they serve do;
    the bundle and its hash are the same from both. Otherwise the host is API
    Gateway's own record of the domain when there is one, and the Host header
    otherwise, which is the local server's case; the local server speaks plain
    HTTP unless a proxy in front of it says otherwise. The stage prefix follows
    the rule the pages' base path follows. None when the host or stage is not
    one that can safely be written into a script.
    """
    through_edge = edge_base(event)
    if through_edge is not None:
        return through_edge
    context = event.get("requestContext") or {}
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    domain = context.get("domainName")
    host = domain or headers.get("host")
    if not isinstance(host, str) or not HOST_PATTERN.fullmatch(host):
        return None
    if domain:
        scheme = "https"
    else:
        forwarded = str(headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        scheme = forwarded if forwarded in ("http", "https") else "http"
    stage = context.get("stage") or ""
    if stage and stage != "$default":
        if not isinstance(stage, str) or not STAGE_PATTERN.fullmatch(stage):
            return None
        return f"{scheme}://{host}/{stage}/"
    return f"{scheme}://{host}/"


def _base_path(event: Dict[str, Any]) -> str:
    """The API base the pages are told about, as api_handlers tells the others."""
    stage = (event.get("requestContext") or {}).get("stage", "")
    return f"/{stage}" if stage and stage != "$default" else ""


def _dashboard(event: Dict[str, Any]) -> Dict[str, Any]:
    try:
        markup = _read_text(os.path.join(WEB_ROOT, DASHBOARD_FILE), f"web/{DASHBOARD_FILE}")
    except _MissingFromDeployment as missing:
        return _missing(missing.name, "Page Unavailable", "urn:threefold:error:page-missing", DASHBOARD_PATHS[0])
    return _api().build_html_response(200, markup.replace(BASE_PATH_TOKEN, _base_path(event)))


def _asset(name: str, event: Dict[str, Any]) -> Dict[str, Any]:
    matched = ASSET_NAME.fullmatch(name)
    root = os.path.realpath(ASSETS_ROOT)
    target = os.path.realpath(os.path.join(root, name)) if matched else ""
    if not matched or os.path.dirname(target) != root or not os.path.isfile(target):
        return _problem(
            404,
            "No Such Asset",
            "No asset of that name is served. Assets are single .js, .css or .svg files.",
            ASSETS_PREFIX + name,
            "urn:threefold:error:asset-not-found",
        )
    text = _read_text(target, f"web/assets/{name}")
    headers = _headers(ASSET_TYPES[matched.group(1)])
    return {"statusCode": 200, "headers": headers, "body": text.replace(BASE_PATH_TOKEN, _base_path(event))}


def _served_installer(event: Dict[str, Any]) -> Tuple[str, str]:
    """The installer as this stack serves it, and the SHA-256 of those bytes.

    Read as bytes and decoded, so line endings reach the reader as they are in
    the package and the hash is of what is actually sent.
    """
    source = _read_text(os.path.join(TOOLS_ROOT, INSTALLER_FILE), f"tools/{INSTALLER_FILE}")
    base = public_base(event)
    if base is None:
        raise _UnknownHost()
    served = source.replace(ENDPOINT_TOKEN, base)
    return served, hashlib.sha256(served.encode("utf-8")).hexdigest()


class _UnknownHost(Exception):
    pass


def _installer(event: Dict[str, Any]) -> Dict[str, Any]:
    try:
        served, digest = _served_installer(event)
    except _MissingFromDeployment as missing:
        return _missing(missing.name, "Installer Unavailable", "urn:threefold:error:script-missing", INSTALLER_PATH)
    except _UnknownHost:
        return _unknown_host(INSTALLER_PATH)
    response = _api().build_script_response(served, INSTALLER_FILE)
    response["headers"][SHA256_HEADER] = digest
    return response


def _bundle(path: str) -> Dict[str, Any]:
    try:
        archive, _ = _built_bundle()
    except _MissingFromDeployment as missing:
        return _missing(missing.name, "Bundle Unavailable", "urn:threefold:error:script-missing", path)
    return build_binary_response(archive, "application/zip", BUNDLE_FILE)


def _manifest(event: Dict[str, Any]) -> Dict[str, Any]:
    try:
        archive, files = _built_bundle()
        _, installer_digest = _served_installer(event)
    except _MissingFromDeployment as missing:
        return _missing(missing.name, "Manifest Unavailable", "urn:threefold:error:script-missing", MANIFEST_PATH)
    except _UnknownHost:
        return _unknown_host(MANIFEST_PATH)
    return _json(
        200,
        {
            "files": files,
            "bundle_sha256": hashlib.sha256(archive).hexdigest(),
            "installer_sha256": installer_digest,
        },
    )


def _bundle_sources() -> List[Tuple[str, str]]:
    """Each file the bundle carries, as (name in the zip, path in the package)."""
    domain_root = os.path.join(PACKAGE_ROOT, "domain")
    domain = (
        sorted(
            name
            for name in os.listdir(domain_root)
            if name.endswith(".py") and os.path.isfile(os.path.join(domain_root, name))
        )
        if os.path.isdir(domain_root)
        else []
    )
    if not domain:
        raise _MissingFromDeployment("domain/*.py")
    return [
        ("bin/threefold_hook.py", os.path.join(HOOKS_ROOT, "threefold_hook.py")),
        ("bin/threefold_cli.py", os.path.join(TOOLS_ROOT, "threefold_cli.py")),
        ("lib/threefold/__init__.py", os.path.join(PACKAGE_ROOT, "__init__.py")),
        *((f"lib/threefold/domain/{name}", os.path.join(domain_root, name)) for name in domain),
    ]


def _built_bundle() -> Tuple[bytes, List[Dict[str, Any]]]:
    """The zip and its file list, built once per container from the package itself."""
    key = (PACKAGE_ROOT, HOOKS_ROOT, TOOLS_ROOT)
    cached = _BUNDLE_CACHE.get(key)
    if cached is not None:
        return cached
    entries = []
    for name, source in _bundle_sources():
        if not os.path.isfile(source):
            raise _MissingFromDeployment(os.path.relpath(source, PACKAGE_ROOT).replace(os.sep, "/"))
        with open(source, "rb") as handle:
            entries.append((name, handle.read()))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = _ZIP_FILE_MODE << 16
            archive.writestr(info, data, compresslevel=9)
    files = [
        {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        for name, data in entries
    ]
    built = (buffer.getvalue(), files)
    _BUNDLE_CACHE[key] = built
    return built


# ---------------------------------------------------------------------- helpers


def build_binary_response(data: bytes, content_type: str, filename: str) -> Dict[str, Any]:
    """A download through the Lambda proxy, which carries bytes only as base64.

    isBase64Encoded tells API Gateway to decode the body before it is sent, so
    the client receives the file itself; without it the client would receive
    the base64 text. The hash header is of the decoded bytes.
    """
    headers = _headers(content_type)
    headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    headers[SHA256_HEADER] = hashlib.sha256(data).hexdigest()
    return {
        "statusCode": 200,
        "headers": headers,
        "body": base64.b64encode(data).decode("ascii"),
        "isBase64Encoded": True,
    }


def _headers(content_type: str) -> Dict[str, str]:
    headers = dict(_api().CORS_HEADERS)
    headers["Content-Type"] = content_type
    headers["Cache-Control"] = "no-cache"
    headers["X-Content-Type-Options"] = "nosniff"
    return headers


def _json(status: int, body: Dict[str, Any], private: bool = False) -> Dict[str, Any]:
    response = _api().build_response(status, body)
    if private:
        # A code or a token in a cached response is a credential in a cache.
        response["headers"]["Cache-Control"] = "no-store"
    return response


def _problem(status: int, title: str, detail: str, path: str, error_type: str) -> Dict[str, Any]:
    return _json(status, rfc7807_error(status, title, detail, path, error_type=error_type), private=True)


def _missing(name: str, title: str, error_type: str, path: str) -> Dict[str, Any]:
    logger.error("%s is missing from the deployment package", name)
    return _problem(500, title, f"{name} is missing from this deployment.", path, error_type)


def _unknown_host(path: str) -> Dict[str, Any]:
    return _problem(
        400,
        "Unknown Host",
        "The request does not say which host it reached, in a form that can be written into a "
        "script or a link, so this stack cannot point back at itself.",
        path,
        "urn:threefold:error:unknown-host",
    )


def _read_text(path: str, name: str) -> str:
    if not os.path.isfile(path):
        raise _MissingFromDeployment(name)
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8")


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
