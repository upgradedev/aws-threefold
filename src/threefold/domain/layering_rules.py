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
from threefold.domain.path_match import MAX_PATTERN_LENGTH, matches_any, normalise

MAX_RULES = 50
MAX_PATTERNS_PER_RULE = 40

# A rule either refuses or watches. Watching is how an architect introduces a
# rule to ten teams without breaking any of them on the first morning: the
# violation is recorded and the call goes through, and a week of the console
# shows what it would have stopped before anyone is stopped.
#
# The default is to refuse. Defaulting to watch would have quietly turned every
# rule already saved, and every shipped rule, into a suggestion — the same kind
# of silent weakening an earlier review caught in this module.
ENFORCE = "enforce"
OBSERVE = "observe"
MODES = (ENFORCE, OBSERVE)

# What ships, and what a deployment enforces until an architect replaces it.
# The first rule is the behaviour this product had before rules existed, kept so
# that turning the format on changes nothing until someone means it to. The rest
# are the same idea said in Java, C# and TypeScript.
DEFAULT_RULES: List[Dict[str, Any]] = [
    {
        "id": "python-domain-stays-pure",
        "mode": ENFORCE,
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
        "mode": ENFORCE,
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
        "mode": ENFORCE,
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
        "mode": ENFORCE,
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


def _pattern_list(entry: Dict[str, Any], field: str, required: bool) -> Tuple[List[str], str]:
    """A field that must be a list of non-empty strings, or the reason it is not.

    Iterating whatever arrived was how a string became a list of one-character
    globs and a number became a 500: `"**/domain/**"` sent without brackets was
    saved as fourteen patterns, one of them `*`.
    """
    value = entry.get(field)
    if value is None or value == []:
        return [], (f"{field} is empty" if required else "")
    if not isinstance(value, list):
        return [], f"{field} must be a list of strings, not {type(value).__name__}"
    if any(not isinstance(item, str) or not item.strip() for item in value):
        return [], f"{field} may contain only non-empty strings"
    if len(value) > MAX_PATTERNS_PER_RULE:
        # Refused rather than truncated: keeping the first forty would narrow
        # the rule without saying which prohibitions were lost.
        return [], f"{field} has {len(value)} patterns; the limit is {MAX_PATTERNS_PER_RULE}"
    too_long = [item for item in value if len(item) > MAX_PATTERN_LENGTH]
    if too_long:
        return [], f"{field} has a pattern longer than {MAX_PATTERN_LENGTH} characters"
    # `**` has one meaning, a whole segment standing for any number of folders.
    # Written inside a segment, as `src**`, the first matcher let it cross
    # folders and the segment matcher that replaced it could not without
    # guessing, so it is refused with the way to say it instead of being
    # quietly read as something narrower.
    separators = "/." if field != "when_path_matches" else "/"
    for item in value:
        segments = [item]
        for separator in separators:
            segments = [piece for segment in segments for piece in segment.split(separator)]
        if any("**" in segment and segment != "**" for segment in segments):
            return [], (
                f"{field} pattern {item!r} uses ** inside a segment; ** must stand alone "
                "between separators, as in src/** or **/domain/**"
            )
    return [item.strip() for item in value], ""


def validate_rules(raw: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Splits what a caller sent into rules that can be evaluated and the rest.

    Returns (usable, problems). Each problem names the rule by position and id
    and says why it cannot be used, so a save can be refused with the reason
    rather than accepted with a rule quietly missing.
    """
    if isinstance(raw, dict):
        raw = raw.get("rules")
    if not isinstance(raw, list):
        return [], [{"index": None, "id": None, "reason": "Expected a list of rules, or {\"rules\": [...]}"}]

    usable: List[Dict[str, Any]] = []
    problems: List[Dict[str, Any]] = []
    seen_ids = set()
    for index, entry in enumerate(raw):
        label = entry.get("id") if isinstance(entry, dict) and isinstance(entry.get("id"), str) else None

        def refuse(reason: str) -> None:
            problems.append({"index": index, "id": label, "reason": reason})

        if index >= MAX_RULES:
            refuse(f"More than {MAX_RULES} rules")
            continue
        if not isinstance(entry, dict):
            refuse("A rule must be an object")
            continue
        paths, why = _pattern_list(entry, "when_path_matches", required=True)
        if why:
            refuse(why)
            continue
        forbid, why = _pattern_list(entry, "forbid_imports", required=True)
        if why:
            refuse(why)
            continue
        allow, why = _pattern_list(entry, "allow_imports", required=False)
        if why:
            refuse(why)
            continue
        raw_mode = entry.get("mode")
        if raw_mode is None:
            mode = ENFORCE
        elif isinstance(raw_mode, str) and raw_mode.strip().lower() in MODES:
            mode = raw_mode.strip().lower()
        else:
            # A typo such as "observ" must not decide whether a rule refuses,
            # and neither may an empty string or a false: only a missing mode
            # means the default.
            refuse(f"mode must be \"enforce\" or \"observe\", not {raw_mode!r}")
            continue
        rule_id = str(entry.get("id") or f"rule-{len(usable) + 1}")[:80]
        if rule_id in seen_ids:
            refuse(f"id {rule_id!r} is used by an earlier rule, and a refusal must name one rule")
            continue
        seen_ids.add(rule_id)
        usable.append(
            {
                "id": rule_id,
                "description": str(entry.get("description") or "")[:240],
                "mode": mode,
                "when_path_matches": paths,
                "forbid_imports": forbid,
                "allow_imports": allow,
            }
        )
    return usable, problems


def normalise_rules(raw: Any) -> List[Dict[str, Any]]:
    """The usable rules only, for rules already stored.

    A save goes through validate_rules and is refused whole if anything is
    wrong. This lenient form is for reading back what was stored before that
    check existed, where refusing to load would remove the gate entirely.
    """
    return validate_rules(raw)[0]


def rules_for_path(path: str, rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every rule that covers this path. All of them apply; none overrides another."""
    return [rule for rule in rules if matches_any(path, rule["when_path_matches"])]


def violations(path: str, content: str, rules: List[Dict[str, Any]]) -> Tuple[List[Dict[str, str]], str]:
    """Every rule this write would break, with the reason for each.

    Returns the list and a note for the case where there is none, so a caller can
    say why nothing was found: no rule covers the path, the language cannot be
    read, or the imports were checked and are fine.
    """
    applicable = rules_for_path(path, rules)
    if not applicable:
        return [], "No layering rule covers this path"

    language = language_for(path)
    if not language:
        return [], (
            f"{len(applicable)} rule(s) cover this path, but the file type is one "
            "Threefold does not read, so no import could be checked"
        )

    _, modules = declared_imports(path, content)
    if not modules:
        return [], f"No import declared in this {language} file"

    found: List[Dict[str, str]] = []
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
            description = rule.get("description") or rule["id"]
            verb = "refuses this write" if rule.get("mode", ENFORCE) == ENFORCE else "would refuse this write"
            found.append(
                {
                    "rule_id": rule["id"],
                    "mode": rule.get("mode", ENFORCE),
                    "module": module,
                    "pattern": offending,
                    "reason": (
                        f"Layering rule '{rule['id']}' {verb}: {description}. "
                        f"'{path}' imports '{module}', which matches '{offending}'"
                    ),
                }
            )
            break  # one finding per rule is enough to act on
    return found, f"Checked {len(modules)} import(s) against {len(applicable)} rule(s)"


def evaluate(path: str, content: str, rules: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """Judges one intended write against the rules that refuse. (allowed, reason)

    Rules in observe mode never refuse; they are reported by `observed()`. A file
    whose language this module cannot read is allowed and the caller is told
    why, because refusing what cannot be read would block every file type nobody
    has taught it yet.
    """
    found, note = violations(path, content, rules)
    enforced = [item for item in found if item["mode"] == ENFORCE]
    if enforced:
        return False, enforced[0]["reason"]
    return True, note


def observed(path: str, content: str, rules: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """What the rules in observe mode would have refused. Never blocks anything."""
    found, _ = violations(path, content, rules)
    return [item for item in found if item["mode"] == OBSERVE]
