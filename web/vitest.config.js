import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Off ~/Desktop on purpose: macOS stalls reads under it (the same problem
  // that made a Python venv there take 82s to import), which leaves vitest
  // workers blocked at 0% CPU instead of running.
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
