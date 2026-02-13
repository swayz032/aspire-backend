/**
 * Tests for Capability Token Validator — 6-Check Validation (Law #5)
 *
 * Tests the TypeScript implementation matches the Python token_service.py
 * and validates all 6 checks per capability-token.schema.v1.yaml.
 */

import { createHmac } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  type CapabilityTokenPayload,
  clearRevocations,
  revokeToken,
  validateToken,
} from './token-validator';

// Must match the Python signing key used in tests
const TEST_SIGNING_KEY = 'test-signing-key-for-ci-only';

const SUITE_A = '00000000-0000-0000-0000-000000000001';
const SUITE_B = '00000000-0000-0000-0000-000000000002';
const OFFICE_A = '00000000-0000-0000-0000-000000000011';
const OFFICE_B = '00000000-0000-0000-0000-000000000022';

/**
 * Build canonical JSON matching Python's json.dumps(payload, sort_keys=True, separators=(",", ":"))
 */
function canonicalJson(obj: Record<string, unknown>): string {
  const sortedKeys = Object.keys(obj).sort();
  const parts: string[] = [];
  for (const key of sortedKeys) {
    const val = obj[key];
    if (Array.isArray(val)) {
      parts.push(`"${key}":[${val.map((v) => JSON.stringify(v)).join(',')}]`);
    } else {
      parts.push(`"${key}":${JSON.stringify(val)}`);
    }
  }
  return `{${parts.join(',')}}`;
}

/**
 * Mint a test token using the same algorithm as Python token_service.
 */
function mintTestToken(overrides?: Partial<CapabilityTokenPayload>): CapabilityTokenPayload {
  const now = new Date();
  const expiresAt = new Date(now.getTime() + 45_000);

  const payload: Record<string, unknown> = {
    correlation_id: '11111111-1111-1111-1111-111111111111',
    expires_at: expiresAt.toISOString().replace('Z', '+00:00'),
    issued_at: now.toISOString().replace('Z', '+00:00'),
    office_id: OFFICE_A,
    scopes: ['invoice.write'],
    suite_id: SUITE_A,
    token_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    tool: 'stripe.invoice.create',
  };

  const canonical = canonicalJson(payload);
  const hmac = createHmac('sha256', TEST_SIGNING_KEY);
  hmac.update(canonical, 'utf-8');
  const signature = hmac.digest('hex');

  return {
    token_id: payload.token_id as string,
    suite_id: payload.suite_id as string,
    office_id: payload.office_id as string,
    tool: payload.tool as string,
    scopes: payload.scopes as string[],
    issued_at: payload.issued_at as string,
    expires_at: payload.expires_at as string,
    signature,
    revoked: false,
    correlation_id: payload.correlation_id as string,
    ...overrides,
  };
}

beforeEach(() => {
  process.env.ASPIRE_TOKEN_SIGNING_KEY = TEST_SIGNING_KEY;
  clearRevocations();
});

afterEach(() => {
  clearRevocations();
});

describe('Token Validation — 6 Checks', () => {
  describe('CHECK 1: Signature Validation', () => {
    it('accepts valid signature', () => {
      const token = mintTestToken();
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
      });
      expect(result.valid).toBe(true);
      expect(result.checksPassed).toBe(6);
    });

    it('rejects tampered scopes', () => {
      const token = mintTestToken();
      token.scopes = ['admin.all'];
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'admin.all',
      });
      expect(result.valid).toBe(false);
      expect(result.error).toBe('SIGNATURE_INVALID');
      expect(result.checksPassed).toBe(0);
    });

    it('rejects tampered tool', () => {
      const token = mintTestToken();
      token.tool = 'admin.system.delete';
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
      });
      expect(result.valid).toBe(false);
      expect(result.error).toBe('SIGNATURE_INVALID');
    });

    it('rejects forged signature', () => {
      const token = mintTestToken();
      token.signature = 'a'.repeat(64);
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
      });
      expect(result.valid).toBe(false);
      expect(result.error).toBe('SIGNATURE_INVALID');
    });
  });

  describe('CHECK 2: Expiry Validation', () => {
    it('accepts active token', () => {
      const token = mintTestToken();
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
      });
      expect(result.valid).toBe(true);
    });

    it('rejects expired token (TC-05)', () => {
      const token = mintTestToken();
      const future = new Date(Date.now() + 300_000); // 5 min later
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
        now: future,
      });
      expect(result.valid).toBe(false);
      expect(result.error).toBe('TOKEN_EXPIRED');
      expect(result.checksPassed).toBe(1); // Signature passed
    });

    it('accepts just-expired within clock skew', () => {
      const token = mintTestToken();
      const expiresAt = new Date(token.expires_at);
      const justAfter = new Date(expiresAt.getTime() + 1000); // 1s after expiry
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
        now: justAfter,
      });
      expect(result.valid).toBe(true); // Within 2s tolerance
    });
  });

  describe('CHECK 3: Revocation Validation', () => {
    it('accepts unrevoked token', () => {
      const token = mintTestToken();
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
      });
      expect(result.valid).toBe(true);
    });

    it('rejects revoked token', () => {
      const token = mintTestToken();
      revokeToken(token.token_id);
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
      });
      expect(result.valid).toBe(false);
      expect(result.error).toBe('TOKEN_REVOKED');
      expect(result.checksPassed).toBe(2); // Sig + expiry passed
    });

    it('rejects token with revoked flag', () => {
      const token = mintTestToken();
      token.revoked = true;
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
      });
      expect(result.valid).toBe(false);
      expect(result.error).toBe('TOKEN_REVOKED');
    });
  });

  describe('CHECK 4: Scope Validation', () => {
    it('accepts matching scope', () => {
      const token = mintTestToken();
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
      });
      expect(result.valid).toBe(true);
    });

    it('rejects wrong scope', () => {
      const token = mintTestToken();
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'payment.transfer',
      });
      expect(result.valid).toBe(false);
      expect(result.error).toBe('SCOPE_MISMATCH');
      expect(result.checksPassed).toBe(3); // Sig + expiry + revocation
    });
  });

  describe('CHECK 5: Suite ID Validation (Tenant Isolation)', () => {
    it('accepts matching suite', () => {
      const token = mintTestToken();
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
      });
      expect(result.valid).toBe(true);
    });

    it('rejects cross-tenant token (EVIL TEST)', () => {
      const token = mintTestToken();
      const result = validateToken(token, {
        expectedSuiteId: SUITE_B, // Different tenant!
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
      });
      expect(result.valid).toBe(false);
      expect(result.error).toBe('SUITE_MISMATCH');
      expect(result.checksPassed).toBe(4);
    });
  });

  describe('CHECK 6: Office ID Validation', () => {
    it('accepts matching office', () => {
      const token = mintTestToken();
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
      });
      expect(result.valid).toBe(true);
    });

    it('rejects wrong office', () => {
      const token = mintTestToken();
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_B,
        requiredScope: 'invoice.write',
      });
      expect(result.valid).toBe(false);
      expect(result.error).toBe('OFFICE_MISMATCH');
      expect(result.checksPassed).toBe(5);
    });
  });

  describe('Edge Cases', () => {
    it('rejects malformed token (missing fields)', () => {
      const result = validateToken({ token_id: 'abc' } as CapabilityTokenPayload, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'test.read',
      });
      expect(result.valid).toBe(false);
      expect(result.error).toBe('MALFORMED_TOKEN');
    });

    it('rejects when signing key missing', () => {
      delete process.env.ASPIRE_TOKEN_SIGNING_KEY;
      const token = mintTestToken();
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
      });
      expect(result.valid).toBe(false);
      expect(result.error).toBe('MISSING_SIGNING_KEY');
    });
  });

  describe('Adversarial Scenarios', () => {
    it('replay attack after revocation fails', () => {
      const token = mintTestToken();

      // First use: valid
      const r1 = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
      });
      expect(r1.valid).toBe(true);

      // Revoke
      revokeToken(token.token_id);

      // Replay: must fail
      const r2 = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'invoice.write',
      });
      expect(r2.valid).toBe(false);
      expect(r2.error).toBe('TOKEN_REVOKED');
    });

    it('privilege escalation via scope tamper fails at signature', () => {
      const token = mintTestToken();
      token.scopes = ['admin.write', 'invoice.write']; // Tamper to add admin scope
      const result = validateToken(token, {
        expectedSuiteId: SUITE_A,
        expectedOfficeId: OFFICE_A,
        requiredScope: 'admin.write',
      });
      expect(result.valid).toBe(false);
      expect(result.error).toBe('SIGNATURE_INVALID');
    });
  });
});
