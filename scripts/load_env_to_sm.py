"""Load all secrets from .env files into AWS Secrets Manager.

Reads Aspire-desktop/.env and backend/orchestrator/.env,
maps keys into the 6 SM secret groups, and uploads via boto3.

Does NOT overwrite aspire/dev/internal (already rotating).
Does NOT store config values (URLs, ports) — only actual secrets.
"""
import json
import os
import sys

# Parse .env file into dict
def parse_env(path):
    env = {}
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if v:  # skip empty values
                    env[k] = v
    return env

BASE = "/mnt/c/Users/tonio/Projects/myapp"
desktop_env = parse_env(f"{BASE}/Aspire-desktop/.env")
orch_env = parse_env(f"{BASE}/backend/orchestrator/.env")
docker_env = parse_env(f"{BASE}/infrastructure/docker/.env")

# --- Build secret groups ---

stripe_secret = {
    "restricted_key": desktop_env.get("STRIPE_CONNECT_SECRET_KEY", ""),
    "secret_key": desktop_env.get("STRIPE_SECRET_KEY", ""),
    "publishable_key": desktop_env.get("STRIPE_PUBLISHABLE_KEY", ""),
}

supabase_secret = {
    "service_role_key": desktop_env.get("SUPABASE_SERVICE_ROLE_KEY", ""),
    "anon_key": desktop_env.get("SUPABASE_ANON_KEY", ""),
}

openai_secret = {
    "api_key": orch_env.get("ASPIRE_OPENAI_API_KEY", ""),
}

twilio_secret = {
    "account_sid": orch_env.get("ASPIRE_TWILIO_ACCOUNT_SID", ""),
    "auth_token": orch_env.get("ASPIRE_TWILIO_AUTH_TOKEN", ""),
}

providers_secret = {
    # Voice / Video / Avatar
    "elevenlabs_key": orch_env.get("ASPIRE_ELEVENLABS_API_KEY", ""),
    "deepgram_key": orch_env.get("ASPIRE_DEEPGRAM_API_KEY", ""),
    "livekit_key": desktop_env.get("LIVEKIT_API_KEY", ""),
    "livekit_secret": desktop_env.get("LIVEKIT_API_SECRET", ""),
    "anam_key": desktop_env.get("ANAM_API_KEY", ""),
    # Search / Research (Adam)
    "tavily_key": orch_env.get("ASPIRE_TAVILY_API_KEY", ""),
    "brave_key": orch_env.get("ASPIRE_BRAVE_API_KEY", ""),
    "google_maps_key": orch_env.get("ASPIRE_GOOGLE_MAPS_API_KEY", ""),
    # Financial (Finn / Quinn / Teressa / Milo)
    "plaid_client_id": orch_env.get("ASPIRE_PLAID_CLIENT_ID", ""),
    "plaid_secret": orch_env.get("ASPIRE_PLAID_SECRET", ""),
    "quickbooks_client_id": orch_env.get("ASPIRE_QUICKBOOKS_CLIENT_ID", ""),
    "quickbooks_client_secret": orch_env.get("ASPIRE_QUICKBOOKS_CLIENT_SECRET", ""),
    "gusto_client_id": orch_env.get("ASPIRE_GUSTO_CLIENT_ID", ""),
    "gusto_client_secret": orch_env.get("ASPIRE_GUSTO_CLIENT_SECRET", ""),
    "moov_api_key": orch_env.get("ASPIRE_MOOV_API_KEY", ""),
    "moov_client_id": orch_env.get("ASPIRE_MOOV_CLIENT_ID", ""),
    "moov_client_secret": orch_env.get("ASPIRE_MOOV_CLIENT_SECRET", ""),
    # Legal (Clara)
    "pandadoc_key": orch_env.get("ASPIRE_PANDADOC_API_KEY", ""),
    # Domains (mail_ops)
    "resellerclub_userid": desktop_env.get("RESCLUB_AUTH_USERID", ""),
    "resellerclub_key": desktop_env.get("RESCLUB_API_KEY", ""),
}

# Remove empty values
for secret in [stripe_secret, supabase_secret, openai_secret, twilio_secret, providers_secret]:
    for k in list(secret.keys()):
        if not secret[k]:
            del secret[k]

# --- Upload to SM ---
import boto3

sm = boto3.client(
    "secretsmanager",
    region_name="us-east-1",
    aws_access_key_id=docker_env["AWS_ROTATION_TRIGGER_ACCESS_KEY_ID"],
    aws_secret_access_key=docker_env["AWS_ROTATION_TRIGGER_SECRET_ACCESS_KEY"],
)

secrets_to_load = {
    "aspire/dev/stripe": stripe_secret,
    "aspire/dev/supabase": supabase_secret,
    "aspire/dev/openai": openai_secret,
    "aspire/dev/twilio": twilio_secret,
    "aspire/dev/providers": providers_secret,
    # NOTE: aspire/dev/internal is SKIPPED — already rotating successfully
}

for secret_id, secret_data in secrets_to_load.items():
    key_count = len(secret_data)
    key_names = list(secret_data.keys())

    try:
        # Try to update existing secret
        sm.put_secret_value(
            SecretId=secret_id,
            SecretString=json.dumps(secret_data),
        )
        print("UPDATED {}: {} keys ({})".format(secret_id, key_count, ", ".join(key_names)))
    except sm.exceptions.ResourceNotFoundException:
        # Create new secret
        sm.create_secret(
            Name=secret_id,
            Description="Aspire dev credentials",
            SecretString=json.dumps(secret_data),
            Tags=[
                {"Key": "Project", "Value": "aspire"},
                {"Key": "Environment", "Value": "dev"},
                {"Key": "ManagedBy", "Value": "load_env_to_sm"},
            ],
        )
        print("CREATED {}: {} keys ({})".format(secret_id, key_count, ", ".join(key_names)))

# --- Summary ---
print()
print("=" * 50)
total_keys = sum(len(v) for v in secrets_to_load.values())
print("Loaded {} keys across {} secret groups".format(total_keys, len(secrets_to_load)))
print()
print("SKIPPED: aspire/dev/internal (already rotating)")
print()
print("NOT STORED (config, not secrets):")
print("  SUPABASE_URL, LIVEKIT_URL, DOMAIN_RAIL_URL, RESCLUB_BASE_URL")
print("  QUICKBOOKS_BASE_URL, ANAM_PERSONA_ID, AWS region/ports")
print()
print("WARNING: backend/orchestrator/.env contains OLD AWS key AKIA4IY2OBQHKKC5P5NZ")
print("  This key should be DEACTIVATED if not already done.")
