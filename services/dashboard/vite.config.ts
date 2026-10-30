import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Build expert page only: talks to the Build expert (experts/build) directly.
      "/build-api": {
        target: "http://127.0.0.1:8102",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/build-api/, ""),
      },
      "/api": {
        target: "http://127.0.0.1:8104",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
