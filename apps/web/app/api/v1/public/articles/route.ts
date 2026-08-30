import type { NextRequest } from "next/server";

import { QueryValidator, handleRoute, newRequestId, ok, validationFailed } from "@/lib/api/contract";
import { listArticles } from "@/lib/db/queries/public";

export const dynamic = "force-dynamic";

/**
 * Mirrors `list_articles` in services/api/app/routers/public.py.
 *
 * Parameters are validated in declaration order (category, page, page_size) and ALL
 * failures are reported together, matching FastAPI. An unknown category is a filter
 * that matches nothing — 200 with an empty list, never 404.
 */
export async function GET(request: NextRequest) {
  const requestId = newRequestId();

  return handleRoute(requestId, async () => {
    const validator = new QueryValidator(request.nextUrl.searchParams);

    const category = validator.optionalString("category", { maxLength: 64 });
    const page = validator.int("page", { default: 1, ge: 1, le: 500 });
    const pageSize = validator.int("page_size", { default: 20, ge: 1, le: 50 });

    if (validator.failed) {
      return validationFailed(validator.collected, requestId);
    }

    return ok(await listArticles({ category, page, pageSize }), requestId);
  });
}
