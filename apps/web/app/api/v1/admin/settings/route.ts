import type { NextRequest } from "next/server";

import { adminJson, requireRole } from "@/lib/api/admin-guard";
import { handleRoute, requestIdFrom } from "@/lib/api/contract";
import { listSettings } from "@/lib/db/queries/admin";

export const dynamic = "force-dynamic";

/**
 * Mirrors `list_settings` in services/api/app/routers/admin.py.
 *
 * RBAC is `editor` (plus `admin` implicitly) — analyst and viewer get 403. That is the
 * inverse of /system/metrics and is not a mistake in this port; see the inconsistency
 * findings pinned in tests/test_rbac_matrix.py.
 *
 * `isProtected` is returned as stored. It marks the verification, security and audit
 * controls that the self-improvement framework may never modify.
 */
export async function GET(request: NextRequest) {
  const requestId = requestIdFrom(request);

  return handleRoute(requestId, async () => {
    const guard = await requireRole(request, requestId, "editor");
    if (!guard.ok) return guard.response;

    return adminJson(await listSettings(), requestId);
  });
}
