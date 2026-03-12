import { test, expect } from '@playwright/test';

/**
 * Login E2E Tests
 *
 * Tests the (auth)/login screen: redirection, form validation,
 * successful sign-in, and error handling.
 */
test.describe('Login', () => {
  const signInButton = (page: any) =>
    page.getByText(/^Sign In$/).last();

  test('redirects unauthenticated user to login page', async ({ page }) => {
    await page.goto('/');
    // Auth gate should redirect to login
    await page.waitForURL(/\/\(auth\)\/login|\/login/, { timeout: 10_000 });

    // Verify login page content is visible
    // data-testid suggestion: [data-testid="login-brand-name"]
    await expect(page.getByText(/^Aspire(?:\s+—\s+Private Beta)?$/).first()).toBeVisible();
    await expect(page.getByText('Governed AI execution for your business')).toBeVisible();
  });

  test('displays email and password fields', async ({ page }) => {
    await page.goto('/');
    await page.waitForURL(/\/\(auth\)\/login|\/login/, { timeout: 10_000 });

    // data-testid suggestions: [data-testid="login-email-input"], [data-testid="login-password-input"]
    const emailInput = page.getByPlaceholder('you@company.com');
    const passwordInput = page.getByPlaceholder('Enter your password');

    await expect(emailInput).toBeVisible();
    await expect(passwordInput).toBeVisible();
  });

  test('shows error on empty form submission', async ({ page }) => {
    await page.goto('/');
    await page.waitForURL(/\/\(auth\)\/login|\/login/, { timeout: 10_000 });

    // Click Sign In without filling fields
    // data-testid suggestion: [data-testid="login-submit-button"]
    const submitButton = signInButton(page);
    await expect(submitButton).toBeVisible();
    await submitButton.click();

    // Should show validation error
    await expect(
      page.getByText('Please enter both email and password')
    ).toBeVisible({ timeout: 5_000 });
  });

  test('shows error for invalid credentials', async ({ page }) => {
    await page.goto('/');
    await page.waitForURL(/\/\(auth\)\/login|\/login/, { timeout: 10_000 });

    const emailInput = page.getByPlaceholder('you@company.com');
    const passwordInput = page.getByPlaceholder('Enter your password');

    await emailInput.fill('invalid@example.com');
    await passwordInput.fill('wrongpassword123');

    await signInButton(page).click();

    // Supabase returns an error message for invalid credentials
    // The exact styling may vary, but the auth error text should appear.
    await expect(
      page.getByText(/invalid login credentials|invalid credentials|authentication failed/i).first()
    ).toBeVisible({ timeout: 10_000 });
  });

  test('successful login redirects to home or onboarding', async ({ page }) => {
    // Skip if no test credentials are configured
    const email = process.env.E2E_TEST_EMAIL || 'founder@test.aspireos.app';
    const password = process.env.E2E_TEST_PASSWORD;
    test.skip(!password, 'E2E_TEST_PASSWORD not set — skipping real login test');

    await page.goto('/');
    await page.waitForURL(/\/\(auth\)\/login|\/login/, { timeout: 10_000 });

    const emailInput = page.getByPlaceholder('you@company.com');
    const passwordInput = page.getByPlaceholder('Enter your password');

    await emailInput.fill(email);
    await passwordInput.fill(password!);

    await signInButton(page).click();

    // Successful auth may land on onboarding or directly on the desktop home shell.
    const loginUrlPattern = /\/\(auth\)\/login|\/login/;
    await page.waitForFunction(
      (pattern) => !new RegExp(pattern).test(window.location.pathname),
      loginUrlPattern.source,
      { timeout: 15_000 }
    );

    const homeMarkers = [
      page.getByText(/Good (morning|afternoon|evening|night)\./i).first(),
      page.getByPlaceholder('Message Ava...'),
      page.getByText('Home').first(),
    ];
    const onboardingMarkers = [
      page.getByText(/Step 1 of 3|Step 2 of 3|Step 3 of 3/i).first(),
      page.getByText(/Tell us about your business|What do you need help with\?|You are all set/i).first(),
    ];

    const landedOnHome = await Promise.any(
      homeMarkers.map((locator) => locator.waitFor({ state: 'visible', timeout: 15_000 }))
    ).then(() => true).catch(() => false);
    const landedOnOnboarding = await Promise.any(
      onboardingMarkers.map((locator) => locator.waitFor({ state: 'visible', timeout: 5_000 }))
    ).then(() => true).catch(() => false);

    expect(landedOnHome || landedOnOnboarding).toBeTruthy();
  });
});
