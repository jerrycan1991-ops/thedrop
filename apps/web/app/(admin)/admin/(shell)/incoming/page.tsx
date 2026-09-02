import Link from "next/link";

import { StatCard } from "@/components/admin/StatCard";
import { AdminError, AdminHeader, loadForAdmin, relativeTime } from "@/lib/admin-page";
import { ingestSummary, listIncoming } from "@/lib/db/queries/ingest";

export const dynamic = "force-dynamic";

const DEDUP_FILTERS = ["unique", "near_duplicate", "exact_duplicate", "pending"] as const;

/**
 * Everything ingestion has captured, newest first.
 *
 * These rows are EVIDENCE, not content. Nothing here is rendered to a reader: articles
 * are generated from a structured evidence packet, never rewritten from source prose
 * (CLAUDE.md, copyright). The screen exists so an operator can see what arrived, what
 * was deduplicated, and what the injection scan flagged.
 *
 * Flagged rows are kept and shown rather than hidden. Text that addresses the system is
 * evidence of an attempt, and deleting it would destroy the audit trail while making
 * the screen look tidier (SECURITY.md 6.2).
 */
export default async function IncomingPage({
  searchParams,
}: {
  searchParams: Promise<{ dedup?: string; flagged?: string; page?: string }>;
}) {
  const params = await searchParams;
  const dedupFilter = params.dedup ?? null;
  const flaggedOnly = params.flagged === "1";
  const page = Number.parseInt(params.page ?? "1", 10) || 1;

  const result = await loadForAdmin(
    async () => ({
      summary: await ingestSummary(),
      list: await listIncoming({ dedupFilter, flaggedOnly, page, pageSize: 50 }),
    }),
    "incoming stories",
  );
  if ("error" in result) return <AdminError title="Incoming Stories" message={result.error} />;

  const { summary, list } = result.data;
  const pages = Math.max(1, Math.ceil(list.total / list.pageSize));

  const filterHref = (next: Record<string, string | null>) => {
    const sp = new URLSearchParams();
    const dedup = "dedup" in next ? next.dedup : dedupFilter;
    const flagged = "flagged" in next ? next.flagged : flaggedOnly ? "1" : null;
    if (dedup) sp.set("dedup", dedup);
    if (flagged) sp.set("flagged", flagged);
    const qs = sp.toString();
    return qs ? `/admin/incoming?${qs}` : "/admin/incoming";
  };

  return (
    <div className="p-6 lg:p-8">
      <AdminHeader
        title="Incoming Stories"
        subtitle={`${summary.total} captured · ${summary.lastHour} in the last hour · ${summary.lastDay} in 24h`}
      />

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Unique" value={summary.byDedupStatus.unique ?? 0} />
        <StatCard
          label="Duplicates"
          value={
            (summary.byDedupStatus.exact_duplicate ?? 0) +
            (summary.byDedupStatus.near_duplicate ?? 0)
          }
          hint="Kept: four sources on one story is a signal"
        />
        <StatCard
          label="Injection flags"
          value={summary.flagged}
          hint="Recorded, never deleted"
        />
        <StatCard
          label="Awaiting embedding"
          value={summary.awaitingEmbedding}
          hint="Desktop work — the VPS never embeds"
        />
      </section>

      <nav className="mt-8 flex flex-wrap items-center gap-2 text-xs" aria-label="Filters">
        <Link
          href={filterHref({ dedup: null })}
          className={`rounded-md border border-line px-2 py-1 ${!dedupFilter ? "bg-surface font-medium" : "text-muted"}`}
        >
          All
        </Link>
        {DEDUP_FILTERS.map((status) => (
          <Link
            key={status}
            href={filterHref({ dedup: status })}
            className={`rounded-md border border-line px-2 py-1 ${dedupFilter === status ? "bg-surface font-medium" : "text-muted"}`}
          >
            {status.replace("_", " ")}
          </Link>
        ))}
        <Link
          href={filterHref({ flagged: flaggedOnly ? null : "1" })}
          className={`rounded-md border border-line px-2 py-1 ${flaggedOnly ? "bg-surface font-medium text-warning" : "text-muted"}`}
        >
          flagged only
        </Link>
      </nav>

      {list.items.length === 0 ? (
        <div className="mt-6 max-w-lg rounded-lg border border-dashed border-line-strong bg-surface p-6">
          <p className="headline text-base">Nothing matches</p>
          <p className="dek mt-2 text-sm">
            {summary.total === 0
              ? "Ingestion has captured nothing yet. Check Providers."
              : "No rows for this filter."}
          </p>
        </div>
      ) : (
        <div className="mt-6 overflow-x-auto rounded-lg border border-line">
          <table className="w-full min-w-[56rem] text-sm">
            <thead className="bg-surface text-left">
              <tr className="meta">
                <th className="px-4 py-2 font-medium">Title</th>
                <th className="px-4 py-2 font-medium">Source</th>
                <th className="px-4 py-2 font-medium">Dedup</th>
                <th className="px-4 py-2 font-medium">Flags</th>
                <th className="px-4 py-2 font-medium">Discovered</th>
              </tr>
            </thead>
            <tbody>
              {list.items.map((item) => {
                const patterns = (item.injection_flags?.patterns as string[]) ?? [];
                return (
                  <tr key={item.public_id} className="border-t border-line align-top">
                    <td className="max-w-md px-4 py-3">
                      <a
                        href={item.canonical_url}
                        target="_blank"
                        rel="noreferrer nofollow"
                        className="font-medium hover:underline"
                      >
                        {item.title}
                      </a>
                      <div className="meta">{item.provider_slug}</div>
                    </td>
                    <td className="px-4 py-3">
                      {item.source_domain}
                      {item.source_authority && (
                        <div className="meta text-success">primary authority</div>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span className={item.dedup_status === "unique" ? undefined : "text-subtle"}>
                        {item.dedup_status.replace("_", " ")}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs">
                      {patterns.length > 0 ? (
                        <span className="text-warning">{patterns.join(", ")}</span>
                      ) : (
                        <span className="text-subtle">clean</span>
                      )}
                    </td>
                    <td className="px-4 py-3">{relativeTime(item.discovered_at_iso)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {pages > 1 && (
        <div className="mt-4 flex items-center gap-3 text-xs">
          <span className="meta">
            Page {list.page} of {pages}
          </span>
          {list.page > 1 && (
            <Link className="hover:underline" href={`${filterHref({})}${filterHref({}).includes("?") ? "&" : "?"}page=${list.page - 1}`}>
              ← Newer
            </Link>
          )}
          {list.page < pages && (
            <Link className="hover:underline" href={`${filterHref({})}${filterHref({}).includes("?") ? "&" : "?"}page=${list.page + 1}`}>
              Older →
            </Link>
          )}
        </div>
      )}
    </div>
  );
}
