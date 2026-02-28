"""Verify receipt persistence — trigger orchestrator E2E test and check Supabase.

1. Trigger orchestrator via E2E webhook
2. Wait for execution to complete
3. Check Supabase receipts table for new rotation receipts
"""
import json
import urllib.request
import urllib.error
import time

with open('/mnt/c/Users/tonio/Projects/myapp/.mcp.json', 'r') as f:
    API_KEY = json.load(f)['mcpServers']['n8n-mcp']['env']['N8N_API_KEY']

HEADERS = {'X-N8N-API-KEY': API_KEY, 'Content-Type': 'application/json'}
ORCH_WF_ID = 'Jyewljst0Znk1mBS'


def api(method, path, data=None, headers=None):
    url = 'http://localhost:5678{}'.format(path)
    body = json.dumps(data).encode() if data else None
    h = headers or HEADERS
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return {'error': e.read().decode()[:500], 'code': e.code}, e.code


def supabase_query(table, query_params=''):
    """Query Supabase PostgREST."""
    import os
    # Read from docker .env
    env = {}
    with open('/mnt/c/Users/tonio/Projects/myapp/infrastructure/docker/.env', 'r') as f:
        for line in f:
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()

    url = '{}/rest/v1/{}?{}'.format(env['SUPABASE_URL'], table, query_params)
    headers = {
        'apikey': env['SUPABASE_SERVICE_ROLE_KEY'],
        'Authorization': 'Bearer {}'.format(env['SUPABASE_SERVICE_ROLE_KEY']),
        'Content-Type': 'application/json',
    }
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return {'error': e.read().decode()[:500]}, e.code


# Step 1: Check current receipt count for rotation receipts
print("Step 1: Checking existing rotation receipts in Supabase...")
receipts, status = supabase_query(
    'receipts',
    'select=id,action_type,outcome,created_at&action_type=like.secret.rotation*&order=created_at.desc&limit=5'
)
if status == 200:
    print("  Found {} existing rotation receipts".format(len(receipts)))
    for r in receipts:
        print("    {} — {} — {}".format(r.get('action_type', '?'), r.get('outcome', '?'), r.get('created_at', '?')))
else:
    print("  Query failed: {}".format(receipts))
    # Check if receipts table exists with a simpler query
    receipts2, status2 = supabase_query('receipts', 'select=id&limit=1')
    print("  Simple query: status={}, result={}".format(status2, receipts2))

# Step 2: Get latest n8n execution before triggering
print("\nStep 2: Getting latest execution ID...")
execs, _ = api('GET', '/api/v1/executions?workflowId={}&limit=1'.format(ORCH_WF_ID))
last_id = execs.get('data', [{}])[0].get('id', 0) if execs.get('data') else 0
print("  Latest execution ID: {}".format(last_id))

# Step 3: Trigger E2E test
print("\nStep 3: Triggering E2E test via webhook...")
trigger_url = 'http://localhost:5678/webhook-test/rotation-e2e-test'
trigger_data = {'test': True, 'source': 'verify_receipt_persistence'}
req = urllib.request.Request(
    trigger_url,
    data=json.dumps(trigger_data).encode(),
    headers={'Content-Type': 'application/json'},
    method='POST'
)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read())
        print("  Trigger response: {}".format(json.dumps(resp)[:200]))
except urllib.error.HTTPError as e:
    print("  Trigger failed: {} - {}".format(e.code, e.read().decode()[:200]))
    # Try production webhook path
    trigger_url2 = 'http://localhost:5678/webhook/rotation-e2e-test'
    req2 = urllib.request.Request(
        trigger_url2,
        data=json.dumps(trigger_data).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req2, timeout=15) as r2:
            resp = json.loads(r2.read())
            print("  Production webhook response: {}".format(json.dumps(resp)[:200]))
    except urllib.error.HTTPError as e2:
        print("  Production webhook also failed: {} - {}".format(e2.code, e2.read().decode()[:200]))
except Exception as e:
    print("  Trigger error: {}".format(e))

# Step 4: Wait for execution to complete
print("\nStep 4: Waiting for new execution to appear (up to 120s)...")
new_exec_id = None
for i in range(24):
    time.sleep(5)
    execs, _ = api('GET', '/api/v1/executions?workflowId={}&limit=1'.format(ORCH_WF_ID))
    if execs.get('data'):
        latest = execs['data'][0]
        latest_id = latest.get('id', 0)
        latest_status = latest.get('status', '?')
        if latest_id != last_id:
            new_exec_id = latest_id
            if latest_status in ('success', 'error', 'crashed'):
                print("  Execution {} completed: {}".format(new_exec_id, latest_status))
                break
            else:
                print("  Execution {} running... ({}s)".format(new_exec_id, (i+1)*5))
        else:
            print("  No new execution yet... ({}s)".format((i+1)*5))

if not new_exec_id:
    print("  TIMEOUT — no new execution appeared")
    print("  Checking last few executions manually...")
    execs, _ = api('GET', '/api/v1/executions?workflowId={}&limit=3'.format(ORCH_WF_ID))
    for e in execs.get('data', []):
        print("    id={} status={} startedAt={}".format(e.get('id'), e.get('status'), e.get('startedAt', '')[:19]))

# Step 5: Check Supabase for new receipts
print("\nStep 5: Checking Supabase for new rotation receipts...")
time.sleep(3)  # small buffer for async receipt writes
receipts_after, status = supabase_query(
    'receipts',
    'select=id,action_type,outcome,created_at,correlation_id&action_type=like.secret.rotation*&order=created_at.desc&limit=10'
)
if status == 200:
    print("  Found {} rotation receipts total".format(len(receipts_after)))
    for r in receipts_after:
        print("    {} — {} — {} — corr={}".format(
            r.get('action_type', '?'),
            r.get('outcome', '?'),
            r.get('created_at', '?')[:19],
            r.get('correlation_id', '?')[:20] if r.get('correlation_id') else '?'
        ))
else:
    print("  Query failed: {}".format(receipts_after))

# Also check for batch_complete receipts (different action_type)
receipts_batch, status2 = supabase_query(
    'receipts',
    'select=id,action_type,outcome,created_at&action_type=like.%25rotation%25&order=created_at.desc&limit=10'
)
if status2 == 200 and len(receipts_batch) > len(receipts_after):
    print("\n  Additional rotation-related receipts:")
    for r in receipts_batch:
        if r not in receipts_after:
            print("    {} — {} — {}".format(r.get('action_type', '?'), r.get('outcome', '?'), r.get('created_at', '?')[:19]))

print("\nDone.")
