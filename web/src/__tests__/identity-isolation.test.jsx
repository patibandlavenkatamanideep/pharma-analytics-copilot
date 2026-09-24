/**
 * A response issued to one identity must never reach another one's screen.
 *
 * The transcript lives in component state, not in the session, so nothing on
 * the server can prevent this: if a slow /api/ask resolves after the user has
 * signed out, React will happily write that answer into the transcript, and
 * the next person to sign in on the same browser reads it. The headline of an
 * Exec's answer can be a WAC figure, so this is a disclosure, not a cosmetic
 * glitch.
 *
 * These tests drive the real component and control when the in-flight request
 * resolves, so the race is exercised deterministically rather than hoped for.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App.jsx";

/** A fetch stub whose /api/ask response is released by the test, not by time. */
function makeFetch() {
  let releaseAsk;
  const askIssued = [];

  const fetchMock = vi.fn(async (path, options = {}) => {
    const json = (body, ok = true) => ({
      ok,
      status: ok ? 200 : 401,
      json: async () => body,
    });

    if (path === "/api/me") {
      return fetchMock.signedIn
        ? json({ user: fetchMock.signedIn, dataset: { latest_month: "2026-09" } })
        : json({ detail: "Not signed in." }, false);
    }
    if (path === "/api/login") {
      const { email } = JSON.parse(options.body);
      const user = {
        name: email, email, role: "exec",
        scope: "all territories and regions", can_view_pricing: true,
      };
      fetchMock.signedIn = user;
      return json({ user });
    }
    if (path === "/api/logout") {
      fetchMock.signedIn = null;
      return json({ status: "signed out" });
    }
    if (path === "/api/ask") {
      askIssued.push(JSON.parse(options.body));
      return new Promise((resolve, reject) => {
        // Honour cancellation the way a real fetch does.
        options.signal?.addEventListener("abort", () => {
          const err = new Error("aborted");
          err.name = "AbortError";
          reject(err);
        });
        releaseAsk = (body) => resolve(json(body));
      });
    }
    throw new Error(`unexpected path ${path}`);
  });

  fetchMock.signedIn = null;
  fetchMock.release = (body) => releaseAsk(body);
  fetchMock.askIssued = askIssued;
  return fetchMock;
}

const EXEC_ANSWER = {
  status: "answered",
  conversation_id: "c_exec_thread",
  message: "Gross revenue: $250,766,926.42.",
  request_id: "r1",
  answer: {
    headline: "Gross revenue: $250,766,926.42",
    columns: ["value"], rows: [], scope_note: "all territories and regions",
    period_note: "", warnings: [], notes: [], row_count: 1, truncated: false,
  },
};

async function signIn(email) {
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value: email } });
  fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "pw" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
  });
}

async function askSomething(text) {
  const box = screen.getByPlaceholderText(/ask/i);
  fireEvent.change(box, { target: { value: text } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /send|ask/i }));
  });
}

describe("identity isolation in the UI", () => {
  beforeEach(() => {
    global.fetch = makeFetch();
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
  });
  afterEach(cleanup);

  it("does not deliver a pending answer to the next user who signs in", async () => {
    render(<App />);
    await signIn("exec@example.com");
    await askSomething("What is our total revenue this quarter?");

    // Sign out while the answer is still in flight.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /sign out/i }));
    });
    await waitFor(() => expect(screen.getByLabelText(/email/i)).toBeDefined());

    // The previous identity's answer now arrives.
    await act(async () => {
      global.fetch.release(EXEC_ANSWER);
      await Promise.resolve();
    });

    await signIn("ram@example.com");

    expect(document.body.textContent).not.toContain("250,766,926");
    expect(document.body.textContent).not.toContain(
      "What is our total revenue this quarter?",
    );
  });

  it("clears the transcript when a new identity signs in", async () => {
    render(<App />);
    await signIn("exec@example.com");
    await askSomething("What is our total revenue this quarter?");
    await act(async () => {
      global.fetch.release(EXEC_ANSWER);
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(document.body.textContent).toContain("250,766,926"),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /sign out/i }));
    });
    await signIn("ram@example.com");

    expect(document.body.textContent).not.toContain("250,766,926");
  });

  it("does not carry the previous identity's conversation id into a new session", async () => {
    render(<App />);
    await signIn("exec@example.com");
    await askSomething("What is our total revenue this quarter?");
    await act(async () => {
      global.fetch.release(EXEC_ANSWER);
      await Promise.resolve();
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /sign out/i }));
    });
    await signIn("ram@example.com");
    await askSomething("What are my top accounts?");

    const last = global.fetch.askIssued[global.fetch.askIssued.length - 1];
    expect(last.conversation_id).toBeNull();
  });

  it("shows no error turn when a request is cancelled by signing out", async () => {
    render(<App />);
    await signIn("exec@example.com");
    await askSomething("What is our total revenue this quarter?");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /sign out/i }));
      await Promise.resolve();
    });
    await signIn("exec@example.com");

    expect(document.body.textContent).not.toContain("aborted");
  });
});
