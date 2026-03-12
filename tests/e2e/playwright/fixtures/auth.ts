import { test as base, Page, BrowserContext } from '@playwright/test';
import { createClient } from '@supabase/supabase-js';

/**
 * Auth fixtures for Aspire Desktop E2E tests.
 *
 * Provides:
 * - authenticatedPage: A page with a Supabase session injected into localStorage,
 *   simulating a logged-in + onboarded user.
 * - freshPage: A page with no authentication state (clean browser context).
 */

const SUPABASE_URL = process.env.EXPO_PUBLIC_SUPABASE_URL || '';
const SUPABASE_ANON_KEY = process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY || '';
const TEST_EMAIL = process.env.E2E_TEST_EMAIL || 'founder@test.aspireos.app';
const TEST_PASSWORD = process.env.E2E_TEST_PASSWORD || 'test123456';

type AuthFixtures = {
  authenticatedPage: Page;
  freshPage: Page;
};

export const test = base.extend<AuthFixtures>({
  authenticatedPage: async ({ browser }, use) => {
    if (!SUPABASE_URL || !SUPABASE_ANON_KEY) {
      throw new Error(
        'Missing EXPO_PUBLIC_SUPABASE_URL or EXPO_PUBLIC_SUPABASE_ANON_KEY env vars. ' +
        'Set them before running authenticated E2E tests.'
      );
    }

    // Sign in via Supabase client to get a real session
    const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
    const { data, error } = await supabase.auth.signInWithPassword({
      email: TEST_EMAIL,
      password: TEST_PASSWORD,
    });

    if (error || !data.session) {
      throw new Error(
        `Supabase auth failed: ${error?.message || 'No session returned'}. ` +
        `Ensure the test user "${TEST_EMAIL}" exists in your Supabase project.`
      );
    }

    const session = data.session;

    // Create a fresh browser context and inject the session into localStorage
    const context: BrowserContext = await browser.newContext();
    const page: Page = await context.newPage();

    // Navigate to origin first so we can set localStorage
    const baseURL = process.env.BASE_URL || 'http://localhost:5000';
    await page.goto(baseURL, { waitUntil: 'domcontentloaded' });

    // Inject the Supabase auth session into localStorage
    // Supabase JS client stores the session under a key derived from the project URL
    const storageKey = `sb-${new URL(SUPABASE_URL).hostname.split('.')[0]}-auth-token`;
    await page.evaluate(
      ({ key, sessionData }) => {
        localStorage.setItem(
          key,
          JSON.stringify({
            access_token: sessionData.access_token,
            refresh_token: sessionData.refresh_token,
            expires_in: sessionData.expires_in,
            expires_at: sessionData.expires_at,
            token_type: sessionData.token_type,
            user: sessionData.user,
          })
        );
      },
      {
        key: storageKey,
        sessionData: {
          access_token: session.access_token,
          refresh_token: session.refresh_token,
          expires_in: session.expires_in,
          expires_at: session.expires_at,
          token_type: session.token_type,
          user: session.user,
        },
      }
    );

    // Reload so the app picks up the session from localStorage.
    // The desktop shell keeps background activity alive, so networkidle is too strict here.
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.locator('body').waitFor({ state: 'visible', timeout: 15_000 });
    await page.waitForTimeout(1500);

    await use(page);

    // Cleanup
    await context.close();
    await supabase.auth.signOut();
  },

  freshPage: async ({ browser }, use) => {
    const context = await browser.newContext();
    const page = await context.newPage();
    await use(page);
    await context.close();
  },
});

export { expect } from '@playwright/test';
