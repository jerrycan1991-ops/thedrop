import type { NextRequest } from "next/server";

import {
  QueryValidator,
  handleRoute,
  ok,
  requestIdFrom,
  validationFailed,
} from "@/lib/api/contract";
import { listLatest } from "@/lib/db/queries/public";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const requestId = requestIdFrom(request);

  return handleRoute(requestId, async () => {
    const validator = new QueryValidator(request.nextUrl.searchParams);
    const limit = validator.int("limit", { default: 20, ge: 1, le: 50 });

    if (validator.failed) {
      return validationFailed(validator.collected, requestId);
    }

    // Python emits `datetime.now(UTC).isoformat()`. The baseline normalises this
    // field, so only its presence and non-null-ness are compared.
    return ok(
      {
        items: await listLatest(limit),
        generatedAt: new Date().toISOString().replace(/\.(\d{3})Z$/, ".$1000+00:00"),
      },
      requestId,
    );
  });
}
