"""A refusal comes with a fix, and the fix has been checked against the gate that refused.

An agent told only "refused" tries the same thing spelled differently. One told
what to write instead does that, so the advice had better be right: a suggested
fix that the same rules would refuse again teaches the agent that Threefold's
advice is noise. Every fix here that claims `validated` is therefore re-judged
in these tests by the real gate, not by the proposer's own account of itself,
and the last test does so for every refusal the rest of the suite contains.

Names, keys and tokens are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator
from threefold.application.fix_proposer import MAX_SUMMARY_CHARS, MAX_WRITE_BYTES, propose_fix
from threefold.domain import imports as import_readers
from threefold.domain.boundary_guard import (
    CONTENT_KEYS,
    PATH_KEYS,
    UNREADABLE_WRITE,
    ArchitecturalBoundaryGuard,
    SecretScanner,
    iter_string_leaves,
    looks_like_path,
)
from threefold.domain.imports import declared_imports
from threefold.domain.layering_rules import DEFAULT_RULES, rules_for_path, violations
from threefold.domain.models import ToolActionType, ToolInvocation

UNLIMITED = 10**9

# Built by concatenation so no file in the repository holds a credential-shaped
# string whole; each still matches the scanner, which a test below asserts.
ACCESS_KEY = "AKIA" + "ACMEEXAMPLE00000"
GITHUB_TOKEN = "ghp_" + "Acme" * 9
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n-----END " + "RSA PRIVATE KEY-----"
SECRETS = (ACCESS_KEY, GITHUB_TOKEN, "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu")

ALL_PASSED = {
    "SECRET_LEAKAGE_FREE": True,
    "ARCHITECTURAL_BOUNDARY_SAFE": True,
    "LOOP_THRASHING_FREE": True,
    "BUDGET_CIRCUIT_BREAKER_SAFE": True,
}


# --- helpers --------------------------------------------------------------------------


def _judge(tool: str, arguments: Dict[str, Any], rules=DEFAULT_RULES, action: str = "FILE_WRITE") -> Tuple[dict, dict]:
    """The request, and the verdict the evaluator's first gate gives it, shaped as the evaluator shapes it."""
    invocation = ToolInvocation(tool_name=tool, action_type=ToolActionType(action), arguments=arguments)
    allowed, reason = ArchitecturalBoundaryGuard.evaluate_tool_boundary(invocation, rules=rules)
    request = {"tool_name": tool, "action_type": action, "arguments": arguments}
    if allowed:
        return request, {"status": "APPROVED", "reason": reason, "rule_evaluations": dict(ALL_PASSED)}
    secret = "Sensitive credential detected" in reason
    evaluations = dict(ALL_PASSED)
    evaluations["SECRET_LEAKAGE_FREE" if secret else "ARCHITECTURAL_BOUNDARY_SAFE"] = False
    status = "BLOCKED_SECRET_DETECTED" if secret else "BLOCKED_BOUNDARY_VIOLATION"
    return request, {"status": status, "reason": reason, "rule_evaluations": evaluations}


def _refused(tool: str, arguments: Dict[str, Any], rules=DEFAULT_RULES, action: str = "FILE_WRITE") -> Tuple[dict, dict]:
    request, result = _judge(tool, arguments, rules, action)
    assert result["status"] != "APPROVED", "The fixture must be refused by the gate, or the test proves nothing"
    return request, result


def _assert_safe_to_show(fix: Dict[str, Any]) -> None:
    """What every fix must be, whatever else it is: short, one line, and free of any credential."""
    summary = fix["summary"]
    assert summary and len(summary) <= MAX_SUMMARY_CHARS, summary
    assert "\n" not in summary and "\r" not in summary
    assert set(fix) <= {"kind", "summary", "steps", "writes", "validated", "checks"}
    assert isinstance(fix["validated"], bool)
    text = json.dumps(fix)
    for secret in SECRETS:
        assert secret not in text, "A fix repeated the credential it was asked to remove"
    for leaf in iter_string_leaves(fix):
        assert SecretScanner.scan_payload(leaf)[0], f"A fix carries something the scanner calls a credential: {leaf[:40]!r}"
    if fix["validated"]:
        assert fix["checks"] and all(check["passed"] for check in fix["checks"] if check["gate"] != "route")


def _fix(request: Any, result: Any, rules=DEFAULT_RULES, **options: Any) -> Dict[str, Any]:
    options.setdefault("max_write_bytes", UNLIMITED)
    fix = propose_fix(request, result, rules, **options)
    assert fix is not None
    _assert_safe_to_show(fix)
    return fix


def _passes_gate(write: Dict[str, Any], rules) -> Tuple[bool, str]:
    """The real gate's view of one proposed write: the boundary guard, and every layering rule in any mode."""
    if write.get("old_string") is not None:
        tool, arguments = "Edit", {"file_path": write["path"], "old_string": write["old_string"], "new_string": write["content"]}
    else:
        tool, arguments = "Write", {"file_path": write["path"], "content": write["content"]}
    allowed, reason = ArchitecturalBoundaryGuard.evaluate_tool_boundary(
        ToolInvocation(tool_name=tool, action_type=ToolActionType.FILE_WRITE, arguments=arguments), rules=rules
    )
    found, _ = violations(write["path"], write["content"], rules)
    return allowed and not found, (reason if not allowed else (found[0]["reason"] if found else ""))


def _assert_really_passes(fix: Dict[str, Any], rules=DEFAULT_RULES) -> None:
    assert fix["validated"] is True, fix["summary"]
    assert fix["writes"], "A validated fix with code in it must carry the code"
    for write in fix["writes"]:
        passed, reason = _passes_gate(write, rules)
        assert passed, f"A validated fix is refused by the gate at {write['path']}: {reason}"


def _write_at(fix: Dict[str, Any], path: str) -> Dict[str, Any]:
    matches = [write for write in fix["writes"] if write["path"] == path]
    assert matches, f"No proposed write at {path}; got {[write['path'] for write in fix['writes']]}"
    return matches[0]


# --- layering: one case per shipped rule --------------------------------------------------

PY_DOMAIN = '''"""Orders, as the domain sees them."""
from __future__ import annotations

import os, boto3
from dataclasses import dataclass


@dataclass
class OrderRepository:
    table: str

    def save(self, order):
        client = boto3.client("dynamodb")
        return client.put_item(TableName=self.table, Item=order)
'''

JAVA_DOMAIN = """package com.acme.billing.domain;

import java.util.List;
import javax.persistence.Entity;
import java.sql.DriverManager;

@Entity
public class Order {
    public Object open(String url) throws Exception {
        return DriverManager.getConnection(url);
    }
}
"""

CS_DOMAIN = """using System;
using System.Data.SqlClient;

namespace Acme.Billing.Domain;

public class Invoice
{
    public void Save() { var connection = new SqlConnection("acme"); }
}
"""

TS_DOMAIN = """import { Money } from './money';
import axios from 'axios';
import {
  S3Client,
  PutObjectCommand,
} from '@aws-sdk/client-s3';
import { AcmeGateway } from '../infrastructure/acme-gateway';

export async function price(id: string): Promise<Money> {
  const response = await axios.get(`/prices/${id}`);
  return response.data;
}
"""

SHIPPED_CASES = {
    "python-domain-stays-pure": (
        "src/acme/domain/order_repository.py", PY_DOMAIN, ["boto3"], "src/acme/infrastructure/order_repository_adapter.py",
        ["boto3"],
    ),
    "java-domain-stays-pure": (
        "src/main/java/com/acme/billing/domain/Order.java", JAVA_DOMAIN, ["javax.persistence", "java.sql"],
        "src/main/java/com/acme/billing/infrastructure/OrderAdapter.java",
        ["javax.persistence.Entity", "java.sql.DriverManager"],
    ),
    "dotnet-domain-stays-pure": (
        "Acme.Billing/Domain/Invoice.cs", CS_DOMAIN, ["System.Data"], "Acme.Billing/Infrastructure/InvoiceAdapter.cs",
        ["System.Data.SqlClient"],
    ),
    "web-domain-stays-pure": (
        "web/src/domain/price.ts", TS_DOMAIN, ["axios", "@aws-sdk/client-s3", "../infrastructure/acme-gateway"],
        "web/src/infrastructure/price.adapter.ts",
        # The relative import is said again from the adapter's own directory.
        ["axios", "@aws-sdk/client-s3", "./acme-gateway"],
    ),
}


def test_every_shipped_rule_has_a_case_here() -> None:
    assert set(SHIPPED_CASES) == {rule["id"] for rule in DEFAULT_RULES}


@pytest.mark.parametrize("rule_id", sorted(SHIPPED_CASES))
def test_every_shipped_rule_gets_a_fix_the_gate_accepts(rule_id: str) -> None:
    path, content, modules, adapter_path, adapter_imports = SHIPPED_CASES[rule_id]
    request, result = _refused("Write", {"file_path": path, "content": content})
    assert rule_id in result["reason"]

    fix = _fix(request, result)

    assert fix["kind"] == "layering"
    _assert_really_passes(fix)
    domain = _write_at(fix, path)
    left = declared_imports(path, domain["content"])[1]
    assert not [module for module in left if any(module.startswith(name) for name in modules)], left
    adapter = _write_at(fix, adapter_path)
    moved = declared_imports(adapter_path, adapter["content"])[1]
    assert set(adapter_imports) <= set(moved), moved
    assert not rules_for_path(adapter_path, DEFAULT_RULES) or not violations(adapter_path, adapter["content"], DEFAULT_RULES)[0]
    assert {check["path"] for check in fix["checks"]} == {write["path"] for write in fix["writes"]}
    assert all(check["passed"] for check in fix["checks"])


def test_the_python_fix_is_python_and_declares_a_protocol() -> None:
    path, content, _, adapter_path, _ = SHIPPED_CASES["python-domain-stays-pure"]
    fix = _fix(*_refused("Write", {"file_path": path, "content": content}))
    domain = _write_at(fix, path)["content"]
    adapter = _write_at(fix, adapter_path)["content"]
    compile(domain, path, "exec")
    compile(adapter, adapter_path, "exec")
    assert "class OrderRepositoryPort(Protocol):" in domain
    assert "def client(self, *args: Any, **kwargs: Any) -> Any: ..." in domain
    assert domain.index("from __future__ import annotations") < domain.index("from typing import Any, Protocol")
    assert "import os\n" in domain, "Only the forbidden name leaves a shared import statement"
    assert "import boto3" in adapter and "return boto3.client(*args, **kwargs)" in adapter


def test_the_java_fix_puts_the_interface_in_its_own_domain_file() -> None:
    path, content, _, adapter_path, _ = SHIPPED_CASES["java-domain-stays-pure"]
    fix = _fix(*_refused("Write", {"file_path": path, "content": content}))
    port = _write_at(fix, "src/main/java/com/acme/billing/domain/OrderPort.java")["content"]
    adapter = _write_at(fix, adapter_path)["content"]
    domain = _write_at(fix, path)["content"]
    assert "package com.acme.billing.domain;" in port and "public interface OrderPort {" in port
    assert "Object getConnection(Object... args);" in port
    assert adapter.startswith("package com.acme.billing.infrastructure;")
    assert "import com.acme.billing.domain.OrderPort;" in adapter and "implements OrderPort" in adapter
    assert "import java.util.List;" in domain, "An import the rule allows stays where it was"


def test_the_csharp_fix_keeps_the_casing_of_the_layers() -> None:
    path, content, _, adapter_path, _ = SHIPPED_CASES["dotnet-domain-stays-pure"]
    fix = _fix(*_refused("Write", {"file_path": path, "content": content}))
    port = _write_at(fix, "Acme.Billing/Domain/IInvoicePort.cs")["content"]
    adapter = _write_at(fix, adapter_path)["content"]
    assert "namespace Acme.Billing.Domain;" in port and "public interface IInvoicePort" in port
    assert "namespace Acme.Billing.Infrastructure;" in adapter and "public class InvoiceAdapter : IInvoicePort" in adapter
    assert adapter.index("using System.Data.SqlClient;") < adapter.index("namespace Acme.Billing.Infrastructure;")


def test_the_typescript_fix_rebases_a_relative_import_into_the_adapter() -> None:
    path, content, _, adapter_path, _ = SHIPPED_CASES["web-domain-stays-pure"]
    fix = _fix(*_refused("Write", {"file_path": path, "content": content}))
    domain = _write_at(fix, path)["content"]
    adapter = _write_at(fix, adapter_path)["content"]
    assert "export interface PricePort {" in domain and "get(...args: unknown[]): unknown;" in domain
    assert "import { Money } from './money';" in domain
    assert "import type { PricePort } from '../domain/price';" in adapter
    assert "import { AcmeGateway } from './acme-gateway';" in adapter, "A relative import must still resolve from the adapter"
    assert "PutObjectCommand" in adapter and "import axios from 'axios';" in adapter


def test_a_javascript_domain_gets_a_class_to_extend_because_it_has_no_interfaces() -> None:
    rules = [
        {
            "id": "acme-js-domain",
            "when_path_matches": ["**/domain/**/*.js"],
            "forbid_imports": ["axios", "**/infrastructure/*"],
        }
    ]
    content = "import axios from 'axios';\n\nexport const load = () => axios.get('/acme');\n"
    fix = _fix(*_refused("Write", {"file_path": "web/src/domain/load.js", "content": content}, rules), rules)
    _assert_really_passes(fix, rules)
    assert "export class LoadPort {" in _write_at(fix, "web/src/domain/load.js")["content"]
    adapter = _write_at(fix, "web/src/infrastructure/load.adapter.js")["content"]
    assert "import { LoadPort } from '../domain/load.js';" in adapter
    assert "export class LoadAdapter extends LoadPort {" in adapter and "return axios.get(...args);" in adapter


# --- several imports, and imports that are not imports ----------------------------------


def test_a_python_file_with_many_forbidden_imports_loses_all_of_them() -> None:
    content = '''from typing import TYPE_CHECKING
import os, boto3
import requests
from sqlalchemy import select

if TYPE_CHECKING:
    import httpx


def cached():
    import redis
    return redis.Redis()


def query():
    return select(requests.get("acme"))
'''
    path = "src/acme/domain/catalog.py"
    fix = _fix(*_refused("Write", {"file_path": path, "content": content}))
    _assert_really_passes(fix)
    domain = _write_at(fix, path)["content"]
    compile(domain, path, "exec")
    assert set(declared_imports(path, domain)[1]) == {"typing", "os"}
    adapter = _write_at(fix, "src/acme/infrastructure/catalog_adapter.py")["content"]
    compile(adapter, "catalog_adapter.py", "exec")
    for statement in ("import boto3", "import requests", "from sqlalchemy import select", "import httpx", "import redis"):
        assert statement in adapter, statement
    assert "    pass" in domain, "A block emptied by the removal keeps a pass, so the file still parses"


def test_a_java_file_with_many_forbidden_imports_loses_all_of_them() -> None:
    content = (
        "package com.acme.shop.domain;\n\nimport java.time.Instant;\nimport javax.persistence.Entity;\n"
        "import org.springframework.stereotype.Service;\nimport static org.hibernate.Hibernate.initialize;\n"
        "import java.sql.*;\n\npublic class Cart { void load() { initialize(this); } }\n"
    )
    path = "src/main/java/com/acme/shop/domain/Cart.java"
    fix = _fix(*_refused("Write", {"file_path": path, "content": content}))
    _assert_really_passes(fix)
    assert declared_imports(path, _write_at(fix, path)["content"])[1] == ["java.time.Instant"]
    port = _write_at(fix, "src/main/java/com/acme/shop/domain/CartPort.java")["content"]
    assert "Object initialize(Object... args);" in port


def test_a_typescript_file_with_every_import_spelling_loses_all_of_them() -> None:
    content = (
        "import React from 'react';\n"
        "import {\n  useState,\n  useEffect,\n} from 'react-dom';\n"
        "const fetcher = require('node-fetch');\n"
        "const {\n  PrismaClient,\n} = require('@prisma/client');\n"
        "export * from 'typeorm';\n"
        "export const total = (items: number[]) => items.reduce((a, b) => a + b, 0);\n"
    )
    path = "web/src/domain/cart.ts"
    fix = _fix(*_refused("Write", {"file_path": path, "content": content}))
    _assert_really_passes(fix)
    domain = _write_at(fix, path)["content"]
    assert declared_imports(path, domain)[1] == []
    assert "export const total" in domain


def test_an_import_in_a_docstring_or_a_comment_is_left_alone() -> None:
    """The gate reads a docstring as text and a comment as nothing; so does the fix."""
    content = '''"""Orders.

Never write this here:
import boto3
"""
# import redis
import requests


def fetch():
    return requests.get("acme")
'''
    path = "src/domain/order.py"
    assert declared_imports(path, content)[1] == ["requests"], "The fixture's docstring must not read as an import"
    fix = _fix(*_refused("Write", {"file_path": path, "content": content}))
    _assert_really_passes(fix)
    domain = _write_at(fix, path)["content"]
    assert "Never write this here:\nimport boto3\n" in domain
    assert "# import redis" in domain
    assert "import requests" not in domain
    adapter = _write_at(fix, "src/infrastructure/order_adapter.py")["content"]
    assert "import requests" in adapter and "boto3" not in adapter and "redis" not in adapter


def test_a_commented_typescript_import_is_left_alone() -> None:
    content = (
        "/*\n * import axios from 'axios';\n */\n"
        "// import react from 'react';\n"
        "import axios from 'axios';\n\n"
        "export const load = () => axios.get('/acme');\n"
    )
    path = "web/src/domain/load.ts"
    assert declared_imports(path, content)[1] == ["axios"]
    fix = _fix(*_refused("Write", {"file_path": path, "content": content}))
    _assert_really_passes(fix)
    domain = _write_at(fix, path)["content"]
    assert " * import axios from 'axios';" in domain and "// import react from 'react';" in domain
    assert declared_imports(path, domain)[1] == []


def test_content_that_does_not_parse_is_fixed_by_the_line_reader() -> None:
    """Content arrives mid-edit. The gate reads it by line, and so does the fix."""
    content = "import boto3\nimport os\n\ndef half_written(:\n"
    path = "src/domain/draft.py"
    fix = _fix(*_refused("Write", {"file_path": path, "content": content}))
    _assert_really_passes(fix)
    assert declared_imports(path, _write_at(fix, path)["content"])[1] == ["os", "typing"]


# --- where there is nowhere to go -----------------------------------------------------


def test_no_permitted_layer_means_no_validated_fix() -> None:
    everywhere = [{"id": "acme-no-boto3-anywhere", "when_path_matches": ["**/*.py"], "forbid_imports": ["boto3"]}]
    request, result = _refused("Write", {"file_path": "src/domain/order.py", "content": "import boto3\n"}, everywhere)

    fix = _fix(request, result, everywhere)

    assert fix["kind"] == "layering"
    assert fix["validated"] is False
    assert fix["writes"] == [], "A fix that did not pass is advice in words, not code"
    assert "no layer" in fix["summary"]
    failed = [check for check in fix["checks"] if not check["passed"]]
    assert failed and all("adapter" in check["path"] for check in failed)
    assert any("architect" in step for step in fix["steps"])


def test_a_layer_taken_from_the_rules_own_patterns_is_tried_first() -> None:
    rules = [
        {
            "id": "acme-core-no-http",
            "when_path_matches": ["**/core/**/*.py"],
            "forbid_imports": ["requests", "**.gateways.**"],
        }
    ]
    fix = _fix(*_refused("Write", {"file_path": "src/acme/core/pricing.py", "content": "import requests\n"}, rules), rules)
    _assert_really_passes(fix, rules)
    assert _write_at(fix, "src/acme/gateways/pricing_adapter.py")


# --- edits and shell writes -----------------------------------------------------------


def test_an_edit_is_retried_as_an_edit() -> None:
    arguments = {
        "file_path": "src/domain/acme_user.py",
        "old_string": "import json\n",
        "new_string": "import json\nimport boto3\n\nLIMIT = 3\n",
    }
    fix = _fix(*_refused("Edit", arguments))
    _assert_really_passes(fix)
    retry = _write_at(fix, "src/domain/acme_user.py")
    assert retry["old_string"] == "import json\n" and retry["content"] == "import json\n\nLIMIT = 3\n"
    assert retry["content"] != retry["old_string"], "An Edit retry must change something, or the Edit tool rejects it"
    assert "class AcmeUserPort(Protocol):" in _write_at(fix, "src/domain/acme_user_port.py")["content"]
    assert _write_at(fix, "src/infrastructure/acme_user_adapter.py")


def test_an_edit_that_only_added_the_import_needs_no_edit_at_all() -> None:
    """Retried without the import, it would be an Edit whose new_string equals its old_string: a no-op the tool rejects."""
    arguments = {"file_path": "src/domain/acme_user.py", "old_string": "import json\n", "new_string": "import json\nimport boto3\n"}
    fix = _fix(*_refused("Edit", arguments))
    _assert_really_passes(fix)
    assert [write["path"] for write in fix["writes"]] == [
        "src/domain/acme_user_port.py",
        "src/infrastructure/acme_user_adapter.py",
    ], "No write to the domain file: leaving it as it is is the fix"
    assert any("Nothing needs retrying in src/domain/acme_user.py" in step for step in fix["steps"])


def test_a_port_and_an_adapter_are_proposed_as_new_files_with_a_warning() -> None:
    """Threefold cannot see the disk, so it says so rather than let a Write overwrite an adapter that exists."""
    arguments = {"file_path": "src/domain/acme_user.py", "old_string": "a = 1", "new_string": "import requests\na = 2"}
    fix = _fix(*_refused("Edit", arguments))
    created = {write["path"] for write in fix["writes"] if write.get("new_file")}
    assert created == {"src/domain/acme_user_port.py", "src/infrastructure/acme_user_adapter.py"}
    assert not _write_at(fix, "src/domain/acme_user.py").get("new_file")
    assert any("cannot see whether they already exist" in step and "rather than overwrite" in step for step in fix["steps"])


def test_a_multi_edit_is_retried_edit_by_edit() -> None:
    arguments = {
        "file_path": "src/domain/acme_user.py",
        "edits": [{"old_string": "a = 1", "new_string": "import requests"}, {"old_string": "b = 2", "new_string": "import redis"}],
    }
    fix = _fix(*_refused("MultiEdit", arguments))
    _assert_really_passes(fix)
    retries = [write for write in fix["writes"] if write["path"] == "src/domain/acme_user.py"]
    assert [write["old_string"] for write in retries] == ["a = 1", "b = 2"]
    adapter = _write_at(fix, "src/infrastructure/acme_user_adapter.py")["content"]
    assert "import requests" in adapter and "import redis" in adapter


@pytest.mark.parametrize(
    "command, partial",
    [
        ("cat > src/domain/acme_user.py <<'EOF'\nimport boto3\n\ndef load():\n    return boto3.resource('s3')\nEOF", False),
        ("echo 'import boto3' > src/domain/x.py", False),
        ("cd src/domain && echo 'import boto3' > x.py", False),
        ("python -c \"open('src/domain/x.py','w').write('import boto3')\"", False),
        ("printf 'import boto3\\nLIMIT = 3\\n' >> src/domain/x.py", True),
        ("printf 'import boto3\\nLIMIT = 3\\n' | tee -a src/domain/x.py", True),
        ("cat >> src/domain/x.py <<'EOF'\nimport boto3\nLIMIT = 3\nEOF", True),
    ],
)
def test_a_shell_write_becomes_the_write_it_meant(command: str, partial: bool) -> None:
    fix = _fix(*_refused("Bash", {"command": command}, action="COMMAND_EXEC"))
    assert fix["kind"] == "layering"
    _assert_really_passes(fix)
    domain = [write for write in fix["writes"] if write["path"].startswith("src/domain/") and "_port" not in write["path"]]
    assert domain and all(bool(write.get("partial")) is partial for write in domain), domain
    assert all(write["content"].strip() for write in domain), "A proposed write must add something"
    assert any("instead of the shell command" in step for step in fix["steps"])


@pytest.mark.parametrize(
    "command",
    [
        "echo 'import boto3' >> src/domain/x.py",
        "echo 'import boto3' | tee -a src/domain/x.py",
        "cat >> src/domain/x.py <<'EOF'\nimport boto3\nEOF",
    ],
)
def test_an_append_of_only_the_import_needs_no_addition(command: str) -> None:
    """Without the import there is nothing left to add: an empty partial write is not a fix."""
    fix = _fix(*_refused("Bash", {"command": command}, action="COMMAND_EXEC"))
    _assert_really_passes(fix)
    assert "src/domain/x.py" not in [write["path"] for write in fix["writes"]]
    assert {write["path"] for write in fix["writes"]} == {"src/domain/x_port.py", "src/infrastructure/x_adapter.py"}
    assert any("Nothing needs retrying in src/domain/x.py" in step for step in fix["steps"])


# --- writes the rules could not read --------------------------------------------------


def test_an_unquoted_heredoc_is_proposed_as_the_text_it_says() -> None:
    command = "cat > web/src/domain/greet.ts <<EOF\nexport const greet = (name: string) => `hello ${name}`;\nEOF"
    request, result = _refused("Bash", {"command": command}, action="COMMAND_EXEC")
    assert UNREADABLE_WRITE in result["reason"]

    fix = _fix(request, result)

    assert fix["kind"] == "unreadable_write"
    _assert_really_passes(fix)
    assert _write_at(fix, "web/src/domain/greet.ts")["content"] == "export const greet = (name: string) => `hello ${name}`;"
    assert any("not quoted" in step for step in fix["steps"])


def test_an_unquoted_heredoc_that_breaks_a_rule_gets_the_layering_fix() -> None:
    command = "cat > src/domain/acme_store.py <<EOF\nimport boto3\nPATH = '$HOME'\nEOF"
    fix = _fix(*_refused("Bash", {"command": command}, action="COMMAND_EXEC"))
    assert fix["kind"] == "unreadable_write"
    _assert_really_passes(fix)
    assert "import boto3" not in _write_at(fix, "src/domain/acme_store.py")["content"]
    assert "import boto3" in _write_at(fix, "src/infrastructure/acme_store_adapter.py")["content"]


@pytest.mark.parametrize(
    "command, advice",
    [
        ("cp /tmp/acme.py src/domain/user.py", "Read the file the command copies"),
        ("sed -i 's/json/boto3/' src/domain/user.py", "Edit"),
        ("git apply acme.patch", "naming its file"),
        ("python generate.py > src/domain/user.py", "run the program on its own"),
    ],
)
def test_a_write_whose_content_cannot_be_seen_is_never_validated(command: str, advice: str) -> None:
    request, result = _refused("Bash", {"command": command}, action="COMMAND_EXEC")
    assert UNREADABLE_WRITE in result["reason"]

    fix = _fix(request, result)

    assert fix["kind"] == "unreadable_write"
    assert fix["validated"] is False
    assert fix["writes"] == []
    assert any(advice in step for step in fix["steps"]), fix["steps"]


def test_a_readable_route_is_recorded_but_does_not_validate() -> None:
    fix = _fix(*_refused("Bash", {"command": "cp /tmp/acme.py src/domain/user.py"}, action="COMMAND_EXEC"))
    assert fix["checks"] == [{"gate": "route", "path": "src/domain/user.py", "passed": True}]
    assert fix["validated"] is False


# --- credentials ------------------------------------------------------------------------

CREDENTIAL_CASES = [
    ("src/acme/config.py", '"""Settings."""\nfrom __future__ import annotations\n\nACCESS_KEY_ID = "' + ACCESS_KEY + '"\n', 'os.environ["ACCESS_KEY_ID"]'),
    ("src/acme/client.py", 'def headers():\n    return {"Authorization": "Bearer ' + GITHUB_TOKEN + '"}\n', '"Bearer " + os.environ["GITHUB_TOKEN"]'),
    # `token` says only "a secret"; the scanner knows which, so its name wins.
    ("web/src/api.ts", 'export const token = "' + GITHUB_TOKEN + '";\n', "process.env.GITHUB_TOKEN"),
    ("web/src/call.ts", "const auth = `Bearer " + GITHUB_TOKEN + "`;\n", "`Bearer ${process.env.GITHUB_TOKEN}`"),
    ("src/main/java/com/acme/Config.java", 'class Config { static final String KEY = "' + ACCESS_KEY + '"; }\n', 'System.getenv("AWS_ACCESS_KEY_ID")'),
    ("Acme/Config.cs", 'class Config { const string Token = "' + GITHUB_TOKEN + '"; }\n', 'System.Environment.GetEnvironmentVariable("GITHUB_TOKEN")'),
    ("cmd/acme/main.go", 'package main\n\nvar key = "' + ACCESS_KEY + '"\n', 'os.Getenv("AWS_ACCESS_KEY_ID")'),
    ("deploy/acme.yml", "token: " + GITHUB_TOKEN + "\n", "token: ${GITHUB_TOKEN}"),
    ("scripts/acme.sh", 'export ACME_KEY="' + ACCESS_KEY + '"\n', 'export ACME_KEY="${ACME_KEY}"'),
    ("src/main/kotlin/com/acme/Config.kt", 'object Config { val token = "' + GITHUB_TOKEN + '" }\n', 'val token = System.getenv("GITHUB_TOKEN")'),
    ("lib/acme/config.rb", "ACME_TOKEN = '" + GITHUB_TOKEN + "'\n", 'ACME_TOKEN = ENV["ACME_TOKEN"]'),
    ("src/Acme/config.php", "<?php\n$auth = 'Bearer " + GITHUB_TOKEN + "';\n", "$auth = 'Bearer ' . getenv('GITHUB_TOKEN');"),
    ("web/src/client.js", "const headers = { Authorization: 'token " + GITHUB_TOKEN + "' };\n", "Authorization: 'token ' + process.env.GITHUB_TOKEN }"),
    # PowerShell expands nothing in '...' and reads the environment as $env:NAME.
    ("scripts/acme.ps1", "$Token = '" + GITHUB_TOKEN + "'\n", "$Token = $env:GITHUB_TOKEN"),
    ("scripts/call.ps1", "$h = 'it''s " + GITHUB_TOKEN + " $x'\n", '$h = "it\'s $($env:GITHUB_TOKEN) `$x"'),
]


def test_the_credential_cases_cover_every_language_with_a_lookup() -> None:
    from threefold.application.fix_proposer import _LOOKUPS, _flavour

    assert {_flavour(path) for path, _, _ in CREDENTIAL_CASES} >= set(_LOOKUPS)


@pytest.mark.parametrize("path, content, lookup", CREDENTIAL_CASES)
def test_a_credential_becomes_the_languages_environment_lookup(path: str, content: str, lookup: str) -> None:
    assert not SecretScanner.scan_payload(content)[0], "The fixture must hold something the scanner calls a credential"
    request, result = _refused("Write", {"file_path": path, "content": content})
    assert result["status"] == "BLOCKED_SECRET_DETECTED"

    fix = _fix(request, result)

    assert fix["kind"] == "credential"
    _assert_really_passes(fix)
    proposed = _write_at(fix, path)["content"]
    assert lookup in proposed, proposed
    assert any(check["gate"] == "credential" and check["passed"] for check in fix["checks"])
    if path.endswith(".py"):
        compile(proposed, path, "exec")
        assert "import os" in proposed


def test_a_private_key_block_is_replaced_whole() -> None:
    content = 'SIGNING_KEY = """' + PEM + '"""\n'
    fix = _fix(*_refused("Write", {"file_path": "src/acme/signing.py", "content": content}))
    _assert_really_passes(fix)
    proposed = _write_at(fix, "src/acme/signing.py")["content"]
    assert 'SIGNING_KEY = os.environ["SIGNING_KEY"]' in proposed
    assert "PRIVATE KEY" not in proposed and "MIIB" not in proposed


PEM_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"
PEM_FOOTER = "-----END " + "RSA PRIVATE KEY-----"
PEM_BODY = SECRETS[2]


@pytest.mark.parametrize(
    "path, content, lookup",
    [
        (
            "deploy/acme.yml",
            "signing:\n  key: |\n    " + PEM_HEADER + "\n    " + PEM_BODY + "\n    " + PEM_FOOTER + "\n  other: kept\n",
            "  key: |\n    ${PRIVATE_KEY_PEM}\n  other: kept\n",
        ),
        # No END line, as when the content was cut off: the body still goes,
        # and the next key of the file is not taken for base64.
        ("deploy/acme.yml", "key: |\n  " + PEM_HEADER + "\n  " + PEM_BODY + "\nother: kept\n", "key: |\n  ${PRIVATE_KEY_PEM}\nother: kept\n"),
        # One line, with the newlines written as \n escapes.
        (
            "src/acme/keys.py",
            'SIGNING_KEY = "' + PEM_HEADER + "\\n" + PEM_BODY + "\\n" + PEM_FOOTER + '"\n',
            'SIGNING_KEY = os.environ["SIGNING_KEY"]\n',
        ),
    ],
)
def test_a_private_key_goes_with_its_body(path: str, content: str, lookup: str) -> None:
    fix = _fix(*_refused("Write", {"file_path": path, "content": content}))
    _assert_really_passes(fix)
    proposed = _write_at(fix, path)["content"]
    assert lookup in proposed, proposed
    assert PEM_BODY not in proposed and "PRIVATE KEY" not in proposed


@pytest.mark.parametrize(
    "tool, arguments",
    [
        # The header in one edit and the footer in the next: the reviewer's case.
        (
            "MultiEdit",
            {
                "file_path": "src/acme/k.py",
                "edits": [
                    {"old_string": "a", "new_string": 'K = """' + PEM_HEADER + "\n" + PEM_BODY},
                    {"old_string": "b", "new_string": PEM_FOOTER + '\n"""'},
                ],
            },
        ),
        # Cut off before its END line, inside a string that never closes.
        ("Write", {"file_path": "src/acme/signing.py", "content": 'KEY = """' + PEM_HEADER + "\n" + PEM_BODY + "\n"}),
        # One string per line, joined with +: the header's string ends before the body.
        (
            "Write",
            {
                "file_path": "src/main/java/com/acme/Keys.java",
                "content": 'class Keys { static final String PEM = "' + PEM_HEADER + '\\n" +\n    "' + PEM_BODY + '\\n" +\n    "'
                + PEM_FOOTER + '"; }\n',
            },
        ),
    ],
)
def test_a_private_key_body_no_rewrite_can_reach_is_never_echoed(tool: str, arguments: Dict[str, Any]) -> None:
    """The scanner knows a key only by its header; the body left behind used to come back validated."""
    fix = _fix(*_refused(tool, arguments))
    assert fix["kind"] == "credential"
    assert fix["validated"] is False and fix["writes"] == []
    assert PEM_BODY not in json.dumps(fix)


def test_a_credential_inside_a_longer_string_is_not_turned_into_text_that_looks_like_code() -> None:
    """Quotes read on one line mistake a quote inside a triple-quoted string for a string of its own."""
    content = 'DOC = """\nSay "token: ' + GITHUB_TOKEN + '" to nobody.\n"""\n'
    fix = _fix(*_refused("Write", {"file_path": "src/acme/doc.py", "content": content}))
    assert fix["validated"] is False and fix["writes"] == []


def test_a_withheld_secret_never_leaves_even_when_a_step_or_a_write_carries_it() -> None:
    """The belt behind the braces: _finish keeps the call's own secret text out of everything it returns."""
    from threefold.application import fix_proposer

    fix = fix_proposer._Fix(
        "credential",
        "Summary quoting " + PEM_BODY,
        ["A step quoting " + PEM_BODY],
        [{"path": "deploy/acme.yml", "content": "key: " + PEM_BODY + "\n"}],
        True,
        [{"gate": "credential", "path": "deploy/acme.yml", "passed": True}],
        withheld=[PEM_BODY],
    )
    out = fix_proposer._finish(fix, UNLIMITED, None)
    assert PEM_BODY not in json.dumps(out)
    assert out["validated"] is False and out["writes"] == []


@pytest.mark.parametrize(
    "path, content, expected, never",
    [
        ("src/acme/c.py", 'PATH = "' + GITHUB_TOKEN + '"\n', 'PATH = os.environ["GITHUB_TOKEN"]', "Set PATH"),
        ("src/acme/c.py", "HOME = '" + ACCESS_KEY + "'\n", 'HOME = os.environ["AWS_ACCESS_KEY_ID"]', "Set HOME"),
        (
            "web/acme.js",
            "const URL = `https://x-access-token:" + GITHUB_TOKEN + "@git.acme.test/r`;\n",
            "`https://x-access-token:${process.env.GITHUB_TOKEN}@git.acme.test/r`",
            "Set URL",
        ),
        (
            "src/acme/c.py",
            'REPO_URL = "https://x-access-token:' + GITHUB_TOKEN + '@git.acme.test/r"\n',
            'REPO_URL = "https://x-access-token:" + os.environ["GITHUB_TOKEN"] + "@git.acme.test/r"',
            "Set REPO_URL",
        ),
    ],
)
def test_a_name_the_system_uses_or_a_place_is_never_the_variable(path: str, content: str, expected: str, never: str) -> None:
    """Reading PATH or HOME as the credential, and telling the user to set them, would break their shell."""
    fix = _fix(*_refused("Write", {"file_path": path, "content": content}))
    _assert_really_passes(fix)
    assert expected in _write_at(fix, path)["content"]
    assert not any(never in step for step in fix["steps"]), fix["steps"]


def test_a_credential_in_a_command_becomes_an_environment_reference() -> None:
    command = f"curl -H 'Authorization: Bearer {GITHUB_TOKEN}' https://api.acme.test/orders"
    fix = _fix(*_refused("Bash", {"command": command}, action="COMMAND_EXEC"))
    assert fix["kind"] == "credential"
    assert fix["validated"] is True and fix["writes"] == []
    # Out of the single quotes and into double ones, where the shell expands it.
    assert "Run instead: curl -H 'Authorization: Bearer '\"${GITHUB_TOKEN}\" https://api.acme.test/orders" in fix["steps"]
    assert {check["gate"] for check in fix["checks"]} == {"credential", "boundary"}


@pytest.mark.parametrize(
    "tool, command, expected",
    [
        # A backslash-escaped quote outside the quotes, read as the shell reads it.
        ("Bash", "printf '%s' 'it'\\''s " + GITHUB_TOKEN + " now'", "Run instead: printf '%s' 'it'\\''s '\"${GITHUB_TOKEN}\"' now'"),
        ("Bash", 'curl -H "Authorization: Bearer ' + GITHUB_TOKEN + '" https://api.acme.test', 'Run instead: curl -H "Authorization: Bearer ${GITHUB_TOKEN}" https://api.acme.test'),
        # Codex sends a shell wrapper as a list: the script is what changes.
        (
            "exec_command",
            ["bash", "-lc", "curl -H 'Authorization: Bearer " + GITHUB_TOKEN + "' https://api.acme.test"],
            "Pass this as the script bash runs: curl -H 'Authorization: Bearer '\"${GITHUB_TOKEN}\" https://api.acme.test",
        ),
        (
            "PowerShell",
            "Invoke-RestMethod -Headers @{Authorization='Bearer " + GITHUB_TOKEN + "'} https://api.acme.test",
            'Run instead: Invoke-RestMethod -Headers @{Authorization="Bearer $($env:GITHUB_TOKEN)"} https://api.acme.test',
        ),
    ],
)
def test_a_rewritten_command_still_expands_the_variable_where_the_secret_was(tool: str, command: Any, expected: str) -> None:
    """Each expected command was run in bash or PowerShell when this was written, and printed the variable's value."""
    fix = _fix(*_refused(tool, {"command": command}, action="COMMAND_EXEC"))
    assert fix["kind"] == "credential" and fix["validated"] is True
    assert expected in fix["steps"], fix["steps"]


@pytest.mark.parametrize(
    "tool, command, why",
    [
        # No shell runs a list of words, so nothing in it is ever expanded.
        ("exec_command", ["curl", "-H", "Authorization: Bearer " + GITHUB_TOKEN, "https://api.acme.test"], "without a shell"),
        # A quoted heredoc is literal text.
        ("Bash", "cat <<'EOF' | curl -d @- https://api.acme.test\n" + GITHUB_TOKEN + "\nEOF", "quoted heredoc"),
        # So is a single-quoted PowerShell here-string.
        ("PowerShell", "$body = @'\n" + GITHUB_TOKEN + "\n'@\nInvoke-RestMethod -Body $body https://api.acme.test", "here-string"),
    ],
)
def test_a_command_where_no_variable_would_expand_gets_no_checked_fix(tool: str, command: Any, why: str) -> None:
    fix = _fix(*_refused(tool, {"command": command}, action="COMMAND_EXEC"))
    assert fix["kind"] == "credential"
    assert fix["validated"] is False and fix["writes"] == []
    assert any(why in step for step in fix["steps"]), fix["steps"]


def test_a_command_that_spans_lines_is_described_not_flattened() -> None:
    """A heredoc put on one line is a different command; a step is one line."""
    command = 'curl -d @- https://api.acme.test <<EOF\n{"token": "' + GITHUB_TOKEN + '"}\nEOF'
    fix = _fix(*_refused("Bash", {"command": command}, action="COMMAND_EXEC"))
    assert fix["validated"] is True
    assert not any(step.startswith("Run instead") for step in fix["steps"])
    assert any("spans several lines" in step and "$GITHUB_TOKEN" in step for step in fix["steps"])


def test_a_credential_written_by_a_heredoc_is_fixed_in_the_file() -> None:
    command = "cat > src/acme/settings.py <<'EOF'\nTOKEN = \"" + GITHUB_TOKEN + "\"\nEOF"
    fix = _fix(*_refused("Bash", {"command": command}, action="COMMAND_EXEC"))
    _assert_really_passes(fix)
    # TOKEN names no particular secret, so the scanner's own name for it is used.
    assert 'TOKEN = os.environ["GITHUB_TOKEN"]' in _write_at(fix, "src/acme/settings.py")["content"]


def test_a_credential_in_an_edits_old_string_cannot_be_fixed_and_is_not_repeated() -> None:
    arguments = {"file_path": "src/acme/config.py", "old_string": f'KEY = "{ACCESS_KEY}"', "new_string": "KEY = None"}
    fix = _fix(*_refused("Edit", arguments))
    assert fix["kind"] == "credential"
    assert fix["validated"] is False and fix["writes"] == []
    assert any("old_string" in step for step in fix["steps"])


def test_a_credential_fix_that_also_breaks_a_layering_rule_gets_both_fixes() -> None:
    content = 'import boto3\nKEY = "' + ACCESS_KEY + '"\n'
    fix = _fix(*_refused("Write", {"file_path": "src/domain/acme_keys.py", "content": content}))
    assert fix["kind"] == "credential"
    _assert_really_passes(fix)
    domain = _write_at(fix, "src/domain/acme_keys.py")["content"]
    assert "import boto3" not in domain
    assert 'KEY = os.environ["AWS_ACCESS_KEY_ID"]' in domain and "import os" in domain
    compile(domain, "acme_keys.py", "exec")
    assert "import boto3" in _write_at(fix, "src/infrastructure/acme_keys_adapter.py")["content"]


# --- advice in words ------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool, arguments, action, expect",
    [
        ("Write", {"file_path": ".claude/settings.json", "content": "{}"}, "FILE_WRITE", "threefold_install.py"),
        ("Write", {"file_path": ".codex/hooks.json", "content": "{}"}, "FILE_WRITE", "threefold_install.py"),
        ("Bash", {"command": "gi" + "t commit --no-verify -m wip"}, "COMMAND_EXEC", "--no-verify"),
        ("Bash", {"command": "gi" + "t -c core.hooksPath=/dev/null commit -m wip"}, "COMMAND_EXEC", "core.hooksPath"),
        ("Bash", {"command": "echo '{}' > .claude/settings.json"}, "COMMAND_EXEC", "threefold_install.py"),
        # `.git` is matched before the hooks check, and the advice is still about hooks.
        ("Write", {"file_path": ".git/hooks/pre-commit", "content": "exit 0"}, "FILE_WRITE", "threefold_install.py"),
        ("Write", {"file_path": ".env", "content": "ACME=1"}, "FILE_WRITE", "environment"),
        ("Read", {"file_path": "deploy/acme.pem"}, "FILE_READ", "operator"),
    ],
)
def test_a_protected_path_gets_the_governed_alternative_in_words(tool, arguments, action, expect) -> None:
    fix = _fix(*_refused(tool, arguments, action=action))
    assert fix["kind"] == "protected_path"
    assert fix["validated"] is False and fix["writes"] == [] and fix["checks"] == []
    assert expect in fix["summary"] + " ".join(fix["steps"])


def test_a_destructive_command_gets_advice_and_no_command() -> None:
    fix = _fix(*_refused("Bash", {"command": "rm -rf / --no-preserve-root"}, action="COMMAND_EXEC"))
    assert fix["kind"] == "destructive_command"
    assert fix["validated"] is False and fix["writes"] == []


# --- through the real evaluator ----------------------------------------------------------


def _request(session: str, tool: str, arguments: Dict[str, Any], action: str, **extra: Any) -> ToolCallRequestDTO:
    return ToolCallRequestDTO(
        session_id=session,
        developer_id="anonymous",
        project_name="Acme-Fixes",
        tool_name=tool,
        action_type=action,
        arguments=arguments,
        projected_input_tokens=extra.pop("input_tokens", 100),
        projected_output_tokens=50,
        budget_usd=extra.pop("budget", 5.0),
        **extra,
    )


def test_the_evaluators_own_verdict_is_accepted_as_it_is() -> None:
    evaluator = GovernanceEvaluator()
    request = _request("fix-e2e-layering", "Write", {"file_path": "src/domain/acme_order.py", "content": "import boto3\n"}, "FILE_WRITE")
    result = evaluator.evaluate_tool_call(request)
    assert result.status == "BLOCKED_BOUNDARY_VIOLATION"

    fix = _fix(request, result, evaluator.rules_in_force("Acme-Fixes")[0])

    assert fix["kind"] == "layering"
    _assert_really_passes(fix)


def test_a_hook_loop_is_named_and_never_validated() -> None:
    evaluator = GovernanceEvaluator()
    arguments = {"command": "make acme-build"}
    results = [
        evaluator.evaluate_tool_call(_request("fix-e2e-loop", "Bash", arguments, "COMMAND_EXEC", origin="hook"))
        for _ in range(3)
    ]
    assert results[-1].status == "BLOCKED_LOOP_DETECTED"

    fix = _fix(_request("fix-e2e-loop", "Bash", arguments, "COMMAND_EXEC", origin="hook"), results[-1])

    assert fix["kind"] == "loop"
    assert fix["validated"] is False and fix["writes"] == [] and fix["checks"] == []
    assert "Bash" in fix["summary"] and "3 times" in fix["summary"]
    assert "judged normally" in fix["summary"]


def test_a_halted_session_and_its_next_call_are_told_how_it_resumes() -> None:
    evaluator = GovernanceEvaluator()
    arguments = {"command": "make acme-build"}
    halting = None
    for _ in range(3):
        halting = evaluator.evaluate_tool_call(_request("sim-fix-halt", "Bash", arguments, "COMMAND_EXEC", origin="page"))
    loop_fix = _fix(_request("sim-fix-halt", "Bash", arguments, "COMMAND_EXEC"), halting)
    assert loop_fix["kind"] == "loop" and "halted" in loop_fix["summary"]

    after = evaluator.evaluate_tool_call(_request("sim-fix-halt", "Read", {"file_path": "README.md"}, "FILE_READ"))
    fix = _fix(_request("sim-fix-halt", "Read", {"file_path": "README.md"}, "FILE_READ"), after)
    assert fix["kind"] == "halted_session"
    assert any("/sessions/sim-fix-halt/resume" in step for step in fix["steps"])


def test_a_spent_budget_is_budget_advice() -> None:
    evaluator = GovernanceEvaluator()
    request = _request("fix-e2e-budget", "Read", {"file_path": "README.md"}, "FILE_READ", input_tokens=5_000_000, budget=1000.0)
    result = evaluator.evaluate_tool_call(request)
    assert result.status == "BLOCKED_CIRCUIT_BREAKER"

    fix = _fix(request, result)

    assert fix["kind"] == "budget" and fix["validated"] is False
    assert "per-call cap" in fix["summary"]


def test_a_dry_run_gets_the_fix_it_would_have_needed() -> None:
    evaluator = GovernanceEvaluator()
    request = _request("fix-e2e-dry", "Write", {"file_path": "src/domain/acme_dry.py", "content": "import boto3\n"}, "FILE_WRITE", dry_run=True)
    result = evaluator.evaluate_tool_call(request)
    assert result.status == "APPROVED" and result.observations

    fix = _fix(request, result)

    assert fix["kind"] == "layering"
    _assert_really_passes(fix)


def test_a_rule_in_observe_mode_gets_a_fix_that_it_would_not_flag_either() -> None:
    watching = [dict(DEFAULT_RULES[0], mode="observe")]
    request, result = _judge("Write", {"file_path": "src/domain/acme_watch.py", "content": "import boto3\n"}, watching)
    assert result["status"] == "APPROVED"
    result["observed_rules"] = ["python-domain-stays-pure"]
    result["observations"] = ["Layering rule 'python-domain-stays-pure' would refuse this write"]

    fix = _fix(request, result, watching)

    assert fix["kind"] == "layering"
    _assert_really_passes(fix, watching)


def test_an_approval_has_nothing_to_fix() -> None:
    request, result = _judge("Write", {"file_path": "src/domain/acme_clean.py", "content": "import json\n"})
    assert propose_fix(request, result, DEFAULT_RULES) is None
    repeat = dict(result, observations=["Repeat of a read or poll, recorded rather than refused: ..."])
    assert propose_fix(request, repeat, DEFAULT_RULES) is None


def test_a_rule_key_on_the_verdict_decides_the_family() -> None:
    request = {"tool_name": "Bash", "action_type": "COMMAND_EXEC", "arguments": {"command": "make acme"}}
    fix = _fix(request, {"status": "BLOCKED_LOOP_DETECTED", "rule_key": "LOOP", "reason": "Monomorphic loop detected"})
    assert fix["kind"] == "loop"


# --- the answer's own properties -----------------------------------------------------------


def test_files_over_the_cap_are_described_not_included() -> None:
    body = "".join(f"\n\ndef acme_rule_{index}(order):\n    return order.total * {index}\n" for index in range(300))
    content = "import boto3\n" + body
    request, result = _refused("Write", {"file_path": "src/domain/acme_big.py", "content": content})

    capped = propose_fix(request, result, DEFAULT_RULES)
    _assert_safe_to_show(capped)
    assert capped["validated"] is True, "Leaving the files out does not skip checking them"
    assert "writes" not in capped
    assert any(str(MAX_WRITE_BYTES) in step and "not included" in step for step in capped["steps"])
    assert "not included" in capped["summary"]

    full = _fix(request, result)
    _assert_really_passes(full)


def test_the_same_refusal_always_gets_the_same_fix() -> None:
    path, content, _, _, _ = SHIPPED_CASES["web-domain-stays-pure"]
    request, result = _refused("Write", {"file_path": path, "content": content})
    assert _fix(request, result) == _fix(request, result)


@pytest.mark.parametrize(
    "request_value, result_value",
    [
        (None, None),
        ("not a request", "not a result"),
        ({"arguments": "garbage"}, {"status": "BLOCKED_BOUNDARY_VIOLATION"}),
        ({"tool_name": "Write", "action_type": "NOT_A_TYPE", "arguments": {"file_path": 7}}, {"status": "BLOCKED_SECRET_DETECTED"}),
        ({"tool_name": "Bash", "arguments": {"command": ["echo", 3]}}, {"rule_evaluations": {"ARCHITECTURAL_BOUNDARY_SAFE": False}}),
    ],
)
def test_nothing_it_is_handed_makes_it_raise(request_value: Any, result_value: Any) -> None:
    fix = propose_fix(request_value, result_value, DEFAULT_RULES)
    assert fix is None or isinstance(fix, dict)


def test_a_phrasing_hook_changes_only_the_summary() -> None:
    path, content, _, _, _ = SHIPPED_CASES["python-domain-stays-pure"]
    request, result = _refused("Write", {"file_path": path, "content": content})
    plain = _fix(request, result)
    worded = _fix(request, result, phrase=lambda fix: "Move boto3 behind a port.\nThe adapter holds it. " + "x" * 400)
    assert worded["summary"].startswith("Move boto3 behind a port. The adapter holds it.")
    assert len(worded["summary"]) <= MAX_SUMMARY_CHARS
    assert {key: value for key, value in worded.items() if key != "summary"} == {key: value for key, value in plain.items() if key != "summary"}

    def broken(fix: Dict[str, Any]) -> str:
        raise RuntimeError("model unavailable")

    assert _fix(request, result, phrase=broken)["summary"] == plain["summary"]


def test_the_import_readers_it_reuses_are_still_there() -> None:
    """The fix finds statements with the gate's own patterns; a rename there must fail here, loudly."""
    for name in ("_PYTHON_FALLBACK", "_JAVA", "_CSHARP", "_TS_FROM", "_TS_BARE", "_TS_CALL", "_LINE_COMMENT", "_HASH_COMMENT", "PYTHON_PARSE_LIMIT"):
        assert hasattr(import_readers, name), name


# --- every refusal in the suite ------------------------------------------------------------
#
# The fixtures other tests use to prove the gate refuses something are the best
# corpus there is of what agents actually get refused for. They are read here as
# data, straight out of the test files, so a refusal added anywhere in the suite
# is covered without anyone remembering to add it.

TESTS_ROOT = Path(__file__).resolve().parents[1]
WRITE_WORDS = (">", "tee", "sed", "perl", "cp ", "mv ", "open(", "patch", "apply", "install ", "dd ", "rsync", "write", "Set-Content", "Out-File")


def _string_constants(tree: ast.AST) -> Dict[str, str]:
    """Module-level NAME = "..." assignments, so a tuple naming a constant can be read."""
    found: Dict[str, str] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value.value
    return found


def _text(node: ast.AST, names: Dict[str, str]) -> Optional[str]:
    """A string the source spells out: a literal, a module constant, or `"a" + "b"` and `"a" * 36` of them.

    The suite builds credentials by concatenation so that no file holds one
    whole, so reading only literals would miss exactly the fixtures that matter.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left, names), _text(node.right, names)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        text = _text(node.left, names)
        count = node.right.value if isinstance(node.right, ast.Constant) and isinstance(node.right.value, int) else None
        return text * count if text is not None and count is not None and 0 <= count <= 1000 else None
    return None


def _harvest() -> Tuple[List[Tuple[str, str, str]], List[Tuple[str, str]], List[Tuple[str, str]]]:
    writes: Dict[Tuple[str, str], str] = {}
    commands: Dict[str, str] = {}
    secrets: Dict[str, str] = {}
    for file in sorted(TESTS_ROOT.rglob("test_*.py")):
        if file.name == Path(__file__).name:
            continue
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        names = _string_constants(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                entries = {}
                for key, value in zip(node.keys, node.values):
                    key_text = _text(key, names) if key is not None else None
                    value_text = _text(value, names)
                    if key_text is not None and value_text is not None:
                        entries[key_text.lower()] = value_text
                path = next((entries[key] for key in PATH_KEYS if key in entries), None)
                content = next((entries[key] for key in CONTENT_KEYS if key in entries), None)
                if path and content is not None:
                    writes.setdefault((path, content), file.name)
                command = entries.get("command")
                if command:
                    commands.setdefault(command, file.name)
            elif isinstance(node, ast.Call):
                keywords = {item.arg: _text(item.value, names) for item in node.keywords if item.arg}
                if keywords.get("path") and keywords.get("content") is not None:
                    writes.setdefault((keywords["path"], keywords["content"]), file.name)
            elif isinstance(node, (ast.Tuple, ast.List)):
                texts = [text for text in (_text(element, names) for element in node.elts) if text is not None]
                for first in texts:
                    if looks_like_path(first) and rules_for_path(first, DEFAULT_RULES):
                        for second in texts:
                            if second is not first:
                                writes.setdefault((first, second), file.name)
            value = _text(node, names) if isinstance(node, (ast.Constant, ast.BinOp)) else None
            if value is not None and len(value) < 5000:
                if "domain" in value.lower() and any(word in value for word in WRITE_WORDS):
                    commands.setdefault(value, file.name)
                if not SecretScanner.scan_payload(value)[0]:
                    secrets.setdefault(value, file.name)
    return (
        [(path, content, origin) for (path, content), origin in writes.items()],
        list(commands.items()),
        list(secrets.items()),
    )


def _corpus() -> Iterator[Tuple[str, dict, dict]]:
    writes, commands, secrets = _harvest()
    for path, content, origin in writes:
        request, result = _judge("Write", {"file_path": path, "content": content})
        if result["status"] != "APPROVED":
            yield origin, request, result
    for command, origin in commands:
        request, result = _judge("Bash", {"command": command}, action="COMMAND_EXEC")
        if result["status"] != "APPROVED":
            yield origin, request, result
    # Every credential-shaped string the suite holds, written into a Python file
    # and run as a command: the shapes the scanner was taught are the shapes a
    # fix must replace without ever repeating them.
    for text, origin in secrets:
        for tool, arguments, action in (
            ("Write", {"file_path": "src/acme/harvested_settings.py", "content": text}, "FILE_WRITE"),
            ("Bash", {"command": text}, "COMMAND_EXEC"),
        ):
            request, result = _judge(tool, arguments, action=action)
            if result["status"] != "APPROVED":
                yield origin, request, result


def _family(reason: str) -> str:
    if "Sensitive credential detected" in reason:
        return "credential"
    if "hooks" in reason or "protected by architectural governance" in reason or "protected path" in reason:
        return "protected_path"
    if UNREADABLE_WRITE in reason:
        return "unreadable_write"
    if "destructive operation" in reason:
        return "destructive_command"
    return "layering"


def test_every_refusal_in_the_suite_gets_a_fix_and_every_validated_one_passes_the_gate() -> None:
    seen = 0
    validated = 0
    layering_writes = 0
    layering_writes_validated = 0
    kinds: Dict[str, int] = {}
    for origin, request, result in _corpus():
        seen += 1
        fix = propose_fix(request, result, DEFAULT_RULES, max_write_bytes=UNLIMITED)
        assert fix is not None, f"{origin}: no fix for a refusal: {result['reason'][:160]}"
        _assert_safe_to_show(fix)
        expected = _family(result["reason"])
        assert fix["kind"] == expected, f"{origin}: the fix calls a {expected} refusal {fix['kind']}: {result['reason'][:160]}"
        kinds[fix["kind"]] = kinds.get(fix["kind"], 0) + 1
        writes_layering = fix["kind"] == "layering" and request["tool_name"] == "Write"
        layering_writes += writes_layering
        if not fix["validated"]:
            assert fix.get("writes", []) == [], f"{origin}: an unvalidated fix handed out code"
            continue
        validated += 1
        assert fix.get("writes") or fix["kind"] == "credential", f"{origin}: validated with nothing to write"
        for write in fix.get("writes", []):
            passed, reason = _passes_gate(write, DEFAULT_RULES)
            assert passed, f"{origin}: a validated fix is refused at {write['path']}: {reason}"
        layering_writes_validated += writes_layering
    # Floors, not exact counts, set near half of what the suite held when this
    # was written (168 refusals, 110 validated). Other tracks add refusals, and
    # each only makes this stronger; a collapse means the harvest broke, which
    # would make the loop above pass by proving nothing.
    assert seen >= 80, f"only {seen} refusals were found in the suite; the harvest is broken"
    assert kinds.get("layering", 0) >= 35 and kinds.get("unreadable_write", 0) >= 15, kinds
    assert kinds.get("credential", 0) >= 10 and kinds.get("protected_path", 0) >= 5, kinds
    assert validated >= 50, f"only {validated} of {seen} refusals got a validated fix"
    # A Write refused by a shipped rule is the case this exists for: its content
    # is all there, and every shipped rule names a layer to move the import to.
    assert layering_writes >= 12 and layering_writes_validated >= 0.8 * layering_writes, (layering_writes_validated, layering_writes)
