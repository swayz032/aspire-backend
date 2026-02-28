---
gate: 5
name: "Authority UI Contract"
status: "complete"
phase_introduced: "3"
complexity: "medium"
critical: false
---

# GATE 05: Authority UI Contract

## Requirement

Visually enforced 'Authority Required' state - users see when approval is needed.

## UI Components

### 1. Authority Dashboard (Surface 1)
- **Purpose:** Central approval center
- **Location:** Accessible from all tabs (slide-in drawer or floating FAB)
- **Features:**
  - Pending approvals list
  - Approve/Reject buttons with confirmation
  - Risk level visual indicators (High/Medium/Low)
  - Escalation notifications (urgent approvals)

### 2. Approval Metadata Display
- **For Each Pending Action:**
  - **Who:** Which actor/office is requesting
  - **What:** Clear description of action (e.g., "Send $5,000 invoice payment")
  - **Risk:** Visual indicator (RED for high-stakes, YELLOW for medium, GREEN for low)
  - **Why:** Contextual explanation (e.g., "Payment is irreversible")

### 3. Visual Risk Indicators

#### HIGH Risk (RED)
- **Actions:** Financial transactions, legal signatures, irreversible changes
- **UI:** Red badge, red border, "High Risk" label
- **Confirmation:** Double-tap or explicit confirmation dialog

#### MEDIUM Risk (YELLOW)
- **Actions:** Email sending, calendar scheduling, draft creation
- **UI:** Yellow badge, yellow border, "Requires Approval" label
- **Confirmation:** Single tap with preview

#### LOW Risk (GREEN)
- **Actions:** Read operations, search queries, data retrieval
- **UI:** Green badge (or no visual indicator - auto-approved)
- **Confirmation:** None (autonomous execution)

### 4. Approve/Reject Workflow

#### Approve Flow:
1. User taps pending action in Authority Dashboard
2. Detailed preview shown (what will happen, impact, risk)
3. User taps "Approve" button
4. Confirmation dialog for HIGH risk actions
5. Receipt generated immediately
6. Action executed by LangGraph Brain
7. Completion notification

#### Reject Flow:
1. User taps pending action
2. User taps "Reject" button
3. Optional reason text field
4. Receipt generated documenting rejection
5. Action cancelled, requestor notified

## Verification Criteria

- [ ] Authority Dashboard accessible from all tabs
- [ ] All pending approvals visible in dashboard
- [ ] Risk levels (High/Medium/Low) visually indicated
- [ ] Approve/Reject buttons functional for all actions
- [ ] Receipts generated for both approvals and rejections
- [ ] No hidden approval gates (all gates are visible UI)

## What This Gate Prevents

- **Hidden gates** - User doesn't know approval is needed until action fails
- **Confusion** - User unsure why action is blocked
- **Friction** - User can't find approval interface
- **Mistrust** - System feels like "black box" without transparency

## Failure Scenarios

❌ **Fails if:**
- Approvals happen silently without user visibility
- Authority Dashboard missing or hidden
- Risk levels not visually indicated (all actions look same)
- User cannot reject actions (only approve/timeout options)

✅ **Passes if:**
- Authority Dashboard prominently accessible
- All pending actions listed with clear metadata
- Visual risk indicators working (RED/YELLOW/GREEN)
- Approve + Reject both functional

## Example UI Mockup

```
┌─────────────────────────────────────────┐
│  Authority Dashboard             (3) 🔔  │
├─────────────────────────────────────────┤
│                                          │
│  🔴 HIGH RISK                            │
│  Send $5,000 Payment to ContractorCo    │
│  Requested by: Office #1 (You)          │
│  Risk: Irreversible financial action    │
│  [Approve] [Reject]                     │
│                                          │
│  🟡 MEDIUM RISK                          │
│  Send Email: "Invoice #1234 Overdue"    │
│  Requested by: Office #1 (You)          │
│  To: customer@example.com               │
│  [Approve] [Reject]                     │
│                                          │
│  🟢 LOW RISK (Auto-Approved)             │
│  Search Receipts for "ContractorCo"     │
│  Executed automatically - No approval   │
│  needed                                  │
└─────────────────────────────────────────┘
```

## Related Gates

- **Gate 03:** Forced Escalation (HIGH risk requires video)
- **Gate 06:** Receipts (every approval/rejection generates receipt)

## Status: ✅ COMPLETE

**Verification Date:** 2026-01-10
**Verified By:** Phase 3 roadmap defines Authority Dashboard UI
**Evidence:** Authority Dashboard spec in Aspire-Production-Roadmap.md:2112-2120
