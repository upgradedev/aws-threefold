"""The layering rule stops being an example written in Python.

The one gate with no incumbent read Python files under a directory named
domain/ and refused twelve hardcoded library names. On a codebase in Java,
C# or TypeScript that enforced nothing at all, and it could not be changed
without a deployment. A rule now says which files it covers, what they may not
depend on, and what is allowed anyway.

Names here are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import pytest

from threefold.domain.imports import declared_imports
from threefold.domain.layering_rules import DEFAULT_RULES, evaluate, normalise_rules
from threefold.domain.path_match import matches

JAVA_DOMAIN = "src/main/java/com/acme/billing/domain/Order.java"
CSHARP_DOMAIN = "Acme.Billing/Domain/Invoice.cs"
TS_DOMAIN = "web/src/domain/price.ts"


@pytest.mark.parametrize(
    "path, content, language, expected",
    [
        (JAVA_DOMAIN, "import javax.persistence.Entity;", "java", ["javax.persistence.Entity"]),
        (JAVA_DOMAIN, "import static org.junit.Assert.assertTrue;", "java", ["org.junit.Assert.assertTrue"]),
        (CSHARP_DOMAIN, "using System.Data.SqlClient;", "csharp", ["System.Data.SqlClient"]),
        (CSHARP_DOMAIN, "using Db = Acme.Infrastructure.Db;", "csharp", ["Acme.Infrastructure.Db"]),
        (TS_DOMAIN, "import axios from 'axios';", "typescript", ["axios"]),
        (TS_DOMAIN, "const x = require('axios');", "typescript", ["axios"]),
        (TS_DOMAIN, "export * from './types';", "typescript", ["./types"]),
        ("src/domain/models.py", "from boto3 import client", "python", ["boto3"]),
        ("notes.md", "import boto3 in prose", "", []),
    ],
)
def test_imports_are_read_in_each_language(path, content, language, expected) -> None:
    read_language, modules = declared_imports(path, content)
    assert read_language == language
    assert modules == expected


def test_a_file_the_agent_is_midway_through_writing_still_reads() -> None:
    """Content arrives invalid, and a parser that failed open would be a hole."""
    _, modules = declared_imports(JAVA_DOMAIN, "import com.acme.infrastructure.Db;\npublic class Order {")
    assert modules == ["com.acme.infrastructure.Db"]


@pytest.mark.parametrize(
    "path, pattern, expected",
    [
        (JAVA_DOMAIN, "**/domain/**", True),
        (CSHARP_DOMAIN, "**/domain/**", True),
        ("docs/domain-driven-design.md", "**/domain/**", False),
        ("src/domainservice/thing.py", "**/domain/**", False),
        ("src\\domain\\models.py", "**/domain/**", True),
    ],
)
def test_paths_match_the_way_an_architect_would_expect(path, pattern, expected) -> None:
    assert matches(path, pattern) is expected


@pytest.mark.parametrize(
    "path, content, allowed",
    [
        (JAVA_DOMAIN, "import javax.persistence.Entity;", False),
        (JAVA_DOMAIN, "import java.util.List;", True),
        (JAVA_DOMAIN, "import com.acme.billing.infrastructure.OracleGateway;", False),
        ("src/main/java/com/acme/billing/application/Service.java", "import javax.persistence.Entity;", True),
        (CSHARP_DOMAIN, "using System.Data.SqlClient;", False),
        (CSHARP_DOMAIN, "using System.Collections.Generic;", True),
        (TS_DOMAIN, "import axios from 'axios';", False),
        ("src/domain/models.py", "from boto3 import client", False),
    ],
)
def test_the_shipped_rules_judge_four_languages(path, content, allowed) -> None:
    assert evaluate(path, content, DEFAULT_RULES)[0] is allowed


def test_a_near_miss_is_not_refused() -> None:
    """A false refusal stops an agent mid-edit and the tool is gone by evening."""
    assert evaluate(CSHARP_DOMAIN, "using System.ComponentModel.DataAnnotations;", DEFAULT_RULES)[0] is True
    assert evaluate(TS_DOMAIN, "import { x } from 'reactive-forms';", DEFAULT_RULES)[0] is True


def test_the_narrower_pattern_decides() -> None:
    """Allowing a namespace must not repeal every prohibition beneath it."""
    rules = [
        {
            "id": "acme-domain",
            "description": "Domain may not reach infrastructure, except its data contracts",
            "when_path_matches": ["**/domain/**/*.java"],
            "forbid_imports": ["**.infrastructure.**"],
            "allow_imports": ["com.acme.billing.infrastructure.dto"],
        }
    ]
    assert evaluate(JAVA_DOMAIN, "import com.acme.billing.infrastructure.dto.OrderDto;", rules)[0] is True
    assert evaluate(JAVA_DOMAIN, "import com.acme.billing.infrastructure.OracleGateway;", rules)[0] is False


def test_a_path_no_rule_covers_is_left_alone() -> None:
    """The shipped rules name their file types, so Kotlin is simply not covered."""
    allowed, reason = evaluate(
        "src/main/kotlin/com/acme/domain/Order.kt", "import javax.persistence.Entity", DEFAULT_RULES
    )
    assert allowed is True
    assert "No layering rule covers this path" == reason


def test_a_covered_file_in_a_language_it_cannot_read_says_so() -> None:
    """Refusing what cannot be read would block every language nobody taught it.

    An architect who writes a broad path pattern will catch file types this
    product has no reader for, and the answer has to name that rather than imply
    the file was examined and found clean.
    """
    broad = [
        {
            "id": "acme-domain-anything",
            "description": "Anything under domain/",
            "when_path_matches": ["**/domain/**"],
            "forbid_imports": ["javax.persistence"],
            "allow_imports": [],
        }
    ]
    allowed, reason = evaluate(
        "src/main/kotlin/com/acme/domain/Order.kt", "import javax.persistence.Entity", broad
    )
    assert allowed is True
    assert "does not read" in reason


def test_the_reason_names_the_rule_and_the_import() -> None:
    """An architect has to be able to act on a refusal without opening the code."""
    _, reason = evaluate(JAVA_DOMAIN, "import javax.persistence.Entity;", DEFAULT_RULES)
    assert "java-domain-stays-pure" in reason
    assert "javax.persistence.Entity" in reason
    assert JAVA_DOMAIN in reason


def test_a_malformed_rule_is_dropped_rather_than_half_applied() -> None:
    cleaned = normalise_rules(
        [
            {"id": "no-paths", "forbid_imports": ["java.sql"]},
            {"id": "no-forbids", "when_path_matches": ["**/domain/**"]},
            "not even an object",
            {"id": "good", "when_path_matches": ["**/domain/**"], "forbid_imports": ["java.sql"]},
        ]
    )
    assert [rule["id"] for rule in cleaned] == ["good"]
