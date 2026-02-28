# Funds Flow Diagrams — Plaid Transfer

## Platform Flow (Aspire)

```mermaid
flowchart LR
  UI[Aspire UI / Ava Voice] --> ORCH[Ava Orchestrator<br/>LangGraph :8000]
  ORCH --> GW[Express Gateway<br/>:3100]
  GW --> TS[Trust Spine<br/>Policy + Approval]
  TS --> TOKEN[Capability Token<br/>< 60s, scoped]
  TOKEN --> FINN[Finn Money Desk<br/>Tool Executor]
  FINN -->|authorization| PLAID[Plaid Transfer API]
  FINN -->|create| PLAID
  PLAID --> BANKS[ACH Rails / Banks]
```

## Transfer Lifecycle

```mermaid
stateDiagram-v2
  [*] --> Requested: User requests transfer
  Requested --> Authorized: Plaid authorization approved
  Authorized --> Created: Transfer created via Plaid API
  Created --> Pending: ACH processing
  Pending --> Settled: Funds settled
  Pending --> Returned: ACH return received
  Settled --> [*]
  Returned --> Reviewed: Policy evaluation
  Reviewed --> [*]
```

## Key Rules
- Always call Plaid `authorization` endpoint before `create` — deny/escalate on declined authorization (Law #3).
- Log both authorization and create steps with receipts linked by `trace_id` (Law #2).
- Finn (Money Desk) executes via capability token — never decides autonomously (Law #7).
- All transfer operations are RED tier — require explicit authority + approval (Law #4).
- Payment state machine (`backend/orchestrator/services/state_machines/payment.py`) governs the lifecycle.
- Dual-approval required for amounts >$10K — same-approver bypass is blocked.
