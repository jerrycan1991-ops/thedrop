import { AdminError, AdminHeader, loadForAdmin, relativeTime } from "@/lib/admin-page";
import { listProviders } from "@/lib/db/queries/ingest";

export const dynamic = "force-dynamic";

/**
 * Providers, with circuit-breaker state and what each has actually produced.
 *
 * Read-only on purpose. Enabling a feed or resetting its poll window goes through
 * `thedrop_ingest.add_provider` on the VPS, which fetches and parses the feed before
 * writing the row. A button here that skipped that check would be a worse tool behind
 * a better interface -- a bad URL would surface days later as a tripped breaker.
 */
export default async function ProvidersPage() {
  const result = await loadForAdmin(listProviders, "providers");
  if ("error" in result) return <AdminError title="Providers" message={result.error} />;

  const providers = result.data;
  const enabled = providers.filter((p) => p.enabled).length;
  const tripped = providers.filter((p) => p.circuit_state !== "closed").length;

  return (
    <div className="p-6 lg:p-8">
      <AdminHeader
        title="Providers"
        subtitle={`${providers.length} configured · ${enabled} enabled${tripped ? ` · ${tripped} circuit open` : ""}`}
      />

      {providers.length === 0 ? (
        <div className="max-w-lg rounded-lg border border-dashed border-line-strong bg-surface p-6">
          <p className="headline text-base">No providers configured</p>
          <p className="dek mt-2 text-sm">
            Add one on the VPS with <code>python -m thedrop_ingest.add_provider</code>. It
            validates the feed before writing the row.
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-line">
          <table className="w-full min-w-[52rem] text-sm">
            <thead className="bg-surface text-left">
              <tr className="meta">
                <th className="px-4 py-2 font-medium">Provider</th>
                <th className="px-4 py-2 font-medium">State</th>
                <th className="px-4 py-2 font-medium">Every</th>
                <th className="px-4 py-2 font-medium">Last success</th>
                <th className="px-4 py-2 font-medium tabular-nums">Articles</th>
                <th className="px-4 py-2 font-medium">Last error</th>
              </tr>
            </thead>
            <tbody>
              {providers.map((p) => {
                const stored = Number(p.article_count);
                return (
                  <tr key={p.id} className="border-t border-line align-top">
                    <td className="px-4 py-3">
                      <div className="font-medium">{p.display_name}</div>
                      <div className="meta">{p.slug}</div>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={
                          !p.enabled
                            ? "text-subtle"
                            : p.circuit_state === "closed"
                              ? "text-success"
                              : "text-danger"
                        }
                      >
                        {!p.enabled ? "disabled" : p.circuit_state}
                      </span>
                      {p.consecutive_failures > 0 && (
                        <div className="meta">{p.consecutive_failures} consecutive failures</div>
                      )}
                    </td>
                    <td className="px-4 py-3 tabular-nums">{p.poll_interval_minutes}m</td>
                    <td className="px-4 py-3">{relativeTime(p.last_success_iso)}</td>
                    <td className="px-4 py-3 tabular-nums">
                      {stored}
                      {/* A provider can poll cleanly forever and store nothing, which
                          looks identical to health in every other column. */}
                      {stored === 0 && p.last_success_iso && (
                        <div className="meta text-warning">polling, storing nothing</div>
                      )}
                    </td>
                    <td className="max-w-xs px-4 py-3 text-xs text-muted">
                      {p.last_error ? p.last_error.slice(0, 140) : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
