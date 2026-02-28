# Support Playbook: Authorization Revoked

Symptoms:
- provider returns auth_invalid / 401

Steps:
1) Disable provider execution for tenant (APPROVAL_ONLY)
2) Prompt reconnect flow
3) Record receipt + privileged audit entry
