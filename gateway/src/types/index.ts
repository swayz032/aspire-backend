/**
 * Aspire Gateway Types — Generated from canonical schemas
 *
 * Source schemas:
 * - plan/schemas/receipts.schema.v1.yaml
 * - plan/schemas/capability-token.schema.v1.yaml
 * - plan/schemas/risk-tiers.enum.yaml
 * - plan/schemas/outcome-status.enum.yaml
 * - plan/schemas/approval-status.enum.yaml
 * - plan/schemas/tenant-identity.yaml
 * - plan/contracts/ava-user/ava_orchestrator_request.schema.json
 * - plan/contracts/ava-user/ava_result.schema.json
 *
 * DO NOT EDIT MANUALLY — regenerate from schemas when they change.
 */

// =============================================================================
// Enums (canonical values from YAML schemas)
// =============================================================================

/** Risk tier classification — Law #4. Use green/yellow/red, never low/medium/high. */
export type RiskTier = 'green' | 'yellow' | 'red';

/** Receipt outcome status — Law #2. Every outcome generates a receipt. */
export type Outcome = 'success' | 'denied' | 'failed' | 'timeout' | 'pending';

/** Approval status — Law #4. "rejected" (approval) vs "denied" (receipt outcome). */
export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'expired' | 'canceled';

/** Actor types — who initiated the action. */
export type ActorType = 'user' | 'system' | 'agent' | 'scheduler';

/** Approval methods — how approval was granted. */
export type ApprovalMethod = 'voice_confirm' | 'video_authority' | 'ui_button' | 'dual_approval';

// =============================================================================
// Receipt (from receipts.schema.v1.yaml)
// =============================================================================

export interface ApprovalEvidence {
  approverId: string;
  approvalMethod: ApprovalMethod;
  sessionId?: string;
  approvedAt: string;
}

/**
 * Immutable audit trail record — Law #2: No Action Without a Receipt.
 * NO UPDATE/DELETE. Corrections are new receipts.
 */
export interface Receipt {
  id: string;
  correlationId: string;
  suiteId: string;
  officeId: string;
  actorType: ActorType;
  actorId: string;
  actionType: string;
  riskTier: RiskTier;
  toolUsed: string;
  capabilityTokenId?: string;
  capabilityTokenHash?: string;
  createdAt: string;
  approvedAt?: string;
  executedAt?: string;
  approvalEvidence?: ApprovalEvidence;
  outcome: Outcome;
  reasonCode?: string;
  redactedInputs?: Record<string, unknown>;
  redactedOutputs?: Record<string, unknown>;
  previousReceiptHash?: string;
  receiptHash: string;
}

/** Receipt type categories per receipt_emission_rules.md. */
export type ReceiptType =
  | 'decision_intake'
  | 'policy_decision'
  | 'approval_requested'
  | 'approval_granted'
  | 'approval_denied'
  | 'tool_execution'
  | 'research_run'
  | 'exception_card_generated'
  | 'ritual_generated';

// =============================================================================
// Capability Token (from capability-token.schema.v1.yaml)
// =============================================================================

/**
 * Capability token — Law #5: Short-lived (<60s), scoped, server-verified.
 * Only the LangGraph orchestrator mints tokens.
 */
export interface CapabilityToken {
  tokenId: string;
  suiteId: string;
  officeId: string;
  tool: string;
  scopes: string[];
  issuedAt: string;
  expiresAt: string;
  signature: string;
  revoked: boolean;
  correlationId: string;
}

// =============================================================================
// AvaOrchestratorRequest (from ava_orchestrator_request.schema.json)
// =============================================================================

/**
 * Inbound request to the orchestrator — POST /v1/intents.
 * Note: suite_id/office_id in the request are validated against auth context.
 * The orchestrator derives the authoritative suite_id from JWT, NOT from this payload.
 */
export interface AvaOrchestratorRequest {
  schema_version: '1.0';
  suite_id: string;
  office_id: string;
  request_id: string;
  correlation_id: string;
  timestamp: string;
  task_type: string;
  payload: Record<string, unknown>;
}

// =============================================================================
// AvaResult (from ava_result.schema.json)
// =============================================================================

/** Route information — which skill pack/agent handled the request. */
export interface AvaResultRoute {
  skill_pack?: string;
  agent?: string;
  tool?: string;
  [key: string]: unknown;
}

/** Risk assessment included in the result. */
export interface AvaResultRisk {
  tier: RiskTier;
  [key: string]: unknown;
}

/** Governance metadata — approvals, tokens, receipt chain. */
export interface AvaResultGovernance {
  approvals_required: string[];
  presence_required: boolean;
  capability_token_required: boolean;
  receipt_ids: string[];
}

/** Plan details returned to the client. */
export interface AvaResultPlan {
  [key: string]: unknown;
}

/**
 * Response from the orchestrator — returned after processing an intent.
 * Validated against schema before returning (egress validation).
 */
export interface AvaResult {
  schema_version: '1.0';
  request_id: string;
  correlation_id: string;
  route: AvaResultRoute;
  risk: AvaResultRisk;
  governance: AvaResultGovernance;
  plan: AvaResultPlan;
}

// =============================================================================
// Error Codes (from architecture.md fail-closed error codes)
// =============================================================================

export type AspireErrorCode =
  | 'SCHEMA_VALIDATION_FAILED'
  | 'APPROVAL_REQUIRED'
  | 'PRESENCE_REQUIRED'
  | 'CAPABILITY_TOKEN_REQUIRED'
  | 'CAPABILITY_TOKEN_EXPIRED'
  | 'TENANT_ISOLATION_VIOLATION'
  | 'POLICY_DENIED'
  | 'SAFETY_BLOCKED'
  | 'RECEIPT_WRITE_FAILED'
  | 'INTERNAL_ERROR';

/** Structured error response from the gateway/orchestrator. */
export interface AspireError {
  error: AspireErrorCode;
  message: string;
  correlation_id: string;
  receipt_id?: string;
}
