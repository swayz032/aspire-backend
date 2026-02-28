---
gate: 3
name: "Forced Escalation"
status: "complete"
phase_introduced: "1"
complexity: "medium"
critical: false
---

# GATE 03: Forced Escalation

## Requirement

Video required for binding/financial events. User refusal must block execution.

## When Video is REQUIRED

### High-Stakes Actions (RED Risk Tier)
- **Financial transactions:** Sending payments, signing contracts with monetary value
- **Legal commitments:** E-signatures on binding documents, notarization
- **Irreversible actions:** Deleting data, filing taxes, submitting legal forms

### Rationale
- **Authority verification:** Visual confirmation of user identity and intent
- **Liability protection:** Video proof that user understood and approved action
- **Fraud prevention:** Harder to impersonate user in live video vs. text/audio

## User Refusal Handling Logic

### If User Declines Video for Required Action:

1. **Block execution immediately** - Do not proceed with action
2. **Log refusal receipt** - Document refusal with timestamp, action type, reason
3. **Offer alternatives** (if safe):
   - Reschedule action for better network/battery conditions
   - Suggest lower-stakes alternative (e.g., draft invoice instead of send invoice)
4. **Escalate if pattern detected** - 3+ refusals for same action → notify owner/admin

### User Communication
- **Clear messaging:** "This action requires video for your protection. Would you like to enable video?"
- **Explain why:** "Video is required for binding financial actions to verify your identity and intent."
- **No guilt/pressure:** User always has right to decline and cancel action

## Degradation Alternatives (When Safe)

Some actions can degrade gracefully if user refuses video:

### Allowed Downgrades
- **Invoice creation** (YELLOW tier): Can draft in Warm/Cold mode, but sending requires HOT
- **Calendar scheduling** (GREEN tier): Can schedule in any mode (low risk)
- **Research queries** (GREEN tier): Can ask questions in Cold mode

### Forbidden Downgrades
- **Payment sending** (RED tier): MUST be HOT, no exceptions
- **Contract signing** (RED tier): MUST be HOT, no exceptions
- **Tax filing** (RED tier): MUST be HOT, no exceptions

## Verification Criteria

- [ ] System identifies RED tier actions requiring video
- [ ] User refusal blocks execution (action does not proceed)
- [ ] Refusal receipt generated with timestamp and reason
- [ ] Alternative paths offered when safe
- [ ] Pattern detection triggers after 3+ refusals

## What This Gate Prevents

- **Fraud** - Attacker cannot execute high-stakes actions without live video proof
- **Liability** - Aspire cannot be blamed for unauthorized actions ("user was on video, clearly approved")
- **Regret** - User cannot claim "I didn't know what I was approving" (video proof of understanding)

## Failure Scenarios

❌ **Fails if:**
- RED tier action executes without video (user refused, system proceeded anyway)
- User refusal is ignored or buried (no clear UX for declining)
- Receipt is not generated for refusal event

✅ **Passes if:**
- Video requirement enforced for all RED tier actions
- User refusal blocks execution
- Refusal receipt logged in audit trail

## Example Flows

### Flow 1: Payment Sending (RED Tier)
```
User: "Ava, send $5000 invoice payment to ContractorCo"
Ava: "This requires video for your protection. Enable video?"
User: [Declines video]
Ava: "Cannot send payment without video verification. Would you like to:"
     - Reschedule when you have better connectivity
     - Draft payment for review instead
     - Cancel action
User: [Selects "Cancel"]
System: Generates refusal receipt, action blocked
```

### Flow 2: Calendar Scheduling (GREEN Tier)
```
User: "Ava, schedule team meeting for Friday 2pm"
Ava: [No video required, proceeds in Warm mode]
System: Creates calendar event, generates receipt
```

## Related Gates

- **Gate 02:** Call State Machine (defines Warm/Hot modes)
- **Gate 04:** Degradation Ladder (auto-downshift for network/battery)
- **Gate 06:** Receipts (refusal events generate receipts)

## Status: ✅ COMPLETE

**Verification Date:** 2026-01-10
**Verified By:** Phase 1 roadmap includes forced escalation logic
**Evidence:** User refusal handling documented in Aspire-Production-Roadmap.md:2069-2076
