"""The standalone pre-commit gate judges by the same layering rules as the live gate.

It used to call import_rules, a list of twelve Python package names that the
live gate had already replaced with declared rules in four languages. A Java
domain class importing JPA passed this script and was refused at runtime, which
is the drift the script's own docstring says it exists to prevent.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parents[2] / "scripts" / "pre-commit-gate.py"


def _gate(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GATE), "--file", str(path)], capture_output=True, text=True)


def _write(root: Path, relative: str, content: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def test_a_java_domain_class_reaching_persistence_is_refused_naming_the_rule(tmp_path: Path) -> None:
    order = _write(tmp_path, "src/main/java/com/acme/domain/Order.java", "package com.acme.domain;\nimport javax.persistence.Entity;\n")
    result = _gate(order)
    assert result.returncode == 1
    assert "java-domain-stays-pure" in result.stderr


def test_a_python_domain_module_importing_a_driver_is_refused_naming_the_rule(tmp_path: Path) -> None:
    user = _write(tmp_path, "src/acme/domain/user.py", "from boto3 import client\n")
    result = _gate(user)
    assert result.returncode == 1
    assert "python-domain-stays-pure" in result.stderr


def test_the_same_import_outside_domain_passes(tmp_path: Path) -> None:
    adapter = _write(tmp_path, "src/acme/infrastructure/store.py", "from boto3 import client\n")
    assert _gate(adapter).returncode == 0


def test_the_old_python_only_list_is_gone() -> None:
    assert not (GATE.parents[1] / "src" / "threefold" / "domain" / "import_rules.py").exists()
