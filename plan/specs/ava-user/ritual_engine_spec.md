# Ritual Engine Specification

**Source:** Ava User Enterprise Handoff v1.1

## Overview
Scheduled check-ins and retention loops that Ava initiates to keep the business owner engaged and informed.

## Weekly Review
- Cash summary
- AR/AP status
- Open loops (unresolved items)
- Top 3 recommended actions

## Monthly Close
- Reconciliation status
- Missing documents
- Draft follow-ups

## Governance
- Ritual outputs are draft artifacts unless explicitly approved
- Each ritual generates a `ritual_generated` receipt
- Rituals never execute actions autonomously (Law #1: Single Brain)

## Cross-reference
- Receipt emission rules: `plan/specs/ava-user/receipt_emission_rules.md`
- Implementation target: Phase 3+ (Certification)
