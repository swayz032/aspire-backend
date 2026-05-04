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

## Pass 18 (2026-04-29) — Gate 5 Security — Office Memory Engine

### NEW Findings

- THREAT-013: `/v1/ingest/document` and `/v1/ingest/aspire-calendar` have NO auth dependency in server.py — comment says "JWT+cap-token enforced by FastAPI dependency in server.py" but no dependency is registered. Both routes are publicly reachable. (CRITICAL)
- THREAT-014: `routes/sarah.py:215` — `called_number` from HMAC-verified payload used in PostgREST filter via f-string without E.164 validation. Can inject PostgREST operators to widen the filter. (HIGH)
- THREAT-015: `services/twilio_provisioning.py:release_number()` — queries tenant_phone_numbers by `id` only, no suite_id/office_id binding. Attacker with a valid capability token for their own office can release another tenant's phone number by guessing/knowing its UUID. (HIGH)
- THREAT-016: `sms_io.py` — outbound `memory_objects` row for sent SMS inserted via raw `supabase_insert` with all scope fields set from in-memory variables, but `twilio_message_sid` column name is `message_sid` in code vs `twilio_message_sid` in schema — likely causes silent insert failures (MEDIUM — reliability/audit gap)
- THREAT-017: DLP / Presidio not invoked on ANY ingestion adapter path (13 adapters). Inbound SMS body, EL transcripts, Zoom transcripts, PandaDoc contract content written to memory_objects without PII scan. Law #9 / Law #10 Gate 5 violation. (MEDIUM)
- THREAT-018: `front_desk_config_mark_current()` trigger function has no SECURITY DEFINER + no SET search_path guard. Risk: low (trigger is on a single tenant-scoped table) but inconsistent with platform pattern. (LOW)
- THREAT-019: Capability token revocation is in-memory only — multi-replica deployment means revocation on replica A doesn't propagate to replica B. Self-documented gap (token_service.py:F-HIGH-3). TTL=59s bounds blast radius. (LOW — already documented)
- THREAT-020: `sms_io.py` outbound receipt includes `to_number` (full E.164 phone number) in `redacted_inputs`. Phone number is PII under Law #9 — should be masked to first 6 digits. (LOW)

### Pass 18 Verdict: CONDITIONAL PASS — 1 critical, 2 high require remediation before production

## Round 7 Wave 1+2 (2026-04-30) — Gate 5 Security — Anam enrichment + multi-store + diag log

### NEW Findings

- THREAT-R7-001: `agentToolRoutes.ts:1007-1016` — Diagnostic log (`LOG_TOOL_INVOKE_DIAG`) fires BEFORE `verifySecret` at line 1018. Unauthenticated callers can inject arbitrary 200-byte strings into the production log stream during the capture window. (HIGH — during window; LOW when flag off)
- THREAT-R7-002: `trades.py:715` — `user_address[:60]` logged in plaintext in the fallback branch (nearest_store is None). Regression of F-HIGH-6 fix; `_redact_user_address()` was introduced specifically for this pattern but was not applied here. (HIGH)
- THREAT-R7-003: `agentToolRoutes.ts:549-550` — `home_city`/`home_state` in briefing response are not in the plan-approved whitelist. Personal residence city+state is sub-street precision but was not explicitly authorized. (MEDIUM)
- THREAT-R7-004: `agentToolRoutes.ts:487-493,527` — `x-user-timezone` header accepted without IANA validation, echoed into LLM context. Can cause time-display DoS or minor prompt pollution. (MEDIUM)
- THREAT-R7-005: `agentToolRoutes.ts:1010` — `content-type` header logged without CRLF stripping. Structured JSON logger neutralizes, but text-mode log shippers are vulnerable. (LOW)

### Round 7 Safe Patterns Confirmed
- `rawBodyPreview` truncated to 200 chars BEFORE diag log — correct
- `_redact_user_address()` correctly used on main path (trades.py:542)
- `home_address_line1/2`, DOB, SSN, EIN, banking NOT in SELECT or response — whitelist correctly enforced on sensitive fields
- `x-aspire-tool-secret` never logged in value — only boolean `hasSecret`/`hasAspireHeader`
- Decision flags (`hd_too_far`, `hd_has_stock`, `nearest_store_distance_miles`) carry no PII — safe in receipts
- SerpApi result sets bounded at :8 (HD) and :6 (shopping) before normalization — memory safe
- `include_other_stores` boolean not cross-tenant — scoped to the PlaybookContext which carries suite_id from session

### Round 7 Verdict: CONDITIONAL PASS — 2 blocking (R-001, R-002 one-line each), 3 advisory

## Wave 5 (2026-05-03) — Status-Callback Dispatch (Trust Hub)

### NEW Findings

- THREAT-W5-001: `FailureReason` from Twilio form written verbatim to `tenant_trust_profiles.rejection_reason` (DB column) AND echoed in GET /status response body without length cap or sanitization. Twilio may include rep names/business details. (MEDIUM — Law #9 concern, no client-response truncation)
- THREAT-W5-002: `failure_reason` is NOT passed as `twilio_rejection_reason` to `cut_trust_receipt` in the status_callback handler (routes/trust_hub.py:1112-1129). The dedicated `twilio_rejection_reason` param of `cut_trust_receipt` is never populated from the callback path — inconsistent with state_machine.py which does pass it. Audit trail is incomplete for rejection events. (MEDIUM — audit gap, Law #2)
- THREAT-W5-003: `_TWILIO_SID_RE` compiled on every request (line 932 inside hot path) rather than at module load. No security impact; minor performance waste. (LOW)
- THREAT-W5-004: `uvicorn proxy_headers=True` with no `forwarded_allow_ips` restriction means any caller can spoof `X-Forwarded-Host` / `X-Forwarded-Proto` headers, potentially causing `str(request.url)` to reflect the attacker-controlled host/scheme. Twilio HMAC covers the URL string — if the URL used for validation differs from what Twilio signed (e.g., `https://attacker.com/v1/trust-hub/status-callback`), HMAC would fail. But if a forged X-Forwarded-Host matches a trusted Twilio URL already registered elsewhere, the HMAC passes against the wrong URL. (MEDIUM — defense-in-depth gap)
- THREAT-W5-005: `_STATE_RANK` is missing `kyb_disputed` state (referenced in dispute receipt's `from_state`). If DB ever writes `trust_state=kyb_disputed` and a stale callback arrives for that profile, `current_rank = _STATE_RANK.get("kyb_disputed", -1)` returns -1, and any target state will have rank >= -1, so the callback WILL advance state. Idempotency guard is bypassed for any profile in kyb_disputed state. (MEDIUM — correctness + idempotency gap)
- THREAT-W5-006: T2 dispatch-table — `is_approved` is a strict equality check (`twilio_status == "twilio-approved"`). Any other status value (empty, `twilio-pending`, unknown) maps `is_approved=False`. `_STATE_MAP.get((bundle_type, False))` then maps to `profile_rejected`/`failed`. This means an unknown status like `twilio-pending` sent with a valid HMAC on a profile-type SID would set `trust_state=profile_rejected`. This is a state-corruption vector. (HIGH)

### Wave 5 Verdict: CONDITIONAL PASS — 1 blocking (W5-006), 3 medium advisory, 1 low

### Wave 5 Safe Patterns Confirmed
- HMAC is first check before ALL DB ops — correct
- SID validated against `^[A-Z]{2}[0-9a-fA-F]{32}$` before PostgREST interpolation — correct
- No form body logged at INFO level before HMAC validation — correct
- `failure_reason` NOT in `redacted_inputs` or `redacted_outputs` — passes `_assert_no_pii`
- `rejection_reason` column write is correct (service-role only per comment)
- Idempotency guard covers the primary happy-path states correctly
- `failed`/`suspended` as terminal (rank=99) prevent re-advancement from terminal state
- ARQ not enqueued on rejection — correct
- `webhook_processing_failed` receipt cut on any exception inside try block — correct
