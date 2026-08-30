import path from "node:path";

import { config as loadEnv } from "dotenv";
import type { NextConfig } from "next";

/**
 * Load the monorepo-root `.env`.
 *
 * Next.js only reads `.env` files from its own project root (`apps/web`), but this is
 * a workspace where one `.env` serves the web app, the API, the worker and Alembic.
 * Duplicating it into `apps/web` would mean two files of secrets drifting apart.
 *
 * `override: false` is the important part: values already present in the environment
 * always win. On Vercel, Railway and systemd the platform injects them and this call
 * changes nothing — it is purely a local-development convenience.
 */
loadEnv({ path: path.resolve(process.cwd(), "../../.env"), override: false });

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
  /**
   * Build output directory.
   *
   * Defaults to `.next` for production (the systemd unit and Vercel both expect it).
   * Running `next build` while `next dev` is live rewrites that directory underneath
   * the dev server and corrupts its chunk map — it starts failing with
   * `Cannot find module './NNN.js'` on any route whose chunk changed, which looks
   * exactly like an application bug and is not one.
   *
   * Verification builds set NEXT_DIST_DIR to keep the two apart.
   */
  distDir: process.env.NEXT_DIST_DIR ?? ".next",

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
        // API responses must match FastAPI's headers exactly, or the migration
        // silently changes the contract. FastAPI sends X-Frame-Options: DENY on every
        // response; the site-wide rule above uses SAMEORIGIN, which is correct for
        // pages but is a weakening for the API. Later rules win on duplicate keys.
        //
        // Caught by tests/test_auth_me_parity.py, which compares the full header set
        // rather than a chosen few.
        source: "/api/:path*",
        headers: [{ key: "X-Frame-Options", value: "DENY" }],
      },
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
