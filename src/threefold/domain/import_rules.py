"""The one rule Threefold enforces that nothing else does.

A file under a `domain/` directory may not import the outside world. Linters
check this after the fact, once the code is already written and committed. This
module lets the check run at the moment an agent asks to write the file, which
is the only moment the edit can still be refused.

The same function backs the live API gate and the pre-commit script, so the
perimeter cannot drift into being weaker than the repository's own checker.
"""
from __future__ import annotations

import ast
import re
from typing import List, Tuple

FORBIDDEN_ROOTS: Tuple[str, ...] = (
    "boto3",
    "botocore",
    "requests",
    "httpx",
    "urllib3",
    "fastapi",
    "flask",
    "django",
    "sqlalchemy",
    "psycopg2",
    "pymongo",
    "redis",
)

FORBIDDEN_PACKAGE_PARTS: Tuple[str, ...] = ("infrastructure", "adapters")

# Used only when the content does not parse. An agent halfway through an edit
# often writes invalid Python, and treating that as permission would be a hole.
_FALLBACK = re.compile(
    r"^\s*(?:from\s+(?P<from>[.\w]+)\s+import\b|import\s+(?P<import>[.\w]+))",
    re.MULTILINE,
)


def _offending_module(module: str) -> str | None:
    """Returns the part of a module path that breaks the rule, or None."""
    if not module:
        return None
    parts = module.split(".")
    for part in parts:
        if part in FORBIDDEN_PACKAGE_PARTS:
            return part
    root = parts[0]
    if root in FORBIDDEN_ROOTS:
        return root
    return None


def find_forbidden_imports(content: str) -> List[str]:
    """Names every import in `content` that a domain file may not make.

    Parsing is what makes this honest. Searching the text for "import boto3"
    misses `from boto3 import client`, and it also fires on a comment that
    merely mentions the rule, which is how the earlier version came to reject
    its own source.
    """
    if not content or not content.strip():
        return []

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return _find_by_regex(content)

    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _offending_module(alias.name):
                    violations.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            # A bare `from . import x` carries no module name; the level is the
            # relative depth and tells us nothing about what is being reached.
            if _offending_module(module):
                violations.append(f"from {module} import ...")
    return violations


def _find_by_regex(content: str) -> List[str]:
    """Line-oriented fallback for content Python cannot parse."""
    violations: List[str] = []
    for match in _FALLBACK.finditer(content):
        module = match.group("from") or match.group("import") or ""
        module = module.lstrip(".")
        if _offending_module(module):
            violations.append(match.group(0).strip())
    return violations
