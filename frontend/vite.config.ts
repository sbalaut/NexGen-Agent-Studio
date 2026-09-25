import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// Relative base so the UI also works behind the JupyterHub proxy (/user/<name>/proxy/8600/).
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: { outDir: "../backend/nexagent/static", emptyOutDir: true, chunkSizeWarningLimit: 900 },
  server: { proxy: { "/api": "http://127.0.0.1:8600" } },
});
