import { relativeTime } from "@/lib/utils";

interface Worker {
  name: string;
  status: string;
  lastHeartbeatAt: string | null;
  currentJobCount: number;
  gpuName: string | null;
  gpuVramFreeMb: number | null;
  agentVersion: string | null;
}

const STATUS_TOKEN: Record<string, string> = {
  online: "var(--positive)",
  degraded: "var(--warning)",
  offline: "var(--danger)",
};

/**
 * The desktop's status.
 *
 * OFFLINE is not an outage — the site stays up, ingestion continues and jobs queue.
 * The card says so explicitly, because a red dot with no context invites someone to
 * go looking for a problem that is not there.
 */
export function WorkerStatusCard({ worker }: { worker: Worker }) {
  const offline = worker.status === "offline";

  return (
    <div className="rounded-lg border border-line bg-surface p-5">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span
            className="h-2.5 w-2.5 rounded-full"
            style={{ backgroundColor: STATUS_TOKEN[worker.status] ?? "var(--fg-subtle)" }}
            aria-hidden="true"
          />
          <span className="font-medium">{worker.name}</span>
        </div>
        <span className="meta">{worker.status}</span>
      </div>

      <dl className="mt-4 space-y-1.5 text-sm">
        <div className="flex justify-between">
          <dt className="text-muted">Last heartbeat</dt>
          <dd>{worker.lastHeartbeatAt ? relativeTime(worker.lastHeartbeatAt) : "never"}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-muted">Active jobs</dt>
          <dd className="tabular-nums">{worker.currentJobCount}</dd>
        </div>
        {worker.gpuName && (
          <div className="flex justify-between">
            <dt className="text-muted">GPU</dt>
            <dd className="truncate pl-4">{worker.gpuName}</dd>
          </div>
        )}
        {worker.gpuVramFreeMb !== null && (
          <div className="flex justify-between">
            <dt className="text-muted">VRAM free</dt>
            <dd className="tabular-nums">{Math.round(worker.gpuVramFreeMb / 1024)} GB</dd>
          </div>
        )}
        {worker.agentVersion && (
          <div className="flex justify-between">
            <dt className="text-muted">Agent</dt>
            <dd>{worker.agentVersion}</dd>
          </div>
        )}
      </dl>

      {offline && (
        <p className="dek mt-4 text-xs">
          The site is unaffected. Ingestion continues and jobs queue until the desktop
          returns.
        </p>
      )}
    </div>
  );
}
