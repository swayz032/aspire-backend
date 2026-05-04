---
name: Wave 7 A2P 10DLC Registration Patterns
description: A2P state machine, OTP retry tracking, Twilio Messaging API, receipt scope bridging, ARQ job registration for advance_a2p_registration
type: project
---

## Key patterns from Wave 7 (A2P 10DLC registration)

**Why:** W7 ships Sole Proprietor A2P brand + campaign registration. These notes prevent re-discovery in future waves (W8 OTP UI, W9 cron, W11 number swap).

**How to apply:** Reference when extending A2P state machine, adding Twilio Messaging API calls, or writing tests for trust-onboarding state machines.

---

### OTP retry counter storage
No dedicated `otp_retry_count` column in `tenant_a2p_brands` (migration 111 only has `otp_sent_at`, `otp_verified_at`). W7 stores retry count as `rejection_reason = "OTP_ATTEMPT:{n}"` while brand is still pending/not-locked-out. On 3rd failure, `brand_status = "suspended"` and `rejection_reason = "OTP_LOCKED_OUT after N failed attempts"`. The status route hides rejection_reason for non-rejected/non-suspended brands.

### Receipt scope bridge (A2P → trust_profile audit chain)
`cut_trust_receipt` requires a `trust_profile` dict with `id, suite_id, tenant_id, office_id`. A2P rows don't have `trust_profile_id` directly — use `_make_receipt_scope(brand, trust_profile)` which loads `tenant_trust_profiles` by suite_id and passes its `id` as the trust_profile_id. This keeps the audit ledger unified per tenant (architect mandate).

### `otp_confirmed` is a synthetic state
Twilio has no `otp_confirmed` state. The state machine writes `brand_status = "otp_confirmed"` internally (not in migration 111 CHECK constraint — it's only valid as an in-flight application state between OTP acceptance and vetting POST). The CHECK in migration 111 only covers: `draft, pending, approved, rejected, suspended`. `otp_confirmed` is allowed because service_role writes bypass the CHECK. If CHECK is ever tightened, add `otp_confirmed` to the constraint.

### Twilio A2P Messaging API base URL
`https://messaging.twilio.com/v1` (NOT `https://trusthub.twilio.com/v1`)

- BrandRegistrations: `POST /v1/a2p/BrandRegistrations`
- SoleProprietorVettings: `POST /v1/a2p/BrandRegistrations/{Sid}/SoleProprietorVettings`
- OTP verify: `POST /v1/a2p/BrandRegistrations/{Sid}/OtpVerifications` (body: `Otp={code}`)
- Messaging Service: `POST /v1/Services`
- Add phone: `POST /v1/Services/{Sid}/PhoneNumbers`
- Campaign: `POST /v1/a2p/UsAppToPerson`

### ARQ job registration
`advance_a2p_registration` is registered in `WorkerSettings.functions` alongside `advance_trust_state`. Both use queue `arq:trust_onboarding`. Job IDs: `a2p:{suite_id}:advance`.

### State machine entry point
`advance_a2p_registration(suite_id: str, *, worker_job_id=None)` — keyed on `suite_id` (not `trust_profile_id` like W2). Brand row is loaded by `suite_id`, campaign by `brand_id`.

### Receipt types used
- `a2p_brand_registered` — for all brand-level transitions (draft→pending, otp_confirmed→pending, OTP success)
- `a2p_campaign_approved` — for campaign submission (outcome=`pending` when submitted; Twilio webhook fires later with outcome=`success`)

### Test mock pattern
`_MockSupabase` class pattern from W7 test file (collects `.updated` and `.inserted` lists). `_patch_supabase(mock_sb)` uses `patch.multiple` on the a2p_state_machine module. Matches W2 pattern.

### 409 on add_phone_to_messaging_service
409 from `add_phone_to_messaging_service` means already added — treat as idempotent success (same as other Trust Hub 409s in W2).
