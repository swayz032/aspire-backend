# Operator / Engineer Toggle (Admin Global Language Mode)

**Source:** Ava Admin Enterprise Handoff v2

## Overview
This toggle switches the entire admin between:
- **Operator Mode** (plain English, business concepts)
- **Engineer Mode** (technical primitives)

This is global state per admin user.

## Affects
- Labels
- Logs
- Receipts rendering
- Errors
- Metrics naming

## Operator Mode examples
- "Approval Needed" instead of "HTTP 403"
- "Money Risk: High" instead of "policy.money.tier=3"

## Engineer Mode
Exposes raw system objects (IDs, diffs, policy keys).

## Requirements
- Must persist per admin user
- Must not change underlying truth (same data, different rendering)
- Both modes must be tested against all Admin Portal routes

## Cross-reference
- Admin Portal route map: `plan/contracts/ava-admin/admin_portal_map.json` (Phase 2)
- Implementation target: Phase 2 (Admin Portal wiring)
