"""Reads the dependencies a file declares, in the four languages the layering rules cover.

Python is parsed. Java, C# and TypeScript are read with line-oriented patterns
over their import syntax, which is weaker and is said so wherever the result is
reported: this module knows what a file says it imports, not what it means.

Two properties matter more than completeness here. The content arrives from an
agent halfway through an edit, so it is often not valid in any language and
parsing must degrade to the line reader rather than fail open. And the patterns
run on every tool call inside a Lambda, so each is anchored and linear: nothing
here backtracks over attacker-supplied content.
"""
from __future__ import annotations

import ast
import re
from typing import List, Tuple

# Which reader a file gets, by extension. A file this module does not recognise
# yields no imports, and a rule that would have matched it is reported as
# unevaluated rather than as passed.
LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".java": "java",
    ".cs": "csharp",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".mjs": "typescript",
}

_PYTHON_FALLBACK = re.compile(
    r"^[ \t]*(?:from[ \t]+(?P<from>[.\w]+)[ \t]+import\b|import[ \t]+(?P<import>[.\w]+))",
    re.MULTILINE,
)

# `import java.sql.Connection;` and `import static org.junit.Assert.*;`
_JAVA = re.compile(r"^[ \t]*import[ \t]+(?:static[ \t]+)?(?P<module>[\w.]+)[ \t]*(?:\*[ \t]*)?;", re.MULTILINE)

# `using System.Data;` and `using Dapper = Some.Thing;`, but not `using (var x = ...)`
_CSHARP = re.compile(
    r"^[ \t]*(?:global[ \t]+)?using[ \t]+(?:static[ \t]+)?(?:[\w]+[ \t]*=[ \t]*)?(?P<module>[\w.]+)[ \t]*;",
    re.MULTILINE,
)

# `import x from 'y'`, `import 'y'`, `export * from 'y'`, `require('y')`, `import('y')`
_TS_FROM = re.compile(r"""(?:^|[\s;])(?:import|export)\b[^'"\n]{0,200}?from\s*['"](?P<module>[^'"\n]{1,200})['"]""", re.MULTILINE)
_TS_BARE = re.compile(r"""(?:^|[\s;])import\s*['"](?P<module>[^'"\n]{1,200})['"]""", re.MULTILINE)
_TS_CALL = re.compile(r"""(?:require|import)\s*\(\s*['"](?P<module>[^'"\n]{1,200})['"]\s*\)""")


def language_for(path: str) -> str:
    """Names the reader a path gets, or an empty string when none does."""
    lowered = (path or "").lower()
    for suffix, language in LANGUAGE_BY_SUFFIX.items():
        if lowered.endswith(suffix):
            return language
    return ""


def _python_imports(content: str) -> List[str]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return [
            (match.group("from") or match.group("import") or "").lstrip(".")
            for match in _PYTHON_FALLBACK.finditer(content)
        ]

    modules: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A bare `from . import x` carries no module name: the level is the
            # relative depth and says nothing about what is being reached.
            if node.module:
                modules.append(node.module)
    return modules


def _matches(pattern: re.Pattern, content: str) -> List[str]:
    return [match.group("module") for match in pattern.finditer(content)]


def declared_imports(path: str, content: str) -> Tuple[str, List[str]]:
    """Returns the language and every module the content says it imports.

    An unrecognised extension returns an empty language, which callers must
    treat as "not evaluated" rather than as "nothing forbidden found".
    """
    language = language_for(path)
    if not language or not content or not content.strip():
        return language, []

    if language == "python":
        return language, _python_imports(content)
    if language == "java":
        return language, _matches(_JAVA, content)
    if language == "csharp":
        return language, _matches(_CSHARP, content)
    if language == "typescript":
        modules = _matches(_TS_FROM, content) + _matches(_TS_BARE, content) + _matches(_TS_CALL, content)
        # Order is stable and duplicates are dropped, so a reason string reads the
        # same way twice for the same file.
        seen = set()
        unique = []
        for module in modules:
            if module not in seen:
                seen.add(module)
                unique.append(module)
        return language, unique
    return language, []
