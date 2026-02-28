"""Daily secret age checker — pushes CloudWatch metrics for alarm evaluation.

Runs on EventBridge schedule (daily). For each secret:
  1. DescribeSecret to get last rotation date
  2. Calculate age in days
  3. Push SecretAgeDays metric to CloudWatch
  4. If any secret exceeds threshold, push to SNS

This drives the CloudWatch alarms that alert on dashboard-only vendors
(ElevenLabs, Deepgram, etc.) approaching their 90-day rotation deadline.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

sm_client = boto3.client("secretsmanager", region_name="us-east-1")
cw_client = boto3.client("cloudwatch", region_name="us-east-1")
sns_client = boto3.client("sns", region_name="us-east-1")

ENVIRONMENT = os.environ.get("ENVIRONMENT", "prod")
SNS_FAILURE_TOPIC = os.environ.get("SNS_FAILURE_TOPIC", "")

# Secrets to check and their max age in days
SECRET_CONFIG = {
    f"aspire/{ENVIRONMENT}/stripe": {
        "max_age_days": 30,
        "keys_to_track": ["restricted_key", "secret_key"],
    },
    f"aspire/{ENVIRONMENT}/supabase": {
        "max_age_days": 90,
        "keys_to_track": ["service_role_key"],
    },
    f"aspire/{ENVIRONMENT}/openai": {
        "max_age_days": 90,
        "keys_to_track": ["api_key"],
    },
    f"aspire/{ENVIRONMENT}/twilio": {
        "max_age_days": 90,
        "keys_to_track": ["api_key"],
    },
    f"aspire/{ENVIRONMENT}/internal": {
        "max_age_days": 90,
        "keys_to_track": ["token_signing_secret", "n8n_hmac_secret"],
    },
    f"aspire/{ENVIRONMENT}/providers": {
        "max_age_days": 90,
        "keys_to_track": [
            "elevenlabs_key", "deepgram_key", "livekit_key",
            "anam_key", "tavily_key", "brave_key",
        ],
    },
}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Check all secrets, push age metrics, alert on overdue."""
    results = []
    alerts = []
    now = datetime.now(timezone.utc)

    for secret_id, config in SECRET_CONFIG.items():
        try:
            metadata = sm_client.describe_secret(SecretId=secret_id)
        except sm_client.exceptions.ResourceNotFoundException:
            logger.warning("Secret not found: %s", secret_id)
            continue
        except Exception as e:
            logger.error("Failed to describe %s: %s", secret_id, e)
            continue

        # Use LastRotatedDate if available, otherwise LastChangedDate, otherwise CreatedDate
        last_rotated = (
            metadata.get("LastRotatedDate")
            or metadata.get("LastChangedDate")
            or metadata.get("CreatedDate")
        )

        if last_rotated:
            if isinstance(last_rotated, str):
                last_rotated = datetime.fromisoformat(last_rotated.replace("Z", "+00:00"))
            age_days = (now - last_rotated).days
        else:
            age_days = 999  # Unknown age — treat as very old

        max_age = config["max_age_days"]
        secret_short_name = secret_id.split("/")[-1]

        # Push per-key metrics to CloudWatch
        for key_name in config["keys_to_track"]:
            cw_client.put_metric_data(
                Namespace="Aspire/SecretsManager",
                MetricData=[
                    {
                        "MetricName": "SecretAgeDays",
                        "Dimensions": [
                            {"Name": "SecretName", "Value": key_name},
                            {"Name": "SecretGroup", "Value": secret_short_name},
                            {"Name": "Environment", "Value": ENVIRONMENT},
                        ],
                        "Value": float(age_days),
                        "Unit": "Count",
                    }
                ],
            )

        results.append({
            "secret": secret_id,
            "age_days": age_days,
            "max_age_days": max_age,
            "overdue": age_days > max_age,
        })

        if age_days > max_age:
            alerts.append(f"{secret_short_name}: {age_days}d old (max {max_age}d)")

        # Also alert at 80% of max age (early warning)
        elif age_days > int(max_age * 0.8):
            alerts.append(f"{secret_short_name}: {age_days}d old (approaching {max_age}d limit)")

    # Send consolidated alert if any secrets are overdue or approaching deadline
    if alerts and SNS_FAILURE_TOPIC:
        sns_client.publish(
            TopicArn=SNS_FAILURE_TOPIC,
            Subject=f"Aspire Secret Age Alert ({len(alerts)} secrets)",
            Message=(
                "The following secrets need attention:\n\n"
                + "\n".join(f"  - {a}" for a in alerts)
                + "\n\nFor auto-rotated secrets, check rotation Lambda logs."
                + "\nFor manual secrets, create new key in vendor dashboard then run:"
                + "\n  ./scripts/import-key.sh <group> <key_name> <new-value>"
            ),
        )

    logger.info(
        "Age check complete: %d secrets checked, %d alerts",
        len(results), len(alerts),
    )

    # Emit receipt for the age check operation (Law #2)
    if SNS_FAILURE_TOPIC:
        import uuid

        receipt = {
            "receipt_id": str(uuid.uuid4()),
            "suite_id": "ffffffff-0000-0000-0000-system000000",
            "tenant_id": "system",
            "office_id": "ffffffff-0000-0000-0000-system000000",
            "receipt_type": "n8n_ops",
            "status": "SUCCEEDED" if not alerts else "WARNING",
            "correlation_id": f"age-check-{now.strftime('%Y-%m-%d')}",
            "actor_type": "SYSTEM",
            "actor_id": "lambda/secret-age-checker",
            "action": {
                "action_type": "ops.secret_age_check",
                "risk_tier": "green",
                "secrets_checked": len(results),
                "alerts_raised": len(alerts),
                "overdue_secrets": [r["secret"] for r in results if r["overdue"]],
            },
            "created_at": now.isoformat(),
        }
        try:
            sns_client.publish(
                TopicArn=SNS_FAILURE_TOPIC,
                Subject="Aspire Secret Age Check Receipt",
                Message=json.dumps(receipt),
                MessageAttributes={
                    "receipt_type": {"DataType": "String", "StringValue": "age_check"},
                },
            )
        except Exception as e:
            logger.error("Failed to emit age check receipt: %s", e)

    return {
        "checked": len(results),
        "alerts": len(alerts),
        "results": results,
    }
