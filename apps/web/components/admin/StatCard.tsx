export function StatCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: number | string;
  hint?: string;
}) {
  return (
    <div
      className="rounded-lg border border-line bg-surface p-5"
      style={{ boxShadow: "var(--highlight-top)" }}
    >
      <p className="meta">{label}</p>
      <p className="display mt-2 text-3xl tabular-nums">{value}</p>
      {hint && <p className="dek mt-1 text-xs">{hint}</p>}
    </div>
  );
}
