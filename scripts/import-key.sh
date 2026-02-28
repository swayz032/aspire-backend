#!/bin/bash
# =============================================================================
# Aspire Manual Key Import
# =============================================================================
# For vendors that don't have key creation APIs (ElevenLabs, Deepgram, etc.).
# When CloudWatch alarm emails you at 80 days:
#   1. Create new key in vendor dashboard
#   2. Run this script
#   3. Done — services auto-reload within 5 min
#
# Usage: ./scripts/import-key.sh <group> <key_name> <new_value>
#
# Examples:
#   ./scripts/import-key.sh providers elevenlabs_key "sk-new-key-here"
#   ./scripts/import-key.sh providers deepgram_key "dg-new-key-here"
#   ./scripts/import-key.sh providers anam_key "ak-new-key-here"
#   ./scripts/import-key.sh providers livekit_key "APInewkey"
#   ./scripts/import-key.sh providers livekit_secret "newsecret"
#
# Prerequisites:
#   - AWS CLI configured with SM write access
#   - ASPIRE_ENV env var (default: prod)
# =============================================================================
set -euo pipefail

GROUP="${1:?Usage: import-key.sh <group> <key_name> <new_value>}"
KEY_NAME="${2:?Usage: import-key.sh <group> <key_name> <new_value>}"
NEW_VALUE="${3:?Usage: import-key.sh <group> <key_name> <new_value>}"
ENV="${ASPIRE_ENV:-prod}"
SECRET_ID="aspire/${ENV}/${GROUP}"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)

# --- Colors ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# --- Input validation (prevent injection) ---
if [[ ! "$GROUP" =~ ^[a-z_]+$ ]]; then
  echo -e "${RED}[error]${NC} Invalid group name: must be lowercase letters and underscores only"
  exit 1
fi
if [[ ! "$KEY_NAME" =~ ^[a-z_]+$ ]]; then
  echo -e "${RED}[error]${NC} Invalid key name: must be lowercase letters and underscores only"
  exit 1
fi

echo -e "${YELLOW}=== Manual Key Import ===${NC}"
echo "Secret: ${SECRET_ID}"
echo "Key: ${KEY_NAME}"
echo "Environment: ${ENV}"
echo ""

# --- Fetch current secret ---
echo -e "${YELLOW}[fetch]${NC} Getting current secret..."
CURRENT=$(aws secretsmanager get-secret-value \
  --secret-id "$SECRET_ID" \
  --query SecretString \
  --output text 2>&1)

if [ $? -ne 0 ]; then
  echo -e "${RED}[error]${NC} Failed to fetch secret: ${CURRENT}"
  exit 1
fi

# --- Update the specific key (values passed via env vars, not string interpolation) ---
echo -e "${YELLOW}[update]${NC} Updating ${KEY_NAME}..."
UPDATED=$(echo "$CURRENT" | IMPORT_KEY_NAME="$KEY_NAME" IMPORT_NEW_VALUE="$NEW_VALUE" IMPORT_TIMESTAMP="$TIMESTAMP" python3 -c "
import json, os, sys
d = json.load(sys.stdin)
key_name = os.environ['IMPORT_KEY_NAME']
d[key_name] = os.environ['IMPORT_NEW_VALUE']
d['_rotated_at'] = os.environ['IMPORT_TIMESTAMP']
d['_rotation_correlation_id'] = 'manual-import-' + key_name + '-' + os.environ['IMPORT_TIMESTAMP']
print(json.dumps(d))
")

if [ -z "$UPDATED" ]; then
  echo -e "${RED}[error]${NC} Failed to build updated secret JSON"
  exit 1
fi

# --- Write new version ---
aws secretsmanager put-secret-value \
  --secret-id "$SECRET_ID" \
  --secret-string "$UPDATED" 2>&1

if [ $? -ne 0 ]; then
  echo -e "${RED}[error]${NC} Failed to write updated secret"
  exit 1
fi

CORRELATION_ID="manual-import-${KEY_NAME}-${TIMESTAMP}"

# --- Emit receipt (Law #2: No Action Without a Receipt) ---
RECEIPT_JSON=$(IMPORT_KEY_NAME="$KEY_NAME" IMPORT_TIMESTAMP="$TIMESTAMP" IMPORT_SECRET_ID="$SECRET_ID" IMPORT_CORRELATION_ID="$CORRELATION_ID" python3 -c "
import json, os
print(json.dumps({
    'receipt_id': 'manual-import-' + os.environ['IMPORT_KEY_NAME'] + '-' + os.environ['IMPORT_TIMESTAMP'],
    'suite_id': 'ffffffff-0000-0000-0000-system000000',
    'office_id': 'ffffffff-0000-0000-0000-system000000',
    'tenant_id': 'system',
    'receipt_type': 'ops_manual',
    'status': 'SUCCEEDED',
    'correlation_id': os.environ['IMPORT_CORRELATION_ID'],
    'actor_type': 'HUMAN',
    'actor_id': 'operator',
    'action': {
        'action_type': 'ops.manual_key_import',
        'risk_tier': 'yellow',
        'secret_id': os.environ['IMPORT_SECRET_ID'],
        'key_name': os.environ['IMPORT_KEY_NAME']
    },
    'created_at': os.environ['IMPORT_TIMESTAMP']
}))
")

if [ -n "${SUPABASE_URL:-}" ] && [ -n "${SUPABASE_SERVICE_ROLE_KEY:-}" ]; then
  curl -s -X POST "${SUPABASE_URL}/rest/v1/receipts" \
    -H "apikey: ${SUPABASE_SERVICE_ROLE_KEY}" \
    -H "Authorization: Bearer ${SUPABASE_SERVICE_ROLE_KEY}" \
    -H "Content-Type: application/json" \
    -H "Prefer: return=minimal" \
    -d "$RECEIPT_JSON" > /dev/null 2>&1
  echo -e "${GREEN}[receipt]${NC} Receipt emitted to Supabase"
else
  echo -e "${YELLOW}[receipt]${NC} Supabase not configured — receipt logged to stdout:"
  echo "$RECEIPT_JSON"
fi

echo -e "${GREEN}[success]${NC} Updated ${KEY_NAME} in ${SECRET_ID}"
echo ""
echo "Services will pick up the new key within 5 min (cache TTL)."
echo ""
echo "To force immediate reload:"
echo "  railway restart --service Aspire-Desktop"
echo "  railway restart --service Domain-Rail"
echo ""
echo "Correlation ID: ${CORRELATION_ID}"
