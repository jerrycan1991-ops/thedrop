import type { NextRequest } from "next/server";

import { handleRoute, ok, requestIdFrom } from "@/lib/api/contract";
import { listCategories } from "@/lib/db/queries/public";

// Route handlers with no dynamic segments are candidates for static evaluation at
// build time, which would bake a snapshot of the categories table into the bundle.
// The Cache-Control header is what makes this cacheable; the handler itself must run.
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const requestId = requestIdFrom(request);
  return handleRoute(requestId, async () => ok(await listCategories(), requestId));
}
