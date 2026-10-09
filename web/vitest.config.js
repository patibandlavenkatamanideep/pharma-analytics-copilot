import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // A stale entry in this directory hangs the worker: it starts, never
  // responds, and the run ends after 60s having collected nothing. That was
  // misdiagnosed for weeks as "macOS stalls reads under ~/Desktop", because
  // every fresh checkout used to test the theory also had a fresh cache.
  // A plain Node worker starts under this path in 11ms, and these tests run
  // here in under half a second once the cache is cleared -- so `npm test`
  // clears it. Half a second does not need a cache.
  cacheDir: process.env.PAC_WEB_CACHE_DIR || "node_modules/.vite",
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.jsx"],
    // A hang here means a promise the component never settles, which is a
    // result worth seeing rather than waiting on.
    testTimeout: 10000,
    hookTimeout: 10000,
    teardownTimeout: 5000,
  },
});
