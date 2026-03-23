---
name: findings_log
description: Per-cycle security findings log with threat IDs and resolution status
type: project
---

# Security Findings Log

## Cycle 5 (2026-03-22) — Full Sweep

### NEW Findings
- THREAT-008: finance_connections/tokens lack suite_id tenant guard in application layer (HIGH)
- THREAT-009: Email PII logged in plaintext at routes.ts:545 (MEDIUM)
- THREAT-010: temporal_task_tokens RLS uses wrong setting key `app.suite_id` vs `app.current_suite_id` (HIGH)
- THREAT-011: Anam session store brute-force — /api/ava/chat-stream context hijack (MEDIUM)
- THREAT-012: Admin portal (import-my-portal-main) not accessible on disk — unverified security posture (REVIEW GAP)

### Previously Tracked (not re-reported)
- THREAT-001 through THREAT-007: see MEMORY.md

## Cycle 4 (prior) — Known Tracked
- THREAT-002 FIXED: allow_internal_routing param bypass
- THREAT-001, 003-007: TRACKED/OPEN
