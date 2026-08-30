import "server-only";

import { query, queryOne } from "@/lib/db/client";
import { redis } from "@/lib/redis/client";

/**
 * Admin session validation — a faithful port of `get_current_user` in
 * `services/api/app/deps.py`.
 *
 * PHASE 3C SCOPE: this module only *reads and validates* sessions. FastAPI remains the
 * sole session creator; `POST /auth/login` and `POST /auth/logout` are untouched. The
 * Redis key format, payload shape, TTLs and epoch semantics are unchanged — this code
 * is a second reader of the same store.
 *
 * The order of operations is load-bearing and mirrors the Python source exactly,
 * including which failures delete the key:
 *
 *   1. no/empty cookie            -> 401 "Not authenticated"        (no side effect)
 *   2. key absent from Redis      -> 401 "Session expired"          (no side effect)
 *   3. absolute_expiry passed     -> 401 "Session expired"          (DELETE key)
 *   4. user missing or inactive   -> 401 "Account unavailable"      (DELETE key)
 *   5. epoch mismatch             -> 401 "Session invalidated"      (DELETE key)
 *   6. otherwise                  -> refresh idle TTL, return user
 *
 * Step 6 is the one most easily dropped. Without it a session expires two hours after
 * login no matter how active the user is, and the failure only appears two hours later.
 *
 * Session identifiers are never logged — they are bearer credentials.
 */

const IDLE_TTL_HOURS = Number(process.env.SESSION_IDLE_TTL_HOURS ?? 2);
const IDLE_TTL_SECONDS = IDLE_TTL_HOURS * 3600;

function sessionKey(sessionId: string): string {
  return `session:${sessionId}`;
}

/** The payload FastAPI writes. Documented in docs/API_BASELINE.md §6. */
interface SessionPayload {
  user_id: number;
  email: string;
  roles: string[];
  epoch: number;
  created_at: string;
  absolute_expiry: string;
}

export interface AdminUser {
  publicId: string;
  email: string;
  displayName: string;
  roles: string[];
  mfaEnabled: boolean;
}

export type SessionFailureDetail =
  | "Not authenticated"
  | "Session expired"
  | "Account unavailable"
  | "Session invalidated";

export type SessionResult =
  | { ok: true; user: AdminUser }
  | { ok: false; detail: SessionFailureDetail };

interface UserRow {
  public_id: string;
  email: string;
  display_name: string;
  mfa_enabled: boolean;
  is_active: boolean;
  session_epoch: number;
}

/**
 * Validate a session id against Redis and Postgres.
 *
 * Throws only on genuine infrastructure failure or a malformed payload — a corrupt
 * payload raises in Python too (`json.loads`), producing a 500, and matching that is
 * deliberate rather than accidental.
 */
export async function validateSession(sessionId: string | undefined): Promise<SessionResult> {
  // 1. Missing or empty cookie. Python's `if not session_cookie` treats "" as absent.
  if (!sessionId) {
    return { ok: false, detail: "Not authenticated" };
  }

  const key = sessionKey(sessionId);

  // 2. No key in Redis — natural TTL expiry leaves exactly this state.
  const raw = await redis.get(key);
  if (raw === null) {
    return { ok: false, detail: "Session expired" };
  }

  const payload = JSON.parse(raw) as SessionPayload;

  // 3. Absolute lifetime, checked in application code rather than by a Redis TTL.
  // A sliding TTL alone would let an active session live forever.
  if (new Date(payload.absolute_expiry).getTime() < Date.now()) {
    await redis.del(key);
    return { ok: false, detail: "Session expired" };
  }

  const user = await queryOne<UserRow>(
    `SELECT public_id::text AS public_id, email, display_name,
            mfa_enabled, is_active, session_epoch
       FROM users WHERE id = $1`,
    [payload.user_id],
  );

  // 4. Deleted or deactivated account.
  if (user === null || !user.is_active) {
    await redis.del(key);
    return { ok: false, detail: "Account unavailable" };
  }

  // 5. Epoch mismatch: the incident-response kill switch. Bumping session_epoch
  // invalidates every session for a user at once, without waiting for a TTL.
  if (user.session_epoch !== payload.epoch) {
    await redis.del(key);
    return { ok: false, detail: "Session invalidated" };
  }

  // Roles are re-read from the database, NOT taken from the session payload, so a
  // revoked role takes effect on the next request rather than at next login.
  //
  // CANONICAL ROLE ORDERING: alphabetical by slug — see the `User.roles` relationship
  // in packages/database/.../models/auth.py for why slug rather than id.
  //
  // Both tiers sort in the database, so they share one collation and cannot disagree.
  // Sorting in application code would risk Python's codepoint order diverging from
  // Postgres's collation for any slug outside plain lowercase ASCII.
  const roleRows = await query<{ slug: string }>(
    `SELECT r.slug FROM roles r
       JOIN user_roles ur ON ur.role_id = r.id
      WHERE ur.user_id = $1
      ORDER BY r.slug`,
    [payload.user_id],
  );

  // 6. Slide the idle window. Must happen only after every check has passed.
  await redis.expire(key, IDLE_TTL_SECONDS);

  return {
    ok: true,
    user: {
      publicId: user.public_id,
      email: user.email,
      displayName: user.display_name,
      roles: roleRows.map((r) => r.slug),
      mfaEnabled: user.mfa_enabled,
    },
  };
}

/**
 * RBAC check, mirroring `require_role` in deps.py: `admin` implicitly satisfies every
 * requirement. Not used by `/auth/me`, which requires only authentication, but defined
 * here so the boundary lives with the session code it belongs to.
 */
export function hasRole(user: AdminUser, ...allowed: string[]): boolean {
  return user.roles.includes("admin") || allowed.some((role) => user.roles.includes(role));
}
