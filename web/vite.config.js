import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API is served from the same origin in production, so the session cookie
// needs no CORS and no cross-site exemption. In development the dev server
// proxies to the backend to preserve that property.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
