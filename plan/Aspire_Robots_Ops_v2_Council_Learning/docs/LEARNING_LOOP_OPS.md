# Ops: Learning Loop Integration

Date: 2026-02-01

## Purpose
Turn incidents and robot failures into permanent improvements:
- runbooks
- eval cases
- robot assertions
- (optional) policy/router/prompt proposals

## Trigger Sources
1) Robot regression fails (pre-release) → open incident
2) Production monitor alert → open incident
3) Provider webhook failure → open incident

## Required outputs after resolution
- incident summary (curated)
- runbook update
- new robot assertion or suite update
- eval case(s) for tool-plan/policy regression prevention

## Governance
All changes go through:
proposal → eval/robot verification → approval → canary → promote/rollback

Receipts:
- learning.object.created
- eval.run.completed
- robot.regression.completed
- learning.change.proposed / approved / object.promoted
