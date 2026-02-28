# Architecture and Flow

**Source:** Ava User Enterprise Handoff v1.1

## Service boundaries (minimum)
- **Client UI (Desktop/Mobile/Web):** renders drafts, requests approvals, never executes tools.
- **Orchestrator API:** validates requests, creates plan, routes to Skill Packs.
- **Policy Engine:** evaluates allowlists, risk tiers, approvals, presence, and capability tokens.
- **Execution Workers (Skill Packs):** perform allowed tool calls; cannot override policy.
- **Receipts Ledger:** append-only event log for decisions, approvals, executions, and research.

## Core request flow (sequence)
```mermaid
sequenceDiagram
  participant UI as Client UI
  participant OR as Orchestrator API
  participant PO as Policy Engine
  participant SP as Skill Pack Worker
  participant TR as Tool Rail (external APIs)
  participant RL as Receipt Ledger

  UI->>OR: AvaOrchestratorRequest (JSON)
  OR->>RL: Receipt(decision_intake)
  OR->>PO: policy.evaluate(plan, context)
  PO->>RL: Receipt(policy_decision)
  alt Policy allows + approvals satisfied
    OR->>SP: execute(plan, capability_token)
    SP->>TR: tool call(s)
    TR-->>SP: result / error
    SP->>RL: Receipt(tool_execution)
    SP-->>OR: results + receipt refs
    OR-->>UI: AvaResult (drafts + receipts)
  else Approval required
    OR-->>UI: ApprovalRequest (draft) + payload_hash
  else Denied
    OR-->>UI: PolicyDenied (machine-readable)
  end
```

## Fail-closed error codes
- `SCHEMA_VALIDATION_FAILED` — Any schema mismatch
- `APPROVAL_REQUIRED` — Any missing approval
- `CAPABILITY_TOKEN_REQUIRED` — Any missing/expired capability token for execution
- `TENANT_ISOLATION_VIOLATION` — Any tenant boundary mismatch

## Risk tier model
Use **green/yellow/red** (not "low/medium/high"). Red-tier requires presence proof.

## Cross-reference
- AvaOrchestratorRequest: `plan/contracts/ava-user/ava_orchestrator_request.schema.json`
- AvaResult: `plan/contracts/ava-user/ava_result.schema.json`
- Policy engine: `plan/specs/ava-user/policy_engine_spec.md`
- Error codes adopted as starting taxonomy for Phase 1
