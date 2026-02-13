/**
 * Capability Token Validator — 6-Check Server-Side Validation (Law #5)
 *
 * Per capability-token.schema.v1.yaml:
 *   1. Signature valid (HMAC-SHA256 verification)
 *   2. Not expired (current_time < expires_at)
 *   3. Not revoked (revoked = false)
 *   4. Scope matches requested action
 *   5. suite_id matches request context
 *   6. office_id matches request context
 *
 * Failure action: Return 403 Forbidden + generate denial receipt
 */

import { createHmac, timingSafeEqual } from 'node:crypto';

// Clock skew tolerance for expiry checks (seconds)
const CLOCK_SKEW_TOLERANCE_SECONDS = 2;

// In-memory revocation set (Phase 1 — moves to DB/Redis in Phase 2)
const revokedTokens = new Set<string>();

export type TokenValidationError =
  | 'SIGNATURE_INVALID'
  | 'TOKEN_EXPIRED'
  | 'TOKEN_REVOKED'
  | 'SCOPE_MISMATCH'
  | 'SUITE_MISMATCH'
  | 'OFFICE_MISMATCH'
  | 'MISSING_SIGNING_KEY'
  | 'MALFORMED_TOKEN';

export interface TokenValidationResult {
  valid: boolean;
  error?: TokenValidationError;
  errorMessage?: string;
  checksPassed: number;
}

/** Wire format from orchestrator (snake_case JSON) */
export interface CapabilityTokenPayload {
  token_id: string;
  suite_id: string;
  office_id: string;
  tool: string;
  scopes: string[];
  issued_at: string;
  expires_at: string;
  signature: string;
  revoked?: boolean;
  correlation_id: string;
}

/**
 * Get the signing key from environment. Fail closed if not configured (Law #3).
 */
function getSigningKey(): string {
  const key = process.env.ASPIRE_TOKEN_SIGNING_KEY ?? '';
  if (!key) {
    throw new Error(
      'ASPIRE_TOKEN_SIGNING_KEY not configured. ' +
        'Cannot validate capability tokens without a signing key. ' +
        'Fail-closed per Law #3.',
    );
  }
  return key;
}

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
 * Compute HMAC-SHA256 matching the Python implementation exactly.
 */
function computeHmacSignature(payload: Record<string, unknown>, signingKey: string): string {
  const canonical = canonicalJson(payload);
  const hmac = createHmac('sha256', signingKey);
  hmac.update(canonical, 'utf-8');
  return hmac.digest('hex');
}

const REQUIRED_FIELDS: (keyof CapabilityTokenPayload)[] = [
  'token_id',
  'suite_id',
  'office_id',
  'tool',
  'scopes',
  'issued_at',
  'expires_at',
  'signature',
  'correlation_id',
];

/**
 * Perform 6-check server-side token validation.
 *
 * Per capability-token.schema.v1.yaml:
 *   1. Signature valid (HMAC-SHA256 verification)
 *   2. Not expired (current_time < expires_at)
 *   3. Not revoked (revoked = false)
 *   4. Scope matches requested action
 *   5. suite_id matches request context
 *   6. office_id matches request context
 */
export function validateToken(
  token: CapabilityTokenPayload,
  options: {
    expectedSuiteId: string;
    expectedOfficeId: string;
    requiredScope: string;
    now?: Date;
  },
): TokenValidationResult {
  const now = options.now ?? new Date();
  let checksPassed = 0;

  // Pre-check: required fields present
  for (const field of REQUIRED_FIELDS) {
    if (!(field in token) || token[field] === undefined || token[field] === null) {
      return {
        valid: false,
        error: 'MALFORMED_TOKEN',
        errorMessage: `Missing required field: ${field}`,
        checksPassed,
      };
    }
  }

  // CHECK 1: Signature valid (HMAC-SHA256 verification)
  let signingKey: string;
  try {
    signingKey = getSigningKey();
  } catch {
    return {
      valid: false,
      error: 'MISSING_SIGNING_KEY',
      errorMessage: 'Token signing key not configured',
      checksPassed,
    };
  }

  const payloadForSigning: Record<string, unknown> = {
    token_id: token.token_id,
    suite_id: token.suite_id,
    office_id: token.office_id,
    tool: token.tool,
    scopes: [...token.scopes].sort(),
    issued_at: token.issued_at,
    expires_at: token.expires_at,
    correlation_id: token.correlation_id,
  };

  const expectedSignature = computeHmacSignature(payloadForSigning, signingKey);

  // Timing-safe comparison to prevent timing attacks
  const sigBuf = Buffer.from(token.signature, 'utf-8');
  const expBuf = Buffer.from(expectedSignature, 'utf-8');
  if (sigBuf.length !== expBuf.length || !timingSafeEqual(sigBuf, expBuf)) {
    return {
      valid: false,
      error: 'SIGNATURE_INVALID',
      errorMessage: 'HMAC-SHA256 signature verification failed',
      checksPassed,
    };
  }
  checksPassed++;

  // CHECK 2: Not expired (current_time < expires_at)
  const expiresAt = new Date(token.expires_at);
  if (isNaN(expiresAt.getTime())) {
    return {
      valid: false,
      error: 'TOKEN_EXPIRED',
      errorMessage: `Invalid expires_at format: ${token.expires_at}`,
      checksPassed,
    };
  }

  const expiresAtWithSkew = new Date(expiresAt.getTime() + CLOCK_SKEW_TOLERANCE_SECONDS * 1000);
  if (now > expiresAtWithSkew) {
    return {
      valid: false,
      error: 'TOKEN_EXPIRED',
      errorMessage: `Token expired at ${token.expires_at}, current time ${now.toISOString()}`,
      checksPassed,
    };
  }
  checksPassed++;

  // CHECK 3: Not revoked (revoked = false)
  if (token.revoked || revokedTokens.has(token.token_id)) {
    return {
      valid: false,
      error: 'TOKEN_REVOKED',
      errorMessage: `Token ${token.token_id.substring(0, 8)}... has been revoked`,
      checksPassed,
    };
  }
  checksPassed++;

  // CHECK 4: Scope matches requested action
  const tokenScopes = new Set(token.scopes);
  if (!tokenScopes.has(options.requiredScope)) {
    // Check for wildcard scope (domain.*)
    const scopeDomain = options.requiredScope.includes('.')
      ? options.requiredScope.split('.')[0]
      : options.requiredScope;
    const wildcardScope = `${scopeDomain}.*`;
    if (!tokenScopes.has(wildcardScope)) {
      return {
        valid: false,
        error: 'SCOPE_MISMATCH',
        errorMessage: `Required scope '${options.requiredScope}' not in token scopes [${[...tokenScopes].sort().join(', ')}]`,
        checksPassed,
      };
    }
  }
  checksPassed++;

  // CHECK 5: suite_id matches request context
  if (token.suite_id !== options.expectedSuiteId) {
    return {
      valid: false,
      error: 'SUITE_MISMATCH',
      errorMessage: 'Token suite_id does not match request context',
      checksPassed,
    };
  }
  checksPassed++;

  // CHECK 6: office_id matches request context
  if (token.office_id !== options.expectedOfficeId) {
    return {
      valid: false,
      error: 'OFFICE_MISMATCH',
      errorMessage: 'Token office_id does not match request context',
      checksPassed,
    };
  }
  checksPassed++;

  // All 6 checks passed
  return { valid: true, checksPassed: 6 };
}

/**
 * Revoke a token by ID. Phase 1: in-memory. Phase 2: DB/Redis.
 */
export function revokeToken(tokenId: string): void {
  revokedTokens.add(tokenId);
}

/**
 * Check if a token is revoked.
 */
export function isRevoked(tokenId: string): boolean {
  return revokedTokens.has(tokenId);
}

/**
 * Clear revocation set. Testing only.
 */
export function clearRevocations(): void {
  revokedTokens.clear();
}
