"""Test API Gateway → Step Functions rotation pipeline.

Sends a real rotation request through the API Gateway to verify:
1. VTL mapping template correctly passes JSON to Step Functions
2. Step Functions starts execution successfully
3. Lambda rotation handler processes the request
"""
import json
import time
import urllib.request
import urllib.error
import boto3

API_URL = "https://z48u43vald.execute-api.us-east-1.amazonaws.com/dev/rotate"
SM_ARN = "arn:aws:states:us-east-1:843479649294:stateMachine:aspire-secret-rotation-dev"

print("=" * 60)
print("Test: API Gateway -> Step Functions -> Lambda rotation")
print("=" * 60)

# Build request body (same format as n8n orchestrator sends)
body = json.dumps({
    "secret_id": "aspire/dev/internal",
    "adapter": "internal",
    "correlation_id": "apigw-e2e-test-001",
    "triggered_by": "test-apigw-direct",
    "idempotency_key": "apigw-e2e-test-001",
    "rotation_interval_days": 90,
}).encode()

print(f"URL: {API_URL}")
print(f"Body: {body.decode()[:200]}")

# Send request
req = urllib.request.Request(
    API_URL,
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
        print(f"\nHTTP {resp.status}")
        print(f"Response: {json.dumps(result, indent=2)[:500]}")

        # Extract execution ARN
        exec_arn = result.get("executionArn", "")
        if exec_arn:
            print(f"\nExecution ARN: {exec_arn}")
            print("Step Functions execution started successfully!")

            # Poll for completion
            sfn = boto3.client("stepfunctions", region_name="us-east-1")
            print("\nPolling execution status...")
            for i in range(60):
                status = sfn.describe_execution(executionArn=exec_arn)
                state = status["status"]
                print(f"  [{i*5}s] Status: {state}")
                if state in ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"):
                    if state == "SUCCEEDED":
                        output = json.loads(status.get("output", "{}"))
                        print(f"\nSUCCESS! Output:")
                        print(json.dumps(output, indent=2)[:1000])
                    else:
                        print(f"\n{state}!")
                        cause = status.get("cause", "")
                        error = status.get("error", "")
                        if cause:
                            print(f"Cause: {cause[:500]}")
                        if error:
                            print(f"Error: {error}")
                        # Get execution history for the failure point
                        history = sfn.get_execution_history(
                            executionArn=exec_arn,
                            reverseOrder=True,
                            maxResults=5,
                        )
                        for event in history["events"]:
                            etype = event["type"]
                            if "Failed" in etype or "Error" in etype:
                                details = event.get("executionFailedEventDetails",
                                         event.get("taskFailedEventDetails",
                                         event.get("lambdaFunctionFailedEventDetails", {})))
                                if details:
                                    print(f"  Event: {etype}")
                                    print(f"    Error: {details.get('error', '?')}")
                                    print(f"    Cause: {str(details.get('cause', '?'))[:300]}")
                    break
                time.sleep(5)
            else:
                print("Timeout waiting for execution (300s)")
        else:
            print("WARNING: No executionArn in response")
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", errors="replace")
    print(f"\nHTTP {e.code}")
    print(f"Error: {body[:500]}")
except Exception as e:
    print(f"\nException: {e}")
