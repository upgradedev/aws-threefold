"""The role CI deploys with may build everything the template declares, and only that.

deploy/iam/github-deploy-policy.json is the whole of what the deploy workflow's
role may do. A resource type added to deploy/template.yml without a matching
grant is found by the deploy, on the live stack, halfway through an update that
CloudFormation then rolls back. This test finds it first: it reads every
resource type out of the template, including the ones the Serverless transform
expands a function and an HTTP API into, and for each one checks that every
action CloudFormation calls to create, read, update, tag and delete it is
allowed on the ARN that resource will have in threefold-prod.

Where each type's actions come from, so the list can be checked rather than
trusted:
- SNS topic, topic policy and subscription, log group and metric filter: the
  handler permissions in the open-source resource provider schemas
  (aws-cloudformation-resource-providers-sns and -logs on GitHub), plus the
  current tagging actions, because the workflow tags the stack with the commit
  and CloudFormation re-tags every taggable resource on each deploy. The
  subscription's own calls (GetSubscriptionAttributes, SetSubscriptionAttributes,
  Unsubscribe) take the topic as their resource in the Service Authorization
  Reference for Amazon SNS, so they are granted on this stack's topic and
  checked against both the topic's ARN and a subscription's, which is the
  topic's with an id appended.
- The HTTP API stage's access log: the API Gateway developer guide, "Configure
  logging for HTTP APIs", which lists the log-delivery actions and gives them
  Resource "*".
- Alarms, the dashboard, the budget, the table's continuous backups: each
  service's API for the property the template sets, with the resource type the
  Service Authorization Reference gives each action.
- Tracing: the AWS SAM developer guide, AWS::Serverless::Function, Tracing:
  "AWS SAM adds the arn:aws:iam::aws:policy/AWSXrayWriteOnlyAccess policy to
  the Lambda execution role that it creates", which also carries
  AWSLambdaBasicExecutionRole from the stack's first deploy.

The policy is matched as IAM matches it: an action pattern is case-insensitive
with '*' and '?', a resource pattern is case-sensitive and its '*' crosses ':'
and '/', and a statement with a condition applies only when the condition holds.
The account id is read out of the policy itself, never written here. Nothing
calls AWS.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[2]
POLICY = json.loads((ROOT / "deploy" / "iam" / "github-deploy-policy.json").read_text(encoding="utf-8"))
TEMPLATE = (ROOT / "deploy" / "template.yml").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
STATEMENTS: List[dict] = POLICY["Statement"]


def _workflow_env(name: str) -> str:
    match = re.search(rf"^  {name}: (\S+)$", WORKFLOW, re.M)
    assert match, f"deploy.yml sets no {name}"
    return match.group(1)


STACK = _workflow_env("STACK_NAME")
REGION = _workflow_env("AWS_REGION")
BUCKET = _workflow_env("PACKAGING_BUCKET")
_STACK_ARN = re.search(
    rf"arn:aws:cloudformation:{re.escape(REGION)}:([0-9]{{12}}):stack/{re.escape(STACK)}\b", json.dumps(POLICY)
)
assert _STACK_ARN, "the policy names no CloudFormation stack in the workflow's region under the workflow's name"
ACCOUNT = _STACK_ARN.group(1)
# A name CloudFormation generates is the stack name, the logical id and a random
# suffix; this stands in for the suffix.
SUFFIX = "AcmeGen01"
BASIC_EXECUTION = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
XRAY_WRITE_ONLY = "arn:aws:iam::aws:policy/AWSXrayWriteOnlyAccess"


# ------------------------------------------------------------------ IAM, modelled


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def _pattern(text: str, case_sensitive: bool) -> "re.Pattern[str]":
    body = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in text)
    return re.compile(body, 0 if case_sensitive else re.IGNORECASE)


def _condition_holds(condition: Optional[dict], context: Dict[str, str]) -> bool:
    if not condition:
        return True
    for operator, clauses in condition.items():
        if operator not in ("ArnEquals", "ArnLike", "StringEquals", "StringLike"):
            raise AssertionError(f"the model does not read the {operator} condition operator")
        for key, expected in clauses.items():
            actual = context.get(key)
            if actual is None:
                return False
            if not any(_pattern(e, True).fullmatch(actual) for e in _as_list(expected)):
                return False
    return True


def allows(action: str, resource: str, context: Optional[Dict[str, str]] = None) -> bool:
    """Whether some Allow statement grants the action on the resource, as IAM reads it."""
    for statement in STATEMENTS:
        if statement["Effect"] != "Allow":
            continue
        if not any(_pattern(a, False).fullmatch(action) for a in _as_list(statement["Action"])):
            continue
        if not any(_pattern(r, True).fullmatch(resource) for r in _as_list(statement["Resource"])):
            continue
        if _condition_holds(statement.get("Condition"), context or {}):
            return True
    return False


@pytest.mark.parametrize(
    "action, resource, expected",
    [
        ("LAMBDA:GetFunction", f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{STACK}-F-{SUFFIX}", True),
        ("lambda:GetFunction", f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:threefold-other-F", False),
        ("logs:CreateLogGroup", f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{STACK}-F:*", True),
        ("dynamodb:DeleteTable", f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{STACK.upper()}-T", False),
        ("iam:AttachRolePolicy", f"arn:aws:iam::{ACCOUNT}:role/{STACK}-R", False),
    ],
)
def test_the_model_matches_as_iam_does(action: str, resource: str, expected: bool) -> None:
    assert allows(action, resource) is expected


# ------------------------------------------------------------ what the template holds


def _resources() -> Dict[str, str]:
    """Each resource's logical id and type."""
    section = re.search(r"^Resources:\n(.*?)^\S", TEMPLATE, re.S | re.M)
    assert section, "the template has no Resources section"
    body = section.group(1)
    found = dict(re.findall(r"^  (\w+):\n(?:    (?!Type:)\S.*\n|      .*\n)*?    Type: (\S+)$", body, re.M))
    # Every resource declares a type, so a resource the pattern misses (one
    # with Condition written above Type, say) shows up as a shortfall here
    # instead of as a type nobody checked.
    assert len(found) == len(re.findall(r"^    Type: ", body, re.M)) >= 20, "the scan has gone blind"
    return found


def _names(key: str) -> List[str]:
    """Every value of a name property, as it reads in this stack."""
    values = re.findall(rf"^\s+{key}: !Sub '([^']+)'$", TEMPLATE, re.M)
    return [v.replace("${AWS::StackName}", STACK) for v in values]


# The types the Serverless transform writes in place of its own. Each HttpApi
# event on the function becomes a Lambda permission, and Tracing adds a
# managed policy to the generated role (checked on its own below).
TRANSFORMED = {
    "AWS::Serverless::Function": ["AWS::Lambda::Function", "AWS::IAM::Role", "AWS::Lambda::Permission"],
    "AWS::Serverless::HttpApi": ["AWS::ApiGatewayV2::Api", "AWS::ApiGatewayV2::Stage"],
}


def _deployed_types() -> List[str]:
    types: List[str] = []
    for resource_type in sorted(set(_resources().values())):
        types.extend(TRANSFORMED.get(resource_type, [resource_type]))
    return sorted(set(types))


FUNCTION_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{STACK}-ThreefoldFunction-{SUFFIX}"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{STACK}-ThreefoldFunctionRole-{SUFFIX}"
TABLE_ARN = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{STACK}-ThreefoldTable-{SUFFIX}"
BUCKET_ARN = f"arn:aws:s3:::{STACK}-evidencebucket-{SUFFIX.lower()}"
FUNCTION_LOG_GROUP = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{STACK}-ThreefoldFunction-{SUFFIX}"
API_ARN = f"arn:aws:apigateway:{REGION}::/apis/acme0api01"


def _log_group_arns() -> List[str]:
    """Both log groups, the access log's name read from the template."""
    named = [n for n in _names("LogGroupName") if not n.startswith("/aws/lambda/")]
    assert named == [f"/aws/vendedlogs/apigateway/{STACK}/access"], named
    return [FUNCTION_LOG_GROUP] + [f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{n}" for n in named]


def _topic_arn() -> str:
    (name,) = _names("TopicName")
    return f"arn:aws:sns:{REGION}:{ACCOUNT}:{name}"


def _required() -> Dict[str, List[tuple]]:
    """Per deployed type, (action, resource ARN) pairs CloudFormation needs granted."""
    topic = _topic_arn()
    (dashboard,) = _names("DashboardName")
    (budget,) = _names("BudgetName")
    alarms = [f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:{name}" for name in _names("AlarmName")]
    log_groups = _log_group_arns()

    def each(actions: List[str], resources: List[str]) -> List[tuple]:
        return [(a, r) for a in actions for r in resources]

    tags = ["TagResource", "UntagResource", "ListTagsForResource"]
    return {
        "AWS::Lambda::Function": each(
            [
                "lambda:CreateFunction", "lambda:GetFunction", "lambda:GetFunctionConfiguration",
                "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration", "lambda:PutFunctionConcurrency",
                "lambda:DeleteFunctionConcurrency", "lambda:TagResource", "lambda:UntagResource", "lambda:ListTags",
                "lambda:DeleteFunction",
            ],
            [FUNCTION_ARN],
        ),
        "AWS::Lambda::Permission": each(["lambda:AddPermission", "lambda:RemovePermission", "lambda:GetPolicy"], [FUNCTION_ARN]),
        "AWS::IAM::Role": each(
            [
                "iam:CreateRole", "iam:GetRole", "iam:PassRole", "iam:PutRolePolicy", "iam:GetRolePolicy",
                "iam:DeleteRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:TagRole",
                "iam:UntagRole", "iam:DeleteRole",
            ],
            [ROLE_ARN],
        ),
        "AWS::ApiGatewayV2::Api": each(
            ["apigateway:POST", "apigateway:GET", "apigateway:PATCH", "apigateway:PUT", "apigateway:DELETE"], [API_ARN]
        ),
        "AWS::ApiGatewayV2::Stage": each(
            ["apigateway:POST", "apigateway:GET", "apigateway:PATCH", "apigateway:DELETE"], [f"{API_ARN}/stages/prod"]
        )
        + each(
            [
                "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
                "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies",
            ],
            ["*"],
        )
        + each(["logs:DescribeLogGroups"], log_groups[1:]),
        "AWS::DynamoDB::Table": each(
            [
                "dynamodb:CreateTable", "dynamodb:DescribeTable", "dynamodb:UpdateTable", "dynamodb:DeleteTable",
                "dynamodb:UpdateTimeToLive", "dynamodb:DescribeTimeToLive", "dynamodb:UpdateContinuousBackups",
                "dynamodb:DescribeContinuousBackups", "dynamodb:DescribeContributorInsights",
                "dynamodb:DescribeKinesisStreamingDestination", "dynamodb:GetResourcePolicy",
                "dynamodb:TagResource", "dynamodb:UntagResource", "dynamodb:ListTagsOfResource",
            ],
            [TABLE_ARN],
        ),
        "AWS::S3::Bucket": each(
            [
                "s3:CreateBucket", "s3:PutBucketPublicAccessBlock", "s3:PutEncryptionConfiguration",
                "s3:PutLifecycleConfiguration", "s3:PutBucketTagging", "s3:GetBucketTagging", "s3:DeleteBucket",
            ],
            [BUCKET_ARN],
        ),
        "AWS::Logs::LogGroup": each(
            [
                "logs:CreateLogGroup", "logs:DescribeLogGroups", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy",
                "logs:TagLogGroup", "logs:UntagLogGroup", "logs:ListTagsLogGroup", "logs:DeleteLogGroup",
                *(f"logs:{t}" for t in tags),
            ],
            log_groups + [arn + ":*" for arn in log_groups],
        ),
        "AWS::Logs::MetricFilter": each(
            ["logs:PutMetricFilter", "logs:DescribeMetricFilters", "logs:DeleteMetricFilter"], [FUNCTION_LOG_GROUP]
        ),
        "AWS::SNS::Topic": each(
            [
                "sns:CreateTopic", "sns:GetTopicAttributes", "sns:SetTopicAttributes", "sns:Subscribe",
                "sns:ListSubscriptionsByTopic", "sns:GetDataProtectionPolicy", "sns:PutDataProtectionPolicy",
                "sns:DeleteTopic", *(f"sns:{t}" for t in tags),
            ],
            [topic],
        ),
        "AWS::SNS::TopicPolicy": each(["sns:SetTopicAttributes"], [topic]),
        "AWS::SNS::Subscription": each(["sns:Subscribe", "sns:GetTopicAttributes"], [topic])
        + each(
            ["sns:GetSubscriptionAttributes", "sns:SetSubscriptionAttributes", "sns:Unsubscribe"],
            [topic, f"{topic}:acme-0000-subscription"],
        ),
        "AWS::CloudWatch::Alarm": each(
            ["cloudwatch:PutMetricAlarm", "cloudwatch:DescribeAlarms", "cloudwatch:DeleteAlarms",
             *(f"cloudwatch:{t}" for t in tags)],
            alarms,
        ),
        "AWS::CloudWatch::Dashboard": each(
            ["cloudwatch:PutDashboard", "cloudwatch:GetDashboard", "cloudwatch:DeleteDashboards"],
            [f"arn:aws:cloudwatch::{ACCOUNT}:dashboard/{dashboard}"],
        )
        + each(["cloudwatch:ListDashboards"], ["*"]),
        "AWS::Budgets::Budget": each(
            ["budgets:ModifyBudget", "budgets:ViewBudget", *(f"budgets:{t}" for t in tags)],
            [f"arn:aws:budgets::{ACCOUNT}:budget/{budget}"],
        ),
    }


REQUIRED = _required()


# -------------------------------------------------------------------- the coverage


def test_every_resource_type_the_template_deploys_has_been_checked_against_the_role() -> None:
    """A new type fails here, naming itself, until its actions are listed and granted."""
    unchecked = [t for t in _deployed_types() if t not in REQUIRED]
    assert not unchecked, f"{unchecked} deploy with no check that the deploy role may create them"


def test_no_type_is_checked_that_the_template_no_longer_deploys() -> None:
    assert sorted(REQUIRED) == _deployed_types()


@pytest.mark.parametrize("resource_type", sorted(REQUIRED))
def test_the_role_may_do_everything_cloudformation_does_to_each_type(resource_type: str) -> None:
    missing = [(action, resource) for action, resource in REQUIRED[resource_type] if not allows(action, resource)]
    assert not missing, f"{resource_type}: the deploy role is refused {missing}"


def test_every_alarm_the_template_names_is_this_stacks_to_manage() -> None:
    names = _names("AlarmName")
    assert len(names) == list(_resources().values()).count("AWS::CloudWatch::Alarm")
    for name in names:
        assert allows("cloudwatch:PutMetricAlarm", f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:{name}"), name


def test_tracing_is_on_so_the_x_ray_policy_has_to_be_attachable() -> None:
    assert re.search(r"^      Tracing: Active$", TEMPLATE, re.M)
    for managed in (BASIC_EXECUTION, XRAY_WRITE_ONLY):
        for action in ("iam:AttachRolePolicy", "iam:DetachRolePolicy"):
            assert allows(action, ROLE_ARN, {"iam:PolicyARN": managed}), (action, managed)


@pytest.mark.parametrize(
    "policy_arn",
    [
        "arn:aws:iam::aws:policy/AdministratorAccess",
        "arn:aws:iam::aws:policy/IAMFullAccess",
        "arn:aws:iam::aws:policy/AWSXrayWriteOnlyAccessExtra",
        None,
    ],
)
def test_the_role_may_attach_no_other_managed_policy(policy_arn) -> None:
    """Attaching any policy to a role the deploy role can pass to a function would be every permission."""
    context = {"iam:PolicyARN": policy_arn} if policy_arn else {}
    assert not allows("iam:AttachRolePolicy", ROLE_ARN, context)


# ------------------------------------------------------------------- and only that


# Actions IAM authorizes against no resource, or whose resource a stack cannot
# be named in, each with the reason it is here. Anything else with Resource "*"
# is a grant on the whole account and fails the test below.
RESOURCELESS = {
    "cloudformation:ValidateTemplate": "takes a template, not a resource",
    "lambda:GetAccountSettings": "reads the account's concurrency, which ReservedConcurrency needs",
    "lambda:ListFunctions": "a listing",
    "apigateway:GET": "API ids are generated, so no ARN can name this stack's API in advance",
    "apigateway:POST": "as above",
    "apigateway:PUT": "as above",
    "apigateway:PATCH": "as above",
    "apigateway:DELETE": "as above",
    "cloudwatch:ListDashboards": "a listing",
    "logs:CreateLogDelivery": "the HTTP API logging guide grants the delivery actions on '*'",
    "logs:GetLogDelivery": "as above",
    "logs:UpdateLogDelivery": "as above",
    "logs:DeleteLogDelivery": "as above",
    "logs:ListLogDeliveries": "as above",
    "logs:PutResourcePolicy": "account-level: the log resource policy that lets API Gateway write",
    "logs:DescribeResourcePolicies": "as above",
}


def test_the_policy_is_plain_allow_statements_each_with_its_own_sid() -> None:
    assert POLICY["Version"] == "2012-10-17"
    sids = [s["Sid"] for s in STATEMENTS]
    assert len(set(sids)) == len(sids)
    for statement in STATEMENTS:
        assert statement["Effect"] == "Allow", statement["Sid"]
        assert not {"NotAction", "NotResource", "Principal"} & set(statement), statement["Sid"]
        assert re.fullmatch(r"[A-Za-z0-9]+", statement["Sid"]), "IAM's pattern for a Sid"


def test_only_actions_that_take_no_resource_are_granted_on_everything() -> None:
    for statement in STATEMENTS:
        if "*" in _as_list(statement["Resource"]):
            for action in _as_list(statement["Action"]):
                assert action in RESOURCELESS, f"{statement['Sid']} grants {action} on every resource in the account"


def test_every_named_resource_belongs_to_this_stack_or_its_deployment() -> None:
    allowed_outside = {
        f"arn:aws:cloudformation:{REGION}:aws:transform/Serverless-2016-10-31",
        f"arn:aws:s3:::{BUCKET}",
        f"arn:aws:s3:::{BUCKET}/threefold/*",
        # DescribeLogGroups is a listing: IAM checks it against every group in
        # the region, and API Gateway's logging guide grants it so.
        f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:*",
    }
    for statement in STATEMENTS:
        for resource in _as_list(statement["Resource"]):
            if resource == "*" or resource in allowed_outside:
                continue
            assert STACK in resource, f"{statement['Sid']} names {resource}, outside {STACK}"
            assert f":{ACCOUNT}:" in resource or resource.startswith("arn:aws:s3:::"), resource


@pytest.mark.parametrize(
    "action, resource",
    [
        ("lambda:DeleteFunction", f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:threefold-dogfood-ThreefoldFunction-X"),
        ("dynamodb:DeleteTable", f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/threefold-dogfood-ThreefoldTable-X"),
        ("dynamodb:UpdateContinuousBackups", f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/acme-ledger"),
        ("logs:DeleteLogGroup", f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/threefold-dogfood-F"),
        ("logs:DeleteLogGroup", f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/vendedlogs/apigateway/threefold-dogfood/access"),
        ("sns:DeleteTopic", f"arn:aws:sns:{REGION}:{ACCOUNT}:threefold-dogfood-alarms"),
        # Another workload's alerting: the role may neither drop nor redirect
        # a subscription it did not create, by the topic or by the subscription.
        ("sns:Unsubscribe", f"arn:aws:sns:{REGION}:{ACCOUNT}:threefold-dogfood-alarms:acme-0000-subscription"),
        ("sns:Unsubscribe", f"arn:aws:sns:{REGION}:{ACCOUNT}:acme-billing-alerts"),
        ("sns:SetSubscriptionAttributes", f"arn:aws:sns:{REGION}:{ACCOUNT}:acme-billing-alerts:acme-0000-subscription"),
        ("sns:GetSubscriptionAttributes", f"arn:aws:sns:us-east-1:{ACCOUNT}:{STACK}-alarms:acme-0000-subscription"),
        ("cloudwatch:DeleteAlarms", f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:threefold-dogfood-function-errors"),
        ("cloudwatch:DeleteDashboards", f"arn:aws:cloudwatch::{ACCOUNT}:dashboard/threefold-dogfood-operations"),
        ("budgets:ModifyBudget", f"arn:aws:budgets::{ACCOUNT}:budget/acme-team-budget"),
        ("iam:PutRolePolicy", f"arn:aws:iam::{ACCOUNT}:role/threefold-github-deploy"),
        ("s3:PutObject", f"arn:aws:s3:::{BUCKET}/elsewhere/template.yml"),
        ("lambda:DeleteFunction", f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:{STACK}-ThreefoldFunction-X"),
    ],
)
def test_the_role_cannot_touch_the_other_stack_or_anything_else(action: str, resource: str) -> None:
    assert not allows(action, resource)


def test_the_policy_fits_the_inline_quota_of_one_role() -> None:
    """RUNBOOK step 1 attaches it with put-role-policy: 10,240 characters in all, whitespace not counted."""
    assert len(json.dumps(POLICY, separators=(",", ":"))) <= 10240


# ----------------------------------------------------------------- the workflow


def _step(name: str) -> str:
    match = re.search(rf"^      - name: {re.escape(name)}\n(.*?)(?=^      - |^  \w|\Z)", WORKFLOW, re.S | re.M)
    assert match, f"deploy.yml has no step named {name}"
    return match.group(1)


def test_the_deploy_sends_the_template_through_the_bucket_the_role_may_write() -> None:
    deploy = _step("Deploy")
    assert re.search(r'--s3-bucket "\$PACKAGING_BUCKET"', deploy), "a template over 51,200 bytes is refused inline"
    prefix = re.search(r"--s3-prefix (\S+)", deploy)
    assert prefix, "without a prefix the template lands at the bucket root, which the role may not write"
    assert prefix.group(1) == re.search(r"--s3-prefix (\S+)", _step("Package")).group(1)
    assert allows("s3:PutObject", f"arn:aws:s3:::{BUCKET}/{prefix.group(1)}/acme0template.template")
    assert allows("s3:GetObject", f"arn:aws:s3:::{BUCKET}/{prefix.group(1)}/acme0template.template")


def test_the_deploy_never_sends_the_edge_secret() -> None:
    """Parameters the command leaves out keep the stack's values; the secret is set once, by the owner."""
    deploy = _step("Deploy")
    assert "EdgeOriginSecret" not in deploy
    assert "--parameter-overrides" not in deploy
    assert "secrets." not in WORKFLOW, "no repository secret carries it either"


def test_the_workflow_deploys_the_stack_and_region_the_policy_names() -> None:
    deploy = _step("Deploy")
    assert '--stack-name "$STACK_NAME"' in deploy and '--region "$AWS_REGION"' in deploy
    assert allows("cloudformation:CreateChangeSet", f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{STACK}/acme-0001")
