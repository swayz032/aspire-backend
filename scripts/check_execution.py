"""Check Step Functions execution status."""
import boto3
import json
import sys

sfn = boto3.client("stepfunctions", region_name="us-east-1")

# Check all recent executions
execs = sfn.list_executions(
    stateMachineArn="arn:aws:states:us-east-1:843479649294:stateMachine:aspire-secret-rotation-dev",
    maxResults=5,
)

for e in execs["executions"]:
    arn = e["executionArn"]
    name = e["name"]
    status = e["status"]
    started = e["startDate"].isoformat()
    stopped = e.get("stopDate", "running").isoformat() if e.get("stopDate") else "running"
    print(f"{name[:30]:30s}  {status:10s}  started={started[:19]}  stopped={stopped[:19]}")

    if status == "SUCCEEDED":
        detail = sfn.describe_execution(executionArn=arn)
        output = json.loads(detail.get("output", "{}"))
        receipt = output.get("receipt", {})
        print(f"  Receipt: {receipt.get('receipt_id', '?')}")
        print(f"  Correlation: {output.get('correlation_id', '?')}")

    elif status == "FAILED":
        detail = sfn.describe_execution(executionArn=arn)
        print(f"  Error: {detail.get('error', '?')}")
        print(f"  Cause: {str(detail.get('cause', '?'))[:200]}")
