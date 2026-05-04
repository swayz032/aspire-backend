"""Per-tenant Twilio Trust Hub + CNAM onboarding worker.

Drives each tenant through the 12-state machine that creates their own
Customer Profile, SHAKEN/STIR Trust Product, and CNAM Trust Product so
outbound calls display the tenant's verified business name on caller ID.

Modules:
    - cnam_sanitizer  : 15-char CNAM display name sanitizer (W2-B)
    - twilio_trust_hub: Twilio Trust Hub REST client (W2-C, in providers/)
    - state_machine   : 12-state advance function (W2-D)
    - trust_receipts  : receipt-cutting helpers + hash chain (W2-E)
    - worker          : ARQ entry point + job registry (W2-A)
    - swap_state_machine: number-swap state machine (W11)
    - cron_jobs       : reputation polling + auto-recovery (W9)

State machine (forward transitions, see state_machine.py):
    kyb_collected → profile_drafted → profile_submitted →
    profile_approved → shaken_created → shaken_submitted →
    shaken_approved → cnam_created → cnam_submitted →
    cnam_approved → number_attached →
    [optionally branded_calling_pending → branded_calling_live]

Some transitions are async-driven by Twilio status callbacks
(profile_submitted → profile_approved, shaken_submitted → shaken_approved,
cnam_submitted → cnam_approved). Others are immediate ARQ jobs.

Receipts (Yellow tier, hash-chained per trust_profile_id):
    See trust_receipts.RECEIPT_TYPES for the full list.
"""
