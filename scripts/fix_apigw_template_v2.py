"""Fix API Gateway VTL mapping template - v2.

Use $input.json('$') instead of $input.body for Step Functions input.
Also add proper integration response mapping.
"""
import boto3
import json
import urllib.request

API_ID = "z48u43vald"
SM_ARN = "arn:aws:states:us-east-1:843479649294:stateMachine:aspire-secret-rotation-dev"
ROLE_ARN = "arn:aws:iam::843479649294:role/aspire-rotation-execution-dev"
REGION = "us-east-1"

apigw = boto3.client("apigateway", region_name=REGION)

# Find /rotate resource
resources = apigw.get_resources(restApiId=API_ID)
rotate_rid = None
for r in resources["items"]:
    if r["path"] == "/rotate":
        rotate_rid = r["id"]
        break

assert rotate_rid, "/rotate not found"
print(f"Resource: {rotate_rid}")

# Check current template
current = apigw.get_integration(restApiId=API_ID, resourceId=rotate_rid, httpMethod="POST")
print(f"Current: {repr(current['requestTemplates']['application/json'])}")

# Use VTL $input.json('$') which returns proper JSON string
# The triple-dollar trick: VTL needs $ to be escaped in some contexts
# Method: direct string building to avoid any shell issues
template_parts = [
    '{"stateMachineArn": "',
    SM_ARN,
    '", "input": "$util.escapeJavaScript($input.json(\'$\'))"}'
]
template = "".join(template_parts)
print(f"New template: {repr(template)}")

# Update
apigw.put_integration(
    restApiId=API_ID,
    resourceId=rotate_rid,
    httpMethod="POST",
    type="AWS",
    integrationHttpMethod="POST",
    uri="arn:aws:apigateway:us-east-1:states:action/StartExecution",
    credentials=ROLE_ARN,
    requestTemplates={"application/json": template},
    passthroughBehavior="WHEN_NO_MATCH",
)

# Verify
check = apigw.get_integration(restApiId=API_ID, resourceId=rotate_rid, httpMethod="POST")
actual = check["requestTemplates"]["application/json"]
print(f"Stored:  {repr(actual)}")
assert "$util" in actual, f"Template still broken: {repr(actual)}"

# Make sure we have proper method response + integration response
try:
    apigw.put_method_response(
        restApiId=API_ID, resourceId=rotate_rid, httpMethod="POST",
        statusCode="200", responseModels={"application/json": "Empty"},
    )
except Exception:
    pass

try:
    apigw.put_integration_response(
        restApiId=API_ID, resourceId=rotate_rid, httpMethod="POST",
        statusCode="200", responseTemplates={"application/json": ""},
    )
except Exception:
    pass

# Redeploy
apigw.create_deployment(restApiId=API_ID, stageName="dev")
print("Redeployed")

# Quick test
print("\nTesting...")
body = json.dumps({
    "secret_id": "aspire/dev/internal",
    "adapter": "internal",
    "correlation_id": "apigw-fix-test-002",
    "triggered_by": "template-fix-test",
}).encode()

req = urllib.request.Request(
    f"https://{API_ID}.execute-api.{REGION}.amazonaws.com/dev/rotate",
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
        if "executionArn" in result:
            print(f"SUCCESS: {result['executionArn']}")
        elif "InvalidExecutionInput" in str(result):
            print(f"STILL BROKEN: {json.dumps(result)[:300]}")
        else:
            print(f"Response: {json.dumps(result)[:300]}")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()[:300]}")
except Exception as e:
    print(f"Error: {e}")
