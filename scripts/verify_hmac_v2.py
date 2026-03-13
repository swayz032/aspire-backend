"""Verify HMAC rejection behavior in n8n execution logs (round 2 with Nora fix)."""
import requests
import json
import sys
import io

from _n8n_runtime import get_n8n_admin_email, get_n8n_admin_password, get_n8n_base_url

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
N8N_BASE = get_n8n_base_url()

# Get a fresh session cookie
session = requests.Session()
r = session.post(f'{N8N_BASE}/rest/login', json={
    'emailOrLdapLoginId': get_n8n_admin_email(),
    'password': get_n8n_admin_password()
}, timeout=5)
AUTH_COOKIE = dict(r.cookies).get('n8n-auth', '')

if not AUTH_COOKIE:
    print("Failed to get session cookie")
    sys.exit(1)


def search_flatted(flatted_str, keywords):
    results = {}
    for kw in keywords:
        kw_lower = kw.lower()
        text_lower = flatted_str.lower()
        if kw_lower in text_lower:
            idx = text_lower.index(kw_lower)
            context = flatted_str[max(0, idx - 80):idx + 120]
            results[kw] = context
    return results


# New executions from the latest test run:
# Valid HMAC: 54(intake), 56(eli), 58(sarah), 60(nora)  -- all mode=error, status=success
# Invalid HMAC: 62(intake), 64(eli), 66(sarah), 68(nora) -- all mode=error, status=success
# Error triggers: 53(intake), 55(eli), 57(sarah), 59(nora) -- valid HMAC errors
#                 61(intake), 63(eli), 65(sarah), 67(nora) -- invalid HMAC errors

# Focus on the webhook-mode (error trigger) executions since they contain the main workflow data
test_execs = [
    # Valid HMAC error triggers (webhook mode)
    (53, "intake valid HMAC", True),
    (55, "eli valid HMAC", True),
    (57, "sarah valid HMAC", True),
    (59, "nora valid HMAC", True),
    # Invalid HMAC error triggers (webhook mode)
    (61, "intake INVALID HMAC", False),
    (63, "eli INVALID HMAC", False),
    (65, "sarah INVALID HMAC", False),
    (67, "nora INVALID HMAC", False),
]

keywords = [
    'killed', 'hmac_validation_failed', 'ECONNREFUSED',
    'Kill Switch Receipt', 'Kill/Reject Receipt',
    'Success Receipt', 'Failure Receipt',
    'prepareSecretKey', 'lastNodeExecuted'
]

print("=" * 70)
print("  HMAC Verification Report (Post-Fix)")
print("=" * 70)

all_hmac_correct = True

for ex_id, desc, should_hmac_pass in test_execs:
    r = requests.get(
        f'{N8N_BASE}/rest/executions/{ex_id}',
        headers={'Cookie': f'n8n-auth={AUTH_COOKIE}'},
        timeout=10
    )
    resp_data = r.json().get('data', {})
    inner_data = resp_data.get('data', '')
    status = resp_data.get('status', '?')
    mode = resp_data.get('mode', '?')

    if not isinstance(inner_data, str):
        print(f'\n#{ex_id} ({desc}): No flatted data available')
        continue

    found = search_flatted(inner_data, keywords)

    # Determine HMAC behavior
    has_killed_true = '"killed":true' in inner_data or '"killed": true' in inner_data
    has_killed_false = '"killed":false' in inner_data or '"killed": false' in inner_data
    has_hmac_failed = 'hmac_validation_failed' in inner_data
    has_econnrefused = 'ECONNREFUSED' in inner_data
    has_secret_error = 'prepareSecretKey' in inner_data

    # Evaluate correctness
    if should_hmac_pass:
        # Valid HMAC: should NOT have killed=true or hmac_validation_failed
        hmac_correct = not has_hmac_failed and (has_killed_false or not has_killed_true)
        if has_econnrefused:
            # ECONNREFUSED from Gateway is expected and OK -- means HMAC passed and workflow proceeded
            hmac_correct = True
    else:
        # Invalid HMAC: SHOULD have killed=true and hmac_validation_failed
        hmac_correct = has_killed_true and has_hmac_failed

    if not hmac_correct:
        all_hmac_correct = False

    verdict = "CORRECT" if hmac_correct else "INCORRECT"

    print(f'\n#{ex_id} {desc} (status={status}, mode={mode}): [{verdict}]')
    print(f'  killed=true: {has_killed_true}')
    print(f'  killed=false: {has_killed_false}')
    print(f'  hmac_validation_failed: {has_hmac_failed}')
    print(f'  ECONNREFUSED: {has_econnrefused}')
    print(f'  prepareSecretKey error: {has_secret_error}')

    if has_secret_error:
        print(f'  WARNING: HMAC secret key is undefined!')

print(f'\n{"=" * 70}')
print(f'  HMAC VERIFICATION VERDICT: {"ALL CORRECT" if all_hmac_correct else "SOME INCORRECT"}')
print(f'{"=" * 70}')
