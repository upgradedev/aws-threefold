"""What the template does to keep the stack it builds observable and bounded.

Point-in-time recovery, tracing, a concurrency cap, a stage throttle, access
logs, alarms, a dashboard and an optional budget. Each is asserted for what it
means rather than for being present: an alarm that reads a dimension the
service never publishes is a line in the template that can never fire, and a
throttle set below a judge's page load would fail the ship gate, so the tests
pin the metric names against the record the service actually writes, the
dimensions against the ones AWS publishes, and the defaults against the
traffic the public stack has to carry.

The template is read as text, as the other template tests do, because PyYAML
is not a dependency of this repository and CloudFormation's short tags would
need a custom loader anyway. Names are synthetic, as the clean-room rule
requires.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = (ROOT / "deploy" / "template.yml").read_text(encoding="utf-8")
HANDLERS = (ROOT / "src" / "threefold" / "interfaces" / "api_handlers.py").read_text(encoding="utf-8")

STACK_NAMESPACE = "Threefold/${AWS::StackName}"
PSEUDO_PARAMETERS = {"AWS::StackName", "AWS::Region", "AWS::AccountId", "AWS::Partition", "AWS::NoValue", "AWS::URLSuffix"}
TREAT_MISSING = {"breaching", "notBreaching", "ignore", "missing"}


# ------------------------------------------------------------------ reading the template


def _section(name: str) -> str:
    match = re.search(rf"^{name}:\n(.*?)(?=^\S|\Z)", TEMPLATE, re.S | re.M)
    assert match, f"The template has no {name} section"
    return match.group(1)


def _blocks(section: str) -> dict[str, str]:
    """A section as a map of each two-space key to the text beneath it."""
    blocks: dict[str, str] = {}
    name = None
    for line in section.splitlines():
        heading = re.match(r"^  ([A-Za-z0-9]+):\s*$", line)
        if heading:
            name = heading.group(1)
            blocks[name] = ""
        elif name:
            blocks[name] += line + "\n"
    return blocks


RESOURCES = _blocks(_section("Resources"))
PARAMETERS = _blocks(_section("Parameters"))


def _conditions() -> str:
    # Read when asked for rather than at import, so a test of the table does
    # not depend on the template having any conditions at all.
    return _section("Conditions")


def _type(block: str) -> str:
    match = re.match(r"^    Type: (\S+)$", block, re.M)
    assert match, "A resource without a Type"
    return match.group(1)


def _of_type(resource_type: str) -> dict[str, str]:
    return {name: block for name, block in RESOURCES.items() if _type(block) == resource_type}


def _default(parameter: str) -> str:
    match = re.search(r"^    Default: '?([^'\n]*)'?$", PARAMETERS[parameter], re.M)
    assert match, f"{parameter} has no default"
    return match.group(1)


# ------------------------------------------------------------------ the table


def test_the_table_has_point_in_time_recovery_encryption_and_keeps_its_ttl() -> None:
    table = RESOURCES["ThreefoldTable"]
    assert re.search(r"^      PointInTimeRecoverySpecification:\n        PointInTimeRecoveryEnabled: true$", table, re.M), (
        "Without point-in-time recovery a bad script or a deleted item on the ledger is permanent"
    )
    assert re.search(r"^      SSESpecification:\n        SSEEnabled: true$", table, re.M), (
        "SSEEnabled: true is the AWS managed key; dropping it silently moves the table to the owned key"
    )
    assert re.search(r"^      TimeToLiveSpecification:\n        AttributeName: ttl\n        Enabled: true$", table, re.M), (
        "Sessions, sign-in codes and rollups all expire through the ttl attribute"
    )
    assert re.search(r"^      BillingMode: PAY_PER_REQUEST$", table, re.M)
