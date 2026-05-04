---
name: Wave 11 Number-Swap Patterns
description: Key patterns discovered implementing W11 number-swap state machine, ARQ worker entry, and swap route
type: project
---

## Lazy imports break patch() — always top-level import

`patch("module.attr")` requires `attr` to exist as a module-level attribute at patch time.
If `purchase_number` or `search_available_numbers` are imported inside a function, `patch("module.purchase_number")` raises `AttributeError`.

**Fix**: move all patchable dependencies to top-level imports in the implementation file.

## ExitStack is required for dynamic patch lists in Python 3.11+

Python 3.11 `with` statement does NOT support `*unpacked_list`. Use `contextlib.ExitStack`:

```python
with ExitStack() as stack:
    stack.enter_context(patch_a)
    stack.enter_context(patch_b)
    for p in dynamic_patches:
        stack.enter_context(p)
```

## tenant_phone_swaps progress JSONB key names

Step completion flags used by idempotency guards:
- `step_1_initiated_receipt` — receipt_id string
- `step_1_new_twilio_sid`    — new number Twilio SID
- `step_2_new_phone_id`      — tenant_phone_numbers UUID
- `step_3_cp_ra_sid`         — CP ChannelEndpointAssignment SID
- `step_4_shaken_ra_sid`     — SHAKEN bundle assignment SID
- `step_5_cnam_ra_sid`       — CNAM bundle assignment SID
- `step_6_caller_id_enabled` — bool
- `step_7_switch_done`       — bool
- `step_8_old_cp_detached`   — bool
- `step_8_old_shaken_detached` — bool
- `step_8_old_cnam_detached`  — bool
- `step_9_caller_id_disabled` — bool
- `step_10_old_released`     — bool
- `step_11_twilio_released`  — bool

## PurchasedNumber does NOT carry the DB UUID

`twilio_provisioning.purchase_number` returns `PurchasedNumber` which has `twilio_sid` but NO `phone_number_id`.
To get the UUID, query `tenant_phone_numbers` by `twilio_sid` + `suite_id` after purchase.

## Receipt count for full swap: 12

number_swap_initiated (1) + number_attached_to_profile × 3 (3) + caller_id_lookup_enabled (1) +
front_desk_phone_switched (1) + number_detached_from_profile × 3 (3) + caller_id_lookup_disabled (1) +
phone_number_released (1) + number_swap_complete (1) = 12

## receipt count when resuming from step 5 (all 3 attaches done): 8

Steps 6-11 produce 8 receipts: cid_enabled + switched + 3×detached + cid_disabled + released + complete.

## ARQ queue name: 'arq:trust_onboarding'

Shared queue for trust_state, a2p, AND swap jobs. Job ID deduplication uses `swap:{swap_job_id}:advance`.

## W11 capability token scope: telephony:swap_number

Route: POST /v1/twilio/swap-number (Yellow tier, 202 Accepted).
Uses _validate_cap_token / _resolve_scope / _cap_token_id from routes/front_desk.py (same as A2P/trust-hub).

## Rollback rule (hard)

Only trigger rollback on step 7 (front_desk_configs switch) failure.
For ANY failure AFTER step 7 succeeds, the new number is live — treat cleanup as non-blocking and log.
Never attempt full rollback after the atomic switch commits.

## Why: post-switch old number cleanup failures do not fail the swap

The tenant has a working new number from the moment step 7 commits. Old number cleanup
(detach, disable, release) is operational hygiene — it is non-blocking by design so that
a transient Twilio failure after a successful switch doesn't roll back an otherwise complete swap.
