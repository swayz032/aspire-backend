#!/usr/bin/env bash
# TypeForge: Generate TypeScript types from backend OpenAPI spec + Supabase schema
# Usage: bash tools/typegen/generate-types.sh [--check]
# With --check: exits 1 if generated types differ from committed (CI drift detection)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCHEMAS_DIR="$PROJECT_ROOT/schemas"
BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"
SUPABASE_PROJECT_ID="${SUPABASE_PROJECT_ID:-qtuehjqlcmfcascqjjhc}"
CHECK_MODE="${1:-}"

mkdir -p "$SCHEMAS_DIR"

echo "TypeForge: Generating types..."
echo ""

# 1. Fetch OpenAPI spec from backend
echo "  [1/3] Fetching OpenAPI spec from $BACKEND_URL/openapi.json..."
if curl -sf "$BACKEND_URL/openapi.json" -o "$SCHEMAS_DIR/openapi.json" 2>/dev/null; then
    echo "  OK    OpenAPI spec saved to schemas/openapi.json"
else
    echo "  WARN  Backend not running — using cached schemas/openapi.json"
    if [ ! -f "$SCHEMAS_DIR/openapi.json" ]; then
        echo "  ERR   No cached OpenAPI spec found. Start backend first."
        exit 1
    fi
fi

# 2. Generate API types from OpenAPI
echo "  [2/3] Generating API types from OpenAPI spec..."
DESKTOP_DIR="$PROJECT_ROOT/Aspire-desktop"
ADMIN_DIR="$PROJECT_ROOT/import-my-portal-main"

# Check if openapi-typescript is installed
if [ -d "$DESKTOP_DIR" ] && [ -f "$DESKTOP_DIR/package.json" ]; then
    cd "$DESKTOP_DIR"
    if npx openapi-typescript "$SCHEMAS_DIR/openapi.json" -o "$SCHEMAS_DIR/api-types.generated.ts" 2>/dev/null; then
        echo "  OK    API types generated: schemas/api-types.generated.ts"
    else
        echo "  WARN  openapi-typescript not installed. Run: npm i -D openapi-typescript"
    fi
    cd "$PROJECT_ROOT"
fi

# 3. Generate Supabase types
echo "  [3/3] Generating Supabase types..."
if command -v supabase &>/dev/null; then
    if supabase gen types typescript --project-id "$SUPABASE_PROJECT_ID" > "$SCHEMAS_DIR/supabase-types.generated.ts" 2>/dev/null; then
        echo "  OK    Supabase types generated: schemas/supabase-types.generated.ts"
    else
        echo "  WARN  Supabase CLI auth issue — using cached types"
    fi
else
    echo "  WARN  Supabase CLI not installed. Run: npm i -g supabase"
fi

echo ""

# Check mode: compare generated types against committed
if [ "$CHECK_MODE" = "--check" ]; then
    echo "TypeForge: Drift detection..."
    DRIFT=0

    if [ -f "$SCHEMAS_DIR/api-types.generated.ts" ]; then
        if ! git diff --quiet "$SCHEMAS_DIR/api-types.generated.ts" 2>/dev/null; then
            echo "  DRIFT  api-types.generated.ts differs from committed version"
            DRIFT=1
        else
            echo "  OK     api-types.generated.ts matches committed"
        fi
    fi

    if [ -f "$SCHEMAS_DIR/supabase-types.generated.ts" ]; then
        if ! git diff --quiet "$SCHEMAS_DIR/supabase-types.generated.ts" 2>/dev/null; then
            echo "  DRIFT  supabase-types.generated.ts differs from committed version"
            DRIFT=1
        else
            echo "  OK     supabase-types.generated.ts matches committed"
        fi
    fi

    if [ "$DRIFT" -eq 1 ]; then
        echo ""
        echo "ERR: Type drift detected! Run 'bash tools/typegen/generate-types.sh' and commit."
        exit 1
    fi

    echo ""
    echo "TypeForge: No drift detected."
fi

echo "TypeForge: Done."
