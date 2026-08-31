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

  /**
   * Proxy ONLY the paths FastAPI still owns.
   *
   * This used to be a catch-all `/api/v1/:path*`, on the assumption that a Next.js
   * route handler always wins over an `afterFiles` rewrite. That assumption is wrong
   * for deeply-nested dynamic routes: `/api/v1/public/articles/[category]/[year]/
   * [month]/[day]/[slug]` lost to the catch-all and was served by FastAPI in BOTH dev
   * and a production build, even though the handler existed and appeared in the route
   * manifest. Nothing caught it because both tiers returned byte-identical responses —
   * it was only provable with a controlled difference (a production server with no
   * DATABASE_URL: Node 500s, the proxy returns FastAPI's 404).
   *
   * Listing the FastAPI-owned paths explicitly makes ownership a declaration rather
   * than a race against framework precedence, and any future migration is one line
   * removed from this list.
   *
   * ROLLBACK: to hand an endpoint back to FastAPI, delete the Node route file AND add
   * its path here. Restoring the old catch-all below also works and returns every
   * unmatched /api/v1 path to FastAPI:
   *
   *   { source: "/api/v1/:path*", destination: `${API_INTERNAL_URL}/api/v1/:path*` }
   *
   * Proxying keeps browser requests first-party, so the session cookie stays a
   * first-party cookie on thedrop.channel and no CORS preflight sits on the hot path.
   */
  async rewrites() {
    return [
      // Worker lease, heartbeat, job claim/complete/fail, status.
      {
        source: "/api/v1/worker/:path*",
        destination: `${API_INTERNAL_URL}/api/v1/worker/:path*`,
      },
      // PUT /admin/settings/{key} — the only admin WRITE still in FastAPI.
      // `:key` matches exactly one segment, so it cannot shadow the Node-owned
      // `/api/v1/admin/settings` collection route.
      {
        source: "/api/v1/admin/settings/:key",
        destination: `${API_INTERNAL_URL}/api/v1/admin/settings/:key`,
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
