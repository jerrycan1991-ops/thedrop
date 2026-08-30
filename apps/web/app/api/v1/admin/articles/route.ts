import type { NextRequest } from "next/server";

import { adminJson, requireRole } from "@/lib/api/admin-guard";
import { QueryValidator, handleRoute, requestIdFrom, validationFailed } from "@/lib/api/contract";
import { listAdminArticles } from "@/lib/db/queries/admin";

export const dynamic = "force-dynamic";

/**
 * Mirrors `list_articles` in services/api/app/routers/admin.py.
 *
 * NOTE the deliberate asymmetry with the public list endpoint: the Python original
 * declares `page: int = 1, page_size: int = 25` as plain arguments with NO `ge`/`le`
 * bounds. Non-integers are still 422 (Pydantic coerces the type), but out-of-range
 * values pass straight through to PostgreSQL, where a negative OFFSET or LIMIT errors
 * and surfaces as a 500.
 *
 * Verified against FastAPI: page=0 -> 500, page=-3 -> 500, page_size=-1 -> 500,
 * page_size=0 -> 200 (empty), page_size=100000 -> 200. All reproduced here rather
 * than corrected; changing them would be a behaviour change smuggled into a migration.
 */
export async function GET(request: NextRequest) {
  const requestId = requestIdFrom(request);

  return handleRoute(requestId, async () => {
    const guard = await requireRole(request, requestId, "editor", "analyst", "viewer");
    if (!guard.ok) return guard.response;

    const validator = new QueryValidator(request.nextUrl.searchParams);
    // No ge/le — matching the Python signature exactly.
    const page = validator.int("page", { default: 1 });
    const pageSize = validator.int("page_size", { default: 25 });

    if (validator.failed) {
      return validationFailed(validator.collected, requestId);
    }

    const statusFilter = request.nextUrl.searchParams.get("status_filter");

    return adminJson(
      await listAdminArticles({ statusFilter, page, pageSize }),
      requestId,
    );
  });
}
