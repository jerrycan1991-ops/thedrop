import "server-only";

import { query, queryOne } from "@/lib/db/client";
import { isoColumn } from "@/lib/db/datetime";

/**
 * Read queries for the Phase 2 ingestion screens: Incoming Stories, Sources, Providers.
 *
 * No Python counterpart. Ingestion was written after Node took the HTTP layer
 * (ADR-0010), so these are originals rather than transliterations, and nothing in
 * `api_baseline.py` compares them.
 *
 * Read-only, parameterized, `server-only`. Everything an operator does to ingestion —
 * enabling a provider, resetting a poll window — goes through the CLIs on the VPS,
 * because those validate the feed before writing. A button in a browser that skipped
 * that validation would be a worse tool with a better interface.
 */

/* ------------------------------------------------------------------ incoming */

interface IncomingRow {
  public_id: string;
  title: string;
  canonical_url: string;
  dedup_status: string;
  ingest_status: string;
  discovered_at_iso: string;
  published_at_iso: string;
  language: string;
  injection_flags: Record<string, unknown>;
  simhash: string | null;
  embedded_at_iso: string | null;
  source_domain: string;
  source_authority: boolean;
  provider_slug: string;
}

export interface IncomingList {
  items: IncomingRow[];
  total: number;
  page: number;
  pageSize: number;
}

/**
 * Most recently discovered first — the order an operator watching ingestion wants.
 *
 * `dedupFilter` accepts the enum values; anything else is ignored rather than
 * interpolated, so a hand-edited query string cannot reach the SQL.
 */
export async function listIncoming(options: {
  dedupFilter: string | null;
  flaggedOnly: boolean;
  page: number;
  pageSize: number;
}): Promise<IncomingList> {
  const page = Math.max(1, options.page);
  const pageSize = Math.min(Math.max(1, options.pageSize), 100);
  const offset = (page - 1) * pageSize;

  const allowed = ["pending", "unique", "near_duplicate", "exact_duplicate"];
  const dedup = options.dedupFilter && allowed.includes(options.dedupFilter)
    ? options.dedupFilter
    : null;

  // `injection_flags->'patterns'` is a JSONB array; a non-empty one means the scan
  // matched something. Empty means scanned and clean, which is why the column is NOT
  // NULL — see SECURITY.md 6.2.
  const where: string[] = [];
  const params: unknown[] = [];

  if (dedup) {
    params.push(dedup);
    where.push(`ra.dedup_status = $${params.length}`);
  }
  if (options.flaggedOnly) {
    where.push(`jsonb_array_length(coalesce(ra.injection_flags->'patterns', '[]'::jsonb)) > 0`);
  }
  const whereSql = where.length ? `WHERE ${where.join(" AND ")}` : "";

  const totalRow = await queryOne<{ total: string }>(
    `SELECT count(*)::text AS total FROM raw_articles ra ${whereSql}`,
    params,
  );

  params.push(pageSize, offset);
  const items = await query<IncomingRow>(
    `SELECT ra.public_id::text AS public_id,
            ra.title,
            ra.canonical_url,
            ra.dedup_status,
            ra.ingest_status,
            ${isoColumn("ra.discovered_at", "discovered_at_iso")},
            ${isoColumn("ra.published_at", "published_at_iso")},
            ra.language,
            ra.injection_flags,
            ra.simhash::text AS simhash,
            ${isoColumn("ra.embedded_at", "embedded_at_iso")},
            s.domain AS source_domain,
            s.is_primary_authority AS source_authority,
            p.slug AS provider_slug
       FROM raw_articles ra
       JOIN sources s ON s.id = ra.source_id
       JOIN providers p ON p.id = ra.provider_id
       ${whereSql}
      ORDER BY ra.discovered_at DESC, ra.id DESC
      LIMIT $${params.length - 1} OFFSET $${params.length}`,
    params,
  );

  return { items, total: Number(totalRow?.total ?? 0), page, pageSize };
}

export interface IngestSummary {
  total: number;
  byDedupStatus: Record<string, number>;
  flagged: number;
  lastDay: number;
  lastHour: number;
  awaitingEmbedding: number;
}

/**
 * Counters for the Incoming header.
 *
 * `awaitingEmbedding` is the queue the desktop will drain in Phase 3 (ADR-0005): rows
 * the VPS has stored and deliberately not embedded, because it never computes one.
 */
export async function ingestSummary(): Promise<IngestSummary> {
  const rows = await query<{ dedup_status: string; n: string }>(
    `SELECT dedup_status, count(*)::text AS n FROM raw_articles GROUP BY dedup_status`,
  );
  const totals = await queryOne<{
    total: string;
    flagged: string;
    last_day: string;
    last_hour: string;
    awaiting: string;
  }>(
    `SELECT count(*)::text AS total,
            count(*) FILTER (
              WHERE jsonb_array_length(coalesce(injection_flags->'patterns','[]'::jsonb)) > 0
            )::text AS flagged,
            count(*) FILTER (WHERE discovered_at > now() - interval '24 hours')::text AS last_day,
            count(*) FILTER (WHERE discovered_at > now() - interval '1 hour')::text AS last_hour,
            count(*) FILTER (WHERE embedding IS NULL)::text AS awaiting
       FROM raw_articles`,
  );

  const byDedupStatus: Record<string, number> = {};
  for (const row of rows) byDedupStatus[row.dedup_status] = Number(row.n);

  return {
    total: Number(totals?.total ?? 0),
    byDedupStatus,
    flagged: Number(totals?.flagged ?? 0),
    lastDay: Number(totals?.last_day ?? 0),
    lastHour: Number(totals?.last_hour ?? 0),
    awaitingEmbedding: Number(totals?.awaiting ?? 0),
  };
}

/* ------------------------------------------------------------------- sources */

export interface SourceRow {
  id: number;
  domain: string;
  name: string;
  source_type: string;
  reliability_score: string;
  is_primary_authority: boolean;
  allow_auto_publish: boolean;
  bias_label: string | null;
  article_count: string;
  last_seen_iso: string | null;
}

/** Busiest first: the sources actually feeding the pipeline are the ones to look at. */
export async function listSources(): Promise<SourceRow[]> {
  return query<SourceRow>(
    `SELECT s.id,
            s.domain,
            s.name,
            s.source_type,
            s.reliability_score::text AS reliability_score,
            s.is_primary_authority,
            s.allow_auto_publish,
            s.bias_label,
            count(ra.id)::text AS article_count,
            ${isoColumn("max(ra.discovered_at)", "last_seen_iso")}
       FROM sources s
       LEFT JOIN raw_articles ra ON ra.source_id = s.id
      GROUP BY s.id
      ORDER BY count(ra.id) DESC, s.domain ASC`,
  );
}

/* ----------------------------------------------------------------- providers */

export interface ProviderRow {
  id: number;
  slug: string;
  display_name: string;
  adapter_class: string;
  enabled: boolean;
  poll_interval_minutes: number;
  circuit_state: string;
  consecutive_failures: number;
  last_success_iso: string | null;
  last_error_iso: string | null;
  last_error: string | null;
  config: Record<string, unknown>;
  article_count: string;
}

/**
 * Providers with their circuit-breaker state and how much each has actually produced.
 *
 * The article count is the honest measure of a feed: a provider can poll cleanly
 * forever and store nothing, which looks identical to health in every other column.
 * fed-press did exactly that for its first three polls.
 */
export async function listProviders(): Promise<ProviderRow[]> {
  return query<ProviderRow>(
    `SELECT p.id,
            p.slug,
            p.display_name,
            p.adapter_class,
            p.enabled,
            p.poll_interval_minutes,
            p.circuit_state,
            p.consecutive_failures,
            ${isoColumn("p.last_success_at", "last_success_iso")},
            ${isoColumn("p.last_error_at", "last_error_iso")},
            p.last_error,
            p.config,
            count(ra.id)::text AS article_count
       FROM providers p
       LEFT JOIN raw_articles ra ON ra.provider_id = p.id
      GROUP BY p.id
      ORDER BY p.enabled DESC, p.slug ASC`,
  );
}
