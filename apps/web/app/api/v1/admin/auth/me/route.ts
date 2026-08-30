import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

import { handleRoute, requestIdFrom } from "@/lib/api/contract";
import { validateSession } from "@/lib/auth/session";

/**
 * GET /api/v1/admin/auth/me — the first migrated admin endpoint (Phase 3C).
 *
 * FastAPI still creates and destroys sessions; this route only validates one. Its
 * Python counterpart in `services/api/app/routers/admin.py` is untouched and remains
 * reachable on port 8000 for parity testing and rollback.
 *
 * Admin responses are per-user and must never be cached — matching FastAPI, which
 * sends no Cache-Control on this route at all.
 */
export const dynamic = "force-dynamic";

const SESSION_COOKIE = "thedrop_session";

export async function GET(request: NextRequest) {
  const requestId = requestIdFrom(request);

  return handleRoute(requestId, async () => {
    const sessionId = request.cookies.get(SESSION_COOKIE)?.value;
    const result = await validateSession(sessionId);

    if (!result.ok) {
      // FastAPI's HTTPException(401, detail) produces exactly `{"detail": ...}` with
      // no Cache-Control and no WWW-Authenticate header.
      const response = NextResponse.json({ detail: result.detail }, { status: 401 });
      response.headers.set("X-Request-ID", requestId);
      return response;
    }

    const { user } = result;
    const response = NextResponse.json({
      id: user.publicId,
      email: user.email,
      displayName: user.displayName,
      roles: user.roles,
      mfaEnabled: user.mfaEnabled,
    });
    response.headers.set("X-Request-ID", requestId);
    return response;
  });
}
