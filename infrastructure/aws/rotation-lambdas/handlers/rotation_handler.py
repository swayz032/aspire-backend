"""Rotation handler Lambda — called by Step Functions for each rotation step.

This is the execution plane. It:
  1. Receives a step + context from Step Functions
  2. Dispatches to the appropriate vendor adapter
  3. Manages SM version staging (AWSPENDING → AWSCURRENT)
  4. Emits receipts for every rotation event (Law #2)

It NEVER decides when to rotate — that's n8n's job (scheduling) and
Step Functions' job (state machine orchestration).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

sm_client = boto3.client("secretsmanager", region_name="us-east-1")
sns_client = boto3.client("sns", region_name="us-east-1")

SNS_EVENTS_TOPIC = os.environ.get("SNS_EVENTS_TOPIC", "")
SNS_FAILURE_TOPIC = os.environ.get("SNS_FAILURE_TOPIC", "")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "prod")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Main Lambda entry point — dispatches based on 'step' field."""
    step = event.get("step", "")
    raw_secret_id = event.get("secret_id", "")
    adapter_name = event.get("adapter", "")
    correlation_id = event.get("correlation_id", f"rotation-{uuid.uuid4().hex[:12]}")

    # Resolve full SM path: n8n sends "stripe", SM expects "aspire/dev/stripe"
    if "/" not in raw_secret_id:
        secret_id = f"aspire/{ENVIRONMENT}/{'providers' if raw_secret_id == 'providers' else raw_secret_id}"
    else:
        secret_id = raw_secret_id
    event["secret_id"] = secret_id

    logger.info(
        "Rotation step=%s adapter=%s secret=%s correlation=%s",
        step, adapter_name, secret_id, correlation_id,
    )

    # Lazy import adapters to avoid loading all vendor SDKs
    from adapters import get_adapter

    try:
        adapter = get_adapter(adapter_name)
    except ValueError as e:
        logger.error("Unknown adapter: %s", adapter_name)
        raise ValueError(f"UNKNOWN_ADAPTER: {e}") from e

    handlers = {
        "create_key": _handle_create_key,
        "write_pending": _handle_write_pending,
        "test_key": _handle_test_key,
        "promote": _handle_promote,
        "verify_cutover": _handle_verify_cutover,
        "revoke_old": _handle_revoke_old,
        "rollback_pending": _handle_rollback_pending,
        "emit_receipt": _handle_emit_receipt,
    }

    handler = handlers.get(step)
    if not handler:
        logger.error("Unknown step: %s", step)
        raise ValueError(f"UNKNOWN_STEP: {step}")

    return handler(event, adapter, correlation_id)


def _handle_create_key(
    event: dict[str, Any], adapter: Any, correlation_id: str
) -> dict[str, Any]:
    """Step 1: Create a new key via vendor API."""
    secret_id = event["secret_id"]

    # Fetch current secret value
    current = _get_current_secret(secret_id)
    if current is None:
        logger.error("Failed to fetch AWSCURRENT for %s", secret_id)
        raise RuntimeError(f"SM_FETCH_FAILED: Cannot fetch current secret for {secret_id}")

    # Store the current key ID before rotation
    old_key_id = current.get("_key_id", current.get("key_id", ""))

    result = adapter.create_key(current)

    if not result.success:
        logger.error("create_key failed for %s: %s", adapter.provider_name, result.error)
        raise Exception(f"CreateKey failed: {result.error}")

    return {
        "success": True,
        "new_key_data": {
            "key_id": result.key_id,
            "key_value": result.key_value,
            "metadata": result.metadata,
        },
        "old_key_id": old_key_id,
        "adapter": adapter.provider_name,
    }


def _handle_write_pending(
    event: dict[str, Any], adapter: Any, correlation_id: str
) -> dict[str, Any]:
    """Step 2: Write new key as AWSPENDING version in SM."""
    secret_id = event["secret_id"]
    new_key_data = event.get("new_key_data", {})
    old_key_id = event.get("old_key_id", "")

    # Fetch current secret to merge new key into existing structure
    current = _get_current_secret(secret_id)
    if current is None:
        raise Exception("Failed to fetch current secret for pending write")

    # Build updated secret with new key values
    updated = dict(current)

    if adapter.provider_name == "internal":
        # Internal adapter returns multiple keys in metadata
        new_keys = new_key_data.get("metadata", {}).get("new_keys", {})
        updated.update(new_keys)
    elif adapter.provider_name == "stripe":
        updated["restricted_key"] = new_key_data["key_value"]
        updated["_old_restricted_key_id"] = old_key_id
    elif adapter.provider_name == "twilio":
        updated["api_key"] = new_key_data["key_id"]
        updated["api_secret"] = new_key_data["key_value"]
        updated["_old_api_key_sid"] = old_key_id
    elif adapter.provider_name == "openai":
        updated["api_key"] = new_key_data["key_value"]
        updated["_old_key_id"] = old_key_id
    elif adapter.provider_name == "supabase":
        updated["service_role_key"] = new_key_data["key_value"]

    # Track rotation metadata
    updated["_key_id"] = new_key_data.get("key_id", "")
    updated["_rotated_at"] = datetime.now(timezone.utc).isoformat()
    updated["_rotation_correlation_id"] = correlation_id

    # Write as new version with AWSPENDING stage
    version_id = str(uuid.uuid4())
    sm_client.put_secret_value(
        SecretId=secret_id,
        ClientRequestToken=version_id,
        SecretString=json.dumps(updated),
        VersionStages=["AWSPENDING"],
    )

    logger.info("Wrote AWSPENDING version %s for %s", version_id, secret_id)

    return {
        "success": True,
        "version_id": version_id,
        "secret_id": secret_id,
    }


def _handle_test_key(
    event: dict[str, Any], adapter: Any, correlation_id: str
) -> dict[str, Any]:
    """Step 3: Test the new key with a synthetic API call."""
    secret_id = event["secret_id"]
    pending_version_id = event.get("pending_version_id", "")

    # Fetch the AWSPENDING version to get the new key values
    pending = _get_pending_secret(secret_id, pending_version_id)
    if pending is None:
        raise Exception("Failed to fetch AWSPENDING secret for testing")

    result = adapter.test_key(pending)

    if not result.success:
        if result.retryable:
            # Raise retryable error for Step Functions retry
            raise type("TestKeyRetryable", (Exception,), {})(
                f"Test failed (retryable): {result.error}"
            )
        else:
            raise Exception(f"Test failed (non-retryable): {result.error}")

    logger.info(
        "Test passed for %s: %s (%.1fms)",
        adapter.provider_name, result.test_name, result.latency_ms,
    )

    return {
        "success": True,
        "test_name": result.test_name,
        "latency_ms": result.latency_ms,
    }


def _handle_promote(
    event: dict[str, Any], adapter: Any, correlation_id: str
) -> dict[str, Any]:
    """Step 4: Promote AWSPENDING → AWSCURRENT."""
    secret_id = event["secret_id"]
    pending_version_id = event.get("pending_version_id", "")

    # Find current version
    metadata = sm_client.describe_secret(SecretId=secret_id)
    versions = metadata.get("VersionIdsToStages", {})
    current_version = next(
        (v for v, stages in versions.items() if "AWSCURRENT" in stages),
        None,
    )

    if not current_version:
        raise Exception("No AWSCURRENT version found")

    # Promote pending to current
    sm_client.update_secret_version_stage(
        SecretId=secret_id,
        VersionStage="AWSCURRENT",
        MoveToVersionId=pending_version_id,
        RemoveFromVersionId=current_version,
    )

    logger.info(
        "Promoted %s: %s → AWSCURRENT (was %s)",
        secret_id, pending_version_id, current_version,
    )

    # Determine overlap window based on adapter
    overlap_map = {
        "stripe": 3600,     # 1 hour
        "twilio": 1800,     # 30 min
        "openai": 900,      # 15 min
        "internal": 300,    # 5 min (services cache TTL)
        "supabase": 1800,   # 30 min
    }
    overlap_seconds = overlap_map.get(adapter.provider_name, 600)

    return {
        "success": True,
        "promoted_version": pending_version_id,
        "previous_version": current_version,
        "overlap_seconds": overlap_seconds,
    }


def _handle_verify_cutover(
    event: dict[str, Any], adapter: Any, correlation_id: str
) -> dict[str, Any]:
    """Step 5: Verify services picked up the new key."""
    # For now, re-run the test against the AWSCURRENT version
    secret_id = event["secret_id"]
    current = _get_current_secret(secret_id)

    if current is None:
        raise type("CutoverRetryable", (Exception,), {})(
            "Failed to fetch AWSCURRENT for cutover verification"
        )

    result = adapter.test_key(current)

    if not result.success:
        raise type("CutoverRetryable", (Exception,), {})(
            f"Cutover verification failed: {result.error}"
        )

    return {
        "success": True,
        "test_name": result.test_name,
        "latency_ms": result.latency_ms,
    }


def _handle_revoke_old(
    event: dict[str, Any], adapter: Any, correlation_id: str
) -> dict[str, Any]:
    """Step 6: Revoke the old key via vendor API."""
    secret_id = event["secret_id"]
    old_key_id = event.get("old_key_id", "")

    current = _get_current_secret(secret_id)
    result = adapter.revoke_key(old_key_id, current or {})

    if not result.success:
        # Revoke failure is non-fatal but alarming — old key stays valid
        logger.warning(
            "Revoke failed for %s key %s: %s",
            adapter.provider_name, old_key_id, result.error,
        )

    return {
        "success": result.success,
        "revoked_key_id": result.revoked_key_id,
        "revocation_immediate": result.revocation_immediate,
        "error": result.error,
    }


def _handle_rollback_pending(
    event: dict[str, Any], adapter: Any, correlation_id: str
) -> dict[str, Any]:
    """Rollback: Remove AWSPENDING version (test failed, key is bad)."""
    secret_id = event["secret_id"]
    pending_version_id = event.get("pending_version_id", "")

    try:
        sm_client.update_secret_version_stage(
            SecretId=secret_id,
            VersionStage="AWSPENDING",
            RemoveFromVersionId=pending_version_id,
        )
        logger.info("Rolled back AWSPENDING %s for %s", pending_version_id, secret_id)
    except Exception as e:
        logger.error("Rollback failed: %s", e)

    # Best-effort: revoke the new (bad) key we created
    new_key_data = event.get("new_key_data", {})
    if new_key_data.get("key_id"):
        current = _get_current_secret(secret_id)
        adapter.revoke_key(new_key_data["key_id"], current or {})

    return {"success": True, "rolled_back_version": pending_version_id}


def _handle_emit_receipt(
    event: dict[str, Any], adapter: Any, correlation_id: str
) -> dict[str, Any]:
    """Step 7: Emit an immutable rotation receipt (Law #2)."""
    outcome = event.get("outcome", "unknown")
    create_result = event.get("create_result", {})
    test_result = event.get("test_result", {})
    revoke_result = event.get("revoke_result", {})
    error_info = event.get("error_info", {})

    receipt = adapter.build_receipt_data(
        correlation_id=correlation_id,
        outcome=outcome,
        old_key_id=create_result.get("old_key_id", ""),
        new_key_id=create_result.get("new_key_data", {}).get("key_id", ""),
        error=error_info.get("Cause", "") if outcome == "failed" else "",
        test_latency_ms=test_result.get("latency_ms", 0.0),
    )

    # Publish receipt to SNS for downstream consumers (Supabase receipts table, etc.)
    if SNS_EVENTS_TOPIC:
        sns_client.publish(
            TopicArn=SNS_EVENTS_TOPIC,
            Subject=f"Aspire Rotation Receipt: {adapter.provider_name} ({outcome})",
            Message=json.dumps(receipt, default=str),
            MessageAttributes={
                "receipt_type": {"DataType": "String", "StringValue": "secret.rotation"},
                "provider": {"DataType": "String", "StringValue": adapter.provider_name},
                "outcome": {"DataType": "String", "StringValue": outcome},
            },
        )

    logger.info(
        "Receipt emitted: %s %s %s (correlation=%s)",
        adapter.provider_name, outcome, receipt["receipt_id"], correlation_id,
    )

    return {
        "receipt_id": receipt["receipt_id"],
        "correlation_id": correlation_id,
        "outcome": outcome,
    }


# =============================================================================
# SM Helpers
# =============================================================================

def _get_current_secret(secret_id: str) -> dict[str, Any] | None:
    """Fetch AWSCURRENT secret value."""
    try:
        resp = sm_client.get_secret_value(SecretId=secret_id, VersionStage="AWSCURRENT")
        return json.loads(resp["SecretString"])
    except Exception as e:
        logger.error("Failed to fetch AWSCURRENT for %s: %s", secret_id, e)
        return None


def _get_pending_secret(secret_id: str, version_id: str) -> dict[str, Any] | None:
    """Fetch AWSPENDING secret value."""
    try:
        resp = sm_client.get_secret_value(
            SecretId=secret_id,
            VersionId=version_id,
            VersionStage="AWSPENDING",
        )
        return json.loads(resp["SecretString"])
    except Exception as e:
        logger.error("Failed to fetch AWSPENDING for %s/%s: %s", secret_id, version_id, e)
        return None
