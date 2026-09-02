import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { middleware } from "./middleware";

/**
 * The admin gate must never build its redirect from the request's Host header.
 *
 * `new URL("/admin/login", request.url)` looks harmless and was wrong twice over:
 *
 *   1. Security. `request.url` derives from the Host header, which the client
 *      controls. A request with `Host: evil.com` made the gate answer
 *      `Location: https://evil.com/admin/login?next=/admin` — an open redirect on the
 *      one route whose entire job is to stop unauthenticated access.
 *
 *   2. Production. The hosting panel's nginx proxies to the Node process without
 *      `proxy_set_header Host $host`, so Next.js saw `Host: localhost:3100` and sent
 *      every admin to a machine-local address that resolves to nothing from a browser.
 *      The site looked healthy; the admin was simply unreachable.
 *
 * The redirect base is now SITE.url, which comes from NEXT_PUBLIC_SITE_URL and is
 * inlined at build time. These tests fail if anyone reintroduces the request-derived
 * form.
 */

const SESSION_COOKIE = "thedrop_session";

/** SITE.url resolves to this when NEXT_PUBLIC_SITE_URL is unset, as it is under test. */
const CONFIGURED_ORIGIN = "http://localhost:3100";

function requestFor(url: string, host?: string): NextRequest {
  const request = new NextRequest(new URL(url), {
    headers: host ? { host } : undefined,
  });
  return request;
}

describe("admin gate redirect", () => {
  it("sends an unauthenticated caller to the login page", () => {
    const response = middleware(requestFor("https://thedrop.channel/admin"));

    expect(response.status).toBe(307);
    const location = new URL(response.headers.get("location") ?? "");
    expect(location.pathname).toBe("/admin/login");
    expect(location.searchParams.get("next")).toBe("/admin");
  });

  it("ignores a forged Host header when building the redirect", () => {
    const response = middleware(
      requestFor("https://evil.example/admin/articles", "evil.example"),
    );

    const location = response.headers.get("location") ?? "";
    expect(location).not.toContain("evil.example");
    expect(location.startsWith(CONFIGURED_ORIGIN)).toBe(true);
  });

  it("uses the configured origin, not the origin the request arrived on", () => {
    // Reproduces the production failure: nginx forwards Host: localhost:3100, but the
    // redirect must still point at the configured public origin.
    const response = middleware(requestFor("http://localhost:3100/admin"));

    const location = new URL(response.headers.get("location") ?? "");
    expect(location.origin).toBe(CONFIGURED_ORIGIN);
  });

  it("lets the login page itself through without redirecting", () => {
    const response = middleware(requestFor("https://thedrop.channel/admin/login"));

    expect(response.headers.get("location")).toBeNull();
  });

  it("does not redirect when a session cookie is present", () => {
    const request = requestFor("https://thedrop.channel/admin");
    request.cookies.set(SESSION_COOKIE, "any-value");

    const response = middleware(request);

    expect(response.headers.get("location")).toBeNull();
    // The gate is not the security boundary, but it must still mark the admin
    // unindexable and unframeable on the way through.
    expect(response.headers.get("X-Robots-Tag")).toContain("noindex");
    expect(response.headers.get("X-Frame-Options")).toBe("DENY");
  });
});
