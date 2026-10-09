/**
 * Sign-out, with the network controlled and every transition observed: what
 * the server did with the session, what happened to the cookie, and who the
 * page says is signed in afterwards.
 *
 * Hosted run 37866723060 failed "an identity change leaves nothing of the
 * previous user on screen": Sign out, then straight to "/", and the previous
 * user was still signed in. The first two tests show, without the app's own
 * code, the two things that can happen to a sign-out the page leaves early:
 * the request never reaches the server and the session lives on, or it does
 * and the session is gone whether or not the page sees the reply. Which one
 * the hosted run hit is not recorded: it kept no artifacts.
 */

import { expect, test } from "@playwright/test";
import { keepTimelineOnFailure, recordApiTimeline } from "./timeline.js";

const EXEC = { email: process.env.PAC_E2E_EMAIL, password: process.env.PAC_E2E_PASSWORD };
const RAM = { email: process.env.PAC_E2E_RAM_EMAIL, password: process.env.PAC_E2E_RAM_PASSWORD };
const MONEY = /\$[\d,]+\.\d{2}/;

test.skip(!EXEC.email || !RAM.email,
  "run through scripts/browser_journeys.py, which provisions the identities");

let timeline;
test.beforeEach(({ page }) => { timeline = recordApiTimeline(page); });
test.afterEach(({ page: _ }, testInfo) => { keepTimelineOnFailure(testInfo, timeline); });

async function signIn(page, who) {
  await page.goto("/");
  await page.getByLabel(/email/i).fill(who.email);
  await page.getByLabel(/password/i).fill(who.password);
  await page.getByRole("button", { name: /^sign in$/i }).click();
  await expect(page.getByPlaceholder(/ask/i)).toBeVisible();
}

async function askForRevenue(page) {
  const answered = page.waitForResponse((r) => r.url().endsWith("/api/ask") && r.ok());
  await page.getByPlaceholder(/ask/i).fill("What is our total revenue this quarter?");
  await page.getByRole("button", { name: /^ask$/i }).click();
  const body = await (await answered).json();
  await expect(page.getByText(MONEY).first()).toBeVisible();
  return body.conversation_id;
}

// Who the server says this browser is: the page's own cookie jar.
async function signedInAs(page) {
  const r = await page.request.get("/api/me");
  return r.status() === 200 ? (await r.json()).user.email : null;
}

test("a sign-out abandoned before it reaches the server leaves the session alive", async ({ page }) => {
  await signIn(page, EXEC);
  // Held by the test and never released: the request is sent by the page
  // but never reaches the server.
  await page.route("**/api/logout", () => {});
  const sent = page.waitForRequest("**/api/logout");
  await page.evaluate(() => { fetch("/api/logout", { method: "POST", credentials: "same-origin" }); });
  await sent;

  await page.goto("/");

  // The request was sent and never answered, /api/me answered with the old
  // session, and the page shows the first user again.
  await expect(page.getByPlaceholder(/ask/i)).toBeVisible();
  expect(await signedInAs(page)).toBe(EXEC.email);
  expect(timeline.some((e) => e.event === "answered" && e.path === "/api/logout")).toBe(false);
});

test("a sign-out the server applied ends the session though the page never saw the reply", async ({ page }) => {
  await signIn(page, EXEC);
  const cookie = (await page.context().cookies()).find((c) => c.name === "pac_session");
  let applied;
  const appliedOnServer = new Promise((resolve) => { applied = resolve; });
  // Sent to the server; its reply, which deletes the cookie, is withheld.
  await page.route("**/api/logout", async (route) => { await route.fetch(); applied(); });
  await page.evaluate(() => { fetch("/api/logout", { method: "POST", credentials: "same-origin" }); });
  await appliedOnServer;
  // route.fetch() shares the context's cookie jar, so the reply's deletion
  // reached it; put the cookie back, as a browser that never saw the reply
  // would still hold it.
  await page.context().addCookies([cookie]);

  await page.goto("/");

  // The browser still holds the cookie; the server no longer honours it.
  expect((await page.context().cookies()).some((c) => c.name === "pac_session")).toBe(true);
  await expect(page.getByLabel(/email/i)).toBeVisible();
  expect(await signedInAs(page)).toBeNull();
});

test("a sign-out the server refuses is not presented as done", async ({ page }) => {
  await signIn(page, EXEC);
  await askForRevenue(page);
  await page.route("**/api/logout", (route) => route.fulfill({
    status: 503, contentType: "application/json",
    body: JSON.stringify({ detail: { code: "unavailable", message: "Service unavailable." } }),
  }));

  await page.getByRole("button", { name: /sign out/i }).click();

  // The session is still alive, so no sign-in form; the first user's answer
  // is off the screen all the same.
  await expect(page.getByText(/not signed out/i)).toBeVisible();
  await expect(page.getByLabel(/email/i)).toHaveCount(0);
  expect(await page.locator("body").innerText()).not.toMatch(MONEY);
  expect(await signedInAs(page)).toBe(EXEC.email);

  await page.unroute("**/api/logout");
  await page.getByRole("button", { name: /try again/i }).click();
  await expect(page.getByLabel(/email/i)).toBeVisible();
  expect(await signedInAs(page)).toBeNull();
});

test("signing straight in as someone else leaves nothing of the first user, before or after a reload",
  async ({ page, playwright, baseURL }) => {
    await signIn(page, EXEC);
    const conversation = await askForRevenue(page);
    expect(conversation).toBeTruthy();
    const execCookie = (await page.context().cookies()).find((c) => c.name === "pac_session");

    await page.getByRole("button", { name: /sign out/i }).click();
    await expect(page.getByLabel(/email/i)).toBeVisible();

    // Deleted in the browser, and revoked on the server: replayed elsewhere,
    // the first user's cookie no longer signs anyone in.
    expect((await page.context().cookies()).some((c) => c.name === "pac_session")).toBe(false);
    const elsewhere = await playwright.request.newContext({
      baseURL, extraHTTPHeaders: { cookie: `pac_session=${execCookie.value}` } });
    expect((await elsewhere.get("/api/me")).status()).toBe(401);
    await elsewhere.dispose();

    await page.getByLabel(/email/i).fill(RAM.email);
    await page.getByLabel(/password/i).fill(RAM.password);
    await page.getByRole("button", { name: /^sign in$/i }).click();
    await expect(page.getByPlaceholder(/ask/i)).toBeVisible();

    for (const moment of ["after signing in", "after a reload"]) {
      if (moment === "after a reload") {
        await page.reload();
        await expect(page.getByPlaceholder(/ask/i)).toBeVisible();
      }
      const body = await page.locator("body").innerText();
      expect(body, moment).not.toMatch(MONEY);
      expect(body, moment).not.toMatch(/total revenue this quarter/i);
      expect(body, moment).not.toContain(conversation);
      const mine = await page.request.get("/api/conversations");
      expect(JSON.stringify(await mine.json()), moment).not.toContain(conversation);
      expect(await signedInAs(page), moment).toBe(RAM.email);
    }
  });
