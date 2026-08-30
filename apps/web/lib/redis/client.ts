import "server-only";

import Redis from "ioredis";

/**
 * Redis connection for the web tier.
 *
 * SECURITY — server-only, same rules as the database module:
 *   * `import "server-only"` makes a client-component import a BUILD ERROR.
 *   * `REDIS_URL` contains the password and is NOT `NEXT_PUBLIC_`, so Next.js never
 *     inlines it into a client bundle.
 *   * Session identifiers are never logged. They are bearer credentials: anything
 *     that can read a log could then impersonate the user.
 *
 * Phase 3C reads sessions that FastAPI created. It does not create or destroy them
 * except via the two deletions the Python implementation also performs (see
 * `lib/auth/session.ts`).
 */

const url = process.env.REDIS_URL;

if (!url) {
  throw new Error(
    "REDIS_URL is not set. The web tier validates admin sessions against Redis as of " +
      "Phase 3C; see .env.example.",
  );
}

/**
 * One client per process, stashed on globalThis so Next.js hot reloads in development
 * do not leak a new connection on every edit.
 */
const globalForRedis = globalThis as unknown as { __thedropRedis?: Redis };

function createClient(): Redis {
  const client = new Redis(url as string, {
    // Fail fast rather than queueing forever behind an unreachable server: a Redis
    // outage should surface as an error, not as a hung request holding a worker.
    maxRetriesPerRequest: 2,
    connectTimeout: 5_000,
    enableOfflineQueue: false,
    lazyConnect: false,
    // Distinguishes this client in `CLIENT LIST` during incident triage.
    connectionName: "thedrop-web",
  });

  // Without a listener, an emitted error is an unhandled event that crashes Node.
  client.on("error", (error) => {
    console.error("[redis] client error", error.message);
  });

  return client;
}

export const redis: Redis = globalForRedis.__thedropRedis ?? createClient();

if (process.env.NODE_ENV !== "production") {
  globalForRedis.__thedropRedis = redis;
}

/** Raised when Redis is unreachable, so callers can answer 503 rather than 500. */
export class RedisUnavailableError extends Error {
  constructor(cause: unknown) {
    super("Session store unavailable");
    this.name = "RedisUnavailableError";
    this.cause = cause;
  }
}
