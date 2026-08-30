import "server-only";

import { NextResponse } from "next/server";

import { DatabaseUnavailableError } from "@/lib/db/client";

/**
 * The public API response contract, reproduced exactly from the FastAPI original.
 *
 * Every constant and message string here is pinned by `tests/baseline/*.json`,
 * captured from the Python implementation at tag `v0.1.0-hybrid`. "Approximately the
 * same" is not acceptable: `infrastructure/scripts/api_baseline.py compare` diffs
 * status, content type, Cache-Control and the full body.
 *
 * The details that are easy to get wrong, and are all deliberate:
 *   - Success responses carry Cache-Control; ERROR responses carry NONE. FastAPI sets
 *     the header on the injected Response object, but raising HTTPException builds a
 *     fresh response that never sees it.
 *   - Validation messages are Pydantic's exact wording, not our own phrasing.
 *   - Validation reports EVERY failing parameter, in declaration order, not just the
 *     first one.
 *   - `requestId` is always present and non-null on error bodies.
 */

export const CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=300";

export function newRequestId(): string {
  return crypto.randomUUID().replace(/-/g, "");
}

/**
 * Request id for this response, echoing an inbound `X-Request-ID` when present.
 *
 * Mirrors the FastAPI middleware (`request.headers.get("x-request-id") or uuid4`), so
 * a trace id set by an upstream proxy survives regardless of which implementation
 * handles the route. Without this, a request crossing from FastAPI to Node loses its
 * correlation id exactly where you most want to follow it.
 */
export function requestIdFrom(request: { headers: { get(name: string): string | null } }): string {
  return request.headers.get("x-request-id") || newRequestId();
}

function withRequestId(response: NextResponse, requestId: string): NextResponse {
  response.headers.set("X-Request-ID", requestId);
  return response;
}

/** 200 with the public cache header. */
export function ok(body: unknown, requestId: string): NextResponse {
  const response = NextResponse.json(body, {
    status: 200,
    headers: { "Cache-Control": CACHE_CONTROL },
  });
  return withRequestId(response, requestId);
}

/** 404 — no Cache-Control, body is `{detail}` only. */
export function notFound(detail: string, requestId: string): NextResponse {
  return withRequestId(NextResponse.json({ detail }, { status: 404 }), requestId);
}

export interface FieldError {
  field: string;
  message: string;
}

/** 422 — mirrors the custom RequestValidationError handler in services/api/app/main.py. */
export function validationFailed(errors: FieldError[], requestId: string): NextResponse {
  return withRequestId(
    NextResponse.json(
      { detail: "Validation failed", errors, requestId },
      { status: 422 },
    ),
    requestId,
  );
}

/** 503 — mirrors the OperationalError handler, including Retry-After. */
export function databaseUnavailable(requestId: string): NextResponse {
  return withRequestId(
    NextResponse.json(
      {
        detail: "Database unavailable",
        hint: "Is PostgreSQL running? See docs/DEPLOYMENT.md §4.",
        requestId,
      },
      { status: 503, headers: { "Retry-After": "30" } },
    ),
    requestId,
  );
}

/* -------------------------------------------------------------------------- */
/* Query parameter validation — Pydantic-compatible                            */
/* -------------------------------------------------------------------------- */

/**
 * Pydantic v2's exact error strings. Reproduced verbatim because the baseline
 * compares the message text, and because a client parsing these should not have to
 * care which language served the request.
 */
const MESSAGES = {
  notAnInteger: "Input should be a valid integer, unable to parse string as an integer",
  ge: (n: number) => `Input should be greater than or equal to ${n}`,
  le: (n: number) => `Input should be less than or equal to ${n}`,
  maxLength: (n: number) => `String should have at most ${n} characters`,
} as const;

export class QueryValidator {
  private readonly errors: FieldError[] = [];

  constructor(private readonly params: URLSearchParams) {}

  /**
   * Integer query parameter with optional bounds, matching
   * `Annotated[int, Query(ge=..., le=...)]`.
   *
   * Absent means "use the default" and is never an error — same as FastAPI.
   */
  int(name: string, options: { default: number; ge?: number; le?: number }): number {
    const raw = this.params.get(name);
    if (raw === null || raw === "") return options.default;

    // Pydantic accepts surrounding whitespace and rejects floats for an int field.
    const trimmed = raw.trim();
    if (!/^[+-]?\d+$/.test(trimmed)) {
      this.errors.push({ field: `query.${name}`, message: MESSAGES.notAnInteger });
      return options.default;
    }

    const value = Number.parseInt(trimmed, 10);

    if (options.ge !== undefined && value < options.ge) {
      this.errors.push({ field: `query.${name}`, message: MESSAGES.ge(options.ge) });
      return options.default;
    }
    if (options.le !== undefined && value > options.le) {
      this.errors.push({ field: `query.${name}`, message: MESSAGES.le(options.le) });
      return options.default;
    }
    return value;
  }

  /** Optional string with a max length, matching `Query(max_length=n)`. */
  optionalString(name: string, options: { maxLength?: number } = {}): string | null {
    const raw = this.params.get(name);
    if (raw === null) return null;

    if (options.maxLength !== undefined && raw.length > options.maxLength) {
      this.errors.push({
        field: `query.${name}`,
        message: MESSAGES.maxLength(options.maxLength),
      });
      return null;
    }
    return raw;
  }

  get failed(): boolean {
    return this.errors.length > 0;
  }

  /** All failures, in the order the parameters were declared. */
  get collected(): FieldError[] {
    return this.errors;
  }
}

/**
 * Wraps a route handler so a database outage becomes 503 rather than an unhandled 500,
 * matching the FastAPI OperationalError handler.
 *
 * The driver message is logged but never returned — it contains the connection string.
 */
export async function handleRoute(
  requestId: string,
  fn: () => Promise<NextResponse>,
): Promise<NextResponse> {
  try {
    return await fn();
  } catch (error) {
    if (error instanceof DatabaseUnavailableError) {
      console.error("[api] database unavailable", { requestId, cause: error.cause });
      return databaseUnavailable(requestId);
    }
    console.error("[api] unhandled error", { requestId, error });
    return withRequestId(
      NextResponse.json({ detail: "Internal server error", requestId }, { status: 500 }),
      requestId,
    );
  }
}
