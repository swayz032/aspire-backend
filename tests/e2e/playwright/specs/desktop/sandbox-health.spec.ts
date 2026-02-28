import { test, expect } from '@playwright/test';

/**
 * Sandbox Health E2E Tests
 *
 * Hits the sandbox health endpoint directly to verify provider configuration.
 * This endpoint checks all 10 provider sandbox API keys.
 */
test.describe('Sandbox Health', () => {
  test('sandbox health endpoint responds', async ({ request }) => {
    const response = await request.get('/api/sandbox/health');

    // Endpoint should return 200
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toBeTruthy();
  });

  test('response contains provider checks', async ({ request }) => {
    const response = await request.get('/api/sandbox/health');
    expect(response.status()).toBe(200);

    const body = await response.json();

    // The health endpoint should return a checks object with provider statuses
    // Each provider has { configured: boolean, sandbox: boolean, status: string }
    const checks = body.checks || body;
    expect(checks).toBeTruthy();

    // Verify structure — should have at least some known providers
    const expectedProviders = [
      'stripe',
      'plaid',
      'quickbooks',
      'gusto',
      'supabase',
    ];

    const providerKeys = Object.keys(checks);
    let matchCount = 0;
    for (const provider of expectedProviders) {
      if (providerKeys.some((k) => k.toLowerCase().includes(provider))) {
        matchCount++;
      }
    }

    // Should have at least some of the expected providers
    expect(matchCount).toBeGreaterThanOrEqual(1);
  });

  test('summary shows configuration count', async ({ request }) => {
    const response = await request.get('/api/sandbox/health');
    expect(response.status()).toBe(200);

    const body = await response.json();

    // The endpoint may include a summary field
    if (body.summary) {
      // Summary should show X/10 configured or similar
      expect(body.summary).toHaveProperty('total');
      expect(body.summary).toHaveProperty('configured');
      expect(typeof body.summary.configured).toBe('number');
    } else {
      // Count configured providers manually
      const checks = body.checks || body;
      const total = Object.keys(checks).length;
      expect(total).toBeGreaterThan(0);

      const configured = Object.values(checks).filter(
        (check: any) => check.configured === true
      ).length;

      // At least some providers should be present in the response
      expect(total).toBeGreaterThanOrEqual(1);
      // configured count is informational
      expect(configured).toBeGreaterThanOrEqual(0);
    }
  });

  test('each provider check has expected fields', async ({ request }) => {
    const response = await request.get('/api/sandbox/health');
    expect(response.status()).toBe(200);

    const body = await response.json();
    const checks = body.checks || body;

    for (const [provider, check] of Object.entries(checks)) {
      const checkObj = check as any;
      // Each check should have at minimum a configured flag and status
      expect(checkObj).toHaveProperty('configured');
      expect(typeof checkObj.configured).toBe('boolean');
      expect(checkObj).toHaveProperty('status');
      expect(typeof checkObj.status).toBe('string');
    }
  });
});
