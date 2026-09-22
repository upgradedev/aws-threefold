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

import inspect
import json
import re
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


# ------------------------------------------------------------------ alarms, in general


def _alarms() -> dict[str, str]:
    return _of_type("AWS::CloudWatch::Alarm")


def _alarm_on(metric: str, namespace: str) -> tuple[str, str]:
    for name, block in _alarms().items():
        if re.search(rf"^\s+MetricName: '?{re.escape(metric)}'?$", block, re.M) and namespace in block:
            return name, block
    raise AssertionError(f"No alarm reads {namespace} {metric}")


def test_every_alarm_is_named_per_stack_explains_itself_and_notifies_the_topic() -> None:
    alarms = _alarms()
    assert len(alarms) >= 9
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
