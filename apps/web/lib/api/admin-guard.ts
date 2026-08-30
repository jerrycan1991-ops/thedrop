import "server-only";

import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

import { type AdminUser, validateSession } from "@/lib/auth/session";

/**
 * Authentication + RBAC for admin routes, mirroring `get_current_user` and
 * `require_role` in `services/api/app/deps.py`.
 *
 * The existing FastAPI behaviour is the contract — including its inconsistencies.
 * `editor` can read /settings but not /system/metrics, while `viewer` can read
 * /system/metrics but not /settings; neither role is a superset of the other. Those
 * are pinned by tests/test_rbac_matrix.py and are NOT corrected here. Redesigning
 * permissions during a migration would make a policy change indistinguishable from a
 * porting bug.
 */

const SESSION_COOKIE = "thedrop_session";

export type GuardResult =
  | { ok: true; user: AdminUser }
  | { ok: false; response: NextResponse };

function deny(status: number, detail: string, requestId: string): NextResponse {
  // FastAPI's HTTPException emits exactly `{"detail": ...}` with no Cache-Control.
  const response = NextResponse.json({ detail }, { status });
  response.headers.set("X-Request-ID", requestId);
  return response;
}

/**
 * Authenticate, then authorize.
 *
 * `allowed` is the role list from the FastAPI dependency. `admin` implicitly satisfies
 * every requirement. An empty list means "any authenticated user", which is how
 * /auth/me and /auth/logout behave.
 */
export async function requireRole(
  request: NextRequest,
  requestId: string,
  ...allowed: string[]
): Promise<GuardResult> {
  const sessionId = request.cookies.get(SESSION_COOKIE)?.value;
  const session = await validateSession(sessionId);

  if (!session.ok) {
    return { ok: false, response: deny(401, session.detail, requestId) };
  }

  const { user } = session;
  if (allowed.length === 0) {
    return { ok: true, user };
  }

  const held = new Set(user.roles);
  if (held.has("admin") || allowed.some((role) => held.has(role))) {
    return { ok: true, user };
  }

  // 403, not 401: the caller is authenticated, just not permitted.
  return { ok: false, response: deny(403, "Insufficient permissions", requestId) };
}

/** Admin responses carry no Cache-Control, matching FastAPI. */
export function adminJson(body: unknown, requestId: string, status = 200): NextResponse {
  const response = NextResponse.json(body, { status });
  response.headers.set("X-Request-ID", requestId);
  return response;
}
