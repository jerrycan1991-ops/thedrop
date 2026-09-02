import { AdminError, AdminHeader, loadForAdmin, relativeTime } from "@/lib/admin-page";
import { listSources } from "@/lib/db/queries/ingest";

export const dynamic = "force-dynamic";

/**
 * Publishers seen by ingestion, busiest first.
 *
 * Two columns carry weight beyond their size.
 *
 * `Authority` comes from the TLD -- a .gov or .mil domain is a primary authority as a
 * matter of fact, and CLAUDE.md lets one satisfy a high-risk story on its own.
 * `Auto-publish` is a judgement nobody has made yet, so every source starts false. A
 * source can corroborate long before it is trusted to stand alone.
 *
 * Rows that are the same organisation under different hostnames are expected, not a
 * data-quality problem: nasa.gov and science.nasa.gov are two sources and one witness.
 * See ADR-0013 -- nothing may infer independence from source identity.
 */
export default async function SourcesPage() {
  const result = await loadForAdmin(listSources, "sources");
  if ("error" in result) return <AdminError title="Sources" message={result.error} />;

  const sources = result.data;
  const authorities = sources.filter((s) => s.is_primary_authority).length;
  const unclassified = sources.filter((s) => s.source_type === "unknown").length;

  return (
    <div className="p-6 lg:p-8">
      <AdminHeader
        title="Sources"
        subtitle={`${sources.length} publishers · ${authorities} primary authorities · ${unclassified} unclassified`}
      />

      {sources.length === 0 ? (
        <div className="max-w-lg rounded-lg border border-dashed border-line-strong bg-surface p-6">
          <p className="headline text-base">No sources yet</p>
          <p className="dek mt-2 text-sm">
            Sources are created automatically the first time an article arrives from a
            domain. Add a provider and wait for a poll.
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-line">
          <table className="w-full min-w-[52rem] text-sm">
            <thead className="bg-surface text-left">
              <tr className="meta">
                <th className="px-4 py-2 font-medium">Domain</th>
                <th className="px-4 py-2 font-medium">Type</th>
                <th className="px-4 py-2 font-medium">Authority</th>
                <th className="px-4 py-2 font-medium">Auto-publish</th>
                <th className="px-4 py-2 font-medium tabular-nums">Reliability</th>
                <th className="px-4 py-2 font-medium tabular-nums">Articles</th>
                <th className="px-4 py-2 font-medium">Last seen</th>
              </tr>
            </thead>
            <tbody>
              {sources.map((s) => (
                <tr key={s.id} className="border-t border-line">
                  <td className="px-4 py-3 font-medium">{s.domain}</td>
                  <td className="px-4 py-3">
                    <span className={s.source_type === "unknown" ? "text-subtle" : undefined}>
                      {s.source_type}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {s.is_primary_authority ? (
                      <span className="text-success">primary</span>
                    ) : (
                      <span className="text-subtle">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {s.allow_auto_publish ? (
                      <span className="text-warning">allowed</span>
                    ) : (
                      <span className="text-subtle">no</span>
                    )}
                  </td>
                  <td className="px-4 py-3 tabular-nums">{s.reliability_score}</td>
                  <td className="px-4 py-3 tabular-nums">{s.article_count}</td>
                  <td className="px-4 py-3">{relativeTime(s.last_seen_iso)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="dek mt-4 max-w-2xl text-xs">
        Reliability is the model default until a source is classified. It is never
        guessed from the domain — being a primary authority is a fact about the TLD,
        trustworthiness is not.
      </p>
    </div>
  );
}
