import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

import { handleRoute, requestIdFrom, validationFailed } from "@/lib/api/contract";
import { validateLoginBody } from "@/lib/api/login-validation";
import { SESSION_COOKIE_NAME, login } from "@/lib/auth/login";

export const dynamic = "force-dynamic";

/**
 * Mirrors the `login` handler in services/api/app/routers/admin.py.
 *
 * Cookie attributes are pinned by tests/baseline/auth_login_contract.json:
 * httpOnly, SameSite=Lax, Path=/, Max-Age=43200, Secure only in production, and a
 * Domain only when COOKIE_DOMAIN is set.
 *
 * SameSite=Lax rather than None is deliberate even when the API lives on a subdomain:
 * the apex and api hosts share a registrable domain, so requests between them are
 * same-site and Lax still applies. None would discard the CSRF protection that Lax
 * provides, which matters more than usual here because no CSRF token is implemented.
 */
const IS_PRODUCTION = process.env.ENVIRONMENT === "production";
const COOKIE_DOMAIN = process.env.COOKIE_DOMAIN || undefined;

export async function POST(request: NextRequest) {
  const requestId = requestIdFrom(request);

  return handleRoute(requestId, async () => {
    let body: unknown;
    try {
      body = await request.json();
    } catch {
      // FastAPI reports a malformed body as a validation failure, not a 400.
      return validationFailed(
        [{ field: "body", message: "Input should be a valid dictionary or object" }],
        requestId,
      );
    }

    const parsed = validateLoginBody(body);
    if (!parsed.ok) {
      return validationFailed(parsed.errors, requestId);
    }

    const forwarded = request.headers.get("x-forwarded-for");
    const ip = forwarded?.split(",")[0]?.trim() || "127.0.0.1";

    const outcome = await login({
      email: parsed.value.email,
      password: parsed.value.password,
      ip,
      userAgent: request.headers.get("user-agent"),
      requestId,
    });

    if (outcome.kind === "rate_limited") {
      return deny(429, "Too many attempts. Try again later.", requestId);
    }
    if (outcome.kind === "locked") {
      return deny(423, "Account temporarily locked", requestId);
    }
    if (outcome.kind === "invalid") {
      // Identical body for an unknown account and a wrong password.
      return deny(401, "Invalid email or password", requestId);
    }

    const response = NextResponse.json({ user: outcome.user });
    response.headers.set("X-Request-ID", requestId);
    response.cookies.set({
      name: SESSION_COOKIE_NAME,
      value: outcome.sessionId,
      httpOnly: true,
      secure: IS_PRODUCTION,
      sameSite: "lax",
      maxAge: outcome.maxAgeSeconds,
      path: "/",
      domain: COOKIE_DOMAIN,
    });
    return response;
  });
}

function deny(status: number, detail: string, requestId: string): NextResponse {
  const response = NextResponse.json({ detail }, { status });
  response.headers.set("X-Request-ID", requestId);
  return response;
}
