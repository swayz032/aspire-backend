"""Check latest Step Functions execution results."""
import boto3
import json
import sys

creds = {}
with open("/mnt/c/Users/tonio/Projects/myapp/infrastructure/docker/.env", "r") as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip()

sfn = boto3.client(
    "stepfunctions",
    region_name="us-east-1",
    aws_access_key_id=creds["AWS_ROTATION_TRIGGER_ACCESS_KEY_ID"],
    aws_secret_access_key=creds["AWS_ROTATION_TRIGGER_SECRET_ACCESS_KEY"],
)

SM_ARN = "arn:aws:states:us-east-1:843479649294:stateMachine:aspire-secret-rotation-dev"

execs = sfn.list_executions(stateMachineArn=SM_ARN, maxResults=12)

print("Latest executions:")
for e in execs["executions"]:
    name = e["name"][:12]
    status = e["status"]
    started = e["startDate"].strftime("%H:%M:%S")
    print("  {} {} {}".format(name, status, started))

# Check each of the latest 5 (from this test round)
print("\nDetailed check for 5 most recent:")
for e in execs["executions"][:5]:
    arn = e["executionArn"]
    name = e["name"][:12]
    status = e["status"]

    detail = sfn.describe_execution(executionArn=arn)
    inp = json.loads(detail.get("input", "{}"))
    adapter = inp.get("adapter", "?")
    secret_id = inp.get("secret_id", "?")

    print("\n--- {} ({}) ---".format(adapter, status))
    print("  secret_id: {}".format(secret_id))

    if status == "SUCCEEDED":
        output = json.loads(detail.get("output", "{}"))
        receipt = output.get("receipt", {})
        timing = output.get("timing", {})
        print("  receipt_id: {}".format(receipt.get("receipt_id", "?")[:20]))
        print("  outcome: {}".format(receipt.get("outcome", "?")))
        print("  adapter: {}".format(receipt.get("adapter", "?")))
        print("  duration: {}s".format(timing.get("total_seconds", "?")))
    elif status == "FAILED":
        history = sfn.get_execution_history(executionArn=arn, maxResults=50, reverseOrder=True)
        for event in history["events"]:
            if event["type"] == "LambdaFunctionFailed":
                cause_raw = event["lambdaFunctionFailedEventDetails"].get("cause", "{}")
                try:
                    cause = json.loads(cause_raw)
                    print("  error_type: {}".format(cause.get("errorType", "?")))
                    print("  error_msg: {}".format(cause.get("errorMessage", "?")[:300]))
                except:
                    print("  raw_cause: {}".format(cause_raw[:300]))
                break
    elif status == "RUNNING":
        print("  (still running)")
