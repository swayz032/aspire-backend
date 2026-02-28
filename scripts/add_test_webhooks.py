"""Add temporary E2E test webhook triggers to rotation workflows.

Adds webhook nodes via the n8n API (not MCP) to ensure webhookId
is preserved, which is required for production webhook registration.
"""
import json
import time
import urllib.request
import urllib.error

with open(r'C:\Users\tonio\Projects\myapp\.mcp.json', 'r') as f:
    API_KEY = json.load(f)['mcpServers']['n8n-mcp']['env']['N8N_API_KEY']

HEADERS = {'X-N8N-API-KEY': API_KEY, 'Content-Type': 'application/json'}

WORKFLOWS = [
    {
        'id': 'Jyewljst0Znk1mBS',
        'name': 'Orchestrator',
        'webhook_path': 'e2e-test-orch',
        'target_node': 'Kill Switch + Prep',
    },
    {
        'id': 'uI4JbtvTA4Vo8Rg4',
        'name': 'Monitor',
        'webhook_path': 'e2e-test-mon',
        'target_node': 'Kill Switch + Prep',
    },
]


def api(method, path, data=None):
    url = f'http://localhost:5678{path}'
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return {'error': e.read().decode()[:300], 'code': e.code}, e.code


for wf_cfg in WORKFLOWS:
    wf_id = wf_cfg['id']
    print(f"\n{'='*50}")
    print(f"Adding webhook to {wf_cfg['name']} ({wf_id})")
    print(f"{'='*50}")

    # Deactivate first so we can edit
    api('POST', f'/api/v1/workflows/{wf_id}/deactivate')
    print("  Deactivated")

    # Get current workflow
    wf, status = api('GET', f'/api/v1/workflows/{wf_id}')
    if status != 200:
        print(f"  ERROR getting workflow: {wf}")
        continue

    # Remove any existing test webhook node
    wf['nodes'] = [n for n in wf['nodes'] if n.get('name') != 'E2E Test Trigger']
    if 'E2E Test Trigger' in wf.get('connections', {}):
        del wf['connections']['E2E Test Trigger']

    # Add webhook node with webhookId
    wf['nodes'].append({
        'id': f'e2e-trigger-{wf_cfg["webhook_path"]}',
        'name': 'E2E Test Trigger',
        'type': 'n8n-nodes-base.webhook',
        'typeVersion': 2,
        'position': [0, 500],
        'webhookId': wf_cfg['webhook_path'],
        'parameters': {
            'path': wf_cfg['webhook_path'],
            'httpMethod': 'POST',
            'responseMode': 'lastNode',
            'options': {},
        },
    })

    # Add connection
    wf['connections']['E2E Test Trigger'] = {
        'main': [[{
            'node': wf_cfg['target_node'],
            'type': 'main',
            'index': 0,
        }]]
    }

    # n8n PUT only accepts: name, nodes, connections, settings, staticData, pinData, active
    allowed = {'name', 'nodes', 'connections', 'settings', 'staticData', 'pinData', 'active'}
    wf = {k: v for k, v in wf.items() if k in allowed}

    # Update via PUT
    result, status = api('PUT', f'/api/v1/workflows/{wf_id}', wf)
    if status == 200:
        # Verify webhookId preserved
        for n in result.get('nodes', []):
            if n.get('name') == 'E2E Test Trigger':
                wh_id = n.get('webhookId', 'NOT SET')
                print(f"  Webhook node added: webhookId={wh_id}")
    else:
        print(f"  ERROR updating: {status} - {json.dumps(result)[:300]}")
        continue

    # Reactivate
    api('POST', f'/api/v1/workflows/{wf_id}/activate')
    print("  Reactivated")

# Wait for webhook registration
print("\nWaiting 4s for webhook registration...")
time.sleep(4)

# Verify webhooks are registered
print("\nVerifying webhook registration:")
for wf_cfg in WORKFLOWS:
    path = wf_cfg['webhook_path']
    url = f'http://localhost:5678/webhook/{path}'
    req = urllib.request.Request(url, data=b'{"probe":true}',
                                headers={'Content-Type': 'application/json'},
                                method='POST')
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            print(f"  {path}: HTTP {r.status} - REGISTERED AND RESPONDING")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  {path}: HTTP 404 - NOT REGISTERED")
        else:
            body = e.read().decode()[:100]
            print(f"  {path}: HTTP {e.code} - {body}")
    except Exception as e:
        print(f"  {path}: timeout/error (workflow may be executing) - {e}")

print("\nDone. Ready for E2E test.")
