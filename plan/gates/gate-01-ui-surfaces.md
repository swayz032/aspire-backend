---
gate: 1
name: "UI Surface Invariants"
status: "complete"
phase_introduced: "3"
complexity: "medium"
critical: false
---

# GATE 01: UI Surface Invariants

## Requirement

Exactly 6 UI surfaces enumerated, 4-tab navigation, call overlay architecture.

## 6 Surfaces Defined

### 1. Authority Dashboard
- **Purpose:** Approval center for all gated actions
- **Features:** Pending approvals list, approve/reject buttons, escalation notifications
- **Risk Display:** High/Medium/Low visual indicators

### 2. Inbox View
- **Purpose:** Dual mailbox architecture
- **Mailboxes:**
  - Business Email (Zoho white-label) - External chaos, uncontrolled senders
  - Office Inbox (internal) - Ava messages, receipts, system notifications
- **Features:** Unified interface, visual separation, search/filter

### 3. Receipts Log
- **Purpose:** Immutable audit trail viewer
- **Features:** Chronological list, collapsed/expanded states, search by correlation ID
- **Data:** Action type, timestamp, actor, outcome, redacted inputs/outputs

### 4. Ava Surface
- **Purpose:** Conversational interface with confidence indicators
- **Features:** Voice input, text fallback, uncertainty flags, source citations
- **Modes:** Cold (text-only), Warm (audio), Hot (video)

### 5. Call Overlay
- **Purpose:** Video/audio controls during active calls
- **Features:** Degradation controls, mute/unmute, end call, escalate to video
- **Modes:** Overlay during any interaction state

### 6. Settings/Market
- **Purpose:** Configuration + skill pack marketplace
- **Settings:** Preferences, integrations, notifications
- **Market:** Skill pack catalog, install/uninstall, pricing

## 4-Tab Navigation

1. **Inbox Tab** (Surface 2)
2. **Receipts Tab** (Surface 3)
3. **Ava Tab** (Surface 4)
4. **Settings Tab** (Surface 6)

**Authority Dashboard** (Surface 1) accessible from all tabs (floating overlay or slide-in drawer)
**Call Overlay** (Surface 5) appears on top of any tab during calls

## Verification Criteria

- [ ] Mobile app implements exactly 6 surfaces (no more, no less)
- [ ] 4-tab navigation implemented as specified
- [ ] Authority Dashboard accessible from all tabs
- [ ] Call Overlay appears during active calls only
- [ ] No additional surfaces added without gate revision

## What This Gate Prevents

- **UI scope creep** - Adding unnecessary surfaces that bloat the app
- **Navigation confusion** - Too many tabs or unclear hierarchy
- **Inconsistent UX** - Each surface has clear, defined purpose

## Failure Scenarios

❌ **Fails if:**
- More than 6 surfaces exist in production build
- 4-tab navigation is modified (e.g., 5 tabs, 3 tabs)
- New surface added without updating this gate definition

✅ **Passes if:**
- Exactly 6 surfaces as defined above
- 4-tab navigation working as specified
- Authority Dashboard and Call Overlay correctly layered

## Status: ✅ COMPLETE

**Verification Date:** 2026-01-10
**Verified By:** Phase 3 roadmap defines all 6 surfaces
**Evidence:** UI architecture documented in Aspire-Production-Roadmap.md:1542-1639
