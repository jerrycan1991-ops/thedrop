import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    include: ["**/*.test.{ts,tsx}"],
    exclude: ["node_modules", ".next", "e2e"],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname),
      "@thedrop/config": path.resolve(__dirname, "../../packages/config/src/index.ts"),
      "@thedrop/shared": path.resolve(__dirname, "../../packages/shared/src/index.ts"),
    },
  },
});
