"""The edge stack keeps the properties it exists for.

deploy/edge.yml is read as a structure (by the reader in test_edge_cfn_yaml.py),
so each property is checked where CloudFormation will read it rather than
somewhere in the text: the bucket is private, encrypted and versioned and
answers only this distribution; the pages origin uses origin access control;
the API origin is the existing API over TLS; every behavior carries the
security headers; the web ACL has its four rules with metrics on, and its body
rules count instead of blocking a tool call's source code; both API origins
send the edge secret, and every API behavior tells the function the viewer's
host, under header names the code reads. Nothing here calls AWS; the viewer
host function's code is run by node when node is installed.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest


def _reader():
    name = "edge_cfn_yaml_reader"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("test_edge_cfn_yaml.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


READER = _reader()
TEMPLATE = READER.load_edge_template()
RESOURCES: Dict[str, Any] = TEMPLATE["Resources"]
PARAMETERS: Dict[str, Any] = TEMPLATE["Parameters"]
DISTRIBUTION = RESOURCES["Distribution"]["Properties"]["DistributionConfig"]
BEHAVIORS: List[dict] = DISTRIBUTION["CacheBehaviors"]
ORIGINS = {origin["Id"]: origin for origin in DISTRIBUTION["Origins"]}
# Every behavior that does not serve the bucket serves the API, through one of its
# two origins: "api" prefixes the stage, "api-stage" passes a staged path through.
API_BEHAVIORS: List[dict] = [b for b in BEHAVIORS if b["TargetOriginId"] != "web"]
HEADERS = RESOURCES["SecurityHeadersPolicy"]["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]
WEB_ACL = RESOURCES["WebAcl"]["Properties"]
ACL_RULES = {rule["Name"]: rule for rule in WEB_ACL["Rules"]}

# Managed policy ids, from the CloudFront developer guide. Named here so a typo in
# the template fails a test instead of attaching some other policy, or none.
CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
ALL_VIEWER_EXCEPT_HOST_HEADER = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
ALL_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"}
EXAMPLE_API_DOMAIN = "abc123def4.execute-api.eu-west-1.amazonaws.com"

# The rules of the two managed groups that inspect the request body, as AWS
# documents them. WAF silently ignores an override naming a rule a managed group
# does not have, so a misspelt name here would leave the rule blocking with no
# error anywhere: the test pins the exact names.
COMMON_BODY_RULES = {
    "SizeRestrictions_BODY", "CrossSiteScripting_BODY", "GenericLFI_BODY", "GenericRFI_BODY", "EC2MetaDataSSRF_BODY",
}
KNOWN_BAD_INPUTS_BODY_RULES = {"JavaDeserializationRCE_BODY", "Log4JRCE_BODY", "ReactJSRCE_BODY"}
# The one rule outside the body that is relaxed, and why: it refuses paths by an
# extension list AWS does not publish, and the install path serves .py files and a
# zip on purpose.
URI_RULES_THAT_COUNT = {"RestrictedExtensions_URIPATH"}
# Web ACL capacity units of each managed group. Above 1,500 in one web ACL every
# request is billed extra, so the sum is checked.
MANAGED_GROUP_WCU = {
    "AWSManagedRulesCommonRuleSet": 700,
    "AWSManagedRulesKnownBadInputsRuleSet": 200,
    "AWSManagedRulesAmazonIpReputationList": 25,
}
PSEUDO_PARAMETERS = {
    "AWS::AccountId", "AWS::NoValue", "AWS::Partition", "AWS::Region", "AWS::StackName", "AWS::StackId",
    "AWS::URLSuffix", "AWS::NotificationARNs",
}


def _walk(node: Any) -> Iterator[Any]:
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else [value]


def csp_directives() -> Dict[str, List[str]]:
    policy = HEADERS["ContentSecurityPolicy"]["ContentSecurityPolicy"]
    text = policy["Fn::Sub"].replace("${ApiDomainName}", EXAMPLE_API_DOMAIN)
    directives: Dict[str, List[str]] = {}
    for part in text.split(";"):
        words = part.split()
        if words:
            assert words[0] not in directives, f"{words[0]} is declared twice"
            directives[words[0]] = words[1:]
    return directives


# --- where it can be deployed ---------------------------------------------------------


def test_the_stack_refuses_to_deploy_outside_us_east_1() -> None:
    assertions = TEMPLATE["Rules"]["CloudFrontScopeLivesInUsEast1"]["Assertions"]
    assert {"Fn::Equals": [{"Ref": "AWS::Region"}, "us-east-1"]} in [item["Assert"] for item in assertions]


def test_every_reference_resolves_to_a_parameter_a_resource_or_a_pseudo_parameter() -> None:
    known = set(PARAMETERS) | set(RESOURCES) | PSEUDO_PARAMETERS
    for node in _walk(TEMPLATE):
        if not isinstance(node, dict):
            continue
        if "Ref" in node:
            assert node["Ref"] in known, f"!Ref {node['Ref']} names nothing"
        if "Fn::GetAtt" in node:
            assert node["Fn::GetAtt"][0] in RESOURCES, f"!GetAtt {node['Fn::GetAtt']} names no resource"
        if "Fn::Sub" in node and isinstance(node["Fn::Sub"], str):
            for name in re.findall(r"\$\{([^}!]+)\}", node["Fn::Sub"]):
                assert name.split(".")[0] in known, f"${{{name}}} names nothing"
    conditions = set(TEMPLATE["Conditions"])
    for resource in RESOURCES.values():
        if "Condition" in resource:
            assert resource["Condition"] in conditions


def test_every_parameter_is_used() -> None:
    text = repr(TEMPLATE["Resources"]) + repr(TEMPLATE["Conditions"])
    for name in PARAMETERS:
        assert f"'Ref': '{name}'" in text or f"${{{name}}}" in text, f"{name} is declared and never used"


def test_the_template_names_no_account_and_no_deployed_api() -> None:
    text = repr(TEMPLATE)
    assert not re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", text), "an account id is in the template"
    assert "execute-api" not in repr(PARAMETERS["ApiDomainName"].get("Default", "")), "the API is a parameter"


# --- the pages bucket ------------------------------------------------------------------


def test_the_pages_bucket_is_private_encrypted_versioned_and_acl_free() -> None:
    bucket = RESOURCES["WebBucket"]["Properties"]
    assert bucket["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True,
    }
    rules = bucket["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert rules and all(
        rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"] in ("AES256", "aws:kms", "aws:kms:dsse") for rule in rules
    )
    assert bucket["VersioningConfiguration"] == {"Status": "Enabled"}
    assert bucket["OwnershipControls"]["Rules"] == [{"ObjectOwnership": "BucketOwnerEnforced"}]
    for public_setting in ("WebsiteConfiguration", "AccessControl", "CorsConfiguration"):
        assert public_setting not in bucket, f"{public_setting} would serve the bucket other than through the edge"


def _statements(resource: str) -> List[dict]:
    return RESOURCES[resource]["Properties"]["PolicyDocument"]["Statement"]


def _web_bucket_allows() -> Dict[str, dict]:
    """The pages bucket's grants, by the one action each carries."""
    allows = [s for s in _statements("WebBucketPolicy") if s["Effect"] == "Allow"]
    by_action: Dict[str, dict] = {}
    for allow in allows:
        actions = _as_list(allow["Action"])
        assert len(actions) == 1, f"one action per grant, so each names the one resource it applies to: {actions}"
        assert actions[0] not in by_action, f"{actions[0]} is granted twice"
        by_action[actions[0]] = allow
    return by_action


def test_only_this_distribution_can_read_the_pages_and_only_read_them() -> None:
    allows = _web_bucket_allows()
    assert set(allows) == {"s3:GetObject", "s3:ListBucket"}, (
        "one reader, and it may read a page and learn whether one exists; any other grant fails here"
    )
    for action, allow in allows.items():
        assert allow["Principal"] == {"Service": "cloudfront.amazonaws.com"}, action
        assert set(allow["Condition"]) == {"StringEquals"}, action
        assert set(allow["Condition"]["StringEquals"]) == {"AWS:SourceArn"}, action
        source_arn = allow["Condition"]["StringEquals"]["AWS:SourceArn"]["Fn::Sub"]
        assert source_arn.endswith(":distribution/${Distribution}"), f"the {action} condition must name this distribution"
        assert source_arn.startswith("arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:"), action
    assert allows["s3:GetObject"]["Condition"] == allows["s3:ListBucket"]["Condition"], "the same distribution, word for word"
    assert allows["s3:GetObject"]["Resource"] == {"Fn::Sub": "${WebBucket.Arn}/*"}, "GetObject applies to the pages"
    assert allows["s3:ListBucket"]["Resource"] == {"Fn::GetAtt": ["WebBucket", "Arn"]}, "ListBucket applies to the bucket"


def test_a_page_that_does_not_exist_is_not_found_rather_than_forbidden() -> None:
    """S3 tells a reader that may list the bucket NoSuchKey (404), and one that may not AccessDenied (403).

    The listing permission is how a mistyped page answers 404 without a custom
    error response, which would also replace the API's problem documents.
    """
    allows = _web_bucket_allows()
    assert "s3:ListBucket" in allows, "without it a missing page answers 403 AccessDenied"
    assert "CustomErrorResponses" not in DISTRIBUTION


def test_the_listing_grant_cannot_be_used_to_list_the_pages_through_the_edge() -> None:
    """ListBucket answers a GET on the bucket root with list parameters, and the edge can send neither.

    A request for the root is answered with the default root object, and no page
    behavior forwards a query string, so prefix, list-type and continuation
    parameters never reach the bucket.
    """
    assert DISTRIBUTION["DefaultRootObject"] == "index.html"
    web_behaviors = [DISTRIBUTION["DefaultCacheBehavior"]] + [b for b in BEHAVIORS if b["TargetOriginId"] == "web"]
    for behavior in web_behaviors:
        policy = behavior["CachePolicyId"]
        assert isinstance(policy, dict) and set(policy) == {"Ref"}, "a page behavior uses one of this template's policies"
        key = RESOURCES[policy["Ref"]]["Properties"]["CachePolicyConfig"]["ParametersInCacheKeyAndForwardedToOrigin"]
        assert key["QueryStringsConfig"] == {"QueryStringBehavior": "none"}, behavior.get("PathPattern", "default")
        assert "OriginRequestPolicyId" not in behavior, "an origin request policy could forward the query string anyway"
        assert set(behavior["AllowedMethods"]) == {"GET", "HEAD"}


def test_every_request_to_either_bucket_must_use_tls() -> None:
    for policy, bucket in (("WebBucketPolicy", "WebBucket"), ("LogBucketPolicy", "LogBucket")):
        denies = [s for s in _statements(policy) if s["Effect"] == "Deny"]
        assert any(
            s["Principal"] == "*"
            and _as_list(s["Action"]) == ["s3:*"]
            and s["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
            and {"Fn::GetAtt": [bucket, "Arn"]} in s["Resource"]
            and {"Fn::Sub": f"${{{bucket}.Arn}}/*"} in s["Resource"]
            for s in denies
        ), f"{bucket} accepts plain HTTP"


def test_the_pages_origin_uses_origin_access_control_not_an_identity() -> None:
    assert not [r for r in RESOURCES.values() if r["Type"] == "AWS::CloudFront::CloudFrontOriginAccessIdentity"]
    control = RESOURCES["WebOriginAccessControl"]["Properties"]["OriginAccessControlConfig"]
    assert control["OriginAccessControlOriginType"] == "s3"
    assert control["SigningBehavior"] == "always"
    assert control["SigningProtocol"] == "sigv4"
    web = ORIGINS["web"]
    assert web["DomainName"] == {"Fn::GetAtt": ["WebBucket", "RegionalDomainName"]}
    assert web["OriginAccessControlId"] == {"Fn::GetAtt": ["WebOriginAccessControl", "Id"]}
    assert web["S3OriginConfig"] == {"OriginAccessIdentity": ""}, "OAC needs the identity empty"


def test_the_buckets_survive_a_stack_delete_rather_than_failing_it() -> None:
    for name in ("WebBucket", "LogBucket"):
        assert RESOURCES[name]["DeletionPolicy"] == "Retain"
        assert RESOURCES[name]["UpdateReplacePolicy"] == "Retain"


def test_old_page_versions_expire() -> None:
    rules = RESOURCES["WebBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
    assert any(rule["Status"] == "Enabled" and "NoncurrentVersionExpiration" in rule for rule in rules)


# --- the access logs ---------------------------------------------------------------------


def test_access_logs_go_to_a_separate_private_bucket_with_ownership_controls() -> None:
    logs = RESOURCES["LogBucket"]
    assert logs["Condition"] == "LogsOn"
    assert RESOURCES["LogBucketPolicy"]["Condition"] == "LogsOn"
    properties = logs["Properties"]
    assert properties["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True,
    }
    # Standard logging writes through an ACL grant, so this bucket needs ACLs on,
    # with the bucket owner owning what is written.
    assert properties["OwnershipControls"]["Rules"] == [{"ObjectOwnership": "BucketOwnerPreferred"}]
    assert properties["BucketEncryption"]["ServerSideEncryptionConfiguration"][0][
        "ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256", "standard logging cannot write to SSE-KMS"
    assert any("ExpirationInDays" in rule for rule in properties["LifecycleConfiguration"]["Rules"])
    assert DISTRIBUTION["Logging"] == {
        "Fn::If": [
            "LogsOn",
            {"Bucket": {"Fn::GetAtt": ["LogBucket", "DomainName"]}, "Prefix": "cloudfront/", "IncludeCookies": False},
            {"Ref": "AWS::NoValue"},
        ]
    }
    assert TEMPLATE["Conditions"]["LogsOn"] == {"Fn::Equals": [{"Ref": "AccessLogs"}, "true"]}


# --- the API origin ------------------------------------------------------------------------


def test_the_api_origin_is_the_existing_api_over_tls_only() -> None:
    api = ORIGINS["api"]
    assert api["DomainName"] == {"Ref": "ApiDomainName"}
    assert api["OriginPath"] == {"Ref": "ApiStagePath"}
    assert PARAMETERS["ApiStagePath"]["Default"] == "/prod"
    config = api["CustomOriginConfig"]
    assert config["OriginProtocolPolicy"] == "https-only"
    assert config["OriginSSLProtocols"] == ["TLSv1.2"]


def test_the_staged_origin_is_the_same_api_with_nothing_prefixed() -> None:
    """/prod/status at the edge must reach the API as /prod/status, not /prod/prod/status."""
    staged = ORIGINS["api-stage"]
    assert staged["DomainName"] == {"Ref": "ApiDomainName"}
    assert "OriginPath" not in staged
    assert staged["CustomOriginConfig"] == ORIGINS["api"]["CustomOriginConfig"], "the two differ only in the path"
    assert staged["OriginCustomHeaders"] == ORIGINS["api"]["OriginCustomHeaders"], "the two differ only in the path"
    assert set(staged) == set(ORIGINS["api"]) - {"OriginPath"}
    assert set(ORIGINS) == {"web", "api", "api-stage"}
    uses = [b for b in BEHAVIORS if b["TargetOriginId"] == "api-stage"]
    assert [b["PathPattern"] for b in uses] == [{"Fn::Sub": "${ApiStagePath}/*"}], (
        "one behavior, following the stage parameter"
    )


@pytest.mark.parametrize(
    "value, accepted",
    [
        (EXAMPLE_API_DOMAIN, True),
        ("raa131f9dj.execute-api.eu-west-1.amazonaws.com", True),
        ("https://abc123def4.execute-api.eu-west-1.amazonaws.com", False),
        ("abc123def4.execute-api.eu-west-1.amazonaws.com/prod", False),
        ("example.com", False),
    ],
)
def test_the_api_domain_is_a_host_name_and_nothing_more(value: str, accepted: bool) -> None:
    parameter = PARAMETERS["ApiDomainName"]
    assert "Default" not in parameter, "an edge must never point at an API by omission"
    # CloudFormation anchors AllowedPattern to the whole value, as fullmatch does.
    assert bool(re.fullmatch(parameter["AllowedPattern"], value)) is accepted


# --- the behaviors ----------------------------------------------------------------------------


def test_pages_are_the_default_and_the_edge_keeps_them_briefly() -> None:
    assert DISTRIBUTION["DefaultRootObject"] == "index.html"
    default = DISTRIBUTION["DefaultCacheBehavior"]
    assert default["TargetOriginId"] == "web"
    assert default["ViewerProtocolPolicy"] == "redirect-to-https"
    assert default["AllowedMethods"] == ["GET", "HEAD"]
    assert default["CachePolicyId"] == {"Ref": "PageCachePolicy"}
    ttl = RESOURCES["PageCachePolicy"]["Properties"]["CachePolicyConfig"]
    assert ttl["MinTTL"] == 0 and ttl["DefaultTTL"] <= 300 and ttl["MaxTTL"] <= 600


def test_assets_are_kept_long_at_the_edge() -> None:
    assets = [b for b in BEHAVIORS if b["PathPattern"] == "/assets/*"]
    assert len(assets) == 1
    assert assets[0]["TargetOriginId"] == "web"
    assert assets[0]["AllowedMethods"] == ["GET", "HEAD"]
    assert assets[0]["CachePolicyId"] == {"Ref": "AssetCachePolicy"}
    ttl = RESOURCES["AssetCachePolicy"]["Properties"]["CachePolicyConfig"]
    assert ttl["DefaultTTL"] >= 86400 and ttl["MaxTTL"] >= 30 * 86400


def test_nothing_from_the_viewer_reaches_the_bucket_or_splits_its_cache() -> None:
    for policy in ("PageCachePolicy", "AssetCachePolicy"):
        key = RESOURCES[policy]["Properties"]["CachePolicyConfig"]["ParametersInCacheKeyAndForwardedToOrigin"]
        assert key["CookiesConfig"] == {"CookieBehavior": "none"}
        assert key["HeadersConfig"] == {"HeaderBehavior": "none"}
        assert key["QueryStringsConfig"] == {"QueryStringBehavior": "none"}


def test_api_behaviors_are_uncached_forward_all_but_host_and_accept_every_method() -> None:
    assert {b["TargetOriginId"] for b in API_BEHAVIORS} == {"api", "api-stage"}
    for behavior in API_BEHAVIORS:
        pattern = repr(behavior["PathPattern"])
        assert behavior["CachePolicyId"] == CACHING_DISABLED, pattern
        assert behavior["OriginRequestPolicyId"] == ALL_VIEWER_EXCEPT_HOST_HEADER, pattern
        assert set(behavior["AllowedMethods"]) == ALL_METHODS, pattern
        # A redirected POST arrives as a GET in most clients, and would not be judged.
        assert behavior["ViewerProtocolPolicy"] == "https-only", pattern


def test_every_behavior_targets_a_declared_origin_and_sends_the_security_headers() -> None:
    for behavior in [DISTRIBUTION["DefaultCacheBehavior"], *BEHAVIORS]:
        assert behavior["TargetOriginId"] in ORIGINS
        assert behavior["ResponseHeadersPolicyId"] == {"Ref": "SecurityHeadersPolicy"}
        assert behavior["ViewerProtocolPolicy"] in ("redirect-to-https", "https-only")


def test_no_custom_error_page_replaces_the_api_s_problem_documents() -> None:
    assert "CustomErrorResponses" not in DISTRIBUTION


def test_the_distribution_is_behind_the_web_acl() -> None:
    assert DISTRIBUTION["WebACLId"] == {"Fn::GetAtt": ["WebAcl", "Arn"]}
    assert DISTRIBUTION["Enabled"] is True


# --- the response headers ------------------------------------------------------------------------


def test_the_security_headers_are_all_set_and_override_the_origin() -> None:
    hsts = HEADERS["StrictTransportSecurity"]
    assert hsts["AccessControlMaxAgeSec"] >= 31536000 and hsts["Override"] is True
    assert HEADERS["ContentTypeOptions"] == {"Override": True}
    assert HEADERS["FrameOptions"] == {"FrameOption": "DENY", "Override": True}
    assert HEADERS["ReferrerPolicy"]["ReferrerPolicy"] in ("no-referrer", "strict-origin-when-cross-origin", "same-origin")
    assert HEADERS["ReferrerPolicy"]["Override"] is True
    assert HEADERS["ContentSecurityPolicy"]["Override"] is True


def test_the_csp_refuses_everything_it_does_not_name() -> None:
    directives = csp_directives()
    assert directives["default-src"] == ["'none'"]
    assert directives["frame-ancestors"] == ["'none'"], "it has to agree with X-Frame-Options: DENY"
    assert directives["object-src"] == ["'none'"]
    assert directives["base-uri"] == ["'none'"]
    assert directives["form-action"] == ["'none'"]
    for name, sources in directives.items():
        assert "'unsafe-eval'" not in sources, name
        for source in sources:
            assert source not in ("*", "https:", "http:", "data:") or (name == "img-src" and source == "data:"), (
                f"{name} {source} would allow any host"
            )
            assert "*" not in source, f"{name} {source} is a wildcard"
    assert directives["connect-src"] == ["'self'", f"https://{EXAMPLE_API_DOMAIN}"]


def test_the_csp_fits_the_header_cloudfront_will_send() -> None:
    policy = HEADERS["ContentSecurityPolicy"]["ContentSecurityPolicy"]["Fn::Sub"]
    longest_domain = "a" * 10 + ".execute-api.ap-southeast-4.amazonaws.com"
    assert len(policy.replace("${ApiDomainName}", longest_domain)) <= 1783, "CloudFront's CSP value limit"


# --- the web ACL -------------------------------------------------------------------------------------


def _visibility_is_on(config: dict) -> bool:
    return config["SampledRequestsEnabled"] is True and config["CloudWatchMetricsEnabled"] is True and config["MetricName"]


def test_the_web_acl_is_for_cloudfront_allows_by_default_and_is_observable() -> None:
    assert WEB_ACL["Scope"] == "CLOUDFRONT"
    assert WEB_ACL["DefaultAction"] == {"Allow": {}}
    assert _visibility_is_on(WEB_ACL["VisibilityConfig"])
    for rule in WEB_ACL["Rules"]:
        assert _visibility_is_on(rule["VisibilityConfig"]), rule["Name"]
    metric_names = [WEB_ACL["VisibilityConfig"]["MetricName"]] + [r["VisibilityConfig"]["MetricName"] for r in WEB_ACL["Rules"]]
    assert len({repr(name) for name in metric_names}) == len(metric_names), "each rule needs its own metric"
    for name in metric_names:
        # Two edges in one account would otherwise add their counts into one metric.
        assert isinstance(name, dict) and name["Fn::Sub"].startswith("${AWS::StackName}-"), name
    priorities = [rule["Priority"] for rule in WEB_ACL["Rules"]]
    assert len(set(priorities)) == len(priorities)


def _managed(rule: dict) -> dict:
    return rule["Statement"]["ManagedRuleGroupStatement"]


def test_the_three_managed_groups_are_enforced_not_merely_counted() -> None:
    groups = {_managed(r)["Name"]: r for r in WEB_ACL["Rules"] if "ManagedRuleGroupStatement" in r["Statement"]}
    assert set(groups) == set(MANAGED_GROUP_WCU)
    for name, rule in groups.items():
        assert _managed(rule)["VendorName"] == "AWS"
        # OverrideAction Count would turn the whole group into a report.
        assert rule["OverrideAction"] == {"None": {}}, name


def test_the_rate_limit_answers_with_a_problem_document() -> None:
    """RFC 7807, like every other error this service returns, with the fields a page reads."""
    response = ACL_RULES["RateLimitPerIp"]["Action"]["Block"]["CustomResponse"]
    body = WEB_ACL["CustomResponseBodies"][response["CustomResponseBodyKey"]]
    assert re.fullmatch(r"[\w\-]+", response["CustomResponseBodyKey"]), "WAF's pattern for a body key"
    assert body["ContentType"] == "APPLICATION_JSON"
    problem = json.loads(body["Content"])
    assert problem["status"] == response["ResponseCode"] == 429
    assert problem["title"] == "Too Many Requests"
    assert problem["type"].startswith("urn:threefold:error:")
    assert problem["detail"] and problem["error"] == problem["detail"], "the function's problems carry both"
    assert len(body["Content"].encode("utf-8")) <= 4096
    headers = {h["Name"].lower(): h["Value"] for h in response["ResponseHeaders"]}
    assert headers["retry-after"] == "300", "one evaluation window"
    assert "content-type" not in headers, "WAF refuses it; ContentType sets it"


def test_the_rate_limit_is_per_ip_parameterised_and_answers_429() -> None:
    rule = ACL_RULES["RateLimitPerIp"]
    statement = rule["Statement"]["RateBasedStatement"]
    assert statement["Limit"] == {"Ref": "RateLimitPerFiveMinutes"}
    assert statement["AggregateKeyType"] == "IP"
    assert statement["EvaluationWindowSec"] == 300
    assert PARAMETERS["RateLimitPerFiveMinutes"]["Default"] == 1000
    assert PARAMETERS["RateLimitPerFiveMinutes"]["Type"] == "Number"
    # The hook reads a 4xx as a refusal of the call and a 429 as a call it could
    # not have judged, so a limit must not answer 403.
    assert rule["Action"]["Block"]["CustomResponse"]["ResponseCode"] == 429


def _counted(group: str) -> set:
    rule = next(r for r in WEB_ACL["Rules"] if "ManagedRuleGroupStatement" in r["Statement"] and _managed(r)["Name"] == group)
    overrides = _managed(rule).get("RuleActionOverrides", [])
    assert all(o["ActionToUse"] == {"Count": {}} for o in overrides), "an override may only relax a rule to count"
    return {o["Name"] for o in overrides}


def test_a_large_or_code_bearing_body_is_counted_not_blocked() -> None:
    """A hook's Write of a 20 KB HTML page, or a Java class naming java.lang.Runtime, must reach the gate."""
    assert _counted("AWSManagedRulesCommonRuleSet") == COMMON_BODY_RULES | URI_RULES_THAT_COUNT
    assert "SizeRestrictions_BODY" in COMMON_BODY_RULES
    assert _counted("AWSManagedRulesKnownBadInputsRuleSet") == KNOWN_BAD_INPUTS_BODY_RULES


def test_only_body_rules_and_the_named_extension_rule_are_relaxed() -> None:
    """Rules on headers, the query string and the rest of the URI keep blocking."""
    for group in ("AWSManagedRulesCommonRuleSet", "AWSManagedRulesKnownBadInputsRuleSet"):
        for name in _counted(group) - URI_RULES_THAT_COUNT:
            assert name.endswith("_BODY"), f"{name} is not a body rule and must keep blocking"
    assert URI_RULES_THAT_COUNT <= _counted("AWSManagedRulesCommonRuleSet")
    assert not _managed(ACL_RULES["AmazonIpReputationList"]).get("RuleActionOverrides")


def test_the_install_path_is_not_refused_for_its_file_extension() -> None:
    """/install.py, the hook and the bundle are served on purpose; see the comment in edge.yml."""
    assert "RestrictedExtensions_URIPATH" in _counted("AWSManagedRulesCommonRuleSet")
    served = {READER.with_defaults(b["PathPattern"], TEMPLATE) for b in API_BEHAVIORS}
    assert {"/install.py", "/claude_code_hook.py", "/hooks/*", "/dist/*"} <= served


def test_the_web_acl_stays_inside_the_capacity_billed_at_the_base_price() -> None:
    managed = sum(MANAGED_GROUP_WCU[_managed(r)["Name"]] for r in WEB_ACL["Rules"] if "ManagedRuleGroupStatement" in r["Statement"])
    rate_based = 2 * sum(1 for r in WEB_ACL["Rules"] if "RateBasedStatement" in r["Statement"])
    assert managed + rate_based <= 1500


# --- what the stack tells its operator -------------------------------------------------------------


@pytest.mark.parametrize(
    "name, value",
    [
        ("DistributionDomainName", {"Fn::GetAtt": ["Distribution", "DomainName"]}),
        ("DistributionId", {"Ref": "Distribution"}),
        ("WebBucketName", {"Ref": "WebBucket"}),
        ("WebAclArn", {"Fn::GetAtt": ["WebAcl", "Arn"]}),
    ],
)
def test_the_outputs_publish_web_and_the_owner_read(name: str, value: dict) -> None:
    assert TEMPLATE["Outputs"][name]["Value"] == value


def test_the_site_url_output_carries_the_trailing_slash() -> None:
    assert TEMPLATE["Outputs"]["SiteUrl"]["Value"] == {"Fn::Sub": "https://${Distribution.DomainName}/"}


# --- the edge tells the function who is calling ------------------------------------------------
#
# The function believes the viewer's address and host name only on a request
# that carries the edge's secret (security_middleware.edge_request_is_trusted),
# so these check the three halves the edge owns: the secret goes to both API
# origins, the viewer's address and host reach the origin, and the names agree
# with the ones the code reads.

# From the CloudFront developer guide, "Add custom headers to origin requests":
# the headers CloudFront refuses to add as origin custom headers.
CUSTOM_ORIGIN_HEADER_DENYLIST = {
    "cache-control", "connection", "content-length", "cookie", "host", "if-match", "if-modified-since",
    "if-none-match", "if-range", "if-unmodified-since", "max-forwards", "pragma", "proxy-authenticate",
    "proxy-authorization", "proxy-connection", "range", "request-range", "te", "trailer", "transfer-encoding",
    "upgrade", "via", "x-real-ip",
}
CUSTOM_ORIGIN_HEADER_DENIED_PREFIXES = ("x-amz-", "x-edge-")
# From "Restrictions on all edge functions": headers a function may not add, and
# the ones it may read but not change in a viewer request.
FUNCTION_DISALLOWED_HEADERS = {
    "connection", "expect", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
    "trailer", "upgrade", "x-accel-buffering", "x-accel-charset", "x-accel-limit-rate", "x-accel-redirect",
    "x-amzn-auth", "x-amzn-cf-billing", "x-amzn-cf-id", "x-amzn-cf-xff", "x-amzn-errortype",
    "x-amzn-fle-profile", "x-amzn-header-count", "x-amzn-header-order", "x-amzn-lambda-integration-tag",
    "x-amzn-requestid", "x-cache", "x-forwarded-proto", "x-real-ip", "cloudfront-viewer-cert-pem",
    "client-cert", "client-cert-chain",
}
FUNCTION_DISALLOWED_PREFIXES = ("x-amz-cf-", "x-edge-")
FUNCTION_READ_ONLY_IN_VIEWER_REQUEST = {"cdn-loop", "content-length", "host", "transfer-encoding", "via"}
FUNCTION = RESOURCES["ViewerHostFunction"]
FUNCTION_ARN = {"Fn::GetAtt": ["ViewerHostFunction", "FunctionMetadata.FunctionARN"]}


def _middleware():
    from threefold.infrastructure import security_middleware

    return security_middleware


def test_the_edge_secret_is_a_long_hidden_parameter_with_no_default() -> None:
    secret = PARAMETERS["EdgeOriginSecret"]
    assert secret["Type"] == "String"
    assert secret["NoEcho"] is True, "describe-stacks would print it"
    assert "Default" not in secret, "an edge must never deploy without a secret"
    assert secret["MinLength"] >= _middleware().MIN_EDGE_SECRET_LENGTH == 32
    assert secret["MaxLength"] <= 1783, "CloudFront's limit on an origin custom header value"


@pytest.mark.parametrize(
    "value, accepted",
    [
        ("acmeSyntheticEdgeSecret_0123456789-abcdefghijklmnopqrstuvwxyzAB", True),
        ("a" * 32, True),
        ("a" * 31, False),
        ("acme synthetic edge secret with spaces 0123456789", False),
        ("acme-synthetic-edge-secret-0123456789-abcdef\n", False),
        ("acme-synthetic-edge-secret-0123456789-abcdef;", False),
    ],
)
def test_the_edge_secret_is_url_safe_and_long_enough(value: str, accepted: bool) -> None:
    """The alphabet secrets.token_urlsafe writes, and the length the function insists on."""
    secret = PARAMETERS["EdgeOriginSecret"]
    fits = bool(re.fullmatch(secret["AllowedPattern"], value)) and secret["MinLength"] <= len(value) <= secret["MaxLength"]
    assert fits is accepted


def test_both_api_origins_send_the_secret_and_the_bucket_does_not() -> None:
    """A request through /prod/* goes to api-stage; without the header there it would be counted as the edge."""
    name = _middleware().EDGE_SECRET_HEADER
    for origin_id in ("api", "api-stage"):
        headers = ORIGINS[origin_id]["OriginCustomHeaders"]
        assert headers == [{"HeaderName": name, "HeaderValue": {"Ref": "EdgeOriginSecret"}}], origin_id
    assert "OriginCustomHeaders" not in ORIGINS["web"], "S3 reads no header of ours"


def test_the_secret_header_is_one_cloudfront_will_add() -> None:
    name = _middleware().EDGE_SECRET_HEADER.lower()
    assert name not in CUSTOM_ORIGIN_HEADER_DENYLIST
    assert not name.startswith(CUSTOM_ORIGIN_HEADER_DENIED_PREFIXES)
    assert re.fullmatch(r"[A-Za-z0-9-]+", name), "a plain header token"


def test_the_viewer_host_function_is_published_and_runs_the_current_runtime() -> None:
    assert FUNCTION["Type"] == "AWS::CloudFront::Function"
    properties = FUNCTION["Properties"]
    assert properties["AutoPublish"] is True, "only a LIVE function can be associated"
    assert properties["FunctionConfig"]["Runtime"] == "cloudfront-js-2.0"
    assert properties["FunctionConfig"]["Comment"]
    assert properties["Name"] == {"Fn::Sub": "${AWS::StackName}-viewer-host"}, "a second edge must not collide"
    assert isinstance(properties["FunctionCode"], str), "plain code, never a !Sub that could eat ${...}"


def test_the_function_copies_host_into_the_header_the_code_reads() -> None:
    code = FUNCTION["Properties"]["FunctionCode"]
    header = _middleware().VIEWER_HOST_HEADER.lower()
    assert f"request.headers['{header}'] = {{ value: host.value }}" in code
    assert "request.headers.host" in code
    assert f"delete request.headers['{header}']" in code, "a viewer's own value is removed when there is no Host"
    assert "${" not in code


def test_the_function_adds_a_header_edge_functions_may_add() -> None:
    header = _middleware().VIEWER_HOST_HEADER.lower()
    assert header not in FUNCTION_DISALLOWED_HEADERS
    assert not header.startswith(FUNCTION_DISALLOWED_PREFIXES)
    assert header not in FUNCTION_READ_ONLY_IN_VIEWER_REQUEST
    assert not header.startswith("cloudfront-"), "CloudFront's own header names are its to set"


def test_every_api_behavior_runs_the_function_on_the_viewer_request() -> None:
    for behavior in API_BEHAVIORS:
        assert behavior["FunctionAssociations"] == [{"EventType": "viewer-request", "FunctionARN": FUNCTION_ARN}], (
            repr(behavior["PathPattern"])
        )


def test_the_bucket_behaviors_do_not_run_the_function() -> None:
    """S3 reads no header of ours, and every run is billed."""
    for behavior in [DISTRIBUTION["DefaultCacheBehavior"], *[b for b in BEHAVIORS if b["TargetOriginId"] == "web"]]:
        assert "FunctionAssociations" not in behavior


def test_the_viewer_address_and_host_reach_the_origin() -> None:
    """AllViewerExceptHostHeader forwards both, by its documentation.

    "Use managed origin request policies": the policy includes every viewer
    header except Host, and "all device type and viewer location headers";
    "Add CloudFront request headers" lists CloudFront-Viewer-Address among the
    viewer location headers. The function's header is part of the viewer
    request by the time the policy applies. A policy that forwarded Host
    instead would be refused by API Gateway, so no other managed policy fits.
    """
    for behavior in API_BEHAVIORS:
        assert behavior["OriginRequestPolicyId"] == ALL_VIEWER_EXCEPT_HOST_HEADER, repr(behavior["PathPattern"])
    assert _middleware().VIEWER_ADDRESS_HEADER == "CloudFront-Viewer-Address"
    assert not [r for r in RESOURCES.values() if r["Type"] == "AWS::CloudFront::OriginRequestPolicy"], (
        "a custom policy would have to be shown to forward both; the managed one is documented to"
    )


def _run_function(event: dict) -> dict:
    """The function's code run by node, the closest runtime this machine has to CloudFront's."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on this machine; the function's text is still checked above")
    script = (
        FUNCTION["Properties"]["FunctionCode"]
        + "\nconst out = handler(JSON.parse(process.argv[1]));\nprocess.stdout.write(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        [node, "-e", script, json.dumps(event)], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


def _viewer_request(headers: dict) -> dict:
    request = {"method": "GET", "uri": "/install.py", "querystring": {}, "cookies": {}, "headers": headers}
    return {"version": "1.0", "context": {"eventType": "viewer-request"}, "request": request}


@pytest.mark.parametrize(
    "headers, expected",
    [
        ({"host": {"value": "d1acme0edge.cloudfront.net"}}, "d1acme0edge.cloudfront.net"),
        (
            {"host": {"value": "d1acme0edge.cloudfront.net"}, "x-threefold-viewer-host": {"value": "acme.example"}},
            "d1acme0edge.cloudfront.net",
        ),
        ({"x-threefold-viewer-host": {"value": "acme.example"}}, None),
        ({}, None),
    ],
)
def test_the_function_sets_the_viewer_host_and_nothing_a_viewer_sent(headers: dict, expected) -> None:
    request = _run_function(_viewer_request(headers))
    value = request["headers"].get("x-threefold-viewer-host")
    assert (value["value"] if value else None) == expected
    assert request["uri"] == "/install.py" and request["method"] == "GET", "the request is otherwise untouched"
    if "host" in headers:
        assert request["headers"]["host"] == headers["host"], "Host is read-only in a viewer request"
