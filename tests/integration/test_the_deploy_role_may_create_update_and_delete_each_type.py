"""Coarsely: for every resource type the template deploys, the deploy role may create, update and delete it.

test_the_deploy_role_covers_the_template.py checks each action CloudFormation
calls against the ARN the resource will have. That list is long and written by
hand, and it once held no update action for the KMS key or the IAM roles, so a
change to the key's description or a role's trust policy would have passed
every test and been refused on the live stack. This check is deliberately
blunter and reads the template on its own: every `Type:` under Resources, with
the Serverless transform's types expanded, must have a row below naming what
creates, updates and deletes it, and the policy must hold an Allow statement
for each of those actions, on "*" or on that service's own ARNs. A resource
type added to the template fails here until someone has thought about all
three. Nothing calls AWS.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

import pytest

ROOT = Path(__file__).resolve().parents[2]
POLICY = json.loads((ROOT / "deploy" / "iam" / "github-deploy-policy.json").read_text(encoding="utf-8"))
TEMPLATE = (ROOT / "deploy" / "template.yml").read_text(encoding="utf-8")

# What the Serverless transform writes in place of each of its own types.
TRANSFORMED = {
    "AWS::Serverless::Function": ["AWS::Lambda::Function", "AWS::IAM::Role", "AWS::Lambda::Permission"],
    "AWS::Serverless::HttpApi": ["AWS::ApiGatewayV2::Api", "AWS::ApiGatewayV2::Stage"],
}

# Per type, the API actions that create it, change a property the template
# sets, and delete it. Where a service authorizes all three as one action, the
# row says so rather than leaving a column empty.
LIFECYCLE: Dict[str, Dict[str, List[str]]] = {
    # API Gateway authorizes by HTTP verb on the API's path.
    "AWS::ApiGatewayV2::Api": {
        "create": ["apigateway:POST"], "update": ["apigateway:PATCH", "apigateway:PUT"], "delete": ["apigateway:DELETE"],
    },
    "AWS::ApiGatewayV2::Stage": {
        "create": ["apigateway:POST"], "update": ["apigateway:PATCH"], "delete": ["apigateway:DELETE"],
    },
    # CreateBudget, UpdateBudget and DeleteBudget are all authorized as ModifyBudget.
    "AWS::Budgets::Budget": {
        "create": ["budgets:ModifyBudget"], "update": ["budgets:ModifyBudget"], "delete": ["budgets:ModifyBudget"],
    },
    "AWS::CloudWatch::Alarm": {
        "create": ["cloudwatch:PutMetricAlarm"], "update": ["cloudwatch:PutMetricAlarm"],
        "delete": ["cloudwatch:DeleteAlarms"],
    },
    "AWS::CloudWatch::Dashboard": {
        "create": ["cloudwatch:PutDashboard"], "update": ["cloudwatch:PutDashboard"],
        "delete": ["cloudwatch:DeleteDashboards"],
    },
    "AWS::DynamoDB::Table": {
        "create": ["dynamodb:CreateTable"],
        "update": ["dynamodb:UpdateTable", "dynamodb:UpdateTimeToLive", "dynamodb:UpdateContinuousBackups"],
        "delete": ["dynamodb:DeleteTable"],
    },
    # The template writes the schedule role's trust policy and both roles'
    # inline policies; the function role's managed policies are checked in
    # the fine-grained test, under their condition.
    "AWS::IAM::Role": {
        "create": ["iam:CreateRole"], "update": ["iam:UpdateAssumeRolePolicy", "iam:PutRolePolicy"],
        "delete": ["iam:DeleteRole"],
    },
    # A key is never deleted at once: CloudFormation schedules its deletion.
    "AWS::KMS::Key": {
        "create": ["kms:CreateKey"], "update": ["kms:UpdateKeyDescription", "kms:PutKeyPolicy"],
        "delete": ["kms:ScheduleKeyDeletion"],
    },
    "AWS::Lambda::EventInvokeConfig": {
        "create": ["lambda:PutFunctionEventInvokeConfig"], "update": ["lambda:UpdateFunctionEventInvokeConfig"],
        "delete": ["lambda:DeleteFunctionEventInvokeConfig"],
    },
    "AWS::Lambda::Function": {
        "create": ["lambda:CreateFunction"],
        "update": ["lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration", "lambda:PutFunctionConcurrency"],
        "delete": ["lambda:DeleteFunction"],
    },
    # Every property of a Lambda permission is create-only, so CloudFormation
    # updates one by replacing it: the new statement added, the old removed.
    "AWS::Lambda::Permission": {
        "create": ["lambda:AddPermission"], "update": ["lambda:AddPermission", "lambda:RemovePermission"],
        "delete": ["lambda:RemovePermission"],
    },
    "AWS::Logs::LogGroup": {
        "create": ["logs:CreateLogGroup"], "update": ["logs:PutRetentionPolicy"], "delete": ["logs:DeleteLogGroup"],
    },
    "AWS::Logs::MetricFilter": {
        "create": ["logs:PutMetricFilter"], "update": ["logs:PutMetricFilter"], "delete": ["logs:DeleteMetricFilter"],
    },
    "AWS::S3::Bucket": {
        "create": ["s3:CreateBucket"],
        "update": ["s3:PutEncryptionConfiguration", "s3:PutLifecycleConfiguration", "s3:PutBucketPublicAccessBlock"],
        "delete": ["s3:DeleteBucket"],
    },
    "AWS::Scheduler::Schedule": {
        "create": ["scheduler:CreateSchedule"], "update": ["scheduler:UpdateSchedule"],
        "delete": ["scheduler:DeleteSchedule"],
    },
    "AWS::SNS::Subscription": {
        "create": ["sns:Subscribe"], "update": ["sns:SetSubscriptionAttributes"], "delete": ["sns:Unsubscribe"],
    },
    "AWS::SNS::Topic": {
        "create": ["sns:CreateTopic"], "update": ["sns:SetTopicAttributes"], "delete": ["sns:DeleteTopic"],
    },
    # A topic policy is the topic's Policy attribute: set, set again, and set
    # back to the default.
    "AWS::SNS::TopicPolicy": {
        "create": ["sns:SetTopicAttributes"], "update": ["sns:SetTopicAttributes"],
        "delete": ["sns:SetTopicAttributes"],
    },
}


def _deployed_types() -> List[str]:
    section = re.search(r"^Resources:\n(.*?)^\S", TEMPLATE, re.S | re.M)
    assert section, "the template has no Resources section"
    declared = re.findall(r"^    Type: (AWS::\S+)$", section.group(1), re.M)
    assert len(declared) >= 20, "the scan has gone blind"
    types = set()
    for resource_type in declared:
        types.update(TRANSFORMED.get(resource_type, [resource_type]))
    return sorted(types)


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def _matches(pattern: str, action: str) -> bool:
    body = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(body, action, re.IGNORECASE) is not None


def _granted(action: str) -> bool:
    """Some Allow statement lists the action, on '*' or on ARNs of the action's own service."""
    service = action.split(":")[0]
    for statement in POLICY["Statement"]:
        if statement["Effect"] != "Allow":
            continue
        if not any(_matches(a, action) for a in _as_list(statement["Action"])):
            continue
        if all(r == "*" or r.startswith(f"arn:aws:{service}:") for r in _as_list(statement["Resource"])):
            return True
    return False


def test_every_type_the_template_deploys_has_a_lifecycle_row() -> None:
    missing = [t for t in _deployed_types() if t not in LIFECYCLE]
    assert not missing, f"{missing}: say what creates, updates and deletes each, and grant it"


def test_no_row_is_kept_for_a_type_the_template_no_longer_deploys() -> None:
    assert sorted(LIFECYCLE) == _deployed_types()


@pytest.mark.parametrize("resource_type", sorted(LIFECYCLE))
def test_the_role_may_create_update_and_delete_it(resource_type: str) -> None:
    row = LIFECYCLE[resource_type]
    assert sorted(row) == ["create", "delete", "update"] and all(row.values()), resource_type
    refused = [(step, action) for step, actions in row.items() for action in actions if not _granted(action)]
    assert not refused, f"{resource_type}: the deploy role holds no grant for {refused}"


def test_no_action_is_a_wildcard_but_lambdas_on_this_stacks_functions() -> None:
    """A row above is satisfied by naming its action, never by a pattern that also grants what nobody listed.

    lambda:* predates this check and is kept: it is scoped to functions named
    threefold-prod-*, and UpdateFunctionCode alone already lets the role run
    any code as those functions.
    """
    for statement in POLICY["Statement"]:
        for action in _as_list(statement["Action"]):
            if not set(action) & {"*", "?"}:
                continue
            assert action == "lambda:*", f"{statement['Sid']} grants the pattern {action}"
            for resource in _as_list(statement["Resource"]):
                assert re.fullmatch(r"arn:aws:lambda:[a-z0-9-]+:[0-9]{12}:function:threefold-prod-\*", resource), resource
