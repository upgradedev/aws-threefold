"""The demo fleet's schedule: created only where DemoFleet is true, and able to do one thing.

The public stack runs the synthetic Acme fleet (src/threefold/application/
demo_fleet.py) from an EventBridge Scheduler schedule. What is pinned here:
the parameter defaults to false, so no existing stack changes on update; the
schedule and its role exist only under the condition; the schedule's input is
exactly the event the handler recognises, read out of the template and handed
to the handler's own check; the interval is the fleet's tick; the flexible
window is off, written so YAML cannot read it as a boolean; nothing retries a
tick, neither the scheduler nor Lambda's own handling of the asynchronous
invocation; the role may invoke this function and nothing else, assumed only
by the scheduler for this account; and the function is told, as DEMO_FLEET,
whether its stack runs the fleet at all.

Read as text, as the other template tests read it, because CloudFormation's
short tags would need a loader of their own. Nothing calls AWS.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from threefold.application import demo_fleet

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = (ROOT / "deploy" / "template.yml").read_text(encoding="utf-8")


def _block(section: str, name: str) -> str:
    """The text under one two-space key of a top-level section."""
    body = re.search(rf"^{section}:\n(.*?)(?=^\S|\Z)", TEMPLATE, re.S | re.M)
    assert body, f"the template has no {section} section"
    found = re.search(rf"^  {name}:\n(.*?)(?=^  \w|\Z)", body.group(1), re.S | re.M)
    assert found, f"{section} has no {name}"
    return found.group(1)


SCHEDULE = _block("Resources", "DemoFleetSchedule")
ROLE = _block("Resources", "DemoFleetScheduleRole")
INVOKE = _block("Resources", "DemoFleetInvokeConfig")
FUNCTION = _block("Resources", "ThreefoldFunction")
FLEET_RESOURCES = ("DemoFleetSchedule", "DemoFleetScheduleRole", "DemoFleetInvokeConfig")


def test_the_parameter_is_off_unless_the_owner_turns_it_on() -> None:
    parameter = _block("Parameters", "DemoFleet")
    assert re.search(r"^    Type: String$", parameter, re.M)
    assert re.search(r"^    Default: 'false'$", parameter, re.M), "An update of an existing stack must change nothing"
    assert re.search(r"^    AllowedValues: \['true', 'false'\]$", parameter, re.M)


def test_the_function_is_told_whether_its_stack_runs_the_fleet() -> None:
    """Under the name the code reads: the six fleet names are synthetic there only, and a tick runs there only."""
    variables = re.search(r"^      Variables:\n(.*?)^Resources:", TEMPLATE, re.S | re.M)
    assert variables, "The template declares no function environment"
    assert re.search(
        rf"^        {re.escape(demo_fleet.rollups.DEMO_FLEET_ENV)}: !Ref DemoFleet$", variables.group(1), re.M
    ), "declared under one name and read under another, a private stack would count its Acme-Payments as synthetic"


def test_the_condition_is_the_parameter_being_true() -> None:
    conditions = re.search(r"^Conditions:\n(.*?)(?=^\S|\Z)", TEMPLATE, re.S | re.M).group(1)
    assert re.search(r"^  RunDemoFleet: !Equals \[!Ref DemoFleet, 'true'\]$", conditions, re.M)


@pytest.mark.parametrize(
    "block, resource_type",
    [(SCHEDULE, "AWS::Scheduler::Schedule"), (ROLE, "AWS::IAM::Role"), (INVOKE, "AWS::Lambda::EventInvokeConfig")],
)
def test_the_schedule_and_its_role_exist_only_under_the_condition(block: str, resource_type: str) -> None:
    assert re.match(rf"^    Type: {re.escape(resource_type)}\n    Condition: RunDemoFleet\n", block)


def test_nothing_unconditional_refers_to_them() -> None:
    """A reference from a resource or an output that always exists fails the stack where DemoFleet is false."""
    resources = re.search(r"^Resources:\n(.*?)(?=^\S|\Z)", TEMPLATE, re.S | re.M).group(1)
    for name, body in re.findall(r"^  (\w+):\n(.*?)(?=^  \w|\Z)", resources, re.S | re.M):
        if re.search(r"^    Condition: RunDemoFleet$", body, re.M):
            continue
        # Comments are not references, and the comment above a resource reads
        # as part of the one before it.
        code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
        for fleet in FLEET_RESOURCES:
            assert fleet not in code, f"{name} refers to {fleet}"
    outputs = re.search(r"^Outputs:\n(.*?)(?=^\S|\Z)", TEMPLATE, re.S | re.M).group(1)
    assert "DemoFleet" not in outputs
    conditional = re.findall(r"^  (\w+):\n    Type: [^\n]+\n    Condition: RunDemoFleet$", resources, re.M)
    assert sorted(conditional) == sorted(FLEET_RESOURCES), "a fleet resource was added without being checked here"


def test_the_schedule_sends_exactly_the_event_the_handler_runs_a_tick_for() -> None:
    sent = re.search(r"^        Input: '(.+)'$", SCHEDULE, re.M)
    assert sent, "the schedule's target has no Input"
    event = json.loads(sent.group(1))
    assert event == {"threefold_fleet": {"tick": 1}}
    assert demo_fleet.is_tick_event(event), "The handler would route the schedule's event as an HTTP request"


def test_the_schedule_targets_this_function_with_its_own_role() -> None:
    assert re.search(r"^        Arn: !GetAtt ThreefoldFunction\.Arn$", SCHEDULE, re.M)
    assert re.search(r"^        RoleArn: !GetAtt DemoFleetScheduleRole\.Arn$", SCHEDULE, re.M)


def test_the_schedule_ticks_every_fifteen_minutes_on_time() -> None:
    expression = re.search(r"^      ScheduleExpression: rate\((\d+) minutes\)$", SCHEDULE, re.M)
    assert expression and int(expression.group(1)) * 60 == demo_fleet.TICK_SECONDS
    assert re.search(r"^      FlexibleTimeWindow:\n        Mode: 'OFF'$", SCHEDULE, re.M), (
        "Unquoted, YAML reads OFF as the boolean false and the schedule is refused"
    )
    assert re.search(r"^      State: ENABLED$", SCHEDULE, re.M)


def test_nothing_retries_a_tick() -> None:
    """A retried tick sends its batch twice; a missed one is only a quieter quarter hour."""
    assert re.search(r"^        RetryPolicy:\n          MaximumRetryAttempts: 0\n", SCHEDULE, re.M)
    # Lambda's own handling of the asynchronous invocation: by default two
    # retries after an error or a timeout, and up to six hours in the queue.
    assert re.search(r"^      MaximumRetryAttempts: 0$", INVOKE, re.M)
    assert re.search(r"^      MaximumEventAgeInSeconds: 60$", INVOKE, re.M), "60 is the least Lambda accepts"
    assert re.search(r"^      FunctionName: !Ref ThreefoldFunction$", INVOKE, re.M)
    timeout = re.search(r"^    Timeout: (\d+)$", TEMPLATE, re.M)
    assert timeout, "the function declares no timeout"
    budget = int(timeout.group(1)) - demo_fleet.STOP_BEFORE_DEADLINE_SECONDS
    assert budget >= 5, "The tick must stop sending with room left, or Lambda retries the timed-out invocation"
    assert demo_fleet.DEFAULT_BUDGET_SECONDS <= int(timeout.group(1)) - demo_fleet.STOP_BEFORE_DEADLINE_SECONDS


def test_the_schedule_is_named_for_its_stack_in_the_default_group() -> None:
    assert re.search(r"^      Name: !Sub '\$\{AWS::StackName\}-demo-fleet'$", SCHEDULE, re.M), (
        "A fixed name collides between two stacks deployed from this template"
    )
    assert not re.search(r"^      GroupName:", SCHEDULE, re.M)


def test_the_role_may_invoke_this_function_and_nothing_else() -> None:
    policies = re.search(r"^      Policies:\n(.*)", ROLE, re.S | re.M)
    assert policies, "the role has no inline policy"
    statements = re.findall(r"^              - Effect: (\w+)\n((?:                .*\n?)+)", policies.group(1), re.M)
    assert len(statements) == 1, statements
    effect, body = statements[0]
    assert effect == "Allow"
    assert re.search(r"^                Action: lambda:InvokeFunction$", body, re.M)
    assert re.search(r"^                Resource: !GetAtt ThreefoldFunction\.Arn$", body, re.M)
    assert body.count("Action") == 1 and body.count("Resource") == 1 and "*" not in body
    assert "ManagedPolicyArns" not in ROLE, "No managed policy, so the role holds exactly what is written here"
    assert not re.search(r"^      RoleName:", ROLE, re.M), "A named role would need CAPABILITY_NAMED_IAM"


def test_only_the_scheduler_acting_for_this_account_may_assume_the_role() -> None:
    trust = re.search(r"^      AssumeRolePolicyDocument:\n(.*?)(?=^      \w)", ROLE, re.S | re.M)
    assert trust
    text = trust.group(1)
    assert re.search(r"^              Service: scheduler\.amazonaws\.com$", text, re.M)
    assert re.search(r"^            Action: sts:AssumeRole$", text, re.M)
    assert re.search(r"^                aws:SourceAccount: !Ref AWS::AccountId$", text, re.M)
    assert text.count("Effect:") == 1


def test_the_retry_setting_covers_the_version_the_schedule_invokes() -> None:
    """The schedule invokes the unqualified function, which is $LATEST while no alias is published."""
    assert re.search(r"^      Qualifier: '\$LATEST'$", INVOKE, re.M)
    assert "AutoPublishAlias" not in FUNCTION, "With an alias, the schedule and this setting must both name it"
    assert re.search(r"^        Arn: !GetAtt ThreefoldFunction\.Arn$", SCHEDULE, re.M)


def test_the_schedule_is_the_function_s_only_asynchronous_caller() -> None:
    """So turning Lambda's retries off changes nothing but the tick: every other event is an HTTP API call, answered synchronously."""
    kinds = re.findall(r"^          Type: (\w+)$", FUNCTION, re.M)
    assert kinds and set(kinds) == {"HttpApi"}, kinds
    for asynchronous in ("AWS::Events::Rule", "AWS::Lambda::EventSourceMapping", "AWS::SNS::Subscription\n    Properties:\n      Protocol: lambda"):
        assert asynchronous not in TEMPLATE
    assert TEMPLATE.count("Type: AWS::Scheduler::Schedule") == 1
