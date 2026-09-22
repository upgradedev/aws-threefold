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
need a custom loader anyway. The operations resources are also read as trees
by a small reader below and compared whole, because a test that looks for one
line cannot see a statistic, an operator or a policy action changed next to
it. Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import copy
import functools
import inspect
import json
import re
import textwrap
import uuid
from pathlib import Path

import pytest

from threefold.infrastructure.metrics_emf import emit_threefold_emf_metrics

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


# ------------------------------------------------------------------ reading a resource as a tree
#
# A regular expression can say a line is present, but not that nothing else in
# the resource changed, so a mutated statistic or a widened policy statement
# next to the line it looks for passes unseen. The operations resources are
# therefore also read as trees and compared whole. This is just enough YAML for
# this template: block mappings and sequences, plain and single-quoted scalars,
# folded and literal blocks, and CloudFormation's short tags kept as the text
# they are written as ("!Ref ThreefoldAlarmTopic"). Every scalar stays a
# string, as CloudFormation hands parameters and properties over. Anything
# outside that subset fails loudly rather than being misread.


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


_KEY = re.compile(r"^([A-Za-z_][\w:.-]*):(?:\s+(.*))?$")
_BLOCK_SCALAR = re.compile(r"^(?:(![A-Za-z]+)\s+)?([|>]-?)$")


def _scalar(text: str) -> str:
    if text.startswith("'"):
        end = 1
        while True:
            end = text.index("'", end)
            if text[end:end + 2] == "''":
                end += 2
                continue
            break
        rest = text[end + 1:].strip()
        assert not rest or rest.startswith("#"), f"Text after a quoted scalar: {text!r}"
        return text[1:end].replace("''", "'")
    assert not text.startswith('"'), f"Double-quoted scalars are outside this reader: {text!r}"
    return re.split(r"\s+#", text, maxsplit=1)[0].rstrip()


class _TreeReader:
    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()

    def significant(self, index: int) -> int:
        while index < len(self.lines) and (
            not self.lines[index].strip() or self.lines[index].lstrip().startswith("#")
        ):
            index += 1
        return index

    def node(self, index: int, indent: int):
        if self.lines[index].lstrip().startswith("- "):
            return self.sequence(index, indent)
        return self.mapping(index, indent)

    def mapping(self, index: int, indent: int):
        result: dict = {}
        while True:
            index = self.significant(index)
            if index >= len(self.lines) or _indent(self.lines[index]) < indent:
                return result, index
            line = self.lines[index]
            assert _indent(line) == indent, f"Unexpected indentation: {line!r}"
            if line.lstrip().startswith("- "):
                return result, index
            match = _KEY.match(line.strip())
            assert match, f"Not a key: {line!r}"
            key = match.group(1)
            assert key not in result, f"{key} is declared twice, and CloudFormation keeps only one"
            result[key], index = self.value((match.group(2) or "").strip(), index, indent)

    def sequence(self, index: int, indent: int):
        items: list = []
        while True:
            index = self.significant(index)
            line = self.lines[index] if index < len(self.lines) else ""
            if index >= len(self.lines) or _indent(line) != indent or not line.lstrip().startswith("- "):
                return items, index
            content = line.strip()[2:].strip()
            if _KEY.match(content):
                # A mapping that starts on the dash line: read that line as the
                # first key of a mapping indented to where the key begins.
                self.lines[index] = " " * (indent + 2) + content
                item, index = self.mapping(index, indent + 2)
            else:
                item, index = self.value(content, index, indent)
            items.append(item)

    def value(self, rest: str, index: int, indent: int):
        block = _BLOCK_SCALAR.match(rest)
        if block:
            tag, style = block.group(1), block.group(2)
            collected, following = [], index + 1
            while following < len(self.lines) and (
                not self.lines[following].strip() or _indent(self.lines[following]) > indent
            ):
                collected.append(self.lines[following])
                following += 1
            while collected and not collected[-1].strip():
                collected.pop()
            text = textwrap.dedent("\n".join(collected))
            if style.startswith(">"):
                text = " ".join(text.split())
            return (f"{tag} {text}" if tag else text), following
        if rest:
            return _scalar(rest), index + 1
        following = self.significant(index + 1)
        if following < len(self.lines) and (
            _indent(self.lines[following]) > indent
            or (_indent(self.lines[following]) == indent and self.lines[following].lstrip().startswith("- "))
        ):
            return self.node(following, _indent(self.lines[following]))
        return None, index + 1


def _tree(text: str):
    reader = _TreeReader(text)
    start = reader.significant(0)
    value, end = reader.node(start, _indent(reader.lines[start]))
    assert reader.significant(end) == len(reader.lines), f"Unread from: {reader.lines[end]!r}"
    return value


@functools.lru_cache(maxsize=None)
def _resource_tree(logical_id: str) -> dict:
    return _tree(RESOURCES[logical_id])


def _properties(logical_id: str) -> dict:
    return copy.deepcopy(_resource_tree(logical_id)["Properties"])


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
    stage = _properties("ThreefoldHttpApi")
    assert stage["DefaultRouteSettings"] == {
        "ThrottlingBurstLimit": "!Ref ApiThrottleBurstLimit",
        "ThrottlingRateLimit": "!Ref ApiThrottleRateLimit",
    }
    assert set(stage["AccessLogSettings"]) == {"DestinationArn", "Format"}


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


# ------------------------------------------------------------------ alarms, in general


def _alarms() -> dict[str, str]:
    return _of_type("AWS::CloudWatch::Alarm")


def _alarm_on(metric: str, namespace: str) -> tuple[str, str]:
    for name, block in _alarms().items():
        if re.search(rf"^\s+MetricName: '?{re.escape(metric)}'?$", block, re.M) and namespace in block:
            return name, block
    raise AssertionError(f"No alarm reads {namespace} {metric}")


_FUNCTION = [{"Name": "FunctionName", "Value": "!Ref ThreefoldFunction"}]
_STAGE = [{"Name": "ApiId", "Value": "!Ref ThreefoldHttpApi"}, {"Name": "Stage", "Value": "prod"}]
_OWN_NAMESPACE = f"!Sub '{STACK_NAMESPACE}'"
_NOTIFY = {
    "TreatMissingData": "notBreaching",
    "AlarmActions": ["!Ref ThreefoldAlarmTopic"],
    "OKActions": ["!Ref ThreefoldAlarmTopic"],
}


def _api_share(status: str, counted: str, floor: int) -> list[dict]:
    """A share of the stage's requests, with a floor under which it reads 0."""

    def stage_sum(metric_id: str, metric: str) -> dict:
        return {
            "Id": metric_id,
            "ReturnData": "false",
            "MetricStat": {
                "Metric": {"Namespace": "AWS/ApiGateway", "MetricName": metric, "Dimensions": _STAGE},
                "Period": "300",
                "Stat": "Sum",
            },
        }

    return [
        stage_sum(counted, status),
        stage_sum("requests", "Count"),
        {
            "Id": "rate",
            "Label": f"{status} responses, percent of requests",
            "Expression": f"IF(requests >= {floor}, 100 * FILL({counted}, 0) / requests, 0)",
            "ReturnData": "true",
        },
    ]


def _table_query(metric_id: str, metric: str) -> list[dict]:
    return [{
        "Id": metric_id,
        "Expression": (
            f"!Sub SELECT SUM({metric}) FROM SCHEMA(\"AWS/DynamoDB\", Operation, TableName) "
            "WHERE TableName = '${ThreefoldTable}'"
        ),
        "Period": "300",
        "ReturnData": "true",
    }]


# Every alarm, whole, except its description. Each value here is the one the
# alarm's reasoning in the template depends on, so a statistic, a period, an
# operator or a dimension that drifts is a failure rather than an alarm that
# quietly can never fire, or never stops firing.
EXPECTED_ALARMS: dict[str, dict] = {
    "ThreefoldFunctionErrorsAlarm": {
        "AlarmName": "!Sub '${AWS::StackName}-function-errors'",
        "Namespace": "AWS/Lambda", "MetricName": "Errors", "Dimensions": _FUNCTION,
        "Statistic": "Sum", "Period": "300", "EvaluationPeriods": "1",
        "Threshold": "1", "ComparisonOperator": "GreaterThanOrEqualToThreshold", **_NOTIFY,
    },
    "ThreefoldFunctionThrottlesAlarm": {
        "AlarmName": "!Sub '${AWS::StackName}-function-throttles'",
        "Namespace": "AWS/Lambda", "MetricName": "Throttles", "Dimensions": _FUNCTION,
        "Statistic": "Sum", "Period": "300", "EvaluationPeriods": "1",
        "Threshold": "1", "ComparisonOperator": "GreaterThanOrEqualToThreshold", **_NOTIFY,
    },
    "ThreefoldFunctionDurationAlarm": {
        "AlarmName": "!Sub '${AWS::StackName}-function-duration-p95'",
        "Namespace": "AWS/Lambda", "MetricName": "Duration", "Dimensions": _FUNCTION,
        "ExtendedStatistic": "p95", "Unit": "Milliseconds", "Period": "300",
        "EvaluationPeriods": "3", "DatapointsToAlarm": "2",
        "Threshold": "!Ref SlowCallAlarmMs", "ComparisonOperator": "GreaterThanThreshold", **_NOTIFY,
    },
    "ThreefoldApi5xxRateAlarm": {
        "AlarmName": "!Sub '${AWS::StackName}-api-5xx-rate'",
        "Metrics": _api_share("5xx", "failed", 10),
        "EvaluationPeriods": "1",
        "Threshold": "5", "ComparisonOperator": "GreaterThanOrEqualToThreshold", **_NOTIFY,
    },
    "ThreefoldApi4xxRateAlarm": {
        "AlarmName": "!Sub '${AWS::StackName}-api-4xx-rate'",
        "Metrics": _api_share("4xx", "refused", 20),
        "EvaluationPeriods": "3", "DatapointsToAlarm": "3",
        "Threshold": "25", "ComparisonOperator": "GreaterThanOrEqualToThreshold", **_NOTIFY,
    },
    "ThreefoldTableThrottlesAlarm": {
        "AlarmName": "!Sub '${AWS::StackName}-table-throttled-requests'",
        "Metrics": _table_query("throttled", "ThrottledRequests"),
        "EvaluationPeriods": "1",
        "Threshold": "1", "ComparisonOperator": "GreaterThanOrEqualToThreshold", **_NOTIFY,
    },
    "ThreefoldTableSystemErrorsAlarm": {
        "AlarmName": "!Sub '${AWS::StackName}-table-system-errors'",
        "Metrics": _table_query("failed", "SystemErrors"),
        "EvaluationPeriods": "1",
        "Threshold": "1", "ComparisonOperator": "GreaterThanOrEqualToThreshold", **_NOTIFY,
    },
    "ThreefoldHaltedSessionCallsAlarm": {
        "AlarmName": "!Sub '${AWS::StackName}-halted-session-calls'",
        "Namespace": _OWN_NAMESPACE, "MetricName": "CircuitBreakerTripped",
        "Statistic": "Sum", "Period": "300", "EvaluationPeriods": "1",
        "Threshold": "10", "ComparisonOperator": "GreaterThanOrEqualToThreshold", **_NOTIFY,
    },
    "ThreefoldEvaluationLatencyAlarm": {
        "AlarmName": "!Sub '${AWS::StackName}-evaluation-latency'",
        "Namespace": _OWN_NAMESPACE, "MetricName": "LatencyMs",
        "Statistic": "Average", "Period": "300", "EvaluationPeriods": "3", "DatapointsToAlarm": "2",
        "Threshold": "!Ref SlowCallAlarmMs", "ComparisonOperator": "GreaterThanThreshold", **_NOTIFY,
    },
    "ThreefoldCallVolumeAlarm": {
        "AlarmName": "!Sub '${AWS::StackName}-call-volume'",
        "Namespace": _OWN_NAMESPACE, "MetricName": "ToolCallsEvaluated",
        "Statistic": "Sum", "Period": "300", "EvaluationPeriods": "3", "DatapointsToAlarm": "3",
        "Threshold": "1500", "ComparisonOperator": "GreaterThanThreshold", **_NOTIFY,
    },
}


def test_the_template_has_exactly_these_alarms() -> None:
    """A deleted alarm is a failure nobody hears about; a new one needs its row above."""
    assert set(_alarms()) == set(EXPECTED_ALARMS)


@pytest.mark.parametrize("logical_id", sorted(EXPECTED_ALARMS))
def test_each_alarm_is_exactly_the_alarm_it_is_meant_to_be(logical_id: str) -> None:
    properties = _properties(logical_id)
    description = properties.pop("AlarmDescription", "")
    assert len(description) > 80, f"{logical_id} does not say what it means or what to do about it"
    assert properties == EXPECTED_ALARMS[logical_id]


def test_every_alarm_fires_when_its_measure_rises() -> None:
    """Each alarm measures failures, throttles, slowness or volume.

    With a less-than operator an alarm would sit in alarm on a quiet stack and
    never fire on a flooded one.
    """
    for logical_id in _alarms():
        operator = _properties(logical_id)["ComparisonOperator"]
        assert operator in {"GreaterThanThreshold", "GreaterThanOrEqualToThreshold"}, logical_id


def test_every_alarm_says_how_it_reads_its_metric() -> None:
    """Nothing is left to a default: CloudFormation rejects some omissions only at deploy time."""
    for logical_id in _alarms():
        properties = _properties(logical_id)
        assert int(properties["EvaluationPeriods"]) >= int(properties.get("DatapointsToAlarm", "1")), logical_id
        if "Metrics" in properties:
            assert not {"Namespace", "MetricName", "Statistic", "ExtendedStatistic", "Period"} & set(properties), (
                f"{logical_id} mixes a single metric with metric math"
            )
            returned = [m for m in properties["Metrics"] if m["ReturnData"] == "true"]
            assert len(returned) == 1, f"{logical_id} must return exactly one series to judge"
            for metric in properties["Metrics"]:
                if "MetricStat" in metric:
                    assert metric["MetricStat"]["Stat"] and metric["MetricStat"]["Period"] == "300", logical_id
                else:
                    assert metric["Expression"] and metric.get("Period", "300") == "300", logical_id
                    if metric["Expression"].startswith("!Sub SELECT"):
                        assert metric.get("Period") == "300", f"{logical_id}: a Metrics Insights query needs its own period"
        else:
            assert ("Statistic" in properties) != ("ExtendedStatistic" in properties), logical_id
            assert properties["Period"] == "300", logical_id


def test_a_count_published_as_ones_is_alarmed_on_its_sum() -> None:
    """The average of a stream of 1s is 1.

    A filter that publishes 1 for each matching record makes a count only when
    summed. Averaged, the halted-session alarm's threshold of 10 could never be
    reached, and the call-volume alarm's 1,500 neither.
    """
    filters = _filters()
    checked = 0
    for logical_id in _alarms():
        properties = _properties(logical_id)
        if properties.get("Namespace") != _OWN_NAMESPACE:
            continue
        published = filters[properties["MetricName"]]
        if published["value"] == "1":
            assert properties.get("Statistic") == "Sum", f"{logical_id} averages a count"
            checked += 1
    assert checked >= 2


def test_every_alarm_is_named_per_stack_explains_itself_and_notifies_the_topic() -> None:
    alarms = _alarms()
    assert set(alarms) == set(EXPECTED_ALARMS)
    for name, block in alarms.items():
        assert re.search(r"^      AlarmName: !Sub '\$\{AWS::StackName\}-[a-z0-9-]+'$", block, re.M), name
        assert re.search(r"^      AlarmDescription: ", block, re.M), f"{name} does not say what to do about it"
        assert re.search(r"^      AlarmActions:\n        - !Ref ThreefoldAlarmTopic$", block, re.M), name
        assert re.search(r"^      OKActions:\n        - !Ref ThreefoldAlarmTopic$", block, re.M), (
            f"{name} would page when it fires and never say it cleared"
        )
        treat = re.search(r"^      TreatMissingData: (\w+)$", block, re.M)
        assert treat and treat.group(1) in TREAT_MISSING, f"{name} leaves missing data to the default"


def test_no_alarm_treats_silence_as_a_breach() -> None:
    """A quiet night or an unvisited demo is not an incident.

    Every alarm here measures failures, throttles, slowness or volume, none of
    which a quiet stack has. Breaching on missing data would page every night
    the owner's agents are idle.
    """
    for name, block in _alarms().items():
        assert re.search(r"^      TreatMissingData: notBreaching$", block, re.M), name


# ------------------------------------------------------------------ alarms, one by one


@pytest.mark.parametrize("metric", ["Errors", "Throttles"])
def test_the_function_alarms_on_errors_and_throttles(metric: str) -> None:
    _, block = _alarm_on(metric, "AWS/Lambda")
    assert re.search(r"^      Namespace: AWS/Lambda$", block, re.M)
    assert re.search(r"^        - Name: FunctionName\n          Value: !Ref ThreefoldFunction$", block, re.M)
    assert re.search(r"^      Statistic: Sum$", block, re.M)
    assert re.search(r"^      Threshold: 1$", block, re.M)
    assert re.search(r"^      ComparisonOperator: GreaterThanOrEqualToThreshold$", block, re.M)


def test_the_function_alarms_on_p95_duration_over_the_slow_call_threshold() -> None:
    _, block = _alarm_on("Duration", "AWS/Lambda")
    assert re.search(r"^      ExtendedStatistic: p95$", block, re.M), "A percentile needs ExtendedStatistic"
    assert not re.search(r"^      Statistic:", block, re.M)
    assert re.search(r"^      Threshold: !Ref SlowCallAlarmMs$", block, re.M)
    assert int(_default("SlowCallAlarmMs")) < 15000, "The function times out at 15 seconds; warn before that"


@pytest.mark.parametrize("status, floor, share", [("5xx", 10, 5), ("4xx", 20, 25)])
def test_the_api_alarms_are_rates_over_the_http_api_stage_metrics(status: str, floor: int, share: int) -> None:
    name, block = _alarm_on(status, "AWS/ApiGateway")
    assert re.search(rf"^              MetricName: '{status}'$", block, re.M)
    assert re.search(r"^              MetricName: Count$", block, re.M), f"{name} is a count, not a rate"
    dimensions = re.findall(
        r"^                - Name: ApiId\n                  Value: !Ref ThreefoldHttpApi\n"
        r"                - Name: Stage\n                  Value: prod$",
        block,
        re.M,
    )
    assert len(dimensions) == 2, "HTTP API metrics are published per ApiId and Stage"
    expression = re.search(r"^          Expression: '(.+)'$", block, re.M)
    assert expression, f"{name} has no rate expression"
    assert re.fullmatch(
        rf"IF\(requests >= {floor}, 100 \* FILL\(\w+, 0\) / requests, 0\)", expression.group(1)
    ), f"{expression.group(1)}: a rate with a floor of {floor} requests"
    assert block.count("ReturnData: true") == 1 and block.count("ReturnData: false") == 2, (
        "Only the expression may be returned, or the alarm has more than one series to judge"
    )
    assert re.search(rf"^      Threshold: {share}$", block, re.M)


def test_the_api_alarms_use_the_http_api_metric_names_not_the_rest_ones() -> None:
    assert "5XXError" not in TEMPLATE and "4XXError" not in TEMPLATE, (
        "Those are REST API names; an HTTP API never publishes them, so the alarm could never fire"
    )


def test_the_client_error_alarm_waits_for_a_sustained_share() -> None:
    """This API answers 400, 401, 403, 404 and 409 as problems by design."""
    _, block = _alarm_on("4xx", "AWS/ApiGateway")
    assert re.search(r"^      EvaluationPeriods: 3$", block, re.M)
    assert re.search(r"^      DatapointsToAlarm: 3$", block, re.M)


@pytest.mark.parametrize("metric", ["ThrottledRequests", "SystemErrors"])
def test_the_table_alarms_sum_every_operation_on_this_table(metric: str) -> None:
    """DynamoDB publishes both per operation; TableName alone is not a series."""
    for name, block in _alarms().items():
        if f"SUM({metric})" in block:
            break
    else:
        raise AssertionError(f"No alarm on DynamoDB {metric}")
    query = re.search(r"^          Expression: !Sub >-\n((?:            .*\n)+)", block, re.M)
    assert query, f"{name} is not a Metrics Insights query"
    text = " ".join(query.group(1).split())
    assert text == (
        f"SELECT SUM({metric}) FROM SCHEMA(\"AWS/DynamoDB\", Operation, TableName) "
        "WHERE TableName = '${ThreefoldTable}'"
    )
    assert re.search(r"^          Period: 300$", block, re.M), "A Metrics Insights alarm query needs its own period"


# ------------------------------------------------------------------ the service's own metrics


def _call_arguments(source: str, start: int) -> str:
    depth, index = 0, start
    while True:
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[start + 1:index]
        index += 1


def _first_dict(text: str) -> str:
    start = text.index("{")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError("Unbalanced metrics literal")


def _emitted() -> dict[tuple[str, str], bool]:
    """Every (namespace, metric) the handler emits, and whether at the default dimensions."""
    default_namespace = inspect.signature(emit_threefold_emf_metrics).parameters["namespace"].default
    found: dict[tuple[str, str], bool] = {}
    for match in re.finditer(r"emit_threefold_emf_metrics\(", HANDLERS):
        if HANDLERS[match.start() - 4:match.start()] == "def ":
            continue
        arguments = _call_arguments(HANDLERS, match.end() - 1)
        namespace = re.search(r'namespace="([^"]+)"', arguments)
        default_dimensions = "dimensions=" not in arguments
        for metric in re.findall(r'"(\w+)"\s*:', _first_dict(arguments)):
            found[(namespace.group(1) if namespace else default_namespace, metric)] = default_dimensions
    assert found, "No EMF emission found in the handler"
    return found


def _records(captured: str) -> list[dict]:
    records = []
    for line in captured.splitlines():
        line = line.strip()
        if line.startswith("{") and '"_aws"' in line:
            records.append(json.loads(line))
    return records


def _pattern_matches(pattern: str, record: dict) -> bool:
    """CloudWatch Logs JSON filter semantics for the subset this template uses.

    Only `$.Field op number` terms joined by `&&` are understood, and anything
    else fails the test, so a pattern this model cannot read cannot pass by
    being misread. A field that is absent or not a number does not match, as
    in CloudWatch.
    """
    inner = pattern.strip()
    assert inner.startswith("{") and inner.endswith("}"), pattern
    matched = True
    for term in inner[1:-1].split("&&"):
        term = term.strip()
        if term.startswith("(") and term.endswith(")"):
            term = term[1:-1].strip()
        parsed = re.fullmatch(r"\$\.(\w+)\s*(>=|<=|!=|>|<|=)\s*(-?\d+(?:\.\d+)?)", term)
        assert parsed, f"{term!r} is outside the subset this test can evaluate"
        field, operator, number = parsed.group(1), parsed.group(2), float(parsed.group(3))
        value = record.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        matched &= {
            ">": value > number, "<": value < number, "=": value == number,
            ">=": value >= number, "<=": value <= number, "!=": value != number,
        }[operator]
    return matched


def _filters() -> dict[str, dict[str, str]]:
    filters = {}
    for name, block in _of_type("AWS::Logs::MetricFilter").items():
        pattern = re.search(r"^      FilterPattern: '(.+)'$", block, re.M)
        metric = re.search(r"^          MetricName: (\w+)$", block, re.M)
        value = re.search(r"^          MetricValue: '?([^'\n]+)'?$", block, re.M)
        assert pattern and metric and value, f"{name} is missing a pattern, a name or a value"
        filters[metric.group(1)] = {"resource": name, "pattern": pattern.group(1), "value": value.group(1), "block": block}
    return filters


def test_the_governance_filters_read_this_stacks_log_into_this_stacks_namespace() -> None:
    filters = _filters()
    assert set(filters) >= {"ToolCallsEvaluated", "VerdictApproved", "CircuitBreakerTripped", "LatencyMs", "CurrentSessionCostUSD"}
    for metric, found in filters.items():
        block = found["block"]
        assert re.search(r"^      LogGroupName: !Ref ThreefoldLogGroup$", block, re.M), (
            f"{metric} must read this stack's own function log, or both stacks count each other's calls"
        )
        assert f"MetricNamespace: !Sub '{STACK_NAMESPACE}'" in block
        assert "$.ToolCallsEvaluated > 0" in found["pattern"], (
            f"{metric} would also count the demo's /simulate-loop record, which is not an evaluated call"
        )


def test_the_filters_read_only_metrics_the_evaluate_route_emits() -> None:
    emitted = {metric for (namespace, metric) in _emitted() if namespace == "Threefold/Governance"}
    for metric, found in _filters().items():
        assert metric in emitted, f"{metric} is not a metric the handler emits"
        for field in re.findall(r"\$\.(\w+)", found["pattern"] + " " + found["value"]):
            assert field in emitted, f"{found['resource']} reads {field}, which the handler never writes"


def _evaluate(event_body: dict) -> dict:
    from threefold.interfaces.api_handlers import lambda_handler

    response = lambda_handler({"httpMethod": "POST", "path": "/evaluate-tool-call", "body": json.dumps(event_body)})
    assert response["statusCode"] == 200, response["body"]
    return json.loads(response["body"])


def test_the_filters_match_the_record_an_evaluated_call_actually_writes(capsys) -> None:
    """Runs the real handler and applies each filter to the EMF line it prints."""
    session = f"acme-template-{uuid.uuid4().hex[:12]}"
    call = {
        "session_id": session,
        "project_name": "Acme-Template",
        "tool_name": "Edit",
        "action_type": "FILE_WRITE",
        "arguments": {"file_path": "src/acme/service.py", "old_string": "a = 1", "new_string": "a = 2"},
        "agent": "page",
        "origin": "page",
    }
    capsys.readouterr()
    verdicts = [_evaluate(call) for _ in range(3)]
    records = [r for r in _records(capsys.readouterr().out) if "ToolCallsEvaluated" in r]
    assert len(records) == 3, "Each evaluated call writes one governance record"
    for record in records:
        definition = record["_aws"]["CloudWatchMetrics"][0]
        assert definition["Namespace"] == "Threefold/Governance"
        assert definition["Dimensions"] == [["Project", "Environment"]], (
            "Per project, so no single series exists to alarm on; this is why the filters exist"
        )

    filters = _filters()
    first, third = records[0], records[2]
    assert verdicts[0]["status"] == "APPROVED" and not verdicts[0]["session_tripped"]
    assert _pattern_matches(filters["ToolCallsEvaluated"]["pattern"], first)
    assert _pattern_matches(filters["VerdictApproved"]["pattern"], first)
    assert not _pattern_matches(filters["CircuitBreakerTripped"]["pattern"], first)
    assert isinstance(first[filters["LatencyMs"]["value"][2:]], float)
    assert isinstance(first[filters["CurrentSessionCostUSD"]["value"][2:]], (int, float))

    assert verdicts[2]["status"] == "BLOCKED_LOOP_DETECTED" and verdicts[2]["session_tripped"]
    assert _pattern_matches(filters["ToolCallsEvaluated"]["pattern"], third)
    assert not _pattern_matches(filters["VerdictApproved"]["pattern"], third)
    assert _pattern_matches(filters["CircuitBreakerTripped"]["pattern"], third)


def test_a_call_let_through_by_a_dry_run_counts_as_approved(capsys) -> None:
    """Why the dashboard cannot call calls - approved anything but refusals.

    A dry run, and a hook call judged in observe, is recorded and never
    refused: a rule that would have refused it adds an observation and the
    verdict is still APPROVED, so the record says VerdictApproved = 1. The
    approved line therefore holds those calls, and the difference between
    evaluated and approved holds only real refusals.
    """
    session = f"acme-template-{uuid.uuid4().hex[:12]}"
    call = {
        "session_id": session,
        "project_name": "Acme-Template",
        "tool_name": "Write",
        "action_type": "FILE_WRITE",
        "arguments": {
            "file_path": "src/main/java/acme/domain/Order.java",
            "content": "package acme.domain;\nimport javax.persistence.Entity;\npublic class Order {}\n",
        },
        "agent": "page",
        "origin": "page",
    }
    filters = _filters()
    capsys.readouterr()
    observed = _evaluate({**call, "dry_run": True})
    refused = _evaluate({**call, "session_id": session + "-enforced", "dry_run": False})
    records = [r for r in _records(capsys.readouterr().out) if "ToolCallsEvaluated" in r]
    assert len(records) == 2

    assert observed["status"] == "APPROVED" and observed["observations"], "A rule flagged it and let it through"
    assert _pattern_matches(filters["VerdictApproved"]["pattern"], records[0])
    assert refused["status"].startswith("BLOCKED_")
    assert not _pattern_matches(filters["VerdictApproved"]["pattern"], records[1])

    widget = next(w for w in _dashboard()["widgets"] if any(
        isinstance(row[0], str) and row[1] == "VerdictApproved" for row in w["properties"].get("metrics", [])
    ))
    rows = widget["properties"]["metrics"]
    approved = next(row[-1] for row in rows if isinstance(row[0], str) and row[1] == "VerdictApproved")
    difference = next(row[0] for row in rows if isinstance(row[0], dict))
    ids = {row[-1]["id"]: row[1] for row in rows if isinstance(row[0], str)}
    assert ids == {"calls": "ToolCallsEvaluated", "approved": "VerdictApproved"}
    assert difference["expression"] == "calls - FILL(approved, 0)"
    assert difference["label"] == "Refused", "calls - approved holds refusals and nothing else"
    assert "observe" in approved["label"] and "dry run" in approved["label"], (
        "The approved line holds every call observe or a dry run let through, and should say so"
    )


def test_the_demo_loop_record_is_not_counted_as_a_governance_trip(capsys) -> None:
    from threefold.interfaces.api_handlers import lambda_handler

    capsys.readouterr()
    response = lambda_handler({"httpMethod": "POST", "path": "/simulate-loop"})
    assert response["statusCode"] == 200
    demo = [r for r in _records(capsys.readouterr().out) if "LoopDetected" in r]
    assert demo and demo[0]["CircuitBreakerTripped"] == 1.0, "The demo record carries the trip on every click"
    for metric, found in _filters().items():
        assert not _pattern_matches(found["pattern"], demo[0]), f"{metric} would count every demo click"


@pytest.mark.parametrize(
    "metric, alarm_threshold",
    [("CircuitBreakerTripped", "10"), ("LatencyMs", "!Ref SlowCallAlarmMs"), ("ToolCallsEvaluated", "1500")],
)
def test_the_service_metrics_are_alarmed_on_per_stack(metric: str, alarm_threshold: str) -> None:
    name, block = _alarm_on(metric, STACK_NAMESPACE)
    assert re.search(rf"^      Namespace: !Sub '{re.escape(STACK_NAMESPACE)}'$", block, re.M), (
        f"{name} must read the namespace this stack's filters publish to"
    )
    assert metric in _filters(), f"{name} reads a metric no filter publishes"
    assert re.search(rf"^      Threshold: {re.escape(alarm_threshold)}$", block, re.M)


def test_no_alarm_reads_the_namespace_both_stacks_share() -> None:
    for name, block in _alarms().items():
        assert "Namespace: Threefold/" not in block, (
            f"{name} reads a shared namespace, where the two stacks cannot be told apart"
        )


# ------------------------------------------------------------------ notifications


def test_the_topic_is_named_per_stack_and_only_this_accounts_services_publish() -> None:
    topic = RESOURCES["ThreefoldAlarmTopic"]
    assert re.search(r"^      TopicName: !Sub '\$\{AWS::StackName\}-alarms'$", topic, re.M)
    policy = RESOURCES["ThreefoldAlarmTopicPolicy"]
    for service in ("cloudwatch.amazonaws.com", "budgets.amazonaws.com"):
        assert f"Service: {service}" in policy, f"{service} cannot publish to the topic"
    assert policy.count("aws:SourceAccount: !Ref AWS::AccountId") == 2, (
        "Without the condition another account's alarm could name this topic"
    )
    assert "Principal: '*'" not in policy and "AWS: '*'" not in policy


def test_the_topic_policy_grants_publish_on_this_topic_and_nothing_else() -> None:
    """Each statement, whole: a widened action or resource is not a detail."""

    def statement(sid: str, service: str) -> dict:
        return {
            "Sid": sid,
            "Effect": "Allow",
            "Principal": {"Service": service},
            "Action": "sns:Publish",
            "Resource": "!Ref ThreefoldAlarmTopic",
            "Condition": {"StringEquals": {"aws:SourceAccount": "!Ref AWS::AccountId"}},
        }

    assert _properties("ThreefoldAlarmTopicPolicy") == {
        "Topics": ["!Ref ThreefoldAlarmTopic"],
        "PolicyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                statement("AlarmsOfThisAccount", "cloudwatch.amazonaws.com"),
                statement("BudgetsOfThisAccount", "budgets.amazonaws.com"),
            ],
        },
    }


def test_the_topic_and_the_subscription_are_exactly_what_they_are_meant_to_be() -> None:
    # No KmsMasterKeyId: CloudWatch cannot publish to a topic under the AWS
    # managed key, and a key of its own is not worth its monthly charge here.
    assert _properties("ThreefoldAlarmTopic") == {"TopicName": "!Sub '${AWS::StackName}-alarms'", "DisplayName": "Threefold"}
    assert _resource_tree("ThreefoldAlarmEmailSubscription") == {
        "Type": "AWS::SNS::Subscription",
        "Condition": "HasAlarmEmail",
        "Properties": {"TopicArn": "!Ref ThreefoldAlarmTopic", "Protocol": "email", "Endpoint": "!Ref AlarmEmail"},
    }
    assert _resource_tree("ThreefoldApiAccessLogGroup") == {
        "Type": "AWS::Logs::LogGroup",
        "Properties": {"LogGroupName": "!Sub '/aws/vendedlogs/apigateway/${AWS::StackName}/access'", "RetentionInDays": "14"},
    }


def test_email_is_subscribed_only_when_an_address_is_given() -> None:
    block = PARAMETERS["AlarmEmail"]
    assert _default("AlarmEmail") == ""
    assert re.search(r"^    NoEcho: true$", block, re.M), "describe-stacks would print the address"
    subscription = RESOURCES["ThreefoldAlarmEmailSubscription"]
    assert _type(subscription) == "AWS::SNS::Subscription"
    assert re.search(r"^    Condition: HasAlarmEmail$", subscription, re.M)
    assert re.search(r"^      Protocol: email$", subscription, re.M)
    assert re.search(r"^      Endpoint: !Ref AlarmEmail$", subscription, re.M)
    assert re.search(r"^  HasAlarmEmail: !Not \[!Equals \[!Ref AlarmEmail, ''\]\]$", _conditions(), re.M)


@pytest.mark.parametrize(
    "address, allowed",
    [("", True), ("ops@acme.example", True), ("not an address", False), ("ops@acme", False)],
)
def test_the_alarm_address_is_checked_before_the_stack_is_touched(address: str, allowed: bool) -> None:
    pattern = re.search(r"^    AllowedPattern: '(.+)'$", PARAMETERS["AlarmEmail"], re.M)
    assert pattern, "A typo in the address would only surface as mail that never arrives"
    assert bool(re.fullmatch(pattern.group(1), address)) is allowed


# ------------------------------------------------------------------ both stacks, one template


def test_every_name_the_template_sets_is_derived_from_the_stack() -> None:
    names = re.findall(
        r"^\s+(TopicName|AlarmName|DashboardName|LogGroupName|BudgetName|MetricNamespace): (.+)$", TEMPLATE, re.M
    )
    assert len(names) >= 15
    for key, value in names:
        reference = re.fullmatch(r"!Ref (\w+)", value)
        if reference:
            # A metric filter names the log group it reads by reference, and
            # that group's own name is checked where it is declared.
            assert reference.group(1) in RESOURCES, f"{key}: {value} names nothing in this template"
            continue
        assert value.startswith("!Sub ") and ("${AWS::StackName}" in value or "${ThreefoldFunction}" in value), (
            f"{key}: {value} is fixed, so the second stack deployed from this template collides with the first"
        )
    declarations = "\n".join(line for line in TEMPLATE.splitlines() if not line.lstrip().startswith("#"))
    assert "threefold-prod" not in declarations and "threefold-dogfood" not in declarations, (
        "A stack's own name written into the template is wrong for the other stack"
    )


def test_every_parameter_the_template_declares_is_used() -> None:
    body = TEMPLATE.replace(_section("Parameters"), "")
    for name in PARAMETERS:
        assert re.search(rf"!Ref {name}\b|\$\{{{name}\}}", body), f"{name} is declared and changes nothing"


def test_every_reference_in_the_template_resolves() -> None:
    """A typo in a Ref or a Sub is otherwise found by the deploy, on the live stack."""
    known = set(RESOURCES) | set(PARAMETERS) | PSEUDO_PARAMETERS
    conditions = set(re.findall(r"^  (\w+): ", _conditions(), re.M))
    for name in re.findall(r"!Ref '?([\w:]+)'?", TEMPLATE):
        assert name in known, f"!Ref {name} names nothing"
    for name in re.findall(r"!GetAtt (\w+)\.", TEMPLATE):
        assert name in RESOURCES, f"!GetAtt {name} names no resource"
    for name in re.findall(r"\$\{([\w:]+)(?:\.\w+)?\}", TEMPLATE):
        assert name in known, f"${{{name}}} names nothing"
    for name in re.findall(r"^    Condition: (\w+)$", TEMPLATE, re.M) + re.findall(r"!If \[(\w+),", TEMPLATE):
        assert name in conditions, f"Condition {name} is not defined"


# ------------------------------------------------------------------ the dashboard


def _dashboard_body() -> str:
    block = RESOURCES["ThreefoldOperationsDashboard"]
    match = re.search(r"^      DashboardBody: !Sub \|\n((?:(?: {8}.*)?\n)+)", block, re.M)
    assert match, "The dashboard body is not a !Sub block"
    return textwrap.dedent(match.group(1))


# Each parameter in the body is filled with a number nothing else in it uses,
# so a widget that follows the parameter is told apart from one that writes
# today's default out as a literal and goes stale when the parameter changes.
_PARAMETER_STANDINS = {"SlowCallAlarmMs": 98761, "ReservedConcurrency": 873}


def _dashboard() -> dict:
    """The body as CloudFormation would hand it over, with every reference filled.

    Each resource reference becomes "<LogicalId>", so a row can be checked
    against the resource it is meant to read rather than against any value.
    """
    body = _dashboard_body()
    for name, standin in _PARAMETER_STANDINS.items():
        body = body.replace("${%s}" % name, str(standin))
    body = body.replace("${AWS::StackName}", "acme-stack").replace("${AWS::Region}", "<region>")
    body = re.sub(r"\$\{(\w+)(\.\w+)?\}", lambda m: f"<{m.group(1)}{m.group(2) or ''}>", body)
    assert "${" not in body, "A reference the dashboard test does not know how to fill"
    return json.loads(body)


def test_the_dashboard_is_named_per_stack() -> None:
    assert re.search(
        r"^      DashboardName: !Sub '\$\{AWS::StackName\}-operations'$", RESOURCES["ThreefoldOperationsDashboard"], re.M
    ), "Dashboard names are unique in a region; two stacks with one name overwrite each other"


def _dashboard_metrics() -> list[list]:
    rows = []
    for widget in _dashboard()["widgets"]:
        rows.extend(widget["properties"].get("metrics", []))
    return rows


def test_the_dashboard_body_is_json_and_fits_the_grid() -> None:
    widgets = _dashboard()["widgets"]
    assert widgets
    for widget in widgets:
        assert widget["x"] + widget["width"] <= 24, widget
        if widget["type"] == "metric":
            assert widget["properties"]["region"] == "<region>", "Every metric widget names the stack's region"


def _stage_name() -> str:
    match = re.search(r"^      StageName: (\S+)$", RESOURCES["ThreefoldHttpApi"], re.M)
    assert match, "The API declares no stage"
    return match.group(1)


def _row_dimensions(row: list) -> dict[str, str]:
    names_and_values = row[2:-1] if isinstance(row[-1], dict) else row[2:]
    assert len(names_and_values) % 2 == 0, f"{row}: dimensions come in name and value pairs"
    return dict(zip(names_and_values[::2], names_and_values[1::2]))


def test_every_dashboard_row_reads_this_stacks_own_resources() -> None:
    """A row on another stage, function or table draws a flat line and says nothing."""
    expected = {
        "AWS/ApiGateway": {"ApiId": "<ThreefoldHttpApi>", "Stage": _stage_name()},
        "AWS/Lambda": {"FunctionName": "<ThreefoldFunction>"},
        "AWS/DynamoDB": {"TableName": "<ThreefoldTable>"},
        "Threefold/acme-stack": {},
    }
    rows = _dashboard_metrics()
    seen = set()
    for row in rows:
        if isinstance(row[0], dict):
            query = row[0]["expression"]
            if "AWS/DynamoDB" in query:
                assert query.endswith("WHERE TableName = '<ThreefoldTable>'") or (
                    "WHERE TableName = '<ThreefoldTable>' GROUP BY Operation" in query
                ), f"{query} does not read this stack's table"
            continue
        namespace = row[0]
        if namespace in expected:
            assert _row_dimensions(row) == expected[namespace], f"{row} reads something other than this stack's own"
            seen.add(namespace)
    assert seen == set(expected), "Each of the stack's own sources has at least one row"


def test_the_api_alarms_and_rows_name_the_stage_the_api_declares() -> None:
    assert _stage_name() == "prod"
    assert {"Name": "Stage", "Value": _stage_name()} in _STAGE


def _widgets_reading(metric: str) -> list[dict]:
    return [
        widget for widget in _dashboard()["widgets"]
        if any(isinstance(row[0], str) and row[1] == metric for row in widget["properties"].get("metrics", []))
    ]


def test_the_dashboard_lines_follow_the_alarms_and_the_parameters_they_draw() -> None:
    """A threshold line drawn in the wrong place misleads more than none at all."""
    halted_threshold = int(_properties("ThreefoldHaltedSessionCallsAlarm")["Threshold"])
    expected_by_metric = {
        "CircuitBreakerTripped": halted_threshold,
        "LatencyMs": _PARAMETER_STANDINS["SlowCallAlarmMs"],
        "Duration": _PARAMETER_STANDINS["SlowCallAlarmMs"],
        "ConcurrentExecutions": _PARAMETER_STANDINS["ReservedConcurrency"],
    }
    assert _properties("ThreefoldEvaluationLatencyAlarm")["Threshold"] == "!Ref SlowCallAlarmMs"
    assert _properties("ThreefoldFunctionDurationAlarm")["Threshold"] == "!Ref SlowCallAlarmMs"
    annotated = 0
    for widget in _dashboard()["widgets"]:
        lines = widget["properties"].get("annotations", {}).get("horizontal", [])
        if not lines:
            continue
        metrics = {row[1] for row in widget["properties"]["metrics"] if isinstance(row[0], str)}
        drawn = metrics & set(expected_by_metric)
        assert len(drawn) == 1, f"{widget['properties']['title']}: a line nobody checks"
        assert [line["value"] for line in lines] == [expected_by_metric[drawn.pop()]], widget["properties"]["title"]
        annotated += 1
    assert annotated == 4
    assert all(_widgets_reading(metric) for metric in expected_by_metric)


def test_the_dashboard_shows_the_api_the_function_the_table_and_governance() -> None:
    rows = _dashboard_metrics()
    plain = [row for row in rows if isinstance(row[0], str)]
    namespaces = {row[0] for row in plain}
    assert {"AWS/ApiGateway", "AWS/Lambda", "AWS/DynamoDB", "Threefold/acme-stack"} <= namespaces
    governance = {row[1] for row in plain if row[0] == "Threefold/acme-stack"}
    assert governance >= {"ToolCallsEvaluated", "VerdictApproved", "CircuitBreakerTripped", "LatencyMs", "CurrentSessionCostUSD"}, (
        "Calls evaluated, approvals, trips, latency and spend"
    )
    assert governance <= set(_filters()), "The dashboard shows a governance metric no filter publishes"
    api = {row[1] for row in plain if row[0] == "AWS/ApiGateway"}
    assert {"Count", "4xx", "5xx", "Latency"} <= api
    queries = [row[0]["expression"] for row in rows if isinstance(row[0], dict)]
    assert any("SUM(ThrottledRequests)" in q for q in queries) and any("SUM(SystemErrors)" in q for q in queries)


def test_the_dashboard_shows_only_shared_metrics_the_service_emits_at_those_dimensions() -> None:
    emitted = _emitted()
    record_dimensions = _default_emf_dimensions()
    for row in _dashboard_metrics():
        if not isinstance(row[0], str) or not row[0].startswith("Threefold/") or row[0] == "Threefold/acme-stack":
            continue
        namespace, metric = row[0], row[1]
        assert (namespace, metric) in emitted, f"{namespace} {metric} is not emitted by the handler"
        assert emitted[(namespace, metric)], f"{namespace} {metric} is emitted with other dimensions"
        dimensions = dict(zip(row[2:-1:2], row[3:-1:2]))
        assert dimensions == record_dimensions, f"{metric}: the widget's dimensions are not the ones emitted"


def _default_emf_dimensions() -> dict[str, str]:
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        emit_threefold_emf_metrics({"TemplateProbe": 1.0})
    record = json.loads(buffer.getvalue())
    keys = record["_aws"]["CloudWatchMetrics"][0]["Dimensions"][0]
    return {key: record[key] for key in keys}


def test_the_dashboard_alarm_widget_lists_every_alarm() -> None:
    body = _dashboard_body()
    listed = set(re.findall(r'"\$\{(\w+)\.Arn\}"', body))
    assert listed == set(_alarms()), "An alarm missing from the widget is an alarm nobody looks at"


# ------------------------------------------------------------------ the budget


def test_the_budget_is_off_unless_asked_for_and_reports_to_the_topic() -> None:
    assert _default("MonthlyBudgetUsd") == "0", "A default deploy must create nothing billable it was not asked for"
    budget = RESOURCES["ThreefoldMonthlyBudget"]
    assert _type(budget) == "AWS::Budgets::Budget"
    assert re.search(r"^    Condition: HasBudget$", budget, re.M)
    assert re.search(r"^  HasBudget: !Not \[!Equals \[!Ref MonthlyBudgetUsd, '0'\]\]$", _conditions(), re.M)
    assert re.search(r"^        BudgetName: !Sub '\$\{AWS::StackName\}-[a-z-]+'$", budget, re.M)
    assert re.search(r"^          Amount: !Ref MonthlyBudgetUsd$", budget, re.M)
    assert budget.count("Address: !Ref ThreefoldAlarmTopic") == 2
    assert re.search(r"^    DependsOn: ThreefoldAlarmTopicPolicy$", budget, re.M), (
        "Budgets checks that it may publish to the topic when the notification is created"
    )


def test_the_budget_warns_at_four_fifths_spent_and_at_a_forecast_overrun() -> None:
    """The whole budget, so a threshold typed as 8000 or a type of FORECASTED twice is caught."""

    def notify(kind: str, percent: str) -> dict:
        return {
            "Notification": {
                "NotificationType": kind,
                "ComparisonOperator": "GREATER_THAN",
                "Threshold": percent,
                "ThresholdType": "PERCENTAGE",
            },
            "Subscribers": [{"SubscriptionType": "SNS", "Address": "!Ref ThreefoldAlarmTopic"}],
        }

    assert _resource_tree("ThreefoldMonthlyBudget") == {
        "Type": "AWS::Budgets::Budget",
        "Condition": "HasBudget",
        "DependsOn": "ThreefoldAlarmTopicPolicy",
        "Properties": {
            "Budget": {
                "BudgetName": "!Sub '${AWS::StackName}-account-monthly-cost'",
                "BudgetType": "COST",
                "TimeUnit": "MONTHLY",
                "BudgetLimit": {"Amount": "!Ref MonthlyBudgetUsd", "Unit": "USD"},
            },
            "NotificationsWithSubscribers": [notify("ACTUAL", "80"), notify("FORECASTED", "100")],
        },
    }
