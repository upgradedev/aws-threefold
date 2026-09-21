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
# `[^'"]` rather than `[^'"\n]`: most formatters wrap a long
# import across lines, and a pattern anchored to one line found nothing in the
# most common formatting there is. Bounded so it cannot run away over a file.
_TS_FROM = re.compile(r"""(?:^|[\s;])(?:import|export)\b[^'"]{0,300}?from\s*['"](?P<module>[^'"\n]{1,200})['"]""", re.MULTILINE)
_TS_BARE = re.compile(r"""(?:^|[\s;])import\s*['"](?P<module>[^'"\n]{1,200})['"]""", re.MULTILINE)
_TS_CALL = re.compile(r"""(?:require|import)\s*\(\s*['"](?P<module>[^'"\n]{1,200})['"]\s*\)""")



# Comments are stripped before the patterns run. A commented-out import is not a
# dependency, and refusing one is the kind of false refusal that gets a guard
# uninstalled: the developer can see with their own eyes that the line is dead.
#
# Block comments are removed by a scan rather than by `/\*.*?\*/`: with no
# closing marker that expression restarts at every `/*` and runs to the end of
# the content each time, which a review measured at 7 seconds for 60 KB of
# repeated openers sent to the open /rules/explain route.
# Not preceded by a colon: stripping `//` blindly ate the scheme out of
# `import x from "https://cdn/mod.js"`, which made a real dependency invisible.
_LINE_COMMENT = re.compile(r"(?m)(?<!:)//.*$")
_HASH_COMMENT = re.compile(r"(?m)#.*$")


def _without_block_comments(content: str) -> str:
    """Drops every `/* ... */`, in one pass. An unclosed one runs to the end."""
    kept = []
    position = 0
    while True:
        start = content.find("/*", position)
        if start == -1:
            kept.append(content[position:])
            break
        kept.append(content[position:start])
        end = content.find("*/", start + 2)
        if end == -1:
            break
        position = end + 2
    return "".join(kept)


def _without_comments(content: str, language: str) -> str:
    if language == "python":
        return _HASH_COMMENT.sub("", content)
    return _LINE_COMMENT.sub("", _without_block_comments(content))


def language_for(path: str) -> str:
    """Names the reader a path gets, or an empty string when none does."""
    lowered = (path or "").lower()
    for suffix, language in LANGUAGE_BY_SUFFIX.items():
        if lowered.endswith(suffix):
            return language
    return ""


# Above this size Python content is read by the line reader instead of parsed.
# A review sent a 1 MB list literal and `ast.parse` peaked at 747 MB, three times
# the function's memory, and deeply nested content raised RecursionError, which
# is not a SyntaxError and escaped as a 500. A source file this large is rare,
# and the line reader still sees every import statement in it.
PYTHON_PARSE_LIMIT = 100_000


def _python_line_imports(content: str) -> List[str]:
    return [
        (match.group("from") or match.group("import") or "").lstrip(".")
        for match in _PYTHON_FALLBACK.finditer(_without_comments(content, "python"))
    ]


def _python_imports(content: str) -> List[str]:
    if len(content) > PYTHON_PARSE_LIMIT:
        return _python_line_imports(content)
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # Content arrives mid-edit and is often not valid Python. Reading it by
        # line rather than failing is what keeps a half-written file judged.
        return _python_line_imports(content)

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
        # ast ignores comments already; the fallback reader below does not.
        return language, _python_imports(content)

    readable = _without_comments(content, language)
    if language == "java":
        return language, _matches(_JAVA, readable)
    if language == "csharp":
        return language, _matches(_CSHARP, readable)
    if language == "typescript":
        modules = _matches(_TS_FROM, readable) + _matches(_TS_BARE, readable) + _matches(_TS_CALL, readable)
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
