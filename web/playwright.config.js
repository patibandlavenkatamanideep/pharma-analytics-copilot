import { defineConfig, devices } from "@playwright/test";

// Real Chromium against the real server, with the real database behind it.
// These are the tests the jsdom component suite cannot be: they exercise
// rendering, the actual cookie jar, real navigation and a real network round
// trip. Start the app first:
//
//   PAC_COOKIE_SECURE=false uvicorn app.api.main:app --port 8010
//
// and provide credentials the tests can sign in with:
//
//   PAC_E2E_EMAIL=... PAC_E2E_PASSWORD=... npx playwright test
export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.PAC_E2E_URL || "http://127.0.0.1:8010",
    // A trace holds request bodies and cookies -- the sign-in form's password
    // among them -- so CI, whose artifacts a public repository exposes, keeps
    // screenshots, the page snapshot and the API timeline (e2e/timeline.js)
    // instead.
    trace: process.env.CI ? "off" : "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
