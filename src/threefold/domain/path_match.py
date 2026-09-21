"""Glob matching for the paths a rule applies to.

`fnmatch` is not enough: it has no `**`, so `src/**/domain/**` cannot be said,
and it is the one thing every architect writes first.

Matching is segment by segment rather than through a regular expression. The
first version translated a glob into one anchored expression, and each `**/`
became `(?:.*/)?`. Stacked, those backtrack as the path length to the power of
their count: a review sent seven of them in a 254-byte request to the open
`/rules/explain` route and held a worker for 57 seconds, and even the shipped
`**/domain/**/*.py` was quadratic on a long path. Here `**` is a step over
whole segments in a table of (pattern segment, path segment) states, each state
is decided once, and a segment is matched by a two-pointer walk that never
revisits more than it has to. The cost is bounded by the product of the two
lengths whatever the pattern says.

Matching is case-insensitive on purpose. The same layer is `domain` in a Python
tree, `Domain` in a C# one and `domain` again in Java, and a rule that silently
missed a whole codebase over a capital letter is the worst kind of wrong: it
reports zero and reads as safety.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Iterable, Tuple

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
def _segments(pattern: str) -> Tuple[str, ...]:
    """Splits a glob into segments, lower-cased, with runs of `**` collapsed.

    `**` crosses directories, including none. `*` and `?` stay inside one
    segment. A `**` written inside a longer segment, such as `src**`, cannot
    cross a directory and is read as `*`. A trailing `/**` therefore means "and
    anything below, including nothing", which is what every architect reads it
    as: `**/infrastructure/**` covers `myapp/infrastructure` itself.
    """
    out = []
    for segment in normalise(pattern).lower().split("/"):
        if segment == "**":
            if out and out[-1] == "**":
                continue
            out.append("**")
        elif segment:
            while "**" in segment:
                segment = segment.replace("**", "*")
            out.append(segment)
    return tuple(out)


def _segment_matches(text: str, pattern: str) -> bool:
    """`*` and `?` inside one segment, by the classic two-pointer walk.

    On a mismatch it resumes just after the most recent `*`, so it is linear in
    practice and at worst the product of the two lengths. It never recurses.
    """
    t = p = 0
    star = -1
    resume = 0
    while t < len(text):
        if p < len(pattern) and (pattern[p] == "?" or pattern[p] == text[t]):
            t += 1
            p += 1
        elif p < len(pattern) and pattern[p] == "*":
            star = p
            resume = t
            p += 1
        elif star != -1:
            p = star + 1
            resume += 1
            t = resume
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def _matches_segments(path_segments: Tuple[str, ...], pattern_segments: Tuple[str, ...]) -> bool:
    """Whether the path's segments are covered, as a table over both positions.

    reachable[j] says the first i pattern segments can consume exactly the
    first j path segments. Each row is computed once from the previous one, so
    no combination is ever tried twice.
    """
    width = len(path_segments)
    reachable = [False] * (width + 1)
    reachable[0] = True
    for segment in pattern_segments:
        following = [False] * (width + 1)
        if segment == "**":
            seen = False
            for j in range(width + 1):
                seen = seen or reachable[j]
                following[j] = seen
        else:
            for j in range(width):
                if reachable[j] and _segment_matches(path_segments[j], segment):
                    following[j + 1] = True
        reachable = following
        if not any(reachable):
            return False
    return reachable[width]


def matches(path: str, pattern: str) -> bool:
    """Whether one path is covered by one glob."""
    if not path or not pattern or len(pattern) > MAX_PATTERN_LENGTH:
        return False
    path_segments = tuple(seg for seg in normalise(path).lower().split("/") if seg)
    if not path_segments:
        return False
    return _matches_segments(path_segments, _segments(pattern))


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(matches(path, pattern) for pattern in patterns or ())
