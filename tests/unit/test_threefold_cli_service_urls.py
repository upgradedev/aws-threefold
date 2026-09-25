"""The pre-commit check fetches rules and stages from http(s) only.

Both helpers build their URL from the operator-configured endpoint and read
it with urlopen, which also speaks file:// and custom schemes. A URL naming
anything else is refused with a reason, and managed mode then judges as
Observe, as for a stack it cannot read.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load("threefold_cli")


def test_fetch_rules_refuses_a_non_http_endpoint() -> None:
    rules, why = cli.fetch_rules("file:///etc/", "Acme-Ledger", None, 1.0)
    assert rules is None
    assert "non-http(s)" in why


def test_managed_mode_refuses_a_non_http_endpoint() -> None:
    settings = SimpleNamespace(project="Acme-Ledger", endpoint="file:///etc/", api_key=None)
    stage, observing, why = cli.managed_mode(settings, 1.0)
    assert stage == "observe"
    assert observing == frozenset()
    assert "non-http(s)" in why
