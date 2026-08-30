import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

import { requireRole } from "@/lib/api/admin-guard";
import { handleRoute, requestIdFrom } from "@/lib/api/contract";
import { SESSION_COOKIE_NAME, destroySession } from "@/lib/auth/login";

export const dynamic = "force-dynamic";

const COOKIE_DOMAIN = process.env.COOKIE_DOMAIN || undefined;

/**
 * Mirrors the `logout` handler in services/api/app/routers/admin.py.
 *
 * Requires authentication: an expired or forged session gets the same 401 as any
 * other admin route, not a courtesy 200.
 *
 * The cookie is deleted with the SAME `path` and `domain` used when setting it.
 * Mismatched attributes are the classic cause of "logout leaves you logged in": the
 * browser keeps the original cookie and only shadows it, so the next request still
 * carries a session id — one that has already been destroyed server-side, which at
 * least fails closed, but the user sees a confusing half-logged-in state.
 *
 * No audit row is written. Login success and failure are both audited and logout is
 * not; that asymmetry is a documented gap (SECURITY.md §9), preserved here rather
 * than quietly corrected during a migration.
 */
export async function POST(request: NextRequest) {
  const requestId = requestIdFrom(request);

  return handleRoute(requestId, async () => {
    // No role requirement — any authenticated user may end their own session.
    const guard = await requireRole(request, requestId);
    if (!guard.ok) return guard.response;

    const sessionId = request.cookies.get(SESSION_COOKIE_NAME)?.value;
    if (sessionId) {
      await destroySession(sessionId);
    }

    const response = NextResponse.json({ status: "ok" });
    response.headers.set("X-Request-ID", requestId);
    response.cookies.set({
      name: SESSION_COOKIE_NAME,
      value: "",
      maxAge: 0,
      path: "/",
      domain: COOKIE_DOMAIN,
    });
    return response;
  });
}
