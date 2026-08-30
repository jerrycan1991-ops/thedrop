import "server-only";

/**
 * Timestamp serialisation that matches Python's `datetime.isoformat()` exactly.
 *
 * Two things make this harder than it looks, and both were latent defects in the
 * Phase 2 migration that no test caught — `publishedAt`, `createdAt` and `updatedAt`
 * are all in the baseline's VOLATILE_KEYS, so every comparison normalised them away.
 *
 * 1. Python OMITS the fractional part when microseconds are zero:
 *        12:00:00.000000  ->  "2026-08-20T12:00:00+00:00"
 *        12:00:00.123456  ->  "2026-08-20T12:00:00.123456+00:00"
 *    JavaScript's `toISOString()` always emits exactly three fractional digits, so
 *    neither case matches.
 *
 * 2. A JavaScript `Date` holds MILLISECONDS. PostgreSQL stores MICROSECONDS. Reading
 *    a timestamp into a Date silently truncates the last three digits, so the value
 *    is wrong before formatting even begins.
 *
 * Both are solved by never letting the timestamp become a Date: PostgreSQL formats it
 * to text with full microsecond precision, and the only work left in JavaScript is
 * dropping a zero fraction.
 */

/**
 * SQL fragment producing a Python-compatible ISO string, minus the offset.
 *
 * The database session runs in UTC (`Etc/UTC`), and every timestamp column is
 * `TIMESTAMPTZ`, so `AT TIME ZONE 'UTC'` yields the same instant Python sees and the
 * offset is always `+00:00`.
 */
export function isoColumn(expression: string, alias: string): string {
  return `to_char(${expression} AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') AS ${alias}`;
}

/** Finish an `isoColumn` value: drop a zero fraction, append the UTC offset. */
export function pyIso(raw: string | null): string | null {
  if (raw === null || raw === undefined) return null;
  const trimmed = raw.endsWith(".000000") ? raw.slice(0, -7) : raw;
  return `${trimmed}+00:00`;
}

/**
 * `datetime.now(UTC).isoformat()` for values generated in the application rather than
 * read from the database. Millisecond precision is padded to six digits, which is what
 * Python emits for a clock that happens to land on a whole millisecond; the field is
 * treated as volatile everywhere it appears, so the padding is never compared.
 */
export function pyIsoNow(now: Date = new Date()): string {
  const ms = now.getUTCMilliseconds();
  const base = now.toISOString().slice(0, 19);
  return ms === 0 ? `${base}+00:00` : `${base}.${String(ms).padStart(3, "0")}000+00:00`;
}
