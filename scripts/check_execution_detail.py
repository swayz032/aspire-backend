"""Check detailed output of most recent successful execution."""
import boto3
import json

sfn = boto3.client("stepfunctions", region_name="us-east-1")

exec_arn = "arn:aws:states:us-east-1:843479649294:execution:aspire-secret-rotation-dev:35e1368d-9dca-4a69-b167-4011708c8128"

detail = sfn.describe_execution(executionArn=exec_arn)
print(f"Status: {detail['status']}")
print(f"Input: {detail.get('input', '?')[:300]}")

output = json.loads(detail.get("output", "{}"))
print(f"\nFull output keys: {list(output.keys())}")

# Check receipt
receipt = output.get("receipt", {})
if receipt:
    print(f"\nReceipt ID: {receipt.get('receipt_id', '?')}")
    print(f"Outcome: {receipt.get('outcome', '?')}")
    print(f"Adapter: {receipt.get('adapter', '?')}")

# Check key details
create = output.get("create_result", {})
if create:
    print(f"\nCreate result keys: {list(create.keys())}")
    print(f"  Key ID: {create.get('key_id', '?')}")
    print(f"  Success: {create.get('success', '?')}")

test = output.get("test_result", {})
if test:
    print(f"\nTest result: {test.get('test_name', '?')} - success={test.get('success', '?')}")

revoke = output.get("revoke_result", {})
if revoke:
    print(f"\nRevoke result: success={revoke.get('success', '?')}, key={revoke.get('revoked_key_id', '?')}")

# Get the SM execution history to see all steps
print("\n--- Execution Timeline ---")
history = sfn.get_execution_history(executionArn=exec_arn, maxResults=50)
for event in history["events"]:
    etype = event["type"]
    ts = event["timestamp"].strftime("%H:%M:%S")
    if "Entered" in etype or "Exited" in etype:
        state = event.get("stateEnteredEventDetails", event.get("stateExitedEventDetails", {}))
        name = state.get("name", "?")
        print(f"  {ts} {etype:40s} {name}")
