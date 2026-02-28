#!/bin/bash
# Fix: Recreate n8n container to pick up rotation env vars
#
# Problem: docker-compose.n8n.yml has rotation env vars (lines 62-67)
# but the container was created before those lines were added.
#
# Run from the infrastructure/docker directory:
#   cd infrastructure/docker
#   bash ../../scripts/fix_n8n_rotation_env.sh

set -e

echo "=== Recreating n8n container with rotation env vars ==="
echo ""
echo "This will:"
echo "  1. Stop and recreate the n8n container"
echo "  2. Preserve all data (volume-mounted)"
echo "  3. Add these missing env vars:"
echo "     - N8N_WORKFLOW_ROTATION_ORCHESTRATOR_ENABLED=true"
echo "     - N8N_WORKFLOW_ROTATION_MONITOR_ENABLED=true"
echo "     - ASPIRE_ROTATION_API_URL (default: http://host.docker.internal:8080)"
echo "     - AWS_ROTATION_TRIGGER_ACCESS_KEY_ID (from host env or empty)"
echo "     - AWS_ROTATION_TRIGGER_SECRET_ACCESS_KEY (from host env or empty)"
echo ""

cd "$(dirname "$0")/../infrastructure/docker"

echo "Working directory: $(pwd)"
echo ""

# Recreate n8n container only (not n8n-db)
docker compose -f docker-compose.n8n.yml up -d --force-recreate n8n

echo ""
echo "=== Waiting for n8n to start ==="
sleep 10

# Verify env vars
echo ""
echo "=== Verifying rotation env vars ==="
docker exec docker-n8n-1 printenv | grep -E "ROTATION|AWS_ROTATION" || echo "WARNING: No rotation env vars found!"

echo ""
echo "=== Checking n8n API health ==="
curl -s -o /dev/null -w "HTTP %{http_code}" http://localhost:5678/api/v1/workflows -H "X-N8N-API-KEY: $(cat ../../.mcp.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["mcpServers"]["n8n-mcp"]["env"]["N8N_API_KEY"])')" && echo " - n8n API is healthy" || echo " - n8n API not reachable yet (may need more time)"

echo ""
echo "=== Done. Re-run test_rotation_pipeline.py to verify ==="
echo "  python scripts/test_rotation_pipeline.py"
