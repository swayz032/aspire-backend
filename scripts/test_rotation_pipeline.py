#!/usr/bin/env python3
"""
Rotation Pipeline E2E Smoke Test

Tests the n8n -> Step Functions -> Lambda rotation pipeline by:
1. Validating production workflows are active and healthy
2. Creating temporary webhook-triggered test workflows
3. Executing smoke tests for orchestrator and monitor patterns
4. Verifying env var accessibility
5. Cleaning up temporary workflows
6. Generating a structured report

Usage: python scripts/test_rotation_pipeline.py
"""

import sys
import io
import json
import time
import hashlib
import urllib.request
import urllib.error
import re
from datetime import datetime

# Fix Windows encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ============================================================
# Configuration
# ============================================================
N8N_BASE = 'http://localhost:5678'

# Read API key from .mcp.json
with open('C:\\Users\\tonio\\Projects\\myapp\\.mcp.json', 'r') as f:
    mcp_config = json.load(f)
API_KEY = mcp_config['mcpServers']['n8n-mcp']['env']['N8N_API_KEY']

ORCHESTRATOR_ID = 'Jyewljst0Znk1mBS'
MONITOR_ID = 'uI4JbtvTA4Vo8Rg4'

# Track created test workflows for cleanup
test_workflow_ids = []
results = []

def api_request(path, method='GET', data=None):
    """Make an n8n API request."""
    url = f'{N8N_BASE}{path}'
    headers = {'X-N8N-API-KEY': API_KEY, 'Content-Type': 'application/json'}
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        return {'error': body, 'status_code': e.code}, e.code
    except Exception as e:
        return {'error': str(e)}, 0

def record(test_name, passed, detail=''):
    """Record a test result."""
    status = 'PASS' if passed else 'FAIL'
    results.append({'test': test_name, 'status': status, 'detail': detail})
    icon = '[PASS]' if passed else '[FAIL]'
    print(f'  {icon} {test_name}: {detail}')

# ============================================================
# PHASE 1: Validate Production Workflows
# ============================================================
print('=' * 70)
print('PHASE 1: Validate Production Rotation Workflows')
print('=' * 70)

# Check orchestrator
orch_wf, status = api_request(f'/api/v1/workflows/{ORCHESTRATOR_ID}')
if status == 200:
    record('orch_exists', True, f'Found: {orch_wf.get("name", "?")}')
    record('orch_active', orch_wf.get('active', False), f'active={orch_wf.get("active")}')

    nodes = orch_wf.get('nodes', [])
    record('orch_node_count', len(nodes) == 16, f'{len(nodes)} nodes (expected 16)')

    # Check critical node types
    node_types = [n['type'].split('.')[-1] for n in nodes]
    has_schedule = 'scheduleTrigger' in node_types
    has_code = node_types.count('code') >= 3
    has_if = node_types.count('if') >= 2
    has_http = node_types.count('httpRequest') >= 7
    has_loop = 'splitInBatches' in node_types
    has_wait = 'wait' in node_types
    has_error = 'errorTrigger' in node_types
    record('orch_has_schedule', has_schedule, 'scheduleTrigger present')
    record('orch_has_code_nodes', has_code, f'{node_types.count("code")} code nodes')
    record('orch_has_if_nodes', has_if, f'{node_types.count("if")} IF nodes')
    record('orch_has_http_nodes', has_http, f'{node_types.count("httpRequest")} HTTP nodes')
    record('orch_has_loop', has_loop, 'splitInBatches present')
    record('orch_has_wait', has_wait, 'Wait Jitter present')
    record('orch_has_error_trigger', has_error, 'errorTrigger present')

    # Check env var references
    raw = json.dumps(orch_wf)
    env_refs = set(re.findall(r'\$env\.([A-Za-z_][A-Za-z0-9_]*)', raw))
    expected_env = {'N8N_WORKFLOW_ROTATION_ORCHESTRATOR_ENABLED', 'ASPIRE_ROTATION_API_URL',
                    'AWS_ROTATION_TRIGGER_ACCESS_KEY_ID', 'AWS_ROTATION_TRIGGER_SECRET_ACCESS_KEY',
                    'ASPIRE_GATEWAY_URL', 'SUPABASE_URL', 'DEFAULT_SUITE_ID', 'DEFAULT_OFFICE_ID',
                    'SUPABASE_SERVICE_ROLE_KEY'}
    missing = expected_env - env_refs
    record('orch_env_vars', len(missing) == 0, f'missing: {missing}' if missing else f'all {len(expected_env)} env vars referenced')

    # Check IF nodes use string conditions
    if_nodes = [n for n in nodes if n['type'] == 'n8n-nodes-base.if']
    all_string = all('string' in json.dumps(n.get('parameters', {}).get('conditions', {})) for n in if_nodes)
    record('orch_if_string_conditions', all_string, 'all IF nodes use string conditions (not boolean)')

    # Check all HTTP nodes have timeouts
    http_nodes = [n for n in nodes if n['type'] == 'n8n-nodes-base.httpRequest']
    all_timeouts = all(n.get('parameters', {}).get('options', {}).get('timeout') for n in http_nodes)
    record('orch_http_timeouts', all_timeouts, 'all HTTP nodes have timeouts')

    # Check onError=continueRegularOutput on critical nodes
    all_continue = all(n.get('onError') == 'continueRegularOutput' for n in http_nodes)
    record('orch_http_onerror', all_continue, 'all HTTP nodes have onError=continueRegularOutput')

    # Check connections integrity
    connections = orch_wf.get('connections', {})
    all_targets = set()
    for src, outputs in connections.items():
        for branch in outputs.get('main', []):
            for c in branch:
                all_targets.add(c['node'])
    node_names = {n['name'] for n in nodes}
    orphans = all_targets - node_names
    record('orch_connections_valid', len(orphans) == 0, f'orphan targets: {orphans}' if orphans else 'all connections resolve')
else:
    record('orch_exists', False, f'HTTP {status}: {orch_wf}')

print()

# Check monitor
mon_wf, status = api_request(f'/api/v1/workflows/{MONITOR_ID}')
if status == 200:
    record('mon_exists', True, f'Found: {mon_wf.get("name", "?")}')
    record('mon_active', mon_wf.get('active', False), f'active={mon_wf.get("active")}')

    nodes = mon_wf.get('nodes', [])
    record('mon_node_count', len(nodes) == 13, f'{len(nodes)} nodes (expected 13)')

    node_types = [n['type'].split('.')[-1] for n in nodes]
    record('mon_has_schedule', 'scheduleTrigger' in node_types, 'scheduleTrigger present')
    record('mon_has_error_trigger', 'errorTrigger' in node_types, 'errorTrigger present')

    # Check env vars
    raw = json.dumps(mon_wf)
    env_refs = set(re.findall(r'\$env\.([A-Za-z_][A-Za-z0-9_]*)', raw))
    expected_env = {'N8N_WORKFLOW_ROTATION_MONITOR_ENABLED', 'ASPIRE_GATEWAY_URL',
                    'SUPABASE_URL', 'DEFAULT_SUITE_ID', 'DEFAULT_OFFICE_ID', 'SUPABASE_SERVICE_ROLE_KEY'}
    missing = expected_env - env_refs
    record('mon_env_vars', len(missing) == 0, f'missing: {missing}' if missing else f'all {len(expected_env)} env vars referenced')

    # Check IF nodes
    if_nodes = [n for n in nodes if n['type'] == 'n8n-nodes-base.if']
    all_string = all('string' in json.dumps(n.get('parameters', {}).get('conditions', {})) for n in if_nodes)
    record('mon_if_string_conditions', all_string, 'all IF nodes use string conditions')

    # Check HTTP timeouts
    http_nodes = [n for n in nodes if n['type'] == 'n8n-nodes-base.httpRequest']
    all_timeouts = all(n.get('parameters', {}).get('options', {}).get('timeout') for n in http_nodes)
    record('mon_http_timeouts', all_timeouts, 'all HTTP nodes have timeouts')

    # Check Gateway credential usage
    cred_nodes = [n for n in nodes if n.get('credentials', {}).get('httpHeaderAuth', {}).get('id') == 'J84m12hyzSwSHo6S']
    record('mon_gateway_creds', len(cred_nodes) >= 3, f'{len(cred_nodes)} nodes use Gateway credential')

    # Connections integrity
    connections = mon_wf.get('connections', {})
    all_targets = set()
    for src, outputs in connections.items():
        for branch in outputs.get('main', []):
            for c in branch:
                all_targets.add(c['node'])
    node_names = {n['name'] for n in nodes}
    orphans = all_targets - node_names
    record('mon_connections_valid', len(orphans) == 0, f'orphan targets: {orphans}' if orphans else 'all connections resolve')
else:
    record('mon_exists', False, f'HTTP {status}: {mon_wf}')

# ============================================================
# PHASE 2: Create Smoke Test Workflows
# ============================================================
print()
print('=' * 70)
print('PHASE 2: Create & Execute Smoke Test Workflows')
print('=' * 70)

# --- Test Workflow 1: Orchestrator Smoke ---
test_orch_def = {
    'name': 'SMOKE TEST - Rotation Orchestrator',
    'nodes': [
        {
            'id': 'webhook-trigger',
            'name': 'Webhook Trigger',
            'type': 'n8n-nodes-base.webhook',
            'typeVersion': 2,
            'position': [220, 300],
            'webhookId': 'test-rotation-orch',
            'parameters': {
                'path': 'test-rotation-orch',
                'httpMethod': 'POST',
                'responseMode': 'lastNode',
                'options': {}
            }
        },
        {
            'id': 'env-check',
            'name': 'Env Check + Kill Switch',
            'type': 'n8n-nodes-base.code',
            'typeVersion': 2,
            'position': [440, 300],
            'parameters': {
                'jsCode': (
                    "const results = {\n"
                    "  kill_switch_orch: $env.N8N_WORKFLOW_ROTATION_ORCHESTRATOR_ENABLED || 'NOT_SET',\n"
                    "  kill_switch_mon: $env.N8N_WORKFLOW_ROTATION_MONITOR_ENABLED || 'NOT_SET',\n"
                    "  rotation_api_url: $env.ASPIRE_ROTATION_API_URL || 'NOT_SET',\n"
                    "  aws_access_key: $env.AWS_ROTATION_TRIGGER_ACCESS_KEY_ID ? 'SET' : 'NOT_SET',\n"
                    "  aws_secret_key: $env.AWS_ROTATION_TRIGGER_SECRET_ACCESS_KEY ? 'SET' : 'NOT_SET',\n"
                    "  gateway_url: $env.ASPIRE_GATEWAY_URL || 'NOT_SET',\n"
                    "  supabase_url: $env.SUPABASE_URL || 'NOT_SET',\n"
                    "  default_suite_id: $env.DEFAULT_SUITE_ID || 'NOT_SET',\n"
                    "  default_office_id: $env.DEFAULT_OFFICE_ID || 'NOT_SET',\n"
                    "  supabase_key: $env.SUPABASE_SERVICE_ROLE_KEY ? 'SET' : 'NOT_SET'\n"
                    "};\n"
                    "const enabled = $env.N8N_WORKFLOW_ROTATION_ORCHESTRATOR_ENABLED;\n"
                    "const killed = (enabled === 'false' || enabled === '0') ? 'true' : 'false';\n"
                    "const crypto = require('crypto');\n"
                    "const today = new Date().toISOString().slice(0, 10);\n"
                    "const requestId = 'smoke-orch-' + Date.now();\n"
                    "const correlationId = 'smoke-rotation-' + today;\n"
                    "const payloadHash = crypto.createHash('sha256').update(JSON.stringify({\n"
                    "  secret_id: 'test-secret', adapter: 'test', date: today\n"
                    "})).digest('hex');\n"
                    "const idempotencyKey = crypto.createHash('sha256').update(\n"
                    "  requestId + 'ops.trigger_rotation.test' + payloadHash\n"
                    ").digest('hex');\n"
                    "return [{ json: {\n"
                    "  killed,\n"
                    "  env_check: results,\n"
                    "  requestId,\n"
                    "  correlationId,\n"
                    "  idempotencyKey,\n"
                    "  request_body: {\n"
                    "    secret_id: 'test-secret',\n"
                    "    adapter: 'test',\n"
                    "    correlation_id: correlationId,\n"
                    "    triggered_by: 'n8n-smoke-test',\n"
                    "    idempotency_key: idempotencyKey,\n"
                    "    rotation_interval_days: 90\n"
                    "  }\n"
                    "} }];\n"
                )
            }
        },
        {
            'id': 'check-killed',
            'name': 'Kill Switch Active?',
            'type': 'n8n-nodes-base.if',
            'typeVersion': 2.2,
            'position': [660, 300],
            'parameters': {
                'conditions': {
                    'options': {'version': 2, 'leftValue': '', 'caseSensitive': True, 'typeValidation': 'strict'},
                    'combinator': 'and',
                    'conditions': [{
                        'id': 'kill-check',
                        'operator': {'type': 'string', 'operation': 'equals'},
                        'leftValue': '={{ $json.killed }}',
                        'rightValue': 'true'
                    }]
                }
            }
        },
        {
            'id': 'killed-result',
            'name': 'Kill Result',
            'type': 'n8n-nodes-base.code',
            'typeVersion': 2,
            'position': [880, 200],
            'parameters': {
                'jsCode': (
                    "return [{ json: {\n"
                    "  test: 'rotation-orchestrator-smoke',\n"
                    "  phase: 'kill_switch',\n"
                    "  status: 'KILLED',\n"
                    "  env_check: $json.env_check,\n"
                    "  message: 'Kill switch blocked execution (expected if env=false/0)'\n"
                    "} }];\n"
                )
            }
        },
        {
            'id': 'try-rotation',
            'name': 'POST Rotate Test',
            'type': 'n8n-nodes-base.httpRequest',
            'typeVersion': 4.2,
            'position': [880, 400],
            'parameters': {
                'method': 'POST',
                'url': '={{ $env.ASPIRE_ROTATION_API_URL }}/rotate',
                'authentication': 'none',
                'sendHeaders': True,
                'headerParameters': {
                    'parameters': [
                        {'name': 'Content-Type', 'value': 'application/json'},
                        {'name': 'X-Correlation-ID', 'value': '={{ $json.correlationId }}'},
                        {'name': 'X-Idempotency-Key', 'value': '={{ $json.idempotencyKey }}'},
                        {'name': 'X-N8N-Workflow-ID', 'value': 'smoke-test-orch'}
                    ]
                },
                'sendBody': True,
                'specifyBody': 'json',
                'jsonBody': '={{ JSON.stringify($json.request_body) }}',
                'options': {'timeout': 10000}
            },
            'onError': 'continueRegularOutput'
        },
        {
            'id': 'api-result',
            'name': 'API Result',
            'type': 'n8n-nodes-base.code',
            'typeVersion': 2,
            'position': [1100, 400],
            'parameters': {
                'jsCode': (
                    "const envCheck = $('Env Check + Kill Switch').item.json.env_check;\n"
                    "const resp = $json;\n"
                    "const hasError = resp.error || (typeof resp.statusCode === 'number' && resp.statusCode >= 400);\n"
                    "return [{ json: {\n"
                    "  test: 'rotation-orchestrator-smoke',\n"
                    "  phase: 'rotation_api_call',\n"
                    "  status: hasError ? 'API_ERROR' : 'API_REACHABLE',\n"
                    "  env_check: envCheck,\n"
                    "  api_response: resp,\n"
                    "  message: hasError\n"
                    "    ? 'Rotation API error (expected if mock not running at ' + envCheck.rotation_api_url + ')'\n"
                    "    : 'Rotation API responded successfully'\n"
                    "} }];\n"
                )
            }
        }
    ],
    'connections': {
        'Webhook Trigger': {'main': [[{'node': 'Env Check + Kill Switch', 'type': 'main', 'index': 0}]]},
        'Env Check + Kill Switch': {'main': [[{'node': 'Kill Switch Active?', 'type': 'main', 'index': 0}]]},
        'Kill Switch Active?': {
            'main': [
                [{'node': 'Kill Result', 'type': 'main', 'index': 0}],
                [{'node': 'POST Rotate Test', 'type': 'main', 'index': 0}]
            ]
        },
        'POST Rotate Test': {'main': [[{'node': 'API Result', 'type': 'main', 'index': 0}]]}
    },
    'settings': {
        'executionOrder': 'v1',
        'saveDataErrorExecution': 'all',
        'saveDataSuccessExecution': 'all',
        'saveManualExecutions': True
    }
}

# --- Test Workflow 2: Monitor Smoke ---
test_mon_def = {
    'name': 'SMOKE TEST - Rotation Monitor',
    'nodes': [
        {
            'id': 'webhook-trigger',
            'name': 'Webhook Trigger',
            'type': 'n8n-nodes-base.webhook',
            'typeVersion': 2,
            'position': [220, 300],
            'webhookId': 'test-rotation-mon',
            'parameters': {
                'path': 'test-rotation-mon',
                'httpMethod': 'POST',
                'responseMode': 'lastNode',
                'options': {}
            }
        },
        {
            'id': 'env-check',
            'name': 'Env Check + Kill Switch',
            'type': 'n8n-nodes-base.code',
            'typeVersion': 2,
            'position': [440, 300],
            'parameters': {
                'jsCode': (
                    "const results = {\n"
                    "  kill_switch_mon: $env.N8N_WORKFLOW_ROTATION_MONITOR_ENABLED || 'NOT_SET',\n"
                    "  kill_switch_orch: $env.N8N_WORKFLOW_ROTATION_ORCHESTRATOR_ENABLED || 'NOT_SET',\n"
                    "  gateway_url: $env.ASPIRE_GATEWAY_URL || 'NOT_SET',\n"
                    "  supabase_url: $env.SUPABASE_URL || 'NOT_SET',\n"
                    "  default_suite_id: $env.DEFAULT_SUITE_ID || 'NOT_SET',\n"
                    "  default_office_id: $env.DEFAULT_OFFICE_ID || 'NOT_SET',\n"
                    "  supabase_key: $env.SUPABASE_SERVICE_ROLE_KEY ? 'SET' : 'NOT_SET',\n"
                    "  rotation_api_url: $env.ASPIRE_ROTATION_API_URL || 'NOT_SET'\n"
                    "};\n"
                    "const enabled = $env.N8N_WORKFLOW_ROTATION_MONITOR_ENABLED;\n"
                    "const killed = (enabled === 'false' || enabled === '0') ? 'true' : 'false';\n"
                    "const requestId = 'smoke-mon-' + Date.now();\n"
                    "const correlationId = 'smoke-rotation-mon-' + new Date().toISOString().slice(0,10);\n"
                    "return [{ json: { killed, env_check: results, requestId, correlationId } }];\n"
                )
            }
        },
        {
            'id': 'check-killed',
            'name': 'Kill Switch Active?',
            'type': 'n8n-nodes-base.if',
            'typeVersion': 2.2,
            'position': [660, 300],
            'parameters': {
                'conditions': {
                    'options': {'version': 2, 'leftValue': '', 'caseSensitive': True, 'typeValidation': 'strict'},
                    'combinator': 'and',
                    'conditions': [{
                        'id': 'kill-check',
                        'operator': {'type': 'string', 'operation': 'equals'},
                        'leftValue': '={{ $json.killed }}',
                        'rightValue': 'true'
                    }]
                }
            }
        },
        {
            'id': 'killed-result',
            'name': 'Kill Result',
            'type': 'n8n-nodes-base.code',
            'typeVersion': 2,
            'position': [880, 200],
            'parameters': {
                'jsCode': (
                    "return [{ json: {\n"
                    "  test: 'rotation-monitor-smoke',\n"
                    "  phase: 'kill_switch',\n"
                    "  status: 'KILLED',\n"
                    "  env_check: $json.env_check,\n"
                    "  message: 'Kill switch blocked execution'\n"
                    "} }];\n"
                )
            }
        },
        {
            'id': 'health-check',
            'name': 'GET Gateway Health',
            'type': 'n8n-nodes-base.httpRequest',
            'typeVersion': 4.2,
            'position': [880, 400],
            'parameters': {
                'method': 'GET',
                'url': '={{ $env.ASPIRE_GATEWAY_URL }}/api/health',
                'authentication': 'none',
                'sendHeaders': True,
                'headerParameters': {
                    'parameters': [
                        {'name': 'X-Correlation-ID', 'value': '={{ $json.correlationId }}'},
                        {'name': 'X-N8N-Workflow-ID', 'value': 'smoke-test-mon'}
                    ]
                },
                'options': {'timeout': 10000}
            },
            'onError': 'continueRegularOutput'
        },
        {
            'id': 'evaluate',
            'name': 'Evaluate Health',
            'type': 'n8n-nodes-base.code',
            'typeVersion': 2,
            'position': [1100, 400],
            'parameters': {
                'jsCode': (
                    "const envCheck = $('Env Check + Kill Switch').item.json.env_check;\n"
                    "const healthResp = $json;\n"
                    "const gatewayHealthy = healthResp && (healthResp.status === 'ok' || healthResp.healthy === true);\n"
                    "const hasError = healthResp.error || (typeof healthResp.statusCode === 'number' && healthResp.statusCode >= 400);\n"
                    "return [{ json: {\n"
                    "  test: 'rotation-monitor-smoke',\n"
                    "  phase: 'gateway_health_check',\n"
                    "  status: hasError ? 'GATEWAY_ERROR' : (gatewayHealthy ? 'GATEWAY_HEALTHY' : 'GATEWAY_UNKNOWN'),\n"
                    "  env_check: envCheck,\n"
                    "  gateway_response: healthResp,\n"
                    "  message: hasError\n"
                    "    ? 'Gateway health check failed (expected if gateway not running at ' + envCheck.gateway_url + ')'\n"
                    "    : (gatewayHealthy ? 'Gateway is healthy' : 'Gateway returned unknown status')\n"
                    "} }];\n"
                )
            }
        }
    ],
    'connections': {
        'Webhook Trigger': {'main': [[{'node': 'Env Check + Kill Switch', 'type': 'main', 'index': 0}]]},
        'Env Check + Kill Switch': {'main': [[{'node': 'Kill Switch Active?', 'type': 'main', 'index': 0}]]},
        'Kill Switch Active?': {
            'main': [
                [{'node': 'Kill Result', 'type': 'main', 'index': 0}],
                [{'node': 'GET Gateway Health', 'type': 'main', 'index': 0}]
            ]
        },
        'GET Gateway Health': {'main': [[{'node': 'Evaluate Health', 'type': 'main', 'index': 0}]]}
    },
    'settings': {
        'executionOrder': 'v1',
        'saveDataErrorExecution': 'all',
        'saveDataSuccessExecution': 'all',
        'saveManualExecutions': True
    }
}

# Create and activate test workflows
for wf_def, label in [(test_orch_def, 'orch'), (test_mon_def, 'mon')]:
    print(f'\n--- Creating {label} smoke test workflow ---')
    result, status = api_request('/api/v1/workflows', method='POST', data=wf_def)
    if status == 200:
        wf_id = result['id']
        test_workflow_ids.append(wf_id)
        print(f'  Created: {wf_id} ({result["name"]})')

        # Activate
        act_result, act_status = api_request(f'/api/v1/workflows/{wf_id}/activate', method='POST')
        if act_status == 200:
            print(f'  Activated: {act_result.get("active")}')
            record(f'smoke_{label}_created', True, f'ID={wf_id}, active')
        else:
            print(f'  Activation failed: {act_status}')
            record(f'smoke_{label}_created', False, f'created but activation failed: {act_status}')
    else:
        print(f'  Creation failed: {status} - {result}')
        record(f'smoke_{label}_created', False, f'creation failed: {status}')

# Wait for webhook registration
print('\nWaiting 3s for webhook registration...')
time.sleep(3)

# ============================================================
# PHASE 3: Execute Smoke Tests
# ============================================================
print()
print('=' * 70)
print('PHASE 3: Execute Smoke Tests via Webhooks')
print('=' * 70)

def trigger_webhook(path, payload=None):
    """Trigger a webhook and return the response.
    Active workflows use /webhook/ (not /webhook-test/ which requires canvas click).
    """
    url = f'{N8N_BASE}/webhook/{path}'
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        try:
            return json.loads(body), e.code
        except:
            return {'error': body, 'status_code': e.code}, e.code
    except Exception as e:
        return {'error': str(e)}, 0

# Test 1: Orchestrator smoke
print('\n--- Orchestrator Smoke Test ---')
orch_resp, orch_status = trigger_webhook('test-rotation-orch', {'test': True})
print(f'  HTTP {orch_status}')
print(f'  Response: {json.dumps(orch_resp, indent=2)[:1000]}')

if orch_status in (200, 201):
    # Check env vars were readable
    env_check = orch_resp.get('env_check', {})
    phase = orch_resp.get('phase', 'unknown')
    status_val = orch_resp.get('status', 'unknown')

    record('smoke_orch_executed', True, f'phase={phase}, status={status_val}')

    # Validate env var readings
    record('env_kill_switch_orch', env_check.get('kill_switch_orch') not in ('NOT_SET', None),
           f'value={env_check.get("kill_switch_orch")}')
    record('env_rotation_api_url', env_check.get('rotation_api_url') not in ('NOT_SET', None),
           f'value={env_check.get("rotation_api_url")}')
    record('env_aws_access_key', env_check.get('aws_access_key') == 'SET',
           f'value={env_check.get("aws_access_key")}')
    record('env_aws_secret_key', env_check.get('aws_secret_key') == 'SET',
           f'value={env_check.get("aws_secret_key")}')
    record('env_gateway_url', env_check.get('gateway_url') not in ('NOT_SET', None),
           f'value={env_check.get("gateway_url")}')
    record('env_supabase_url', env_check.get('supabase_url') not in ('NOT_SET', None),
           f'value={env_check.get("supabase_url")}')
    record('env_default_suite_id', env_check.get('default_suite_id') not in ('NOT_SET', None),
           f'value={env_check.get("default_suite_id")}')
    record('env_supabase_key', env_check.get('supabase_key') == 'SET',
           f'value={env_check.get("supabase_key")}')

    if phase == 'kill_switch':
        record('smoke_orch_kill_switch', True, 'Kill switch blocked as expected (workflow enabled = false/0)')
    elif phase == 'rotation_api_call':
        api_resp = orch_resp.get('api_response', {})
        if status_val == 'API_REACHABLE':
            record('smoke_orch_rotation_api', True, 'Rotation API responded')
        else:
            record('smoke_orch_rotation_api', False, f'Rotation API error: {json.dumps(api_resp)[:200]}')
else:
    record('smoke_orch_executed', False, f'HTTP {orch_status}: {json.dumps(orch_resp)[:200]}')

# Test 2: Monitor smoke
print('\n--- Monitor Smoke Test ---')
mon_resp, mon_status = trigger_webhook('test-rotation-mon', {'test': True})
print(f'  HTTP {mon_status}')
print(f'  Response: {json.dumps(mon_resp, indent=2)[:1000]}')

if mon_status in (200, 201):
    env_check = mon_resp.get('env_check', {})
    phase = mon_resp.get('phase', 'unknown')
    status_val = mon_resp.get('status', 'unknown')

    record('smoke_mon_executed', True, f'phase={phase}, status={status_val}')

    # Validate env var readings
    record('env_kill_switch_mon', env_check.get('kill_switch_mon') not in ('NOT_SET', None),
           f'value={env_check.get("kill_switch_mon")}')

    if phase == 'kill_switch':
        record('smoke_mon_kill_switch', True, 'Kill switch blocked as expected')
    elif phase == 'gateway_health_check':
        if status_val == 'GATEWAY_HEALTHY':
            record('smoke_mon_gateway_health', True, 'Gateway health check passed')
        elif status_val == 'GATEWAY_ERROR':
            record('smoke_mon_gateway_health', False, f'Gateway not reachable: {mon_resp.get("message")}')
        else:
            record('smoke_mon_gateway_health', False, f'Unknown status: {status_val}')
else:
    record('smoke_mon_executed', False, f'HTTP {mon_status}: {json.dumps(mon_resp)[:200]}')

# ============================================================
# PHASE 4: Cleanup
# ============================================================
print()
print('=' * 70)
print('PHASE 4: Cleanup Temporary Test Workflows')
print('=' * 70)

for wf_id in test_workflow_ids:
    # Deactivate first
    api_request(f'/api/v1/workflows/{wf_id}/deactivate', method='POST')
    # Delete
    del_result, del_status = api_request(f'/api/v1/workflows/{wf_id}', method='DELETE')
    if del_status == 200:
        print(f'  Deleted: {wf_id}')
        record('cleanup', True, f'Deleted {wf_id}')
    else:
        print(f'  Delete failed: {wf_id} (HTTP {del_status})')
        record('cleanup', False, f'Failed to delete {wf_id}: {del_status}')

# ============================================================
# PHASE 5: Final Report
# ============================================================
print()
print('=' * 70)
print('FINAL REPORT: Rotation Pipeline E2E Test')
print('=' * 70)
print(f'Timestamp: {datetime.utcnow().isoformat()}Z')
print(f'Total tests: {len(results)}')

passed = sum(1 for r in results if r['status'] == 'PASS')
failed = sum(1 for r in results if r['status'] == 'FAIL')
print(f'Passed: {passed}')
print(f'Failed: {failed}')
print(f'Pass rate: {passed}/{len(results)} ({100*passed//len(results) if results else 0}%)')

if failed > 0:
    print('\n--- FAILURES ---')
    for r in results:
        if r['status'] == 'FAIL':
            print(f'  FAIL: {r["test"]}: {r["detail"]}')

print('\n--- ALL RESULTS ---')
for r in results:
    icon = 'PASS' if r['status'] == 'PASS' else 'FAIL'
    print(f'  [{icon}] {r["test"]}: {r["detail"]}')

# Exit code
sys.exit(0 if failed == 0 else 1)
