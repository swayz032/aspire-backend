import { test, expect } from '@playwright/test';

/**
 * Login E2E Tests
 *
 * Tests the (auth)/login screen: redirection, form validation,
 * successful sign-in, and error handling.
 */
test.describe('Login', () => {
  test('redirects unauthenticated user to login page', async ({ page }) => {
    await page.goto('/');
    // Auth gate should redirect to login
    await page.waitForURL(/\/\(auth\)\/login|\/login/, { timeout: 10_000 });

    // Verify login page content is visible
    // data-testid suggestion: [data-testid="login-brand-name"]
    await expect(page.getByText('Aspire')).toBeVisible();
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
    const signInButton = page.getByText('Sign In');
    await expect(signInButton).toBeVisible();
    await signInButton.click();

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

    const signInButton = page.getByText('Sign In');
    await signInButton.click();

    // Supabase returns an error message for invalid credentials
    // The exact message may vary, but an error box should appear
    const errorBox = page.locator('[style*="rgba(239, 68, 68"]');
    await expect(errorBox).toBeVisible({ timeout: 10_000 });
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

    const signInButton = page.getByText('Sign In');
    await signInButton.click();

    // Should redirect away from login — either to tabs (home) or onboarding
    await page.waitForURL(
      /\/\(tabs\)|\/\(auth\)\/onboarding/,
      { timeout: 15_000 }
    );
  });
});
