import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = `http://${process.env.ANIVAG_HTTP_HOST || "127.0.0.1"}:${process.env.ANIVAG_HTTP_PORT || "9621"}`;

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": apiTarget,
      "/healthz": apiTarget,
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
  },
});
