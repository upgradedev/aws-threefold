"""Glob matching for the paths a rule applies to.

`fnmatch` is not enough: it has no `**`, so `src/**/domain/**` cannot be said,
and it is the one thing every architect writes first. This translates a glob to
an anchored regular expression once and caches it, because the match runs on
every tool call and compiling per request would put the cost inside the gate.

Matching is case-insensitive on purpose. The same layer is `domain` in a Python
tree, `Domain` in a C# one and `domain` again in Java, and a rule that silently
missed a whole codebase over a capital letter is the worst kind of wrong: it
reports zero and reads as safety.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable

MAX_PATTERN_LENGTH = 300


def normalise(path: str) -> str:
    """Windows separators, drive letters and leading ./ all become one shape."""
    if not path:
        return ""
    cleaned = path.replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern:
    """Translates a glob into an anchored expression.

    `**` crosses directories, `*` does not, `?` is one character. Everything else
    is literal. The result has no nested quantifiers over user input, so it
    cannot be made to backtrack.
    """
    out = ["^"]
    index = 0
    text = normalise(pattern)
    while index < len(text):
        char = text[index]
        if text.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif text.startswith("**", index):
            out.append(".*")
            index += 2
        elif char == "*":
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(char))
            index += 1
    out.append("$")
    return re.compile("".join(out), re.IGNORECASE)


def matches(path: str, pattern: str) -> bool:
    """Whether one path is covered by one glob."""
    if not path or not pattern or len(pattern) > MAX_PATTERN_LENGTH:
        return False
    return bool(_compiled(pattern).match(normalise(path)))


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(matches(path, pattern) for pattern in patterns or ())
