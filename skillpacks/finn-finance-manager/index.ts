/**
 * Finn Finance Manager — Skillpack Entry Point
 *
 * Proposal-only skillpack for strategic finance management.
 * Finn reads snapshots/exceptions, creates proposals, and delegates via A2A.
 * All outputs validate against schemas/06_output_schema.json.
 */

export const SKILLPACK_ID = 'finn-finance-manager';
export const VERSION = 'v1';

export interface FinnProposal {
  agent: 'finn-finance-manager';
  suite_id: string;
  office_id: string;
  intent_summary: string;
  risk_tier: 'green' | 'yellow' | 'red';
  required_approval_mode: 'none' | 'admin' | 'owner' | 'ava_video';
  correlation_id?: string;
  proposals: Array<{
    action: string;
    inputs: Record<string, unknown>;
    inputs_hash: string;
    rationale?: string;
    expected_receipts?: string[];
  }>;
  escalations: Array<{
    kind: string;
    severity: 'info' | 'warn' | 'error';
    summary: string;
    details?: Record<string, unknown>;
    recommended_next_action?: string;
  }>;
}

export interface FinnA2ADelegation {
  action: 'a2a.create';
  inputs: {
    to_agent: 'adam' | 'teressa' | 'milo' | 'eli';
    request_type: 'ResearchRequest' | 'BookkeepingRequest' | 'PayrollRequest' | 'InboxRequest';
    payload: Record<string, unknown>;
    risk_tier: 'green' | 'yellow' | 'red';
    correlation_id: string;
  };
  inputs_hash: string;
  rationale: string;
}

/** Allowed A2A delegation targets */
export const ALLOWED_DELEGATION_AGENTS = ['adam', 'teressa', 'milo', 'eli'] as const;

/** Maximum delegation depth (Finn → Ava → target) */
export const MAX_DELEGATION_DEPTH = 2;

/** Proposal actions this skillpack can produce */
export const PROPOSAL_ACTIONS = [
  'finance.proposal.create',
  'finance.packet.draft',
  'a2a.create',
] as const;

/** Receipt events this skillpack emits */
export const RECEIPT_EVENTS = [
  'finance.snapshot.read',
  'finance.exceptions.read',
  'finance.proposal.created',
  'a2a.item.created',
] as const;
