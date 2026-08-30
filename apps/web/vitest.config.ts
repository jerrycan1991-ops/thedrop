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
    env: {
      // The pool is constructed lazily and never connects during these tests -- they
      // exercise pure serialisation and validation helpers. A value is required only
      // because the db module refuses to load without one.
      DATABASE_URL: "postgresql://test:test@127.0.0.1:5432/test",
    },
  },
  resolve: {
    alias: {
      // `server-only` throws unless imported under React's react-server condition,
      // which Vitest does not set. Stubbing it lets server modules be unit-tested;
      // the real guard still applies in every Next.js build.
      "server-only": path.resolve(__dirname, "test/stubs/server-only.ts"),
      "@": path.resolve(__dirname),
      "@thedrop/config": path.resolve(__dirname, "../../packages/config/src/index.ts"),
      "@thedrop/shared": path.resolve(__dirname, "../../packages/shared/src/index.ts"),
    },
  },
});
