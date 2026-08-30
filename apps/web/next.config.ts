import type { NextConfig } from "next";

/**
 * The API is never exposed publicly. Next.js rewrites /api/* to FastAPI on loopback,
 * which is what lets Phase 1 ship with ZERO nginx changes (ADR-0004, DEPLOYMENT.md §7).
 *
 * The hop costs ~2-4ms. If it ever shows up in p95, the fix is one `location /api/`
 * block added through the hosting panel -- not a code change.
 */
const API_INTERNAL_URL = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // Produces .next/standalone, which the systemd unit runs directly. No `next start`,
  // no node_modules on the server.
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,

  experimental: {
    // Media is served from a symlinked public dir; keep the tracing root at the repo
    // root so the standalone bundle resolves workspace packages.
    externalDir: true,
  },

  images: {
    // All imagery is our own, generated locally and served from disk. No remote
    // patterns: there is deliberately no code path that rehosts third-party media.
    formats: ["image/avif", "image/webp"],
    remotePatterns: [],
  },

  async rewrites() {
    return [
      {
        source: "/api/v1/:path*",
        destination: `${API_INTERNAL_URL}/api/v1/:path*`,
      },
    ];
  },

  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=(), interest-cohort=()",
          },
        ],
      },
      {
        // Content-addressed media paths are immutable, so caching is trivially correct.
        source: "/media/:path*",
        headers: [{ key: "Cache-Control", value: "public, max-age=31536000, immutable" }],
      },
    ];
  },
};

export default nextConfig;
