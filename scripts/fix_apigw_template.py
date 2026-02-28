"""Fix API Gateway VTL mapping template for rotation endpoint.

The original deployment lost $ signs due to shell variable expansion.
This restores proper VTL: $util.escapeJavaScript($input.body)
"""
import boto3

API_ID = "z48u43vald"
SM_ARN = "arn:aws:states:us-east-1:843479649294:stateMachine:aspire-secret-rotation-dev"
REGION = "us-east-1"

apigw = boto3.client("apigateway", region_name=REGION)

# Find /rotate resource
resources = apigw.get_resources(restApiId=API_ID)
rotate_rid = None
for r in resources["items"]:
    if r["path"] == "/rotate":
        rotate_rid = r["id"]
        break

assert rotate_rid, "/rotate resource not found"
print(f"Found /rotate resource: {rotate_rid}")

# Show current (broken) template
current = apigw.get_integration(restApiId=API_ID, resourceId=rotate_rid, httpMethod="POST")
print(f"Current template: {repr(current['requestTemplates']['application/json'])}")

# Correct VTL template
template = '{"stateMachineArn": "' + SM_ARN + '", "input": "$util.escapeJavaScript($input.body)"}'
print(f"New template: {repr(template)}")

# Update integration
apigw.put_integration(
    restApiId=API_ID,
    resourceId=rotate_rid,
    httpMethod="POST",
    type="AWS",
    integrationHttpMethod="POST",
    uri="arn:aws:apigateway:us-east-1:states:action/StartExecution",
    credentials="arn:aws:iam::843479649294:role/aspire-rotation-execution-dev",
    requestTemplates={"application/json": template},
    passthroughBehavior="WHEN_NO_MATCH",
)
print("Integration updated")

# Also need to set method response + integration response for 200
try:
    apigw.put_method_response(
        restApiId=API_ID,
        resourceId=rotate_rid,
        httpMethod="POST",
        statusCode="200",
        responseModels={"application/json": "Empty"},
    )
except Exception:
    pass  # May already exist

try:
    apigw.put_integration_response(
        restApiId=API_ID,
        resourceId=rotate_rid,
        httpMethod="POST",
        statusCode="200",
        responseTemplates={"application/json": ""},
    )
except Exception:
    pass  # May already exist

# Verify
check = apigw.get_integration(restApiId=API_ID, resourceId=rotate_rid, httpMethod="POST")
actual = check["requestTemplates"]["application/json"]
assert "$util" in actual and "$input" in actual, f"VTL still broken: {repr(actual)}"
print(f"Verified template: {repr(actual)}")

# Redeploy
apigw.create_deployment(restApiId=API_ID, stageName="dev")
print("Redeployed to dev stage")
print("DONE - API Gateway template fixed")
