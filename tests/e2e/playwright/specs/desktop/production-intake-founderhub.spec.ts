import { test, expect } from '../../fixtures/auth';

const ADMIN_URL = process.env.ADMIN_URL || 'http://localhost:5173';

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function hasSupabaseAccessToken(page: any): Promise<boolean> {
  return page.evaluate(() => {
    try {
      const key = Object.keys(window.localStorage).find((k) => k.startsWith('sb-') && k.endsWith('-auth-token'));
      if (!key) return false;
      const raw = window.localStorage.getItem(key);
      if (!raw) return false;
      const parsed = JSON.parse(raw);
      if (parsed?.access_token) return true;
      if (parsed?.currentSession?.access_token) return true;
      if (Array.isArray(parsed) && parsed[0]?.access_token) return true;
      return false;
    } catch {
      return false;
    }
  });
}

async function ensureAuthenticatedSession(page: any, email: string, password: string): Promise<void> {
  const clickSignInCta = async () => {
    const roleBtn = page.getByRole('button', { name: 'Sign In' }).first();
    if (await roleBtn.isVisible().catch(() => false)) {
      await roleBtn.click();
      return;
    }
    const textBtn = page.locator('text=Sign In').first();
    if (await textBtn.isVisible().catch(() => false)) {
      await textBtn.click();
      return;
    }
    await page.getByText('Sign In').first().click();
  };

  for (let attempt = 0; attempt < 4; attempt++) {
    if (await hasSupabaseAccessToken(page)) return;
    const onboardingVisible =
      (await page.getByTestId('onboarding-screen').isVisible().catch(() => false)) ||
      (await page.getByText('Step 1 of 3').isVisible().catch(() => false));
    if (onboardingVisible) return;
    const homeVisible = await page.getByText('Good night.').isVisible().catch(() => false);
    if (homeVisible) return;
    const emailInputVisible = await page.getByPlaceholder('you@company.com').isVisible().catch(() => false);
    if (!emailInputVisible) {
      await page.goto('/login', { waitUntil: 'networkidle' });
    }
    const emailInput = page.getByPlaceholder('you@company.com');
    const passwordInput = page.getByPlaceholder('Enter your password');
    const emailEditable = await emailInput.isEditable().catch(() => false);
    const passwordEditable = await passwordInput.isEditable().catch(() => false);

    if (emailEditable) await emailInput.fill(email);
    if (passwordEditable) {
      await passwordInput.fill(password);
      await passwordInput.press('Enter');
    }
    if (!(await hasSupabaseAccessToken(page))) {
      await clickSignInCta();
    }
    await page.waitForLoadState('networkidle');
    await sleep(500);
  }
  if (!(await hasSupabaseAccessToken(page))) {
    const onboardingVisible =
      (await page.getByTestId('onboarding-screen').isVisible().catch(() => false)) ||
      (await page.getByText('Step 1 of 3').isVisible().catch(() => false));
    const homeVisible = await page.getByText('Good night.').isVisible().catch(() => false);
    if (onboardingVisible || homeVisible) return;
    throw new Error('No authenticated session after login retries.');
  }
}

test.describe('Production Intake -> Profile Sync -> Founder Hub', () => {
  test.describe.configure({ mode: 'serial' });

  test('completes intake and validates identity + founder hub population', async ({ freshPage: page }) => {
    test.setTimeout(8 * 60 * 1000);

    const onboardingEmailEnv = process.env.E2E_ONBOARDING_EMAIL;
    const onboardingPassword = process.env.E2E_ONBOARDING_PASSWORD || 'test123456';
    const inviteCode = process.env.E2E_INVITE_CODE;

    let onboardingEmail = onboardingEmailEnv || '';
    const willAttemptSignup = !onboardingEmail && !!inviteCode;

    test.skip(
      !onboardingEmail && !inviteCode,
      'Set E2E_ONBOARDING_EMAIL or E2E_INVITE_CODE for production intake validation'
    );

    const runSuffix = Date.now().toString().slice(-6);
    const ownerName = `QA Founder ${runSuffix}`;
    const businessName = `QA Roofing ${runSuffix}`;
    const expectedIndustry = 'Construction & Trades';

    await page.goto('/');

    if (!(await page.getByPlaceholder('you@company.com').isVisible().catch(() => false))) {
      await page.goto('/login', { waitUntil: 'networkidle' });
    }

    const loginEmailInput = page.getByPlaceholder('you@company.com');
    if (willAttemptSignup && (await page.getByText('Sign Up').first().isVisible().catch(() => false))) {
      onboardingEmail = `e2e.qa.${Date.now()}@example.com`;
      await page.getByText('Sign Up').first().click();
      await page.getByPlaceholder('Enter your private beta invite code').fill(inviteCode!);
      await page.getByPlaceholder('you@company.com').fill(onboardingEmail);
      await page.getByPlaceholder('Min. 8 characters').fill(onboardingPassword);
      await page.getByPlaceholder('Re-enter your password').fill(onboardingPassword);
      await page.getByText('Create Account').first().click();
      // Signup can leave a partial client session on production.
      // Force a clean credential login before onboarding submit.
      await page.goto('/login', { waitUntil: 'networkidle' });
      await page.getByPlaceholder('you@company.com').fill(onboardingEmail);
      const loginPasswordInput = page.getByPlaceholder('Enter your password');
      await loginPasswordInput.fill(onboardingPassword);
      await loginPasswordInput.press('Enter');
      await page.waitForLoadState('networkidle');
    } else if (await loginEmailInput.isVisible().catch(() => false)) {
      await loginEmailInput.fill(onboardingEmail);
      const loginPasswordInput = page.getByPlaceholder('Enter your password');
      await loginPasswordInput.fill(onboardingPassword);
      await loginPasswordInput.press('Enter');
    }

    await ensureAuthenticatedSession(page, onboardingEmail, onboardingPassword);

    await page.waitForLoadState('networkidle');
    if (!(await page.getByTestId('onboarding-screen').isVisible().catch(() => false))) {
      await page.goto('/onboarding', { waitUntil: 'networkidle' });
      await page.waitForLoadState('networkidle');
    }
    const onboardingVisible =
      (await page.getByTestId('onboarding-screen').isVisible().catch(() => false)) ||
      (await page.getByText('Step 1 of 3').isVisible().catch(() => false));
    if (!onboardingVisible) {
      throw new Error(
        'Onboarding screen is not reachable for this account. This user is already onboarded or onboarding is blocked. ' +
        'Use a dedicated fresh QA account (invite-code approved) to execute intake + celebration popup + Founder Hub population validation.'
      );
    }
    if (await page.getByTestId('onboarding-screen').isVisible().catch(() => false)) {
      await expect(page.getByTestId('onboarding-screen')).toBeVisible();
      await expect(page.getByTestId('onboarding-step-indicator')).toContainText('Step 1 of 3');
    } else {
      await expect(page.getByText('Step 1 of 3')).toBeVisible();
    }

    const ownerNameInput = page.getByTestId('onboarding-owner-name').or(page.getByPlaceholder('Full name')).first();
    await ownerNameInput.fill(ownerName);
    const dobInput = page.getByTestId('onboarding-date-of-birth').or(page.getByPlaceholder('YYYY-MM-DD')).first();
    await dobInput.fill('1990-01-01');
    await page.getByTestId('onboarding-gender-male').click();
    const businessNameInput = page.getByTestId('onboarding-business-name').or(page.getByPlaceholder(/Apex Plumbing LLC|Your Business Name/i)).first();
    await businessNameInput.fill(businessName);
    await page.getByTestId('onboarding-industry-construction-trades').click();
    await page.getByTestId('onboarding-specialty-roofing').click();
    await page.getByTestId('onboarding-team-size-just-me').click();

    await page.getByText('Select entity type').click();
    await page.getByText('LLC').click();
    await page.getByText('1-3', { exact: true }).click();

    await page.getByTestId('onboarding-continue-button').click();

    if (await page.getByTestId('onboarding-step-indicator').isVisible().catch(() => false)) {
      await expect(page.getByTestId('onboarding-step-indicator')).toContainText('Step 2 of 3');
    } else {
      await expect(page.getByText('Step 2 of 3')).toBeVisible();
    }
    await page.getByTestId('onboarding-home-address-line1').or(page.getByPlaceholder('Street address').first()).first().fill('100 Main St');
    await page.getByTestId('onboarding-home-address-city').or(page.getByPlaceholder('City').first()).first().fill('Austin');
    await page.getByTestId('onboarding-home-address-state').or(page.getByPlaceholder('State').first()).first().fill('TX');
    await page.getByTestId('onboarding-home-address-zip').or(page.getByPlaceholder('ZIP').first()).first().fill('78701');
    await page.getByTestId('onboarding-home-address-country').or(page.getByPlaceholder(/Country code/i).first()).first().fill('US');

    await page.getByTestId('onboarding-continue-button').click();

    if (await page.getByTestId('onboarding-step-indicator').isVisible().catch(() => false)) {
      await expect(page.getByTestId('onboarding-step-indicator')).toContainText('Step 3 of 3');
    } else {
      await expect(page.getByText('Step 3 of 3')).toBeVisible();
    }
    await page.getByText('$75K-$100K').click();
    await page.getByText('Google Search').click();
    await page.getByText(/I agree to a personalized experience/i).click();

    const bootstrapResponsePromise = page
      .waitForResponse((response) => response.url().includes('/api/onboarding/bootstrap') && response.request().method() === 'POST', { timeout: 60_000 })
      .catch(() => null);

    await page.getByTestId('onboarding-launch-button').or(page.getByText(/Launch Aspire|Start Using Aspire/i)).first().click();

    const loadingVisible = await page
      .getByTestId('onboarding-premium-loading-screen')
      .isVisible({ timeout: 15_000 })
      .catch(() => false);
    test.info().annotations.push({
      type: 'premium-loading-screen',
      description: loadingVisible ? 'visible' : 'not-visible',
    });
    if (!loadingVisible) {
      await page.waitForLoadState('networkidle');
    }

    const celebrationVisible =
      (await page.getByTestId('onboarding-celebration-modal').isVisible({ timeout: 120_000 }).catch(() => false)) ||
      (await page.getByText('Congratulations!').isVisible({ timeout: 120_000 }).catch(() => false));
    test.info().annotations.push({
      type: 'celebration-modal',
      description: celebrationVisible ? 'visible' : 'not-visible; direct-dashboard onboarding behavior',
    });

    let suiteDisplayId = '';
    let officeDisplayId = '';
    if (celebrationVisible) {
      const suiteText = await page.getByTestId('onboarding-celebration-suite').or(page.locator('text=/Suite\\s+[A-Za-z0-9-]+/').first()).first().innerText();
      const officeText = await page.getByTestId('onboarding-celebration-office').or(page.locator('text=/Office\\s+[A-Za-z0-9-]+/').first()).first().innerText();
      const suiteMatch = suiteText.match(/Suite\s+([A-Za-z0-9-]+)/i);
      const officeMatch = officeText.match(/Office\s+([A-Za-z0-9-]+)/i);
      suiteDisplayId = suiteMatch?.[1] ?? '';
      officeDisplayId = officeMatch?.[1] ?? '';
    }

    const bootstrapResponse = await bootstrapResponsePromise;
    if (bootstrapResponse) {
      const json = await bootstrapResponse.json().catch(() => null);
      suiteDisplayId = suiteDisplayId || json?.suiteDisplayId || '';
      officeDisplayId = officeDisplayId || json?.officeDisplayId || '';
    }
    if (celebrationVisible) {
      await page.getByTestId('onboarding-celebration-enter').or(page.getByText('Enter Aspire')).first().click();
    }

    await page.waitForLoadState('networkidle');
    const dashboardDeadline = Date.now() + 120_000;
    let dashboardText = await page.locator('body').innerText();
    while (
      Date.now() < dashboardDeadline &&
      /Setting up your workspace/i.test(dashboardText)
    ) {
      await sleep(3000);
      dashboardText = await page.locator('body').innerText();
    }
    if (!suiteDisplayId) {
      suiteDisplayId = dashboardText.match(/Suite\s+([A-Za-z0-9-]+)/i)?.[1] ?? '';
    }
    if (!officeDisplayId) {
      officeDisplayId = dashboardText.match(/Office\s+([A-Za-z0-9-]+)/i)?.[1] ?? '';
    }
    expect(suiteDisplayId).toBeTruthy();
    expect(officeDisplayId).toBeTruthy();
    test.info().annotations.push({
      type: 'suite-office',
      description: `Suite ${suiteDisplayId} / Office ${officeDisplayId}`,
    });
    expect(dashboardText).toContain(`Suite ${suiteDisplayId}`);
    expect(dashboardText).toContain(`Office ${officeDisplayId}`);
    expect(dashboardText).toContain(businessName);

    const headerBusinessName = page.getByTestId('desktop-header-business-name');
    const headerSuiteOffice = page.getByTestId('desktop-header-suite-office');
    if (await headerBusinessName.isVisible().catch(() => false)) {
      await expect(headerBusinessName).toContainText(businessName);
    }
    if (await headerSuiteOffice.isVisible().catch(() => false)) {
      await expect(headerSuiteOffice).toContainText(`Suite ${suiteDisplayId}`);
      await expect(headerSuiteOffice).toContainText(`Office ${officeDisplayId}`);
    }

    await page.goto('/session/conference-lobby');
    const conferenceHeader = page.getByTestId('conference-header-suite-office').or(page.locator('text=/Suite\\s+[A-Za-z0-9-]+\\s+•\\s+Office\\s+[A-Za-z0-9-]+/').first()).first();
    await expect(conferenceHeader).toContainText(`Suite ${suiteDisplayId}`);
    await expect(conferenceHeader).toContainText(`Office ${officeDisplayId}`);

    await page.goto('/more/office-identity');
    await expect(page.getByText('Office Identity').first()).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText(businessName).first()).toBeVisible();
    await expect(page.getByText('Suite ID')).toBeVisible();
    await expect(page.getByText(suiteDisplayId).first()).toBeVisible();
    await expect(page.getByText('Office ID')).toBeVisible();
    await expect(page.getByText(officeDisplayId).first()).toBeVisible();

    await page.goto('/founder-hub/daily-brief');
    const briefPageVisible = await page.getByTestId('founder-hub-daily-brief-page').isVisible({ timeout: 20_000 }).catch(() => false);
    if (briefPageVisible) {
      const subtitle = page.getByTestId('founder-hub-daily-brief-subtitle').or(page.locator('text=/Insights for your .* business/i').first()).first();
      if (await subtitle.isVisible().catch(() => false)) {
        await expect(subtitle).toContainText(expectedIndustry);
      }
      await expect(page.getByTestId('founder-hub-daily-brief-locked')).toHaveCount(0);

      const startedAt = Date.now();
      const deadlineMs = 5 * 60 * 1000;
      let populated = await page.getByTestId('founder-hub-daily-brief-populated').isVisible().catch(() => false);
      while (!populated && Date.now() - startedAt < deadlineMs) {
        await sleep(15_000);
        await page.reload({ waitUntil: 'networkidle' });
        populated = await page.getByTestId('founder-hub-daily-brief-populated').isVisible().catch(() => false);
      }
      expect(populated).toBeTruthy();
    } else {
      // Founder Hub shell variant: validate section is present and navigable.
      await expect(page.getByText('Founder Hub').first()).toBeVisible();
      await expect(page.getByText('Daily Brief').first()).toBeVisible();
    }

    const adminEmail = process.env.E2E_ADMIN_EMAIL;
    const adminPassword = process.env.E2E_ADMIN_PASSWORD;
    if (adminEmail && adminPassword) {
      const adminPage = await page.context().newPage();
      const adminLanding = await adminPage.goto(ADMIN_URL, { timeout: 20_000 }).catch(() => null);
      if (adminLanding && adminLanding.ok()) {
        const adminEmailInput = adminPage.locator('input[type="email"], input[placeholder*="email" i]').first();
        const adminPasswordInput = adminPage.locator('input[type="password"], input[placeholder*="password" i]').first();
        const showLogin = await adminEmailInput.isVisible().catch(() => false);
        if (showLogin) {
          await adminEmailInput.fill(adminEmail);
          await adminPasswordInput.fill(adminPassword);
          await adminPage.locator('button[type="submit"]').first().click();
          await adminPage.waitForLoadState('networkidle');
        }
        await adminPage.goto(`${ADMIN_URL}/customers`, { timeout: 20_000 }).catch(() => null);
        await adminPage.waitForLoadState('networkidle');
        const adminBody = await adminPage.locator('body').innerText();
        expect(adminBody).toContain(businessName);
      } else {
        test.info().annotations.push({
          type: 'admin-check',
          description: `Admin portal unreachable at ${ADMIN_URL}; skipped admin profile visibility check`,
        });
      }
      await adminPage.close();
    } else {
      test.info().annotations.push({
        type: 'admin-check',
        description: 'E2E_ADMIN_EMAIL/E2E_ADMIN_PASSWORD missing; skipped admin profile visibility check',
      });
    }
  });
});
