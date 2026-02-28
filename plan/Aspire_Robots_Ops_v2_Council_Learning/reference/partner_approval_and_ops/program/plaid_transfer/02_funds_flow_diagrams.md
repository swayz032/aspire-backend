# Funds Flow Diagrams (Mermaid Templates)

## Platform flow
```mermaid
flowchart LR
  UI[Aspire UI] --> B[Brain]
  B --> GW[Gateway]
  GW --> TS[Trust Spine]
  TS --> OB[Outbox]
  OB --> EX[Executor]
  EX -->|authorization| PLAID[Plaid Transfer]
  EX -->|create| PLAID
  PLAID --> BANKS[ACH Rails/Banks]
```

## Notes
- Always authorization before create
- log both steps with receipts linked by trace_id
