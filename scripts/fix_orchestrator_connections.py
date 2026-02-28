"""Fix Loop Over Jobs connections in orchestrator workflow.

The MCP tool created connections with type '0' instead of 'main',
and the removeNode operation shifted the main array. This script
fixes the connections via direct n8n API PUT.
"""
import json
import urllib.request
import urllib.error

with open(r'C:\Users\tonio\Projects\myapp\.mcp.json', 'r') as f:
    API_KEY = json.load(f)['mcpServers']['n8n-mcp']['env']['N8N_API_KEY']

HEADERS = {'X-N8N-API-KEY': API_KEY, 'Content-Type': 'application/json'}
WF_ID = 'Jyewljst0Znk1mBS'


def api(method, path, data=None):
    url = f'http://localhost:5678{path}'
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return {'error': e.read().decode()[:300], 'code': e.code}, e.code


# Deactivate
api('POST', f'/api/v1/workflows/{WF_ID}/deactivate')
print("Deactivated")

# Get current
wf, status = api('GET', f'/api/v1/workflows/{WF_ID}')
if status != 200:
    print(f"ERROR: {wf}")
    exit(1)

# Fix connections — replace entire connections object
wf['connections'] = {
    "Daily 2am UTC": {
        "main": [[{"node": "Kill Switch + Prep", "type": "main", "index": 0}]]
    },
    "Kill Switch + Prep": {
        "main": [[{"node": "Kill Switch Active?", "type": "main", "index": 0}]]
    },
    "Kill Switch Active?": {
        "main": [
            [{"node": "Emit Kill Switch Receipt", "type": "main", "index": 0}],
            [{"node": "Build Rotation Jobs", "type": "main", "index": 0}]
        ]
    },
    "Build Rotation Jobs": {
        "main": [[{"node": "Loop Over Jobs", "type": "main", "index": 0}]]
    },
    "Loop Over Jobs": {
        "main": [
            [{"node": "Emit Batch Complete Receipt", "type": "main", "index": 0}],
            [{"node": "Prepare Rotation Request", "type": "main", "index": 0}]
        ]
    },
    "Prepare Rotation Request": {
        "main": [[{"node": "POST Rotate \u2014 Rotation API", "type": "main", "index": 0}]]
    },
    "POST Rotate \u2014 Rotation API": {
        "main": [[{"node": "Rotation Succeeded?", "type": "main", "index": 0}]]
    },
    "Rotation Succeeded?": {
        "main": [
            [{"node": "Emit Rotation Success Receipt", "type": "main", "index": 0}],
            [{"node": "Alert Rotation Failure \u2014 Gateway", "type": "main", "index": 0}]
        ]
    },
    "Emit Rotation Success Receipt": {
        "main": [[{"node": "Loop Over Jobs", "type": "main", "index": 0}]]
    },
    "Alert Rotation Failure \u2014 Gateway": {
        "main": [[{"node": "Emit Rotation Failure Receipt", "type": "main", "index": 0}]]
    },
    "Emit Rotation Failure Receipt": {
        "main": [[{"node": "Loop Over Jobs", "type": "main", "index": 0}]]
    },
    "Error Trigger": {
        "main": [[{"node": "Emit Error Receipt", "type": "main", "index": 0}]]
    },
    "E2E Test Trigger": {
        "main": [[{"node": "Kill Switch + Prep", "type": "main", "index": 0}]]
    }
}

# Strip to allowed PUT fields (n8n rejects additional properties)
allowed = {'name', 'nodes', 'connections', 'settings'}
payload = {k: v for k, v in wf.items() if k in allowed}

result, status = api('PUT', f'/api/v1/workflows/{WF_ID}', payload)
if status == 200:
    # Verify Loop Over Jobs connections
    for src, conns in result.get('connections', {}).items():
        if src == 'Loop Over Jobs':
            print(f"\nLoop Over Jobs connections:")
            for conn_type, outputs in conns.items():
                for idx, targets in enumerate(outputs):
                    for t in targets:
                        print(f"  {conn_type}[{idx}] -> {t['node']} (type={t['type']})")
    print(f"\nConnections fixed successfully")
else:
    print(f"ERROR: {status} - {json.dumps(result)[:400]}")
    exit(1)

# Reactivate
api('POST', f'/api/v1/workflows/{WF_ID}/activate')
print("Reactivated")
