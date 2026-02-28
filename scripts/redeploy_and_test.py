"""Force redeploy API Gateway and test external URL."""
import boto3
import json
import time
import urllib.request
import urllib.error

API_ID = "z48u43vald"
REGION = "us-east-1"

apigw = boto3.client("apigateway", region_name=REGION)

# Force fresh deployment
resp = apigw.create_deployment(
    restApiId=API_ID, stageName="dev", description="force-vtl-fix"
)
deploy_id = resp["ResponseMetadata"]["RequestId"]
print(f"Deployment triggered: {deploy_id}")

# Wait for propagation
print("Waiting 15s for deployment propagation...")
time.sleep(15)

# Test external URL
url = f"https://{API_ID}.execute-api.{REGION}.amazonaws.com/dev/rotate"
body = json.dumps({
    "secret_id": "aspire/dev/internal",
    "adapter": "internal",
    "correlation_id": "ext-test-004",
    "triggered_by": "external-url-test",
}).encode()

req = urllib.request.Request(
    url, data=body, headers={"Content-Type": "application/json"}, method="POST"
)

try:
    with urllib.request.urlopen(req, timeout=15) as r:
        result = json.loads(r.read())
        if "executionArn" in result:
            print(f"SUCCESS: {result['executionArn']}")
        else:
            print(f"Response: {json.dumps(result)[:400]}")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()[:300]}")
except Exception as e:
    print(f"Error: {e}")
