"""The layering rules, declared rather than compiled in.

The gate that has no incumbent used to be one sentence of Python: a file under a
directory called domain/ may not import one of twelve library names. That rule
enforced nothing on an estate written in Java, C# and TypeScript, which is most
estates, and it could not be changed without a deployment.

A rule here says three things: which files it covers, what they may not depend
on, and what is allowed anyway. Nothing else. The deliberate omissions are in
`UNSUPPORTED` at the bottom of this module, because a format that quietly does
less than a reader assumes is worse than one that says no.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from threefold.domain.imports import declared_imports, language_for
from threefold.domain.path_match import matches_any, normalise

MAX_RULES = 50
MAX_PATTERNS_PER_RULE = 40

# What ships, and what a deployment enforces until an architect replaces it.
# The first rule is the behaviour this product had before rules existed, kept so
# that turning the format on changes nothing until someone means it to. The rest
# are the same idea said in Java, C# and TypeScript.
DEFAULT_RULES: List[Dict[str, Any]] = [
    {
        "id": "python-domain-stays-pure",
        "description": "A Python file under domain/ may not import infrastructure or a driver",
        "when_path_matches": ["**/domain/**/*.py", "**/domain/**/*.pyi"],
        "forbid_imports": [
            "boto3", "botocore", "requests", "httpx", "urllib3",
            "fastapi", "flask", "django", "sqlalchemy", "psycopg2", "pymongo", "redis",
            "**.infrastructure.**", "**.adapters.**",
        ],
        "allow_imports": [],
    },
    {
        "id": "java-domain-stays-pure",
        "description": "A Java class under domain/ may not reach persistence, HTTP or the container",
        "when_path_matches": ["**/domain/**/*.java"],
        "forbid_imports": [
            "java.sql", "javax.sql", "javax.persistence", "jakarta.persistence",
            "javax.ejb", "jakarta.ejb", "javax.ws.rs", "jakarta.ws.rs",
            "org.springframework", "org.hibernate", "oracle.jdbc",
            "**.infrastructure.**", "**.adapters.**",
        ],
        "allow_imports": ["java.util", "java.time", "java.math", "java.lang"],
    },
    {
        "id": "dotnet-domain-stays-pure",
        "description": "A C# type under Domain/ may not reach ADO.NET, EF or the web stack",
        "when_path_matches": ["**/Domain/**/*.cs"],
        "forbid_imports": [
            "System.Data", "Microsoft.EntityFrameworkCore", "Microsoft.AspNetCore",
            "Dapper", "Oracle.ManagedDataAccess", "System.Net.Http",
            "**.Infrastructure.**", "**.Adapters.**",
        ],
        "allow_imports": ["System", "System.Collections", "System.Linq", "System.Text"],
    },
    {
        "id": "web-domain-stays-pure",
        "description": "A TypeScript module under domain/ may not import a client or a framework",
        "when_path_matches": ["**/domain/**/*.ts", "**/domain/**/*.tsx"],
        "forbid_imports": [
            "axios", "node-fetch", "@aws-sdk/*", "react", "react-dom", "next/*",
            "@angular/*", "typeorm", "prisma", "@prisma/*",
            "**/infrastructure/*", "**/adapters/*",
        ],
        "allow_imports": [],
    },
]

UNSUPPORTED = (
    "A rule reads the file's own import statements and its path. It does not "
    "resolve an import to a file, follow it transitively, or know that a class "
    "in an allowed package wraps a forbidden one. It does not read build files, "
    "module systems or dependency injection wiring, so a dependency delivered by "
    "a container rather than an import is invisible to it."
)


def _module_segments(module: str) -> str:
    """One shape for a dotted package and a slashed module path."""
    return normalise((module or "").replace(".", "/"))


def _forbidden_by(module: str, patterns: Iterable[str]) -> Optional[str]:
    """Returns the pattern that catches this module, or None.

    A pattern without a wildcard matches the module or anything under it, on a
    segment boundary: `System.Data` catches `System.Data.SqlClient` and leaves
    `System.DataAnnotations` alone, and `react` never catches `reactive-forms`.
    A pattern with a wildcard is a glob over the same segments.
    """
    candidate = _module_segments(module)
    if not candidate:
        return None
    for pattern in patterns or ():
        if not pattern:
            continue
        if "*" in pattern or "?" in pattern:
            if matches_any(candidate, [_module_segments(pattern)]):
                return pattern
        else:
            target = _module_segments(pattern)
            if candidate == target or candidate.lower().startswith(target.lower() + "/"):
                return pattern
    return None



def _specificity(pattern: str) -> Tuple[int, int]:
    """How precisely a pattern names something: deeper first, wildcards last."""
    segments = [seg for seg in _module_segments(pattern).split("/") if seg]
    wildcards = sum(1 for seg in segments if "*" in seg or "?" in seg)
    return (len(segments) - wildcards, -wildcards)


def normalise_rules(raw: Any) -> List[Dict[str, Any]]:
    """Accepts what a caller sent and returns rules this module can evaluate.

    Anything malformed is dropped rather than half-applied: a rule that was
    saved wrong must not silently widen or narrow what is enforced.
    """
    if isinstance(raw, dict):
        raw = raw.get("rules")
    if not isinstance(raw, list):
        return []

    cleaned: List[Dict[str, Any]] = []
    for entry in raw[:MAX_RULES]:
        if not isinstance(entry, dict):
            continue
        paths = [p for p in (entry.get("when_path_matches") or []) if isinstance(p, str) and p.strip()]
        forbid = [p for p in (entry.get("forbid_imports") or []) if isinstance(p, str) and p.strip()]
        if not paths or not forbid:
            continue
        cleaned.append(
            {
                "id": str(entry.get("id") or f"rule-{len(cleaned) + 1}")[:80],
                "description": str(entry.get("description") or "")[:240],
                "when_path_matches": paths[:MAX_PATTERNS_PER_RULE],
                "forbid_imports": forbid[:MAX_PATTERNS_PER_RULE],
                "allow_imports": [
                    p for p in (entry.get("allow_imports") or []) if isinstance(p, str) and p.strip()
                ][:MAX_PATTERNS_PER_RULE],
            }
        )
    return cleaned


def rules_for_path(path: str, rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every rule that covers this path. All of them apply; none overrides another."""
    return [rule for rule in rules if matches_any(path, rule["when_path_matches"])]


def evaluate(path: str, content: str, rules: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """Judges one intended write. Returns (allowed, reason).

    An allowance beats a prohibition inside the same rule, so a team can forbid a
    whole namespace and keep the two packages they need. A file whose language
    this module cannot read is allowed and the caller is told why, because
    refusing what cannot be read would block every file type nobody has taught
    it yet.
    """
    applicable = rules_for_path(path, rules)
    if not applicable:
        return True, "No layering rule covers this path"

    language = language_for(path)
    if not language:
        return True, (
            f"{len(applicable)} rule(s) cover this path, but the file type is one "
            "Threefold does not read, so no import could be checked"
        )

    _, modules = declared_imports(path, content)
    if not modules:
        return True, f"No import declared in this {language} file"

    for rule in applicable:
        for module in modules:
            offending = _forbidden_by(module, rule["forbid_imports"])
            if not offending:
                continue
            permitted = _forbidden_by(module, rule.get("allow_imports"))
            # The more specific pattern decides, not the order they were written
            # in. Allowing `System` while forbidding `System.Data` has to leave
            # System.Data.SqlClient refused, or a broad allowance quietly repeals
            # every narrower prohibition under it.
            if permitted and _specificity(permitted) > _specificity(offending):
                continue
            if offending:
                description = rule.get("description") or rule["id"]
                return False, (
                    f"Layering rule '{rule['id']}' refuses this write: {description}. "
                    f"'{path}' imports '{module}', which matches '{offending}'"
                )

    return True, f"Checked {len(modules)} import(s) against {len(applicable)} rule(s)"


def describe(rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What the console prints beside the counts, computed from the rules in force."""
    languages = sorted({
        suffix
        for rule in rules
        for pattern in rule["when_path_matches"]
        for suffix in (language_for(pattern),)
        if suffix
    })
    return {
        "rule_count": len(rules),
        "rule_ids": [rule["id"] for rule in rules],
        "languages_targeted": languages or ["any file type these paths match"],
        "unsupported": UNSUPPORTED,
    }
