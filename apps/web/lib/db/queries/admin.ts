import "server-only";

import { query, queryOne } from "@/lib/db/client";
import { isoColumn, pyIso, pyIsoNow } from "@/lib/db/datetime";

/**
 * Read queries backing the admin API — transliterations of
 * `services/api/app/routers/admin.py`.
 *
 * Two deliberate differences from the public query layer, both because the Python
 * originals differ:
 *
 *   * `/admin/articles` uses a TRUE `COUNT(*)`, not the public endpoint's estimate.
 *   * `/admin/articles` has NO bounds on `page`/`page_size`. A negative offset or
 *     limit therefore reaches PostgreSQL and errors, producing a 500. That is the
 *     existing behaviour (verified: page=0, page=-3 and page_size=-1 all return 500),
 *     and it is reproduced rather than corrected — a migration is the wrong moment to
 *     change what an endpoint does.
 */

/* ----------------------------------------------------------------- articles */

interface AdminArticleRow {
  public_id: string;
  headline: string;
  slug: string;
  status: string;
  article_type: string;
  category_slug: string;
  risk_tier: string;
  editorial_confidence: number | null;
  published_at_iso: string | null;
  created_at_iso: string;
}

export interface AdminArticleList {
  items: Record<string, unknown>[];
  total: number;
  page: number;
  pageSize: number;
}

export async function listAdminArticles(options: {
  statusFilter: string | null;
  page: number;
  pageSize: number;
}): Promise<AdminArticleList> {
  const { statusFilter, page, pageSize } = options;

  // Python applies the filter only when `status_filter` is truthy, so an empty string
  // means "no filter" rather than "status = ''".
  const filtered = Boolean(statusFilter);
  const where = `WHERE a.deleted_at IS NULL${filtered ? " AND a.status = $1" : ""}`;

  const countRow = await queryOne<{ total: string }>(
    `SELECT count(*) AS total FROM articles a ${where}`,
    filtered ? [statusFilter] : [],
  );
  // count(*) comes back as a string from pg (bigint); Python returns an int.
  const total = Number(countRow?.total ?? 0);

  const offset = (page - 1) * pageSize;
  const params: unknown[] = filtered ? [statusFilter, offset, pageSize] : [offset, pageSize];
  const offsetParam = filtered ? "$2" : "$1";
  const limitParam = filtered ? "$3" : "$2";

  const rows = await query<AdminArticleRow>(
    `SELECT
       a.public_id::text AS public_id,
       a.headline,
       a.slug,
       a.status,
       a.article_type,
       c.slug AS category_slug,
       a.risk_tier,
       a.editorial_confidence,
       ${isoColumn("a.published_at", "published_at_iso")},
       ${isoColumn("a.created_at", "created_at_iso")}
     FROM articles a
     JOIN categories c ON c.id = a.category_id
     ${where}
     ORDER BY a.created_at DESC
     OFFSET ${offsetParam} LIMIT ${limitParam}`,
    params,
  );

  return {
    items: rows.map((row) => ({
      id: row.public_id,
      headline: row.headline,
      slug: row.slug,
      status: row.status,
      articleType: row.article_type,
      category: row.category_slug,
      riskTier: row.risk_tier,
      editorialConfidence: row.editorial_confidence,
      publishedAt: pyIso(row.published_at_iso),
      createdAt: pyIso(row.created_at_iso),
    })),
    total,
    page,
    pageSize,
  };
}

/* ----------------------------------------------------------------- settings */

interface SettingRow {
  key: string;
  value: unknown;
  description: string | null;
  is_protected: boolean;
}

export async function listSettings(): Promise<Record<string, unknown>[]> {
  const rows = await query<SettingRow>(
    `SELECT key, value, description, is_protected FROM settings ORDER BY key`,
  );
  return rows.map((row) => ({
    key: row.key,
    value: row.value,
    description: row.description,
    isProtected: row.is_protected,
  }));
}

/* ------------------------------------------------------------------ metrics */

/** Two missed 30s heartbeats. Matches the constant in the Python route. */
const STALE_HEARTBEAT_SECONDS = 90;

interface WorkerRow {
  name: string;
  status: string;
  last_heartbeat_at_iso: string | null;
  heartbeat_age_seconds: number | null;
  current_job_count: number;
  gpu_name: string | null;
  gpu_vram_free_mb: number | null;
  agent_version: string | null;
}

export async function systemMetrics(redisOk: boolean): Promise<Record<string, unknown>> {
  const [articleCounts, jobCounts, publishedToday, oldest, workers] = await Promise.all([
    query<{ status: string; count: string }>(
      `SELECT status, count(*) AS count FROM articles
        WHERE deleted_at IS NULL GROUP BY status`,
    ),
    query<{ status: string; count: string }>(
      `SELECT status, count(*) AS count FROM jobs GROUP BY status`,
    ),
    // Python compares against `now` truncated to midnight UTC, which is what
    // date_trunc gives for a UTC session.
    queryOne<{ count: string }>(
      `SELECT count(*) AS count FROM articles
        WHERE status = 'published'
          AND published_at >= date_trunc('day', now() AT TIME ZONE 'UTC')`,
    ),
    queryOne<{ age: number | null }>(
      `SELECT floor(extract(epoch FROM (now() - min(created_at))))::int AS age
         FROM jobs WHERE status = 'queued'`,
    ),
    query<WorkerRow>(
      `SELECT
         name,
         status,
         ${isoColumn("last_heartbeat_at", "last_heartbeat_at_iso")},
         extract(epoch FROM (now() - last_heartbeat_at)) AS heartbeat_age_seconds,
         current_job_count,
         gpu_name,
         gpu_vram_free_mb,
         agent_version
       FROM worker_nodes WHERE is_active = true`,
    ),
  ]);

  const byStatus = (rows: { status: string; count: string }[]): Record<string, number> =>
    Object.fromEntries(rows.map((r) => [r.status, Number(r.count)]));

  const jobs = byStatus(jobCounts);

  return {
    generatedAt: pyIsoNow(),
    articles: {
      byStatus: byStatus(articleCounts),
      publishedToday: Number(publishedToday?.count ?? 0),
    },
    jobs: {
      byStatus: jobs,
      queueDepth: jobs.queued ?? 0,
      oldestQueuedJobAgeSeconds: oldest?.age ?? null,
    },
    workers: workers.map((w) => ({
      name: w.name,
      // A node that has never checked in, or has been silent for more than two
      // heartbeat intervals, reads as offline regardless of its stored status.
      status:
        w.heartbeat_age_seconds === null || w.heartbeat_age_seconds > STALE_HEARTBEAT_SECONDS
          ? "offline"
          : w.status,
      lastHeartbeatAt: pyIso(w.last_heartbeat_at_iso),
      currentJobCount: w.current_job_count,
      gpuName: w.gpu_name,
      gpuVramFreeMb: w.gpu_vram_free_mb,
      agentVersion: w.agent_version,
    })),
    redis: redisOk,
  };
}
