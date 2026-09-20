# Live deployment evidence, 2026-09-20

Every line below was produced by the command shown, against AWS account 308857099262 in eu-west-1.

## Stack
```
$ aws cloudformation describe-stacks --stack-name threefold-prod --region eu-west-1
{
    "StackId": "arn:aws:cloudformation:eu-west-1:308857099262:stack/threefold-prod/564220b0-b4e9-11f1-8793-02eeddc1500f",
    "Status": "UPDATE_COMPLETE",
    "Created": "2026-09-20T11:49:29.307000+00:00",
    "Updated": "2026-09-20T11:59:22.611000+00:00",
    "Outputs": [
        {
            "OutputKey": "LambdaFunctionArn",
            "OutputValue": "arn:aws:lambda:eu-west-1:308857099262:function:threefold-prod-ThreefoldFunction-CbdNiW0oTSmT",
            "Description": "Threefold Lambda Function ARN"
        },
        {
            "OutputKey": "ApiEndpoint",
            "OutputValue": "https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod",
            "Description": "HTTP API Gateway endpoint URL for Threefold"
        }
    ]
}
```

## Anonymous request, no credentials, no API key
```
$ curl -s https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/status
{"active_rules": ["SECRET_LEAKAGE_FREE", "ARCHITECTURAL_BOUNDARY_SAFE", "LOOP_THRASHING_FREE", "BUDGET_CIRCUIT_BREAKER_SAFE"], "service": "Threefold", "stage": "prod", "status": "HEALTHY", "target_track": "#workplace-efficiency", "version": "1.0.0"}
```

## Bedrock invoked by the function role, not by a workstation user
```
$ aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventName,AttributeValue=Converse --region eu-west-1
{
  "eventTime": "2026-09-20T12:00:39Z",
  "principalType": "AssumedRole",
  "arn": "arn:aws:sts::308857099262:assumed-role/threefold-prod-ThreefoldFunctionRole-JzGN3b6RjC7w/threefold-prod-ThreefoldFunction-CbdNiW0oTSmT",
  "modelId": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
  "errorCode": "none"
}
{
  "eventTime": "2026-09-20T12:00:37Z",
  "principalType": "AssumedRole",
  "arn": "arn:aws:sts::308857099262:assumed-role/threefold-prod-ThreefoldFunctionRole-JzGN3b6RjC7w/threefold-prod-ThreefoldFunction-CbdNiW0oTSmT",
  "modelId": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
  "errorCode": "none"
}
{
  "eventTime": "2026-09-20T12:00:35Z",
  "principalType": "AssumedRole",
  "arn": "arn:aws:sts::308857099262:assumed-role/threefold-prod-ThreefoldFunctionRole-JzGN3b6RjC7w/threefold-prod-ThreefoldFunction-CbdNiW0oTSmT",
  "modelId": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
  "errorCode": "none"
}
```
