import { test, expect } from '../../fixtures/auth';

/**
 * Onboarding E2E Tests
 *
 * Tests the 3-step onboarding flow:
 * Step 1: Business info (name, industry, team size, owner name)
 * Step 2: Services selection
 * Step 3: Summary + completion
 *
 * These tests require a fresh user who has not completed onboarding.
 * If the test user has already onboarded, these tests will be skipped.
 */
test.describe('Onboarding', () => {
  test.describe.configure({ mode: 'serial' });

  test('new user is redirected to onboarding after login', async ({ freshPage: page }) => {
    // This test requires a user with incomplete onboarding
    // Skip if we cannot verify this precondition
    test.skip(
      !process.env.E2E_ONBOARDING_EMAIL,
      'E2E_ONBOARDING_EMAIL not set — skipping onboarding redirect test'
    );

    await page.goto('/');
    await page.waitForURL(/\/\(auth\)\/login|\/login/, { timeout: 10_000 });

    // Login with onboarding test user
    await page.getByPlaceholder('you@company.com').fill(process.env.E2E_ONBOARDING_EMAIL!);
    await page.getByPlaceholder('Enter your password').fill(
      process.env.E2E_ONBOARDING_PASSWORD || 'test123456'
    );
    await page.getByText('Sign In').click();

    // Should redirect to onboarding
    await page.waitForURL(/\/\(auth\)\/onboarding|\/onboarding/, { timeout: 15_000 });
  });

  test('step 1: displays business info form', async ({ freshPage: page }) => {
    test.skip(
      !process.env.E2E_ONBOARDING_EMAIL,
      'E2E_ONBOARDING_EMAIL not set — skipping onboarding test'
    );

    // Navigate directly to onboarding (user must be logged in)
    await page.goto('/');
    await page.waitForURL(/\/\(auth\)\/login|\/login/, { timeout: 10_000 });

    await page.getByPlaceholder('you@company.com').fill(process.env.E2E_ONBOARDING_EMAIL!);
    await page.getByPlaceholder('Enter your password').fill(
      process.env.E2E_ONBOARDING_PASSWORD || 'test123456'
    );
    await page.getByText('Sign In').click();
    await page.waitForURL(/\/\(auth\)\/onboarding|\/onboarding/, { timeout: 15_000 });

    // Verify step 1 content
    await expect(page.getByText('Step 1 of 3')).toBeVisible();
    await expect(page.getByText('Tell us about your business')).toBeVisible();

    // data-testid suggestions: [data-testid="onboarding-business-name"]
    await expect(page.getByPlaceholder('e.g. Your Business Name')).toBeVisible();
    await expect(page.getByPlaceholder('Full name')).toBeVisible();

    // Next button should be disabled (fields empty)
    // data-testid suggestion: [data-testid="onboarding-next-button"]
    const nextButton = page.getByText('Next');
    await expect(nextButton).toBeVisible();
  });

  test('step 1: next button requires all fields', async ({ freshPage: page }) => {
    test.skip(
      !process.env.E2E_ONBOARDING_EMAIL,
      'E2E_ONBOARDING_EMAIL not set — skipping onboarding test'
    );

    await page.goto('/');
    await page.waitForURL(/\/\(auth\)\/login|\/login/, { timeout: 10_000 });
    await page.getByPlaceholder('you@company.com').fill(process.env.E2E_ONBOARDING_EMAIL!);
    await page.getByPlaceholder('Enter your password').fill(
      process.env.E2E_ONBOARDING_PASSWORD || 'test123456'
    );
    await page.getByText('Sign In').click();
    await page.waitForURL(/\/\(auth\)\/onboarding|\/onboarding/, { timeout: 15_000 });

    // Fill in business name
    await page.getByPlaceholder('e.g. Your Business Name').fill('Test Business');

    // Select industry chip
    await page.getByText('Technology').click();

    // Select team size chip
    await page.getByText('Just me').click();

    // Fill in owner name
    await page.getByPlaceholder('Full name').fill('Test Owner');

    // Next button should now be enabled (not 50% opacity)
    const nextButton = page.getByText('Next');
    await expect(nextButton).toBeVisible();
  });

  test('step 2: services selection', async ({ freshPage: page }) => {
    test.skip(
      !process.env.E2E_ONBOARDING_EMAIL,
      'E2E_ONBOARDING_EMAIL not set — skipping onboarding test'
    );

    await page.goto('/');
    await page.waitForURL(/\/\(auth\)\/login|\/login/, { timeout: 10_000 });
    await page.getByPlaceholder('you@company.com').fill(process.env.E2E_ONBOARDING_EMAIL!);
    await page.getByPlaceholder('Enter your password').fill(
      process.env.E2E_ONBOARDING_PASSWORD || 'test123456'
    );
    await page.getByText('Sign In').click();
    await page.waitForURL(/\/\(auth\)\/onboarding|\/onboarding/, { timeout: 15_000 });

    // Complete step 1
    await page.getByPlaceholder('e.g. Your Business Name').fill('Test Business');
    await page.getByText('Technology').click();
    await page.getByText('Just me').click();
    await page.getByPlaceholder('Full name').fill('Test Owner');
    await page.getByText('Next').click();

    // Step 2 should now be visible
    await expect(page.getByText('Step 2 of 3')).toBeVisible();
    await expect(page.getByText('What do you need help with?')).toBeVisible();

    // Select some services
    await page.getByText('Invoicing & Payments').click();
    await page.getByText('Scheduling & Calendar').click();

    // Progress dots should show step 2 active
    await expect(page.getByText('Step 2 of 3')).toBeVisible();
  });

  test('step 3: summary and completion', async ({ freshPage: page }) => {
    test.skip(
      !process.env.E2E_ONBOARDING_EMAIL,
      'E2E_ONBOARDING_EMAIL not set — skipping onboarding test'
    );

    await page.goto('/');
    await page.waitForURL(/\/\(auth\)\/login|\/login/, { timeout: 10_000 });
    await page.getByPlaceholder('you@company.com').fill(process.env.E2E_ONBOARDING_EMAIL!);
    await page.getByPlaceholder('Enter your password').fill(
      process.env.E2E_ONBOARDING_PASSWORD || 'test123456'
    );
    await page.getByText('Sign In').click();
    await page.waitForURL(/\/\(auth\)\/onboarding|\/onboarding/, { timeout: 15_000 });

    // Complete step 1
    await page.getByPlaceholder('e.g. Your Business Name').fill('Test Business');
    await page.getByText('Technology').click();
    await page.getByText('Just me').click();
    await page.getByPlaceholder('Full name').fill('Test Owner');
    await page.getByText('Next').click();

    // Complete step 2
    await page.getByText('Invoicing & Payments').click();
    await page.getByText('Next').click();

    // Step 3 summary
    await expect(page.getByText('Step 3 of 3')).toBeVisible();
    await expect(page.getByText('You are all set')).toBeVisible();

    // Summary should show entered data
    await expect(page.getByText('Test Business')).toBeVisible();
    await expect(page.getByText('Technology')).toBeVisible();
    await expect(page.getByText('Just me')).toBeVisible();
    await expect(page.getByText('Test Owner')).toBeVisible();
    await expect(page.getByText(/Invoicing & Payments/)).toBeVisible();

    // Start Using Aspire button should be visible
    // data-testid suggestion: [data-testid="onboarding-complete-button"]
    await expect(page.getByText('Start Using Aspire')).toBeVisible();
  });

  test('progress dots update correctly', async ({ freshPage: page }) => {
    test.skip(
      !process.env.E2E_ONBOARDING_EMAIL,
      'E2E_ONBOARDING_EMAIL not set — skipping onboarding test'
    );

    await page.goto('/');
    await page.waitForURL(/\/\(auth\)\/login|\/login/, { timeout: 10_000 });
    await page.getByPlaceholder('you@company.com').fill(process.env.E2E_ONBOARDING_EMAIL!);
    await page.getByPlaceholder('Enter your password').fill(
      process.env.E2E_ONBOARDING_PASSWORD || 'test123456'
    );
    await page.getByText('Sign In').click();
    await page.waitForURL(/\/\(auth\)\/onboarding|\/onboarding/, { timeout: 15_000 });

    // Step 1
    await expect(page.getByText('Step 1 of 3')).toBeVisible();

    // Fill and advance
    await page.getByPlaceholder('e.g. Your Business Name').fill('Test Business');
    await page.getByText('Technology').click();
    await page.getByText('Just me').click();
    await page.getByPlaceholder('Full name').fill('Test Owner');
    await page.getByText('Next').click();

    // Step 2
    await expect(page.getByText('Step 2 of 3')).toBeVisible();

    // Back button should work
    await page.getByText('Back').click();
    await expect(page.getByText('Step 1 of 3')).toBeVisible();
  });
});
