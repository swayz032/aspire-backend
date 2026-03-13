"""AWS Secrets Manager Setup - Create secret groups from environment-backed values.

Run with the required secret values exported in the environment. This script never
stores live credentials in source control.
"""

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


SECRET_TEMPLATES = {
    "aspire/dev/stripe": {
        "description": "Stripe sandbox API keys (environment-backed import)",
        "values": {
            "restricted_key": ("STRIPE_RESTRICTED_KEY", "ASPIRE_STRIPE_RESTRICTED_KEY"),
            "secret_key": ("STRIPE_SECRET_KEY", "ASPIRE_STRIPE_API_KEY"),
            "publishable_key": ("STRIPE_PUBLISHABLE_KEY", "ASPIRE_STRIPE_PUBLISHABLE_KEY"),
            "webhook_secret": ("STRIPE_WEBHOOK_SECRET", "ASPIRE_STRIPE_WEBHOOK_SECRET"),
        },
    },
    "aspire/dev/supabase": {
        "description": "Supabase project credentials",
        "values": {
            "service_role_key": ("SUPABASE_SERVICE_ROLE_KEY", "ASPIRE_SUPABASE_SERVICE_ROLE_KEY"),
            "jwt_secret": ("SUPABASE_JWT_SECRET", "ASPIRE_SUPABASE_JWT_SECRET"),
        },
    },
    "aspire/dev/openai": {
        "description": "OpenAI API key (Ava Brain)",
        "values": {
            "api_key": ("OPENAI_API_KEY", "ASPIRE_OPENAI_API_KEY"),
        },
    },
    "aspire/dev/twilio": {
        "description": "Twilio credentials",
        "values": {
            "account_sid": ("TWILIO_ACCOUNT_SID", "ASPIRE_TWILIO_ACCOUNT_SID"),
            "auth_token": ("TWILIO_AUTH_TOKEN", "ASPIRE_TWILIO_AUTH_TOKEN"),
            "api_key": ("TWILIO_API_KEY", "ASPIRE_TWILIO_API_KEY"),
            "api_secret": ("TWILIO_API_SECRET", "ASPIRE_TWILIO_API_SECRET"),
        },
    },
    "aspire/dev/internal": {
        "description": "Internal signing, encryption, and HMAC keys",
        "values": {
            "token_signing_secret": ("TOKEN_SIGNING_SECRET", "ASPIRE_TOKEN_SIGNING_SECRET"),
            "token_encryption_key": ("TOKEN_ENCRYPTION_KEY", "ASPIRE_TOKEN_ENCRYPTION_KEY"),
            "n8n_hmac_secret": ("ASPIRE_N8N_HMAC_SECRET", "N8N_INTAKE_WEBHOOK_SECRET"),
            "n8n_eli_webhook_secret": ("N8N_ELI_WEBHOOK_SECRET",),
            "n8n_sarah_webhook_secret": ("N8N_SARAH_WEBHOOK_SECRET",),
            "n8n_nora_webhook_secret": ("N8N_NORA_WEBHOOK_SECRET",),
            "domain_rail_hmac_secret": ("DOMAIN_RAIL_HMAC_SECRET", "ASPIRE_DOMAIN_RAIL_HMAC_SECRET"),
            "gateway_internal_key": ("GATEWAY_INTERNAL_KEY", "ASPIRE_GATEWAY_INTERNAL_KEY"),
        },
    },
    "aspire/dev/providers": {
        "description": "Third-party provider API keys (environment-backed import)",
        "values": {
            "elevenlabs_key": ("ELEVENLABS_API_KEY", "ASPIRE_ELEVENLABS_API_KEY"),
            "deepgram_key": ("DEEPGRAM_API_KEY", "ASPIRE_DEEPGRAM_API_KEY"),
            "livekit_key": ("LIVEKIT_API_KEY", "ASPIRE_LIVEKIT_API_KEY"),
            "livekit_secret": ("LIVEKIT_API_SECRET", "ASPIRE_LIVEKIT_API_SECRET"),
            "anam_key": ("ANAM_API_KEY", "ASPIRE_ANAM_API_KEY"),
            "tavily_key": ("TAVILY_API_KEY", "ASPIRE_TAVILY_API_KEY"),
            "brave_key": ("BRAVE_API_KEY", "ASPIRE_BRAVE_API_KEY"),
            "google_maps_key": ("GOOGLE_MAPS_API_KEY", "ASPIRE_GOOGLE_MAPS_API_KEY"),
            "plaid_client_id": ("PLAID_CLIENT_ID", "ASPIRE_PLAID_CLIENT_ID"),
            "plaid_secret": ("PLAID_SECRET", "ASPIRE_PLAID_SECRET"),
            "quickbooks_client_id": ("QUICKBOOKS_CLIENT_ID", "ASPIRE_QUICKBOOKS_CLIENT_ID"),
            "quickbooks_client_secret": ("QUICKBOOKS_CLIENT_SECRET", "ASPIRE_QUICKBOOKS_CLIENT_SECRET"),
            "gusto_client_id": ("GUSTO_CLIENT_ID", "ASPIRE_GUSTO_CLIENT_ID"),
            "gusto_client_secret": ("GUSTO_CLIENT_SECRET", "ASPIRE_GUSTO_CLIENT_SECRET"),
            "pandadoc_api_key": ("PANDADOC_API_KEY", "ASPIRE_PANDADOC_API_KEY"),
            "moov_api_key": ("MOOV_API_KEY", "ASPIRE_MOOV_API_KEY"),
            "moov_client_id": ("MOOV_CLIENT_ID", "ASPIRE_MOOV_CLIENT_ID"),
            "moov_client_secret": ("MOOV_CLIENT_SECRET", "ASPIRE_MOOV_CLIENT_SECRET"),
            "resellerclub_userid": ("RESELLERCLUB_USERID", "ASPIRE_RESELLERCLUB_USERID"),
            "resellerclub_api_key": ("RESELLERCLUB_API_KEY", "ASPIRE_RESELLERCLUB_API_KEY"),
        },
    },
}


def build_secret_payloads() -> dict[str, dict[str, object]]:
    payloads: dict[str, dict[str, object]] = {}
    for path, config in SECRET_TEMPLATES.items():
        values = {key: _env(*env_names) for key, env_names in config["values"].items()}
        payloads[path] = {
            "description": config["description"],
            "values": values,
        }
    return payloads


def main():
    print("=" * 60)
    print("AWS Secrets Manager Setup - Aspire Dev Environment")
    print("=" * 60)

    print("\n[1/4] Verifying AWS credentials...")
    client = boto3.client("secretsmanager", region_name=REGION)
    sts = boto3.client("sts", region_name=REGION)

    try:
        identity = sts.get_caller_identity()
        print(f"  Account: {identity['Account']}")
        print(f"  ARN: {identity['Arn']}")
        print(f"  UserID: {identity['UserId']}")
    except Exception as exc:
        print(f"  FATAL: AWS credential verification failed: {exc}")
        sys.exit(1)

    secrets = build_secret_payloads()

    print(f"\n[2/4] Creating {len(secrets)} secret groups...")
    created = 0
    updated = 0

    for path, config in secrets.items():
        secret_string = json.dumps(config["values"])
        try:
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
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceExistsException":
                client.put_secret_value(SecretId=path, SecretString=secret_string)
                print(f"  UPDATED: {path} ({len(config['values'])} keys)")
                updated += 1
            else:
                print(f"  FAILED: {path}: {exc}")
                raise

    print(f"\n  Summary: {created} created, {updated} updated")

    print(f"\n[3/4] Verifying all {len(secrets)} secrets readable...")
    for path in secrets:
        try:
            resp = client.get_secret_value(SecretId=path)
            data = json.loads(resp["SecretString"])
            key_count = len(data)
            non_empty = sum(1 for value in data.values() if value)
            print(f"  OK: {path} - {key_count} keys ({non_empty} non-empty)")
        except Exception as exc:
            print(f"  FAIL: {path}: {exc}")

    print("\n[4/4] Listing all aspire/* secrets in SM...")
    paginator = client.get_paginator("list_secrets")
    for page in paginator.paginate(Filters=[{"Key": "name", "Values": ["aspire/"]}]):
        for secret in page["SecretList"]:
            print(f"  {secret['Name']} - {secret.get('Description', 'no desc')}")
            if "LastRotatedDate" in secret:
                print(f"    Last rotated: {secret['LastRotatedDate']}")

    print("\n" + "=" * 60)
    print("SM setup complete. Secrets ready for service bootstrap.")
    print("=" * 60)


if __name__ == "__main__":
    main()
