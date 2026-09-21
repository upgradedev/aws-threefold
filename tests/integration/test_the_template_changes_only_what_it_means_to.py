"""The stack a parameter update must not disturb, and the values the code reads.

Two new parameters reach the function as environment variables. Both have to
default to what the stack already did, or updating the live stack to pick up
this change would quietly relabel every project or close every read. The names
are checked against the constants the code reads, because a variable declared
under one name and read under another is a setting that silently does nothing.
The API's logical id and its stage name are pinned: renaming either replaces
the API, and with it the public URL every artifact points at.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from threefold.application.labels import DEFAULT_PROJECT_PATTERN, PROJECT_PATTERN_ENV
from threefold.infrastructure.security_middleware import PUBLIC_READS_ENV, reads_are_public

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "deploy" / "template.yml"
TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")


def _parameters() -> dict[str, str]:
    """The Parameters section, as a map of name to the text declaring it."""
    section = re.search(r"^Parameters:\n(.*?)^\w", TEMPLATE, re.S | re.M)
    assert section, "The template has no Parameters section"
    blocks: dict[str, str] = {}
    name = None
    for line in section.group(1).splitlines():
        heading = re.match(r"^  (\w+):\s*$", line)
        if heading:
            name = heading.group(1)
            blocks[name] = ""
        elif name:
            blocks[name] += line + "\n"
    return blocks


def _environment() -> str:
    section = re.search(r"^      Variables:\n(.*?)^Resources:", TEMPLATE, re.S | re.M)
    assert section, "The template declares no function environment"
    return section.group(1)


@pytest.mark.parametrize("name", ["AllowedProjectPattern", "PublicReads"])
def test_the_new_parameters_exist_and_reach_the_function(name: str) -> None:
    assert name in _parameters(), f"{name} is not a parameter of this template"
    assert f"!Ref {name}" in _environment(), f"{name} is declared and never passed to the function"


def test_every_parameter_has_a_default_so_an_update_changes_nothing_else() -> None:
    """`sam deploy` on the live stack must not stop to ask, or change behaviour."""
    for name, block in _parameters().items():
        assert re.search(r"^    Default:", block, re.M), f"{name} has no default"


def test_the_pattern_the_stack_deploys_is_the_one_the_code_falls_back_to() -> None:
    """Two defaults that drift label projects differently on the stack and off it."""
    declared = re.search(r"^    Default: '(.+)'$", _parameters()["AllowedProjectPattern"], re.M)
    assert declared, "AllowedProjectPattern has no quoted default"
    assert declared.group(1) == DEFAULT_PROJECT_PATTERN


def test_reads_are_public_by_default_on_the_stack_and_in_the_code() -> None:
    block = _parameters()["PublicReads"]
    assert re.search(r"^    Default: 'true'$", block, re.M)
    assert re.search(r"^    AllowedValues: \['true', 'false'\]$", block, re.M), (
        "Anything but these two would be read as 'not false', which is public"
    )
    assert reads_are_public(), "With the variable unset, the code has to agree with the default"


@pytest.mark.parametrize("variable", [PROJECT_PATTERN_ENV, PUBLIC_READS_ENV])
def test_the_variable_the_template_sets_is_the_one_the_code_reads(variable: str) -> None:
    assert re.search(rf"^        {variable}: !Ref \w+$", _environment(), re.M), (
        f"{variable} is read by the code and not set by the template"
    )


def test_the_endpoint_output_carries_the_trailing_slash() -> None:
    """API Gateway answers the bare /prod with its own Not Found, before the function."""
    output = re.search(r"^  ApiEndpoint:\n(.*?)^  \w", TEMPLATE, re.S | re.M)
    assert output, "The template publishes no ApiEndpoint"
    value = re.search(r"Value: !Sub '(.+)'", output.group(1)).group(1)
    assert value.endswith("/prod/"), f"{value} is the link every artifact points at"


def test_the_api_and_its_stage_are_not_renamed() -> None:
    """Renaming either replaces the API, and the public URL with it."""
    assert re.search(r"^  ThreefoldHttpApi:\n    Type: AWS::Serverless::HttpApi$", TEMPLATE, re.M)
    assert re.search(r"^      StageName: prod$", TEMPLATE, re.M)
    assert "${ThreefoldHttpApi}" in TEMPLATE, "The output must name the API it belongs to"
