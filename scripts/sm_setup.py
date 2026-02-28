"""AWS Secrets Manager Setup — Create secret groups + import values from .env files.

Run via WSL:
  source ~/venvs/aspire/bin/activate
  AWS_ACCESS_KEY_ID=<key> AWS_SECRET_ACCESS_KEY=<secret> python scripts/sm_setup.py

Creates 6 SM secret groups under aspire/dev/*:
  aspire/dev/stripe, aspire/dev/supabase, aspire/dev/openai,
  aspire/dev/twilio, aspire/dev/internal, aspire/dev/providers
"""

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"

# ── Secret Groups ──────────────────────────────────────────────────────────────

SECRETS = {
    "aspire/dev/stripe": {
        "description": "Stripe sandbox API keys (Scott Consultants test account)",
        "values": {
            "restricted_key": "rk_test_51Sx304HG2dxKUrArr9uPAdZprd6GkWIaRI5MHXGprgKMMYIY4y4lc2dau8dPTBzNOFURl0IJLqQIf2l7ickjO74W00vJGp2Hxh",
            "secret_key": "sk_test_51Sx304HG2dxKUrArb8S48wzY0peUgAr34sTZ1St7MhAm4PX0yZuC16LonBhSdBChKKcM7dyN7KGqdAxJaTPYCveV00TcXeqBrZ",
            "publishable_key": "pk_test_51Sx304HG2dxKUrAr7DQI36dNUhNYeoGqNWgIXyIU4SuZA2jBRLrbCfABroE3hc69nOxiFp7eVafn7KfZcCjW2H2X00TsDy4rDV",
            "webhook_secret": "",
        },
    },
    "aspire/dev/supabase": {
        "description": "Supabase project credentials",
        "values": {
            "service_role_key": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InF0dWVoanFsY21mY2FzY3FqamhjIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2NTg0MzY2MiwiZXhwIjoyMDgxNDE5NjYyfQ.qfoKM6lyk95qTVmapP7UGeLDBo8WcLlyPLWfSJMuSDE",
            "jwt_secret": "",
        },
    },
    "aspire/dev/openai": {
        "description": "OpenAI API key (Ava Brain)",
        "values": {
            "api_key": "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",
        },
    },
    "aspire/dev/twilio": {
        "description": "Twilio trial credentials (Sarah Front Desk)",
        "values": {
            "account_sid": "ACec203fae571300ffade21ce91c5d0bfb",
            "auth_token": "aba7a11a10f487cd4a183718f8e528f4",
            "api_key": "",
            "api_secret": "",
        },
    },
    "aspire/dev/internal": {
        "description": "Internal signing, encryption, and HMAC keys",
        "values": {
            "token_signing_secret": "0713b7e0899b4db6f312ced928ee5dcff8ff2087fbf15f234a50c5534005730f",
            "token_encryption_key": "9077c233a854d71ed00d58402991750a",
            "n8n_hmac_secret": "aspire-n8n-dev-secret",
            "n8n_eli_webhook_secret": "aspire-eli-dev-secret",
            "n8n_sarah_webhook_secret": "aspire-sarah-dev-secret",
            "n8n_nora_webhook_secret": "aspire-nora-dev-secret",
            "domain_rail_hmac_secret": "A5DWNJ8gP5jiaGkprzNOSm2bypgsSgfFPCXA2AKKA6k",
            "gateway_internal_key": "",
        },
    },
    "aspire/dev/providers": {
        "description": "Third-party provider API keys (Tier 2 — alert-based rotation)",
        "values": {
            "elevenlabs_key": "sk_648c94797fbd0c1bb72a249d4b5b1d304978475395055e1b",
            "deepgram_key": "3dc18b941e09110033d3a3745849e09dd5785dfb",
            "livekit_key": "API5GKMaontnTq4",
            "livekit_secret": "VRIt6ojPJqf940Y63Y2olvGIPDXl8veFfaj0Lb6S32PB",
            "anam_key": "ZDY3ZmViMGItMjNmNy00MTFlLWJlMDAtNDc0NzIxZmRjZDEwOi9pa3ZQcXI0Z1F3NzRnZlpoVGNCWW5Ca1M2TnNwZlA1MDV0N0lpb1BnRlk9",
            "tavily_key": "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",
            "brave_key": "BSAgu5P1nU6-AGkVyT5faLz63AphY18",
            "google_maps_key": "AIzaSyDfOXAUQAbW-yg7vEpQVllMhlmw-cY5d2g",
            "plaid_client_id": "69831b28fd4ffa0021dbf0b6",
            "plaid_secret": "5f23214d156844ee7de641fd9b7533",
            "quickbooks_client_id": "ABKpjf7ZbKu25f6ssn5rWorTXA0s4W6nB9PbH4Cn1wHtI6ngpQ",
            "quickbooks_client_secret": "urZLrgDKl5ZdGe2jfJDNCy0BwlAUbpB0WPhmuO8y",
            "gusto_client_id": "jo5P_4IYILNEhyEpEr2KZDt5sUZPak7PSzyUL4IT9Nw",
            "gusto_client_secret": "mMfUhCZU8dG77vg3FfW-wPglJik1opOATqnIQy9sYQg",
            "pandadoc_api_key": "f414d4ab73ba0d1a6a584f56b07f1b5267ae7d7e",
            "moov_api_key": "NqHAP4_vPgX6kf2n",
            "moov_client_id": "NqHAP4_vPgX6kf2n",
            "moov_client_secret": "NffXvpq7icKNotTV8qJo03f0S5Ppdyor",
            "resellerclub_userid": "1315307",
            "resellerclub_api_key": "Ztyk3X1ooNV6i3wNJpGKcVwEbjzUOqWx",
        },
    },
}


def main():
    print("=" * 60)
    print("AWS Secrets Manager Setup — Aspire Dev Environment")
    print("=" * 60)

    # ── Step 1: Verify credentials ──
    print("\n[1/4] Verifying AWS credentials...")
    client = boto3.client("secretsmanager", region_name=REGION)
    sts = boto3.client("sts", region_name=REGION)

    try:
        identity = sts.get_caller_identity()
        print(f"  Account: {identity['Account']}")
        print(f"  ARN: {identity['Arn']}")
        print(f"  UserID: {identity['UserId']}")
    except Exception as e:
        print(f"  FATAL: AWS credential verification failed: {e}")
        sys.exit(1)

    # ── Step 2: Create or update secrets ──
    print(f"\n[2/4] Creating {len(SECRETS)} secret groups...")
    created = 0
    updated = 0

    for path, config in SECRETS.items():
        secret_string = json.dumps(config["values"])
        try:
            # Try to create new secret
            client.create_secret(
                Name=path,
                Description=config["description"],
                SecretString=secret_string,
                Tags=[
                    {"Key": "Project", "Value": "aspire"},
                    {"Key": "Environment", "Value": "dev"},
                    {"Key": "ManagedBy", "Value": "boto3-setup"},
                ],
            )
            print(f"  CREATED: {path} ({len(config['values'])} keys)")
            created += 1
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceExistsException":
                # Secret already exists — update it
                client.put_secret_value(SecretId=path, SecretString=secret_string)
                print(f"  UPDATED: {path} ({len(config['values'])} keys)")
                updated += 1
            else:
                print(f"  FAILED: {path}: {e}")
                raise

    print(f"\n  Summary: {created} created, {updated} updated")

    # ── Step 3: Verify by reading back ──
    print(f"\n[3/4] Verifying all {len(SECRETS)} secrets readable...")
    for path in SECRETS:
        try:
            resp = client.get_secret_value(SecretId=path)
            data = json.loads(resp["SecretString"])
            key_count = len(data)
            non_empty = sum(1 for v in data.values() if v)
            print(f"  OK: {path} — {key_count} keys ({non_empty} non-empty)")
        except Exception as e:
            print(f"  FAIL: {path}: {e}")

    # ── Step 4: List all aspire secrets ──
    print("\n[4/4] Listing all aspire/* secrets in SM...")
    paginator = client.get_paginator("list_secrets")
    for page in paginator.paginate(Filters=[{"Key": "name", "Values": ["aspire/"]}]):
        for s in page["SecretList"]:
            print(f"  {s['Name']} — {s.get('Description', 'no desc')}")
            if "LastRotatedDate" in s:
                print(f"    Last rotated: {s['LastRotatedDate']}")

    print("\n" + "=" * 60)
    print("SM setup complete. Secrets ready for service bootstrap.")
    print("=" * 60)


if __name__ == "__main__":
    main()
