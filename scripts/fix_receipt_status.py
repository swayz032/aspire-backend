"""Fix Kill Switch receipt nodes — change SKIPPED to DENIED.

The receipts table CHECK constraint only allows: PENDING, SUCCEEDED, FAILED, DENIED.
Kill Switch nodes use SKIPPED which violates the constraint.
DENIED is semantically correct — the kill switch denies rotation execution.

Also fixes both Orchestrator and Monitor Kill Switch receipt nodes.
"""
import json
import urllib.request
import urllib.error

with open('/mnt/c/Users/tonio/Projects/myapp/.mcp.json', 'r') as f:
    API_KEY = json.load(f)['mcpServers']['n8n-mcp']['env']['N8N_API_KEY']

HEADERS = {'X-N8N-API-KEY': API_KEY, 'Content-Type': 'application/json'}

WORKFLOWS = {
    'Jyewljst0Znk1mBS': 'Orchestrator',
    'uI4JbtvTA4Vo8Rg4': 'Monitor',
}


def api(method, path, data=None):
    url = 'http://localhost:5678{}'.format(path)
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return {'error': e.read().decode()[:300], 'code': e.code}, e.code


for wf_id, label in WORKFLOWS.items():
    print("=== {} ({}) ===".format(label, wf_id))

    # Deactivate
    api('POST', '/api/v1/workflows/{}/deactivate'.format(wf_id))

    # Get workflow
    wf, status = api('GET', '/api/v1/workflows/{}'.format(wf_id))
    if status != 200:
        print("  ERROR: {}".format(wf))
        continue

    fixed = 0
    for node in wf.get('nodes', []):
        name = node.get('name', '')
        params = node.get('parameters', {})
        json_body = params.get('jsonBody', '')

        if 'SKIPPED' in json_body:
            params['jsonBody'] = json_body.replace("'SKIPPED'", "'DENIED'")
            fixed += 1
            print("  FIXED: {} — SKIPPED → DENIED".format(name))

    if fixed > 0:
        allowed = {'name', 'nodes', 'connections', 'settings'}
        payload = {k: v for k, v in wf.items() if k in allowed}
        result, status = api('PUT', '/api/v1/workflows/{}'.format(wf_id), payload)
        if status == 200:
            print("  Updated successfully ({} nodes fixed)".format(fixed))
        else:
            print("  ERROR updating: {} - {}".format(status, json.dumps(result)[:300]))
    else:
        print("  No SKIPPED status found")

    # Reactivate
    api('POST', '/api/v1/workflows/{}/activate'.format(wf_id))
    print("  Reactivated")
    print()

print("Done.")
