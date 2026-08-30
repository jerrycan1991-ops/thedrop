import "server-only";

import { randomBytes } from "node:crypto";

import { query, queryOne, withTransaction } from "@/lib/db/client";
import { needsRehash, hashPassword, verifyPassword } from "@/lib/auth/password";
import { redis } from "@/lib/redis/client";

/**
 * Login, logout and session creation — a faithful port of the `/auth/login` and
 * `/auth/logout` handlers in `services/api/app/routers/admin.py`.
 *
 * The ORDER of checks is a security property, not an implementation detail, and is
 * reproduced exactly:
 *
 *   1. read the rate-limit counter; >= 5 -> 429 (BEFORE any database work)
 *   2. look the user up by lowercased email
 *   3. unknown user, or inactive -> generic 401 (counter incremented)
 *   4. locked_until in the future -> 423
 *   5. wrong password -> increment failed_login_count, lock at 5, audit, generic 401
 *   6. rehash if parameters changed
 *   7. reset counters, stamp last_login_at, create the session, audit, set the cookie
 *
 * Step 1 running before step 4 is why the 423 branch is effectively unreachable: the
 * Redis counter and `failed_login_count` reach five on the same attempt, and the rate
 * limiter answers first. That is the observed behaviour of the existing service and it
 * is preserved deliberately -- "fixing" it here would be a behaviour change disguised
 * as a migration.
 *
 * Unknown accounts and wrong passwords share one exit path with one message, so the
 * response cannot be used to enumerate users.
 */

const RATE_LIMIT_MAX_ATTEMPTS = 5;
const RATE_LIMIT_WINDOW_SECONDS = 900;
const LOCKOUT_MINUTES = 15;

const IDLE_TTL_HOURS = Number(process.env.SESSION_IDLE_TTL_HOURS ?? 2);
const ABSOLUTE_TTL_HOURS = Number(process.env.SESSION_ABSOLUTE_TTL_HOURS ?? 12);
const COOKIE_NAME = process.env.SESSION_COOKIE_NAME ?? "thedrop_session";

export const SESSION_COOKIE_NAME = COOKIE_NAME;

/** `secrets.token_urlsafe(32)` — 32 random bytes, base64url, unpadded. */
function newSessionId(): string {
  return randomBytes(32).toString("base64url");
}

/** Python's `datetime.isoformat()` for a tz-aware UTC value. */
function isoNow(date: Date): string {
  const ms = date.getUTCMilliseconds();
  const base = date.toISOString().slice(0, 19);
  return ms === 0 ? `${base}+00:00` : `${base}.${String(ms).padStart(3, "0")}000+00:00`;
}

export type LoginOutcome =
  | { kind: "ok"; sessionId: string; user: PublicUser; maxAgeSeconds: number }
  | { kind: "rate_limited" }
  | { kind: "invalid" }
  | { kind: "locked" };

export interface PublicUser {
  id: string;
  email: string;
  displayName: string;
  roles: string[];
}

interface UserRow {
  /**
   * `users.id` is BIGINT, and node-postgres returns bigints as STRINGS so that values
   * beyond 2^53 are not silently corrupted. Python writes an int into the session
   * payload, so this must be coerced before it is stored — otherwise FastAPI reads
   * `user_id: "66"`, fails its lookup, and returns 500 for any Node-created session.
   *
   * Caught by TestCrossTierSessionCompatibility, which is the only test that could
   * have caught it: both tiers looked correct in isolation.
   */
  id: string;
  public_id: string;
  email: string;
  password_hash: string;
  display_name: string;
  is_active: boolean;
  session_epoch: number;
  failed_login_count: number;
  locked_until: Date | null;
}

function rateKey(ip: string, email: string): string {
  return `login_attempts:${ip}:${email.toLowerCase()}`;
}

/** INCR + EXPIRE in one round trip, matching the Python pipeline. */
async function recordFailure(key: string): Promise<void> {
  await redis.multi().incr(key).expire(key, RATE_LIMIT_WINDOW_SECONDS).exec();
}

async function auditLog(
  client: Parameters<Parameters<typeof withTransaction>[0]>[0],
  fields: {
    actorType: string;
    actorId: string | null;
    action: string;
    entityType: string;
    entityId: string | null;
    ip: string | null;
    userAgent: string | null;
    requestId: string | null;
  },
): Promise<void> {
  await client.query(
    `INSERT INTO audit_logs
       (actor_type, actor_id, action, entity_type, entity_id, ip, user_agent, request_id, created_at)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())`,
    [
      fields.actorType,
      fields.actorId,
      fields.action,
      fields.entityType,
      fields.entityId,
      fields.ip,
      fields.userAgent,
      fields.requestId,
    ],
  );
}

export async function login(input: {
  email: string;
  password: string;
  ip: string;
  userAgent: string | null;
  requestId: string;
}): Promise<LoginOutcome> {
  const email = input.email.toLowerCase();
  const key = rateKey(input.ip, email);

  // 1. Rate limit, checked before any database work.
  const attempts = Number((await redis.get(key)) ?? 0);
  if (attempts >= RATE_LIMIT_MAX_ATTEMPTS) {
    return { kind: "rate_limited" };
  }

  const user = await queryOne<UserRow>(
    `SELECT id, public_id::text AS public_id, email, password_hash, display_name,
            is_active, session_epoch, failed_login_count, locked_until
       FROM users WHERE email = $1`,
    [email],
  );

  // Coerce the BIGINT id exactly once; see the UserRow comment.
  const userId = user === null ? null : Number(user.id);

  // 3. Unknown or deactivated account — same exit as a wrong password.
  if (user === null || userId === null || !user.is_active) {
    await recordFailure(key);
    return { kind: "invalid" };
  }

  // 4. Explicit lockout. Unreachable in practice (see the module docstring), but
  // present so the ported logic matches statement for statement.
  if (user.locked_until !== null && user.locked_until.getTime() > Date.now()) {
    return { kind: "locked" };
  }

  // 5. Password check.
  if (!(await verifyPassword(user.password_hash, input.password))) {
    const failed = user.failed_login_count + 1;
    const lockedUntil =
      failed >= RATE_LIMIT_MAX_ATTEMPTS
        ? new Date(Date.now() + LOCKOUT_MINUTES * 60_000)
        : null;

    await withTransaction(async (client) => {
      await client.query(
        `UPDATE users
            SET failed_login_count = $1,
                locked_until = COALESCE($2, locked_until)
          WHERE id = $3`,
        [failed, lockedUntil, userId],
      );
      await auditLog(client, {
        actorType: "system",
        actorId: null,
        action: "login.failed",
        entityType: "user",
        entityId: String(userId),
        ip: input.ip,
        userAgent: input.userAgent,
        requestId: input.requestId,
      });
    });

    await recordFailure(key);
    return { kind: "invalid" };
  }

  // 6 + 7. Success: upgrade the hash if parameters changed, reset the counters,
  // stamp the login, write the audit row.
  const rehashed = needsRehash(user.password_hash)
    ? await hashPassword(input.password)
    : null;

  await withTransaction(async (client) => {
    await client.query(
      `UPDATE users
          SET failed_login_count = 0,
              locked_until = NULL,
              last_login_at = now(),
              password_hash = COALESCE($1, password_hash)
        WHERE id = $2`,
      [rehashed, userId],
    );
    await auditLog(client, {
      actorType: "user",
      actorId: String(userId),
      action: "login.success",
      entityType: "user",
      entityId: String(userId),
      ip: input.ip,
      userAgent: input.userAgent,
      requestId: input.requestId,
    });
  });

  // Roles come from the database, ordered alphabetically by slug (the canonical
  // ordering); the session payload stores a snapshot of the same list.
  const roleRows = await query<{ slug: string }>(
    `SELECT r.slug FROM roles r
       JOIN user_roles ur ON ur.role_id = r.id
      WHERE ur.user_id = $1
      ORDER BY r.slug`,
    [userId],
  );
  const roles = roleRows.map((row) => row.slug);

  const sessionId = newSessionId();
  const now = new Date();
  const payload = {
    user_id: userId,
    email: user.email,
    roles,
    epoch: user.session_epoch,
    created_at: isoNow(now),
    absolute_expiry: isoNow(new Date(now.getTime() + ABSOLUTE_TTL_HOURS * 3600_000)),
  };

  await redis.setex(
    `session:${sessionId}`,
    IDLE_TTL_HOURS * 3600,
    JSON.stringify(payload),
  );

  return {
    kind: "ok",
    sessionId,
    maxAgeSeconds: ABSOLUTE_TTL_HOURS * 3600,
    user: {
      id: user.public_id,
      email: user.email,
      displayName: user.display_name,
      roles,
    },
  };
}

/** Destroy a session. Matches `destroy_session` — a plain DELETE, no audit row. */
export async function destroySession(sessionId: string): Promise<void> {
  await redis.del(`session:${sessionId}`);
}
