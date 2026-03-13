#!/usr/bin/env python3
"""Get Supabase database password metadata from AWS Secrets Manager."""

import json
import os

import boto3

region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
sm = boto3.client("secretsmanager", region_name=region)

# Check for Supabase credentials
try:
    response = sm.get_secret_value(SecretId="aspire/dev/supabase")
    secret = json.loads(response["SecretString"])
    print("Supabase credentials found:")
    print(json.dumps({k: "***" if "password" in k.lower() or "key" in k.lower() else v for k, v in secret.items()}, indent=2))
except Exception as e:
    print(f"No Supabase secret found: {e}")

# List all secrets
try:
    response = sm.list_secrets()
    print("\nAll secrets:")
    for secret in response["SecretList"]:
        print(f"  - {secret['Name']}")
except Exception as e:
    print(f"Error listing secrets: {e}")
