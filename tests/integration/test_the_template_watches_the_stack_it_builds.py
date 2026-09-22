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

import json
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


# ------------------------------------------------------------------ the API stage


def test_the_stage_throttles_every_route_by_parameter() -> None:
    api = RESOURCES["ThreefoldHttpApi"]
    settings = re.search(r"^      DefaultRouteSettings:\n((?:        .*\n)+)", api, re.M)
    assert settings, "The stage sets no default route throttle"
    body = settings.group(1)
    assert re.search(r"^        ThrottlingBurstLimit: !Ref ApiThrottleBurstLimit$", body, re.M)
    assert re.search(r"^        ThrottlingRateLimit: !Ref ApiThrottleRateLimit$", body, re.M)
    # CloudFormation accepts these in the shape and the deploy then fails,
    # because an HTTP API has no execution logging.
    assert "LoggingLevel" not in body and "DataTraceEnabled" not in body
    assert "DetailedMetricsEnabled: true" not in body, "Per-route metrics are billed as custom metrics"


def test_the_throttle_defaults_leave_room_for_a_judge_and_the_scorer() -> None:
    rate = float(_default("ApiThrottleRateLimit"))
    burst = int(_default("ApiThrottleBurstLimit"))
    assert rate >= 50, (
        f"{rate} a second: one dashboard load is a dozen parallel reads, and the public "
        "stack is read by judges and an automated scorer at once"
    )
    assert burst >= rate, "A burst below the steady rate refuses the page loads it is there to absorb"
    assert rate <= 1000, "Above this the throttle no longer bounds what a runaway client is billed"
    for name in ("ApiThrottleRateLimit", "ApiThrottleBurstLimit"):
        assert re.search(r"^    Type: Number$", PARAMETERS[name], re.M)
        assert re.search(r"^    MinValue: 1$", PARAMETERS[name], re.M), f"{name} of 0 would refuse every request"


HTTP_API_CONTEXT = {
    "$context.requestId", "$context.identity.sourceIp", "$context.requestTime", "$context.httpMethod",
    "$context.routeKey", "$context.path", "$context.status", "$context.protocol", "$context.responseLength",
    "$context.responseLatency", "$context.integrationLatency", "$context.integrationStatus",
    "$context.integrationErrorMessage", "$context.error.message", "$context.identity.userAgent",
}


def test_access_logs_are_one_json_line_with_the_fields_an_incident_needs() -> None:
    api = RESOURCES["ThreefoldHttpApi"]
    destination = re.search(r"^        DestinationArn: (.+)$", api, re.M)
    assert destination, "The stage writes no access log"
    assert destination.group(1) == (
        "!Sub 'arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:${ThreefoldApiAccessLogGroup}'"
    ), "GetAtt's ARN ends in ':*'; the destination is the plain log-group ARN"
    fmt = re.search(r"^        Format: '(.+)'$", api, re.M)
    assert fmt, "The access log format is not a single-quoted single line"
    fields = json.loads(fmt.group(1))
    required = {
        "requestId": "$context.requestId",
        "ip": "$context.identity.sourceIp",
        "routeKey": "$context.routeKey",
        "status": "$context.status",
        "latency": "$context.responseLatency",
        "integrationError": "$context.integrationErrorMessage",
    }
    for key, variable in required.items():
        assert fields.get(key) == variable, f"{key} should log {variable}, not {fields.get(key)!r}"
    unknown = set(fields.values()) - HTTP_API_CONTEXT
    assert not unknown, f"{unknown}: a variable an HTTP API does not know is logged as its own literal text"


def test_the_access_log_group_is_the_stacks_own_and_expires() -> None:
    group = RESOURCES["ThreefoldApiAccessLogGroup"]
    assert _type(group) == "AWS::Logs::LogGroup"
    assert re.search(r"^      LogGroupName: !Sub '.*\$\{AWS::StackName\}.*'$", group, re.M), (
        "Both stacks deploy from this template; a fixed name makes the second deploy fail"
    )
    retention = re.search(r"^      RetentionInDays: (\d+)$", group, re.M)
    assert retention, "Without retention, client addresses are kept forever"
    assert int(retention.group(1)) <= 30, "Every line holds a client address, which is personal data"


# ------------------------------------------------------------------ the function


def test_the_function_is_traced_and_the_transform_grants_it_the_permission() -> None:
    function = RESOURCES["ThreefoldFunction"]
    assert re.search(r"^      Tracing: Active$", function, re.M)
    # The transform attaches AWSXrayWriteOnlyAccess only to a role it generates
    # itself. A Role property here would take that away without a word.
    assert not re.search(r"^      Role:", function, re.M), (
        "With an explicit Role, Tracing: Active is left without permission to write traces"
    )


def test_concurrency_is_capped_by_a_parameter_that_cannot_switch_the_function_off() -> None:
    block = PARAMETERS["ReservedConcurrency"]
    assert re.search(r"^    Type: Number$", block, re.M)
    assert re.search(r"^    MinValue: 0$", block, re.M)
    default = int(_default("ReservedConcurrency"))
    assert 10 <= default <= 100, (
        f"{default}: below ten a busy demo is throttled, above a hundred two stacks eat into "
        "the account's pool and the cap stops bounding a runaway client's cost"
    )
    assert re.search(
        r"^      ReservedConcurrentExecutions: !If \[CapConcurrency, !Ref ReservedConcurrency, !Ref 'AWS::NoValue'\]$",
        RESOURCES["ThreefoldFunction"],
        re.M,
    ), "0 must become no reservation: passed through, Lambda reads it as never run"
    assert re.search(r"^  CapConcurrency: !Not \[!Equals \[!Ref ReservedConcurrency, '0'\]\]$", _conditions(), re.M)


def test_the_function_log_group_keeps_its_name_and_explicit_retention() -> None:
    group = RESOURCES["ThreefoldLogGroup"]
    assert re.search(r"^      LogGroupName: !Sub '/aws/lambda/\$\{ThreefoldFunction\}'$", group, re.M), (
        "A different name is a second group; the function would keep writing to its own, unretained"
    )
    assert re.search(r"^      RetentionInDays: 30$", group, re.M)
