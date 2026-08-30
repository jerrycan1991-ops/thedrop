import { headers } from "next/headers";

import { StatCard } from "@/components/admin/StatCard";
import { WorkerStatusCard } from "@/components/admin/WorkerStatusCard";

interface SystemMetrics {
  generatedAt: string;
  articles: { byStatus: Record<string, number>; publishedToday: number };
  jobs: {
    byStatus: Record<string, number>;
    queueDepth: number;
    oldestQueuedJobAgeSeconds: number | null;
  };
  workers: {
    name: string;
    status: string;
    lastHeartbeatAt: string | null;
    currentJobCount: number;
    gpuName: string | null;
    gpuVramFreeMb: number | null;
    agentVersion: string | null;
  }[];
  redis: boolean;
}

/**
 * Fetches admin metrics with the caller's cookies forwarded.
 *
 * The API is the security boundary — it re-validates the session server-side. If it
 * says 401, we surface that rather than rendering a shell full of zeros, because a
 * dashboard that silently shows nothing is indistinguishable from a healthy quiet system.
 */
async function getMetrics(): Promise<SystemMetrics | { error: string }> {
  const apiBase = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000";
  const cookie = (await headers()).get("cookie") ?? "";

  try {
    const response = await fetch(`${apiBase}/api/v1/admin/system/metrics`, {
      headers: { cookie },
      cache: "no-store",
    });
    if (response.status === 401) return { error: "Session expired. Sign in again." };
    if (!response.ok) return { error: `API returned ${response.status}` };
    return (await response.json()) as SystemMetrics;
  } catch {
    return { error: "Cannot reach the API. Is thedrop-api running?" };
  }
}

export default async function AdminDashboard() {
  const metrics = await getMetrics();

  if ("error" in metrics) {
    return (
      <div className="p-8">
        <h1 className="display text-2xl">Dashboard</h1>
        <p className="mt-6 rounded-md border border-danger/30 bg-danger-subtle px-4 py-3 text-sm text-danger">
          {metrics.error}
        </p>
      </div>
    );
  }

  const byStatus = metrics.articles.byStatus;

  return (
    <div className="p-6 lg:p-8">
      <header className="mb-8">
        <h1 className="display text-2xl">Dashboard</h1>
        <p className="meta mt-1">
          Live · generated {new Date(metrics.generatedAt).toLocaleTimeString("en-US")}
        </p>
      </header>

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Published today" value={metrics.articles.publishedToday} />
        <StatCard label="Drafts" value={byStatus.draft ?? 0} />
        <StatCard label="In QA" value={byStatus.qa ?? 0} />
        <StatCard label="Rejected" value={byStatus.rejected ?? 0} />
      </section>

      <section className="mt-8 grid gap-6 lg:grid-cols-2">
        <div>
          <h2 className="meta mb-3">AI desktop</h2>
          {metrics.workers.length === 0 ? (
            <div className="rounded-lg border border-dashed border-line-strong bg-surface p-5">
              <p className="text-sm font-medium">No worker registered</p>
              <p className="dek mt-1 text-sm">
                The RTX 4070 SUPER runner registers itself on first heartbeat (Phase 2).
                Until then, AI jobs queue and nothing is published — by design.
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              {metrics.workers.map((worker) => (
                <WorkerStatusCard key={worker.name} worker={worker} />
              ))}
            </div>
          )}
        </div>

        <div>
          <h2 className="meta mb-3">Queue</h2>
          <div className="rounded-lg border border-line bg-surface p-5">
            <div className="flex items-baseline gap-2">
              <span className="display text-3xl">{metrics.jobs.queueDepth}</span>
              <span className="dek text-sm">jobs waiting</span>
            </div>
            {metrics.jobs.oldestQueuedJobAgeSeconds !== null && (
              <p className="meta mt-2">
                Oldest waiting {Math.round(metrics.jobs.oldestQueuedJobAgeSeconds / 60)}m
              </p>
            )}
            <dl className="mt-4 grid grid-cols-2 gap-2 text-sm">
              {Object.entries(metrics.jobs.byStatus).map(([status, count]) => (
                <div key={status} className="flex justify-between border-b border-line py-1">
                  <dt className="text-muted capitalize">{status}</dt>
                  <dd className="font-medium tabular-nums">{count}</dd>
                </div>
              ))}
            </dl>
          </div>

          <div className="mt-4 flex items-center gap-2 rounded-lg border border-line bg-surface px-5 py-3">
            <span
              className="h-2 w-2 rounded-full"
              style={{ backgroundColor: metrics.redis ? "var(--positive)" : "var(--danger)" }}
              aria-hidden="true"
            />
            <span className="text-sm">Redis {metrics.redis ? "connected" : "unreachable"}</span>
          </div>
        </div>
      </section>
    </div>
  );
}
