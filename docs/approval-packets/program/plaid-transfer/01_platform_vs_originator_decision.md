# Plaid Transfer: Platform vs Originator Decision

Fill this out before applying for Plaid Transfer access.

## Decision Questions

1. **Who is the sender on the ACH file?**
   - Aspire operates as a **Platform** — our customers (small businesses) are the originators. Aspire facilitates the transfer on their behalf via Finn (Money Desk).

2. **Whose bank account funds the transfer?**
   - The customer's (suite owner's) linked bank account. Aspire does not hold or pool funds.

3. **Who bears returns risk?**
   - The customer (originator) bears primary returns risk. Aspire enforces limits, monitoring, and escalation policies to mitigate risk.

4. **Who owns customer support for returns?**
   - Aspire provides first-line support via Ava orchestrator + Finn (Money Desk). Escalation to Plaid for provider-level issues.

5. **Are you facilitating transfers for your customers (platform) or your own business (originator)?**
   - **Platform** — Aspire facilitates ACH transfers for small business customers. Each customer is a separate originator with isolated funds flow.

## Decision Output
- **Chosen mode**: Platform
- **Rationale**: Aspire is a multi-tenant platform serving small businesses. Each suite (tenant) originates their own transfers. Aspire provides the governed execution infrastructure but does not originate transfers itself.
- **Operational owner for returns/fraud**: Aspire engineering team (first-line), with escalation to Plaid and compliance counsel as needed. Kill switch available per-tenant (`docs/operations/kill_switch.md`).
