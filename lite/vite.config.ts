import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// Relative base: the built site works from any GitHub Pages path (https://<user>.github.io/<repo>/).
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, target: "es2022", chunkSizeWarningLimit: 1500 },
  worker: { format: "es" },
  test: { environment: "node", include: ["tests/**/*.test.ts"] },
} as any);
