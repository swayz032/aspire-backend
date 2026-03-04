#!/bin/bash
set -e

CONN="${ASPIRE_SUPABASE_DB_URL:-${DATABASE_URL:-}}"
if [ -z "$CONN" ]; then
  echo "ERROR: Missing database URL. Set ASPIRE_SUPABASE_DB_URL (preferred) or DATABASE_URL."
  exit 1
fi

echo "======================================================================"
echo "APPLYING CONVERSATIONAL INTELLIGENCE MIGRATIONS (066, 067, 068)"
echo "======================================================================"
echo

echo "📄 Applying 066_general_knowledge_base.sql..."
psql "$CONN" -f /mnt/c/Users/tonio/Projects/myapp/backend/infrastructure/supabase/migrations/066_general_knowledge_base.sql > /dev/null 2>&1 && echo "✅ 066 applied" || echo "❌ 066 failed"

echo "📄 Applying 067_communication_knowledge_base.sql..."
psql "$CONN" -f /mnt/c/Users/tonio/Projects/myapp/backend/infrastructure/supabase/migrations/067_communication_knowledge_base.sql > /dev/null 2>&1 && echo "✅ 067 applied" || echo "❌ 067 failed"

echo "📄 Applying 068_agent_memory.sql..."
psql "$CONN" -f /mnt/c/Users/tonio/Projects/myapp/backend/infrastructure/supabase/migrations/068_agent_memory.sql > /dev/null 2>&1 && echo "✅ 068 applied" || echo "❌ 068 failed"

echo
echo "======================================================================"
echo "VERIFYING TABLES"
echo "======================================================================"
psql "$CONN" -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name LIKE '%knowledge%' OR table_name LIKE '%agent%' ORDER BY table_name;" -t

echo
echo "✅ ALL MIGRATIONS APPLIED"
