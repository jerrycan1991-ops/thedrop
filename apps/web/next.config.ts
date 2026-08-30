import type { NextConfig } from "next";

/**
 * The API base the web app talks to.
 *
 * Single-host deployment (VPS): loopback, and the rewrite keeps FastAPI off the
 * public interface with no nginx change (ADR-0004, DEPLOYMENT.md §7).
 *
 * Split deployment (Vercel + Railway): the public API origin. The rewrite is still
 * worth keeping -- see the note on /api/v1 below.
 */
const API_INTERNAL_URL = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000";

// Vercel builds and serves its own output format; `standalone` is only for the
// systemd unit on the VPS, which runs .next/standalone/apps/web/server.js directly.
const isVercel = process.env.VERCEL === "1";

const isProd = process.env.NODE_ENV === "production";

const nextConfig: NextConfig = {
  ...(isVercel ? {} : { output: "standalone" as const }),
  reactStrictMode: true,
  poweredByHeader: false,

  experimental: {
    externalDir: true,
  },

  images: {
    formats: ["image/avif", "image/webp"],
    // Populated when media moves to object storage (see MEDIA_HOSTING note in
    // docs/DEPLOYMENT_CLOUD.md). Local-disk media does not survive a serverless
    // host, so this stays empty until that migration happens -- an empty list is
    // honest, a speculative entry is not.
    remotePatterns: [],
  },

  async rewrites() {
    return [
      {
        // Proxying keeps every browser request first-party: no CORS preflight on
        // the hot path, and the session cookie stays a first-party cookie on
        // thedrop.channel rather than relying on cross-site cookie behaviour that
        // browsers keep tightening. CORS on the API is configured as well, for
        // direct access, but this is the path the app itself uses.
        source: "/api/v1/:path*",
        destination: `${API_INTERNAL_URL}/api/v1/:path*`,
      },
    ];
  },

  async headers() {
    const securityHeaders = [
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
      {
        key: "Permissions-Policy",
        value: "camera=(), microphone=(), geolocation=(), interest-cohort=()",
      },
      { key: "X-Frame-Options", value: "SAMEORIGIN" },
      ...(isProd
        ? [
            {
              // Two years, subdomains included. Do NOT add `preload` until you are
              // certain every subdomain of thedrop.channel can serve HTTPS forever
              // -- preload removal takes months.
              key: "Strict-Transport-Security",
              value: "max-age=63072000; includeSubDomains",
            },
          ]
        : []),
    ];

    return [
      { source: "/:path*", headers: securityHeaders },
      {
        source: "/media/:path*",
        headers: [{ key: "Cache-Control", value: "public, max-age=31536000, immutable" }],
      },
      {
        // The admin is per-user and must never be cached by a CDN edge.
        source: "/admin/:path*",
        headers: [
          { key: "Cache-Control", value: "no-store, must-revalidate" },
          { key: "X-Robots-Tag", value: "noindex, nofollow, noarchive" },
        ],
      },
    ];
  },
};

export default nextConfig;
