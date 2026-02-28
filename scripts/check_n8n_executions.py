"""Check n8n execution details for rotation workflows."""
import io
import json
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

with open(r'C:\Users\tonio\Projects\myapp\.mcp.json', 'r') as f:
    API_KEY = json.load(f)['mcpServers']['n8n-mcp']['env']['N8N_API_KEY']

headers = {'X-N8N-API-KEY': API_KEY}

for exec_id in ['1669', '1670', '1671']:
    req = urllib.request.Request(
        f'http://localhost:5678/api/v1/executions/{exec_id}?includeData=true',
        headers=headers,
    )
    with urllib.request.urlopen(req) as r:
        e = json.loads(r.read())

    status = e.get('status', '?')
    wf_id = e.get('workflowId', '?')
    wf_names = {'Jyewljst0Znk1mBS': 'ORCHESTRATOR', 'uI4JbtvTA4Vo8Rg4': 'MONITOR'}
    wf_label = wf_names.get(wf_id, wf_id)

    print(f'\n{"="*70}')
    print(f'Execution {exec_id} ({status}) - {wf_label}')
    print(f'{"="*70}')

    result_data = e.get('data', {}).get('resultData', {})

    if result_data.get('error'):
        err = result_data['error']
        print(f'  TOP ERROR: {err.get("message", str(err))[:400]}')

    run_data = result_data.get('runData', {})
    for node_name, node_runs in run_data.items():
        for i, run in enumerate(node_runs):
            if run.get('error'):
                err = run['error']
                msg = err.get('message', str(err))[:300]
                print(f'  [ERROR] {node_name}: {msg}')
            else:
                output_data = run.get('data', {}).get('main', [[]])
                for branch_idx, branch in enumerate(output_data):
                    if branch:
                        j = branch[0].get('json', {})
                        summary = json.dumps(j, ensure_ascii=False)[:200]
                        if len(output_data) > 1:
                            print(f'  [OK   ] {node_name} [branch {branch_idx}]: {summary}')
                        else:
                            print(f'  [OK   ] {node_name}: {summary}')
                    elif len(output_data) > 1:
                        print(f'  [EMPTY] {node_name} [branch {branch_idx}]: no items')
