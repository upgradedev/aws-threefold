"""The API stack hands the edge's secret to the function, and trusts nothing without it.

deploy/template.yml gains one parameter, EdgeOriginSecret, passed to the
function as THREEFOLD_EDGE_SECRET. It defaults to empty so an update of either
live stack changes nothing until the owner sets it, and with it empty the
middleware ignores every header only the edge may set. The two templates must
agree on what a secret looks like, or a value one accepts would fail the other's
deploy, or reach the function and never match.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from threefold.infrastructure import security_middleware

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = (ROOT / "deploy" / "template.yml").read_text(encoding="utf-8")


def _edge_template() -> dict:
    name = "edge_cfn_yaml_reader"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / "unit" / "test_edge_cfn_yaml.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name].load_edge_template()


def _parameter(name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \w|^\S)", TEMPLATE, re.S | re.M)
    assert match, f"deploy/template.yml declares no {name}"
    return match.group(1)


def _allowed_pattern() -> str:
    match = re.search(r"^    AllowedPattern: '(.+)'$", _parameter("EdgeOriginSecret"), re.M)
    assert match, "EdgeOriginSecret has no quoted AllowedPattern"
    return match.group(1)


def test_the_secret_is_a_hidden_parameter_that_defaults_to_no_edge() -> None:
    block = _parameter("EdgeOriginSecret")
    assert re.search(r"^    Type: String$", block, re.M)
    assert re.search(r"^    Default: ''$", block, re.M), "an update that does not set it must change nothing"
    assert re.search(r"^    NoEcho: true$", block, re.M), "describe-stacks would print it"


def test_the_secret_reaches_the_function_under_the_name_the_code_reads() -> None:
    variables = re.search(r"^      Variables:\n(.*?)^Resources:", TEMPLATE, re.S | re.M).group(1)
    assert re.search(
        rf"^        {re.escape(security_middleware.EDGE_SECRET_ENV)}: !Ref EdgeOriginSecret$", variables, re.M
    ), "declared under one name and read under another, it would silently do nothing"


SAMPLES = [
    "",
    "a" * 31,
    "a" * 32,
    "acmeSyntheticEdgeSecret_0123456789-abcdefghijklmnopqrstuvwxyzAB",
    "b" * 256,
    "b" * 257,
    "acme synthetic edge secret with spaces 0123456789",
    "acme-synthetic-edge-secret-0123456789-abcdef\n",
    "acme-synthetic-edge-secret-0123456789-abcdef=",
]


@pytest.mark.parametrize("value", SAMPLES)
def test_both_templates_accept_the_same_secrets(value: str) -> None:
    """Java's regex, as CloudFormation uses, reads these patterns as Python's does."""
    edge = _edge_template()["Parameters"]["EdgeOriginSecret"]
    edge_accepts = (
        bool(re.fullmatch(edge["AllowedPattern"], value)) and edge["MinLength"] <= len(value) <= edge["MaxLength"]
    )
    api_accepts = bool(re.fullmatch(_allowed_pattern(), value))
    assert edge_accepts == (api_accepts and value != ""), "only the API stack may run with no edge"


def test_every_secret_the_api_template_accepts_is_one_the_function_trusts(monkeypatch) -> None:
    """A secret too short for the middleware must be refused by the deploy, not ignored at run time."""
    for value in SAMPLES:
        if value and re.fullmatch(_allowed_pattern(), value):
            assert len(value) >= security_middleware.MIN_EDGE_SECRET_LENGTH
            monkeypatch.setenv(security_middleware.EDGE_SECRET_ENV, value)
            assert security_middleware.edge_request_is_trusted({"x-threefold-edge": value})


def test_the_default_trusts_no_edge_header(monkeypatch) -> None:
    monkeypatch.setenv(security_middleware.EDGE_SECRET_ENV, "")
    headers = {"x-threefold-edge": "", "cloudfront-viewer-address": "198.51.100.10:1"}
    assert not security_middleware.edge_request_is_trusted(headers)
    assert security_middleware.rate_limit_key(headers, "192.0.2.200") == "192.0.2.200"
