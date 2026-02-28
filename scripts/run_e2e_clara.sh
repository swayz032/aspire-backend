#!/bin/bash
# Run E2E Clara NDA test with orchestrator
set -e

cd /mnt/c/Users/tonio/Projects/myapp/backend/orchestrator
source ~/venvs/aspire/bin/activate

# Start orchestrator in background
echo "Starting orchestrator..."
python -m uvicorn aspire_orchestrator.server:app --host 0.0.0.0 --port 8000 --log-level info > /tmp/orchestrator_e2e.log 2>&1 &
ORCH_PID=$!
echo "Orchestrator PID: $ORCH_PID"

# Wait for startup
for i in 1 2 3 4 5 6 7 8; do
    sleep 1
    if curl -s http://localhost:8000/healthz > /dev/null 2>&1; then
        echo "Orchestrator is healthy!"
        break
    fi
    echo "Waiting... ($i)"
done

# Verify it's up
if ! curl -s http://localhost:8000/healthz > /dev/null 2>&1; then
    echo "FAILED: Orchestrator didn't start"
    cat /tmp/orchestrator_e2e.log
    kill $ORCH_PID 2>/dev/null
    exit 1
fi

# Run the E2E test
echo ""
echo "========================================="
echo "Running E2E Clara NDA Test"
echo "========================================="
echo ""

cd /mnt/c/Users/tonio/Projects/myapp
python scripts/e2e_clara_nda.py 2>&1
TEST_EXIT=$?

# Show orchestrator logs
echo ""
echo "========================================="
echo "Orchestrator Logs"
echo "========================================="
cat /tmp/orchestrator_e2e.log

# Cleanup
kill $ORCH_PID 2>/dev/null
wait $ORCH_PID 2>/dev/null

exit $TEST_EXIT
