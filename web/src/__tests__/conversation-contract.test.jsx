/**
 * The interface against API version 2: what it sends, and what it shows.
 *
 * - every question carries an Idempotency-Key, and a retry after a dropped
 *   connection resends the SAME key, so the server returns the outcome it
 *   already committed instead of answering twice;
 * - Stop aborts the request and cancels the run on the server by that key;
 * - clarification choices are shown in the server's order, and choosing one
 *   sends the position the server will read;
 * - a turn the server could not save says so;
 * - an ended session returns to sign-in, clearing the transcript;
 * - busy and rate-limited answers are shown as the server worded them.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App.jsx";

const USER = { name: "Exec", email: "e@x", role: "exec", scope: "all", can_view_pricing: true };

function respond(status, body, headers = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    headers: { get: (name) => headers[name] ?? null },
  };
}

/** /api/ask answers come from a script: each entry is a response, an Error
 *  to throw (a dropped connection), or "hang" (never settles unless aborted). */
function makeFetch(script) {
  const asks = [];
  const cancels = [];
  const fetchMock = vi.fn(async (path, options = {}) => {
    if (path === "/api/me") return respond(200, { user: USER, dataset: { latest_month: "2026-09" } });
    if (path === "/api/logout") return respond(200, { status: "signed out" });
    if (path === "/api/runs/cancel") {
      cancels.push(JSON.parse(options.body));
      return respond(200, { status: "cancel_requested" });
    }
    if (path === "/api/ask") {
      asks.push({ body: JSON.parse(options.body), key: options.headers?.["Idempotency-Key"] });
      const next = script.shift();
      if (next === "hang") {
        return new Promise((_, reject) => {
          options.signal?.addEventListener("abort", () => {
            const err = new Error("aborted");
            err.name = "AbortError";
            reject(err);
          });
        });
      }
      if (next instanceof Error) throw next;
      return next;
    }
    throw new Error(`unexpected ${path}`);
  });
  fetchMock.asks = asks;
  fetchMock.cancels = cancels;
  return fetchMock;
}

const answered = (extra = {}) => respond(200, {
  status: "answered", conversation_id: "c1", message: "1,234 packs", request_id: "r",
  run_id: "r_1", persistence: "saved",
  answer: { headline: "1,234 packs", columns: ["value"], rows: [], scope_note: "", period_note: "",
            warnings: [], notes: [], row_count: 1, truncated: false },
  ...extra,
});

async function renderSignedIn() {
  await act(async () => { render(<App />); });
  await screen.findByText(/new conversation/i);
}

async function ask(text) {
  fireEvent.change(screen.getByPlaceholderText(/ask/i), { target: { value: text } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: /^ask$/i })); });
}

describe("API v2 in the interface", () => {
  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("sends an Idempotency-Key with every question, a new one each time", async () => {
    global.fetch = makeFetch([answered(), answered()]);
    await renderSignedIn();
    await ask("first");
    await ask("second");
    const [a, b] = global.fetch.asks;
    expect(a.key).toBeTruthy();
    expect(b.key).toBeTruthy();
    expect(a.key).not.toEqual(b.key);
  });

  it("resends the same key after a dropped connection", async () => {
    global.fetch = makeFetch([new TypeError("Failed to fetch"), answered()]);
    await renderSignedIn();
    await ask("total volume");
    await screen.findByText("1,234 packs", {}, { timeout: 3000 });
    const [first, retry] = global.fetch.asks;
    expect(retry.key).toEqual(first.key);
  });

  it("does not retry a refusal that a retry cannot change", async () => {
    global.fetch = makeFetch([respond(429, { detail: { code: "rate_limited",
      message: "Too many questions in the last minute. Try again shortly." } },
      { "Retry-After": "60" })]);
    await renderSignedIn();
    await ask("total volume");
    await screen.findByText(/too many questions/i);
    expect(global.fetch.asks).toHaveLength(1);
  });

  it("Stop aborts the request and cancels the run by its key", async () => {
    global.fetch = makeFetch(["hang"]);
    await renderSignedIn();
    await ask("a slow question");
    const key = global.fetch.asks[0].key;
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /stop/i })); });
    await screen.findByText("Stopped.");
    await waitFor(() => expect(global.fetch.cancels).toEqual([{ idempotency_key: key }]));
    expect(screen.getByRole("button", { name: /^ask$/i })).toBeTruthy();
  });

  it("shows clarification choices in the server's order and sends the position chosen", async () => {
    global.fetch = makeFetch([
      respond(200, { status: "clarify", conversation_id: "c9", message: "Which one?",
        request_id: "r", run_id: "r_2", persistence: "saved",
        choices: [{ id: "SA2", label: "Riverside Clinic", detail: "1 facility, OR" },
                  { id: "SA1", label: "Riverside Clinic", detail: "1 facility, TX" }] }),
      answered({ conversation_id: "c9" }),
    ]);
    await renderSignedIn();
    await ask("volume for Riverside Clinic");
    const buttons = await screen.findAllByRole("button", { name: /riverside clinic/i });
    expect(buttons.map((b) => b.textContent)).toEqual([
      "Riverside Clinic 1 facility, OR", "Riverside Clinic 1 facility, TX"]);
    await act(async () => { fireEvent.click(buttons[1]); });
    const reply = global.fetch.asks[1].body;
    expect(reply).toEqual(expect.objectContaining({ question: "2", conversation_id: "c9" }));
    expect(screen.getByText("Riverside Clinic (1 facility, TX)")).toBeTruthy();
  });

  it("says when an answer was not saved to the conversation", async () => {
    global.fetch = makeFetch([answered({ persistence: "failed" })]);
    await renderSignedIn();
    await ask("total volume");
    await screen.findByText(/was not saved to the conversation/i);
  });

  it("returns to sign-in when the session has ended, clearing the transcript", async () => {
    global.fetch = makeFetch([answered(), respond(401, { detail: "Not signed in." })]);
    await renderSignedIn();
    await ask("first");
    await screen.findByText("1,234 packs");
    await ask("second");
    await screen.findByText(/your session ended/i);
    expect(screen.queryByText("1,234 packs")).toBeNull();
  });

  it("shows a busy conversation as the server worded it", async () => {
    global.fetch = makeFetch([respond(409, { detail: { code: "conversation_busy",
      message: "This conversation is already answering another question." } })]);
    await renderSignedIn();
    await ask("total volume");
    await screen.findByText(/already answering another question/i);
  });
});
