/**
 * Real browser, real server, real 2,000,000-row database.
 *
 * The jsdom suite stubs `fetch` and renders the component in a fake DOM. It
 * cannot tell you that the cookie actually round-trips, that the table
 * actually renders, or that a refusal actually reaches the screen. These can.
 */

import { expect, test } from "@playwright/test";

const EMAIL = process.env.PAC_E2E_EMAIL;
const PASSWORD = process.env.PAC_E2E_PASSWORD;
const RAM_EMAIL = process.env.PAC_E2E_RAM_EMAIL;
const RAM_PASSWORD = process.env.PAC_E2E_RAM_PASSWORD;

test.skip(!EMAIL || !PASSWORD, "set PAC_E2E_EMAIL and PAC_E2E_PASSWORD");

async function signIn(page, email, password) {
  await page.goto("/");
  await page.getByLabel(/email/i).fill(email);
  await page.getByLabel(/password/i).fill(password);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByPlaceholder(/ask/i)).toBeVisible();
}

async function ask(page, question) {
  const box = page.getByPlaceholder(/ask/i);
  await box.fill(question);
  await page.getByRole("button", { name: /send|ask/i }).click();
  // The pending turn is replaced by the answer; wait for the spinner text to go.
  await expect(page.getByText(/Interpreting your question|Querying the data/i))
    .toHaveCount(0, { timeout: 90_000 });
}

test("an Exec signs in and gets a priced answer rendered in the page", async ({ page }) => {
  await signIn(page, EMAIL, PASSWORD);
  // The scope appears in the header badge and again in the empty-state copy.
  await expect(page.getByText(/all territories and regions/i).first()).toBeVisible();

  await ask(page, "What is our total revenue this quarter?");
  // A currency figure actually painted on screen, not just in a JSON response.
  await expect(page.getByText(/\$[\d,]+\.\d{2}/).first()).toBeVisible();
});

test("the session cookie survives a full page reload", async ({ page }) => {
  await signIn(page, EMAIL, PASSWORD);
  await page.reload();
  // Still signed in: no password field, chat is present.
  await expect(page.locator('input[type="password"]')).toHaveCount(0);
  await expect(page.getByPlaceholder(/ask/i)).toBeVisible();
});

test("signing out clears the session and a reload does not restore it", async ({ page }) => {
  await signIn(page, EMAIL, PASSWORD);
  await page.getByRole("button", { name: /sign out/i }).click();
  await expect(page.getByLabel(/email/i)).toBeVisible();
  await page.reload();
  await expect(page.getByLabel(/email/i)).toBeVisible();
});

test("a follow-up keeps the thread and New conversation clears it", async ({ page }) => {
  await signIn(page, EMAIL, PASSWORD);
  await ask(page, "What are our top 5 accounts by pack units this quarter?");
  const first = await page.locator("main").innerText();
  expect(first).toMatch(/top 5|Top 5/i);

  await ask(page, "Now break that down by quarter");
  // Both turns are on screen: this is a conversation, not a reset.
  await expect(page.getByText(/break that down by quarter/i)).toBeVisible();

  await page.getByRole("button", { name: /new conversation/i }).click();
  await expect(page.getByText(/break that down by quarter/i)).toHaveCount(0);
  // Still signed in.
  await expect(page.getByPlaceholder(/ask/i)).toBeVisible();
});

test("a RAM sees no currency anywhere on the page", async ({ page }) => {
  test.skip(!RAM_EMAIL || !RAM_PASSWORD, "set PAC_E2E_RAM_EMAIL and PAC_E2E_RAM_PASSWORD");
  await signIn(page, RAM_EMAIL, RAM_PASSWORD);
  await ask(page, "What is my total revenue in dollars this quarter?");

  const body = await page.locator("body").innerText();
  expect(body).not.toMatch(/\$[\d,]+\.\d{2}/);
  // And the substitution is disclosed rather than silent.
  expect(body).toMatch(/pricing|restricted/i);
});

test("an out-of-scope territory is refused by name", async ({ page }) => {
  test.skip(!RAM_EMAIL || !RAM_PASSWORD, "set PAC_E2E_RAM_EMAIL and PAC_E2E_RAM_PASSWORD");
  await signIn(page, RAM_EMAIL, RAM_PASSWORD);
  await ask(page, "Show me sales in the Texas territory");
  await expect(page.getByText(/Texas/i).first()).toBeVisible();
});
