# Research Routing Specification (Adam Integration)

**Source:** Ava User Enterprise Handoff v1.1

## Principles
- Research is read-only by default
- Finite shortlist (5-10 results)
- Evidence references required
- Uncertainty disclosure mandatory
- All research runs produce ResearchReceipt
- n8n may schedule research but must call internal endpoints only (Law #7)

## Governance
- Research routing is green tier (read-only)
- Still requires receipts (`research_run` type)
- n8n scheduling follows request-only model (never decides)

## Cross-reference
- Adam (Research Specialist) agent roster
- n8n governance: CLAUDE.md Law #1, #7
- Implementation target: Phase 3+ (placeholder-grade, needs refinement)
