/**
 * Type validation tests for Aspire Gateway types.
 * Ensures TypeScript types match canonical schema definitions.
 */

import { describe, it, expect } from 'vitest';
import type {
  Receipt,
  CapabilityToken,
  AvaOrchestratorRequest,
  AvaResult,
  RiskTier,
  Outcome,
  ApprovalStatus,
  ActorType,
  ReceiptType,
  AspireErrorCode,
  AspireError,
} from './index.js';

describe('TypeScript types match canonical schemas', () => {
  it('RiskTier only allows green/yellow/red', () => {
    const tiers: RiskTier[] = ['green', 'yellow', 'red'];
    expect(tiers).toHaveLength(3);
    // @ts-expect-error — 'low' is not a valid RiskTier
    const invalid: RiskTier = 'low';
    void invalid; // suppress unused
  });

  it('Outcome has 5 values', () => {
    const outcomes: Outcome[] = ['success', 'denied', 'failed', 'timeout', 'pending'];
    expect(outcomes).toHaveLength(5);
  });

  it('ApprovalStatus uses rejected not denied', () => {
    const statuses: ApprovalStatus[] = ['pending', 'approved', 'rejected', 'expired', 'canceled'];
    expect(statuses).toHaveLength(5);
    expect(statuses).toContain('rejected');
    expect(statuses).not.toContain('denied');
  });

  it('Receipt has all required fields from schema', () => {
    const receipt: Receipt = {
      id: '00000000-0000-0000-0000-000000000001',
      correlationId: '00000000-0000-0000-0000-000000000002',
      suiteId: '00000000-0000-0000-0000-000000000003',
      officeId: '00000000-0000-0000-0000-000000000004',
      actorType: 'system',
      actorId: 'orchestrator',
      actionType: 'receipt.search',
      riskTier: 'green',
      toolUsed: 'receipts.search',
      createdAt: new Date().toISOString(),
      outcome: 'success',
      receiptHash: 'sha256_test',
    };
    expect(receipt.riskTier).toBe('green');
    expect(receipt.outcome).toBe('success');
  });

  it('CapabilityToken has scopes array', () => {
    const token: CapabilityToken = {
      tokenId: '00000000-0000-0000-0000-000000000001',
      suiteId: '00000000-0000-0000-0000-000000000002',
      officeId: '00000000-0000-0000-0000-000000000003',
      tool: 'stripe.invoice.create',
      scopes: ['invoice.write'],
      issuedAt: new Date().toISOString(),
      expiresAt: new Date().toISOString(),
      signature: 'hmac_test',
      revoked: false,
      correlationId: '00000000-0000-0000-0000-000000000004',
    };
    expect(token.scopes).toHaveLength(1);
    expect(token.revoked).toBe(false);
  });

  it('AvaOrchestratorRequest matches contract schema', () => {
    const req: AvaOrchestratorRequest = {
      schema_version: '1.0',
      suite_id: 'suite_test',
      office_id: 'office_test',
      request_id: 'req_test',
      correlation_id: 'corr_test',
      timestamp: new Date().toISOString(),
      task_type: 'invoice.create',
      payload: { customer: 'acme', amount: 1500 },
    };
    expect(req.schema_version).toBe('1.0');
    expect(req.task_type).toBe('invoice.create');
  });

  it('AvaResult matches contract schema', () => {
    const result: AvaResult = {
      schema_version: '1.0',
      request_id: 'req_test',
      correlation_id: 'corr_test',
      route: { skill_pack: 'invoicing', agent: 'quinn' },
      risk: { tier: 'yellow' },
      governance: {
        approvals_required: ['owner_approval'],
        presence_required: false,
        capability_token_required: true,
        receipt_ids: ['receipt_1'],
      },
      plan: { actions: [{ tool: 'stripe.invoice.create' }] },
    };
    expect(result.risk.tier).toBe('yellow');
    expect(result.governance.capability_token_required).toBe(true);
  });

  it('ReceiptType covers all 8+ types from emission rules', () => {
    const types: ReceiptType[] = [
      'decision_intake',
      'policy_decision',
      'approval_requested',
      'approval_granted',
      'approval_denied',
      'tool_execution',
      'research_run',
      'exception_card_generated',
      'ritual_generated',
    ];
    expect(types.length).toBeGreaterThanOrEqual(8);
  });

  it('AspireError has required structure', () => {
    const err: AspireError = {
      error: 'APPROVAL_REQUIRED',
      message: 'Yellow-tier action requires user confirmation',
      correlation_id: 'corr_test',
    };
    expect(err.error).toBe('APPROVAL_REQUIRED');
  });
});
