"""Verify HMAC rejection behavior in n8n execution logs."""
import requests
import json
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

AUTH_COOKIE = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6IjRmZDc5ZTg5LTMwMTctNGRiZS04Y2VjLTY3NmZjYWI2ZjkzOCIsImhhc2giOiJmdm50Sm90OUFSIiwidXNlZE1mYSI6ZmFsc2UsImlhdCI6MTc3MTQ1NTIzNSwiZXhwIjoxNzcyMDYwMDM1fQ.oHz5suoUVo6GSp2odxioI1a229hNH_KHxNvkLaXFoLU'


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


# Execution pairs from our test:
# Round 2 valid HMAC: 36(intake-success), 38(eli), 40(sarah), 42(nora)
# Round 3 invalid HMAC: 44(intake-success), 46(eli), 48(sarah), 50(nora)
# Error triggers (paired): 35, 37, 39, 41, 43, 45, 47, 49

test_execs = [
    (36, "intake valid HMAC (success path)"),
    (35, "intake valid HMAC (error trigger)"),
    (44, "intake INVALID HMAC (success path)"),
    (43, "intake INVALID HMAC (error trigger)"),
    (38, "eli valid HMAC"),
    (46, "eli INVALID HMAC"),
    (40, "sarah valid HMAC"),
    (48, "sarah INVALID HMAC"),
    (42, "nora valid HMAC"),
    (50, "nora INVALID HMAC"),
]

keywords = [
    'killed', 'hmac', 'ECONNREFUSED', 'kill_switch',
    'Kill Switch Receipt', 'Kill/Reject Receipt',
    'Success Receipt', 'Failure Receipt',
    'hmac_validation_failed', 'lastNodeExecuted'
]

for ex_id, desc in test_execs:
    r = requests.get(
        f'http://localhost:5678/rest/executions/{ex_id}',
        headers={'Cookie': f'n8n-auth={AUTH_COOKIE}'},
        timeout=10
    )
    resp_data = r.json().get('data', {})
    inner_data = resp_data.get('data', '')
    status = resp_data.get('status', '?')
    mode = resp_data.get('mode', '?')

    if not isinstance(inner_data, str):
        print(f'#{ex_id} ({desc}): No flatted data')
        continue

    found = search_flatted(inner_data, keywords)

    print(f'\n#{ex_id} {desc} (status={status}, mode={mode}):')
    if found:
        for kw, ctx in found.items():
            # Clean up context
            clean = ctx.replace('\n', ' ')[:150]
            print(f'  [{kw}]: ...{clean}...')
    else:
        print(f'  No relevant keywords found')
