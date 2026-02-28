# Receipt Emission Rules

**Source:** Ava User Enterprise Handoff v1.1

## Receipt types
At minimum, persist these receipt types (append-only):
1. `decision_intake`
2. `policy_decision`
3. `approval_requested`
4. `approval_granted` / `approval_denied`
5. `tool_execution`
6. `research_run`
7. `exception_card_generated`
8. `ritual_generated`

## Required fields (minimum)
- `receipt_id` (ULID/UUID)
- `type`
- `timestamp`
- `suite_id`, `office_id`
- `request_id`, `correlation_id`
- `actor` (user | system | skill_pack)
- `payload_hash` (for approvals and executions)
- `status` (ok | denied | error)
- `error_code` (optional)
- `refs` (links to artifacts: emails drafted, invoices, transcripts, etc.)

## Integrity
- Receipts must be immutable after write.
- Hash-chain receipts per suite to support replay verification (see `plan/specs/ava-admin/receipt_chain_spec.md`).

## Runtime Integration

### Ingress
- Validate AvaOrchestratorRequest against JSON Schema at the edge.
- Derive `suite_id` / `office_id` from auth context (do not trust client provided ids).

### Governance
- Compute candidate plan.
- Evaluate policy deterministically.
- If approval required: return ApprovalRequest draft (no execution).
- If allowed: require capability token for execution.

### Receipts
- Write receipts for: intake, policy decision, approvals, presence verification, tool execution.
- If receipts cannot be written: fail closed and degrade to draft-only.

### Egress
- Validate AvaResult schema before returning.

## Cross-reference
- Canonical receipts schema: `plan/schemas/receipts.schema.v1.yaml`
- Receipt hash chain spec: `plan/specs/ava-admin/receipt_chain_spec.md`
- AvaOrchestratorRequest: `plan/contracts/ava-user/ava_orchestrator_request.schema.json`
- AvaResult: `plan/contracts/ava-user/ava_result.schema.json`
