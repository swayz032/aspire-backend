"""Debug API Gateway integration by using test-invoke-method.

This shows exactly what API Gateway sends to Step Functions.
"""
import boto3
import json

API_ID = "z48u43vald"
REGION = "us-east-1"

apigw = boto3.client("apigateway", region_name=REGION)

# Find /rotate resource
resources = apigw.get_resources(restApiId=API_ID)
rotate_rid = None
for r in resources["items"]:
    if r["path"] == "/rotate":
        rotate_rid = r["id"]
        break

print(f"Resource ID: {rotate_rid}")

# Check the stored template one more time
integration = apigw.get_integration(
    restApiId=API_ID, resourceId=rotate_rid, httpMethod="POST"
)
template = integration["requestTemplates"]["application/json"]
print(f"Stored template: {repr(template)}")
print(f"Has $util: {'$util' in template}")
print(f"Has $input: {'$input' in template}")

# Test invoke to see what gets sent
body = json.dumps({
    "secret_id": "aspire/dev/internal",
    "adapter": "internal",
    "correlation_id": "debug-test-003",
    "triggered_by": "debug-test",
})

result = apigw.test_invoke_method(
    restApiId=API_ID,
    resourceId=rotate_rid,
    httpMethod="POST",
    body=body,
    headers={"Content-Type": "application/json"},
)

print(f"\nTest invoke status: {result['status']}")
print(f"Response body: {result.get('body', '')[:500]}")
print(f"Latency: {result.get('latency')}ms")

# The log shows exactly what was sent to Step Functions
log = result.get("log", "")
if log:
    print(f"\nExecution log:")
    for line in log.split("\n"):
        if line.strip():
            print(f"  {line[:200]}")
