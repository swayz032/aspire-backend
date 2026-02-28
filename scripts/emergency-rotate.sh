#!/bin/bash
# =============================================================================
# Aspire Emergency Secret Rotation
# =============================================================================
# Usage: ./scripts/emergency-rotate.sh <provider|all>
#
# Triggers IMMEDIATE rotation via the Rotation API Gateway.
# The Step Functions state machine handles the full 7-step process:
#   CreateKey -> WritePending -> TestKey -> Promote -> Verify -> Revoke -> Receipt
#
# Examples:
#   ./scripts/emergency-rotate.sh stripe     # Rotate Stripe keys NOW
#   ./scripts/emergency-rotate.sh internal   # Rotate all internal HMAC/signing keys
#   ./scripts/emergency-rotate.sh all        # Rotate everything (nuclear option)
#
# Prerequisites:
#   - AWS CLI configured with credentials that can invoke the Rotation API
#   - ASPIRE_ROTATION_API_URL env var set (or pass --api-url)
#   - ASPIRE_ENV env var set (default: prod)
# =============================================================================
set -euo pipefail

# --- Config ---
PROVIDER="${1:?Usage: emergency-rotate.sh <stripe|twilio|openai|internal|supabase|all>}"
ENV="${ASPIRE_ENV:-prod}"
API_URL="${ASPIRE_ROTATION_API_URL:?Set ASPIRE_ROTATION_API_URL to the API Gateway URL}"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${RED}=== EMERGENCY SECRET ROTATION ===${NC}"
echo -e "Provider: ${YELLOW}${PROVIDER}${NC}"
echo -e "Environment: ${YELLOW}${ENV}${NC}"
echo -e "API: ${API_URL}"
echo ""

# --- Confirm ---
if [ "$PROVIDER" == "all" ]; then
  echo -e "${RED}WARNING: This will rotate ALL secrets (stripe, twilio, openai, internal, supabase).${NC}"
  echo -e "${RED}Services will reload within 5 minutes (cache TTL).${NC}"
  read -p "Type 'ROTATE ALL' to confirm: " CONFIRM
  if [ "$CONFIRM" != "ROTATE ALL" ]; then
    echo "Aborted."
    exit 1
  fi
fi

# --- Rotate function ---
rotate_one() {
  local adapter=$1
  local secret_id="aspire/${ENV}/${adapter}"
  local correlation_id="emergency-${adapter}-${TIMESTAMP}"

  echo -e "${YELLOW}[rotate]${NC} Triggering rotation for ${adapter} (${secret_id})..."

  # Call Rotation API Gateway (IAM-authenticated)
  RESPONSE=$(aws apigateway test-invoke-method \
    --rest-api-id "$(echo "$API_URL" | grep -oP '(?<=https://)[^.]+')" \
    --resource-id "rotate" \
    --http-method POST \
    --body "{
      \"secret_id\": \"${secret_id}\",
      \"adapter\": \"${adapter}\",
      \"correlation_id\": \"${correlation_id}\",
      \"triggered_by\": \"emergency-script\",
      \"emergency\": true
    }" 2>/dev/null || \
    # Fallback: direct Step Functions start
    aws stepfunctions start-execution \
      --state-machine-arn "arn:aws:states:us-east-1:843479649294:stateMachine:aspire-secret-rotation-${ENV}" \
      --name "${correlation_id}" \
      --input "{
        \"secret_id\": \"${secret_id}\",
        \"adapter\": \"${adapter}\",
        \"correlation_id\": \"${correlation_id}\",
        \"triggered_by\": \"emergency-script\",
        \"emergency\": true
      }" 2>&1)

  if [ $? -eq 0 ]; then
    echo -e "${GREEN}[rotate]${NC} Rotation triggered for ${adapter}. Correlation: ${correlation_id}"
    echo "  Step Functions will handle: CreateKey -> Test -> Promote -> Verify -> Revoke -> Receipt"
  else
    echo -e "${RED}[rotate]${NC} FAILED to trigger rotation for ${adapter}: ${RESPONSE}"
  fi
}

# --- Execute ---
if [ "$PROVIDER" == "all" ]; then
  for s in stripe twilio openai supabase internal; do
    rotate_one "$s"
    echo ""
  done
else
  rotate_one "$PROVIDER"
fi

# --- Emit receipt (Law #2: No Action Without a Receipt) ---
RECEIPT_JSON=$(python3 -c "
import json
print(json.dumps({
    'receipt_id': 'emergency-rotate-${PROVIDER}-${TIMESTAMP}',
    'suite_id': 'ffffffff-0000-0000-0000-system000000',
    'office_id': 'ffffffff-0000-0000-0000-system000000',
    'tenant_id': 'system',
    'receipt_type': 'ops_emergency',
    'status': 'SUCCEEDED',
    'correlation_id': 'emergency-${PROVIDER}-${TIMESTAMP}',
    'actor_type': 'HUMAN',
    'actor_id': 'operator',
    'action': {
        'action_type': 'ops.emergency_rotation_trigger',
        'risk_tier': 'red',
        'provider': '${PROVIDER}',
        'environment': '${ENV}',
        'emergency': True
    },
    'created_at': '${TIMESTAMP}'
}))
")

if [ -n "${SUPABASE_URL:-}" ] && [ -n "${SUPABASE_SERVICE_ROLE_KEY:-}" ]; then
  curl -s -X POST "${SUPABASE_URL}/rest/v1/receipts" \
    -H "apikey: ${SUPABASE_SERVICE_ROLE_KEY}" \
    -H "Authorization: Bearer ${SUPABASE_SERVICE_ROLE_KEY}" \
    -H "Content-Type: application/json" \
    -H "Prefer: return=minimal" \
    -d "$RECEIPT_JSON" > /dev/null 2>&1
  echo -e "${GREEN}[receipt]${NC} Emergency rotation receipt emitted to Supabase"
else
  echo -e "${YELLOW}[receipt]${NC} Supabase not configured — receipt logged to stdout:"
  echo "$RECEIPT_JSON"
fi

echo ""
echo -e "${YELLOW}[info]${NC} Rotation triggered. Monitor progress:"
echo "  aws stepfunctions list-executions --state-machine-arn arn:aws:states:us-east-1:843479649294:stateMachine:aspire-secret-rotation-${ENV} --max-items 5"
echo ""
echo -e "${YELLOW}[info]${NC} Services will pick up new secrets within 5 min (cache TTL)."
echo "  To force immediate reload: restart Railway services"
echo "  Verify: curl https://www.aspireos.app/api/health"
