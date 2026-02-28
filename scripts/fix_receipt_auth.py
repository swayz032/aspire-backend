"""Fix n8n receipt emission nodes — add Supabase auth headers.

Problem: All receipt emission HTTP Request nodes POST to Supabase PostgREST
but have no authentication credentials. Supabase RLS silently rejects the
inserts, returning 200 with empty array. Receipts are NOT being persisted.

Fix: Add apikey + Authorization headers using n8n env var expressions:
  - apikey: {{ $env.SUPABASE_SERVICE_ROLE_KEY }}
  - Authorization: Bearer {{ $env.SUPABASE_SERVICE_ROLE_KEY }}
  - Prefer: return=representation (so we get the inserted row back as confirmation)
"""
import json
import urllib.request
import urllib.error

with open('/mnt/c/Users/tonio/Projects/myapp/.mcp.json', 'r') as f:
    API_KEY = json.load(f)['mcpServers']['n8n-mcp']['env']['N8N_API_KEY']

HEADERS = {'X-N8N-API-KEY': API_KEY, 'Content-Type': 'application/json'}

ORCHESTRATOR_WF_ID = 'Jyewljst0Znk1mBS'
MONITOR_WF_ID = 'uI4JbtvTA4Vo8Rg4'

# Receipt node names to fix (any node with "Emit" and "Receipt" in name)
RECEIPT_NODE_PATTERN = lambda name: 'Emit' in name and 'Receipt' in name


def api(method, path, data=None):
    url = 'http://localhost:5678{}'.format(path)
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return {'error': e.read().decode()[:300], 'code': e.code}, e.code


def fix_receipt_nodes(wf_id, wf_label):
    """Fix all receipt emission nodes in a workflow."""
    print("\n=== Fixing {} ({}) ===".format(wf_label, wf_id))

    # Deactivate
    api('POST', '/api/v1/workflows/{}/deactivate'.format(wf_id))
    print("Deactivated")

    # Get current workflow
    wf, status = api('GET', '/api/v1/workflows/{}'.format(wf_id))
    if status != 200:
        print("ERROR fetching workflow: {}".format(wf))
        return False

    fixed_count = 0
    for node in wf.get('nodes', []):
        name = node.get('name', '')
        node_type = node.get('type', '')

        if not RECEIPT_NODE_PATTERN(name):
            continue

        if node_type != 'n8n-nodes-base.httpRequest':
            print("  SKIP {} (type={}, not httpRequest)".format(name, node_type))
            continue

        params = node.get('parameters', {})

        # Add Supabase auth headers
        # n8n httpRequest node uses sendHeaders + headerParameters for custom headers
        params['sendHeaders'] = True
        params['headerParameters'] = {
            'parameters': [
                {
                    'name': 'apikey',
                    'value': '={{ $env.SUPABASE_SERVICE_ROLE_KEY }}'
                },
                {
                    'name': 'Authorization',
                    'value': '=Bearer {{ $env.SUPABASE_SERVICE_ROLE_KEY }}'
                },
                {
                    'name': 'Prefer',
                    'value': 'return=representation'
                }
            ]
        }

        # Remove empty credentials field that might interfere
        if 'credentials' in node and not node['credentials']:
            del node['credentials']

        fixed_count += 1
        print("  FIXED: {} — added apikey + Authorization + Prefer headers".format(name))

    if fixed_count == 0:
        print("  No receipt nodes found to fix!")
        api('POST', '/api/v1/workflows/{}/activate'.format(wf_id))
        return False

    # Strip to allowed PUT fields
    allowed = {'name', 'nodes', 'connections', 'settings'}
    payload = {k: v for k, v in wf.items() if k in allowed}

    result, status = api('PUT', '/api/v1/workflows/{}'.format(wf_id), payload)
    if status == 200:
        # Verify the fix
        print("\n  Verification — checking receipt nodes in updated workflow:")
        for node in result.get('nodes', []):
            name = node.get('name', '')
            if RECEIPT_NODE_PATTERN(name):
                params = node.get('parameters', {})
                has_headers = params.get('sendHeaders', False)
                header_params = params.get('headerParameters', {}).get('parameters', [])
                header_names = [h.get('name', '') for h in header_params]
                print("    {} — sendHeaders={}, headers={}".format(
                    name, has_headers, header_names
                ))

        print("\n  {} receipt nodes fixed successfully".format(fixed_count))
    else:
        print("  ERROR updating workflow: {} - {}".format(status, json.dumps(result)[:400]))
        api('POST', '/api/v1/workflows/{}/activate'.format(wf_id))
        return False

    # Reactivate
    api('POST', '/api/v1/workflows/{}/activate'.format(wf_id))
    print("  Reactivated")
    return True


# Fix both workflows
ok1 = fix_receipt_nodes(ORCHESTRATOR_WF_ID, "Orchestrator")
ok2 = fix_receipt_nodes(MONITOR_WF_ID, "Monitor")

print("\n" + "=" * 50)
if ok1 and ok2:
    print("ALL DONE — both workflows fixed")
elif ok1:
    print("Orchestrator fixed, Monitor had no receipt nodes (or failed)")
elif ok2:
    print("Monitor fixed, Orchestrator failed")
else:
    print("BOTH FAILED — check errors above")
