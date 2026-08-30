import type { NextRequest } from "next/server";

import { adminJson, requireRole } from "@/lib/api/admin-guard";
import { handleRoute, requestIdFrom } from "@/lib/api/contract";
import { systemMetrics } from "@/lib/db/queries/admin";
import { redis } from "@/lib/redis/client";

export const dynamic = "force-dynamic";

/**
 * Mirrors `system_metrics` in services/api/app/routers/admin.py.
 *
 * RBAC is `analyst`, `viewer` (plus `admin`) — `editor` gets 403. Preserved as-is.
 *
 * The Redis reachability probe is reported, never raised: the Python route wraps
 * `r.ping()` in a bare try/except and returns `redis: false`. A metrics page that
 * 500s because Redis is down tells you nothing; one that says "redis: false" tells
 * you exactly what is wrong.
 */
export async function GET(request: NextRequest) {
  const requestId = requestIdFrom(request);

  return handleRoute(requestId, async () => {
    const guard = await requireRole(request, requestId, "analyst", "viewer");
    if (!guard.ok) return guard.response;

    let redisOk = true;
    try {
      await redis.ping();
    } catch {
      redisOk = false;
    }

    return adminJson(await systemMetrics(redisOk), requestId);
  });
}
