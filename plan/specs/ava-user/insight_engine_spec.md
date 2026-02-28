# Insight Engine Specification

**Source:** Ava User Enterprise Handoff v1.1

## Overview
Ava generates ExceptionCards proactively based on business data patterns.

## ExceptionCard Structure
- Finding (what was detected)
- Evidence references (receipt_ids, data points)
- Impact estimate (quantified business impact)
- Confidence (0-1 score)
- Draftable next actions (proposed responses)

## Triggers
- Cash risk (cash flow projections below threshold)
- AR overdue spike (accounts receivable aging beyond SLA)
- Close blockers (month-end reconciliation issues)
- Scheduling conflicts (overlapping commitments)

## Governance
- ExceptionCards are draft artifacts unless explicitly approved for execution
- Each card generates an `exception_card_generated` receipt
- Actions derived from cards follow standard risk tier approval flows

## Cross-reference
- OpsExceptionCard schema (admin-side): `plan/contracts/ava-admin/ops_exception_card.schema.json`
- Implementation target: Phase 3+ (Certification)
