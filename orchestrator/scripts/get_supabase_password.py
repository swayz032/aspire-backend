#!/usr/bin/env python3
"""Get Supabase database password from AWS Secrets Manager."""

import os
import boto3
import json

# AWS credentials from .env
os.environ.setdefault("AWS_ACCESS_KEY_ID", "AKIA4IY2OBQHG7PBSERR")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "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")  # Need actual secret
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

sm = boto3.client("secretsmanager", region_name="us-east-1")

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
