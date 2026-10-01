/**
 * Acceptance journeys the review asked for, in real Chromium against the
 * real server: identity change, conversation ownership, clarification, and a
 * session that ends. Run by scripts/browser_journeys.py, which provisions
 * disposable identities in the disposable database and removes them after.
 */

import { expect, test } from "@playwright/test";

const EXEC = { email: process.env.PAC_E2E_EMAIL, password: process.env.PAC_E2E_PASSWORD };
const OTHER = { email: process.env.PAC_E2E_OTHER_EMAIL, password: process.env.PAC_E2E_OTHER_PASSWORD };
const RAM = { email: process.env.PAC_E2E_RAM_EMAIL, password: process.env.PAC_E2E_RAM_PASSWORD };
const TWIN = process.env.PAC_E2E_TWIN;

test.skip(!EXEC.email || !OTHER.email || !RAM.email || !TWIN,
  "run through scripts/browser_journeys.py, which provisions the identities");

async function signIn(page, who) {
  await page.goto("/");
  await page.getByLabel(/email/i).fill(who.email);
  await page.getByLabel(/password/i).fill(who.password);
  await page.getByRole("button", { name: /^sign in$/i }).click();
  await expect(page.getByPlaceholder(/ask/i)).toBeVisible();
}

async function ask(page, question) {
  await page.getByPlaceholder(/ask/i).fill(question);
  await page.getByRole("button", { name: /^ask$/i }).click();
  await expect(page.getByText(/Interpreting your question|Querying the data/i))
    .toHaveCount(0, { timeout: 60_000 });
}

test("an identity change leaves nothing of the previous user on screen", async ({ page }) => {
  await signIn(page, EXEC);
  await ask(page, "What is our total revenue this quarter?");
  await expect(page.getByText(/\$[\d,]+\.\d{2}/).first()).toBeVisible();

  await page.getByRole("button", { name: /sign out/i }).click();
  await signIn(page, RAM);
  const body = await page.locator("body").innerText();
  expect(body).not.toMatch(/\$[\d,]+\.\d{2}/);
  expect(body).not.toMatch(/total revenue this quarter/i);
});

test("a conversation belongs to the user who started it", async ({ browser }) => {
  const alice = await browser.newContext();
  const alicePage = await alice.newPage();
  await signIn(alicePage, EXEC);
  const asked = await alicePage.request.post("/api/ask", {
    data: { question: "What is our total volume this quarter?" },
  });
  const { conversation_id } = await asked.json();

  const bob = await browser.newContext();
  const bobPage = await bob.newPage();
  await signIn(bobPage, OTHER);
  const history = await bobPage.request.get(`/api/conversations/${conversation_id}`);
  expect(history.status()).toBe(404);
  const continued = await bobPage.request.post("/api/ask", {
    data: { question: "And last quarter?", conversation_id },
  });
  expect(continued.status()).toBe(404);
  await alice.close();
  await bob.close();
});

test("a clarification is answered by choosing one of the options shown", async ({ page }) => {
  await signIn(page, EXEC);
  await ask(page, `What was the volume for ${TWIN} in the last 3 months?`);
  const options = page.getByRole("group", { name: /choose one/i }).getByRole("button");
  await expect(options).toHaveCount(2);
  const second = await options.nth(1).innerText();

  await options.nth(1).click();
  await expect(page.getByText(/Interpreting your question|Querying the data/i))
    .toHaveCount(0, { timeout: 60_000 });
  // The reply shown is the option chosen, and an answer follows it.
  await expect(page.getByText(second.replace(/\s+/g, " ").split(" 1 facility")[0]).last())
    .toBeVisible();
  await expect(page.locator(".bubble.answered").last()).toBeVisible();
  await expect(page.getByRole("group", { name: /choose one/i })).toHaveCount(1);
});

test("a session that ends returns the user to sign-in with the reason", async ({ page, context }) => {
  await signIn(page, EXEC);
  await ask(page, "What is our total volume this quarter?");
  await context.clearCookies();
  await page.getByPlaceholder(/ask/i).fill("And last quarter?");
  await page.getByRole("button", { name: /^ask$/i }).click();
  await expect(page.getByText(/your session ended/i)).toBeVisible();
  await expect(page.getByLabel(/password/i)).toBeVisible();
});
