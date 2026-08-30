import "server-only";

import { Pool, type PoolClient, type QueryResultRow } from "pg";

/**
 * PostgreSQL connection pool for the web tier.
 *
 * SECURITY — this module and everything under `lib/db/` is server-only.
 *
 *   * `import "server-only"` makes importing it from a client component a BUILD
 *     ERROR, not a review catch. The package is a real dependency (it was missing
 *     until Phase 1, which made every such import a silent no-op).
 *   * `DATABASE_URL` is read from the server environment. It is NOT prefixed
 *     `NEXT_PUBLIC_`, so Next.js never inlines it into a client bundle.
 *   * Nothing here is ever re-exported from a component module.
 *
 * MIGRATIONS — Alembic remains the only schema authority. This layer reads and (in
 * later phases) writes rows. It never creates, alters or drops anything, and there is
 * deliberately no Drizzle, no `db push`, and no migrations directory here.
 */

const connectionString = process.env.DATABASE_URL;

if (!connectionString) {
  throw new Error(
    "DATABASE_URL is not set. The web tier reads the database directly as of Phase 2; " +
      "see docs/DOMAIN_MODEL.md and .env.example.",
  );
}

/**
 * A single pool per process.
 *
 * Next.js dev mode re-evaluates modules on every hot reload, which would leak a new
 * pool each time and exhaust `max_connections` within minutes. Stashing it on
 * `globalThis` keeps one pool across reloads. In production the module is evaluated
 * once and this is simply a no-op.
 */
const globalForDb = globalThis as unknown as { __thedropPool?: Pool };

function createPool(): Pool {
  return new Pool({
    connectionString,
    // Deliberately small. Postgres runs with max_connections=60 shared across the
    // API, the worker and this tier (DATABASE.md §12). Serverless makes this worse,
    // not better: every warm function instance holds its own pool.
    max: Number(process.env.DATABASE_POOL_MAX ?? 5),
    idleTimeoutMillis: 30_000,
    // Fail fast rather than inheriting the OS TCP timeout, which on Windows is about
    // two minutes and turns a database outage into a hung request.
    connectionTimeoutMillis: 5_000,
    application_name: "thedrop-web",
  });
}

export const pool: Pool = globalForDb.__thedropPool ?? createPool();

if (process.env.NODE_ENV !== "production") {
  globalForDb.__thedropPool = pool;
}

// An idle client erroring (e.g. the database restarted) emits on the pool. Without a
// listener, Node treats it as an unhandled error event and crashes the process.
pool.on("error", (error) => {
  console.error("[db] idle client error", error);
});

/** Raised when Postgres is unreachable, so callers can answer 503 rather than 500. */
export class DatabaseUnavailableError extends Error {
  constructor(cause: unknown) {
    super("Database unavailable");
    this.name = "DatabaseUnavailableError";
    this.cause = cause;
  }
}

const UNAVAILABLE_CODES = new Set([
  "ECONNREFUSED",
  "ETIMEDOUT",
  "ENOTFOUND",
  "EHOSTUNREACH",
  "57P01", // admin_shutdown
  "57P03", // cannot_connect_now
  "08000", // connection_exception
  "08006", // connection_failure
]);

function isUnavailable(error: unknown): boolean {
  const code = (error as { code?: string } | null)?.code;
  return typeof code === "string" && UNAVAILABLE_CODES.has(code);
}

/**
 * Parameterized query. There is no string-interpolation path by design — the same
 * rule as the Python side: no f-string SQL, ever.
 */
export async function query<T extends QueryResultRow>(
  text: string,
  params: readonly unknown[] = [],
): Promise<T[]> {
  try {
    const result = await pool.query<T>(text, params as unknown[]);
    return result.rows;
  } catch (error) {
    if (isUnavailable(error)) throw new DatabaseUnavailableError(error);
    throw error;
  }
}

/** Single row or null. */
export async function queryOne<T extends QueryResultRow>(
  text: string,
  params: readonly unknown[] = [],
): Promise<T | null> {
  const rows = await query<T>(text, params);
  return rows[0] ?? null;
}

/** Transactional scope. Used by later phases; reads do not need it. */
export async function withTransaction<T>(fn: (client: PoolClient) => Promise<T>): Promise<T> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const result = await fn(client);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    await client.query("ROLLBACK");
    if (isUnavailable(error)) throw new DatabaseUnavailableError(error);
    throw error;
  } finally {
    client.release();
  }
}
