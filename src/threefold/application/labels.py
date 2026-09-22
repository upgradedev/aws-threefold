"""The two identity fields a public page shows, reduced to what may be shown.

A project name and a developer name are both request-controlled strings that
end up on pages anyone can open and, for the project, as a CloudWatch metric
dimension. Left raw, the first leaks whatever a caller types (a real repository
name, a customer) and lets a caller mint a new metric per request; the second
names a person. So a project is kept only when it matches the stack's
AllowedProjectPattern and is otherwise "unlabelled", and a developer is shown
only as a short hash. Both are applied on the way out as well as on the way in,
because rows written before this rule existed are still in the table.
"""
from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
from typing import Any, Dict, Optional, Pattern, Tuple

logger = logging.getLogger(__name__)

# The same default the template's AllowedProjectPattern parameter carries, so a
# local run and a stack deployed with no override label projects identically.
DEFAULT_PROJECT_PATTERN = r"^Acme-[A-Za-z0-9-]{1,40}$"
PROJECT_PATTERN_ENV = "ALLOWED_PROJECT_PATTERN"
UNLABELLED = "unlabelled"

# Longer than any name the default pattern admits. A name past this is refused
# before the pattern runs, so an operator's own pattern is never matched against
# a megabyte of caller input.
MAX_PROJECT_NAME_LENGTH = 120

DEVELOPER_HASH_LENGTH = 8


@functools.lru_cache(maxsize=8)
def _compile(pattern: str) -> Optional[Pattern[str]]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        # Logged once per pattern, because the cache remembers the failure.
        logger.warning(
            "%s=%r is not a valid regular expression (%s); labelling with the default instead",
            PROJECT_PATTERN_ENV,
            pattern,
            exc,
        )
        return None


def allowed_project_pattern() -> Pattern[str]:
    """The pattern in force, read from the environment on every call.

    Read per call rather than at import, because the handler module is imported
    once per container and a test, or an operator reading this code, expects the
    variable the function runs with to be the one that applies. A pattern that
    does not compile falls back to the default rather than to "allow anything".
    """
    configured = os.environ.get(PROJECT_PATTERN_ENV) or DEFAULT_PROJECT_PATTERN
    return _compile(configured) or _compile(DEFAULT_PROJECT_PATTERN)


def is_labelled(project: Any) -> bool:
    """True when the name may be stored, counted and shown as it is."""
    if not isinstance(project, str) or not project or len(project) > MAX_PROJECT_NAME_LENGTH:
        return False
    # fullmatch, so a pattern written without anchors still has to match the
    # whole name, and a trailing newline cannot slip past a `$`.
    return allowed_project_pattern().fullmatch(project) is not None


def project_label(project: Any) -> str:
    """The name a page or a metric may carry for this project."""
    return project if is_labelled(project) else UNLABELLED


def label_project(project: Any) -> Tuple[str, Optional[str]]:
    """The name to store for an incoming call, and a warning when it was replaced.

    The caller's own value is not echoed in the warning: it is the caller's to
    begin with, and a response that repeats input verbatim is one more place for
    it to be logged.
    """
    if project is None or project == "":
        return UNLABELLED, (
            "project_name was not sent, so this call was recorded, counted and reported "
            f"as '{UNLABELLED}'."
        )
    if is_labelled(project):
        return project, None
    return UNLABELLED, (
        "project_name does not match this deployment's AllowedProjectPattern, so this "
        f"call was recorded, counted and reported as '{UNLABELLED}'."
    )


def developer_hash(developer: Any) -> str:
    """A short, stable stand-in for a developer value, never the value itself.

    Every value is hashed, "anonymous" included, so a reader cannot tell a hash
    a hook computed from a name an old caller sent. Empty stays empty, so a row
    with no developer still reads as unattributed rather than as a person.
    """
    if developer is None or developer == "":
        return ""
    return hashlib.sha256(str(developer).encode("utf-8")).hexdigest()[:DEVELOPER_HASH_LENGTH]


def public_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """A ledger or session row as an open page may show it.

    A copy, so the rows the caller holds are not rewritten underneath it.

    Of a suggested fix, a row carries `suggested_fix_kind` and
    `suggested_fix_validated` and they pass as they are. The fix itself never
    does: its writes are the caller's own source, and on a stack with
    PublicReads=true this is what anyone reads. The ledger never stores it, so
    the line below should never find one; it is here so a row that somehow
    held one still does not reach an open page.
    """
    shown = dict(row)
    shown.pop("suggested_fix", None)
    if "project_name" in shown:
        shown["project_name"] = project_label(shown.get("project_name"))
    if "developer_id" in shown:
        shown["developer_id"] = developer_hash(shown.get("developer_id"))
    return shown
