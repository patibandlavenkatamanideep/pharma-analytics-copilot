/**
 * What the page asked the API, in order: method, path, status, or the
 * browser's reason for abandoning the request. No headers, cookies, query
 * strings or bodies, so it can be kept as a CI artifact of a public
 * repository. Written beside the screenshot when a test fails.
 */

import fs from "node:fs";

export function recordApiTimeline(page) {
  const events = [];
  const started = Date.now();
  const path = (url) => new URL(url).pathname;
  const api = (request) => path(request.url()).startsWith("/api/");
  page.on("request", (r) => api(r) &&
    events.push({ ms: Date.now() - started, event: "sent", method: r.method(), path: path(r.url()) }));
  page.on("response", (r) => api(r.request()) &&
    events.push({ ms: Date.now() - started, event: "answered", path: path(r.url()), status: r.status() }));
  page.on("requestfailed", (r) => api(r) &&
    events.push({ ms: Date.now() - started, event: "abandoned", path: path(r.url()),
                  reason: r.failure()?.errorText ?? "unknown" }));
  page.on("framenavigated", (frame) => frame === page.mainFrame() &&
    events.push({ ms: Date.now() - started, event: "navigated", path: path(frame.url()) }));
  return events;
}

export function keepTimelineOnFailure(testInfo, events) {
  if (testInfo.status !== testInfo.expectedStatus) {
    fs.writeFileSync(testInfo.outputPath("api-timeline.json"), JSON.stringify(events, null, 1));
  }
}
