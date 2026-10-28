import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    globals: true,
  },
  server: {
    port: 5173,
    proxy: {
      // Main PipelineIQ API (services/api).
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      // Infra demo page only: talks to the Infra expert directly.
      "/infra-api": {
        target: "http://127.0.0.1:8104",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/infra-api/, ""),
      },
    },
  },
});
